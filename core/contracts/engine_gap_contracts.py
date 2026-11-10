#!/usr/bin/env python3
"""DOC-114 的小型运行契约：证据、候选去重、harness、oracle 与测试资产。

只使用标准库。这里不启动服务、不执行命令；它只生成/校验受控元数据，真正的
执行仍由已有 Supervisor 和 test_capability_verifier 负责。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
MAX_CANDIDATES = 50
MAX_ORACLE_RESULTS = 5
MAX_TEST_ASSETS = 20
MAX_HITS = 40
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_ORACLE_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


class ContractError(ValueError):
    """DOC-114 输入契约错误。"""


def _state_root(root: Path) -> Path:
    try:
        import core.bugx_paths as bugx_paths

        return bugx_paths.require(root).state_root
    except Exception:
        return root / ".bugx" / "state"


def _bounded_text(value: object, field: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ContractError(f"{field} 必须是非空字符串")
    text = " ".join(value.split())
    if len(text) > limit:
        raise ContractError(f"{field} 超过 {limit} 字符")
    return text


def _relative_path(value: object, field: str) -> str:
    text = _bounded_text(value, field, 1024).replace("\\", "/")
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{field} 必须是仓库内相对路径")
    return path.as_posix()


def validate_evidence_bundle(
    bundle: Mapping[str, object], *, root: Path | None = None,
) -> dict[str, object]:
    """校验 versioned evidence bundle；recipe 是适配器元数据，不是命令。"""
    if not isinstance(bundle, Mapping):
        raise ContractError("evidence_bundle 必须是对象")
    allowed_bundle = {"schema_version", "candidate_id", "target_fingerprint", "status", "artifacts", "recipe"}
    unknown_bundle = sorted(set(bundle) - allowed_bundle)
    if unknown_bundle:
        raise ContractError(f"evidence_bundle 含未知字段: {unknown_bundle}")
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("evidence_bundle schema_version 不支持")
    required = {"candidate_id", "target_fingerprint", "status", "artifacts"}
    missing = sorted(required - set(bundle))
    if missing:
        raise ContractError(f"evidence_bundle 缺字段: {missing}")
    result = dict(bundle)
    result["candidate_id"] = _bounded_text(bundle["candidate_id"], "candidate_id", 256)
    result["target_fingerprint"] = _bounded_text(
        bundle["target_fingerprint"], "target_fingerprint", 256,
    )
    if bundle["status"] not in {"candidate", "ready_for_confirmation", "rejected", "unavailable"}:
        raise ContractError("evidence_bundle.status 无效")
    artifacts = bundle["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > MAX_CANDIDATES:
        raise ContractError("evidence_bundle.artifacts 必须是有界数组")
    normalized_artifacts: list[dict[str, object]] = []
    for index, raw in enumerate(artifacts):
        if not isinstance(raw, Mapping):
            raise ContractError(f"artifacts[{index}] 必须是对象")
        unknown_artifact = sorted(set(raw) - {"path", "sha256"})
        if unknown_artifact:
            raise ContractError(f"artifacts[{index}] 含未知字段: {unknown_artifact}")
        item = dict(raw)
        item["path"] = _relative_path(item.get("path"), f"artifacts[{index}].path")
        if root is not None and not (root / str(item["path"])).resolve(strict=False).is_relative_to(root.resolve()):
            raise ContractError(f"artifacts[{index}] 越过项目根目录")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ContractError(f"artifacts[{index}].sha256 无效")
        normalized_artifacts.append(item)
    result["artifacts"] = normalized_artifacts
    recipe = bundle.get("recipe")
    if recipe is not None:
        if not isinstance(recipe, Mapping):
            raise ContractError("recipe 必须是注册适配器对象")
        unknown_recipe = sorted(set(recipe) - {"adapter", "params"})
        if unknown_recipe:
            raise ContractError(f"recipe 含未知字段: {unknown_recipe}")
        adapter = _bounded_text(recipe.get("adapter"), "recipe.adapter", 96)
        if not _SAFE_NAME.fullmatch(adapter):
            raise ContractError("recipe.adapter 含非法字符")
        params = recipe.get("params", {})
        if not isinstance(params, Mapping) or len(params) > 20:
            raise ContractError("recipe.params 必须是有界对象")
        result["recipe"] = {"adapter": adapter, "params": dict(params)}
    return result


def _candidate_key(item: Mapping[str, object]) -> str:
    evidence = item.get("evidence")
    path = ""
    line = ""
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], Mapping):
        path = str(evidence[0].get("path") or evidence[0].get("file") or "")
        line = str(evidence[0].get("line") or "")
    root_cause = str(item.get("root_cause") or item.get("trigger_condition") or "")
    title = str(item.get("title") or item.get("description") or "")
    normalized = "|".join((path, line, root_cause, title)).casefold()
    normalized = re.sub(r"[^\w|:/.-]+", " ", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def mark_semantic_duplicates(
    findings: Iterable[Mapping[str, object]], *, round_no: int,
) -> list[dict[str, object]]:
    """保留全部候选，只标记同轮保守重复，不影响最终确认。"""
    if isinstance(round_no, bool) or round_no < 1:
        raise ContractError("round_no 必须是正整数")
    result: list[dict[str, object]] = []
    primary: dict[str, str] = {}
    for item in list(findings)[:MAX_CANDIDATES]:
        if not isinstance(item, Mapping):
            raise ContractError("finding 必须是对象")
        copy = dict(item)
        key = _candidate_key(copy)
        copy.setdefault("candidate_key", key)
        copy.setdefault("candidate_round", round_no)
        if key in primary:
            copy["suspected_duplicate_of"] = primary[key]
            copy["dedup_status"] = "suspected_duplicate"
        else:
            primary[key] = str(copy.get("id") or copy.get("fingerprint") or key)
            copy["dedup_status"] = "primary"
        result.append(copy)
    return result


def harness_snapshot(root: Path) -> dict[str, object]:
    """返回项目会话内受控 harness 注册表；不存在时明确 static-only。"""
    path = _state_root(root) / "harness" / "registry.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    entries = data.get("entries", []) if isinstance(data, Mapping) else []
    safe = [dict(item) for item in entries[:8] if isinstance(item, Mapping)]
    return {"schema_version": SCHEMA_VERSION, "status": "ready" if safe else "static-only", "entries": safe}


def ensure_harness_registry(root: Path) -> dict[str, object]:
    """登记内置静态适配器；不安装依赖、不执行任意 worker 命令。"""
    path = _state_root(root) / "harness" / "registry.json"
    payload = harness_snapshot(root)
    if payload["status"] == "ready":
        return payload
    entry = {
        "name": "builtin-static",
        "version": "1",
        "adapter_hash": hashlib.sha256(b"builtin-static-v1").hexdigest(),
        "allowed_root": str(root.resolve()),
        "timeout_seconds": 30,
        "status": "static-only",
    }
    payload = {"schema_version": SCHEMA_VERSION, "entries": [entry]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        return {**payload, "status": "static-only", "write_error": True}
    return {**payload, "status": "ready"}


def discover_test_assets(root: Path, unit: str = "") -> list[dict[str, object]]:
    """发现现有测试资产，只返回文件元数据，不把命令当成确认依据。"""
    roots = [root / name for name in ("tests", "test", "e2e", "spec")]
    extensions = {".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".rs"}
    result: list[dict[str, object]] = []
    for base in roots:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if len(result) >= MAX_TEST_ASSETS or path.is_symlink() or not path.is_file() or path.suffix.lower() not in extensions:
                continue
            relative = path.relative_to(root).as_posix()
            if unit and not (unit.replace("\\", "/") in relative or Path(unit).name in relative):
                continue
            name = path.name.casefold()
            if path.suffix.casefold() == ".go" and name.endswith("_test.go"):
                capability = "go-unit"
            elif "e2e" in relative.casefold() or "playwright" in name:
                capability = "browser-e2e"
            elif "mutat" in name or "verify" in name:
                capability = "mutation-oracle"
            else:
                capability = "unit"
            result.append({
                "path": relative, "kind": "test", "status": "available",
                "capability": capability,
            })
    return result


def extract_oracle_candidates(
    root: Path, unit: str = "", *, limit: int = MAX_TEST_ASSETS,
) -> list[dict[str, object]]:
    """Extract bounded repository-contract hints from existing test assets.

    This is intentionally lexical: it supplies advisory candidates and never
    treats a test filename or assertion as confirmation evidence.
    """
    candidates: list[dict[str, object]] = []
    for asset in discover_test_assets(root, unit)[:max(1, min(limit, MAX_TEST_ASSETS))]:
        path = root / str(asset["path"])
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:120_000].casefold()
        except OSError:
            continue
        tags: list[tuple[str, str, str]] = [
            ("auth", "contract", "repository test exercises an authentication boundary"),
            ("permission", "contract", "repository test exercises an authorization boundary"),
            ("tenant", "invariant", "repository test exercises tenant isolation"),
            ("security", "test", "repository security test is an independent oracle candidate"),
            ("assert", "test", "repository test contains an executable assertion"),
        ]
        for needle, tier, summary in tags:
            if needle not in text:
                continue
            source = str(asset["path"])
            oracle_id = hashlib.sha256(f"{source}|{needle}".encode()).hexdigest()[:24]
            candidates.append({
                "oracle_id": f"repo-{oracle_id}",
                "summary": summary,
                "tier": tier,
                "source": source,
                "provenance": "repository-contract",
                "base_revision": _git_revision(root),
                "extractor_version": "builtin-v1",
                "confidence": "low" if tier == "test" else "medium",
                "advisory": True,
            })
            if len(candidates) >= MAX_ORACLE_RESULTS * 4:
                return candidates
    return candidates


def _git_revision(root: Path) -> str:
    """Return a bounded revision marker without making git a hard dependency."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=3,
        )
        revision = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
            return revision
    except (OSError, subprocess.SubprocessError):
        pass
    return "unavailable"


