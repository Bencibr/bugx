#!/usr/bin/env python3
"""core/dynamic_flow_analyzer.py — 动态语言局部常量折叠、数据流追踪与框架污点分析引擎 (DOC-063)。

纯 Python 标准库实现，提供：
1. 函数内局部常量折叠与 Def-Use 变量链回溯（Constant Folding & Def-Use Propagation）；
2. 动态 Web 框架全息 Sources-Sinks 矩阵建模（Flask, Django, Express, PHP, Rails）；
3. 上下文感知高精度净化器消歧（区分强类型安全防御与弱转换伪净化）；
4. 动态间接调用还原（如 getattr(os, x) -> os.system）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 动态 Web 框架不可信输入源（Sources）正则建模
FRAMEWORK_SOURCES: dict[str, list[str]] = {
    "python": [
        r"\brequest\.(args|form|values|json|get_json\(\)|files|data|headers|cookies)\b",
        r"\brequest\.(GET|POST|body|headers|COOKIES)\b",
        r"\b(sys\.argv|os\.environ(\.get)?)\b",
    ],
    "javascript": [
        r"\breq\.(query|body|params|headers|cookies)\b",
        r"\brequest\.(query|body|params|headers)\b",
        r"\bprocess\.env\b",
    ],
    "typescript": [
        r"\breq\.(query|body|params|headers|cookies)\b",
        r"\brequest\.(query|body|params|headers)\b",
        r"\bprocess\.env\b",
    ],
    "php": [
        r"\$_(GET|POST|REQUEST|COOKIE|SERVER|FILES)\b",
        r"\$request->(input|query|post|cookie|header|all)\b",
    ],
    "ruby": [
        r"\bparams(\[[^\]]+\])?\b",
        r"\brequest\.(params|GET|POST)\b",
        r"\bENV(\[[^\]]+\])?\b",
    ],
}

# 针对各漏洞类别的“强类型安全防御”净化器（Sanitizers）
STRONG_SANITIZERS: dict[str, dict[str, list[str]]] = {
    "sql-injection": {
        "python": [r"\bint\(", r"\bfloat\(", r"\bdecimal\.Decimal\(", r"\bparametrize\b"],
        "javascript": [r"\bparseInt\(", r"\bNumber\(", r"\bparseFloat\("],
        "typescript": [r"\bparseInt\(", r"\bNumber\(", r"\bparseFloat\("],
        "php": [r"\(int\)", r"\bintval\(", r"\bfilter_var\(", r"\bprepare\("],
        "ruby": [r"\.to_i\b", r"\bInteger\(", r"\bsanitize_sql\b"],
    },
    "command-injection": {
        "python": [r"\bshlex\.quote\("],
        "javascript": [r"\bexecFile\b"],
        "typescript": [r"\bexecFile\b"],
        "php": [r"\bescapeshellarg\(", r"\bescapeshellcmd\("],
        "ruby": [r"\bShellwords\.escape\b"],
    },
    "path-traversal": {
        "python": [r"\bos\.path\.abspath\(", r"\bPath\.resolve\(", r"\bos\.path\.basename\("],
        "javascript": [r"\bpath\.resolve\(", r"\bpath\.normalize\(", r"\bpath\.basename\("],
        "typescript": [r"\bpath\.resolve\(", r"\bpath\.normalize\(", r"\bpath\.basename\("],
        "php": [r"\brealpath\(", r"\bbasename\("],
        "ruby": [r"\bFile\.expand_path\(", r"\bFile\.basename\("],
    },
    "xss": {
        "python": [r"\bhtml\.escape\(", r"\bmarkupsafe\.escape\("],
        "javascript": [r"\bencodeURIComponent\(", r"\bDOMPurify\.sanitize\("],
        "typescript": [r"\bencodeURIComponent\(", r"\bDOMPurify\.sanitize\("],
        "php": [r"\bhtmlspecialchars\(", r"\bhtmlentities\("],
        "ruby": [r"\bERB::Util\.html_escape\(", r"\bsanitize\("],
    },
}

# 伪净化（无法防御真实注入的弱类型/格式化转换）
WEAK_TRANSFORMS = [
    r"\bstr\(", r"\bString\(", r"\.strip\(\)", r"\.trim\(\)",
    r"\.toLowerCase\(\)", r"\.lower\(\)", r"\(string\)", r"\.to_s\b",
]

# 字符串字面量起始引号（三引号优先，单双引号其次）。
# 反引号（JS/TS 模板字面量）**不在**此列：``${host}`` 的插值必须能被折叠。
_LITERAL_QUOTES = ('"""', "'''", '"', "'")


