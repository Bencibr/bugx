from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import *
from ..infra import _mcp_session_hygiene_check
from ..infra.engine_installer import (
    _engine_build_id,
    _engine_receipt_build_id,
    _engine_integrity_status,
    _engine_self_integrity,
    _global_bugx_dir,
    _installed_engine_version,
    _full_engine_version,
    _is_global_home_scope,
    _is_newer_version,
)


_CLOUD_ENDPOINT_CHECK_NAME = "云端端点连通性"
_CLOUD_DEFAULT_BASE_URL = "https://bugx.dev"
_CLOUD_HEALTH_PATHS = ("/health", "/api/v1/health")
_CLOUD_PROBE_TIMEOUT = 1.5


def _normalize_cloud_probe_base_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("云端地址为空")
    value = raw.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("云端地址必须是带 host 的 http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("云端地址不能包含用户名、密码、查询参数或 fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程云端地址必须使用 HTTPS；HTTP 只允许本机测试服务")
    base_url = value.rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[: -len("/api/v1")]
    return base_url


def _project_cloud_mode_enabled(root: Path) -> bool:
    onboarding = root / ".bugx" / "cloud-onboard.json"
    try:
        data = json.loads(onboarding.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("state") in {"done", "sync"}


def _cloud_probe_target(root: Path) -> tuple[str | None, bool, str | None]:
    """Resolve an explicit cloud target without triggering route selection or DNS."""
    env_base = os.environ.get("BUGX_CLOUD_API_BASE", "").strip()
    if env_base:
        try:
            return _normalize_cloud_probe_base_url(env_base), True, None
        except ValueError as exc:
            return None, True, f"BUGX_CLOUD_API_BASE 无效：{exc}"

    try:
        from .cloud import _telemetry_config_path

        config_path = _telemetry_config_path()
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        return None, True, f"云端配置读取失败：{exc}"
    if not isinstance(config, dict):
        return None, True, "云端配置必须是 JSON 对象"

    raw_base = config.get("base_url")
    cloud_mode = _project_cloud_mode_enabled(root)
    if raw_base is not None and (not isinstance(raw_base, str) or raw_base.strip()):
        try:
            base_url = _normalize_cloud_probe_base_url(raw_base)
        except ValueError as exc:
            return None, True, f"cloud-config.json 的 base_url 无效：{exc}"
        # The stock endpoint is intentionally skipped for a local-only project;
        # an explicit onboarding state still means the user opted into cloud mode.
        return base_url, base_url != _CLOUD_DEFAULT_BASE_URL or cloud_mode, None
    if cloud_mode:
        return _CLOUD_DEFAULT_BASE_URL, True, None
    return None, False, None


def _cloud_endpoint_check(root: Path) -> tuple[str, bool, str]:
    base_url, explicit, config_error = _cloud_probe_target(root)
    if config_error:
        return _CLOUD_ENDPOINT_CHECK_NAME, False, config_error
    if not explicit or not base_url:
        return _CLOUD_ENDPOINT_CHECK_NAME, True, "跳过（未配置自定义云端端点）"

    _ensure_core_importable()
    ctx = None
    try:
        from core.infra.client_metadata import tls_context
        ctx = tls_context(base_url=base_url)
    except Exception:
        pass

    last_error: Exception | None = None
    for path in _CLOUD_HEALTH_PATHS:
        health_url = f"{base_url}{path}"
        try:
            kwargs: dict[str, Any] = {"timeout": _CLOUD_PROBE_TIMEOUT}
            if ctx is not None:
                kwargs["context"] = ctx
            response = urllib.request.urlopen(health_url, **kwargs)
            try:
                status = getattr(response, "status", None)
                if not isinstance(status, int):
                    status = response.getcode()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if status in (200, 204):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {status})"
            if status in (401, 403):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {status} 需鉴权)"
            return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 端点可达，但健康检查返回 HTTP {status}"
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 204):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {exc.code})"
            if exc.code in (401, 403):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {exc.code} 需鉴权)"
            if exc.code == 404 and path != _CLOUD_HEALTH_PATHS[-1]:
                last_error = exc
                continue
            return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 端点可达，但健康检查返回 HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - doctor must report, not crash, on network errors.
            last_error = exc
            break

    if last_error is not None:
        try:
            from core.cloud_sync.cloud_client import diagnose_network_error
            diag = diagnose_network_error(last_error, health_url)
            if diag.get("code") in {
                "LOCAL_NETWORK_BLOCKED",
                "LOCAL_NETWORK_UNREACHABLE",
                "LOCAL_NETWORK_PROBE_UNAVAILABLE",
            }:
                return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 连通性诊断：{diag['message']}"
        except Exception:
            pass
        return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 连通性检查失败：{last_error}"

    return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 健康检查未返回结果"


def _heal_project_manifest_version(root: Path, *, force: bool = False) -> tuple[str, str] | None:
    """把滞后于当前 CLI 的 ``manifest.bugx_version`` 就地对齐（DOC-180）。

    返回 ``(旧值, 新值)``；无需对齐时返回 ``None``。只改这一个字段，绝不触碰
    运行态（生命、结算、错题集）。

    历史缺陷：这段自愈原先嵌在 ``if not version_ok:`` 里，而
    ``_version_contract_status`` 的归一化会把 ``0.1.1+2`` 视同 ``0.1.1`` 并返回
    ``version_ok=True``，于是**自愈分支永远进不去**——清单永久停在旧标记，而引擎
    门禁（另一套不归一化的比较）却一直拒绝启动。虚假阻断就这样被"修"成了永久阻断。
    """
    manifest_file = root / ".bugx" / "manifest.json"
    if not manifest_file.is_file() or manifest_file.is_symlink():
        return None
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    old = data.get("bugx_version")
    if not isinstance(old, str) or old == __version__:
        return None
    from ..infra.engine_installer import _contract_version
    if not force and _contract_version(old) != _contract_version(__version__):
        return None
    data["bugx_version"] = __version__
    try:
        _doctor_atomic_replace(
            manifest_file,
            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            mode=manifest_file.stat().st_mode & 0o777,
        )
    except OSError:
        return None
    return old, __version__


def _asset_can_replace_global_engine() -> tuple[bool, str]:
    """Doctor repair may replace the global engine only from a strictly newer or self-intact same-version asset."""
    asset_id = _engine_build_id(asset_root())
    global_id = _engine_build_id(global_engine_root())
    if asset_id is None:
        return False, "安装资源（asset_root）缺少有效的 Build ID 标记，无法自动修复；请重新安装/升级 CLI。"
    if global_id is None:
        return False, (
            "全局引擎缺少 Build ID 标记（可能损坏或未初始化）；无法确认目标版本，拒绝自动替换。"
            "请先备份并核对共享引擎路径与可信安装资源；Doctor 不建议运行 `bugx install --force`。"
        )

    asset_version = _installed_engine_version(asset_root()) or _full_engine_version(__version__, asset_root())
    global_version = _installed_engine_version(global_engine_root())

    if _is_newer_version(global_version, asset_version):
        return False, (
            f"CLI 安装资源版本 ({asset_version}) 低于全局引擎版本 ({global_version})；"
            f"拒绝自动降级。请先升级或重装更高版本的 CLI wheel，"
            "再重跑 doctor；不要运行 `bugx install --force`，它会将共享引擎降级。"
        )

    if _is_newer_version(asset_version, global_version):
        source_ok, source_detail = _engine_self_integrity(asset_root())
        if not source_ok:
            return False, (
                f"CLI 安装资源版本 ({asset_version}) 严格高于全局引擎 ({global_version})，"
                f"但安装资源自身完整性校验失败：{source_detail}；"
                "无法安全替换全局引擎，请重新安装或修复当前 CLI wheel。"
            )
        return True, f"CLI 安装资源版本 ({asset_version}) 严格高于全局引擎 ({global_version})，允许升级替换。"

    # 版本解析缺失时的兜底：基于 build_id 比对
    if (asset_version is None or global_version is None) and global_id > asset_id:
        return False, (
            f"CLI 安装资源 build_id={asset_id} 低于全局引擎 build_id={global_id}；"
            f"拒绝自动降级。请先升级或重装 build_id >= {global_id} 的 CLI wheel，"
            "再重跑 doctor；不要运行 `bugx install --force`，它会将共享引擎降级。"
        )

    # 同版本分支：
    healthy, _detail = _engine_integrity_status(asset_root(), global_engine_root())
    if healthy:
        return False, (
            f"全局引擎与安装资源版本相同 ({asset_version or f'build_id={global_id}'})"
            "且内容与源一致，无需替换。"
        )
    source_ok, source_detail = _engine_self_integrity(asset_root())
    if not source_ok:
        return False, (
            "全局引擎与安装资源都不自洽："
            f"引擎↔源={_detail or '不一致'}；源自身={source_detail}。"
            "此时重放资源只会把坏字节原样覆盖回去，故拒绝修复。"
            "可执行动作：重装/升级 CLI（写入新的安装资源）后重跑 doctor；"
            "若资源是新装的仍报不自洽，说明该 wheel 本身损坏。"
        )
    return True, f"同版本安装资源自身完好（{source_detail}），允许重放修复损坏的全局引擎。"


def _user_site_asset_candidates() -> list[tuple[str, Path]]:
    """Enumerate user-site engine payloads across interpreters and platforms.

    ``site.getuserbase()`` describes only the *running* interpreter, yet the
    replica that actually drifts is the one another interpreter installed (a
    second pyenv version, a Homebrew Python, or ``pip install --user`` under a
    different toolchain).  Those land in well-known per-platform bases, so all
    of them are enumerated instead of trusting a single lookup.
    """
    home = Path.home()
    candidates: list[tuple[str, Path]] = []

    def add(label: str, base: Path | None) -> None:
        if base is not None:
            candidates.append((label, base / "share" / "bugx-agent" / "core"))

    try:
        add("user-site", Path(site.getuserbase()))
    except Exception:  # noqa: BLE001 - discovery degrades, never aborts the check.
        pass
    # macOS: pip --user installs into ~/Library/Python/<X.Y>/
    mac_root = home / "Library" / "Python"
    if mac_root.is_dir():
        for base in sorted(mac_root.iterdir()):
            if base.is_dir():
                add(f"user-site:macos/{base.name}", base)
    # POSIX/pyenv: pip --user installs into ~/.local
    add("user-site:posix", home / ".local")
    # Windows: %APPDATA%\Python and %APPDATA%\Python<XY>
    appdata = os.environ.get("APPDATA")
    if appdata:
        roaming = Path(appdata)
        add("user-site:windows", roaming / "Python")
        if roaming.is_dir():
            for base in sorted(roaming.glob("Python*")):
                if base.is_dir():
                    add(f"user-site:windows/{base.name}", base)
    return candidates


