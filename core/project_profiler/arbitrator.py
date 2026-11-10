#!/usr/bin/env python3
"""Heuristic classification and LLM-assisted project arbitration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import core.secure_atomic as secure_atomic
except ImportError:
    from .. import secure_atomic  # type: ignore

from .constants import (
    BUILD_FINGERPRINT_FILES,
    DEFAULT_BYTE_LIMIT,
    DEFAULT_FILE_LIMIT,
    FORMS,
    LANGUAGES_BY_EXT,
    MANIFEST_NAMES,
    SOURCE_EXTENSIONS_BY_ECOSYSTEM,
    TEST_PATH_SEGMENTS,
)
from .extractors import stable_files
from .fingerprint import read_bounded

_ENGINE_DIR = Path(__file__).resolve().parent.parent


class HeuristicProfileClassifier:
    """DOC-200: 基于文件系统拓扑的主根系锚定与从属固件孤立特征自判器。"""

    def __init__(self, root: str | os.PathLike[str], files: list[tuple[str, Path, int, str]] | None = None,
                 *, persist: bool = True) -> None:
        self.root = Path(root).resolve()
        self.persist = persist
        if files is None:
            files, _, _ = stable_files(self.root, file_limit=DEFAULT_FILE_LIMIT, byte_limit=DEFAULT_BYTE_LIMIT)
        self.files = files
        self.root_manifests = self._find_root_manifests()
        self.primary_ecosystem = self._determine_primary_ecosystem()

    def _find_root_manifests(self) -> set[str]:
        names = set()
        for rel, path, _, _ in self.files:
            if "/" not in rel and "\\" not in rel:
                cname = path.name.casefold()
                if cname in MANIFEST_NAMES:
                    names.add(cname)
        return names

    def _determine_primary_ecosystem(self) -> str:
        if "pyproject.toml" in self.root_manifests:
            return "python"
        if "go.mod" in self.root_manifests:
            return "go"
        if "cargo.toml" in self.root_manifests:
            return "rust"
        if "package.json" in self.root_manifests:
            return "node"
        if {"pom.xml", "build.gradle", "build.gradle.kts"} & self.root_manifests:
            return "jvm"
        if "pubspec.yaml" in self.root_manifests:
            return "flutter"
        return "unknown"

    def classify_isolated_fixtures(self) -> tuple[set[str], bool]:
        """识别仅包含辅助固件/测试样板但无伴生业务代码实现的孤立清单路径。"""
        excluded: set[str] = set()
        requires_recheck = False

        if not self.primary_ecosystem or self.primary_ecosystem == "unknown":
            return excluded, False

        dir_source_exts: dict[str, set[str]] = defaultdict(set)
        for rel, path, _, _ in self.files:
            ext = path.suffix.casefold()
            parent = str(Path(rel).parent)
            if ext in LANGUAGES_BY_EXT:
                dir_source_exts[parent].add(ext)

        _ALL_BUILD_NAMES = MANIFEST_NAMES | set(BUILD_FINGERPRINT_FILES) | {"podfile.lock"}
        _HTML_EXTS = {".html", ".htm", ".xhtml", ".vue", ".svelte"}

        for rel, path, _, _ in self.files:
            cname = path.name.casefold()
            if "/" not in rel and "\\" not in rel:
                continue

            parts = [p.casefold() for p in Path(rel).parts[:-1]]
            has_test_semantic = bool(set(parts) & TEST_PATH_SEGMENTS)

            is_build_manifest = cname in _ALL_BUILD_NAMES or path.suffix.casefold() in {".gradle", ".kts", ".lock"}
            if not is_build_manifest and path.suffix.casefold() not in _HTML_EXTS:
                continue

            conflict_form = None
            if cname in {"pubspec.yaml", "pubspec.lock", "project.pbxproj", "podfile", "podfile.lock"}:
                conflict_form = "mobile"
            elif cname in {"build.gradle", "build.gradle.kts"}:
                text = read_bounded(path).casefold()
                conflict_form = "mobile" if "com.android" in text else "jvm"
            elif cname in {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
                conflict_form = "node"
            elif path.suffix.casefold() in {".html", ".htm", ".xhtml", ".vue", ".svelte"}:
                conflict_form = "web"

            if conflict_form:
                rel_dir = str(Path(rel).parent)
                nearby_exts = set()
                for d, exts in dir_source_exts.items():
                    if d == rel_dir or d.startswith(rel_dir + "/"):
                        nearby_exts.update(exts)

                matching_code = nearby_exts & SOURCE_EXTENSIONS_BY_ECOSYSTEM.get(conflict_form, set())
                if has_test_semantic and not matching_code:
                    excluded.add(rel)
                elif not has_test_semantic and not matching_code and conflict_form != self.primary_ecosystem:
                    requires_recheck = True
                else:
                    pass

        return excluded, not requires_recheck


class LLMProfileArbitrator:
    """DOC-200: LLM 动态语义复核与冲突裁决器。"""

    def __init__(self, root: Path, fingerprint: str, *, persist: bool = True) -> None:
        self.root = root
        self.fingerprint = fingerprint
        self.persist = persist

    def _is_engine_dir(self) -> bool:
        try:
            return self.root.resolve() == _ENGINE_DIR
        except OSError:
            return True

    def _cache_path(self) -> Path:
        return self.root / ".bugx" / "state" / "profile-arbitration-cache.json"

    def get_cached_arbitration(self) -> dict[str, Any] | None:
        if self._is_engine_dir():
            return None
        cpath = self._cache_path()
        if not cpath.is_file():
            return None
        try:
            data = json.loads(cpath.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("build_fingerprint") == self.fingerprint:
                return data
        except Exception:
            pass
        return None

    def save_arbitration(self, data: dict[str, Any]) -> None:
        if not self.persist or self._is_engine_dir():
            return
        cpath = self._cache_path()
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(data)
            payload["build_fingerprint"] = self.fingerprint
            secure_atomic.atomic_write_json(cpath, payload)
        except Exception:
            pass

    def arbitrate(
        self,
        candidate_fixtures: set[str],
        heuristic_forms: list[str],
    ) -> tuple[set[str], list[str]]:
        """唤起 LLM 进行语义复核，超时或无环境时安全回退。"""
        if self._is_engine_dir():
            return candidate_fixtures, heuristic_forms

        if not self.persist:
            return candidate_fixtures, heuristic_forms

        cached = self.get_cached_arbitration()
        if cached:
            return set(cached.get("excluded_fixtures", [])), list(cached.get("real_forms", heuristic_forms))

        if os.environ.get("BUGX_OFFLINE") == "1" or os.environ.get("BUGX_PROFILE_NO_LLM") == "1":
            return candidate_fixtures, heuristic_forms

        result_excluded = set(candidate_fixtures)
        result_forms = list(heuristic_forms)
        arbitration_source = "heuristic_baseline"
        summary = ""

        rev_path = self.root / ".bugx" / "state" / "l1-semantic-review.json"
        if rev_path.is_file():
            try:
                rev_data = json.loads(rev_path.read_text(encoding="utf-8"))
                profile_verdict = rev_data.get("project_profile_verdict") or {}
                if profile_verdict.get("status") in ("verified", "needs_revision"):
                    arbitration_source = "l1_semantic_review"
                    summary = str(profile_verdict.get("summary") or "")
                    missed = profile_verdict.get("missed_components") or []
                    for m in missed:
                        m_low = str(m).lower()
                        for f in FORMS:
                            if f in m_low and f not in result_forms:
                                result_forms.append(f)
            except Exception:
                pass

        if arbitration_source == "heuristic_baseline":
            active_host = ""
            try:
                from core.host_runtime import host_registry
                active_host = host_registry.resolve_scout_host(self.root)
            except Exception:
                pass

            if active_host:
                try:
                    from core.host_runtime import host_registry
                    from core.autonomous_cve import attack_graph_store

                    timeout = float(os.environ.get("BUGX_RECON_TIMEOUT") or "180.0")
                    tmp_prompt = self.root / ".bugx" / "run" / "profile_arbitration_prompt.txt"
                    tmp_prompt.parent.mkdir(parents=True, exist_ok=True)

                    root_manifests = []
                    for p in self.root.glob("*"):
                        if p.is_file() and not p.name.startswith("."):
                            root_manifests.append(p.name)

                    prompt_payload = {
                        "candidate_fixtures": sorted(candidate_fixtures),
                        "heuristic_forms": heuristic_forms,
                        "root_manifests": root_manifests[:20],
                    }
                    content = (
                        "<system_rules>\n"
                        "You are BugX Project Profile Arbitrator.\n"
                        "Your mission: review candidate fixtures and determine true application forms for this project.\n"
                        "Forms must be selected from: ['backend', 'web', 'mobile', 'cli', 'desktop', 'embedded'].\n"
                        "Output STRICTLY a JSON object with this exact schema (no markdown, no other text):\n"
                        "{\n"
                        '  "excluded_fixtures": ["paths that are strictly fixtures or test samples"],\n'
                        '  "real_forms": ["verified forms"],\n'
                        '  "primary_framework": "optional framework name",\n'
                        '  "summary": "brief arbitration explanation"\n'
                        "}\n"
                        "</system_rules>\n"
                        "<untrusted_code_context>\n"
                        f"{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}\n"
                        "</untrusted_code_context>\n"
                    )
                    tmp_prompt.write_text(content, encoding="utf-8")
                    cmd = host_registry.build_recon_scout_command(active_host, self.root, tmp_prompt)
                    if not cmd:
                        return None

                    child_env = os.environ.copy()
                    child_env["BUGX_RECON_SCOUT_ACTIVE"] = "1"
                    try:
                        child_env.update(host_registry.build_recon_scout_env(active_host, self.root))
                    except Exception:
                        pass

                    res = subprocess.run(
                        cmd, cwd=self.root, capture_output=True, text=True,
                        timeout=timeout, env=child_env, check=False,
                    )
                    spurious_files = ("analysis.json", "profile_arbitration.json", "semantic_review.json")
                    file_parsed = None
                    for stray in spurious_files:
                        stray_path = self.root / stray
                        if stray_path.is_file():
                            try:
                                file_parsed = json.loads(stray_path.read_text(encoding="utf-8"))
                            except Exception:
                                pass
                            try:
                                stray_path.unlink()
                            except Exception:
                                pass
                            if isinstance(file_parsed, dict):
                                break

                    if res.returncode == 0 and (res.stdout or file_parsed):
                        parsed = file_parsed if isinstance(file_parsed, dict) else attack_graph_store.extract_resilient_json(res.stdout)
                        if isinstance(parsed, dict):
                            if "excluded_fixtures" in parsed and isinstance(parsed["excluded_fixtures"], list):
                                result_excluded = set(parsed["excluded_fixtures"])
                            if "real_forms" in parsed and isinstance(parsed["real_forms"], list):
                                valid_forms = [f for f in parsed["real_forms"] if f in FORMS]
                                if valid_forms:
                                    result_forms = valid_forms
                            arbitration_source = "ai_arbitrator"
                            summary = str(parsed.get("summary") or "")
                            print(f"  [ProfileArbitrator] ✓ AI 宿主 ({active_host}) 完成项目画像裁决: forms={result_forms}, 排除测试夹具={len(result_excluded)} 个", file=sys.stderr)
                except Exception as _exc:
                    print(f"  [ProfileArbitrator] AI 裁决异常或超时 ({type(_exc).__name__})，降级至启发式基线", file=sys.stderr)

        self.save_arbitration({
            "excluded_fixtures": sorted(result_excluded),
            "real_forms": result_forms,
            "source": arbitration_source,
            "summary": summary,
        })
        return result_excluded, result_forms
