#!/usr/bin/env python3
"""BugX 离线 MCP runtime（DOC-008 §12 / TASK-008-015）。

from mcp-lock.json 安装锁定版本的 MCP 包到 ``~/.bugx/mcp-runtime/<lock-hash>/``，
启动时只执行通过完整性校验的本地入口，禁止运行时 npx/npm 下载（AD-008-06、
不变量 8）。只有安装阶段允许访问 npm registry，且以 ``--ignore-scripts``
运行——SPIKE-008-01 已确认三个上游包均无 install/postinstall 生命周期脚本；
同时以 ``--no-bin-links`` 安装，保证 runtime 树内不含任何符号链接。

布局：
    ~/.bugx/mcp-runtime/
      <lock-hash>/            完整 runtime（package.json、package-lock.json、
                              node_modules/、runtime-manifest.json、
                              file-hashes.json）
      current -> <lock-hash>  已批准当前版本（原子切换）
      history.json            批准顺序（回滚只在此列表内切换，不重新下载）

安全边界：本模块只启动 lock 中显式声明的 MCP 服务入口，不执行任意命令；
与 Supervisor MCP（bugx mcp serve）的职责互不重叠。
"""

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

import argparse
import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:  # POSIX 进程锁；无 fcntl 的平台安装/回滚降级为无互斥（仅影响并发窗口）
    import fcntl
except ImportError:  # pragma: no cover - 平台差异
    fcntl = None

# QUA-03：文件哈希只保留一份实现（``symbol_index.file_sha256``），公开名保持不变。

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

try:
    import core.mcp_runtime_support.common as _c
    import core.mcp_runtime_support.verifier as _v
    import core.mcp_runtime_support.environment as _env
except ImportError:
    import core.mcp_runtime_support.common as _c
    import core.mcp_runtime_support.verifier as _v
    import core.mcp_runtime_support.environment as _env

# Re-export common symbols
sha256_file = _c.sha256_file
LOCK_SCHEMA_VERSION = _c.LOCK_SCHEMA_VERSION
MANIFEST_SCHEMA_VERSION = _c.MANIFEST_SCHEMA_VERSION
MANIFEST_KIND = _c.MANIFEST_KIND
MANIFEST_NAME = _c.MANIFEST_NAME
FILEHASH_NAME = _c.FILEHASH_NAME
HISTORY_NAME = _c.HISTORY_NAME
CURRENT_LINK = _c.CURRENT_LINK
STAGING_PREFIX = _c.STAGING_PREFIX
DEFAULT_MIN_NODE = _c.DEFAULT_MIN_NODE
WORLD_WRITE_BITS = _c.WORLD_WRITE_BITS
HEX64_RE = _c.HEX64_RE
VERSION_RE = _c.VERSION_RE
BYTECODE_GUARD_NAME = _c.BYTECODE_GUARD_NAME
INTEGRITY_RE = _c.INTEGRITY_RE
SHA256_HASH_RE = _c.SHA256_HASH_RE
NODE_VERSION_RE = _c.NODE_VERSION_RE
TREE_HASH_EXCLUDE = _c.TREE_HASH_EXCLUDE
MAX_REPORTED_MISMATCHES = _c.MAX_REPORTED_MISMATCHES
HISTORY_LIMIT = _c.HISTORY_LIMIT
NPM_TIMEOUT_SECONDS = _c.NPM_TIMEOUT_SECONDS
INSTALL_LOCK_NAME = _c.INSTALL_LOCK_NAME
INSTALL_LOCK_TIMEOUT_SECONDS = _c.INSTALL_LOCK_TIMEOUT_SECONDS
INSTALL_LOCK_POLL_INTERVAL = _c.INSTALL_LOCK_POLL_INTERVAL
ECOSYSTEM_NPM = _c.ECOSYSTEM_NPM
ECOSYSTEM_PYPI = _c.ECOSYSTEM_PYPI
ECOSYSTEMS = _c.ECOSYSTEMS
PACKAGE_KEY_RE = _c.PACKAGE_KEY_RE
PYPI_SECTION = _c.PYPI_SECTION
PYPI_ENVIRONMENTS = _c.PYPI_ENVIRONMENTS
VENV_DIRNAME = _c.VENV_DIRNAME
PYPI_REQUIREMENTS_NAME = _c.PYPI_REQUIREMENTS_NAME
PYPI_INSTALL_FLAGS = _c.PYPI_INSTALL_FLAGS
PYTHON_REQUIRES_OPERATORS = _c.PYTHON_REQUIRES_OPERATORS
LAUNCH_ENV_OVERRIDES = _c.LAUNCH_ENV_OVERRIDES
McpRuntimeError = _c.McpRuntimeError
_is_world_writable = _c._is_world_writable
canonical_json_bytes = _c.canonical_json_bytes
sha256_hex = _c.sha256_hex
utc_now = _c.utc_now
reject_symlink_components = _c.reject_symlink_components
read_json_file = _c.read_json_file
_fsync_directory = _c._fsync_directory
write_json_atomic = _c.write_json_atomic

