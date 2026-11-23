#!/usr/bin/env python3
"""verify_loop.py — 独立 Verification 角色盲审包构造与报错驱动 PoC 自纠错闭环（P0 / T-004）。

职责边界：
1. 盲审隔离（D-01）：从 Supervisor 权威态构造最小 ``VerifyBundle``（只含白名单
   缺陷声明 + 目标代码切片 + 入口摘要 + 运行环境事实 + 上一轮引擎诊断），绝不透传
   Analysis/Judge 长推理链。
2. 结构化失败诊断（D-02）：将每次引擎 ``verify_finding`` 失败按 8 类精确
   ``failure_code`` 落盘至当前 lifecycle ``state/verify/<fp_slug>/attempt-<n>.json``。
3. 有界自纠错循环（D-03）：最多 ``max_attempts``（默认 3，上限 5）轮报错驱动修订；
   连续两轮代码哈希相同即判定自旋锁（``verifier_stalled``）立即熔断；遇到
   ``env_or_dependency_blocked`` 或连续 2 次 ``positive_flaky`` 立即熔断。
4. 显式终态收敛（D-04）：禁止静默丢弃；成功进 ``confirmed``，环境阻塞标 ``blocked``，
   预算耗尽标 ``deferred_budget_exhausted``（保持 pending 可续跑），重试耗尽按
   规范 ``rejection_class``（``not_reproducible`` / ``flaky`` / ``insufficient_evidence``）驳回。
5. Anti-Gaming（D-05）：锁定 ``oracle_reference`` / ``oracle_hash`` 不可被重试覆写；
   AST 拦截空壳负对照；执行前后校验目标源码 SHA-256 防止 PoC 篡改被测代码。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.ledger.finding_verification import (
    RETRYABLE_FAILURE_CODES,
    VerificationDiagnostic,
    diagnose_verification_error,
    diagnose_verification_receipt,
    snapshot_target_hashes,
    validate_python_poc_pair,
    verify_target_hashes_unchanged,
)

DEFAULT_MAX_ATTEMPTS = 3
HARD_MAX_ATTEMPTS = 5
MAX_SLICE_BYTES = 8 * 1024
_SHELL_TOKENS = (";", "&&", "||", "`", "$(", ">", "<")

_POS_EXEC_TEMPLATE = (
    "import sys as _s\n"
    "_g = {{'__name__': '__bugx_verification__'}}\n"
    "_rc = 0\n"
    "try:\n"
    "    exec(compile({src!r}, 'verification-positive', 'exec'), _g)\n"
    "except SystemExit as _e:\n"
    "    _rc = _e.code if isinstance(_e.code, int) else 1\n"
    "except BaseException as _e:\n"
    "    _rc = 0\n"
    "    print('BUGX_VERIFY_POSITIVE unexpected-error=%s' % (type(_e).__name__,))\n"
    "print('BUGX_VERIFY_POSITIVE rc=%s' % (_rc,))\n"
    "_s.exit(1 if _rc else 0)\n"
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fp_slug(fingerprint: str) -> str:
    return _sha256_text(fingerprint.strip())[:16]


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
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


def strip_runner_wrapper(code: str) -> str:
    """Strip accidental `python3 -c` / `python -c` wrapper from verifier output."""
    text = (code or "").strip()
    for prefix in ("python3 -c", "python -c"):
        if text.startswith(prefix):
            body = text[len(prefix):].strip()
            if len(body) >= 2 and body[0] in "'\"" and body[-1] == body[0]:
                body = body[1:-1]
            return body.strip()
    return text


def precheck_shell_tokens(code: str, label: str) -> str | None:
    for token in _SHELL_TOKENS:
        if token in code:
            return f"{label} 含 shell 禁字符 {token!r}：请改用换行分隔语句或 chr() 构造载荷"
    return None


def wrap_positive_argv(code: str) -> list[str]:
    return [sys.executable or "python3", "-c", _POS_EXEC_TEMPLATE.format(src=strip_runner_wrapper(code))]


def wrap_negative_argv(code: str) -> list[str]:
    return [sys.executable or "python3", "-c", strip_runner_wrapper(code)]


def normalize_max_attempts(value: int | None) -> int:
    if value is None:
        return DEFAULT_MAX_ATTEMPTS
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > HARD_MAX_ATTEMPTS:
        raise ValueError(f"max_attempts 必须为 1..{HARD_MAX_ATTEMPTS} 之间的整数，收到: {value!r}")
    return value


def verify_dir_for_finding(state_dir: Path, fingerprint: str) -> Path:
    path = Path(state_dir) / "verify" / _fp_slug(fingerprint)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _extract_target_file_and_line(finding: Mapping[str, Any]) -> tuple[str, int | None]:
    file_val = str(finding.get("file") or finding.get("path") or "").strip()
    line_val = finding.get("line")
    line_no = line_val if isinstance(line_val, int) and line_val > 0 else None
    location = str(finding.get("location") or "").strip()
    if not file_val and location:
        parts = location.split(":", 1)
        file_val = parts[0].strip()
        if len(parts) == 2 and parts[1].strip().isdigit():
            line_no = int(parts[1].strip())
    elif line_no is None and ":" in location:
        tail = location.rsplit(":", 1)[-1].strip()
        if tail.isdigit():
            line_no = int(tail)
    return file_val.replace("\\", "/"), line_no


def _extract_target_hints(finding: Mapping[str, Any], rel_file: str) -> tuple[str, ...]:
    hints: list[str] = []
    if rel_file:
        stem = Path(rel_file).stem
        if stem and stem != "__init__":
            hints.append(stem)
        parent = Path(rel_file).parent.name
        if parent and parent not in {".", ""}:
            hints.append(parent)
    for key in ("symbol", "function", "method"):
        raw = str(finding.get(key) or "").strip()
        if raw:
            hints.append(raw.split(".")[-1])
    deduped: list[str] = []
    for h in hints:
        if h not in deduped:
            deduped.append(h)
    return tuple(deduped)


def _extract_code_slice(root: Path, rel_file: str, line_no: int | None) -> tuple[str, bool]:
    """Extract bounded AST/line slice for blind verification without leaking analysis text."""
    if not rel_file:
        return "", True
    resolved_root = Path(root).resolve()
    candidate = Path(rel_file)
    if candidate.is_absolute() or ".." in candidate.parts:
        return "", True
    try:
        target = (resolved_root / candidate).resolve()
        target.relative_to(resolved_root)
    except (OSError, ValueError):
        return "", True
    if not target.is_file() or target.is_symlink():
        return "", True

    # Try AST slice_extractor first if available
    slice_fallback = False
    try:
        from core.retrieval import slice_extractor  # type: ignore
        if hasattr(slice_extractor, "extract_slice"):
            extracted = slice_extractor.extract_slice(target, line_no or 1)
            if isinstance(extracted, str) and extracted.strip():
                raw = extracted.encode("utf-8", errors="replace")[:MAX_SLICE_BYTES].decode("utf-8", errors="ignore")
                return raw, False
    except Exception:
        slice_fallback = True

    # Bounded line-window fallback (fail-closed recorded via slice_fallback=True)
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", True
    center = max(1, line_no or 1)
    start = max(1, center - 25)
    end = min(len(lines), center + 45)
    numbered = [f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1)]
    text = "\n".join(numbered)
    raw = text.encode("utf-8", errors="replace")[:MAX_SLICE_BYTES].decode("utf-8", errors="ignore")
    return raw, True


def ensure_locked_oracle(root: Path, finding: Mapping[str, Any]) -> tuple[str, str]:
    """Deterministically create and lock the immutable oracle file + SHA-256."""
    fp = str(finding.get("fingerprint") or finding.get("id") or "").strip()
    if not fp:
        raise ValueError("finding 缺少 fingerprint")
    slug = _sha256_text(fp)[:12]
    oracle_dir = Path(root).resolve() / ".bugx" / "knowledge" / "oracles"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    oracle_rel = f".bugx/knowledge/oracles/{slug}.txt"
    oracle_path = oracle_dir / f"{slug}.txt"
    if not oracle_path.is_file():
        severity = str(finding.get("severity") or "unknown")
        category = str(finding.get("category") or "unknown")
        rel_file, line_no = _extract_target_file_and_line(finding)
        loc = f"{rel_file}:{line_no}" if rel_file and line_no else rel_file
        oracle_path.write_text(
            f"fingerprint={fp}\n"
            f"severity={severity}\n"
            f"category={category}\n"
            f"location={loc}\n"
            f"positive: 漏洞存在时非零退出（引擎锁定，独立验证员不可篡改）\n"
            f"negative: 安全输入下 0 退出（必须调用同一目标入口）\n",
            encoding="utf-8",
        )
    oracle_hash = hashlib.sha256(oracle_path.read_bytes()).hexdigest()
    return oracle_rel, oracle_hash


def build_verify_bundle(
    root: Path,
    state_dir: Path,
    finding: Mapping[str, Any],
    *,
    attempt: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    last_diagnostic: Mapping[str, Any] | None = None,
    host: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    policy: str | None = None,
    last_error: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Build and persist the blind-review VerifyBundle (D-01 + Prompt Policy Engine)."""
    from core.planning import prompt_policy_engine

    fp = str(finding.get("fingerprint") or finding.get("id") or "").strip()
    if not fp:
        raise ValueError("finding 缺少 fingerprint")
    rel_file, line_no = _extract_target_file_and_line(finding)
    code_slice, slice_fallback = _extract_code_slice(root, rel_file, line_no)
    oracle_rel, oracle_hash = ensure_locked_oracle(root, finding)
    target_hints = list(_extract_target_hints(finding, rel_file))

    resolved_policy = prompt_policy_engine.resolve_policy(
        host=host,
        provider=provider,
        model=model,
        last_error=last_error,
        explicit_policy=policy,
        root=Path(root).resolve(),
    )
    override_rel = prompt_policy_engine.resolve_stage_override_relpath(resolved_policy, "verify")
    protocol_ref = f"core/prompts/{override_rel}" if override_rel else "core/prompts/stages/stage_verify.md"

    summary_text = str(
        finding.get("summary")
        or finding.get("title")
        or finding.get("description")
        or ""
    ).strip()[:300]

    bundle: dict[str, Any] = {
        "schema_version": 1,
        "role": "verification_specialist",
        "policy_profile": resolved_policy.name,
        "protocol_ref": protocol_ref,
        "safe_repro_directive": resolved_policy.safe_repro_directive,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "candidate": {
            "fingerprint": fp,
            "category": str(finding.get("category") or ""),
            "severity": str(finding.get("severity") or ""),
            "file": rel_file,
            "line": line_no,
            "location": f"{rel_file}:{line_no}" if rel_file and line_no else rel_file,
            "summary": summary_text,
            "target_hints": target_hints,
        },
        "code_slice": {
            "path": rel_file,
            "content": code_slice,
            "slice_fallback": slice_fallback,
        },
        "locked_oracle": {
            "oracle_reference": oracle_rel,
            "oracle_hash": oracle_hash,
            "immutable": True,
        },
        "environment": {
            "cwd": ".",
            "python_executable": sys.executable or "python3",
            "timeout_seconds": 30,
        },
        "last_diagnostic": dict(last_diagnostic) if isinstance(last_diagnostic, Mapping) else None,
        "created_at": time.time(),
    }
    vdir = verify_dir_for_finding(state_dir, fp)
    bundle_path = vdir / "bundle.json"
    _atomic_write_json(bundle_path, bundle)
    return bundle_path, bundle


