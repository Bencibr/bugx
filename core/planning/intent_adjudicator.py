#!/usr/bin/env python3
"""core/intent_adjudicator.py - BugX 业务意图对齐与真伪缺陷裁决门禁 (DOC-245 / DOC-247).

职责:
1. 拿候选缺陷 (Candidate Finding) 与业务真理账本 (business-spec.json) 做意图交叉核验;
2. 【物理红线 AUDIT-01】绝对禁止赦免任何物理不可违背高危漏洞（SQLi/RCE/路径穿越/代码注入/反序列化等）；
3. 识别并拦截“符合设计规约预期”的业务虚假缺陷 (如公开免密端点被误报为未授权访问、正常限流被误报为服务拒绝);
4. 判定结果输出: REJECTED_AS_INTENDED (拒绝立案并记录理由) 或 VALID_CANDIDATE (准予进入双跑差分).
"""

from __future__ import annotations

import re
from typing import Any

# 物理高危不可违背漏洞特征（坚决禁止意图豁免，直接送物理差分）
_PHYSICAL_VULN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:sqli|sql[_\s-]?injection|cwe[_\s-]?89)\b", re.IGNORECASE),
    re.compile(r"\b(?:command[_\s-]?injection|rce|remote[_\s-]?code[_\s-]?execution|cwe[_\s-]?78|os[_\s-]?command)\b", re.IGNORECASE),
    re.compile(r"\b(?:path[_\s-]?traversal|directory[_\s-]?traversal|arbitrary[_\s-]?file[_\s-]?(?:read|write)|cwe[_\s-]?22)\b", re.IGNORECASE),
    re.compile(r"\b(?:code[_\s-]?injection|eval[_\s-]?injection|dynamic[_\s-]?eval|cwe[_\s-]?94)\b", re.IGNORECASE),
    re.compile(r"\b(?:deserialization|unpickl(?:e|ing)|cwe[_\s-]?502)\b", re.IGNORECASE),
    re.compile(r"\b(?:buffer[_\s-]?overflow|memory[_\s-]?corruption|use[_\s-]?after[_\s-]?free|cwe[_\s-]?120)\b", re.IGNORECASE),
    re.compile(r"\b(?:ssrf|server[_\s-]?side[_\s-]?request[_\s-]?forgery|cwe[_\s-]?918)\b", re.IGNORECASE),
    re.compile(r"\b(?:double[_\s-]?spend(?:ing)?|oversell(?:ing)?|negative[_\s-]?balance|race[_\s-]?condition[_\s-]?on[_\s-]?(?:balance|wallet|stock))\b", re.IGNORECASE),
)

# 认证/免密诉求模式
_AUTH_COMPLAINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:unauthorized|unauthenticated|missing[_\s-]?auth|未授权|缺少鉴权|免密|越权访问|anonymous[_\s-]?access)", re.IGNORECASE),
)

# 尺寸/限制诉求模式
_SIZE_COMPLAINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:payload[_\s-]?too[_\s-]?large|size[_\s-]?limit|400[_\s-]?bad[_\s-]?request|超过大小|拒绝处理|file[_\s-]?too[_\s-]?big)", re.IGNORECASE),
)

# 限流/防刷诉求模式
_RATE_COMPLAINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:429[_\s-]?too[_\s-]?many[_\s-]?requests|rate[_\s-]?limit(?:ed)?|throttl(?:ed?|ing)|限流|拒绝服务|ddos|brute[_\s-]?force[_\s-]?denial)", re.IGNORECASE),
)

# 只读写入违规诉求模式
_READONLY_COMPLAINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:405[_\s-]?method[_\s-]?not[_\s-]?allowed|read[_\s-]?only[_\s-]?violation|write[_\s-]?refused|只读|禁止写入)", re.IGNORECASE),
)

# 角色权限违规诉求模式
_ROLE_COMPLAINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:403[_\s-]?forbidden|permission[_\s-]?denied|role[_\s-]?required|权限不足|需要管理员|admin[_\s-]?only)", re.IGNORECASE),
)


def _is_physical_vulnerability(finding: dict[str, Any]) -> bool:
    """检查是否属于物理不可违背高危漏洞（绝不赦免）。"""
    search_space = " ".join((
        str(finding.get("title", "")),
        str(finding.get("description", "")),
        str(finding.get("category", "")),
        str(finding.get("cwe", "")),
        str(finding.get("root_cause", "")),
    ))
    return any(pat.search(search_space) for pat in _PHYSICAL_VULN_PATTERNS)


