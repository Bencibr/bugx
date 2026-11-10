#!/usr/bin/env python3
"""Reusable fail-closed atomic file write.

Single audited implementation of "write a temp file in the target directory,
fsync, then atomically replace" that closes the TOCTOU window where the parent
directory is swapped for a symlink between the confinement check and the final
``os.replace``.

Technique
---------
Hold an open directory file descriptor (dirfd) for the parent directory and
perform every step — temp creation, write, replace — relative to that dirfd
via ``openat``/``renameat``.  Even if the parent path is renamed away and
replaced with a symlink after we open it, our operations still target the
original directory, never the symlink target.

macOS /var alias
----------------
``O_NOFOLLOW`` only applies to the *final* path component, so system-level
aliases such as ``/var -> /private/var`` are followed normally, while a
symlink at the actual write target (the parent directory) fails closed.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any


class AtomicWriteError(OSError):
    """Raised when a secure atomic write cannot be completed safely."""


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# dirfd confinement (openat/renameat) is POSIX-only.  On Windows the CRT
# cannot open directories at all (EACCES) and os.open/os.replace reject their
# dir_fd arguments, so a path-based fallback is used there instead.
#
# 探测必须**实测行为**，不能查 ``os.supports_dir_fd``。两个独立原因：
#
# 1. CPython 的 ``os.supports_dir_fd`` 只登记 ``os.rename``，**从不包含
#    ``os.replace``**（尽管 replace 实际接受 src_dir_fd/dst_dir_fd）。因此
#    ``os.replace in os.supports_dir_fd`` 在任何平台恒为假——旧写法让
#    ``_write_confined`` 变成死代码，docstring 承诺的 dirfd/TOCTOU 加固
#    从未真正生效。
# 2. 该集合比较的是函数对象标识，任何对 ``os.open`` 的包装（宿主 brokering
#    层、测试 mock）都会让判定误报「不支持」。
#
# 真实调用一次 openat/renameat/unlinkat，以实际能力为准：既修好死代码，
# 也在真正不支持 *at() 语义时安全降级。
def _probe_dirfd_confinement() -> bool:
    """实测 *at() 语义是否可用；失败一律视为不支持（fail-closed）。"""
    if os.name != "posix" or not hasattr(os, "fchmod"):
        return False
    if not _O_DIRECTORY or not _O_NOFOLLOW:
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dirfd = os.open(tmp, os.O_RDONLY | _O_DIRECTORY)
            try:
                handle = os.open(
                    "probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                    dir_fd=dirfd,
                )
                os.close(handle)
                os.replace("probe", "probe2", src_dir_fd=dirfd, dst_dir_fd=dirfd)
                os.unlink("probe2", dir_fd=dirfd)
            finally:
                os.close(dirfd)
        return True
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        return False


_DIRFD_CONFINEMENT = _probe_dirfd_confinement()


def _system_alias(path: Path) -> Path | None:
    """Return the real path for macOS's harmless /tmp and /var aliases."""
    if path.parent != Path("/") or path.name not in {"tmp", "var"}:
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    expected = Path("/private") / path.name
    return resolved if resolved == expected else None


def reject_symlink_components(path: Path) -> Path:
    """Return the abspath of *path*, failing closed if any component from the
    deepest existing non-symlink ancestor down to *path* is a symlink.

    System-level aliases (e.g. macOS ``/var -> /private/var``) sit above the
    deepest existing non-symlink ancestor and are therefore not treated as an
    attack surface; a symlink inside the user-writable area is.
    """
    absolute = Path(os.path.abspath(str(path)))
    if absolute.is_symlink() and _system_alias(absolute) is None:
        raise AtomicWriteError(f"拒绝通过符号链接写入: {path}")
    boundary = absolute.parent
    while boundary != boundary.parent and (
        not boundary.exists() or boundary.is_symlink()
    ):
        boundary = boundary.parent
    current = boundary
    for part in absolute.relative_to(boundary).parts:
        current = current / part
        if current.is_symlink() and _system_alias(current) is None:
            raise AtomicWriteError(f"拒绝通过符号链接写入: {path}")
    return absolute


