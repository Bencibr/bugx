#!/usr/bin/env python3
"""launch_phases/report_sharing.py - 测试报告分享、云端同步收据与断点重放 (DOC-069)。"""

from __future__ import annotations
from typing import Any

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import core.secure_atomic as secure_atomic
from core.launch_phases.common import (
    REPORT_POLICY_CHOICES,
    resolve_project_root,
    uses_project_layout,
    bugx_paths,
    pollings_root,
)
from core.launch_phases.finding_settlement import lifecycle_root_for
from core.launch_phases.session_runtime import iso_time, load_runtime


def normalize_report_policy(raw: object) -> str | None:
    """归一报告提交策略（DOC-236 C6）；非法值 **fail-closed**（抛 ValueError）。

    空值/None 表示"未声明"，由调用方取默认值。
    """
    if raw is None:
        return None
    value = str(raw).strip().casefold()
    if not value:
        return None
    if value not in REPORT_POLICY_CHOICES:
        raise ValueError(
            f"report_policy 只能是 {'/'.join(REPORT_POLICY_CHOICES)}，收到 {raw!r}")
    return value


def _runtime_report_policy(runtime: dict) -> str:
    """读取并强校验 L1 持久化的报告提交策略。

    L5 没有兼容旧 ``share_policy`` 或 ``mcp-auto`` 的默认路径：缺失、损坏
    或非法策略都必须在任何飞轮/导入/提交副作用前硬阻断。
    """
    try:
        if "report_policy" in runtime:
            raw = runtime.get("report_policy")
        else:
            raw = load_runtime().get("report_policy")
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取已持久化的 report_policy：{exc}") from exc
    try:
        policy = normalize_report_policy(raw)
    except ValueError as exc:
        raise RuntimeError(f"已持久化的 report_policy 非法：{exc}") from exc
    if policy is None:
        raise RuntimeError("运行态缺少已持久化的 report_policy；拒绝进入共享兼容路径")
    return policy


def _compute_report_hash(lifecycle_root: Path) -> str | None:
    """计算 report.md 的 sha256，用于 operation 绑定与重放校验。"""
    report = lifecycle_root / "report.md"
    if report.is_file() and not report.is_symlink():
        try:
            return hashlib.sha256(report.read_bytes()).hexdigest()
        except OSError:
            return None
    return None


def _write_share_receipt(runtime: dict, *, operation_id: str, report_hash: str | None,
                         visibility: str = "private", result: dict | None = None) -> None:
    """成功分享后写入可查询 receipt，不修改已封存报告。"""
    if uses_project_layout():
        root = bugx_paths.require(resolve_project_root()).lifecycle_root / "sync"
    else:
        root = resolve_project_root() / ".bugx" / "sync"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "share_receipt.json"
    if path.is_symlink():
        raise OSError(f"拒绝写入符号链接回执: {path}")
    receipt = {
        "schema_version": 1,
        "share_operation_id": operation_id,
        "session_id": runtime.get("session_id"),
        "round_id": runtime.get("round_id") or runtime.get("lifecycle_id"),
        "report": runtime.get("lifecycle_last_report"),
        "report_hash": report_hash,
        "visibility": visibility,
        "shared_at": iso_time(time.time()),
        "result": result,
    }
    secure_atomic.atomic_write_json(path, receipt, mode=0o600, parent_mode=0o700)