# Re-export verifier symbols
load_lock = _v.load_lock
parse_python_requires = _v.parse_python_requires
check_python_requires = _v.check_python_requires
lock_hash = _v.lock_hash
build_file_map = _v.build_file_map
compute_tree_hash = _v.compute_tree_hash
chmod_tree = _v.chmod_tree
find_node = _v.find_node
parse_node_version = _v.parse_node_version
node_version_tuple = _v.node_version_tuple
engines_requirement_floor = _v.engines_requirement_floor
check_node_compatibility = _v.check_node_compatibility
required_manifest_fields = _v.required_manifest_fields
load_manifest = _v.load_manifest
resolve_server_entry = _v.resolve_server_entry
verify_runtime = _v.verify_runtime
load_history = _v.load_history
update_history = _v.update_history
switch_current = _v.switch_current
read_current = _v.read_current

# Re-export environment symbols
_npm_executable = _env._npm_executable
_resolve_bins = _env._resolve_bins
_install_npm_packages = _env._install_npm_packages
_venv_dir = _env._venv_dir
_venv_bin_dir = _env._venv_bin_dir
_python_version_of = _env._python_version_of
_normalize_python_spec = _env._normalize_python_spec
_allowed_minors = _env._allowed_minors
_uv_managed_python = _env._uv_managed_python
_interpreter_candidates = _env._interpreter_candidates
_resolve_env_python = _env._resolve_env_python
_materialize_venv_links = _env._materialize_venv_links
_create_venv = _env._create_venv
_install_pypi_environment = _env._install_pypi_environment
_install_bytecode_guard = _env._install_bytecode_guard
_resolve_pypi_entry = _env._resolve_pypi_entry
_read_pyvenv_version = _env._read_pyvenv_version
_check_venv_shebang = _env._check_venv_shebang
_rewrite_venv_staging_paths = _env._rewrite_venv_staging_paths

@contextlib.contextmanager
def _install_lock(runtime_root: Path):
    """install/rollback 的跨进程互斥锁（runtime_root/.install.lock）。

    并发安装会互相踩踏 staging/current/history；flock LOCK_EX|LOCK_NB
    轮询等待至多 INSTALL_LOCK_TIMEOUT_SECONDS 秒，超时 fail-closed。
    """
    lock_path = runtime_root / INSTALL_LOCK_NAME
    if fcntl is None:  # pragma: no cover - 平台差异
        yield
        return
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    lock_handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    deadline = time.monotonic() + INSTALL_LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise McpRuntimeError(
                        f"等待 MCP runtime 安装锁超时"
                        f"（{INSTALL_LOCK_TIMEOUT_SECONDS:.0f}s）: {lock_path}"
                    ) from None
                time.sleep(INSTALL_LOCK_POLL_INTERVAL)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock_handle.close()


def install_runtime(
    lock_path: Path,
    runtime_root: Path,
    *,
    npm_bin: str | None = None,
    bugx_version: str = "unknown",
    timeout: int = NPM_TIMEOUT_SECONDS,
) -> dict:
    lock_path = Path(lock_path)
    lock = load_lock(lock_path)
    digest = lock_hash(lock)
    runtime_root = reject_symlink_components(Path(runtime_root))
    runtime_root.mkdir(parents=True, exist_ok=True)
    with _install_lock(runtime_root):
        return _install_runtime_locked(
            lock, digest, runtime_root,
            npm_bin=npm_bin, bugx_version=bugx_version, timeout=timeout,
        )


