#!/usr/bin/env python3
"""BugX 自举动态审计：安全 CLI/MCP 探针、Fuzz、ddmin 与回归快照。

默认只运行本地只读入口。它是编排器，不是第二套 Supervisor，也不绕过
Finding confirmation 或知识库导入门禁。
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
import sys
from pathlib import Path


import argparse
import ast
import json
import os
import shlex
import shutil
import tempfile
from typing import Any, Mapping, Sequence

try:
    from core.runtime import bugx_paths
    from core.probes import differential_probe
    from core.repair import minimize_repro
    from core.fuzzing import fuzz_input
    from core.supervisor.worker_execution import run_bounded
except ImportError:  # direct execution from the global engine
    import core.bugx_paths as bugx_paths  # type: ignore[no-redef]
    from core.probes import differential_probe  # type: ignore[no-redef]
    from core.fuzzing import fuzz_input  # type: ignore[no-redef]
    from core.repair import minimize_repro  # type: ignore[no-redef]
    from core.supervisor.worker_execution import run_bounded  # type: ignore[no-redef]


MAX_PROBE_INPUT_BYTES = 16 * 1024 * 1024
MAX_PROBE_ARGS = 64
MAX_BASELINE_OUTPUT = 32 * 1024
SAFE_CLI_COMMANDS = frozenset({
    "help", "doctor", "agent", "mcp", "probe", "logic-analysis", "knowledge",
})
BASELINE_CASES: tuple[tuple[str, ...], ...] = (
    ("--help",),
    ("doctor", "--help"),
    ("mcp", "--help"),
    ("agent", "--help"),
)
MCP_SEED = (
    b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{'
    b'"protocolVersion":"2024-11-05","capabilities":{},'
    b'"clientInfo":{"name":"bugx-self-audit","version":"1"}}}\n'
    b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}\n'
    b'{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}\n'
)


class SelfAuditError(ValueError):
    """自举参数或安全边界无效。"""


def _read_input(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SelfAuditError(f"探针输入不是普通文件: {path}")
    data = path.read_bytes()
    if len(data) > MAX_PROBE_INPUT_BYTES:
        raise SelfAuditError(f"探针输入超过 {MAX_PROBE_INPUT_BYTES} 字节")
    return data


def _safe_output_path(path: Path, label: str) -> Path:
    """拒绝把自举证据写入调用方未明确指定的符号链接目标。"""
    path = path.expanduser()
    if path.is_symlink():
        raise SelfAuditError(f"拒绝写入符号链接{label}: {path}")
    return path


def _cli_argv(data: bytes) -> list[str] | None:
    """解析 CLI 变异；危险或非只读组合作为预期拒绝，不执行。"""
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8").strip()
        argv = shlex.split(text)
    except (UnicodeDecodeError, ValueError):
        return None
    if not argv or len(argv) > MAX_PROBE_ARGS:
        return None
    if argv in (["--help"], ["-h"]):
        return argv
    if argv[0] not in SAFE_CLI_COMMANDS:
        return None
    # Only help/usage paths are safe for an automatically mutated input.  This
    # prevents reset/install/cloud/login and arbitrary project writes.
    if not argv[1:] or any(item not in {"--help", "-h"} for item in argv[1:]):
        return None
    return argv


def _cli_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    source = root / "src"
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(source), old)))
    return env


def _run_cli_probe(data: bytes, root: Path, timeout: float) -> tuple[int, str]:
    argv = _cli_argv(data)
    if argv is None:
        return 2, "expected-rejection"
    result = run_bounded(
        [sys.executable, "-m", "bugx_cli.cli", *argv],
        cwd=root,
        env=_cli_environment(root),
        timeout=timeout,
        max_output_bytes=fuzz_input.MAX_OUTPUT_BYTES,
    )
    if getattr(result, "timed_out", False):
        return 124, "timeout"
    if getattr(result, "output_exceeded", False):
        return 125, "output-limit"
    return int(result.returncode), "ok" if result.returncode == 0 else "exit"


def _mcp_call_is_mutating(data: bytes) -> bool:
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(decoded, Mapping) and decoded.get("method") == "tools/call"


def _run_mcp_probe(data: bytes, root: Path, timeout: float) -> tuple[int, str]:
    # A fuzzed tools/call can mutate runtime state.  It is rejected before a
    # child process is launched; malformed/read-only protocol messages still
    # exercise the real stdio parser.
    if _mcp_call_is_mutating(data):
        return 0, "blocked-mutating-call"
    with tempfile.NamedTemporaryFile(prefix="bugx-mcp-input-", suffix=".jsonl", delete=False) as handle:
        stream_path = Path(handle.name)
        handle.write(data)
        if not data.endswith(b"\n"):
            handle.write(b"\n")
    try:
        result = run_bounded(
            [sys.executable, str(Path(__file__).resolve()), "serve-mcp-probe",
             "--root", str(root)],
            cwd=root,
            timeout=timeout,
            stdin_path=stream_path,
            max_output_bytes=fuzz_input.MAX_OUTPUT_BYTES,
        )
    finally:
        try:
            stream_path.unlink()
        except OSError:
            pass
    if getattr(result, "timed_out", False):
        return 124, "timeout"
    if getattr(result, "output_exceeded", False):
        return 125, "output-limit"
    return int(result.returncode), "ok" if result.returncode == 0 else "exit"


def _probe(kind: str, input_path: Path, root: Path, timeout: float) -> int:
    data = _read_input(input_path)
    if not root.is_dir():
        raise SelfAuditError(f"项目根目录不是目录: {root}")
    if kind == "cli":
        code, status = _run_cli_probe(data, root, timeout)
    elif kind == "mcp":
        code, status = _run_mcp_probe(data, root, timeout)
    else:
        raise SelfAuditError(f"未知探针类型: {kind}")
    print(json.dumps({
        "probe": kind,
        "state": status,
        "feedback": {"probe_status": status, "exit_code": code},
    }, ensure_ascii=False, separators=(",", ":")))
    return code


def _serve_mcp_probe(root: Path) -> int:
    """仅作为有界子进程入口，避免 self_audit 对 MCP server 做 import 侧写。"""
    server_path = Path(__file__).resolve().parents[1] / "skills" / "bugx_mcp_server.py"
    if not server_path.is_file():
        server_path = Path(__file__).resolve().parent / "skills" / "bugx_mcp_server.py"
    if not server_path.is_file():
        server_path = Path(__file__).with_name("bugx_mcp_server.py")
    command = [sys.executable, str(server_path),
               "serve", "--root", str(root), "--role", "worker"]
    # stdin is inherited from the bounded parent; its file descriptor is safe
    # and already bounded by the caller.
    import subprocess
    completed = subprocess.run(command, cwd=str(root), stdin=None, capture_output=False)
    return int(completed.returncode)


def _probe_command(kind: str, root: Path) -> str:
    return shlex.join([
        sys.executable, str(Path(__file__).resolve()), "probe",
        "--kind", kind, "--root", str(root), "--input", "{input}",
    ])


def _prepare_mcp_scratch_project() -> Path:
    """为 MCP fuzz 建立一次性已初始化项目（0.6.78 审计修复）。

    serve() 以项目运行态身份写日志、做布局自愈；worker 角色虽不抢控制器
    租约，但超时被杀的实例仍会在调用方真实项目里留下陈旧运行态。隔离到
    临时初始化项目后，fuzz 对调用方项目零写入，残留随临时目录一起丢弃。
    """
    scratch = Path(tempfile.mkdtemp(prefix="self-audit-mcp-"))
    bugx_paths.initialize(scratch)
    return scratch


def _run_fuzz(kind: str, root: Path, out: Path, *, count: int,
              jobs: int, timeout: float, seed: int | None) -> dict:
    seed_path = out / f"{kind}-seed.bin"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "cli":
        seed_path.write_bytes(b"--help\n")
        accepted = "0,2"
    else:
        # One bounded JSONL stream covers the read-only handshake surface;
        # mutation then exercises malformed framing and field boundaries.
        seed_path.write_bytes(MCP_SEED)
        accepted = "0"
    fuzz_out = out / kind
    evolve = fuzz_out / "evolution"
    argv = [
        "--input", str(seed_path), "--cmd", _probe_command(kind, root),
        "--out", str(fuzz_out), "--count", str(count), "--jobs", str(jobs),
        "--timeout", str(timeout), "--accepted-exits", accepted,
        "--feedback-json", "--evolve", str(evolve),
    ]
    if seed is not None:
        argv.extend(["--seed", str(seed)])
    code = fuzz_input.main(argv)
    summary_path = fuzz_out / "summary.json"
    if code != 0 or not summary_path.is_file():
        raise SelfAuditError(f"{kind} Fuzz 未生成汇总（exit={code}）")
    return json.loads(summary_path.read_text(encoding="utf-8"))


def _minimize_candidate(kind: str, root: Path, source: Path, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="self-audit-ddmin-") as directory:
        workdir = Path(directory)
        check = shlex.join([
            sys.executable, str(Path(__file__).resolve()), "repro",
            "--kind", kind, "--root", str(root), "--input", "{input}",
            "--timeout", str(timeout),
        ])
        minimizer = minimize_repro.Minimizer(
            cmd=shlex.join(["true", "{input}"]),
            check=check,
            timeout=timeout,
        )
        data = source.read_bytes()
        if not minimizer.reproduces(data, workdir):
            return {"status": "unverified", "reason": "异常未能重复触发", "input": str(source)}
        minimized = minimizer.ddmin(data, workdir)
        target = source.with_name(source.name + ".min")
        target.write_bytes(minimized)
        verified = minimizer.reproduces(minimized, workdir)
    return {
        "status": "verified" if verified else "unverified",
        "input": str(source), "minimized": str(target),
        "original_bytes": len(data), "minimized_bytes": len(minimized),
    }


def _candidate_records(kind: str, summary: Mapping[str, Any], root: Path,
                       fuzz_out: Path, timeout: float) -> list[dict]:
    candidates = []
    for item in summary.get("crashes", []):
        if not isinstance(item, Mapping):
            continue
        filename = item.get("file")
        if not isinstance(filename, str):
            continue
        source = fuzz_out / "crashes" / filename
        if not source.is_file() or source.is_symlink():
            continue
        record = {
            "kind": kind, "crash_signature": item.get("crash_signature"),
            "occurrences": item.get("occurrences", 1),
            "feedback": item.get("feedback", {}), "input": str(source),
        }
        try:
            record["minimization"] = _minimize_candidate(kind, root, source, timeout)
        except (OSError, ValueError) as exc:
            record["minimization"] = {"status": "unverified", "reason": str(exc)}
        candidates.append(record)
    return candidates


def _normalize_output(value: object) -> str:
    text = str(value or "")
    return "\n".join(line.rstrip() for line in text.splitlines())[:MAX_BASELINE_OUTPUT]


def _baseline_snapshot(root: Path) -> dict:
    cases = []
    for argv in BASELINE_CASES:
        result = run_bounded(
            [sys.executable, "-m", "bugx_cli.cli", *argv],
            cwd=root, env=_cli_environment(root), timeout=10,
            max_output_bytes=MAX_BASELINE_OUTPUT,
        )
        cases.append({
            "argv": list(argv), "exit": result.returncode,
            "stdout": _normalize_output(result.stdout),
            "stderr": _normalize_output(result.stderr),
            "timed_out": bool(getattr(result, "timed_out", False)),
            "output_exceeded": bool(getattr(result, "output_exceeded", False)),
        })
    return {"schema_version": 1, "cases": cases}


def _baseline(root: Path, action: str, path: Path) -> int:
    path = _safe_output_path(path, "基线")
    if action == "create":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_baseline_snapshot(root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[self_audit] baseline created: {path}")
        return 0
    if path.is_symlink() or not path.is_file():
        raise SelfAuditError(f"基线文件不存在或不安全: {path}")
    expected = json.loads(path.read_text(encoding="utf-8"))
    actual = _baseline_snapshot(root)
    if expected != actual:
        print(json.dumps({"baseline": "mismatch", "expected": expected, "actual": actual}, ensure_ascii=False, indent=2))
        return 1
    print(f"[self_audit] baseline verified: {path}")
    return 0


def _deadcode_scan(root: Path) -> dict:
    """静态未引用定义报告（report-only，绝不删除文件）。

    近似规则：收集 src/ 与 core/ 顶层函数与类定义，在 src/core/tests 的
    全部 AST 名称引用（Name 与 Attribute）中计数；引用数为 0 才入选。
    已知误报来源：argparse choices 字符串分发、getattr 动态引用——
    因此本报告仅提供建议，删除决策必须逐项人工确认。
    """
    targets = sorted((root / "src").rglob("*.py")) + sorted((root / "core").rglob("*.py"))
    corpus = targets + sorted((root / "tests").rglob("*.py"))
    references: dict[str, int] = {}
    definitions: dict[str, list[dict]] = {}
    for path in corpus:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                references[node.id] = references.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute):
                references[node.attr] = references.get(node.attr, 0) + 1
        if path not in targets:
            continue
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.If):
                continue
            for name in names:
                if name.startswith("__"):
                    continue
                definitions.setdefault(name, []).append({
                    "file": str(path.relative_to(root)), "line": node.lineno,
                })
    unused = []
    for name in sorted(definitions):
        if references.get(name, 0) == 0:
            unused.append({"name": name, "definitions": definitions[name]})
    return {
        "schema_version": 1,
        "scanned_files": len(corpus),
        "definitions": sum(len(v) for v in definitions.values()),
        "unused_candidates": unused,
        "note": "report-only；字符串动态分发（argparse choices/getattr）可能误报，删除前必须逐项人工确认",
    }


def _deadcode(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    out = _safe_output_path(args.out, "deadcode 报告")
    report = _deadcode_scan(root)
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "deadcode-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[self_audit] deadcode 扫描：{report['scanned_files']} 个文件，"
              f"{report['definitions']} 个顶层定义，"
              f"{len(report['unused_candidates'])} 个零引用候选")
        for item in report["unused_candidates"]:
            for loc in item["definitions"]:
                print(f"  ? {item['name']}  ({loc['file']}:{loc['line']})")
        print(f"[self_audit] 报告：{report_path}（report-only，删除前逐项人工确认）")
    return 0


def run_audit(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    out = _safe_output_path(args.out, "自举输出目录")
    if not root.is_dir():
        raise SelfAuditError(f"项目根目录不是目录: {root}")
    out = out.resolve()
    if out == root:
        raise SelfAuditError("--out 不能覆盖项目根目录")
    if bool(args.target_cmd) != bool(args.reference_cmd):
        raise SelfAuditError("--target-cmd 与 --reference-cmd 必须同时提供")
    if args.target_cmd and args.reference_cmd:
        if args.target_cmd.strip() == args.reference_cmd.strip():
            raise SelfAuditError("target/reference 不能是同一命令；这不是有效差分 oracle")
        if args.inputs is None:
            raise SelfAuditError("使用差分时必须提供 --inputs")
    out.mkdir(parents=True, exist_ok=True)
    summaries = {}
    candidates = []
    scratch_project: Path | None = None
    try:
        for kind in ("cli", "mcp") if args.mode == "all" else (args.mode,):
            fuzz_root = root
            if kind == "mcp":
                scratch_project = _prepare_mcp_scratch_project()
                fuzz_root = scratch_project
            summary = _run_fuzz(kind, fuzz_root, out, count=args.count, jobs=args.jobs,
                                 timeout=args.timeout, seed=args.seed)
            summaries[kind] = summary
            candidates.extend(_candidate_records(
                kind, summary, fuzz_root, out / kind, args.timeout))
    finally:
        if scratch_project is not None:
            shutil.rmtree(scratch_project, ignore_errors=True)
    baseline_status = None
    if args.baseline:
        baseline_status = _baseline(root, "verify", args.baseline)
    differential = None
    if args.target_cmd and args.reference_cmd:
        differential = differential_probe.run_comparison(
            args.target_cmd, args.reference_cmd, args.inputs, out / "differential",
            repeat= args.repeat, timeout=args.timeout, allow_shell=args.allow_shell,
        )
    report = {
        "schema_version": 1, "root": str(root), "mode": args.mode,
        "summaries": summaries, "candidates": candidates,
        "mcp_scratch_project": None if scratch_project is None else "(isolated temp project, removed after run)",
        "baseline_status": baseline_status, "differential": differential,
        "knowledge_policy": "candidate-only; finding_confirmation and review required",
    }
    (out / "knowledge-candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (out / "audit-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"[self_audit] report: {out / 'audit-report.json'}")
    return 0 if baseline_status in (None, 0) else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="运行本地 CLI/MCP 自举 Fuzz")
    run.add_argument("--root", type=Path, default=Path.cwd())
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--mode", choices=("cli", "mcp", "all"), default="all")
    run.add_argument("--count", type=int, default=32)
    run.add_argument("--jobs", type=int, default=4)
    run.add_argument("--timeout", type=float, default=3.0)
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--baseline", type=Path, default=None)
    run.add_argument("--target-cmd", default=None)
    run.add_argument("--reference-cmd", default=None)
    run.add_argument("--inputs", type=Path, default=None)
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--allow-shell", action="store_true")
    base = sub.add_parser("baseline", help="创建或验证 CLI 只读快照")
    base.add_argument("action", choices=("create", "verify"))
    base.add_argument("--root", type=Path, default=Path.cwd())
    base.add_argument("--out", "--baseline", dest="path", type=Path, required=True)
    probe = sub.add_parser("probe", help=argparse.SUPPRESS)
    probe.add_argument("--kind", choices=("cli", "mcp"), required=True)
    probe.add_argument("--root", type=Path, required=True)
    probe.add_argument("--input", type=Path, required=True)
    probe.add_argument("--timeout", type=float, default=2.0)
    repro = sub.add_parser("repro", help=argparse.SUPPRESS)
    repro.add_argument("--kind", choices=("cli", "mcp"), required=True)
    repro.add_argument("--root", type=Path, required=True)
    repro.add_argument("--input", type=Path, required=True)
    repro.add_argument("--timeout", type=float, default=2.0)
    serve = sub.add_parser("serve-mcp-probe", help=argparse.SUPPRESS)
    serve.add_argument("--root", type=Path, required=True)
    dead = sub.add_parser(
        "deadcode", help="静态未引用定义报告（report-only，不删除任何文件）")
    dead.add_argument("--root", type=Path, default=Path.cwd())
    dead.add_argument("--out", type=Path, required=True)
    dead.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            if args.count < 1 or args.jobs < 1 or args.timeout <= 0:
                raise SelfAuditError("--count、--jobs、--timeout 必须 > 0")
            if bool(args.target_cmd) != bool(args.reference_cmd):
                raise SelfAuditError("--target-cmd 与 --reference-cmd 必须同时提供")
            if args.target_cmd and args.inputs is None:
                raise SelfAuditError("使用差分时必须提供 --inputs")
            return run_audit(args)
        if args.command == "baseline":
            return _baseline(args.root.expanduser().resolve(), args.action, args.path.expanduser())
        if args.command == "probe":
            return _probe(args.kind, args.input, args.root.expanduser().resolve(), args.timeout)
        if args.command == "repro":
            code = _probe(args.kind, args.input, args.root.expanduser().resolve(), args.timeout)
            accepted = {0, 2} if args.kind == "cli" else {0}
            return 0 if code not in accepted else 1
        if args.command == "serve-mcp-probe":
            return _serve_mcp_probe(args.root.expanduser().resolve())
        if args.command == "deadcode":
            return _deadcode(args)
        raise SelfAuditError(f"未知命令: {args.command}")
    except (OSError, ValueError, json.JSONDecodeError, SelfAuditError) as exc:
        print(f"[self_audit] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
