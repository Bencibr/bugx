#!/usr/bin/env python3
"""minimize_repro.py — 失败输入自动最小化（delta-debugging）。

用途：给定一个"能复现 bug 的完整命令 + 输入文件"，自动删除冗余片段，
输出仍能复现的最短输入——最小输入 = 根因最集中 = 挖掘更深、举证更高效。

用法：
  python3 minimize_repro.py --cmd '<目标命令，用 {input} 占位>' \
      --input <原始输入文件> --check '<验证脚本，{input} 占位，exit0=仍复现>'

示例：
  # 目标：python3 app.py 读入 input.txt，崩溃则 exit 非 0
  python3 minimize_repro.py \
      --cmd 'python3 app.py {input}' \
      --input crash.txt \
      --check 'test -s {input}'

  # 更精确：用 shell 判断是否仍复现
  python3 minimize_repro.py \
      --cmd 'python3 app.py {input}' \
      --input crash.txt \
      --check 'python3 app.py {input} 2>&1 | grep -q "Segmentation fault"'

工作原理（ddmin 算法）：
  1. 把输入按粒度 2 切块，逐个删除块；删除后仍复现 → 保留删除（缩小）。
  2. 粒度翻倍，重复；粒度超过输入长度则停止。
  3. 每一步都在临时文件验证，不污染原输入。

输出：最小输入写入 <input>.min，并打印原大小→最小大小的对比。

安全：命令默认按 argv 执行；使用管道、&& 等 shell 语法时必须显式加
`--allow-shell`，且仅应对完全可信的命令模板使用。
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
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import core.bugx_paths as bugx_paths

from core.supervisor.worker_execution import run_bounded

# QUA-03：命令拆分与 shell argv 复用 fuzz_input 的唯一实现，避免两份漂移。
from core.fuzzing.fuzz_input import shell_argv as _shell_argv, split_argv as _split_argv

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024


def _failed_result(argv: list[str], error: OSError) -> subprocess.CompletedProcess:
    result = subprocess.CompletedProcess(argv, "EXECUTION_ERROR", "", str(error))
    result.status = "execution-error"  # type: ignore[attr-defined]
    result.error = str(error)  # type: ignore[attr-defined]
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--synthesize-standalone", action="store_true",
                   help="合成单个零依赖独立复现验证脚本")
    p.add_argument("--target-module", default="",
                   help="目标模块名称或文件路径")
    p.add_argument("--call", default="",
                   help="触发调用的 Python 表达式")
    p.add_argument("--expected-exc", default="",
                   help="预期捕获的异常类名（如 AssertionError, KeyError）")
    p.add_argument("--assert-expr", default="",
                   help="验证通过时应满足的断言表达式（如 res == 42）")
    p.add_argument("--setup", default="",
                   help="前置初始化代码片段")
    p.add_argument("--out", default="",
                   help="合成脚本的输出路径")
    p.add_argument("--cmd", default="",
                   help="目标命令，用 {input} 占位表示输入文件路径")
    p.add_argument("--input", default="", help="原始输入文件路径")
    p.add_argument("--check", default="",
                   help="复现验证命令，{input} 占位；exit 0 = 仍复现，非 0 = 不再复现")
    p.add_argument("--timeout", type=float, default=30,
                   help="单次验证超时秒数（默认 30）")
    p.add_argument("--allow-shell", action="store_true",
                   help="允许通过 shell 执行命令（仅限完全可信的命令模板）")
    return p.parse_args()


class Minimizer:
    def __init__(self, cmd: str, check: str, timeout: float, allow_shell: bool = False):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout 必须是正数")
        self.cmd = cmd
        self.check = check
        self.timeout = timeout
        self.allow_shell = allow_shell
        self.last_failure: dict[str, object] | None = None

    def _run(self, template: str, inp: Path) -> subprocess.CompletedProcess:
        if not template.strip():
            raise ValueError("命令不能为空")
        if self.allow_shell:
            rendered = template.replace("{input}", shlex.quote(str(inp)))
            argv = _shell_argv(rendered)
        else:
            argv = [token.replace("{input}", str(inp)) for token in _split_argv(template)]
        try:
            result = run_bounded(
                argv,
                cwd=inp.parent,
                timeout=self.timeout,
                max_output_bytes=MAX_OUTPUT_BYTES,
            )
        except OSError as exc:
            return _failed_result(argv, exc)
        if getattr(result, "timed_out", False):
            result.status = "timeout"  # type: ignore[attr-defined]
        elif getattr(result, "output_exceeded", False):
            result.status = "output-limit"  # type: ignore[attr-defined]
        elif result.returncode == 0:
            result.status = "ok"  # type: ignore[attr-defined]
        else:
            result.status = "exit"  # type: ignore[attr-defined]
        return result

    def reproduces(self, data: bytes, tmp: Path) -> bool:
        """data 写入临时文件，跑验证命令；exit 0 = 仍复现。"""
        self.last_failure = None
        if len(data) > MAX_INPUT_BYTES:
            self.last_failure = {
                "status": "input-limit", "exit": "INPUT_LIMIT",
                "error": f"输入样本超过 {MAX_INPUT_BYTES} 字节上限: {len(data)}",
            }
            return False
        if not tmp.is_dir():
            raise ValueError(f"执行工作目录不是目录: {tmp}")
        inp = tmp / "input"
        if inp.is_symlink():
            inp.unlink()
        inp.write_bytes(data)
        # 配置校验：check 模板必须含 {input} 占位符，否则验证命令会改读
        # stdin 导致复现判定非确定（假阳/假阴）。配置错误必须上抛。
        # cmd 是"触发目标"命令，允许不含 {input}（如 `true`、`timeout 30 ./prog`）。
        if "{input}" not in self.check:
            raise ValueError(
                f"check 模板必须包含 {{input}} 占位符: {self.check!r}"
            )
        r = self._run(self.cmd, inp)
        if getattr(r, "status", "exit") != "ok":
            self.last_failure = {
                "status": getattr(r, "status", "exit"),
                "exit": r.returncode,
                "error": getattr(r, "error", ""),
            }
            return False
        r2 = self._run(self.check, inp)
        if getattr(r2, "status", "exit") != "ok":
            self.last_failure = {
                "status": getattr(r2, "status", "exit"),
                "exit": r2.returncode,
                "error": getattr(r2, "error", ""),
            }
            return False
        return True

    def ddmin(self, data: bytes, tmp: Path) -> bytes:
        """经典 ddmin：按粒度 2 起逐步分块删除。"""
        n = 2
        while len(data) >= 2:
            chunk = (len(data) + n - 1) // n
            reduced = False
            for start in range(0, len(data), chunk):
                candidate = data[:start] + data[start + chunk:]
                if candidate and self.reproduces(candidate, tmp):
                    data = candidate
                    n = max(n - 1, 2)
                    reduced = True
                    break
            if not reduced:
                if n >= len(data):
                    break
                n = min(n * 2, len(data))
        return data

    def ddmin_lines(self, data: bytes, tmp: Path) -> bytes:
        """行级 ddmin：按行切除无关代码块，保留整行结构。"""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        lines = text.splitlines(keepends=True)
        if len(lines) <= 2:
            return data

        n = 2
        while len(lines) >= 2:
            chunk = (len(lines) + n - 1) // n
            reduced = False
            for start in range(0, len(lines), chunk):
                candidate_lines = lines[:start] + lines[start + chunk:]
                candidate_bytes = "".join(candidate_lines).encode("utf-8")
                if candidate_bytes and self.reproduces(candidate_bytes, tmp):
                    lines = candidate_lines
                    n = max(n - 1, 2)
                    reduced = True
                    break
            if not reduced:
                if n >= len(lines):
                    break
                n = min(n * 2, len(lines))
        return "".join(lines).encode("utf-8")

    def hierarchical_ddmin(self, data: bytes, tmp: Path) -> bytes:
        """两阶段层次化 ddmin：先行级大块切除，再字符级精细切除。"""
        data = self.ddmin_lines(data, tmp)
        data = self.ddmin(data, tmp)
        return data


def minimize_repro_input(
    cmd: str,
    input_data: str | bytes | None = None,
    *,
    input_path: str | Path | None = None,
    root: str | Path | None = None,
    check_cmd: str | None = None,
    timeout: float = 10.0,
    allow_shell: bool = False,
    hierarchical: bool = True,
) -> dict[str, Any]:
    """供 Python 模块或 MCP 工具调用的 PoC 输入最小化引擎。

    ``input_data`` 是**纯内容**：即便字面量恰好等于某个已存在文件的路径，也不会
    被当作文件读取。要从项目内文件取样本时显式使用 ``input_path``（必须落在
    ``root`` 内且不含符号链接）。
    """
    if timeout <= 0:
        raise ValueError("timeout 必须 > 0")
    if input_data is not None and input_path is not None:
        raise ValueError("input_data 与 input_path 不能同时提供")

    if input_path is not None:
        if root is None:
            raise ValueError("使用 input_path 时必须提供 root 以校验项目边界")
        original = bugx_paths.read_confined_bytes(
            Path(root), input_path, max_bytes=MAX_INPUT_BYTES, error_cls=ValueError,
        )
    elif isinstance(input_data, bytes):
        original = input_data
    elif input_data is None:
        raise ValueError("必须提供 input_data 或 input_path")
    else:
        original = str(input_data).encode("utf-8")

    actual_check = check_cmd or (cmd if "{input}" in cmd else f"test -s {{input}}")
    if "{input}" not in actual_check:
        actual_check = f"{actual_check} {{input}}"

    m = Minimizer(cmd, actual_check, timeout, allow_shell)
    with tempfile.TemporaryDirectory(prefix="ddmin-api-") as td:
        tmp = Path(td)
        if not m.reproduces(original, tmp):
            return {
                "status": "NOT_REPRODUCIBLE",
                "reproduces": False,
                "original_bytes": len(original),
                "minimized_bytes": len(original),
                "reduction_pct": 0.0,
                "minimized_content": original.decode("utf-8", errors="replace"),
                "detail": "原始输入未能触发复现条件（check 返回非 0 或超时）",
            }
        minimized = m.hierarchical_ddmin(original, tmp) if hierarchical else m.ddmin(original, tmp)
        final_reproduces = m.reproduces(minimized, tmp)

    reduction = (1.0 - len(minimized) / len(original)) * 100.0 if len(original) > 0 else 0.0
    return {
        "status": "SUCCESS",
        "reproduces": final_reproduces,
        "original_bytes": len(original),
        "minimized_bytes": len(minimized),
        "reduction_pct": round(reduction, 2),
        "minimized_content": minimized.decode("utf-8", errors="replace"),
        "detail": f"输入从 {len(original)} 字节缩小至 {len(minimized)} 字节 (缩减 {reduction:.1f}%)",
    }


def synthesize_standalone_repro(
    target_module: str = "",
    call_expr: str = "",
    expected_exception: str = "",
    assert_expr: str = "",
    setup_code: str = "",
    output_path: Path | None = None,
) -> str:
    """合成一个单个 .py 文件、零外部第三方依赖（仅用 Python 标准库）、自包含 assert 的验证脚本。

    脚本行为契约（必须与补丁包红绿回放一致：``before_exit=0`` / ``after_exit=1``）：
    - 若 Bug 存在（或抛出预期异常 / 触发断言错误）：退出码为 0（复现成功证明）；
    - 若 Bug 已修复（未抛出异常且满足正常断言）：退出码为 1（修复闭环证明）。

    合成的脚本会被 ``stage_repair.md`` 指引直接作为 ``bugx patch-forge --repro`` 的输入，
    因此这里的退出码方向必须与 ``docs/patch-engine/schemas/patch-manifest.schema.json``
    中 ``repro[].before_exit=0 / after_exit=1`` 的约束逐字一致；反向产出会让红绿预检
    在 Phase 1 直接判「未复现缺陷」。
    """
    script_lines = [
        "#!/usr/bin/env python3",
        '"""自包含零依赖独立复现验证脚本（由 BugX 自动合成）。',
        "",
        "退出码契约（与 patch-forge 红绿回放一致）：",
        "  0 = Bug 依然存在（复现成功证明，对应 before_exit）",
        "  1 = Bug 已被修复（闭环通过证明，对应 after_exit）",
        '"""',
        "",
        "import sys",
        "import traceback",
        "",
    ]
    if setup_code.strip():
        script_lines.append("# --- 前置初始化 ---")
        script_lines.append(setup_code.strip())
        script_lines.append("")

    if target_module.strip():
        script_lines.append("# --- 引入目标模块 ---")
        if target_module.endswith(".py"):
            p = Path(target_module).resolve()
            script_lines.append(f"sys.path.insert(0, {repr(str(p.parent))})")
            script_lines.append(f"import {p.stem} as target")
        else:
            script_lines.append(f"import {target_module} as target")
        script_lines.append("")

    script_lines.append("def verify_bug():")
    script_lines.append("    # --- 执行触发调用 ---")
    if expected_exception.strip():
        script_lines.append("    try:")
        if call_expr.strip():
            script_lines.append(f"        {call_expr}")
        else:
            script_lines.append("        pass")
        script_lines.append(f"    except {expected_exception} as exc:")
        script_lines.append('        print(f"[REPRO CONFIRMED] 成功触发预期缺陷异常: {exc}", file=sys.stderr)')
        script_lines.append("        traceback.print_exc()")
        script_lines.append("        # 退出码 0 表示复现成功（Bug 确实存在）")
        script_lines.append("        sys.exit(0)")
        script_lines.append('    print("[REPRO CLOSED] 未观察到预期缺陷异常（已修复闭环）")')
        script_lines.append("    sys.exit(1)")
    elif assert_expr.strip():
        # 语义断言模式：断言成立表示修复通过（退出 1）；断言失败或异常表示 Bug 仍存在（退出 0）
        script_lines.append("    try:")
        if call_expr.strip():
            script_lines.append(f"        res = {call_expr}")
        else:
            script_lines.append("        res = None")
        safe_expr = repr(assert_expr)
        script_lines.append(f"        assert {assert_expr}, f\"断言未满足: 期望 {{{safe_expr}}}, 实际 res={{res!r}}\"")
        script_lines.append('        print("[REPRO CLOSED] 语义断言成立且调用正常（已修复闭环）")')
        script_lines.append("        sys.exit(1)")
        script_lines.append("    except AssertionError as exc:")
        script_lines.append('        print(f"[REPRO CONFIRMED] 捕获缺陷断言未满足: {exc}", file=sys.stderr)')
        script_lines.append("        sys.exit(0)")
        script_lines.append("    except Exception as exc:")
        script_lines.append('        print(f"[REPRO CONFIRMED] 捕获内部异常: {exc}", file=sys.stderr)')
        script_lines.append("        sys.exit(0)")
    else:
        # Crash-Only Oracle 兜底模式：无异常且有正常返回值退出 1；发生崩溃/异常退出 0
        script_lines.append("    try:")
        if call_expr.strip():
            script_lines.append(f"        res = {call_expr}")
        else:
            script_lines.append("        res = None")
        script_lines.append('        print("[REPRO CLOSED] 调用正常执行未崩溃（已修复闭环）")')
        script_lines.append("        sys.exit(1)")
        script_lines.append("    except Exception as exc:")
        script_lines.append('        print(f"[REPRO CONFIRMED] 捕获内部异常崩溃: {exc}", file=sys.stderr)')
        script_lines.append("        sys.exit(0)")

    script_lines.append("")
    script_lines.append('if __name__ == "__main__":')
    script_lines.append("    verify_bug()")
    script_lines.append("")

    content = "\n".join(script_lines)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        try:
            out.chmod(0o755)
        except OSError:
            pass
    return content


