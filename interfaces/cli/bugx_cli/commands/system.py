from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from .. import __build_id__, __version__
from ..common import *
from ..infra import _abandoned_mcp_processes, _layout_module
from ..infra.engine_installer import (
    MissingAgentAssetError,
    _engine_build_id,
    _engine_install_lock,
    _engine_self_integrity,
    resolve_engine_file,
)
from ..infra.security import _assert_no_symlink_components
from .cloud import _supervisor_deploy, _supervisor_status


_COMPLETION_CHILDREN = {
    "auth": ("login", "status", "set-key", "logout"),
    "cloud": (
        "config", "status", "log-report", "feedback", "sync", "export",
        "upload", "conflicts", "projects", "devices", "onboard",
    ),
    "completion": ("bash", "zsh", "fish"),
    "hosts": ("--json", "--root"),
    "install": ("--agent", "--allow-missing", "--force", "--json", "--non-interactive", "--root"),
    "autonomous": ("--agent-timeout", "--host", "--json", "--max-turns", "--max-wall-time", "--root"),
    "skill": ("list", "status", "validate", "rollback"),
    "update": ("--allow-downgrade", "--channel", "--check", "--force", "--repair-assets", "--root"),
}

_ASSET_REPAIR_ATTEMPT_ENV = "BUGX_ASSET_REPAIR_ATTEMPTED"
# Stable package recovery is a separate trust path from the user's configurable
# knowledge/sync endpoint. Keep it pinned to the production release service.
_STABLE_AGENT_RELEASE_BASE = "https://bugx.dev"
_STABLE_AGENT_PYPI_JSON = "https://pypi.org/pypi/bugx-cli/json"
_STABLE_AGENT_PYPI_HOST = "pypi.org"
_STABLE_AGENT_FILES_HOST = "files.pythonhosted.org"
_MAX_PYPI_METADATA_BYTES = 4 * 1024 * 1024
_MAX_PYPI_WHEEL_BYTES = 128 * 1024 * 1024


def command_completion(args: argparse.Namespace) -> int:
    """Print a small, dependency-free shell completion script."""
    commands = tuple(getattr(args, "completion_commands", ()))
    if not commands:
        commands = (
            "auth", "autonomous", "cloud", "completion", "daemon", "doctor", "dsh", "help", "hosts",
            "init", "install", "knowledge", "lesson", "log", "logic-analysis",
            "mcp", "mcp-runtime", "mining-audit", "patch-forge", "plugin", "probe",
            "project", "reset", "self-audit", "skill", "stats", "supervisor", "test-audit",
            "test-generate", "test-verify", "uninstall", "update",
        )
    words = " ".join(commands)
    children = {name: " ".join(values) for name, values in _COMPLETION_CHILDREN.items()}
    shell = args.shell
    if shell == "bash":
        print(f'''_bugx_complete() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  local cmd="${{COMP_WORDS[1]:-}}"
  local words="{words}"
  if [[ "$COMP_CWORD" -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "$words" -- "$cur") )
    return
  fi
  case "$cmd" in
    auth) words="{children['auth']}" ;;
    cloud) words="{children['cloud']}" ;;
    completion) words="{children['completion']}" ;;
    hosts) words="{children['hosts']}" ;;
    install) words="{children['install']}" ;;
    autonomous) words="{children['autonomous']}" ;;
    skill) words="{children['skill']}" ;;
    update) words="{children['update']}" ;;
    *) return ;;
  esac
  COMPREPLY=( $(compgen -W "$words" -- "$cur") )
}}
complete -F _bugx_complete bugx''')
    elif shell == "zsh":
        command_items = " ".join(f"'{item}'" for item in commands)
        print(f'''#compdef bugx
_bugx() {{
  local -a commands
  commands=({command_items})
  _arguments '1:command:->command' '*::arg:->args'
  case $state in
    command) _describe 'command' commands ;;
    args)
      case $words[2] in
        auth) _describe 'auth action' ({children['auth'].replace(' ', ' ')}) ;;
        cloud) _describe 'cloud command' ({children['cloud']}) ;;
        completion) _describe 'shell' ({children['completion']}) ;;
        hosts) _describe 'option' ({children['hosts']}) ;;
        install) _describe 'option' ({children['install']}) ;;
        autonomous) _describe 'option' ({children['autonomous']}) ;;
        skill) _describe 'skill action' ({children['skill']}) ;;
        update) _describe 'option' ({children['update']}) ;;
      esac
    ;;
  esac
}}
_bugx''')
    elif shell == "fish":
        print(f'''complete -c bugx -f -n '__fish_use_subcommand' -a '{words}'
complete -c bugx -f -n '__fish_seen_subcommand_from auth' -a '{children['auth']}'
complete -c bugx -f -n '__fish_seen_subcommand_from cloud' -a '{children['cloud']}'
complete -c bugx -f -n '__fish_seen_subcommand_from completion' -a '{children['completion']}'
complete -c bugx -f -n '__fish_seen_subcommand_from hosts' -a '{children['hosts']}'
complete -c bugx -f -n '__fish_seen_subcommand_from install' -a '{children['install']}'
complete -c bugx -f -n '__fish_seen_subcommand_from autonomous' -a '{children['autonomous']}'
complete -c bugx -f -n '__fish_seen_subcommand_from skill' -a '{children['skill']}'
complete -c bugx -f -n '__fish_seen_subcommand_from update' -a '{children['update']}' ''')
    else:  # argparse choices make this unreachable; keep the handler safe for embedding.
        raise CliError(f"不支持的 shell: {shell}")
    return 0


def _print_version_tls_hint(error: object) -> None:
    text = str(error)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text.lower():
        print(
            "[update] TLS 证书校验失败；内网或自签 CA 可设置 "
            "BUGX_CA_BUNDLE=/path/to/ca.pem 后重试"
        )


def _update_channel(args: argparse.Namespace) -> str:
    channel = str(getattr(args, "channel", "") or "").strip()
    if channel:
        return channel
    try:
        from core.infra.client_metadata import release_channel

        return release_channel()
    except (ImportError, OSError, ValueError):
        return "stable"


def command_version_check(args: argparse.Namespace) -> int:
    """bugx update --check：向服务端查询最新版本，提示更新。

    不执行更新，只输出版本信息和建议。退出码：
    0 = 已最新或无需更新
    1 = 有更新可用
    2 = 检查失败（网络不可达等）
    """
    if not getattr(args, "check", False):
        # 仅供内部复用；公共入口是 bugx update --check。
        print(f"BugX CLI v{__version__}")
        return 0
    if os.environ.get("BUGX_DISABLE_VERSION_CHECK", "").lower() in {"1", "true", "yes"}:
        print("[update] 版本检查已由 BUGX_DISABLE_VERSION_CHECK 禁用")
        return 0

    try:
        _ensure_core_importable()
        from core.cloud_sync.cloud_client import CloudClient
    except Exception as exc:  # noqa: BLE001
        print(f"[update] 依赖加载失败: {exc}")
        return 2

    client = CloudClient()
    channel = _update_channel(args)
    try:
        result = client.check_version(current_version=__version__, channel=channel)
    except Exception as exc:  # noqa: BLE001
        print(f"[update] 版本检查失败: {exc}")
        _print_version_tls_hint(exc)
        return 2

    if not result.ok:
        # 网络不可达时静默提示，不阻断
        err = getattr(result, "error", "") or "未知错误"
        print(f"[update] 无法检查更新: {err}")
        _print_version_tls_hint(err)
        print("[update] 可使用 BUGX_DISABLE_VERSION_CHECK=1 跳过启动时检查")
        return 2

    data = result.data if isinstance(result.data, dict) else {}
    info = data.get("data") if isinstance(data.get("data"), dict) else data
    latest = str(info.get("latest_version") or "").strip()
    min_client = str(info.get("min_client_version") or "").strip()
    notes = str(info.get("release_notes") or "")
    current_version = _release_version_tuple(__version__)
    latest_version = _release_version_tuple(latest)
    min_version = _release_version_tuple(min_client) if min_client else None
    metadata_error = None
    if current_version is None or latest_version is None:
        metadata_error = f"服务端返回了无效版本号: {latest!r}"
    elif latest_version < current_version:
        metadata_error = f"服务端版本 {latest} 低于当前版本 {__version__}"
    elif min_client and min_version is None:
        metadata_error = f"服务端返回了无效最低兼容版本: {min_client!r}"
    elif min_version is not None and min_version > latest_version:
        metadata_error = (
            f"服务端版本元数据矛盾：最低兼容版本 {min_client} 高于发布版本 {latest}"
        )
    if metadata_error:
        if getattr(args, "json", False):
            payload = dict(info)
            payload["error"] = metadata_error
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[update] {metadata_error}")
        return 2

    update_available = (info.get("update_available") is True
                        or latest_version > current_version)
    update_required = (info.get("update_required") is True
                       or (min_version is not None and current_version < min_version))

    if getattr(args, "json", False):
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    print(f"当前版本: {__version__}")
    if latest:
        print(f"最新版本: {latest}")
    if min_client:
        print(f"最低兼容: {min_client}")
    if notes:
        print(f"变更说明: {notes}")

    if update_required:
        print(f"\n⚠ 当前版本 {__version__} 低于最低兼容版本 {min_client}，必须更新！")
        print("执行以下命令更新：")
        print("  bugx update")
        return 1
    if update_available:
        print(f"\n✦ 发现新版本 {latest}，建议执行：bugx update")
        return 1

    print(f"\n✓ BugX 已是最新版本 ({__version__})")
    return 0


RELEASE_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?P<pre>a|b|rc)(?P<pre_num>\d+)|\+[0-9A-Za-z.-]+)?$"
)


def _release_version_tuple(value: object) -> tuple[int, int, int, int, int] | None:
    """Parse the server release contract, including explicit prereleases.

    The update endpoint uses the compact PEP 440-style prerelease spelling
    ``X.Y.ZaN``, ``X.Y.ZbN`` or ``X.Y.ZrcN``.  A stable ``X.Y.Z`` release sorts
    after all prereleases of the same base version.  No other suffixes are
    accepted so malformed server metadata remains fail-closed.
    """
    if not isinstance(value, str):
        return None
    match = RELEASE_VERSION_RE.fullmatch(value.strip())
    if match is None:
        return None
    prerelease = match.group("pre")
    stage = {"a": 0, "b": 1, "rc": 2}.get(prerelease, 3)
    number = int(match.group("pre_num")) if prerelease else 0
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        stage,
        number,
    )


