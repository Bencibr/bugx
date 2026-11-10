#!/usr/bin/env python3
"""guide_budget.py — 指南上下文预算度量：每文档、每章节的 token 成本。

bugx.md 全文是每次会话的固定上下文地板（实测约 5 万 conservative token）。
本工具按 Markdown 标题切分统计各层/各章节成本，为「L0 常驻 + 章节按需
加载」的分层策略提供度量，防止指南回潮膨胀。

用法：
  python3 guide_budget.py [--top 15] [--json]
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterable, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPT_DIR) not in sys.path:
    # 幂等注入：本模块会被 `spec_from_file_location` 重复执行（预算门禁按仓库 core/
    # 显式加载），无条件 insert 会让 sys.path 每次多一条，撞坏
    # `test_spec_contracts::test_core_dir_is_added_to_sys_path_exactly_once`。
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from .token_budget import estimate_tokens  # noqa: E402
except ImportError:
    from core.governance.token_budget import estimate_tokens  # noqa: E402

SYSTEM_PROMPT_BUDGET_LIMITS = {
    "L0_router": 1500,        # L0 路由入口 (l0_router.md)
    "L1_stage": 2500,         # L1 各阶段卡 (stage_*.md)
    "L2_rule": 1500,          # L2 各专项规则 (rules/*.md)
    "worker": 4000,           # Worker 提示词 (bugx-worker.md)
    "host_skill": 3600,       # 宿主 Skill 入口 (SKILL.md) 承载 AI 执行元指南、决策树与紧凑命令雷达 (DOC-251)
    "opencode_entry": 3500,   # OpenCode 精简入口 (bugx-opencode.md)
    "agent_guide_startup": 1800,  # AGENT_GUIDE 启动协议单章 (DOC-034 验收判据)
    "agent_guide_total": 8500,    # AGENT_GUIDE 全文上限
    "quick_card": 1000,           # bugx-quick.md 极速卡上限
    "bugx_full": 38000,           # bugx.md 全文上限 (DOC-034 验收判据)
    "l0_ratio_max": 0.10,         # L0 常驻占比上限 (DOC-034 验收判据)
}

# 路径相对 ``core/``（``SCRIPT_DIR``）。契约类文档统一归档在 ``contracts/``、
# 账本与知识库分别归档在 ``ledger/`` 与 ``planning/``——迁移后旧的扁平名已不存在，
# 而 ``measure()`` 对缺失文件是**静默跳过**的，路径一漂就会少统计若干层且不报错。
# 因此每个条目都必须指向真实存在的文件，并由
# ``tests/governance/test_token_economy.py`` 的覆盖守卫钉住条数。
DOC_LAYERS = [
    ("L0", "prompts/l0_router.md"),
    ("L1", "contracts/bugx-quick.md"),
    ("L1", "contracts/bugx-opencode.md"),
    ("L1", "contracts/AGENT_GUIDE.md"),
    ("L1", "prompts/stages/stage_gate.md"),
    ("L1", "prompts/stages/stage_explore.md"),
    ("L1", "prompts/stages/stage_mutate.md"),
    ("L1", "prompts/stages/stage_repair.md"),
    ("L1", "bugx.md"),
    ("L2", "prompts/rules/anti_patterns.md"),
    ("L2", "prompts/rules/concurrency_rules.md"),
    ("L2", "prompts/rules/memory_leak_rules.md"),
    ("L2", "prompts/rules/prompt_injection_defense.md"),
    ("L2", "ledger/mistake-book.md"),
    ("L2", "planning/tools-kb.md"),
]


def _sections(text: str) -> list[tuple[str, str]]:
    """按 ##/### 标题切分；返回 (标题, 正文) 列表，首个无标题段记为 preamble。"""
    parts: list[tuple[str, str]] = []
    current_title = "(preamble)"
    buffer: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(("# ", "## ", "### ")):
            if buffer:
                parts.append((current_title, "\n".join(buffer)))
                buffer = []
            current_title = line.lstrip().lstrip("#").strip() or "(untitled)"
        buffer.append(line)
    if buffer:
        parts.append((current_title, "\n".join(buffer)))
    return parts


