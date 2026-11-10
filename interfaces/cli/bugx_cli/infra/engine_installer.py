#!/usr/bin/env python3
"""src/bugx_cli/infra/engine_installer.py - 共享核心引擎安装、原子替换与完整性校验 (DOC-070)。"""

from __future__ import annotations

import errno
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import sys
import sysconfig
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import bugx_cli
from bugx_cli import __version__
from .security import (
    CliError,
    _assert_no_symlink_components,
    _find_repo_root,
    _is_reparse_point,
    _i18n_exc,
    _sha256_file,
)


class MissingAgentAssetError(CliError):
    """Installed Agent package data is missing or incomplete.

    ``repairable`` is true only when the failure came from the default installed
    data location. Explicit asset overrides and source-development mode are
    operator choices and must never trigger a package installation.
    """

    def __init__(self, message: str, *, repairable: bool) -> None:
        super().__init__(message)
        self.repairable = repairable

ENGINE_INTEGRITY_NAME = "engine-integrity.json"
ENGINE_INTEGRITY_SCHEMA = 2
REQUIRED_I18N_ASSETS = ("locales/en-US.json", "locales/zh-CN.json")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\+[0-9A-Za-z.-]+)?$")
_CONTRACT_VERSION_RE = re.compile(
    r"^v?(?P<base>\d+\.\d+\.\d+)"
    r"(?:(?:[-.]?)(?:alpha|a|beta|b|rc)[-.]?\d+|\+[0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)


def _canonical_contract_version():
    """惰性取回权威 ``core/bugx_paths.contract_version``（DES-17 单一实现来源）。

    只缓存**成功**的解析结果：引导期引擎/资产都缺失时返回 ``None``，一旦就位，
    下一次调用仍能切换到权威实现（与 ``security._canonical_bugx_paths`` 同约定）。
    """
    global _CONTRACT_VERSION_FN
    if _CONTRACT_VERSION_FN is not None:
        return _CONTRACT_VERSION_FN
    try:
        from .security import _canonical_bugx_paths
        module = _canonical_bugx_paths()
        candidate = getattr(module, "contract_version", None)
    except Exception:  # noqa: BLE001 - 引导期导入失败即回落本地实现
        return None
    if callable(candidate):
        _CONTRACT_VERSION_FN = candidate
        return candidate
    return None


def _contract_version(value: str | None) -> str | None:
    """契约版本归一化：只看 ``x.y.z``，忽略预发布与构建/本地后缀。

    优先委托权威 ``core/bugx_paths.contract_version``；仅在无法加载权威实现时
    回落到本地同规则实现。**CLI 与引擎门禁必须共用同一条规则**，否则会重现
    "``bugx doctor`` 判一致、``bugx engine`` 门禁判不一致并拒绝启动"的双向打脸。
    """
    canonical = _canonical_contract_version()
    if canonical is not None:
        return canonical(value)
    if not isinstance(value, str):
        return None
    match = _CONTRACT_VERSION_RE.fullmatch(value.strip())
    return match.group("base") if match else None


def _parse_version_with_build_id(value: str | None) -> tuple[object, ...] | None:
    """Parse a contract version and optional numeric ``+build.N`` suffix."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(?P<base>\d+\.\d+\.\d+)"
        r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
        r"(?:\+(?P<meta>[0-9A-Za-z.-]+))?",
        value.strip(), re.IGNORECASE,
    )
    if not match:
        if _contract_version(value) is None:
            return None
        return None
    metadata = match.group("meta") or ""
    build = re.search(r"\bbuild[.-]?(\d+)\b", metadata, re.IGNORECASE)
    if metadata and build is None:
        return None
    prerelease = tuple(
        (part.isdigit(), int(part) if part.isdigit() else part.lower())
        for part in (match.group("pre") or "").replace("-", ".").split(".") if part
    )
    return (
        *(int(part) for part in match.group("base").split(".")),
        1 if not prerelease else 0,
        prerelease,
        int(build.group(1)) if build else 0,
    )


def _engine_build_id(root: Path) -> int | None:
    """Read the engine build ID from its trusted metadata marker."""
    marker = root / "metadata.txt"
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        match = re.search(r"^build_id:\s*(\d+)\s*$", marker.read_text(encoding="utf-8"), re.M)
    except (OSError, UnicodeError):
        return None
    return int(match.group(1)) if match else None


def _engine_receipt_build_id(path: Path) -> int | None:
    """Read a valid engine installation receipt Build ID, rejecting symlinks."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        _assert_no_symlink_components(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, CliError):
        return None
    value = payload.get("engine_build_id") if isinstance(payload, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _full_engine_version(version: str | None, root: Path) -> str | None:
    if not version:
        return None
    if "+" in version:
        return version
    build_id = _engine_build_id(root)
    if build_id is None:
        return version
    return f"{version}+build.{build_id}"


_CONTRACT_VERSION_FN: Any = None

RUNTIME_NAMES = {
    "bugx-life.json", "bugx-life.json.snapshot", "bugx-life.json.lock",
    "bugx-runtime.json", "bugx-runtime.lock", "current.json",
    "coverage-evidence.json", "coverage-evidence.lock", "finding-ledger.json",
    "finding-ledger.lock", "finding-lifecycle.json", "audit-supervisor.json",
    "audit-supervisor.json.bak", "audit-supervisor.json.lock",
    "lifecycle.lock", "audit-campaign.json",
    "repair-audit.log", "startup-todo.md", "acceptance-evidence-receipt.json",
}


def _is_runtime_artifact_name(name: str) -> bool:
    return (
        name in RUNTIME_NAMES
        or name.startswith("findings_round")
        or name.endswith(".bak")
        or name.endswith(".lock")
    )


RUNTIME_DIR_NAMES = frozenset({".bugx", "finding-commit"})

#: macOS 伴生元数据产物——**不得**进入静态引擎清单。
#:
#: ``.DS_Store`` 由 Finder 在浏览目录时写入，``._*`` 是 AppleDouble 伴生文件
#: （来自非 HFS+ 卷/U 盘），``__MACOSX/`` 是 zip 解包后残留的 macOS 资源目录。
#: 它们都被 ``.gitignore_global`` 忽略、**从不**被 git 跟踪，于是：清单生成器
#: 一旦把它们扫进来，签入的 ``core/engine-integrity.json`` 就会声称引擎包含
#: 有「任何安装端都不存在的文件」，``file_count`` 与 ``bundle_sha256`` 随之失真
#: ——在**不含该文件的检出**（CI 全新克隆、Linux 容器）上，一致性门禁直接变红
#: （复核 E3-D-1 实测 FAILED）。
#:
#: 关键点：``.gitignore`` **不约束打包清单**——清单生成器只认文件系统，
#: 所以排除必须发生在清单侧。
#:
#: 清单完整性由 ``tests.test_consistency::test_source_engine_integrity_is_current``
#: 守护，但那只在**当前机器**上成立；本清单本身必须由
#: ``tests/test_engine_installer.py`` 的 ``_is_macos_metadata_path`` 用例钉死
#: （修复轮复核 E4-BL-N11：此函数此前零测试覆盖，CI 测不出回归）。
MACOS_METADATA_NAMES = frozenset({
    ".DS_Store",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    ".TemporaryItems",
    ".apdisk",
    ".DocumentRevisions-V100",
    "__MACOSX",
})


def _is_macos_metadata_path(relative: str) -> bool:
    """判定是否 macOS 伴生元数据产物（目录成分 + 文件名双重判定）。

    目录成分判定是必需的：``__MACOSX/`` 目录里的文件名**未必**带 ``._`` 前缀，
    只按文件名判定会漏掉整个目录。
    """

    parts = Path(relative).parts
    if not parts:
        return False
    if any(part in MACOS_METADATA_NAMES for part in parts):
        return True
    name = parts[-1]
    return name in MACOS_METADATA_NAMES or name.startswith("._")


def _is_runtime_artifact_path(relative: str) -> bool:
    """运行态产物判定：**按目录成分 + 文件名双重排除**。

    DOC-004 Engine-State Separation 要求引擎目录只承载静态协议文件。
    旧实现只按 basename 判定（``RUNTIME_NAMES``），一旦有工具以 ``core/``
    为 cwd 运行，``probe_registry.run_preflight_probes`` 就会把
    ``.bugx/state/environment-profile.json`` 这类运行态缓存写进引擎目录，
    并被本清单**冻结进随包产物**（实测 196 → 197 个文件），随后
    ``_stage_engine`` 会把它复制进每个用户的 ``~/.bugx/core``。

    因此目录成分 ``.bugx`` 必须与文件名一样被排除：引擎根之下不存在
    任何合法的 ``.bugx`` 子目录（项目侧运行态位于引擎目录之外）。
    """
    parts = Path(relative).parts
    if any(part in RUNTIME_DIR_NAMES for part in parts):
        return True
    return bool(parts) and _is_runtime_artifact_name(parts[-1])


def project_core(root: Path) -> Path:
    """Return the removed legacy project-core path.

    This helper remains only for explicit migration/diagnostics.  Runtime
    resolution must never use the returned path as an executable asset root.
    """
    return root.resolve() / ".bugx" / "core"


def _dev_mode_enabled() -> bool:
    """单一开发总开关：BUGX_DEV_MODE=1（BUGX_DEV_ASSET_ROOT=1 为兼容别名）。

    双重保险的第二道闸：开关只在**进程本身跑在源码仓库**时才改变解析——
    安装副本的 CLI 即使误设该开关，repository 根探测也会落在
    ``<pyenv>/lib``（无 core/），一切解析退回生产路径。
    """
    return _source_checkout_root() is not None and (
        os.environ.get("BUGX_DEV_MODE") == "1"
        or os.environ.get("BUGX_DEV_ASSET_ROOT") == "1"
    )


def _source_checkout_root() -> Path | None:
    """Return the real source checkout root, not an installed package root.

    An npm tarball intentionally ships ``pyproject.toml`` and ``core/`` so the
    Python engine can run from the package.  Those files are not sufficient
    evidence that the process is running from a writable source checkout;
    treating the npm payload as one makes ``bugx init`` reject every fresh
    installation as an unsafe self-install.  The repository marker and the
    versioning script together are stable checkout-only evidence.
    """
    root = _find_repo_root(Path(__file__))
    if (root / ".git").exists() and (root / "scripts" / "bump_version.py").is_file():
        return root
    return None


def _repository_core() -> Path | None:
    root = _source_checkout_root()
    return root / "core" if root is not None else None


def global_engine_root() -> Path:
    """全局引擎根目录 ~/.bugx/core（方案三唯一运行时引擎）。

    静态协议与脚本集中于此；项目 ``.bugx`` 不再承载可执行引擎。
    可用 ``BUGX_GLOBAL_ENGINE_ROOT`` 覆盖（测试与隔离环境）；
    ``BUGX_DEV_MODE=1`` 且进程跑在源码仓库时，引擎根随仓库 core/ 走
    （改源码免安装验证）。显式覆盖优先于 dev 开关。
    """
    override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if _dev_mode_enabled():
        repo_core = _repository_core()
        if repo_core is not None and (repo_core / "bugx.md").is_file() \
                and (repo_core / "launch_bugx.py").is_file():
            return repo_core.resolve()
    return (Path.home() / ".bugx" / "core").resolve()


def refresh_i18n_after_engine_swap() -> None:
    """引擎 staging 换血后热重载 i18n（宿主镜像渲染正确性的前提）。

    安装进程早期（CLI 文案）已 ``import i18n``，模块级 locale 词典绑着旧
    staged 引擎；staging 换血后不重载的话，随后的宿主镜像/入口渲染会拿到
    旧 locale，产出过期镜像（实测：改 locale 后第一次 install 出旧镜像，
    第二次 install 才自愈）。优先 in-place reload——保持既有引用身份，全部
    惰性导入点（template_engine/host_adapters/agent）统一看到新数据；
    reload 失败（旧目录被原子替换）则弹出缓存，让后续惰性导入落到新引擎。
    """
    cached = sys.modules.get("i18n")
    if cached is None:
        return
    import importlib

    try:
        importlib.reload(cached)
    except Exception:
        sys.modules.pop("i18n", None)


def _legacy_project_core_present(root: Path) -> bool:
    """Return whether a project still exposes the removed executable surface."""
    legacy = project_core(root)
    try:
        return _is_reparse_point(legacy) or legacy.exists()
    except OSError:
        return True


def _delete_tree_without_following_reparse(path: Path) -> None:
    """Delete one legacy path without traversing symlinks or reparse points."""
    if _is_reparse_point(path) or path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for child in path.iterdir():
        _delete_tree_without_following_reparse(child)
    path.rmdir()


def _require_global_only_layout(root: Path) -> Path | None:
    """Enforce the global-only runtime contract by deleting legacy core.

    The scheme-3 contract is unconditional: an old project
    ``.bugx/core`` is removed on the first ``init`` or ``install``.  Mutable
    project data recognised by the migration helpers is detached first; the
    executable legacy tree itself is never archived or used as a fallback.
    """
    if not _legacy_project_core_present(root):
        return None
    legacy = project_core(root)
    _assert_no_symlink_components(legacy.parent)
    # Runtime artifacts and project-owned knowledge are detached before the
    # old directory is deleted, so the migration never loses project data.
    _detach_project_runtime_core_entries(root)
    if not _is_reparse_point(legacy) and (legacy / "bugx_paths.py").exists():
        manifest = root / ".bugx" / "manifest.json"
        if manifest.is_file() and not manifest.is_symlink():
            _detach_project_owned_core_links(root)
    try:
        _delete_tree_without_following_reparse(legacy)
    except OSError as exc:
        raise CliError(_i18n_exc(
            "common.legacy_core_delete_failed",
            f"无法直接删除旧项目引擎入口，迁移已停止: {legacy}: {exc}",
            legacy=legacy, exc=exc,
        )) from exc
    return legacy


def _distribution_located_path(located: object) -> Path | None:
    """把 ``Distribution.locate_file()`` 的返回值收敛为真实文件系统路径。

    zipimport（``PYTHONPATH=<wheel>``）下 ``distribution._path`` 是
    ``zipfile.Path``，``locate_file()`` 随之返回 zip 内部的伪路径对象：它没有
    ``__fspath__``，既不是 ``os.PathLike``，也无法被 ``pathlib.Path`` 包装
    （``Path()`` 会抛 ``TypeError: argument should be a str or an os.PathLike
    object where __fspath__ returns a str, not 'Path'``）。

    轮子内部的资产本就不在磁盘上（引擎按路径 exec ``.py``、按路径读模板，都需要
    真实文件），无法作为资源目录使用。因此这里统一返回 ``None`` 交由调用方跳过，
    把「从 zip 内加载」降级为「未找到该资产」，而不是让 CLI 以内部异常崩溃。
    """
    if isinstance(located, str):
        return Path(located)
    if isinstance(located, os.PathLike):
        try:
            text = os.fspath(located)
        except TypeError:
            # 畸形的 __fspath__（返回非 str）同样视为不可用。
            return None
        if isinstance(text, str):
            return Path(text)
    return None


def _asset_candidates() -> list[Path]:
    repository_root = _source_checkout_root()
    candidates: list[Path] = [
        Path(sysconfig.get_path("data")) / "share" / "bugx-agent" / "core",
    ]

    def add_secure_candidate(candidate: Path, *, index: int = 0) -> bool:
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.stat().st_mode
            safe_files = all(
                (resolved / name).is_file() and not (resolved / name).is_symlink()
                for name in ("bugx.md", "launch_bugx.py")
            )
            if resolved.is_dir() and not candidate.is_symlink() \
                    and not mode & 0o022 and safe_files:
                candidates.insert(index, resolved)
                return True
        except OSError:
            pass
        return False

    override = os.environ.get("BUGX_ASSET_ROOT")
    override_added = bool(override and add_secure_candidate(Path(override)))
    if _dev_mode_enabled() and repository_root is not None:
        add_secure_candidate(repository_root / "core", index=1 if override_added else 0)
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        for item in distribution.files or []:
            text = item.as_posix()
            if text.endswith("share/bugx-agent/core/bugx.md"):
                located = _distribution_located_path(distribution.locate_file(item))
                if (located is not None
                        and not ((located.parent.name == "core")
                                 and (located.parent.parent / "pyproject.toml").is_file()
                                 and (located.parent.parent / "scripts" / "bump_version.py").is_file())):
                    candidates.append(located.parent)
    except importlib.metadata.PackageNotFoundError:
        pass
    # Checkout assets are deliberately absent by default. The explicit
    # BUGX_DEV_ASSET_ROOT=1 branch above is the only source-tree opt-in.
    return list(dict.fromkeys(path.resolve() for path in candidates))


def asset_root() -> Path:
    candidates = _asset_candidates()
    for path in candidates:
        if (path / "bugx.md").is_file() and (path / "launch_bugx.py").is_file():
            return path
    # Distinguish a present-but-incomplete install payload from a missing one,
    # preserving the exact directory in the repair error. Neither case falls
    # back to the repository checkout.
    incomplete = [
        path for path in candidates
        if path.is_dir() and not all(
            (path / name).is_file() for name in ("bugx.md", "launch_bugx.py")
        )
    ]
    repairable = (
        os.environ.get("BUGX_ASSET_ROOT") is None
        and not _dev_mode_enabled()
    )
    if incomplete:
        raise MissingAgentAssetError(_i18n_exc(
            "common.agent_asset_incomplete",
            "BugX Agent 资源目录不完整（缺少 bugx.md 或 launch_bugx.py）："
            + "、".join(str(path) for path in incomplete)
            + "；请重新安装 bugx-cli，或设置 BUGX_ASSET_ROOT 指向完整资源目录。"
            + "源码仓库开发需显式设置 BUGX_DEV_ASSET_ROOT=1。",
        ), repairable=repairable)
    raise MissingAgentAssetError(_i18n_exc(
        "common.agent_asset_missing",
        "找不到 BugX Agent 资源；请重新安装 bugx-cli，或设置 BUGX_ASSET_ROOT "
        "指向完整资源目录。仅在源码仓库开发时，显式设置 BUGX_DEV_ASSET_ROOT=1。",
    ), repairable=repairable)


# DES-16：项目核心剥离名单的唯一来源是 ``core/bugx_paths.PROJECT_OWNED_CORE_NAMES``。
# 本模块（CLI 包）不能在模块级 ``import bugx_paths``：它不在 sys.path 上，且
# ``bugx_cli.common`` → ``bugx_cli.infra`` → 本模块 构成模块级循环导入。因此
# 按路径 exec 全局引擎/资产目录中的同名脚本，取其常量。
_PROJECT_OWNED_CORE_NAMES_FALLBACK = frozenset({
    "bug-log.md", "mistake-book.md", "lesson-ledger.json",
    "lesson-ledger.json.lock", "prep-record.md", "module-coverage.md",
})

#: ``core/bugx_paths.ENGINE_ASSET_SEED_NAMES`` 的兜底副本（唯一事实源仍是引擎核心）。
_ENGINE_ASSET_SEED_NAMES_FALLBACK = frozenset({
    "lesson-ledger.json", "bug-log.md", "mistake-book.md",
})


def _core_layout_candidates() -> list[Path]:
    candidates = [global_engine_root() / "bugx_paths.py"]
    try:
        candidates.append(asset_root() / "bugx_paths.py")
    except Exception:  # noqa: BLE001 - 资产目录缺失时只保留全局引擎候选
        pass
    return candidates


def _exec_core_layout_module():
    """按路径 exec 引擎核心 ``bugx_paths.py``，返回模块或 ``None``。

    任何失败（路径缺失、exec 抛错）都返回 ``None``，由调用方回落到兜底集合，
    避免把「引擎核心不可读」升级为 CLI 启动期的硬失败。
    """
    for path in _core_layout_candidates():
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("bugx_core_layout_constants", path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            # 必须在 exec 前登记到 sys.modules：bugx_paths.py 用 @dataclass，
            # 数据类装饰器需要能从 sys.modules 取回自身模块命名空间。
            sys.modules[spec.name] = module
            previous = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                spec.loader.exec_module(module)
            finally:
                sys.dont_write_bytecode = previous
                sys.modules.pop(spec.name, None)
            return module
        except Exception:  # noqa: BLE001 - 核心脚本不可用时保持兜底而非中断导入
            continue
    return None


def _load_core_project_owned_names() -> frozenset[str] | None:
    """读取引擎核心的 ``PROJECT_OWNED_CORE_NAMES``（失败返回 ``None``）。"""
    module = _exec_core_layout_module()
    names = getattr(module, "PROJECT_OWNED_CORE_NAMES", None) if module else None
    return frozenset(names) if names else None


def _load_core_engine_seed_names() -> frozenset[str] | None:
    """读取引擎核心的 ``ENGINE_ASSET_SEED_NAMES``（失败返回 ``None``）。"""
    module = _exec_core_layout_module()
    names = getattr(module, "ENGINE_ASSET_SEED_NAMES", None) if module else None
    return frozenset(names) if names else None


PROJECT_OWNED_CORE_NAMES: frozenset[str] = (
    _load_core_project_owned_names() or _PROJECT_OWNED_CORE_NAMES_FALLBACK
)

#: 引擎载荷必须安装、但不得按项目数据剥离的种子模板（D1 根因修正）。
ENGINE_ASSET_SEED_NAMES: frozenset[str] = (
    _load_core_engine_seed_names() or _ENGINE_ASSET_SEED_NAMES_FALLBACK
)

#: 静态引擎文件清单的剥离名单：项目数据名 **减去** 随包分发的种子模板。
#: 只在这里生效——项目核心目录（``bugx_paths._project_core_files``）仍按
#: ``PROJECT_OWNED_CORE_NAMES`` 全量剥离，两种树语义不得混用。
_ENGINE_PAYLOAD_EXCLUDED_NAMES: frozenset[str] = (
    PROJECT_OWNED_CORE_NAMES - ENGINE_ASSET_SEED_NAMES
)



def _engine_file_manifest(root: Path) -> dict[str, dict[str, int | str]]:
    """Return the hash manifest for static engine files only."""
    if root.is_symlink() or not root.is_dir():
        raise CliError(_i18n_exc("common.global_engine_insecure", f"全局引擎不是安全目录: {root}", root=root))
    result: dict[str, dict[str, int | str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CliError(_i18n_exc("common.engine_dir_contains_symlink", f"引擎目录不得包含符号链接: {path}", path=path))
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if path.name == ENGINE_INTEGRITY_NAME or _is_runtime_artifact_path(relative) \
                or _is_macos_metadata_path(relative) \
                or path.name in _ENGINE_PAYLOAD_EXCLUDED_NAMES:
            continue
        result[relative] = {
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
    return result


def _engine_manifest_payload(version: str,
                             files: dict[str, dict[str, int | str]]) -> dict[str, object]:
    canonical = {
        "schema_version": ENGINE_INTEGRITY_SCHEMA,
        "bugx_version": version,
        "file_count": len(files),
        "files": files,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return {
        **canonical,
        "bundle_sha256": hashlib.sha256(encoded).hexdigest(),
        "launch_bugx_sha256": str(files.get("launch_bugx.py", {}).get("sha256", "")),
    }


def _write_engine_integrity(root: Path, payload: dict[str, object]) -> None:
    integrity = root / ENGINE_INTEGRITY_NAME
    _assert_no_symlink_components(integrity)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(root), prefix=".engine-integrity.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, integrity)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _fsync_engine_tree(root: Path) -> None:
    """Make a staged engine durable before publishing its directory."""
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and not path.is_symlink():
            try:
                # Windows rejects fsync on a read-only descriptor (EBADF).
                # Opening append-only does not alter the file and gives the
                # CRT a writable descriptor that can be flushed.
                mode = "ab" if os.name == "nt" else "rb"
                with path.open(mode) as handle:
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise CliError(_i18n_exc("common.engine_file_persist_failed", f"无法持久化引擎文件: {path}: {exc}", path=path, exc=exc)) from exc
        elif path.is_dir() and not path.is_symlink() and os.name != "nt":
            # Some POSIX filesystems (tmpfs, network mounts) reject directory
            # fsync with EINVAL.  File data above is already durable, so a
            # directory-sync refusal must not abort the whole install.
            try:
                descriptor = os.open(path, os.O_RDONLY)
            except OSError as exc:
                print(f"[bugx] 警告：目录持久化已跳过（不影响安装）: {path}: {exc}", file=sys.stderr)
                continue
            try:
                os.fsync(descriptor)
            except OSError as exc:
                print(f"[bugx] 警告：目录持久化已跳过（不影响安装）: {path}: {exc}", file=sys.stderr)
            finally:
                os.close(descriptor)


_ENGINE_LOCK_ATTEMPTS = 5
_ENGINE_LOCK_RETRY_SECONDS = 1.0


def _acquire_lock_with_retry(acquire, *, subject: str) -> None:
    """Try to take an exclusive lock without ever blocking indefinitely.

    msvcrt.LK_LOCK and fcntl.LOCK_EX are both blocking; a stuck holder would
    hang the CLI forever.  Both backends therefore use their non-blocking
    variant and poll a few times before giving up with a clear error.
    """
    last_error: OSError | None = None
    for attempt in range(_ENGINE_LOCK_ATTEMPTS):
        try:
            acquire()
            return
        except OSError as exc:
            last_error = exc
            if attempt < _ENGINE_LOCK_ATTEMPTS - 1:
                time.sleep(_ENGINE_LOCK_RETRY_SECONDS)
    waited = _ENGINE_LOCK_ATTEMPTS * _ENGINE_LOCK_RETRY_SECONDS
    raise CliError(
        f"另一{subject}正在进行或引擎被占用，已等待约 {waited:.0f} 秒仍无法获得安装锁，请稍后重试: {last_error}"
    )


def _acquire_windows_install_lock(msvcrt: Any, descriptor: int,
                                  *, timeout: float = 30.0) -> None:
    """Acquire the Windows lock without allowing a second CLI to hang forever."""
    deadline = time.monotonic() + timeout
    while True:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise CliError("Windows 引擎安装锁等待超时，请确认没有并发运行 bugx install") from exc
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


@contextmanager
def _engine_install_lock(destination: Path):
    """Serialize updates to the shared engine across concurrent CLI processes."""
    lock_path = destination.parent / f".{destination.name}.install.lock"
    _assert_no_symlink_components(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    backend = None
    try:
        if os.name == "nt":
            try:
                import msvcrt
            except ImportError as exc:  # pragma: no cover - Windows runtime
                raise CliError("Windows 当前 Python 无法提供安装锁，已拒绝并发安装") from exc
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            _acquire_windows_install_lock(msvcrt, descriptor)
            backend = msvcrt
        else:
            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - unsupported platform
                raise CliError("当前 Python 无法提供引擎安装锁，已拒绝并发安装") from exc
            _acquire_lock_with_retry(
                lambda: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB),
                subject="引擎安装",
            )
            backend = fcntl
        yield
    finally:
        if backend is not None:
            try:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    backend.locking(descriptor, backend.LK_UNLCK, 1)
                else:
                    backend.flock(descriptor, backend.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _remove_engine_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except OSError:
            try:
                path.chmod(0o700)
                path.unlink()
            except OSError:
                pass
    elif path.is_dir():
        for root, dirs, files in os.walk(path):
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o600)
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)


def _engine_receipt_build_id(receipt: Path) -> int | None:
    if receipt.is_symlink() or not receipt.is_file():
        return None
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    build_id = payload.get("engine_build_id") if isinstance(payload, dict) else None
    return build_id if type(build_id) is int else None


def _recover_engine_swap(destination: Path) -> None:
    """Recover a process interrupted between the two directory renames."""
    parent = destination.parent
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    staging = sorted(
        path for path in parent.glob(f".{destination.name}.staging-*")
        if not path.name.endswith(".engine-receipt")
    )
    receipt_staging = sorted(
        parent.glob(f".{destination.name}.staging-*.engine-receipt")
    )
    receipt_path = parent / "engine-receipt.json"

    if receipt_staging and destination.exists() and not destination.is_symlink():
        staged_receipt = receipt_staging[-1]
        staged_build_id = _engine_receipt_build_id(staged_receipt)
        current_build_id = _engine_build_id(destination)
        if staged_build_id is not None and staged_build_id == current_build_id:
            if receipt_path.is_dir() and not receipt_path.is_symlink():
                raise CliError(f"引擎收据路径是目录，拒绝继续安装: {receipt_path}")
            _assert_no_symlink_components(receipt_path)
            os.replace(staged_receipt, receipt_path)
            for path in backups + staging + receipt_staging[:-1]:
                if path.exists() or path.is_symlink():
                    _remove_engine_path(path)
            return
        if backups:
            _remove_engine_path(destination)
            os.replace(backups[-1], destination)
            for path in backups[:-1] + staging + receipt_staging:
                if path.exists() or path.is_symlink():
                    _remove_engine_path(path)
            return
        for path in staging:
            if path.exists() or path.is_symlink():
                _remove_engine_path(path)
        for path in receipt_staging:
            _remove_engine_receipt_path(path)
        return

    if destination.exists() or destination.is_symlink():
        for path in backups + staging:
            _remove_engine_path(path)
        for path in receipt_staging:
            _remove_engine_receipt_path(path)
        return
    if backups:
        os.replace(backups[-1], destination)
        for path in backups[:-1] + staging:
            _remove_engine_path(path)
        for path in receipt_staging:
            _remove_engine_receipt_path(path)
        return
    for path in staging:
        _remove_engine_path(path)
    for path in receipt_staging:
        _remove_engine_receipt_path(path)


def _engine_receipt_stage_path(stage: Path) -> Path:
    return stage.with_name(f"{stage.name}.engine-receipt")


def _is_bugx_engine_receipt(receipt: Path) -> bool:
    """Return whether a path holds a BugX-authored engine receipt."""
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return (type(payload.get("engine_build_id")) is int
            and isinstance(payload.get("source_asset_root"), str))


def _remove_engine_receipt_path(receipt: Path, *, foreign_is_fatal: bool = True) -> None:
    """Delete a BugX-authored receipt; a foreign file is never destroyed.

    ``engine-receipt.json`` is BugX's own sidecar and is deliberately absent
    from the wheel ``RECORD``, so reclaiming a stale copy from a legacy engine
    root (including the Python package data directory) is legitimate recovery.
    Only files that actually carry the BugX receipt schema are removed: an
    unrelated user file that happens to share the name is left untouched and,
    by default, aborts the install so the collision is reported instead of
    being silently destroyed.
    """
    if not os.path.lexists(receipt):
        return
    if receipt.is_dir() and not receipt.is_symlink():
        raise CliError(f"引擎收据路径是目录，拒绝继续安装: {receipt}")
    if not _is_bugx_engine_receipt(receipt):
        if foreign_is_fatal:
            raise CliError(f"引擎收据路径存在非 BugX 文件，拒绝删除: {receipt}")
        return
    try:
        receipt.unlink()
    except OSError as exc:
        raise CliError(f"无法删除旧引擎收据，拒绝继续安装: {receipt}: {exc}") from exc


def _remove_root_engine_receipt(root: Path, *, foreign_is_fatal: bool = True) -> None:
    _remove_engine_receipt_path(
        root / "engine-receipt.json", foreign_is_fatal=foreign_is_fatal,
    )


def _write_engine_receipt(source: Path, receipt_path: Path) -> None:
    receipt = {
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "installed_by_cli_version": _full_engine_version(__version__, source),
        "source_asset_root": str(source.resolve()),
        "engine_build_id": _engine_build_id(source),
    }
    _assert_no_symlink_components(receipt_path)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    with receipt_path.open("ab" if os.name == "nt" else "rb") as handle:
        os.fsync(handle.fileno())


def _stage_engine(source: Path, destination: Path, stage: Path) -> tuple[int, int]:
    """Build and validate a complete engine tree before publication."""
    source_files = _engine_file_manifest(source)
    # 种子模板按**文件名**匹配，不按仓库相对路径：它们随 ledger 包归档
    # （``core/ledger/bug-log.md`` / ``core/ledger/mistake-book.md``），而
    # ``ENGINE_ASSET_SEED_NAMES`` 声明的是文件名。此前直接拿文件名去查相对路径
    # 键，迁移后必然判定为缺失，安装被一个恒假的门禁整体拒绝。
    source_basenames = {relative.rsplit("/", 1)[-1] for relative in source_files}
    missing_seed_assets = sorted(
        name for name in ENGINE_ASSET_SEED_NAMES if name not in source_basenames
    )
    if missing_seed_assets:
        # D1：种子模板是「新项目 pre 的必需输入」而不是可选资产。缺一个就必须在
        # 安装期 fail-closed——否则装出来的引擎对所有新项目都是「启动即失败」，
        # 且 install 退出码仍为 0（历史缺陷：全局引擎缺 lesson-ledger.json）。
        raise CliError(_i18n_exc(
            "common.engine_seed_assets_missing",
            "引擎载荷缺少内置知识种子模板，拒绝安装："
            + ", ".join(missing_seed_assets)
            + "；请检查安装资源（core 目录）或重新打包。",
            assets=", ".join(missing_seed_assets),
        ))
    missing_locale_assets = [
        relative for relative in REQUIRED_I18N_ASSETS
        if relative not in source_files
    ]
    if missing_locale_assets:
        raise CliError(_i18n_exc("common.locale_assets_incomplete", "核心语言资源不完整，拒绝安装：" + ", ".join(missing_locale_assets), assets=", ".join(missing_locale_assets)))
    for relative in REQUIRED_I18N_ASSETS:
        try:
            catalog = json.loads((source / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(_i18n_exc("common.locale_resource_unreadable", f"核心语言资源无法读取，拒绝安装: {source / relative}: {exc}", path=source / relative, exc=exc)) from exc
        if not isinstance(catalog, dict):
            raise CliError(_i18n_exc("common.locale_resource_invalid_format", f"核心语言资源格式无效，拒绝安装: {source / relative}", path=source / relative))
    stage.mkdir(parents=True, exist_ok=False)
    copied = 0
    for relative in source_files:
        source_path = source / relative
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
        copied += 1

    actual = _engine_file_manifest(stage)
    if actual != source_files:
        raise CliError(_i18n_exc("common.staged_engine_hash_failed", "暂存引擎哈希校验失败，拒绝发布"))
    _write_engine_integrity(stage, _engine_manifest_payload(__version__, actual))
    _fsync_engine_tree(stage)
    return copied, 0


def _remove_engine_orphans(destination: Path, expected: dict[str, dict[str, int | str]]) -> int:
    """Delete engine files that no manifest owns (legacy-install leftovers).

    Legacy installers (0.9 flat copies, 0.10.1 staging) and failed swaps can
    leave files behind that the current engine manifest does not declare
    (e.g. a top-level ``memory-macos.json`` from the flat layout).  They are
    stale engine garbage and are removed outright.  User-owned data
    (``PROJECT_OWNED_CORE_NAMES``) and mutable runtime artifacts keep their
    existing protection; ``__pycache__`` caches are purged as well.
    """
    expected_names = set(expected)
    expected_dirs = {str(PurePosixPath(name).parent) for name in expected_names}
    removed = 0
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        if path.is_dir():
            if path.name == "__pycache__":
                shutil.rmtree(path, ignore_errors=True)
                continue
            relative = path.relative_to(destination).as_posix()
            if relative in expected_dirs or any(
                expected_dir.startswith(relative + "/") for expected_dir in expected_dirs
            ):
                continue
            try:
                # Directory placeholders from abandoned layouts vanish once
                # empty; the call only succeeds when nothing is left inside.
                path.rmdir()
            except OSError:
                pass
            continue
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(destination).as_posix()
        if relative in expected_names:
            continue
        if (
            path.name == ENGINE_INTEGRITY_NAME
            or _is_runtime_artifact_path(relative)
            or path.name in PROJECT_OWNED_CORE_NAMES
        ):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _legacy_integrity_manifest(legacy: Path) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Read the previous project snapshot manifest used for safe refreshes.

    A copied project core can be refreshed without ``--force`` only when the
    old integrity manifest proves that the target still is the package-owned
    file from that snapshot.  Missing or edited manifests deliberately disable
    replacement so user files are never guessed to be BugX-owned.
    """
    path = legacy / ENGINE_INTEGRITY_NAME
    if path.is_symlink() or not path.is_file():
        return None, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, {}
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict) or not files:
        return None, {}
    normalized: dict[str, dict[str, Any]] = {}
    for relative, record in files.items():
        if not isinstance(relative, str) or not isinstance(record, dict):
            return None, {}
        digest = record.get("sha256")
        size = record.get("size")
        if not isinstance(digest, str) or not isinstance(size, int):
            return None, {}
        normalized[relative] = {"sha256": digest, "size": size}
    return payload, normalized


def _matches_integrity_record(path: Path, record: dict[str, Any] | None) -> bool:
    """Return whether an existing regular file is unchanged from a snapshot."""
    if record is None or path.is_symlink() or not path.is_file():
        return False
    try:
        return (
            record.get("sha256") == _sha256_file(path)
            and record.get("size") == path.stat().st_size
        )
    except OSError:
        return False


def _installed_engine_version(destination: Path) -> str | None:
    """读取已安装引擎声明的版本；缺失、软链接或不可读时返回 None。"""
    integrity = destination / ENGINE_INTEGRITY_NAME
    if integrity.is_file() and not integrity.is_symlink():
        try:
            payload = json.loads(integrity.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        version = payload.get("bugx_version") if isinstance(payload, dict) else None
        if isinstance(version, str) and version.strip():
            return _full_engine_version(version.strip(), destination)
    marker = destination / "metadata.txt"
    if marker.is_file() and not marker.is_symlink():
        try:
            content = marker.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        match = re.search(r'bugx_version:\s*"([^"]+)"', content)
        if match:
            return _full_engine_version(match.group(1), destination)
    return None


def _is_newer_version(candidate: str | None, reference: str) -> bool:
    """candidate 严格高于 reference 时返回 True。

    任一版本号非规范时**保守返回 False**（走原有的替换路径），避免因为解析不出
    版本而把正常的引擎刷新挡住。
    """
    if not candidate:
        return False
    left = _parse_version_with_build_id(candidate)
    right = _parse_version_with_build_id(reference)
    if left is None or right is None:
        return False
    return left > right


def _install_engine(
    source: Path, force: bool = False, *, destination: Path | None = None,
) -> tuple[int, int]:
    """Atomically install a complete, hash-verified shared engine bundle.

    ``destination`` is an internal seam for callers that must target one
    specific engine root (isolated harnesses and regression tests).  Production
    installs pass ``None``, so the destination resolves from
    ``BUGX_GLOBAL_ENGINE_ROOT`` or ``~/.bugx/core``.  Both routes share the
    identical validation below, therefore the seam cannot bypass the payload or
    protected-root guards.
    """
    # dev 模式短路：引擎根就是源码树本身，安装是恒等操作。
    #
    # 不加这个短路会形成死锁——``global_engine_root()`` 在 dev 模式下返回源码树
    # （见该函数），于是本函数的 destination 变成源码树，随后被下面两道守卫
    # 依次拒绝：
    #   1. 「拒绝从项目源码树安装引擎」（source == repository_core）
    #   2. 「拒绝把引擎布署到源码仓库 core/」（destination == repository_core）
    # 结果是 dev 模式下 ``bugx init`` 对任何新项目都必然失败（实测 bugx-dev
    # init 真实项目：错误退出码 2，项目未初始化）。
    #
    # 判定用「source 与 destination 指向同一棵树」而不是只看开关：开关只是
    # 在进程跑于源码仓库时才生效，用实际路径判等能同时覆盖
    # BUGX_GLOBAL_ENGINE_ROOT 显式指向源码树的情形，且不会放过任何真正的
    # 跨树写入（那才是守卫要防的）。
    #
    # 注意：这里**不看 force**。``force`` 在调用方的语义是「强制重装宿主入口」
    # （``autonomous`` 的 _install_host 会带 --force），不是「允许把引擎覆写进
    # 源码树」。用 force 做开关会让 automatic 无人值守恰好绕过短路——
    # 实测：加了只看 not force 的版本后，``bugx-dev init`` 通了，但
    # ``bugx-dev autonomous`` 仍然 blocked。判等本身就足够安全：只有
    # 「来源与目的地是同一棵源码树」这一种情况才短路，跨树写入一律照旧拒绝。
    _src = source.resolve()
    _dst = (destination or global_engine_root()).resolve()
    repository_core = _repository_core()
    if repository_core is not None and _src == _dst == repository_core.resolve():
        return (0, 0)
    if repository_core is not None and not force \
            and source.resolve() == repository_core.resolve():
        print(
            f"[bugx] 警告：拒绝从项目源码树安装引擎：{source.resolve()}；"
            "仅显式 force=True 才允许该开发操作。",
            file=sys.stderr,
        )
        raise CliError("拒绝从项目源码树安装引擎；请确认来源后显式使用 force=True。")
    if destination is None:
        override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT")
        raw_destination = (Path(override).expanduser() if override
                           else Path.home() / ".bugx" / "core")
        destination = global_engine_root()
    else:
        raw_destination = Path(destination).expanduser()
        destination = raw_destination.parent.resolve() / raw_destination.name
    resolved_destination = destination.resolve()
    if repository_core is not None and not force \
            and resolved_destination == repository_core.resolve():
        raise CliError(
            "拒绝把引擎布署到源码仓库 core/（BUGX_DEV_MODE 解析目标）；"
            "开发态直接运行仓库即可，无需安装。"
        )
    installed_asset_root = asset_root().resolve()
    if resolved_destination == installed_asset_root:
        raise CliError("目的地不能为 Python 包安装载荷目录（asset_root），严禁覆写 wheel 自带文件。")
    protected_roots: list[Path] = []
    data_root = sysconfig.get_path("data")
    if data_root:
        protected_roots.append(Path(data_root).resolve())
    protected_roots.append(installed_asset_root.parent)
    if any(
        resolved_destination == root or root in resolved_destination.parents
        for root in protected_roots
    ):
        raise CliError("目的地不能位于 Python 包安装数据目录或 asset_root 父目录，严禁覆写 wheel 自带文件。")
    checked_destination = raw_destination.parent.resolve() / raw_destination.name
    _assert_no_symlink_components(checked_destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _engine_install_lock(destination):
        _recover_engine_swap(destination)
        _remove_root_engine_receipt(destination)
        # 载荷目录（asset_root）内的收据是 BugX 自身写下的历史残留（不在 wheel
        # RECORD 中），回收它是合法恢复；但同名外来文件必须保持不动，且不阻塞安装。
        _remove_root_engine_receipt(installed_asset_root, foreign_is_fatal=False)
        source_build_id = _engine_build_id(source)
        installed_build_id = _engine_build_id(destination)
        destination_nonempty = False
        if destination.is_dir():
            try:
                destination_nonempty = any(destination.iterdir())
            except OSError:
                destination_nonempty = True
        elif destination.exists() or destination.is_symlink():
            destination_nonempty = True
        if not force and destination_nonempty:
            if installed_build_id is None:
                raise CliError(
                    "已安装全局引擎缺少 Build ID 标记（可能损坏或来源未知），拒绝静默覆盖；"
                    "如确需覆盖请显式传入 force=True。"
                )
            if source_build_id is None:
                raise CliError(
                    "安装源缺少 Build ID 标记，拒绝安装；请检查安装资源或显式传入 force=True。"
                )
            if installed_build_id > source_build_id:
                raise CliError(
                    f"拒绝降级全局引擎：已安装 build_id={installed_build_id}，"
                    f"安装源 build_id={source_build_id}；如确需回退请显式传入 force=True。"
                )
        expected = _engine_file_manifest(source)
        healthy, detail = _engine_integrity_status(source, destination)
        if healthy and not force:
            receipt_path = destination.parent / "engine-receipt.json"
            if not receipt_path.is_file() and not receipt_path.is_symlink():
                _write_engine_receipt(source, receipt_path)
            return 0, len(expected)
        # 共享引擎是所有项目共用的单点运行时（DOC-004）。用**旧** CLI 自带的 core
        # 覆盖一个更新的引擎，等于把依赖新引擎的其它项目一起打回旧版本 —— 这类
        # 回退必须显式 --force，不能作为普通 install 的副作用发生。
        installed_version = _installed_engine_version(destination)
        source_version = _full_engine_version(__version__, source)
        if not force and _is_newer_version(installed_version, source_version or __version__):
            # 「已安装更新」只允许跳过**自身完整**的引擎替换。损坏/残缺的引擎（例如
            # 缺 launch_phases 这类整包目录）如果也走这条静默返回，install 会报
            # 退出码 0 却把每个项目的 `bugx engine pre` 打成 ModuleNotFoundError。
            # 注意：这里必须用**自身清单**校验，不能拿它跟 source 比对——版本不同
            # 本来就该不同，源比对必然失败，会把「跳过」变成「一律拒绝安装」。
            self_ok, detail = _engine_self_integrity(destination)
            if self_ok:
                print(
                    f"[engine] 已跳过引擎替换：已安装 {installed_version} 比当前 CLI "
                    f"{__version__} 更新且自身完整（{detail}）；如需强制回退请追加 --force",
                    file=sys.stderr,
                )
                return 0, 0
            raise CliError(
                f"已安装的共享引擎 {installed_version} 比当前 CLI {__version__} 新，"
                f"但完整性校验失败（{detail}）；拒绝静默跳过。"
                "请升级 CLI 后重跑，或显式追加 '--force' 用当前载荷重建引擎。"
            )
        stage = destination.parent / f".{destination.name}.staging-{os.getpid()}-{time.time_ns()}"
        backup = destination.parent / f".{destination.name}.backup-{os.getpid()}-{time.time_ns()}"
        receipt_stage = _engine_receipt_stage_path(stage)
        receipt_path = destination.parent / "engine-receipt.json"
        destination_swapped = False
        committed = False
        copied = skipped = 0
        try:
            copied, skipped = _stage_engine(source, destination, stage)
            _write_engine_receipt(source, receipt_stage)
            if os.path.lexists(receipt_path):
                if receipt_path.is_dir() and not receipt_path.is_symlink():
                    raise CliError(f"引擎收据路径是目录，拒绝继续安装: {receipt_path}")
                _assert_no_symlink_components(receipt_path)
            if destination.exists() or destination.is_symlink():
                os.replace(destination, backup)
            os.replace(stage, destination)
            destination_swapped = True
            removed_orphans = _remove_engine_orphans(destination, expected)
            if removed_orphans:
                print(
                    f"[bugx] 已清理 {removed_orphans} 个旧版引擎残留文件（清单外的过时垃圾）",
                    file=sys.stderr,
                )
            os.replace(receipt_stage, receipt_path)
            # 发布后自证：安装的返回值必须是「已验证可用」而不是「已尝试复制」。
            # 历史缺陷：发布后的引擎树与源清单不一致时 install 仍返回 0，直到
            # 下一条 `bugx engine pre` 才以 ModuleNotFoundError 暴露。
            published_ok, published_detail = _engine_integrity_status(source, destination)
            if not published_ok:
                raise CliError(
                    f"引擎发布后完整性自证失败（{published_detail}）；已回滚到上一版本。"
                )
            committed = True
        except BaseException:
            if destination_swapped and (destination.exists() or destination.is_symlink()):
                _remove_engine_path(destination)
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        finally:
            if stage.exists() or stage.is_symlink():
                _remove_engine_path(stage)
            if not committed and os.path.lexists(receipt_stage):
                _remove_engine_receipt_path(receipt_stage)
        if backup.exists() or backup.is_symlink():
            try:
                _remove_engine_path(backup)
            except OSError as exc:
                print(f"[bugx] 警告：无法清理旧引擎备份: {backup}: {exc}", file=sys.stderr)
        return copied, skipped


_SYMLINK_SUPPORT_CACHE: dict[str, bool] = {}


def _link_legacy_project_core(root: Path, *, force: bool = False) -> int:
    """项目 .bugx/core 退化为指向全局引擎的引用（兼容旧布局调用方）。"""
    engine = global_engine_root()
    root = root.resolve()
    legacy = project_core(root)
    _assert_no_symlink_components(legacy.parent)
    if legacy.exists() and legacy.is_symlink():
        raise CliError(_i18n_exc("common.legacy_core_symlink_refused", f"拒绝通过符号链接写入项目核心目录: {legacy}", legacy=legacy))
    legacy.mkdir(parents=True, exist_ok=True)
    legacy_snapshot = False
    metadata = legacy / "metadata.txt"
    if metadata.is_file() and not metadata.is_symlink():
        try:
            legacy_snapshot = metadata.read_text(encoding="utf-8").lstrip().startswith(
                "bugx_version:"
            )
        except OSError:
            legacy_snapshot = False
    if not legacy_snapshot and metadata.is_symlink():
        try:
            metadata_is_engine_link = metadata.resolve() == (engine / "metadata.txt").resolve()
            legacy_snapshot = metadata_is_engine_link and any(
                (legacy / entry.name).exists()
                and not (legacy / entry.name).is_symlink()
                for entry in engine.iterdir()
            )
        except OSError:
            legacy_snapshot = False
    legacy_manifest, legacy_files = _legacy_integrity_manifest(legacy)
    linked = 0

    def _link_or_copy(entry: Path, target: Path) -> None:
        """Use links when available; copy static assets on Windows without link privilege.

        Windows ntpath.relpath raises ValueError when source and target are on
        different drives (C: global engine -> D: project). In that case relpath
        is impossible, so fall back to copy; symlink with absolute path would
        still be fragile across moves, and copy preserves the legacy-compat
        semantics while closing the cross-drive crash.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            rel = os.path.relpath(entry.resolve(), target.parent.resolve())
        except ValueError:
            # Cross-mount on Windows (e.g. C: -> D:); relpath cannot be computed.
            rel = None
        if rel is not None:
            try:
                os.symlink(rel, target)
                return
            except OSError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
                    raise
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)

    def _refreshable_copy(entry: Path, target: Path) -> bool:
        if not legacy_snapshot:
            return False
        relative = entry.relative_to(engine).as_posix()
        if relative == ENGINE_INTEGRITY_NAME:
            if legacy_manifest is None:
                return False
            try:
                current = json.loads(
                    (engine / ENGINE_INTEGRITY_NAME).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False
            return legacy_manifest != current
        return _matches_integrity_record(target, legacy_files.get(relative))

    def _owned_link(target: Path) -> bool:
        try:
            current = target.resolve(strict=False)
            engine_root = engine.resolve()
            legacy_root = legacy.resolve()
            return (
                current == engine_root or engine_root in current.parents
                or current == legacy_root or legacy_root in current.parents
            )
        except OSError:
            return False

    def _link_one(entry: Path, target: Path | None = None) -> None:
        nonlocal linked
        target = target or (legacy / entry.name)
        if target.is_symlink():
            try:
                current = target.resolve(strict=False)
                expected = entry.resolve()
                same = current == expected
            except OSError:
                same = False
            if same or (not force and not _owned_link(target)):
                return
            target.unlink()
        elif target.exists():
            if not force and not _refreshable_copy(entry, target):
                return
            _assert_no_symlink_components(target)
            _remove_engine_path(target)
        _link_or_copy(entry, target)
        linked += 1

    def _link_directory(entry: Path, target: Path) -> None:
        nonlocal linked
        if target.is_symlink():
            try:
                same = target.resolve(strict=False) == entry.resolve()
            except OSError:
                same = False
            if same or (not force and not _owned_link(target)):
                return
            target.unlink()
            _link_or_copy(entry, target)
            linked += 1
            return
        if not target.exists():
            _link_or_copy(entry, target)
            linked += 1
            return
        if not target.is_dir():
            return
        if force:
            _assert_no_symlink_components(target)
            _remove_engine_path(target)
            _link_or_copy(entry, target)
            linked += 1
            return
        if not legacy_snapshot:
            return
        # Reconcile copied directory contents one file at a time.  This adds
        # new package files and refreshes only files proven unchanged by the
        # old manifest, while preserving user edits and unknown files.
        for child in sorted(entry.iterdir()):
            child_target = target / child.name
            if child.is_dir():
                _link_directory(child, child_target)
            else:
                _link_one(child, child_target)

    for entry in sorted(engine.iterdir()):
        if (entry.name in PROJECT_OWNED_CORE_NAMES
                or _is_runtime_artifact_path(entry.name)):
            continue
        if entry.is_file():
            _link_one(entry)
        elif entry.is_dir() and not entry.is_symlink():
            if entry.name == "__pycache__":
                continue
            target = legacy / entry.name
            _link_directory(entry, target)
    return linked


_ENGINE_INDEX_CACHE: dict[tuple[str, int], dict[str, list[Path]]] = {}


def _engine_index_key(root: Path) -> tuple[str, int]:
    """索引缓存键：路径 + 目录自身 mtime。

    引擎根会在同一个进程里被整体换血（``bugx init``/``install`` 的 staging 交换，
    测试夹具的 copy-on-write 取代软链接），路径字符串不变而文件集合已变。
    只按路径缓存会把换血前的可见文件集固定下来——新增脚本被误判为「引擎内
    不存在」。目录 mtime 在增删顶层条目与被替换时都会变，足以识别换血。
    """
    try:
        return (str(root), root.stat().st_mtime_ns)
    except OSError:
        return (str(root), 0)


def _engine_basename_index(root: Path) -> dict[str, list[Path]]:
    """按文件名索引引擎树，供白名单缺失时递归定位。

    引擎已按领域拆成子包（``core/<domain>/x.py``），逐层维护白名单必然滞后于
    一次重构；这里改为一次性建索引 + 唯一性校验，重构后无需再改代码。
    """
    key = _engine_index_key(root)
    cached = _ENGINE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index: dict[str, list[Path]] = {}
    try:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            index.setdefault(path.name, []).append(path)
    except OSError:
        index = {}
    for paths in index.values():
        paths.sort()
    _ENGINE_INDEX_CACHE[key] = index
    return index


def _engine_roots() -> list[Path]:
    """按优先级返回候选引擎根：已安装引擎 → 随包资产。

    两个根是**同一份引擎的两份拷贝**（安装态 ``~/.bugx/core`` 与 wheel 资产），
    不是同一命名空间。取舍必须按根的先后顺序决定，绝不能把它们并成一个
    候选集去比"同名个数"，否则每次正常安装后的 ``bugx init`` 都会因为
    "两个根里各有一个 lesson_ledger.py" 被误判成歧义而 fail-closed。
    """
    roots: list[Path] = []
    for candidate in (global_engine_root(),):
        if candidate.is_dir():
            roots.append(candidate)
    try:
        bundled = asset_root()
    except CliError:
        bundled = None
    if bundled is not None and bundled.is_dir() and bundled not in roots:
        roots.append(bundled)
    return roots


def resolve_engine_file(name: str | Path, *, kind: str = "脚本") -> Path:
    """把引擎内相对文件名解析成真实路径。

    解析顺序（逐根，前一根命中即返回）：
      1. 根下的精确相对路径；
      2. 根内的同名递归查找——**在同一根内**必须唯一。
    单个根内出现多个同名文件时 fail-closed 并列出候选，绝不静默挑一个；
    而跨根的重复是"同一引擎的两份拷贝"，按根优先级取舍，不算歧义。
    """
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise CliError(f"引擎文件名必须是引擎内的相对路径: {relative}")

    roots = _engine_roots()
    basename = relative.name
    for root in roots:
        direct = root / relative
        if direct.is_file() and not direct.is_symlink():
            return direct
        matches = _engine_basename_index(root).get(basename, [])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            listed = "、".join(str(path) for path in matches)
            raise CliError(
                f"引擎内存在多个同名{kind}，请改用带目录的相对路径：{listed}"
            )
    raise CliError(_i18n_exc(
        "common.script_not_found", f"全局引擎中不存在{kind}：{relative.as_posix()}",
        name=relative.as_posix(),
    ))


def _script(root: Path, name: str) -> Path:
    return resolve_engine_file(name, kind="脚本")


_LAYOUT_MODULE_CACHE: dict[tuple[str, int, int], Any] = {}


def _layout_module(root: Path):
    path = _script(root, "bugx_paths.py")
    cache_key: tuple[str, int, int] | None = None
    try:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None:
        cached = _LAYOUT_MODULE_CACHE.get(cache_key)
        if cached is not None:
            return cached
    spec = importlib.util.spec_from_file_location("bugx_runtime_layout", path)
    if spec is None or spec.loader is None:
        raise CliError(_i18n_exc("common.layout_module_load_failed", f"无法加载 BugX 目录布局模块: {path}", path=path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
    if cache_key is not None:
        _LAYOUT_MODULE_CACHE[cache_key] = module
    return module


def _detach_project_owned_core_links(root: Path) -> None:
    """Migrate old engine entities away from mutable project data paths."""
    root = root.resolve()
    legacy = project_core(root)
    if not legacy.is_dir() or legacy.is_symlink():
        return
    layout = _layout_module(root).require(root)
    mapping = {
        "bug-log.md": layout.bug_log,
        "mistake-book.md": layout.mistake_book,
        "lesson-ledger.json": layout.knowledge_seed,
        "lesson-ledger.json.lock": layout.knowledge_root / "lesson-ledger.json.lock",
        "prep-record.md": layout.prep_record,
        "module-coverage.md": layout.state_root / "module-coverage.md",
    }
    engine = global_engine_root().resolve()
    metadata = legacy / "metadata.txt"
    legacy_snapshot = False
    if metadata.is_file() and not metadata.is_symlink():
        try:
            legacy_snapshot = metadata.read_text(encoding="utf-8").lstrip().startswith(
                "bugx_version:"
            )
        except OSError:
            pass
    if metadata.is_symlink():
        try:
            legacy_snapshot = metadata.resolve(strict=False) == (engine / "metadata.txt").resolve()
        except OSError:
            pass

    def _preserve(source: Path, destination: Path, name: str) -> None:
        if not source.is_file():
            return
        if destination.exists():
            try:
                same = _sha256_file(source) == _sha256_file(destination)
            except OSError:
                same = False
            if not same:
                backup = root / ".bugx" / "backups" / "core-migration" / name
                if not backup.exists():
                    _assert_no_symlink_components(backup)
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, backup)
                    backup.chmod(0o600)
            return
        _assert_no_symlink_components(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(0o600)

    for name, destination in mapping.items():
        legacy_link = legacy / name
        if not legacy_link.exists() and not legacy_link.is_symlink():
            continue
        if legacy_link.is_symlink():
            try:
                resolved = legacy_link.resolve(strict=False)
                resolved.relative_to(engine)
            except (OSError, ValueError):
                continue
            _preserve(resolved, destination, name)
            legacy_link.unlink()
        elif legacy_snapshot:
            _preserve(legacy_link, destination, name)
            legacy_link.unlink()


def _legacy_core_runtime_entries(root: Path) -> list[tuple[Path, str]]:
    """List legacy mutable runtime entries that must not live under ``.bugx/core``."""
    legacy = project_core(root)
    if legacy.is_symlink() or not legacy.is_dir():
        return []
    try:
        children = sorted(legacy.iterdir())
    except OSError as exc:
        raise CliError(_i18n_exc("common.legacy_dir_unreadable", f"无法读取项目核心目录，拒绝判断重置范围: {legacy}: {exc}", legacy=legacy, exc=exc)) from exc
    return [
        (path, f"project/core/{path.name}")
        for path in children
        if _is_runtime_artifact_name(path.name)
    ]


def _detach_project_runtime_core_entries(root: Path) -> None:
    """Remove legacy core runtime entries from the active compatibility area."""
    entries = _legacy_core_runtime_entries(root)
    if not entries:
        return
    backup_root = root.resolve() / ".bugx" / "backups" / "core-migration" / "runtime"
    for source, _relative in entries:
        if source.is_symlink():
            source.unlink()
            continue
        _assert_no_symlink_components(backup_root)
        backup_root.mkdir(parents=True, exist_ok=True)
        destination = backup_root / source.name
        if destination.exists() or destination.is_symlink():
            destination = backup_root / f"{source.name}.{time.time_ns()}.bak"
        _assert_no_symlink_components(destination.parent)
        shutil.move(str(source), str(destination))


def _manifest_drift_detail(
    declared: dict[str, object], actual: dict[str, object],
) -> str | None:
    """清单与磁盘的差异摘要（``None`` = 一致）。

    两处调用方（对源清单 / 对自身清单）此前各抄一份比对逻辑，改一处忘另一处就会
    让两条门禁判据漂移——收敛成唯一实现。
    """
    if declared == actual:
        return None
    declared_names = set(declared)
    actual_names = set(actual)
    parts: list[str] = []
    missing = sorted(declared_names - actual_names)
    extra = sorted(actual_names - declared_names)
    changed = sorted(
        name for name in declared_names & actual_names if declared[name] != actual[name]
    )
    if missing:
        parts.append("缺少=" + ",".join(missing[:5]))
    if extra:
        parts.append("多余=" + ",".join(extra[:5]))
    if changed:
        parts.append("哈希漂移=" + ",".join(changed[:5]))
    return "；".join(parts) or "文件集合不一致"


def _engine_self_integrity(destination: Path) -> tuple[bool, str]:
    """校验**已安装引擎自身**的自洽性（不拿它跟当前源码比对）。

    「已安装比 CLI 新」时不能用源清单比对：不同版本本来就该不同，比对必然失败，
    于是唯一可能的结论只剩「拒绝安装」——而正确语义是「跳过且不破坏」。这里只问
    一个问题：它自己声明的清单与磁盘是否一致（残缺引擎 = 缺文件/哈希漂移）。
    """

    if destination.is_symlink() or not destination.is_dir():
        return False, f"全局引擎目录缺失或不安全: {destination}"
    integrity = destination / ENGINE_INTEGRITY_NAME
    if integrity.is_symlink() or not integrity.is_file():
        return False, "缺少 engine-integrity.json"
    try:
        payload = json.loads(integrity.read_text(encoding="utf-8"))
        actual_files = _engine_file_manifest(destination)
    except (OSError, json.JSONDecodeError, CliError) as exc:
        return False, f"引擎清单无法读取: {exc}"
    if payload.get("schema_version") != ENGINE_INTEGRITY_SCHEMA:
        return False, "engine-integrity.json schema_version 过期"
    declared = payload.get("files")
    if not isinstance(declared, dict) or not declared:
        return False, "engine-integrity.json 缺少文件清单"
    if payload.get("file_count") != len(declared):
        return False, "引擎文件数量与自身清单不一致"
    # 刻意**不**重算 bundle_sha256：它把 bugx_version 一起纳入哈希，而这里要回答的
    # 是「已安装引擎是否完整可运行」，不是「版本戳是否被改写」。用源版本重算必然
    # 失配，会把合法的「已安装更新」误判成损坏而拒绝安装。
    drift = _manifest_drift_detail(declared, actual_files)
    if drift is not None:
        return False, drift or "引擎文件与自身清单不一致"
    return True, f"{len(declared)} 个静态文件与自身清单一致"


def _engine_integrity_status(source: Path, destination: Path) -> tuple[bool, str]:
    """Compare the installed engine with the current source bundle."""
    if destination.is_symlink() or not destination.is_dir():
        return False, f"全局引擎目录缺失或不安全: {destination}"
    integrity = destination / ENGINE_INTEGRITY_NAME
    if integrity.is_symlink() or not integrity.is_file():
        return False, "缺少 engine-integrity.json"
    try:
        payload = json.loads(integrity.read_text(encoding="utf-8"))
        expected_files = _engine_file_manifest(source)
        actual_files = _engine_file_manifest(destination)
    except (OSError, json.JSONDecodeError, CliError) as exc:
        return False, f"引擎清单无法读取: {exc}"
    payload_version = payload.get("bugx_version")
    if _contract_version(payload_version) != _contract_version(__version__):
        return False, f"引擎版本 {payload_version!r} != {__version__}"
    # Keep the serialized hash contract tied to the installed marker while
    # comparing alpha/stable builds by their normalized base version.
    expected = _engine_manifest_payload(str(payload_version), expected_files)
    if payload.get("schema_version") != ENGINE_INTEGRITY_SCHEMA:
        return False, "engine-integrity.json schema_version 过期"
    if payload.get("file_count") != expected["file_count"]:
        return False, "引擎文件数量与源清单不一致"
    if payload.get("bundle_sha256") != expected["bundle_sha256"]:
        return False, "引擎 bundle_sha256 与源清单不一致"
    if payload.get("files") != expected_files:
        return False, "engine-integrity.json 文件清单与源不一致"
    drift = _manifest_drift_detail(expected_files, actual_files)
    if drift is not None:
        return False, drift or "引擎文件与源清单不一致"
    return True, f"{payload['file_count']} 个静态文件哈希一致，bundle={payload['bundle_sha256'][:12]}"


def _version_marker(path: Path, pattern: re.Pattern[str], *, allow_symlink: bool = False) -> str | None:
    """Read a version marker from a file without following untrusted links."""
    try:
        if allow_symlink and path.is_symlink():
            path = path.resolve(strict=True)
        else:
            _assert_no_symlink_components(path)
        if not path.is_file():
            return None
        match = pattern.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, CliError):
        return None
    value = match.group(1) if match else None
    return value if value and _CONTRACT_VERSION_RE.fullmatch(value) else None


def _json_version_marker(path: Path, key: str) -> str | None:
    try:
        _assert_no_symlink_components(path)
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, CliError):
        return None
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, str) and _CONTRACT_VERSION_RE.fullmatch(value) else None


def _version_contract_status(root: Path, engine: Path) -> tuple[bool, str]:
    """Compare CLI, source, project manifest and global engine markers."""
    source_root = asset_root()
    raw_values: dict[str, str | None] = {
        "cli": (
            f"{__version__}+build.{getattr(bugx_cli, '__build_id__', None)}"
            if getattr(bugx_cli, "__build_id__", None) is not None
            else _full_engine_version(__version__, source_root)
        ),
        "source": _full_engine_version(_version_marker(
            source_root / "bugx.md", re.compile(r"^version:\s*([0-9A-Za-z.+-]+)", re.M),
        ), source_root),
        "engine_metadata": _version_marker(
            engine / "metadata.txt", re.compile(r'bugx_version:\s*"([0-9A-Za-z.+-]+)"'),
        ),
        "engine_integrity": _json_version_marker(
            engine / ENGINE_INTEGRITY_NAME, "bugx_version",
        ),
        "engine_descriptor": _version_marker(
            engine / "bugx.md", re.compile(r"^version:\s*([0-9A-Za-z.+-]+)", re.M),
        ),
    }
    for name in ("engine_metadata", "engine_integrity", "engine_descriptor"):
        raw_values[name] = _full_engine_version(raw_values[name], engine)
    values = raw_values
    is_project_initialized = (root / ".bugx" / "manifest.json").is_file()
    if is_project_initialized:
        legacy_manifest = _contract_version(_json_version_marker(
            root / ".bugx" / "manifest.json", "bugx_version",
        ))
        layout_version = _json_version_marker(
            root / ".bugx" / "manifest.json", "layout_version",
        )
    else:
        legacy_manifest = None
        layout_version = None
    missing = [name for name, value in values.items() if value is None]
    distinct = sorted({value for value in values.values() if value is not None})
    issues = []
    if missing:
        issues.append("缺失或无效=" + ",".join(missing))
    if len(distinct) > 1:
        issues.append("版本不一致")
    core_versions = {_contract_version(value) for value in values.values() if value is not None}
    if legacy_manifest is not None and distinct and legacy_manifest not in core_versions:
        issues.append(f"project_manifest={legacy_manifest}（与核心版本不一致）")
    if is_project_initialized:
        try:
            layout_module = _layout_module(root)
            if layout_version != getattr(layout_module, "LAYOUT_VERSION", None):
                issues.append(
                    f"project_layout={layout_version or 'missing'}（需要 "
                    f"{getattr(layout_module, 'LAYOUT_VERSION', 'unknown')}；"
                    "请运行 bugx init）"
                )
            project_integrity = layout_module.project_core_integrity_status(root, engine)
            if project_integrity.get("available") and not project_integrity.get("ok"):
                issues.append("旧项目核心入口仍存在")
        except (CliError, OSError, RuntimeError, ValueError) as exc:
            issues.append(f"项目布局无法校验: {exc}")
    if issues:
        return False, "；".join(issues) + "；版本明细=" + json.dumps(values, ensure_ascii=False)
    detail = f"CLI、项目清单和全局引擎均为 {distinct[0]}；项目无本地可执行引擎"
    return True, detail


def _find_current_cli_executable() -> Path | None:
    """定位当前运行中的真实 bugx 可执行入口。"""
    exe_dir = Path(sys.executable).parent
    names = ("bugx.exe", "bugx") if os.name == "nt" else ("bugx",)
    for name in names:
        candidate = exe_dir / name
        if candidate.is_file():
            return candidate.resolve()
    which_bugx = shutil.which("bugx")
    if which_bugx:
        p = Path(which_bugx).resolve()
        if p.is_file():
            return p
    return None


def _ensure_global_cli_symlinks() -> list[tuple[Path, str]]:
    """在常见用户级标准 PATH 目录（~/.local/bin、~/.cargo/bin）维护 bugx 软链接。

    返回处理的 (symlink_path, status) 列表。
    status 取值: 'created', 'unchanged', 'skipped_regular_file', 'skipped_error'。
    当处于只读环境、Windows 无特权或目标为非软链接文件时安全跳过。
    """
    if os.name == "nt":
        return []
    cli_exe = _find_current_cli_executable()
    if not cli_exe or not cli_exe.is_file():
        return []

    home = Path.home().resolve()
    candidate_dirs = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        home / ".bugx" / "bin",
    ]
    results: list[tuple[Path, str]] = []
    for cdir in candidate_dirs:
        if not cdir.is_dir():
            if cdir.parent.is_dir() and cdir.name == "bin":
                try:
                    cdir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    continue
            else:
                continue
        target_link = cdir / "bugx"
        if cli_exe.parent.resolve() == cdir.resolve():
            continue
        try:
            if target_link.is_symlink():
                try:
                    resolved = target_link.resolve(strict=True)
                    if resolved == cli_exe:
                        results.append((target_link, "unchanged"))
                        continue
                except OSError:
                    pass
                target_link.unlink()
            elif target_link.exists():
                results.append((target_link, "skipped_regular_file"))
                continue

            target_link.symlink_to(cli_exe)
            results.append((target_link, "created"))
        except OSError:
            results.append((target_link, "skipped_error"))
    return results

