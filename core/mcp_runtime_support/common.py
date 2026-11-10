"""Shared constants, exceptions, and serialization helpers for MCP runtime."""

from __future__ import annotations

import datetime
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

from core.retrieval.symbol_index import file_sha256 as sha256_file

from core.retrieval.symbol_index import file_sha256 as sha256_file  # noqa: E402

LOCK_SCHEMA_VERSION = 4
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "bugx-mcp-runtime"
MANIFEST_NAME = "runtime-manifest.json"
FILEHASH_NAME = "file-hashes.json"
HISTORY_NAME = "history.json"
CURRENT_LINK = "current"
STAGING_PREFIX = ".staging-"
DEFAULT_MIN_NODE = (18, 0, 0)
WORLD_WRITE_BITS = 0o002
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
BYTECODE_GUARD_NAME = "sitecustomize.py"
INTEGRITY_RE = re.compile(r"^sha512-[A-Za-z0-9+/]+={0,3}$")
SHA256_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)")
TREE_HASH_EXCLUDE = frozenset({MANIFEST_NAME, FILEHASH_NAME})
MAX_REPORTED_MISMATCHES = 20
HISTORY_LIMIT = 20
NPM_TIMEOUT_SECONDS = 900
# 实测：默认 30s 在冷机上连 `npm install playwright` 都跑不完（真实安装被超时切断，
# 留下 .install.lock/.staging 残骸），"一键装齐"必然失败。安装是显式用户动作且已有
# 进度输出，用 15 分钟上限换取真正可用的冷装；仍可由 --timeout 覆盖。
INSTALL_LOCK_NAME = ".install.lock"
INSTALL_LOCK_TIMEOUT_SECONDS = 30.0
INSTALL_LOCK_POLL_INTERVAL = 0.1

# 生态（DOC-235）：lock v3 的包键带 ``<ecosystem>:`` 前缀，杜绝同名歧义。
ECOSYSTEM_NPM = "npm"
ECOSYSTEM_PYPI = "pypi"
ECOSYSTEMS = (ECOSYSTEM_NPM, ECOSYSTEM_PYPI)
PACKAGE_KEY_RE = re.compile(r"^(npm|pypi):([^\s:]+)$")
PYPI_SECTION = "pypi"
PYPI_ENVIRONMENTS = "environments"
# v4（DOC-235 §5.2）：**每个 pypi server 一个独立 venv**。原因（实测）：
# pypi 是单一扁平依赖环境，而 npm 是嵌套解析 ⇒ 两个 pypi MCP 的 pin 冲突不可解
# （semgrep-mcp 死锁 mcp==1.12.2 vs redis-mcp-server 要求 mcp>=1.26,<2）。
# 故 lock 的 pypi 段按**环境**分组：pypi.environments.<env>.packages，
# server 通过 "venv" 字段引用环境（缺省 = server 名），venv 目录为 venv/<env>/。
VENV_DIRNAME = "venv"
PYPI_REQUIREMENTS_NAME = "pypi-requirements.txt"
# pypi 安装的“禁任意代码执行”等价物（DOC-235 §2.3）：
#   --only-binary=:all: 只接受 wheel，绝不构建 sdist（无 setup.py / PEP-517 build hook）
#   --no-deps           不做任何额外解析，装什么完全由 lock 决定
#   --require-hashes    sha256 强校验，且**所有**依赖都必须带 hash
PYPI_INSTALL_FLAGS = (
    "--only-binary=:all:", "--no-deps", "--require-hashes",
    "--no-input", "--disable-pip-version-check",
)
# python_requires 支持的最小子集（fail-closed：解析不了就拒绝）。
PYTHON_REQUIRES_OPERATORS = (">=", "<=", "==", "!=", ">", "<")