def slice_guide_context(
    text: str,
    sections: Iterable[str] | None = None,
    *,
    max_chars: int = 12000,
    required_markers: Iterable[str] = (),
) -> str:
    """按标题白名单切片指南，返回可直接注入 Worker 的 bounded 文本。

    ``required_markers`` 用于调用方声明不可丢失的安全/证据短语；切片后缺失时
    显式失败，避免为了节省上下文而静默移除门禁。未知标题也显式失败。
    """
    if not isinstance(text, str):
        raise ValueError("指南内容必须是字符串")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars 必须是正整数")
    available = _sections(text)
    by_title = {title.casefold(): body for title, body in available}
    wanted = [str(item).strip() for item in (sections or []) if str(item).strip()]
    unknown = [item for item in wanted if item.casefold() not in by_title]
    if unknown:
        raise ValueError(f"指南章节不存在: {', '.join(unknown)}")
    selected = [by_title[item.casefold()] for item in wanted] if wanted else [body for _, body in available]
    result = "\n\n".join(selected).strip()
    if len(result) > max_chars:
        marker = "\n...[BugX guide slice truncated]\n"
        if max_chars <= len(marker):
            raise ValueError("max_chars 太小，无法保留切片标记")
        cut = result[:max_chars - len(marker)].rsplit("\n", 1)[0].rstrip()
        result = cut + marker
    missing = [marker for marker in required_markers if str(marker) not in result]
    if missing:
        raise ValueError(f"切片缺少必需规则: {', '.join(map(str, missing))}")
    return result


def recommend_budget(
    units: Iterable[Mapping[str, object]],
    *,
    total_budget: int,
    min_budget: int = 1,
) -> dict:
    """按已观测 Finding/新行为把预算倾斜到热点单元，只给建议不改调度状态。"""
    records = [item for item in units if isinstance(item, Mapping)]
    if not records:
        raise ValueError("units 不能为空")
    if not isinstance(total_budget, int) or isinstance(total_budget, bool) or total_budget < 1:
        raise ValueError("total_budget 必须是正整数")
    if not isinstance(min_budget, int) or isinstance(min_budget, bool) or min_budget < 1:
        raise ValueError("min_budget 必须是正整数")
    if total_budget < len(records) * min_budget:
        raise ValueError("total_budget 不足以覆盖每个单元的最小预算")
    weights: list[float] = []
    normalized: list[dict] = []
    for index, raw in enumerate(records):
        unit_id = str(raw.get("unit_id") or raw.get("id") or f"unit-{index + 1}")
        findings = max(0, int(raw.get("findings", 0) or 0))
        new_behaviors = max(0, int(raw.get("new_behaviors", 0) or 0))
        weight = 1.0 + findings * 4.0 + new_behaviors * 2.0
        weights.append(weight)
        normalized.append({
            "unit_id": unit_id, "findings": findings,
            "new_behaviors": new_behaviors, "weight": weight,
        })
    remaining = total_budget - len(records) * min_budget
    weight_sum = sum(weights)
    raw_extra = [remaining * weight / weight_sum for weight in weights]
    extras = [int(value) for value in raw_extra]
    remainder = remaining - sum(extras)
    order = sorted(range(len(records)), key=lambda index: (-raw_extra[index] + extras[index], index))
    for index in order[:remainder]:
        extras[index] += 1
    allocations = []
    for index, item in enumerate(normalized):
        budget = min_budget + extras[index]
        reason = "baseline"
        if item["findings"] or item["new_behaviors"]:
            reason = "hot-streak" if item["findings"] else "new-behavior"
        allocations.append({**item, "budget": budget, "reason": reason})
    return {
        "schema_version": 1,
        "total_budget": total_budget,
        "min_budget": min_budget,
        "allocations": allocations,
        "policy": "recommendation-only",
    }