class _RejectReleaseRedirects(urllib.request.HTTPRedirectHandler):
    """Do not let release metadata redirect recovery requests to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _validate_official_pypi_url(value: object, *, expected_host: str) -> str:
    url = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise CliError("PyPI 返回了无效的 HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CliError(f"PyPI URL 不在固定 HTTPS 主机白名单内：{url[:160]}")
    return url


def _read_official_pypi_response(
    url: str,
    *,
    expected_host: str,
    accept: str,
    max_bytes: int,
) -> bytes:
    trusted_url = _validate_official_pypi_url(url, expected_host=expected_host)
    request = urllib.request.Request(
        trusted_url,
        headers={
            "Accept": accept,
            "User-Agent": f"BugX-Client/{__version__} (marker=stable; channel=stable)",
        },
    )
    opener = urllib.request.build_opener(_RejectReleaseRedirects())
    try:
        response = opener.open(request, timeout=15)
    except urllib.error.HTTPError as exc:
        try:
            status = exc.code
            if 300 <= status < 400:
                raise CliError("PyPI 响应试图重定向，已拒绝请求") from exc
            raise CliError(f"PyPI 请求失败：HTTP {status}") from exc
        finally:
            exc.close()
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"PyPI 请求失败：{type(exc).__name__}: {exc}") from exc

    try:
        final_url = str(response.geturl() or "")
        _validate_official_pypi_url(final_url, expected_host=expected_host)
        headers = getattr(response, "headers", None)
        raw_length = headers.get("Content-Length") if headers is not None else None
        if raw_length:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError) as exc:
                raise CliError("PyPI 响应 Content-Length 无效") from exc
            if content_length < 0 or content_length > max_bytes:
                raise CliError("PyPI 响应超过允许大小")
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CliError("PyPI 响应超过允许大小")
        if raw_length and len(data) != content_length:
            raise CliError("PyPI 响应长度与 Content-Length 不一致")
        return data
    finally:
        response.close()


def _select_pypi_stable_wheel(payload: object, *, current_version: str) -> dict[str, str]:
    """Select one non-yanked official pure-Python stable wheel without downgrades."""
    current_order = _release_version_tuple(current_version)
    if current_order is None or current_order[3] != 3:
        raise CliError(f"当前 CLI 版本不是可验证的 stable 版本：{current_version!r}")
    if not isinstance(payload, dict):
        raise CliError("PyPI JSON 根节点格式无效")
    info = payload.get("info")
    releases = payload.get("releases")
    if not isinstance(info, dict) or not isinstance(releases, dict):
        raise CliError("PyPI JSON 缺少 info 或 releases 对象")
    package_name = re.sub(r"[-_.]+", "-", str(info.get("name") or "").strip().lower())
    if package_name != "bugx-cli":
        raise CliError(f"PyPI 返回了错误的发行名：{package_name or '未知'}")

    candidates: list[tuple[tuple[int, int, int, int, int], dict[str, str]]] = []
    for version, files in releases.items():
        version_text = str(version)
        order = _release_version_tuple(version_text)
        if (
            not re.fullmatch(r"\d+\.\d+\.\d+", version_text)
            or order is None
            or order[3] != 3
            or order < current_order
            or not isinstance(files, list)
        ):
            continue
        expected_filename = f"bugx_cli-{version_text}-py3-none-any.whl"
        matching = [
            item for item in files
            if isinstance(item, dict)
            and item.get("packagetype") == "bdist_wheel"
            and item.get("filename") == expected_filename
        ]
        eligible: list[dict[str, str]] = []
        for item in matching:
            if item.get("yanked") is not False:
                continue
            digests = item.get("digests")
            digest = str(
                digests.get("sha256") if isinstance(digests, dict) else ""
            ).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise CliError(f"PyPI {expected_filename} 缺少有效 SHA-256")
            url = _validate_official_pypi_url(
                item.get("url"), expected_host=_STABLE_AGENT_FILES_HOST,
            )
            eligible.append({
                "version": version_text,
                "checksum_sha256": digest,
                "url": url,
                "filename": expected_filename,
            })
        if len(eligible) > 1:
            raise CliError(f"PyPI {expected_filename} 存在重复的候选 wheel")
        if eligible:
            candidates.append((order, eligible[0]))

    if not candidates:
        advertised = str(info.get("version") or "未知")
        raise CliError(
            f"PyPI 没有不低于 CLI {current_version} 的未撤回 stable py3-none-any wheel "
            f"（当前公告版本：{advertised}）"
        )
    _, selected = max(candidates, key=lambda item: item[0])
    return selected


def _fetch_pypi_stable_wheel(*, current_version: str) -> dict[str, str]:
    raw = _read_official_pypi_response(
        _STABLE_AGENT_PYPI_JSON,
        expected_host=_STABLE_AGENT_PYPI_HOST,
        accept="application/json",
        max_bytes=_MAX_PYPI_METADATA_BYTES,
    )
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("PyPI stable 发行元数据不是有效 UTF-8 JSON") from exc
    return _select_pypi_stable_wheel(payload, current_version=current_version)


def _download_pypi_stable_wheel(
    url: str,
    destination: Path,
    *,
    expected_sha256: str,
) -> tuple[bool, str]:
    digest = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False, "PyPI wheel 缺少有效 SHA-256"
    try:
        trusted_url = _validate_official_pypi_url(
            url, expected_host=_STABLE_AGENT_FILES_HOST,
        )
    except CliError as exc:
        return False, str(exc)
    request = urllib.request.Request(
        trusted_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"BugX-Client/{__version__} (marker=stable; channel=stable)",
        },
    )
    opener = urllib.request.build_opener(_RejectReleaseRedirects())
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        try:
            if 300 <= exc.code < 400:
                return False, "PyPI wheel 响应试图重定向，已拒绝下载"
            return False, f"PyPI wheel 下载失败：HTTP {exc.code}"
        finally:
            exc.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"PyPI wheel 下载失败：{type(exc).__name__}: {exc}"

    fd: int | None = None
    try:
        _validate_official_pypi_url(
            str(response.geturl() or ""), expected_host=_STABLE_AGENT_FILES_HOST,
        )
        headers = getattr(response, "headers", None)
        raw_length = headers.get("Content-Length") if headers is not None else None
        content_length: int | None = None
        if raw_length:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                return False, "PyPI wheel 响应 Content-Length 无效"
            if content_length < 0 or content_length > _MAX_PYPI_WHEEL_BYTES:
                return False, "PyPI wheel 超过允许大小"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(destination, flags, 0o600)
        hasher = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "wb") as output:
            fd = None
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_PYPI_WHEEL_BYTES:
                    raise CliError("PyPI wheel 超过允许大小")
                output.write(chunk)
                hasher.update(chunk)
        if content_length is not None and total != content_length:
            raise CliError("PyPI wheel 响应长度与 Content-Length 不一致")
        if hasher.hexdigest() != digest:
            raise CliError("PyPI wheel SHA-256 与发行元数据不匹配")
        return True, f"PyPI wheel 已下载并校验：{destination.name}"
    except Exception as exc:  # noqa: BLE001
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(exc)
    finally:
        if fd is not None:
            os.close(fd)
        response.close()


def _prepare_cli_package(path: Path) -> Path:
    """Give a downloaded wheel/sdist the extension pip uses to identify it."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                metadata_names = sorted(
                    name for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
                wheel_names = sorted(
                    name for name in archive.namelist()
                    if name.endswith(".dist-info/WHEEL")
                )
                if not metadata_names:
                    raise CliError(_i18n_exc("system.wheel_metadata_missing", "wheel 包缺少 dist-info/METADATA"))
                metadata = Parser().parsestr(
                    archive.read(metadata_names[0]).decode("utf-8", errors="strict")
                )
                name = str(metadata.get("Name") or "").strip()
                version = str(metadata.get("Version") or "").strip()
                if not name or not version:
                    raise CliError(_i18n_exc("system.wheel_name_version_missing", "wheel 包缺少 Name/Version 元数据"))
                tags: list[str] = []
                if wheel_names:
                    for line in archive.read(wheel_names[0]).decode(
                        "utf-8", errors="strict"
                    ).splitlines():
                        if line.startswith("Tag:"):
                            tag = line.split(":", 1)[1].strip()
                            if tag:
                                tags.append(tag)
                tag = tags[0] if tags else "py3-none-any"
                safe_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
                safe_version = re.sub(r"[^A-Za-z0-9.]+", "_", version).strip("_")
                if not safe_name or not safe_version or not re.fullmatch(
                    r"[A-Za-z0-9_.-]+", tag
                ):
                    raise CliError(_i18n_exc("system.wheel_metadata_filename_invalid", "wheel 包元数据无法生成合法文件名"))
                target = path.with_name(
                    f"{safe_name}-{safe_version}-{tag}.whl"
                )
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                roots = sorted({
                    member.name.split("/", 1)[0]
                    for member in archive.getmembers()
                    if member.name and not member.name.startswith("/")
                })
                root = next((item for item in roots if re.fullmatch(
                    r"[A-Za-z0-9_.-]+", item
                )), None)
                if root is None:
                    raise CliError(_i18n_exc("system.sdist_missing_top_dir", "源码包缺少合法的顶层项目目录"))
                target = path.with_name(f"{root}.tar.gz")
        else:
            raise CliError(_i18n_exc("system.unrecognized_package_archive", "下载内容不是可识别的 wheel 或源码包"))
    except (OSError, UnicodeError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise CliError(_i18n_exc("system.cli_package_format_unrecognized", f"无法识别 CLI 包格式: {exc}", exc=exc)) from exc
    if target != path:
        path.replace(target)
    return target


def _installed_cli_distribution_version() -> str | None:
    """Read the version metadata installed into the interpreter running BugX."""
    try:
        return importlib.metadata.version("bugx-cli")
    except importlib.metadata.PackageNotFoundError:
        return None


def _trusted_repair_cloud_client():
    """Load CloudClient only from a self-consistent installed global engine."""
    engine_root = global_engine_root()
    valid, detail = _engine_self_integrity(engine_root)
    if not valid:
        raise CliError(f"全局引擎无法作为资源恢复引导源：{detail}")
    client_path = engine_root / "cloud_client.py"
    if client_path.is_symlink() or not client_path.is_file():
        raise CliError(f"全局引擎缺少安全的 cloud_client.py：{client_path}")

    trusted_modules = (
        "cloud_client", "report_client", "client_metadata", "secure_atomic", "cloud_schema",
    )
    for name in trusted_modules:
        loaded = sys.modules.get(name)
        if loaded is None:
            continue
        loaded_path = getattr(loaded, "__file__", None)
        try:
            loaded_root = (
                Path(loaded_path).resolve(strict=True).is_relative_to(
                    engine_root.resolve(strict=True)
                )
                if loaded_path is not None else False
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise CliError(f"无法验证当前进程已加载的 {name} 来源") from exc
        if not loaded_root:
            raise CliError(f"当前进程已加载非全局引擎的 {name}；拒绝混用恢复代码")

    # ``core/`` 是命名空间包：``import core.*`` 需要它的父目录在 sys.path 上。
    engine_paths = [str(engine_root.resolve(strict=True))]
    engine_paths.append(str(Path(engine_paths[0]).parent))
    inserted = [item for item in engine_paths if item not in sys.path]
    for item in inserted:
        sys.path.insert(0, item)
    try:
        module = importlib.import_module("core.cloud_sync.cloud_client")
    except Exception as exc:  # noqa: BLE001
        raise CliError(f"无法从已验证全局引擎加载恢复客户端：{exc}") from exc
    finally:
        for item in inserted:
            try:
                sys.path.remove(item)
            except ValueError:
                pass

    module_path = getattr(module, "__file__", None)
    if module_path is None or Path(module_path).resolve(strict=True) != client_path.resolve(strict=True):
        raise CliError("恢复客户端实际来源与已验证全局引擎不一致")
    for name in trusted_modules:
        loaded = sys.modules.get(name)
        if loaded is None:
            continue
        loaded_path = getattr(loaded, "__file__", None)
        if loaded_path is None or not Path(loaded_path).resolve(strict=True).is_relative_to(
            engine_root.resolve(strict=True)
        ):
            raise CliError(f"全局引擎恢复客户端混入了非可信模块 {name}")
    client_type = getattr(module, "CloudClient", None)
    if not callable(client_type):
        raise CliError("已验证全局引擎中的 CloudClient 不可用")
    return client_type


def _construct_stable_repair_cloud_client(client_type):
    """Construct the pinned release client without consulting sync routing."""
    env_key = "BUGX_CLOUD_API_BASE"
    had_value = env_key in os.environ
    previous_value = os.environ.get(env_key)
    # Build 334's trusted bootstrap engine parses cloud_base_url_status() even
    # when base_url is explicit. Shadow arbitrary/invalid user routing only for
    # that synchronous constructor, then restore the process environment.
    os.environ[env_key] = _STABLE_AGENT_RELEASE_BASE
    try:
        return client_type(base_url=_STABLE_AGENT_RELEASE_BASE)
    finally:
        if had_value:
            os.environ[env_key] = previous_value or ""
        else:
            os.environ.pop(env_key, None)


def _validate_agent_repair_wheel(
    wheel_path: Path,
    *,
    expected_version: str,
    minimum_build_id: int,
    extraction_root: Path,
) -> int:
    """Validate release identity and the complete engine payload before pip runs."""
    if wheel_path.suffix.lower() != ".whl":
        raise CliError("资源自动修复只接受 wheel；拒绝安装源码包")
    max_unpacked_bytes = 256 * 1024 * 1024
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(names) != len(set(names)):
                raise CliError("wheel 含有重复文件路径，拒绝安装")

            total_size = 0
            for item in members:
                name = item.filename
                if "\\" in name or name.startswith("/"):
                    raise CliError("wheel 含有非规范或绝对路径，拒绝安装")
                parts = name.rstrip("/").split("/")
                if not parts or any(
                    part in {"", ".", ".."} or ":" in part or part.endswith((".", " "))
                    for part in parts
                ):
                    raise CliError("wheel 含有路径穿越条目，拒绝安装")
                mode = (item.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise CliError("wheel 含有符号链接条目，拒绝安装")
                if not item.is_dir():
                    total_size += item.file_size
                    if total_size > max_unpacked_bytes:
                        raise CliError("wheel 解包体积超过资源修复上限，拒绝安装")

            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise CliError("wheel 必须且只能包含一个 dist-info/METADATA")
            dist_info = metadata_names[0].rsplit("/", 1)[0]
            if f"{dist_info}/WHEEL" not in names or f"{dist_info}/RECORD" not in names:
                raise CliError("wheel 缺少 WHEEL 或 RECORD 元数据")
            wheel_metadata = Parser().parsestr(
                archive.read(f"{dist_info}/WHEEL").decode("utf-8", errors="strict")
            )
            wheel_tags = {str(tag).strip() for tag in wheel_metadata.get_all("Tag", [])}
            if wheel_metadata.get("Root-Is-Purelib", "").strip().lower() != "true" or wheel_tags != {"py3-none-any"}:
                raise CliError("资源修复仅接受 py3-none-any 纯 Python wheel")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8", errors="strict"))
            package_name = re.sub(r"[-_.]+", "-", str(metadata.get("Name") or "")).lower()
            package_version = str(metadata.get("Version") or "").strip()
            if package_name != "bugx-cli" or package_version != expected_version:
                raise CliError(
                    f"wheel 身份不匹配：期望 bugx-cli {expected_version}，"
                    f"收到 {package_name or '未知包'} {package_version or '未知版本'}"
                )

            init_path = "bugx_cli/__init__.py"
            if init_path not in names:
                raise CliError("wheel 缺少 bugx_cli/__init__.py，无法确认 Build ID")
            init_source = archive.read(init_path).decode("utf-8", errors="strict")
            version_match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$", init_source, re.M)
            build_match = re.search(r"^__build_id__\s*:\s*int\s*=\s*(\d+)\s*$", init_source, re.M)
            if version_match is None or version_match.group(1) != expected_version or build_match is None:
                raise CliError("wheel 中的 CLI 版本或 Build ID 声明无效")
            wheel_build_id = int(build_match.group(1))
            if wheel_build_id < minimum_build_id:
                raise CliError(
                    f"拒绝 Build ID 降级：最低要求 {minimum_build_id}，wheel {wheel_build_id}"
                )

            engine_prefix = f"bugx_cli-{expected_version}.data/data/share/bugx-agent/core/"
            if not any(name.startswith(engine_prefix) for name in names):
                raise CliError("wheel 未包含 share/bugx-agent/core 引擎载荷")
            core_root = extraction_root / "validated-core"
            for item in members:
                if not item.filename.startswith(engine_prefix) or item.is_dir():
                    continue
                relative = item.filename[len(engine_prefix):]
                relative_parts = PurePosixPath(relative).parts
                if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
                    raise CliError("wheel 核心载荷路径无效，拒绝安装")
                destination = core_root.joinpath(*relative_parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item, "r") as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

            if not (core_root / "bugx.md").is_file() or not (core_root / "launch_bugx.py").is_file():
                raise CliError("wheel 缺少 bugx.md 或 launch_bugx.py")
            valid, detail = _engine_self_integrity(core_root)
            if not valid:
                raise CliError(f"wheel 核心载荷与自身清单不一致：{detail}")
            metadata_text = (core_root / "metadata.txt").read_text(encoding="utf-8")
            engine_build_match = re.search(r"^build_id:\s*(\d+)\s*$", metadata_text, re.M)
            if engine_build_match is None or int(engine_build_match.group(1)) != wheel_build_id:
                raise CliError("wheel 的 CLI 与引擎 Build ID 不一致")
            return wheel_build_id
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, CliError):
            raise
        raise CliError(f"无法安全校验资源修复 wheel：{exc}") from exc


def _asset_repair_state(current_build_id: int) -> dict[str, object]:
    """Inspect the installed payload and derive a non-downgrade repair floor."""
    explicitly_configured = (
        os.environ.get("BUGX_ASSET_ROOT") is not None
        or os.environ.get("BUGX_DEV_ASSET_ROOT") == "1"
    )
    selected_assets: Path | None = None
    try:
        selected_assets = asset_root()
    except MissingAgentAssetError as exc:
        if not exc.repairable:
            raise CliError(f"显式资源路径或开发模式资源错误，不执行包修复：{exc}") from exc

    if selected_assets is not None and explicitly_configured:
        return {"complete": True, "message": "已安装 Agent 资源完整，无需修复"}

    data_root = sysconfig.get_path("data")
    if not data_root:
        raise CliError("无法确定当前 Python 的安装数据目录，拒绝覆盖其他资源副本")
    installed_assets = Path(data_root) / "share" / "bugx-agent" / "core"
    try:
        _assert_no_symlink_components(installed_assets)
    except CliError as exc:
        raise CliError(
            f"当前解释器安装资源路径包含符号链接或重解析点，拒绝自动写入：{installed_assets}"
        ) from exc
    if installed_assets.exists() and not installed_assets.is_dir():
        raise CliError(f"当前解释器安装资源路径不是目录，拒绝自动覆盖：{installed_assets}")
    if selected_assets is not None:
        if selected_assets.is_symlink():
            raise CliError(f"当前 Agent 资源目录是符号链接，拒绝包修复：{selected_assets}")
        try:
            selected_path = selected_assets.resolve(strict=True)
            installed_path = installed_assets.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise CliError(f"无法验证当前 Agent 资源目录：{exc}") from exc
        if selected_path != installed_path:
            raise CliError(
                f"当前 Agent 资源位于其他 Python 副本，拒绝覆盖：{selected_path}；"
                f"当前解释器 wheel 载荷位置为 {installed_path}"
            )

    global_root = global_engine_root()
    global_build_id = _engine_build_id(global_root)
    asset_build_id = _engine_build_id(selected_assets) if selected_assets is not None else None
    asset_valid = False
    if selected_assets is not None:
        asset_valid, _ = _engine_self_integrity(selected_assets)

    if (
        selected_assets is not None
        and asset_valid
        and asset_build_id == current_build_id
        and (
            global_build_id is None
            or (global_build_id > 0 and current_build_id >= global_build_id)
        )
    ):
        return {"complete": True, "message": "已安装 Agent 资源完整，无需修复"}

    if global_build_id is None or global_build_id < 1:
        raise CliError("全局引擎缺少有效 Build ID，无法确定安全的 CLI wheel 修复下限")
    minimum_build_id = max(
        current_build_id,
        asset_build_id if isinstance(asset_build_id, int) and asset_build_id > 0 else 0,
        global_build_id,
    )
    return {
        "complete": False,
        "global_root": global_root,
        "installed_assets": installed_assets,
        "asset_build_id": asset_build_id,
        "global_build_id": global_build_id,
        "minimum_build_id": minimum_build_id,
    }


def _installed_cli_build_id(distribution: Any, *, expected_version: str) -> int:
    """Read the CLI Build ID from the installed distribution without importing it."""
    init_path = Path(distribution.locate_file("bugx_cli/__init__.py"))
    if init_path.is_symlink() or not init_path.is_file():
        raise CliError(f"安装后缺少可信 CLI 声明：{init_path}")
    try:
        init_source = init_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CliError(f"安装后无法读取 CLI Build ID：{exc}") from exc
    version_match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$", init_source, re.M)
    build_match = re.search(r"^__build_id__\s*:\s*int\s*=\s*(\d+)\s*$", init_source, re.M)
    if version_match is None or version_match.group(1) != expected_version or build_match is None:
        raise CliError("安装后 CLI 版本或 Build ID 声明无效")
    return int(build_match.group(1))


def _installed_distribution_core_records(
    installed_assets: Path,
) -> tuple[dict[str, tuple[str, int]] | None, str]:
    """Read exact core file hashes from this interpreter's non-editable wheel RECORD."""
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        package_name = re.sub(
            r"[-_.]+", "-", str(distribution.metadata.get("Name") or ""),
        ).lower()
        if package_name != "bugx-cli" or str(distribution.version) != __version__:
            return None, "当前 distribution 名称或版本与 CLI 不一致"
        record_text = distribution.read_text("RECORD")
        if not record_text:
            return None, "当前 wheel 缺少 RECORD"
        direct_url_text = distribution.read_text("direct_url.json")
        if direct_url_text:
            direct_url = json.loads(direct_url_text)
            if not isinstance(direct_url, dict):
                return None, "当前 wheel direct_url.json 格式无效"
            dir_info = direct_url.get("dir_info")
            if isinstance(dir_info, dict) and dir_info.get("editable") is True:
                return None, "当前安装为 editable，不能用其 RECORD 恢复 wheel 资源"
    except importlib.metadata.PackageNotFoundError:
        return None, "当前 Python 未安装 bugx-cli distribution"
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        return None, f"无法读取当前 wheel RECORD：{exc}"

    try:
        record_rows = list(csv.reader(io.StringIO(record_text)))
    except (csv.Error, UnicodeError, ValueError, TypeError) as exc:
        return None, f"当前 wheel RECORD CSV 无效：{exc}"
    if not record_rows:
        return None, "当前 wheel RECORD 没有可验证的文件清单"
    try:
        _assert_no_symlink_components(installed_assets)
        installed_root = installed_assets.resolve(strict=False)
    except (CliError, OSError, RuntimeError) as exc:
        return None, f"当前解释器安装资源路径不安全：{exc}"

    marker = "share/bugx-agent/core/"
    records: dict[str, tuple[str, int]] = {}
    total_size = 0
    for row in record_rows:
        if not row:
            continue
        if len(row) != 3 or not row[0]:
            return None, "当前 wheel RECORD 含有无效记录行"
        normalized = row[0].replace("\\", "/")
        if marker not in normalized:
            continue
        if normalized.count(marker) != 1 or normalized.startswith("/"):
            return None, "当前 wheel RECORD 含有非规范引擎资源路径"
        relative = normalized.split(marker, 1)[1]
        parts = PurePosixPath(relative).parts
        if not parts or any(
            part in {"", ".", ".."} or ":" in part or part.endswith((".", " "))
            for part in parts
        ):
            return None, "当前 wheel RECORD 含有路径穿越条目"
        if "__pycache__" in parts or parts[-1].endswith(".pyc"):
            continue
        expected_path = installed_root.joinpath(*parts)
        try:
            located_path = Path(distribution.locate_file(normalized)).resolve(strict=False)
            if located_path != expected_path.resolve(strict=False):
                return None, "当前 wheel RECORD 引擎资源不属于当前解释器数据目录"
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return None, f"当前 wheel RECORD 路径无法验证：{exc}"

        hash_algorithm, separator, hash_value = row[1].partition("=")
        if not separator:
            hash_value = ""
        size_text = row[2]
        if (
            hash_algorithm != "sha256"
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", hash_value)
        ):
            return None, "当前 wheel RECORD 引擎资源缺少有效 SHA-256"
        if not re.fullmatch(r"[0-9]+", size_text):
            return None, "当前 wheel RECORD 引擎资源文件大小无效"
        if len(size_text) > 9:
            return None, "当前 wheel RECORD 引擎资源超过 256 MiB 上限"
        file_size = int(size_text)
        if relative in records:
            return None, "当前 wheel RECORD 含有重复引擎资源路径"
        total_size += file_size
        if total_size > 256 * 1024 * 1024:
            return None, "当前 wheel RECORD 引擎资源超过 256 MiB 上限"
        records[relative] = (hash_value, file_size)

    required = {"bugx.md", "launch_bugx.py", "metadata.txt", "engine-integrity.json"}
    if not required.issubset(records):
        return None, "当前 wheel RECORD 未完整记录 BugX 核心入口与清单"
    return records, "当前 wheel RECORD 可用于逐项恢复"


def _file_matches_distribution_record(
    path: Path,
    record: tuple[str, int],
) -> bool:
    try:
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != record[1]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
        return actual == record[0]
    except OSError:
        return False


def _remove_failed_agent_asset_restore_target(
    installed_assets: Path,
    expected_identity: tuple[int, int],
) -> str:
    """Remove only this restore's failed publication, preserving replacements."""
    quarantined = installed_assets.with_name(
        f".{installed_assets.name}.failed-restore-{time.time_ns()}"
    )
    if quarantined.exists() or quarantined.is_symlink():
        raise CliError(f"失败资源隔离目标已存在：{quarantined}")
    try:
        os.replace(installed_assets, quarantined)
    except FileNotFoundError:
        return "失败发布目标已不存在"

    try:
        info = quarantined.lstat()
    except OSError as exc:
        raise CliError(f"无法核对失败资源隔离目录 {quarantined}：{exc}") from exc
    is_our_directory = (
        stat.S_ISDIR(info.st_mode)
        and (info.st_dev, info.st_ino) == expected_identity
    )
    if is_our_directory:
        shutil.rmtree(quarantined)
        return "已清理本次发布的失败资源目录"

    if installed_assets.exists() or installed_assets.is_symlink():
        raise CliError(
            f"失败资源目标被并发替换且原目标路径已被占用；保留隔离路径 {quarantined}"
        )
    try:
        os.replace(quarantined, installed_assets)
    except OSError as exc:
        raise CliError(
            f"失败资源目标被并发替换且无法原子恢复；保留隔离路径 {quarantined}：{exc}"
        ) from exc
    raise CliError("失败资源目标已被并发替换；已保留原目标，停止自动修复")


def _restore_agent_assets_from_global_record(
    global_root: Path,
    installed_assets: Path,
    *,
    current_build_id: int,
) -> tuple[bool, str, int]:
    """Restore only wheel-recorded bytes from a complete same-build global engine."""
    try:
        _assert_no_symlink_components(global_root)
        _assert_no_symlink_components(installed_assets)
    except CliError as exc:
        return False, f"拒绝通过符号链接或重解析点恢复：{exc}", 0

    global_build_id = _engine_build_id(global_root)
    if global_build_id != current_build_id:
        return False, (
            f"全局引擎 Build ID={global_build_id!r} 与当前 CLI Build ID="
            f"{current_build_id} 不相同"
        ), 0
    global_valid, global_detail = _engine_self_integrity(global_root)
    if not global_valid:
        return False, f"全局引擎完整性校验失败：{global_detail}", 0

    records, detail = _installed_distribution_core_records(installed_assets)
    if records is None:
        return False, detail, 0
    global_resolved = global_root.resolve(strict=True)
    for relative, record in records.items():
        source = global_resolved.joinpath(*PurePosixPath(relative).parts)
        try:
            _assert_no_symlink_components(source)
        except CliError as exc:
            return False, f"全局引擎资源路径不安全 {relative}：{exc}", 0
        if not _file_matches_distribution_record(source, record):
            return False, f"全局引擎资源与当前 wheel RECORD 不匹配：{relative}", 0

    parent = installed_assets.parent
    stage_root: Path | None = None
    backup: Path | None = None
    backup_moved = False
    published_identity: tuple[int, int] | None = None
    try:
        _assert_no_symlink_components(parent)
        parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_components(parent)
        if installed_assets.is_symlink():
            raise CliError(f"安装资源目录是符号链接，拒绝替换：{installed_assets}")
        if installed_assets.exists() and not installed_assets.is_dir():
            raise CliError(f"安装资源路径不是目录，拒绝替换：{installed_assets}")

        stage_root = Path(tempfile.mkdtemp(prefix=".bugx-core-repair-", dir=parent))
        os.chmod(stage_root, 0o700)
        for relative, record in records.items():
            parts = PurePosixPath(relative).parts
            source = global_resolved.joinpath(*parts)
            target = stage_root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            with source.open("rb") as source_file, target.open("xb") as target_file:
                shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
            source_mode = stat.S_IMODE(source.stat().st_mode) & 0o755
            target.chmod(source_mode or 0o644)
            if not _file_matches_distribution_record(target, record):
                raise CliError(f"暂存引擎资源与当前 wheel RECORD 不匹配：{relative}")

        staged_valid, staged_detail = _engine_self_integrity(stage_root)
        if not staged_valid or _engine_build_id(stage_root) != current_build_id:
            raise CliError(f"暂存引擎完整性或 Build ID 校验失败：{staged_detail}")
        os.chmod(stage_root, 0o755)

        if installed_assets.exists():
            backup = installed_assets.with_name(
                f".{installed_assets.name}.repair-backup-{time.time_ns()}"
            )
            if backup.exists() or backup.is_symlink():
                raise CliError(f"资源恢复备份目标已存在：{backup}")
            os.replace(installed_assets, backup)
            backup_moved = True
        stage_info = stage_root.lstat()
        published_identity = (stage_info.st_dev, stage_info.st_ino)
        os.replace(stage_root, installed_assets)
        stage_root = None

        _assert_no_symlink_components(installed_assets)
        installed_valid, installed_detail = _engine_self_integrity(installed_assets)
        if (
            not installed_valid
            or _engine_build_id(installed_assets) != current_build_id
            or any(
                not _file_matches_distribution_record(
                    installed_assets.joinpath(*PurePosixPath(relative).parts), record,
                )
                for relative, record in records.items()
            )
        ):
            raise CliError(f"恢复后安装载荷校验失败：{installed_detail}")

        backup_detail = ""
        if backup_moved and backup is not None:
            try:
                shutil.rmtree(backup)
                backup_moved = False
            except OSError as exc:
                backup_detail = f"；旧损坏载荷暂存于 {backup}（清理失败：{exc}）"
        return True, f"从同 Build ID 的全局引擎按 wheel RECORD 恢复 {len(records)} 个文件{backup_detail}", len(records)
    except Exception as exc:  # noqa: BLE001 - verified wheel remains the fallback
        if backup_moved and backup is not None:
            try:
                if installed_assets.is_symlink():
                    raise CliError(
                        f"回滚目标被替换为符号链接，拒绝覆盖：{installed_assets}"
                    )
                if installed_assets.exists():
                    if not installed_assets.is_dir():
                        raise CliError(
                            f"回滚目标被替换为非目录文件，拒绝覆盖：{installed_assets}"
                        )
                    shutil.rmtree(installed_assets)
                os.replace(backup, installed_assets)
                backup_moved = False
            except Exception as rollback_exc:  # noqa: BLE001 - ambiguous state must fail closed
                raise CliError(
                    f"本地资源恢复失败且原目录回滚失败；保留备份 {backup}：{rollback_exc}"
                ) from exc
        rollback_detail = ""
        if published_identity is not None and backup is None:
            try:
                rollback_detail = _remove_failed_agent_asset_restore_target(
                    installed_assets, published_identity,
                )
            except Exception as rollback_exc:  # noqa: BLE001 - preserve ambiguous targets
                raise CliError(
                    f"本地资源恢复失败或目标状态已变化，停止自动修复：{rollback_exc}"
                ) from exc
        suffix = f"；{rollback_detail}" if rollback_detail else ""
        return False, f"本地资源恢复未完成：{exc}{suffix}", 0
    finally:
        if stage_root is not None and stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)


def repair_cli_agent_assets(
    *,
    channel: str = "stable",
    root: Path | None = None,
) -> str:
    """Restore missing installed Agent data from a verified official wheel."""
    if channel != "stable":
        raise CliError("CLI 安装资源自动修复仅接受 stable 发布通道")
    project_root = Path(root or Path.cwd()).resolve()
    started = time.monotonic()
    request_fields: dict[str, object] = {}

    def record(event: str, message: str, *, phase: str, **fields: object) -> None:
        _log_update_event(
            project_root, event, message,
            phase=phase, duration_ms=round((time.monotonic() - started) * 1000, 1),
            **fields,
        )

    try:
        current_build_id = __build_id__
        if not isinstance(current_build_id, int) or isinstance(current_build_id, bool) or current_build_id < 1:
            raise CliError("资源自动修复失败：当前 CLI 缺少可信的正整数 Build ID")
        repair_state = _asset_repair_state(current_build_id)
    except Exception as exc:  # noqa: BLE001
        record(
            "asset_repair_failed", str(exc), phase="preflight",
            failure_kind=type(exc).__name__, channel=channel,
        )
        raise
    if repair_state["complete"]:
        return str(repair_state["message"])
    request_fields.update({
        "minimum_build_id": repair_state["minimum_build_id"],
        "current_cli_build_id": current_build_id,
        "current_asset_build_id": repair_state["asset_build_id"],
        "global_build_id": repair_state["global_build_id"],
    })

    lock_target = Path.home() / ".bugx" / "cli-package-repair"
    record(
        "asset_repair_started", "开始修复 BugX CLI 安装资源", phase="preflight",
        channel=channel, **request_fields,
    )
    try:
        with _engine_install_lock(lock_target):
            repair_state = _asset_repair_state(current_build_id)
            if repair_state["complete"]:
                record("asset_repair_skipped", "另一个 CLI 进程已完成资源修复", phase="lock")
                return "另一个 CLI 进程已完成资源修复"
            minimum_build_id = int(repair_state["minimum_build_id"])
            global_root = Path(repair_state["global_root"])
            installed_assets = Path(repair_state["installed_assets"])
            request_fields.update({
                "minimum_build_id": minimum_build_id,
                "current_cli_build_id": current_build_id,
                "current_asset_build_id": repair_state["asset_build_id"],
                "global_build_id": repair_state["global_build_id"],
            })

            local_recovery_ok, local_recovery_detail, restored_files = (
                _restore_agent_assets_from_global_record(
                    global_root,
                    installed_assets,
                    current_build_id=current_build_id,
                )
            )
            local_recovery_restored = local_recovery_ok
            if local_recovery_ok:
                verified_state = _asset_repair_state(current_build_id)
                if verified_state["complete"]:
                    message = (
                        f"BugX CLI {__version__}+build.{current_build_id} 安装资源已恢复"
                        f"（来源：当前 wheel RECORD，对照恢复 {restored_files} 个文件）"
                    )
                    record(
                        "asset_repair_succeeded", message,
                        phase="local_record_restore", build_id=current_build_id,
                        restored_files=restored_files, **request_fields,
                    )
                    return message
                local_recovery_ok = False
                local_recovery_detail = (
                    "本地字节恢复完成，但安装后状态复验仍未通过："
                    + str(verified_state.get("message") or "载荷版本或完整性仍不一致")
                )
            request_fields["local_recovery"] = (
                "restored_but_unverified" if local_recovery_restored else "unavailable"
            )
            request_fields["local_recovery_detail"] = local_recovery_detail[:240]
            record(
                "asset_repair_local_recovery_unavailable",
                local_recovery_detail,
                phase="local_record_restore",
                channel=channel,
                **request_fields,
            )

            cloud_client_type = _trusted_repair_cloud_client()
            client = _construct_stable_repair_cloud_client(cloud_client_type)
            release_source = "stable_api"
            package_url: str | None = None
            api_failure = ""
            try:
                result = client.check_version(current_version=__version__, channel=channel)
            except Exception as exc:  # noqa: BLE001
                result = None
                api_failure = f"{type(exc).__name__}: {exc}"
            if not getattr(result, "ok", False):
                if result is not None:
                    api_failure = (
                        str(getattr(result, "error", "") or "")
                        or "发布服务未返回可用版本"
                    )
                    request_fields.update(_update_failure_fields(result))
                record(
                    "asset_repair_fallback_started",
                    f"BugX stable API 不可用，检查官方 PyPI stable wheel：{api_failure}",
                    phase="pypi_fallback", channel=channel,
                    release_source="pypi", **request_fields,
                )
                try:
                    pypi_release = _fetch_pypi_stable_wheel(current_version=__version__)
                except Exception as exc:  # noqa: BLE001
                    record(
                        "asset_repair_failed", str(exc), phase="pypi_metadata",
                        failure_kind=type(exc).__name__, release_source="pypi",
                        release_api_error=api_failure, **request_fields,
                    )
                    raise CliError(
                        f"BugX stable 发布 API 不可用：{api_failure}；"
                        f"PyPI 后备也不可用：{exc}；"
                        f"同 Build ID 本地 wheel RECORD 恢复不可用：{local_recovery_detail}"
                    ) from exc
                release_source = "pypi"
                latest = pypi_release["version"]
                checksum = pypi_release["checksum_sha256"]
                package_url = pypi_release["url"]
                min_client = ""
                remote_build_id = None
                request_fields.update({
                    "release_source": release_source,
                    "release_api_error": api_failure[:240],
                    "pypi_wheel": pypi_release["filename"],
                })
                record(
                    "asset_repair_fallback_selected",
                    f"已选择 PyPI stable wheel {pypi_release['filename']}",
                    phase="pypi_metadata",
                    latest_version=latest, **request_fields,
                )
            else:
                request_fields.update(_update_failure_fields(result))
                payload = result.data if isinstance(result.data, dict) else {}
                info = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                latest = str(info.get("latest_version") or "").strip()
                min_client = str(info.get("min_client_version") or "").strip()
                checksum = str(info.get("checksum_sha256") or "").strip().lower()
                remote_build_id = info.get("build_id")
                request_fields["release_source"] = release_source

            latest_order = _release_version_tuple(latest)
            current_order = _release_version_tuple(__version__)
            minimum_order = _release_version_tuple(min_client) if min_client else None
            if latest_order is None or current_order is None:
                raise CliError(f"资源自动修复失败：{release_source} 返回无效版本 {latest!r}")
            if latest_order[3] != 3:
                raise CliError(f"资源自动修复拒绝安装 {release_source} 预发布版本")
            if release_source == "pypi" and not re.fullmatch(r"\d+\.\d+\.\d+", latest):
                raise CliError("PyPI stable wheel 必须使用 X.Y.Z 版本，拒绝安装")
            if min_client and minimum_order is None:
                raise CliError(f"资源自动修复失败：发布服务返回无效最低版本 {min_client!r}")
            if minimum_order is not None and minimum_order > latest_order:
                raise CliError("资源自动修复失败：发布服务的最低兼容版本高于发布版本")
            if latest_order < current_order:
                raise CliError(
                    f"资源自动修复拒绝版本降级：当前 {__version__}，服务端 {latest}"
                )
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise CliError("资源自动修复失败：发布服务未提供有效 SHA-256 校验值")
            if remote_build_id is not None and (
                not isinstance(remote_build_id, int)
                or isinstance(remote_build_id, bool)
                or remote_build_id < 1
            ):
                raise CliError("资源自动修复失败：发布服务返回无效 Build ID")
            if remote_build_id is not None and remote_build_id < minimum_build_id:
                raise CliError(
                    f"资源自动修复拒绝低于共享引擎的发布包：最低要求 Build ID "
                    f"{minimum_build_id}，服务端 {remote_build_id}"
                )

            with tempfile.TemporaryDirectory(prefix="bugx-asset-repair-") as temp_name:
                temp_root = Path(temp_name)
                package_path = temp_root / "agent.package"
                if release_source == "pypi":
                    ok, download_message = _download_pypi_stable_wheel(
                        package_url or "", package_path, expected_sha256=checksum,
                    )
                else:
                    ok, download_message = client.download_package(
                        package_path, expected_sha256=checksum, expected_version=latest,
                    )
                if not ok:
                    record(
                        "asset_repair_failed", download_message, phase="package_download",
                        failure_kind="download", latest_version=latest,
                        release_source=release_source,
                    )
                    raise CliError(f"资源自动修复下载失败：{download_message}")
                wheel_path = _prepare_cli_package(package_path)
                if wheel_path.suffix.lower() != ".whl":
                    raise CliError("资源自动修复只接受 wheel；拒绝安装源码包")
                wheel_build_id = _validate_agent_repair_wheel(
                    wheel_path, expected_version=latest,
                    minimum_build_id=minimum_build_id, extraction_root=temp_root,
                )
                if remote_build_id is not None and remote_build_id != wheel_build_id:
                    raise CliError(
                        f"资源自动修复失败：服务端 Build ID {remote_build_id} 与 wheel {wheel_build_id} 不一致"
                    )

                with _engine_install_lock(global_root):
                    global_valid, global_detail = _engine_self_integrity(global_root)
                    if not global_valid:
                        raise CliError(f"安装前全局引擎完整性校验失败：{global_detail}")
                    latest_global_build_id = _engine_build_id(global_root)
                    latest_asset_build_id = _engine_build_id(installed_assets)
                    if latest_global_build_id is None or latest_global_build_id < 1:
                        raise CliError("安装前全局引擎 Build ID 失效，拒绝调用 pip")
                    latest_floor = max(
                        current_build_id,
                        latest_global_build_id,
                        latest_asset_build_id if latest_asset_build_id and latest_asset_build_id > 0 else 0,
                    )
                    if wheel_build_id < latest_floor:
                        raise CliError(
                            f"资源自动修复拒绝过期 wheel：下载期间 Build ID 下限升至 "
                            f"{latest_floor}，wheel 为 {wheel_build_id}"
                        )

                    pip_cmd = [
                        sys.executable, "-m", "pip", "install", "--force-reinstall",
                        "--no-deps", "--no-input", "--disable-pip-version-check",
                        str(wheel_path),
                    ]
                    try:
                        proc = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=120)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        record(
                            "asset_repair_failed", str(exc), phase="package_install",
                            failure_kind="install_exception", latest_version=latest,
                        )
                        raise CliError(f"资源自动修复 pip 安装失败：{exc}") from exc
                    if proc.returncode != 0:
                        detail = (proc.stderr or proc.stdout or "pip 未提供错误详情")[-500:]
                        record(
                            "asset_repair_failed", "pip 安装返回非零状态", phase="package_install",
                            failure_kind="nonzero_exit", exit_code=proc.returncode,
                            latest_version=latest,
                        )
                        raise CliError(
                            f"资源自动修复 pip 安装失败（exit {proc.returncode}）：{detail}"
                        )

                    installed_version = _installed_cli_distribution_version()
                    if installed_version != latest:
                        raise CliError(
                            f"资源自动修复后版本不匹配：期望 {latest}，实际 {installed_version or '未知'}"
                        )
                    try:
                        distribution = importlib.metadata.distribution("bugx-cli")
                        direct_url_text = distribution.read_text("direct_url.json")
                        direct_url = json.loads(direct_url_text) if direct_url_text else {}
                    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError, OSError) as exc:
                        raise CliError(f"资源自动修复后无法验证安装来源：{exc}") from exc
                    if not isinstance(direct_url, dict):
                        raise CliError("资源自动修复后 direct_url.json 格式无效")
                    dir_info = direct_url.get("dir_info")
                    if isinstance(dir_info, dict) and dir_info.get("editable") is True:
                        raise CliError("资源自动修复后仍为 editable 安装，拒绝继续执行")
                    installed_cli_build_id = _installed_cli_build_id(
                        distribution, expected_version=latest,
                    )
                    if installed_cli_build_id != wheel_build_id:
                        raise CliError(
                            f"资源自动修复后 CLI Build ID 不匹配：期望 {wheel_build_id}，"
                            f"实际 {installed_cli_build_id}"
                        )

                    if installed_assets.is_symlink() or not installed_assets.is_dir():
                        raise CliError(f"资源自动修复后安装载荷目录缺失或不安全：{installed_assets}")
                    valid, detail = _engine_self_integrity(installed_assets)
                    if not valid:
                        raise CliError(f"资源自动修复后引擎资源校验失败：{detail}")
                    installed_asset_build_id = _engine_build_id(installed_assets)
                    if installed_asset_build_id != wheel_build_id:
                        raise CliError(
                            f"资源自动修复后 asset Build ID 不匹配：期望 {wheel_build_id}，"
                            f"实际 {installed_asset_build_id}"
                        )
                    try:
                        resolved_assets = asset_root()
                    except MissingAgentAssetError as exc:
                        raise CliError(f"资源自动修复后 CLI 仍找不到安装载荷：{exc}") from exc
                    if resolved_assets.resolve(strict=True) != installed_assets.resolve(strict=True):
                        raise CliError("资源自动修复后 asset_root 未指向刚安装的 wheel 载荷")

            source_label = "官方 PyPI" if release_source == "pypi" else "BugX stable 发行 API"
            message = f"BugX CLI {latest}+build.{wheel_build_id} 安装资源已恢复（来源：{source_label}）"
            record(
                "asset_repair_succeeded", message, phase="package_verify",
                latest_version=latest, build_id=wheel_build_id, **request_fields,
            )
            return message
    except Exception as exc:  # noqa: BLE001
        record(
            "asset_repair_failed", str(exc), phase="repair", failure_kind=type(exc).__name__,
            channel=channel, **request_fields,
        )
        raise


