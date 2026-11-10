#!/usr/bin/env python3
"""Lightweight, bounded code scale, complexity & risk analyzer for BugX (DOC-058).

Provides deterministic line-of-code (SLOC), comment, docstring, blank line,
language breakdown, module breakdown, test-to-code ratio, scale estimation,
cyclomatic complexity, God module/function detection, sensitive sinks scanning,
and intrinsic risk scoring using purely Python standard library primitives.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
import hashlib
import io
import math
import os
import re
import tokenize
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from core.planning.dynamic_flow_analyzer import DynamicFlowAnalyzer, FRAMEWORK_SOURCES
except ImportError:
    DynamicFlowAnalyzer = None
    FRAMEWORK_SOURCES = {}

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".vue": "vue",
    ".svelte": "svelte",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".less": "less",
    ".dart": "dart",
    ".lua": "lua",
    ".r": "r",
    ".sol": "solidity",
    ".proto": "protobuf",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".md": "markdown",
}

_CODE_LANGUAGES = {
    "python", "javascript", "typescript", "go", "java", "kotlin", "rust",
    "c", "cpp", "csharp", "swift", "shell", "ruby", "php", "sql", "vue",
    "svelte", "dart", "lua", "r", "solidity", "protobuf",
}

_TEST_DIR_PATTERNS = {"/test/", "/tests/", "/__tests__/", "/spec/", "/specs/", "/testing/", "/e2e/"}

_TEST_FILE_PREFIXES = ("test_", "spec_")
_TEST_FILE_SUFFIXES = (
    "_test.py", "_spec.py",
    "_test.go",
    ".test.js", ".spec.js", ".test.jsx", ".spec.jsx",
    ".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx",
    "test.js", "spec.js", "test.ts", "spec.ts",
    "test.py",
    "test.java", "tests.java", "test.kt", "tests.kt",
)

_SENSITIVE_SINKS = {
    "command_exec": re.compile(r"\b(exec|eval|system|popen|subprocess|spawn|child_process|Runtime\.getRuntime|ProcessBuilder)\b"),
    "dynamic_sql": re.compile(r"(?i)\b(execute|query|raw_query|rawQuery)\s*\(\s*(?:f['\"]|['\"].*?(?:%s|\?|\$\d|\{\w+\}))"),
    "unsafe_deserial": re.compile(r"\b(pickle\.loads?|yaml\.load|Marshal\.load|unserialize|readObject)\b"),
    "path_traversal": re.compile(r"\b(extractall|readFile|readFileSync|open)\s*\([^)]*(?:\.\./|\.\.\\)"),
    "weak_crypto": re.compile(r"\b(md5|sha1|DES|RC4)\b"),
}

_C_BRANCH_RE = re.compile(r"\b(if|else\s+if|for|while|switch|case|catch)\b|&&|\|\||\?")
_C_FUNC_RE = re.compile(r"\b(function|func|fn)\b|=>|\b(?:public|private|protected|static|\w+)\s+\w+\s*\([^)]*\)\s*\{")
_C_CLASS_RE = re.compile(r"\b(class|struct|interface|enum)\b")


def is_test_file(rel_path: str) -> bool:
    """Deterministic predicate to classify whether a file belongs to automated test suites."""
    norm = "/" + rel_path.replace("\\", "/").strip("/").casefold()
    if any(p in norm for p in _TEST_DIR_PATTERNS):
        return True
    filename = os.path.basename(norm)
    if any(filename.startswith(p) for p in _TEST_FILE_PREFIXES):
        return True
    if any(filename.endswith(s) for s in _TEST_FILE_SUFFIXES):
        return True
    return False


def _count_python(raw_bytes: bytes) -> tuple[int, int, int, int, int]:
    """Tokenize-assisted counter for Python files: (total, blank, comments, docstrings, sloc)."""
    lines = raw_bytes.decode("utf-8", errors="ignore").splitlines()
    total = len(lines)
    blank = sum(1 for l in lines if not l.strip())

    comment_lines: set[int] = set()
    docstring_lines: set[int] = set()
    code_lines: set[int] = set()

    try:
        tokens = list(tokenize.tokenize(io.BytesIO(raw_bytes).readline))
        prev_tok = None
        for tok in tokens:
            tok_type, _, (srow, _), (erow, _), _ = tok
            if tok_type == tokenize.COMMENT:
                comment_lines.add(srow)
            elif tok_type == tokenize.STRING:
                if prev_tok is None or prev_tok.type in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.ENCODING
                ):
                    for r in range(srow, erow + 1):
                        docstring_lines.add(r)
                else:
                    for r in range(srow, erow + 1):
                        code_lines.add(r)
            elif tok_type not in (
                tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                tokenize.ENCODING, tokenize.ENDMARKER
            ):
                for r in range(srow, erow + 1):
                    code_lines.add(r)
            if tok_type not in (tokenize.NL, tokenize.COMMENT, tokenize.ENCODING):
                prev_tok = tok
    except Exception:
        c = 0
        for l in lines:
            s = l.strip()
            if s.startswith("#"):
                c += 1
        sloc = max(0, total - blank - c)
        return total, blank, c, 0, sloc

    pure_comments = len(comment_lines - code_lines)
    pure_docstrings = len(docstring_lines - code_lines)
    sloc = len(code_lines)
    return total, blank, pure_comments, pure_docstrings, sloc


def _count_c_family(lines: list[str]) -> tuple[int, int, int, int, int]:
    """Line scanner for C-style comments (// and /* */): C, C++, Go, Java, TS, JS, Rust, C#, Swift."""
    total = len(lines)
    blank = 0
    comment = 0
    code = 0
    in_block = False

    for line in lines:
        s = line.strip()
        if not s:
            blank += 1
            continue
        if in_block:
            comment += 1
            if "*/" in s:
                in_block = False
                after = s.split("*/", 1)[1].strip()
                if after and not after.startswith("//"):
                    code += 1
            continue
        if s.startswith("/*"):
            comment += 1
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("//"):
            comment += 1
            continue
        code += 1
        if "/*" in s and "*/" not in s:
            in_block = True

    return total, blank, comment, 0, code


def _count_hash_comment(lines: list[str]) -> tuple[int, int, int, int, int]:
    """Line scanner for hash-based comments (#): Shell, Ruby, YAML, etc."""
    total = len(lines)
    blank = 0
    comment = 0
    code = 0
    for line in lines:
        s = line.strip()
        if not s:
            blank += 1
        elif s.startswith("#"):
            comment += 1
        else:
            code += 1
    return total, blank, comment, 0, code


def _count_generic(lines: list[str]) -> tuple[int, int, int, int, int]:
    """Fallback scanner: count blanks and non-blanks."""
    total = len(lines)
    blank = sum(1 for l in lines if not l.strip())
    code = total - blank
    return total, blank, 0, 0, code


def count_file_lines(
    path: Path | str, language: str, *, raw_bytes: bytes | None = None,
) -> tuple[int, int, int, int, int]:
    """Return (total, blank, comments, docstrings, sloc) for a given file.

    ``raw_bytes`` lets a caller that already read the file (``analyze_codebase``
    does) hand the bytes down instead of triggering a second disk read.  When
    it is omitted the file is read from ``path`` exactly as before.
    """
    if raw_bytes is None:
        try:
            raw_bytes = Path(path).read_bytes()
        except OSError:
            return 0, 0, 0, 0, 0

    if language == "python":
        return _count_python(raw_bytes)

    lines = raw_bytes.decode("utf-8", errors="ignore").splitlines()
    if language in {
        "javascript", "typescript", "go", "java", "kotlin", "rust",
        "c", "cpp", "csharp", "swift", "vue", "svelte", "dart", "solidity", "protobuf",
    }:
        return _count_c_family(lines)
    elif language in {"shell", "ruby", "yaml", "toml"}:
        return _count_hash_comment(lines)
    else:
        return _count_generic(lines)


def _analyze_python_ast(raw_bytes: bytes | str, sloc: int = 1) -> dict[str, Any]:
    """Extract AST-level complexity, cognitive load, Halstead volume, taints and imports."""
    try:
        tree = ast.parse(raw_bytes)
    except Exception:
        return {
            "cc": 1, "cognitive": 0, "max_depth": 1, "functions": 0, "classes": 0,
            "god_functions": 0, "taint_flows": [], "imports": [], "maintainability_index": 100.0,
        }

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.current_depth = 0
            self.max_d = 0
            self.branches = 0
            self.func_count = 0
            self.class_count = 0
            self.god_funcs = 0
            self.cognitive = 0
            self.nesting = 0
            self.operators: Counter[str] = Counter()
            self.operands: Counter[str] = Counter()
            self.taints: list[dict[str, Any]] = []
            self.critical_sinks: list[dict[str, Any]] = []
            self.mitigations: list[dict[str, Any]] = []
            self.imports: list[str] = []
            self.alias_map: dict[str, str] = {}
            self.local_params: set[str] = set()
            self.tainted_vars: set[str] = set()
            self.taint_kind_by_var: dict[str, str] = {}
            self.current_func_name: str = ""
            self.current_env: dict[str, Any] = {}

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self.imports.append(alias.name)
                if alias.asname:
                    self.alias_map[alias.asname] = alias.name
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            mod = node.module or ""
            if mod:
                self.imports.append(mod)
            for alias in node.names:
                full_name = f"{mod}.{alias.name}" if mod else alias.name
                target_name = alias.asname or alias.name
                self.alias_map[target_name] = full_name
                if full_name:
                    self.imports.append(full_name)
                if alias.name:
                    self.imports.append(alias.name)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.func_count += 1
            self.operators[node.__class__.__name__] += 1
            func_span = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            sub_branches = sum(
                1 for child in ast.walk(node)
                if isinstance(
                    child,
                    (
                        ast.If, ast.While, ast.For, ast.AsyncFor,
                        ast.ExceptHandler, ast.With, ast.AsyncWith, ast.Assert
                    ),
                )
            )
            if func_span > 100 or sub_branches > 20:
                self.god_funcs += 1

            old_params = self.local_params
            old_tainted = set(self.tainted_vars)
            old_func = self.current_func_name
            old_env = self.current_env
            self.current_func_name = node.name
            
            # 闭包感知：保留外层自由变量的污点标记，叠加函数入参
            new_params = {a.arg for a in node.args.args}
            self.local_params = new_params
            self.tainted_vars.update(new_params)

            if DynamicFlowAnalyzer:
                try:
                    analyzer = DynamicFlowAnalyzer(language="python")
                    self.current_env = analyzer.fold_python_function_scope(node)
                    for v in self.current_env.values():
                        if getattr(v, "is_tainted", False):
                            self.tainted_vars.add(v.name)
                except Exception:
                    self.current_env = {}
            else:
                self.current_env = {}

            self.nesting += 1
            old_d = self.current_depth
            self.current_depth += 1
            if self.current_depth > self.max_d:
                self.max_d = self.current_depth

            self.generic_visit(node)

            self.current_depth = old_d
            self.nesting -= 1
            self.local_params = old_params
            self.tainted_vars = old_tainted
            self.current_func_name = old_func
            self.current_env = old_env

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_count += 1
            self.operators["ClassDef"] += 1
            old_d = self.current_depth
            self.current_depth += 1
            if self.current_depth > self.max_d:
                self.max_d = self.current_depth
            self.generic_visit(node)
            self.current_depth = old_d

        def visit_If(self, node: ast.If) -> None:
            self.branches += 1
            self.operators["If"] += 1
            is_elif = getattr(node, "_is_elif", False)
            if is_elif:
                self.cognitive += 1
            else:
                self.cognitive += (1 + self.nesting)

            old_d = self.current_depth
            self.current_depth += 1
            if self.current_depth > self.max_d:
                self.max_d = self.current_depth

            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                node.orelse[0]._is_elif = True

            self.nesting += 1
            self.generic_visit(node)
            self.nesting -= 1
            self.current_depth = old_d

        def visit_While(self, node: ast.While) -> None:
            self.branches += 1
            self.operators["While"] += 1
            self.cognitive += (1 + self.nesting)
            old_d = self.current_depth
            self.current_depth += 1
            if self.current_depth > self.max_d:
                self.max_d = self.current_depth
            self.nesting += 1
            self.generic_visit(node)
            self.nesting -= 1
            self.current_depth = old_d

        def visit_For(self, node: ast.For | ast.AsyncFor) -> None:
            self.branches += 1
            self.operators[node.__class__.__name__] += 1
            self.cognitive += (1 + self.nesting)
            old_d = self.current_depth
            self.current_depth += 1
            if self.current_depth > self.max_d:
                self.max_d = self.current_depth
            self.nesting += 1
            self.generic_visit(node)
            self.nesting -= 1
            self.current_depth = old_d

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.visit_For(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            self.branches += 1
            self.operators["ExceptHandler"] += 1
            self.cognitive += (1 + self.nesting)
            self.nesting += 1
            self.generic_visit(node)
            self.nesting -= 1

        def visit_BoolOp(self, node: ast.BoolOp) -> None:
            self.branches += len(node.values) - 1
            self.cognitive += 1
            self.operators[node.op.__class__.__name__] += len(node.values) - 1
            self.generic_visit(node)

        def visit_Break(self, node: ast.Break) -> None:
            self.cognitive += 1
            self.operators["Break"] += 1
            self.generic_visit(node)

        def visit_Continue(self, node: ast.Continue) -> None:
            self.cognitive += 1
            self.operators["Continue"] += 1
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            self.operators["Assign"] += 1
            assigned_tainted = False
            taint_kind = ""

            # 1. 检查右值是否引入或传递未净化输入
            has_user_input = False
            for child in ast.walk(node.value):
                if isinstance(child, ast.Name):
                    if child.id in self.tainted_vars:
                        has_user_input = True
                    elif child.id in {"argv", "environ", "request", "params", "query", "user_input", "cmd", "filename", "filepath", "sql"}:
                        has_user_input = True
                elif isinstance(child, ast.Attribute) and child.attr in {"argv", "environ", "args", "form", "json", "params", "GET", "POST"}:
                    has_user_input = True

            # 2. 局部污点折叠：SQL/命令字符串插值与路径拼接
            if isinstance(node.value, ast.JoinedStr) and has_user_input:
                assigned_tainted = True
                taint_kind = "dynamic_sql"
            elif isinstance(node.value, ast.BinOp) and isinstance(node.value.op, (ast.Add, ast.Mod)) and has_user_input:
                assigned_tainted = True
                taint_kind = "dynamic_sql"
            elif (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "join"
                and has_user_input
            ):
                assigned_tainted = True
                taint_kind = "path_traversal"
            elif has_user_input:
                assigned_tainted = True
                taint_kind = "generic_taint"

            if assigned_tainted:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.tainted_vars.add(target.id)
                        if taint_kind:
                            self.taint_kind_by_var[target.id] = taint_kind

            # 3. 检查防御特征 (如 re.match / SafeLoader / shlex.quote)
            if isinstance(node.value, ast.Call):
                call_func = ""
                if isinstance(node.value.func, ast.Name):
                    call_func = node.value.func.id
                elif isinstance(node.value.func, ast.Attribute):
                    call_func = node.value.func.attr
                if call_func in {"match", "search", "escape", "quote", "int", "float", "isidentifier"}:
                    self.mitigations.append({
                        "kind": "validation",
                        "func": call_func,
                        "line": getattr(node, "lineno", 0),
                    })

            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            self.operators["Call"] += 1
            func_name = ""
            full_resolved = ""
            has_shell_false = False

            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                full_resolved = self.alias_map.get(func_name, func_name)
            elif isinstance(node.func, ast.Attribute):
                attr_name = node.func.attr
                val_name = ""
                if isinstance(node.func.value, ast.Name):
                    val_name = self.alias_map.get(node.func.value.id, node.func.value.id)
                func_name = attr_name
                full_resolved = f"{val_name}.{attr_name}" if val_name else attr_name
            elif (
                isinstance(node.func, ast.Call)
                and isinstance(node.func.func, ast.Name)
                and node.func.func.id == "getattr"
            ):
                if len(node.func.args) >= 2:
                    if isinstance(node.func.args[1], ast.Constant) and isinstance(node.func.args[1].value, str):
                        func_name = node.func.args[1].value
                        full_resolved = func_name
                    elif isinstance(node.func.args[1], ast.Name) and getattr(self, "current_env", {}):
                        v_obj = self.current_env.get(node.func.args[1].id)
                        if v_obj:
                            func_name = getattr(v_obj, "value", "")
                            full_resolved = func_name

            # 1. 绝对高危特征判定 (一票置顶 Sink)
            # 反序列化
            if "pickle.loads" in full_resolved or (full_resolved == "loads" and "pickle" in self.imports):
                self.critical_sinks.append({
                    "kind": "rce_deserialization",
                    "sink": full_resolved,
                    "line": getattr(node, "lineno", 0),
                })
            elif "yaml.load" in full_resolved:
                has_safe_loader = False
                for kw in node.keywords:
                    if kw.arg == "Loader":
                        kw_val = ast.unparse(kw.value) if hasattr(ast, "unparse") else ""
                        if "SafeLoader" in kw_val:
                            has_safe_loader = True
                if not has_safe_loader:
                    self.critical_sinks.append({
                        "kind": "rce_deserialization",
                        "sink": "yaml.load",
                        "line": getattr(node, "lineno", 0),
                    })
                else:
                    self.mitigations.append({
                        "kind": "safe_deserializer",
                        "sink": "yaml.SafeLoader",
                        "line": getattr(node, "lineno", 0),
                    })
            elif "yaml.safe_load" in full_resolved:
                self.mitigations.append({
                    "kind": "safe_deserializer",
                    "sink": "yaml.safe_load",
                    "line": getattr(node, "lineno", 0),
                })

            # 命令执行
            if full_resolved in {"os.system", "system", "posix.system"} or (
                full_resolved in {"subprocess.Popen", "Popen", "subprocess.call", "call", "subprocess.run", "run"}
            ):
                has_shell_true = False
                has_shell_false = False
                for kw in node.keywords:
                    if kw.arg == "shell":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            has_shell_true = True
                        elif isinstance(kw.value, ast.Constant) and kw.value.value is False:
                            has_shell_false = True

                if has_shell_true or full_resolved in {"os.system", "system"}:
                    self.critical_sinks.append({
                        "kind": "command_injection",
                        "sink": full_resolved,
                        "line": getattr(node, "lineno", 0),
                    })
                elif has_shell_false:
                    self.mitigations.append({
                        "kind": "safe_subprocess",
                        "sink": f"{full_resolved}(shell=False)",
                        "line": getattr(node, "lineno", 0),
                    })

            # 2. 污点汇聚流检测 (排除显式 safe_subprocess)
            target_sinks = {
                "system", "popen", "call", "check_call", "check_output", "run",
                "eval", "exec", "loads", "execute", "raw_query", "query",
                "render_template_string", "from_string", "open", "read",
            }
            if not has_shell_false and (func_name.lower() in target_sinks or full_resolved.lower() in target_sinks):
                for a in node.args:
                    arg_vars = {child.id for child in ast.walk(a) if isinstance(child, ast.Name)}
                    tainted_hits = arg_vars.intersection(self.tainted_vars)
                    if tainted_hits or isinstance(a, ast.JoinedStr):
                        hits = sorted(tainted_hits) if tainted_hits else ["<f-string>"]
                        self.taints.append({
                            "func": getattr(self, "current_func_name", "") or "unknown",
                            "sink": full_resolved or func_name,
                            "source_vars": hits,
                            "line": getattr(node, "lineno", 0),
                        })
                        break
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            self.operands[node.id] += 1
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            self.operands[str(type(node.value))] += 1
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)

    n1 = len(visitor.operators)
    n2 = len(visitor.operands)
    vocab = max(2, n1 + n2)
    length = max(1, sum(visitor.operators.values()) + sum(visitor.operands.values()))
    halstead_v = length * math.log2(vocab)

    cc = 1 + visitor.branches
    safe_sloc = max(1, sloc)
    raw_mi = 171.0 - 5.2 * math.log(max(1.0, halstead_v)) - 0.23 * cc - 16.2 * math.log(safe_sloc)
    norm_mi = round(max(0.0, min(100.0, raw_mi * 100.0 / 171.0)), 1)

    return {
        "cc": cc,
        "cognitive": visitor.cognitive,
        "cognitive_complexity": visitor.cognitive,
        "max_depth": max(1, visitor.max_d),
        "functions": visitor.func_count,
        "classes": visitor.class_count,
        "god_functions": visitor.god_funcs,
        "taint_flows": visitor.taints,
        "critical_sinks": visitor.critical_sinks,
        "mitigations": visitor.mitigations,
        "imports": visitor.imports,
        "halstead_volume": round(halstead_v, 2),
        "maintainability_index": norm_mi,
    }



def _analyze_c_family_structure(lines: list[str], sloc: int = 1, language: str = "") -> dict[str, Any]:
    """Lexical scanner for branch complexity, cognitive load, indentation depth, functions and classes."""
    branches = 0
    funcs = 0
    classes = 0
    max_depth = 0
    cognitive = 0
    imports: list[str] = []

    for l in lines:
        s = l.strip()
        if not s or s.startswith(("//", "/*", "*")):
            continue
        leading_spaces = len(l) - len(l.lstrip(" "))
        depth = leading_spaces // 2
        if depth > max_depth:
            max_depth = depth

        found_branches = len(_C_BRANCH_RE.findall(s))
        branches += found_branches
        if found_branches > 0:
            cognitive += (found_branches + depth)

        if _C_FUNC_RE.search(s):
            funcs += 1
        if _C_CLASS_RE.search(s):
            classes += 1

        m_imp = re.search(r"""(?:import|require)\s*\(?['"]([^'"]+)['"]""", s)
        if m_imp:
            imports.append(m_imp.group(1))

    taints: list[dict[str, Any]] = []
    dynamic_langs = {"javascript", "typescript", "php", "ruby"}
    if language in dynamic_langs:
        try:
            flow_analyzer = DynamicFlowAnalyzer(language=language)
            line_env = flow_analyzer.fold_line_scope(lines)
            tainted_vars = {k: v for k, v in line_env.items() if v.is_tainted}

            sink_patterns = [
                re.compile(r'\b(eval|exec|execSync|spawn|spawnSync|system|passthru|shell_exec|popen|proc_open)\s*\(([^)]*)\)'),
                re.compile(r'\b(query|execute|rawQuery)\s*\(([^)]*)\)'),
            ]
            for lineno, line in enumerate(lines, 1):
                s = line.strip().split("//")[0].split("#")[0].strip()
                if not s:
                    continue
                for pat in sink_patterns:
                    m = pat.search(s)
                    if m:
                        sink_name = m.group(1)
                        args_expr = m.group(2) if len(m.groups()) >= 2 else ""
                        hit_sources = [
                            vname for vname in tainted_vars
                            if re.search(rf"(?<![A-Za-z0-9_$]){re.escape(vname)}(?![A-Za-z0-9_$])", args_expr)
                        ]
                        if not hit_sources and flow_analyzer.has_framework_source(args_expr):
                            hit_sources = ["<framework_source>"]
                        if hit_sources:
                            taints.append({
                                "func": "unknown",
                                "sink": sink_name,
                                "source_vars": hit_sources,
                                "line": lineno,
                            })
                            break
        except Exception:
            taints = []

    safe_sloc = max(1, sloc)
    cc = 1 + branches
    halstead_v = safe_sloc * 4.5
    raw_mi = 171.0 - 5.2 * math.log(max(1.0, halstead_v)) - 0.23 * cc - 16.2 * math.log(safe_sloc)
    norm_mi = round(max(0.0, min(100.0, raw_mi * 100.0 / 171.0)), 1)

    return {
        "cc": cc,
        "cognitive": cognitive,
        "cognitive_complexity": cognitive,
        "max_depth": max(1, max_depth),
        "functions": funcs,
        "classes": classes,
        "god_functions": 0,
        "taint_flows": taints,
        "imports": imports,
        "halstead_volume": round(halstead_v, 2),
        "maintainability_index": norm_mi,
    }



def get_python_non_code_mask(raw_bytes: bytes) -> set[int]:
    """Extract line numbers (1-indexed) of comments and standalone docstrings."""
    mask: set[int] = set()
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(raw_bytes).readline))
        prev_tok = None
        for tok in tokens:
            tok_type, _, (srow, _), (erow, _), _ = tok
            if tok_type == tokenize.COMMENT:
                mask.add(srow)
            elif tok_type == tokenize.STRING:
                if prev_tok is None or prev_tok.type in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.ENCODING
                ):
                    for r in range(srow, erow + 1):
                        mask.add(r)
            if tok_type not in (tokenize.NL, tokenize.COMMENT, tokenize.ENCODING):
                prev_tok = tok
    except Exception:
        pass
    return mask


