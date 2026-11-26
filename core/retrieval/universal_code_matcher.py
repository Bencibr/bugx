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

import ast
import re
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: 行级抑制注释的**唯一**解析口径：``# bugx-ignore[: RULE-A, RULE-B] 理由`` /
#: ``// bugx-ignore`` / ``# nosec``。规则名限定为逗号分隔的标识符列表，其后的
#: 自由文本（理由）不会被吞进规则集合——旧实现用 ``[A-Za-z0-9_,\-\s]+`` 会把
#: 英文理由当成规则名，导致「写了抑制却仍报警」。
_SUPPRESSION_LINE_RE = re.compile(
    r"(?:#|//|/\*)\s*(?:bugx-ignore(?::\s*"
    r"([A-Za-z0-9][A-Za-z0-9_\-]*(?:\s*,\s*[A-Za-z0-9][A-Za-z0-9_\-]*)*)"
    r")?|nosec\b)",
    re.IGNORECASE,
)


def extract_suppressions(lines: Sequence[str]) -> dict[int, set[str]]:
    """按行提取抑制信息：``{行号: 规则集合}``，空集合表示抑制该行全部规则。

    Python AST 扫描器与多语言引擎共用这一份解析，避免两条路径口径漂移
    （AST 路径此前只认「有没有注释」、不认规则名，等于抑制了整行）。
    """
    suppressions: dict[int, set[str]] = {}
    for lineno, line in enumerate(lines, 1):
        match = _SUPPRESSION_LINE_RE.search(line)
        if not match:
            continue
        rules_str = match.group(1)
        if rules_str:
            suppressions[lineno] = {
                rule.strip().upper() for rule in rules_str.split(",") if rule.strip()
            }
        else:
            suppressions[lineno] = set()
    return suppressions


def is_suppressed(suppressions: Mapping[int, set[str]], lineno: int, rule_id: str) -> bool:
    """该行是否抑制了 ``rule_id``：空集合 = 整行抑制，否则按规则名精确匹配。"""
    if lineno not in suppressions:
        return False
    suppressed_rules = suppressions[lineno]
    if not suppressed_rules:
        return True
    return rule_id.upper() in suppressed_rules

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
    ".dart": "dart",
    ".sh": "shell",
    ".bash": "shell",
}


# 字符串动态构造的统一判据。``raw_args``（折叠前）与 ``resolved_args``（折叠后）
# 都要用它判：折叠会把 ``query`` 还原成它被赋值的那个表达式，此时原始实参只剩
# 一个裸变量名，只在折叠前判定必然漏掉「拼接发生在上一行」这一最常见形态。
_CONCAT_PATTERNS = (
    r"\bfmt\.Sprintf\b",    # Go
    r"\bString\.format\b",  # Java / Python
    r"`[^`]*\$\{",          # JS/TS 模板字面量插值
    r'(?:^|[^\w])[fF]["\']',  # Python f-string 前缀（折叠后保留）
)

_STRING_LITERAL_RE = re.compile(
    r'"""(?:\\.|[^\\])*?"""'
    r"|'''(?:\\.|[^\\])*?'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'"
    r"|`(?:\\.|[^`\\])*`"
)


def detect_string_concat(text: str) -> bool:
    """判断一段实参文本是否包含字符串动态构造。

    先剥离字符串字面量再找 ``+``：SQL 语句里 ``attempts = attempts + 1`` 这类
    **字面量内部的算术加号**不是拼接，把它算进去会让每一条参数化查询都变成
    SQL 注入（实测仓库自身因此产出 30+ 条 critical 假阳性）。
    """
    if not text:
        return False
    if any(re.search(pat, text) for pat in _CONCAT_PATTERNS):
        return True
    return "+" in _STRING_LITERAL_RE.sub('""', text)


@dataclass
class _PyScope:
    """一个 Python 词法作用域：行区间 + 变量折叠结果 + 形参名。"""
    start: int
    end: int
    name: str
    env: dict
    params: tuple[str, ...] = ()


def _python_function_params(node: ast.AST) -> tuple[str, ...]:
    """取函数节点的全部形参名（含仅位置参数、*args、**kwargs、仅关键字参数）。"""
    args = getattr(node, "args", None)
    if args is None:
        return ()
    names: list[str] = []
    for group in (
        getattr(args, "posonlyargs", []),
        getattr(args, "args", []),
        getattr(args, "kwonlyargs", []),
    ):
        for item in group:
            names.append(item.arg)
    for extra in (getattr(args, "vararg", None), getattr(args, "kwarg", None)):
        if extra is not None:
            names.append(extra.arg)
    return tuple(names)


def _python_scope_envs(analyzer: Any, text: str, filepath: Path) -> list[_PyScope]:
    """按 Python AST 折叠出**按作用域隔离**的变量环境。

    模块级语句作为一个覆盖全文件的兜底作用域（作用域名为空串）。解析失败时返回
    空列表，调用方退化为「不做折叠」而不是拿错环境去还原实参。
    """
    try:
        tree = ast.parse(text, filename=str(filepath))
    except (SyntaxError, ValueError):
        return []

    total = max(len(text.splitlines()), 1)
    scopes: list[_PyScope] = []
    try:
        module_env = analyzer.fold_python_function_scope(
            types.SimpleNamespace(body=tree.body)
        )
    except Exception:
        module_env = {}
    if module_env:
        scopes.append(_PyScope(1, total, "", module_env))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        try:
            env = analyzer.fold_python_function_scope(node)
        except Exception:
            continue
        params = _python_function_params(node)
        if not env and not params:
            continue
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", None) or total
        scopes.append(_PyScope(start, end, node.name, env, params))
    return scopes