def restart_cli_after_asset_repair() -> None:
    """Restart this exact CLI command once so imports use the installed wheel."""
    os.environ[_ASSET_REPAIR_ATTEMPT_ENV] = "1"
    command = [sys.executable, "-m", "bugx_cli.cli", *sys.argv[1:]]
    try:
        os.execv(sys.executable, command)
    except OSError as exc:
        raise CliError(f"资源已修复，但无法重启 CLI：{exc}") from exc
    raise CliError("资源已修复，但 CLI 进程替换意外返回")


def _update_host_names(root: Path) -> list[str]:
    """Return BugX-managed hosts that should be refreshed after a CLI update."""
    known_hosts = {adapter.name for adapter in HOST_ADAPTERS}
    names: list[str] = []
    record = root / ".bugx" / "hosts.json"
    if record.is_file() and not record.is_symlink():
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
            installed = payload.get("installed") if isinstance(payload, dict) else None
            if isinstance(installed, list):
                for item in installed:
                    name = item.get("name") if isinstance(item, dict) else None
                    if isinstance(name, str) and name in known_hosts and name not in names:
                        names.append(name)
        except (OSError, json.JSONDecodeError):
            # A malformed historical record must not become a command source;
            # fall back to the live host detector below.
            pass
    if names:
        return names
    try:
        return [
            str(item["name"])
            for item in discover_hosts(root)
            if isinstance(item, dict)
            and item.get("installed") is True
            and isinstance(item.get("name"), str)
            and item["name"] in known_hosts
        ]
    except (OSError, ValueError, RuntimeError):
        return []


