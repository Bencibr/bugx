#!/usr/bin/env python3
"""Ledger 子包的**同级符号解析器**（DES-02）。

原先 ``core/ledger/*.py`` 各自带一个 ``_LedgerProxy``，通过
``sys.modules.get("lesson_ledger")``（取不到就 ``from . import lesson_ledger``）
反向取符号；而 ``lesson_ledger`` 又是顶层导入 ``ledger.*`` 的纯 Facade。
这构成一个 **Facade ↔ 领域子包** 的双向依赖环：
``ledger/*`` 想拿一个**同包内的兄弟符号**，却要绕道顶层 Facade 再下来。

本模块把「名字 → 定义它的兄弟模块」做成**静态表**并在取用时**延迟导入**，
于是 ``ledger/*`` 只依赖同包（或 ``token_budget`` 这类下层），不再触碰
``lesson_ledger``，环被消除。

等价性：``lesson_ledger`` 本身也只是「从各兄弟模块 re-export」的纯 Facade，
且经核对 **160 个导出名无一被多个模块重复导出**，因此「名字 → 模块」的映射
是**唯一确定**的，本表与 ``getattr(lesson_ledger, name)`` 必然解析到同一对象。
该等价性由 ``tests/test_ledger_dependency_direction.py`` 逐名字断言守护。
"""

from __future__ import annotations

import importlib
from typing import Any