def record_oracle(
    root: Path, oracle: Mapping[str, object], *, source: str | None = None,
) -> dict[str, object]:
    """Append one advisory oracle candidate idempotently to the project index."""
    if not isinstance(oracle, Mapping):
        raise ContractError("oracle 必须是对象")
    oracle_id = oracle.get("oracle_id")
    if not isinstance(oracle_id, str) or not _ORACLE_ID.fullmatch(oracle_id):
        raise ContractError("oracle_id 无效")
    summary = _bounded_text(oracle.get("summary"), "oracle.summary", 512)
    payload = {
        "oracle_id": oracle_id,
        "summary": summary,
        "tier": str(oracle.get("tier") or "heuristic")[:32],
        "source": source or str(oracle.get("source") or "repository-contract")[:1024],
        "base_revision": str(oracle.get("base_revision") or _git_revision(root))[:64],
        "extractor_version": str(oracle.get("extractor_version") or "builtin-v1")[:64],
        "confidence": str(oracle.get("confidence") or "low")[:16],
        "advisory": True,
    }
    path = _state_root(root) / "oracle-library.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: set[object] = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines()[-500:]:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, Mapping):
                    existing.add(item.get("oracle_id"))
        if oracle_id not in existing:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"oracle-library 写入失败: {exc}") from exc
    return payload


