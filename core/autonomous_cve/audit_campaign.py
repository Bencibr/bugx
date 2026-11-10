#!/usr/bin/env python3
"""维护多维审计覆盖、风险优先级和可恢复的最小单元任务队列。"""
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
import sys
from pathlib import Path


import argparse
import copy
import contextlib
import datetime as dt
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid

from core.runtime import project_graph
from core.retrieval import symbol_index

_CAMPAIGN_LOCK_THREAD = threading.RLock()
_CAMPAIGN_LOCK_STATE = threading.local()

SCHEMA_VERSION = 2
EXECUTION_STATES = {"not_run", "pass", "fail", "blocked", "not_applicable"}
EVIDENCE_KINDS = {"source", "test", "command", "finding"}
EVIDENCE_RESULTS = {"pass", "fail", "blocked", "not_applicable"}
EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ASSET_LOCATOR_RE = re.compile(r"^(?:runtime:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}|env:BUGX_[A-Za-z0-9_]{1,127})$")
ASSET_KINDS = {"account", "session", "token", "cookie", "fixture", "identifier", "header", "other"}
ASSET_SENSITIVITIES = {"public", "internal", "secret"}
ASSET_SOURCES = {"unit", "external"}
ASSET_FORBIDDEN_FIELDS = {
    "value", "password", "passwd", "token_value", "secret", "cookie_value",
    "authorization", "api_key", "access_token", "refresh_token",
}
DEFAULT_LEASE_SECONDS = 300.0
MAX_LEASE_SECONDS = 24 * 60 * 60


class CampaignError(ValueError):
    """审计任务状态协议错误。"""


def _atomic_write(path: Path, payload: dict) -> None:
    if not path.parent.is_dir():
        raise CampaignError(f"输出目录不存在: {path.parent}")
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            parent_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            # Directory fsync is unavailable on some supported platforms.
            pass
        else:
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            finally:
                os.close(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _epoch() -> float:
    return time.time()


def _finite_epoch(value: object) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    except (OverflowError, ValueError):
        return False


@contextlib.contextmanager
def _campaign_lock(path: Path, timeout: float = 10.0):
    """Serialize campaign read-modify-write operations without changing the state path.

    在 ``.audit-campaign.json.lock`` 上使用 ``fcntl.flock``（POSIX）或
    ``msvcrt.locking``（Windows）实现跨进程互斥；缺一不可时显式阻断而非
    静默无锁。进程内再用线程锁避免同进程重入竞争。
    """
    if not path.parent.is_dir():
        raise CampaignError(f"输出目录不存在: {path.parent}")
    lock_path = path.with_name(f".{path.name}.lock")
    # 符号链接硬化：拒绝直接劫持的锁文件；父目录本身若为符号链接也视为劫持。
    # 不逐层检查 /var 等系统级符号链接（/var -> private/var），避免在 macOS
    # tempfile（/var/folders）下误报；与 bugx_paths 的 resolved-root 策略保持一致。
    if lock_path.is_symlink():
        raise CampaignError(f"拒绝使用符号链接锁文件: {lock_path}")
    if lock_path.parent.is_symlink():
        raise CampaignError(f"拒绝使用符号链接锁目录: {lock_path.parent}")
    lock_key = str(lock_path.resolve(strict=False))
    held_counts = getattr(_CAMPAIGN_LOCK_STATE, "counts", None)
    if held_counts is None:
        held_counts = {}
        _CAMPAIGN_LOCK_STATE.counts = held_counts
    depth = held_counts.get(lock_key, 0)
    if depth > 0:
        held_counts[lock_key] = depth + 1
        try:
            yield
        finally:
            held_counts[lock_key] = depth
        return
    with _CAMPAIGN_LOCK_THREAD:
        # O_NOFOLLOW closes the check-then-open symlink race on POSIX.  Keep
        # the explicit checks above for platforms without this flag.
        open_flags = os.O_RDWR | os.O_CREAT
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, open_flags | nofollow, 0o600)
        except OSError as exc:
            raise CampaignError(f"无法安全打开 campaign 锁: {lock_path}") from exc
        handle = os.fdopen(descriptor, "a+")
        _fcntl = None
        _msvcrt = None
        try:
            try:
                import fcntl as _fcntl_mod  # type: ignore[import-not-found]
                _fcntl = _fcntl_mod
            except ImportError:  # pragma: no cover - Windows fallback
                _fcntl = None
            try:
                import msvcrt as _msvcrt_mod  # type: ignore[import-not-found]
                _msvcrt = _msvcrt_mod
            except ImportError:  # pragma: no cover - POSIX fallback
                _msvcrt = None
            if _fcntl is not None:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise CampaignError("campaign 锁等待超时")
                        time.sleep(0.01)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise CampaignError("campaign 锁等待超时")
                        time.sleep(0.01)
            else:  # pragma: no cover - 无任何可用锁原语
                raise CampaignError("campaign 锁在当前平台不可用（fcntl 和 msvcrt 均缺失），拒绝并发不安全写")
            held_counts[lock_key] = 1
            try:
                yield
            finally:
                held_counts.pop(lock_key, None)
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


def _repo_relative_path(value: object, field: str, root: Path | None) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CampaignError(f"{field}.path 必须是非空仓库内相对路径")
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CampaignError(f"{field}.path 必须是仓库内相对路径")
    if root is not None:
        try:
            (root / candidate).resolve(strict=False).relative_to(root.resolve())
        except ValueError as exc:
            raise CampaignError(f"{field}.path 越过仓库根目录") from exc
    return candidate.as_posix()


