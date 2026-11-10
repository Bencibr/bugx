#!/usr/bin/env python3
"""Finding 报告的读取/校验/原子写入原语。

DES-02：本模块是 ``finding_report`` 与 ``finding_lifecycle`` 之间的**下沉共享层**。

原先 ``finding_report`` 自带 ``validate_run_binding`` / ``load_and_validate`` /
``write_atomic``，而 ``finding_lifecycle`` 为在 CLI 中复用它们而顶层
``import finding_report``；同时 ``finding_report.diff_reports`` 又延迟导入
``finding_lifecycle.reconcile`` —— 两个模块互为上下游，构成 Facade 环。

这三个原语只依赖 ``agent_eval`` 与 ``finding_confirmation``（都在两者之下），
把它们下沉到本模块后，依赖方向变为单向：
``finding_report`` → ``finding_lifecycle`` → ``finding_io``（无环）。

``finding_report`` 仍按原样再导出这三个名字，``finding_report.<name>`` 的
既有调用方（含测试）不受影响。
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.agent import agent_eval
from core.ledger import finding_confirmation

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def validate_run_binding(payload: dict) -> dict | None:
    """校验可选的运行绑定章：把报告钉死在「哪次运行、哪一轮」。

    字段：``run_id``（非空字符串）、``lifecycle_id``（非空字符串）、
    ``round``（正整数）。旧报告中的 ``pre_baseline_hash`` 保持读取兼容并忽略。
    缺省时返回 None；存在但形状无效时抛 EvalError——绑定章不允许半填。
    """
    binding = payload.get("run_binding")
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise agent_eval.EvalError("run_binding 必须是对象")
    for field in ("run_id", "lifecycle_id"):
        value = binding.get(field)
        if not isinstance(value, str) or not value.strip():
            raise agent_eval.EvalError(f"run_binding.{field} 必须是非空字符串")
    round_no = binding.get("round")
    if not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1:
        raise agent_eval.EvalError("run_binding.round 必须是正整数")
    return binding

def load_and_validate(path: Path, root: Path) -> dict:
    payload = agent_eval.load_json(path)
    return validate_payload(payload, root)


def validate_payload(payload: object, root: Path) -> dict:
    """Validate an already-read final Finding payload without reopening it.

    Lifecycle callers that need to hash the exact bytes used for a decision
    parse one bounded file read and pass that object here.  Keep this path in
    lockstep with :func:`load_and_validate` so a decision cannot be validated
    differently depending on its caller.
    """
    if not isinstance(payload, dict):
        raise agent_eval.EvalError("findings 根节点必须是对象")
    findings = agent_eval.validate_findings(payload)
    invalid = agent_eval.invalid_evidence_ids(findings, root)
    if invalid:
        raise agent_eval.EvalError("finding 缺真实仓库内证据: " + ", ".join(invalid))
    binding = validate_run_binding(payload)
    if binding is None:
        raise agent_eval.EvalError("最终 finding 报告必须包含 run_binding")
    if binding["run_id"] != str(payload.get("run_id") or ""):
        raise agent_eval.EvalError("run_binding.run_id 必须与报告 run_id 一致")
    for finding in findings:
        try:
            finding_confirmation.validate_confirmation(finding)
        except finding_confirmation.ConfirmationError as exc:
            raise agent_eval.EvalError(str(exc)) from exc
        artifact_errors = finding_confirmation.verify_observation_artifacts(
            finding, root,
        )
        if artifact_errors:
            raise agent_eval.EvalError(
                f"{finding.get('id')} 观察工件复核失败: " + "; ".join(artifact_errors)
            )
    return payload


def snapshot_relative_path(raw: object, root: Path) -> str:
    """Normalize a relative artifact name and prove lexical root containment.

    This intentionally performs no resolve/stat/open operation. The artifact
    snapshot producer owns no-follow filesystem validation; this pure layer
    checks that report paths select entries from that already-verified
    root-relative snapshot.
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise agent_eval.EvalError("artifact path 必须是非空相对路径")
    value = raw.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise agent_eval.EvalError(f"artifact path 必须位于 root 内: {raw}")
    normalized = posixpath.normpath(value)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise agent_eval.EvalError(f"artifact path 必须位于 root 内: {raw}")
    root_text = os.fspath(root)
    if not isinstance(root_text, str) or not root_text:
        raise agent_eval.EvalError("root 必须是有效路径")
    root_absolute = os.path.abspath(root_text)
    candidate = os.path.abspath(os.path.join(root_absolute, *normalized.split("/")))
    try:
        if os.path.commonpath((root_absolute, candidate)) != root_absolute:
            raise agent_eval.EvalError(f"artifact path 必须位于 root 内: {raw}")
    except ValueError as exc:
        raise agent_eval.EvalError(f"artifact path 必须位于 root 内: {raw}") from exc
    return normalized


