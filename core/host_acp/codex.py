#!/usr/bin/env python3
"""core/host_acp/codex.py - Codex 的 ACP 状态声明（宿主产品无 ACP 面）。

事实（2026-10-22 真机审计 + 2026-10-23 复核，``codex-cli 0.163.0``）：

* 顶层 ``codex --help`` 的子命令只有 ``exec`` / ``mcp`` / ``app-server`` /
  ``exec-server`` / ``agents`` / ``remote-control`` 等，**没有** ``acp``，也没有
  ``--acp`` flag；``codex exec --help`` 同样只有无头 exec 面。
* 因此本插件声明 ``status = "unavailable"``：这不是「本仓还没适配」，而是
  **上游没有该协议面**。探针据此报 ``BLOCKED(adapter_status=unavailable)``，并把
  Codex 的受支持通道指向无头 ``codex exec``（argv 与仓库门禁见
  ``core/hosts/codex.py`` 的 ``engine_run`` 面）。

上游一旦发布 ACP 入口：把 ``status`` 改成 ``ready`` 并实现 ``acp_argv`` 即可，
探针代码一行都不用改（连同 ``core/hosts/codex.py`` 的 ``acp`` 面一起核对）。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .base import AcpPlugin, McpServersBuilder

__all__ = ["CodexAcpPlugin", "PLUGIN"]

#: 宿主名由模块名派生（``core/host_acp/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]


class CodexAcpPlugin(AcpPlugin):
    """Codex 的 ACP 面：上游不存在，显式声明为 ``unavailable``。"""

    name = _HOST_NAME
    executable = _HOST_NAME
    status = "unavailable"
    #: 原因进探针报告；写版本号与实测入口，便于日后判断「上游是否已变」。
    reason = (
        "codex-cli 0.163.0 的 --help 无 acp 子命令/--acp flag，"
        "宿主产品本身没有 ACP 入口；受支持通道是无头 codex exec"
    )

    def headless_probe_argv(self, argv: list[str]) -> list[str]:
        """Request Codex's structured JSONL stream for probes only.

        Production Worker argv remains owned by ``core/hosts/codex.py`` and is
        returned unchanged by this method's caller.
        """
        command = list(argv)
        if len(command) < 2 or command[1] != "exec":
            raise ValueError("Codex headless probe expected an exec command")
        if "--json" not in command[2:]:
            command.insert(2, "--json")
        return command

    def prepare_isolation(
        self,
        root: Path,
        keep: set[str] | None,
        *,
        servers: McpServersBuilder,
    ) -> tuple[dict[str, str], Path | None, dict[str, Any]]:
        """为 headless fallback 建立临时 Git 根和隔离的 Codex home。

        Codex 对未信任项目会静默忽略项目级 ``.codex/config.toml``。信任只写到
        探活容器内的新 home；用户 config 不复制、不修改。凭据仅复制 ``auth.json``
        到容器兄弟目录，父容器是 0700 且默认在探活结束后整体删除。
        """
        del keep, servers
        root = Path(root).resolve()
        home = root.parent / "host-home"
        try:
            if home.exists() or home.is_symlink():
                return {}, None, {"mode": "unavailable", "reason": "home_path_exists"}
            home.mkdir(mode=0o700)
            os.chmod(home, 0o700)

            configured_home = os.environ.get("CODEX_HOME")
            real_home = (
                Path(configured_home).expanduser()
                if configured_home else Path.home() / ".codex"
            ).resolve(strict=False)
            auth_src = real_home / "auth.json"
            if auth_src.is_symlink():
                return {}, None, {"mode": "unavailable", "reason": "auth_is_symlink"}
            if not auth_src.is_file():
                return {}, None, {"mode": "unavailable", "reason": "auth_missing"}

            max_auth_bytes = 1024 * 1024
            src_fd = os.open(
                str(auth_src), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(src_fd, "rb") as source:
                source_stat = os.fstat(source.fileno())
                if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size > max_auth_bytes:
                    return {}, None, {
                        "mode": "unavailable", "reason": "auth_not_regular_or_too_large",
                    }
                auth_bytes = source.read(max_auth_bytes + 1)
            if len(auth_bytes) > max_auth_bytes:
                return {}, None, {"mode": "unavailable", "reason": "auth_too_large"}
            auth_dest = home / "auth.json"
            dst_fd = os.open(
                str(auth_dest),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(dst_fd, "wb") as target:
                target.write(auth_bytes)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(auth_dest, 0o600)

            import core.hosts._mcp_toml as mcp_toml  # noqa: PLC0415 - share the Codex TOML primitive

            config_text, _ = mcp_toml.set_table_key(
                "", ("projects", str(root)), "trust_level", '"trusted"',
            )
            parsed = tomllib.loads(config_text)
            projects = parsed.get("projects")
            trust = projects.get(str(root)) if isinstance(projects, dict) else None
            if not isinstance(trust, dict) or trust.get("trust_level") != "trusted":
                return {}, None, {"mode": "unavailable", "reason": "trust_readback_failed"}
            config_path = home / "config.toml"
            config_path.write_text(config_text, encoding="utf-8")
            os.chmod(config_path, 0o600)

            git = shutil.which("git")
            if not git:
                return {}, None, {"mode": "unavailable", "reason": "git_missing"}
            git_template = root.parent / "git-template"
            git_template.mkdir(mode=0o700)
            os.chmod(git_template, 0o700)
            # Git init/commit must not import the user's template hooks, system config,
            # or inherited GIT_* overrides into a probe which is meant to be disposable.
            git_env = {
                key: value for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            git_env.update({
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TEMPLATE_DIR": str(git_template),
            })
            git_steps = (
                ("init", "-q", "-b", "main"),
                ("config", "--local", "user.email", "probe@bugx.invalid"),
                ("config", "--local", "user.name", "bugx-probe"),
                ("commit", "--allow-empty", "-q", "-m", "bugx-headless-probe"),
            )
            for args in git_steps:
                result = subprocess.run(
                    [git, *args], cwd=root, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=10, check=False,
                    env=git_env,
                )
                if result.returncode != 0:
                    return {}, None, {
                        "mode": "unavailable", "reason": f"git_{args[0]}_failed",
                    }
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return {}, None, {
                "mode": "unavailable",
                "reason": f"isolation_setup_failed:{type(exc).__name__}",
            }

        return {"CODEX_HOME": str(home)}, None, {
            "mode": "isolated_codex_home",
            "codex_home": str(home),
            "auth": "copied",
            "trust": "trusted",
            "git": "initialized_with_empty_commit",
        }

    def ensure_probe_servers(
        self,
        project_root: Path,
        config_paths: tuple[Path, ...],
        servers: dict[str, dict[str, Any]],
    ) -> list[str]:
        """在临时项目 TOML 内重建 BugX MCP 根路径，不沿用源项目绝对路径。"""
        import core.hosts._mcp_toml as mcp_toml  # noqa: PLC0415
        from core.hosts import codex as host_plugin

        project_root = Path(project_root).resolve()
        toml_servers = {
            name: {
                key: spec[key]
                for key in ("command", "args", "env")
                if key in spec
            }
            for name, spec in servers.items()
        }
        injected: list[str] = []
        for path in config_paths:
            try:
                resolved = path.resolve(strict=False)
            except OSError:
                continue
            if (
                project_root not in resolved.parents
                or path.is_symlink()
                or not resolved.is_file()
            ):
                continue
            text = resolved.read_text(encoding="utf-8")
            new_text, added, updated = mcp_toml.upsert_servers(
                text,
                toml_servers,
                owned=host_plugin._is_bugx_entry,
                banner=host_plugin._MCP_BANNER,
            )
            changed = [*added, *updated]
            if not changed:
                continue
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent,
            )
            tmp = Path(tmp_name)
            mode = resolved.stat().st_mode & 0o777
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(new_text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, resolved)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            injected.extend(changed)
        return injected


#: 注册表按类注册（``_register`` 自己实例化），与 ``opencode``/``cline`` 同形。
PLUGIN = CodexAcpPlugin
