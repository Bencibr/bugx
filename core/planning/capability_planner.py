#!/usr/bin/env python3
"""能力编排器（DOC-233 设计项 2）：画像 + 技能包声明 → MCP/工具装配清单。

**唯一编排点**：取代原先散落在 ``project_init._matched_optional_mcp_names`` 的
硬编码形态规则，使"探查结果 → 工具/MCP 自动配置"成为闭环。

契约（DOC-233 审计）：
  * 真源分离：``applies_if`` 只决定**技能包匹配**；``required_runtime`` 决定**装配**。
  * fail-closed：只输出 ``MCP_REGISTRY`` 中 ``status == "active"`` 的登记项；
    未登记 / ``planned`` 名称一律丢弃（防任意包名注入）。
  * **绝不抛异常**：任何内部错误都降级为"无附加能力"+ 结构化 ``errors``，
    因为可选件按 ``DOC-226`` 不得阻断启动。
  * 可审计：每项带 ``reason``（``skill:<id>`` / ``registry:applies``）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

def _get_skill_pack():
    from core.skills import skill_pack
    return skill_pack

try:
    from core.supervisor import supervisor_host
except ImportError:  # pragma: no cover
    try:
        from core import supervisor_host  # type: ignore[no-redef]
    except ImportError:
        supervisor_host = None  # type: ignore[assignment]


def _registry_entries() -> tuple[dict[str, object], ...]:
    if supervisor_host is None:
        return ()
    return tuple(getattr(supervisor_host, "MCP_REGISTRY", ()))


def _matches_applies(entry: Mapping[str, object], classification: Mapping[str, Any]) -> bool:
    """注册表 ``applies`` 与画像形态是否相交（空 applies = 与形态无关）。"""
    applies = entry.get("applies")
    if not isinstance(applies, Mapping) or not applies:
        return False
    for key in ("forms", "surfaces"):
        wanted = {str(v).casefold() for v in (applies.get(key) or ())}
        if not wanted:
            continue
        have = {str(v).casefold() for v in (classification.get(key) or ())}
        if have & wanted:
            return True
    return False


def _credentials_satisfied(name: str) -> tuple[bool, tuple[str, ...]]:
    """凭据声明校验（DOC-234 §批次 6）：返回 ``(是否满足, 声明的 env 列表)``。

    ``require_any=True`` 时，**至少一个**声明的环境变量必须已设置；否则该能力
    不参与装配（避免像 `redis-mcp-server` 那样在缺省时静默回落 localhost）。
    任何内部错误都视为"满足"（可选件不得阻断启动，DOC-226）。
    """
    if supervisor_host is None:
        return True, ()
    getter = getattr(supervisor_host, "mcp_credentials", None)
    if not callable(getter):
        return True, ()
    try:
        credentials = getter(name)
    except Exception:  # noqa: BLE001 - 可选件不得阻断启动
        return True, ()
    if not isinstance(credentials, Mapping):
        return True, ()
    env = tuple(str(item) for item in (credentials.get("env") or ()))
    if not credentials.get("require_any") or not env:
        return True, env
    return any(os.environ.get(item) for item in env), env


def plan_runtime_capabilities(
    classification: Mapping[str, Any] | None,
    *,
    skills_dir: Path | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """返回装配清单。

    ``{"items": [{"name", "tier", "source", "reason"}], "errors": [str, ...]}``
    """
    items: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    skipped: list[dict[str, object]] = []
    seen_skipped: set[str] = set()

    def record_skipped(name: str, source: str, env: tuple[str, ...]) -> None:
        if name in seen_skipped:
            return
        seen_skipped.add(name)
        skipped.append({
            "name": name, "source": source,
            "reason": "missing_credentials", "env": list(env),
        })

    classification = classification if isinstance(classification, Mapping) else {}

    # 1) 技能包声明优先（真源：required_runtime）
    skill_pack = _get_skill_pack()
    if skill_pack is not None:
        try:
            packs = skill_pack.find_matching_skill_packs(
                classification, skills_dir=skills_dir, project_root=project_root,
            )
        except Exception as exc:  # noqa: BLE001 - 可选件不得阻断启动
            errors.append(f"技能包匹配失败: {type(exc).__name__}: {exc}")
            packs = []
        for pack in packs:
            manifest = pack.get("manifest")
            skill_id = str(pack.get("skill_id") or "?")
            if not isinstance(manifest, Mapping):
                continue
            try:
                req = skill_pack.runtime_requirements(manifest)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"技能包 {skill_id} 运行时需求解析失败: {exc}")
                continue
            for entry in req.get("mcp", []):
                name = entry["name"]
                if not _is_active(name):
                    errors.append(f"技能包 {skill_id} 声明了未登记/未激活的 MCP: {name}")
                    continue
                satisfied, env = _credentials_satisfied(name)
                if not satisfied:
                    record_skipped(name, f"skill:{skill_id}", env)
                    continue
                items.setdefault(name, {
                    "name": name, "tier": entry["tier"],
                    "source": f"skill:{skill_id}", "reason": "required_runtime",
                })

    # 2) 注册表形态兜底（保持既有默认行为等价；skills 声明优先）
    for entry in _registry_entries():
        if entry.get("status") != "active":
            continue
        name = str(entry["name"])
        if name in items:
            continue
        if _matches_applies(entry, classification):
            satisfied, env = _credentials_satisfied(name)
            if not satisfied:
                record_skipped(name, "registry:applies", env)
                continue
            items[name] = {
                "name": name, "tier": str(entry.get("tier") or "optional"),
                "source": "registry:applies", "reason": "applies",
            }

    return {
        "items": sorted(items.values(), key=lambda item: item["name"]),
        "errors": errors,
        "skipped": skipped,
    }


def _is_active(name: str) -> bool:
    """登记且激活才算数（``planned`` / 未登记一律拒绝）。"""
    if supervisor_host is None:
        return False
    checker = getattr(supervisor_host, "mcp_tier", None)
    if callable(checker):
        return checker(name) is not None
    return name in set(getattr(supervisor_host, "MCP_CATALOG", ()))


def optional_mcp_names(
    classification: Mapping[str, Any] | None,
    *,
    skills_dir: Path | None = None,
    project_root: Path | str | None = None,
) -> tuple[set[str], list[str]]:
    """供 ``project_init`` 使用的窄接口：只取 **optional** 项（hard 由 N1.11 门禁负责）。"""
    plan = plan_runtime_capabilities(
        classification, skills_dir=skills_dir, project_root=project_root,
    )
    names = {
        item["name"] for item in plan["items"]
        if item.get("tier") == "optional"
    }
    errors = list(plan["errors"])
    for item in plan.get("skipped") or []:
        env = "、".join(str(name) for name in item.get("env") or []) or "（未声明）"
        errors.append(
            f"能力 {item.get('name')} 因缺少凭据被跳过：请设置环境变量 {env}"
        )
    return names, errors
