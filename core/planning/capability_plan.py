#!/usr/bin/env python3
"""能力计划：探查画像 → 技能包 → MCP/工具装配清单（只读，默认不改任何文件）。

用法：
  python3 capability_plan.py plan --root .          # 人类可读
  python3 capability_plan.py plan --root . --json   # 结构化（供 CLI 消费）

输出结构：
  {"profile": {...}, "packs": [...], "mcp": [...], "skipped": [...],
   "tools": [...], "installers": {...}, "errors": [...]}

设计要点（DOC-233/234）：
  * 技能包 ``required_runtime`` 是装配**真源**，注册表 ``applies`` 仅作形态兜底；
  * 缺凭据的能力**不装配**，但必须给出"要设置哪个环境变量"；
  * 绝不抛异常：任何失败降级为空计划 + ``errors``（可选件不得阻断启动）。
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import json
import sys
from pathlib import Path

def _get_skill_pack():
    from core.skills import skill_pack
    return skill_pack


from core.planning import capability_planner
from core.runtime import project_profile

# ``supervisor_host`` **不能**在模块级导入：它反过来 import
# ``core.host_runtime.autonomous_runner``，而后者 → ``goal_governor`` →
# ``governance/__init__`` → ``capacity_planner`` → ``supervisor/__init__`` →
# ``lease_manager`` → ``core.planning.unit_kind`` → ``planning/__init__`` →
# 本模块，构成真实循环。以 ``autonomous_runner`` 为入口直接 import 时必然
# ``ImportError: cannot import name 'RealHooks' from partially initialized module``；
# 在 pytest 里因为别的用例先导入了 supervisor 才侥幸不炸（顺序依赖型假绿）。
# 这里只在真正用到的函数内导入，打断循环。


def _classification(profile: dict) -> dict[str, list[str]]:
    classification = {
        key: list(profile.get(key) or ())
        for key in ("forms", "surfaces", "languages", "frameworks")
    }
    inner = profile.get("classification")
    if isinstance(inner, dict):
        for key in ("forms", "surfaces", "languages", "frameworks"):
            if inner.get(key):
                classification[key] = list(inner[key])
    return classification


def build_plan(root: Path | str) -> dict:
    """构造能力计划（只读）。任何异常都降级为带 ``errors`` 的空计划。

    画像缓存键 = **文件指纹 + DETECTOR_VERSION**（后者含规则摘要）⇒ 源码变化或
    探查规则变化都会自动失效，无需显式 "force" 开关。
    """
    root = Path(root)
    result: dict = {
        "profile": {}, "packs": [], "mcp": [], "skipped": [], "tools": [],
        "installers": {}, "errors": [],
    }
    if not root.is_dir():
        # 路径打错必须显式报错：静默空计划会让人误以为"项目没有可用能力"。
        result["errors"].append(f"项目根目录不存在或不是目录: {root}")
        return result
    try:
        profile = project_profile.detect_project_profile(root, persist=False)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"项目画像探查失败: {type(exc).__name__}: {exc}")
        return result
    result["profile"] = {
        key: classification_value
        for key, classification_value in _classification(profile).items()
    }
    result["profile"]["status"] = profile.get("status")
    result["profile"]["detector_version"] = profile.get("detector_version")
    classification = _classification(profile)
    sp = _get_skill_pack()

    try:
        packs = sp.find_matching_skill_packs(
            classification, project_root=root,
        ) if sp is not None else []
        result["packs"] = [
            {
                "skill_id": pack.get("skill_id"),
                "version": pack.get("version"),
                "score": pack.get("score"),
                "phases": len((pack.get("manifest") or {}).get("stage_pipeline") or ()),
            }
            for pack in packs
        ]
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"技能包匹配失败: {type(exc).__name__}: {exc}")

    try:
        plan = capability_planner.plan_runtime_capabilities(
            classification, project_root=root,
        )
        result["mcp"] = list(plan.get("items") or [])
        result["skipped"] = list(plan.get("skipped") or [])
        result["errors"].extend(plan.get("errors") or [])
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"能力编排失败: {type(exc).__name__}: {exc}")

    # 工具需求（技能包声明的既有探针）+ 每个 MCP 的安装器（供 install 编排）
    tools: set[str] = set()
    installers: dict[str, str] = {}
    if sp is not None:
        for pack_info in result["packs"]:
            try:
                manifest = sp.load_skill_pack(
                    str(pack_info["skill_id"]),
                )["manifest"]
                requirement = sp.runtime_requirements(manifest)
            except Exception:  # noqa: BLE001
                continue
            tools.update(str(item["name"]) for item in requirement.get("tools") or [])
    from core.supervisor import supervisor_host

    for item in result["mcp"]:
        installer = supervisor_host.mcp_installer(str(item["name"]))
        if installer:
            installers[str(item["name"])] = installer
    result["tools"] = sorted(tools)
    result["installers"] = installers
    return result


def _print_plan(plan: dict) -> None:
    profile = plan.get("profile") or {}
    print("[capability] 项目画像")
    print(
        "  forms=%s surfaces=%s languages=%s frameworks=%s"
        % (profile.get("forms"), profile.get("surfaces"),
           profile.get("languages"), profile.get("frameworks"))
    )
    print(f"  detector_version={profile.get('detector_version')}")
    packs = plan.get("packs") or []
    print("[capability] 命中技能包: " + (", ".join(
        f"{item['skill_id']}({item['phases']} phases, score={item['score']})"
        for item in packs
    ) or "无"))
    mcp_items = plan.get("mcp") or []
    print("[capability] 装配 MCP: " + (", ".join(
        f"{item['name']}<-{item['source']}" for item in mcp_items
    ) or "无"))
    for item in plan.get("skipped") or []:
        env = "、".join(item.get("env") or ()) or "（未声明）"
        print(f"  ⏸ {item['name']}：缺少凭据，请设置 {env}")
    print("[capability] 复用工具: " + (", ".join(plan.get("tools") or []) or "无"))
    for error in plan.get("errors") or []:
        print(f"  ⚠ {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan", help="输出能力计划（只读）")
    plan_parser.add_argument("--root", type=Path, default=Path.cwd())
    plan_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan = build_plan(args.root)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_plan(plan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
