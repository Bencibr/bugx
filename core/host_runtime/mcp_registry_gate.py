#!/usr/bin/env python3
"""core/host_runtime/mcp_registry_gate.py - MCP 注册表与门禁校验。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

IMPLEMENTED_INSTALLERS = ("builtin", "npm", "pip")
MCP_INSTALLERS = ("builtin", "npm", "pip", "self-built")
MCP_TRUST_LEVELS = ("builtin", "A", "B", "C")

MCP_REGISTRY: tuple[dict[str, object], ...] = (
    {
        "name": "bugx-supervisor", "tier": "hard_for_mcp_auto", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {}, "installer": "builtin", "package": None, "source": "builtin",
        "trust": "builtin", "audit": "DOC-233 设计项 3a（内建通道）",
    },
    {
        "name": "bugx-supervisor-worker", "tier": "hard_for_mcp_auto", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {}, "installer": "builtin", "package": None, "source": "builtin",
        "trust": "builtin", "audit": "DOC-233 设计项 3a（内建通道）",
    },
    {
        "name": "postmcp", "tier": "optional", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {"surfaces": ("rest", "graphql")}, "installer": "npm",
        "package": "@bencibro/postmcp",
        "source": "https://github.com/Bencibr/postmcp",
        "trust": "B",
        "audit": ("公开 npm 包供应链审查记录：核对安装、MCP 握手、原生依赖和遥测配置；"
                  "发布前应重新执行当前版本的供应链检查。"),
    },
    {
        "name": "playwright", "tier": "optional", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {"forms": ("web",), "surfaces": ("ui",)}, "installer": "npm",
        "package": "@playwright/mcp", "source": "https://github.com/microsoft/playwright-mcp",
        "trust": "A",
        "audit": ("npm 元数据实测 maintainers 含 microsoft-oss-releases/microsoft1es；"
                  "载荷经 mcp_runtime lock-hash 安装（DOC-233 设计项 4）"),
    },
    {
        "name": "bugx-docs", "tier": "optional", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {}, "installer": "builtin", "package": None, "source": "builtin",
        "trust": "builtin", "audit": "DOC-233 设计项 3a（内建通道）",
    },
    # ---- 形态位登记：**planned 项永不进入装配清单**（DOC-233 设计项 3a/3b 边界）。
    # 第三方项必须逐项通过供应链/隔离审计（DOC-234 设计项 2/3/4）才可转 active。
    {
        "name": "dbhub", "tier": "optional", "status": "active",
        "credentials": {"env": ("DSN", "DBHUB_HOST", "DB_HOST"), "require_any": True},
        "applies": {"surfaces": ("storage", "queue")}, "installer": "npm",
        "package": "@bytebase/dbhub", "source": "https://github.com/bytebase/dbhub",
        "trust": "B",
        "audit": ("DOC-234 批次 2：@bytebase/dbhub@1.5.0 + npm dist.integrity(sha512) "
                  "实测 install(--ignore-scripts)/verify/start 全链通过"),
    },
    {
        "name": "redis", "tier": "optional", "status": "active",
        "credentials": {"env": ("REDIS_HOST", "REDIS_PORT", "REDIS_USERNAME", "REDIS_PWD"), "require_any": True},
        "applies": {"surfaces": ("storage", "queue")}, "installer": "pip",
        "package": "redis-mcp-server", "source": "https://github.com/redis/mcp-redis",
        "trust": "A",
        "audit": ("DOC-235 批次 4：redis-mcp-server@0.5.1 + uv 生成闭包(54 包/1486 "
                  "sha256) 实测 install(--only-binary --no-deps --require-hashes)/"
                  "verify/start 全链通过；实测零遥测、零硬编码外发端点"),
    },
    {
        "name": "semgrep", "tier": "optional", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {"surfaces": ("native",), "forms": ("cli",)}, "installer": "pip",
        "package": "semgrep", "source": "https://github.com/semgrep/semgrep",
        "trust": "A",
        "audit": ("DOC-240：semgrep==1.182.0（uv 哈希闭包 66 包）install/verify/start 实测通过；"
                  "MCP 入口改由引擎子命令提供（lock: entry=semgrep, args=[mcp]），"
                  "不再依赖已被上游改成空壳的 semgrep-mcp 0.9.0。"
                  "实测 `semgrep mcp` **默认开启 Datadog APM tracing**（stderr 打印 "
                  "app.datadoghq.com trace id），已在 LAUNCH_ENV_OVERRIDES 硬关闭"
                  "（DD_TRACE_ENABLED=false / OTEL_SDK_DISABLED=true / SEMGREP_SEND_METRICS=off），"
                  "关闭后无 tracing 输出"),
    },
    {
        "name": "mobile", "tier": "optional", "status": "active",
        "credentials": {"env": (), "require_any": False},
        "applies": {"forms": ("mobile",)}, "installer": "npm",
        "package": "@mobilenext/mobile-mcp",
        "source": "https://github.com/mobile-next/mobile-mcp",
        "trust": "B",
        "audit": ("公开 npm 包供应链审查记录：核对完整性、安装/启动链路、遥测开关和许可证风险；"
                  "发布前应重新执行当前版本的供应链检查。"),
    },
    {
        "name": "desktop", "tier": "optional", "status": "planned",
        "credentials": {"env": (), "require_any": False},
        "applies": {"forms": ("desktop",)}, "installer": "self-built", "package": None,
        "source": "self-built", "trust": "builtin", "audit": "",
    },
    {
        "name": "native", "tier": "optional", "status": "planned",
        "credentials": {"env": (), "require_any": False},
        "applies": {"surfaces": ("native",)}, "installer": "self-built", "package": None,
        "source": "self-built", "trust": "builtin", "audit": "",
    },
)


class McpRegistryError(RuntimeError):
    """MCP 注册表结构性缺陷（fail-fast：禁止带病运行）。"""


def validate_mcp_registry(
    entries: tuple[dict[str, object], ...] | list[dict[str, object]],
) -> tuple[str, ...]:
    """自检注册表，返回违规清单（空元组 = 合规）。

    规则（DOC-234 设计项 1 审计）：
      1. 字段完备且取值合法、名称不重复；
      2. ``trust == "C"`` ⇒ 必须 ``planned``（社区包不得自动装配）；
      3. ``active`` ⇒ 安装器**必须已实现**（否则是纸面能力）；
      4. ``active`` ⇒ ``audit`` 非空（无审计不得放行）；
      5. ``builtin``/``self-built`` ⇒ ``package`` 必须为 ``None``；
      6. ``npm``/``pip`` ⇒ ``package`` 必须非空。
    """
    required = (
        "name", "tier", "status", "applies", "installer", "package", "source",
        "trust", "audit", "credentials",
    )
    violations: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            violations.append(f"[{index}] 条目必须是映射，实际为 {type(entry).__name__}")
            continue
        name = str(entry.get("name") or f"[{index}]")
        missing = [field for field in required if field not in entry]
        if missing:
            violations.append(f"{name}: 缺少字段 {', '.join(missing)}")
            continue
        if name in seen:
            violations.append(f"{name}: 名称重复")
        seen.add(name)
        status = str(entry["status"])
        installer = str(entry["installer"])
        trust = str(entry["trust"])
        tier = str(entry["tier"])
        if status not in ("active", "planned"):
            violations.append(f"{name}: status 非法 {status!r}")
        if tier not in ("hard_for_mcp_auto", "optional"):
            violations.append(f"{name}: tier 非法 {tier!r}")
        if installer not in MCP_INSTALLERS:
            violations.append(f"{name}: installer 非法 {installer!r}")
        if trust not in MCP_TRUST_LEVELS:
            violations.append(f"{name}: trust 非法 {trust!r}")
        if not isinstance(entry["applies"], Mapping):
            violations.append(f"{name}: applies 必须是映射")
        credentials = entry["credentials"]
        if not isinstance(credentials, Mapping):
            violations.append(f"{name}: credentials 必须是映射")
        else:
            unknown = sorted(set(credentials) - {"env", "require_any"})
            if unknown:
                violations.append(
                    f"{name}: credentials 含未知字段 {', '.join(unknown)}"
                )
            env = credentials.get("env")
            if not isinstance(env, (list, tuple)) or any(
                not isinstance(item, str) or not item.strip() for item in env
            ):
                violations.append(f"{name}: credentials.env 必须是非空字符串序列")
            elif len(set(env)) != len(env):
                violations.append(f"{name}: credentials.env 存在重复项")
            require_any = credentials.get("require_any")
            if not isinstance(require_any, bool):
                violations.append(f"{name}: credentials.require_any 必须是布尔值")
            elif require_any and not env:
                violations.append(
                    f"{name}: require_any=True 时 credentials.env 不能为空"
                )
        if trust == "C" and status != "planned":
            violations.append(f"{name}: trust=C 的社区包必须保持 planned")
        if status == "active":
            if installer not in IMPLEMENTED_INSTALLERS:
                violations.append(
                    f"{name}: 安装器 {installer!r} 尚未实现，不得 active（纸面能力）"
                )
            if not str(entry["audit"]).strip():
                violations.append(f"{name}: active 项必须填写 audit（审计依据）")
            if trust == "builtin" and installer != "builtin":
                violations.append(f"{name}: trust=builtin 与 installer={installer!r} 矛盾")
        package = entry["package"]
        if installer in ("builtin", "self-built"):
            if package is not None:
                violations.append(f"{name}: installer={installer} 时 package 必须为 None")
        elif not (isinstance(package, str) and package.strip()):
            violations.append(f"{name}: installer={installer} 时必须声明 package")
        if installer == "builtin" and str(entry["source"]) != "builtin":
            violations.append(f"{name}: installer=builtin 时 source 必须为 'builtin'")
    return tuple(violations)


_MCP_REGISTRY_VIOLATIONS = validate_mcp_registry(MCP_REGISTRY)
if _MCP_REGISTRY_VIOLATIONS:  # pragma: no cover - 静态数据缺陷，CI 必先命中
    raise McpRegistryError(
        "MCP 注册表自检失败:\n"
        + "\n".join(f"  - {item}" for item in _MCP_REGISTRY_VIOLATIONS)
    )


def registry_violations() -> tuple[str, ...]:
    """导入期自检结果（合规时恒为空；保留给审计与诊断输出）。"""
    return _MCP_REGISTRY_VIOLATIONS


# 派生投影（保持既有消费方语义；**真源仍是 MCP_REGISTRY**）。
MCP_CATALOG = tuple(
    str(entry["name"]) for entry in MCP_REGISTRY if entry["status"] == "active"
)
MCP_POLICY = {
    str(entry["name"]): str(entry["tier"])
    for entry in MCP_REGISTRY if entry["status"] == "active"
}


# 派生投影（保持既有消费方语义；**真源仍是 MCP_REGISTRY**）。
MCP_CATALOG = tuple(
    str(entry["name"]) for entry in MCP_REGISTRY if entry["status"] == "active"
)
MCP_POLICY = {
    str(entry["name"]): str(entry["tier"])
    for entry in MCP_REGISTRY if entry["status"] == "active"
}


def active_mcp_names() -> tuple[str, ...]:
    """可参与装配的 MCP 名称（按注册表顺序）。"""
    return MCP_CATALOG


def planned_mcp_names() -> tuple[str, ...]:
    """已登记但**不参与装配**的形态位。"""
    return tuple(
        str(entry["name"]) for entry in MCP_REGISTRY if entry["status"] == "planned"
    )


def mcp_tier(name: str) -> str | None:
    """返回分级；未登记或未激活返回 None（调用方须 fail-closed 拒绝）。"""
    for entry in MCP_REGISTRY:
        if entry["name"] == name and entry["status"] == "active":
            return str(entry["tier"])
    return None


def host_entry_status(root: Path) -> dict[str, object]:
    """宿主侧入口（skill/MCP 入口文件）就绪性检查（DOC-236 C7）。

    数据源：``.bugx/hosts.json``（由 ``bugx install`` 写入，含 ``bugx_build_id`` 与逐文件
    ``sha256``）。这样引擎侧无需依赖 CLI 层即可判定"宿主入口是否已装配/是否过期"。

    只读、不装配；任何异常一律返回 ``ready=False``（fail-closed，绝不谎报就绪）。
    """
    import hashlib as _hashlib

    result: dict[str, object] = {"ready": False, "reason": "", "hosts": [], "stale_files": []}
    try:
        record_path = Path(root) / ".bugx" / "hosts.json"
        if record_path.is_symlink() or not record_path.is_file():
            result["reason"] = "宿主入口未登记（.bugx/hosts.json 缺失）"
            return result
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["reason"] = f"宿主入口登记不可读（{type(exc).__name__}）"
        return result
    if not isinstance(payload, dict):
        result["reason"] = "宿主入口登记格式非法"
        return result
    installed = payload.get("installed")
    if not isinstance(installed, list) or not installed:
        result["reason"] = "宿主入口登记为空（尚未安装任何宿主入口）"
        return result

    try:
        from core.infra.build_info import get_build_id as _get_build_id
        current_build = _get_build_id()
    except Exception:  # noqa: BLE001 - 读不到 build id 时不谎报就绪
        current_build = None
    recorded_build = payload.get("bugx_build_id")
    if (isinstance(recorded_build, int) and isinstance(current_build, int)
            and recorded_build != current_build):
        result["reason"] = (
            f"宿主入口为 build {recorded_build}，当前引擎为 build {current_build}"
            "（引擎已更新，宿主入口需重新装配并重启）"
        )
        return result

    stale: list[str] = []
    hosts: list[str] = []
    for record in installed:
        if not isinstance(record, dict):
            continue
        hosts.append(str(record.get("name") or "unknown"))
        for item in record.get("files") or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path") or "")
            expected = str(item.get("sha256") or "")
            if not rel or not expected:
                continue
            target = Path(root) / rel
            try:
                if target.is_symlink() or not target.is_file():
                    stale.append(rel)
                    continue
                actual = _hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                stale.append(rel)
                continue
            if actual != expected:
                stale.append(rel)
    result["hosts"] = hosts
    if stale:
        result["stale_files"] = stale[:5]
        result["reason"] = "宿主入口文件缺失或被改动：" + ", ".join(stale[:3])
        return result
    result["ready"] = True
    result["reason"] = f"宿主入口就绪（{', '.join(hosts)}；build {recorded_build}）"
    return result


def mcp_entry(name: str) -> dict[str, object] | None:
    """按名取登记项（**不限状态**，供审计/诊断溯源 source/trust/package）。"""
    for entry in MCP_REGISTRY:
        if entry["name"] == name:
            return dict(entry)
    return None


def mcp_installer(name: str) -> str | None:
    """返回安装器；未登记或未激活返回 None（planned 不得装配，故不得安装）。"""
    for entry in MCP_REGISTRY:
        if entry["name"] == name and entry["status"] == "active":
            return str(entry["installer"])
    return None


def mcp_credentials(name: str) -> dict[str, object] | None:
    """返回凭据声明（仅 active 项）；未登记/未激活返回 None。

    形状：``{"env": (str, ...), "require_any": bool}``。
    ``require_any=True`` 表示"必须至少设置其中一个环境变量才可装配"
    （避免像 redis-mcp-server 那样在缺省时**静默回落 localhost**）。
    """
    for entry in MCP_REGISTRY:
        if entry["name"] == name and entry["status"] == "active":
            credentials = entry["credentials"]
            return dict(credentials) if isinstance(credentials, Mapping) else None
    return None
