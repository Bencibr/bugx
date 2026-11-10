#!/usr/bin/env python3
"""BugX 挖掘方案与用例全息保证框架 (Mining Quality Framework, DOC-052).

专司对 BugX 漏洞挖掘方案 (Mining Plan / Campaign) 与挖掘用例进行全息质量保证：
  1. MiningSurfaceExplorer: 全息勘测代码库的可挖掘功能主体 (AST函数、Web路由、CLI分支、临界区、反序列化);
  2. ThreatModelMatcher: 威胁模型与挖掘技术、Oracle精准对齐;
  3. MiningPlanAuditor: 四维 (准确/完整/有效/全面) 100 分制量化审计评估与严格门禁;
  4. MiningPlanHardener: 挖掘方案自动加固 (自动补全隐式入口、健全 DAG 依赖、绑定深度语义 Oracle).
"""
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
import sys
from pathlib import Path


import argparse
import ast
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

try:
    from core.retrieval.universal_code_matcher import EXT_TO_LANG, UniversalCodeMatcher
except ImportError:
    try:
        from core.retrieval.universal_code_matcher import EXT_TO_LANG, UniversalCodeMatcher
    except ImportError:
        EXT_TO_LANG = {}
        UniversalCodeMatcher = None

SCHEMA_VERSION = 1

SUPPORTED_SOURCE_EXTS = {
    ".py",
    ".go",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".java", ".kt", ".kts",
    ".rs",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".cs", ".php", ".rb",
}

SKIP_DIRS = {
    ".git", ".bugx", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", "Pods", ".gradle", ".idea",
}

# 路由装饰器识别正则与特征
ROUTE_DECORATOR_PATTERNS = [
    re.compile(r"^(get|post|put|delete|patch|options|head|route|api_route|websocket)$", re.I),
]
CLI_PATTERNS = [
    re.compile(r"\b(argparse|click|typer|fire|docopt|sys\.argv)\b"),
]
DESERIALIZE_PATTERNS = [
    re.compile(r"\b(pickle|yaml|marshal|shelve|json\.loads|JSON\.parse|eval|exec)\b"),
]
CONCURRENCY_PATTERNS = [
    re.compile(r"\b(lock|rlock|semaphore|event|condition|barrier|mutex|threading\.lock|asyncio\.lock)\b", re.I),
]
STATE_TRANSITION_PATTERNS = [
    re.compile(r"\b(state|status|transition|workflow|stage|step|phase)\b", re.I),
]

