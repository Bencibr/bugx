#!/usr/bin/env python3
"""src/bugx_cli/infra/mcp_advisor.py - 宿主 MCP 配置建议、模板发现与 Runtime 诊断 (DOC-070)。"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any

from .engine_installer import _distribution_located_path, _script, asset_root
from .security import CliError, _find_repo_root, _i18n_exc

CONFIG_NAME = "opencode.json"
LOCK_NAME = "mcp-lock.json"
MCP_RUNTIME_COMMAND = "mcp-runtime"
BUILTIN_MCP_MANIFEST_NAME = "bugx-mcp-manifest.json"

OPENCODE_MCP_PACKAGES = {
    "playwright": "@playwright/mcp",
    "postmcp": "@bencibro/postmcp",
    "codindex": "codindex",
    "dbhub": "@bytebase/dbhub",
    "redis": "redis-mcp-server",
    "semgrep": "semgrep",
    "mobile": "@mobilenext/mobile-mcp",
}

# 本地内建通道的启动命令（与外部 MCP 的 runtime launcher 形态不同）。
_LOCAL_MCP_COMMANDS = {
    "bugx-supervisor": ["bugx", "mcp", "serve", "--root", ".", "--role", "host"],
    "bugx-supervisor-worker": ["bugx", "mcp", "serve", "--root", ".", "--role", "worker"],
    "bugx-cloud": [
        "bugx", "mcp", "docs", "serve", "--root", ".",
        "--db", ".bugx/knowledge/docs.sqlite3",
    ],
}

# 宿主固定命令（**派生**，不逐条硬编码）：外部 MCP 一律走 BugX 离线 runtime
# launcher；名字集合以 OPENCODE_MCP_PACKAGES 为唯一来源（消除重复条目，
# 由 test_consistency 的跨触点一致性守护兜底）。
OPENCODE_PINNED_MCP_COMMANDS = {
    **{
        name: ["bugx", MCP_RUNTIME_COMMAND, "start", "--server", name]
        for name in OPENCODE_MCP_PACKAGES
    },
    **_LOCAL_MCP_COMMANDS,
}

REQUIRED_MCP_RECOMMENDATIONS = (
    {
        "name": "postmcp",
        "purpose": "API 请求、状态码和响应契约验证",
        "command": "bugx mcp-runtime install --timeout 30 && bugx mcp-runtime start --server postmcp",
        "install_hint": ("API 路由项目才建议装配；安装最多等待 30 秒。"
                         "自 1.0.4 起改用 Node 内置 node:sqlite，零原生依赖，"
                         "兼容 npm --ignore-scripts 离线安装（见 AUDIT-postmcp-supply-chain.md）"),
    },
    {
        "name": "playwright",
        "purpose": "Web UI、浏览器交互和网络证据",
        "command": "bugx mcp-runtime install --timeout 30 && bugx mcp-runtime start --server playwright",
        "install_hint": "仅 Web UI 项目装配；安装最多等待 30 秒，失败时跳过该工具并继续本地模式。",
    },
    {
        "name": "codindex",
        "purpose": "本地结构化代码索引、调用图和影响面查询",
        "command": "bugx mcp-runtime install --timeout 30 && bugx mcp-runtime start --server codindex",
        "install_hint": ("需要 Node.js >=22.5；默认使用 Node 内置 node:sqlite。"
                         "不要设置 CODINDEX_SQLITE=better-sqlite3，避免原生 ABI/安装脚本依赖。"),
    },
    {
        "name": "bugx-supervisor",
        "purpose": "本地生命周期、并发租约、心跳和 checkpoint 协调",
        "command": "bugx mcp serve --root . --role host",
        "install_hint": "由宿主通过本地 stdio MCP 自动启动；不会执行任意 worker 命令",
    },
    {
        "name": "bugx-supervisor-worker",
        "purpose": "Worker 专用租约、启动确认、心跳和 checkpoint 通道",
        "command": "bugx mcp serve --root . --role worker",
        "install_hint": "只暴露 Worker 控制面；宿主主控工具不会在此通道提供",
    },
    {
        "name": "bugx-cloud",
        "purpose": "项目私有版本化文档检索与 citation",
        "command": "bugx mcp docs serve --root . --db .bugx/knowledge/docs.sqlite3",
        "install_hint": "内置 SQLite FTS5 文档服务；首次启动自动索引 README/docs/core Markdown，"
                       "不需要 Context7 API Key，也不访问第三方文档后端",
    },
)


def _is_incompatible_managed_mcp_command(name: str, tokens: list[str]) -> bool:
    """Detect retired BugX-managed MCP command shapes without rewriting them."""
    package = OPENCODE_MCP_PACKAGES.get(name)
    launcher = Path(tokens[0]).name.casefold() if tokens else ""
    launcher = launcher.removesuffix(".exe").removesuffix(".cmd")
    package_tokens = tokens[:1]
    if launcher in {"npx", "npm", "pnpm", "yarn", "bun", "uvx", "pipx"}:
        # Package manager invocations place the retired package near the
        # executable; later MCP arguments may legitimately repeat the server
        # name (for example ``--server semgrep``).
        package_tokens = tokens[1:5]
    if package and any(
        item == package or item.startswith(f"{package}@")
        for item in package_tokens
    ):
        return True
    return name == "bugx-supervisor" and tokens == [
        "bugx", "mcp", "serve", "--root", ".",
    ]


def _mcp_entry_state(name: str, entry: object) -> str:
    """Classify a required MCP without executing or resolving external code."""
    if not isinstance(entry, dict):
        return "recommended"
    if entry.get("disabled") is True or entry.get("enabled") is False:
        return "disabled"
    if "enabled" in entry:
        return "incompatible"
    if name == "bugx-cloud" and isinstance(entry.get("url"), str) and entry["url"].strip():
        try:
            from core.host_runtime.mcp_process_tracker import probe_remote_docs_mcp
            probe = probe_remote_docs_mcp({"bugx-cloud": entry})
            return "configured" if probe.get("healthy") else "incompatible"
        except Exception:
            return "incompatible"
    command = entry.get("command")
    tokens = command if isinstance(command, list) else []
    tokens = [item for item in tokens if isinstance(item, str)]
    if _is_incompatible_managed_mcp_command(name, tokens):
        return "incompatible"
    if tokens == OPENCODE_PINNED_MCP_COMMANDS.get(name):
        return "configured"
    return "custom"


def _config_candidates() -> list[Path]:
    repository_root = _find_repo_root(Path(__file__))
    candidates = [
        repository_root / "ecosystem" / "integrations" / "opencode" / CONFIG_NAME,
        repository_root / "integrations" / "opencode" / CONFIG_NAME,
    ]
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        for item in distribution.files or []:
            if item.as_posix().endswith(f"share/bugx-agent/{CONFIG_NAME}"):
                located = _distribution_located_path(distribution.locate_file(item))
                if located is not None:
                    candidates.append(located)
    except importlib.metadata.PackageNotFoundError:
        pass
    return list(dict.fromkeys(path.resolve() for path in candidates))


def _integration_dir() -> Path:
    """Return the bundled integrations directory containing host templates."""
    repository_root = _find_repo_root(Path(__file__))
    candidates = [
        repository_root / "ecosystem" / "integrations",
        repository_root / "integrations",
        Path(sysconfig.get_path("data")) / "share" / "bugx-agent",
    ]
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        for item in distribution.files or []:
            if item.as_posix().endswith(
                f"share/bugx-agent/{BUILTIN_MCP_MANIFEST_NAME}"
            ):
                located = _distribution_located_path(distribution.locate_file(item))
                if located is not None:
                    candidates.insert(0, located.parent)
    except importlib.metadata.PackageNotFoundError:
        pass
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return (repository_root / "ecosystem" / "integrations") if (repository_root / "ecosystem" / "integrations").is_dir() else (repository_root / "integrations")


def bundled_config() -> Path:
    for path in _config_candidates():
        if path.is_file():
            return path
    raise CliError(_i18n_exc("common.config_template_missing", f"找不到 {CONFIG_NAME} 模板；请重新安装 BugX", name=CONFIG_NAME))


def _builtin_mcp_manifest() -> Path:
    candidates = (
        _integration_dir() / "opencode" / BUILTIN_MCP_MANIFEST_NAME,
        _integration_dir() / BUILTIN_MCP_MANIFEST_NAME,
    )
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            return path
    raise CliError(f"找不到受信任 MCP manifest: {BUILTIN_MCP_MANIFEST_NAME}")


def verify_builtin_mcp_manifest() -> dict[str, object]:
    """校验内置 MCP 来源、命令和文件 hash，失败时阻止宿主安装。"""
    path = _builtin_mcp_manifest()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"受信任 MCP manifest 无法读取: {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CliError("受信任 MCP manifest schema 无效")
    servers = payload.get("servers")
    if not isinstance(servers, dict):
        raise CliError("受信任 MCP manifest 缺少 servers")
    if path.parent.name == "opencode":
        repository_root = path.parents[3] if (len(path.parents) > 3 and path.parents[2].name == "ecosystem") else path.parents[2]
    else:
        repository_root = path.parent
    for name in ("bugx-supervisor", "bugx-supervisor-worker", "bugx-cloud"):
        item = servers.get(name)
        if not isinstance(item, dict) or item.get("type") != "builtin":
            raise CliError(f"受信任 MCP manifest 缺少内置服务: {name}")
        source = item.get("source")
        digest = item.get("sha256")
        command = item.get("command")
        if not isinstance(source, str) or not isinstance(digest, str) or not isinstance(command, list):
            raise CliError(f"受信任 MCP manifest 条目无效: {name}")
        source_path = repository_root / source
        if not source_path.is_file() or source_path.is_symlink():
            source_path = asset_root() / Path(source).name
        if not source_path.is_file() or source_path.is_symlink():
            raise CliError(f"受信任 MCP 来源缺失: {name} ({source})")
        actual = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual != digest:
            integrity_file = repository_root / "core" / "engine-integrity.json"
            if integrity_file.is_file():
                try:
                    integrity_data = json.loads(integrity_file.read_text(encoding="utf-8"))
                    src_key = Path(source).name
                    expected_integrity = (integrity_data.get("files", {}).get(src_key, {}) or {}).get("sha256")
                    if expected_integrity == actual:
                        digest = actual
                        item["sha256"] = actual
                        if path.is_file():
                            try:
                                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                            except OSError:
                                pass
                except Exception:
                    pass
        if actual != digest:
            raise CliError(f"受信任 MCP hash 不匹配: {name}")
        if name == "bugx-cloud" and command[:4] != ["bugx", "mcp", "docs", "serve"]:
            raise CliError("bugx-docs manifest 命令不受信任")
    return payload


def _lock_candidates() -> list[Path]:
    repository_root = _find_repo_root(Path(__file__))
    candidates = [
        repository_root / "ecosystem" / "integrations" / "opencode" / LOCK_NAME,
        repository_root / "integrations" / "opencode" / LOCK_NAME,
        Path(sysconfig.get_path("data")) / "share" / "bugx-agent" / LOCK_NAME,
    ]
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        for item in distribution.files or []:
            if item.as_posix().endswith(f"share/bugx-agent/{LOCK_NAME}"):
                located = _distribution_located_path(distribution.locate_file(item))
                if located is not None:
                    candidates.append(located)
    except importlib.metadata.PackageNotFoundError:
        pass
    return list(dict.fromkeys(path.resolve() for path in candidates))


def bundled_lock() -> Path:
    for path in _lock_candidates():
        if path.is_file() and not path.is_symlink():
            return path
    raise CliError(_i18n_exc("common.lock_file_missing", f"找不到 {LOCK_NAME}；请重新安装 BugX", name=LOCK_NAME))


def _lock_disabled_servers() -> set[str]:
    """Servers the shipped lock marks enabled=false (SPIKE-008-01 gate)."""
    try:
        lock = json.loads(bundled_lock().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, CliError):
        return set()
    servers = lock.get("servers") if isinstance(lock, dict) else None
    if not isinstance(servers, dict):
        return set()
    return {name for name, meta in servers.items()
            if isinstance(meta, dict) and meta.get("enabled") is False}


def default_mcp_runtime_root() -> Path:
    """离线 MCP runtime 根目录（DOC-008 §12：~/.bugx/mcp-runtime）。"""
    override = os.environ.get("BUGX_MCP_RUNTIME_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bugx" / "mcp-runtime"


def installed_runtime_servers() -> set[str]:
    """当前 runtime 中**已安装且启用**的 server 名。

    宿主配置只应注入"真能启动"的条目：模板里列出 ≠ 载荷已装。实测缺陷：
    ``opencode.json`` 模板（由 MCP_REGISTRY 生成）会把 dbhub/redis/semgrep 等
    外部 server 一并写进**项目级**配置，即使它们未安装或被 lock 禁用 ⇒ 宿主启动即报错。
    """
    manifest = default_mcp_runtime_root() / "current" / "runtime-manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    servers = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(servers, dict):
        return set()
    return {
        name for name, meta in servers.items()
        if isinstance(meta, dict) and meta.get("enabled") is not False
    }


def targeted_mcp_names(root: Path) -> set[str]:
    """Select the two control channels plus only project-evidenced tools."""
    root = root.resolve()
    profile: dict[str, Any] = {}
    profile_path = root / ".bugx" / "knowledge" / "project-profile.json"
    if profile_path.is_file() and not profile_path.is_symlink():
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                profile = payload
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    if not profile:
        try:
            engine = asset_root()
            if str(engine) not in sys.path:
                sys.path.insert(0, str(engine))
            spec = importlib.util.spec_from_file_location(
                "bugx_project_profile_for_mcp_advisor", engine / "project_profile.py",
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                try:
                    detected = module.detect_project_profile(root, allow_llm=False, persist=False)
                except TypeError:
                    detected = module.detect_project_profile(root, persist=False)
                if isinstance(detected, dict):
                    profile = detected
        except (CliError, OSError, ImportError, ValueError, RuntimeError):
            pass
    classification: dict[str, Any] = {
        key: list(profile.get(key) or [])
        for key in ("forms", "surfaces", "languages", "frameworks")
    }
    inner = profile.get("classification")
    if isinstance(inner, dict):
        for key in ("forms", "surfaces", "languages", "frameworks"):
            if inner.get(key):
                classification[key] = list(inner[key])
    selected = {"bugx-supervisor", "bugx-supervisor-worker", "bugx-cloud"}
    try:
        engine = asset_root()
        repo_root = engine.parent
        for path_entry in (str(repo_root), str(engine)):
            if path_entry not in sys.path:
                sys.path.insert(0, path_entry)
        from core.planning import capability_planner

        matched, _errors = capability_planner.optional_mcp_names(
            classification, project_root=root,
        )
        selected.update(matched)
    except Exception:
        surfaces = set(classification.get("surfaces") or [])
        if "ui" in surfaces:
            selected.add("playwright")
        if {"rest", "graphql"} & surfaces:
            selected.add("postmcp")
        if classification.get("languages"):
            selected.add("codindex")
        if {"storage", "queue"} & surfaces:
            selected.update({"dbhub", "redis"})
    return selected


_PROVISION_FAILURE_TTL_SECONDS = 24 * 3600


def _provision_failure_cache_path() -> Path:
    return default_mcp_runtime_root() / ".provision-failed.json"


def _read_provision_failure_cache() -> dict[str, float]:
    """读取可选 MCP 装配的失败负缓存：{server: failed_epoch}。"""
    path = _provision_failure_cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_provision_failure_cache(servers: set[str], now: float) -> None:
    """记录装配失败的 server 与时间戳（TTL 内自动跳过重试）。"""
    path = _provision_failure_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_provision_failure_cache()
        for server in servers:
            data[server] = float(now)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"[bugx] ⚠ 装配失败缓存写入失败（不影响流程）: {exc}", file=sys.stderr)


def _recent_failures(servers: set[str], now: float) -> set[str]:
    """TTL 内失败过的 server 集合（过期条目自动视为可重试）。"""
    cache = _read_provision_failure_cache()
    fresh = {
        str(server)
        for server, failed_at in cache.items()
        if isinstance(failed_at, (int, float))
        and now - float(failed_at) < _PROVISION_FAILURE_TTL_SECONDS
    }
    return servers & fresh


def provision_targeted_mcp_runtime(root: Path, selected: set[str]) -> set[str]:
    """Install only selected, lock-enabled external servers with a 30s cap.

    F-RSI-20261112-086：装配在离线/慢网环境必吃满 30s subprocess 超时且
    无记忆——每次 install 白等 30s+（实测 33.37s vs 历史基线 3.18s）。
    修复：失败负缓存（TTL 24h），TTL 内跳过自动重试并留痕；
    ``BUGX_MCP_FORCE_PROVISION=1`` 或删除 ``.provision-failed.json`` 强制重试。
    """
    disabled = _lock_disabled_servers()
    requested = (selected & set(OPENCODE_MCP_PACKAGES)) - disabled
    if not requested:
        return set()
    now = time.time()
    if os.environ.get("BUGX_MCP_FORCE_PROVISION", "").strip() not in {"1", "true", "yes"}:
        recent = _recent_failures(requested, now)
        if recent:
            print(
                f"[bugx] ⚠ 可选 MCP {', '.join(sorted(recent))} 近 24h 内装配失败，"
                "本次跳过自动重试（BUGX_MCP_FORCE_PROVISION=1 或删除 "
                f"{_provision_failure_cache_path().name} 可强制重试）",
                file=sys.stderr,
            )
            requested -= recent
            if not requested:
                return set()

    def probe_existing() -> set[str]:
        try:
            engine = asset_root()
            if str(engine) not in sys.path:
                sys.path.insert(0, str(engine))
            spec = importlib.util.spec_from_file_location(
                "bugx_project_init_mcp_probe", engine / "project_init.py",
            )
            if spec is None or spec.loader is None:
                return set()
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            report = module._probe_and_sanitize_optional_mcps(root)
            return requested & set(report.get("available") or [])
        except (CliError, OSError, ImportError, RuntimeError, ValueError, TypeError) as exc:
            print(f"[bugx] ⚠ 可选 MCP 验活失败，已降级为内置模式：{exc}", file=sys.stderr)
            return set()

    script = _script(root, "mcp_runtime.py")
    command = [
        sys.executable, str(script), "install", "--lock", str(bundled_lock()),
        "--runtime-root", str(default_mcp_runtime_root()), "--timeout", "30", "--json",
    ]
    try:
        result = subprocess.run(
            command, cwd=root, capture_output=True, text=True, check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[bugx] ⚠ 可选 MCP 安装失败，已跳过 {', '.join(sorted(requested))}：{exc}", file=sys.stderr)
        _write_provision_failure_cache(requested, time.time())
        return probe_existing()
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        print(f"[bugx] ⚠ 可选 MCP 安装失败，已跳过 {', '.join(sorted(requested))}：{detail}", file=sys.stderr)
        _write_provision_failure_cache(requested, time.time())
    return probe_existing()


def _mcp_recommendations(
    root: Path,
    *,
    installed_hosts: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    """Report host MCP recommendations without starting external code."""
    return _collect_mcp_recommendations(root, installed_hosts=installed_hosts)


def _print_mcp_recommendations(recommendations: list[dict[str, object]]) -> None:
    """Print a compact host-neutral MCP setup recommendation."""
    print("  MCP 工具配置概览（核心通道随宿主装配；可选能力按项目栈在审计 pre 阶段启用）：")
    for item in recommendations:
        status = str(item["status"])
        marker = "✓" if status in {"configured", "disabled_by_lock", "audit_optional", "global_mode"} else "!"
        req = str(item.get("requirement") or "optional")
        if req == "required":
            requirement = "必需"
        elif req == "conditional":
            requirement = "条件必需"
        else:
            requirement = "按需（审计阶段）"
        status_label = {
            "configured": "已就绪",
            "global_mode": "全局随宿主就绪",
            "audit_optional": "延迟至审计按需启用",
        }.get(status, status)
        print(f"    {marker} {item['name']}：{requirement}；{status_label}；{item['purpose']}")
        if status in {"disabled_by_lock", "audit_optional", "global_mode"}:
            continue
        if status == "incompatible":
            hint = str(item.get("install_hint") or "")
            print(
                f"      建议：旧版 BugX MCP 命令不再接受自动迁移；请手动删除 "
                f"`mcp.servers.{item['name']}`，再运行 `{hint}`"
            )
            continue
        if status != "configured":
            hint = str(item.get("install_hint") or "")
            if status == "custom":
                hint += "；检测到自定义命令，BugX 不会覆盖，请由用户确认其版本"
            else:
                print(f"      启动命令：{item['command']}")
            if hint:
                print(f"      建议：{hint}")


def _mcp_runtime_doctor_payload(root: Path, runtime_root: Path,
                                include_lock: bool = True) -> tuple[int, dict]:
    """Run the engine-side runtime doctor and return (exit_code, payload)."""
    script_args = [
        "doctor", "--runtime-root", str(runtime_root), "--json", "--fast",
    ]
    if include_lock:
        try:
            script_args.extend(["--lock", str(bundled_lock())])
        except CliError:
            pass
    # Avoid circular import by accessing python environment lazily
    from ..common import _python_environment
    result = subprocess.run(
        [sys.executable, str(_script(root, "mcp_runtime.py")), *script_args],
        cwd=root, capture_output=True, encoding="utf-8", errors="replace", check=False,
        env=_python_environment(), timeout=120,
    )
    if result.returncode != 0 and "--fast" in script_args and "unrecognized arguments: --fast" in (result.stderr or ""):
        script_args_fallback = [a for a in script_args if a != "--fast"]
        result = subprocess.run(
            [sys.executable, str(_script(root, "mcp_runtime.py")), *script_args_fallback],
            cwd=root, capture_output=True, encoding="utf-8", errors="replace", check=False,
            env=_python_environment(), timeout=120,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CliError(_i18n_exc("common.mcp_runtime_doctor_unparseable", f"mcp_runtime doctor 输出无法解析（exit {result.returncode}）：{(result.stderr or result.stdout).strip()[:300] or exc}", code=result.returncode, detail=(result.stderr or result.stdout).strip()[:300] or exc)) from exc
    if not isinstance(payload, dict):
        raise CliError(_i18n_exc("common.mcp_runtime_doctor_must_be_object", "mcp_runtime doctor 输出必须是 JSON 对象"))
    return result.returncode, payload


def _mcp_runtime_check(root: Path, *, strict: bool = False) -> tuple[bool, str]:
    """Doctor row for the offline MCP runtime; absence fails under --strict."""
    runtime_root = default_mcp_runtime_root()
    if not runtime_root.exists():
        if strict:
            return False, (
                f"未安装离线 MCP runtime（--strict 严格模式要求就绪）：{runtime_root} 不存在；"
                "运行 bugx mcp-runtime install 可构建离线 MCP runtime"
            )
        return True, (
            f"未安装离线 MCP runtime（可选）：{runtime_root} 不存在；"
            "运行 bugx mcp-runtime install 可移除宿主 npx 启动路径"
        )
    try:
        code, payload = _mcp_runtime_doctor_payload(root, runtime_root)
    except (CliError, OSError, subprocess.TimeoutExpired) as exc:
        return False, f"runtime 诊断失败：{exc}"
    issues = payload.get("issues") or []
    current = payload.get("current") or "未设置"
    summary = f"lock={str(current)[:12]} 就绪={payload.get('ready')}"
    detail = summary if not issues else f"{summary}；问题 {'; '.join(str(i) for i in issues[:3])}"
    return code == 0 and bool(payload.get("ready")), detail


# ---------------------------------------------------------------------------
# DOC-230 3.2：MCP 会话进程卫生（被遗弃会话的**可观测**与**可清理**入口）
# ---------------------------------------------------------------------------
_MCP_HYGIENE_MAX_PROCESSES = 3
_MCP_HYGIENE_MAX_AGE_SECONDS = 3600.0


def _parse_ps_elapsed(value: object) -> int | None:
    """解析 ``ps`` 的 ``etime``（``[[dd-]hh:]mm:ss``）为秒；无法解析返回 None。

    必须用 ``etime`` 而不是 ``etimes``：macOS 的 BSD ``ps`` 不支持 ``etimes``
    关键字（实测报 ``keyword not found``），会让枚举静默返回空表。
    """
    text = str(value or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_field, _, text = text.partition("-")
        if not day_field.isdigit():
            return None
        days = int(day_field)
    fields = text.split(":")
    if len(fields) == 2:
        fields = ["0", *fields]
    if len(fields) != 3 or not all(field.isdigit() for field in fields):
        return None
    hours, minutes, seconds = (int(field) for field in fields)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _run_external(
    argv: list[str], *, timeout: float = 5.0,
) -> tuple[str | None, str | None]:
    """Run a host utility with explicit success/empty/failure states."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{argv[0]} 无法执行: {exc}"
    stdout = result.stdout or ""
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit={result.returncode}"
        return None, f"{argv[0]} 退出码 {result.returncode}: {detail[:200]}"
    return stdout, None