# 名字 -> 定义它的模块（取自 lesson_ledger.py 的导入块，无重复导出）
_FACADE_SOURCES: dict[str, str] = {
    'AUDITED_TABLES': 'core.ledger.schema',
    'CARD_FIELDS': 'core.ledger.schema',
    'CARD_OPTIONAL_FIELDS': 'core.ledger.schema',
    'DEFAULT_BYTE_BUDGET': 'core.ledger.schema',
    'DEFAULT_LEDGER': 'core.ledger.schema',
    'resolve_default_ledger': 'core.ledger.schema',
    'DEFAULT_LIMIT': 'core.ledger.schema',
    'DEFAULT_TOKEN_BUDGET': 'core.ledger.schema',
    'FTS_COLUMNS': 'core.ledger.schema',
    'INCIDENT_STATUSES': 'core.ledger.schema',
    'LESSON_STATUSES': 'core.ledger.schema',
    'LessonLedgerError': 'core.ledger.schema',
    'MAX_EVENTS': 'core.ledger.schema',
    'MAX_LIST_ITEMS': 'core.ledger.schema',
    'MAX_TEXT_LENGTH': 'core.ledger.schema',
    'MAX_TOOLS_PER_FEEDBACK': 'core.ledger.schema',
    'OUTCOMES': 'core.ledger.schema',
    'PATTERN_OUTCOMES': 'core.ledger.schema',
    'PATTERN_STATUSES': 'core.ledger.schema',
    'ROUTING_TAG_KEYS': 'core.ledger.schema',
    'SCHEMA': 'core.ledger.schema',
    'SCHEMA_VERSION': 'core.ledger.schema',
    'TOOL_EXPERIENCE_FIELDS': 'core.ledger.schema',
    'TOOL_EXPERIENCE_FORBIDDEN_KEYS': 'core.ledger.schema',
    'TOOL_EXPERIENCE_LEVELS': 'core.ledger.schema',
    'TOOL_LOOKUP_BYTE_BUDGET': 'core.ledger.schema',
    'TOOL_LOOKUP_LIMIT': 'core.ledger.schema',
    'TOOL_LOOKUP_TOKEN_BUDGET': 'core.ledger.schema',
    'TOOL_NAME_RE': 'core.ledger.schema',
    'AUDIT_ORDER_BY': 'core.ledger.schema',
    'CARD_LIMITS': 'core.ledger.schema',
    'FP_REGEX': 'core.ledger.schema',
    'ID_REGEX': 'core.ledger.schema',
    'INCIDENT_ID_REGEX': 'core.ledger.schema',
    'ManagedConnection': 'core.ledger.schema',
    'SECRET_REGEX': 'core.ledger.schema',
    'apply_synced_tombstone_tx': 'core.ledger.sync_ops',
    'archive_manifest': 'core.ledger.schema',
    'assert_no_symlink_components': 'core.ledger.schema',
    'audit_snapshot': 'core.ledger.schema',
    'book_line': 'core.ledger.export_ops',
    'canonical_bytes': 'core.ledger.schema',
    'confirmed_finding_digest': 'core.ledger.incidents',
    'connect_ledger': 'core.ledger.schema',
    'count_active_events': 'core.ledger.archive_ops',
    'decode_routing_tags': 'core.ledger.patterns',
    'ensure_schema': 'core.ledger.schema',
    'record_event': 'core.ledger.schema',
    'event_from_row_values': 'core.ledger.archive_ops',
    'execute_script': 'core.ledger.schema',
    'extract_card_fields': 'core.ledger.patterns',
    'extract_feedback_tools': 'core.ledger.tool_experience',
    'extract_routing_tags': 'core.ledger.patterns',
    'fetch_incident_tools': 'core.ledger.incidents',
    'fetch_tool_aggregates': 'core.ledger.tool_experience',
    'init_fts': 'core.ledger.schema',
    'fts_expected_row': 'core.ledger.archive_ops',
    'import_synced_incident_tx': 'core.ledger.sync_ops',
    'import_synced_pattern_tx': 'core.ledger.sync_ops',
    'import_tool_experience_tx': 'core.ledger.tool_experience',
    'incident_content': 'core.ledger.incidents',
    'incident_content_hash': 'core.ledger.incidents',
    'incident_value': 'core.ledger.incidents',
    'insert_incident': 'core.ledger.incidents',
    'insert_pattern': 'core.ledger.schema',
    'lessons_dict': 'core.ledger.patterns',
    'parse_json_list': 'core.ledger.schema',
    'get_meta_value': 'core.ledger.schema',
    'normalize_duration': 'core.ledger.incidents',
    'normalize_finding_fingerprint': 'core.ledger.incidents',
    'normalize_id': 'core.ledger.schema',
    'normalize_incident': 'core.ledger.incidents',
    'normalize_pattern': 'core.ledger.patterns',
    'normalize_tool_experience_records': 'core.ledger.tool_experience',
    'normalize_tool_name': 'core.ledger.tool_experience',
    'normalize_tool_value': 'core.ledger.tool_experience',
    'parse_not_after': 'core.ledger.schema',
    'now_datetime': 'core.ledger.incidents',
    'now_iso': 'core.ledger.schema',
    'pattern_value': 'core.ledger.patterns',
    'patterns_are_equal': 'core.ledger.patterns',
    'project_profile_defaults': 'core.ledger.patterns',
    'read_seed': 'core.ledger.schema',
    'record_consumption_event_tx': 'core.ledger.incidents',
    'record_feedback_tx': 'core.ledger.incidents',
    'refresh_materialized_views': 'core.ledger.schema',
    'is_regular_file': 'core.ledger.schema',
    'retire_migrate_backup': 'core.ledger.schema',
    'routing_match': 'core.ledger.patterns',
    'get_schema_version': 'core.ledger.schema',
    'split_sync_values': 'core.ledger.sync_ops',
    'state_digest': 'core.ledger.schema',
    'sync_import_content_hash': 'core.ledger.sync_ops',
    'sync_state_hash': 'core.ledger.schema',
    'synced_incident': 'core.ledger.sync_ops',
    'synced_pattern': 'core.ledger.sync_ops',
    'sanitize_text': 'core.ledger.schema',
    'sanitize_text_long': 'core.ledger.schema',
    'sanitize_text_opt': 'core.ledger.schema',
    'tool_experience_content': 'core.ledger.tool_experience',
    'tool_experience_hash': 'core.ledger.tool_experience',
    'tool_experience_level': 'core.ledger.tool_experience',
    'ledger_transaction': 'core.ledger.schema',
    'validate_pattern': 'core.ledger.schema',
    'validate_tool_experience_record': 'core.ledger.tool_experience',
    'with_project_profile_defaults': 'core.ledger.patterns',
    'activate_incident': 'core.ledger.incidents',
    'activate_tool_experience': 'core.ledger.tool_experience',
    'apply_auto_materialization': 'core.ledger.auto_materialization',
    'add_incident': 'core.ledger.incidents',
    'add_lesson': 'core.ledger.patterns',
    'apply_review': 'core.ledger.incidents',
    'archive_events': 'core.ledger.archive_ops',
    'archive_incidents': 'core.ledger.incidents',
    'backup_database': 'core.ledger.export_ops',
    'created_not_after': 'core.ledger.patterns',
    'database_path': 'core.ledger.schema',
    'estimate_tokens': 'core.governance.token_budget',
    'export_book': 'core.ledger.export_ops',
    'export_consumption_events': 'core.ledger.incidents',
    'export_snapshot': 'core.ledger.export_ops',
    'export_tool_experience': 'core.ledger.tool_experience',
    'feedback_outcomes': 'core.ledger.incidents',
    'fts_audit': 'core.ledger.archive_ops',
    'hygiene': 'core.ledger.patterns',
    'import_patterns': 'core.ledger.patterns',
    'import_synced_records': 'core.ledger.sync_ops',
    'import_tool_experience': 'core.ledger.tool_experience',
    'incident_id': 'core.ledger.incidents',
    'inherit_patterns': 'core.ledger.export_ops',
    'initialize': 'core.ledger.schema',
    'lesson_id': 'core.ledger.patterns',
    'list_incidents': 'core.ledger.incidents',
    'list_tool_experience': 'core.ledger.tool_experience',
    'load_archived_events': 'core.ledger.archive_ops',
    'lookup_incidents': 'core.ledger.incidents',
    'lookup_tools': 'core.ledger.tool_experience',
    'main': 'core.ledger.cli',
    'merge_patterns': 'core.ledger.patterns',
    'metrics': 'core.ledger.sync_ops',
    'migrate': 'core.ledger.schema',
    'normalize_signature': 'core.ledger.incidents',
    'promote_incident': 'core.ledger.export_ops',
    'promote_pattern': 'core.ledger.export_ops',
    'promote_tool_experience': 'core.ledger.export_ops',
    'synthesize_skill_pack': 'core.ledger.export_ops',
    'read': 'core.ledger.patterns',
    'read_pattern': 'core.ledger.patterns',
    'rebuild_fts': 'core.ledger.archive_ops',
    'record_consumption_event': 'core.ledger.incidents',
    'record_evidence_receipt': 'core.ledger.sync_ops',
    'record_feedback': 'core.ledger.incidents',
    'record_tool_feedback': 'core.ledger.tool_experience',
    'reset_cloud_scope': 'core.ledger.sync_ops',
    'retire_pattern': 'core.ledger.patterns',
    'retrieve': 'core.ledger.patterns',
    'review_details': 'core.ledger.incidents',
    'review_exists': 'core.ledger.incidents',
    'revoke_incident': 'core.ledger.incidents',
    'revoke_pattern': 'core.ledger.patterns',
    'seed_path': 'core.ledger.schema',
    'sync_import_receipt': 'core.ledger.sync_ops',
    'tool_matrix': 'core.ledger.tool_experience',
    'tool_metrics': 'core.ledger.tool_experience',
    'validate_deep': 'core.ledger.archive_ops',
}


class LedgerProxy:
    """把 ``_ledger.<name>`` 解析到**定义它的兄弟模块**（延迟导入）。

    DES-02：替代原先 ``sys.modules.get("lesson_ledger")`` 的软环取法。
    延迟导入（属性访问时才 ``importlib.import_module``）保证不产生任何
    导入期依赖，因此也不会与包内其它兄弟模块形成新的环。
    """

    __slots__ = ()

    @staticmethod
    def _resolve(name: str) -> Any:
        module = _FACADE_SOURCES.get(name)
        if module is None:
            raise AttributeError(
                f"ledger 同级解析表中没有 {name!r}；若确有新符号，请把它加到 "
                f"core/ledger/_facade.py 的 _FACADE_SOURCES（DES-02）"
            )
        mod = importlib.import_module(module)
        return getattr(mod, name)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self._resolve(name)

    def __dir__(self) -> list[str]:
        return sorted(_FACADE_SOURCES)


__all__ = ["LedgerProxy", "_FACADE_SOURCES"]