def _snapshot_metadata(
    artifact_snapshot: Mapping[str, object], path: str, *,
    require_line_count: bool,
) -> Mapping[str, object]:
    metadata = artifact_snapshot.get(path)
    if not isinstance(metadata, Mapping):
        raise agent_eval.EvalError(f"artifact_snapshot 缺少已验证文件: {path}")
    if metadata.get("path") != path:
        raise agent_eval.EvalError(f"artifact_snapshot 路径元数据不一致: {path}")
    for field in ("device", "inode", "size"):
        value = metadata.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise agent_eval.EvalError(f"artifact_snapshot.{field} 无效: {path}")
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise agent_eval.EvalError(f"artifact_snapshot.sha256 无效: {path}")
    line_count = metadata.get("line_count")
    if require_line_count or line_count is not None:
        if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count < 0:
            raise agent_eval.EvalError(f"artifact_snapshot.line_count 无效: {path}")
    return metadata


def _expected_binding_snapshot(expected_binding: object) -> dict[str, object]:
    required = {"runtime_run_uuid", "lifecycle_id", "round", "polling_id"}
    if not isinstance(expected_binding, Mapping) or set(expected_binding) != required:
        raise agent_eval.EvalError(
            "expected_binding 必须包含 runtime_run_uuid、lifecycle_id、round、polling_id"
        )
    for field in ("runtime_run_uuid", "lifecycle_id"):
        value = expected_binding.get(field)
        if not isinstance(value, str) or not value.strip():
            raise agent_eval.EvalError(f"expected_binding.{field} 必须是非空字符串")
    round_no = expected_binding.get("round")
    if not isinstance(round_no, int) or isinstance(round_no, bool) or round_no < 1:
        raise agent_eval.EvalError("expected_binding.round 必须是正整数")
    polling_id = expected_binding.get("polling_id")
    if polling_id is not None and (
        not isinstance(polling_id, str) or not polling_id.strip()
    ):
        raise agent_eval.EvalError("expected_binding.polling_id 必须是非空字符串或 null")
    return dict(expected_binding)


def _validate_snapshot_binding(
    payload: dict, expected_binding: object, *, layout_policy: str,
) -> None:
    binding = validate_run_binding(payload)
    if binding is None:
        if layout_policy == "formal":
            raise agent_eval.EvalError("正式项目布局的 Finding 必须包含 run_binding")
        if layout_policy == "legacy":
            return
        raise agent_eval.EvalError("layout_policy 必须是 formal 或 legacy")
    expected = _expected_binding_snapshot(expected_binding)
    payload_run_id = payload.get("run_id")
    if payload_run_id != binding["run_id"]:
        raise agent_eval.EvalError("run_binding.run_id 必须与报告 run_id 一致")

    lifecycle_id = str(expected["lifecycle_id"])
    polling_id = expected["polling_id"]
    allowed_run_ids = {str(expected["runtime_run_uuid"])}
    if polling_id is not None:
        allowed_run_ids.update({
            f"{polling_id}-{lifecycle_id}",
            f"lifecycle-{polling_id}-{lifecycle_id}",
        })
    else:
        # The bare Supervisor ID is retained only for the existing no-polling
        # legacy layout. It is never accepted when a polling namespace exists.
        allowed_run_ids.add(f"lifecycle-{lifecycle_id}")
    if binding["run_id"] not in allowed_run_ids:
        raise agent_eval.EvalError("run_binding.run_id 与当前 runtime/polling 身份不一致")
    if binding["lifecycle_id"] != lifecycle_id:
        raise agent_eval.EvalError("run_binding.lifecycle_id 与当前 runtime 不一致")
    if binding["round"] != expected["round"]:
        raise agent_eval.EvalError("run_binding.round 与当前 runtime 不一致")