def adjudicate_finding_against_spec(
    finding: dict[str, Any],
    business_spec: dict[str, Any] | None,
) -> tuple[bool, str]:
    """意图对齐裁决门禁.

    返回: (is_admitted, reason)
    - True: 缺陷合法, 准予立案并进入后续物理差分双跑验证;
    - False: 缺陷与设计规约预期相符, 判定为业务假阳性并强阻断立案。
    """
    if not isinstance(finding, dict):
        return True, "INVALID_FINDING_PAYLOAD"

    # 1. 物理红线校验 (AUDIT-01)：物理不可违背漏洞直接准入差分，绝不走意图豁免！
    if _is_physical_vulnerability(finding):
        return True, "VALID_CANDIDATE"

    # 2. 规约存在性校验
    if not business_spec or not isinstance(business_spec, dict):
        return True, "NO_BUSINESS_SPEC_PASSTHROUGH"

    invariants = business_spec.get("explicit_invariants", [])
    if not isinstance(invariants, list) or not invariants:
        return True, "NO_EXPLICIT_INVARIANTS_PASSTHROUGH"

    finding_title = str(finding.get("title", "")).lower()
    finding_desc = str(finding.get("description", "")).lower()
    target_path = str(finding.get("path") or finding.get("file") or "").lower()
    endpoint = str(finding.get("endpoint", "")).lower()
    combined_text = f"{finding_title} {finding_desc}"

    # 3. 意图争议双轨对齐裁决
    # (A) 匿名访问与公开端点核验 (ALLOW_ANONYMOUS / HEALTHCHECK_ENDPOINT)
    if any(pat.search(combined_text) for pat in _AUTH_COMPLAINT_PATTERNS):
        for inv in invariants:
            inv_dict = inv if isinstance(inv, dict) else (inv.to_dict() if hasattr(inv, "to_dict") else {})
            rule = inv_dict.get("rule")
            if rule in {"ALLOW_ANONYMOUS", "HEALTHCHECK_ENDPOINT"}:
                inv_target = str(inv_dict.get("target", "")).lower()
                source_file = inv_dict.get("source_file", "business-spec.json")
                detail = inv_dict.get("detail", rule)
                if (inv_target != "global" and (inv_target in endpoint or inv_target in target_path)) or \
                   ("health" in endpoint or "ping" in endpoint or "public" in endpoint):
                    return (
                        False,
                        f"REJECTED_AS_INTENDED: 端点 '{inv_target}' 被设计规约 ({source_file}) "
                        f"明确声明为允许匿名或探活端点 ({rule}: {detail})，行为符合业务预期，严禁立案。",
                    )

    # (B) 限制上传尺寸或参数超限 (SIZE_LIMIT)
    if any(pat.search(combined_text) for pat in _SIZE_COMPLAINT_PATTERNS):
        for inv in invariants:
            inv_dict = inv if isinstance(inv, dict) else (inv.to_dict() if hasattr(inv, "to_dict") else {})
            if inv_dict.get("rule") == "SIZE_LIMIT":
                inv_target = str(inv_dict.get("target", "")).lower()
                source_file = inv_dict.get("source_file", "business-spec.json")
                detail = inv_dict.get("detail", "SIZE_LIMIT")
                if inv_target == "global" or inv_target in endpoint or inv_target in target_path:
                    return (
                        False,
                        f"REJECTED_AS_INTENDED: 拒绝行为符合设计规约 ({source_file}) "
                        f"声明的尺寸上限规则 ({detail})，属于正常防御逻辑。",
                    )

    # (C) 正常限流与防刷 (RATE_LIMITED)
    if any(pat.search(combined_text) for pat in _RATE_COMPLAINT_PATTERNS):
        for inv in invariants:
            inv_dict = inv if isinstance(inv, dict) else (inv.to_dict() if hasattr(inv, "to_dict") else {})
            if inv_dict.get("rule") == "RATE_LIMITED":
                inv_target = str(inv_dict.get("target", "")).lower()
                source_file = inv_dict.get("source_file", "business-spec.json")
                detail = inv_dict.get("detail", "RATE_LIMITED")
                if inv_target == "global" or inv_target in endpoint or inv_target in target_path:
                    return (
                        False,
                        f"REJECTED_AS_INTENDED: 接口限流符合设计规约 ({source_file}) "
                        f"声明的速率保护规则 ({detail})，属于正常防御逻辑。",
                    )

    # (D) 只读资源写入拒绝 (READ_ONLY)
    if any(pat.search(combined_text) for pat in _READONLY_COMPLAINT_PATTERNS):
        for inv in invariants:
            inv_dict = inv if isinstance(inv, dict) else (inv.to_dict() if hasattr(inv, "to_dict") else {})
            if inv_dict.get("rule") == "READ_ONLY":
                inv_target = str(inv_dict.get("target", "")).lower()
                source_file = inv_dict.get("source_file", "business-spec.json")
                detail = inv_dict.get("detail", "READ_ONLY")
                if inv_target == "global" or inv_target in endpoint or inv_target in target_path:
                    return (
                        False,
                        f"REJECTED_AS_INTENDED: 拒绝写操作符合设计规约 ({source_file}) "
                        f"声明的只读资源属性 ({detail})，属于正常设计。",
                    )

    # (E) 角色权限访问拒绝 (ROLE_PERMISSION)
    if any(pat.search(combined_text) for pat in _ROLE_COMPLAINT_PATTERNS):
        for inv in invariants:
            inv_dict = inv if isinstance(inv, dict) else (inv.to_dict() if hasattr(inv, "to_dict") else {})
            if inv_dict.get("rule") == "ROLE_PERMISSION":
                inv_target = str(inv_dict.get("target", "")).lower()
                source_file = inv_dict.get("source_file", "business-spec.json")
                detail = inv_dict.get("detail", "ROLE_PERMISSION")
                if inv_target == "global" or inv_target in endpoint or inv_target in target_path:
                    return (
                        False,
                        f"REJECTED_AS_INTENDED: 403 拒绝访问符合设计规约 ({source_file}) "
                        f"声明的角色访问控制权限 ({detail})，非未授权缺陷。",
                    )

    return True, "VALID_CANDIDATE"