def _collect_mcp_recommendations(
    root: Path,
    *,
    installed_hosts: set[str] | list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    """Collect core MCP readiness and optional audit-time capability notes."""
    from .engine_installer import _is_global_home_scope

    global_scope = _is_global_home_scope(root)
    active_hosts: list[str] = sorted({str(h) for h in (installed_hosts or ()) if str(h)})
    if not active_hosts and not global_scope:
        hosts_path = root.resolve() / ".bugx" / "hosts.json"
        if hosts_path.is_file() and not hosts_path.is_symlink():
            try:
                hosts_data = json.loads(hosts_path.read_text(encoding="utf-8"))
                raw_list = hosts_data.get("installed") if isinstance(hosts_data, dict) else None
                if isinstance(raw_list, list):
                    active_hosts = sorted({
                        str(item["name"]) for item in raw_list
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    })
            except (OSError, UnicodeError, json.JSONDecodeError):
                active_hosts = []

    config = root.resolve() / CONFIG_NAME
    configured: dict[str, object] = {}
    if not global_scope and config.is_file() and not config.is_symlink():
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
            mcp = payload.get("mcp") if isinstance(payload, dict) else None
            servers = mcp.get("servers") if isinstance(mcp, dict) else None
            if isinstance(servers, dict):
                configured = servers
        except (OSError, UnicodeError, json.JSONDecodeError):
            configured = {}

    lock_disabled = _lock_disabled_servers()
    core_channels = {"bugx-supervisor", "bugx-supervisor-worker", "bugx-cloud"}
    missing_hosts: list[str] = []
    if not global_scope and active_hosts:
        try:
            from ..commands.agent import _check_host_mcp_config

            for host_name in active_hosts:
                ok_host, _ = _check_host_mcp_config(root, host_name)
                if not ok_host:
                    missing_hosts.append(host_name)
        except Exception:
            pass

    recommendations: list[dict[str, object]] = []
    for item in REQUIRED_MCP_RECOMMENDATIONS:
        name = str(item["name"])
        recommendation = dict(item)
        if name in core_channels:
            if active_hosts and not missing_hosts:
                status = "configured"
                hint = f"已在宿主（{', '.join(active_hosts)}）就绪"
            elif missing_hosts:
                status = "missing"
                hint = (
                    "请运行 `bugx install --agent "
                    + missing_hosts[0]
                    + (f" --root {root}` 补齐核心 MCP" if not global_scope else "` 补齐核心 MCP")
                )
            elif global_scope:
                status = "global_mode"
                hint = "核心通道随宿主全局资产装配；审计具体项目时自动绑定项目根"
            else:
                entry = configured.get(name)
                status = _mcp_entry_state(name, entry)
                hint = str(item.get("install_hint") or "")
            recommendation.update({
                "required": True,
                "requirement": "required",
                "required_when": "已选宿主核心协调与文档检索通道",
                "status": status,
                "install_hint": hint,
            })
            recommendations.append(recommendation)
            continue

        entry = configured.get(name)
        status = _mcp_entry_state(name, entry) if entry is not None else "audit_optional"
        if name in lock_disabled and status != "incompatible":
            status = "disabled_by_lock"
        recommendation.update({
            "required": False,
            "requirement": "optional",
            "required_when": "项目类型/审查方向需要该能力（审计 pre 阶段按需启用）",
            "status": status,
            "install_hint": (
                str(item.get("install_hint") or "")
                if status in {"custom", "incompatible"}
                else "可选能力 MCP：进入项目审计 `pre` 阶段按技术栈自动探测并询问是否启用"
            ),
        })
        recommendations.append(recommendation)
    return recommendations


def _windows_project_mcp_processes(
    root: Path,
) -> tuple[list[dict[str, object]] | None, str | None]:
    """Enumerate project-scoped BugX MCP worker processes on Windows via PowerShell CIM."""
    resolved = str(root.resolve()).lower()
    ps_script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$now=[DateTimeOffset]::UtcNow;"
        "$items=@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -and $_.CommandLine -match 'bugx_mcp_server\\.py|bugx.*mcp.*serve' "
        "} | ForEach-Object { "
        "$age=0; if ($_.CreationDate) { "
        "try { $age=[Math]::Max(0,[int]($now - [DateTimeOffset]$_.CreationDate).TotalSeconds) } catch {} "
        "}; "
        "[PSCustomObject]@{pid=[int]$_.ProcessId;ppid=[int]$_.ParentProcessId;"
        "age_seconds=[double]$age;command=[string]$_.CommandLine} "
        "});"
        "ConvertTo-Json -InputObject $items -Compress"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"unsupported_platform: windows powershell enumeration failed ({exc})"
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()[:200] or f"rc={res.returncode}"
        return None, f"unsupported_platform: windows cim query failed ({detail})"
    raw = (res.stdout or "").strip()
    if not raw or raw == "null":
        return [], None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"unsupported_platform: windows process json parse failed ({exc})"
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return [], None
    entries: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("command") or "")
        if " serve" not in cmd.lower():
            continue
        cmd_norm = cmd.lower().replace("/", "\\")
        root_win = resolved.replace("/", "\\")
        if resolved not in cmd.lower() and root_win not in cmd_norm:
            continue
        pid = item.get("pid")
        ppid = item.get("ppid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        entries.append({
            "pid": pid,
            "ppid": int(ppid) if isinstance(ppid, int) and ppid > 0 else None,
            "age_seconds": float(item.get("age_seconds") or 0.0),
            "command": cmd,
        })
    return entries, None


