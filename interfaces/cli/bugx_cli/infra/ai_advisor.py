#!/usr/bin/env python3
"""src/bugx_cli/infra/ai_advisor.py - AI 辅助意图理解、智能排障与确定性自愈协同引擎。

设计原则：
- AI 负责：理解非结构化报错、补充根因分析与语义模糊匹配；
- 程序负责：选择已实现的修复路径、原子操作执行、文件沙箱隔离与确定性验活门禁；
- 双轨协同：在线/宿主可用时唤起宿主 AI 动态推断，离线/受限环境平滑退守本地确定性专家基线。
"""

from __future__ import annotations

import difflib
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .. import __version__


COMMAND_CAPABILITIES: dict[str, dict[str, Any]] = {
    "init": {
        "description": "初始化项目 BugX 核心环境与运行态",
        "keywords": ["初始化", "建仓", "开始", "创建", "setup", "start", "initialize", "new"],
    },
    "doctor": {
        "description": "环境健康度诊断、依赖检测与一键自愈",
        "keywords": ["体检", "健康", "诊断", "排查", "修复", "自愈", "检查", "环境", "check", "fix", "health", "doctor"],
    },
    "hosts": {
        "description": "发现系统中已安装的可接入 AI 宿主 Agent",
        "keywords": ["宿主", "智能体", "接入", "agent", "hosts", "discover"],
    },
    "install": {
        "description": "安装 BugX MCP / Skill 至已发现的 AI 宿主",
        "keywords": ["安装", "集成", "配置宿主", "install", "setup", "integrate"],
    },
    "uninstall": {
        "description": "从指定 AI 宿主卸载 BugX 集成",
        "keywords": ["卸载", "清理", "移除", "uninstall", "remove"],
    },
    "audit": {
        "description": "项目级漏洞渗透挖掘与代码安全审计",
        "keywords": ["审计", "漏洞", "扫描", "找洞", "安全", "渗透", "audit", "scan", "security", "vulnerability"],
    },
    "autonomous": {
        "description": "启动全自动无人值守 BugX 漏洞挖掘",
        "keywords": ["自主", "无人值守", "自动挖掘", "挂机", "探索", "autonomous", "mine", "auto"],
    },
    "analyze": {
        "description": "项目画像深度分析与代码覆盖度全景扫描",
        "keywords": ["画像", "分析", "覆盖度", "项目全景", "技术栈", "analyze", "profile", "coverage"],
    },
    "project": {
        "description": "项目元数据管理、画像展示与云端同步",
        "keywords": ["项目", "元数据", "云同步", "project", "sync"],
    },
    "patch": {
        "description": "补丁合成、验证、对抗与自动应用",
        "keywords": ["补丁", "修复代码", "修bug", "合成", "patch", "repair-code"],
    },
    "skill": {
        "description": "BugX 动态扩展 Skill 市场管理与挂载",
        "keywords": ["技能", "插件", "skill", "plugins"],
    },
    "cloud": {
        "description": "BugX 云端多设备协同、同步与设备认证",
        "keywords": ["云端", "多设备", "同步", "协同", "cloud", "sync"],
    },
    "auth": {
        "description": "云端登录、凭据管理与设备配对",
        "keywords": ["登录", "认证", "token", "凭证", "auth", "login"],
    },
    "stats": {
        "description": "代码漏洞挖掘统计指标与 ROI 分析",
        "keywords": ["统计", "度量", "指标", "投入产出", "stats", "metrics"],
    },
    "report": {
        "description": "本地 Web 控制台与审计报告预览",
        "keywords": ["报告", "控制台", "预览", "看板", "report", "console", "dashboard", "web"],
    },
    "engine": {
        "description": "全局引擎核心管理与状态查询",
        "keywords": ["引擎", "核心", "版本", "engine", "core"],
    },
    "daemon": {
        "description": "BugX 后台常驻守护进程管理",
        "keywords": ["守护", "后台", "常驻", "daemon", "service"],
    },
    "reset": {
        "description": "重置项目运行态或全局错题集缓存",
        "keywords": ["重置", "清空", "还原", "reset", "clear"],
    },
    "help": {
        "description": "查看 BugX 命令使用说明",
        "keywords": ["帮助", "说明", "手册", "help", "usage"],
    },
}


