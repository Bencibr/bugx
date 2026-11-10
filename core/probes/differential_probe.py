#!/usr/bin/env python3
"""differential_probe.py — 同一语料差分对照目标实现与参考实现。

把同一批输入分别喂给目标命令和参考命令，比对退出码与规范化后的
stdout/stderr；行为分歧即候选语义 Bug（单看任何一侧都看不出来的那种）。
产物同时是最高级 Oracle 的真实工件：finding 确认协议里
``oracle.tier="differential"``、``provenance.kind="independent-implementation"``、
``oracle.reference="differential:<report_id>#case-<n>"`` 必须指向本工具的报告条目，
不允许凭空声明差分通过。

用法：
  python3 differential_probe.py run \
    --target-cmd "python3 target.py {input}" \
    --reference-cmd "python3 reference.py {input}" \
    --inputs corpus_dir --out diff-out [--repeat 3] [--timeout 10]

exit 0 = 对照完成并写出报告（分歧数见报告）；exit 2 = 参数或执行错误。
对照发现分歧不会让进程失败——它是候选，不是结论。
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
import shlex
import sys
import tempfile
import uuid
from pathlib import Path

from core.supervisor.worker_execution import run_bounded

# QUA-03：shell argv 复用 core.fuzzing.fuzz_input 的唯一实现；本模块的 _split_argv 保留自己的
# DifferentialError 语义（错误类型与文案与 fuzz_input 不同），因此不合并。
from core.fuzzing.fuzz_input import shell_argv as _shell_argv

DEFAULT_TIMEOUT = 10.0
DEFAULT_REPEAT = 1
MAX_INPUTS = 2000
MAX_CAPTURE = 65536
MAX_INPUT_BYTES = 16 * 1024 * 1024
_SHELL_PUNCTUATION = ";&|<>`$()"
_SHELL_OPERATORS = frozenset({
    ";", "&", "&&", "|", "||", "<", ">", ">>", "<<", "<&", ">&",
    "`", "$(", "(", ")",
})


class DifferentialError(ValueError):
    """差分对照协议或输入无效。"""


def _split_argv(template: str) -> list[str]:
    """解析 argv 模板，并拒绝未授权的 shell 语法。"""
    lexer = shlex.shlex(template, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        argv = list(lexer)
    except ValueError as exc:
        raise DifferentialError(f"命令无法解析: {exc}") from exc
    if not argv:
        raise DifferentialError("命令不能为空")
    if any(token in _SHELL_OPERATORS for token in argv):
        raise DifferentialError("命令包含 shell 运算符；如确需使用，请显式传入 --allow-shell")
    return argv


def check_readiness() -> dict:
    """Check the bounded runner and argv parser without executing target commands."""
    checks = [
        {
            "name": "bounded_runner",
            "ok": callable(run_bounded),
            "detail": "受限子进程执行器已加载" if callable(run_bounded) else "执行器不可用",
        },
        {
            "name": "argv_parser",
            "ok": callable(_shell_argv) and callable(_split_argv),
            "detail": "argv 解析器已加载" if callable(_shell_argv) and callable(_split_argv)
            else "argv 解析器不可用",
        },
    ]
    ready = all(check["ok"] for check in checks)
    return {
        "schema_version": 1,
        "name": "differential",
        "status": "READY" if ready else "DEGRADED",
        "checks": checks,
        "capabilities": ["bounded_differential_runner"] if ready else [],
        "fallbacks": {},
        "warnings": [],
    }


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8", errors="replace")


def _run_side(cmd: str, payload: bytes, workdir: Path, timeout: float,
              allow_shell: bool) -> dict:
    """执行一侧命令；{input} 占位符替换为写入的临时文件路径。"""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise DifferentialError("--timeout 必须 > 0")
    if len(payload) > MAX_INPUT_BYTES:
        return {
            "status": "input-limit", "exit": "INPUT_LIMIT",
            "stdout": b"", "stderr": b"",
            "error": f"输入样本超过 {MAX_INPUT_BYTES} 字节上限: {len(payload)}",
        }
    if not workdir.is_dir():
        raise DifferentialError(f"执行工作目录不是目录: {workdir}")
    inp = workdir / "input"
    if inp.is_symlink():
        inp.unlink()
    inp.write_bytes(payload)
    if allow_shell:
        rendered = cmd.replace("{input}", shlex.quote(str(inp)))
        argv = _shell_argv(rendered)
    else:
        argv = [token.replace("{input}", str(inp)) for token in _split_argv(cmd)]
    try:
        result = run_bounded(
            argv,
            cwd=workdir,
            timeout=timeout,
            max_output_bytes=MAX_CAPTURE,
        )
    except OSError as exc:
        return {
            "status": "execution-error", "exit": "EXECUTION_ERROR",
            "stdout": b"", "stderr": b"", "error": str(exc),
        }
    stdout = _as_bytes(result.stdout)[:MAX_CAPTURE]
    stderr = _as_bytes(result.stderr)[:MAX_CAPTURE]
    if getattr(result, "timed_out", False):
        status, exit_code = "timeout", "TIMEOUT"
    elif getattr(result, "output_exceeded", False):
        status, exit_code = "output-limit", "OUTPUT_LIMIT"
    elif result.returncode == 0:
        status, exit_code = "ok", 0
    else:
        status, exit_code = "exit", result.returncode
    return {
        "status": status,
        "exit": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }


def _normalize(stream: bytes) -> str:
    """逐行去尾部空白后拼接；掩盖纯格式差异，保留语义差异。"""
    text = stream.decode("utf-8", errors="replace")
    return "\n".join(line.rstrip() for line in text.splitlines())


def _outcome(side: dict) -> tuple:
    return (str(side["exit"]), _normalize(side["stdout"]), _normalize(side["stderr"]))


def _divergence_kinds(target: tuple, reference: tuple) -> list[str]:
    kinds = []
    if target[0] != reference[0]:
        kinds.append("exit_mismatch")
    if target[1] != reference[1]:
        kinds.append("stdout_mismatch")
    if target[2] != reference[2]:
        kinds.append("stderr_mismatch")
    return kinds


def _collect_inputs(inputs: Path) -> list[Path]:
    if inputs.is_file():
        return [inputs]
    if not inputs.is_dir():
        raise DifferentialError(f"--inputs 路径不存在: {inputs}")
    files = sorted(p for p in inputs.rglob("*") if p.is_file())
    if not files:
        raise DifferentialError(f"语料目录为空: {inputs}")
    if len(files) > MAX_INPUTS:
        raise DifferentialError(f"语料超过 {MAX_INPUTS} 条上限，请分批对照")
    return files


def run_comparison(target_cmd: str, reference_cmd: str, inputs: Path, out: Path, *,
                   repeat: int = DEFAULT_REPEAT, timeout: float = DEFAULT_TIMEOUT,
                   allow_shell: bool = False) -> dict:
    if not target_cmd.strip() or not reference_cmd.strip():
        raise DifferentialError("--target-cmd 与 --reference-cmd 必须是非空命令")
    if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= 10:
        raise DifferentialError("--repeat 必须在 1..10")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise DifferentialError("--timeout 必须 > 0")
    corpus = _collect_inputs(inputs)
    out.mkdir(parents=True, exist_ok=True)
    divergence_dir = out / "divergences"
    divergence_dir.mkdir(exist_ok=True)
    report_id = "D-" + uuid.uuid4().hex[:12]
    cases: list[dict] = []
    divergent = flaky = 0
    with tempfile.TemporaryDirectory(prefix="diff-probe-") as td:
        workdir = Path(td)
        for index, path in enumerate(corpus, 1):
            payload = path.read_bytes()
            observations: list[tuple] = []
            sides: dict[str, dict] = {}
            case_workdir = workdir / f"case-{index:04d}"
            for repeat_index in range(repeat):
                target_workdir = case_workdir / f"repeat-{repeat_index:02d}" / "target"
                reference_workdir = case_workdir / f"repeat-{repeat_index:02d}" / "reference"
                target_workdir.mkdir(parents=True, exist_ok=True)
                reference_workdir.mkdir(parents=True, exist_ok=True)
                target = _run_side(target_cmd, payload, target_workdir, timeout, allow_shell)
                reference = _run_side(
                    reference_cmd, payload, reference_workdir, timeout, allow_shell,
                )
                observations.append((_outcome(target), _outcome(reference)))
                sides = {"target": target, "reference": reference}
            first_kinds = _divergence_kinds(*observations[0])
            stable = all(
                _divergence_kinds(*obs) == first_kinds for obs in observations[1:]
            )
            case_id = f"case-{index:04d}"
            record = {
                "case_id": case_id,
                "input": str(path),
                "input_sha256": hashlib.sha256(payload).hexdigest(),
                "target_exit": str(sides["target"]["exit"]),
                "reference_exit": str(sides["reference"]["exit"]),
                "target_status": sides["target"]["status"],
                "reference_status": sides["reference"]["status"],
                "kinds": first_kinds,
                "stable": stable,
                "repeat": repeat,
            }
            for side_name in ("target", "reference"):
                error = sides[side_name].get("error")
                if error:
                    record[f"{side_name}_error"] = error
            if first_kinds:
                divergent += 1
                if not stable:
                    flaky += 1
                    record["flaky"] = True
                stem = f"{case_id}-{hashlib.sha256(payload).hexdigest()[:8]}"
                (divergence_dir / f"{stem}.input").write_bytes(payload)
                (divergence_dir / f"{stem}.target.out").write_bytes(
                    sides["target"]["stdout"] + sides["target"]["stderr"],
                )
                (divergence_dir / f"{stem}.reference.out").write_bytes(
                    sides["reference"]["stdout"] + sides["reference"]["stderr"],
                )
                record["artifacts"] = str(divergence_dir)
            cases.append(record)
    summary = {
        "schema_version": 1,
        "report_id": report_id,
        "target_cmd": target_cmd,
        "reference_cmd": reference_cmd,
        "inputs_total": len(corpus),
        "repeat": repeat,
        "timeout": timeout,
        "divergences": divergent,
        "flaky_divergences": flaky,
        "cases": cases,
        "oracle_usage": (
            "finding 确认协议引用本报告：oracle.tier=differential, "
            f"provenance.kind=independent-implementation, "
            f"oracle.reference=differential:{report_id}#<case_id>"
        ),
    }
    (out / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("check", "run"), nargs="?", default="run")
    parser.add_argument("--target-cmd",
                        help="目标实现命令；用 {input} 占位输入文件")
    parser.add_argument("--reference-cmd",
                        help="参考实现命令；用 {input} 占位输入文件")
    parser.add_argument("--inputs", type=Path,
                        help="语料文件或目录（≤2000 条）")
    parser.add_argument("--out", type=Path, help="报告输出目录")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT,
                        help="每条语料重复次数（默认 1；>1 可标记 flaky 分歧）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="单次执行超时秒（默认 10）")
    parser.add_argument("--allow-shell", action="store_true",
                        help="允许 shell 运算符（默认拒绝，防注入）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "check":
        report = check_readiness()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"[{report['status']}] differential 探针可用性")
            for check in report["checks"]:
                print(f"  {'✓' if check['ok'] else '✗'} {check['name']}: {check['detail']}")
        return 0 if report["status"] == "READY" else 2
    missing = [
        flag for flag, value in (
            ("--target-cmd", args.target_cmd), ("--reference-cmd", args.reference_cmd),
            ("--inputs", args.inputs), ("--out", args.out),
        ) if value is None
    ]
    if missing:
        parser.error("run 必须提供 " + ", ".join(missing))
    try:
        summary = run_comparison(
            args.target_cmd, args.reference_cmd, args.inputs, args.out,
            repeat=args.repeat, timeout=args.timeout, allow_shell=args.allow_shell,
        )
    except (DifferentialError, OSError) as exc:
        print(f"[differential_probe] ✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"[differential_probe] 对照 {summary['inputs_total']} 条语料 "
              f"(repeat={summary['repeat']})：分歧 {summary['divergences']}"
              f"（其中 flaky {summary['flaky_divergences']}）")
        for case in summary["cases"]:
            if case["kinds"]:
                flag = " (flaky)" if case.get("flaky") else ""
                print(f"  {case['case_id']}: {','.join(case['kinds'])}{flag} "
                      f"target_exit={case['target_exit']} "
                      f"reference_exit={case['reference_exit']} <- {case['input']}")
        print(f"[differential_probe] 报告: {args.out / 'report.json'}")
        print(f"[differential_probe] {summary['oracle_usage']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
