"""Install the native Antigravity plugin for defensive code review."""

from __future__ import annotations

import json
from pathlib import Path

from core.hosts.antigravity import (
    AGENT, LOGO, MANIFEST, MCP, PLUGIN_DIR, PROJECT_MCP, SKILL, WORKER_AGENT,
)

from .. import __version__
from ..common import CliError
from ..infra.engine_installer import asset_root, global_engine_root
from ..infra.security import _assert_no_symlink_components


_LEGACY_MCP = json.dumps({"mcpServers": {"docs": {
    "command": "bugx", "args": ["mcp", "docs", "serve", "--root", "."],
}}}, indent=2) + "\n"

# Exact payloads emitted by the first Antigravity adapter revision.  They are
# accepted only as an upgrade input; arbitrary edits to a user's plugin still
# fail closed instead of being silently overwritten.
_LEGACY_FILES = {
    AGENT: """---
name: bugx-review
description: Review source changes and identify defects in an authorized workspace.
mainAgent: true
subagent: false
inheritMcp: false
inheritCustomizations: false
commandExecutionPolicy: 'off'
skills:
  - skills/bugx-review
---

You are BugX Review. Use the BugX Review skill to inspect the requested workspace scope. Use the plugin's documentation search when the repository's documentation is relevant. Report findings with source locations, impact, and concrete remediation.
""",
    SKILL: """---
name: bugx-review
description: Review authorized source changes for correctness, maintainability, and defensive security.
---

Identify the requested files and the expected behavior. Inspect those files and their immediate callers. Report each defect with an exact source location, its impact, and a remediation. Distinguish source evidence from assumptions and state any verification gaps. This is a read-only review.
""",
    MANIFEST: json.dumps({
        "name": "bugx-review", "version": __version__,
        "description": "BugX defensive code review and local documentation search.",
    }, indent=2) + "\n",
}

_LEGACY_AGENT_V2 = """---
name: bugx-review
description: Review source changes and identify defects in an authorized workspace.
mainAgent: true
subagent: false
inheritMcp: true
inheritCustomizations: false
commandExecutionPolicy: 'off'
skills:
  - skills/bugx-review
---

You are BugX Review. Before doing work, read `bugx engine path --name bugx-runtime-contract.md` and `bugx engine path --name bugx-quick.md`.
Use the inherited workspace-scoped BugX MCP servers. Use `bugx-docs` for project documentation and `bugx-supervisor` for lifecycle, evidence, checkpoints, findings, and final reporting. Never treat model text, a generated prompt, or a local CLI printout as lifecycle evidence. Report findings with source locations, impact, and concrete remediation.

`bugx-docs` is project-scoped: every `resolve_library`, `search_docs`, or `fetch_doc` call MUST include `project_id`. Unless the server instructions explicitly advertise another value, use the current workspace directory name (the scope used by `bugx mcp docs serve`) and never omit the field.
"""

# Exact payload emitted by the immediately preceding managed revision. It is
# accepted only for this one-way rename so an existing BugX installation can
# be upgraded from the lowercase identifier without weakening ownership
# checks for arbitrary user edits.
_LEGACY_AGENT_BUGX = """---
name: bugx
description: Review source changes and identify defects in an authorized workspace.
mainAgent: true
subagent: false
inheritMcp: true
inheritCustomizations: false
commandExecutionPolicy: 'off'
skills:
  - skills/bugx-review
---

You are BugX Review. Before doing work, read `bugx engine path --name bugx-runtime-contract.md` and `bugx engine path --name bugx-quick.md`.
Use the inherited workspace-scoped BugX MCP servers. Use `bugx-docs` for project documentation and `bugx-supervisor` for lifecycle, evidence, checkpoints, findings, and final reporting. Never treat model text, a generated prompt, or a local CLI printout as lifecycle evidence. Report findings with source locations, impact, and concrete remediation.

`bugx-docs` is project-scoped: every `resolve_library`, `search_docs`, or `fetch_doc` call MUST include `project_id`. Unless the server instructions explicitly advertise another value, use the current workspace directory name (the scope used by `bugx mcp docs serve`) and never omit the field.
"""

