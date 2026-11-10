#!/usr/bin/env python3
"""Candidate finding revalidation and CI test coverage proposal for launch phases."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.launch_phases.common import resolve_project_root, uses_project_layout
from core.launch_phases.finding_settlement import findings_payload_path
from core.launch_phases.session_runtime import (
    append_runtime_event,
    load_runtime,
    runtime_lock,
    save_runtime,
)

try:
    from core import bugx_paths
except ImportError:  # pragma: no cover - 隔离测试环境
    bugx_paths = None

try:
    from core.governance import dependency_lock
except ImportError:  # pragma: no cover - 隔离测试环境
    dependency_lock = None

def inside_project(root: Path, raw_path: str) -> Path | None:
    """Resolve a candidate artifact without permitting path traversal."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        resolved_root = root.resolve()
        resolved = (resolved_root / candidate).resolve()
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def candidate_inventory(root: Path) -> list[dict[str, Any]]:
    """Collect candidate findings only; never promote one during revalidation."""
    candidates: list[dict[str, Any]] = []
    paths: list[Path] = []
    try:
        if root == resolve_project_root().resolve():
            path = findings_payload_path(load_runtime())
            if path is not None:
                paths.append(path)
    except (OSError, ValueError, TypeError):
        pass
    bugx_root = root / ".bugx"
    if bugx_root.is_dir():
        try:
            paths.extend(
                path for path in list(bugx_root.rglob("findings.json"))[:200]
                if path.is_file() and not path.is_symlink()
            )
        except OSError:
            pass
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        findings = payload.get("findings") if isinstance(payload, dict) else payload
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            confirmation = finding.get("confirmation")
            state = confirmation.get("state") if isinstance(confirmation, dict) else None
            if state in {"confirmed", "rejected"} or (
                state not in {None, "candidate", "observed", "reproduced", "root_caused"}
                and finding.get("status") != "candidate"
            ):
                continue
            if state is None and finding.get("status") != "candidate":
                continue
            fingerprint = finding.get("fingerprint") or finding.get("id")
            if not isinstance(fingerprint, str) or not fingerprint.strip():
                fingerprint = hashlib.sha256(
                    json.dumps(finding, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
            candidates.append({
                "fingerprint": fingerprint,
                "finding": finding,
                "source": str(path),
            })
    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        unique.setdefault(str(item["fingerprint"]), item)
    return list(unique.values())[:200]


def candidate_dependencies(finding: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("dependencies", "required_dependencies", "imports", "tools"):
        raw = finding.get(field)
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            values.extend(item.strip() for item in raw if isinstance(item, str) and item.strip())
    return list(dict.fromkeys(values))[:20]


def declared_dependencies(root: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted(root.glob("requirements*.txt")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "--")):
                continue
            match = re.match(r"([A-Za-z0-9_.-]+)", line)
            if match:
                names.add(match.group(1).casefold())
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            raw = payload.get("project", {}).get("dependencies", [])
            for item in raw if isinstance(raw, list) else []:
                match = re.match(r"([A-Za-z0-9_.-]+)", str(item).strip())
                if match:
                    names.add(match.group(1).casefold())
        except (OSError, UnicodeDecodeError, ValueError, ImportError):
            pass
    return names


def verify_candidate_assets(root: Path, finding: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    assets = finding.get("assets") or finding.get("verification_assets") or []
    if isinstance(assets, dict):
        assets = [assets]
    if not isinstance(assets, list):
        return ["assets 必须是数组"]
    for evidence in finding.get("evidence", []) if isinstance(finding.get("evidence"), list) else []:
        if isinstance(evidence, dict) and evidence.get("artifact_path"):
            assets.append({"path": evidence.get("artifact_path"), "sha256": evidence.get("artifact_sha256")})
    for asset in assets[:20]:
        if not isinstance(asset, dict):
            errors.append("asset 不是对象")
            continue
        path = inside_project(root, asset.get("path") or asset.get("artifact_path"))
        if path is None or not path.is_file() or path.is_symlink():
            errors.append(f"asset 路径无效或不存在: {asset.get('path') or asset.get('artifact_path')}")
            continue
        expected = asset.get("sha256") or asset.get("artifact_sha256")
        if expected:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if str(expected).casefold() != actual:
                errors.append(f"asset hash 不匹配: {asset.get('path') or asset.get('artifact_path')}")
    return errors


def safe_reproduction_command(root: Path, finding: dict[str, Any]) -> list[str] | None:
    command = finding.get("reproduction") or finding.get("reproduction_command")
    if not isinstance(command, str) or not command.strip():
        return None
    if any(char in command for char in (";", "&", "|", ">", "<", "`", "$", "\n", "\r")):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    executable = Path(parts[0]).name.casefold()
    allowed = {"python", "python3", Path(sys.executable).name.casefold(), "pytest", "node"}
    if executable not in allowed:
        return None
    target_index = 1
    if executable == "pytest":
        target_index = 1
    elif parts[1] in {"-m", "-c", "--version"}:
        return None
    target = inside_project(root, parts[target_index])
    if target is None or not target.is_file():
        return None
    return parts


def revalidate_candidates(args: list[str]) -> int:
    """Safely replay candidate PoCs and emit proposals, never confirmations."""
    # 局部导入：orchestrator_controls 模块级反向依赖本模块（ci_detection_report），
    # 顶层互相 import 会形成循环。公开名 option_value = _option_value（架构棘轮
    # 禁止跨模块导入下划线私有符号）。
    from core.launch_phases.orchestrator_controls import option_value

    root = Path(option_value(args, "--root") or resolve_project_root()).resolve()
    dry_run = "--dry-run" in args
    auto_install = "--auto-install-deps" in args
    lock: dict[str, dependency_lock.LockedRequirement] = {}
    lock_error = ""
    if auto_install:
        # SEC-05：自动安装必须绑定「项目之外」的哈希锁。旧实现用项目自带的
        # requirements*.txt 充当白名单，而被审计项目同时控制依赖名与白名单，
        # 门禁形同虚设，等于在宿主机上执行目标仓库指定的 pip install。
        if dependency_lock is None:  # pragma: no cover - 隔离测试环境
            lock_error = "受信依赖锁模块不可用，已拒绝自动安装"
            auto_install = False
        else:
            try:
                lock = dependency_lock.load_trusted_lock(
                    option_value(args, "--deps-lock") or "",
                    project_root=root,
                )
            except dependency_lock.DependencyLockError as exc:
                lock_error = str(exc)
                auto_install = False
    proposals: list[dict[str, Any]] = []
    source_updates: dict[str, list[dict[str, Any]]] = {}
    declared = declared_dependencies(root)
    for item in candidate_inventory(root):
        finding = item["finding"]
        dependencies = candidate_dependencies(finding)
        missing: list[str] = []
        for dependency in dependencies:
            module_name = dependency.split("[", 1)[0].replace("-", "_").split(".", 1)[0]
            available = bool(shutil.which(dependency))
            if not available:
                try:
                    import importlib.util
                    available = importlib.util.find_spec(module_name) is not None
                except (ImportError, ModuleNotFoundError, ValueError):
                    available = False
            if not available:
                missing.append(dependency)
        errors = verify_candidate_assets(root, finding)
        installed: list[str] = []
        refused: list[str] = []
        install_note = ""
        if missing and auto_install and not dry_run:
            selected, refused = dependency_lock.select_installable(missing, lock)
            if selected:
                installed_names, failed_names, install_note = dependency_lock.install_locked(selected)
                installed = installed_names
                refused = [*refused, *failed_names]
                resolved = {dependency_lock.canonical_name(name) for name in installed}
                missing = [
                    dependency for dependency in missing
                    if dependency_lock.canonical_name(dependency) not in resolved
                ]
        # 项目声明但未被受信锁覆盖的缺失依赖：仅作可观测信息，不参与任何门禁。
        declared_unlocked = [
            dependency for dependency in missing
            if dependency.casefold() in declared
        ] if auto_install or lock_error else []
        command = safe_reproduction_command(root, finding)
        replay = "not_run"
        output = ""
        if errors or missing:
            replay = "blocked"
        elif command is None:
            replay = "no_safe_reproduction"
        elif not dry_run:
            try:
                proc = subprocess.run(
                    command, cwd=str(root), capture_output=True, text=True, timeout=30,
                )
                replay = "replayed_pass" if proc.returncode == 0 else "replayed_fail"
                output = (proc.stdout or proc.stderr or "")[-1000:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                replay = "replayed_error"
                output = str(exc)
        proposal = {
            "fingerprint": item["fingerprint"],
            "source": item["source"],
            "replay": replay,
            "missing_dependencies": missing,
            "installed_dependencies": installed,
            "refused_dependencies": refused,
            "declared_but_unlocked": declared_unlocked,
            "install_note": install_note,
            "lock_error": lock_error,
            "asset_errors": errors,
            "output": output,
            "upgrade_proposal": (
                "提交到 confirmation gate 做独立验证；本报告不改变 Finding 状态。"
                if replay == "replayed_pass" else None
            ),
        }
        if replay == "replayed_pass" and not dry_run:
            source_updates.setdefault(item["source"], []).append({
                "fingerprint": item["fingerprint"],
                "replay": replay,
                "output": output,
            })
            proposal["proposal_persisted"] = False
        proposals.append(proposal)
    if source_updates and not dry_run:
        import core.secure_atomic as secure_atomic
        for raw_source, updates in source_updates.items():
            source = Path(raw_source)
            if source.is_absolute():
                try:
                    source = source.resolve()
                    source.relative_to(root.resolve())
                except (OSError, RuntimeError, ValueError):
                    source = None
            else:
                source = inside_project(root, raw_source)
            if source is None or not source.is_file() or source.is_symlink():
                continue
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
                findings = payload.get("findings") if isinstance(payload, dict) else payload
                if not isinstance(findings, list):
                    continue
                changed = False
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    fingerprint = finding.get("fingerprint") or finding.get("id")
                    for update in updates:
                        if str(fingerprint) != str(update["fingerprint"]):
                            continue
                        finding["revalidation"] = {
                            "status": "passed",
                            "replay_output": update["output"],
                            "requires_confirmation_gate": True,
                        }
                        changed = True
                        for proposal in proposals:
                            if proposal.get("fingerprint") == update["fingerprint"]:
                                proposal["proposal_persisted"] = True
                if changed:
                    secure_atomic.atomic_write_json(source, payload)
            except (OSError, UnicodeDecodeError, ValueError, TypeError):
                continue
    result = {
        "schema_version": 1,
        "event_type": "cycle-revalidate",
        "project_root": str(root),
        "dry_run": dry_run,
        "auto_install_deps": auto_install,
        "deps_lock": str(option_value(args, "--deps-lock") or ""),
        "lock_error": lock_error,
        "candidates": proposals,
    }
    if not dry_run:
        try:
            state_dir = bugx_paths.require(root).state_root
        except (OSError, ValueError, RuntimeError):
            state_dir = root / ".bugx" / "state"
        if bugx_paths is not None:
            bugx_paths.ensure_dir(state_dir)
        else:  # pragma: no cover - 隔离测试环境
            state_dir.mkdir(parents=True, exist_ok=True)
        import core.secure_atomic as secure_atomic
        secure_atomic.atomic_write_json(state_dir / "revalidation-report.json", result)
        try:
            if root == resolve_project_root().resolve():
                with runtime_lock():
                    runtime = load_runtime()
                    append_runtime_event(
                        runtime, "cycle-revalidate", time.time(),
                        f"candidates={len(proposals)}",
                    )
                    save_runtime(runtime)
        except (OSError, ValueError, RuntimeError):
            pass
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"候选复验：{len(proposals)} 条；仅生成 proposal，不直接确认 Finding。")
        if lock_error:
            print(f"  ⏸ 依赖自动安装已停用：{lock_error}")
        for proposal in proposals:
            print(f"  - {proposal['fingerprint']}: {proposal['replay']}")
            if proposal.get("refused_dependencies"):
                print(f"      拒绝安装（不在受信锁内）: {', '.join(proposal['refused_dependencies'])}")
    return 0


def changed_test_files(root: Path) -> list[str]:
    files: set[str] = set()
    for command in (
        ["git", "diff", "--name-only", "--relative", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            proc = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            files.update(item.strip() for item in proc.stdout.splitlines() if item.strip())
    return sorted(path for path in files if re.search(r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_[^/]+$|_test\.", path, re.I))


def ci_detection_report(root: Path, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Detect CI test coverage and return a diff proposal without writing files."""
    payload = payload or {}
    ci_files: list[Path] = []
    for path in (root / "Jenkinsfile", root / "Makefile"):
        if path.is_file():
            ci_files.append(path)
    for directory in (root / ".gitea" / "workflows", root / ".github" / "workflows"):
        if directory.is_dir():
            ci_files.extend(path for path in directory.glob("*.y*ml") if path.is_file())
    changed_tests = payload.get("changed_tests")
    if not isinstance(changed_tests, list):
        changed_tests = changed_test_files(root)
    stages: list[str] = []
    existing_test_stage = False
    source = ""
    jenkins = root / "Jenkinsfile"
    if jenkins.is_file():
        source = jenkins.read_text(encoding="utf-8")
        stages = re.findall(r"stage\s*\(\s*['\"]([^'\"]+)", source)
        existing_test_stage = any("test" in stage.casefold() or "check" in stage.casefold() for stage in stages)
    for path in ci_files:
        if path.name != "Jenkinsfile":
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            existing_test_stage = existing_test_stage or bool(re.search(r"\b(test|pytest|unittest|check)\b", text, re.I))
    if not ci_files:
        status = "new_pipeline_required"
        patch = ""
        questions = ["CI 配置不存在；确认新建受保护的测试 stage 和触发条件。"]
    elif existing_test_stage:
        status = "test_stage_present"
        patch = ""
        questions = []
    elif jenkins.is_file():
        insertion = """        stage('BugX Targeted Tests') {\n            steps {\n                sh 'python3 -m unittest discover -s tests -p \"test_*.py\"'\n            }\n        }\n"""
        marker = "    post {" if "    post {" in source else "}"
        updated = source.replace(marker, insertion + marker, 1)
        patch = "".join(difflib.unified_diff(
            source.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile="a/Jenkinsfile", tofile="b/Jenkinsfile",
        ))
        status = "test_stage_missing"
        questions = ["确认将该 targeted test stage 插入 Jenkinsfile；本命令不会自动写文件。"]
    else:
        status = "test_stage_missing"
        patch = ""
        questions = ["确认在现有 CI 配置中增加与变更测试关联的测试 stage；本命令不会自动写文件。"]
    return {
        "status": status,
        "ci_files": [str(path.relative_to(root)) for path in ci_files],
        "stages": stages,
        "changed_tests": changed_tests,
        "patch": patch,
        "manual_questions": questions,
    }


# Compatibility aliases
_inside_project = inside_project
_candidate_inventory = candidate_inventory
_candidate_dependencies = candidate_dependencies
_declared_dependencies = declared_dependencies
_verify_candidate_assets = verify_candidate_assets
_safe_reproduction_command = safe_reproduction_command
_changed_test_files = changed_test_files
_ci_detection_report = ci_detection_report
