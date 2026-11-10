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
            return "".join(parts), all_const

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
            for name, var in env.items():
                pat = rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])"
                if re.search(pat, resolved_callee):
                    resolved_callee = re.sub(pat, var.value, resolved_callee)
                    if var.is_tainted:
                        is_tainted = True

        # 2. 还原实参中的变量
        for name, var in env.items():
            pat = rf"(?<![A-Za-z0-9_$]){re.escape(name)}(?![A-Za-z0-9_$])"
            if re.search(pat, resolved_args):
                resolved_args = re.sub(pat, var.value, resolved_args)
                if var.is_tainted:
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

        if self.has_framework_source(resolved_args):
            is_tainted = True

        return resolved_callee, resolved_args, is_tainted