LAUNCH_ENV_OVERRIDES = {
    # 运行时树必须**使用期不可变**（verify 以 tree_hash 为准）。
    # 实测：在 venv 里跑一次 `python -c "import semgrep"` 就会写入
    # `site-packages/__pycache__/*.pyc`，随即 verify 报 "多出的未登记文件" +
    # tree_hash 漂移，且 already-installed 路径也会因 verify 失败而拒绝自愈。
    # 由启动器强制关闭字节码落盘（所有宿主都经 `bugx mcp-runtime start` 启动）。
    "PYTHONDONTWRITEBYTECODE": "1",
    "NO_UPDATE_NOTIFIER": "1",
    "npm_config_update_notifier": "false",
    "npm_config_fund": "false",
    "npm_config_audit": "false",
    # semgrep-mcp 实测**默认开启** OTLP 遥测（telemetry.semgrep.dev）并向 semgrep CLI
    # 传 ``--trace``（上报计数，非源码；见 DOC-235 §5）。按宪法"**控制面闸门而非日志
    # 告警**"在启动环境**强制关闭**（硬控制，可观测）：
    #   SEMGREP_MCP_DISABLE_TRACING=true → 不建 exporter，且给 CLI 传 --no-trace
    #   SEMGREP_SEND_METRICS=off        → 关闭 semgrep CLI 自身 metrics 上报
    "SEMGREP_MCP_DISABLE_TRACING": "true",
    # semgrep 自身会定期查"是否有新版本"（联网 + 提示），离线 runtime 必须关掉：
    # 我们的版本由 lock 决定，不允许被上游"建议升级"改写行为。
    "SEMGREP_ENABLE_VERSION_CHECK": "0",
    "SEMGREP_SEND_METRICS": "off",
    # semgrep `mcp` 子命令（≥1.176）实测**默认启用 Datadog APM tracing**：stderr 会打印
    # `Tracing initialized with trace ID: … (https://app.datadoghq.com/apm/trace/…)`。
    # 按 DOC-234「零遥测」硬门禁，在启动环境**强制关闭**（实测关闭后无 tracing 输出）：
    "DD_TRACE_ENABLED": "false",
    "OTEL_SDK_DISABLED": "true",
    # mobile-mcp 默认向 PostHog / Scarf 上报匿名遥测（README 明示）；
    # 离线 runtime 一律硬关闭，不依赖调用方记得设置。
    "MOBILEMCP_DISABLE_TELEMETRY": "1",
}


class McpRuntimeError(RuntimeError):
    """MCP runtime 操作失败；消息面向 doctor 与调用方，fail-closed。"""


def _is_world_writable(mode: int) -> bool:
    """Only POSIX exposes the permission bits used by this check."""
    return os.name != "nt" and bool(mode & WORLD_WRITE_BITS)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def reject_symlink_components(path: Path) -> Path:
    """拒绝路径在新创建组件的位置穿过符号链接。

    只从最深的「已存在且非符号链接」祖先向下检查：macOS 的 /var 等系统级
    符号链接位于任何可写目标之上，不属于攻击面；而用户可写区域内的
    符号链接（如被替换的 ~/.bugx、runtime 内部跳板）一律 fail-closed。
    """
    absolute = Path(os.path.abspath(str(path)))
    if absolute.is_symlink():
        raise McpRuntimeError(f"拒绝通过符号链接访问: {path}")
    boundary = absolute.parent
    while boundary != boundary.parent and (
        not boundary.exists() or boundary.is_symlink()
    ):
        boundary = boundary.parent
    current = boundary
    for part in absolute.relative_to(boundary).parts:
        current = current / part
        if current.is_symlink():
            raise McpRuntimeError(f"拒绝通过符号链接访问: {path}")
    return absolute


