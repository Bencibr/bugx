#!/usr/bin/env python3
"""用可执行证据、Oracle 和反事实复验把候选问题升级为确认 Bug。"""

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
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Mapping

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# DES-02：原先这里顶层 ``import agent_eval``，而 agent_eval 又要（延迟）
# 导入本模块的 ``reproducible_contract_violation`` / ``is_confirmed`` —— 一对
# 顶层模块互为上下游。本模块对 agent_eval 的需求**只出现在 ``gate()`` 与
# ``main()`` 两个入口**，因此把导入下沉到函数内：导入期不再成环，运行期
# 语义完全不变（模块导入有缓存，延迟导入不产生额外开销）。

SCHEMA_VERSION = 1
CONFIRMATION_STATES = (
    "candidate", "observed", "reproduced", "root_caused", "confirmed", "rejected",
)
ORACLE_TIERS = {
    "requirement": 1,
    "contract": 2,
    "test": 3,
    "invariant": 4,
    "differential": 5,
    "heuristic": 6,
}
OBSERVATION_OUTCOMES = {"failure", "pass", "blocked"}

#: 空字符串的 SHA-256：用于判定观测维度是否有实际内容（审计 A1）。
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ORACLE_PROVENANCE_KINDS = {
    "user-approved", "repository-contract", "external-spec",
    "independent-implementation", "runtime-invariant", "agent-generated",
}
REJECTION_CLASSES = {
    "expected_behavior", "environment", "tool_failure", "duplicate",
    "insufficient_evidence", "flaky", "not_reproducible",
}

# These weights are a quality signal only.  They never replace the boolean
# confirmation gate below; a finding must still satisfy every hard check.
ASSURANCE_WEIGHTS = {
    "oracle": 20,
    "repeat": 20,
    "independent_channels": 15,
    "negative_control": 15,
    "root_cause": 10,
    "reproduction": 10,
    "target_execution": 10,
    # 工件绑定（F1' 新代际口径）：对带 adjudicator_build 标记的新代际
    # confirmed，failure 观测的工件哈希是硬门（缺失即在结算路径回退
    # candidate）；存量（无标记）confirmed 维持"缺省不罚"豁免不受追溯。
    # 加分权重本身只作用于 assurance 画像，不改变硬门判定。
    "observation_artifacts": 10,
}


class ConfirmationError(ValueError):
    """Finding 确认协议错误。"""


REMEDIATION_DECISIONS = {"fixed", "deferred"}


def validate_remediation(finding: dict, root: Path | None = None) -> dict:
    """Validate the non-blocking remediation contract for confirmed Findings."""
    remediation = finding.get("remediation")
    if not isinstance(remediation, dict):
        return {"status": "fix_gap", "errors": ["缺 remediation 块"]}
    decision = remediation.get("decision")
    if decision not in REMEDIATION_DECISIONS:
        return {"status": "fix_gap", "errors": ["remediation.decision 必须是 fixed 或 deferred"]}
    if decision == "fixed":
        fix_ref = remediation.get("fix_ref")
        valid_ref = (
            _nonempty(fix_ref)
            or (
                isinstance(fix_ref, dict)
                and any(_nonempty(fix_ref.get(key)) for key in ("commit_ref", "test_ref"))
            )
        )
        verified = remediation.get("verification_passed") is True
        errors = []
        if not valid_ref:
            errors.append("fixed remediation 缺有效 fix_ref（commit/test 证据）")
        if not verified:
            errors.append("fixed remediation 必须 verification_passed=true")
        return {"status": "ok" if not errors else "fix_gap", "errors": errors}

    errors: list[str] = []
    for field in ("proposal", "verification_plan"):
        if not _nonempty(remediation.get(field)):
            errors.append(f"deferred remediation 缺 {field}")
    blast_radius = remediation.get("blast_radius")
    if not isinstance(blast_radius, list) or not blast_radius or any(
        not _nonempty(item) for item in blast_radius
    ):
        errors.append("deferred remediation.blast_radius 必须是非空路径数组")
    elif root is not None:
        resolved_root = Path(root).resolve()
        for raw_path in blast_radius:
            try:
                raw = str(raw_path).replace("\\", "/")
                candidate = Path(raw)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("路径越出项目根")
                matches = list(resolved_root.glob(raw))
                if not matches or any(
                    resolved_root not in item.resolve().parents
                    and item.resolve() != resolved_root
                    for item in matches
                ):
                    raise ValueError("路径不存在或越出项目根")
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"blast_radius 无效: {raw_path} ({exc})")
    blocking_deps = remediation.get("blocking_deps")
    if blocking_deps is not None and (
        not isinstance(blocking_deps, list) or any(not _nonempty(item) for item in blocking_deps)
    ):
        errors.append("blocking_deps 必须是字符串数组")
    return {"status": "ok" if not errors else "fix_gap", "errors": errors}


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def reproducible_contract_violation(finding: dict) -> str | None:
    """判定 finding 的 reproducible/reproduction 契约，纯函数、无副作用。

    这是「标为可复现的 finding 必须带非空复现步骤」这条规则的**唯一真源**：
    确认门禁 :func:`confirmation_issues` 与评测协议 ``agent_eval.validate_findings``
    都调用它。此前两处各写一遍，一处改一处漏就会让同一 finding 通过一个门禁却
    卡在另一个，门禁结论互相矛盾。

    契约（与 ``evals`` 的 output_contract 一致）：
    - ``reproducible`` 必须是布尔值，缺失或非布尔即违规；
    - ``reproducible`` 为 ``True`` 时必须提供非空字符串 ``reproduction``；
    - ``reproducible`` 为 ``False`` 时不要求 ``reproduction``。

    只报告「是否成立、为什么不成立」，不抛异常也不收集文案，由调用方各自决定
    上报风格（抛 ``EvalError`` 或追加 issue 字符串）。

    :returns: ``None`` 表示契约成立；否则返回违规原因标识：
        ``"not_bool"`` —— ``reproducible`` 不是布尔值；
        ``"missing_reproduction"`` —— 标为可复现却缺有效 ``reproduction``。
    """
    reproducible = finding.get("reproducible")
    if not isinstance(reproducible, bool):
        return "not_bool"
    if reproducible and not _nonempty(finding.get("reproduction")):
        return "missing_reproduction"
    if reproducible and reproduction_not_executable(finding.get("reproduction")):
        return "reproduction_not_executable"
    return None