def static_hit_contract(raw: Iterable[Mapping[str, object]], *, tool: str, status: str = "hit") -> dict[str, object]:
    if status not in {"hit", "timeout", "unsupported", "degraded", "no-hit"}:
        raise ContractError("static scan status 无效")
    hits: list[dict[str, object]] = []
    for item in list(raw)[:MAX_HITS]:
        if not isinstance(item, Mapping):
            continue
        path = item.get("file") or item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        try:
            line = max(1, int(item.get("line") or 1))
        except (TypeError, ValueError):
            line = 1
        hits.append({
            "rule_id": str(item.get("rule_id") or item.get("pattern_id") or "unknown")[:96],
            "rule_version": str(item.get("rule_version") or f"{tool}-v1")[:64],
            "path": path.replace("\\", "/")[:1024],
            "line": line,
            "category": str(item.get("category") or "generic")[:96],
            "severity": str(item.get("severity") or "medium").lower()[:16],
            "status": status,
            "tool": tool,
        })
    return {"schema_version": SCHEMA_VERSION, "tool": tool, "status": status, "hits": hits}


def oracle_suggest(root: Path, query: str, *, limit: int = MAX_ORACLE_RESULTS) -> list[dict[str, object]]:
    path = _state_root(root) / "oracle-library.jsonl"
    needle = str(query or "").casefold()
    result: list[dict[str, object]] = []
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return result
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, Mapping):
            continue
        haystack = json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
        if needle and needle not in haystack:
            continue
        result.append({
            "oracle_id": item.get("oracle_id"),
            "summary": str(item.get("summary") or "")[:240],
            "tier": item.get("tier", "heuristic"),
            "source": item.get("source"),
            "provenance": item.get("source"),
            "base_revision": item.get("base_revision"),
            "extractor_version": item.get("extractor_version", "builtin-v1"),
            "confidence": item.get("confidence", "low"),
            "advisory": True,
        })
        if len(result) >= max(1, min(limit, MAX_ORACLE_RESULTS)):
            break
    return result


__all__ = [
    "ContractError", "discover_test_assets", "ensure_harness_registry",
    "extract_oracle_candidates", "harness_snapshot", "mark_semantic_duplicates",
    "oracle_suggest", "record_oracle",
    "static_hit_contract", "validate_evidence_bundle",
]
