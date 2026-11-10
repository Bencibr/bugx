#!/usr/bin/env python3
"""prompt_harness.py — BugX 阶段 Harness 与提示词编译装配中心 (DOC-038, DOC-043)。

提供：
  - load_stage: 按阶段加载 L1 提示词 (gate / explore / mutate / repair)
  - load_rule: 按名称加载 L2 专项规则库 (concurrency / memory_leak / prompt_injection / anti_patterns)
  - get_stage_prompt: 组装阶段 Harness 与动态上下文
  - analyze_source_rules: 基于 AST 静态推断源码关联规则 (DOC-043)
  - detect_unit_rule_slice: 依据 AST 特征或单元特质智能提取 L2 规则片段 (DOC-043)
  - get_stage_metadata: 获取阶段行为契约与异构模型分级建议 (DOC-043)
  - decorate_stage_task: 状态机驱动的主动任务修饰与 Harness 注入 (DOC-043)
  - enforce_section_budget: 注入分段的 Token 硬预算强制与截断留痕 (DOC-043, DOC-125)
  - enforce_global_budget: 在段级上限之内分配统一 Prompt 总预算
  - distill_evidence: 长堆栈与 Fuzz 输出结构化证据蒸馏 (DOC-043)
"""

from __future__ import annotations
from collections.abc import Sequence

import ast
from collections import OrderedDict
import logging
import os
from pathlib import Path
import re
from typing import Any, Mapping

from core.planning import unit_kind

try:
    from core.governance import token_budget
except ImportError:
    try:
        from core.governance import token_budget
    except ImportError:  # pragma: no cover
        token_budget = None  # type: ignore[assignment]

try:
    import core.i18n as i18n
except ImportError:
    try:
        from . import i18n
    except ImportError:
        i18n = None  # type: ignore[assignment]

def _get_skill_pack():
    from core.skills import skill_pack
    return skill_pack

try:
    from core.planning import domain_archetype
except ImportError:
    try:
        from . import domain_archetype
    except ImportError:  # pragma: no cover
        domain_archetype = None  # type: ignore[assignment]

SKILL_WORKER_AFFINITY_TIER = {
    "recon": ("Recon-Worker", "FAST"),
    "auth": ("Auth-Worker", "BALANCED"),
    "logic": ("Logic-Worker", "PRO"),
    "exploit": ("Exploit-Worker", "PRO"),
    "patch": ("Patch-Worker", "BALANCED"),
}

# DOC-043 / DOC-125 / DOC-248：各注入分段的硬预算上限（单位：conservative token）。
# 这些数值此前只是提示词里的标签字符串，本模块现在在出口真正强制它们。
# 数值来源：core/bugx-runtime-contract.md §下一代智能 Prompt Harness 契约。
HARNESS_SECTION_BUDGETS: dict[str, int] = {
    "l0_router": 1500,               # L0 路由入口
    "l1_stage_harness": 2500,        # L1 阶段专精 Harness
    "l2_rule_slice": 500,            # L2 专项规则智能切片
    "l2_risk_radar": 500,            # L2 代码智能脆弱性雷达
    "skill_l1_checklist": 300,       # 技能包 L1 核心行动清单（DOC-125 默认）
    "skill_attack_graph_slice": 200,  # 技能包 Attack Graph 切片摘要
    "skill_experience_hint": 600,    # 技能包 L2 经验检索建议（DOC-125 默认）
    # DOC-248 三维正交领域专精分段预算
    "header_role": 150,
    "role_sop": 1200,
    "tech_slice": 500,
    "probes_spec": 600,
    "domain_fixtures": 800,
    "task_context": 500,
}

# DOC-139 / DOC-248：段级预算的安全天花板。调用方可以把某一段预算调高，但不能
# 通过配置绕过这些上限；低于地板的值也不会让截断标记本身被截掉。
HARNESS_SECTION_BUDGET_CEILINGS: dict[str, int] = {
    "l0_router": 3000,
    "l1_stage_harness": 4000,
    "l2_rule_slice": 1200,
    "l2_risk_radar": 1200,
    "skill_l1_checklist": 800,
    "skill_attack_graph_slice": 600,
    "skill_experience_hint": 1500,
    "dynamic_context": 800,
    # DOC-248
    "header_role": 300,
    "role_sop": 2000,
    "tech_slice": 1000,
    "probes_spec": 1000,
    "domain_fixtures": 1500,
    "task_context": 1000,
}

# 这是段级 clamp 已经使用的最小可分配额度，而不是凭空新增的总预算。
# 全局预算由调用方实际启用的段级 ceiling 求和得到，避免另造一套数字。
HARNESS_SECTION_BUDGET_FLOOR = 50
# Keep a usable task specification even when a high-priority rule section is
# larger than the worker envelope; fail closed if the caller cannot afford it.
HARNESS_TASK_TEXT_FLOOR = 200

# The worker task itself is a budgeted section too.  These totals preserve the
# existing documented envelopes while preventing a full L1 card from reducing
# the actual task specification to the 50-token floor.
HARNESS_WORKER_GLOBAL_BUDGET = 3500
HARNESS_SKILL_GLOBAL_BUDGET = 1800

logger = logging.getLogger("bugx.prompt_harness")
_TOKEN_ESTIMATOR_FAILURE_LOGGED = False

_STAGE_MAP = {
    "l0": "l0_router.md",
    "gate": "stages/stage_gate.md",
    "explore": "stages/stage_explore.md",
    "mutate": "stages/stage_mutate.md",
    "repair": "stages/stage_repair.md",
}

_RULE_MAP = {
    "concurrency": "rules/concurrency_rules.md",
    "memory_leak": "rules/memory_leak_rules.md",
    "prompt_injection": "rules/prompt_injection_defense.md",
    "anti_patterns": "rules/anti_patterns.md",
}

_STAGE_TIER_MAP: dict[str, dict[str, Any]] = {
    "gate": {
        "tier": "tier_1_lightweight",
        "description": "快速轻量级模型 (Flash / Haiku / GPT-4o-mini)，适用于快速门禁与基线核验",
        "allowed_actions": ["check", "inspect", "diff", "pre_check"],
        "recommended_tools": ["bugx_supervisor_gate", "bugx_supervisor_status"],
    },
    "explore": {
        "tier": "tier_1_lightweight",
        "description": "高吞吐扫描模型 (Flash / Haiku / GPT-4o-mini)，适用于大规模单元与路由勘察",
        "allowed_actions": ["discover", "scan", "ast_inspect", "manifest_build", "mining_explore", "plan_audit"],
        "recommended_tools": ["bugx_mining_explore", "bugx_mining_audit", "bugx_mining_harden"],
    },
    "mutate": {
        "tier": "tier_2_deep_reasoning",
        "description": "深度推理模型 (Sonnet / DeepSeek-R1 / GPT-4o)，适用于并发竞态、状态推演与差分验证",
        "allowed_actions": ["fuzz", "perturb", "differential_test", "race_detect", "test_verify"],
        "recommended_tools": ["bugx_test_verify", "bugx_test_mutate", "bugx_fuzz_input", "bugx_fuzz_concurrency", "bugx_fuzz_stateful"],
    },
    "repair": {
        "tier": "tier_3_precise_code",
        "description": "高精度工程代码模型 (Claude 3.5 Sonnet / Codex)，适用于 TDD 最小修复与单测闭环",
        "allowed_actions": ["minimize_repro", "tdd_fix", "directed_test", "post_settle", "test_verify"],
        "recommended_tools": ["bugx_minimize_repro", "bugx_test_verify"],
    },
}

_AST_RULE_CACHE_MAX = 256
_AST_RULE_CACHE: OrderedDict[str, tuple[int, int, list[str]]] = OrderedDict()


def _cached_ast_rules(path: Path) -> list[str] | None:
    """Return rules only while the cached file fingerprint is unchanged."""
    key = str(path.resolve())
    try:
        stat = path.stat()
    except OSError:
        _AST_RULE_CACHE.pop(key, None)
        return None
    cached = _AST_RULE_CACHE.get(key)
    if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        _AST_RULE_CACHE.move_to_end(key)
        return cached[2]
    if cached:
        _AST_RULE_CACHE.pop(key, None)
    return None


def _cache_ast_rules(path: Path, rules: list[str]) -> None:
    key = str(path.resolve())
    try:
        stat = path.stat()
    except OSError:
        return
    _AST_RULE_CACHE[key] = (stat.st_mtime_ns, stat.st_size, rules)
    _AST_RULE_CACHE.move_to_end(key)
    while len(_AST_RULE_CACHE) > _AST_RULE_CACHE_MAX:
        _AST_RULE_CACHE.popitem(last=False)

_RE_PY_TRACE = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\w+)\n\s*(?P<code>.+)',
    re.MULTILINE,
)
_RE_EXC_LINE = re.compile(
    r'^(?P<exc>[A-Za-z0-9_]+(?:Error|Exception)):\s*(?P<msg>.*)$',
    re.MULTILINE,
)
_RE_ASSERT_FAIL = re.compile(r'AssertionError:\s*(.*)', re.DOTALL)


