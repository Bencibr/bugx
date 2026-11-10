#!/usr/bin/env python3
"""core/host_runtime/host_bootstrap.py - 宿主 MCP 启动、探测与子进程环境守卫。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import core.hosts as host_plugins
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any

def _get_sh_attr(name: str, default: Any = None) -> Any:
    mod = sys.modules.get('core.supervisor.supervisor_host')
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    return default


from core.supervisor.worker_execution import terminate_group
from core.host_runtime.adapters import BUGX_SUPERVISOR_MCP, HostAdapter, host_adapter
from core.host_runtime.mcp_registry_gate import active_mcp_names
import core.host_runtime.mcp_process_tracker as _pt
_current_mcp_server_version = _pt._current_mcp_server_version

logger = logging.getLogger(__name__)

class _BadRun:
    def __init__(self, returncode: int = 1):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _verify_launcher(script: Path, expected_sha256: str | None = None) -> bool:
    """运行 launch_bugx.py 前校验路径、完整性和可编译性。"""
    if not script.is_file():
        return False
    if script.is_symlink():
        return False
    current = Path(script.anchor)
    for part in Path(os.path.abspath(str(script))).parts[1:]:
        current /= part
        if current.is_symlink():
            return False
    if expected_sha256:
        digest = hashlib.sha256()
        try:
            with script.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return False
        if digest.hexdigest() != expected_sha256:
            return False
    import os as _os
    import py_compile
    import tempfile
    cfile = None
    try:
        fd, cfile = tempfile.mkstemp(suffix=".pyc")
        _os.close(fd)
        # ponytail: cfile 写临时文件避免在脚本旁留字节码垃圾；/dev/null 会因非 regular file 被拒
        py_compile.compile(str(script), doraise=True, cfile=cfile)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if cfile:
            try:
                _os.unlink(cfile)
            except OSError:
                pass


def _extract_session_id(output: str) -> str:
    """Extract a provider session id without trusting arbitrary long output."""
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            # json.JSONDecodeError 是 ValueError 的子类；此外 3.11+ 对超长十进制
            # 数字串的 int() 转换也会抛 ValueError。只捕 JSONDecodeError 会让这
            # 类载荷把异常冒泡出去，整段宿主输出被 str(exc) 顶替——标记全丢，
            # provider 限流退化成"反复重试直到回合预算耗尽"（L2-C2 被架空）。
            value = None
        if isinstance(value, dict):
            for key in (
                "session_id", "sessionId", "sessionID", "session",
                "thread_id", "threadId", "id",
            ):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        match = re.search(
            r"\b(?:session[_ -]?id|session)\b\s*[:=]\s*[\"']?([A-Za-z0-9._:-]+)",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1)
    return ""


_RETRY_WINDOW_RE = re.compile(r"retry-after=(\d+)秒")


def _retry_window_seconds(detail: str) -> int | None:
    """从分类器 detail 里读出结构化重试窗（秒）。

    分类器把窗口写进人可读的 detail（"…；retry-after=58466秒"），这里转成
    机器可读字段供调度侧直接消费——避免每个调用方各自正则解析文本，也避免
    "窗内不得重跑" 只停留在字符串里（无人读、无人守）。
    """

    match = _RETRY_WINDOW_RE.search(detail or "")
    return int(match.group(1)) if match else None


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _post_summary(root: Path, code: int, output: str) -> dict:
    """Read authoritative post metrics from life/findings state, not defaults."""
    summary = {"exit": code, "alive": True, "credited": 0, "tokens": 0,
               "raw": output[:400]}
    parsed = _extract_json_object(output)
    if parsed:
        for key in ("alive", "credited", "tokens"):
            if key in parsed:
                summary[key] = parsed[key]

    life_paths = [root / ".bugx" / "bugx-life.json", root / ".bugx" / "life.json"]
    findings_paths = [root / "findings.json", root / ".bugx" / "findings.json"]
    try:
        import core.bugx_paths as bugx_paths
        scope = bugx_paths.require(root)
        life_paths.insert(0, scope.life)
        findings_paths.insert(0, scope.lifecycle_root / "findings.json")
    except (ImportError, OSError, ValueError, RuntimeError):
        pass
    for path in life_paths:
        state = _read_json(path)
        if not state:
            continue
        if "alive" in state:
            summary["alive"] = bool(state["alive"])
        elif isinstance(state.get("life"), int) and not isinstance(state["life"], bool):
            summary["alive"] = state["life"] > 0
        history = state.get("history")
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            last = history[-1]
            findings = last.get("findings")
            credited = last.get("credited", len(findings) if isinstance(findings, list) else 0)
            if isinstance(credited, int) and not isinstance(credited, bool):
                summary["credited"] = credited
        break
    for path in findings_paths:
        payload = _read_json(path)
        if payload and isinstance(payload.get("token_count"), int):
            summary["tokens"] = payload["token_count"]
            break
    return summary


def _extract_json_object(output: str) -> dict | None:
    decoder = json.JSONDecoder()
    for offset, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _builtin_mcp_servers() -> dict[str, dict[str, object]]:
    """Return the three fixed BugX local MCP registrations."""
    return {
        "bugx-supervisor": {
            "command": "bugx",
            "args": ["mcp", "serve", "--root", ".", "--role", "host"],
        },
        "bugx-supervisor-worker": {
            "command": "bugx",
            "args": ["mcp", "serve", "--root", ".", "--role", "worker"],
        },
        "bugx-docs": {
            "command": "bugx",
            "args": ["mcp", "docs", "serve", "--root", ".",
                     "--db", ".bugx/knowledge/docs.sqlite3"],
        },
    }


def bootstrap_host_mcp(root: Path, adapter: HostAdapter) -> dict[str, object]:
    """Install only BugX's fixed MCP entries for the explicit host run.

    配置形态由宿主插件拥有：``mcp_write.bootstrap`` 非空时直接委托（如 Codex 的
    项目级 ``.codex/config.toml`` 表 + ``$CODEX_HOME`` 项目信任 + ``.bugx`` 运行期
    回执；原生子 Agent 不继承父进程的 ``-c`` 覆盖，故必须有项目级配置，而 Codex
    只对 trusted 项目加载该层——信任自愈与 TOML 写入都在插件里，本函数不复制）。
    尚未迁移的宿主（Claude）仍走本函数内的项目级 ``.mcp.json`` 分支，逐宿主切片
    时删除。
    """
    import shutil
    import core.secure_atomic as secure_atomic

    root = root.resolve()
    servers = _builtin_mcp_servers()
    if adapter.name == "claude":
        target = root / ".mcp.json"
        if target.is_symlink():
            raise OSError(f"拒绝通过符号链接写入 Claude MCP 配置: {target}")
        payload: dict[str, object] = {}
        if target.exists():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise OSError(f"Claude MCP 配置无法解析: {target}") from exc
            if not isinstance(loaded, dict):
                raise OSError(
                    f"Claude MCP 配置顶层必须是对象: {target}。"
                    '合法形态为 {"mcpServers": {"<name>": {...}}}；'
                    "若文件为空或只有一个列表，请重建为对象后再重试。"
                )
            payload = loaded
        current = payload.get("mcpServers")
        if current is None:
            current = {}
            payload["mcpServers"] = current
        if not isinstance(current, dict):
            raise OSError(f"Claude MCP 配置 mcpServers 必须是对象: {target}")
        changed = any(current.get(name) != definition
                      for name, definition in servers.items())
        backup = None
        if changed and target.is_file():
            backup_dir = root / ".bugx" / "backups" / "host-mcp"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"claude-{time.time_ns()}.json"
            shutil.copy2(target, backup)
            backup.chmod(0o600)
        if changed:
            current.update(servers)
            mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
            secure_atomic.atomic_write_json(target, payload, mode=mode)
        return {
            "host": adapter.name, "path": str(target), "changed": changed,
            "backup": str(backup) if backup else None,
        }

    plugin = host_plugins.get(adapter.name)
    if (
        plugin is not None
        and plugin.ready
        and plugin.mcp_write is not None
        and plugin.mcp_write.bootstrap is not None
    ):
        # 宿主配置形态非「JSON 容器键」（如 codex 的 TOML 表 + 运行期回执）：
        # 写入实现由插件 ``mcp_write.bootstrap`` 拥有，引擎只传内置定义。
        return plugin.mcp_write.bootstrap(root, servers)

    return {"host": adapter.name, "path": None, "changed": False, "backup": None}


def probe_builtin_mcp_session(
    root: Path, *, deadline: float | None = None,
) -> dict[str, object]:
    """Perform a real stdio initialize/tools-list probe for host and worker MCP."""
    import shutil

    executable = shutil.which("bugx")
    if not executable:
        return {"error": "找不到 bugx CLI，无法探活内置 MCP"}
    expected_version = _current_mcp_server_version()
    if expected_version is None:
        return {"error": "当前引擎版本不可用，无法生成 MCP 会话证明"}
    proof: dict[str, object] = {}
    for name, role in (("bugx-supervisor", "host"),
                       ("bugx-supervisor-worker", "worker")):
        timeout = 10.0
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                return {"error": "autonomous_wall_time_budget"}
        command = [executable, "mcp", "serve", "--root", ".", "--role", role]
        request = "\n".join((
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}}, ensure_ascii=False),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized",
                        "params": {}}, ensure_ascii=False),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                        "params": {}}, ensure_ascii=False),
            "",
        ))
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=str(root), env={**os.environ,
                "BUGX_PROJECT_ROOT": str(root.resolve())},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=(os.name == "posix"),
            )
            stdout, _stderr = process.communicate(request, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                _get_sh_attr("terminate_group", terminate_group)(process)
                process.communicate()
            detail = "autonomous_wall_time_budget" if deadline is not None and time.monotonic() >= deadline else f"{name} MCP 探活超时: {exc}"
            return {"error": detail}
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            if process is not None and process.poll() is None:
                _get_sh_attr("terminate_group", terminate_group)(process)
            return {"error": f"{name} MCP 探活失败: {type(exc).__name__}: {exc}"}
        finally:
            if process is not None:
                _get_sh_attr("terminate_group", terminate_group)(process)
        responses: list[dict[str, object]] = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                responses.append(value)
        initialize_result = next(
            (item.get("result") for item in responses if item.get("id") == 1), None,
        )
        initialized = isinstance(initialize_result, dict)
        server_info = initialize_result.get("serverInfo") if initialized else None
        if (not isinstance(server_info, Mapping)
                or server_info.get("name") != BUGX_SUPERVISOR_MCP
                or server_info.get("version") != expected_version):
            return {"error": f"{name} MCP serverInfo 与当前引擎不匹配"}
        tool_result = next(
            (item.get("result") for item in responses if item.get("id") == 2), None,
        )
        tools = tool_result.get("tools") if isinstance(tool_result, dict) else None
        if not initialized or not isinstance(tools, list):
            return {"error": f"{name} MCP 探活未返回有效 initialize/tools/list"}
        proof[name] = {
            "server": BUGX_SUPERVISOR_MCP,
            "server_version": expected_version,
            "root": str(root.resolve()),
            "loaded": True, "callable": True, "healthy": True,
            "tool_names": [
                str(item.get("name")) for item in tools
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ][:256],
        }
    return proof


def _pinned_child_environment(
    cwd: Path, extra: Mapping[str, str | None] | None = None,
) -> dict[str, str]:
    """Build a child environment whose ``PWD`` agrees with ``cwd``.

    ``subprocess`` changes the process working directory but leaves an inherited
    ``PWD`` untouched. OpenCode uses ``PWD`` when binding its project/session,
    so a stale caller value can send a headless host to the wrong workspace.
    """

    child_env = os.environ.copy()
    if extra:
        for name, value in extra.items():
            if value is None:
                child_env.pop(name, None)
            else:
                child_env[name] = value
    child_env["PWD"] = str(cwd.resolve())
    return child_env


