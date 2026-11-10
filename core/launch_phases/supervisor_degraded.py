#!/usr/bin/env python3
"""launch_phases/supervisor_degraded.py - 降级探针治理、自愈修复、突变门禁与工具链豁免。"""

from __future__ import annotations

import shlex

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from collections.abc import Mapping
from core.launch_phases.common import (
    resolve_project_root,
    uses_project_layout,
    supervisor_state_path,
    child_environment,
    run_child,
    bugx_paths,
)
from core.launch_phases.session_runtime import (
    load_runtime,
    save_runtime,
    iso_time,
    runtime_lock,
)
from core.repair import self_heal
from core.host_runtime import host_registry
from core.runtime.tool_install_targets import PROBE_REPAIR_TARGETS



def ask_interaction(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "ask_interaction"):
        fn = getattr(sb, "ask_interaction")
        if fn is not ask_interaction:
            return fn(*args, **kwargs)
    from core.launch_phases.startup_interaction import ask_interaction as real
    return real(*args, **kwargs)

def answered_values(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "answered_values"):
        fn = getattr(sb, "answered_values")
        if fn is not answered_values:
            return fn(*args, **kwargs)
    from core.launch_phases.startup_interaction import answered_values as real
    return real(*args, **kwargs)

def answered_value(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "answered_value"):
        fn = getattr(sb, "answered_value")
        if fn is not answered_value:
            return fn(*args, **kwargs)
    from core.launch_phases.startup_interaction import answered_value as real
    return real(*args, **kwargs)

def _degraded_install(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "_degraded_install"):
        fn = getattr(sb, "_degraded_install")
        if fn is not _degraded_install:
            return fn(*args, **kwargs)
    return degraded_install(*args, **kwargs)


def run_preflight_probes(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "run_preflight_probes"):
        return sb.run_preflight_probes(*args, **kwargs)
    raise RuntimeError("supervisor_bridge.run_preflight_probes not available")


class _AgentDirProxy:
    def _target(self):
        sb = _sb()
        if sb and hasattr(sb, "AGENT_DIR"):
            return getattr(sb, "AGENT_DIR")
        from core.launch_phases.common import AGENT_DIR as real
        return real
    def resolve(self):
        return self._target().resolve()
    def __getattr__(self, name):
        return getattr(self._target(), name)

AGENT_DIR = _AgentDirProxy()

def _sb():
    return sys.modules.get("core.launch_phases.supervisor_bridge") or sys.modules.get("launch_phases.supervisor_bridge")

def _get_sb_attr(name: str, fallback=None):
    sb = _sb()
    if sb and hasattr(sb, name):
        return getattr(sb, name)
    return fallback

def _resolve_project_root(*args, **kwargs):
    fn = _get_sb_attr("resolve_project_root", resolve_project_root)
    return fn(*args, **kwargs)

def degraded_actions(report: dict, *, probe_name: str = "") -> list[dict]:
    """从探针 JSON 报告提取降级项与对应处置选项。

    MCP 缺失/未配置必须进入专用交互菜单。``probe_name`` 用来保留探针
    的组件语义：codebase-memory-mcp 的子检查名称是 binary/cli，单看子
    检查名无法识别它属于 MCP。
    """
    from core.probes import probe_registry
    return probe_registry.extract_degraded_actions(report, probe_name=probe_name)

_LAST_SELF_HEAL_REPORT: self_heal.HealReport | None = None

# C1 decisions are deliberately separate from their human-readable labels.
# A display string can be copied from a prompt or supplied by a host; it must
# never be able to manufacture a successful gate result.
DEGRADED_HEALED = "healed"
DEGRADED_NOT_APPLICABLE = "not_applicable"
DEGRADED_EXEMPTED = "exempted"
DEGRADED_BLOCKED = "blocked"
DEGRADED_DEFERRED = "deferred"
DEGRADED_TRANSIENT = "transient"
DEGRADED_PASSING_STATUSES = frozenset({
    DEGRADED_HEALED, DEGRADED_NOT_APPLICABLE, DEGRADED_TRANSIENT,
})

_DEGRADED_HEALED = DEGRADED_HEALED
_DEGRADED_NOT_APPLICABLE = DEGRADED_NOT_APPLICABLE
_DEGRADED_EXEMPTED = DEGRADED_EXEMPTED
_DEGRADED_BLOCKED = DEGRADED_BLOCKED
_DEGRADED_DEFERRED = DEGRADED_DEFERRED
_DEGRADED_TRANSIENT = DEGRADED_TRANSIENT
_DEGRADED_PASSING_STATUSES = DEGRADED_PASSING_STATUSES


class DegradedDecision(str):
    """String-compatible display value carrying an internal typed C1 status."""

    __slots__ = ("status", "receipt")

    def __new__(
        cls, status: str, display: str, receipt: Mapping[str, Any] | None = None,
    ):
        value = str.__new__(cls, display)
        value.status = status
        value.receipt = dict(receipt or {})
        return value


def degraded_decision(
    status: str, display: str, receipt: Mapping[str, Any] | None = None,
) -> _DegradedDecision:
    return _DegradedDecision(status, display, receipt)


def degraded_item_not_applicable(item: Mapping[str, Any]) -> bool:
    """Return True only for an explicit machine-level applicability result."""
    status = str(item.get("status", "")).strip().casefold()
    applicability = str(item.get("applicability", "")).strip().casefold()
    return bool(
        item.get("not_applicable") is True
        or status == _DEGRADED_NOT_APPLICABLE
        or applicability == _DEGRADED_NOT_APPLICABLE
    )


