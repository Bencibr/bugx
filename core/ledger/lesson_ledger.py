#!/usr/bin/env python3
"""SQLite-backed lesson/pattern ledger with a JSON seed and bounded retrieval (DOC-072 facade)."""
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
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR), str(_CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.governance.token_budget import estimate_tokens

# 1. Schema, DDL, and Connection Management
from core.ledger.schema import (
    AUDITED_TABLES,
    CARD_FIELDS,
    CARD_OPTIONAL_FIELDS,
    DEFAULT_BYTE_BUDGET,
    DEFAULT_LEDGER,
    DEFAULT_LIMIT,
    DEFAULT_TOKEN_BUDGET,
    FTS_COLUMNS,
    INCIDENT_STATUSES,
    LESSON_STATUSES,
    LessonLedgerError,
    resolve_default_ledger,
    MAX_EVENTS,
    MAX_LIST_ITEMS,
    MAX_TEXT_LENGTH,
    MAX_TOOLS_PER_FEEDBACK,
    OUTCOMES,
    PATTERN_OUTCOMES,
    PATTERN_STATUSES,
    ROUTING_TAG_KEYS,
    SCHEMA,
    SCHEMA_VERSION,
    TOOL_EXPERIENCE_FIELDS,
    TOOL_EXPERIENCE_FORBIDDEN_KEYS,
    TOOL_EXPERIENCE_LEVELS,
    TOOL_LOOKUP_BYTE_BUDGET,
    TOOL_LOOKUP_LIMIT,
    TOOL_LOOKUP_TOKEN_BUDGET,
    TOOL_NAME_RE,
    AUDIT_ORDER_BY,
    CARD_LIMITS,
    FP_REGEX,
    ID_REGEX,
    INCIDENT_ID_REGEX,
    ManagedConnection,
    SECRET_REGEX,
    archive_manifest,
    assert_no_symlink_components,
    audit_snapshot,
    canonical_bytes,
    connect_ledger,
    ensure_schema,
    record_event,
    execute_script,
    init_fts,
    insert_pattern,
    parse_json_list,
    get_meta_value,
    normalize_id,
    parse_not_after,
    now_iso,
    read_seed,
    refresh_materialized_views,
    is_regular_file,
    retire_migrate_backup,
    get_schema_version,
    state_digest,
    sync_state_hash,
    sanitize_text,
    sanitize_text_long,
    sanitize_text_opt,
    ledger_transaction,
    validate_pattern,
    database_path,
    initialize,
    migrate,
    seed_path,
)

# 2. Pattern domain operations
from core.ledger.patterns import (
    decode_routing_tags,
    extract_card_fields,
    extract_routing_tags,
    lessons_dict,
    normalize_pattern,
    pattern_value,
    patterns_are_equal,
    project_profile_defaults,
    routing_match,
    with_project_profile_defaults,
    add_lesson,
    created_not_after,
    hygiene,
    import_patterns,
    lesson_id,
    merge_patterns,
    read,
    read_pattern,
    retire_pattern,
    retrieve,
    revoke_pattern,
)

# 3. Incident domain operations
from core.ledger.incidents import (
    activate_incident,
    add_incident,
    apply_review,
    archive_incidents,
    confirmed_finding_digest,
    export_consumption_events,
    feedback_outcomes,
    fetch_incident_tools,
    incident_content,
    incident_content_hash,
    incident_id,
    incident_value,
    insert_incident,
    list_incidents,
    lookup_incidents,
    normalize_duration,
    normalize_finding_fingerprint,
    normalize_incident,
    normalize_signature,
    now_datetime,
    record_consumption_event,
    record_consumption_event_tx,
    record_feedback,
    record_feedback_tx,
    review_details,
    review_exists,
    revoke_incident,
)

# 4. Tool Experience
from core.ledger.tool_experience import (
    activate_tool_experience,
    export_tool_experience,
    extract_feedback_tools,
    fetch_tool_aggregates,
    import_tool_experience,
    import_tool_experience_tx,
    list_tool_experience,
    lookup_tools,
    normalize_tool_experience_records,
    normalize_tool_name,
    normalize_tool_value,
    record_tool_feedback,
    tool_experience_content,
    tool_experience_hash,
    tool_experience_level,
    tool_matrix,
    tool_metrics,
    validate_tool_experience_record,
)

# DOC-208 D1: single-transaction persistence for sanitized automatic knowledge.
from core.ledger.auto_materialization import apply_auto_materialization

# 5. Cloud sync operations
from core.ledger.sync_ops import (
    apply_synced_tombstone_tx,
    import_synced_incident_tx,
    import_synced_pattern_tx,
    import_synced_records,
    metrics,
    record_evidence_receipt,
    reset_cloud_scope,
    split_sync_values,
    sync_import_content_hash,
    sync_import_receipt,
    synced_incident,
    synced_pattern,
)

# 6. Export and archive operations
from core.ledger.export_ops import (
    backup_database,
    book_line,
    export_book,
    export_snapshot,
    inherit_patterns,
    promote_incident,
    promote_pattern,
    promote_tool_experience,
    synthesize_skill_pack,
)

from core.ledger.archive_ops import (
    archive_events,
    count_active_events,
    event_from_row_values,
    fts_audit,
    fts_expected_row,
    load_archived_events,
    rebuild_fts,
    validate_deep,
)

from core.ledger.cli import main

# Compatibility aliases
_pattern = normalize_pattern
_pattern_value = pattern_value

if __name__ == '__main__':
    sys.exit(main())
