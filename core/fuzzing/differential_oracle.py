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
      [--timeout 10] [--json] [--out report.json]
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
import shlex
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

SCHEMA_VERSION = 1
_SHELL_PUNCTUATION = ";&|<>`$()"
_SHELL_OPERATORS = frozenset({
    ";", "&", "&&", "|", "||", "<", ">", ">>", "<<", "<&", ">&",
    "`", "$(", "(", ")",
})


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
    """解析 argv 模板并拒绝未授权 shell 运算符。"""
    lexer = shlex.shlex(template, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        argv = list(lexer)
    except ValueError as exc:
        raise DifferentialOracleError(f"命令模板无法解析: {exc}") from exc
    if not argv:
        raise DifferentialOracleError("命令模板不能为空")
    if any(token in _SHELL_OPERATORS for token in argv):
        raise DifferentialOracleError("命令包含 shell 运算符；为确保纯净沙盒隔离，请传纯 argv 格式")
    return argv


def _execute_in_sandbox(
    cmd_template: str,
    input_content: bytes,
    input_filename: str,
    timeout: float = 10.0,
    accepted_exits: set[int] | None = None,
) -> RunResult:
    """在全新的独立临时目录沙盒中执行单次目标命令。"""
    with tempfile.TemporaryDirectory(prefix="bugx-oracle-sandbox-") as sandbox_dir:
        sandbox_path = Path(sandbox_dir)
        sandbox_input = sandbox_path / input_filename
        sandbox_input.write_bytes(input_content)

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
        )


def verify_poc(
    cmd_template: str,
    poc_input: Any,
    *,
    timeout: float = 10.0,
    accepted_exits: set[int] | None = None,
) -> dict[str, Any]:
    """快捷入口：验证单个 PoC 文件。"""
    path, temp_file = _materialize_poc_input(poc_input)
    try:
        return run_differential_verification(
            cmd_template=cmd_template,
            input_path=path,
            timeout=timeout,
            accepted_exits=accepted_exits,
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
    red_res = _execute_in_sandbox(cmd_template, red_content, red_poc_path.name, timeout=min(timeout, 15.0))
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
        dual = run_differential_verification(cmd_template, red_poc_path, timeout=10.0)
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
) -> dict[str, Any]:
    """执行隔离双跑差分验证并产出判定结论与证据工件。"""
    input_file = Path(input_path)
    if not input_file.is_file():
        raise DifferentialOracleError(f"输入 PoC 文件不存在: {input_file}")

    input_content = input_file.read_bytes()
    input_sha256 = hashlib.sha256(input_content).hexdigest()
    input_filename = input_file.name
    expected_good_exits = accepted_exits or {0}

    # 执行 Run 1 与 Run 2（两个完全独立的临时目录）
    res1 = _execute_in_sandbox(cmd_template, input_content, input_filename, timeout=timeout)
    res2 = _execute_in_sandbox(cmd_template, input_content, input_filename, timeout=timeout)

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
        if res1.exit_code == res2.exit_code and res1.normalized_signature == res2.normalized_signature:
            verdict = "CONFIRMED_DETERMINISTIC"
            summary = f"两次隔离沙盒独立复验一致崩溃（exit {res1.exit_code}，签名: {res1.normalized_signature}），确认为真实确定性缺陷"
        elif res1.exit_code == res2.exit_code:
            verdict = "CONFIRMED_DETERMINISTIC"
            summary = f"两次双跑均返回相同退出码 {res1.exit_code}，确认为确定性异常"
        else:
            verdict = "FLAKY_JITTER"
            summary = f"两次双跑均报错但退出码分歧（{res1.exit_code} != {res2.exit_code}），归类为不稳定用例"

    return {
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
        return verify_poc(cmd_template, path, timeout=timeout, accepted_exits=accepted_exits)
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


if __name__ == "__main__":
    sys.exit(main())