def degraded_item_advisory(item: Mapping[str, Any]) -> bool:
    """描述符声明 ``required=False`` 的建议性探针（如 openwiki）。

    注册表已判定"缺失不阻断启动"，门禁必须尊重同一份机器声明；否则
    advisory 探针会从降级通道反向变成硬阻断，与它自己的注册契约冲突。
    """
    return item.get("advisory") is True


def degraded_item_transient(item: Mapping[str, Any]) -> bool:
    """二进制/插件**存在**但本次调用超时或异常（``transient`` 由注册中心置位）。"""
    return item.get("transient") is True


def degraded_item_critical(item: Mapping[str, Any]) -> bool:
    """能力是否 critical：以描述符机器声明为准，缺失时才回落到 ``kind=mcp`` 猜测。"""
    declared = item.get("critical")
    if isinstance(declared, bool):
        return declared
    name = str(item.get("name", "")).casefold()
    return bool(item.get("kind") == "mcp" or "mcp" in name)


def degraded_exemption_hit(name: str, exemptions: set[str]) -> bool:
    """豁免名匹配：支持完整条目名与探针级前缀（``codebase-memory-mcp/cli`` ← ``codebase-memory-mcp``）。"""
    lowered = name.casefold()
    if lowered in exemptions:
        return True
    probe = lowered.split("/", 1)[0].strip()
    return bool(probe) and probe in exemptions


def degraded_decision_for_policy(item: Mapping[str, Any]) -> _DegradedDecision | None:
    """按注册表机器语义给出**非阻断**决策；返回 None 表示仍需人工/自愈决策。

    顺序即优先级：显式不适用 → 建议性探针 → 瞬态降级。三者都必须留痕，
    且都不得借此启用任何能力（例如 codebase-memory 仍会被强制降级模式）。
    """
    name = str(item.get("name", "unknown"))
    detail = str(item.get("detail", ""))
    if _degraded_item_not_applicable(item):
        return _degraded_decision(
            _DEGRADED_NOT_APPLICABLE,
            f"不适用：{detail or '探针已按项目形态剪枝'}",
            {
                "source": "probe_applicability",
                "name": name,
                "status": _DEGRADED_NOT_APPLICABLE,
            },
        )
    if _degraded_item_advisory(item):
        return _degraded_decision(
            _DEGRADED_NOT_APPLICABLE,
            f"不阻断：建议性探针未就绪（{detail or '无详情'}）",
            {
                "source": "probe_advisory",
                "name": name,
                "status": _DEGRADED_NOT_APPLICABLE,
                "probe": item.get("probe", ""),
            },
        )
    if _degraded_item_transient(item) and not _degraded_item_critical(item):
        return _degraded_decision(
            _DEGRADED_TRANSIENT,
            f"瞬态降级：本次探测超时（{detail or '无详情'}）",
            {
                "source": "probe_transient",
                "name": name,
                "status": _DEGRADED_TRANSIENT,
                "probe": item.get("probe", ""),
            },
        )
    return None


