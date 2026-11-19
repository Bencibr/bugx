"""功能完备性基准评测（F-022：六维度①功能完备性的评分维度）。

BugX 的差异化核心是审计 AI 生成代码的功能完备性——登录页缺注册/找回密码/
验证码/错误提示/loading 这类「只做字面最小集」缺陷。本模块给 agent_eval
补上缺失的评分域：对照**功能基准**（feature baseline）评审计运行对植入
功能缺失的检出能力。

协议：
- feature_baseline 声明被审产品的功能清单。每项 ``planted_gap``:
  true = 基准植入的功能缺失（审计应发现它，等价 bug case）；
  false = 已实现功能（报告其缺失 = 误报，等价负样本）。
- 审计 findings 通过既有 ``case_id`` 字段引用功能 id——与 bug case
  共用同一匹配协议，审计端无需新增字段。

评分（仅当调用方提供 feature_baseline 时计入 quality_score）：
- completeness_recall：植入缺失中被发现的占比（核心维度）
- completeness_precision：指向功能的 finding 中指向真实植入缺失的占比
- completeness_by_domain：按功能域分桶 recall（复刻 recall_by_root_cause）
- 权重重分配：recall 0.30 / precision 0.20 / evidence 0.15 / repro 0.10 /
  completeness_recall 0.25。无 baseline 时保持旧权重，行为不变。
"""

from __future__ import annotations

MAX_FEATURES = 512
DOMAIN_MAX_LEN = 64
FEATURE_ID_MAX_LEN = 128
WEIGHT_MIN, WEIGHT_MAX = 0.0, 10.0
# 带 feature baseline 时的质量分权重（六维度①为差异化核心，占比最高增量）
COMPLETENESS_WEIGHTED = {
    "recall": 0.30,
    "precision": 0.20,
    "evidence_validity": 0.15,
    "reproducibility": 0.10,
    "completeness_recall": 0.25,
}


class FeatureBaselineError(ValueError):
    """feature baseline 协议错误（与 agent_eval.EvalError 同级 fail-closed）。"""


def validate_feature_baseline(payload: object) -> list[dict]:
    """校验 feature baseline 并返回规范化 features 列表；违规即抛错。"""
    if not isinstance(payload, dict):
        raise FeatureBaselineError("feature_baseline 必须是对象")
    if payload.get("schema_version") != 1:
        raise FeatureBaselineError("feature_baseline.schema_version 必须为 1")
    target = payload.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("name"), str) \
            or not target["name"].strip():
        raise FeatureBaselineError("feature_baseline.target.name 必须是非空字符串")
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise FeatureBaselineError("feature_baseline.features 必须是非空数组")
    if len(features) > MAX_FEATURES:
        raise FeatureBaselineError(
            f"feature_baseline.features 超过上限 {MAX_FEATURES}")
    seen: set[str] = set()
    planted_count = 0
    for index, feature in enumerate(features, 1):
        if not isinstance(feature, dict):
            raise FeatureBaselineError(f"feature #{index} 必须是对象")
        feature_id = feature.get("id")
        if not isinstance(feature_id, str) or not feature_id.strip():
            raise FeatureBaselineError(f"feature #{index} 缺 id")
        if len(feature_id) > FEATURE_ID_MAX_LEN:
            raise FeatureBaselineError(
                f"feature #{index} id 超过 {FEATURE_ID_MAX_LEN} 字符")
        if feature_id in seen:
            raise FeatureBaselineError(f"feature id 重复: {feature_id}")
        seen.add(feature_id)
        if not isinstance(feature.get("name"), str) or not feature["name"].strip():
            raise FeatureBaselineError(f"{feature_id} 缺 name")
        domain = feature.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            raise FeatureBaselineError(f"{feature_id} 缺 domain")
        if len(domain) > DOMAIN_MAX_LEN:
            raise FeatureBaselineError(
                f"{feature_id} domain 超过 {DOMAIN_MAX_LEN} 字符")
        weight = feature.get("weight", 1.0)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                or not WEIGHT_MIN < float(weight) <= WEIGHT_MAX:
            raise FeatureBaselineError(
                f"{feature_id} weight 必须在 ({WEIGHT_MIN}, {WEIGHT_MAX}]")
        planted = feature.get("planted_gap")
        if not isinstance(planted, bool):
            raise FeatureBaselineError(f"{feature_id} 缺 planted_gap（bool）")
        if planted:
            planted_count += 1
            if not isinstance(feature.get("gap_description"), str) \
                    or not feature["gap_description"].strip():
                raise FeatureBaselineError(f"{feature_id} 植入缺失必须给 gap_description")
        hints = feature.get("detection_hints", [])
        if hints is not None and (not isinstance(hints, list)
                                  or not all(isinstance(h, str) for h in hints)):
            raise FeatureBaselineError(f"{feature_id} detection_hints 必须是字符串数组")
    if planted_count == 0:
        raise FeatureBaselineError(
            "feature_baseline 必须至少包含一个 planted_gap=true 的植入缺失")
    return features


def completeness_metrics(features: list[dict], findings: list[dict]) -> dict:
    """按功能基准对 findings 计算 completeness 评分（纯计算，无 IO）。"""
    gap_ids = {f["id"] for f in features if f["planted_gap"]}
    implemented_ids = {f["id"] for f in features if not f["planted_gap"]}
    feature_ids = gap_ids | implemented_ids

    found_gap_ids: set[str] = set()
    gap_finding_count = 0        # 指向植入缺失的 finding 数
    false_gap_count = 0          # 把已实现功能报成缺失的 finding 数（误报）
    for finding in findings:
        case_id = finding.get("case_id")
        if case_id in gap_ids:
            found_gap_ids.add(case_id)
            gap_finding_count += 1
        elif case_id in implemented_ids:
            false_gap_count += 1

    planted_total = len(gap_ids)
    recall = len(found_gap_ids) / planted_total if planted_total else 0.0
    feature_finding_total = gap_finding_count + false_gap_count
    precision = gap_finding_count / feature_finding_total if feature_finding_total else 0.0

    # 按功能域分桶 recall（对齐 recall_by_root_cause 的口径）
    domain_buckets: dict[str, dict[str, int]] = {}
    for feature in features:
        if not feature["planted_gap"]:
            continue
        bucket = domain_buckets.setdefault(
            str(feature["domain"]), {"gaps": 0, "found": 0})
        bucket["gaps"] += 1
        if feature["id"] in found_gap_ids:
            bucket["found"] += 1
    by_domain = {
        domain: round(bucket["found"] / bucket["gaps"], 4)
        for domain, bucket in sorted(domain_buckets.items())
        if bucket["gaps"]
    }
    return {
        "features_total": len(features),
        "planted_gaps_total": planted_total,
        "planted_gaps_found": len(found_gap_ids),
        "missed_gap_ids": sorted(gap_ids - found_gap_ids),
        "false_gap_findings": false_gap_count,
        "completeness_recall": round(recall, 4),
        "completeness_precision": round(precision, 4),
        "completeness_by_domain": by_domain,
    }
