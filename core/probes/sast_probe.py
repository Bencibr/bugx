#!/usr/bin/env python3
"""探测并执行 SAST 静态分析与轻量 SCA 依赖安全扫描 (DOC-061)。

SAST 层定位：
1. 规则驱动的已知漏洞模式扫描（SQLi/XSS/SSRF/RCE/SCA/反模式/反序列化/机密泄漏）；
2. 零外部依赖多语言支持：内置通用结构化分词与括号平衡树引擎，
   原生覆盖 Go、Java、TypeScript/JavaScript、Rust、C/C++、Python、PHP 等主流语言；
3. 供应链安全 (SCA)：内置轻量离线依赖漏洞匹配器 (pom.xml/package.json/go.mod/requirements.txt/Cargo.lock)；
4. 企业级工程治理：支持跨语言行级抑制 (// bugx-ignore)、基线快照 (--baseline) 与 OASIS SARIF v2.1.0 标准导出；
5. 非侵入容灾原则：严禁强制外部工具安装，无外部工具时 100% 自动无缝降级至内置多语言引擎。
"""

from __future__ import annotations
from typing import Any
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
import collections
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import sys
from pathlib import Path

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR), str(_CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.probes.sast_rules import BUILTIN_RULES, SASTRuleMatcher
from core.probes.sca_scanner import SCAScanner

from core.planning.dynamic_flow_analyzer import DynamicFlowAnalyzer

from core.runtime.tool_install_targets import OPTIONAL_TOOL_PACKAGE_SPECS
from core.retrieval.universal_code_matcher import EXT_TO_LANG, ParsedSource, UniversalCodeMatcher

VALIDATED = {
    "semgrep": "1.80.0",
    "codeql": "2.18.0",
    "trivy": "0.54.0",
    "bandit": "1.8.6",
}
# Exact package pins remain separate from VALIDATED: an install target is not
# evidence that this checkout exercised the corresponding runtime.
MINIMUM = {
    "semgrep": "1.50.0",
    "codeql": "2.15.0",
    "trivy": "0.40.0",
    "bandit": "1.7.0",
}
VERSION_FLAG = {"codeql": "version"}

SKIP_DIRS = {
    ".git", ".bugx", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", "Pods", ".gradle", ".idea",
}


def _skipped(relative: Path) -> bool:
    """SAST 也要排除引擎/宿主的 ``.bugx*`` 运行态命名空间。

    此前只按 ``SKIP_DIRS`` 的字面名排除 ``.bugx``，``.bugx-audit-artifacts``
    这类兄弟目录会被当源码扫描——实测靶场里宿主写的 ``repro.py`` 就产生了
    两条 ``SEC-006-SILENT-EXCEPTION``，被报告当成被测产品的缺陷。
    判定与覆盖枚举同源（``core.retrieval.symbol_index``），避免两份名单漂移。
    """
    from core.retrieval.symbol_index import _is_engine_namespace_segment

    return any(
        part in SKIP_DIRS or _is_engine_namespace_segment(part)
        for part in relative.parts
    )


def _probe_environment() -> dict[str, str]:
    try:
        try:
            from .probe_registry import normalized_tool_environment
        except (ImportError, ValueError):
            try:
                from .probe_registry import normalized_tool_environment
            except ImportError:
                from core.probes.probe_registry import normalized_tool_environment
        return normalized_tool_environment()
    except ImportError:
        return dict(os.environ)


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+){1,3})", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


_TOOL_PROBE_CACHE: dict[str, dict] = {}


def clear_tool_probe_cache() -> None:
    _TOOL_PROBE_CACHE.clear()


def _tool_candidates(names: tuple[str, ...], search_path: str) -> list[str]:
    """Return deduplicated executables in PATH order for the requested aliases."""
    directories = [entry for entry in search_path.split(os.pathsep) if entry]
    candidates: list[str] = []
    seen: set[str] = set()
    for name in names:
        for directory in directories:
            candidate = shutil.which(name, path=directory)
            if not candidate:
                continue
            identity = os.path.normcase(os.path.realpath(candidate))
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(candidate)
    return candidates


def _candidate_signature(candidates: list[str], search_path: str) -> tuple:
    """Invalidate cached versions when PATH candidates or their files change."""
    signatures = []
    for candidate in candidates:
        try:
            stat = Path(candidate).stat()
            file_signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_signature = None
        signatures.append((candidate, os.path.realpath(candidate), file_signature))
    return search_path, tuple(signatures)