def _project_mcp_processes(
    root: Path,
) -> tuple[list[dict[str, object]] | None, str | None]:
    """枚举**本项目**的 MCP serve 进程（POSIX）；不支持平台或探测失败返回 None + 原因。

    三重过滤（DOC-230 3.2 第 5 项，供 `bugx mcp prune` 与 doctor 共用同一实现）：
    1. 命令行必须是 ``bugx_mcp_server.py ... serve``；
    2. ``--root`` 必须与解析后的项目根**精确匹配**（不误伤其他项目/手工进程）；
    3. **活动判定**：进程自己发布的 session proof 若比阈值更新，说明它是**活跃会话**，不得清理。
    """
    if os.name == "nt":
        return _windows_project_mcp_processes(root)
    stdout, failure = _run_external(["ps", "-eo", "pid=,ppid=,etime=,command="])
    if failure is not None:
        return None, failure
    marker = f"--root {root.resolve()}"
    entries: list[dict[str, object]] = []
    for line in (stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_field, ppid_field, age_field, command = parts
        if not pid_field.isdigit():
            continue
        age_seconds = _parse_ps_elapsed(age_field)
        if age_seconds is None:
            continue
        if "bugx_mcp_server.py" not in command or " serve" not in command:
            continue
        if marker not in command:
            continue
        entries.append({
            "pid": int(pid_field),
            # DOC-230 3.2 加固：记录父进程，供「已挂载 vs 真孤儿」判定使用。
            # 长命宿主会话里的 MCP server 天然空闲（host 只在工具调用时刷新 proof），
            # 仅凭年龄+活动缺失会把它误报为泄漏（实测 3 个会话全被判遗弃）。
            "ppid": int(ppid_field) if ppid_field.isdigit() else None,
            "age_seconds": float(age_seconds),
            "command": command,
        })
    return entries, None


def _live_pids() -> set[int] | None:
    """当前存活 PID 集合；枚举失败返回 None（调用方据此回退到保守旧判据）。"""
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            res = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_Process | ForEach-Object { $_.ProcessId }",
                ],
                capture_output=True, text=True, timeout=5, check=False,
                creationflags=creationflags,
            )
            if res.returncode != 0:
                return None
            live = {int(tok.strip()) for tok in (res.stdout or "").splitlines() if tok.strip().isdigit()}
            return live or None
        except (OSError, subprocess.SubprocessError):
            return None
    stdout, failure = _run_external(["ps", "-eo", "pid="])
    if failure is not None:
        return None
    live: set[int] = set()
    for line in (stdout or "").splitlines():
        text = line.strip()
        if text.isdigit():
            live.add(int(text))
    return live or None


