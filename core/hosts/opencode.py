#!/usr/bin/env python3
"""core/hosts/opencode.py - OpenCode 宿主插件及自治配置环境。

事实来源：
* identity  ← ``core/host_registry.HOST_MARKERS["opencode"]``（``opencode.json``）
* install   ← ``src/bugx_cli/hosts/registry.HOST_COMPONENTS``（agent/shared-symlink）
* engine    ← OpenCode v2 CLI（``opencode run --standalone --agent … --format json --auto``）
* mcp_write ← ``src/bugx_cli/commands/plugin.py:_host_project_mcp_config_target``（``mcp.servers`` 键）
* acp       ← ``core/host_acp/opencode.py``（``opencode acp``，无 ``--dir``/``--model``）

自治子进程使用私有的用户级配置副本：保留 provider/权限设置和项目级插件/MCP，
避免把无关的用户级插件/MCP 自动带入 BugX 生命周期。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import Iterator

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

__all__ = ["PLUGIN"]

_NAME = "opencode"


def _configured_model(root: Path) -> str:
    """Read the project model without making it part of BugX state."""
    configured = os.environ.get("BUGX_OPENCODE_MODEL", "").strip()
    if configured:
        return configured
    try:
        payload = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    model = payload.get("model") if isinstance(payload, dict) else ""
    return model.strip() if isinstance(model, str) else ""


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
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _read_opencode_config(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(
            _strip_jsonc_comments_and_trailing_commas(path.read_text(encoding="utf-8")),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"OpenCode 自治配置无法安全隔离: {path.name} ({type(exc).__name__})"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"OpenCode 自治配置必须是对象: {path.name}")
    return parsed


def _child_path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _open_code_global_config_dir(root: Path) -> Path:
    configured = os.environ.get("OPENCODE_CONFIG_DIR")
    if "OPENCODE_CONFIG_DIR" in os.environ:
        if not configured:
            raise RuntimeError("OPENCODE_CONFIG_DIR 为空，无法隔离 OpenCode 用户配置")
        return _child_path(configured, root)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = _child_path(xdg, root) if xdg else Path.home() / ".config"
    return (base / "opencode").resolve()


@contextmanager
def _autonomous_environment(root: Path) -> Iterator[dict[str, str | None]]:
    """Hide user-global plugin/MCP sources while preserving OpenCode's project config.

    OpenCode v2 loads configured plugins and MCPs from merged system, global, and
    project sources. ``OPENCODE_CONFIG_DIR`` changes only the global config root;
    project config, OpenCode data/auth, and managed system policy remain available.
    """
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT"):
        if os.environ.get(name):
            raise RuntimeError(
                f"OpenCode 自治配置隔离不接受额外覆盖 {name}；请先移除该覆盖再运行"
            )
    for name in ("OPENCODE_CONFIG_PROJECT_DISABLE", "OPENCODE_DISABLE_PROJECT_CONFIG"):
        value = os.environ.get(name, "")
        if value == "1" or value.lower() == "true":
            raise RuntimeError(f"{name} 禁用了项目配置，BugX MCP 无法加载")

    # 函数级导入：tempfile 会拉起 shutil，撞上适配器导入纯净性门禁
    # （test_import_pulls_no_engine_or_cli_or_fs_modules）；仅此路径用到。
    import tempfile

    source_dir = _open_code_global_config_dir(root)
    parent = source_dir.parent if source_dir.parent.is_dir() else Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="bugx-opencode-config-", dir=parent) as private_name:
        private_dir = Path(private_name).resolve()
        project_root = root.resolve()
        if private_dir == project_root or project_root in private_dir.parents:
            raise RuntimeError("OpenCode 自治私有配置目录落在被测项目内，已阻断")
        private_dir.chmod(0o700)

        if source_dir.exists():
            if not source_dir.is_dir():
                raise RuntimeError("OPENCODE_CONFIG_DIR 不是目录，无法安全隔离")
            try:
                for entry in source_dir.iterdir():
                    if entry.name in {"opencode.json", "opencode.jsonc", "plugin", "plugins"}:
                        continue
                    (private_dir / entry.name).symlink_to(
                        entry, target_is_directory=entry.is_dir(),
                    )
            except OSError as exc:
                raise RuntimeError(
                    f"OpenCode 自治配置资产无法隔离 ({type(exc).__name__})"
                ) from exc

            for name in ("opencode.json", "opencode.jsonc"):
                source_file = source_dir / name
                if not source_file.exists() and not source_file.is_symlink():
                    continue
                if not source_file.is_file():
                    raise RuntimeError(f"OpenCode 用户配置不是文件: {name}")
                payload = _read_opencode_config(source_file)
                for key in ("mcp", "plugin", "plugins"):
                    payload.pop(key, None)
                target = private_dir / name
                target.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                target.chmod(0o600)

        yield {
            "OPENCODE_CONFIG_DIR": str(private_dir),
            # Reject above, then remove empty inherited overrides so they cannot
            # shadow the private directory or the target project's config.
            "OPENCODE_CONFIG": None,
            "OPENCODE_CONFIG_CONTENT": None,
        }


def _argv(ctx: RunContext, *, auto: bool) -> list[str]:
    # 工作区绑定只由调用方的 ``subprocess(cwd=root)`` 提供：``opencode run`` 没有
    # ``--dir`` flag，拼上会以退出码 1 死掉（build 179 实测）。root 只用于读项目模型。
    cmd = ["opencode", "run"]
    if auto:
        # Autonomous runs must not attach to a pre-existing user service whose
        # project instance can resolve shell work outside the requested root.
        cmd.append("--standalone")
    cmd += ["--agent", ctx.agent, "--format", "json"]
    model = _configured_model(ctx.root)
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
    """OpenCode 的一次性无头侦察：``opencode run --format json -f <prompt>``。"""
    _ = root  # root 由调用方 cwd 承担
    cmd = ["opencode", "run", "--standalone", "--format", "json", "--auto"]
    scout_model = (
        os.environ.get("BUGX_SCOUT_MODEL")
        or os.environ.get("BUGX_OPENCODE_MODEL")
        or os.environ.get("BUGX_SMOKE_MODEL")
        or os.environ.get("OPENCODE_MODEL")
    )
    if scout_model:
        cmd.extend(["-m", scout_model])
    cmd.extend(["-f", str(prompt_file), "Execute BugX Recon Scout review"])
    return cmd


def _mcp_config_paths(root: Path) -> tuple[Path, ...]:
    return (root / "opencode.json",)


def _mcp_health(name: str, cfg: dict, root: Path | None = None) -> bool:
    del name, cfg, root  # bugx-supervisor 是宿主加载的 stdio MCP，恒健康
    return True


PLUGIN = HostPlugin(
    identity=IdentityFacet(
        name=_NAME,
        label="OpenCode",
        order=0,
        executable="opencode",
        markers=("opencode.json",),
        aliases=(),
        app_markers=None,
        native_question=True,
    ),
    status=STATUS_READY,
    install=InstallFacet(
        kind="agent",
        path=".opencode/agents/bugx.md",
        marker=".opencode",
        entry_mode="shared-symlink",
        required_paths=(),
        legacy_paths=(),
        capabilities=frozenset({"project-mcp-config", "global-agent-link"}),
        interaction={
            "tool": "question",
            "single_select": "multiple=false",
        },
        mcp_relpaths=("opencode.json", ".opencode/opencode.json"),
        # HostComponent 未声明 has_worker_subagent（worker 入口由 agent 面单独链接），
        # 本切片按权威声明取 False，避免凭空扩大安装面。
        worker_subagent=False,
    ),
    engine_run=EngineRunFacet(
        supports_headless=True,
        headless_argv=_headless_argv,
        autonomous_argv=_autonomous_argv,
        mcp_config_paths=_mcp_config_paths,
        mcp_health=_mcp_health,
        host_surface="tui",
        # Recon Scout：``opencode run --format json -f <prompt_file> <message>``。
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
        project_relpath="opencode.json",
        global_scope=None,
        mirror_relpaths=(".opencode/opencode.json",),
        requires_absolutize=True,
        # opencode.json 由 ``_ensure_opencode_config`` 专用写入器合并
        # （v2 plugins / mcp.servers / pinned 命令），通用 JSON 骨架不得接管。
        custom_writer=True,
    ),
    acp=AcpFacet(plugin=_NAME),
)
