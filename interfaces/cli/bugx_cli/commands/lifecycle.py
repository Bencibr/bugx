from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import *
from ..infra.engine_installer import refresh_i18n_after_engine_swap
from ..host_adapters import ENTRY_MULTI_PATH, HOST_ADAPTERS
from ..hosts.registry import (
    engine_host_cli_hook,
    engine_host_user_agent_surfaces,
)
from .agent import (
    _dsh_home,
    _global_opencode_agents_dir,
    _inherit_project_knowledge,
    _initialize_lesson_ledger,
    _initialize_life_state,
    _unlink_global_pi_skill,
)
from .plugin import (
    _bootstrap_global_knowledge,
    _chatgpt_global_plugin_root,
    _chatgpt_marketplace_name,
    _clean_reset_entries,
    _external_reset_backup_root,
    _external_uninstall_backup_root,
    _host_project_mcp_config_target,
    _is_bugx_marketplace_source,
    _move_reset_entries,
    _read_hosts_record,
    _remove_chatgpt_marketplace_entry,
    _remove_claude_mcp_entry,
    _remove_dsh_project_files,
    _remove_dsh_user_preset,
    _remove_empty_parents,
    _remove_host_mcp_entry,
    _remove_opencode_mcp_entry,
    _remove_owned_record,
    _remove_qoder_mcp_entry,
    _reset_active_blockers,
    _reset_confirmation,
    _reset_entries,
    _restore_reset_entries,
    _sweep_unrecorded_host_entry,
    _SWEEPABLE_SKILL_ADAPTERS,
    _zcode_config_path,
    _zcode_user_agents_dir,
)


def _assemble_project_materials(root: Path, source: Path, layout: Any) -> None:
    """统一装配项目开工材料：开工准备记录与覆盖清单引导。"""
    for name, target in (
        ("prep-record.md", layout.prep_record if hasattr(layout, "prep_record") else layout.require(root).prep_record),
        ("module-coverage.md", (layout.state_root if hasattr(layout, "state_root") else layout.require(root).state_root) / "module-coverage.md"),
    ):
        source_record = source / name
        if source_record.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_record, target)
            target.chmod(0o600)

    try:
        _ensure_core_importable()
        from core.runtime import prep_validate
        prep_validate.ensure_prep_record(root)
    except Exception:
        pass

    try:
        _ensure_core_importable()
        from core.coverage import coverage_bootstrap
        coverage = coverage_bootstrap.inspect(root)
        if not coverage.get("ready") and not coverage.get("blocked"):
            coverage_bootstrap.bootstrap(root, confirmed=True)
    except Exception:
        pass


