"""Closed-loop verification and verifiable receipt generation."""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from .models import HuntReceipt

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts" / "cve"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

try:
    from run_oss_cve_exploit_verify import run_manifest  # type: ignore
except ImportError:
    run_manifest = None


class AutoHuntVerifier:
    """Execute manifest-driven closed-loop verification and issue anti-tamper receipts."""

    def __init__(self, output_receipt_path: Path | str | None = None) -> None:
        self.output_receipt_path = (
            Path(output_receipt_path).resolve()
            if output_receipt_path
            else REPO_ROOT / ".bugx" / "evidence" / "cve-hunt-receipt.json"
        )

    def verify(
        self,
        manifest_path: Path | str,
        target_root: str,
        surfaces_count: int,
        exploits_count: int,
        patches_count: int,
    ) -> HuntReceipt:
        manifest_file = Path(manifest_path).resolve()
        if not manifest_file.exists():
            raise FileNotFoundError(f"Manifest not found: {manifest_file}")

        evidence_dir = self.output_receipt_path.parent
        evidence_dir.mkdir(parents=True, exist_ok=True)
        driver_out = evidence_dir / f"driver-{manifest_file.stem}-out.json"

        if run_manifest is None:
            raise RuntimeError("Could not import run_manifest from scripts/cve/run_oss_cve_exploit_verify.py")

        report = run_manifest(manifest_file, driver_out)
        status = report.get("status", "BLOCKED")

        controls = report.get("negative_controls", {})
        total_controls = controls.get("total", 0)
        suppressed_controls = controls.get("suppressed", 0)
        neg_pass_rate = (suppressed_controls / total_controls) if total_controls > 0 else 1.0

        now = datetime.datetime.now(datetime.timezone.utc)
        receipt_dict: dict[str, Any] = {
            "schema_version": "1.0",
            "receipt_type": "autonomous_cve_hunting_closure",
            "target_root": str(target_root),
            "status": "PASSED" if status == "PASS" and neg_pass_rate == 1.0 else "FAILED",
            "surfaces_scouted": surfaces_count,
            "exploits_tested": exploits_count,
            "patches_verified": patches_count,
            "negative_control_pass_rate": neg_pass_rate,
            "driver_status": status,
            "driver_report_sha256": report.get("report_sha256", ""),
            "timestamp": now.timestamp(),
            "iso_time": now.isoformat(),
            "targets": report.get("targets", []),
        }

        canonical_bytes = json.dumps(receipt_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
        receipt_dict["sha256_hash"] = receipt_sha256

        self.output_receipt_path.write_text(
            json.dumps(receipt_dict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return HuntReceipt(
            schema_version="1.0",
            target_root=str(target_root),
            status=receipt_dict["status"],
            surfaces_scouted=surfaces_count,
            exploits_tested=exploits_count,
            patches_verified=patches_count,
            negative_control_pass_rate=neg_pass_rate,
            sha256_hash=receipt_sha256,
            timestamp=receipt_dict["timestamp"],
            iso_time=receipt_dict["iso_time"],
            cases=report.get("targets", []),
        )