def degraded_receipt_for_report(report: self_heal.HealReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    try:
        return report.as_dict()
    except (AttributeError, TypeError, ValueError):
        return {"status": "unserializable", "attempts": len(report.attempts)}


def report_self_heal(report: self_heal.HealReport) -> None:
    """打印穷尽自愈的处置清单，并留存供门禁在阻断时一并展示（DOC-236 C2）。"""
    global _LAST_SELF_HEAL_REPORT
    _LAST_SELF_HEAL_REPORT = report
    print(f"[launch_bugx] 穷尽自愈未能补齐（已试 {len(report.attempts)} 个方案）：")
    for line in self_heal.escalation_lines(report):
        print(f"[launch_bugx] {line}")


def emit_self_heal_evidence() -> None:
    """阻断时向人展示本轮已试过的自愈方案与失败原因（穷尽后上报）。"""
    report = _LAST_SELF_HEAL_REPORT
    if report is None or not report.attempts:
        return
    print("  已尝试的自愈方案（穷尽后上报）：")
    for line in self_heal.escalation_lines(report):
        print(f"  {line}")


def preflight_probe_id(item: dict) -> str:
    raw = str(item.get("name", "")).strip().casefold().split("/", 1)[0]
    normalized = raw.replace("_", "-")
    return "codebase-memory" if normalized == "codebase-memory-mcp" else normalized


def auto_provision_accelerators(
    root: Path,
    degraded: list[dict],
    *,
    required_tools: set[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """尝试自动装配/就绪缺失的环境加速器（DOC-155 满血自治就绪）。

    针对未建立索引的 codebase-memory-mcp 等本地已安装工具，在自治启动前
    自动触发就绪命令，而不是消极降级关闭。返回 (未能自动补齐的剩余降级项, 已成功就绪说明列表)。
    """
    if os.environ.get("BUGX_AUTO_PROVISION", "1").strip() in ("0", "false", "no"):
        return degraded, []

    remaining: list[dict] = []
    provisioned: list[str] = []

    selected_tools = {
        "codebase-memory" if str(value).strip().casefold().replace("_", "-")
        == "codebase-memory-mcp" else str(value).strip().casefold().replace("_", "-")
        for value in (required_tools or set())
    }

    for item in degraded:
        name = str(item.get("name", "")).strip().casefold()
        detail = str(item.get("detail", ""))
        probe_id = _preflight_probe_id(item)
        if required_tools is not None and probe_id not in selected_tools \
                and "supervisor" not in probe_id:
            remaining.append(item)
            continue

        # 1. codebase-memory-mcp 自动安装与自动建索引
        if probe_id == "codebase-memory":
            try:
                from core.probes import codebase_memory_probe

                probe_report = codebase_memory_probe.inspect(root)
                failed_checks = [
                    check for check in probe_report.get("checks", [])
                    if isinstance(check, dict) and check.get("ok") is False
                ] if isinstance(probe_report, dict) else []
                runtime_mismatch = any(
                    check.get("name") in {"binary", "validated_version", "supported_version"}
                    for check in failed_checks
                )
                if isinstance(probe_report, dict) and probe_report.get("status") == "READY":
                    msg = f"✓ {item.get('name')} 已由统一探针复核就绪"
                    print(f"[launch_bugx] {msg}")
                    provisioned.append(msg)
                    os.environ["BUGX_USE_CODEBASE_MEMORY"] = "1"
                    continue

                if runtime_mismatch:
                    print(
                        f"[launch_bugx] {item.get('name')} 缺少固定版本，"
                        "启动隔离安装自愈并按固定版本复核..."
                    )
                    healed, heal_report = self_heal.heal_tool(
                        "codebase-memory", detail=detail, root=root,
                    )
                    if not healed:
                        _report_self_heal(heal_report)
                    else:
                        # Installation success is not accelerator readiness: run
                        # the full version and project-index probe again.
                        probe_report = codebase_memory_probe.inspect(root)
                        if isinstance(probe_report, dict) \
                                and probe_report.get("status") == "READY":
                            msg = f"✓ {item.get('name')} 自愈后由统一探针复核就绪"
                            print(f"[launch_bugx] {msg}")
                            provisioned.append(msg)
                            os.environ["BUGX_USE_CODEBASE_MEMORY"] = "1"
                            continue
                        failed_checks = [
                            check for check in probe_report.get("checks", [])
                            if isinstance(check, dict) and check.get("ok") is False
                        ] if isinstance(probe_report, dict) else []

                check_names = [str(check.get("name", "unknown")) for check in failed_checks]
                if not runtime_mismatch:
                    check_names.extend(
                        str(check.get("name", "unknown"))
                        for check in probe_report.get("checks", [])
                        if isinstance(check, dict) and check.get("ok") is False
                    )
                failed = ", ".join(list(dict.fromkeys(check_names))[:4]) or str(
                    probe_report.get("status", "invalid-report")
                    if isinstance(probe_report, dict) else "invalid-report"
                )
                print(
                    f"[launch_bugx] Codebase Memory 复核未 READY（{failed}），保留降级选项"
                )
            except Exception as exc:
                print(
                    "[launch_bugx] Codebase Memory 自愈/复核异常 "
                    f"({type(exc).__name__})，保留降级选项"
                )

        # 2. bugx-supervisor / 宿主 MCP 自动就地配置注入
        if "supervisor" in probe_id or "bugx-supervisor" in probe_id:
            try:
                from bugx_cli.commands.plugin import write_host_mcp_config
                from core.supervisor import supervisor_host
                adapter = supervisor_host.detect_host(root)
                if adapter and host_registry.host_mcp_writable(adapter.name):
                    print(f"[launch_bugx] 发现宿主 {adapter.name} 缺失 supervisor MCP 配置，正在就地自愈注入...")
                    st, _, _, _ = write_host_mcp_config(root, adapter.name)
                    if st in {"configured", "created", "updated", "unchanged"}:
                        msg = f"✓ 宿主 {adapter.name} supervisor MCP 配置已自动就地自愈注入"
                        print(f"[launch_bugx] {msg}")
                        provisioned.append(msg)
                        continue
            except Exception as exc:
                print(f"[launch_bugx] 自动注入 supervisor MCP 配置异常: {exc}")

        # 3. 修复 SAST 工具只通过 self_heal 的固定 package spec 与复核器。
        if probe_id in {"sast", "bandit", "semgrep"}:
            try:
                from core.runtime import project_profile
                from core.probes import sast_probe
                profile = project_profile.detect_project_profile(root)
                languages = {str(lang).casefold() for lang in profile.get("languages", [])}
                targets = list(PROBE_REPAIR_TARGETS.get(probe_id, ()))
                if probe_id == "sast" and "python" not in languages:
                    targets = [tool for tool in targets if tool != "bandit"]

                satisfied = False
                for target in targets:
                    status = sast_probe.check_tool(target)
                    if status.get("ok"):
                        satisfied = True
                        if probe_id != "sast":
                            break
                        continue
                    healed, heal_report = self_heal.heal_tool(
                        target, detail=detail, root=root,
                    )
                    sast_probe.clear_tool_probe_cache()
                    status = sast_probe.check_tool(target)
                    if healed and status.get("ok"):
                        msg = (
                            f"✓ {target} 已由受信隔离配方安装并通过精确版本复核"
                        )
                        print(f"[launch_bugx] {msg}")
                        provisioned.append(msg)
                        satisfied = True
                        if probe_id != "sast":
                            break
                    else:
                        _report_self_heal(heal_report)

                if probe_id == "sast" and not satisfied:
                    satisfied = sast_probe.inspect(root).get("status") == "READY"
                if satisfied:
                    continue
            except Exception as exc:
                print(f"[launch_bugx] SAST 外部工具自愈/复核异常 ({type(exc).__name__})")

        # 4. 效率探针仅对已登记的 ast-grep 加速器运行隔离自愈。
        if probe_id in {"efficiency", "ast-grep"}:
            from core.probes import efficiency_probe
            status = efficiency_probe._command(("ast-grep", "sg"), "ast-grep")
            if not status.get("ok"):
                healed, heal_report = self_heal.heal_tool(
                    "ast-grep", detail=detail, root=root,
                )
                status = efficiency_probe._command(("ast-grep", "sg"), "ast-grep")
                if healed and status.get("ok"):
                    provisioned.extend(heal_report.healed)
                else:
                    _report_self_heal(heal_report)
            if probe_id == "ast-grep" and status.get("ok"):
                continue
            if probe_id == "efficiency" and efficiency_probe.inspect(root).get("status") == "READY":
                continue

        remaining.append(item)

    return remaining, provisioned


def autonomous_degraded_choices(
    degraded: list[dict],
) -> dict[str, _DegradedDecision]:
    """Classify every autonomous item before the shared gate.

    Autonomous mode has no user to authorize a missing capability.  A fallback
    option in a probe's display text is therefore never sufficient; only an
    explicit probe applicability result or a non-critical project exemption
    may produce an allowed decision.  Every other item receives an explicit
    blocked decision so the caller must route the complete set through the
    shared gate and cannot accidentally record part of it and continue.
    """
    choices: dict[str, _DegradedDecision] = {}
    exemptions = _tooling_exemptions()
    for item in degraded:
        name = str(item.get("name", "unknown"))
        policy_decision = _degraded_decision_for_policy(item)
        if policy_decision is not None:
            # 形态不适用 / 建议性探针 / 瞬态超时：注册表机器结论已足够，
            # 但决策与收据必须留痕（不得静默放行）。
            choices[name] = policy_decision
            continue
        is_critical = _degraded_item_critical(item)
        exempted = _degraded_exemption_hit(name, exemptions)
        if exempted and not is_critical:
            choices[name] = _degraded_decision(
                _DEGRADED_EXEMPTED,
                "显式豁免（config.json tooling_exemptions）",
                {
                    "source": "project_config",
                    "name": name,
                    "status": _DEGRADED_EXEMPTED,
                },
            )
            continue
        # No fallback option, display text, or critical exemption is enough
        # to let autonomous startup proceed; classify it for the shared gate.
        if is_critical and exempted:
            print(f"[launch_bugx] ✗ critical MCP/工具 {name} 不得豁免，自治启动阻断")
            reason = "critical_capability_cannot_be_exempted"
        else:
            reason = "autonomous_decision_required"
        choices[name] = _degraded_decision(
            _DEGRADED_BLOCKED,
            "阻断：自治模式未取得真实自愈/非适用/豁免结论",
            {
                "source": "autonomous_classification",
                "name": name,
                "attempted": False,
                "reason": reason,
            },
        )
    return choices

def watchlist_error_report(exc: Exception) -> dict[str, Any]:
    """Return and report a sanitized Watchlist failure without hiding its state."""
    error_type = type(exc).__name__
    if not error_type.isascii() or not error_type.isidentifier() or len(error_type) > 64:
        error_type = "Exception"
    print(
        "⚠ [BugX 回归看护] 巡查不可用 "
        f"(status=ERROR, error_type={error_type})；可检查账本后运行 "
        "fix_watchlist.py check --root <项目根> --json。"
    )
    return {
        "status": "ERROR",
        "possible_regressions": 0,
        "inconclusive": 0,
        "error_type": error_type,
    }


def safe_watchlist_incident_id(value: object) -> str:
    text = str(value or "")
    if (
        len(text) == 14 and text.startswith("I-")
        and all(char in "0123456789abcdef" for char in text[2:])
    ):
        return text
    return "invalid-id"


def check_fix_regressions(root: Path | None = None) -> dict:
    """巡查近期已修复代码片段，并显式报告回归、不可判定和巡查错误。"""
    try:
        proj_root = (root or resolve_project_root()).resolve()
        ledger_file = proj_root / ".bugx" / "knowledge" / "lesson-ledger.json"
        if not ledger_file.is_file():
            return {"status": "NO_LEDGER", "possible_regressions": 0, "inconclusive": 0}

        from core.repair import fix_watchlist
        report = fix_watchlist.check(ledger_file, proj_root, limit=20)
        if not isinstance(report, dict):
            raise TypeError("Watchlist report must be an object")
        report = dict(report)
        report["status"] = "COMPLETED"
        possible = report.get("possible_regressions", 0)
        inconclusive = report.get("inconclusive", 0)
        possible = possible if isinstance(possible, int) and not isinstance(possible, bool) else 0
        inconclusive = (
            inconclusive
            if isinstance(inconclusive, int) and not isinstance(inconclusive, bool)
            else 0
        )
        if possible > 0 or inconclusive > 0:
            print(
                "[BugX 回归看护] 巡查完成："
                f"incidents={report.get('incidents_watched', 0)}，"
                f"intact={report.get('intact', 0)}，"
                f"possible-regression={possible}，inconclusive={inconclusive}。"
            )
            for entry in report.get("entries", []):
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status not in {"possible-regression", "inconclusive", "skipped"}:
                    continue
                incident_id = _safe_watchlist_incident_id(entry.get("incident_id"))
                reason_code = entry.get("reason_code")
                if not isinstance(reason_code, str) or not reason_code.isascii() \
                        or not reason_code.replace("_", "").isalnum():
                    reason_code = "none"
                print(
                    f"  • Incident {incident_id}: {status}, "
                    f"reason_code={reason_code}"
                )
            if possible > 0:
                print("  → possible-regression 是片段级 advisory，需按 Incident 核验；不会自动阻断 post。")
            if inconclusive > 0:
                print("  → inconclusive 表示证据不足；先处理原因码，再重新巡查。")
        return report
    except Exception as exc:
        return _watchlist_error_report(exc)


def _sync_codebase_memory_env(profile: dict) -> None:
    """把 codebase-memory 探针结论同步到进程环境变量。"""
    cb_probe = (profile.get("probes") or {}).get("codebase-memory")
    if isinstance(cb_probe, dict):
        os.environ["BUGX_USE_CODEBASE_MEMORY"] = (
            "1" if cb_probe.get("status") == "READY" else "0"
        )


def report_degraded_probes(*, refresh: bool = False,
                            degraded: list[dict] | None = None) -> list[dict]:
    """收集并显式汇报探针 DEGRADED 项；返回降级动作清单供写入准备记录。

    ``degraded`` 允许由 :func:`run_preflight_probes` 预计算后传入，这样同一
    次 ``pre()`` 只需执行一轮探针（PERF-09/DES-13），不会重复扇出子进程。
    """
    if degraded is None:
        from core.probes import probe_registry
        profile = probe_registry.run_preflight_probes(
            resolve_project_root(), parallel=True, refresh=refresh,
        )
        degraded = list(profile.get("degraded_actions", []))

        # 同步环境变量
        _sync_codebase_memory_env(profile)

        # 启动前主动触发修复代码看护巡查
        check_fix_regressions(resolve_project_root())

    if not degraded:
        return []
    print()
    print("⚠ 环境加速器降级告警（启动前必须取得用户处置决定）")
    for item in degraded:
        print(f"  ✗ {item['name']}: {item['detail']}")
        for option in item.get("options", []):
            print(f"    · {option}")
    print("  → 发现 MCP 未安装/未配置时，下一步立即弹出交互选择；无终端时停止启动，"
          "不会静默进入后续审计。")
    print()
    return degraded

def record_degraded_choices(
    degraded: list[dict], choices: Mapping[str, object],
) -> None:
    """把降级项与用户选择追加写入准备记录（存在才写，不代用户创建）。

    **DOC-004 Engine-State Separation**：运行时记录**绝不**写进引擎目录。
    实测污染：旧实现回落到 ``AGENT_DIR / "prep-record.md"``，导致运行/测试把
    "环境加速器降级处置" 段追加进引擎自带的协议资产（``core/prep-record.md``）。
    现在只写项目态记录；解析失败或目标落在引擎目录内 ⇒ 直接跳过（不创建、不改写）。
    """
    if not degraded:
        return
    try:
        record = bugx_paths.require(resolve_project_root()).prep_record
    except (OSError, ValueError, RuntimeError):
        # 项目布局缺失/不安全（LayoutError 继承 RuntimeError）⇒ 不写、不创建。
        return
    try:
        record.resolve().relative_to(AGENT_DIR.resolve())
    except ValueError:
        pass
    else:
        print("[pre] 跳过降级记录写入：目标位于引擎目录（Engine-State Separation）")
        return
    if not record.is_file():
        return
    heading = "## 环境加速器降级处置（pre 自动记录）"
    lines = [heading]
    for item in degraded:
        choice = choices.get(item["name"], "未选择")
        if isinstance(choice, _DegradedDecision):
            receipt = json.dumps(
                choice.receipt, ensure_ascii=False, sort_keys=True, default=str,
            )
            rendered = f"{choice} [status={choice.status}; receipt={receipt}]"
        else:
            rendered = str(choice)
        lines.append(f"- {item['name']}: {item['detail']} → 用户选择：{rendered}")
    try:
        existing = record.read_text(encoding="utf-8")
        block = "\n".join(lines)
        if heading in existing:
            start = existing.index(heading)
            end = existing.find("\n## ", start + len(heading))
            if end < 0:
                end = len(existing)
            existing = existing[:start].rstrip() + "\n\n" + block + existing[end:]
        else:
            existing = existing.rstrip() + "\n\n" + block + "\n"
        record.write_text(existing, encoding="utf-8")
    except OSError:
        pass

def degraded_install(item: dict) -> _DegradedDecision:
    """Attempt one bounded provisioning action and fail closed on uncertainty.

    A missing unified installer, a failed installer, or an unverified result is
    a blocked decision.  The returned receipt carries the actual attempt and
    any self-heal report; it is never converted into a fallback success.
    """
    global _LAST_SELF_HEAL_REPORT
    previous_report = _LAST_SELF_HEAL_REPORT
    name = str(item.get("name", "未命名工具"))
    receipt: dict[str, Any] = {
        "action": "auto_provision_accelerators",
        "name": name,
        "attempted": True,
        "provisioned": [],
        "remaining": [name],
    }
    try:
        remaining, provisioned = _auto_provision_accelerators(resolve_project_root(), [item])
        receipt["provisioned"] = list(provisioned)
        receipt["remaining"] = [
            str(entry.get("name", "unknown"))
            for entry in remaining
            if isinstance(entry, Mapping)
        ]
        current_report = _LAST_SELF_HEAL_REPORT
        if provisioned and not remaining:
            return _degraded_decision(
                _DEGRADED_HEALED,
                "✓ 自动就地自愈安装就绪",
                receipt,
            )
        if current_report is not None and current_report is not previous_report:
            receipt["self_heal"] = _degraded_receipt_for_report(current_report)
    except Exception as exc:
        receipt["exception"] = f"{type(exc).__name__}: {exc}"
        print(f"  [launch_bugx] 自动装配尝试异常: {exc}")

    print(
        f"  [launch_bugx] ✗ 工具 {name} 未完成自动装配；本轮启动阻断，"
        "请完成配置后重试。"
    )
    return _degraded_decision(
        _DEGRADED_BLOCKED,
        "阻断：自动装配未完成（真实尝试收据已记录）",
        receipt,
    )

def prompt_degraded_choices(
    degraded: list[dict],
    explicit_choices: dict[str, str] | None = None,
) -> dict[str, _DegradedDecision]:
    """Ask for a bounded action; skip/defer always remains blocked."""
    choices: dict[str, _DegradedDecision] = {}
    if not degraded:
        return choices

    combined_explicit: dict[str, str] = dict(explicit_choices or {})
    # [BEGIN DEV_ONLY: FAST_VERIFICATION_TOOLING]
    from core.runtime.interaction import try_get_preset_runtime_config
    if try_get_preset_runtime_config is not None:
        preset_cfg = try_get_preset_runtime_config(resolve_project_root() if uses_project_layout() else None)
        if preset_cfg.get("auto_install_tools") or preset_cfg.get("missing_tools_action") == "install":
            for item in degraded:
                name = str(item.get("name", "unknown"))
                if name not in combined_explicit:
                    combined_explicit[name] = "install"
        elif preset_cfg.get("missing_tools_action") in {"skip", "fallback"}:
            for item in degraded:
                name = str(item.get("name", "unknown"))
                if name not in combined_explicit:
                    combined_explicit[name] = "defer"
    # [END DEV_ONLY: FAST_VERIFICATION_TOOLING]
    env_choices = os.environ.get("BUGX_TOOLING_CHOICES", "")
    if env_choices:
        for part in env_choices.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() not in combined_explicit:
                    combined_explicit[k.strip()] = v.strip()

    # A non-interactive host cannot silently authorize a missing capability.
    # It receives a durable blocked decision instead of an implicit fallback.
    is_interactive = False
    try:
        is_interactive = bool(sys.stdin.isatty())
    except (OSError, AttributeError):
        pass

    exemptions = _tooling_exemptions()

    pending: list[dict] = []
    for item in degraded:
        name = str(item.get("name", "unknown"))
        detail = str(item.get("detail", ""))
        is_mcp = item.get("kind") == "mcp" or "mcp" in name.casefold()

        # DOC-236 C1：项目 ``config.json`` 的显式豁免是唯一放行通道，且必须与
        # 自治路径同源生效。非 critical 能力被显式豁免 ⇒ 留痕放行，
        # 不再落进「非交互环境未取得决策」的阻断分支。
        if _degraded_exemption_hit(name, exemptions) and not _degraded_item_critical(item):
            choices[name] = _degraded_decision(
                _DEGRADED_EXEMPTED,
                "显式豁免（config.json tooling_exemptions）",
                {
                    "source": "project_config",
                    "name": name,
                    "status": _DEGRADED_EXEMPTED,
                    "probe": item.get("probe", ""),
                },
            )
            continue

        if _degraded_item_not_applicable(item):
            choices[name] = _degraded_decision(
                _DEGRADED_NOT_APPLICABLE,
                f"不适用：{detail or '探针已按项目形态剪枝'}",
                {
                    "source": "probe_applicability",
                    "name": name,
                    "status": _DEGRADED_NOT_APPLICABLE,
                },
            )
            continue

        # 建议性探针 / 瞬态超时：注册表已机器判定这两种情形不构成启动阻断。
        # 它们仍然留痕（决策 + 收据 + 控制台告警），不是静默降级。
        if _degraded_item_advisory(item):
            choices[name] = _degraded_decision(
                _DEGRADED_NOT_APPLICABLE,
                f"不阻断：建议性探针未就绪（{detail or '无详情'}）",
                {
                    "source": "probe_advisory",
                    "name": name,
                    "status": _DEGRADED_NOT_APPLICABLE,
                    "probe": item.get("probe", ""),
                },
            )
            print(
                f"[launch_bugx] ⚠ 建议性探针 {name} 未就绪，按注册表策略不阻断启动（已记录）"
            )
            continue
        if _degraded_item_transient(item) and not _degraded_item_critical(item):
            choices[name] = _degraded_decision(
                _DEGRADED_TRANSIENT,
                f"瞬态降级：本次探测超时（{detail or '无详情'}）",
                {
                    "source": "probe_transient",
                    "name": name,
                    "status": _DEGRADED_TRANSIENT,
                    "probe": item.get("probe", ""),
                },
            )
            print(
                f"[launch_bugx] ⚠ {name} 本次探测超时（非未安装），"
                "按瞬态降级继续，本轮走内置回退（已记录）"
            )
            continue

        # 非交互环境且未显式指定选择时，明确阻断而不是自动采纳回退
        if not is_interactive and name not in combined_explicit and not any(k.casefold() == name.casefold() for k in combined_explicit):
            choices[name] = _degraded_decision(
                _DEGRADED_BLOCKED,
                "阻断：非交互环境未取得安装/配置决策",
                {
                    "source": "noninteractive",
                    "name": name,
                    "attempted": False,
                    "reason": "decision_required",
                },
            )
            print(f"[launch_bugx] ✗ 非交互环境未取得加速器 {name} 的安装/配置决策，启动阻断")
            continue

        explicit_action = None
        for k, v in combined_explicit.items():
            if k.casefold() == name.casefold():
                explicit_action = v.strip().lower()
                break
        if explicit_action is not None:
            if explicit_action in {"skip", "fallback", "2"}:
                choices[name] = _degraded_decision(
                    _DEGRADED_BLOCKED,
                    "阻断：跳过/回退不会放行本轮启动",
                    {
                        "source": "explicit_choice",
                        "name": name,
                        "attempted": False,
                        "reason": "fallback_not_authorized",
                    },
                )
            elif explicit_action in {"defer", "later", "next", "3"}:
                choices[name] = _degraded_decision(
                    _DEGRADED_DEFERRED,
                    "下次再说（停止本轮启动）",
                    {
                        "source": "explicit_choice",
                        "name": name,
                        "attempted": False,
                        "reason": "deferred_by_user",
                    },
                )
            elif explicit_action in {"install", "config", "1"}:
                choices[name] = _degraded_install(item)
            else:
                choices[name] = _degraded_decision(
                    _DEGRADED_BLOCKED,
                    f"阻断：未知降级处置 {explicit_action}",
                    {
                        "source": "explicit_choice",
                        "name": name,
                        "attempted": False,
                        "reason": "unknown_choice",
                    },
                )
            continue

        pending.append(item)

    if not pending:
        return choices

    if len(pending) > 1:
        # 同名多实例合并：同一探针名可能产出多条降级项（如 web_conformance
        # 的 5 个实例），若逐条出选项会出现完全同名同描述、用户无法区分的
        # 重复项。按名称去重成组，单条选项代表该名称的全部实例，并标注计数。
        groups: dict[str, list[dict]] = {}
        for item in pending:
            groups.setdefault(str(item.get("name", "unknown")), []).append(item)
        options = []
        for group_name, instances in groups.items():
            label = (
                group_name if len(instances) == 1
                else f"{group_name}（{len(instances)} 个探针实例）"
            )
            is_auto = any(
                any(k in str(item.get("name", "")).casefold() for k in ("codebase-memory", "supervisor", "sast", "bandit", "semgrep", "ast-grep"))
                for item in instances
            )
            tag = "[支持自动装配]" if is_auto else "[无自动安装器，选择将启用内置回退]"
            details: list[str] = []
            for item in instances:
                detail = str(item.get("detail") or "").strip()
                if detail and detail not in details:
                    details.append(detail)
            desc = "；".join(details) if details else "本轮选择安装/配置该工具"
            options.append((
                label,
                f"{tag} {desc}",
            ))
        result = ask_interaction(
            "tooling:batch",
            "请勾选本轮要安装/配置的工具（留空表示全部阻断本轮启动；"
            "同名多个探针实例已合并为一条，勾选即对该名称的全部实例生效）。"
            "也可不经交互：设置环境变量 BUGX_TOOLING_CHOICES=name=install|defer",
            options,
            phase="tooling", detail="未勾选的工具本轮阻断启动",
            default=0, multiple=True, emit_request=True,
        )
        values = answered_values(result)
        if values is None:
            return choices
        selected = set(values)
        for index, (group_name, instances) in enumerate(groups.items(), 1):
            chosen = (
                _degraded_install(instances[0]) if str(index) in selected
                else _degraded_decision(
                    _DEGRADED_BLOCKED,
                    "阻断：批量未勾选，不会启用回退方案",
                    {
                        "source": "batch_selection",
                        "name": group_name,
                        "attempted": False,
                        "reason": "not_selected",
                    },
                )
            )
            choices[group_name] = chosen
        return choices

    for item in pending:
        name = str(item.get("name", "unknown"))
        detail = str(item.get("detail", ""))
        is_mcp = item.get("kind") == "mcp" or "mcp" in name.casefold()

        options = [
            ("立即安装/配置", "完成该工具配置后重新探测"),
            ("停止本轮启动", "未完成配置不得以回退方案放行"),
        ]
        if is_mcp:
            options.append(("下次再说", "停止本轮启动，稍后重新处理该 MCP"))
        result = ask_interaction(
            f"tooling:{name}", f"工具 {name} 未就绪，选择本轮处置方式", options,
            phase="tooling", detail=detail, default=0, emit_request=True,
        )
        value = answered_value(result)
        if value is None:
            return choices
        if value == "2":
            choices[name] = _degraded_decision(
                _DEGRADED_DEFERRED,
                "下次再说（停止本轮启动）",
                {
                    "source": "interactive_choice",
                    "name": name,
                    "attempted": False,
                    "reason": "deferred_by_user",
                },
            )
        elif value == "3" and is_mcp:
            choices[name] = _degraded_decision(
                _DEGRADED_DEFERRED,
                "下次再说（停止本轮启动）",
                {
                    "source": "interactive_choice",
                    "name": name,
                    "attempted": False,
                    "reason": "deferred_by_user",
                },
            )
        elif value == "1":
            choices[name] = _degraded_install(item)
        else:
            return choices
    return choices

def tooling_exemptions() -> set[str]:
    """DOC-236 C1：显式豁免通道（``config.json`` 的 ``tooling_exemptions``）。

    取消"可选件自动回退放行"后，无安装器的专有工具会阻断启动；唯一放行方式是
    **项目配置里显式列出名称**（可审计、可追溯），而不是命令行开关或静默降级。
    读取失败/类型不符一律返回空集（fail-closed）。

    注意：这里**只**要求 ``<root>/.bugx/config.json`` 这个普通文件本身可读，
    不再要求完整的 lifecycle 布局（``bugx_paths.require``）。否则在尚未
    ``init`` 完整布局的工作区里，阻断提示让用户去写 ``tooling_exemptions``，
    而读取端却拒绝读同一份文件——形成无法自救的死锁。
    """
    names: set[str] = set()
    try:
        cfg = _resolve_project_root() / ".bugx" / "config.json"
        if not cfg.is_file() or cfg.is_symlink() or cfg.stat().st_size > 64 * 1024:
            return names
        payload = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    raw = payload.get("tooling_exemptions") if isinstance(payload, dict) else None
    if isinstance(raw, list):
        names = {
            str(item).strip().casefold()
            for item in raw
            if isinstance(item, str) and str(item).strip()
        }
    return names


def degraded_gate(
    degraded: list[dict],
    explicit_choices: Mapping[str, object] | None = None,
) -> bool:
    """Require a machine-valid disposition for every degraded item.

    Human-readable choice text is intentionally never parsed here.  Values
    returned by older callers or test doubles are treated as blocked unless a
    non-critical item is explicitly exempted in project configuration.
    """
    if not degraded:
        return True
    try:
        structured = bool(explicit_choices) and all(
            isinstance(value, _DegradedDecision)
            for value in explicit_choices.values()
        )
        if structured:
            choices: Mapping[str, object] = dict(explicit_choices)
        else:
            raw_choices = (
                {str(key): str(value) for key, value in explicit_choices.items()}
                if explicit_choices is not None else None
            )
            choices = _prompt_degraded_choices(degraded, explicit_choices=raw_choices)
    except Exception as exc:  # noqa: BLE001
        print(f"启动前降级项询问失败，不能自动继续: {exc}")
        return False
    names = {str(item.get("name", "unknown")) for item in degraded}
    missing = sorted(names - set(choices))
    if missing:
        print("启动前仍有降级项未取得用户决策，不能自动继续: " + ", ".join(missing))
        _record_degraded_choices(degraded, choices)
        return False
    exemptions = _tooling_exemptions()
    normalized: dict[str, object] = dict(choices)
    real_blockers: list[str] = []
    deferred: list[str] = []
    for item in degraded:
        name = str(item.get("name", ""))
        choice = choices.get(name)
        is_typed_decision = (
            isinstance(choice, _DegradedDecision)
            and choice.status in {
                _DEGRADED_HEALED,
                _DEGRADED_NOT_APPLICABLE,
                _DEGRADED_EXEMPTED,
                _DEGRADED_BLOCKED,
                _DEGRADED_DEFERRED,
                _DEGRADED_TRANSIENT,
            }
            and isinstance(choice.receipt, dict)
        )
        status = choice.status if is_typed_decision else None
        is_critical = _degraded_item_critical(item)
        exempted = _degraded_exemption_hit(name, exemptions)
        if "codebase-memory" in name.casefold() and status != _DEGRADED_HEALED:
            # A degraded choice is never allowed to enable the bridge.  The
            # successful probe path above is the only automatic enable path.
            os.environ["BUGX_USE_CODEBASE_MEMORY"] = "0"
        if status in _DEGRADED_PASSING_STATUSES:
            continue
        if exempted and not is_critical:
            # Explicit project policy is the only non-success release valve.
            print(
                f"  ⚠ [launch_bugx] {name} 未完成，但已在 config.json "
                "tooling_exemptions 中显式豁免（已记录）"
            )
            receipt = choice.receipt if is_typed_decision else {}
            normalized[name] = _degraded_decision(
                _DEGRADED_EXEMPTED,
                "显式豁免（config.json tooling_exemptions）",
                {
                    "source": "project_config",
                    "name": name,
                    "status": _DEGRADED_EXEMPTED,
                    "original_status": status or "unstructured",
                    "original_receipt": receipt,
                },
            )
            continue
        if is_critical and exempted:
            normalized[name] = _degraded_decision(
                _DEGRADED_BLOCKED,
                "阻断：critical MCP/工具不得豁免",
                {
                    "source": "project_config",
                    "name": name,
                    "reason": "critical_capability_cannot_be_exempted",
                },
            )
            real_blockers.append(name)
            continue
        if status == _DEGRADED_DEFERRED:
            deferred.append(name)
            continue
        real_blockers.append(name)

    # Persist the normalized final decision before returning either success or
    # failure, so explicit exemptions and real failed attempts cannot vanish
    # on a sibling blocker.
    _record_degraded_choices(degraded, normalized)
    if real_blockers:
        print("MCP/环境工具安装或配置尚未完成，本轮启动已停止：" + ", ".join(real_blockers))
        print(
            "  DOC-236 C1：本轮不允许降级放行——请先自愈安装/配置后重跑；"
            "确无自动安装器的专有工具，可在项目 .bugx/config.json 的 "
            "tooling_exemptions 中显式列出（会留痕，不影响 critical 结论）。"
        )
        _emit_self_heal_evidence()
        return False
    if deferred:
        print("MCP/环境工具处置选择为“下次再说”，本轮启动已停止："
              + ", ".join(deferred))
        print("  处理完成后重新运行启动流程，不会自动绕过该 MCP 决策。")
        return False
    return True



_degraded_actions = degraded_actions
_DegradedDecision = DegradedDecision
_degraded_decision = degraded_decision
_degraded_item_not_applicable = degraded_item_not_applicable
_degraded_item_advisory = degraded_item_advisory
_degraded_item_transient = degraded_item_transient
_degraded_item_critical = degraded_item_critical
_degraded_exemption_hit = degraded_exemption_hit
_degraded_decision_for_policy = degraded_decision_for_policy
_degraded_receipt_for_report = degraded_receipt_for_report
_report_self_heal = report_self_heal
_emit_self_heal_evidence = emit_self_heal_evidence
_preflight_probe_id = preflight_probe_id
_watchlist_error_report = watchlist_error_report
_safe_watchlist_incident_id = safe_watchlist_incident_id
def _record_degraded_choices(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "_record_degraded_choices"):
        fn = getattr(sb, "_record_degraded_choices")
        if fn is not _record_degraded_choices:
            return fn(*args, **kwargs)
    return record_degraded_choices(*args, **kwargs)
def _auto_provision_accelerators(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "auto_provision_accelerators"):
        fn = getattr(sb, "auto_provision_accelerators")
        if fn is not _auto_provision_accelerators and fn is not auto_provision_accelerators:
            return fn(*args, **kwargs)
    return auto_provision_accelerators(*args, **kwargs)

def _degraded_install(item):
    sb = _sb()
    if sb and hasattr(sb, "_degraded_install"):
        fn = getattr(sb, "_degraded_install")
        if fn is not _degraded_install:
            return fn(item)
    return degraded_install(item)
def _prompt_degraded_choices(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "_prompt_degraded_choices"):
        fn = getattr(sb, "_prompt_degraded_choices")
        if fn is not _prompt_degraded_choices:
            return fn(*args, **kwargs)
    return prompt_degraded_choices(*args, **kwargs)
def _tooling_exemptions(*args, **kwargs):
    sb = _sb()
    if sb and hasattr(sb, "_tooling_exemptions"):
        fn = getattr(sb, "_tooling_exemptions")
        if fn is not _tooling_exemptions:
            return fn(*args, **kwargs)
    return tooling_exemptions(*args, **kwargs)