# 多语言函数定义提取正则
GO_FUNC_PATTERN = re.compile(
    r"^\s*func\s+(?:\((?P<recv>[^)]+)\)\s+)?(?P<name>[A-Za-z0-9_]+)\s*\(",
    re.MULTILINE,
)
C_CPP_FUNC_PATTERN = re.compile(
    r"^\s*(?:(?:inline|static|virtual|extern|const|unsigned|signed|volatile|void|int|char|bool|float|double|size_t|ssize_t|auto|[A-Za-z0-9_]+)(?:\s*[*&]+|\s+))+\s*(?:(?P<class>[A-Za-z0-9_]+)::)?(?P<name>[A-Za-z0-9_~]+)\s*\([^;{()]*\)\s*(?:const|override|noexcept)?\s*\{",
    re.MULTILINE,
)
JAVA_FUNC_PATTERN = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:public|protected|private|static|final|abstract|synchronized|native|\s)*\s*(?:[\w<>\[\],?]+)\s+(?P<name>[A-Za-z0-9_]+)\s*\([^;{]*\)\s*(?:throws\s+[\w.,\s]+)?\{",
    re.MULTILINE,
)
KOTLIN_FUNC_PATTERN = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:suspend\s+|override\s+|private\s+|public\s+|protected\s+|internal\s+)*fun\s+(?:\([^)]+\)\.)?(?P<name>[A-Za-z0-9_]+)\s*\(",
    re.MULTILINE,
)
RUST_FUNC_PATTERN = re.compile(
    r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>[A-Za-z0-9_]+)\s*(?:<[^>]*>)?\s*\(",
    re.MULTILINE,
)
JS_TS_FUNC_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z0-9_]+)\s*\(|^\s*(?:export\s+)?(?:const|let|var)\s+(?P<var_name>[A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
    re.MULTILINE,
)

# 多语言高危与特征模式
MEMORY_SAFETY_PATTERNS = [
    re.compile(r"\b(strcpy|strcat|sprintf|vsprintf|gets|scanf|malloc|calloc|realloc|free|memcpy|memmove|memset|unsafe\.Pointer|unsafe\s*\{)\b"),
]
UNIVERSAL_ROUTE_PATTERNS = [
    re.compile(
        r"(?:http\.ResponseWriter|\*http\.Request|\*gin\.Context|echo\.Context|"
        r"\b(?:http\.HandleFunc|r\.(?:GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD|Group)|e\.(?:GET|POST|PUT|DELETE)|router\.(?:HandleFunc|Handle|GET|POST)|"
        r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping|PatchMapping)|"
        r"CROW_ROUTE|ENDPOINT\(|ADD_ROUTE|(?:app|router)\.(?:get|post|put|delete|patch))\b)",
        re.IGNORECASE,
    ),
]
UNIVERSAL_CONCURRENCY_PATTERNS = [
    re.compile(r"\b(sync\.(?:Mutex|RWMutex|WaitGroup|Once|Cond)|atomic\.|select\s*\{|chan\s+[A-Za-z0-9_]+|go\s+(?:func|[A-Za-z0-9_]+)|pthread_mutex|std::mutex|std::lock_guard|std::unique_lock|pthread_create|std::thread|std::atomic|synchronized|ReentrantLock|ConcurrentHashMap|AtomicInteger|AtomicReference|tokio::sync|std::sync::Mutex)\b"),
]
UNIVERSAL_DESERIALIZE_PATTERNS = [
    re.compile(r"\b(json\.Unmarshal|xml\.Unmarshal|gob\.NewDecoder|yaml\.Unmarshal|readObject|ObjectMapper|XMLDecoder|fromXML|protobuf.*ParseFromString|nlohmann::json::parse)\b"),
]
UNIVERSAL_CLI_PATTERNS = [
    re.compile(r"\b(flag\.Parse|cobra\.Command|os\.Args|process\.argv|getopt|getopt_long|argc\s*,\s*argv|CommandLineParser|args\[|clap::|std::env::args)\b"),
]

# Explicit overrides are intentionally limited to the productized surface
# languages.  Auto-detection still preserves the older regex fallback for the
# other extensions supported by this module.
SURFACE_LANGUAGE_ALIASES = {
    "python": "python", "py": "python",
    "javascript": "javascript", "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "typescript": "typescript", "ts": "typescript", "tsx": "tsx",
    "javascript/typescript": "javascript/typescript", "js/ts": "javascript/typescript",
    "go": "go",
    "c": "c", "cpp": "cpp", "c++": "cpp", "c/c++": "c/c++", "c-family": "c/c++",
    "java": "java",
}
TREE_SITTER_SURFACE_LANGUAGES = {"javascript", "typescript", "tsx", "go", "c", "cpp", "java"}
TREE_SITTER_CPP_EXTS = {".cc", ".cpp", ".cxx", ".hpp"}
TREE_SITTER_TS_EXTS = {".ts", ".tsx"}
TREE_SITTER_NAME_TYPES = {
    "identifier", "type_identifier", "property_identifier", "field_identifier",
    "namespace_identifier", "name",
}
TREE_SITTER_TYPE_NODES = {
    "class_declaration": "class", "abstract_class_declaration": "class",
    "interface_declaration": "type", "type_alias_declaration": "type",
    "enum_declaration": "type", "record_declaration": "type",
    "annotation_type_declaration": "type", "type_declaration": "type",
    "class_specifier": "class", "struct_specifier": "type",
    "union_specifier": "type", "enum_specifier": "type",
}
TREE_SITTER_FUNCTION_NODES = {
    "function_declaration", "function_definition", "function_item",
    "method_definition", "method_declaration", "constructor_declaration",
    "generator_function_declaration", "function_expression", "arrow_function",
    "variable_declarator",
}
TREE_SITTER_MACRO_NODES = {"preproc_def", "preproc_function_def"}
TREE_SITTER_ROUTE_METHODS = {
    "get", "post", "put", "delete", "patch", "options", "head", "all",
    "route", "websocket", "handle", "handlefunc", "endpoint",
}
TREE_SITTER_DANGEROUS_MACRO_PATTERNS = [
    re.compile(r"\b(strcpy|strcat|sprintf|vsprintf|gets|memcpy|memmove|system|popen|exec(?:l|le|lp|v|ve|vp)?)\s*\(", re.I),
    re.compile(r"\b(unsafe|danger|raw_copy|unchecked|no_sanitize)\b", re.I),
]
ROUTE_ENDPOINT_PATTERNS = (
    re.compile(
        r"@(?P<method>GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
        r"\s*\(\s*[\"'](?P<path>[^\"']+)"
    ),
    re.compile(
        r"\b(?:app|router|r|e)\s*\.\s*(?P<method>get|post|put|delete|patch|options|head|route|handle)"
        r"\s*\(\s*[\"'](?P<path>[^\"']+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:http\.HandleFunc|router\.HandleFunc|router\.Handle|e\.GET|e\.POST)"
        r"\s*\(\s*[\"'](?P<path>[^\"']+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:ADD_ROUTE|ENDPOINT|CROW_ROUTE)\s*\(\s*(?:[^,]+\s*,\s*)?"
        r"[\"'](?P<path>[^\"']+)"
    ),
    re.compile(
        r"@\w+\s*\.\s*(?P<method>get|post|put|delete|patch|route)"
        r"\s*\(\s*[\"'](?P<path>[^\"']+)"
    ),
)


@dataclass
class MineableSubject:
    """可挖掘功能主体结构化表示。"""
    scope: str
    path: str
    name: str
    kind: str  # function, method, http_route, cli_command, critical_section, state_transition, deserialization
    start_line: int
    end_line: int
    threat_models: list[str] = field(default_factory=list)
    recommended_techniques: list[str] = field(default_factory=list)
    oracle_types: list[str] = field(default_factory=list)
    risk_level: str = "MEDIUM"  # CRITICAL, HIGH, MEDIUM, LOW
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MiningSurfaceExplorer:
    """全息勘测代码库的可挖掘功能主体。"""

    def __init__(self, root: Path, language: str | None = None) -> None:
        self.root = root.resolve()
        self.language = self._normalize_language(language)
        # Partial discovery is useful, but callers must be able to tell when
        # a file or an enrichment source was skipped.
        self.errors: list[str] = []

    @staticmethod
    def _normalize_language(language: str | None) -> str | None:
        if language is None:
            return None
        key = str(language).strip().lower()
        normalized = SURFACE_LANGUAGE_ALIASES.get(key)
        if normalized is None:
            supported = ", ".join(sorted(set(SURFACE_LANGUAGE_ALIASES.values())))
            raise ValueError(f"不支持的扫描语言 {language!r}；可选值: {supported}")
        return normalized

    def _language_for_path(self, filepath: Path) -> str | None:
        override = self.language
        suffix = filepath.suffix.lower()
        if override == "javascript/typescript":
            return "typescript" if suffix in TREE_SITTER_TS_EXTS else "javascript"
        if override == "c/c++":
            return "cpp" if suffix in TREE_SITTER_CPP_EXTS else "c"
        if override:
            return override
        return EXT_TO_LANG.get(suffix)

    def _record_error(self, context: str, exc: Exception) -> None:
        if len(self.errors) >= 32:
            return
        detail = str(exc).strip().replace("\n", " ")[:160]
        suffix = f": {detail}" if detail else ""
        self.errors.append(f"{context}: {type(exc).__name__}{suffix}")

    def explore(self, subpath: str | None = None) -> list[MineableSubject]:
        self.errors.clear()
        subjects: list[MineableSubject] = []
        target_dir = (self.root / subpath) if subpath else self.root
        if not target_dir.exists():
            return subjects

        source_files: list[Path] = []
        if target_dir.is_file():
            if target_dir.suffix.lower() in SUPPORTED_SOURCE_EXTS:
                source_files = [target_dir]
        elif target_dir.is_dir():
            for p in target_dir.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
                    continue
                rel_parts = p.relative_to(self.root).parts
                if any(part in SKIP_DIRS or part.startswith((".", "venv", "__pycache__", "build", "dist", "node_modules", "target", "vendor")) for part in rel_parts):
                    continue
                source_files.append(p)

        for src_path in source_files:
            rel_path = str(src_path.relative_to(self.root)).replace("\\", "/")
            try:
                content = src_path.read_text(encoding="utf-8", errors="replace")
                language = self._language_for_path(src_path)
                if language == "python":
                    try:
                        tree = ast.parse(content, filename=str(src_path))
                        subjects.extend(self._scan_ast(tree, rel_path, content))
                    except Exception as ast_exc:
                        self._record_error(f"scan {rel_path}", ast_exc)
                        subjects.extend(self._scan_universal(src_path, rel_path, content, language=language))
                else:
                    subjects.extend(self._scan_universal(src_path, rel_path, content, language=language))
            except Exception as exc:
                self._record_error(f"scan {rel_path}", exc)
                continue


        # 融合架构拓扑循环依赖脆弱靶点 (DOC-060)
        try:

            profile_path = self.root / ".bugx" / "knowledge" / "project-profile.json"
            if profile_path.is_file():
                import json
                pdata = json.loads(profile_path.read_text(encoding="utf-8"))
                topo = pdata.get("architecture_topology") or {}
                cycles = topo.get("cycles") or topo.get("circular_dependency_cycles") or []
                seen_mods: set[str] = set()
                for cycle in cycles:
                    for mod_file in cycle:
                        mod_str = str(mod_file).replace("\\", "/")
                        if mod_str in seen_mods:
                            continue
                        seen_mods.add(mod_str)
                        if subpath and not mod_str.startswith(subpath):
                            continue
                        subjects.append(MineableSubject(
                            scope=f"{mod_str}::circular_dependency",
                            path=mod_str,
                            name="circular_dependency_hazard",
                            kind="circular_dependency",
                            start_line=1,
                            end_line=1,
                            threat_models=["reentrancy_deadlock", "state_corruption"],
                            recommended_techniques=["concurrency-race", "state-machine"],
                            oracle_types=["concurrency_barrier", "transition_invariant"],
                            risk_level="HIGH",
                        ))
        except Exception as exc:
            self._record_error("load architecture topology", exc)

        # 排序：CRITICAL > HIGH > MEDIUM > LOW，同级按路径+行号
        risk_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        subjects.sort(key=lambda s: (risk_order.get(s.risk_level, 4), s.path, s.start_line))
        return subjects


    def _scan_ast(self, tree: ast.AST, rel_path: str, content: str) -> list[MineableSubject]:
        found: list[MineableSubject] = []
        lines = content.splitlines()

        class SurfaceVisitor(ast.NodeVisitor):
            def __init__(self, explorer: MiningSurfaceExplorer) -> None:
                self.explorer = explorer
                self.current_class: str | None = None

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                prev = self.current_class
                self.current_class = node.name
                found.append(MineableSubject(
                    scope=f"{rel_path}::{node.name}",
                    path=rel_path,
                    name=node.name,
                    kind="class",
                    start_line=getattr(node, "lineno", 1),
                    end_line=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                    threat_models=["boundary", "negative-control"],
                    recommended_techniques=["boundary", "negative-control"],
                    oracle_types=["invariant", "contract"],
                    risk_level="LOW",
                    metadata={"language": "python", "parser": "python-ast", "node_type": "ClassDef"},
                ))
                self.generic_visit(node)
                self.current_class = prev

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._analyze_func(node)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._analyze_func(node)
                self.generic_visit(node)

            def _analyze_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                scope_prefix = f"{self.current_class}." if self.current_class else ""
                scope = f"{rel_path}::{scope_prefix}{node.name}"
                start_l = getattr(node, "lineno", 1)
                end_l = getattr(node, "end_lineno", start_l)
                func_snippet = "\n".join(lines[max(0, start_l - 1):end_l])

                kind = "method" if self.current_class else "function"
                threats: list[str] = ["boundary", "negative-control"]
                techniques: list[str] = ["boundary", "negative-control"]
                oracles: list[str] = ["invariant", "contract"]
                risk = "LOW"

                # 1. 检测 Web 路由装饰器
                is_route = False
                route_method = "GET"
                route_path = ""
                for dec in node.decorator_list:
                    dec_call = dec if isinstance(dec, ast.Call) else None
                    dec_func = dec_call.func if dec_call else dec
                    dec_name = ""
                    if isinstance(dec_func, ast.Attribute):
                        dec_name = dec_func.attr
                    elif isinstance(dec_func, ast.Name):
                        dec_name = dec_func.id

                    for pat in ROUTE_DECORATOR_PATTERNS:
                        if pat.match(dec_name):
                            is_route = True
                            route_method = dec_name.upper()
                            if dec_call and dec_call.args and isinstance(dec_call.args[0], ast.Constant):
                                route_path = str(dec_call.args[0].value)
                            break
                    if is_route:
                        break

                if is_route:
                    kind = "http_route"
                    threats.extend(["auth_bypass", "input_injection", "idor"])
                    techniques.extend(["property-based", "guided-fuzz", "contract-differential"])
                    oracles.extend(["status_code", "schema_validation"])
                    risk = "CRITICAL" if any(kw in route_path.lower() for kw in ("admin", "auth", "user", "order", "pay", "token")) else "HIGH"

                # 2. 检测 CLI 解析入口
                if any(pat.search(func_snippet) for pat in CLI_PATTERNS):
                    kind = "cli_command"
                    threats.extend(["argument_injection", "flag_abuse", "path_traversal"])
                    techniques.extend(["guided-fuzz", "boundary"])
                    risk = "HIGH" if risk != "CRITICAL" else risk

                # 3. 检测反序列化高危汇聚点
                if any(pat.search(func_snippet) for pat in DESERIALIZE_PATTERNS):
                    kind = "deserialization" if not is_route else kind
                    threats.extend(["rce", "type_confusion", "resource_exhaustion"])
                    techniques.extend(["fault-injection", "guided-fuzz"])
                    oracles.extend(["memory_safety", "sandbox_integrity"])
                    risk = "CRITICAL"

                # 4. 检测并发临界区
                if any(pat.search(func_snippet) for pat in CONCURRENCY_PATTERNS):
                    if kind in ("function", "method"):
                        kind = "critical_section"
                    threats.extend(["race_condition", "deadlock", "lost_update"])
                    techniques.extend(["concurrency-race"])
                    oracles.append("concurrency_barrier")
                    if risk == "LOW":
                        risk = "MEDIUM"

                # 5. 检测状态迁移
                if any(pat.search(func_snippet) for pat in STATE_TRANSITION_PATTERNS):
                    threats.extend(["state_machine_bypass", "illegal_transition"])
                    techniques.extend(["state-machine"])
                    oracles.append("transition_invariant")
                    if risk == "LOW":
                        risk = "MEDIUM"

                found.append(MineableSubject(
                    scope=scope,
                    path=rel_path,
                    name=node.name,
                    kind=kind,
                    start_line=start_l,
                    end_line=end_l,
                    threat_models=sorted(set(threats)),
                    recommended_techniques=sorted(set(techniques)),
                    oracle_types=sorted(set(oracles)),
                    risk_level=risk,
                    metadata={
                        "language": "python",
                        "parser": "python-ast",
                        **({"endpoint": route_path, "method": route_method} if is_route else {}),
                    },
                ))

        visitor = SurfaceVisitor(self)
        visitor.visit(tree)
        found.extend(self._route_subjects(rel_path, content, "python"))

        # 融合代码分析引擎的深度污点流分析 (Taint Flow) 与高熵机密 (DOC-060)
        try:
            from core.retrieval import code_analyzer

            ast_metrics = code_analyzer._analyze_python_ast(content)
            taints = ast_metrics.get("taint_flows") or []
            for t in taints[:5]:  # 每个文件最多取前 5 处关键污点流
                func_name = str(t.get("func") or "unknown")
                sink = str(t.get("sink") or "sensitive_sink")
                line_no = int(t.get("line") or 1)
                found.append(MineableSubject(
                    scope=f"{rel_path}::{func_name}",
                    path=rel_path,
                    name=f"{func_name}_taint_{sink}",
                    kind="taint_sink",
                    start_line=line_no,
                    end_line=line_no,
                    threat_models=["command_injection", "rce", "sql_injection", "unsafe_deserialization"],
                    recommended_techniques=["guided-fuzz", "fault-injection"],
                    oracle_types=["invariant", "contract", "exit_code"],
                    risk_level="CRITICAL",
                ))

            secrets = code_analyzer.scan_high_entropy_secrets(content)
            for sec in secrets[:3]:  # 每个文件最多取前 3 处高熵机密
                var_name = str(sec.get("var") or "secret")
                line_no = int(sec.get("line") or 1)
                found.append(MineableSubject(
                    scope=f"{rel_path}::{var_name}",
                    path=rel_path,
                    name=f"secret_{var_name}",
                    kind="hardcoded_secret",
                    start_line=line_no,
                    end_line=line_no,
                    threat_models=["credential_leak", "unauthorized_access"],
                    recommended_techniques=["boundary"],
                    oracle_types=["invariant"],
                    risk_level="HIGH",
                ))
        except Exception as exc:
            self._record_error(f"deep analysis {rel_path}", exc)

        return found

    @staticmethod
    def _tree_node_name(node: object) -> str | None:
        direct = node.child_by_field_name("name")
        if direct is not None:
            return direct.text.decode("utf-8", errors="replace")
        declarator = node.child_by_field_name("declarator")
        queue = [declarator] if declarator is not None else list(node.named_children[:6])
        while queue:
            current = queue.pop(0)
            if current.type in TREE_SITTER_NAME_TYPES:
                return current.text.decode("utf-8", errors="replace")
            queue[0:0] = list(current.named_children[:6])
        return None

    @staticmethod
    def _tree_node_text(node: object, source_bytes: bytes) -> str:
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _classify_tree_surface(
        self,
        base_kind: str,
        snippet: str,
        language: str,
        *,
        endpoint: str | None = None,
        method: str | None = None,
    ) -> tuple[str, list[str], list[str], list[str], str, dict[str, Any]]:
        kind = base_kind
        threats: list[str] = ["boundary", "negative-control"]
        techniques: list[str] = ["boundary", "negative-control"]
        oracles: list[str] = ["invariant", "contract"]
        risk = "LOW"
        metadata: dict[str, Any] = {
            "language": language,
            "parser": f"tree-sitter-{language}",
        }
        if endpoint:
            metadata.update({"endpoint": endpoint, "method": method or "GET"})

        if base_kind in {"class", "type"}:
            return kind, threats, techniques, oracles, risk, metadata

        if language in {"c", "cpp", "go"} and any(
            pattern.search(snippet) for pattern in MEMORY_SAFETY_PATTERNS
        ):
            kind = "memory_safety"
            threats.extend(["buffer_overflow", "heap_corruption", "use_after_free", "out_of_bounds_write"])
            techniques.extend(["guided-fuzz", "boundary", "fault-injection"])
            oracles.extend(["memory_safety", "asan_crash", "crash_signal"])
            risk = "CRITICAL"

        route_call = bool(re.search(
            r"(?:http\.HandleFunc|router\.(?:HandleFunc|Handle|GET|POST|PUT|DELETE|PATCH)|"
            r"r\.(?:GET|POST|PUT|DELETE|PATCH)|e\.(?:GET|POST|PUT|DELETE|PATCH)|"
            r"(?:app|router)\.(?:get|post|put|delete|patch)|@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping|PatchMapping)|"
            r"CROW_ROUTE|ENDPOINT\(|ADD_ROUTE)",
            snippet,
            re.IGNORECASE,
        ))
        http_handler_signature = bool(re.search(r"http\.ResponseWriter|\*http\.Request|\*gin\.Context|echo\.Context", snippet))
        if endpoint or route_call:
            kind = "http_route"
            threats.extend(["auth_bypass", "input_injection", "idor"])
            techniques.extend(["property-based", "guided-fuzz", "contract-differential"])
            oracles.extend(["status_code", "schema_validation"])
            risk = "CRITICAL" if any(
                word in (endpoint or snippet).lower()
                for word in ("admin", "auth", "user", "order", "pay", "token")
            ) else "HIGH"
        elif http_handler_signature:
            # A Go/HTTP handler is an attack surface even when its declaration
            # has no literal route call; keep the callable kind for planning.
            threats.extend(["auth_bypass", "input_injection", "idor"])
            techniques.extend(["property-based", "guided-fuzz", "contract-differential"])
            oracles.extend(["status_code", "schema_validation"])
            risk = "CRITICAL" if any(
                word in snippet.lower() for word in ("admin", "auth", "user", "order", "pay", "token")
            ) else "HIGH"

        if any(pattern.search(snippet) for pattern in UNIVERSAL_CLI_PATTERNS):
            if kind in {"function", "method"}:
                kind = "cli_command"
            threats.extend(["argument_injection", "flag_abuse", "path_traversal"])
            techniques.extend(["guided-fuzz", "boundary"])
            if risk != "CRITICAL":
                risk = "HIGH"

        if any(pattern.search(snippet) for pattern in UNIVERSAL_DESERIALIZE_PATTERNS) or any(
            pattern.search(snippet) for pattern in DESERIALIZE_PATTERNS
        ):
            if kind not in {"http_route", "memory_safety"}:
                kind = "deserialization"
            threats.extend(["rce", "type_confusion", "resource_exhaustion"])
            techniques.extend(["fault-injection", "guided-fuzz"])
            oracles.extend(["memory_safety", "sandbox_integrity"])
            risk = "CRITICAL"

        if any(pattern.search(snippet) for pattern in UNIVERSAL_CONCURRENCY_PATTERNS) or any(pattern.search(snippet) for pattern in CONCURRENCY_PATTERNS):
            if kind in {"function", "method"}:
                kind = "critical_section"
            threats.extend(["race_condition", "deadlock", "lost_update"])
            techniques.append("concurrency-race")
            oracles.append("concurrency_barrier")
            if risk == "LOW":
                risk = "MEDIUM"

        if any(pattern.search(snippet) for pattern in STATE_TRANSITION_PATTERNS):
            threats.extend(["state_machine_bypass", "illegal_transition"])
            techniques.append("state-machine")
            oracles.append("transition_invariant")
            if risk == "LOW":
                risk = "MEDIUM"

        return kind, threats, techniques, oracles, risk, metadata

    def _route_subjects(self, rel_path: str, content: str, language: str) -> list[MineableSubject]:
        found: list[MineableSubject] = []
        seen: set[tuple[str, int]] = set()
        for pattern in ROUTE_ENDPOINT_PATTERNS:
            for match in pattern.finditer(content):
                endpoint = match.groupdict().get("path")
                if not endpoint:
                    continue
                line = content[:match.start()].count("\n") + 1
                method = (match.groupdict().get("method") or "GET").upper()
                if method.endswith("MAPPING"):
                    method = method.removesuffix("MAPPING") or "GET"
                if method == "REQUEST":
                    method = "ANY"
                key = (endpoint, line)
                if key in seen:
                    continue
                seen.add(key)
                kind, threats, techniques, oracles, risk, metadata = self._classify_tree_surface(
                    "http_route", endpoint, language, endpoint=endpoint, method=method,
                )
                found.append(MineableSubject(
                    scope=f"{rel_path}::route:{method} {endpoint}",
                    path=rel_path,
                    name=f"{method} {endpoint}",
                    kind=kind,
                    start_line=line,
                    end_line=line,
                    threat_models=sorted(set(threats)),
                    recommended_techniques=sorted(set(techniques)),
                    oracle_types=sorted(set(oracles)),
                    risk_level=risk,
                    metadata=metadata,
                ))
        return found

    def _scan_tree_sitter(
        self, filepath: Path, rel_path: str, content: str, language: str,
    ) -> list[MineableSubject]:
        from tree_sitter_language_pack import get_parser

        tree = get_parser(language).parse(content.encode("utf-8"))
        source_bytes = content.encode("utf-8")
        found: list[MineableSubject] = []

        def append_node(node: object, parents: list[str], base_kind: str, name: str) -> list[str]:
            snippet = self._tree_node_text(node, source_bytes)
            node_kind = node.type
            if base_kind == "macro":
                dangerous = any(pattern.search(snippet) for pattern in TREE_SITTER_DANGEROUS_MACRO_PATTERNS)
                if not dangerous:
                    return parents
                threats = ["unsafe_macro"]
                techniques = ["guided-fuzz", "boundary", "fault-injection"]
                oracles = ["invariant", "memory_safety"]
                if re.search(r"\b(strcpy|strcat|sprintf|vsprintf|gets|memcpy|memmove)\s*\(", snippet, re.I):
                    threats.extend(["buffer_overflow", "out_of_bounds_write"])
                if re.search(r"\b(system|popen|exec(?:l|le|lp|v|ve|vp)?)\s*\(", snippet, re.I):
                    threats.extend(["command_injection", "rce"])
                found.append(MineableSubject(
                    scope=f"{rel_path}::{name}",
                    path=rel_path,
                    name=name,
                    kind="dangerous_macro",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    threat_models=sorted(set(threats)),
                    recommended_techniques=techniques,
                    oracle_types=oracles,
                    risk_level="CRITICAL",
                    metadata={
                        "language": language,
                        "parser": f"tree-sitter-{language}",
                        "node_type": node_kind,
                    },
                ))
                return parents

            kind, threats, techniques, oracles, risk, metadata = self._classify_tree_surface(
                base_kind, snippet, language,
            )
            metadata["node_type"] = node_kind
            found.append(MineableSubject(
                scope=f"{rel_path}::{'.'.join([*parents, name])}",
                path=rel_path,
                name=name,
                kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                threat_models=sorted(set(threats)),
                recommended_techniques=sorted(set(techniques)),
                oracle_types=sorted(set(oracles)),
                risk_level=risk,
                metadata=metadata,
            ))
            return [*parents, name]

        def walk(node: object, parents: list[str]) -> None:
            node_type = node.type
            if node_type in TREE_SITTER_MACRO_NODES:
                name = self._tree_node_name(node)
                if name:
                    append_node(node, parents, "macro", name)
            elif node_type in TREE_SITTER_TYPE_NODES:
                name = self._tree_node_name(node)
                if name:
                    parents = append_node(node, parents, TREE_SITTER_TYPE_NODES[node_type], name)
            elif node_type in TREE_SITTER_FUNCTION_NODES:
                is_arrow_declaration = node_type == "variable_declarator" and any(
                    child.type in {"arrow_function", "function_expression"}
                    for child in node.named_children
                )
                if node_type != "variable_declarator" or is_arrow_declaration:
                    name = self._tree_node_name(node)
                    if name:
                        base_kind = "method" if node_type in {
                            "method_definition", "method_declaration", "constructor_declaration",
                        } or parents else "function"
                        parents = append_node(node, parents, base_kind, name)
            for child in node.named_children:
                walk(child, parents)

        walk(tree.root_node, [])
        found.extend(self._route_subjects(rel_path, content, language))
        if tree.root_node.has_error:
            self._record_error(
                f"tree-sitter parse {rel_path}",
                ValueError(f"{language} syntax tree contains ERROR nodes"),
            )
        return found

    def _append_universal_taint(
        self, found: list[MineableSubject], filepath: Path, rel_path: str,
        content: str, language: str | None,
    ) -> list[MineableSubject]:
        if not UniversalCodeMatcher:
            return found
        try:
            matcher = UniversalCodeMatcher(language=language or UniversalCodeMatcher.detect_language(filepath))
            parsed = matcher.parse_content(content, filepath, rel_path)
            for call in parsed.calls[:5]:
                if call.is_tainted:
                    found.append(MineableSubject(
                        scope=f"{rel_path}::{call.enclosing_function or call.callee}",
                        path=rel_path,
                        name=f"{call.method_name}_taint_sink",
                        kind="taint_sink",
                        start_line=call.span.line,
                        end_line=call.span.end_line or call.span.line,
                        threat_models=["command_injection", "rce", "sql_injection", "unsafe_deserialization"],
                        recommended_techniques=["guided-fuzz", "fault-injection"],
                        oracle_types=["invariant", "contract", "exit_code"],
                        risk_level="CRITICAL",
                        metadata={"language": language or parsed.language, "parser": "universal-code-matcher"},
                    ))
        except Exception as exc:
            self._record_error(f"universal parse {rel_path}", exc)
        return found

    def _scan_universal(
        self, filepath: Path, rel_path: str, content: str, *, language: str | None = None,
    ) -> list[MineableSubject]:
        language = language or self._language_for_path(filepath)
        if language in TREE_SITTER_SURFACE_LANGUAGES:
            try:
                found = self._scan_tree_sitter(filepath, rel_path, content, language)
                if found:
                    return self._append_universal_taint(found, filepath, rel_path, content, language)
            except Exception as exc:
                self._record_error(f"tree-sitter scan {rel_path}", exc)

        found: list[MineableSubject] = []
        lines = content.splitlines()
        if not lines:
            return found

        ext = filepath.suffix.lower()

        parsed = None
        if UniversalCodeMatcher:
            try:
                parsed = UniversalCodeMatcher.parse_file(filepath, rel_path=rel_path, content=content)
            except Exception as exc:
                self._record_error(f"universal parse {rel_path}", exc)

        func_headers: list[tuple[str, str, int]] = []
        if ext == ".go":
            for m in GO_FUNC_PATTERN.finditer(content):
                fn_name = m.group("name")
                recv = m.group("recv")
                start_l = content[:m.start()].count("\n") + 1
                scope_prefix = ""
                if recv:
                    recv_clean = re.sub(r"[*&]", "", recv).strip().split()
                    if recv_clean:
                        scope_prefix = f"{recv_clean[-1]}."
                func_headers.append((fn_name, scope_prefix, start_l))
        elif ext in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"):
            for m in C_CPP_FUNC_PATTERN.finditer(content):
                fn_name = m.group("name")
                cls_name = m.group("class")
                start_l = content[:m.start()].count("\n") + 1
                scope_prefix = f"{cls_name}." if cls_name else ""
                func_headers.append((fn_name, scope_prefix, start_l))
        elif ext in (".java", ".cs"):
            for m in JAVA_FUNC_PATTERN.finditer(content):
                fn_name = m.group("name")
                start_l = content[:m.start()].count("\n") + 1
                func_headers.append((fn_name, "", start_l))
        elif ext in (".kt", ".kts"):
            for m in KOTLIN_FUNC_PATTERN.finditer(content):
                fn_name = m.group("name")
                start_l = content[:m.start()].count("\n") + 1
                func_headers.append((fn_name, "", start_l))
        elif ext == ".rs":
            for m in RUST_FUNC_PATTERN.finditer(content):
                fn_name = m.group("name")
                start_l = content[:m.start()].count("\n") + 1
                func_headers.append((fn_name, "", start_l))
        elif ext in (".js", ".jsx", ".mjs", ".ts", ".tsx"):
            for m in JS_TS_FUNC_PATTERN.finditer(content):
                fn_name = m.group("name") or m.group("var_name") or "anonymous"
                start_l = content[:m.start()].count("\n") + 1
                func_headers.append((fn_name, "", start_l))

        if not func_headers:
            func_headers.append((filepath.stem, "", 1))

        func_headers.sort(key=lambda x: x[2])
        for idx, (fn_name, scope_prefix, start_l) in enumerate(func_headers):
            scope = f"{rel_path}::{scope_prefix}{fn_name}"
            next_start = func_headers[idx + 1][2] if idx + 1 < len(func_headers) else len(lines) + 1

            end_l = start_l
            depth = 0
            seen_open = False
            for line_idx in range(start_l - 1, min(len(lines), next_start - 1)):
                line = lines[line_idx]
                opens = line.count("{")
                closes = line.count("}")
                depth += opens - closes
                if opens > 0:
                    seen_open = True
                if seen_open and depth <= 0:
                    end_l = line_idx + 1
                    break
            else:
                end_l = max(start_l, next_start - 1)

            func_snippet = "\n".join(lines[max(0, start_l - 1):end_l])

            kind = "method" if scope_prefix else "function"
            threats: list[str] = ["boundary", "negative-control"]
            techniques: list[str] = ["boundary", "negative-control"]
            oracles: list[str] = ["invariant", "contract"]
            risk = "LOW"

            # 1. 内存安全检测 (C/C++, unsafe Rust/Go)
            if ext in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".rs", ".go"):
                if any(pat.search(func_snippet) for pat in MEMORY_SAFETY_PATTERNS):
                    kind = "memory_safety"
                    threats.extend(["buffer_overflow", "heap_corruption", "use_after_free", "out_of_bounds_write"])
                    techniques.extend(["guided-fuzz", "boundary", "fault-injection"])
                    oracles.extend(["memory_safety", "asan_crash", "crash_signal"])
                    risk = "CRITICAL"

            # 2. Web 路由检测
            if any(pat.search(func_snippet) for pat in UNIVERSAL_ROUTE_PATTERNS):
                kind = "http_route"
                threats.extend(["auth_bypass", "input_injection", "idor"])
                techniques.extend(["property-based", "guided-fuzz", "contract-differential"])
                oracles.extend(["status_code", "schema_validation"])
                risk = "CRITICAL" if any(kw in func_snippet.lower() for kw in ("admin", "auth", "user", "order", "pay", "token")) else "HIGH"

            # 3. CLI 入口检测
            if any(pat.search(func_snippet) for pat in UNIVERSAL_CLI_PATTERNS) or any(pat.search(func_snippet) for pat in CLI_PATTERNS):
                if kind in ("function", "method"):
                    kind = "cli_command"
                threats.extend(["argument_injection", "flag_abuse", "path_traversal"])
                techniques.extend(["guided-fuzz", "boundary"])
                if risk != "CRITICAL":
                    risk = "HIGH"

            # 4. 反序列化汇聚点
            if any(pat.search(func_snippet) for pat in UNIVERSAL_DESERIALIZE_PATTERNS) or any(pat.search(func_snippet) for pat in DESERIALIZE_PATTERNS):
                if kind not in ("http_route", "memory_safety"):
                    kind = "deserialization"
                threats.extend(["rce", "type_confusion", "resource_exhaustion"])
                techniques.extend(["fault-injection", "guided-fuzz"])
                oracles.extend(["memory_safety", "sandbox_integrity"])
                risk = "CRITICAL"

            # 5. 并发临界区与协程/锁争用
            if any(pat.search(func_snippet) for pat in UNIVERSAL_CONCURRENCY_PATTERNS) or any(pat.search(func_snippet) for pat in CONCURRENCY_PATTERNS):
                if kind in ("function", "method"):
                    kind = "critical_section"
                threats.extend(["race_condition", "deadlock", "lost_update"])
                techniques.extend(["concurrency-race"])
                oracles.append("concurrency_barrier")
                if risk == "LOW":
                    risk = "MEDIUM"

            # 6. 状态迁移检测
            if any(pat.search(func_snippet) for pat in STATE_TRANSITION_PATTERNS):
                threats.extend(["state_machine_bypass", "illegal_transition"])
                techniques.append("state-machine")
                oracles.append("transition_invariant")
                if risk == "LOW":
                    risk = "MEDIUM"

            found.append(MineableSubject(
                scope=scope,
                path=rel_path,
                name=fn_name,
                kind=kind,
                start_line=start_l,
                end_line=end_l,
                threat_models=sorted(set(threats)),
                recommended_techniques=sorted(set(techniques)),
                oracle_types=sorted(set(oracles)),
                risk_level=risk,
            ))

        if parsed and parsed.calls:
            for call in parsed.calls[:5]:
                if call.is_tainted:
                    found.append(MineableSubject(
                        scope=f"{rel_path}::{call.enclosing_function or call.callee}",
                        path=rel_path,
                        name=f"{call.method_name}_taint_sink",
                        kind="taint_sink",
                        start_line=call.span.line,
                        end_line=call.span.end_line or call.span.line,
                        threat_models=["command_injection", "rce", "sql_injection", "unsafe_deserialization"],
                        recommended_techniques=["guided-fuzz", "fault-injection"],
                        oracle_types=["invariant", "contract", "exit_code"],
                        risk_level="CRITICAL",
                    ))

        return found


