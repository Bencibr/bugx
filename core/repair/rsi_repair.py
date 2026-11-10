#!/usr/bin/env python3
"""core/rsi_repair.py - 把「发现退化」分流成可自动修复 / 需决策 / 需改源码。

## 为什么不能一律自动修（这条边界是本模块存在的全部理由）

``rsi_loop`` 在链路里是**量具**。如果量具能顺手改被测物，绿灯的含义就从
「产品健康」变成「产品与量具达成了一致」——这是 Goodhart 定律的教科书形态：
指标一旦同时是目标，就不再是指标。

所以不是「能修就修」，而是**修它有权修的那部分**。判据只有一条：

    修复是否改变「这次测量断言了什么」？

* 不改变 → 可以自动修。清残留、删重复、删孤儿文件。修完重测，通过就是真通过。
* 改变 → 必须升级决策。补一条 golden 是**增加断言**，删一个 case 是**减少
  断言**，两者都改写了测量含义，自动做等于自己给自己出题再自己判卷。
* 需改产品源码 → 单独立案。改完必须由独立证据（双向差分）证明，
  而不是「量具复测绿了」。

## 三层

    NOISE     语义不变，自动修 + 重测确认
    SEMANTIC  改断言，必须决策（工作单带两个互斥选项）
    PRODUCT   改源码，工作单附复现命令 + 必须的差分证明要求
    ENVIRONMENT  缺工具/权限，工作单附修复指引
    UNKNOWN   不认识就不猜，一律升级
"""

from __future__ import annotations

import json
import re
from pathlib import Path

NOISE = "noise"
SEMANTIC = "semantic"
PRODUCT = "product"
ENVIRONMENT = "environment"
UNKNOWN = "unknown"

#: 自动修复只允许动这些类别。``SEMANTIC`` 及以上一律升级——
#: 把它们放进白名单，等于让量具自己决定考什么。
#: 模式 1（guarded）下只允许清 noise；模式 2（full）下 product 也可尝试，
#: 但必须走 ``rsi_fixer`` 的隔离 + 双向门禁，且受 ``rsi_scope`` 的形状守卫。
AUTO_REPAIRABLE = frozenset({NOISE})
FULL_AUTO_REPAIRABLE = frozenset({NOISE, PRODUCT})


class Diagnosis:
    """一条失败分流结论。"""

    __slots__ = ("level", "check", "kind", "reason", "action", "auto")

    def __init__(self, level: str, check: str, kind: str, reason: str,
                 action: str = "", auto: bool = False) -> None:
        self.level = level
        self.check = check
        self.kind = kind
        self.reason = reason
        self.action = action
        self.auto = auto

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level, "check": self.check, "kind": self.kind,
            "reason": self.reason, "action": self.action, "auto_repair": self.auto,
        }


def _l4_missing_cases(checks: list[dict]) -> list[str]:
    """从 L4 检查详情里抽出漏检的 case id。"""
    missing: list[str] = []
    for check in checks:
        if check.get("ok"):
            continue
        detail = str(check.get("detail") or "")
        missing.extend(re.findall(r"漏检:\s*([A-Z0-9\-_]+)", detail))
    return missing


def _case_golden_desync(root: Path) -> list[str]:
    """找出「有 case 没 golden」的 case id。

    这类不一致正是「只写 case 不写 golden」留下的痕迹，已经实测踩过两次。
    """
    cases_path = root / "evals" / "cases.json"
    golden_path = root / "evals" / "golden-findings.json"
    if not cases_path.is_file() or not golden_path.is_file():
        return []
    try:
        cases = json.loads(cases_path.read_text(encoding="utf-8")).get("cases") or []
        golden = json.loads(golden_path.read_text(encoding="utf-8")).get("findings") or []
    except (OSError, ValueError):
        return []
    covered = {
        str(item.get("case_id")) for item in golden
        if isinstance(item, dict) and item.get("case_id")
    }
    return [
        str(case.get("id")) for case in cases
        if isinstance(case, dict) and case.get("id")
        and str(case.get("id")) not in covered
    ]