def _install_runtime_locked(
    lock: dict,
    digest: str,
    runtime_root: Path,
    *,
    npm_bin: str | None = None,
    bugx_version: str = "unknown",
    timeout: int = NPM_TIMEOUT_SECONDS,
) -> dict:
    final_dir = runtime_root / digest

    if final_dir.exists():
        if final_dir.is_symlink():
            raise McpRuntimeError(f"runtime 目录被符号链接占用，拒绝处理: {final_dir}")
        report = verify_runtime(final_dir, expect_hash=digest)
        if not report["ok"]:
            raise McpRuntimeError(
                "已存在同 lock-hash 的 runtime 但完整性校验失败，拒绝覆盖；"
                f"请人工检查后删除该目录: {final_dir}；问题："
                + "；".join(report["issues"][:5])
            )
        # 先记入批准历史再切 current：若中途失败，重跑 install 会走
        # already-installed 路径自愈，不会出现"current 不在批准历史"粘死。
        approved = update_history(runtime_root, digest)
        switch_current(runtime_root, digest)
        return {
            "status": "already-installed",
            "lock_hash": digest,
            "dir": str(final_dir),
            "approved_history": approved,
            "npm_invoked": False,
        }

    staging = runtime_root / f"{STAGING_PREFIX}{digest[:12]}-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    npm_invoked = False
    pypi_invoked = False
    try:
        all_npm = {
            key: meta for key, meta in lock["packages"].items()
            if meta["ecosystem"] == ECOSYSTEM_NPM
        }
        all_environments: dict = dict(
            (lock.get(PYPI_SECTION) or {}).get(PYPI_ENVIRONMENTS) or {}
        )
        if not all_npm and not all_environments:
            raise McpRuntimeError("lock 未声明任何可安装的包")
        # **只安装会被启动的载荷**：lock 中 ``enabled: false`` 的 server 永不启动，
        # 装它的载荷是纯浪费（启用必然改 lock ⇒ 换 hash ⇒ 照旧要重装，缓存零收益），
        # 还会制造 "装了却不会启动" 的假装配状态。
        enabled_servers = {
            name for name, meta in lock["servers"].items()
            if meta.get("enabled") is not False
        }
        enabled_npm: set[str] = set()
        enabled_envs: set[str] = set()
        for name in enabled_servers:
            meta = lock["servers"][name]
            package = str(meta.get("package") or "")
            match = PACKAGE_KEY_RE.match(package)
            if match is None:
                continue
            if match.group(1) == ECOSYSTEM_NPM:
                enabled_npm.add(package)
            else:
                enabled_envs.add(str(meta.get("venv") or name))
        npm_packages = {
            key: meta for key, meta in all_npm.items() if key in enabled_npm
        }
        environments = {
            env_id: spec for env_id, spec in all_environments.items()
            if env_id in enabled_envs
        }
        skipped_payloads = {
            "npm": sorted(set(all_npm) - set(npm_packages)),
            "pypi": sorted(set(all_environments) - set(environments)),
        }
        if skipped_payloads["npm"] or skipped_payloads["pypi"]:
            print(
                "[mcp-runtime] 跳过 lock 禁用载荷（永不启动，不安装）: "
                + ", ".join(skipped_payloads["npm"] + skipped_payloads["pypi"]),
                file=sys.stderr,
            )
        env_pythons: dict[str, tuple[str, str, str]] = {}
        if environments:
            # 逐环境解析解释器 + 版本闸门，全部在**联网/建 venv 之前**完成（fail-fast）。
            for env_id in sorted(environments):
                env_spec = environments[env_id].get("python")
                python_bin, python_version, python_source = _resolve_env_python(
                    env_id, env_spec,
                )
                if python_source == "uv-managed":
                    print(
                        f"[mcp-runtime] 自愈：pypi 环境 {env_id} 需要 Python {env_spec}，"
                        f"已通过 uv 获取 {python_bin}（{python_version}）",
                        file=sys.stderr,
                    )
                satisfied, detail = check_python_requires(
                    str(lock[PYPI_SECTION]["python_requires"]),
                    tuple(int(part) for part in python_version.split(".")),
                )
                if not satisfied:
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 安装被拒绝：{detail}"
                    )
                env_pythons[env_id] = (python_bin, python_version, python_source)
        locked_packages: dict = {}
        if npm_packages:
            npm_invoked = True
            locked_packages = _install_npm_packages(
                staging, npm_packages, npm_bin=npm_bin, timeout=timeout,
            )
        pypi_executables: set[str] = set()
        shebangs_rewritten = 0
        for env_id in sorted(environments):
            pypi_invoked = True
            python_bin, python_version, _python_source = env_pythons[env_id]
            pypi_executables |= _install_pypi_environment(
                staging, lock, env_id, environments[env_id]["packages"],
                timeout=timeout, python_bin=python_bin, python_spec=python_version,
            )
            # P0 修复：pip 写入的 shebang 指向 staging 路径，必须在**记录入口 hash
            # 之前**改写到最终目录，否则 rename 后入口直接执行报 No such file。
            shebangs_rewritten += _rewrite_venv_staging_paths(
                staging, final_dir, env_id,
            )

        engines: dict[str, str | None] = {}
        servers_manifest: dict[str, dict[str, object]] = {}
        executable_rels: set[str] = set(pypi_executables)
        for server, server_meta in lock["servers"].items():
            package = server_meta["package"]
            ecosystem = PACKAGE_KEY_RE.match(package).group(1)
            if server_meta.get("enabled") is False:
                servers_manifest[server] = {
                    "package": package,
                    "ecosystem": ecosystem,
                    **(
                        {"venv": str(server_meta.get("venv") or server)}
                        if ecosystem == ECOSYSTEM_PYPI else {}
                    ),
                    "enabled": False,
                    # 载荷**未安装**（禁用即永不启动，装它纯属浪费）。
                    "payload": "not-installed",
                    **(
                        {"disabled_reason": str(server_meta["disabled_reason"])}
                        if server_meta.get("disabled_reason") else {}
                    ),
                }
                continue
            if ecosystem == ECOSYSTEM_PYPI:
                env_id = str(server_meta.get("venv") or server)
                entry_rel = _resolve_pypi_entry(
                    staging, env_id, str(server_meta["entry"]),
                )
                servers_manifest[server] = {
                    "package": package,
                    "ecosystem": ecosystem,
                    "venv": env_id,
                    "entry": entry_rel,
                    "entry_sha256": sha256_file(staging / entry_rel),
                    "enabled": True,
                    **(
                        {"args": [str(item) for item in server_meta["args"]]}
                        if server_meta.get("args") else {}
                    ),
                }
                continue
            expected_meta = lock["packages"][package]
            npm_name = package.split(":", 1)[1]
            locked = locked_packages.get(f"node_modules/{npm_name}")
            if not isinstance(locked, dict):
                raise McpRuntimeError(
                    f"package-lock.json 缺少 {npm_name} 的顶层记录；安装树不完整"
                )
            actual_integrity = str(locked.get("integrity", "")).strip()
            if actual_integrity != str(expected_meta["integrity"]).strip():
                raise McpRuntimeError(
                    f"{npm_name} integrity 与批准 lock 不一致，疑似 registry 漂移："
                    f"期望 {expected_meta['integrity'][:24]}… "
                    f"实际 {actual_integrity[:24]}…"
                )
            actual_version = str(locked.get("version", ""))
            if actual_version != str(expected_meta["version"]):
                raise McpRuntimeError(
                    f"{npm_name} 版本与批准 lock 不一致：期望 "
                    f"{expected_meta['version']} 实际 {actual_version}"
                )
            pkg_json_path = staging / "node_modules" / npm_name / "package.json"
            package_meta = read_json_file(pkg_json_path, f"{npm_name}/package.json")
            if not isinstance(package_meta, dict):
                raise McpRuntimeError(f"{npm_name}/package.json 顶层必须是对象")
            engines_field = package_meta.get("engines")
            engines[npm_name] = (
                engines_field.get("node") if isinstance(engines_field, dict) else None
            )
            bin_key, entry_rel = _resolve_bins(package_meta, npm_name, server)
            entry_abs = staging / entry_rel
            if entry_abs.is_symlink() or not entry_abs.is_file():
                raise McpRuntimeError(f"{npm_name} 的入口文件不存在: {entry_rel}")
            servers_manifest[server] = {
                "package": package,
                "ecosystem": ecosystem,
                "bin_key": bin_key,
                "entry": entry_rel,
                "entry_sha256": sha256_file(entry_abs),
                "enabled": True,
            }

        bins_by_package: dict[str, list[str]] = {}
        for server_meta in servers_manifest.values():
            if server_meta.get("enabled") is False:
                continue
            if server_meta.get("ecosystem") != ECOSYSTEM_NPM:
                continue
            npm_name = str(server_meta["package"]).split(":", 1)[1]
            pkg_json_path = staging / "node_modules" / npm_name / "package.json"
            package_meta = read_json_file(pkg_json_path, f"{npm_name}/package.json")
            bin_field = package_meta.get("bin") if isinstance(package_meta, dict) else None
            declared: list[str] = []
            if isinstance(bin_field, str):
                declared.append(bin_field)
            elif isinstance(bin_field, dict):
                declared.extend(
                    value for value in bin_field.values() if isinstance(value, str)
                )
            bins_by_package[npm_name] = declared
        for directory, _dirnames, filenames in os.walk(staging):
            dir_path = Path(directory)
            for filename in filenames:
                candidate = dir_path / filename
                rel = candidate.relative_to(staging).as_posix()
                for npm_name, declared in bins_by_package.items():
                    prefix = f"node_modules/{npm_name}/"
                    if rel.startswith(prefix) and rel[len(prefix):] in declared:
                        executable_rels.add(rel)
        chmod_tree(staging, executable_rels)

        files = build_file_map(staging)
        tree = compute_tree_hash(files)
        write_json_atomic(staging / FILEHASH_NAME, files, 0o644)
        manifest: dict[str, object] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "kind": MANIFEST_KIND,
            "lock_hash": digest,
            "created_at": utc_now(),
            "bugx_version": bugx_version,
            "install_scripts_ignored": bool(npm_packages),
            "pypi_only_binary": bool(environments),
            # 因 ``enabled: false`` 而**完全未安装**的载荷（不是"装了不会启动"）。
            "skipped_payloads": skipped_payloads,
            "venv_shebangs_rewritten": shebangs_rewritten,
            "python_requires": (
                str(lock[PYPI_SECTION]["python_requires"]) if environments else None
            ),
            "pypi_environments": {
                env_id: {
                    "python_version": env_pythons[env_id][1],
                    "python_source": env_pythons[env_id][2],
                    "python_requires": str(lock[PYPI_SECTION]["python_requires"]),
                    "package_count": len(environments[env_id]["packages"]),
                }
                for env_id in sorted(environments)
            },
            "packages": {
                **{
                    key: {
                        "ecosystem": meta["ecosystem"],
                        "version": str(meta["version"]),
                        "engines_node": engines.get(key.split(":", 1)[1]),
                        "integrity": str(meta["integrity"]),
                    }
                    for key, meta in lock["packages"].items()
                },
                **{
                    f"pypi:{env_id}:{name}": {
                        "ecosystem": ECOSYSTEM_PYPI,
                        "version": str(meta["version"]),
                        "hashes": list(meta["hashes"]),
                        "venv": env_id,
                        "engines_node": None,
                    }
                    for env_id, env_meta in environments.items()
                    for name, meta in env_meta["packages"].items()
                },
            },
            "servers": servers_manifest,
            "tree_hash": tree,
            "file_count": len(files),
            "total_bytes": sum(int(meta["size"]) for meta in files.values()),
        }
        if npm_packages:
            node_bin = find_node(None)
            manifest["node_version_used"] = "v%d.%d.%d" % node_version_tuple(node_bin)
        write_json_atomic(staging / MANIFEST_NAME, manifest, 0o644)

        report = verify_runtime(
            staging, expect_hash=digest, check_dirname=False,
            published_dir=final_dir,
        )
        if not report["ok"]:
            raise McpRuntimeError(
                "安装后自检失败，未发布该 runtime：" +
                "；".join(report["issues"][:5])
            )

        try:
            os.rename(staging, final_dir)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                # 持锁后仍撞上同名目录：另一进程在我们 npm install 期间
                # 抢先发布了同一 lock-hash（历史遗留无锁安装）。转为明确
                # 的 already-installed 错误，绝不覆盖既有 runtime。
                raise McpRuntimeError(
                    f"already-installed: runtime 目录已被并发安装发布: {final_dir}；"
                    "请重跑 install 走 already-installed 校验路径"
                ) from exc
            raise
        os.chmod(final_dir, 0o755)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # 先记入批准历史再切 current：若切 current 中途失败，重跑 install
    # 会走 already-installed 路径自愈，避免"current 不在批准历史"粘死。
    approved = update_history(runtime_root, digest)
    switch_current(runtime_root, digest)
    return {
        "status": "installed",
        "lock_hash": digest,
        "dir": str(final_dir),
        "servers": servers_manifest,
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "tree_hash": manifest["tree_hash"],
        "approved_history": approved,
        "npm_invoked": npm_invoked,
        "pypi_invoked": pypi_invoked,
        "install_scripts_ignored": bool(npm_packages),
        "pypi_only_binary": bool(environments),
        # 因 ``enabled: false`` 而**完全未安装**的载荷（不是"装了不会启动"）。
        "skipped_payloads": skipped_payloads,
    }


