#!/usr/bin/env python3
"""Project profiling and classification package."""
from __future__ import annotations

from .arbitrator import HeuristicProfileClassifier, LLMProfileArbitrator
from .constants import (
    ALIASES,
    ARTIFACT_DIRS,
    BUILD_FINGERPRINT_FILES,
    CHECK_PROFILE_REQUIREMENTS,
    CLI_MARKERS,
    CLIENT_ONLY_PROFILE_KEYS,
    CONTENT_SCAN_BYTES,
    CONTENT_SCAN_FILES,
    CONTENT_SUFFIXES,
    DEFAULT_BYTE_LIMIT,
    DEFAULT_FILE_LIMIT,
    DETECTOR_VERSION_BASE,
    ENTRYPOINT_NAMES,
    FORMS,
    HTTP_MARKERS,
    LANGUAGES_BY_EXT,
    MANIFEST_NAMES,
    MAX_COMMANDS,
    MAX_COMPONENTS,
    MAX_EVIDENCE,
    MAX_FACTS,
    MAX_MANIFEST_BYTES,
    PROFILE_MEM_CACHE,
    PROFILE_ROW_KEY,
    PROFILE_SCHEMA_VERSION,
    PY_CLI_DEPS,
    PY_FRAMEWORK_DEPS,
    PY_HTTP_DEPS,
    PY_QUEUE_DEPS,
    PY_STORAGE_DEPS,
    QUEUE_MARKERS,
    RUNTIMES,
    SENSITIVE,
    SKIP_DIRS,
    SOURCE_EXTENSIONS_BY_ECOSYSTEM,
    STORAGE_MARKERS,
    SURFACES,
    TARGETS,
    TEST_PATH_SEGMENTS,
    VERSION_FILES,
    WIRE_COMPONENTS_LIMIT,
    WIRE_DROP_ORDER,
    WIRE_EVIDENCE_LIMIT,
    WIRE_FACTS_LIMIT,
    WIRE_PAYLOAD_BUDGET_BYTES,
    WIRE_RISK_SIGNAL_KEYS,
    WIRE_RISK_SIGNALS_LIMIT,
)
from .detector import code_analyzer_module, detect_project_profile
from .extractors import (
    DETECTOR_VERSION,
    add_fact,
    add_path_signals,
    add_signal,
    content_signals,
    detector_rules_digest,
    detector_source_digest,
    evidence,
    extract_manifest_signals,
    extract_version_facts,
    fact,
    find_ipa_marker,
    hash_projection,
    is_manifest_name,
    manifest_data,
    python_dependency_signals,
    requirement_names,
    sca_module,
    stable_files,
    version_line_fact,
    version_value,
)
from .fingerprint import (
    canon,
    canonical,
    compute_build_fingerprint,
    private_cache_directory,
    private_profile_cache,
    profile_cache_path,
    read_bounded,
    safe_relative,
)
from .storage import (
    confirmed_first,
    ensure_project_profile,
    export_project_profile,
    ledger_module,
    load_project_profile,
    save_project_profile,
    slim_component,
    slim_risk_signal,
    wire_project_profile,
    wire_safe_entry,
    wire_safe_slice,
)
from .strategy import (
    classify_profile,
    compile_audit_strategy,
    routing_tags,
)

# Backwards compatibility aliases for leading-underscore names
_PROFILE_MEM_CACHE = PROFILE_MEM_CACHE
_ALIASES = ALIASES
_SKIP_DIRS = SKIP_DIRS
_ENTRYPOINT_NAMES = ENTRYPOINT_NAMES
_SENSITIVE = SENSITIVE
_MANIFEST_NAMES = MANIFEST_NAMES
_VERSION_FILES = VERSION_FILES
_BUILD_FINGERPRINT_FILES = BUILD_FINGERPRINT_FILES
_CHECK_PROFILE_REQUIREMENTS = CHECK_PROFILE_REQUIREMENTS
_ARTIFACT_DIRS = ARTIFACT_DIRS
_WIRE_COMPONENTS_LIMIT = WIRE_COMPONENTS_LIMIT
_WIRE_RISK_SIGNALS_LIMIT = WIRE_RISK_SIGNALS_LIMIT
_WIRE_EVIDENCE_LIMIT = WIRE_EVIDENCE_LIMIT
_WIRE_FACTS_LIMIT = WIRE_FACTS_LIMIT
_WIRE_PAYLOAD_BUDGET_BYTES = WIRE_PAYLOAD_BUDGET_BYTES
_WIRE_RISK_SIGNAL_KEYS = WIRE_RISK_SIGNAL_KEYS
_WIRE_DROP_ORDER = WIRE_DROP_ORDER
_CLIENT_ONLY_PROFILE_KEYS = CLIENT_ONLY_PROFILE_KEYS
_TEST_PATH_SEGMENTS = TEST_PATH_SEGMENTS
_SOURCE_EXTENSIONS_BY_ECOSYSTEM = SOURCE_EXTENSIONS_BY_ECOSYSTEM

_canon = canon
_canonical = canonical
_safe_relative = safe_relative
_read_bounded = read_bounded
_private_cache_directory = private_cache_directory
_profile_cache_path = profile_cache_path
_private_profile_cache = private_profile_cache

_hash_projection = hash_projection
_manifest_data = manifest_data
_version_value = version_value
_add_fact = add_fact
_fact = fact
_version_line_fact = version_line_fact
_sca_module = sca_module
_add_signal = add_signal
_evidence = evidence
_is_manifest_name = is_manifest_name
_python_dependency_signals = python_dependency_signals
_requirement_names = requirement_names
_content_signals = content_signals
_extract_manifest_signals = extract_manifest_signals
_detector_rules_digest = detector_rules_digest
_detector_source_digest = detector_source_digest
_find_ipa_marker = find_ipa_marker
_add_path_signals = add_path_signals
_extract_version_facts = extract_version_facts
_stable_files = stable_files

_code_analyzer_module = code_analyzer_module
_ledger_module = ledger_module
_slim_component = slim_component
_slim_risk_signal = slim_risk_signal
_confirmed_first = confirmed_first
_wire_safe_entry = wire_safe_entry
_wire_safe_slice = wire_safe_slice

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "DETECTOR_VERSION_BASE",
    "DETECTOR_VERSION",
    "PROFILE_ROW_KEY",
    "DEFAULT_FILE_LIMIT",
    "DEFAULT_BYTE_LIMIT",
    "MAX_MANIFEST_BYTES",
    "MAX_COMMANDS",
    "MAX_EVIDENCE",
    "MAX_FACTS",
    "MAX_COMPONENTS",
    "FORMS",
    "SURFACES",
    "TARGETS",
    "RUNTIMES",
    "LANGUAGES_BY_EXT",
    "WIRE_COMPONENTS_LIMIT",
    "WIRE_RISK_SIGNALS_LIMIT",
    "WIRE_EVIDENCE_LIMIT",
    "WIRE_FACTS_LIMIT",
    "WIRE_PAYLOAD_BUDGET_BYTES",
    "compute_build_fingerprint",
    "classify_profile",
    "compile_audit_strategy",
    "detect_project_profile",
    "load_project_profile",
    "save_project_profile",
    "ensure_project_profile",
    "export_project_profile",
    "wire_project_profile",
    "routing_tags",
    "HeuristicProfileClassifier",
    "LLMProfileArbitrator",
]
