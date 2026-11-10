#!/usr/bin/env python3
"""Pure Host/Worker packet operations; no state mutation or invented receipts."""
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
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


def decode(packet: Any) -> dict[str, Any]:
    if not isinstance(packet, dict) or packet.get("isError"):
        raise ValueError("expected a successful JSON object")
    for key in ("structuredContent", "structured_content"):
        if isinstance(packet.get(key), dict):
            return packet[key]
    if isinstance(packet.get("result"), dict):
        return decode(packet["result"])
    blocks = packet.get("content")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    value = json.loads(block["text"])
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(value, dict):
                    return value
        return {"text": "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict))}
    return packet


def summary(packet: dict[str, Any]) -> dict[str, Any]:
    value = decode(packet)
    keys = ("status", "state", "active", "revision", "next_action", "run_id", "runtime_run_id",
            "polling_id", "session_id", "lifecycle_id", "round_no", "round_id", "life", "rounds_completed", "root",
            "build_id", "gate", "parallel_gate", "host_channel", "recovery_chain", "changed",
            "blocked_reason", "error", "errors", "prompt", "tool_feedback", "finding_count")
    result = {key: value[key] for key in keys if key in value}
    for key in value:
        if any(word in key.lower() for word in ("gate", "pending", "interaction", "required_action", "startup")):
            result[key] = value[key]
    if isinstance(value.get("state"), dict):
        result["state"] = summary(value["state"])
    for name in ("runtime", "supervisor", "legacy_supervisor"):
        if isinstance(value.get(name), dict):
            result[name] = summary(value[name])
    if isinstance(value.get("claims"), dict):
        result["claims"] = [{key: claim.get(key) for key in (
            "worker_id", "run_id", "round_no", "lease_id", "execution_id", "state",
            "execution_state", "unit_ids", "artifact_root", "provider", "target_fingerprint",
        )} for claim in value["claims"].values()]
    if isinstance(value.get("coverage"), dict):
        from .supervisor_models import VALID_COVERAGE_STATES
        coverage = value["coverage"]
        if all(isinstance(state, str) and state in VALID_COVERAGE_STATES for state in coverage.values()):
            counts: dict[str, int] = {}
            for state in coverage.values():
                counts[state] = counts.get(state, 0) + 1
            result["coverage_counts"] = counts
        else:
            result["coverage"] = {key: item for key, item in coverage.items()
                if isinstance(item, (bool, int, float)) or key in ("status", "counts", "total", "error")}
    if isinstance(value.get("findings"), list) and not value.get("findings_truncated"):
        result.setdefault("finding_count", len(value["findings"]))
    if "tokens_consumed" in value:
        method = value.get("token_method", "unmeasured")
        result.update(token_method=method, tokens_consumed=value["tokens_consumed"] if method != "unmeasured" else None)
    return result


def handoff(task: str, claim: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    required = ("worker_id", "run_id", "round_no", "lease_id", "execution_id", "artifact_root", "unit_ids", "target_fingerprint")
    missing = [key for key in required if claim.get(key) in (None, "", [])]
    if missing or not task.strip():
        raise ValueError(f"incomplete Task/claim identity: {missing}")
    for key in ("root", "authorization", "ownership", "graph_evidence", "coverage_gaps"):
        if key not in context:
            raise ValueError(f"missing bounded context: {key}")
    message = (
        "You are the assigned BugX Worker. Use only Worker capabilities; Host has routed knowledge.\n"
        "You are not alone in the codebase. Preserve other work; modify only the assigned scope.\n"
        "Use worker_start, then lease-bound run_sandboxed_probe. Inspect its output and source; "
        "submit returned evidence unchanged through Worker checkpoint. Never invent execution receipts.\n"
        "Claim identity:\n" + json.dumps(claim, ensure_ascii=False) +
        "\nBounded context and authorization:\n" + json.dumps(context, ensure_ascii=False) +
        "\nExact CLI-generated Task (authoritative):\n" + task
    )
    return {"fork_turns": "none", "message": message}


def checkpoint(packet: dict[str, Any], unit: str, note: str, *, covered: bool) -> dict[str, Any]:
    result = decode(packet)
    if unit not in result.get("unit_ids", []):
        raise ValueError("unit must be an exact unit_id returned by this probe's claim")
    evidence = result.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("execution_receipt"), dict):
        raise ValueError("probe evidence/receipt missing; rerun the real Worker probe")
    receipt = evidence["execution_receipt"]
    if receipt.get("schema_version") != 1:
        raise ValueError("unsupported receipt schema")
    for key in ("worker_id", "lease_id", "command", "exit_code", "artifact_path", "artifact_sha256", "status"):
        if key not in evidence or receipt.get(key) != evidence[key]:
            raise ValueError(f"receipt mismatch: {key}")
    if not receipt.get("execution_id") or not unit or not note.strip():
        raise ValueError("execution identity, exact unit and audit note are required")
    if covered and (evidence.get("status") != "passed" or evidence.get("exit_code") != 0):
        raise ValueError("failed execution cannot be covered")
    payload = {"caller_role": "worker", "worker_id": evidence["worker_id"],
               "coverage": {unit: "covered" if covered else "unverified"},
               "evidence": {unit: evidence}, "notes": {unit: note}, "life_delta": 0}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {**payload, "checkpoint_id": "checkpoint-" + digest}


def zero_review(identity: dict[str, Any], reason: str) -> dict[str, Any]:
    if identity.get("finding_count") != 0:
        raise ValueError("zero-review requires an observed finding_count=0")
    for key in ("polling_id", "lifecycle_id", "round"):
        if not identity.get(key):
            raise ValueError(f"missing actual lifecycle identity: {key}")
    if not reason.strip():
        raise ValueError("an evidence-based no-new-lesson reason is required")
    return {"actor": "supervisor", **{k: identity[k] for k in ("polling_id", "lifecycle_id", "round")},
            "finding_count": 0, "credited_count": 0, "lesson_ids": [], "finding_fingerprints": [],
            "new_lessons": [], "no_new_lesson_reason": reason}


def paths(root: Path) -> dict[str, Any]:
    import core.bugx_paths as bugx_paths
    active = bugx_paths.require(root.resolve())
    baseline = root.resolve() / ".bugx" / "state" / "pre-baseline.json"
    return {"lifecycle_root": str(active.lifecycle_root), "state_root": str(active.state_root),
            "prep_record": str(active.prep_record), "supervisor": str(active.supervisor),
            "pre_baseline_candidate": str(baseline) if baseline.is_file() else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("decode", "summary", "handoff", "checkpoint", "zero-review", "prep-headings", "paths"))
    parser.add_argument("--input", type=Path, help="JSON packet (default stdin)")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--unit")
    parser.add_argument("--note", default="")
    parser.add_argument("--covered", action="store_true", help="only after source and output review")
    args = parser.parse_args(argv)
    try:
        if args.operation == "paths":
            if not args.root:
                raise ValueError("paths requires the explicit --root")
            print(json.dumps(paths(args.root), ensure_ascii=False, indent=2))
            return 0
        if args.operation == "prep-headings":
            from core.runtime.prep_validate import REQUIRED
            print(json.dumps({"required_headings": list(REQUIRED)}, ensure_ascii=False, indent=2))
            return 0
        packet = json.loads(args.input.read_text() if args.input else sys.stdin.read())
        if args.operation == "decode":
            result = decode(packet)
        elif args.operation == "summary":
            result = summary(packet)
        elif args.operation == "handoff":
            if not args.task or not args.context:
                raise ValueError("handoff needs --task and --context")
            result = handoff(args.task.read_text(), decode(packet), json.loads(args.context.read_text()))
        elif args.operation == "checkpoint":
            result = checkpoint(packet, args.unit or "", args.note, covered=args.covered)
        else:
            result = zero_review(decode(packet), args.note)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
