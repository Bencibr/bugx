"""Idempotent project bootstrap used by ``pre`` on first entry."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import core.bugx_paths as bugx_paths
from core.planning import capability_planner
from core.coverage import coverage_bootstrap
from core.skills import mcp_runtime
from core.runtime import prep_validate
from core.runtime import project_profile
from core.runtime import resume_ticket
import core.secure_atomic as secure_atomic

CONFIG_SCHEMA_VERSION = 2
_POLICY_DEFAULTS = {
    "review_mode": "auto",
    "fix_mode": "auto",
    "polling_policy": "auto",
    "commit_policy": "round",
    "cloud_policy": "local",
    "session_recovery": "resume",
    "startup_action": "start",
}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_config(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


#: ``.bugx`` 下**派生/可重建**的条目——它们不是项目身份，缺了可以随时重生成。
#: 启动早期阶段（门禁交互、AI 建议、工具选择）可能在本进程创建它们，若把它们
#: 当成"已有状态"，初始化就会被自己的派生视图永久拒绝（自锁死锁）。
DERIVED_LAYOUT_FILES = frozenset({".init_progress.json", "startup-todo.md"})
DERIVED_LAYOUT_DIRS = frozenset({"logs", "run", "test-state"})


def _is_derived_layout_entry(entry: Path) -> bool:
    """``.bugx`` 条目是否纯粹是派生视图/一次性的临时产物。

    只认白名单：任何无法证明可重建的条目（``manifest.json``、``current.json``、
    ``pollings/``、``knowledge/``、``backups/`` 等权威状态）仍然要求人工恢复，
    绝不允许用一个空 manifest 覆盖真实数据。判定必须是 O(1) 的——这里刻意不遍历
    目录内容，避免在初始化前置门禁里做无界扫描。
    """
    if entry.is_symlink():
        return False
    if entry.is_file():
        return entry.name in DERIVED_LAYOUT_FILES
    return entry.is_dir() and entry.name in DERIVED_LAYOUT_DIRS


def is_uninitialized_project(root: Path) -> bool:
    """Identify a new project without treating orphaned runtime data as fresh.

    A config can be supplied before first use.  The init lock is also allowed:
    ``prepare_project`` rechecks this predicate while holding that lock.
    Derived views (``startup-todo.md``、``run/``、``logs/``、``test-state/``) are
    tolerated because startup gates legitimately write them before the layout
    exists; treating them as state would deadlock initialization against its
    own output.  Everything else requires recovery rather than inventing a new
    manifest.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"项目根目录不存在: {root}")
    base = root / ".bugx"
    bugx_paths.assert_no_symlink_components(base)
    if not base.exists():
        return True
    if not base.is_dir():
        return False
    try:
        for entry in base.iterdir():
            if entry.is_symlink():
                return False
            if entry.name == "init.lock":
                continue
            if _is_derived_layout_entry(entry):
                continue
            if not entry.is_file():
                return False
            if entry.name != "config.json" or entry.stat().st_size > 64 * 1024:
                return False
            if not isinstance(json.loads(entry.read_text(encoding="utf-8")), dict):
                return False
    except (OSError, UnicodeError, ValueError):
        return False
    return True


def _engine_version(engine_root: Path) -> str:
    """Read the trusted engine version without mutating project state."""
    metadata = engine_root / "metadata.txt"
    try:
        text = metadata.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取全局引擎版本标记: {metadata}") from exc
    match = re.search(
        r'bugx_version:\s*"([0-9]+\.[0-9]+\.[0-9]+(?:\+[0-9A-Za-z.-]+)?)"',
        text,
    )
    if not match:
        raise RuntimeError(f"全局引擎版本标记无效: {metadata}")
    return match.group(1)


def _acquire_lock(root: Path) -> Path:
    lock = root / ".bugx" / "init.lock"
    for attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if lock.is_symlink():
                    raise RuntimeError(f"初始化锁是符号链接: {lock}")
                state = json.loads(lock.read_text(encoding="utf-8"))
                pid = int(state.get("pid", 0)) if isinstance(state, dict) else 0
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pid = 0
            if _pid_alive(pid):
                raise RuntimeError(f"项目初始化正在由 PID {pid} 执行")
            if attempt:
                raise RuntimeError("无法安全回收孤儿初始化锁")
            lock.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": time.time()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return lock
    raise RuntimeError("无法获取项目初始化锁")


