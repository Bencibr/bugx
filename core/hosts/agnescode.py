#!/usr/bin/env python3
"""core/hosts/agnescode.py - Agnes Code 宿主插件及自治配置环境。

事实来源（Agnes AI / Agnes Code）：
* identity   ← 可执行程序 ``agnescode``（别名 ``agnes``）；项目标记 ``agnescode.json`` / ``.agnescode``
* install    ← ``subagent`` / ``agent`` 入口：``.agnescode/agents/bugx.md``
* engine_run ← ``agnescode run --standalone --agent <agent> --format json --auto <prompt>``
* mcp_write  ← ``mcp.servers`` 容器键，配置文件 ``agnescode.json`` 与镜像 ``.agnescode/agnescode.json``
* acp        ← ``core/host_acp/agnescode.py``（``agnescode acp``）
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
from typing import Iterator

from core.infra.json_utils import unique_json_object

from .base import (
    AcpFacet,
    EngineRunFacet,
    HostPlugin,
    IdentityFacet,
    InstallFacet,
    McpWriteFacet,
    RunContext,
    STATUS_READY,
    WorkerRuntimeFacet,
)

__all__ = ["PLUGIN", "is_available", "run_static_degraded"]

_NAME = "agnescode"


def _resolve_executable() -> str | None:
    for cmd in ("agnescode", "agnes"):
        found = shutil.which(cmd)
        if found:
            return found
    app_agnesd = Path("/Applications/AgnesCode.app/Contents/Resources/bin/agnesd")
    if app_agnesd.is_file():
        return str(app_agnesd)
    return None


def _agnes_app_markers() -> tuple[Path, ...]:
    home = Path.home()
    return (
        Path("/Applications/AgnesCode.app"),
        home / "Applications" / "AgnesCode.app",
        home / "Library" / "Application Support" / "AgnesCode",
        home / ".agnes",
    )


def _configured_model(root: Path) -> str:
    """Read the project model without making it part of BugX state."""
    configured = os.environ.get("BUGX_AGNESCODE_MODEL", "").strip()
    if configured:
        return configured
    for candidate in (root / "agnescode.json", root / ".agnescode" / "agnescode.json"):
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            model = payload.get("model") if isinstance(payload, dict) else ""
            if isinstance(model, str) and model.strip():
                return model.strip()
    return ""


def _strip_jsonc_comments_and_trailing_commas(source: str) -> str:
    """Remove JSONC comments and trailing commas without touching string data."""
    clean: list[str] = []
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
                clean.append(char)
            else:
                clean.append(" ")
        elif block_comment:
            if char == "*" and next_char == "/":
                clean.extend((" ", " "))
                index += 1
                block_comment = False
            else:
                clean.append(char if char in "\r\n" else " ")
        elif in_string:
            clean.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            clean.append(char)
        elif char == "/" and next_char == "/":
            clean.extend((" ", " "))
            index += 1
            line_comment = True
        elif char == "/" and next_char == "*":
            clean.extend((" ", " "))
            index += 1
            block_comment = True
        else:
            clean.append(char)
        index += 1

    if block_comment:
        raise ValueError("unterminated block comment")

    without_comments = "".join(clean).lstrip("\ufeff")
    clean = []
    in_string = False
    escaped = False
    index = 0
    while index < len(without_comments):
        char = without_comments[index]
        if in_string:
            clean.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            clean.append(char)
        elif char == ",":
            lookahead = index + 1
            while lookahead < len(without_comments) and without_comments[lookahead].isspace():
                lookahead += 1
            if lookahead >= len(without_comments) or without_comments[lookahead] not in "]}":
                clean.append(char)
        else:
            clean.append(char)
        index += 1
    return "".join(clean)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    return unique_json_object(
        pairs,
        error_factory=lambda key: ValueError(f"duplicate key: {key}"),
    )


def _read_agnescode_config(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(
            _strip_jsonc_comments_and_trailing_commas(path.read_text(encoding="utf-8")),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"AgnesCode 自治配置无法安全隔离: {path.name} ({type(exc).__name__})"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"AgnesCode 自治配置必须是对象: {path.name}")
    return parsed


def _child_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _agnescode_global_config_dir(root: Path) -> Path:
    configured = os.environ.get("AGNESCODE_CONFIG_DIR")
    if "AGNESCODE_CONFIG_DIR" in os.environ:
        if not configured:
            raise RuntimeError("AGNESCODE_CONFIG_DIR 为空，无法隔离 AgnesCode 用户配置")
        return _child_path(configured, root)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = _child_path(xdg, root) if xdg else Path.home() / ".config"
    return (base / "agnescode").resolve()


@contextmanager
def _autonomous_environment(root: Path) -> Iterator[dict[str, str | None]]:
    """Hide user-global plugin/MCP sources while preserving AgnesCode's project config."""
    for name in ("AGNESCODE_CONFIG", "AGNESCODE_CONFIG_CONTENT"):
        if os.environ.get(name):
            raise RuntimeError(
                f"AgnesCode 自治配置隔离不接受额外覆盖 {name}；请先移除该覆盖再运行"
            )
    for name in ("AGNESCODE_CONFIG_PROJECT_DISABLE", "AGNESCODE_DISABLE_PROJECT_CONFIG"):
        value = os.environ.get(name, "")
        if value == "1" or value.lower() == "true":
            raise RuntimeError(f"{name} 禁用了项目配置，BugX MCP 无法加载")

    import tempfile

    source_dir = _agnescode_global_config_dir(root)
    parent = source_dir.parent if source_dir.parent.is_dir() else Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="bugx-agnescode-config-", dir=parent) as private_name:
        private_dir = Path(private_name).resolve()
        project_root = root.resolve()
        if private_dir == project_root or project_root in private_dir.parents:
            raise RuntimeError("AgnesCode 自治私有配置目录落在被测项目内，已阻断")
        private_dir.chmod(0o700)

        if source_dir.exists():
            if not source_dir.is_dir():
                raise RuntimeError("AGNESCODE_CONFIG_DIR 不是目录，无法安全隔离")
            try:
                for entry in source_dir.iterdir():
                    if entry.name in {"agnescode.json", "agnescode.jsonc", "plugin", "plugins"}:
                        continue
                    dest = private_dir / entry.name
                    try:
                        dest.symlink_to(entry, target_is_directory=entry.is_dir())
                    except OSError:
                        if os.name != "nt":
                            raise
                        if entry.is_dir():
                            shutil.copytree(entry, dest, dirs_exist_ok=True)
                        else:
                            shutil.copy2(entry, dest)
            except OSError as exc:
                raise RuntimeError(
                    f"AgnesCode 自治配置资产无法隔离 ({type(exc).__name__})"
                ) from exc

            for name in ("agnescode.json", "agnescode.jsonc"):
                source_file = source_dir / name
                if not source_file.exists() and not source_file.is_symlink():
                    continue
                if not source_file.is_file():
                    raise RuntimeError(f"AgnesCode 用户配置不是文件: {name}")
                payload = _read_agnescode_config(source_file)
                for key in ("mcp", "plugin", "plugins"):
                    payload.pop(key, None)
                target = private_dir / name
                target.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                target.chmod(0o600)

        yield {
            "AGNESCODE_CONFIG_DIR": str(private_dir),
            "AGNESCODE_CONFIG": None,
            "AGNESCODE_CONFIG_CONTENT": None,
        }


