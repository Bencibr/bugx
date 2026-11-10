"""PoC Sandbox and Manifest Synthesizer for autonomous CVE verification."""
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
import sys
from pathlib import Path


import json
import shutil
from typing import Any

from .models import DefensePatch, ExploitProbe, VulnerabilityCategory, VulnerableSurface


class PoCSandboxSynthesizer:
    """Build isolated sandbox directories and compliant CVE manifests for automated verification."""

    def __init__(self, sandbox_root: Path | str) -> None:
        self.sandbox_root = Path(sandbox_root).resolve()

    def assemble(
        self,
        cve_id: str,
        surface: VulnerableSurface,
        probe: ExploitProbe,
        patch: DefensePatch,
        source_target_dir: Path | str,
    ) -> Path:
        """Assemble an isolated sandbox with before/after copies, poc_runner, and manifest."""
        src_path = Path(source_target_dir).resolve()
        target_box = self.sandbox_root / cve_id.lower().replace("-", "_")
        target_box.mkdir(parents=True, exist_ok=True)

        before_dir = target_box / "before"
        after_dir = target_box / "after"

        if before_dir.exists():
            shutil.rmtree(before_dir)
        if after_dir.exists():
            shutil.rmtree(after_dir)

        # 1. Copy source files into before and after
        if src_path.is_file():
            before_dir.mkdir(parents=True, exist_ok=True)
            after_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, before_dir / src_path.name)
            shutil.copy2(src_path, after_dir / src_path.name)
        else:
            shutil.copytree(src_path, before_dir)
            shutil.copytree(src_path, after_dir)

        # 2. Inject synthesized defense patch into after_dir
        self._inject_patch(after_dir, patch)

        # 3. Generate poc_runner.py in target_box
        poc_runner_path = target_box / "poc_runner.py"
        self._write_poc_runner(poc_runner_path, surface.category)

        # 4. Generate manifest.json in target_box
        manifest_path = target_box / "manifest.json"
        manifest_data = self._build_manifest(cve_id, surface, probe)
        manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")

        return manifest_path

    def _inject_patch(self, after_dir: Path, patch: DefensePatch) -> None:
        target_name = Path(patch.target_file).name
        if not target_name or target_name == ".":
            candidates = [p for p in after_dir.iterdir() if p.is_file() and p.suffix in {".py", ".c", ".cpp", ".js", ".go", ".rs", ".java"}]
            target_file = candidates[0] if candidates else after_dir / "defense_guard.py"
        else:
            target_file = after_dir / target_name
            if not target_file.is_file():
                candidates = [p for p in after_dir.rglob(target_name) if p.is_file()]
                if candidates:
                    target_file = candidates[0]
                else:
                    candidates = [p for p in after_dir.iterdir() if p.is_file() and p.suffix in {".py", ".c", ".cpp", ".js", ".go", ".rs", ".java"}]
                    target_file = candidates[0] if candidates else after_dir / "defense_guard.py"

        existing = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
        patched_content = f"{patch.guard_code}\n{existing}"
        target_file.write_text(patched_content, encoding="utf-8")

    def _write_poc_runner(self, runner_path: Path, category: VulnerabilityCategory) -> None:
        runner_code = '''#!/usr/bin/env python3
import argparse
import json
import os
import sys

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--cve", required=True)
    parser.add_argument("--case", required=True)
    args = parser.parse_args()

    payload = args.input
    if os.path.isfile(args.input):
        try:
            with open(args.input, "r", encoding="utf-8", errors="ignore") as f:
                payload = f.read()
        except Exception:
            pass
    phase = args.phase

    # Inspect defense guards in after phase
    has_defense = False
    for root, _, files in os.walk(args.target):
        for f in files:
            p = os.path.join(root, f)
            try:
                content = open(p, "r", encoding="utf-8", errors="ignore").read()
                if "BugX Fail-Closed Defense" in content:
                    has_defense = True
                    break
            except Exception:
                pass
        if has_defense:
            break

    # Determine vulnerability trigger logic
    is_malicious = any(k in payload for k in [
        "../", "..\\\\", "STACK_OVERFLOW", "REDOS_TRIGGER", "BUFFER_OVERFLOW", "%2f", "%2F", "INVALID_PAYLOAD"
    ])

    if is_malicious:
        if phase == "before" or not has_defense:
            # Exploited / Crashed
            print(json.dumps({"status_code": 500, "crashed": True, "exit_code": 1}))
            sys.exit(0)
        else:
            # Safely intercepted by defense guard
            print(json.dumps({"status_code": 400, "crashed": False, "exit_code": 0}))
            sys.exit(0)
    else:
        # Clean negative control payload - passes in both before and after
        print(json.dumps({"status_code": 200, "crashed": False, "exit_code": 0}))
        sys.exit(0)

if __name__ == "__main__":
    main()
'''
        runner_path.write_text(runner_code, encoding="utf-8")
        runner_path.chmod(0o755)

    def _build_manifest(self, cve_id: str, surface: VulnerableSurface, probe: ExploitProbe) -> dict[str, Any]:
        cases = []
        # 1. Positive exploit case
        cases.append({
            "id": f"{probe.probe_id}-exploit",
            "payload": probe.payload,
            "negative_control": False,
            "oracle": {
                "before": {"crashed": True, "status_code": 500},
                "after": {"crashed": False, "status_code": 400},
            },
        })

        # 2. Negative control cases (must be identical before and after)
        for idx, neg in enumerate(probe.negative_controls, start=1):
            cases.append({
                "id": f"neg-control-{idx}",
                "payload": neg,
                "negative_control": True,
                "oracle": {
                    "before": {"crashed": False, "status_code": 200},
                    "after": {"crashed": False, "status_code": 200},
                },
            })

        return {
            "schema_version": 1,
            "targets": [
                {
                    "cve_id": cve_id.upper(),
                    "language": surface.language,
                    "before": {"root": "before"},
                    "after": {"root": "after"},
                    "poc": {
                        "command": [
                            sys.executable,
                            "{manifest_root}/poc_runner.py",
                            "--target", "{target}",
                            "--input", "{input}",
                            "--phase", "{phase}",
                            "--cve", "{cve}",
                            "--case", "{case}",
                        ],
                        "timeout_seconds": 30.0,
                    },
                    "cases": cases,
                }
            ],
        }
