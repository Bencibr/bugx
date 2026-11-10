#!/usr/bin/env python3
"""BugX Cloud Sync V2-only wire schema（DOC-008 §5）。

本模块是客户端唯一的 V2 协议编解码入口：

- 严格 V2-only：schema_version 缺失、为 1 或其他值一律拒绝，无任何 V1/legacy fallback；
- canonical manifest：UTF-8、key 排序、无多余空白、数组保序、整数禁止浮点表示、
  null 与缺字段不可互换（DOC-008 §5.4）；
- 强类型记录校验：patterns / incidents / tool_experience / consumption_events / tombstones 五个数组必须
  同时存在（禁止 omitempty 造成 canonical hash 分叉），未知字段拒绝；
- 错误契约解析（DOC-008 §5.3）：426 携带 required_schema_version/min_client_version。

与后端 Go 实现共享同一组 JSON fixture 和预期 SHA-256
（tests/fixtures/cloud_sync_v2_fixtures.json）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 2
MIN_CLIENT_VERSION = "0.6.1"
SYNC_SCOPES = ("global", "project")
RECORD_TYPES = (
    "patterns", "incidents", "tool_experience", "consumption_events", "tombstones",
)

MANIFEST_HASH_PREFIX = "sha256:"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# Must stay aligned with server/internal/services/cloud_sync_service.go.
# Cursors are signed opaque values; accepting a larger value locally only
# delays a deterministic 400 from the server.
MAX_CURSOR_LENGTH = 512
MAX_SYNC_RECORDS = 500

# DOC-008 §5.3 错误契约：HTTP 状态 -> 默认业务错误码。
ERROR_CODES: dict[int, str] = {
    400: "SYNC_INVALID_ENVELOPE",
    401: "SYNC_UNAUTHENTICATED",
    403: "SYNC_SCOPE_FORBIDDEN",
    409: "SYNC_VERSION_CONFLICT",
    413: "SYNC_PAYLOAD_TOO_LARGE",
    422: "SYNC_MANIFEST_MISMATCH",
    426: "SYNC_PROTOCOL_UPGRADE_REQUIRED",
    429: "SYNC_RATE_LIMITED",
    503: "SYNC_MAINTENANCE",
}
# 是否可重试（409 需先 pull/reconcile，413 需拆页，均不算直接网络重试）。
RETRYABLE_STATUS = frozenset({429, 503})
NETWORK_ERROR_CODE = "SYNC_NETWORK_UNAVAILABLE"
SERVER_ERROR_CODE = "SYNC_SERVER_UNAVAILABLE"


class SchemaError(ValueError):
    """V2 wire 数据不合法（缺字段、未知字段、类型错误、hash 不一致）。"""


class ProtocolUpgradeRequired(SchemaError):
    """服务端/客户端协议版本不受支持（V2-only：缺失、=1 或其他）。"""

    def __init__(self, message: str, *, required_schema_version: int = SCHEMA_VERSION,
                 min_client_version: str = MIN_CLIENT_VERSION) -> None:
        super().__init__(message)
        self.required_schema_version = required_schema_version
        self.min_client_version = min_client_version


@dataclass
class SyncError:
    """结构化同步错误（DOC-008 §5.3）。"""

    status: int | None
    code: str
    message: str = ""
    request_id: str = ""
    retryable: bool = False
    retry_policy: str = "no"
    retry_after_seconds: int | None = None
    required_schema_version: int | None = None
    min_client_version: str | None = None


# ---------------------------------------------------------------------------
# canonical JSON / manifest hash（DOC-008 §5.4）
# ---------------------------------------------------------------------------

def _assert_canonical(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise SchemaError("canonical JSON 禁止浮点表示（整数必须以整数传输）")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError("canonical JSON 对象键必须是字符串")
            _assert_canonical(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_canonical(item)
        return
    raise SchemaError(f"canonical JSON 不支持的值类型: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """确定性 canonical 编码：UTF-8、key 排序、无多余空白、数组保序。"""
    _assert_canonical(value)
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8")


def content_hash(value: Any) -> str:
    """记录级内容 hash（sha256 hex，不带前缀），用于幂等与冲突检测。"""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def manifest_hash(records: dict[str, Any]) -> str:
    """manifest 只覆盖完整 records 对象；返回 ``sha256:<64 lowercase hex>``。"""
    digest = hashlib.sha256(canonical_json_bytes(records)).hexdigest()
    return MANIFEST_HASH_PREFIX + digest


def _require_manifest_hash(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith(MANIFEST_HASH_PREFIX):
        raise SchemaError("manifest_hash 必须是 sha256:<hex> 字符串")
    hex_part = value[len(MANIFEST_HASH_PREFIX):]
    if not _SHA256_HEX_RE.fullmatch(hex_part):
        raise SchemaError("manifest_hash 格式无效")
    return value


# ---------------------------------------------------------------------------
# 标量字段校验原语（null 与缺字段不可互换：missing -> 缺少字段；null -> 类型无效）
# ---------------------------------------------------------------------------

def _has(data: dict[str, Any], key: str) -> bool:
    return key in data


def _str_field(record: dict[str, Any], key: str, *, required: bool = True,
               allow_empty: bool = True) -> str:
    if not _has(record, key):
        if required:
            raise SchemaError(f"{key}: 缺少必填字段")
        return ""
    value = record[key]
    if not isinstance(value, str):
        raise SchemaError(f"{key}: 必须是字符串")
    if not allow_empty and not value:
        raise SchemaError(f"{key}: 不能为空字符串")
    return value


def _int_field(record: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    if not _has(record, key):
        raise SchemaError(f"{key}: 缺少必填字段")
    value = record[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{key}: 必须是整数（禁止浮点/布尔表示）")
    if minimum is not None and value < minimum:
        raise SchemaError(f"{key}: 不得小于 {minimum}")
    return value


def _bool_field(record: dict[str, Any], key: str) -> bool:
    if not _has(record, key):
        raise SchemaError(f"{key}: 缺少必填字段")
    value = record[key]
    if not isinstance(value, bool):
        raise SchemaError(f"{key}: 必须是布尔值")
    return value


def _details_field(record: dict[str, Any], key: str, allowed: tuple[str, ...],
                   label: str, *, scalar_values: bool = False) -> None:
    value = record.get(key, {})
    if not isinstance(value, dict):
        raise SchemaError(f"{label}.{key}: 必须是对象")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise SchemaError(f"{label}.{key} 含未知字段: {', '.join(unknown)}")
    for name, item in value.items():
        if scalar_values:
            if not isinstance(item, str):
                raise SchemaError(f"{label}.{key}.{name}: 必须是字符串")
        elif not isinstance(item, list) or any(not isinstance(v, str) for v in item):
            raise SchemaError(f"{label}.{key}.{name}: 必须是字符串数组")


def _check_exact_keys(record: dict[str, Any], fields: tuple[str, ...], label: str,
                      *, optional: tuple[str, ...] = ()) -> None:
    if not isinstance(record, dict):
        raise SchemaError(f"{label} 记录必须是对象")
    keys = set(record.keys())
    expected = set(fields)
    unknown = sorted(keys - expected)
    missing = sorted(expected - keys - set(optional))
    if unknown:
        raise SchemaError(f"{label} 含未知字段: {', '.join(unknown)}")
    if missing:
        raise SchemaError(f"{label} 缺少字段: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# V2 记录强类型（字段集与 core/cloud_client.py 的 wire 映射一一对应）
# ---------------------------------------------------------------------------

PATTERN_FIELDS = (
    "lesson_id", "category", "title", "root_cause", "reflection",
    "probes", "source_refs", "status", "experience_level",
    "hit_count", "use_count", "miss_count", "false_positive_count",
    "rejected_count",
    "version", "superseded_by", "deleted", "device_id", "details",
    "routing_tags",
    # 可复用漏洞模式卡（Pattern Card）标量字段：全部可选（空串=未采集），
    # 服务端 language 小写 / cwe_id 大写归一；tags 逗号串（与 details.tags 并存）。
    "severity", "language", "cwe_id", "error_signature",
    "before_code", "after_code", "fix_summary", "repro_oracle", "tags",
    "finding_fingerprints",
)

INCIDENT_FIELDS = (
    "incident_id", "lesson_id", "category", "title", "root_cause",
    "summary", "before_code", "after_code", "decision_chain",
    "duration", "status", "version", "deleted", "device_id", "details",
    "routing_tags",
    # 工具轨迹与经验回流（可选）：tools_used 逗号串；tool_attempts 为
    # [{seq,tool,action,outcome,note}] JSON 串（联调方案 §2.3）。
    "tools_used", "tool_attempts", "error_signature", "repro_oracle",
)

# Full private sync keeps the methodology/guardrail fields in one explicit
# extension object.  This avoids making every future knowledge field a wire
# migration while retaining strict V2 top-level keys and canonical hashing.
PATTERN_DETAIL_FIELDS = (
    "playbook", "attempts_tried", "successful_solution", "applicability",
    "false_positive_patterns", "oracle_hints", "tags", "aliases",
    "negative_controls", "evidence_requirements",
    # 无损列表通道：probes/source_refs 的 wire 逗号串对含逗号元素有损，
    # 列表原文放 details 往返（pull 端优先还原）。
    "probes", "source_refs",
)
INCIDENT_DETAIL_FIELDS = ("context",)
ROUTING_TAG_FIELDS = (
    "forms", "languages", "runtimes", "frameworks", "surfaces", "targets", "check_profiles",
)

# DOC-008 §7 Tool Experience V2 强类型字段（只允许聚合数据上云）。
TOOL_EXPERIENCE_FIELDS = (
    "tool_name", "category", "experience_level",
    "use_count", "hit_count", "miss_count",
    "false_positive_count", "rejected_count",
    "independent_lifecycle_count",
    "first_observed_at", "last_observed_at",
    "tool_version", "record_version", "device_id",
    "deleted", "deleted_at",
)

TOMBSTONE_FIELDS = ("record_type", "record_id", "version", "deleted_at", "device_id")
TOMBSTONE_TARGET_TYPES = ("pattern", "incident", "tool_experience")
CONSUMPTION_EVENT_FIELDS = (
    "event_id", "subject_type", "subject_id", "event_type",
    "lifecycle_id", "run_id", "created_at",
)

_RECORD_FIELDS = {
    "patterns": PATTERN_FIELDS,
    "incidents": INCIDENT_FIELDS,
    "tool_experience": TOOL_EXPERIENCE_FIELDS,
    "consumption_events": CONSUMPTION_EVENT_FIELDS,
    "tombstones": TOMBSTONE_FIELDS,
}


def validate_pattern_record(record: Any) -> dict[str, Any]:
    # ``details`` was added inside V2 as the full-mode extension.  Empty
    # details are valid for summary mode and for an empty legacy snapshot;
    # generated full-mode records always carry the complete object.
    _check_exact_keys(record, PATTERN_FIELDS, "pattern", optional=("details", "routing_tags"))
    _str_field(record, "lesson_id", allow_empty=False)
    for key in ("category", "title", "root_cause", "reflection", "probes",
                "source_refs", "status", "experience_level", "superseded_by", "device_id",
                "severity", "language", "cwe_id", "error_signature",
                "before_code", "after_code", "fix_summary", "repro_oracle", "tags",
                "finding_fingerprints"):
        _str_field(record, key)
    for key in ("hit_count", "use_count", "miss_count", "false_positive_count",
                "rejected_count"):
        _int_field(record, key, minimum=0)
    _int_field(record, "version", minimum=0)
    _bool_field(record, "deleted")
    _details_field(record, "details", PATTERN_DETAIL_FIELDS, "pattern")
    _details_field(record, "routing_tags", ROUTING_TAG_FIELDS, "pattern")
    return record


def validate_incident_record(record: Any) -> dict[str, Any]:
    _check_exact_keys(record, INCIDENT_FIELDS, "incident", optional=("details", "routing_tags"))
    _str_field(record, "incident_id", allow_empty=False)
    for key in ("lesson_id", "category", "title", "root_cause", "summary",
                "before_code", "after_code", "decision_chain", "status", "device_id",
                "tools_used", "tool_attempts", "error_signature", "repro_oracle"):
        _str_field(record, key)
    _int_field(record, "duration", minimum=0)
    _int_field(record, "version", minimum=0)
    _bool_field(record, "deleted")
    _details_field(record, "details", INCIDENT_DETAIL_FIELDS, "incident",
                   scalar_values=True)
    _details_field(record, "routing_tags", ROUTING_TAG_FIELDS, "incident")
    return record


def validate_tool_experience_record(record: Any) -> dict[str, Any]:
    _check_exact_keys(record, TOOL_EXPERIENCE_FIELDS, "tool_experience")
    _str_field(record, "tool_name", allow_empty=False)
    for key in ("category", "experience_level", "first_observed_at",
                "last_observed_at", "tool_version", "device_id", "deleted_at"):
        _str_field(record, key)
    for key in ("use_count", "hit_count", "miss_count", "false_positive_count",
                "rejected_count", "independent_lifecycle_count", "record_version"):
        _int_field(record, key, minimum=0)
    _bool_field(record, "deleted")
    return record


def validate_tombstone_record(record: Any) -> dict[str, Any]:
    _check_exact_keys(record, TOMBSTONE_FIELDS, "tombstone")
    target = _str_field(record, "record_type", allow_empty=False)
    if target not in TOMBSTONE_TARGET_TYPES:
        raise SchemaError("tombstone.record_type 不受支持")
    _str_field(record, "record_id", allow_empty=False)
    _int_field(record, "version", minimum=0)
    _str_field(record, "deleted_at")
    _str_field(record, "device_id")
    return record


def validate_consumption_event_record(record: Any) -> dict[str, Any]:
    _check_exact_keys(record, CONSUMPTION_EVENT_FIELDS, "consumption_event")
    for key in CONSUMPTION_EVENT_FIELDS:
        _str_field(record, key, allow_empty=key not in {
            "event_id", "subject_type", "subject_id", "event_type",
        })
    if record["event_type"] not in {"selected", "retrieved", "feedback"}:
        raise SchemaError("consumption_event.event_type 不受支持")
    for key in CONSUMPTION_EVENT_FIELDS:
        if len(record[key]) > 200:
            raise SchemaError(f"consumption_event.{key} 超过长度限制")
    return record


_RECORD_VALIDATORS = {
    "patterns": validate_pattern_record,
    "incidents": validate_incident_record,
    "tool_experience": validate_tool_experience_record,
    "consumption_events": validate_consumption_event_record,
    "tombstones": validate_tombstone_record,
}


def validate_records(records: Any) -> dict[str, list[dict[str, Any]]]:
    """records 五个数组必须全部存在且为对象数组（禁止 omitempty）。"""
    if not isinstance(records, dict):
        raise SchemaError("records 必须是对象")
    unknown = sorted(set(records.keys()) - set(RECORD_TYPES))
    missing = sorted(set(RECORD_TYPES) - set(records.keys()))
    if unknown:
        raise SchemaError(f"records 含未知类别: {', '.join(unknown)}")
    if missing:
        raise SchemaError(f"records 缺少类别: {', '.join(missing)}")
    for kind in RECORD_TYPES:
        items = records[kind]
        if not isinstance(items, list):
            raise SchemaError(f"records.{kind} 必须是数组")
    total = sum(len(records[kind]) for kind in RECORD_TYPES)
    if total > MAX_SYNC_RECORDS:
        raise SchemaError(f"records 总数超过 {MAX_SYNC_RECORDS} 条")
    for kind in RECORD_TYPES:
        items = records[kind]
        for index, item in enumerate(items):
            try:
                _RECORD_VALIDATORS[kind](item)
            except SchemaError as exc:
                raise SchemaError(f"records.{kind}[{index}]: {exc}") from exc
    return records


# ---------------------------------------------------------------------------
# V2 envelope（push 请求 / pull 响应）
# ---------------------------------------------------------------------------

_PUSH_ENVELOPE_FIELDS = (
    "schema_version", "scope", "project_id", "snapshot_id", "operation_id",
    "base_version", "manifest_hash", "device_id", "records", "client_version",
)
# 生产者版本留痕（provenance）：可选字段，旧客户端不发送；服务端据此
# 拒收低于动态下限的推送（不兼容低版本数据在入口丢弃）。
_PUSH_ENVELOPE_OPTIONAL_FIELDS = ("client_version",)

_PULL_ENVELOPE_FIELDS = (
    "schema_version", "scope", "project_id", "snapshot_id", "snapshot_version",
    "manifest_hash", "records", "next_cursor", "has_more",
)

# Additive lifecycle metadata emitted by the deletion/GC protocol.  Older V2
# servers may omit these fields, so the envelope accepts them optionally while
# validating their types strictly when present.
_PULL_METADATA_OPTIONAL_FIELDS = (
    "scope_status", "deletion_seq", "tombstone_floor_seq",
)

# legacy/V1 标记：出现即明确拒绝（AD-008-01，不给静默降级任何空间）。
_LEGACY_KEYS = ("lessons", "incidents", "deleted_at_2")


def _reject_legacy_keys(data: dict[str, Any]) -> None:
    present = [key for key in _LEGACY_KEYS if key in data]
    if present:
        raise SchemaError(
            f"响应包含 legacy/V1 字段({', '.join(present)})，V2-only 协议拒绝"
        )


def _schema_version_strict(data: dict[str, Any]) -> int:
    if "schema_version" not in data:
        raise ProtocolUpgradeRequired(
            "云端响应缺少 schema_version（V1 兼容已关闭，需升级到 "
            f"{MIN_CLIENT_VERSION}）"
        )
    version = data["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProtocolUpgradeRequired("schema_version 必须是整数")
    if version != SCHEMA_VERSION:
        raise ProtocolUpgradeRequired(
            f"云端 schema_version={version} 不受支持（仅支持 2）"
        )
    return version


def build_push_envelope(scope: str, project_id: int | None,
                        records: dict[str, Any], *,
                        snapshot_id: str, operation_id: str,
                        base_version: int, device_id: str,
                        client_version: str = "") -> dict[str, Any]:
    """构造并通过校验的完整 V2 push envelope（所有必填字段恒在）。"""
    if scope not in SYNC_SCOPES:
        raise SchemaError("scope 必须是 global 或 project")
    if scope == "project":
        if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
            raise SchemaError("project scope 必须提供正整数 project_id")
    elif project_id is not None:
        # global scope：省略或显式 null，服务端统一归一为 null（禁用 0 占位）。
        raise SchemaError("global scope 的 project_id 必须省略或为 null")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise SchemaError("snapshot_id 不能为空")
    if not isinstance(operation_id, str) or not operation_id:
        raise SchemaError("operation_id 不能为空")
    if not isinstance(device_id, str) or not device_id:
        raise SchemaError("device_id 不能为空")
    if not isinstance(base_version, int) or isinstance(base_version, bool) or base_version < 0:
        raise SchemaError("base_version 必须是非负整数")
    if not isinstance(client_version, str):
        raise SchemaError("client_version 必须是字符串")
    if len(client_version) > 64:
        raise SchemaError("client_version 过长")
    validate_records(records)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "operation_id": operation_id,
        "base_version": base_version,
        "manifest_hash": manifest_hash(records),
        "device_id": device_id,
        "records": records,
    }
    if client_version:
        envelope["client_version"] = client_version
    validate_push_envelope(envelope)
    return envelope


def validate_push_envelope(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SchemaError("push envelope 必须是对象")
    _schema_version_strict(data)
    # client_version 是可选的生产者版本留痕（provenance），出现时必须是
    # 非空且长度 ≤64 的字符串。
    _check_exact_keys(data, _PUSH_ENVELOPE_FIELDS, "push envelope",
                      optional=_PUSH_ENVELOPE_OPTIONAL_FIELDS)
    scope = data["scope"]
    if scope not in SYNC_SCOPES:
        raise SchemaError("envelope.scope 必须是 global 或 project")
    project_id = data["project_id"]
    if scope == "project":
        if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id <= 0:
            raise SchemaError("project scope 的 project_id 必须是正整数")
    elif project_id is not None:
        raise SchemaError("global scope 的 project_id 必须为 null")
    _str_field(data, "snapshot_id", allow_empty=False)
    _str_field(data, "operation_id", allow_empty=False)
    _int_field(data, "base_version", minimum=0)
    _str_field(data, "device_id", allow_empty=False)
    manifest = _require_manifest_hash(data["manifest_hash"])
    validate_records(data["records"])
    if manifest != manifest_hash(data["records"]):
        raise SchemaError("manifest_hash 校验失败")
    return data


def validate_pull_response(data: Any, *, scope: str, project_id: str = "") -> dict[str, Any]:
    """严格校验 V2 pull 响应并绑定请求的 scope/project（opaque cursor 原样透传）。"""
    if not isinstance(data, dict):
        raise SchemaError("云端响应必须是对象")
    _reject_legacy_keys(data)
    _schema_version_strict(data)
    _check_exact_keys(
        data, _PULL_ENVELOPE_FIELDS + _PULL_METADATA_OPTIONAL_FIELDS,
        "pull envelope", optional=_PULL_METADATA_OPTIONAL_FIELDS,
    )
    if data["scope"] != scope:
        raise SchemaError("云端响应 scope 与请求不一致")
    remote_project = data["project_id"]
    if scope == "project":
        if str(remote_project or "") != str(project_id):
            raise SchemaError("云端响应 project_id 与请求不一致")
    elif remote_project is not None:
        raise SchemaError("global 响应 project_id 必须为 null")
    _str_field(data, "snapshot_id", allow_empty=False)
    _int_field(data, "snapshot_version", minimum=0)
    manifest = _require_manifest_hash(data["manifest_hash"])
    validate_records(data["records"])
    if manifest != manifest_hash(data["records"]):
        raise SchemaError("云端 manifest_hash 校验失败")
    next_cursor = data["next_cursor"]
    if not isinstance(next_cursor, str):
        raise SchemaError("next_cursor 必须是字符串")
    if len(next_cursor) > MAX_CURSOR_LENGTH:
        raise SchemaError("next_cursor 超过长度上限")
    has_more = data["has_more"]
    if not isinstance(has_more, bool):
        raise SchemaError("has_more 必须是布尔值")
    if has_more and not next_cursor:
        raise SchemaError("has_more=true 时 next_cursor 不能为空")
    if "scope_status" in data and data["scope_status"] not in {"active", "deleted"}:
        raise SchemaError("scope_status 必须是 active 或 deleted")
    for key in ("deletion_seq", "tombstone_floor_seq"):
        if key in data:
            _int_field(data, key, minimum=0)
    return data


# ---------------------------------------------------------------------------
# 错误契约解析（DOC-008 §5.3）
# ---------------------------------------------------------------------------

def parse_sync_error(status: int | None, body: Any) -> SyncError:
    """把错误响应体解析为结构化 SyncError；未知形状时按 HTTP 状态归一。

    兼容两类载体：真实 HTTP 状态码，以及后端 HTTP 200 + ``{"code": <int>}``
    的业务态包装（code 命中错误契约时按该状态归类）。
    """
    payload: dict[str, Any] = body if isinstance(body, dict) else {}
    effective = status
    wrapped_code = payload.get("code")
    if (not isinstance(wrapped_code, bool) and isinstance(wrapped_code, int)
            and wrapped_code in ERROR_CODES
            and (effective is None or effective == 200 or effective not in ERROR_CODES)):
        effective = wrapped_code
    inner = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = ""
    for source in (inner, payload):
        candidate = source.get("code") if isinstance(source, dict) else None
        if isinstance(candidate, str) and candidate.startswith("SYNC_"):
            code = candidate
            break
    message = ""
    for source in (inner, payload):
        candidate = source.get("message") or source.get("error")
        if isinstance(candidate, str) and candidate:
            message = candidate
            break
    request_id = ""
    for source in (inner, payload):
        candidate = source.get("request_id")
        if isinstance(candidate, str) and candidate:
            request_id = candidate
            break
    retry_after: int | None = None
    raw_retry_after = payload.get("retry_after")
    if isinstance(raw_retry_after, int) and not isinstance(raw_retry_after, bool) \
            and raw_retry_after >= 0:
        retry_after = raw_retry_after
    required_schema = payload.get("required_schema_version")
    if not isinstance(required_schema, int) or isinstance(required_schema, bool):
        required_schema = None
    min_client = payload.get("min_client_version")
    if not isinstance(min_client, str):
        min_client = None

    http_status = effective if effective in ERROR_CODES else None
    default_code = ERROR_CODES.get(effective or 0, "SYNC_UNKNOWN_ERROR")
    if code:
        final_code = code
    elif effective is None:
        final_code = NETWORK_ERROR_CODE
    elif isinstance(effective, int) and effective >= 500:
        final_code = SERVER_ERROR_CODE
    else:
        final_code = default_code
    if http_status == 426:
        if required_schema is None:
            required_schema = SCHEMA_VERSION
        if min_client is None:
            min_client = MIN_CLIENT_VERSION
        retry_policy = "upgrade_client"
        retryable = False
    elif effective == 401:
        retry_policy = "refresh_auth"
        retryable = False
    elif effective == 409:
        retry_policy = "pull_reconcile"
        retryable = False
    elif effective == 413:
        retry_policy = "repage"
        retryable = False
    elif final_code == NETWORK_ERROR_CODE or (
        isinstance(effective, int) and effective >= 500
    ) or effective in RETRYABLE_STATUS:
        retry_policy = "backoff"
        retryable = True
    else:
        retry_policy = "no"
        retryable = False
    return SyncError(
        status=effective, code=final_code, message=message, request_id=request_id,
        retryable=retryable, retry_policy=retry_policy,
        retry_after_seconds=retry_after,
        required_schema_version=required_schema, min_client_version=min_client,
    )
