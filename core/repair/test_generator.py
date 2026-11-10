#!/usr/bin/env python3
"""core/test_generator.py - 自动化全息测试用例与脚手架生成引擎。

根据目标单元的 AST 符号签名（函数、方法、类、参数类型、默认值）与推荐的测试技术
（boundary, negative-control, property-based, concurrency, state-machine 等），
自动生成具备标准 Arrange-Act-Assert (AAA) 结构的高质量、可执行测试套件或自包含 PoC 脚本。

集成上下文感知测试用例合成器 (ContextualTestSynthesizer) 与毫秒级编译/语法门禁 (verify_code_syntax)。
生成可运行的 AAA 测试脚手架。没有业务 oracle 时只执行 smoke call；不会推测返回值或异常契约。
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
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

__all__ = [
    "ContextualTestSynthesizer",
    "GeneratorError",
    "ParameterInfo",
    "TargetSymbol",
    "TypeDescriptor",
    "extract_target_symbol",
    "generate_standalone_poc",
    "generate_unittest_suite",
    "verify_code_syntax",
]


class GeneratorError(ValueError):
    """测试用例生成异常。"""


@dataclass
class ParameterInfo:
    name: str
    type_hint: str = "Any"
    default_repr: str | None = None
    has_default: bool = False
    is_vararg: bool = False
    is_kwarg: bool = False


@dataclass
class TargetSymbol:
    name: str
    qualname: str
    is_method: bool
    class_name: str | None
    is_async: bool
    parameters: list[ParameterInfo]
    return_hint: str
    docstring: str
    file_path: str
    start_line: int
    end_line: int


@dataclass
class TypeDescriptor:
    """结构化类型元数据描述符。"""

    kind: str  # "primitive", "optional", "union", "list", "dict", "tuple", "set", "path", "any"
    name: str = ""
    inner: TypeDescriptor | None = None
    elements: list[TypeDescriptor] = field(default_factory=list)
    key_type: TypeDescriptor | None = None
    val_type: TypeDescriptor | None = None
    raw_str: str = ""


def _parse_type_ast(node: ast.AST) -> TypeDescriptor:
    """递归解析 AST 形式的复杂类型注解。"""
    if isinstance(node, ast.Name):
        nid = node.id
        if nid.lower() in ("int", "float", "str", "bool", "bytes"):
            return TypeDescriptor(kind="primitive", name=nid.lower(), raw_str=nid)
        if nid in ("Path", "PurePath", "PosixPath", "WindowsPath"):
            return TypeDescriptor(kind="path", name=nid, raw_str=nid)
        if nid in ("list", "List", "Sequence", "Iterable", "MutableSequence"):
            return TypeDescriptor(kind="list", name=nid, inner=TypeDescriptor(kind="any"), raw_str=nid)
        if nid in ("dict", "Dict", "Mapping", "MutableMapping"):
            return TypeDescriptor(kind="dict", name=nid, raw_str=nid)
        if nid in ("set", "Set"):
            return TypeDescriptor(kind="set", name=nid, raw_str=nid)
        if nid in ("tuple", "Tuple"):
            return TypeDescriptor(kind="tuple", name=nid, raw_str=nid)
        if nid in ("None", "NoneType"):
            return TypeDescriptor(kind="primitive", name="None", raw_str="None")
        return TypeDescriptor(kind="any", name=nid, raw_str=nid)

    if isinstance(node, ast.Attribute):
        attr = node.attr
        if attr in ("Path", "PurePath", "PosixPath", "WindowsPath"):
            return TypeDescriptor(kind="path", name=attr, raw_str=attr)
        if attr in ("Optional", "Union", "List", "Dict", "Sequence", "Iterable", "Set", "Tuple", "Any"):
            return _parse_type_ast(ast.Name(id=attr, ctx=ast.Load()))
        return TypeDescriptor(kind="any", name=attr, raw_str=attr)

    if isinstance(node, ast.Constant):
        if node.value is None:
            return TypeDescriptor(kind="primitive", name="None", raw_str="None")
        return TypeDescriptor(kind="primitive", name=str(node.value), raw_str=str(node.value))

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left_desc = _parse_type_ast(node.left)
        right_desc = _parse_type_ast(node.right)
        if left_desc.name == "None":
            return TypeDescriptor(kind="optional", inner=right_desc, raw_str="optional")
        if right_desc.name == "None":
            return TypeDescriptor(kind="optional", inner=left_desc, raw_str="optional")
        elements: list[TypeDescriptor] = []
        for d in (left_desc, right_desc):
            if d.kind == "union":
                elements.extend(d.elements)
            else:
                elements.append(d)
        return TypeDescriptor(kind="union", elements=elements, raw_str="union")

    if isinstance(node, ast.Subscript):
        val_name = ""
        if isinstance(node.value, ast.Name):
            val_name = node.value.id
        elif isinstance(node.value, ast.Attribute):
            val_name = node.value.attr

        if val_name == "Optional":
            inner_desc = _parse_type_ast(node.slice)
            return TypeDescriptor(kind="optional", inner=inner_desc, raw_str="Optional")

        if val_name == "Union":
            elements = []
            if isinstance(node.slice, ast.Tuple):
                for elt in node.slice.elts:
                    elements.append(_parse_type_ast(elt))
            else:
                elements.append(_parse_type_ast(node.slice))
            has_none = any(e.name == "None" for e in elements)
            non_none = [e for e in elements if e.name != "None"]
            if has_none and len(non_none) == 1:
                return TypeDescriptor(kind="optional", inner=non_none[0], raw_str="Optional")
            return TypeDescriptor(kind="union", elements=elements, raw_str="Union")

        if val_name in ("list", "List", "Sequence", "Iterable", "MutableSequence"):
            inner_desc = _parse_type_ast(node.slice)
            return TypeDescriptor(kind="list", inner=inner_desc, raw_str=f"list[{inner_desc.name}]")

        if val_name in ("dict", "Dict", "Mapping", "MutableMapping"):
            k_desc, v_desc = None, None
            if isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2:
                k_desc = _parse_type_ast(node.slice.elts[0])
                v_desc = _parse_type_ast(node.slice.elts[1])
            return TypeDescriptor(kind="dict", key_type=k_desc, val_type=v_desc, raw_str="dict")

        if val_name in ("set", "Set"):
            inner_desc = _parse_type_ast(node.slice)
            return TypeDescriptor(kind="set", inner=inner_desc, raw_str="set")

        if val_name in ("tuple", "Tuple"):
            elts = []
            if isinstance(node.slice, ast.Tuple):
                for elt in node.slice.elts:
                    elts.append(_parse_type_ast(elt))
            else:
                elts.append(_parse_type_ast(node.slice))
            return TypeDescriptor(kind="tuple", elements=elts, raw_str="tuple")

    return TypeDescriptor(kind="any", raw_str=ast.unparse(node) if hasattr(ast, "unparse") else "")


def parse_type_annotation(hint: str) -> TypeDescriptor:
    """解析任意类型注解字符串，返回结构化 TypeDescriptor。"""
    if not hint or hint.strip() in ("", "Any", "object"):
        return TypeDescriptor(kind="any", raw_str=hint or "Any")
    hint_clean = hint.strip()
    try:
        tree = ast.parse(hint_clean, mode="eval")
        return _parse_type_ast(tree.body)
    except Exception:
        # 正则与关键词降级容错
        h_low = hint_clean.lower()
        if "optional[" in h_low or "| none" in h_low or "none |" in h_low:
            return TypeDescriptor(kind="optional", raw_str=hint_clean)
        if "union[" in h_low or " | " in h_low:
            return TypeDescriptor(kind="union", raw_str=hint_clean)
        if "list[dict" in h_low or "sequence[dict" in h_low:
            return TypeDescriptor(kind="list", inner=TypeDescriptor(kind="dict"), raw_str=hint_clean)
        if "list[" in h_low or "sequence[" in h_low:
            return TypeDescriptor(kind="list", raw_str=hint_clean)
        if "dict[" in h_low or "mapping[" in h_low:
            return TypeDescriptor(kind="dict", raw_str=hint_clean)
        if "path" in h_low:
            return TypeDescriptor(kind="path", raw_str=hint_clean)
        if "int" in h_low:
            return TypeDescriptor(kind="primitive", name="int", raw_str=hint_clean)
        if "float" in h_low:
            return TypeDescriptor(kind="primitive", name="float", raw_str=hint_clean)
        if "str" in h_low:
            return TypeDescriptor(kind="primitive", name="str", raw_str=hint_clean)
        if "bool" in h_low:
            return TypeDescriptor(kind="primitive", name="bool", raw_str=hint_clean)
        return TypeDescriptor(kind="any", raw_str=hint_clean)


class ContextualTestSynthesizer:
    """上下文感知、语义推断的测试用例生成器。

    基于参数名称语义推断与复杂类型注解（Optional, Union, list[dict], Path, Sequence 等），
    合成高保真合法基准输入、结构化极端边界值与反事实负对照毒化值。
    """

    # 标准合规 JWT 样例
    SAMPLE_JWT = (
        '"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
        'eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IlRlc3QiLCJpYXQiOjE1MTYyMzkwMjJ9.'
        '4phN4_39Fm8_k7654x_sample"'
    )

    def _match_semantic_param(self, name: str) -> str | None:
        """根据参数名称语义匹配专属基准值。"""
        if not name:
            return None
        norm = name.lower().strip().replace("-", "_")
        parts = set(norm.split("_"))

        # port -> 8080
        if norm == "port" or norm.endswith("_port") or "port" in parts:
            return "8080"

        # amount / balance / price -> 100.0
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("amount", "balance", "price", "cost", "fee")):
            return "100.0"

        # email -> "user_test@example.com"
        if "email" in parts or "mail" in parts or norm.endswith("email"):
            return '"user_test@example.com"'

        # uuid -> "12345678-1234-5678-1234-567812345678"
        if "uuid" in parts or "guid" in parts or norm.endswith("uuid"):
            return '"12345678-1234-5678-1234-567812345678"'

        # timestamp / date -> "2026-10-27T00:00:00Z"
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("timestamp", "date", "datetime", "created_at", "updated_at")):
            return '"2026-10-27T00:00:00Z"'

        # url -> "https://api.example.com/v1/test"
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("url", "uri", "endpoint", "link")):
            return '"https://api.example.com/v1/test"'

        # token -> 合法 JWT 样例
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("token", "jwt", "bearer", "auth_token")):
            return self.SAMPLE_JWT

        # ip -> "127.0.0.1"
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("ip", "ipv4", "host_ip", "client_ip", "ip_address")):
            return '"127.0.0.1"'

        # 通用语义启发式推断
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("username", "user", "author", "owner")):
            return '"test_user"'
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("password", "secret", "passwd")):
            return '"Secret_12345!"'
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("phone", "mobile", "tel")):
            return '"+12345678901"'
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("host", "hostname", "domain")):
            return '"localhost"'

        return None

    def synthesize_valid_value(self, param_name: str = "", type_hint: str = "Any", docstring: str = "") -> str:
        """根据参数名、类型注解及上下文语义合成高保真合法基准输入。"""
        # 1. 优先尝试参数名称语义智能推断
        semantic_val = self._match_semantic_param(param_name)
        if semantic_val is not None:
            return semantic_val

        # 2. 复杂类型注解 AST 解析与合成
        type_desc = parse_type_annotation(type_hint)
        return self._synthesize_from_descriptor(param_name, type_desc)

    def _synthesize_from_descriptor(self, param_name: str, desc: TypeDescriptor) -> str:
        if desc.kind == "optional":
            if desc.inner:
                return self._synthesize_from_descriptor(param_name, desc.inner)
            return "10"

        if desc.kind == "union":
            non_none = [e for e in desc.elements if e.name != "None"]
            target_desc = non_none[0] if non_none else desc.elements[0]
            return self._synthesize_from_descriptor(param_name, target_desc)

        if desc.kind == "path":
            return 'Path("test_file.txt")'

        if desc.kind == "list":
            if desc.inner and desc.inner.kind == "dict":
                return '[{"sample_key": "sample_value"}]'
            if desc.inner and desc.inner.name == "int":
                return "[1, 2, 3]"
            if desc.inner and desc.inner.name == "str":
                return '["item_1", "item_2"]'
            if desc.inner and desc.inner.name == "float":
                return "[1.0, 2.0, 3.0]"
            return '["item_1", "item_2"]'

        if desc.kind == "dict":
            return '{"sample_key": "sample_value"}'

        if desc.kind == "tuple":
            if desc.elements:
                return f"({', '.join(self._synthesize_from_descriptor(param_name, e) for e in desc.elements)})"
            return "(1, 2)"

        if desc.kind == "set":
            return '{"item_1", "item_2"}'

        if desc.kind == "primitive":
            if desc.name == "int":
                p_low = param_name.lower().strip()
                if p_low in ("a", "x", "left", "first"):
                    return "10"
                if p_low in ("b", "y", "right", "second"):
                    return "20"
                if any(k in p_low for k in ("id", "count", "num", "size", "limit", "total")):
                    return "10"
                return "1"

            if desc.name == "float":
                p_low = param_name.lower().strip()
                if p_low in ("a", "x", "left", "first"):
                    return "1.0"
                if p_low in ("b", "y", "right", "second"):
                    return "2.0"
                if any(k in p_low for k in ("rate", "ratio", "weight", "percent")):
                    return "0.5"
                if any(k in p_low for k in ("timeout", "delay", "latency", "duration")):
                    return "5.0"
                return "1.5"

            if desc.name == "str":
                if param_name:
                    return f'"sample_{param_name.lower()}"'
                return '"sample_string"'

            if desc.name == "bool":
                return "True"

            if desc.name == "bytes":
                return 'b"sample_bytes"'

            if desc.name == "None":
                return "None"

        # Any / fallback
        if param_name:
            inferred = _infer_type_from_name_and_annotation(param_name, None)
            if inferred != "Any":
                return self._synthesize_from_descriptor(param_name, parse_type_annotation(inferred))
            return f'"sample_{param_name.lower()}"'
        return '"test_value"'

    def synthesize_boundary_values(self, param_name: str = "", type_hint: str = "Any", docstring: str = "") -> list[str]:
        """根据参数语义与类型注解生成结构化边界测试值与异常变异值。"""
        norm = param_name.lower().strip().replace("-", "_")
        parts = set(norm.split("_"))

        # 语义边界值
        if norm == "port" or norm.endswith("_port") or "port" in parts:
            return ["0", "1", "65535", "65536", "-1"]

        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("amount", "balance", "price", "cost", "fee")):
            return ["0.0", "-0.01", "1000000.0", "float('inf')", "float('nan')"]

        if "email" in parts or "mail" in parts or norm.endswith("email"):
            return ['""', '"user@localhost"', '"invalid-email"', '"a" * 256 + "@example.com"']

        if "uuid" in parts or "guid" in parts or norm.endswith("uuid"):
            return ['""', '"00000000-0000-0000-0000-000000000000"', '"invalid-uuid"']

        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("timestamp", "date", "datetime", "created_at", "updated_at")):
            return ['""', '"1970-01-01T00:00:00Z"', '"9999-12-31T23:59:59Z"', '"invalid-date"']

        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("url", "uri", "endpoint", "link")):
            return ['""', '"http://localhost"', '"not_a_url"', '"https://" + "a" * 2048']

        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("token", "jwt", "bearer", "auth_token")):
            return ['""', '"expired.token.jwt"', '"Bearer "']

        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("ip", "ipv4", "host_ip", "client_ip")):
            return ['""', '"0.0.0.0"', '"255.255.255.255"', '"999.999.999.999"']

        # 复杂类型边界值
        type_desc = parse_type_annotation(type_hint)
        return self._synthesize_boundary_from_desc(param_name, type_desc)

    def _synthesize_boundary_from_desc(self, param_name: str, desc: TypeDescriptor) -> list[str]:
        if desc.kind == "optional":
            inner_boundaries = (
                self._synthesize_boundary_from_desc(param_name, desc.inner)
                if desc.inner
                else ["0", '""']
            )
            return ["None"] + [b for b in inner_boundaries if b != "None"]

        if desc.kind == "union":
            combined: list[str] = []
            has_none = any(e.name == "None" for e in desc.elements)
            if has_none:
                combined.append("None")
            for elem in desc.elements:
                if elem.name != "None":
                    for b in self._synthesize_boundary_from_desc(param_name, elem):
                        if b not in combined:
                            combined.append(b)
            return combined or ["None", '""', "0"]

        if desc.kind == "path":
            return ['Path(".")', 'Path("/dev/null")', 'Path("/nonexistent/file.txt")', 'Path("/etc/passwd")']

        if desc.kind == "list":
            if desc.inner and desc.inner.kind == "dict":
                return ["[]", "[{}]", "[{'nested': {'depth': 100}}]", "[{'key': None}]"]
            if desc.inner and desc.inner.name == "int":
                return ["[]", "[0]", "[-1]", "[1] * 1000"]
            if desc.inner and desc.inner.name == "str":
                return ["[]", '[""]', '["a" * 1000]']
            return ["[]", "[None]", "[i for i in range(1000)]"]

        if desc.kind == "dict":
            return ["{}", '{"__proto__": True}', '{"nested": {"depth": 100}}', '{"": ""}']

        if desc.kind == "primitive":
            if desc.name == "int":
                return ["0", "-1", "9223372036854775807", "-9223372036854775808"]
            if desc.name == "float":
                return ["0.0", "-0.0", "float('inf')", "float('-inf')", "float('nan')", "1e308"]
            if desc.name == "str":
                return ['""', '"A" * 4096', '"🚀你好世界\\x00\\n\\r"', '"../../etc/passwd"', '"\' OR \'1\'=\'1"']
            if desc.name == "bool":
                return ["False", "True"]
            if desc.name == "bytes":
                return [b'""', b'"\\x00" * 1024', b'"\\xff" * 1024']

        return ["None", '""', "0"]

    def synthesize_poison_value(self, param_name: str = "", type_hint: str = "Any") -> str:
        """合成用于负对照的反事实毒化值。"""
        type_desc = parse_type_annotation(type_hint)
        if type_desc.kind == "optional" and type_desc.inner:
            type_desc = type_desc.inner

        if type_desc.kind == "primitive" and type_desc.name in ("int", "float"):
            return '"invalid_string_instead_of_number"'

        if type_desc.kind == "primitive" and type_desc.name == "str":
            return "12345"

        if type_desc.kind == "path":
            return "12345"

        if type_desc.kind == "list":
            return '"string_instead_of_list"'

        if type_desc.kind == "dict":
            return '"string_instead_of_dict"'

        norm = param_name.lower().strip().replace("-", "_")
        parts = set(norm.split("_"))
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("port", "amount", "balance", "price", "count", "num")):
            return '"invalid_string_instead_of_number"'
        if any(k in parts or norm.endswith(f"_{k}") or norm == k for k in ("email", "uuid", "timestamp", "date", "url", "token", "ip")):
            return "12345"

        return "object()"


_DEFAULT_SYNTHESIZER = ContextualTestSynthesizer()


def _sample_valid_value(type_hint: str, param_name: str = "") -> str:
    """向下兼容的合法基准值合成门禁。"""
    return _DEFAULT_SYNTHESIZER.synthesize_valid_value(param_name, type_hint)


def _sample_boundary_values(type_hint: str, param_name: str = "") -> list[str]:
    """向下兼容的边界值集合合成门禁。"""
    return _DEFAULT_SYNTHESIZER.synthesize_boundary_values(param_name, type_hint)


def _sample_poison_value(type_hint: str, param_name: str = "") -> str:
    """向下兼容的毒化值合成门禁。"""
    return _DEFAULT_SYNTHESIZER.synthesize_poison_value(param_name, type_hint)


def verify_code_syntax(
    code_str: str,
    language: str = "python",
    timeout_seconds: float = 2.0,
) -> tuple[bool, str | None]:
    """毫秒级编译与语法合法性检查门禁函数。

    - Python: 进程内 ast.parse 极速校验（微秒级，零子进程开销）；
    - 若语法错误，返回 (False, "Line X, Col Y: SyntaxError msg")；
    - 其它语言: 进程隔离沙箱在 timeout_seconds 约束下编译/静态检查，若缺少环境安全优雅回退。
    """
    lang = (language or "python").lower().strip()
    if lang in ("python", "py", "python3"):
        try:
            ast.parse(code_str)
            return (True, None)
        except SyntaxError as exc:
            lineno = exc.lineno or 1
            offset = exc.offset or 1
            msg = exc.msg or "invalid syntax"
            return (False, f"Line {lineno}, Col {offset}: SyntaxError {msg}")
        except Exception as exc:
            return (False, f"Line 1, Col 1: SyntaxError {exc}")

    # 异构语言本地静态/语法检查（具备 timeout_seconds 硬超时与缺少环境时的优雅安全回退）
    if lang in ("javascript", "js", "node"):
        node_bin = shutil.which("node")
        if not node_bin:
            return (True, None)
        try:
            proc = subprocess.run(
                [node_bin, "--check", "-e", code_str],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            if proc.returncode != 0:
                err = proc.stderr.strip() or proc.stdout.strip()
                return (False, f"Line 1, Col 1: SyntaxError {err}")
            return (True, None)
        except subprocess.TimeoutExpired:
            return (False, f"Line 1, Col 1: SyntaxError check timed out after {timeout_seconds}s")
        except Exception:
            return (True, None)

    if lang in ("bash", "sh", "shell"):
        bash_bin = shutil.which("bash") or shutil.which("sh")
        if not bash_bin:
            return (True, None)
        try:
            proc = subprocess.run(
                [bash_bin, "-n", "-c", code_str],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            if proc.returncode != 0:
                err = proc.stderr.strip() or proc.stdout.strip()
                return (False, f"Line 1, Col 1: SyntaxError {err}")
            return (True, None)
        except subprocess.TimeoutExpired:
            return (False, f"Line 1, Col 1: SyntaxError check timed out after {timeout_seconds}s")
        except Exception:
            return (True, None)

    return (True, None)


def _infer_type_from_name_and_annotation(name: str, annotation_node: ast.AST | None, docstring: str = "") -> str:
    if annotation_node is not None:
        try:
            return ast.unparse(annotation_node).strip()
        except Exception:
            pass

    lower = name.lower()
    if any(k in lower for k in ("id", "count", "num", "size", "length", "port", "age", "index", "code")):
        return "int"
    if any(k in lower for k in ("name", "title", "path", "url", "str", "desc", "msg", "key", "token", "hash", "email", "uuid")):
        return "str"
    if any(k in lower for k in ("is_", "has_", "can_", "flag", "enabled", "strict", "allow")):
        return "bool"
    if any(k in lower for k in ("rate", "ratio", "weight", "timeout", "latency", "score", "factor", "amount", "balance", "price")):
        return "float"
    if any(k in lower for k in ("items", "list", "array", "rows", "lines", "values", "args")):
        return "list"
    if any(k in lower for k in ("data", "payload", "config", "options", "mapping", "dict", "info")):
        return "dict"

    if docstring:
        pattern = re.compile(rf":type\s+{name}\s*:\s*([\w\[\],\s]+)", re.I)
        match = pattern.search(docstring)
        if match:
            return match.group(1).strip()
    return "Any"


def _return_assertion(result_name: str, return_hint: str) -> str:
    """Assert only a concrete declared return type; object/Any carries no oracle."""
    hint = return_hint.strip().strip("'\"")
    if hint in {"None", "NoneType"}:
        return f"self.assertIsNone({result_name})"
    if hint in {"bool", "int", "float", "str", "list", "dict", "tuple", "set", "bytes"}:
        return f"self.assertIsInstance({result_name}, {hint})"
    return ""


def extract_target_symbol(file_path: Path, scope: str) -> TargetSymbol:
    if not file_path.is_file():
        raise GeneratorError(f"目标文件不存在: {file_path}")

    symbol_name = scope.split("::")[-1].strip() if "::" in scope else scope.strip()
    class_name = None
    func_name = symbol_name
    if "." in symbol_name:
        parts = symbol_name.split(".", 1)
        class_name = parts[0]
        func_name = parts[1]

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except Exception as exc:
        raise GeneratorError(f"无法解析目标源文件 AST: {file_path}: {exc}") from exc

    target_node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None = None
    parent_class: str | None = None

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if class_name and node.name == class_name:
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == func_name:
                        target_node = sub
                        parent_class = class_name
                        break
            elif not class_name and node.name == func_name:
                # 目标本身是一个类
                target_node = node
                break
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not class_name and node.name == func_name:
                target_node = node
                break

    if target_node is None:
        raise GeneratorError(f"在 {file_path} 中未找到目标符号: {scope}")

    docstring = ast.get_docstring(target_node) or ""
    start_line = target_node.lineno
    end_line = getattr(target_node, "end_lineno", start_line)

    if isinstance(target_node, ast.ClassDef):
        # 如果目标是类，解析其 __init__
        init_node = next(
            (n for n in target_node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
            None,
        )
        params: list[ParameterInfo] = []
        if init_node:
            args_list = init_node.args
            defaults = [None] * (len(args_list.args) - len(args_list.defaults)) + list(args_list.defaults)
            for arg_node, def_node in zip(args_list.args, defaults):
                if arg_node.arg in ("self", "cls"):
                    continue
                th = _infer_type_from_name_and_annotation(arg_node.arg, arg_node.annotation, docstring)
                def_repr = ast.unparse(def_node) if def_node is not None else None
                params.append(ParameterInfo(
                    name=arg_node.arg,
                    type_hint=th,
                    default_repr=def_repr,
                    has_default=def_node is not None,
                ))
        return TargetSymbol(
            name=target_node.name,
            qualname=target_node.name,
            is_method=False,
            class_name=None,
            is_async=False,
            parameters=params,
            return_hint=target_node.name,
            docstring=docstring,
            file_path=str(file_path),
            start_line=start_line,
            end_line=end_line,
        )

    # 目标是函数或方法
    is_method = parent_class is not None
    is_async = isinstance(target_node, ast.AsyncFunctionDef)
    args_list = target_node.args
    defaults = [None] * (len(args_list.args) - len(args_list.defaults)) + list(args_list.defaults)
    params = []
    for arg_node, def_node in zip(args_list.args, defaults):
        if is_method and arg_node.arg in ("self", "cls"):
            continue
        th = _infer_type_from_name_and_annotation(arg_node.arg, arg_node.annotation, docstring)
        def_repr = ast.unparse(def_node) if def_node is not None else None
        params.append(ParameterInfo(
            name=arg_node.arg,
            type_hint=th,
            default_repr=def_repr,
            has_default=def_node is not None,
        ))

    return_hint = "Any"
    if target_node.returns:
        try:
            return_hint = ast.unparse(target_node.returns).strip()
        except Exception:
            pass

    qualname = f"{parent_class}.{func_name}" if parent_class else func_name
    return TargetSymbol(
        name=func_name,
        qualname=qualname,
        is_method=is_method,
        class_name=parent_class,
        is_async=is_async,
        parameters=params,
        return_hint=return_hint,
        docstring=docstring,
        file_path=str(file_path),
        start_line=start_line,
        end_line=end_line,
    )


def generate_unittest_suite(
    symbol: TargetSymbol,
    techniques: Sequence[str] | None = None,
    output_path: Path | None = None,
    assert_expr: str = "",
    expected_exception: str = "",
) -> str:
    """生成 AAA 测试脚手架；不猜测目标的返回值或非法输入异常契约。"""
    tech_set = set(techniques or ["boundary", "negative-control"])
    assert_expr = assert_expr.strip()
    expected_exception = expected_exception.strip()
    if assert_expr:
        try:
            ast.parse(assert_expr, mode="eval")
        except SyntaxError as exc:
            raise GeneratorError(f"断言表达式无效: {exc.msg}") from exc
    if expected_exception and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
        expected_exception,
    ):
        raise GeneratorError("预期异常必须是类名或限定类名")

    path_obj = Path(symbol.file_path).resolve()
    module_stem = path_obj.stem
    dir_path = str(path_obj.parent)

    lines: list[str] = [
        "#!/usr/bin/env python3",
        f'"""由 BugX 测试生成器合成: 针对 {symbol.qualname} 的 AAA 测试脚手架。',
        "",
        "默认调用是 smoke check；除非调用者提供 oracle，否则不会声称结果或异常契约已验证。",
        "边界输入与负对照仅作候选场景；负对照要求显式预期异常。",
    ]
    if "concurrency" in tech_set:
        lines.append("  5. Concurrency 并发竞态与线程安全测试")
    if "state-machine" in tech_set:
        lines.append("  6. Stateful 状态机非法跃迁与重入测试")
    lines.extend([
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import os",
        "import sys",
        "import unittest",
    ])
    if symbol.is_async:
        lines.append("import asyncio")
    if "concurrency" in tech_set:
        lines.append("import concurrent.futures")
    lines.extend([
        "from pathlib import Path",
        "",
        "# --- 安全引入目标模块 ---",
        f"TARGET_DIR = os.environ.get('BUGX_TEST_TARGET_DIR', {repr(dir_path)})",
        "if TARGET_DIR not in sys.path:",
        "    sys.path.insert(0, TARGET_DIR)",
        "",
        f"import {module_stem} as target_module",
        "",
        "",
        f"class Test{symbol.name.replace('_', ' ').title().replace(' ', '')}Coverage(unittest.TestCase):",
        '    """针对目标符号的标准化 AAA 测试用例集。"""',
        "",
        "    def setUp(self) -> None:",
        '        """[Arrange] 前置环境初始化。"""',
    ])

    if symbol.is_method and symbol.class_name:
        lines.append(f"        # 实例化类 {symbol.class_name}")
        lines.append(f"        cls_obj = getattr(target_module, {repr(symbol.class_name)}, None)")
        lines.append("        self.assertIsNotNone(cls_obj, '被测类不存在')")
        lines.append("        try:")
        lines.append("            self.instance = cls_obj()")
        lines.append("        except TypeError:")
        lines.append("            # 构造函数需要参数时的降级安全实例化")
        lines.append("            self.instance = cls_obj.__new__(cls_obj)")
    else:
        lines.append("        pass")

    lines.extend([
        "",
        "    def tearDown(self) -> None:",
        '        """[Teardown] 后置清理与资源释放。"""',
        "        pass",
        "",
    ])

    synthesizer = _DEFAULT_SYNTHESIZER

    # 1. Happy Path 测试
    lines.extend([
        "    def test_happy_path_execution(self) -> None:",
        '        """[Happy Path] 验证合法基准输入下的正常执行与有效返回值。"""',
        "        # Arrange",
    ])
    for p in symbol.parameters:
        lines.append(f"        {p.name} = {synthesizer.synthesize_valid_value(p.name, p.type_hint)}")

    caller = f"self.instance.{symbol.name}" if symbol.is_method else f"target_module.{symbol.name}"
    call_stmt = f"{caller}({', '.join(p.name for p in symbol.parameters)})"
    if symbol.is_async:
        call_stmt = f"asyncio.run({call_stmt})"

    lines.extend([
        "",
        "        # Act",
        f"        res = {call_stmt}",
    ])
    if assert_expr:
        lines.extend([
            "",
            "        # Assert",
            f"        self.assertTrue({assert_expr}, '调用结果不符合提供的 oracle')",
        ])
    else:
        inferred_assertion = _return_assertion("res", symbol.return_hint)
        if inferred_assertion:
            lines.extend(["", "        # Assert", f"        {inferred_assertion}"])
        else:
            lines.extend([
                "",
                "        # Smoke call only: provide --assert-expr for a semantic oracle.",
            ])
    lines.append("")

    # 2. Boundary 测试
    lines.extend([
        "    def test_boundary_values(self) -> None:",
        '        """[Boundary] 验证极限边界参数下的防御性行为（未发生无防护崩溃）。"""',
    ])
    if not symbol.parameters:
        lines.append("        # 目标无参数，直接调用验证边界")
        lines.append(f"        res = {call_stmt}")
        inferred_assertion = _return_assertion("res", symbol.return_hint)
        if inferred_assertion:
            lines.append(f"        {inferred_assertion}")
    else:
        first_param = symbol.parameters[0]
        boundaries = synthesizer.synthesize_boundary_values(first_param.name, first_param.type_hint)
        lines.append(f"        boundary_candidates = [{', '.join(boundaries)}]")
        lines.append("        for b_val in boundary_candidates:")
        lines.append("            with self.subTest(boundary_val=b_val):")
        lines.append("                try:")
        boundary_args = [f"b_val" if idx == 0 else synthesizer.synthesize_valid_value(p.name, p.type_hint)
                         for idx, p in enumerate(symbol.parameters)]
        b_call = f"{caller}({', '.join(boundary_args)})"
        if symbol.is_async:
            b_call = f"asyncio.run({b_call})"
        lines.append(f"                    res = {b_call}")
        inferred_assertion = _return_assertion("res", symbol.return_hint)
        if inferred_assertion:
            lines.append(f"                    {inferred_assertion}")
        lines.append("                except (ValueError, TypeError, KeyError, IndexError, ArithmeticError):")
        lines.append("                    # 预期内的合法输入防御异常")
        lines.append("                    pass")
    lines.append("")

    # 3. A negative control needs a caller-supplied exception contract.
    if "negative-control" in tech_set and expected_exception and symbol.parameters:
        lines.extend([
            "    def test_negative_control_dual_oracle(self) -> None:",
            '        """[Negative Control] 使用调用者声明的异常契约做反事实对照。"""',
        ])
        for p in symbol.parameters:
            lines.append(f"        valid_{p.name} = {synthesizer.synthesize_valid_value(p.name, p.type_hint)}")
        normal_call = f"{caller}({', '.join(f'valid_{p.name}' for p in symbol.parameters)})"
        if symbol.is_async:
            normal_call = f"asyncio.run({normal_call})"
        lines.append(f"        res = {normal_call}")
        if assert_expr:
            lines.append(f"        self.assertTrue({assert_expr}, '正向对照不符合提供的 oracle')")
        else:
            inferred_assertion = _return_assertion("res", symbol.return_hint)
            if inferred_assertion:
                lines.append(f"        {inferred_assertion}")
        lines.append("")
        poison_param = symbol.parameters[0]
        poison_val = synthesizer.synthesize_poison_value(poison_param.name, poison_param.type_hint)
        poison_args = ["poison_val" if idx == 0 else f"valid_{p.name}"
                       for idx, p in enumerate(symbol.parameters)]
        poison_call = f"{caller}({', '.join(poison_args)})"
        if symbol.is_async:
            poison_call = f"asyncio.run({poison_call})"
        lines.append(f"        poison_val = {poison_val}")
        lines.append(f"        with self.assertRaises({expected_exception}):")
        lines.append(f"            {poison_call}")
        lines.append("")
    lines.append("")

    # 4. 并发测试（若策略包含 concurrency）
    if "concurrency" in tech_set:
        lines.extend([
            "    def test_concurrency_race_safety(self) -> None:",
            '        """[Concurrency] 并发多线程交错轰炸，检测 TOCTOU 与竞态状态破坏。"""',
            "        errors: list[Exception] = []",
            "        def worker():",
            "            try:",
            f"                {call_stmt}",
            "            except Exception as exc:",
            "                errors.append(exc)",
            "",
            "        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:",
            "            futures = [pool.submit(worker) for _ in range(20)]",
            "            concurrent.futures.wait(futures)",
            "",
            "        self.assertEqual(len(errors), 0, f'并发执行发生未受控竞争错误: {errors}')",
            "",
        ])

    # 5. 状态机测试（若策略包含 state-machine）
    if "state-machine" in tech_set:
        lines.extend([
            "    def test_state_reentrancy_and_idempotency(self) -> None:",
            '        """[Stateful] 状态机重入与重复操作连续调用校验。"""',
            f"        first_run = {call_stmt}",
            f"        second_run = {call_stmt}",
            "        # 连续两次调用不应导致内部状态崩溃",
        ])
        first_assertion = _return_assertion("first_run", symbol.return_hint)
        second_assertion = _return_assertion("second_run", symbol.return_hint)
        if first_assertion and second_assertion:
            lines.extend([f"        {first_assertion}", f"        {second_assertion}"])
        lines.append("")

    lines.extend([
        'if __name__ == "__main__":',
        "    unittest.main()",
        "",
    ])

    content = "\n".join(lines)

    # 确定性物理语法门禁校验
    is_valid, syntax_err = verify_code_syntax(content, language="python")
    if not is_valid:
        raise GeneratorError(f"生成的单元测试代码未通过语法编译门禁: {syntax_err}")

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
    return content


