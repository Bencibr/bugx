#!/usr/bin/env python3
"""Bounded Worker checkpoint finalizer for host-agent tasks.

The host still owns scheduling and repair. This command only turns a
Worker's already-written artifact into the existing Supervisor checkpoint
contract, so a model cannot finish with an uncommitted evidence file.
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
import hashlib
import json
import time
import uuid
import sys
from pathlib import Path
from typing import Mapping

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.supervisor import audit_supervisor
import core.secure_atomic as secure_atomic


_STATUSES = ("covered", "unverified", "partial", "blocked")


def _confined_artifact(root: Path, artifact_root: str, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError("artifact 必须是相对仓库根的路径")
    claim_root = (root / artifact_root).resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(claim_root)
    except ValueError as exc:
        raise ValueError("artifact 必须位于当前 claim 的 artifact_root 内") from exc
    if not resolved.is_file():
        raise ValueError(f"artifact 不是普通文件: {value}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize(
    root: Path,
    state_path: Path,
    worker_id: str,
    *,
    status: str,
    command: str,
    exit_code: int,
    artifact: str | None,
    note: str,
    tokens: int = 0,
    artifacts: Mapping[str, str] | None = None,
) -> dict[str, object]:
    if status not in _STATUSES:
        raise ValueError(f"status 必须是 {_STATUSES}")
    if not command.strip():
        raise ValueError("command 不能为空")
    if status == "covered" and exit_code != 0:
        raise ValueError("covered 要求 exit_code=0")

    supervisor = audit_supervisor.Supervisor(root, state_path=state_path)
    state = supervisor.status()
    claim = state.claims.get(worker_id)
    if claim is None or not claim.active:
        raise audit_supervisor.ClaimRejected(f"worker claim 不可提交: {worker_id}")

    unit_set = set(claim.unit_ids)
    if artifacts is not None:
        if set(artifacts) != unit_set:
            raise ValueError("artifacts 必须逐一提供当前 claim 的每个 unit artifact")
        if any(not isinstance(value, str) or not value.strip() for value in artifacts.values()):
            raise ValueError("artifacts 的路径必须是非空字符串")
    elif artifact and len(claim.unit_ids) > 1:
        raise ValueError("多 unit claim 必须使用 artifacts=unit_id -> artifact；禁止复用单一 artifact")

    evidence_status = "passed" if status == "covered" else "blocked"
    evidence_exit = 0 if status == "covered" else (exit_code or 1)
    evidence: dict[str, dict[str, object]] = {}
    for unit in claim.unit_ids:
        unit_artifact = artifacts.get(unit) if artifacts is not None else artifact
        if unit_artifact:
            artifact_path = _confined_artifact(root, claim.artifact_root, unit_artifact)
        else:
            # Recovery is intentionally non-covered.  Multi-unit claims still
            # receive one file per unit, so the validator can prove ownership.
            if len(claim.unit_ids) == 1:
                recovery_name = "checkpoint-recovery.json"
            else:
                suffix = hashlib.sha256(unit.encode("utf-8")).hexdigest()[:16]
                recovery_name = f"checkpoint-recovery-{suffix}.json"
            artifact_path = (root / claim.artifact_root / recovery_name).resolve()
            secure_atomic.atomic_write_json(
                artifact_path,
                {
                    "worker_id": worker_id,
                    "unit_id": unit,
                    "run_id": state.run_id,
                    "round_no": claim.round_no,
                    "status": status,
                    "command": command,
                    "note": note,
                    "timestamp": time.time(),
                },
                mode=0o600,
                parent_mode=0o700,
            )
        artifact_rel = artifact_path.relative_to(root.resolve()).as_posix()
        artifact_sha256 = _sha256(artifact_path)
        evidence_entry: dict[str, object] = {
            "command": command,
            "exit_code": evidence_exit,
            "artifact_path": artifact_rel,
            "artifact_sha256": artifact_sha256,
            "target_fingerprint": state.target_fingerprint,
            "timestamp": time.time(),
            "worker_id": worker_id,
            "run_id": state.run_id,
            "round_no": claim.round_no,
            "lease_id": claim.lease_id,
            "status": evidence_status,
            "execution_receipt": {
                "schema_version": 1,
                "execution_id": claim.execution_id,
                "worker_id": worker_id,
                "lease_id": claim.lease_id,
                "command": command,
                "exit_code": evidence_exit,
                "artifact_path": artifact_rel,
                "artifact_sha256": artifact_sha256,
                "status": evidence_status,
            },
        }
        if tokens > 0:
            evidence_entry["tokens_consumed"] = int(tokens)
            evidence_entry["token_method"] = "worker_reported"
        evidence[unit] = evidence_entry
    checkpoint = supervisor.submit_checkpoint(
        worker_id,
        checkpoint_id=f"checkpoint-{worker_id}-{uuid.uuid4().hex}",
        coverage={unit: status for unit in claim.unit_ids},
        evidence=evidence,
        notes={unit: note for unit in claim.unit_ids},
        life_delta=0,
    )
    return {
        "worker_id": worker_id,
        "checkpoint_id": checkpoint.claims[worker_id].last_checkpoint_id,
        "coverage": {unit: status for unit in claim.unit_ids},
        "revision": checkpoint.revision,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("finalize", nargs="?")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--status", choices=_STATUSES, required=True)
    parser.add_argument("--artifact")
    parser.add_argument(
        "--artifacts-json",
        help="JSON object mapping each claimed unit_id to its artifact path",
    )
    parser.add_argument("--command", required=True)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--note", required=True)
    parser.add_argument(
        "--tokens", type=int, default=0,
        help="本 Worker 实际消耗的 token 数（宿主从 Agent 结果读取）；"
             ">0 时写入证据条目，由 Supervisor 计入 claim/账本/报告/云端汇总",
    )
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    state_path = args.state if args.state.is_absolute() else root / args.state
    try:
        artifacts = None
        if args.artifacts_json:
            parsed = json.loads(args.artifacts_json)
            if not isinstance(parsed, dict):
                raise ValueError("--artifacts-json 必须是 JSON 对象")
            artifacts = parsed
        result = finalize(
            root, state_path, args.worker, status=args.status,
            command=args.command, exit_code=args.exit_code,
            artifact=args.artifact, note=args.note, tokens=args.tokens,
            artifacts=artifacts,
        )
    except (OSError, ValueError, audit_supervisor.SupervisorError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
