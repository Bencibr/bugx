#!/usr/bin/env python3
"""BugX project runtime layout and recovery-safe path resolver."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# Layout 2 removes the project-local executable engine.  ``.bugx`` now stores
# only project state/configuration; executable assets are resolved from the
# trusted global engine by the CLI/bootstrap layer.
LAYOUT_VERSION = "2.0.0"
POLLING_ID_RE = re.compile(r"^polling-[0-9]{6,}$")
LIFECYCLE_ID_RE = re.compile(r"^lifecycle-[0-9]{6,}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\+[0-9A-Za-z.-]+)?$")

# 版本契约的**唯一**归一化规则（DES-17 单一实现来源）。
#
# 兼容边界是 `major.minor.patch`；预发布后缀（`rc1`/`beta2`…）与构建/本地
# 后缀（`+2`）都不构成不兼容跃迁，因此不参与契约比较。历史缺陷：门禁的
# `_normalize_cli_version` 把 `+build` 留在 base 组里，而 CLI 侧
# `_contract_version` 把它丢掉——同一份清单，`bugx doctor` 判"一致"、
# `bugx engine` 门禁判"不一致并拒绝启动"，两条入口互相打脸。
CONTRACT_VERSION_RE = re.compile(
    r"^v?(?P<base>\d+\.\d+\.\d+)"
    r"(?:[-.]?(?:alpha|a|beta|b|rc)[-.]?\d+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)


def contract_version(value: object) -> str | None:
    """Return the compatibility contract version (``x.y.z``).

    预发布与构建后缀被忽略：``0.1.0+2``、``0.1.0rc1``、``v0.1.0`` 都归一到
    ``0.1.0``。这是门禁与 CLI 共用的唯一实现；任何一侧自行实现比较规则都会
    重新引入"两个入口结论相反"的缺陷。
    """
    if not isinstance(value, str):
        return None
    match = CONTRACT_VERSION_RE.fullmatch(value.strip())
    return match.group("base") if match else None

# These names are data or preparation artifacts, not executable engine assets.
# Layout 2 keeps them under the project state tree; the project has no
# executable core link or snapshot.
PROJECT_OWNED_CORE_NAMES = frozenset({
    "bug-log.md",
    "mistake-book.md",
    "lesson-ledger.json",
    "lesson-ledger.json.lock",
    "prep-record.md",
    "module-coverage.md",
})

#: 同时是**项目数据名**与**引擎载荷模板**的文件名（D1 根因修正）。
#:
#: ``PROJECT_OWNED_CORE_NAMES`` 只说明「在项目树里这些文件属于用户数据，不进
#: 项目核心完整性校验」。历史缺陷：引擎安装器把同一份名单直接当成「全局引擎
#: 载荷剥离名单」，于是 ``~/.bugx/core/lesson-ledger.json``（内置知识种子模板）
#: 等模板从未被安装，而 ``project_init._initialize_project_ledger``、
#: ``launch_phases.common.LESSON_LEDGER_FILE``、``incident_eval.run`` 都要求
#: 该模板存在于引擎根 —— 新项目 ``pre`` 直接报「缺少内置知识种子」。
#:
#: 语义边界：**项目树**里这些名字永远是项目数据（照旧剥离/忽略）；**引擎树**
#: 里它们是随包分发的只读模板（必须安装，安装后不得被运行态写坏）。
ENGINE_ASSET_SEED_NAMES = frozenset({
    "lesson-ledger.json",
    "bug-log.md",
    "mistake-book.md",
})



class LayoutError(RuntimeError):
    """The project runtime layout is missing, invalid, or unsafe."""


def _version_from_text(path: Path, pattern: re.Pattern[str],
                       *, allow_symlink: bool = False) -> str | None:
    """Read a version marker without trusting malformed or symlinked files."""
    if allow_symlink and path.is_symlink():
        try:
            path = path.resolve(strict=True)
        except OSError:
            return None
    else:
        _assert_no_symlink_components(path)
    if not path.is_file():
        return None
    try:
        match = pattern.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None
    value = match.group(1) if match else None
    return value if value and VERSION_RE.fullmatch(value) else None


def _version_from_json(path: Path, key: str) -> str | None:
    """Read a version field from a trusted JSON object."""
    _assert_no_symlink_components(path)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) and VERSION_RE.fullmatch(value) else None


# PERF-08：项目核心完整性校验的 (size, mtime_ns) 指纹缓存。
# 每次启动对 192 个引擎文件做 read_bytes()+sha256 约 443.7 ms（冷读）/ 14.5 ms
# （热读），其中 99% 的成本是重复哈希未变化的文件。缓存把「上次已与清单核对
# 通过」的文件指纹记在 .bugx/index/ 下，下次启动只做 stat()；指纹变化（或缓存
# 缺失/损坏/清单变更）时仍回退到全量哈希——**判定强度不降低**。
#
# 残余理论盲点：攻击者改写文件后把 mtime 精确还原成缓存记录的值且保持 size
# 不变，可命中快速路径。为缩小该窗口，文件的 mtime 若晚于清单本身（说明它
# 在清单生成之后被动过）也强制重新哈希。
_INTEGRITY_STAT_CACHE_SCHEMA = 1
_INTEGRITY_STAT_CACHE_NAME = "engine-integrity-statcache.json"


def _integrity_stat_cache_path(root: Path) -> Path:
    return bugx_root(root) / "index" / _INTEGRITY_STAT_CACHE_NAME


def _load_integrity_stat_cache(
    root: Path, manifest_sha256: str,
) -> dict[str, tuple[int, int, str]]:
    """读取上次校验的 (size, mtime_ns, sha256) 指纹。

    任何异常（缺失、损坏、schema 不符、清单已变更）都返回空表，调用方随即
    回退到全量哈希——缓存失效必须是 fail-closed，绝不能反过来放宽判定。
    """
    path = _integrity_stat_cache_path(root)
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") != _INTEGRITY_STAT_CACHE_SCHEMA:
        return {}
    if payload.get("manifest_sha256") != manifest_sha256:
        return {}
    entries = payload.get("files")
    if not isinstance(entries, dict):
        return {}
    result: dict[str, tuple[int, int, str]] = {}
    for relative, entry in entries.items():
        if not isinstance(relative, str) or not isinstance(entry, dict):
            continue
        size = entry.get("size")
        mtime_ns = entry.get("mtime_ns")
        sha = entry.get("sha256")
        if isinstance(size, int) and isinstance(mtime_ns, int) and isinstance(sha, str):
            result[relative] = (size, mtime_ns, sha)
    return result


def _save_integrity_stat_cache(
    root: Path, manifest_sha256: str,
    fingerprints: dict[str, tuple[int, int, str]],
) -> None:
    """尽力持久化指纹缓存；写入失败（只读项目等）不影响本次判定结果。"""
    payload = {
        "schema_version": _INTEGRITY_STAT_CACHE_SCHEMA,
        "manifest_sha256": manifest_sha256,
        "files": {
            relative: {"size": size, "mtime_ns": mtime_ns, "sha256": sha}
            for relative, (size, mtime_ns, sha) in fingerprints.items()
        },
    }
    try:
        _atomic_json(_integrity_stat_cache_path(root), payload)
    except (OSError, ValueError, LayoutError):
        pass


def _project_core_files(project: Path) -> set[str]:
    """Enumerate materialized compatibility-core files without following dirs."""
    files: set[str] = set()
    if not project.is_dir() or project.is_symlink():
        return files
    for path in project.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(project)
        if relative.name in PROJECT_OWNED_CORE_NAMES or relative.name == "engine-integrity.json":
            continue
        files.add(relative.as_posix())
    return files


def project_core_integrity_status(root: Path, engine_root: Path) -> dict[str, Any]:
    """Report the removed project executable-engine surface.

    Layout 2 deliberately does not hash or repair ``.bugx/core``.  A project
    that still has that path is an un-migrated legacy installation.  The next
    ``init`` or ``install`` deletes it after moving recognised project data.
    A project without it is healthy: the global engine is checked by the
    engine contract itself.
    """
    project = bugx_root(root) / "core"
    try:
        if project.is_symlink() or project.exists():
            return {
                "available": True,
                "ok": False,
                "detail": f"旧项目引擎入口仍存在: {project}；运行 bugx init/install 将直接删除",
            }
    except OSError as exc:
        return {"available": True, "ok": False, "detail": f"旧项目引擎入口无法检查: {exc}"}
    return {
        "available": True,
        "ok": True,
        "detail": "项目不包含可执行引擎；运行时统一使用全局引擎",
    }


def version_status(root: Path, engine_root: Path,
                   *, cli_version: str | None = None) -> dict[str, Any]:
    """Return the global-engine version contract for a running project."""
    root = root.resolve()
    engine_root = engine_root.resolve()
    values: dict[str, str | None] = {
        "cli": cli_version,
        "engine_metadata": _version_from_text(
            engine_root / "metadata.txt",
            re.compile(r'bugx_version:\s*"([\d.]+(?:\+[0-9A-Za-z.-]+)?)"'),
        ),
        "engine_integrity": _version_from_json(
            engine_root / "engine-integrity.json", "bugx_version",
        ),
        "engine_descriptor": _version_from_text(
            engine_root / "bugx.md",
            re.compile(r"^version:\s*([\d.]+(?:\+[0-9A-Za-z.-]+)?)", re.M),
        ),
    }
    legacy_manifest = _version_from_json(
        bugx_root(root) / "manifest.json", "bugx_version",
    )
    project_layout = _version_from_text(
        bugx_root(root) / "manifest.json",
        re.compile(r'"layout_version"\s*:\s*"([\d.]+)"'),
    )
    # 契约比较只看 x.y.z，忽略预发布/构建后缀——与 CLI 侧（doctor）共用
    # `contract_version` 这一条规则，避免两条入口对同一份清单给出相反结论。
    contract_values = {
        name: contract_version(value) for name, value in values.items()
    }
    contract_manifest = contract_version(legacy_manifest)
    errors: list[str] = []
    for name, value in values.items():
        if value is None:
            errors.append(f"{name}=missing_or_invalid")
    if project_layout != LAYOUT_VERSION:
        errors.append(
            f"project_layout={project_layout or 'missing'}（项目状态格式应为 {LAYOUT_VERSION}）"
        )
    distinct = sorted({value for value in contract_values.values() if value is not None})
    project_integrity = project_core_integrity_status(root, engine_root)
    if project_integrity.get("available") and not project_integrity.get("ok"):
        errors.append("project_core_integrity=failed")
    if len(distinct) > 1:
        errors.append("版本不一致")
    if legacy_manifest is not None and distinct and contract_manifest not in distinct:
        errors.append(f"project_manifest={legacy_manifest}（与核心版本不一致）")
    if errors:
        detail = "；".join(errors)
    else:
        detail = f"所有核心版本标记一致：{distinct[0]}"
    return {
        "consistent": not errors,
        "versions": values,
        "legacy_versions": {
            "project_manifest": legacy_manifest,
            "project_layout": project_layout,
        },
        "errors": errors,
        "detail": detail,
        "project_core_integrity": project_integrity,
    }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def project_root(script_dir: Path | None = None) -> Path:
    override = os.environ.get("BUGX_PROJECT_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise LayoutError(f"BUGX_PROJECT_ROOT 不是目录: {root}")
        return root
    directory = (script_dir or Path(__file__).resolve()).resolve()
    # DOC-004 Engine-State Separation：静态协议脚本位于全局引擎
    # ~/.bugx/core，其父目录名也是 .bugx，与目录名检测冲突——必须
    # 优先判定。命中全局引擎时回退到 cwd（调用方已 cd 到项目根），
    # 与 BUGX_PROJECT_ROOT 显式覆盖共同保证正确性。
    global_engine_marker = Path.home() / ".bugx" / "core"
    if (
        directory == global_engine_marker
        or directory.is_relative_to(global_engine_marker)
    ):
        return Path.cwd().resolve()
    if directory.name == "core" and directory.parent.name == ".bugx":
        return directory.parent.parent
    return Path.cwd().resolve()


def bugx_root(root: Path) -> Path:
    return root.resolve() / ".bugx"


def project_knowledge_root(root: Path) -> Path:
    return bugx_root(root) / "knowledge"


def global_knowledge_root() -> Path:
    override = os.environ.get("BUGX_GLOBAL_KNOWLEDGE_ROOT")
    return (Path(override).expanduser() if override else Path.home() / ".bugx" / "knowledge").resolve()


def global_engine_root() -> Path:
    """Return the trusted global BugX engine root directory."""
    override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT")
    return (Path(override).expanduser() if override else Path.home() / ".bugx" / "core").resolve()


def global_skills_dir() -> Path:
    """Return the global skills directory without creating it."""
    override = os.environ.get("BUGX_GLOBAL_SKILLS_DIR")
    return (Path(override).expanduser() if override else Path.home() / ".bugx" / "skills").resolve()


def global_cache_root() -> Path:
    """Return the per-user cache root without creating or resolving it."""
    override = os.environ.get("BUGX_GLOBAL_CACHE_ROOT")
    return Path(override).expanduser() if override else Path.home() / ".bugx" / "cache"


def global_telemetry_root() -> Path:
    override = os.environ.get("BUGX_GLOBAL_TELEMETRY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    knowledge_override = os.environ.get("BUGX_GLOBAL_KNOWLEDGE_ROOT")
    if knowledge_override:
        return (Path(knowledge_override).expanduser().resolve().parent / "telemetry").resolve()
    return (Path.home() / ".bugx" / "telemetry").resolve()


def global_telemetry_path() -> Path:
    override = os.environ.get("BUGX_GLOBAL_TELEMETRY_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return global_telemetry_root() / "stats.sqlite3"


def is_reparse_point(path: Path) -> bool:
    """Return whether ``path`` is a symlink, a Windows junction, or a reparse point.

    DES-17：这是符号链接防越界的**唯一平台分支**。POSIX 上
    ``is_symlink()`` 已足够；Windows 上 junction 与其它 reparse point
    同样是可被用来把写入重定向到边界之外的目录重定向，必须一并拒绝，
    否则同一条安全路径在 Windows 上会被绕过。
    """
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
            # 路径不存在（2/3）不算 reparse；其它错误保守视为不安全。
            return ctypes.get_last_error() not in (2, 3)
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (AttributeError, OSError, ValueError):
        return False


def assert_no_symlink_components(
    path: Path,
    error_cls: type[Exception] = LayoutError,
    message_formatter: Any = None,
    allow_var_alias: bool = True,
) -> None:
    """Reject a path whose existing parent or target component is a symlink.

    ``Path.resolve()`` alone is insufficient: it follows the link before the
    confinement check and can therefore make an escaped path look legitimate.
    This check is intentionally lexical and runs before every runtime mkdir or
    atomic replace performed by this module.

    DES-17：组件判定统一走 :func:`is_reparse_point`，Windows 上同时覆盖
    junction / reparse point；``src/bugx_cli/infra/security.py`` 与
    ``core/verify_life.py`` 均委托到本函数，不再各自维护一份实现。
    """
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if is_reparse_point(current):
            if allow_var_alias and current.parent == Path(absolute.anchor):
                continue
            if callable(message_formatter):
                err_msg = message_formatter(current, path)
            elif isinstance(message_formatter, str):
                err_msg = message_formatter
            elif error_cls.__name__ == "SyncStoreError":
                err_msg = f"sync store 路径包含符号链接: {current}"
            elif error_cls.__name__ == "CliError":
                err_msg = f"拒绝通过符号链接写入: {path}"
            elif error_cls.__name__ in ("LessonLedgerError", "OSError"):
                err_msg = f"拒绝使用符号链接路径组件: {path}"
            else:
                err_msg = f"拒绝使用符号链接路径组件: {current}"
            raise error_cls(err_msg)


_assert_no_symlink_components = assert_no_symlink_components


def ensure_dir(path: Path, mode: int | None = None) -> Path:
    """Create ``path`` if it is not already a directory.

    ``Path.mkdir(exist_ok=True)`` is the idiomatic form, but it is not portable
    across the brokered filesystems BugX runs inside: some host shims replace
    ``Path.mkdir`` with a version that raises ``PermissionError: EEXIST`` when
    the target already exists instead of honouring ``exist_ok``.  Since BugX is
    designed to run *inside* sandboxed hosts, the explicit ``is_dir`` pre-check
    keeps directory setup idempotent there as well.
    """
    if path.is_dir():
        return path
    if mode is None:
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
    return path


def confined_regular_file(
    root: Path,
    raw_path: str | Path,
    *,
    error_cls: type[Exception] = LayoutError,
) -> Path:
    """Resolve ``raw_path`` inside ``root`` and prove it is a regular file.

    ``resolve() + relative_to()`` alone is not enough: ``resolve`` follows
    symlinks *before* the boundary check, so a link inside the project can make
    an out-of-tree file look legitimate.  This helper therefore checks the
    lexical path first, then every component for symlinks, and only then
    canonicalises.  It is the single entry point for "read a project file named
    by untrusted input" (BugX audits other people's repositories, so every path
    that arrives over MCP or from a project artifact is untrusted).
    """
    root_raw = Path(os.path.abspath(str(Path(root))))
    root_resolved = Path(root).resolve()
    raw = Path(raw_path)
    candidate = raw if raw.is_absolute() else root_raw / raw
    resolved = candidate.resolve()

    # Canonical confinement is authoritative: a link inside the project that
    # points outside resolves to an outside path and is rejected here.
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise error_cls(f"路径必须位于项目根目录内: {raw_path}") from exc
    if resolved == root_resolved:
        raise error_cls(f"路径必须指向项目内的文件: {raw_path}")

    # Refuse symlinked components as well (stronger than the boundary check:
    # a link that still resolves inside the project is refused too, because it
    # can be swapped between this check and the read).
    #
    # When the caller used a system alias (macOS ``/var`` -> ``/private/var``)
    # the raw and canonical spellings share no lexical prefix, so only the
    # canonical confinement above applies; otherwise walk the raw in-project
    # components.
    lexical = Path(os.path.abspath(str(candidate)))
    try:
        raw_relative = lexical.relative_to(root_raw)
    except ValueError:
        raw_relative = None
    if raw_relative is not None:
        current = root_raw
        for part in raw_relative.parts:
            current /= part
            if current.is_symlink():
                raise error_cls(f"路径不能包含符号链接: {raw_path}")

    if not resolved.is_file():
        raise error_cls(f"路径不是普通文件: {raw_path}")
    return resolved


def read_confined_bytes(
    root: Path,
    raw_path: str | Path,
    *,
    max_bytes: int | None = None,
    error_cls: type[Exception] = LayoutError,
) -> bytes:
    """Read a project-confined regular file with an optional size ceiling.

    The ceiling is enforced *after* reading only because a regular file cannot
    be safely pre-sized for the caller's purpose; callers that need a hard
    memory bound must pass ``max_bytes`` and treat the failure as fatal.
    """
    path = confined_regular_file(root, raw_path, error_cls=error_cls)
    if max_bytes is not None and max_bytes > 0:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise error_cls(f"无法读取文件大小: {raw_path}") from exc
        if size > max_bytes:
            raise error_cls(f"文件超过大小上限（{size} > {max_bytes}）: {raw_path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise error_cls(f"无法读取文件: {raw_path}") from exc


def _validate_identifier(value: object, kind: str) -> str:
    if not isinstance(value, str):
        raise LayoutError(f"{kind} 标识无效: {value!r}")
    pattern = POLLING_ID_RE if kind == "polling" else LIFECYCLE_ID_RE
    if not pattern.fullmatch(value):
        raise LayoutError(f"{kind} 标识无效: {value!r}")
    return value


def _id_path(root: Path, value: str, kind: str) -> Path:
    identifier = _validate_identifier(value, kind)
    candidate = root / identifier
    _assert_no_symlink_components(root)
    _assert_no_symlink_components(candidate)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:  # defensive: the identifier regex must be enough
        raise LayoutError(f"{kind} 路径越出受控目录: {value!r}") from exc
    return candidate


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _assert_no_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    _assert_no_symlink_components(path)
    if path.is_symlink() or not path.is_file():
        raise LayoutError(f"缺少或不安全的布局文件: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LayoutError(f"布局文件无法解析: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LayoutError(f"布局文件必须是对象: {path}")
    return value


_PROJECT_LOCK_THREAD = threading.RLock()


@contextlib.contextmanager
def _project_lock(root: Path, timeout: float = 10.0):
    """项目级互斥：串行化 Session 创建与 current/ manifest 变更。

    在 ``.bugx/locks/project.lock`` 上使用 ``fcntl.flock``（POSIX）或
    ``msvcrt.locking``（Windows）实现跨进程互斥；缺一不可时显式阻断而非
    静默无锁，避免并发复用同一 Session 编号。进程内再用线程锁避免同进程
    重入竞争。
    """
    root = root.resolve()
    base = bugx_root(root)
    _assert_no_symlink_components(base)
    locks_dir = base / "locks"
    # locks 目录在并发初始化时可能不存在，创建放在线程锁外但需校验
    # 符号链接，避免把锁文件建在被劫持的目录。
    locks_dir.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(locks_dir)
    try:
        os.chmod(locks_dir, 0o700)
    except OSError:
        pass
    lock_path = locks_dir / "project.lock"
    _assert_no_symlink_components(lock_path.parent)
    if lock_path.is_symlink():
        raise LayoutError(f"拒绝使用符号链接锁文件: {lock_path}")
    # 线程内串行化，避免同进程多线程同时抢文件锁
    with _PROJECT_LOCK_THREAD:
        if lock_path.is_symlink():
            raise LayoutError(f"拒绝使用符号链接锁文件: {lock_path}")
        # O_NOFOLLOW closes the check-then-open symlink race on POSIX.  The
        # explicit pre-check remains useful on platforms without the flag;
        # either way, never follow a lock path supplied by an attacker.
        open_flags = os.O_RDWR | os.O_CREAT
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(lock_path), open_flags | nofollow, 0o600)
        except OSError as exc:
            raise LayoutError(f"无法安全打开项目锁: {lock_path}") from exc
        handle = os.fdopen(descriptor, "a+")
        try:
            try:
                import fcntl as _fcntl
            except ImportError:
                _fcntl = None  # type: ignore[assignment]
            try:
                import msvcrt as _msvcrt
            except ImportError:
                _msvcrt = None  # type: ignore[assignment]
            if _fcntl is not None:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LayoutError("项目锁等待超时")
                        time.sleep(0.01)
            elif _msvcrt is not None:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise LayoutError("项目锁等待超时")
                        time.sleep(0.01)
            else:  # pragma: no cover - 无任何可用锁原语的平台
                raise LayoutError("项目锁在当前平台不可用（fcntl 和 msvcrt 均缺失），拒绝并发不安全写")
            yield
        finally:
            try:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    try:
                        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                handle.close()


def _validate_polling_manifest(root: Path, polling_id: str, expected_no: int | None = None) -> dict[str, Any]:
    """校验 polling manifest 与磁盘指针一致性。"""
    base = bugx_root(root)
    polling_root = _id_path(base / "pollings", polling_id, "polling")
    if not polling_root.is_dir() or polling_root.is_symlink():
        raise LayoutError(f"polling 目录缺失或不安全: {polling_root}")
    manifest_path = polling_root / "manifest.json"
    payload = _read_json(manifest_path)
    if payload.get("polling_id") != polling_id:
        raise LayoutError(f"polling manifest 归属不一致: {polling_id}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LayoutError(f"polling manifest schema_version 不受支持: {manifest_path}")
    polling_no = payload.get("polling_no")
    if isinstance(polling_no, bool) or not isinstance(polling_no, int) or polling_no < 1:
        raise LayoutError(f"polling manifest polling_no 无效: {manifest_path}")
    if expected_no is not None and polling_no != expected_no:
        raise LayoutError(f"polling_no 指针不一致: manifest={polling_no} vs expected={expected_no}")
    # polling state 也作为指针完整性的一部分
    state_path = polling_root / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise LayoutError(f"polling state 缺失或不安全: {state_path}")
    state = _read_json(state_path)
    if state.get("polling_id") != polling_id:
        raise LayoutError(f"polling state 归属不一致: {polling_id}")
    return payload


def _validate_lifecycle_manifest(root: Path, polling_id: str, lifecycle_id: str,
                                 expected_no: int | None = None) -> dict[str, Any]:
    """校验 lifecycle manifest 归属与编号。"""
    base = bugx_root(root)
    polling_root = _id_path(base / "pollings", polling_id, "polling")
    lifecycle_root = _id_path(polling_root / "lifecycles", lifecycle_id, "lifecycle")
    if not lifecycle_root.is_dir() or lifecycle_root.is_symlink():
        raise LayoutError(f"lifecycle 目录缺失或不安全: {lifecycle_root}")
    manifest_path = lifecycle_root / "manifest.json"
    payload = _read_json(manifest_path)
    if payload.get("polling_id") != polling_id:
        raise LayoutError(f"lifecycle manifest polling 归属不一致: {lifecycle_id} not in {polling_id}")
    if payload.get("lifecycle_id") != lifecycle_id:
        raise LayoutError(f"lifecycle manifest 标识不一致: {lifecycle_id}")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LayoutError(f"lifecycle manifest schema_version 不受支持: {manifest_path}")
    lifecycle_no = payload.get("lifecycle_no")
    if isinstance(lifecycle_no, bool) or not isinstance(lifecycle_no, int) or lifecycle_no < 1:
        raise LayoutError(f"lifecycle manifest lifecycle_no 无效: {manifest_path}")
    if expected_no is not None and lifecycle_no != expected_no:
        raise LayoutError(f"lifecycle_no 指针不一致: manifest={lifecycle_no} vs expected={expected_no}")
    # lifecycle state
    state_path = lifecycle_root / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise LayoutError(f"lifecycle state 缺失或不安全: {state_path}")
    state = _read_json(state_path)
    if state.get("lifecycle_id") != lifecycle_id:
        raise LayoutError(f"lifecycle state 标识不一致: {lifecycle_id}")
    return payload


#: ``.bugx`` 里持久化项目身份的键（清单与索引各存一份，互为交叉校验）。
PROJECT_UID_KEY = "project_uid"


def _path_fingerprint(root: Path) -> str:
    """根路径哈希——只作为**旧布局**（无 uid）的身份，以及新布局 uid 的初值。"""
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _new_project_uid(root: Path) -> str:
    """生成与路径无关的项目身份（首次初始化时落盘，此后不再随路径变化）。

    缺陷 #10：``project_id`` 原本恒为 ``sha256(根路径)[:16]``，于是项目目录被
    搬移/改名/换机恢复后，``require()`` 直接抛「.bugx 与当前项目不匹配」，而
    错误给出的唯一出路是 ``rm -rf .bugx``——账本、知识库、报告全部作废。
    身份必须随布局持久化，不能从路径反推。
    """
    return _path_fingerprint(root)


def _read_index_uid(base: Path) -> str:
    """读索引里记录的项目身份；缺失/损坏时返回空串。"""
    try:
        index_path = base / "index" / "index.json"
        if index_path.is_file() and not index_path.is_symlink():
            value = _read_json(index_path).get(PROJECT_UID_KEY)
            return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError, LayoutError):
        return ""
    return ""


def _project_id(root: Path) -> str:
    """项目身份：优先取持久化的 ``project_uid``，旧布局退回路径指纹。

    新布局一旦落盘 uid，身份就与路径解绑——搬移目录后 ``project_id`` 不变。
    旧布局（无 uid）保持原有路径哈希语义，行为与历史完全一致。
    """
    base = bugx_root(root)
    try:
        manifest_path = base / "manifest.json"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            persisted = _read_json(manifest_path).get(PROJECT_UID_KEY)
            if isinstance(persisted, str) and persisted.strip():
                return persisted.strip()
    except (OSError, ValueError, LayoutError):
        # 清单读不出来时退回路径指纹：真正的诊断由 initialize/require 各自报出，
        # 身份计算这一层不吞错也不越权。
        pass
    return _path_fingerprint(root)


def _identity_mismatch(root: Path, manifest: dict) -> bool:
    """``.bugx`` 是否属于**当前**根（fail-closed 判据）。

    判定依旧锚定路径指纹：把 ``.bugx`` 拷进另一个项目必须被拒绝——清单只是
    元数据，而账本、知识库、pollings 报告全在目录里，自动放行等于让源项目的
    洞察/错题集/机密静默流进目标项目（``tests/gates/test_l1b_contract_assertions``
    的 B4 红线）。

    可移植性由**显式迁移**提供，而不是自动放行：``bugx project rebind --migrate``
    把 ``project_id`` 重写为新根指纹，并保留持久化的 ``project_uid``——于是项目
    搬移后身份（云端 wire id、全局工程表）连续，同时"串用他人布局"仍然被拒。
    """
    return manifest.get("project_id") != _path_fingerprint(root)


def _scope_root(root: Path, polling_id: str, lifecycle_id: str) -> Path:
    polling_root = _id_path(bugx_root(root) / "pollings", polling_id, "polling")
    lifecycle_root = _id_path(polling_root / "lifecycles", lifecycle_id, "lifecycle")
    return lifecycle_root


@dataclass(frozen=True)
class Scope:
    root: Path
    polling_root: Path
    session_state: Path
    lifecycle_root: Path
    state_root: Path
    locks_root: Path
    life: Path
    life_snapshot: Path
    life_lock: Path
    runtime: Path
    runtime_lock: Path
    supervisor: Path
    supervisor_lock: Path
    coverage: Path
    prep_record: Path
    events: Path
    conclusion: Path
    report: Path
    integrity: Path
    knowledge_root: Path
    knowledge_seed: Path
    knowledge_db: Path
    bug_log: Path
    mistake_book: Path
    global_knowledge_root: Path
    global_knowledge_seed: Path
    global_knowledge_db: Path
    logs_root: Path
    diagnostic_log: Path


def scope(root: Path, current: dict[str, Any] | None = None) -> Scope:
    root = root.resolve()
    current = current or read_current(root)
    polling_id = current.get("polling_id")
    lifecycle_id = current.get("lifecycle_id")
    if not isinstance(polling_id, str) or not isinstance(lifecycle_id, str):
        raise LayoutError("current.json 缺少 polling_id 或 lifecycle_id")
    polling_root = _id_path(bugx_root(root) / "pollings", polling_id, "polling")
    lifecycle_root = _scope_root(root, polling_id, lifecycle_id)
    state_root = lifecycle_root / "state"
    locks_root = state_root / "locks"
    return Scope(
        root=root,
        polling_root=polling_root,
        session_state=polling_root / "state.json",
        lifecycle_root=lifecycle_root,
        state_root=state_root,
        locks_root=locks_root,
        life=state_root / "life.json",
        life_snapshot=state_root / "snapshots" / "life.snapshot.json",
        life_lock=locks_root / "life.lock",
        runtime=state_root / "runtime.json",
        runtime_lock=locks_root / "runtime.lock",
        supervisor=state_root / "supervisor.json",
        supervisor_lock=locks_root / "supervisor.lock",
        coverage=state_root / "coverage-evidence.json",
        prep_record=state_root / "prep-record.md",
        events=lifecycle_root / "events.jsonl",
        conclusion=lifecycle_root / "conclusion.json",
        report=lifecycle_root / "report.md",
        integrity=lifecycle_root / "integrity.json",
        knowledge_root=project_knowledge_root(root),
        knowledge_seed=project_knowledge_root(root) / "lesson-ledger.json",
        knowledge_db=project_knowledge_root(root) / "lesson-ledger.sqlite3",
        bug_log=project_knowledge_root(root) / "bug-log.md",
        mistake_book=project_knowledge_root(root) / "mistake-book.md",
        global_knowledge_root=global_knowledge_root(),
        global_knowledge_seed=global_knowledge_root() / "global.json",
        global_knowledge_db=global_knowledge_root() / "global.sqlite3",
        logs_root=bugx_root(root) / "logs",
        diagnostic_log=bugx_root(root) / "logs" / "bugx.jsonl",
    )


def read_current(root: Path) -> dict[str, Any]:
    current = _read_json(bugx_root(root) / "current.json")
    if current.get("schema_version") != SCHEMA_VERSION:
        raise LayoutError("current.json schema_version 不受支持")
    _validate_identifier(current.get("polling_id"), "polling")
    _validate_identifier(current.get("lifecycle_id"), "lifecycle")
    for name in ("polling_no", "lifecycle_no"):
        value = current.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LayoutError(f"current.json {name} 无效")
    return current


def _write_current(root: Path, polling_id: str, lifecycle_id: str,
                   polling_no: int, lifecycle_no: int) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "layout_version": LAYOUT_VERSION,
        "polling_id": polling_id,
        "polling_no": polling_no,
        "lifecycle_id": lifecycle_id,
        "lifecycle_no": lifecycle_no,
        "updated_at": utc_now(),
    }
    _atomic_json(bugx_root(root) / "current.json", value)
    # PERF-06：current.json 被改写，显式失效 require() 的 Scope 缓存，
    # 避免仅依赖 mtime 粒度（部分文件系统会粗化时间戳）导致读到过期 Scope。
    clear_require_cache(root)
    return value


def restore_current(root: Path, polling_id: str, lifecycle_id: str) -> Scope:
    """回滚/恢复场景：把 current.json 原子指回既有 Session/Round。

    两个 id 都必须在磁盘上真实存在（各自 manifest 可读）才写回，
    保证 current.json 不指向不存在的布局；编号从 manifest 读取，
    避免调用方手填出错。返回恢复后的 Scope。
    """
    root = root.resolve()
    base = bugx_root(root)
    if not base.is_dir() or base.is_symlink():
        raise LayoutError(f"缺少或不安全的 .bugx 目录: {base}")
    _validate_identifier(polling_id, "polling")
    _validate_identifier(lifecycle_id, "lifecycle")
    pollings = base / "pollings"
    polling_root = _id_path(pollings, polling_id, "polling")
    manifest = _read_json(polling_root / "manifest.json")
    polling_no = int(manifest["polling_no"])
    lifecycles = polling_root / "lifecycles"
    lifecycle_root = _id_path(lifecycles, lifecycle_id, "lifecycle")
    lc_manifest = _read_json(lifecycle_root / "manifest.json")
    lifecycle_no = int(lc_manifest["lifecycle_no"])
    return _write_current(root, polling_id, lifecycle_id, polling_no, lifecycle_no)


def _create_scope(root: Path, polling_id: str, lifecycle_id: str) -> Scope:
    active = scope(root, {"polling_id": polling_id, "lifecycle_id": lifecycle_id})
    for directory in (
        bugx_root(root), active.polling_root, active.lifecycle_root, active.state_root,
        active.locks_root, active.life_snapshot.parent, active.lifecycle_root / "rounds",
        active.lifecycle_root / "recovery", active.lifecycle_root / "workers",
        active.knowledge_root, active.logs_root, bugx_root(root) / "backups",
        bugx_root(root) / "artifacts", bugx_root(root) / "index",
    ):
        _assert_no_symlink_components(directory)
        directory.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(directory)
        os.chmod(directory, 0o700)
    return active


def compute_project_hash(path: Path | str) -> str:
    """Compute standard hash for a project path: <name>-<sha256[:8]>."""
    p = Path(path).resolve()
    h = hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:8]
    return f"{p.name}-{h}"


def count_project_sessions(root: Path) -> int:
    """Count sessions/pollings in a project."""
    count = 0
    try:
        pollings_dir = root / ".bugx" / "pollings"
        if pollings_dir.is_dir():
            count += len([p for p in pollings_dir.iterdir() if p.name.startswith("polling-")])
        sessions_dir = root / ".bugx" / "sessions"
        if sessions_dir.is_dir():
            count += len([s for s in sessions_dir.iterdir() if not s.name.startswith(".")])
    except Exception:
        pass
    return max(1, count) if (root / ".bugx").is_dir() else 0


def get_project_cloud_info(root: Path) -> dict[str, str]:
    """Retrieve bound cloud project_id / account_id if available."""
    info: dict[str, str] = {}
    try:
        cf = root / ".bugx" / "cloud-project.json"
        if cf.is_file():
            data = json.loads(cf.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("project_id"):
                    info["cloud_project_id"] = str(data["project_id"])
                if data.get("account_id"):
                    info["cloud_account_id"] = str(data["account_id"])
    except Exception:
        pass
    return info


def is_system_temp_dir(path: Path | str) -> bool:
    """Check if path is inside system temp directory (/tmp, /var/folders, etc.)."""
    try:
        resolved = Path(path).resolve()
        system_temps = [
            Path(tempfile.gettempdir()).resolve(),
            Path("/tmp").resolve(),
            Path("/private/tmp").resolve(),
            Path("/var/folders").resolve(),
            Path("/private/var/folders").resolve(),
            Path("/private/var/tmp").resolve(),
            Path("/var/tmp").resolve(),
        ]
        for st in system_temps:
            try:
                resolved.relative_to(st)
                return True
            except ValueError:
                pass
        return False
    except Exception:
        return False


def is_ephemeral_test_name(name: str) -> bool:
    """Check if project/directory name is an automated test ephemeral name (e.g. tmp9kt7dn15, auto-continue-xxx)."""
    n = name.lower()
    if n.startswith("auto-continue-") or n.startswith("pytest-") or n.startswith("tox-"):
        return True
    # tempfile 随机命名，如 tmp9kt7dn15, tmp_3oktjrb
    if n.startswith("tmp") and len(n) >= 7 and any(c.isdigit() for c in n):
        return True
    return False


def is_valid_project_dir(path: Path | str, *, name: str | None = None, allow_registered: bool = False) -> bool:
    """Strict check: directory is a valid BugX project, never home, root, or ephemeral test trash."""
    try:
        resolved = Path(path).resolve()
        home = Path.home().resolve()
        root = Path("/").resolve()
        # 1. 绝不能是家目录、根目录，且磁盘上必须真实存在并且是目录
        if resolved in (home, root) or not resolved.is_dir():
            return False

        # 2. 检查名称是否为自动化测试随机临时名
        if name and not is_ephemeral_test_name(name):
            pass  # 显式登记了合法的业务项目名称（如 sample-agent-project），放行
        elif is_ephemeral_test_name(resolved.name):
            return False
        elif name and is_ephemeral_test_name(name):
            return False

        # 3. 必须具备实际的 BugX 特征（有 .bugx 目录或 .bugx-project.json）
        return (resolved / ".bugx").is_dir() or (resolved / ".bugx-project.json").is_file()
    except Exception:
        return False


def sync_global_projects(workspace: Path | None = None) -> list[dict[str, Any]]:
    """Scan, reconcile, and persist all valid BugX projects into ~/.bugx/projects.json."""
    home = Path.home().resolve()
    pfile = home / ".bugx" / "projects.json"
    pfile.parent.mkdir(mode=0o700, exist_ok=True)

    # 1. Load existing registered projects, hidden_hashes, and purge invalid/temp/deleted entries (Self-Healing)
    existing_projects: list[dict[str, Any]] = []
    hidden_hashes: set[str] = set()
    if pfile.is_file():
        try:
            raw = json.loads(pfile.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("projects"), list):
                    existing_projects = raw["projects"]
                if isinstance(raw.get("hidden_hashes"), list):
                    hidden_hashes = {str(h) for h in raw["hidden_hashes"] if h}
            elif isinstance(raw, list):
                existing_projects = raw
        except Exception:
            existing_projects = []

    # Map of resolved Path -> existing dict (严格过滤掉所有临时、已删除、已隐藏或非法的垃圾条目)
    known_map: dict[Path, dict[str, Any]] = {}
    for p in existing_projects:
        if isinstance(p, dict) and p.get("working_dir"):
            wdir = Path(p["working_dir"]).resolve()
            h_cand = p.get("hash") or compute_project_hash(wdir)
            if h_cand in hidden_hashes or compute_project_hash(wdir) in hidden_hashes:
                continue
            p_name = p.get("name")
            if is_valid_project_dir(wdir, name=p_name):
                known_map[wdir] = p

    # 2. Add active workspace if provided or Path.cwd()
    # 活跃工作区必须自可见：hidden_hashes 只抑制**非活跃**项目的自动复活。
    # 否则在当前项目里 remove 过一次（或历史测试遗留）之后，服务把自己所在的
    # 工作区从 /projects 里永久隐藏，列表里连"我正在用的项目"都没有。
    active_ws = (workspace or Path.cwd()).resolve()
    if is_valid_project_dir(active_ws):
        if active_ws not in known_map:
            known_map[active_ws] = {}

    # 3. Discover sibling workspaces under the active workspace container
    # 核心硬防线：如果父目录是系统临时目录、家目录或根目录，绝对禁止扫描兄弟目录；已隐藏项目绝不复活！
    try:
        ws_parent = active_ws.parent.resolve()
        if (
            ws_parent not in (home, Path("/").resolve())
            and ws_parent.is_dir()
            and not is_system_temp_dir(ws_parent)
        ):
            for sub in sorted(ws_parent.iterdir()):
                if sub.is_dir():
                    sub_res = sub.resolve()
                    if (
                        sub_res not in known_map
                        and compute_project_hash(sub_res) not in hidden_hashes
                        and is_valid_project_dir(sub_res)
                    ):
                        known_map[sub_res] = {}
    except Exception:
        pass

    # 4. Check ~/.atomcode/recent_dirs.txt if exists
    recent_file = home / ".atomcode" / "recent_dirs.txt"
    if recent_file.is_file():
        try:
            for line in recent_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rp = Path(line).resolve()
                    if (
                        rp not in known_map
                        and compute_project_hash(rp) not in hidden_hashes
                        and is_valid_project_dir(rp)
                    ):
                        known_map[rp] = {}
        except Exception:
            pass

    # 5. Build consolidated project records
    now_ms = int(time.time() * 1000)
    consolidated: list[dict[str, Any]] = []

    # Put active_ws at top if valid, followed by other projects
    target_paths = list(known_map.keys())
    if active_ws in target_paths:
        target_paths.remove(active_ws)
        ordered_paths = [active_ws] + sorted(target_paths, key=lambda p: p.name.lower())
    else:
        ordered_paths = sorted(target_paths, key=lambda p: p.name.lower())

    for p in ordered_paths:
        orig = known_map.get(p) or {}
        h = compute_project_hash(p)
        added_at = orig.get("added_at") or now_ms
        last_seen = now_ms if p == active_ws else (orig.get("last_seen_at") or added_at)
        session_cnt = count_project_sessions(p)
        cloud_info = get_project_cloud_info(p)
        archived = bool(orig.get("archived", False))

        record: dict[str, Any] = {
            "hash": h,
            "name": orig.get("name") or p.name,
            "working_dir": str(p),
            "session_count": session_cnt,
            "added_at": added_at,
            "last_seen_at": last_seen,
            "archived": archived,
        }
        if orig.get("archived_at"):
            record["archived_at"] = orig["archived_at"]
        if cloud_info.get("cloud_project_id"):
            record["cloud_project_id"] = cloud_info["cloud_project_id"]
        if cloud_info.get("cloud_account_id"):
            record["cloud_account_id"] = cloud_info["cloud_account_id"]

        consolidated.append(record)

    # 6. Atomically persist to projects.json
    payload = {
        "version": 1,
        "projects": consolidated,
        "hidden_hashes": sorted(hidden_hashes),
    }
    tmp = pfile.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(pfile)

    return consolidated


def archive_global_project(proj_hash: str, *, archived: bool = True, workspace: Path | None = None) -> dict[str, Any] | None:
    """Archive or unarchive a project in ~/.bugx/projects.json (REQ-06)."""
    home = Path.home().resolve()
    pfile = home / ".bugx" / "projects.json"
    sync_global_projects(workspace=workspace)
    if not pfile.is_file():
        return None
    try:
        raw = json.loads(pfile.read_text(encoding="utf-8"))
        projects = raw.get("projects", []) if isinstance(raw, dict) else []
        hidden_hashes = raw.get("hidden_hashes", []) if isinstance(raw, dict) else []
        target: dict[str, Any] | None = None
        now_ms = int(time.time() * 1000)
        for item in projects:
            if isinstance(item, dict) and item.get("hash") == proj_hash:
                item["archived"] = bool(archived)
                if archived:
                    item["archived_at"] = now_ms
                else:
                    item.pop("archived_at", None)
                target = item
                break
        if target is not None:
            payload = {"version": 1, "projects": projects, "hidden_hashes": hidden_hashes}
            tmp = pfile.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(pfile)
        return target
    except Exception:
        return None


def remove_global_project(
    proj_hash: str | Path,
    *,
    mode: str = "remove",
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Remove a project from ~/.bugx/projects.json and suppress auto-resurrection (REQ-06).

    Modes:
    - 'remove': remove from list and record in hidden_hashes; keep .bugx on disk.
    - 'purge': remove from list, record in hidden_hashes, and safely delete ONLY <project_root>/.bugx
      (never touching user source files).
    """
    if mode not in ("remove", "purge"):
        raise ValueError(f"Unsupported delete mode: {mode}")
    if isinstance(proj_hash, Path) or (isinstance(proj_hash, str) and ("/" in proj_hash or "\\" in proj_hash)):
        proj_hash = compute_project_hash(Path(proj_hash).resolve())
    proj_hash = str(proj_hash)
    home = Path.home().resolve()
    pfile = home / ".bugx" / "projects.json"
    sync_global_projects(workspace=workspace)
    projects: list[dict[str, Any]] = []
    hidden_hashes: set[str] = set()
    if pfile.is_file():
        try:
            raw = json.loads(pfile.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                projects = list(raw.get("projects") or [])
                hidden_hashes = {str(h) for h in (raw.get("hidden_hashes") or []) if h}
        except Exception:
            projects = []

    removed_entry: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for item in projects:
        if isinstance(item, dict) and item.get("hash") == proj_hash:
            removed_entry = item
        else:
            remaining.append(item)

    hidden_hashes.add(proj_hash)
    purged_bugx = False
    if mode == "purge" and removed_entry and removed_entry.get("working_dir"):
        proj_root = Path(removed_entry["working_dir"]).resolve()
        dot_bugx = proj_root / ".bugx"
        # Red-team invariant: strictly assert we only ever delete the '.bugx' subdirectory inside proj_root
        if (
            proj_root not in (home, Path("/").resolve())
            and dot_bugx.name == ".bugx"
            and dot_bugx.parent.resolve() == proj_root
            and dot_bugx.exists()
            and not dot_bugx.is_symlink()
        ):
            shutil.rmtree(dot_bugx, ignore_errors=True)
            purged_bugx = not dot_bugx.exists()

    pfile.parent.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "version": 1,
        "projects": remaining,
        "hidden_hashes": sorted(hidden_hashes),
    }
    tmp = pfile.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(pfile)

    return {
        "ok": True,
        "deleted": True,
        "hash": proj_hash,
        "mode": mode,
        "purged_bugx": purged_bugx,
        "purged_bugx_dir": purged_bugx,
        "suppressed": proj_hash in hidden_hashes,
        "working_dir": removed_entry.get("working_dir") if removed_entry else None,
    }