def command_update(args: argparse.Namespace) -> int:
    """bugx update：检查、下载并安装最新 CLI 包。

    流程：
    1. GET /api/v1/agent/version 获取最新版本元数据
    2. 如果已是最新 → 提示并退出
    3. 下载 CLI 包到临时目录
    4. SHA-256 校验
    5. pip install --upgrade <下载的包路径>
    6. bugx install --agent <已登记宿主> --root . --force --non-interactive（刷新宿主入口）
    7. bugx doctor --installation-only 验证
    """
    root = getattr(args, "root", Path.cwd()).resolve()
    started = time.monotonic()

    if getattr(args, "repair_assets", False):
        if getattr(args, "check", False):
            print("[update] --check 为只读操作，不能与 --repair-assets 同时使用")
            return 2
        if not getattr(args, "_console_entrypoint", False):
            print("[update] --repair-assets 只允许由已安装的 CLI 控制台入口执行")
            return 2
        if getattr(args, "channel", "") not in {"", "stable"}:
            print("[update] --repair-assets 只使用 stable 发布通道；请移除 -c alpha")
            return 2
        if os.environ.get(_ASSET_REPAIR_ATTEMPT_ENV) == "1":
            print("[update] CLI 安装资源已恢复。")
            return 0
        else:
            try:
                detail = repair_cli_agent_assets(
                    channel="stable", root=root,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[update] 安装资源修复失败：{exc}")
                return 2
            if detail == "已安装 Agent 资源完整，无需修复":
                print(f"[update] {detail}")
                return 0
            print(f"[update] {detail}；正在重启原命令以加载新安装... ")
            restart_cli_after_asset_repair()

    def record_failure(message: str, *, phase: str, **fields: object) -> None:
        _log_update_event(
            root, "update_check_failed", message,
            phase=phase, duration_ms=round((time.monotonic() - started) * 1000, 1),
            **fields,
        )

    try:
        _ensure_core_importable()
        from core.cloud_sync.cloud_client import CloudClient
    except MissingAgentAssetError:
        # Preserve the typed failure for main(), where only an actual console
        # invocation may repair the current Python install and re-exec once.
        raise
    except Exception as exc:  # noqa: BLE001
        record_failure(str(exc), phase="dependency_load", failure_kind="dependency")
        print(f"[update] 依赖加载失败: {exc}")
        return 2

    if getattr(args, "check", False):
        return command_version_check(args)

    client = CloudClient()

    # 1. 版本检查（支持指定发布通道）
    channel = _update_channel(args)
    try:
        result = client.check_version(current_version=__version__, channel=channel)
    except Exception as exc:  # noqa: BLE001
        record_failure(str(exc), phase="version_check", failure_kind="exception")
        print(f"[update] 版本检查失败: {exc}")
        # `bugx update --check` 早就有这个提示，但非 --check 路径漏了：
        # 自签/内网 CA 下的升级失败只剩一串 CERTIFICATE_VERIFY_FAILED，
        # 而升级通道恰恰是用户摆脱旧 CLI 的唯一出路。
        _print_version_tls_hint(exc)
        return 2

    if not result.ok:
        err = getattr(result, "error", "") or "未知错误"
        record_failure(
            err, phase="version_check", failure_kind="remote",
            **_update_failure_fields(result),
        )
        print(f"[update] 无法获取版本信息: {err}")
        _print_version_tls_hint(err)
        return 2

    data = result.data if isinstance(result.data, dict) else {}
    info = data.get("data") if isinstance(data.get("data"), dict) else data
    if channel:
        print(f"[update] 检查通道 ({channel}) 最新版本...")
    latest = str(info.get("latest_version") or "").strip()
    min_client = str(info.get("min_client_version") or "").strip()
    checksum = str(info.get("checksum_sha256") or "").strip().lower()
    notes = str(info.get("release_notes") or "")
    update_available = info.get("update_available") is True
    update_required = info.get("update_required") is True
    rollback_required = (info.get("rollback_required") is True
                         or info.get("allow_downgrade") is True)
    allow_downgrade = (getattr(args, "allow_downgrade", False)
                       or getattr(args, "force", False)
                       or rollback_required)

    current_version = _release_version_tuple(__version__)
    latest_version = _release_version_tuple(latest)
    min_version = _release_version_tuple(min_client) if min_client else None
    if current_version is None or latest_version is None:
        record_failure("服务端返回了无效版本号", phase="version_validation",
                       failure_kind="invalid_version", latest_version=latest)
        print(f"[update] 服务端返回了无效版本号: {latest!r}")
        return 2
    if min_client and min_version is None:
        record_failure("服务端返回了无效最低兼容版本", phase="version_validation",
                       failure_kind="invalid_min_client", min_client_version=min_client)
        print(f"[update] 服务端返回了无效最低兼容版本: {min_client!r}")
        return 2
    if min_version is not None and min_version > latest_version:
        record_failure("服务端版本元数据矛盾", phase="version_validation",
                       failure_kind="inconsistent_metadata",
                       latest_version=latest, min_client_version=min_client)
        print(
            f"[update] 服务端版本元数据矛盾：最低兼容版本 {min_client} "
            f"高于发布版本 {latest}"
        )
        return 2
    is_rollback = latest_version < current_version
    if is_rollback and not allow_downgrade:
        record_failure("服务端版本低于当前版本", phase="version_validation",
                       failure_kind="downgrade", latest_version=latest,
                       current_version=__version__)
        print(f"[update] 拒绝降级：服务端版本 {latest} 低于当前版本 {__version__}（使用 --allow-downgrade 允许版本回退）")
        return 2
    if is_rollback:
        print(f"[update] 确认服务端版本回退请求 ({__version__} → {latest})...")

    # 2. 已是最新？
    if (latest_version == current_version and not update_available and not update_required
            and not getattr(args, "force", False)):
        print(f"[update] BugX 已是最新版本 ({__version__})")
        return 0

    # The package is downloaded through the trusted BugX endpoint even when
    # the server internally redirects to an external mirror. This preserves
    # one client-side verification path and prevents pip from installing an
    # arbitrary URL without the advertised digest. A checksum is not needed
    # for the no-op path above, but is mandatory for every actual install.
    if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        record_failure("服务端未提供有效 SHA-256 校验值", phase="package_validation",
                       failure_kind="missing_checksum", latest_version=latest)
        print("[update] 服务端未提供有效 SHA-256 校验值，拒绝安装未校验的升级包")
        return 2

    print(f"[update] 当前版本: {__version__}")
    print(f"[update] 目标版本: {latest}")
    if notes:
        print(f"[update] 变更说明: {notes}")

    # 3. 下载 CLI 包。无论 download_url 是否为外部地址，都走服务端
    # /agent/download；服务端的 302 由下载器跟随，摘要校验始终在本地完成。
    tmpdir = Path(tempfile.mkdtemp(prefix="bugx-update-"))
    try:
        dest = tmpdir / f"bugx-{latest}.package"
        print(f"[update] 正在下载 CLI 包...")
        ok, msg = client.download_package(
            dest, expected_sha256=checksum, expected_version=latest,
        )
        if not ok:
            record_failure(msg, phase="package_download", failure_kind="download")
            print(f"[update] {msg}")
            return 1
        print(f"[update] {msg}")
        try:
            package_target = str(_prepare_cli_package(dest))
        except (CliError, OSError) as exc:
            record_failure(str(exc), phase="package_validation", failure_kind="package_format")
            print(f"[update] {exc}")
            return 1

        # 4. pip install --upgrade
        print(f"[update] 执行 pip install --upgrade {package_target}...")
        pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
                   "--no-input", package_target]
        if is_rollback:
            # 回退到更低版本：pip --upgrade 不会降级，改用 --force-reinstall
            pip_cmd = [sys.executable, "-m", "pip", "install",
                       "--force-reinstall", "--no-input", package_target]
        try:
            proc = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            record_failure("pip install 超时（120s）", phase="package_install", failure_kind="timeout")
            print("[update] pip install 超时（120s）")
            return 1
        except OSError as exc:
            record_failure(str(exc), phase="package_install", failure_kind="exec")
            print(f"[update] pip install 执行失败: {exc}")
            return 1

        if proc.returncode != 0:
            record_failure(
                f"pip install exit {proc.returncode}", phase="package_install",
                failure_kind="nonzero_exit", exit_code=proc.returncode,
            )
            print(f"[update] pip install 失败 (exit {proc.returncode})")
            if proc.stderr:
                print(proc.stderr[-500:])
            return 1

        installed_version = _installed_cli_distribution_version()
        if installed_version != latest:
            record_failure(
                "安装后版本校验失败", phase="package_install",
                failure_kind="version_mismatch", expected_version=latest,
                installed_version=installed_version,
            )
            print(
                f"[update] 安装后版本校验失败：期望 {latest}，"
                f"实际 {installed_version or '未知'}"
            )
            return 1
        print(f"[update] pip install 成功，已安装版本 {installed_version}")

        # 更新全局运行时引擎与宿主登记独立；没有 hosts.json 也必须刷新并验证。
        try:
            _install_engine(asset_root(), force=False)
            engine_ok, engine_detail = _engine_integrity_status(
                asset_root(), global_engine_root(),
            )
        except Exception as exc:  # noqa: BLE001
            engine_ok, engine_detail = False, str(exc)
        if not engine_ok:
            record_failure(engine_detail, phase="engine_refresh", failure_kind="integrity")
            print(f"[update] 全局引擎刷新或完整性校验失败：{engine_detail}")
            return 1

        # 5. 刷新宿主入口。失败必须报告失败，不能把“CLI 已更新但项目仍旧”
        # 伪装成完整成功；用户可重试 bugx install --force。
        host_names = _update_host_names(root)
        if host_names:
            print("[update] 刷新 BugX 宿主入口...")
            install_cmd = [sys.executable, "-m", "bugx_cli.cli",
                           "install", "--root", str(root), "--force",
                           "--non-interactive", "--allow-missing"]
            for host_name in host_names:
                install_cmd.extend(["--agent", host_name])
            try:
                install_proc = subprocess.run(
                    install_cmd, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                record_failure("宿主入口刷新超时", phase="host_refresh", failure_kind="timeout")
                print("[update] 宿主入口刷新失败：超时（30s）")
                return 1
            except OSError as exc:
                record_failure(str(exc), phase="host_refresh", failure_kind="exec")
                print(f"[update] 宿主入口刷新失败：{exc}")
                return 1
            if install_proc.returncode != 0:
                record_failure(
                    f"宿主入口刷新 exit {install_proc.returncode}", phase="host_refresh",
                    failure_kind="nonzero_exit", exit_code=install_proc.returncode,
                )
                print(f"[update] 宿主入口刷新失败 (exit {install_proc.returncode})")
                if install_proc.stderr:
                    print(install_proc.stderr[-500:])
                if install_proc.stdout:
                    print(install_proc.stdout[-1000:])
                return 1
            print("[update] 宿主入口已刷新：" + ", ".join(host_names))
        else:
            print("[update] 未发现已登记的 BugX 宿主，跳过宿主入口刷新")

        # 6. 验证安装完整性。必须校验同一个 --root，避免更新 A 项目却诊断
        # 当前工作目录 B 项目。
        print("[update] 验证安装完整性...")
        doctor_cmd = [sys.executable, "-m", "bugx_cli.cli", "doctor",
                      "--root", str(root), "--installation-only"]
        try:
            doctor_proc = subprocess.run(doctor_cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            record_failure("安装验证超时", phase="installation_verify", failure_kind="timeout")
            print("[update] 安装验证失败：超时（30s）")
            return 1
        except OSError as exc:
            record_failure(str(exc), phase="installation_verify", failure_kind="exec")
            print(f"[update] 安装验证失败：{exc}")
            return 1
        if doctor_proc.returncode != 0:
            record_failure(
                f"安装验证 exit {doctor_proc.returncode}", phase="installation_verify",
                failure_kind="nonzero_exit", exit_code=doctor_proc.returncode,
            )
            print(f"[update] 安装验证失败 (exit {doctor_proc.returncode})")
            if doctor_proc.stderr:
                print(doctor_proc.stderr[-500:])
            if doctor_proc.stdout:
                print(doctor_proc.stdout[-1500:])
            return 1
        print("[update] 安装验证通过")

        print(f"\n✓ BugX 已更新到 {latest}")
        print("请重新运行 bugx 命令以使用新版本。")
        return 0
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass



# DOC-230 层 1：恢复动作的带外入口。这些动作在 MCP 侧是 host-only，而 host 通道
# 可能被闲置会话独占（BX-ENGINE-11），导致宿主无法恢复/推进/收尾。CLI 直连引擎
# `supervisor/cli.py`，经 Supervisor 自身状态锁串行化，因此不引入提权面（审计 R2-Q1）。
SUPERVISOR_RECOVERY_ACTIONS = (
    "reset-budget", "recover", "rebind-target", "gate", "repair-fence",
)

# DOC-230 §3.8 第 4 项：severity 修复通道（历史脏数据出口，host 或 CLI）。
# 与恢复动作**分列**：修复不是 `next_action` 恢复链的一环，混在一起会让
# 「恢复链穷举」守卫与 CLI 白名单语义互相污染。
SUPERVISOR_REPAIR_ACTIONS = ("findings",)

# DOC-230 §3.1：`finish` / `next_round` 等**决策类**动作在 MCP 侧保持 host-only
# （它们表达"宿主决策"），但"必须通过层 2/3 变得**可达**"。CLI 直连即该可达性，
# 故纳入带外白名单——否则恢复动作注册表指向的 `bugx supervisor next-round`
# 会被 argparse 拒绝（声明不可达，与 A1 同型）。
SUPERVISOR_DECISION_ACTIONS = ("next-round", "finish")

# CLI `choices` 与 dispatch 的**唯一**白名单真源。此前两处各自硬编码枚举
# （argparse choices 与 dispatch 元组），新增动作时漏改任一处即出现
# 「帮助里列出但调用被拒」或反之的静默漂移——与本会话反复审计的
# 「声明与实现不一致」同型，故由本元组派生。
SUPERVISOR_PASSTHROUGH_ACTIONS = (
    SUPERVISOR_RECOVERY_ACTIONS
    + SUPERVISOR_REPAIR_ACTIONS
    + SUPERVISOR_DECISION_ACTIONS
)


def _supervisor_recovery_action(root: Path, action: str, forwarded: list[str]) -> int:
    """把恢复动作转发给引擎 supervisor CLI，并强制指向**真实**的生命周期状态路径。

    引擎 CLI 的默认 ``--state .bugx/audit-supervisor.json`` 与真实状态
    （``.bugx/pollings/<p>/lifecycles/<l>/state/supervisor.json``）不一致；
    沿用默认值会让命令「成功」却作用在一个不存在的账本上（静默空转）。
    """
    try:
        layout = _layout_module(root).require(root)
        state_path = Path(layout.supervisor)
    except Exception as exc:  # noqa: BLE001 - 布局不可解析时必须显式失败
        print(f"[supervisor] 无法解析生命周期状态路径: {exc}", file=sys.stderr)
        return 2
    argv: list[str] = [action, "--root", str(root), "--state", str(state_path)]
    skip_next = False
    for item in forwarded:
        if skip_next:
            skip_next = False
            continue
        if item in ("--root", "--state"):
            skip_next = True
            continue
        if item.startswith("--root=") or item.startswith("--state="):
            continue
        argv.append(item)
    return _run_script(root, Path("supervisor") / "cli.py", argv)


def command_supervisor(args: argparse.Namespace) -> int:
    """canonical supervisor MCP：install（部署/接线）/ status（部署状态）/ rpc（协议回退通道）。"""
    forwarded = list(getattr(args, "supervisor_args", []) or [])
    # ``supervisor_args`` uses argparse.REMAINDER so rpc options can be passed
    # through. That also captures ``--root`` when it appears after the action
    # (for example ``supervisor status --root <project>``). Honor that root for
    # all actions while leaving rpc's argv intact.
    root = args.root
    index = 0
    while index < len(forwarded):
        item = forwarded[index]
        if item == "--root":
            if index + 1 >= len(forwarded) or not forwarded[index + 1]:
                print("[supervisor] --root 缺少路径", file=sys.stderr)
                return 2
            root = Path(forwarded[index + 1])
            index += 2
            continue
        if item.startswith("--root="):
            value = item.partition("=")[2]
            if not value:
                print("[supervisor] --root 缺少路径", file=sys.stderr)
                return 2
            root = Path(value)
        index += 1
    root = root.expanduser().resolve()
    act = args.supervisor_command
    if act == "rpc":
        # PE-20261005-003 集成：worker 协议 CLI 回退通道，一问一答直连引擎
        has_forwarded_root = any(
            item == "--root" or item.startswith("--root=")
            for item in forwarded
        )
        if not has_forwarded_root:
            forwarded = ["--root", str(root), *forwarded]
        return _run_script(root, Path("supervisor") / "cli.py", ["rpc", *forwarded])
    if act == "install":
        return _supervisor_deploy(root)
    if act == "status":
        return _supervisor_status(root)
    if act in SUPERVISOR_PASSTHROUGH_ACTIONS:
        return _supervisor_recovery_action(root, act, forwarded)
    return 2



def command_self_audit(args: argparse.Namespace) -> int:
    """Expose the safe core self-audit orchestrator as a discoverable CLI command."""
    root = args.root.resolve()
    forwarded = list(getattr(args, "self_audit_args", []) or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if not forwarded:
        forwarded = [
            "run", "--root", str(root),
            "--out", str(root / ".bugx" / "evidence" / "self-audit"),
        ]
    elif forwarded[0] == "run" and "--root" not in forwarded:
        forwarded[1:1] = ["--root", str(root)]
    return _run_script(root, "self_audit.py", forwarded, timeout=300)


def command_engine(args: argparse.Namespace) -> int:
    """Run the lifecycle launcher from the single global engine.

    This is the public replacement for the removed project-local launch entry.
    ``BUGX_PROJECT_ROOT`` is passed to
    the engine so the same command works from Windows, Linux and macOS even
    when the global engine and project live on different volumes.
    """
    root = resolve_project_root(args.root).resolve()
    if not root.is_dir():
        # 宿主最容易犯的错是把 `--root` 指到拼错的路径 / 还没 clone 的目录。
        # 静默让 subprocess 以 cwd 缺失抛 FileNotFoundError 只会产出一条
        # 「Unhandled exception」堆栈，宿主无法判断是参数错还是引擎坏。
        raise CliError(
            f"项目根目录不存在或不是目录：{root}；"
            "请用 `--root` 指向已初始化的项目目录（或先在该目录执行 `bugx init`）。"
        )
    forwarded = list(getattr(args, "engine_args", []) or [])
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if not forwarded:
        forwarded = ["status"]
    if forwarded[0] == "path":
        if len(forwarded) != 3 or forwarded[1] not in {"--name", "-n"}:
            raise CliError("用法：bugx engine --root <项目> path --name <引擎文件>")
        # 引擎按领域拆包后，逐层白名单永远滞后于一次重构；改为「精确相对路径 →
        # 同名递归查找（必须唯一）」，命中歧义时 fail-closed。
        print(resolve_engine_file(Path(forwarded[2]), kind="文件").resolve())
        return 0
    script = _script(root, "launch_bugx.py")
    environment = _python_environment({
        "BUGX_PROJECT_ROOT": str(root),
        "BUGX_GLOBAL_ENGINE_ROOT": str(global_engine_root()),
        # The global launcher cannot infer which installed CLI wrapper invoked
        # it when PATH contains an older `bugx`.  Pass the current wrapper's
        # contract explicitly so an unrelated PATH entry cannot create a false
        # version drift report.
        "BUGX_CLI_VERSION": __version__,
    })
    return subprocess.call(
        [sys.executable, str(script), *forwarded],
        cwd=root,
        env=environment,
    )


def command_probe(args: argparse.Namespace, extra_args: list[str] | None = None) -> int:
    """运行探针包装命令，透传选定 probe 的参数。"""
    root = args.root.resolve()
    script_name = PROBE_SCRIPTS[args.probe_kind]
    forward_args: list[str] = []
    actual_extra = extra_args if extra_args is not None else getattr(args, "probe_args", [])
    if actual_extra:
        forward_args.extend(actual_extra)
    if args.probe_kind not in PROBES_WITHOUT_ROOT_ARGUMENT \
            and "--root" not in forward_args and "-r" not in forward_args:
        forward_args.extend(["--root", str(root)])
    return _run_script(root, script_name, forward_args, timeout=300)


def command_mcp_prune(args: argparse.Namespace) -> int:
    """DOC-230 3.2 / A11: 清理被遗弃的同项目 MCP 会话进程（默认 dry-run）。"""
    import core.bugx_paths as bugx_paths

    root = resolve_project_root(args.root).resolve()
    older_than = float(getattr(args, "older_than", 1800.0) or 0.0)
    scope = bugx_paths.require(root)
    candidates, failure = _abandoned_mcp_processes(
        root, scope.state_root, older_than=older_than,
    )
    if failure is not None:
        print(
            f"MCP 进程枚举失败，结果未知；拒绝声称为空或继续清理：{failure}",
            file=sys.stderr,
        )
        return 1
    if not candidates:
        print(f"无被遗弃的 MCP 会话进程（root={root}, older-than={older_than:.0f}s）")
        return 0
    print(f"检测到 {len(candidates)} 个可能遗弃的 MCP 会话（仅展示，不会裸 PID 终止）：")
    for entry in candidates:
        age = float(entry["age_seconds"])
        activity = entry.get("activity_age_seconds")
        activity_note = "无活动记录" if activity is None else f"活动距今 {float(activity):.0f}s"
        print(
            f"  PID {entry['pid']}（存活 {age:.0f}s，"
            f"{activity_note}）：{entry['command']}"
        )
    if getattr(args, "yes", False):
        print(
            "拒绝 --yes：当前证据不含可验证的进程身份与协作式退出确认，"
            "不会仅凭 PID 发送信号。",
            file=sys.stderr,
        )
        return 2
    print("dry-run：只有获得稳定身份与协作式退出证据后才能执行清理。")
    return 0


def command_mcp(args: argparse.Namespace) -> int:
    """启动本地 stdio MCP；MCP 只协调 Supervisor，不执行 worker。"""
    root = resolve_project_root(args.root).resolve()
    forwarded = ["serve", "--root", str(root), "--role", args.role]
    if getattr(args, "takeover", False):
        forwarded.append("--takeover")
    return _run_script(root, "bugx_mcp_server.py", forwarded)


def command_mcp_docs(args: argparse.Namespace) -> int:
    """索引或启动本地项目文档 MCP，不访问第三方文档服务。"""
    root = resolve_project_root(args.root).resolve()
    forwarded = [args.mcp_docs_command, "--root", str(root), "--db", str(args.db)]
    if args.project_id:
        forwarded.extend(["--project-id", args.project_id])
    if args.version:
        forwarded.extend(["--version", args.version])
    if getattr(args, "no_auto_index", False):
        forwarded.append("--no-auto-index")
    return _run_script(
        root, "bugx_docs_mcp_server.py", forwarded,
        timeout=None if args.mcp_docs_command == "serve" else 300,
    )


def command_mcp_schema(args: argparse.Namespace) -> int:
    """输出 MCP 工具契约，不绑定项目，也不启动 Supervisor。"""
    _ensure_core_importable()
    # 单一导入路径（MCP 服务已归档到 ``core/skills/``）。
    from core.skills import bugx_mcp_server

    print(json.dumps(
        {"server": bugx_mcp_server.SERVER_NAME,
         "version": bugx_mcp_server.SERVER_VERSION,
         "tools": bugx_mcp_server._tool_definitions(args.role)},
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    return 0


def command_mcp_spy(args: argparse.Namespace) -> int:
    """以透明管道代理底层 MCP 进程并录制双向 JSON-RPC wire 报文。"""
    raw_cmd = list(getattr(args, "spy_cmd", []))
    if raw_cmd and raw_cmd[0] == "--":
        raw_cmd = raw_cmd[1:]
    if not raw_cmd:
        print("[bugx mcp spy] 错误：必须指定被代理的目标命令，例如：bugx mcp spy -- bugx mcp serve", file=sys.stderr)
        return 1

    root = resolve_project_root(args.root).resolve()
    log_file = args.log
    if log_file is None:
        log_dir = root / ".bugx" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "mcp-wire.log"
    else:
        log_file = Path(log_file).resolve()
        log_file.parent.mkdir(parents=True, exist_ok=True)

    import datetime as dt
    import subprocess
    import threading

    dump_stream = log_file.open("a", encoding="utf-8", buffering=1)
    lock = threading.Lock()

    def record_wire(direction: str, data: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with lock:
            dump_stream.write(f"[{now}] [{direction}] {data}\n")
            dump_stream.flush()

    record_wire("META", f"SPY_START target={raw_cmd}")

    proc = subprocess.Popen(
        raw_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        bufsize=0,
    )

    def forward_stdin() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                record_wire("IN ", line.rstrip("\r\n"))
                if proc.stdin is not None:
                    proc.stdin.write(line.encode("utf-8"))
                    proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    def forward_stdout() -> None:
        try:
            assert proc.stdout is not None
            while True:
                line_bytes = proc.stdout.readline()
                if not line_bytes:
                    break
                line_str = line_bytes.decode("utf-8", errors="replace")
                record_wire("OUT", line_str.rstrip("\r\n"))
                sys.stdout.write(line_str)
                sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass

    t_in = threading.Thread(target=forward_stdin, name="mcp-spy-stdin", daemon=True)
    t_out = threading.Thread(target=forward_stdout, name="mcp-spy-stdout", daemon=True)
    t_in.start()
    t_out.start()

    ret = proc.wait()
    t_out.join(timeout=1.0)
    record_wire("META", f"SPY_END exit_code={ret}")
    dump_stream.close()
    return ret




def _run_mcp_runtime_captured(
    root: Path, script_args: list[str], *, before_output: Any = None,
) -> int:
    """Run the engine and reprint its stdout at the Python layer.

    机器可读子命令（install/verify/rollback/start --print-command）必须让
    调用方可以可靠地捕获输出；真正的 ``start`` 执行仍走原始 stdio 直通。
    """
    result = subprocess.run(
        [sys.executable, str(_script(root, "mcp_runtime.py")), *script_args],
        cwd=root, capture_output=True, text=True, check=False,
        env=_python_environment(), timeout=300,
    )
    if callable(before_output):
        before_output(result.returncode)
    if result.stdout:
        print(result.stdout if result.stdout.endswith("\n")
              else result.stdout + "\n", end="")
    if result.returncode != 0 and result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


def command_mcp_runtime(args: argparse.Namespace) -> int:
    """管理 lock-hash 专用离线 MCP runtime（DOC-008 §12）。"""
    root = args.root.resolve()
    runtime_root = Path(args.runtime_root).expanduser() \
        if args.runtime_root else default_mcp_runtime_root()
    subcommand = args.mcp_runtime_command
    script_args: list[str]
    passthrough = False
    if subcommand == "install":
        lock_path = Path(args.lock).expanduser().resolve() \
            if args.lock else bundled_lock()
        script_args = [
            "install", "--lock", str(lock_path),
            "--runtime-root", str(runtime_root),
            "--bugx-version", __version__,
        ]
        if args.json:
            script_args.append("--json")
    elif subcommand == "verify":
        script_args = ["verify", "--runtime-root", str(runtime_root)]
        if args.json:
            script_args.append("--json")
    elif subcommand == "start":
        script_args = [
            "start", "--server", args.server,
            "--runtime-root", str(runtime_root),
        ]
        if args.print_command:
            script_args.append("--print-command")
        else:
            # 真正启动：引擎以 os.execve 换身 node，stdio 必须直通宿主。
            passthrough = True
    elif subcommand == "rollback":
        script_args = ["rollback", "--runtime-root", str(runtime_root)]
        if args.json:
            script_args.append("--json")
    else:  # pragma: no cover - argparse restricts choices
        raise CliError(_i18n_exc("system.unknown_mcp_runtime_subcommand", f"未知的 mcp-runtime 子命令: {subcommand}", subcommand=subcommand))
    if passthrough:
        return _run_script(root, "mcp_runtime.py", script_args)
    from .agent import _CliProgress
    progress = _CliProgress(args)
    progress.update(0, f"准备 mcp-runtime {subcommand}")
    progress.update(20, f"正在执行 mcp-runtime {subcommand}")

    def finish_progress(returncode: int) -> None:
        if returncode == 0:
            progress.finish(f"mcp-runtime {subcommand} 完成")
        else:
            progress.fail(f"mcp-runtime {subcommand} 失败")

    try:
        return _run_mcp_runtime_captured(
            root, script_args, before_output=finish_progress,
        )
    except BaseException:
        progress.fail(f"mcp-runtime {subcommand} 失败")
        raise


def _lock_server_flags(lock_path: Path) -> dict[str, tuple[bool, str]]:
    """读取 lock 中各 server 的启用姿态与**停用理由**（缺省启用）。

    ``enabled: false`` 表示载荷已安装但**不会被启动**（供应链姿态，如 SPIKE-008-01）。
    计划输出必须如实区分"已装配"与"已安装但禁用"，并给出 lock 中记录的理由，
    否则用户只看到"被禁用"却无从判断该补什么证明。
    """
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    flags: dict[str, tuple[bool, str]] = {}
    for name, meta in (data.get("servers") or {}).items():
        if isinstance(meta, dict):
            flags[str(name)] = (
                bool(meta.get("enabled", True)),
                str(meta.get("disabled_reason") or ""),
            )
    return flags


def command_capability(args: argparse.Namespace) -> int:
    """探查项目形态并装配能力（DOC-233/234）。

    ``plan``    只读输出画像 → 技能包 → MCP/工具 的完整装配计划；
    ``install`` 按计划装齐**所需 MCP 运行时**（lock-hash 校验 + 真实可执行自检），
                缺凭据的能力自动跳过并给出需要设置的环境变量。

    缺失可选件（凭据/未安装）**绝不阻断**：退出码只在运行时安装/校验失败时非零。
    """
    root = args.root.resolve() if args.root else Path.cwd().resolve()
    plan = _run_script_json(root, "capability_plan.py",
                            ["plan", "--root", str(root), "--json"])
    if plan is None:
        raise CliError(_i18n_exc(
            "system.capability_plan_failed",
            "无法生成能力计划（capability_plan.py 未返回结构化结果）",
        ))

    lock_path = Path(args.lock).expanduser().resolve() \
        if getattr(args, "lock", None) else bundled_lock()
    # 如实标注启用姿态：disabled 的 server 载荷会被安装但不会被启动。
    flags = _lock_server_flags(lock_path)
    for item in plan.get("mcp") or ():
        enabled, reason = flags.get(str(item.get("name")), (True, ""))
        item["enabled"] = enabled
        if reason:
            item["disabled_reason"] = reason

    if args.capability_command == "plan":
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        profile = plan.get("profile") or {}
        print("[capability] 项目形态: forms=%s surfaces=%s frameworks=%s"
              % (profile.get("forms"), profile.get("surfaces"),
                 profile.get("frameworks")))
        print("[capability] 技能包: " + (", ".join(
            str(item.get("skill_id")) for item in plan.get("packs") or ()
        ) or "无"))
        print("[capability] MCP: " + (", ".join(
            f"{item.get('name')}<-{item.get('source')}"
            + (
                f"（lock 禁用：{item['disabled_reason']}）"
                if not item.get("enabled", True) and item.get("disabled_reason")
                else ("" if item.get("enabled", True) else "（lock 禁用，不会启动）")
            )
            for item in plan.get("mcp") or ()
        ) or "无"))
        for item in plan.get("skipped") or ():
            print("  ⏸ %s：缺少凭据，请设置 %s"
                  % (item.get("name"), "、".join(item.get("env") or ()) or "（未声明）"))
        print("[capability] 工具: " + (", ".join(plan.get("tools") or ()) or "无"))
        for error in plan.get("errors") or ():
            print(f"  ⚠ {error}")
        return 0

    # ---- install ----
    installers = plan.get("installers") or {}
    assembled = {str(item.get("name")) for item in plan.get("mcp") or ()}
    # **禁用即不安装**：lock 里 enabled:false 的 server 永不启动，装载荷纯属浪费
    # （启用要改 lock ⇒ 换 hash ⇒ 照旧重装，缓存零收益），还会伪造"已装配"。
    # 是否要跑 install 由 **lock 的启用集**决定：runtime 是整 lock 制品，
    # 与本项目是否装配某个能力无关。
    enabled_lock = sorted(name for name, (flag, _r) in flags.items() if flag)
    disabled_lock = sorted(name for name, (flag, _r) in flags.items() if not flag)
    status: dict[str, Any] = {
        "installed": False, "verified": False, "enabled_servers": enabled_lock,
    }
    scope = {
        "lock_servers": sorted(flags),
        "assembled": sorted(assembled),
        "not_assembled": sorted(set(flags) - assembled),
        "installed_enabled": enabled_lock,
        "not_installed_disabled": disabled_lock,
        "selective_install_supported": False,
    }
    status["scope"] = scope
    if enabled_lock:
        runtime_root = Path(args.runtime_root).expanduser() \
            if getattr(args, "runtime_root", None) else default_mcp_runtime_root()
        install_code = _run_script(root, "mcp_runtime.py", [
            "install", "--lock", str(lock_path),
            "--runtime-root", str(runtime_root),
            "--bugx-version", __version__, "--json",
        ])
        status["installed"] = install_code == 0
        verify_code = _run_script(root, "mcp_runtime.py", [
            "verify", "--runtime-root", str(runtime_root), "--json",
        ])
        status["verified"] = verify_code == 0
    else:
        status["installed"] = status["verified"] = True

    if args.json:
        print(json.dumps({**plan, "runtime": status}, ensure_ascii=False, indent=2))
    else:
        print("[capability] 已装配 %d 个 MCP（%d 个因缺凭据跳过）"
              % (len(plan.get("mcp") or ()), len(plan.get("skipped") or ())))
        print("[capability] 安装范围：整个 lock（%d 个 server；lock-hash 为整体制品，"
              "不支持只装子集）" % len(scope["lock_servers"]))
        print("  安装（lock 启用，可启动）：" + ("、".join(enabled_lock) or "无"))
        if disabled_lock:
            print("  未安装（lock 禁用，永不启动）：" + "、".join(disabled_lock))
        not_assembled = [n for n in scope["not_assembled"] if n not in disabled_lock]
        if not_assembled:
            print("  本项目未装配（载荷已装，但不适用于当前形态）："
                  + "、".join(not_assembled))
        for item in plan.get("mcp") or ():
            name = str(item.get("name"))
            if name in disabled_lock:
                reason = str(item.get("disabled_reason") or "")
                print("  ⏸ %s：lock 禁用，**未安装**载荷%s"
                      % (name, ("；理由：" + reason) if reason else ""))
            elif name in enabled_lock:
                print("  ✓ %s（%s）" % (name, installers.get(name, "runtime")))
        if enabled_lock and not (status["installed"] and status["verified"]):
            print("  ✗ MCP 运行时安装或自检失败，请运行 bugx mcp-runtime verify 排查")
        print("[capability] 可用工具: " + (", ".join(plan.get("tools") or ()) or "无"))
    return 0 if (status["installed"] and status["verified"]) else 1


def _parser_for_command_path(parser: argparse.ArgumentParser,
                             command_path: list[str]) -> argparse.ArgumentParser:
    """Return the argparse parser for a top-level or nested command path."""
    current = parser
    for command in command_path:
        subparsers = next(
            (action for action in current._actions
             if isinstance(action, argparse._SubParsersAction)),
            None,
        )
        if subparsers is None or command not in subparsers.choices:
            path = " ".join(command_path)
            raise CliError(_i18n_exc("system.unknown_help_topic", f"未知的帮助主题：{path}", path=path))
        current = subparsers.choices[command]
    return current


def command_help(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Print root or command-specific help without entering command dispatch."""
    command_path = list(getattr(args, "help_command", []))
    try:
        target = _parser_for_command_path(parser, command_path)
    except CliError as exc:
        parser.error(str(exc))
    target.print_help()
    return 0


def command_log(args: argparse.Namespace) -> int:
    """bugx log：诊断日志分析、错误过滤、链路跟踪与耗时瀑布图。"""
    root = getattr(args, "root", Path.cwd()).resolve()
    try:
        _ensure_core_importable()
        from core.runtime import log_analytics
        return log_analytics.run_log_command(root, args)
    except Exception as exc:
        print(f"[bugx log] ✗ 日志分析执行失败: {exc}")
        return 1





def command_test_generate(args: argparse.Namespace) -> int:
    """运行 core/repair/test_generator.py 自动生成测试脚手架或 PoC。"""
    root = getattr(args, "root", Path.cwd()).resolve()
    forwarded = [
        "--root", str(root),
        "--scope", str(args.scope),
        "--techniques", str(args.techniques),
    ]
    if getattr(args, "out", None):
        forwarded.extend(["--out", str(args.out)])
    if getattr(args, "standalone", False):
        forwarded.append("--standalone")
    if getattr(args, "assert_expr", ""):
        forwarded.extend(["--assert-expr", str(args.assert_expr)])
    if getattr(args, "expected_exc", ""):
        forwarded.extend(["--expected-exc", str(args.expected_exc)])
    if getattr(args, "json", False):
        forwarded.append("--json")
    return _run_script(root, "test_generator.py", forwarded, timeout=120)


def command_test_audit(args: argparse.Namespace) -> int:
    """运行 core/repair/test_quality_auditor.py 对测试用例进行四维质量量化审计。"""
    root = getattr(args, "root", Path.cwd()).resolve()
    forwarded = [str(args.file)]
    if getattr(args, "scope", ""):
        forwarded.extend(["--scope", str(args.scope)])
    if getattr(args, "min_score", None) is not None:
        forwarded.extend(["--min-score", str(args.min_score)])
    if getattr(args, "strict", False):
        forwarded.append("--strict")
    if getattr(args, "json", False):
        forwarded.append("--json")
    return _run_script(root, "test_quality_auditor.py", forwarded, timeout=120)


def command_mining_audit(args: argparse.Namespace) -> int:
    """运行 core/mining_quality_framework.py 对挖掘方案进行四维质量量化审计与加固。"""
    root = getattr(args, "root", Path.cwd()).resolve()
    sub = getattr(args, "mining_subcommand", "audit")
    forwarded = [sub]
    if sub == "audit":
        forwarded.append(str(args.plan_file))
        forwarded.extend(["--root", str(root)])
        if getattr(args, "min_score", None) is not None:
            forwarded.extend(["--min-score", str(args.min_score)])
        if getattr(args, "strict", False):
            forwarded.append("--strict")
        if getattr(args, "allow_degraded", False):
            forwarded.append("--allow-degraded")
        if getattr(args, "json", False):
            forwarded.append("--json")
    elif sub == "explore":
        forwarded.extend(["--root", str(root)])
        if getattr(args, "language", None):
            forwarded.extend(["--language", str(args.language)])
        if getattr(args, "json", False):
            forwarded.append("--json")
    elif sub == "harden":
        forwarded.append(str(args.plan_file))
        forwarded.extend(["--root", str(root)])
        if getattr(args, "out", None):
            forwarded.extend(["--out", str(args.out)])
    return _run_script(root, "mining_quality_framework.py", forwarded, timeout=120)


def command_test_verify(args: argparse.Namespace) -> int:
    """运行 core/test_capability_verifier.py 对测试用例能力进行动态变异杀伤验证。"""
    root = getattr(args, "root", Path.cwd()).resolve()
    sub = getattr(args, "verify_subcommand", "verify")
    forwarded = [sub]
    if sub == "verify":
        forwarded.extend(["--target", str(args.target)])
        forwarded.extend(["--test", str(args.test)])
        if getattr(args, "max_mutants", None) is not None:
            forwarded.extend(["--max-mutants", str(args.max_mutants)])
        if getattr(args, "timeout", None) is not None:
            forwarded.extend(["--timeout", str(args.timeout)])
        if getattr(args, "strict", False):
            forwarded.append("--strict")
        if getattr(args, "json", False):
            forwarded.append("--json")
    elif sub == "mutate":
        forwarded.extend(["--target", str(args.target)])
        if getattr(args, "max_mutants", None) is not None:
            forwarded.extend(["--max-mutants", str(args.max_mutants)])
        if getattr(args, "json", False):
            forwarded.append("--json")
    return _run_script(root, "test_capability_verifier.py", forwarded, timeout=180)