_LEGACY_MANIFEST_WITH_LOGO = json.dumps({
    "name": "bugx-review", "version": __version__,
    "logo": LOGO,
    "description": "BugX defensive code review and local documentation search.",
}, indent=2) + "\n"

# The old relative MCP config could start the docs server from the plugin
# directory, leaving only these BugX-owned SQLite artifacts behind.  Keep them
# readable during upgrade (they are no longer loaded); unknown files remain a
# hard failure.
_LEGACY_GENERATED = (
    Path(".bugx/knowledge/docs.sqlite3"),
    Path(".bugx/knowledge/docs.sqlite3-shm"),
    Path(".bugx/knowledge/docs.sqlite3-wal"),
)


_FILES = {
    AGENT: """---
name: BugX
description: Review source changes and identify defects in an authorized workspace.
mainAgent: true
subagent: false
inheritMcp: true
inheritCustomizations: false
commandExecutionPolicy: 'off'
skills:
  - skills/bugx-review
---

You are BugX Review. Before doing work, read `bugx engine path --name bugx-runtime-contract.md` and `bugx engine path --name bugx-quick.md`.
Use the inherited workspace-scoped BugX MCP servers. Use `bugx-docs` for project documentation and `bugx-supervisor` for lifecycle, evidence, checkpoints, findings, and final reporting. Never treat model text, a generated prompt, or a local CLI printout as lifecycle evidence. Report findings with source locations, impact, and concrete remediation.

`bugx-docs` is project-scoped: every `resolve_library`, `search_docs`, or `fetch_doc` call MUST include `project_id`. Unless the server instructions explicitly advertise another value, use the current workspace directory name (the scope used by `bugx mcp docs serve`) and never omit the field.
""",
    WORKER_AGENT: """---
name: bugx-worker
description: Execute one bounded BugX review claim and return evidence to the Supervisor.
mainAgent: false
subagent: true
inheritMcp: true
inheritCustomizations: false
commandExecutionPolicy: 'off'
skills:
  - skills/bugx-review
---

You are the BugX Worker. Read `bugx engine path --name bugx-worker.md` and `bugx engine path --name bugx-runtime-contract.md` first. Work only on the assigned claim, use `bugx-supervisor-worker` for claim, heartbeat, checkpoint, and finding calls, and never invent a checkpoint or finding. Return bounded evidence and a clear terminal status.
""",
    SKILL: """---
name: bugx-review
description: Review authorized source changes for correctness, maintainability, and defensive security.
---

Identify the requested files and the expected behavior. Inspect those files and their immediate callers. Report each defect with an exact source location, its impact, and a remediation. Distinguish source evidence from assumptions and state any verification gaps. This is a read-only review. When using `bugx-docs`, always include the current workspace directory name as the required `project_id` unless the server advertises a different exact scope.
""",
    # Project-bound MCP is written to ``<root>/.agents/mcp_config.json`` by
    # the generic host writer.  The plugin-level file must not start a server
    # against the plugin/app CWD, so it is intentionally an empty container.
    MCP: json.dumps({"mcpServers": {}}, indent=2) + "\n",
    MANIFEST: json.dumps({
        "name": "bugx-review", "displayName": "BugX", "version": __version__, "logo": LOGO,
        "description": "BugX defensive code review and local documentation search.",
    }, indent=2) + "\n",
}


