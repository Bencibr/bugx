"""Summarize and verify Web UI audit receipts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from core.probes.ui_accessibility import AXE_SHA256, AXE_VERSION
from core.probes.ui_contract import STATES
from core.probes.ui_storybook import STORY_ROOT, verify_story_url
from core.secure_atomic import atomic_write_json

MANIFEST_SCHEMA = "bugx.ui-audit.v3"
EVIDENCE_SCHEMA = "bugx.ui-evidence.v1"
CASE_GROUPS = ("captures", "journeys", "keyboard_journeys")
STATUSES = ("FAIL", "BLOCKED", "UNVERIFIED", "PASS")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def combine_status(statuses: list[str]) -> str:
    if not statuses or any(status not in STATUSES for status in statuses):
        return "UNVERIFIED"
    return next(value for value in STATUSES if value in statuses)


def _check(status: str, group: str, message: str) -> dict:
    return {"status": status, "evidence": f"manifest.json#{group}", "message": message}


def summarize_ui(manifest: dict) -> dict:
    """Derive audit statuses from the recorded browser observations."""
    captures = manifest["captures"]
    journeys = manifest["journeys"]
    keyboard = manifest["keyboard_journeys"]
    rules = manifest["design_rules"]
    inspected = [row["inspection"] for row in captures if row["status"] == "PASS"]
    availability = [row["status"] for row in captures if row["status"] != "PASS"]
    contrasts = [row["text_contrast"]["status"] for row in inspected]
    contrasts.extend(check["status"] for row in inspected for check in row["non_text_contrast"])
    if not rules["contrast"]:
        contrasts.append("UNVERIFIED")
    layouts = [check["status"] for row in inspected for check in row["anchors"]]
    layouts.extend("FAIL" if row["horizontal_overflow_px"] > 1 else "PASS" for row in inspected)
    if not rules["reflow"]:
        layouts.append("UNVERIFIED")
    states = {
        state: combine_status([row["status"] for row in journeys if row["state"] == state])
        for state in STATES
    }
    a11y = []
    for row in captures + journeys + keyboard:
        if row["status"] == "BLOCKED":
            a11y.append("BLOCKED")
        else:
            a11y.append(row["inspection"]["axe"]["status"])
            if not row["evidence"]["accessibility"]["snapshot"]:
                a11y.append("UNVERIFIED")
    checks = {
        "keyboard-focus": _check(
            combine_status([row["status"] for row in keyboard]), "keyboard_journeys",
            "Declared keyboard traversal, focused target and visible indicator changes."),
        "aria-tree": _check(combine_status(a11y), "captures",
                            "ARIA snapshots and automated axe-core rules in rendered cases."),
        "contrast": _check(combine_status(availability + contrasts), "captures",
                           "Viewport text and declared control/focus color pairs."),
        "reflow-zoom": _check(combine_status(availability + layouts), "captures",
                              "Breakpoints, native 200%/400% zoom, overflow and declared anchors."),
        "state-feedback": _check(combine_status(list(states.values())), "journeys",
                                 "Declared loading, error, empty and disabled journeys."),
    }
    design = combine_status(availability + [
        row["status"] for item in inspected for row in item["tokens"] + item["components"]
    ])
    return {
        "status": combine_status([design, *(row["status"] for row in checks.values())]),
        "design_system": {"status": design, "path": rules["path"], "sha256": rules["sha256"],
                          **({"token_source": rules["token_source"]} if rules.get("token_source") else {})},
        "state_status": states,
        "dynamic_evidence": {"checks": checks},
    }


def make_manifest(*, root: Path, url: str, theme: str, rules: dict, planned: dict,
                  captures: list[dict], journeys: list[dict], keyboard: list[dict]) -> dict:
    manifest = {
        "schema_version": MANIFEST_SCHEMA, "run_id": uuid4().hex, "project_root": str(root.resolve()),
        "url": url, "theme": theme, "design_rules": rules, "planned_cases": planned,
        "captures": captures, "journeys": journeys, "keyboard_journeys": keyboard,
    }
    return {**manifest, **summarize_ui(manifest)}


def _bounded_bytes(path: Path, maximum: int) -> bytes:
    if not path.is_file():
        raise ValueError(f"expected an evidence file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f"evidence file exceeds size limit: {path.name}")
    return data


def _artifact_path(directory: Path, raw: str) -> Path:
    if not isinstance(raw, str):
        raise ValueError("evidence artifact path must be text")
    path = directory / raw
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(directory) or path != resolved:
        raise ValueError("evidence artifact must be inside the receipt directory without symlinks")
    return resolved


def screenshot_bytes(directory: Path, record: dict) -> bytes:
    """Read a confined PNG and verify its recorded content hash."""
    path = _artifact_path(directory.resolve(), record["path"])
    data = _bounded_bytes(path, 20 * 1024 * 1024)
    if not data.startswith(PNG_MAGIC) or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise ValueError(f"invalid screenshot or SHA-256 mismatch: {path.name}")
    return data


def _verify_cases(manifest: dict, directory: Path) -> None:
    if not manifest["captures"]:
        raise ValueError("UI manifest has no viewport captures")
    names = set()
    for group in CASE_GROUPS:
        cases = manifest[group]
        if not isinstance(cases, list) or len(cases) > 32:
            raise ValueError(f"invalid UI case list: {group}")
        if [case["name"] for case in cases] != manifest["planned_cases"][group]:
            raise ValueError(f"UI case plan and receipts differ: {group}")
        for case in cases:
            if case["name"] in names or case["status"] not in STATUSES:
                raise ValueError("duplicate UI case or invalid status")
            names.add(case["name"])
            if case["status"] == "BLOCKED":
                continue
            width, height = (case["viewport"][key] for key in ("width", "height"))
            if type(width) is not int or type(height) is not int or min(width, height) <= 0:
                raise ValueError(f"invalid viewport: {case['name']}")
            if not case["url"] or not case["browser"]["version"]:
                raise ValueError(f"missing browser identity: {case['name']}")
            record, = case["evidence"]["screenshots"]
            image = screenshot_bytes(directory, record)
            size = tuple(int.from_bytes(image[index:index + 4], "big") for index in (16, 20))
            if len(image) < 24 or size != (width, height):
                raise ValueError(f"PNG and browser viewport dimensions disagree: {case['name']}")
            inspection = case["inspection"]
            axe = inspection["axe"]
            if "story" in case:
                verify_story_url(case["url"], case["story"])
                if axe.get("scope") != STORY_ROOT:
                    raise ValueError(f"axe-core scope does not match Storybook canvas: {case['name']}")
            if axe["version"] != AXE_VERSION or axe["sha256"] != AXE_SHA256:
                raise ValueError(f"unrecognized axe-core receipt: {case['name']}")
            if axe["status"] != "BLOCKED":
                expected_axe = "FAIL" if axe["violations"] else (
                    "UNVERIFIED" if axe["incomplete"] or not axe["passed_rules"] else "PASS"
                )
                if axe["status"] != expected_axe:
                    raise ValueError(f"axe-core outcome and observations disagree: {case['name']}")
            if group == "keyboard_journeys":
                steps = [step["status"] for step in inspection["steps"]]
                if len(steps) != inspection["planned_steps"]:
                    steps.append("UNVERIFIED")
                if case["status"] != combine_status(steps):
                    raise ValueError(f"keyboard outcome and steps disagree: {case['name']}")
            elif group == "journeys":
                assertions = inspection["assertions"]
                expected = "PASS" if assertions and all(row["passed"] is True for row in assertions) else "FAIL"
                if case["status"] != expected:
                    raise ValueError(f"state outcome and assertions disagree: {case['name']}")


def write_ui_evidence(directory: Path, manifest: dict) -> None:
    """Publish one hashed manifest and its companion evidence reference."""
    directory = directory.resolve()
    _verify_cases(manifest, directory)
    path = directory / "manifest.json"
    atomic_write_json(path, manifest)
    atomic_write_json(directory / "dynamic-evidence.json", {
        "schema_version": EVIDENCE_SCHEMA, "run_id": manifest["run_id"],
        "manifest": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })


def load_ui_evidence(evidence: Path, root: Path) -> dict:
    """Verify the exact manifest, project, case coverage and screenshot digests."""
    directory = evidence.parent.resolve()
    reference = json.loads(_bounded_bytes(evidence, 32 * 1024))
    if (not isinstance(reference, dict) or reference.get("schema_version") != EVIDENCE_SCHEMA
            or reference.get("manifest") != "manifest.json"):
        raise ValueError("dynamic evidence must be a versioned UI manifest reference")
    path = _artifact_path(directory, reference["manifest"])
    raw = _bounded_bytes(path, 32 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != reference.get("sha256"):
        raise ValueError("UI manifest SHA-256 mismatch")
    manifest = json.loads(raw)
    run_id = manifest.get("run_id")
    if (manifest.get("schema_version") != MANIFEST_SCHEMA or not isinstance(run_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", run_id) or reference.get("run_id") != run_id):
        raise ValueError("UI run identity mismatch")
    if manifest.get("project_root") != str(root.resolve()):
        raise ValueError("UI evidence is for a different project root")
    _verify_cases(manifest, directory)
    verified = summarize_ui(manifest)["dynamic_evidence"]
    if verified != manifest.get("dynamic_evidence"):
        raise ValueError("UI check summary disagrees with browser observations")
    return verified["checks"]
