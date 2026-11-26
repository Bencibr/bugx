#!/usr/bin/env python3
"""core/host_acp/agnescode.py - Agnes Code 的 ACP 适配插件。

实机口径：
* ACP 入口为 ``agnescode acp``（或 ``agnes acp``），工作区绑定由探针 cwd 提供；
* 探活服务器绑定到探活项目的 ``agnescode.json``（v2 ``mcp.servers`` 形状）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any

from .base import AcpPlugin

__all__ = ["AgnesCodeAcpPlugin", "PLUGIN", "is_available", "run_static_degraded"]


def _resolve_executable() -> str:
    for cmd in ("agnescode", "agnes"):
        found = shutil.which(cmd)
        if found:
            return found
    return "agnescode"


class AgnesCodeAcpPlugin(AcpPlugin):
    """Agnes Code ACP 插件。"""

    name = "agnescode"
    executable = "agnescode"
    status = "ready"
    supports_project_permissions = True

    def acp_argv(
        self,
        *,
        model: str | None = None,
        data_dir: Path | str | None = None,
    ) -> list[str]:
        del model, data_dir
        return [_resolve_executable(), "acp"]

    def ensure_probe_servers(
        self,
        project_root: Path,
        config_paths: tuple[Path, ...],
        servers: dict[str, dict[str, Any]],
    ) -> list[str]:
        """把 BugX 通道绑定到探活项目的 ``agnescode.json``（v2 ``mcp.servers`` 形状）。"""
        updated: list[str] = []
        root_resolved = project_root.resolve()
        for path in config_paths:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if root_resolved not in resolved.parents and resolved.parent != root_resolved:
                continue
            try:
                if resolved.is_symlink() or not resolved.is_file():
                    continue
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if "mcp" not in payload:
                payload["mcp"] = {}
            block = payload["mcp"]
            if not isinstance(block, dict):
                raise ValueError(f"AgnesCode v2 mcp 配置必须是对象: {resolved}")
            entries = block.get("servers")
            if "servers" not in block:
                entries = {}
                block["servers"] = entries
            if not isinstance(entries, dict):
                raise ValueError(f"AgnesCode v2 mcp.servers 必须是对象: {resolved}")

            changed = False
            for server_name, server_def in servers.items():
                target_def = {
                    "type": "local",
                    "command": [server_def.get("command", "bugx"), *server_def.get("args", [])],
                    "disabled": False,
                }
                if entries.get(server_name) != target_def:
                    entries[server_name] = target_def
                    changed = True

            if changed:
                resolved.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                updated.append(str(resolved))
        return updated


PLUGIN = AgnesCodeAcpPlugin


def is_available() -> tuple[bool, str]:
    for cmd in ("agnescode", "agnes"):
        found = shutil.which(cmd)
        if found:
            return True, found
    return False, "agnescode 可执行文件未安装；ACP 通道不可用"


def run_static_degraded(root: Path | None = None) -> dict[str, Any]:
    available, detail = is_available()
    return {
        "plugin": "agnescode",
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": "static_facts_only" if available else "agnescode_not_installed",
        "probe_mode": "static_degraded",
        "evidence": {
            "availability_detail": detail,
            "project_root": str(root) if root else None,
        },
    }