def _write_share_pending(runtime: dict, *, reason: str, visibility: str = "private",
                         operation_id: str | None = None, report_hash: str | None = None) -> str:
    """保存最终 Round 共享失败的可重放记录，不修改已封存报告。返回 operation_id。"""
    if report_hash is None:
        try:
            report_hash = _compute_report_hash(lifecycle_root_for(runtime))
        except (OSError, ValueError, RuntimeError):
            pass
    if uses_project_layout():
        root = bugx_paths.require(resolve_project_root()).lifecycle_root / "sync"
        if report_hash is None:
            try:
                report_hash = _compute_report_hash(bugx_paths.require(resolve_project_root()).lifecycle_root)
            except (OSError, ValueError):
                pass
    else:
        root = resolve_project_root() / ".bugx" / "sync"
        if report_hash is None:
            report_hash = _compute_report_hash(root.parent)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "share_pending.json"
    if path.is_symlink():
        raise OSError(f"拒绝写入符号链接共享队列: {path}")
    op_id = operation_id or ("share-" + uuid.uuid4().hex)
    operation = {
        "schema_version": 1,
        "share_operation_id": op_id,
        "session_id": runtime.get("session_id"),
        "round_id": runtime.get("round_id") or runtime.get("lifecycle_id"),
        "report": runtime.get("lifecycle_last_report"),
        "report_hash": report_hash,
        "visibility": visibility,
        "reason": reason,
        "created_at": iso_time(time.time()),
        "state": "pending",
    }
    secure_atomic.atomic_write_json(path, operation, mode=0o600, parent_mode=0o700)
    return op_id


def _share_pending_files() -> list[Path]:
    """Find pending Round shares across sessions without following links."""
    base = resolve_project_root() / ".bugx"
    found: list[Path] = []
    direct = base / "sync" / "share_pending.json"
    if direct.is_file() and not direct.is_symlink():
        found.append(direct)
    pollings = pollings_root()
    if not pollings.is_dir() or pollings.is_symlink():
        return found
    try:
        for polling in pollings.iterdir():
            if polling.is_symlink() or not polling.is_dir():
                continue
            lifecycles = polling / "lifecycles"
            if lifecycles.is_symlink() or not lifecycles.is_dir():
                continue
            for lifecycle in lifecycles.iterdir():
                if lifecycle.is_symlink() or not lifecycle.is_dir():
                    continue
                pending = lifecycle / "sync" / "share_pending.json"
                if pending.is_file() and not pending.is_symlink():
                    found.append(pending)
    except OSError:
        return found
    return sorted(set(found), key=str)


def replay_share_pending(*, force: bool = False) -> None:
    """Replay authorized Round shares before cloud knowledge Bootstrap.

    ``submit`` 是自动重放授权；``local`` 的 share_pending 是**事后**通道——
    旧实现对 ``local`` 硬短路，导致队列永不补偿（2026-11-09 复盘 4.3-2）。
    现在 ``--replay-share-pending`` 显式授权（``force=True``）即可在 ``local``
    下重放；``none`` 仍是硬禁止（本轮不参与共享），只有显式把策略改为
    ``local``/``submit`` 才能清账。
    """
    report_policy = _runtime_report_policy({})
    if report_policy == "none":
        print("  （report_policy=none：跳过待共享结论重放，保留 share_pending）")
        return
    if report_policy != "submit" and not force:
        print(
            f"  （report_policy={report_policy}：跳过待共享结论重放，保留 share_pending；"
            "如需补偿请加 --replay-share-pending）"
        )
        return
    if not uses_project_layout():
        return
    try:
        from core.report_engine.report_share import share_run
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 待共享结论组件不可用，保留 share_pending：{exc}")
        return
    for pending in _share_pending_files():
        try:
            if pending.is_symlink():
                continue
            payload = json.loads(pending.read_text(encoding="utf-8"))
            operation_id = payload.get("share_operation_id")
            if not isinstance(operation_id, str) or not operation_id.strip():
                print(f"  ⚠ share_pending 缺少 operation_id，保留：{pending}")
                continue
            if payload.get("visibility") not in {None, "private"}:
                print(f"  ⚠ share_pending 含禁用的公开可见性，保留：{pending}")
                continue
            lifecycle_root = pending.parent.parent
            if not (lifecycle_root / "report.md").is_file():
                print(f"  ⚠ 待共享 Round 报告缺失，保留：{pending}")
                continue
            current_hash = _compute_report_hash(lifecycle_root)
            pending_hash = payload.get("report_hash")
            if isinstance(pending_hash, str) and pending_hash and current_hash and pending_hash != current_hash:
                print(f"  ⚠ share_pending report_hash 不一致（pending={pending_hash[:8]} current={current_hash[:8]}），保留待人工核验：{pending}")
                continue
            result = share_run(
                lifecycle_root,
                resolve_project_root(),
                polling_id=pending.parents[3].name,
                lifecycle_id=pending.parents[1].name,
                visibility="private",
                operation_id=operation_id,
            )
            if not result.get("ok"):
                print(f"  ⚠ share_pending 重放失败，保留待下次启动：{result.get('error')}")
                continue
            receipt_written = False
            try:
                receipt_path = lifecycle_root / "sync" / "share_receipt.json"
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                if not receipt_path.is_symlink() and not receipt_path.parent.is_symlink():
                    receipt = {
                        "schema_version": 1,
                        "share_operation_id": operation_id,
                        "report_hash": current_hash or pending_hash,
                        "visibility": "private",
                        "replayed_at": iso_time(time.time()),
                        "result": result,
                    }
                    secure_atomic.atomic_write_json(
                        receipt_path, receipt, mode=0o600, parent_mode=0o700,
                    )
                    receipt_written = True
            except (OSError, ValueError) as exc:
                print(f"  ⚠ 共享成功但 receipt 写入失败，保留 share_pending：{exc}")
            if not receipt_written:
                continue
            if pending.is_symlink():
                print(f"  ⚠ share_pending 在重放后变为符号链接，拒绝删除：{pending}")
                continue
            pending.unlink()
            print(f"  ✓ 已重放待共享 Round 结论（operation_id={operation_id} report_hash={current_hash or pending_hash or 'unknown'}）")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"  ⚠ share_pending 无法重放，保留待下次启动：{exc}")