def _open_parent_dir(parent: Path) -> int:
    """Open *parent* as a directory without following a final-component symlink.

    Returns an open dirfd.  Fails closed if *parent* is a symlink or not a
    directory.
    """
    flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open(str(parent), flags)
    except OSError as exc:
        raise AtomicWriteError(f"无法安全打开写入目录: {parent}: {exc}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise AtomicWriteError(f"写入目标不是目录: {parent}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise AtomicWriteError("写入临时文件失败")
        view = view[written:]


def _write_confined(
    target: Path, content: bytes, *, mode: int | None, parent_mode: int | None,
) -> None:
    """POSIX write path: every step is pinned to an open parent dirfd."""
    parent = _system_alias(target.parent) or target.parent
    dirfd = _open_parent_dir(parent)
    temp_name = f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = -1
    try:
        if parent_mode is not None:
            os.fchmod(dirfd, parent_mode)
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o600,
            dir_fd=dirfd,
        )
        _write_all(fd, content)
        os.fsync(fd)
        if mode is not None:
            os.fchmod(fd, mode)
        os.close(fd)
        fd = -1
        os.replace(temp_name, target.name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temp_name, dir_fd=dirfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dirfd)


def _write_path_based(
    target: Path, content: bytes, *, mode: int | None, parent_mode: int | None,
) -> None:
    """Windows/other write path: temp + replace by absolute path.

    The dirfd TOCTOU hardening is unavailable here.  The symlink defenses that
    do port (final-component check, reject_symlink_components pre- and
    post-mkdir) are still enforced, and on Windows unprivileged sessions
    cannot create symlinks at all, which closes the same attack in practice.
    """
    temp_path = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = -1
    try:
        fd = os.open(
            str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600,
        )
        _write_all(fd, content)
        os.fsync(fd)
        if mode is not None:
            os.close(fd)
            fd = -1
            os.chmod(temp_path, mode)
        else:
            os.close(fd)
            fd = -1
        os.replace(str(temp_path), str(target))
    except BaseException:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    if parent_mode is not None:
        # Windows chmod only toggles the read-only bit; modes carrying the
        # user-write bit leave the directory writable, so this is harmless.
        try:
            os.chmod(target.parent, parent_mode)
        except OSError:
            pass


def atomic_write_bytes(
    path: str | os.PathLike[str],
    content: bytes,
    *,
    mode: int | None = None,
    parent_mode: int | None = None,
) -> None:
    """Atomically write *content* to *path*, fail-closed on symlink parents.

    On POSIX the parent directory is opened once and held as a dirfd; the temp
    file is created, written, fsynced and renamed all relative to that dirfd,
    so a parent-directory swap to a symlink after the open cannot redirect the
    write.  Platforms without *at() support (Windows) use the path-based
    variant with the same fail-closed symlink checks.
    """
    target = Path(path)
    if target.is_symlink():
        raise AtomicWriteError(f"拒绝通过符号链接写入: {target}")
    # Lexical pre-check for intermediate symlink components (handles /var).
    reject_symlink_components(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after mkdir: a symlink may have been planted during creation.
    reject_symlink_components(target.parent)
    writer = _write_confined if _DIRFD_CONFINEMENT else _write_path_based
    writer(target, content, mode=mode, parent_mode=parent_mode)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    mode: int | None = None,
    parent_mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Atomically write *text* to *path* (see :func:`atomic_write_bytes`)."""
    atomic_write_bytes(path, text.encode(encoding), mode=mode, parent_mode=parent_mode)


def atomic_write_json(
    path: str | os.PathLike[str],
    payload: Any,
    *,
    mode: int | None = None,
    parent_mode: int | None = None,
    indent: int | None = 2,
) -> None:
    """Atomically write *payload* as JSON to *path* (see :func:`atomic_write_bytes`)."""
    data = json.dumps(
        payload, ensure_ascii=False, indent=indent, sort_keys=True, allow_nan=False,
    )
    atomic_write_bytes(path, (data + "\n").encode("utf-8"), mode=mode,
                       parent_mode=parent_mode)