@dataclass
class IntentArbitrationResult:
    matched_command: str | None
    suggestions: list[tuple[str, str]]
    source: str  # "ai_dynamic", "heuristic_synonym", "fuzzy_match", "none"
    reasoning: str = ""


class CommandIntentArbitrator:
    """AI 与程序协同的命令意图理解与纠错仲裁器。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()

    def infer_intent(self, raw_input: str) -> IntentArbitrationResult:
        cleaned = str(raw_input or "").strip()
        if not cleaned:
            return IntentArbitrationResult(None, [], "none", "输入为空")

        # 1. 本地快速同义词与关键词精确初筛
        low = cleaned.casefold()
        for cmd, meta in COMMAND_CAPABILITIES.items():
            if low == cmd or low in meta["keywords"]:
                return IntentArbitrationResult(
                    matched_command=cmd,
                    suggestions=[(cmd, meta["description"])],
                    source="heuristic_synonym",
                    reasoning=f"输入精确匹配命令或同义词: '{cleaned}'",
                )

        # 2. 尝试调用宿主 AI 进行动态语义意图推断 (若当前处于在线宿主环境)
        ai_result = self._try_ai_dynamic_inference(cleaned)
        if ai_result:
            return ai_result

        # 3. 本地语义关键词多路打分启发式匹配
        scored: dict[str, int] = {}
        for cmd, meta in COMMAND_CAPABILITIES.items():
            score = 0
            for kw in meta["keywords"]:
                kw_low = kw.casefold()
                if len(kw_low) >= 2 and kw_low in low:
                    score += len(kw_low) * 10
            if score > 0:
                scored[cmd] = score

        if scored:
            ranked = sorted(scored.items(), key=lambda x: x[1], reverse=True)
            top_cmd = ranked[0][0]
            suggestions = [
                (c, COMMAND_CAPABILITIES[c]["description"])
                for c, _ in ranked[:2]
            ]
            return IntentArbitrationResult(
                matched_command=top_cmd,
                suggestions=suggestions,
                source="heuristic_synonym",
                reasoning=f"根据语义关键词匹配推荐: '{cleaned}'",
            )

        # 4. 兜底编辑距离模糊匹配 (difflib)
        candidates = list(COMMAND_CAPABILITIES.keys())
        close = difflib.get_close_matches(low, candidates, n=2, cutoff=0.35)
        if close:
            return IntentArbitrationResult(
                matched_command=close[0],
                suggestions=[(c, COMMAND_CAPABILITIES[c]["description"]) for c in close],
                source="fuzzy_match",
                reasoning=f"编辑距离近似推荐",
            )

        return IntentArbitrationResult(None, [], "none", "未匹配到有效意图")

    def _try_ai_dynamic_inference(self, raw_input: str) -> IntentArbitrationResult | None:
        """调用宿主 AI 理解复杂自然语言或未知拼写意图。"""
        if os.environ.get("BUGX_OFFLINE") == "1" or os.environ.get("BUGX_INTENT_NO_LLM") == "1":
            return None

        active_host = ""
        try:
            from core.host_runtime import host_registry
            active_host = host_registry.resolve_scout_host(self.root)
        except Exception:
            return None

        if not active_host:
            return None

        try:
            from core.host_runtime import host_registry

            prompt_payload = {
                "user_input": raw_input,
                "supported_commands": {k: v["description"] for k, v in COMMAND_CAPABILITIES.items()},
            }
            prompt_content = (
                "<system_rules>\n"
                "You are the BugX CLI Intent Parser.\n"
                "Understand user's input (Chinese or English, possibly with typos or natural language) and map to the most accurate BugX command.\n"
                "Output STRICTLY a JSON object with this exact schema (no markdown, no other text):\n"
                "{\n"
                '  "matched_command": "one of supported_commands or null",\n'
                '  "confidence": "high|medium|low",\n'
                '  "reasoning": "brief explanation in Chinese"\n'
                "}\n"
                "</system_rules>\n"
                "<context>\n"
                f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}\n"
                "</context>\n"
            )

            tmp_prompt = self.root / ".bugx" / "run" / "intent_prompt.txt"
            tmp_prompt.parent.mkdir(parents=True, exist_ok=True)
            tmp_prompt.write_text(prompt_content, encoding="utf-8")

            cmd = host_registry.build_recon_scout_command(active_host, self.root, tmp_prompt)
            if not cmd:
                return None
            child_env = os.environ.copy()
            child_env["BUGX_RECON_SCOUT_ACTIVE"] = "1"

            res = subprocess.run(
                cmd, cwd=self.root, capture_output=True, text=True,
                timeout=10, env=child_env, check=False,
            )
            raw_out = (res.stdout or "").strip()
            # 提取 JSON 对象
            m = re.search(r"\{[\s\S]*\}", raw_out)
            if not m:
                return None
            data = json.loads(m.group(0))
            matched = data.get("matched_command")
            if matched and matched in COMMAND_CAPABILITIES:
                return IntentArbitrationResult(
                    matched_command=matched,
                    suggestions=[(matched, COMMAND_CAPABILITIES[matched]["description"])],
                    source="ai_dynamic",
                    reasoning=data.get("reasoning", "AI 语义意图理解推荐"),
                )
        except Exception:
            return None
        return None


@dataclass
class DiagnosticItem:
    name: str
    detail: str
    required: bool
    root_cause: str
    prescription: str
    auto_repairable: bool
    confidence: str = "high"


@dataclass
class DiagnosticsResult:
    summary: str
    can_auto_repair: bool
    auto_repair_command: str | None
    items: list[DiagnosticItem] = field(default_factory=list)
    ai_arbitrated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "can_auto_repair": self.can_auto_repair,
            "auto_repair_command": self.auto_repair_command,
            "ai_arbitrated": self.ai_arbitrated,
            "items": [
                {
                    "name": item.name,
                    "detail": item.detail,
                    "required": item.required,
                    "root_cause": item.root_cause,
                    "prescription": item.prescription,
                    "auto_repairable": item.auto_repairable,
                    "confidence": item.confidence,
                }
                for item in self.items
            ],
        }


class DiagnosticAIArbitrator:
    """AI 根因分析与程序确定性修复提示仲裁器。"""

    # 本地确定性诊断知识。模式必须完整匹配检查项名称，未知检查不得继承修复能力。
    BASELINE_KNOWLEDGE = [
        (
            r"Python >= 3\.11",
            "当前 Python 解释器版本低于 3.11 或运行态环境不满足要求，缺少异步上下文与现代类型特性。",
            "请升级 Python 至 >= 3.11，或激活符合要求的虚拟环境 (例如: conda activate py311 或 pyenv shell 3.11)。",
        ),
        (
            r"安装资源",
            "核心静态资源或模板文件缺失，可能由于打包解压损坏或安装目录被意外清理。",
            "检查报错中的安装资源目录；使用 `bugx update --repair-assets` 修复当前 Python 安装资源，或重装经校验的 BugX wheel。",
        ),
        (
            r"(?:全局引擎完整性|引擎副本一致性)",
            "全局引擎代码清单 (engine-integrity.json) 哈希不匹配或文件缺失，存在版本漂移或代码被意外修改。",
            "`bugx doctor --repair` 在两种情况下会重放安装资源修好引擎：(1) 安装资源 Build ID 严格高于全局引擎；(2) 同 Build ID 且安装资源自身完好。仅当资源自身也损坏（或需要降级）时才会拒绝，此时请重装 CLI。",
        ),
        (
            r"BugX 版本一致性",
            "CLI 命令入口与全局引擎、镜像清单版本声明不一致，存在环境版本漂移。",
            "运行 `bugx doctor --repair` 前先确认安装资源 Build ID 严格高于全局引擎；不满足时升级 CLI wheel 后重试。",
        ),
        (
            r"项目初始化状态",
            # 旧文案硬编码"manifest.json 缺失"，但 initialized 实际由
            # manifest_ok ∧ layout ∧ init_issues 三者合取判定，最常见的
            # 实因是缺 .bugx/cloud-onboard.json。模板不得覆盖真实 detail，
            # 否则用户按"manifest 缺失"排查会走进死胡同（实测该文件存在
            # 且 project_id 完全匹配，诊断仍报缺失）。
            "项目初始化未通过完整性判定（manifest 存在不代表就绪：还需 .bugx 布局可加载、"
            "云端 onboarding 记录与启动视图齐备）；具体缺口见本项 detail。",
            "先只读运行 `bugx engine --root <ROOT> status --json`；只有状态精确为 `UNINITIALIZED` 时才运行 `bugx engine --root <ROOT> pre`。若返回 `FATAL_BLOCKED`、其他状态或检查失败，保留现场并停止；不得用 `bugx init`、`bugx reset` 或 `--force` 绕过。",
        ),
        (
            r"工作树卫生与嵌套隔离",
            "当前工作树存在未跟踪改动或存在嵌套 Git 仓库冲突，可能导致自动化漏洞验证隔离失效。",
            "检查 git status，将未跟踪的临时产物加入 .gitignore，或提交当前未保存的代码。",
        ),
        (
            r"云端端点连通性",
            "与 BugX 云端服务协同端点通信失败，可能由于未登录鉴权、网络代理阻塞或端点不可达。",
            "若需鉴权请运行 bugx auth login；若为网络问题请检查系统网络与 BUGX_CLOUD_API_BASE 设置。",
        ),
        (
            r"项目资产修复",
            "项目 .bugx 目录中的运行态快照、current.json 或历史记录链存在断裂或文件损坏。",
            "查看 Doctor 列出的具体修复错误；缺少可信备份或校验失败时，从可信备份恢复，不能凭空重建运行态数据。",
        ),
        (
            r"MCP 离线 runtime",
            "本地离线 MCP 运行时沙箱未建立或环境依赖缺失，导致离线代码工具链不可用。",
            "按需运行 `bugx system mcp-runtime install` 建立隔离运行时；这不是 `bugx doctor --repair` 的修复项。",
        ),
        (
            r"MCP 会话进程卫生",
            "检测到系统中存在失控或遗留的 MCP 僵尸进程，可能持续占用端口与系统句柄。",
            "运行 bugx system mcp-runtime clean 清理遗留僵尸进程。",
        ),
        (
            r"BugX CLI 路径",
            "PATH 环境变量中同时存在多个不同包管理器安装的 bugx 入口（例如 npm 与 pip 冲突）。",
            "建议卸载冲突的多余版本（npm: npm uninstall -g bugx-cli，pip: pip uninstall bugx-cli）。",
        ),
        (
            r"基础效率工具",
            "系统缺失部分提速工具（如 ast-grep, ripgrep 等），当前已平滑降级至内置 Python 解析引擎。",
            "可使用包管理器安装对应外部工具（如 brew install ast-grep ripgrep）提升大规模审计速度。",
        ),
        (
            r"Tree-sitter 多语言增强",
            "未检测到 tree-sitter-language-pack 库，跨语言语法树深度解析回退至词法模式。",
            "运行 `bugx doctor --repair` 可尝试安装 `tree-sitter-language-pack`；安装失败时请按错误详情检查 Python 环境和网络。",
        ),
        (
            r"codebase-memory 推荐加速",
            "知识图谱组件 codebase-memory 未就绪或未启动，当前运行于基础模式。",
            "这是可选加速组件；检查插件安装与索引状态，基础模式仍可运行。",
        ),
        (
            r"UI 视觉与浏览器挖掘环境(?:（可选）)?",
            "Playwright 浏览器驱动或系统渲染依赖未就绪，UI 自动化挖掘功能受限。",
            "按需运行 `python3 setup_ui_env.py install` 安装 Chromium 浏览器驱动及依赖。",
        ),
        (
            r".+ (?:完整适配包|MCP 配置|全局入口完整性|全局 Skill 完整性)",
            "宿主配置或适配资产与 BugX 当前模板不一致；本地文件可能包含用户自定义内容。",
            "先检查并保存目标配置中的本地修改，再按单个宿主执行 `bugx install --root <ROOT> --agent <HOST>`；不要用 Doctor 批量强制覆盖宿主资产。",
        ),
    ]

    # 只有这些修复路径由 Doctor 当前控制流直接实现；调用方还必须证明本次
    # 检查满足运行时前置条件。模型输出和静态知识均不能自行扩大这个集合。
    AUTO_REPAIRABLE_CHECKS = frozenset({
        "全局引擎完整性",
        "BugX 版本一致性",
        "Tree-sitter 多语言增强",
    })

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _baseline_for_name(self, name: str) -> tuple[str, str] | None:
        for pattern, cause, prescription in self.BASELINE_KNOWLEDGE:
            if re.fullmatch(pattern, name, re.IGNORECASE):
                return cause, prescription
        return None

    def diagnose(
        self,
        checks: list[tuple[str, bool, str]],
        installation_required: set[str],
        strict: bool = False,
        auto_repairable_checks: set[str] | None = None,
    ) -> DiagnosticsResult:
        eligible_repairs = set(auto_repairable_checks or ()) & self.AUTO_REPAIRABLE_CHECKS
        failed_raw = [(n, d, n in installation_required) for n, ok, d in checks if not ok]
        if not failed_raw:
            return DiagnosticsResult(
                summary="🎉 所有环境检测项与就绪门禁全部通过，BugX 处于最优运行状态！",
                can_auto_repair=False,
                auto_repair_command=None,
                items=[],
                ai_arbitrated=False,
            )

        # 1. 优先尝试唤起宿主 AI 生成动态深入诊断分析
        ai_items = self._try_ai_deep_diagnosis(failed_raw)
        if ai_items:
            for item in ai_items:
                baseline = self._baseline_for_name(item.name)
                item.prescription = baseline[1] if baseline else (
                    "该检查项没有经过本地规则验证的修复路径；请根据失败详情先做只读排查，勿运行批量修复。"
                )
                item.auto_repairable = item.name in eligible_repairs
                item.confidence = "medium" if baseline else "low"
            repairable_count = sum(1 for item in ai_items if item.auto_repairable)
            can_auto_repair = repairable_count > 0
            return DiagnosticsResult(
                summary=self._build_summary(len(ai_items), repairable_count),
                can_auto_repair=can_auto_repair,
                auto_repair_command="bugx doctor --repair" if can_auto_repair else None,
                items=ai_items,
                ai_arbitrated=True,
            )

        # 2. 回退到本地高可靠专家知识库
        baseline_items: list[DiagnosticItem] = []
        for name, detail, is_req in failed_raw:
            baseline = self._baseline_for_name(name)
            if baseline:
                canned_cause, prescription = baseline
                # 模板给出的是"可能的根因族"，detail 才是本机实测事实。
                # 两者必须同时呈现：只给模板会把用户引向错误的排查方向。
                cause = f"{canned_cause} 实测详情：{detail}" if detail else canned_cause
                confidence = "high"
            else:
                cause = f"检查项 '{name}' 返回未就绪状态：{detail}"
                prescription = "该检查项没有经过本地规则验证的修复路径；请根据失败详情先做只读排查，勿运行批量修复。"
                confidence = "low"
            baseline_items.append(DiagnosticItem(
                name=name,
                detail=detail,
                required=is_req,
                root_cause=cause,
                prescription=prescription,
                auto_repairable=name in eligible_repairs,
                confidence=confidence,
            ))

        repairable_count = sum(1 for item in baseline_items if item.auto_repairable)
        can_auto_repair = repairable_count > 0
        return DiagnosticsResult(
            summary=self._build_summary(len(baseline_items), repairable_count),
            can_auto_repair=can_auto_repair,
            auto_repair_command="bugx doctor --repair" if can_auto_repair else None,
            items=baseline_items,
            ai_arbitrated=False,
        )

    def _build_summary(self, total: int, repairable: int) -> str:
        if repairable == total and total > 0:
            return f"检测到 {total} 项环境异常，均有 Doctor 已实现的修复路径；可运行 `bugx doctor --repair` 尝试处理。"
        if repairable > 0:
            return f"检测到 {total} 项环境异常，其中 {repairable} 项有 Doctor 已实现的修复路径。"
        return f"检测到 {total} 项环境异常，请参考下方诊断和对应处置指引。"

    def _try_ai_deep_diagnosis(
        self,
        failed_raw: list[tuple[str, str, bool]],
    ) -> list[DiagnosticItem] | None:
        """调用宿主 AI 分析当前异常根因；修复权限由本地逻辑裁定。"""
        if os.environ.get("BUGX_OFFLINE") == "1" or os.environ.get("BUGX_DOCTOR_NO_LLM") == "1":
            return None

        active_host = ""
        try:
            from core.host_runtime import host_registry
            active_host = host_registry.resolve_scout_host(self.root)
        except Exception:
            return None

        if not active_host:
            return None

        try:
            from core.host_runtime import host_registry

            prompt_payload = {
                "os": sys.platform,
                "python_version": sys.version,
                "failed_checks": [
                    {"name": n, "detail": d, "is_required": r}
                    for n, d, r in failed_raw
                ],
            }
            prompt_content = (
                "<system_rules>\n"
                "You are the BugX System Diagnostic Expert.\n"
                "Analyze the failed environment checks and explain a likely technical root cause for each item.\n"
                "Do not prescribe commands or classify repairability; BugX will use locally verified repair rules for those decisions.\n"
                "Output STRICTLY a JSON object with this exact schema (no markdown, no other text):\n"
                "{\n"
                '  "diagnostics": [\n'
                '    {\n'
                '      "name": "check item name",\n'
                '      "root_cause": "likely technical root cause in Chinese"\n'
                '    }\n'
                '  ]\n'
                "}\n"
                "</system_rules>\n"
                "<context>\n"
                f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}\n"
                "</context>\n"
            )

            # 临时 prompt 必须落在**项目树之外**：``bugx doctor`` 对外承诺
            # 「默认只读诊断」，此前写 ``<root>/.bugx/run/doctor_prompt.txt``
            # 会连带创建 ``.bugx/run`` 等 23 个条目，直接违反零写盘契约
            # （test_a1_doctor_is_read_only_by_default 实测失败）。改用系统临时目录，
            # 用完即删。
            import tempfile

            tmp_fd, tmp_prompt_name = tempfile.mkstemp(
                prefix="bugx-doctor-prompt-", suffix=".txt",
            )
            os.close(tmp_fd)
            tmp_prompt = Path(tmp_prompt_name)
            try:
                tmp_prompt.write_text(prompt_content, encoding="utf-8")
            except OSError:
                tmp_prompt.unlink(missing_ok=True)
                return None

            cmd = host_registry.build_recon_scout_command(active_host, self.root, tmp_prompt)
            if not cmd:
                return None
            child_env = os.environ.copy()
            child_env["BUGX_RECON_SCOUT_ACTIVE"] = "1"

            try:
                res = subprocess.run(
                    cmd, cwd=self.root, capture_output=True, text=True,
                    timeout=12, env=child_env, check=False,
                )
            finally:
                # 临时 prompt 已在子进程读取后被消费，立即清理避免 /tmp 泄漏。
                tmp_prompt.unlink(missing_ok=True)
            raw_out = (res.stdout or "").strip()
            m = re.search(r"\{[\s\S]*\}", raw_out)
            if not m:
                return None
            data = json.loads(m.group(0))
            diag_list = data.get("diagnostics")
            if not isinstance(diag_list, list) or len(diag_list) != len(failed_raw):
                return None

            items: list[DiagnosticItem] = []
            expected_names = [name for name, _detail, _required in failed_raw]
            if any(not isinstance(item, dict) for item in diag_list):
                return None
            returned_names = [item.get("name") for item in diag_list]
            if (
                any(not isinstance(name, str) for name in returned_names)
                or len(set(returned_names)) != len(expected_names)
                or set(returned_names) != set(expected_names)
            ):
                return None
            diag_map = {item["name"]: item for item in diag_list}
            for n, d, r in failed_raw:
                parsed = diag_map[n]
                cause = parsed.get("root_cause")
                if not isinstance(cause, str) or not cause.strip():
                    return None
                items.append(DiagnosticItem(
                    name=n,
                    detail=d,
                    required=r,
                    root_cause=cause,
                    prescription="",
                    auto_repairable=False,
                    confidence="medium",
                ))
            return items
        except Exception:
            return None


def _is_pid_alive(pid: int) -> bool:
    """跨平台安全检测进程 PID 是否存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as err:
        return err.errno == errno.EPERM


