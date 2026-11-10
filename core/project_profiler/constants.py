#!/usr/bin/env python3
"""Constants, vocabularies, and schemas for project profiling."""
from __future__ import annotations

import re
from typing import Any

PROFILE_SCHEMA_VERSION = 2
DETECTOR_VERSION_BASE = "2"
PROFILE_ROW_KEY = "project"
DEFAULT_FILE_LIMIT = 4000
DEFAULT_BYTE_LIMIT = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_COMMANDS = 32
MAX_EVIDENCE = 96
MAX_FACTS = 128
MAX_COMPONENTS = 512
PROFILE_MEM_CACHE: dict[str, dict[str, Any]] = {}
_PROFILE_MEM_CACHE = PROFILE_MEM_CACHE

LANGUAGES_BY_EXT: dict[str, str] = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy", ".go": "go",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".rs": "rust", ".rb": "ruby",
    ".php": "php", ".swift": "swift", ".m": "objective-c",
    ".mm": "objective-cpp", ".sh": "shell", ".bash": "shell", ".sql": "sql",
    ".vue": "vue", ".svelte": "svelte", ".html": "html", ".css": "css",
    ".scss": "scss", ".dart": "dart", ".lua": "lua", ".r": "r",
    ".ino": "cpp", ".proto": "protobuf", ".sol": "solidity",
}

FORMS = ("web", "backend", "mobile", "embedded", "desktop", "cli", "library", "data", "infra")
SURFACES = ("ui", "rest", "graphql", "grpc", "websocket", "auth", "storage", "file", "queue", "native", "build", "ci-cd", "dependency")
TARGETS = ("browser", "server", "container", "emulator", "device", "mcu", "rtos", "linux", "macos", "windows", "android", "ios")
RUNTIMES = ("cpython", "nodejs", "deno", "bun", "jvm", "dotnet", "dart-vm", "embedded-toolchain")

ALIASES = {
    "js": "javascript", "node": "nodejs", "node.js": "nodejs", "py": "python",
    "golang": "go", "c++": "cpp", "c#": "csharp", "ios-app": "ios",
    "android-app": "android", "rest-api": "rest", "graphql-api": "graphql",
}
_ALIASES = ALIASES

SKIP_DIRS = {
    ".git", ".bugx", ".opencode", ".codex", ".claude", ".cline", ".vscode",
    ".agents", ".cursor", ".gemini", "node_modules", ".venv",
    "venv", "env", "__pycache__", "dist", "build", ".idea", ".tox", ".mypy_cache",
    ".pytest_cache", "target", "vendor", "Pods", "DerivedData", ".next", ".nuxt",
    "evals",
}
_SKIP_DIRS = SKIP_DIRS

ENTRYPOINT_NAMES = {"main.py", "main.go", "server.py", "manage.py", "app.py", "server.ts", "server.js"}
_ENTRYPOINT_NAMES = ENTRYPOINT_NAMES

SENSITIVE = re.compile(r"(?i)(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)\s*[:=]")
_SENSITIVE = SENSITIVE

MANIFEST_NAMES = {
    "pyproject.toml", "package.json", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "podfile", "cargo.toml", "composer.json",
    "platformio.ini", "cmakelists.txt", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "terraform.tf", "main.tf", "app.json", "pubspec.yaml", "mix.exs", "project.pbxproj",
}
_MANIFEST_NAMES = MANIFEST_NAMES

VERSION_FILES = {
    ".java-version": ("jvm", "java"),
    ".python-version": ("python", "python"),
    ".nvmrc": ("nodejs", "node"),
    ".node-version": ("nodejs", "node"),
    ".bun-version": ("bun", "javascript"),
    ".deno-version": ("deno", "javascript"),
    "rust-toolchain": ("rust", "rust"),
    "rust-toolchain.toml": ("rust", "rust"),
    "global.json": ("dotnet", ".net"),
}
_VERSION_FILES = VERSION_FILES

