#!/usr/bin/env python3
"""source_inspector.py — 安全源码切片与视窗自愈探针 (DOC-225).

提供轻量、安全、具备边界自愈能力的源文件切片与行读取能力，
杜绝在 Shell 或 Agent 环境中手拼脆弱 Python 脚本引发 IndexError、乱码与临时文件残留。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import tempfile
from typing import Any

MAX_INSPECT_FILE_BYTES = 10 * 1024 * 1024  # 10MB
DEFAULT_MAX_LINES = 500
ABSOLUTE_MAX_LINES = 2000

# 过滤破坏性不可打印控制字符（保留换行、回车、制表符与合法 Unicode）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class InspectResult:
    """源码检查与切片结果对象。"""
    path: str
    resolved_path: str
    total_lines: int
    start_line: int
    end_line: int
    clamped: bool
    eof_reached: bool
    line_count: int
    content: str
    lines: list[dict[str, Any]]
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_allowed_roots(root: Path | None = None) -> list[Path]:
    """计算允许读取的沙箱根目录白名单。

    支持跨平台（macOS、Linux、Windows）安全受控临时目录，
    确保 /tmp、/var/tmp 以及 Windows %TEMP% / %TMP% 能够合法只读访问，
    同时严禁路径穿越至敏感系统目录。
    """
    if root is not None:
        return [root.resolve()]

    allowed: list[Path] = []
    env_root = os.environ.get("BUGX_PROJECT_ROOT")
    if env_root:
        allowed.append(Path(env_root).resolve())
    allowed.append(Path.cwd().resolve())

    # 全局引擎目录 ~/.bugx
    home_bugx = Path.home() / ".bugx"
    allowed.append(home_bugx.resolve())

    # 跨平台受控临时目录（系统 tempfile 目录）
    try:
        sys_temp = Path(tempfile.gettempdir()).resolve()
        allowed.append(sys_temp)
    except Exception:
        pass

    # POSIX 标准 /tmp 与 /var/tmp 目录（包括 macOS /private/tmp 与软链接）
    if os.name == "posix":
        for p in ("/tmp", "/var/tmp"):
            tp = Path(p)
            try:
                if tp.exists():
                    allowed.append(tp.resolve())
            except OSError:
                pass

    # 环境变量中的临时目录（Windows %TEMP%, %TMP% 或 Linux/macOS $TMPDIR）
    for var in ("TEMP", "TMP", "TMPDIR"):
        val = os.environ.get(var)
        if val:
            wp = Path(val)
            try:
                if wp.exists():
                    allowed.append(wp.resolve())
            except OSError:
                pass

    # 保持唯一并保序
    seen: set[Path] = set()
    unique_allowed: list[Path] = []
    for p in allowed:
        if p not in seen:
            seen.add(p)
            unique_allowed.append(p)

    return unique_allowed


def _verify_sandbox_path(target_path: Path, allowed_roots: list[Path]) -> None:
    """验证目标路径是否在允许的沙箱边界之内。"""
    resolved = target_path.resolve()
    for base in allowed_roots:
        try:
            resolved.relative_to(base)
            return
        except ValueError:
            continue
    raise PermissionError(
        f"沙箱拦截：路径 '{target_path}' 逃逸出允许的工作区边界 ({[str(p) for p in allowed_roots]})"
    )


def inspect_source(
    path: str | Path,
    start_line: int = 1,
    end_line: int | None = None,
    *,
    numbered: bool = True,
    max_lines: int = DEFAULT_MAX_LINES,
    root: str | Path | None = None,
) -> InspectResult:
    """安全读取并切片源文件。

    具备以下强自愈与安全特性：
    1. 边界夹紧 (Clamping)：若 start_line / end_line 超出范围或倒置，自动自愈调整，绝不拋出 IndexError；
    2. 编码自愈 (Sanitation)：UTF-8 容错解码，清除 NUL 字节等有害控制符，完整保留多字节 Unicode（中文、日文、特殊字符）；
    3. 内存保护 (OOM Defense)：限制文件读取上限 (10MB) 与单次最大返回行数；
    4. 沙箱防护 (Sandbox Guard)：限制只读访问当前项目根目录、全局 ~/.bugx 及受控临时目录。
    """
    raw_path = Path(path).expanduser()
    base_root = Path(root).resolve() if root else None
    allowed_roots = _get_allowed_roots(base_root)

    # 若为相对路径，基于 base_root 或 cwd 解析
    if not raw_path.is_absolute():
        anchor = base_root or Path.cwd()
        target_path = (anchor / raw_path).resolve()
    else:
        target_path = raw_path.resolve()

    _verify_sandbox_path(target_path, allowed_roots)

    if not target_path.exists():
        raise FileNotFoundError(f"文件不存在: {target_path}")
    if target_path.is_dir():
        raise IsADirectoryError(f"目标是目录而非文件: {target_path}")

    size = target_path.stat().st_size
    if size > MAX_INSPECT_FILE_BYTES:
        raise ValueError(
            f"文件大小 ({size} bytes) 超过检查上限 ({MAX_INSPECT_FILE_BYTES} bytes)"
        )

    # 二进制读取并容错解码
    raw_bytes = target_path.read_bytes()
    decoded = raw_bytes.decode("utf-8", errors="replace")
    sanitized = _CONTROL_CHAR_RE.sub("", decoded)
    all_lines = sanitized.splitlines()
    total_lines = len(all_lines)

    # 规范化行数上限
    bounded_max_lines = max(1, min(int(max_lines), ABSOLUTE_MAX_LINES))

    clamped = False
    eof_reached = False
    warning: str | None = None

    # 空文件处理
    if total_lines == 0:
        return InspectResult(
            path=str(path),
            resolved_path=str(target_path),
            total_lines=0,
            start_line=1,
            end_line=0,
            clamped=False,
            eof_reached=True,
            line_count=0,
            content="",
            lines=[],
            warning="文件为空",
        )

    # 规范化 start_line
    start = int(start_line)
    if start < 1:
        start = 1
        clamped = True

    # 规范化 end_line
    if end_line is None:
        end = min(total_lines, start + bounded_max_lines - 1)
        if end >= total_lines:
            eof_reached = True
    else:
        end = int(end_line)
        if end < start:
            # 倒置行号自愈纠正
            start, end = end, start
            clamped = True
            if start < 1:
                start = 1

    # 起始行越界检查（超越文件 EOF）
    if start > total_lines:
        return InspectResult(
            path=str(path),
            resolved_path=str(target_path),
            total_lines=total_lines,
            start_line=start,
            end_line=total_lines,
            clamped=True,
            eof_reached=True,
            line_count=0,
            content="",
            lines=[],
            warning=f"start_line ({start}) 超过文件总行数 ({total_lines})",
        )

    # 结束行越界自愈
    if end > total_lines:
        end = total_lines
        clamped = True
        eof_reached = True

    # 跨度过大截断自愈
    span = end - start + 1
    if span > bounded_max_lines:
        end = start + bounded_max_lines - 1
        clamped = True
        warning = f"请求行数跨度超过最大限制 ({bounded_max_lines})"

    # 安全原生切片（Python 原生切片绝对不抛 IndexError）
    selected_slice = all_lines[start - 1 : end]
    line_count = len(selected_slice)

    # 组装结构化行与纯文本
    pad_width = max(len(str(end)), 2)
    lines_list: list[dict[str, Any]] = []
    formatted_texts: list[str] = []

    for idx, line_text in enumerate(selected_slice, start=start):
        lines_list.append({"line": idx, "content": line_text})
        if numbered:
            formatted_texts.append(f"{idx:>{pad_width}}: {line_text}")
        else:
            formatted_texts.append(line_text)

    content = "\n".join(formatted_texts)

    return InspectResult(
        path=str(path),
        resolved_path=str(target_path),
        total_lines=total_lines,
        start_line=start,
        end_line=end,
        clamped=clamped,
        eof_reached=eof_reached,
        line_count=line_count,
        content=content,
        lines=lines_list,
        warning=warning,
    )
