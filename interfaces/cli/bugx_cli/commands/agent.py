from __future__ import annotations

import argparse
import contextvars
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import _close_bugx_logging
from ..infra.engine_installer import refresh_i18n_after_engine_swap
from ..common import *
from ..hosts.registry import (
    engine_host_cli_hook,
    engine_host_label,
    engine_host_plugins,
    engine_mirror_root,
    engine_worker_entry_dir,
)
# 安装流程按能力标签分支，而不是按宿主名字符串比较；标签与宿主名的绑定
# 只在 host_adapters.HOST_ADAPTERS 声明处发生一次。
from ..host_adapters import (
    CAP_DISTRIBUTION_PLUGIN,
    CAP_EXTENSION_PAYLOAD,
    CAP_EXTRA_TRACKED_PATHS,
    CAP_GLOBAL_AGENT_LINK,
    CAP_GLOBAL_SKILL_LINK,
    CAP_NATIVE_MCP_CONFIG,
    CAP_PREVALIDATE_CATALOG,
    CAP_PROJECT_MCP_CONFIG,
    CAP_PROJECT_PATCH_OVERLAY,
    CAP_USER_AGENT_REGISTRATION,
    CAP_USER_LEVEL_ARTIFACT,
    CAP_WORKSPACE_MIRROR,
    CAP_WORKSPACE_MIRROR_VARIANTS,
    ENTRY_MULTI_PATH,
    ENTRY_PLUGIN_CATALOG,
    ENTRY_SHARED_SYMLINK,
    ENTRY_SKILL_PAYLOAD,
    ENTRY_USER_REGISTRATION,
    HOST_ADAPTERS,
    HOST_NAME_CLAUDE,
    HOST_NAME_FUNIDE,
    HOST_NAME_QODER,
    HOST_NAME_ZCODE,
)
from .plugin import (
    _chatgpt_marketplace,
    _ensure_chatgpt_plugin,
    _ensure_zcode_plugin,
    _sweep_legacy_skill_entry,
    _sweep_legacy_zcode_project_entries,
    _prompt_for_hosts,
    _restore_host_mcp_config_change,
    _validate_host_supervisor_mcp,
    _validate_zcode_global_supervisor_mcp,
    _write_host_entry,
    write_host_mcp_config,
    _zcode_bound_project_root,
    _ensure_zcode_user_skill,
    _zcode_user_agents_dir,
)
from .plugin_claude import (
    _claude_user_agents_dir,
)
from .plugin_funide import (
    _NATIVE_AGENT_OWNERSHIP_MARK,
    _funide_data_dir,
    _funide_profile_mcp_path,
    _funide_user_skills_dir,
    _funide_user_agents_dir,
)



class _CliProgress:
    """Render completed CLI stages without affecting machine-readable output."""

    _WIDTH = 24

    def __init__(self, args: argparse.Namespace) -> None:
        self.enabled = (
            not getattr(args, "json", False)
            and os.environ.get("BUGX_NO_PROGRESS") != "1"
            and bool(getattr(sys.stderr, "isatty", lambda: False)())
        )

    def update(self, percent: int, label: str) -> None:
        if not self.enabled:
            return
        percent = max(0, min(100, percent))
        filled = self._WIDTH * percent // 100
        line = f"[bugx] 进度 [{('=' * filled).ljust(self._WIDTH, '-')}] {percent:3d}% {label}"
        sys.stderr.write("\r" + line)
        sys.stderr.flush()

    def finish(self, label: str = "完成") -> None:
        if not self.enabled:
            return
        self.update(100, label)
        sys.stderr.write("\n")
        sys.stderr.flush()

    def fail(self, label: str = "失败，正在回滚") -> None:
        if not self.enabled:
            return
        sys.stderr.write("\n")
        self.update(0, label)
        sys.stderr.write("\n")
        sys.stderr.flush()


# Backward-compatible test/import name; the renderer is shared by all long CLI commands.
_InstallProgress = _CliProgress

#: **未迁移**宿主的安装面基线（逐宿主切片时删除对应项）。已迁移宿主的目录由
#: ``HOST_ADAPTERS.managed_surfaces()``（插件 marker 投影）自动追加。
#: **未迁移**宿主的用户级可见位置提示；已迁移宿主用 ``全局 <label> 可见``。
_USER_AGENT_VISIBILITY_HINTS = {
    "funide": "FunIDE 设置 → 智能体可见",
}


def _user_agent_visibility(host_name: str) -> str:
    """用户级注册后的「在哪里可见」提示（未迁移宿主走基线表）。"""

    hint = _USER_AGENT_VISIBILITY_HINTS.get(host_name)
    return hint or f"全局 {engine_host_label(host_name)} 可见"


_BASE_AGENT_INSTALL_SURFACES = (
    ".bugx", ".opencode", ".claude", ".zcode", ".pi", ".agents",
    ".workbuddy", ".codebuddy", ".qoder", ".github",
    ".dsh", "plugins", CONFIG_NAME,
)
_AGENT_INSTALL_SURFACES = tuple(
    dict.fromkeys(
        list(_BASE_AGENT_INSTALL_SURFACES)
        + [surface for adapter in HOST_ADAPTERS for surface in adapter.managed_surfaces()]
    )
)

# The transaction protects only BugX-owned files that the install path can
# write.  It must never copy an entire host directory: user-level installs
# commonly run from HOME, where .codex/.tmp, plugin caches, sessions, and
# databases are both unrelated to BugX and likely to change concurrently.
_AGENT_INSTALL_SNAPSHOT_FILES = (
    ".bugx/manifest.json",
    ".bugx/current.json",
    ".bugx/hosts.json",
    ".bugx/README.md",
    ".bugx/index/index.json",
    ".bugx/knowledge/manifest.json",
    ".bugx/knowledge/lesson-ledger.json",
    ".bugx/knowledge/lesson-ledger.sqlite3",
    ".bugx/knowledge/lesson-ledger.sqlite3-shm",
    ".bugx/knowledge/lesson-ledger.sqlite3-wal",
    ".bugx/knowledge/bug-log.md",
    ".bugx/knowledge/mistake-book.md",
    "opencode.json",
    ".mcp.json",
    ".opencode/agent/bugx.md",
    ".opencode/agents/bugx.md",
    ".opencode/agents/bugx-worker.md",
    ".claude/agents/bugx.md",
    ".claude/skills/bugx/SKILL.md",
    ".claude/dist/bugx.plugin",
    ".claude/dist/bugx.skill",
    ".dsh/patch.yml",
    ".dsh/skills/bugx/SKILL.md",
    ".dsh/skills/bugx-dsh/SKILL.md",
    ".dsh/agents/bugx.md",
    ".zcode/agents/bugx.md",
    ".workbuddy/skills/bugx/SKILL.md",
    ".workbuddy/connector/bugx/connector-meta.json",
    ".workbuddy/connector/bugx/icon.png",
    ".workbuddy/connector/bugx/skills/bugx/SKILL.md",
    ".codebuddy/skills/bugx/SKILL.md",
    ".pi/skills/bugx/SKILL.md",
    ".pi/extensions/bugx/index.ts",
    ".pi/extensions/bugx/manifest.json",
    ".pi/extensions/bugx/README.md",
    ".agents/plugins/marketplace.json",
    ".qoder/agents/bugx.md",
    ".qoder/agents/bugx-worker.md",
    ".qoder/mcp.json",
    ".github/agents/bugx.agent.md",
    ".trea/agents/bugx.md",
    ".trea/mcp.json",
    "plugins/bugx/.codex-plugin/plugin.json",
    "plugins/bugx/assets/bugx-avatar-512.png",
    "plugins/bugx/.zcode-plugin/plugin.json",
    "plugins/bugx/agents/bugx.md",
    "plugins/bugx/agents/bugx-worker.md",
    "plugins/bugx/skills/bugx/SKILL.md",
    "plugins/bugx/skills/bugx-zcode/SKILL.md",
    "plugins/bugx/skills/bugx-claude/SKILL.md",
    "plugins/bugx/skills/bugx-dsh/SKILL.md",
    "integrations/qoder/connector-meta.json",
    "integrations/qoder/CONNECTORS.md",
) + tuple(
    dict.fromkeys(
        rel
        for plugin in engine_host_plugins()
        for rel in (
            plugin.install.path,
            *plugin.install.required_paths,
            *plugin.install.legacy_paths,
        )
        if rel and not Path(rel).is_absolute()
    )
)


def _json_default(value: object) -> str:
    """Serialize filesystem paths in machine-readable install results."""
    if isinstance(value, Path):
        return str(value)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )
_AGENT_INSTALL_SNAPSHOT_MANIFEST = ".agent-install-snapshot.json"

_ENGINE_VERSION_RE = re.compile(r'bugx_version:\s*"(\d+\.\d+\.\d+(?:\+[0-9A-Za-z.-]+)?)"')


def _read_engine_version(engine: Path) -> str | None:
    """读取已安装引擎声明的版本；缺失或不可读时返回 None。"""
    marker = engine / "metadata.txt"
    try:
        if not marker.is_file() or marker.is_symlink():
            return None
        content = marker.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    match = _ENGINE_VERSION_RE.search(content)
    return match.group(1) if match else None


def _version_key(value: str) -> tuple[int, ...] | None:
    """把 ``x.y.z`` 归一为可比较的元组；非规范版本返回 None。"""
    try:
        return tuple(int(part) for part in value.split("+", 1)[0].split("."))
    except ValueError:
        return None


def _warn_engine_version_drift(
    prior_engine_version: str | None, *, replaced: bool | None = None,
) -> None:
    """引擎版本与当前 CLI 不一致时告警，并给出方向对应的补救动作。

    必须在 ``_install_engine`` **之前**采样版本：``_stage_engine`` 会用运行中的
    CLI 版本重新盖章 ``metadata.txt``，因此安装之后再比对必然得到两个相等的版本，
    永远观察不到漂移（旧实现即因此成为不可达分支，两条路径都到不了告警）。

    ``replaced`` 说明引擎是否**真的**被换掉了：``_install_engine`` 遇到「已安装
    引擎更新且未 --force」时会跳过替换，此时不能宣称「已回退」。
    """
    if prior_engine_version is None or prior_engine_version == __version__:
        return
    order = None
    prior_key = _version_key(prior_engine_version)
    current_key = _version_key(__version__)
    if prior_key is not None and current_key is not None:
        order = prior_key < current_key
    if order is True:
        detail = f"已安装的 core 版本 {prior_engine_version} 落后于当前 CLI 版本 {__version__}。"
        remedy = "请先使用普通 'bugx install' 刷新；仅存在静态冲突时再追加 '--force'。"
    elif order is False:
        detail = f"当前 CLI 版本 {__version__} 落后于已安装的 core 版本 {prior_engine_version}。"
        if replaced is True:
            remedy = (
                "本次安装已把共享引擎回退到该 CLI 自带的版本，可能影响依赖新引擎的其他项目；"
                "请升级 CLI 后重跑（例如 'uv tool install --reinstall bugx-cli'）。"
            )
        elif replaced is False:
            remedy = (
                "已跳过引擎替换以避免回退所有项目共享的引擎；"
                "请升级 CLI 后重跑（例如 'uv tool install --reinstall bugx-cli'），"
                "确需回退请追加 '--force'。"
            )
        else:
            remedy = (
                "请升级 CLI 后重跑（例如 'uv tool install --reinstall bugx-cli'）；"
                "确需回退请追加 '--force'。"
            )
    else:
        detail = f"已安装的 core 版本 {prior_engine_version} 与当前 CLI 版本 {__version__} 不一致。"
        remedy = "请确认 CLI 与共享引擎来自同一发行版本。"
    # 警告走 stderr，避免污染 --json 的机器可读 stdout 契约。
    print(f"[bugx] 警告：全局引擎版本不一致，{detail}{remedy}", file=sys.stderr)


def _knowledge_marker_is_global(marker: Path) -> bool:
    """标记文件显式声明 ``scope=global`` 时返回 True。

    项目知识根与全局知识根塌缩到同一目录时（从 HOME 执行安装），全局清单会占用
    项目清单的路径。它不构成「项目错题集曾经存在」的证据，因此不能被当作
    fail-closed 的依据。无法解析时返回 False，保留原有的 fail-closed 行为。
    """
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("scope") == "global"


def command_agent(args: argparse.Namespace) -> int:
    """Run host install as a recoverable project-local transaction."""
    _prior_no_llm = os.environ.get("BUGX_PROFILE_NO_LLM")
    os.environ["BUGX_PROFILE_NO_LLM"] = "1"
    try:
        if getattr(args, "agent_command", "install") == "discover":
            return _command_agent_impl(args)
        verify_builtin_mcp_manifest()
        progress = _CliProgress(args)
        progress.update(0, "准备安装")
        root = args.root.resolve()
        try:
            staging, existed = _agent_install_snapshot(root)
        except (OSError, shutil.Error) as exc:
            raise CliError(_i18n_exc(
                "agent.install_snapshot_failed",
                f"创建安装事务快照失败，未修改文件：{exc}；请确认 --root 存在且受管文件可读",
                exc=exc,
            )) from exc
        token = _GLOBAL_TX.set(staging)
        preserve_staging = False
        try:
            return _command_agent_impl(args, progress=progress)
        except BaseException as install_error:
            progress.fail()
            rollback_errors: list[str] = []
            try:
                # Rollback deletes the live ``.bugx`` tree; release its diagnostic
                # lock before restoring (Windows otherwise raises WinError 32).
                _close_bugx_logging()
            except BaseException as exc:
                rollback_errors.append(f"关闭诊断日志失败: {exc}")
            # Restore global entries before project-managed paths. Continue after
            # one failure so independent snapshots still have a chance to recover.
            try:
                _restore_global_transaction(staging)
            except BaseException as exc:
                rollback_errors.append(f"全局入口: {exc}")
            try:
                _restore_agent_install_snapshot(root, staging, existed)
            except BaseException as exc:
                rollback_errors.append(f"项目入口: {exc}")
            if rollback_errors:
                preserve_staging = True
                details = "；".join(rollback_errors)
                raise CliError(
                    f"安装失败（{install_error}），且回滚不完整：{details}；"
                    f"事务快照已保留在 {staging}"
                ) from install_error
            raise
        finally:
            _GLOBAL_TX.reset(token)
            if not preserve_staging:
                shutil.rmtree(staging, ignore_errors=True)
    finally:
        if _prior_no_llm is None:
            os.environ.pop("BUGX_PROFILE_NO_LLM", None)
        else:
            os.environ["BUGX_PROFILE_NO_LLM"] = _prior_no_llm


def _is_special_install_entry(path: Path) -> bool:
    """Runtime sockets/FIFOs/devices are not install transaction payload."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode))


def _ignore_install_snapshot_entries(directory: str, names: list[str]) -> list[str]:
    return [name for name in names if _is_special_install_entry(Path(directory) / name)]


def _agent_install_snapshot(root: Path) -> tuple[Path, dict[str, bool]]:
    """Stage only BugX-owned files before a host install.

    Installation touches shared runtime files as well as one or more host
    entries.  A failure in a later host must not leave an apparently installed
    but incomplete bundle, so the wrapper below restores this snapshot.  Host
    directories themselves are never recursively copied; unrelated files are
    preserved in place.
    """
    staging = Path(tempfile.mkdtemp(prefix="bugx-install-transaction-")).resolve()
    existed: dict[str, bool] = {}
    try:
        for relative in _AGENT_INSTALL_SURFACES:
            target = root / relative
            existed[relative] = target.exists() or _is_reparse_point(target)
            if not existed[relative]:
                continue
            if _is_reparse_point(target):
                if relative == ".agents":
                    # ponytail: .agents may be a host-owned Windows reparse point;
                    # leave it outside this project transaction. ChatGPT has a
                    # restricted resolver; other host writers remain fail-closed.
                    existed.pop(relative)
                    continue
                if not target.exists():
                    try:
                        target.unlink()
                        existed[relative] = False
                        continue
                    except OSError:
                        pass
                raise CliError(_i18n_exc("agent.symlink_install_target_refused", f"拒绝以符号链接作为安装事务目标: {target}", target=target))

        entries: list[dict[str, object]] = []
        for relative in dict.fromkeys(_AGENT_INSTALL_SNAPSHOT_FILES):
            surface = Path(relative).parts[0]
            if surface not in existed:
                # .agents may be a host-owned reparse point and was explicitly
                # removed from the transaction above.
                continue
            target = root / relative
            entry: dict[str, object] = {"path": relative, "existed": False}
            if target.is_symlink():
                entry.update({
                    "existed": True,
                    "kind": "symlink",
                    "target": os.readlink(target),
                    "target_is_directory": target.is_dir(),
                })
            elif target.is_file():
                staged = staging / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, staged)
                entry.update({"existed": True, "kind": "file"})
            elif target.is_dir():
                # The install path expects a file at these locations.  Record
                # the shape without traversing it so rollback can preserve it.
                entry.update({"existed": True, "kind": "directory"})
            entries.append(entry)

        (staging / _AGENT_INSTALL_SNAPSHOT_MANIFEST).write_text(
            json.dumps({"files": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return staging, existed
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# F-L0-03：安装事务的**全局面**日志（用户级全局入口的回滚依据）
#
# 项目内受管面由 ``_agent_install_snapshot`` / ``_restore_agent_install_snapshot``
# 负责；本机制补齐**用户级全局入口**（``~/.config/opencode/agents``、``~/.agents``、
# ``~/.pi``、WorkBuddy 用户级 skills 等）。任一宿主安装失败时，两者都必须回到
# 安装前状态，不得留下半安装。
# ---------------------------------------------------------------------------
_GLOBAL_TX: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "bugx_agent_global_tx", default=None,
)


def _global_tx_journal(staging: Path) -> Path:
    return staging / "global-journal.json"


def _read_global_tx_entries(journal: Path) -> list[dict[str, object]]:
    if journal.is_symlink():
        raise CliError(f"全局安装事务日志不能是符号链接: {journal}")
    if not journal.exists():
        return []
    if not journal.is_file():
        raise CliError(f"全局安装事务日志不是普通文件: {journal}")
    try:
        loaded = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"无法读取全局安装事务日志: {journal}: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), list):
        raise CliError(f"全局安装事务日志格式无效: {journal}")
    if any(not isinstance(entry, dict) for entry in loaded["entries"]):
        raise CliError(f"全局安装事务日志含无效记录: {journal}")
    return list(loaded["entries"])


def _tx_stage_global(target: Path) -> None:
    """先持久化全局目标快照；无法保护目标时必须在写入前失败。"""
    staging = _GLOBAL_TX.get()
    if staging is None:
        return
    journal = _global_tx_journal(staging)
    if not target.is_absolute():
        raise CliError(f"全局安装事务目标必须是绝对路径: {target}")
    entries = _read_global_tx_entries(journal)
    index = len(entries)
    snapshot = staging / "global" / str(index)
    entry: dict[str, object] = {"path": str(target), "index": index}
    try:
        if target.is_symlink():
            entry.update(kind="symlink", link=os.readlink(target))
        elif target.is_file():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, snapshot)
            entry.update(kind="file", snapshot=str(snapshot))
        elif target.is_dir():
            entry.update(kind="dir")
        else:
            entry.update(kind="missing")
    except OSError as exc:
        raise CliError(f"无法为全局安装目标创建回滚快照，目标未修改: {target}: {exc}") from exc
    entries.append(entry)
    try:
        _write_private_json(journal, {"entries": entries})
    except (OSError, CliError) as exc:
        raise CliError(f"无法持久化全局安装事务日志，目标未修改: {journal}: {exc}") from exc


def _ensure_global_tx_target_replaceable(target: Path) -> None:
    if target.is_symlink() or target.is_file() or not target.exists():
        return
    raise OSError(f"拒绝替换非普通文件的回滚目标: {target}")


def _restore_global_tx_file(snapshot: Path, target: Path) -> None:
    if snapshot.is_symlink() or not snapshot.is_file():
        raise OSError(f"全局回滚快照缺失或不是普通文件: {snapshot}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(target.parent)
    _ensure_global_tx_target_replaceable(target)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.restore-",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(snapshot, temporary)
        # replace 操作替换目标符号链接本身，不会向它所指向的引擎文件写入。
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _restore_global_tx_symlink(link: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_symlink_components(target.parent)
    _ensure_global_tx_target_replaceable(target)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.restore-",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        os.symlink(link, temporary)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _restore_global_transaction(staging: Path) -> None:
    """按日志逆序恢复全局入口；无法完整恢复时保留证据并 fail-closed。"""
    journal = _global_tx_journal(staging)
    if not journal.exists() and not journal.is_symlink():
        return
    entries = _read_global_tx_entries(journal)
    failures: list[str] = []
    for entry in reversed(entries):
        target_text = entry.get("path")
        if not isinstance(target_text, str) or not target_text:
            failures.append("日志记录缺少目标路径")
            continue
        target = Path(target_text)
        if not target.is_absolute():
            failures.append(f"日志目标不是绝对路径: {target}")
            continue
        kind = entry.get("kind")
        try:
            _assert_no_symlink_components(target.parent)
            if kind == "dir":
                if not target.is_dir() or target.is_symlink():
                    raise OSError(f"原目录状态已变化，无法安全自动恢复: {target}")
                continue
            _ensure_global_tx_target_replaceable(target)
            if kind == "missing":
                if target.is_symlink() or target.is_file():
                    target.unlink()
            elif kind == "symlink":
                link = entry.get("link")
                if not isinstance(link, str):
                    raise OSError("符号链接快照缺少 link 字符串")
                _restore_global_tx_symlink(link, target)
            elif kind == "file":
                snapshot_text = entry.get("snapshot")
                if not isinstance(snapshot_text, str) or not snapshot_text:
                    raise OSError("普通文件快照缺少路径")
                snapshot = Path(snapshot_text)
                if not snapshot.is_absolute() or not snapshot.is_relative_to(staging):
                    raise OSError(f"回滚快照越出事务目录: {snapshot}")
                _restore_global_tx_file(snapshot, target)
            else:
                raise OSError(f"未知全局回滚记录类型: {kind!r}")
        except (OSError, CliError, ValueError) as exc:
            failures.append(f"{target}: {exc}")
    if failures:
        raise CliError("全局入口回滚不完整: " + "；".join(failures))


def _atomic_write_global_file_tx(path: Path, content: bytes) -> None:
    """F-L0-03：全局写入的**唯一收口**——写入前登记事务快照，失败时按记录还原。

    安装期所有用户级全局写入都必须经由此包装器，避免逐点插桩遗漏。
    """
    _tx_stage_global(Path(path))
    _atomic_write_global_file(path, content)


def _remove_install_snapshot_tree(path: Path) -> None:
    """Remove rollback-owned entries without touching runtime special files."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.is_dir():
        return
    for child in path.iterdir():
        if _is_special_install_entry(child):
            continue
        _remove_install_snapshot_tree(child)
    try:
        path.rmdir()
    except OSError:
        # A directory containing a preserved socket/FIFO remains host-owned.
        pass


