#!/usr/bin/env python3
"""受信依赖锁：把 `pip install` 限制在操作者提供的哈希锁定清单内 (SEC-05)。

背景
----
``round_orchestrator.revalidate_candidates()`` 曾用 ``--auto-install-deps`` 直接执行::

    pip install -- <dependency>          # cwd = 被审计项目根目录

其中 ``dependency`` 来自被测项目的 ``.bugx/**/findings.json``，而充当「白名单」
的 ``declared`` 来自同一项目的 ``requirements*.txt`` —— **两个输入都在被审计项目
控制之下**，所以「必须命中声明」这道门禁不构成任何约束。再加上无版本锁定、无哈希、
``cwd`` 指向项目（pip 会读取项目内的 ``pip.conf`` / ``pyproject.toml``），目标仓库
只要同时放入 ``requirements.txt: requests`` 与 ``findings.json: {"dependencies":
["requests"]}``，就能让 BugX 在宿主机上安装任意包；pip 的构建后端
（``setup.py`` / ``build-backend``）在安装期执行任意代码 → **宿主 RCE**。

现在的纪律
----------
1. 自动安装默认关闭；开启时必须显式提供 ``--deps-lock``。
2. 锁文件必须位于**项目根目录之外**（项目内的文件同样由被审计方控制，不可信），
   且必须是普通文件、不能是符号链接。
3. 每条锁目必须是 ``name==version --hash=sha256:<64hex>``；缺版本、缺哈希、
   使用范围说明符（``>=`` / ``~=`` / ``*``）、直接 URL 或路径一律拒绝。
4. 只有「锁里存在」且被 findings 声明缺失的依赖才进入安装集合。
5. 安装时强制 ``--require-hashes --no-deps --only-binary=:all:``，在**中性临时
   目录**下运行，并清空所有 ``PIP_*`` / ``PYTHON*`` 环境变量，``PIP_CONFIG_FILE``
   指向 ``os.devnull``。

``--only-binary=:all:`` 是这里的关键：它禁止安装 sdist，从而杜绝构建后端在安装期
执行任意代码；``--require-hashes`` 保证安装内容与锁文件逐字节对应；``--no-deps``
阻止锁文件之外的传递依赖被顺带拉入。
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from core.supervisor import worker_execution
except ImportError:
    from core import worker_execution

__all__ = [
    "DependencyLockError",
    "LockedRequirement",
    "MAX_LOCK_BYTES",
    "PIP_INSTALL_FLAGS",
    "canonical_name",
    "install_locked",
    "load_trusted_lock",
    "parse_lock_text",
    "sanitized_pip_env",
    "select_installable",
]

#: 锁文件体积上限：正常锁文件远小于此值，超出即视为异常输入。
MAX_LOCK_BYTES = 1 << 20  # 1 MiB

#: 安装阶段固定使用的 pip 参数（顺序固定，便于测试与审计）。
PIP_INSTALL_FLAGS: tuple[str, ...] = (
    "--require-hashes",
    "--no-deps",
    "--only-binary=:all:",
    "--no-input",
    "--disable-pip-version-check",
    "--no-cache-dir",
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXACT_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^=;\s]+)$")

#: 允许透传给子进程的环境变量白名单（其余一律清空）。
_SAFE_ENV_KEYS = (
    "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "SYSTEMROOT", "COMSPEC", "PYTHONIOENCODING",
)


class DependencyLockError(RuntimeError):
    """受信依赖锁缺失、位置越界或格式非法。"""


@dataclass(frozen=True)
class LockedRequirement:
    """一条通过校验的锁定依赖。"""

    name: str
    """规范化后的项目名（PEP 503：小写、``-_.`` 折叠为 ``-``）。"""

    requirement: str
    """可直接写入 requirements 文件的行：``name==version --hash=sha256:<hex>``。"""


def canonical_name(raw: str) -> str:
    """PEP 503 名称规范化，用于在锁文件与 findings 之间做键匹配。"""
    return re.sub(r"[-_.]+", "-", str(raw).strip()).casefold()


def parse_lock_text(text: str) -> dict[str, LockedRequirement]:
    """解析锁文件文本；任何不合规的行都抛 :class:`DependencyLockError`。

    fail-closed：宁可整体拒绝，也不跳过无法解析的行——被跳过的那一行正是
    攻击者最希望我们忽略的内容。
    """
    if not isinstance(text, str):
        raise DependencyLockError("锁文件内容必须是文本")
    requirements: dict[str, LockedRequirement] = {}
    # 处理行尾反斜杠续行（pip requirements 语义）。
    logical: list[str] = []
    buffer = ""
    for raw_line in text.splitlines():
        stripped = raw_line.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1]
            continue
        logical.append(buffer + stripped)
        buffer = ""
    if buffer:
        logical.append(buffer)

    for number, line in enumerate(logical, start=1):
        # `` #`` 起注释；行首 ``#`` 亦是注释。
        content = line.split(" #", 1)[0].strip()
        if not content or content.startswith("#"):
            continue
        try:
            tokens = shlex.split(content)
        except ValueError as exc:
            raise DependencyLockError(f"锁文件第 {number} 行无法解析: {exc}") from exc
        if not tokens:
            continue
        spec, *hash_tokens = tokens
        match = _EXACT_RE.match(spec)
        if match is None:
            raise DependencyLockError(
                f"锁文件第 {number} 行必须是 `name==version`（不支持范围说明符、"
                f"直接 URL 或路径）: {spec}"
            )
        name = match.group("name")
        version = match.group("version")
        if not _NAME_RE.match(name) or not _VERSION_RE.match(version):
            raise DependencyLockError(f"锁文件第 {number} 行名称或版本含非法字符: {spec}")
        hashes: list[str] = []
        index = 0
        while index < len(hash_tokens):
            token = hash_tokens[index]
            if token == "--hash":
                index += 1
                if index >= len(hash_tokens):
                    raise DependencyLockError(f"锁文件第 {number} 行 --hash 缺少取值")
                value = hash_tokens[index]
            elif token.startswith("--hash="):
                value = token[len("--hash="):]
            else:
                raise DependencyLockError(
                    f"锁文件第 {number} 行含未授权参数 `{token}`；只允许 --hash"
                )
            if not _HASH_RE.match(value):
                raise DependencyLockError(
                    f"锁文件第 {number} 行哈希必须是 sha256:<64 位小写十六进制>: {value}"
                )
            hashes.append(value)
            index += 1
        if not hashes:
            raise DependencyLockError(f"锁文件第 {number} 行缺少 --hash，拒绝无哈希依赖: {spec}")
        key = canonical_name(name)
        requirement = " ".join([spec, *(f"--hash={value}" for value in hashes)])
        requirements[key] = LockedRequirement(name=key, requirement=requirement)
    if not requirements:
        raise DependencyLockError("锁文件未包含任何有效依赖条目")
    return requirements


def load_trusted_lock(
    lock_path: str | os.PathLike[str],
    *,
    project_root: str | os.PathLike[str],
) -> dict[str, LockedRequirement]:
    """加载并校验受信锁文件。

    位置约束是这里的核心：锁文件必须在被审计项目**之外**。项目内的任何清单都
    与被审计源码同源，不能作为信任根。
    """
    if lock_path is None or str(lock_path).strip() == "":
        raise DependencyLockError("必须显式提供 --deps-lock 受信锁文件路径")
    raw = Path(lock_path).expanduser()
    root = Path(project_root).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    resolved = raw.resolve()
    root_resolved = root.resolve()
    if raw.is_symlink():
        raise DependencyLockError(f"锁文件不能是符号链接: {lock_path}")
    if resolved == root_resolved or root_resolved in resolved.parents:
        raise DependencyLockError(
            f"锁文件必须位于被审计项目之外（项目内清单与被审计源码同源，不可作为信任根）: {lock_path}"
        )
    if not resolved.is_file():
        raise DependencyLockError(f"锁文件不是普通文件: {lock_path}")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise DependencyLockError(f"锁文件无法读取: {lock_path}") from exc
    if size > MAX_LOCK_BYTES:
        raise DependencyLockError(f"锁文件超过 {MAX_LOCK_BYTES} 字节上限: {lock_path}")
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DependencyLockError(f"锁文件无法解码: {lock_path}") from exc
    return parse_lock_text(text)


def select_installable(
    requested: list[str] | tuple[str, ...],
    lock: dict[str, LockedRequirement],
) -> tuple[list[LockedRequirement], list[str]]:
    """把 findings 声明的依赖与锁文件求交集。

    返回 ``(可安装项, 被拒绝项)``。被拒绝项一律不安装，只作为阻塞原因上报——
    这正是与旧实现的分界线：旧实现的「白名单」来自被审计项目本身，因此形同虚设。
    """
    installable: list[LockedRequirement] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        if not isinstance(raw, str) or not raw.strip():
            continue
        key = canonical_name(raw)
        if key in seen:
            continue
        seen.add(key)
        locked = lock.get(key)
        if locked is None:
            rejected.append(str(raw).strip())
            continue
        installable.append(locked)
    return installable, rejected


def sanitized_pip_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """构造 pip 子进程环境：只保留白名单变量，并禁用一切 pip 配置文件。

    ``PIP_CONFIG_FILE`` 指向 ``os.devnull`` 会关闭 pip 对 ``pip.conf`` /
    ``pip.ini`` 的查找，从而切断「项目内配置文件改写 index-url / extra-index-url」
    这条依赖混淆路径。
    """
    source = os.environ if base is None else base
    env = {key: source[key] for key in _SAFE_ENV_KEYS if key in source}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


def install_locked(
    locked: list[LockedRequirement],
    *,
    timeout: float = 180.0,
    python: str | None = None,
) -> tuple[list[str], list[str], str]:
    """在隔离环境中按锁文件安装依赖。

    返回 ``(已安装名称, 失败名称, 说明文本)``。调用方据此决定 replay 状态；
    本函数**不抛异常**，把失败降级为可上报的结果，避免中断整个复现流程。

    关键隔离点：

    * ``cwd`` 是新建的中性临时目录，**不是**被审计项目 —— pip 不再读取项目内的
      ``pip.conf`` / ``pyproject.toml``；
    * 环境变量经 :func:`sanitized_pip_env` 清洗；
    * 参数固定为 :data:`PIP_INSTALL_FLAGS`（含 ``--require-hashes``、
      ``--no-deps``、``--only-binary=:all:``），因此不会安装 sdist、不会执行
      构建后端、不会拉入锁外依赖。
    """
    if not locked:
        return [], [], ""
    executable = python or sys.executable
    argv_head = [executable, "-m", "pip", "install", *PIP_INSTALL_FLAGS]
    installed: list[str] = []
    failed: list[str] = []
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bugx-deps-") as neutral:
        neutral_dir = Path(neutral)
        requirements_file = neutral_dir / "requirements.lock.txt"
        requirements_file.write_text(
            "\n".join(item.requirement for item in locked) + "\n", encoding="utf-8",
        )
        command = [*argv_head, "-r", str(requirements_file)]
        try:
            result = worker_execution.run_bounded(
                command,
                cwd=neutral_dir,
                env=sanitized_pip_env(),
                timeout=timeout,
            )
        except worker_execution.WorkerExecutionError as exc:
            notes.append(f"依赖安装被拒绝: {exc}")
            return [], [item.name for item in locked], "; ".join(notes)
        if result.returncode == 0:
            installed = [item.name for item in locked]
        else:
            failed = [item.name for item in locked]
            tail = (result.stderr or result.stdout or "").strip()[-600:]
            notes.append(f"pip 退出码 {result.returncode}: {tail}")
        if getattr(result, "timed_out", False):
            notes.append("pip 安装超时，已回收进程组")
    return installed, failed, "; ".join(notes)