# P4 规范语气度量词表：硬性措辞（红线词 + 强调词）。仅用于度量定位，不改写语义。
_MODAL_TERMS = ("必须", "禁止", "不得", "严禁", "绝对", "务必", "一定要")


def modal_density_report(threshold: float = 25.0) -> list[dict]:
    """输出规范密度超阈值的章节（降序），供语气治理定位。

    threshold：每千 token 硬性措辞数；超过即视为密度异常。
    """
    report: list[dict] = []
    for document in measure()["documents"]:
        for section in document["sections"]:
            density = section.get("modal_density")
            if density is not None and density >= threshold:
                report.append({
                    "file": document["file"],
                    "title": section["title"],
                    "tokens": section["tokens"],
                    "modal_density": density,
                })
    return sorted(report, key=lambda item: -item["modal_density"])


def measure() -> dict:
    documents = []
    missing: list[str] = []
    for layer, name in DOC_LAYERS:
        path = SCRIPT_DIR / name
        if not path.is_file():
            # 静默跳过会让"少统计 5 层"伪装成"预算达标"。缺失必须可见。
            missing.append(name)
            continue
        raw = path.read_bytes()
        total_tokens, method = estimate_tokens(raw)
        text = raw.decode("utf-8", errors="replace")
        sections = []
        for title, body in _sections(text):
            tokens, _ = estimate_tokens(body.encode("utf-8"))
            section_entry = {"title": title, "tokens": tokens,
                             "lines": body.count("\n") + 1}
            # P4 度量：规范语气密度（每千 token 的硬性措辞数）。
            # 密度过高 = 重点稀释；用于定位需要语气治理的章节。
            modal_count = sum(body.count(word) for word in _MODAL_TERMS)
            if tokens:
                section_entry["modal_density"] = round(
                    modal_count * 1000 / tokens, 1
                )
            sections.append(section_entry)
        documents.append({
            "layer": layer, "file": name, "bytes": len(raw),
            "lines": text.count("\n") + 1, "tokens": total_tokens,
            "token_method": method,
            "sections": sorted(sections, key=lambda x: -x["tokens"]),
        })
    l0_total = sum(d["tokens"] for d in documents if d["layer"] == "L0")
    l1_total = sum(d["tokens"] for d in documents if d["layer"] == "L1")
    return {
        "schema_version": 1,
        "documents": documents,
        "layers": {
            "L0_quick_always": l0_total,
            "L1_full_guide_on_demand": l1_total,
            "L0_vs_L1_ratio": round(l0_total / l1_total, 4) if l1_total else None,
        },
        "missing": missing,
        "guidance": (
            "worker 常驻只加载 L0；L1 章节按需 sed 提取。"
            "任何指南 PR 都应先跑本工具对比增量。"
        ),
    }


def _prompt_body(text: str) -> str:
    """剥掉 YAML frontmatter，只量模型实际读到的提示词正文。

    宿主 agent 文档的 frontmatter（description/mode/permissions）是**宿主
    解析的配置元数据**，不进入模型上下文；预算门禁约束的是系统提示词成本，
    因此 worker/opencode 入口等 agent 文档按正文计量。无 frontmatter 时原样返回。
    """
    if text.startswith("---"):
        stripped = text[3:]
        end = stripped.find("\n---")
        if end != -1:
            return stripped[end + 4:].lstrip("\n")
    return text


