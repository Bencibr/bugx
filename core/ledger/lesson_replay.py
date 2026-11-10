#!/usr/bin/env python3
"""离线评估错题集检索质量，不向运行账本写入任何数据。

输入是一个 JSON 对象或数组，单个 case 至少包含 ``query``；可选
``expected_pattern_ids`` 和 ``category`` 用于计算 holdout 命中率。该工具只输出
聚合指标与少量 case 结果，避免把历史复盘内容重新灌入模型上下文。
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
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from core.governance.token_budget import budget_summary

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CASES = SCRIPT_DIR / "lesson-replay-cases.json"


class ReplayError(ValueError):
    """回放输入或账本协议无效。"""


def _ledger_module() -> Any:
    path = SCRIPT_DIR / "lesson_ledger.py"
    spec = importlib.util.spec_from_file_location("bugx_lesson_ledger", path)
    if spec is None or spec.loader is None:
        raise ReplayError(f"无法加载错题集模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"回放数据无效: {exc}") from exc
    if isinstance(value, dict):
        value = value.get("cases")
    if not isinstance(value, list) or not value:
        raise ReplayError("回放数据必须是非空 cases 数组")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or not isinstance(item.get("query"), str):
            raise ReplayError(f"第 {index + 1} 个回放 case 缺少 query")
        expected = item.get("expected_pattern_ids", [])
        if not isinstance(expected, list) or not all(isinstance(x, str) for x in expected):
            raise ReplayError(f"第 {index + 1} 个 case 的 expected_pattern_ids 无效")
        result.append({**item, "expected_pattern_ids": expected})
    return result


def evaluate(ledger_path: Path, cases_path: Path, *, limit: int = 6,
             token_budget: int = 1200, byte_budget: int = 9000,
             holdout_mod: int = 5, holdout_remainder: int = 0) -> dict[str, Any]:
    if not 1 <= limit <= 100 or token_budget < 1 or byte_budget < 1:
        raise ReplayError("limit/token_budget/byte_budget 参数无效")
    if holdout_mod < 2 or not 0 <= holdout_remainder < holdout_mod:
        raise ReplayError("holdout 参数无效")
    ledger = _ledger_module()
    cases = _cases(cases_path)
    evaluated: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        rows = ledger.retrieve(
            ledger_path, query=case["query"], category=case.get("category"),
            limit=limit, token_budget=token_budget, byte_budget=byte_budget,
        )
        returned = [row.get("pattern_id") for row in rows if row.get("pattern_id")]
        expected = set(case["expected_pattern_ids"])
        hit = bool(expected.intersection(returned)) if expected else None
        usage = budget_summary(rows)
        record = {
            "case_id": str(case.get("case_id", index + 1)),
            "returned_pattern_ids": returned,
            "expected_count": len(expected),
            "hit": hit,
            "cards": len(rows),
            "tokens_estimate": usage["tokens"],
            "bytes": usage["bytes"],
            "token_method": usage["token_method"],
        }
        evaluated.append(record)
        if index % holdout_mod == holdout_remainder and expected:
            holdout.append(record)
    labeled = [x for x in evaluated if x["hit"] is not None]
    hits = sum(1 for x in labeled if x["hit"])
    # 对照只测“检索记忆可用性”：无账本时不注入 pattern 卡片，因此它不是
    # 完整 Agent 的生产 A/B 结果，不能用来声称 Bug 发现率提升。
    availability_lift = round(hits / len(labeled), 4) if labeled else None
    return {
        "schema_version": 1,
        "evaluation": "lesson-retrieval-holdout",
        "cases_total": len(evaluated),
        "labeled_cases": len(labeled),
        "hit_cases": hits,
        "hit_rate": round(hits / len(labeled), 4) if labeled else None,
        "without_memory_hit_rate": 0.0 if labeled else None,
        "retrieval_availability_lift": availability_lift,
        "comparison_scope": "retrieval-availability-only; not an end-to-end bug-discovery A/B result",
        "holdout_cases": len(holdout),
        "holdout_hit_rate": round(sum(1 for x in holdout if x["hit"]) / len(holdout), 4) if holdout else None,
        "budget": {"limit": limit, "token_budget": token_budget, "byte_budget": byte_budget},
        "cases": evaluated,
    }


def evaluate_gain(ledger_path: Path, *, limit: int = 6,
                  token_budget: int = 1200, byte_budget: int = 9000) -> dict[str, Any]:
    """时序 holdout：incident 发生时，既有模式能否被签名检索命中。

    对每个绑定了 pattern 的 incident，只用 ``created_at`` 早于该 incident 的
    pattern 做检索（模式晚于 incident 创建的记为 not-yet-born，不计入命中
    分母也不算命中，避免泄漏）。对照基线是同一时点下无签名的通用检索。
    这是检索层的知识增益代理指标；真实 Bug 发现率提升仍需独立 A/B 运行。
    """
    ledger = _ledger_module()
    incidents = [
        item for item in ledger.list_incidents(ledger_path)
        if item.get("pattern_id") and item.get("signature")
    ]
    evaluated: list[dict[str, Any]] = []
    signature_hits = baseline_hits = 0
    signature_reciprocal: list[float] = []
    not_yet_born = 0
    for item in incidents:
        moment = str(item["created_at"])
        pattern = ledger.read_pattern(ledger_path, item["pattern_id"])
        if pattern is None:
            continue
        if not ledger.created_not_after(pattern["created_at"], moment):
            not_yet_born += 1
            continue
        query = " ".join([str(item["signature"]), str(item.get("summary") or "")])
        rows = ledger.retrieve(
            ledger_path, query=query, limit=limit,
            token_budget=token_budget, byte_budget=byte_budget,
            created_before=moment,
        )
        returned = [row.get("pattern_id") for row in rows if row.get("pattern_id")]
        baseline_rows = ledger.retrieve(
            ledger_path, query="", limit=limit,
            token_budget=token_budget, byte_budget=byte_budget,
            created_before=moment,
        )
        baseline_returned = [
            row.get("pattern_id") for row in baseline_rows if row.get("pattern_id")
        ]
        target = item["pattern_id"]
        hit = target in returned
        baseline_hit = target in baseline_returned
        if hit:
            signature_hits += 1
            signature_reciprocal.append(1.0 / (returned.index(target) + 1))
        if baseline_hit:
            baseline_hits += 1
        evaluated.append({
            "incident_id": item["incident_id"], "pattern_id": target,
            "created_at": moment, "hit": hit, "baseline_hit": baseline_hit,
            "returned_pattern_ids": returned,
        })
    total = len(evaluated)
    return {
        "schema_version": 1,
        "evaluation": "temporal-knowledge-gain",
        "incidents_total": len(incidents),
        "evaluated": total,
        "pattern_not_yet_born": not_yet_born,
        "signature_hits": signature_hits,
        "signature_hit_rate": round(signature_hits / total, 4) if total else None,
        "signature_mrr": round(
            sum(signature_reciprocal) / len(signature_reciprocal), 4
        ) if signature_reciprocal else None,
        "baseline_hits": baseline_hits,
        "baseline_hit_rate": round(baseline_hits / total, 4) if total else None,
        "comparison_scope": "retrieval-level proxy at incident time; not an end-to-end bug-discovery A/B result",
        "budget": {"limit": limit, "token_budget": token_budget, "byte_budget": byte_budget},
        "cases": evaluated,
    }


def evaluate_tools(ledger_path: Path, cases: list[dict[str, Any]], *,
                   limit: int = 4) -> dict[str, Any]:
    """回放工具经验检索：case 含 query/category 与 expected_tool_names。"""
    ledger = _ledger_module()
    evaluated: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        query = case.get("query", "")
        rows = ledger.lookup_tools(
            ledger_path, query=query, category=case.get("category"), limit=limit,
        )
        returned = [row.get("tool_name") for row in rows if row.get("tool_name")]
        expected = {str(x) for x in case.get("expected_tool_names", [])}
        hit = bool(expected.intersection(returned)) if expected else None
        reciprocal = 0.0
        for rank, name in enumerate(returned, 1):
            if name in expected:
                reciprocal = 1.0 / rank
                break
        evaluated.append({
            "case_id": str(case.get("case_id", index + 1)),
            "returned_tool_names": returned,
            "expected_count": len(expected),
            "hit": hit,
            "reciprocal_rank": reciprocal,
        })
    labeled = [x for x in evaluated if x["hit"] is not None]
    hits = sum(1 for x in labeled if x["hit"])
    return {
        "schema_version": 1,
        "evaluation": "tool-retrieval-replay",
        "cases_total": len(evaluated),
        "labeled_cases": len(labeled),
        "hit_cases": hits,
        "hit_rate": round(hits / len(labeled), 4) if labeled else None,
        "mrr": round(
            sum(x["reciprocal_rank"] for x in labeled) / len(labeled), 4
        ) if labeled else None,
        "budget": {"limit": limit},
        "cases": evaluated,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--token-budget", type=int, default=1200)
    parser.add_argument("--byte-budget", type=int, default=9000)
    parser.add_argument("--holdout-mod", type=int, default=5)
    parser.add_argument("--holdout-remainder", type=int, default=0)
    parser.add_argument("--mode", choices=("cases", "gain", "tools"), default="cases",
                        help="cases=模式检索回放；gain=incident 时序 holdout；tools=工具经验回放")
    args = parser.parse_args(argv)
    try:
        if args.mode == "gain":
            payload: dict[str, Any] = evaluate_gain(
                args.ledger, limit=args.limit,
                token_budget=args.token_budget, byte_budget=args.byte_budget,
            )
        elif args.mode == "tools":
            payload = evaluate_tools(args.ledger, _cases(args.cases), limit=4)
        else:
            payload = evaluate(args.ledger, args.cases, limit=args.limit,
                               token_budget=args.token_budget, byte_budget=args.byte_budget,
                               holdout_mod=args.holdout_mod,
                               holdout_remainder=args.holdout_remainder)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (ReplayError, OSError, ValueError) as exc:
        print(f"[lesson_replay] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