def _argv(ctx: RunContext, *, auto: bool) -> list[str]:
    exe = _resolve_executable() or "agnescode"
    cmd = [exe, "run"]
    if auto:
        cmd.append("--standalone")
    cmd += ["--agent", ctx.agent, "--format", "json"]
    model = getattr(ctx, "model", None) or _configured_model(ctx.root)
    if model:
        cmd += ["--model", model]
    if auto:
        cmd.append("--auto")
    if ctx.session:
        cmd += ["-s", ctx.session]
    cmd.append(ctx.prompt)
    return cmd


def _headless_argv(ctx: RunContext) -> list[str]:
    return _argv(ctx, auto=False)


def _autonomous_argv(ctx: RunContext) -> list[str]:
    return _argv(ctx, auto=True)


def _recon_scout_argv(
    root: Path, prompt_text: str, prompt_file: Path, inventory: object | None = None,
) -> list[str]:
    del root
    exe = _resolve_executable() or "agnescode"
    cmd = [exe, "run", "--standalone", "--format", "json", "--auto"]
    scout_model = (
        os.environ.get("BUGX_SCOUT_MODEL")
        or os.environ.get("BUGX_AGNESCODE_MODEL")
        or os.environ.get("AGNESCODE_MODEL")
    )
    if scout_model:
        cmd.extend(["-m", scout_model])
    cmd.extend(["-f", str(prompt_file), "Execute BugX Recon Scout review"])
    return cmd


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    return (root / "agnescode.json", root / ".agnescode" / "agnescode.json")