def _validate_evidence(item: object, field: str, root: Path | None) -> dict:
    if not isinstance(item, dict):
        raise CampaignError(f"{field} 必须是结构化对象，不能是任意字符串")
    kind = item.get("kind", item.get("type"))
    if not isinstance(kind, str) or kind not in EVIDENCE_KINDS:
        raise CampaignError(f"{field}.kind 必须是 {sorted(EVIDENCE_KINDS)} 之一")
    common_fields = {"kind", "type"}
    kind_fields = {
        "source": {"path", "line", "column"},
        "test": {
            "command", "result", "exit_code", "artifact_path", "artifact_sha256",
            "target_fingerprint", "observed_at", "worker_id", "status", "timestamp",
            "run_id", "round_no", "lease_id",
        },
        "command": {
            "command", "result", "exit_code", "artifact_path", "artifact_sha256",
            "target_fingerprint", "observed_at", "worker_id", "status", "timestamp",
            "run_id", "round_no", "lease_id",
        },
        "finding": {"id", "finding_id", "path", "line", "column"},
    }[kind]
    allowed = common_fields | kind_fields
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise CampaignError(f"{field} 含未知字段: {', '.join(unknown)}")
    if any(isinstance(value, (dict, list, tuple, set))
           for key, value in item.items() if key in allowed):
        raise CampaignError(f"{field} 字段值不能是嵌套对象或数组")
    normalized = {
        key: value for key, value in item.items()
        if key in allowed and key != "type"
    }
    normalized["kind"] = kind
    if kind == "source":
        normalized["path"] = _repo_relative_path(item.get("path"), field, root)
        line = item.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise CampaignError(f"{field}.line 必须是正整数")
        if "column" in item and (
            not isinstance(item["column"], int) or isinstance(item["column"], bool)
            or item["column"] < 1
        ):
            raise CampaignError(f"{field}.column 必须是正整数")
    elif kind in {"test", "command"}:
        command = item.get("command")
        result = item.get("result")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise CampaignError(f"{field}.command 必须是非空字符串")
        if not isinstance(result, str) or result not in EVIDENCE_RESULTS:
            raise CampaignError(f"{field}.result 必须是 {sorted(EVIDENCE_RESULTS)} 之一")
    else:
        finding_id = item.get("id", item.get("finding_id"))
        if not isinstance(finding_id, str) or not EVIDENCE_ID_RE.fullmatch(finding_id):
            raise CampaignError(f"{field}.id 必须是有效 finding ID")
        normalized.pop("finding_id", None)
        normalized["id"] = finding_id
    return normalized


def _validate_evidence_list(value: object, field: str, root: Path | None) -> list[dict]:
    if not isinstance(value, list):
        raise CampaignError(f"{field} 必须是数组")
    return [_validate_evidence(item, f"{field}[{index}]", root)
            for index, item in enumerate(value)]


def _validate_asset(item: object, field: str, *, source: str) -> dict:
    """Validate an opaque test-asset reference; never accept its secret value."""
    if not isinstance(item, dict):
        raise CampaignError(f"{field} 必须是结构化对象")
    forbidden = sorted(set(item) & ASSET_FORBIDDEN_FIELDS)
    if forbidden:
        raise CampaignError(f"{field} 含禁止的秘密字段: {', '.join(forbidden)}")
    allowed = {
        "asset_id", "kind", "source", "locator", "sensitivity",
        "expires_at", "revoked",
    }
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise CampaignError(f"{field} 含未知字段: {', '.join(unknown)}")
    asset_id = item.get("asset_id")
    if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
        raise CampaignError(f"{field}.asset_id 必须是有效资产 ID")
    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in ASSET_KINDS:
        raise CampaignError(f"{field}.kind 必须是 {sorted(ASSET_KINDS)} 之一")
    sensitivity = item.get("sensitivity", "internal")
    if not isinstance(sensitivity, str) or sensitivity not in ASSET_SENSITIVITIES:
        raise CampaignError(
            f"{field}.sensitivity 必须是 {sorted(ASSET_SENSITIVITIES)} 之一"
        )
    normalized = {
        "asset_id": asset_id,
        "kind": kind,
        "sensitivity": sensitivity,
    }
    if source == "consumed":
        asset_source = item.get("source")
        if not isinstance(asset_source, str) or asset_source not in ASSET_SOURCES:
            raise CampaignError(
                f"{field}.source 必须是 {sorted(ASSET_SOURCES)} 之一"
            )
        normalized["source"] = asset_source
        if asset_source == "unit" and "sensitivity" not in item:
            normalized.pop("sensitivity", None)
        locator = item.get("locator")
        if asset_source == "external":
            if (not isinstance(locator, str)
                    or not ASSET_LOCATOR_RE.fullmatch(locator)
                    or not locator.startswith("env:")):
                raise CampaignError(
                    f"{field}.locator 必须是受控 env:BUGX_* 引用"
                )
        elif locator is not None and (
            not isinstance(locator, str) or not ASSET_LOCATOR_RE.fullmatch(locator)
        ):
            raise CampaignError(f"{field}.locator 必须是 runtime:<id> 或 env:BUGX_* 引用")
        if locator is not None:
            normalized["locator"] = locator
    else:
        if "source" in item:
            raise CampaignError(f"{field}.source 只允许出现在 consumed 资产")
        locator = item.get("locator")
        if not isinstance(locator, str) or not ASSET_LOCATOR_RE.fullmatch(locator):
            raise CampaignError(
                f"{field}.locator 必须是 runtime:<id> 或 env:BUGX_* 引用"
            )
        normalized["locator"] = locator
    expires_at = item.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, str) or not expires_at.strip():
            raise CampaignError(f"{field}.expires_at 必须是 ISO-8601 字符串")
        try:
            dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CampaignError(f"{field}.expires_at 不是有效 ISO-8601 时间") from exc
        normalized["expires_at"] = expires_at
    revoked = item.get("revoked", False)
    if not isinstance(revoked, bool):
        raise CampaignError(f"{field}.revoked 必须是布尔值")
    normalized["revoked"] = revoked
    return normalized


