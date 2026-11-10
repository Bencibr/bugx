#!/usr/bin/env python3
"""core/domain_router.py - 领域专精多智能体三维正交路由与配方解析器 (DOC-248)。

负责：
  - 微秒级根据目标单元路径、代码特征、项目全息档案 (Classification) 计算三维装配配方：
    1. 核心行为角色 (worker_role) 与亲和性 (domain_affinity) 及模型等级 (model_tier)
    2. 目标形态 (target_form) 与黑盒动态探针清单 (active_probes)
    3. 技术栈专精领域 (tech_stack) 与底层测试夹具清单 (assigned_fixtures)
  - 避免 400+ 笛卡尔积 Agent 爆炸，实行运行时 JIT 即时拼装装配
  - 落实 AUDIT-01：跨领域复合漏洞升阶检测 (escalate_cross_domain)
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


class DomainSpecializedRouter:
    """DOC-248: 领域专精路由器，在本地内存 <0.1ms 完成确定性分派。"""

    # 1. 核心行为角色与模型等级权威映射
    ROLE_AFFINITY_TIER_MAP: dict[str, tuple[str, str]] = {
        "recon": ("Recon-Worker", "FAST"),
        "auth": ("Auth-Worker", "BALANCED"),
        "logic": ("Logic-Worker", "PRO"),
        "exploit": ("Exploit-Worker", "PRO"),
        "patch": ("Patch-Worker", "BALANCED"),
    }

    @classmethod
    def classify_unit_affinity(
        cls,
        file_path: str,
        symbol_name: str = "",
        ast_features: Mapping[str, Any] | None = None,
        auth_required: bool = False,
        preferred_affinity: str | None = None,
    ) -> tuple[str, str, str]:
        """返回 (worker_role, affinity, model_tier)。计算开销 <0.05ms。"""
        if preferred_affinity and preferred_affinity.lower() in cls.ROLE_AFFINITY_TIER_MAP:
            aff = preferred_affinity.lower()
            role, tier = cls.ROLE_AFFINITY_TIER_MAP[aff]
            return role, aff, tier

        feats = ast_features or {}
        lowered = f"{file_path} {symbol_name}".lower()

        # 1. 优先匹配并发、状态机与核心高维金融/业务逻辑特征
        if any(kw in lowered for kw in (
            "balance", "wallet", "stock", "inventory", "order", "pay",
            "payment", "transfer", "lock", "concurrency", "race", "mutex",
        )):
            return "Logic-Worker", "logic", "PRO"
        if feats.get("has_concurrency") or feats.get("has_state_machine") or feats.get("concurrency"):
            return "Logic-Worker", "logic", "PRO"

        # 2. 匹配身份认证、租户权限与越权特征
        if auth_required or any(kw in lowered for kw in (
            "auth", "user", "tenant", "login", "role", "permission",
            "privilege", "token", "session", "credential", "jwt",
        )):
            return "Auth-Worker", "auth", "BALANCED"

        # 3. 匹配深度注入与漏洞利用特征
        if feats.get("has_taint_sink") or feats.get("taint_flow") or any(kw in lowered for kw in (
            "query", "exec", "download", "load", "render", "sink", "eval",
            "cmd", "shell", "serialize", "deserialize", "sql", "inject",
        )):
            return "Exploit-Worker", "exploit", "PRO"

        # 4. 匹配补丁修复与回归验证特征
        if any(kw in lowered for kw in ("patch", "verify", "regression", "fix", "repair")):
            return "Patch-Worker", "patch", "BALANCED"

        # 5. 默认侦察与轻量特征
        return "Recon-Worker", "recon", "FAST"

    @classmethod
    def _clean_path(cls, file_path: str) -> str:
        """从覆盖单元、URI 或复合 ID 中提取纯净文件路径。"""
        p = file_path.strip()
        # 去除前缀（如 unit:, 业务:, 内部:）
        if ":" in p:
            parts = p.split(":")
            # 若包含冒号，取最长看起来像路径的部分
            candidate = parts[-1]
            if "/" in candidate or "." in candidate:
                p = candidate
        # 去除符号分隔符（#func 或 ::method）
        for sep in ("#", "::"):
            if sep in p:
                p = p.split(sep, 1)[0]
        return p.strip()

    @classmethod
    def resolve_target_form_and_probes(
        cls,
        file_path: str,
        project_profile: Mapping[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        """依据项目档案与文件特征判定形态及挂载的黑盒探针。"""
        profile = project_profile or {}
        forms = set(profile.get("forms") or [])
        surfaces = set(profile.get("surfaces") or [])
        frameworks = set(profile.get("frameworks") or [])
        lowered = cls._clean_path(file_path).lower()

        # 1. 优先按文件路径特征精确匹配 (Path-first specificity)
        # A. 微信/支付宝小程序
        if any(kw in lowered for kw in ("miniprogram", ".wxml", ".wxss", "app.json", "cloudfunctions")):
            return "miniprogram", ("miniprogram_probe",)

        # B. 移动端 (Android vs iOS)
        if any(kw in lowered for kw in ("android", "apk", "manifest.xml", ".kt", "gradle")):
            return "mobile_android", ("android_probe",)
        if any(kw in lowered for kw in ("ios", ".swift", "info.plist", "macho", "xcode")):
            return "mobile_ios", ("ios_probe",)

        # C. Web 动态端点与前端页面
        if any(kw in lowered for kw in ("web", "front", "html", "page", "route", "view")):
            return "web", ("web_dynamic_probe",)

        # D. 嵌入式 / 二进制 / CLI
        if any(ext in lowered for kw in ("binary", "embedded") for ext in (".c", ".cpp", ".cc", ".rs", ".h")) or any(ext in lowered for ext in (".c", ".cpp", ".cc", ".rs", ".h")):
            return "binary", ("binary_probe",)

        # 2. 文件路径未命中具体形态时，回退至项目级 Profile 特征
        if "miniprogram" in frameworks:
            return "miniprogram", ("miniprogram_probe",)
        if "android" in frameworks or "mobile" in forms:
            return "mobile_android", ("android_probe",)
        if "ios" in frameworks:
            return "mobile_ios", ("ios_probe",)
        if "web" in forms or "ui" in surfaces:
            return "web", ("web_dynamic_probe",)
        if "embedded" in forms:
            return "binary", ("binary_probe",)

        # 3. 默认后端纯逻辑
        return "backend", ()

    @classmethod
    def resolve_tech_stack(
        cls,
        file_path: str,
        project_profile: Mapping[str, Any] | None = None,
    ) -> str:
        """判定技术栈专精知识切片归属。"""
        lowered = cls._clean_path(file_path).lower()
        if lowered.endswith((".java", ".jar", ".class", ".jsp")):
            return "java"
        if lowered.endswith((".cs", ".csproj", ".sln", ".cshtml")):
            return "csharp"
        if lowered.endswith((".kt", ".kts")):
            return "kotlin"
        if lowered.endswith((".swift", ".m", ".h")):
            return "swift"
        if lowered.endswith((".go",)):
            return "go"
        if lowered.endswith((".py",)):
            return "python"
        if lowered.endswith((".c", ".cpp", ".cc", ".cxx")):
            return "cpp"
        if lowered.endswith((".rs",)):
            return "rust"
        if lowered.endswith((".js", ".jsx", ".ts", ".tsx")):
            return "javascript"

        # 回退至 profile 声明语言
        profile = project_profile or {}
        languages = profile.get("languages") or []
        for lang in languages:
            norm = str(lang).lower()
            if norm in ("java", "csharp", "c#", "kotlin", "swift", "go", "python", "cpp", "c++", "rust", "javascript", "typescript"):
                return "csharp" if norm == "c#" else ("cpp" if norm == "c++" else norm)
        return "generic"

    @classmethod
    def resolve_worker_spec(
        cls,
        file_path: str,
        symbol_name: str = "",
        project_profile: Mapping[str, Any] | None = None,
        ast_features: Mapping[str, Any] | None = None,
        auth_required: bool = False,
        preferred_affinity: str | None = None,
    ) -> dict[str, Any]:
        """计算三维正交装配完整规格配方字典。

        Returns:
            dict包含:
              - worker_role: str
              - domain_affinity: str
              - model_tier: str
              - target_form: str
              - tech_stack: str
              - active_probes: tuple[str, ...]
              - assigned_fixtures: tuple[str, ...]
        """
        role, affinity, tier = cls.classify_unit_affinity(
            file_path=file_path,
            symbol_name=symbol_name,
            ast_features=ast_features,
            auth_required=auth_required,
            preferred_affinity=preferred_affinity,
        )
        target_form, probes = cls.resolve_target_form_and_probes(
            file_path=file_path,
            project_profile=project_profile,
        )
        tech_stack = cls.resolve_tech_stack(
            file_path=file_path,
            project_profile=project_profile,
        )

        fixtures: list[str] = []
        if affinity == "auth":
            fixtures.append("dual_identity_token_matrix")
        elif affinity == "logic":
            fixtures.append("concurrency_threadpool_10")
        elif affinity == "exploit":
            fixtures.append("twin_sandbox_diff_oracle")
        elif affinity == "patch":
            fixtures.append("cwe_verify_py_scaffold")

        return {
            "worker_role": role,
            "domain_affinity": affinity,
            "model_tier": tier,
            "target_form": target_form,
            "tech_stack": tech_stack,
            "active_probes": probes,
            "assigned_fixtures": tuple(fixtures),
        }

    @classmethod
    def escalate_cross_domain(
        cls,
        indicator: Mapping[str, Any],
        current_role: str,
    ) -> dict[str, Any]:
        """落实 AUDIT-01：跨领域复合漏洞升阶机制。

        当一个 Worker (如 Auth-Worker) 发现超出自身主要范围的高危特征时，
        自动生成升阶配方，交由 Exploit-Worker 综合利用接力。
        """
        detected_aff = str(indicator.get("detected_affinity") or "").lower()
        sink = str(indicator.get("sink") or "")
        reason = str(indicator.get("reason") or "跨领域高危复合利用特征")

        # 默认向具备综合 PoC 验证能力的 Exploit-Worker 升阶
        target_role, target_tier = cls.ROLE_AFFINITY_TIER_MAP.get(
            detected_aff, ("Exploit-Worker", "PRO")
        )
        target_aff = detected_aff if detected_aff in cls.ROLE_AFFINITY_TIER_MAP else "exploit"

        return {
            "escalated": True,
            "escalated_from_role": current_role,
            "escalated_to_role": target_role,
            "domain_affinity": target_aff,
            "model_tier": target_tier,
            "sink": sink,
            "reason": reason,
            "compound_hypothesis": True,
        }
