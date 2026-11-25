#!/usr/bin/env python3
"""Sensitive data scanning, redaction, and secret shape validation for cloud sync."""
from __future__ import annotations

import datetime as _datetime
import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from core.cloud_sync.cloud_schema import validate_pull_response
from core.secure_atomic import reject_symlink_components

logger = logging.getLogger(__name__)

class SensitiveSyncError(ValueError):
    """拒绝把含疑似凭据的知识记录发送到云端；普通源码证据允许按 scope 同步。"""


# DOC-211：云端同步内容策略（用户级 ~/.bugx/cloud-config.json）。
SYNC_DETAIL_MODES = ("full", "summary")
SENSITIVE_POLICIES = ("block", "redact", "allow")
SYNC_SCOPE_DEFAULTS = ("all", "project", "global")
SYNC_POLICY_VOCABS: dict[str, tuple[str, ...]] = {
    "sync_detail_mode": SYNC_DETAIL_MODES,
    "sensitive_policy": SENSITIVE_POLICIES,
    "sync_scope_default": SYNC_SCOPE_DEFAULTS,
}
# DOC-211 G4：block 档失败信息必须出示配置出口（仅在出网侧闸追加）。
_SENSITIVE_EXIT_HINTS = (
    "\n可选项：① bugx cloud sync --detail-mode summary（不上传代码/详情）\n"
    "        ② bugx config set cloud.sensitive_policy redact（按命中脱敏后上传）\n"
    "        ③ bugx config set cloud.sensitive_policy allow（原样上传，留审计）"
)
_REDACT_UNUSABLE_HINT = (
    "；该形态 redact 不适用，可移除该内容，或 "
    "bugx config set cloud.sensitive_policy allow（原样上传，留审计）"
)



_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloud_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [^-]{0,40}PRIVATE KEY-----")),
    # PE-20261004-005B：值部分不含点/冒号（文件路径:行号类散文不再误报），
    # 显式覆盖 "bearer <JWT/长token>"（旧式漏报）；≥16 字符保持口令/hex 检出。
    # PE-20261008-001：原子组禁止回溯 + 值段紧跟中文视为散文——派生 signature
    # 是 "{category}: {title}"（如 "authorization: write_text_atomic 的…"），
    # category 恰为敏感词且 title 以 ≥16 字符标识符开头，不构成凭据赋值；
    # 真凭据值后不会紧跟中文散文。
    ("credential_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|authorization|cookie)"
        r"\s*[:=]\s*[\"']?(?:bearer\s+)?"
        # 负向先行：值为源文件引用（路径 + 源码扩展名 [+ :行号]）时不算凭据
        r"(?![A-Za-z0-9_./-]*\.(?:py|js|ts|go|md|sql|json|ya?ml|txt|sh|rb|java|toml|cfg|ini|lock|whl|gemspec)\b(?::\d+)?)"
        r"(?>[A-Za-z0-9+/=_\-]{16,})(?!\s*[\u4e00-\u9fff])"
    )),
)

# DOC-007 强化凭据与密码脱敏正则
_ADDITIONAL_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("ai_api_key", re.compile(r"(?i)\b(?:sk-ant|sk|kimi|moonshot|deepseek)-[a-zA-Z0-9_\-\.]{15,}\b"), "{{REDACTED_API_KEY}}"),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.\-~+/=]{20,}\b"), "Bearer {{REDACTED_TOKEN}}"),
    ("dsn_password", re.compile(r"(?i)(\b(?:postgres(?:ql)?|mysql|redis|rediss|mongo(?:db)?(?:\+srv)?|amqp|amqps)://[^:/@\s]+:)(.*?)(@[a-zA-Z0-9_\-\.:]+(?:/[^\s]*)?)"), r"\1[REDACTED_PASSWORD]\3"),
)

# DOC-007 用户主路径与用户名归一化正则
_PATH_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/(?:Users|home)/[a-zA-Z0-9_\-\.]+"), "~"),
    (re.compile(r"[a-zA-Z]:\\\\Users\\\\[a-zA-Z0-9_\-\.]+"), "~"),
)

_SOURCE_CODE_PATTERN = re.compile(r"```[\w\s]*?\n[\s\S]*?```")


def scan_sensitive_text(value: object, *, field: str) -> list[str]:
    """Return sensitive categories found in a wire field without returning values."""
    text = str(value or "")
    return [category for category, pattern in _SENSITIVE_PATTERNS if pattern.search(text)]