def generate_standalone_poc(
    symbol: TargetSymbol,
    assert_expr: str = "",
    expected_exception: str = "",
    setup_code: str = "",
    custom_call: str = "",
    output_path: Path | None = None,
) -> str:
    """合成单文件、零依赖自包含独立验证脚本，严格遵循 0/1 退出码契约。"""
    from core.repair.minimize_repro import synthesize_standalone_repro

    path_obj = Path(symbol.file_path).resolve()
    caller = f"target.{symbol.name}"
    synthesizer = _DEFAULT_SYNTHESIZER
    if custom_call.strip():
        call_expr = custom_call.strip()
    elif symbol.is_method and symbol.class_name:
        call_expr = f"target.{symbol.class_name}().{symbol.name}({', '.join(synthesizer.synthesize_valid_value(p.name, p.type_hint) for p in symbol.parameters)})"
    else:
        call_expr = f"{caller}({', '.join(synthesizer.synthesize_valid_value(p.name, p.type_hint) for p in symbol.parameters)})"

    if "Path(" in call_expr and "from pathlib import Path" not in setup_code:
        setup_code = ("from pathlib import Path\n" + setup_code).strip()

    code = synthesize_standalone_repro(
        target_module=str(path_obj),
        call_expr=call_expr,
        expected_exception=expected_exception,
        assert_expr=assert_expr,
        setup_code=setup_code,
        output_path=output_path,
    )

    # 确定性物理语法门禁校验
    is_valid, syntax_err = verify_code_syntax(code, language="python")
    if not is_valid:
        if output_path and Path(output_path).exists():
            Path(output_path).unlink(missing_ok=True)
        raise GeneratorError(f"生成的独立 PoC 脚本未通过语法编译门禁: {syntax_err}")

    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目根路径")
    parser.add_argument("--file", type=Path, help="目标源文件路径（相对或绝对）")
    parser.add_argument("--scope", required=True, help="被测单元符号范围，如 path/foo.py::MyClass.func")
    parser.add_argument("--techniques", default="boundary,negative-control", help="测试策略逗号分隔列表")
    parser.add_argument("--out", type=Path, help="生成的测试文件保存路径")
    parser.add_argument("--standalone", action="store_true", help="是否生成单文件 0/1 独立验证 PoC 脚本")
    parser.add_argument("--assert-expr", default="", help="PoC 模式下的成功断言表达式")
    parser.add_argument("--expected-exc", default="", help="PoC 模式下的预期缺陷异常类名")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    scope = args.scope.strip()
    if args.file:
        target_file = (root / args.file).resolve() if not args.file.is_absolute() else args.file
    elif "::" in scope:
        target_file = (root / scope.split("::", 1)[0]).resolve()
    else:
        target_file = (root / scope).resolve()

    try:
        symbol = extract_target_symbol(target_file, scope)
        techniques = [t.strip() for t in args.techniques.split(",") if t.strip()]

        if args.standalone:
            code = generate_standalone_poc(
                symbol,
                assert_expr=args.assert_expr,
                expected_exception=args.expected_exc,
                output_path=args.out,
            )
        else:
            code = generate_unittest_suite(
                symbol,
                techniques=techniques,
                output_path=args.out,
                assert_expr=args.assert_expr,
                expected_exception=args.expected_exc,
            )

        if args.json:
            result = {
                "status": "success",
                "scope": scope,
                "target_symbol": {
                    "name": symbol.name,
                    "qualname": symbol.qualname,
                    "is_method": symbol.is_method,
                    "is_async": symbol.is_async,
                    "parameters": [
                        {"name": p.name, "type_hint": p.type_hint, "has_default": p.has_default}
                        for p in symbol.parameters
                    ],
                },
                "techniques": techniques,
                "output_path": str(args.out) if args.out else None,
                "lines_count": len(code.splitlines()),
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if args.out:
                print(f"[test_generator] ✓ 成功为 {scope} 生成测试脚手架 -> {args.out}")
            else:
                print(code)
        return 0
    except GeneratorError as exc:
        print(f"[test_generator] ✗ 错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