def check_budget_compliance() -> tuple[bool, list[str]]:
    """检查系统提示词 2.0 是否满足 Token 预算门禁。"""
    errors: list[str] = []
    # 1. 检查 L0 路由
    l0_path = SCRIPT_DIR / "prompts" / "l0_router.md"
    if l0_path.is_file():
        tokens, _ = estimate_tokens(l0_path.read_bytes())
        if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["L0_router"]:
            errors.append(f"L0 路由入口超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['L0_router']}")

    # 2. 检查各 L1 阶段卡
    stages_dir = SCRIPT_DIR / "prompts" / "stages"
    if stages_dir.is_dir():
        for p in stages_dir.glob("stage_*.md"):
            tokens, _ = estimate_tokens(p.read_bytes())
            if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["L1_stage"]:
                errors.append(f"L1 阶段卡 {p.name} 超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['L1_stage']}")

    # 3. 检查各 L2 专项规则
    rules_dir = SCRIPT_DIR / "prompts" / "rules"
    if rules_dir.is_dir():
        for p in rules_dir.glob("*.md"):
            tokens, _ = estimate_tokens(p.read_bytes())
            if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["L2_rule"]:
                errors.append(f"L2 专项规则 {p.name} 超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['L2_rule']}")

    # 4. 检查 worker 提示词
    worker_path = SCRIPT_DIR / "bugx-worker.md"
    if worker_path.is_file():
        tokens, _ = estimate_tokens(_prompt_body(worker_path.read_text(encoding="utf-8")).encode("utf-8"))
        if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["worker"]:
            errors.append(f"Worker 提示词超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['worker']}")

    # 5. 检查宿主 Skill 入口（**版本受控的源**，不是安装副本）
    #
    # 历史缺陷：这里读 ``plugins/bugx/skills/bugx/SKILL.md``，而 ``plugins/bugx/``
    # 被 ``.gitignore`` 忽略——它是 ``bugx install`` 的产物。于是这个门禁有两个
    # 反向失效：(a) 干净克隆里该文件不存在 → ``is_file()`` 跳过 → **静默通过**；
    # (b) 开发机上它比源文件旧/新不一致 → 拿废弃副本报超标（实测 3643 > 3600，
    # 而受控源只有 3397）。门禁必须盯住提交进仓库的那份。
    repo_root = SCRIPT_DIR.parent
    skill_path = repo_root / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx" / "SKILL.md"
    if not skill_path.is_file():
        # fail-closed：源文件缺失说明契约被动过，不能当作「无需检查」。
        errors.append(
            f"宿主 Skill 源缺失: {skill_path.relative_to(repo_root).as_posix()}"
            f"（预算门禁不能靠安装副本判定）"
        )
    else:
        tokens, _ = estimate_tokens(_prompt_body(skill_path.read_text(encoding="utf-8")).encode("utf-8"))
        if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["host_skill"]:
            errors.append(f"宿主 Skill 入口超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['host_skill']}")

    # 6. 检查 OpenCode 精简入口
    opencode_path = SCRIPT_DIR / "bugx-opencode.md"
    if opencode_path.is_file():
        tokens, _ = estimate_tokens(_prompt_body(opencode_path.read_text(encoding="utf-8")).encode("utf-8"))
        if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["opencode_entry"]:
            errors.append(f"OpenCode 入口超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['opencode_entry']}")

    # 7. 检查 AGENT_GUIDE.md 启动协议章节与全文 (DOC-034/DOC-039)
    guide_path = SCRIPT_DIR / "AGENT_GUIDE.md"
    if guide_path.is_file():
        text = guide_path.read_text(encoding="utf-8")
        total_tokens, _ = estimate_tokens(text.encode("utf-8"))
        if total_tokens > SYSTEM_PROMPT_BUDGET_LIMITS["agent_guide_total"]:
            errors.append(
                f"AGENT_GUIDE.md 全文超标: {total_tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['agent_guide_total']}"
            )
        for title, body in _sections(text):
            if "启动协议" in title:
                sec_tokens, _ = estimate_tokens(body.encode("utf-8"))
                if sec_tokens > SYSTEM_PROMPT_BUDGET_LIMITS["agent_guide_startup"]:
                    errors.append(
                        f"AGENT_GUIDE.md 启动协议章节超标: {sec_tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['agent_guide_startup']}"
                    )

    # 8. 检查 bugx-quick.md 极速卡 (DOC-039)
    quick_path = SCRIPT_DIR / "bugx-quick.md"
    if quick_path.is_file():
        tokens, _ = estimate_tokens(quick_path.read_bytes())
        if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["quick_card"]:
            errors.append(f"bugx-quick.md 极速卡超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['quick_card']}")

    # 9. 检查 bugx.md 全文 (DOC-034 阶段 2 验收判据)
    bugx_path = SCRIPT_DIR / "bugx.md"
    if bugx_path.is_file():
        tokens, _ = estimate_tokens(bugx_path.read_bytes())
        if tokens > SYSTEM_PROMPT_BUDGET_LIMITS["bugx_full"]:
            errors.append(
                f"bugx.md 全文超标: {tokens} > {SYSTEM_PROMPT_BUDGET_LIMITS['bugx_full']}"
            )

    # 10. 检查 L0 占比与 L0 常驻总额 (DOC-034 阶段 3 验收判据)
    res = measure()
    layers = res.get("layers", {})
    l0_total = layers.get("L0_quick_always", 0)
    if l0_total > SYSTEM_PROMPT_BUDGET_LIMITS["L0_router"]:
        errors.append(
            f"L0 常驻总额超标: {l0_total} > {SYSTEM_PROMPT_BUDGET_LIMITS['L0_router']}"
        )
    ratio = layers.get("L0_vs_L1_ratio")
    if ratio is not None and ratio > SYSTEM_PROMPT_BUDGET_LIMITS["l0_ratio_max"]:
        errors.append(
            f"L0 常驻占比超标: {ratio:.4f} > {SYSTEM_PROMPT_BUDGET_LIMITS['l0_ratio_max']:.2f}"
        )

    return len(errors) == 0, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=15, help="显示最重章节数")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true", help="校验系统提示词 2.0 预算门禁")
    parser.add_argument(
        "--modal-density", action="store_true",
        help="输出规范硬性措辞密度报告（默认阈值：每千 token 25）",
    )
    parser.add_argument(
        "--modal-density-threshold", type=float, default=25.0,
        help="规范密度报告阈值（每千 token，必须为非负有限数）",
    )
    args = parser.parse_args(argv)

    if not math.isfinite(args.modal_density_threshold) or args.modal_density_threshold < 0:
        print("[guide_budget] ✗ --modal-density-threshold 必须是非负有限数")
        return 2

    if args.check:
        passed, errors = check_budget_compliance()
        if not passed:
            print("[guide_budget] ✗ 系统提示词 2.0 预算门禁失败：")
            for err in errors:
                print(f"  - {err}")
            return 1
        print("[guide_budget] ✓ 系统提示词 2.0 预算门禁全部通过！")
        return 0

    if args.modal_density:
        report = modal_density_report(args.modal_density_threshold)
        if args.json:
            print(json.dumps({
                "threshold": args.modal_density_threshold,
                "matches": report,
            }, ensure_ascii=False, indent=2))
        else:
            print(
                f"[guide_budget] 规范密度报告（阈值 "
                f"{args.modal_density_threshold:g}/千 token）："
            )
            for item in report:
                print(
                    f"  {item['modal_density']:>5.1f}/千  "
                    f"{item['file']} :: {item['title']}"
                )
            print(f"[guide_budget] 命中 {len(report)} 个章节")
        return 0

    result = measure()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("[guide_budget] 文档层成本：")
    for doc in result["documents"]:
        print(f"  {doc['layer']} {doc['file']:<35} {doc['lines']:>5} 行 "
              f"~{doc['tokens']:>6} tokens")
    layers = result["layers"]
    print(f"[guide_budget] L0 常驻 ~{layers['L0_quick_always']} tokens vs "
          f"L1 全文 ~{layers['L1_full_guide_on_demand']} tokens "
          f"（占比 {layers['L0_vs_L1_ratio']}）")
    print(f"[guide_budget] 最重章节 Top {args.top}：")
    all_sections = [
        (section, doc["file"])
        for doc in result["documents"]
        for section in doc["sections"]
    ]
    for section, file_name in sorted(
        all_sections, key=lambda item: -item[0]["tokens"],
    )[:args.top]:
        print(f"  {section['tokens']:>6} tokens  {file_name} :: {section['title'][:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