def _estimate_section_tokens(text: str) -> tuple[int, str]:
    """复用 ``token_budget.estimate_tokens``；缺失时退化为保守 UTF-8 估算。

    绝不返回“假装精确”的模型真实 token 数，同时保证本模块在 token_budget
    不可导入时仍能强制预算（fail-closed，而不是静默放行）。
    """
    if token_budget is not None:
        try:
            return token_budget.estimate_tokens(text)
        except Exception as exc:  # pragma: no cover - provider failure is environment-specific
            global _TOKEN_ESTIMATOR_FAILURE_LOGGED
            if not _TOKEN_ESTIMATOR_FAILURE_LOGGED:
                logger.warning("prompt token estimator failed; using conservative fallback: %s", exc)
                _TOKEN_ESTIMATOR_FAILURE_LOGGED = True
    raw = text.encode("utf-8")
    return max(1, (len(raw) + 2) // 3), "conservative-utf8-fallback"


class BudgetAllocationError(ValueError):
    """全局预算无法同时满足段级地板时的显式失败。"""


def _clamp_section_budget(name: str, requested: object, default: int) -> int:
    """应用段级地板/ceiling；非法配置显式失败，不静默放大预算。"""
    try:
        value = default if requested is None else int(requested)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BudgetAllocationError(f"{name} budget 必须是整数") from exc
    if isinstance(requested, bool):
        raise BudgetAllocationError(f"{name} budget 不接受布尔值")
    ceiling = HARNESS_SECTION_BUDGET_CEILINGS.get(name, value)
    return min(max(value, HARNESS_SECTION_BUDGET_FLOOR), ceiling)


def enforce_global_budget(
    sections: Mapping[str, str],
    total_budget: int,
    *,
    caps: Mapping[str, int] | None = None,
    floors: Mapping[str, int] | None = None,
    priorities: Mapping[str, int] | None = None,
    labels: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """在段级 ceiling 之内分配一个真实的全局预算。

    ``enforce_section_budget`` 仍负责最终的保留前缀与降级标记；本函数只
    决定每段在同一个总额中的额度。分配先满足每个非空段的 floor，再按
    ``priorities`` 从高到低给有剩余需求的段分配，无法满足 floor 时显式失败。
    未传 ``caps`` 时不猜数字，而是使用调用方给出的段级 ceiling/默认值。
    """
    if isinstance(total_budget, bool) or not isinstance(total_budget, int) or total_budget < 1:
        raise BudgetAllocationError("全局 prompt budget 必须是正整数")

    ordered = list(sections.items())
    cap_map = dict(caps or {})
    floor_map = dict(floors or {})
    priority_map = dict(priorities or {})
    label_map = dict(labels or {})
    prepared: list[dict[str, Any]] = []
    for index, (name, text) in enumerate(ordered):
        if not isinstance(text, str):
            raise BudgetAllocationError(f"{name} prompt section 必须是字符串")
        if not text.strip():
            continue
        default_cap = cap_map.get(name, total_budget)
        cap = _clamp_section_budget(name, default_cap, total_budget)
        requested_floor = floor_map.get(name, HARNESS_SECTION_BUDGET_FLOOR)
        floor = _clamp_section_budget(name, requested_floor, HARNESS_SECTION_BUDGET_FLOOR)
        if floor > cap:
            floor = cap
        actual, method = _estimate_section_tokens(text)
        desired = min(actual, cap)
        prepared.append({
            "name": name,
            "text": text,
            "actual": actual,
            "method": method,
            "cap": cap,
            "floor": min(desired, floor),
            "desired": desired,
            "priority": priority_map.get(name, -index),
            "label": label_map.get(name, name),
        })

    required = sum(item["floor"] for item in prepared)
    if required > total_budget:
        raise BudgetAllocationError(
            f"全局 prompt budget {total_budget}T 不足以满足段级地板 {required}T"
        )

    remaining = total_budget - required
    # 稳定排序：显式 priority 高的先分配，同优先级保留调用方顺序。
    allocation = {item["name"]: item["floor"] for item in prepared}
    for item in sorted(prepared, key=lambda value: -value["priority"]):
        extra = min(remaining, item["desired"] - allocation[item["name"]])
        allocation[item["name"]] += extra
        remaining -= extra
        if remaining == 0:
            break

    result: dict[str, str] = {}
    degradations: list[dict[str, Any]] = []
    for item in prepared:
        name = item["name"]
        text, record = enforce_section_budget(
            item["text"], allocation[name], item["label"],
        )
        result[name] = text
        if record:
            record["global_budget"] = total_budget
            record["allocated"] = allocation[name]
            degradations.append(record)
    # 保留空段，方便调用方不必重新拼装不存在的可选片段。
    result.update({name: text for name, text in ordered if name not in result})
    return result, degradations


def enforce_section_budget(
    text: str,
    cap: int,
    label: str,
) -> tuple[str, dict[str, Any] | None]:
    """把单段注入文本硬截断到 ``cap`` token 以内（DOC-043 / DOC-125）。

    返回 ``(可能被截断的文本, 截断记录或 None)``。截断记录包含 ``label`` /
    ``cap`` / ``tokens`` / ``token_method``，用于上层汇总为 DEGRADED 事件。
    截断处会留下显式标记，绝不静默丢弃内容。
    """
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        raise BudgetAllocationError(f"{label} budget 必须是正整数")
    if not text or not text.strip():
        return text, None

    tokens, method = _estimate_section_tokens(text)
    if tokens <= cap:
        return text, None

    marker = (
        f"\n... [BUDGET-DEGRADED: {label} 超出硬预算 {cap}T"
        f"（实测 {tokens}T），已硬截断；完整内容见原始文件] ..."
    )
    marker_tokens, _ = _estimate_section_tokens(marker)
    if marker_tokens > cap:
        raise BudgetAllocationError(
            f"{label} budget {cap}T 小于截断标记所需 {marker_tokens}T"
        )
    # 二分查找最大前缀，保证「前缀 + 标记」整体不超预算。
    lo, hi, best = 0, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _estimate_section_tokens(text[:mid] + marker)[0] <= cap:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    truncated = text[:best].rstrip() + marker
    return truncated, {
        "label": label,
        "cap": cap,
        "tokens": tokens,
        "token_method": method,
        "truncated": True,
    }


def _record_budget_degradations(
    degradations: list[dict[str, Any]],
    *,
    stage: str,
) -> str:
    """汇总截断记录：发出 DEGRADED 事件并渲染机器可读的降级记录段。"""
    if not degradations:
        return ""
    logger.warning(
        "harness.budget_degraded: stage=%s sections=%s",
        stage,
        ", ".join(
            f"{d['label']}({d['tokens']}T>{d['cap']}T)" for d in degradations
        ),
        extra={
            "bugx_event": "harness.budget_degraded",
            "status": "DEGRADED",
            "stage": stage,
            "sections": [
                {
                    "label": d["label"],
                    "cap": d["cap"],
                    "tokens": d["tokens"],
                    "token_method": d["token_method"],
                }
                for d in degradations
            ],
        },
    )
    lines = [
        "",
        "-" * 57,
        "=== Budget Degradation Record (DEGRADED) ===",
    ]
    for d in degradations:
        lines.append(
            f"- {d['label']}: 实测 {d['tokens']}T > 硬预算 {d['cap']}T，已硬截断"
        )
    return "\n".join(lines)


def get_prompts_dir(root: Path | None = None) -> Path:
    """定位全局引擎 prompts 根目录，兼容源码目录与安装目录。"""
    if root is not None:
        p1 = root / "core" / "prompts"
        if p1.is_dir():
            return p1
    current = Path(__file__).resolve().parent
    local_prompts = current / "prompts"
    if local_prompts.is_dir():
        return local_prompts

    home = os.environ.get("BUGX_HOME")
    if home:
        home_prompts = Path(home).expanduser() / "core" / "prompts"
        if home_prompts.is_dir():
            return home_prompts

    # 全局引擎根（BUGX_GLOBAL_ENGINE_ROOT 刻意不隔离：引擎是只读资产）。
    # 测试沙盒只隔离 HOME/BUGX_HOME，经它们解析会落到缺资产的假家；
    # 只读的阶段提示词应回到真实引擎根解析，否则「直跑单模块过、进
    # runner 的 shard 沙盒就 FileNotFoundError」。
    global_engine_root = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT")
    if global_engine_root:
        global_prompts = Path(global_engine_root).expanduser() / "prompts"
        if global_prompts.is_dir():
            return global_prompts

    # 回退至全局引擎 prompts
    default_home = Path.home() / ".bugx" / "core" / "prompts"
    return default_home


def list_available_stages() -> list[str]:
    return sorted(k for k in _STAGE_MAP.keys() if k != "l0")


# bugx.md 权威章节 → 阶段映射（P1，2026-09-06）。
# 阶段卡是精简执行摘要；详细规则的权威来源是 bugx.md 对应章节。
# 键 = prompt_harness 阶段名；值 = bugx.md 章节标题（与文内 `## `/`### ` 精确匹配）。
STAGE_SECTIONS: dict[str, tuple[str, ...]] = {
    "gate": (
        "当前执行口径（唯一生效规则）",
        "宿主原生交互适配",
        "宪法",
    ),
    "explore": (
        "工作流程（每轮内的完整动作）",
        "持久化状态文件",
    ),
    "mutate": (
        "Round 内 Life 执行（严格遵守）",
        "目标内容注入防御（对抗性红线）",
    ),
    "repair": (
        "修复环节（发现之后必须做的闭环）",
        "寿命结算规则（决定你的生死）",
        "错题集（经验记忆：反思 → 归类 → 复用）",
        "bug 记录清单（只记录模式的产物，也是全模式的去重依据）",
    ),
}


def stage_sections(stage: str, root: Path | None = None) -> list[str]:
    """返回该阶段在 bugx.md 的权威章节标题（存在性已校验）。

    显式传入 root 时严格以 `<root>/core/bugx.md` 为准，缺失返回空——
    协议测试沙盒不得静默回退读取安装引擎的真实 bugx.md。
    """
    if root is not None:
        guide = root / "core" / "bugx.md"
        if not guide.is_file():
            return []
    else:
        guide = get_prompts_dir(root).parent / "bugx.md"
    if not guide.is_file():
        return []
    try:
        text = guide.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    return [
        title for title in STAGE_SECTIONS.get(stage.strip().lower(), ())
        if re.search(rf"^##+\s*{re.escape(title)}\s*$", text, re.M)
    ]


def _stage_section_pointers(stage: str, root: Path | None = None) -> str:
    """生成阶段卡尾部的权威章节指针段（机器生成，不漂移）。"""
    titles = stage_sections(stage, root)
    if not titles:
        return ""
    lines = ["", "## 权威章节指针（bugx.md 按需提取；本卡与全文冲突时以全文为准）"]
    for title in titles:
        lines.append(
            f"- bugx.md §{title}（`bugx engine path --name bugx.md` 后按章节读取）"
        )
    return "\n".join(lines)


def load_stage(stage: str, root: Path | None = None) -> str:
    """加载指定阶段的提示词内容。"""
    key = stage.strip().lower()
    if key not in _STAGE_MAP:
        raise ValueError(
            f"未知的阶段: {stage}，可选: {', '.join(sorted(_STAGE_MAP.keys()))}"
        )
    prompts_dir = get_prompts_dir(root)
    file_path = prompts_dir / _STAGE_MAP[key]
    if not file_path.is_file():
        raise FileNotFoundError(f"阶段提示词文件缺失: {file_path}")
    content = file_path.read_text(encoding="utf-8").strip()
    # P1：附加 bugx.md 权威章节指针，消除"阶段卡 vs 全文"双源竞争。
    content += _stage_section_pointers(key, root)
    if key == "l0":
        # CLI 的 ``prompt --stage l0`` 直接调用 load_stage，因此 L0 不能
        # 只在 get_stage_prompt 中受预算保护。
        sections, degradations = enforce_global_budget(
            {"l0_router": content},
            HARNESS_SECTION_BUDGETS["l0_router"],
            caps={"l0_router": HARNESS_SECTION_BUDGETS["l0_router"]},
            labels={"l0_router": "L0 路由入口"},
        )
        return sections["l0_router"] + _record_budget_degradations(
            degradations, stage="l0",
        )
    return content


def load_rule(rule_name: str, root: Path | None = None) -> str:
    """加载指定名称的规则库内容。"""
    key = rule_name.strip().lower()
    if key not in _RULE_MAP:
        raise ValueError(
            f"未知的规则: {rule_name}，可选: {', '.join(sorted(_RULE_MAP.keys()))}"
        )
    prompts_dir = get_prompts_dir(root)
    file_path = prompts_dir / _RULE_MAP[key]
    if not file_path.is_file():
        raise FileNotFoundError(f"专项规则文件缺失: {file_path}")
    return file_path.read_text(encoding="utf-8").strip()


def get_stage_prompt(
    stage: str,
    context: Mapping[str, object] | None = None,
    root: Path | None = None,
    locale: str | None = None,
) -> str:
    """组装阶段提示词，注入动态上下文事实 (DOC-043, DOC-067, DOC-139)。"""
    key = stage.strip().lower()
    base_prompt = load_stage(key, root=root)
    if not context:
        # L0 是常驻入口，也必须经过和其他注入段相同的硬预算出口。
        if key == "l0":
            return base_prompt
        return base_prompt

    title = "## 动态运行时事实（Dynamic Context）"
    if i18n is not None:
        title = i18n.prompt_text("harness.dynamic_context_title", locale=locale, fallback=title)
    context_lines = [f"\n\n{title}"]
    for k, v in context.items():
        value = str(v)
        if len(value) > 500:
            value = value[:470] + " ...[TRUNCATED]"
        context_lines.append(f"- **{k}**: {value}")
    context_text = "\n".join(context_lines)
    context_text, context_degradations = enforce_section_budget(
        context_text,
        _clamp_section_budget(
            "dynamic_context",
            HARNESS_SECTION_BUDGET_CEILINGS["dynamic_context"],
            HARNESS_SECTION_BUDGET_CEILINGS["dynamic_context"],
        ),
        "Dynamic Context 事实注入",
    )
    if key == "l0":
        # L0 与动态事实共同受一个总额约束；不把动态事实偷偷追加到
        # 已经满额的常驻路由卡之后。
        sections, degradations = enforce_global_budget(
            {"l0_router": base_prompt, "dynamic_context": context_text},
            HARNESS_SECTION_BUDGETS["l0_router"]
            + HARNESS_SECTION_BUDGET_CEILINGS["dynamic_context"],
            caps={
                "l0_router": HARNESS_SECTION_BUDGETS["l0_router"],
                "dynamic_context": HARNESS_SECTION_BUDGET_CEILINGS["dynamic_context"],
            },
            priorities={"l0_router": 2, "dynamic_context": 1},
            labels={
                "l0_router": "L0 路由入口",
                "dynamic_context": "Dynamic Context 事实注入",
            },
        )
        return sections["l0_router"] + sections["dynamic_context"] + _record_budget_degradations(
            context_degradations + degradations, stage="l0",
        )
    return base_prompt + context_text + _record_budget_degradations(
        context_degradations, stage=key,
    )


def get_stage_metadata(stage: str, locale: str | None = None) -> dict[str, Any]:
    """返回指定阶段的模型分级与行为契约元数据 (DOC-043, DOC-067)。"""
    key = stage.strip().lower()
    meta = _STAGE_TIER_MAP.get(key)
    if meta is None:
        desc = "通用默认模型"
        if i18n is not None:
            desc = i18n.prompt_text("tier.default.description", locale=locale, fallback=desc)
        return {
            "stage": key,
            "tier": "tier_1_lightweight",
            "description": desc,
            "allowed_actions": ["general_audit"],
        }
    desc = meta.get("description", "")
    if i18n is not None:
        desc = i18n.prompt_text(f"tier.{key}.description", locale=locale, fallback=desc)
    return {
        "stage": key,
        **meta,
        "description": desc,
    }



def analyze_source_rules(source_code: str) -> list[str]:
    """通过 Python AST 静态推断源码所涉及的技术领域，返回匹配的规则名列表 (DOC-043)。"""
    if not source_code or not source_code.strip():
        return []
    rules: list[str] = []
    try:
        tree = ast.parse(source_code)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return []

    has_concurrency = False
    has_leak = False
    has_injection = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.lower()
                if any(m in name for m in ("threading", "multiprocessing", "asyncio", "concurrent.futures")):
                    has_concurrency = True
                if any(m in name for m in ("socket", "sqlite3", "psycopg", "pymysql", "sqlalchemy", "requests", "aiohttp", "urllib")):
                    has_leak = True
                if any(m in name for m in ("pickle", "yaml", "subprocess", "shutil")):
                    has_injection = True
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").lower()
            if any(m in mod for m in ("threading", "multiprocessing", "asyncio", "concurrent")):
                has_concurrency = True
            if any(m in mod for m in ("socket", "sqlite3", "psycopg", "pymysql", "sqlalchemy", "urllib")):
                has_leak = True
            if any(m in mod for m in ("pickle", "yaml", "subprocess")):
                has_injection = True
        elif isinstance(node, (ast.AsyncFunctionDef, ast.AsyncWith, ast.Await)):
            has_concurrency = True
        elif isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id.lower()
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr.lower()

            # Concurrency calls
            if func_name in ("acquire", "release", "lock", "rlock", "semaphore", "event", "condition"):
                has_concurrency = True
            # Resource leak calls
            if func_name in ("open", "connect", "cursor", "listen", "accept", "socket", "session", "connection"):
                has_leak = True
            # Injection / parsing calls
            if func_name in ("loads", "load", "eval", "exec", "popen", "system", "run", "check_output"):
                has_injection = True

    if has_concurrency:
        rules.append("concurrency")
    if has_leak:
        rules.append("memory_leak")
    if has_injection:
        rules.append("prompt_injection")
    return rules


def detect_unit_rule_slice(
    unit_id: str,
    root: Path | None = None,
    source_code: str | None = None,
    file_path: Path | str | None = None,
) -> str | None:
    """依据覆盖单元特质（AST 语法树特征或名称关键词），智能匹配并提取对应的 L2 规则片段 (DOC-043)。"""
    matched_rule: str | None = None

    # 1. 优先尝试 AST 静态推断
    code_to_parse = source_code
    target_file = Path(file_path) if file_path else None
    if not code_to_parse and target_file and target_file.is_file() and target_file.suffix == ".py":
        ast_rules = _cached_ast_rules(target_file)
        if ast_rules is not None:
            if ast_rules:
                matched_rule = ast_rules[0]
        else:
            try:
                code_to_parse = target_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                pass

    if not matched_rule and not code_to_parse and root:
        cand_rel = str(unit_id).split(":")[0]
        f_p = (Path(root) / cand_rel).resolve()
        if f_p.is_file() and f_p.suffix == ".py":
            ast_rules = _cached_ast_rules(f_p)
            if ast_rules is not None:
                if ast_rules:
                    matched_rule = ast_rules[0]
            else:
                try:
                    code_to_parse = f_p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass

    if code_to_parse and not matched_rule:
        ast_rules = analyze_source_rules(code_to_parse)
        if target_file:
            _cache_ast_rules(target_file, ast_rules)
        elif root:
            cand_rel = str(unit_id).split(":")[0]
            f_p = (Path(root) / cand_rel).resolve()
            if f_p.is_file():
                _cache_ast_rules(f_p, ast_rules)
        if ast_rules:
            matched_rule = ast_rules[0]

    # 2. 回退降级至名称关键字推断
    if not matched_rule:
        normalized = unit_id.lower()
        if any(kw in normalized for kw in ("lock", "race", "thread", "async", "concurrent", "mutex", "deadlock")):
            matched_rule = "concurrency"
        elif any(kw in normalized for kw in ("leak", "pool", "socket", "stream", "fd", "handle", "memory", "gc")):
            matched_rule = "memory_leak"
        elif any(kw in normalized for kw in ("parse", "json", "ast", "payload", "fuzz", "inject", "sanitize")):
            matched_rule = "prompt_injection"

    if not matched_rule:
        return None

    try:
        content = load_rule(matched_rule, root=root)
        lines = content.splitlines()
        # 提取前 15 行核心规则摘要，避免 token 膨胀
        summary = "\n".join(lines[:15]).strip()
        return f"【领域定向规则推荐 - {matched_rule}】\n{summary}"
    except (FileNotFoundError, ValueError):
        return None


def get_unit_risk_advice(unit_id: str, root: Path | None = None) -> str | None:
    """提取当前单元所属模块的代码智能内在风险雷达报告与变异攻击建议 (DOC-060)。"""
    if not unit_id or not unit_id.strip():
        return None

    clean_id = unit_kind.strip_unit_prefix(unit_id)
    parts = clean_id.split(":")
    if len(parts) > 1 and parts[0].isdigit():
        clean_id = ":".join(parts[1:])
    fp = clean_id.split("::")[0].strip()
    if not fp:
        return None

    target_root = root or Path.cwd()
    profile_path = target_root / ".bugx" / "knowledge" / "project-profile.json"
    if not profile_path.is_file():
        return None

    try:
        import json
        pdata = json.loads(profile_path.read_text(encoding="utf-8"))
        risk_metrics = pdata.get("risk_metrics") or {}
        hotspots = risk_metrics.get("top_risk_hotspots") or []

        matched_hotspot = None
        for h in hotspots:
            if isinstance(h, dict) and "file" in h:
                h_file = str(h["file"]).replace("\\", "/")
                if h_file == fp or h_file.endswith(fp) or fp.endswith(h_file):
                    matched_hotspot = h
                    break

        if not matched_hotspot:
            return None

        cc = matched_hotspot.get("cyclomatic_complexity", 1)
        cog = matched_hotspot.get("cognitive_complexity", 0)
        tags = matched_hotspot.get("tags") or []
        sinks = matched_hotspot.get("sensitive_sinks", 0)

        advice_points = []
        if "critical-taint" in tags or "sensitive-sinks" in tags:
            advice_points.append("发现入参直通敏感执行/反序列化/代码调用槽位，重点设计命令注入、特殊字符逃逸与反序列化边界 Payload。")
        if "deep-nesting" in tags or "high-cognitive" in tags:
            advice_points.append(f"认知复杂度较高 ({cog})，嵌套深且分支复杂，重点测试多重分支边界与反常序调用。")
        if "hardcoded-secret" in tags:
            advice_points.append("扫描到疑似高熵机密，重点核验凭证未授权访问与身份绕过。")
        if not advice_points:
            advice_points.append(f"圈复杂度 {cc}，重点设计边界值反转与异常条件注入。")

        tags_str = " · ".join(tags[:4]) if tags else "high-complexity"
        return (
            f"【⚠️ 代码智能脆弱性雷达 (Vulnerability Radar)】\n"
            f"• 模块特征: CC={cc} | 认知负荷={cog} | 敏感槽位={sinks} | 标签: {tags_str}\n"
            f"• 渗透指引: {' '.join(advice_points)}"
        )
    except Exception:
        return None


def decorate_skill_stage_task(
    stage_id: str,
    task_text: str,
    *,
    skill_id: str = "web-audit",
    worker_affinity: str = "logic",
    attack_graph_slice: str = "",
    experience_hint: str = "",
    root: Path | None = None,
    locale: str | None = None,
    budgets: Mapping[str, Any] | None = None,
    global_budget: int | None = None,
) -> str:
    """DOC-125: 审计技能包双层 Token 硬隔离任务修饰器。"""
    b = dict(budgets or {})
    max_l1 = _clamp_section_budget(
        "skill_l1_checklist",
        b.get("max_l1_checklist_tokens"),
        HARNESS_SECTION_BUDGETS["skill_l1_checklist"],
    )
    max_exp_cards = b.get("max_experience_cards", 3)
    max_exp_tokens = _clamp_section_budget(
        "skill_experience_hint",
        b.get("max_experience_tokens"),
        HARNESS_SECTION_BUDGETS["skill_experience_hint"],
    )
    max_attack_graph = _clamp_section_budget(
        "skill_attack_graph_slice",
        b.get("max_attack_graph_tokens"),
        HARNESS_SECTION_BUDGETS["skill_attack_graph_slice"],
    )

    affinity_info = SKILL_WORKER_AFFINITY_TIER.get(
        worker_affinity.lower(),
        (f"{worker_affinity.title()}-Worker", "PRO"),
    )
    worker_label, tier_label = affinity_info

    l1_checklist = ""
    l2_playbook_path = f"core/audit_packs/{skill_id}/stages/{stage_id}.md"
    prompts_dir = get_prompts_dir(root)
    audit_packs_base = prompts_dir.parent / "audit_packs"
    skills_base = audit_packs_base if audit_packs_base.is_dir() else prompts_dir / "skills"
    if not (prompts_dir.parent / l2_playbook_path).exists() and (prompts_dir / f"skills/{skill_id}/stages/{stage_id}.md").exists():
        l2_playbook_path = f"core/prompts/skills/{skill_id}/stages/{stage_id}.md"
    skill_pack = _get_skill_pack()
    if skill_pack is not None:
        try:
            pack = skill_pack.load_skill_pack(skill_id, skills_dir=skills_base)
            stage_info = pack.get("stages", {}).get(stage_id)
            if stage_info:
                l1_checklist = stage_info.get("l1_checklist", "")
                l2_playbook_path = stage_info.get("file_path", l2_playbook_path)
                try:
                    if root:
                        l2_playbook_path = str(Path(l2_playbook_path).relative_to(root))
                except (ValueError, OSError):
                    pass
        except Exception:
            pass

    if not l1_checklist:
        l1_checklist = f"1. 执行 {stage_id} 审计任务；产出结构化验证证据与对照实验数据。"

    # DOC-125/DOC-139：段级 ceiling/clamp 后，再由同一个全局出口分配
    # 总额。任务正文保留独立地板，避免完整 L1 卡吞掉可执行任务。
    sections, degradations = enforce_global_budget(
        {
            "skill_l1_checklist": l1_checklist,
            "skill_attack_graph_slice": attack_graph_slice.strip(),
            "skill_experience_hint": experience_hint.strip(),
            "task_text": task_text,
        },
        global_budget
        if global_budget is not None
        else int(
            b.get(
                "global_budget",
                b.get("total_budget", HARNESS_SKILL_GLOBAL_BUDGET),
            )
        ),
        caps={
            "skill_l1_checklist": max_l1,
            "skill_attack_graph_slice": max_attack_graph,
            "skill_experience_hint": max_exp_tokens,
        },
        priorities={
            "skill_l1_checklist": 4,
            "task_text": 3,
            "skill_attack_graph_slice": 2,
            "skill_experience_hint": 1,
        },
        floors={"task_text": HARNESS_TASK_TEXT_FLOOR},
        labels={
            "skill_l1_checklist": "技能包 L1 核心行动清单",
            "skill_attack_graph_slice": "Attack Graph 切片摘要",
            "skill_experience_hint": "技能包 L2 经验检索建议",
            "task_text": "任务主体",
        },
    )
    l1_checklist = sections["skill_l1_checklist"]
    attack_graph_slice = sections["skill_attack_graph_slice"]
    experience_hint = sections["skill_experience_hint"]
    task_text = sections["task_text"]

    banner_top = "=== BugX Stage Harness (Active Injection) ==="
    stage_header = f"[Stage: {stage_id.upper()} · Worker: {worker_label} · Tier: {tier_label}]"
    sep = "-" * 57

    lines = [
        banner_top,
        stage_header,
        sep,
        f"[L1 核心行动清单 (Hard Cap: ≤{max_l1} tokens)]:",
        f"  {l1_checklist}",
        f"  遇到复杂拦截，使用 Read 工具查阅攻防手册:\n     {l2_playbook_path}",
    ]

    if attack_graph_slice:
        lines.extend([
            sep,
            f"[Attack Graph 切片摘要 (≤{max_attack_graph} tokens)]:",
            f"  {attack_graph_slice}",
        ])

    if experience_hint:
        lines.extend([
            sep,
            f"[L2 经验检索建议 (有界检索: ≤{max_exp_cards} 卡/{max_exp_tokens}T)]:",
            f"  {experience_hint}",
        ])

    lines.extend([
        sep,
        "=== 任务主体 (Task Specification) ===",
        task_text,
    ])
    return "\n".join(lines) + _record_budget_degradations(degradations, stage=stage_id)


def decorate_stage_task(
    stage: str,
    task_text: str,
    unit_id: str = "",
    root: Path | None = None,
    context: Mapping[str, object] | None = None,
    locale: str | None = None,
    global_budget: int | None = None,
) -> str:
    """为 Worker 任务主动注入当前阶段的 Harness 前缀与智能切片 (Active Injection) (DOC-043, DOC-060, DOC-067, DOC-125)。"""
    eff_locale = locale or (os.environ.get("BUGX_LOCALE") or "zh-CN")

    # DOC-125: 若为技能包阶段或显式声明 skill_plan，走技能包双层 Harness 修饰
    if stage.startswith(("00-", "10-", "20-", "30-", "40-", "50-", "60-", "70-", "80-")) or (context and "skill_plan" in context):
        skill_id = "web-audit"
        stage_id = stage
        worker_affinity = "logic"
        attack_graph_slice = ""
        experience_hint = ""
        budgets = None
        if context:
            plan = context.get("skill_plan")
            if isinstance(plan, Mapping):
                skill_id = str(plan.get("skill_id", skill_id))
                b = plan.get("budgets")
                if isinstance(b, Mapping):
                    budgets = b
            if "stage_id" in context:
                stage_id = str(context["stage_id"])
            if "worker_affinity" in context:
                worker_affinity = str(context["worker_affinity"])
            if "attack_graph_slice" in context:
                attack_graph_slice = str(context["attack_graph_slice"])
            if "experience_hint" in context:
                experience_hint = str(context["experience_hint"])
        return decorate_skill_stage_task(
            stage_id=stage_id,
            task_text=task_text,
            skill_id=skill_id,
            worker_affinity=worker_affinity,
            attack_graph_slice=attack_graph_slice,
            experience_hint=experience_hint,
            root=root,
            locale=eff_locale,
            budgets=budgets,
            global_budget=global_budget,
        )

    stage_prompt = get_stage_prompt(stage, context=context, root=root, locale=eff_locale)
    rule_slice = detect_unit_rule_slice(unit_id, root=root) if unit_id else None
    risk_radar = get_unit_risk_advice(unit_id, root=root) if unit_id else None
    meta = get_stage_metadata(stage, locale=eff_locale)

    banner_top = "=== BugX Stage Harness (Active Injection) ==="
    stage_info = f"当前阶段: {stage.upper()} · 推荐模型等级: {meta['tier']}"
    task_spec_banner = "=== 任务主体 (Task Specification) ==="
    if i18n is not None:
        banner_top = i18n.prompt_text("harness.banner_top", locale=eff_locale, fallback=banner_top)
        stage_info = i18n.prompt_text(
            "harness.stage_info",
            locale=eff_locale,
            fallback=stage_info,
            stage=stage.upper(),
            tier=meta["tier"],
        )
        task_spec_banner = i18n.prompt_text("harness.task_spec_banner", locale=eff_locale, fallback=task_spec_banner)

    # DOC-043/DOC-139：把 L1、L2 和任务主体交给同一个全局出口；任务主体
    # 不再是预算旁路，并保留可执行的正文地板。
    sections, degradations = enforce_global_budget(
        {
            "l1_stage_harness": stage_prompt,
            "l2_rule_slice": rule_slice or "",
            "l2_risk_radar": risk_radar or "",
            "task_text": task_text,
        },
        global_budget
        if global_budget is not None
        else HARNESS_WORKER_GLOBAL_BUDGET,
        caps={
            "l1_stage_harness": HARNESS_SECTION_BUDGETS["l1_stage_harness"],
            "l2_rule_slice": HARNESS_SECTION_BUDGETS["l2_rule_slice"],
            "l2_risk_radar": HARNESS_SECTION_BUDGETS["l2_risk_radar"],
        },
        priorities={
            "l1_stage_harness": 4,
            "task_text": 3,
            "l2_rule_slice": 2,
            "l2_risk_radar": 1,
        },
        floors={"task_text": HARNESS_TASK_TEXT_FLOOR},
        labels={
            "l1_stage_harness": "L1 阶段专精 Harness",
            "l2_rule_slice": "L2 专项规则智能切片",
            "l2_risk_radar": "L2 代码智能脆弱性雷达",
            "task_text": "任务主体",
        },
    )
    stage_prompt = sections["l1_stage_harness"]
    rule_slice = sections["l2_rule_slice"]
    risk_radar = sections["l2_risk_radar"]
    task_text = sections["task_text"]

    lines = [
        banner_top,
        stage_info,
        "-" * 45,
        stage_prompt,
    ]
    if rule_slice:
        lines.extend([
            "-" * 45,
            rule_slice,
        ])
    if risk_radar:
        lines.extend([
            "-" * 45,
            risk_radar,
        ])
    lines.extend([
        task_spec_banner,
        task_text,
    ])
    return "\n".join(lines) + _record_budget_degradations(degradations, stage=stage)


def route_source_inspection(
    path: str,
    *,
    symbol: str | None = None,
    kind: str | None = None,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Determine whether a source inspection request should route to structured slice retrieval.

    When inspecting code with a specific symbol or method target, routes to bugx_retrieve_slice
    for dense, self-contained syntax slices, while falling back safely to bugx_inspect_source.
    """
    path_clean = str(path).strip()
    is_code = path_clean.endswith((
        ".py", ".go", ".rs", ".js", ".ts", ".jsx", ".tsx",
        ".c", ".cpp", ".cc", ".h", ".hpp", ".java", ".kt", ".swift",
    ))
    if is_code and symbol and symbol.strip():
        return {
            "prefer_slice": True,
            "target_tool": "bugx_retrieve_slice",
            "slice_args": {
                "target": path_clean,
                "symbol": symbol.strip(),
                "kind": kind,
                "to_executable": True,
                "include_context": True,
            },
            "fallback_tool": "bugx_inspect_source",
            "fallback_args": {
                "path": path_clean,
                "start_line": start_line,
                "end_line": end_line,
            },
        }
    return {
        "prefer_slice": False,
        "target_tool": "bugx_inspect_source",
        "slice_args": None,
        "fallback_tool": "bugx_inspect_source",
        "fallback_args": {
            "path": path_clean,
            "start_line": start_line,
            "end_line": end_line,
        },
    }



def distill_evidence(raw_output: str, max_lines: int = 15, locale: str | None = None) -> str:
    """将冗长复杂的终端异常堆栈或 Fuzz 运行日志蒸馏为 ≤15 行结构化 Markdown 证据提要 (DOC-043, DOC-067)。"""
    eff_locale = locale or (os.environ.get("BUGX_LOCALE") or "zh-CN")
    no_output = "无输出内容"
    if i18n is not None:
        no_output = i18n.prompt_text("evidence.no_output", locale=eff_locale, fallback=no_output)

    if not raw_output or not raw_output.strip():
        return no_output

    text = raw_output.strip()
    # 1. 尝试提取 Python Traceback
    tb_matches = list(_RE_PY_TRACE.finditer(text))
    exc_match = _RE_EXC_LINE.search(text)

    distilled_lines: list[str] = []
    if tb_matches or exc_match:
        distilled_title = "### 🔍 结构化异常证据提要 (Distilled Evidence)"
        if i18n is not None:
            distilled_title = i18n.prompt_text("evidence.distilled_title", locale=eff_locale, fallback=distilled_title)
        distilled_lines.append(distilled_title)

        if exc_match:
            exc_line = f"- **异常类型**: `{exc_match.group('exc')}`"
            err_line = f"- **错误详情**: {exc_match.group('msg').strip()}"
            if i18n is not None:
                exc_line = i18n.prompt_text("evidence.exc_type", locale=eff_locale, fallback=exc_line, exc=exc_match.group('exc'))
                err_line = i18n.prompt_text("evidence.error_detail", locale=eff_locale, fallback=err_line, msg=exc_match.group('msg').strip())
            distilled_lines.append(exc_line)
            distilled_lines.append(err_line)

        if tb_matches:
            # 取最深一层的项目内或目标代码触发点
            last_frame = tb_matches[-1]
            loc_line = f"- **触发位置**: `{last_frame.group('file')}:{last_frame.group('line')}` in `{last_frame.group('func')}`"
            fault_line = f"- **故障代码**: `{last_frame.group('code').strip()}`"
            if i18n is not None:
                loc_line = i18n.prompt_text(
                    "evidence.trigger_location",
                    locale=eff_locale,
                    fallback=loc_line,
                    location=f"{last_frame.group('file')}:{last_frame.group('line')}",
                    func=last_frame.group('func'),
                )
                fault_line = i18n.prompt_text(
                    "evidence.fault_code",
                    locale=eff_locale,
                    fallback=fault_line,
                    code=last_frame.group('code').strip(),
                )
            distilled_lines.append(loc_line)
            distilled_lines.append(fault_line)

            if len(tb_matches) > 1:
                call_chain = " ➔ ".join(m.group("func") for m in tb_matches[-3:])
                chain_line = f"- **调用链末端**: `{call_chain}`"
                if i18n is not None:
                    chain_line = i18n.prompt_text(
                        "evidence.call_chain",
                        locale=eff_locale,
                        fallback=chain_line,
                        chain=call_chain,
                    )
                distilled_lines.append(chain_line)

        # 检查是否有断言失败差异
        assert_match = _RE_ASSERT_FAIL.search(text)
        if assert_match:
            first_assert_line = assert_match.group(1).strip().splitlines()[0]
            diff_line = f"- **断言差异**: `{first_assert_line[:120]}`"
            if i18n is not None:
                diff_line = i18n.prompt_text(
                    "evidence.assert_diff",
                    locale=eff_locale,
                    fallback=diff_line,
                    diff=first_assert_line[:120],
                )
            distilled_lines.append(diff_line)

        footer = "_（完整日志已归档至 `.bugx/logs/bugx.jsonl`，可通过 `bugx log` 查看）_"
        if i18n is not None:
            footer = i18n.prompt_text("evidence.footer", locale=eff_locale, fallback=footer)
        distilled_lines.append(footer)
        return "\n".join(distilled_lines[:max_lines])

    # 2. 兜底通用压缩：保留头部 4 行与尾部 8 行
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text

    head = lines[:4]
    tail = lines[-8:]
    omitted = len(lines) - 12
    omitted_msg = f"... [省略 {omitted} 行冗余输出，完整记录查阅 .bugx/logs/bugx.jsonl] ..."
    if i18n is not None:
        omitted_msg = i18n.prompt_text("evidence.omitted", locale=eff_locale, fallback=omitted_msg, omitted=omitted)
    compressed = head + [omitted_msg] + tail
    return "\n".join(compressed[:max_lines])



def parse_structured_evidence(text: str, distilled: str | None = None) -> dict[str, object]:
    """从原始终端输出或 Traceback 中提取结构化异常元数据 (DOC-045)。"""
    tb_matches = list(_RE_PY_TRACE.finditer(text))
    exc_match = _RE_EXC_LINE.search(text)
    assert_match = _RE_ASSERT_FAIL.search(text)

    location = ""
    function_name = ""
    failing_code = ""
    call_chain: list[str] = []
    if tb_matches:
        last = tb_matches[-1]
        location = f"{last.group('file')}:{last.group('line')}"
        function_name = last.group("func")
        failing_code = last.group("code").strip()
        call_chain = [m.group("func") for m in tb_matches]

    assertion_diff = ""
    if assert_match:
        assertion_diff = assert_match.group(1).strip().splitlines()[0][:120]

    msg = exc_match.group("msg").strip() if exc_match else ""
    return {
        "exception_type": exc_match.group("exc") if exc_match else "Unknown",
        "error_message": msg,
        "summary": msg,
        "failing_location": location,
        "failing_function": function_name,
        "failing_code": failing_code,
        "call_chain": call_chain,
        "assertion_diff": assertion_diff,
        "distilled_summary": distilled or distill_evidence(text, max_lines=5),
    }


# ── DOC-248 三维正交技术栈规则切片 (300~500 Tokens) ───────────────────
TECH_STACK_RULE_SLICES: dict[str, str] = {
    "java": (
        "### [Tech Stack Slice: Java / JVM & Spring Ecosystem (DOC-248)]\n"
        "- 反序列化利用链 (Deserialization Gadgets):\n"
        "  * 重点审计 Fastjson (`@type` 自动类型解析)、Jackson (`enableDefaultTyping` / `TypeNameHandling`) 以及原生 `ObjectInputStream`。\n"
        "  * 检查类路径中是否存在 Commons-Collections, Spring-Beans, C3P0, Rome 等已知 RCE 利用链；禁止反序列化外部不可信字节流。\n"
        "- 表达式注入与远程代码执行 (SpEL & OGNL):\n"
        "  * 审查 SpEL 表达式解析：用户输入传入 `ExpressionParser.parseExpression().getValue()` 时必须使用只读 `SimpleEvaluationContext`，禁止传入高权限 `StandardEvaluationContext`。\n"
        "- 数据访问与 SQL 拼接 (MyBatis / JPA / Hibernate):\n"
        "  * 排查 MyBatis XML 中的 `${}` 直接字符串拼接（尤其是 ORDER BY、LIKE、IN 语句），强制重构为 `#{}` 参数化占位符。\n"
        "- JNDI 与远程服务查找:\n"
        "  * `InitialContext.lookup()` 严禁拼接用户可控协议前缀（如 `ldap://`, `rmi://`）；防范 Log4j / Fastjson 驱动的 JNDI 注入。\n"
        "- 暴露面与配置泄露:\n"
        "  * 检查 Spring Boot Actuator (`/actuator/env`, `/actuator/heapdump`, `/actuator/gateway`) 是否未授权对外暴露敏感环境变量或堆内存。"
    ),
    "csharp": (
        "### [Tech Stack Slice: C# / .NET Ecosystem (DOC-248)]\n"
        "- 危险反序列化 (Unsafe Deserialization):\n"
        "  * 彻底禁用 `BinaryFormatter.Deserialize()`；审查 `Json.NET` 是否启用了危险的 `TypeNameHandling.Auto` / `All` / `Objects`。\n"
        "  * 防范 `NetDataContractSerializer`, `LosFormatter`, `SoapFormatter` 等历史遗留易受反序列化攻击的类型组件。\n"
        "- ViewState 与 MAC 完整性校验:\n"
        "  * ASP.NET WebForms 中 ViewState 必须开启 MAC 校验 (`EnableViewStateMac=true`)；严禁硬编码默认 `machineKey` 导致远程执行。\n"
        "- 路径穿越与绝对路径覆盖风险:\n"
        "  * `Path.Combine(basePath, userInput)`: 在 .NET 中若 `userInput` 以 `/` 或 `\\` 开头，将被视作绝对根路径直接覆盖 `basePath`，造成致命越权路径写。\n"
        "- 动态表达式与 LINQ 注入:\n"
        "  * `System.Linq.Dynamic.Core` 动态构造查询字符串时必须使用参数化占位符，严禁字符串拼接触发任意方法调用注入。\n"
        "- 外部通信与 WCF 安全:\n"
        "  * 审查 WCF / .NET Remoting 契约端点，杜绝未认证暴露或明文传输敏感凭据。"
    ),
    "kotlin": (
        "### [Tech Stack Slice: Kotlin & Android Native (DOC-248)]\n"
        "- JNI 与 Native C/C++ 内存生命周期:\n"
        "  * JNI 本地方法调用中跨边界指针的生命周期管理；排查内存释放后使用 (UAF) 与缓冲区越界。\n"
        "  * 审查 `GetStringUTFChars` 与 `ReleaseStringUTFChars` 是否严格成对释放，防范 Native 堆内存泄漏。\n"
        "- 协程异常安全与死锁:\n"
        "  * 审查 `CoroutineExceptionHandler` 与 `SupervisorJob` 作用域；防止子协程未捕获异常级联取消根作用域引发 App 崩溃。\n"
        "  * 警惕在挂起函数中持有非协程安全的锁 (如 Java 原生 `synchronized` 或 `ReentrantLock`) 引发的协程调度死锁。\n"
        "- 弱引用与生命周期持有:\n"
        "  * 检查 `WeakReference` 在异步回调中的空安全校验；防止 Activity 销毁后异步任务持有野指针引发空指针异常 (NPE) 或内存泄漏。\n"
        "- 移动端组件导出与 Intent 走私:\n"
        "  * 检查 `AndroidManifest.xml` 中未受保护的 `android:exported=\"true\"` 组件与未鉴权 DeepLink。\n"
        "  * 构造 `PendingIntent` 时必须显式声明 `FLAG_IMMUTABLE`，杜绝恶意第三方 App 篡改内部 Intent 参数。"
    ),
    "go": (
        "### [Tech Stack Slice: Go / Golang Systems (DOC-248)]\n"
        "- Goroutine 闭包变量捕获竞态 (Closure Capture Race):\n"
        "  * 审查循环体中并发启动的 goroutine: `for i, v := range items { go func() { use(v) }() }`，必须显式以参数传递或在循环局部重新绑定。\n"
        "- Channel 阻塞与 Goroutine 泄漏:\n"
        "  * 审查无缓冲 Channel 在超时或发送异常时的阻塞；确保每个 `context.WithTimeout` / `WithCancel` 都被及时 `defer cancel()`。\n"
        "- 数据读写竞争 (Data Race Invariant):\n"
        "  * 共享变量（如全局 map、切片、统计计数器）必须使用 `sync.Mutex` / `sync.RWMutex` 或 `sync/atomic` 保护，严禁无锁并发读写。\n"
        "- SQL 拼接与命令执行:\n"
        "  * `database/sql` 必须使用参数占位符 (`?` 或 `$1`)，严禁使用 `fmt.Sprintf` 拼接 SQL 语句。\n"
        "  * `os/exec.Command` 禁止拼接外部不可信字符串调用 `sh -c` / `bash -c`，防范命令注入。"
    ),
    "python": (
        "### [Tech Stack Slice: Python & Modern Frameworks (DOC-248)]\n"
        "- 属性与原型污染 (Attribute & Prototype Pollution):\n"
        "  * 递归字典更新或动态属性赋值时，必须严格拦截 `__class__`, `__globals__`, `__subclasses__`, `__builtins__`，防范沙箱逃逸与元对象篡改。\n"
        "- 不安全反序列化与模板渲染:\n"
        "  * 彻底禁止加载外部输入的 `pickle.loads()`；`yaml.load()` 必须显式声明 `Loader=SafeLoader`。\n"
        "  * 审查 Jinja2 / Django 模板渲染，严禁直接拼接用户输入至 `render_template_string` 引发的 SSTI。\n"
        "- 多线程与 GIL 释放竞态:\n"
        "  * 审查调用底层 C-Extension 或 I/O 密集型代码释放 GIL 后的并发共享变量读写，确保线程安全。\n"
        "- 进程命令执行与路径拼接:\n"
        "  * `subprocess.Popen(..., shell=True)` 传入变量必须经 `shlex.quote` 转义或改用列表传参。\n"
        "  * `os.path.join(base, user_input)`: 当 `user_input` 包含前导 `/` 时会静默丢弃 `base`，必须做安全前缀检查与规范化。"
    ),
    "generic": (
        "### [Tech Stack Slice: Generic Systems & Secure Invariants (DOC-248)]\n"
        "- 输入边界验证: 外部不可信输入必须在入口层执行严格白名单类型、长度与正则过滤。\n"
        "- 状态守恒与鉴权对齐: 权限校验与底层资源访问必须处于同一原子上下文中，杜绝 TOCTOU 竞争。\n"
        "- 防御性失败 (Fail-Closed): 发生异常时必须安全熔断，严禁空异常捕获，禁止对外回显调试错误堆栈。"
    ),
}

# ── DOC-248 动态黑盒探针工具契约 ──────────────────────────────────────
PROBE_SPECIFICATIONS: dict[str, str] = {
    "web_dynamic_probe": (
        "### [黑盒探针: WebDynamicProbe (Playwright & Raw Socket)]\n"
        "- 工具接口: `headless_browser_action` (DOM 点击/表单提交/Cookie 读取), `http_raw_request` (HTTP/1.1 与 HTTP/2 畸形头/管道走私)\n"
        "- 操作规范: 结合网络状态码与 DOM 执行树验证 XSS、CORS 凭据泄露及请求走私漏洞。"
    ),
    "android_probe": (
        "### [黑盒探针: AndroidProbe (APK Decompile & ADB Dispatcher)]\n"
        "- 工具接口: `apk_decompile_manifest` (解析 exported 组件与 DeepLink 过滤器), `adb_send_intent` (跨进程触发 Intent 越权)\n"
        "- 操作规范: 验证能否未认证越权拉起私有 Activity/Service，或通过伪造 Intent Extras 提取沙箱 SQLite 敏感数据。"
    ),
    "ios_probe": (
        "### [黑盒探针: IOSProbe (Mach-O Symbols & Plist Security)]\n"
        "- 工具接口: `macho_symbol_inspector` (未剥离符号表与 IPC 接口), `ios_plist_security_gate` (URL Schemes 伪造/Keychain 共享)\n"
        "- 操作规范: 审查私有 URL Schemes 劫持、ATS 强制放宽及敏感数据明文落盘隐患。"
    ),
    "miniprogram_probe": (
        "### [黑盒探针: MiniProgramProbe (Subpackage Router & Cloud Functions)]\n"
        "- 工具接口: `miniprogram_router_crawler` (`app.json` 全量子包未授权直达), `cloud_function_probe` (`OPENID` 缺失探测)\n"
        "- 操作规范: 验证能否绕过前端登录守卫直达管理员分包页面，或未校验 `OPENID` 篡改他人数据。"
    ),
    "binary_probe": (
        "### [黑盒探针: BinaryProbe (ASan Instrumentation & Fuzzing)]\n"
        "- 工具接口: `run_fuzz_mutation` (边界变异生成器), Clang `-fsanitize=address,undefined` 编译插桩\n"
        "- 操作规范: 针对 C/C++ 内存分配、指针运算驱动边界变异测试，捕获缓冲区溢出与 UAF 崩溃。"
    ),
}

# ── DOC-248 五大核心行为角色 SOP ──────────────────────────────────────
ROLE_SOP_SPECIFICATIONS: dict[str, str] = {
    "Recon-Worker": (
        "### [核心行为角色 SOP: Recon-Worker (广度优先资产侦察)]\n"
        "- **思维模式**: 广度覆盖、无死角测绘、暴露面拓扑建模。\n"
        "- **核心职责**:\n"
        "  1. 梳理系统所有对外暴露接口、路由、未授权端点与组件入口；\n"
        "  2. 提取入参模式与身份约束边界，构建初始攻击面假说图谱；\n"
        "  3. 快速归纳资产拓扑，为下游专精 Worker 提供高价值靶标索引，禁止做耗时单点深挖。"
    ),
    "Auth-Worker": (
        "### [核心行为角色 SOP: Auth-Worker (鉴权与越权专精挖掘)]\n"
        "- **思维模式**: 对抗性身份切换、双租户交叉矩阵推演、租户物理隔离边界审计。\n"
        "- **核心职责**:\n"
        "  1. 必须始终基于注入的双租户身份矩阵 (Role A vs Role B) 进行对比验证；\n"
        "  2. 深挖水平越权 (BOLA/IDOR)、垂直提权、Token 签名失效与未授权访问；\n"
        "  3. 严格执行跨租户断言门禁：Role A 请求 Tenant B 必须返回 401/403，任何返回 200 或泄漏数据一律裁决为高危越权。"
    ),
    "Logic-Worker": (
        "### [核心行为角色 SOP: Logic-Worker (状态机与并发竞态专精)]\n"
        "- **思维模式**: 时序推演、微秒级竞争碰撞、状态跃迁合法性审查、守恒定律验证。\n"
        "- **核心职责**:\n"
        "  1. 针对支付、充值、扣款、库存、卡券等状态转换，强制执行并发竞态压测；\n"
        "  2. 严格使用 `ThreadPoolExecutor(max_workers=10)` 脚手架，受 3.0s 硬熔断保护 (AUDIT-04)；\n"
        "  3. 验证业务状态守恒：并发 10 线程扣款必须严格仅成功 1 笔，余额绝不可为负数。"
    ),
    "Exploit-Worker": (
        "### [核心行为角色 SOP: Exploit-Worker (深度利用与差分验证)]\n"
        "- **思维模式**: 污点溯源 (Source->Sanitizer->Sink)、实锤验证、零误报物理差分。\n"
        "- **核心职责**:\n"
        "  1. 沿 AST 污点路径追踪外部输入至高危 Sink 点 (SQL, 命令执行, 反序列化, SSTI, SpEL)；\n"
        "  2. 严格遵循 AUDIT-05：必须产出独立的双沙箱 0/1 退出码差分验证脚本；\n"
        "  3. 严禁仅凭 HTTP 500/403 单独定性，只有攻击沙箱确凿复现且退出码为 1 才可立案。"
    ),
    "Patch-Worker": (
        "### [核心行为角色 SOP: Patch-Worker (规范修复与回归闭环)]\n"
        "- **思维模式**: 最小外科手术式修改、防御性契约加固、零业务功能回归、绝对路径物理脱敏。\n"
        "- **核心职责**:\n"
        "  1. 依据 CWE 知识库制定针对性修复方案，根除病灶而非表面遮掩；\n"
        "  2. 生成自动化验证脚本 `verify.py`，确保退出码 0 为修复且无回归，退出码 1 为失败；\n"
        "  3. 严格执行绝对路径脱敏红线：补丁与脚本中绝对禁止出现本地 `/Users/...` 或 `/home/...`。"
    ),
}


def build_domain_specialized_prompt(
    worker_role: str,
    target_form: str = "backend",
    tech_stack: str = "generic",
    active_probes: Sequence[str] = (),
    *,
    task_desc: str = "",
    unit_id: str = "",
    file_path: str = "",
    symbol_name: str = "",
    ast_features: Mapping[str, Any] | None = None,
    max_prompt_tokens: int = 2200,
    root: Path | None = None,
    locale: str | None = None,
    extra_context: Mapping[str, Any] | None = None,
) -> str:
    """DOC-248: 三维正交领域专精 JIT 提示词拼装与硬预算门禁。

    参数:
      worker_role: 核心角色 (recon, auth, logic, exploit, patch 或对应 Worker 名)
      target_form: 目标形态 (backend, web, mobile_android, mobile_ios, miniprogram, binary)
      tech_stack: 技术栈领域 (java, csharp, kotlin, go, python, generic)
      active_probes: 挂载的黑盒动态探针清单
      max_prompt_tokens: 严格硬预算上限 (AUDIT-03，默认 <= 2200)

    按角色优先级保障：核心 SOP (100) > 技术栈切片 (80) > 探针描述 (70) > 领域夹具 (50) > 任务上下文 (40)。
    """
    # 1. 规范化核心角色与亲和性等级
    role_raw = (worker_role or "generalist").strip()
    role_key = role_raw.lower().replace("-worker", "")
    if role_key in SKILL_WORKER_AFFINITY_TIER:
        role_label, tier_label = SKILL_WORKER_AFFINITY_TIER[role_key]
        affinity = role_key
    else:
        role_label = f"{role_key.title()}-Worker" if not role_raw.endswith("-Worker") else role_raw
        affinity = role_key
        tier_label = "BALANCED"

    # 2. 规范化形态与技术栈
    target_form_norm = (target_form or "backend").strip().lower()
    tech_norm = (tech_stack or "generic").strip().lower()
    if tech_norm in ("c#", "cs", ".net"):
        tech_norm = "csharp"
    elif tech_norm in ("kt", "kts"):
        tech_norm = "kotlin"
    elif tech_norm in ("golang",):
        tech_norm = "go"
    elif tech_norm in ("py",):
        tech_norm = "python"
    elif tech_norm not in TECH_STACK_RULE_SLICES:
        tech_norm = "generic"

    # 3. 规范化探针清单
    probes = list(active_probes or ())
    if not probes:
        if target_form_norm == "web":
            probes = ["web_dynamic_probe"]
        elif target_form_norm in ("mobile_android", "android"):
            probes = ["android_probe"]
        elif target_form_norm in ("mobile_ios", "ios"):
            probes = ["ios_probe"]
        elif target_form_norm in ("miniprogram", "wechat"):
            probes = ["miniprogram_probe"]
        elif target_form_norm in ("binary", "embedded"):
            probes = ["binary_probe"]

    # 4. 构建分段
    # A. 头部基础信息
    header_text = (
        f"## BugHunter 领域专精 Worker JIT 提示词 (DOC-248)\n"
        f"- **专精角色**: {role_label} (亲和性: {affinity}, 建议模型等级: {tier_label})\n"
        f"- **目标形态**: {target_form_norm}\n"
        f"- **技术栈知识**: {tech_norm}\n"
        f"- **挂载动态探针**: {', '.join(probes) if probes else '无（纯静态代码审计）'}\n"
    )

    # B. 核心角色 SOP
    role_sop_text = ROLE_SOP_SPECIFICATIONS.get(
        role_label,
        ROLE_SOP_SPECIFICATIONS.get(
            f"{role_key.title()}-Worker",
            f"### [核心行为角色 SOP: {role_label}]\n- 依据领域专精特征执行针对性审计任务。\n",
        ),
    )

    # C. 技术栈规则切片 (300~500T)
    tech_slice_text = TECH_STACK_RULE_SLICES.get(tech_norm, TECH_STACK_RULE_SLICES["generic"])

    # D. 探针描述
    probe_items = []
    for p in probes:
        spec = PROBE_SPECIFICATIONS.get(p)
        if spec:
            probe_items.append(spec)
        else:
            probe_items.append(f"### [黑盒探针: {p}]\n已挂载探针工具，请依据形态契约执行交互验证。")
    probes_text = "\n\n".join(probe_items) if probe_items else ""

    # E. 领域专属测试底座夹具 (Auth, Logic, Exploit, Patch, Mobile, Web)
    if domain_archetype is not None:
        fixture_pack = domain_archetype.generate_domain_fixtures(
            worker_role=role_label,
            target_form=target_form_norm,
            tech_stack=tech_norm,
        )
    else:
        fixture_pack = {
            "worker_role": role_label,
            "target_form": target_form_norm,
            "tech_stack": tech_norm,
            "fixtures": {},
            "scaffold_code": "",
            "guidance": "",
        }

    fixtures_text_parts = ["### [领域专属测试底座夹具 (Domain Synthetic Fixtures)]"]
    if fixture_pack.get("guidance"):
        fixtures_text_parts.append(f"**操作指引**: {fixture_pack['guidance']}")

    fixtures_dict = fixture_pack.get("fixtures", {})
    if "dual_identity_token_matrix" in fixtures_dict:
        matrix = fixtures_dict["dual_identity_token_matrix"]
        ra, rb = matrix.get("role_a", {}), matrix.get("role_b", {})
        fixtures_text_parts.append(
            "- **双租户身份矩阵 (Role A vs Role B)**:\n"
            f"  * Role A (租户 A): User={ra.get('user_id')}, Tenant={ra.get('tenant_id')}, Token={ra.get('token')}\n"
            f"  * Role B (租户 B): User={rb.get('user_id')}, Tenant={rb.get('tenant_id')}, Token={rb.get('token')}\n"
            "- **跨租户访问断言**: Role A 请求 Tenant B 必须返回 401/403，任何 200/204 均判定为 BOLA/IDOR 严重越权。"
        )

    if "concurrency_threadpool_10" in fixtures_dict:
        fixtures_text_parts.append(
            "- **并发竞态压测契约 (AUDIT-04)**:\n"
            "  * 物理并发上限: max_workers <= 10 (严格限制并发度防雪崩)\n"
            "  * 硬熔断时间: 3.0s 超时强制中断 (防止死锁)\n"
            "  * 状态守恒校验: 10 线程并发扣款必须严格只有 1 笔成功，余额不得为负。"
        )

    if "twin_sandbox_oracle" in fixtures_dict:
        fixtures_text_parts.append(
            "- **AST 污点上下文与 0/1 差分真值 (AUDIT-05)**:\n"
            "  * 污点追踪: Source -> Sanitizer -> Sink 完整路径分析\n"
            "  * 0/1 差分门禁: 基准良性沙箱退出码为 0，攻击利用沙箱确凿复现且退出码为 1；严禁凭单一 HTTP 状态码定性。"
        )

    if "cwe_knowledge_base" in fixtures_dict:
        fixtures_text_parts.append(
            "- **CWE 知识库与规范修复**:\n"
            "  * 参照 CWE 规则实施针对性最小修复，根除根本病灶。\n"
            "  * 绝对路径物理脱敏: 补丁与脚本中绝对禁止出现本地 `/Users/...` 或 `/home/...`，必须统一使用 `<PROJECT_ROOT>` 或仓库相对路径替代。\n"
            "  * verify.py 规范: 独立自动化验证脚本，退出码 0 代表成功修复且无回归，退出码 1 代表失败。"
        )

    if fixture_pack.get("scaffold_code"):
        code_snip = fixture_pack["scaffold_code"].strip()
        fixtures_text_parts.append(f"```python\n{code_snip}\n```")

    fixtures_text = "\n\n".join(fixtures_text_parts)

    # F. 任务上下文
    task_context_parts = []
    if unit_id or file_path or symbol_name or task_desc:
        task_context_parts.append("### [任务上下文与被测标的]")
        if unit_id:
            task_context_parts.append(f"- **单元标识**: `{unit_id}`")
        if file_path:
            task_context_parts.append(f"- **源码路径**: `{file_path}`")
        if symbol_name:
            task_context_parts.append(f"- **目标符号**: `{symbol_name}`")
        if task_desc:
            task_context_parts.append(f"- **任务描述**: {task_desc}")
    task_context_text = "\n".join(task_context_parts) if task_context_parts else ""

    # 5. 拼装与预算硬裁剪 (AUDIT-03)
    sections = OrderedDict()
    sections["header_role"] = header_text
    sections["role_sop"] = role_sop_text
    sections["tech_slice"] = tech_slice_text
    if probes_text:
        sections["probes_spec"] = probes_text
    sections["domain_fixtures"] = fixtures_text
    if task_context_text:
        sections["task_context"] = task_context_text

    max_tokens = max(100, int(max_prompt_tokens))
    separator_tokens = len(sections) * 2
    effective_budget = max(50 * len(sections), max_tokens - separator_tokens)

    # 角色优先级策略：保核心 SOP (100) > 技术栈切片 (80) > 探针描述 (70) > 领域夹具 (50) > 任务上下文 (40)
    priorities = {
        "header_role": 90,
        "role_sop": 100,
        "tech_slice": 80,
        "probes_spec": 70,
        "domain_fixtures": 50,
        "task_context": 40,
    }

    num_sections = len(sections)
    dyn_floor = min(HARNESS_SECTION_BUDGET_FLOOR, effective_budget // (num_sections * 2))
    dyn_floor = max(10, dyn_floor)
    floors = {k: dyn_floor for k in sections}

    try:
        budgeted_sections, degradations = enforce_global_budget(
            sections,
            total_budget=effective_budget,
            priorities=priorities,
            floors=floors,
        )
        assembled = "\n\n".join(text for text in budgeted_sections.values() if text.strip())
    except BudgetAllocationError:
        raw_combined = "\n\n".join(text for text in sections.values() if text.strip())
        assembled, _ = enforce_section_budget(raw_combined, max_tokens, "domain_specialized_prompt")

    final_tokens, _ = _estimate_section_tokens(assembled)
    if final_tokens > max_tokens:
        assembled, _ = enforce_section_budget(assembled, max_tokens, "domain_specialized_prompt")

    return assembled


def build_worker_prompt(
    worker_role: str,
    target_form: str = "backend",
    tech_stack: str = "generic",
    active_probes: Sequence[str] = (),
    **kwargs: Any,
) -> str:
    """DOC-248: build_domain_specialized_prompt 别名与外层通用包装。"""
    return build_domain_specialized_prompt(
        worker_role=worker_role,
        target_form=target_form,
        tech_stack=tech_stack,
        active_probes=active_probes,
        **kwargs,
    )


__all__ = [
    "BudgetAllocationError",
    "HARNESS_SECTION_BUDGET_CEILINGS",
    "HARNESS_SECTION_BUDGETS",
    "HARNESS_SECTION_BUDGET_FLOOR",
    "HARNESS_TASK_TEXT_FLOOR",
    "HARNESS_SKILL_GLOBAL_BUDGET",
    "HARNESS_WORKER_GLOBAL_BUDGET",
    "PROBE_SPECIFICATIONS",
    "ROLE_SOP_SPECIFICATIONS",
    "SKILL_WORKER_AFFINITY_TIER",
    "TECH_STACK_RULE_SLICES",
    "analyze_source_rules",
    "build_domain_specialized_prompt",
    "build_worker_prompt",
    "decorate_stage_task",
    "detect_unit_rule_slice",
    "distill_evidence",
    "enforce_section_budget",
    "enforce_global_budget",
    "get_prompts_dir",
    "get_stage_metadata",
    "get_stage_prompt",
    "list_available_stages",
    "load_rule",
    "load_stage",
    "parse_structured_evidence",
]

