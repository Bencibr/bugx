from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import _ensure_core_importable


def _render_bar(pct: float, width: int = 20) -> str:
    filled = int(round(pct / 100.0 * width))
    filled = min(width, max(0, filled))
    return "█" * filled + "░" * (width - filled)


def _rebind_project_identity(
    root: Path, args: argparse.Namespace, as_json: bool,
) -> int:
    """``bugx project rebind``：把 .bugx 的项目身份迁到当前路径。

    场景：项目目录被搬移/改名（或从另一台机器恢复备份）后，``.bugx`` 里存的是
    旧路径的 ``project_id``，``require()`` 会 fail-closed 报「不属于当前项目」，
    而历史实现给出的唯一出路是 ``rm -rf .bugx`` —— 账本、知识库、报告全丢。

    ``--migrate`` 是**唯一**放行搬移的方式，且显式表达语义：布局本来就属于
    这些内容，只是根变了，因此把 ``project_id`` 重写为新根指纹并保留
    ``project_uid``（身份不因路径变化而改变）。不给 ``--migrate`` 时只做诊断。
    """
    import core.bugx_paths as bugx_paths

    base = root / ".bugx"
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        message = f"缺少 {manifest_path}；请先运行 bugx init"
        if as_json:
            print(json.dumps({"status": "error", "error": message}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx project] ✗ {message}")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored = str(manifest.get("project_id") or "")
    expected = bugx_paths._path_fingerprint(root)
    uid = str(manifest.get(bugx_paths.PROJECT_UID_KEY) or "")
    matches = stored == expected

    if not getattr(args, "migrate", False):
        detail = {
            "status": "ok" if matches else "mismatch",
            "root": str(root),
            "stored_project_id": stored,
            "expected_project_id": expected,
            "project_uid": uid or None,
            "portable": bool(uid),
            "hint": (
                "身份已与路径解绑（project_uid 持久化），无需处理。"
                if matches and uid else
                "路径已变更：确认这是搬移而非误用他人布局后，运行 "
                "bugx project rebind --migrate 保留现有账本并重绑身份。"
            ),
        }
        if as_json:
            print(json.dumps(detail, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx project] 根: {root}")
            print(f"  盘上 project_id : {stored or '（缺失）'}")
            print(f"  当前路径指纹    : {expected}")
            print(f"  持久化 uid      : {uid or '（旧布局，无 uid）'}")
            print(f"  {detail['hint']}")
        return 0 if matches else 2

    if matches and uid:
        if as_json:
            print(json.dumps({"status": "ok", "changed": False, "root": str(root)},
                             ensure_ascii=False, indent=2))
        else:
            print("[bugx project] 身份已匹配，无需迁移。")
        return 0

    manifest["project_id"] = expected
    manifest.setdefault(bugx_paths.PROJECT_UID_KEY, bugx_paths._new_project_uid(root))
    bugx_paths._atomic_json(manifest_path, manifest)
    try:
        bugx_paths.clear_require_cache(root)
    except Exception:  # pragma: no cover - 缓存清理失败不影响正确性
        pass
    payload = {
        "status": "ok", "changed": True, "root": str(root),
        "previous_project_id": stored, "project_id": expected,
        "project_uid": manifest[bugx_paths.PROJECT_UID_KEY],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[bugx project] ✓ 已把 {base} 的项目身份重绑到当前路径")
        print(f"  project_id: {stored or '（缺失）'} → {expected}")
        print(f"  project_uid 保留: {payload['project_uid']}")
    return 0


def command_project(args: argparse.Namespace) -> int:
    """bugx project：工程代码体量、技术栈画像与规模估模分析。"""
    action = getattr(args, "project_action", "analyze") or "analyze"
    root = Path(getattr(args, "root", Path.cwd()) or Path.cwd()).resolve()
    as_json = bool(getattr(args, "json", False))

    try:
        _ensure_core_importable()
        import core.bugx_paths as bugx_paths
        from core.runtime import project_profile
        ledger_path = bugx_paths.project_knowledge_root(root) / "lesson-ledger.json"
    except Exception as exc:
        if as_json:
            print(json.dumps({"status": "error", "error": f"无法加载核心分析引擎: {exc}"}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx project] ✗ 无法加载核心分析引擎: {exc}")
        return 1

    if action == "show":
        cached = None
        if ledger_path.is_file():
            try:
                cached = project_profile.load_project_profile(ledger_path)
            except Exception:
                cached = None

        if cached is None:
            # 就地自愈：画像未持久化时自动执行首次全景分析并持久化，开箱即用直接展示
            try:
                print(f"[bugx project] 首次运行，正在自动分析并生成项目画像...", file=sys.stderr)
                cached = project_profile.detect_project_profile(root)
                cached = project_profile.save_project_profile(
                    ledger_path, cached, producer_version=__version__,
                )
            except Exception as exc:
                if as_json:
                    print(json.dumps({"status": "not_found", "message": f"未找到本地画像且自动生成失败: {exc}"}, ensure_ascii=False, indent=2))
                else:
                    print(f"[bugx project] ✗ 自动生成项目画像失败: {exc}")
                return 1

        profile = cached

    elif action == "analyze":
        try:
            profile = project_profile.detect_project_profile(root)
            profile = project_profile.save_project_profile(
                ledger_path, profile, producer_version=__version__,
            )
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": f"项目代码分析失败: {exc}"}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx project] ✗ 项目代码分析失败: {exc}")
            return 1

    elif action == "sync":
        try:
            profile = project_profile.detect_project_profile(root)
            from core.cloud_sync import cloud_client
            client = cloud_client.CloudClient()
            if not client or not client.is_authenticated():
                if as_json:
                    print(json.dumps({"status": "unauthenticated", "error": "未登录云端，请先运行 bugx auth login"}, ensure_ascii=False, indent=2))
                    return 1
                else:
                    # 严禁假性报错阻断！直接跳转登录界面（打开浏览器授权并登记设备）
                    print("[bugx project] 当前未登录云端，正在打开浏览器完成登录授权…")
                    try:
                        login_res = client.login(name="bugx-cli")
                        if not getattr(login_res, "ok", False):
                            print(f"[bugx project] ✗ 登录未完成: {getattr(login_res, 'error', '未知错误')}")
                            return 1
                        client.register_device(name="bugx-cli")
                    except Exception as exc:
                        print(f"[bugx project] ✗ 登录或设备登记失败: {exc}")
                        return 1

            # Project identity is repository state, not a mutable source
            # snapshot.  Using ``source_fingerprint`` here created a new
            # cloud project whenever the source tree changed.
            from core.cloud_sync.cloud_onboard import get_account_id, _save_project_binding
            from core.cloud_sync.onboard_wizard import ensure_project_identity
            identity = ensure_project_identity(root, root.name)
            account_id = get_account_id(client)
            bound_account = identity.get("account_id", "")
            if bound_account and bound_account != account_id:
                raise ValueError("本地项目绑定账号与当前云端账号不一致")
            reg = client.register_project(
                slug=identity["slug"], name=root.name,
                project_key=identity["project_key"],
            )
            pid = client.project_id(reg)
            if not pid:
                raise ValueError("云端未能返回有效的 project_id")
            remote_key = client.project_key(reg)
            if remote_key != identity["project_key"]:
                raise ValueError("云端返回 project_key 不一致，拒绝错误绑定")
            _save_project_binding(
                root, pid, identity["slug"], identity["project_key"], account_id,
            )
            # DOC-057 设计项 4：云端只接收紧凑投影，完整 profile 留在本地。
            res = client.put_project_profile(pid, project_profile.wire_project_profile(profile))
            if as_json:
                print(json.dumps({"status": "ok", "project_id": pid, "synced": True, "result": res.data}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx project] ✓ 已成功同步项目画像至云端 (Project ID: {pid})！")
            return 0
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": f"同步云端失败: {exc}"}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx project] ✗ 同步云端失败: {exc}")
            return 1

    elif action in ("list", "refresh"):
        try:
            projects = bugx_paths.sync_global_projects(workspace=root)
            if as_json:
                print(json.dumps({"status": "ok", "total": len(projects), "projects": projects}, ensure_ascii=False, indent=2))
            else:
                title = "已全量刷新并对齐" if action == "refresh" else "当前登记"
                print(f"[bugx project] ✓ {title}全局受管项目列表 (~/.bugx/projects.json)，共 {len(projects)} 个项目：")
                print("=" * 72)
                for idx, p in enumerate(projects, 1):
                    is_active = (Path(p["working_dir"]).resolve() == root.resolve())
                    tag = " [当前工作区]" if is_active else ""
                    cloud_tag = f" (云端 ID: {p['cloud_project_id']})" if p.get("cloud_project_id") else ""
                    sessions = p.get("session_count", 1)
                    print(f"  {idx:2d}. {p['name']:<22} {p['working_dir']}{tag}")
                    print(f"      会话: {sessions} 轮{cloud_tag} | Hash: {p['hash']}")
                print("=" * 72)
            return 0
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": f"刷新全局项目失败: {exc}"}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx project] ✗ 刷新全局项目失败: {exc}")
            return 1

    elif action == "rebind":
        return _rebind_project_identity(root, args, as_json)

    elif action == "settings":
        try:
            try:
                from core.runtime import project_settings
            except ImportError:
                from core.runtime import project_settings as project_settings

            target_val = getattr(args, "target", None)
            target_path = root
            if target_val:
                t_cand = Path(target_val).expanduser().resolve()
                if t_cand.is_dir():
                    target_path = t_cand
                else:
                    by_hash = project_settings.resolve_project_dir_by_hash(target_val)
                    if by_hash:
                        target_path = by_hash
                    else:
                        raise ValueError(f"无法定位目标项目路径或 Hash: {target_val}")

            updates = {}
            if getattr(args, "set_cloud_sync", None):
                updates["cloud_sync_enabled"] = (args.set_cloud_sync == "on")
            if getattr(args, "set_review", None):
                updates["review_mode"] = args.set_review
            if getattr(args, "set_polling", None):
                updates["polling_policy"] = args.set_polling
            if getattr(args, "set_auto_fix", None):
                v = args.set_auto_fix
                updates["fix_mode"] = "auto" if v == "on" else ("confirm" if v == "confirm" else "off")
            if getattr(args, "set_commit", None):
                updates["commit_policy"] = args.set_commit

            if updates:
                info = project_settings.update_project_settings(target_path, updates)
                updated_flag = True
            else:
                info = project_settings.get_project_settings(target_path)
                updated_flag = False

            if as_json:
                print(json.dumps({"status": "ok", "settings": info}, ensure_ascii=False, indent=2))
                return 0

            # 终端高颜值看板渲染
            gates = info["gates"]
            mining = info["mining"]
            mcps = info["mcps"]
            skills = info["skills"]

            print("=" * 72)
            print(f"       BugX 工程门禁配置与全景透视看板: {info['name']}")
            print("=" * 72)
            print(f"项目物理路径: {info['working_dir']}")
            print(f"项目 Hash 码: {info['project_hash']}")
            if updated_flag:
                print("⚡ [提示] 门禁配置已成功更新并原子落盘至 .bugx/config.json！")
            print()

            print("【1. 五大核心自愈与安全门禁 (Gates & Safeguards)】")
            cloud_str = f"🟢 开启 (绑定 Project ID: {gates['cloud_project_id']})" if gates["cloud_sync_enabled"] else "⚪ 未开启 (纯本地模式)"
            review_str = "🟢 全自动深入审查 (auto)" if gates["review_mode"] == "auto" else (
                "🟡 交互式逐步确认 (interactive)" if gates["review_mode"] == "interactive" else "🔴 关闭审查 (off)"
            )
            polling_str = "🟢 持续自治轮询 (auto)" if gates["polling_policy"] == "auto" else (
                "🟡 单轮手动触发 (manual)" if gates["polling_policy"] == "manual" else "🔴 关闭轮询 (off)"
            )
            auto_fix_str = "🟢 开启 (发现漏洞自动合入补丁)" if gates["auto_fix_code"] else (
                "🟡 需确认 (生成补丁待用户批准)" if gates["fix_mode"] == "confirm" else "🔴 关闭 (仅报告不出补丁)"
            )
            commit_str = "🟢 每轮自动提交 (round)" if gates["commit_policy"] == "round" else (
                "🟡 按任务提交 (task)" if gates["commit_policy"] == "task" else "🔴 关闭自动提交 (off)"
            )
            print(f"  1. 云端同步门禁 : {cloud_str}")
            print(f"  2. 审查方式门禁 : {review_str}")
            print(f"  3. 轮询策略门禁 : {polling_str}")
            print(f"  4. 修复模式门禁 : {auto_fix_str}")
            print(f"  5. 提交策略门禁 : {commit_str}")
            print(f"  * 修复前物理备份: {'✓ 强制开启 (带外沙箱镜像)' if gates['backup_before_fix'] else '✗ 已关闭'}")
            print()

            # 1.5 项目定位与体量属性画像
            prof = info.get("profile", {})
            pos = prof.get("positioning", {})
            met = prof.get("metrics", {})
            econ = prof.get("engineering_economics", {})
            qnr = prof.get("quality_and_risk", {})
            print("【1.5 项目定位与体量属性画像 (Positioning & Attributes Profile)】")
            forms_str = ", ".join(f.upper() for f in pos.get("forms", [])) or "通用工程"
            print(f"  - 工程定位形态 : [{forms_str}] | 规模级别: {pos.get('scale_level', 'medium').upper()} | 主语言: {pos.get('primary_language', 'unknown').capitalize()}")
            print(f"  - 代码规模净量 : 有效代码 {met.get('code_lines', 0):,} 行 (SLOC) | 总行数 {met.get('total_lines', 0):,} 行 | 文件数 {met.get('total_files', 0):,} 个 | 单测比 {met.get('test_to_code_ratio', 0.0) * 100:.1f}%")
            print(f"  - 软件工程经济 : 预估研发 {econ.get('effort_person_months', 0.0)} 人月 (周期: {econ.get('estimated_schedule_months', 0.0)} 个月) | 技术债务: {econ.get('tech_debt_days', 0.0)} 天 ({econ.get('tech_debt_ratio_pct', 0.0)}%)")
            print(f"  - 质量与脆弱性 : 评级 Grade {qnr.get('health_grade', 'B')} | 可维护性 {qnr.get('maintainability_index', 0.0)}/100 | 敏感沉入点: {qnr.get('sensitive_sinks', 0)} 处 | 污染流: {qnr.get('taint_flows', 0)} 条 | 秘密残留: {qnr.get('hardcoded_secrets', 0)} 处")
            print()

            print("【2. 挖掘策略、进度与轮次全景 (Mining Strategy, Progress & Rounds)】")
            cur_p = mining.get("current_polling_id")
            cur_p_no = mining.get("current_polling_no", 1)
            prog = mining.get("progress", {})
            strat = mining.get("strategy", {})
            print(f"  - 挖掘轮次状态 : 当前活跃轮次 {cur_p or '尚未启动'} (第 {cur_p_no} 轮) | 累计完成 {mining.get('total_rounds', 0)} 轮")
            print(f"  - 挖掘执行进度 : [{prog.get('status', 'IDLE').upper()}] {prog.get('current_phase', '就绪')}")
            cov_tot = prog.get('coverage_total', 0)
            cov_cov = prog.get('coverage_covered', 0)
            cov_pct = prog.get('coverage_pct', 0.0)
            print(f"  - 单元覆盖进度 : {cov_cov} / {cov_tot} ({cov_pct}%) | 耗时: {prog.get('elapsed_display', '0s')}")
            langs = ", ".join(strat.get("languages", []))
            surfs = ", ".join(strat.get("surfaces", []))
            print(f"  - 靶向攻防策略 : 语言: [{langs}] | 暴露面: [{surfs}]")
            print(f"  - 调度建议基线 : 评级 Grade {strat.get('health_grade', 'B')} | 推荐 {strat.get('recommended_concurrency', 4)} 并发 / {strat.get('recommended_rounds', 5)} 轮次")
            findings = mining.get("findings", {})
            print(f"  - 累计发现漏洞 : 共 {findings.get('total', 0)} 个 (严重: {findings.get('critical', 0)}, 高危: {findings.get('high', 0)}, 中危: {findings.get('medium', 0)}, 低危: {findings.get('low', 0)})")
            print(f"  - 积累防御经验 : {mining.get('lessons_learned', 0)} 条 (错题集知识库)")
            rounds_list = mining.get("rounds_history", [])
            if rounds_list:
                print("  - 历史各轮流水 :")
                for r in rounds_list[:4]:
                    print(f"    * #{r['polling_no']:<2} {r['polling_id']:<16} [{r['state'].upper():<9}] 耗时 {r['elapsed_display']:<7} | 漏洞: {r['findings_count']} 个 | {r['created_at']}")
            print()

            print(f"【3. 挂载的 MCP 服务器与工具 ({mcps['total_servers']} 个服务, 共 {mcps['total_tools']} 个工具)】")
            for s in mcps["servers"]:
                src = f" [来源: {s.get('source')}]" if s.get("source") else ""
                print(f"  - {s['name']:<22} {s.get('tools', 0):>2} 工具 | 状态: {s.get('status', 'connected')}{src}")
                if s.get("command"):
                    print(f"    命令: {s['command']}")
            print()

            print(f"【4. 可用与激活的 Skill 技能集 ({skills['total_skills']} 个技能)】")
            for sk in skills["skills"]:
                tag = f"[{sk.get('type', 'core').upper()}]"
                print(f"  - {sk['command']:<10} {tag:<8} {sk.get('description', '')}")
            print("=" * 72)
            return 0
        except Exception as exc:
            if as_json:
                print(json.dumps({"status": "error", "error": f"获取项目配置失败: {exc}"}, ensure_ascii=False, indent=2))
            else:
                print(f"[bugx project settings] ✗ 获取项目配置失败: {exc}")
            return 1

    else:
        print(f"[bugx project] 未知子操作: {action}", file=sys.stderr)
        return 1

    if as_json:
        payload = {
            "status": "ok",
            "root": str(root),
            "profile": profile,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    # 终端看板渲染
    metrics = profile.get("code_metrics", {})
    scale = profile.get("scale_estimation", {})
    languages = profile.get("code_languages", [])
    modules = profile.get("code_modules", [])
    forms = profile.get("forms", [])

    print("=" * 72)
    print("                 BugX 工程体量与技术栈代码分析看板")
    print("=" * 72)
    print(f"工程路径: {root}")
    print(f"工程形态: {', '.join(forms) if forms else '通用代码库'}")
    print(f"主编程语言: {scale.get('primary_language', 'unknown').capitalize()}")
    print()

    print("【代码规模指标】")
    tot_files = metrics.get("total_files", 0)
    tot_lines = metrics.get("total_lines", 0)
    sloc = metrics.get("code_lines", 0)
    doc_config = metrics.get("doc_and_config_lines", 0)
    com = metrics.get("comment_lines", 0)
    doc = metrics.get("docstring_lines", 0)
    blk = metrics.get("blank_lines", 0)
    print(f"  - 源码文件总数: {tot_files:,} 个")
    print(f"  - 物理总行数  : {tot_lines:,} 行")
    print(f"  - 有效代码行数: {sloc:,} 行 (SLOC, 编程语言净代码)")
    if doc_config > 0:
        print(f"  - 文档与配置  : {doc_config:,} 行 (Markdown, JSON, YAML 等)")
    print(f"  - 注释与文档  : {com + doc:,} 行 (注释: {com:,}, Docstring: {doc:,})")
    print(f"  - 空白行数    : {blk:,} 行")
    print()

    print("【质量与自动化测试基线】")
    prod_sloc = metrics.get("production_code_lines", 0)
    test_sloc = metrics.get("test_code_lines", 0)
    prod_files = metrics.get("production_files", 0)
    test_files = metrics.get("test_files", 0)
    ratio = metrics.get("test_to_code_ratio", 0.0)
    print(f"  - 生产代码体量: {prod_sloc:,} 行 ({prod_files:,} 个文件)")
    print(f"  - 自动化测试  : {test_sloc:,} 行 ({test_files:,} 个测试文件)")
    print(f"  - 测试/生产比 : {ratio:.1%} (Test-to-Code Ratio)")
    if ratio < 0.15:
        print("  - 质量预警    : ⚠️ 自动化测试代码占比较低 (<15%)，建议补充回归用例")
    elif ratio >= 0.5:
        print("  - 质量评级    : 🟢 自动化测试完备度极高 (≥50%)，具备扎实的回归与变异基准")
    else:
        print("  - 质量评级    : 🔵 自动化测试覆盖基线良好")
    print()

    econ = profile.get("engineering_economics", {})
    if econ:
        effort = econ.get("effort_person_months", 0.0)
        sched = econ.get("estimated_schedule_months", 0.0)
        debt = econ.get("tech_debt_days", 0.0)
        debt_pct = econ.get("tech_debt_ratio_pct", 0.0)
        print("【软件工程经济学估算 (COCOMO II 基线)】")
        print(f"  - 基准研发工作量: ~{effort:.1f} 人月 (Person-Months)")
        print(f"  - 理论开发周期  : ~{sched:.1f} 个月 (标准团队规模基线)")
        print(f"  - 技术债务存量  : ~{debt:.1f} 人天 (待治理技术债工时估算)")
        print(f"  - 技术债务比率  : ~{debt_pct:.1f}% (Debt Ratio)")
        print()

    maint = profile.get("maintainability", {})
    dup = profile.get("duplication", {})
    if maint:
        mi = maint.get("maintainability_index", 100.0)
        mi_grade = maint.get("maintainability_grade", "🟢 良好")
        avg_cog = maint.get("avg_cognitive_complexity", 0.0)
        max_cog = maint.get("max_cognitive_complexity", 0)
        dup_pct = dup.get("duplication_percentage", 0.0)
        dup_lines = dup.get("duplicated_lines", 0)

        print("【代码可维护性与认知负荷】")
        print(f"  - 可维护性指数  : {mi:.1f} / 100 ({mi_grade})")
        print(f"  - 认知复杂度基准: 平均 {avg_cog:.1f} (单文件峰值: {max_cog})")
        print(f"  - 代码克隆重复率: {dup_pct:.1f}% ({dup_lines:,} 行重复代码)")
        print()

    topo = profile.get("architecture_topology", {})
    if topo:
        tot_cycles = topo.get("total_cycles", 0)
        cycles = topo.get("cycles", [])
        unstable = topo.get("unstable_modules", [])

        print("【架构拓扑与耦合稳定性】")
        if tot_cycles > 0:
            print(f"  - 模块循环依赖环: ⚠️ 探测到 {tot_cycles} 个循环依赖链条")
            for c in cycles[:2]:
                chain_str = " ⇄ ".join(c[:3])
                print(f"    • {chain_str}")
        else:
            print("  - 模块循环依赖环: 🟢 0 处循环导入 (拓扑结构无环健康)")

        if unstable:
            top_u = unstable[0]
            print(f"  - 架构不稳定靶点: {top_u['file']} (不稳定度: {top_u['instability']}, Fan-out: {top_u['fan_out']})")
        print()

    risk = profile.get("risk_metrics", {})
    if risk:
        health_grade = risk.get("health_grade", "N/A")
        grade_icons = {"A": "🟢 A (优秀)", "B": "🔵 B (良好)", "C": "🟡 C (关注)", "D": "🔴 D (高危)"}
        grade_desc = grade_icons.get(health_grade, health_grade)
        avg_cc = risk.get("avg_cyclomatic_complexity", 0.0)
        max_cc = risk.get("max_cyclomatic_complexity", 0)
        funcs = risk.get("total_functions", 0)
        classes = risk.get("total_classes", 0)
        god_mods = risk.get("god_modules_count", 0)
        god_funcs = risk.get("god_functions_count", 0)
        sinks = risk.get("total_sensitive_sinks", 0)
        taints = risk.get("total_taint_flows", 0)
        secrets = risk.get("total_hardcoded_secrets", 0)
        hotspots = risk.get("top_risk_hotspots", [])

        print("【代码复杂度与内在风险画像】")
        print(f"  - 代码健康评级  : {grade_desc}")
        print(f"  - 圈复杂度基准  : 平均 {avg_cc:.1f} (单文件峰值: {max_cc})")
        print(f"  - 代码结构要素  : {funcs:,} 个函数/方法, {classes:,} 个类结构")
        print(f"  - 架构异味探测  : {god_mods} 个巨石模块 (God Module), {god_funcs} 个神仙函数 (God Function)")
        print(f"  - 敏感高危槽位  : {sinks} 处潜在敏感调用 (命令执行/代码解析/反序列化/动态SQL等)")
        if taints > 0:
            print(f"  - 关键污点流向  : ⚠️ {taints} 条入参直通敏感 Sink 路径 (Taint Flow)")
        if secrets > 0:
            print(f"  - 硬编码机密泄露: ⚠️ {secrets} 处高熵敏感密钥 (Shannon Entropy)")
        print()

        if hotspots:
            print("【TOP 风险热点关注 (前 5 个高危靶点)】")
            for h in hotspots[:5]:
                file_rel = h.get("file", "")
                if len(file_rel) > 34:
                    file_rel = "..." + file_rel[-31:]
                r_score = h.get("risk_score", 0)
                cc = h.get("cyclomatic_complexity", 0)
                cog = h.get("cognitive_complexity", 0)
                s_count = h.get("sensitive_sinks", 0)
                tags = " ".join(h.get("tags", []))
                print(f"  • {file_rel:<34} 风险分: {r_score:>2} | CC:{cc:>2} | Cog:{cog:>2} | Sinks:{s_count:>2}  {tags}")
            print()

    print("【工程体量与审计估模推荐】")
    level = scale.get("scale_level", "unknown").upper()
    conc = scale.get("recommended_concurrency", 1)
    rounds = scale.get("recommended_rounds", 1)
    budget = scale.get("recommended_token_budget", 50000)
    print(f"  - 工程规模分级: [{level}] 规模项目")
    print(f"  - 建议并发度  : {conc} Workers 并发执行")
    print(f"  - 建议审计轮次: {rounds} 轮 (建议 Token 预算: ~{budget:,} Tokens)")
    print()

    code_langs = [l for l in languages if l.get("is_code", True)]
    non_code_langs = [l for l in languages if not l.get("is_code", True)]

    if code_langs:
        print("【主要编程语言分布】")
        for l in code_langs[:6]:
            lang_name = l["language"].capitalize()
            l_code = l["code_lines"]
            pct = l["percentage"]
            bar = _render_bar(pct, width=18)
            print(f"  {lang_name:<14} {bar}  {pct:>5.1f}%  ({l_code:>6,} SLOC, {l['files']} 文件)")
        print()

    if non_code_langs:
        print("【文档与配置资源分布】")
        for l in non_code_langs[:4]:
            lang_name = l["language"].capitalize()
            l_code = l["code_lines"]
            pct = l["percentage"]
            bar = _render_bar(pct, width=18)
            print(f"  {lang_name:<14} {bar}  {pct:>5.1f}%  ({l_code:>6,} 行, {l['files']} 文件)")
        print()

    if modules:
        print("【核心模块/目录分布】")
        for m in modules[:8]:
            m_name = m["module"]
            if len(m_name) > 16:
                m_name = m_name[:13] + "..."
            m_code = m["code_lines"]
            pct = m["percentage"]
            bar = _render_bar(pct, width=14)
            print(f"  {m_name:<16} {bar}  {pct:>5.1f}%  ({m_code:>6,} SLOC)")
        print()

    print("=" * 72)
    print("提示：执行 'bugx cloud sync' 可将此项目画像与代码指标同步至云端看板。")
    return 0


def _parse_line_range(raw: str | None) -> tuple[int, int | None]:
    """解析 42:90、42-90 或 42 格式的行范围字符串。"""
    if not raw:
        return 1, 200
    cleaned = raw.strip()
    if ":" in cleaned:
        parts = cleaned.split(":", 1)
    elif "-" in cleaned and not cleaned.startswith("-"):
        parts = cleaned.split("-", 1)
    else:
        try:
            val = int(cleaned)
            return val, val
        except ValueError:
            return 1, 200

    try:
        start = int(parts[0].strip()) if parts[0].strip() else 1
    except ValueError:
        start = 1
    try:
        end = int(parts[1].strip()) if parts[1].strip() else None
    except ValueError:
        end = None
    return start, end


def command_view(args: argparse.Namespace) -> int:
    """bugx view：安全查看源文件切片与视窗（具越界自愈与沙箱隔离）。"""
    path_str = getattr(args, "path", "")
    lines_arg = getattr(args, "lines", "1:200")
    numbered = not bool(getattr(args, "no_numbers", False))
    as_json = bool(getattr(args, "json", False))
    max_lines = int(getattr(args, "max_lines", 500) or 500)
    root = getattr(args, "root", None)

    try:
        _ensure_core_importable()
        from core.planning import source_inspector
    except Exception as exc:
        if as_json:
            print(json.dumps({"status": "error", "error": f"无法加载核心查看引擎: {exc}"}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx view] ✗ 无法加载核心查看引擎: {exc}", file=sys.stderr)
        return 1

    start_line, end_line = _parse_line_range(lines_arg)

    try:
        result = source_inspector.inspect_source(
            path=path_str,
            start_line=start_line,
            end_line=end_line,
            numbered=numbered,
            max_lines=max_lines,
            root=root,
        )
    except Exception as exc:
        if as_json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[bugx view] ✗ {exc}", file=sys.stderr)
        return 1

    if as_json:
        payload = {"status": "ok", **result.to_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if result.warning:
            print(f"[bugx view] 提示: {result.warning}", file=sys.stderr)
        if result.content:
            print(result.content)
        elif result.total_lines > 0:
            print(f"[bugx view] （所请求范围 {start_line}:{end_line} 无内容，文件总行数: {result.total_lines}）")
        else:
            print("[bugx view] （文件为空）")

    return 0

