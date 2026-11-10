#!/usr/bin/env python3
"""core/universal_code_matcher.py — 零依赖通用多语言结构化分词与模式匹配引擎 (DOC-061)。

用于在无外部工具（如 Semgrep）时，为 Go、Java、Kotlin、TypeScript、JavaScript、
Rust、C/C++、PHP、Python、C#、Swift、Ruby 提供确定性的代码结构抽象、注释/字符串隔离与模式匹配。

核心设计：
1. 语言感知分词（Language-Aware Lexing）：剥离单行/多行注释与字符串字面量；
2. 行级/块级抑制提取：自动捕获 `// bugx-ignore` / `# bugx-ignore` / `nosec`；
3. 分界符平衡块提取（Delimited Block Tree）：按 `{}` 抽取代码块，按 `()` 抽取调用；
4. 结构化调用与拼接匹配器：支持通配函数调用、链式方法与动态字符串拼接检测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 扩展名到语言标识映射
EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".php": "php",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
}


@dataclass
class CodeSpan:
    line: int
    col: int
    end_line: int
    end_col: int
    text: str
    snippet: str = ""


@dataclass
class CallNode:
    """提取的函数/方法调用节点"""
    callee: str               # 被调用者名称，如 "exec.Command", "Runtime.getRuntime().exec", "os.system"
    base_obj: str             # 主调对象，如 "exec", "child_process", "cursor"
    method_name: str          # 末尾方法名，如 "Command", "exec", "execute"
    args_raw: str             # 括号内原始实参字符串
    args: list[str]           # 拆分后的实参列表
    has_string_concat: bool   # 实参中是否包含字符串动态拼接（+、fmt.Sprintf、反引号模版等）
    span: CodeSpan
    enclosing_function: str = ""
    resolved_callee: str = "" # 经局部常量折叠/解混淆还原后的被调用者名
    resolved_args: str = ""   # 经局部变量替换/折叠后的实参列表字符串
    is_tainted: bool = False  # 实参是否已被 Web 框架 Source 污染


@dataclass
class ParsedSource:
    filepath: Path
    rel_path: str
    language: str
    lines: list[str]
    clean_lines: list[str]                  # 注释与字符串替换后的净代码行
    suppressions: dict[int, set[str]]       # line_no -> set of suppressed rule_ids (empty set means ignore all)
    calls: list[CallNode]                   # 提取出的结构化调用列表
    raw_text: str


class UniversalCodeMatcher:
    """多语言结构化模式匹配器"""

    def __init__(self, language: str = "generic") -> None:
        self.language = language

    @staticmethod
    def detect_language(path: Path | str) -> str:
        p = Path(path)
        ext = p.suffix.lower()
        return EXT_TO_LANG.get(ext, "generic")

    @classmethod
    def parse_file(cls, filepath: Path, rel_path: str | None = None, content: str | None = None) -> ParsedSource:
        lang = cls.detect_language(filepath)
        if rel_path is None:
            rel_path = str(filepath)
        if content is None:
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
        matcher = cls(language=lang)
        return matcher.parse_content(content, filepath, rel_path)

    def parse_content(self, text: str, filepath: Path, rel_path: str) -> ParsedSource:
        lines = text.splitlines()
        suppressions = self._extract_suppressions(lines)
        clean_text, clean_lines = self._strip_comments_and_strings(text, self.language)
        calls = self._extract_calls(clean_text, text, lines)

        # 动态变量流推导与局部符号常量折叠下沉 (DOC-064)
        try:
            from core.planning.dynamic_flow_analyzer import DynamicFlowAnalyzer
        except ImportError:
            DynamicFlowAnalyzer = None

        if DynamicFlowAnalyzer:
            try:
                analyzer = DynamicFlowAnalyzer(language=self.language)
                var_env = analyzer.fold_line_scope(lines)
                if self.language == "python":
                    try:
                        import ast
                        tree = ast.parse(text, filename=str(filepath))
                        for node in ast.walk(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                var_env.update(analyzer.fold_python_function_scope(node))
                    except Exception:
                        pass

                for call in calls:
                    rc, ra, it = analyzer.resolve_call_target(
                        call.callee, call.args_raw, var_env, snippet=call.span.snippet
                    )
                    call.resolved_callee = rc or call.callee
                    call.resolved_args = ra or call.args_raw
                    call.is_tainted = it
            except Exception:
                for call in calls:
                    if not call.resolved_callee:
                        call.resolved_callee = call.callee
                    if not call.resolved_args:
                        call.resolved_args = call.args_raw
        else:
            for call in calls:
                call.resolved_callee = call.callee
                call.resolved_args = call.args_raw

        return ParsedSource(
            filepath=filepath,
            rel_path=rel_path,
            language=self.language,
            lines=lines,
            clean_lines=clean_lines,
            suppressions=suppressions,
            calls=calls,
            raw_text=text,
        )

    def _extract_suppressions(self, lines: list[str]) -> dict[int, set[str]]:
        """从注释中提取行级抑制信息。
        支持语法:
          // bugx-ignore: RULE-001, RULE-002 [reason]
          # bugx-ignore: RULE-001
          // bugx-ignore (忽略该行全部规则)
          # nosec (兼容 Bandit 忽略标记)
        """
        suppressions: dict[int, set[str]] = {}
        ignore_pattern = re.compile(r"(?:#|//|/\*)\s*(?:bugx-ignore(?::\s*([A-Za-z0-9_,\-\s]+))?|nosec\b)", re.IGNORECASE)

        for lineno, line in enumerate(lines, 1):
            m = ignore_pattern.search(line)
            if m:
                rules_str = m.group(1)
                if rules_str:
                    rule_ids = {r.strip().upper() for r in rules_str.split(",") if r.strip()}
                    suppressions[lineno] = rule_ids
                else:
                    # 忽略全部
                    suppressions[lineno] = set()
        return suppressions

    def _strip_comments_and_strings(self, text: str, lang: str) -> tuple[str, list[str]]:
        """语言感知分词：将注释替换为空格，将字符串内容替换为标号以保留位置，生成净代码。"""
        chars = list(text)
        length = len(chars)
        i = 0
        in_single_quote = False
        in_double_quote = False
        in_backtick = False
        in_line_comment = False
        in_block_comment = False

        is_python_like = lang in {"python", "ruby", "shell"}

        while i < length:
            c = chars[i]
            c_next = chars[i + 1] if i + 1 < length else ""

            # 处理多行注释结束
            if in_block_comment:
                if c == "*" and c_next == "/":
                    chars[i] = " "
                    chars[i + 1] = " "
                    i += 2
                    in_block_comment = False
                    continue
                if c != "\n":
                    chars[i] = " "
                i += 1
                continue

            # 处理单行注释结束
            if in_line_comment:
                if c == "\n":
                    in_line_comment = False
                else:
                    chars[i] = " "
                i += 1
                continue

            # 处理字符串转义
            if (in_single_quote or in_double_quote or in_backtick) and c == "\\":
                chars[i] = " "
                if i + 1 < length and chars[i + 1] != "\n":
                    chars[i + 1] = " "
                    i += 2
                else:
                    i += 1
                continue

            # 处理反引号字符串 (JS/TS 模板字符串, Go 反引号字符串)
            if in_backtick:
                if c == "`":
                    in_backtick = False
                else:
                    chars[i] = " "
                i += 1
                continue

            # 处理双引号字符串
            if in_double_quote:
                if c == '"':
                    in_double_quote = False
                else:
                    chars[i] = " "
                i += 1
                continue

            # 处理单引号字符串
            if in_single_quote:
                if c == "'":
                    in_single_quote = False
                else:
                    chars[i] = " "
                i += 1
                continue

            # 未在字符串或注释内时，判定进入
            if is_python_like:
                if c == "#":
                    chars[i] = " "
                    in_line_comment = True
                    i += 1
                    continue
            else:
                if c == "/" and c_next == "/":
                    chars[i] = " "
                    chars[i + 1] = " "
                    in_line_comment = True
                    i += 2
                    continue
                if c == "/" and c_next == "*":
                    chars[i] = " "
                    chars[i + 1] = " "
                    in_block_comment = True
                    i += 2
                    continue

            if c == '"':
                in_double_quote = True
                i += 1
                continue
            if c == "'":
                in_single_quote = True
                i += 1
                continue
            if c == "`" and lang in {"javascript", "typescript", "go"}:
                in_backtick = True
                i += 1
                continue

            i += 1

        clean_text = "".join(chars)
        return clean_text, clean_text.splitlines()

    def _extract_calls(self, clean_text: str, original_text: str, lines: list[str]) -> list[CallNode]:
        """从净代码中利用平衡括号扫描抽取所有函数与方法调用。"""
        calls: list[CallNode] = []
        call_regex = re.compile(r"\b([A-Za-z_][A-Za-z0-9_\.]*)\s*\(")

        for match in call_regex.finditer(clean_text):
            callee = match.group(1)
            start_pos = match.start(1)

            # 排除函数定义关键字 (func, def, function, fn)
            prev_slice = clean_text[:start_pos].rstrip()
            last_word = prev_slice.split()[-1] if prev_slice.split() else ""
            if last_word in {"func", "def", "function", "fn"}:
                continue

            open_paren_idx = match.end() - 1

            # 寻找平衡的闭合圆括号
            depth = 0
            close_paren_idx = -1
            for j in range(open_paren_idx, len(clean_text)):
                ch = clean_text[j]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        close_paren_idx = j
                        break
            if close_paren_idx == -1:
                continue

            # 提取参数文本（来自原文本，保留字符串与变量）
            raw_args = original_text[open_paren_idx + 1: close_paren_idx].strip()
            args = [a.strip() for a in self._split_arguments(raw_args) if a.strip()]

            # 判定是否包含字符串拼接
            has_concat = bool(
                re.search(r'(\+\s*["\']|["\']\s*\+|\bfmt\.Sprintf\b|\bString\.format\b|`[^`]*\${)', raw_args)
                or ("+" in raw_args and any(q in raw_args for q in ('"', "'", "`")))
            )

            # 计算行号列号
            start_pos = match.start(1)
            line_no = clean_text[:start_pos].count("\n") + 1
            line_start = clean_text.rfind("\n", 0, start_pos)
            col_offset = start_pos if line_start == -1 else (start_pos - line_start - 1)

            snippet = lines[line_no - 1].strip() if 1 <= line_no <= len(lines) else ""
            span = CodeSpan(
                line=line_no,
                col=col_offset,
                end_line=clean_text[:close_paren_idx].count("\n") + 1,
                end_col=0,
                text=original_text[start_pos: close_paren_idx + 1],
                snippet=snippet,
            )

            parts = callee.split(".")
            base_obj = parts[0] if len(parts) > 1 else ""
            method_name = parts[-1]

            calls.append(CallNode(
                callee=callee,
                base_obj=base_obj,
                method_name=method_name,
                args_raw=raw_args,
                args=args,
                has_string_concat=has_concat,
                span=span,
            ))

        return calls

    @staticmethod
    def _split_arguments(args_str: str) -> list[str]:
        """在括号和逗号平衡下分割实参。"""
        if not args_str:
            return []
        args = []
        current: list[str] = []
        paren_depth = 0
        bracket_depth = 0
        brace_depth = 0
        in_quote = False
        quote_char = ""

        for char in args_str:
            if in_quote:
                current.append(char)
                if char == quote_char:
                    in_quote = False
                continue

            if char in {'"', "'", "`"}:
                in_quote = True
                quote_char = char
                current.append(char)
                continue

            if char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth -= 1
            elif char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth -= 1
            elif char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1

            if char == "," and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(char)

        if current:
            args.append("".join(current).strip())
        return args

    @staticmethod
    def is_line_suppressed(parsed: ParsedSource, lineno: int, rule_id: str) -> bool:
        """检查指定行是否被抑制。"""
        if lineno not in parsed.suppressions:
            return False
        supp_rules = parsed.suppressions[lineno]
        if not supp_rules:
            # 空集合代表抑制该行全部规则
            return True
        return rule_id.upper() in supp_rules
