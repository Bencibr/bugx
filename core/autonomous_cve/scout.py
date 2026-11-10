"""Autonomous surface scouting and vulnerability risk assessment."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Sequence

from .models import VulnerabilityCategory, VulnerableSurface


class AutonomousSurfaceScout:
    """Scout target directories and source files for high-risk vulnerability surfaces."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def scout(self, relative_subpath: str | None = None) -> list[VulnerableSurface]:
        """Scout target path for vulnerable surfaces, ranked by risk score descending."""
        target_dir = self.root / relative_subpath if relative_subpath else self.root
        if not target_dir.exists():
            return []

        surfaces: list[VulnerableSurface] = []
        if target_dir.is_file():
            files = [target_dir]
        else:
            files = [
                p for p in target_dir.rglob("*")
                if p.is_file() and not p.is_symlink() and self._is_supported_source(p)
            ]

        for file_path in files:
            surfaces.extend(self._analyze_file(file_path))

        # Sort by risk score descending
        surfaces.sort(key=lambda s: s.risk_score, reverse=True)
        return surfaces

    def _is_supported_source(self, path: Path) -> bool:
        ext = path.suffix.lower()
        return ext in {".py", ".c", ".cpp", ".cc", ".h", ".hpp", ".js", ".ts", ".cjs", ".mjs", ".go", ".rs", ".java", ".php"}

    def _detect_language(self, path: Path) -> str:
        ext = path.suffix.lower()
        mapping = {
            ".py": "python",
            ".c": "c",
            ".cpp": "cpp",
            ".cc": "cpp",
            ".h": "c_header",
            ".hpp": "cpp_header",
            ".js": "javascript",
            ".ts": "typescript",
            ".cjs": "javascript",
            ".mjs": "javascript",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
            ".php": "php",
        }
        return mapping.get(ext, "unknown")

    def _analyze_file(self, file_path: Path) -> list[VulnerableSurface]:
        surfaces: list[VulnerableSurface] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []

        lang = self._detect_language(file_path)
        if self.root.is_file():
            rel_path = file_path.name
        else:
            rel_path = str(file_path.relative_to(self.root))

        lines = content.splitlines()

        # 1. Path Traversal patterns
        path_patterns = [
            (r"(?:safe_join|join|resolve_path|open_file|serve_file|get_file|Clean|Open|ReadFile)\s*\(", "Path resolution function susceptible to directory traversal"),
            (r"(?:os|path|filepath)\.(?:join|Join|Clean|Open|ReadFile)\s*\(", "Direct path join or file open without boundary normalization"),
            (r"path\.join\s*\(", "Node path join without sandbox containment"),
        ]
        for idx, line in enumerate(lines, start=1):
            for pat, desc in path_patterns:
                if re.search(pat, line):
                    surfaces.append(VulnerableSurface(
                        surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-path",
                        file_path=rel_path,
                        language=lang,
                        line_start=idx,
                        line_end=idx,
                        symbol_name=file_path.stem,
                        category=VulnerabilityCategory.PATH_TRAVERSAL,
                        risk_score=0.88,
                        reason=desc,
                        matched_snippet=line.strip()[:100],
                    ))

        # 2. Stack Overflow / Extreme Recursion patterns
        recurse_patterns = [
            (r"(?:def|function|fn|func|void|public)\s+(?:\(.*?\)\s*)?([A-Za-z0-9_]+)\s*\(.*?\).*(?:parse|decode|unmarshal|visit|walk|eval|read|get|find|set|from_str|from_slice|from_reader|take|map)", "Recursive parser/deserializer or data accessor without explicit depth limiter"),
            (r"class\s+([A-Za-z0-9_]+Parser|[A-Za-z0-9_]+Decoder)", "Parser class prone to deep nesting stack exhaustion"),
        ]
        for idx, line in enumerate(lines, start=1):
            for pat, desc in recurse_patterns:
                m = re.search(pat, line)
                if m:
                    sym = m.group(1) if m.groups() else file_path.stem
                    surfaces.append(VulnerableSurface(
                        surface_id=f"surf-{len(surfaces)+1}-{sym}-recursion",
                        file_path=rel_path,
                        language=lang,
                        line_start=idx,
                        line_end=idx,
                        symbol_name=sym,
                        category=VulnerabilityCategory.STACK_OVERFLOW,
                        risk_score=0.85,
                        reason=desc,
                        matched_snippet=line.strip()[:100],
                    ))

        # 3. ReDoS (Catastrophic Backtracking) patterns
        redos_patterns = [
            (r"(?:RegExp|re\.compile|pathToRegexp|match)\s*\(.*?([+*].*?[+*]|\([a-zA-Z0-9_\\|\s]+\)[+*])", "Nested quantifiers susceptible to ReDoS exponential backtracking"),
            (r"new\s+RegExp\s*\(", "Dynamic regex instantiation without complexity bounds"),
        ]
        for idx, line in enumerate(lines, start=1):
            for pat, desc in redos_patterns:
                if re.search(pat, line):
                    surfaces.append(VulnerableSurface(
                        surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-redos",
                        file_path=rel_path,
                        language=lang,
                        line_start=idx,
                        line_end=idx,
                        symbol_name=file_path.stem,
                        category=VulnerabilityCategory.REDOS,
                        risk_score=0.82,
                        reason=desc,
                        matched_snippet=line.strip()[:100],
                    ))

        # 4. Buffer Overflow / Memory Safety patterns (C/C++)
        if lang in {"c", "cpp", "c_header", "cpp_header"}:
            buffer_patterns = [
                (r"(?:strcpy|strcat|sprintf|vsprintf|gets)\s*\(", "Unbounded string function in C/C++"),
                (r"\*\w+\+\+\s*=\s*\*\w+\+\+", "Pointer arithmetic copy without bounds check"),
                (r"while\s*\(\s*\*\w+\s*\)", "Unbounded null-terminated string traversal"),
            ]
            for idx, line in enumerate(lines, start=1):
                for pat, desc in buffer_patterns:
                    if re.search(pat, line):
                        surfaces.append(VulnerableSurface(
                            surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-buf",
                            file_path=rel_path,
                            language=lang,
                            line_start=idx,
                            line_end=idx,
                            symbol_name=file_path.stem,
                            category=VulnerabilityCategory.BUFFER_OVERFLOW,
                            risk_score=0.92,
                            reason=desc,
                            matched_snippet=line.strip()[:100],
                        ))

        # 5. Route Confusion / Malformed URL patterns
        route_patterns = [
            (r"(?:router|app|mux|engine|group|r)\.(?:GET|POST|Handle|HandleFunc|match|use|Use|route|nest)\s*\(|Router::new", "HTTP route dispatcher susceptible to URL-encoded path bypass"),
            (r"(?:url\.QueryUnescape|decodeURIComponent|unquote)", "URL decoding before routing resolution"),
        ]
        for idx, line in enumerate(lines, start=1):
            for pat, desc in route_patterns:
                if re.search(pat, line):
                    surfaces.append(VulnerableSurface(
                        surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-route",
                        file_path=rel_path,
                        language=lang,
                        line_start=idx,
                        line_end=idx,
                        symbol_name=file_path.stem,
                        category=VulnerabilityCategory.ROUTE_CONFUSION,
                        risk_score=0.80,
                        reason=desc,
                        matched_snippet=line.strip()[:100],
                    ))

        # 6. SQL / Database / ORM Injections
        sql_patterns = [
            (r"(?:db|conn|tx|gorm)\.(?:Raw|Exec|Query|Where|Order|Select|Table)\s*\(", "Database/ORM query construction susceptible to SQL fragment injection"),
        ]
        for idx, line in enumerate(lines, start=1):
            for pat, desc in sql_patterns:
                if re.search(pat, line):
                    surfaces.append(VulnerableSurface(
                        surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-sql",
                        file_path=rel_path,
                        language=lang,
                        line_start=idx,
                        line_end=idx,
                        symbol_name=file_path.stem,
                        category=VulnerabilityCategory.GENERIC_LOGIC,
                        risk_score=0.86,
                        reason=desc,
                        matched_snippet=line.strip()[:100],
                    ))

        # 7. Concurrency / Memory Hazards (Go/Rust/C)
        hazard_patterns = [
            (r"(?:sync\.Mutex|sync\.RWMutex|atomic\.|unsafe\.Pointer|make\(\[\]byte|unsafe\s*\{|from_raw_parts|as_ptr|as_mut_ptr|std::ptr|core::ptr|sync::Mutex|sync::RwLock|Atomic)", "Low-level memory, unsafe block or concurrent data structure susceptible to race or leak"),
        ]
        for idx, line in enumerate(lines, start=1):
            for pat, desc in hazard_patterns:
                if re.search(pat, line):
                    surfaces.append(VulnerableSurface(
                        surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-hazard",
                        file_path=rel_path,
                        language=lang,
                        line_start=idx,
                        line_end=idx,
                        symbol_name=file_path.stem,
                        category=VulnerabilityCategory.GENERIC_LOGIC,
                        risk_score=0.84,
                        reason=desc,
                        matched_snippet=line.strip()[:100],
                    ))

        # 8. Java Deserialization / Reflection / JNDI / SpEL patterns
        if lang == "java":
            java_patterns = [
                (r"(?:JSON\.parseObject|readValue|Class\.forName|Method\.invoke|readObject|XMLDecoder|InitialContext|lookup|SpelExpressionParser)\b", "Java reflection, dynamic deserialization or JNDI lookup surface"),
                (r"@(?:Select|Update|Delete|Insert)\s*\(|#\{|\$\{", "MyBatis SQL query mapping susceptible to dynamic fragment injection"),
                (r"(?:th:text|th:utext|th:replace|th:include)", "Thymeleaf template expression evaluation surface"),
            ]
            for idx, line in enumerate(lines, start=1):
                for pat, desc in java_patterns:
                    if re.search(pat, line):
                        surfaces.append(VulnerableSurface(
                            surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-java",
                            file_path=rel_path,
                            language=lang,
                            line_start=idx,
                            line_end=idx,
                            symbol_name=file_path.stem,
                            category=VulnerabilityCategory.DESERIALIZATION_DOS if "JSON" in pat or "read" in pat else VulnerabilityCategory.GENERIC_LOGIC,
                            risk_score=0.89,
                            reason=desc,
                            matched_snippet=line.strip()[:100],
                        ))

        # 9. PHP Unserialize / File Inclusion / Eval patterns
        if lang == "php":
            php_patterns = [
                (r"(?:unserialize|eval|assert|create_function)\s*\(", "PHP dynamic code evaluation or object unserialization"),
                (r"(?:include|require|include_once|require_once)\s*\(", "PHP dynamic file inclusion susceptible to LFI/RFI"),
                (r"(?:file_get_contents|readfile|fopen|unlink)\s*\(", "PHP file operation susceptible to path traversal"),
                (r"(?:header|setcookie)\s*\(", "PHP response header manipulation susceptible to header injection"),
            ]
            for idx, line in enumerate(lines, start=1):
                for pat, desc in php_patterns:
                    if re.search(pat, line):
                        surfaces.append(VulnerableSurface(
                            surface_id=f"surf-{len(surfaces)+1}-{file_path.stem}-php",
                            file_path=rel_path,
                            language=lang,
                            line_start=idx,
                            line_end=idx,
                            symbol_name=file_path.stem,
                            category=VulnerabilityCategory.GENERIC_LOGIC,
                            risk_score=0.88,
                            reason=desc,
                            matched_snippet=line.strip()[:100],
                        ))

        return surfaces
