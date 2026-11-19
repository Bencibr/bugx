#!/usr/bin/env python3
"""differential_oracle.py — 确定性沙盒隔离双跑差分验证器（DESIGN-A1）。

用途：
  消除由于本地残留文件、端口占用、时钟/随机数抖动或环境污染造成的假阳性与 Flaky 幽灵用例。
  所有触发异常的候选 Finding / PoC 在两个完全独立的临时沙盒环境中连续执行 2 次：
  1. 归一化清洗：剥离内存地址（0x...）、时间戳、临时路径、PID 等非确定性噪音；
  2. 双跑差分裁决：
     - CONFIRMED_DETERMINISTIC: 两次独立双跑一致崩溃且堆栈签名对齐，确认为真实确定性 Bug；
     - FLAKY_JITTER: 退出码或异常特征不一致，判定为偶发抖动用例，拦截防污染错题集；
     - FALSE_POSITIVE: 两次双跑均正常退出（exit 0），判定为无法复现的环境假阳性；
  3. 契约工件输出：生成符合 finding_confirmation.py 规范的完整证据链工件与 SHA-256 指纹。

CLI 用法：
  python3 differential_oracle.py verify \
      --cmd 'python3 target.py {input}' \
      --input /path/to/poc.json \
      [--workspace /path/to/project] [--timeout 10] [--json] [--out report.json]

沙盒投递契约（DEF-1）：提供 --workspace（或库调用传 workspace_root/allowed_root）时，
沙盒执行前会把 PoC 文本与命令模板中实际引用的文件/目录按相对布局复制进沙盒
（有文件数/字节硬预算）。引用来源有三类且统一经越界/存在性裁决：
① 引号字面量（runpy.run_path("app/admin.py")）；② import 子句（from app import files）；
③ **命令模板的裸 argv token**（python3 target.py {input} 中的 target.py）。
对引用到的单个文件同时捕获其父目录子树（跳过 .git/__pycache__ 等噪声目录），
保证被测目标源码自身的同级依赖在沙盒内可达；PoC 字节本身不改写。
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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from core.supervisor.worker_execution import run_bounded
from core.fuzzing.fuzz_input import split_argv as _split_argv_shared

SCHEMA_VERSION = 1


class DifferentialOracleError(ValueError):
    """差分验证器异常。"""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    duration_ms: float
    stdout: str
    stderr: str
    normalized_signature: str
    timed_out: bool = False
    infrastructure_error: bool = False
    infrastructure_reason: str = ""
    # DEF-1: 沙盒内被测目标源码投递收据（仅 workspace_root 提供时非空）。
    provisioning: dict[str, Any] | None = None


_INFRASTRUCTURE_PATTERNS = (
    re.compile(r"\b(?:no such file or directory|enoent)\b", re.IGNORECASE),
    re.compile(r"\b(?:command not found|executable file not found)\b", re.IGNORECASE),
    re.compile(r"\b(?:permission denied|eacces|eperm)\b", re.IGNORECASE),
)


def _infrastructure_failure_reason(
    *,
    exit_code: int,
    stderr: str,
    stdout: str,
    launch_error: str | None = None,
) -> str | None:
    """Classify launch/environment failures before applying the dual-run rule.

    The oracle must not turn a stable inability to launch/read the requested
    command or target into a confirmed defect.  This classifier is deliberately
    conservative: an ambiguous infrastructure-looking message is downgraded
    to ``INFRA_ERROR`` and retained in the report for human review.
    """
    if launch_error:
        return launch_error[:400]
    combined = "\n".join(part for part in (stderr, stdout) if part)
    for pattern in _INFRASTRUCTURE_PATTERNS:
        match = pattern.search(combined)
        if match:
            return match.group(0)
    if exit_code in {126, 127}:
        return f"exit {exit_code} indicates command launch failure"
    return None


def _temporary_input(content: str, *, suffix: str = ".tmp") -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", delete=False, encoding="utf-8", suffix=suffix,
    )
    try:
        handle.write(content)
        handle.flush()
    finally:
        handle.close()
    return Path(handle.name)


# --- DEF-1 修复：沙盒内被测目标源码投递 -------------------------------------
# 沙盒 cwd 是全新临时目录，PoC 模板引用的 workspace 相对路径（如
# runpy.run_path("app/admin.py")）此前必然 FileNotFoundError → 全部 INFRA_ERROR，
# deterministic dual-run 确认链路被结构性切断。修复方式是按 PoC/命令模板中
# 实际引用（引号字面量）且真实存在于 workspace 根内的文件/目录，按原相对
# 布局复制进沙盒——PoC 字节本身不改写（保住 input_sha256 证据链），只复制
# 引用到的路径，绝不镜像全仓（沙盒隔离保持），双跑共享同一份投递计划以
# 维持确定性裁决语义。
_QUOTED_LITERAL_RE = re.compile(r"['\"]([^'\"\n]{1,1024})['\"]")
# DEF-1 缺口1：import 型 PoC（``from app import files`` / ``import app.admin``）
# 没有任何引号路径字面量，只靠引号提取会得到空投递计划 → 沙箱缺被测包 →
# 双跑同指纹 ModuleNotFoundError。除引号字面量外，把 import 语句中的点分
# 模块路径也提取为投递候选（仍经 _safe_workspace_member 真实存在 + 越界过滤）。
# 锚点覆盖三种 import 出现边界：行首（多行 -c 程序体）、分号后（一行式）、
# 引号后（``python3 -c "from x.y import z"`` 单引号包裹的一行式程序体——
# 引号字面量通道会把整段程序体当一个含空格 token 吃掉且无法解析为路径，
# 2026-11-18 dsh 第三批端到端实证该形态仍漏提取 → 沙箱 ModuleNotFoundError）。
_IMPORT_CLAUSE_RE = re.compile(
    r"(?:^[ \t]*|[;\"'][ \t]*)(?:from[ \t]+([.\w]+)[ \t]+import\b|import[ \t]+([.\w]+(?:[ \t]*,[ \t]*[.\w]+)*))",
    re.MULTILINE,
)
_DOTTED_MODULE_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
# DEF-1 缺口2：模块缺失/导入失败类错误是沙箱执行环境问题（投递计划缺包、
# 解释器环境不完整），不是目标缺陷证据。双跑同指纹命中时不得升级为
# CONFIRMED_DETERMINISTIC（现有 _INFRASTRUCTURE_PATTERNS 不覆盖该形态）。
_STABLE_INFRA_SIGNATURE_RE = re.compile(
    r"\bModuleNotFoundError\b|\bNo module named\b", re.IGNORECASE
)
_SANDBOX_PROVISION_MAX_FILES = 400
_SANDBOX_PROVISION_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_SANDBOX_PROVISION_SCAN_BYTES = 1024 * 1024
_SANDBOX_PROVISION_REPORT_LIMIT = 50
# 目录子树捕获时跳过的噪声/巨型目录（VCS、缓存、依赖与引擎状态）。
_PROVISION_SKIP_DIR_NAMES = frozenset({
    ".git", "__pycache__", ".bugx", ".venv", "venv", "node_modules",
})
# plan 条目: (token, 引用路径, kind)；kind: "file" 精确引用文件 / "dir" 引用目录
# 或引用文件的父目录捕获（覆盖目标源码自身未被 PoC 引用的同级依赖）。
_WorkspaceProvisionPlan = tuple[Path, list[tuple[str, Path, str]]]


def _workspace_reference_tokens(text: str) -> list[str]:
    """提取文本中去重后的 workspace 相对路径候选。

    DEF-1 缺口1 扩展：除引号字面量外，同时提取 import 语句中的点分模块
    路径（``from <pkg> import ...`` 的 ``<pkg>``、``import <pkg>.<mod>[, ...]``
    的逐个模块名）。候选是否真实存在、是否越界仍由 ``_safe_workspace_member``
    统一裁决；``runpy.run_path(...)`` 等带引号的调用形态已由引号字面量覆盖。
    """
    if not text:
        return []
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        token = (raw or "").strip()
        if not token or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for match in _QUOTED_LITERAL_RE.finditer(text):
        _add(match.group(1))
    for match in _IMPORT_CLAUSE_RE.finditer(text):
        _add(match.group(1))
        for piece in (match.group(2) or "").split(","):
            _add(piece)
    return tokens


def _command_argv_reference_tokens(cmd_template: str) -> list[str]:
    """提取命令模板 argv 中的裸路径候选（DEF-1 缺口3，根治）。

    文档自带 CLI 示例 ``--cmd 'python3 target.py {input}'`` 的目标脚本是裸 argv
    token——既不在引号里也不是 import 子句，旧投递源（引号+import）提取为空，
    沙盒缺少被测目标 → 双跑同指纹 FileNotFoundError → INFRA_ERROR，确定性确认
    链路被结构性切断。本函数用仓库唯一 argv 解析器拆分命令模板，跳过解释器/运行
    时标志与占位符参数，把其余 token 作为路径候选交给 ``_safe_workspace_member``
    统一裁决（真实存在 + workspace 根内 + 非逃逸）。

    只收录"疑似路径形态"的 token（含路径分隔符或常见脚本/源码扩展名），避免把
    普通参数值（如 ``--level 3`` 的 ``3``）当候选误投；即便误收也会被
    ``_safe_workspace_member`` 的存在性检查过滤。模板解析失败时返回空列表，不在
    投递阶段抛错——命令合法性由执行阶段 ``_split_argv`` 统一负责。
    """
    if not cmd_template:
        return []
    try:
        argv = _split_argv(cmd_template)
    except DifferentialOracleError:
        return []
    path_like = re.compile(
        r"(?:/|\\|\.(?:py|js|mjs|cjs|ts|tsx|sh|bash|rb|pl|php|java|go|rs|c|cpp|exe)$)",
        re.IGNORECASE,
    )
    tokens: list[str] = []
    for token in argv:
        if not token or "{" in token or "}" in token:
            continue
        if token.startswith("-"):
            continue
        if not path_like.search(token):
            continue
        tokens.append(token)
    return tokens


def _dotted_module_candidates(token: str) -> list[str]:
    """把 import 型点分模块路径展开为候选 workspace 相对路径。

    ``app.admin`` 依次尝试 ``app/admin.py``、``app/admin/__init__.py``、
    ``app/admin`` 目录，最后回退顶层包 ``app``——引用文件的父目录子树捕获
    会把同级依赖一并投递，任一候选真实存在即可保证被测包在沙箱内可达。
    非点分模块形状（含 ``/``、引号残留等）的 token 不展开，保持引号型
    回归行为不变。
    """
    if not _DOTTED_MODULE_RE.fullmatch(token):
        return []
    rel = token.replace(".", "/")
    candidates = [f"{rel}.py", f"{rel}/__init__.py", rel]
    top = token.split(".", 1)[0]
    if top != token:
        candidates.append(top)
    return candidates


def _safe_workspace_member(workspace_root: Path, token: str) -> Path | None:
    """把引号字面量解析为严格位于 workspace 根内的真实文件/目录。

    拒绝绝对路径、``..`` 越界与符号链接逃逸；返回未解析的引用路径以便
    沙盒内按 PoC 所见布局落盘（内容复制仍经解析后的真实文件）。
    """
    if not token or "\x00" in token or len(token) > 512:
        return None
    try:
        candidate = Path(token)
    except (OSError, ValueError):
        return None
    if candidate.is_absolute() or not candidate.parts:
        return None
    if any(part == ".." for part in candidate.parts):
        return None
    target = workspace_root / candidate
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(workspace_root)
    except (OSError, ValueError):
        return None
    if resolved == workspace_root:
        return None
    if not (resolved.is_file() or resolved.is_dir()):
        return None
    return target


def _plan_workspace_provisioning(
    texts: list[str],
    workspace_root: Path | None,
    *,
    cmd_template: str | None = None,
) -> _WorkspaceProvisionPlan | None:
    """构建沙盒投递计划：PoC/命令模板引用且存在于 workspace 根内的路径集合。

    三类引用源统一进 ``seen_tokens`` 去重后由 ``_safe_workspace_member`` 裁决：
    ① texts 中的引号字面量 + import 子句（``_workspace_reference_tokens``）；
    ② ``cmd_template`` 的裸 argv 路径 token（DEF-1 缺口3；runpy 形态的 CLI 示例）。

    对引用到的单个文件，同时捕获其父目录子树（有界预算 + 噪声目录跳过）：
    被测目标源码自身的同级依赖（admin.py 读 payload.txt、包内 import 兄弟
    模块）随父目录一并投递，多文件目标在沙盒内可达。``workspace_root`` 为
    None 时返回 None（保持旧的 workspace-blind 行为，调用方零破坏）。
    """
    if workspace_root is None:
        return None
    root = Path(workspace_root).expanduser().resolve()
    members: list[tuple[str, Path, str]] = []
    seen_tokens: set[str] = set()
    seen_targets: set[Path] = set()
    sources: list[tuple[str, bool]] = [(text, False) for text in texts]
    if cmd_template:
        sources.append((cmd_template, True))
    for text, is_command in sources:
        if not text:
            continue
        scan = text[:_SANDBOX_PROVISION_SCAN_BYTES]
        tokens = _workspace_reference_tokens(scan)
        if is_command:
            tokens = tokens + _command_argv_reference_tokens(scan)
        for token in tokens:
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            member = _safe_workspace_member(root, token)
            if member is None:
                # DEF-1 缺口1：import 型点分模块路径（如 app / app.admin）按
                # 候选相对路径逐个解析，仍走同一 _safe_workspace_member 过滤。
                for candidate in _dotted_module_candidates(token):
                    member = _safe_workspace_member(root, candidate)
                    if member is not None:
                        break
            if member is None:
                continue
            resolved_key = member.resolve(strict=False)
            if resolved_key in seen_targets:
                continue
            seen_targets.add(resolved_key)
            if member.is_dir():
                members.append((token, member, "dir"))
                continue
            members.append((token, member, "file"))
            # 父目录捕获：引用文件的同级依赖一并投递（引用根级文件时父目录
            # 即 workspace 根，受文件数/字节硬预算约束并记录截断收据）。
            capture_dir = member.parent
            resolved_capture = capture_dir.resolve(strict=False)
            if resolved_capture not in seen_targets:
                seen_targets.add(resolved_capture)
                members.append((f"{token}::parent", capture_dir, "dir"))
    return root, members


def _apply_sandbox_provisioning(
    sandbox_path: Path,
    provision_plan: _WorkspaceProvisionPlan,
    reserved_path: Path | None = None,
) -> dict[str, Any]:
    """把投递计划中的 workspace 文件/目录按相对布局复制进沙盒（有界预算）。

    ``reserved_path``（沙盒内的 {input} 文件）不可被投递覆盖，否则会在
    内容分歧时破坏被裁决的 PoC 证据。
    """
    root, members = provision_plan
    file_count = 0
    total_bytes = 0
    truncated = False
    skipped = 0
    copied_files: list[str] = []
    copied_dirs: list[str] = []
    copied_dests: set[Path] = set()

    def _over_budget(size: int) -> bool:
        return (
            file_count >= _SANDBOX_PROVISION_MAX_FILES
            or total_bytes + size > _SANDBOX_PROVISION_MAX_TOTAL_BYTES
        )

    for _token, member, kind in members:
        if truncated:
            break
        rel = member.relative_to(root)
        if kind == "dir" or member.is_dir():
            dest_dir = sandbox_path / rel
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                skipped += 1
                continue
            copied_dirs.append(rel.as_posix())
            for entry in sorted(
                p for p in member.rglob("*")
                if not any(part in _PROVISION_SKIP_DIR_NAMES for part in p.parts)
            ):
                try:
                    if not entry.is_file():
                        continue
                    resolved_entry = entry.resolve(strict=False)
                    resolved_entry.relative_to(root)
                    size = entry.stat().st_size
                except (OSError, ValueError):
                    skipped += 1
                    continue
                dest_file = sandbox_path / entry.relative_to(root)
                if dest_file in copied_dests:
                    continue
                if reserved_path is not None and dest_file == reserved_path:
                    skipped += 1
                    continue
                if _over_budget(size):
                    truncated = True
                    break
                try:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry, dest_file)
                except OSError:
                    skipped += 1
                    continue
                copied_dests.add(dest_file)
                file_count += 1
                total_bytes += size
                if len(copied_files) < _SANDBOX_PROVISION_REPORT_LIMIT:
                    copied_files.append(entry.relative_to(root).as_posix())
        elif member.is_file():
            dest_file = sandbox_path / rel
            if dest_file in copied_dests:
                continue
            if reserved_path is not None and dest_file == reserved_path:
                skipped += 1
                continue
            try:
                size = member.stat().st_size
            except OSError:
                skipped += 1
                continue
            if _over_budget(size):
                truncated = True
                continue
            try:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(member, dest_file)
            except OSError:
                skipped += 1
                continue
            copied_dests.add(dest_file)
            file_count += 1
            total_bytes += size
            copied_files.append(rel.as_posix())
    return {
        "copied_files": copied_files,
        "copied_dirs": copied_dirs[:_SANDBOX_PROVISION_REPORT_LIMIT],
        "file_count": file_count,
        "total_bytes": total_bytes,
        "truncated": truncated,
        "skipped": skipped,
    }


def _materialize_poc_input(
    poc_input: Any,
    *,
    allow_raw_string: bool = True,
    base_dir: Path | None = None,
) -> tuple[Path, Path | None]:
    """Resolve a path or materialize textual/JSON PoC input.

    Worker findings are allowed to carry a structured JSON value.  Converting
    that value with ``str()`` would produce a Python representation which is
    not the JSON contract expected by the target command, so mappings/sequences
    are serialized explicitly.
    """
    if isinstance(poc_input, (str, os.PathLike)):
        try:
            candidate = Path(poc_input)
        except TypeError as exc:
            raise DifferentialOracleError("PoC 输入路径类型无效") from exc
        if base_dir is not None and not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            is_file = candidate.is_file()
        except OSError:
            # A long or otherwise invalid textual payload is still valid raw
            # input; only a PathLike value must resolve to a real file.
            is_file = False
        if is_file:
            return candidate, None
        if not isinstance(poc_input, str) or not allow_raw_string:
            raise DifferentialOracleError(f"输入 PoC 文件不存在: {candidate}")
        temporary = _temporary_input(poc_input)
        return temporary, temporary

    if isinstance(poc_input, (Mapping, list, tuple)):
        try:
            serialized = json.dumps(
                poc_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DifferentialOracleError(f"结构化 PoC 输入无法序列化为 JSON: {exc}") from exc
        temporary = _temporary_input(serialized, suffix=".json")
        return temporary, temporary

    raise DifferentialOracleError(
        "PoC 输入必须是文件路径、字符串或 JSON 对象/数组"
    )


def normalize_output(text: str) -> str:
    """归一化清洗输出：消除内存地址、时间戳、临时目录路径、PID 等非确定性干扰。"""
    if not text:
        return ""

    cleaned = text

    # 1. 优先清洗 Python 异常堆栈中的行号微小偏移或文件路径尾缀，保留文件名
    cleaned = re.sub(r'File ".*?[/\\]([^/\\]+\.py)"', r'File "\1"', cleaned)

    # 2. 清洗十六进制内存地址 (如 0x7ffee1234567)
    cleaned = re.sub(r"0x[0-9a-fA-F]{4,16}", "0x<ADDR>", cleaned)

    # 3. 清洗绝对临时目录 (如 /tmp/..., /var/folders/..., C:\Users\...\Temp\...)
    cleaned = re.sub(r"/(?:private/)?(?:var|tmp)/[a-zA-Z0-9_\-./]+", "<TEMP_DIR>", cleaned)
    cleaned = re.sub(r"[A-Za-z]:\\[^ \r\n\t:]+", "<TEMP_DIR>", cleaned)

    # 4. 清洗 ISO / 标准时间戳 (如 2026-09-06T07:15:10, 2026-09-06 07:15:10)
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", "<TIMESTAMP>", cleaned)

    # 5. 统一换行与空行收拢
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines)


def extract_crash_signature(exit_code: int, stderr: str, stdout: str) -> str:
    """提取崩溃签名：捕获异常类型、致命信号或关键退出信息。"""
    normalized_err = normalize_output(stderr)
    normalized_out = normalize_output(stdout)

    # 匹配经典 Python / Java / Rust / C 异常信息
    exception_patterns = [
        r"([A-Za-z_]+Error: [^\n]+)",
        r"(Exception: [^\n]+)",
        r"(panicked at '[^']+')",
        r"(panic: [^\n]+)",
        r"(fatal error: [^\n]+)",
        r"(Segmentation fault[^\n]*)",
        r"(Aborted[^\n]*)",
    ]
    for pat in exception_patterns:
        match = re.search(pat, normalized_err) or re.search(pat, normalized_out)
        if match:
            return match.group(1).strip()

    # 兜底：若有 stderr 取最后一行，否则取非零退出码签名
    if normalized_err:
        last_line = normalized_err.splitlines()[-1].strip()
        return f"STDERR: {last_line[:120]}"
    return f"EXIT_CODE_{exit_code}"


def _split_argv(template: str) -> list[str]:
    """复用全仓 argv 解析器，仅适配本模块的异常契约。"""
    return _split_argv_shared(
        template,
        error_type=DifferentialOracleError,
        parse_error_prefix="命令模板无法解析",
        empty_message="命令模板不能为空",
        operator_message="命令包含 shell 运算符；为确保纯净沙盒隔离，请传纯 argv 格式",
    )


def _execute_in_sandbox(
    cmd_template: str,
    input_content: bytes,
    input_filename: str,
    timeout: float = 10.0,
    accepted_exits: set[int] | None = None,
    provision_plan: _WorkspaceProvisionPlan | None = None,
) -> RunResult:
    """在全新的独立临时目录沙盒中执行单次目标命令。"""
    with tempfile.TemporaryDirectory(prefix="bugx-oracle-sandbox-") as sandbox_dir:
        sandbox_path = Path(sandbox_dir)
        sandbox_input = sandbox_path / input_filename
        sandbox_input.write_bytes(input_content)

        # DEF-1：执行前把 PoC 引用的被测目标源码投递进沙盒，否则引用
        # workspace 相对路径的 PoC 必然 FileNotFoundError → 全 INFRA_ERROR。
        provisioning_summary: dict[str, Any] | None = None
        if provision_plan is not None:
            provisioning_summary = _apply_sandbox_provisioning(
                sandbox_path, provision_plan, reserved_path=sandbox_input,
            )

        argv = _split_argv(cmd_template)
        rendered_argv = [
            arg.replace("{input}", str(sandbox_input)) for arg in argv
        ]

        t0 = time.perf_counter()
        launch_error: str | None = None
        try:
            completed = run_bounded(
                rendered_argv,
                cwd=sandbox_path,
                timeout=timeout,
                max_output_bytes=64 * 1024,
            )
            exit_code = completed.returncode
            timed_out = getattr(completed, "timed_out", False)
            stdout_str = completed.stdout or ""
            stderr_str = completed.stderr or ""
        except subprocess.TimeoutExpired:
            exit_code = 124
            timed_out = True
            stdout_str = ""
            stderr_str = "Execution timed out"
        except OSError as exc:
            # A command that cannot be launched is an oracle/infrastructure
            # failure, not evidence about the target project.
            exit_code = 126 if isinstance(exc, PermissionError) else 127
            timed_out = False
            stdout_str = ""
            stderr_str = str(exc)
            launch_error = f"{type(exc).__name__}: {exc}"

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        sig = extract_crash_signature(exit_code, stderr_str, stdout_str)
        infrastructure_reason = _infrastructure_failure_reason(
            exit_code=exit_code,
            stderr=stderr_str,
            stdout=stdout_str,
            launch_error=launch_error,
        )
        return RunResult(
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout_str,
            stderr=stderr_str,
            normalized_signature=sig,
            timed_out=timed_out,
            infrastructure_error=infrastructure_reason is not None,
            infrastructure_reason=infrastructure_reason or "",
            provisioning=provisioning_summary,
        )


def _stable_infra_signature_reason(res1: RunResult, res2: RunResult) -> str | None:
    """DEF-1 缺口2：双跑同指纹且命中基础设施模式时返回降级理由。

    复用现有 ``_infrastructure_failure_reason`` 分类器语义，并补上其未覆盖的
    模块缺失/导入失败形态（``ModuleNotFoundError: No module named 'app'``）。
    仅在两侧归一化签名完全一致（稳定复现的环境错误）时生效；签名分歧、
    单侧偶发的路径维持原有 FLAKY/CONFIRMED 裁决，判定语义不放宽——真实
    缺陷（非 infra 签名）的双跑确定性复现仍然 CONFIRMED。
    """
    if res1.normalized_signature != res2.normalized_signature:
        return None
    reason = _infrastructure_failure_reason(
        exit_code=res1.exit_code, stderr=res1.stderr, stdout=res1.stdout,
    )
    if reason:
        return reason
    if _STABLE_INFRA_SIGNATURE_RE.search(res1.normalized_signature):
        return (
            f"stable same-fingerprint module/import failure: "
            f"{res1.normalized_signature[:160]}"
        )
    return None


def verify_poc(
    cmd_template: str,
    poc_input: Any,
    *,
    timeout: float = 10.0,
    accepted_exits: set[int] | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """快捷入口：验证单个 PoC 文件。"""
    path, temp_file = _materialize_poc_input(poc_input)
    try:
        return run_differential_verification(
            cmd_template=cmd_template,
            input_path=path,
            timeout=timeout,
            accepted_exits=accepted_exits,
            workspace_root=workspace_root,
        )
    finally:
        if temp_file is not None and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def verify_red_blue_debate(
    cmd_template: str,
    red_poc_path: Path,
    blue_counter_template: str | None = None,
    timeout: float = 30.0,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """DOC-137 2.0 / D137-07: 红蓝双 Agent 闭环对抗博弈与差分验证裁决器。

    Red 提交候选攻击 PoC，Blue 提交反例/防御断言命令。
    在独立沙盒执行并判定：
    - CONFIRMED_DETERMINISTIC: Red 成功穿透且 Blue 无法反驳
    - BLUE_REFUTED_FALSE_POSITIVE: Blue 证明该情况被防御拦截或属合法业务反例
    - FALSE_POSITIVE: Red 原 PoC 在干净沙盒无法复现
    - DISPUTED_CANDIDATE: 对抗超时（>30s）或争议未决，归档人工复核
    """
    start_t = time.perf_counter()
    if not red_poc_path.is_file():
        raise DifferentialOracleError(f"Red PoC 文件不存在: {red_poc_path}")

    # 1. Red 独立沙盒穿透验证（硬超时 <= timeout）
    red_content = red_poc_path.read_bytes()
    red_provision_plan = _plan_workspace_provisioning(
        [cmd_template, red_content.decode("utf-8", errors="replace")],
        workspace_root,
        cmd_template=cmd_template,
    )
    red_res = _execute_in_sandbox(
        cmd_template, red_content, red_poc_path.name,
        timeout=min(timeout, 15.0), provision_plan=red_provision_plan,
    )
    if red_res.infrastructure_error:
        return {
            "schema_version": SCHEMA_VERSION,
            "oracle_tier": "red_blue_debate",
            "verdict": "INFRA_ERROR",
            "confirmed": False,
            "red_penetrated": False,
            "blue_refuted": False,
            "infrastructure_reason": red_res.infrastructure_reason,
            "summary": f"Red PoC 验证基础设施失败，未对目标作出裁决: {red_res.infrastructure_reason}",
            "latency_ms": (time.perf_counter() - start_t) * 1000,
        }
    red_penetrated = red_res.exit_code != 0 or red_res.timed_out

    if not red_penetrated:
        return {
            "schema_version": SCHEMA_VERSION,
            "oracle_tier": "red_blue_debate",
            "verdict": "FALSE_POSITIVE",
            "confirmed": False,
            "red_penetrated": False,
            "blue_refuted": False,
            "summary": "Red PoC 在干净基线沙盒中未触发任何异常（exit 0），判定为环境假阳性",
            "latency_ms": (time.perf_counter() - start_t) * 1000,
        }

    # 2. 如果存在 Blue 反驳/防御脚本
    blue_refuted = False
    blue_timed_out = False
    blue_detail = "未提供 Blue 反驳脚本，退化为双跑一致性判定"

    if blue_counter_template:
        blue_res = _execute_in_sandbox(
            blue_counter_template,
            red_content,
            red_poc_path.name,
            timeout=min(timeout - (time.perf_counter() - start_t), 15.0),
            provision_plan=red_provision_plan,
        )
        blue_timed_out = blue_res.timed_out
        if blue_res.infrastructure_error:
            return {
                "schema_version": SCHEMA_VERSION,
                "oracle_tier": "red_blue_debate",
                "verdict": "INFRA_ERROR",
                "confirmed": False,
                "red_penetrated": True,
                "blue_refuted": False,
                "infrastructure_reason": blue_res.infrastructure_reason,
                "summary": f"Blue 反驳验证基础设施失败，未对目标作出裁决: {blue_res.infrastructure_reason}",
                "latency_ms": (time.perf_counter() - start_t) * 1000,
            }
        if blue_timed_out:
            verdict = "DISPUTED_CANDIDATE"
            summary = "Blue 反驳验证超时（达到 30s 硬限制），标记为争议候选归档人工复核"
            return {
                "schema_version": SCHEMA_VERSION,
                "oracle_tier": "red_blue_debate",
                "verdict": verdict,
                "confirmed": False,
                "red_penetrated": True,
                "blue_refuted": False,
                "summary": summary,
                "latency_ms": (time.perf_counter() - start_t) * 1000,
            }

        # 若 Blue 脚本成功运行并证明正常阻断（exit 0），说明防御有效
        if blue_res.exit_code == 0:
            blue_refuted = True
            blue_detail = "Blue 防御审查反例成立：现有网关/类型系统已正常拦截该行为"
        else:
            blue_detail = f"Blue 尝试反驳失败（退出码 {blue_res.exit_code}），未能防御 Red 攻击"

    if blue_refuted:
        verdict = "BLUE_REFUTED_FALSE_POSITIVE"
        summary = f"Red 触发异常但被 Blue 成功反驳：{blue_detail}，拦截假阳性"
        confirmed = False
    else:
        # 进行第二次沙盒验证以保证确定性
        dual = run_differential_verification(
            cmd_template, red_poc_path, timeout=10.0, workspace_root=workspace_root,
        )
        if dual["confirmed"]:
            verdict = "CONFIRMED_DETERMINISTIC"
            summary = f"Red 攻击真实有效且 Blue 无法反驳（{blue_detail}），两次双跑确定性复现，确认为真实漏洞"
            confirmed = True
        else:
            verdict = dual["verdict"]
            summary = f"Red 攻击在双跑中表现不稳定（{dual['summary']}），暂未升级为确定性缺陷"
            confirmed = False

    return {
        "schema_version": SCHEMA_VERSION,
        "oracle_tier": "red_blue_debate",
        "verdict": verdict,
        "confirmed": confirmed,
        "red_penetrated": red_penetrated,
        "blue_refuted": blue_refuted,
        "summary": summary,
        "blue_detail": blue_detail,
        "latency_ms": (time.perf_counter() - start_t) * 1000,
    }


def run_differential_verification(
    cmd_template: str,
    input_path: str | os.PathLike[str],
    timeout: float = 10.0,
    accepted_exits: set[int] | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """执行隔离双跑差分验证并产出判定结论与证据工件。"""
    input_file = Path(input_path)
    if not input_file.is_file():
        raise DifferentialOracleError(f"输入 PoC 文件不存在: {input_file}")

    input_content = input_file.read_bytes()
    input_sha256 = hashlib.sha256(input_content).hexdigest()
    input_filename = input_file.name
    expected_good_exits = accepted_exits or {0}

    # DEF-1：双跑共享同一份沙盒投递计划（同源同布局），保证确定性裁决；
    # workspace_root 为 None 时保持旧的 workspace-blind 行为。
    provision_plan = _plan_workspace_provisioning(
        [cmd_template, input_content.decode("utf-8", errors="replace")],
        workspace_root,
        cmd_template=cmd_template,
    )

    # 执行 Run 1 与 Run 2（两个完全独立的临时目录）
    res1 = _execute_in_sandbox(
        cmd_template, input_content, input_filename,
        timeout=timeout, provision_plan=provision_plan,
    )
    res2 = _execute_in_sandbox(
        cmd_template, input_content, input_filename,
        timeout=timeout, provision_plan=provision_plan,
    )

    infrastructure_runs = [
        result for result in (res1, res2) if result.infrastructure_error
    ]
    if infrastructure_runs:
        reasons = sorted({item.infrastructure_reason for item in infrastructure_runs if item.infrastructure_reason})
        verdict = "INFRA_ERROR"
        summary = "差分验证基础设施失败，拒绝把稳定错误升级为目标缺陷"
        if reasons:
            summary += f": {'; '.join(reasons)}"
    else:
        verdict = ""
        summary = ""

    is_fail1 = res1.exit_code not in expected_good_exits or res1.timed_out
    is_fail2 = res2.exit_code not in expected_good_exits or res2.timed_out

    # 差分裁决
    if verdict == "INFRA_ERROR":
        pass
    elif not is_fail1 and not is_fail2:
        verdict = "FALSE_POSITIVE"
        summary = f"双跑均正常退出（exit {res1.exit_code}），未能复现异常，判定为环境假阳性"
    elif is_fail1 != is_fail2:
        verdict = "FLAKY_JITTER"
        summary = f"双跑行为分歧（Run 1 exit {res1.exit_code} vs Run 2 exit {res2.exit_code}），判定为偶发不稳定抖动"
    else:
        # 两次均失败，比较归一化签名是否吻合
        # DEF-1 缺口2：同指纹但命中基础设施模式（含模块缺失/导入失败）时，
        # 这是稳定环境错误而非目标缺陷证据，必须降级为 INFRA_ERROR，
        # 绝不能升级 CONFIRMED_DETERMINISTIC（负对照门保持不变）。
        stable_infra_reason = _stable_infra_signature_reason(res1, res2)
        if stable_infra_reason is not None:
            verdict = "INFRA_ERROR"
            summary = (
                "双跑同指纹但命中基础设施错误模式，拒绝把稳定环境错误升级为目标缺陷"
                f": {stable_infra_reason}"
            )
        elif res1.exit_code == res2.exit_code and res1.normalized_signature == res2.normalized_signature:
            verdict = "CONFIRMED_DETERMINISTIC"
            summary = f"两次隔离沙盒独立复验一致崩溃（exit {res1.exit_code}，签名: {res1.normalized_signature}），确认为真实确定性缺陷"
        elif res1.exit_code == res2.exit_code:
            verdict = "CONFIRMED_DETERMINISTIC"
            summary = f"两次双跑均返回相同退出码 {res1.exit_code}，确认为确定性异常"
        else:
            verdict = "FLAKY_JITTER"
            summary = f"两次双跑均报错但退出码分歧（{res1.exit_code} != {res2.exit_code}），归类为不稳定用例"

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "oracle_tier": "deterministic_dual_run",
        "verdict": verdict,
        "confirmed": verdict == "CONFIRMED_DETERMINISTIC",
        "command_template": cmd_template,
        "input_file": str(input_file.resolve()),
        "input_sha256": input_sha256,
        "runs": [
            {
                "run": 1,
                "exit_code": res1.exit_code,
                "duration_ms": res1.duration_ms,
                "signature": res1.normalized_signature,
                "timed_out": res1.timed_out,
                "infrastructure_error": res1.infrastructure_error,
                "infrastructure_reason": res1.infrastructure_reason,
                "stderr_preview": res1.stderr[:300],
            },
            {
                "run": 2,
                "exit_code": res2.exit_code,
                "duration_ms": res2.duration_ms,
                "signature": res2.normalized_signature,
                "timed_out": res2.timed_out,
                "infrastructure_error": res2.infrastructure_error,
                "infrastructure_reason": res2.infrastructure_reason,
                "stderr_preview": res2.stderr[:300],
            },
        ],
        "summary": summary,
    }
    if provision_plan is not None:
        # DEF-1 收据：记录沙盒投递了哪些被测目标源码（证据可追溯，
        # 且与 workspace-blind 调用路径字段级可区分）。
        plan_root, plan_members = provision_plan
        report["sandbox_provisioning"] = {
            "workspace_root": str(plan_root),
            "references": [token for token, _, _ in plan_members],
            "run1": res1.provisioning,
            "run2": res2.provisioning,
        }
    return report


def verify_differential_poc(
    cmd_template: str,
    poc_input: Any,
    *,
    timeout: float = 10.0,
    accepted_exits: set[int] | None = None,
    allowed_root: Path | None = None,
) -> dict[str, Any]:
    """供 MCP 或外部模块便捷调用的确定性双跑沙盒差分验证接口。"""
    root = allowed_root.expanduser().resolve() if allowed_root is not None else None
    path, temp_file = _materialize_poc_input(poc_input, base_dir=root)
    if temp_file is None:
        resolved_path = path.resolve(strict=True)
        if root is not None:
            try:
                resolved_path.relative_to(root)
            except ValueError as exc:
                raise DifferentialOracleError(
                    f"PoC 输入文件必须位于项目根目录内: {resolved_path}"
                ) from exc
        path = resolved_path
    try:
        # DEF-1 关键接线：allowed_root 之前只用于 PoC 路径越界校验，随后在
        # verify_poc 边界被丢弃，导致沙盒从不投递被测目标源码；这里把根
        # 目录传入双跑，让 PoC 引用的 workspace 相对路径在沙盒内真实可达。
        return verify_poc(
            cmd_template, path, timeout=timeout, accepted_exits=accepted_exits,
            workspace_root=root,
        )
    finally:
        if temp_file is not None and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    p_verify = sub.add_parser("verify", help="对候选 PoC 执行确定性沙盒双跑差分验证")
    p_verify.add_argument("--cmd", required=True, help="目标命令模板（使用 {input} 占位输入文件）")
    p_verify.add_argument("--input", type=Path, required=True, help="候选 PoC 输入文件")
    p_verify.add_argument("--timeout", type=float, default=10.0, help="单次运行超时秒数（默认 10）")
    p_verify.add_argument("--accepted-exits", default="0", help="预期正常退出码（逗号分隔，默认 0）")
    p_verify.add_argument(
        "--workspace", type=Path, default=None,
        help="被测项目根目录；提供后把 PoC 引用的项目内文件投递进沙盒再双跑（DEF-1）",
    )
    p_verify.add_argument("--out", type=Path, default=None, help="输出报告 JSON 保存路径")
    p_verify.add_argument("--json", action="store_true", help="以 JSON 格式输出至 stdout")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.action == "verify":
        accepted = {int(code.strip()) for code in args.accepted_exits.split(",") if code.strip()}
        try:
            report = verify_poc(
                args.cmd,
                args.input,
                timeout=args.timeout,
                accepted_exits=accepted,
                workspace_root=args.workspace,
            )
        except DifferentialOracleError as exc:
            print(f"[differential_oracle] 错误: {exc}", file=sys.stderr)
            return 2

        formatted_json = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(formatted_json, encoding="utf-8")
            print(f"[differential_oracle] 验证报告已写入: {args.out}")

        if args.json or not args.out:
            print(formatted_json)
        else:
            status_icon = "✓" if report["confirmed"] else "✗"
            print(f"[differential_oracle] {status_icon} 裁决: {report['verdict']} — {report['summary']}")

        # 确认为真实 Bug 返回 0，基础设施失败返回 2，其他未确认返回 1。
        if report["verdict"] == "INFRA_ERROR":
            return 2
        return 0 if report["confirmed"] else 1

    return 0


def is_available() -> tuple[bool, str]:
    """降级契约：机械探活沙盒双跑前提（解释器 + 可写临时目录，零子进程）。"""
    if not sys.executable:
        return False, "缺少 Python 解释器；隔离沙盒双跑不可用"
    tmp = tempfile.gettempdir()
    if not os.path.isdir(tmp) or not os.access(tmp, os.W_OK):
        return False, f"沙盒临时目录不可写: {tmp}"
    return True, f"沙盒临时目录可用: {tmp}"


def run_static_degraded(
    run1: Mapping[str, Any] | None = None,
    run2: Mapping[str, Any] | None = None,
    *,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    """降级契约：沙盒双跑不可用时，仅对已记录输出做归一化签名静态裁决。

    输出与成功路径（``run_differential_verification`` 的真实隔离双跑工件）
    字段级可区分：``status="degraded"`` + ``degraded=True`` + ``degraded_reason``、
    ``oracle_tier="static_signature_only"``，且 ``confirmed`` 恒为 False——
    静态签名比对只能提供线索，绝不直接确认确定性缺陷。
    """
    available, availability_detail = is_available()

    def _normalize_recorded(run: Mapping[str, Any] | None) -> tuple[int, str, str] | None:
        if not isinstance(run, Mapping):
            return None
        exit_code = run.get("exit_code")
        if exit_code is None:
            return None
        stdout = run.get("stdout")
        stderr = run.get("stderr")
        stdout_text = (
            stdout.decode("utf-8", errors="replace")
            if isinstance(stdout, bytes) else str(stdout or "")
        )
        stderr_text = (
            stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes) else str(stderr or "")
        )
        try:
            return int(exit_code), stdout_text, stderr_text
        except (TypeError, ValueError):
            return None

    side1 = _normalize_recorded(run1)
    side2 = _normalize_recorded(run2)
    if side1 is None or side2 is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "success": False,
            "status": "degraded",
            "degraded": True,
            "degraded_reason": "dynamic_dual_run_unavailable",
            "oracle_tier": "static_signature_only",
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confirmed": False,
            "input_sha256": input_sha256,
            "findings": [],
            "evidence": {
                "availability_detail": availability_detail,
                "recorded_runs_present": False,
            },
            "metadata": {
                "degraded_mode": "no_static_evidence_available",
                "sandbox_runs_executed": 0,
            },
        }
    signature1 = extract_crash_signature(side1[0], side1[2], side1[1])
    signature2 = extract_crash_signature(side2[0], side2[2], side2[1])
    if side1[0] != side2[0]:
        verdict = "FLAKY_JITTER"
        verdict_summary = (
            f"已记录双跑退出码分歧（{side1[0]} != {side2[0]}），归类为不稳定用例"
        )
    elif signature1 == signature2:
        verdict = "DETERMINISTIC_SIGNATURE_MATCH"
        verdict_summary = (
            f"已记录双跑签名一致（exit {side1[0]}，签名: {signature1}）；"
            "需真实沙盒双跑复验后才能确认确定性缺陷"
        )
    else:
        verdict = "SIGNATURE_MISMATCH_SAME_EXIT"
        verdict_summary = (
            f"已记录双跑退出码相同（exit {side1[0]}）但归一化签名分歧，证据不足"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "status": "degraded",
        "degraded": True,
        "degraded_reason": (
            "static_mode_forced" if available else "dynamic_dual_run_unavailable"
        ),
        "oracle_tier": "static_signature_only",
        "verdict": verdict,
        "confirmed": False,
        "summary": verdict_summary,
        "input_sha256": input_sha256,
        "findings": [],
        "evidence": {
            "availability_detail": availability_detail,
            "recorded_runs_present": True,
            "signatures": [signature1, signature2],
            "exit_codes": [side1[0], side2[0]],
        },
        "metadata": {
            "degraded_mode": "recorded_output_signature_compare",
            "sandbox_runs_executed": 0,
            "fallback": "沙盒双跑不可用时只做签名线索裁决；正式确认必须重跑 run_differential_verification",
        },
    }


if __name__ == "__main__":
    sys.exit(main())