def get_c_family_non_code_mask(text: str) -> set[int]:
    """Extract line numbers of comments in C-family / JS / Go source code."""
    non_code_lines: set[int] = set()
    in_block_comment = False
    for idx, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if in_block_comment:
            non_code_lines.add(idx)
            if "*/" in s:
                in_block_comment = False
            continue
        if s.startswith("/*"):
            non_code_lines.add(idx)
            if "*/" not in s:
                in_block_comment = True
            continue
        if s.startswith(("//", "#", "*")):
            non_code_lines.add(idx)
    return non_code_lines


def scan_sensitive_sinks(
    text: str,
    *,
    non_code_mask: set[int] | None = None,
) -> int:
    """Return total count of sensitive sink patterns found in active code (excluding comments/docstrings)."""
    if not text:
        return 0

    lines = text.splitlines()
    line_offsets = []
    curr = 0
    for l in lines:
        line_offsets.append(curr)
        curr += len(l) + 1

    import bisect

    total = 0
    for sink_name, pattern in _SENSITIVE_SINKS.items():
        for m in pattern.finditer(text):
            start_pos = m.start()
            lineno = bisect.bisect_right(line_offsets, start_pos)
            if non_code_mask and lineno in non_code_mask:
                continue

            matched_text = m.group(0)
            if sink_name == "unsafe_deserial" and "yaml.load" in matched_text:
                line_content = lines[lineno - 1] if 0 <= lineno - 1 < len(lines) else ""
                if "SafeLoader" in line_content or "yaml.safe_load" in line_content:
                    continue

            total += 1
    return total


