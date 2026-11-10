#!/usr/bin/env python3
"""Deterministic project profile detection pipeline."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import core.secure_atomic as secure_atomic
except ImportError:
    from .. import secure_atomic  # type: ignore

from .arbitrator import HeuristicProfileClassifier, LLMProfileArbitrator
from .constants import (
    CONTENT_SCAN_BYTES,
    CONTENT_SCAN_FILES,
    CONTENT_SUFFIXES,
    DEFAULT_BYTE_LIMIT,
    DEFAULT_FILE_LIMIT,
    FORMS,
    LANGUAGES_BY_EXT,
    MAX_COMMANDS,
    MAX_COMPONENTS,
    MAX_EVIDENCE,
    MAX_FACTS,
    PROFILE_MEM_CACHE,
    PROFILE_SCHEMA_VERSION,
    RUNTIMES,
    SURFACES,
    TARGETS,
)
from .extractors import (
    DETECTOR_VERSION,
    add_path_signals,
    add_signal,
    content_signals,
    extract_manifest_signals,
    extract_version_facts,
    find_ipa_marker,
    hash_projection,
    is_manifest_name,
    sca_module,
    stable_files,
)
from .fingerprint import (
    canon,
    canonical,
    compute_build_fingerprint,
    private_profile_cache,
    profile_cache_path,
)
from .strategy import classify_profile


# 覆盖钩子只认**真实模块名**。``core.project_profile`` / ``project_profile``
# 是迁移前的扁平名，早已不存在：保留它们等于「查了三个永远为 None 的键」，
# 于是任何 `mock.patch.object(project_profile, "LLMProfileArbitrator", ...)`
# 都不生效 —— 测试以为替换了裁决器，实际仍走真实实现，断言以
# 「'heuristic_baseline' != 'llm'」这种语义假象失败（真因是补丁打空）。
_OVERRIDE_MODULES = ("core.runtime.project_profile",)


def _get_arbitrator_cls():
    for mod_name in _OVERRIDE_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "LLMProfileArbitrator"):
            return getattr(mod, "LLMProfileArbitrator")
    return LLMProfileArbitrator


def _get_detector_version() -> str:
    for mod_name in _OVERRIDE_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "DETECTOR_VERSION"):
            return str(getattr(mod, "DETECTOR_VERSION"))
    return DETECTOR_VERSION


def code_analyzer_module():
    try:
        from core.retrieval import code_analyzer
        return code_analyzer
    except ImportError:
        try:
            from .. import code_analyzer  # type: ignore
            return code_analyzer
        except ImportError:
            from core.retrieval import code_analyzer
            return code_analyzer


def detect_project_profile(root: str | os.PathLike[str], *, file_limit: int = DEFAULT_FILE_LIMIT, byte_limit: int = DEFAULT_BYTE_LIMIT, persist: bool = True, allow_llm: bool = True) -> dict[str, Any]:
    """Detect a normalized ProjectProfile without executing project code."""
    project_root = Path(root).resolve()
    files, truncated, scanned_bytes = stable_files(project_root, file_limit=file_limit, byte_limit=byte_limit)
    classifier = HeuristicProfileClassifier(project_root, files=files, persist=persist)
    isolated_fixtures, high_confidence = classifier.classify_isolated_fixtures()
    arbitrated_forms: list[str] | None = None
    arbitration_data: dict[str, Any] | None = None

    if isolated_fixtures and allow_llm and os.environ.get("BUGX_PROFILE_NO_LLM") != "1":
        arbitrator_cls = _get_arbitrator_cls()
        arbitrator = arbitrator_cls(
            project_root, compute_build_fingerprint(project_root), persist=persist,
        )
        isolated_fixtures, arbitrated_forms = arbitrator.arbitrate(isolated_fixtures, [])
        arbitration_data = arbitrator.get_cached_arbitration()

    filtered_files = [f for f in files if f[0] not in isolated_fixtures]

    signals: dict[str, set[str]] = {}
    commands: list[str] = []
    evidence_list: list[dict[str, Any]] = []
    fingerprint_rows: list[dict[str, Any]] = []
    content_scanned = 0
    for rel, path, size, digest in filtered_files:
        fingerprint_rows.append({"path": rel, "size": size, "file_fingerprint": digest})
        add_path_signals(rel, path.name, signals, evidence_list)
        if (
            content_scanned < CONTENT_SCAN_FILES
            and size <= CONTENT_SCAN_BYTES
            and path.suffix.casefold() in CONTENT_SUFFIXES
        ):
            content_signals(path, size, signals)
            content_scanned += 1
        if is_manifest_name(path.name):
            extract_manifest_signals(rel, path, signals, commands, evidence_list)

    names = {path.name.casefold() for _, path, _, _ in filtered_files}
    if "app.json" in names or "pubspec.yaml" in names:
        add_signal(signals, "forms", "mobile")

    ipa_present = find_ipa_marker(project_root) or any(
        path.suffix.casefold() == ".ipa" for _, path, _, _ in files
    )
    ios_bundle = False
    if not ipa_present:
        for _, path, _, _ in files:
            parts = path.parts
            for index, part in enumerate(parts[:-1]):
                if part.casefold().endswith(".app") and parts[index + 1] == "Info.plist":
                    ios_bundle = True
                    break
            if ios_bundle:
                break
    if ipa_present or ios_bundle:
        add_signal(signals, "forms", "mobile")
        add_signal(signals, "targets", "ios")

    if "project.config.json" in names or any(
        path.suffix.casefold() == ".wxml" for _, path, _, _ in filtered_files
    ):
        add_signal(signals, "forms", "mobile")
        add_signal(signals, "frameworks", "miniprogram")
    if "project.pbxproj" in names:
        add_signal(signals, "forms", "mobile")
        add_signal(signals, "targets", "ios")
    if "gradlew" in names or "androidmanifest.xml" in names:
        add_signal(signals, "forms", "mobile")
        add_signal(signals, "targets", "android")
    if "package.json" in names and "cli" not in signals.get("forms", set()) and not ({"web", "backend"} & signals.get("forms", set())):
        add_signal(signals, "forms", "library")
    if len(signals.get("languages", set())) == 1 and signals.get("languages", set()) & {"c", "cpp", "rust"} and "embedded" not in signals.get("forms", set()):
        add_signal(signals, "forms", "desktop")
    if arbitrated_forms and (arbitration_data or {}).get("source") != "heuristic_baseline":
        for form in arbitrated_forms:
            add_signal(signals, "forms", form)

    languages = sorted(signals.get("languages", set()))
    if "python" in languages:
        add_signal(signals, "runtimes", "cpython")
    if any(x in languages for x in ("javascript", "typescript", "jsx", "tsx", "vue", "svelte")):
        add_signal(signals, "runtimes", "nodejs")
    if any(x in languages for x in ("java", "kotlin", "scala", "groovy")):
        add_signal(signals, "runtimes", "jvm")
    if "csharp" in languages:
        add_signal(signals, "runtimes", "dotnet")
    if "dart" in languages:
        add_signal(signals, "runtimes", "dart-vm")
    runtime_facts, framework_facts, build_targets, toolchains, unknowns = extract_version_facts(filtered_files)
    for f_item in runtime_facts:
        if f_item.get("name"):
            add_signal(signals, "runtimes", str(f_item["name"]))
        if f_item.get("language"):
            add_signal(signals, "languages", str(f_item["language"]))
    for f_item in framework_facts:
        if f_item.get("name"):
            add_signal(signals, "frameworks", str(f_item["name"]))
    for target in build_targets:
        platform = str(target.get("platform") or target.get("name") or "").casefold()
        if platform in {"android", "ios", "mobile"} or platform == "android-ios":
            add_signal(signals, "forms", "mobile")
        if platform in {"desktop", "windows", "macos", "linux"} or str(target.get("name")) in {"desktop", "windows-macos-linux"}:
            add_signal(signals, "forms", "desktop")
        if platform in TARGETS:
            add_signal(signals, "declared_targets", platform)
    for key, allowed in (("forms", FORMS), ("languages", tuple(LANGUAGES_BY_EXT.values())), ("runtimes", RUNTIMES), ("surfaces", SURFACES), ("declared_targets", TARGETS)):
        signals[key] = {canonical(value, allowed) for value in signals.get(key, set()) if canonical(value, allowed) != "unknown"}
    if not signals.get("forms") and signals.get("languages"):
        langs = signals["languages"]
        if any(l in langs for l in ("python", "go", "rust", "java", "c", "cpp")):
            signals.setdefault("forms", set()).add("backend")
        elif any(l in langs for l in ("javascript", "typescript", "vue", "svelte", "html")):
            signals.setdefault("forms", set()).add("web")
        elif "shell" in langs:
            signals.setdefault("forms", set()).add("cli")
    elif not signals.get("languages") and not signals.get("forms"):
        ext_counts: dict[str, int] = {}
        for _, f_path, _, _ in files:
            ext = f_path.suffix.lower()
            if ext in LANGUAGES_BY_EXT:
                lang = LANGUAGES_BY_EXT[ext]
                ext_counts[lang] = ext_counts.get(lang, 0) + 1
        if ext_counts:
            primary_lang = max(ext_counts.items(), key=lambda x: x[1])[0]
            signals.setdefault("languages", set()).add(primary_lang)
            if primary_lang == "python":
                signals.setdefault("runtimes", set()).add("cpython")
            elif primary_lang in {"javascript", "typescript", "jsx", "tsx"}:
                signals.setdefault("runtimes", set()).add("nodejs")
            elif primary_lang in {"java", "kotlin"}:
                signals.setdefault("runtimes", set()).add("jvm")
            primary_form = "backend" if primary_lang in {"python", "go", "rust", "java", "c", "cpp"} else "web"
            signals.setdefault("forms", set()).add(primary_form)
            languages = sorted(signals.get("languages", set()))
    forms = sorted(signals.get("forms", set()))

    if not isolated_fixtures and allow_llm and os.environ.get("BUGX_PROFILE_NO_LLM") != "1":
        if len(forms) <= 1:
            arbitration_data = {
                "source": "mechanical_consensus",
                "excluded_fixtures": [],
                "real_forms": list(forms),
                "summary": (
                    "证据无冲突且无待剔除 fixture：机械共识核验通过"
                    + ("（零信号，generic）" if not forms else "")
                    + "，无需 AI 复核"
                ),
            }
        else:
            arbitrator_cls = _get_arbitrator_cls()
            arbitrator = arbitrator_cls(
                project_root, compute_build_fingerprint(project_root), persist=persist,
            )
            _fx, arbitrated_forms = arbitrator.arbitrate(set(), forms)
            arbitration_data = arbitrator.get_cached_arbitration()
            if arbitrated_forms and (arbitration_data or {}).get("source") != "heuristic_baseline":
                forms = sorted(set(forms) | {f for f in arbitrated_forms if f in FORMS})
    status = "partial" if truncated else ("complete" if forms or languages else "unknown")
    if evidence_list:
        evidence_list = evidence_list[:MAX_EVIDENCE]
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    source_fingerprint = "sha256:" + hashlib.sha256(canon(fingerprint_rows)).hexdigest()
    for item in evidence_list:
        item["observed_at"] = observed_at
        item["source_fingerprint"] = source_fingerprint

    active_detector_version = _get_detector_version()
    cache_key = f"{source_fingerprint}:{active_detector_version}:llm={int(bool(allow_llm))}"
    cache_token = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:24]
    cache_file = profile_cache_path(cache_token)
    if cache_key in PROFILE_MEM_CACHE:
        cached = copy.deepcopy(PROFILE_MEM_CACHE[cache_key])
        cached.pop("observed_at", None)
        cached["generated_at"] = time.time()
        return cached
    if cache_file is not None and private_profile_cache(cache_file):
        try:
            cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached_data, dict) and cached_data.get("source_fingerprint") == source_fingerprint and cached_data.get("detector_version") == str(active_detector_version):
                PROFILE_MEM_CACHE[cache_key] = copy.deepcopy(cached_data)
                cached_data.pop("observed_at", None)
                cached_data["generated_at"] = time.time()
                return cached_data
        except Exception:
            pass

    try:
        scanner = sca_module()()
        dependencies = scanner.collect_dependencies(project_root)
        components = []
        for dep in dependencies[:MAX_COMPONENTS]:
            resolved = dep.get("resolved_version")
            declared = dep.get("declared_version")
            components.append({
                "ecosystem": str(dep.get("ecosystem") or ""),
                "name": str(dep.get("package") or ""),
                "module": str(dep.get("module") or ""),
                "declared_version": declared,
                "resolved_version": resolved,
                "observed_version": None,
                "source_kind": str(dep.get("source_kind") or "manifest"),
                "source_file": str(dep.get("source_file") or ""),
                "dependency_kind": str(dep.get("dependency_kind") or "declared"),
                "evidence_refs": list(dep.get("evidence_refs") or []),
                "confidence": 0.95 if resolved else 0.7,
                "status": "confirmed" if resolved or declared else "unknown",
            })
        risk_signals = scanner.advisory_candidates(dependencies)[:MAX_COMPONENTS]
    except Exception as exc:
        components = []
        risk_signals = []
        unknowns.append({"kind": "sca", "reason": f"dependency parser unavailable: {type(exc).__name__}"})

    conflicts: list[dict[str, Any]] = []
    by_runtime: dict[str, set[str]] = {}
    for f_item in runtime_facts:
        name = str(f_item.get("name") or "")
        version = str(f_item.get("resolved_version") or f_item.get("declared_version") or "")
        if name and version:
            by_runtime.setdefault(name, set()).add(version)
    for name, versions in sorted(by_runtime.items()):
        if len(versions) > 1:
            conflicts.append({"kind": "runtime_version", "name": name, "versions": sorted(versions), "status": "conflict"})
    unknowns.extend(
        {"kind": str(f_item.get("kind") or "fact"), "name": f_item.get("name"), "source_file": f_item.get("source_file"), "reason": "version unavailable"}
        for f_item in runtime_facts + framework_facts
        if isinstance(f_item, Mapping) and f_item.get("status") == "unknown"
    )

    ca = code_analyzer_module()
    analysis = ca.analyze_codebase(project_root, files=files)

    primary_language = str((analysis.get("scale_estimation") or {}).get("primary_language") or "unknown")
    if primary_language == "unknown" and languages:
        primary_language = languages[0]
    profile: dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "detector_version": active_detector_version,
        "status": status,
        "forms": forms,
        "category": [value for value in ("web", "backend", "mobile", "embedded", "desktop", "cli", "library", "data", "infra") if value in forms] or ["unknown"],
        "languages": languages,
        "primary_language": primary_language,
        "runtimes": sorted(signals.get("runtimes", set())),
        "frameworks": sorted(signals.get("frameworks", set()))[:24],
        "surfaces": sorted(signals.get("surfaces", set())),
        "declared_targets": sorted(signals.get("declared_targets", set())),
        "declared_commands": sorted(set(commands))[:MAX_COMMANDS],
        "flows": evidence_list[:24],
        "evidence": evidence_list,
        "files_scanned": len(files),
        "scanned_bytes": scanned_bytes,
        "scan_truncated": truncated,
        "source_fingerprint": source_fingerprint,
        "code_metrics": analysis.get("summary", {}),
        "code_languages": analysis.get("languages", []),
        "code_modules": analysis.get("modules", []),
        "scale_estimation": analysis.get("scale_estimation", {}),
        "maintainability": analysis.get("maintainability", {}),
        "duplication": analysis.get("duplication", {}),
        "architecture_topology": analysis.get("architecture_topology", {}),
        "engineering_economics": analysis.get("engineering_economics", {}),
        "risk_metrics": analysis.get("risk_metrics", {}),
        "runtime_facts": runtime_facts[:MAX_FACTS],
        "framework_facts": framework_facts[:MAX_FACTS],
        "components": components,
        "build_targets": build_targets[:MAX_FACTS],
        "toolchains": toolchains[:MAX_FACTS],
        "risk_signals": risk_signals[:MAX_COMPONENTS],
        "unknowns": unknowns[:MAX_FACTS],
        "conflicts": conflicts[:MAX_FACTS],
    }
    if arbitration_data:
        profile["ai_profile_arbitration"] = arbitration_data
    profile["classification"] = classify_profile(profile)
    hash_input = hash_projection({
        k: v for k, v in profile.items()
        if k not in {"category", "files_scanned", "scanned_bytes", "scan_truncated", "profile_hash", "generated_at"}
    })
    profile["profile_hash"] = "sha256:" + hashlib.sha256(canon(hash_input)).hexdigest()
    profile["generated_at"] = time.time()
    PROFILE_MEM_CACHE[cache_key] = copy.deepcopy(profile)
    if cache_file is not None:
        try:
            secure_atomic.atomic_write_json(
                cache_file, profile, mode=0o600, indent=None,
            )
        except (OSError, ValueError):
            pass
    return profile


# Compatibility alias
_code_analyzer_module = code_analyzer_module