def _version_at_least(actual: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    width = max(len(actual), len(minimum))
    return actual + (0,) * (width - len(actual)) >= minimum + (0,) * (width - len(minimum))


def _command(names: tuple[str, ...], expected: str) -> dict:
    environment = _probe_environment()
    search_path = environment.get("PATH", "")
    candidates = _tool_candidates(names, search_path)
    if not candidates:
        return {"name": expected, "ok": False, "path": None, "version": None,
                "detail": "未安装"}

    signature = _candidate_signature(candidates, search_path)
    cached = _TOOL_PROBE_CACHE.get(expected)
    if cached and cached.get("_signature") == signature:
        return {key: value for key, value in cached.items() if key != "_signature"}

    flag = VERSION_FLAG.get(expected, "--version")
    minimum = version_tuple(MINIMUM[expected])
    observations: list[dict] = []
    transient_failure = False
    for path in candidates:
        try:
            result = subprocess.run(
                [path, flag], capture_output=True, text=True,
                check=False, timeout=10, env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            transient_failure = True
            observations.append({
                "path": path, "version": None, "detail": type(exc).__name__,
            })
            continue
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        version_text = next(
            (line.strip() for line in output.splitlines() if version_tuple(line)), "",
        )
        actual = version_tuple(version_text)
        if result.returncode == 0 and actual and _version_at_least(actual, minimum):
            info = {
                "name": expected,
                "ok": True,
                "path": path,
                "version": version_text,
                "minimum": MINIMUM[expected],
                "validated": VALIDATED[expected],
                "detail": version_text,
                "rejected_candidates": observations,
            }
            _TOOL_PROBE_CACHE[expected] = {**info, "_signature": signature}
            return info
        observations.append({
            "path": path,
            "version": version_text or None,
            "detail": version_text or f"--version exited {result.returncode}",
        })

    first = observations[0] if observations else {}
    info = {
        "name": expected,
        "ok": False,
        "path": first.get("path", candidates[0]),
        "version": first.get("version"),
        "minimum": MINIMUM[expected],
        "validated": VALIDATED[expected],
        "detail": f"未找到满足最低版本 {MINIMUM[expected]} 的 PATH 候选",
        "rejected_candidates": observations,
    }
    if not transient_failure:
        _TOOL_PROBE_CACHE[expected] = {**info, "_signature": signature}
    return info


def check_tool(name: str) -> dict:
    """Check one registered SAST executable using the same PATH/version policy."""
    if name not in MINIMUM:
        raise ValueError(f"不支持的 SAST 工具: {name}")
    return _command((name,), name)


def inspect(root: Path) -> dict:
    root = root.resolve()
    base = [
        _command(("semgrep",), "semgrep"),
        _command(("codeql",), "codeql"),
        _command(("trivy",), "trivy"),
        _command(("bandit",), "bandit"),
    ]
    ready = any(item["ok"] for item in base)
    return {
        "status": "READY" if ready else "DEGRADED",
        "required": False,
        "recommended": True,
        "root": str(root),
        "base_tools": base,
        "install": {
            "semgrep": f"pipx install {OPTIONAL_TOOL_PACKAGE_SPECS['semgrep']}",
            "codeql": "https://github.com/github/codeql-cli-binaries/releases",
            "trivy": "brew install trivy",
            "bandit": f"pipx install {OPTIONAL_TOOL_PACKAGE_SPECS['bandit']}",
        },
        "fallback": "pattern_probe.py scan + ast-grep 有界静态扫描",
        "workflow": "SAST 扫描出候选点 → codebase-memory trace_path 验 Source→Sink → PostMCP/Playwright 发 PoC 复现",
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"[{report['status']}] SAST 静态分析工具链（推荐，非硬依赖，支持多语言自动降级）")
    for item in report["base_tools"]:
        print(f"  {'✓' if item['ok'] else '!'} {item['name']}: {item['detail']}")
    if report["status"] != "READY":
        print(f"  → 回退：{report['fallback']}")
        print("  → 架构契约：非侵入式零依赖，未安装外部工具时直接使用内置多语言分析引擎，无需配置或编译环境。")


#: 常见的「占位符」值片段：命中即说明是文档/测试假凭证而非真实机密。
_SECRET_PLACEHOLDER_HINTS = (
    "example", "placeholder", "changeme", "dummy", "sample", "redacted",
    "your_", "xxxxx", "todo", "fixme", "fake",
)
_SECRET_CHAR_CLASSES = (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
#: 行级抑制注释，语义与多语言引擎的 ``// bugx-ignore`` / ``# nosec`` 保持一致。
_SUPPRESSION_RE = re.compile(r"(?:#|//)\s*(?:bugx-ignore(?::[^\n]*)?|nosec\b)", re.IGNORECASE)


def _shannon_entropy(value: str) -> float:
    """字符级 Shannon 熵（bits/char），用于区分随机密钥与可读占位串。"""
    if not value:
        return 0.0
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in collections.Counter(value).values()
    )


def _looks_like_hardcoded_secret(value: str) -> bool:
    """判断字面量本身是否具备「硬编码机密」的值特征（QUA-08）。

    仅按变量名（含 ``api_key``/``token``）判定会把中文提示文案、状态哨兵与
    测试假 token 全部报成 high，真实机密告警被误报淹没。这里要求值同时满足：
    长度 ≥ 16、纯 ASCII 无空白、非 SCREAMING_SNAKE_CASE 常量、非占位符，
    且字符级熵达标（两类字符时阈值更高）。
    """
    if len(value) < 16 or not value.isascii():
        return False
    if any(character.isspace() for character in value):
        return False
    if value.startswith(("http://", "https://", "ENV_")):
        return False
    if re.fullmatch(r"[A-Z0-9_]+", value):  # 常量名 / 状态哨兵，不是机密
        return False
    lowered = value.lower()
    if any(hint in lowered for hint in _SECRET_PLACEHOLDER_HINTS):
        return False
    classes = sum(1 for pattern in _SECRET_CHAR_CLASSES if re.search(pattern, value))
    if classes < 2:
        return False
    return _shannon_entropy(value) >= (3.5 if classes == 2 else 3.0)


class BuiltinAstScanner(ast.NodeVisitor):
    """基于 Python 标准库 AST 的确定性安全与风险反模式检测器（保持向前兼容）。"""

    def __init__(self, filepath: Path, rel_path: str, source_lines: list[str]) -> None:
        self.filepath = filepath
        self.rel_path = rel_path
        self.source_lines = source_lines
        self.findings: list[dict[str, Any]] = []
        self.dynamic_analyzer = DynamicFlowAnalyzer(language="python")
        self.var_env: dict[str, Any] = {}

    def _get_snippet(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()
        return ""

    def _is_suppressed(self, lineno: int) -> bool:
        """行级抑制：``# bugx-ignore`` / ``# nosec``（与多语言引擎语义一致）。"""
        return bool(_SUPPRESSION_RE.search(self._get_snippet(lineno)))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.var_env.update(self.dynamic_analyzer.fold_python_function_scope(node))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.var_env.update(self.dynamic_analyzer.fold_python_function_scope(node))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # os.system / os.popen 与 tempfile.mktemp，支持直接和 getattr 调用。
        mod = ""
        attr = ""
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            mod = node.func.value.id
            attr = node.func.attr
        elif (
            isinstance(node.func, ast.Call)
            and isinstance(node.func.func, ast.Name)
            and node.func.func.id == "getattr"
            and len(node.func.args) >= 2
        ):
            target, attr_value = node.func.args[:2]
            if isinstance(target, ast.Name):
                mod = target.id
            if isinstance(attr_value, ast.Constant) and isinstance(attr_value.value, str):
                attr = attr_value.value
            elif isinstance(attr_value, ast.Name) and attr_value.id in self.var_env:
                value = self.var_env[attr_value.id].value
                attr = value if isinstance(value, str) else ""
        if mod == "os" and attr in {"system", "popen"}:
            self.findings.append({
                "rule_id": "SEC-001-OS-SYSTEM",
                "name": "OS Command Injection via os.system",
                "category": "command-injection",
                "severity": "high",
                "confidence": "high",
                "cwe": "CWE-78",
                "message": f"调用 {mod}.{attr} 执行外部命令，存在命令注入风险",
                "file": self.rel_path,
                "line": node.lineno,
                "col": node.col_offset,
                "evidence": self._get_snippet(node.lineno),
                "remediation": "建议使用 subprocess.run 并传入数组形式的 argv，避免 shell 解析",
            })
        elif mod == "tempfile" and attr == "mktemp":
            self.findings.append({
                "rule_id": "SEC-007-INSECURE-TEMPFILE",
                "name": "Insecure Temporary File",
                "category": "race-condition",
                "severity": "medium",
                "confidence": "high",
                "cwe": "CWE-377",
                "message": "使用不安全的 tempfile.mktemp 创建临时文件，存在 TOCTOU 竞态漏洞",
                "file": self.rel_path,
                "line": node.lineno,
                "col": node.col_offset,
                "evidence": self._get_snippet(node.lineno),
                "remediation": "改用 tempfile.NamedTemporaryFile 或 tempfile.mkstemp",
            })

        # subprocess(..., shell=True)
        for kw in node.keywords:
            if kw.arg == "shell":
                is_true = False
                if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    is_true = True
                if is_true:
                    self.findings.append({
                        "rule_id": "SEC-001-SUBPROCESS-SHELL",
                        "name": "Subprocess shell=True Injection",
                        "category": "command-injection",
                        "severity": "high",
                        "confidence": "high",
                        "cwe": "CWE-78",
                        "message": "subprocess 执行携带 shell=True，可能导致未转义命令注入",
                        "file": self.rel_path,
                        "line": node.lineno,
                        "col": node.col_offset,
                        "evidence": self._get_snippet(node.lineno),
                        "remediation": "移除 shell=True，将命令拆分为参数列表传入",
                    })

        # SEC-002: 危险执行 eval / exec
        if func_name in {"eval", "exec"}:
            self.findings.append({
                "rule_id": "SEC-002-DANGEROUS-EVAL",
                "name": "Dangerous Dynamic Code Execution",
                "category": "code-injection",
                "severity": "critical",
                "confidence": "medium",
                "cwe": "CWE-95",
                "message": f"直接调用动态代码执行函数 {func_name}()",
                "file": self.rel_path,
                "line": node.lineno,
                "col": node.col_offset,
                "evidence": self._get_snippet(node.lineno),
                "remediation": "避免使用 eval/exec；如需解析字面量使用 ast.literal_eval",
            })

        # SEC-003: 不安全反序列化 pickle.loads / marshal.loads
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id in {"pickle", "_pickle", "cPickle", "marshal"} and node.func.attr in {"load", "loads"}:
                self.findings.append({
                    "rule_id": "SEC-003-INSECURE-DESERIALIZATION",
                    "name": "Insecure Deserialization",
                    "category": "deserialization",
                    "severity": "critical",
                    "confidence": "high",
                    "cwe": "CWE-502",
                    "message": f"使用未受信任的 {node.func.value.id}.{node.func.attr} 反序列化，可导致任意代码执行",
                    "file": self.rel_path,
                    "line": node.lineno,
                    "col": node.col_offset,
                    "evidence": self._get_snippet(node.lineno),
                    "remediation": "改用安全的结构化格式（如 json / protobuf / hmac 签名）",
                })

        # SEC-SSTI-001: Python SSTI (render_template_string, jinja2.Template)
        if func_name == "render_template_string" or (
            isinstance(node.func, ast.Attribute) and node.func.attr in {"render_template_string", "from_string"}
        ):
            is_vuln = True
            if node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    # 纯静态字面量视为安全
                    is_vuln = False
                elif isinstance(first_arg, ast.Name) and first_arg.id in self.var_env:
                    var_obj = self.var_env[first_arg.id]
                    if var_obj.is_constant:
                        is_vuln = False
            if is_vuln:
                self.findings.append({
                    "rule_id": "SEC-SSTI-001",
                    "name": "Server-Side Template Injection (SSTI)",
                    "category": "template-injection",
                    "severity": "critical",
                    "confidence": "high",
                    "cwe": "CWE-1336",
                    "message": "动态渲染外部可控的模版字符串，可能导致服务端模版注入与远程代码执行",
                    "file": self.rel_path,
                    "line": node.lineno,
                    "col": node.col_offset,
                    "evidence": self._get_snippet(node.lineno),
                    "remediation": "改用静态模版文件并传递参数字典（如 render_template），严禁直接动态编译外部字符串为模版。",
                })

        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            is_broad = node.type is None
            if isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}:
                is_broad = True
            if is_broad:
                self.findings.append({
                    "rule_id": "SEC-006-SILENT-EXCEPTION",
                    "name": "Silent Generic Exception Swallowing",
                    "category": "error-handling",
                    "severity": "low",
                    "confidence": "high",
                    "cwe": "CWE-391",
                    "message": "静默捕获并吞没通用异常（except: pass），掩盖运行时致命故障",
                    "file": self.rel_path,
                    "line": node.lineno,
                    "col": node.col_offset,
                    "evidence": self._get_snippet(node.lineno),
                    "remediation": "指定具体异常类型，并在 except 块中记录日志或妥善恢复",
                })
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        sensitive_kw = {"password", "secret", "api_key", "apikey", "token", "private_key"}
        for target in node.targets:
            target_name = ""
            if isinstance(target, ast.Name):
                target_name = target.id.lower()
            elif isinstance(target, ast.Attribute):
                target_name = target.attr.lower()

            if any(kw in target_name for kw in sensitive_kw):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    val = node.value.value
                    # QUA-08：变量名匹配只是必要条件；值本身还必须像机密。
                    if _looks_like_hardcoded_secret(val) and not self._is_suppressed(node.lineno):
                        self.findings.append({
                            "rule_id": "SEC-004-HARDCODED-SECRET",
                            "name": "Hardcoded Secret",
                            "category": "secrets",
                            "severity": "high",
                            "confidence": "medium",
                            "cwe": "CWE-798",
                            "message": f"疑似在变量 {target_name} 中硬编码静态机密凭证",
                            "file": self.rel_path,
                            "line": node.lineno,
                            "col": node.col_offset,
                            "evidence": self._get_snippet(node.lineno),
                            "remediation": "从安全环境变量或凭据保险箱动态获取，切勿写入代码",
                        })
        self.generic_visit(node)


def scan_source_ast(
    root: Path, max_files: int = 1000, stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """扫描 Python 源码（BuiltinAstScanner，保持向前兼容）。

    ``stats`` 若给出，会填入 ``files``（成功解析的文件数）。报告层需要这个
    数字才能说清覆盖范围：没有它，调用方只能拿「命中数」冒充「暴露面总量」。
    """
    findings: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        if scanned >= max_files:
            break
        if path.is_symlink() or not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        if _skipped(Path(rel)):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            source_lines = content.splitlines()
            tree = ast.parse(content, filename=str(path))
            scanner = BuiltinAstScanner(path, rel, source_lines)
            scanner.visit(tree)
            findings.extend(scanner.findings)
            scanned += 1
        except (SyntaxError, OSError):
            continue
    if stats is not None:
        stats["files"] = stats.get("files", 0) + scanned
    return findings


def scan_multilang_sources(
    root: Path, max_files: int = 2000, stats: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """使用 UniversalCodeMatcher 与 SASTRuleMatcher 对非 Python 及多语言源码执行扫描。"""
    matcher = SASTRuleMatcher(BUILTIN_RULES)
    findings: list[dict[str, Any]] = []
    scanned = 0

    # 重点扫描非 Python 多语言文件（Python 已由 BuiltinAstScanner 深度处理，亦可补充）
    for ext, lang in EXT_TO_LANG.items():
        if ext == ".py":
            continue
        for path in root.rglob(f"*{ext}"):
            if scanned >= max_files:
                break
            if path.is_symlink() or not path.is_file():
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = str(path)
            if _skipped(Path(rel)):
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                parsed = UniversalCodeMatcher.parse_file(path, rel_path=rel, content=content)
                file_findings = matcher.match_file(parsed)
                findings.extend(file_findings)
                scanned += 1
            except Exception:
                continue

    if stats is not None:
        stats["files"] = stats.get("files", 0) + scanned
    return findings


def scan_external_bandit(root: Path, timeout: float = 60.0) -> list[dict[str, Any]]:
    """调用外部 Native Bandit 对 Python 源码进行深度安全扫描。

    ``timeout`` 由调用方按**剩余总预算**下发（``run_scan(deadline_seconds=...)``），
    使外部工具无法把整轮扫描拖过调用方的墙钟上限。
    """
    environment = _probe_environment()
    exe = shutil.which("bandit", path=environment.get("PATH"))
    if not exe:
        return []
    try:
        cmd = [exe, "-r", str(root), "-f", "json", "-q", "-x", ",".join(SKIP_DIRS)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             check=False, env=environment)
        if res.returncode not in (0, 1):
            return []
        if not res.stdout.strip():
            return []
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            return []
        results = []
        for item in data.get("results", []):
            path_str = item.get("filename", "")
            try:
                rel = Path(path_str).relative_to(root).as_posix()
            except Exception:
                rel = path_str
            cwe_id = "CWE-General"
            issue_cwe = item.get("issue_cwe")
            if isinstance(issue_cwe, dict) and issue_cwe.get("id"):
                cwe_id = f"CWE-{issue_cwe.get('id')}"
            results.append({
                "rule_id": item.get("test_id", "bandit-rule"),
                "name": item.get("test_name", "Bandit Finding"),
                "category": "sast-bandit",
                "severity": str(item.get("issue_severity", "medium")).lower(),
                "confidence": str(item.get("issue_confidence", "medium")).lower(),
                "cwe": cwe_id,
                "message": item.get("issue_text", "Bandit 安全规则触发"),
                "file": rel,
                "line": item.get("line_number", 1),
                "col": item.get("col_offset", 1),
                "evidence": str(item.get("code", "")).strip(),
                "remediation": f"参考 Bandit 规则文档修复: {item.get('more_info', '')}",
            })
        return results
    except Exception:
        return []


def scan_external_semgrep(root: Path, timeout: float = 60.0) -> list[dict[str, Any]]:
    """调用外部 Semgrep 做深度规则扫描；``timeout`` 由调用方的剩余预算下发。"""
    environment = _probe_environment()
    exe = shutil.which("semgrep", path=environment.get("PATH"))
    if not exe:
        return []
    try:
        cmd = [exe, "scan", "--json", "--quiet", "--config", "p/security-audit", str(root)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             check=False, env=environment)
        if res.returncode not in (0, 1):
            return []
        data = json.loads(res.stdout)
        results = []
        for item in data.get("results", []):
            path_str = item.get("path", "")
            try:
                rel = Path(path_str).relative_to(root).as_posix()
            except Exception:
                rel = path_str
            start = item.get("start", {})
            extra = item.get("extra", {})
            results.append({
                "rule_id": item.get("check_id", "semgrep-rule"),
                "name": item.get("check_id", "Semgrep Finding"),
                "category": "sast-semgrep",
                "severity": extra.get("severity", "medium").lower(),
                "confidence": "high",
                "cwe": (extra.get("metadata", {}).get("cwe") or ["CWE-General"])[0],
                "message": extra.get("message", "Semgrep 规则触发"),
                "file": rel,
                "line": start.get("line", 1),
                "col": start.get("col", 1),
                "evidence": extra.get("lines", "").strip(),
                "remediation": "根据 Semgrep 安全最佳实践修复代码逻辑",
            })
        return results
    except Exception:
        return []


def compute_finding_fingerprint(finding: dict[str, Any]) -> str:
    """计算用于基线快照匹配的确定性指纹"""
    raw = f"{finding.get('rule_id')}:{finding.get('file')}:{finding.get('evidence', '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def apply_baseline_filter(findings: list[dict[str, Any]], baseline_path: Path) -> tuple[list[dict[str, Any]], int]:
    """过滤掉已存在于基线快照中的历史 Finding"""
    if not baseline_path.exists():
        return findings, 0
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_hashes = set(data.get("fingerprints", []))
    except Exception:
        return findings, 0

    filtered = []
    suppressed_count = 0
    for item in findings:
        fp = compute_finding_fingerprint(item)
        if fp in baseline_hashes:
            suppressed_count += 1
        else:
            filtered.append(item)
    return filtered, suppressed_count


def save_baseline(findings: list[dict[str, Any]], baseline_path: Path) -> None:
    fingerprints = sorted(list({compute_finding_fingerprint(f) for f in findings}))
    payload = {
        "version": 1,
        "total": len(fingerprints),
        "fingerprints": fingerprints,
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def export_to_sarif(scan_res: dict[str, Any]) -> dict[str, Any]:
    """将扫描结果导出为标准 OASIS SARIF v2.1.0 格式"""
    results = []
    rules_map: dict[str, dict[str, Any]] = {}

    for f in scan_res.get("findings", []):
        rule_id = f.get("rule_id", "BUGX-RULE")
        if rule_id not in rules_map:
            rules_map[rule_id] = {
                "id": rule_id,
                "name": f.get("name", rule_id),
                "shortDescription": {"text": f.get("name", rule_id)},
                "fullDescription": {"text": f.get("message", "")},
                "help": {"text": f.get("remediation", f.get("recommendation", ""))},
                "properties": {
                    "cwe": f.get("cwe", "CWE-Unknown"),
                    "category": f.get("category", "security"),
                }
            }

        level = "warning"
        sev = f.get("severity", "medium").lower()
        if sev in {"critical", "high"}:
            level = "error"
        elif sev == "low":
            level = "note"

        results.append({
            "ruleId": rule_id,
            "level": level,
            "message": {"text": f.get("message", "")},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": f.get("file", "")},
                    "region": {
                        "startLine": f.get("line", 1),
                        "startColumn": f.get("col", 1) + 1,
                        "snippet": {"text": f.get("evidence", "")}
                    }
                }
            }]
        })

    return {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "BugX-SAST",
                    "informationUri": "https://bugx.dev",
                    "rules": list(rules_map.values()),
                }
            },
            "results": results,
        }]
    }