def _ensure_project_startup_todo(root: Path) -> Path:
    """生成或确保项目级开工总览 .bugx/startup-todo.md 存在且符合契约。"""
    target = root / ".bugx" / "startup-todo.md"
    if target.is_symlink():
        raise OSError(f"拒绝写入软链接目标：{target}")
    if not target.is_file() or target.stat().st_size == 0:
        iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        content = (
            "# BugX 启动总览 TODO\n\n"
            f"- 项目：`{root}`\n"
            "- 宿主/界面：`unknown` / `cli` / `idle`\n"
            "- 当前阶段：`environment`\n"
            "- 当前门禁：`none`\n"
            f"- 最后校准：`{iso_now}`\n\n"
            "- [x] 1. 环境检测与前置校验\n"
            "  - 校验 Python 版本、核心静态文件完整性与项目运行布局\n"
            "- [~] 2. 启动配置与策略确认\n"
            "  - 确认云端同步、审查方式、轮询策略、修复模式与提交策略\n"
            "- [ ] 3. 工具与执行能力准备\n"
            "  - 确认 MCP/探针连通性与项目上下文加载\n"
            "- [ ] 4. 覆盖勘察与漏洞挖掘\n"
            "  - 生成覆盖清单并调度 Worker 并发挖掘验证\n"
            "- [ ] 5. 同步与知识复盘\n"
            "  - 沉淀错题集、更新经验账本与云端同步\n"
            "- [ ] 6. 报告与结论共享\n"
            "  - 生成 Round 审计报告与证据闭环\n"
            "- [ ] 7. 安全收尾与生命周期结算\n"
            "  - 执行受控提交并结算运行状态\n\n"
            "> 本文件是给用户和 Agent 查看全局进度的派生视图，不是账本、覆盖清单、Finding 来源或完成判定器。\n"
            "> 正式 Round 开始后，详细进度位于当前生命周期 `state/audit-todo.md`。\n"
            "> 阶段专精提示词（Harness）：可通过 `bugx engine --root . prompt --stage <gate|explore|mutate|repair>` 或 MCP `bugx_get_stage_harness` 动态获取。\n"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return target



def _has_bugx_project_markers(bugx: Path) -> bool:
    """Check if .bugx directory contains recognizable BugX runtime artifacts."""
    known_markers = (
        "manifest.json", "current.json", "config.json", "hosts.json",
        "pollings", "state", "logs", "artifacts", "index", "knowledge",
        "backups", "locks", "resume_ticket.json", "prep-record.md",
        "module-coverage.md", "init.lock", "audit-supervisor.json",
        "bugx-life.json", "finding-ledger.json",
    )
    for name in known_markers:
        p = bugx / name
        if p.exists() or p.is_symlink():
            return True
    return False


def _snapshot_reset_paths(root: Path) -> set[Path]:
    """Record lexical paths below the project runtime without following links."""
    paths: set[Path] = set()
    if not root.exists() or root.is_symlink():
        return paths
    for current, directories, files in os.walk(root, followlinks=False):
        parent = Path(current)
        paths.add(parent)
        for name in (*directories, *files):
            paths.add(parent / name)
        directories[:] = [name for name in directories if not (parent / name).is_symlink()]
    return paths


def _remove_reset_created_paths(root: Path, existing: set[Path]) -> None:
    """Remove only paths created by a failed reset; refuse to traverse links."""
    created = _snapshot_reset_paths(root) - existing
    for path in sorted(created, key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            path.rmdir()


def _auto_abort_residual_runtime(root: Path, blockers: list[str]) -> bool:
    """残留运行态自愈：先走引擎安全中止，让状态回到可重置的确定态。

    为什么必须有这一步（2026-11-02 实测）：``pre`` 成功后 ``runtime.json`` 会留下
    ``active=true / state=running / pid=null``。此时进程早已退出，但 blocker 判定
    会因「pid 字段缺失且 mtime 很新」把它当活任务，要求用户自己保存 ``blob``、
    抄下 ``bugx engine ... abort`` 再重试——而这正是工具应当自己做完的确定性收口。
    中止是引擎的**受支持入口**（在安全边界结算并释放租约），因此这里只做编排：

    * 只对 ``runtime.json`` 类 blocker 触发，锁被占用（真有进程持有）不碰，
      否则会与正在写入的会话互相破坏；
    * 中止失败/超时只是「自愈未成功」，由调用方回落到带命令提示的报错，
      绝不让 reset 静默继续。
    """
    if not blockers:
        return False
    # 锁被占用说明确实有活进程在写：交给用户/--force，不自动中止。
    if any("锁被占用" in item for item in blockers):
        return False
    script = root.resolve() / ".bugx"
    if not script.is_dir():
        return False
    try:
        launcher = global_engine_root() / "launch_bugx.py"
        if not launcher.is_file():
            return False
        print("[bugx] 检测到残留运行态，正在自动安全中止（abort）后重置…")
        completed = subprocess.run(
            [sys.executable, str(launcher), "abort", "--reason", "auto: reset"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            env=_reset_abort_env(root),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if completed.returncode != 0:
        return False
    # 中止写入是异步安全的；给状态一次落盘机会再复查 blocker。
    time.sleep(0.2)
    return True


def _reset_abort_env(root: Path) -> dict[str, str]:
    """中止子进程的环境：绑定当前项目根与全局引擎，避免解析到别的项目。"""
    env = dict(os.environ)
    env["BUGX_PROJECT_ROOT"] = str(root.resolve())
    try:
        env["BUGX_GLOBAL_ENGINE_ROOT"] = str(global_engine_root())
    except (OSError, ValueError):
        pass
    env["BUGX_CLI_VERSION"] = __version__
    return env


def command_reset(args: argparse.Namespace) -> int:
    """Archive and recreate project runtime or global knowledge in one action."""
    root = args.root.resolve()
    global_options = (
        args.reset_identity, args.reset_credentials,
        args.reset_mcp_runtime, args.reset_backups,
    )
    if args.scope == "project" and any(global_options):
        raise CliError(_i18n_exc("lifecycle.reset_flags_scope_global_only", "--reset-identity/--reset-credentials/--reset-mcp-runtime/--reset-backups 仅适用于 --scope global"))
    if args.scope == "global" and args.reset_knowledge:
        raise CliError(_i18n_exc("lifecycle.reset_knowledge_scope_project_only", "--reset-knowledge 仅适用于 --scope project；global 默认重置全局知识库"))
    if args.scope == "project":
        bugx = root / ".bugx"
        if bugx.is_symlink() or not bugx.is_dir():
            raise CliError(_i18n_exc("lifecycle.project_not_initialized", "项目未初始化或 .bugx 不安全；请先运行 bugx init"))
        manifest = bugx / "manifest.json"
        force = getattr(args, "force", False)
        if manifest.is_symlink() or (manifest.exists() and not manifest.is_file()):
            raise CliError(_i18n_exc("lifecycle.manifest_missing_for_reset", "项目缺少安全的 .bugx/manifest.json，拒绝猜测重置范围"))
        if not manifest.exists() and not force and not _has_bugx_project_markers(bugx):
            raise CliError(_i18n_exc("lifecycle.manifest_missing_for_reset", "项目缺少安全的 .bugx/manifest.json，拒绝猜测重置范围"))
        if _legacy_project_core_present(root):
            raise CliError(
                "当前项目仍有旧的 .bugx/core 可执行入口；请先运行 "
                "`bugx init --root .`，旧入口会被直接删除"
            )
        blockers = _reset_active_blockers(root)
        if blockers and not force:
            # 自愈：残留运行态是**可确定性收口**的状态，不该让用户手抄下一句命令。
            # 有活进程时先走引擎的安全中止（它会在安全边界结算），再复查一次；
            # 仍然存在才报错，并把可直接执行的命令写进提示。
            if _auto_abort_residual_runtime(root, blockers):
                blockers = _reset_active_blockers(root)
        if blockers and not force:
            raise CliError(
                _i18n_exc(
                    "lifecycle.running_tasks_block_reset",
                    "检测到仍在写入的运行中任务，已尝试自动安全中止但未成功："
                    + "；".join(blockers)
                    + "。请手动执行 `bugx engine --root . abort --reason reset` 后重试，"
                    "或加 --force 强制重置",
                    blockers="；".join(blockers),
                )
            )
        include_knowledge = bool(args.reset_knowledge)
        targets = _reset_entries(root, include_project_knowledge=include_knowledge)
        if manifest.is_file() and force:
            try:
                manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
                layout_module = _layout_module(root)
                expected_project_id = getattr(layout_module, "_project_id", lambda r: "")(root)
                if (not isinstance(manifest_data, dict)
                        or manifest_data.get("schema_version") != getattr(layout_module, "SCHEMA_VERSION", 1)
                        or (expected_project_id and manifest_data.get("project_id") != expected_project_id)):
                    targets.append((manifest, "project/manifest.json"))
            except (OSError, json.JSONDecodeError, ValueError):
                targets.append((manifest, "project/manifest.json"))
        no_backup = bool(getattr(args, "no_backup", False))
        archive = None if no_backup else _external_reset_backup_root(root, "project")
        summary = (
            "项目级重置将清理并重建轮询、生命周期、状态、日志、证据、索引和备份（跳过备份）；"
            if no_backup else
            "项目级重置将归档并重建轮询、生命周期、状态、日志、证据、索引和备份；"
        ) + (
            "同时重置项目错题集/经验集。" if include_knowledge
            else "项目错题集/经验集将保留。"
        ) + (
            f"备份位置：{archive}" if archive else ""
        )
        _reset_confirmation(summary, yes=args.yes, dry_run=args.dry_run, root=root)
        if args.dry_run:
            payload = {
                "status": "DRY_RUN", "scope": "project", "root": str(root),
                "reset_knowledge": include_knowledge,
                "targets": [relative for _, relative in targets],
                "backup": str(archive) if archive else None,
            }
        else:
            _close_bugx_logging()
            transaction_archive = archive or Path(
                tempfile.mkdtemp(prefix="bugx-reset-rollback-")
            ).resolve()
            if archive is not None:
                transaction_archive.mkdir(parents=True, exist_ok=False)
            moved: list[tuple[Path, Path]] = []
            preexisting: set[Path] | None = None
            try:
                moved = _move_reset_entries(targets, transaction_archive)
                preexisting = _snapshot_reset_paths(root / ".bugx")
                layout = _layout_module(root)
                layout.initialize(root, bugx_version=__version__)
                source = asset_root()
                _assemble_project_materials(root, source, layout)
                if include_knowledge:
                    _initialize_lesson_ledger(root, source)
                    _inherit_project_knowledge(root)
                _initialize_life_state(root)
                if archive is not None:
                    _write_private_json(archive / "reset-manifest.json", {
                        "scope": "project", "root": str(root),
                        "reset_knowledge": include_knowledge,
                        "created_at": time.time(),
                        "entries": [str(destination.relative_to(archive))
                                    for _, destination in moved],
                    })
                payload = {
                    "status": "RESET", "scope": "project", "root": str(root),
                    "reset_knowledge": include_knowledge,
                    "backup": str(archive) if archive else None,
                    "archived": [str(destination.relative_to(transaction_archive))
                                 for _, destination in moved] if archive else [],
                    "recreated": ["polling-000001", "lifecycle-000001", "life.json"],
                }
            except BaseException as reset_error:
                try:
                    if preexisting is not None:
                        _assert_no_symlink_components(root / ".bugx")
                        _remove_reset_created_paths(root / ".bugx", preexisting)
                        _assert_no_symlink_components(root / ".bugx")
                    _restore_reset_entries(moved)
                except BaseException as rollback_error:
                    raise CliError(
                        f"项目重置失败且回滚不完整；原始数据保留于 {transaction_archive}: "
                        f"{rollback_error}"
                    ) from reset_error
                if preexisting is None and any(transaction_archive.iterdir()):
                    raise CliError(
                        f"项目条目移动失败且未能确认回滚状态；staging 保留于 {transaction_archive}"
                    ) from reset_error
                shutil.rmtree(transaction_archive, ignore_errors=True)
                raise
            if archive is None:
                shutil.rmtree(transaction_archive, ignore_errors=True)
    else:
        bugx_home_lexical = Path.home() / ".bugx"
        bugx_home = bugx_home_lexical
        global_root = bugx_home_lexical / "knowledge"
        if "BUGX_GLOBAL_KNOWLEDGE_ROOT" in os.environ:
            override = os.environ["BUGX_GLOBAL_KNOWLEDGE_ROOT"]
            if not override.strip():
                raise CliError("BUGX_GLOBAL_KNOWLEDGE_ROOT 不能是空路径")
            global_root = Path(override).expanduser()
            _assert_no_symlink_components(global_root)
            global_root = Path(os.path.abspath(str(global_root)))
            try:
                global_root.relative_to(bugx_home_lexical)
            except ValueError as exc:
                raise CliError("全局知识重置目标必须位于 ~/.bugx 内") from exc
        _assert_no_symlink_components(global_root)
        optional: list[tuple[Path, str]] = []
        if args.reset_identity:
            optional.extend(((bugx_home / "cloud-device-id", "global/cloud-device-id"),
                             (bugx_home / "identity" / "installation-secret",
                              "global/identity/installation-secret")))
        if args.reset_credentials:
            optional.append((bugx_home / "cloud-credentials.json", "global/cloud-credentials.json"))
        if args.reset_mcp_runtime:
            runtime_root = default_mcp_runtime_root()
            optional.append((runtime_root, "global/mcp-runtime"))
        if args.reset_backups:
            optional.append((bugx_home / "backups", "global/backups"))
        targets = [(global_root, "global/knowledge"),
                   (bugx_home_lexical / "cloud-sync", "global/cloud-sync"),
                   *optional]
        for target, _ in targets:
            _assert_no_symlink_components(target)
            try:
                target.relative_to(bugx_home_lexical)
            except ValueError as exc:
                raise CliError(f"全局重置目标必须位于 ~/.bugx 内: {target}") from exc
        no_backup = bool(getattr(args, "no_backup", False))
        archive = None if no_backup else _external_reset_backup_root(root, "global")
        additions = []
        if args.reset_identity:
            additions.append("设备身份")
        if args.reset_credentials:
            additions.append("云端凭证")
        if args.reset_mcp_runtime:
            additions.append("MCP 运行时缓存")
        if args.reset_backups:
            additions.append("历史备份")
        summary = "全局级重置将清空并重建全局错题集/经验集"
        if additions:
            summary += "，并处理：" + "、".join(additions)
        if archive:
            summary += f"；全局引擎目录永不随项目重置删除。备份位置：{archive}"
        else:
            summary += "；全局引擎目录永不随项目重置删除（跳过备份）。"
        _reset_confirmation(summary, yes=args.yes, dry_run=args.dry_run, root=root)
        if args.dry_run:
            payload = {
                "status": "DRY_RUN", "scope": "global",
                "reset_knowledge": True,
                "targets": [relative for _, relative in targets],
                "backup": str(archive) if archive else None,
            }
        else:
            if archive is not None:
                archive.mkdir(parents=True, exist_ok=False)
                moved = []
                created_dirs: list[Path] = []
                knowledge_moved = False
                try:
                    moved = _move_reset_entries(targets, archive)
                    knowledge_moved = True
                    source = asset_root()
                    _bootstrap_global_knowledge(root, source)
                    if args.reset_mcp_runtime:
                        runtime_root = default_mcp_runtime_root()
                        if not runtime_root.exists():
                            runtime_root.mkdir(parents=True, mode=0o700)
                            created_dirs.append(runtime_root)
                    if args.reset_backups:
                        backup_root = bugx_home / "backups"
                        if not backup_root.exists():
                            backup_root.mkdir(parents=True, mode=0o700)
                            created_dirs.append(backup_root)
                    _write_private_json(archive / "reset-manifest.json", {
                        "scope": "global", "reset_knowledge": True,
                        "reset_identity": bool(args.reset_identity),
                        "reset_credentials": bool(args.reset_credentials),
                        "reset_mcp_runtime": bool(args.reset_mcp_runtime),
                        "reset_backups": bool(args.reset_backups),
                        "created_at": time.time(),
                        "entries": [str(destination.relative_to(archive))
                                    for _, destination in moved],
                    })
                    payload = {
                        "status": "RESET", "scope": "global", "reset_knowledge": True,
                        "reset_identity": bool(args.reset_identity),
                        "reset_credentials": bool(args.reset_credentials),
                        "reset_mcp_runtime": bool(args.reset_mcp_runtime),
                        "reset_backups": bool(args.reset_backups),
                        "backup": str(archive),
                        "archived": [str(destination.relative_to(archive))
                                     for _, destination in moved],
                        "preserved": ["global engine"],
                    }
                except BaseException:
                    for directory in reversed(created_dirs):
                        if directory.is_dir() and not directory.is_symlink():
                            shutil.rmtree(directory, ignore_errors=True)
                    try:
                        if knowledge_moved:
                            shutil.rmtree(global_root, ignore_errors=True)
                    finally:
                        _restore_reset_entries(moved)
                        shutil.rmtree(archive, ignore_errors=True)
                    raise
            else:
                _clean_reset_entries(targets)
                source = asset_root()
                _bootstrap_global_knowledge(root, source)
                if args.reset_mcp_runtime:
                    runtime_root = default_mcp_runtime_root().resolve()
                    if not runtime_root.exists():
                        runtime_root.mkdir(parents=True, mode=0o700)
                if args.reset_backups:
                    backup_root = bugx_home / "backups"
                    if not backup_root.exists():
                        backup_root.mkdir(parents=True, mode=0o700)
                payload = {
                    "status": "RESET", "scope": "global", "reset_knowledge": True,
                    "reset_identity": bool(args.reset_identity),
                    "reset_credentials": bool(args.reset_credentials),
                    "reset_mcp_runtime": bool(args.reset_mcp_runtime),
                    "reset_backups": bool(args.reset_backups),
                    "backup": None,
                    "archived": [],
                    "preserved": ["global engine"],
                }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[bugx] {payload['status']}：{payload['scope']} 重置完成")
        if payload.get("backup"):
            print(f"  可恢复备份：{payload['backup']}")
    return 0


def _backup_tree_external(
    source: Path,
    destination: Path,
    *,
    ignore: Any = None,
) -> Path:
    if source.is_symlink():
        # 如果根目录本身是符号链接，备份解析后的真实目标。
        source = source.resolve()
    if not source.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copy2(source, destination)
        destination.chmod(0o600)
        return destination
    # copytree 使用 symlinks=True 保留符号链接结构，不再因包含软链接报错；
    # dirs_exist_ok 允许与 P2-1 重定向的逐文件卸载备份合并到同一外部归档。
    shutil.copytree(
        source, destination, symlinks=True, dirs_exist_ok=True, ignore=ignore,
    )
    for directory in (destination, *destination.rglob("*")):
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o700)
        elif directory.is_file() and not directory.is_symlink():
            directory.chmod(0o600)
    return destination


def _backup_uninstall_delta(root: Path, backup_root: Path) -> None:
    """保存卸载阶段追加到项目运行态的可恢复数据。

    项目级 purge 会先备份原始 ``.bugx``，之后宿主文件删除逻辑才会把
    被删除文件的副本写入 ``.bugx/backups``，日志也可能在这段时间追加。
    这些内容不在初始快照里，必须在删除 ``.bugx`` 前补充到外部归档。
    只归档这两个由卸载阶段追加的目录，避免再次复制可能很大的报告和证据。
    """
    bugx_root = root.resolve() / ".bugx"
    for name, destination_name in (
        ("backups", "project-uninstall-backups"),
        ("logs", "project-final-logs"),
    ):
        source = bugx_root / name
        if source.exists() and not source.is_symlink():
            _backup_tree_external(source, backup_root / destination_name)


def _write_uninstall_receipt(
    backup_root: Path,
    *,
    root: Path,
    hosts: list[str],
    removed: list[str],
    preserved: list[str],
    purge_global_data: bool,
) -> None:
    """Persist the completed purge result beside its recovery archive."""
    _write_private_json(backup_root / "uninstall-receipt.json", {
        "schema_version": 1,
        "status": "UNINSTALLED",
        "root": str(root.resolve()),
        "hosts": hosts,
        "removed": removed,
        "preserved": preserved,
        "purged_project_data": True,
        "purged_global_data": bool(purge_global_data),
        "created_at": time.time(),
    })


def _purge_private_plugin_stores(root: Path, removed: list[str], preserved: list[str]) -> None:
    store = _plugin_store(root)
    if not store.is_dir() or store.is_symlink():
        return
    for plugin_path in sorted(store.iterdir()):
        manifest_path = plugin_path / "plugin.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        try:
            _, manifest = _plugin_manifest(plugin_path, validate_payload=False)
        except CliError:
            preserved.append(f"{manifest_path.relative_to(root)}（清单无效，未删除）")
            continue
        if manifest.get("private") is not True or manifest.get("autoload") is not False:
            continue
        shutil.rmtree(plugin_path)
        removed.append(str(plugin_path.relative_to(root)))
    _remove_empty_parents(root, removed)


def _rmtree_force(path: Path) -> None:
    """删除可能为**只读**的目录树（引擎载荷按不可变分发物安装）。

    实测缺陷：`uninstall --self` 原先直接 ``shutil.rmtree(~/.bugx)``；当引擎目录
    是 0o555/0o444 的只读载荷时抛 ``PermissionError: .bugx/core``，导致整个
    自卸载失败（测试与真实用户同路径）。

    关键点：删除条目需要**父目录**写权限，因此先自顶向下放开整棵树，再交给
    ``rmtree``；仅对失败条目 chmod 是不够的（实测仍 EACCES）。
    """
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    for root, _dirs, files in os.walk(path, topdown=True):
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        for name in files:
            target = Path(root) / name
            if target.is_symlink():
                continue
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass

    def _on_error(func, target, _exc):  # noqa: ANN001 - stdlib 回调签名
        try:
            os.chmod(Path(target).parent, 0o700)
        except OSError:
            pass
        func(target)

    try:
        shutil.rmtree(path, onexc=_on_error)
    except TypeError:  # Python < 3.12
        shutil.rmtree(path, onerror=_on_error)


def _self_home_dir(*parts: str) -> Path:
    """Return user-profile paths without creating them.

    BugX data/engine overrides must not relocate host configuration: install
    and self-uninstall use the same actual user profile discovery directory.
    """
    base = Path.home().resolve()
    return base.joinpath(*parts) if parts else base


def _self_backup_root() -> Path:
    # Windows legacy path handling can reject a valid backup tree once the
    # global archive already contains ``~/.bugx/backups/uninstall`` entries:
    # nesting that tree below ``home-bugx`` can exceed MAX_PATH. Keep the
    # durable user-home location when it is safe, but select the user temp
    # directory for long-home test profiles and similarly deep Windows homes.
    preferred = Path.home() / f".bugx-self-uninstall-backup-{time.time_ns()}"
    if os.name == "nt" and len(str(preferred)) + 220 >= 240:
        preferred = Path(tempfile.gettempdir()) / preferred.name
    base = preferred.resolve()
    _assert_no_symlink_components(base)
    return base


def _backup_global_home_for_self_uninstall(
    bugx_home: Path, backup_root: Path,
) -> None:
    """Snapshot global data without nesting historical archives indefinitely.

    Project purge archives already live under ``~/.bugx/backups/uninstall``.
    Copying that tree below ``home-bugx/backups/uninstall`` during a later
    self-uninstall needlessly adds path segments and exceeds Windows MAX_PATH.
    Keep the archive contents, but publish them beside the main snapshot.
    """
    uninstall_archives = (bugx_home / "backups" / "uninstall").resolve()

    def ignore_nested_archives(source: str, names: list[str]) -> list[str]:
        try:
            if Path(source).resolve() == bugx_home.resolve() / "backups":
                return ["uninstall"] if "uninstall" in names else []
        except OSError:
            return []
        return []

    _backup_tree_external(
        bugx_home,
        backup_root / "home-bugx",
        ignore=ignore_nested_archives,
    )
    if uninstall_archives.is_dir() and not uninstall_archives.is_symlink():
        _backup_tree_external(
            uninstall_archives,
            backup_root / "uninstall-archives",
        )


def _self_zcode_bugx_dirs(config_path: Path) -> list[str]:
    """读取 ZCode 用户配置中指向 BugX 插件目录的 plugins.dirs 条目。"""
    if config_path.is_symlink() or not config_path.is_file():
        return []
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    plugins_cfg = loaded.get("plugins") if isinstance(loaded, dict) else None
    dirs_raw = plugins_cfg.get("dirs") if isinstance(plugins_cfg, dict) else None
    if not isinstance(dirs_raw, list):
        return []
    return [
        str(item) for item in dirs_raw
        if isinstance(item, str)
        and item.replace("\\", "/").rstrip("/").endswith("plugins/bugx")
    ]


def _self_remove_zcode_registrations(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    config_path = _zcode_config_path()
    entries = _self_zcode_bugx_dirs(config_path)
    if not entries:
        return
    if config_path.is_symlink() or not config_path.is_file():
        preserved.append("~/.zcode/cli/config.json（不是安全的普通文件，未清理）")
        return
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        preserved.append(f"~/.zcode/cli/config.json（无法解析：{exc}）")
        return
    plugins_cfg = dict(loaded.get("plugins") or {})
    dirs_raw = plugins_cfg.get("dirs") if isinstance(plugins_cfg, dict) else []
    kept = [
        str(item) for item in (dirs_raw or [])
        if not (
            isinstance(item, str)
            and item.replace("\\", "/").rstrip("/").endswith("plugins/bugx")
        )
    ]
    plugins_cfg["dirs"] = kept
    loaded["plugins"] = plugins_cfg
    mcp_cfg = loaded.get("mcp")
    if isinstance(mcp_cfg, dict) and isinstance(mcp_cfg.get("servers"), dict):
        servers = mcp_cfg["servers"]
        for key in list(servers.keys()):
            if "bugx" in key.lower():
                del servers[key]
    if backup_root is not None:
        _backup_tree_external(config_path, backup_root / "zcode-config.json")
    _atomic_write_global_file(config_path, json.dumps(
        loaded, ensure_ascii=False, indent=2,
    ).encode("utf-8") + b"\n")
    removed.append(
        f"~/.zcode/cli/config.json（移除 {len(entries)} 条 BugX plugins.dirs 登记）"
    )


_ZCODE_USER_AGENT_OWNERSHIP_MARKS = (
    "## 用户级子智能体边界", "## 用户级插件边界", "bugx engine path --name bugx-runtime-contract.md",
)


def _self_remove_zcode_user_agents(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """清理用户级 BugX 子智能体（storage.dir/agents 下的 bugx*.md）。

    ZCode 的 user 作用域扫描根是 storageRoot/agents；这里只删除能证明是
    BugX 用户级入口的文件（内容含 BugX 边界标记或协议指认），其余保留。
    """
    agents_dir = _zcode_user_agents_dir()
    display_dir = f"{agents_dir}/"
    if agents_dir.is_symlink() or not agents_dir.is_dir():
        return
    for filename in ("bugx.md", "bugx-worker.md"):
        entry = agents_dir / filename
        if not entry.exists() and not entry.is_symlink():
            continue
        if entry.is_symlink() or not entry.is_file():
            preserved.append(f"{display_dir}{filename}（不是安全的普通文件，未清理）")
            continue
        try:
            current = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            preserved.append(f"{display_dir}{filename}（无法读取：{exc}，未清理）")
            continue
        if not any(mark in current for mark in _ZCODE_USER_AGENT_OWNERSHIP_MARKS):
            preserved.append(
                f"{display_dir}{filename}（内容不是 BugX 用户级入口，保留）"
            )
            continue
        if backup_root is not None:
            _backup_tree_external(entry, backup_root / f"zcode-user-agent-{filename}")
        try:
            entry.unlink()
        except OSError as exc:
            preserved.append(f"{display_dir}{filename}（删除失败：{exc}）")
            continue
        removed.append(f"{display_dir}{filename}（BugX 用户级子智能体已移除）")


_CLAUDE_USER_AGENT_OWNERSHIP_MARKS = (
    "## 用户级智能体边界", "## User-level Agent Boundary",
    "bugx engine path --name bugx-runtime-contract.md",
    "BugX Worker — Claude Code",
)


def _self_remove_claude_user_agents(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """清理用户级 BugX 智能体（~/.claude/agents 下的 bugx*.md、技能与插件登记）。"""
    from .plugin_claude import (
        _claude_user_agents_dir,
        _remove_claude_user_skills,
        _remove_claude_plugin_registry,
    )

    _remove_claude_user_skills(removed, preserved, backup_root=backup_root)
    _remove_claude_plugin_registry(removed, preserved, backup_root=backup_root)
    agents_dir = _claude_user_agents_dir()
    display_dir = f"{agents_dir}/"
    if agents_dir.is_symlink() or not agents_dir.is_dir():
        return
    for filename in ("bugx.md", "bugx-worker.md"):
        entry = agents_dir / filename
        if not entry.exists() and not entry.is_symlink():
            continue
        if entry.is_symlink() or not entry.is_file():
            preserved.append(f"{display_dir}{filename}（不是安全的普通文件，未清理）")
            continue
        try:
            current = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            preserved.append(f"{display_dir}{filename}（无法读取：{exc}，未清理）")
            continue
        if not any(mark in current for mark in _CLAUDE_USER_AGENT_OWNERSHIP_MARKS):
            preserved.append(
                f"{display_dir}{filename}（内容不是 BugX 用户级入口，保留）"
            )
            continue
        if backup_root is not None:
            _backup_tree_external(entry, backup_root / f"claude-user-agent-{filename}")
        try:
            entry.unlink()
        except OSError as exc:
            preserved.append(f"{display_dir}{filename}（删除失败：{exc}）")
            continue
        removed.append(f"{display_dir}{filename}（BugX 用户级智能体已移除）")


_QODER_USER_ASSET_OWNERSHIP_MARKS = (
    "## 用户级智能体与插件边界", "bugx engine path --name bugx-runtime-contract.md",
)


def _self_remove_qoder_user_assets(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """清理 Qoder 用户级 BugX 资产（~/.qoder/agents, ~/.qoder/skills, 插件注册）。"""
    from .plugin_qoder import (
        _qoder_user_agents_dir,
        _qoder_user_plugins_dir,
        _qoder_user_skills_dir,
    )

    agents_dir = _qoder_user_agents_dir()
    if agents_dir.is_dir() and not agents_dir.is_symlink():
        display_dir = f"{agents_dir}/"
        for filename in ("bugx.md", "bugx-worker.md"):
            entry = agents_dir / filename
            if not entry.exists() and not entry.is_symlink():
                continue
            if entry.is_symlink() or not entry.is_file():
                preserved.append(f"{display_dir}{filename}（不是安全的普通文件，未清理）")
                continue
            try:
                current = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                preserved.append(f"{display_dir}{filename}（无法读取：{exc}，未清理）")
                continue
            if not any(mark in current for mark in _QODER_USER_ASSET_OWNERSHIP_MARKS):
                preserved.append(f"{display_dir}{filename}（内容不是 BugX 用户级入口，保留）")
                continue
            if backup_root is not None:
                _backup_tree_external(entry, backup_root / f"qoder-user-agent-{filename}")
            try:
                entry.unlink()
            except OSError as exc:
                preserved.append(f"{display_dir}{filename}（删除失败：{exc}）")
                continue
            removed.append(f"{display_dir}{filename}（BugX 用户级智能体已移除）")

    skills_dir = _qoder_user_skills_dir() / "bugx"
    if skills_dir.is_dir() and not skills_dir.is_symlink():
        skill_file = skills_dir / "SKILL.md"
        if skill_file.is_file() and not skill_file.is_symlink():
            if backup_root is not None:
                _backup_tree_external(skill_file, backup_root / "qoder-user-skill-SKILL.md")
            try:
                skill_file.unlink()
                removed.append(f"{skills_dir}/SKILL.md（BugX 用户级技能已移除）")
                skills_dir.rmdir()
            except OSError as exc:
                preserved.append(f"{skills_dir}（删除失败：{exc}）")

    plugins_dir = _qoder_user_plugins_dir()
    v2_path = plugins_dir / "installed_plugins_v2.json"
    if v2_path.is_file() and not v2_path.is_symlink():
        try:
            data = json.loads(v2_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
                plugins_map = data["plugins"]
                if "bugx@bugx-agent" in plugins_map:
                    plugins_map.pop("bugx@bugx-agent", None)
                    v2_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    removed.append(f"{v2_path}（已注销 bugx 插件 v2）")
        except (OSError, json.JSONDecodeError) as exc:
            preserved.append(f"{v2_path}（插件注销失败：{exc}）")

    v1_path = plugins_dir / "installed_plugins.json"
    if v1_path.is_file() and not v1_path.is_symlink():
        try:
            data = json.loads(v1_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
                plugins_map = data["plugins"]
                if "bugx@bugx-agent" in plugins_map:
                    plugins_map.pop("bugx@bugx-agent", None)
                    v1_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    removed.append(f"{v1_path}（已注销 bugx 插件 v1）")
        except (OSError, json.JSONDecodeError) as exc:
            preserved.append(f"{v1_path}（插件注销失败：{exc}）")

    from .plugin_qoder import (
        _qoder_user_plugin_cache_dir,
        _qoder_user_settings_path,
    )
    import shutil

    # 清理 settings.json 中 BugX 插件注册；MCP 仅由项目级配置管理。
    settings_path = _qoder_user_settings_path()
    if settings_path.is_file() and not settings_path.is_symlink():
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
            settings_modified = False
            if isinstance(settings_data, dict):
                enabled = settings_data.get("enabledPlugins")
                if isinstance(enabled, dict) and "bugx@bugx-agent" in enabled:
                    enabled.pop("bugx@bugx-agent", None)
                    settings_modified = True
                if settings_modified:
                    settings_path.write_text(json.dumps(settings_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    removed.append(f"{settings_path}（已移除 bugx 插件注册）")
        except (OSError, json.JSONDecodeError) as exc:
            preserved.append(f"{settings_path}（清理失败：{exc}）")

    # 清理 ~/.qoder/plugins/cache/bugx-agent/bugx
    cache_dir = _qoder_user_plugin_cache_dir()
    if cache_dir.is_dir() and not cache_dir.is_symlink():
        try:
            shutil.rmtree(cache_dir)
            removed.append(f"{cache_dir}（已移除插件缓存）")
        except OSError as exc:
            preserved.append(f"{cache_dir}（删除失败：{exc}）")


_FUNIDE_USER_AGENT_OWNERSHIP_MARKS = (
    "## FunIDE 用户级智能体边界",
    "bugx engine path --name bugx-runtime-contract.md",
)


def _self_remove_funide_user_agent(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """清理 FunIDE/Copilot 用户级 BugX Agent，保留用户自有文件。"""
    from .plugin_funide import _funide_user_agents_dir

    agents_dir = _funide_user_agents_dir()
    entry = agents_dir / "bugx.agent.md"
    if not entry.exists() and not entry.is_symlink():
        return
    display = str(entry)
    if entry.is_symlink() or not entry.is_file():
        preserved.append(f"{display}（不是安全的普通文件，未清理）")
        return
    try:
        current = entry.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        preserved.append(f"{display}（无法读取：{exc}，未清理）")
        return
    if not all(mark in current for mark in _FUNIDE_USER_AGENT_OWNERSHIP_MARKS):
        preserved.append(f"{display}（内容不是 BugX 用户级入口，保留）")
        return
    if backup_root is not None:
        _backup_tree_external(entry, backup_root / "funide-user-agent-bugx.agent.md")
    try:
        entry.unlink()
    except OSError as exc:
        preserved.append(f"{display}（删除失败：{exc}）")
        return
    removed.append(f"{display}（FunIDE 用户级智能体已移除）")


def _self_remove_funide_native_agent(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """Remove only BugX's FunIDE Settings → 智能体 registry entry."""
    from .plugin_funide import _NATIVE_AGENT_OWNERSHIP_MARK, _funide_data_dir

    target = _funide_data_dir() / "agents.json"
    if not target.exists() and not target.is_symlink():
        return
    display = str(target)
    if target.is_symlink() or not target.is_file():
        preserved.append(f"{display}（不是安全的普通文件，未清理）")
        return
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        preserved.append(f"{display}（无法读取：{exc}，未清理）")
        return
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        preserved.append(f"{display}（格式异常，未清理）")
        return
    agents = data["agents"]
    index = next(
        (i for i, item in enumerate(agents)
         if isinstance(item, dict) and item.get("id") == "bugx"),
        None,
    )
    if index is None:
        return
    item = agents[index]
    if not isinstance(item, dict) or _NATIVE_AGENT_OWNERSHIP_MARK not in str(item.get("systemPrompt", "")):
        preserved.append(f"{display}（id=bugx 不是 BugX 注册项，保留）")
        return
    if backup_root is not None:
        _backup_tree_external(target, backup_root / "funide-native-agents.json")
    agents.pop(index)
    try:
        _atomic_write_global_file(
            target,
            (json.dumps(data, ensure_ascii=False, indent="\t") + "\n").encode("utf-8"),
        )
    except Exception as exc:
        preserved.append(f"{display}（写回失败：{exc}，未清理）")
        return
    removed.append(f"{display}（FunIDE 原生智能体注册已移除）")


def _self_remove_funide_native_skill(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """Remove only BugX's FunIDE Settings → 技能 entry."""
    from .plugin_funide import _NATIVE_SKILL_OWNERSHIP_MARK, _funide_user_skills_dir

    target = _funide_user_skills_dir() / "bugx" / "SKILL.md"
    if not target.exists() and not target.is_symlink():
        return
    display = str(target)
    if target.is_symlink() or not target.is_file():
        preserved.append(f"{display}（不是安全的普通文件，未清理）")
        return
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        preserved.append(f"{display}（无法读取：{exc}，未清理）")
        return
    if _NATIVE_SKILL_OWNERSHIP_MARK not in content:
        preserved.append(f"{display}（内容不是 BugX 用户级技能，保留）")
        return
    if backup_root is not None:
        _backup_tree_external(target, backup_root / "funide-native-skill-bugx.SKILL.md")
    try:
        target.unlink()
        target.parent.rmdir()
    except OSError as exc:
        preserved.append(f"{display}（删除失败：{exc}）")
        return
    removed.append(f"{display}（FunIDE 原生技能已移除）")


def _self_remove_funide_profile_mcp(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """Remove BugX-owned entries from FunIDE's user-level MCP profile."""
    from .plugin_funide import (
        _funide_native_mcp_names,
        _funide_profile_mcp_entry_owned,
        _funide_profile_mcp_path,
    )

    target = _funide_profile_mcp_path()
    if not target.exists() and not target.is_symlink():
        return
    display = str(target)
    if target.is_symlink() or not target.is_file():
        preserved.append(f"{display}（不是安全的普通文件，未清理）")
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        preserved.append(f"{display}（无法解析：{exc}，未清理）")
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("servers"), dict):
        preserved.append(f"{display}（缺少 servers 对象，未清理）")
        return
    servers = payload["servers"]
    names = [
        name for name in _funide_native_mcp_names()
        if name in servers and _funide_profile_mcp_entry_owned(name, servers[name])
    ]
    if not names:
        return
    if backup_root is not None:
        _backup_tree_external(target, backup_root / "funide-user-mcp.json")
    for name in names:
        servers.pop(name, None)
    try:
        _atomic_write_global_file(
            target,
            (json.dumps(payload, ensure_ascii=False, indent="\t") + "\n").encode("utf-8"),
        )
    except OSError as exc:
        preserved.append(f"{display}（写回失败：{exc}，未清理）")
        return
    removed.append(f"{display}（FunIDE 用户级 MCP：已移除 {', '.join(names)}）")


def _self_uninstall_opencode_links(
    backup_root: Path | None,
    removed: list[str], preserved: list[str],
) -> None:
    agents_dir = _self_home_dir(".config", "opencode", "agents")
    engine_root = global_engine_root()
    for filename in ("bugx.md", "bugx-worker.md"):
        entry = agents_dir / filename
        if not entry.is_symlink() and not entry.exists():
            continue
        alt_name = "bugx-opencode.md" if filename == "bugx.md" else filename
        cand = engine_root / "contracts" / alt_name
        expected = cand if cand.is_file() else (engine_root / alt_name)
        if not entry.is_symlink():
            # Windows may not grant SeCreateSymbolicLinkPrivilege.  The
            # installer then publishes a byte-for-byte copy; recognize only
            # that exact copy as BugX-owned and preserve user edits.
            try:
                owned_copy = entry.is_file() and expected.is_file() \
                    and _sha256_file(entry) == _sha256_file(expected)
            except OSError:
                owned_copy = False
            if not owned_copy:
                preserved.append(
                    f"~/.config/opencode/agents/{filename}（不是 BugX 副本入口或内容已变化，保留）"
                )
                continue
            if backup_root is not None:
                _backup_tree_external(entry, backup_root / f"opencode-copy-{filename}")
            try:
                entry.unlink()
            except OSError as exc:
                preserved.append(f"~/.config/opencode/agents/{filename}（删除失败：{exc}）")
                continue
            removed.append(f"~/.config/opencode/agents/{filename}（全局入口副本）")
            continue
        try:
            target = entry.resolve(strict=False)
        except OSError as exc:
            preserved.append(f"~/.config/opencode/agents/{filename}（无法解析：{exc}）")
            continue
        if target in (expected.resolve(strict=False), (engine_root / filename).resolve(strict=False)):
            try:
                entry.unlink()
            except OSError as exc:
                preserved.append(f"~/.config/opencode/agents/{filename}（删除失败：{exc}）")
                continue
            removed.append(f"~/.config/opencode/agents/{filename}（全局入口链接）")
        else:
            preserved.append(
                f"~/.config/opencode/agents/{filename}（未指向 BugX 全局引擎，保留）"
            )


def _self_uninstall_pi_skill(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    agents_skill_dir = _self_home_dir(".agents", "skills", "bugx")
    pi_skill_dir = _self_home_dir(".pi", "skills", "bugx")
    pi_agent_skill_dir = _self_home_dir(".pi", "agent", "skills", "bugx")
    for label, p_dir in (("~/.pi/skills/bugx", pi_skill_dir), ("~/.pi/agent/skills/bugx", pi_agent_skill_dir)):
        if p_dir.is_symlink():
            p_dir.unlink()
            removed.append(f"{label}（全局 skill 链接）")
        elif p_dir.exists():
            # On Windows the installer may publish a directory copy when
            # symlink creation is unavailable. Remove only an exact copy of the
            # BugX-owned global skill; never remove a user-edited directory.
            global_skill = agents_skill_dir / "SKILL.md"
            candidate = p_dir / "SKILL.md"
            try:
                owned_copy = (p_dir.is_dir() and candidate.is_file()
                              and global_skill.is_file()
                              and _sha256_file(candidate) == _sha256_file(global_skill))
            except OSError:
                owned_copy = False
            if owned_copy:
                if backup_root is not None:
                    _backup_tree_external(p_dir, backup_root / p_dir.name)
                shutil.rmtree(p_dir)
                removed.append(f"{label}（全局 skill 副本）")
            else:
                preserved.append(f"{label}（不是 BugX 副本或内容已变化，保留）")
    if agents_skill_dir.is_symlink():
        agents_skill_dir.unlink()
        removed.append("~/.agents/skills/bugx（符号链接）")
        return
    if agents_skill_dir.is_dir():
        if backup_root is not None:
            _backup_tree_external(agents_skill_dir, backup_root / "pi-global-skill")
        shutil.rmtree(agents_skill_dir)
        removed.append("~/.agents/skills/bugx（全局 skill 实体）")
    elif agents_skill_dir.exists():
        preserved.append("~/.agents/skills/bugx（不是安全目录，保留）")


def _self_uninstall_dsh_preset(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    preset_dir = _self_home_dir(".dsh", ".agent-presets", "bugx")
    if preset_dir.is_symlink() or not preset_dir.is_dir():
        return
    if backup_root is not None:
        _backup_tree_external(preset_dir, backup_root / "dsh-agent-presets")
    shutil.rmtree(preset_dir)
    removed.append("~/.dsh/.agent-presets/bugx（全局预设）")


def _self_uninstall_dsh_ui_plugin(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """Remove the BugX DSH web bundle and its profile registration."""
    dsh_home = _dsh_home()
    plugin_root = dsh_home / "plugins" / "bugx-ui"
    profile_root = dsh_home / "profiles" / "web"
    if not plugin_root.exists():
        return
    dsh = shutil.which("dsh")
    if dsh and (profile_root / "package.json").is_file():
        try:
            result = subprocess.run(
                [dsh, "plugin", "--profile", "web", "remove", "@bencibro/bugx-dsh-ui"],
                cwd=profile_root, capture_output=True, text=True, check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            preserved.append(f"{plugin_root}（web profile 注销失败：{exc}）")
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "dsh plugin remove 失败").strip()
            preserved.append(f"{plugin_root}（web profile 注销失败：{detail[-300:]}）")
            return
    if plugin_root.is_symlink() or not plugin_root.is_dir():
        preserved.append(f"{plugin_root}（不是安全目录，未清理）")
        return
    if backup_root is not None:
        _backup_tree_external(plugin_root, backup_root / "dsh-ui-plugin")
    shutil.rmtree(plugin_root)
    removed.append(f"{plugin_root}（DSH UI 插件）")


def _self_uninstall_workbuddy_skill(
    root: Path, backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    """清理用户级 WorkBuddy Skill；仅当与当前模板一致时删除。

    用户级 Skill 是全局层工件（与 zcode 用户级 agents 同语义），
    项目级卸载永远保留；只有 ``uninstall --self`` 才处理。所有权判定
    采用内容比对：与当前渲染模板（``strip_locale_marker`` 后）逐字
    一致才视为 BugX 自有；用户改过一行即保留并如实报告。
    """
    valid_templates: set[str] = set()
    candidate = global_engine_root() / "bugx.md"
    if not candidate.is_file():
        candidate = asset_root() / "bugx.md"
    if candidate.is_file():
        for loc in ("zh-CN", "en-US", None):
            try:
                valid_templates.add(
                    strip_locale_marker(
                        get_adapter("workbuddy").render(candidate, locale=loc)
                    ).strip()
                )
            except (CliError, OSError):
                pass
    for config_root in _workbuddy_config_roots():
        skill = config_root / "skills" / "bugx" / "SKILL.md"
        if not skill.is_file() or skill.is_symlink():
            continue
        display = str(skill)
        if not valid_templates:
            preserved.append(f"{display}（无法读取当前模板，保留）")
            continue
        try:
            current = strip_locale_marker(
                skill.read_text(encoding="utf-8")
            ).strip()
        except (OSError, UnicodeDecodeError) as exc:
            preserved.append(f"{display}（读取失败：{exc}，保留）")
            continue
        if current not in valid_templates:
            preserved.append(f"{display}（内容与 BugX 模板不一致，保留）")
            continue
        if backup_root is not None:
            _backup_tree_external(
                skill,
                backup_root / "workbuddy-user-skill" / config_root.name / skill.name,
            )
        try:
            skill.unlink()
        except OSError as exc:
            preserved.append(f"{display}（删除失败：{exc}）")
            continue
        removed.append(f"{display}（WorkBuddy 用户级 Skill）")
        for parent in (skill.parent, skill.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
        expert_dir = config_root / "plugins" / "marketplaces" / "my-experts" / "plugins" / "bugx"
        if expert_dir.is_dir() and not expert_dir.is_symlink():
            if backup_root is not None:
                _backup_tree_external(
                    expert_dir,
                    backup_root / "workbuddy-custom-expert" / config_root.name / "bugx",
                )
            try:
                shutil.rmtree(expert_dir)
                removed.append(f"{expert_dir}（WorkBuddy 自定义专家）")
            except OSError as exc:
                preserved.append(f"{expert_dir}（删除失败：{exc}）")
            market_manifest_path = config_root / "plugins" / "marketplaces" / "my-experts" / ".codebuddy-plugin" / "marketplace.json"
            if market_manifest_path.is_file():
                try:
                    m_data = json.loads(market_manifest_path.read_text(encoding="utf-8"))
                    if isinstance(m_data, dict) and isinstance(m_data.get("plugins"), list):
                        m_data["plugins"] = [p for p in m_data["plugins"] if isinstance(p, dict) and p.get("name") != "bugx"]
                        market_manifest_path.write_text(json.dumps(m_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                except (OSError, json.JSONDecodeError):
                    pass


def _self_codex_registrations() -> tuple[Path, list[str], list[str]]:
    """读取 ~/.codex/config.toml 中 BugX 本地 marketplace 与插件登记。"""
    config_path = _self_home_dir(".codex", "config.toml")
    marketplaces: list[str] = []
    plugins: list[str] = []
    if config_path.is_symlink() or not config_path.is_file():
        return config_path, marketplaces, plugins
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(_i18n_exc("lifecycle.codex_config_unparseable", f"~/.codex/config.toml 无法解析：{exc}", exc=exc)) from exc
    section = payload.get("marketplaces")
    if isinstance(section, dict):
        marketplaces = sorted(
            str(key) for key in section
            if str(key).startswith("bugx-local-")
            or str(key) == CHATGPT_GLOBAL_MARKETPLACE_NAME
        )
    plugin_section = payload.get("plugins")
    if isinstance(plugin_section, dict):
        plugins = sorted(
            str(key) for key in plugin_section
            if str(key).startswith(f"{CHATGPT_PLUGIN_NAME}@bugx-local-")
            or str(key) == f"{CHATGPT_PLUGIN_NAME}@{CHATGPT_GLOBAL_MARKETPLACE_NAME}"
        )
    return config_path, marketplaces, plugins


def _codex_binary() -> str | None:
    return shutil.which("codex")


def _run_codex_plugin_command(
    command: list[str],
) -> tuple[bool, str]:
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (process.stderr or process.stdout or "").strip()[-200:]
    return process.returncode == 0, detail


def _self_uninstall_codex_plugins(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    try:
        config_path, marketplaces, plugins = _self_codex_registrations()
    except CliError as exc:
        preserved.append(str(exc))
        return
    if not marketplaces and not plugins:
        return
    codex = _codex_binary()
    if not codex:
        preserved.append(
            "~/.codex/config.toml（未找到 codex 命令，无法自动注销；请手工执行 "
            "codex plugin remove / codex plugin marketplace remove："
            + ", ".join(sorted(set(marketplaces))) + "）"
        )
        return
    if backup_root is not None and config_path.is_file():
        _backup_tree_external(config_path, backup_root / "codex-config.toml")
    for plugin_key in plugins:
        ok, detail = _run_codex_plugin_command(
            [codex, "plugin", "remove", plugin_key, "--json"])
        if ok:
            removed.append(f"~/.codex/config.toml（注销 codex 插件 {plugin_key}）")
        else:
            preserved.append(f"~/.codex/config.toml（注销 {plugin_key} 失败：{detail}）")
    for name in marketplaces:
        ok, detail = _run_codex_plugin_command(
            [codex, "plugin", "marketplace", "remove", name])
        if ok:
            removed.append(f"~/.codex/config.toml（移除 codex marketplace {name}）")
        else:
            preserved.append(f"~/.codex/config.toml（移除 marketplace {name} 失败：{detail}）")
    cache_root = _self_home_dir(".codex", "plugins", "cache")
    if cache_root.is_dir():
        for child in sorted(cache_root.iterdir()):
            if (child.name.startswith("bugx-local-")
                    or child.name == CHATGPT_GLOBAL_MARKETPLACE_NAME) \
                    and child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
                removed.append(f"~/.codex/plugins/cache/{child.name}（codex 插件缓存）")


def _deregister_codex_plugin(
    root: Path, removed: list[str], preserved: list[str],
) -> None:
    """项目级 chatgpt 卸载：清理项目域历史登记，并**显式声明**全局单例保留。

    本代 ``install`` 只注册机器级单例 ``bugx@bugx-global``（``plugin.py`` 的
    ``codex plugin marketplace add ~/.bugx/chatgpt-plugin`` + ``codex plugin
    add bugx@bugx-global``），从不写项目域 marketplace 名。旧实现只匹配
    ``bugx-local-<hash>``，因此恒为空操作却声称"反向注销全局登记"（2026-11-09
    审计 P2）。修复采用**共享资源语义**：一个项目卸载不得撤销其它项目仍在
    使用的机器级注册，全局清理由 ``bugx uninstall --self`` 负责；这里在按
    所有权核验后显式记录"保留"原因，杜绝静默空转。
    """
    try:
        _config_path, marketplaces, plugins = _self_codex_registrations()
    except CliError as exc:
        preserved.append(str(exc))
        return
    name = _chatgpt_marketplace_name(root)
    legacy_plugins = [item for item in plugins if item.endswith(f"@{name}")]
    legacy_marketplaces = [name] if name in marketplaces else []
    codex = _codex_binary()
    if legacy_plugins or legacy_marketplaces:
        if not codex:
            preserved.append(
                f"codex 插件登记 {name}（未找到 codex 命令；请手工 codex plugin remove / "
                "codex plugin marketplace remove）"
            )
        else:
            for plugin_key in legacy_plugins:
                ok, detail = _run_codex_plugin_command(
                    [codex, "plugin", "remove", plugin_key, "--json"])
                if ok:
                    removed.append(f"codex 插件 {plugin_key}（已注销）")
                else:
                    preserved.append(f"codex 插件 {plugin_key}（注销失败：{detail}）")
            for legacy_name in legacy_marketplaces:
                ok, detail = _run_codex_plugin_command(
                    [codex, "plugin", "marketplace", "remove", legacy_name])
                if ok:
                    removed.append(f"codex marketplace {legacy_name}（已移除）")
                else:
                    preserved.append(f"codex marketplace {legacy_name}（移除失败：{detail}）")

    global_plugin = f"{CHATGPT_PLUGIN_NAME}@{CHATGPT_GLOBAL_MARKETPLACE_NAME}"
    global_registered = (
        global_plugin in plugins or CHATGPT_GLOBAL_MARKETPLACE_NAME in marketplaces
    )
    if not global_registered:
        return
    owned = False
    try:
        owned = _is_bugx_marketplace_source(_chatgpt_global_plugin_root())
    except (OSError, ValueError):
        owned = False
    if owned:
        preserved.append(
            f"~/.codex/config.toml（全局 ChatGPT 插件 {global_plugin} 保留："
            "机器级共享资源，清理请执行 bugx uninstall --self）"
        )
    else:
        preserved.append(
            f"~/.codex/config.toml（{global_plugin} 不是 BugX 所有的来源，未修改）"
        )


def _self_uninstall_inventory() -> list[str]:
    """--dry-run 的全机残留清单：只读，不动任何文件。"""
    lines: list[str] = []
    zcode_entries = _self_zcode_bugx_dirs(_zcode_config_path())
    if zcode_entries:
        lines.append("将清理 ZCode plugins.dirs 登记：")
        lines.extend(f"    - {entry}" for entry in zcode_entries)
    else:
        lines.append("ZCode plugins.dirs：无 BugX 登记")
    user_agents_dir = _zcode_user_agents_dir()
    user_agent_entries = [
        filename for filename in ("bugx.md", "bugx-worker.md")
        if (user_agents_dir / filename).is_file()
    ]
    if user_agent_entries:
        lines.append(f"将清理 ZCode 用户级子智能体：{user_agents_dir}")
        lines.extend(f"    - {filename}" for filename in user_agent_entries)
    else:
        lines.append("ZCode 用户级子智能体：无 BugX 登记")
    # 用户级智能体目录来自宿主自己的 ``user_agent_surfaces`` 钩子
    # （``commands/plugin_<host>.py``）；通用层不写宿主名，也不猜目录名。
    for host_adapter in HOST_ADAPTERS:
        surfaces = engine_host_user_agent_surfaces(host_adapter.name)
        if not surfaces:
            continue
        primary = surfaces[0]
        agent_entries = [
            filename for filename in primary["entries"]
            if (primary["path"] / filename).is_file()
        ]
        if agent_entries:
            lines.append(f"将清理 {primary['label']} 用户级智能体：{primary['path']}")
            lines.extend(f"    - {filename}" for filename in agent_entries)
        else:
            lines.append(f"{primary['label']} 用户级智能体：无 BugX 登记")
    from .plugin_claude import _claude_user_agents_dir
    claude_agents_dir = _claude_user_agents_dir()
    claude_agent_entries = [
        filename for filename in ("bugx.md", "bugx-worker.md")
        if (claude_agents_dir / filename).is_file()
    ]
    if claude_agent_entries:
        lines.append(f"将清理 Claude 用户级智能体：{claude_agents_dir}")
        lines.extend(f"    - {filename}" for filename in claude_agent_entries)
    else:
        lines.append("Claude 用户级智能体：无 BugX 登记")
    from .plugin_qoder import _qoder_user_agents_dir
    qoder_agents_dir = _qoder_user_agents_dir()
    qoder_agent_entries = [
        filename for filename in ("bugx.md", "bugx-worker.md")
        if (qoder_agents_dir / filename).is_file()
    ]
    if qoder_agent_entries:
        lines.append(f"将清理 Qoder 用户级智能体：{qoder_agents_dir}")
        lines.extend(f"    - {filename}" for filename in qoder_agent_entries)
    else:
        lines.append("Qoder 用户级智能体：无 BugX 登记")
    from .plugin_funide import _funide_user_agents_dir
    funide_agent = _funide_user_agents_dir() / "bugx.agent.md"
    if funide_agent.is_file():
        lines.append(f"将清理 FunIDE 用户级智能体：{funide_agent}")
    else:
        lines.append("FunIDE 用户级智能体：无 BugX 登记")
    from .plugin_funide import _funide_data_dir
    funide_native = _funide_data_dir() / "agents.json"
    if funide_native.is_file():
        lines.append(f"将清理 FunIDE 原生智能体注册：{funide_native}")
    else:
        lines.append("FunIDE 原生智能体注册：无 BugX 登记")
    agents_dir = _self_home_dir(".config", "opencode", "agents")
    engine_root = global_engine_root()
    for filename in ("bugx.md", "bugx-worker.md"):
        entry = agents_dir / filename
        alt_name = "bugx-opencode.md" if filename == "bugx.md" else filename
        cand = engine_root / "contracts" / alt_name
        expected = cand if cand.is_file() else (engine_root / alt_name)
        if entry.is_symlink():
            target = entry.resolve(strict=False)
            if target in (expected.resolve(strict=False), (engine_root / filename).resolve(strict=False)):
                lines.append(f"将清理 ~/.config/opencode/agents/{filename}（指向全局引擎）")
            else:
                lines.append(f"保留 ~/.config/opencode/agents/{filename}（未指向全局引擎）")
        elif entry.exists():
            try:
                owned_copy = entry.is_file() and expected.is_file() \
                    and _sha256_file(entry) == _sha256_file(expected)
            except OSError:
                owned_copy = False
            if owned_copy:
                lines.append(f"将清理 ~/.config/opencode/agents/{filename}（全局引擎副本入口）")
            else:
                lines.append(f"保留 ~/.config/opencode/agents/{filename}（非 BugX 副本或内容已变化）")
    pi_skill_dir = _self_home_dir(".pi", "skills", "bugx")
    agents_skill_dir = _self_home_dir(".agents", "skills", "bugx")
    for label, path in (("~/.pi/skills/bugx", pi_skill_dir),
                        ("~/.agents/skills/bugx", agents_skill_dir)):
        if path.is_symlink() or path.exists():
            lines.append(f"将清理 {label}")
        else:
            lines.append(f"{label}：不存在")
    dsh_preset = _self_home_dir(".dsh", ".agent-presets", "bugx")
    lines.append(
        f"将清理 ~/.dsh/.agent-presets/bugx（全局预设）"
        if dsh_preset.is_dir() and not dsh_preset.is_symlink()
        else "~/.dsh/.agent-presets/bugx：不存在"
    )
    dsh_ui = _dsh_home() / "plugins" / "bugx-ui"
    lines.append(
        f"将清理 {dsh_ui}（DSH web UI 插件）"
        if dsh_ui.is_dir() and not dsh_ui.is_symlink()
        else f"{dsh_ui}：不存在"
    )
    workbuddy_skills = [
        root / "skills" / "bugx" / "SKILL.md"
        for root in _workbuddy_config_roots()
        if (root / "skills" / "bugx" / "SKILL.md").is_file()
    ]
    if workbuddy_skills:
        lines.append("将按模板比对清理 WorkBuddy 用户级 Skill：")
        lines.extend(f"    - {path}" for path in workbuddy_skills)
        lines.append("    （内容与 BugX 模板不一致的一律保留）")
    else:
        lines.append("WorkBuddy 用户级 Skill：不存在")
    try:
        _codex_config, marketplaces, plugins = _self_codex_registrations()
    except CliError as exc:
        lines.append(f"~/.codex/config.toml（无法解析：{exc}）")
    else:
        if marketplaces or plugins:
            lines.append(
                "将注销 codex 登记：" + ", ".join(plugins + marketplaces))
        else:
            lines.append("~/.codex/config.toml：无 BugX 本地 marketplace 登记")
    bugx_home = _self_home_dir(".bugx")
    if bugx_home.is_symlink():
        lines.append("~/.bugx 是符号链接：拒绝清理，请人工检查")
    elif bugx_home.is_dir():
        lines.append("将清理 ~/.bugx/（全局引擎、知识库、云数据；先整体外部备份）")
    else:
        lines.append("~/.bugx/：不存在")
    return lines


def _self_uninstall_flow(args: argparse.Namespace, root: Path) -> int:
    """卸载 BugX 全机自有工件：全局引擎、宿主全局登记与 ~/.bugx 数据。

    项目级卸载（--all/--agent）只清理单个项目；本流程处理其余所有全局层
    残留。永远不触碰 pip 包本身（运行中的 CLI 不自删），只给出提示命令。
    """
    if args.all or args.agent or args.purge_data or args.purge_global_data:
        raise CliError(_i18n_exc("lifecycle.self_mutually_exclusive", "--self 不能与 --all/--agent/--purge-data/--purge-global-data 同时使用"))
    if (root / ".bugx" / "manifest.json").exists() and not args.dry_run:
        raise CliError(_i18n_exc("lifecycle.self_project_still_attached", "当前项目仍接入 BugX（存在 .bugx/manifest.json）；请先执行 bugx uninstall --root <项目> --all --yes --purge-data，再运行 --self"))
    if not args.yes and not args.dry_run:
        raise CliError(_i18n_exc("lifecycle.self_confirmation_required", "--self 会删除全局引擎与全局数据；确认请追加 --yes，或先用 --dry-run 查看清单"))

    if args.dry_run:
        payload = {
            "status": "DRY_RUN",
            "self": True,
            "inventory": _self_uninstall_inventory(),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"BugX {__version__} · 全局自卸载预览（--dry-run，未修改任何文件）")
            for line in payload["inventory"]:
                print(f"  {line}")
        return 0

    removed: list[str] = []
    preserved: list[str] = []
    external_backup: Path | None = None
    no_backup = bool(getattr(args, "no_backup", False))
    bugx_home = _self_home_dir(".bugx")
    if bugx_home.is_symlink():
        raise CliError(_i18n_exc("lifecycle.clean_global_symlink_refused", "拒绝清理符号链接 ~/.bugx"))
    backup_root: Path | None = None
    if bugx_home.is_dir() and not no_backup:
        backup_root = _self_backup_root()
        try:
            backup_root.relative_to(bugx_home)
        except ValueError:
            pass
        else:
            raise CliError(_i18n_exc("lifecycle.self_backup_outside_global_required", "自卸载备份目录必须位于 ~/.bugx 之外"))
        _backup_global_home_for_self_uninstall(bugx_home, backup_root)
        external_backup = backup_root

    _self_remove_zcode_registrations(backup_root, removed, preserved)
    _self_remove_zcode_user_agents(backup_root, removed, preserved)
    _self_remove_claude_user_agents(backup_root, removed, preserved)
    # 宿主专属的自卸载动作按约定模块名调用 ``self_remove_user_agents`` 钩子：
    # 通用层不再出现「某宿主的用户级智能体在哪、长什么样」。
    for host_adapter in HOST_ADAPTERS:
        self_remove = engine_host_cli_hook(host_adapter.name, "self_remove_user_agents")
        if self_remove is not None:
            self_remove(backup_root, removed, preserved)
    _self_remove_qoder_user_assets(backup_root, removed, preserved)
    _self_remove_funide_user_agent(backup_root, removed, preserved)
    _self_remove_funide_native_agent(backup_root, removed, preserved)
    _self_remove_funide_native_skill(backup_root, removed, preserved)
    _self_remove_funide_profile_mcp(backup_root, removed, preserved)
    _self_uninstall_opencode_links(external_backup, removed, preserved)
    _self_uninstall_pi_skill(backup_root, removed, preserved)
    _self_uninstall_dsh_preset(backup_root, removed, preserved)
    _self_uninstall_dsh_ui_plugin(backup_root, removed, preserved)
    _self_uninstall_workbuddy_skill(root, backup_root, removed, preserved)
    _self_uninstall_codex_plugins(backup_root, removed, preserved)
    if bugx_home.is_dir():
        # 引擎载荷可能是只读分发物（0o555/0o444）：必须强制删除，否则自卸载
        # 会在 .bugx/core 上抛 PermissionError（实测）。
        _rmtree_force(bugx_home)
        removed.append(
            "~/.bugx/（全局引擎、知识库与云数据；已外部备份）"
            if external_backup else "~/.bugx/（全局引擎、知识库与云数据）"
        )

    cli_source = os.environ.get("BUGX_CLI_SOURCE", "")
    if cli_source == "npm" or "bin/bugx.js" in sys.argv[0]:
        next_step = "如需移除 CLI 本体：npm uninstall -g bugx-cli"
    elif "uv/tools" in sys.executable or "uv/tools" in sys.argv[0] or ".local/share/uv" in sys.executable:
        next_step = "如需移除 CLI 本体：uv tool uninstall bugx-cli"
    else:
        next_step = "如需移除 CLI 本体：pip uninstall bugx-cli（若使用 npm，运行 npm uninstall -g bugx-cli）"

    payload = {
        "status": "UNINSTALLED",
        "self": True,
        "removed": removed,
        "preserved": preserved,
        "backup": str(external_backup) if external_backup else None,
        "next_step": next_step,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[bugx] 全局自卸载完成：移除 {len(removed)} 项，保留 {len(preserved)} 项")
        for item in removed:
            print(f"  - {item}")
        for item in preserved:
            print(f"  = {item}")
        if external_backup:
            print(f"[bugx] 备份：{external_backup}")
        print(f"[bugx] 下一步：{payload['next_step']}")
    return 0


def _uninstall_guidance_flow(args: argparse.Namespace, root: Path) -> int:
    """裸 uninstall 的引导路径：让用户在两级意图间二选一。

    1) 仅卸载当前项目：删除项目下的 .bugx/ 与各 AI 工具入口，本项目回到
       未接入状态（删除前自动备份到项目外）。
    2) 全局卸载：移除 BugX 本体，所有 AI 工具都无法再使用 BugX
       （若当前项目仍接入，先执行 1 再移除全局层）。

    TTY 走统一交互组件；非 TTY 输出两级选项与建议命令并返回 2。显式
    flag 路径（--agent/--all/--purge-*）不经过本函数，自动化行为不变。
    """
    hosts_payload = _read_hosts_record(root / ".bugx" / "hosts.json")
    installed = hosts_payload.get("installed", [])
    host_names: list[str] = []
    for item in installed:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            name = str(item["name"])
            if name not in host_names:
                host_names.append(name)
    data_dir = root / ".bugx"
    has_project = bool(host_names) or (data_dir.exists() and not data_dir.is_symlink())

    if not sys.stdin.isatty():
        payload = {
            "status": "NEEDS_SCOPE",
            "installed": host_names,
            "has_project_data": data_dir.exists(),
            "options": {
                "project": "仅卸载当前项目（删除本项目 .bugx/ 与各 AI 工具入口，"
                           "先外部备份）: bugx uninstall --root . --all --purge-data --yes",
                "global": "全局卸载（所有 AI 工具都不可用；先卸载当前项目）: "
                          "bugx uninstall --all --purge-data --yes && bugx uninstall --self --yes",
            },
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("[bugx] 卸载分两级，请选择（当前项目"
                  f"{'已接入 BugX' if has_project else '未接入 BugX'}）：")
            print("  1) 仅卸载当前项目：删除本项目下的 .bugx/ 与各 AI 工具入口，"
                  "本项目回到未接入状态；其他项目不受影响（删除前自动备份到项目外）")
            print("     bugx uninstall --root . --all --purge-data --yes")
            print("  2) 全局卸载：删除 BugX 本体（全局引擎与所有 AI 工具的登记），"
                  "所有 AI 工具都无法再使用 BugX")
            print("     bugx uninstall --all --purge-data --yes && bugx uninstall --self --yes")
        return 2

    _ensure_core_importable()
    from core.runtime import interaction
    from core.runtime import startup_environment

    options = []
    if has_project:
        options.append(interaction.InteractionOption(
            "project", "仅卸载当前项目",
            "删除本项目下的 .bugx/ 与各 AI 工具入口，本项目回到未接入状态；"
            "其他项目不受影响（删除前自动备份到项目外）"))
    options.append(interaction.InteractionOption(
        "global", "全局卸载（所有 AI 工具都不可用）",
        "删除 BugX 本体：全局引擎、全局数据与所有 AI 工具的登记"
        + ("；会先卸载当前项目" if has_project else "")))
    options.append(interaction.InteractionOption(
        "cancel", "取消", "不修改任何文件"))
    question = interaction.InteractionQuestion(
        question_id="uninstall_scope",
        phase="uninstall",
        kind="select",
        title="选择卸载范围",
        question=(
            "仅卸载当前项目 = 本项目回到未接入状态，其他项目不受影响；"
            "全局卸载 = 所有 AI 工具都无法再使用 BugX。请选择"
        ),
        options=tuple(options),
        multiple=False,
        default=len(options) - 1,
    )
    environment = startup_environment.detect(root)
    result = interaction.ask(question, environment)
    if result.status != interaction.ANSWERED or result.value == "cancel":
        print("[bugx] 已取消卸载，未修改任何文件")
        return 0
    if result.value not in {"project", "global"}:
        raise CliError(_i18n_exc("lifecycle.unknown_uninstall_scope", f"未知卸载范围选项: {result.value}", value=result.value))

    if result.value == "global" and has_project:
        # 全局卸载前先把当前项目摘干净，避免留下悬空的项目运行态。
        args.all = True
        args.agent = []
        args.purge_data = True
        args.purge_global_data = False
        args.yes = True
        args.json = False
        code = command_uninstall(args)
        if code != 0:
            return code

    if result.value == "global":
        # 注意：不能以 Namespace(**vars(args)) 重建——"self" 键与构造器形参
        # 冲突；直接原地改写后交给全局自卸载流程。
        args.self = True
        args.all = False
        args.agent = []
        args.purge_data = False
        args.purge_global_data = False
        args.dry_run = False
        args.yes = True
        args.json = False
        return _self_uninstall_flow(args, root)

    args.all = True
    args.agent = []
    args.purge_data = True
    args.purge_global_data = False
    args.yes = True
    return command_uninstall(args)


def _uninstall_dry_run_preview(
    root: Path,
    installed: list[object],
    selected_names: list[str],
    hosts_path: Path,
    *,
    purge_data: bool,
    purge_global_data: bool,
    force: bool = False,
) -> tuple[list[str], list[str]]:
    """Build an uninstall preview without invoking any mutating helper.

    The normal uninstall path has several side effects besides the final
    deletion: per-file backups, host-record updates, structured logging, and
    host-specific configuration cleanup.  A dry-run must not call those
    helpers even when ``--yes`` is present, so this function only inspects
    ownership records and current filesystem state.
    """
    planned: list[str] = []
    preserved: list[str] = []
    recorded_names = {
        str(item.get("name")) for item in installed if isinstance(item, dict)
    }
    seen: set[str] = set()

    def add_planned(label: str) -> None:
        if label not in seen:
            seen.add(label)
            planned.append(label)

    for item in installed:
        if not isinstance(item, dict) or item.get("name") not in selected_names:
            continue
        name = str(item["name"])
        records = item.get("files")
        if not isinstance(records, list):
            records = []
        for record in records:
            if not isinstance(record, dict):
                continue
            relative = record.get("path")
            expected = record.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                preserved.append(f"{relative or '<unknown>'}（清单缺少哈希）")
                continue
            target = _safe_record_target(root, relative)
            if target.is_symlink():
                preserved.append(f"{relative}（符号链接）")
                continue
            if not target.exists():
                continue
            if not target.is_file():
                preserved.append(f"{relative}（不是普通文件）")
                continue
            try:
                current_hash = _sha256_file(target)
            except OSError as exc:
                preserved.append(f"{relative}（无法读取：{exc}）")
                continue
            if current_hash != expected:
                if not force:
                    preserved.append(f"{relative}（内容已被用户修改）")
                    continue
                else:
                    add_planned(f"{relative}（内容已修改，将强制清理并备份）")
                    continue
            add_planned(relative)

        # These entries are intentionally not represented as ordinary hashed
        # files: OpenCode uses project symlinks/config merging, and ChatGPT
        # uses a shared marketplace entry.  List the affected surfaces without
        # calling their mutating uninstall implementations.
        if name == "opencode":
            for relative in (
                ".opencode/agents/bugx.md",
                ".opencode/agents/bugx-worker.md",
                CONFIG_NAME,
            ):
                if (root / relative).exists() or (root / relative).is_symlink():
                    add_planned(relative + "（按 BugX 归属检查）")
        elif name == "chatgpt":
            marketplace = root / ".agents" / "plugins" / "marketplace.json"
            if marketplace.exists() or marketplace.is_symlink():
                add_planned(str(marketplace.relative_to(root)) + "（移除 BugX 条目）")
        elif name == "dsh":
            for relative in (
                ".dsh/patch.yml",
                ".dsh/skills/bugx/SKILL.md",
                ".dsh/agents/bugx.md",
            ):
                if (root / relative).exists() or (root / relative).is_symlink():
                    add_planned(relative + "（按 BugX 归属检查）")

        for mcp_host in _mcp_config_host_names(name):
            target_info = _host_project_mcp_config_target(root, mcp_host)
            if not target_info:
                continue
            target_path, _ = target_info
            if target_path.is_file() and not target_path.is_symlink():
                try:
                    rel = str(target_path.relative_to(root))
                except ValueError:
                    rel = str(target_path)
                add_planned(f"{rel}（检查并清理 BugX MCP 注册）")

    for name in selected_names:
        if name not in recorded_names:
            preserved.append(f"{name}（当前项目没有本版本安装记录，未删除未知文件）")

    if purge_data:
        bugx_root = root / ".bugx"
        if bugx_root.exists() or bugx_root.is_symlink():
            add_planned(".bugx/（项目运行态、报告、证据与共享资源）")
        global_root = (Path.home() / ".bugx" / "knowledge").resolve()
        if purge_global_data and global_root.exists():
            add_planned("~/.bugx/knowledge/（全局错题集与经验集）")
        add_planned("项目级宿主登记与 BugX 配置（随 .bugx/ 一并清理）")
    elif hosts_path.exists():
        add_planned(".bugx/hosts.json（更新宿主登记）")

    return planned, preserved


def _mcp_config_host_names(name: str) -> tuple[str, ...]:
    """宿主的项目级 MCP 登记路径名（含多路径宿主的兼容副本）。

    安装侧（agent.py）对 ``entry_mode == ENTRY_MULTI_PATH`` 的宿主会把同一份 MCP
    登记**同时**写进 `.workbuddy/mcp.json` 与兼容的 `.codebuddy/mcp.json`。
    卸载必须成对清理，否则兼容副本里的 bugx-supervisor / worker / docs 会残留，
    宿主仍会去拉起已卸载项目的 BugX MCP。
    """
    for adapter in HOST_ADAPTERS:
        if adapter.name == name:
            if getattr(adapter, "entry_mode", None) == ENTRY_MULTI_PATH:
                return (name, "codebuddy")
            return (name,)
    return (name,)


def _remove_host_mcp_entries_for(
    root: Path,
    name: str,
    removed: list[str],
    preserved: list[str],
    *,
    backup_dir: Path | None = None,
) -> None:
    """清理宿主项目级 MCP 登记，含多路径宿主的 `.codebuddy` 兼容副本。"""
    # Claude Code and FunIDE intentionally share the project-root `.mcp.json`
    # shape.  Removing FunIDE must not tear down a Claude installation that is
    # still present in the same project; both hosts consume the same BugX
    # servers.  A FunIDE-only project has no `.claude` marker and is cleaned as
    # usual.
    if name == "funide" and (root / ".claude").exists():
        preserved.append(
            ".mcp.json（与 Claude Code 共用，卸载 FunIDE 时保留 BugX MCP）"
        )
    else:
        _remove_host_mcp_entry(root, name, removed, preserved, backup_dir=backup_dir)
    for mcp_host in _mcp_config_host_names(name):
        if mcp_host == name:
            continue
        _remove_host_mcp_entry(
            root, mcp_host, removed, preserved,
            backup_host=name, backup_dir=backup_dir,
        )


def command_uninstall(args: argparse.Namespace) -> int:
    """Safely remove BugX host entries while preserving user-owned assets."""
    root = args.root.resolve()
    if getattr(args, "self", False):
        return _self_uninstall_flow(args, root)
    if (root / ".bugx").is_symlink():
        raise CliError(_i18n_exc("lifecycle.operate_project_symlink_refused", "拒绝操作符号链接 .bugx"))
    if args.all and args.agent:
        raise CliError(_i18n_exc("lifecycle.all_and_agent_exclusive", "--all 不能与 --agent 同时使用"))
    if not args.all and not args.agent:
        return _uninstall_guidance_flow(args, root)
    if args.purge_data and not args.all:
        raise CliError(_i18n_exc("lifecycle.purge_data_all_only", "--purge-data 只能与 --all 一起使用"))
    if args.purge_global_data and not args.purge_data:
        raise CliError(_i18n_exc("lifecycle.purge_global_requires_purge_data", "--purge-global-data 必须与 --purge-data 一起使用"))
    if (args.purge_data or args.purge_global_data) and not args.yes and not args.dry_run:
        raise CliError(_i18n_exc("lifecycle.purge_data_yes_required", "清理数据需要显式提供 --yes；执行前会创建外部备份"))

    force = bool(getattr(args, "force", False))
    hosts_path = root / ".bugx" / "hosts.json"
    hosts_payload = _read_hosts_record(hosts_path)
    installed = hosts_payload.get("installed", [])
    if not isinstance(installed, list):
        raise CliError(_i18n_exc("lifecycle.hosts_installed_not_array", "宿主清单 installed 必须是数组"))
    known: dict[str, HostAdapter] = {adapter.name: adapter for adapter in HOST_ADAPTERS}
    for adapter in HOST_ADAPTERS:
        for alias in adapter.aliases:
            known[alias] = adapter
    raw_names = (
        [str(item.get("name")) for item in installed if isinstance(item, dict)]
        if args.all else list(dict.fromkeys(args.agent))
    )
    for name in raw_names:
        if name not in known:
            choices = ", ".join(adapter.name for adapter in HOST_ADAPTERS)
            raise CliError(_i18n_exc("lifecycle.unknown_ai_agent", f"未知 AI Agent: {name}；可选：{choices}", name=name, known=choices))
    selected_names = list(dict.fromkeys(known[name].name for name in raw_names))

    if args.dry_run:
        planned, preserved = _uninstall_dry_run_preview(
            root,
            installed,
            selected_names,
            hosts_path,
            purge_data=bool(args.purge_data),
            purge_global_data=bool(args.purge_global_data),
            force=force,
        )
        payload = {
            "status": "DRY_RUN",
            "root": str(root),
            "hosts": selected_names,
            "removed": planned,
            "preserved": preserved,
            "purged_project_data": bool(args.purge_data),
            "purged_global_data": bool(args.purge_global_data),
            "backup": None,
            "backup_planned": bool(args.purge_data) and not getattr(args, "no_backup", False),
            "dry_run": True,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("[bugx] 卸载预览（--dry-run，未修改任何文件）")
            for item in planned:
                print(f"  - {item}")
            for item in preserved:
                print(f"  = {item}")
            if args.purge_data:
                if not getattr(args, "no_backup", False):
                    print("  备份：正式执行时会创建项目外部备份；本次未创建")
                else:
                    print("  备份：跳过备份（--no-backup）")
        return 0

    external_backup: Path | None = None
    uninstall_backup_dir: Path | None = None
    # Global knowledge is a user-owned singleton.  Never let an environment
    # variable turn ``uninstall --purge-global-data`` into an arbitrary-path
    # deletion primitive; test/development overrides are intentionally ignored
    # by destructive cleanup.
    bugx_home_lexical = Path.home() / ".bugx"
    global_root = bugx_home_lexical / "knowledge"
    try:
        global_root.relative_to(bugx_home_lexical)
    except ValueError as exc:  # pragma: no cover - fixed child path invariant
        raise CliError("全局知识卸载目标必须位于 ~/.bugx 内") from exc
    if args.purge_global_data:
        _assert_no_symlink_components(global_root)
    no_backup = bool(getattr(args, "no_backup", False))
    if args.purge_data and not no_backup:
        backup_root = _external_uninstall_backup_root(root)
        try:
            backup_root.relative_to(root)
        except ValueError:
            pass
        else:
            raise CliError(_i18n_exc("lifecycle.uninstall_backup_outside_root", "卸载备份目录必须位于项目根目录之外"))
        if (root / ".bugx").exists():
            _backup_tree_external(root / ".bugx", backup_root / "project-bugx")
        if args.purge_global_data and global_root.exists():
            _backup_tree_external(global_root, backup_root / "global-knowledge")
        external_backup = backup_root
        # P2-1：宿主文件逐个备份直接重定向到项目外部归档；purge 删除
        # .bugx（连同其中的 .bugx/backups）后这些备份仍然可恢复。
        uninstall_backup_dir = backup_root / "project-uninstall-backups"

    removed: list[str] = []
    preserved: list[str] = []
    remaining: list[object] = []
    for item in installed:
        if not isinstance(item, dict) or item.get("name") not in selected_names:
            remaining.append(item)
            continue
        name = str(item["name"])
        records = item.get("files")
        if not isinstance(records, list) or not records:
            # OpenCode 入口按设计是符号链接，files 清单恒为空；只要记录还带
            # entry_symlink 字段，下方专分支就会清理入口与全局注册，无需误报。
            has_opencode_entry = name == "opencode" and isinstance(
                item.get("entry_symlink"), str
            )
            if not has_opencode_entry:
                preserved.append(f"{name}（缺少本版本安装清单，未删除宿主文件）")
            records = []
        if name == "chatgpt":
            # Marketplace is a shared user catalog.  It is edited structurally
            # below so unrelated plugins are retained; never delete the whole
            # JSON file through the ordinary hash-owned-file path.
            records = [
                record for record in records
                if not isinstance(record, dict)
                or record.get("path") != ".agents/plugins/marketplace.json"
            ]
        for record in records:
            _remove_owned_record(root, record, name, removed, preserved, uninstall_backup_dir, force=force)
        _remove_host_mcp_entries_for(root, name, removed, preserved, backup_dir=uninstall_backup_dir)
        if name == "chatgpt":
            _remove_chatgpt_marketplace_entry(root, removed, preserved, backup_dir=uninstall_backup_dir)
            _deregister_codex_plugin(root, removed, preserved)
        if name == "dsh":
            _remove_dsh_user_preset(root, removed, preserved, purge_global=args.purge_global_data, backup_dir=uninstall_backup_dir)
            _remove_dsh_project_files(root, removed, preserved, records, backup_dir=uninstall_backup_dir, force=force)
        if name == "opencode":
            pass
        if name == "claude":
            from .plugin_claude import (
                _claude_user_agents_dir,
                _remove_claude_project_skills_and_dist,
            )
            _remove_claude_project_skills_and_dist(root, removed, preserved, backup_dir=uninstall_backup_dir)
            user_agents_dir = _claude_user_agents_dir()
            kept_agents = [
                filename for filename in ("bugx.md", "bugx-worker.md")
                if (user_agents_dir / filename).is_file()
            ]
            if kept_agents:
                preserved.append(
                    f"{user_agents_dir}/{'、'.join(kept_agents)}"
                    "（Claude Code 用户级智能体保留，供其他项目使用；全局清理需 uninstall --self）"
                )
        for surface in engine_host_user_agent_surfaces(name):
            kept_agents = [
                filename for filename in surface["entries"]
                if (surface["path"] / filename).is_file()
            ]
            if kept_agents:
                preserved.append(
                    f"{surface['path']}/{'、'.join(kept_agents)}"
                    f"（{surface['label']} 用户级智能体保留，供其他项目使用；"
                    "全局清理需 uninstall --self）"
                )
        if name == "pi":
            _unlink_global_pi_skill(root, removed, preserved)
        if name == "zcode":
            # 用户级子智能体跨项目共享；MCP 配置只保留在各自项目中；
            # 项目级卸载不删，只如实报告，全局清理走 uninstall --self。
            user_agents_dir = _zcode_user_agents_dir()
            kept_agents = [
                filename for filename in ("bugx.md", "bugx-worker.md")
                if (user_agents_dir / filename).is_file()
            ]
            if kept_agents:
                preserved.append(
                    f"{user_agents_dir}/{'、'.join(kept_agents)}"
                    "（用户级子智能体保留，供其他项目使用；全局清理需 uninstall --self）"
                )
        if name == "workbuddy":
            # 用户级 Skill 与 zcode 用户级 agents 同层：跨项目共享，
            # 项目级卸载只报告保留，全局清理统一走 uninstall --self。
            user_skill = workbuddy_user_skill_root() / "skills" / "bugx" / "SKILL.md"
            if user_skill.is_file():
                preserved.append(
                    f"{user_skill}"
                    "（WorkBuddy 用户级 Skill 保留，供其他项目使用；全局清理需 uninstall --self）"
                )
        if name == "qoder":
            from .plugin_qoder import _qoder_user_agents_dir
            user_agents_dir = _qoder_user_agents_dir()
            kept_agents = [
                filename for filename in ("bugx.md", "bugx-worker.md")
                if (user_agents_dir / filename).is_file()
            ]
            if kept_agents:
                preserved.append(
                    f"{user_agents_dir}/{'、'.join(kept_agents)}"
                    "（Qoder 用户级智能体保留，供其他项目使用；全局清理需 uninstall --self）"
                )
        if name == "funide":
            from .plugin_funide import (
                _funide_data_dir,
                _funide_profile_mcp_path,
                _funide_user_agents_dir,
                _funide_user_skills_dir,
            )
            user_agent = _funide_user_agents_dir() / "bugx.agent.md"
            if user_agent.is_file():
                preserved.append(
                    f"{user_agent}"
                    "（FunIDE 用户级智能体保留，供其他项目使用；全局清理需 uninstall --self）"
                )
            native_agent = _funide_data_dir() / "agents.json"
            if native_agent.is_file():
                preserved.append(
                    f"{native_agent}"
                    "（FunIDE 设置页原生智能体注册保留；全局清理需 uninstall --self）"
                )
            native_skill = _funide_user_skills_dir() / "bugx" / "SKILL.md"
            if native_skill.is_file():
                preserved.append(
                    f"{native_skill}"
                    "（FunIDE 设置页原生技能保留；全局清理需 uninstall --self）"
                )
            native_mcp = _funide_profile_mcp_path()
            if native_mcp.is_file():
                preserved.append(
                    f"{native_mcp}"
                    "（FunIDE 设置页用户级 MCP 保留；全局清理需 uninstall --self）"
                )

    # A requested host not present in hosts.json has no ownership proof.  The
    # clean-break release never guesses ownership or removes old-layout files.
    recorded_names = {
        str(item.get("name")) for item in installed if isinstance(item, dict)
    }
    for name in selected_names:
        if name in recorded_names:
            continue
        _remove_host_mcp_entries_for(root, name, removed, preserved, backup_dir=uninstall_backup_dir)
        if name == "chatgpt":
            _remove_chatgpt_marketplace_entry(root, removed, preserved, backup_dir=uninstall_backup_dir)
            _deregister_codex_plugin(root, removed, preserved)
        elif name == "dsh":
            # No hosts.json record means there is no ownership proof.  In
            # particular, an empty record list triggers the legacy cleanup
            # fallback in _remove_dsh_project_files, so preserve all DSH files.
            _remove_dsh_project_files(root, removed, preserved, [], backup_dir=uninstall_backup_dir, force=force)
        elif name == "opencode":
            pass
        elif name == "pi":
            _unlink_global_pi_skill(root, removed, preserved)
        else:
            adapter = known.get(name)
            if (adapter is not None and adapter.name in _SWEEPABLE_SKILL_ADAPTERS
                    and (global_engine_root() / "bugx.md").is_file()):
                _sweep_unrecorded_host_entry(root, adapter, removed, preserved, uninstall_backup_dir, force=force)
            else:
                preserved.append(f"{name}（当前项目没有本版本安装记录，未删除宿主文件）")

    if args.all:
        # "像新项目一样"：对没有任何安装记录的宿主入口与项目级 MCP 做清扫。
        recorded = {
            str(item.get("name")) for item in installed if isinstance(item, dict)
        }
        for adapter in HOST_ADAPTERS:
            if adapter.name not in recorded:
                _remove_host_mcp_entries_for(root, adapter.name, removed, preserved, backup_dir=uninstall_backup_dir)
                if (global_engine_root() / "bugx.md").is_file():
                    _sweep_unrecorded_host_entry(root, adapter, removed, preserved, uninstall_backup_dir, force=force)

    if not args.purge_data and hosts_path.exists():
        _write_private_json(hosts_path, {
            **{key: value for key, value in hosts_payload.items() if key != "installed"},
            "installed": remaining,
        })
    else:
        # Layout 2 has no project copy of the executable engine.  Never
        # reconstruct a legacy ``.bugx/core`` inventory during uninstall.
        shared_records = hosts_payload.get("shared_files", [])
        if not isinstance(shared_records, list):
            shared_records = []
        for record in shared_records:
            _remove_owned_record(root, record, "shared-runtime", removed, preserved, uninstall_backup_dir)
        _purge_private_plugin_stores(root, removed, preserved)
        bugx_root = root / ".bugx"
        if bugx_root.is_symlink():
            raise CliError(_i18n_exc("lifecycle.clean_project_symlink_refused", "拒绝清理符号链接 .bugx"))
        if bugx_root.is_dir():
            if args.purge_data and external_backup is not None:
                # The initial external snapshot predates host-file deletion.
                # Persist the uninstall backups and the final diagnostic log
                # before removing the project runtime, otherwise the reported
                # recovery archive would silently miss the files just removed.
                _log_update_event(
                    root,
                    "uninstall_completed",
                    "BugX 项目卸载即将完成",
                    removed_count=len(removed),
                    preserved_count=len(preserved),
                    purge_data=True,
                    backup=str(external_backup),
                )
                try:
                    _backup_uninstall_delta(root, external_backup)
                    _write_uninstall_receipt(
                        external_backup,
                        root=root,
                        hosts=selected_names,
                        removed=removed,
                        preserved=preserved,
                        purge_global_data=bool(args.purge_global_data),
                    )
                except (OSError, ValueError, CliError) as exc:
                    raise CliError(_i18n_exc("lifecycle.uninstall_backup_failed", f"卸载补充备份失败，已停止删除项目运行态：{exc}", exc=exc)) from exc
            # Close active logging handlers before purging the log directory
            import logging
            logger = logging.getLogger("bugx")
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
            shutil.rmtree(bugx_root)
            # 与 --dry-run 预览保持一致：项目运行态被清掉必须在 removed 里可查，
            # 否则用户只能靠 ls 猜（removed 为空时会误以为没清干净）。
            removed.append(".bugx/（项目运行态、报告、证据与共享资源）")
        if args.purge_global_data and global_root.exists():
            if global_root.is_symlink():
                raise CliError(_i18n_exc("lifecycle.clean_global_knowledge_symlink_refused", "拒绝清理符号链接全局错题集目录"))
            shutil.rmtree(global_root)
            removed.append("~/.bugx/knowledge/（全局错题集与经验集）")
    _remove_empty_parents(root, removed)
    payload = {
        "status": "UNINSTALLED",
        "root": str(root),
        "hosts": selected_names,
        "removed": removed,
        "preserved": preserved,
        "purged_project_data": bool(args.purge_data),
        "purged_global_data": bool(args.purge_global_data),
        "backup": str(external_backup) if external_backup else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[bugx] 卸载完成：删除 {len(removed)} 个 BugX 文件，保留 {len(preserved)} 个用户文件")
        if preserved:
            print("  未删除（内容已变化或归属不明）：" + "；".join(preserved))
        if external_backup:
            print(f"  数据备份：{external_backup}")
        backup_note = (
            f"外部归档 {external_backup}/project-uninstall-backups"
            if external_backup else ".bugx/backups"
        )
        print(
            "  测试报告、证据和错题集默认保留；被移除的宿主配置已备份到 "
            f"{backup_note}，OpenCode 中 BugX 添加的 MCP 已移除（用户自有配置保留）"
        )
    return 0


def _assert_writable(directory: Path, label: str) -> None:
    """写探针：目录不可写时立即以 CliError 阻断（探针文件随即删除，零残留）。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".bugx-write-probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise CliError(f"{label}不可写：{directory}: {exc}") from exc


def _precheck_initialization(root: Path, source: Path) -> None:
    """F-L0-04 B 预检阶段：把可预知的失败全部前移到写盘之前（零落盘）。"""
    if not source.is_dir():
        raise CliError(_i18n_exc(
            "common.asset_root_unavailable",
            f"安装资源不可用，初始化已停止：{source}", source=source,
        ))
    _assert_writable(root, "项目根目录")
    _assert_no_symlink_components(root / ".bugx")
    # 引擎目的地的权威校验留在 ``_install_engine``（避免两处解析口径漂移导致误报）；
    # 此处只预检父目录可写性，把「目的地不可写」这类失败提前到写盘之前。
    _assert_writable(global_engine_root().parent, "全局引擎父目录")


def _plan_cloud_onboarding(
    root: Path, *, cloud_sync_choice: str, had_onboarding: bool,
    cloud_sync_explicit: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """F-L0-04 A 决策阶段：判定云端 onboarding 目标动作（**项目状态零写入**）。

    返回 ``(plan, exit_code)``：

    * ``plan is not None`` → 决策已确定，交由 D 阶段落地（``exit_code`` 无意义）；
    * ``plan is None`` → 决策未完成，调用方必须**在写盘前**以 ``exit_code`` 阻断
      （``2`` = 决策未完成；``1`` = onboarding 检查异常）。

    既有语义（必须保留）：
    * ``local`` / ``sync`` 是**已完成的用户决策**，同步未执行也放行；
    * ``done``（已开启同步）但本次同步未完成 → **必须阻断**（不得静默放行）；
    * 状态非法或异常 → 阻断。
    """
    # An explicit CLI choice is authoritative even when a previous attempt
    # left an onboarding file in ``pending``.  Defer its side effects until
    # the commit phase, after project state (including the lesson ledger) has
    # been initialized.  Probing sync here can otherwise make first-time
    # ``init --cloud-sync sync`` fail before it creates the very ledger that
    # sync requires.
    if not had_onboarding and cloud_sync_explicit and cloud_sync_choice in {"local", "sync"}:
        action = "enable_sync" if cloud_sync_choice == "sync" else "set_local"
        return {"action": action}, 0
    if not had_onboarding:
        if cloud_sync_choice == "sync":
            return {"action": "enable_sync"}, 0
        return {"action": "set_local"}, 0
    try:
        _ensure_core_importable()
        from core.cloud_sync.cloud_onboard import get_state
        from core.cloud_sync.onboard_wizard import maybe_onboard
        onboarding_ok = maybe_onboard(root, perform_sync=True)
        state = get_state(root)
    except Exception as exc:  # noqa: BLE001
        print(f"[bugx] 云端 onboarding 检查失败，初始化已停止：{exc}")
        return None, 1
    if state not in {"done", "local", "sync"} or (
        state not in {"local", "sync"} and not onboarding_ok
    ):
        return None, 2
    return {"action": "already_decided", "state": state}, 0


def _apply_cloud_onboarding_plan(
    root: Path, plan: dict[str, Any], preset_data: dict[str, Any],
) -> str:
    """F-L0-04 D 阶段：落地云端 onboarding 决策。

    与既有语义保持一致（本地/同步分支只记录意图，已有状态才执行同步）；
    同步失败按既有设计降级为 ``sync_pending``，**不阻断初始化**。
    """
    _ensure_core_importable()
    from core.cloud_sync.cloud_onboard import CloudOnboardDecisionRequired, get_state, set_state
    from core.cloud_sync.onboard_wizard import maybe_onboard

    action = plan["action"]
    if action == "set_local":
        set_state(root, "local")
        return "local"
    if action == "enable_sync":
        set_state(root, "sync")
        # 用户已在命令行显式选择同步（意图明确），因此项目身份与登记必须在
        # init 阶段实际落盘，由 ensure_project_identity 确定性生成标准 v2 身份文件。
        try:
            from core.cloud_sync.onboard_wizard import ensure_project_identity
            ensure_project_identity(root)
            maybe_onboard(
                root, perform_sync=True, allow_identity_create=False,
                require_interaction=False,
            )
        except CloudOnboardDecisionRequired as exc:
            print(f"  （项目登记延后到首次同步：{exc}）")
        except Exception as exc:  # noqa: BLE001
            # 登记失败不得回滚用户已做出的同步决策；状态保持 `sync`，
            # 由首次同步重试，并在输出里暴露真实失败原因。
            print(f"  （项目登记未完成：{exc}；首次同步将重试）")
        return "sync"
    # action == "already_decided"：A 阶段已完成同步验证，D 阶段无需重复动作
    return str(plan.get("state") or get_state(root))


def _commit_with_self_heal(commit, *, retries: int = 1):
    """F-L0-04 D 阶段包装：写入失败先自愈重试；仍失败则返回 None（绝不谎报成功）。

    ``CliError`` 属决策类失败（已由 B 预检覆盖），直接抛出交由 CLI 统一处理。
    """
    last: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return commit()
        except CliError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                print(
                    f"  ⚠ 初始化写入失败，正在自愈重试（{type(exc).__name__}: {exc}）",
                    file=sys.stderr,
                )
    print(f"[bugx] 初始化失败：{type(last).__name__}: {last}", file=sys.stderr)
    return None


def command_init(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    preset_data = {}
    try:
        _ensure_core_importable()
        from core.dev_harness.interaction_preset import _read_preset_raw_payload
        preset_data = _read_preset_raw_payload(root)
    except Exception:
        preset_data = {}

    cloud_sync_explicit = bool(getattr(args, "cloud_sync_explicit", False))
    if not cloud_sync_explicit and "cloud_sync" in preset_data:
        setattr(args, "cloud_sync", preset_data["cloud_sync"])
        cloud_sync_explicit = True
        setattr(args, "cloud_sync_explicit", True)

    if not getattr(args, "commit_policy", None) and "commit_policy" in preset_data:
        setattr(args, "commit_policy", preset_data["commit_policy"])

    # argparse suppresses the cloud-sync default so headless callers cannot
    # silently turn an omitted user decision into local mode.
    if not cloud_sync_explicit and not sys.stdin.isatty():
        print(
            "[bugx] 非交互模式禁止静默选择云端同步策略；请显式传入 "
            "--cloud-sync local 或 --cloud-sync sync。",
            file=sys.stderr,
        )
        return 2
    root.mkdir(parents=True, exist_ok=True)
    source = asset_root()

    # F-L0-04 整改（commit-or-nothing）：所有**已知**失败路径都必须在写盘前阻断。
    # ---- B 预检阶段（零落盘）------------------------------------------------
    _precheck_initialization(root, source)

    # ---- A 决策阶段（零落盘）：云端同步决策必须在任何写盘之前解析完成 --------
    onboarding_path = root / ".bugx" / "cloud-onboard.json"
    had_onboarding = onboarding_path.is_file()
    cloud_sync_choice = getattr(args, "cloud_sync", "local")
    onboarding_plan, onboarding_gate_code = _plan_cloud_onboarding(
        root, cloud_sync_choice=cloud_sync_choice, had_onboarding=had_onboarding,
        cloud_sync_explicit=cloud_sync_explicit,
    )
    if onboarding_plan is None:
        if onboarding_gate_code != 1:
            print(
                "[bugx] 云端 onboarding 未完成；初始化/重置不会跳过登录门禁，"
                "请完成云端授权或明确选择本地模式。"
            )
        return onboarding_gate_code

    was_initialized = (root / ".bugx" / "manifest.json").is_file()

    # ---- D 原子提交阶段：唯一写盘窗口（含自愈重试）--------------------------
    # 说明（O3 未落地）：项目状态**不能**整体在 staging 内构建——
    # ``_assemble_project_materials`` / ``prep_validate`` / ``coverage_bootstrap``
    # 必须扫描**真实项目树**才能生成覆盖清单（实测：指向 staging 会得到
    # 「清单漏列全部最小单元」），且既有 ``.bugx`` 下的用户自定义目录必须原样保留。
    # 因此本阶段仍直接写真实项目根；零落盘由 A/B 阶段保证。
    def _commit_project_state() -> dict[str, Any]:
        # DOC-004 Layout 2：初始化只创建项目状态，不创建项目可执行引擎。
        # 旧项目的可执行入口按方案三默认直接删除，避免继续暴露双运行时。
        removed = _require_global_only_layout(root)
        copied_, skipped_ = _install_engine(source, args.force)
        refresh_i18n_after_engine_swap()
        _ensure_global_cli_symlinks()
        layout_ = _layout_module(root).initialize(root, bugx_version=__version__)
        _ensure_project_readme(root, source)
        _assemble_project_materials(root, source, layout_)
        _initialize_lesson_ledger(root, source)
        inherited_ = {"imported": 0, "skipped": 0}
        if not was_initialized:
            inherited_ = _inherit_project_knowledge(root)
        _initialize_project_knowledge_snapshots(root, source)
        life_created_ = _initialize_life_state(root)
        commit_policy_ = getattr(args, "commit_policy", None)
        profile_file_ = None
        if commit_policy_:
            _ensure_core_importable()
            from core.runtime.session_profile import save_commit_policy
            profile_file_ = save_commit_policy(commit_policy_, root=root)
        _apply_cloud_onboarding_plan(root, onboarding_plan, preset_data)
        return {
            "removed": removed, "copied": copied_, "skipped": skipped_,
            "layout": layout_, "inherited": inherited_,
            "life_created": life_created_, "profile_file": profile_file_,
            "commit_policy": commit_policy_,
        }

    committed = _commit_with_self_heal(_commit_project_state)
    if committed is None:
        return 1
    removed_legacy_core = committed["removed"]
    copied = committed["copied"]
    skipped = committed["skipped"]
    layout = committed["layout"]
    inherited = committed["inherited"]
    life_created = committed["life_created"]
    profile_file = committed["profile_file"]
    commit_policy = committed["commit_policy"]
    # ---- E 收尾阶段（降级不阻断）-------------------------------------------
    try:
        # Ensure the project diagnostic log file exists so that subsequent
        # commands (and tests that read a baseline) see a readable path.
        log_file = root / ".bugx" / "logs" / "bugx.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.touch(exist_ok=True)
        log_file.chmod(0o600)
        _ensure_project_startup_todo(root)
    except OSError as exc:
        print(f"  ⚠ 收尾步骤未完成（不影响初始化结果）：{exc}", file=sys.stderr)
    try:
        _ensure_core_importable()
        from core.runtime import bugx_logging
        logger = bugx_logging.configure(root, command="init")
        bugx_logging.log_event(
            logger,
            "lifecycle_initialized",
            f"项目初始化完成: {root}",
            extra={
                "project_root": str(root),
                "cloud_sync": cloud_sync_choice,
                "commit_policy": commit_policy or "unset",
                "layout_version": getattr(layout, "manifest", {}).get("layout_version", "2.0.0") if hasattr(layout, "manifest") else "2.0.0",
            },
        )
    except Exception:
        pass

    # ---- F 输出阶段：仅在关键写入全部成功后打印 ----------------------------
    print(f"[bugx] 初始化完成: 全局引擎 {global_engine_root()}")
    print("  项目布局：仅保存 .bugx 状态/配置；可执行引擎统一来自全局目录")
    if removed_legacy_core:
        print(f"  已直接删除旧项目引擎入口：{removed_legacy_core}")
    print(f"  运行目录：{layout.root}")
    print(f"  写入 {copied} 个文件，保留 {skipped} 个已有文件")
    if life_created:
        print("  已创建 .bugx 当前生命周期生命状态（1 条初始寿命）")
    else:
        print("  保留已有 .bugx 当前生命周期生命状态；未重置运行状态")
    print("  错题集：项目 SQLite 已建立；全局通用库按用户目录初始化/复用（Agent 只读）")
    if profile_file:
        print(f"  提交策略：{commit_policy}（配置已写入 {profile_file}）")
    if not was_initialized:
        print(f"  已从全局通用库继承 active 模式：{inherited.get('imported', 0)} 条")
    print("  宿主：未绑定；请使用 `bugx install --agent <宿主>` 安装原生入口")
    if skipped:
        print("  使用 --force 可更新静态 Agent 文件；运行时状态不会被覆盖")
    try:
        from .cloud import confirm_data_sharing
        confirm_data_sharing(args)
    except Exception:
        pass
    return 0


def _global_opencode_integrity_check() -> tuple[bool, str]:
    agents_dir = _global_opencode_agents_dir()
    engine = global_engine_root()
    missing: list[str] = []
    for filename in ("bugx.md", "bugx-worker.md"):
        link = agents_dir / filename
        alt_name = "bugx-opencode.md" if filename == "bugx.md" else filename
        cand = engine / "contracts" / alt_name
        expected = cand if cand.is_file() else (engine / alt_name)
        try:
            linked = (link.is_symlink()
                      and link.resolve(strict=True) == expected.resolve(strict=True))
            # 无符号链接特权（Windows 非管理员/非开发者模式）时安装会发布
            # 字节一致的副本；哈希一致同样视为有效入口。
            copied = (not link.is_symlink() and link.is_file()
                      and _sha256_file(link) == _sha256_file(expected))
            if not (linked or copied):
                missing.append(filename)
        except OSError:
            missing.append(filename)
    if missing:
        return False, "全局入口缺失或未指向当前引擎：" + ", ".join(missing)
    return True, "全局 bugx/bugx-worker 入口均指向当前引擎"


def _global_pi_integrity_check(root: Path) -> tuple[bool, str]:
    project_skill = root / ".pi" / "skills" / "bugx" / "SKILL.md"
    home = Path.home().resolve()
    global_skill = home / ".agents" / "skills" / "bugx" / "SKILL.md"
    pi_link = home / ".pi" / "skills" / "bugx"
    pi_agent_link = home / ".pi" / "agent" / "skills" / "bugx"
    try:
        if not project_skill.is_file() or project_skill.is_symlink():
            return False, "项目 Pi Skill 缺失或不安全"
        if not global_skill.is_file() or global_skill.is_symlink():
            return False, "全局 Pi Skill 缺失或不安全"
        if _sha256_file(project_skill) != _sha256_file(global_skill):
            try:
                global_text = global_skill.read_text(encoding="utf-8")
                if "BugX for ChatGPT" in global_text or "BugX L0 路由入口" in global_text:
                    return True, "全局 Skill 由 ChatGPT/L0 路由占用，Pi 兼容共存"
            except OSError:
                pass
            return False, "全局 Pi Skill 与项目版本哈希不一致"

        def _check_link(link: Path) -> bool:
            linked = (link.is_symlink()
                      and link.resolve(strict=True) == global_skill.parent.resolve())
            copied_dir = (not link.is_symlink() and link.is_dir()
                          and (link / "SKILL.md").is_file()
                          and _sha256_file(link / "SKILL.md") == _sha256_file(global_skill))
            return linked or copied_dir

        if not (_check_link(pi_link) or _check_link(pi_agent_link)):
            return False, "~/.pi/skills/bugx 或 ~/.pi/agent/skills/bugx 未指向全局 Pi Skill"
    except OSError as exc:
        return False, f"全局 Pi Skill 无法校验: {exc}"
    return True, "项目、全局 Pi Skill 与发现入口哈希一致"