BUILD_FINGERPRINT_FILES = (
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
    "yarn.lock", "bun.lock", "bun.lockb", "pyproject.toml", "uv.lock",
    "poetry.lock", "requirements.txt", "Pipfile.lock", "go.mod", "go.sum",
    "Cargo.toml", "Cargo.lock", "pom.xml", "build.gradle", "build.gradle.kts",
    "gradle.lockfile", "Gemfile", "Gemfile.lock", "composer.json",
    "composer.lock", "pubspec.yaml", "Package.swift", "Package.resolved",
    "global.json", "Directory.Build.props", "Dockerfile", "docker-compose.yml",
)
_BUILD_FINGERPRINT_FILES = BUILD_FINGERPRINT_FILES

CHECK_PROFILE_REQUIREMENTS: dict[str, dict[str, list[str]]] = {
    "web-input": {
        "capabilities": ["browser-automation", "http-client"],
        "evidence": ["page-state", "request-response", "negative-control"],
    },
    "browser-state": {
        "capabilities": ["browser-automation"],
        "evidence": ["navigation-state", "storage-state", "permission-difference"],
    },
    "auth-flow": {
        "capabilities": ["http-client", "browser-automation"],
        "evidence": ["identity-difference", "session-state", "unauthorized-control"],
    },
    "api-contract": {
        "capabilities": ["http-client", "schema-differential"],
        "evidence": ["request-response", "schema-validation", "negative-control"],
    },
    "authz": {
        "capabilities": ["http-client", "database-observer"],
        "evidence": ["identity-difference", "authorization-boundary", "persistence-state"],
    },
    "state-transition": {
        "capabilities": ["state-machine", "fault-injection"],
        "evidence": ["transition-trace", "rollback-state", "recovery-state"],
    },
    "mobile-lifecycle": {
        "capabilities": ["device-automation", "log-observer"],
        "evidence": ["lifecycle-trace", "device-state", "crash-recovery"],
    },
    "offline-sync": {
        "capabilities": ["device-automation", "network-fault-injection"],
        "evidence": ["offline-online-transition", "conflict-resolution", "replay-state"],
    },
    "deep-link": {
        "capabilities": ["device-automation", "http-client"],
        "evidence": ["link-routing", "identity-difference", "permission-boundary"],
    },
    "firmware-boundary": {
        "capabilities": ["protocol-fuzz", "static-analysis"],
        "evidence": ["frame-boundary", "reset-recovery", "memory-boundary"],
    },
    "memory-safety": {
        "capabilities": ["static-analysis", "native-fuzz"],
        "evidence": ["sanitizer-result", "boundary-condition", "crash-reproduction"],
    },
    "update-path": {
        "capabilities": ["artifact-validation", "fault-injection"],
        "evidence": ["version-transition", "rollback-state", "integrity-check"],
    },
    "argument-boundary": {
        "capabilities": ["pty-runner", "property-fuzz"],
        "evidence": ["argv-result", "exit-code", "negative-control"],
    },
    "filesystem-boundary": {
        "capabilities": ["sandbox-runner", "path-fuzz"],
        "evidence": ["path-boundary", "permission-difference", "artifact-scope"],
    },
    "process-exit": {
        "capabilities": ["pty-runner", "signal-injection"],
        "evidence": ["exit-code", "signal-recovery", "restart-state"],
    },
    "deployment-config": {
        "capabilities": ["config-validator", "container-observer"],
        "evidence": ["effective-config", "startup-health", "rollback-state"],
    },
    "secret-boundary": {
        "capabilities": ["secret-scanner", "config-validator"],
        "evidence": ["redaction-proof", "scope-boundary", "negative-control"],
    },
    "supply-chain": {
        "capabilities": ["dependency-audit", "artifact-validation"],
        "evidence": ["lockfile-integrity", "dependency-resolution", "artifact-provenance"],
    },
    "generic-code": {
        "capabilities": ["static-analysis", "test-runner"],
        "evidence": ["boundary-condition", "negative-control", "regression-result"],
    },
}
_CHECK_PROFILE_REQUIREMENTS = CHECK_PROFILE_REQUIREMENTS

