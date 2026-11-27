"""Antigravity ACP adapter.

``agy acp`` is a real ACP v1 server.  Authentication remains an external
precondition owned by Antigravity; the adapter must expose the correct command
and let the probe report ``Authentication required`` instead of hiding it as a
missing/unsupported ACP implementation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .base import AcpPlugin


class AntigravityAcpPlugin(AcpPlugin):
    name = "antigravity"
    executable = "agy"
    status = "ready"
    supports_project_permissions = False

    def acp_argv(
        self,
        *,
        model: str | None = None,
        data_dir: Path | str | None = None,
    ) -> list[str]:
        # Antigravity exposes no model/data-dir ACP flags.  The selected Agent
        # is explicit so ACP cannot silently fall back to the default Agent.
        # The official ACP flag is required for an unattended client to execute
        # MCP calls; otherwise Antigravity emits a permission request that a
        # headless ACP client cannot approve and the tool call is denied.
        del model, data_dir
        return [
            "agy", "acp", "--agent", "BugX",
            "--dangerously-skip-permissions", "--disable-telemetry",
        ]

    def launch_policy(self) -> dict[str, object]:
        return {
            "auto_approve": True,
            "auto_approve_flag": "--dangerously-skip-permissions",
            "model_flag": None,
            "reason": (
                "Antigravity ACP 使用缓存登录凭据；无人值守 MCP 探活必须使用官方 "
                "--dangerously-skip-permissions，否则 ACP 客户端无法批准工具请求；"
                "无凭据时由宿主返回 Authentication required"
            ),
        }

    def ensure_probe_servers(
        self,
        project_root: Path,
        config_paths: tuple[Path, ...],
        servers: dict[str, dict[str, Any]],
    ) -> list[str]:
        """重绑隔离项目的 MCP，并保持 ACP 冷启动的直通引擎启动器。

        Antigravity 会在 ACP 会话建立前预加载 stdio MCP。探针项目是从真实项目
        复制出来的，不能沿用真实项目的绝对 ``--root``；而通用探针定义使用完整
        ``bugx`` CLI，在 Windows 冷启动窗口内又可能超时。因此这里由宿主适配器
        重建三条 BugX 条目：解释器直接执行已安装引擎的 ``skills/*_mcp_server.py``。
        只允许改写探针项目内部的 ``.agents/mcp_config.json``。
        """
        project_root = Path(project_root).resolve()
        engine_override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT", "").strip()
        engine_root = (
            Path(engine_override).expanduser().resolve()
            if engine_override
            else (Path.home() / ".bugx" / "core").resolve()
        )
        python = str(Path(sys.executable).resolve())
        expected: dict[str, dict[str, Any]] = {}
        roles = {
            "bugx-supervisor": "host",
            "bugx-supervisor-worker": "worker",
        }
        for name, role in roles.items():
            script = engine_root / "skills" / "bugx_mcp_server.py"
            if not script.is_file():
                raise ValueError(f"Antigravity 探活缺少 BugX 引擎脚本: {script}")
            expected[name] = {
                "type": "stdio",
                "command": python,
                "args": [
                    str(script), "serve", "--root", str(project_root),
                    "--role", role,
                ],
            }
        docs_script = engine_root / "skills" / "bugx_docs_mcp_server.py"
        if not docs_script.is_file():
            raise ValueError(f"Antigravity 探活缺少 BugX 文档引擎脚本: {docs_script}")
        expected["bugx-cloud"] = {
            "type": "stdio",
            "command": python,
            "args": [
                str(docs_script), "serve", "--root", str(project_root),
                "--db", str(project_root / ".bugx" / "knowledge" / "docs.sqlite3"),
            ],
        }
        # ``servers`` 是探针的通用输入；名称集合仍由探针控制，具体命令由本适配器
        # 收敛为直通引擎定义，避免隐式接受第三方条目。
        names = [name for name in expected if name in servers]
        updated: list[str] = []
        for path in config_paths:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if project_root not in resolved.parents or resolved.is_symlink() or not resolved.is_file():
                continue
            try:
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            block = payload.get("mcpServers")
            if not isinstance(block, dict):
                block = {}
                payload["mcpServers"] = block
            changed = False
            for name in names:
                if block.get(name) != expected[name]:
                    block[name] = expected[name]
                    changed = True
                    if name not in updated:
                        updated.append(name)
            if not changed:
                continue
            mode = resolved.stat().st_mode & 0o777
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent,
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, resolved)
            finally:
                tmp.unlink(missing_ok=True)
        return updated


PLUGIN = AntigravityAcpPlugin
