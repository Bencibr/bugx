#!/usr/bin/env python3
"""launch_phases/post_receipt.py - 标准化验收凭证收据生成与权威核验 (DOC-154)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.launch_phases.common import (
    AGENT_DIR,
    resolve_project_root,
    uses_project_layout,
    bugx_paths,
)
from core.launch_phases.session_runtime import (
    append_runtime_event,
    baseline_json,
)
from core.coverage.coverage_forensics import coverage_debt_units
from core.ledger.receipt_contract import (
    verify_receipt_coverage_contract,
    verify_receipt_terminal_contract,
)


def _pp():
    return sys.modules.get("core.launch_phases.post_phase") or sys.modules.get("launch_phases.post_phase")


def _get_pp_attr(name: str, fallback=None):
    pp = _pp()
    if pp and hasattr(pp, name):
        return getattr(pp, name)
    return fallback


def refresh_receipt_supervisor_hash(
    supervisor_path: Path, receipt_paths: Iterable[Path],
) -> None:
    """Bind already-written receipts to the current supervisor state hash."""
    if not supervisor_path.is_file():
        return
    import core.secure_atomic as secure_atomic

    digest = hashlib.sha256(supervisor_path.read_bytes()).hexdigest()
    for receipt_path in receipt_paths:
        if not receipt_path.is_file():
            continue
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        hashes = receipt.get("artifact_hashes")
        if not isinstance(hashes, dict):
            raise ValueError("acceptance receipt 缺少 artifact_hashes")
        hashes["audit-supervisor.json"] = digest
        secure_atomic.atomic_write_json(receipt_path, receipt)


def write_acceptance_receipt(runtime: dict, outcome: str) -> Path | None:
    """原子固化标准化验收凭证收据 (DOC-154 / AGENTS.md 第 6 完成门)。"""
    try:
        import core.secure_atomic as secure_atomic

        root_fn = _get_pp_attr("resolve_project_root", resolve_project_root)
        root = root_fn()
        uses_layout_fn = _get_pp_attr("uses_project_layout", uses_project_layout)
        state_dir: Path | None = None
        if uses_layout_fn():
            try:
                state_dir = bugx_paths.require(root).state_root
            except Exception:
                state_dir = None
        if state_dir is None:
            # 无项目布局时收据属于**项目运行态**，与 ``_path_values_for_root``
            # 同源：落在项目根，绝不回退到 AGENT_DIR（可执行引擎目录）。
            # 旧实现回退 AGENT_DIR 后，``state_dir.parent`` 是引擎目录的父级，
            # 镜像写盘会把收据复制到源码树里（实测出现
            # ``core/acceptance-evidence-receipt.json``），而自检按项目根找不到
            # 工件，报「工件缺失: audit-supervisor.json」。
            state_dir = root

        state_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = state_dir / "acceptance-evidence-receipt.json"

        # Git Commit
        commit_hash = "unknown"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                commit_hash = res.stdout.strip()
        except Exception:
            pass

        # Engine Version
        engine_version = "unknown"
        try:
            ver_file = Path(__file__).resolve().parent.parent / "metadata.txt"
            if ver_file.is_file():
                version: str | None = None
                build_id: str | None = None
                for line in ver_file.read_text(encoding="utf-8").splitlines():
                    if version is None and (
                        line.startswith("bugx_version:") or line.startswith("version:")
                    ):
                        version = line.split(":", 1)[1].strip().strip('"').strip("'")
                    elif build_id is None and line.startswith("build_id:"):
                        build_id = line.split(":", 1)[1].strip().strip('"').strip("'")
                if version:
                    engine_version = (
                        f"{version}+build.{build_id}" if build_id else version
                    )
        except Exception:
            pass

        # Calculate artifact hashes
        artifact_hashes: dict[str, str | None] = {}
        supervisor_payload: dict[str, Any] | None = None
        target_candidates: dict[str, list[str]] = {
            "module-coverage.md": ["module-coverage.md"],
            "audit-supervisor.json": ["supervisor.json", "audit-supervisor.json"],
            "bugx-runtime.json": ["runtime.json", "bugx-runtime.json"],
            "bugx-life.json": ["life.json", "bugx-life.json"],
            "report.md": ["report.md"],
            "findings.json": ["findings.json", "findings-snapshot.json"],
        }
        search_dirs = [state_dir, state_dir.parent]
        # 约定状态目录兜底：布局不可解析（回退项目根）时，supervisor/runtime
        # 等工件按惯例仍落在 <root>/.bugx/state——搜索链缺它会漏掉密封件
        # （audit-supervisor.json），验收核验随即以「找不到密封件」误杀。
        conventional_state = root / ".bugx" / "state"
        if conventional_state.is_dir() and all(
            Path(x) != conventional_state for x in search_dirs
        ):
            search_dirs.append(conventional_state)
        for fname, candidates in target_candidates.items():
            found_file: Path | None = None
            for search_dir in search_dirs:
                for cand in candidates:
                    candidate_path = search_dir / cand
                    if candidate_path.is_file():
                        found_file = candidate_path
                        break
                if found_file is not None:
                    break
            if found_file is not None:
                try:
                    artifact_hashes[fname] = hashlib.sha256(found_file.read_bytes()).hexdigest()
                except Exception:
                    pass
                if fname == "audit-supervisor.json":
                    try:
                        loaded = json.loads(found_file.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            supervisor_payload = loaded
                    except Exception:
                        supervisor_payload = None

        debt_units = coverage_debt_units(supervisor_payload)
        coverage_complete: bool | None = None
        if debt_units is not None:
            coverage_complete = debt_units == 0
        allow_incomplete = str(outcome or "").strip().upper() == "INCOMPLETE"
        stop_reason = runtime.get("lifecycle_stop_reason") or (
            "coverage_incomplete" if allow_incomplete else None
        )

        base_json_fn = _get_pp_attr("baseline_json", baseline_json)
        receipt_data = {
            "receipt_version": "1.0",
            "timestamp": time.time(),
            "git_commit": commit_hash,
            "engine_version": engine_version,
            "host": runtime.get("host") or "unknown",
            "session_id": runtime.get("session_id"),
            "lifecycle_id": runtime.get("lifecycle_id"),
            "round_id": runtime.get("round_id"),
            "outcome": outcome,
            "allow_incomplete": allow_incomplete,
            "lifecycle_stop_reason": stop_reason,
            "polling_policy": runtime.get("polling_policy"),
            "fix_mode": (base_json_fn() or {}).get("mode", "auto"),
            "coverage_debt_units": debt_units,
            "coverage_complete": coverage_complete,
            "artifact_hashes": artifact_hashes,
            "round_delta": {
                "lifecycle_cycles_completed": runtime.get("lifecycle_cycles_completed", 0),
                "lifecycle_findings_total": runtime.get("lifecycle_findings_total", 0),
                "lifecycle_credited_total": runtime.get("lifecycle_credited_total", 0),
                "effective_total_seconds": runtime.get("effective_total_seconds", 0),
            },
        }
        secure_atomic.atomic_write_json(receipt_path, receipt_data)
        # 仅在真实生命周期布局下镜像到上一级；项目根没有可镜像的父级。
        if uses_layout_fn() and state_dir.parent != state_dir and state_dir.parent.is_dir():
            mirror_receipt = state_dir.parent / "acceptance-evidence-receipt.json"
            try:
                secure_atomic.atomic_write_json(mirror_receipt, receipt_data)
            except Exception:
                pass
        return receipt_path
    except Exception as exc:
        append_runtime_event(runtime, "warning", time.time(), f"acceptance_receipt_failed: {exc}")
        return None


def git_repo_root(start: Path) -> Path | None:
    """从 ``start`` 向上找 git 仓库根（不依赖 CWD）。"""
    try:
        current = start.resolve()
    except Exception:
        return None
    for candidate in [current, *current.parents]:
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            continue
    return None


def resolve_receipt_artifact_dirs(receipt_path: Path, data: dict) -> list[Path]:
    """收据工件目录候选（索引收据与密封件收据统一解析）。"""
    parent = receipt_path.parent
    dirs: list[Path] = []
    bundle = str(data.get("sealed_bundle") or "").strip()
    if bundle:
        raw = Path(bundle)
        if raw.is_absolute():
            anchors = [raw]
        else:
            anchors = [parent / raw, parent.parent / raw]
            repo_root = git_repo_root(receipt_path)
            if repo_root is not None:
                anchors.append(repo_root / raw)
        for cand in anchors:
            if cand.is_dir():
                dirs.append(cand)
                if (cand / "state").is_dir():
                    dirs.append(cand / "state")
                break
    dirs.append(parent)
    if parent.name in {"receipts", "state"} and parent.parent.is_dir():
        dirs.append(parent.parent)
    if (parent / "state").is_dir():
        dirs.append(parent / "state")
    # 约定状态目录兜底（与 write_acceptance_receipt 的搜索链对称）：布局回退态
    # 下收据落项目根，而 supervisor/runtime 工件按惯例在 <项目根>/.bugx/state。
    # 缺这条会以「工件缺失」误杀真实密封件（fault-recovery e2e 实测）。
    for anchor in (parent, parent.parent):
        conventional = anchor / ".bugx" / "state"
        if conventional.is_dir() and conventional not in dirs:
            dirs.append(conventional)
    return dirs


class ReceiptVerificationResult(tuple):
    """标准化验收凭证收据核验结果元组。"""

    def __new__(cls, verified: bool, reason: str):
        return super().__new__(cls, (bool(verified), str(reason)))

    @property
    def verified(self) -> bool:
        return self[0]

    @property
    def reason(self) -> str:
        return self[1]

    def get(self, key: str, default: Any = None) -> Any:
        if key == "verified":
            return self[0]
        if key in ("reason", "note", "message"):
            return self[1]
        return default


def verify_acceptance_receipt(receipt_path: Path) -> ReceiptVerificationResult:
    """校验标准化验收凭证收据完整性与 SHA-256 哈希 (DOC-154)。"""
    if not receipt_path.is_file():
        return ReceiptVerificationResult(False, f"收据文件不存在: {receipt_path}")
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not data.get("git_commit") or data.get("git_commit") == "unknown":
            return ReceiptVerificationResult(False, "缺少有效的 Git Commit 证据")
        engine_version = str(data.get("engine_version") or "")
        if not engine_version or engine_version == "unknown":
            return ReceiptVerificationResult(False, "缺少有效的 Engine Version 声明")
        if "+build." not in engine_version:
            return ReceiptVerificationResult(
                False,
                f"Engine Version 声明必须带 build id（形如 0.1.0+build.N）："
                f"当前为 {engine_version!r}——收据无法自证是哪个引擎版本产出的",
            )
        hashes = data.get("artifact_hashes") or {}
        target_candidates: dict[str, list[str]] = {
            "module-coverage.md": ["module-coverage.md"],
            "audit-supervisor.json": ["audit-supervisor.json", "supervisor.json"],
            "bugx-runtime.json": ["bugx-runtime.json", "runtime.json"],
            "bugx-life.json": ["bugx-life.json", "life.json"],
            "report.md": ["report.md"],
            "findings.json": ["findings.json", "findings-snapshot.json"],
        }
        candidate_dirs = resolve_receipt_artifact_dirs(receipt_path, data)

        located: dict[str, Path] = {}
        for fname, expected_hash in hashes.items():
            if expected_hash is None:
                continue
            candidates = target_candidates.get(fname, [fname])
            found_file: Path | None = None
            for cdir in candidate_dirs:
                for cand in candidates:
                    p = cdir / cand
                    if p.is_file():
                        found_file = p
                        break
                if found_file is not None:
                    break
            if found_file is None:
                return ReceiptVerificationResult(False, f"工件缺失: {fname}（收据声明封存了该工件，但磁盘上找不到）")
            located[fname] = found_file

        for fname, expected_hash in hashes.items():
            if expected_hash is None or fname not in located:
                continue
            actual = hashlib.sha256(located[fname].read_bytes()).hexdigest()
            if actual != expected_hash:
                return ReceiptVerificationResult(False, f"工件哈希漂移: {fname} 预期 {expected_hash}，实际 {actual}")

        artifact_files = {
            "audit-supervisor.json": located["audit-supervisor.json"]
            if "audit-supervisor.json" in located
            else Path("/nonexistent"),
            "bugx-life.json": located["bugx-life.json"]
            if "bugx-life.json" in located
            else Path("/nonexistent"),
        }
        contract_errors, _contract_warnings = verify_receipt_coverage_contract(
            data,
            artifact_files,
            sealed_life_required=False,
        )
        if contract_errors:
            return ReceiptVerificationResult(False, "覆盖债务口径校验失败: " + "；".join(contract_errors[:3]))
        terminal_errors, _terminal_warnings = verify_receipt_terminal_contract(data)
        if terminal_errors:
            return ReceiptVerificationResult(False, "终态口径校验失败: " + "；".join(terminal_errors[:3]))
        return ReceiptVerificationResult(True, "验收收据校验通过")
    except Exception as exc:
        return ReceiptVerificationResult(False, f"收据解析异常: {exc}")
