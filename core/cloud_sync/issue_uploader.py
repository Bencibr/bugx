#!/usr/bin/env python3
"""BugX Issue & Session Diagnostics Uploader.

Extracts session history from .bugx/sessions/, captures execution logs and
environment fingerprint, applies 3-level sensitive data redaction, and uploads
the diagnostic bundle via Gzip to BugX Server.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.cloud_sync.device_identity import get_device_id
from core.cloud_sync.endpoint_router import configured_base_url, select_lowest_latency_endpoint
from core.cloud_sync.sensitive_filter import redact_diagnostics_payload, redact_sensitive_text
from core.infra.client_metadata import client_version, tls_context

logger = logging.getLogger(__name__)

ERROR_PATTERNS = re.compile(
    r"(?i)\b(?:error|exception|fail(?:ed|ure)?|traceback|panic|segfault|crash)\b"
)


def find_latest_error_session(project_root: Path) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """智能定位最近一次包含错误标记的会话；若无错误会话则回退到最近一次会话。

    返回: (session_id, session_data_dict, extracted_error_snippet)
    """
    sessions_dir = project_root / ".bugx" / "sessions"
    if not sessions_dir.is_dir():
        return None, None, None

    candidate_files = []
    for p in sessions_dir.glob("*.json"):
        if p.name in {"index.json", "s1.json"}:
            continue
        try:
            stat = p.stat()
            candidate_files.append((stat.st_mtime, p))
        except OSError:
            continue

    if not candidate_files:
        return None, None, None

    # 按修改时间从新到旧排序
    candidate_files.sort(key=lambda x: x[0], reverse=True)

    fallback_session_id: str | None = None
    fallback_data: dict[str, Any] | None = None

    for _, path in candidate_files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(content)
        except Exception:
            continue

        sid = data.get("session_id") or data.get("id") or path.stem
        if not fallback_session_id:
            fallback_session_id = sid
            fallback_data = data

        # 检查 messages 中是否存在错误信息
        messages = data.get("messages") or []
        for msg in reversed(messages):
            text = msg.get("content") or ""
            parts = msg.get("parts") or []
            for part in parts:
                if isinstance(part, dict) and "text" in part:
                    text += "\n" + str(part["text"])

            if ERROR_PATTERNS.search(text):
                # 提取首行或核心错误片段作为摘要
                error_snippet = ""
                for line in text.splitlines():
                    sline = line.strip()
                    if sline and ERROR_PATTERNS.search(sline):
                        error_snippet = sline[:200]
                        break
                if not error_snippet and text.strip():
                    error_snippet = text.strip().splitlines()[0][:200]
                return sid, data, error_snippet

    return fallback_session_id, fallback_data, None


def collect_environment_context() -> dict[str, Any]:
    """收集 OS、Python、Node、Git 与宿主环境上下文元数据。"""
    env: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_arch": platform.machine(),
        "python_version": platform.python_version(),
        "bugx_version": client_version(),
        "device_id": get_device_id(),
        "host": os.environ.get("BUGX_HOST", "cli"),
        "node_version": None,
        "git_commit": None,
    }

    # 尝试获取 Node 版本
    try:
        proc = subprocess.run(
            ["node", "-v"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            env["node_version"] = proc.stdout.strip()
    except Exception:
        pass

    # 尝试获取 Git Commit
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            env["git_commit"] = proc.stdout.strip()
    except Exception:
        pass

    return env


def collect_recent_logs(project_root: Path, max_lines: int = 500) -> list[str]:
    """读取最近的运行态核心日志切片。"""
    log_candidates = [
        project_root / ".bugx" / "logs" / "bugx.jsonl",
        project_root / ".bugx" / "logs" / "audit.log",
    ]

    for log_path in log_candidates:
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                return lines[-max_lines:]
            except Exception:
                continue

    return []


def build_issue_payload(
    project_root: Path,
    title: str,
    desc: str = "",
    session_id: str | None = None,
    include_logs: bool = True,
    mask_source: bool = False,
    anonymous: bool = False,
    attached_fingerprint: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """装配、脱敏并构建问题上报请求 Payload。

    返回: (redacted_payload, redacted_categories)
    """
    env = collect_environment_context()
    session_data: dict[str, Any] | None = None
    extracted_error: str | None = None
    sid = session_id

    if sid:
        sess_file = project_root / ".bugx" / "sessions" / f"{sid}.json"
        if sess_file.is_file():
            try:
                session_data = json.loads(sess_file.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                session_data = None
    else:
        found_sid, found_data, found_err = find_latest_error_session(project_root)
        sid = found_sid
        session_data = found_data
        extracted_error = found_err

    # 关联读取事件流（若存在）
    events: list[dict[str, Any]] = []
    if sid:
        events_file = project_root / ".bugx" / "sessions" / f"{sid}.events.jsonl"
        if events_file.is_file():
            try:
                for line in events_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip():
                        events.append(json.loads(line))
            except Exception:
                pass

    logs = collect_recent_logs(project_root) if include_logs else []

    error_type = "SessionAnomaly"
    error_message = extracted_error or ""
    if not title:
        title = error_message or "Agent 会话异常诊断上报"

    payload: dict[str, Any] = {
        "device_id": "[ANONYMOUS]" if anonymous else env["device_id"],
        "client_version": env["bugx_version"],
        "host": env["host"],
        "title": title,
        "description": desc,
        "error_type": error_type,
        "error_message": error_message,
        "environment": env,
        "session_id": sid or "",
        "session_data": {
            "session": session_data,
            "events": events[-100:] if events else [],
        },
        "logs_slice": logs,
    }
    if attached_fingerprint:
        payload["attached_fingerprint"] = attached_fingerprint

    # 执行三级深度脱敏
    redacted_payload, categories = redact_diagnostics_payload(payload, mask_source=mask_source)
    if attached_fingerprint:
        redacted_payload["attached_fingerprint"] = attached_fingerprint
    return redacted_payload, categories


def prepare_issue_gzip_package(payload: dict[str, Any]) -> bytes:
    """序列化并使用 Gzip 压缩 Payload。"""
    raw_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return gzip.compress(raw_json)


def upload_issue(
    payload: dict[str, Any],
    api_url: str | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """将脱敏后的问题现场数据 Gzip 压缩后上传至 BugX 服务端。

    端点: POST /api/v1/agent/issues
    """
    base_url = api_url or configured_base_url() or select_lowest_latency_endpoint() or "https://bugx.dev"
    endpoint = f"{base_url.rstrip('/')}/api/v1/agent/issues"

    # 若未指定 token，尝试从默认凭据文件读取
    if not token and payload.get("device_id") != "[ANONYMOUS]":
        try:
            cred_path = Path(os.environ.get("BUGX_CLOUD_CREDENTIALS", str(Path.home() / ".bugx" / "cloud-credentials.json")))
            if cred_path.is_file():
                creds = json.loads(cred_path.read_text(encoding="utf-8"))
                token = creds.get("token") or creds.get("access_token")
        except Exception:
            pass

    compressed_body = prepare_issue_gzip_package(payload)

    headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "Accept": "application/json",
        "X-BugX-Client-Version": str(payload.get("client_version") or client_version()),
        "X-BugX-Device-ID": str(payload.get("device_id") or ""),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        endpoint,
        data=compressed_body,
        headers=headers,
        method="POST",
    )

    ctx = tls_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            resp_bytes = resp.read()
            data = json.loads(resp_bytes.decode("utf-8", errors="replace"))
            return data
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message") or err_json.get("error") or err_body
        except Exception:
            msg = err_body
        raise RuntimeError(f"服务端拒绝上报 (HTTP {err.code}): {msg}") from err
    except Exception as err:
        raise RuntimeError(f"网络连接或上报失败: {err}") from err


_NON_FATAL_EXCEPTIONS = (
    KeyboardInterrupt,
    EOFError,
    SystemExit,
)


def is_fatal_internal_defect(exc: BaseException) -> bool:
    """严格判定异常是否属于系统内部未预期缺陷。

    排查准则：
    1. 用户中断、正常退出、命令行输入错误（如 argparse 参数解析错误）一律排除；
    2. 明确的内部缺陷（AssertionError, KeyError, IndexError, TypeError, AttributeError,
       UnboundLocalError, MemoryError, NameError, ZeroDivisionError, RecursionError）属于致命缺陷；
    3. 顶层未捕获的通用 Exception/RuntimeError 属于缺陷，但若是由于用户取消或已知环境阻断则排除。
    """
    if isinstance(exc, _NON_FATAL_EXCEPTIONS):
        return False
    exc_name = type(exc).__name__
    if any(k in exc_name for k in ("UsageError", "ArgumentError", "Help", "Cancel")):
        return False
    # 明确内部缺陷
    if isinstance(exc, (
        AssertionError, KeyError, IndexError, TypeError, AttributeError,
        UnboundLocalError, MemoryError, NameError, ZeroDivisionError, RecursionError,
    )):
        return True
    # 顶层未捕获的通用异常
    if isinstance(exc, Exception):
        return True
    return False


def normalize_traceback_and_paths(tb_text: str, project_root: Path | None = None) -> str:
    """对堆栈文本中的开发者用户名、主目录及项目绝对路径进行规范化脱敏。"""
    if not tb_text:
        return ""
    # 1. 替换 HOME 路径为 ~
    try:
        home_path = str(Path.home().resolve())
        if home_path and home_path != "/" and home_path in tb_text:
            tb_text = tb_text.replace(home_path, "~")
    except Exception:
        pass

    # 2. 替换项目根目录为 [PROJECT_ROOT]
    if project_root:
        try:
            root_str = str(project_root.resolve())
            if root_str and root_str != "/" and root_str in tb_text:
                tb_text = tb_text.replace(root_str, "[PROJECT_ROOT]")
        except Exception:
            pass

    # 3. 剥离 URL 中的 Basic Auth 密码 (https://user:password@domain -> https://user:[REDACTED]@domain)
    tb_text = re.sub(r"(https?://[^/@:\s]+):[^/@:\s]+(@)", r"\1:[REDACTED]\2", tb_text)
    return tb_text


def compute_crash_fingerprint(exc: BaseException) -> str:
    """计算异常的确定性稳定指纹 (SHA-256 前 10 位 hex，前缀 bk-)。"""
    tb = getattr(exc, "__traceback__", None)
    top_file = "unknown"
    top_func = "unknown"
    line_no = 0
    while tb is not None:
        frame = tb.tb_frame
        code = frame.f_code
        top_file = Path(code.co_filename).name
        top_func = code.co_name
        line_no = tb.tb_lineno
        tb = tb.tb_next
    raw = f"{type(exc).__name__}:{top_file}:{top_func}:{line_no}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"bk-{digest}"


def crashes_cache_dir() -> Path:
    """本地 Crash Outbox 目录 (~/.bugx/cache/crashes/)。"""
    base = Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx"))).expanduser()
    d = base / "cache" / "crashes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reported_crashes_path() -> Path:
    """崩溃上报频控缓存记录文件 (~/.bugx/cache/reported_crashes.json)。"""
    base = Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx"))).expanduser()
    p = base / "cache" / "reported_crashes.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def is_crash_reported_recently(fingerprint: str, ttl_seconds: int = 86400) -> bool:
    """检查指纹是否在指定 TTL（默认 24 小时）内已上报过。"""
    p = reported_crashes_path()
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        last_ts = data.get(fingerprint)
        if isinstance(last_ts, (int, float)):
            return (time.time() - last_ts) < ttl_seconds
    except Exception:
        pass
    return False


def record_crash_reported(fingerprint: str) -> None:
    """记录指纹已上报时间，并清理 7 天前过期记录。"""
    p = reported_crashes_path()
    try:
        data: dict[str, Any] = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
        now = time.time()
        # 清理超过 7 天的历史记录
        data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and now - v < 7 * 86400}
        data[fingerprint] = now
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def dump_crash_outbox(payload: dict[str, Any]) -> Path | None:
    """毫秒级将脱敏后的崩溃 Payload 写入本地 Crash Outbox。"""
    try:
        cdir = crashes_cache_dir()
        fp = payload.get("fingerprint") or f"crash_{int(time.time())}"
        target = cdir / f"{fp}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        return target
    except Exception:
        return None


def auto_report_crash(
    exc: BaseException,
    project_root: Path | None = None,
    command: str | None = None,
    timeout: float = 1.2,
) -> str | None:
    """主动异常上报核心函数：执行错误分类、权限门禁、指纹去重、Outbox 落盘与快速即时上报。"""
    if not is_fatal_internal_defect(exc):
        return None
    from core.cloud_sync.consent import auto_report_enabled
    if not auto_report_enabled():
        return None

    fingerprint = compute_crash_fingerprint(exc)

    # 24 小时去重检查（若已在 24h 内上报过，直接返回指纹，不重复发起网络调用）
    if is_crash_reported_recently(fingerprint):
        return fingerprint

    raw_tb = traceback.format_exc()
    norm_tb = normalize_traceback_and_paths(raw_tb, project_root)
    env = collect_environment_context()
    logs = collect_recent_logs(project_root, max_lines=50) if project_root else []

    title = f"Crash [{type(exc).__name__}]: {str(exc)[:120]}"
    payload: dict[str, Any] = {
        "device_id": env.get("device_id"),
        "client_version": env.get("bugx_version"),
        "host": env.get("host"),
        "title": title,
        "description": f"Command '{command}' triggered internal defect: {type(exc).__name__}",
        "error_type": type(exc).__name__,
        "error_message": str(exc),
        "traceback": norm_tb,
        "fingerprint": fingerprint,
        "command": command or "unknown",
        "is_auto_reported": True,
        "environment": env,
        "logs_slice": logs,
    }

    # 执行三级安全脱敏与源码遮蔽
    redacted_payload, _ = redact_diagnostics_payload(payload, mask_source=True)
    redacted_payload["fingerprint"] = fingerprint  # 保留指纹不被混淆
    redacted_payload["is_auto_reported"] = True

    # 1. 毫秒级写入本地 Crash Outbox (保障进程瞬退不丢包)
    outbox_file = dump_crash_outbox(redacted_payload)

    # 2. 标记指纹已记录（防止风暴）
    record_crash_reported(fingerprint)

    # 3. 尝试受控短超时即时上传 (如 1.2 秒)
    try:
        upload_issue(redacted_payload, timeout=timeout)
        # 即时上传成功，安全清理本地 Outbox
        if outbox_file and outbox_file.is_file():
            try:
                outbox_file.unlink()
            except OSError:
                pass
    except Exception:
        # 网络超时或失败时保持 Outbox，留待下次启动后台 Flush
        pass

    return fingerprint


def flush_pending_crashes(max_count: int = 5, timeout: float = 2.0) -> int:
    """启动时后台静默 Flush 遗留的 Crash Outbox 队列。"""
    from core.cloud_sync.consent import auto_report_enabled
    if not auto_report_enabled():
        return 0

    try:
        cdir = crashes_cache_dir()
        dump_files = list(cdir.glob("*.json"))
    except Exception:
        return 0

    if not dump_files:
        return 0

    flushed = 0
    for p in dump_files[:max_count]:
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            payload = json.loads(content)
            if isinstance(payload, dict):
                upload_issue(payload, timeout=timeout)
                p.unlink()
                flushed += 1
        except Exception:
            continue

    return flushed

