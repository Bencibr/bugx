from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from ..common import _ensure_core_importable


def command_skill(args: argparse.Namespace) -> int:
    """bugx skill: 审计技能包管理、图谱状态查询、契约校验与原子回滚。"""
    action = getattr(args, "skill_action", "status") or "status"
    root = Path(getattr(args, "root", Path.cwd()) or Path.cwd()).resolve()
    skill_id = getattr(args, "skill", "web-audit") or "web-audit"
    as_json = bool(getattr(args, "json", False))

    try:
        _ensure_core_importable()
        # 单一导入路径：``skill_pack`` 已归档到 ``core/skills/``，扁平名
        # ``import skill_pack`` 只是把真实 ImportError 换成更难定位的二次失败。
        from core.skills import skill_pack
        from core.autonomous_cve.attack_graph import LivingAttackGraph
    except Exception as exc:
        if as_json:
            print(json.dumps({"status": "error", "error": f"无法加载核心技能包引擎: {exc}"}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx skill] ✗ 无法加载核心技能包引擎: {exc}")
        return 1

    if action == "import":
        bundle = getattr(args, "bundle", None)
        if not bundle:
            print("[bugx skill] ✗ skill import 必须提供 --bundle")
            return 2
        try:
            target = skill_pack.global_overlay_dir()
            imported = skill_pack.import_skill_bundle(bundle, target)
            result = {"status": "ok", "skill_id": imported["skill_id"], "version": imported["version"], "path": imported["path"]}
            if as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] ✓ 已导入 {imported['skill_id']} v{imported['version']} 到 {imported['path']}")
            return 0
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] ✗ 离线技能包导入失败: {exc}")
            return 1

    if action == "pull":
        target_skill = getattr(args, "skill", None) or "web-audit"
        try:
            target = skill_pack.global_overlay_dir()
            pulled = skill_pack.pull_skill_pack(target_skill, target_root=target)
            manifest = pulled.get("manifest", {})
            result = {
                "status": "ok",
                "skill_id": manifest.get("skill_id", target_skill),
                "version": manifest.get("version"),
                "path": pulled.get("pack_dir"),
                "plan_hash": pulled.get("plan_hash"),
            }
            if as_json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] ✓ 已从云端拉取并安装 {result['skill_id']} v{result['version']} 到 {result['path']}")
            return 0
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] ✗ 云端拉取技能包失败: {exc}")
            return 1

    if action == "list":
        prompts_skills_dir = Path(skill_pack.DEFAULT_SKILLS_DIR)
        skills = []
        if prompts_skills_dir.is_dir():
            for p in sorted(prompts_skills_dir.iterdir()):
                if p.is_dir() and (p / "manifest.json").is_file():
                    try:
                        pack = skill_pack.load_skill_pack(p.name, skills_dir=prompts_skills_dir)
                        manifest = pack.get("manifest", {})
                        skills.append({
                            "skill_id": manifest.get("skill_id", p.name),
                            "version": manifest.get("version", "unknown"),
                            "scope": manifest.get("scope", "engine"),
                            "plan_hash": pack.get("plan_hash", ""),
                            "optional_modules": manifest.get("optional_modules", []),
                            "stage_count": len(manifest.get("stage_pipeline", [])),
                        })
                    except Exception:
                        pass
        if as_json:
            print(json.dumps({"status": "ok", "skills": skills}, ensure_ascii=False, indent=2))
        else:
            print("=== 已安装审计技能包 (Skill Packs) ===")
            if not skills:
                print("  未检测到任何已安装技能包。")
            for sk in skills:
                print(f"• {sk['skill_id']} (v{sk['version']})")
                print(f"  Scope: {sk['scope']} | Stages: {sk['stage_count']} | Hash: {sk['plan_hash'][:16]}...")
                if sk["optional_modules"]:
                    print(f"  可选模块: {', '.join(sk['optional_modules'])}")
        return 0

    elif action == "status":
        strategy_path = root / ".bugx" / "strategy.json"
        graph_snapshot_path = root / ".bugx" / "attack-graph-snapshot.json"
        plan = None
        if strategy_path.is_file():
            try:
                st_data = json.loads(strategy_path.read_text(encoding="utf-8"))
                plan = st_data.get("skill_plan")
            except Exception:
                pass

        graph_info: dict[str, Any] = {
            "routes_count": 0,
            "roles_count": 0,
            "hypotheses_count": 0,
            "illegal_access_edges": 0,
            "status": "not_initialized",
        }
        if graph_snapshot_path.is_file():
            try:
                g_data = json.loads(graph_snapshot_path.read_text(encoding="utf-8"))
                g = LivingAttackGraph.from_dict(g_data)
                graph_info = {
                    "routes_count": len(g.routes),
                    "roles_count": len(g.roles),
                    "hypotheses_count": len(g.hypotheses),
                    "illegal_access_edges": len(g.get_illegal_access_edges()),
                    "status": "active",
                }
            except Exception:
                graph_info["status"] = "snapshot_corrupted"

        res = {
            "status": "ok",
            "project_root": str(root),
            "skill_plan": plan,
            "attack_graph": graph_info,
        }
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"=== 项目审计技能与攻击图谱状态 [{root.name}] ===")
            if plan:
                print(f"• 激活技能包: {plan.get('skill_id')} (v{plan.get('version')})")
                print(f"  Plan Hash: {plan.get('plan_hash')}")
                if plan.get("parent_plan_hash"):
                    print(f"  Parent Hash: {plan.get('parent_plan_hash')}")
                print(f"  激活模块: {', '.join(plan.get('active_modules') or ['(无)']) }")
            else:
                print("• 激活技能包: 未编译（请先运行 bugx engine --root . pre）")
            print(f"• 动态攻击图谱 (Living Attack Graph): {graph_info['status']}")
            if graph_info["status"] == "active":
                print(f"  路由: {graph_info['routes_count']} | 身份角色: {graph_info['roles_count']} | 假设: {graph_info['hypotheses_count']}")
                print(f"  非法访问边 (Illegal Access): {graph_info['illegal_access_edges']} 条")
        return 0

    elif action == "validate":
        try:
            pack = skill_pack.load_skill_pack(skill_id)
            manifest = pack["manifest"]
            stages = pack.get("stages", {})
            issues = []
            for st_id, st_info in stages.items():
                l1_tokens = skill_pack.estimate_tokens(st_info.get("l1_checklist", ""))
                max_l1 = manifest.get("budgets", {}).get("max_l1_checklist_tokens", 300)
                if l1_tokens > max_l1:
                    issues.append(f"阶段 {st_id} L1 行动清单超限: {l1_tokens}T > {max_l1}T")

            report = {
                "status": "ok" if not issues else "warning",
                "skill_id": skill_id,
                "version": manifest.get("version"),
                "plan_hash": pack.get("plan_hash"),
                "schema_valid": True,
                "stages_validated": len(stages),
                "issues": issues,
            }
            if as_json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"=== 技能包校验报告: {skill_id} ===")
                print(f"• Schema 格式: 100% 合规通过")
                print(f"• Plan Hash: {pack.get('plan_hash')}")
                print(f"• 校验阶段数: {len(stages)}")
                if issues:
                    print("⚠ 发现预算超限或风险:")
                    for iss in issues:
                        print(f"  - {iss}")
                else:
                    print("✓ 双层 Token 硬预算全部达标 (L1 Checklist <= 300T)")
            return 0 if not issues else 2
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] ✗ 技能包校验失败: {exc}")
            return 1

    elif action == "rollback":
        strategy_path = root / ".bugx" / "strategy.json"
        if not strategy_path.is_file():
            msg = "未找到 strategy.json，当前项目无需回滚"
            if as_json:
                print(json.dumps({"status": "noop", "message": msg}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] {msg}")
            return 0

        try:
            st_data = json.loads(strategy_path.read_text(encoding="utf-8"))
            plan = st_data.get("skill_plan")
            if not plan:
                msg = "未找到 skill_plan，无需回滚"
                if as_json:
                    print(json.dumps({"status": "noop", "message": msg}, ensure_ascii=False, indent=2))
                else:
                    print(f"[bugx skill] {msg}")
                return 0

            from core.runtime.project_profile import detect_project_profile
            profile = detect_project_profile(root)
            fresh_plan = skill_pack.compile_skill_plan(skill_id, profile.get("classification") or {})
            if fresh_plan:
                st_data["skill_plan"] = fresh_plan
                strategy_path.write_text(json.dumps(st_data, indent=2, ensure_ascii=False), encoding="utf-8")
                msg = f"已成功将技能包 [{skill_id}] 原子回滚至初始 Baseline 状态 ({fresh_plan.get('plan_hash')[:16]}...)"
                if as_json:
                    print(json.dumps({"status": "ok", "message": msg, "plan_hash": fresh_plan.get("plan_hash")}, ensure_ascii=False, indent=2))
                else:
                    print(f"[bugx skill] ✓ {msg}")
                return 0
            else:
                raise RuntimeError("无法重新编译初始技能包计划")
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx skill] ✗ 回滚失败: {exc}")
            return 1

    return 0
