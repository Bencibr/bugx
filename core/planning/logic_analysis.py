#!/usr/bin/env python3
"""意图、逻辑链、Oracle 与证据包的最小可审计契约。

该模块只负责结构化、校验和状态降级，不替模型推断真实意图，也不把静态命中
直接升级为 confirmed。它可被 Worker 调用，结算仍由现有调度器负责。
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
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SOURCE_TIERS = {"explicit", "contract", "test", "invariant", "inferred", "model"}
INTENT_CONFIDENCE = {"high", "medium", "low"}
CHAIN_STATES = {"hypothesized", "observed", "verified", "contradicted", "unverified"}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class LogicAnalysisError(ValueError):
    """逻辑分析契约无效。"""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise LogicAnalysisError(f"{field} 必须是非空字符串")
    return value.strip()


def _hash(value: Any, field: str) -> str:
    result = _text(value, field).lower()
    if not HASH_RE.fullmatch(result):
        raise LogicAnalysisError(f"{field} 必须是 64 位小写 SHA-256")
    return result


def _path(value: Any, field: str, root: Path | None = None) -> str:
    result = _text(value, field)
    candidate = Path(result)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise LogicAnalysisError(f"{field} 必须是仓库内相对路径")
    if root is not None:
        try:
            (root / candidate).resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise LogicAnalysisError(f"{field} 越过仓库根目录") from exc
    return candidate.as_posix()


def _list(value: Any, field: str, *, allow_empty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise LogicAnalysisError(f"{field} 必须是{'非空' if not allow_empty else ''}数组")
    return value


def _refs(value: Any, field: str, root: Path | None = None) -> list[dict]:
    refs = _list(value, field)
    result = []
    for index, ref in enumerate(refs):
        name = f"{field}[{index}]"
        if not isinstance(ref, dict):
            raise LogicAnalysisError(f"{name} 必须是对象")
        normalized = dict(ref)
        if "path" in ref:
            normalized["path"] = _path(ref["path"], f"{name}.path", root)
            line = ref.get("line")
            if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
                raise LogicAnalysisError(f"{name}.line 必须是正整数")
        elif "kind" not in ref:
            raise LogicAnalysisError(f"{name} 必须包含 path 或 kind")
        result.append(normalized)
    return result


def build_intent_hypothesis(
    *,
    intent_id: str,
    subject: str,
    statement: str,
    source_refs: list[dict],
    source_tier: str,
    preconditions: list[str] | None = None,
    expected_outcomes: list[str] | None = None,
    forbidden_side_effects: list[str] | None = None,
    contradictions: list[str] | None = None,
    scope: str = "round",
) -> dict:
    if source_tier not in SOURCE_TIERS:
        raise LogicAnalysisError(f"source_tier 无效: {source_tier}")
    if scope not in {"project", "round", "task"}:
        raise LogicAnalysisError(f"scope 无效: {scope}")
    payload = {
        "intent_id": _text(intent_id, "intent_id"),
        "subject": _text(subject, "subject"),
        "statement": _text(statement, "statement"),
        "source_refs": _refs(source_refs, "source_refs"),
        "source_tier": source_tier,
        "intent_confidence": "high" if source_tier in {"explicit", "contract"} else "medium",
        "preconditions": [_text(item, "preconditions item") for item in (preconditions or [])],
        "expected_outcomes": [_text(item, "expected_outcomes item") for item in (expected_outcomes or [])],
        "forbidden_side_effects": [_text(item, "forbidden_side_effects item") for item in (forbidden_side_effects or [])],
        "contradictions": [_text(item, "contradictions item") for item in (contradictions or [])],
        "scope": scope,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["evidence_hash"] = hashlib.sha256(encoded).hexdigest()
    return payload


def build_logic_chain(*, chain_id: str, intent_id: str, nodes: list[dict], edges: list[dict], target_fingerprint: str) -> dict:
    chain_id = _text(chain_id, "chain_id")
    intent_id = _text(intent_id, "intent_id")
    target_fingerprint = _hash(target_fingerprint, "target_fingerprint")
    node_ids: set[str] = set()
    normalized_nodes = []
    for index, node in enumerate(_list(nodes, "nodes", allow_empty=False)):
        field = f"nodes[{index}]"
        if not isinstance(node, dict):
            raise LogicAnalysisError(f"{field} 必须是对象")
        node_id = _text(node.get("id"), f"{field}.id")
        if node_id in node_ids:
            raise LogicAnalysisError(f"节点重复: {node_id}")
        node_ids.add(node_id)
        state = node.get("status", "hypothesized")
        if state not in CHAIN_STATES:
            raise LogicAnalysisError(f"{field}.status 无效")
        normalized_nodes.append({**node, "id": node_id, "status": state})
    normalized_edges = []
    for index, edge in enumerate(_list(edges, "edges")):
        field = f"edges[{index}]"
        if not isinstance(edge, dict):
            raise LogicAnalysisError(f"{field} 必须是对象")
        source = _text(edge.get("from"), f"{field}.from")
        target = _text(edge.get("to"), f"{field}.to")
        if source not in node_ids or target not in node_ids:
            raise LogicAnalysisError(f"{field} 引用了不存在节点")
        normalized_edges.append({**edge, "from": source, "to": target})
    return {
        "chain_id": chain_id,
        "intent_id": intent_id,
        "target_fingerprint": target_fingerprint,
        "nodes": normalized_nodes,
        "edges": normalized_edges,
        "status": "hypothesized",
    }


def build_oracle_spec(*, oracle_id: str, logic_chain_ref: str, input_state: str,
                      expected_behavior: str, forbidden_behavior: str, failure_state: str,
                      observation_method: str, negative_control: str,
                      reproduction_requirement: str) -> dict:
    return {
        "oracle_id": _text(oracle_id, "oracle_id"),
        "logic_chain_ref": _text(logic_chain_ref, "logic_chain_ref"),
        "input_state": _text(input_state, "input_state"),
        "expected_behavior": _text(expected_behavior, "expected_behavior"),
        "forbidden_behavior": _text(forbidden_behavior, "forbidden_behavior"),
        "failure_state": _text(failure_state, "failure_state"),
        "observation_method": _text(observation_method, "observation_method"),
        "negative_control": _text(negative_control, "negative_control"),
        "reproduction_requirement": _text(reproduction_requirement, "reproduction_requirement"),
        "frozen": True,
    }


def build_evidence_packet(*, candidate_id: str, intent_id: str, logic_chain_ref: str,
                          oracle_ref: str, target_fingerprint: str, execution_attempt_id: str,
                          normal_observation: str, boundary_observations: list[str],
                          negative_control_observation: str | None,
                          independent_reproduction: dict | None, root_cause_summary: str | None,
                          artifact_refs: list[dict], artifact_hashes: list[str],
                          tool_receipts: list[dict], knowledge_context_hash: str) -> dict:
    packet = {
        "candidate_id": _text(candidate_id, "candidate_id"),
        "intent_id": _text(intent_id, "intent_id"),
        "logic_chain_ref": _text(logic_chain_ref, "logic_chain_ref"),
        "oracle_ref": _text(oracle_ref, "oracle_ref"),
        "target_fingerprint": _hash(target_fingerprint, "target_fingerprint"),
        "execution_attempt_id": _text(execution_attempt_id, "execution_attempt_id"),
        "normal_observation": _text(normal_observation, "normal_observation"),
        "boundary_observations": [_text(item, "boundary_observations item") for item in boundary_observations],
        "negative_control_observation": (
            _text(negative_control_observation, "negative_control_observation")
            if negative_control_observation is not None else None
        ),
        "independent_reproduction": independent_reproduction,
        "root_cause_summary": (
            _text(root_cause_summary, "root_cause_summary") if root_cause_summary is not None else None
        ),
        "artifact_refs": _refs(artifact_refs, "artifact_refs"),
        "artifact_hashes": [_hash(item, "artifact_hashes item") for item in artifact_hashes],
        "tool_receipts": _list(tool_receipts, "tool_receipts"),
        "knowledge_context_hash": _hash(knowledge_context_hash, "knowledge_context_hash"),
    }
    packet["status"] = evidence_status(packet)
    return packet


def evidence_status(packet: dict) -> str:
    """判断是否具备进入既有 confirmation gate 的最低材料。

    这里故意不返回 ``confirmed``：最终确认仍由
    ``finding_confirmation.gate`` 的完整门禁裁决，避免本模块形成第二套、
    更弱的确认系统。
    """
    required = (
        packet.get("normal_observation"),
        packet.get("negative_control_observation"),
        packet.get("independent_reproduction"),
        packet.get("root_cause_summary"),
    )
    if all(required):
        reproduction = packet["independent_reproduction"]
        if isinstance(reproduction, dict) and reproduction.get("status") == "pass":
            return "ready_for_confirmation"
    return "unverified"


def build_bundle(*, intent: dict, logic_chain: dict, oracle: dict, evidence: dict) -> dict:
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "intent_hypothesis": copy.deepcopy(intent),
        "logic_chain": copy.deepcopy(logic_chain),
        "oracle_spec": copy.deepcopy(oracle),
        "evidence_packet": copy.deepcopy(evidence),
    }
    validate_bundle(bundle)
    return bundle


def validate_bundle(bundle: dict, root: Path | None = None) -> dict:
    if not isinstance(bundle, dict) or bundle.get("schema_version") != SCHEMA_VERSION:
        raise LogicAnalysisError("logic analysis schema_version 必须为 1")
    for name in ("intent_hypothesis", "logic_chain", "oracle_spec", "evidence_packet"):
        if not isinstance(bundle.get(name), dict):
            raise LogicAnalysisError(f"缺少 {name}")
    intent = bundle["intent_hypothesis"]
    if intent.get("source_tier") not in SOURCE_TIERS:
        raise LogicAnalysisError("intent_hypothesis.source_tier 无效")
    if intent.get("intent_confidence") not in INTENT_CONFIDENCE:
        raise LogicAnalysisError("intent_hypothesis.intent_confidence 无效")
    _refs(intent.get("source_refs"), "intent_hypothesis.source_refs", root)
    chain = bundle["logic_chain"]
    if chain.get("intent_id") != intent.get("intent_id"):
        raise LogicAnalysisError("logic_chain.intent_id 与 intent 不一致")
    rebuilt_chain = build_logic_chain(
        chain_id=chain.get("chain_id"), intent_id=chain.get("intent_id"),
        nodes=chain.get("nodes"), edges=chain.get("edges"),
        target_fingerprint=chain.get("target_fingerprint"),
    )
    chain_status = chain.get("status", "hypothesized")
    if chain_status not in CHAIN_STATES:
        raise LogicAnalysisError("logic_chain.status 无效")
    # Rebuilding normalizes references, but must not erase an observed/verified
    # chain state during validation.
    rebuilt_chain["status"] = chain_status
    oracle = bundle["oracle_spec"]
    if oracle.get("logic_chain_ref") != chain.get("chain_id") or oracle.get("frozen") is not True:
        raise LogicAnalysisError("oracle_spec 必须绑定已封存 logic_chain")
    evidence = bundle["evidence_packet"]
    if evidence.get("intent_id") != intent.get("intent_id"):
        raise LogicAnalysisError("evidence_packet.intent_id 与 intent 不一致")
    if evidence.get("logic_chain_ref") != chain.get("chain_id"):
        raise LogicAnalysisError("evidence_packet.logic_chain_ref 与 chain 不一致")
    if evidence.get("oracle_ref") != oracle.get("oracle_id"):
        raise LogicAnalysisError("evidence_packet.oracle_ref 与 oracle 不一致")
    if evidence.get("target_fingerprint") != chain.get("target_fingerprint"):
        raise LogicAnalysisError("evidence_packet.target_fingerprint 与 chain 不一致")
    _refs(evidence.get("artifact_refs"), "evidence_packet.artifact_refs", root)
    status = evidence_status(evidence)
    if evidence.get("status") != status:
        raise LogicAnalysisError("evidence_packet.status 与证据完整度不一致")
    if status == "confirmed" and evidence.get("independent_reproduction", {}).get("status") != "pass":
        raise LogicAnalysisError("缺独立复现时不得 confirmed")
    bundle["logic_chain"] = rebuilt_chain
    bundle["evidence_packet"]["status"] = status
    return bundle


def _digest(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _binding_refs(checked: dict) -> dict[str, Any]:
    evidence = checked["evidence_packet"]
    oracle = checked["oracle_spec"]
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_hash": _digest(checked),
        "intent_id": checked["intent_hypothesis"]["intent_id"],
        "intent_context_hash": checked["intent_hypothesis"]["evidence_hash"],
        "logic_chain_ref": checked["logic_chain"]["chain_id"],
        "logic_chain_status": checked["logic_chain"].get("status", "hypothesized"),
        "oracle_ref": oracle["oracle_id"],
        "oracle_hash": _digest(oracle),
        "target_fingerprint": evidence["target_fingerprint"],
        "knowledge_context_hash": evidence["knowledge_context_hash"],
        "evidence_status": evidence["status"],
        "artifact_refs": copy.deepcopy(evidence["artifact_refs"]),
    }


def bind_task(task: dict, bundle: dict) -> dict:
    """将分析契约绑定到任务，保留任务原字段，不改变调度状态。"""
    if not isinstance(task, dict):
        raise LogicAnalysisError("task 必须是对象")
    checked = validate_bundle(copy.deepcopy(bundle))
    result = copy.deepcopy(task)
    result["logic_analysis"] = _binding_refs(checked)
    result["logic_analysis"]["finding_confidence"] = "low"
    return result


def bind_finding(finding: dict, bundle: dict) -> dict:
    """绑定分析证据到 Finding，但不伪造或迁移其 confirmation 状态。

    调用方随后必须把返回值交给既有 ``finding_confirmation.gate``；本函数
    只提供可核验的引用、hash 和上下文，不负责最终结论。
    """
    if not isinstance(finding, dict):
        raise LogicAnalysisError("finding 必须是对象")
    checked = validate_bundle(copy.deepcopy(bundle))
    result = copy.deepcopy(finding)
    result["logic_analysis"] = _binding_refs(checked)
    result["logic_analysis"]["finding_confidence"] = "low"
    return result


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LogicAnalysisError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LogicAnalysisError(f"{path} 根节点必须是对象")
    return value


def _output_path(path: Path) -> Path:
    """Return a repository-local output path and reject traversal."""
    if not path.is_absolute() and ".." in path.parts:
        raise LogicAnalysisError("--output 必须是当前仓库内的相对路径")
    root = Path.cwd().resolve()
    target = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise LogicAnalysisError("--output 越过仓库根目录") from exc
    return target


def _write_json(path: Path, value: dict) -> None:
    """Atomically publish a generated task binding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{id(value)}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "bind", "bind-finding"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--finding", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        root = Path.cwd()
        bundle = validate_bundle(_load_json(args.input), root)
        if args.command == "validate":
            print(json.dumps({
                "ok": True, "status": bundle["evidence_packet"]["status"],
            }, ensure_ascii=False))
            return 0
        if args.command == "bind":
            if args.task is None:
                raise LogicAnalysisError("bind 必须提供 --task")
            bound = bind_task(_load_json(args.task), bundle)
        else:
            if args.finding is None:
                raise LogicAnalysisError("bind-finding 必须提供 --finding")
            bound = bind_finding(_load_json(args.finding), bundle)
        if args.output is None:
            print(json.dumps(bound, ensure_ascii=False, indent=2))
        else:
            target = _output_path(args.output)
            _write_json(target, bound)
            print(json.dumps({"ok": True, "output": target.relative_to(root).as_posix()}, ensure_ascii=False))
        return 0
    except LogicAnalysisError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