def _orphan_fixtures(root: Path) -> list[str]:
    """找出没被任何 case 引用的 fixture 文件（语义不变的残留）。"""
    cases_path = root / "evals" / "cases.json"
    fixtures_dir = root / "evals" / "fixtures"
    if not cases_path.is_file() or not fixtures_dir.is_dir():
        return []
    try:
        cases = json.loads(cases_path.read_text(encoding="utf-8")).get("cases") or []
    except (OSError, ValueError):
        return []
    referenced = {
        Path(str(case.get("fixture"))).name
        for case in cases
        if isinstance(case, dict) and case.get("fixture")
    }
    return sorted(
        path.name for path in fixtures_dir.iterdir()
        if path.is_file() and path.name not in referenced
    )


def _duplicate_case_ids(root: Path) -> list[str]:
    cases_path = root / "evals" / "cases.json"
    if not cases_path.is_file():
        return []
    try:
        cases = json.loads(cases_path.read_text(encoding="utf-8")).get("cases") or []
    except (OSError, ValueError):
        return []
    seen: set[str] = set()
    dups: list[str] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "")
        if not case_id:
            continue
        if case_id in seen:
            dups.append(case_id)
        seen.add(case_id)
    return dups


def classify(level_id: str, checks: list[dict], root: Path,
             *, mode: str = "guarded") -> list[Diagnosis]:
    """把一个失败级别分流成若干诊断。

    ``mode`` 决定 ``product`` 类是否可交给自动修复走一遍。注意：能自动修
    不等于会自动改好——它只是「允许尝试」，真正的落地仍受隔离 + 双向门禁 +
    形状守卫三重约束。
    """
    auto_kinds = (FULL_AUTO_REPAIRABLE if mode == "full"
                  else AUTO_REPAIRABLE)
    diagnoses: list[Diagnosis] = []
    failed_checks = [c for c in checks if not c.get("ok")]

    if level_id == "L4":
        missing = _l4_missing_cases(failed_checks)
        desynced = set(_case_golden_desync(root))
        for case_id in missing:
            if case_id in desynced:
                # 有 case 没 golden：不能自动补。补 golden 是**增加断言**，
                # 而该 fixture 是否真含这个根因，量具无权单方面断定。
                diagnoses.append(Diagnosis(
                    level_id, f"漏检 {case_id}", SEMANTIC,
                    "case 已登记但 golden 无对应条目（固化只做了一半）",
                    "二选一：补齐 golden（增加断言），或撤下该 case（减少断言）。"
                    "两者都改变测量含义，必须由人或有审查的流程决定。",
                ))
            else:
                diagnoses.append(Diagnosis(
                    level_id, f"漏检 {case_id}", PRODUCT,
                    "golden 存在但产品未能发现该缺陷",
                    "改产品发现能力；改完须给出双向差分证据，"
                    "不得仅以本评测复测变绿作为证明。",
                ))
        if not missing:
            for check in failed_checks:
                diagnoses.append(Diagnosis(
                    level_id, str(check.get("check")), PRODUCT,
                    "评测门槛未达标",
                    str(check.get("detail") or "")[:200],
                ))

    elif level_id == "L5":
        for check in failed_checks:
            detail = str(check.get("detail") or "")
            if "Permission" in detail or "权限" in detail:
                diagnoses.append(Diagnosis(
                    level_id, str(check.get("check")), ENVIRONMENT,
                    "权限不足导致探测无法进行", "修正目录权限后重跑",
                ))
            else:
                diagnoses.append(Diagnosis(
                    level_id, str(check.get("check")), PRODUCT,
                    "畸形未被 fail-closed 拒绝（静默降级）",
                    "修产品的校验逻辑；这是最危险的一类——用户会以为审计过了。",
                ))

    elif level_id in {"L1", "L2", "L3", "L6"}:
        for check in failed_checks:
            diagnoses.append(Diagnosis(
                level_id, str(check.get("check")), PRODUCT,
                "产品行为不符合预期",
                str(check.get("detail") or "")[:200],
            ))
    else:
        for check in failed_checks:
            diagnoses.append(Diagnosis(
                level_id, str(check.get("check")), UNKNOWN,
                "未知级别或未知失败形态", "不猜测，交人工判断",
            ))

    # 噪声类诊断与级别无关：残留与重复不会改变任何断言，随时可清。
    for name in _duplicate_case_ids(root):
        diagnoses.append(Diagnosis(
            level_id, f"重复 case {name}", NOISE,
            "cases.json 存在重复 id", "删除后出现的重复项", auto=True,
        ))

    # 统一按模式收紧 auto 标记。放在最后做，而不是在每个 append 里各写一遍：
    # 分散写时只要漏一处，就会有一条 product 诊断在 guarded 模式下被当成可自动修。
    for diagnosis in diagnoses:
        if diagnosis.kind in auto_kinds and diagnosis.kind != NOISE:
            diagnosis.auto = True
            diagnosis.action = (diagnosis.action or "") + "（交给 rsi_fixer 隔离修复）"
    return diagnoses


