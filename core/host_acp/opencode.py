#!/usr/bin/env python3
"""core/host_acp/opencode.py - OpenCode 的 ACP 适配插件（S2 收敛宿主）。

实机口径（``opencode acp --help``，build 179 起多次复测；模型面于 build 204 复测）：

* ACP 入口就是 ``opencode acp``，工作区绑定由探针的 ``subprocess(cwd=<probe_root>)``
  提供；``opencode acp`` **没有** ``--dir`` flag，误传会以退出码 1 直接死掉。
* 没有 ``--model`` flag（help 只有 ``--login``）：显式模型必须在协议内下发，命令行传
  ``--model`` 只会打印 usage 并以 0 退出，形成「静默死会话」——比失败更难归因。
* 模型面契约（2.1.3 实测，2026-10-24）：``session/new`` 只回 ``configOptions``
  （``{"id": "model", "category": "model", "type": "select", "currentValue": ...,
  "options": [{"value": ...}]}``），切换走 ``session/set_config_option
  {configId: "model", value: ...}`` 并由宿主回写 ``currentValue``；旧式
  ``session/set_model`` 回 ``-32601 Method not found``。探针以 config option 优先，
  ``set_model`` 只作旧版本降级阶梯。
* 没有 ``--data-dir``：用户级数据目录不可重定向，因此也不做 cline 式沙箱。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .base import AcpPlugin

__all__ = ["OpenCodeAcpPlugin", "PLUGIN", "is_available", "run_static_degraded"]


class OpenCodeAcpPlugin(AcpPlugin):
    """OpenCode ACP 插件：S2 首选收敛宿主。"""

    name = "opencode"
    executable = "opencode"
    status = "ready"
    supports_project_permissions = True

    def acp_argv(
        self,
        *,
        model: str | None = None,
        data_dir: Path | str | None = None,
    ) -> list[str]:
        # model / data_dir 被**故意忽略**：该入口没有对应 flag，见模块文档。
        del model, data_dir
        return ["opencode", "acp"]

    def ensure_probe_servers(
        self,
        project_root: Path,
        config_paths: tuple[Path, ...],
        servers: dict[str, dict[str, Any]],
    ) -> list[str]:
        """把 BugX 通道绑定到**探活项目**的 ``opencode.json``（v2 ``mcp.servers`` 形状）。

        OpenCode 的服务器形状是 ``{"type": "local", "command": [argv...], "disabled": false}``；
        规范 BugX 名称由探针 builder 独占：既有条目也重绑到本次隔离 root，避免复制的
        项目配置把 MCP 指回源项目。非 BugX 条目只在隔离副本中保留且不改写。
        """
        updated: list[str] = []
        root_resolved = project_root.resolve()
        for path in config_paths:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if root_resolved not in resolved.parents and resolved.parent != root_resolved:
                continue  # 只写探活项目内部
            try:
                if resolved.is_symlink() or not resolved.is_file():
                    continue
                payload = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            legacy = sorted({"plugin", "permission", "tools", "agent"}.intersection(payload))
            if legacy:
                raise ValueError(
                    f"检测到 OpenCode v1 配置字段 {', '.join(legacy)}，拒绝探活迁移: {resolved}"
                )
            if "mcp" not in payload:
                payload["mcp"] = {}
            block = payload["mcp"]
            if not isinstance(block, dict):
                raise ValueError(f"OpenCode v2 mcp 配置必须是对象: {resolved}")
            entries = block.get("servers")
            if "servers" not in block and block:
                raise ValueError(f"检测到旧版扁平 OpenCode mcp 配置，拒绝探活迁移: {resolved}")
            if "servers" not in block:
                entries = {}
                block["servers"] = entries
            if not isinstance(entries, dict):
                raise ValueError(f"OpenCode v2 mcp.servers 必须是对象: {resolved}")
            legacy_entries = sorted(
                str(name) for name, spec in entries.items()
                if isinstance(spec, dict) and "enabled" in spec
            )
            if legacy_entries:
                raise ValueError(
                    "检测到 OpenCode v1 MCP enabled 字段，拒绝探活迁移: "
                    f"{', '.join(legacy_entries)}: {resolved}"
                )
            changed = False
            for name, spec in servers.items():
                command = spec.get("command")
                args = spec.get("args") or []
                if not isinstance(command, str) or not command:
                    raise ValueError(f"无效的探活 MCP 命令: {name}")
                if not isinstance(args, (list, tuple)):
                    raise ValueError(f"无效的探活 MCP 参数: {name}")
                expected = {
                    "type": "local",
                    "command": [command, *(str(item) for item in args)],
                }
                if name in {"bugx-supervisor", "bugx-supervisor-worker"}:
                    expected["codemode"] = False
                if entries.get(name) == expected:
                    continue
                entries[name] = expected
                if name not in updated:
                    updated.append(name)
                changed = True
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

    def launch_policy(self) -> dict:
        return {
            "auto_approve": False,
            "auto_approve_flag": None,
            "model_flag": None,
            "reason": ("opencode acp 无 --model/--dir flag；"
                       "模型走 session/set_config_option(configId=model)"),
        }


PLUGIN = OpenCodeAcpPlugin


def is_available() -> tuple[bool, str]:
    """降级契约：机械探活宿主 CLI 是否安装（零子进程 ``shutil.which``）。

    ACP 适配器探活语义：宿主 CLI 缺失时本适配器无法拉起任何真实 ACP 会话，
    调用方必须走 :func:`run_static_degraded` 输出静态适配器事实声明；真实 ACP
    可达性（仅 ``acp_status == "ready"`` 的宿主适用）归宿主探活链路验证，
    本函数不发任何子进程。
    """
    path = shutil.which(PLUGIN.executable)
    if path is None:
        return False, (
            f"{PLUGIN.executable} 可执行文件未安装；ACP 适配器无法拉起真实会话"
        )
    return True, path


def run_static_degraded(root: Path | None = None) -> dict:
    """宿主 CLI 缺失/降级时的静态输出：只声明适配器事实，绝不伪装 ACP 会话。

    与真实 ACP 探活输出字段级可区分：``status="degraded"`` + ``degraded=True``
    + ``degraded_reason``；``acp_status`` 保持机读声明，MCP 健康显式 ``unknown``
    （真实可达性归宿主探活链路，本函数不探 MCP、不发子进程）。
    """
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
