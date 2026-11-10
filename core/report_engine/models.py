"""BugX 报告引擎核心数据模型 (Report Engine Data Models)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 报告承认的严重度词表（大写）。 Supervisor 侧用小写，这里统一大写呈现。
_KNOWN_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"})
#: 严重度 → 处置 SLA（小时）。只用于呈现，不参与门禁判定。
_SLA_BY_SEVERITY = {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}


@dataclass
class ProjectConfig:
    """被测项目的审计参数与多端同步配置。"""
    audit_depth: str = "standard"  # quick, standard, deep
    worker_concurrency: int = 4    # 1 ~ 12
    kill_rate_gate: float = 0.90   # 0.80 ~ 1.00
    fail_closed: bool = True       # 发现暴击/阻断漏洞时强阻断 CI
    local_watcher: bool = True     # 开启本地目录变更监听
    cloud_sync: bool = True        # 开启团队飞轮云端同步
    cloud_endpoint: str = "https://cloud.bugx.io/api/v1"
    auto_heal: bool = True         # 仅为下游 CI 记录的偏好；本模块不合成补丁
    exclude_paths: List[str] = field(default_factory=lambda: ["node_modules", ".git", "venv", "dist", "build"])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProjectWorkspaceInfo:
    """受管项目的核心档案与工作空间信息。"""
    id: str
    name: str
    path: str
    branch: str
    languages: List[str]
    surfaces: int
    kill_rate: float
    rating: str
    status: str  # SYNCED, PENDING_SYNC, LOCAL_ONLY
    findings_count: int
    last_sync: str
    config: ProjectConfig = field(default_factory=ProjectConfig)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["config"] = self.config.to_dict() if isinstance(self.config, ProjectConfig) else self.config
        return d


@dataclass
class VulnerabilityFinding:
    """已裁决并经过 PoC 验证的安全漏洞发现项。"""
    id: str
    title: str
    package_name: str
    ecosystem: str
    category: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    cwe_id: str
    cwe_title: str
    surface_id: str
    target_path: str
    description: str
    exploit_poc: str
    defense_patch: str
    receipt_sha256: str
    # 溯源字段：Supervisor 运行态里的源码级发现通常**没有** CVE/GHSA 编号，
    # 也没有官方 CVSS 打分。历史缺陷是把它们一律填成 ``CVE-2026-XXXX`` 与
    # ``7.5``（``from_eval_dir`` 的默认值），报告上就出现了凭空捏造的 CVE 号。
    # 因此这三个字段可空，渲染层遇到空值必须输出「未分配」而不是编一个。
    cve_id: str = ""
    cvss_score: Optional[float] = None
    cvss_vector: str = ""
    status: str = "VERIFIED_EXPLOITABLE"
    sla_hours: int = 24  # 处置 SLA 期限 (小时)

    @property
    def cve_display(self) -> str:
        """CVE/GHSA 展示值：没有编号就说没有，不占位伪造。"""
        return self.cve_id.strip() or "无 CVE/GHSA 编号（源码级缺陷）"

    @property
    def cvss_display(self) -> str:
        """CVSS 展示值：未打分为 ``NOT-SCORED``，不得回退成 0.0/7.5。"""
        if self.cvss_score is None:
            return "NOT-SCORED"
        return f"{self.cvss_score:g}"

    @property
    def cvss_vector_display(self) -> str:
        return self.cvss_vector.strip() or "未提供 CVSS 向量"

    @property
    def poc_display(self) -> str:
        return self.exploit_poc.strip() or "未提供 PoC 载荷"

    @property
    def patch_display(self) -> str:
        return self.defense_patch.strip() or "未提供修复建议（仅报告缺陷）"

    @classmethod
    def from_supervisor(
        cls, finding: Dict[str, Any], *, project_name: str = "",
    ) -> "VulnerabilityFinding":
        """把 Supervisor 运行态的一条发现映射成报告条目。

        只搬运磁盘上真实存在的字段：``id``/``title``/``severity``/
        ``root_cause``/``poc_cmd``/``evidence``/``verification``。缺失的
        CVE/CVSS/补丁一律留空，由渲染层如实呈现。
        """
        severity = str(finding.get("severity") or "info").strip().upper()
        evidence = finding.get("evidence") or ()
        first_path = ""
        line_no = 0
        if isinstance(evidence, (list, tuple)):
            for item in evidence:
                if isinstance(item, dict) and item.get("path"):
                    first_path = str(item["path"])
                    try:
                        line_no = int(item.get("line") or 0)
                    except (TypeError, ValueError):
                        line_no = 0
                    break
        target = first_path or str(finding.get("fingerprint") or "")
        if first_path and line_no > 0:
            target = f"{first_path}:{line_no}"

        verification = finding.get("verification")
        receipt = ""
        if isinstance(verification, dict):
            receipt = str(verification.get("finding_content_sha256") or "")

        confirmed = isinstance(finding.get("confirmation"), dict) or isinstance(
            finding.get("verification_confirmed"), dict,
        )
        poc = str(finding.get("poc_cmd") or finding.get("reproduction") or "")

        # CWE/CVSS 是 Supervisor 侧真实字段；缺失就留空，绝不补默认值。
        cwe_id = str(finding.get("cwe") or "").strip()
        cwe_title = ""
        if cwe_id:
            # 懒导入：sast_bridge 反向依赖 models，模块级导入会成环。
            from .sast_bridge import _CWE_TITLES
            cwe_title = _CWE_TITLES.get(cwe_id, "")

        cvss_score: Optional[float] = None
        raw_cvss = finding.get("cvss")
        if raw_cvss is not None:
            try:
                cvss_score = float(raw_cvss)
            except (TypeError, ValueError):
                cvss_score = None

        # remediation 是结构体：只有 ``proposal`` 是修复建议；整块 repr 上报告
        # 会把 blast_radius/decision 等治理字段当成"补丁"展示。
        remediation = finding.get("remediation")
        proposal = ""
        if isinstance(remediation, dict):
            proposal = str(remediation.get("proposal") or "")
        elif isinstance(remediation, str):
            proposal = remediation

        return cls(
            id=str(finding.get("id") or finding.get("candidate_key") or "FINDING"),
            title=str(finding.get("title") or "未命名缺陷"),
            package_name=project_name or "project",
            ecosystem="source",
            category="源码级逻辑缺陷",
            severity=severity if severity in _KNOWN_SEVERITIES else "INFO",
            cwe_id=cwe_id,
            cwe_title=cwe_title,
            surface_id=str(finding.get("candidate_key") or finding.get("fingerprint") or ""),
            target_path=target,
            description=str(finding.get("root_cause") or ""),
            exploit_poc=poc,
            defense_patch=proposal,
            receipt_sha256=receipt,
            cve_id="",
            cvss_score=cvss_score,
            cvss_vector="",
            status="CONFIRMED" if confirmed else "CANDIDATE",
            sla_hours=_SLA_BY_SEVERITY.get(severity, 720),
        )


@dataclass
class CategoryMetric:
    """单一漏洞类别 / 靶标的审计度量。"""
    category: str
    target_name: str
    repo: str
    cve_id: str
    surfaces_scouted: int
    probes_executed: int
    mutations_tested: int
    mutations_killed: int
    kill_rate: float
    findings_count: int
    false_positives_eliminated: int
    rating: str
    receipt_sha256: str


@dataclass
class LanguageAuditSummary:
    """单一编程语言维度的全类别汇总审计度量。"""
    language: str
    categories_count: int
    total_surfaces: int
    total_probes: int
    total_mutations: int
    total_killed: int
    kill_rate: float
    rating: str
    zero_false_positives: bool
    negative_control_rate: float
    categories: List[CategoryMetric] = field(default_factory=list)


@dataclass
class AuditMetric:
    """全局聚合审计度量指标。"""
    total_targets: int
    total_surfaces: int
    total_probes: int
    total_mutations: int
    total_killed: int
    overall_kill_rate: float
    false_positive_rate: float
    negative_control_pass_rate: float
    rating: str
    total_findings: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int


@dataclass
class ReportDataBundle:
    """双轨制报告数据总线 (Unified Report Data Bundle)。

    ``authority`` 是**溯源块**：记录本次报告的发现究竟来自哪里（Supervisor
    运行态 / 引擎自测收据）、门禁是否阻断、快照 revision。渲染层只据此决定
    能否出具「官方会签」，不再各自硬编码 ``PASS (COMPLIANT)`` 合规话术。
    """
    report_id: str
    title: str
    created_at: str
    auditor: str
    assessment_scope: str
    metrics: AuditMetric
    languages: List[LanguageAuditSummary] = field(default_factory=list)
    findings: List[VulnerabilityFinding] = field(default_factory=list)
    compliance_notes: List[str] = field(default_factory=list)
    remediation_roadmap: List[Dict[str, Any]] = field(default_factory=list)
    sha256_seal: str = ""
    authority: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ReportDataBundle:
        """从字典还原数据对象。"""
        metrics_raw = data.get("metrics", {})
        metrics = AuditMetric(
            total_targets=metrics_raw.get("total_targets", 0),
            total_surfaces=metrics_raw.get("total_surfaces", 0),
            total_probes=metrics_raw.get("total_probes", 0),
            total_mutations=metrics_raw.get("total_mutations", 0),
            total_killed=metrics_raw.get("total_killed", 0),
            overall_kill_rate=metrics_raw.get("overall_kill_rate", 0.0),
            false_positive_rate=metrics_raw.get("false_positive_rate", 0.0),
            negative_control_pass_rate=metrics_raw.get("negative_control_pass_rate", 1.0),
            rating=metrics_raw.get("rating", "ELITE"),
            total_findings=metrics_raw.get("total_findings", 0),
            critical_count=metrics_raw.get("critical_count", 0),
            high_count=metrics_raw.get("high_count", 0),
            medium_count=metrics_raw.get("medium_count", 0),
            low_count=metrics_raw.get("low_count", 0),
        )

        languages: List[LanguageAuditSummary] = []
        for lang_raw in data.get("languages", []):
            cats: List[CategoryMetric] = []
            for c in lang_raw.get("categories", []):
                cats.append(CategoryMetric(**c))
            lang_dict = dict(lang_raw)
            lang_dict["categories"] = cats
            languages.append(LanguageAuditSummary(**lang_dict))

        findings: List[VulnerabilityFinding] = []
        for f in data.get("findings", []):
            findings.append(VulnerabilityFinding(**f))

        return cls(
            report_id=data.get("report_id", "BUGX-AUDIT-REPORT"),
            title=data.get("title", "BugX Security Audit Report"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            auditor=data.get("auditor", "BugX Autonomous Security Agent"),
            assessment_scope=data.get("assessment_scope", "Multi-Language OSS Penetration Audit"),
            metrics=metrics,
            languages=languages,
            findings=findings,
            compliance_notes=data.get("compliance_notes", []),
            remediation_roadmap=data.get("remediation_roadmap", []),
            sha256_seal=data.get("sha256_seal", ""),
        )

    @classmethod
    def from_eval_dir(cls, eval_dir: Path | str) -> ReportDataBundle:
        """从 evals/oss 目录中的结构化审计收据中自动聚合生成数据总线。"""
        eval_path = Path(eval_dir).resolve()
        if not eval_path.exists() and "/evals/" in str(eval_path):
            alt = Path(str(eval_path).replace("/evals/", "/verification/evals/"))
            if alt.exists():
                eval_path = alt
        if not eval_path.exists():
            raise FileNotFoundError(f"Evaluation directory not found: {eval_path}")

        languages: List[LanguageAuditSummary] = []
        total_targets = 0
        total_surfaces = 0
        total_probes = 0
        total_mutations = 0
        total_killed = 0

        # Scan for language-specific audit JSON files
        pattern_files = sorted(eval_path.glob("*_all_categories_audit.json"))
        for audit_file in pattern_files:
            try:
                content = json.loads(audit_file.read_text(encoding="utf-8"))
                lang_name = content.get("language", audit_file.stem.split("_")[0].capitalize())
                agg = content.get("aggregate_metrics", {})
                qg = content.get("quality_gate", {})

                cat_list: List[CategoryMetric] = []
                for target in content.get("targets", []):
                    cat_metric = CategoryMetric(
                        category=target.get("category", "General"),
                        target_name=target.get("name", target.get("id", "target")),
                        repo=target.get("repo", "unknown"),
                        # 缺编号就留空：兜底 ``CVE-2026-XXXX`` 等于凭空捏造 CVE。
                        cve_id=str(target.get("cve_id") or ""),
                        surfaces_scouted=int(target.get("surfaces_scouted", 0)),
                        probes_executed=int(target.get("probes_executed", 0)),
                        mutations_tested=int(target.get("mutations_tested", 0)),
                        mutations_killed=int(target.get("mutations_killed", 0)),
                        kill_rate=float(target.get("kill_rate", 0.0)),
                        findings_count=int(target.get("findings_adjudicated", 0)),
                        false_positives_eliminated=int(target.get("false_positives_eliminated", 0)),
                        # 收据没写评级就留空，不预设 ELITE；无变异样本时下游
                        # 一律按 NOT-EXERCISED 呈现。
                        rating=str(target.get("rating") or ""),
                        receipt_sha256=target.get("receipt_sha256", ""),
                    )
                    cat_list.append(cat_metric)
                    total_targets += 1

                lang_surfaces = int(agg.get("total_surfaces_scouted", sum(c.surfaces_scouted for c in cat_list)))
                lang_probes = int(agg.get("total_probes_executed", sum(c.probes_executed for c in cat_list)))
                lang_mutations = int(agg.get("total_mutations_tested", sum(c.mutations_tested for c in cat_list)))
                lang_killed = int(agg.get("total_mutations_killed", sum(c.mutations_killed for c in cat_list)))
                # 没写聚合击杀率就按真实分子分母算；0 变异 = 0.0，绝不用 0.95 兜底。
                lang_kill_rate = float(agg.get("aggregate_kill_rate", (lang_killed / lang_mutations) if lang_mutations else 0.0))

                total_surfaces += lang_surfaces
                total_probes += lang_probes
                total_mutations += lang_mutations
                total_killed += lang_killed

                lang_summary = LanguageAuditSummary(
                    language=lang_name,
                    categories_count=len(cat_list),
                    total_surfaces=lang_surfaces,
                    total_probes=lang_probes,
                    total_mutations=lang_mutations,
                    total_killed=lang_killed,
                    kill_rate=lang_kill_rate,
                    rating=qg.get("rating", "ELITE"),
                    zero_false_positives=bool(qg.get("zero_false_positives", True)),
                    negative_control_rate=float(qg.get("negative_control_pass_rate", 1.0)),
                    categories=cat_list,
                )
                languages.append(lang_summary)
            except (OSError, ValueError, TypeError, KeyError):
                # 收据损坏的单个语言包跳过，但不吞掉 KeyboardInterrupt/SystemExit。
                continue

        # Scan for GHSA / CVE advisories
        findings: List[VulnerabilityFinding] = []
        ghsa_file = eval_path / "ghsa_advisories.json"
        if ghsa_file.exists():
            try:
                ghsa_data = json.loads(ghsa_file.read_text(encoding="utf-8"))
                for adv in ghsa_data.get("advisories", []):
                    severity = str(adv.get("severity") or "").strip().upper()
                    if severity not in _KNOWN_SEVERITIES:
                        severity = "INFO"
                    # 缺编号/缺打分就留空：历史缺陷用 ``CVE-2026-XXXX`` 与
                    # ``7.5`` 兜底，等于给每条发现伪造一个 CVE 与一个 CVSS。
                    raw_score = adv.get("cvss_score")
                    try:
                        cvss_score = float(raw_score) if raw_score is not None else None
                    except (TypeError, ValueError):
                        cvss_score = None
                    finding = VulnerabilityFinding(
                        id=str(adv.get("ghsa_id") or adv.get("cve_id") or "ADVISORY"),
                        title=str(adv.get("summary") or "Security Advisory"),
                        package_name=str(adv.get("package_name") or "unknown"),
                        ecosystem=str(adv.get("ecosystem") or "unknown"),
                        category=str(adv.get("cwe_title") or "General Security Flaw"),
                        severity=severity,
                        cwe_id=str(adv.get("cwe_id") or ""),
                        cwe_title=str(adv.get("cwe_title") or ""),
                        surface_id=str(adv.get("package_name") or "surface-01"),
                        target_path=str(adv.get("target_path") or ""),
                        description=str(adv.get("description") or ""),
                        exploit_poc=str(adv.get("poc_script") or ""),
                        defense_patch=str(adv.get("patch_diff") or ""),
                        receipt_sha256=str(adv.get("receipt_sha256") or ""),
                        cve_id=str(adv.get("cve_id") or ""),
                        cvss_score=cvss_score,
                        cvss_vector=str(adv.get("cvss_vector") or ""),
                        # 公告导入 ≠ 本轮验证：明确标成 ADVISORY-IMPORTED，
                        # 报告据此拒绝把这些条目算作「已裁决」。
                        status="ADVISORY-IMPORTED",
                        sla_hours=_SLA_BY_SEVERITY.get(severity, 720),
                    )
                    findings.append(finding)
            except (OSError, ValueError, TypeError):
                # 单个收据损坏不应炸掉整轮报告，但也不得静默吞掉全部异常。
                pass

        # Severity distribution
        crit_count = sum(1 for f in findings if f.severity == "CRITICAL")
        high_count = sum(1 for f in findings if f.severity == "HIGH")
        med_count = sum(1 for f in findings if f.severity == "MEDIUM")
        low_count = sum(1 for f in findings if f.severity == "LOW")

        # Global kill rate
        # 无变异样本时击杀率**未测量**：禁止用 0.9534/ELITE 兜底宣传——那会把
        # "没做变异测试"渲染成"业界最高击杀率"（反模式：无依据指标捏造）。
        # 未测量记 0.0 并显式标注 NOT-EXERCISED，由模板按零变异分支如实呈现。
        mutation_exercised = total_mutations > 0
        overall_kill_rate = (total_killed / total_mutations) if mutation_exercised else 0.0

        metrics = AuditMetric(
            total_targets=total_targets,
            total_surfaces=total_surfaces,
            total_probes=total_probes,
            total_mutations=total_mutations,
            total_killed=total_killed,
            overall_kill_rate=overall_kill_rate,
            false_positive_rate=0.0,
            negative_control_pass_rate=1.0,
            rating=(
                ("ELITE" if overall_kill_rate >= 0.90 else "STRONG")
                if mutation_exercised else "NOT-EXERCISED"
            ),
            total_findings=len(findings),
            critical_count=crit_count,
            high_count=high_count,
            medium_count=med_count,
            low_count=low_count,
        )

        # 合规背书与整改路线只陈述**实际做过**的验证：无变异样本时不得宣称
        # "变异拦截合规/100% 覆盖率"，零发现时不得虚构 CVE 整改行动项
        # （反模式：无依据指标与行动项捏造）。
        compliance_notes = _compliance_notes(
            mutation_exercised=mutation_exercised,
            findings=findings,
        )
        remediation_roadmap = _remediation_roadmap(findings)

        # Calculate SHA256 cryptographic seal
        raw_seal_data = f"{total_surfaces}:{total_killed}:{overall_kill_rate}:{len(findings)}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        sha256_seal = hashlib.sha256(raw_seal_data.encode("utf-8")).hexdigest()

        return cls(
            report_id=f"BUGX-AUDIT-{datetime.now(timezone.utc).strftime('%Y%m%d')}-001",
            title="BugX 企业级全语种控制流安全审计与攻防渗透综合报告",
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            auditor="BugX Autonomous Security Intelligence & Mutation Engine v0.1.0",
            # 范围陈述必须来自收据实况：语言数/靶标数/验证深度，不硬编码
            # "7 大语言 42 类别"之类的宣传文案（实测单语言收据也照抄全文）。
            assessment_scope=(
                f"{len(languages)} 个生态语言 / {total_targets} 个靶标控制流审计"
                f"（验证深度：{'对抗性变异突防' if mutation_exercised else 'triage 执行探针'}）"
            ),
            metrics=metrics,
            languages=languages,
            findings=findings,
            compliance_notes=compliance_notes,
            remediation_roadmap=remediation_roadmap,
            sha256_seal=sha256_seal,
            authority={
                "source": "engine-eval-receipts",
                "eval_dir": str(eval_path),
                "gate_blockers": [],
            },
        )

    @classmethod
    def from_project(
        cls,
        root: Path | str,
        *,
        eval_dir: Path | str | None = None,
    ) -> ReportDataBundle:
        """从**项目运行态**（Supervisor 权威状态）聚合报告数据总线。

        这是报告的正确入口：``from_eval_dir`` 只覆盖「引擎自测收据」，而真实
        项目的缺陷与确认结论都在 ``.bugx/.../state/supervisor.json``。两者可以
        合并：发现以 Supervisor 为准（它有 confirmation 与 evidence），
        变异/探针指标仍来自收据（Supervisor 不测变异）。

        无 Supervisor 权威状态时**不伪造**：返回的 bundle 带
        ``authority["gate_blockers"]``，渲染层据此拒绝出具会签报告。
        """
        from . import authority as authority_mod

        project_root = Path(root).expanduser()
        try:
            project_root = project_root.resolve()
        except OSError:
            pass
        blockers: List[str] = []
        supervisor_findings: List[VulnerabilityFinding] = []
        authority_info: Dict[str, Any] = {
            "source": "none",
            "project_root": str(project_root),
            "gate_blockers": blockers,
        }

        try:
            auth = authority_mod.load(project_root)
        except authority_mod.AuthorityUnavailable as exc:
            blockers.append(f"无 Supervisor 权威状态: {exc}")
        else:
            authority_info.update({
                "source": "supervisor",
                "verification_executed": auth.verification_executed,
                "supervisor_path": str(auth.supervisor_path),
                "run_id": auth.run_id,
                "status": auth.status,
                "revision": auth.revision,
                "polling_id": auth.polling_id,
                "lifecycle_id": auth.lifecycle_id,
                "confirmation_gate": dict(auth.confirmation_gate),
                "coverage": dict(auth.coverage),
            })
            blockers.extend(auth.gate_blockers)
            project_name = project_root.name
            supervisor_findings = [
                VulnerabilityFinding.from_supervisor(item, project_name=project_name)
                for item in auth.findings
            ]

        # 指标侧：若给了收据目录则沿用收据；否则按 Supervisor 实况构造
        # 最小可信指标（变异次数 0 → 评级 NOT-EXERCISED，绝不写 ELITE）。
        if eval_dir is not None:
            base = cls.from_eval_dir(eval_dir)
            base.authority = dict(authority_info)
            if supervisor_findings:
                base.findings = supervisor_findings
                base.metrics = _metrics_with_findings(base.metrics, base.findings)
                base.compliance_notes = _compliance_notes(
                    mutation_exercised=base.metrics.total_mutations > 0,
                    findings=base.findings,
                )
                base.remediation_roadmap = _remediation_roadmap(base.findings)
            return base

        findings = supervisor_findings
        # 误报率与负对照放行率必须**由账本推导**，不能写常量：
        # 全部发现都 CONFIRMED 时 FP=0 是真结论，一条都没验证时它只是「没测」。
        # 历史缺陷是这里硬编码 ``false_positive_rate=0.0`` /
        # ``negative_control_pass_rate=1.0``，于是零验证的项目也会被判
        # ``PASS (COMPLIANT)`` 并渲染出「0.00% / 100.00%」两个凭空的实测值。
        verified = int(authority_info.get("verification_executed") or 0)
        total = len(findings)
        confirmed = sum(1 for f in findings if f.status in ("CONFIRMED", "VERIFIED_EXPLOITABLE"))
        fp_rate = round((total - confirmed) / total, 6) if (total and verified >= total) else 0.0
        nc_rate = round(verified / total, 6) if total else 0.0
        metrics = _metrics_with_findings(
            AuditMetric(
                total_targets=0,
                total_surfaces=0,
                total_probes=0,
                total_mutations=0,
                total_killed=0,
                overall_kill_rate=0.0,
                false_positive_rate=fp_rate,
                negative_control_pass_rate=nc_rate,
                rating="NOT-EXERCISED",
                total_findings=0,
                critical_count=0,
                high_count=0,
                medium_count=0,
                low_count=0,
            ),
            findings,
        )
        scope = (
            f"项目 {project_root.name} 源码级缺陷处置审计"
            if findings else f"项目 {project_root.name} 未产出任何已确认缺陷"
        )
        seal = hashlib.sha256(
            "|".join(
                [
                    project_root.name,
                    authority_info.get("run_id", ""),
                    str(authority_info.get("revision", 0)),
                    ",".join(sorted(f.id for f in findings)),
                ]
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            report_id=(
                f"BUGX-AUDIT-{datetime.now(timezone.utc).strftime('%Y%m%d')}-"
                f"{project_root.name.upper()[:6]}"
            ),
            title=f"BugX 源码级安全审计报告 · {project_root.name}",
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            auditor="BugX Autonomous Security Intelligence & Mutation Engine v0.1.0",
            assessment_scope=f"{scope}（验证深度：白盒裁判与执行探针，未执行变异突防）",
            metrics=metrics,
            languages=[],
            findings=findings,
            compliance_notes=_compliance_notes(
                mutation_exercised=False, findings=findings,
            ),
            remediation_roadmap=_remediation_roadmap(findings),
            sha256_seal=seal,
            authority=authority_info,
        )


def _metrics_with_findings(
    metrics: AuditMetric, findings: List[VulnerabilityFinding],
) -> AuditMetric:
    """把发现分布写进指标副本（严重度计数与发现清单必须同源）。"""
    return AuditMetric(
        total_targets=metrics.total_targets,
        total_surfaces=metrics.total_surfaces,
        total_probes=metrics.total_probes,
        total_mutations=metrics.total_mutations,
        total_killed=metrics.total_killed,
        overall_kill_rate=metrics.overall_kill_rate,
        false_positive_rate=metrics.false_positive_rate,
        negative_control_pass_rate=metrics.negative_control_pass_rate,
        rating=metrics.rating,
        total_findings=len(findings),
        critical_count=sum(1 for f in findings if f.severity == "CRITICAL"),
        high_count=sum(1 for f in findings if f.severity == "HIGH"),
        medium_count=sum(1 for f in findings if f.severity == "MEDIUM"),
        low_count=sum(1 for f in findings if f.severity in {"LOW", "INFO"}),
    )


def _compliance_notes(
    *, mutation_exercised: bool, findings: List[VulnerabilityFinding],
) -> List[str]:
    """合规陈述只覆盖**本轮真做过**的事。

    历史缺陷：零发现时仍写「零假阳性：本轮无任何已报告脆弱性，无需对抗裁决」
    还配上 ✅——把「没查出东西」包装成「通过了对抗裁决」。
    """
    confirmed = [f for f in findings if f.status == "CONFIRMED"]
    if mutation_exercised:
        notes = [
            "ISO/IEC 27001:2022 A.8.28 (Secure Coding) 自动化对抗与代码变异拦截验证合规",
            "NIST SP 800-218 SSDF v1.1 PW.7 / PW.8 自动化代码缺陷与恶意突防消除合规",
        ]
        if findings:
            notes.append(
                f"本轮 {len(confirmed)}/{len(findings)} 条发现具备执行型验证收据"
                "（双正例 + 固定负对照），其余为未确认候选"
            )
        else:
            notes.append("本轮未产出任何已确认缺陷；零发现不等于零风险，覆盖范围见审计范围章节")
        return notes
    notes = [
        "本轮验证深度为 triage（执行探针 + 白盒通读）：未执行对抗性变异攻击，"
        "变异拦截类合规声明不适用",
    ]
    if findings:
        notes.append(
            f"共 {len(findings)} 条发现，其中 {len(confirmed)} 条已通过确认门禁；"
            "未确认项不得对外表述为已验证漏洞"
        )
    else:
        notes.append(
            "本轮未产出任何已确认缺陷，因此不作出「零假阳性裁决通过」类声明"
            "——没有裁决对象就不存在通过"
        )
    return notes


def _remediation_roadmap(
    findings: List[VulnerabilityFinding],
) -> List[Dict[str, Any]]:
    """整改路线由真实发现推导，不套用与项目无关的 GHSA/依赖模板。"""
    if not findings:
        return [
            {
                "phase": "Continuous Integration",
                "action": "保持既有安全门禁；按计划提升验证深度（triage → standard/deep 变异突防）",
                "scope": "本轮覆盖的全部内部最小单元",
                "priority": "P2 - Standard",
            },
        ]
    groups: List[tuple[str, str, List[str], str]] = [
        (
            "Immediate SLA (<24h)",
            "修复暴击/高危源码级缺陷并补充回归用例",
            [f.id for f in findings if f.severity == "CRITICAL"],
            "P0 - Blocker",
        ),
        (
            "Short-term SLA (<7d)",
            "修复高危缺陷并复核同类代码路径",
            [f.id for f in findings if f.severity == "HIGH"],
            "P1 - Critical",
        ),
        (
            "Next Iteration",
            "修复中低危缺陷，纳入常规迭代",
            [f.id for f in findings if f.severity in {"MEDIUM", "LOW", "INFO"}],
            "P2 - Standard",
        ),
    ]
    roadmap: List[Dict[str, Any]] = []
    for phase, action, ids, priority in groups:
        if not ids:
            continue
        shown = ", ".join(ids[:8]) + (" …" if len(ids) > 8 else "")
        roadmap.append({
            "phase": phase,
            "action": action,
            "scope": shown,
            "priority": priority,
        })
    roadmap.append({
        "phase": "Continuous Integration",
        "action": "在 CI/CD 流水线中挂载 `.github/workflows/bugx-security-gate.yml` 强阻断守卫",
        "scope": "全生命周期代码合并与发版门禁",
        "priority": "P3 - Standard",
    })
    return roadmap