def scan_wire_value(value: object, *, path: str, hits: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            scan_wire_value(item, path=f"{path}.{key}", hits=hits)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            scan_wire_value(item, path=f"{path}[{index}]", hits=hits)
    elif isinstance(value, str):
        hits.update(scan_sensitive_text(value, field=path))


def validate_knowledge_for_sync(lessons: list[dict[str, Any]] | None,
                                incidents: list[dict[str, Any]] | None, *,
                                policy: str = "block") -> None:
    """Fail closed before any nested knowledge field reaches the network."""
    if policy == "allow":
        return
    for kind, records in (("lesson", lessons or []), ("incident", incidents or [])):
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise SensitiveSyncError(f"{kind}[{index}] 不是对象，拒绝同步")
            hits: set[str] = set()
            scan_wire_value(record, path=f"{kind}[{index}]", hits=hits)
            if hits:
                raise_sensitive_hit(f"{kind}[{index}]", hits, policy=policy)


def raise_sensitive_hit(where: str, hits: set[str], *, policy: str = "block") -> None:
    categories = ", ".join(sorted(hits))
    if policy == "redact":
        raise SensitiveSyncError(
            f"{where} redact 脱敏后仍命中敏感规则({categories})，拒绝上传{_REDACT_UNUSABLE_HINT}")
    raise SensitiveSyncError(
        f"{where} 命中敏感信息规则({categories})，拒绝上传{_SENSITIVE_EXIT_HINTS}")


def validate_records_for_sync(records: dict[str, list[dict[str, Any]]], *,
                              policy: str = "block") -> None:
    """V2 push 前对全部五类记录做敏感扫描（DOC-211：policy=allow 时跳过）。"""
    if policy == "allow":
        return
    for kind, items in records.items():
        if kind == "patterns":
            validate_knowledge_for_sync(items, [], policy=policy)
        elif kind == "incidents":
            validate_knowledge_for_sync([], items, policy=policy)
        else:
            scan_kind(kind, items, policy=policy)


def scan_kind(kind: str, items: list[dict[str, Any]], *,
               policy: str = "block") -> None:
    for index, record in enumerate(items or []):
        if not isinstance(record, dict):
            raise SensitiveSyncError(f"{kind}[{index}] 不是对象，拒绝同步")
        hits: set[str] = set()
        scan_wire_value(record, path=f"{kind}[{index}]", hits=hits)
        if hits:
            raise_sensitive_hit(f"{kind}[{index}]", hits, policy=policy)


class RedactedTextResult(str):
    """Dual-mode text result: behaves as str (e.g. `assert 'x' in r`) and unpacks as `(str, list[str])`."""

    def __new__(cls, text: str, categories: list[str] | None = None):
        obj = super().__new__(cls, text)
        obj.categories = categories or []
        return obj

    def __iter__(self):
        yield str(self)
        yield self.categories


def redact_sensitive_text(text: str, mask_source: bool = False) -> RedactedTextResult:
    """DOC-211 / DOC-007 redact：按出网闸命中 span 与三级脱敏管道替换敏感信息。

    支持三级过滤：
    - Level 1: 凭据/密钥硬过滤 (API Keys, Bearer tokens, DSN passwords)
    - Level 2: 系统路径与用户身份脱敏 (/Users/<name>, /home/<name> -> ~)
    - Level 3: 源代码片段可选遮蔽 (mask_source=True)
    返回 RedactedTextResult，既兼容元组解包 (text, categories)，又可直接当 str 使用。
    """
    categories: list[str] = []
    out = text or ""

    for category, pattern in _SENSITIVE_PATTERNS:
        replaced, count = pattern.subn("{{REDACTED_%s}}" % category.upper(), out)
        if count:
            categories.append(category)
            out = replaced

    for category, pattern, replacement in _ADDITIONAL_SECRET_PATTERNS:
        replaced, count = pattern.subn(replacement, out)
        if count:
            categories.append(category)
            out = replaced

    for pattern, replacement in _PATH_REDACT_PATTERNS:
        replaced, count = pattern.subn(replacement, out)
        if count:
            categories.append("user_path")
            out = replaced

    if mask_source:
        replaced, count = _SOURCE_CODE_PATTERN.subn("[SOURCE_CODE_REDACTED]", out)
        if count:
            categories.append("source_code")
            out = replaced

    return RedactedTextResult(out, sorted(set(categories)))


def redact_diagnostics_payload(data: Any, mask_source: bool = False) -> tuple[Any, list[str]]:
    """递归遍历并深度脱敏任意嵌套字典、列表或字符串结构。"""
    categories: set[str] = set()

    def _walk(val: Any) -> Any:
        if isinstance(val, dict):
            return {k: _walk(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_walk(v) for v in val]
        if isinstance(val, str):
            res = redact_sensitive_text(val, mask_source=mask_source)
            if res.categories:
                categories.update(res.categories)
            return str(res)
        return val

    processed = _walk(data)
    return processed, sorted(categories)


def redact_records_for_sync(records: Mapping[str, list]) -> tuple[dict[str, list], list[str], int]:
    """字段级确定性 redact（DOC-211 §4）：无跨字段计数器，同输入恒同输出，
    保证 manifest_hash 稳定、operation_id 不漂移。返回 (records, categories, 字段数)。"""
    categories: set[str] = set()
    total = 0

    def _walk(value: Any) -> Any:
        nonlocal total
        if isinstance(value, dict):
            return {key: _walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, str):
            new_text, hits = redact_sensitive_text(value)
            if hits:
                categories.update(hits)
                total += 1
                return new_text
        return value

    redacted = {kind: [_walk(item) for item in items]
                for kind, items in records.items()}
    return redacted, sorted(categories), total


def iter_record_strings(value: Any) -> "Iterator[str]":
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str):
                yield f"{key}: {item}"
            else:
                yield from iter_record_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_record_strings(item)


def raise_secret_shape(where: str, matched: str, *, policy: str) -> None:
    if policy == "redact":
        raise SensitiveSyncError(
            f"{where} redact 脱敏后仍命中凭据形状({matched[:12]!r}…)，拒绝上传{_REDACT_UNUSABLE_HINT}")
    raise SensitiveSyncError(
        f"{where} 命中疑似凭据或密钥形状({matched[:12]!r}…)，拒绝上传{_SENSITIVE_EXIT_HINTS}")


def validate_secret_shapes(records: Mapping[str, list], *, policy: str = "block") -> None:
    """构建闸同源复扫（DOC-211 §4）：三类正则之外的真凭据形状（sk-/JWT/DSN 等）。

    push 主路径在构建闸（enforce_credential_block）已扫过，此处对 import/upload
    直连路径补强（审计实证旧 3 正则弱闸放行 bare sk-/JWT/DSN/键位 AKIA）。
    """
    if policy == "allow":
        return
    from core.governance.secret_redactor import find_unredacted_secrets
    for kind, items in records.items():
        for index, record in enumerate(items or []):
            if not isinstance(record, dict):
                continue
            for text in iter_record_strings(record):
                matches = find_unredacted_secrets(text)
                if matches:
                    raise_secret_shape(f"{kind}[{index}]", matches[0], policy=policy)


def emit_policy_audit(policy: str, categories: list[str], redacted_count: int) -> None:
    """DOC-211 G3：非 block 档必须留痕（用户级审计文件，只增不阻，绝不含原值）。"""
    try:
        base = Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx")))
        audit_path = base / "logs" / "sync-policy-audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        reject_symlink_components(audit_path)
        entry = {
            "ts": _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds"),
            "policy": policy,
            "categories": categories,
            "redacted_fields": redacted_count,
            "pid": os.getpid(),
        }
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 - 审计失败绝不阻断同步主流程
        pass


def prepare_records_for_sync(records: Mapping[str, list], *,
                             policy: str = "redact") -> dict[str, Any]:
    """DOC-211：push/upload 发网前的敏感策略统一入口（默认档安全默认 redact，F-025）。

    必须在 manifest_hash/envelope 构建之前调用（redact 会改内容，幂等键在
    manifest 之后派生会错位）。block=三正则+凭据形状双扫描，命中即拒（原语义
    + import 弱闸补强）；redact=span 整段占位符替换后双重复扫，仍命中即拒；
    allow=原样放行（留审计事件）。
    """
    if policy not in SENSITIVE_POLICIES:
        raise SensitiveSyncError(f"未知敏感策略: {policy!r}（允许 {'/'.join(SENSITIVE_POLICIES)}）")
    if policy == "allow":
        emit_policy_audit("allow", [], 0)
        return {"records": records, "policy": policy,
                "categories": [], "redacted_count": 0}
    if policy == "redact":
        redacted, categories, count = redact_records_for_sync(records)
        validate_records_for_sync(redacted, policy="redact")
        validate_secret_shapes(redacted, policy="redact")
        emit_policy_audit("redact", categories, count)
        return {"records": redacted, "policy": policy,
                "categories": categories, "redacted_count": count}
    validate_records_for_sync(records, policy="block")
    validate_secret_shapes(records, policy="block")
    return {"records": records, "policy": policy,
            "categories": [], "redacted_count": 0}


def wire_project_id(project_id: str) -> int | None:
    if not project_id:
        return None
    value = str(project_id)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("project_id 必须是服务端返回的正整数 ID")
    return int(value)


def validate_pull_envelope(data: object, *, scope: str, project_id: str = "") -> None:
    """严格校验 V2 pull 响应（DOC-008 §5.2）；缺失 schema 或 legacy 字段一律拒绝。"""
    validate_pull_response(data, scope=scope, project_id=project_id)



# Compatibility aliases
_scan_wire_value = scan_wire_value
_raise_sensitive_hit = raise_sensitive_hit
_scan_kind = scan_kind
_iter_record_strings = iter_record_strings
_raise_secret_shape = raise_secret_shape
_validate_secret_shapes = validate_secret_shapes
_emit_policy_audit = emit_policy_audit
_wire_project_id = wire_project_id