def _is_attached_to_live_parent(entry: dict[str, object], live: set[int] | None) -> bool:
    """进程是否仍挂在一个**存活的非 init** 父进程下（= 会话仍在，只是空闲）。

    真泄漏的形态是**孤儿**：宿主/桥进程已退出（ppid 变 1 或父 PID 已消失）而
    server 未随之退出。因此「父进程存活且不是 init」时不得判遗弃——否则任何
    长命但健康的宿主会话都会被误报（实测误报 2/3 个会话，doctor 长期黄项）。
    """
    if live is None:
        return False
    ppid = entry.get("ppid")
    if not isinstance(ppid, int) or isinstance(ppid, bool) or ppid <= 1:
        return False
    return ppid in live



def _mcp_process_activity_age(state_root: Path, pid: int) -> float | None:
    """该 PID 自己发布的 session proof 距今秒数；无 proof 返回 None（视为遗弃）。"""
    for name in ("mcp-session-proof-host.json", "mcp-session-proof-worker.json"):
        path = state_root / name
        try:
            if path.is_symlink() or not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("pid") != pid:
            continue
        updated_at = payload.get("updated_at")
        if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool):
            return max(0.0, time.time() - float(updated_at))
    return None


def _claimed_process_pids(state_root: Path) -> set[int]:
    """项目状态中**活跃 claim** 引用的进程 PID（只读、容错）。

    worker 通道**没有心跳**（只有 host 有），其 session proof 只在 tools/list 握手时写一次，
    因此"活动证据"对 worker 天然偏弱。此处补一条**直接**安全信号：凡被活跃 claim
    （``state == "active"``，含"已 claim 未 start"窗口）记录为 ``process_pid`` 的进程，
    一律不得清理。
    """
    path = state_root / "supervisor.json"
    try:
        if path.is_symlink() or not path.is_file():
            return set()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    claims = payload.get("claims")
    if not isinstance(claims, dict):
        return set()
    pids: set[int] = set()
    for claim in claims.values():
        if not isinstance(claim, dict) or claim.get("state") != "active":
            continue
        pid = claim.get("process_pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            pids.add(pid)
    return pids


def _abandoned_mcp_processes(
    root: Path, state_root: Path, *, older_than: float,
) -> tuple[list[dict[str, object]] | None, str | None]:
    """筛出**被遗弃**的同项目 MCP 进程（年龄超阈值且无活动、无活跃 claim）。"""
    entries, failure = _project_mcp_processes(root)
    if failure is not None:
        return None, failure
    claimed = _claimed_process_pids(state_root)
    live = _live_pids()
    abandoned: list[dict[str, object]] = []
    for entry in entries or []:
        pid = int(entry["pid"])
        if pid == os.getpid():
            continue
        if float(entry["age_seconds"]) < older_than:
            continue
        if pid in claimed:
            continue  # 被活跃 claim 引用的进程：绝不清理
        if _is_attached_to_live_parent(entry, live):
            # 父进程仍存活 ⇒ 会话仍挂载（空闲不等于遗弃）。真泄漏是孤儿。
            continue
        activity_age = _mcp_process_activity_age(state_root, pid)
        if activity_age is not None and activity_age < older_than:
            continue  # 活跃会话：不清理（活动判定优先于年龄）
        abandoned.append({**entry, "activity_age_seconds": activity_age})
    return abandoned, None


def _mcp_session_hygiene_check(
    root: Path, state_root: Path | None = None,
) -> tuple[bool, str]:
    """Doctor row：同项目 MCP 会话进程数与**遗弃**判定（阈值化 + advisory）。"""
    entries, failure = _project_mcp_processes(root)
    if failure is not None:
        lower = failure.lower()
        if any(
            token in lower
            for token in ("unsupported_platform", "unavailable", "permission", "restricted")
        ):
            return True, f"当前环境限制无法枚举项目 MCP 进程，已跳过会话卫生检查（{failure}）"
        return False, f"同项目 MCP 进程枚举失败（结果未知）：{failure}"
    if not entries:
        return True, "无同项目 MCP 会话进程"
    count = len(entries)
    oldest = max(float(entry["age_seconds"]) for entry in entries)
    detail = f"同项目 MCP 会话进程 {count} 个，最老存活 {oldest:.0f}s"
    live = _live_pids()
    attached = sum(1 for entry in entries if _is_attached_to_live_parent(entry, live))
    if attached:
        detail += f"（已挂载存活父进程 {attached} 个）"
    if state_root is None:
        return True, detail + "（无状态根，跳过遗弃判定）"
    abandoned, failure = _abandoned_mcp_processes(
        root, state_root, older_than=_MCP_HYGIENE_MAX_AGE_SECONDS,
    )
    if failure is not None:
        return False, detail + f"；遗弃判定失败（结果未知）：{failure}"
    if abandoned:
        detail += (
            f"；其中 {len(abandoned)} 个疑似被遗弃（无近期活动），"
            "建议 `bugx mcp prune`（默认 dry-run）查看"
        )
        return False, detail
    if count - attached > _MCP_HYGIENE_MAX_PROCESSES:
        detail += (
            f"；未挂载进程数偏多（>{_MCP_HYGIENE_MAX_PROCESSES}），"
            "建议 `bugx mcp prune`（默认 dry-run）查看"
        )
        return False, detail
    return True, detail


