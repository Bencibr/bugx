"""Visual comparison of matched browser captures and repair rechecks."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from core.probes.ui_evidence import CASE_GROUPS, combine_status, screenshot_bytes

CHANNEL_DELTA = 16
MAX_PIXELS = 8_000_000


def _image(directory: Path, record: dict, mode: str) -> Any:
    from PIL import Image

    raw = screenshot_bytes(directory, record)
    with Image.open(io.BytesIO(raw), formats=("PNG",)) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("visual comparison image is too large")
        return image.convert(mode)


def _case_map(manifest: dict) -> dict[str, dict]:
    return {row["name"]: row for group in CASE_GROUPS for row in manifest[group]}


def _profile(case: dict) -> tuple:
    details = case["inspection"].get("viewport", {})
    return (case["browser"], case["viewport"], details.get("width"), details.get("height"),
            details.get("dpr"), case.get("zoom", {}).get("requested"))


def _png(image: Any, path: Path) -> dict:
    image.save(path, format="PNG")
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _compare_case(reference: dict, current: dict, reference_dir: Path, output: Path,
                  max_ratio: float) -> dict:
    from PIL import Image, ImageChops, ImageOps

    name = current["name"]
    if current["status"] != "PASS" or reference["status"] != "PASS":
        raise ValueError("equivalent browser state was not established")
    if _profile(reference) != _profile(current):
        raise ValueError("browser version, zoom or viewport differs")
    first, = reference["evidence"]["screenshots"]
    second, = current["evidence"]["screenshots"]
    if first.get("mask_counts") != second.get("mask_counts"):
        raise ValueError("visible mask selector match counts differ")
    before, after = _image(reference_dir, first, "RGB"), _image(output.parent, second, "RGB")
    if before.size != after.size:
        raise ValueError("screenshot pixel dimensions differ")
    red, green, blue = ImageChops.difference(before, after).split()
    delta = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = delta.point([0] * (CHANNEL_DELTA + 1) + [255] * (255 - CHANNEL_DELTA))
    changed = mask.histogram()[255]
    ratio = changed / (mask.width * mask.height)
    case = {"name": name, "status": "FAIL" if ratio > max_ratio else "PASS",
            "reference_sha256": first["sha256"], "current_sha256": second["sha256"],
            "size": list(mask.size), "changed_pixels": changed, "changed_ratio": ratio,
            "bounds": mask.getbbox(), "mask_counts": second.get("mask_counts", {})}
    if changed:
        backdrop = ImageOps.grayscale(after).convert("RGB")
        marked = Image.composite(Image.new("RGB", mask.size, "#e81c74"), backdrop, mask)
        case["diff"] = _png(marked, output / f"{name}.png")
        case["change_mask"] = _png(mask, output / "masks" / f"{name}.png")
    return case


def compare_visual(reference: dict, current: dict, reference_dir: Path, directory: Path,
                   max_ratio: float) -> dict:
    """Compare equivalent cases captured by the same Chromium configuration."""
    output = directory / "visual-diff"
    output.mkdir(mode=0o700)
    (output / "masks").mkdir(mode=0o700)
    plan = {"reference_run_id": reference["run_id"], "reference_url": reference["url"],
            "max_changed_ratio": max_ratio, "channel_delta": CHANNEL_DELTA}
    previous, observed = _case_map(reference), _case_map(current)
    if previous.keys() != observed.keys() or reference["theme"] != current["theme"]:
        return {**plan, "status": "UNVERIFIED", "cases": [],
                "reason": "reference and current capture plans differ"}
    cases = []
    for name, item in observed.items():
        try:
            cases.append(_compare_case(previous[name], item, reference_dir, output, max_ratio))
        except (OSError, ValueError) as exc:
            cases.append({"name": name, "status": "UNVERIFIED", "reason": str(exc)})
    return {**plan, "status": combine_status([case["status"] for case in cases]), "cases": cases}


def visual_comparable(report: dict) -> bool:
    return bool(report["cases"]) and all(case["status"] in {"PASS", "FAIL"} for case in report["cases"])


def _mask(directory: Path, case: dict) -> Any:
    from PIL import Image

    if case["changed_pixels"]:
        return _image(directory, case["change_mask"], "L")
    return Image.new("L", tuple(case["size"]))


def visual_regressions(before: dict, after: dict, directory: Path) -> list[str]:
    """Detect newly changed pixels against the same reference after a repair."""
    from PIL import ImageChops

    if not visual_comparable(before) or not visual_comparable(after):
        return ["visual comparison lost coverage"]
    old = {row["name"]: row for row in before["cases"]}
    new = {row["name"]: row for row in after["cases"]}
    if (old.keys() != new.keys() or before["reference_run_id"] != after["reference_run_id"]
            or before["max_changed_ratio"] != after["max_changed_ratio"]):
        return ["visual reference or comparison plan changed"]
    reasons = []
    for name, current in new.items():
        previous = old[name]
        if previous["size"] != current["size"] or previous["reference_sha256"] != current["reference_sha256"]:
            reasons.append(f"{name}: visual baseline changed")
            continue
        added = ImageChops.subtract(_mask(directory, current), _mask(directory, previous))
        pixels = added.histogram()[255]
        fraction = pixels / (added.width * added.height)
        if fraction > before["max_changed_ratio"] or (
            previous["status"] == "PASS" and current["status"] == "FAIL"
        ):
            reasons.append(f"{name}: visual regression ({pixels} newly changed pixels)")
    return reasons
