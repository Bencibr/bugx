"""SAST 扫描结果 → 报告发现条目的唯一映射（SAST to Report Bridge）。

``bugx audit`` 的报告条目只能来自**被测代码的真实扫描结果**。历史缺陷是命令
里硬编码两条「演示漏洞」（``mock_findings_spec``）并伪造 ``CVE-2026-91xx``
编号、``src/gateway.py`` 路径与 ``9.8`` 分数，于是对完全干净的项目也会输出
「2 项已验证漏洞，含 1 严重」。

本模块承担那一层映射：``core.probes.sast_probe`` 的规则命中
（``rule_id``/``severity``/``cwe``/``file``/``line``/``message``/``remediation``）
被翻译成 ``VulnerabilityFinding``，**不补任何字段**：

* 没有 CVE/GHSA 编号 → ``cve_id`` 留空（渲染层输出「无 CVE/GHSA 编号」）；
* 没有官方 CVSS 打分 → ``cvss_score`` 保持 ``None``（渲染层输出 ``NOT-SCORED``）；
* 没有实测 PoC → ``exploit_poc`` 留空，只搬运规则自带的证据行；
* 状态一律 ``SAST-REPORTED``：静态命中不等于已验证可利用，报告不得据此会签。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from .models import VulnerabilityFinding

#: 静态规则命中 ≠ 引擎裁决。attestation 不把它算作已确认发现。
SAST_STATUS = "SAST-REPORTED"

#: SAST 严重度（小写）→ 报告词表（大写）。未知值降级为 ``INFO``。
_SEVERITY_MAP: Dict[str, str] = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
    "info": "INFO",
}

#: 常见 CWE 的标题。缺失时保留空标题，渲染层输出「未映射 CWE」而不是编号占位。
_CWE_TITLES: Dict[str, str] = {
    "CWE-22": "Improper Limitation of a Pathname to a Restricted Directory",
    "CWE-78": "Improper Neutralization of Special Elements used in an OS Command",
    "CWE-79": "Improper Neutralization of Input During Web Page Generation",
    "CWE-89": "Improper Neutralization of Special Elements used in an SQL Command",
    "CWE-95": "Improper Neutralization of Directives in Dynamically Evaluated Code",
    "CWE-327": "Use of a Broken or Risky Cryptographic Algorithm",
    "CWE-377": "Insecure Temporary File",
    "CWE-502": "Deserialization of Untrusted Data",
    "CWE-798": "Use of Hard-coded Credentials",
    "CWE-1336": "Improper Neutralization of Special Elements Used in a Template Engine",
}

#: 严重度 → 处置 SLA（小时）。只用于呈现，不参与门禁判定。
_SLA_HOURS = {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720, "INFO": 720}


def _severity(raw: Any) -> str:
    return _SEVERITY_MAP.get(str(raw or "").strip().lower(), "INFO")


def _relative(path_value: Any, root: Path) -> str:
    """把扫描器给出的路径规范成仓库相对 POSIX 形式（跨平台）。"""
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    try:
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return raw
    return Path(raw).as_posix()


def finding_from_sast(
    item: Mapping[str, Any], root: Path, *, index: int = 0,
) -> VulnerabilityFinding:
    """把一条 SAST 命中映射为报告发现条目。

    ``root`` 用于把绝对路径折算成仓库相对路径；只做搬运与规范化，不推断
    任何扫描器没有给出的结论（CVE、CVSS、PoC、可利用性）。
    """
    root = Path(root).resolve()
    rule_id = str(item.get("rule_id") or item.get("category") or "SAST").strip()
    severity = _severity(item.get("severity"))
    cwe_id = str(item.get("cwe") or "").strip().upper()
    rel_path = _relative(item.get("file"), root)
    line_no = item.get("line")
    try:
        line_no = int(line_no) if line_no is not None else 0
    except (TypeError, ValueError):
        line_no = 0
    target = f"{rel_path}:{line_no}" if rel_path and line_no > 0 else rel_path
    title = str(item.get("name") or item.get("message") or rule_id).strip()
    evidence = str(item.get("evidence") or "").strip()
    message = str(item.get("message") or "").strip()
    remediation = str(item.get("remediation") or "").strip()

    return VulnerabilityFinding(
        id=rule_id or f"SAST-{index + 1}",
        title=title,
        package_name=root.name,
        ecosystem="source",
        category=str(item.get("category") or rule_id),
        severity=severity,
        cwe_id=cwe_id,
        cwe_title=_CWE_TITLES.get(cwe_id, ""),
        surface_id=f"{rel_path}:{line_no}" if rel_path else rule_id,
        target_path=rel_path,
        description=message,
        # 规则自带的命中行是**证据**，不是可复现 PoC：两者语义必须分开，
        # 否则报告会把一行源码当成「已验证的恶意载荷」。
        exploit_poc=evidence,
        defense_patch=remediation,
        receipt_sha256="",
        status=SAST_STATUS,
        sla_hours=_SLA_HOURS.get(severity, 720),
    )