def _logo_source() -> Path:
    """Return the packaged official BugX avatar used by the plugin manifest."""

    candidates = (
        asset_root() / "assets" / "bugx-avatar.svg",
        global_engine_root() / "assets" / "bugx-avatar.svg",
        Path(__file__).resolve().parents[4] / "core" / "assets" / "bugx-avatar.svg",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    raise CliError("找不到 BugX 官方头像资源 bugx-avatar.svg，拒绝安装 Antigravity 插件。")


def _owned_files() -> dict[str, bytes | str]:
    return {**_FILES, LOGO: _logo_source().read_bytes()}


def pre_validate() -> None:
    dest = Path.home() / PLUGIN_DIR
    _assert_no_symlink_components(dest)
    owned = _owned_files()
    paths = {dest / relative for relative in owned}
    paths.update(dest / relative for relative in _LEGACY_GENERATED)
    directories = {dest} | {parent for path in paths for parent in path.parents if dest in parent.parents}
    allowed = paths | directories
    for folder in directories:
        _assert_no_symlink_components(folder)
        if folder.exists() and (
            not folder.is_dir() or any(path not in allowed for path in folder.iterdir())
        ):
            raise CliError(f"Antigravity 插件目录含有未识别的内容，拒绝覆盖: {folder}")
    for relative, content in owned.items():
        path = dest / relative
        _assert_no_symlink_components(path)
        if not path.exists():
            continue
        if not path.is_file():
            raise CliError(f"Antigravity 插件路径不是普通文件，拒绝覆盖: {path}")
        current = path.read_bytes()
        expected = content if isinstance(content, bytes) else content.encode("utf-8")
        if current != expected:
            # This is the exact MCP file emitted by the previous BugX build.
            # It is accepted only to be replaced during this upgrade; any
            # other foreign content remains a hard failure.
            if relative == LOGO:
                legacy_contents = ()
            elif relative == MCP:
                legacy_contents = (_LEGACY_MCP,)
            else:
                legacy_contents = tuple(
                    value for value in (_LEGACY_FILES.get(relative),
                                        _LEGACY_AGENT_V2 if relative == AGENT else None,
                                        _LEGACY_AGENT_BUGX if relative == AGENT else None,
                                        _LEGACY_MANIFEST_WITH_LOGO if relative == MANIFEST else None)
                    if value is not None
                )
            if not any(
                current == (value if isinstance(value, bytes) else value.encode("utf-8"))
                for value in legacy_contents
            ):
                raise CliError(f"Antigravity 插件路径含有未识别的内容，拒绝覆盖: {path}")


def install_review_plugin() -> dict[str, object]:
    from .agent import _atomic_write_global_file_tx

    pre_validate()
    dest = Path.home() / PLUGIN_DIR
    written = []
    changed_any = False
    for relative, content in _owned_files().items():
        path = dest / relative
        payload = content if isinstance(content, bytes) else content.encode("utf-8")
        created = not path.exists()
        changed = created or path.read_bytes() != payload
        if changed:
            _atomic_write_global_file_tx(path, payload)
            changed_any = True
        written.append({
            "path": str(path),
            "status": "created" if created else "updated" if changed else "unchanged",
            "backup": None, "created": created,
        })
    return {
        "status": "updated" if changed_any else "unchanged",
        "dir": str(dest), "written": written, "backups": [],
    }


def _validate_workspace_mcp(root: Path) -> tuple[bool, str]:
    target = root.resolve() / PROJECT_MCP
    if target.is_symlink() or not target.is_file():
        return False, f"缺少项目级 Antigravity MCP 配置: {target}"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"项目级 Antigravity MCP 配置无法解析: {target}: {exc}"
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    required = {"bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"}
    if not isinstance(servers, dict) or not required.issubset(servers):
        return False, "项目级 Antigravity MCP 缺少 bugx-supervisor / worker / docs 双通道"
    root_resolved = root.resolve()
    root_text = str(root_resolved)
    expected_db = str(root_resolved / ".bugx" / "knowledge" / "docs.sqlite3")
    for name in required:
        entry = servers.get(name)
        command = entry.get("command") if isinstance(entry, dict) else None
        command_name = Path(str(command)).name.casefold() if isinstance(command, str) else ""
        if not isinstance(entry, dict) or entry.get("type") != "stdio":
            return False, f"项目级 Antigravity MCP 条目不是 BugX stdio 配置: {name}"
        args = entry.get("args") if isinstance(entry, dict) else None
        if not isinstance(args, list) or "--root" not in args:
            return False, f"项目级 Antigravity MCP 条目缺少绝对 --root: {name}"
        if command_name in {"python", "python.exe", "python3", "python3.exe", "pypy", "pypy.exe"}:
            expected_script = "bugx_docs_mcp_server.py" if name == "bugx-docs" else "bugx_mcp_server.py"
            if len(args) < 2 or Path(str(args[0])).name.casefold() != expected_script or args[1] != "serve":
                return False, f"项目级 Antigravity Python MCP 条目启动器不受支持: {name}"
            script = Path(str(args[0])).expanduser()
            trusted_scripts = {
                (global_engine_root() / "skills" / expected_script).resolve(),
                (asset_root() / "skills" / expected_script).resolve(),
            }
            try:
                if script.resolve() not in trusted_scripts:
                    return False, f"项目级 Antigravity MCP 脚本不属于 BugX 引擎: {name}"
            except OSError:
                return False, f"项目级 Antigravity MCP 脚本路径无效: {name}"
        elif command_name not in {"bugx", "bugx.exe", "bugx.cmd", "bugx.ps1"}:
            return False, f"项目级 Antigravity MCP 条目不是 BugX stdio 配置: {name}"
        try:
            root_arg = str(args[args.index("--root") + 1])
        except (ValueError, IndexError):
            return False, f"项目级 Antigravity MCP 条目 --root 无值: {name}"
        if root_arg != root_text:
            return False, f"项目级 Antigravity MCP 根目录不匹配: {name} -> {root_arg}"
        if name == "bugx-docs":
            if "--db" not in args:
                return False, "项目级 Antigravity bugx-docs 条目缺少 --db"
            try:
                db_arg = str(args[args.index("--db") + 1])
            except (ValueError, IndexError):
                return False, "项目级 Antigravity bugx-docs 条目 --db 无值"
            if db_arg != expected_db:
                return False, f"项目级 Antigravity docs 数据库不匹配: {db_arg}"
        elif name == "bugx-supervisor":
            if "--role" not in args or args[args.index("--role") + 1:args.index("--role") + 2] != ["host"]:
                return False, "项目级 Antigravity supervisor 条目必须使用 --role host"
        elif name == "bugx-supervisor-worker":
            if "--role" not in args or args[args.index("--role") + 1:args.index("--role") + 2] != ["worker"]:
                return False, "项目级 Antigravity worker 条目必须使用 --role worker"
    return True, f"Antigravity 项目级 MCP 已绑定当前工作区: {target}"


def validate_install(root: Path) -> tuple[bool, str]:
    dest = Path.home() / PLUGIN_DIR
    try:
        pre_validate()
    except (OSError, CliError) as exc:
        return False, str(exc)
    if any(not (dest / relative).is_file() for relative in _owned_files()):
        return False, "BugX Agent 安装不完整，请运行 bugx install --agent antigravity。"
    mcp_ok, mcp_detail = _validate_workspace_mcp(root)
    if not mcp_ok:
        return False, mcp_detail + "；请运行 bugx install --agent antigravity"
    return True, f"Antigravity BugX Agent、Worker、Skill 和项目 MCP 已注册：{dest}"


def user_agent_surfaces() -> list[dict[str, object]]:
    return [{"path": Path.home() / PLUGIN_DIR, "entries": tuple(_owned_files()), "label": "Google Antigravity"}]


def self_remove_user_agents(
    backup_root: Path | None, removed: list[str], preserved: list[str],
) -> None:
    from .lifecycle import _backup_tree_external

    dest = Path.home() / PLUGIN_DIR
    if not dest.exists() and not dest.is_symlink():
        return
    try:
        pre_validate()
        paths = [dest / relative for relative in reversed(_owned_files()) if (dest / relative).is_file()]
        if backup_root is not None:
            for path in paths:
                _backup_tree_external(path, backup_root / "antigravity-bugx-review" / path.relative_to(dest))
        for path in paths:
            path.unlink()
            removed.append(str(path))
    except (CliError, OSError) as exc:
        preserved.append(f"{dest}（未清理: {exc}）")
