#!/usr/bin/env python3
"""interfaces/cli/bugx_cli/infra/project_reconcile.py - 项目级版本与宿主入口无感自动收敛。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .. import __version__
from .engine_installer import (
    _engine_build_id,
    _is_global_home_scope,
    _is_newer_version,
    _layout_module,
    global_engine_root,
)
from .security import _assert_no_symlink_components, _sha256_file


def _read_json_dict(path: Path) -> dict[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    _assert_no_symlink_components(path)
    content = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _reconcile_manifest_version(root: Path, manifest_path: Path, data: dict[str, object]) -> bool:
    """在 layout_version 兼容且不降级的前提下，将 manifest.bugx_version 对齐到当前 CLI。"""
    old = data.get("bugx_version")
    if not isinstance(old, str) or not old or old == __version__:
        return False
    if _is_newer_version(old, __version__):
        return False
    updated = dict(data)
    updated["bugx_version"] = __version__
    _atomic_write_json(manifest_path, updated)
    return True


def _collect_modified_host_files(root: Path, installed: list[object]) -> frozenset[Path]:
    """根据 hosts.json 记录的基准 sha256，识别被用户手工修改过的项目级文件。"""
    modified: set[Path] = set()
    for record in installed:
        if not isinstance(record, dict):
            continue
        for item in record.get("files") or []:
            if not isinstance(item, dict):
                continue
            rel = item.get("path")
            expected = item.get("sha256")
            if not isinstance(rel, str) or not isinstance(expected, str) or not rel or not expected:
                continue
            target = root / rel
            if target.is_symlink() or not target.is_file():
                continue
            try:
                if _sha256_file(target) != expected:
                    modified.add(target.resolve())
            except OSError:
                continue
    return frozenset(modified)


def auto_reconcile_project_if_needed(root: Path) -> tuple[bool, str]:
    """进入已初始化项目时，自动将滞后的 manifest 与宿主入口收敛到当前全局引擎版本。

    设计约束：
    - 正常情况下（版本与 Build ID 已一致）仅做 O(1) JSON 元数据读取后立即返回；
    - 绝不跨过不兼容的 ``layout_version``，绝不静默降级；
    - 通过三态哈希保留用户手工修改的宿主文件，仅静默刷新未修改模板。
    """
    root = root.expanduser().resolve()
    if _is_global_home_scope(root):
        return False, "home_scope"
    manifest_path = root / ".bugx" / "manifest.json"
    manifest_data = _read_json_dict(manifest_path)
    if manifest_data is None:
        return False, "uninitialized"

    try:
        expected_layout = getattr(_layout_module(root), "LAYOUT_VERSION", None)
    except Exception:
        expected_layout = None
    if not expected_layout or manifest_data.get("layout_version") != expected_layout:
        return False, "incompatible_layout"

    old_manifest_ver = manifest_data.get("bugx_version")
    manifest_behind = (
        isinstance(old_manifest_ver, str)
        and bool(old_manifest_ver)
        and old_manifest_ver != __version__
        and not _is_newer_version(old_manifest_ver, __version__)
    )

    hosts_path = root / ".bugx" / "hosts.json"
    hosts_data = _read_json_dict(hosts_path)
    installed_hosts: list[str] = []
    hosts_behind = False
    if hosts_data is not None:
        raw_installed = hosts_data.get("installed")
        if isinstance(raw_installed, list):
            from ..host_adapters import HOST_ADAPTERS

            known = {adapter.name for adapter in HOST_ADAPTERS}
            for item in raw_installed:
                name = item.get("name") if isinstance(item, dict) else None
                if isinstance(name, str) and name in known and name not in installed_hosts:
                    installed_hosts.append(name)
        if installed_hosts:
            hosts_ver = hosts_data.get("bugx_version")
            hosts_build = hosts_data.get("bugx_build_id")
            current_build = _engine_build_id(global_engine_root())
            if isinstance(hosts_ver, str) and hosts_ver != __version__:
                hosts_behind = not _is_newer_version(hosts_ver, __version__)
            elif (
                hosts_ver == __version__
                and isinstance(current_build, int)
                and (not isinstance(hosts_build, int) or current_build > hosts_build)
            ):
                hosts_behind = True

    if not manifest_behind and not (installed_hosts and hosts_behind):
        return False, "up_to_date"

    actions: list[str] = []
    if manifest_behind:
        if _reconcile_manifest_version(root, manifest_path, manifest_data):
            actions.append(f"manifest:{old_manifest_ver}->{__version__}")

    if installed_hosts and hosts_behind and hosts_data is not None:
        raw_installed = hosts_data.get("installed")
        preserve_paths = _collect_modified_host_files(
            root, raw_installed if isinstance(raw_installed, list) else [],
        )
        from ..commands.agent import command_agent

        reconcile_args = argparse.Namespace(
            root=root,
            agent=installed_hosts,
            force=True,
            allow_missing=True,
            non_interactive=True,
            json=True,
            agent_command="install",
            docs_transport=None,
            docs_remote_url=None,
            locale=None,
            _reconcile_preserve_paths=preserve_paths,
            _reconcile_skip_deps=True,
            _reconcile_quiet=True,
        )
        command_agent(reconcile_args)
        actions.append("hosts:" + ",".join(installed_hosts))

    if not actions:
        return False, "up_to_date"
    return True, "；".join(actions)
