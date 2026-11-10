#!/usr/bin/env python3
"""Supervisor CLI 参数解析与业务分发。

状态机实现保留在 :mod:`audit_supervisor`；本模块只负责 CLI 边界，避免
解析和命令分发继续膨胀核心状态机模块。
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
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover - 仅注解
    from core.supervisor import api

# `rpc` 回退通道：与 core/bugx_mcp_server.py 的 MCP_PROTOCOL_VERSION 对齐。
_RPC_PROTOCOL_VERSION = "2024-11-05"
_RPC_DEFAULT_TIMEOUT_SECONDS = 15.0
_RPC_CLIENT_INFO: Mapping[str, object] = {
    "name": "bugx-supervisor-rpc", "version": "1",
}


try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


def _api(module: Any | None = None) -> Any:
    """Resolve the state-machine API without making an import-time cycle."""
    if module is not None:
        return module
    current = (
        sys.modules.get("core.supervisor.audit_supervisor")
    )
    if current is not None:
        return current
    import importlib

    return importlib.import_module("core.supervisor.audit_supervisor")


def _cli_merge_budget(args: argparse.Namespace, existing_budget: Any | None):
    api = _api()
    defaults = {
        "max_rounds": getattr(existing_budget, "max_rounds", 10),
        "max_wall_seconds": getattr(existing_budget, "max_wall_seconds", 3600.0),
        "max_round_wall_seconds": getattr(existing_budget, "max_round_wall_seconds", 900.0),
        "max_tool_calls": getattr(existing_budget, "max_tool_calls", 1000),
        "max_cost_units": getattr(existing_budget, "max_cost_units", 10000.0),
        "max_concurrency": getattr(existing_budget, "max_concurrency", None),
        "require_parallel": getattr(existing_budget, "require_parallel", True),
        "serial_reason": getattr(existing_budget, "serial_reason", None),
        "require_confirmation": getattr(existing_budget, "require_confirmation", False),
    }
    max_rounds = args.max_rounds if getattr(args, "max_rounds", None) is not None else defaults["max_rounds"]
    max_wall_seconds = args.max_wall_seconds if getattr(args, "max_wall_seconds", None) is not None else defaults["max_wall_seconds"]
    max_round_wall_seconds = (
        args.max_round_wall_seconds if getattr(args, "max_round_wall_seconds", None) is not None else defaults["max_round_wall_seconds"]
    )
    max_tool_calls = args.max_tool_calls if getattr(args, "max_tool_calls", None) is not None else defaults["max_tool_calls"]
    max_cost_units = args.max_cost_units if getattr(args, "max_cost_units", None) is not None else defaults["max_cost_units"]
    max_concurrency = args.max_concurrency if getattr(args, "max_concurrency", None) is not None else defaults["max_concurrency"]

    allow_serial = getattr(args, "allow_serial", False)
    require_parallel = (not allow_serial) if allow_serial else defaults["require_parallel"]
    serial_reason = args.serial_reason if allow_serial else defaults["serial_reason"]
    require_confirmation = (
        args.require_confirmation if getattr(args, "require_confirmation", False) else defaults["require_confirmation"]
    )

    return api.RunBudget(
        max_rounds=max_rounds,
        max_wall_seconds=max_wall_seconds,
        max_round_wall_seconds=max_round_wall_seconds,
        max_tool_calls=max_tool_calls,
        max_cost_units=max_cost_units,
        max_concurrency=max_concurrency,
        require_parallel=require_parallel,
        serial_reason=serial_reason,
        require_confirmation=require_confirmation,
    )


def cli_budget(args: argparse.Namespace):
    api = _api()
    d = api.RunBudget()
    return api.RunBudget(
        max_rounds=args.max_rounds if getattr(args, "max_rounds", None) is not None else d.max_rounds,
        max_wall_seconds=args.max_wall_seconds if getattr(args, "max_wall_seconds", None) is not None else d.max_wall_seconds,
        max_round_wall_seconds=args.max_round_wall_seconds if getattr(args, "max_round_wall_seconds", None) is not None else d.max_round_wall_seconds,
        max_tool_calls=args.max_tool_calls if getattr(args, "max_tool_calls", None) is not None else d.max_tool_calls,
        max_cost_units=args.max_cost_units if getattr(args, "max_cost_units", None) is not None else d.max_cost_units,
        max_concurrency=getattr(args, "max_concurrency", None),
        require_parallel=not getattr(args, "allow_serial", False),
        serial_reason=getattr(args, "serial_reason", None) if getattr(args, "allow_serial", False) else None,
        require_confirmation=getattr(args, "require_confirmation", False),
    )


def cli_supervisor(args: argparse.Namespace):
    # Preserve the caller's lexical root spelling (notably macOS /var ->
    # /private/var) so Supervisor can distinguish a real inner symlink from a
    # harmless system path alias while still canonicalizing confinement.
    root = args.root
    state = args.state if args.state.is_absolute() else root / args.state
    if getattr(args, "command", None) == "reset-budget":
        return _api().Supervisor(root, state_path=state)
    return _api().Supervisor(root, budget=cli_budget(args), state_path=state)


def _manifest_units(root: Path, manifest: Path):
    return _api().load_manifest_units(root, manifest)


def _cli_payload(path: Path):
    return _api().load_checkpoint_payload(path)


def cli_print(value: Mapping[str, object]) -> None:
    api = _api()
    if hasattr(value, "to_dict"):
        payload = value.to_dict()
    elif isinstance(value, api.RunState):
        payload = value.to_dict()
    elif isinstance(value, api.WorkerClaim):
        payload = value.to_dict()
    else:
        payload = dict(value)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def incident_hints(supervisor: "api.Supervisor", ledger: Path, limit: int) -> dict[str, object]:
    """按 recent_signatures 检索错题账本 incident 深案例，供 worker few-shot。

    Supervisor 本体不读账本（保持状态机与知识库解耦）；这是显式的只读
    桥接命令。账本缺失/无效只降级为空提示，不阻断调度。
    """
    status = supervisor.status()
    signatures = [
        str(item) for item in getattr(status, "recent_signatures", [])
        if isinstance(item, str) and item.strip()
    ]
    result: dict[str, object] = {
        "status": "ok", "signatures": signatures, "incidents": [],
    }
    if not signatures:
        result["note"] = "尚无 worker 提交错误签名；checkpoint 携带 signatures 后可用"
        return result
    try:
        from core.ledger import lesson_ledger

        database = lesson_ledger.database_path(ledger)
        if not database.is_file():
            result["note"] = f"错题账本运行库不存在: {database}"
            return result
        incidents = lesson_ledger.lookup_incidents(
            ledger, signatures=signatures, limit=limit,
        )
        result["incidents"] = [
            {
                "incident_id": item.get("incident_id"),
                "signature": item.get("signature"),
                "summary": item.get("summary"),
                "before_snippet": item.get("before_snippet"),
                "after_snippet": item.get("after_snippet"),
                "diff_ref": item.get("diff_ref"),
                "commit_ref": item.get("commit_ref"),
                "match_score": item.get("match_score"),
            }
            for item in incidents
        ]
    except (ImportError, OSError, ValueError, TypeError) as exc:
        result["status"] = "degraded"
        result["note"] = f"账本读取失败: {exc}"
    return result


def _rpc_server_script() -> Path:
    """定位 bugx_mcp_server.py；解析顺序与安装布局一致（方案三全局引擎）。

    首选与 cli.py 同级的引擎目录（开发仓库与 ``~/.bugx/core`` 安装布局都是
    ``core/supervisor/cli.py`` 旁边放 ``core/bugx_mcp_server.py``），其次尊重
    ``BUGX_GLOBAL_ENGINE_ROOT`` 覆盖，最后回落默认全局引擎路径。
    """
    engine_root = Path(__file__).resolve().parent.parent
    candidates = [engine_root / "skills" / "bugx_mcp_server.py"]
    override = os.environ.get("BUGX_GLOBAL_ENGINE_ROOT")
    if override:
        candidates.append(Path(override).expanduser() / "skills" / "bugx_mcp_server.py")
    candidates.append(Path.home() / ".bugx" / "core" / "skills" / "bugx_mcp_server.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "找不到 bugx_mcp_server.py（候选: "
        + ", ".join(str(item) for item in candidates) + "）"
    )


def _rpc_spawn(server_script: Path, root: Path, role: str) -> subprocess.Popen:
    """spawn `bugx mcp serve` 等价子进程；stderr 继承宿主，诊断不污染 stdout。"""
    return subprocess.Popen(
        [sys.executable, str(server_script),
         "serve", "--root", str(root), "--role", role],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        cwd=str(root),
    )


def _rpc_pump(stdout, lines: "queue.Queue") -> None:
    """后台线程逐行读子进程 stdout；EOF/异常都以 None 结尾标记。"""
    try:
        for raw in iter(stdout.readline, b""):
            lines.put(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def _rpc_send(proc: subprocess.Popen, payload: Mapping[str, object]) -> None:
    stdin = proc.stdin
    if stdin is None or stdin.closed:
        raise BrokenPipeError("MCP 子进程 stdin 不可写")
    stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    stdin.flush()


def _rpc_recv(lines: "queue.Queue", deadline: float):
    """读下一条带 id 的 JSON-RPC 响应；超时或 EOF 返回 None。"""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and "id" in message:
            return message


def _rpc_shutdown(proc: subprocess.Popen) -> None:
    # 顺序关键：先让子进程死掉（其 stdout 随之 EOF，解除 pump 线程对
    # readline/缓冲锁的占用），再关闭本地流；否则 close(stdout) 会在
    # 缓冲锁上与僵死的 pump 线程互相等待（死锁，kill 永远不会执行）。
    if proc.stdin is not None:
        try:
            proc.stdin.close()  # stdin EOF → serve 循环正常收尾
        except (OSError, ValueError):
            pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _rpc_fail(detail: Mapping[str, object], diagnostic: str) -> int:
    """通道级失败：stdout 输出可解析错误对象，诊断进 stderr，退出码 1。"""
    print(json.dumps(
        {"rpc_error": diagnostic, "detail": dict(detail)},
        ensure_ascii=False, default=str,
    ))
    print(f"[supervisor rpc] ✗ {diagnostic}", file=sys.stderr)
    return 1


def _cli_rpc(args: argparse.Namespace) -> int:
    """`rpc` 子命令：单次 MCP JSON-RPC 调用的 CLI 回退通道。

    spawn ``bugx mcp serve`` 子进程，完成 initialize/initialized 握手后发送
    ``tools/call``（或原始 method），读回响应并把完整 JSON 打到 stdout。
    退出码：0=成功；1=响应 error/通道失败；2=用法错误。
    """
    tool = getattr(args, "tool", None)
    method = getattr(args, "method", None)
    root = getattr(args, "root", None)
    role = getattr(args, "role", "worker")
    timeout = float(getattr(args, "timeout_seconds", _RPC_DEFAULT_TIMEOUT_SECONDS) or 0.0)

    def usage(message: str) -> int:
        print(f"[supervisor rpc] ✗ {message}", file=sys.stderr)
        return 2

    if root is None:
        return usage("必须提供 --root 项目根目录")
    if not root.is_dir():
        return usage(f"项目根目录不存在: {root}")
    if bool(tool) == bool(method):
        return usage(
            "--tool/--method 必须二选一："
            "--tool TOOL_NAME [--args JSON] 或 --method METHOD [--params JSON]"
        )
    if getattr(args, "args", None) is not None and method:
        return usage("--args 只能与 --tool 搭配")
    if getattr(args, "params", None) is not None and tool:
        return usage("--params 只能与 --method 搭配")
    if timeout <= 0:
        return usage("--timeout-seconds 必须是正数")

    call_arguments: dict[str, object] = {}
    raw_params: dict[str, object] = {}
    try:
        if tool and getattr(args, "args", None) is not None:
            call_arguments = json.loads(args.args)
        if method and getattr(args, "params", None) is not None:
            raw_params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        return usage(f"--args/--params 必须是合法 JSON: {exc}")
    if tool and not isinstance(call_arguments, dict):
        return usage("--args 必须是 JSON 对象（tools/call arguments）")
    if method and not isinstance(raw_params, dict):
        return usage("--params 必须是 JSON 对象")

    try:
        server_script = _rpc_server_script()
    except FileNotFoundError as exc:
        return _rpc_fail({"stage": "resolve_server"}, str(exc))

    deadline = time.monotonic() + timeout
    lines: "queue.Queue" = queue.Queue()
    proc = _rpc_spawn(server_script, root, role)
    pump = threading.Thread(
        target=_rpc_pump, args=(proc.stdout, lines), daemon=True,
    )
    pump.start()

    def channel_failed(stage: str, response: Any) -> int:
        _rpc_shutdown(proc)
        if time.monotonic() >= deadline:
            if proc.poll() is None:
                proc.kill()
            return _rpc_fail(
                {"stage": stage},
                f"MCP 子进程 {timeout:g}s 内无响应，已 kill（可用 --timeout-seconds 调大）",
            )
        return _rpc_fail(
            {"stage": stage, "response": response},
            "MCP 子进程提前退出（诊断见上方 stderr）",
        )

    try:
        try:
            _rpc_send(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": _RPC_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": dict(_RPC_CLIENT_INFO),
                },
            })
        except (BrokenPipeError, OSError):
            return _rpc_fail({"stage": "initialize"}, "MCP 子进程在握手前退出")
        handshake = _rpc_recv(lines, deadline)
        if handshake is None:
            return channel_failed("initialize", None)
        if "error" in handshake:
            return _rpc_fail(handshake, "initialize 握手被拒绝")
        try:
            _rpc_send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            if tool:
                request = {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": tool, "arguments": call_arguments},
                }
            else:
                request = {"jsonrpc": "2.0", "id": 2, "method": method, "params": raw_params}
            _rpc_send(proc, request)
        except (BrokenPipeError, OSError) as exc:
            return _rpc_fail({"stage": "request"}, f"MCP 子进程写入失败: {exc}")
        response = _rpc_recv(lines, deadline)
        if response is None:
            return channel_failed("request", None)
        print(json.dumps(response, ensure_ascii=False, default=str))
        if "error" in response:
            return 1
        result = response.get("result")
        if isinstance(result, Mapping) and result.get("isError"):
            return 1
        return 0
    finally:
        _rpc_shutdown(proc)


def main(argv: Sequence[str] | None = None, *, api: Any | None = None) -> int:
    """CLI wrapper；只管理状态，不执行 worker 的 command 字符串。"""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: str) -> argparse.ArgumentParser:
        item = sub.add_parser(command)
        item.add_argument("--root", type=Path, default=Path.cwd())
        item.add_argument(
            "--state", type=Path,
            default=Path(".bugx/audit-supervisor.json"),
        )
        item.add_argument(
            "--coverage-manifest", type=Path,
            default=None,
        )
        item.add_argument("--max-rounds", type=int, default=10)
        item.add_argument("--max-wall-seconds", type=float, default=3600.0)
        item.add_argument("--max-round-wall-seconds", type=float, default=900.0)
        item.add_argument("--max-tool-calls", type=int, default=1000)
        item.add_argument("--max-cost-units", type=float, default=10000.0)
        item.add_argument("--max-concurrency", type=int, default=None)
        item.add_argument(
            "--allow-serial", action="store_true",
            help="仅在明确资源/顺序约束时允许串行；必须同时提供 --serial-reason",
        )
        item.add_argument("--serial-reason")
        item.add_argument(
            "--require-confirmation", action="store_true",
            help="结束前要求所有 Finding 进入 confirmed 或 rejected 终态",
        )
        return item

    start = common("start")
    start.add_argument("--life-score", type=float, default=1.0)
    start.add_argument("--coverage-unit", action="append", default=[])
    start.add_argument("--run-id")
    start.add_argument("--round-id", default=None)
    start.add_argument("--life-id", default=None)

    claim = common("claim")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=float, default=1800.0)
    claim.add_argument("--unit", action="append", default=[])
    claim.add_argument("--resource-key", action="append", default=[])
    claim.add_argument(
        "--allow-bundle", action="store_true",
        help="允许一个 Worker 领取 1-5 个亲和聚合 coverage units",
    )
    claim.add_argument(
        "--process-pid", type=int, default=None,
        help="绑定真实且当前存活的执行进程 PID，用于僵死快速检测与抢占",
    )
    claim.add_argument(
        "--continue-worker", action="store_true",
        help="提交后由同一 Worker 身份继续领取同一 Round 的未完成单元",
    )

    claim.add_argument(
        "--recover-expired", action="store_true",
        help="同一 worker_id 对 expired/released claim 自接手（同代重试，attempt +1）",
    )
    claim.add_argument("--supersede-worker", default=None,
                       help="同一 Round 接手 expired/released worker，复用 artifact_root 并递增 attempt")

    worker_start = common("worker-start")
    worker_start.add_argument("--worker", required=True)
    worker_start.add_argument("--provider", default="unknown")
    worker_start.add_argument("--execution-id")
    worker_start.add_argument(
        "--process-pid", type=int, default=None,
        help="绑定真实且当前存活的执行进程 PID，用于僵死快速检测与抢占",
    )

    heartbeat = common("heartbeat")
    heartbeat.add_argument("--worker", required=True)
    heartbeat.add_argument("--lease-seconds", type=float, default=1800.0)
    heartbeat.add_argument("--heartbeat-nonce")

    tool_call = common("tool-call")
    tool_call.add_argument("--worker", required=True)
    tool_call.add_argument("--cost-units", type=float, default=0.0)
    tool_call.add_argument("--tool-name")

    checkpoint = common("checkpoint")
    checkpoint.add_argument("--worker", required=True)
    checkpoint.add_argument("--input", type=Path, required=True)
    checkpoint.add_argument(
        "--life-delta", type=float,
        help="兼容旧调用；非零值会被拒绝，生命必须由宿主统一结算",
    )

    for command in ("status", "gate", "next-round", "finish", "repair-fence"):
        common(command)
    # DOC-230 §3.1 层 1：`recover` 必须支持 `--reset-worker-attempts`——host 通道不可达时，
    # 这是唯一能解除 `expired_workers_require_retry` 并发门禁（从而推进轮次）的带外入口；
    # 缺该参数会使层 1 的"可达性兜底"在真实卡点处失效（只能重置运行时＝丢数据）。
    recover_cmd = common("recover")
    recover_cmd.add_argument(
        "--reset-worker-attempts", action="store_true",
        help="重置 worker 尝试计数，使 expired worker 可被重新领取（并发门禁要求）",
    )
    # DOC-230 §3.8 第 4 项：severity 修复通道。宿主 MCP 通道不可达时（例如被其它
    # IDE 会话占用），这是唯一能修复历史脏 severity 的带外入口——缺它则脏数据
    # 只能靠重置运行时（＝丢数据）来绕过，与"可修复而非永久毒化"的规格相悖。
    findings_cmd = common("findings")
    findings_cmd.add_argument(
        "--fix-severity", action="store_true",
        help="修复单条 finding 的 severity（只改该字段，绝不改动确认状态）",
    )
    findings_cmd.add_argument(
        "--finding", default=None,
        help="finding_key / fingerprint / id / finding_id 任一",
    )
    findings_cmd.add_argument(
        "--severity", default=None,
        help="目标 severity，必须属于写侧词表（info/low/medium/high/critical；p0-p3 别名可归一）",
    )
    findings_cmd.add_argument(
        "--reason", default=None,
        help=(
            "合法值之间改级的理由（C1/A12：severity 参与 Life 奖励与评分判定，"
            "静默改级会变成刷分/藏缺陷工具）；修复非法值无需提供"
        ),
    )
    findings_cmd.add_argument("--expected-revision", type=int, default=None)
    reset_budget_cmd = common("reset-budget")
    reset_budget_cmd.set_defaults(
        max_rounds=None,
        max_wall_seconds=None,
        max_round_wall_seconds=None,
        max_tool_calls=None,
        max_cost_units=None,
        max_concurrency=None,
    )
    reset_budget_cmd.add_argument(
        "--renew-wall-clock", action="store_true",
        help="刷新全局墙钟锚点 started_at，允许在墙钟耗尽后续期",
    )
    # DOC-230 §3.1 层 1 声明：`reset-budget --root <p> [--renew-wall-clock]
    # [--expected-revision N]`。core `Supervisor.reset_budget` 本就接受
    # `expected_revision`，但 CLI 未暴露 → 声明不可达（并发续期无法做乐观校验）。
    reset_budget_cmd.add_argument(
        "--expected-revision", type=int, default=None,
        help="乐观并发校验：状态 revision 不匹配即拒绝（并发续期必须带）",
    )
    reset_budget_cmd.add_argument(
        "--force", action="store_true",
        help="强制释放孤儿 active worker 后开启新的预算 epoch",
    )
    rebind_target = common("rebind-target")
    rebind_target.add_argument("--reason", required=True)
    fail = common("fail")
    fail.add_argument("--reason", required=True)
    hints = common("hints")
    hints.add_argument(
        "--ledger", type=Path,
        default=Path(".bugx/knowledge/lesson-ledger.json"),
        help="错题账本；按 recent_signatures 检索 incident 深案例",
    )
    hints.add_argument("--limit", type=int, default=2)

    reopen = common("reopen-finding")
    reopen.add_argument("--finding-id", required=True)
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--expected-revision", type=int, required=True)

    admit = common("admit-business")
    admit.add_argument("--unit-id", required=True)
    admit.add_argument("--risk-score", type=float, required=True)
    admit.add_argument("--reason", required=True)
    admit.add_argument("--expected-revision", type=int, default=None)

    rpc = sub.add_parser(
        "rpc",
        help="单次 MCP JSON-RPC 调用：spawn `bugx mcp serve`，握手后调用工具或原始方法并回传响应（宿主 MCP 路由不可用时的 CLI 回退通道）",
        epilog=(
            "退出码：0=成功；1=响应 error/通道失败（含超时 kill）；2=用法错误。"
            "stdout 只输出一条完整 JSON 响应，诊断走 stderr。"
        ),
    )
    rpc.add_argument("--root", type=Path, required=True, help="项目根目录")
    rpc.add_argument(
        "--role", choices=("host", "worker"), default="worker",
        help="MCP 通道角色；默认 worker（不持有主控租约，适合回退探测）",
    )
    rpc.add_argument(
        "--tool", default=None,
        help="tools/call 工具名，如 bugx_supervisor_status；与 --method 二选一",
    )
    rpc.add_argument(
        "--args", default=None,
        help="工具参数 JSON 对象；缺省 {}，只能与 --tool 搭配",
    )
    rpc.add_argument(
        "--method", default=None,
        help="原始 JSON-RPC 方法（如 ping）；与 --tool 二选一",
    )
    rpc.add_argument(
        "--params", default=None,
        help="原始方法参数 JSON 对象；缺省 {}，只能与 --method 搭配",
    )
    rpc.add_argument(
        "--timeout-seconds", type=float, default=_RPC_DEFAULT_TIMEOUT_SECONDS,
        help="子进程无响应 kill 时限，默认 15 秒；长轮询工具应显式调大",
    )

    args = parser.parse_args(argv)
    if args.command == "rpc":
        # rpc 不依赖状态机 api：允许 `python core/supervisor/cli.py rpc` 直接运行。
        return _cli_rpc(args)
    api = _api(api)
    InvalidCheckpoint = api.InvalidCheckpoint
    SupervisorError = api.SupervisorError
    default_coverage_manifest = getattr(api, "default_coverage_manifest", getattr(api, "_default_coverage_manifest", None))
    load_checkpoint_payload = api.load_checkpoint_payload
    load_manifest_units = api.load_manifest_units
    if args.coverage_manifest is None and default_coverage_manifest is not None:
        args.coverage_manifest = default_coverage_manifest(args.root.resolve())
    try:
        supervisor = cli_supervisor(args)
        if args.command == "start":
            trusted_units = load_manifest_units(args.root.resolve(), args.coverage_manifest)
            supplied_units = list(args.coverage_unit)
            if supplied_units and sorted(set(supplied_units)) != trusted_units:
                raise InvalidCheckpoint(
                    "--coverage-unit 必须与受信 coverage manifest 完全一致，不能注入虚假 unit"
                )
            round_id = getattr(args, "round_id", None)
            life_id = getattr(args, "life_id", None)
            if round_id is None and life_id is None:
                try:
                    import core.bugx_paths as bugx_paths
                    scope = bugx_paths.require(args.root.resolve())
                    round_id = scope.lifecycle_root.name
                    life_id = scope.lifecycle_root.name
                except Exception:
                    pass
            value = supervisor.start(
                life_score=args.life_score,
                coverage_units=trusted_units,
                run_id=args.run_id,
                round_id=round_id,
                life_id=life_id,
            )
        elif args.command == "status":
            value = supervisor.status()
        elif args.command == "claim":
            value = supervisor.claim_worker(
                args.worker,
                lease_seconds=args.lease_seconds,
                unit_ids=args.unit or None,
                resource_keys=args.resource_key or None,
                process_pid=getattr(args, "process_pid", None),
                allow_bundle=getattr(args, "allow_bundle", False),
                supersede_worker_id=getattr(args, "supersede_worker", None),
                recover_expired=getattr(args, "recover_expired", False),
                continue_worker=getattr(args, "continue_worker", False),
            )
        elif args.command == "worker-start":
            value = supervisor.start_worker(
                args.worker,
                provider=args.provider,
                execution_id=args.execution_id,
                process_pid=getattr(args, "process_pid", None),
            )
        elif args.command == "heartbeat":
            value = supervisor.heartbeat(
                args.worker,
                lease_seconds=args.lease_seconds,
                heartbeat_nonce=args.heartbeat_nonce,
            )
        elif args.command == "tool-call":
            value = supervisor.record_tool_call(
                args.worker, cost_units=args.cost_units, tool_name=args.tool_name,
            )
        elif args.command == "checkpoint":
            payload = dict(load_checkpoint_payload(args.input))
            checkpoint_id = payload.get("checkpoint_id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
                raise InvalidCheckpoint(
                    "checkpoint_id 必须是非空字符串；重试必须复用同一个 checkpoint_id"
                )
            if args.life_delta is not None:
                payload["life_delta"] = args.life_delta
            value = supervisor.submit_checkpoint(
                args.worker,
                findings=payload.get("findings"),
                coverage=payload.get("coverage"),
                evidence=payload.get("evidence"),
                life_delta=float(payload.get("life_delta", 0.0)),
                signatures=payload.get("signatures"),
                notes=payload.get("notes"),
                suggested_modules=payload.get("suggested_modules"),
                checkpoint_id=payload.get("checkpoint_id"),
            )
        elif args.command == "hints":
            value = incident_hints(supervisor, args.ledger, args.limit)
        elif args.command == "gate":
            value = supervisor.parallel_gate()
            cli_print(value)
            return 0 if value.get("satisfied") else 2
        elif args.command == "next-round":
            value = supervisor.next_round()
        elif args.command == "finish":
            value = supervisor.finish()
        elif args.command == "recover":
            value = supervisor.recover(
                reset_worker_attempts=getattr(args, "reset_worker_attempts", False),
            )
        elif args.command == "findings":
            # 目前唯一写操作是 severity 修复；不带 --fix-severity 时不静默成功，
            # 明确拒绝以免被误当作"只读汇总"而掩盖真实意图。
            if not getattr(args, "fix_severity", False):
                raise InvalidCheckpoint(
                    "findings 目前仅支持 --fix-severity（只读汇总请用 status / coverage）"
                )
            value = supervisor.fix_finding_severity(
                finding=args.finding,
                severity=args.severity,
                reason=getattr(args, "reason", None),
                expected_revision=getattr(args, "expected_revision", None),
            )
        elif args.command == "reset-budget":
            curr_state = supervisor.status()
            target_budget = _cli_merge_budget(args, getattr(curr_state, "budget", None))
            value = supervisor.reset_budget(
                target_budget,
                force=getattr(args, "force", False),
                renew_wall_clock=getattr(args, "renew_wall_clock", False),
                expected_revision=getattr(args, "expected_revision", None),
            )
        elif args.command == "rebind-target":
            value = supervisor.rebind_target(reason=args.reason)
        elif args.command == "repair-fence":
            # BX-ENGINE-22：栅栏与快照失配时**没有任何**读路径可用（连 recover
            # 都需要先读），必须提供不依赖读语义的带外修复入口。
            # D1：重锚必须在 Supervisor 写锁内执行。否则会与在用写者竞争：
            # persist 现在以「栅栏最后写」为提交标记，锁外重锚会用陈旧读取
            # 覆盖掉对方刚提交的栅栏，制造新的失配。
            from core.supervisor import state_store as _state_store
            with supervisor._file_lock():
                revision = _state_store.reanchor_fence(
                    supervisor.state_path, supervisor.backup_path,
                )
            value = {
                "status": "fence_reanchored",
                "revision": revision,
                "state": str(supervisor.state_path),
            }
        elif args.command == "reopen-finding":
            value = supervisor.reopen_finding(
                args.finding_id,
                reason=args.reason,
                expected_revision=args.expected_revision,
            )
        elif args.command == "admit-business":
            value = supervisor.admit_business_unit(
                args.unit_id,
                risk_score=args.risk_score,
                reason=args.reason,
                expected_revision=args.expected_revision,
            )
        else:
            value = supervisor.fail(args.reason)
        cli_print(value)
        return 0
    except (SupervisorError, ValueError, OSError) as exc:
        print(f"[audit_supervisor] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
