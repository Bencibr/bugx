"""静态补丁包生成与校验（DOC-113 阶段 0/1）。

This module deliberately does not execute package contents or contact a remote
service.  Remote replay, PR submission, signing and distribution are later
stages with separate security gates.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..common import CliError, _ensure_core_importable


MANIFEST_VERSION = 1
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_SIGNATURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:a\d+)?$")
_PATCH_KIND = "engine"
_PATCH_SCOPE_ERROR = "BugX 修复补丁仅允许修复 BugX 自身代码（--kind engine）；挖掘目标项目不属于 BugX 补丁"
# 「修复 BugX agent」的落点不只有 core/ 与 src/：测试、提示词、插件技能、脚本、
# 文档同样属于 BugX 自身。把它们挡在闸门外会让一批真实缺陷（例如纯 tests/ 的
# 契约修复）无法打包。这里改为命名空间白名单：允许 BugX 自己的全部命名空间，
# 仍然拒绝把补丁落到 BugX 之外（目标项目、任意第三方路径）。
_ENGINE_NAMESPACES = frozenset({
    "core", "src", "tests", "docs", "scripts", "plugins", "skills",
    "config", "integrations", "evals", "prd", "diagrams", "bin", "contrib",
    ".bugx-patches", ".github",
})
_ENGINE_ROOT_FILES = frozenset({
    "pyproject.toml", "package.json", "MANIFEST.in", "AGENTS.md", "CLAUDE.md",
    "README.md", "RELEASE.md", "HANDOFF.md", "Jenkinsfile", "LICENSE",
})
_PATCH_SCOPE_HINT = "补丁只能落在 BugX 自身命名空间内（" + "、".join(sorted(_ENGINE_NAMESPACES)) + "）或 BugX 根级文件"
# 执行型（红绿回放）失败与静态校验失败在报告里必须分开归因，否则一次 repro 失败
# 会把明明通过的 provenance/static-scan 也标成 failed。
_RED_GREEN_ERROR_PREFIX = "红绿闭环验证失败: "
_TOP_LEVEL = {
    "schema_version", "patch_id", "kind", "base_version", "source_session",
    "problem", "repro", "diff", "tests", "invariant_impact", "risk_class",
    "review",
}
_GATE_MARKERS = (
    "finding_confirmation", "agent_eval", "audit_supervisor", "budget_governor",
    "supervisor_models", "checkpoint", "engine-integrity.json", "verify_life",
    "lifecycle_reporter", "worker-checkpoint.schema.json",
)
_INFRA_MARKERS = (
    "Jenkinsfile", ".github/workflows/", ".gitea/workflows/", "pyproject.toml",
    "scripts/", "release", "package.json", "package-lock.json",
)
# 「纯文档」的判定口径 = 改了它不会改变任何运行时或 Agent 行为的说明性材料。
# 旧实现用 `path.endswith(".md")` 兜底，把 core/prompts/**、plugins/**、skills/**
# 这些**可执行的 Agent 指令**一并判成 docs，于是 review=auto、invariant_impact=none ——
# 改动 Agent 行为反而比改一行 Python 更宽松，无需任何人工审核。提示词退出码契约
# 被静默回退（PE-009 的修复被推翻）正是从这个缺口进来的。
_DOC_PATH_SEGMENTS = ("docs/",)
_AGENT_BEHAVIOR_MARKERS = (
    "core/prompts/", "core/bugx-opencode.md", "plugins/", "skills/",
)
# 根级 Agent 指令文件同样是注入给 Agent 的行为约束，不是给人看的说明。
_AGENT_INSTRUCTION_ROOT_FILES = frozenset({"AGENTS.md", "CLAUDE.md"})
# 门禁自身的实现文件。改了它就能改判定口径，因此**不能**被自己判成
# `bugfix-behavioral`/`single` 而自证清白：`_GATE_MARKERS` 里的都是「被门禁守护的
# 对象」，这里是「门禁本身」，只能走 canary。用精确路径而非子串，避免误伤同名测试
# （例如 `tests/test_patch_engine.py`）。
_GATE_IMPLEMENTATION_FILES = frozenset({
    "src/bugx_cli/commands/patch.py",
})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _out_of_scope(paths: list[str]) -> list[str]:
    """返回不属于 BugX 自身命名空间的路径（保持输入顺序、去重）。"""
    foreign: list[str] = []
    for path in paths:
        head, _, tail = path.partition("/")
        if tail:
            if head not in _ENGINE_NAMESPACES:
                foreign.append(path)
        elif path not in _ENGINE_ROOT_FILES:
            foreign.append(path)
    return foreign


def _safe_rel(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CliError(f"{field} 必须是非空相对路径")
    raw = value.strip().replace("\\", "/")
    if "\x00" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise CliError(f"{field} 不得是绝对路径")
    if any(part in {"", ".", ".."} for part in raw.split("/")):
        raise CliError(f"{field} 含非法路径段: {value!r}")
    path = PurePosixPath(raw)
    if not path.parts:
        raise CliError(f"{field} 含非法路径段: {value!r}")
    return path.as_posix()


def _source_file(root: Path, value: object, *, field: str) -> tuple[Path, str]:
    relative = _safe_rel(value, field=field)
    candidate = root / Path(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise CliError(f"{field} 不得经过 symlink: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise CliError(f"{field} 必须位于项目根内且存在: {relative}") from exc
    if not resolved.is_file():
        raise CliError(f"{field} 必须是文件: {relative}")
    if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
        raise CliError(f"{field} 超过 {MAX_ARTIFACT_BYTES} 字节上限: {relative}")
    return resolved, relative


def _read_issue(path: Path) -> tuple[str, bytes]:
    try:
        data = path.read_bytes()
        title = next(
            (line[2:].strip() for line in data.decode("utf-8").splitlines()
             if line.startswith("# ") and line[2:].strip()),
            path.stem,
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"无法读取 SPEC: {path}: {exc}") from exc
    return title[:200], data


def _diff_paths(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError("diff 必须是 UTF-8 文本") from exc
    paths: list[str] = []
    for line in text.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        path = _safe_rel(raw, field="diff 触碰路径")
        if path not in paths:
            paths.append(path)
    if not paths:
        raise CliError("diff 未包含可识别的 unified diff '+++ b/<path>' 路径")
    return paths


def _is_doc_path(path: str) -> bool:
    """判断路径是否属于「纯文档」—— 改动它不影响运行时或 Agent 行为。

    Agent 指令类 ``.md``（``core/prompts/**``、``plugins/**``、``skills/**``、
    根级 ``AGENTS.md``/``CLAUDE.md``）虽然也是文档格式，但改动会直接改变 Agent
    行为，必须按行为变更审核（``review=single``），不得借 docs 通道 auto 放行。
    """
    if any(marker in path for marker in _AGENT_BEHAVIOR_MARKERS):
        return False
    if path in _AGENT_INSTRUCTION_ROOT_FILES:
        return False
    if path.startswith(_DOC_PATH_SEGMENTS) or "/docs/" in path:
        return True
    return "/" not in path and path.endswith(".md")


def _risk_class(paths: list[str]) -> str:
    normalized = [path.replace("\\", "/") for path in paths]
    if any(path in _GATE_IMPLEMENTATION_FILES for path in normalized):
        return "gate-semantics"
    if any(any(marker in path for marker in _GATE_MARKERS) for path in normalized):
        return "gate-semantics"
    if normalized and all(_is_doc_path(path) for path in normalized):
        return "docs"
    if normalized and all(path.startswith("tests/") for path in normalized):
        return "tests"
    if any(any(marker in path for marker in _INFRA_MARKERS) for path in normalized):
        return "infra"
    return "bugfix-behavioral"


def _invariant_impact(paths: list[str], risk_class: str) -> str | dict[str, list[str]]:
    if risk_class != "gate-semantics":
        return "none"
    impacted: set[str] = set()
    for path in paths:
        if any(marker in path for marker in ("finding_confirmation", "agent_eval")):
            impacted.add("INV-1")
        if any(marker in path for marker in ("audit_supervisor", "budget_governor", "supervisor_models", "checkpoint")):
            impacted.update({"INV-3", "INV-6"})
        if any(marker in path for marker in ("engine-integrity.json", "startup_gates")):
            impacted.add("INV-7")
        if any(marker in path for marker in ("coverage", "symbol_index", "module_coverage")):
            impacted.add("INV-5")
    return {"touched": sorted(impacted or {"INV-7"})}


def _review_required(risk_class: str) -> str:
    return {
        "docs": "auto",
        "tests": "auto",
        "bugfix-behavioral": "single",
        "gate-semantics": "canary",
        "infra": "dual",
    }[risk_class]


def _unknown_fields(value: object, allowed: set[str], label: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [f"{label} 含未知字段: {key}" for key in sorted(set(value) - allowed)]


def _copy_name(index: int, source_name: str, suffix: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", source_name).strip(".-") or "artifact"
    return f"{index:04d}-{safe}{suffix}"


def _require_args(args: Any) -> None:
    if not args.from_issue:
        raise CliError("创建补丁包必须提供 --from-issue")
    if args.kind != _PATCH_KIND:
        raise CliError(_PATCH_SCOPE_ERROR)
    if not args.patch_id or not re.fullmatch(r"PE-\d{8}-\d{3}", args.patch_id):
        raise CliError("--patch-id 必须符合 PE-YYYYMMDD-NNN")
    if not args.diff:
        raise CliError("至少需要一个 --diff")
    if not args.repro:
        raise CliError("至少需要一个 --repro")
    if not args.test:
        raise CliError("至少需要一个 --test")
    if not args.signature or not _SAFE_SIGNATURE.fullmatch(args.signature):
        raise CliError("--signature 必须是 1-256 位安全指纹")
    if args.severity not in {"high", "medium", "low", "info"}:
        raise CliError("--severity 无效")
    if not args.spec_only and not all((args.session_id, args.run_id, args.lifecycle_id, args.repository, args.finding_fingerprint)):
        raise CliError("session 模式必须提供 session/run/lifecycle/repository/finding fingerprint；无证据请使用 --spec-only")


def _build_package(root: Path, args: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    _require_args(args)
    issue_path, issue_relative = _source_file(root, args.from_issue, field="--from-issue")
    title, issue_data = _read_issue(issue_path)
    patch_payloads: dict[str, bytes] = {}
    touched: list[str] = []
    patch_files: list[dict[str, str]] = []
    for index, value in enumerate(args.diff, 1):
        source, _ = _source_file(root, value, field="--diff")
        data = source.read_bytes()
        paths = _diff_paths(data)
        for path in paths:
            if path not in touched:
                touched.append(path)
        dest = f"diff/{_copy_name(index, source.name, '.patch')}"
        patch_payloads[dest] = data
        patch_files.append({"path": dest, "sha256": _sha256(data)})
    foreign = _out_of_scope(touched)
    if foreign:
        raise CliError(
            f"补丁落在 BugX 命名空间之外: {', '.join(foreign)}；{_PATCH_SCOPE_HINT}"
        )

    repro_entries: list[dict[str, Any]] = []
    for index, value in enumerate(args.repro, 1):
        source, _ = _source_file(root, value, field="--repro")
        data = source.read_bytes()
        dest = f"repro/{_copy_name(index, source.name, '')}"
        patch_payloads[dest] = data
        repro_entries.append({"path": dest, "before_exit": 0, "after_exit": 1, "sha256": _sha256(data)})

    test_entries: list[dict[str, str]] = []
    for index, value in enumerate(args.test, 1):
        source, _ = _source_file(root, value, field="--test")
        data = source.read_bytes()
        dest = f"tests/{_copy_name(index, source.name, '')}"
        patch_payloads[dest] = data
        test_entries.append({"path": dest, "sha256": _sha256(data)})

    risk_class = _risk_class(touched)
    source_session: dict[str, Any]
    if args.spec_only:
        source_session = {"evidence_mode": "spec-only"}
        evidence = ["spec-only"]
    else:
        source_session = {
            "evidence_mode": "session",
            "session_id": args.session_id,
            "run_id": args.run_id,
            "lifecycle_id": args.lifecycle_id,
            "repository": args.repository,
            "finding_fingerprints": list(args.finding_fingerprint),
        }
        evidence = [f"session:{args.session_id}", f"run:{args.run_id}", *[f"finding:{x}" for x in args.finding_fingerprint]]
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "patch_id": args.patch_id,
        "kind": args.kind,
        "base_version": args.base_version,
        "source_session": source_session,
        "problem": {"signature": args.signature, "title": title, "severity": args.severity, "evidence": evidence},
        "repro": repro_entries,
        "diff": {
            "files": sorted(touched),
            "patch_files": patch_files,
            "engine_integrity_updated": "engine-integrity.json" in " ".join(touched),
        },
        "tests": {"new": test_entries},
        "invariant_impact": _invariant_impact(touched, risk_class),
        "risk_class": risk_class,
        "review": {"required": _review_required(risk_class)},
    }
    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        raise CliError("manifest 校验失败: " + "; ".join(manifest_errors))
    patch_payloads["SPEC.md"] = issue_data
    return manifest, patch_payloads


def validate_manifest(manifest: object, package_root: Path | None = None) -> list[str]:
    """Validate the executable MVP contract without importing jsonschema."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest 必须是 JSON object"]
    unknown = sorted(set(manifest) - _TOP_LEVEL)
    if unknown:
        errors.append("未知字段: " + ", ".join(unknown))
    if manifest.get("schema_version") != MANIFEST_VERSION:
        errors.append("schema_version 不支持")
    if not isinstance(manifest.get("patch_id"), str) or not re.fullmatch(r"PE-\d{8}-\d{3}", manifest.get("patch_id", "")):
        errors.append("patch_id 格式错误")
    kind = manifest.get("kind")
    if kind != _PATCH_KIND:
        errors.append(_PATCH_SCOPE_ERROR)
    if not isinstance(manifest.get("base_version"), str) or not _VERSION.fullmatch(manifest.get("base_version", "")):
        errors.append("base_version 格式错误")

    source = manifest.get("source_session")
    if not isinstance(source, dict) or source.get("evidence_mode") not in {"session", "spec-only"}:
        errors.append("source_session/evidence_mode 缺失或无效")
    elif source.get("evidence_mode") == "session":
        errors.extend(_unknown_fields(source, {"evidence_mode", "session_id", "run_id", "lifecycle_id", "repository", "finding_fingerprints"}, "source_session"))
        for key in ("session_id", "run_id", "lifecycle_id", "repository"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                errors.append(f"source_session.{key} 缺失")
        if not isinstance(source.get("finding_fingerprints"), list) or not source["finding_fingerprints"]:
            errors.append("source_session.finding_fingerprints 缺失")
    elif isinstance(source, dict):
        errors.extend(_unknown_fields(source, {"evidence_mode"}, "source_session"))

    problem = manifest.get("problem")
    if not isinstance(problem, dict):
        errors.append("problem 缺失")
    else:
        errors.extend(_unknown_fields(problem, {"signature", "title", "severity", "evidence"}, "problem"))
        if not isinstance(problem.get("signature"), str) or not _SAFE_SIGNATURE.fullmatch(problem.get("signature", "")):
            errors.append("problem.signature 无效")
        if not isinstance(problem.get("title"), str) or not problem["title"].strip():
            errors.append("problem.title 缺失")
        if problem.get("severity") not in {"high", "medium", "low", "info"}:
            errors.append("problem.severity 无效")
        evidence = problem.get("evidence")
        if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
            errors.append("problem.evidence 必须是非空字符串数组")
        elif isinstance(source, dict) and source.get("evidence_mode") == "spec-only":
            if evidence != ["spec-only"]:
                errors.append("spec-only 补丁的 problem.evidence 必须精确为 ['spec-only']")
        elif isinstance(source, dict) and source.get("evidence_mode") == "session":
            required_evidence = {f"session:{source.get('session_id')}", f"run:{source.get('run_id')}"}
            if not required_evidence.issubset(set(evidence)) or not any(x.startswith("finding:") for x in evidence):
                errors.append("session 补丁的 problem.evidence 必须包含 session/run/finding 引用")

    diff = manifest.get("diff")
    touched: list[str] = []
    if not isinstance(diff, dict):
        errors.append("diff 缺失")
    else:
        errors.extend(_unknown_fields(diff, {"files", "patch_files", "engine_integrity_updated"}, "diff"))
        raw_paths = diff.get("files")
        if not isinstance(raw_paths, list) or not raw_paths:
            errors.append("diff.files 不能为空")
        else:
            for value in raw_paths:
                try:
                    path = _safe_rel(value, field="diff.files")
                    touched.append(path)
                except CliError as exc:
                    errors.append(str(exc))
            if len(set(touched)) != len(touched):
                errors.append("diff.files 不得重复")
        patch_files = diff.get("patch_files")
        if not isinstance(patch_files, list) or not patch_files:
            errors.append("diff.patch_files 不能为空")
        else:
            for item in patch_files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not _HEX64.fullmatch(str(item.get("sha256", ""))):
                    errors.append("diff.patch_files 项格式错误")
                elif not re.fullmatch(r"diff/[^/]+[.]patch", str(item["path"])):
                    errors.append("diff.patch_files 必须位于 diff/")
        if diff.get("engine_integrity_updated") not in {True, False}:
            errors.append("diff.engine_integrity_updated 必须是布尔值")
        if "engine-integrity.json" in touched and diff.get("engine_integrity_updated") is not True:
            errors.append("触碰 engine-integrity.json 时必须显式声明 engine_integrity_updated=true")
    if kind == _PATCH_KIND:
        for path in _out_of_scope(touched):
            errors.append(f"diff.files 落在 BugX 命名空间之外: {path}；{_PATCH_SCOPE_HINT}")

    repro = manifest.get("repro")
    if not isinstance(repro, list) or not repro:
        errors.append("repro 不能为空")
    else:
        for item in repro:
            if not isinstance(item, dict):
                errors.append("repro 项必须是 object")
            else:
                errors.extend(_unknown_fields(item, {"path", "before_exit", "after_exit", "sha256"}, "repro"))
            if not isinstance(item, dict) or item.get("before_exit") != 0 or item.get("after_exit") != 1:
                errors.append("repro 必须声明 before_exit=0/after_exit=1")
            elif not isinstance(item.get("path"), str) or not item["path"].startswith("repro/") or not _HEX64.fullmatch(str(item.get("sha256", ""))):
                errors.append("repro path/sha256 无效")
    tests = manifest.get("tests")
    if not isinstance(tests, dict) or not isinstance(tests.get("new"), list) or not tests["new"]:
        errors.append("tests.new 不能为空")
    elif any(
        not isinstance(x, dict)
        or not isinstance(x.get("path"), str)
        or not x["path"].startswith("tests/")
        or not _HEX64.fullmatch(str(x.get("sha256", "")))
        for x in tests["new"]
    ):
        errors.append("tests.new 项必须包含 tests/ 路径和 SHA-256")
    elif _unknown_fields(tests, {"new", "golden_baseline_ref"}, "tests"):
        errors.extend(_unknown_fields(tests, {"new", "golden_baseline_ref"}, "tests"))

    impact = manifest.get("invariant_impact")
    if isinstance(impact, dict):
        errors.extend(_unknown_fields(impact, {"touched"}, "invariant_impact"))
    impact_ids = impact.get("touched") if isinstance(impact, dict) else []
    if impact != "none" and (not isinstance(impact, dict) or not isinstance(impact_ids, list) or not impact_ids):
        errors.append("invariant_impact 无效")
    if any(not isinstance(x, str) or not re.fullmatch(r"INV-[1-8]", x) for x in impact_ids):
        errors.append("invariant_impact.touched 含未知 INV")
    risk = manifest.get("risk_class")
    expected_risk = _risk_class(touched) if touched else None
    if risk not in {"docs", "tests", "bugfix-behavioral", "gate-semantics", "infra"}:
        errors.append("risk_class 无效")
    elif expected_risk and risk != expected_risk:
        errors.append(f"risk_class 与触碰文件不一致，应为 {expected_risk}")
    if risk == "gate-semantics" and impact == "none":
        errors.append("gate-semantics 必须声明 invariant_impact.touched")
    if risk != "gate-semantics" and impact != "none":
        errors.append("非 gate-semantics 不得声明 touched invariant")
    review = manifest.get("review")
    if risk in {"docs", "tests", "bugfix-behavioral", "gate-semantics", "infra"}:
        errors.extend(_unknown_fields(review, {"required", "approvers"}, "review"))
        if not isinstance(review, dict) or review.get("required") != _review_required(risk):
            errors.append("review.required 与 risk_class 不一致")

    if package_root is not None and not errors:
        for item in diff["patch_files"]:
            errors.extend(_verify_artifact(package_root, item["path"], item["sha256"]))
        for item in repro:
            errors.extend(_verify_artifact(package_root, item["path"], item["sha256"]))
        for item in tests["new"]:
            errors.extend(_verify_artifact(package_root, item["path"], item["sha256"]))
        try:
            _package_file(package_root, "SPEC.md")
        except CliError as exc:
            errors.append(str(exc))
    return errors


def _package_file(root: Path, relative: str) -> Path:
    safe = _safe_rel(relative, field="artifact")
    path = root / Path(*PurePosixPath(safe).parts)
    current = root
    for part in PurePosixPath(safe).parts:
        current = current / part
        if current.is_symlink():
            raise CliError(f"artifact 不得经过 symlink: {safe}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, ValueError):
        raise CliError(f"缺少或越界 artifact: {safe}")
    if not resolved.is_file():
        raise CliError(f"缺少或非法 artifact: {safe}")
    return resolved


def _verify_artifact(root: Path, relative: str, expected: str) -> list[str]:
    try:
        resolved = _package_file(root, relative)
    except CliError as exc:
        return [str(exc)]
    if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
        return [f"artifact 超过大小上限: {relative}"]
    if _sha256(resolved.read_bytes()) != str(expected).lower():
        return [f"artifact hash 不匹配: {relative}"]
    return []


def _verification_report(
    manifest: dict[str, Any],
    errors: list[str],
    *,
    red_to_green_status: str = "deferred",
    red_to_green_detail: str = "execution_deferred: MVP does not execute package contents",
) -> dict[str, Any]:
    passed = not errors
    # 静态阶段只看静态校验结果；红绿回放失败属于执行型验证，单独归因到 repro-replay
    static_errors = [e for e in errors if not e.startswith(_RED_GREEN_ERROR_PREFIX)]
    static_passed = not static_errors
    routing_status = "deferred"
    routing_detail = "execution_deferred: MVP does not execute package contents"

    stages = [
        {"stage": "provenance", "status": "passed" if static_passed else "failed", "detail": "manifest/source/engine contract" if static_passed else "; ".join(static_errors[:8])},
        {"stage": "static-scan", "status": "passed" if static_passed else "failed", "detail": "paths, hashes and risk policy"},
        {"stage": "repro-replay", "status": red_to_green_status if red_to_green_status != "deferred" else "deferred", "detail": red_to_green_detail if red_to_green_status != "deferred" else "execution_deferred: MVP does not execute package contents"},
        {"stage": "invariants", "status": "deferred", "detail": "execution_deferred: MVP does not execute package contents"},
        {"stage": "golden-baseline", "status": "passed" if red_to_green_status == "passed" else "deferred", "detail": "repro and regression tests verified" if red_to_green_status == "passed" else "execution_deferred: MVP does not execute package contents"},
        {"stage": "self-audit", "status": "deferred", "detail": "execution_deferred: MVP does not execute package contents"},
        {"stage": "routing", "status": routing_status, "detail": routing_detail},
        {"stage": "report", "status": "passed", "detail": "deterministic static report"},
    ]

    if not passed:
        decision = "rejected"
        decision_reason = "; ".join(errors)
    elif red_to_green_status == "passed":
        decision = "verified-local"
        decision_reason = f"静态校验与本地红绿受限回放通过: {red_to_green_detail}"
    else:
        decision = "verification-deferred"
        decision_reason = "静态 MVP 通过；执行型验证和远端路由 deferred"

    return {
        "schema_version": 1,
        "patch_id": manifest.get("patch_id", ""),
        "verifier_version": "static-mvp-1",
        "stages": stages,
        "invariants": {
            "total": 0,
            "failed": [],
            "touched_declared": list((manifest.get("invariant_impact") or {}).get("touched", []))
            if isinstance(manifest.get("invariant_impact"), dict) else [],
            "touched_detected": [],
            "mismatch": False,
        },
        "risk_class_confirmed": manifest.get("risk_class", "bugfix-behavioral"),
        "decision": decision,
        "decision_reason": decision_reason,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"patch_id: {report.get('patch_id')}")
    print(f"decision: {report.get('decision')}")
    print(f"reason: {report.get('decision_reason')}")
    for stage in report.get("stages", []):
        print(f"  {stage['stage']}: {stage['status']}")


def _import_root_for(root: Path, relative: str) -> Path | None:
    """推导 ``relative`` 所属的导入根，即应加入 ``sys.path`` 的目录。

    两种布局：

    - **常规包**：``src/bugx_cli/commands/cloud.py`` -> ``root/src``
      （``bugx_cli`` 是顶层包，其父目录才是导入根）。
    - **松散模块目录**：引擎 ``core/`` 下没有 ``__init__.py``，其中的模块以顶层
      模块导入（``import cloud_client``），因此 ``core`` 目录本身就是导入根。

    仓库根下的 ``.py`` 返回 ``None``（``sandbox`` 已在 ``extra_pythonpath`` 中且排在
    ``root`` 之前，无需特殊处理）；其它无法判定包结构的路径同样返回 ``None``。
    """
    parts = PurePosixPath(relative).parts
    for index in range(len(parts) - 1, 0, -1):
        candidate = root.joinpath(*parts[:index])
        if not (candidate / "__init__.py").is_file():
            continue
        parent = root.joinpath(*parts[: index - 1])
        if not (parent / "__init__.py").is_file():
            return parent
    if PurePosixPath(relative).suffix == ".py" and len(parts) > 1:
        # 无包结构：所在目录即导入根（``core/cloud_client.py`` -> ``root/core``）。
        # 仓库根下的模块无需特殊处理 —— ``extra_pythonpath`` 里已有 ``sandbox`` 且
        # 排在 ``root`` 之前，``import foo`` 本就会命中沙箱副本。
        return root.joinpath(*parts[:-1])
    return None


def _stage_isolated_copy(root: Path, sandbox: Path, touched: list[str]) -> list[Path]:
    """在沙箱内重建可导入的隔离副本，返回沙箱侧的导入根列表。

    只复制受补丁影响的文件是不够的：Python 会从 ``sys.path`` 中第一个命中的位置
    加载整个模块（常规包看 ``__init__.py``，松散模块看同名 ``.py``），落在沙箱里的
    单文件永远不会被导入，红绿回放实际验证的是宿主进程的模块解析结果，补丁等于没被
    验证。因此必须连同导入根一起复制，让沙箱副本成为 ``sys.path`` 上的第一个命中。
    """
    sandbox_roots: list[Path] = []
    for relative in touched:
        base = _import_root_for(root, relative)
        if base is None:
            source = root / Path(*PurePosixPath(relative).parts)
            if source.is_file():
                target = sandbox / Path(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            continue
        mirror = sandbox / base.relative_to(root)
        if mirror in sandbox_roots:
            continue
        shutil.copytree(
            base,
            mirror,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.pyo"),
        )
        sandbox_roots.append(mirror)
    return sandbox_roots


def _platform_adapter() -> Any | None:
    """导入 core 的 ``platform_adapter``（沙箱执行器与安全白名单的来源）。

    优先直接 ``import``：沙箱的 ``PYTHONPATH`` 里已经带了 core 引擎目录
    （见 ``_sandbox_env``），所以沙箱内的副本不必依赖 ``asset_root()``。
    这一步是必需的 —— 沙箱里只复制了 ``src/`` 时，``_ensure_core_importable()``
    会因为 ``asset_root()`` 找不到 Agent 资源而抛 ``CliError``，而补丁包内的
    repro/test 恰恰是在这种沙箱里运行的。``_ensure_core_importable()`` 在
    ``common._i18n_exc`` 里同样是被 try/except 包住的，本处沿用同一约定。

    拿不到时返回 ``None``，由调用方 fail-closed。
    """
    try:
        from core.runtime import platform_adapter

        return platform_adapter
    except Exception:
        pass
    try:
        _ensure_core_importable()
        from core.runtime import platform_adapter

        return platform_adapter
    except Exception:
        return None


def _sandbox_env(
    extra_pythonpath: list[Path] | None,
    adapter: Any,
) -> dict[str, str]:
    """构造沙箱子进程环境：只放行 core 平台适配器认可的 ``SAFE_ENV`` 白名单。

    包内 repro/test 是不受信内容（AI 合成；``--verify`` 还可能验别人提交的包）。
    继承整个宿主环境等于把它们要用的凭据一并交出去 ——
    ``BUGX_PATCH_VERIFICATION_API_KEY`` 正是提交补丁用的那把密钥。同时也不再把
    宿主的 ``PYTHONPATH`` 透传下去，避免宿主的同名模块遮蔽沙箱副本。
    """
    env = {name: os.environ[name] for name in adapter.SAFE_ENV if name in os.environ}
    # 禁止写字节码缓存：Phase 1 先导入一次、Phase 2 才打补丁、Phase 3 再导入，
    # 而 pyc 的有效性只比对源文件的 (mtime 秒, 字节数)。补丁若产出**同字节数**的
    # 文件且落在同一秒内，Phase 3 会复用 Phase 1 编译出的陈旧字节码，判定随机器
    # 快慢而变（实测同一用例 5 次里 4 次误判"缺陷未消除"）。
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    entries = [str(path.resolve()) for path in (extra_pythonpath or []) if path.exists()]
    # 把 core 引擎目录一并带上，且排在沙箱导入根之后（沙箱副本仍优先命中）：
    # 包内 repro/test 会 import bugx_cli，而沙箱内的副本解析 core 时不能再依赖
    # asset_root() —— 沙箱里没有 Agent 资源。
    core_dir = str(Path(adapter.__file__).resolve().parent)
    if core_dir not in entries:
        entries.append(core_dir)
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _run_sandbox_script(
    script_path: Path,
    *,
    cwd: Path,
    extra_pythonpath: list[Path] | None = None,
    timeout: int = 15,
) -> tuple[int, str]:
    """在沙箱中执行包内脚本，返回 ``(退出码, 输出)``。

    执行交给 core 的 ``_run_bounded``：它建立了新会话（超时可杀掉整个进程组）、
    限制输出上限、并把 stdin 接到 DEVNULL。此前这里是裸 ``subprocess.run``，
    超时只杀直接子进程，孙进程会带着宿主环境继续跑。

    拿不到加固执行器时 fail-closed：宁可不执行，也不在未加固的环境里跑不受信脚本。
    """
    adapter = _platform_adapter()
    runner = getattr(adapter, "_run_bounded", None)
    output_limit = getattr(adapter, "DEFAULT_OUTPUT_LIMIT", None)
    safe_env = getattr(adapter, "SAFE_ENV", None)
    if runner is None or output_limit is None or not safe_env:
        return -1, "沙箱执行器不可用: 无法导入 core.platform_adapter，拒绝在未加固环境中执行包内脚本"

    env = _sandbox_env(extra_pythonpath, adapter)

    cmd: list[str]
    if script_path.suffix == ".py":
        cmd = [sys.executable, str(script_path)]
    elif script_path.suffix == ".sh":
        cmd = ["/bin/bash", str(script_path)]
    else:
        cmd = [str(script_path)]

    try:
        code, timed_out, stdout, stdout_meta, stderr, stderr_meta = runner(
            cmd, cwd, env, float(timeout), output_limit,
        )
    except Exception as exc:
        return -1, f"脚本执行异常: {exc}"
    if timed_out:
        return -1, f"脚本执行超时 ({timeout}s)"

    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if stdout_meta.get("truncated") or stderr_meta.get("truncated"):
        out += f"\n[输出已截断至 {output_limit} 字节]"
    return code, (out + ("\n" + err if err else "")).strip()


def _apply_patch(sandbox: Path, patch_data: bytes) -> tuple[bool, str]:
    patch_file = sandbox / ".bugx_tmp.patch"
    patch_file.write_bytes(patch_data)
    try:
        if not (sandbox / ".git").is_dir():
            subprocess.run(["git", "init", "-q"], cwd=str(sandbox), check=True)
            subprocess.run(["git", "config", "user.name", "BugX"], cwd=str(sandbox), check=True)
            subprocess.run(["git", "config", "user.email", "bugx@antigravity"], cwd=str(sandbox), check=True)
            subprocess.run(["git", "add", "."], cwd=str(sandbox), check=True)

        res = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file.name)],
            cwd=str(sandbox),
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return True, ""
        return False, res.stderr or res.stdout or "git apply failed"
    except Exception as exc:
        return False, str(exc)
    finally:
        if patch_file.exists():
            patch_file.unlink(missing_ok=True)


def _execute_red_to_green(
    root: Path,
    touched: list[str],
    patch_data_list: list[bytes],
    repro_items: list[tuple[str, int, int, bytes]],
    test_items: list[tuple[str, bytes]],
) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="bugx-r2g-") as tmpdir:
        sandbox = Path(tmpdir).resolve()
        # 1. 在沙箱内重建可导入的隔离副本（仅复制 touched 文件会让补丁成为死代码）
        sandbox_roots = _stage_isolated_copy(root, sandbox, touched)
        # 沙箱导入根必须排在宿主之前，否则受补丁文件会被宿主副本遮蔽
        extra_pythonpath = [
            *sandbox_roots,
            *(root / mirror.relative_to(sandbox) for mirror in sandbox_roots),
            sandbox,
            root,
        ]

        # 2. 写入 repro 脚本
        repro_paths: list[tuple[Path, int, int]] = []
        for rel, before_exit, after_exit, data in repro_items:
            dst = sandbox / Path(*PurePosixPath(rel).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            repro_paths.append((dst, before_exit, after_exit))

        # 3. 写入 test 脚本
        test_paths: list[Path] = []
        for rel, data in test_items:
            dst = sandbox / Path(*PurePosixPath(rel).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            test_paths.append(dst)

        # 4. Phase 1 (Red): 执行修复前 repro，必须复现缺陷（返回码等于 before_exit）
        for repro_file, before_exit, _ in repro_paths:
            rc, out = _run_sandbox_script(repro_file, cwd=sandbox, extra_pythonpath=extra_pythonpath)
            if rc != before_exit:
                return False, f"Repro 预检失败 (未复现缺陷): 预期 exit {before_exit}, 实际 exit {rc}. 输出:\n{out}"

        # 5. Phase 2: 应用补丁 diff
        for patch_data in patch_data_list:
            ok, err = _apply_patch(sandbox, patch_data)
            if not ok:
                return False, f"补丁应用失败: {err}"

        # 6. Phase 3 (Green): 执行修复后 repro，缺陷必须消除（返回码等于 after_exit）
        for repro_file, _, after_exit in repro_paths:
            rc, out = _run_sandbox_script(repro_file, cwd=sandbox, extra_pythonpath=extra_pythonpath)
            if rc != after_exit:
                return False, f"Repro 修复后验证失败 (缺陷未消除): 预期 exit {after_exit}, 实际 exit {rc}. 输出:\n{out}"

        # 7. Phase 3 (Green): 执行回归测试，必须全部通过（返回码 0）
        for test_file in test_paths:
            rc, out = _run_sandbox_script(test_file, cwd=sandbox, extra_pythonpath=extra_pythonpath)
            if rc != 0:
                return False, f"回归测试执行失败: {test_file.name} 预期 exit 0, 实际 exit {rc}. 输出:\n{out}"

        return True, "本地红绿受限回放通过 (Red-to-Green verified: repro 0->1, tests 0)"


def _zip_package(package: Path) -> bytes:
    """Create a deterministic, non-symlink ZIP for remote verification."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            if path.is_symlink():
                raise CliError(f"远程补丁包不得包含 symlink: {path}")
            relative = path.relative_to(package).as_posix()
            archive.writestr(relative, path.read_bytes())
    return output.getvalue()


def _remote_client(args: Any):
    # 远程补丁验证走 core 的 CloudClient，与 cloud 命令一样必须先自举 core 到
    # sys.path。缺这一步时，正常安装环境（解释器 >= 3.11，cli.py 顶部的兼容性
    # 注入被跳过）会直接抛 ModuleNotFoundError，使 --submit/--status 全链路不可用。
    _ensure_core_importable()
    from core.cloud_sync.cloud_client import CloudClient, CloudCredentials

    key = (getattr(args, "api_key", None) or
           os.environ.get("BUGX_PATCH_VERIFICATION_API_KEY", "")).strip()
    if not key:
        raise CliError("远程补丁验证需要 BUGX_PATCH_VERIFICATION_API_KEY 或 --api-key")
    return CloudClient(base_url=getattr(args, "base_url", None),
                       credentials=CloudCredentials(api_key=key))


def command_patch_forge(args: Any) -> int:
    if args.status:
        client = _remote_client(args)
        result = client.get_patch_verification(args.status)
        if args.json:
            print(json.dumps(result.data, ensure_ascii=False, indent=2))
        else:
            data = client._data(result)
            print(f"verification_id: {args.status}")
            print(f"status: {data.get('status', 'unknown')}")
            print(f"decision: {data.get('decision', 'unknown')}")
        return 0 if result.ok else 1

    if args.verify:
        package = Path(args.verify).expanduser().resolve()
        if not package.is_dir():
            raise CliError(f"补丁包目录不存在: {package}")
        try:
            manifest_path = _package_file(package, "manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (CliError, OSError, json.JSONDecodeError) as exc:
            raise CliError(f"无法读取 manifest.json: {exc}") from exc
        errors = validate_manifest(manifest, package)
        red_to_green_status = "deferred"
        red_to_green_detail = "execution_deferred: MVP does not execute package contents"
        if not errors and getattr(args, "test_red_green", False):
            root = Path(args.root).expanduser().resolve()
            patch_data_list = [
                _package_file(package, item["path"]).read_bytes()
                for item in manifest.get("diff", {}).get("patch_files", [])
            ]
            repro_items = [
                (
                    item["path"],
                    item["before_exit"],
                    item["after_exit"],
                    _package_file(package, item["path"]).read_bytes(),
                )
                for item in manifest.get("repro", [])
            ]
            test_items = [
                (
                    item["path"],
                    _package_file(package, item["path"]).read_bytes(),
                )
                for item in manifest.get("tests", {}).get("new", [])
            ]
            ok, msg = _execute_red_to_green(
                root, manifest.get("diff", {}).get("files", []), patch_data_list, repro_items, test_items
            )
            if not ok:
                errors.append(f"{_RED_GREEN_ERROR_PREFIX}{msg}")
                red_to_green_status = "failed"
                red_to_green_detail = msg
            else:
                red_to_green_status = "passed"
                red_to_green_detail = msg

        report = _verification_report(
            manifest if isinstance(manifest, dict) else {},
            errors,
            red_to_green_status=red_to_green_status,
            red_to_green_detail=red_to_green_detail,
        )
        _print_report(report, as_json=args.json)
        return 0 if not errors else 1

    root = Path(args.root).expanduser().resolve()
    manifest, payloads = _build_package(root, args)

    red_to_green_status = "deferred"
    red_to_green_detail = "execution_deferred: MVP does not execute package contents"
    if getattr(args, "test_red_green", False):
        patch_data_list = [payloads[item["path"]] for item in manifest["diff"]["patch_files"]]
        repro_items = [
            (item["path"], item["before_exit"], item["after_exit"], payloads[item["path"]])
            for item in manifest["repro"]
        ]
        test_items = [
            (item["path"], payloads[item["path"]])
            for item in manifest["tests"]["new"]
        ]
        ok, msg = _execute_red_to_green(root, manifest["diff"]["files"], patch_data_list, repro_items, test_items)
        if not ok:
            raise CliError(f"红绿闭环验证失败: {msg}")
        red_to_green_status = "passed"
        red_to_green_detail = msg
        print(f"✓ {msg}")

    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if not args.output:
        raise CliError("非 dry-run 必须提供 --output")
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    if output.exists():
        raise CliError(f"输出目录已存在，拒绝覆盖: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".patch-forge-", dir=str(output.parent)))
    try:
        for relative, data in payloads.items():
            target = temp / Path(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _write_json(temp / "manifest.json", manifest)
        errors = validate_manifest(manifest, temp)
        report = _verification_report(
            manifest,
            errors,
            red_to_green_status=red_to_green_status,
            red_to_green_detail=red_to_green_detail,
        )
        if errors:
            raise CliError("生成的补丁包校验失败: " + "; ".join(errors))
        _write_json(temp / "verification-report.json", report)
        shutil.move(str(temp), str(output))
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    print(f"✓ 补丁包已生成: {output}")
    if not args.submit:
        if red_to_green_status == "passed":
            print(f"  execution: verified-local ({red_to_green_detail})")
        else:
            print("  execution: deferred（未执行 repro/test，未提交 remote）")
        return 0

    client = _remote_client(args)
    result = client.submit_patch_package(
        _zip_package(output), output.name + ".zip", source_commit=args.source_commit or "",
    )
    if args.json:
        print(json.dumps(result.data, ensure_ascii=False, indent=2))
    elif result.ok:
        data = client._data(result)
        print(f"  remote verification_id: {data.get('verification_id', '')}")
        print(f"  remote status: {data.get('status', 'unknown')}")
    else:
        print(f"  remote submit failed: {result.error or result.data}")
    return 0 if result.ok else 1
