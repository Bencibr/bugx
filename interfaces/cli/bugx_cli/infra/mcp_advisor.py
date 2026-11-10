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
    "dbhub": "@bytebase/dbhub",
    "redis": "redis-mcp-server",
    "semgrep": "semgrep",
    "mobile": "@mobilenext/mobile-mcp",
}

# 本地内建通道的启动命令（与外部 MCP 的 runtime launcher 形态不同）。
_LOCAL_MCP_COMMANDS = {
    "bugx-supervisor": ["bugx", "mcp", "serve", "--root", ".", "--role", "host"],
    "bugx-supervisor-worker": ["bugx", "mcp", "serve", "--root", ".", "--role", "worker"],
    "bugx-docs": [
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
        "name": "bugx-docs",
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
    if "enabled" in entry:
        return "incompatible"
    command = entry.get("command")
    tokens = command if isinstance(command, list) else []
    tokens = [item for item in tokens if isinstance(item, str)]
    if _is_incompatible_managed_mcp_command(name, tokens):
        return "incompatible"
    if entry.get("disabled") is True:
        return "disabled"
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
    for name in ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"):
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
        if name == "bugx-docs" and command[:4] != ["bugx", "mcp", "docs", "serve"]:
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
    surfaces = set(profile.get("surfaces") or [])
    classification = profile.get("classification")
    if isinstance(classification, dict):
        surfaces.update(classification.get("surfaces") or [])
    selected = {"bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"}
    if "ui" in surfaces:
        selected.add("playwright")
    if {"storage", "queue"} & surfaces:
        # 数据层：结构化查询（dbhub，npm）与缓存/队列（redis，pypi）。
        # 注：两者在批准 lock 中为 enabled=false ⇒ 仍会被 provision 的
        # ``- disabled`` 排除，不会自动联网安装；此处只声明"形态适用"。
        selected.update({"dbhub", "redis"})
    return selected


def provision_targeted_mcp_runtime(root: Path, selected: set[str]) -> set[str]:
    """Install only selected, lock-enabled external servers with a 30s cap."""
    disabled = _lock_disabled_servers()
    requested = (selected & set(OPENCODE_MCP_PACKAGES)) - disabled
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
        return probe_existing()
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        print(f"[bugx] ⚠ 可选 MCP 安装失败，已跳过 {', '.join(sorted(requested))}：{detail}", file=sys.stderr)
    return probe_existing()


def _mcp_recommendations(root: Path) -> list[dict[str, object]]:
    """Report host MCP recommendations without starting external code."""
    config = root.resolve() / CONFIG_NAME
    configured: dict[str, object] = {}
    if config.is_file() and not config.is_symlink():
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
            mcp = payload.get("mcp") if isinstance(payload, dict) else None
            servers = mcp.get("servers") if isinstance(mcp, dict) else None
            if isinstance(servers, dict):
                configured = servers
        except (OSError, json.JSONDecodeError):
            configured = {}
    lock_disabled = _lock_disabled_servers()
    recommendations: list[dict[str, object]] = []
    for item in REQUIRED_MCP_RECOMMENDATIONS:
        name = str(item["name"])
        entry = configured.get(item["name"])
        status = _mcp_entry_state(name, entry)
        if name in lock_disabled and status != "incompatible":
            # 只要 lock 停用就一律如实报告（不仅是已配置的条目）：
            # 否则会给用户打印一条**必然失败**的启动命令。
            status = "disabled_by_lock"
        recommendation = dict(item)
        is_conditional_required = name in {"bugx-supervisor", "bugx-supervisor-worker"}
        recommendation.update({
            "required": is_conditional_required,
            "requirement": "conditional" if is_conditional_required else "optional",
            "required_when": (
                "审查方式=mcp-auto"
                if is_conditional_required else "项目类型/审查方向需要该能力"
            ),
            "status": status,
        })
        recommendations.append(recommendation)
    return recommendations


def _print_mcp_recommendations(recommendations: list[dict[str, object]]) -> None:
    """Print a compact host-neutral MCP setup recommendation."""
    print("  MCP 工具建议（由宿主 Agent 按需加载，BugX CLI 不启动外部 MCP）：")
    for item in recommendations:
        status = str(item["status"])
        marker = "✓" if status in {"configured", "disabled_by_lock"} else "!"
        requirement = "条件必需" if item.get("requirement") == "conditional" else "按需"
        print(f"    {marker} {item['name']}：{requirement}；{status}；{item['purpose']}")
        if status == "disabled_by_lock":
            continue
        if status == "incompatible":
            print(
                f"      建议：旧版 BugX MCP 命令不再接受自动迁移；请手动删除 "
                f"`mcp.servers.{item['name']}`，再运行 `bugx install --agent opencode --root <项目路径>`"
            )
            continue
        if status != "configured":
            print(f"      启动命令：{item['command']}")
            hint = str(item["install_hint"])
            if status == "custom":
                hint += "；检测到自定义命令，BugX 不会覆盖，请由用户确认其版本"
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
    from bugx_cli.common import _python_environment
    result = subprocess.run(
        [sys.executable, str(_script(root, "mcp_runtime.py")), *script_args],
        cwd=root, capture_output=True, text=True, check=False,
        env=_python_environment(), timeout=120,
    )
    if result.returncode != 0 and "--fast" in script_args and "unrecognized arguments: --fast" in (result.stderr or ""):
        script_args_fallback = [a for a in script_args if a != "--fast"]
        result = subprocess.run(
            [sys.executable, str(_script(root, "mcp_runtime.py")), *script_args_fallback],
            cwd=root, capture_output=True, text=True, check=False,
            env=_python_environment(), timeout=120,
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CliError(_i18n_exc("common.mcp_runtime_doctor_unparseable", f"mcp_runtime doctor 输出无法解析（exit {result.returncode}）：{(result.stderr or result.stdout).strip()[:300] or exc}", code=result.returncode, detail=(result.stderr or result.stdout).strip()[:300] or exc)) from exc
    if not isinstance(payload, dict):
        raise CliError(_i18n_exc("common.mcp_runtime_doctor_must_be_object", "mcp_runtime doctor 输出必须是 JSON 对象"))
    return result.returncode, payload


def _mcp_runtime_check(root: Path) -> tuple[bool, str]:
    """Doctor row for the offline MCP runtime; absence is informational."""
    runtime_root = default_mcp_runtime_root()
    if not runtime_root.exists():
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


def _project_mcp_processes(
    root: Path,
) -> tuple[list[dict[str, object]] | None, str | None]:
    """枚举**本项目**的 MCP serve 进程（POSIX）；不支持平台或探测失败返回 None + 原因。

    三重过滤（DOC-230 3.2 第 5 项，供 `bugx mcp prune` 与 doctor 共用同一实现）：
    1. 命令行必须是 ``bugx_mcp_server.py ... serve``；
    2. ``--root`` 必须与解析后的项目根**精确匹配**（不误伤其他项目/手工进程）；
    3. **活动判定**：进程自己发布的 session proof 若比阈值更新，说明它是**活跃会话**，不得清理。
    """
    if os.name == "nt":  # pragma: no cover - 平台不支持时如实返回未知
        return None, "unsupported_platform: process enumeration unavailable"
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


