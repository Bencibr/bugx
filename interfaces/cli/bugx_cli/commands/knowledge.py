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


def _rebuild_global_lesson(root: Path, args: argparse.Namespace) -> int:
    """Explicitly rebuild the global SQLite projection from its JSON seed."""
    if not getattr(args, "from_seed", False) or not getattr(args, "confirm_data_loss", False):
        raise CliError(
            "全局错题集重建必须同时提供 --from-seed --confirm-data-loss；"
            "该操作会替换 SQLite 运行库"
    )
    layout_module = _layout_module(root)
    global_root = layout_module.global_knowledge_root().resolve()
    _assert_no_symlink_components(global_root)
    seed = global_root / "global.json"
    database = global_root / "global.sqlite3"
    marker = global_root / "manifest.json"
    for path in (seed, database, marker):
        if path.is_symlink():
            raise CliError(f"拒绝使用符号链接全局错题集文件: {path}")
    if not seed.is_file():
        raise CliError(f"全局错题集 JSON 种子不存在: {seed}；请先从备份恢复")
    global_root.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    created_database = False
    try:
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(database) + suffix)
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise CliError(f"拒绝覆盖不安全的全局 SQLite 文件: {source}")
            backup = Path(str(source) + ".corrupt.bak")
            if backup.exists():
                backup = Path(str(source) + f".corrupt.{time.time_ns()}.bak")
            shutil.move(str(source), str(backup))
            moved.append((source, backup))
        process = subprocess.run(
            [sys.executable, str(_script(root, "lesson_ledger.py")),
             "init", "--ledger", str(seed), "--json"],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment(),
        )
        created_database = database.is_file()
        if process.returncode != 0 or not created_database:
            detail = (process.stderr or process.stdout).strip()[:800]
            raise CliError(f"全局错题集重建失败: {detail or 'lesson_ledger init 返回非 0'}")
        database.chmod(0o600)
        seed.chmod(0o600)
        if not marker.exists():
            _write_private_json(marker, {
                "schema_version": 1,
                "scope": "global",
                "authority": "sqlite",
                "read_only_for_agents": True,
                "seed": seed.name,
                "database": database.name,
                "initialized_at": time.time(),
                "rebuild": True,
            })
    except BaseException:
        if created_database:
            for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
                try:
                    path.unlink()
                except OSError:
                    pass
        for source, backup in reversed(moved):
            if backup.exists() and not source.exists():
                shutil.move(str(backup), str(source))
        raise
    payload = {
        "status": "REBUILT",
        "seed": str(seed),
        "database": str(database),
        "corrupt_backup": str(moved[0][1]) if moved else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"全局错题集已从 JSON 种子重建: {database}")
        if moved:
            print(f"原 SQLite 已保留: {moved[0][1]}")
    return 0


