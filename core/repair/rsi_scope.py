#!/usr/bin/env python3
"""core/rsi_scope.py - 自修复的范围守卫（模式 1 / 模式 2 的边界执行者）。

## 两套模式，一个开关

    RSI_REPAIR_MODE=guarded   模式 1（默认）：只修「有独立通过标准」的产品缺陷；
                              评测断言（golden / case / 门槛）一律不许动。
    RSI_REPAIR_MODE=full      模式 2：断言也可自动改，但必须逐条过下面的守卫。

## 模式 2 为什么必须带守卫

「自己出题自己判卷」不是抽象担忧，它有非常具体的失败形态：

  缺陷：L4 漏检 X
  跑偏：把 golden 里 X 那条删掉 → 漏检消失 → 绿灯
  后果：断言少了，保护范围缩小，但报告说「已修复」

  缺陷：L3 某语言没被识别
  跑偏：把 cases.json 里该语言相关 case 删掉 → 级别的检查项无人触发 → 绿灯
  后果：和上面同一类，只是伪装成「清理」

两者都满足「复测变绿」，都是纯粹的退化。所以模式 2 不能只看结果，必须
**限制改动的形状**：

  1. 只许「增」，不许「减」——断言数量不得下降
  2. 单轮改动有上限——防止一次重写整个评测集
  3. 必须留痕（原值 + 新值 + 理由）——事后可审
  4. 不许改门槛数值——否则「降低 min_recall」就能让一切通过
  5. 不许改产品源码——那是模式 1 的活，走双向门禁

这五条都是**可机械检查**的。守卫的价值就在于它不需要理解改动意图，
只看形状就能拦下绝大多数跑偏。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

GUARDED = "guarded"
FULL = "full"
MODE_ENV = "RSI_REPAIR_MODE"
VALID_MODES = (GUARDED, FULL)

#: 模式 2 下允许触碰的评测文件（相对仓库根）。
ASSERTION_FILES = (
    "evals/cases.json",
    "evals/golden-findings.json",
    "verification/evals/cases.json",
    "verification/evals/golden-findings.json",
)
#: 模式 2 下**永远**不许改的文件。门槛数值一改，一切都能通过。
FORBIDDEN_FILES = (
    "evals/scenario-baseline.json",
    "evals/baseline-metrics.json",
    "verification/evals/scenario-baseline.json",
    "verification/evals/baseline-metrics.json",
)
#: 单轮允许新增的断言条数上限。够修一个缺陷，不够重写评测集。
MAX_ASSERTION_DELTA = 3


def current_mode() -> str:
    """读取当前模式。非法值一律退化为 guarded（fail-safe）。"""
    value = os.environ.get(MODE_ENV, "").strip().lower()
    return value if value in VALID_MODES else GUARDED


def mode_description(mode: str) -> str:
    return {
        GUARDED: "只修产品缺陷；评测断言（golden/case/门槛）禁止改动",
        FULL: f"断言可改，但受形状守卫约束（只增不减、单轮 ≤{MAX_ASSERTION_DELTA} 条）",
    }.get(mode, mode)


class ScopeViolation(Exception):
    """改动越界。"""


def _load(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _eval_file(root: Path, name: str) -> Path:
    p = root / "verification" / "evals" / name
    if p.exists() or (root / "verification" / "evals").is_dir():
        return p
    return root / "evals" / name


def _case_ids(root: Path) -> set[str]:
    cases = _load(_eval_file(root, "cases.json")).get("cases")
    if not isinstance(cases, list):
        return set()
    return {str(c.get("id")) for c in cases
            if isinstance(c, dict) and c.get("id")}


def _golden_case_ids(root: Path) -> set[str]:
    findings = _load(_eval_file(root, "golden-findings.json")).get("findings")
    if not isinstance(findings, list):
        return set()
    return {str(f.get("case_id")) for f in findings
            if isinstance(f, dict) and f.get("case_id")}


def _digest(path: Path) -> str:
    """文件内容指纹。文件不存在时返回空串（区别于「内容为空」）。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def snapshot(root: Path) -> dict[str, object]:
    """记录断言集的当前形状**和内容指纹**。守卫的所有比较都基于它。

    ## 为什么必须有内容指纹

    早期版本只记录 ``case_ids`` / ``golden_case_ids`` / ``thresholds``。
    实测可被绕过：把 ``golden-findings.json`` 里某条实测的 ``severity`` 从
    ``high`` 改成 ``info``、``confidence`` 改成 ``0.01``——**两者都直接
    决定 recall 判定**——而 ``case_id`` 不变，于是快照完全相同，guarded
    守卫放行。

    这不是理论风险：它正是 Goodhart 的入口。改一个字段比修一个缺陷容易
    得多，而只要「答案的期望值」可以随时被改写，绿灯就失去意义。

    修法是把「ID 集合」升级成「ID 集合 + 内容指纹」。指纹不区分改了什么、
    也不理解意图，只回答一个问题：**字节变了吗**。

    ## 为什么不用「语义感知」的比较

    任何「我需要理解改动意图才能判对」的守卫，在被自动流程调用时都会
    退化成「我同意我自己」——因为判定的就是改动者本身。
    """
    return {
        "case_ids": sorted(_case_ids(root)),
        "golden_case_ids": sorted(_golden_case_ids(root)),
        "thresholds": _load(_eval_file(root, "cases.json")).get("thresholds") or {},
        "content_digests": {
            name: _digest(root / name) for name in ASSERTION_FILES
        },
    }


