#!/usr/bin/env python3
"""core/session_profile.py - Session Profile 快速启动通道与历史意图继承。

支持 --profile <name>、`bugx init --commit-policy life|round|none` 或读取 .bugx/profile.json：
  - default: 交互式全量询问
  - ci: 非交互只读回退 (review: manual, polling: manual, fix: log-only, commit: manual, startup_action: start)
  - targeted: 定向修复快道 (review: mcp-auto, polling: auto, fix: auto, commit: round, recovery: resume, startup_action: start)
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping


LEGACY_SHARE_POLICY_MESSAGE = (
    "share_policy 已移除；请删除旧字段并使用 report_policy=none|local|submit 重建"
)


class LegacySharePolicyError(ValueError):
    """Raised when a removed share-policy field is still present."""


PRESET_PROFILES: dict[str, dict[str, str]] = {
    "default": {},  # 空代表走全量交互
    "ci": {
        "cloud_sync": "skip",
        "review_mode": "manual",
        "polling_policy": "manual",
        "fix_mode": "log-only",
        "commit_policy": "manual",
        "startup_action": "start",
    },
    "targeted": {
        "cloud_sync": "skip",
        "review_mode": "mcp-auto",
        "polling_policy": "auto",
        "fix_mode": "auto",
        "commit_policy": "round",
        "session_recovery": "resume",
        "startup_action": "start",
    },
    # Explicit opt-in for host-agent YOLO mode.  Values stay flat because the
    # existing project profile format is a string map; the resolved snapshot
    # below is the machine-readable contract written to runtime.json.
    "autonomous": {
        "mode": "autonomous",
        "cloud_sync": "sync",
        "review_mode": "mcp-auto",
        "polling_policy": "auto",
        "fix_mode": "auto",
        "commit_policy": "round",
        "session_recovery": "resume",
        "startup_action": "start",
        "mcp_auto_install": "true",
        "docs_mcp": "bugx-docs",
        "coverage_bootstrap": "true",
    },
}

AUTONOMOUS_POLICY_SCHEMA_VERSION = 1
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def autonomous_policy(config: Mapping[str, str] | None) -> dict[str, object] | None:
    """Validate and freeze the small host-agent autonomous policy.

    The profile remains human-editable, while the returned object is the
    immutable runtime snapshot.  Invalid values fail closed instead of being
    silently replaced by a guessed default.
    """
    if isinstance(config, Mapping) and "share_policy" in config:
        raise LegacySharePolicyError(LEGACY_SHARE_POLICY_MESSAGE)
    if not isinstance(config, Mapping) or str(config.get("mode", "")).strip().lower() != "autonomous":
        return None
    cloud_sync = str(config.get("cloud_sync", "sync")).strip().lower()
    if cloud_sync not in {"sync", "skip", "local"}:
        raise ValueError("autonomous cloud_sync 必须是 sync、skip 或 local")
    auto_install = str(config.get("mcp_auto_install", "true")).strip().lower()
    if auto_install not in _TRUE_VALUES | _FALSE_VALUES:
        raise ValueError("autonomous mcp_auto_install 必须是布尔值")
    docs_mcp = str(config.get("docs_mcp", "bugx-docs")).strip()
    if docs_mcp != "bugx-docs":
        raise ValueError("autonomous docs_mcp 只能是 bugx-docs")
    coverage_bootstrap = str(config.get("coverage_bootstrap", "true")).strip().lower()
    if coverage_bootstrap not in _TRUE_VALUES | _FALSE_VALUES:
        raise ValueError("autonomous coverage_bootstrap 必须是布尔值")
    policy: dict[str, object] = {
        "schema_version": AUTONOMOUS_POLICY_SCHEMA_VERSION,
        "mode": "autonomous",
        "auth": {"required": True, "source": "bugx_session"},
        "cloud_sync": {"enabled": cloud_sync == "sync", "policy": cloud_sync},
        "mcp": {
            "auto_install": auto_install in _TRUE_VALUES,
            "required": ["bugx-supervisor", "bugx-supervisor-worker", docs_mcp],
        },
        "fix": {
            "mode": "auto",
            "require_confirmed_finding": True,
            "rollback_on_failure": True,
        },
        "round": {"policy": "auto", "stop": "coverage_and_zero_open_bugs"},
        "commit": {"policy": "round", "push": False},
        "docs": {"mcp": docs_mcp, "auto_retrieve": True},
        "coverage_bootstrap": coverage_bootstrap in _TRUE_VALUES,
    }
    canonical = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    policy["policy_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return policy

COMMIT_POLICY_ALIASES = {
    "life": "life",
    "round": "round",
    "none": "manual",
    "manual": "manual",
    "no-submit": "manual",
    "no_submit": "manual",
    "不提交": "manual",
}


def profile_path(root: Path | None = None) -> Path:
    base = root or Path.cwd()
    return base / ".bugx" / "profile.json"


def _reject_legacy_share_policy(data: Mapping[str, Any]) -> None:
    """Reject removed fields before filtering or merging profile values."""
    if "share_policy" in data:
        raise LegacySharePolicyError(LEGACY_SHARE_POLICY_MESSAGE)
    overrides = data.get("overrides")
    if isinstance(overrides, Mapping) and "share_policy" in overrides:
        raise LegacySharePolicyError(LEGACY_SHARE_POLICY_MESSAGE)


def load_profile(name: str | None = None, root: Path | None = None) -> dict[str, str] | None:
    """按指定名称或 .bugx/profile.json 读取启动 Profile。"""
    if name:
        normalized = name.strip().lower()
        if normalized in PRESET_PROFILES:
            return dict(PRESET_PROFILES[normalized])
    
    # 尝试读取项目级 .bugx/profile.json
    path = profile_path(root)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _reject_legacy_share_policy(data)
                # 支持 {"active_profile": "ci"} 或 直接声明字典
                if "active_profile" in data and str(data["active_profile"]) in PRESET_PROFILES:
                    preset = dict(PRESET_PROFILES[str(data["active_profile"])])
                    if "overrides" in data and isinstance(data["overrides"], dict):
                        preset.update({str(k): str(v) for k, v in data["overrides"].items()})
                    return preset
                return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_profile(name: str, overrides: Mapping[str, str] | None = None, root: Path | None = None) -> Path:
    """持久化保存项目级 Profile 预设。"""
    if overrides is not None:
        _reject_legacy_share_policy(overrides)
    path = profile_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"active_profile": name.strip().lower()}
    if overrides:
        payload["overrides"] = dict(overrides)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def save_commit_policy(policy: str, root: Path | None = None) -> Path:
    """只更新项目提交策略，保留已有 Profile 及其它覆盖项。"""
    normalized = COMMIT_POLICY_ALIASES.get(policy.strip().lower())
    if normalized is None:
        raise ValueError("提交策略必须是 life、round、manual 或 none")
    path = profile_path(root)
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                _reject_legacy_share_policy(loaded)
                payload = loaded
        except (json.JSONDecodeError, OSError):
            payload = {}
    if "active_profile" in payload:
        overrides = payload.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        overrides["commit_policy"] = normalized
        payload["overrides"] = overrides
    else:
        payload["commit_policy"] = normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
