"""Plan and verify bounded CSS token repairs for the UI audit."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import signal
import stat
from collections import Counter
from pathlib import Path
from typing import Callable

from core.probes.ui_contract import UIContract, UIContractError
from core.probes.ui_evidence import CASE_GROUPS, write_ui_evidence
from core.probes.ui_visual_diff import visual_comparable, visual_regressions
from core.secure_atomic import atomic_write_bytes, atomic_write_json, reject_symlink_components

_LITERAL = re.compile(r"(?:#[0-9a-fA-F]{3,8}|[-\w. %]+|(?:rgba?|hsla?)\([-+\d.,% /]+\))")
_DECLARATION = re.compile(r"(?m)^[ \t]*(--[a-zA-Z_][\w-]*)[ \t]*:[ \t]*([^;\r\n]+);[ \t]*\r?$")
_RANK = {"PASS": 0, "UNVERIFIED": 1, "BLOCKED": 2, "FAIL": 3}


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(root: Path, css: Path) -> Path:
    path = Path(os.path.abspath(root / css))
    if not path.is_relative_to(root) or path.suffix.lower() != ".css":
        raise UIContractError("repair target must be a CSS file within the project")
    reject_symlink_components(path)
    if not path.is_file() or path.resolve(strict=True) != path:
        raise UIContractError("repair target must be an existing regular file without symlink ancestors")
    return path


def _proposal(before: bytes, contract: UIContract, baseline: dict) -> tuple[bytes, list[str]]:
    source = before.decode("utf-8")
    # Keep source offsets when masking comments, so only the value span changes.
    masked = re.sub(r"/\*.*?\*/", lambda match: re.sub(r"[^\r\n]", " ", match[0]), source, flags=re.S)
    block = re.fullmatch(r"\s*:root\s*\{([^{}]*)\}\s*", masked)
    if not block:
        raise UIContractError("repair requires a token stylesheet with a single :root block")
    declarations: dict[str, list[tuple[int, int]]] = {}
    for match in _DECLARATION.finditer(block[1]):
        start, end = match.span(2)
        declarations.setdefault(match[1], []).append((block.start(1) + start, block.start(1) + end))
    replacements = []
    for name, desired in (contract.tokens or {}).items():
        spans = declarations.get(name, [])
        if not desired or not _LITERAL.fullmatch(desired) or len(spans) != 1:
            continue
        start, end = spans[0]
        old = source[start:end]
        if not _LITERAL.fullmatch(old.strip()) or old == desired:
            continue
        rows = [
            row for capture in baseline["captures"] for row in capture["inspection"]["tokens"]
            if row["id"] == name
        ]
        if rows and all(row["status"] == "FAIL" and row["actual"] == old.strip() for row in rows):
            replacements.append((start, start + len(old.rstrip()), desired, name))
    for start, end, value, _ in sorted(replacements, reverse=True):
        source = source[:start] + value + source[end:]
    return source.encode(), [item[-1] for item in replacements]


def _measurements(capture: dict) -> dict[tuple, str]:
    report = capture["inspection"]
    rows = {(group, row["id"]): row["status"] for group in ("tokens", "anchors")
            for row in report[group]}
    for group in ("components", "non_text_contrast"):
        for item in report[group]:
            rows[(group, item["id"])] = item["status"]
            for row in item["checks"]:
                rows[(group, item["id"], row.get("selector"), row["id"])] = row["status"]
    return rows


def _contrast_failures(check: dict) -> Counter:
    return Counter((item["selector"], item["threshold"]) for item in check["failures"])


def _worsened_contrast(before: dict, after: dict) -> bool:
    first, second = before["min_ratio"], after["min_ratio"]
    return bool(after["failures"] and first is not None and second is not None and second < first)


def _regressions(before: dict, after: dict, names: list[str], directory: Path) -> list[str]:
    previous = {row["name"]: row for row in before["captures"]}
    current = {row["name"]: row for row in after["captures"]}
    if set(previous) != set(current):
        return ["viewport set changed"]
    reasons = []
    for name, old in previous.items():
        new = current[name]
        if new["status"] != "PASS" or new["url"] != old["url"]:
            reasons.append(f"{name}: target capture unavailable or changed")
            continue
        old_rows, new_rows = _measurements(old), _measurements(new)
        if any(new_rows.get(("tokens", token)) != "PASS" for token in names):
            reasons.append(f"{name}: edited token did not match its expected rendered value")
        if set(old_rows) - set(new_rows) or any(
            _RANK[status] > _RANK[old_rows.get(key, "PASS")] for key, status in new_rows.items()
        ):
            reasons.append(f"{name}: design, layout or indicator measurement regressed")
        first, second = old["inspection"], new["inspection"]
        old_text, new_text = first["text_contrast"], second["text_contrast"]
        if (second["horizontal_overflow_px"] > max(1, first["horizontal_overflow_px"]) or
                old_text["samples"] > new_text["samples"] or
                new_text["unknown_count"] > old_text["unknown_count"] or
                _contrast_failures(new_text) - _contrast_failures(old_text) or
                _worsened_contrast(old_text, new_text) or
                (first["keyboard_focus"] and not second["keyboard_focus"]) or
                any(new["evidence"][f"{key}_count"] > old["evidence"][f"{key}_count"]
                    for key in ("console", "page_errors", "request_failures", "http_errors"))):
            reasons.append(f"{name}: overflow, focus, contrast coverage or runtime regressed")
    for group in CASE_GROUPS:
        old_cases = {row["name"]: row for row in before[group]}
        new_cases = {row["name"]: row for row in after[group]}
        if old_cases.keys() != new_cases.keys():
            reasons.append(f"{group}: audited case set changed")
            continue
        for name, new in new_cases.items():
            old = old_cases[name]
            if _RANK[new["status"]] > _RANK[old["status"]]:
                reasons.append(f"{name}: case result regressed")
            first = old.get("inspection", {}).get("axe", {})
            second = new.get("inspection", {}).get("axe", {})
            if second and (_RANK[second["status"]] > _RANK[first.get("status", "UNVERIFIED")]
                           or _axe_findings(second) - _axe_findings(first)):
                reasons.append(f"{name}: accessibility findings regressed")
    old_checks = before["dynamic_evidence"]["checks"]
    for key, row in after["dynamic_evidence"]["checks"].items():
        if _RANK[row["status"]] > _RANK[old_checks[key]["status"]]:
            reasons.append(f"{key}: aggregate result regressed")
    if "visual_regression" in before:
        reasons.extend(visual_regressions(before["visual_regression"], after["visual_regression"], directory))
    return reasons


def _axe_findings(result: dict) -> Counter:
    return Counter({(kind, row["id"]): row["node_count"]
                    for kind in ("violations", "incomplete") for row in result.get(kind, ())})


def repair_tokens(
    root: Path, css: Path, directory: Path, contract: UIContract, baseline: dict,
    *, apply: bool, verify: Callable[[Path], dict],
) -> tuple[dict, dict | None]:
    """Preview an exact token diff or write it and verify a fresh page, rolling back on regression."""
    target = _target(root, css)
    receipt_path = directory / "repair.json"
    receipt = {"status": "BLOCKED", "target": str(target)}
    if any(capture["status"] != "PASS" for capture in baseline["captures"]):
        receipt["reason"] = "one or more baseline captures are unavailable"
        atomic_write_json(receipt_path, receipt)
        return receipt, None
    if "visual_regression" in baseline and not visual_comparable(baseline["visual_regression"]):
        receipt["reason"] = "visual reference coverage is incomplete"
        atomic_write_json(receipt_path, receipt)
        return receipt, None
    with target.open("rb") as stream:
        before = stream.read(256 * 1024 + 1)
    if len(before) > 256 * 1024:
        raise UIContractError("repair stylesheet exceeds 256 KiB")
    after, names = _proposal(before, contract, baseline)
    if not names:
        receipt.update(status="NO_CHANGES", reason="no unambiguous declared token mismatch")
        atomic_write_json(receipt_path, receipt)
        return receipt, None
    diff = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in difflib.unified_diff(before.decode().splitlines(keepends=True),
                                        after.decode().splitlines(keepends=True),
                                        fromfile=f"a/{target.relative_to(root)}",
                                        tofile=f"b/{target.relative_to(root)}")
    )
    patch = directory / "repair.patch"
    atomic_write_bytes(patch, diff.encode(), mode=0o600)
    receipt.update(status="PREVIEW", tokens=names, patch=str(patch),
                   before_sha256=_fingerprint(before), proposed_sha256=_fingerprint(after))
    atomic_write_json(receipt_path, receipt)
    if not apply:
        return receipt, None

    lock = target.with_name(f".{target.name}.bugx-ui.lock")
    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        receipt.update(status="BLOCKED", reason="another CSS repair owns the stylesheet lock")
        atomic_write_json(receipt_path, receipt)
        return receipt, None
    os.close(lock_fd)
    backup = directory / "repair-before.css"
    written = False
    verified = None
    old_handler = None

    def interrupt(_sig: int, _frame: object) -> None:
        raise InterruptedError("repair interrupted")

    try:
        if _target(root, css).read_bytes() != before:
            raise UIContractError("stylesheet changed after the repair preview")
        mode = stat.S_IMODE(target.stat().st_mode)
        atomic_write_bytes(backup, before, mode=0o600)
        receipt.update(status="APPLYING", backup=str(backup))
        atomic_write_json(receipt_path, receipt)
        old_handler = signal.signal(signal.SIGTERM, interrupt)
        written = True
        atomic_write_bytes(target, after, mode=mode)
        rerun = directory / "after"
        rerun.mkdir(mode=0o700)
        verified = verify(rerun)
        write_ui_evidence(rerun, verified)
        receipt["verification"] = str(rerun / "manifest.json")
        reasons = _regressions(baseline, verified, names, directory)
        if reasons:
            raise UIContractError("; ".join(reasons))
        if _target(root, css).read_bytes() != after:
            raise UIContractError("stylesheet changed during verification")
        receipt["status"] = "VERIFIED_APPLIED"
    except BaseException as exc:
        receipt["reason"] = str(exc) or type(exc).__name__
        receipt["status"] = "BLOCKED"
        if written:
            try:
                current = _target(root, css).read_bytes()
                if current == after:
                    atomic_write_bytes(target, before, mode=mode)
                elif current != before:
                    raise UIContractError("stylesheet contains other edits")
                if _target(root, css).read_bytes() != before:
                    raise UIContractError("rollback byte verification failed")
                receipt["status"] = "ROLLED_BACK"
            except (OSError, UIContractError) as rollback:
                receipt.update(status="RECOVERY_REQUIRED", rollback_error=str(rollback))
        verified = None
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            atomic_write_json(receipt_path, receipt)
            raise
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)
        lock.unlink(missing_ok=True)
    atomic_write_json(receipt_path, receipt)
    return receipt, verified