def _maybe_enqueue_flywheel(runtime: dict, lifecycle: dict) -> None:
    """DOC-126 C126-1: 审计收尾时自动将脱敏攻防拓扑与 DPO 偏好对注入飞轮 Outbox。"""
    try:
        from core.cloud_sync.flywheel_client import FlywheelUploader
        uploader = FlywheelUploader()
        if not getattr(uploader, "data_sharing", False):
            return

        static_topology: list[dict[str, Any]] = []
        dpo_pair: dict[str, Any] | None = None
        try:
            from core.autonomous_cve import attack_graph_store
            from core.autonomous_cve.attack_graph import ReflexiveBacktrackingEngine
            root = resolve_project_root()
            graph = attack_graph_store.load_graph(root)
            if graph:
                static_topology = [
                    {"id": r_id, "type": "route", "path": node.get("path")}
                    for r_id, node in graph.routes.items()
                ]
                engine = ReflexiveBacktrackingEngine(graph)
                for hypo_id in graph.hypotheses:
                    dpo = engine.build_dpo_pair(hypo_id)
                    if dpo:
                        dpo_pair = dpo
                        break
        except Exception:
            pass

        if not static_topology:
            routes = lifecycle.get("routes") or []
            static_topology = [{"route": r} if isinstance(r, str) else r for r in routes]

        round_no = runtime.get("round_no") or lifecycle.get("lifecycle_no")
        exploit_delta = {
            "round_no": round_no,
            "polling_id": runtime.get("polling_id"),
            "findings_count": len(lifecycle.get("findings") or []),
            "proof_hash": lifecycle.get("report_hash") or lifecycle.get("proof_hash"),
        }
        proof_hash = lifecycle.get("report_hash") or lifecycle.get("proof_hash")
        quality = "tier-1" if proof_hash else "tier-2"
        metrics = {"replay_proof_hash": proof_hash} if proof_hash else {}

        uploader.enqueue_graph(
            static_topology=static_topology,
            exploit_delta=exploit_delta,
            quality=quality,
            dpo_pair=dpo_pair,
            metrics=metrics,
        )
        try:
            uploader.flush(limit=20, batch_size=20)
        except Exception:
            pass
    except Exception:  # noqa: BLE001 - 飞轮入队失败降级，不阻断主链路
        pass