def _scope_env_for_line(scopes: list[_PyScope], line: int) -> tuple[dict, str, tuple[str, ...]]:
    """取覆盖该行的作用域链，按「外层→内层」合并（内层覆盖外层）。"""
    containing = [s for s in scopes if s.start <= line <= s.end]
    if not containing:
        return {}, "", ()
    containing.sort(key=lambda s: s.end - s.start, reverse=True)
    env: dict = {}
    params: list[str] = []
    for scope in containing:
        env.update(scope.env)
        params.extend(scope.params)
    return env, containing[-1].name, tuple(dict.fromkeys(params))


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
    param_names: tuple[str, ...] = ()  # 所在函数的形参名（「外部可控」的最小判据）
    # callee 前一个字符是否为 `.`。``base_obj`` 只覆盖 ``a.b.c(...)`` 这类链式
    # 调用；``re.exec(s)``、``/x/g.exec(s)``、``f().exec(s)`` 的 receiver 以
    # ``)``/``]``/正则字面量结尾，正则抽取只剩裸方法名，``base_obj`` 为空，
    # 结构上无法与真正的裸函数调用区分——必须靠这个标记。
    preceded_by_dot: bool = False


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
                if self.language == "python":
                    # Python 有真实 AST，作用域是确定的：按**函数**折叠，再按调用
                    # 所在行挑对应函数的变量环境。绝不能把各函数的环境平铺合并
                    # ——同名局部变量（如多个函数各自定义的 ``req``）会互相覆盖，
                    # 一处调用的实参被还原成另一处函数的赋值，直接制造假阳性。
                    # 也绝不叠加 ``fold_line_scope``：那是对整份文件逐行做正则
                    # 匹配，跨函数、跨缩进层级，对 Python 既冗余又不安全。
                    scopes = _python_scope_envs(analyzer, text, filepath)
                    for call in calls:
                        env, scope_name, params = _scope_env_for_line(scopes, call.span.line)
                        call.enclosing_function = scope_name
                        call.param_names = params
                        rc, ra, it = analyzer.resolve_call_target(
                            call.callee, call.args_raw, env, snippet=call.span.snippet
                        )
                        call.resolved_callee = rc or call.callee
                        call.resolved_args = ra or call.args_raw
                        call.is_tainted = it
                else:
                    var_env = analyzer.fold_line_scope(lines)
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

        # 折叠后补齐拼接判定：``execute(query)`` 的原始实参是裸变量名，真正的
        # 拼接/插值在 ``query`` 的赋值行上，只有折叠后的 ``resolved_args`` 才看得见。
        for call in calls:
            if not call.has_string_concat:
                call.has_string_concat = detect_string_concat(call.resolved_args)

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
        """从注释中提取行级抑制信息（解析口径见模块级 ``extract_suppressions``）。
        支持语法:
          // bugx-ignore: RULE-001, RULE-002 [reason]
          # bugx-ignore: RULE-001
          // bugx-ignore (忽略该行全部规则)
          # nosec (兼容 Bandit 忽略标记)
        """
        return extract_suppressions(lines)

    def _strip_comments_and_strings(self, text: str, lang: str) -> tuple[str, list[str]]:
        """语言感知分词：将注释替换为空格，将字符串内容替换为标号以保留位置，生成净代码。"""
        chars = list(text)
        length = len(chars)
        i = 0
        in_single_quote = False
        in_double_quote = False
        in_backtick = False
        in_triple = ""
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
            if (in_single_quote or in_double_quote or in_backtick or in_triple) and c == "\\":
                chars[i] = " "
                if i + 1 < length and chars[i + 1] != "\n":
                    chars[i + 1] = " "
                    i += 2
                else:
                    i += 1
                continue

            # 处理三引号字符串（Python/Ruby 的 docstring 与多行字面量）。
            # 必须整体识别：把 `"""` 当成三次普通引号开关会让 docstring 正文
            # 泄漏成代码（文档里的示例载荷被当成真实调用），同时逐字符置空会
            # 吞掉内部换行、使后续所有行号前移。
            if in_triple:
                if text.startswith(in_triple, i):
                    for k in range(len(in_triple)):
                        chars[i + k] = " "
                    i += len(in_triple)
                    in_triple = ""
                    continue
                if c != "\n":
                    chars[i] = " "
                i += 1
                continue

            # 处理反引号字符串 (JS/TS 模板字符串, Go 反引号字符串)
            if in_backtick:
                if c == "`":
                    in_backtick = False
                elif c != "\n":
                    chars[i] = " "
                i += 1
                continue

            # 处理双引号字符串
            if in_double_quote:
                if c == '"':
                    in_double_quote = False
                elif c != "\n":
                    chars[i] = " "
                i += 1
                continue

            # 处理单引号字符串
            if in_single_quote:
                if c == "'":
                    in_single_quote = False
                elif c != "\n":
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

            # 进入三引号字符串（仅 Python：`"""` / `'''` 是真正的多行字面量，
            # 其余语言的 `"""` 只是相邻空串，按普通引号处理即可）
            if lang == "python" and (
                text.startswith('"""', i) or text.startswith("'''", i)
            ):
                in_triple = text[i:i + 3]
                chars[i] = chars[i + 1] = chars[i + 2] = " "
                i += 3
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
            has_concat = detect_string_concat(raw_args)

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
                preceded_by_dot=start_pos > 0 and clean_text[start_pos - 1] == ".",
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
        """检查指定行是否抑制了 ``rule_id``（口径见模块级 ``is_suppressed``）。"""
        return is_suppressed(parsed.suppressions, lineno, rule_id)
