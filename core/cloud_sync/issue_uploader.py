#!/usr/bin/env python3
"""BugX Issue & Session Diagnostics Uploader.

Extracts session history from .bugx/sessions/, captures execution logs and
environment fingerprint, applies 3-level sensitive data redaction, and uploads
the diagnostic bundle via Gzip to BugX Server.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import platform
import re
import subprocess
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

    # 执行三级深度脱敏
    redacted_payload, categories = redact_diagnostics_payload(payload, mask_source=mask_source)
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