def _restore_agent_install_snapshot(
    root: Path, staging: Path, existed: dict[str, bool],
) -> None:
    manifest_path = staging / _AGENT_INSTALL_SNAPSHOT_MANIFEST
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = payload.get("files", []) if isinstance(payload, dict) else []
        for item in reversed(entries):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            target = root / str(item["path"])
            if not item.get("existed"):
                _remove_agent_snapshot_path(target)
                continue
            kind = item.get("kind")
            if kind == "directory" and target.is_dir() and not target.is_symlink():
                continue
            _remove_agent_snapshot_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "file":
                shutil.copy2(staging / str(item["path"]), target)
            elif kind == "symlink":
                os.symlink(
                    str(item.get("target", "")), target,
                    target_is_directory=bool(item.get("target_is_directory")),
                )
            elif kind == "directory":
                target.mkdir(parents=True, exist_ok=True)

        # Remove only surfaces that did not exist before this transaction.
        # Existing host directories (including HOME/.codex) remain untouched.
        for relative in reversed(tuple(existed)):
            if existed[relative]:
                continue
            _remove_agent_snapshot_path(root / relative)
        return

    # Compatibility fallback for snapshots created by older in-process callers.
    for relative in tuple(existed):
        target = root / relative
        staged = staging / relative
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            _remove_install_snapshot_tree(target)
        if existed[relative] and staged.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if staged.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staged, target, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(staged, target)