def validate_payload_snapshot(
    payload: object,
    root: Path,
    expected_binding: Mapping[str, object] | None,
    artifact_snapshot: Mapping[str, object],
    layout_policy: str,
) -> dict:
    """Purely validate a Finding payload against one runtime/artifact snapshot.

    ``artifact_snapshot`` maps normalized root-relative paths to metadata
    objects containing ``path``, ``device``, ``inode``, ``size`` and ``sha256``;
    text evidence entries also require ``line_count``. Confirmation artifacts
    may omit ``line_count``. ``layout_policy`` is explicitly ``formal`` or
    ``legacy``; only legacy may accept a missing run binding. The function does
    not inspect the filesystem: callers must pass artifacts captured by their
    bounded no-follow reader.
    """
    if not isinstance(payload, dict):
        raise agent_eval.EvalError("findings 根节点必须是对象")
    if not isinstance(artifact_snapshot, Mapping):
        raise agent_eval.EvalError("artifact_snapshot 必须是对象")
    if layout_policy not in {"formal", "legacy"}:
        raise agent_eval.EvalError("layout_policy 必须是 formal 或 legacy")
    _validate_snapshot_binding(
        payload, expected_binding, layout_policy=layout_policy,
    )
    findings = agent_eval.validate_findings(payload)

    for finding in findings:
        valid_evidence = False
        for item in finding["evidence"]:
            try:
                path = snapshot_relative_path(item.get("path"), root)
                metadata = _snapshot_metadata(
                    artifact_snapshot, path, require_line_count=True,
                )
                line = item.get("line")
                if line is not None and line > metadata["line_count"]:
                    continue
                valid_evidence = True
                break
            except agent_eval.EvalError:
                continue
        if not valid_evidence:
            raise agent_eval.EvalError(
                f"{finding.get('id')} 缺真实仓库内证据或行号超出快照范围"
            )

        try:
            confirmation = finding_confirmation.validate_confirmation(finding)
        except finding_confirmation.ConfirmationError as exc:
            raise agent_eval.EvalError(str(exc)) from exc
        artifact_refs = [
            item
            for item in confirmation.get("observations", [])
            if isinstance(item, Mapping) and item.get("artifact_path") is not None
        ]
        negative = confirmation.get("negative_control")
        if isinstance(negative, Mapping) and negative.get("artifact_path") is not None:
            artifact_refs.append(negative)
        for artifact in artifact_refs:
            path = snapshot_relative_path(artifact.get("artifact_path"), root)
            metadata = _snapshot_metadata(
                artifact_snapshot, path, require_line_count=False,
            )
            expected_hash = artifact.get("artifact_sha256")
            if not isinstance(expected_hash, str) or (
                metadata["sha256"].lower() != expected_hash.lower()
            ):
                raise agent_eval.EvalError(
                    f"{finding.get('id')} observation artifact_sha256 不匹配: {path}"
                )
    return payload

def write_atomic(path: Path, content: str) -> None:
    if not path.parent.is_dir():
        raise agent_eval.EvalError(f"输出目录不存在: {path.parent}")
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