def calculate_shannon_entropy(text: str) -> float:
    """Calculate Shannon information entropy of a string."""
    if not text:
        return 0.0
    freq = Counter(text)
    length = float(len(text))
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


_SECRET_KEYWORD_RE = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|passwd|auth|bearer|credentials|private[_-]?key)"
)
_SECRET_VALUE_RE = re.compile(
    r"""['"]([a-zA-Z0-9_\-\.\$\/+=]{20,120})['"]"""
)


def scan_high_entropy_secrets(text: str) -> list[dict[str, Any]]:
    """Scan text for high-entropy strings and secret patterns."""
    secrets = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, 1):
        s_line = line.strip()
        if not s_line or s_line.startswith(("#", "//", "/*", "*")):
            continue
        has_secret_kw = bool(_SECRET_KEYWORD_RE.search(s_line))
        for m in _SECRET_VALUE_RE.finditer(s_line):
            val = m.group(1)
            if val.startswith(("http://", "https://", "ftp://", "file://")):
                continue
            if val.count("/") >= 3 or val.count("\\") >= 3:
                continue
            if re.match(r"^[0-9a-f]{32,64}$", val):
                continue
            if len(set(val)) <= 8:
                continue

            ent = calculate_shannon_entropy(val)
            if ent >= 4.3 or (has_secret_kw and ent >= 3.6):
                masked = val[:4] + "****" + val[-4:] if len(val) >= 12 else "****"
                secrets.append({
                    "line": idx,
                    "entropy": round(ent, 2),
                    "masked": masked,
                })
                if len(secrets) >= 5:
                    break
        if len(secrets) >= 5:
            break
    return secrets