def _remove_agent_snapshot_path(path: Path) -> None:
    """Remove a transaction-owned path without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)



def _installed_engine_build_id() -> int:
    """实际安装源（asset_root）metadata 的 build_id（F-RSI-20261110-009）。

    hosts.json 的 bugx_build_id 必须记录「实际装配的引擎版本」——
    common.get_build_id() 的 bugx_cli.__build_id__ 短路返回的是 **CLI 包**
    的构建号，与 BUGX_ASSET_ROOT override 后的实际安装源脱钩（实测：
    override 装了仓库 439，hosts.json 却记 CLI 包 430，版本门禁
    recorded vs 引擎 永远错位）。安装源 metadata 是唯一权威。
    """
    try:
        # 仓库版显式导入：agent.py 的 .infra 相对导入在 -m 运行态会解析到
        # site-packages 的 bugx_cli 包（快照视角），必须绕开。
        from interfaces.cli.bugx_cli.infra.engine_installer import global_engine_root as _ger
        meta = _ger() / "metadata.txt"
        if meta.is_file():
            match = re.search(r"build_id:\s*(\d+)", meta.read_text(encoding="utf-8"))
            if match:
                return int(match.group(1))
    except (OSError, ImportError):
        pass
    return get_build_id() or 1

def _command_agent_impl(
    args: argparse.Namespace, *, progress: _CliProgress | None = None,
) -> int:
    """发现并安装到宿主 AI；不启动宿主，也不执行审计。"""
    root = args.root.resolve()
    command = getattr(args, "agent_command", "install")
    _ensure_core_importable()
    import core.i18n as i18n
    deployment_locale = i18n.resolve_locale().resolved_locale
    if getattr(args, "locale", None):
        norm = i18n.normalize_locale(args.locale)
        if not norm:
            raise CliError(_i18n_exc("common.invalid_locale", f"不支持的 locale: {args.locale}；可选：zh-CN, en-US", locale=args.locale))
        deployment_locale = norm
    hosts = discover_hosts(root)
    if command == "discover":
        payload = {"root": str(root), "hosts": hosts, "execution": "none"}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in hosts:
                state = "FOUND" if item["installed"] else "NOT_FOUND"
                executable = item["executable"] or item.get("app_path") or "-"
                print(f"{item['name']}\t{state}\t{executable}\t{item['install_path']}")
        return 0
    if command != "install":
        raise CliError(_i18n_exc("agent.unknown_agent_command", f"未知 Agent 命令: {command}", command=command))
    raw_requested = []
    for item in getattr(args, "agent", []):
        for sub in str(item).split(","):
            sub = sub.strip()
            if sub:
                raw_requested.append(sub)
    requested = list(dict.fromkeys(raw_requested))
    if not requested:
        if args.non_interactive:
            raise CliError(_i18n_exc("agent.non_interactive_agent_required", "非交互安装必须显式提供 --agent opencode/dsh/codex/cline/claude/zcode/workbuddy/pi/chatgpt/qoder/trea/funide"))
        if not sys.stdin.isatty():
            raise CliError(_i18n_exc("agent.non_tty_agent_required", "当前不是交互终端；请显式提供 --agent"))
        requested = _prompt_for_hosts(hosts, root)
    if "all" in requested:
        available_names = [str(item["name"]) for item in hosts if item["installed"]]
        if not available_names and not args.allow_missing:
            raise CliError(_i18n_exc(
                "plugin.no_agent_detected",
                "没有检测到已安装的 AI Agent；请安装 OpenCode/Codex/Claude/ZCode/Pi/ChatGPT，"
                "或显式指定 --agent",
            ))
        if not available_names and args.allow_missing:
            available_names = [str(item["name"]) for item in hosts]
        expanded = []
        for name in requested:
            if name == "all":
                for av in available_names:
                    if av not in expanded:
                        expanded.append(av)
            else:
                if name not in expanded:
                    expanded.append(name)
        requested = expanded
    selected = []
    seen_adapters = set()
    for name in requested:
        try:
            adapter = get_adapter(name)
        except ValueError as exc:
            raise CliError(str(exc)) from exc
        if adapter.name in seen_adapters:
            continue
        seen_adapters.add(adapter.name)
        detected = next(item for item in hosts if item["name"] == adapter.name)
        if not detected["installed"] and not args.allow_missing:
            raise CliError(_i18n_exc("agent.adapter_missing_allow_missing", f"未检测到 {adapter.label}；如仍要写入项目入口，请加 --allow-missing", label=adapter.label))
        selected.append(adapter)
    if progress is not None:
        progress.update(10, "已确认安装目标")

    # Validate an existing host manifest before any resource or runtime writes;
    # a corrupt manifest must never cause a partial installation and then fail.
    existing_record = root / ".bugx" / "hosts.json"
    if existing_record.exists() or existing_record.is_symlink():
        _merge_host_records(existing_record, [])

    # Validate host catalogs or prerequisites before shared runtime writes so
    # an invalid or conflicting catalog cannot leave a partial install.
    for adapter in selected:
        adapter.pre_validate(root)
    if any(adapter.supports(CAP_PROJECT_MCP_CONFIG) for adapter in selected):
        _preflight_opencode_mcp_config(root)
    if progress is not None:
        progress.update(15, "已完成配置校验")

    root.mkdir(parents=True, exist_ok=True)
    source = asset_root()
    # DOC-004 Layout 2：项目不再承载可执行 core。旧项目必须显式选择
    # 破坏性迁移；迁移会先转移运行态/项目数据，再把旧 core 移出活动布局。
    removed_legacy_core = _require_global_only_layout(root)
    if progress is not None:
        progress.update(20, "正在安装共享引擎")
    engine = global_engine_root()
    # 版本漂移必须在替换之前采样：_stage_engine 会用当前 CLI 版本重新盖章
    # metadata.txt，安装之后两个版本必然相等，漂移就再也观察不到了。
    prior_engine_version = _read_engine_version(engine)
    # F-RSI-20261110-017：非 force 的降级拒绝（守卫在 _install_engine 内）
    # 不再让整个 install 失败——宿主入口装配与引擎 swap 是两件事，宿主轮
    # 重装入口时全局引擎已比快照源新是常态，跳过 swap 继续装配即可。
    try:
        copied, skipped = _install_engine(source, args.force)
    except CliError as exc:
        if "拒绝降级全局引擎" in str(exc) and not args.force:
            print(f"[bugx] ⚠ {exc}（引擎保持现状，继续装配宿主入口）")
            copied, skipped = 0, 0
        else:
            raise
    # 引擎换血后热重载 i18n：宿主镜像渲染必须读到新 staged locale（防旧词典）
    refresh_i18n_after_engine_swap()
    if progress is not None:
        progress.update(40, "共享引擎安装完成")
    # 引擎没被换掉 ⇔ 版本仍是替换前的那个（_install_engine 可能因「已安装引擎
    # 更新且未 --force」而跳过），此时不能宣称「已回退」。
    _warn_engine_version_drift(
        prior_engine_version,
        replaced=_read_engine_version(engine) != prior_engine_version,
    )
    _ensure_global_cli_symlinks()

    _layout_module(root).initialize(root, bugx_version=__version__)
    _ensure_project_readme(root, source)
    # The common runtime is intentionally installed once even when the selected
    # host is Codex or Claude; every host entry points to this shared protocol.
    _initialize_lesson_ledger(root, source)
    _initialize_project_knowledge_snapshots(root, source)
    _initialize_life_state(root)
    if progress is not None:
        progress.update(60, "项目运行态初始化完成")
    if removed_legacy_core:
        print(f"[bugx] 已直接删除旧项目引擎入口：{removed_legacy_core}")
    selected_mcp_names = targeted_mcp_names(root)
    optional_mcp_names = selected_mcp_names & {"playwright", "postmcp"}
    provisioned_optional_mcps = provision_targeted_mcp_runtime(root, optional_mcp_names)
    opencode_config_status: str | None = None
    opencode_config_backup: Path | None = None
    host_mcp_configs: dict[str, dict[str, object]] = {}
    dsh_patch_status: str | None = None
    dsh_ui_plugin: dict[str, object] | None = None
    if any(adapter.supports(CAP_PROJECT_MCP_CONFIG) for adapter in selected):
        opencode_config_status, opencode_config_backup = _ensure_opencode_config(
            root, selected=selected_mcp_names,
        )
    if any(adapter.supports(CAP_PROJECT_PATCH_OVERLAY) for adapter in selected):
        dsh_patch_status, dsh_ui_plugin = _ensure_dsh_patch(root)
    installed = []
    host_copied = host_skipped = 0
    pi_extension_copied = pi_extension_skipped = 0
    chatgpt_plugin: dict[str, object] | None = None
    host_backups: list[Path] = []
    zcode_legacy_swept: list[str] = []
    workbuddy_entry_content: str | None = None
    workbuddy_record: dict[str, object] | None = None
    workbuddy_connector: dict[str, object] | None = None
    workbuddy_custom_expert: dict[str, object] | None = None
    project_descriptor = global_engine_root() / "bugx.md"
    for adapter in selected:
        extension_backups: list[Path] = []
        if adapter.entry_mode == ENTRY_PLUGIN_CATALOG:
            chatgpt_plugin = _ensure_chatgpt_plugin(
                root, source, locale=deployment_locale,
            )
            host_copied += int(chatgpt_plugin["copied"])
            host_skipped += int(chatgpt_plugin["skipped"])
            installed.append({
                "name": adapter.name,
                "path": adapter.install_path,
                "kind": adapter.install_kind,
                "components": ["plugin", "skill", "marketplace"],
                "plugin_path": chatgpt_plugin["plugin_path"],
                "marketplace_path": chatgpt_plugin["marketplace_path"],
                "locale": deployment_locale,
                "status": chatgpt_plugin["status"],
                "bugx_build_id": _installed_engine_build_id(),
                "backups": chatgpt_plugin["backups"],
                "files": _records_for_paths(root, [
                    root / "plugins" / CHATGPT_PLUGIN_NAME / ".codex-plugin" / "plugin.json",
                    root / "plugins" / CHATGPT_PLUGIN_NAME / "assets" / "bugx-avatar-512.png",
                    root / "plugins" / CHATGPT_PLUGIN_NAME / "skills" / "bugx" / "SKILL.md",
                ]),
            })
            continue
        if adapter.entry_mode == ENTRY_SKILL_PAYLOAD:
            entry_content = add_locale_marker(
                (pi_skill_root() / "SKILL.md").read_text(encoding="utf-8"),
                deployment_locale,
            )
        elif adapter.entry_mode == ENTRY_SHARED_SYMLINK:
            # 唯一实体原则：入口直接以相对符号链接指向 .bugx 载荷，
            # 不再复制整份协议（历史实体副本在链接函数内备份迁移）。
            _migrate_legacy_opencode_agent_entry(root)
            entry_content = None
        else:
            entry_content = adapter.render(project_descriptor, locale=deployment_locale)
        if entry_content is None:
            copied_count, skipped_count, backup = _link_opencode_agent_entry(root)
            worker_copied, worker_skipped, _ = _link_opencode_worker_entry(root)
            copied_count += worker_copied
            skipped_count += worker_skipped
        elif adapter.entry_mode == ENTRY_USER_REGISTRATION:
            # 0.7.22：主入口收敛为仅用户级注册（storageRoot/agents 全局
            # 注册面）。桌面端 Settings → Subagents 以名字合并各作用域且
            # 项目级同名副本胜出，双写会把用户级条目遮蔽出"用户级"列表；
            # 项目内不再写 .zcode/agents/bugx.md，旧版本遗留副本按模板
            # 比对清扫（内容有出入则保留并如实报告）。
            zcode_swept, sweep_backups = _sweep_legacy_zcode_project_entries(
                root, entry_content,
            )
            zcode_legacy_swept.extend(zcode_swept)
            host_backups.extend(sweep_backups)
            copied_count = skipped_count = 0
            backup = None
        elif adapter.entry_mode == ENTRY_MULTI_PATH:
            copied_count = skipped_count = 0
            backup = None
            for relative in adapter.required_paths:
                copied_item, skipped_item, backup_item = _write_host_entry(
                    root, root / relative, entry_content, args.force, adapter.name,
                )
                copied_count += copied_item
                skipped_count += skipped_item
                if backup_item is not None:
                    host_backups.append(backup_item)
            workbuddy_entry_content = entry_content
        else:
            copied_count, skipped_count, backup = _write_host_entry(
                root, adapter.target(root), entry_content, args.force, adapter.name,
            )
            if adapter.supports(CAP_WORKSPACE_MIRROR_VARIANTS):
                for extra_dir in (".trae", ".trae-cn"):
                    if (root / extra_dir).is_dir():
                        m_copied, m_skipped, m_backup = _write_host_entry(
                            root, root / extra_dir / "agents" / "bugx.md", entry_content, args.force, adapter.name,
                        )
                        copied_count += m_copied
                        skipped_count += m_skipped
                        if m_backup is not None:
                            host_backups.append(m_backup)
        for legacy_rel in adapter.legacy_paths:
            # DOC-169：声明式遗留路径清扫（如 codex 旧 .codex/skills）；
            # 模板一致才删，用户改动保留。
            _sweep_legacy_skill_entry(root, legacy_rel, entry_content, adapter.name)
        host_copied += copied_count
        host_skipped += skipped_count
        if backup is not None:
            host_backups.append(backup)
        if adapter.supports(CAP_EXTRA_TRACKED_PATHS):
            for rel in adapter.required_paths:
                sub_content = adapter.render_required_entry(
                    rel, project_descriptor, locale=deployment_locale,
                )
                if sub_content is not None:
                    c, s, b = _write_host_entry(
                        root, root / rel, sub_content, args.force, adapter.name,
                    )
                    host_copied += c
                    host_skipped += s
                    if b is not None:
                        host_backups.append(b)
                    mirror_root = engine_mirror_root(adapter.name)
                    entry_dir = engine_worker_entry_dir(adapter.name)
                    if (
                        adapter.supports(CAP_WORKSPACE_MIRROR)
                        and mirror_root and entry_dir
                        and (root / mirror_root).is_dir()
                        and rel.startswith(entry_dir + "/")
                    ):
                        roo_rel = f"{mirror_root}/agents/" + rel.removeprefix(entry_dir + "/")
                        _write_host_entry(
                            root, root / roo_rel, sub_content, args.force, adapter.name,
                        )
        zcode_plugin: dict[str, object] | None = None
        zcode_registration_warning: str | None = None
        if adapter.supports(CAP_DISTRIBUTION_PLUGIN):
            # ZCode 双产物：项目级 agent 入口之外，同时生成可分发的本地
            # 插件包（.zcode-plugin 清单 + agents 目录），供用户级跨项目启用。
            zcode_plugin = _ensure_zcode_plugin(root, source, locale=deployment_locale)
            host_copied += int(zcode_plugin["copied"])
            host_skipped += int(zcode_plugin["skipped"])
            host_backups.extend(zcode_plugin["backups"])
            # 用户级全局注册（0.10.14）：插件实体发布到 ~/.bugx/zcode-plugin，
            # plugins.dirs 只登记用户目录路径；旧的项目内注册（包括指向源码
            # 仓库的）在本次重写中识别并迁移，避免宿主加载到工作副本。
            try:
                from .plugin_zcode import (
                    _upsert_zcode_plugin_dirs,
                    _write_zcode_global_plugin,
                )

                _write_zcode_global_plugin(root, source, locale=deployment_locale)
                _upsert_zcode_plugin_dirs(register=None, remove=False)
            except Exception as exc:
                zcode_registration_warning = f"{type(exc).__name__}: {exc}"
        record = {
            "name": adapter.name,
            "path": str(adapter.target(root).relative_to(root)),
            "kind": adapter.install_kind,
            "components": list(adapter.installed_components),
            "locale": deployment_locale,
            "bugx_build_id": _installed_engine_build_id(),
        }
        if adapter.supports(CAP_EXTENSION_PAYLOAD):
            pi_extension_copied, pi_extension_skipped, extension_backups = _copy_pi_extension(
                root, args.force,
            )
            host_backups.extend(extension_backups)
            record["components"] = ["skill", "extension"]
            record["extension_path"] = ".pi/extensions/bugx/index.ts"
        if adapter.supports(CAP_DISTRIBUTION_PLUGIN):
            # 主入口不再落项目级（见上），组件记录只保留插件镜像；用户级
            # 注册是全局层产物，不在项目 hosts.json 记录内（卸载保留并
            # 报告，全局清理走 uninstall --self）。
            record["components"] = ["plugin"]
            record["path"] = str(zcode_plugin["manifest_path"])
            if zcode_registration_warning:
                record["registration_warning"] = zcode_registration_warning
        if backup is not None or (
            adapter.supports(CAP_EXTENSION_PAYLOAD) and extension_backups
        ):
            record["backups"] = [
                str(path.relative_to(root)) for path in host_backups
                if path.parent.name == adapter.name
            ]
        tracked_paths = (
            [] if adapter.entry_mode == ENTRY_USER_REGISTRATION else [adapter.target(root)]
        )
        if adapter.entry_mode == ENTRY_MULTI_PATH:
            tracked_paths = [root / relative for relative in adapter.required_paths]
        if adapter.entry_mode == ENTRY_SHARED_SYMLINK:
            record["entry_symlink"] = adapter.install_path
        if adapter.supports(CAP_EXTRA_TRACKED_PATHS):
            tracked_paths.extend(root / value for value in adapter.required_paths)
            mirror_root = engine_mirror_root(adapter.name)
            entry_dir = engine_worker_entry_dir(adapter.name)
            if adapter.supports(CAP_WORKSPACE_MIRROR) and mirror_root and (root / mirror_root).is_dir():
                for value in adapter.required_paths:
                    if entry_dir and value.startswith(entry_dir + "/"):
                        tracked_paths.append(
                            root / (f"{mirror_root}/agents/" + value.removeprefix(entry_dir + "/"))
                        )
        if adapter.supports(CAP_WORKSPACE_MIRROR_VARIANTS):
            for extra_dir in (".trae", ".trae-cn"):
                if (root / extra_dir).is_dir():
                    tracked_paths.append(root / extra_dir / "agents" / "bugx.md")
        if adapter.supports(CAP_DISTRIBUTION_PLUGIN):
            # P3：assert 会被 python -O 剥离，改成显式失败路径。
            if zcode_plugin is None:
                raise CliError(_i18n_exc("agent.zcode_bundle_missing_result", "zcode 插件包生成失败：缺少插件安装结果，拒绝写入 hosts 记录"))
            tracked_paths.extend([
                root / str(zcode_plugin["manifest_path"]),
                root / str(zcode_plugin["agent_path"]),
                root / str(zcode_plugin["worker_agent_path"]),
            ])
        if adapter.entry_mode == ENTRY_MULTI_PATH:
            # 用户级 Skill 是全局层产物（与 zcode 用户级 agents 同语义），
            # 跨项目共享，不进入项目哈希清单；卸载只在模板比对一致时删除。
            workbuddy_record = record
        record["files"] = _records_for_paths(root, tracked_paths)
        installed.append(record)
    if progress is not None:
        progress.update(78, "宿主入口安装完成")
    # Cline and ZCode do not consume OpenCode's project configuration. Write
    # their native project/workspace MCP containers only after project
    # entries/plugins have succeeded. MCP configuration stays in the project.
    host_mcp_changes: list[tuple[Path, Path | None, bool]] = []



    #: 用户级注册结果按宿主名归档（报告键由宿主名派生，不再逐个写变量）。
    user_agents_by_host: dict[str, dict[str, object]] = {}

    workbuddy_user_skill: dict[str, object] | None = None
    opencode_user_skill: dict[str, object] | None = None
    # 需要宿主原生 MCP 容器的宿主按注册表顺序处理，保证写入与回滚顺序稳定。
    selected_names = {adapter.name for adapter in selected}
    native_mcp_adapters = [
        adapter for adapter in HOST_ADAPTERS
        if adapter.supports(CAP_NATIVE_MCP_CONFIG) and adapter.name in selected_names
    ]
    try:
        for adapter in native_mcp_adapters:
            config_hosts = (adapter.name, "codebuddy") if adapter.entry_mode == ENTRY_MULTI_PATH else (adapter.name,)
            config_paths: list[str] = []
            status = "unchanged"
            path = root / ".bugx" / "state" / "unused"
            backup = None
            # ZCode 的 MCP 注册是用户级全局单例（绝对 --root 指向单个项目）；
            # 重写前采样既有绑定，切换挖掘项目时输出明确的切换提示。
            previous_bound_root = (
                _zcode_bound_project_root()
                if adapter.name == HOST_NAME_ZCODE else None
            )
            for config_host in config_hosts:
                item_status, path, backup, created = write_host_mcp_config(
                    root, config_host, optional_names=provisioned_optional_mcps,
                )
                config_paths.append(str(path))
                if item_status != "unchanged":
                    status = item_status
                    host_mcp_changes.append((path, backup, created))
                    if backup is not None:
                        host_backups.append(backup)
            host_info: dict[str, object] = {
                "status": status,
                "path": str(path),
                "paths": config_paths,
                "backup": str(backup) if backup is not None else None,
                "session_restart_required": status != "unchanged",
            }
            if (
                adapter.name == HOST_NAME_ZCODE
                and status != "unchanged"
                and previous_bound_root is not None
                and previous_bound_root != root
            ):
                host_info["root_switch"] = {
                    "from": str(previous_bound_root),
                    "to": str(root),
                }
            host_mcp_configs[adapter.name] = host_info
            if adapter.supports(CAP_USER_AGENT_REGISTRATION):
                # User-level agent registrations share the install transaction;
                # MCP files remain scoped to this project.
                registered_user_agents = adapter.ensure_user_agents(
                    root, source, locale=deployment_locale,
                )
                if registered_user_agents is not None:
                    user_agents_by_host[adapter.name] = registered_user_agents
                    if adapter.name == HOST_NAME_ZCODE:
                        # ZCode 专属：用户级使用手册 Skill 与 agents 同事务部署
                        # （全局层，跨项目可见；内容随 wheel 版本演进）。
                        zcode_user_skill = _ensure_zcode_user_skill(root, source)
                        user_agents_by_host[adapter.name]["user_skill"] = (
                            zcode_user_skill
                        )
                        for item in zcode_user_skill["written"]:
                            if item["status"] == "unchanged":
                                continue
                            host_mcp_changes.append((
                                Path(str(item["path"])),
                                Path(str(item["backup"])) if item["backup"] else None,
                                bool(item["created"]),
                            ))
                        host_backups.extend(zcode_user_skill["backups"])
                    for item in registered_user_agents["written"]:

                        if item["status"] == "unchanged":
                            continue
                        host_mcp_changes.append((
                            Path(str(item["path"])),
                            Path(str(item["backup"])) if item["backup"] else None,
                            bool(item["created"]),
                        ))
                    host_backups.extend(registered_user_agents["backups"])
        for adapter in selected:
            if adapter.supports(CAP_GLOBAL_AGENT_LINK):
                # global-agent-link 宿主（现仅 opencode）：用户级使用手册 Skill
                # 与 agents 全局软链同事务部署（~/.config/opencode/skills/，
                # 跨项目可见）。不挂在 CAP_USER_AGENT_REGISTRATION 块内——
                # 该宿主 capabilities 不含该标签，挂在里面永不执行（实测踩坑）；
                # 分支用能力查询，遵守 DES-14 宿主字面量守护。
                # 回执经顶层 opencode_user_skill 专键承载（与 workbuddy_user_skill
                # 同形态）；不挂 user_agents_by_host——该结构供统一汇报循环遍历
                # written，塞入 {"user_skill": …} 会 KeyError（实测踩坑）。
                opencode_user_skill = _ensure_opencode_user_skill(
                    root, source, backup_label=adapter.name,
                )
                for item in opencode_user_skill["written"]:
                    if item["status"] == "unchanged":
                        host_skipped += 1
                        continue
                    host_copied += 1
                    host_mcp_changes.append((
                        Path(str(item["path"])),
                        Path(str(item["backup"])) if item["backup"] else None,
                        bool(item["created"]),
                    ))
                host_backups.extend(opencode_user_skill["backups"])
        if any(adapter.supports(CAP_USER_LEVEL_ARTIFACT) for adapter in selected):
            # 默认用户级履约：WorkBuddy 桌面端从 <configDir>/skills 发现
            # 用户级 Skill，任何项目的会话都能看到 BugX；项目级双入口
            # 仍保留。全局层写入与 zcode 用户级 agents 同一事务回滚。
            workbuddy_user_skill = _ensure_workbuddy_user_skill(
                root, workbuddy_entry_content or "",
            )
            for item in workbuddy_user_skill["written"]:
                if item["status"] == "unchanged":
                    host_skipped += 1
                    continue
                host_copied += 1
                host_mcp_changes.append((
                    Path(str(item["path"])),
                    Path(str(item["backup"])) if item["backup"] else None,
                    bool(item["created"]),
                ))
            host_backups.extend(workbuddy_user_skill["backups"])
            workbuddy_connector = _ensure_workbuddy_connector(
                root, workbuddy_entry_content or "",
            )
            for item in workbuddy_connector["written"]:
                if item["status"] == "unchanged":
                    host_skipped += 1
                    continue
                host_copied += 1
                host_mcp_changes.append((
                    Path(str(item["path"])),
                    Path(str(item["backup"])) if item["backup"] else None,
                    bool(item["created"]),
                ))
            host_backups.extend(workbuddy_connector["backups"])
            workbuddy_custom_expert = _ensure_workbuddy_custom_expert(
                root, workbuddy_entry_content or "",
            )
            for item in workbuddy_custom_expert["written"]:
                if item["status"] == "unchanged":
                    host_skipped += 1
                    continue
                host_copied += 1
                host_mcp_changes.append((
                    Path(str(item["path"])),
                    Path(str(item["backup"])) if item["backup"] else None,
                    bool(item["created"]),
                ))
            host_backups.extend(workbuddy_custom_expert["backups"])
            for cfg_root in _workbuddy_config_roots():
                if cfg_root.is_dir():
                    if cfg_root != workbuddy_user_skill_root():
                        _ensure_workbuddy_custom_expert(root, workbuddy_entry_content or "", config_root=cfg_root)
            if workbuddy_record is not None:
                written_paths = [
                    str(item["path"]) for item in workbuddy_user_skill["written"]
                ]
                if written_paths:
                    workbuddy_record["user_skill"] = written_paths[0]
                workbuddy_record["connector_package"] = str(
                    workbuddy_connector["root"]
                )
                workbuddy_record["custom_expert"] = str(
                    workbuddy_custom_expert["root"]
                )
                workbuddy_record["components"] = ["skill", "connector"]
                workbuddy_record["files"] = _records_for_paths(
                    root,
                    [root / relative for relative in WORKBUDDY_PROJECT_PATHS]
                    + [Path(str(path)) for path in workbuddy_connector["paths"]],
                )
            # DOC-181 方案 1：自动探测并同步 WorkBuddy 私有 Python 虚拟环境
            workbuddy_venv_synced, workbuddy_venv_msg = _sync_workbuddy_private_venv(
                source, target_version=__version__,
            )
            if workbuddy_record is not None:
                workbuddy_record["private_venv_synced"] = workbuddy_venv_synced
                workbuddy_record["private_venv_message"] = workbuddy_venv_msg
    except BaseException:
        for path, backup, created in reversed(host_mcp_changes):
            _restore_host_mcp_config_change(path, backup, created)
        raise
    opencode_global: dict | None = None
    if any(adapter.supports(CAP_GLOBAL_AGENT_LINK) for adapter in selected):
        # 0.5.38（DOC-004）：agent 入口回到全局注册——入口指向全局引擎
        # 资源，任何项目目录下启动 OpenCode 均可发现 @bugx。
        # 运行态（pollings/知识库/报告）仍按项目隔离；不再清理全局链接。
        global_copied, global_backups = _register_global_opencode_agent(root)
        opencode_global = {"agents_dir": str(_global_opencode_agents_dir())}
        if global_backups:
            opencode_global["backups"] = [str(path) for path in global_backups]
    if any(adapter.supports(CAP_GLOBAL_SKILL_LINK) for adapter in selected):
        _link_global_pi_skill(root, locale=deployment_locale)
        _self_check_pi_skill_registered(root)
    if progress is not None:
        progress.update(90, "宿主配置同步完成")
    record_dir = root / ".bugx"
    record_dir.mkdir(parents=True, exist_ok=True)
    record = record_dir / "hosts.json"
    merged_hosts = _merge_host_records(record, installed)
    _write_private_json(record, {
        "schema_version": 1,
        "bugx_version": __version__,
        "bugx_build_id": _installed_engine_build_id(),
        "engine_bundle_sha256": _engine_manifest_payload(
            __version__, _engine_file_manifest(engine),
        )["bundle_sha256"],
        "installed": merged_hosts,
        # Layout 2 has no project copy to record or uninstall.  The executable
        # shared runtime is validated independently at global_engine_root().
        "shared_files": [],
        "shared_runtime": "global-engine",
        "execution": "delegated-to-host",
        "complete_bundles": True,
    })
    if progress is not None:
        progress.update(98, "安装记录已写入")
        progress.finish()
    mcp_recommendations = _mcp_recommendations(root)
    payload = {
        "status": "INSTALLED", "root": str(root),
        "hosts": installed, "shared_copied": copied, "shared_skipped": skipped,
        "host_copied": host_copied, "host_skipped": host_skipped,
        "extension_copied": pi_extension_copied,
        "extension_skipped": pi_extension_skipped,
        "host_backups": [str(path.relative_to(root)) for path in host_backups],
        "opencode_config": opencode_config_status,
        "opencode_session_restart_required": opencode_config_status in {
            "created", "merged",
        },
        "opencode_config_backup": (
            str(opencode_config_backup.relative_to(root))
            if opencode_config_backup is not None else None
        ),
        "host_mcp_configs": host_mcp_configs,
        **{
            f"{host_name}_user_agents": payload
            for host_name, payload in user_agents_by_host.items()
        },
        "zcode_legacy_swept": zcode_legacy_swept,
        "workbuddy_user_skill": workbuddy_user_skill,
        "opencode_user_skill": opencode_user_skill,

        "workbuddy_connector": workbuddy_connector,
        "workbuddy_custom_expert": workbuddy_custom_expert,
        "dsh_ui_plugin": dsh_ui_plugin,
        "chatgpt_plugin": chatgpt_plugin,
        "opencode_global": opencode_global,
        "execution": "none",
        "mcp_recommendations": mcp_recommendations,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    else:
        names = ", ".join(item["name"] for item in installed)
        print(f"[bugx] 已安装到 {names}；CLI 未启动任何 AI Agent")
        print(f"  共享协议：写入 {copied}，保留 {skipped}")
        print(f"  宿主入口：写入 {host_copied}，保留 {host_skipped}")
        if pi_extension_copied or pi_extension_skipped:
            print(
                f"  Pi 完整适配包：写入 {pi_extension_copied}，"
                f"保留 {pi_extension_skipped}"
            )
        if chatgpt_plugin is not None:
            print(
                f"  ChatGPT Plugin：{chatgpt_plugin['status']}；"
                f"写入 {chatgpt_plugin['copied']}，保留 {chatgpt_plugin['skipped']}"
            )
            cleanup = chatgpt_plugin.get("legacy_cleanup")
            if isinstance(cleanup, dict) and cleanup.get("removed"):
                removed = ", ".join(str(item) for item in cleanup["removed"])
                print(f"    已迁移旧 BugX Marketplace：{removed}")
            if chatgpt_plugin.get("registration_warning"):
                print(f"    注意：{chatgpt_plugin['registration_warning']}")
            if chatgpt_plugin.get("shared_skill_warning"):
                print(f"    注意：{chatgpt_plugin['shared_skill_warning']}")
            print(f"    下一步：{chatgpt_plugin['next_step']}")
        if opencode_config_status == "created":
            print(
                f"  OpenCode 配置：创建 {CONFIG_NAME}"
                "（外部 MCP 统一经由离线 runtime launcher 启动）"
            )
        elif opencode_config_status == "merged":
            print(
                f"  OpenCode 配置：已备份并补齐 MCP 到离线 runtime launcher（备份："
                f"{opencode_config_backup.relative_to(root)}）"
            )
        elif opencode_config_status == "unchanged":
            print("  OpenCode 配置：已是离线 runtime launcher 版本，保持不变")
        for native_adapter in native_mcp_adapters:
            host_name = native_adapter.name
            info = host_mcp_configs.get(host_name)
            if not info:
                continue
            status = str(info["status"])
            path = str(info["path"])
            if status == "created":
                print(f"  {host_name} MCP 配置：已创建 {path}（host/worker 双通道）")
            elif status == "unchanged":
                print(f"  {host_name} MCP 配置：已是最新双通道配置，保持不变（{path}）")
            else:
                print(f"  {host_name} MCP 配置：已备份并{status}双通道（{path}）")
                switched = info.get("root_switch")
                if isinstance(switched, dict):
                    print(
                        f"    已切换项目根：{switched.get('from')} → "
                        f"{switched.get('to')}（重启宿主会话生效）"
                    )
        reported_user_agents: set[str] = set()
        for item in installed:
            warning = item.get("registration_warning")
            if warning:
                print(f"  注意：{item['name']} 用户级插件注册失败：{warning}")
        if (zcode_user_agents := user_agents_by_host.get(HOST_NAME_ZCODE)) is not None:
            reported_user_agents.add(HOST_NAME_ZCODE)
            changed = [
                item for item in zcode_user_agents["written"]
                if item["status"] != "unchanged"
            ]
            if changed:
                names = "、".join(Path(str(item["path"])).name for item in changed)
                print(
                    f"  ZCode 用户级子智能体：已写入 {names}"
                    f"（{zcode_user_agents['dir']}，Settings → Subagents 可见）"
                )
            else:
                print(
                    f"  ZCode 用户级子智能体：已是最新，保持不变"
                    f"（{zcode_user_agents['dir']}）"
                )
            zcode_user_skill = zcode_user_agents.get("user_skill")
            if isinstance(zcode_user_skill, dict):
                skill_changed = [
                    item for item in zcode_user_skill["written"]
                    if item["status"] != "unchanged"
                ]
                if skill_changed:
                    print(
                        f"  ZCode 用户级 Skill（bugx-zcode 使用手册）：已部署"
                        f"（{zcode_user_skill['dir']}，新环境即获全部实战流程）"
                    )
                else:
                    print(
                        f"  ZCode 用户级 Skill：已是最新，保持不变"
                        f"（{zcode_user_skill['dir']}）"
                    )
        if (claude_user_agents := user_agents_by_host.get(HOST_NAME_CLAUDE)) is not None:
            reported_user_agents.add(HOST_NAME_CLAUDE)
            agents_dir_str = str(claude_user_agents["dir"])
            changed = [
                item for item in claude_user_agents["written"]
                if item["status"] != "unchanged"
                and str(item["path"]).startswith(agents_dir_str)
            ]
            if changed:
                names = "、".join(Path(str(item["path"])).name for item in changed)
                print(
                    f"  Claude Code 用户级智能体：已写入 {names}"
                    f"（{claude_user_agents['dir']}，全局 claude 可见）"
                )
            else:
                print(
                    f"  Claude Code 用户级智能体：已是最新，保持不变"
                    f"（{claude_user_agents['dir']}）"
                )
            skills_res = claude_user_agents.get("skills")
            if isinstance(skills_res, dict) and skills_res.get("user"):
                print(f"  Claude 用户级 Skill：已配置（{skills_res['user']}）")
            host_manual = skills_res.get("host_manual") if isinstance(skills_res, dict) else None
            if isinstance(host_manual, dict):
                skill_changed = [
                    item for item in host_manual.get("written", ())
                    if item["status"] != "unchanged"
                ]
                if skill_changed:
                    print(
                        "  Claude 用户级 Skill（bugx-claude 使用手册）：已部署"
                        f"（{host_manual['dir']}，含强制阅读清单与实证坑位）"
                    )
                else:
                    print(
                        "  Claude 用户级 Skill（bugx-claude 使用手册）：已是最新，保持不变"
                        f"（{host_manual['dir']}）"
                    )
            packages_res = claude_user_agents.get("packages")
            if isinstance(packages_res, dict) and packages_res.get("dist"):
                print(f"  Claude Desktop 独立安装包：已生成 bugx.plugin 与 bugx.skill（{packages_res['dist']}）")
            plugins_res = claude_user_agents.get("plugins")
            if isinstance(plugins_res, dict) and plugins_res.get("registered"):
                print(f"  Claude Desktop 插件系统：已就绪并启用 bugx@bugx（版本 0.1.0）")
        if (qoder_user_agents := user_agents_by_host.get(HOST_NAME_QODER)) is not None:
            reported_user_agents.add(HOST_NAME_QODER)
            changed = [
                item for item in qoder_user_agents["written"]
                if item["status"] != "unchanged"
            ]
            if changed:
                names = "、".join(Path(str(item["path"])).name for item in changed)
                print(
                    f"  Qoder 用户级资产：已写入 {names}"
                    f"（{qoder_user_agents['dir']}，全局 Qoder 可见）"
                )
            else:
                print(
                    f"  Qoder 用户级资产：已是最新，保持不变"
                    f"（{qoder_user_agents['dir']}）"
                )
        # 其余宿主的用户级注册结果统一汇报（标签来自插件/组件表，不写宿主名）。
        for host_name, payload in user_agents_by_host.items():
            if host_name in reported_user_agents:
                continue
            label = engine_host_label(host_name)
            changed = [
                item for item in payload["written"]
                if item["status"] != "unchanged"
            ]
            if changed:
                names = "、".join(Path(str(item["path"])).name for item in changed)
                print(
                    f"  {label} 用户级智能体：已写入 {names}"
                    f"（{payload['dir']}，{_user_agent_visibility(host_name)}）"
                )
            else:
                print(
                    f"  {label} 用户级智能体：已是最新，保持不变"
                    f"（{payload['dir']}）"
                )

        if zcode_legacy_swept:
            print(
                "  ZCode 项目级遗留副本：已按模板比对清扫 "
                + "、".join(zcode_legacy_swept)
                + "（子智能体改为仅用户级注册，避免同名遮蔽 Settings 用户级列表）"
            )
        if workbuddy_user_skill is not None:
            changed = [
                item for item in workbuddy_user_skill["written"]
                if item["status"] != "unchanged"
            ]
            if changed:
                print(
                    f"  WorkBuddy 用户级 Skill：已写入 {workbuddy_user_skill['root']}"
                    "/skills/bugx/SKILL.md（所有项目的会话均可发现）"
                )
            else:
                print(
                    f"  WorkBuddy 用户级 Skill：已是最新，保持不变"
                    f"（{workbuddy_user_skill['root']}/skills/bugx/SKILL.md）"
                )
        if workbuddy_connector is not None:
            print(
                f"  WorkBuddy Connector：已生成 {workbuddy_connector['root']}"
                f"（含 icon.png；交付类型 skill-only）"
            )
        if workbuddy_custom_expert is not None:
            print(
                f"  WorkBuddy 自定义专家：已注册 {workbuddy_custom_expert['root']}"
                "（在专家中心可见）"
            )
        if workbuddy_record and workbuddy_record.get("private_venv_synced"):
            print(f"  WorkBuddy 私有运行态：{workbuddy_record.get('private_venv_message')}")
        if dsh_ui_plugin is not None:
            status = str(dsh_ui_plugin["status"])
            if status == "registered":
                print(
                    f"  DSH UI 插件：已注册到 web profile（{dsh_ui_plugin['root']}）"
                )
            else:
                print(
                    f"  DSH UI 插件：已暂存 {dsh_ui_plugin['root']}（{dsh_ui_plugin['detail']}）"
                )
            print("  DSH Web 启动：dsh web（按当前 Session 工作区自动绑定；未选目录会明确提示）")
        if opencode_config_status in {"created", "merged"}:
            print("  ⚠ OpenCode MCP 配置已变化；现有会话通常不会热加载。启动 BugX 时会先主动探活，"
                  "探活未命中时再提示重启 OpenCode 会话")
        _print_mcp_recommendations(mcp_recommendations)
    return 0



def _initialize_life_state(root: Path, *, identity_root: Path | None = None) -> bool:
    """Create the canonical first-run life state without copying maintainer runtime data."""
    layout = _layout_module(root)
    active = layout.require(root)
    life = active.life
    if life.is_file():
        return False
    verifier = _script(root, "verify_life.py")
    try:
        process = subprocess.run(
            [sys.executable, str(verifier), "reset"], cwd=root,
            capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment({
                "BUGX_PROJECT_ROOT": str(root.resolve()),
                **({"BUGX_IDENTITY_ROOT": str(Path(identity_root).resolve())} if identity_root else {}),
            }),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(_i18n_exc("agent.life_state_init_failed", f"初始化寿命状态失败: {exc}", exc=exc)) from exc
    if process.returncode != 0 or not life.is_file():
        detail = (process.stderr or process.stdout).strip()[:500]
        raise CliError(_i18n_exc("agent.life_state_file_missing", f"初始化寿命状态失败: {detail or 'verify_life 未生成状态文件'}", detail=detail or 'verify_life 未生成状态文件'))
    try:
        payload = json.loads(life.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc("agent.life_state_file_invalid", f"初始化寿命状态失败：生成文件无效: {exc}", exc=exc)) from exc
    expected = {
        "life": 1, "found_total": 0, "round": 1,
        "rounds_completed": 0, "alive": True, "mode": None, "history": [],
    }
    if payload != expected:
        raise CliError(_i18n_exc("agent.life_state_not_initial", "初始化寿命状态失败：verify_life 生成了非初始状态"))
    return True


def _backup_opencode_config(root: Path, source: Path) -> Path:
    """Back up an existing project OpenCode config before changing it."""
    backup_dir = root.resolve() / ".bugx" / "backups" / "opencode"
    _assert_no_symlink_components(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"opencode-{time.time_ns()}.json"
    shutil.copy2(source, backup)
    backup.chmod(0o600)
    return backup


# ── DSH 集成实现 ────────────────────────────────────────────────────────────
# 本段出现的 "dsh" 字面量是宿主**专属集成实现的数据**（可执行名、npm 包路径、
# 用户数据目录名、备份命名空间），不是「按宿主名分派」：这些代码只会在
# 已选定 dsh 宿主时执行，没有可参数化的等价能力。安装流程的分派已改为
# adapter.supports(...) 能力查询。
def _dsh_home() -> Path:
    configured = os.environ.get("DSH_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home().resolve() / ".dsh"


def _all_dsh_homes() -> list[Path]:
    """Return all detected DSH home roots (CLI ~/.dsh and macOS DSH Desktop harness)."""
    homes = [_dsh_home()]
    desktop_harness = Path.home().resolve() / "Library" / "Application Support" / "dsh-desktop" / "harness"
    if desktop_harness.is_dir() and desktop_harness not in homes:
        homes.append(desktop_harness)
    return homes


def _discover_dsh_project_root(start: Path) -> Path | None:
    """Find the nearest BugX/Git project without treating ``~/.bugx`` as one."""
    candidate = start.expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / ".bugx" / "manifest.json").is_file():
            return directory
        if (directory / ".git").exists():
            return directory
    return None


def _dsh_project_cwd(root: Path | None) -> Path:
    """Resolve one absolute workspace for the DSH web process."""
    configured = os.environ.get("DSH_CWD", "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_dir():
            raise CliError(f"DSH_CWD 不是可访问目录：{candidate}")
        return candidate

    if root is not None:
        candidate = root.expanduser().resolve()
        if not candidate.is_dir():
            raise CliError(f"DSH 项目目录不存在：{candidate}")
        return candidate

    configured_root = os.environ.get("BUGX_PROJECT_ROOT", "").strip()
    if configured_root:
        candidate = Path(configured_root).expanduser().resolve()
        if candidate.is_dir():
            return candidate

    discovered = _discover_dsh_project_root(Path.cwd())
    if discovered is not None:
        return discovered
    raise CliError(
        "无法自动确定 DSH 项目根；请在项目目录执行 `bugx dsh`，"
        "或显式传入 `bugx dsh --root <项目绝对路径>`"
    )


def command_dsh(args: argparse.Namespace) -> int:
    """Start DSH with a project-bound ``DSH_CWD``."""
    dsh = shutil.which("dsh")
    if not dsh:
        raise CliError("未找到 dsh 命令，请先安装 DeepSeek Harness")
    project_cwd = _dsh_project_cwd(getattr(args, "root", None))
    environment = os.environ.copy()
    environment["DSH_CWD"] = str(project_cwd)
    profile = getattr(args, "profile", "web") or "web"
    dsh_args = list(getattr(args, "dsh_args", []))
    if dsh_args[:1] == ["--"]:
        dsh_args.pop(0)
    command = [dsh, "--profile", profile, *dsh_args]
    try:
        result = subprocess.run(command, cwd=project_cwd, env=environment, check=False)
        if result.returncode != 0:
            print(
                "\n[bugx dsh] 提示：若 DSH 因缺失 preset 插件报错（如 @deepseek-ai/dsh-agent-preset），"
                "可执行 `bugx install --agent dsh --force` 刷新配置，"
                "或使用 `bugx autonomous` / `bugx install` 连接其它受管 AI 宿主。",
                file=sys.stderr,
            )
    except OSError as exc:
        raise CliError(f"启动 dsh 失败：{exc}") from exc
    return int(result.returncode)


def _dsh_standard_preset_candidates() -> list[Path]:
    """Find the installed DSH preset without assuming a Node version or drive.

    ``DSH_HOME`` is the user data directory, not the DSH package checkout.  On
    Windows the package may be installed under any npm prefix (and ``dsh.cmd``
    is only a shim), so derive the package from the executable and its shim
    instead of hard-coding one developer's ``C:``/``D:`` layout.
    """
    relative = Path(
        "config", "agent-presets", "standard", "agent.cordis.yml",
    )
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    for env_name in ("DSH_PACKAGE_ROOT", "DSH_INSTALL_ROOT"):
        configured = os.environ.get(env_name)
        if configured:
            add(Path(configured).expanduser() / relative)

    executable = shutil.which("dsh")
    if executable:
        executable_path = Path(executable)
        try:
            executable_path = executable_path.resolve()
        except OSError:
            pass
        probe_dirs = [executable_path.parent, *executable_path.parents]
        for directory in probe_dirs:
            for package_root in (
                directory / "node_modules" / "@deepseek-ai" / "dsh",
                directory / "lib" / "node_modules" / "@deepseek-ai" / "dsh",
                directory / "@deepseek-ai" / "dsh",
            ):
                add(package_root / relative)

        # npm on Windows commonly exposes a .cmd/.ps1 shim whose body names
        # the real package.  Reading the shim avoids guessing the npm prefix.
        try:
            shim = Path(executable).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            shim = ""
        if shim:
            pattern = re.compile(
                r"([A-Za-z]:[\\/][^\"\r\n]*?node_modules[\\/]"
                r"(?:@deepseek-ai[\\/])?dsh)",
                re.IGNORECASE,
            )
            for raw in pattern.findall(shim):
                add(Path(raw.replace("\\", os.sep)) / relative)

    for entry in os.environ.get("NODE_PATH", "").split(os.pathsep):
        if entry.strip():
            add(Path(entry).expanduser() / "@deepseek-ai" / "dsh" / relative)
    return candidates


def _deploy_dsh_ui_plugin() -> dict[str, object]:
    """Install the DSH bundle into the user web profile when it exists.

    ``dsh.client`` modules are discovered from the web profile's bundle list;
    putting the files in an agent preset alone cannot make a browser sidebar
    icon appear.  A missing web profile is valid for headless-only installs,
    so the package is staged and reported instead of blocking the agent setup.
    """
    source_dir = _integration_dir() / "dsh" / "ui"
    required = ("package.json", "cordis.patch.yml", "index.js", "client.js")
    # ``bugx-mcp``（第三方 @bencibro/bugx-dsh-mcp-client 桥）已停用：它把 MCP 的
    # JSON Schema 当 schemastery spec 编译，实测每次调用抛
    # ``v3Schema.safeParse is not a function``，且自带 serverName 默认值会与官方
    # 通道重名。MCP 三条通道改由 core/hosts/dsh.py 用官方 client 写 profile patch。

    for name in required:
        source = source_dir / name
        if source.is_symlink() or not source.is_file():
            raise CliError(_i18n_exc(
                "agent.dsh_ui_resource_missing",
                f"缺少 DSH UI 适配资源：{source}", source=source,
            ))

    for home in _all_dsh_homes():
        plugin_root = home / "plugins" / "bugx-ui"
        _assert_no_symlink_components(plugin_root)
        plugin_root.mkdir(parents=True, exist_ok=True)
        for name in required:
            _atomic_write_global_file_tx(
                plugin_root / name, (source_dir / name).read_bytes(),
            )


    plugin_root = _dsh_home() / "plugins" / "bugx-ui"
    profile_root = _dsh_home() / "profiles" / "web"
    dsh = shutil.which("dsh")
    if not dsh or not (profile_root / "package.json").is_file():
        return {
            "root": str(plugin_root),
            "mcp_root": None,  # 第三方 MCP bridge 已停用；通道由 profile patch 提供
            "profile": str(profile_root),
            "status": "staged",
            "detail": "未发现可管理的 DSH web profile，保留包供 dsh plugin add 使用",
        }
    try:
        result = subprocess.run(
            [dsh, "plugin", "--profile", "web", "add", str(plugin_root)],
            cwd=profile_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "root": str(plugin_root),
            "mcp_root": None,  # 第三方 MCP bridge 已停用；通道由 profile patch 提供
            "profile": str(profile_root),
            "status": "staged",
            "detail": f"web profile 注册未完成：{exc}",
        }
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "dsh plugin add 失败").strip()
        return {
            "root": str(plugin_root),
            "mcp_root": None,  # 第三方 MCP bridge 已停用；通道由 profile patch 提供
            "profile": str(profile_root),
            "status": "staged",
            "detail": f"web profile 注册未完成：{detail[-500:]}",
        }
    for home in _all_dsh_homes():
        p_manifest = home / "profiles" / "web" / "package.json"
        if p_manifest.is_file():
            try:
                data = json.loads(p_manifest.read_text(encoding="utf-8"))
                bundles = data.setdefault('dsh', {}).setdefault("profile", {}).setdefault("bundles", [])
                deps = data.get("dependencies", {})
                changed = False
                for pkg_name in ("@bencibro/bugx-dsh-ui", "@bencibro/bugx-dsh-mcp-client"):
                    if pkg_name in deps and pkg_name not in bundles:
                        bundles.append(pkg_name)
                        changed = True
                if changed:
                    _atomic_write_global_file_tx(
                        p_manifest,
                        (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                    )
            except Exception:
                pass
    return {
        "root": str(plugin_root),
        "mcp_root": None,  # 第三方 MCP bridge 已停用；通道由 profile patch 提供
        "profile": str(profile_root),
        "status": "registered",
        "detail": "已由 dsh plugin add 纳入 web profile bundle",
    }


def _ensure_dsh_patch(root: Path) -> tuple[str, dict[str, object] | None]:
    """Install or upgrade the BugX integration for DeepSeek Harness (dsh).

    Writes:
      .dsh/patch.yml                         — project patch overlay
      ~/.dsh/.agent-presets/bugx/agent.cordis.yml  — preset composition
      ~/.dsh/.agent-presets/bugx/preset.yml         — preset display metadata
      .dsh/skills/bugx/SKILL.md                     — skill entry
      .dsh/agents/bugx.md                           — agent definition

    The browser UI package is installed separately into the DSH web profile;
    agent presets do not load ``dsh.client`` modules.
    """
    dsh_dir = root.resolve() / ".dsh"
    dsh_dir.mkdir(parents=True, exist_ok=True)

    patch_path = dsh_dir / "patch.yml"
    skill_path = dsh_dir / "skills" / "bugx" / "SKILL.md"
    dsh_skill_path = dsh_dir / "skills" / "bugx-dsh" / "SKILL.md"
    agent_path = dsh_dir / "agents" / "bugx.md"
    patched = False
    any_existing = any(path.exists() for path in (patch_path, skill_path, dsh_skill_path, agent_path))

    # Read bundled templates from integrations/dsh/
    integration_dir = _integration_dir()
    bundled_patch = integration_dir / "dsh" / "dsh-patch.yml"
    bundled_skill = integration_dir / "dsh" / "skills" / "bugx" / "SKILL.md"
    bundled_dsh_skill = integration_dir / "dsh" / "skills" / "bugx-dsh" / "SKILL.md"
    bundled_agent = integration_dir / "dsh" / "agents" / "bugx.md"

    for bundled, target in (
        (bundled_patch, patch_path),
        (bundled_skill, skill_path),
        (bundled_dsh_skill, dsh_skill_path),
        (bundled_agent, agent_path),
    ):
        if not bundled.is_file():
            raise CliError(_i18n_exc("agent.dsh_resource_missing", f"缺少 dsh 适配资源：{bundled}", bundled=bundled))
        copied, _skipped, _backup = _write_host_resource(
            root, target, bundled.read_bytes(), "dsh",
        )
        patched = patched or bool(copied)

    # 项目 patch 也要带官方 MCP overlay：`bugx doctor` 的「DeepSeek Harness
    # MCP 配置」扫的是项目 `.dsh/patch.yml` 与全局预设，不是 profile patch。
    # 只在 profile 层写入会让 install 之后 doctor 仍报「缺少 bugx-supervisor
    # MCP」——修好调用却报告缺失，是更难排查的破窗。
    if patch_path.is_file():
        project_patch = patch_path.read_text(encoding="utf-8")
        with_overlay = _ensure_official_mcp_overlay(project_patch)
        if with_overlay != project_patch:
            _atomic_write_global_file_tx(patch_path, with_overlay.encode("utf-8"))
            patched = True

    # Deploy BugX's agent preset and the browser UI bundle to their native
    # DSH user-level locations.
    try:
        _deploy_dsh_preset()
        ui_plugin = _deploy_dsh_ui_plugin()
    except (CliError, OSError) as exc:
        raise CliError(_i18n_exc("agent.dsh_preset_write_failed", f"写入 dsh 全局预设失败：{exc}", exc=exc)) from exc

    if patched:
        return ("patched", ui_plugin) if any_existing else ("created", ui_plugin)
    return ("unchanged", ui_plugin)


def _deploy_dsh_preset() -> None:
    """Deploy the BugX preset cleanly to ~/.dsh/.agent-presets/bugx/.

    Reads the installed standard preset, replaces the persona text with BugX's
    audit persona (without duplicating the fixed id), and appends a unique
    bugx-supervisor MCP client entry. Duplicate loader entry ids otherwise
    make dsh reject the preset at session creation.
    """
    preset_dir = _dsh_home() / ".agent-presets" / "bugx"
    preset_dir.mkdir(parents=True, exist_ok=True)

    candidates = _dsh_standard_preset_candidates()
    standard_preset = next((p for p in candidates if p.is_file()), None)

    composition_path = preset_dir / "agent.cordis.yml"
    if standard_preset and standard_preset.is_file():
        lines = standard_preset.read_text(encoding="utf-8").splitlines()
        out_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip() == "- id: persona":
                # Emit a single persona entry with the BugX persona text.
                out_lines.append("- id: persona")
                out_lines.append("  name: '@deepseek-ai/dsh-persona'")
                out_lines.append("  config:")
                out_lines.append("    prefix: >-")
                out_lines.append("      You are BugX, an autonomous adversarial code-audit agent. Before")
                out_lines.append("      any exploration, first run bugx engine path --name bugx-quick.md (the L0 protocol card), then consult the global AGENT_GUIDE.md and bugx.md sections on demand, and")
                out_lines.append("      follow the life-ledger, supervisor lease, evidence and report protocol. The DSH package checkout and DSH_HOME are not the project root; use the active Session cwd for every project path and run pwd when paths are ambiguous.")
                # Skip every remaining line of the original persona entry.
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("- id:"):
                    i += 1
                continue
            if line.strip() == "- id: workflow-worker-thread":
                indent = len(line) - len(line.lstrip())
                line = (" " * indent) + "- id: workflow-ptc"
            elif "@deepseek-ai/dsh-workflow-worker-thread" in line:
                indent = len(line) - len(line.lstrip())
                line = (" " * indent) + "name: '@deepseek-ai/dsh-workflow-ptc'"
            out_lines.append(line)
            i += 1
    else:
        # Fallback: minimal viable preset composition when the installed dsh
        # standard template is not discoverable (e.g. isolated test HOME).
        out_lines = [
            "# BugX minimal preset composition (fallback).",
            "- id: persona",
            "  name: '@deepseek-ai/dsh-persona'",
            "  config:",
            "    prefix: >-",
            "      You are BugX, an autonomous adversarial code-audit agent. Before",
            "      any exploration, first run bugx engine path --name bugx-quick.md (L0 card), then consult the global AGENT_GUIDE.md and bugx.md sections on demand,",
            "      and follow the life-ledger, supervisor lease, evidence protocol. The DSH package checkout and DSH_HOME are not the project root; use the active Session cwd for every project path and run pwd when paths are ambiguous.",
            "- id: skill-filesystem",
            "  name: '@deepseek-ai/dsh-skill-filesystem'",
        ]

    # Native DSH questions are an agent capability, not merely a skill
    # instruction. Older standard presets and the isolated fallback may not
    # include the model-facing tool, so make the registration idempotently
    # explicit. The Web/TUI answerer remains host-owned and is not mounted
    # into the agent preset.
    if "@deepseek-ai/dsh-tool-ask-user" not in "\n".join(out_lines):
        out_lines.extend([
            "",
            "# ── BugX native question interaction ───────────────────────────────────",
            "- id: tool-ask-user",
            "  name: '@deepseek-ai/dsh-tool-ask-user'",
        ])

    # 预设层**不再**注入 MCP client。三条通道（supervisor / supervisor-worker /
    # docs）由 ``core/hosts/dsh.py::_mcp_insert_block`` 以官方
    # ``@deepseek-ai/dsh-mcp-client`` 统一写到 profile patch 的顶层 ``- insert:``。
    #
    # 此处曾硬编码 ``@bencibro/bugx-dsh-mcp-client`` 单通道。它有两个致命问题：
    #   1. 把 MCP 的 JSON Schema 当成 schemastery spec 交给 dsh-tools 编译，
    #      实测每次调用都抛 ``v3Schema.safeParse is not a function``——工具在清单
    #      里看得见、调用必失败；
    #   2. 自带 ``serverName: bugx-supervisor`` 默认值，与官方通道注册名冲突，
    #      两套并存时哪个生效取决于加载顺序。
    # 保留这段死文本会让「删掉 profile 里的坏条目」在下次 install 后复活。

    preset_name = "BugX 深度审计"
    preset_desc = (
        "你的自动化代码安全体检专家。深度排查项目中隐藏的逻辑漏洞、鉴权越界与并发隐患，"
        "不仅找 Bug，更能自动生成复现验证与修复建议。"
        "适合在项目提测发版前、安全合规体检或排查疑难缺陷时开启；日常开发与普通交流不打扰。"
    )
    preset_order = 0
    composition_text = "\n".join(out_lines) + "\n"

    for home in _all_dsh_homes():
        p_dir = home / ".agent-presets" / "bugx"
        _assert_no_symlink_components(p_dir)
        p_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_global_file_tx(
            p_dir / "agent.cordis.yml", composition_text.encode("utf-8"),
        )
        _atomic_write_global_file_tx(
            p_dir / "preset.yml",
            (
                f"name: {preset_name}\n"
                f"description: >-\n"
                f"  {preset_desc}\n"
                f"order: {preset_order}\n"
            ).encode("utf-8"),
        )
        _sync_dsh_profile_patch(
            home,
            preset_id="bugx",
            composition_text=composition_text,
            name=preset_name,
            description=preset_desc.replace("\n", " "),
            order=preset_order,
        )


def _format_dsh_preset_patch(
    preset_id: str,
    composition_text: str,
    name: str,
    description: str,
    order: int = 0,
) -> str:
    """Format an agent-preset patch block for DSH profile cordis.patch.yml."""
    fields = [f"        id: {preset_id}"]
    for key, val in (("name", name), ("description", description)):
        if val:
            fields.append(f"        {key}: {json.dumps(val, ensure_ascii=False)}")
    fields.append(f"        order: {order}")

    rows = []
    for line in composition_text.strip().splitlines():
        rows.append(f"          {line}" if line.strip() else "")

    header = (
        f"- insert:\n"
        f"    - id: preset-{preset_id}\n"
        f"      name: '@deepseek-ai/dsh-agent-preset'\n"
        f"      config:"
    )
    return (
        f"{header}\n"
        f"{chr(10).join(fields)}\n"
        f"        plugins:\n"
        f"{chr(10).join(rows)}"
    )


def _sync_dsh_profile_patch(
    home: Path,
    preset_id: str,
    composition_text: str,
    name: str,
    description: str,
    order: int = 0,
) -> None:
    """Ensure profiles/web/cordis.patch.yml carries the latest preset patch block."""
    profile_dir = home / "profiles" / "web"
    if not profile_dir.is_dir():
        return
    patch_path = profile_dir / "cordis.patch.yml"
    _assert_no_symlink_components(patch_path)
    patch_block = _format_dsh_preset_patch(preset_id, composition_text, name, description, order)

    if not patch_path.is_file():
        content = f"# Web profile user patch layer\n\n{patch_block}\n"
        _atomic_write_global_file_tx(patch_path, content.encode("utf-8"))
        return

    content = patch_path.read_text(encoding="utf-8")
    pattern = (
        rf"(- insert:\s*\n\s*- id: preset-{re.escape(preset_id)}\b"
        rf".*?(?=\n- id:|\n- insert:|\n# dsh-desktop legacy presets end|\Z))"
    )
    if re.search(pattern, content, re.DOTALL):
        updated = re.sub(pattern, patch_block, content, count=1, flags=re.DOTALL)
    elif "# dsh-desktop legacy presets end" in content:
        updated = content.replace(
            "# dsh-desktop legacy presets end",
            f"{patch_block}\n\n# dsh-desktop legacy presets end",
            1,
        )
    else:
        stripped = content.strip()
        if stripped.endswith("[]"):
            updated = content.replace("[]", f"\n{patch_block}\n")
        else:
            updated = f"{content.rstrip()}\n\n{patch_block}\n"

    updated = _ensure_official_mcp_overlay(updated)

    if updated != content:
        _atomic_write_global_file_tx(patch_path, updated.encode("utf-8"))


#: 官方 dsh MCP client（dsh 自身依赖，profile 内可解析）。
_OFFICIAL_MCP_CLIENT = "@deepseek-ai/dsh-mcp-client"
#: 三条受控通道；serverName 必须唯一，且与 core/hosts/dsh.py 的声明一致。
_OFFICIAL_MCP_SERVERS = (
    ("bugx-supervisor", ["mcp", "serve", "--root", "."]),
    ("bugx-supervisor-worker", ["mcp", "serve", "--root", ".", "--role", "worker"]),
    ("bugx-docs", ["mcp", "docs", "serve", "--root", ".", "--db",
                   ".bugx/knowledge/docs.sqlite3"]),
)
_OFFICIAL_MCP_MARKER = "# BugX MCP overlay — official dsh-mcp-client (stdio)"


def _ensure_official_mcp_overlay(content: str) -> str:
    """向 profile patch 追加/更新官方 MCP 三通道 ``- insert:`` 块。

    为什么必须由 install 自己写：``bugx install --agent dsh`` 之后
    ``bugx doctor`` 的「DeepSeek Harness MCP 配置」会扫项目 patch 与全局预设，
    确认 supervisor 通道存在。此前这份内容由预设层的第三方
    ``@bencibro/bugx-dsh-mcp-client`` 提供——但该桥把 MCP 的 JSON Schema 当
    schemastery spec 编译，实测每次调用抛 ``v3Schema.safeParse is not a
    function``（工具在清单里看得见、调用必失败）。改为官方 client 后，预设层
    不再承载 MCP，若 install 也不写，doctor 就会判定「缺少 bugx-supervisor
    MCP」，形成「修好调用却报告缺失」的新破窗。
    """
    lines = [_OFFICIAL_MCP_MARKER, "- insert:"]
    for server_name, argv in _OFFICIAL_MCP_SERVERS:
        lines.extend([
            f"    - id: {server_name}",
            f"      name: '{_OFFICIAL_MCP_CLIENT}'",
            "      config:",
            f"        serverName: {server_name}",
            "        transport: stdio",
            "        command: bugx",
            "        args:",
        ])
        lines.extend(f"          - '{arg}'" for arg in argv)
    block = "\n".join(lines) + "\n"

    # 归一算法（2026-11-08 实测重写，两次迭代）：按**条目 id** 识别受控块，
    # 不依赖 MARKER 头——历史 web patch 里存在「裸块」（旧版写入无 MARKER）
    # 与「MARKER 块」并存，只认 MARKER 会漏删裸块（实测输入 2 套输出仍 2 套）。
    # 扫描顶层 ``- insert:`` 块：块内含任一受控 id（bugx-supervisor/-worker/
    # docs）即整块删除；外来块（tool-web、preset-*）不含受控 id，原样保留。
    # 删完后尾部追加单一规范块——输入 N 套输出恒 1 套。
    # 注意 re.MULTILINE：block_text 是多行串，串首是 ``- insert:`` 而条目行
    # 带缩进在后续行——无 MULTILINE 时 ^ 只匹配串首，永远失配（实测踩坑）。
    entry_re = re.compile(r"^\s*- id:\s*(bugx-(?:supervisor|supervisor-worker|docs))\b", re.MULTILINE)
    lines = content.splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == _OFFICIAL_MCP_MARKER:
            i += 1  # MARKER 行自身一并清除（裸块收集可能终止于此，见下）
            continue
        if line.strip() == "- insert:":
            j = i + 1
            while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                j += 1
            block_text = "\n".join(lines[i:j])
            if entry_re.search(block_text):
                i = j
                continue
        kept.append(line)
        i += 1
    base = "\n".join(kept).rstrip("\n")
    if not base.strip() or base.strip() == "# Web profile user patch layer":
        return block
    return f"{base}\n\n{block}"


def _global_opencode_skills_dir() -> Path:
    """Return ~/.config/opencode/skills, creating parents if missing.

    与 :func:`_global_opencode_agents_dir` 同纪律：宿主配置属用户档案，
    BugX 的数据/引擎 override 不迁移宿主自己的发现目录。
    """
    d = Path.home().resolve() / ".config" / "opencode" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _global_opencode_agents_dir() -> Path:
    """Return ~/.config/opencode/agents, creating parents if missing.

    Host configuration belongs to the user profile. BugX data/engine overrides
    relocate BugX storage, not the host's configuration discovery directory.
    """
    d = Path.home().resolve() / ".config" / "opencode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def _link_opencode_agent_entry(root: Path) -> tuple[int, int, Path | None]:
    """项目入口指向使用 OpenCode v2 frontmatter 的专用载荷。"""
    return _link_opencode_agent_file(
        root, "bugx.md", payload_name="bugx-opencode.md",
    )


def _link_opencode_worker_entry(root: Path) -> tuple[int, int, Path | None]:
    """并发 worker 精简入口 .opencode/agents/bugx-worker.md（同源相对链接）。

    worker 只需要单元级执行规则，不加载完整 150KB 宪法。OpenCode v2 的
    subagent 调度依赖该入口；缺失载荷必须阻止安装，不能留下半套配置。
    """
    return _link_opencode_agent_file(root, "bugx-worker.md")


def _atomic_link_or_copy(payload: Path, target: Path, *, directory: bool = False) -> bool:
    """Publish *payload* at *target* as a relative symlink, else as a copy.

    Windows grants symlink creation only to elevated sessions or Developer
    Mode (WinError 1314 elsewhere), so the same tmp+replace dance falls back
    to a byte copy; doctor's integrity checks accept a hash-identical copy.
    Returns True when a symlink was published, False for the copy fallback.
    """
    try:
        rel = os.path.relpath(payload.resolve(), target.parent.resolve())
    except ValueError:
        # 跨盘时 relpath 无法计算（Windows C: -> D:），回退为绝对路径
        rel = str(payload.resolve())
    temporary = target.parent / f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        os.symlink(rel, temporary)
        os.replace(temporary, target)
        return True
    except (OSError, NotImplementedError):
        try:
            temporary.unlink()
        except OSError:
            pass
    try:
        if directory:
            shutil.copytree(payload, temporary, symlinks=True)
        else:
            shutil.copy2(payload, temporary)
        os.replace(temporary, target)
        print(
            "[bugx] 提示：当前环境不允许创建符号链接（Windows 需管理员或开发者模式），"
            f"已改用文件副本发布入口: {target}",
            file=sys.stderr,
        )
        return False
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _register_global_opencode_agent(root: Path) -> tuple[int, list[Path]]:
    """在 ~/.config/opencode/agents/ 建立指向全局引擎的符号链接。

    0.5.38（DOC-004）：Agent 入口注册到全局，在任意项目目录下启动
    OpenCode 均可直接发现并调用 @bugx / @bugx-worker。
    """
    engine = global_engine_root()
    payloads = {}
    for filename, src_name in (
        ("bugx.md", "bugx-opencode.md"),
        ("bugx-worker.md", "bugx-worker.md"),
    ):
        payload = engine / src_name
        if not payload.is_file():
            payload = engine / "contracts" / src_name
        if not payload.is_file():
            payload = asset_root() / src_name
        if not payload.is_file():
            payload = asset_root() / "contracts" / src_name
        if not payload.is_file():
            raise CliError(
                f"缺少 OpenCode v2 Agent 载荷 {src_name}，不能注册不完整的宿主 Agent"
            )
        payloads[filename] = payload
    agents_dir = _global_opencode_agents_dir()
    copied = 0
    backups: list[Path] = []
    for filename, payload in payloads.items():
        target = agents_dir / filename
        _assert_no_symlink_components(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_symlink():
                backup_dir = root / ".bugx" / "backups" / "opencode"
                _assert_no_symlink_components(backup_dir)
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / (
                    f"global-agent-{filename}-" + time.strftime("%Y%m%d-%H%M%S") + ".md"
                )
                shutil.copy2(target, backup)
                backups.append(backup)
        # F-L0-03：全局入口写入前登记事务快照，失败时按记录逆序还原。
        _tx_stage_global(target)
        # 统一相对链接：全局 agent 入口指向全局引擎，升级一处即可生效。
        _atomic_link_or_copy(payload, target)
        copied += 1
    return copied, backups


def _link_opencode_agent_file(
    root: Path, filename: str, payload_name: str | None = None,
) -> tuple[int, int, Path | None]:
    target = root / ".opencode" / "agents" / filename
    actual_name = payload_name or filename
    payload = global_engine_root() / actual_name
    if not payload.is_file():
        payload = global_engine_root() / "contracts" / actual_name
    if not payload.is_file():
        payload = (asset_root() / actual_name).resolve()
    if not payload.is_file():
        payload = (asset_root() / "contracts" / actual_name).resolve()
    if not payload.is_file():
        raise CliError(_i18n_exc("agent.opencode_payload_missing", f"缺少 {actual_name} 载荷，无法安装 OpenCode 入口", actual_name=actual_name))
    backup = None
    if target.exists():
        if not target.is_symlink():
            backup_dir = root / ".bugx" / "backups" / "opencode"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / (
                "agents-bugx-" + time.strftime("%Y%m%d-%H%M%S") + ".md"
            )
            shutil.copy2(target, backup)
    _assert_no_symlink_components(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    # resolve 消除 /var → /private/var 等 macOS 符号链接差异
    _atomic_link_or_copy(payload, target)
    return 1, 0, backup


def _migrate_legacy_opencode_agent_entry(root: Path) -> None:
    """0.5.33 前的 .opencode/agent/bugx.md（单数，非官方路径）迁移到备份。"""
    legacy = root / ".opencode" / "agent" / "bugx.md"
    if legacy.is_symlink() or not legacy.is_file():
        return
    backup_dir = root / ".bugx" / "backups" / "opencode"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (
        "agent-bugx-" + time.strftime("%Y%m%d-%H%M%S") + ".md"
    )
    try:
        shutil.copy2(legacy, backup)
        legacy.unlink()
    except OSError:
        pass


def _unlink_global_opencode_agent(root: Path) -> None:
    """0.5.38（DOC-004）：agent 全局注册是设计义务，不再视为「遗留痕迹」。

    保留函数体以兼容上游调用者；不再删除 ~/.config/opencode/agents/ 下的
    任何链接。彻底卸载请使用 `bugx uninstall`（同样不触碰全局 agent 注册，
    因为全局引擎属多项目共享资产）。
    """
    return None


def _self_check_pi_skill_registered(root: Path) -> None:
    """安装完成后自检：pi 是否能发现 bugx skill。

    默认开启，可通过 BUGX_SELF_CHECK=0 显式关闭。
    """
    if os.environ.get("BUGX_SELF_CHECK") == "0":
        return
    home = Path.home().resolve()
    pi_skill_link = home / ".pi" / "skills" / "bugx"
    pi_agent_skill_link = home / ".pi" / "agent" / "skills" / "bugx"
    agents_skill_dir = home / ".agents" / "skills" / "bugx"

    def _is_reg(link: Path) -> bool:
        return link.is_symlink() or (link.is_dir() and (link / "SKILL.md").is_file())

    if not (_is_reg(pi_skill_link) or _is_reg(pi_agent_skill_link)):
        raise CliError(_i18n_exc("agent.pi_skill_symlink_missing", f"自检失败：pi 未注册 bugx skill 符号链接 ({pi_skill_link})", path=pi_skill_link))
    if not agents_skill_dir.is_dir():
        raise CliError(_i18n_exc("agent.pi_global_skill_dir_missing", f"自检失败：全局 skill 目录不存在 ({agents_skill_dir})", path=agents_skill_dir))
    if not (agents_skill_dir / "SKILL.md").is_file():
        raise CliError(_i18n_exc("agent.pi_skill_md_missing", f"自检失败：SKILL.md 不存在于 ({agents_skill_dir})", path=agents_skill_dir))


def _link_global_pi_skill(root: Path, locale: str | None = None) -> None:
    """把项目 .pi/skills/bugx/SKILL.md 注册到全局 pi 可发现的位置。

    Pi 0.85+ 优先从 ~/.pi/agent/skills/<name>/ 发现用户全局 skill，同时
    兼容旧版 ~/.pi/skills/<name>/。本函数：
    1) 把 SKILL.md 复制到 ~/.agents/skills/bugx/（如已有 L0 路由则刷新其 description）
    2) 建立/更新 ~/.pi/skills/bugx 与 ~/.pi/agent/skills/bugx 相对符号链接
    """
    source = root.resolve() / ".pi" / "skills" / "bugx" / "SKILL.md"
    if not source.is_file():
        return
    home = Path.home().resolve()
    agents_skill_parent = (home / ".agents" / "skills").resolve(strict=False)
    if not agents_skill_parent.is_relative_to(home):
        raise CliError(_i18n_exc("agent.pi_global_skill_escaped_user", f"Pi 全局 Skill 目录越出用户目录：{agents_skill_parent}", path=agents_skill_parent))
    _assert_no_symlink_components(agents_skill_parent)
    agents_skill_dir = agents_skill_parent / "bugx"
    pi_skill_dir = home / ".pi" / "skills" / "bugx"
    pi_agent_skill_dir = home / ".pi" / "agent" / "skills" / "bugx"
    _assert_no_symlink_components(agents_skill_dir)
    _assert_no_symlink_components(pi_skill_dir.parent)
    _assert_no_symlink_components(pi_agent_skill_dir.parent)
    agents_skill_dir.mkdir(parents=True, exist_ok=True)
    target = agents_skill_dir / "SKILL.md"
    should_write = True
    if target.is_file():
        try:
            existing_text = target.read_text(encoding="utf-8")
            if "BugX for ChatGPT" in existing_text or "BugX L0 路由入口" in existing_text:
                should_write = False
                # 同步刷新 Frontmatter description 保证全局描述与当前语言对齐
                from ..host_adapters import get_agent_description
                desc = get_agent_description(locale)
                new_text = re.sub(
                    r"^description:[^\n]+",
                    f"description: {desc}",
                    existing_text,
                    count=1,
                    flags=re.MULTILINE,
                )
                if new_text != existing_text:
                    _tx_stage_global(target)
                    _write_host_resource(root, target, new_text.encode("utf-8"), "pi-global")
        except OSError:
            pass
    if should_write:
        _tx_stage_global(target)
        _write_host_resource(root, target, source.read_bytes(), "pi-global")

    for p_dir in (pi_skill_dir, pi_agent_skill_dir):
        if p_dir.is_dir() and not p_dir.is_symlink():
            # Windows 无符号链接权限时，首次安装会发布一个目录副本。
            # 第二次安装必须对这个由 BugX 生成且仍与全局实体完全一致的副本
            # 幂等放行；若内容已被用户改写，则继续拒绝覆盖，避免破坏用户目录。
            global_skill = agents_skill_dir / "SKILL.md"
            candidate = p_dir / "SKILL.md"
            try:
                owned_copy = (
                    candidate.is_file()
                    and global_skill.is_file()
                    and _sha256_file(candidate) == _sha256_file(global_skill)
                )
            except OSError:
                owned_copy = False
            if not owned_copy:
                raise CliError(_i18n_exc("agent.pi_user_dir_skill_refused", f"拒绝覆盖用户目录形式的 Pi Skill：{p_dir}", path=p_dir))
            continue
        p_dir.parent.mkdir(parents=True, exist_ok=True)
        _tx_stage_global(p_dir)
        _atomic_link_or_copy(agents_skill_dir, p_dir, directory=True)


def _unlink_global_pi_skill(root: Path, removed: list[str], preserved: list[str]) -> None:
    """普通项目卸载保留全局 Pi Skill，只清理悬空的 ~/.pi 链接。

    ~/.agents/skills/bugx 与 ~/.pi/skills/bugx 是多项目共享的全局注册，
    无法按单项目判定归属（A 项目卸载不能破坏 B 项目的安装）；全局
    清理统一由 ``uninstall --self`` 的 ``_self_uninstall_pi_skill`` 负责。
    这里仅删除目标已不存在的悬空链接，避免残留死链。
    """
    agents_skill_dir = Path.home() / ".agents" / "skills" / "bugx"
    pi_skill_dir = Path.home() / ".pi" / "skills" / "bugx"
    pi_agent_skill_dir = Path.home() / ".pi" / "agent" / "skills" / "bugx"
    try:
        if (agents_skill_dir / "SKILL.md").is_file():
            preserved.append(
                "~/.agents/skills/bugx（全局 Pi Skill 保留：多项目共享，"
                "全局清理需 uninstall --self）"
            )
            return
        for p_dir, label in ((pi_skill_dir, "~/.pi/skills/bugx"), (pi_agent_skill_dir, "~/.pi/agent/skills/bugx")):
            if p_dir.is_symlink() and not (p_dir / "SKILL.md").exists():
                p_dir.unlink()
                removed.append(f"{label}（悬空链接）")
    except OSError:
        pass


def _sync_workbuddy_private_venv(
    source: Path, *, target_version: str | None = None,
) -> tuple[bool, str]:
    """探测并同步 WorkBuddy 私有 Python 虚拟环境中的 bugx-cli 包。"""
    from bugx_cli.host_adapters import workbuddy_cli_venv
    venv = workbuddy_cli_venv()
    if not venv:
        return False, "未检测到 WorkBuddy 私有虚拟环境"

    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    py_exe = bin_dir / ("python.exe" if os.name == "nt" else "python")
    if not py_exe.is_file():
        return False, f"WorkBuddy 虚拟环境缺失 Python 解释器: {py_exe}"

    from ..common import _is_bugx_source_repo
    import subprocess
    is_source = _is_bugx_source_repo(source)
    if is_source:
        cmd = [str(py_exe), "-m", "pip", "install", "-e", str(source.resolve()), "--no-deps"]
    else:
        pkg = f"bugx-cli=={target_version}" if target_version else "bugx-cli"
        cmd = [str(py_exe), "-m", "pip", "install", "--upgrade", pkg, "--no-deps"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            return False, f"同步失败 (exit={proc.returncode}): {err}"

        # 恶性设计治理 (DOC-180)：强制同步多语言 Tree-sitter 依赖包，杜绝环境缺少 AST 解析器
        subprocess.run(
            [str(py_exe), "-m", "pip", "install", "tree-sitter-language-pack>=1.14.3", "--no-warn-script-location"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        mode_desc = f"开发模式 ({source.name})" if is_source else pkg
        return True, f"已同步沙箱环境 {venv} ({mode_desc})"
    except Exception as exc:
        return False, f"同步异常: {exc}"


def _opencode_usage_skill_path(root: Path, source: Path) -> Path:
    """定位 bugx-opencode 使用手册 Skill 的唯一权威源。

    与 :func:`_zcode_usage_skill_path` 同模式：wheel 载荷优先
    （``share/bugx-agent/plugins/bugx/skills/bugx-opencode/SKILL.md``），
    源码仓库开发态回退 ecosystem/ 与 plugins/ 镜像。
    """
    candidates = (
        source.parent / "plugins" / "bugx" / "skills" / "bugx-opencode" / "SKILL.md",
        root / "ecosystem" / "plugins" / "bugx" / "skills" / "bugx-opencode" / "SKILL.md",
        root / "plugins" / "bugx" / "skills" / "bugx-opencode" / "SKILL.md",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return candidates[0]


def _ensure_opencode_user_skill(
    root: Path, source: Path, *, backup_label: str,
) -> dict[str, object]:
    """把 bugx-opencode 使用手册 Skill 写入 OpenCode 用户级 skills 目录。

    OpenCode v2 从 ``~/.config/opencode/skills/<name>/SKILL.md`` 发现用户级
    Skill（与 ZCode ``~/.zcode/skills`` 同构）。全局层产物跨项目可见，与
    agents 全局软链同一安装事务；内容随 wheel 版本演进——新环境
    ``bugx install --agent opencode`` 即获得。增广件语义与 zcode 一致：
    旧 wheel 载荷缺资产时跳过部署（不阻塞安装），升级后自动生效。
    """
    skill_source = _opencode_usage_skill_path(root, source)
    if skill_source.is_symlink() or not skill_source.is_file():
        return {
            "dir": str(_global_opencode_skills_dir()),
            "written": [],
            "backups": [],
            "status": "skipped_missing_source",
        }
    skills_dir = _global_opencode_skills_dir()
    if skills_dir.is_symlink():
        raise CliError(_i18n_exc(
            "agent.opencode_symlink_skills_dir",
            f"拒绝写入符号链接 OpenCode 用户级 skills 目录: {skills_dir}",
            skills_dir=skills_dir,
        ))
    target = skills_dir / "bugx-opencode" / "SKILL.md"
    if target.is_symlink():
        raise CliError(_i18n_exc(
            "agent.opencode_symlink_skill_refused",
            f"拒绝覆盖符号链接 OpenCode 用户级 Skill: {target}",
            target=target,
        ))
    if target.exists() and not target.is_file():
        raise CliError(_i18n_exc(
            "agent.opencode_user_skill_not_regular_file",
            f"OpenCode 用户级 Skill 不是普通文件，拒绝覆盖: {target}", target=target,
        ))
    payload = skill_source.read_bytes()
    if target.is_file():
        try:
            if target.read_bytes() == payload:
                return {
                    "dir": str(skills_dir),
                    "written": [{
                        "path": str(target), "status": "unchanged",
                        "backup": None, "created": False,
                    }],
                    "backups": [],
                }
        except OSError as exc:
            raise CliError(_i18n_exc(
                "agent.opencode_user_skill_unreadable",
                f"无法读取已有 OpenCode 用户级 Skill，未修改: {target}: {exc}",
                target=target, exc=exc,
            )) from exc
    backup = _backup_host_resource(root, backup_label, target) if target.exists() else None
    backups = [backup] if backup is not None else []
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_global_file(target, payload)
    return {
        "dir": str(skills_dir),
        "written": [{
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
            "created": backup is None,
        }],
        "backups": backups,
    }


def _ensure_workbuddy_user_skill(root: Path, content: str) -> dict[str, object]:
    """Write the WorkBuddy user-level Skill so every project can see BugX.

    The desktop host discovers skills from ``<configDir>/skills`` (configDir =
    ``WORKBUDDY_CONFIG_DIR`` for the running edition, e.g. ``~/.workbuddy-ai``
    for the overseas build).  This is a global-layer artifact shared by all
    projects: ownership is content-based, and a wrong or missing target never
    breaks the project-level install.  Returns per-file status for the install
    transaction rollback, mirroring :func:`_ensure_zcode_user_agents`.
    """
    target_root = workbuddy_user_skill_root()
    skill_dir = target_root / "skills" / "bugx"
    target = skill_dir / "SKILL.md"
    if target.is_symlink():
        raise CliError(_i18n_exc(
            "agent.workbuddy_user_skill_symlink_refused",
            f"拒绝覆盖符号链接 WorkBuddy 用户级 Skill: {target}", target=target,
        ))
    if target.exists() and not target.is_file():
        raise CliError(_i18n_exc(
            "agent.workbuddy_user_skill_not_regular_file",
            f"WorkBuddy 用户级 Skill 不是普通文件，拒绝覆盖: {target}", target=target,
        ))
    payload = content.encode("utf-8")
    if target.is_file():
        try:
            if target.read_bytes() == payload:
                return {
                    "root": str(target_root),
                    "written": [{
                        "path": str(target), "status": "unchanged",
                        "backup": None, "created": False,
                    }],
                    "backups": [],
                }
        except OSError as exc:
            raise CliError(_i18n_exc(
                "agent.workbuddy_user_skill_unreadable",
                f"无法读取已有 WorkBuddy 用户级 Skill，未修改: {target}: {exc}",
                target=target, exc=exc,
            )) from exc
    backup = _backup_host_resource(root, "workbuddy", target) if target.exists() else None
    backups = [backup] if backup is not None else []
    # _atomic_write_global_file_tx is the single snapshot/journal boundary.
    _atomic_write_global_file_tx(target, payload)
    return {
        "root": str(target_root),
        "written": [{
            "path": str(target),
            "status": "created" if backup is None else "upgraded",
            "backup": str(backup) if backup is not None else None,
            "created": backup is None,
        }],
        "backups": backups,
    }


def _ensure_workbuddy_connector(root: Path, content: str) -> dict[str, object]:
    """Stage the official WorkBuddy ``skill-only`` Connector package.

    WorkBuddy Skills have no icon field.  The Connector is the supported
    packaging boundary for marketplace/import display, while project and
    user-level Skill paths remain the runtime discovery surfaces.
    """
    source_dir = _integration_dir() / "workbuddy"
    source_meta = source_dir / "connector-meta.json"
    source_icon = source_dir / "icon.png"
    if (source_meta.is_symlink() or not source_meta.is_file()
            or source_icon.is_symlink() or not source_icon.is_file()):
        raise CliError(_i18n_exc(
            "agent.workbuddy_connector_resource_missing",
            f"缺少 WorkBuddy Connector 适配资源：{source_dir}",
            source=source_dir,
        ))
    try:
        metadata = json.loads(source_meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc(
            "agent.workbuddy_connector_metadata_invalid",
            f"WorkBuddy Connector 元数据无效：{source_meta}: {exc}",
            source=source_meta, exc=exc,
        )) from exc
    if not isinstance(metadata, dict) or metadata.get("type") != "skill-only":
        raise CliError(_i18n_exc(
            "agent.workbuddy_connector_metadata_invalid",
            f"WorkBuddy Connector 必须声明 type=skill-only：{source_meta}",
            source=source_meta, exc="type != skill-only",
        ))

    target_root = root.resolve() / ".workbuddy" / "connector" / "bugx"
    _assert_no_symlink_components(target_root)
    targets = (
        (source_meta, target_root / "connector-meta.json"),
        (source_icon, target_root / "icon.png"),
    )
    written: list[dict[str, object]] = []
    backups: list[Path] = []
    for source, target in targets:
        copied, skipped, backup = _write_host_resource(
            root, target, source.read_bytes(), "workbuddy",
        )
        written.append({
            "path": str(target),
            "status": "created" if copied and backup is None else
            "upgraded" if copied else "unchanged",
            "backup": str(backup) if backup is not None else None,
            "created": bool(copied and backup is None),
        })
        if backup is not None:
            backups.append(backup)

    skill_target = target_root / "skills" / "bugx" / "SKILL.md"
    copied, skipped, backup = _write_host_resource(
        root, skill_target, content.encode("utf-8"), "workbuddy",
    )
    written.append({
        "path": str(skill_target),
        "status": "created" if copied and backup is None else
        "upgraded" if copied else "unchanged",
        "backup": str(backup) if backup is not None else None,
        "created": bool(copied and backup is None),
    })
    if backup is not None:
        backups.append(backup)
    return {
        "root": str(target_root),
        "paths": [str(target) for _source, target in targets] + [str(skill_target)],
        "written": written,
        "backups": backups,
    }


def _ensure_workbuddy_custom_expert(
    root: Path, content: str, config_root: Path | None = None,
) -> dict[str, object]:
    """Package and register BugX as a WorkBuddy Custom Expert in my-experts marketplace."""
    if config_root is None:
        config_root = workbuddy_user_skill_root()
    my_experts_dir = config_root / "plugins" / "marketplaces" / "my-experts"
    expert_plugin_dir = my_experts_dir / "plugins" / "bugx"

    written: list[dict[str, object]] = []
    backups: list[Path] = []

    for sub in (".codebuddy-plugin", ".workbuddy-plugin", "avatars", "agents", "skills/bugx"):
        (expert_plugin_dir / sub).mkdir(parents=True, exist_ok=True)

    wb_name = "work" + "buddy"

    # 1. Avatar
    avatar_src = _integration_dir() / wb_name / "icon.png"
    avatar_dest = expert_plugin_dir / "avatars" / "expert.png"
    if avatar_src.is_file():
        src_bytes = avatar_src.read_bytes()
        if not avatar_dest.is_file() or avatar_dest.read_bytes() != src_bytes:
            backup = _backup_host_resource(root, wb_name, avatar_dest) if avatar_dest.exists() else None
            if backup is not None:
                backups.append(backup)
            _atomic_write_global_file_tx(avatar_dest, src_bytes)
            written.append({
                "path": str(avatar_dest),
                "status": "created" if backup is None else "upgraded",
                "backup": str(backup) if backup is not None else None,
                "created": backup is None,
            })

    # 2. Agent definition
    agent_dest = expert_plugin_dir / "agents" / "bugx.md"
    agent_content = (
        "---\n"
        "name: bugx\n"
        "description: >-\n"
        "  BugX 代码审计专家：专注于代码质量与安全的自主审查，深入分析代码逻辑、发现隐蔽缺陷并自动编写 PoC 验证，提供确凿的证据闭环与修复方案。\n"
        "description_zh: >-\n"
        "  BugX 代码审计专家：专注于代码质量与安全的自主审查，深入分析代码逻辑、发现隐蔽缺陷并自动编写 PoC 验证，提供确凿的证据闭环与修复方案。\n"
        "description_en: >-\n"
        "  BugX Code Auditor: Autonomous code quality and security auditing expert. Specializes in deep defect hunting, adversarial testing, PoC reproduction, and verified remediation.\n"
        "tools: Bash,Read,Edit\n"
        "color: \"#08B7D4\"\n"
        "---\n\n"
        "你是一位专业的代码质量与漏洞挖掘专家（BugX 代码审计专家）。你的核心使命是帮助开发者在代码合入、发布前或疑难故障排查时，进行深层次的代码审计、对抗性缺陷排查与自动化验证。\n\n"
        "## 核心工作原则\n"
        "1. **证据驱动（Evidence-Driven）**：不凭空猜测 Bug，必须通过静态逻辑推演或最小复现测试（PoC）验证缺陷存在。\n"
        "2. **闭环验证（Verification Loop）**：缺陷发现后，优先编写自动化回归单测，验证失败；给出修复方案后重新运行测试验证通过。\n"
        "3. **安全与质量并重**：涵盖业务逻辑越权、并发死锁/竞态条件、边界值溢出、资源泄漏、SQL注入、SSRF等安全与稳定性隐患。\n\n"
        "## 协作流程\n"
        "1. **审计准备**：读取目标文件、架构设计与历史提交记录，理清数据流向与契约约束。\n"
        "2. **深入排查**：对输入校验、状态转移、异常处理、外部调用等关键节点进行对抗式推演。\n"
        "3. **产出报告**：输出清晰的问题说明、触发条件、风险等级与最小修复代码。\n"
    )
    backup = _backup_host_resource(root, wb_name, agent_dest) if agent_dest.exists() else None
    if backup is not None:
        backups.append(backup)
    _atomic_write_global_file_tx(agent_dest, agent_content.encode("utf-8"))
    written.append({
        "path": str(agent_dest),
        "status": "created" if backup is None else "upgraded",
        "backup": str(backup) if backup is not None else None,
        "created": backup is None,
    })

    # 3. Plugin manifest
    plugin_meta = {
        "name": "bugx",
        "version": "0.1.0",
        "expertType": "agent",
        "displayName": {
            "zh": "BugX",
            "en": "BugX"
        },
        "profession": {
            "zh": "代码审计专家",
            "en": "Code Audit Expert"
        },
        "description": "专注于代码质量与安全的自主审查智能体。专为深层缺陷排查而生——在功能写完、提 PR 或排查疑难 Bug 时，它能深入分析代码逻辑、发现隐蔽缺陷并自动编写 PoC 验证，提供确凿的证据闭环与修复方案。",
        "displayDescription": {
            "zh": "专注于代码质量与安全的自主审查智能体。专为深层缺陷排查而生——在功能写完、提 PR 或排查疑难 Bug 时，它能深入分析代码逻辑、发现隐蔽缺陷并自动编写 PoC 验证，提供确凿的证据闭环与修复方案。",
            "en": "Autonomous code quality and security auditing expert. Specializes in deep defect hunting, adversarial testing, PoC reproduction, and verified remediation."
        },
        "avatar": "avatars/expert.png",
        "category": "development",
        "categoryId": "01-Development",
        "tags": [
            {"zh": "代码审计", "en": "Code Audit"},
            {"zh": "漏洞挖掘", "en": "Vulnerability"},
            {"zh": "自动化测试", "en": "Automated Testing"}
        ],
        "quickPrompts": [
            {"zh": "对当前代码库进行一次全面的代码审计与漏洞排查", "en": "Perform a comprehensive code audit and vulnerability check on the current codebase"},
            {"zh": "排查最近提交的代码中潜在的并发与边界缺陷", "en": "Investigate potential concurrency and boundary defects in recent commits"},
            {"zh": "针对已知 Bug 编写最小复现 PoC 并验证修复方案", "en": "Write a minimal reproduction PoC for an issue and verify the fix"}
        ],
        "agents": ["./agents/bugx.md"],
        "skills": ["./skills/bugx"]
    }
    meta_bytes = (json.dumps(plugin_meta, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    for meta_path in (
        expert_plugin_dir / ".codebuddy-plugin" / "plugin.json",
        expert_plugin_dir / ".workbuddy-plugin" / "plugin.json",
    ):
        _atomic_write_global_file_tx(meta_path, meta_bytes)
        written.append({"path": str(meta_path), "status": "created", "backup": None, "created": True})

    # 4. Skill copy
    skill_file = expert_plugin_dir / "skills" / "bugx" / "SKILL.md"
    _atomic_write_global_file_tx(skill_file, content.encode("utf-8"))
    written.append({"path": str(skill_file), "status": "created", "backup": None, "created": True})

    # 5. Marketplace manifest
    market_manifest_path = my_experts_dir / ".codebuddy-plugin" / "marketplace.json"
    market_manifest: dict[str, object] = {
        "name": "my-experts",
        "description": "my-experts marketplace (auto-generated)",
        "plugins": []
    }
    if market_manifest_path.is_file():
        try:
            existing = json.loads(market_manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                market_manifest = existing
        except (OSError, json.JSONDecodeError):
            pass
    plugins_list = market_manifest.get("plugins")
    if not isinstance(plugins_list, list):
        plugins_list = []
        market_manifest["plugins"] = plugins_list
    has_bugx = any(isinstance(p, dict) and p.get("name") == "bugx" for p in plugins_list)
    if not has_bugx:
        plugins_list.append({
            "name": "bugx",
            "source": "./plugins/bugx",
            "description": plugin_meta["description"]
        })
        _atomic_write_global_file_tx(
            market_manifest_path,
            (json.dumps(market_manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
        written.append({"path": str(market_manifest_path), "status": "created", "backup": None, "created": True})

    # 6. User experts
    custom_experts_root = config_root / "experts" / "custom"
    target_uids: set[str] = set()
    if custom_experts_root.is_dir():
        for entry in custom_experts_root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                target_uids.add(entry.name)
    settings_file = config_root / "settings.json"
    if settings_file.is_file():
        try:
            s_data = json.loads(settings_file.read_text(encoding="utf-8"))
            owner_uid = s_data.get("legacyOwnerUid")
            if owner_uid and isinstance(owner_uid, str) and "/" not in owner_uid:
                target_uids.add(owner_uid)
        except (OSError, json.JSONDecodeError):
            pass
    for uid in target_uids:
        user_experts_file = custom_experts_root / uid / "experts.json"
        existing_list: list[str] = []
        if user_experts_file.is_file():
            try:
                loaded_list = json.loads(user_experts_file.read_text(encoding="utf-8"))
                if isinstance(loaded_list, list):
                    existing_list = [str(x) for x in loaded_list]
            except (OSError, json.JSONDecodeError):
                pass
        if "bugx" not in existing_list:
            existing_list.append("bugx")
            _atomic_write_global_file_tx(
                user_experts_file,
                (json.dumps(existing_list, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            )
            written.append({"path": str(user_experts_file), "status": "created", "backup": None, "created": True})

    return {
        "root": str(expert_plugin_dir),
        "written": written,
        "backups": backups,
    }


def _check_workbuddy_user_skill(root: Path, *, repair: bool = False) -> tuple[bool, str]:
    """Validate the WorkBuddy user-level Skill against the current template."""
    source = global_engine_root() / "bugx.md"
    if not source.is_file():
        return True, "项目尚未初始化，跳过用户级校验"
    skill = workbuddy_user_skill_root() / "skills" / "bugx" / "SKILL.md"
    if not skill.is_file():
        return False, "缺少用户级 Skill（<配置根>/skills/bugx/SKILL.md）；请运行 bugx install --agent workbuddy"
    try:
        content = skill.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"用户级 Skill 无法读取: {skill}: {exc}"
    locale = read_locale_marker(content)
    try:
        rendered = strip_locale_marker(
            get_adapter("workbuddy").render(source, locale=locale)
        ).strip()
    except CliError as exc:
        return False, str(exc)
    if strip_locale_marker(content).strip() != rendered:
        if repair:
            try:
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text(rendered + "\n", encoding="utf-8")
                _ensure_workbuddy_custom_expert(root, rendered)
                return True, f"用户级 Skill 已自动修复对齐模板: {skill}"
            except OSError as exc:
                return False, f"用户级 Skill 与当前模板不一致，且自动修复失败: {skill}: {exc}"
        return False, f"用户级 Skill 与当前模板不一致: {skill}"
    return True, f"用户级 Skill 就绪: {skill}"


def _incompatible_opencode_mcp_names(servers: object) -> list[str]:
    if not isinstance(servers, dict):
        return []
    return [
        name for name in OPENCODE_ALL_REQUIRED_MCP
        if name in servers and _mcp_entry_state(name, servers[name]) == "incompatible"
    ]


def _reject_incompatible_opencode_mcp(names: list[str], target: Path) -> None:
    if names:
        raise CliError(
            f"OpenCode 配置包含已退役的 BugX MCP 命令（{', '.join(names)}）：{target}。"
            "此格式不再自动迁移或覆盖；请手动删除对应的 mcp 条目后，重新运行 "
            "`bugx install --agent opencode --root <项目路径>`。"
        )


_OPENCODE_V1_CONFIG_KEYS = {"plugin", "permission", "tools", "agent"}
_OPENCODE_REQUIRED_PLUGIN = "@dietrichgebert/ponytail"


def _opencode_v2_servers(
    payload: dict[str, Any], target: Path, *, create: bool = False,
) -> dict[str, Any]:
    """Validate and return the OpenCode v2 ``mcp.servers`` map."""
    legacy = sorted(_OPENCODE_V1_CONFIG_KEYS.intersection(payload))
    if legacy:
        raise CliError(
            f"OpenCode 配置使用 v1 字段（{', '.join(legacy)}）：{target}。"
            "BugX 只写 OpenCode v2 格式，不自动迁移旧配置；请备份后按 v2 重建，"
            "再运行 `bugx install --agent opencode --root <项目路径>`。"
        )
    plugins = payload.get("plugins")
    if plugins is not None and not isinstance(plugins, list):
        raise CliError(f"OpenCode v2 配置的 plugins 必须是数组：{target}")

    if "mcp" not in payload:
        if not create:
            return {}
        payload["mcp"] = {}
    mcp = payload["mcp"]
    if not isinstance(mcp, dict):
        raise CliError(f"OpenCode v2 配置的 mcp 必须是对象：{target}")
    servers = mcp.get("servers")
    if servers is None:
        if mcp:
            raise CliError(
                f"OpenCode 配置的 MCP server 必须位于 mcp.servers：{target}。"
                "检测到旧的扁平 mcp 结构，BugX 不自动迁移；请备份后按 v2 重建。"
            )
        if not create:
            return {}
        servers = {}
        mcp["servers"] = servers
    if not isinstance(servers, dict):
        raise CliError(f"OpenCode v2 配置的 mcp.servers 必须是对象：{target}")
    legacy_entries = sorted(
        str(name) for name, entry in servers.items()
        if isinstance(entry, dict) and "enabled" in entry
    )
    if legacy_entries:
        raise CliError(
            f"OpenCode v2 MCP 条目使用已退役的 enabled 字段：{', '.join(legacy_entries)}。"
            "请改用 disabled（仅停用时设为 true），然后重新运行安装。"
        )
    return servers


def _preflight_opencode_mcp_config(root: Path) -> None:
    """Reject OpenCode v1 config before install writes project or engine state."""
    target = root.resolve() / CONFIG_NAME
    _assert_no_symlink_components(target)
    if target.is_symlink():
        raise CliError(_i18n_exc(
            "agent.opencode_symlink_config_refused",
            f"拒绝使用符号链接 OpenCode 配置: {target}", target=target,
        ))
    if not target.exists():
        return
    if not target.is_file():
        raise CliError(_i18n_exc(
            "agent.opencode_config_not_regular_file",
            f"OpenCode 配置不是普通文件: {target}", target=target,
        ))
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc(
            "agent.opencode_existing_unparseable",
            f"已有 OpenCode 配置无法解析，未修改: {target}: {exc}",
            target=target, exc=exc,
        )) from exc
    if not isinstance(payload, dict):
        raise CliError(_i18n_exc(
            "agent.opencode_existing_not_object",
            f"已有 OpenCode 配置顶层必须是 JSON 对象，未修改: {target}",
            target=target,
        ))
    servers = _opencode_v2_servers(payload, target)
    _reject_incompatible_opencode_mcp(_incompatible_opencode_mcp_names(servers), target)


def _ensure_opencode_config(
    root: Path, *, selected: set[str] | None = None,
) -> tuple[str, Path | None]:
    """Create or safely merge the bundled OpenCode MCP configuration.

    Existing user entries are retained. Missing BugX-recommended MCP entries
    are added after a recoverable backup in ``.bugx/backups/opencode``. Retired
    managed commands are rejected before any config backup or write.

    **注入白名单**（DOC-236）：只写 BugX 自有控制通道 + **本轮被定向且已安装**的
    外部 server。模板里的外部条目只是"目录"，不能因为列在模板里就写进项目配置：
    未安装会让宿主启动即失败，disabled（供应链未证实）更是绝不该出现。
    """
    target = root.resolve() / CONFIG_NAME
    _assert_no_symlink_components(target)
    if target.is_symlink():
        raise CliError(_i18n_exc("agent.opencode_symlink_config_refused", f"拒绝使用符号链接 OpenCode 配置: {target}", target=target))
    source = bundled_config()
    if source.is_symlink() or not source.is_file():
        raise CliError(_i18n_exc("agent.opencode_template_insecure", f"OpenCode 配置模板不存在或不安全: {source}", source=source))
    try:
        bundled = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc("agent.opencode_template_invalid", f"OpenCode 配置模板无效: {source}: {exc}", source=source, exc=exc)) from exc
    if not isinstance(bundled, dict):
        raise CliError(_i18n_exc("agent.opencode_template_invalid", f"OpenCode 配置模板顶层必须是对象: {source}", source=source))
    try:
        bundled_servers = _opencode_v2_servers(bundled, source)
    except CliError as exc:
        raise CliError(f"OpenCode v2 配置模板无效: {exc}") from exc
    bundled_plugins = bundled.get("plugins")
    if not isinstance(bundled_plugins, list) or _OPENCODE_REQUIRED_PLUGIN not in bundled_plugins:
        raise CliError(f"OpenCode v2 配置模板缺少 plugins 中的 {_OPENCODE_REQUIRED_PLUGIN}: {source}")
    # 注入白名单：自有控制通道 + （被定向 ∩ 已安装）的外部 server。
    core_channels = {"bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"}
    available = installed_runtime_servers()
    keep = core_channels | (set(selected or ()) & available)
    # 仅移除当前配置中不属于本轮注入白名单、且精确匹配受管理模板的可选项。
    # 已知退役命令在进入合并前就会被拒绝，不会在这里被静默清理或升级。
    dropped_names = {
        name for name in OPENCODE_ALL_REQUIRED_MCP
        if name not in keep
    }
    for name in list(bundled_servers):
        if name not in keep:
            bundled_servers.pop(name, None)
    if target.exists():
        if not target.is_file():
            raise CliError(_i18n_exc("agent.opencode_config_not_regular_file", f"OpenCode 配置不是普通文件: {target}", target=target))
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CliError(_i18n_exc("agent.opencode_existing_unparseable", f"已有 OpenCode 配置无法解析，未修改: {target}: {exc}", target=target, exc=exc)) from exc
        if not isinstance(payload, dict):
            raise CliError(_i18n_exc("agent.opencode_existing_not_object", f"已有 OpenCode 配置顶层必须是 JSON 对象，未修改: {target}", target=target))
        servers = _opencode_v2_servers(payload, target, create=True)
        _reject_incompatible_opencode_mcp(
            _incompatible_opencode_mcp_names(servers), target,
        )
        configuration_changed = False
        plugins = payload.get("plugins")
        if plugins is None:
            plugins = []
            payload["plugins"] = plugins
            configuration_changed = True
        if not isinstance(plugins, list):
            raise CliError(f"OpenCode v2 配置的 plugins 必须是数组：{target}")
        if _OPENCODE_REQUIRED_PLUGIN not in plugins:
            plugins.append(_OPENCODE_REQUIRED_PLUGIN)
            configuration_changed = True
        # OpenCode Code Mode is disabled in BugX agents so their MCP calls must
        # use the explicit, permission-checked direct tool names. Apply this to
        # every BugX-managed server present in this install, including Docs and
        # selected optional runtimes.
        managed_names = [name for name in OPENCODE_ALL_REQUIRED_MCP if name in bundled_servers]
        for name in managed_names:
            entry = servers.get(name)
            if (
                isinstance(entry, dict)
                and _mcp_entry_state(name, entry) == "configured"
                and entry.get("codemode") is not False
            ):
                entry["codemode"] = False
                configuration_changed = True
        remove_unavailable_optional = [
            name for name in dropped_names
            if _mcp_entry_state(name, servers.get(name)) == "configured"
        ]
        missing = [
            name for name in managed_names
            if not isinstance(servers.get(name), dict)
        ]
        if not missing and not remove_unavailable_optional and not configuration_changed:
            return "unchanged", None
        backup = _backup_opencode_config(root, target)
        for name in remove_unavailable_optional:
            servers.pop(name, None)
        for name in missing:
            servers[name] = copy.deepcopy(bundled_servers[name])
        updated = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(updated)
            os.chmod(temporary, target.stat().st_mode)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return "merged", backup

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(bundled, ensure_ascii=False, indent=2) + "\n")
        os.chmod(temporary, source.stat().st_mode & 0o777)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return "created", None


def _check_host_mcp_config(root: Path, host_name: str, *, repair: bool = False) -> tuple[bool, str]:
    """Validate MCP configurations across all supported AI Agent hosts.

    Inspects each host's native configuration entry (.opencode/opencode.json,
    .dsh/patch.yml or preset, .claude.json/.mcp.json for Claude, .codex/config,
    .pi extension manifest, or ChatGPT marketplace) to check if the required BugX
    Supervisor MCP or client dependencies are properly configured.

    这里刻意保留按宿主名的分派，而不是收敛为 ``adapter.supports(...)``：每个
    分支是**宿主专属的诊断规则**（各自的文件布局、必需项和用户可见文案），
    不是同一种能力的参数化差异。把它下沉到适配器需要 agent 层的
    ``_check_opencode_config`` / ``_dsh_home`` / ``_check_workbuddy_user_skill``
    与 plugin 层的 ``_validate_host_supervisor_mcp`` 跨模块搬移，收益低于
    引入循环依赖的风险。安装流程（``_command_agent_impl``）已改为能力查询。
    """
    root = root.resolve()
    if host_name == "opencode":
        return _check_opencode_config(root)

    if host_name == "dsh":
        patch_file = root / ".dsh" / "patch.yml"
        user_preset = _dsh_home() / ".agent-presets" / "bugx" / "agent.cordis.yml"
        ui_root = _dsh_home() / "plugins" / "bugx-ui"
        ui_files = ("package.json", "cordis.patch.yml", "index.js", "client.js")
        missing_ui = [name for name in ui_files if not (ui_root / name).is_file()]
        ui_detail = ""
        if missing_ui:
            ui_detail = "；缺少 DSH UI 包文件：" + "、".join(missing_ui)
        else:
            profile = _dsh_home() / "profiles" / "web" / "package.json"
            try:
                ui_manifest = json.loads(
                    (ui_root / "package.json").read_text(encoding="utf-8")
                )
                profile_manifest = (
                    json.loads(profile.read_text(encoding="utf-8"))
                    if profile.is_file() else None
                )
            except (OSError, json.JSONDecodeError):
                ui_manifest = profile_manifest = None
            ui_name = (
                ui_manifest.get("name") if isinstance(ui_manifest, dict) else None
            )
            dsh_client = (
                isinstance(ui_manifest, dict)
                and isinstance(ui_manifest.get("dsh"), dict)
                and isinstance(ui_manifest["dsh"].get("client"), dict)
                and "./client" in (ui_manifest.get("exports") or {})
            )
            bundles = (
                profile_manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
                if isinstance(profile_manifest, dict) else []
            )
            dependencies = (
                profile_manifest.get("dependencies", {})
                if isinstance(profile_manifest, dict) else {}
            )
            if not dsh_client or not isinstance(ui_name, str):
                ui_detail = "；DSH UI 包 manifest 缺少 dsh.client 或 ./client 导出"
            elif profile.is_file() and (ui_name not in bundles or ui_name not in dependencies):
                ui_detail = "；DSH UI 包未注册到 web profile，请重新运行 bugx install --agent dsh"
            elif not profile.is_file():
                ui_detail = "；已暂存 DSH UI 包（当前未发现 web profile）"
            else:
                ui_detail = "；DSH UI 图标包已注册到 web profile"
        if patch_file.is_file() or user_preset.is_file():
            content = ""
            if patch_file.is_file():
                content += patch_file.read_text(encoding="utf-8", errors="ignore")
            if user_preset.is_file():
                content += user_preset.read_text(encoding="utf-8", errors="ignore")
            has_supervisor = "bugx-supervisor" in content or "mcp-client" in content
            has_native_question = "@deepseek-ai/dsh-tool-ask-user" in content
            if has_supervisor and has_native_question:
                if missing_ui or ui_detail.startswith("；DSH UI 包未") or ui_detail.startswith("；DSH UI 包 manifest"):
                    return False, "已包含 Agent 通道，但" + ui_detail.lstrip("；")
                return True, "已包含 bugx-supervisor MCP、DSH ask_user_question 原生单选工具" + ui_detail
            missing = []
            if not has_supervisor:
                missing.append("bugx-supervisor MCP")
            if not has_native_question:
                missing.append("ask_user_question（@deepseek-ai/dsh-tool-ask-user）原生问答工具")
            return False, "缺少 " + "、".join(missing) + ui_detail + "；请运行 bugx install --agent dsh"
        return False, "缺少 .dsh/patch.yml 或全局预设；请运行 bugx install --agent dsh"

    if host_name == "claude":
        claude_mcp = root / ".mcp.json"
        claude_project = root / ".claude" / "mcp.json"
        candidates = (
            (claude_mcp, "flat"),
            (claude_project, "flat"),
        )
        ok, detail = _validate_host_supervisor_mcp(candidates, root)
        user_agents_dir = _claude_user_agents_dir()
        user_main = user_agents_dir / "bugx.md"
        user_worker = user_agents_dir / "bugx-worker.md"
        missing_user = [
            name for name, path in (("bugx", user_main), ("bugx-worker", user_worker))
            if not path.is_file()
        ]
        entry = root / ".claude" / "agents" / "bugx.md"
        has_entry = (entry.is_file() and not entry.is_symlink()) or not missing_user
        if ok and has_entry:
            user_status = "（含用户级智能体）" if not missing_user else ""
            return True, f"{detail}；Claude Code Agent 入口已就绪{user_status}"
        if ok and not has_entry:
            return True, detail
        if entry.is_file() and not entry.is_symlink():
            return True, "Claude Code Agent 入口已就绪 (.claude/agents/bugx.md)"
        if not missing_user:
            return True, f"Claude Code 用户级智能体已就绪（{user_agents_dir}）"
        return False, "缺少 Claude Code Agent 入口 .claude/agents/bugx.md；请运行 bugx install --agent claude"

    if host_name == "funide":
        project_ok, project_detail = _validate_host_supervisor_mcp(
            ((root / ".mcp.json", "flat"),), root,
        )
        native_mcp_path = _funide_profile_mcp_path()
        native_mcp_ok, native_mcp_detail = _validate_host_supervisor_mcp(
            ((native_mcp_path, "funide"),), root,
        )
        ok = project_ok and native_mcp_ok
        detail = project_detail if project_ok else project_detail
        if not native_mcp_ok:
            detail += "；FunIDE 用户级 MCP：" + native_mcp_detail
        project_entry = root / ".github" / "agents" / "bugx.agent.md"
        user_entry = _funide_user_agents_dir() / "bugx.agent.md"
        has_project = project_entry.is_file() and not project_entry.is_symlink()
        has_user = user_entry.is_file() and not user_entry.is_symlink()
        native_registry = _funide_data_dir() / "agents.json"
        has_native = False
        try:
            native_data = json.loads(native_registry.read_text(encoding="utf-8"))
            has_native = isinstance(native_data, dict) and any(
                isinstance(item, dict)
                and item.get("id") == "bugx"
                and _NATIVE_AGENT_OWNERSHIP_MARK in str(item.get("systemPrompt", ""))
                for item in native_data.get("agents", [])
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            has_native = False
        native_skill = _funide_user_skills_dir() / "bugx" / "SKILL.md"
        has_skill = False
        try:
            has_skill = native_skill.is_file() and "<!-- BugX managed FunIDE native skill -->" in native_skill.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            has_skill = False
        if ok and (has_project or has_user) and has_native and has_skill:
            surfaces = []
            if has_project:
                surfaces.append("项目级 .github/agents/bugx.agent.md")
            if has_user:
                surfaces.append(f"用户级 {user_entry}")
            surfaces.append(f"设置页原生注册 {native_registry}")
            surfaces.append(f"设置页原生技能 {native_skill}")
            surfaces.append(f"设置页用户 MCP {native_mcp_path}")
            return True, detail + "；FunIDE Agent/Skill/MCP 入口已就绪（" + "、".join(surfaces) + "）"
        if not has_project and not has_user:
            detail += "；缺少项目级 .github/agents/bugx.agent.md 与用户级 ~/.copilot/agents/bugx.agent.md"
        elif not has_native:
            detail += f"；缺少 FunIDE 设置页原生智能体注册 {native_registry}"
        elif not has_skill:
            detail += f"；缺少 FunIDE 设置页原生技能 {native_skill}"
        elif not native_mcp_ok:
            detail += f"；缺少 FunIDE 设置页用户 MCP {native_mcp_path}"
        elif not ok:
            detail += "；Agent 入口存在但不能替代 FunIDE MCP 配置"
        return False, detail + "；请运行 bugx install --agent funide"

    if host_name == "zcode":
        # PE-20261004-003：ZCode 只读用户级 ~/.zcode/cli/config.json 的
        # mcp.servers（无工作区级 MCP 作用域），注册与校验都必须落在那里；
        # 用户级配置只保留插件目录登记，避免同名全局服务器覆盖项目根。
        ok, detail = _validate_zcode_global_supervisor_mcp(root)
        if ok:
            return ok, detail
        # 0.7.22：主/worker 子智能体均收敛为仅用户级注册，doctor 改查
        # 用户级注册面（storage.dir/agents，默认 ~/.zcode/agents）。
        user_agents_dir = _zcode_user_agents_dir()
        user_main = user_agents_dir / "bugx.md"
        user_worker = user_agents_dir / "bugx-worker.md"
        missing = [
            name for name, path in (("bugx", user_main), ("bugx-worker", user_worker))
            if not path.is_file()
        ]
        if not missing:
            return False, detail + "；用户级子智能体已注册但不能替代 MCP 配置"
        return False, (
            detail
            + "；缺少用户级子智能体 " + "、".join(missing)
            + f"（{user_agents_dir}，重跑 bugx install --agent zcode 补齐）"
        )

    # 宿主专属安装诊断：按约定模块名调用 ``plugin_<host>.validate_install`` 钩子。
    # 通用层不再持有「某宿主的入口在哪、MCP 写在哪个作用域」这类事实。
    validate_install = engine_host_cli_hook(host_name, "validate_install")
    if validate_install is not None:
        return validate_install(root)

    if host_name == "trea":
        # DOC-169 / DOC-210：补全 Trea 的 MCP 校验分支（项目 flat 候选 + Agent 入口，支持 Trae 与 Trae CN 变体）。
        candidates = (
            (root / ".trea" / "mcp.json", "flat"),
            (root / ".trae" / "mcp.json", "flat"),
            (root / ".trae-cn" / "mcp.json", "flat"),
            (root / ".mcp.json", "flat"),
        )
        ok, detail = _validate_host_supervisor_mcp(candidates, root)
        trea_entry_candidates = (
            root / ".trea" / "agents" / "bugx.md",
            root / ".trae" / "agents" / "bugx.md",
            root / ".trae-cn" / "agents" / "bugx.md",
        )
        has_entry = any(p.is_file() for p in trea_entry_candidates)
        if ok and has_entry:
            return True, detail + "；Trea Agent 入口已就绪"
        if not has_entry:
            detail += "；缺少 .trea/agents/bugx.md（或 .trae/.trae-cn 变体）"
        elif not ok:
            detail += "；Agent 入口存在但不能替代 Trea MCP 配置"
        return False, detail + "；请运行 bugx install --agent trea"

    if host_name == "pi":
        ext_manifest = root / ".pi" / "extensions" / "bugx" / "manifest.json"
        skill_file = root / ".pi" / "skills" / "bugx" / "SKILL.md"
        if ext_manifest.is_file() and skill_file.is_file():
            return True, "Pi 扩展与技能包均存在，扩展已包含 MCP / stdio 适配支持"
        return False, "Pi 扩展或技能包缺失；请运行 bugx install --agent pi"

    if host_name == "chatgpt":
        marketplace = root / ".agents" / "plugins" / "marketplace.json"
        if marketplace.is_file():
            return True, "ChatGPT Marketplace 清单已登记 BugX 插件与资源契约"
        return False, "缺少 .agents/plugins/marketplace.json；请运行 bugx install --agent chatgpt"

    if host_name == "workbuddy":
        from bugx_cli.host_adapters import WORKBUDDY_PROJECT_PATHS

        missing = [relative for relative in WORKBUDDY_PROJECT_PATHS
                   if not (root / relative).is_file()]
        if missing:
            return False, "缺少 " + "、".join(missing) + "；请运行 bugx install --agent workbuddy"
        connector_root = root / ".workbuddy" / "connector" / "bugx"
        connector_files = (
            connector_root / "connector-meta.json",
            connector_root / "icon.png",
            connector_root / "skills" / "bugx" / "SKILL.md",
        )
        if any(not path.is_file() for path in connector_files):
            return False, "缺少 WorkBuddy Connector 图标包；请运行 bugx install --agent workbuddy"
        try:
            connector_meta = json.loads(
                (connector_root / "connector-meta.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            connector_meta = None
        if not isinstance(connector_meta, dict) or connector_meta.get("type") != "skill-only":
            return False, "WorkBuddy Connector 元数据无效；请运行 bugx install --agent workbuddy"
        user_ok, user_detail = _check_workbuddy_user_skill(root, repair=repair)
        if not user_ok:
            return False, user_detail

        # DOC-181 方案 2：WorkBuddy 沙箱 Python 运行态一致性检查与自愈
        from bugx_cli.host_adapters import workbuddy_cli_venv, workbuddy_cli_venv_version
        venv = workbuddy_cli_venv()
        venv_detail = ""
        if venv is not None:
            from ..infra.engine_installer import _contract_version
            venv_ver = workbuddy_cli_venv_version(venv)
            if venv_ver and _contract_version(venv_ver) != _contract_version(__version__):
                if repair:
                    synced, sync_msg = _sync_workbuddy_private_venv(
                        asset_root(), target_version=__version__,
                    )
                    if synced:
                        venv_detail = f"；WorkBuddy 沙箱运行态已自动升级至 {__version__}"
                    else:
                        return False, f"WorkBuddy 沙箱运行态升级失败: {sync_msg}"
                else:
                    return False, (
                        f"WorkBuddy 沙箱运行态版本滞后: {venv} (包版本 {venv_ver} != 当前 CLI {__version__})；"
                        "请运行 `bugx doctor --repair` 或 `bugx install --agent workbuddy` 自动同步"
                    )
            elif venv_ver:
                venv_detail = f"；WorkBuddy 沙箱运行态就绪 ({venv_ver})"

        return True, "WorkBuddy `.workbuddy` 与 CodeBuddy 兼容 Skill 入口、Connector 图标包均已就绪；" + user_detail + venv_detail

    if host_name == HOST_NAME_QODER:
        qoder_mcp = root / ".qoder" / "mcp.json"
        project_mcp = root / ".mcp.json"
        candidates = (
            (qoder_mcp, "flat"),
            (project_mcp, "flat"),
        )
        from .plugin_qoder import _clean_legacy_qoder_plugin_and_skills, _qoder_user_agents_dir

        legacy_warning = ""
        if repair:
            _clean_legacy_qoder_plugin_and_skills()
        else:
            from .plugin_qoder import (
                _qoder_user_mcp_path, _qoder_user_plugins_dir,
                _qoder_user_settings_path, _qoder_user_skills_dir,
            )
            legacy_paths = (
                _qoder_user_skills_dir() / "bugx",
                _qoder_user_plugins_dir() / "cache" / "bugx-agent",
            )
            has_legacy = any(path.exists() or path.is_symlink() for path in legacy_paths)
            registry_paths = (
                _qoder_user_plugins_dir() / "installed_plugins_v2.json",
                _qoder_user_plugins_dir() / "installed_plugins.json",
                _qoder_user_settings_path(),
                _qoder_user_mcp_path(),
            )
            for path in registry_paths:
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if path.name.startswith("installed_plugins"):
                    has_legacy |= "bugx@bugx-agent" in (payload.get("plugins") or {}) if isinstance(payload, dict) else False
                elif path == _qoder_user_settings_path():
                    has_legacy |= "bugx@bugx-agent" in (payload.get("enabledPlugins") or {}) if isinstance(payload, dict) else False
                else:
                    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
                    has_legacy |= isinstance(servers, dict) and any(
                        name in {"bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"}
                        or (isinstance(value, dict) and any("bugx" in str(arg).lower()
                                                           for arg in value.get("args", [])))
                        for name, value in servers.items()
                    )
            if has_legacy:
                legacy_warning = "；发现用户级 Qoder 遗留配置，可运行 `bugx doctor --repair` 清理"

        ok, detail = _validate_host_supervisor_mcp(candidates, root)
        user_agents_dir = _qoder_user_agents_dir()
        user_main = user_agents_dir / "bugx.md"
        user_worker = user_agents_dir / "bugx-worker.md"
        project_agent = root / ".qoder" / "agents" / "bugx.md"

        has_project_agent = project_agent.is_file() and not project_agent.is_symlink()
        missing_user = [
            name for name, path in (("bugx", user_main), ("bugx-worker", user_worker))
            if not path.is_file()
        ]

        if repair:
            from .plugin_qoder import _ensure_qoder_user_agents
            _ensure_qoder_user_agents(root, source=asset_root())
            if not ok:
                write_host_mcp_config(root, host_name)
                ok, detail = _validate_host_supervisor_mcp(candidates, root)
            missing_user = [
                name for name, path in (("bugx", user_main), ("bugx-worker", user_worker))
                if not path.is_file()
            ]

        if ok and (has_project_agent or not missing_user):
            user_status = "（含用户级智能体）" if not missing_user else ""
            return True, f"{detail}；Qoder Agent 入口已就绪{user_status}{legacy_warning}"
        if not ok:
            return False, f"{detail}；请运行 bugx install --agent qoder{legacy_warning}"
        return False, "缺少 Qoder Agent 入口（.qoder/agents/bugx.md 或 ~/.qoder/agents/bugx.md）；请运行 bugx install --agent qoder" + legacy_warning

    return False, f"未知宿主 MCP 校验规则: {host_name}"


def _detected_host_name(root: Path | None = None) -> str:
    """当前宿主的规范名；无法识别时返回 ``unknown``（绝不猜成 opencode）。

    身份唯一权威是进程祖先链探查（DOC-217 D211.3 已废除 ``BUGX_HOST`` 覆盖），
    因此这里直接问引擎适配器，不读环境变量、也不看「哪个宿主装了 CLI」。
    """
    try:
        from core.host_runtime.adapters import detect_host

        adapter = detect_host(root or Path.cwd())
    except Exception:  # pragma: no cover - 探查失败即 unknown，由调用方如实上报
        return "unknown"
    name = str(getattr(adapter, "name", "") or "").strip()
    return name or "unknown"


def _check_opencode_config(root: Path) -> tuple[bool, str]:
    """Validate the recommended MCP entries for an installed OpenCode bundle."""
    path = root.resolve() / CONFIG_NAME
    if path.is_symlink() or not path.is_file():
        return False, f"缺少 {CONFIG_NAME}；请重新运行 bugx install --agent opencode"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"{CONFIG_NAME} 无法解析：{exc}"
    if not isinstance(payload, dict):
        return False, f"{CONFIG_NAME} 顶层必须是 JSON 对象"
    try:
        servers = _opencode_v2_servers(payload, path)
    except CliError as exc:
        return False, str(exc)
    plugins = payload.get("plugins")
    plugin_ready = isinstance(plugins, list) and _OPENCODE_REQUIRED_PLUGIN in plugins
    # lock 停用的 server（SPIKE-008-01 未通过）不要求启用，但配置必须与
    # lock 姿态一致：不得在宿主侧自行启用。
    lock_disabled = _lock_disabled_servers()
    # 只有**内建通道**必须启用；外部能力（playwright/dbhub/redis…）按需启用——
    # 它们需要 runtime 载荷与凭据，宿主侧默认停用是正常姿态（F-234-03 同类修复：
    # 此前这里硬编码豁免 {"playwright", "postmcp"}，新增能力即漏判）。
    active_required = [
        name for name in OPENCODE_LOCAL_MCP if name not in lock_disabled
    ]
    missing = [
        name for name in active_required
        if not isinstance(servers.get(name), dict)
    ]
    disabled = [
        name for name in active_required
        if _mcp_entry_state(name, servers.get(name)) == "disabled"
    ]
    direct_tools_hidden = [
        name for name in OPENCODE_ALL_REQUIRED_MCP
        if isinstance(servers.get(name), dict)
        if (
            _mcp_entry_state(name, servers[name]) == "configured"
            and servers[name].get("codemode") is not False
        )
    ]
    incompatible = _incompatible_opencode_mcp_names(servers)
    custom = [
        name for name in OPENCODE_ALL_REQUIRED_MCP
        if name in servers and name not in missing
        and _mcp_entry_state(name, servers[name]) == "custom"
    ]
    conflicts = [
        name for name in sorted(lock_disabled)
        if isinstance(servers.get(name), dict)
        and servers[name].get("disabled") is not True
    ]
    details = []
    if not plugin_ready:
        details.append(f"plugins 缺少 {_OPENCODE_REQUIRED_PLUGIN}")
    if missing:
        details.append("缺少 " + ", ".join(missing))
    if disabled:
        details.append("已禁用 " + ", ".join(disabled))
    if direct_tools_hidden:
        details.append(
            "Code Mode 未关闭，BugX 工具不会作为原生工具直接暴露: "
            + ", ".join(direct_tools_hidden)
        )
    if incompatible:
        details.append(
            "旧版 BugX MCP 命令已拒绝自动迁移，请手动删除对应 mcp 条目后重新安装: "
            + ", ".join(incompatible)
        )
    if custom:
        details.append("自定义/未验证 " + ", ".join(custom))
    if conflicts:
        details.append(
            "lock 已停用但配置仍启用（启动会被 runtime 拒绝）: "
            + ", ".join(conflicts)
        )
    if details:
        return False, f"{CONFIG_NAME} MCP 配置不完整：" + "；".join(details)
    note = ""
    if lock_disabled:
        note = f"（{'、'.join(sorted(lock_disabled))} 按 mcp-lock.json 停用，待 SPIKE-008-01 证明）"
    return True, (f"{CONFIG_NAME} 已包含当前离线 runtime launcher 版本的 "
                  f"Playwright、postmcp、Context7，以及 host/worker 两个本地 BugX Supervisor 通道{note}")


def _merge_host_records(path: Path, installed: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge newly installed hosts without discarding earlier host entries."""
    if path.is_symlink():
        raise CliError(_i18n_exc("agent.hosts_symlink_refused", f"拒绝写入符号链接宿主清单: {path}", path=path))
    merged: dict[str, dict[str, object]] = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CliError(_i18n_exc("agent.hosts_unreadable", f"宿主清单无法读取，拒绝覆盖: {path}: {exc}", path=path, exc=exc)) from exc
        previous = payload.get("installed") if isinstance(payload, dict) else None
        if not isinstance(previous, list):
            raise CliError(_i18n_exc("agent.hosts_missing_installed", f"宿主清单缺少 installed 数组，拒绝覆盖: {path}", path=path))
        for item in previous:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise CliError(_i18n_exc("agent.hosts_invalid_entry", f"宿主清单包含无效记录，拒绝覆盖: {path}", path=path))
            merged[str(item["name"])] = item
    for item in installed:
        merged[str(item["name"])] = item
    return list(merged.values())


