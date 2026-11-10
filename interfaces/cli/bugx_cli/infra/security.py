#!/usr/bin/env python3
"""src/bugx_cli/infra/security.py - CLI 安全边界、符号链接防御与原子写操作 (DOC-070)。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class CliError(RuntimeError):
    """CLI 初始化或调度错误。"""


def _i18n_exc(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.exception_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _i18n_prompt(key: str, fallback: str, locale: str | None = None, **kwargs: Any) -> str:
    try:
        import core.i18n as i18n
        return i18n.prompt_text(key, locale=locale, fallback=fallback, **kwargs)
    except Exception:
        if not kwargs:
            return fallback
        try:
            return fallback.format(**kwargs)
        except Exception:
            return fallback


def _find_repo_root(anchor: Path) -> Path:
    """从 anchor 路径安全回溯查找到包含 core/bugx.md 与 pyproject.toml 的仓库根目录。"""
    cur = anchor.resolve()
    for p in (cur, *cur.parents):
        if (p / "core" / "bugx.md").is_file() and (p / "pyproject.toml").is_file():
            return p
    for idx in (4, 3, 2):
        if len(cur.parents) > idx:
            return cur.parents[idx]
    return cur.parent


_CANONICAL_LAYOUT_MODULE: Any = None


def _canonical_bugx_paths():
    """加载权威 ``core/bugx_paths.py``（DES-17 单一实现来源）。

    本模块属于 CLI 包，不能在模块级 ``import bugx_paths``（不在 sys.path 上，
    且与 ``engine_installer`` 构成循环导入），因此按路径加载。

    优先级：CLI 自带资产目录（``share/bugx-agent/core``，与 CLI 版本一致）
    → 全局引擎 ``~/.bugx/core``。两者都是磁盘上的引擎候选，只有同时具备
    DES-17 的平台分支（``is_reparse_point``）才被采纳；否则返回 ``None`` 让
    调用方回落到本地实现，绝不用一份缺少 reparse 判定的旧副本冒充权威实现。

    只缓存**成功**的加载结果：引导期（引擎/资产都缺失）失败后一旦就位，
    下一次调用仍能切换到权威实现。
    """
    global _CANONICAL_LAYOUT_MODULE
    if _CANONICAL_LAYOUT_MODULE is not None:
        return _CANONICAL_LAYOUT_MODULE
    try:
        from bugx_cli.infra.engine_installer import asset_root, global_engine_root
    except Exception:  # noqa: BLE001 - 引导期导入失败时回落
        return None
    candidates = []
    try:
        candidates.append(asset_root())
    except Exception:  # noqa: BLE001
        pass
    try:
        candidates.append(global_engine_root())
    except Exception:  # noqa: BLE001
        pass
    import importlib.util
    for root in candidates:
        script = Path(root) / "bugx_paths.py"
        try:
            if not script.is_file() or script.is_symlink():
                continue
            spec = importlib.util.spec_from_file_location(
                "bugx_cli_infra_canonical_layout", script,
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # @dataclass 装饰器需要能从 sys.modules 取回自身模块命名空间。
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(spec.name, None)
                raise
        except Exception:  # noqa: BLE001
            continue
        if not (callable(getattr(module, "assert_no_symlink_components", None))
                and callable(getattr(module, "is_reparse_point", None))):
            continue
        _CANONICAL_LAYOUT_MODULE = module
        return module
    return None


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point.

    DES-17：正常环境下委托 ``bugx_paths.is_reparse_point``（唯一平台分支）；
    仅在引擎/资产目录都不可用时才使用本地最小判定。
    """
    canonical = _canonical_bugx_paths()
    if canonical is not None:
        return bool(canonical.is_reparse_point(path))
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_attributes = kernel32.GetFileAttributesW
        get_attributes.argtypes = [ctypes.c_wchar_p]
        get_attributes.restype = ctypes.c_uint32
        attributes = get_attributes(str(path))
        if attributes == 0xFFFFFFFF:
            return ctypes.get_last_error() not in (2, 3)
        return bool(attributes & 0x400)
    except (AttributeError, OSError, ValueError):
        return False


