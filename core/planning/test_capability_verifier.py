#!/usr/bin/env python3
"""BugX 测试用例能力动态验证框架 (Test Capability Verifier, DOC-052).

基于 AST 语义微观变异 (Mutation Testing) 与隔离沙箱执行，对测试用例抓 Bug 能力进行动态验证：
  1. SemanticMutator: 针对被测代码生成 6 类高阶语义变异体 (ROR, AOR, LCR, BCR, EOR, SVR);
  2. MutationSandboxRunner: 独立安全沙箱中执行基线验证与变异体杀伤动态测试;
  3. TestCapabilityEvaluator: 计算变异击杀率 (Mutation Score) 并评定能力等级 (ELITE/CAPABLE/WEAK/INEFFECTIVE);
  4. 产出结构化报告与覆盖智能系统 (coverage_intelligence) 兼容数据。
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

import argparse
import ast
import copy
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

SCHEMA_VERSION = 1

# 允许作为测试入口的解释器白名单。``test_target`` 由 Worker 通过 MCP 传入，
# 而 Worker 上下文必然携带被审计项目的不可信源码：若不限制首 token，
# ``sh -c '<任意命令>'`` 会被直接执行并获得完整 RCE 与环境变量。
ALLOWED_TEST_RUNNERS = frozenset({
    "python", "python3", "py", "pytest",
    "go",
    "ctest", "cargo", "make", "ninja",
    "node", "npm",
})

# 沙箱执行时透传的环境变量白名单（对齐 platform_adapter.SAFE_ENV 并覆盖多语言工具链）。
SAFE_ENV_KEYS = (
    "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "SYSTEMROOT", "COMSPEC", "PYTHONIOENCODING",
    # Go 运行环境
    "GOPATH", "GOROOT", "GOCACHE", "GO111MODULE", "CGO_ENABLED", "CGO_CFLAGS", "CGO_LDFLAGS",
    # Rust 运行环境
    "CARGO_HOME", "RUSTUP_HOME", "RUST_BACKTRACE",
    # C/C++ 构建与测试环境
    "CC", "CXX", "CFLAGS", "CXXFLAGS", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    # Node / JS / TS 运行环境
    "NODE_PATH", "NODE_OPTIONS",
)

# 变异验证隔离副本的排除规则。SEC-11：变异测试只改写私有副本，
# 复制 ``self.cwd`` 时跳过缓存、仓库元数据与构建产物等无关且体积巨大的目录，
# 避免把整个仓库（含 .git / .bugx）逐变异体复制一遍。
MUTATION_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".git", ".bugx", "node_modules", "build", "dist", ".venv",
    "*.pyc", "*.pyo", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
)

# 能力评级阈值
LEVEL_ELITE_THRESHOLD = 80.0
LEVEL_CAPABLE_THRESHOLD = 60.0
LEVEL_WEAK_THRESHOLD = 30.0

# 变异算子定义
OP_ROR = "ROR"  # Relational Operator Replacement
OP_AOR = "AOR"  # Arithmetic Operator Replacement
OP_LCR = "LCR"  # Logical Connector Replacement
OP_BCR = "BCR"  # Boundary Condition Replacement
OP_EOR = "EOR"  # Exception / Return Alteration
OP_SVR = "SVR"  # Statement Voiding Replacement

OPERATOR_DESCRIPTIONS = {
    OP_ROR: "关系比较符变异 (<, <=, >, >=, ==, !=, in, is)",
    OP_AOR: "算术运算符变异 (+, -, *, //, /, %)",
    OP_LCR: "逻辑连接符变异 (and, or)",
    OP_BCR: "边界数值与布尔常量变异 (0<->1, True<->False, 边界增减)",
    OP_EOR: "异常与返回值篡改 (return None, 抹除 raise)",
    OP_SVR: "语句置空变异 (副作用调用/累加语句置为 pass)",
}

OP_SYMBOLS: dict[type, str] = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
    ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%",
    ast.And: "and", ast.Or: "or",
}


@dataclass
class Mutant:
    """单个变异体定义。"""
    mutant_id: str
    operator: str
    target_path: str
    lineno: int
    col_offset: int
    original_repr: str
    mutated_repr: str
    description: str
    mutated_source: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("mutated_source", None)  # 摘要中默认剔除完整代码，避免体积过大
        return d


@dataclass
class MutantExecutionResult:
    """变异体执行结果。"""
    mutant_id: str
    operator: str
    lineno: int
    status: str  # KILLED, SURVIVED, KILLED_TIMEOUT, INVALID, BASELINE_FAIL
    duration_ms: float
    description: str
    diagnostic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CapabilitySummary:
    """测试用例能力评估汇总。"""
    target_path: str
    test_target: str
    total_mutants: int
    killed_mutants: int
    survived_mutants: int
    timeout_mutants: int
    mutation_score: float
    capability_level: str  # ELITE, CAPABLE, WEAK, INEFFECTIVE
    operator_scores: dict[str, dict[str, Any]]
    survived_mutants_details: list[dict[str, Any]]
    recommendations: list[str]
    execution_time_sec: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SemanticMutator:
    """基于 AST 或通用 Token 的语义变异体生成引擎。"""

    def __init__(self, target_path: str | Path, source_code: str | None = None):
        self.target_path = Path(target_path)
        if source_code is not None:
            self.source_code = source_code
        else:
            self.source_code = self.target_path.read_text(encoding="utf-8")
        self.is_python = self.target_path.suffix.lower() == ".py"
        self.tree: ast.AST | None = None
        if self.is_python:
            try:
                self.tree = ast.parse(self.source_code, filename=str(self.target_path))
                self._index_tree(self.tree)
            except Exception:
                self.tree = None

    @staticmethod
    def _index_tree(tree: ast.AST) -> None:
        """为 AST 每个节点打上唯一的内部 _nid。"""
        idx = 0
        for node in ast.walk(tree):
            node._nid = idx  # type: ignore[attr-defined]
            idx += 1

    def generate_mutants(self, max_mutants: int = 30, operators: Sequence[str] | None = None) -> list[Mutant]:
        """生成变异体列表，支持最大数量控制与算子过滤。"""
        if self.tree is None:
            return self._generate_universal_mutants(max_mutants, operators)

        enabled_ops = set(operators or [OP_ROR, OP_AOR, OP_LCR, OP_BCR, OP_EOR, OP_SVR])
        raw_mutants: list[Mutant] = []

        # 1. 收集被保护的节点（如文档字符串、函数参数类型标注等）
        protected_nids = self._collect_protected_nids()

        # 2. 遍历 AST 收集各算子候选
        for node in ast.walk(self.tree):
            nid = getattr(node, "_nid", -1)
            if nid in protected_nids:
                continue

            lineno = getattr(node, "lineno", 1)
            col_offset = getattr(node, "col_offset", 0)

            # ROR 算子
            if OP_ROR in enabled_ops and isinstance(node, ast.Compare):
                for i, op in enumerate(node.ops):
                    swapped_ops = self._mutate_relational_op(op)
                    orig_sym = OP_SYMBOLS.get(type(op), op.__class__.__name__)
                    for new_op, desc in swapped_ops:
                        new_sym = OP_SYMBOLS.get(type(new_op), new_op.__class__.__name__)
                        mutant = self._create_mutant(
                            operator=OP_ROR,
                            target_nid=nid,
                            transformer=lambda n, i=i, new_op=new_op: self._apply_compare_op(n, i, new_op),
                            lineno=lineno,
                            col_offset=col_offset,
                            orig_repr=orig_sym,
                            mut_repr=new_sym,
                            description=f"ROR 行 {lineno}: {desc}",
                        )
                        if mutant:
                            raw_mutants.append(mutant)

            # AOR 算子
            if OP_AOR in enabled_ops and isinstance(node, ast.BinOp):
                swapped_ops = self._mutate_arithmetic_op(node.op)
                orig_sym = OP_SYMBOLS.get(type(node.op), node.op.__class__.__name__)
                for new_op, desc in swapped_ops:
                    new_sym = OP_SYMBOLS.get(type(new_op), new_op.__class__.__name__)
                    mutant = self._create_mutant(
                        operator=OP_AOR,
                        target_nid=nid,
                        transformer=lambda n, new_op=new_op: self._apply_bin_op(n, new_op),
                        lineno=lineno,
                        col_offset=col_offset,
                        orig_repr=orig_sym,
                        mut_repr=new_sym,
                        description=f"AOR 行 {lineno}: {desc}",
                    )
                    if mutant:
                        raw_mutants.append(mutant)

            # LCR 算子
            if OP_LCR in enabled_ops and isinstance(node, ast.BoolOp):
                new_op, desc = self._mutate_logical_op(node.op)
                if new_op:
                    orig_sym = OP_SYMBOLS.get(type(node.op), node.op.__class__.__name__)
                    new_sym = OP_SYMBOLS.get(type(new_op), new_op.__class__.__name__)
                    mutant = self._create_mutant(
                        operator=OP_LCR,
                        target_nid=nid,
                        transformer=lambda n, new_op=new_op: self._apply_bool_op(n, new_op),
                        lineno=lineno,
                        col_offset=col_offset,
                        orig_repr=orig_sym,
                        mut_repr=new_sym,
                        description=f"LCR 行 {lineno}: {desc}",
                    )
                    if mutant:
                        raw_mutants.append(mutant)

            # BCR 算子 (边界数值与布尔常量)
            if OP_BCR in enabled_ops and isinstance(node, ast.Constant):
                if isinstance(node.value, bool):
                    new_val = not node.value
                    mutant = self._create_mutant(
                        operator=OP_BCR,
                        target_nid=nid,
                        transformer=lambda n, new_val=new_val: self._apply_constant(n, new_val),
                        lineno=lineno,
                        col_offset=col_offset,
                        orig_repr=str(node.value),
                        mut_repr=str(new_val),
                        description=f"BCR 行 {lineno}: 布尔翻转 {node.value} -> {new_val}",
                    )
                    if mutant:
                        raw_mutants.append(mutant)
                elif isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                    candidates = self._mutate_numeric_boundary(node.value)
                    for new_val, desc in candidates:
                        mutant = self._create_mutant(
                            operator=OP_BCR,
                            target_nid=nid,
                            transformer=lambda n, new_val=new_val: self._apply_constant(n, new_val),
                            lineno=lineno,
                            col_offset=col_offset,
                            orig_repr=str(node.value),
                            mut_repr=str(new_val),
                            description=f"BCR 行 {lineno}: {desc}",
                        )
                        if mutant:
                            raw_mutants.append(mutant)

            # EOR 算子 (返回值或异常)
            if OP_EOR in enabled_ops:
                if isinstance(node, ast.Return) and node.value is not None:
                    if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                        mutant = self._create_mutant(
                            operator=OP_EOR,
                            target_nid=nid,
                            transformer=lambda n: self._apply_return_none(n),
                            lineno=lineno,
                            col_offset=col_offset,
                            orig_repr=ast.unparse(node),
                            mut_repr="return None",
                            description=f"EOR 行 {lineno}: 返回值篡改为 return None",
                        )
                        if mutant:
                            raw_mutants.append(mutant)
                elif isinstance(node, ast.Raise):
                    mutant = self._create_mutant(
                        operator=OP_EOR,
                        target_nid=nid,
                        transformer=lambda n: ast.Pass(),
                        lineno=lineno,
                        col_offset=col_offset,
                        orig_repr=ast.unparse(node),
                        mut_repr="pass",
                        description=f"EOR 行 {lineno}: 抹除异常抛出 (raise -> pass)",
                    )
                    if mutant:
                        raw_mutants.append(mutant)

            # SVR 算子 (语句置空)
            if OP_SVR in enabled_ops and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for stmt in node.body:
                    stmt_nid = getattr(stmt, "_nid", -1)
                    if isinstance(stmt, (ast.Expr, ast.AugAssign)) and stmt_nid not in protected_nids:
                        mutant = self._create_mutant(
                            operator=OP_SVR,
                            target_nid=stmt_nid,
                            transformer=lambda n: ast.Pass(),
                            lineno=getattr(stmt, "lineno", lineno),
                            col_offset=getattr(stmt, "col_offset", 0),
                            orig_repr=ast.unparse(stmt),
                            mut_repr="pass",
                            description=f"SVR 行 {getattr(stmt, 'lineno', lineno)}: 置空副作用语句为 pass",
                        )
                        if mutant:
                            raw_mutants.append(mutant)

        # 3. 按算子进行均衡采样截断，防止单类算子占满配额
        return self._balance_mutants(raw_mutants, max_mutants)

    def _collect_protected_nids(self) -> set[int]:
        """识别不应被变异的节点，如文档字符串、类型注解、导入语句等。"""
        protected: set[int] = set()
        for node in ast.walk(self.tree):
            nid = getattr(node, "_nid", -1)
            # 导入语句
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                protected.add(nid)
                for child in ast.walk(node):
                    protected.add(getattr(child, "_nid", -1))
            # 函数返回值注解与参数注解
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns:
                    for child in ast.walk(node.returns):
                        protected.add(getattr(child, "_nid", -1))
                for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                    if arg.annotation:
                        for child in ast.walk(arg.annotation):
                            protected.add(getattr(child, "_nid", -1))
                # 函数体首句 docstring
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    if isinstance(node.body[0].value.value, str):
                        protected.add(getattr(node.body[0], "_nid", -1))
                        protected.add(getattr(node.body[0].value, "_nid", -1))
            # 类体首句 docstring
            elif isinstance(node, ast.ClassDef):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    if isinstance(node.body[0].value.value, str):
                        protected.add(getattr(node.body[0], "_nid", -1))
                        protected.add(getattr(node.body[0].value, "_nid", -1))
            # 模块首句 docstring
            elif isinstance(node, ast.Module):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                    if isinstance(node.body[0].value.value, str):
                        protected.add(getattr(node.body[0], "_nid", -1))
                        protected.add(getattr(node.body[0].value, "_nid", -1))
        return protected

    def _create_mutant(
        self,
        operator: str,
        target_nid: int,
        transformer: Callable[[ast.AST], ast.AST],
        lineno: int,
        col_offset: int,
        orig_repr: str,
        mut_repr: str,
        description: str,
    ) -> Mutant | None:
        """克隆 AST 并替换指定 nid 节点，产出合法 Python 源码变异体。"""
        new_tree = copy.deepcopy(self.tree)

        class Replacer(ast.NodeTransformer):
            def visit(self, node: ast.AST) -> ast.AST:
                if getattr(node, "_nid", -1) == target_nid:
                    res = transformer(node)
                    return ast.copy_location(res, node)
                return self.generic_visit(node)

        transformed = Replacer().visit(new_tree)
        ast.fix_missing_locations(transformed)

        try:
            mutated_code = ast.unparse(transformed)
            # 语法健全性防御：必须能够被 Python 编译
            compile(mutated_code, f"<mutant_{target_nid}>", "exec")
        except Exception:
            return None

        # 确保变异后源码有实质变动
        if mutated_code == self.source_code:
            return None

        mutant_id = f"MUT_{operator}_{lineno}_{abs(hash(mut_repr)) % 10000:04d}"
        return Mutant(
            mutant_id=mutant_id,
            operator=operator,
            target_path=str(self.target_path),
            lineno=lineno,
            col_offset=col_offset,
            original_repr=orig_repr,
            mutated_repr=mut_repr,
            description=description,
            mutated_source=mutated_code,
        )

    # ---- 算子变异辅助规则 ----

    @staticmethod
    def _mutate_relational_op(op: ast.cmpop) -> list[tuple[ast.cmpop, str]]:
        res: list[tuple[ast.cmpop, str]] = []
        if isinstance(op, ast.Lt):
            res.append((ast.LtE(), "< -> <="))
            res.append((ast.Gt(), "< -> >"))
        elif isinstance(op, ast.LtE):
            res.append((ast.Lt(), "<= -> <"))
            res.append((ast.GtE(), "<= -> >="))
        elif isinstance(op, ast.Gt):
            res.append((ast.GtE(), "> -> >="))
            res.append((ast.Lt(), "> -> <"))
        elif isinstance(op, ast.GtE):
            res.append((ast.Gt(), ">= -> >"))
            res.append((ast.LtE(), ">= -> <="))
        elif isinstance(op, ast.Eq):
            res.append((ast.NotEq(), "== -> !="))
        elif isinstance(op, ast.NotEq):
            res.append((ast.Eq(), "!= -> =="))
        elif isinstance(op, ast.In):
            res.append((ast.NotIn(), "in -> not in"))
        elif isinstance(op, ast.NotIn):
            res.append((ast.In(), "not in -> in"))
        elif isinstance(op, ast.Is):
            res.append((ast.IsNot(), "is -> is not"))
        elif isinstance(op, ast.IsNot):
            res.append((ast.Is(), "is not -> is"))
        return res

    @staticmethod
    def _mutate_arithmetic_op(op: ast.operator) -> list[tuple[ast.operator, str]]:
        res: list[tuple[ast.operator, str]] = []
        if isinstance(op, ast.Add):
            res.append((ast.Sub(), "+ -> -"))
        elif isinstance(op, ast.Sub):
            res.append((ast.Add(), "- -> +"))
        elif isinstance(op, ast.Mult):
            res.append((ast.FloorDiv(), "* -> //"))
        elif isinstance(op, (ast.Div, ast.FloorDiv)):
            res.append((ast.Mult(), "/ -> *"))
        elif isinstance(op, ast.Mod):
            res.append((ast.Mult(), "% -> *"))
        return res

    @staticmethod
    def _mutate_logical_op(op: ast.boolop) -> tuple[ast.boolop | None, str]:
        if isinstance(op, ast.And):
            return ast.Or(), "and -> or"
        elif isinstance(op, ast.Or):
            return ast.And(), "or -> and"
        return None, ""

    @staticmethod
    def _mutate_numeric_boundary(val: int | float) -> list[tuple[int | float, str]]:
        res: list[tuple[int | float, str]] = []
        if val == 0:
            res.append((1, "0 -> 1"))
        elif val == 1:
            res.append((0, "1 -> 0"))
        elif val > 1:
            res.append((val - 1, f"{val} -> {val - 1}"))
            res.append((val + 1, f"{val} -> {val + 1}"))
        elif val < 0:
            res.append((val + 1, f"{val} -> {val + 1}"))
        return res

    @staticmethod
    def _apply_compare_op(node: ast.AST, index: int, new_op: ast.cmpop) -> ast.AST:
        assert isinstance(node, ast.Compare)
        node.ops[index] = new_op
        return node

    @staticmethod
    def _apply_bin_op(node: ast.AST, new_op: ast.operator) -> ast.AST:
        assert isinstance(node, ast.BinOp)
        node.op = new_op
        return node

    @staticmethod
    def _apply_bool_op(node: ast.AST, new_op: ast.boolop) -> ast.AST:
        assert isinstance(node, ast.BoolOp)
        node.op = new_op
        return node

    @staticmethod
    def _apply_constant(node: ast.AST, new_val: Any) -> ast.AST:
        assert isinstance(node, ast.Constant)
        node.value = new_val
        return node

    @staticmethod
    def _apply_return_none(node: ast.AST) -> ast.AST:
        assert isinstance(node, ast.Return)
        node.value = ast.Constant(value=None)
        return node

    @staticmethod
    def _balance_mutants(mutants: list[Mutant], max_mutants: int) -> list[Mutant]:
        """按算子均衡采样，避免单类变异体过度集中。"""
        if len(mutants) <= max_mutants:
            return mutants
        by_op: dict[str, list[Mutant]] = {}
        for m in mutants:
            by_op.setdefault(m.operator, []).append(m)

        selected: list[Mutant] = []
        op_keys = sorted(by_op.keys())
        idx = 0
        while len(selected) < max_mutants and any(by_op.values()):
            op = op_keys[idx % len(op_keys)]
            if by_op[op]:
                selected.append(by_op[op].pop(0))
            idx += 1

        # 按行号排序输出
        selected.sort(key=lambda m: (m.lineno, m.col_offset))
        return selected

    def _generate_universal_mutants(self, max_mutants: int = 30, operators: Sequence[str] | None = None) -> list[Mutant]:
        """为 C-family 语言 (C/C++, Go, Rust, Java, TS/JS) 提供基于源码分词与切片的通用变异生成。"""
        enabled_ops = set(operators or [OP_ROR, OP_AOR, OP_LCR, OP_BCR])
        raw_mutants: list[Mutant] = []

        lines = self.source_code.splitlines(keepends=True)

        # ROR: ==, !=, <=, >=, <, >
        ror_map = {
            "==": "!=",
            "!=": "==",
            "<=": ">",
            ">=": "<",
            "<": ">=",
            ">": "<=",
        }

        # AOR: +, -, *, /, %
        aor_map = {
            "+": "-",
            "-": "+",
            "*": "/",
            "/": "*",
            "%": "*",
        }

        # LCR: &&, ||
        lcr_map = {
            "&&": "||",
            "||": "&&",
        }

        # BCR: 0, 1, true, false
        bcr_map = {
            "0": "1",
            "1": "0",
            "true": "false",
            "false": "true",
        }

        # ROR 匹配：优先匹配双字符运算符
        ror_pat = re.compile(r"(==|!=|<=|>=|<|>)")
        # AOR 匹配：二元算术运算符，避免匹配 ++, --, +=, -=, ->, /* 等
        aor_pat = re.compile(r"(?<![+\-*/%=<>!&|])(?<!->)\s*([+\-*/%])\s*(?![+\-*/%=<>!&|])")
        # LCR 匹配：&&, ||
        lcr_pat = re.compile(r"(&&|\|\|)")
        # BCR 匹配：布尔与基本数值边界
        bcr_pat = re.compile(r"\b(0|1|true|false)\b")

        for lineno_0, line in enumerate(lines):
            lineno = lineno_0 + 1
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "/*", "*", "#", "import ", "package ", "using ", "include ")):
                continue

            # 剥离行内注释
            code_line = line
            comment_idx = -1
            if "//" in code_line:
                comment_idx = code_line.index("//")
            elif "/*" in code_line:
                comment_idx = code_line.index("/*")
            if comment_idx != -1:
                code_line = code_line[:comment_idx]

            # 1. ROR 算子
            if OP_ROR in enabled_ops:
                for match in ror_pat.finditer(code_line):
                    orig_op = match.group(1)
                    if orig_op in ror_map:
                        new_op = ror_map[orig_op]
                        start_col = match.start(1)
                        end_col = match.end(1)
                        mut_lines = list(lines)
                        mut_lines[lineno - 1] = line[:start_col] + new_op + line[end_col:]
                        mut_code = "".join(mut_lines)
                        mut_id = f"MUT_ROR_{lineno}_{abs(hash(new_op + str(lineno) + str(start_col))) % 10000:04d}"
                        raw_mutants.append(Mutant(
                            mutant_id=mut_id,
                            operator=OP_ROR,
                            target_path=str(self.target_path),
                            lineno=lineno,
                            col_offset=start_col,
                            original_repr=orig_op,
                            mutated_repr=new_op,
                            description=f"ROR 行 {lineno}: {orig_op} -> {new_op}",
                            mutated_source=mut_code,
                        ))

            # 2. AOR 算子
            if OP_AOR in enabled_ops:
                for match in aor_pat.finditer(code_line):
                    orig_op = match.group(1)
                    if orig_op in aor_map:
                        new_op = aor_map[orig_op]
                        start_col = match.start(1)
                        end_col = match.end(1)
                        mut_lines = list(lines)
                        mut_lines[lineno - 1] = line[:start_col] + new_op + line[end_col:]
                        mut_code = "".join(mut_lines)
                        mut_id = f"MUT_AOR_{lineno}_{abs(hash(new_op + str(lineno) + str(start_col))) % 10000:04d}"
                        raw_mutants.append(Mutant(
                            mutant_id=mut_id,
                            operator=OP_AOR,
                            target_path=str(self.target_path),
                            lineno=lineno,
                            col_offset=start_col,
                            original_repr=orig_op,
                            mutated_repr=new_op,
                            description=f"AOR 行 {lineno}: {orig_op} -> {new_op}",
                            mutated_source=mut_code,
                        ))

            # 3. LCR 算子
            if OP_LCR in enabled_ops:
                for match in lcr_pat.finditer(code_line):
                    orig_op = match.group(1)
                    if orig_op in lcr_map:
                        new_op = lcr_map[orig_op]
                        start_col = match.start(1)
                        end_col = match.end(1)
                        mut_lines = list(lines)
                        mut_lines[lineno - 1] = line[:start_col] + new_op + line[end_col:]
                        mut_code = "".join(mut_lines)
                        mut_id = f"MUT_LCR_{lineno}_{abs(hash(new_op + str(lineno) + str(start_col))) % 10000:04d}"
                        raw_mutants.append(Mutant(
                            mutant_id=mut_id,
                            operator=OP_LCR,
                            target_path=str(self.target_path),
                            lineno=lineno,
                            col_offset=start_col,
                            original_repr=orig_op,
                            mutated_repr=new_op,
                            description=f"LCR 行 {lineno}: {orig_op} -> {new_op}",
                            mutated_source=mut_code,
                        ))

            # 4. BCR 算子
            if OP_BCR in enabled_ops:
                for match in bcr_pat.finditer(code_line):
                    orig_val = match.group(1)
                    if orig_val in bcr_map:
                        new_val = bcr_map[orig_val]
                        start_col = match.start(1)
                        end_col = match.end(1)
                        mut_lines = list(lines)
                        mut_lines[lineno - 1] = line[:start_col] + new_val + line[end_col:]
                        mut_code = "".join(mut_lines)
                        mut_id = f"MUT_BCR_{lineno}_{abs(hash(new_val + str(lineno) + str(start_col))) % 10000:04d}"
                        raw_mutants.append(Mutant(
                            mutant_id=mut_id,
                            operator=OP_BCR,
                            target_path=str(self.target_path),
                            lineno=lineno,
                            col_offset=start_col,
                            original_repr=orig_val,
                            mutated_repr=new_val,
                            description=f"BCR 行 {lineno}: {orig_val} -> {new_val}",
                            mutated_source=mut_code,
                        ))

        return self._balance_mutants(raw_mutants, max_mutants)


def _is_python_runner(token: str) -> bool:
    """判断 argv 首 token 是否为本仓库认可的 Python 解释器入口。"""
    if token == sys.executable:
        return True
    name = Path(token).name
    return name in ("python", "python3", "py") or bool(re.fullmatch(r"python3\.\d+", name))


def normalize_test_command(test_command: list[str] | str) -> list[str]:
    """把 ``test_target`` 归一化为 argv，并校验执行入口与子命令合法性。

    这是测试入口的单一归一化实现：MCP handler 与沙箱执行器都调用它，
    避免「协议层校验一套、执行层归一化另一套」的漂移。

    单 token 字符串按 schema 语义视为测试文件路径，展开为
    ``[python, -m, unittest, <path>]``；多 token 字符串按 argv 解析。
    """
    if isinstance(test_command, str):
        cmd_str = test_command.strip()
        if not cmd_str:
            raise ValueError("test_target 不能为空")
        if " " not in cmd_str:
            return [sys.executable, "-m", "unittest", cmd_str]
        try:
            parts = shlex.split(cmd_str)
        except ValueError as exc:
            raise ValueError(f"test_target 无法解析: {exc}") from exc
    else:
        parts = [str(item) for item in test_command]
    if not parts:
        raise ValueError("test_target 不能为空")

    runner = Path(parts[0]).name

    # 1. Python 解释器入口
    if _is_python_runner(parts[0]):
        if "-m" not in parts or "unittest" not in parts:
            raise ValueError(
                "test_target 必须是 `python -m unittest <target> ...`；"
                "拒绝内联代码与任意模块执行"
            )
        parts[0] = sys.executable
        return parts

    # 2. Go 测试运行器
    if runner == "go":
        if len(parts) < 2 or parts[1] != "test":
            raise ValueError("Go test_target 必须是 `go test ...`；拒绝执行非 test 子命令")
        if any(p.startswith("-exec") or p == "-exec" for p in parts):
            raise ValueError("Go test_target 拒绝使用 -exec 高危参数")
        return parts

    # 3. CTest 测试运行器
    if runner == "ctest":
        return parts

    # 4. Make / Ninja 测试目标
    if runner in ("make", "ninja"):
        if len(parts) >= 2 and any(t in ("test", "check") for t in parts[1:]):
            return parts
        raise ValueError(f"{runner} test_target 必须指定 test 或 check 目标")

    # 5. Cargo 测试运行器
    if runner == "cargo":
        if len(parts) < 2 or parts[1] != "test":
            raise ValueError("Cargo test_target 必须是 `cargo test ...`；拒绝执行非 test 子命令")
        return parts

    # 6. Pytest 运行器
    if runner == "pytest":
        return parts

    # 7. Node / NPM 测试运行器
    if runner in ("node", "npm"):
        if runner == "npm":
            if len(parts) < 2 or parts[1] != "test":
                raise ValueError("npm test_target 必须是 `npm test ...`；拒绝执行非 test 子命令")
        return parts

    raise ValueError(
        f"test_target 必须使用受支持的测试运行器（python/go/ctest/cargo/node/npm），"
        f"拒绝执行: {parts[0]}"
    )


class MutationSandboxRunner:
    """安全沙箱执行器：支持 Golden 基线验证与变异杀伤隔离执行。"""

    def __init__(
        self,
        target_path: str | Path,
        test_command: list[str] | str,
        timeout_sec: float = 2.0,
        cwd: str | Path | None = None,
    ):
        self.target_path = Path(target_path).resolve()
        self.cwd = Path(cwd).resolve() if cwd else Path.cwd()
        # 首 token 必须是受信任的 Python 解释器。此处是唯一的可执行入口防线：
        # 任何非 Python 可执行名（sh/bash/curl/…）一律拒绝，不做「看起来安全就放行」
        # 的猜测——shell 与下载器只需一次放行即可完成 RCE。
        self.test_command = normalize_test_command(test_command)

        # 规整测试文件路径，避免 unittest 对绝对路径抛出 ModuleNotFoundError
        for i, arg in enumerate(self.test_command):
            if arg.endswith(".py"):
                p = Path(arg)
                try:
                    rel_p = p.resolve().relative_to(self.cwd)
                    self.test_command[i] = str(rel_p)
                except Exception:
                    if i >= 2 and self.test_command[i-2:i] == ["-m", "unittest"]:
                        self.test_command = [
                            self.test_command[0], "-m", "unittest", "discover",
                            "-s", str(p.parent.resolve()), "-p", p.name,
                        ]
                        break

        # 确保 Python 运行测试时不写入和复用 .pyc 缓存
        if self.test_command and _is_python_runner(self.test_command[0]) and "-B" not in self.test_command:
            self.test_command.insert(1, "-B")

        self.timeout_sec = timeout_sec
        # FIX-003 D1：基线实测耗时，用于把变异体看门狗设为**相对**上限。
        # 固定绝对超时（默认 2s）在「测试模块本身 ≥ 超时」时会让**所有**变异体
        # 超时并被误判为"击杀"，伪造出 100%/ELITE 的假绿（实测 8/8 全超时）。
        self._baseline_duration_sec: float | None = None

        # SEC-11 隔离副本的**每次运行只建一次**状态。
        # 历史实现把 ``mkdtemp``/``copytree`` 放在 ``run_mutant`` 内部，于是每个
        # 变异体都完整复制一次 ``self.cwd``；而生产路径上 ``self.cwd`` 就是项目根
        # （``mcp_handlers/mining_handlers.py`` 传 ``cwd=service.root``）。实测复制
        # ``core/``（205 文件 / 3.7MB）在本环境需 ~50s，``max_mutants=50`` 会让
        # ``bugx_test_verify`` 完全不可用。改为惰性建一次、运行结束统一销毁。
        self._sandbox_root: Path | None = None
        self._staged_cwd: Path | None = None
        self._staged_target: Path | None = None
        self._rel_target: Path | None = None
        self._original_target_source: str | None = None

    def _relative_target(self) -> Path:
        """目标文件相对 ``self.cwd`` 的路径；必须在 cwd 内才能被副本承接。"""
        if self._rel_target is None:
            try:
                self._rel_target = self.target_path.relative_to(self.cwd)
            except ValueError as exc:
                raise RuntimeError(
                    "变异验证要求 target_path 位于 cwd 之内，才能在不改写共享工作树的前提下"
                    f"隔离执行: target={self.target_path} cwd={self.cwd}"
                ) from exc
        return self._rel_target

    def _ensure_sandbox(self) -> Path:
        """惰性创建隔离副本（整个验证运行只创建一次），返回副本根目录。

        副本排除缓存/仓库元数据等无关重目录（``MUTATION_COPY_IGNORE``），
        并记录副本内目标文件的原始内容，供每个变异体跑完后恢复。
        """
        if self._staged_cwd is not None:
            return self._staged_cwd
        rel_target = self._relative_target()
        ignored_patterns = [
            "__pycache__", ".git", ".bugx", "node_modules", ".venv",
            "*.pyc", "*.pyo", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        ]
        rel_parts = rel_target.parts
        if "build" not in rel_parts:
            ignored_patterns.append("build")
        if "dist" not in rel_parts:
            ignored_patterns.append("dist")
        copy_ignore = shutil.ignore_patterns(*ignored_patterns)

        tmp_dir = tempfile.mkdtemp(prefix="bugx-mut-sandbox-")
        sandbox_root = Path(tmp_dir)
        staged_cwd = sandbox_root / "work"
        try:
            shutil.copytree(
                self.cwd,
                staged_cwd,
                ignore=copy_ignore,
                symlinks=False,
                ignore_dangling_symlinks=True,
            )
            staged_target = staged_cwd / rel_target
            original = staged_target.read_text(encoding="utf-8")
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        self._sandbox_root = sandbox_root
        self._staged_cwd = staged_cwd
        self._staged_target = staged_target
        self._original_target_source = original
        return staged_cwd

    def cleanup(self) -> None:
        """销毁隔离副本；可重复调用，共享工作树全程未被写入。"""
        root = self._sandbox_root
        self._sandbox_root = None
        self._staged_cwd = None
        self._staged_target = None
        self._original_target_source = None
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    def _build_env(self, cwd: Path | None = None, target_path: Path | None = None) -> dict[str, str]:
        # 白名单继承：被测项目的测试进程不需要、也不应看到宿主的凭据类环境变量。
        # ``cwd`` / ``target_path`` 允许调用方传入隔离副本路径，使 PYTHONPATH
        # 指向副本而非共享工作树（SEC-11）。
        env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
        base_cwd = cwd if cwd is not None else self.cwd
        base_target = target_path if target_path is not None else self.target_path
        extra_paths = [str(base_cwd)]
        if (base_cwd / "src").is_dir():
            extra_paths.append(str(base_cwd / "src"))

        # 向上寻找模块所在的最外层包根目录，避免将包内子目录（如 werkzeug/ 或 werkzeug/sansio/）
        # 错误作为顶层搜索路径加入 PYTHONPATH，导致遮蔽标准库模块（如 http、email 等）
        pkg_root = base_target.parent
        while pkg_root != base_cwd and pkg_root.parent != pkg_root and (pkg_root / "__init__.py").exists():
            pkg_root = pkg_root.parent
        if str(pkg_root) not in extra_paths:
            extra_paths.append(str(pkg_root))

        env["PYTHONPATH"] = os.pathsep.join(extra_paths)
        env["BUGX_TEST_TARGET_DIR"] = str(base_target.parent)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if (self.cwd / "node_modules").is_dir():
            orig_nm = str(self.cwd / "node_modules")
            existing_np = env.get("NODE_PATH", "")
            env["NODE_PATH"] = f"{orig_nm}:{existing_np}" if existing_np else orig_nm
        return env

    def run_baseline(self) -> tuple[bool, str]:
        """运行 Golden Check 原版测试；在隔离副本内执行，共享工作树只读。

        SEC-11：基线与变异体必须在**同一份副本**上运行。若基线跑真实工作树、
        变异体跑副本，而副本又排除了 ``.git`` 等目录，则「测试需要被排除目录」
        的项目会出现「基线通过、变异体全部失败」→ 变异分数被静默抬高。
        统一到副本后，副本缺东西只会表现为基线失败（显式 INEFFECTIVE），
        不会伪装成高覆盖率。
        """
        try:
            staged_cwd = self._ensure_sandbox()
        except Exception as exc:
            return False, f"隔离副本创建失败: {exc}"
        try:
            baseline_start = time.perf_counter()
            proc = subprocess.run(
                self.test_command,
                cwd=str(staged_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._build_env(cwd=staged_cwd, target_path=self._staged_target),
                timeout=max(self.timeout_sec * 3, 10.0),
            )
            self._baseline_duration_sec = time.perf_counter() - baseline_start
            if proc.returncode == 0:
                return True, "Baseline OK"
            return False, f"原版测试失败 (退出码 {proc.returncode}):\n{proc.stderr}\n{proc.stdout}"
        except subprocess.TimeoutExpired:
            return False, f"原版基线测试运行超时 (> {self.timeout_sec * 3}s)"
        except Exception as exc:
            return False, f"原版测试执行异常: {exc}"

    def _mutant_timeout(self) -> float:
        """变异体看门狗：以基线实测耗时为准，而非固定绝对值（FIX-003 D1）。

        固定 2s 在「测试模块运行 ≥ 2s」时会让每个变异体都超时并被计为击杀，
        从而伪造 100%/ELITE。这里取 ``max(配置下限, 基线*3 + 1s)``：
        配置值仍是**下限**（不降低既有严格度），基线足够慢时自动放宽。
        """
        base = self._baseline_duration_sec
        if base is None or base <= 0:
            return self.timeout_sec
        return max(self.timeout_sec, base * 3.0 + 1.0)

    def run_mutant(self, mutant: Mutant) -> MutantExecutionResult:
        """在隔离副本中执行变异杀伤测试，绝不改写共享工作树源码。

        SEC-11：历史实现直接原地覆写 ``self.target_path``，并写出
        ``.bugx_mut_bak`` 备份用于恢复。并发 Worker 共享同一工作树时会读到
        彼此的变异源码（污染 ``target_fingerprint``），崩溃/中断还会残留
        ``.bugx_mut_bak`` 与半变异源码。

        现改为：把 ``self.cwd`` 复制到私有 ``tempfile`` 目录（**整个验证运行
        只复制一次**，见 :meth:`_ensure_sandbox`），只在副本内应用
        ``mutant.mutated_source``，并让测试进程的 cwd / PYTHONPATH 全部指向
        副本。每个变异体跑完后把副本内目标文件恢复成原始内容，避免上一个
        变异体的改动泄漏到下一个。共享工作树全程只读。
        """
        rel_target = self._relative_target()
        staged_cwd = self._ensure_sandbox()
        staged_target = self._staged_target
        assert staged_target is not None  # _ensure_sandbox 已保证

        start_time = time.perf_counter()
        status = "UNKNOWN"
        diagnostic = ""
        duration_ms = 0.0

        try:
            # 1. 只改写副本内的目标文件，共享工作树保持只读
            staged_target.write_text(mutant.mutated_source, encoding="utf-8")

            # 2. 在副本内执行测试命令，测试导入的是变异后的副本
            proc = subprocess.run(
                self.test_command,
                cwd=str(staged_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._build_env(cwd=staged_cwd, target_path=staged_target),
                timeout=self._mutant_timeout(),
            )
            duration_ms = (time.perf_counter() - start_time) * 1000.0

            if proc.returncode != 0:
                status = "KILLED"
                diagnostic = f"测试捕获变异 (退出码 {proc.returncode})"
            else:
                status = "SURVIVED"
                diagnostic = "测试通过，未能捕获此逻辑变异"

        except subprocess.TimeoutExpired:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            status = "KILLED_TIMEOUT"
            diagnostic = f"变异导致超时 (> {self._mutant_timeout():.1f}s)，被超时看门狗捕获"
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            status = "INVALID"
            diagnostic = f"变异体执行异常: {exc}"
        finally:
            # 3. 恢复副本内目标文件，使下一个变异体从干净副本开始。
            #    必须在 finally 中执行：变异体超时/异常时若忘记恢复，后续
            #    变异体会带着上一个变异体的源码运行，结果全部失真。
            if self._original_target_source is not None:
                try:
                    staged_target.write_text(
                        self._original_target_source, encoding="utf-8",
                    )
                except OSError:
                    pass

        return MutantExecutionResult(
            mutant_id=mutant.mutant_id,
            operator=mutant.operator,
            lineno=mutant.lineno,
            status=status,
            duration_ms=round(duration_ms, 2),
            description=mutant.description,
            diagnostic=diagnostic,
        )

class TestCapabilityEvaluator:
    """测试用例能力量化评估器。"""

    @classmethod
    def evaluate(
        cls,
        target_path: str | Path,
        test_target: str,
        results: Sequence[MutantExecutionResult],
        total_time_sec: float,
    ) -> CapabilitySummary:
        total = len(results)
        # FIX-003 D1：KILLED_TIMEOUT 是「测量失效」而非「击杀」。把超时计入击杀
        # 会在测试模块本身 ≥ 超时时伪造 100%/ELITE（实测 8/8 全超时仍报 ELITE）。
        killed = sum(1 for r in results if r.status == "KILLED")
        survived = sum(1 for r in results if r.status == "SURVIVED")
        timeouts = sum(1 for r in results if r.status == "KILLED_TIMEOUT")

        score = round((killed / total * 100.0) if total > 0 else 0.0, 2)

        # 评定等级
        if score >= LEVEL_ELITE_THRESHOLD:
            level = "ELITE"
        elif score >= LEVEL_CAPABLE_THRESHOLD:
            level = "CAPABLE"
        elif score >= LEVEL_WEAK_THRESHOLD:
            level = "WEAK"
        else:
            level = "INEFFECTIVE"

        # FIX-003 D1：存在超时 ⇒ 测量不可靠，绝不冒领 ELITE。
        if timeouts > 0 and level == "ELITE":
            level = "CAPABLE"

        # 算子分项击杀率统计
        op_groups: dict[str, list[MutantExecutionResult]] = {}
        for r in results:
            op_groups.setdefault(r.operator, []).append(r)

        operator_scores: dict[str, dict[str, Any]] = {}
        for op, op_res in sorted(op_groups.items()):
            op_total = len(op_res)
            op_killed = sum(1 for r in op_res if r.status == "KILLED")
            op_score = round(op_killed / op_total * 100.0, 2) if op_total > 0 else 0.0
            operator_scores[op] = {
                "name": op,
                "description": OPERATOR_DESCRIPTIONS.get(op, op),
                "total": op_total,
                "killed": op_killed,
                "survived": op_total - op_killed,
                "score": op_score,
            }

        # 存活变异体详情
        survived_details = [
            {
                "mutant_id": r.mutant_id,
                "operator": r.operator,
                "lineno": r.lineno,
                "description": r.description,
            }
            for r in results
            if r.status == "SURVIVED"
        ]

        # 智能诊断建议
        recommendations = cls._generate_recommendations(
            score, level, operator_scores, survived_details, timeouts=timeouts,
        )

        return CapabilitySummary(
            target_path=str(target_path),
            test_target=test_target,
            total_mutants=total,
            killed_mutants=killed,
            survived_mutants=survived,
            timeout_mutants=timeouts,
            mutation_score=score,
            capability_level=level,
            operator_scores=operator_scores,
            survived_mutants_details=survived_details,
            recommendations=recommendations,
            execution_time_sec=round(total_time_sec, 2),
        )

    @staticmethod
    def _generate_recommendations(
        score: float,
        level: str,
        op_scores: dict[str, dict[str, Any]],
        survived: list[dict[str, Any]],
        timeouts: int = 0,
    ) -> list[str]:
        recs: list[str] = []
        if timeouts > 0:
            recs.append(
                f"【测量不可靠】{timeouts} 个变异体被超时看门狗终止，不构成有效击杀；"
                "请提高 --timeout 或缩小被测模块范围，否则击杀率会被系统性低估。"
            )
        if level in ("INEFFECTIVE", "WEAK"):
            recs.append("【高危弱用例警示】测试用例断言严重不足或存在假断言 (Fake Assertion)，仅断言非空不足以杀死语义突变。")

        for op, data in op_scores.items():
            if data["score"] < 60.0 and data["survived"] > 0:
                if op == OP_ROR:
                    recs.append(f"【强化边界断言】关系比较算子 (ROR) 击杀率偏低 ({data['score']}%)，请补充边界条件测试 (如 0, 负数, 极端大值).")
                elif op == OP_AOR:
                    recs.append(f"【强化计算断言】算术运算符 (AOR) 击杀率偏低 ({data['score']}%)，用例需校验具体的运算数值而非仅校验返回值类型.")
                elif op == OP_BCR:
                    recs.append(f"【强化布尔/零值断言】边界常量 (BCR) 存在存活变异体，请增加对 False/True 与 0 边界特判用例.")
                elif op == OP_EOR:
                    recs.append(f"【强化返回值与异常检验】EOR 击杀率低 ({data['score']}%)，请使用 assertRaises 显式断言预期异常.")

        if survived:
            lines = sorted({str(s['lineno']) for s in survived[:5]})
            recs.append(f"优先在以下源码行补充精确断言：第 {', '.join(lines)} 行.")

        if not recs:
            recs.append("测试用例具备卓越的逻辑杀伤力，有效防范微观缺陷注入。")

        return recs


def to_coverage_intelligence_format(summary: CapabilitySummary) -> dict[str, Any]:
    """生成兼容 core/coverage_intelligence.py 变异格式的结构化数据。"""
    locations = [
        {"line": s["lineno"], "operator": s["operator"], "status": "survived"}
        for s in summary.survived_mutants_details
    ]
    return {
        "format": "mutation",
        "files": [
            {
                "path": summary.target_path,
                "killed": summary.killed_mutants,
                "total": summary.total_mutants,
                "mutation": summary.mutation_score,
                "locations": locations,
            }
        ]
    }


def verify_test_capability(
    target_path: str | Path,
    test_target: str | Sequence[str],
    max_mutants: int = 30,
    timeout_sec: float = 2.0,
    operators: Sequence[str] | None = None,
    cwd: str | Path | None = None,
) -> CapabilitySummary:
    """高层执行入口：变异体生成 -> 基线检查 -> 沙箱执行 -> 综合评估。"""
    t_start = time.perf_counter()
    mutator = SemanticMutator(target_path)
    mutants = mutator.generate_mutants(max_mutants=max_mutants, operators=operators)

    runner = MutationSandboxRunner(
        target_path=target_path,
        test_command=test_target,
        timeout_sec=timeout_sec,
        cwd=cwd,
    )
    # 报告里记录归一化后的 argv，便于复核实际执行入口。
    display_target = " ".join(runner.test_command)

    # SEC-11：隔离副本由 runner 惰性创建（整个运行只建一次），
    # 无论成功、基线失败还是异常，都必须在 finally 中销毁。
    try:
        # 1. 基线 Golden Check
        baseline_ok, msg = runner.run_baseline()
        if not baseline_ok:
            t_cost = time.perf_counter() - t_start
            # 基线失败直接评为 INEFFECTIVE 并说明原因
            return CapabilitySummary(
                target_path=str(target_path),
                test_target=display_target,
                total_mutants=len(mutants),
                killed_mutants=0,
                survived_mutants=len(mutants),
                timeout_mutants=0,
                mutation_score=0.0,
                capability_level="INEFFECTIVE",
                operator_scores={},
                survived_mutants_details=[],
                recommendations=[f"【门禁阻断】{msg}"],
                execution_time_sec=round(t_cost, 2),
            )

        # 2. 沙箱执行杀伤测试（复用同一份隔离副本）
        results: list[MutantExecutionResult] = []
        for mutant in mutants:
            res = runner.run_mutant(mutant)
            results.append(res)

        t_cost = time.perf_counter() - t_start
        return TestCapabilityEvaluator.evaluate(
            target_path=target_path,
            test_target=display_target,
            results=results,
            total_time_sec=t_cost,
        )
    finally:
        runner.cleanup()


# ---------------------------------------------------------------------------
# CLI 入口实现
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bugx test-verify",
        description="BugX 测试用例能力动态验证框架 (Mutation Testing Verifier)",
    )
    sub = parser.add_subparsers(dest="subcommand", help="子命令")

    # verify 子命令
    p_verify = sub.add_parser("verify", help="动态验证测试用例对目标代码的变异杀伤能力")
    p_verify.add_argument("--target", required=True, help="被测源码文件路径 (例如 core/foo.py)")
    p_verify.add_argument("--test", required=True, help="测试用例模块或路径 (例如 tests/test_foo.py 或 tests.test_foo)")
    p_verify.add_argument("--max-mutants", type=int, default=30, help="最大变异体数量 (默认 30)")
    p_verify.add_argument("--timeout", type=float, default=2.0, help="单次变异测试超时秒数 (默认 2.0s)")
    p_verify.add_argument("--strict", action="store_true", help="严格门禁模式 (击杀率 < 60%% 时非零退出)")
    p_verify.add_argument("--json", action="store_true", help="输出完整 JSON 结构报告")

    # mutate 子命令 (仅生成预览变异体)
    p_mut = sub.add_parser("mutate", help="仅生成并预览 AST 变异体")
    p_mut.add_argument("--target", required=True, help="目标源码路径")
    p_mut.add_argument("--max-mutants", type=int, default=30, help="最大变异体数量")
    p_mut.add_argument("--json", action="store_true", help="以 JSON 输出变异体清单")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.subcommand or args.subcommand == "verify":
        target = getattr(args, "target", None)
        test = getattr(args, "test", None)
        if not target or not test:
            parser.print_help()
            return 1

        summary = verify_test_capability(
            target_path=target,
            test_target=test,
            max_mutants=args.max_mutants,
            timeout_sec=args.timeout,
        )

        if args.json:
            print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
        else:
            print("=" * 68)
            print(" BugX 测试用例能力动态验证报告 (Mutation Capability Report)")
            print("=" * 68)
            print(f"被测源码: {summary.target_path}")
            print(f"测试目标: {summary.test_target}")
            print(f"变异体数: {summary.total_mutants} 个")
            print(f"击杀数量: {summary.killed_mutants} 个 (其中超时 {summary.timeout_mutants} 个)")
            print(f"存活数量: {summary.survived_mutants} 个")
            print(f"击杀比例: {summary.mutation_score}%")
            print(f"能力评级: [{summary.capability_level}]")
            print(f"执行耗时: {summary.execution_time_sec}s")
            print("-" * 68)
            print("各变异算子击杀率:")
            for op, d in summary.operator_scores.items():
                print(f"  - {op:<4} ({d['description']}): {d['killed']}/{d['total']} ({d['score']}%)")
            print("-" * 68)
            print("智能加固建议:")
            for rec in summary.recommendations:
                print(f"  * {rec}")
            print("=" * 68)

        if args.strict and summary.mutation_score < LEVEL_CAPABLE_THRESHOLD:
            return 2
        return 0

    elif args.subcommand == "mutate":
        mutator = SemanticMutator(args.target)
        mutants = mutator.generate_mutants(max_mutants=args.max_mutants)
        if args.json:
            print(json.dumps([m.to_dict() for m in mutants], indent=2, ensure_ascii=False))
        else:
            print(f"成功为 {args.target} 生成 {len(mutants)} 个有效变异体：")
            for m in mutants:
                print(f"[{m.operator}] {m.mutant_id} (行 {m.lineno}): {m.description}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