# ---------------------------------------------------------------------------
# 回滚 / 启动
# ---------------------------------------------------------------------------


def rollback_runtime(runtime_root: Path, *, node_override: str | None = None) -> dict:
    runtime_root = reject_symlink_components(Path(runtime_root))
    if not runtime_root.is_dir():
        raise McpRuntimeError("没有批准历史可回滚；请先执行 bugx mcp-runtime install")
    with _install_lock(runtime_root):
        return _rollback_runtime_locked(runtime_root, node_override=node_override)


def _rollback_runtime_locked(runtime_root: Path, *,
                             node_override: str | None = None) -> dict:
    history = load_history(runtime_root)
    if not history:
        raise McpRuntimeError("没有批准历史可回滚；请先执行 bugx mcp-runtime install")
    current = read_current(runtime_root)
    if current is None:
        raise McpRuntimeError(
            "current 链接缺失或非法，无法定位当前位置；请重新执行 install 或 doctor 诊断"
        )
    if current not in history:
        raise McpRuntimeError(
            f"当前 hash {current[:12]}… 不在批准历史中，拒绝盲切；请人工核查"
        )
    index = history.index(current)
    if index == 0:
        raise McpRuntimeError("当前已是最早的批准版本，没有更早的可回滚目标")
    previous = history[index - 1]
    previous_dir = runtime_root / previous
    if previous_dir.is_symlink() or not previous_dir.is_dir():
        raise McpRuntimeError(
            f"上一批准版本的 runtime 目录缺失: {previous_dir}；不会重新下载"
        )
    report = verify_runtime(previous_dir, expect_hash=previous, node_override=node_override)
    if not report["ok"]:
        raise McpRuntimeError(
            "上一批准版本完整性校验失败，拒绝切换：" +
            "；".join(report["issues"][:5])
        )
    switch_current(runtime_root, previous)
    return {
        "status": "rolled-back",
        "rolled_back_to": previous,
        "previous_current": current,
        "approved_history": history,
        "verification": report["checks"],
    }