def command_lesson(args: argparse.Namespace) -> int:
    """管理结构化复盘账本；写入只通过外部账本协议完成。"""
    root = args.root.resolve()
    if args.lesson_command == "rebuild":
        return _rebuild_global_lesson(root, args)
    if args.ledger is None:
        ledger_target = _layout_module(root).require(root).knowledge_seed
    else:
        layout = _layout_module(root).require(root)
        global_root = getattr(layout, "global_knowledge_root", None)
        candidate = args.ledger if args.ledger.is_absolute() else root / args.ledger
        resolved = candidate.resolve(strict=False)
        if global_root is not None and resolved.is_relative_to(global_root.resolve()):
            _assert_no_symlink_components(resolved)
            ledger_target = resolved
        else:
            ledger_target = _rooted(root, args.ledger)
    if args.lesson_command in {"init", "bootstrap", "migrate"} and not ledger_target.exists():
        bundled_seed = asset_root() / "lesson-ledger.json"
        if not bundled_seed.is_file() or bundled_seed.is_symlink():
            raise CliError(_i18n_exc("knowledge.init_seed_missing", "初始化错题集失败：缺少内置 JSON 种子"))
        ledger_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled_seed, ledger_target)
    values = [args.lesson_command, "--ledger", str(ledger_target)]
    command = args.lesson_command
    if command == "validate" and getattr(args, "deep", False):
        # 对外只保留一个自然入口；底层脚本的 validate-deep 是实现命令，
        # 不再让用户同时维护两个几乎相同的校验入口。
        values[0] = "validate-deep"
    if command == "retrieve":
        values.extend(["--query", args.query, "--limit", str(args.limit)])
        if args.category:
            values.extend(["--category", args.category])
        if args.token_budget is not None:
            values.extend(["--token-budget", str(args.token_budget)])
        if args.byte_budget is not None:
            values.extend(["--byte-budget", str(args.byte_budget)])
        for lesson_id in args.exclude_lesson_id:
            values.extend(["--exclude-lesson-id", lesson_id])
    elif command == "incident-lookup":
        values.extend(["--query", args.query, "--limit", str(args.limit)])
        for signature in args.signature:
            values.extend(["--signature", signature])
    elif command == "incident-add":
        if args.input is None:
            raise CliError(_i18n_exc("knowledge.incident_add_input_required", "lesson incident-add 必须提供 --input"))
        values.extend(["--input", str(_rooted(root, args.input))])
    elif command == "evidence-add":
        if args.input is None:
            raise CliError(_i18n_exc("knowledge.evidence_add_input_required", "lesson evidence-add 必须提供 --input"))
        values.extend(["--input", str(_rooted(root, args.input))])
    elif command == "incident-list":
        values.extend(["--limit", str(args.limit), "--offset", str(args.offset)])
        if args.pattern_id:
            values.extend(["--pattern-id", args.pattern_id])
    elif command == "consume":
        if args.input is not None:
            values.extend(["--input", str(_rooted(root, args.input))])
        else:
            event_id = getattr(args, "event_id", None)
            if not event_id:
                raise CliError(_i18n_exc("knowledge.consume_param_required", "lesson consume 必须提供 --event-id", param="event-id"))
            subject_type = getattr(args, "subject_type", None)
            if not subject_type:
                raise CliError(_i18n_exc("knowledge.consume_param_required", "lesson consume 必须提供 --subject-type", param="subject-type"))
            subject_id = getattr(args, "subject_id", None)
            if not subject_id:
                raise CliError(_i18n_exc("knowledge.consume_param_required", "lesson consume 必须提供 --subject-id", param="subject-id"))
            event_type = getattr(args, "event_type", None)
            if not event_type:
                raise CliError(_i18n_exc("knowledge.consume_param_required", "lesson consume 必须提供 --event-type", param="event-type"))
            values.extend([
                "--event-id", event_id,
                "--subject-type", subject_type,
                "--subject-id", subject_id,
                "--event-type", event_type,
            ])
            lifecycle_id = getattr(args, "lifecycle_id", None)
            if lifecycle_id:
                values.extend(["--lifecycle-id", lifecycle_id])
            run_id = getattr(args, "run_id", None)
            if run_id:
                values.extend(["--run-id", run_id])
    elif command == "tool-exp-import":
        if args.input is None:
            raise CliError(_i18n_exc("knowledge.tool_exp_import_input_required", "lesson tool-exp-import 必须提供 --input"))
        values.extend(["--input", str(_rooted(root, args.input))])
    elif command == "tool-exp-export":
        pass
    elif command == "incident-activate":
        if not args.incident_id or not args.evidence_receipt_id:
            raise CliError(_i18n_exc("knowledge.incident_activate_params_required", "lesson incident-activate 必须提供 --incident-id 和 --evidence-receipt-id"))
        values.extend(["--incident-id", args.incident_id,
                       "--evidence-receipt-id", args.evidence_receipt_id])
    elif command == "promote-incident":
        if not args.incident_id or not args.evidence_receipt_id:
            raise CliError("lesson promote-incident 必须提供 --incident-id 和 --evidence-receipt-id")
        layout = _layout_module(root).require(root)
        values.extend(["--incident-id", args.incident_id,
                       "--evidence-receipt-id", args.evidence_receipt_id,
                       "--global-ledger", str(layout.global_knowledge_seed)])
    elif command == "promote-tool":
        if not args.tool or not args.category:
            raise CliError("lesson promote-tool 必须提供 --tool 和 --category")
        layout = _layout_module(root).require(root)
        values.extend(["--tool", args.tool[0], "--category", args.category,
                       "--global-ledger", str(layout.global_knowledge_seed)])
    elif command == "replay":
        if args.cases is None:
            raise CliError(_i18n_exc("knowledge.replay_cases_required", "lesson replay 必须提供 --cases"))
        values = ["--ledger", str(ledger_target)]
        values.extend(["--cases", str(_rooted(
            root, args.cases, allow_controlled_core=True,
        ))])
        values.extend(["--limit", str(args.limit), "--token-budget", str(args.token_budget or 1200),
                        "--byte-budget", str(args.byte_budget or 9000),
                        "--mode", args.mode,
                        "--holdout-mod", str(args.holdout_mod),
                        "--holdout-remainder", str(args.holdout_remainder)])
        return _run_script(root, "lesson_replay.py", values, timeout=300)
    elif command in {"record", "review"}:
        if args.input is None:
            raise CliError(_i18n_exc("knowledge.command_input_required", f"lesson {command} 必须提供 --input", command=command))
        values.extend(["--input", str(_rooted(root, args.input))])
    elif command == "feedback":
        lesson_id = getattr(args, "lesson_id", None)
        if not lesson_id:
            raise CliError(_i18n_exc("knowledge.feedback_param_required", "lesson feedback 必须提供 --lesson-id", param="lesson-id"))
        lifecycle_id = getattr(args, "lifecycle_id", None)
        if not lifecycle_id:
            raise CliError(_i18n_exc("knowledge.feedback_param_required", "lesson feedback 必须提供 --lifecycle-id", param="lifecycle-id"))
        finding_fingerprint = getattr(args, "finding_fingerprint", None)
        if not finding_fingerprint:
            raise CliError(_i18n_exc("knowledge.feedback_param_required", "lesson feedback 必须提供 --finding-fingerprint", param="finding-fingerprint"))
        outcome = getattr(args, "outcome", None)
        if not outcome:
            raise CliError(_i18n_exc("knowledge.feedback_param_required", "lesson feedback 必须提供 --outcome", param="outcome"))
        values.extend([
            "--lesson-id", lesson_id,
            "--lifecycle-id", lifecycle_id,
            "--finding-fingerprint", finding_fingerprint,
            "--outcome", outcome,
        ])
        if args.round is None:
            raise CliError(_i18n_exc("knowledge.feedback_round_required", "lesson feedback 必须提供 --round"))
        values.extend(["--round", str(args.round)])
        if args.finding is not None:
            values.extend(["--finding", str(_rooted(root, args.finding))])
        if args.signature:
            values.extend(["--signature", args.signature[0]])
    elif command == "hygiene":
        values.extend(["--min-feedback", str(args.min_feedback)])
    elif command == "rebuild-fts":
        pass
    elif command == "archive-events":
        if args.output is None:
            raise CliError(_i18n_exc("knowledge.archive_events_output_required", "lesson archive-events 必须提供 --output（归档目录）"))
        values.extend(["--output", str(_rooted(root, args.output)),
                       "--keep-tail", str(args.keep_tail)])
    elif command == "incident-archive":
        values.extend(["--older-than-days", str(args.older_than_days),
                       "--max-archive", str(args.max_archive)])
    elif command == "merge":
        source_pattern_id = getattr(args, "source_pattern_id", None)
        if not source_pattern_id:
            raise CliError(_i18n_exc("knowledge.merge_param_required", "lesson merge 必须提供 --source-pattern-id", param="source-pattern-id"))
        target_pattern_id = getattr(args, "target_pattern_id", None)
        if not target_pattern_id:
            raise CliError(_i18n_exc("knowledge.merge_param_required", "lesson merge 必须提供 --target-pattern-id", param="target-pattern-id"))
        reason = getattr(args, "reason", None)
        if not reason:
            raise CliError(_i18n_exc("knowledge.merge_param_required", "lesson merge 必须提供 --reason", param="reason"))
        values.extend([
            "--source-pattern-id", source_pattern_id,
            "--target-pattern-id", target_pattern_id,
            "--reason", reason,
        ])
    elif command == "retire":
        if not args.lesson_id or not args.reason:
            raise CliError(_i18n_exc("knowledge.retire_params_required", "lesson retire 必须提供 --lesson-id 和 --reason"))
        values.extend(["--lesson-id", args.lesson_id, "--reason", args.reason])
    elif command == "revoke":
        if not args.lesson_id or not args.reason:
            raise CliError(_i18n_exc("knowledge.revoke_params_required", "lesson revoke 必须提供 --lesson-id 和 --reason"))
        values.extend(["--lesson-id", args.lesson_id, "--reason", args.reason])
    elif command == "incident-revoke":
        if not args.incident_id or not args.reason:
            raise CliError(_i18n_exc("knowledge.incident_revoke_params_required", "lesson incident-revoke 必须提供 --incident-id 和 --reason"))
        values.extend(["--incident-id", args.incident_id, "--reason", args.reason])
    elif command == "tool-feedback":
        if args.input is not None:
            values.extend(["--input", str(_rooted(root, args.input))])
        else:
            category = getattr(args, "category", None)
            if not category:
                raise CliError(_i18n_exc("knowledge.tool_feedback_param_required", "lesson tool-feedback 必须提供 --category", param="category"))
            outcome = getattr(args, "outcome", None)
            if not outcome:
                raise CliError(_i18n_exc("knowledge.tool_feedback_param_required", "lesson tool-feedback 必须提供 --outcome", param="outcome"))
            finding_fingerprint = getattr(args, "finding_fingerprint", None)
            if not finding_fingerprint:
                raise CliError(_i18n_exc("knowledge.tool_feedback_param_required", "lesson tool-feedback 必须提供 --finding-fingerprint", param="finding-fingerprint"))
            values.extend([
                "--category", category,
                "--outcome", outcome,
                "--finding-fingerprint", finding_fingerprint,
            ])
            if not args.tool:
                raise CliError(_i18n_exc("knowledge.tool_feedback_tool_required", "lesson tool-feedback 必须提供 --tool（可重复）"))
            if not args.lifecycle_id or args.round is None:
                raise CliError(_i18n_exc("knowledge.tool_feedback_lifecycle_required", "lesson tool-feedback 必须提供 --lifecycle-id 和 --round"))
            for tool in args.tool:
                values.extend(["--tool", tool])
            values.extend(["--lifecycle-id", args.lifecycle_id,
                          "--round", str(args.round)])
            if args.finding is not None:
                values.extend(["--finding", str(_rooted(root, args.finding))])
    elif command == "tool-lookup":
        values.extend(["--query", args.query, "--limit", str(args.limit)])
        if args.category:
            values.extend(["--category", args.category])
        if args.token_budget is not None:
            values.extend(["--token-budget", str(args.token_budget)])
        if args.byte_budget is not None:
            values.extend(["--byte-budget", str(args.byte_budget)])
    elif command == "promote":
        if not args.pattern_id and not args.lesson_id:
            raise CliError(_i18n_exc("knowledge.promote_params_required", "lesson promote 必须提供 --pattern-id 或 --lesson-id"))
        if args.pattern_id:
            values.extend(["--pattern-id", args.pattern_id])
        else:
            values.extend(["--lesson-id", args.lesson_id])
        # DOC-004：全局错题集默认走布局层的 global_knowledge_seed
        layout = _layout_module(root).require(root)
        values.extend(["--global-ledger", str(layout.global_knowledge_seed)])
    elif command == "inherit":
        layout = _layout_module(root).require(root)
        values.extend(["--global-ledger", str(layout.global_knowledge_seed)])
    elif command in {"export", "backup", "export-book"}:
        if args.output is None:
            raise CliError(_i18n_exc("knowledge.command_output_required", f"lesson {command} 必须提供 --output", command=command))
        values.extend(["--output", str(_rooted(root, args.output))])
    if args.json:
        values.append("--json")
    return _run_script(root, "lesson_ledger.py", values, timeout=300)