def _load_attempt_history(vdir: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if not vdir.is_dir():
        return history
    for path in sorted(vdir.glob("attempt-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                history.append(data)
        except (OSError, ValueError):
            continue
    return history


def record_attempt_diagnostic(
    vdir: Path,
    diagnostic: VerificationDiagnostic,
    *,
    code_digest: str = "",
) -> Path:
    payload = diagnostic.to_dict()
    if code_digest:
        payload["code_digest"] = code_digest
    path = vdir / f"attempt-{diagnostic.attempt}.json"
    _atomic_write_json(path, payload)
    return path


def _code_pair_digest(positive: str, negative: str) -> str:
    norm_pos = strip_runner_wrapper(positive)
    norm_neg = strip_runner_wrapper(negative)
    return _sha256_text(f"{norm_pos}\n---\n{norm_neg}")


def _rejection_class_for_failure(failure_code: str) -> str:
    if failure_code == "positive_flaky":
        return "flaky"
    if failure_code in {"positive_not_triggered", "negative_false_trigger"}:
        return "not_reproducible"
    return "insufficient_evidence"


def execute_verify_attempt(
    *,
    root: Path,
    state_dir: Path,
    supervisor: Any,
    finding: Mapping[str, Any],
    positive: str,
    negative: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    attempt: int | None = None,
    budget_remaining: int | None = None,
    auto_settle_terminal: bool = True,
) -> dict[str, Any]:
    """Execute one bounded verifier attempt with Anti-Gaming, diagnostic persistence & terminal settlement."""
    max_attempts = normalize_max_attempts(max_attempts)
    fp = str(finding.get("fingerprint") or finding.get("id") or "").strip()
    if not fp:
        raise ValueError("finding 缺少 fingerprint")
    vdir = verify_dir_for_finding(state_dir, fp)
    history = _load_attempt_history(vdir)
    current_attempt = attempt if attempt is not None else (len(history) + 1)

    if budget_remaining is not None and budget_remaining <= 0:
        return {
            "status": "DEFERRED_BUDGET_EXHAUSTED",
            "fingerprint": fp,
            "attempt": current_attempt,
            "max_attempts": max_attempts,
            "reason": "verification budget exhausted; finding remains pending for resume",
        }

    oracle_rel, oracle_hash = ensure_locked_oracle(root, finding)
    rel_file, _ = _extract_target_file_and_line(finding)
    target_hints = _extract_target_hints(finding, rel_file)

    norm_pos = strip_runner_wrapper(positive)
    norm_neg = strip_runner_wrapper(negative)
    pair_digest = _code_pair_digest(norm_pos, norm_neg)

    # Anti-spinlock: reject identical consecutive code submissions
    if history and history[-1].get("code_digest") == pair_digest:
        diag = VerificationDiagnostic(
            finding_key=fp,
            attempt=current_attempt,
            failure_code="verifier_stalled",
            stage="spinlock_guard",
            retryable=False,
            summary="verifier submitted identical positive/negative code on consecutive attempts",
            created_at=time.time(),
        )
        diag_path = record_attempt_diagnostic(vdir, diag, code_digest=pair_digest)
        if auto_settle_terminal:
            supervisor.confirm_finding(
                finding=fp,
                decision="rejected",
                reason=f"verify_loop 熔断（verifier_stalled）：连续两轮提交相同 PoC（{diag_path.name}）",
                rejection_class="insufficient_evidence",
            )
        return {
            "status": "STALLED",
            "fingerprint": fp,
            "attempt": current_attempt,
            "max_attempts": max_attempts,
            "diagnostic": diag.to_dict(),
            "diagnostic_path": str(diag_path),
            "terminal_decision": "rejected" if auto_settle_terminal else None,
        }

    # 1. Shell token precheck
    shell_err = precheck_shell_tokens(norm_pos, f"{fp} positive") or precheck_shell_tokens(norm_neg, f"{fp} negative")
    if shell_err:
        diag = diagnose_verification_error(
            shell_err, attempt=current_attempt, finding_key=fp,
            failure_code="shell_token_rejected", stage="shell_precheck",
        )
        return _finalize_failed_attempt(
            root=root, state_dir=state_dir, supervisor=supervisor, finding=finding,
            vdir=vdir, diag=diag, code_digest=pair_digest, history=history,
            current_attempt=current_attempt, max_attempts=max_attempts,
            auto_settle_terminal=auto_settle_terminal,
        )

    # 2. Anti-Gaming AST validation (reject trivial/empty negative control & syntax errors)
    ast_diag = validate_python_poc_pair(
        norm_pos, norm_neg, finding_key=fp, attempt=current_attempt, target_hints=target_hints,
    )
    if ast_diag is not None:
        return _finalize_failed_attempt(
            root=root, state_dir=state_dir, supervisor=supervisor, finding=finding,
            vdir=vdir, diag=ast_diag, code_digest=pair_digest, history=history,
            current_attempt=current_attempt, max_attempts=max_attempts,
            auto_settle_terminal=auto_settle_terminal,
        )

    # 3. Snapshot target source hashes before execution (D-05 anti-tamper)
    watched_files = [rel_file] if rel_file else []
    before_hashes = snapshot_target_hashes(root, watched_files)

    # 4. Build immutable-oracle recipe and execute via engine
    recipe = {
        "positive": {
            "argv": wrap_positive_argv(norm_pos),
            "oracle": {"kind": "exit_code", "expected_exit_code": 1},
        },
        "negative_control": {
            "argv": wrap_negative_argv(norm_neg),
            "oracle": {"kind": "exit_code", "expected_exit_code": 0},
        },
        "cwd": ".",
        "timeout_seconds": 30,
        "oracle_reference": oracle_rel,
        "oracle_hash": oracle_hash,
    }

    try:
        verify_res = supervisor.verify_finding(finding=fp, recipe=recipe)
    except Exception as exc:
        tampered = verify_target_hashes_unchanged(root, before_hashes)
        if tampered:
            diag = VerificationDiagnostic(
                finding_key=fp, attempt=current_attempt,
                failure_code="source_tampered_rejected", stage="anti_tamper",
                retryable=False,
                summary=f"PoC modified target source file(s): {', '.join(tampered)}",
                created_at=time.time(),
            )
        else:
            diag = diagnose_verification_error(
                exc, attempt=current_attempt, finding_key=fp, stage="supervisor_verify",
            )
        return _finalize_failed_attempt(
            root=root, state_dir=state_dir, supervisor=supervisor, finding=finding,
            vdir=vdir, diag=diag, code_digest=pair_digest, history=history,
            current_attempt=current_attempt, max_attempts=max_attempts,
            auto_settle_terminal=auto_settle_terminal,
        )

    # 5. Post-execution source tamper check (even if receipt says CONFIRMED!)
    tampered = verify_target_hashes_unchanged(root, before_hashes)
    if tampered:
        diag = VerificationDiagnostic(
            finding_key=fp,
            attempt=current_attempt,
            failure_code="source_tampered_rejected",
            stage="anti_tamper",
            retryable=False,
            summary=f"PoC modified target source file(s): {', '.join(tampered)}",
            created_at=time.time(),
        )
        return _finalize_failed_attempt(
            root=root, state_dir=state_dir, supervisor=supervisor, finding=finding,
            vdir=vdir, diag=diag, code_digest=pair_digest, history=history,
            current_attempt=current_attempt, max_attempts=max_attempts,
            auto_settle_terminal=auto_settle_terminal,
        )

    receipt = verify_res.get("receipt") if isinstance(verify_res, Mapping) else {}
    status = str(verify_res.get("status") or (receipt.get("status") if isinstance(receipt, Mapping) else ""))
    if status == "CONFIRMED":
        severity = str(finding.get("severity") or "").strip() or None
        supervisor.confirm_finding(
            finding=fp,
            decision="confirmed",
            reason=f"verify_loop 独立盲审验证通过（第 {current_attempt}/{max_attempts} 轮双正例+负对照 CONFIRMED）",
            root_cause_verified=True,
            oracle={"tier": "differential", "provenance_kind": "runtime-invariant", "reference": oracle_rel},
            severity=severity,
        )
        summary_payload = {
            "fingerprint": fp,
            "status": "CONFIRMED",
            "verified_at_attempt": current_attempt,
            "max_attempts": max_attempts,
            "oracle_reference": oracle_rel,
            "oracle_hash": oracle_hash,
            "verification_id": receipt.get("verification_id") if isinstance(receipt, Mapping) else None,
            "attempt_history_count": len(history) + 1,
            "completed_at": time.time(),
        }
        _atomic_write_json(vdir / "summary.json", summary_payload)
        return summary_payload

    diag = diagnose_verification_receipt(
        receipt if isinstance(receipt, Mapping) else {},
        attempt=current_attempt,
        finding_key=fp,
    )
    return _finalize_failed_attempt(
        root=root, state_dir=state_dir, supervisor=supervisor, finding=finding,
        vdir=vdir, diag=diag, code_digest=pair_digest, history=history,
        current_attempt=current_attempt, max_attempts=max_attempts,
        auto_settle_terminal=auto_settle_terminal,
    )


def _finalize_failed_attempt(
    *,
    root: Path,
    state_dir: Path,
    supervisor: Any,
    finding: Mapping[str, Any],
    vdir: Path,
    diag: VerificationDiagnostic,
    code_digest: str,
    history: Sequence[Mapping[str, Any]],
    current_attempt: int,
    max_attempts: int,
    auto_settle_terminal: bool,
) -> dict[str, Any]:
    fp = diag.finding_key
    diag_path = record_attempt_diagnostic(vdir, diag, code_digest=code_digest)

    # Immediate circuit-breakers (D-03)
    if diag.failure_code == "env_or_dependency_blocked":
        summary = {
            "status": "BLOCKED",
            "fingerprint": fp,
            "attempt": current_attempt,
            "max_attempts": max_attempts,
            "diagnostic": diag.to_dict(),
            "diagnostic_path": str(diag_path),
            "terminal_decision": "blocked",
        }
        _atomic_write_json(vdir / "summary.json", summary)
        return summary

    if diag.failure_code == "source_tampered_rejected":
        if auto_settle_terminal:
            supervisor.confirm_finding(
                finding=fp,
                decision="rejected",
                reason=f"verify_loop 反作弊阻断（source_tampered_rejected）：{diag.summary}",
                rejection_class="insufficient_evidence",
            )
        summary = {
            "status": "REJECTED",
            "fingerprint": fp,
            "attempt": current_attempt,
            "max_attempts": max_attempts,
            "diagnostic": diag.to_dict(),
            "diagnostic_path": str(diag_path),
            "terminal_decision": "rejected" if auto_settle_terminal else None,
            "rejection_class": "insufficient_evidence",
        }
        _atomic_write_json(vdir / "summary.json", summary)
        return summary

    flaky_count = sum(1 for item in history if item.get("failure_code") == "positive_flaky") + (
        1 if diag.failure_code == "positive_flaky" else 0
    )
    can_retry = (
        diag.retryable
        and diag.failure_code in RETRYABLE_FAILURE_CODES
        and current_attempt < max_attempts
        and flaky_count < 2
    )
    if can_retry:
        next_bundle_path, _ = build_verify_bundle(
            root,
            state_dir,
            finding,
            attempt=current_attempt + 1,
            max_attempts=max_attempts,
            last_diagnostic=diag.to_dict(),
        )
        return {
            "status": "RETRY",
            "fingerprint": fp,
            "attempt": current_attempt,
            "next_attempt": current_attempt + 1,
            "max_attempts": max_attempts,
            "diagnostic": diag.to_dict(),
            "diagnostic_path": str(diag_path),
            "next_bundle_path": str(next_bundle_path),
        }

    # Terminal rejection (never silently dropped)
    rej_class = _rejection_class_for_failure(diag.failure_code)
    if auto_settle_terminal:
        supervisor.confirm_finding(
            finding=fp,
            decision="rejected",
            reason=(
                f"verify_loop 验证耗尽/熔断（{diag.failure_code}, "
                f"attempt {current_attempt}/{max_attempts}）：{diag.summary}"
            ),
            rejection_class=rej_class,
        )
    # P1-D04: 仅当真实实跑证伪（not_reproducible）时沉淀为带源码哈希绑定的负样本护栏；
    # 严禁把 env_blocked / timeout / source_tampered / flaky 误记为“代码安全”。
    fp_guard_id: str | None = None
    if rej_class == "not_reproducible" and diag.failure_code in {"positive_not_triggered", "negative_false_trigger"}:
        rel_file, _ = _extract_target_file_and_line(finding)
        if rel_file:
            try:
                from core.ledger import fp_ledger
                rec = fp_ledger.record_false_positive(
                    root,
                    file_path=rel_file,
                    rejection_class=rej_class,
                    guard_evidence=(
                        f"verify_loop {current_attempt}/{max_attempts} 轮实跑证伪 "
                        f"({diag.failure_code}): {diag.summary}"
                    ),
                    category=str(finding.get("category") or ""),
                    cwe=str(finding.get("cwe") or ""),
                    symbol=str(finding.get("symbol") or ""),
                    fingerprint=fp,
                )
                fp_guard_id = str(rec.get("fp_id") or "") or None
            except Exception:
                fp_guard_id = None
    summary = {
        "status": "REJECTED",
        "fingerprint": fp,
        "attempt": current_attempt,
        "max_attempts": max_attempts,
        "diagnostic": diag.to_dict(),
        "diagnostic_path": str(diag_path),
        "terminal_decision": "rejected" if auto_settle_terminal else None,
        "rejection_class": rej_class,
        "fp_guard_id": fp_guard_id,
    }
    _atomic_write_json(vdir / "summary.json", summary)
    return summary


def run_verification_loop(
    *,
    root: Path,
    state_dir: Path,
    supervisor: Any,
    finding: Mapping[str, Any],
    proposer: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    budget_remaining: int | None = None,
    auto_settle_terminal: bool = True,
) -> dict[str, Any]:
    """Drive the full error-driven PoC self-correction loop via a pluggable Verifier proposer."""
    max_attempts = normalize_max_attempts(max_attempts)
    last_diag: Mapping[str, Any] | None = None
    remaining = budget_remaining

    for attempt_no in range(1, max_attempts + 1):
        if remaining is not None and remaining <= 0:
            return {
                "status": "DEFERRED_BUDGET_EXHAUSTED",
                "fingerprint": str(finding.get("fingerprint") or ""),
                "attempt": attempt_no,
                "max_attempts": max_attempts,
                "reason": "verification budget exhausted; finding remains pending for resume",
            }
        _, bundle = build_verify_bundle(
            root,
            state_dir,
            finding,
            attempt=attempt_no,
            max_attempts=max_attempts,
            last_diagnostic=last_diag,
        )
        proposal = proposer(bundle) or {}
        if remaining is not None:
            remaining -= 1
        result = execute_verify_attempt(
            root=root,
            state_dir=state_dir,
            supervisor=supervisor,
            finding=finding,
            positive=str(proposal.get("positive") or ""),
            negative=str(proposal.get("negative") or ""),
            max_attempts=max_attempts,
            attempt=attempt_no,
            budget_remaining=(None if remaining is None else remaining + 1),
            auto_settle_terminal=auto_settle_terminal,
        )
        if result.get("status") != "RETRY":
            return result
        last_diag = result.get("diagnostic")

    return {
        "status": "REJECTED",
        "fingerprint": str(finding.get("fingerprint") or ""),
        "attempt": max_attempts,
        "max_attempts": max_attempts,
    }


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "HARD_MAX_ATTEMPTS",
    "verify_dir_for_finding",
    "ensure_locked_oracle",
    "build_verify_bundle",
    "record_attempt_diagnostic",
    "execute_verify_attempt",
    "run_verification_loop",
    "normalize_max_attempts",
    "strip_runner_wrapper",
    "precheck_shell_tokens",
]