def _pypi_launch_env(
    runtime_dir: Path, server: str, server_meta: dict,
) -> dict[str, str]:
    """pypi server 的启动环境：**锁定载荷优先**。

    实测：``semgrep-mcp`` 通过 PATH 解析 ``semgrep`` CLI；不把本 runtime 的 venv
    放到最前，它会静默使用主机上任意版本的 semgrep（lock 钉 1.135.0，主机 1.176.0
    被实际调用）——等于绕过 lock 完整性。
    """
    env_id = str(server_meta.get("venv") or server)
    venv_dir = runtime_dir / VENV_DIRNAME / env_id
    venv_bin = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    inherited = [
        item for item in os.environ.get("PATH", "").split(os.pathsep) if item
    ]
    return {
        "PATH": os.pathsep.join([str(venv_bin), *inherited]),
        "VIRTUAL_ENV": str(venv_dir),
    }


def start_server(
    runtime_root: Path,
    server: str,
    *,
    print_command: bool = False,
    node_override: str | None = None,
) -> int:
    runtime_root = reject_symlink_components(Path(runtime_root))
    current = read_current(runtime_root)
    if current is None:
        raise McpRuntimeError(
            "离线 MCP runtime 尚未安装或 current 缺失；请运行 bugx mcp-runtime install"
        )
    runtime_dir = runtime_root / current
    report = verify_runtime(runtime_dir, expect_hash=current, node_override=node_override)
    if not report["ok"]:
        raise McpRuntimeError(
            f"MCP runtime 完整性校验失败，fail-closed 拒绝启动 {server}：" +
            "；".join(report["issues"][:5])
        )
    manifest = load_manifest(runtime_dir)
    entry_rel, _expected = resolve_server_entry(manifest, server)
    entry_abs = (runtime_dir / entry_rel).resolve(strict=True)
    server_meta = manifest["servers"][server]
    ecosystem = (
        str(server_meta.get("ecosystem", ECOSYSTEM_NPM))
        if isinstance(server_meta, dict) else ECOSYSTEM_NPM
    )
    if ecosystem == ECOSYSTEM_PYPI:
        # pypi 服务：console script 自带 venv 解释器 shebang，直接 exec；
        # 绝不调用 pip/uv（network_policy 不变量不变）。
        # 部分包把 MCP 入口做成子命令（如 `semgrep mcp`），args 由 lock 显式声明，
        # 不在代码里硬编码任何包名。
        extra = server_meta.get("args") or []
        if extra and not isinstance(extra, list):
            raise McpRuntimeError(f"server {server} 的 manifest args 必须为数组")
        argv = [str(entry_abs), *[str(item) for item in extra]]
    else:
        node_bin = find_node(node_override)
        argv = [str(node_bin), str(entry_abs)]
    if print_command:
        print(json.dumps({
            "server": server,
            "ecosystem": ecosystem,
            "argv": argv,
            "env_overrides": LAUNCH_ENV_OVERRIDES,
            "lock_hash": current,
            "runtime_dir": str(runtime_dir),
            "network_policy": "no-package-manager",
        }, ensure_ascii=False))
        return 0
    print(
        f"[bugx-mcp-runtime] server={server} entry={entry_abs} "
        f"lock={current[:12]} policy=no-package-manager",
        file=sys.stderr,
    )
    environment = dict(os.environ)
    environment.update(LAUNCH_ENV_OVERRIDES)
    if ecosystem == ECOSYSTEM_PYPI:
        environment.update(_pypi_launch_env(runtime_dir, server, server_meta))
    os.execve(argv[0], argv, environment)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def doctor_runtime(
    runtime_root: Path,
    lock_path: Path | None = None,
    *, node_override: str | None = None,
    fast: bool = True,
) -> dict:
    runtime_root = reject_symlink_components(Path(runtime_root))
    checks: list[dict[str, object]] = []
    available: list[str] = []
    if runtime_root.is_dir() and not runtime_root.is_symlink():
        for entry in sorted(runtime_root.iterdir()):
            if entry.name.startswith(STAGING_PREFIX):
                checks.append({
                    "name": "残留暂存目录", "ok": False,
                    "detail": f"安装中断残留 {entry.name}；可安全删除后重试 install",
                })
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            if HEX64_RE.match(entry.name):
                available.append(entry.name)
    checks.insert(0, {
        "name": "runtime 根目录",
        "ok": runtime_root.is_dir() and not runtime_root.is_symlink(),
        "detail": str(runtime_root),
    })

    lock_info: dict[str, object] | None = None
    if lock_path is not None:
        try:
            lock = load_lock(Path(lock_path))
            lock_info = {
                "path": str(lock_path),
                "hash": lock_hash(lock),
                "packages": sorted(lock["packages"]),
                "servers": sorted(lock["servers"]),
            }
            checks.append({
                "name": "bundled lock", "ok": True,
                "detail": f"hash={lock_info['hash'][:12]} 包数 {len(lock['packages'])}",
            })
        except McpRuntimeError as exc:
            checks.append({"name": "bundled lock", "ok": False, "detail": str(exc)})
            lock_info = {"path": str(lock_path), "error": str(exc)}

    current = None
    try:
        current = read_current(runtime_root)
        checks.append({
            "name": "current 链接",
            "ok": current is not None,
            "detail": current or "未设置（尚未安装）",
        })
    except McpRuntimeError as exc:
        checks.append({"name": "current 链接", "ok": False, "detail": str(exc)})

    verification: dict[str, object] | None = None
    if current is not None:
        current_dir = runtime_root / current
        verification = verify_runtime(current_dir, node_override=node_override, fast=fast)
        for item in verification["checks"]:
            label = {
                "runtime 目录": "runtime 目录存在",
                "runtime manifest": "runtime manifest",
            }.get(str(item["name"]), str(item["name"]))
            checks.append({
                "name": f"当前 runtime：{label}",
                "ok": bool(item["ok"]),
                "detail": str(item["detail"]),
            })
        try:
            manifest = load_manifest(current_dir)
        except McpRuntimeError:
            manifest = None
        if isinstance(manifest, dict):
            for server, meta in sorted(manifest["servers"].items()):
                enabled = bool(meta.get("enabled", True)) \
                    if isinstance(meta, dict) else True
                checks.append({
                    "name": f"server {server}",
                    "ok": True,
                    "detail": ("启用" if enabled else
                               "按 lock 停用（SPIKE-008-01 未通过）"),
                })

    node_ok = True
    node_detail = "node 未找到"
    try:
        node_bin = find_node(node_override)
        version = node_version_tuple(node_bin)
        node_ok = True
        node_detail = f"node {'v%d.%d.%d' % version} @ {node_bin}"
    except McpRuntimeError as exc:
        node_ok = False
        node_detail = str(exc)
    checks.append({"name": "Node 可用性", "ok": node_ok, "detail": node_detail})

    ready = all(bool(item["ok"]) for item in checks)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "target": "mcp-runtime",
        "runtime_root": str(runtime_root),
        "installed": bool(available),
        "available_hashes": available,
        "current": current,
        "lock": lock_info,
        "checks": checks,
        "issues": [] if verification is None else list(verification["issues"]),
        "ready": ready,
    }
    return payload


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_runtime.py", description="BugX 离线 MCP runtime 管理（DOC-008 §12）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lock_info = sub.add_parser("lock-info", help="读取并校验 mcp-lock.json")
    lock_info.add_argument("--lock", type=Path, required=True)
    lock_info.set_defaults(func=_cmd_lock_info)

    install = sub.add_parser("install", help="按 lock 安装新 runtime 并原子发布")
    install.add_argument("--lock", type=Path, required=True)
    install.add_argument("--runtime-root", type=Path, required=True)
    install.add_argument("--npm", default=None)
    install.add_argument("--bugx-version", default="unknown")
    install.add_argument("--timeout", type=int, default=NPM_TIMEOUT_SECONDS)
    install.add_argument("--json", action="store_true")
    install.set_defaults(func=_cmd_install)

    verify = sub.add_parser("verify", help="深度校验当前 runtime 完整性")
    verify.add_argument("--runtime-root", type=Path, required=True)
    verify.add_argument("--expect-hash", default=None)
    verify.add_argument("--node", default=None)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_cmd_verify)

    start = sub.add_parser("start", help="校验后以 node 直接启动 MCP 服务入口")
    start.add_argument("--runtime-root", type=Path, required=True)
    start.add_argument("--server", required=True)
    start.add_argument("--print-command", action="store_true")
    start.add_argument("--node", default=None)
    start.set_defaults(func=_cmd_start)

    rollback = sub.add_parser("rollback", help="切换回上一批准 lock-hash（不下载）")
    rollback.add_argument("--runtime-root", type=Path, required=True)
    rollback.add_argument("--node", default=None)
    rollback.add_argument("--json", action="store_true")
    rollback.set_defaults(func=_cmd_rollback)

    doctor = sub.add_parser("doctor", help="机器可读的 runtime 诊断")
    doctor.add_argument("--runtime-root", type=Path, required=True)
    doctor.add_argument("--lock", type=Path, default=None)
    doctor.add_argument("--node", default=None)
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--fast", action="store_true", default=True, help="快速校验（默认启用）")
    doctor.add_argument("--deep", action="store_false", dest="fast", help="深度全量哈希重算校验")
    doctor.set_defaults(func=_cmd_doctor)
    return parser


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_lock_info(args: argparse.Namespace) -> int:
    lock = load_lock(args.lock)
    _emit({
        "schema_version": lock["schema_version"],
        "lock_hash": lock_hash(lock),
        "packages": sorted(lock["packages"]),
        "servers": sorted(lock["servers"]),
    })
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    result = install_runtime(
        args.lock, args.runtime_root, npm_bin=args.npm,
        bugx_version=args.bugx_version, timeout=args.timeout,
    )
    if args.json or sys.stdout.isatty() is False:
        _emit(result)
    else:
        print(f"已发布 {result['status']}: {result['dir']}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    runtime_root = Path(args.runtime_root)
    current = read_current(runtime_root)
    expect = args.expect_hash or current
    if expect is None:
        raise McpRuntimeError("runtime 未安装（缺少 current），无可校验对象")
    report = verify_runtime(
        runtime_root / expect, expect_hash=expect, node_override=args.node,
    )
    if args.json:
        _emit(report)
    else:
        for item in report["checks"]:
            print(f"  {'✓' if item['ok'] else '!'} {item['name']}: {item['detail']}")
    return 0 if report["ok"] else 1


def _cmd_start(args: argparse.Namespace) -> int:
    return start_server(
        Path(args.runtime_root), args.server,
        print_command=args.print_command, node_override=args.node,
    )


def _cmd_rollback(args: argparse.Namespace) -> int:
    result = rollback_runtime(Path(args.runtime_root), node_override=args.node)
    if args.json:
        _emit(result)
    else:
        print(f"已回滚到 {result['rolled_back_to'][:12]}（原 {result['previous_current'][:12]}）")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    fast = getattr(args, "fast", True)
    payload = doctor_runtime(
        Path(args.runtime_root), args.lock, node_override=args.node, fast=fast,
    )
    if args.json or not sys.stdout.isatty():
        _emit(payload)
    else:
        print(f"MCP runtime @ {payload['runtime_root']}")
        for item in payload["checks"]:
            marker = "✓" if item["ok"] else "!"
            print(f"  {marker} {item['name']}: {item['detail']}")
        for issue in payload["issues"]:
            print(f"  ! 不一致：{issue}")
        print("结论：" + ("就绪" if payload["ready"] else "存在问题"))
    return 0 if payload["ready"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except McpRuntimeError as exc:
        wants_json = getattr(args, "json", False) or not sys.stdout.isatty()
        if wants_json:
            _emit({"ok": False, "error": str(exc)})
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        wants_json = getattr(args, "json", False) or not sys.stdout.isatty()
        message = f"文件系统错误: {exc}"
        if wants_json:
            _emit({"ok": False, "error": message})
        else:
            print(f"错误：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
