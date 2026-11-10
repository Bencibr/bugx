#!/usr/bin/env python3
"""A/B 评测：incident 深卡签名检索对仅关键词检索的增益。

只测检索管线，不调用模型；真实 Bug 发现率提升必须由独立 holdout 运行对比。
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
import sys
from pathlib import Path


import argparse
import json
import shutil
import tempfile

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent
FIXTURE_ROOT = (REPO_ROOT / "verification" / "evals" / "incident-fixtures") if (REPO_ROOT / "verification" / "evals").is_dir() else (REPO_ROOT / "evals" / "incident-fixtures")


def load_fixtures(root: Path = FIXTURE_ROOT) -> list[dict]:
    fixtures = []
    if not root.is_dir():
        raise FileNotFoundError(f"缺少 incident 评测夹具: {root}")
    for case_dir in sorted(root.iterdir()):
        meta = case_dir / "meta.json"
        before = case_dir / "before.py"
        after = case_dir / "after.py"
        if not (meta.is_file() and before.is_file() and after.is_file()):
            continue
        fixture = json.loads(meta.read_text(encoding="utf-8"))
        fixture.update({
            "case_id": case_dir.name,
            "before_snippet": before.read_text(encoding="utf-8"),
            "after_snippet": after.read_text(encoding="utf-8"),
        })
        fixtures.append(fixture)
    if not fixtures:
        raise ValueError("incident 评测夹具为空")
    return fixtures


def score(ledger: Path, fixtures: list[dict]) -> dict:
    from core.ledger import lesson_ledger

    def _rank(signatures: bool) -> tuple[int, float]:
        total_at_1 = 0
        mrr = 0.0
        for item in fixtures:
            kwargs = (
                {"signatures": [item["signature"]]}
                if signatures else {"query": item.get("query", "")}
            )
            hits = lesson_ledger.lookup_incidents(ledger, limit=2, **kwargs)
            if not hits:
                continue
            if hits[0]["incident_id"] == item["incident_id"]:
                total_at_1 += 1
            for index, hit in enumerate(hits, 1):
                if hit["incident_id"] == item["incident_id"]:
                    mrr += 1.0 / index
                    break
        return total_at_1, mrr

    # Signature mode uses the exact error signature; query mode mimics a fuzzy
    # natural-language lookup without memorizing the signature.
    signature_hits, signature_mrr = _rank(signatures=True)
    query_hits, query_mrr = _rank(signatures=False)
    query_mrr /= max(len(fixtures), 1)
    return {
        "schema_version": 1,
        "fixtures": len(fixtures),
        "signature_recall_at_1": round(signature_hits / len(fixtures), 4),
        "signature_mrr": round(signature_mrr / len(fixtures), 4),
        "query_recall_at_1": round(query_hits / len(fixtures), 4),
        "query_mrr": round(query_mrr, 4),
        "gain_recall": round(
            (signature_hits - query_hits) / len(fixtures), 4,
        ),
    }


def run(ledger_holder: Path | None = None) -> tuple[Path, dict]:
    from core.ledger import lesson_ledger

    if ledger_holder is None:
        ledger_holder = Path(
            tempfile.mkdtemp(prefix="incident-eval-"),
        ) / "lesson-ledger.json"
    else:
        ledger_holder = Path(ledger_holder)
    seed = AGENT_DIR / "lesson-ledger.json"
    if not seed.is_file():
        raise FileNotFoundError(f"缺少错题集种子: {seed}")
    ledger_holder.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed, ledger_holder)
    lesson_ledger.initialize(ledger_holder)
    fixtures = load_fixtures()
    for item in fixtures:
        item["incident_id"] = lesson_ledger.incident_id(
            item["signature"], item["summary"], item.get("commit_ref"),
        )
        lesson_ledger.add_incident(ledger_holder, {
            "signature": item["signature"],
            "summary": item["summary"],
            "context": item["context"],
            "before_snippet": item["before_snippet"],
            "after_snippet": item["after_snippet"],
            "diff_ref": "before.py:after.py",
            "commit_ref": item["commit_ref"],
            "decision": item["decision"],
            "duration_seconds": 600,
            "lifecycle_id": "eval",
            "round": 1,
        })
    return ledger_holder, score(ledger_holder, fixtures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        _, metrics = run()
    except (OSError, ValueError, FileNotFoundError) as exc:
        print(f"[incident_eval] ✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print("incident 检索 A/B（不调用模型）")
        for key, value in metrics.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