def _path_entry_asset_candidates() -> list[tuple[str, Path]]:
    """Derive engine payload paths from every BugX console entry on PATH.

    A console script lives at ``<prefix>/bin/bugx`` (POSIX) or
    ``<prefix>\\Scripts\\bugx.exe`` (Windows), and pip installs the engine payload
    relative to that same prefix.  This catches interpreters whose data
    directory is unrelated to the running one (extra venvs, pipx, pyenv).
    """
    candidates: list[tuple[str, Path]] = []
    try:
        entries = _bugx_cli_paths()
    except Exception:  # noqa: BLE001 - discovery degrades, never aborts the check.
        return candidates
    for entry in entries:
        prefix = entry.parent.parent
        candidates.append((
            f"cli-prefix:{prefix.name or prefix}",
            prefix / "share" / "bugx-agent" / "core",
        ))
    return candidates


def _engine_replica_consistency_check() -> tuple[bool, str]:
    """Compare installed engine copies and the receipt that identifies the global copy."""
    global_root = global_engine_root()
    global_id = _engine_build_id(global_root)
    global_version = _installed_engine_version(global_root)
    asset_build_id: int | None = None
    asset_version: str | None = None
    issues: list[str] = []
    replica_versions: list[tuple[str, str, int | None]] = []
    replica_inventory_complete = True
    replicas: list[tuple[str, Path]] = [("global", global_root)]
    try:
        replicas.append(("asset", asset_root()))
    except Exception as exc:  # Doctor reports discovery errors instead of aborting.
        issues.append(f"asset_root 无法读取：{exc}")
        replica_inventory_complete = False
    for label, user_asset in _user_site_asset_candidates() + _path_entry_asset_candidates():
        try:
            if user_asset.exists() or user_asset.is_symlink():
                replicas.append((label, user_asset))
        except OSError as exc:
            issues.append(f"{label} 安装资源路径无法读取：{user_asset}：{exc}")
            replica_inventory_complete = False

    seen: set[Path] = set()
    replica_details: list[str] = []
    for name, path in replicas:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            issues.append(f"{name} 引擎副本路径无法解析：{path} ({exc})")
            replica_inventory_complete = False
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        build_id = _engine_build_id(path)
        ver = _installed_engine_version(path)
        if name == "asset":
            asset_build_id = build_id
            if ver is None and build_id is not None:
                ver = _full_engine_version(__version__, path)
            asset_version = ver
        ver_display = ver or f"build_id={build_id!r}"
        replica_details.append(f"{name}={path} version={ver_display}")
        if build_id is None and ver is None:
            issues.append(f"{name} 引擎副本缺少有效版本或 Build ID：{path}")
            replica_inventory_complete = False
        else:
            ver_norm = ver or f"0.0.0+build.{build_id}"
            replica_versions.append((name, ver_norm, build_id))
            if name != "global":
                mismatch = False
                if ver is not None and global_version is not None:
                    mismatch = (ver != global_version)
                else:
                    mismatch = (build_id != global_id)
                if mismatch:
                    issues.append(f"{name} version={ver_display} 与 global version={global_version or f'build_id={global_id!r}'} 不一致")

    receipt_path = global_root.parent / "engine-receipt.json"
    receipt_id = _engine_receipt_build_id(receipt_path)
    if receipt_id is None:
        issues.append(f"全局引擎安装回执缺失或无效：{receipt_path}")
    elif receipt_id != global_id:
        issues.append(f"安装回执 engine_build_id={receipt_id} 与 global build_id={global_id!r} 不一致")

    if issues:
        detail = "；".join(issues)
        if (
            global_version is not None
            and asset_version is not None
            and _is_newer_version(global_version, asset_version)
        ):
            detail += (
                f"；CLI 安装资源 ({asset_version}) 低于全局引擎 ({global_version})，"
                "拒绝自动降级。请先升级或重装更高版本的 CLI wheel 后重跑 doctor；"
                "不要运行 `bugx install --force`，它会将共享引擎降级并影响所有项目。"
            )
        elif (
            asset_version is None
            and global_version is None
            and global_id is not None
            and asset_build_id is not None
            and asset_build_id < global_id
        ):
            detail += (
                f"；CLI 安装资源 build_id={asset_build_id} 低于全局引擎 build_id={global_id}，"
                f"拒绝自动降级。请先升级或重装 build_id >= {global_id} 的 CLI wheel 后重跑 doctor；"
                "不要运行 `bugx install --force`，它会将共享引擎降级并影响所有项目。"
            )
        elif asset_build_id is None or global_id is None or not replica_inventory_complete:
            detail += (
                "；当前 asset、全局引擎或至少一个已发现副本缺少有效 Build ID/无法读取，"
                "无法证明替换来源安全；不要运行 `bugx install --force`。"
                "先核对上述路径、安装包来源与版本，再按确认后的目标修复。"
            )
        else:
            newer_replicas = [
                (name, r_ver) for name, r_ver, _ in replica_versions
                if name != "asset" and asset_version is not None and _is_newer_version(r_ver, asset_version)
            ]
            if newer_replicas:
                newest_name, newest_ver = newer_replicas[0]
                detail += (
                    f"；已发现 {newest_name} 版本 ({newest_ver}) 高于当前 CLI 安装资源 "
                    f"({asset_version})；不要运行 `bugx install --force`，"
                    f"它会用较旧资源覆盖共享引擎。请先改用或升级到更高版本的 CLI。"
                )
            elif asset_version is not None and global_version is not None and _is_newer_version(asset_version, global_version):
                detail += (
                    f"；当前 CLI 安装资源 ({asset_version}) 严格高于共享全局引擎 "
                    f"({global_version})，且不低于所有已发现副本；如需升级共享引擎，"
                    "可运行 `bugx doctor --repair` 或 `bugx install`。"
                )
            elif asset_build_id == global_id:
                detail += (
                    f"；当前 CLI 安装资源与共享全局引擎均为 build_id={global_id}，"
                    "同号不能证明逐文件内容可安全替换；不要运行 `bugx install --force`。"
                    "该命令只重建共享引擎，不会同步 user-site/cli-prefix 等独立副本；"
                    "请按上列路径与版本逐项处理冲突副本。"
                )
            else:
                detail += (
                    f"；当前 CLI 安装资源高于或等于共享全局引擎；如需重建共享引擎，"
                    "可运行 `bugx install --force`。该命令不会同步较旧的 user-site/cli-prefix 副本。"
                )
        return False, detail
    return True, "；".join(replica_details) + f"；receipt engine_build_id={receipt_id}"


