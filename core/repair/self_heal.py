#!/usr/bin/env python3
"""DOC-236 C2：穷尽自愈编排器（多方案 → 失败分析 → 搜索/知识库 → 替代方案 → 上限 → 上报）。

设计要点（与 DOC-236 §2 C2 一一对应）：

1. **受信方案登记**：只有 ``PLAN_REGISTRY`` 中显式登记的工具才能产生安装 argv；未知工具名
   不会被推断为包名。每条登记方案声明 ``requires``（缺可执行文件则跳过）与 ``verify``
   （目标可执行文件必须存在；Codebase Memory 还必须精确匹配固定 CLI 版本），避免"命令返回 0 但其实没装上或装错版本"。
2. **失败分析**：把退出码与输出归类为 ``permission`` / ``network`` / ``version-conflict`` /
   ``platform`` / ``unknown``，用于选择下一步方案与给出可读原因。
3. **搜索与知识库**：``search`` 与 ``knowledge`` 为可注入钩子。搜索能力未装配时**如实记录**
   "搜索不可用"（不伪造建议）；知识库默认接 `lesson_ledger.lookup_tools`。
4. **替代实现**：命中 `ALTERNATIVES` 时尝试替代工具，并在报告中**显式记录覆盖差异**。
5. **上限与预算**：尝试次数、单次超时、总耗时三个上限，全部可由环境变量配置；
   达到任一上限即停止（防自旋，符合 AGENTS.md 耗时防卫）。
6. **可观测收据**：每次尝试追加一行 JSONL 到 ``<root>/.bugx/state/self-heal.jsonl``。
7. **穷尽后上报**：返回结构化报告（已试方案 + 失败分类 + 提示），交由 C1 门禁阻断并展示。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.runtime.tool_install_targets import OPTIONAL_TOOL_PACKAGE_SPECS

DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_ATTEMPT_TIMEOUT = 120.0
DEFAULT_TOTAL_BUDGET = 300.0

FAILURE_CLASSES = (
    "permission", "network", "version-conflict", "platform", "unknown",
)


@dataclass(frozen=True)
class Plan:
    """一条自愈方案。"""

    label: str
    argv: tuple[str, ...]
    requires: str | None = None
    verify: tuple[str, ...] = ()
    coverage_note: str = ""
    verify_version_spec: str | None = None


@dataclass(frozen=True)
class Budget:
    max_attempts: int
    attempt_timeout: float
    total_budget: float


@dataclass
class Attempt:
    tool: str
    label: str
    argv: tuple[str, ...]
    returncode: int | None
    failure: str | None
    duration: float
    output_tail: str = ""


@dataclass
class HealReport:
    healed: list[str] = field(default_factory=list)
    remaining: list[dict] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "healed": list(self.healed),
            "remaining": [str(item.get("name", "unknown")) for item in self.remaining],
            "attempts": [
                {
                    "tool": item.tool, "plan": item.label, "argv": list(item.argv),
                    "returncode": item.returncode, "failure": item.failure,
                    "duration": round(item.duration, 3),
                }
                for item in self.attempts
            ],
            "hints": list(self.hints),
            "notes": list(self.notes),
            "stop_reason": self.stop_reason,
        }

def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_budget() -> Budget:
    """上限全部显式可配置（默认值见常量）。"""
    return Budget(
        max_attempts=_env_int("BUGX_SELF_HEAL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        attempt_timeout=_env_float("BUGX_SELF_HEAL_ATTEMPT_TIMEOUT", DEFAULT_ATTEMPT_TIMEOUT),
        total_budget=_env_float("BUGX_SELF_HEAL_TOTAL_BUDGET", DEFAULT_TOTAL_BUDGET),
    )


# 已知工具的方案阶梯（按成功率 × 侵入性排序）。
PLAN_REGISTRY: dict[str, tuple[Plan, ...]] = {
    "codebase-memory": (
        Plan("pipx 隔离重装固定版本", (
            "pipx", "install", "--force", OPTIONAL_TOOL_PACKAGE_SPECS["codebase-memory"],
        ),
             requires="pipx", verify=("codebase-memory-mcp",),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["codebase-memory"]),
        Plan("uv tool 隔离重装固定版本", (
            "uv", "tool", "install", "--reinstall", OPTIONAL_TOOL_PACKAGE_SPECS["codebase-memory"],
        ),
             requires="uv", verify=("codebase-memory-mcp",),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["codebase-memory"]),
    ),
    "ast-grep": (
        Plan("pipx 隔离重装固定版本", (
            "pipx", "install", "--force", OPTIONAL_TOOL_PACKAGE_SPECS["ast-grep"],
        ),
             requires="pipx", verify=("ast-grep", "sg"),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["ast-grep"]),
        Plan("uv tool 隔离重装固定版本", (
            "uv", "tool", "install", "--reinstall", OPTIONAL_TOOL_PACKAGE_SPECS["ast-grep"],
        ),
             requires="uv", verify=("ast-grep", "sg"),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["ast-grep"]),
    ),
    "semgrep": (
        Plan("pipx 隔离安装固定版本", (
            "pipx", "install", "--force", OPTIONAL_TOOL_PACKAGE_SPECS["semgrep"],
        ), requires="pipx", verify=("semgrep",),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["semgrep"]),
        Plan("uv tool 隔离安装固定版本", (
            "uv", "tool", "install", "--reinstall", OPTIONAL_TOOL_PACKAGE_SPECS["semgrep"],
        ), requires="uv", verify=("semgrep",),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["semgrep"]),
    ),
    "bandit": (
        Plan("pipx 隔离安装固定版本", (
            "pipx", "install", "--force", OPTIONAL_TOOL_PACKAGE_SPECS["bandit"],
        ), requires="pipx", verify=("bandit",),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["bandit"]),
        Plan("uv tool 隔离安装固定版本", (
            "uv", "tool", "install", "--reinstall", OPTIONAL_TOOL_PACKAGE_SPECS["bandit"],
        ), requires="uv", verify=("bandit",),
             verify_version_spec=OPTIONAL_TOOL_PACKAGE_SPECS["bandit"]),
    ),
}

# 替代实现：A 不可用时用 B，且必须记录覆盖差异。
ALTERNATIVES: dict[str, tuple[str, str]] = {
    "ast-grep": ("semgrep", "semgrep 亦提供结构匹配；ast-grep 的 YAML 规则集不通用，"
                            "覆盖度与规则迁移需重新标定"),
}


def plans_for(tool: str) -> tuple[Plan, ...]:
    """只返回代码内登记的受信安装方案；未知名称一律没有安装阶梯。"""
    key = str(tool).strip().casefold()
    return PLAN_REGISTRY.get(key, ())


def classify_failure(returncode: int | None, output: str) -> str:
    """把失败归类，用于选择下一步方案与给出可读原因。"""
    text = (output or "").casefold()
    if returncode is None:
        return "unknown"
    if "permission denied" in text or "errno 13" in text or "eacces" in text:
        return "permission"
    if any(marker in text for marker in (
        "could not resolve", "connection refused", "timed out", "timeout",
        "temporary failure in name resolution", "network is unreachable",
    )):
        return "network"
    if any(marker in text for marker in (
        "conflicting dependencies", "resolutionimpossible", "version conflict",
        "no matching distribution", "cannot install",
    )):
        return "version-conflict"
    if any(marker in text for marker in (
        "not supported", "unsupported platform", "no such file or directory",
        "is not compatible", "platform",
    )):
        return "platform"
    return "unknown"



# --------------------------------------------------------------------------- #
# 运行器与收据
# --------------------------------------------------------------------------- #

Runner = Callable[[tuple[str, ...], float], tuple[int | None, str]]
Which = Callable[[str], str | None]
Clock = Callable[[], float]


def _default_runner(argv: tuple[str, ...], timeout: float) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s"
    except OSError as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, f"{proc.stdout or ''}\n{proc.stderr or ''}"


def receipt_path(root: Path | None) -> Path | None:
    if root is None:
        return None
    return Path(root) / ".bugx" / "state" / "self-heal.jsonl"


def append_receipt(root: Path | None, payload: dict) -> None:
    """追加一行 JSONL 收据；写失败不得影响自愈主流程。"""
    target = receipt_path(root)
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 搜索与知识库钩子（DOC-236 C3 接入点）
# --------------------------------------------------------------------------- #

SearchHook = Callable[[str, str, str], list[str]]
KnowledgeHook = Callable[[str], list[str]]
HttpGet = Callable[[str, float], str]

# 内置 HTTP 搜索端点（DOC-236 C3：无 Key、无凭据；仅在显式开启时访问）。
DEFAULT_SEARCH_ENDPOINT = "https://duckduckgo.com/html/"

# 测试注入点：设置后替代 urllib（避免测试期真实外发）。
_HTTP_GET: HttpGet | None = None


def _http_get(query: str, timeout: float) -> str:
    if _HTTP_GET is not None:
        return _HTTP_GET(query, timeout)
    import urllib.parse
    import urllib.request

    request = urllib.request.Request(
        DEFAULT_SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({"q": query}),
        headers={"User-Agent": "bugx-self-heal/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(200_000).decode("utf-8", "replace")


def _parse_search_hits(html: str) -> list[str]:
    """从搜索结果页提取标题行（去标签、去重、限量）。"""
    import re

    hits: list[str] = []
    for raw in re.findall(r'result__a[^>]*>(.*?)</a>', html, flags=re.S | re.I):
        text = re.sub(r"<[^>]+>", "", raw).strip()
        if text and text not in hits:
            hits.append(text)
        if len(hits) >= 5:
            break
    return hits


def builtin_http_search(query: str, *, timeout: float = 10.0) -> list[str]:
    """内置 HTTP 搜索（C3 允许的路径）：显式开启才外发，单次限时，失败即空。"""
    mode = os.environ.get("BUGX_SELF_HEAL_SEARCH", "").strip().casefold()
    if mode not in ("builtin", "http"):
        return []
    try:
        return _parse_search_hits(_http_get(query, timeout))
    except Exception:
        return []


def default_search_hook(tool: str, failure: str, output: str) -> list[str]:
    """搜索同类问题与替代方案（DOC-236 C3 接入点，两级后端）。

    ① 外部搜索 MCP/CLI：``BUGX_SELF_HEAL_SEARCH_CMD``（准入完成后由 C3 装配）；
    ② 内置 HTTP 搜索：``BUGX_SELF_HEAL_SEARCH=builtin`` 显式开启（无 Key、无凭据）。
    两者都未装配时**如实返回空**——绝不伪造建议。
    """
    query = f"{tool} install failed {failure}"
    command = os.environ.get("BUGX_SELF_HEAL_SEARCH_CMD", "").strip()
    if command and shutil.which(command.split()[0]) is not None:
        argv = tuple(command.split()) + (query,)
        try:
            proc = subprocess.run(list(argv), capture_output=True, text=True,
                                  timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None:
            lines = [line.strip() for line in (proc.stdout or "").splitlines()
                     if line.strip()]
            if lines:
                return lines[:5]
    return builtin_http_search(query)


def default_knowledge_hook(tool: str) -> list[str]:
    """查经验账本（lesson_ledger）中该工具的既有处置经验。"""
    try:
        from core.ledger import lesson_ledger  # type: ignore
    except Exception:
        return []
    try:
        rows = lesson_ledger.lookup_tools(lesson_ledger.DEFAULT_LEDGER, query=tool, limit=3)
    except Exception:
        return []
    hints: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            text = row.get("remedy") or row.get("hint") or row.get("name") or ""
        else:
            text = str(row)
        if text:
            hints.append(str(text))
    return hints[:5]


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #

# 降级项显示名 → 方案阶梯键（方案阶梯按“工具”而非“症状”组织）。
TOOL_ALIASES: tuple[tuple[str, str], ...] = (
    ("codebase-memory", "codebase-memory"),
    ("ast-grep", "ast-grep"),
    ("ast_grep", "ast-grep"),
    ("semgrep", "semgrep"),
    ("bandit", "bandit"),
)


def tool_key(name: str) -> str:
    folded = str(name).strip().casefold()
    for marker, key in TOOL_ALIASES:
        if marker in folded:
            return key
    return folded


def _usable(plan: Plan, which: Which) -> bool:
    return plan.requires is None or which(plan.requires) is not None


def _verified(
    plan: Plan, which: Which,
    version_resolver: Callable[..., str | None] | None = None,
) -> bool:
    if plan.verify_version_spec is not None:
        match = re.fullmatch(
            r"(?P<package>[A-Za-z0-9_.-]+)==(?P<version>\d+(?:\.\d+){1,3})",
            plan.verify_version_spec,
        )
        if not match:
            return False
        if plan.verify_version_spec not in OPTIONAL_TOOL_PACKAGE_SPECS.values():
            return False
        package = match.group("package")
        expected = match.group("version")
        if package == "codebase-memory-mcp":
            if version_resolver is None:
                try:
                    from core.probes import codebase_memory_probe
                    version_resolver = codebase_memory_probe.resolve_compatible_binary
                except ImportError:
                    return False
            try:
                return bool(version_resolver(expected_version=expected))
            except Exception:
                return False
        try:
            if package in {"semgrep", "bandit"}:
                from core.probes import sast_probe
                # Installation may replace the executable at the same path.
                # Force a fresh process/version observation before trusting
                # the post-install result.
                sast_probe.clear_tool_probe_cache()
                info = sast_probe.check_tool(package)
                actual = sast_probe.version_tuple(str(info.get("version") or ""))
            elif package == "ast-grep-cli":
                from core.probes import efficiency_probe
                info = efficiency_probe._command(("ast-grep", "sg"), "ast-grep")
                actual = efficiency_probe.version_tuple(str(info.get("version") or ""))
            else:
                return False
            return info.get("ok") is True and actual == tuple(
                int(part) for part in expected.split(".")
            )
        except Exception:
            return False
    if not plan.verify:
        return True
    return any(which(name) is not None for name in plan.verify)


def heal_tool(
    name: str,
    *,
    detail: str = "",
    root: Path | None = None,
    runner: Runner | None = None,
    which: Which | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    search: SearchHook | None = None,
    knowledge: KnowledgeHook | None = None,
    report: HealReport | None = None,
    elapsed: Callable[[], float] | None = None,
    version_resolver: Callable[..., str | None] | None = None,
) -> tuple[bool, HealReport]:
    """对单个工具穷尽尝试方案阶梯；返回 (是否已就绪, 报告)。

    停止条件（任一满足即停止，绝不无限重试）：尝试次数上限、总耗时预算上限、
    方案阶梯穷尽。停止后由调用方上报（C1 门禁阻断 + 展示已试方案）。
    """
    runner = runner or _default_runner
    which = which or shutil.which
    clock = clock or time.monotonic
    budget = budget or resolve_budget()
    search = search or default_search_hook
    knowledge = knowledge or default_knowledge_hook
    report = report or HealReport()

    def spent() -> float:
        return elapsed() if elapsed is not None else 0.0

    plans = plans_for(name)
    if not plans:
        tool_name = str(name).strip()
        reason = "unregistered_tool" if tool_name else "invalid_tool_name"
        reported_name = tool_name or "<empty>"
        report.stop_reason = reason
        report.remaining.append({"name": reported_name, "detail": detail})
        report.notes.append(
            f"{reported_name}: 未登记受信安装方案，拒绝将工具名当作包名自动安装"
        )
        append_receipt(root, {
            "event": reason,
            "tool": reported_name[:128],
        })
        return False, report

    for plan in plans:
        if spent() >= budget.total_budget:
            report.stop_reason = "total_budget"
            return False, report
        if len(report.attempts) >= budget.max_attempts:
            report.stop_reason = "max_attempts"
            return False, report
        if not _usable(plan, which):
            report.notes.append(f"{name}: 跳过方案「{plan.label}」（缺少 {plan.requires}）")
            continue

        started = clock()
        returncode, output = runner(plan.argv, budget.attempt_timeout)
        duration = max(0.0, clock() - started)
        failure = None if returncode == 0 else classify_failure(returncode, output)
        report.attempts.append(Attempt(name, plan.label, plan.argv, returncode,
                                       failure, duration, output.strip()[-500:]))
        append_receipt(root, {"event": "attempt", "tool": name, "plan": plan.label,
                              "argv": list(plan.argv), "returncode": returncode,
                              "failure": failure, "duration": round(duration, 3)})

        if returncode == 0 and _verified(plan, which, version_resolver):
            report.healed.append(f"✓ {name} 已通过方案「{plan.label}」就绪")
            append_receipt(root, {"event": "healed", "tool": name, "plan": plan.label})
            return True, report

        if returncode == 0:
            expected = plan.verify_version_spec or "/".join(plan.verify)
            report.notes.append(
                f"{name}: 方案「{plan.label}」返回 0 但校验失败（未验证 "
                f"{expected}），视为失败")
            append_receipt(root, {"event": "verify_failed", "tool": name,
                                  "plan": plan.label})

        for hint in search(name, str(failure), output):
            entry = f"{name}｜搜索提示：{hint}"
            if entry not in report.hints:
                report.hints.append(entry)
        for hint in knowledge(name):
            entry = f"{name}｜经验账本：{hint}"
            if entry not in report.hints:
                report.hints.append(entry)

    # The plan list may end on the same attempt that consumes the configured
    # cap, so there may be no next loop iteration to record the stop reason.
    if len(report.attempts) >= budget.max_attempts:
        report.stop_reason = "max_attempts"

    substitute = ALTERNATIVES.get(str(name).casefold())
    if substitute is not None:
        alternative, coverage = substitute
        if which(alternative) is not None:
            report.notes.append(f"{name}: 已就绪的替代实现「{alternative}」（{coverage}）")
        else:
            report.notes.append(
                f"{name}: 方案穷尽，替代实现「{alternative}」亦不可用（{coverage}）")

    report.remaining.append({"name": name, "detail": detail})
    append_receipt(root, {"event": "exhausted", "tool": name,
                          "attempts": len(report.attempts),
                          "failure": report.attempts[-1].failure if report.attempts else None})
    return False, report


def heal_degraded(
    items: list[dict],
    *,
    root: Path | None = None,
    runner: Runner | None = None,
    which: Which | None = None,
    clock: Clock | None = None,
    budget: Budget | None = None,
    search: SearchHook | None = None,
    knowledge: KnowledgeHook | None = None,
) -> tuple[list[dict], list[str], HealReport]:
    """对一批降级项穷尽自愈；返回 (剩余降级项, 已就绪说明, 报告)。

    预算与尝试次数在**整批**内共享：达到上限即停止并如实记录未尝试项，
    绝不静默继续（防自旋）。
    """
    budget = budget or resolve_budget()
    clock = clock or time.monotonic
    started = clock()
    report = HealReport()
    remaining: list[dict] = []
    items = list(items)

    for index, item in enumerate(items):
        display = str(item.get("name", "")) or "unknown"
        healed, report = heal_tool(
            tool_key(display),
            detail=str(item.get("detail", "")),
            root=root, runner=runner, which=which, clock=clock, budget=budget,
            search=search, knowledge=knowledge, report=report,
            elapsed=lambda: clock() - started,
        )
        if healed:
            continue
        remaining.append(item)
        if report.stop_reason in {"unregistered_tool", "invalid_tool_name"}:
            # The rejection applies to this item only.  It must not masquerade
            # as a batch budget stop or prevent later registered tools healing.
            report.stop_reason = None
            continue
        if report.stop_reason:
            pending = len(items) - index - 1
            if pending > 0:
                report.notes.append(
                    f"达到自愈上限（{report.stop_reason}），剩余 {pending} 项未尝试")
                remaining.extend(items[index + 1:])
            break

    append_receipt(root, {"event": "summary", **report.as_dict()})
    return remaining, report.healed, report


def escalation_lines(report: HealReport) -> list[str]:
    """把穷尽报告渲染成给人看的处置清单（已试方案 + 失败原因 + 提示 + 手工处置）。"""
    lines: list[str] = []
    for attempt in report.attempts:
        reason = attempt.failure or "unknown"
        lines.append(
            f"  · 方案「{attempt.label}」失败（原因：{reason}，退出码 {attempt.returncode}）")
    for hint in report.hints:
        lines.append(f"  · {hint}")
    for note in report.notes:
        lines.append(f"  · {note}")
    if report.stop_reason:
        lines.append(f"  · 已达自愈上限（{report.stop_reason}），停止重试并上报")
    if not lines:
        lines.append("  · 无可用方案（该工具未登记方案阶梯）")
    return lines