def _assert_no_symlink_components(path: Path) -> None:
    """Reject existing symlink/reparse-point components before any BugX write.

    DES-17：委托 ``bugx_paths.assert_no_symlink_components``——符号链接防越界
    只保留一份实现（含 Windows junction/reparse 平台分支）。``allow_var_alias``
    保持旧版本函数的严格语义（不豁免顶层 ``/var`` 别名）；仅当权威模块不可
    加载时才使用下方等价的最小本地实现。
    """
    canonical = _canonical_bugx_paths()
    if canonical is not None:
        canonical.assert_no_symlink_components(
            path,
            CliError,
            lambda _current, target: _i18n_exc(
                "common.symlink_write_refused",
                f"拒绝通过符号链接写入: {target}",
                path=target,
            ),
            False,
        )
        return
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_reparse_point(current):
            raise CliError(_i18n_exc("common.symlink_write_refused", f"拒绝通过符号链接写入: {path}", path=path))


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically publish a private initialization marker."""
    if path.is_symlink():
        raise CliError(_i18n_exc("common.symlink_json_write_refused", f"拒绝写入符号链接 JSON: {path}", path=path))
    _assert_no_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _controlled_core_resource(root: Path, candidate: Path, resolved: Path) -> bool:
    """Return whether a read-only path is trusted under Layout 2.

    Layout 2 has no project executable-core surface.  Keeping this guard
    false prevents old ``.bugx/core`` links from becoming an accidental
    compatibility channel back into the global engine.
    """
    del root, candidate, resolved
    return False


def _guard_plugin_write_target(target: Path) -> None:
    """拒绝插件写入穿透项目边界（P1-2）。"""
    if ".." in target.parts:
        raise CliError(_i18n_exc("common.plugin_write_path_escapes_parent", f"插件写入路径包含父目录跳转: {target}", target=target))
    _assert_no_symlink_components(target)
    if target.is_symlink():
        raise CliError(_i18n_exc("common.plugin_write_target_symlink_refused", f"插件写入目标不得是符号链接: {target}", target=target))


def _backup_host_resource(
    root: Path, host_name: str, source: Path, backup_dir: Path | None = None,
) -> Path:
    """备份一个即将被修改/删除的宿主资源文件。"""
    backup_base = backup_dir if backup_dir is not None else (root.resolve() / ".bugx" / "backups")
    dir_path = backup_base / host_name
    _assert_no_symlink_components(dir_path)
    dir_path.mkdir(parents=True, exist_ok=True)
    backup = dir_path / f"{source.name}.{time.time_ns()}.bak"
    shutil.copy2(source, backup)
    backup.chmod(0o600)
    return backup


def _write_host_resource(
    root: Path, target: Path, content: bytes, host_name: str,
) -> tuple[int, int, Path | None]:
    """Atomically write a host resource file with change detection and backup."""
    _assert_no_symlink_components(target)
    if target.is_symlink():
        raise CliError(_i18n_exc("common.host_resource_symlink_refused", f"拒绝写入符号链接宿主资源: {target}", target=target))
    backup: Path | None = None
    if target.exists():
        if not target.is_file():
            raise CliError(_i18n_exc("common.host_resource_not_regular_file", f"宿主资源不是普通文件: {target}", target=target))
        try:
            if target.read_bytes() == content:
                return 0, 1, None
        except OSError as exc:
            raise CliError(_i18n_exc("common.existing_host_resource_unreadable", f"无法读取已有宿主资源，未修改: {target}: {exc}", target=target, exc=exc)) from exc
        backup = _backup_host_resource(root, host_name, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        if target.exists():
            os.chmod(temporary, target.stat().st_mode)
        else:
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except (OSError, ValueError) as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise CliError(_i18n_exc("common.host_resource_write_failed", f"写入宿主资源失败 {target}: {exc}", target=target, exc=exc)) from exc
    return 1, 0, backup


def _atomic_write_global_file(path: Path, content: bytes) -> None:
    """Atomically publish a BugX-owned file outside the project root."""
    absolute = Path(os.path.abspath(str(path)))
    if str(absolute).startswith("/var/") or str(absolute).startswith("/private/var/"):
        absolute = Path(os.path.realpath(str(absolute)))
    parent = absolute.parent
    _assert_no_symlink_components(parent)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / path.name
    if target.is_symlink():
        raise CliError(_i18n_exc("common.global_resource_symlink_refused", f"拒绝写入符号链接全局资源: {target}", target=target))
    descriptor, temporary = tempfile.mkstemp(
        dir=str(parent), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, target.stat().st_mode if target.exists() else 0o600)
        os.replace(temporary, target)
    except (OSError, ValueError) as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise CliError(_i18n_exc("common.global_resource_write_failed", f"写入全局宿主资源失败 {target}: {exc}", target=target, exc=exc)) from exc


def _sha256_file(path: Path) -> str:
    """Return a file hash used for ownership-safe uninstall decisions."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_record_target(root: Path, relative: str) -> Path:
    from bugx_cli.infra.engine_installer import global_engine_root
    relative_path = Path(relative) if isinstance(relative, str) else Path("")
    if not isinstance(relative, str) or not relative or relative_path.is_absolute():
        raise CliError(_i18n_exc("common.uninstall_manifest_invalid_path", f"卸载清单包含无效路径: {relative!r}", relative=repr(relative)))
    if ".." in relative_path.parts:
        raise CliError(_i18n_exc("common.uninstall_manifest_parent_traversal", f"卸载清单路径包含父目录跳转: {relative}", relative=relative))
    root = root.resolve()
    target = root / relative_path
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise CliError(_i18n_exc("common.uninstall_manifest_path_escapes", f"卸载清单路径逃出项目根目录: {relative}", relative=relative)) from exc
    parent = root
    engine_root = global_engine_root().resolve()
    for part in relative_path.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            if not parent.resolve().is_relative_to(engine_root):
                raise CliError(_i18n_exc("common.uninstall_parent_symlink_refused", f"卸载清单父目录是符号链接，拒绝操作: {relative}", relative=relative))
    return target
