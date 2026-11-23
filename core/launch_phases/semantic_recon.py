#!/usr/bin/env python3
"""core/launch_phases/semantic_recon.py — Recon 阶段轻量语义侦察叠加层（P2 / T-006）。

在确定性静态图谱之上叠加三类高价值语义资产，严格遵守零外部依赖与非受信输入隔离：
1. ``implicit_routes``：动态分发、插件注册表、RPC/装饰器工厂等隐式入口路由；
2. ``auth_boundaries``：自定义鉴权中间件、装饰器、租户隔离与信任域切换点；
3. ``business_invariants``：状态机合法迁移、金额/库存守恒、幂等键等业务不变量。

核心门禁：
- 物理锚点核验：拒绝不存在文件、越界路径（``..``）、符号链接穿越及超界行号；
- 反提示词注入净化：剥离控制字符、截断 ≤200 字符、拦截典型 prompt-injection 载荷；
- 文件级 SHA-256 哈希绑定：目标源码一旦修改，该文件的过期语义自动过滤失效。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

OVERLAY_SCHEMA_VERSION = 1
MAX_FIELD_CHARS = 200
MAX_CONTEXT_FILES = 24
MAX_CONTEXT_CHARS = 12000

_SKIP_DIRS = frozenset({
    ".git", ".bugx", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".pytest_cache",
})
_CODE_SUFFIXES = frozenset({".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".kt"})
_PRIORITY_KEYWORDS = (
    "route", "router", "dispatch", "handler", "middleware", "auth", "guard",
    "permission", "rbac", "tenant", "order", "payment", "billing", "balance",
    "state", "workflow", "webhook", "rpc", "plugin",
)

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|above|prior)\s+instructions?"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior)\s+instructions?"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:a|an|in)\s+"),
    re.compile(r"(?i)</?(?:system|assistant|user|instruction|prompt)\b[^>]*>"),
    re.compile(r"(?i)^\s*system\s*:"),
    re.compile(r"(?i)mark\s+all\s+(?:findings|vulnerabilities|issues)\s+(?:as\s+)?safe"),
)


def overlay_path(root: Path) -> Path:
    return Path(root).resolve() / ".bugx" / "state" / "semantic-recon-overlay.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _sanitize_text(value: object, *, max_len: int = MAX_FIELD_CHARS) -> tuple[str, bool]:
    """Strip control chars, bound length, and neutralize prompt-injection payloads."""
    raw = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    injected = False
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(raw):
            raw = pattern.sub("[REDACTED_INJECTION]", raw)
            injected = True
    if len(raw) > max_len:
        raw = raw[:max_len].rstrip()
    return raw, injected


def _resolve_anchor(root: Path, rel_file: object, line: object) -> tuple[str, int, str] | None:
    """Physically verify file exists inside root (no traversal/symlink) and line is in bounds."""
    if not isinstance(rel_file, str) or not rel_file.strip():
        return None
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return None
    candidate = Path(rel_file.replace("\\", "/").strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved_root = Path(root).resolve()
    cursor = resolved_root
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    try:
        abs_path = (resolved_root / candidate).resolve()
        rel_norm = abs_path.relative_to(resolved_root).as_posix()
    except (OSError, ValueError):
        return None
    if not abs_path.is_file() or abs_path.is_symlink():
        return None
    try:
        raw_bytes = abs_path.read_bytes()
    except OSError:
        return None
    line_count = max(1, len(raw_bytes.decode("utf-8", errors="replace").splitlines()))
    if line > line_count:
        return None
    return rel_norm, line, _sha256_bytes(raw_bytes)


def _iter_candidate_files(root: Path, *, max_files: int = MAX_CONTEXT_FILES) -> list[Path]:
    resolved_root = Path(root).resolve()
    scored: list[tuple[int, str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if fpath.is_symlink() or fpath.suffix.lower() not in _CODE_SUFFIXES:
                continue
            rel = fpath.relative_to(resolved_root).as_posix()
            rel_lower = rel.lower()
            score = sum(2 for kw in _PRIORITY_KEYWORDS if kw in rel_lower)
            scored.append((score, rel, fpath))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:max_files]]


def collect_semantic_context_pack(
    root: Path,
    *,
    max_files: int = MAX_CONTEXT_FILES,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> dict[str, Any]:
    """Build a bounded context pack for lightweight LLM semantic recon."""
    resolved_root = Path(root).resolve()
    files_payload: list[dict[str, Any]] = []
    used = 0
    for fpath in _iter_candidate_files(resolved_root, max_files=max_files):
        rel = fpath.relative_to(resolved_root).as_posix()
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = max_chars - used
        if remaining <= 128:
            break
        excerpt = text[: min(len(text), 2000, remaining)]
        files_payload.append({
            "file": rel,
            "line_count": len(text.splitlines()),
            "excerpt": excerpt,
        })
        used += len(excerpt)
    return {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "root_name": resolved_root.name,
        "file_count": len(files_payload),
        "files": files_payload,
    }


def _heuristic_extract_python(root: Path, fpath: Path) -> dict[str, list[dict[str, Any]]]:
    """Zero-dependency Python AST heuristic extractor for implicit routes, auth boundaries & invariants."""
    rel = fpath.resolve().relative_to(Path(root).resolve()).as_posix()
    routes: list[dict[str, Any]] = []
    auths: list[dict[str, Any]] = []
    invariants: list[dict[str, Any]] = []
    try:
        src = fpath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=rel)
    except (OSError, SyntaxError):
        return {"implicit_routes": routes, "auth_boundaries": auths, "business_invariants": invariants}

    for node in ast.walk(tree):
        # 1. Implicit dispatch: getattr(..., action/method/handler) or HANDLERS/ROUTES dict registry
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            routes.append({
                "file": rel,
                "line": getattr(node, "lineno", 1),
                "endpoint_or_dispatch": "dynamic getattr() dispatch",
                "handler": "dynamic_target",
                "auth_required": "unknown",
                "confidence": "medium",
                "source": "heuristic_ast",
            })
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and any(
                    tok in target.id.upper() for tok in ("ROUTE", "HANDLER", "DISPATCH", "RPC_METHOD")
                ) and isinstance(node.value, ast.Dict):
                    routes.append({
                        "file": rel,
                        "line": getattr(node, "lineno", 1),
                        "endpoint_or_dispatch": f"registry table {target.id}",
                        "handler": target.id,
                        "auth_required": "unknown",
                        "confidence": "medium",
                        "source": "heuristic_ast",
                    })
                elif isinstance(target, ast.Name) and any(
                    tok in target.id.upper() for tok in ("TRANSITION", "ALLOWED_STATE", "VALID_STATUS")
                ):
                    invariants.append({
                        "file": rel,
                        "line": getattr(node, "lineno", 1),
                        "entity": target.id,
                        "invariant_rule": f"explicit state transition table {target.id}",
                        "violation_impact": "state machine bypass if transition check is skipped",
                        "confidence": "medium",
                        "source": "heuristic_ast",
                    })

        # 2. Custom auth middleware / guard functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lname = node.name.lower()
            if any(tok in lname for tok in ("require_auth", "check_auth", "verify_token", "auth_middleware", "require_role", "tenant_guard")):
                auths.append({
                    "file": rel,
                    "line": getattr(node, "lineno", 1),
                    "mechanism": f"custom guard `{node.name}`",
                    "scope": rel,
                    "trust_transition": "untrusted request -> authenticated principal",
                    "confidence": "high",
                    "source": "heuristic_ast",
                })
            elif any(tok in lname for tok in ("transfer", "refund", "checkout", "withdraw", "transition_state", "approve_order")):
                invariants.append({
                    "file": rel,
                    "line": getattr(node, "lineno", 1),
                    "entity": node.name,
                    "invariant_rule": f"business operation `{node.name}` requires non-negative conservation & valid state check",
                    "violation_impact": "financial/state integrity violation",
                    "confidence": "medium",
                    "source": "heuristic_ast",
                })

    return {
        "implicit_routes": routes,
        "auth_boundaries": auths,
        "business_invariants": invariants,
    }


def heuristic_semantic_recon(root: Path, *, max_files: int = MAX_CONTEXT_FILES) -> dict[str, list[dict[str, Any]]]:
    """Offline zero-dependency semantic clue extractor across candidate files."""
    combined: dict[str, list[dict[str, Any]]] = {
        "implicit_routes": [],
        "auth_boundaries": [],
        "business_invariants": [],
    }
    for fpath in _iter_candidate_files(root, max_files=max_files):
        if fpath.suffix.lower() == ".py":
            part = _heuristic_extract_python(root, fpath)
            for key in combined:
                combined[key].extend(part[key])
    return combined


def validate_and_anchor_overlay(
    root: Path,
    raw_overlay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Physically verify file/line anchors & sanitize prompt-injection payloads (P2-D02)."""
    watched_hashes: dict[str, str] = {}
    rejected_hallucinations = 0
    sanitized_injections = 0

    def _clean_items(
        items: object,
        spec_fields: tuple[str, ...],
        dedup_keys: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        nonlocal rejected_hallucinations, sanitized_injections
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            return []
        cleaned: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for entry in items:
            if not isinstance(entry, Mapping):
                rejected_hallucinations += 1
                continue
            anchor = _resolve_anchor(root, entry.get("file"), entry.get("line"))
            if anchor is None:
                rejected_hallucinations += 1
                continue
            rel_norm, line_no, file_hash = anchor
            watched_hashes[rel_norm] = file_hash
            record: dict[str, Any] = {"file": rel_norm, "line": line_no}
            for field in spec_fields:
                text, inj = _sanitize_text(entry.get(field, ""))
                if inj:
                    sanitized_injections += 1
                record[field] = text
            conf = str(entry.get("confidence") or "medium").strip().lower()
            record["confidence"] = conf if conf in {"high", "medium", "low"} else "medium"
            src_tag, _ = _sanitize_text(entry.get("source") or "llm_semantic", max_len=48)
            record["source"] = src_tag or "llm_semantic"
            sig = tuple(str(record.get(k, "")) for k in dedup_keys)
            if sig in seen:
                continue
            seen.add(sig)
            cleaned.append(record)
        return cleaned

    routes = _clean_items(
        raw_overlay.get("implicit_routes"),
        ("endpoint_or_dispatch", "handler", "auth_required"),
        ("file", "line", "endpoint_or_dispatch"),
    )
    auths = _clean_items(
        raw_overlay.get("auth_boundaries"),
        ("mechanism", "scope", "trust_transition"),
        ("file", "line", "mechanism"),
    )
    invariants = _clean_items(
        raw_overlay.get("business_invariants"),
        ("entity", "invariant_rule", "violation_impact"),
        ("file", "line", "entity", "invariant_rule"),
    )

    validated = {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "implicit_routes": routes,
        "auth_boundaries": auths,
        "business_invariants": invariants,
        "audit_stats": {
            "rejected_hallucinations": rejected_hallucinations,
            "sanitized_injections": sanitized_injections,
            "anchored_files": len(watched_hashes),
        },
    }
    return validated, watched_hashes


def run_semantic_recon_overlay(
    root: Path,
    *,
    llm_analyzer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    max_files: int = MAX_CONTEXT_FILES,
    max_chars: int = MAX_CONTEXT_CHARS,
    persist: bool = True,
) -> dict[str, Any]:
    """Run dual-channel semantic recon (optional LLM + heuristic baseline), anchor-verify & persist."""
    resolved_root = Path(root).resolve()
    merged: dict[str, list[dict[str, Any]]] = {
        "implicit_routes": [],
        "auth_boundaries": [],
        "business_invariants": [],
    }
    mode = "heuristic_fallback"

    if llm_analyzer is not None:
        context_pack = collect_semantic_context_pack(
            resolved_root, max_files=max_files, max_chars=max_chars,
        )
        try:
            llm_out = llm_analyzer(context_pack)
            if isinstance(llm_out, Mapping):
                for key in merged:
                    raw_list = llm_out.get(key)
                    if isinstance(raw_list, list):
                        for item in raw_list:
                            if isinstance(item, Mapping):
                                merged[key].append({"source": "llm_semantic", **dict(item)})
                mode = "llm_plus_heuristic"
        except Exception:
            mode = "heuristic_fallback_after_llm_error"

    heur = heuristic_semantic_recon(resolved_root, max_files=max_files)
    for key in merged:
        merged[key].extend(heur[key])

    validated, watched_hashes = validate_and_anchor_overlay(resolved_root, merged)
    payload = {
        **validated,
        "mode": mode,
        "watched_file_hashes": watched_hashes,
        "created_at": time.time(),
    }
    if persist:
        _atomic_write_json(overlay_path(resolved_root), payload)
    return payload


def load_semantic_overlay(
    root: Path,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Load persisted semantic overlay and drop entries whose anchored file hash drifted (P2-D03)."""
    resolved_root = Path(root).resolve()
    path = overlay_path(resolved_root)
    if not path.is_file():
        return {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "implicit_routes": [],
            "auth_boundaries": [],
            "business_invariants": [],
            "stale_files": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "implicit_routes": [],
            "auth_boundaries": [],
            "business_invariants": [],
            "stale_files": [],
        }
    if not isinstance(data, dict):
        return {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "implicit_routes": [],
            "auth_boundaries": [],
            "business_invariants": [],
            "stale_files": [],
        }

    watched = data.get("watched_file_hashes") if isinstance(data.get("watched_file_hashes"), dict) else {}
    stale_files: set[str] = set()
    for rel, expected_hash in watched.items():
        if not isinstance(rel, str) or not isinstance(expected_hash, str):
            continue
        target = resolved_root / rel
        if not target.is_file() or target.is_symlink():
            stale_files.add(rel)
            continue
        try:
            actual = _sha256_bytes(target.read_bytes())
        except OSError:
            stale_files.add(rel)
            continue
        if actual != expected_hash:
            stale_files.add(rel)

    def _filter_list(key: str) -> list[dict[str, Any]]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("file") or "")
            is_stale = rel in stale_files
            if is_stale and not allow_stale:
                continue
            copied = dict(item)
            if is_stale:
                copied["stale"] = True
            out.append(copied)
        return out

    return {
        "schema_version": int(data.get("schema_version") or OVERLAY_SCHEMA_VERSION),
        "mode": data.get("mode", ""),
        "implicit_routes": _filter_list("implicit_routes"),
        "auth_boundaries": _filter_list("auth_boundaries"),
        "business_invariants": _filter_list("business_invariants"),
        "audit_stats": data.get("audit_stats") or {},
        "stale_files": sorted(stale_files),
    }


def render_semantic_overlay_guidance(
    overlay: Mapping[str, Any],
    *,
    target_files: Sequence[str] = (),
    char_budget: int = 900,
) -> str:
    """Render target-filtered, budget-capped semantic recon guidance for Worker prompts."""
    if char_budget <= 0 or not isinstance(overlay, Mapping):
        return ""
    wanted = {f.replace("\\", "/").strip() for f in target_files if isinstance(f, str) and f.strip()}

    def _match(items: object) -> list[dict[str, Any]]:
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            return []
        res = [it for it in items if isinstance(it, dict)]
        if wanted:
            res = [it for it in res if str(it.get("file") or "") in wanted]
        return res

    routes = _match(overlay.get("implicit_routes"))
    auths = _match(overlay.get("auth_boundaries"))
    invs = _match(overlay.get("business_invariants"))
    if not (routes or auths or invs):
        return ""

    lines = ["[语义侦察叠加层 (Semantic Recon Overlay)]"]
    used = len(lines[0]) + 1
    candidates: list[str] = []
    for r in routes:
        candidates.append(
            f"- [隐式路由] {r.get('file')}:{r.get('line')} | {r.get('endpoint_or_dispatch')} -> "
            f"{r.get('handler')} (auth={r.get('auth_required')})"
        )
    for a in auths:
        candidates.append(
            f"- [鉴权边界] {a.get('file')}:{a.get('line')} | {a.get('mechanism')} ({a.get('trust_transition')})"
        )
    for inv in invs:
        candidates.append(
            f"- [业务不变量] {inv.get('file')}:{inv.get('line')} | {inv.get('entity')}: {inv.get('invariant_rule')}"
        )

    for line in candidates:
        if used + len(line) + 1 > char_budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = [
    "OVERLAY_SCHEMA_VERSION",
    "overlay_path",
    "collect_semantic_context_pack",
    "heuristic_semantic_recon",
    "validate_and_anchor_overlay",
    "run_semantic_recon_overlay",
    "load_semantic_overlay",
    "render_semantic_overlay_guidance",
]