def is_bugx_source_repo(root: Path) -> bool:
    """判断该目录是否是 BugX 自身的**源码检出**（而非被扫描的用户项目）。

    权威判定放在 ``core/``：``core/`` 不能反向依赖 ``interfaces/cli/``，而
    CLI 侧原先自带一份同名实现（``bugx_cli.common._is_bugx_source_repo``），
    两处独立推导正是本仓反复出现的缺陷族，故统一到这里、CLI 改为委托。

    特征取「构建脚本 + 打包清单」同时存在：普通用户项目不会同时具备
    ``scripts/bump_version.py`` 与 ``pyproject.toml``。
    """
    try:
        base = Path(root)
        return (base / "scripts" / "bump_version.py").is_file() \
            and (base / "pyproject.toml").is_file()
    except OSError:
        return False


def _register_global_project(root: Path) -> None:
    """Record initialized project into global ~/.bugx/projects.json (bypasses temp/test directories)."""
    try:
        # 如果是临时测试随机目录，严禁写入宿主全局注册表
        if is_ephemeral_test_name(root.name) or (is_system_temp_dir(root) and is_ephemeral_test_name(root.name)):
            return
        # BugX 自己的源码检出不是"用户用 BugX 扫过的项目"，不能进这张表。
        # ``~/.bugx/projects.json`` 描述的是安装后用户的扫描对象清单，
        # 开发仓库混进去会让 Web 控制台列出指向开发树的伪项目（实测已积累
        # 3 条源码/测试树条目）。这条判定**不依赖环境变量**，因此对
        # 手工运行、引擎子进程、宿主调用同样生效。
        if is_bugx_source_repo(root):
            return
        # 如果处于单元测试运行环境，亦严禁污染真实注册表
        if any(k in os.environ for k in ("PYTEST_CURRENT_TEST", "BUGX_TEST_MODE", "BUGX_SANDBOX_TEST")):
            return
        sync_global_projects(workspace=root)
    except Exception:
        pass