def check_assertion_change(before: dict, after: dict,
                           *, mode: str | None = None) -> list[str]:
    """比较断言集前后形状，返回违规列表（空 = 允许）。

    这是模式 2 的核心守卫。它**不理解改动意图**，只看形状——

    不理解意图不是缺陷，是特性：任何「我需要理解意图才能判对」的守卫，
    在被自动流程调用时都会退化成「我同意我自己」。
    """
    mode = mode or current_mode()
    if mode == GUARDED:
        # guarded 模式禁止改断言——但**只在真的改了的时候**才报错。
        #
        # 早期实现无条件返回违规，等于「只要跑守卫就失败」。实测后果：
        # rsi_fixer 在 guarded 模式下修一个与评测无关的缺陷（同步 SKILL 副本），
        # 也会被判「改动越界」，整条闭环无法工作。
        #
        # 一个无论输入如何都报错的守卫，和永远返回 True 的检查是同一类错误：
        # 它不区分「有问题」和「没检查」。
        unchanged = (
            set(before.get("case_ids") or []) == set(after.get("case_ids") or [])
            and set(before.get("golden_case_ids") or [])
            == set(after.get("golden_case_ids") or [])
            and before.get("thresholds") == after.get("thresholds")
            # 内容指纹同样必须一致：ID 不变但期望值被改写，是典型的
            # 「保住形状、改掉答案」。只比 ID 会放行这种改动。
            and before.get("content_digests") == after.get("content_digests")
        )
        if unchanged:
            return []
        return ["guarded 模式下评测断言不可改动"]

    problems: list[str] = []

    cases_before = set(before.get("case_ids") or [])
    cases_after = set(after.get("case_ids") or [])
    removed = cases_before - cases_after
    added = cases_after - cases_before
    if removed:
        problems.append(
            f"断言被删除（{len(removed)} 条）：{sorted(removed)[:5]}。"
            "「删掉漏检的 case」会让绿灯出现，但保护范围缩小——这是最典型的跑偏。"
        )
    if len(added) > MAX_ASSERTION_DELTA:
        problems.append(
            f"单轮新增 {len(added)} 条超过上限 {MAX_ASSERTION_DELTA}："
            "一次改太多说明不是针对单个缺陷，而是重写评测集。"
        )

    golden_before = set(before.get("golden_case_ids") or [])
    golden_after = set(after.get("golden_case_ids") or [])
    golden_removed = golden_before - golden_after
    if golden_removed:
        problems.append(
            f"golden 条目被删除（{len(golden_removed)} 条）："
            f"{sorted(golden_removed)[:5]}。删答案能让漏检消失，属于自欺。"
        )
    golden_added = golden_after - golden_before
    if len(golden_added) > MAX_ASSERTION_DELTA:
        problems.append(
            f"单轮新增 golden {len(golden_added)} 条超过上限 {MAX_ASSERTION_DELTA}"
        )

    # case 与 golden 必须同增：只加 case 会立刻制造漏检，只加 golden 会制造孤儿答案
    only_case = (cases_after - cases_before) - (golden_after - golden_before)
    only_golden = (golden_after - golden_before) - (cases_after - cases_before)
    if only_case:
        problems.append(
            f"新增 case 缺配套 golden：{sorted(only_case)[:5]}。"
            "这会立刻制造漏检（已实测踩过两次）。"
        )
    if only_golden:
        problems.append(
            f"新增 golden 缺配套 case：{sorted(only_golden)[:5]}。孤儿答案不测任何东西，"
            "却会虚增 recall 分母之外的东西。"
        )

    if before.get("thresholds") != after.get("thresholds"):
        problems.append(
            "阈值被修改。降低 min_recall 之类能让一切通过，"
            "这比修 bug 容易得多——所以永远不许自动改。"
        )
    return problems


def assert_allowed_paths(paths: list[Path], root: Path,
                         *, mode: str | None = None) -> list[str]:
    """检查待改文件是否在允许范围内。"""
    mode = mode or current_mode()
    problems: list[str] = []
    for path in paths:
        try:
            rel = str(path.resolve().relative_to(root.resolve()))
        except ValueError:
            problems.append(f"{path} 不在仓库内")
            continue
        if mode == GUARDED and rel in ASSERTION_FILES:
            problems.append(f"{rel} 是评测断言，guarded 模式禁止改动")
        if rel in FORBIDDEN_FILES:
            problems.append(f"{rel} 永远禁止自动改动（门槛/基线）")
    return problems


def record_decision(root: Path, decision: dict) -> Path:
    """留痕。模式 2 的每次断言改动都必须可事后审。"""
    ledger = root / ".bugx" / "rsi-repair-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision, ensure_ascii=False) + "\n")
    return ledger