def _literal_end(expr: str, start: int, quote: str) -> int:
    """从 ``start`` 起找 ``quote`` 的收尾位置（返回结束后的下标），找不到返回 -1。"""
    index = start
    while index < len(expr):
        char = expr[index]
        if char == "\\":
            index += 2
            continue
        if expr.startswith(quote, index):
            return index + len(quote)
        # 单行引号不跨行；跨行说明解析器给的实参文本本身不完整，交由调用方兜底。
        if len(quote) == 1 and char == "\n":
            return -1
        index += 1
    return -1


def _matching_brace(text: str, start: int) -> int:
    """``text[start] == '{'``，返回与之配对的 ``}`` 之后的下标，找不到返回 -1。"""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char in "\"'":
            end = _literal_end(text, index + 1, char)
            if end < 0:
                return -1
            index = end
            continue
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return -1


def _fstring_body_spans(body: str, depth: int = 0) -> list[tuple[bool, str]]:
    """f-string 体内：``{...}`` 是代码（插值），其余是字面量文本。

    ``{{`` / ``}}`` 是转义后的普通花括号，按字面量处理。
    """
    spans: list[tuple[bool, str]] = []
    buffer: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char in "{}" and index + 1 < len(body) and body[index + 1] == char:
            buffer.append(char * 2)
            index += 2
            continue
        if char != "{":
            buffer.append(char)
            index += 1
            continue
        end = _matching_brace(body, index)
        if end < 0:
            buffer.append(body[index:])
            break
        if buffer:
            spans.append((False, "".join(buffer)))
            buffer = []
        spans.append((True, "{"))
        spans.extend(_split_code_and_literals(body[index + 1:end - 1], depth + 1))
        spans.append((True, "}"))
        index = end
    if buffer:
        spans.append((False, "".join(buffer)))
    return spans


def _split_code_and_literals(expr: str, depth: int = 0) -> list[tuple[bool, str]]:
    """把实参文本切成 ``(是否代码, 片段)``。

    普通字符串字面量（``"..."``、``'...'``、三引号）内部**不是代码**，变量折叠
    不得进入；f-string 的**字面量文本**同样不是代码，只有 ``{...}`` 插值段是。
    反引号模板字面量按代码处理（``${host}`` 必须能被折叠）。
    引号未闭合时退回"整段都是代码"，保持与旧实现一致的兜底行为。
    """
    spans: list[tuple[bool, str]] = []
    buffer: list[str] = []
    index = 0
    while index < len(expr):
        char = expr[index]
        if char not in "\"'":
            buffer.append(char)
            index += 1
            continue

        # 紧邻引号的标识符字母就是字符串前缀（f / r / b / u / rf ...）。
        prefix_start = index
        while prefix_start > 0 and (expr[prefix_start - 1].isalnum() or expr[prefix_start - 1] == "_"):
            prefix_start -= 1
        prefix = expr[prefix_start:index].lower()

        quote = next((q for q in _LITERAL_QUOTES if expr.startswith(q, index)), char)
        end = _literal_end(expr, index + len(quote), quote)
        if end < 0:
            buffer.append(expr[index:])
            break
        if "f" in prefix and depth < 3:
            # f-string：插值是代码，必须继续参与折叠（SQLI/SSRF 的插值判据依赖它）；
            # 但字面量文本不是代码，不能被同名变量替换。
            buffer.append(quote)
            spans.append((True, "".join(buffer)))
            buffer = []
            spans.extend(
                _fstring_body_spans(expr[index + len(quote):end - len(quote)], depth)
            )
            spans.append((True, quote))
            index = end
            continue

        if buffer:
            spans.append((True, "".join(buffer)))
            buffer = []
        spans.append((False, expr[index:end]))
        index = end

    if buffer:
        spans.append((True, "".join(buffer)))
    return spans


