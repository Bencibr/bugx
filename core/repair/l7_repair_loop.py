#!/usr/bin/env python3
"""core/l7_repair_loop.py - L7 失败 → 立案 → 派宿主修 → 双向门禁 → 复检。

## 闭环补的是哪一段

在此之前，两半各自都能工作，但没接上：

    l7_selfcheck  能发现 BugX 自身坏了，但只打印红叉
    rsi_fixer     能在隔离副本里派 dsh 修、过双向门禁，但要人工递工单

本模块把它们串成一条：

    L7 跑出失败
      → 生成可执行工单（含复现命令、验收标准、范围限制）
      → 派 dsh 在**隔离副本**里改
      → 双向门禁：L7 复检通过 + 既有测试不退化
      → 通过才交出候选树，原仓库全程不动

## 一个刻意的设计：修复从不自动落地

``attempt_fix`` 只在临时副本里改，产出的是 ``candidate_tree``。本模块**不会**
把它写回原仓库——只把候选树路径和改动清单交给调用方。

理由与 ``rsi_scope`` 同源：门禁证明的是「这个改动让 L7 绿了且没弄坏别处」，
但它**不能证明改动是对的**。让量具自动改写被测物，绿灯就从「产品健康」变成
「产品与量具达成了一致」。所以落地这一步永远留给人或有审查的流程。

``--apply`` 参数存在，但它只是把候选树里的改动复制回原仓库，并在复制后**立刻
重跑一次 L7 和回归**——如果复检失败，改动会被回滚。它不是「自动合并」，
而是「带保险的落地」。

## 为什么工单里必须带回归命令

L7 的修复通常只涉及一个文件。跑全量 pytest（数分钟）没有必要，而且会大幅
降低闭环可用性。门禁要拦的是「改坏别处」，不是「必须跑全量」——所以工单
指定最小有意义回归集：

    python3 -m core.repair.l7_selfcheck     ← 本缺陷的判定器
    python3 -m core.runtime.scenario_eval --level L7   ← 正式级别的口径

两者都便宜（0.00s / 0.1s），可以随每轮跑。
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

import json
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "core"))

from core.repair import l7_selfcheck  # noqa: E402
from core.repair import rsi_fixer  # noqa: E402
from core.repair import rsi_scope  # noqa: E402


def build_work_order(payload: dict, *, root: Path,
                     dynamic: bool = False) -> dict[str, object]:
    """把 L7 自检结果转成 rsi_fixer 能吃的工单。

    工单必须自带三件事，缺一不可执行：

    * **复现命令** —— 让 agent 自己确认缺陷存在，而不是听我们转述
    * **验收标准** —— 明确「L7 复检通过 + 回归不退化」
    * **范围限制** —— 说清哪些文件不许动
    """
    failed = [c for c in payload.get("checks", []) if not c.get("ok")]
    diagnoses = []
    for check in failed:
        check_id = str(check.get("id") or "")
        target = l7_selfcheck.CHECK_TARGETS.get(check_id, {})
        # ## 落点必须随诊断一起下发
        #
        # 实测：只给一句自然语言描述，agent 会去 grep 全仓找线索，分钟级预算
        # 秒秒烧光；补上「哪个文件、哪个符号、改法长什么样」之后同一处缺陷
        # 几秒就修完。差的不是模型能力，是**它不知道去哪**。
        #
        # notes 也要带上：检查函数在那里写了「参照样例」（例如
        # 「ChatGptAdapter 走硬编码通路 _chatgpt_marketplace（已登记）」），
        # 那句话等于把答案的形态直接摆出来了。
        diagnoses.append({
            "kind": "product",
            "level": "L7",
            "check": f"{check_id} {check.get('name')}",
            "reason": str(check.get("detail") or "")[:600],
            "files": list(target.get("files") or []),
            "symbols": list(target.get("symbols") or []),
            "hint": str(target.get("hint") or ""),
            "notes": [str(n)[:400] for n in (check.get("notes") or [])][:8],
        })
    return {
        "schema_version": 1,
        "source": "l7_selfcheck",
        "diagnoses": diagnoses,
        "reproduce": [
            {"level": "L7",
             "command": "python3 -m core.repair.l7_selfcheck"
                        + (" --dynamic" if dynamic else "")},
        ],
        # 回归只跑 L7 自身（0.27s）而不是全量单测（24 分钟）。
        #
        # 实测教训：最初把 `pytest tests/ -q` 写进回归命令，单轮闭环因此
        # 永远跑不完——3600+ 条测试要 24 分钟，而修复预算只有分钟级。
        # 更糟的是它同时出现在**给 agent 的提示词**里，agent 会真的去跑，
        # 于是超时、零输出、报告「未修复」，而缺陷一行就能改好。
        #
        # L7 静态检查 0.27s、含动态 13s，覆盖「引擎自己的结构有没有坏」，
        # 正是本闭环的验收面。全量回归属于提交前的人工动作，不是每轮
        # 自动闭环的一部分——**跑不动的门禁等于没有门禁**。
        # 命令必须用 ``-m`` 点号形式：两个脚本都已归档（``core/runtime/``、
        # ``core/repair/``），且各自以点号导入兄弟包——直接跑脚本会在
        # ``__package__`` 为空时 ImportError，门禁于是**恒失败**，
        # 把每一次本来成功的修复都判成 `unfixed`（"跑不动的门禁等于没有门禁"）。
        "regression_commands": [
            ["python3", "-m", "core.runtime.scenario_eval",
             "--level", "L7", "--json"],
            ["python3", "-m", "core.repair.l7_selfcheck"],
        ],
        "verification_required": (
            "修复必须同时满足："
            "1) `python3 -m core.repair.l7_selfcheck"
            + (" --dynamic" if dynamic else "") + "` 退出码 0；"
            "2) `python3 -m core.runtime.scenario_eval --level L7` 通过；"
            "3) 不修改 evals/ 下的评测断言与基线。"
            "仅让其中一条变绿不构成修复——L7 的六项检查互相独立，"
            "让目标项变绿而弄红其他项同样算失败。"
        ),
        "scope_hint": {
            "allowed": [
                "core/l7_selfcheck.py（仅当检查逻辑本身有缺陷时）",
                "src/",
                "core/",
                "plugins/",
                "integrations/",
            ],
            "forbidden": [
                "evals/golden-findings.json",
                "evals/cases.json",
                "evals/scenario-baseline.json",
                "verification/evals/golden-findings.json",
                "verification/evals/cases.json",
                "verification/evals/scenario-baseline.json",
            ],
        },
        "root": str(root),
    }


def run_once(*, host: str = "dsh", root: Path | None = None,
             mode: str | None = None,
             timeout: float = rsi_fixer.DEFAULT_FIX_TIMEOUT,
             dry_run: bool = False,
             dynamic: bool = False) -> dict[str, object]:
    """跑一轮「检查 → 立案 → 修 → 门禁」。

    ``dry_run`` 只生成工单不派 agent——用于验证工单构造是否正确，
    以及在还没有把握让 agent 改代码时先看清它要做什么。
    """
    root = (root or REPO_ROOT).resolve()
    mode = mode or rsi_scope.current_mode()
    started = time.monotonic()

    payload = l7_selfcheck.run_all(root, dynamic=dynamic)
    summary = payload.get("summary", {})
    report: dict[str, object] = {
        "schema_version": 1,
        "host": host,
        "mode": mode,
        "initial_ok": bool(summary.get("ok")),
        "initial_passed": summary.get("passed"),
        "initial_total": summary.get("total"),
        "initial_failed_checks": summary.get("checks_failed") or [],
        "duration_seconds": 0.0,
    }

    if summary.get("not_applicable"):
        # 「没检查」不是「检查通过」。非引擎目录上 L7 整体 N/A，此时报
        # already_healthy 会把"选错目标"伪装成"引擎健康"——与本模块要防的
        # 那类假成功（install --force 报成功却没写盘）是同一个病。
        report["status"] = "not_applicable"
        report["reason"] = summary.get("reason")
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        return report

    if summary.get("ok"):
        # 已经是绿的：不制造工作。闭环的价值在于「发现坏了能修」，
        # 而不是「每轮都必须干点什么」。
        report["status"] = "already_healthy"
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        return report

    work_order = build_work_order(payload, root=root, dynamic=dynamic)
    report["work_order"] = work_order
    if dry_run:
        report["status"] = "dry_run"
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        return report

    attempt = rsi_fixer.attempt_fix(
        work_order,
        host=host,
        root=root,
        timeout=timeout,
        mode=mode,
        regression_commands=[
            [str(part) for part in argv]
            for argv in work_order.get("regression_commands", [])
        ],
    )
    report["fix_attempt"] = attempt
    # ## 「门禁通过」和「已落地」是两件事，状态名必须分开
    #
    # 实测踩坑：agent 在沙箱里改好了、L7 7/7 通过、`verified=True`，但
    # run_once 一律报 `fixed`/`unfixed`——于是「候选改动已就绪，等你落地」
    # 和「agent 没修出来」显示成同一个词。我据此反复重跑同一处缺陷，
    # 而它早就修好了，只差一步 --apply。
    #
    # 按本模块刻意的设计（修复从不自动落地），三个状态要分清：
    #   verified_pending_apply  候选树已就绪，原仓库未动
    #   applied                 已落地且落地后复检通过（见 apply_candidate）
    #   unfixed                 确实没修出来
    if attempt.get("verified"):
        report["status"] = "verified_pending_apply"
        report["next_action"] = (
            "候选改动已通过双向门禁，原仓库未改动。"
            "落地：python3 -m core.repair.l7_repair_loop --apply"
            "（重跑一轮并在门禁通过后自动落地复检）"
        )
    else:
        report["status"] = "unfixed"
        report["error"] = attempt.get("error") or "未通过双向门禁"
    report["duration_seconds"] = round(time.monotonic() - started, 2)
    return report


def _run_l7_subprocess(root: Path, *, timeout: float = 120.0) -> dict[str, object]:
    """在**新解释器**里跑 L7，拿磁盘上的真实状态。

    为什么不能直接 ``l7_selfcheck.run_all(root)``：那是同进程调用，
    ``core/l7_selfcheck.py`` 早已 import 进 ``sys.modules``，改动文件后
    模块对象不会重新加载。落地复检因此永远读旧代码，把已修好的改动判死。
    """
    # 路径与执行方式都必须是「迁移后仍成立」的那一种：``core/l7_selfcheck.py``
    # 已归档到 ``core/repair/``；且该模块以点号导入兄弟包，直接跑脚本会在
    # ``__package__`` 为空时 ImportError。走 ``-m`` 才能同时满足两者，
    # ``cwd=root`` 保证被检的是**候选/目标仓库**而不是本进程所在仓库。
    code, out, err = rsi_fixer._run(
        [sys.executable, "-m", "core.repair.l7_selfcheck", "--json"],
        cwd=root, timeout=timeout,
    )
    if code != 0 and not (out or "").strip().startswith("{"):
        return {"ok": False, "failed": [f"L7 子进程退出码 {code}：{(err or out)[:200]}"]}
    try:
        import json as _json
        payload = _json.loads(out)
    except (ValueError, TypeError):
        return {"ok": False, "failed": ["L7 子进程输出无法解析"]}
    summary = payload.get("summary") or {}
    if summary.get("not_applicable"):
        # 目标不是引擎仓库：没有可复检的东西，按通过处理但不声称健康。
        return {"ok": True, "failed": [], "not_applicable": True}
    return {
        "ok": bool(summary.get("ok")),
        "failed": list(summary.get("checks_failed") or []),
        "passed": summary.get("passed"),
        "total": summary.get("total"),
    }


def apply_candidate(candidate_tree: Path, *, root: Path | None = None,
                    changed_files: list[str] | None = None,
                    regression_commands: list[list[str]] | None = None,
                    regression_timeout: float = 600.0) -> dict[str, object]:
    """把候选树的改动落地，并立刻复检；失败则回滚。

    ## 为什么落地后必须复检

    候选树里门禁通过，不代表复制回原仓库后仍然通过——目标仓库可能有
    候选树里没有的状态（未跟踪文件、本地改动）。所以流程是：

        备份受影响文件 → 复制 → 复检 L7 → 复检回归 → 失败则逐字节还原

    ## 为什么复检必须包含回归，而不只是 L7

    早期版本只复检 L7。**这是保险上的漏洞**：候选改动可能让 L7 全绿，
    同时弄坏一个 L7 看不见的东西（例如 worker 调度、MCP handler）——
    复制回原仓库后照样通过 L7，于是落地成功，坏改动留在主线上。

    它能被漏掉的原因很具体：L7 是**静态结构检查**，只覆盖「BugX 自身结构
    是否健康」，不覆盖「刚才这一改动是否弄坏了别的模块」。候选树里跑过
    回归命令，但那是在**候选树**里跑的；落地到真实仓库后，环境不同
    （未跟踪文件、本地改动、不同的 .bugx 状态），结论不自动继承。

    所以落地后的复检集合必须与门禁时一致：L7 + 全部回归命令。

    这是「带保险的落地」，不是「自动合并」。保险的具体形式是字节级备份 +
    L7 复检 + 回归复检 + 任一失败即回滚，而不是「相信门禁结论」。
    """
    root = (root or REPO_ROOT).resolve()
    tree = Path(candidate_tree).resolve()
    if not tree.is_dir():
        return {"applied": False, "error": f"候选树不存在：{tree}"}

    targets = changed_files or rsi_fixer._changed_files(root, tree)
    if not targets:
        return {"applied": False, "error": "候选树与原仓库无差异"}

    # 断言守卫：落地前再查一次，防止候选树本身动了评测断言
    blocked = [
        name for name in targets
        if name in rsi_scope.ASSERTION_FILES or name in rsi_scope.FORBIDDEN_FILES
    ]
    if blocked:
        return {"applied": False, "error": f"候选树改动了被禁文件：{blocked}"}

    backups: dict[str, bytes | None] = {}
    written: list[str] = []
    try:
        for name in targets:
            source = tree / name
            destination = root / name
            backups[name] = (
                destination.read_bytes() if destination.is_file() else None
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            written.append(name)

        # ## 复检必须开子进程，不能在当前解释器里调 run_all
        #
        # 实测事故：把候选文件复制回原仓库后，用同进程 `l7_selfcheck.run_all()`
        # 复检——它跑的是 **已经 import 进内存的旧代码**，永远看不到刚写下去
        # 的改动。于是每次落地都报「落地后 L7 复检失败」，然后回滚，
        # **候选改动永远落不了地**。
        #
        # 更隐蔽的是：同一份文件在新进程里跑是 7/7 通过的。所以这个 bug
        # 会让「已经修好的改动」被反复判死，而报告只说"复检失败"，看不出
        # 是缓存问题。
        #
        # 检查的对象是磁盘上的源码，那就必须让检查从磁盘重新加载——
        # 开子进程是唯一可靠的方式，也顺带隔离了检查自身对环境的副作用。
        recheck = _run_l7_subprocess(root)
        if not recheck.get("ok"):
            raise RuntimeError(
                "落地后 L7 复检失败：" + ", ".join(recheck.get("failed") or [])
            )

        # 回归复检：与门禁时的命令集一致。落地后环境不同于候选树，
        # 结论不能自动继承——必须重跑。
        regression_results: list[dict[str, object]] = []
        for argv in regression_commands or []:
            code, out, err = rsi_fixer._run(
                [str(part) for part in argv], cwd=root,
                timeout=regression_timeout,
            )
            regression_results.append({
                "command": " ".join(str(part) for part in argv),
                "exit_code": code,
                "tail": ((out or "") + (err or ""))[-400:],
            })
            if code != 0:
                raise RuntimeError(
                    "落地后回归复检失败（退出码 "
                    f"{code}）：{' '.join(str(part) for part in argv)}"
                )

        return {
            "applied": True,
            "changed_files": written,
            "recheck_passed": recheck.get("passed"),
            "recheck_total": recheck.get("total"),
            "regression_recheck": regression_results,
        }
    except (OSError, RuntimeError) as exc:
        for name, content in backups.items():
            destination = root / name
            try:
                if content is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.write_bytes(content)
            except OSError:
                continue
        return {"applied": False, "error": f"{type(exc).__name__}: {exc}",
                "rolled_back": True}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="L7 失败 → 立案 → 派宿主修 → 双向门禁",
    )
    parser.add_argument("--host", default="dsh")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--timeout", type=float,
                        default=rsi_fixer.DEFAULT_FIX_TIMEOUT)
    parser.add_argument("--dry-run", action="store_true",
                        help="只生成工单，不派 agent")
    parser.add_argument("--dynamic", action="store_true",
                        help="含动态检查（起子进程，约 12s）")
    parser.add_argument("--apply", action="store_true",
                        help="修好后落地并复检（失败自动回滚）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = run_once(
        host=args.host, root=args.root, timeout=args.timeout,
        dry_run=args.dry_run, dynamic=args.dynamic,
    )

    if args.apply and report.get("status") == "verified_pending_apply":
        attempt = report.get("fix_attempt") or {}
        candidate = attempt.get("candidate_tree")
        if candidate:
            order = report.get("work_order") or {}
            report["apply"] = apply_candidate(
                Path(str(candidate)), root=args.root,
                changed_files=list(attempt.get("changed_files") or []),
                regression_commands=[
                    [str(part) for part in argv]
                    for argv in order.get("regression_commands", [])
                ],
            )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"初始状态：{report['initial_passed']}/{report['initial_total']} 通过"
              f"（失败：{report['initial_failed_checks']}）")
        status = report.get("status")
        if status == "already_healthy":
            print("✓ 已健康，无需修复")
        elif status == "dry_run":
            order = report["work_order"]
            print(f"工单已生成（{len(order['diagnoses'])} 条诊断）：")
            for item in order["diagnoses"]:
                print(f"  [{item['level']}] {item['check']}：{item['reason'][:100]}")
            print(f"  复现命令：{order['reproduce'][0]['command']}")
        elif status == "verified_pending_apply":
            attempt = report["fix_attempt"]
            print(f"✓ 双向门禁通过，改动文件：{attempt.get('changed_files')}")
            print(f"  候选树：{attempt.get('candidate_tree')}")
            print("  （未写回原仓库——落地请加 --apply）")
        else:
            print(f"✗ 未能修复：{report.get('error')}")
        print(f"耗时 {report['duration_seconds']}s")
    return 0 if report.get("status") in {"already_healthy", "fixed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