#: 一条 reproduction 里允许的最大「自然语言痕迹」数。命令里出现一两个英文单词
#: 是正常的（`--flag explain`），但整句解说必然是缺陷。
_NARRATIVE_MARKERS = (
    " shows ", " records ", " demonstrates ", " confirms ", " indicates ",
    " which ", " while ", " whereas ", " that is ",
)
_SHELL_BINARIES = (
    "python", "python3", "pytest", "node", "npm", "pnpm", "yarn", "bash",
    "sh", "zsh", "curl", "go", "cargo", "make", "git", "ruby", "php",
)


MAX_TOOLS_PER_FINDING = 8
SHA256_RE = __import__("re").compile(r"^[0-9a-fA-F]{64}$")


def reproduction_not_executable(value: object) -> bool:
    """判断 ``reproduction`` 是不是「伪装成命令的解说」。

    实测缺陷（OctoPrintMCP，dsh 宿主）：worker 提交的 ``reproduction`` 是

        python3 probe_files.py records the outbound request with httpx.MockTransport
        (no network); unit-139-delete_file.json shows benign_url='...' vs ...

    也就是「命令 + 一句英文解说」拼在一起。它满足「非空」契约，
    ``reproducible: True`` 一路通过摄入侧校验，但**根本跑不起来**
    （``bash: syntax error near unexpected token '('``）。

    危害不是「不好看」，而是**让不可复现的东西看起来可复现**——正是
    reproducible 契约要防的事，却因为只查非空而漏掉了。

    ## 判据：只审「自称是命令」的串

    关键前提是**先判断这串有没有资格被当作命令**——必须以已知 shell 二进制
    开头。人工复现步骤（``构造 10 条记录，latest() 返回最早 5 条``、
    ``mentions('a@b.c') 返回 [...]``）天然含括号和自然语言，它们是**合法的
    描述**，不该被命令规则审。

    早期版本漏了这条前提，直接对任何含 ``()`` 的串跑 ``bash -n``，结果把
    golden 里的两条中文/表达式描述误判成不可执行，连带把 agent_eval 的
    confirmation_rate 从 1.0 打到 0.6667（实测回归）。所以顺序必须是：

        不是命令开头 → 直接放行（人工描述是合法形态）
        是命令开头   → 才检查是否混入了解说

    刻意**不**执行该命令：复现步骤可能有副作用（起服务、写文件、打网络）。
    静态判定足以拦下这个缺陷，且不引入新风险。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False

    words = text.split()
    first = words[0] if words else ""
    looks_like_command = any(
        first == binary or first.endswith("/" + binary) or first.startswith(binary + " ")
        for binary in _SHELL_BINARIES
    )
    if not looks_like_command:
        # 人工复现描述：合法形态，不按命令规则审。
        return False

    lowered = f" {text.lower()} "
    if any(marker in lowered for marker in _NARRATIVE_MARKERS):
        return True

    if any(token in text for token in (";", "&&", "||", "|", "(", ")", "`")):
        import subprocess
        try:
            result = subprocess.run(
                ["bash", "-n", "-c", text], capture_output=True, text=True,
                timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return True

    # 以命令开头但长到像句子：命令 + 解释的混合写法。
    if len(words) > 12 and any(marker in lowered for marker in
                               (" the ", " a ", " and then ")):
        return True
    return False


def _artifact_pair(item: dict, label: str, finding_id: object) -> None:
    """可选工件绑定：artifact_path 与 artifact_sha256 必须成对且格式合法。

    绑定后「两个通道各失败一次」「负对照通过」不再是纯自报——报告期会用
    :func:`verify_observation_artifacts` 重算哈希，伪造就现形。
    """
    path_value = item.get("artifact_path")
    hash_value = item.get("artifact_sha256")
    if path_value is None and hash_value is None:
        return
    if path_value is None or hash_value is None:
        raise ConfirmationError(f"{finding_id} {label} 的 artifact_path/artifact_sha256 必须成对提供")
    if not _nonempty(path_value) or not isinstance(path_value, str):
        raise ConfirmationError(
            f"{finding_id} {label}.artifact_path 必须是非空字符串，形如 "
            '"state/host-confirmation/<slug>-a.json"（仓库相对路径）'
        )
    if not isinstance(hash_value, str) or not SHA256_RE.fullmatch(hash_value):
        raise ConfirmationError(
            f"{finding_id} {label}.artifact_sha256 必须是 64 位十六进制 SHA-256，"
            "形如 " + "a" * 8 + "..." + "（用 `shasum -a 256 <file>` 生成），"
            "且必须与 artifact_path 指向的文件实际内容一致"
        )


def validate_tools(finding: dict) -> list[str]:
    """结构化工具归因：可选字段 ``tools``，存在非空时必须是去重字符串数组。

    归因数据是工具经验账本（tool-feedback）的输入源；这里只做协议校验，
    不参与确认门禁——缺归因或空数组（=无归因）不阻止确认，但带错归因会污染经验统计。
    空数组等价于未填（无归因），返回空列表；若存在且非空，则必须满足非空字符串数组规则。
    """
    tools = finding.get("tools")
    if tools is None or tools == []:
        return []
    if not isinstance(tools, list):
        raise ConfirmationError(
            f"{finding.get('id')} tools 必须是字符串数组，"
            '形如 ["bugx_knowledge_route", "bugx_oracle_suggest"]；'
            "未使用工具请省略该字段，不要传 null 或空字符串"
        )
    if len(tools) > MAX_TOOLS_PER_FINDING:
        raise ConfirmationError(f"{finding.get('id')} tools 不得超过 {MAX_TOOLS_PER_FINDING} 个")
    seen: set[str] = set()
    for item in tools:
        if not _nonempty(item):
            raise ConfirmationError(f"{finding.get('id')} tools 含空工具名")
        if item in seen:
            raise ConfirmationError(f"{finding.get('id')} tools 重复: {item}")
        seen.add(item)
    return tools


def _confirmation(finding: dict) -> dict:
    value = finding.get("confirmation")
    return value if isinstance(value, dict) else {}


def _validate_oracle_block(finding: dict, confirmation: dict) -> dict:
    oracle = confirmation.get("oracle")
    if not isinstance(oracle, dict) or oracle.get("tier") not in ORACLE_TIERS:
        raise ConfirmationError(f"{finding.get('id')} 缺有效 confirmation.oracle")
    if not _nonempty(oracle.get("reference")):
        raise ConfirmationError(f"{finding.get('id')} oracle.reference 不能为空")
    provenance = oracle.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") not in ORACLE_PROVENANCE_KINDS:
        raise ConfirmationError(f"{finding.get('id')} oracle.provenance 来源无效")
    for field in ("independent_from_change", "verified"):
        if not isinstance(provenance.get(field), bool):
            raise ConfirmationError(
            f"{finding.get('id')} oracle.provenance.{field} 必须是布尔值，"
            "形如 true（不要写 \"yes\" / 1 / null）；"
            "provenance 用于声明 oracle 独立于本次改动"
        )
    return oracle


def _validate_observations_block(finding: dict, confirmation: dict) -> list:
    observations = confirmation.get("observations")
    if not isinstance(observations, list):
        raise ConfirmationError(
            f"{finding.get('id')} observations 必须是数组（同指纹 >=2 次独立运行），"
            '形如 [{"run_id": "run-a", "channel": "channel-a", "fingerprint": "...", '
            '"reference": "...", "outcome": "failure", "artifact_path": "...", '
            '"artifact_sha256": "..."}]；两个 observation 必须用**不同** channel'
        )
    seen_runs: set[str] = set()
    for index, item in enumerate(observations, 1):
        if not isinstance(item, dict):
            raise ConfirmationError(
            f"{finding.get('id')} observation #{index} 必须是对象，形如 "
            '{"run_id": "...", "channel": "...", "fingerprint": "...", '
            '"reference": "...", "outcome": "failure", '
            '"artifact_path": "...", "artifact_sha256": "..."}；'
            "outcome 只能是 failure / pass / blocked"
        )
        for field in ("run_id", "channel", "fingerprint", "reference"):
            if not _nonempty(item.get(field)):
                raise ConfirmationError(
                    f"{finding.get('id')} observation #{index} 缺 {field}"
                )
        if item["run_id"] in seen_runs:
            raise ConfirmationError(f"{finding.get('id')} observation run_id 重复")
        seen_runs.add(item["run_id"])
        if item.get("outcome") not in OBSERVATION_OUTCOMES:
            raise ConfirmationError(f"{finding.get('id')} observation outcome 无效")
        _artifact_pair(item, f"observation #{index}", finding.get("id"))
    return observations


def _validate_negative_control_block(finding: dict, confirmation: dict) -> None:
    negative = confirmation.get("negative_control")
    if negative is not None and (
        not isinstance(negative, dict)
        or negative.get("outcome") not in OBSERVATION_OUTCOMES
        or not _nonempty(negative.get("reference"))
    ):
        raise ConfirmationError(f"{finding.get('id')} negative_control 无效")
    if isinstance(negative, dict):
        _artifact_pair(negative, "negative_control", finding.get("id"))


def _validate_target_execution_block(finding: dict, confirmation: dict) -> None:
    target = confirmation.get("target_execution")
    if target is None:
        return
    if not isinstance(target, dict):
        raise ConfirmationError(
            f"{finding.get('id')} target_execution 必须是对象，形如 "
            '{"target_platform": "darwin-arm64-cpython-3.14", '
            '"executed_platform": "darwin-arm64-cpython-3.14", '
            '"executed": true, "simulated": false, '
            '"outcome": "failure", "errors": []}；'
            "target_platform 与 executed_platform 必须**逐字相同**"
        )
    for field in (
        "target_platform", "executed_platform", "target_fingerprint", "run_reference",
    ):
        if not _nonempty(target.get(field)):
            raise ConfirmationError(f"{finding.get('id')} target_execution 缺 {field}")
    for field in ("executed", "simulated"):
        if not isinstance(target.get(field), bool):
            raise ConfirmationError(
            f"{finding.get('id')} target_execution.{field} 必须是布尔值，"
            "形如 true / false（不要写 \"yes\" / 1）；"
            "executed=true 表示真的在目标平台跑过，simulated 必须为 false"
        )
    if target.get("outcome") not in OBSERVATION_OUTCOMES:
        raise ConfirmationError(f"{finding.get('id')} target_execution.outcome 无效")
    errors = target.get("errors")
    if not isinstance(errors, list) or any(not isinstance(item, str) or not item.strip() for item in errors):
        raise ConfirmationError(
            f"{finding.get('id')} target_execution.errors 必须是字符串数组"
            "（无错误时传 []，不要传 null）；每个元素须为非空字符串，"
            '形如 ["ModuleNotFoundError: httpx"]'
        )


def validate_confirmation(finding: dict) -> dict:
    """校验 confirmation 协议；``rejected`` 是明确的非 Bug 终态，按档要求。

    分档原因（L1-ADV-D1 修复配套）：驳回不需要 oracle/复现证据，否则"判为不是
    Bug"这条终态会被确认侧的证据契约反噬（`gate()` 记 issue、写入侧无法落盘）。
    确认侧（candidate/observed/reproduced/root_caused/confirmed）仍要求完整证据，
    且附带字段只要出现就必须合法——放宽的只是"驳回是否需要证据"，不是"证据可否乱填"。
    """
    validate_tools(finding)
    confirmation = _confirmation(finding)
    if not confirmation:
        return {}
    state = confirmation.get("state")
    if state not in CONFIRMATION_STATES:
        raise ConfirmationError(f"{finding.get('id')} confirmation.state 无效: {state}")
    if state == "rejected":
        if confirmation.get("rejection_class") not in REJECTION_CLASSES:
            raise ConfirmationError(f"{finding.get('id')} rejected 缺 rejection_class")
        if not _nonempty(confirmation.get("decision_reason")):
            raise ConfirmationError(f"{finding.get('id')} rejected 缺 decision_reason")
        if "oracle" in confirmation:
            _validate_oracle_block(finding, confirmation)
        if "observations" in confirmation:
            _validate_observations_block(finding, confirmation)
        _validate_negative_control_block(finding, confirmation)
        _validate_target_execution_block(finding, confirmation)
        return confirmation
    _validate_oracle_block(finding, confirmation)
    _validate_observations_block(finding, confirmation)
    _validate_negative_control_block(finding, confirmation)
    _validate_target_execution_block(finding, confirmation)
    return confirmation


def verify_observation_artifacts(
    finding: dict, root, *, require_artifacts: bool = False,
) -> list[str]:
    """重算 observation/negative_control 工件哈希；返回错误清单（空即通过）。

    只校验「声明了绑定」的工件；未绑定的旧 finding 不受影响，但会在
    assurance 画像里拿不到 observation_artifacts 加分。

    F1' 新代际口径：``require_artifacts=True``（结算路径对带
    ``adjudicator_build`` 标记的 confirmed 启用）时，failure 观测**未声明**
    工件绑定也视为错误——新代际 confirmed 不允许"缺省不罚"式的无工件
    确认，缺哈希由结算路径回退 candidate 留存；存量 confirmed 不受追溯。
    """
    import hashlib
    from pathlib import Path

    errors: list[str] = []
    confirmation = _confirmation(finding)
    candidates: list[tuple[str, dict]] = [
        (f"observation #{index}", item)
        for index, item in enumerate(confirmation.get("observations", []), 1)
        if isinstance(item, dict)
    ]
    negative = confirmation.get("negative_control")
    if isinstance(negative, dict):
        candidates.append(("negative_control", negative))
    root_path = Path(root).resolve()
    for label, item in candidates:
        artifact_path = item.get("artifact_path")
        if artifact_path is None:
            continue
        target = (root_path / str(artifact_path)).resolve()
        if target != root_path and root_path not in target.parents:
            errors.append(f"{label} artifact_path 逃逸项目根: {artifact_path}")
            continue
        if not target.is_file():
            errors.append(f"{label} artifact_path 不存在: {artifact_path}")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual.lower() != str(item.get("artifact_sha256", "")).lower():
            errors.append(f"{label} artifact_sha256 不匹配: {artifact_path}")
    if require_artifacts:
        for index, item in enumerate(confirmation.get("observations", []), 1):
            if not isinstance(item, dict) or item.get("outcome") != "failure":
                continue
            declared = (
                _nonempty(item.get("artifact_path"))
                and isinstance(item.get("artifact_sha256"), str)
                and bool(SHA256_RE.fullmatch(item["artifact_sha256"]))
            )
            if not declared:
                errors.append(
                    f"observation #{index} 新代际 confirmed 缺工件哈希绑定"
                    "（adjudicator_build 口径，缺失即回退候选）"
                )
    return errors


def confirmation_assurance(finding: dict) -> dict:
    """Return a bounded, explainable confidence profile for one finding.

    The profile is deliberately separate from :func:`is_confirmed`: it helps
    the Supervisor prioritize follow-up work, but it cannot turn a partial
    finding into a confirmed Bug.
    """
    confirmation = _confirmation(finding)
    if not confirmation:
        return {
            "score": 0,
            "level": "candidate",
            "checks": {name: False for name in ASSURANCE_WEIGHTS},
            "missing": ["缺 confirmation 协议"],
        }
    validate_confirmation(finding)
    if confirmation.get("state") == "rejected":
        # 驳回是终态非 Bug 决定：不要求 oracle/复现画像，也不因缺证据扣分。
        return {
            "score": 0,
            "level": "rejected",
            "checks": {name: False for name in ASSURANCE_WEIGHTS},
            "missing": [],
        }
    oracle = confirmation["oracle"]
    provenance = oracle["provenance"]
    failures = [
        item for item in confirmation["observations"]
        if item["outcome"] == "failure"
    ]
    fingerprints = Counter(item["fingerprint"] for item in failures)
    checks = {
        "oracle": (
            oracle["tier"] != "heuristic"
            and provenance["kind"] != "agent-generated"
            and provenance["independent_from_change"] is True
            and provenance["verified"] is True
        ),
        "repeat": max(fingerprints.values(), default=0) >= 2,
        "independent_channels": len({item["channel"] for item in failures}) >= 2,
        "negative_control": (
            isinstance(confirmation.get("negative_control"), dict)
            and confirmation["negative_control"].get("outcome") == "pass"
        ),
        "root_cause": confirmation.get("root_cause_verified") is True,
        "reproduction": (
            # 加分项与门禁同源（reproducible_contract_violation），但这里额外要求
            # reproducible 显式为 True：不可复现的 finding 不该拿复现加分。
            finding.get("reproducible") is True
            and reproducible_contract_violation(finding) is None
        ),
        "target_execution": (
            isinstance(confirmation.get("target_execution"), dict)
            and confirmation["target_execution"].get("executed") is True
            and confirmation["target_execution"].get("simulated") is False
            and confirmation["target_execution"].get("outcome") == "failure"
            and not confirmation["target_execution"].get("errors")
            and confirmation["target_execution"].get("target_platform")
            == confirmation["target_execution"].get("executed_platform")
        ),
        "observation_artifacts": (
            all(
                isinstance(item, dict) and _nonempty(item.get("artifact_path"))
                for item in failures
            )
            and isinstance(confirmation.get("negative_control"), dict)
            and _nonempty(confirmation["negative_control"].get("artifact_path"))
        ),
    }
    score = sum(ASSURANCE_WEIGHTS[name] for name, passed in checks.items() if passed)
    issues = confirmation_issues(finding)
    if not issues and confirmation.get("state") == "confirmed":
        level = "confirmed"
    elif score >= 70:
        level = "strong_candidate"
    elif score >= 35:
        level = "partial"
    else:
        level = "candidate"
    return {
        "score": score,
        "level": level,
        "checks": checks,
        "missing": issues,
    }


def confirmation_issues(finding: dict) -> list[str]:
    confirmation = validate_confirmation(finding)
    if not confirmation:
        return ["缺 confirmation 协议"]
    issues: list[str] = []
    if confirmation.get("state") == "rejected":
        return issues
    if confirmation.get("state") == "confirmed":
        from core.agent import agent_eval

        if str(finding.get("severity") or "").lower() not in agent_eval.SEVERITY_ORDER:
            issues.append("confirmed Finding 缺有效 severity")
    oracle = confirmation["oracle"]
    if oracle["tier"] == "heuristic":
        issues.append("仅有启发式 Oracle，不能确认 Bug")
    provenance = oracle["provenance"]
    if provenance["kind"] == "agent-generated":
        issues.append("Oracle 与被测改动同源，不能独立确认 Bug")
    if provenance["independent_from_change"] is not True:
        issues.append("Oracle 未独立于被测改动")
    if provenance["verified"] is not True:
        issues.append("Oracle 来源尚未核验")
    failures = [
        item for item in confirmation["observations"] if item["outcome"] == "failure"
    ]
    # 复现次数按**独立运行**计（审计 A2）：同一次运行的多维观测不是两次复现。
    reproductions: dict[str, set[str]] = {}
    for item in failures:
        key = str(item.get("fingerprint") or "")
        execution = str(item.get("execution_id") or item.get("run_id") or "")
        reproductions.setdefault(key, set()).add(execution.split("#", 1)[0])
    stable = max((len(runs) for runs in reproductions.values()), default=0)
    if stable < 2:
        issues.append("缺至少两次同指纹失败复现")
    channels = {item["channel"] for item in failures}
    if len(channels) < 2:
        issues.append(
            "缺两个独立证据通道（engine-verify 只从 receipt 的 stdout/stderr "
            "派生第二通道：正例验证命令必须打印非空失败观测，纯静默 "
            "SystemExit 非零只有 exit-code 一个通道，会被本门禁拒绝——"
            "请在正例命令退出前 print 失败标记后重跑 verify_finding）"
        )
    negative = confirmation.get("negative_control")
    if not isinstance(negative, dict) or negative.get("outcome") != "pass":
        issues.append("缺通过的负对照")
    if confirmation.get("root_cause_verified") is not True:
        issues.append("根因尚未验证")
    # 与 agent_eval 共用唯一真源。注意：reproducible 显式为 False 时**不再**要求
    # reproduction——规则是「标为可复现才必须给步骤」；旧实现用 `is not True`
    # 会把 reproducible=False 也判为缺步骤，与 agent_eval 判定相反。
    if reproducible_contract_violation(finding) is not None:
        issues.append("Finding 缺可复现步骤")
    target = confirmation.get("target_execution")
    if not isinstance(target, dict):
        issues.append("缺真实目标执行证明")
    else:
        if target["executed"] is not True:
            issues.append("目标环境未实际执行")
        if target["simulated"] is not False:
            issues.append("模拟执行不能确认目标平台 Bug")
        if target["target_platform"] != target["executed_platform"]:
            issues.append("目标平台与实际执行平台不一致")
        if target["outcome"] != "failure":
            issues.append("目标执行未观察到失败")
        # PE-20261005-003: target_execution.errors 是记录字段不是阻断项——
        # 复现失败天然带报错输出（如沙盒超时型 DoS），"未观察到失败"已由
        # outcome != failure 承担；assurance 画像仍对非空 errors 扣 10 分留信号。
    return issues


def _evidence_paths(finding: dict) -> set[str]:
    """收集 finding 指向的源码路径（file/path/artifact_path/evidence[].path）。"""
    paths: set[str] = set()
    for key in ("file", "path", "artifact_path"):
        value = finding.get(key)
        if _nonempty(value):
            paths.add(str(value).strip().lstrip("./"))
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict) and _nonempty(item.get("path")):
                paths.add(str(item["path"]).strip().lstrip("./"))
            elif isinstance(item, str) and item.strip():
                paths.add(item.strip().lstrip("./"))
    return paths


def _oracle_provenance(finding: dict, reference: str, oracle_hash: object, root) -> dict:
    """引擎侧核算 oracle 来源：两项都是**算出来**的，不是声明出来的。

    - ``verified``：``reference`` 的文件部分指向真实文件，且其 SHA-256 与 recipe
      里已封入 digest 的 ``oracle_hash`` 一致（宿主事后无法改写）；
    - ``independent_from_change``：该文件不在 finding 的证据路径集合内——不拿被测
      缺陷文件本身当判据。
    """
    import hashlib
    from pathlib import Path

    verdict = {"verified": False, "independent_from_change": False, "reason": ""}
    path_text = str(reference or "").strip().split("#", 1)[0].strip()
    if not path_text:
        verdict["reason"] = "oracle.reference 缺文件路径（要求 path[#anchor]）"
        return verdict
    if root is None:
        verdict["reason"] = "缺项目根，无法核算 oracle 文件哈希"
        return verdict
    root_path = Path(root).resolve()
    candidate = (root_path / path_text).resolve()
    if candidate != root_path and root_path not in candidate.parents:
        verdict["reason"] = "oracle.reference 越出项目根"
        return verdict
    if not candidate.is_file():
        verdict["reason"] = "oracle.reference 指向的文件不存在"
        return verdict
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if not isinstance(oracle_hash, str) or actual != oracle_hash.strip().lower():
        verdict["reason"] = "oracle_hash 与 oracle.reference 文件内容不一致"
        return verdict
    verdict["verified"] = True
    relative = candidate.relative_to(root_path).as_posix()
    if relative in _evidence_paths(finding):
        verdict["reason"] = "oracle 与 finding 证据文件重合，无法独立判定"
        return verdict
    verdict["independent_from_change"] = True
    return verdict


def _verification_observations(receipt: dict, fingerprint: str) -> list[dict]:
    """把 receipt 的双次正例重放成失败观测（判据必须能表达失败）。"""
    runs = receipt.get("positive_runs")
    if not isinstance(runs, list) or len(runs) < 2:
        raise ConfirmationError("verification receipt 缺双次正例运行")
    observations: list[dict] = []
    for index, record in enumerate(runs, 1):
        if not isinstance(record, dict):
            raise ConfirmationError(f"verification 正例 #{index} 记录无效")
        oracle = record.get("oracle") if isinstance(record.get("oracle"), dict) else {}
        expected = oracle.get("expected_exit_code")
        if (
            oracle.get("kind") != "exit_code"
            or isinstance(expected, bool)
            or not isinstance(expected, int)
            or expected == 0
        ):
            raise ConfirmationError(
                f"verification 正例 #{index} 判据无法表达失败观测"
                "（要求 exit_code 且 expected_exit_code != 0）"
            )
        if record.get("oracle_matched") is not True:
            raise ConfirmationError(f"verification 正例 #{index} 未观察到失败")
        if record.get("timed_out") or record.get("output_exceeded"):
            raise ConfirmationError(f"verification 正例 #{index} 超时或输出超限，不能作为复现证据")
        execution_id = str(record.get("execution_id") or "").strip()
        if not execution_id:
            raise ConfirmationError(f"verification 正例 #{index} 缺 execution_id")
        argv = record.get("argv") if isinstance(record.get("argv"), list) else []
        reference = " ".join(str(item) for item in argv)[:200] or execution_id
        base = {
            "execution_id": execution_id,
            "reference": reference,
            "outcome": "failure",
            "exit_code": record.get("exit_code"),
            "stdout_sha256": record.get("stdout_sha256"),
            "stderr_sha256": record.get("stderr_sha256"),
            "verification_id": str(receipt.get("verification_id") or ""),
        }
        observations.append({
            **base,
            "run_id": f"{execution_id}#exit-code",
            "channel": "engine-verify:exit-code",
            "fingerprint": f"exit:{record.get('exit_code')}",
        })
        for field, channel, label in (
            ("stderr_sha256", "engine-verify:stderr", "stderr"),
            ("stdout_sha256", "engine-verify:stdout", "stdout"),
        ):
            digest = str(record.get(field) or "").strip()
            if not digest or digest == _EMPTY_SHA256:
                continue
            observations.append({
                **base,
                "run_id": f"{execution_id}#{label}",
                "channel": channel,
                "fingerprint": f"{label}:{digest[:16]}",
            })
    return observations


def _verification_negative_control(receipt: dict) -> dict:
    """负对照只能来自 receipt 里引擎真跑过、判据命中的那条记录。"""
    record = receipt.get("negative_control")
    if not isinstance(record, dict):
        raise ConfirmationError("verification receipt 缺负对照记录")
    if record.get("oracle_matched") is not True:
        raise ConfirmationError("verification 负对照未通过，不能确认 Finding")
    if record.get("timed_out") or record.get("output_exceeded"):
        raise ConfirmationError("verification 负对照超时或输出超限")
    execution_id = str(record.get("execution_id") or "").strip()
    if not execution_id:
        raise ConfirmationError("verification 负对照缺 execution_id")
    return {
        "outcome": "pass",
        "reference": execution_id,
        "exit_code": record.get("exit_code"),
        "stdout_sha256": record.get("stdout_sha256"),
        "verification_id": str(receipt.get("verification_id") or ""),
    }


def build_confirmation_from_verification(
    finding: dict, receipt: object, *, oracle: object,
    root_cause_verified: object, target_platform: object = None, root=None,
) -> dict:
    """用引擎核验 receipt 组装 confirmation 证据块（只重放已记录事实）。

    L1-ADV-D1 根因修复：确认侧原先只写 ``{state, reason, at}``，读取侧却要求
    oracle/observations/negative_control/root_cause_verified/target_execution，
    两个引擎自有契约互斥 ⇒ post 永不收敛。本函数把"证据从哪来"固定为
    **引擎 receipt**：宿主自述不构成证据，receipt 没记录的材料一律不造。
    """
    if not isinstance(receipt, dict) or not receipt:
        raise ConfirmationError(
            "confirmed 需要引擎核验 receipt：先调用 bugx_supervisor_verify_finding"
        )
    if receipt.get("status") != "CONFIRMED":
        raise ConfirmationError(
            f"引擎核验状态为 {receipt.get('status')}，未通过双正例 + 负对照，不能确认 Finding"
        )
    verification_id = str(receipt.get("verification_id") or "").strip()
    if not verification_id:
        raise ConfirmationError("verification receipt 缺 verification_id")
    fingerprint = str(
        finding.get("fingerprint") or finding.get("id") or finding.get("finding_id") or ""
    ).strip()
    if not fingerprint:
        raise ConfirmationError("Finding 缺稳定指纹，无法绑定复现")
    target_fingerprint = str(receipt.get("target_fingerprint") or "").strip()
    if not target_fingerprint:
        raise ConfirmationError("verification receipt 缺 target_fingerprint，无法证明目标环境执行")
    if root_cause_verified is not True:
        raise ConfirmationError("confirmed 需要宿主显式断言 root_cause_verified=true")
    declared = oracle if isinstance(oracle, Mapping) else {}
    tier = declared.get("tier")
    if tier not in ORACLE_TIERS:
        raise ConfirmationError(f"oracle.tier 无效: {tier}（允许 {sorted(ORACLE_TIERS)}）")
    kind = declared.get("provenance_kind", declared.get("kind"))
    if kind not in ORACLE_PROVENANCE_KINDS:
        raise ConfirmationError(f"oracle.provenance_kind 无效: {kind}")
    reference = str(receipt.get("oracle_reference") or "").strip()
    if not reference:
        raise ConfirmationError("verification receipt 缺 oracle_reference")
    claimed_reference = declared.get("reference")
    if claimed_reference is not None and str(claimed_reference).strip() != reference:
        raise ConfirmationError("声明的 oracle.reference 与引擎核验时使用的 reference 不一致")
    provenance = _oracle_provenance(finding, reference, receipt.get("oracle_hash"), root)
    if not provenance["verified"] or not provenance["independent_from_change"]:
        raise ConfirmationError(
            "oracle 来源未通过引擎核算: " + (provenance["reason"] or "未知原因")
        )
    platform = str(target_platform or receipt.get("target_platform") or "source").strip() or "source"
    return {
        "oracle": {
            "tier": tier,
            "reference": reference,
            "hash": receipt.get("oracle_hash"),
            "provenance": {
                "kind": kind,
                "independent_from_change": True,
                "verified": True,
            },
        },
        "observations": _verification_observations(receipt, fingerprint),
        "negative_control": _verification_negative_control(receipt),
        "root_cause_verified": True,
        "target_execution": {
            "target_platform": platform,
            "executed_platform": platform,
            "target_fingerprint": target_fingerprint,
            "run_reference": str(receipt.get("run_id") or verification_id),
            "executed": True,
            "simulated": False,
            "outcome": "failure",
            "errors": [],
        },
        "verification_id": verification_id,
        "verification_digest": receipt.get("verification_digest"),
        "verified_by": "engine:bugx_supervisor_verify_finding",
        "evidence_reproduction": {
            "receipt_path": str(receipt.get("receipt_path") or ""),
            "verification_digest": str(receipt.get("verification_digest") or ""),
            "oracle_reference": reference,
            "oracle_hash": receipt.get("oracle_hash"),
            "recipe_digest": str(receipt.get("recipe_digest") or ""),
            "positive_command": (
                " ".join(str(x) for x in receipt["positive_runs"][0].get("argv", []))
                if isinstance(receipt.get("positive_runs"), list) and receipt["positive_runs"]
                else ""
            ),
        },
    }


def apply_decision(
    finding: dict, decision: str, *, reason: str, now: object = None,
    receipt: object = None, oracle: object = None, root_cause_verified: object = None,
    remediation: object = None, target_platform: object = None,
    rejection_class: object = None, root=None,
) -> dict:
    """唯一的 Finding 裁决写入口：合并证据 → 门禁自检 → 通过才返回新块。

    失败一律抛 :class:`ConfirmationError`，调用方不得落盘——半成品 confirmed
    正是 L1-ADV-D1 的成因（写入侧不校验读取侧门禁）。
    """
    import copy

    if decision not in {"confirmed", "rejected"}:
        raise ConfirmationError("decision 只能是 confirmed 或 rejected")
    if not _nonempty(reason):
        raise ConfirmationError("reason 不能为空")
    existing = _confirmation(finding)
    work = copy.deepcopy(finding)
    confirmation = dict(existing)
    if decision == "confirmed":
        built = build_confirmation_from_verification(
            work, receipt, oracle=oracle, root_cause_verified=root_cause_verified,
            target_platform=target_platform, root=root,
        )
        prior_observations = (
            [item for item in existing["observations"] if isinstance(item, dict)]
            if isinstance(existing.get("observations"), list) else []
        )
        merged_runs = {str(item.get("run_id")) for item in prior_observations}
        prior_negative = existing.get("negative_control")
        if isinstance(prior_negative, dict) and prior_negative != built["negative_control"]:
            # 不同来源的负对照留档，避免重复裁决时丢掉 worker 提交的那份记录。
            confirmation["prior_negative_control"] = prior_negative
        confirmation.update(built)
        confirmation["observations"] = prior_observations + [
            item for item in built["observations"]
            if str(item.get("run_id")) not in merged_runs
        ]
        confirmation["state"] = "confirmed"
    else:
        if rejection_class not in REJECTION_CLASSES:
            raise ConfirmationError(
                f"rejected 必须提供 rejection_class（允许 {sorted(REJECTION_CLASSES)}）"
            )
        confirmation["rejection_class"] = rejection_class
        confirmation["state"] = "rejected"
    confirmation["decision_reason"] = str(reason).strip()
    if now is not None:
        confirmation["decided_at"] = now
    confirmation["decided_by"] = "confirm_finding"
    work["confirmation"] = confirmation
    if decision == "confirmed":
        issues = confirmation_issues(work)
        if issues:
            raise ConfirmationError(
                "确认门禁失败，拒绝写入不可收敛的 confirmed：" + "；".join(issues)
            )
    validate_confirmation(work)
    result: dict = {"confirmation": confirmation, "remediation": None}
    if remediation is not None:
        if not isinstance(remediation, Mapping):
            # 只说「必须是对象」是不可自愈的：worker 交了合理的人类可读建议
            # （实测 OctoPrintMCP：一条完整的 validate/encode 建议字符串），
            # 被告知「要对象」却不知道对象长什么样，只能反复猜。
            # 错误文案必须自带最小可用 schema 和一个反例。
            raise ConfirmationError(
                f"remediation 必须是对象（收到 {type(remediation).__name__}）。"
                "最小合法形态："
                '{"decision": "fixed" | "deferred", '
                '"fix_ref": "<commit sha 或测试路径>"}；'
                "decision=fixed 时 fix_ref 必填，"
                'decision=deferred 时可写 {"decision": "deferred", "reason": "..."}。'
                "自然语言修复建议请写进 finding 的 notes 或 root_cause，不要塞进 remediation。"
            )
        work["remediation"] = copy.deepcopy(dict(remediation))
        verdict = validate_remediation(work, root)
        if verdict.get("status") != "ok":
            raise ConfirmationError(
                "remediation 无效: " + "；".join(verdict.get("errors") or [])
            )
        result["remediation"] = work["remediation"]
    return result


def is_confirmed(finding: dict) -> bool:
    confirmation = _confirmation(finding)
    return confirmation.get("state") == "confirmed" and not confirmation_issues(finding)


def open_findings(findings: object, root: Path | None = None) -> list[dict[str, object]]:
    """Return bugs that still prevent an unattended successful stop.

    ``rejected`` is a terminal non-bug decision.  A confirmed finding is only
    closed after a valid fixed remediation; ``deferred`` remains open by
    design.  This is intentionally independent from the coverage ledger so a
    fully covered but still-broken project cannot be reported as converged.
    """
    if not isinstance(findings, (list, tuple)):
        return [{"id": "unknown", "state": "invalid", "reason": "finding ledger 无效"}]
    result: list[dict[str, object]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            result.append({
                "id": "unknown", "state": "invalid", "reason": "finding entry 无效",
            })
            continue
        finding_id = str(finding.get("id") or finding.get("fingerprint") or "unknown")
        confirmation = _confirmation(finding)
        state = str(confirmation.get("state") or "candidate")
        reason = "confirmation_pending: 未进入 confirmed/rejected 终态"
        if state == "rejected":
            continue
        if state == "confirmed" and is_confirmed(finding):
            remediation = validate_remediation(finding, root)
            if remediation.get("status") == "ok" and (
                isinstance(finding.get("remediation"), dict)
                and finding["remediation"].get("decision") == "fixed"
            ):
                continue
            reason = "confirmed Finding 缺有效 fixed remediation"
        result.append({"id": finding_id, "state": state, "reason": reason})
    return result


def transition(finding: dict, state: str, reason: str | None = None,
               rejection_class: str | None = None) -> dict:
    if state not in CONFIRMATION_STATES:
        raise ConfirmationError(f"目标状态无效: {state}")
    confirmation = validate_confirmation(finding)
    if not confirmation:
        raise ConfirmationError("Finding 尚未配置 confirmation 协议")
    current = confirmation["state"]
    if current in {"confirmed", "rejected"} and state != current:
        raise ConfirmationError(f"终态 {current} 不可直接迁移")
    if state == "confirmed":
        issues = confirmation_issues(finding)
        if issues:
            raise ConfirmationError("确认门禁失败: " + "；".join(issues))
    elif state == "rejected":
        if rejection_class not in REJECTION_CLASSES or not _nonempty(reason):
            raise ConfirmationError("rejected 必须提供 rejection_class 和 reason")
        confirmation["rejection_class"] = rejection_class
        confirmation["decision_reason"] = reason
    else:
        current_index = CONFIRMATION_STATES.index(current)
        target_index = CONFIRMATION_STATES.index(state)
        if target_index < current_index:
            raise ConfirmationError(f"禁止从 {current} 回退到 {state}")
    confirmation["state"] = state
    if reason and state != "rejected":
        confirmation["decision_reason"] = reason
    finding["confirmation"] = confirmation
    return finding


def degraded_gate(error: str) -> dict:
    """Return a stable gate shape when aggregate metrics cannot be trusted."""
    return {
        "schema_version": SCHEMA_VERSION,
        "total": None,
        "confirmed": None,
        "rejected": None,
        "pending": None,
        "ready": False,
        "degraded": True,
        "error": error,
        "states": {},
        "assurance": {
            "average_score": None,
            "minimum_score": None,
            "fully_confirmed_rate": None,
            "blocking_reasons": {},
        },
        "findings": [],
    }


def gate(payload: dict) -> dict:
    """Aggregate the confirmation gate without inventing metrics on failure.

    Payload-level errors leave the finding list enumerable, so each row is
    still classified independently.  Only an unenumerable ``findings`` field
    makes aggregate counts unknown; that path returns ``None`` plus
    ``degraded`` rather than a misleading zero.
    """
    from core.agent import agent_eval  # DES-02：见模块顶部说明（避免导入期成环）

    structural = _gate_structural_error(payload)
    findings_value = payload.get("findings") if isinstance(payload, Mapping) else None
    if structural is not None and not isinstance(findings_value, list):
        return degraded_gate(f"confirmation protocol invalid: {structural}")

    findings = findings_value
    details = []
    counts = Counter()
    row_validation_errors: list[str] = []
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            counts["candidate"] += 1
            details.append({
                "id": f"#{index}",
                "state": "candidate",
                "confirmed": False,
                "issues": [f"finding #{index} 必须是对象"],
                "assurance": {"score": 0, "level": "invalid"},
            })
            continue
        confirmation = _confirmation(finding)
        state = str(confirmation.get("state", "candidate"))
        counts[state] += 1
        issues: list[str] = []
        # Validate one row at a time so malformed data cannot poison the
        # aggregate, while retaining the original agent_eval contract.
        try:
            agent_eval.validate_findings({
                "schema_version": 1,
                "duration_seconds": 0,
                "token_count": 0,
                "findings": [finding],
            })
        except agent_eval.EvalError as exc:  # row-level schema error is pending
            issues.append(str(exc))
            row_validation_errors.append(str(exc))
        except Exception as exc:
            # An unexpected validator failure means the aggregate itself is
            # unknown; do not turn an implementation fault into row counts.
            return degraded_gate(f"confirmation validator unavailable: {exc}")
        try:
            issues.extend(confirmation_issues(finding))
        except ConfirmationError as exc:
            issues.append(str(exc))
        except Exception as exc:
            return degraded_gate(f"confirmation issue evaluation unavailable: {exc}")
        try:
            assurance = confirmation_assurance(finding)
        except ConfirmationError:
            assurance = {"score": 0, "level": "invalid"}
        except Exception as exc:
            return degraded_gate(f"confirmation assurance unavailable: {exc}")
        details.append({
            "id": finding.get("id") if isinstance(finding.get("id"), str)
            and finding.get("id") else f"#{index}",
            "state": state,
            "confirmed": state == "confirmed" and not issues,
            "issues": issues,
            "assurance": assurance,
        })
    confirmed = sum(item["confirmed"] for item in details)
    rejected = counts["rejected"]
    pending = len(details) - confirmed - rejected
    scores = [item["assurance"]["score"] for item in details]
    blocking = Counter(
        issue
        for item in details
        if not item["confirmed"] and item["state"] != "rejected"
        for issue in item["issues"]
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "total": len(details),
        "confirmed": confirmed,
        "rejected": rejected,
        "pending": pending,
        # rejected 是有证据、有分类、有理由的终态，不应阻塞最终报告；只有仍待
        # 判定的 candidate/observed/reproduced/root_caused 或无效 confirmed 阻塞。
        "ready": pending == 0,
        "states": dict(sorted(counts.items())),
        "assurance": {
            "average_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "minimum_score": min(scores) if scores else 0,
            "fully_confirmed_rate": round(confirmed / len(details), 4) if details else 0.0,
            "blocking_reasons": dict(blocking.most_common()),
        },
        "findings": details,
    }
    if structural is not None:
        result["degraded"] = True
        result["error"] = f"confirmation protocol invalid: {structural}"
    elif row_validation_errors:
        # FIX-003 B4（回归修复）：单条数据问题只**逐条**上报，绝不给整门设全局
        # ``error``（BX-ENGINE-18 契约：单条越界不得让整门失败）。聚合仍可计算，
        # 故不置 degraded；机器可见性由 ``findings[].issues``、
        # ``assurance.blocking_reasons`` 与 ``ready`` 提供，另以独立字段保留原始
        # 行级错误，避免旧调用方把畸形行误当干净门禁。
        result["row_validation_errors"] = row_validation_errors[:3]
    return result


def _gate_structural_error(payload: object) -> str | None:
    """只检查**结构性**问题（fail-closed）；单条数据问题由逐条路径处理。"""
    if not isinstance(payload, Mapping):
        return "payload 必须是对象"
    if payload.get("schema_version") != 1:
        return "findings.schema_version 必须为 1"
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return "findings 必须是数组"
    duration = payload.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        return "findings.duration_seconds 必须是非负数"
    token_count = payload.get("token_count")
    if token_count is not None and (
        not isinstance(token_count, int) or isinstance(token_count, bool) or token_count < 0
    ):
        return "findings.token_count 必须是非负整数或 null"
    return None


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    from core.agent import agent_eval  # DES-02：见模块顶部说明（避免导入期成环）
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "gate", "transition"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--state", choices=CONFIRMATION_STATES)
    parser.add_argument("--reason")
    parser.add_argument("--rejection-class", choices=sorted(REJECTION_CLASSES))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = agent_eval.load_json(args.input)
        findings = agent_eval.validate_findings(payload)
        if args.command == "transition":
            if not args.id or not args.state:
                raise ConfirmationError("transition 必须提供 --id 和 --state")
            finding = next((item for item in findings if item["id"] == args.id), None)
            if finding is None:
                raise ConfirmationError(f"Finding 不存在: {args.id}")
            transition(finding, args.state, args.reason, args.rejection_class)
            result = payload
        else:
            for finding in findings:
                validate_confirmation(finding)
            result = gate(payload)
        if args.output:
            _write_atomic(args.output, result)
        if args.json or not args.output:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "gate" and not result["ready"]:
            return 1
        return 0
    except (ConfirmationError, agent_eval.EvalError, OSError, json.JSONDecodeError) as exc:
        print(f"[finding_confirmation] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