def initialize(root: Path, *, bugx_version: str = "unknown") -> Scope:
    root = root.resolve()
    if not root.is_dir():
        raise LayoutError(f"项目根目录不存在: {root}")
    base = bugx_root(root)
    if base.is_symlink():
        raise LayoutError(f"拒绝使用符号链接 .bugx: {base}")
    _assert_no_symlink_components(base)
    base.mkdir(mode=0o700, exist_ok=True)
    manifest_path = base / "manifest.json"
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise LayoutError(".bugx/manifest.json schema_version 不受支持")
        if _identity_mismatch(root, manifest):
            # ``project_id`` 是 ``sha256(根路径)``，即整份 ``.bugx`` 的身份
            # **绑死在绝对路径**上——它按设计就不可移植。因此清单对不上新根
            # 不是"脏状态"，而是**这份状态不属于这个根**。
            #
            # 绝不能"就地重建清单然后继续"：清单只是元数据，``.bugx`` 下的
            # 寿命账本、知识库、pollings 报告统统保留，于是源项目的洞察、
            # 错题集乃至机密会静默流进目标项目（实测：copytree 后的项目能
            # 直接读到源项目的 knowledge_seed 与 life 账本）。那是 fail-open
            # 数据污染，比拒绝服务严重得多。
            #
            # 正确处置是 fail-closed 并且**把出路写进错误**：唯一安全的迁移
            # 是整目录重建（旧状态整体作废），由用户显式执行。
            raise LayoutError(
                f"'.bugx' 目录不属于当前项目：{base}\n"
                f"  该目录绑定的根与当前根不一致（project_id 由根路径哈希得出，"
                f"因此 .bugx 不可复制/搬移）。\n"
                f"  若这是从别处复制来的布局，其中的寿命账本、知识库和报告都"
                f"属于原项目，继续使用会污染本项目。\n"
                f"  修复方式（二选一）：\n"
                f"    1) 备份后删除该目录并重新初始化：rm -rf {base} && bugx init\n"
                f"    2) 若确认要保留其中人工产出，先手工取出需要的文件，再执行 1)"
            )
        if (manifest.get("bugx_version") != bugx_version
                or manifest.get("layout_version") != LAYOUT_VERSION):
            # Version metadata is installation state, not user-authored
            # runtime state. Refresh it atomically so a project does not keep
            # claiming the version/layout from its first initialization forever.
            manifest["bugx_version"] = bugx_version
            manifest["layout_version"] = LAYOUT_VERSION
            manifest["updated_at"] = utc_now()
            _atomic_json(manifest_path, manifest)
    else:
        _atomic_json(manifest_path, {
            "schema_version": SCHEMA_VERSION,
            "layout_version": LAYOUT_VERSION,
            "project_id": _path_fingerprint(root),
            PROJECT_UID_KEY: _new_project_uid(root),
            "bugx_version": bugx_version,
            "created_at": utc_now(),
        })
    # PERF-06：manifest.json 被改写，显式失效 require() 的 Scope 缓存。
    clear_require_cache(root)
    # 登记必须在**布局建立之后**：``is_valid_project_dir`` 要求目录已含
    # ``.bugx``，而历史实现把登记放在 ``base.mkdir()`` 之前，于是全新项目
    # 首次 ``initialize`` 永远登记失败（要跑第二次才进表）——这正是
    # ``~/.bugx/projects.json`` 里混着开发树/测试副本、且真实项目缺失的原因。
    _register_global_project(root)
    current_path = base / "current.json"
    if current_path.exists():
        current = _read_json(current_path)
        active = scope(root, current)
        _create_scope(root, current["polling_id"], current["lifecycle_id"])
        return active
    polling_id = "polling-000001"
    lifecycle_id = "lifecycle-000001"
    _create_scope(root, polling_id, lifecycle_id)
    _write_current(root, polling_id, lifecycle_id, 1, 1)
    polling_root = bugx_root(root) / "pollings" / polling_id
    _atomic_json(polling_root / "manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "polling_id": polling_id,
        "polling_no": 1,
        "policy": None,
        "created_at": utc_now(),
    })
    _atomic_json(polling_root / "state.json", {
        "schema_version": SCHEMA_VERSION,
        "state": "created",
        "polling_id": polling_id,
        "lifecycle_ids": [lifecycle_id],
    })
    # 索引里冗余一份 project_uid：整目录 copytree 会同时带走清单与索引，两者
    # 一致 ⇒ 合法搬移；手工拼装/串用他人布局 ⇒ 不一致 ⇒ fail-closed。
    _atomic_json(bugx_root(root) / "index" / "index.json", {
        "schema_version": SCHEMA_VERSION,
        "last_polling_id": polling_id,
        PROJECT_UID_KEY: _new_project_uid(root),
    })
    lifecycle_root = _scope_root(root, polling_id, lifecycle_id)
    _atomic_json(lifecycle_root / "manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "polling_id": polling_id,
        "lifecycle_id": lifecycle_id,
        "lifecycle_no": 1,
        "created_at": utc_now(),
    })
    _atomic_json(lifecycle_root / "state.json", {
        "schema_version": SCHEMA_VERSION,
        "state": "created",
        "lifecycle_id": lifecycle_id,
    })
    return scope(root)