def _cwe_head(value: Any) -> str:
    """取 CWE 编号本体：semgrep 会给出 ``CWE-78: Improper Neutralization...``。"""
    return str(value or "").strip().upper().split(":", 1)[0].strip()


def _dedupe_cross_tool(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并不同扫描器对同一处缺陷的重复命中（同文件 + 同 CWE + 行号相近）。"""
    kept: list[dict[str, Any]] = []
    seen: list[tuple[str, str, str, int]] = []
    for item in findings:
        path = str(item.get("file") or "")
        cwe = _cwe_head(item.get("cwe"))
        rule = str(item.get("rule_id") or "")
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        duplicate = False
        if path and cwe:
            for seen_path, seen_cwe, seen_rule, seen_line in seen:
                # 只有**不同规则**命中同一处才算跨工具重复：同一条规则在相邻
                # 两行命中是两处真实缺陷（实测 src/config.py:5 与 :6 是两个
                # 独立的硬编码凭据），不能被合并掉。
                if (seen_path == path and seen_cwe == cwe
                        and seen_rule != rule and abs(seen_line - line) <= 3):
                    duplicate = True
                    break
        if duplicate:
            continue
        seen.append((path, cwe, rule, line))
        kept.append(item)
    return kept


def run_scan(
    root: Path,
    tools: str = "auto",
    baseline: Path | None = None,
    write_baseline: Path | None = None,
    include_sca: bool = True,
    deadline_seconds: float | None = None,
) -> dict[str, Any]:
    """执行一次 SAST 扫描。

    ``deadline_seconds`` 给出**整轮扫描的总墙钟预算**（None = 不限）。预算只约束
    「是否还允许启动下一个外部工具」以及每个外部子进程的 ``timeout``：内建
    AST/multilang/SCA 阶段是纯进程内计算，不设预算；外部 semgrep/bandit 一旦
    剩余预算不足（<=0.5s）就跳过并标记 ``status=DEGRADED`` +
    ``budget_exhausted=True``，绝不静默返回 ``COMPLETED`` 冒充完整扫描。
    """
    root = root.resolve()
    findings: list[dict[str, Any]] = []
    file_stats: dict[str, int] = {}
    used_tools: list[str] = []
    started_at = time.monotonic()
    deadline = (
        None if deadline_seconds is None else started_at + max(0.0, float(deadline_seconds))
    )
    budget_exhausted = False

    def _remaining() -> float | None:
        if deadline is None:
            return None
        return deadline - time.monotonic()

    # 1. 执行 Python 原生 AST 扫描 (保持向前兼容)
    builtin_py_results = scan_source_ast(root, stats=file_stats)
    findings.extend(builtin_py_results)
    used_tools.append("builtin-ast")

    # 2. 执行多语言通用结构化模式扫描 (Go, Java, JS/TS, Rust, C/C++, PHP 等)
    multilang_results = scan_multilang_sources(root, stats=file_stats)
    if multilang_results:
        findings.extend(multilang_results)
        used_tools.append("builtin-multilang-sast")

    # 3. 执行多语言依赖供应链分析 (SCA)
    if include_sca:
        sca = SCAScanner()
        sca_results = sca.scan_project(root)
        if sca_results:
            findings.extend(sca_results)
            used_tools.append("builtin-sca")

    # 4. 外部 Native 工具加速通道（若存在且被允许）
    allow_external = os.environ.get("BUGX_DISABLE_EXTERNAL_SAST") != "1"
    if allow_external and tools in {"auto", "semgrep"} and shutil.which(
        "semgrep", path=_probe_environment().get("PATH")
    ):
        remaining = _remaining()
        if remaining is not None and remaining <= 0.5:
            budget_exhausted = True
        else:
            semgrep_results = scan_external_semgrep(
                root, timeout=60.0 if remaining is None else min(60.0, remaining),
            )
            if semgrep_results:
                findings.extend(semgrep_results)
                used_tools.append("semgrep")

    if allow_external and tools in {"auto", "bandit"} and shutil.which(
        "bandit", path=_probe_environment().get("PATH")
    ):
        remaining = _remaining()
        if remaining is not None and remaining <= 0.5:
            budget_exhausted = True
        else:
            bandit_results = scan_external_bandit(
                root, timeout=60.0 if remaining is None else min(60.0, remaining),
            )
            if bandit_results:
                findings.extend(bandit_results)
                used_tools.append("bandit")

    # 跨工具去重：内建 AST 与 semgrep/bandit 会独立命中同一处缺陷（实测
    # ``eval`` 在 src/evals.py:11 被内建与 semgrep 各报一次，``shell=True``
    # 被内建报在 11 行、semgrep 报在 12 行）。不去重时报告会把一个漏洞
    # 计入两条发现、双倍抬升严重度计数。判据是「同一文件 + 同一 CWE +
    # 行号相近（<=3 行）」，保留首个（内建优先，字段最全）。
    findings = _dedupe_cross_tool(findings)

    # 排序：按严重度及路径排序
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda x: (severity_order.get(x.get("severity", "medium"), 2), x.get("file", ""), x.get("line", 0)))

    # 保存基线
    if write_baseline:
        save_baseline(findings, write_baseline)

    # 过滤基线
    suppressed_baseline = 0
    if baseline:
        findings, suppressed_baseline = apply_baseline_filter(findings, baseline)

    return {
        "schema_version": 2,
        "status": "DEGRADED" if budget_exhausted else "COMPLETED",
        "root": str(root),
        # 报告层需要「扫了多少文件」才能说清楚覆盖范围。此前收据不报这个数，
        # 调用方只能用命中数顶替，把「命中数」当成「暴露面总量」呈现。
        "scanned_files": int(file_stats.get("files", 0)),
        "tools_executed": used_tools,
        "total_findings": len(findings),
        "suppressed_by_baseline": suppressed_baseline,
        "budget_exhausted": budget_exhausted,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status", "scan"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--sarif", action="store_true", help="输出 OASIS SARIF v2.1.0 格式")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--allow-degraded", action="store_true",
        help="明确允许使用回退方案；否则 DEGRADED 返回 2，要求调用方先取得用户决策",
    )
    parser.add_argument("--tools", choices=("auto", "builtin", "semgrep"), default="auto", help="指定扫描使用的工具")
    parser.add_argument("--baseline", type=Path, default=None, help="从已有基线快照过滤已知历史问题")
    parser.add_argument("--write-baseline", type=Path, default=None, help="将当前扫描结果持久化为基线快照")
    args = parser.parse_args(argv)

    if args.command == "scan":
        scan_res = run_scan(
            args.root,
            tools=args.tools,
            baseline=args.baseline,
            write_baseline=args.write_baseline,
        )
        if args.sarif:
            sarif_doc = export_to_sarif(scan_res)
            print(json.dumps(sarif_doc, ensure_ascii=False, indent=2))
        elif args.json:
            print(json.dumps(scan_res, ensure_ascii=False, indent=2))
        else:
            print(f"[SAST Scan] 执行完成（工具链: {', '.join(scan_res['tools_executed'])}），发现 {scan_res['total_findings']} 个候选漏洞：")
            if scan_res.get("suppressed_by_baseline", 0) > 0:
                print(f"  ℹ️ 已基于 Baseline 过滤 {scan_res['suppressed_by_baseline']} 个已知历史问题")
            for idx, item in enumerate(scan_res["findings"][:25], 1):
                cwe_tag = f"[{item.get('cwe', '')}] " if item.get('cwe') else ""
                print(f"  #{idx} [{item['severity'].upper()}] {item['file']}:{item['line']} - {cwe_tag}{item['rule_id']}: {item['message']}")
            if scan_res['total_findings'] > 25:
                print(f"  ... 以及另外 {scan_res['total_findings'] - 25} 项（使用 --json 或 --sarif 查看全量）")
        return 0

    report = inspect(args.root)
    print_report(report, args.json)
    if report["status"] == "READY":
        return 0
    if not args.json and not args.allow_degraded and not args.strict:
        print("  ⏸ 当前仅完成探测，等待用户明确允许回退或配置工具；不会自动进入下一步。")
    if args.strict:
        return 1
    return 0 if args.allow_degraded else 2


if __name__ == "__main__":
    raise SystemExit(main())