def command_logic_analysis(args: argparse.Namespace) -> int:
    """Validate or bind the auditable intent/logic/evidence contract."""
    root = args.root.resolve()
    if args.input is None:
        raise CliError(_i18n_exc("knowledge.logic_input_required", f"logic-analysis {args.logic_command} 必须提供 --input", command=args.logic_command))
    values = [args.logic_command, "--input", str(_rooted(root, args.input))]
    if args.logic_command == "bind":
        if args.task is None:
            raise CliError(_i18n_exc("knowledge.logic_bind_task_required", "logic-analysis bind 必须提供 --task"))
        values.extend(["--task", str(_rooted(root, args.task))])
    elif args.logic_command == "bind-finding":
        if args.finding is None:
            raise CliError(_i18n_exc("knowledge.logic_bind_finding_required", "logic-analysis bind-finding 必须提供 --finding"))
        values.extend(["--finding", str(_rooted(root, args.finding))])
    if args.output is not None:
        values.extend(["--output", str(_rooted(root, args.output))])
    return _run_script(root, "logic_analysis.py", values, timeout=300)


def _cloud_summary_has_auth_failure(summary: object) -> bool:
    """Detect a server-side auth rejection even when a stale token exists."""
    try:
        text = json.dumps(summary, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        text = str(summary).lower()
    return any(marker in text for marker in (
        "401", "unauthorized", "unauthenticated", "token_expired", "expired_token",
        "登录态已过期", "凭据过期", "会话已过期",
    ))


def _cloud_summary_is_recoverable_calibration(summary: object) -> bool:
    try:
        text = json.dumps(summary, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        text = str(summary).lower()
    return any(marker in text for marker in (
        "sync_resync_required", "sync_version_conflict", "cursor",
        "stale_version", "scope_state_missing", "服务端 scope 状态缺失",
    ))


def _cloud_device_label(get_device_id=None) -> str:
    """Resolve the display label while keeping older client shims usable."""
    try:
        from core.cloud_sync.cloud_client import get_device_label
        return get_device_label()
    except Exception:
        device_id = get_device_id() if callable(get_device_id) else ""
        return str(device_id)[:8] or "bugx-cli"


def _cloud_login_and_register(client, get_device_id, *, reason: str = "") -> None:
    """Open device authorization and require both login and device registration."""
    if reason:
        print(f"[cloud] {reason}，正在打开浏览器授权页…", flush=True)
    label = _cloud_device_label(get_device_id)
    login_result = client.login(name=label)
    login_ok = getattr(login_result, "ok", None)
    if login_ok is not True:
        raise RuntimeError(getattr(login_result, "error", None) or "授权未完成")
    device_result = client.register_device(name=label)
    device_ok = getattr(device_result, "ok", None)
    if device_ok is not True:
        raise RuntimeError(
            getattr(device_result, "error", None) or
            f"设备登记失败 code={getattr(device_result, 'status', '')}"
        )


def _cloud_register_existing_device(client, get_device_id=None) -> None:
    """Refresh device registration even when credentials predate this device."""
    device_result = client.register_device(name=_cloud_device_label(get_device_id))
    if getattr(device_result, "ok", None) is not True:
        raise RuntimeError(
            getattr(device_result, "error", None) or
            f"设备登记失败 code={getattr(device_result, 'status', '')}"
        )


def _cloud_project_slug(root: Path, requested: str | None = None) -> str:
    """Resolve the display slug without deriving identity from the path."""
    if requested:
        return requested
    binding_path = root / ".bugx" / "cloud-project.json"
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        if isinstance(binding, dict) and binding.get("slug"):
            return str(binding["slug"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    from core.cloud_sync.cloud_onboard import _load_committed_slug
    committed = _load_committed_slug(root)
    if committed:
        return committed
    return root.name


def _cloud_sync_command(args: argparse.Namespace, client: Any, root: Path,
                        get_device_id, *, default_scope: str = "all") -> int:
    """执行一次确定性的云端同步，不把同步流程交给宿主 AI 编排。

    ``cloud sync`` 和 ``cloud onboard on`` 共用这条路径，保证登录、设备登记、
    401 重授权、项目绑定、scope 收敛以及 onboarding 状态落地完全一致。显式
    同步默认覆盖 global + project；调用方仍可用 ``--scope`` 缩小范围。
    """
    from .agent import _CliProgress

    progress = _CliProgress(args)
    progress.update(0, "准备云端同步")
    scope = getattr(args, "sync_scope", None) or default_scope
    onboard_project = scope in {"project", "all"}
    onboarding = None
    if onboard_project:
        try:
            from core.cloud_sync.cloud_onboard import get_state, set_state
            onboarding = (get_state, set_state)
            if get_state(root) in {"dismissed", "local"}:
                set_state(root, "pending")
                print("[cloud] 显式同步已重新开启本项目 onboarding")
        except Exception as exc:  # noqa: BLE001
            progress.fail("onboarding 状态读取失败")
            print(f"[cloud] onboarding 状态读取失败，同步已取消: {exc}")
            return 1
    if not client.cred.is_authenticated():
        print("[cloud] 当前未登录云端，先完成登录，再继续同步。")
        try:
            _cloud_login_and_register(client, get_device_id)
        except RuntimeError as exc:
            progress.fail("云端登录失败")
            print(f"[cloud] 登录失败，同步已取消: {exc}")
            if onboarding is not None:
                _, set_state = onboarding
                set_state(root, "pending")
            return 1
    else:
        try:
            _cloud_register_existing_device(client, get_device_id)
        except RuntimeError as exc:
            if not _cloud_summary_has_auth_failure({"error": str(exc)}):
                progress.fail("设备登记失败")
                print(f"[cloud] 设备登记失败，同步已取消: {exc}")
                return 1
            # 当前 token 可能只是本地看起来有效；设备登记是第一次真实
            # 鉴权请求。401 必须进入同一套浏览器授权流程，不能直接跳过。
            try:
                _cloud_login_and_register(
                    client, get_device_id, reason="设备登记被服务端拒绝",
                )
            except RuntimeError as login_exc:
                progress.fail("重新授权失败")
                print(f"[cloud] 授权未完成，同步已取消: {login_exc}")
                if onboarding is not None:
                    _, set_state = onboarding
                    set_state(root, "pending")
                return 1
    progress.update(35, "认证和设备登记完成")
    try:
        from core.cloud_sync.cloud_onboard import sync_layers
        scopes = {"global", "project"} if scope == "all" else {scope}
        progress.update(50, "正在同步云端数据")
        summary = sync_layers(
            client, root, args.project_slug, scopes=scopes,
            detail_mode=getattr(args, "detail_mode", None),
        )
        if not summary.get("ok") and _cloud_summary_has_auth_failure(summary):
            # 凭据文件存在不代表 token 仍被服务端接受；401 是重新授权的权威信号。
            try:
                _cloud_login_and_register(
                    client, get_device_id,
                    reason="服务端拒绝当前登录态",
                )
            except RuntimeError as exc:
                progress.fail("重新授权失败")
                print(f"[cloud] 授权未完成，同步已取消: {exc}")
                if onboarding is not None:
                    _, set_state = onboarding
                    set_state(root, "pending")
                return 1
            summary = sync_layers(
                client, root, args.project_slug, scopes=scopes,
                detail_mode=getattr(args, "detail_mode", None),
            )
        if not summary.get("ok") and not _cloud_summary_has_auth_failure(summary) and (
            getattr(args, "calibrate", False) or _cloud_summary_is_recoverable_calibration(summary)
        ):
            progress.update(60, "检测到基线失步，正在执行自动校准自愈")
            print("[cloud] 检测到基线状态失步或游标失效，自动启动基线自愈校准…")
            try:
                from core.cloud_sync.cloud_onboard import get_account_id, get_imports, resolve_store_path, load_project_binding
                ns = get_imports()
                account_id = get_account_id(client)
                pid = ""
                try:
                    pid = load_project_binding(root)
                except Exception:
                    pass
                for s in scopes:
                    s_pid = pid if s == "project" else ""
                    try:
                        s_store = ns["SyncStore"](resolve_store_path(ns, root, s))
                        try:
                            s_store.reset_scope(account_id, s, s_pid, error_code="AUTO_CALIBRATION")
                            s_store.prune_converged_conflicts(account_id, s, s_pid, resolver_identity="auto-calibration")
                        finally:
                            s_store.close()
                    except Exception:
                        pass
                summary = sync_layers(
                    client, root, args.project_slug, scopes=scopes,
                    detail_mode=getattr(args, "detail_mode", None),
                )
                if summary.get("ok") is True:
                    print("[cloud] 自动校准成功，两端基线已完成对齐收敛！")
            except Exception as cal_exc:  # noqa: BLE001
                print(f"[cloud] 自动校准重试未完成: {cal_exc}")
        progress.update(90, "云端同步结果已收到")
        if summary.get("ok") is True:
            progress.finish("云端同步完成")
        else:
            progress.fail("云端同步未完成")
        for key, value in summary.items():
            print(f"  [cloud] {key}: {value}")
        if onboarding is not None:
            _, set_state = onboarding
            set_state(root, "done" if summary.get("ok") is True else "pending")
        return 0 if summary.get("ok") is True else 1
    except Exception as exc:  # noqa: BLE001
        progress.fail("云端同步失败")
        print(f"[cloud] 同步失败: {exc}")
        if onboarding is not None:
            try:
                _, set_state = onboarding
                set_state(root, "pending")
            except Exception as state_exc:  # noqa: BLE001
                print(f"[cloud] onboarding 状态未恢复: {state_exc}")
        return 1



def command_knowledge(args: argparse.Namespace) -> int:
    """执行显式的项目候选导出、审核、全局导入和回滚。"""
    root = args.root.resolve()
    layout = _layout_module(root).require(root)
    command = args.knowledge_command
    values = [command, "--project-ledger", str(layout.knowledge_seed),
              "--global-ledger", str(layout.global_knowledge_seed),
              "--global-root", str(layout.global_knowledge_root)]
    if command == "export":
        if args.output is None:
            raise CliError(_i18n_exc("knowledge.export_output_required", "knowledge export 必须提供 --output"))
        values.extend(["--output", str(_rooted(root, args.output)),
                       "--contributor", args.contributor])
    elif command == "approve":
        if args.bundle is None or args.output is None or not args.reviewer or not args.reason:
            raise CliError(_i18n_exc("knowledge.approve_params_required", "knowledge approve 必须提供 --bundle、--output、--reviewer、--reason"))
        values.extend(["--bundle", str(_rooted(root, args.bundle)),
                       "--output", str(_rooted(root, args.output)),
                       "--reviewer", args.reviewer, "--reason", args.reason])
    elif command == "import":
        if args.bundle is None or not args.reviewer:
            raise CliError(_i18n_exc("knowledge.import_params_required", "knowledge import 必须提供 --bundle 和 --reviewer"))
        values.extend(["--bundle", str(_rooted(root, args.bundle)), "--reviewer", args.reviewer])
    elif command == "rollback":
        if not args.bundle_id or not args.reviewer:
            raise CliError(_i18n_exc("knowledge.rollback_params_required", "knowledge rollback 必须提供 --bundle-id 和 --reviewer"))
        values.extend(["--bundle-id", args.bundle_id, "--reviewer", args.reviewer])
    return _run_script(root, "knowledge_sync.py", values)