# PERF-06：``require()`` 每次都会重读并解析 manifest.json / current.json（还要
# 走一遍符号链接组件检查）。这里缓存「解析后的 Scope」，并用两个布局文件的
# mtime_ns+size 作为失效戳：引擎写入新生命周期会改写 current.json，戳变化即
# 自动重算，因此不会把运行期状态变更固定住。仅缓存成功结果，异常不缓存。
_REQUIRE_CACHE: dict[Path, tuple[tuple[Any, ...], Scope]] = {}


def _layout_stamp(base: Path) -> tuple[Any, ...]:
    """布局失效戳：manifest.json / current.json 的 (mtime_ns, size)。"""
    stamps: list[Any] = []
    for name in ("manifest.json", "current.json"):
        try:
            stat = (base / name).stat()
            stamps.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append(None)
    return tuple(stamps)


def clear_require_cache(root: Path | None = None) -> None:
    """清空 ``require()`` 的 Scope 缓存（不传 root 则全部清空）。"""
    if root is None:
        _REQUIRE_CACHE.clear()
    else:
        _REQUIRE_CACHE.pop(root.resolve(), None)


def require(root: Path) -> Scope:
    root = root.resolve()
    base = bugx_root(root)
    stamp = _layout_stamp(base)
    if all(part is not None for part in stamp):
        cached = _REQUIRE_CACHE.get(root)
        if cached is not None and cached[0] == stamp:
            active = cached[1]
            # 缓存命中也复核派生目录仍在：目录被删除时立即回退到完整校验，
            # 由完整路径抛出 LayoutError，而不是返回过期 Scope。
            if active.lifecycle_root.is_dir() and active.state_root.is_dir():
                return active
    if not base.is_dir() or base.is_symlink():
        raise LayoutError(f"缺少或不安全的 .bugx 目录: {base}；请先运行 bugx init")
    manifest = _read_json(base / "manifest.json")
    if _identity_mismatch(root, manifest):
        raise LayoutError(".bugx/manifest.json 与当前项目不匹配")
    current = read_current(root)
    active = scope(root, current)
    if not active.lifecycle_root.is_dir() or not active.state_root.is_dir():
        raise LayoutError("当前生命周期目录缺失；请运行 bugx doctor --repair 或恢复备份")
    _REQUIRE_CACHE[root] = (stamp, active)
    return active