def _bugx_cli_paths() -> list[Path]:
    """Find every BugX console entry visible through PATH, one per directory.

    npm publishes several shim spellings (``bugx``/``bugx.cmd``/``bugx.ps1``)
    side by side in the same global bin directory.  Those are one install
    source, not an overwrite conflict, so they are collapsed to a single
    representative; only entries resolving into different directories are
    reported as distinct sources.

    pyenv also introduces shims (``~/.pyenv/shims/bugx``) that proxy execution
    to the active pyenv Python installation (e.g. ``~/.pyenv/versions/.../bin/bugx``).
    When both the shim and the resolved pyenv executable are present, they are
    collapsed to a single pyenv source rather than reported as conflicting installations.
    """
    names = ("bugx", "bugx.exe", "bugx.cmd", "bugx.bat") if os.name == "nt" else ("bugx",)
    found: list[Path] = []
    found_dirs: set[str] = set()
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = Path(raw_entry or os.curdir)
        for name in names:
            candidate = (entry / name).resolve()
            if not candidate.is_file():
                continue
            if candidate.parent.name == "shims" and (
                ".pyenv" in str(candidate) or candidate.parent.parent.name == ".pyenv"
            ):
                try:
                    res = subprocess.run(
                        ["pyenv", "which", candidate.name],
                        capture_output=True,
                        text=True,
                        timeout=1.0,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        target = Path(res.stdout.strip()).resolve()
                        if target.is_file():
                            candidate = target
                except (subprocess.SubprocessError, OSError):
                    pass
            directory = os.path.normcase(str(candidate.parent.resolve()))
            if directory in found_dirs:
                continue
            if ".pyenv" in directory and any(".pyenv" in d for d in found_dirs):
                continue
            found_dirs.add(directory)
            found.append(candidate)
    return found


def _inspect_cli_entry(path: Path) -> dict[str, Any]:
    """Classify a BugX executable path and sample its version."""
    path_str = str(path)
    source_type = "python"
    if "node_modules" in path_str or ".nvm" in path_str:
        source_type = "npm"
    elif path.is_file():
        try:
            head = path.read_bytes()[:256]
            if b"node" in head or b"bugx.js" in head:
                source_type = "npm"
            elif b"uv" in head or "uv/tools" in path_str:
                source_type = "uv"
        except OSError:
            pass

    if source_type != "npm":
        if "uv/tools" in path_str or ".local/share/uv" in path_str:
            source_type = "uv"
        elif "pyenv" in path_str:
            source_type = "pyenv"
        elif "pip" in path_str or "site-packages" in path_str:
            source_type = "pip"

    version: str | None = None
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            parts = out.split()
            if len(parts) >= 2 and parts[0] == "bugx":
                version = parts[1]
            elif parts:
                version = parts[-1]
    except (subprocess.SubprocessError, OSError):
        pass

    return {
        "path": path,
        "type": source_type,
        "version": version,
    }


def _doctor_atomic_replace(target: Path, payload: bytes, *, mode: int | None = None) -> None:
    """Replace a regular project file atomically without following a target symlink."""
    if target.is_symlink():
        raise OSError(f"拒绝修复软链接目标：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _doctor_repair_assets(root: Path, layout: Any) -> list[str]:
    """修复损坏或缺失的运行态、覆盖清单与调度状态。"""
    actions: list[str] = []
    if layout is None:
        return actions
    def failed(step: str, exc: BaseException) -> None:
        actions.append(f"ERROR: {step}失败：{exc}")

    try:
        runtime_path = layout.runtime
        if runtime_path.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{runtime_path}")
        bak_path = runtime_path.with_name(runtime_path.name + ".bak")
        need_repair = False
        if runtime_path.is_file() and not runtime_path.is_symlink():
            try:
                content = runtime_path.read_text(encoding="utf-8").strip()
                if not content:
                    need_repair = True
                else:
                    json.loads(content)
            except (json.JSONDecodeError, OSError):
                need_repair = True
        elif runtime_path.is_symlink() or bak_path.is_file():
            need_repair = True

        if need_repair:
            restored = False
            if bak_path.is_file() and not bak_path.is_symlink():
                try:
                    bak_data = json.loads(bak_path.read_text(encoding="utf-8").strip())
                    if isinstance(bak_data, dict):
                        _doctor_atomic_replace(
                            runtime_path, bak_path.read_bytes(),
                            mode=bak_path.stat().st_mode & 0o777,
                        )
                        actions.append("runtime.json 从 .bak 恢复")
                        restored = True
                except Exception as exc:
                    failed("runtime.json 备份恢复", exc)
            if not restored:
                if not any(item.startswith("ERROR: runtime.json 备份恢复") for item in actions):
                    actions.append("ERROR: runtime.json 损坏且无有效 .bak，拒绝重建；请从可信备份恢复")
    except Exception as exc:
        failed("runtime.json 修复", exc)

    try:
        onboarding_file = root / ".bugx" / "cloud-onboard.json"
        if onboarding_file.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{onboarding_file}")
        if not onboarding_file.is_file():
            payload = {
                "schema_version": 1,
                "state": "local",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _doctor_atomic_replace(
                onboarding_file,
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
            )
            actions.append("已自动补齐缺失的项目本地 onboarding 配置（.bugx/cloud-onboard.json）")
    except Exception as exc:
        failed("cloud-onboard.json 修复", exc)

    try:
        project_file = root / ".bugx-project.json"
        if project_file.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{project_file}")
        if not project_file.is_file():
            onboarding_file = root / ".bugx" / "cloud-onboard.json"
            if onboarding_file.is_file():
                try:
                    ob = json.loads(onboarding_file.read_text(encoding="utf-8"))
                    if isinstance(ob, dict) and ob.get("state") == "done":
                        import uuid
                        proj_id = f"proj-{uuid.uuid4().hex[:12]}"
                        payload = {
                            "project_id": proj_id,
                            "slug": root.name,
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        }
                        _doctor_atomic_replace(
                            project_file,
                            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
                        )
                        actions.append("已自动补齐缺失的项目身份文件 .bugx-project.json")
                except Exception as exc:
                    failed("项目身份文件修复", exc)
    except Exception as exc:
        failed("项目身份文件修复", exc)

    try:
        sup_state = layout.supervisor
        if sup_state.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{sup_state}")
        if sup_state.is_file():
            try:
                json.loads(sup_state.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                corrupted = sup_state.parent / f"{sup_state.name}.corrupted.{int(time.time())}"
                os.replace(sup_state, corrupted)
                bak = sup_state.with_name(sup_state.name + ".bak")
                if bak.is_file():
                    try:
                        bak_data = json.loads(bak.read_text(encoding="utf-8"))
                        if not isinstance(bak_data, dict):
                            raise ValueError("备份内容不是 JSON 对象")
                        _doctor_atomic_replace(
                            sup_state, bak.read_bytes(), mode=bak.stat().st_mode & 0o777,
                        )
                        actions.append("audit-supervisor.json 从 .bak 恢复")
                    except Exception as exc:
                        failed("audit-supervisor.json 备份恢复", exc)
                else:
                    actions.append("ERROR: audit-supervisor.json 损坏且无 .bak，拒绝重建")
    except Exception as exc:
        failed("audit-supervisor.json 修复", exc)

    try:
        repaired_links = _ensure_global_cli_symlinks()
        for link, status in repaired_links:
            if status == "created":
                actions.append(f"已创建全局 CLI 软链接：{link}")
    except Exception as exc:
        failed("全局 CLI 软链接修复", exc)

    try:
        from .lifecycle import _ensure_project_startup_todo
        startup_todo = root / ".bugx" / "startup-todo.md"
        if startup_todo.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{startup_todo}")
        if not startup_todo.is_file():
            _ensure_project_startup_todo(root)
            actions.append("已重新生成 .bugx/startup-todo.md")
    except Exception as exc:
        failed("startup-todo.md 修复", exc)
    return actions


def _check_nested_worktrees_hygiene(root: Path) -> tuple[bool, str]:
    """检查 Git 注册的、位于项目根目录内的 linked worktree。"""
    git_dir = root / ".git"
    if not git_dir.exists():
        return True, "非 Git 仓库"

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(root), capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"无法读取 Git linked worktree：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "未知 Git 错误"
        return False, f"无法读取 Git linked worktree：{detail}"

    # `git worktree list --porcelain` emits one `worktree <path>` line per
    # registered worktree.  The first entry is the current checkout itself;
    # only linked worktrees nested below root can pollute this repository.
    nested_worktrees: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree ").strip()).expanduser()
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if resolved != root.resolve():
            nested_worktrees.append(resolved)

    if nested_worktrees:
        unignored: list[str] = []
        for wt in nested_worktrees:
            rel = wt.relative_to(root.resolve())
            try:
                res = subprocess.run(
                    ["git", "check-ignore", "-q", "--", str(rel)],
                    cwd=str(root),
                    capture_output=True,
                    timeout=2,
                )
                if res.returncode != 0:
                    unignored.append(str(rel))
            except Exception:
                unignored.append(str(rel))
        if unignored:
            return False, f"发现未被 .gitignore 忽略的嵌套 worktree（{', '.join(unignored)}），极易污染索引，建议加入 .gitignore 或移出仓库"
    return True, "工作树隔离正常（无未忽略嵌套 worktree）"



def _host_runtime_gate(
    host_checks: list[tuple[str, bool, str]],
    installed_host_names: list[str] | set[str],
    detected_hosts: dict[str, dict[str, object]],
) -> bool:
    """Decide whether every recorded host is runtime-ready.

    ``installed_host_names`` comes from ``.bugx/hosts.json`` (BugX wiring was
    installed); ``detected_hosts`` comes from live discovery, where
    ``installed`` means "the host CLI is on PATH" and ``project_marker`` means
    "the project-level wiring exists". Those are different facts (P3f: the dsh
    adapter deliberately decouples marker from CLI presence), so a host whose
    CLI is missing from PATH but whose complete bundle is wired into the project
    must not flip ``runtime_ready`` to False — that produced the contradictory
    "44 checks green, runtime_ready=false" report.
    """
    if not installed_host_names:
        return True
    ready = True
    for name in installed_host_names:
        try:
            adapter = get_adapter(name)
        except ValueError:
            ready = False
            continue
        bundle_ok = any(
            item[0] in {f"{adapter.label} 完整适配包", f"{adapter.label} 全局适配包"} and item[1]
            for item in host_checks
        )
        detected = detected_hosts.get(name, {})
        usable = bool(detected.get("installed") or detected.get("project_marker") or bundle_ok)
        ready = ready and bundle_ok and usable
    return ready


def _doctor_auto_repairable_checks(
    checks: list[tuple[str, bool, str]],
    *,
    repair_requested: bool,
    installation_only: bool,
) -> set[str]:
    """Return only checks with a concrete, currently available Doctor repair path.

    ``doctor --repair`` has already attempted its repairs before diagnostics are
    rendered, so remaining failures must not be offered the same repair again.
    Host bundle replacement is deliberately excluded because it used to force
    rewrite project-owned files without preserving local edits.
    """
    del installation_only
    if repair_requested:
        return set()

    failed_items = {name: detail for name, ok, detail in checks if not ok}
    failed = set(failed_items)
    eligible: set[str] = set()
    for tool_name in (
        "Tree-sitter 语法解析引擎",
        "Tree-sitter 多语言增强",
        "codebase-memory 代码图谱",
        "主目录全局隔离",
    ):
        if tool_name in failed:
            eligible.add(tool_name)
    for name, detail in failed_items.items():
        if name.endswith(" MCP 配置") and not any(
            marker in detail for marker in ("自定义", "旧版", "拒绝自动迁移", "无法解析")
        ):
            eligible.add(name)

    engine_checks = failed & {"全局引擎完整性", "BugX 版本一致性"}
    if engine_checks:
        try:
            can_replace, _detail = _asset_can_replace_global_engine()
        except (CliError, OSError, ValueError):
            can_replace = False
        if can_replace:
            eligible.update(engine_checks)
    return eligible


def _generate_ai_diagnostics(
    checks: list[tuple[str, bool, str]],
    installation_required: set[str],
    root: Path,
    strict: bool = False,
    auto_repairable_checks: set[str] | None = None,
    use_ai: bool = False,
) -> dict[str, Any]:
    """基于失败项生成根因分析和修复提示（默认确定性基线，--ai 开启 AI 深诊）。"""
    from ..infra.ai_advisor import DiagnosticAIArbitrator
    arbitrator = DiagnosticAIArbitrator(root)
    result = arbitrator.diagnose(
        checks,
        installation_required,
        strict=strict,
        auto_repairable_checks=auto_repairable_checks,
        use_ai=use_ai,
    )
    return result.as_dict()


def _render_ai_diagnostics_panel(ai_diag: dict[str, Any], is_explicit_ai: bool = False) -> None:
    items = ai_diag.get("items", [])
    if items:
        print("\n╭── 🤖 BugX AI 根因分析与确定性修复提示 ─────────────────────────────────╮")
        print(f"│ {ai_diag.get('summary', '')}")
        print("│")
        for idx, item in enumerate(items, 1):
            req_badge = "[必需]" if item.get("required") else "[可选]"
            repair_badge = " (可由 doctor --repair 尝试修复)" if item.get("auto_repairable") else ""
            print(f"│ {idx}. {req_badge} 【{item['name']}】{repair_badge}")
            print(f"│    • 根因分析: {item['root_cause']}")
            print(f"│    • 修复处方: {item['prescription']}")
            if idx < len(items):
                print("│")
        if ai_diag.get("can_auto_repair"):
            print("│")
            print("│ Doctor 检测到可尝试的修复项；执行前请确认其影响范围:")
            print(f"│    👉 {ai_diag['auto_repair_command']}")
        print("╰───────────────────────────────────────────────────────────────────────╯")
    elif is_explicit_ai:
        print("\n╭── 🤖 BugX AI 智能排障分析 ──────────────────────────────────────────╮")
        print(f"│ {ai_diag.get('summary', '')}")
        print("╰───────────────────────────────────────────────────────────────────────╯")


# ---------------------------------------------------------------------------
# Doctor 并行只读探测（耗时账目 W-002 遗留性能项）
#
# 检查项并行性分类（依据 command_doctor 的检查链逐项审计）：
# - 【纯读取可并行】只读文件系统 / 进程表 / 网络探测，互相独立、无顺序依赖：
#   工作树卫生、云端端点连通性、全局引擎完整性判定、版本契约判定、引擎副本
#   一致性、OpenCode/Pi 全局入口完整性、各宿主 MCP 配置（repair=False）、
#   错题集账本、全局错题集、codebase-memory 探测、基础效率工具、MCP 离线
#   runtime 诊断、MCP 会话进程卫生、UI 环境状态、BugX CLI 路径。
# - 【有写操作须串行】所有 --repair 写路径永远不进入并行块：
#   layout_module.repair / _doctor_repair_assets（项目资产修复）、
#   _install_engine(force=True)（引擎重放）、_heal_project_manifest_version
#   （清单自愈）、pip install（Tree-sitter 补装）、
#   _check_host_mcp_config(repair=True)。因此并行批只在
#   「非 --repair 且非 --installation-only」的运行态诊断启用，两种模式都
#   保持原串行路径不变。
# - 【有顺序依赖】项目初始化状态/.bugx 运行目录依赖 manifest+layout；
#   宿主适配包检查（_host_bundle_checks）的 installed_host_names 是
#   OpenCode/Pi/MCP 配置检查的注册条件；错题集依赖 initialized；
#   全局错题集与 UI 检查依赖 layout/ui_script 存在性。这些依赖在入批前
#   已由串行前置段完成。
# 渲染处按原始行序从并行结果中取值；timeout 语义与判定逻辑逐字保留。
# ---------------------------------------------------------------------------

_DOCTOR_PARALLEL_MAX_WORKERS = 6


def _doctor_probe_job(fn):
    """包装单个并行探测任务。

    各检查 row 函数已在内部按串行语义捕获预期异常类型；此处仅兜底意外异常，
    取用处（_unwrap_parallel_result）会将其原样重抛，保持与串行实现一致的
    失败行为：意外异常 = 命令失败，而不是静默降级。
    """
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - 意外异常延迟到取用处重抛
        return ("error", exc)


def _unwrap_parallel_result(parallel_results: dict[str, Any], key: str) -> Any:
    status, value = parallel_results[key]
    if status != "ok":
        raise value
    return value


def _engine_integrity_check_row() -> tuple[bool, str]:
    """全局引擎完整性（只读判定；--repair 的引擎重放写路径留在串行段）。"""
    try:
        return _engine_integrity_status(asset_root(), global_engine_root())
    except (CliError, OSError, ValueError) as exc:
        return False, f"全局引擎无法校验：{exc}"


def _version_contract_check_row(root: Path) -> tuple[bool, str]:
    """版本契约一致性（只读；--repair 的清单自愈写路径留在串行段）。"""
    try:
        return _version_contract_status(root, global_engine_root())
    except (CliError, OSError, ValueError) as exc:
        return False, f"版本契约无法校验：{exc}"


def _lesson_ledger_check_row(root: Path, lesson_json: Path) -> tuple[bool, str]:
    lesson_db = lesson_json.with_suffix(".sqlite3")
    if not lesson_db.is_file() or lesson_db.is_symlink():
        return False, "缺少或不安全的 SQLite 运行库；请从备份恢复，禁止静默重建"
    try:
        result = subprocess.run(
            [sys.executable, str(_script(root, "lesson_ledger.py")),
             "validate", "--ledger", str(lesson_json), "--json"],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment(),
        )
        lesson_ready = result.returncode == 0
        lesson_detail = ("SQLite 运行库有效" if lesson_ready else (
            (result.stderr or result.stdout).strip()[:500] or "SQLite 校验失败"
        ))
        if lesson_ready and (not lesson_json.is_file() or lesson_json.is_symlink()):
            lesson_detail += "；JSON 种子缺失（不影响读取，恢复时需人工确认）"
        return lesson_ready, lesson_detail
    except (OSError, subprocess.TimeoutExpired, CliError) as exc:
        return False, f"SQLite 校验异常：{exc}"


_HOME_PSEUDO_PROJECT_FILES = (
    "manifest.json",
    "hosts.json",
    "cloud-onboard.json",
    "startup-todo.md",
    "life-state.json",
    "runtime.json",
    "audit-supervisor.json",
    "prep-record.md",
    "module-coverage.md",
    "knowledge/lessons.json",
    "knowledge/lessons.sqlite3",
    "knowledge/lessons.sqlite3-wal",
    "knowledge/lessons.sqlite3-shm",
)


def _global_home_pseudo_project_residuals(home_bugx: Path | None = None) -> list[str]:
    """Return relative paths of accidental project state files inside ~/.bugx."""
    base = home_bugx or _global_bugx_dir()
    found: list[str] = []
    if not base.is_dir():
        return found
    for rel in _HOME_PSEUDO_PROJECT_FILES:
        target = base / rel
        if target.exists() or target.is_symlink():
            found.append(rel)
    return found


def _clean_global_home_pseudo_project_residuals(home_bugx: Path | None = None) -> list[str]:
    """Remove accidental pseudo-project files inside ~/.bugx while preserving core/global knowledge."""
    base = home_bugx or _global_bugx_dir()
    removed: list[str] = []
    if not base.is_dir():
        return removed
    for rel in _HOME_PSEUDO_PROJECT_FILES:
        target = base / rel
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or target.is_file():
            target.unlink()
            removed.append(rel)
    return removed


def _global_home_host_checks(
    root: Path,
    *,
    target_hosts: set[str] | None = None,
) -> tuple[list[tuple[str, bool, str]], set[str]]:
    """Inspect installed user-level host integrations when running Doctor in ~."""
    from .agent import _dsh_home, _global_opencode_agents_dir
    from .plugin_claude import _claude_user_agents_dir
    from .plugin_kimi import _kimi_code_home, validate_install as _validate_kimi
    from .plugin_minimax import _minimax_plugin_root, validate_install as _validate_minimax
    from .plugin_qoder import _qoder_user_agents_dir
    from .plugin_zcode import _zcode_global_plugin_root, _zcode_user_agents_dir
    from ..host_adapters import workbuddy_user_skill_root

    del root
    checks: list[tuple[str, bool, str]] = []
    user_mcp_hosts: set[str] = set()

    def wanted(name: str) -> bool:
        return target_hosts is None or name in target_hosts

    if wanted("dsh") and (
        (_dsh_home() / ".agent-presets" / "bugx" / "agent.cordis.yml").exists()
        or (_dsh_home() / "plugins" / "bugx-ui").exists()
        or (target_hosts is not None and "dsh" in target_hosts)
    ):
        preset_ok = (_dsh_home() / ".agent-presets" / "bugx" / "agent.cordis.yml").is_file()
        ui_ok = (_dsh_home() / "plugins" / "bugx-ui" / "package.json").is_file()
        checks.append((
            "DeepSeek Harness 全局适配包",
            preset_ok and ui_ok,
            "全局预设与 UI 包均存在" if (preset_ok and ui_ok) else "全局预设或 UI 包不完整；请运行 `bugx install --agent dsh`",
        ))
        user_mcp_hosts.add("dsh")

    if wanted("zcode") and (
        (_zcode_user_agents_dir() / "bugx.md").exists()
        or (_zcode_global_plugin_root() / "plugins" / "bugx").exists()
        or (target_hosts is not None and "zcode" in target_hosts)
    ):
        u_dir = _zcode_user_agents_dir()
        missing = [f for f in ("bugx.md", "bugx-worker.md") if not (u_dir / f).is_file()]
        checks.append((
            "ZCode 全局适配包",
            not missing,
            "用户级子智能体与全局插件均就绪" if not missing else f"缺少用户级 {', '.join(missing)}（{u_dir}）",
        ))
        user_mcp_hosts.add("zcode")

    if wanted("minimax") and (_minimax_plugin_root().exists() or (target_hosts is not None and "minimax" in target_hosts)):
        ok_mm, detail_mm = _validate_minimax(Path.home())
        checks.append(("MiniMax Code 全局适配包", ok_mm, detail_mm))
        user_mcp_hosts.add("minimax")

    if wanted("kimi") and ((_kimi_code_home() / "plugins" / "managed" / "bugx").exists() or (target_hosts is not None and "kimi" in target_hosts)):
        ok_km, detail_km = _validate_kimi(Path.home())
        checks.append(("Kimi Code 全局适配包", ok_km, detail_km))
        user_mcp_hosts.add("kimi")

    if wanted("opencode") and ((_global_opencode_agents_dir() / "bugx.md").exists() or (target_hosts is not None and "opencode" in target_hosts)):
        from .lifecycle import _global_opencode_integrity_check

        checks.append(("OpenCode 全局入口完整性", *_global_opencode_integrity_check()))

    if wanted("pi") and ((Path.home() / ".agents" / "skills" / "bugx" / "SKILL.md").exists() or (target_hosts is not None and "pi" in target_hosts)):
        pi_global = Path.home() / ".agents" / "skills" / "bugx" / "SKILL.md"
        pi_ok = pi_global.is_file()
        checks.append((
            "Pi 全局 Skill 完整性",
            pi_ok,
            "~/.agents/skills/bugx/SKILL.md 已就绪" if pi_ok else "缺少 ~/.agents/skills/bugx/SKILL.md；请运行 `bugx install --agent pi`",
        ))

    if wanted("claude") and ((_claude_user_agents_dir() / "bugx.md").exists() or (target_hosts is not None and "claude" in target_hosts)):
        c_dir = _claude_user_agents_dir()
        missing = [f for f in ("bugx.md", "bugx-worker.md") if not (c_dir / f).is_file()]
        checks.append((
            "Claude Code 全局适配包",
            not missing,
            f"用户级智能体已就绪（{c_dir}）；工作区 .mcp.json 需进入具体项目运行 bugx install" if not missing else f"缺少用户级 {', '.join(missing)}",
        ))

    if wanted("qoder") and ((_qoder_user_agents_dir() / "bugx.md").exists() or (target_hosts is not None and "qoder" in target_hosts)):
        q_dir = _qoder_user_agents_dir()
        missing = [f for f in ("bugx.md", "bugx-worker.md") if not (q_dir / f).is_file()]
        checks.append((
            "Qoder 全局适配包",
            not missing,
            f"用户级智能体已就绪（{q_dir}）" if not missing else f"缺少用户级 {', '.join(missing)}",
        ))

    if wanted("workbuddy") and ((workbuddy_user_skill_root() / "skills" / "bugx" / "SKILL.md").exists() or (target_hosts is not None and "workbuddy" in target_hosts)):
        wb_skill = workbuddy_user_skill_root() / "skills" / "bugx" / "SKILL.md"
        checks.append((
            "WorkBuddy 全局适配包",
            wb_skill.is_file(),
            f"用户级 Skill 已就绪（{wb_skill}）" if wb_skill.is_file() else f"缺少用户级 Skill（{wb_skill}）",
        ))

    return checks, user_mcp_hosts


def _repair_active_host_mcp_config(
    root: Path,
    host_name: str,
    *,
    is_home_scope: bool,
) -> bool:
    """Safely repair BugX-managed host MCP config without clobbering custom user config."""
    from .agent import _check_host_mcp_config, _ensure_dsh_patch, _ensure_opencode_config
    from .plugin import write_host_mcp_config

    ok_before, detail_before = _check_host_mcp_config(root, host_name, repair=False)
    if ok_before:
        return True
    blocked_markers = ("自定义", "旧版", "拒绝自动迁移", "符号链接", "无法解析", "legacy")
    if any(marker in detail_before for marker in blocked_markers):
        return False
    source = asset_root()
    try:
        if host_name == "dsh":
            _ensure_dsh_patch(root)
        elif host_name == "minimax":
            from .plugin_minimax import _ensure_minimax_plugin

            _ensure_minimax_plugin(root, source)
        elif host_name == "kimi":
            from .plugin_kimi import _ensure_kimi_plugin

            _ensure_kimi_plugin(root, source)
        elif host_name == "zcode":
            write_host_mcp_config(root, host_name)
        elif not is_home_scope and host_name == "opencode":
            _ensure_opencode_config(root)
        elif not is_home_scope and host_name in {"claude", "codex", "cline", "trea", "funide", "qoder", "copilot"}:
            write_host_mcp_config(root, host_name)
        else:
            return False
    except Exception:
        return False
    ok_after, _ = _check_host_mcp_config(root, host_name, repair=False)
    return ok_after


def _awaiting_first_audit(root: Path, layout: Any) -> bool:
    """Return True when BugX is installed in project and awaiting its first audit `pre`."""
    hosts_file = root / ".bugx" / "hosts.json"
    if not hosts_file.is_file():
        return False
    pollings_dir = root / ".bugx" / "pollings"
    if pollings_dir.is_dir():
        try:
            if any(pollings_dir.glob("*/lifecycles/*/rounds/*")):
                return False
        except OSError:
            return False
    life_file = layout.life if layout is not None and hasattr(layout, "life") else (root / ".bugx" / "life-state.json")
    if life_file.is_file() and not life_file.is_symlink():
        try:
            life_data = json.loads(life_file.read_text(encoding="utf-8"))
            if isinstance(life_data, dict):
                if life_data.get("mode") is not None or int(life_data.get("rounds_completed") or 0) > 0:
                    return False
        except (OSError, UnicodeError, ValueError, TypeError):
            return False
    return True


def _global_lesson_ledger_check_row(root: Path, layout: Any) -> tuple[bool, str]:
    if layout is not None:
        global_root = layout.global_knowledge_root
        global_db = layout.global_knowledge_db
        global_seed = layout.global_knowledge_seed
    else:
        override = os.environ.get("BUGX_GLOBAL_KNOWLEDGE_ROOT")
        global_root = Path(override).expanduser().resolve() if override else (_global_bugx_dir() / "knowledge")
        global_db = global_root / "global.sqlite3"
        global_seed = global_root / "global.json"
    global_marker = global_root / "manifest.json"
    if not global_marker.exists() and not global_db.exists():
        return True, "尚未初始化；项目库可独立运行"
    if not global_db.is_file() or global_db.is_symlink():
        return False, "全局 SQLite 缺失；不会静默从 JSON 重建"
    try:
        result = subprocess.run(
            [sys.executable, str(_script(root, "lesson_ledger.py")),
             "validate", "--ledger", str(global_seed), "--json"],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment(),
        )
        global_ok = result.returncode == 0
        global_detail = "全局 SQLite 有效（只读投影源）" if global_ok else (
            (result.stderr or result.stdout).strip()[:500] or "全局 SQLite 校验失败"
        )
        return global_ok, global_detail
    except (OSError, subprocess.TimeoutExpired, CliError) as exc:
        return False, f"全局 SQLite 校验异常：{exc}"


def _codebase_memory_check_row(root: Path, *, check_project_index: bool = True) -> tuple[bool, str]:
    # 优化：优先尝试内存级快速 inspect，失败回退子进程
    try:
        from core.probes import codebase_memory_probe
        cbm = codebase_memory_probe.inspect(root, check_project_index=check_project_index)
        checks = cbm.get("checks") or []
        binary_ok = all(
            bool(item.get("ok"))
            for item in checks
            if isinstance(item, dict) and item.get("name") in {"binary", "validated_version", "supported_version"}
        ) and len([item for item in checks if isinstance(item, dict) and item.get("name") in {"binary", "validated_version", "supported_version"}]) >= 3
        cbm_ok = (cbm.get("status") == "READY") and binary_ok
        if cbm_ok:
            suffix = "项目索引已就绪" if check_project_index else "核心工具已就绪（项目索引在审计启动时校准）"
            return True, f"READY，版本 {cbm.get('version') or 'unknown'}；{suffix}"
        detail = cbm.get("error") or cbm.get("install_hint") or cbm.get("fallback") or "未安装"
        return False, f"NOT READY（必需核心依赖，不可降级）：版本 {cbm.get('version') or '未安装'}；{detail}"
    except Exception:
        try:
            cmd = [
                sys.executable, str(_script(root, "codebase_memory_probe.py")),
                "status", "--root", str(root), "--json",
            ]
            if not check_project_index:
                cmd.append("--installation-only")
            cbm_result = subprocess.run(
                cmd,
                cwd=root, capture_output=True, text=True, check=False, timeout=60,
                env=_python_environment(),
            )
            cbm = json.loads(cbm_result.stdout)
            cbm_ok = cbm.get("status") == "READY"
            if cbm_ok:
                suffix = "项目索引已就绪" if check_project_index else "核心工具已就绪（项目索引在审计启动时校准）"
                return True, f"READY，版本 {cbm.get('version') or 'unknown'}；{suffix}"
            return False, f"NOT READY（必需核心依赖，不可降级）：版本 {cbm.get('version') or '未安装'}"
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, CliError) as exc:
            return False, f"NOT READY（必需核心依赖）：{exc}"


def _efficiency_tools_check_row(root: Path, *, strict: bool = False) -> tuple[bool, str]:
    try:
        from core.probes import efficiency_probe
        efficiency = efficiency_probe.inspect(root)
        efficiency_ok = efficiency.get("status") == "READY"
        missing = [
            item.get("name", "unknown")
            for item in efficiency.get("base_tools", []) if not item.get("ok")
        ]
        if efficiency_ok:
            return True, "READY；内置回退与常用加速工具均就绪"
        if not strict:
            return True, f"内置检索就绪（可选外部加速 {', '.join(missing)} 将按需在审计阶段使用）"
        efficiency_detail = efficiency.get("status", "DEGRADED")
        if missing:
            efficiency_detail += f"；待补充 {', '.join(missing)}"
        efficiency_detail += "；内置回退可用"
        return False, efficiency_detail
    except Exception:
        try:
            efficiency_result = subprocess.run(
                [sys.executable, str(_script(root, "efficiency_probe.py")),
                 "status", "--root", str(root), "--json"],
                cwd=root, capture_output=True, text=True, check=False, timeout=45,
                env=_python_environment(),
            )
            efficiency = json.loads(efficiency_result.stdout)
            efficiency_ok = efficiency.get("status") == "READY"
            missing = [
                item.get("name", "unknown")
                for item in efficiency.get("base_tools", []) if not item.get("ok")
            ]
            if efficiency_ok:
                return True, "READY；内置回退与常用加速工具均就绪"
            if not strict:
                return True, f"内置检索就绪（可选外部加速 {', '.join(missing)} 将按需在审计阶段使用）"
            efficiency_detail = efficiency.get("status", "DEGRADED")
            if missing:
                efficiency_detail += f"；待补充 {', '.join(missing)}"
            efficiency_detail += "；内置回退可用"
            return False, efficiency_detail
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, CliError) as exc:
            return (True, f"内置检索可用（探测异常已跳过：{exc}）") if not strict else (False, f"DEGRADED：{exc}")


def _mcp_hygiene_check_row(root: Path) -> tuple[bool, str]:
    # DOC-230 3.2 第 6 项：MCP 会话进程卫生（阈值化 + advisory）。传 state_root
    # 以做**活动感知**判定，避免把长命但健康的会话误报为泄漏；拿不到状态根时
    # 退化为跳过遗弃判定。
    try:
        import core.bugx_paths as bugx_paths

        mcp_state_root = bugx_paths.require(root).state_root
    except Exception:  # noqa: BLE001 - 拿不到状态根时退化为跳过遗弃判定
        mcp_state_root = None
    return _mcp_session_hygiene_check(root, mcp_state_root)


def _ui_env_check_row(root: Path, ui_script: Path, *, strict: bool = False) -> tuple[bool, str]:
    try:
        ui_res = subprocess.run(
            [sys.executable, str(ui_script), "status"],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
            env=_python_environment(),
        )
        if ui_res.returncode == 0:
            return True, "UI/TUI 环境就绪（Playwright / TUI 可用）"
        missing_lines = [
            line.strip() for line in (ui_res.stdout or "").splitlines()
            if "缺失" in line or "✗" in line
        ]
        detail = missing_lines[0] if missing_lines else (
            "部分依赖缺失；运行 python3 setup_ui_env.py install 补装"
        )
        if not strict:
            return True, f"按需延迟启用（进入 UI/E2E 审计阶段时按项目栈检查；当前：{detail}）"
        return False, detail
    except Exception as exc:
        return (True, f"按需延迟启用（跳过：{exc}）") if not strict else (False, f"检测跳过: {exc}")


def _bugx_cli_path_check_row() -> tuple[bool, str]:
    """BugX CLI 路径检查（只读：PATH 扫描 + 对每个入口采样 --version）。"""
    cli_paths = _bugx_cli_paths()
    if len(cli_paths) > 1:
        inspected = [_inspect_cli_entry(p) for p in cli_paths]
        active_entry = shutil.which("bugx") or (str(cli_paths[0]) if cli_paths else None)
        active_path = Path(active_entry).resolve() if active_entry else cli_paths[0]

        has_npm = any(item["type"] == "npm" for item in inspected)
        has_pip = any(item["type"] in ("pip", "uv", "pyenv", "python") for item in inspected)
        versions = {item["version"] for item in inspected if item["version"]}

        parts_desc = []
        for item in inspected:
            is_active = (item["path"] == active_path)
            tag = "生效中" if is_active else "被覆盖"
            ver_str = f" v{item['version']}" if item["version"] else ""
            parts_desc.append(f"{item['path']} ({item['type']}{ver_str}, {tag})")

        if has_npm and has_pip:
            if len(versions) > 1:
                cli_detail = (
                    "发现 npm 与 pip/uv 双重安装且版本冲突："
                    + "；".join(parts_desc)
                    + "。建议卸载多余版本：npm 运行 npm uninstall -g bugx-cli，"
                    "pip 运行 pip uninstall bugx-cli 或 uv tool uninstall bugx-cli"
                )
            else:
                cli_detail = (
                    "发现 npm 与 pip/uv 均存在安装（当前版本一致）："
                    + "；".join(parts_desc)
                    + "。建议保留单一包管理器以避免后续升级漂移"
                )
        else:
            cli_detail = "发现多个 PATH 中的 BugX 入口，可能存在安装覆盖：" + "；".join(parts_desc)
    elif cli_paths:
        cli_detail = str(cli_paths[0])
    else:
        cli_detail = "PATH 中未发现 bugx 入口"
    return len(cli_paths) <= 1, cli_detail


def _doctor_parallel_reads(
    root: Path,
    *,
    layout: Any,
    initialized: bool,
    installed_host_names: set[str],
    target_hosts: set[str] | None,
    strict: bool = False,
    is_home_scope: bool = False,
) -> dict[str, Any]:
    """并发执行 doctor 中互相独立的【纯读取】检查（仅非 --repair 运行态调用）。

    只收录同时满足以下条件的检查：
    1. 纯读取：只读文件系统、进程表或做网络探测，无写副作用；
    2. 互相独立：不依赖其他并行检查的计算结果；
    3. 依赖已就绪：依赖 layout/initialized/宿主清单的检查在调用方串行
       前置段完成依赖计算后才注册。

    返回 ``{key: ("ok", value) | ("error", exc)}``；渲染处按原始行序取用，
    意外异常在取用处原样重抛（与串行失败语义一致）。
    """
    from .agent import _check_host_mcp_config
    from .lifecycle import _global_opencode_integrity_check, _global_pi_integrity_check

    jobs: dict[str, Any] = {
        "engine_integrity": _engine_integrity_check_row,
        "version_contract": lambda: _version_contract_check_row(root),
        "engine_replicas": _engine_replica_consistency_check,
        "detected_hosts": lambda: {item["name"]: item for item in discover_hosts(root)},
        "codebase_memory": lambda: _codebase_memory_check_row(root, check_project_index=bool(strict and not is_home_scope)),
        "efficiency_tools": lambda: _efficiency_tools_check_row(root, strict=strict),
        "cli_paths": _bugx_cli_path_check_row,
        "global_lesson_ledger": lambda: _global_lesson_ledger_check_row(root, layout),
    }
    if not is_home_scope:
        jobs["worktree_hygiene"] = lambda: _check_nested_worktrees_hygiene(root)
        jobs["cloud_endpoint"] = lambda: _cloud_endpoint_check(root)
        jobs["mcp_hygiene"] = lambda: _mcp_hygiene_check_row(root)
        ui_script = _script(root, "setup_ui_env.py")
        if ui_script.is_file():
            jobs["ui_env"] = lambda: _ui_env_check_row(root, ui_script, strict=strict)
    if strict or default_mcp_runtime_root().exists():
        jobs["mcp_runtime"] = lambda: _mcp_runtime_check(root, strict=strict)
    if not is_home_scope and "opencode" in installed_host_names and (target_hosts is None or "opencode" in target_hosts):
        jobs["opencode_integrity"] = _global_opencode_integrity_check
    if not is_home_scope and "pi" in installed_host_names and (target_hosts is None or "pi" in target_hosts):
        jobs["pi_integrity"] = lambda: _global_pi_integrity_check(root)
    for host_name in sorted(installed_host_names):
        if target_hosts is not None and host_name not in target_hosts:
            continue
        try:
            get_adapter(host_name)
        except ValueError:
            # 未识别宿主：渲染处按原逻辑输出结构化失败行，不进入并行探测。
            continue
        jobs[f"mcp_config:{host_name}"] = (
            lambda host=host_name: _check_host_mcp_config(root, host, repair=False)
        )
    if not is_home_scope and initialized:
        lesson_json = (
            layout.knowledge_seed if layout is not None
            else root / ".bugx" / "knowledge" / "lesson-ledger.json"
        )
        jobs["lesson_ledger"] = lambda: _lesson_ledger_check_row(root, lesson_json)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_DOCTOR_PARALLEL_MAX_WORKERS) as pool:
        futures = {key: pool.submit(_doctor_probe_job, fn) for key, fn in jobs.items()}
        return {key: future.result() for key, future in futures.items()}


def command_doctor(args: argparse.Namespace) -> int:
    from .agent import _check_host_mcp_config
    from .lifecycle import _global_opencode_integrity_check, _global_pi_integrity_check
    from .agent import _CliProgress

    progress = _CliProgress(args)
    progress.update(0, "准备诊断")
    root = args.root.resolve()
    strict = bool(getattr(args, "strict", False))
    is_home_scope = _is_global_home_scope(root)
    if getattr(args, "target", "all") == "mcp-runtime":
        progress.update(20, "正在诊断 MCP runtime")
        runtime_root = default_mcp_runtime_root()
        code, payload = _mcp_runtime_doctor_payload(root, runtime_root)
        progress.finish("MCP runtime 诊断完成")
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"BugX {get_full_version()} · MCP 离线 runtime 诊断")
            for item in payload.get("checks", []):
                marker = "✓" if item["ok"] else "!"
                print(f"  {marker} {item['name']}: {item['detail']}")
            for issue in payload.get("issues", []):
                print(f"  ! 不一致：{issue}")
        return code
    checks = []
    checks.append(("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    try:
        assets = asset_root()
        checks.append(("安装资源", True, str(assets)))
    except CliError as exc:
        checks.append(("安装资源", False, str(exc)))
    manifest_ok = False if is_home_scope else (root / ".bugx" / "manifest.json").is_file()
    layout = None
    layout_error = None
    repair_actions: list[str] = []
    init_issues: list[str] = []
    home_residuals: list[str] = []

    if is_home_scope:
        home_bugx = _global_bugx_dir()
        home_residuals = _global_home_pseudo_project_residuals(home_bugx)
        if args.repair and home_residuals:
            removed = _clean_global_home_pseudo_project_residuals(home_bugx)
            if removed:
                repair_actions.append(
                    f"已清理全局目录误建伪项目文件：{', '.join(removed)}（已保留 core 与 global.sqlite3）"
                )
            home_residuals = _global_home_pseudo_project_residuals(home_bugx)
        try:
            repaired_links = _ensure_global_cli_symlinks()
            if args.repair:
                for link, status in repaired_links:
                    if status == "created":
                        repair_actions.append(f"已创建全局 CLI 软链接：{link}")
        except Exception as exc:
            if args.repair:
                repair_actions.append(f"ERROR: 全局 CLI 软链接修复失败：{exc}")
        initialized = True
        checks.append((
            "项目初始化状态",
            True,
            "用户全局模式（未指定 --root 项目，跳过项目初始化与工作区检查）",
        ))
        checks.append((".bugx 运行目录", True, f"全局运行目录：{home_bugx}"))
        if home_residuals:
            checks.append((
                "主目录全局隔离",
                False,
                f"检测到 {home_bugx} 中存在误按项目初始化的残留文件（{', '.join(home_residuals)}）；"
                "运行 `bugx doctor --repair` 自动清理（不会影响全局引擎和 global.sqlite3）",
            ))
    else:
        if manifest_ok:
            try:
                layout_module = _layout_module(root)
                if args.repair:
                    layout = layout_module.repair(root)
                    repair_actions = _doctor_repair_assets(root, layout)
                else:
                    layout = layout_module.require(root)
            except (CliError, OSError, ValueError, RuntimeError) as exc:
                layout_error = str(exc)

            if not getattr(args, "installation_only", False):
                onboarding_file = root / ".bugx" / "cloud-onboard.json"
                awaiting_first_audit = _awaiting_first_audit(root, layout)
                if not onboarding_file.is_file():
                    if not awaiting_first_audit:
                        init_issues.append("缺少云端 onboarding 记录（.bugx/cloud-onboard.json）")
                else:
                    try:
                        ob_data = json.loads(onboarding_file.read_text(encoding="utf-8"))
                        ob_state = ob_data.get("state") if isinstance(ob_data, dict) else None
                        if ob_state not in ("done", "local", "sync"):
                            init_issues.append(f"云端 onboarding 未决（状态: {ob_state}）")
                        elif ob_state in ("done", "sync"):
                            project_file = root / ".bugx-project.json"
                            if not project_file.is_file():
                                init_issues.append("开启了云端同步但缺少项目身份文件（.bugx-project.json）")
                    except Exception as exc:
                        init_issues.append(f"云端 onboarding 记录损坏: {exc}")

                startup_todo = root / ".bugx" / "startup-todo.md"
                if not startup_todo.is_file():
                    init_issues.append("缺少启动视图（.bugx/startup-todo.md）")
        else:
            init_issues.append("缺少 .bugx/manifest.json")

        if layout_error:
            init_issues.insert(0, f".bugx 项目布局校验失败: {layout_error}")

        initialized = manifest_ok and layout is not None and not init_issues
        status_guidance = (
            "先只读运行 `bugx engine --root <ROOT> status --json`；"
            "只有状态精确为 `UNINITIALIZED` 时才运行 `bugx engine --root <ROOT> pre`。"
            "返回 `FATAL_BLOCKED`、其他状态或检查失败时保留现场并停止；"
            "不得用 `bugx init`、`bugx reset` 或 `--force` 绕过。"
        )
        if initialized:
            init_detail = str(root / ".bugx")
        elif not manifest_ok:
            init_detail = f"项目状态未判定（缺少 .bugx/manifest.json）。{status_guidance}"
        else:
            init_detail = f"初始化未就绪（{'；'.join(init_issues)}）。{status_guidance}"

        checks.append(("项目初始化状态", initialized, init_detail))
        checks.append((".bugx 运行目录", layout is not None, layout_error or str(root / ".bugx")))
    raw_requested_hosts = []
    for item in getattr(args, "agent", []):
        for sub in str(item).split(","):
            sub = sub.strip()
            if sub:
                raw_requested_hosts.append(sub)
    target_hosts = set(raw_requested_hosts) if raw_requested_hosts else None

    progress.update(25, "项目和宿主检查完成")
    if is_home_scope:
        host_checks, installed_host_names = _global_home_host_checks(root, target_hosts=target_hosts)
        host_registry_fallback = False
    else:
        host_checks, installed_host_names = _host_bundle_checks(root, target_hosts=target_hosts)
        if target_hosts:
            installed_host_names = installed_host_names | target_hosts
        host_registry_fallback = any(
            name == "宿主安装清单缺失或损坏" for name, _, _ in host_checks
        )
        host_checks = [
            (
                name,
                ok,
                detail + (
                    "；Doctor 不会强制重装整套宿主资产，以免覆盖项目内本地修改；"
                    "先检查并保存目标文件，再对单个宿主执行 `bugx install --root <ROOT> --agent <HOST>`。"
                ) if not ok and name.endswith("完整适配包") else detail,
            )
            for name, ok, detail in host_checks
        ]
    # ---- 并行只读探测批（W-002 遗留性能项）：仅非 --repair 运行态启用 ----
    # --repair 的全部写路径（资产修复、引擎重放、清单自愈、pip 补装、MCP 配置
    # 修复）与 --installation-only 模式都保持原串行路径，绝不进入并行块。
    # 检查行仍按原始顺序渲染：这里先并发算完互相独立的纯读取检查，
    # 后续渲染处按各自原始行序从结果中取值。
    parallel_results: dict[str, Any] = {}
    if (not getattr(args, "repair", False)
            and not getattr(args, "installation_only", False)):
        progress.update(30, "并发执行独立只读检查")
        parallel_results = _doctor_parallel_reads(
            root,
            layout=layout,
            initialized=initialized,
            installed_host_names=installed_host_names,
            target_hosts=target_hosts,
            strict=strict,
            is_home_scope=is_home_scope,
        )
        progress.update(42, "独立只读检查完成")
    if is_home_scope:
        wt_ok, wt_detail = True, "跳过（用户全局模式）"
        checks.append(("工作树卫生与嵌套隔离", wt_ok, wt_detail))
        checks.append((_CLOUD_ENDPOINT_CHECK_NAME, True, "跳过（用户全局模式）"))
    else:
        wt_ok, wt_detail = (
            _unwrap_parallel_result(parallel_results, "worktree_hygiene")
            if "worktree_hygiene" in parallel_results
            else _check_nested_worktrees_hygiene(root)
        )
        checks.append(("工作树卫生与嵌套隔离", wt_ok, wt_detail))
        if getattr(args, "installation_only", False):
            checks.append((_CLOUD_ENDPOINT_CHECK_NAME, True, "跳过（--installation-only）"))
        elif "cloud_endpoint" in parallel_results:
            checks.append(_unwrap_parallel_result(parallel_results, "cloud_endpoint"))
        else:
            checks.append(_cloud_endpoint_check(root))

    if args.repair:
        repair_errors = [item for item in repair_actions if item.startswith("ERROR:")]
        checks.append((
            "项目资产修复",
            not repair_errors,
            "；".join(repair_errors or repair_actions or ["无需修复"]),
        ))
    checks.extend(host_checks)
    if "engine_integrity" in parallel_results:
        engine_ok, engine_detail = _unwrap_parallel_result(parallel_results, "engine_integrity")
    else:
        engine_ok, engine_detail = _engine_integrity_check_row()
    if not engine_ok and getattr(args, "repair", False):
        can_replace, guard_detail = _asset_can_replace_global_engine()
        if not can_replace:
            engine_detail = guard_detail
        else:
            try:
                _install_engine(asset_root(), force=True)
                engine_ok, engine_detail = _engine_integrity_status(
                    asset_root(), global_engine_root(),
                )
                engine_detail += "（已自动修复恢复完整性）"
            except Exception as exc:
                engine_detail = f"自动修复全局引擎失败：{exc}"
    if engine_ok and not (global_engine_root() / "bundled_docs" / "manifest.json").is_file():
        engine_detail += "（缺少官方离线文档基线 bundled_docs，建议运行 `bugx update --repair-assets` 补齐）"
    checks.append(("全局引擎完整性", engine_ok, engine_detail))
    if "version_contract" in parallel_results:
        version_ok, version_detail = _unwrap_parallel_result(parallel_results, "version_contract")
    else:
        version_ok, version_detail = _version_contract_check_row(root)
    # 项目 manifest 只存语义版本；它的基础版本自愈必须独立于完整 Build ID 比对。
    if version_ok and getattr(args, "repair", False):
        healed = _heal_project_manifest_version(root)
        if healed is not None:
            version_detail += f"（已就地对齐项目清单版本 {healed[0]} → {healed[1]}）"
    if not version_ok and getattr(args, "repair", False):
        can_replace, guard_detail = _asset_can_replace_global_engine()
        if not can_replace:
            version_detail = guard_detail
        else:
            try:
                _install_engine(asset_root(), force=True)
                _heal_project_manifest_version(root, force=True)
                version_ok, version_detail = _version_contract_status(
                    root, global_engine_root(),
                )
                if version_ok:
                    version_detail += "（已自动刷新全局引擎与项目清单；旧项目入口会在 init/install 时直接删除）"
            except Exception as exc:
                version_detail = f"自动对齐版本失败：{exc}"
    checks.append(("BugX 版本一致性", version_ok, version_detail))
    if "engine_replicas" in parallel_results:
        replicas_ok, replicas_detail = _unwrap_parallel_result(parallel_results, "engine_replicas")
    else:
        replicas_ok, replicas_detail = _engine_replica_consistency_check()
    checks.append(("引擎副本一致性", replicas_ok, replicas_detail))
    progress.update(45, "引擎和版本检查完成")
    if not is_home_scope and "opencode" in installed_host_names and (target_hosts is None or "opencode" in target_hosts):
        if "opencode_integrity" in parallel_results:
            checks.append((
                "OpenCode 全局入口完整性",
                *_unwrap_parallel_result(parallel_results, "opencode_integrity"),
            ))
        else:
            checks.append(("OpenCode 全局入口完整性", *_global_opencode_integrity_check()))
    if not is_home_scope and "pi" in installed_host_names and (target_hosts is None or "pi" in target_hosts):
        if "pi_integrity" in parallel_results:
            checks.append((
                "Pi 全局 Skill 完整性",
                *_unwrap_parallel_result(parallel_results, "pi_integrity"),
            ))
        else:
            checks.append(("Pi 全局 Skill 完整性", *_global_pi_integrity_check(root)))
    detected_hosts = (
        _unwrap_parallel_result(parallel_results, "detected_hosts")
        if "detected_hosts" in parallel_results
        else {item["name"]: item for item in discover_hosts(root)}
    )
    for host_name in sorted(installed_host_names):
        if target_hosts is not None and host_name not in target_hosts:
            continue
        if host_name not in {adapter.name for adapter in HOST_ADAPTERS}:
            # hosts.json 可能由更新版本的 CLI 写入（降级/前向兼容场景）。
            # doctor 必须结构化报告未知宿主，禁止以未处理异常崩溃。
            checks.append((
                f"{host_name} MCP 配置", False,
                "当前 CLI 不识别该宿主（可能由更新版本的 BugX 安装）；请升级 CLI 后重试",
            ))
            continue
        mcp_config_key = f"mcp_config:{host_name}"
        if mcp_config_key in parallel_results:
            ok, detail = _unwrap_parallel_result(parallel_results, mcp_config_key)
        else:
            repair_flag = bool(getattr(args, "repair", False) and not host_registry_fallback)
            ok, detail = _check_host_mcp_config(
                root, host_name,
                repair=repair_flag,
            )
            if repair_flag and not ok:
                if _repair_active_host_mcp_config(root, host_name, is_home_scope=is_home_scope):
                    ok, detail = _check_host_mcp_config(root, host_name, repair=False)
                    if ok:
                        refreshed_bundle_checks, _ = (
                            _global_home_host_checks(root, target_hosts={host_name})
                            if is_home_scope
                            else _host_bundle_checks(root, target_hosts={host_name})
                        )
                        refreshed_map = {n: (n, b_ok, b_det) for n, b_ok, b_det in refreshed_bundle_checks}
                        checks = [refreshed_map.get(item[0], item) for item in checks]
        adapter = get_adapter(host_name)
        checks.append((f"{adapter.label} MCP 配置", ok, detail))
    is_installation_only = bool(getattr(args, "installation_only", False))

    def _run_required_tools_checks(*, check_project_index: bool) -> None:
        ts_repair_err: str | None = None
        try:
            _ensure_core_importable()
            from core.repair.self_heal import (
                ensure_codebase_memory_ready,
                ensure_tree_sitter_ready,
                mount_bugx_pydeps,
            )
            mount_bugx_pydeps()
            importlib.invalidate_caches()
        except Exception as exc:
            ensure_codebase_memory_ready = None  # type: ignore[assignment]
            ensure_tree_sitter_ready = None  # type: ignore[assignment]
            ts_repair_err = str(exc)

        graph = importlib.util.find_spec("tree_sitter_language_pack") is not None
        if not graph and getattr(args, "repair", False) and ensure_tree_sitter_ready is not None:
            ts_ok, ts_msg = ensure_tree_sitter_ready()
            importlib.invalidate_caches()
            graph = importlib.util.find_spec("tree_sitter_language_pack") is not None
            if not ts_ok and not graph:
                ts_repair_err = ts_msg
        graph_detail = (
            "已就绪（tree-sitter-language-pack）"
            if graph
            else f"NOT READY（必需核心依赖，不可降级；{ts_repair_err or '运行 `bugx doctor --repair` 或 `bugx install` 自动安装'}）"
        )
        checks.append(("Tree-sitter 语法解析引擎", graph, graph_detail))

        if "codebase_memory" in parallel_results and not getattr(args, "repair", False):
            cbm_ok, cbm_detail = _unwrap_parallel_result(parallel_results, "codebase_memory")
        else:
            cbm_ok, cbm_detail = _codebase_memory_check_row(
                root, check_project_index=check_project_index,
            )
        if not cbm_ok and getattr(args, "repair", False) and ensure_codebase_memory_ready is not None:
            repaired_cbm_ok, repaired_cbm_msg = ensure_codebase_memory_ready(root)
            cbm_ok, cbm_detail = _codebase_memory_check_row(
                root, check_project_index=check_project_index,
            )
            if not cbm_ok and not repaired_cbm_ok:
                cbm_detail = f"NOT READY（必需核心依赖，不可降级）：{repaired_cbm_msg}"
        checks.append(("codebase-memory 代码图谱", cbm_ok, cbm_detail))

    def _run_mcp_runtime_deferred_aware() -> tuple[bool, str]:
        runtime_root = default_mcp_runtime_root()
        if not strict and not runtime_root.exists():
            return True, "按需延迟构建（首次审计需要 playwright/postmcp 等外部 MCP 时再启用，不阻塞安装）"
        if "mcp_runtime" in parallel_results:
            return _unwrap_parallel_result(parallel_results, "mcp_runtime")
        return _mcp_runtime_check(root, strict=strict)

    if is_installation_only:
        lesson_ready = True
        checks.append(("错题集账本", True, "跳过运行态检查（--installation-only）"))
        if is_home_scope or layout is not None:
            checks.append(("全局错题集（可选）", True, "跳过运行态检查（--installation-only）"))
        _run_required_tools_checks(check_project_index=False)
        checks.append(("基础效率工具", True, "跳过运行态检查（--installation-only）"))
        mcp_runtime_ok, mcp_runtime_detail = _run_mcp_runtime_deferred_aware()
        checks.append(("MCP 离线 runtime", mcp_runtime_ok, mcp_runtime_detail))
        progress.update(80, "运行态检查已跳过")
        checks.append(("UI 视觉与浏览器挖掘环境（可选）", True, "跳过运行态检查（--installation-only）"))
    elif is_home_scope:
        lesson_ready = True
        checks.append(("错题集账本", True, "跳过（用户全局模式）"))
        if "global_lesson_ledger" in parallel_results:
            global_ok, global_detail = _unwrap_parallel_result(
                parallel_results, "global_lesson_ledger",
            )
        else:
            global_ok, global_detail = _global_lesson_ledger_check_row(root, None)
        checks.append(("全局错题集（可选）", global_ok, global_detail))
        _run_required_tools_checks(check_project_index=False)
        if "efficiency_tools" in parallel_results:
            efficiency_ok, efficiency_detail = _unwrap_parallel_result(
                parallel_results, "efficiency_tools",
            )
        else:
            efficiency_ok, efficiency_detail = _efficiency_tools_check_row(root, strict=strict)
        checks.append(("基础效率工具", efficiency_ok, efficiency_detail))
        mcp_runtime_ok, mcp_runtime_detail = _run_mcp_runtime_deferred_aware()
        checks.append(("MCP 离线 runtime", mcp_runtime_ok, mcp_runtime_detail))
        checks.append(("MCP 会话进程卫生", True, "跳过（用户全局模式）"))
        progress.update(80, "全局环境检查完成")
    else:
        lesson_json = layout.knowledge_seed if layout is not None else root / ".bugx" / "knowledge" / "lesson-ledger.json"
        if "lesson_ledger" in parallel_results:
            lesson_ready, lesson_detail = _unwrap_parallel_result(parallel_results, "lesson_ledger")
        elif initialized:
            lesson_ready, lesson_detail = _lesson_ledger_check_row(root, lesson_json)
        else:
            lesson_ready, lesson_detail = False, "项目尚未初始化"
        checks.append(("错题集账本", lesson_ready, lesson_detail))
        if layout is not None:
            if "global_lesson_ledger" in parallel_results:
                global_ok, global_detail = _unwrap_parallel_result(
                    parallel_results, "global_lesson_ledger",
                )
            else:
                global_ok, global_detail = _global_lesson_ledger_check_row(root, layout)
            checks.append(("全局错题集（可选）", global_ok, global_detail))
        _run_required_tools_checks(check_project_index=bool(strict and not is_home_scope))
        if "efficiency_tools" in parallel_results:
            efficiency_ok, efficiency_detail = _unwrap_parallel_result(
                parallel_results, "efficiency_tools",
            )
        else:
            efficiency_ok, efficiency_detail = _efficiency_tools_check_row(root, strict=strict)
        checks.append(("基础效率工具", efficiency_ok, efficiency_detail))
        mcp_runtime_ok, mcp_runtime_detail = _run_mcp_runtime_deferred_aware()
        checks.append(("MCP 离线 runtime", mcp_runtime_ok, mcp_runtime_detail))
        # DOC-230 3.2 第 6 项：MCP 会话进程卫生（阈值化 + advisory，不进
        # installation_required —— 审计 Q7）。state_root 的**活动感知**判定与
        # 拿不到状态根时的退化跳过都保留在 _mcp_hygiene_check_row 内。
        if "mcp_hygiene" in parallel_results:
            mcp_hygiene_ok, mcp_hygiene_detail = _unwrap_parallel_result(
                parallel_results, "mcp_hygiene",
            )
        else:
            mcp_hygiene_ok, mcp_hygiene_detail = _mcp_hygiene_check_row(root)
        checks.append(("MCP 会话进程卫生", mcp_hygiene_ok, mcp_hygiene_detail))
        progress.update(80, "运行态检查完成")
        # DOC-065: UI 视觉与浏览器挖掘环境（按需延迟，除非 --strict）
        ui_script = _script(root, "setup_ui_env.py")
        if "ui_env" in parallel_results:
            ui_ok, ui_detail = _unwrap_parallel_result(parallel_results, "ui_env")
            checks.append(("UI 视觉与浏览器挖掘环境（可选）", ui_ok, ui_detail))
        elif ui_script.is_file():
            checks.append(("UI 视觉与浏览器挖掘环境（可选）", *_ui_env_check_row(root, ui_script, strict=strict)))
    if "cli_paths" in parallel_results:
        cli_ok, cli_detail = _unwrap_parallel_result(parallel_results, "cli_paths")
    else:
        cli_ok, cli_detail = _bugx_cli_path_check_row()
    checks.append(("BugX CLI 路径", cli_ok, cli_detail))
    installation_required = {
        "Python >= 3.11", "安装资源", "全局引擎完整性",
        "BugX 版本一致性",
        "引擎副本一致性",
        "Tree-sitter 语法解析引擎",
        "codebase-memory 代码图谱",
    }
    if is_home_scope and home_residuals:
        installation_required.add("主目录全局隔离")
    # F-L0-01 整改：``--installation-only`` 是「安装体检」，不得因为目标目录
    # 尚未初始化（没有 .bugx/manifest.json）而判定安装失败——安装完整性与
    # 项目状态是两个独立维度。但**已存在项目**（manifest 已落盘）却状态损坏
    # 时必须继续 fail-closed（DOC-227 半初始化拒绝放行），因此仅在
    # 「manifest 存在」或「非 installation-only 模式」时才把项目初始化状态
    # 计入必需项。
    if not is_home_scope and (manifest_ok or not args.installation_only):
        installation_required.add("项目初始化状态")
    if not is_home_scope and initialized and not args.installation_only:
        installation_required.add("错题集账本")
    if strict and not is_home_scope:
        installation_required.add("工作树卫生与嵌套隔离")
        installation_required.add(_CLOUD_ENDPOINT_CHECK_NAME)
    if args.repair:
        installation_required.add("项目资产修复")
    if strict or default_mcp_runtime_root().exists():
        installation_required.add("MCP 离线 runtime")
    installation_required.update(name for name, _, _ in host_checks)
    for host_name in sorted(installed_host_names):
        if target_hosts is not None and host_name not in target_hosts:
            continue
        try:
            host_label = get_adapter(host_name).label
        except ValueError:
            host_label = host_name
        installation_required.add(f"{host_label} MCP 配置")
    if not is_home_scope and "opencode" in installed_host_names:
        installation_required.update({"OpenCode MCP 配置", "OpenCode 全局入口完整性"})
    if not is_home_scope and "pi" in installed_host_names:
        installation_required.add("Pi 全局 Skill 完整性")
    installation_ready = all(
        ok for name, ok, detail in checks if name in installation_required
    )
    if installed_host_names:
        host_runtime_ready = _host_runtime_gate(
            host_checks, installed_host_names, detected_hosts,
        )
        runtime_ready = installation_ready and lesson_ready and host_runtime_ready
    else:
        # BugX core is host-neutral.  With no host selected, doctor validates
        # the core and knowledge stores only; an AI host may be installed later.
        runtime_ready = installation_ready and lesson_ready
    if (default_mcp_runtime_root()).exists():
        # 已安装过 runtime 时才参与就绪门禁；未安装只是可选项，不阻断。
        runtime_ready = runtime_ready and mcp_runtime_ok
    # 可选项（Tree-sitter、codebase-memory、效率工具、UI 环境等）失败只降级，
    # 不再把退出码拖到 1；只有 --strict 时才把它们计入。
    optional_degraded = [
        name for name, ok, _detail in checks
        if not ok and name not in installation_required
    ]
    if args.installation_only:
        ready = installation_ready
    else:
        ready = runtime_ready if strict else installation_ready
    failed_required = [
        {"name": name, "detail": detail}
        for name, ok, detail in checks
        if not ok and name in installation_required
    ]
    auto_repairable_checks = _doctor_auto_repairable_checks(
        checks,
        repair_requested=bool(getattr(args, "repair", False)),
        installation_only=is_installation_only,
    )
    ai_diag = _generate_ai_diagnostics(
        checks,
        installation_required,
        root,
        strict=strict,
        auto_repairable_checks=auto_repairable_checks,
        use_ai=bool(getattr(args, "ai", False)),
    )
    escalation = None
    if not ready:
        escalation = {
            "required": True,
            "failed_checks": failed_required,
            "suggested_llm_action": "diagnose_environment_failure",
            "message": "程序级检查未完全通过，已生成 AI 智能诊断分析与自愈处方",
            "ai_prescriptions": [
                {
                    "name": item["name"],
                    "root_cause": item["root_cause"],
                    "prescription": item["prescription"],
                    "auto_repairable": item["auto_repairable"],
                }
                for item in ai_diag.get("items", [])
                if item.get("required")
            ],
            "auto_repair_command": ai_diag.get("auto_repair_command"),
        }
    payload = {
        "version": __version__, "root": str(root),
        "mode": "installation-only" if args.installation_only else "runtime",
        "installation_ready": installation_ready,
        "runtime_ready": runtime_ready,
        "optional_degraded": optional_degraded,
        "strict": strict,
        "ready": ready,
        "escalation": escalation,
        "ai_diagnostics": ai_diag,
        "checks": [{"name": name, "ok": ok, "detail": detail} for name, ok, detail in checks],
    }
    progress.finish("诊断完成")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"BugX {get_full_version()}")
        for name, ok, detail in checks:
            optional_miss = (not ok) and name not in installation_required
            marker = "✓" if ok else ("△" if optional_miss else "!")
            suffix = "（可选增强缺失，不影响退出码）" if optional_miss and not strict else ""
            print(f"  {marker} {name}: {detail}{suffix}")
        if getattr(args, "ai", False) or not payload["ready"] or (strict and optional_degraded):
            _render_ai_diagnostics_panel(ai_diag, is_explicit_ai=bool(getattr(args, "ai", False)))
    return 0 if payload["ready"] else 1
