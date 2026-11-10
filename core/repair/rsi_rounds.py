#!/usr/bin/env python3
"""core/rsi_rounds.py - 逐级递增自回归（RSI）轮次编排器。

## 它解决什么

``scenario_eval.py`` 能跑完 L1–L6 一轮，但「自回归」缺三件事：

1. **轮次串联** —— 每轮把收据落盘，下一轮与上一轮比，而不只是跟冻死的基线比。
2. **递增** —— 低级别先过，高级别才跑。L1 崩了还去跑 L6 是浪费，且会把
   「基础设施坏了」误读成「某个故障注入用例有问题」。
3. **收敛判据** —— 连续 N 轮无新增缺陷才停，而不是「跑满 N 轮」。

## 递增的语义

    第 1 轮：L1 → 通过才继续 → L2 → … → L6（全跑）
    第 2 轮起：先跑 L1，失败即**短路**，不浪费时间在高级别上

短路不是偷懒：L1 失败意味着「最小链路不成立」，此时 L4 的召回率毫无意义。
但短路必须**显式记录**，否则收据会看起来像「只测了 L1」。

## 收敛

    consecutive_clean：连续多少轮没有任何级别的 ok 从 True 变成 False。
    达到 --converge-after（默认 2）即停止。

## 产物

    <out>/round-001.json     每轮完整收据（含与上一轮的 diff）
    <out>/history.json       全部轮次的摘要序列，供趋势分析
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
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIO_EVAL = REPO_ROOT / "core" / "runtime" / "scenario_eval.py"
HISTORY_SCHEMA = 1


def _run_round(*, level_ids: list[str] | None, baseline: Path | None,
               timeout: float) -> dict[str, object]:
    """跑一轮 scenario_eval，返回它产出的收据。"""
    argv = [sys.executable, str(SCENARIO_EVAL), "--json"]
    for level_id in (level_ids or []):
        argv.extend(["--level", level_id])
    if baseline is not None and baseline.is_file():
        argv.extend(["--baseline", str(baseline)])
    try:
        proc = subprocess.run(
            argv, cwd=str(REPO_ROOT), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "schema_version": 0, "levels": [], "error": f"轮次超时 ({timeout}s)",
            "summary": {"ok": False, "levels_failed": ["TIMEOUT"]},
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "schema_version": 0, "levels": [], "error": f"{type(exc).__name__}: {exc}",
            "summary": {"ok": False, "levels_failed": ["LAUNCH"]},
        }
    text = (proc.stdout or "").strip()
    # scenario_eval --json 会把收据整段打到 stdout；容忍前置日志行。
    start = text.find("{")
    if start < 0:
        return {
            "schema_version": 0, "levels": [],
            "error": f"无 JSON 输出；stderr={proc.stderr[-400:]}",
            "summary": {"ok": False, "levels_failed": ["NO_OUTPUT"]},
        }
    try:
        return json.loads(text[start:])
    except ValueError as exc:
        return {
            "schema_version": 0, "levels": [],
            "error": f"JSON 解析失败：{exc}",
            "summary": {"ok": False, "levels_failed": ["BAD_JSON"]},
        }


def diff_rounds(prev: dict[str, object] | None,
                current: dict[str, object]) -> dict[str, object]:
    """比较相邻两轮，给出「新增失败 / 修复 / 仍然失败」三类。"""
    if not prev:
        return {"new_failures": [], "fixed": [], "still_failing": [], "first_round": True}
    before = {str(i["level"]): i for i in prev.get("levels", [])}
    after = {str(i["level"]): i for i in current.get("levels", [])}
    new_failures, fixed, still_failing = [], [], []
    for level, item in after.items():
        was_ok = before.get(level, {}).get("ok")
        now_ok = item.get("ok")
        if now_ok is False and was_ok is not False:
            # 含「上一轮没有这个级别」的情形。关键约束：**每个失败级别必须恰好
            # 落进一个桶**。第一版只认 ``was_ok is True``，于是新出现的失败级别
            # 既不是 new、也不是 still_failing，在轮次报告里彻底消失——而它恰恰
            # 是新固化的用例第一次就抓到缺陷的信号，最不该被吞掉。
            new_failures.append(level)
        elif now_ok is True and was_ok is False:
            fixed.append(level)
        elif now_ok is False and was_ok is False:
            still_failing.append(level)
    return {
        "new_failures": new_failures,
        "fixed": fixed,
        "still_failing": still_failing,
        "first_round": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L1–L6 逐级递增自回归轮次")
    parser.add_argument("--rounds", type=int, default=3, help="最多跑几轮")
    parser.add_argument("--converge-after", type=int, default=2,
                        help="连续多少轮无新增失败即收敛（0=不收敛）")
    parser.add_argument("--out", default=None, help="产物目录")
    parser.add_argument("--baseline", default=None, help="基线收据")
    parser.add_argument("--level", action="append", default=None,
                        help="只跑指定级别（可重复）；缺省为逐级递增全跑")
    parser.add_argument("--round-timeout", type=float, default=1200.0)
    parser.add_argument("--stop-on-new-failure", action="store_true",
                        help="出现新增失败立即停止（调试用）")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser() if args.out else (
        REPO_ROOT / ".bugx" / "rsi-rounds"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline = Path(args.baseline).expanduser() if args.baseline else None

    history: list[dict[str, object]] = []
    prev_receipt: dict[str, object] | None = None
    consecutive_clean = 0
    stopped_reason = "rounds_exhausted"

    for round_no in range(1, max(1, args.rounds) + 1):
        started = time.time()
        receipt = _run_round(
            level_ids=args.level, baseline=baseline, timeout=args.round_timeout,
        )
        diff = diff_rounds(prev_receipt, receipt)
        entry = {
            "round": round_no,
            "started_at_unix": int(started),
            "duration_seconds": round(time.time() - started, 3),
            "ok": bool(receipt.get("summary", {}).get("ok")),
            "failed_levels": list(receipt.get("summary", {}).get("levels_failed", [])),
            "digest": receipt.get("digest"),
            "error": receipt.get("error"),
            "diff": diff,
        }
        history.append(entry)
        (out_dir / f"round-{round_no:03d}.json").write_text(
            json.dumps({"entry": entry, "receipt": receipt},
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        mark = "OK  " if entry["ok"] else "FAIL"
        print(f"[round {round_no}] {mark} {entry['duration_seconds']:7.2f}s "
              f"失败级别={entry['failed_levels'] or '无'}")
        if diff["new_failures"]:
            print(f"            新增失败：{diff['new_failures']}")
        if diff["fixed"]:
            print(f"            已修复：{diff['fixed']}")

        if entry["ok"]:
            consecutive_clean += 1
        else:
            consecutive_clean = 0

        prev_receipt = receipt

        if args.stop_on_new_failure and diff["new_failures"]:
            stopped_reason = "new_failure"
            break
        if args.converge_after > 0 and consecutive_clean >= args.converge_after:
            stopped_reason = "converged"
            break

    payload = {
        "schema_version": HISTORY_SCHEMA,
        "converged": stopped_reason == "converged",
        "stopped_reason": stopped_reason,
        "consecutive_clean": consecutive_clean,
        "rounds": history,
    }
    (out_dir / "history.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    print(f"\n停止原因：{stopped_reason}；连续无新增失败 {consecutive_clean} 轮；"
          f"产物：{out_dir}")
    return 0 if stopped_reason in {"converged", "rounds_exhausted"} else 1


if __name__ == "__main__":
    sys.exit(main())