def repair(root: Path) -> Scope:
    """Rebuild only derived layout pointers and directories.

    This function is intentionally conservative: it requires the immutable
    project manifest and an existing polling/lifecycle manifest.  It may
    recreate ``current.json`` and empty derived directories, but it never
    creates a life file, settlement, report, or knowledge database.  Missing
    authority therefore remains visible to ``doctor`` instead of becoming a
    fresh, silently reset project.
    """
    root = root.resolve()
    base = bugx_root(root)
    if not base.is_dir() or base.is_symlink():
        raise LayoutError(f"缺少或不安全的 .bugx 目录: {base}")
    manifest = _read_json(base / "manifest.json")
    if _identity_mismatch(root, manifest):
        raise LayoutError(".bugx/manifest.json 与当前项目不匹配")

    candidates: list[tuple[int, int, str, str]] = []
    pollings = base / "pollings"
    if pollings.is_dir() and not pollings.is_symlink():
        for polling_dir in pollings.iterdir():
            if not polling_dir.is_dir() or polling_dir.is_symlink():
                continue
            try:
                polling_manifest = _read_json(polling_dir / "manifest.json")
                polling_id = str(polling_manifest["polling_id"])
                polling_no = int(polling_manifest["polling_no"])
                _id_path(pollings, polling_id, "polling")
            except (KeyError, TypeError, ValueError, LayoutError):
                continue
            lifecycle_root = polling_dir / "lifecycles"
            if not lifecycle_root.is_dir() or lifecycle_root.is_symlink():
                continue
            for lifecycle_dir in lifecycle_root.iterdir():
                if not lifecycle_dir.is_dir() or lifecycle_dir.is_symlink():
                    continue
                try:
                    lifecycle_manifest = _read_json(lifecycle_dir / "manifest.json")
                    lifecycle_id = str(lifecycle_manifest["lifecycle_id"])
                    lifecycle_no = int(lifecycle_manifest["lifecycle_no"])
                    if lifecycle_manifest.get("polling_id") != polling_id:
                        continue
                    _id_path(lifecycle_root, lifecycle_id, "lifecycle")
                except (KeyError, TypeError, ValueError, LayoutError):
                    continue
                candidates.append((polling_no, lifecycle_no, polling_id, lifecycle_id))
    if not candidates:
        raise LayoutError("没有可验证的 polling/lifecycle manifest，拒绝重建为新项目")

    current_path = base / "current.json"
    selected: tuple[int, int, str, str] | None = None
    if current_path.is_file() and not current_path.is_symlink():
        try:
            current = _read_json(current_path)
            key = (int(current["polling_no"]), int(current["lifecycle_no"]),
                   str(current["polling_id"]), str(current["lifecycle_id"]))
            if key in candidates:
                selected = key
        except (KeyError, TypeError, ValueError, LayoutError):
            selected = None
    selected = selected or max(candidates)
    polling_no, lifecycle_no, polling_id, lifecycle_id = selected
    _create_scope(root, polling_id, lifecycle_id)
    _write_current(root, polling_id, lifecycle_id, polling_no, lifecycle_no)
    index = base / "index" / "index.json"
    _atomic_json(index, {
        "schema_version": SCHEMA_VERSION,
        "last_polling_id": polling_id,
        "last_lifecycle_id": lifecycle_id,
        "repaired_at": utc_now(),
    })
    return scope(root)