def read_json_file(path: Path, label: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise McpRuntimeError(f"{label} 缺失或不是安全的普通文件: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpRuntimeError(f"{label} 无法解析: {path}: {exc}") from exc


def _fsync_directory(parent: Path) -> None:
    """os.replace 后同步目录项（与 secure_atomic 同型，P3-6）。

    只 fsync 文件本身时，崩溃后 rename 可能尚未落盘，新文件名会丢失；
    目录 fsync 把目录条目变更刷盘。非 POSIX 平台或不受支持的文件系统
    上目录 fd 打开/同步可能失败，此处按尽力而为处理。
    """
    try:
        dirfd = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except OSError:
        pass
    finally:
        os.close(dirfd)


def write_json_atomic(path: Path, payload: object, mode: int) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# lock 解析与哈希
# ---------------------------------------------------------------------------




def parse_python_requires(spec: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """解析 ``python_requires``（支持 ``>=``/``>``/``<=``/``<``/``==``/``!=`` 逗号列表）。

    **fail-closed**：任何无法解析的片段直接抛 ``McpRuntimeError``（DOC-235 设计项 3）。
    """
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw in str(spec).split(","):
        clause = raw.strip()
        if not clause:
            continue
        operator = ""
        for candidate in PYTHON_REQUIRES_OPERATORS:
            if clause.startswith(candidate):
                operator = candidate
                break
        if not operator:
            raise McpRuntimeError(f"python_requires 片段缺少合法运算符: {clause!r}")
        version = clause[len(operator):].strip()
        parts = version.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            raise McpRuntimeError(f"python_requires 版本号非法: {version!r}")
        clauses.append((operator, tuple(int(part) for part in parts)))
    if not clauses:
        raise McpRuntimeError("python_requires 不能为空")
    return tuple(clauses)


def check_python_requires(
    spec: str, version: tuple[int, ...] | None = None,
) -> tuple[bool, str]:
    """校验解释器版本是否满足 ``python_requires``；返回 (是否满足, 说明)。"""
    clauses = parse_python_requires(spec)
    current = tuple(version) if version is not None else tuple(sys.version_info[:3])
    shown = ".".join(str(item) for item in current)
    for operator, wanted in clauses:
        have = current[: len(wanted)]
        satisfied = {
            ">=": have >= wanted,
            ">": have > wanted,
            "<=": have <= wanted,
            "<": have < wanted,
            "==": have == wanted,
            "!=": have != wanted,
        }[operator]
        if not satisfied:
            return False, (
                f"python_requires {spec!r} 不满足：当前解释器 {shown} "
                f"（lock 由更高版本解析，禁止降级解析）"
            )
    return True, f"python_requires {spec!r} 满足（当前 {shown}）"



def _normalize_python_spec(spec: str) -> str:
    """把 ``"3.12"`` 归一化为 ``"==3.12"``（前缀匹配）；约束原样返回。"""
    text = str(spec).strip()
    if re.fullmatch(r"\d+\.\d+", text):
        return f"=={text}"
    return text


def _allowed_minors(spec: str) -> list[tuple[int, int]]:
    """枚举满足约束的 ``(major, minor)`` 候选，升序。

    用于"本机该找哪些解释器"以及"需要自愈时该装哪个版本"；
    上界来自约束本身（如 ``<3.14`` ⇒ 最高 3.13），因此**不写死**小版本。
    """
    normalized = _normalize_python_spec(spec)
    results: list[tuple[int, int]] = []
    for major in (3,):
        for minor in range(8, 20):
            ok, _detail = check_python_requires(normalized, (major, minor, 0))
            if ok:
                results.append((major, minor))
    return results



def _read_pyvenv_version(config_path: Path) -> str:
    """从 venv 的 ``pyvenv.cfg`` 读取解释器版本（``version_info = X.Y.Z``）。"""
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version_info", "version"):
            return value.strip()
    return ""


def _check_venv_shebang(
    runtime_dir: Path, entry_path: Path, entry_rel: str,
    published_dir: Path | None = None,
) -> str | None:
    """校验 pypi 入口的 shebang 解释器存在且位于本 runtime 内；返回问题描述或 None。

    实测缺陷（P0）：pip 写入的 shebang 是**安装期 staging 绝对路径**，
    发布（rename）后该路径消失 ⇒ 入口直接执行报 "No such file or directory"。
    这里把不变量固化为引擎级 fail-closed 校验。
    """
    try:
        with entry_path.open("rb") as handle:
            first_line = handle.readline(4096)
    except OSError as exc:
        return f"入口无法读取: {entry_rel}（{exc}）"
    if not first_line.startswith(b"#!"):
        return f"入口缺少 shebang（pypi console script 必须有）: {entry_rel}"
    interpreter = first_line[2:].strip().decode("utf-8", "replace")
    if not interpreter or " " in interpreter:
        return f"入口 shebang 非法: {entry_rel} -> {interpreter!r}"
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute():
        return f"入口 shebang 必须是绝对路径: {entry_rel} -> {interpreter!r}"
    if any(part.startswith(STAGING_PREFIX) for part in interpreter_path.parts):
        return (
            f"入口 shebang 指向 staging 路径（发布后必然失效）: "
            f"{entry_rel} -> {interpreter}"
        )
    resolved_runtime = runtime_dir.resolve()
    # 安装期自检在 **staging** 上跑，而 shebang 已被改写到**发布后路径**
    # （必须先改，否则 rename 之后入口直接失效）。因此把发布路径映射回
    # staging 再校验——不变量不变（解释器必须存在且位于本 runtime 内），
    # 只是允许目标尚未 rename。
    # 前缀比较两侧都必须先 resolve（PE-20261004-003 同族坑）：macOS 的
    # TMPDIR 形如 /var/folders（/var → /private/var 符号链接），pip 写
    # shebang 时用的是 resolve 后的 /private/var 真实路径，而调用方传入的
    # published_dir 往往还是未 resolve 的 /var/folders 字面路径——字面前缀
    # 比较永不命中，staging 映射候选被静默丢弃，发布前路径必然「缺失」。
    # resolve(strict=False) 会解析已存在祖先的符号链接、保留尚未存在的尾部，
    # 恰好匹配「发布目录尚未 rename」的场景。
    candidates = [interpreter_path]
    if published_dir is not None:
        prefix = str(Path(published_dir).resolve())
        text = str(interpreter_path.resolve())
        if text == prefix or text.startswith(prefix + os.sep):
            candidates.append(
                resolved_runtime / os.path.relpath(text, prefix)
            )
    for candidate in candidates:
        try:
            candidate.resolve(strict=True).relative_to(resolved_runtime)
        except (OSError, ValueError):
            continue
        return None
    return (
        f"入口 shebang 解释器缺失或越出本 runtime: "
        f"{entry_rel} -> {interpreter}"
    )

