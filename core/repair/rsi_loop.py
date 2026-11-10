#!/usr/bin/env python3
"""core/rsi_loop.py - 挖掘 → 固化 → 回归 的闭环入口。

## 三个组件的关系

    scenario_eval.py  跑 L1–L6，回答「现在有没有退化」
    fixture_solidify  把真实发现变成新 case，回答「这轮学的有没有记进去」
    rsi_rounds.py     串多轮并判收敛，回答「还要不要继续」

本模块把三者接起来，形成一条命令：

    rsi_loop.py --findings <本轮发现.json> --rounds 3

每轮做四件事：

    1. 固化本轮发现（已确认 + 可复现的才固化）
    2. 跑 L1–L6
    3. 与基线比回退
    4. 记录本轮新增用例数、通过级别、耗时

## 为什么固化要放在评测**之前**

固化会改变 cases，而 cases 直接决定 L4 的 recall 分母。放在评测之后，本轮
的评测结果就对应的是「上一轮的用例集」，报告里的「已固化 N 条」与「本轮
评测」说的不是同一件事——数字对不上，人会去查一个不存在的问题。

## 不进化的失败

固化失败（findings 格式坏、路径逃逸…）不得让整轮失败。发现已经拿到了，
记不下来是「少一层保护」，不是「本轮无效」。所以固化错误只记录不阻断。
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
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "core"))

from core.probes import probe_registry
from core.retrieval import symbol_index
from core.repair import rsi_fixer, rsi_repair, rsi_rounds, rsi_scope
from core.runtime import fixture_solidify

LOOP_SCHEMA = 1


def solidify_round(findings_path: Path | None, *,
                   source_root: Path | None = None) -> dict[str, object]:
    """固化一轮的发现。任何失败都记录为错误，不抛出。"""
    if findings_path is None:
        return {"skipped": "未提供 --findings"}
    if not findings_path.is_file():
        return {"error": f"findings 不存在：{findings_path}"}
    try:
        payload = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"findings 无法解析：{exc}"}
    try:
        # 只传 repo_root，三份产物（cases / fixtures / golden）由它派生。
        # 曾经这里显式传 golden_path=GOLDEN_PATH，而 CASES_PATH 走默认——
        # 两者一旦指向不同根（例如测试注入临时仓库），就会「case 写这头、
        # golden 写那头」，L4 立刻 recall 85.71%。让一个根统管三份产物，
        # 这类分家在结构上不可能发生。
        report = fixture_solidify.solidify(
            payload, source_root=source_root or REPO_ROOT,
            repo_root=REPO_ROOT,
        )
    except (fixture_solidify.SolidifyError, OSError) as exc:
        # OSError（cases.json 缺失等 I/O 问题）同样只记录不阻断：
        # 「不进化的失败」契约要求固化失败不得让整轮崩掉。
        return {"error": f"固化失败：{exc}"}
    return {
        "added": [item["id"] for item in report.get("added", [])],
        "skipped": report.get("skipped", []),
        "rejected": report.get("rejected", []),
        "cases_before": report.get("cases_before"),
        "cases_after": report.get("cases_after"),
    }


def repair_then_recheck(receipt: dict[str, object]) -> dict[str, object]:
    """分流失败 → 自动修语义安全的部分 → 重测。

    ## 为什么只修 noise

    见 ``rsi_repair`` 模块头：量具不能改被测物。补 golden（增加断言）、删
    case（减少断言）都改写测量含义，自动做等于自己出题自己判卷。所以这里
    只清残留/重复，然后重测——重测通过就是真通过，因为断言集没变。
    """
    failed = list(receipt.get("summary", {}).get("levels_failed", []))
    if not failed:
        return {"attempted": False}
    levels = {str(item.get("level")): item for item in receipt.get("levels", [])}
    diagnoses: list[rsi_repair.Diagnosis] = []
    for level_id in failed:
        item = levels.get(level_id) or {}
        diagnoses.extend(
            rsi_repair.classify(level_id, list(item.get("checks") or []), REPO_ROOT),
        )

    mode = rsi_scope.current_mode()
    auto = [d for d in diagnoses if d.auto]
    escalated = [d for d in diagnoses if not d.auto]
    result: dict[str, object] = {
        "attempted": True, "mode": mode,
        "auto_repairable": [d.as_dict() for d in auto],
        "escalated": [d.as_dict() for d in escalated],
        "rechecked": False,
    }

    # 模式 2：product 类交给 rsi_fixer 在隔离副本里改 + 双向门禁。
    # 它从不写原仓库——产出的是候选树，落地仍须人或有审查的流程决定。
    product = [d for d in auto if d.kind == rsi_repair.PRODUCT]
    if product:
        order = rsi_repair.build_work_order(0, product, receipt)
        attempt = rsi_fixer.attempt_fix(order, mode=mode)
        result["fixer"] = attempt
        if not attempt.get("verified"):
            # 修不成不算失败之外的事：它只是「这轮没能自动修」，继续按原流程
            # 出工作单给人。绝不能因为「试过了」就当作已处理。
            result["fixer_failed"] = attempt.get("error") or "未通过双向门禁"
    if not auto:
        return result

    fix = rsi_repair.repair_noise(REPO_ROOT)
    result["applied"] = fix
    if not fix.get("changed"):
        return result

    # 修完必须重测：不重测就等于「相信自己的修复」，而修复本身可能引入问题。
    recheck = rsi_rounds._run_round(
        level_ids=failed, baseline=None, timeout=1800.0,
    )
    result["rechecked"] = True
    result["receipt"] = recheck
    result["now_ok"] = bool(recheck.get("summary", {}).get("ok"))
    return result


def run_loop(*, findings_paths: list[Path | None], rounds: int,
             converge_after: int, baseline: Path | None,
             out_dir: Path, source_root: Path | None) -> dict[str, object]:
    """执行闭环。``findings_paths`` 每轮取一个，用尽后复用最后一个。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, object]] = []
    prev_receipt: dict[str, object] | None = None
    consecutive_clean = 0
    stopped_reason = "rounds_exhausted"

    for round_no in range(1, max(1, rounds) + 1):
        started = time.time()
        findings = (
            findings_paths[min(round_no - 1, len(findings_paths) - 1)]
            if findings_paths else None
        )
        # 先固化，再评测：否则本轮评测的用例集与报告里的「已固化」不是一回事。
        solidify = solidify_round(findings, source_root=source_root)

        receipt = rsi_rounds._run_round(
            level_ids=None, baseline=baseline, timeout=1800.0,
        )
        # 失败立即分流：可自动修的（语义不变）当场修完重测；改断言的、要改
        # 产品源码的一律升级成工作单。不做「只记录不动作」——那等于把问题
        # 攒到没人再看的日志里。
        repair = {"attempted": False}
        if not receipt.get("summary", {}).get("ok"):
            repair = repair_then_recheck(receipt)
            if repair.get("rechecked"):
                receipt = repair["receipt"]
            # 仍有失败就出工作单：带复现命令 + 证明要求，可直接派给下一个人。
            if not receipt.get("summary", {}).get("ok"):
                levels_now = {
                    str(item.get("level")): item for item in receipt.get("levels", [])
                }
                still: list[rsi_repair.Diagnosis] = []
                for level_id in receipt.get("summary", {}).get("levels_failed", []):
                    item = levels_now.get(str(level_id)) or {}
                    still.extend(rsi_repair.classify(
                        str(level_id), list(item.get("checks") or []), REPO_ROOT,
                    ))
                repair["work_order"] = rsi_repair.build_work_order(
                    round_no, still, receipt,
                )
        diff = rsi_rounds.diff_rounds(prev_receipt, receipt)
        entry = {
            "round": round_no,
            "started_at_unix": int(started),
            "duration_seconds": round(time.time() - started, 3),
            "ok": bool(receipt.get("summary", {}).get("ok")),
            "failed_levels": list(
                receipt.get("summary", {}).get("levels_failed", []),
            ),
            "digest": receipt.get("digest"),
            "solidify": solidify,
            "repair": repair,
            "diff": diff,
        }
        history.append(entry)
        (out_dir / f"loop-{round_no:03d}.json").write_text(
            json.dumps({"entry": entry, "receipt": receipt},
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        mark = "OK  " if entry["ok"] else "FAIL"
        added = solidify.get("added") or []
        print(f"[round {round_no}] {mark} {entry['duration_seconds']:7.2f}s "
              f"固化 {len(added)} 条 失败级别={entry['failed_levels'] or '无'}")
        for case_id in added:
            print(f"            + {case_id}")
        if solidify.get("error"):
            print(f"            ! 固化错误：{solidify['error']}")
        if diff["new_failures"]:
            print(f"            新增失败：{diff['new_failures']}")
        if diff["fixed"]:
            print(f"            已修复：{diff['fixed']}")

        consecutive_clean = consecutive_clean + 1 if entry["ok"] else 0
        prev_receipt = receipt

        # 收敛条件：连续干净 **且** 本轮没有新增用例。新增用例意味着知识还在
        # 增长，此时停下来会漏掉它带来的新失败。
        if (converge_after > 0 and consecutive_clean >= converge_after
                and not added):
            stopped_reason = "converged"
            break

    payload = {
        "schema_version": LOOP_SCHEMA,
        "converged": stopped_reason == "converged",
        "stopped_reason": stopped_reason,
        "consecutive_clean": consecutive_clean,
        "rounds": history,
    }
    (out_dir / "loop-history.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="挖掘 → 固化 → 回归 闭环",
    )
    parser.add_argument("--findings", action="append", default=None,
                        help="本轮 findings JSON（可重复，按轮次取）")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--converge-after", type=int, default=2)
    parser.add_argument("--baseline", default=str(
        (REPO_ROOT / "verification" / "evals" / "scenario-baseline.json")
        if (REPO_ROOT / "verification" / "evals").is_dir()
        else (REPO_ROOT / "evals" / "scenario-baseline.json")))
    parser.add_argument("--out", default=None)
    parser.add_argument("--source-root", default=None,
                        help="发现证据的解析根（缺省=仓库根）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser() if args.out else (
        REPO_ROOT / ".bugx" / "rsi-loop"
    )
    findings_paths = [Path(p).expanduser() for p in (args.findings or [])]
    payload = run_loop(
        findings_paths=findings_paths, rounds=args.rounds,
        converge_after=args.converge_after,
        baseline=Path(args.baseline).expanduser() if args.baseline else None,
        out_dir=out_dir,
        source_root=Path(args.source_root).expanduser() if args.source_root else None,
    )
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    work_orders = [
        item["repair"]["work_order"] for item in payload["rounds"]
        if isinstance(item.get("repair"), dict)
        and isinstance(item["repair"].get("work_order"), dict)
    ]
    if work_orders:
        latest = work_orders[-1]
        wo_path = out_dir / "work-order.json"
        wo_path.write_text(
            json.dumps(latest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        print(f"\n待处理工作单：{wo_path}")
        for diag in latest.get("diagnoses", []):
            print(f"  [{diag['kind']}] {diag['level']} {diag['check']}：{diag['reason']}")
            if diag.get("action"):
                print(f"       → {diag['action']}")

    print(f"\n停止原因：{payload['stopped_reason']}；产物：{out_dir}")
    # 退出码语义：
    #   0  最后一轮通过（含收敛）
    #   1  最后一轮仍失败——「跑满轮次」不是成功
    last_ok = bool(payload["rounds"][-1]["ok"]) if payload["rounds"] else False
    return 0 if last_ok else 1


if __name__ == "__main__":
    sys.exit(main())