def activate_lifecycle(root: Path, lifecycle_no: int, lifecycle_id: str,
                       *, polling_id: str | None = None, polling_no: int | None = None) -> Scope:
    if isinstance(lifecycle_no, bool) or not isinstance(lifecycle_no, int) or lifecycle_no < 1:
        raise LayoutError("lifecycle_no 无效")
    _validate_identifier(lifecycle_id, "lifecycle")
    if polling_id is not None:
        _validate_identifier(polling_id, "polling")
    if polling_no is not None:
        if isinstance(polling_no, bool) or not isinstance(polling_no, int) or polling_no < 1:
            raise LayoutError("polling_no 无效")
    root = root.resolve()
    base = bugx_root(root)
    if not base.is_dir() or base.is_symlink():
        raise LayoutError(f"缺少或不安全的 .bugx 目录: {base}")
    with _project_lock(root):
        current = read_current(root)
        # 指针完整性：current 必须能定位到已存在的 polling/lifecycle
        _validate_polling_manifest(root, str(current["polling_id"]),
                                   expected_no=int(current["polling_no"]))
        _validate_lifecycle_manifest(root, str(current["polling_id"]),
                                     str(current["lifecycle_id"]),
                                     expected_no=int(current["lifecycle_no"]))
        # 解析目标 polling：同一 polling 下仅递增 lifecycle，不得篡改 polling_no
        if polling_id is None:
            resolved_polling_id = str(current["polling_id"])
            resolved_polling_no = int(current["polling_no"])
            if polling_no is not None and polling_no != resolved_polling_no:
                raise LayoutError(
                    f"polling_no 与当前 polling 不一致: current={resolved_polling_no} vs provided={polling_no}"
                )
        else:
            resolved_polling_id = polling_id
            _validate_identifier(resolved_polling_id, "polling")
            if polling_no is None:
                # 调用方指定了新 polling 但未提供编号时，以磁盘 manifest 为准
                try:
                    manifest = _read_json(base / "pollings" / resolved_polling_id / "manifest.json")
                    resolved_polling_no = int(manifest["polling_no"])
                except (LayoutError, KeyError, TypeError, ValueError, OSError) as exc:
                    raise LayoutError("指定 polling_id 时必须提供 polling_no") from exc
                if isinstance(resolved_polling_no, bool) or resolved_polling_no < 1:
                    raise LayoutError("polling_no 无效")
            else:
                resolved_polling_no = polling_no
        # 若切换到同一 polling，polling_no 必须与当前 manifest 一致；若切换到新 polling，则校验目标 polling
        _validate_polling_manifest(root, resolved_polling_id, expected_no=resolved_polling_no)
        # 若目标 lifecycle 已存在，校验其归属；若不存在则本次激活将创建它，仍需保证 polling 归属一致
        target_polling_root = _id_path(base / "pollings", resolved_polling_id, "polling")
        target_lifecycle_root = _id_path(target_polling_root / "lifecycles", lifecycle_id, "lifecycle")
        if target_lifecycle_root.is_dir() and not target_lifecycle_root.is_symlink():
            manifest_path = target_lifecycle_root / "manifest.json"
            if manifest_path.is_file() and not manifest_path.is_symlink():
                existing = _read_json(manifest_path)
                if existing.get("polling_id") != resolved_polling_id:
                    raise LayoutError(
                        f"lifecycle 归属不一致: {lifecycle_id} 属于 {existing.get('polling_id')} 而非 {resolved_polling_id}"
                    )
                if existing.get("lifecycle_id") != lifecycle_id:
                    raise LayoutError(f"lifecycle manifest 标识不一致: {lifecycle_id}")
                if int(existing.get("lifecycle_no", -1)) != lifecycle_no:
                    raise LayoutError(
                        f"lifecycle_no 指针不一致: manifest={existing.get('lifecycle_no')} vs expected={lifecycle_no}"
                    )
        active = _create_scope(root, resolved_polling_id, lifecycle_id)
        # 若 lifecycle manifest 尚不存在则原子创建，保证后续校验完整
        lifecycle_manifest_path = active.lifecycle_root / "manifest.json"
        if not lifecycle_manifest_path.is_file():
            _atomic_json(lifecycle_manifest_path, {
                "schema_version": SCHEMA_VERSION,
                "polling_id": resolved_polling_id,
                "lifecycle_id": lifecycle_id,
                "lifecycle_no": lifecycle_no,
                "created_at": utc_now(),
            })
            lifecycle_state_path = active.lifecycle_root / "state.json"
            if not lifecycle_state_path.is_file():
                _atomic_json(lifecycle_state_path, {
                    "schema_version": SCHEMA_VERSION,
                    "state": "created",
                    "lifecycle_id": lifecycle_id,
                })
        # 最终校验：激活前后指针必须完整
        _validate_polling_manifest(root, resolved_polling_id, expected_no=resolved_polling_no)
        _validate_lifecycle_manifest(root, resolved_polling_id, lifecycle_id, expected_no=lifecycle_no)
        _write_current(root, resolved_polling_id, lifecycle_id, resolved_polling_no, lifecycle_no)
        return active