class ThreatModelMatcher:
    """威胁模型匹配与挖掘用例合法性验证器。"""

    @staticmethod
    def validate_command_template(template: str) -> tuple[bool, str]:
        """验证挖掘命令模板合法性（占位符、参数注入、非破坏性）。"""
        if not template or not isinstance(template, str):
            return False, "命令模板不能为空"
        # 阻断直接 rm -rf 危险命令
        if re.search(r"\brm\s+-rf\s+[/~]", template):
            return False, "命令模板包含高危全盘删除指令"
        if "{input}" not in template and "{payload}" not in template and "{target}" not in template:
            return False, "命令模板缺少输入占位符 ({input} / {payload} / {target})"
        return True, "OK"

    @staticmethod
    def match_techniques_for_threats(threats: list[str]) -> list[dict[str, str]]:
        mapping = {
            "auth_bypass": {"name": "boundary", "oracle": "invariant", "reason": "无凭据与越权身份边界"},
            "input_injection": {"name": "guided-fuzz", "oracle": "contract", "reason": "特殊字符与反常编码注入"},
            "idor": {"name": "metamorphic", "oracle": "invariant", "reason": "对象ID置换与跨租户访问关系"},
            "argument_injection": {"name": "boundary", "oracle": "contract", "reason": "Shell参数逃逸与连字符注入"},
            "flag_abuse": {"name": "property-based", "oracle": "contract", "reason": "参数组合空间属性探测"},
            "path_traversal": {"name": "boundary", "oracle": "contract", "reason": "点斜杠相对路径穿透"},
            "rce": {"name": "fault-injection", "oracle": "invariant", "reason": "恶意载荷沙箱隔离与逃逸阻断"},
            "command_injection": {"name": "fault-injection", "oracle": "invariant", "reason": "命令注入与管道逃逸"},
            "sql_injection": {"name": "guided-fuzz", "oracle": "contract", "reason": "动态SQL拼接与注入测试"},
            "credential_leak": {"name": "boundary", "oracle": "invariant", "reason": "敏感凭据硬编码与泄露验证"},
            "reentrancy_deadlock": {"name": "concurrency", "oracle": "invariant", "reason": "循环依赖重入死锁与时序反常"},
            "state_corruption": {"name": "state-machine", "oracle": "invariant", "reason": "循环调用状态破坏与数据污染"},
            "race_condition": {"name": "concurrency", "oracle": "invariant", "reason": "并发时序与原子性竞争"},
            "deadlock": {"name": "concurrency", "oracle": "invariant", "reason": "多锁逆序争用与超时检测"},
            "lost_update": {"name": "concurrency", "oracle": "invariant", "reason": "无锁读改写丢失更新验证"},
            "state_machine_bypass": {"name": "state-machine", "oracle": "invariant", "reason": "跳步与反向状态跃迁校验"},
            "illegal_transition": {"name": "state-machine", "oracle": "invariant", "reason": "非法前置状态强行流转"},
            "buffer_overflow": {"name": "guided-fuzz", "oracle": "invariant", "reason": "缓冲区超长边界与内存覆写"},
            "heap_corruption": {"name": "guided-fuzz", "oracle": "invariant", "reason": "堆破坏与异常释放检测"},
            "use_after_free": {"name": "guided-fuzz", "oracle": "invariant", "reason": "悬挂指针与释放后引用检测"},
            "out_of_bounds_write": {"name": "boundary", "oracle": "invariant", "reason": "数组与指针越界写入"},
        }

        res: list[dict[str, str]] = []
        for t in threats:
            if t in mapping:
                res.append(mapping[t])
        if not res:
            res = [
                {"name": "boundary", "oracle": "contract", "reason": "默认边界探测"},
                {"name": "negative-control", "oracle": "invariant", "reason": "正反双向对照"},
            ]
        # 去重
        seen = set()
        deduped = []
        for r in res:
            if r["name"] not in seen:
                seen.add(r["name"])
                deduped.append(r)
        return deduped


