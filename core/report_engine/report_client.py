#!/usr/bin/env python3
"""测试结论匿名上报客户端 —— 网络请求的单一封装点。

设计目标（服务为人：把对外 HTTP 收敛到一处，替换/测试/切换目标都只动这里）：
  - 统一超时、一次性重试与退避、错误归一：任何网络情况都返回结构化
    ReportResult，不向调用方抛裸异常。
  - 一键切换目标：环境变量 BUGX_SHARE_API_BASE / BUGX_SHARE_API_PATH /
    BUGX_SHARE_TIMEOUT / BUGX_SHARE_RETRIES。
  - 可测试：允许注入 urlopen；生产默认使用禁止自动重定向的 urllib opener，单测无需真实网络。
  - 匿名：默认不携带任何认证头。
  - 匿名身份（DOC-008 §11）：run_id 由 scoped installation identity
    （随机 128-bit secret × server_origin+account 的 HMAC）派生，不含
    hostname/machine-id；历史 machine-id 格式 ID 只保留读取语义。

现状（2026-08-28）：后台「运行汇总」匿名端点为 `POST /api/v1/agent/runs`——仅要求
`run_id`，实测无认证返回 201「运行汇总提交成功」，接受 test_duration/tokens_consumed/
major_rounds/minor_rounds/bugs_found/cases_total/project_type/languages/visibility。
本模块 DEFAULT_PATH 即指向它（一次运行一条汇总）。另 `/api/v1/agent/findings/anonymous`
按单条 bug 模型，供需要逐条上报时用。字段约定见 docs/backend-report-gap.md。
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import core.secure_atomic as secure_atomic
from core.infra.client_metadata import client_headers, client_user_agent, client_version, tls_context

# 目标基址与路径；发布包默认使用正式环境，环境变量仍可临时覆盖。
DEFAULT_BASE = os.environ.get("BUGX_SHARE_API_BASE", "https://bugx.dev")
DEFAULT_PATH = os.environ.get(
    "BUGX_SHARE_API_PATH", "/api/v1/agent/runs"
)
DEFAULT_TIMEOUT = float(os.environ.get("BUGX_SHARE_TIMEOUT", "8"))
DEFAULT_RETRIES = int(os.environ.get("BUGX_SHARE_RETRIES", "1"))
REPORT_USER_AGENT = client_user_agent()
_RETRYABLE_HTTP_STATUS = frozenset({429, 503})
#: 上报脱敏模式（2026-11-09 用户口径：**默认不脱敏**）。
#: ``off``   = 原样上传（默认）；``standard`` = 删除机器标识键 + 凭据/绝对路径脱敏。
#: 显式开启：``BUGX_REPORT_REDACTION=standard``（或 1/on/strict）。
REDACTION_MODES = ("off", "standard")
REDACTION_DEFAULT = "off"
_TRUE_REDACTION_TOKENS = frozenset({"1", "on", "true", "yes", "strict", "standard"})
_FALSE_REDACTION_TOKENS = frozenset({"0", "off", "false", "no", "none", "raw", ""})


def resolve_redaction_mode(mode: str | None = None) -> str:
    """归一脱敏模式：显式参数 > ``BUGX_REPORT_REDACTION`` > 默认 off。

    非法值 fail-closed 抛 ``ValueError``——不接受「猜一个」的降级。
    """
    raw = mode
    if raw is None:
        raw = os.environ.get("BUGX_REPORT_REDACTION", REDACTION_DEFAULT)
    value = str(raw).strip().casefold()
    if value in _TRUE_REDACTION_TOKENS:
        return "standard"
    if value in _FALSE_REDACTION_TOKENS:
        return "off"
    raise ValueError(
        f"脱敏模式只能是 {'/'.join(REDACTION_MODES)}，收到 {raw!r}"
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep report requests on the configured origin and scheme."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _build_no_redirect_open(base_url: str | None = None):
    return urllib.request.build_opener(
        _NoRedirectHandler(), urllib.request.HTTPSHandler(context=tls_context(base_url=base_url))
    ).open


_NO_REDIRECT_OPEN = _build_no_redirect_open(DEFAULT_BASE)


def default_report_base_url() -> str:
    """按 BUGX_SHARE_API_BASE > cloud_client.configured_base_url() > DEFAULT_BASE 解析默认分享服务基址。"""
    if "BUGX_SHARE_API_BASE" in os.environ:
        return os.environ["BUGX_SHARE_API_BASE"]
    try:
        from core.cloud_sync.cloud_client import configured_base_url
        return configured_base_url()
    except Exception:
        return DEFAULT_BASE


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("分享服务地址不能为空")
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("分享服务地址必须是带 host 的 http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("分享服务地址不能包含用户名、密码、查询参数或 fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程分享服务必须使用 HTTPS；HTTP 只允许本机测试服务")
    return base_url.rstrip("/")


@dataclass(frozen=True)
class ReportResult:
    """一次网络请求的结构化结果；任何异常都被归一到这里。"""
    ok: bool
    status: int | None
    data: dict | list | None
    error: str | None


class ReportClient:
    """对单个后端基址的封装客户端；默认匿名，可选 Bearer token。"""

    def __init__(
        self,
        base_url: str | None = None,
        path: str | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        token: str | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        self.base = _validate_base_url(base_url or default_report_base_url())
        self.path = path or DEFAULT_PATH
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self.retries = retries if retries is not None else DEFAULT_RETRIES
        self.token = token.strip() if isinstance(token, str) and token.strip() else None
        # The injectable transport is for tests/custom callers; production defaults
        # to the no-redirect opener so Authorization cannot follow another origin.
        self._urlopen = urlopen or _build_no_redirect_open(self.base)

    def _request(
        self, method: str, path: str,
        payload: dict | None = None, params: dict | None = None,
        operation_id: str | None = None,
    ) -> ReportResult:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/json",
            # Cloudflare and the BugX gateway reject requests without a stable
            # client identity.  This is not authentication and carries no
            # machine/account identifier.
            **client_headers(),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if operation_id:
            headers["X-BugX-Operation-ID"] = operation_id
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        last_status: int | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._urlopen(req, timeout=self.timeout)
                try:
                    status = getattr(resp, "status", 200)
                    raw = resp.read() if hasattr(resp, "read") else b""
                finally:
                    resp.close()
                data = parse_response_json(raw)
                code_marker = data.get("code") if isinstance(data, dict) else None
                if operation_id and (status == 409 or code_marker == 409):
                    # 同 ``HTTPError`` 分支：带 operation_id 的 409 = 已存在 = 成功。
                    return ReportResult(
                        ok=True, status=status, data=data, error=None,
                    )
                retryable = (
                    status in _RETRYABLE_HTTP_STATUS
                    or code_marker in _RETRYABLE_HTTP_STATUS
                    or status >= 500
                )
                if retryable and attempt < self.retries:
                    time.sleep(self._retry_delay(data, attempt))
                    continue
                business_error = isinstance(code_marker, int) and code_marker >= 400
                return ReportResult(
                    ok=200 <= status < 300 and not business_error,
                    status=status, data=data,
                    error=message_from_response(
                        raw, ValueError(f"服务端返回业务错误 code={code_marker}")
                    ) if business_error else None,
                )
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                raw = exc.read()
                data = parse_response_json(raw)
                if operation_id and exc.code == 409:
                    # 服务端对同一 ``X-BugX-Operation-ID`` 的重放可能返回 409
                    # （记录已存在）。幂等语义下这就是成功：调用方只需知道
                    # "该 operation 已经落地"，不应把它当成失败重试并产生
                    # 第二条记录（实测 db_id=63/64 重复的根因）。
                    try:
                        exc.close()
                    except Exception:  # noqa: BLE001
                        pass
                    return ReportResult(
                        ok=True, status=exc.code, data=data, error=None,
                    )
                if (exc.code >= 500 or exc.code in _RETRYABLE_HTTP_STATUS) \
                        and attempt < self.retries:
                    try:
                        exc.close()
                    except Exception:  # noqa: BLE001
                        pass
                    retry_after = None
                    try:
                        retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    except AttributeError:
                        pass
                    time.sleep(self._retry_delay(parse_response_json(raw), attempt, retry_after))
                    continue
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
                return ReportResult(
                    ok=False, status=exc.code,
                    data=parse_response_json(raw), error=message_from_response(raw, exc),
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return ReportResult(
                    ok=False, status=None,
                    data={"code": "REPORT_NETWORK_UNAVAILABLE", "message": str(exc)},
                    error=str(exc),
                )
            except ValueError as exc:
                return ReportResult(
                    ok=False, status=None,
                    data={"code": "REPORT_INVALID_RESPONSE", "message": str(exc)},
                    error=str(exc),
                )

        # 理论上不可达（retries 内已返回），兜底
        return ReportResult(ok=False, status=last_status, data=None,
                            error="unknown network failure")

    @staticmethod
    def _retry_delay(data: dict | list | None, attempt: int,
                     retry_after: str | None = None) -> float:
        """Honor a bounded Retry-After value, then use short exponential backoff."""
        candidate: object = retry_after
        if candidate in (None, "") and isinstance(data, dict):
            candidate = data.get("retry_after")
            if candidate in (None, "") and isinstance(data.get("data"), dict):
                candidate = data["data"].get("retry_after")
        try:
            if candidate not in (None, ""):
                return float(min(max(float(candidate), 0.0), 30.0))
        except (TypeError, ValueError):
            pass
        return 0.5 * (attempt + 1)

    def post(self, payload: dict, *, params: dict | None = None,
             operation_id: str | None = None) -> ReportResult:
        """匿名 POST JSON 到本客户端路径。"""
        return self._request(
            "POST", self.path, payload=payload, params=params,
            operation_id=operation_id,
        )

    def get(self, path: str = "", *, params: dict | None = None) -> ReportResult:
        """匿名 GET（用于公开排行榜读取等）。"""
        return self._request("GET", path, params=params)


def installation_secret_path(home: str | os.PathLike[str] | None = None) -> Path:
    """128-bit installation secret 的固定落盘位置（0600）。"""
    root = Path(home) if home is not None else Path.home()
    return root / ".bugx" / "identity" / "installation-secret"


def atomic_write_nofollow(path: Path, data: bytes, *, mode: int = 0o600,
                           parent_mode: int | None = None) -> None:
    """Atomically write ``data`` to ``path`` without following symlinks.

    Rejects a symlinked target or immediate parent directory, and pins the
    parent directory with ``O_NOFOLLOW`` so a concurrent swap of the parent
    path cannot redirect the write (parent-directory TOCTOU defense).  Raises
    ``ValueError`` for symlink rejection and ``OSError`` for I/O failures.
    """
    try:
        secure_atomic.atomic_write_bytes(path, data, mode=mode, parent_mode=parent_mode)
    except secure_atomic.AtomicWriteError as exc:
        raise ValueError(str(exc)) from exc


def load_installation_secret(*, home: str | os.PathLike[str] | None = None,
                             create: bool = True) -> bytes:
    """读取（或首次生成）随机 128-bit installation secret。

    DOC-008 §11：首次安装生成随机 secret，权限 0600；拒绝符号链接，
    不复用旧 machine fingerprint。内容是 32 位 hex。
    """
    path = installation_secret_path(home)
    if path.is_symlink():
        raise ValueError(f"installation secret 不能是符号链接: {path}")
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", raw):
            raise ValueError("installation secret 内容无效（需要 32 位 hex）")
        return bytes.fromhex(raw)
    if not create:
        raise ValueError(f"installation secret 尚未生成: {path}")
    parent = path.parent
    if parent.is_symlink():
        raise ValueError(f"identity 目录不能是符号链接: {parent}")
    atomic_write_nofollow(path, secrets.token_hex(16).encode("ascii"),
                           parent_mode=0o700)
    return load_installation_secret(home=home, create=False)


def _normalize_origin(server_origin: str) -> str:
    parsed = urllib.parse.urlsplit(server_origin)
    if not parsed.netloc:
        raise ValueError(f"server_origin 不是合法 URL: {server_origin}")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def installation_identity(server_origin: str | None = None, account_id: str = "",
                          *, secret: bytes | None = None,
                          home: str | os.PathLike[str] | None = None) -> str:
    """针对 server_origin + account 派生假名 installation ID。

    HMAC(secret, origin‖account) → 同一安装对不同服务端/账号呈现不同稳定 ID；
    payload 与日志都不含 hostname、machine-id 或跨账号稳定标识（AD-008-07）。
    """
    scoped_secret = secret if secret is not None else load_installation_secret(home=home)
    origin = _normalize_origin(server_origin or DEFAULT_BASE)
    digest = hmac.new(
        scoped_secret,
        f"bugx-installation\x00{origin}\x00{account_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"bi_{digest[:32]}"


def compute_run_id(
    *,
    polling_id: str = "",
    lifecycle_id: str = "",
    seed: str = "",
    server_origin: str | None = None,
    account_id: str = "",
    installation: str | None = None,
    secret: bytes | None = None,
    home: str | os.PathLike[str] | None = None,
) -> str:
    """计算幂等的匿名 run ID（DOC-008 §11：scoped ID + lifecycle 身份）。

    构成 = HMAC(scoped installation ID, 运行稳定身份)，前缀 ``r-``，不含任何
    主机名/machine-id 前缀。同一逻辑运行（固定 polling/lifecycle/seed）重试得到
    相同 run_id 供后端去重；不同安装（secret 不同）或不同 server/account scope
    得到不同 ID，且无法互相关联。历史 machine-id 格式 ID 只保留读取语义，
    新上传不再产生。
    """
    if installation is None:
        installation = installation_identity(
            server_origin or DEFAULT_BASE, account_id, secret=secret, home=home,
        )
    raw = "\0".join([polling_id, lifecycle_id, seed])
    digest = hmac.new(installation.encode("utf-8"), raw.encode("utf-8"),
                      hashlib.sha256).hexdigest()[:16]
    return f"r-{digest}"


# ---- 匿名负载净化与上传预览（DOC-008 §11 / TO-008-021） ----

_FORBIDDEN_KEY_NAMES = frozenset({
    "hostname", "machine_id", "machine-id", "machineserial",
    "device_name", "computername", "mac_address", "serial_number",
    "username", "user_name", "home", "homedir", "cwd", "abs_path", "abspath",
    # 凭据类键名（2026-11-09 E7：值是真实凭据时键+值一并删）
    "api_key", "apikey", "secret", "secret_key", "access_token",
    "refresh_token", "password", "passwd", "authorization", "cookie",
})
_ABS_PATH_RE = re.compile(r"/(?:Users|home|private/var|var/folders|tmp)/[^\s\"'`,;)\]]*")
_CREDENTIAL_RE = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"ak_live_[A-Za-z0-9_-]{10,}|whsec_[A-Za-z0-9_-]{10,}|"
    r"Bearer\s+[^\s\"',]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:password|passwd|secret|token|api[_ -]?key|authorization|cookie)\s*[:=]\s*\S+)",
    re.I,
)


def _redaction_token(value: str) -> str:
    return "[redacted:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8] + "]"


def _sanitize_text_line_wise(value: str) -> str:
    """长文本（报告正文等）逐行脱敏：命中行只抹 ``key: value`` 的 value 部分。

    实测缺陷（2026-11-09 复盘 E7）：整串替换曾把 2021 字符的报告正文替换成
    26 字符的 ``[redacted:hash]``——云端交付物凭空丢失。逐行保留上下文；
    ``Worker 自报 Token: `0`` 这类纯数字指标行不视作凭据（数值白名单）。
    """
    numeric_metric = re.compile(
        r"((?:token|tokens|自行报|自报)[^:]*:\s*)`?(\d+)`?", re.I,
    )
    lines = []
    for line in value.splitlines():
        if numeric_metric.search(line) and not re.search(
            r"(gh[pousr]_|sk-|AKIA|BEGIN [A-Z ]*PRIVATE KEY|ak_live)", line, re.I,
        ):
            lines.append(line)
            continue
        if _CREDENTIAL_RE.search(line):
            line = _CREDENTIAL_RE.sub("[redacted]", line)
        lines.append(line)
    return "\n".join(lines)


def sanitize_anonymous_payload(
    payload: dict, *, redaction: str | None = None,
) -> tuple[dict, list[dict[str, str]]]:
    """净化匿名负载：默认**原样**返回；``redaction="standard"`` 才脱敏。

    2026-11-09 用户口径：上报默认不脱敏（此前 long-report 凭据正则/路径规则
    会把报告正文大面积改写，云端交付物失真）。``standard`` 模式保留原有
    行为：删除机器标识键、脱敏凭据与绝对路径。

    返回 (净化后的 payload, redaction 清单)。清单只含 JSON 路径和原因类别，
    本身不携带被删改的内容，可安全写日志（§11：日志只记录结果与计数）。
    """
    mode = resolve_redaction_mode(redaction)
    if mode == "off":
        return json.loads(json.dumps(payload)), []
    redactions: list[dict[str, str]] = []

    def clean(value: Any, pointer: str) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                child = f"{pointer}.{key}"
                if str(key).lower() in _FORBIDDEN_KEY_NAMES:
                    redactions.append({"path": child, "reason": "machine-identity-key"})
                    continue
                cleaned[key] = clean(item, child)
            return cleaned
        if isinstance(value, list):
            return [clean(item, f"{pointer}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, str):
            # 长文（报告正文/结论）：逐行脱敏保留上下文（2026-11-09 E7：
            # 整串替换曾吞掉整份报告）。行内数值指标白名单先于凭据正则。
            if len(value) >= 512:
                sanitized = _sanitize_text_line_wise(value)
                if sanitized != value:
                    redactions.append({"path": pointer, "reason": "credential-like-value"})
                return sanitized
            if _CREDENTIAL_RE.search(value):
                redactions.append({"path": pointer, "reason": "credential-like-value"})
                return _redaction_token(value)
            if _ABS_PATH_RE.search(value):
                redactions.append({"path": pointer, "reason": "absolute-path"})
                return _ABS_PATH_RE.sub("[redacted:path]", value)
            return value
        return value

    return clean(json.loads(json.dumps(payload)), "$"), redactions


# 字段类别映射：预览按类别汇总字节与脱敏情况
_FIELD_CATEGORY: dict[str, str] = {
    "run_id": "identifier",
    "project_name": "label",
    "test_duration": "metric", "tokens_consumed": "metric",
    "major_rounds": "metric", "minor_rounds": "metric",
    "bugs_found": "metric", "cases_total": "metric",
    "project_type": "classification", "languages": "classification",
    "host": "classification",
    "visibility": "state", "environment": "environment",
    "report_markdown": "content", "conclusion_json": "content",
    "integrity_json": "security", "artifacts_json": "content",
    "report_hash": "security", "polling_id": "identifier",
    "lifecycle_id": "identifier", "project_id": "identifier",
}


def _field_category(name: str) -> str:
    return _FIELD_CATEGORY.get(name, "content")


def preview_report_payload(payload: dict, *, base_url: str | None = None,
                           path: str | None = None,
                           redaction: str | None = None) -> dict[str, Any]:
    """`bugx report preview --json` 的库入口：发网前展示将上报什么。

    输出目标 endpoint/scope、字段类别、字节数、脱敏数量与净化后的最终
    payload；不发起任何网络请求，也不修改调用方传入的对象。``redaction``
    默认跟随 ``resolve_redaction_mode``（默认 off = 原样上报）。
    """
    target_base = _validate_base_url(base_url or default_report_base_url())
    endpoint = target_base + (path or DEFAULT_PATH)
    mode = resolve_redaction_mode(redaction)
    sanitized, redactions = sanitize_anonymous_payload(payload, redaction=mode)
    fields: list[dict[str, Any]] = []
    categories: dict[str, int] = {}
    total_bytes = 0
    for name in sorted(sanitized):
        value = sanitized[name]
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        category = _field_category(name)
        fields.append({"name": name, "category": category, "bytes": size})
        categories[category] = categories.get(category, 0) + size
        total_bytes += size
    return {
        "target": {
            "endpoint": endpoint,
            "scope": "anonymous",
            "auth": "none",
        },
        "redaction_mode": mode,
        "field_count": len(fields),
        "total_bytes": total_bytes,
        "categories": dict(sorted(categories.items())),
        "fields": fields,
        "redactions": redactions,
        "redaction_count": len(redactions),
        "payload": sanitized,
    }


def build_anonymous_finding_payload(
    *,
    finding_id: str,
    title: str,
    root_cause: str,
    severity: str,
    project_name: str,
    run_id: str,
    test_duration: int,
    tokens_consumed: int,
    major_rounds: int,
    minor_rounds: int,
    bugs_found: int,
    confidence: float | None = None,
    reproducible: bool | None = None,
    reproduction: str | None = None,
    evidence: str | None = None,
    fingerprint: str | None = None,
    tags: str | None = None,
    test_type: str | None = None,
    lifecycle_state: str | None = None,
    cases_total: int | None = None,
    project_type: str | None = None,
    languages: list[str] | str | None = None,
) -> dict[str, Any]:
    """构造 `POST /api/v1/agent/findings/anonymous` 的匿名 finding 负载。

    该端点按「单条 finding」建模：每确认一条 bug 发一条，并在同一条上携带本次
    运行的**核心指标**（test_duration/tokens_consumed/major_rounds/minor_rounds/
    bugs_found/cases_total/project_type/languages）。必填：
    finding_id/title/root_cause/severity；其余可选按语义填，None 不写入（避免编造）。
    visibility 默认固定为 private；当前版本不允许公共运行报告发布。
    languages 传串则原样、传列表则逗号连接
    （后端接受逗号分隔字符串）。
    """
    payload: dict[str, Any] = {
        "finding_id": finding_id,
        "title": title,
        "root_cause": root_cause,
        "severity": severity,
        "project_name": project_name,
        "run_id": run_id,
        "test_duration": int(test_duration),
        "tokens_consumed": int(tokens_consumed),
        "major_rounds": int(major_rounds),
        "minor_rounds": int(minor_rounds),
        "bugs_found": int(bugs_found),
    }
    if isinstance(languages, (list, tuple)):
        languages = ",".join(str(item) for item in languages)
    optional = {
        "confidence": confidence, "reproducible": reproducible,
        "reproduction": reproduction, "evidence": evidence,
        "fingerprint": fingerprint, "tags": tags,
        "test_type": test_type, "lifecycle_state": lifecycle_state,
        "cases_total": None if cases_total is None else int(cases_total),
        "project_type": project_type, "languages": languages,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    return payload


def build_run_conclusion_payload(
    *,
    run_id: str,
    project_name: str | None = None,
    test_duration: int | None = None,
    survival_time: int | None = None,
    tokens_consumed: int | None = None,
    major_rounds: int | None = None,
    minor_rounds: int | None = None,
    life_total: int | None = None,
    bugs_found: int | None = None,
    fixes_total: int | None = None,
    modules_count: int | None = None,
    features_count: int | None = None,
    lessons_count: int | None = None,
    incidents_count: int | None = None,
    lessons_hit: int | None = None,
    incidents_hit: int | None = None,
    cases_total: int | None = None,
    cases_covered: int | None = None,
    cases_passed: int | None = None,
    cases_failed: int | None = None,
    cases_blocked: int | None = None,
    claims_count: int | None = None,
    status: str | None = None,
    result: str | None = None,
    code_volume: int | None = None,
    project_type: str | None = None,
    host: str | None = None,
    languages: list[str] | str | None = None,
    visibility: str = "private",
    environment: str | dict | None = None,
    report_markdown: str | None = None,
    conclusion_json: str | dict | None = None,
    integrity_json: str | dict | None = None,
    artifacts_json: str | dict | None = None,
    report_hash: str | None = None,
    polling_id: str | None = None,
    lifecycle_id: str | None = None,
    project_id: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造 `POST /api/v1/agent/runs` 的运行汇总负载（一次运行一条汇总）。

    对应后端 requests.RunSummaryRequest：run_id 必填，其余可选；None 不写入
    （不编造）。languages 传列表则逗号连接、传字符串原样；visibility 默认 private。
    environment 为独立的系统环境信息（os/arch/python/host/…），str 原样、
    dict 序列化为 JSON 字符串。
    """
    if visibility != "private":
        raise ValueError("公共运行报告发布已禁用；visibility 必须为 private")
    if isinstance(languages, (list, tuple)):
        languages = ",".join(str(item) for item in languages)
    payload: dict[str, Any] = {"run_id": run_id, "visibility": visibility}
    optional = {
        "project_name": project_name,
        "test_duration": None if test_duration is None else int(test_duration),
        "survival_time": None if survival_time is None else int(survival_time),
        "tokens_consumed": None if tokens_consumed is None else int(tokens_consumed),
        "major_rounds": None if major_rounds is None else int(major_rounds),
        "minor_rounds": None if minor_rounds is None else int(minor_rounds),
        "life_total": None if life_total is None else int(life_total),
        "bugs_found": None if bugs_found is None else int(bugs_found),
        "fixes_total": None if fixes_total is None else int(fixes_total),
        "modules_count": None if modules_count is None else int(modules_count),
        "features_count": None if features_count is None else int(features_count),
        "lessons_count": None if lessons_count is None else int(lessons_count),
        "incidents_count": None if incidents_count is None else int(incidents_count),
        "lessons_hit": None if lessons_hit is None else int(lessons_hit),
        "incidents_hit": None if incidents_hit is None else int(incidents_hit),
        "cases_total": None if cases_total is None else int(cases_total),
        "cases_covered": None if cases_covered is None else int(cases_covered),
        "cases_passed": None if cases_passed is None else int(cases_passed),
        "cases_failed": None if cases_failed is None else int(cases_failed),
        "cases_blocked": None if cases_blocked is None else int(cases_blocked),
        "claims_count": None if claims_count is None else int(claims_count),
        "status": status,
        "result": result,
        "code_volume": None if code_volume is None else int(code_volume),
        "project_type": project_type,
        "host": host,
        "languages": languages,
        "environment": _env_to_str(environment),
        "report_markdown": report_markdown,
        "conclusion_json": _env_to_str(conclusion_json) if isinstance(conclusion_json, dict) else conclusion_json,
        "integrity_json": _env_to_str(integrity_json) if isinstance(integrity_json, dict) else integrity_json,
        "artifacts_json": _env_to_str(artifacts_json) if isinstance(artifacts_json, dict) else artifacts_json,
        "report_hash": report_hash,
        "polling_id": polling_id,
        "lifecycle_id": lifecycle_id,
        "project_id": None if project_id is None else int(project_id),
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    return payload


def _env_to_str(environment: str | dict | None) -> str | None:
    """environment：str 原样；dict 序列化为 JSON 字符串；None 保留 None。"""
    if environment is None:
        return None
    if isinstance(environment, dict):
        try:
            return json.dumps(environment, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return None
    return str(environment)


def parse_response_json(raw: bytes) -> dict | list | None:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def message_from_response(raw: bytes, exc: Exception) -> str | None:
    """从 HTTPError 的 body 提取后端 message（若有），否则用异常文本。"""
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return str(exc)
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or exc)
    return str(exc)