def create_session(root: Path, *, session_no: int | None = None) -> Scope:
    """Create a fresh Project Session and its first Round.

    The on-disk ``pollings/lifecycles`` names are retained as storage details,
    but this operation is the single authority for the public
    ``Session → Round → Life`` boundary.  It never overwrites an existing
    session and updates ``current.json`` only after both manifests exist.
    """
    root = root.resolve()
    base = bugx_root(root)
    if not base.is_dir() or base.is_symlink():
        raise LayoutError(f"缺少或不安全的 .bugx 目录: {base}")
    _read_json(base / "manifest.json")
    if session_no is not None:
        if isinstance(session_no, bool) or not isinstance(session_no, int) or session_no < 1:
            raise LayoutError("Session 编号无效")
    with _project_lock(root):
        pollings = base / "pollings"
        _assert_no_symlink_components(pollings)
        pollings.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(pollings, 0o700)
        except OSError:
            pass
        _assert_no_symlink_components(pollings)
        numbers: list[int] = []
        for item in pollings.iterdir():
            if not item.is_dir() or item.is_symlink():
                continue
            try:
                value = _read_json(item / "manifest.json")
                numbers.append(int(value["polling_no"]))
            except (LayoutError, KeyError, TypeError, ValueError):
                continue
        next_no = session_no if session_no is not None else (max(numbers, default=0) + 1)
        if isinstance(next_no, bool) or not isinstance(next_no, int) or next_no < 1:
            raise LayoutError("Session 编号无效")
        polling_id = f"polling-{next_no:06d}"
        _validate_identifier(polling_id, "polling")
        # 显式 session_no 不得覆盖既有 Session：原子预留检查
        candidate_polling_root = _id_path(pollings, polling_id, "polling")
        if candidate_polling_root.exists():
            # 目录或文件已存在即视为冲突；显式和自动均拒绝复用
            raise LayoutError(f"Session 已存在，拒绝覆盖: {polling_id}")
        if next_no in numbers:
            raise LayoutError(f"Session 编号已存在: {polling_id}")
        lifecycle_id = "lifecycle-000001"
        active = _create_scope(root, polling_id, lifecycle_id)
        _atomic_json(active.polling_root / "manifest.json", {
            "schema_version": SCHEMA_VERSION,
            "polling_id": polling_id,
            "polling_no": next_no,
            "policy": None,
            "created_at": utc_now(),
        })
        _atomic_json(active.polling_root / "state.json", {
            "schema_version": SCHEMA_VERSION,
            "state": "created",
            "polling_id": polling_id,
            "lifecycle_ids": [lifecycle_id],
            "session_id": polling_id,
            "sessions_completed": 0,
            "sessions_aborted": 0,
            "sessions": [],
        })
        _atomic_json(active.lifecycle_root / "manifest.json", {
            "schema_version": SCHEMA_VERSION,
            "polling_id": polling_id,
            "lifecycle_id": lifecycle_id,
            "lifecycle_no": 1,
            "created_at": utc_now(),
        })
        _atomic_json(active.lifecycle_root / "state.json", {
            "schema_version": SCHEMA_VERSION,
            "state": "created",
            "lifecycle_id": lifecycle_id,
        })
        # 指针更新放在所有 manifest 落盘之后，保证 current 指向的必已完整
        _write_current(root, polling_id, lifecycle_id, next_no, 1)
        # 保留既有 project_uid：索引重建不得把项目身份抹掉（否则下一次
        # ``_identity_mismatch`` 会误判成「他人布局」，正常项目反被拒绝）。
        index_path = base / "index" / "index.json"
        index_uid = ""
        try:
            if index_path.is_file() and not index_path.is_symlink():
                index_uid = str(_read_json(index_path).get(PROJECT_UID_KEY) or "")
        except (OSError, ValueError, LayoutError):
            index_uid = ""
        _atomic_json(index_path, {
            "schema_version": SCHEMA_VERSION,
            "last_polling_id": polling_id,
            "last_lifecycle_id": lifecycle_id,
            PROJECT_UID_KEY: index_uid or _project_id(root),
            "updated_at": utc_now(),
        })
        return active