def _agent_config_path(root: Path) -> Path:
    return root / ".agnescode" / "agents" / "bugx.md"


def _mcp_health(name: str, cfg: dict, root: Path | None = None) -> bool:
    del name, cfg, root
    return True


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="Agnes Code",
        order=20,
        executable="agnescode",
        markers=("agnescode.json", ".agnescode/agnescode.json", ".agnescode", ".agnes"),
        aliases=("agnes",),
        app_markers=_agnes_app_markers,
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="agent",
        path=".agnescode/agents/bugx.md",
        marker=".agnescode",
        entry_mode="rendered",
        required_paths=(),
        legacy_paths=(),
        capabilities=frozenset({"native-mcp-config", "global-agent-link"}),
        interaction={
            "tool": "question",
            "single_select": "multiple=false",
        },
        mcp_relpaths=("agnescode.json", ".agnescode/agnescode.json"),
        worker_subagent=False,
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        agent_config_path=_agent_config_path,
        mcp_health=_mcp_health,
        host_surface="tui",
        recon_scout_argv=_recon_scout_argv,
        native_task=True,
        autonomous_environment=_autonomous_environment,
    ),
    worker_runtime=WorkerRuntimeFacet(
        mode="multi",
        max_concurrency=None,
        requires_process_mode=False,
        sandbox_sync=(),
    ),
    mcp_write=McpWriteFacet(
        container_key="mcp.servers",
        writer_key=_NAME,
        project_relpath="agnescode.json",
        global_scope=None,
        mirror_relpaths=(".agnescode/agnescode.json",),
        requires_absolutize=True,
        custom_writer=False,
    ),
    acp=AcpFacet(plugin=_NAME),
)


def is_available() -> tuple[bool, str]:
    path = _resolve_executable()
    if path is None:
        return False, "agnescode 未安装；headless 与 ACP 通道不可用"
    return True, path


def run_static_degraded(root: Path | None = None) -> dict:
    available, availability_detail = is_available()
    return {
        "plugin": _NAME,
        "schema_version": 1,
        "success": available,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": "static_facts_only" if available else "agnescode_not_installed",
        "probe_mode": "static_degraded",
        "headless_available": available,
        "native_task_available": bool(available and PLUGIN.engine_run.native_task),
        "identity": {
            "executable": PLUGIN.identity.executable,
            "aliases": list(PLUGIN.identity.aliases),
            "markers": list(PLUGIN.identity.markers),
        },
        "agent_entry": PLUGIN.install.path,
        "mcp_project_relpath": PLUGIN.mcp_write.project_relpath,
        "mcp_global_scope": PLUGIN.mcp_write.global_scope,
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "project_root": str(root) if root else None,
        },
        "metadata": {
            "degraded_mode": "static_plugin_facts_only",
            "acp": "ready（agnescode acp 原生支持标准 ACP，见 core/host_acp/agnescode.py）",
            "fallback": "宿主 CLI 缺失时由外层宿主/用户决策接管；BugX 不代装宿主 CLI",
        },
    }
