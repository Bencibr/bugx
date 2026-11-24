#!/usr/bin/env python3
"""core/host_acp/kimi.py - Kimi Code CLI 的 ACP 适配插件。

实机口径（``kimi 2.1.1``，2026-11-23 实测）：

* ACP 入口为 ``kimi acp``，通过 stdio 运行标准 JSON-RPC 2.0 Agent Client Protocol
  （``initialize`` 返回 ``protocolVersion: 1``、``agentInfo.name: "Kimi Code CLI"``）。
* ``kimi acp --help`` 仅接受 ``--login`` 与 ``--region``，无 ``--dir`` / ``--model`` /
  ``--data-dir`` flag；工作区绑定由探针子进程 ``cwd=<probe_root>`` 与 ``session/new``
  参数提供。
* 项目级 MCP 配置位于 ``.kimi-code/mcp.json``，容器键为标准 ``mcpServers``
  （``{"command": "...", "args": [...]}``），优先级高于用户级
  ``<KIMI_CODE_HOME>/mcp.json``（默认 ``~/.kimi-code/mcp.json``）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .base import AcpPlugin

__all__ = ["KimiAcpPlugin", "PLUGIN", "is_available", "run_static_degraded"]

_HOST_NAME = __name__.rsplit(".", 1)[-1]


def _kimi_code_home() -> Path:
    configured = os.environ.get("KIMI_CODE_HOME")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".kimi-code"


def _resolve_kimi_executable() -> str | None:
    found = shutil.which("kimi")
    if found:
        return found
    bin_dir = _kimi_code_home() / "bin"
    names = ("kimi.exe", "kimi.cmd", "kimi.bat", "kimi") if os.name == "nt" else ("kimi",)
    for name in names:
        fallback = bin_dir / name
        if fallback.is_file():
            return str(fallback)
    return None


class KimiAcpPlugin(AcpPlugin):
    """Kimi Code CLI ACP 插件。"""

    name = _HOST_NAME
    executable = "kimi"
    status = "ready"
    supports_project_permissions = False

    def acp_argv(
        self,
        *,
        model: str | None = None,
        data_dir: Path | str | None = None,
    ) -> list[str]:
        del model, data_dir
        return [_resolve_kimi_executable() or self.executable, "acp"]

    def launch_policy(self) -> dict[str, Any]:
        """返回 Kimi Code CLI 的 ACP 启动策略。"""
        return {
            "auto_approve": False,
            "auto_approve_flag": None,
            "model_flag": None,
            "reason": "kimi acp 无 --model/--dir flag；工作区由 cwd 绑定，通过 ACP 协议交互",
        }

    def ensure_probe_servers(
        self,
        project_root: Path,
        config_paths: tuple[Path, ...],
        servers: dict[str, dict[str, Any]],
    ) -> list[str]:
        """将 BugX 通道写入探活项目内部的 ``.kimi-code/mcp.json``（标准 ``mcpServers`` 形状）。"""
        updated: list[str] = []
        root_resolved = project_root.resolve()
        targets = list(config_paths) or [root_resolved / ".kimi-code" / "mcp.json"]
        for path in targets:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if root_resolved not in resolved.parents and resolved.parent != root_resolved:
                continue  # 严格限定只写探活项目内部
            payload: dict[str, Any] = {}
            if resolved.is_file() and not resolved.is_symlink():
                try:
                    loaded = json.loads(resolved.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload = loaded
                except (OSError, UnicodeError, ValueError):
                    payload = {}
            mcp_servers = payload.get("mcpServers")
            if not isinstance(mcp_servers, dict):
                mcp_servers = {}
                payload["mcpServers"] = mcp_servers
            changed = False
            for name, spec in servers.items():
                command = spec.get("command")
                args = spec.get("args") or []
                if not isinstance(command, str) or not command:
                    raise ValueError(f"无效的探活 MCP 命令: {name}")
                if not isinstance(args, (list, tuple)):
                    raise ValueError(f"无效的探活 MCP 参数: {name}")
                expected: dict[str, Any] = {
                    "command": command,
                    "args": [str(item) for item in args],
                }
                env = spec.get("env")
                if isinstance(env, dict) and env:
                    expected["env"] = {str(k): str(v) for k, v in env.items()}
                if mcp_servers.get(name) != expected:
                    mcp_servers[name] = expected
                    changed = True
            if changed or not resolved.exists():
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                updated.append(str(resolved))
        return updated


PLUGIN = KimiAcpPlugin


def is_available() -> tuple[bool, str]:
    """降级契约：机械探活 Kimi Code CLI 是否安装（零子进程）。"""
    path = _resolve_kimi_executable()
    if path is None:
        return False, "kimi 可执行文件未安装；ACP 适配器无法拉起真实会话"
    return True, path


def run_static_degraded(root: Path | None = None) -> dict:
    """宿主 CLI 缺失/降级时的静态输出：只声明适配器事实，绝不伪装 ACP 会话。"""
    available, availability_detail = is_available()
    plugin = PLUGIN()
    return {
        "adapter": f"core/host_acp/{plugin.name}.py",
        "plugin": plugin.name,
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "static_facts_only" if available else f"{plugin.name}_cli_not_installed"
        ),
        "probe_mode": "static_degraded",
        "acp_status": plugin.status,
        "acp_supported": plugin.supports_acp(),
        "acp_reason": plugin.reason or None,
        "executable": plugin.executable,
        "mcp_health": "unknown",
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "project_root": str(root) if root else None,
        },
        "metadata": {
            "degraded_mode": "static_adapter_facts_only",
            "mcp_probing": "deferred_to_host_probe_chain",
            "fallback": "宿主 CLI 缺失时由外层宿主/用户决策接管；BugX 不代装宿主 CLI",
        },
    }