@dataclass
class FlowVariable:
    name: str
    value: str                 # 折叠或追踪到的表达式/常量文本
    is_constant: bool          # 是否为完全静态字面量
    is_tainted: bool           # 是否受外部框架 Source 污染
    sanitizers: set[str] = field(default_factory=set) # 经历过的净化器
    line: int = 1


class DynamicFlowAnalyzer:
    """动态语言局部变量流与上下文推导分析器"""

    def __init__(self, language: str = "python") -> None:
        self.language = language.lower()

    @staticmethod
    def is_strong_sanitized(expr: str, category: str, language: str) -> bool:
        """检查表达式是否经历了针对特定漏洞类别的强防御净化"""
        lang = language.lower()
        rules = STRONG_SANITIZERS.get(category, {}).get(lang, [])
        for pat in rules:
            if re.search(pat, expr):
                return True
        return False

    @staticmethod
    def is_weak_transform(expr: str) -> bool:
        """检查是否仅为无安全防护效果的弱转换"""
        for pat in WEAK_TRANSFORMS:
            if re.search(pat, expr):
                return True
        return False

    def has_framework_source(self, expr: str) -> bool:
        """检查表达式是否直接引用了不可信 Web 框架输入源"""
        sources = FRAMEWORK_SOURCES.get(self.language, [])
        for pat in sources:
            if re.search(pat, expr):
                return True
        return False

    def fold_python_function_scope(self, func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, FlowVariable]:
        """对 Python AST 函数节点执行单作用域常量折叠与 Def-Use 传播"""
        var_env: dict[str, FlowVariable] = {}

        for stmt in func_node.body:
            # 赋值语句追踪: x = "a" + "b" 或 x = f"..."
            if isinstance(stmt, ast.Assign):
                rhs_val, is_const = self._eval_python_ast_expr(stmt.value, var_env)
                is_tainted = self.has_framework_source(rhs_val)
                # 若右值引用了已受污染的变量，传递污染
                for v in var_env.values():
                    if v.is_tainted and re.search(rf"\b{re.escape(v.name)}\b", rhs_val):
                        is_tainted = True
                        break

                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        var_env[target.id] = FlowVariable(
                            name=target.id,
                            value=rhs_val,
                            is_constant=is_const,
                            is_tainted=is_tainted,
                            line=stmt.lineno,
                        )
        return var_env

    def _eval_python_ast_expr(self, node: ast.AST, env: dict[str, FlowVariable]) -> tuple[str, bool]:
        """递归评估 Python AST 表达式，实现常量字符串折叠"""
        # 常量字符串
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, True

        # 变量引用查找
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id].value, env[node.id].is_constant
            return node.id, False

        # 字符串拼接 BinOp (+)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left_val, left_const = self._eval_python_ast_expr(node.left, env)
            right_val, right_const = self._eval_python_ast_expr(node.right, env)
            if left_const and right_const:
                return left_val + right_val, True
            return f"{left_val} + {right_val}", False

        # f-string / JoinedStr
        if isinstance(node, ast.JoinedStr):
            parts = []
            all_const = True
            for part in node.values:
                val, c = self._eval_python_ast_expr(part, env)
                parts.append(val)
                if not c:
                    all_const = False
            folded = "".join(parts)
            # 含插值的 f-string 必须保留 ``f`` 前缀：折叠只做「值归一化」，不能
            # 把动态插值抹平成普通字面量。下游的 SQLi/CMDi 规则靠「这是不是动态
            # 拼出来的串」判定，前缀一丢，`execute(query)` 里 query 的插值痕迹
            # 就彻底不可见（实测靶场种子 ① 因此漏检）。
            if not all_const:
                return 'f"' + folded + '"', False
            return folded, all_const

        # FormattedValue
        if isinstance(node, ast.FormattedValue):
            val, c = self._eval_python_ast_expr(node.value, env)
            return f"{{{val}}}", c

        # getattr(obj, "func")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2:
                obj_val, _ = self._eval_python_ast_expr(node.args[0], env)
                attr_val, attr_const = self._eval_python_ast_expr(node.args[1], env)
                if attr_const:
                    return f"{obj_val}.{attr_val}", True
                return f"getattr({obj_val}, {attr_val})", False

        try:
            return ast.unparse(node), False
        except Exception:
            return "<expr>", False

    def fold_line_scope(self, lines: list[str]) -> dict[str, FlowVariable]:
        """通用的轻量行级变量赋值传播与常量折叠（适用于 JS/TS, PHP, Ruby 等）"""
        var_env: dict[str, FlowVariable] = {}

        # 赋值正则匹配: const/let/var/$x = ...
        assign_pat = re.compile(
            r'^\s*(?:(?:const|let|var|my|local)\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(.+?);?\s*$'
        )

        for lineno, line in enumerate(lines, 1):
            s = line.strip().split("//")[0].split("#")[0].strip()
            if not s:
                continue

            m = assign_pat.match(s)
            if m:
                var_name = m.group(1)
                rhs_raw = m.group(2)

                # 展开并替换已知变量
                resolved_rhs = self._resolve_line_vars(rhs_raw, var_env)
                folded_str, is_const = self._fold_string_concat(resolved_rhs)

                is_tainted = self.has_framework_source(folded_str)
                if not is_tainted:
                    for v in var_env.values():
                        v_pat = rf"(?<![A-Za-z0-9_$]){re.escape(v.name)}(?![A-Za-z0-9_$])"
                        if v.is_tainted and re.search(v_pat, folded_str):
                            is_tainted = True
                            break

                var_env[var_name] = FlowVariable(
                    name=var_name,
                    value=folded_str,
                    is_constant=is_const,
                    is_tainted=is_tainted,
                    line=lineno,
                )

        return var_env

    def _resolve_line_vars(self, expr: str, env: dict[str, FlowVariable], depth: int = 5) -> str:
        """递归解析单行表达式中的变量引用"""
        if depth <= 0:
            return expr
        res = expr
        for name, var in env.items():
            pattern = rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])"
            if re.search(pattern, res):
                val_to_sub = var.value
                if var.is_constant and not (val_to_sub.startswith(('"', "'")) and val_to_sub.endswith(('"', "'"))):
                    val_to_sub = f'"{val_to_sub}"'
                res = re.sub(pattern, val_to_sub, res)
        return res

    def _fold_string_concat(self, expr: str) -> tuple[str, bool]:
        """折叠字符串字面量拼接（+ 或 . 或反引号）"""
        # "a" + "b" 或 'a' . 'b'
        concat_pat = re.compile(r'["\']([^"\']*)["\']\s*(?:\+|\.)\s*["\']([^"\']*)["\']')
        curr = expr
        changed = True
        iterations = 0
        while changed and iterations < 10:
            iterations += 1
            new_curr = concat_pat.sub(r'"\1\2"', curr)
            changed = new_curr != curr
            curr = new_curr

        # 判断是否收敛为纯字面量
        m_pure = re.match(r'^["\']([^"\']*)["\']$', curr.strip())
        if m_pure:
            return m_pure.group(1), True
        return curr, False

    # 顶层关键字实参的起点（排除 ``==`` 比较）。
    _KWARG_START_RE = re.compile(r",\s*[A-Za-z_][A-Za-z0-9_]*\s*=(?!=)")

    def payload_portion(self, args: str) -> str:
        """取第一个关键字实参之前的部分——注入类规则的载荷面。"""
        if not args:
            return args
        match = self._KWARG_START_RE.search(args)
        return args[: match.start()] if match else args

    # 标识符边界：不替换更长标识符的前缀，也不替换关键字实参名（``timeout=``）。
    _IDENT_RE = re.compile(
        r"(?<![A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)(?![\w$])(?!\s*=(?!=))"
    )

    def _substitute_vars_once(
        self, expr: str, env: dict[str, FlowVariable]
    ) -> tuple[str, bool]:
        """在表达式上**单次**替换变量引用，替换结果不再参与匹配。

        返回 ``(替换后表达式, 是否引用了受污染变量)``。

        替换只在**代码面**上做：普通字符串字面量（含三引号）内部不是代码，
        按标识符直接替换会**凭空篡改分析对象**——实测 ``help="... cloud sync"``
        里的 ``cloud`` 被同名变量替换成 ``subparsers.add_parser('cloud')``，
        静态 DDL 里的列名被替换成 f-string，整段实参变成源码里不存在的东西。
        全仓 317 个调用点存在这种改写。

        f-string / 模板字面量（``f"..."``、``rf'...'``、JS 反引号）**整体仍按
        代码处理**：``f"SELECT * FROM {table}"`` 的 ``{table}``、``${host}``
        必须能被折叠还原，SQLI 与 SSRF 的插值判据都建立在折叠后的文本上。
        """
        if not expr or not env:
            return expr, False
        tainted = False

        def _repl(match: re.Match[str]) -> str:
            nonlocal tainted
            var = env.get(match.group(1))
            if var is None:
                return match.group(0)
            if var.is_tainted:
                tainted = True
            return var.value

        pieces: list[str] = []
        for is_code, chunk in _split_code_and_literals(expr):
            pieces.append(self._IDENT_RE.sub(_repl, chunk) if is_code else chunk)
        return "".join(pieces), tainted

    def resolve_call_target(
        self, callee: str, args_raw: str, env: dict[str, FlowVariable], snippet: str = ""
    ) -> tuple[str, str, bool]:
        """还原可能被混淆的调用目标与实参，返回 (resolved_callee, resolved_args, is_tainted)"""
        resolved_callee = callee
        resolved_args = args_raw
        is_tainted = False

        # 1. 还原 callee 中的变量（直接变量名与 getattr(os, func) 中的变量）
        if callee in env:
            resolved_callee = env[callee].value
            if env[callee].is_tainted:
                is_tainted = True
        else:
            resolved_callee, tainted_by_callee = self._substitute_vars_once(
                resolved_callee, env
            )
            is_tainted = is_tainted or tainted_by_callee

        # 2. 还原实参中的变量
        #
        # 必须**单次扫描、不回扫替换结果**。原先按 env 逐项 ``re.sub`` 的写法会让
        # 替换进去的文本被后续变量名再次替换，产生级联污染：实测
        # ``corpus_fetch.py`` 的 ``urlopen(req, timeout=timeout, ...)`` 被还原成
        # 一整段把多个函数里 ``req`` 的赋值、``pool.submit(...)`` 片段拼在一起的
        # 垃圾文本，进而被拼接检测判成动态构造（假 SSRF）。单次替换用一次
        # ``re.sub`` + 回调实现：只在**原始实参**上找标识符，替换结果不再参与匹配。
        resolved_args, _ = self._substitute_vars_once(resolved_args, env)
        # 污点传播同样只看载荷面：`env=environment` 这类配置实参即使本身来自
        # `os.environ`，也不构成注入面（见下方 payload_portion 说明）。
        payload = self.payload_portion(args_raw)
        if any(
            var.is_tainted
            and re.search(
                rf"(?<![A-Za-z0-9_$]){re.escape(var.name)}(?![A-Za-z0-9_$])", payload
            )
            for var in env.values()
        ):
            is_tainted = True

        # 3. 检查 getattr(os, "system") 或 getattr(os, system) 字符串内嵌动态调用还原
        m_getattr = re.search(r"getattr\(([^,]+),\s*['\"]?([A-Za-z0-9_]+)['\"]?\)", resolved_callee)
        if m_getattr:
            resolved_callee = f"{m_getattr.group(1).strip()}.{m_getattr.group(2).strip()}"

        # 4. 检查独立 getattr 节点还原及链式实参提取
        if callee == "getattr":
            parts = [a.strip() for a in resolved_args.split(",") if a.strip()]
            if len(parts) >= 2:
                obj_name = parts[0]
                attr_name = parts[1].strip("'\"")
                resolved_callee = f"{obj_name}.{attr_name}"
                if snippet:
                    m_chain = re.search(r'getattr\([^)]+\)\s*\(([^)]*)\)', snippet)
                    if m_chain:
                        chain_raw = m_chain.group(1).strip()
                        resolved_args = self._resolve_line_vars(chain_raw, env)

        # 污点只看**载荷面**（第一个关键字实参之前）。注入类规则的注入点是命令/查询
        # 串本身；把 `env=os.environ.copy()`、`cwd=`、`timeout=` 这类配置实参也算进去，
        # 会让 `subprocess.run(cmd, env=os.environ.copy())` 这种完全正常的调用被判成
        # 命令注入（实测仓库自身就有一条 critical 假阳性）。
        if self.has_framework_source(self.payload_portion(resolved_args)):
            is_tainted = True

        return resolved_callee, resolved_args, is_tainted