class MiningPlanAuditor:
    """挖掘方案与用例四维质量量化审计评估器 (满分 100 分)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()

    def audit(self, plan: dict[str, Any], *, min_score: float = 80.0, strict: bool = False) -> dict[str, Any]:
        violations: list[str] = []
        dimensions = {
            "accuracy": {"score": 25.0, "max_score": 25.0, "issues": []},
            "completeness": {"score": 25.0, "max_score": 25.0, "issues": []},
            "effectiveness": {"score": 25.0, "max_score": 25.0, "issues": []},
            "comprehensiveness": {"score": 25.0, "max_score": 25.0, "issues": []},
        }

        units = plan.get("units", [])
        if not isinstance(units, list):
            units = []

        # ── 1. 准确性 (Accuracy, 25分) ──
        # 1.1 目标主体真实存在率 (10分)
        missing_count = 0
        for u in units:
            scope = u.get("scope", "")
            file_part = scope.split("::")[0] if "::" in scope else u.get("path", "")
            if file_part and not (self.root / file_part).exists():
                missing_count += 1
        if units and missing_count > 0:
            deduct = min(10.0, round((missing_count / len(units)) * 10.0, 2))
            dimensions["accuracy"]["score"] -= deduct
            msg = f"存在 {missing_count} 个引用不存在源文件的挖掘单元"
            dimensions["accuracy"]["issues"].append(msg)
            violations.append(f"[Accuracy] {msg}")

        # 1.2 挖掘命令模板与占位符合法性 (10分)
        invalid_cmd_count = 0
        for u in units:
            cmd = u.get("cmd_template") or u.get("command") or ""
            if cmd:
                valid, reason = ThreatModelMatcher.validate_command_template(cmd)
                if not valid:
                    invalid_cmd_count += 1
        if invalid_cmd_count > 0:
            deduct = min(10.0, invalid_cmd_count * 2.5)
            dimensions["accuracy"]["score"] -= deduct
            msg = f"{invalid_cmd_count} 个挖掘命令模板不合法或缺少占位符"
            dimensions["accuracy"]["issues"].append(msg)
            violations.append(f"[Accuracy] {msg}")

        # 1.3 威胁模型与目标匹配度 (5分)
        # 检查是否全部单元都是无脑挂单一策略
        all_techs = [t.get("name") if isinstance(t, dict) else t for u in units for t in u.get("techniques", [])]
        if units and len(set(all_techs)) <= 1 and len(units) >= 3:
            dimensions["accuracy"]["score"] -= 3.0
            dimensions["accuracy"]["issues"].append("挖掘策略手段过于单一，缺少针对目标特性的威胁模型匹配")

        # ── 2. 完整性 (Completeness, 25分) ──
        # 2.1 可挖掘功能主体覆盖度 (10分)
        explorer = MiningSurfaceExplorer(self.root)
        discovered = explorer.explore()
        planned_scopes = {u.get("scope") for u in units if u.get("scope")}
        planned_files = {u.get("path") for u in units if u.get("path")}

        uncovered_critical = [
            s for s in discovered
            if s.risk_level in ("CRITICAL", "HIGH")
            and s.scope not in planned_scopes
            and s.path not in planned_files
        ]
        if uncovered_critical:
            deduct = min(10.0, len(uncovered_critical) * 1.5)
            dimensions["completeness"]["score"] -= deduct
            msg = f"遗漏 {len(uncovered_critical)} 个高危可挖掘主体（如 Web 路由/CLI/反序列化点）未纳入计划"
            dimensions["completeness"]["issues"].append(msg)
            violations.append(f"[Completeness] {msg}")

        # 2.2 DAG 调度依赖闭合度 (8分)
        # 检查是否存在死循环依赖或未知依赖
        scope_set = {u.get("scope") for u in units if u.get("scope")}
        broken_prereqs = 0
        for u in units:
            prereqs = u.get("prerequisites", [])
            for p in prereqs:
                if p not in scope_set:
                    broken_prereqs += 1
        if broken_prereqs > 0:
            deduct = min(8.0, broken_prereqs * 2.0)
            dimensions["completeness"]["score"] -= deduct
            msg = f"存在 {broken_prereqs} 处前置依赖无法在挖掘计划中闭合解析"
            dimensions["completeness"]["issues"].append(msg)
            violations.append(f"[Completeness] {msg}")

        # 2.3 资源声明与资产完整性 (7分)
        # 针对需要凭据/会话的单元是否有资源说明
        needs_resource_units = [u for u in units if any(t in ("auth_bypass", "idor", "state-machine") for t in [
            t.get("name") if isinstance(t, dict) else str(t) for t in u.get("techniques", [])
        ])]
        missing_res = [u for u in needs_resource_units if not u.get("resources") and not u.get("prerequisites")]
        if missing_res:
            deduct = min(7.0, len(missing_res) * 1.5)
            dimensions["completeness"]["score"] -= deduct
            dimensions["completeness"]["issues"].append(f"{len(missing_res)} 个敏感/状态挖掘单元缺少资源上下文或前置依赖")

        # ── 3. 有效性 (Effectiveness, 25分) ──
        # 3.1 风险密度倾斜 (10分)
        high_risk_count = sum(1 for u in units if u.get("risk_score", 0) >= 60 or u.get("priority", 0) >= 60)
        if units and high_risk_count == 0 and len(discovered) > 0:
            dimensions["effectiveness"]["score"] -= 5.0
            dimensions["effectiveness"]["issues"].append("挖掘计划未建立风险倾斜，高危脆弱点未被优先排期")

        # 3.2 深度语义 Oracle 绑定率 (10分)
        # 非纯 Crash 判定，至少有 invariant / contract / differential / status_code
        semantic_oracle_count = 0
        for u in units:
            techs = u.get("techniques", [])
            oracles = [t.get("oracle") for t in techs if isinstance(t, dict)]
            if any(o in ("invariant", "contract", "differential", "standard") for o in oracles):
                semantic_oracle_count += 1
        if units and semantic_oracle_count < len(units) * 0.5:
            deduct = round((1 - (semantic_oracle_count / max(1, len(units)))) * 10.0, 2)
            dimensions["effectiveness"]["score"] -= deduct
            msg = f"语义 Oracle 绑定率仅为 {round(semantic_oracle_count / max(1, len(units)) * 100, 1)}%，过多依赖粗暴 Crash 退出码"
            dimensions["effectiveness"]["issues"].append(msg)
            violations.append(f"[Effectiveness] {msg}")

        # 3.3 路径有效性与死代码排除 (5分)
        empty_units = [u for u in units if u.get("lines_count", 1) == 0]
        if empty_units:
            dimensions["effectiveness"]["score"] -= min(5.0, len(empty_units) * 1.0)
            dimensions["effectiveness"]["issues"].append(f"包含 {len(empty_units)} 个空代码/无效文件单元")

        # ── 4. 全面性 (Comprehensiveness, 25分) ──
        # 4.1 挖掘技术手段多样性 (10分)
        unique_techniques = set(all_techs)
        if len(unique_techniques) >= 5:
            tech_score = 10.0
        elif len(unique_techniques) >= 3:
            tech_score = 7.0
        elif len(unique_techniques) >= 2:
            tech_score = 4.0
        else:
            tech_score = 1.0
        dimensions["comprehensiveness"]["score"] = tech_score
        if tech_score < 7.0:
            dimensions["comprehensiveness"]["issues"].append(f"规划的挖掘手段种类较少 ({len(unique_techniques)} 种)")

        # 4.2 多阶段挖掘场景谱系覆盖 (15分)
        # Happy Path / Boundary / Fault Injection / Concurrency / Stateful 各 3 分
        scenario_scores = 0.0
        has_boundary = any("boundary" in all_techs for _ in [1])
        has_negative = any("negative-control" in all_techs for _ in [1])
        has_fuzz = any("guided-fuzz" in all_techs or "property-based" in all_techs for _ in [1])
        has_concurrency = any("concurrency" in all_techs for _ in [1])
        has_state = any("state-machine" in all_techs or "fault-injection" in all_techs for _ in [1])

        if has_boundary:
            scenario_scores += 3.0
        if has_negative:
            scenario_scores += 3.0
        if has_fuzz:
            scenario_scores += 3.0
        if has_concurrency:
            scenario_scores += 3.0
        if has_state:
            scenario_scores += 3.0
        dimensions["comprehensiveness"]["score"] += scenario_scores
        if scenario_scores < 12.0:
            dimensions["comprehensiveness"]["issues"].append("挖掘场景未形成全谱系闭环（缺边界/负对照/模糊变异/并发/状态机中的某些场景）")

        # 归一化各维分数
        for k in dimensions:
            dimensions[k]["score"] = max(0.0, min(dimensions[k]["max_score"], round(dimensions[k]["score"], 1)))

        total_score = round(sum(d["score"] for d in dimensions.values()), 1)
        if total_score >= min_score and (not strict or len(violations) == 0):
            level = "PASS"
        elif total_score >= 60.0:
            level = "DEGRADED"
        else:
            level = "FAIL"

        return {
            "score": total_score,
            "level": level,
            "min_score": min_score,
            "strict_mode": strict,
            "units_count": len(units),
            "discovered_surfaces_count": len(discovered),
            "dimensions": dimensions,
            "violations": violations,
            "summary": {
                "high_risk_units": high_risk_count,
                "unique_techniques": sorted(unique_techniques),
                "uncovered_critical_surfaces": len(uncovered_critical),
                "exploration_errors": len(explorer.errors),
            },
            "exploration_errors": list(explorer.errors),
        }


class MiningPlanHardener:
    """挖掘方案自动加固器：补全缺失主体、完善依赖图与强化 Oracle。"""

    @staticmethod
    def harden(plan: dict[str, Any], root: Path) -> dict[str, Any]:
        root = root.resolve()
        hardened = dict(plan)
        units: list[dict[str, Any]] = list(hardened.get("units", []))

        existing_scopes = {u.get("scope") for u in units if u.get("scope")}
        explorer = MiningSurfaceExplorer(root)
        discovered = explorer.explore()

        added_count = 0
        for s in discovered:
            if s.scope not in existing_scopes and s.risk_level in ("CRITICAL", "HIGH"):
                # 自动补全高危隐式端点
                new_unit = {
                    "scope": s.scope,
                    "path": s.path,
                    "name": s.name,
                    "kind": s.kind,
                    "risk_score": 85.0 if s.risk_level == "CRITICAL" else 70.0,
                    # This score orders automatically discovered surfaces; it is
                    # a heuristic derived from static risk level, not measured evidence.
                    "risk_score_source": "heuristic_surface_risk_level",
                    "priority": 85 if s.risk_level == "CRITICAL" else 70,
                    "techniques": ThreatModelMatcher.match_techniques_for_threats(s.threat_models),
                    "prerequisites": [],
                    "resources": {"consumed": [], "produced": []},
                    "scaffold_ready": True,
                    "auto_hardened": True,
                }
                units.append(new_unit)
                existing_scopes.add(s.scope)
                added_count += 1

        # 强化已有单元中的弱 Oracle
        for u in units:
            techs = u.get("techniques", [])
            for t in techs:
                if isinstance(t, dict) and t.get("oracle") in (None, "", "crash"):
                    t["oracle"] = "invariant"
                    t["reason"] = (t.get("reason") or "") + " (由 Hardener 自动绑定不变量断言)"

        # 重新按优先级排序
        units.sort(key=lambda item: (-item.get("risk_score", item.get("priority", 0)), item.get("scope", "")))
        hardened["units"] = units
        hardened["hardened_additions_count"] = added_count
        hardened["exploration_errors"] = list(explorer.errors)
        return hardened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BugX 挖掘方案与用例全息保证框架")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. audit 子命令
    audit_parser = subparsers.add_parser("audit", help="对挖掘方案进行四维质量量化审计")
    audit_parser.add_argument("plan_file", type=Path, help="挖掘方案 JSON 文件路径")
    audit_parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    audit_parser.add_argument("--min-score", type=float, default=80.0, help="最低通过分数（默认 80.0）")
    audit_parser.add_argument("--strict", action="store_true", help="严格模式（有违规项即阻断）")
    audit_parser.add_argument(
        "--allow-degraded", action="store_true",
        help="明确接受降级方案；否则 DEGRADED 返回 2，要求调用方先取得用户决策",
    )
    audit_parser.add_argument("--json", action="store_true", help="以 JSON 格式输出审计结果")

    # 2. explore 子命令
    explore_parser = subparsers.add_parser("explore", help="勘测项目可挖掘功能主体（多语种 AST/Web路由/CLI/反序列化/临界区）")
    explore_parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    explore_parser.add_argument("--language", help="显式指定扫描语言；省略时按文件扩展名识别")
    explore_parser.add_argument("--json", action="store_true", help="以 JSON 格式输出勘测结果")

    # 3. harden 子命令
    harden_parser = subparsers.add_parser("harden", help="自动加固补全挖掘方案")
    harden_parser.add_argument("plan_file", type=Path, help="待加固挖掘方案 JSON 文件")
    harden_parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录")
    harden_parser.add_argument("--out", type=Path, help="加固后保存路径（默认打印到 stdout）")

    args = parser.parse_args(argv)

    if args.subcommand == "explore":
        try:
            explorer = MiningSurfaceExplorer(args.root, language=args.language)
        except ValueError as exc:
            parser.error(str(exc))
        surfaces = explorer.explore()
        if args.json:
            print(json.dumps([s.to_dict() for s in surfaces], indent=2, ensure_ascii=False))
        else:
            print(f"[mining-explorer] 在 {args.root} 中勘测到 {len(surfaces)} 个可挖掘功能主体：")
            for s in surfaces[:20]:
                print(f"  - [{s.risk_level}] {s.scope} ({s.kind}) -> 威胁: {','.join(s.threat_models[:3])}")
            if len(surfaces) > 20:
                print(f"  ... 另有 {len(surfaces) - 20} 个主体")
            for error in explorer.errors:
                print(f"  ⚠ {error}", file=sys.stderr)
        return 0

    if args.subcommand == "audit":
        if not args.plan_file.is_file():
            print(f"[mining-audit] ✗ 挖掘方案文件不存在: {args.plan_file}", file=sys.stderr)
            return 1
        try:
            plan_data = json.loads(args.plan_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[mining-audit] ✗ 读取方案 JSON 失败: {exc}", file=sys.stderr)
            return 1

        auditor = MiningPlanAuditor(args.root)
        report = auditor.audit(plan_data, min_score=args.min_score, strict=args.strict)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(f"[mining-audit] 评分: {report['score']} / 100 ({report['level']})")
            for dim, ddata in report["dimensions"].items():
                print(f"  - {dim}: {ddata['score']}/{ddata['max_score']}")
                for issue in ddata["issues"]:
                    print(f"      ⚠ {issue}")
            if report["violations"]:
                print("  违规拦截项:")
                for v in report["violations"]:
                    print(f"    ✗ {v}")
        # 退出码必须区分「验证通过」与「降级通过」：把 DEGRADED 和 PASS 一起
        # 返回 0，会让一个远低于阈值的方案被下游自动化当成「验证通过」放行，
        # 也就是「无法验证伪装成验证通过」。语义与 sast_probe 保持一致：
        # PASS=0 / FAIL=1 / DEGRADED=2（显式 --allow-degraded 才降为 0）。
        level = report["level"]
        if level == "PASS":
            return 0
        if level == "FAIL":
            return 1
        if args.strict:
            return 1
        if not args.json and not args.allow_degraded:
            print(f"  ⏸ 方案评分 {report['score']}/{report['min_score']} 仅达降级水平（{level}）；"
                  "需显式 --allow-degraded 才视为通过。")
        return 0 if args.allow_degraded else 2

    if args.subcommand == "harden":
        if not args.plan_file.is_file():
            print(f"[mining-harden] ✗ 挖掘方案文件不存在: {args.plan_file}", file=sys.stderr)
            return 1
        plan_data = json.loads(args.plan_file.read_text(encoding="utf-8"))
        hardened = MiningPlanHardener.harden(plan_data, args.root)
        output_str = json.dumps(hardened, indent=2, ensure_ascii=False)
        if args.out:
            args.out.write_text(output_str, encoding="utf-8")
            print(f"[mining-harden] ✓ 加固方案已写入: {args.out} (补全 {hardened['hardened_additions_count']} 个主体)")
        else:
            print(output_str)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