def _record_stage(root: Path, stage: str) -> None:
    secure_atomic.atomic_write_json(
        root / ".bugx" / ".init_progress.json",
        {"schema_version": 1, "pid": os.getpid(), "stage": stage,
         "updated_at": time.time()},
        mode=0o600, parent_mode=0o700,
    )


def _initialize_project_ledger(active: bugx_paths.Scope, engine_root: Path) -> None:
    seed = active.knowledge_seed
    marker = active.knowledge_root / "manifest.json"
    database = active.knowledge_db
    for path in (seed, marker, database):
        bugx_paths.assert_no_symlink_components(path)
    if marker.is_file() and not database.is_file():
        raise RuntimeError("项目知识 SQLite 缺失；拒绝从 JSON 种子覆盖恢复")
    if not seed.exists():
        source = engine_root / "lesson-ledger.json"
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"缺少内置知识种子: {source}")
        seed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, seed)
        seed.chmod(0o600)
    if not marker.exists():
        ledger_script = engine_root / "ledger" / "lesson_ledger.py"
        if not ledger_script.is_file() or ledger_script.is_symlink():
            raise RuntimeError(f"缺少知识账本引擎: {ledger_script}")
        result = subprocess.run(
            [sys.executable, str(ledger_script), "init", "--ledger", str(seed), "--json"],
            cwd=active.root, capture_output=True, text=True, check=False, timeout=30,
            env={**os.environ, "BUGX_PROJECT_ROOT": str(active.root)},
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()[-800:]
            raise RuntimeError(f"项目知识账本初始化失败: {detail}")
        database.chmod(0o600)
        secure_atomic.atomic_write_json(marker, {
            "schema_version": 1, "scope": "project", "authority": "sqlite",
            "seed": seed.name, "database": database.name,
            "initialized_at": time.time(),
        }, mode=0o600, parent_mode=0o700)


def _initialize_life(active: bugx_paths.Scope, engine_root: Path) -> None:
    if active.life.is_file():
        return
    script = engine_root / "ledger" / "verify_life.py"
    result = subprocess.run(
        [sys.executable, str(script), "reset"], cwd=active.root,
        capture_output=True, text=True, check=False, timeout=30,
        env={**os.environ, "BUGX_PROJECT_ROOT": str(active.root)},
    )
    if result.returncode or not active.life.is_file():
        detail = (result.stderr or result.stdout).strip()[-800:]
        raise RuntimeError(f"项目生命状态初始化失败: {detail or result.returncode}")


def _write_project_profile(active: bugx_paths.Scope) -> None:
    profile, _changed = project_profile.ensure_project_profile(
        active.knowledge_seed, active.root, force=False,
    )
    project_profile.export_project_profile(
        profile, active.knowledge_root / "project-profile.json",
    )


def _matched_optional_mcp_names(root: Path) -> set[str]:
    """画像 + 技能包声明 → 待探活的可选 MCP（DOC-233 设计项 2）。

    原**硬编码形态规则已删除**（`ui`/`web`→playwright、`rest`/`graphql`→postmcp），
    统一由 ``capability_planner`` 编排：技能包 ``required_runtime`` 为装配真源，
    注册表 ``applies`` 仅作形态兜底（保持默认行为等价）。

    任何失败都降级为空集并打印原因——可选件按 ``DOC-226`` 不得阻断启动。
    """
    profile_path = root / ".bugx" / "knowledge" / "project-profile.json"
    profile = _read_config(profile_path)
    if not profile:
        try:
            profile = project_profile.detect_project_profile(root, allow_llm=False)
        except (OSError, ValueError, RuntimeError):
            return set()
    classification: dict[str, Any] = {
        key: list(profile.get(key) or [])
        for key in ("forms", "surfaces", "languages", "frameworks")
    }
    inner = profile.get("classification")
    if isinstance(inner, dict):
        for key in ("forms", "surfaces", "languages", "frameworks"):
            if inner.get(key):
                classification[key] = list(inner[key])
    try:
        names, errors = capability_planner.optional_mcp_names(
            classification, project_root=root,
        )
    except Exception as exc:  # noqa: BLE001 - 编排器异常不得阻断启动
        print(f"[launch_bugx] 能力编排异常，降级为无附加能力（不阻断）：{exc}")
        return set()
    for err in errors:
        print(f"[launch_bugx] 能力编排降级（不阻断）：{err}")
    return names


def _optional_mcp_runtime_roots(root: Path) -> list[Path]:
    override = os.environ.get("BUGX_MCP_RUNTIME_ROOT")
    shared = Path(override).expanduser() if override else Path.home() / ".bugx" / "mcp-runtime"
    return list(dict.fromkeys((root / ".bugx" / "mcp-runtime", shared)))


def _clean_optional_mcp_staging(runtime_roots: list[Path]) -> list[str]:
    cleaned: list[str] = []
    for runtime_root in runtime_roots:
        try:
            runtime_root = mcp_runtime.reject_symlink_components(runtime_root)
            if not runtime_root.is_dir() or runtime_root.is_symlink():
                continue
            with mcp_runtime._install_lock(runtime_root):
                for candidate in runtime_root.iterdir():
                    if (candidate.name.startswith(mcp_runtime.STAGING_PREFIX)
                            and candidate.is_dir() and not candidate.is_symlink()):
                        shutil.rmtree(candidate)
                        cleaned.append(str(candidate))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            continue
    return cleaned


def _probe_and_sanitize_optional_mcps(root: Path) -> dict[str, Any]:
    """Probe profile-matched optional MCP launchers and discard incomplete staging caches."""
    requested = _matched_optional_mcp_names(root)
    report: dict[str, Any] = {
        "matched": sorted(requested), "available": [], "warnings": [], "cleaned_staging": [],
    }
    if not requested:
        return report

    runtime_roots = _optional_mcp_runtime_roots(root)
    _skills_script = Path(__file__).resolve().parents[1] / "skills" / "mcp_runtime.py"
    script = _skills_script if _skills_script.is_file() else Path(__file__).with_name("mcp_runtime.py")
    for server in sorted(requested):
        failure = "runtime 未安装或当前不可用"
        for runtime_root in runtime_roots:
            try:
                result = subprocess.run(
                    [sys.executable, str(script), "start", "--runtime-root", str(runtime_root),
                     "--server", server, "--print-command"],
                    cwd=root, capture_output=True, text=True, check=False, timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failure = str(exc)
                continue
            if result.returncode == 0:
                try:
                    payload = json.loads(result.stdout)
                except (TypeError, ValueError):
                    payload = None
                if (isinstance(payload, dict) and payload.get("server") == server
                        and isinstance(payload.get("argv"), list)):
                    report["available"].append(server)
                    failure = ""
                    break
                failure = "runtime probe 返回格式无效"
            else:
                failure = (result.stderr or result.stdout or "runtime probe 失败").strip()[-400:]
        if failure:
            report["warnings"].append(f"{server}: {failure}")

    if report["warnings"]:
        report["cleaned_staging"] = _clean_optional_mcp_staging(runtime_roots)
    report["available"] = sorted(set(report["available"]))
    return report


def _write_ready_config(
    root: Path, previous: dict[str, Any], fingerprint: str,
    optional_mcp_report: dict[str, Any] | None = None,
) -> None:
    payload = dict(previous)
    payload.update({
        "schema_version": CONFIG_SCHEMA_VERSION,
        "status": "ready",
        "build_fingerprint": fingerprint,
        "optional_mcp_servers": sorted(set((optional_mcp_report or {}).get("available") or [])),
        "optional_mcp_probe": optional_mcp_report or {},
    })
    for key, value in _POLICY_DEFAULTS.items():
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            payload[key] = value
    secure_atomic.atomic_write_json(
        root / ".bugx" / "config.json", payload, mode=0o600, parent_mode=0o700,
    )


def _write_initializing_config(
    root: Path, previous: dict[str, Any], fingerprint: str,
) -> None:
    """Persist only the cheap pre-gate state; never claim the project is ready."""
    payload = dict(previous)
    payload.update({
        "schema_version": CONFIG_SCHEMA_VERSION,
        "status": "initializing",
        "build_fingerprint": fingerprint,
    })
    secure_atomic.atomic_write_json(
        root / ".bugx" / "config.json", payload, mode=0o600, parent_mode=0o700,
    )


def prepare_project(root: Path, engine_root: Path) -> dict[str, Any]:
    """Prepare the minimal project layout before startup hard gates.

    This stage is deliberately limited to path/layout validation, a bounded
    build fingerprint and an ``initializing`` config marker.  It must not scan
    the project, probe optional MCPs, or bootstrap coverage.  A fresh project
    needs a manifest/current layout so Git and version gates have real inputs;
    existing manifests are never refreshed here, otherwise a stale project
    version would be overwritten before the version gate can reject it.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"项目根目录不存在: {root}")
    bugx_paths.assert_no_symlink_components(root / ".bugx")
    (root / ".bugx").mkdir(mode=0o700, exist_ok=True)

    lock = _acquire_lock(root)
    try:
        config_path = root / ".bugx" / "config.json"
        manifest_path = root / ".bugx" / "manifest.json"
        if manifest_path.is_symlink():
            raise RuntimeError(f"项目布局 manifest 是符号链接: {manifest_path}")
        initialized_layout = False
        if not manifest_path.exists():
            if not is_uninitialized_project(root):
                raise RuntimeError(
                    "项目状态不完整：.bugx/manifest.json 缺失，但已有其他状态或配置无效；"
                    "已保留原有数据，请检查并恢复项目状态。"
                )
            # Only a genuinely fresh layout may be materialized before the
            # version gate.  Existing metadata is evidence and stays intact.
            bugx_paths.initialize(root, bugx_version=_engine_version(engine_root))
            initialized_layout = True
        elif not manifest_path.is_file():
            raise RuntimeError(f"项目布局 manifest 不是普通文件: {manifest_path}")

        config = _read_config(config_path)
        fingerprint = project_profile.compute_build_fingerprint(root)
        ready = (
            not initialized_layout
            and config.get("status") == "ready"
            and config.get("schema_version") == CONFIG_SCHEMA_VERSION
            and config.get("build_fingerprint") == fingerprint
        )
        if not ready:
            _write_initializing_config(root, config, fingerprint)
        return {
            "status": "ready" if ready else "initializing",
            "initialized": initialized_layout,
            "fingerprint": fingerprint,
        }
    finally:
        lock.unlink(missing_ok=True)


def run_init_pipeline(root: Path, engine_root: Path) -> dict[str, Any]:
    """Complete project initialization after startup gates have passed.

    Callers that own the launcher startup sequence should invoke
    :func:`prepare_project` first, then run the cheap host/Git/version/cloud
    gates, and only then enter this full pipeline.  Direct callers retain the
    historical complete-init behavior for explicit initialization workflows.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"项目根目录不存在: {root}")
    bugx_paths.assert_no_symlink_components(root / ".bugx")
    (root / ".bugx").mkdir(mode=0o700, exist_ok=True)
    config_path = root / ".bugx" / "config.json"
    config = _read_config(config_path)
    fingerprint = project_profile.compute_build_fingerprint(root)
    if (config.get("status") == "ready"
            and config.get("schema_version") == CONFIG_SCHEMA_VERSION
            and config.get("build_fingerprint") == fingerprint):
        return {
            "status": "ready", "initialized": False, "fingerprint": fingerprint,
            "optional_mcps": config.get("optional_mcp_probe") or {},
        }

    lock = _acquire_lock(root)
    # Reaching the pipeline means schema, fingerprint, or readiness drifted.
    # Any successful rebuild needs a fresh one-use handoff ticket.
    initialized = True
    try:
        _record_stage(root, "profiling")
        current = project_profile.compute_build_fingerprint(root)
        bugx_paths.initialize(root, bugx_version=_engine_version(engine_root))
        active = bugx_paths.require(root)
        _record_stage(root, "provisioning")
        _initialize_project_ledger(active, engine_root)
        _initialize_life(active, engine_root)
        _write_project_profile(active)
        optional_mcp_report = _probe_and_sanitize_optional_mcps(root)
        _record_stage(root, "configuring")
        prep_validate.ensure_prep_record(root)
        coverage = coverage_bootstrap.inspect(root)
        if not coverage.get("ready"):
            if coverage.get("blocked"):
                raise RuntimeError(str(coverage.get("detail") or "覆盖清单被阻断"))
            coverage_bootstrap.bootstrap(root, confirmed=True)
        _write_ready_config(root, config, current, optional_mcp_report)
        ticket = resume_ticket.create_resume_ticket(root) if initialized else None
        (root / ".bugx" / ".init_progress.json").unlink(missing_ok=True)
        return {
            "status": "ready", "initialized": initialized,
            "fingerprint": current, "resume_ticket": ticket,
            "optional_mcps": optional_mcp_report,
        }
    finally:
        lock.unlink(missing_ok=True)