PY_HTTP_DEPS = ("fastapi", "flask", "django", "starlette", "tornado", "aiohttp", "sanic", "falcon")
PY_STORAGE_DEPS = (
    "sqlalchemy", "psycopg2", "psycopg", "pymysql", "mysqlclient", "pymongo",
    "motor", "alembic", "peewee", "tortoise-orm", "aiosqlite",
)
PY_QUEUE_DEPS = ("redis", "aioredis", "celery", "kafka-python", "pika", "nats-py", "rq", "dramatiq")
PY_CLI_DEPS = ("click", "typer", "argparse", "rich")
PY_FRAMEWORK_DEPS = (
    ("fastapi", "fastapi"), ("flask", "flask"), ("django", "django"),
    ("starlette", "starlette"), ("sqlalchemy", "sqlalchemy"), ("pydantic", "pydantic"),
    ("tornado", "tornado"), ("aiohttp", "aiohttp"),
)

CONTENT_SCAN_FILES = 80
CONTENT_SCAN_BYTES = 128 * 1024
CONTENT_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".mjs", ".cjs", ".go", ".rb", ".php", ".java", ".kt",
})
HTTP_MARKERS = (
    "@app.route", "@app.get", "@app.post", "apirouter(", "fastapi(",
    "flask(", "http.server", "express()", "createapp(",
)
STORAGE_MARKERS = (
    "sqlite3", "psycopg", "pymysql", "sqlalchemy", "pymongo", "aiomysql",
    "cursor()", "session.execute",
)
QUEUE_MARKERS = ("redis.", "celery", "pika.", "kafka", "nats.")
CLI_MARKERS = ("argparse", "click.command", "typer.typer", "from click", "sys.argv")

ARTIFACT_DIRS = ("build", "dist", "out", "artifacts", "release")
_ARTIFACT_DIRS = ARTIFACT_DIRS

WIRE_COMPONENTS_LIMIT = 24
WIRE_RISK_SIGNALS_LIMIT = 16
WIRE_EVIDENCE_LIMIT = 4
WIRE_FACTS_LIMIT = 8
WIRE_PAYLOAD_BUDGET_BYTES = 64 * 1024

WIRE_RISK_SIGNAL_KEYS = (
    "kind", "source", "id", "package", "ecosystem", "matched_version",
    "affected_range", "fixed_version", "severity", "confidence",
)
_WIRE_RISK_SIGNAL_KEYS = WIRE_RISK_SIGNAL_KEYS

WIRE_DROP_ORDER = (
    "flows", "evidence", "declared_commands", "unknowns", "conflicts",
    "toolchains", "build_targets", "framework_facts", "runtime_facts",
    "code_modules", "code_languages", "risk_signals", "components",
)
_WIRE_DROP_ORDER = WIRE_DROP_ORDER

CLIENT_ONLY_PROFILE_KEYS = frozenset({
    "ai_profile_arbitration",
})
_CLIENT_ONLY_PROFILE_KEYS = CLIENT_ONLY_PROFILE_KEYS

TEST_PATH_SEGMENTS = frozenset({
    "test", "tests", "fixture", "fixtures", "testdata", "mock", "mocks",
    "sample", "samples", "example", "examples", "corpus", "stub", "stubs",
})
_TEST_PATH_SEGMENTS = TEST_PATH_SEGMENTS

SOURCE_EXTENSIONS_BY_ECOSYSTEM: dict[str, set[str]] = {
    "mobile": {".dart", ".kt", ".swift", ".java", ".m", ".mm"},
    "flutter": {".dart"},
    "android": {".kt", ".java"},
    "ios": {".swift", ".m", ".mm"},
    "web": {".vue", ".svelte", ".jsx", ".tsx", ".html"},
    "jvm": {".java", ".kt", ".scala", ".groovy"},
    "node": {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"},
    "go": {".go"},
    "rust": {".rs"},
    "python": {".py", ".pyw"},
}
_SOURCE_EXTENSIONS_BY_ECOSYSTEM = SOURCE_EXTENSIONS_BY_ECOSYSTEM
