#!/usr/bin/env python3
"""Signal extraction, facts gathering, and file traversal for project profiling."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .constants import (
    ARTIFACT_DIRS,
    CLI_MARKERS,
    CONTENT_SCAN_BYTES,
    CONTENT_SCAN_FILES,
    CONTENT_SUFFIXES,
    DETECTOR_VERSION_BASE,
    ENTRYPOINT_NAMES,
    FORMS,
    HTTP_MARKERS,
    LANGUAGES_BY_EXT,
    MANIFEST_NAMES,
    MAX_COMMANDS,
    MAX_FACTS,
    MAX_MANIFEST_BYTES,
    PY_CLI_DEPS,
    PY_FRAMEWORK_DEPS,
    PY_HTTP_DEPS,
    PY_QUEUE_DEPS,
    PY_STORAGE_DEPS,
    QUEUE_MARKERS,
    RUNTIMES,
    SENSITIVE,
    SKIP_DIRS,
    STORAGE_MARKERS,
    SURFACES,
    TARGETS,
    VERSION_FILES,
)
from .fingerprint import read_bounded, safe_relative

try:
    from core.retrieval import symbol_index as _symbol_index
except ImportError:
    try:
        from .. import symbol_index as _symbol_index
    except ImportError:  # pragma: no cover
        _symbol_index = None


def hash_projection(value: Any) -> Any:
    """Remove observation timestamps from the deterministic profile hash."""
    if isinstance(value, Mapping):
        return {
            key: hash_projection(item)
            for key, item in value.items()
            if key not in {"generated_at", "observed_at"}
        }
    if isinstance(value, list):
        return [hash_projection(item) for item in value]
    return value


def manifest_data(path: Path) -> Mapping[str, Any]:
    text = read_bounded(path)
    if not text or SENSITIVE.search(text):
        return {}
    try:
        if path.name.casefold() == "package.json":
            value = json.loads(text)
            return value if isinstance(value, Mapping) else {}
        if path.name.casefold() == "pyproject.toml":
            try:
                import tomllib
            except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
                try:
                    import tomli as tomllib  # type: ignore[no-redef]
                except ModuleNotFoundError:
                    return {}
            value = tomllib.loads(text)
            return value if isinstance(value, Mapping) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}
    return {}


def version_value(value: Any) -> str | None:
    """Extract a bounded version token without pretending ranges are resolved."""
    raw = str(value or "").strip().strip("'\"")
    if not raw or raw in {"*", "latest", "unknown"}:
        return None
    match = re.search(r"(?:\d+)(?:\.\d+){0,3}(?:[-+][0-9A-Za-z.-]+)?", raw)
    return match.group(0) if match else None


def add_fact(target: list[dict[str, Any]], fact_item: Mapping[str, Any], *, limit: int = MAX_FACTS) -> None:
    """Append a deterministic fact once, keeping evidence bounded."""
    if len(target) >= limit:
        return
    item = dict(fact_item)
    item.setdefault("declared_version", None)
    item.setdefault("resolved_version", None)
    item.setdefault("observed_version", None)
    item.setdefault("vendor", None)
    item.setdefault("confidence", 0.0)
    item.setdefault("status", "unknown")
    item["confidence"] = round(max(0.0, min(1.0, float(item.get("confidence") or 0.0))), 2)
    key = (
        str(item.get("kind") or ""),
        str(item.get("name") or item.get("language") or ""),
        str(item.get("source_file") or ""),
        str(item.get("declared_version") or item.get("resolved_version") or ""),
    )
    for old in target:
        old_key = (
            str(old.get("kind") or ""),
            str(old.get("name") or old.get("language") or ""),
            str(old.get("source_file") or ""),
            str(old.get("declared_version") or old.get("resolved_version") or ""),
        )
        if old_key == key:
            return
    target.append(item)


def fact(kind: str, *, name: str | None = None, language: str | None = None,
         ecosystem: str | None = None, declared_version: Any = None,
         resolved_version: Any = None, source_kind: str, source_file: str,
         evidence_refs: Iterable[str], confidence: float, status: str = "confirmed",
         vendor: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "language": language,
        "ecosystem": ecosystem,
        "declared_version": version_value(declared_version),
        "resolved_version": version_value(resolved_version),
        "observed_version": None,
        "vendor": vendor,
        "source_kind": source_kind,
        "source_file": source_file,
        "evidence_refs": sorted(set(str(x) for x in evidence_refs if x)),
        "confidence": confidence,
        "status": status,
        **extra,
    }


def version_line_fact(rel: str, path: Path, *, kind: str, ecosystem: str, language: str, text: str, name: str | None = None) -> dict[str, Any] | None:
    value = version_value(text.splitlines()[0] if text.splitlines() else text)
    if not value:
        return None
    return fact(
        kind, name=name or language, language=language, ecosystem=ecosystem,
        declared_version=value, source_kind="toolchain", source_file=rel,
        evidence_refs=[f"{rel}:1"], confidence=0.95,
    )


def sca_module():
    try:
        from core.probes.sca_scanner import SCAScanner
    except ImportError:
        try:
            from ..sca_scanner import SCAScanner  # type: ignore
        except ImportError:
            from core.probes.sca_scanner import SCAScanner
    return SCAScanner


def add_signal(signals: dict[str, set[str]], key: str, value: str) -> None:
    if value and value != "unknown":
        signals.setdefault(key, set()).add(value)


def evidence(ref: str, kind: str, signals: Iterable[str], *, source_kind: str = "detected", confidence: float = 0.8) -> dict[str, Any]:
    return {
        "ref": ref,
        "evidence_refs": [ref],
        "kind": kind,
        "signals": sorted(set(str(x) for x in signals if x)),
        "source_kind": source_kind,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
    }


def is_manifest_name(name: str) -> bool:
    """是否为需要解析的清单（含 ``requirements*.txt`` / ``Pipfile`` 这类通配/变体）。"""
    lowered = name.casefold()
    if lowered in MANIFEST_NAMES:
        return True
    if lowered == "pipfile":
        return True
    return lowered.startswith("requirements") and lowered.endswith(".txt")


def python_dependency_signals(deps: set[str], signals: dict[str, set[str]]) -> None:
    """Python 依赖 → 形态/界面信号（requirements.txt 与 pyproject 共用同一套规则）。"""
    def has(name: str) -> bool:
        return any(dep == name or dep.startswith(name + "-") or dep.startswith(name + "_")
                   for dep in deps)
    for dep, framework in PY_FRAMEWORK_DEPS:
        if has(dep):
            add_signal(signals, "frameworks", framework)
    if any(has(dep) for dep in PY_HTTP_DEPS):
        add_signal(signals, "forms", "backend")
        add_signal(signals, "surfaces", "rest")
    if any(has(dep) for dep in PY_STORAGE_DEPS):
        add_signal(signals, "surfaces", "storage")
    if any(has(dep) for dep in PY_QUEUE_DEPS):
        add_signal(signals, "surfaces", "queue")
    if (any(has(dep) for dep in PY_CLI_DEPS)
            and not ({"web", "backend"} & signals.get("forms", set()))):
        add_signal(signals, "forms", "cli")


def requirement_names(path: Path) -> set[str]:
    """从 ``requirements*.txt`` 抽取依赖名（PEP 508 前缀；跳过选项行与注释）。"""
    names: set[str] = set()
    for raw in read_bounded(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")):
            continue
        token = re.split(r"[<>=!~;\[\]()\s]", line, maxsplit=1)[0].strip().casefold()
        if token:
            names.add(token)
    return names


def content_signals(path: Path, size: int, signals: dict[str, set[str]]) -> None:
    """源码内容信号（**有界**扫描；供依赖清单/文件名无法覆盖的形态识别）。"""
    if size > CONTENT_SCAN_BYTES or path.suffix.casefold() not in CONTENT_SUFFIXES:
        return
    text = read_bounded(path).casefold()
    if not text:
        return
    if any(marker in text for marker in HTTP_MARKERS):
        add_signal(signals, "forms", "backend")
        add_signal(signals, "surfaces", "rest")
    if any(marker in text for marker in STORAGE_MARKERS):
        add_signal(signals, "surfaces", "storage")
    if any(marker in text for marker in QUEUE_MARKERS):
        add_signal(signals, "surfaces", "queue")
    if (
        any(marker in text for marker in CLI_MARKERS)
        and not ({"web", "backend"} & signals.get("forms", set()))
    ):
        add_signal(signals, "forms", "cli")
    elif (
        path.name.casefold() in ENTRYPOINT_NAMES
        and not ({"web", "backend"} & signals.get("forms", set()))
    ):
        add_signal(signals, "forms", "cli")


def extract_manifest_signals(rel: str, path: Path, signals: dict[str, set[str]], commands: list[str], ev_list: list[dict[str, Any]]) -> None:
    name = path.name.casefold()
    data = manifest_data(path)
    deps: set[str] = set()
    if name == "package.json":
        add_signal(signals, "languages", "javascript")
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            value = data.get(key, {}) if isinstance(data, Mapping) else {}
            if isinstance(value, Mapping):
                deps.update(str(k).casefold() for k in value)
        scripts = data.get("scripts", {}) if isinstance(data, Mapping) else {}
        if isinstance(scripts, Mapping):
            commands.extend(f"npm:{str(k)[:60]}" for k in scripts if len(commands) < MAX_COMMANDS)
        if any(x in deps for x in ("react", "react-dom", "vue", "svelte", "vite", "next", "nuxt", "@angular/core")):
            add_signal(signals, "forms", "web")
            add_signal(signals, "surfaces", "ui")
        if any(x in deps for x in ("electron", "electron-builder", "@tauri-apps/api", "@tauri-apps/cli")):
            add_signal(signals, "forms", "desktop")
            add_signal(signals, "surfaces", "ui")
        if any(x in deps for x in ("express", "koa", "fastify", "nestjs", "hono", "@grpc/grpc-js", "graphql")):
            add_signal(signals, "forms", "backend")
        if "typescript" in deps or any(dep.endswith((".ts", ".tsx")) for dep in deps):
            add_signal(signals, "languages", "typescript")
    elif name == "pyproject.toml":
        add_signal(signals, "languages", "python")
        project = data.get("project", {}) if isinstance(data, Mapping) else {}
        if isinstance(project, Mapping):
            raw_deps = project.get("dependencies", [])
            if isinstance(raw_deps, list):
                for dep in raw_deps:
                    token = re.split(r"[<>=!~;\[\]\s]", str(dep), maxsplit=1)[0].strip().casefold()
                    if token:
                        deps.add(token)
            scripts = project.get("scripts", {})
            if isinstance(scripts, Mapping):
                commands.extend(f"python:{str(k)[:60]}" for k in scripts if len(commands) < MAX_COMMANDS)
        add_signal(signals, "runtimes", "cpython")
        python_dependency_signals(deps, signals)
    elif name == "pipfile":
        add_signal(signals, "languages", "python")
        packages: Mapping[str, Any] = {}
        text = read_bounded(path)
        if text:
            try:
                import tomllib
            except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
                tomllib = None  # type: ignore[assignment]
            if tomllib is not None:
                try:
                    packages = tomllib.loads(text)
                except (ValueError, TypeError):
                    packages = {}
        for section in ("packages", "dev-packages"):
            values = packages.get(section, {}) if isinstance(packages, Mapping) else {}
            if isinstance(values, Mapping):
                deps.update(str(key).casefold() for key in values)
        add_signal(signals, "runtimes", "cpython")
        python_dependency_signals(deps, signals)
    elif name.startswith("requirements") and name.endswith(".txt"):
        deps.update(requirement_names(path))
        add_signal(signals, "runtimes", "cpython")
        python_dependency_signals(deps, signals)
    elif name == "go.mod":
        add_signal(signals, "languages", "go")
        add_signal(signals, "runtimes", "embedded-toolchain" if "tinygo" in read_bounded(path).casefold() else "")
        text = read_bounded(path).casefold()
        for needle, framework in (("gin-gonic/gin", "gin"), ("labstack/echo", "echo"), ("gofiber/fiber", "fiber"), ("google.golang.org/grpc", "grpc"), ("graphql-go", "graphql")):
            if needle in text:
                add_signal(signals, "frameworks", framework)
        if any(x in text for x in ("gin-gonic/gin", "labstack/echo", "gofiber/fiber")):
            add_signal(signals, "forms", "backend")
        commands.append("go:test")
    elif name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
        text = read_bounded(path).casefold()
        add_signal(signals, "languages", "kotlin" if "kotlin" in text else "java")
        add_signal(signals, "forms", "mobile" if "com.android" in text else "backend")
        add_signal(signals, "runtimes", "jvm")
        commands.append("gradle:test")
    elif name == "podfile":
        add_signal(signals, "forms", "mobile")
        add_signal(signals, "targets", "ios")
        commands.append("xcode:test")
    elif name == "cargo.toml":
        add_signal(signals, "languages", "rust")
        add_signal(signals, "runtimes", "embedded-toolchain" if "embassy" in read_bounded(path).casefold() else "")
        commands.append("cargo:test")
    elif name in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}:
        add_signal(signals, "forms", "infra")
        add_signal(signals, "targets", "container")
        add_signal(signals, "surfaces", "build")
    elif name in {"platformio.ini", "cmakelists.txt"}:
        add_signal(signals, "forms", "embedded")
        add_signal(signals, "surfaces", "native")
        add_signal(signals, "runtimes", "embedded-toolchain")
        add_signal(signals, "targets", "mcu")
        commands.append("embedded:build")
    elif name in {"terraform.tf", "main.tf"}:
        add_signal(signals, "forms", "infra")
        add_signal(signals, "surfaces", "ci-cd")
    ev_list.append(evidence(rel, "manifest", sorted(set(deps))[:12] or [name], confidence=0.9))


def _get_rule_attr(name: str, default: Any) -> Any:
    # 只查真实模块名；前两个扁平名在归档后永远为 None（补丁打空的根因）。
    for mod_name in ("core.runtime.project_profile", "core.project_profiler.constants"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, name):
            return getattr(mod, name)
    return default


def detector_rules_digest() -> str:
    """探查规则摘要（10 hex）：任何**影响分类的规则**变更都会改变它。"""
    forms = _get_rule_attr("FORMS", FORMS)
    surfaces = _get_rule_attr("SURFACES", SURFACES)
    runtimes = _get_rule_attr("RUNTIMES", RUNTIMES)
    targets = _get_rule_attr("TARGETS", TARGETS)
    manifest_names = _get_rule_attr("MANIFEST_NAMES", MANIFEST_NAMES)
    languages = _get_rule_attr("LANGUAGES_BY_EXT", LANGUAGES_BY_EXT)
    py_http = _get_rule_attr("PY_HTTP_DEPS", PY_HTTP_DEPS)
    py_storage = _get_rule_attr("PY_STORAGE_DEPS", PY_STORAGE_DEPS)
    py_queue = _get_rule_attr("PY_QUEUE_DEPS", PY_QUEUE_DEPS)
    py_cli = _get_rule_attr("PY_CLI_DEPS", PY_CLI_DEPS)
    py_framework = _get_rule_attr("PY_FRAMEWORK_DEPS", PY_FRAMEWORK_DEPS)
    http_markers = _get_rule_attr("HTTP_MARKERS", HTTP_MARKERS)
    storage_markers = _get_rule_attr("STORAGE_MARKERS", STORAGE_MARKERS)
    queue_markers = _get_rule_attr("QUEUE_MARKERS", QUEUE_MARKERS)
    cli_markers = _get_rule_attr("CLI_MARKERS", CLI_MARKERS)
    content_suffixes = _get_rule_attr("CONTENT_SUFFIXES", CONTENT_SUFFIXES)
    scan_files = _get_rule_attr("CONTENT_SCAN_FILES", CONTENT_SCAN_FILES)
    scan_bytes = _get_rule_attr("CONTENT_SCAN_BYTES", CONTENT_SCAN_BYTES)

    payload = "|".join((
        repr(forms), repr(surfaces), repr(runtimes), repr(targets),
        repr(sorted(manifest_names)), repr(sorted(languages.items())),
        repr(py_http), repr(py_storage), repr(py_queue),
        repr(py_cli), repr(py_framework),
        repr(http_markers), repr(storage_markers), repr(queue_markers),
        repr(cli_markers), repr(sorted(content_suffixes)),
        str(scan_files), str(scan_bytes),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


def detector_source_digest() -> str:
    """检测器**源码**摘要（8 hex）：规则表之外的控制流变更也必须让缓存失效。"""
    try:
        data = Path(__file__).read_bytes()
    except OSError:
        return "nosrc"
    return hashlib.sha256(data).hexdigest()[:8]


DETECTOR_VERSION = f"{DETECTOR_VERSION_BASE}+{detector_rules_digest()}+{detector_source_digest()}"


def find_ipa_marker(root: Path) -> bool:
    """在常见产物目录**浅层**查找 `.ipa`。"""
    try:
        if any(root.glob("*.ipa")):
            return True
        for name in ARTIFACT_DIRS:
            candidate = root / name
            if candidate.is_dir() and any(candidate.glob("*.ipa")):
                return True
    except OSError:
        return False
    return False


def add_path_signals(rel: str, name: str, signals: dict[str, set[str]], ev_list: list[dict[str, Any]]) -> None:
    low = f"{rel}/{name}".casefold()
    ext = Path(name).suffix.casefold()
    language = LANGUAGES_BY_EXT.get(ext)
    if language:
        add_signal(signals, "languages", language)
    if any(x in low for x in ("route", "router", "controller", "handler", "api/", "endpoint")):
        add_signal(signals, "surfaces", "rest")
    if "graphql" in low:
        add_signal(signals, "surfaces", "graphql")
    if ext == ".proto":
        add_signal(signals, "surfaces", "grpc")
    if any(x in low for x in ("auth", "login", "oauth", "jwt", "permission")) and ext not in {".md", ".txt", ".rst"}:
        add_signal(signals, "surfaces", "auth")
    if any(x in low for x in ("migration", "model", "database", "storage", "repository")) and ext not in {".md", ".txt", ".rst"}:
        add_signal(signals, "surfaces", "storage")
    if any(x in low for x in ("upload", "download", "attachment", "file")) and ext not in {".md", ".txt", ".rst"}:
        add_signal(signals, "surfaces", "file")
    if any(x in low for x in ("kafka", "rabbit", "celery", "queue", "redis", "worker")) and ext not in {".md", ".txt", ".rst"}:
        add_signal(signals, "surfaces", "queue")
    is_test_path = any(x in low for x in ("test/", "tests/", "fixture", "fixtures/", "testdata/", "mock/", "sample/", "example/"))
    if ext in {".vue", ".jsx", ".tsx", ".svelte"}:
        if not is_test_path:
            add_signal(signals, "forms", "web")
        add_signal(signals, "surfaces", "ui")
    elif ext in {".html", ".htm"}:
        if not is_test_path:
            add_signal(signals, "forms", "web")
            add_signal(signals, "surfaces", "ui")
    elif ext in {".css", ".scss", ".sass", ".less"}:
        add_signal(signals, "surfaces", "ui")
    if ext in {".swift", ".kt", ".kts", ".dart"} and any(x in low for x in ("app/", "android/", "ios/", "view", "screen")):
        if not is_test_path:
            add_signal(signals, "forms", "mobile")
            add_signal(signals, "surfaces", "ui")
    if name.casefold() in ENTRYPOINT_NAMES:
        ev_list.append(evidence(rel, "entrypoint-candidate", [], confidence=0.30))
    if any(x in low for x in ("cli", "command", "commands", "/bin/")):
        add_signal(signals, "forms", "cli")
    if any(x in low for x in ("notebook", ".ipynb", "dbt", "airflow", "spark")):
        add_signal(signals, "forms", "data")
    if any(x in low for x in ("firmware", "embedded", "zephyr", "freertos", "arduino", "stm32", "esp32")):
        add_signal(signals, "forms", "embedded")
        add_signal(signals, "surfaces", "native")


def extract_version_facts(
    files: list[tuple[str, Path, int, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract versioned runtime/framework/build/target facts from project files."""
    runtime_facts: list[dict[str, Any]] = []
    framework_facts: list[dict[str, Any]] = []
    build_targets: list[dict[str, Any]] = []
    toolchains: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []

    def add_target(name: str, rel: str, *, platform: str, ev_ref: str, **extra: Any) -> None:
        add_fact(build_targets, fact(
            "build_target", name=name, ecosystem=platform, source_kind="build_file",
            source_file=rel, evidence_refs=[ev_ref], confidence=0.9, platform=platform, **extra,
        ))

    def add_framework(name: str, version: Any, rel: str, line: int, *, ecosystem: str) -> None:
        add_fact(framework_facts, fact(
            "framework", name=name, ecosystem=ecosystem, declared_version=version,
            source_kind="manifest", source_file=rel, evidence_refs=[f"{rel}:{line}"], confidence=0.9,
            status="confirmed" if version_value(version) else "unknown",
        ))

    def add_toolchain(name: str, version: Any, rel: str, line: int, *, ecosystem: str) -> None:
        add_fact(toolchains, fact(
            "build_tool", name=name, ecosystem=ecosystem, declared_version=version,
            source_kind="manifest", source_file=rel, evidence_refs=[f"{rel}:{line}"], confidence=0.9,
            status="confirmed" if version_value(version) else "unknown",
        ))

    for rel, path, _size, _digest in files:
        name = path.name.casefold()
        text = read_bounded(path)
        low = text.casefold()
        lines = text.splitlines()

        if name in VERSION_FILES:
            ecosystem, language = VERSION_FILES[name]
            fact_item = version_line_fact(rel, path, kind="runtime", ecosystem=ecosystem, language=language, name=ecosystem, text=text)
            if fact_item:
                add_fact(runtime_facts, fact_item)
            continue

        if name == "package.json":
            data = manifest_data(path)
            deps: dict[str, Any] = {}
            for group in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "optionalDependencies"):
                value = data.get(group, {}) if isinstance(data, Mapping) else {}
                if isinstance(value, Mapping):
                    deps.update({str(k).casefold(): v for k, v in value.items()})
            engines = data.get("engines", {}) if isinstance(data, Mapping) else {}
            node_version = engines.get("node") if isinstance(engines, Mapping) else None
            if node_version:
                add_fact(runtime_facts, fact(
                    "runtime", name="nodejs", language="javascript", ecosystem="node",
                    declared_version=node_version, source_kind="manifest", source_file=rel,
                    evidence_refs=[f"{rel}:engines.node"], confidence=0.95,
                    status="confirmed" if version_value(node_version) else "unknown",
                ))
            elif deps:
                add_fact(runtime_facts, fact(
                    "runtime", name="nodejs", language="javascript", ecosystem="node",
                    source_kind="manifest", source_file=rel, evidence_refs=[f"{rel}:dependencies"],
                    confidence=0.7, status="inferred",
                ))
            framework_map = {
                "react": "react", "react-dom": "react", "vue": "vue", "@angular/core": "angular",
                "svelte": "svelte", "next": "nextjs", "nuxt": "nuxt", "express": "express",
                "koa": "koa", "fastify": "fastify", "@nestjs/core": "nestjs", "hono": "hono",
                "electron": "electron", "@tauri-apps/api": "tauri", "react-native": "react-native",
                "@ionic/core": "ionic", "vite": "vite", "webpack": "webpack", "rollup": "rollup",
                "esbuild": "esbuild", "rspack": "rspack", "parcel": "parcel", "@babel/core": "babel",
                "@swc/core": "swc", "eslint": "eslint", "prettier": "prettier", "playwright": "playwright",
                "jest": "jest", "vitest": "vitest",
            }
            build_tools = {"typescript", "vite", "webpack", "rollup", "esbuild", "rspack", "parcel", "babel", "swc", "eslint", "prettier", "playwright", "jest", "vitest"}
            for dependency, framework in framework_map.items():
                if dependency in deps:
                    if framework in build_tools:
                        add_toolchain(framework, deps[dependency], rel, 1, ecosystem="node")
                    else:
                        add_framework(framework, deps[dependency], rel, 1, ecosystem="node")
            if any(x in deps for x in ("electron", "@tauri-apps/api")):
                add_target("desktop", rel, platform="desktop", ev_ref=f"{rel}:dependencies", framework=framework_map.get(next(x for x in ("electron", "@tauri-apps/api") if x in deps)))
            if any(x in deps for x in ("react-native", "@ionic/core")):
                mobile_framework = framework_map.get(next(x for x in ("react-native", "@ionic/core") if x in deps))
                add_target("android", rel, platform="android", ev_ref=f"{rel}:dependencies", framework=mobile_framework)
                add_target("ios", rel, platform="ios", ev_ref=f"{rel}:dependencies", framework=mobile_framework)
            if "electron" in deps:
                add_target("windows-macos-linux", rel, platform="desktop", ev_ref=f"{rel}:electron", runtime="nodejs")
            if "@tauri-apps/api" in deps:
                add_target("windows-macos-linux", rel, platform="desktop", ev_ref=f"{rel}:@tauri-apps/api", runtime="rust+webview")
            continue

        if name == "pyproject.toml":
            data = manifest_data(path)
            project = data.get("project", {}) if isinstance(data, Mapping) else {}
            requires = project.get("requires-python") if isinstance(project, Mapping) else None
            add_fact(runtime_facts, fact(
                "runtime", name="cpython", language="python", ecosystem="python",
                declared_version=requires, source_kind="manifest", source_file=rel,
                evidence_refs=[f"{rel}:project.requires-python"], confidence=0.95 if requires else 0.7,
                status="confirmed" if version_value(requires) else "unknown",
            ))
            deps = project.get("dependencies", []) if isinstance(project, Mapping) else []
            dep_names = " ".join(str(x).casefold() for x in deps if isinstance(x, str))
            py_frameworks = {"django": "django", "flask": "flask", "fastapi": "fastapi", "sqlalchemy": "sqlalchemy", "celery": "celery", "pydantic": "pydantic"}
            for dependency, framework in py_frameworks.items():
                if dependency in dep_names:
                    match = re.search(rf"{re.escape(dependency)}\s*(?:[<>=~!]+\s*)?([0-9][^,; ]*)?", dep_names)
                    add_framework(framework, match.group(1) if match else None, rel, 1, ecosystem="pypi")
            build_system = data.get("build-system", {}) if isinstance(data, Mapping) else {}
            requires_build = build_system.get("requires", []) if isinstance(build_system, Mapping) else []
            if any("poetry" in str(x).casefold() for x in requires_build):
                add_framework("poetry", None, rel, 1, ecosystem="pypi")
            continue

        if name == "go.mod":
            go_match = re.search(r"(?m)^\s*go\s+([0-9][^\s]+)", text)
            tool_match = re.search(r"(?m)^\s*toolchain\s+(go[0-9][^\s]+)", text)
            go_decl_re = re.compile(r"\s*go\s+")
            add_fact(runtime_facts, fact(
                "runtime", name="go", language="go", ecosystem="go",
                declared_version=go_match.group(1) if go_match else None,
                resolved_version=tool_match.group(1) if tool_match else None,
                source_kind="build_file", source_file=rel,
                evidence_refs=[f"{rel}:{next((i for i, line in enumerate(lines, 1) if go_decl_re.match(line)), 1)}"],
                confidence=0.95 if go_match else 0.7,
                status="confirmed" if go_match or tool_match else "unknown",
            ))
            for needle, framework in (("gin-gonic/gin", "gin"), ("labstack/echo", "echo"), ("gofiber/fiber", "fiber"), ("google.golang.org/grpc", "grpc")):
                if needle in low:
                    add_framework(framework, None, rel, 1, ecosystem="go")
            continue

        if name in {"pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
            jvm_match = re.search(r"(?:java\.version|sourceCompatibility|targetCompatibility|jvmToolchain|<maven.compiler.(?:source|target|release)>)\s*[=:> ]+['\"]?([0-9]+(?:\.[0-9]+)?)", text, re.I)
            jvm_match = jvm_match or re.search(r"VERSION_(\d+)", text, re.I)
            add_fact(runtime_facts, fact(
                "runtime", name="jvm", language="java", ecosystem="jvm",
                declared_version=jvm_match.group(1) if jvm_match else None,
                source_kind="build_file", source_file=rel, evidence_refs=[f"{rel}:1"],
                confidence=0.9 if jvm_match else 0.65, status="confirmed" if jvm_match else "unknown",
            ))
            framework_patterns = {
                "spring-boot": r"spring-boot[^\n\"']*?[=:> ]\s*([0-9][^\s\"'<,)]+)?",
                "micronaut": r"micronaut[^\n\"']*?[=:> ]\s*([0-9][^\s\"'<,)]+)?",
                "quarkus": r"quarkus[^\n\"']*?[=:> ]\s*([0-9][^\s\"'<,)]+)?",
                "kotlin": r"kotlin[^\n\"']*?[=:> ]\s*([0-9][^\s\"'<,)]+)?",
            }
            for framework, pattern in framework_patterns.items():
                match = re.search(pattern, low, re.I)
                if match:
                    add_framework(framework, match.group(1) if match.lastindex else None, rel, 1, ecosystem="jvm")
            if "com.android" in low or "android.application" in low:
                add_target("android", rel, platform="android", ev_ref=f"{rel}:android-plugin", compile_sdk=(re.search(r"compileSdk(?:Version)?\s*[=: ]\s*(\d+)", text, re.I) or [None, None])[1], min_sdk=(re.search(r"minSdk(?:Version)?\s*[=: ]\s*(\d+)", text, re.I) or [None, None])[1], target_sdk=(re.search(r"targetSdk(?:Version)?\s*[=: ]\s*(\d+)", text, re.I) or [None, None])[1])
            continue

        if name in {"cargo.toml", "rust-toolchain", "rust-toolchain.toml"}:
            add_fact(runtime_facts, fact(
                "runtime", name="rust", language="rust", ecosystem="cargo",
                declared_version=version_value(text), source_kind="toolchain" if "toolchain" in name else "build_file",
                source_file=rel, evidence_refs=[f"{rel}:1"], confidence=0.8,
                status="confirmed" if version_value(text) else "unknown",
            ))
            if "tauri" in low:
                add_framework("tauri", None, rel, 1, ecosystem="cargo")
                add_target("desktop", rel, platform="desktop", ev_ref=f"{rel}:tauri")
            continue

        if name in {"global.json"}:
            sdk = re.search(r"\"version\"\s*:\s*\"([^\"]+)\"", text)
            add_fact(runtime_facts, fact(
                "runtime", name="dotnet", language="csharp", ecosystem="nuget",
                declared_version=sdk.group(1) if sdk else None, source_kind="toolchain", source_file=rel,
                evidence_refs=[f"{rel}:version"], confidence=0.95 if sdk else 0.7,
                status="confirmed" if sdk else "unknown",
            ))
            continue

        if name.endswith((".csproj", ".fsproj", ".vbproj")):
            target_match = re.search(r"<TargetFrameworks?>\s*([^<]+)", text, re.I)
            target = target_match.group(1).strip() if target_match else None
            add_fact(runtime_facts, fact(
                "runtime", name="dotnet", language="csharp", ecosystem="nuget",
                declared_version=target, source_kind="build_file", source_file=rel,
                evidence_refs=[f"{rel}:TargetFramework"], confidence=0.95 if target else 0.7,
                status="confirmed" if target else "unknown",
            ))
            if target and any(x in target.casefold() for x in ("android", "ios", "maccatalyst", "maui")):
                add_target("mobile", rel, platform="mobile", ev_ref=f"{rel}:TargetFramework", target_framework=target)
            elif any(x in low for x in ("usewpf", "usewindowsforms", "microsoft.net.sdk.windowsdesktop")):
                add_target("desktop", rel, platform="desktop", ev_ref=f"{rel}:desktop-sdk", target_framework=target)
            continue

        if name == "pubspec.yaml":
            dart_match = re.search(r"(?m)^\s*sdk:\s*['\"]?([^'\"\s]+)", text)
            add_fact(runtime_facts, fact(
                "runtime", name="dart", language="dart", ecosystem="pub",
                declared_version=dart_match.group(1) if dart_match else None,
                source_kind="manifest", source_file=rel, evidence_refs=[f"{rel}:environment.sdk"],
                confidence=0.95 if dart_match else 0.7, status="confirmed" if dart_match else "unknown",
            ))
            if "flutter:" in low:
                add_framework("flutter", None, rel, 1, ecosystem="pub")
                add_target("android", rel, platform="android", ev_ref=f"{rel}:flutter", framework="flutter")
                add_target("ios", rel, platform="ios", ev_ref=f"{rel}:flutter", framework="flutter")
            continue

        if name in {"podfile", "podfile.lock", "project.pbxproj"} or path.suffix.casefold() == ".xcodeproj":
            add_fact(runtime_facts, fact(
                "toolchain", name="xcode", ecosystem="apple", source_kind="build_file",
                source_file=rel, evidence_refs=[f"{rel}:1"], confidence=0.75,
            ))
            add_target("ios", rel, platform="ios", ev_ref=f"{rel}:ios-project")
            if "swiftui" in low:
                add_framework("swiftui", None, rel, 1, ecosystem="apple")

        if name in {"cmakelists.txt", "*.pro"} or path.suffix.casefold() in {".pro", ".sln"}:
            add_fact(toolchains, fact(
                "build_tool", name="cmake" if name == "cmakelists.txt" else "native-build",
                ecosystem="native", source_kind="build_file", source_file=rel,
                evidence_refs=[f"{rel}:1"], confidence=0.85,
            ))
            if any(x in low for x in ("qt", "wxwidgets", "wpf", "winui")):
                add_target("desktop", rel, platform="desktop", ev_ref=f"{rel}:desktop-framework")

    return runtime_facts, framework_facts, build_targets, toolchains, unknowns


def stable_files(root: Path, *, file_limit: int, byte_limit: int) -> tuple[list[tuple[str, Path, int, str]], bool, int]:
    result: list[tuple[str, Path, int, str]] = []
    total_bytes = 0
    truncated = False
    if not root.is_dir():
        return result, True, 0

    def mark_walk_error(_error: OSError) -> None:
        nonlocal truncated
        truncated = True

    gitignore_rules: list[str] | None = None

    def _scope_excluded(relative: Path, *, is_dir: bool) -> bool:
        if _symbol_index is None:
            return False
        nonlocal gitignore_rules
        if gitignore_rules is None:
            try:
                gitignore_rules = _symbol_index._gitignore_rules(root)
            except (OSError, ValueError, RuntimeError):
                gitignore_rules = []
        try:
            if _symbol_index._directory_is_excluded(
                relative, include_test_assets=True, gitignore_rules=gitignore_rules,
                is_dir=is_dir,
            ):
                return True
            if not is_dir and _symbol_index._is_engine_plugin_package(relative, root):
                return True
        except Exception:
            return False
        return False

    for dirpath, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=mark_walk_error,
    ):
        try:
            relative_dir = Path(dirpath).relative_to(root)
        except ValueError:
            relative_dir = Path(".")
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS
            and not (Path(dirpath) / d).is_symlink()
            and not _scope_excluded(relative_dir / d, is_dir=True)
        )
        for name in sorted(filenames):
            if len(result) >= file_limit:
                truncated = True
                break
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = safe_relative(root, path)
            if rel is None:
                continue
            if total_bytes >= byte_limit:
                truncated = True
                break
            if _scope_excluded(rel, is_dir=False):
                continue
            try:
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    continue
                size = int(before.st_size)
                sample_size = min(size, 1024 * 1024)
                if total_bytes + sample_size > byte_limit:
                    truncated = True
                    break
                with path.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    before_signature = (
                        before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns, before.st_ctime_ns,
                    )
                    opened_signature = (
                        opened.st_dev, opened.st_ino, opened.st_size,
                        opened.st_mtime_ns, opened.st_ctime_ns,
                    )
                    if before_signature != opened_signature:
                        truncated = True
                        continue
                    sample = stream.read(sample_size)
                    after = os.fstat(stream.fileno())
                after_signature = (
                    after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns,
                )
                if len(sample) != sample_size or before_signature != after_signature:
                    truncated = True
                    continue
            except OSError:
                truncated = True
                continue
            file_digest = hashlib.sha256()
            file_digest.update(sample)
            file_digest.update(b"\0")
            file_digest.update(
                f"{size}\0{before.st_mtime_ns}\0{before.st_ctime_ns}".encode("ascii")
            )
            digest = file_digest.hexdigest()
            result.append((rel, path, size, digest))
            total_bytes += sample_size
        if truncated:
            break
    return result, truncated, total_bytes


# Compatibility aliases
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