def _initialize_lesson_ledger(root: Path, source: Path) -> bool:
    """Create the lesson seed and runtime store during explicit project init.

    Startup must never call an ``ensure`` operation: a missing runtime store in
    an already-initialized project is a fail-closed condition.  The CLI init
    command is the one explicit bootstrap boundary allowed to create it.
    """
    layout = _layout_module(root).require(root)
    # DOC-004 Layout 2：静态协议脚本只来自全局引擎；项目 .bugx 仅保存账本。
    ledger_script = _script(root, "lesson_ledger.py")
    ledger_path = layout.knowledge_seed
    seed_path = source / "lesson-ledger.json"
    if not ledger_script.is_file():
        raise CliError(_i18n_exc("agent.lesson_ledger_missing", "初始化错题集失败：缺少 lesson_ledger.py"))
    project_marker = layout.knowledge_root / "manifest.json"
    # `bugx install` is intentionally supported from HOME for user-level
    # host registration.  In that mode the project and global knowledge roots
    # are the same directory; the global manifest must not be mistaken for a
    # project manifest, otherwise a missing project-named SQLite file blocks
    # every host install.  Keep the project ledger branch disabled and let the
    # explicit global-store branch below report/recover its own state.
    knowledge_roots_collide = (
        layout.knowledge_root.resolve() == layout.global_knowledge_root.resolve()
    )
    if knowledge_roots_collide:
        # 静默跳过会让 40%→60% 这一段看起来「做了事但其实没做」。显式说明一次，
        # 否则用户只能看到进度前进，却不知道项目错题集分支被整体关闭了。
        print(
            f"[bugx] 提示：项目知识根与全局知识根指向同一目录（{layout.knowledge_root}），"
            f"本次跳过项目错题集初始化，错题集以全局库为准。",
            file=sys.stderr,
        )
    elif project_marker.is_file() and _knowledge_marker_is_global(project_marker):
        # 两个知识根没有塌缩，项目清单路径上却是全局清单：这是配置异常。
        # 必须 fail-closed —— 函数尾部会用 `scope=project` 重写 marker，继续走下去
        # 等于把全局清单覆盖成项目清单，破坏全局知识库的身份。
        raise CliError(
            f"初始化错题集失败：项目知识清单 {project_marker} 声明 scope=global，"
            f"拒绝用项目清单覆盖全局清单；请检查 BUGX_GLOBAL_KNOWLEDGE_ROOT 与项目根配置"
        )
    if not knowledge_roots_collide and project_marker.is_symlink():
        raise CliError(_i18n_exc("agent.project_knowledge_symlink_refused", "初始化错题集失败：拒绝使用符号链接项目知识清单"))
    if not knowledge_roots_collide and not ledger_path.exists():
        if not seed_path.is_file() or seed_path.is_symlink():
            raise CliError(_i18n_exc("agent.project_lesson_seed_missing", "初始化错题集失败：缺少内置 lesson-ledger.json 种子"))
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(seed_path, ledger_path)
    if not knowledge_roots_collide and ledger_path.is_symlink():
        raise CliError(_i18n_exc("agent.project_ledger_symlink_refused", "初始化错题集失败：拒绝使用符号链接账本"))
    project_db = layout.knowledge_db
    if not knowledge_roots_collide and project_marker.is_file() and not project_db.is_file():
        raise CliError(_i18n_exc("agent.project_sqlite_missing_rebuild_refused", "项目错题集 SQLite 运行库缺失；拒绝从 JSON 种子静默重建，请从备份恢复或执行显式恢复流程"))
    if not knowledge_roots_collide:
        try:
            process = subprocess.run(
                [sys.executable, str(ledger_script), "init", "--ledger", str(ledger_path), "--json"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                env=_python_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CliError(_i18n_exc("agent.project_lesson_init_exc", f"初始化错题集失败: {exc}", exc=exc)) from exc
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()[:800]
            raise CliError(_i18n_exc("agent.project_lesson_init_failed", f"初始化错题集失败: {detail or 'lesson_ledger init 返回非 0'}", detail=detail or 'lesson_ledger init 返回非 0'))
        project_db.chmod(0o600)
        ledger_path.chmod(0o600)
        _write_private_json(project_marker, {
            "schema_version": 1,
            "scope": "project",
            "authority": "sqlite",
            "seed": ledger_path.name,
            "database": project_db.name,
            "initialized_at": time.time(),
        })

    # The global ledger is shared across projects, so it is initialized only
    # when the user's global store does not exist at all.  Once its manifest
    # exists, a missing SQLite file is a data-loss condition and must never be
    # silently rebuilt from the (possibly stale) JSON seed.
    global_root = layout.global_knowledge_root
    global_root.mkdir(parents=True, exist_ok=True)
    global_seed = layout.global_knowledge_seed
    global_db = layout.global_knowledge_db
    global_marker = global_root / "manifest.json"
    if any(path.is_symlink() for path in (global_marker, global_seed, global_db)):
        raise CliError(_i18n_exc("agent.global_knowledge_symlink_refused", "初始化全局错题集失败：拒绝使用符号链接知识文件"))
    if not global_marker.exists() and not global_db.exists():
        if not global_seed.exists():
            shutil.copy2(seed_path, global_seed)
        process = subprocess.run(
            [sys.executable, str(ledger_script), "init", "--ledger", str(global_seed), "--json"],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment(),
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()[:800]
            raise CliError(_i18n_exc("agent.global_lesson_init_failed", f"初始化全局错题集失败: {detail or 'lesson_ledger init 返回非 0'}", detail=detail or 'lesson_ledger init 返回非 0'))
        global_seed.chmod(0o600)
        global_db.chmod(0o600)
        _write_private_json(global_marker, {
            "schema_version": 1,
            "scope": "global",
            "authority": "sqlite",
            "read_only_for_agents": True,
            "seed": global_seed.name,
            "database": global_db.name,
            "initialized_at": time.time(),
        })
    elif global_marker.exists() and not global_db.is_file():
        # 全局为可选加速器（doctor 已标为可选），缺失时不阻断项目初始化，仅告警
        # 原 fail-closed 会导致 `bugx init` 在 C:全局污染的 Windows 跨盘项目中固定 exit 2
        print(
            "⚠ 全局错题集 SQLite 缺失（已跳过全局继承，项目初始化不受影响）。"
            "如需恢复请执行：bugx lesson rebuild --from-seed --confirm-data-loss"
            f" --ledger {global_seed}",
            file=sys.stderr,
        )
    elif global_db.is_file():
        try:
            global_db.chmod(0o600)
            if global_seed.is_file():
                global_seed.chmod(0o600)
        except (PermissionError, OSError):
            pass
        validation = subprocess.run(
            [sys.executable, str(ledger_script), "validate", "--ledger", str(global_seed), "--json"],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment(),
        )
        if validation.returncode != 0:
            detail = (validation.stderr or validation.stdout).strip()[:800]
            if "schema_version" in detail:
                # 全局库是可选只读加速器；旧 schema 需要显式迁移，不能静默改写
                # 用户拥有的全局账本，也不能因此阻断项目初始化。
                print(
                    "⚠ 全局错题集需要显式迁移（项目库不受影响）。"
                    "迁移命令：bugx lesson migrate "
                    f"--ledger {layout.global_knowledge_seed}",
                    file=sys.stderr,
                )
            else:
                raise CliError(_i18n_exc("agent.global_ledger_check_failed", f"全局错题集校验失败: {detail or 'SQLite 无效'}", detail=detail or 'SQLite 无效'))
        if not global_marker.exists():
            _write_private_json(global_marker, {
                "schema_version": 1,
                "scope": "global",
                "authority": "sqlite",
                "read_only_for_agents": True,
                "seed": global_seed.name,
                "database": global_db.name,
                "initialized_at": time.time(),
                "adopted_existing_store": True,
            })
    return True


def _inherit_project_knowledge(root: Path) -> dict[str, Any]:
    """Seed a fresh project with active patterns from the shared global ledger."""
    layout = _layout_module(root).require(root)
    script = _script(root, "lesson_ledger.py")
    process = subprocess.run(
        [sys.executable, str(script), "inherit",
         "--ledger", str(layout.knowledge_seed),
         "--global-ledger", str(layout.global_knowledge_seed), "--json"],
        cwd=root.resolve(), capture_output=True, text=True, check=False,
        timeout=30, env=_python_environment(),
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout).strip()[:800]
        if "schema_version" in detail:
            # 全局库是可选的只读加速器；旧 schema 需要显式迁移，不能让
            # 一个全局知识库的维护问题阻断新项目初始化。
            return {"imported": 0, "skipped": 0, "status": "schema_pending"}
        raise CliError(_i18n_exc("agent.project_inherit_global_failed", f"项目错题集继承全局知识失败: {detail or 'inherit 返回非 0'}", detail=detail or 'inherit 返回非 0'))
    try:
        payload = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CliError(_i18n_exc("agent.project_inherit_invalid_json", "项目错题集继承全局知识失败: inherit 输出不是有效 JSON")) from exc
    return payload if isinstance(payload, dict) else {}
