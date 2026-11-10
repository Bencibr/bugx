#!/usr/bin/env python3
"""core/mcp_handlers/base.py - MCP 工具协议基类、参数校验与公共辅助 (DOC-070)。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from core.supervisor.supervisor_models import UNIT_LEVEL_DEBT, worker_attempts_exhausted

# ── Protocol size/depth/quantity caps (fail-closed, structured error) ──
MAX_MCP_REQUEST_BYTES = 512 * 1024  # 512 KiB per line
MAX_MCP_JSON_DEPTH = 32
MAX_MCP_JSON_KEYS = 5000
MAX_FINDINGS_PER_CHECKPOINT = 50
MAX_COVERAGE_PER_CHECKPOINT = 5  # DOC-044 / DOC-045 支持亲和任务包，单个 Worker 每次最多 5 个覆盖单元
MAX_EVIDENCE_PER_CHECKPOINT = 5
MAX_UNITS_PER_CLAIM = 5
MAX_COMMAND_LENGTH = 8192
MAX_ARTIFACT_PATH_LENGTH = 1024
MAX_FINDING_FINGERPRINT_LENGTH = 256
MAX_FINDING_FIELD_LENGTH = 4096
MAX_INLINE_COVERAGE_UNITS = 32


class McpProtocolError(RuntimeError):
    """Invalid MCP request or unsupported operation."""

    def __init__(self, message: str, code: int = -32602) -> None:
        super().__init__(message)
        self.code = code


def i18n_exc(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.exception_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def i18n_prompt(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def check_json_depth_and_keys(value: object, depth: int = 0) -> int:
    """Return total key count and enforce depth/keys caps (fail-closed)."""
    if depth > MAX_MCP_JSON_DEPTH:
        raise McpProtocolError(
            f"JSON 嵌套过深（>{MAX_MCP_JSON_DEPTH}）", code=-32602,
        )
    if isinstance(value, dict):
        total = len(value)
        if total > MAX_MCP_JSON_KEYS:
            raise McpProtocolError(
                f"JSON 对象键数过多（>{MAX_MCP_JSON_KEYS}）", code=-32602,
            )
        for v in value.values():
            total += check_json_depth_and_keys(v, depth + 1)
            if total > MAX_MCP_JSON_KEYS:
                raise McpProtocolError(
                    f"JSON 总键数过多（>{MAX_MCP_JSON_KEYS}）", code=-32602,
                )
        return total
    if isinstance(value, list):
        total = 0
        for item in value:
            total += check_json_depth_and_keys(item, depth + 1)
            if total > MAX_MCP_JSON_KEYS:
                raise McpProtocolError(
                    f"JSON 总键数过多（>{MAX_MCP_JSON_KEYS}）", code=-32602,
                )
        return total
    return 0


def number_argument(
    arguments: Mapping[str, object],
    name: str,
    default: int | float,
    *,
    integer: bool = False,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> int | float:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McpProtocolError(f"{name} 必须是数字")
    if not math.isfinite(float(value)):
        raise McpProtocolError(f"{name} 必须是有限数字")
    if integer and (not isinstance(value, int) or isinstance(value, bool)):
        if not float(value).is_integer():
            raise McpProtocolError(f"{name} 必须是整数")
        value = int(value)
    elif integer:
        value = int(value)
    else:
        value = float(value)
    if minimum is not None and value < minimum:
        raise McpProtocolError(f"{name} 必须 >= {minimum}")
    if maximum is not None and value > maximum:
        raise McpProtocolError(f"{name} 必须 <= {maximum}")
    return value


def bool_argument(
    arguments: Mapping[str, object],
    name: str,
    default: bool = False,
) -> bool:
    value = arguments.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    raise McpProtocolError(f"{name} 必须是布尔值")


def string_array_argument(
    arguments: Mapping[str, object], name: str,
) -> list[str] | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise McpProtocolError(f"{name} 必须是非空字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise McpProtocolError(f"{name} 必须是非空字符串数组")
    return value


def optional_string_array_argument(
    arguments: Mapping[str, object], name: str,
) -> list[str] | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, list):
        raise McpProtocolError(f"{name} 必须是字符串数组")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise McpProtocolError(f"{name} 必须是非空字符串数组")
    return value


def optional_string(arguments: Mapping[str, object], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise McpProtocolError(f"{name} 必须是非空字符串")
    return value.strip()


def validate_checkpoint_sizes(
    findings: object,
    coverage: object,
    evidence: object,
    signatures: object,
    notes: object,
) -> None:
    """Enforce bounded checkpoint payloads before touching Supervisor state."""
    if findings is not None:
        if not isinstance(findings, list):
            raise McpProtocolError("findings 必须是数组", code=-32602)
        if len(findings) > MAX_FINDINGS_PER_CHECKPOINT:
            raise McpProtocolError(
                f"findings 条数过多（>{MAX_FINDINGS_PER_CHECKPOINT}）", code=-32602,
            )
        check_json_depth_and_keys(findings)
        for item in findings:
            if not isinstance(item, dict):
                raise McpProtocolError("findings 成员必须是对象", code=-32602)
            fp = item.get("fingerprint")
            if not isinstance(fp, str) or not fp.strip():
                raise McpProtocolError("findings.fingerprint 必须是非空字符串", code=-32602)
            if len(fp) > MAX_FINDING_FINGERPRINT_LENGTH:
                raise McpProtocolError(
                    f"fingerprint 过长（>{MAX_FINDING_FINGERPRINT_LENGTH}）", code=-32602,
                )
            for key, val in item.items():
                if isinstance(val, str) and len(val) > MAX_FINDING_FIELD_LENGTH:
                    # DOC-W1：不静默截断（截断会丢 finding 内容且 Worker 无感知——
                    # 实测 models 模块 4 条 finding 只入册 2 条）。显式报错并给出
                    # 两条自愈路径：压缩字段或分批提交（findings 条数上限内拆包）。
                    raise McpProtocolError(
                        f"findings[{key}] 过长（{len(val)}>{MAX_FINDING_FIELD_LENGTH}）："
                        "禁止提交超长字段——请压缩该 finding 的字段文本，或将 findings "
                        "拆成多次 checkpoint（每次 ≤"
                        f"{MAX_FINDINGS_PER_CHECKPOINT} 条、字段各 ≤{MAX_FINDING_FIELD_LENGTH} 字符）后重试；"
                        "已入册的 finding 不可重复提交，拆包时按 fingerprint 去重",
                        code=-32602,
                    )
    if coverage is not None:
        if not isinstance(coverage, dict):
            raise McpProtocolError("coverage 必须是对象", code=-32602)
        check_json_depth_and_keys(coverage)
        if len(coverage) > MAX_COVERAGE_PER_CHECKPOINT:
            raise McpProtocolError(
                f"coverage 单元过多（>{MAX_COVERAGE_PER_CHECKPOINT}）；每个 Worker 仅限最多 {MAX_COVERAGE_PER_CHECKPOINT} 个单元",
                code=-32602,
            )
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise McpProtocolError("evidence 必须是对象", code=-32602)
        check_json_depth_and_keys(evidence)
        if len(evidence) > MAX_EVIDENCE_PER_CHECKPOINT:
            raise McpProtocolError(
                f"evidence 单元过多（>{MAX_EVIDENCE_PER_CHECKPOINT}）", code=-32602,
            )
        for unit, item in evidence.items():
            if not isinstance(item, dict):
                raise McpProtocolError(f"evidence[{unit}] 必须是对象", code=-32602)
            cmd = item.get("command")
            if isinstance(cmd, str) and len(cmd) > MAX_COMMAND_LENGTH:
                raise McpProtocolError(
                    f"evidence[{unit}].command 过长（>{MAX_COMMAND_LENGTH}）", code=-32602,
                )
            ap = item.get("artifact_path")
            if isinstance(ap, str) and len(ap) > MAX_ARTIFACT_PATH_LENGTH:
                raise McpProtocolError(
                    f"evidence[{unit}].artifact_path 过长（>{MAX_ARTIFACT_PATH_LENGTH}）",
                    code=-32602,
                )
    if signatures is not None:
        if not isinstance(signatures, list):
            raise McpProtocolError("signatures 必须是数组", code=-32602)
        if len(signatures) > 10:
            raise McpProtocolError("signatures 最多10条", code=-32602)
        check_json_depth_and_keys(signatures)
        for sig in signatures:
            if isinstance(sig, str) and len(sig) > 512:
                raise McpProtocolError("signature 过长（>512）", code=-32602)
    if notes is not None:
        if not isinstance(notes, dict):
            raise McpProtocolError("notes 必须是对象", code=-32602)
        check_json_depth_and_keys(notes)
        if len(notes) > MAX_COVERAGE_PER_CHECKPOINT:
            raise McpProtocolError(
                f"notes 单元过多（>{MAX_COVERAGE_PER_CHECKPOINT}）", code=-32602,
            )
        for k, v in notes.items():
            if isinstance(v, str) and len(v) > 2000:
                raise McpProtocolError(f"notes[{k}] 过长（>2000）", code=-32602)


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def format_result(value: object, *, is_error: bool = False, summary: str | None = None) -> dict[str, object]:
    structured = value if isinstance(value, dict) else {"value": value}
    if summary is not None:
        text = str(summary)
    elif isinstance(value, dict):
        rendered = compact_json(value)
        if len(rendered) > 300 or len(value) > 3:
            # DOC-139 / T-1: 字典较大时仅在 text 承载紧凑摘要，避免 wire payload 重复编码
            brief = []
            for k in ("status", "revision", "run_id", "round_no", "changed", "satisfied", "worker_id", "lease_id"):
                if k in value:
                    brief.append(f"{k}={value[k]}")
            if brief:
                text = ", ".join(brief)
            elif is_error:
                error_msg = value.get("error") or value.get("message") or value.get("detail")
                text = f"ERROR: {error_msg}" if error_msg else f"ERROR ({len(value)} fields)"
            else:
                text = f"OK ({len(value)} fields)"
        else:
            text = rendered
    else:
        text = str(value)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    }


def serialize_value(value: object) -> object:
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if hasattr(value, "_asdict"):
        return serialize_value(value._asdict())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize_value(item) for item in value]
    return value


def project_state(
    value: object,
    *,
    coverage_delta: Mapping[str, str] | None = None,
    compact: bool = False,
) -> dict[str, object]:
    """Return a bounded MCP state view; the full coverage map stays on disk."""
    payload = serialize_value(value)
    if not isinstance(payload, Mapping):
        return {"value": payload}
    result = dict(payload)
    if compact:
        for field in ("history", "findings", "parallel_proof_history", "claims", "recent_signatures"):
            if field in result:
                result[f"{field}_count"] = len(result[field]) if isinstance(result[field], (list, dict)) else 0
                result.pop(field, None)
    # MCP consumers must be able to pin the response shape.  Keep one
    # canonical envelope version at the serialization boundary so aliases
    # introduced by an internal RunState refactor cannot silently drift into
    # host integrations.
    result["mcp_schema_version"] = 1
    # DOC-139 / T-2: 在单一入口统一实施历史截断，杜绝旁路 handler 全量输出
    for field, limit in (("history", 50), ("findings", 100), ("parallel_proof_history", 20)):
        field_val = result.get(field)
        if isinstance(field_val, list) and len(field_val) > limit:
            result[field] = field_val[-limit:]
            result[f"{field}_truncated"] = True

    coverage = result.pop("coverage", {})
    if not isinstance(coverage, Mapping):
        coverage = {}
    by_state: dict[str, int] = {}
    for status in coverage.values():
        key = str(status)
        by_state[key] = by_state.get(key, 0) + 1
    summary = {
        "total": len(coverage),
        "by_state": dict(sorted(by_state.items())),
        "complete": bool(coverage) and all(
            state in {"covered", "not_applicable", "removed", "superseded"}
            for state in coverage.values()
        ),
    }
    # Keep the old shape for tiny protocol fixtures; real projects receive a
    # bounded summary only, preventing a large map from entering every turn.
    result["coverage"] = (
        dict(sorted((str(key), str(value)) for key, value in coverage.items()))
        if len(coverage) <= MAX_INLINE_COVERAGE_UNITS else summary
    )
    if len(coverage) > MAX_INLINE_COVERAGE_UNITS:
        result["coverage_summary"] = summary
    result["coverage_delta"] = dict(sorted((coverage_delta or {}).items()))
    result["coverage_resource"] = "bugx://state/coverage"
    return result


# DOC-230 3.7：恢复动作的**单一真源**。
#
# 规则：
#   1. `_next_action` 的每个取值都必须在 `RECOVERY_CHAIN_BY_ACTION` 中出现
#      （空元组 = 明确「无需恢复」），由测试穷举守卫；
#   2. 每步的角色归属只在本表声明，禁止在 `_recovery_chain` 内硬编码
#      （BX-ENGINE-16：工具表 / handler / 恢复链三源漂移曾导致「虚假可用性」）；
#   3. 步骤顺序即拓扑序（BX-R4-04）：一次只干一件，前置互相打架的三岔路口不再出现。
#
# 每项：(action 单值, 宿主应调的工具/CLI, why 一句话原因, required_role)
RECOVERY_STEP_SPECS: dict[str, tuple[str, str, str, str]] = {
    "REBIND_TARGET": (
        "REBUILD_PLAN", "bugx_supervisor_rebind_target",
        "目标漂移：先重绑指纹，否则一切 claim 都被拒", "host",
    ),
    "RESET_BUDGET": (
        "RESET_BUDGET", "bugx_supervisor_reset_budget",
        "预算耗尽：先续期墙钟，否则 claim/heartbeat 全拒", "host",
    ),
    "RESET_BUDGET_FORCE": (
        "ESCALATE_RESET_ATTEMPTS", "bugx_supervisor_reset_budget",
        "待派发单元全部熔断：以 force 推进预算 epoch 解冻", "host",
    ),
    "RECLAIM_EXPIRED_WORKERS": (
        "RECLAIM_EXPIRED_WORKERS", "bugx_supervisor_claim(recover_expired=true)",
        "过期租约：认领接手 expired worker 的单元与 artifact", "worker",
    ),
    "RECOVER": (
        "STALLED_OR_RECOVER", "bugx_supervisor_recover",
        "停滞：推进 scheduler_epoch 解冻熔断单元", "host",
    ),
    "REPORT": (
        "REPORT_OR_RECOVER", "bugx_supervisor_finish",
        "无可恢复单元：如实收尾并报告未覆盖债务", "host",
    ),
    "NEXT_ROUND": (
        "FINISH_OR_NEXT_ROUND", "bugx_supervisor_next_round",
        "覆盖与并发门禁满足：推进下一 Round", "host",
    ),
    "FINISH": (
        "FINISH_OR_NEXT_ROUND", "bugx_supervisor_finish",
        "覆盖与并发门禁满足：结束本次 run", "host",
    ),
}

# next_action -> 有序步骤 id（穷举，空元组表示无需恢复）。
RECOVERY_CHAIN_BY_ACTION: dict[str, tuple[str, ...]] = {
    "REBUILD_PLAN": ("REBIND_TARGET",),
    "RESET_BUDGET": ("RESET_BUDGET",),
    "ESCALATE_RESET_ATTEMPTS": ("RESET_BUDGET_FORCE",),
    "RECLAIM_EXPIRED_WORKERS": ("RECLAIM_EXPIRED_WORKERS",),
    "STALLED_OR_RECOVER": ("RECOVER",),
    "REPORT_OR_RECOVER": ("RECOVER", "REPORT"),
    "FINISH_OR_NEXT_ROUND": ("NEXT_ROUND", "FINISH"),
    "CLAIM_WORKERS": (),
    "WAIT_OR_HEARTBEAT_WORKERS": (),
}

# 规范拓扑序（多条件同时成立时的链顺序；首个命中项即 next_action）。
RECOVERY_ACTION_ORDER: tuple[str, ...] = (
    "REBUILD_PLAN", "RESET_BUDGET", "ESCALATE_RESET_ATTEMPTS",
    "RECLAIM_EXPIRED_WORKERS", "STALLED_OR_RECOVER", "REPORT_OR_RECOVER",
    "FINISH_OR_NEXT_ROUND", "CLAIM_WORKERS", "WAIT_OR_HEARTBEAT_WORKERS",
)

# 与 next_action 并行的条件触发（保留 BX-R4-04 的多步拓扑序）。
RECOVERY_EXTRA_TRIGGERS: tuple[tuple[str, str], ...] = (
    ("REBUILD_PLAN", "target_drift"),
    ("RECLAIM_EXPIRED_WORKERS", "expired_workers"),
)
RECOVERY_BUDGET_EXHAUSTED_ACTION = "RESET_BUDGET"

# 恢复动作的带外 CLI 入口（DOC-230 层 1）：CLI 直连引擎 supervisor/cli.py，
# 经 Supervisor 自身状态锁串行化，因此不引入提权面（审计 R2-Q1）。
RECOVERY_CLI_HINTS: dict[str, str | None] = {
    "REBIND_TARGET": "rebind-target --reason <原因>",
    "RESET_BUDGET": "reset-budget --renew-wall-clock",
    "RESET_BUDGET_FORCE": "reset-budget --force",
    "RECLAIM_EXPIRED_WORKERS": None,  # worker 通道可调，无需 CLI 兜底
    "RECOVER": "recover",
    "REPORT": "recover",
    "NEXT_ROUND": "recover",
    "FINISH": "recover",
}


def recovery_unblock_hint(
    step_id: str, required_role: str, host_channel: Mapping[str, object] | None,
) -> str:
    """DOC-230 3.4：被拒时必须给出**可执行**的解除方式（禁止死指引）。"""
    hints: list[str] = []
    cli_action = RECOVERY_CLI_HINTS.get(step_id)
    if cli_action:
        hints.append(f"bugx supervisor {cli_action} --root <项目>")
    if required_role == "host":
        holder_pid = host_channel.get("holder_pid") if isinstance(host_channel, Mapping) else None
        if isinstance(holder_pid, int) and not isinstance(holder_pid, bool):
            hints.append(
                f"释放被占用的 host 通道：kill {holder_pid}（或关闭占用它的 IDE 会话）"
            )
        else:
            hints.append(
                "若 host 通道被占用：关闭占用它的 IDE 会话，"
                "或 bugx mcp serve --role host --takeover"
            )
    if not hints:
        return "无需额外动作"
    return "；".join(hints)


def recovery_step(
    step_id: str,
    caller_role: str,
    host_channel: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """构造单个恢复步骤；角色归属只来自 `RECOVERY_STEP_SPECS`。"""
    action, tool, why, required_role = RECOVERY_STEP_SPECS[step_id]
    available = caller_role == required_role
    step: dict[str, object] = {
        "step_id": step_id,
        "action": action,
        "tool": tool,
        "why": why,
        "required_role": required_role,
        "available_in_channel": available,
    }
    if not available:
        step["channel_guidance"] = f"此步骤需通过 {required_role} MCP 通道调用"
        step["blocked_by"] = (
            dict(host_channel)
            if required_role == "host" and isinstance(host_channel, Mapping)
            else None
        )
        step["how_to_unblock"] = recovery_unblock_hint(
            step_id, required_role, host_channel,
        )
    return step


def triggered_recovery_actions(
    state: Mapping[str, object], gate: Mapping[str, object],
) -> list[str]:
    """按 `RECOVERY_ACTION_ORDER` 返回当前成立的动作（含 next_action 本身）。"""
    primary = next_action(state, gate)
    triggered = {primary}
    for action, flag in RECOVERY_EXTRA_TRIGGERS:
        if gate.get(flag):
            triggered.add(action)
    if str(state.get("status", "")) == "BUDGET_EXHAUSTED":
        triggered.add(RECOVERY_BUDGET_EXHAUSTED_ACTION)
    ordered = [action for action in RECOVERY_ACTION_ORDER if action in triggered]
    # 契约外的新取值：不静默丢弃，交由 _recovery_chain fail-closed 暴露。
    ordered.extend(sorted(triggered - set(ordered)))
    return ordered


def recovery_chain(
    state: Mapping[str, object],
    gate: Mapping[str, object],
    caller_role: str = "host",
    host_channel: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """返回当前状态下有序的恢复链（可为空）。

    `host_channel` 为 host 通道可用性快照（DOC-230 3.3），用于在步骤不可达时
    给出**可操作**的 `blocked_by` / `how_to_unblock`。
    """
    chain: list[dict[str, object]] = []
    seen: set[str] = set()
    for action in triggered_recovery_actions(state, gate):
        if action not in RECOVERY_CHAIN_BY_ACTION:
            # fail-closed：未知 next_action 必须显式暴露，不得静默返回空链。
            chain.append({
                "step_id": None,
                "action": action,
                "tool": None,
                "why": "未知 next_action：恢复链未定义（请升级 CLI/引擎）",
                "required_role": "host",
                "available_in_channel": caller_role == "host",
                "blocked_by": None,
                "how_to_unblock": "升级 CLI 与全局引擎到同一 Build ID 后重试",
            })
            continue
        for step_id in RECOVERY_CHAIN_BY_ACTION[action]:
            if step_id in seen:
                continue
            seen.add(step_id)
            chain.append(recovery_step(step_id, caller_role, host_channel))
    return chain


def status_payload_safe(
    service: object,
    supervisor: object,
    *,
    mutation_committed: bool = False,
) -> dict[str, object]:
    """变更后的响应构造：渲染失败不得回退为「未变更」的错误语义。

    FIX-003 B4（回归修复）：本函数**模块级**实现，与 ``_next_action`` /
    ``_recovery_chain`` / ``_project_state`` 同一模式，**不扩展** service 的私有
    接口——否则任何只实现 ``_status_payload`` 的测试替身/宿主都会 AttributeError
    （实测：``tests.test_audit_supervisor`` 的 ``_Service``）。

    - 正常路径：逐字委托 ``service._status_payload(supervisor)``，语义不变。
    - 失败路径：返回**显式降级信封**（``response_degraded`` + 原因 + 最新
      revision），既如实告知「变更已提交」，又避免静默降级。
    """
    try:
        return service._status_payload(supervisor)  # type: ignore[attr-defined]
    except Exception as exc:  # 渲染失败必须降级为「已提交」而非误报失败
        payload: dict[str, object] = {
            "status": "UNKNOWN",
            "revision": None,
            "root": str(getattr(service, "root", "")),
            "response_degraded": True,
            "degraded_reason": f"{type(exc).__name__}: {exc}",
        }
        if mutation_committed:
            payload["mutation_committed"] = True
        try:
            state = supervisor.status()  # type: ignore[attr-defined]
            status = getattr(state, "status", None)
            payload["status"] = getattr(status, "value", None) or str(status)
            payload["revision"] = getattr(state, "revision", None)
            payload["budget_epoch"] = getattr(state, "budget_epoch", None)
        except Exception:
            pass
        return payload


def all_pending_units_exhausted(
    state: Mapping[str, object], gate: Mapping[str, object],
) -> bool:
    """Recognize a reset-only stall without confusing ineligible units with exhaustion."""
    pending_units = gate.get("pending_units")
    exhausted_pending_units = gate.get("exhausted_pending_units")
    if (
        isinstance(pending_units, int)
        and isinstance(exhausted_pending_units, int)
    ):
        return pending_units > 0 and exhausted_pending_units == pending_units

    coverage = state.get("coverage")
    claims_raw = state.get("claims")
    if not isinstance(coverage, Mapping) or not isinstance(claims_raw, Mapping):
        return False
    if "by_state" in coverage or "total" in coverage:
        return False
    unfinished = [
        str(unit_id) for unit_id, status in coverage.items()
        if isinstance(status, str) and status in UNIT_LEVEL_DEBT
    ]
    if not unfinished:
        return False
    claims = {
        worker_id: (
            SimpleNamespace(**claim) if isinstance(claim, Mapping) else claim
        )
        for worker_id, claim in claims_raw.items()
    }
    snapshot = SimpleNamespace(
        coverage=coverage,
        claims=claims,
        scheduler_epoch=state.get("scheduler_epoch"),
    )
    try:
        return all(worker_attempts_exhausted(snapshot, unit) for unit in unfinished)
    except (TypeError, ValueError):
        return False


def next_action(state: Mapping[str, object], gate: Mapping[str, object]) -> str:
    status = str(state.get("status", ""))
    if gate.get("recovery_required") or gate.get("recovery_failed"):
        from core.supervisor.rejection_remedies import RECOVERY_REQUIRED_ACTION
        return RECOVERY_REQUIRED_ACTION
    if gate.get("target_drift"):
        return "REBUILD_PLAN"
    if status == "BUDGET_EXHAUSTED":
        return "RESET_BUDGET"
    if status in {"COMPLETE", "FAILED", "INCOMPLETE"}:
        return "REPORT_OR_RECOVER"
    if not gate.get("active_workers") and all_pending_units_exhausted(state, gate):
        return "ESCALATE_RESET_ATTEMPTS"
    expired = bool(gate.get("expired_workers"))
    if expired:
        takeover_unavailable = bool(
            gate.get("takeover_unavailable")
            or gate.get("expired_workers_unclaimable")
        )
        if takeover_unavailable and not gate.get("active_workers"):
            return "STALLED_OR_RECOVER"
        return "RECLAIM_EXPIRED_WORKERS"

    # 滑动窗口流水线决策：
    # 只要存在空闲并发槽位 (available_slots > 0) 且有待申领覆盖单元 (unclaimed_pending_units > 0)，
    # 即使当前有 active_workers 在运行，依然允许且推荐宿主补位申领 (CLAIM_WORKERS)，杜绝木桶效应。
    unclaimed = gate.get("unclaimed_pending_units")
    available_slots = gate.get("available_slots")
    pending = gate.get("pending_units", 0)

    # 若门禁快照未计算 unclaimed_pending_units，安全回退到 pending_units
    has_work_to_claim = (
        int(unclaimed) > 0 if isinstance(unclaimed, int) else (int(pending) > 0 if isinstance(pending, int) else False)
    )
    can_dispatch_more = (
        int(available_slots) > 0 if isinstance(available_slots, int) else True
    )

    if gate.get("active_workers"):
        if has_work_to_claim and can_dispatch_more:
            return "CLAIM_WORKERS"
        return "WAIT_OR_HEARTBEAT_WORKERS"

    if gate.get("satisfied") and not gate.get("pending_units"):
        return "FINISH_OR_NEXT_ROUND"
    if has_work_to_claim:
        return "CLAIM_WORKERS"
    # 覆盖债务存在但全部熔断/不合格：本轮无可派发单元（能力枯竭或停滞）。
    # 出路：宿主应先调用 bugx_supervisor_recover(caller_role="host") 推进 scheduler_epoch
    # 解冻熔断单元；切勿再运行 `bugx engine workers`（必然 exit=1 空转）。
    # 若 recover 后仍无法派发（MAX_RUN_WORKER_ATTEMPTS 全 Run 上限已触及），
    # 应输出 BLOCKED 并如实通过 finish 结束并报告未覆盖债务，不得空转。
    return "STALLED_OR_RECOVER"