def update_session_state(root: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Atomically update the public Session state stored beside its Rounds.

    ``pollings/<id>/state.json`` is the Session-level index.  Round runtime
    files are intentionally disposable checkpoints and must not be used to
    count a whole Session across multiple Rounds.
    """
    if not isinstance(updates, dict):
        raise LayoutError("Session 状态更新必须是对象")
    root = root.resolve()
    base = bugx_root(root)
    if not base.is_dir() or base.is_symlink():
        raise LayoutError(f"缺少或不安全的 .bugx 目录: {base}")
    with _project_lock(root):
        active = require(root)
        current = read_current(root)
        # 指针完整性：current 指向的 polling/lifecycle 必须与 manifest 一致
        _validate_polling_manifest(root, str(current["polling_id"]),
                                   expected_no=int(current["polling_no"]))
        _validate_lifecycle_manifest(root, str(current["polling_id"]),
                                     str(current["lifecycle_id"]),
                                     expected_no=int(current["lifecycle_no"]))
        # 会话归属：active 的 polling 必须等于 current polling
        if active.polling_root.name != str(current["polling_id"]):
            raise LayoutError(
                f"Session 归属不一致: active {active.polling_root.name} vs current {current['polling_id']}"
            )
        if active.lifecycle_root.name != str(current["lifecycle_id"]):
            raise LayoutError(
                f"Lifecycle 指针不一致: active {active.lifecycle_root.name} vs current {current['lifecycle_id']}"
            )
        payload = _read_json(active.session_state)
        # 会话文件归属校验
        if payload.get("polling_id") and payload.get("polling_id") != active.polling_root.name:
            raise LayoutError(
                f"Session state 归属不一致: {payload.get('polling_id')} vs {active.polling_root.name}"
            )
        if payload.get("session_id") and payload.get("session_id") != active.polling_root.name:
            # session_id 与 polling_id 同义，允许一致性校验
            if str(payload.get("session_id")) != active.polling_root.name and str(payload.get("session_id")) != str(payload.get("polling_id") or ""):
                raise LayoutError(f"Session state session_id 不一致: {payload.get('session_id')}")
        payload.update(updates)
        # 禁止通过 updates 悄然篡改归属指针
        if payload.get("polling_id") and payload.get("polling_id") != active.polling_root.name:
            raise LayoutError(f"禁止篡改 Session polling_id: {payload.get('polling_id')}")
        if payload.get("session_id") and str(payload.get("session_id")) != active.polling_root.name:
            raise LayoutError(f"禁止篡改 Session session_id: {payload.get('session_id')}")
        payload["schema_version"] = SCHEMA_VERSION
        payload["session_id"] = str(payload.get("session_id") or active.polling_root.name)
        _atomic_json(active.session_state, payload)
        return payload


_FORBIDDEN_BROAD_PATTERNS = (
    re.compile(r"\bfind\s+(?:-[a-zA-Z0-9_-]+\s+)*(?:/Users|/home|/private|\$HOME|\$\{HOME\}|/|~)(?:\s|$|/)"),
    re.compile(r"\b(?:grep\s+.*-[a-zA-Z0-9]*r|grep\s+.*--recursive|rgrep\b).*(?:\s|^)(?:/Users|/home|/private|\$HOME|\$\{HOME\}|/|~)(?:\s|$|/)"),
    re.compile(r"\bls\s+(?:-[a-zA-Z0-9]+\s+)*(?:/Users(?:\*|/|\s|$)|/(?:\*|\s|$)|~(?:\*|/|\s|$))"),
)


def assert_safe_command(cmd: str, *, root: Path | None = None) -> None:
    """前置校验待执行的外部命令，坚决拦截违规全盘广域搜索反模式 (DOC-170)。

    根据《耗时防卫宪法》第一条：严禁在排查或任何自动化任务中执行 find ~、find /Users/、find /
    等跨越当前工作区边界的发散式全盘搜索。命中即刻抛出 LayoutError 阻断执行。
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return
    normalized = " ".join(cmd.strip().split())
    for pat in _FORBIDDEN_BROAD_PATTERNS:
        if pat.search(normalized):
            raise LayoutError(
                f"[FATAL_BLOCKED] 命令安全防卫拦截：禁止执行发散式全盘搜索 '{cmd}'！"
                "根据《耗时防卫宪法》，文件排查必须严格限定在当前被测项目作用域内，"
                "严禁发起无边界全盘扫描（单次将导致 120 秒超时黑洞）。"
            )