class DeterministicHealer:
    """只清理已确认失效的 BugX 锁；不创建或重建项目状态。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def heal_stale_locks(self) -> tuple[bool, str]:
        """扫描并解除残留的僵尸锁文件（PID 已消亡或锁文件过期）。"""
        dot_bugx = self.root / ".bugx"
        if dot_bugx.is_symlink() or not dot_bugx.is_dir():
            return False, "锁目录不存在，未修改项目状态"

        removed_locks: list[str] = []
        lock_candidates = list(dot_bugx.rglob("*.lock"))
        now = time.time()

        for lock_file in lock_candidates:
            if not lock_file.is_file():
                continue
            should_remove = False
            reason = ""

            try:
                content = lock_file.read_text(encoding="utf-8").strip()
                if not content:
                    if now - lock_file.stat().st_mtime > 5:
                        should_remove = True
                        reason = "空锁超时"
                else:
                    pid = None
                    try:
                        data = json.loads(content)
                        if isinstance(data, dict):
                            pid = data.get("pid")
                    except Exception:
                        try:
                            pid = int(content)
                        except Exception:
                            pass

                    if pid is not None and isinstance(pid, int):
                        if not _is_pid_alive(pid):
                            should_remove = True
                            reason = f"持有锁的进程 PID {pid} 已消亡"
                    else:
                        if now - lock_file.stat().st_mtime > 60:
                            should_remove = True
                            reason = "锁文件超时残留"
            except Exception:
                pass

            if should_remove:
                try:
                    lock_file.unlink()
                    removed_locks.append(f"{lock_file.name} ({reason})")
                except Exception:
                    pass

        if removed_locks:
            return True, f"已安全清除僵尸锁: {', '.join(removed_locks)}"
        return False, "未发现可安全清除的僵尸锁"

class RuntimeSelfHealInterceptor:
    """运行时执行异常拦截与就地自愈恢复器。

    只允许清理已确认失效的锁并最多重试一次。项目布局、身份、manifest、配置、
    JSON 损坏和启动门禁错误均由调用方保留现场并 fail-closed。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._retried = False

    def can_heal(self, exc: Exception, command: str | None = None) -> bool:
        """仅白名单内的锁故障可进入确定性修复。"""
        if os.environ.get("BUGX_NO_AUTO_HEAL") == "1":
            return False
        if self._retried:
            return False

        text = str(exc).casefold()
        if "fatal_blocked" in text or "project_layout=" in text:
            return False

        # 锁错误也可能出现在门禁或损坏状态的描述中；这些标记始终优先拒绝。
        project_state_markers = (
            "尚未初始化", "not initialized", "manifest.json", "current.json",
            ".bugx-project.json", "缺少项目身份", "身份文件损坏", "配置损坏",
            "jsondecodeerror", "json.decoder", "expecting value", "unterminated string",
            "破损", "格式错误", "版本漂移", "哈希不匹配",
        )
        if any(marker.casefold() in text for marker in project_state_markers):
            return False

        stale_lock_markers = (
            "timeout waiting for lock", "lock acquisition exceeded",
            "锁文件已存在", "supervisor lock",
        )
        return any(marker in text for marker in stale_lock_markers)

    def try_heal(self, exc: Exception, command: str | None = None) -> tuple[bool, str]:
        """对已通过白名单的锁故障清理一次；其他异常保持现场。"""
        if not self.can_heal(exc, command=command):
            return False, "异常不在运行时安全修复白名单中；项目状态未修改"

        self._retried = True
        healer = DeterministicHealer(self.root)
        return healer.heal_stale_locks()