def analyze_file_risk(
    path: Path | str,
    language: str,
    sloc: int,
    is_test: bool,
    *,
    raw_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Extract structural complexity, sensitive sinks, cognitive load and intrinsic risk for a single file.

    ``raw_bytes`` lets a caller that already read the file hand the bytes down so
    a single analysis pass reads every file at most once; omitting it preserves
    the original single-file behaviour.
    """
    if raw_bytes is None:
        try:
            raw_bytes = Path(path).read_bytes()
        except OSError:
            return {
                "cc": 1, "cognitive": 0, "max_depth": 1, "functions": 0, "classes": 0,
                "god_functions": 0, "god_module": False, "sinks": 0,
                "taint_flows": [], "critical_sinks": [], "mitigations": [], "secrets": [], "imports": [], "maintainability_index": 100.0,
                "risk_score": 0.0, "tags": [],
            }
    text = raw_bytes.decode("utf-8", errors="ignore")

    if language == "python":
        struct = _analyze_python_ast(raw_bytes, sloc=sloc)
        non_code_mask = get_python_non_code_mask(raw_bytes)
    elif language in {
        "javascript", "typescript", "go", "java", "kotlin", "rust",
        "c", "cpp", "csharp", "swift", "vue", "svelte", "dart",
        "php", "ruby",
    }:
        struct = _analyze_c_family_structure(text.splitlines(), sloc=sloc, language=language)
        non_code_mask = get_c_family_non_code_mask(text)
    else:
        struct = {
            "cc": 1, "cognitive": 0, "max_depth": 1, "functions": 0, "classes": 0,
            "god_functions": 0, "taint_flows": [], "critical_sinks": [], "mitigations": [], "imports": [], "maintainability_index": 100.0,
        }
        non_code_mask = set()

    sinks = scan_sensitive_sinks(text, non_code_mask=non_code_mask)
    secrets = scan_high_entropy_secrets(text) if not is_test else []
    taints = struct.get("taint_flows", [])
    critical_sinks = struct.get("critical_sinks", [])
    mitigations = struct.get("mitigations", [])
    cc = struct.get("cc", 1)
    cognitive = struct.get("cognitive", 0)
    max_depth = struct.get("max_depth", 1)
    mi = struct.get("maintainability_index", 100.0)

    # God Module: only for production code
    is_god_module = False
    if not is_test and (sloc > 1500 or cc > 80):
        is_god_module = True

    tags = []
    if is_god_module:
        tags.append("god-module")
    if struct.get("god_functions", 0) > 0:
        tags.append("god-function")
    if cc > 25:
        tags.append("high-cc")
    if cognitive > 30:
        tags.append("high-cognitive")
    if sinks >= 3:
        tags.append("sensitive-sinks")
    if max_depth >= 5:
        tags.append("deep-nesting")
    if len(taints) > 0:
        tags.append("critical-taint")
    if len(critical_sinks) > 0:
        tags.append("critical-sink")
    if len(mitigations) > 0:
        tags.append("mitigated-control")
    if len(secrets) > 0:
        tags.append("hardcoded-secret")
    if mi < 40.0:
        tags.append("low-maintainability")

    # Composite Risk score calculation (0 ~ 100) - 彻底废除复杂度即风险，确立高危一票置顶
    if len(critical_sinks) > 0 or len(taints) > 0:
        # 基准分直接锁定 80+
        base_score = 80.0 + min(len(critical_sinks) * 5.0 + len(taints) * 5.0, 15.0)
        # 如果存在有效防御且无裸露高危
        if len(mitigations) > 0 and len(critical_sinks) == 0:
            base_score = max(20.0, base_score - len(mitigations) * 30.0)
    else:
        # 无绝对高危 Sink 时，根据结构复杂度与防御进行基准打分，上限压制在 50 分以下
        base_score = min(
            45.0,
            (cc * 0.2)
            + (cognitive * 0.1)
            + (sinks * 3.0)
            + (len(secrets) * 6.0)
            + (max_depth * 1.5)
            + (10.0 if mi < 40.0 else 0.0),
        )

    risk_score = round(min(100.0, max(10.0, base_score)), 1)

    return {
        "cc": cc,
        "cognitive": cognitive,
        "max_depth": max_depth,
        "functions": struct.get("functions", 0),
        "classes": struct.get("classes", 0),
        "god_functions": struct.get("god_functions", 0),
        "god_module": is_god_module,
        "sinks": sinks,
        "taint_flows": taints,
        "critical_sinks": critical_sinks,
        "mitigations": mitigations,
        "secrets": secrets,
        "imports": struct.get("imports", []),
        "maintainability_index": mi,
        "risk_score": risk_score,
        "tags": tags,
    }


def compute_health_grade(avg_cc: float, god_modules: int, test_ratio: float) -> str:
    """Deterministic engineering health rating (A / B / C / D)."""
    if avg_cc <= 8 and god_modules == 0 and test_ratio >= 0.4:
        return "A"
    elif avg_cc <= 15 and god_modules <= 2 and test_ratio >= 0.2:
        return "B"
    elif avg_cc <= 25 or test_ratio >= 0.1:
        return "C"
    else:
        return "D"


def estimate_scale(sloc: int, primary_lang: str = "unknown") -> dict[str, Any]:
    """Estimate project scale level, recommended concurrency, and round budget."""
    if sloc < 5000:
        level = "micro"
        concurrency = 1
        rounds = 1
        budget = 30000
    elif sloc < 25000:
        level = "small"
        concurrency = 2
        rounds = 2
        budget = 80000
    elif sloc < 100000:
        level = "medium"
        concurrency = 3
        rounds = 3
        budget = 200000
    elif sloc < 500000:
        level = "large"
        concurrency = 4
        rounds = 5
        budget = 500000
    else:
        level = "giant"
        concurrency = 6
        rounds = 8
        budget = 1200000

    return {
        "scale_level": level,
        "primary_language": primary_lang,
        "recommended_concurrency": concurrency,
        "recommended_rounds": rounds,
        "recommended_token_budget": budget,
    }


def _window_digest(encoded_window: Sequence[bytes]) -> str:
    """Stable, collision-resistant digest for one clone-detection window.

    The builtin ``hash()`` of a ``tuple[str, ...]`` is salted per process
    (``PYTHONHASHSEED``), so clone detection used to drift between runs and was
    not reproducible.  ``blake2b`` is used instead: identical windows always map
    to the same digest in every process, and distinct windows cannot collide.
    Each line is length-prefixed so two different line sequences can never
    encode to the same payload.
    """
    hasher = hashlib.blake2b(digest_size=16)
    for line in encoded_window:
        hasher.update(len(line).to_bytes(4, "big"))
        hasher.update(line)
    return hasher.hexdigest()


def detect_code_duplication(
    project_root: Path | str,
    prod_files: list[Any],
    window_size: int = 8,
    max_scan_files: int = 150,
    *,
    content_cache: Mapping[Path, str] | None = None,
) -> dict[str, Any]:
    """Detect clone blocks and code duplication ratio using sliding window hashing.

    ``content_cache`` is an optional per-pass mapping of already-decoded file
    text (see ``analyze_codebase``) so a file is not read a second time just for
    duplication scanning.  A missing entry falls back to reading from disk, so
    direct callers keep the original behaviour.
    """
    root_path = Path(project_root)
    block_map: dict[str, list[tuple[str, int]]] = defaultdict(list)
    total_prod_lines = 0

    scan_targets = prod_files[:max_scan_files]
    for item in scan_targets:
        if isinstance(item, dict):
            rel = item.get("rel_path") or item.get("path", "")
            p = item.get("path")
            if not isinstance(p, Path):
                p = root_path / rel
        elif isinstance(item, (tuple, list)):
            rel = item[0]
            p = item[1]
            if not isinstance(p, Path):
                p = root_path / rel
        else:
            continue

        content = content_cache.get(Path(p)) if content_cache is not None else None
        if content is None:
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

        cleaned = []
        for line in content.splitlines():
            s = line.strip()
            if not s or s.startswith(("#", "//", "/*", "*", '"""', "'''")):
                continue
            if len(s) < 3 or s in {"{", "}", "(", ")", "[", "]", ":", ";"}:
                continue
            cleaned.append(s)

        total_prod_lines += len(cleaned)

        if len(cleaned) >= window_size:
            encoded = [line.encode("utf-8") for line in cleaned]
            for i in range(len(cleaned) - window_size + 1):
                h = _window_digest(encoded[i:i + window_size])
                block_map[h].append((rel, i))

    duplicated_files: set[str] = set()
    duplicated_line_count = 0
    top_clones: list[dict[str, Any]] = []

    for h, occurrences in block_map.items():
        if len(occurrences) < 2:
            continue
        distinct_occurrences = []
        last_file = ""
        last_idx = -100
        for f, idx in occurrences:
            if f != last_file or (idx - last_idx) >= window_size:
                distinct_occurrences.append((f, idx))
                last_file = f
                last_idx = idx

        if len(distinct_occurrences) >= 2:
            for f, _ in distinct_occurrences:
                duplicated_files.add(f)
            duplicated_line_count += window_size
            if len(top_clones) < 3:
                files_involved = sorted(set(f for f, _ in distinct_occurrences))
                top_clones.append({
                    "files": files_involved[:4],
                    "lines_count": window_size,
                    "occurrences": len(distinct_occurrences),
                })

    dup_ratio = round(min(100.0, (duplicated_line_count / max(1, total_prod_lines)) * 100.0), 1)
    return {
        "duplicated_lines": duplicated_line_count,
        "duplicate_lines": duplicated_line_count,
        "duplication_percentage": dup_ratio,
        "duplication_ratio": dup_ratio,
        "duplicated_files_count": len(duplicated_files),
        "top_clones": top_clones,
        "clone_blocks": top_clones,
    }


def analyze_architecture_dependencies(
    arg1: Any,
    arg2: Any = None,
) -> dict[str, Any]:
    """Analyze architecture dependency graph, detect circular dependencies via Tarjan SCC, and measure instability."""
    if arg2 is not None:
        file_import_map = arg2
    elif isinstance(arg1, dict):
        file_import_map = arg1
    elif isinstance(arg1, list):
        file_import_map = {}
        for item in arg1:
            if isinstance(item, dict):
                file_import_map[item.get("rel_path") or item.get("module", "")] = item.get("imports", [])
    else:
        file_import_map = {}

    all_files = set(file_import_map.keys())
    mod_to_file: dict[str, str] = {}
    for f in all_files:
        no_ext = f.rsplit(".", 1)[0]
        mod_to_file[no_ext] = f
        mod_to_file[no_ext.replace("/", ".")] = f
        mod_to_file[Path(f).name] = f
        mod_to_file[Path(f).stem] = f

    graph: dict[str, set[str]] = defaultdict(set)
    in_degrees: Counter[str] = Counter()
    out_degrees: Counter[str] = Counter()

    for src_file, raw_imports in file_import_map.items():
        for imp in raw_imports:
            target = mod_to_file.get(imp)
            if not target and "/" in imp:
                target = mod_to_file.get(imp.split("/")[-1])
            if target and target != src_file and target in all_files:
                graph[src_file].add(target)

    for src, targets in graph.items():
        out_degrees[src] += len(targets)
        for t in targets:
            in_degrees[t] += 1

    idx = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    cycles: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal idx
        indices[node] = idx
        lowlink[node] = idx
        idx += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph.get(node, ()):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif neighbor in on_stack:
                lowlink[node] = min(lowlink[node], indices[neighbor])

        if lowlink[node] == indices[node]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == node:
                    break
            if len(scc) > 1:
                cycles.append(scc)

    for node in list(all_files):
        if node not in indices:
            strongconnect(node)

    cycles.sort(key=len, reverse=True)

    unstable_modules = []
    module_couplings = []
    for f in all_files:
        ca = in_degrees[f]
        ce = out_degrees[f]
        instability = round(ce / (ca + ce), 2) if (ca + ce) > 0 else 0.0
        module_couplings.append({
            "module": f,
            "fan_in": ca,
            "fan_out": ce,
            "instability": instability,
        })
        if (ca + ce) > 0 and instability >= 0.75 and ce >= 2:
            unstable_modules.append({
                "file": f,
                "instability": instability,
                "fan_in": ca,
                "fan_out": ce,
            })

    unstable_modules.sort(key=lambda x: (x["instability"], x["fan_out"]), reverse=True)

    return {
        "total_cycles": len(cycles),
        "circular_dependencies_count": len(cycles),
        "cycles": [c[:6] for c in cycles[:5]],
        "circular_dependency_cycles": [c[:6] for c in cycles[:5]],
        "unstable_modules": unstable_modules[:5],
        "highest_instability_modules": unstable_modules[:5],
        "module_couplings": module_couplings,
        "avg_afferent_coupling": round(sum(in_degrees.values()) / max(1, len(all_files)), 1),
        "avg_efferent_coupling": round(sum(out_degrees.values()) / max(1, len(all_files)), 1),
    }


def calculate_cocomo_and_debt(
    sloc: int,
    god_modules: int = 0,
    god_functions: int = 0,
    cycles_count: int = 0,
    taint_count: int = 0,
    secrets_count: int = 0,
    high_cc_count: int = 0,
    sinks_count: int = 0,
    duplicate_lines: int = 0,
    secret_count: int = 0,
    complex_files_count: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Calculate COCOMO II development baseline and technical debt remediation effort."""
    eff_secrets = secrets_count or secret_count
    eff_high_cc = high_cc_count or complex_files_count

    ksloc = sloc / 1000.0
    effort_pm = max(0.1, round(2.4 * (ksloc ** 1.05), 1))
    schedule_months = max(0.5, round(2.5 * (effort_pm ** 0.38), 1))

    tech_debt = (
        (god_modules * 2.0)
        + (god_functions * 1.5)
        + (cycles_count * 1.0)
        + (taint_count * 0.5)
        + (eff_secrets * 0.5)
        + (eff_high_cc * 0.3)
        + (sinks_count * 0.1)
        + (duplicate_lines * 0.01)
    )
    debt_days = round(tech_debt, 1)
    debt_ratio = min(100.0, round((debt_days / max(1.0, effort_pm * 22.0)) * 100.0, 1))

    debt_breakdown = {
        "god_modules_debt_days": round(god_modules * 2.0, 1),
        "god_functions_debt_days": round(god_functions * 1.5, 1),
        "circular_cycles_debt_days": round(cycles_count * 1.0, 1),
        "taint_exposure_debt_days": round(taint_count * 0.5, 1),
        "secret_exposure_debt_days": round(eff_secrets * 0.5, 1),
        "high_cc_debt_days": round(eff_high_cc * 0.3, 1),
        "sinks_debt_days": round(sinks_count * 0.1, 1),
        "duplication_debt_days": round(duplicate_lines * 0.01, 1),
    }

    return {
        "effort_person_months": effort_pm,
        "cocomo_effort_pm": effort_pm,
        "estimated_schedule_months": schedule_months,
        "cocomo_schedule_months": schedule_months,
        "tech_debt_days": debt_days,
        "tech_debt_ratio_pct": debt_ratio,
        "tech_debt_ratio": debt_ratio,
        "debt_breakdown": debt_breakdown,
    }



def analyze_codebase(
    root: Path | str,
    files: Sequence[tuple[str, Path, int, str]] | None = None,
    *,
    max_files: int = 4000,
) -> dict[str, Any]:
    """Perform deterministic codebase line, scale, complexity & risk analysis."""
    project_root = Path(root).resolve()

    if files is None:
        files_list = []
        for dirpath, dirnames, filenames in os.walk(project_root, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in {
                ".git", ".bugx", ".opencode", ".codex", ".claude", ".cline",
                ".vscode", ".agents", ".cursor", ".gemini", "node_modules",
                ".venv", "venv", "__pycache__", "dist", "build", ".idea"
            } and not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                rel = str(p.relative_to(project_root).as_posix())
                files_list.append((rel, p, p.stat().st_size if p.is_file() else 0, ""))
                if len(files_list) >= max_files:
                    break
            if len(files_list) >= max_files:
                break
        files = files_list

    lang_stats: dict[str, dict[str, Any]] = {}
    module_stats: dict[str, dict[str, int]] = {}

    total_files = 0
    total_lines = 0
    total_sloc = 0
    total_comments = 0
    total_docstrings = 0
    total_blanks = 0

    code_files = 0
    code_sloc = 0
    doc_config_lines = 0

    prod_sloc = 0
    test_sloc = 0
    prod_files = 0
    test_files = 0

    # Risk metrics accumulators
    total_cc = 0
    max_cc = 0
    total_cognitive = 0
    max_cognitive = 0
    total_funcs = 0
    total_classes = 0
    total_god_funcs = 0
    total_god_modules = 0
    total_sinks = 0
    all_taints: list[dict[str, Any]] = []
    all_critical_sinks: list[dict[str, Any]] = []
    all_secrets: list[dict[str, Any]] = []
    maintainability_scores: list[float] = []
    file_import_map: dict[str, list[str]] = {}
    prod_files_list: list[tuple[str, Path, int, str]] = []
    # Per-pass cache of decoded production source text.  ``count_file_lines``,
    # ``analyze_file_risk`` and ``detect_code_duplication`` used to read every
    # file separately; the bytes are now read once here and handed down.  The
    # cache lives only for this ``analyze_codebase`` call, so it can never serve
    # stale content to a later pass over a changed workspace.
    prod_text_cache: dict[Path, str] = {}
    hotspots: list[dict[str, Any]] = []

    for rel, abs_path, size, _ in files:
        ext = Path(rel).suffix.casefold()
        lang = LANGUAGE_BY_EXTENSION.get(ext)
        if not lang:
            continue

        file_path = Path(abs_path)
        try:
            raw_bytes = file_path.read_bytes()
        except OSError:
            # count_file_lines() reported (0, 0, 0, 0, 0) for unreadable files
            # and the loop skipped them; keep that outcome without re-reading.
            continue

        tot, blk, com, doc, sloc = count_file_lines(file_path, lang, raw_bytes=raw_bytes)
        if tot == 0 and sloc == 0:
            continue

        total_files += 1
        total_lines += tot
        total_sloc += sloc
        total_comments += com
        total_docstrings += doc
        total_blanks += blk

        is_code = lang in _CODE_LANGUAGES
        is_test = is_test_file(rel)

        if is_code:
            code_files += 1
            code_sloc += sloc
            if is_test:
                test_sloc += sloc
                test_files += 1
            else:
                prod_sloc += sloc
                prod_files += 1
                prod_files_list.append((rel, abs_path, size, ""))

            # Risk and structural analysis
            risk_info = analyze_file_risk(file_path, lang, sloc, is_test, raw_bytes=raw_bytes)
            cc = risk_info["cc"]
            cog = risk_info.get("cognitive", 0)
            total_cc += cc
            if cc > max_cc:
                max_cc = cc
            total_cognitive += cog
            if cog > max_cognitive:
                max_cognitive = cog

            maintainability_scores.append(risk_info.get("maintainability_index", 100.0))
            if risk_info.get("critical_sinks"):
                for cs in risk_info["critical_sinks"]:
                    all_critical_sinks.append({
                        **cs,
                        "file": rel,
                        "location": rel,
                        "source_file": rel,
                    })
            if risk_info.get("taint_flows"):
                for tf in risk_info["taint_flows"]:
                    all_taints.append({
                        **tf,
                        "file": rel,
                        "location": rel,
                        "source_file": rel,
                    })
            if risk_info.get("secrets"):
                for sec in risk_info["secrets"]:
                    all_secrets.append({
                        **sec,
                        "file": rel,
                        "location": rel,
                        "source_file": rel,
                    })
            if risk_info.get("imports"):
                file_import_map[rel] = risk_info["imports"]

            total_funcs += risk_info["functions"]
            total_classes += risk_info["classes"]
            total_god_funcs += risk_info["god_functions"]
            if risk_info["god_module"]:
                total_god_modules += 1
            total_sinks += risk_info["sinks"]

            # Collect candidates for hotspots (production code with notable risk)
            if not is_test:
                prod_text_cache[file_path] = raw_bytes.decode("utf-8", errors="ignore")
                hotspots.append({
                    "file": rel,
                    "risk_score": risk_info["risk_score"],
                    "cyclomatic_complexity": cc,
                    "cognitive_complexity": cog,
                    "maintainability_index": risk_info.get("maintainability_index", 100.0),
                    "sensitive_sinks": risk_info["sinks"],
                    "max_depth": risk_info["max_depth"],
                    "sloc": sloc,
                    "tags": risk_info["tags"],
                })
        else:
            doc_config_lines += sloc

        # Language aggregate
        if lang not in lang_stats:
            lang_stats[lang] = {
                "files": 0, "total_lines": 0, "code_lines": 0,
                "comment_lines": 0, "docstring_lines": 0, "blank_lines": 0,
                "is_code": is_code,
            }
        lang_stats[lang]["files"] += 1
        lang_stats[lang]["total_lines"] += tot
        lang_stats[lang]["code_lines"] += sloc
        lang_stats[lang]["comment_lines"] += com
        lang_stats[lang]["docstring_lines"] += doc
        lang_stats[lang]["blank_lines"] += blk

        # Module aggregate
        parts = rel.split("/")
        if len(parts) > 2 and parts[0] in {"src", "packages", "apps", "pkg"}:
            mod = f"{parts[0]}/{parts[1]}"
        elif len(parts) > 1:
            mod = parts[0]
        else:
            mod = "(root)"

        if mod not in module_stats:
            module_stats[mod] = {"files": 0, "code_lines": 0, "total_lines": 0}
        module_stats[mod]["files"] += 1
        module_stats[mod]["code_lines"] += (sloc if is_code else 0)
        module_stats[mod]["total_lines"] += tot

    # Sort languages
    languages_breakdown = []
    primary_lang = "unknown"
    highest_code_sloc = -1

    for lang, s in sorted(
        lang_stats.items(),
        key=lambda x: (1 if x[1]["is_code"] else 0, x[1]["code_lines"]),
        reverse=True,
    ):
        if s["is_code"] and s["code_lines"] > highest_code_sloc:
            highest_code_sloc = s["code_lines"]
            primary_lang = lang

        base_sloc = code_sloc if s["is_code"] else max(1, doc_config_lines)
        ratio = round((s["code_lines"] / max(1, base_sloc)) * 100, 1)
        languages_breakdown.append({
            "language": lang,
            "is_code": s["is_code"],
            "files": s["files"],
            "total_lines": s["total_lines"],
            "code_lines": s["code_lines"],
            "comment_lines": s["comment_lines"],
            "docstring_lines": s["docstring_lines"],
            "blank_lines": s["blank_lines"],
            "percentage": ratio,
        })

    # Sort modules
    sorted_modules = sorted(module_stats.items(), key=lambda x: x[1]["code_lines"], reverse=True)
    modules_breakdown = []
    other_mod = {"files": 0, "code_lines": 0, "total_lines": 0}

    for idx, (mod, s) in enumerate(sorted_modules):
        if idx < 15:
            ratio = round((s["code_lines"] / max(1, code_sloc if code_sloc > 0 else total_sloc)) * 100, 1)
            modules_breakdown.append({
                "module": mod,
                "files": s["files"],
                "code_lines": s["code_lines"],
                "total_lines": s["total_lines"],
                "percentage": ratio,
            })
        else:
            other_mod["files"] += s["files"]
            other_mod["code_lines"] += s["code_lines"]
            other_mod["total_lines"] += s["total_lines"]

    if other_mod["files"] > 0:
        ratio = round((other_mod["code_lines"] / max(1, code_sloc if code_sloc > 0 else total_sloc)) * 100, 1)
        modules_breakdown.append({
            "module": "other",
            "files": other_mod["files"],
            "code_lines": other_mod["code_lines"],
            "total_lines": other_mod["total_lines"],
            "percentage": ratio,
        })

    test_to_code_ratio = round(test_sloc / max(1, prod_sloc), 3) if prod_sloc > 0 else 0.0
    scale = estimate_scale(code_sloc if code_sloc > 0 else total_sloc, primary_lang)

    avg_cc = round(total_cc / max(1, code_files), 1) if code_files > 0 else 1.0
    avg_cog = round(total_cognitive / max(1, code_files), 1) if code_files > 0 else 0.0
    avg_mi = round(sum(maintainability_scores) / max(1, len(maintainability_scores)), 1) if maintainability_scores else 100.0
    mi_grade = "🟢 良好" if avg_mi >= 65 else ("🟡 关注" if avg_mi >= 40 else "🔴 恶化")

    health_grade = compute_health_grade(avg_cc, total_god_modules, test_to_code_ratio)

    # Duplication analysis
    duplication_info = detect_code_duplication(
        project_root, prod_files_list, content_cache=prod_text_cache,
    )

    # Architecture topology & cycles analysis
    architecture_info = analyze_architecture_dependencies(project_root, file_import_map)

    # COCOMO & Technical debt calculation
    high_cc_count = sum(1 for h in hotspots if h["cyclomatic_complexity"] > 25)
    cocomo_debt_info = calculate_cocomo_and_debt(
        prod_sloc,
        total_god_modules,
        total_god_funcs,
        architecture_info.get("total_cycles", 0),
        len(all_taints),
        len(all_secrets),
        high_cc_count,
        total_sinks,
    )

    # Sort hotspots by risk_score descending, keep top 5
    hotspots.sort(key=lambda x: x["risk_score"], reverse=True)
    top_hotspots = hotspots[:5]

    return {
        "summary": {
            "total_files": total_files,
            "total_lines": total_lines,
            "code_lines": code_sloc if code_sloc > 0 else total_sloc,
            "doc_and_config_lines": doc_config_lines,
            "comment_lines": total_comments,
            "docstring_lines": total_docstrings,
            "blank_lines": total_blanks,
            "production_code_lines": prod_sloc,
            "production_files": prod_files,
            "test_code_lines": test_sloc,
            "test_files": test_files,
            "test_to_code_ratio": test_to_code_ratio,
        },
        "languages": languages_breakdown,
        "modules": modules_breakdown,
        "scale_estimation": scale,
        "maintainability": {
            "maintainability_index": avg_mi,
            "avg_maintainability_index": avg_mi,
            "maintainability_grade": mi_grade,
            "avg_cognitive_complexity": avg_cog,
            "max_cognitive_complexity": max_cognitive,
        },

        "duplication": duplication_info,
        "architecture_topology": architecture_info,
        "engineering_economics": cocomo_debt_info,
        "risk_metrics": {
            "health_grade": health_grade,
            "avg_cyclomatic_complexity": avg_cc,
            "max_cyclomatic_complexity": max_cc,
            "total_functions": total_funcs,
            "total_classes": total_classes,
            "god_modules_count": total_god_modules,
            "god_functions_count": total_god_funcs,
            "total_sensitive_sinks": total_sinks,
            "total_taint_flows": len(all_taints),
            "total_hardcoded_secrets": len(all_secrets),
            "secrets": all_secrets,
            "critical_sinks": all_critical_sinks,
            "taint_flows": all_taints,
            "maintainability_index": avg_mi,
            "avg_cognitive_complexity": avg_cog,
            "duplication_percentage": duplication_info.get("duplication_percentage", 0.0),
            "circular_dependencies_count": architecture_info.get("total_cycles", 0),
            "tech_debt_days": cocomo_debt_info.get("tech_debt_days", 0.0),
            "top_risk_hotspots": top_hotspots,
        },
    }