def _validate_resources(value: object, field: str) -> dict[str, list[dict]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise CampaignError(f"{field} 必须是对象")
    if set(value) - {"consumed", "produced"}:
        raise CampaignError(f"{field} 只允许 consumed/produced")
    consumed = value.get("consumed", [])
    produced = value.get("produced", [])
    if not isinstance(consumed, list) or not isinstance(produced, list):
        raise CampaignError(f"{field}.consumed/produced 必须是数组")
    normalized_consumed = [
        _validate_asset(item, f"{field}.consumed[{index}]", source="consumed")
        for index, item in enumerate(consumed)
    ]
    normalized_produced = [
        _validate_asset(item, f"{field}.produced[{index}]", source="produced")
        for index, item in enumerate(produced)
    ]
    consumed_ids = [item["asset_id"] for item in normalized_consumed]
    produced_ids = [item["asset_id"] for item in normalized_produced]
    if len(set(consumed_ids)) != len(consumed_ids):
        raise CampaignError(f"{field}.consumed asset_id 不能重复")
    if len(set(produced_ids)) != len(produced_ids):
        raise CampaignError(f"{field}.produced asset_id 不能重复")
    if set(consumed_ids) & set(produced_ids):
        raise CampaignError(f"{field} 同一 Unit 不能同时消费并生产同一资产")
    return {"consumed": normalized_consumed, "produced": normalized_produced}


def _validate_dependency_graph(units: list[dict]) -> dict[str, list[str]]:
    by_scope = {unit["scope"]: unit for unit in units}
    graph: dict[str, list[str]] = {}
    for unit in units:
        value = unit.get("prerequisites", [])
        if not isinstance(value, list):
            raise CampaignError(f"{unit['scope']}.prerequisites 必须是数组")
        if any(not isinstance(item, str) or not item for item in value):
            raise CampaignError(f"{unit['scope']}.prerequisites 必须是非空字符串数组")
        if len(set(value)) != len(value):
            raise CampaignError(f"{unit['scope']}.prerequisites 不能重复")
        unknown = sorted(set(value) - set(by_scope))
        if unknown:
            raise CampaignError(
                f"{unit['scope']}.prerequisites 引用未知 Unit: {', '.join(unknown)}"
            )
        if unit["scope"] in value:
            raise CampaignError(f"{unit['scope']} 不能依赖自身")
        graph[unit["scope"]] = list(value)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(scope: str) -> None:
        if scope in visiting:
            raise CampaignError("prerequisites 存在循环依赖")
        if scope in visited:
            return
        visiting.add(scope)
        for prerequisite in graph[scope]:
            visit(prerequisite)
        visiting.remove(scope)
        visited.add(scope)

    for scope in graph:
        visit(scope)
    return graph


def _asset_catalog(units: list[dict]) -> dict[str, tuple[str, dict]]:
    catalog: dict[str, tuple[str, dict]] = {}
    locator_catalog: dict[str, str] = {}
    for unit in units:
        for asset in unit["resources"]["produced"]:
            asset_id = asset["asset_id"]
            if asset_id in catalog:
                raise CampaignError(
                    f"资产 {asset_id} 被多个 Unit 生产，无法确定唯一来源"
                )
            locator = asset["locator"]
            prior_asset_id = locator_catalog.get(locator)
            if prior_asset_id is not None:
                raise CampaignError(
                    f"资产 locator {locator} 被多个 asset_id 共享: "
                    f"{prior_asset_id}, {asset_id}"
                )
            locator_catalog[locator] = asset_id
            catalog[asset_id] = (unit["scope"], asset)
    return catalog


def _dependency_closure(scope: str, graph: dict[str, list[str]]) -> set[str]:
    found: set[str] = set()
    pending = list(graph.get(scope, []))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(graph.get(current, []))
    return found


def _asset_expired(asset: dict, now: dt.datetime | None = None) -> bool:
    expires_at = asset.get("expires_at")
    if not expires_at:
        return False
    current = now or dt.datetime.now(dt.timezone.utc)
    try:
        expires = dt.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    return expires <= current


def _asset_usable(asset: dict) -> bool:
    """Return whether the reference can be handed to a worker right now."""
    if asset.get("revoked") or _asset_expired(asset):
        return False
    locator = asset.get("locator")
    return not (
        isinstance(locator, str)
        and locator.startswith("env:")
        and not os.environ.get(locator.removeprefix("env:"))
    )


def _unit_blockers(unit: dict, by_scope: dict[str, dict], catalog: dict[str, tuple[str, dict]],
                   graph: dict[str, list[str]], _seen: set[str] | None = None) -> list[dict[str, str]]:
    seen = _seen if _seen is not None else set()
    blockers: list[dict[str, str]] = []
    for prerequisite in graph.get(unit["scope"], []):
        if not _unit_effectively_complete(by_scope[prerequisite], by_scope, catalog, graph, seen):
            blockers.append({"kind": "prerequisite", "scope": prerequisite})
    for asset in unit["resources"]["consumed"]:
        source = asset["source"]
        if asset.get("revoked"):
            blockers.append({"kind": "asset_revoked", "asset_id": asset["asset_id"]})
            continue
        if _asset_expired(asset):
            blockers.append({"kind": "asset_expired", "asset_id": asset["asset_id"]})
            continue
        if source == "external":
            locator = asset["locator"]
            if not os.environ.get(locator.removeprefix("env:")):
                blockers.append({
                    "kind": "missing_external_asset", "asset_id": asset["asset_id"],
                })
            continue
        producer_scope, produced = catalog[asset["asset_id"]]
        if producer_scope not in _dependency_closure(unit["scope"], graph):
            # This is normally rejected during validation; keep the runtime guard
            # so callers using a hand-built payload cannot bypass the contract.
            blockers.append({
                "kind": "undeclared_asset_dependency", "asset_id": asset["asset_id"],
            })
            continue
        if not _unit_effectively_complete(by_scope[producer_scope], by_scope, catalog, graph, seen):
            blockers.append({
                "kind": "asset_producer_incomplete", "asset_id": asset["asset_id"],
            })
        if produced.get("revoked"):
            blockers.append({"kind": "asset_revoked", "asset_id": asset["asset_id"]})
        elif _asset_expired(produced):
            blockers.append({"kind": "asset_expired", "asset_id": asset["asset_id"]})
        elif not _asset_usable(produced):
            blockers.append({
                "kind": "missing_produced_asset", "asset_id": asset["asset_id"],
            })
    return blockers


def _unit_effectively_complete(unit: dict, by_scope: dict[str, dict],
                               catalog: dict[str, tuple[str, dict]],
                               graph: dict[str, list[str]],
                               seen: set[str] | None = None) -> bool:
    """Completion also requires all declared prerequisites and assets to be usable."""
    if not unit_complete(unit):
        return False
    active = seen if seen is not None else set()
    scope = unit["scope"]
    if scope in active:
        return False
    active.add(scope)
    try:
        return not _unit_blockers(unit, by_scope, catalog, graph, active)
    finally:
        active.remove(scope)


def _resource_context(unit: dict, catalog: dict[str, tuple[str, dict]]) -> list[dict]:
    context: list[dict] = []
    for asset in unit["resources"]["consumed"]:
        if asset["source"] == "external":
            locator = asset["locator"]
            kind = asset["kind"]
            sensitivity = asset["sensitivity"]
        else:
            _producer, produced = catalog[asset["asset_id"]]
            locator = produced["locator"]
            # 生产者元数据是权威值，避免消费方把 secret 资产降级为 public。
            kind = produced["kind"]
            sensitivity = produced["sensitivity"]
        context.append({
            "asset_id": asset["asset_id"], "kind": kind,
            "sensitivity": sensitivity, "locator": locator,
        })
    return context


def _unit_view(unit: dict, by_scope: dict[str, dict], catalog: dict[str, tuple[str, dict]],
               graph: dict[str, list[str]]) -> dict:
    result = copy.deepcopy(unit)
    blockers = _unit_blockers(unit, by_scope, catalog, graph)
    result["dependency_status"] = {
        "ready": not blockers, "blocked_by": blockers,
    }
    result["resource_context"] = _resource_context(unit, catalog)
    return result


def _lease_active(lease: object, now: float) -> bool:
    if not isinstance(lease, dict):
        return False
    expires_at = lease.get("expires_at")
    return _finite_epoch(expires_at) and expires_at > now


def unit_complete(unit: dict) -> bool:
    dimensions = unit["dimensions"]
    return (
        dimensions["enumerated"] is True
        and dimensions["reviewed"] is True
        and dimensions["dynamic"] in {"pass", "not_applicable"}
        and dimensions["boundary"] in {"pass", "not_applicable"}
        and bool(dimensions["evidence"])
        and all(item.get("result") not in {"fail", "blocked"}
                for item in dimensions["evidence"])
    )


def _validate_session_id(value: object, field: str = "campaign.session_id") -> str:
    if not isinstance(value, str) or not SESSION_ID_RE.fullmatch(value):
        raise CampaignError(
            f"{field} 必须是安全的非空 Session ID（仅允许字母、数字、._:-）"
        )
    return value


def _check_session_binding(payload: dict, expected_session_id: str | None) -> None:
    """Reject a campaign when the caller is operating another Session's state."""
    stored = _validate_session_id(payload.get("session_id"))
    if expected_session_id is None:
        return
    expected = _validate_session_id(expected_session_id, "expected_session_id")
    if stored != expected:
        raise CampaignError(
            f"campaign Session 不匹配: expected={expected}, actual={stored}"
        )


def validate_campaign(payload: dict, root: Path | None = None) -> list[dict]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CampaignError(
            f"campaign.schema_version 必须为 {SCHEMA_VERSION}；旧版本不兼容，请重新创建 campaign"
        )
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        raise CampaignError("campaign 缺 run_id")
    _validate_session_id(payload.get("session_id"))
    revision = payload.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise CampaignError("campaign.revision 必须是非负整数")
    if root is None and isinstance(payload.get("root"), str):
        root = Path(payload["root"]).resolve()
    units = payload.get("units")
    if not isinstance(units, list) or not units:
        raise CampaignError("campaign.units 必须是非空数组")
    seen: set[str] = set()
    for index, unit in enumerate(units, 1):
        if not isinstance(unit, dict):
            raise CampaignError(f"unit #{index} 必须是对象")
        scope = unit.get("scope")
        if not isinstance(scope, str) or not scope or scope in seen:
            raise CampaignError(f"unit #{index} scope 缺失或重复")
        seen.add(scope)
        priority = unit.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            raise CampaignError(f"{scope} priority 必须是非负整数")
        dimensions = unit.get("dimensions")
        if not isinstance(dimensions, dict):
            raise CampaignError(f"{scope} 缺 dimensions")
        for field in ("enumerated", "reviewed"):
            if not isinstance(dimensions.get(field), bool):
                raise CampaignError(f"{scope} dimensions.{field} 必须是布尔值")
        for field in ("dynamic", "boundary"):
            if dimensions.get(field) not in EXECUTION_STATES:
                raise CampaignError(f"{scope} dimensions.{field} 状态无效")
        dimensions["evidence"] = _validate_evidence_list(
            dimensions.get("evidence"), f"{scope}.dimensions.evidence", root,
        )
        lease = unit.get("lease")
        if lease is not None:
            if not isinstance(lease, dict) or not isinstance(lease.get("owner"), str) \
                    or not lease["owner"] or not isinstance(lease.get("token"), str) \
                    or not lease["token"] or not isinstance(lease.get("expires_at"), (int, float)) \
                    or not _finite_epoch(lease.get("expires_at")):
                raise CampaignError(f"{scope}.lease 格式无效")
        prerequisites = unit.get("prerequisites", [])
        if not isinstance(prerequisites, list):
            raise CampaignError(f"{scope}.prerequisites 必须是数组")
        unit["prerequisites"] = list(prerequisites)
        unit["resources"] = _validate_resources(
            unit.get("resources"), f"{scope}.resources",
        )
        unit["status"] = "complete" if unit_complete(unit) else "pending"
    graph = _validate_dependency_graph(units)
    catalog = _asset_catalog(units)
    for unit in units:
        closure = _dependency_closure(unit["scope"], graph)
        for asset in unit["resources"]["consumed"]:
            if asset["source"] != "unit":
                continue
            produced = catalog.get(asset["asset_id"])
            if produced is None:
                raise CampaignError(
                    f"{unit['scope']} 消费未知资产: {asset['asset_id']}"
                )
            producer_scope, produced_asset = produced
            if producer_scope not in closure:
                raise CampaignError(
                    f"{unit['scope']} 消费资产 {asset['asset_id']} 但未声明其生产 Unit 为前置"
                )
            if produced_asset["kind"] != asset["kind"]:
                raise CampaignError(
                    f"资产 {asset['asset_id']} 的 kind 与生产者不一致"
                )
            if (
                    "sensitivity" in asset
                    and asset["sensitivity"] != produced_asset["sensitivity"]
            ):
                raise CampaignError(
                    f"资产 {asset['asset_id']} 的 sensitivity 与生产者不一致"
                )
            if "locator" in asset and asset["locator"] != produced_asset["locator"]:
                raise CampaignError(
                    f"资产 {asset['asset_id']} 的 locator 与生产者不一致"
                )
    usage = payload.setdefault("asset_usage", {})
    if not isinstance(usage, dict):
        raise CampaignError("campaign.asset_usage 必须是对象")
    for asset_id, count in usage.items():
        if asset_id not in catalog:
            raise CampaignError(f"campaign.asset_usage 引用未知资产: {asset_id}")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise CampaignError(f"campaign.asset_usage[{asset_id}] 必须是非负整数")
    return units


def create_campaign(root: Path, base: str | None = None,
                    session_id: str | None = None) -> dict:
    root = root.resolve()
    session_id = _validate_session_id(session_id or str(uuid.uuid4()), "session_id")
    priority_by_scope: dict[str, tuple[int, str]] = {}
    if base:
        incremental = project_graph.incremental_plan(root, base)
        priority_by_scope = {
            item["scope"]: (item["priority"], item["reason"])
            for item in incremental["units"]
        }
    try:
        audit_units = symbol_index.audit_units(root, strict=True)
    except symbol_index.SourceDiscoveryError as exc:
        raise CampaignError(str(exc)) from exc
    unit_errors = [unit for unit in audit_units if unit.get("error")]
    if unit_errors:
        raise CampaignError(
            "源码单元解析不完整，拒绝创建假完整任务队列: "
            + "; ".join(
                f"{unit['path']}: {unit['error']}" for unit in unit_errors[:5]
            )
        )
    units = []
    for unit in audit_units:
        priority, reason = priority_by_scope.get(unit["scope"], (20, "full-coverage"))
        units.append({
            "scope": unit["scope"],
            "path": unit["path"],
            "name": unit["name"],
            "kind": unit["kind"],
            "priority": priority,
            "priority_reason": reason,
            "status": "pending",
            "prerequisites": [],
            "resources": {"consumed": [], "produced": []},
            "dimensions": {
                "enumerated": True,
                "reviewed": False,
                "dynamic": "not_run",
                "boundary": "not_run",
                "evidence": [],
            },
        })
    if not units:
        raise CampaignError("没有发现可审计源码单元")
    units.sort(key=lambda item: (-item["priority"], item["scope"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "session_id": session_id,
        "created_at": _now(),
        "updated_at": _now(),
        "base": base,
        "root": str(root),
        "revision": 0,
        "asset_usage": {},
        "units": units,
    }


def campaign_summary(payload: dict, *, expected_session_id: str | None = None) -> dict:
    _check_session_binding(payload, expected_session_id)
    units = validate_campaign(payload)
    total = len(units)
    by_scope = {unit["scope"]: unit for unit in units}
    graph = _validate_dependency_graph(units)
    catalog = _asset_catalog(units)
    dimensions = {
        "enumerated": sum(unit["dimensions"]["enumerated"] is True for unit in units),
        "reviewed": sum(unit["dimensions"]["reviewed"] is True for unit in units),
        "dynamic": sum(unit["dimensions"]["dynamic"] in {"pass", "not_applicable"}
                       for unit in units),
        "boundary": sum(unit["dimensions"]["boundary"] in {"pass", "not_applicable"}
                        for unit in units),
        "evidence": sum(bool(unit["dimensions"]["evidence"]) for unit in units),
    }
    complete = sum(
        _unit_effectively_complete(unit, by_scope, catalog, graph) for unit in units
    )
    blocked_units = []
    for unit in units:
        if _unit_effectively_complete(unit, by_scope, catalog, graph):
            continue
        blockers = _unit_blockers(unit, by_scope, catalog, graph)
        if blockers:
            blocked_units.append({"scope": unit["scope"], "blocked_by": blockers})
    produced = [asset for _scope, asset in catalog.values()]
    available = sum(
        _unit_effectively_complete(by_scope[scope], by_scope, catalog, graph)
        and _asset_usable(asset)
        for scope, asset in catalog.values()
    )
    consumed = [
        asset for unit in units for asset in unit["resources"]["consumed"]
    ]
    usage = payload.get("asset_usage", {})
    reuse_count = sum(int(usage.get(asset_id, 0))
                      for asset_id in {asset["asset_id"] for asset in consumed})
    reuse_rate = round(reuse_count / len(consumed) * 100, 1) if consumed else 0.0
    return {
        "run_id": payload["run_id"],
        "session_id": payload["session_id"],
        "complete": complete,
        "total": total,
        "percent": round(complete / total * 100, 1),
        "blocked": len(blocked_units),
        "blocked_units": blocked_units,
        "dimensions": {
            key: {"complete": value, "total": total,
                  "percent": round(value / total * 100, 1)}
            for key, value in dimensions.items()
        },
        "asset_summary": {
            "total_produced": len(produced),
            "total_consumed": len(consumed),
            "available": available,
            "reuse_count": reuse_count,
            "reuse_rate": reuse_rate,
        },
    }


def next_units(payload: dict, limit: int = 10, owner: str | None = None,
               token: str | None = None, lease_seconds: float = DEFAULT_LEASE_SECONDS,
               expected_session_id: str | None = None) \
        -> list[dict]:
    if owner is not None:
        return claim_units(
            payload, owner, limit, token, lease_seconds,
            expected_session_id=expected_session_id,
        )
    if limit < 1:
        raise CampaignError("limit 必须大于 0")
    _check_session_binding(payload, expected_session_id)
    units = validate_campaign(payload)
    by_scope = {unit["scope"]: unit for unit in units}
    graph = _validate_dependency_graph(units)
    catalog = _asset_catalog(units)
    now = _epoch()
    pending = [
        unit for unit in units
        if not _unit_effectively_complete(unit, by_scope, catalog, graph)
        and not _unit_blockers(unit, by_scope, catalog, graph)
        and not _lease_active(unit.get("lease"), now)
    ]
    pending.sort(key=lambda item: (-item["priority"], item["scope"]))
    return [_unit_view(unit, by_scope, catalog, graph) for unit in pending[:limit]]


def claim_units(payload: dict, owner: str, limit: int = 10,
                token: str | None = None,
                lease_seconds: float = DEFAULT_LEASE_SECONDS,
                *, expected_session_id: str | None = None) -> list[dict]:
    if not isinstance(owner, str) or not owner.strip() or "\x00" in owner:
        raise CampaignError("claim owner 必须是非空字符串")
    if token is not None and (not isinstance(token, str) or not token or "\x00" in token):
        raise CampaignError("claim token 必须是非空字符串")
    if limit < 1:
        raise CampaignError("limit 必须大于 0")
    if not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool) \
            or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise CampaignError(f"lease_seconds 必须在 1..{MAX_LEASE_SECONDS}")
    _check_session_binding(payload, expected_session_id)
    units = validate_campaign(payload)
    by_scope = {unit["scope"]: unit for unit in units}
    graph = _validate_dependency_graph(units)
    catalog = _asset_catalog(units)
    now = _epoch()
    claim_token = token or uuid.uuid4().hex
    claimed: list[dict] = []
    newly_claimed: list[dict] = []
    for unit in sorted(units, key=lambda item: (-item["priority"], item["scope"])):
        if _unit_effectively_complete(unit, by_scope, catalog, graph):
            continue
        if _unit_blockers(unit, by_scope, catalog, graph):
            continue
        lease = unit.get("lease")
        if _lease_active(lease, now):
            if lease["owner"] == owner and lease["token"] == claim_token:
                claimed.append(_unit_view(unit, by_scope, catalog, graph))
            continue
        if lease is not None:
            unit.pop("lease", None)
        unit["lease"] = {
            "owner": owner,
            "token": claim_token,
            "expires_at": round(now + float(lease_seconds), 3),
        }
        newly_claimed.append(unit)
        if len(claimed) + len(newly_claimed) >= limit:
            break
    # The newly claimed list is separate from the returned views so an
    # idempotent retry with the same owner/token does not inflate usage stats.
    for unit in newly_claimed:
        for asset in unit["resources"]["consumed"]:
            asset_id = asset["asset_id"]
            payload["asset_usage"][asset_id] = payload["asset_usage"].get(asset_id, 0) + 1
    if newly_claimed:
        payload["revision"] = int(payload.get("revision", 0)) + 1
        payload["updated_at"] = _now()
    claimed.extend(
        _unit_view(unit, by_scope, catalog, graph) for unit in newly_claimed
    )
    return claimed


def update_unit(payload: dict, scope: str, reviewed: bool | None = None,
                dynamic: str | None = None, boundary: str | None = None,
                evidence: list[dict] | None = None, owner: str | None = None,
                token: str | None = None, expected_revision: int | None = None,
                prerequisites: list[str] | None = None,
                resources: dict | None = None,
                produce_assets: list[dict] | None = None,
                consume_assets: list[dict] | None = None,
                *, expected_session_id: str | None = None) -> dict:
    _check_session_binding(payload, expected_session_id)
    units = validate_campaign(payload)
    revision = int(payload.get("revision", 0))
    if expected_revision is not None and expected_revision != revision:
        raise CampaignError(
            f"campaign revision 冲突: expected={expected_revision}, actual={revision}"
        )
    target = next((unit for unit in units if unit["scope"] == scope), None)
    if target is None:
        raise CampaignError(f"未知 scope: {scope}")
    lease = target.get("lease")
    if owner is not None or token is not None:
        if not isinstance(owner, str) or not owner or not isinstance(token, str) or not token:
            raise CampaignError("带 owner/token 更新时必须同时提供二者")
        if not _lease_active(lease, _epoch()) or lease["owner"] != owner \
                or lease["token"] != token:
            raise CampaignError("unit lease 不存在、已过期或不属于当前 owner/token")
    candidate = copy.deepcopy(payload)
    candidate_units = validate_campaign(candidate)
    candidate_target = next(item for item in candidate_units if item["scope"] == scope)
    candidate_dimensions = candidate_target["dimensions"]
    if reviewed is not None:
        candidate_dimensions["reviewed"] = reviewed
    for field, value in (("dynamic", dynamic), ("boundary", boundary)):
        if value is not None:
            if value not in EXECUTION_STATES:
                raise CampaignError(f"{field} 状态无效: {value}")
            candidate_dimensions[field] = value
    if evidence:
        checked = _validate_evidence_list(
            evidence, f"{scope}.dimensions.evidence", Path(payload["root"]).resolve()
            if isinstance(payload.get("root"), str) else None,
        )
        merged = [*candidate_dimensions["evidence"], *checked]
        candidate_dimensions["evidence"] = list({
            json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in merged
        }.values())
    if prerequisites is not None:
        candidate_target["prerequisites"] = copy.deepcopy(prerequisites)
    if resources is not None:
        candidate_target["resources"] = copy.deepcopy(resources)
    if produce_assets or consume_assets:
        current_resources = candidate_target.setdefault(
            "resources", {"consumed": [], "produced": []},
        )
        if produce_assets:
            current_resources.setdefault("produced", []).extend(copy.deepcopy(produce_assets))
        if consume_assets:
            current_resources.setdefault("consumed", []).extend(copy.deepcopy(consume_assets))
    candidate_target["status"] = "complete" if unit_complete(candidate_target) else "pending"
    if candidate_target["status"] == "complete":
        candidate_target.pop("lease", None)
    candidate["revision"] = revision + 1
    candidate["updated_at"] = _now()
    validate_campaign(candidate)
    payload.clear()
    payload.update(candidate)
    return next(item for item in payload["units"] if item["scope"] == scope)


def reprioritize(payload: dict, findings_payload: dict, *, bump: int = 15,
                 max_priority: int = 100,
                 expected_session_id: str | None = None) -> dict:
    """热点再排序：按本轮 findings 的证据路径提升同路径/同目录单元的优先级。

    静态优先级在登记时固定，热点无法在运行中扩散——模块 A 挖到 bug 后，
    同根因的兄弟模块大概率也有问题，却仍按初始顺序排队。本函数把
    findings 的 evidence 路径（及其父目录）映射到未完成单元，有限度地
    提升 priority 并记录可审计的 priority_history；已 complete 的单元不动。
    """
    if not isinstance(bump, int) or isinstance(bump, bool) or not 1 <= bump <= 50:
        raise CampaignError("bump 必须在 1..50")
    if not isinstance(max_priority, int) or isinstance(max_priority, bool) or max_priority < 1:
        raise CampaignError("max_priority 必须是正整数")
    _check_session_binding(payload, expected_session_id)
    findings = findings_payload.get("findings")
    if not isinstance(findings, list):
        raise CampaignError("findings_payload.findings 必须是数组")
    hot_paths: set[str] = set()
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        confirmation = finding.get("confirmation")
        if isinstance(confirmation, dict) and confirmation.get("state") == "rejected":
            continue
        for evidence in finding.get("evidence", []):
            if isinstance(evidence, dict) and isinstance(evidence.get("path"), str):
                normalized = evidence["path"].replace("\\", "/").lstrip("./")
                hot_paths.add(normalized)
                parent = normalized.rsplit("/", 1)[0]
                if parent:
                    hot_paths.add(parent)
    if not hot_paths:
        return {"status": "no-op", "boosted": [], "hot_paths": [],
                "source_findings": len(findings)}
    units = validate_campaign(payload)
    boosted: list[dict] = []
    for unit in units:
        if unit["status"] == "complete":
            continue
        unit_path = str(unit.get("path", "")).replace("\\", "/").lstrip("./")
        matched = sorted(
            path for path in hot_paths
            if unit_path == path or unit_path.startswith(path + "/") or path.startswith(unit_path + "/")
        )
        if not matched:
            continue
        before = unit["priority"]
        unit["priority"] = min(before + bump, max_priority)
        unit["priority_reason"] = "hot-streak:" + ",".join(matched[:3])
        boosted.append({
            "scope": unit["scope"], "before": before, "after": unit["priority"],
            "matched": matched[:3],
        })
    if boosted:
        history = payload.setdefault("priority_history", [])
        if not isinstance(history, list):
            raise CampaignError("campaign.priority_history 必须是数组")
        history.append({
            "at": _now(),
            "bump": bump,
            "source_findings": len(findings),
            "hot_paths": sorted(hot_paths)[:32],
            "boosted_scopes": [item["scope"] for item in boosted],
        })
        payload["revision"] = int(payload.get("revision", 0)) + 1
        payload["updated_at"] = _now()
        units.sort(key=lambda item: (-item["priority"], item["scope"]))
    return {
        "status": "boosted" if boosted else "no-op",
        "boosted": boosted,
        "hot_paths": sorted(hot_paths),
        "source_findings": len(findings),
    }


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CampaignError(f"无法读取 campaign: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignError("campaign 根节点必须是对象")
    return value


def _parse_evidence_args(values: list[str]) -> list[dict]:
    parsed: list[dict] = []
    for value in values:
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CampaignError("--evidence 必须是 JSON 结构化对象") from exc
        if not isinstance(item, dict):
            raise CampaignError("--evidence 必须是 JSON 结构化对象")
        parsed.append(item)
    return parsed


def _parse_resource_args(values: list[str], option: str) -> list[dict]:
    parsed = []
    for value in values:
        try:
            item = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CampaignError(f"{option} 必须是 JSON 结构化对象") from exc
        if not isinstance(item, dict):
            raise CampaignError(f"{option} 必须是 JSON 结构化对象")
        parsed.append(item)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "status", "next", "claim", "update",
                                            "reprioritize"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--campaign", type=Path,
                        help="campaign 文件；省略时使用 .bugx/sessions/<session-id>/audit-campaign.json")
    parser.add_argument("--session-id",
                        help="当前 Session ID；所有非 create 操作必须显式提供")
    parser.add_argument("--base")
    parser.add_argument("--scope")
    parser.add_argument("--findings", type=Path,
                        help="reprioritize 使用：本轮 findings JSON，驱动热点再排序")
    parser.add_argument("--reviewed", choices=("true", "false"))
    parser.add_argument("--dynamic", choices=sorted(EXECUTION_STATES))
    parser.add_argument("--boundary", choices=sorted(EXECUTION_STATES))
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--prerequisite", action="append", default=[],
                        help="update 使用：前置 Unit scope，可重复")
    parser.add_argument("--produce-asset", action="append", default=[],
                        help="update 使用：安全资产引用 JSON，不得含秘密值")
    parser.add_argument("--consume-asset", action="append", default=[],
                        help="update 使用：消费资产引用 JSON，不得含秘密值")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--owner")
    parser.add_argument("--token")
    parser.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--revision", type=int)
    parser.add_argument("--bump", type=int, default=15,
                        help="reprioritize 单次优先级提升量（默认 15，上限 50）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        session_id = args.session_id
        if args.command == "create":
            session_id = _validate_session_id(
                session_id or str(uuid.uuid4()), "session_id",
            )
        elif session_id is None:
            raise CampaignError(f"{args.command} 必须提供 --session-id，防止误用其他 Session 的 campaign")
        else:
            session_id = _validate_session_id(session_id, "--session-id")
        campaign_path = args.campaign or (
            args.root.resolve() / ".bugx" / "sessions" / session_id / "audit-campaign.json"
        )
        if args.command == "create":
            if args.campaign is None:
                campaign_path.parent.mkdir(parents=True, exist_ok=True)
            with _campaign_lock(campaign_path):
                if campaign_path.exists():
                    raise CampaignError(
                        f"campaign 已存在: {campaign_path}；请为新 Session 使用新的目录，禁止覆盖旧运行产物"
                    )
                payload = create_campaign(args.root, args.base, session_id=session_id)
                _atomic_write(campaign_path, payload)
            result: object = campaign_summary(payload, expected_session_id=session_id)
        else:
            if args.command == "status":
                with _campaign_lock(campaign_path):
                    payload = _load(campaign_path)
                    result = campaign_summary(payload, expected_session_id=session_id)
            elif args.command == "next" and args.owner is None:
                payload = _load(campaign_path)
                result = next_units(
                    payload, args.limit, expected_session_id=session_id,
                )
            elif args.command in {"next", "claim"}:
                if not args.owner:
                    raise CampaignError(f"{args.command} 必须提供 --owner")
                with _campaign_lock(campaign_path):
                    payload = _load(campaign_path)
                    result = claim_units(
                        payload, args.owner, args.limit, args.token, args.lease_seconds,
                        expected_session_id=session_id,
                    )
                    _atomic_write(campaign_path, payload)
            elif args.command == "reprioritize":
                if args.findings is None:
                    raise CampaignError("reprioritize 必须提供 --findings")
                try:
                    findings_payload = json.loads(
                        args.findings.read_text(encoding="utf-8"),
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CampaignError(f"无法读取 findings: {exc}") from exc
                with _campaign_lock(campaign_path):
                    payload = _load(campaign_path)
                    result = reprioritize(
                        payload, findings_payload, bump=args.bump,
                        expected_session_id=session_id,
                    )
                    _atomic_write(campaign_path, payload)
            else:
                if not args.scope:
                    raise CampaignError("update 必须提供 --scope")
                with _campaign_lock(campaign_path):
                    payload = _load(campaign_path)
                    result = update_unit(
                        payload, args.scope,
                        None if args.reviewed is None else args.reviewed == "true",
                        args.dynamic, args.boundary,
                        _parse_evidence_args(args.evidence),
                        args.owner, args.token, args.revision,
                        args.prerequisite or None,
                        None,
                        _parse_resource_args(args.produce_asset, "--produce-asset"),
                        _parse_resource_args(args.consume_asset, "--consume-asset"),
                        expected_session_id=session_id,
                    )
                    _atomic_write(campaign_path, payload)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except CampaignError as exc:
        print(f"[audit_campaign] ✗ {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