def repair_noise(root: Path, *, dry_run: bool = False) -> dict[str, object]:
    """执行语义安全的自动修复。

    只做三件事，全部可逆或无害：

    1. 删除未被任何 case 引用的 fixture 文件
    2. 删除重复的 case 条目（保留第一条）
    3. 清掉上一轮的临时轮次产物

    每一件都不会改变「这次测量断言了什么」。所以修完重测，通过就是真通过。
    """
    removed_fixtures: list[str] = []
    removed_duplicates: list[str] = []
    removed_artifacts: list[str] = []

    for name in _orphan_fixtures(root):
        if not dry_run:
            try:
                (root / "evals" / "fixtures" / name).unlink()
            except OSError:
                continue
        removed_fixtures.append(name)

    for case_id in _duplicate_case_ids(root):
        if dry_run:
            removed_duplicates.append(case_id)
            continue
        cases_path = root / "evals" / "cases.json"
        try:
            payload = json.loads(cases_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            break
        cases = payload.get("cases")
        if not isinstance(cases, list):
            break
        seen: set[str] = set()
        kept: list = []
        for case in cases:
            case_id_value = (
                str(case.get("id")) if isinstance(case, dict) else ""
            )
            if case_id_value and case_id_value in seen:
                continue
            if case_id_value:
                seen.add(case_id_value)
            kept.append(case)
        payload["cases"] = kept
        cases_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        removed_duplicates.append(case_id)

    for pattern in ("round-*.json", "loop-*.json"):
        for path in (root / ".bugx" / "rsi-rounds").glob(pattern):
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    continue
            removed_artifacts.append(path.name)
        for path in (root / ".bugx" / "rsi-loop").glob(pattern):
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    continue
            removed_artifacts.append(path.name)

    return {
        "removed_fixtures": removed_fixtures,
        "removed_duplicates": removed_duplicates,
        "removed_artifacts": removed_artifacts,
        "dry_run": dry_run,
        "changed": bool(
            removed_fixtures or removed_duplicates or removed_artifacts
        ),
    }


def build_work_order(round_no: int, diagnoses: list[Diagnosis],
                     receipt: dict[str, object]) -> dict[str, object]:
    """把不可自动修的诊断整理成可执行的工作单。

    工作单必须自带**复现命令**。没有复现命令的工单等于「你去看看」，
    接手的人还得先花时间重建现场——多数工单死在这一步。
    """
    escalated = [d for d in diagnoses if not d.auto
                 and d.kind != NOISE]
    repro = []
    for item in escalated:
        repro.append({
            "level": item.level,
            "command": f"python3 -m core.runtime.scenario_eval --level {item.level} --json",
        })
    unique_repro: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in repro:
        if item["level"] in seen:
            continue
        seen.add(item["level"])
        unique_repro.append(item)
    return {
        "schema_version": 1,
        "round": round_no,
        "levels_failed": sorted({d.level for d in escalated}),
        "diagnoses": [d.as_dict() for d in escalated],
        "reproduce": unique_repro,
        "verification_required": (
            "修完必须提供双向差分证据：同一输入在修复前失败、修复后通过，"
            "且断言差异可解释。仅以本评测复测变绿**不构成证明**——"
            "量具与被测物同源时，绿只说明两者一致。"
        ),
        "receipt_digest": receipt.get("digest"),
    }