def main() -> int:
    args = parse_args()
    if args.synthesize_standalone:
        out_path = Path(args.out) if args.out else Path(".bugx/evidence/repro_standalone.py")
        synthesize_standalone_repro(
            target_module=args.target_module,
            call_expr=args.call,
            expected_exception=args.expected_exc,
            assert_expr=args.assert_expr,
            setup_code=args.setup,
            output_path=out_path,
        )
        print(f"[minimize_repro] 独立自包含复现脚本已生成: {out_path}")
        return 0

    if not args.cmd or not args.input or not args.check:
        print("[minimize_repro] 缺少必要参数：--cmd, --input, --check 或 --synthesize-standalone")
        return 2

    if args.timeout <= 0:
        print("[minimize_repro] --timeout 必须 > 0")
        return 2
    src = Path(args.input)
    if not src.is_file():
        print(f"[minimize_repro] 输入文件不存在: {src}")
        return 2
    original = src.read_bytes()
    m = Minimizer(args.cmd, args.check, args.timeout, args.allow_shell)

    final_reproduces = False
    with tempfile.TemporaryDirectory(prefix="ddmin-") as td:
        tmp = Path(td)
        # 先确认原始输入确实复现
        if not m.reproduces(original, tmp):
            print("[minimize_repro] ✗ 原始输入不触发复现（check 返回非 0 或超时）——")
            print("  先确认 --cmd/--check 正确。不进行最小化。")
            return 1
        print(f"[minimize_repro] 原始输入可复现，开始最小化 "
              f"（{len(original)} 字节）…")
        minimized = m.ddmin(original, tmp)
        final_reproduces = m.reproduces(minimized, tmp)

    out = src.with_name(src.name + ".min")
    out.write_bytes(minimized)
    print(f"[minimize_repro] 完成: {len(original)} → {len(minimized)} 字节 "
          f"（缩减 {100 * (1 - len(minimized) / len(original)):.1f}%）")
    print(f"[minimize_repro] 最小输入已写入: {out}")
    print(f"[minimize_repro] 校验：最小输入仍复现 = "
          f"{'是' if final_reproduces else '否'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