def maybe_share_lifecycle(runtime: dict, lifecycle: dict, share: str | None) -> str:
    """按已持久化 ``report_policy`` 处理封存报告，不再通过旧 flag 交互。

    DOC-236 C6：若 L1 已采集并持久化 `report_policy`，则**按它执行且不再交互**
    （无人值守闭环）：`none` 连飞轮与待重放都不参与；`local` 不提交但保留事后通道；
    `submit` 直接无交互提交（私有）。
    """
    report_policy = _runtime_report_policy(runtime)
    if share is not None:
        raise RuntimeError(
            "旧的 share/--no-share 参数已移除；请在 L1 使用 report_policy=none|local|submit"
        )
    if report_policy == "none":
        print("  （report_policy=none：本轮不参与结论共享与知识飞轮）")
        return "completed"
    _maybe_enqueue_flywheel(runtime, lifecycle)

    if report_policy == "local":
        lifecycle_root = lifecycle_root_for(runtime)
        report_hash = _compute_report_hash(lifecycle_root)
        operation_id = "share-" + uuid.uuid4().hex
        _write_share_pending(
            runtime,
            reason="local_policy",
            visibility="private",
            operation_id=operation_id,
            report_hash=report_hash,
        )
        print(f"  （report_policy=local：仅保留本地报告，已写入待重放队列 operation_id={operation_id}）")
        return "pending"

    try:
        from core.report_engine.report_share import share_run
    except Exception as exc:  # noqa: BLE001
        print(f"  （分享组件加载失败，跳过: {exc}）")
        _write_share_pending(
            runtime,
            reason=f"component:{type(exc).__name__}",
            visibility="private",
            operation_id="share-" + uuid.uuid4().hex,
            report_hash=_compute_report_hash(lifecycle_root_for(runtime)),
        )
        return "pending"

    # `report_policy=submit` is the only remaining execution branch.  The
    # legacy `share` argument was rejected above: submission is private and
    # non-interactive by contract.
    visibility = "private"
    lifecycle_root = lifecycle_root_for(runtime)
    report_hash = _compute_report_hash(lifecycle_root)
    operation_id = "share-" + uuid.uuid4().hex
    try:
        result = share_run(
            lifecycle_root, resolve_project_root(),
            polling_id=str(runtime.get("polling_id") or "polling-000001"),
            lifecycle_id=str(runtime.get("lifecycle_id") or ""),
            seed=str(runtime.get("lifecycle_no") or ""),
            visibility=visibility,
            operation_id=operation_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  （分享失败: {exc}）")
        _write_share_pending(
            runtime,
            reason=f"exception:{type(exc).__name__}",
            visibility=visibility,
            operation_id=operation_id,
            report_hash=report_hash,
        )
        return "pending"

    if isinstance(result, dict) and result.get("ok") and isinstance(result.get("summary"), dict):
        print(f"  ✓ 已匿名分享运行汇总（run_id={result['summary'].get('run_id')}"
              f"，visibility={visibility}，operation_id={operation_id}，report_hash={report_hash or 'unknown'}）")
        receipt_ok = True
        try:
            _write_share_receipt(runtime, operation_id=operation_id, report_hash=report_hash,
                                 visibility=visibility, result=result)
        except (OSError, ValueError, RuntimeError) as exc:
            receipt_ok = False
            print(f"  （共享已提交但回执写入失败，保留待重放：{exc}）")
            _write_share_pending(
                runtime, reason=f"receipt:{type(exc).__name__}", visibility=visibility,
                operation_id=operation_id, report_hash=report_hash,
            )
        return "completed" if receipt_ok else "pending"

    if isinstance(result, dict):
        reason = str(result.get("error") or result.get("status") or "share_failed")
    else:
        reason = "invalid_share_result"
    print(f"  ✗ 分享失败: {reason}")
    _write_share_pending(
        runtime,
        reason=reason,
        visibility=visibility,
        operation_id=operation_id,
        report_hash=report_hash,
    )
    return "pending"
