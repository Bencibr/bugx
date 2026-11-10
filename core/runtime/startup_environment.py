#!/usr/bin/env python3
"""BugX 启动环境探测与交互通道选择。

启动脚本通常由宿主的 Bash/MCP 子进程执行，子进程本身没有 TTY，不能仅凭
``stdin.isatty()`` 判断是否可以向用户呈现选择。这里沿进程树识别宿主，区分
宿主 TUI、ACP/headless 和普通终端，并给启动门禁选择唯一的交互通道。

本模块只做只读探测，不执行宿主命令、不写配置、不改变运行态。宿主选择控件仍
由 Agent 调用；核心只返回可验证的能力契约和结构化门禁所需的环境信息。

进程树与 Git 环境在一次会话里不会改变，而 ``detect()`` 单次要同步执行 7 次
git 子进程与最多 8 层进程祖先 ``ps``（实测 65-108 ms）。因此这两个子探测按
"一个进程"缓存，见 :func:`clear_cache`。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import functools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Mapping, Sequence

try:
    import core.i18n as i18n
except ImportError:  # pragma: no cover - malformed legacy snapshot
    i18n = None

try:
    from core.host_runtime import host_registry
except ImportError:  # pragma: no cover - 引擎快照残缺时 fail-closed
    raise ImportError(
        "引擎快照残缺：缺少 core/host_registry.py。宿主清单只能来自 host_registry/"
        "core.hosts 插件，禁止用本地字面量降级；请重新运行 bugx install 修复引擎快照。"
    ) from None

# 宿主清单来自唯一真源 core/host_registry.py（其数据来源于 core/hosts 插件），
# 禁止在本模块再写字面量：三份互相漂移的清单曾导致 workbuddy/dsh/chatgpt 永远无法被识别。
HOSTS = host_registry.HOST_NAMES
NATIVE_QUESTION_HOSTS = host_registry.NATIVE_QUESTION_HOSTS
GIT_PROBE_TIMEOUT_SECONDS = 2.0


def _run_git(git: str, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded, shell-free Git probe."""
    return subprocess.run(
        [git, "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_PROBE_TIMEOUT_SECONDS,
        check=False,
    )


def detect_git(root: Path, *, environ: Mapping[str, str] | None = None) -> dict[str, object]:
    """Return a read-only Git readiness snapshot for the target project.

    Git is a startup prerequisite because BugX needs a reproducible baseline,
    rollback boundary and (when selected) a commit target.  This probe never
    initializes a repository or writes Git configuration; the host must act on
    the resulting startup gate after an explicit user choice.
    """
    root = root.expanduser().resolve()
    env = dict(os.environ if environ is None else environ)
    # When a caller supplies an explicit environment, an absent PATH must not
    # silently inherit the host process PATH and produce a false-ready probe.
    git = shutil.which("git", path=env.get("PATH", ""))
    result: dict[str, object] = {
        "status": "missing",
        "ready": False,
        "executable": git,
        "version": None,
        "root": str(root),
        "repository_root": None,
        "head": None,
        "identity": {"name": False, "email": False},
        "git_dir": None,
        "git_dir_writable": False,
        "baseline_readable": False,
        "detail": "未找到 git 可执行文件",
    }
    if not git:
        return result

    try:
        version_probe = subprocess.run(
            [git, "--version"], capture_output=True, text=True,
            timeout=GIT_PROBE_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["detail"] = f"git 无法执行: {exc}"
        return result
    result["version"] = version_probe.stdout.strip() or None
    if version_probe.returncode != 0:
        result["status"] = "unusable"
        result["detail"] = (version_probe.stderr or "git --version 失败").strip()
        return result

    try:
        repo_probe = _run_git(git, root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError) as exc:
        result["status"] = "unusable"
        result["detail"] = f"Git 仓库探测失败: {exc}"
        return result
    if repo_probe.returncode != 0:
        result["status"] = "not-repository"
        result["detail"] = "当前项目根目录不是 Git 仓库"
        return result
    try:
        repository_root = Path(repo_probe.stdout.strip()).expanduser().resolve()
    except (OSError, ValueError):
        repository_root = Path(repo_probe.stdout.strip()).expanduser()
    result["repository_root"] = str(repository_root)
    if repository_root != root:
        result["status"] = "wrong-root"
        result["detail"] = f"Git 仓库根目录与项目根目录不一致: {repository_root}"
        return result

    try:
        head_probe = _run_git(git, root, "rev-parse", "--verify", "HEAD")
        git_dir_probe = _run_git(git, root, "rev-parse", "--git-dir")
        name_probe = _run_git(git, root, "config", "--get", "user.name")
        email_probe = _run_git(git, root, "config", "--get", "user.email")
        status_probe = _run_git(git, root, "status", "--porcelain=v1", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError) as exc:
        result["status"] = "unusable"
        result["detail"] = f"Git 基线探测失败: {exc}"
        return result

    head = head_probe.stdout.strip() if head_probe.returncode == 0 else ""
    result["head"] = head or None
    result["baseline_readable"] = bool(head) and status_probe.returncode == 0
    identity = {
        "name": name_probe.returncode == 0 and bool(name_probe.stdout.strip()),
        "email": email_probe.returncode == 0 and bool(email_probe.stdout.strip()),
    }
    result["identity"] = identity
    git_dir_text = git_dir_probe.stdout.strip()
    if git_dir_text:
        git_dir = Path(git_dir_text)
        if not git_dir.is_absolute():
            git_dir = root / git_dir
        try:
            git_dir = git_dir.resolve()
        except OSError:
            pass
        result["git_dir"] = str(git_dir)
        result["git_dir_writable"] = git_dir.is_dir() and os.access(git_dir, os.W_OK)

    if not head:
        result["status"] = "unborn"
        result["detail"] = "Git 仓库没有可读取的 HEAD 基线"
    elif not result["baseline_readable"]:
        result["status"] = "baseline-unreadable"
        result["detail"] = "Git HEAD 或工作树状态不可读取"
    elif not identity["name"] or not identity["email"]:
        result["status"] = "identity-missing"
        result["detail"] = "Git 提交身份未完整配置（需要 user.name 和 user.email）"
    elif not result["git_dir_writable"]:
        result["status"] = "git-dir-readonly"
        result["detail"] = "Git 元数据目录不可写"
    else:
        result["status"] = "ready"
        result["ready"] = True
        result["detail"] = "Git 仓库、HEAD 基线、提交身份和元数据写入能力均可用"
    return result


@dataclass(frozen=True)
class StartupEnvironment:
    """启动时可审计的宿主与交互能力快照。"""

    host: str
    host_source: str
    interface: str
    interaction: str
    stdin_tty: bool
    stdout_tty: bool
    term: str
    command_line: tuple[str, ...]
    native_question: bool
    reason: str
    requested_locale: str | None = None
    resolved_locale: str = "en-US"
    locale_source: str = "default"
    locale_fallback_reason: str | None = None
    git: dict[str, object] = field(default_factory=dict)

    @property
    def interactive(self) -> bool:
        return self.interaction != "blocked"

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["command_line"] = list(self.command_line)
        payload["interactive"] = self.interactive
        return payload


def _command_for_pid(pid: int) -> str:
    """Read one process command line without invoking a shell."""
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                b"\x00", b" ",
            ).decode("utf-8", "replace").strip()
        except OSError:
            return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=0.5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


# --- macOS 原生父链回退 ------------------------------------------------------
#
# 沙箱化宿主（WorkBuddy 桌面端等）会用 seatbelt 策略拒绝 ``ps``；此时
# ``_command_for_pid`` 与 ``ps -o ppid=`` 双双失败，``process_ancestry()`` 会
# **静默返回空链**，宿主身份随之退化为 ``unknown``，启动门禁在第一步以
# 「没有宿主原生选择能力」为由硬阻断——原因与真实原因（探测被拒）完全不符。
#
# macOS 提供三条不经过 ``ps`` 的等价原语，本模块只用它们做回退：
#   ``libproc.proc_pidpath``            → 某个 pid 的可执行文件路径
#   ``sysctl(KERN_PROC_PID)``           → ``kinfo_proc``，含父 pid
#   ``sysctl(KERN_PROCARGS2)``          → 完整 argv（等价 ``ps -o command=``）
# 任一原语失败都返回空值，绝不用猜测填补，保持 fail-closed。

_DARWIN_CTL_KERN = 1
_DARWIN_KERN_PROC = 14
_DARWIN_KERN_PROC_PID = 1
_DARWIN_KERN_PROCARGS2 = 49
# ``struct kinfo_proc``（LP64）中 ``kp_proc.p_pid`` / ``kp_eproc.e_ppid`` 的偏移。
# 缓冲区必须不小于内核的 ``sizeof(kinfo_proc)``（当前 648 字节），否则 ``sysctl``
# 直接以 ENOMEM 失败、父 pid 永远读不到；取 1 KiB 留出结构体增长余量。
_DARWIN_KINFO_PID_OFFSET = 40
_DARWIN_KINFO_PPID_OFFSET = 560
_DARWIN_KINFO_BUFFER = 1024
_DARWIN_PIDPATH_BUFFER = 4096
_DARWIN_PROCARGS2_BUFFER = 1 << 18


def _darwin_sysctl(mib: Sequence[int], size: int) -> bytes:
    """Run one bounded ``sysctl`` read; empty bytes means the probe failed."""
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libc.dylib")
        buffer = ctypes.create_string_buffer(size)
        length = ctypes.c_size_t(size)
        array = (ctypes.c_int * len(mib))(*mib)
        if libc.sysctl(array, len(mib), buffer, ctypes.byref(length), None, 0) != 0:
            return b""
    except (OSError, AttributeError, ValueError, TypeError):
        return b""
    return buffer.raw[: length.value]


def _darwin_proc_pidpath(pid: int) -> str:
    """Executable path of ``pid`` through libproc, without spawning ``ps``."""
    try:
        import ctypes

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        buffer = ctypes.create_string_buffer(_DARWIN_PIDPATH_BUFFER)
        written = libproc.proc_pidpath(
            ctypes.c_int(pid), buffer, ctypes.c_uint32(_DARWIN_PIDPATH_BUFFER),
        )
    except (OSError, AttributeError, ValueError, TypeError):
        return ""
    if written <= 0:
        return ""
    return buffer.value.decode("utf-8", "replace").strip()


def _darwin_parent_pid(pid: int) -> int:
    """Parent pid of ``pid`` from ``kinfo_proc``; ``0`` when unreadable.

    The parsed ``kp_proc.p_pid`` must equal the requested pid; a mismatch means
    the kernel struct layout is not what this module assumes, so the walk stops
    instead of following a garbage parent.
    """
    import struct

    raw = _darwin_sysctl(
        (_DARWIN_CTL_KERN, _DARWIN_KERN_PROC, _DARWIN_KERN_PROC_PID, pid),
        _DARWIN_KINFO_BUFFER,
    )
    if len(raw) < _DARWIN_KINFO_PPID_OFFSET + 4:
        return 0
    if struct.unpack_from("<I", raw, _DARWIN_KINFO_PID_OFFSET)[0] != pid:
        return 0
    return struct.unpack_from("<I", raw, _DARWIN_KINFO_PPID_OFFSET)[0]


def _darwin_command_for_pid(pid: int) -> str:
    """Command line of ``pid`` from ``KERN_PROCARGS2``; path-only on failure."""
    import struct

    raw = _darwin_sysctl(
        (_DARWIN_CTL_KERN, _DARWIN_KERN_PROCARGS2, pid), _DARWIN_PROCARGS2_BUFFER,
    )
    if len(raw) >= 8:
        argc = struct.unpack_from("<i", raw, 0)[0]
        body = raw[4:]
        terminator = body.find(b"\x00")
        # 布局：argc | exec_path\0 | NUL 填充 | argv[0..argc-1]（各自 NUL 结尾）| env
        if 0 < argc <= 4096 and terminator > 0:
            remainder = body[terminator:].lstrip(b"\x00")
            arguments: list[str] = []
            for _ in range(argc):
                if not remainder:
                    break
                cut = remainder.find(b"\x00")
                if cut < 0:
                    cut = len(remainder)
                arguments.append(remainder[:cut].decode("utf-8", "replace"))
                remainder = remainder[cut + 1:]
            if arguments:
                return " ".join(arguments).strip()
    return _darwin_proc_pidpath(pid)


def _darwin_ancestry(max_depth: int) -> tuple[str, ...]:
    """``ps``-free macOS parent chain, nearest first."""
    commands: list[str] = []
    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            break
        command = _darwin_command_for_pid(pid)
        if command:
            commands.append(command)
        next_pid = _darwin_parent_pid(pid)
        if next_pid <= 0 or next_pid == pid:
            break
        pid = next_pid
    return tuple(commands)


_WINDOWS_PROC_TABLE: dict[int, tuple[int, str]] | None = None
_WINDOWS_PROC_TABLE_LOADED = False
WINDOWS_PROC_TABLE_TIMEOUT_SECONDS = 10.0


def _windows_process_table() -> dict[int, tuple[int, str]]:
    """Snapshot pid → (ppid, command line) for the current process lifetime.

    ``Get-CimInstance Win32_Process`` is the supported Windows replacement for
    ``/proc`` and ``ps -o ppid=``; the snapshot is taken once because a host
    identity derived from the ancestry chain cannot change mid-run.
    """
    global _WINDOWS_PROC_TABLE, _WINDOWS_PROC_TABLE_LOADED
    if _WINDOWS_PROC_TABLE_LOADED:
        return _WINDOWS_PROC_TABLE or {}
    _WINDOWS_PROC_TABLE_LOADED = True
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId, ParentProcessId, CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=WINDOWS_PROC_TABLE_TIMEOUT_SECONDS, check=False,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        _WINDOWS_PROC_TABLE = {}
        return _WINDOWS_PROC_TABLE
    if isinstance(data, dict):
        data = [data]
    table: dict[int, tuple[int, str]] = {}
    for entry in data:
        try:
            pid_value = int(entry["ProcessId"])
            ppid_value = int(entry["ParentProcessId"])
        except (KeyError, TypeError, ValueError):
            continue
        table[pid_value] = (ppid_value, (entry.get("CommandLine") or "").strip())
    _WINDOWS_PROC_TABLE = table
    return table


_MSYS_PS_BY_WINPID: dict[int, tuple[int, str]] = {}
_MSYS_PS_BY_CYGPID: dict[int, tuple[int, int]] = {}
_MSYS_PS_LOADED = False
MSYS_PS_PROBE_TIMEOUT_SECONDS = 2.0


def _parse_msys_ps_output(
    output: str,
) -> tuple[dict[int, tuple[int, str]], dict[int, tuple[int, int]]]:
    """Parse ``ps`` rows into winpid→(cygwin ppid, command) and cygpid→parent maps."""
    by_winpid: dict[int, tuple[int, str]] = {}
    by_cygpid: dict[int, tuple[int, int]] = {}
    for line in output.splitlines()[1:]:
        fields = line.split(None, 7)
        if len(fields) < 7:
            continue
        try:
            cygpid = int(fields[0])
            cygppid = int(fields[1])
            winpid = int(fields[3])
        except ValueError:
            continue
        command = fields[7].strip() if len(fields) > 7 else ""
        if winpid <= 0:
            continue
        by_winpid[winpid] = (cygppid, command)
        by_cygpid[cygpid] = (cygppid, winpid)
    return by_winpid, by_cygpid


def _msys_ps_tables() -> tuple[dict[int, tuple[int, str]], dict[int, tuple[int, int]]]:
    """Snapshot the MSYS ``ps`` table once; it keeps fork-accurate parent links."""
    global _MSYS_PS_BY_WINPID, _MSYS_PS_BY_CYGPID, _MSYS_PS_LOADED
    if _MSYS_PS_LOADED:
        return _MSYS_PS_BY_WINPID, _MSYS_PS_BY_CYGPID
    _MSYS_PS_LOADED = True
    if sys.platform == "win32":
        ps = shutil.which("ps")
        if ps:
            try:
                result = subprocess.run(
                    [ps], capture_output=True, text=True, errors="replace",
                    timeout=MSYS_PS_PROBE_TIMEOUT_SECONDS, check=False,
                )
                _MSYS_PS_BY_WINPID, _MSYS_PS_BY_CYGPID = _parse_msys_ps_output(
                    result.stdout,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    return _MSYS_PS_BY_WINPID, _MSYS_PS_BY_CYGPID


def _walk_windows_ancestry(
    table: dict[int, tuple[int, str]],
    ps_by_winpid: dict[int, tuple[int, str]],
    ps_by_cygpid: dict[int, tuple[int, int]],
    start_pid: int,
    max_depth: int,
) -> tuple[str, ...]:
    """Collect parent command lines, recovering MSYS fork gaps through ``ps``.

    fork-spawned MSYS processes (npm shims such as ``sh.exe``, coreutils such
    as ``timeout``) record the transient fork helper as their Windows ppid,
    so a plain ppid walk breaks before reaching the host process.  The MSYS
    ``ps`` table keeps the logical parent, and its WINPID column maps back
    into the Windows table where the full command line lives.  A hole may sit
    at a shim that already exited, so the walker bridges from the nearest
    chain entry that still has a ``ps`` row.
    """
    commands: list[str] = []
    pid = start_pid
    seen: set[int] = set()
    last_ps_pid = 0
    while pid > 0 and pid not in seen and len(commands) < max_depth:
        seen.add(pid)
        entry = table.get(pid)
        if entry is not None:
            ppid, command = entry
        else:
            ps_row = ps_by_winpid.get(pid)
            if ps_row is None:
                break
            ppid, command = ps_row[0], ps_row[1]
            parent = ps_by_cygpid.get(ppid)
            ppid = parent[1] if parent else 0
        if command:
            commands.append(command)
        if pid in ps_by_winpid:
            last_ps_pid = pid
        if ppid <= 0:
            break
        if ppid in table or ppid in ps_by_winpid:
            pid = ppid
            continue
        # Dangling Windows ppid: jump through the logical parent recorded by
        # the nearest ancestor present in the MSYS process table.
        bridge_pid = 0
        ps_row = ps_by_winpid.get(pid) or ps_by_winpid.get(last_ps_pid)
        if ps_row is not None:
            parent = ps_by_cygpid.get(ps_row[0])
            if parent is not None:
                bridge_pid = parent[1]
        if bridge_pid <= 0 or bridge_pid in seen:
            break
        pid = bridge_pid
    return tuple(commands)


def _windows_ancestry(*, max_depth: int) -> tuple[str, ...]:
    table = _windows_process_table()
    ps_by_winpid, ps_by_cygpid = _msys_ps_tables()
    return _walk_windows_ancestry(
        table, ps_by_winpid, ps_by_cygpid, os.getppid(), max_depth,
    )


def process_ancestry(*, max_depth: int = 8) -> tuple[str, ...]:
    """Return parent command lines, nearest first, with bounded traversal.

    结果按 ``max_depth`` 在进程内缓存（PERF-01）：一个进程的祖先链在生命周期
    内不会改变，Windows 分支的 ``_windows_process_table`` / ``_msys_ps_tables``
    早已采用同一"快照一次"的设计。缓存随进程结束失效；需要在同一进程内强制
    重探时调用 ``clear_cache()``。
    """
    return _process_ancestry_cached(max_depth)


@functools.lru_cache(maxsize=None)
def _process_ancestry_cached(max_depth: int) -> tuple[str, ...]:
    """Uncached ancestry walk behind :func:`process_ancestry`."""
    if sys.platform == "win32":
        return _windows_ancestry(max_depth=max_depth)
    commands = _posix_ancestry_via_ps(max_depth)
    if not commands and sys.platform == "darwin":
        # 沙箱宿主会拒绝 ``ps``，使 ps 版祖先链静默变空。空链不是「没有宿主」，
        # 只是「探测被拒」；改用 libproc/sysctl 原生原语重走，避免把可恢复的
        # 探测失败误判成宿主缺失。
        commands = _darwin_ancestry(max_depth)
    return commands


def _posix_ancestry_via_ps(max_depth: int) -> tuple[str, ...]:
    """POSIX parent chain through ``ps`` / ``/proc``."""
    commands: list[str] = []
    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            break
        command = _command_for_pid(pid)
        if command:
            commands.append(command)
        if sys.platform.startswith("linux"):
            try:
                status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
                parent = next(
                    (line.split("\t", 1)[1] for line in status.splitlines()
                     if line.startswith("PPid:\t")),
                    "1",
                )
                next_pid = int(parent)
            except (OSError, UnicodeError, ValueError):
                break
        else:
            # macOS exposes the same information through ``ps``. Walking more
            # than one level matters for Cline/ACP, where the launcher is often
            # behind a shell or an editor bridge.
            if sys.platform == "darwin":
                try:
                    parent_probe = subprocess.run(
                        ["ps", "-o", "ppid=", "-p", str(pid)],
                        capture_output=True, text=True, timeout=0.5, check=False,
                    )
                    next_pid = int(parent_probe.stdout.strip())
                except (OSError, subprocess.SubprocessError, ValueError):
                    break
            else:
                break
        if next_pid == pid:
            break
        pid = next_pid
    return tuple(commands)


def _tokens(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return tuple(command.split())


def _exe_stem(token: str) -> str:
    """Executable name: basename across both separators, launcher suffix stripped.

    Windows reports ``ZCode.exe``/``claude.cmd`` where POSIX reports plain
    binary names, and process command lines keep ``\\`` separators even when
    the detector runs on POSIX, so split on both and casefold.
    """
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    if name.casefold().endswith((".exe", ".cmd", ".bat")):
        name = name.rsplit(".", 1)[0]
    return name.casefold()


def _command_host(tokens: Sequence[str]) -> str | None:
    """Identify a host from the executable portion, not from a user prompt.

    Cline/OpenCode prompts can contain another host's name.  Looking through
    every argv token lets that data masquerade as process evidence.  Inspect
    the executable first, then only the script token of a small set of known
    launchers (``node``, ``python`` and wrappers such as ``env``/``sudo``).
    """
    if not tokens:
        return None
    wrappers = {
        "env", "sudo", "node", "nodejs", "python", "python3", "bun", "deno",
    }
    first = _exe_stem(tokens[0])
    if first in HOSTS[:-1]:
        return first
    if first not in wrappers:
        launch_tokens = tokens[:1]
    else:
        launch_tokens = tokens[1:5]
    for token in launch_tokens:
        if first == "env" and "=" in token and not token.startswith("/"):
            continue
        name = _exe_stem(token)
        if name in HOSTS[:-1]:
            return name
        # 别名归一化收敛到 host_registry（claude-code→claude、deepseek→dsh、
        # codebuddy→workbuddy），避免每个调用点各写一套映射。
        alias = host_registry.normalize_host_name(name) if host_registry is not None else None
        if alias:
            return alias
        if name in {"zcode", "zcode.cjs", "zcode.js", "zcode.mjs"} or name.startswith("zcode helper"):
            return "zcode"
    return None


# macOS ``.app`` bundle：``/Applications/WorkBuddy AI.app/...`` → ``WorkBuddy AI``。
# 打包型宿主（WorkBuddy 的 ``MacOS`` 可执行名就是 ``Electron``）无法从可执行名
# 识别，只能靠 bundle 名；而 bundle 名可能含空格，``shlex`` 会把
# ``WorkBuddy AI.app`` 拆成 ``/Applications/WorkBuddy`` + ``AI.app/...``，使旧实现
# 的命中**纯属巧合**（改名成无空格的 ``WorkBuddy.app`` 即失配）。因此从原始
# 命令行里读 bundle 名，不经过 ``shlex``。锚定命令行开头，确保只在可执行路径
# 上生效——出现在参数位置的 ``.app``（``cat /Applications/ZCode.app/...``）是数据，
# 不是进程证据。路径前缀组件不允许含空格，以便把 ``/bin/cat /Applications/X.app``
# 这类「命令 + 参数」整体排除。
_MACOS_APP_BUNDLE = re.compile(
    r"""^["']?/(?:[^/\s]+/)*([^/\s][^/]*)\.app(?=/|["']|\s|$)""",
)


def _app_bundle_host(command: str) -> str | None:
    """Identify a host from the leading macOS ``.app`` bundle name."""
    match = _MACOS_APP_BUNDLE.match(command)
    if match is None:
        return None
    for word in re.split(r"[\s_\-]+", match.group(1)):
        name = word.casefold()
        if not name:
            continue
        if name in HOSTS[:-1]:
            return name
        alias = (
            host_registry.normalize_host_name(name)
            if host_registry is not None else None
        )
        if alias:
            return alias
    return None


def _host_from_commands(commands: Sequence[str]) -> tuple[str | None, str, bool]:
    """Return (host, source, headless) from process command lines."""
    for command in commands:
        lower = command.casefold()
        tokens = _tokens(lower)
        host = _command_host(tokens)
        if host is None:
            # 打包型宿主的 ``MacOS`` 可执行名不是宿主名（WorkBuddy → Electron），
            # 且 ``shlex`` 会破坏含空格的 ``.app`` 路径；改从 bundle 名识别。
            host = _app_bundle_host(lower)
        if host is None:
            continue
        interactive = any(marker in tokens for marker in ("--tui", "-i"))
        headless = not interactive and any(
            marker in tokens
            for marker in ("acp", "--acp", "--json", "run", "exec", "--non-interactive", "--print-logs")
        )
        return host, "process-tree", headless
    return None, "unidentified", False


def _configured_host(root: Path) -> str | None:
    """Use one project host marker as a diagnostic fallback, never as proof.

    Multiple host entries are common when a project has been installed for
    more than one AI IDE.  In that case a marker cannot identify the current
    host and must not silently select OpenCode (or any other host).

    Marker table lives in ``host_registry`` so a newly supported host cannot be
    silently missing from this fallback (previously ``.workbuddy`` had no
    branch at all).
    """
    if host_registry is None:  # pragma: no cover - malformed snapshot
        return None
    try:
        return host_registry.configured_host(root)
    except OSError:
        return None


def _bool_env(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


@functools.lru_cache(maxsize=None)
def _git_snapshot_cached(root: str, path_env: str) -> dict[str, object]:
    """``detect()`` 复用的 Git 快照缓存（键：项目根 + PATH）。

    ``detect_git`` 只把 ``environ`` 用于 ``shutil.which`` 解析 git 可执行文件，
    其内部的 git 子进程一律继承真实 ``os.environ``，所以缓存键只需 PATH。

    ``detect_git()`` 自身的"每次都是新探测"契约保持不变：启动门禁
    （``git_startup_gate``）直接调用它时仍然拿到实时结果，用户修复 Git 后
    不需要重启进程即可重新探测。

    本函数是内部实现，返回值是缓存里的**共享对象**，调用方一律经
    :func:`_git_snapshot_for_detect` 取副本。
    """
    return detect_git(Path(root), environ={"PATH": path_env})


def _git_snapshot_for_detect(root: str, path_env: str) -> dict[str, object]:
    """返回缓存快照的独立副本。

    ``lru_cache`` 会把同一个 dict 交给所有调用方；一旦有调用方就地改写
    （或改写嵌套的 ``identity``），污染会扩散到所有持有该快照的对象——
    缓存的全部价值就是「可安全共享」，因此这里显式复制一层。
    """
    snapshot = _git_snapshot_cached(root, path_env)
    copied = dict(snapshot)
    identity = snapshot.get("identity")
    if isinstance(identity, dict):
        copied["identity"] = dict(identity)
    return copied


def detect(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    commands: Sequence[str] | None = None,
    stdin_tty: bool | None = None,
    stdout_tty: bool | None = None,
) -> StartupEnvironment:
    """Detect host, UI surface and the only safe startup interaction channel.

    ``BUGX_INTERACTION`` is an explicit integration hook for the interaction
    channel only.  Host identity is NEVER taken from the environment:
    PE-20261004-004（用户指令）——宿主身份只来自进程祖先链主动探查；
    探查失败 host=None（broken），由引擎显式告知宿主修复运行环境。
    A project config is only reported as a fallback host hint; it cannot
    grant native interaction to a headless process.
    """
    env = dict(os.environ if environ is None else environ)
    root = root.resolve()
    stdin = bool(sys.stdin.isatty()) if stdin_tty is None else bool(stdin_tty)
    stdout = bool(sys.stdout.isatty()) if stdout_tty is None else bool(stdout_tty)
    ancestry = tuple(commands) if commands is not None else process_ancestry()
    host, source, detected_headless = _host_from_commands(ancestry)
    host_observed = host is not None
    if host is None:
        host = _configured_host(root)
        source = "project-config" if host else "unidentified"

    explicit_channel = env.get("BUGX_INTERACTION", "").strip().casefold()
    explicit_transport = env.get("BUGX_TRANSPORT", "").strip().casefold()
    explicit_process_mode = env.get("BUGX_PROCESS_MODE", "").strip().casefold()
    explicit_headless = (
        _bool_env(env, "BUGX_ACP")
        or _bool_env(env, "BUGX_HEADLESS")
        or explicit_transport in {"acp", "pipe", "stdio"}
        or explicit_process_mode == "headless"
    )
    # An explicit headless/ACP marker is stronger than a copied TUI hint. ACP
    # brokers commonly inherit BUGX_INTERACTION=question from their desktop
    # host; allowing that hint to win makes the child claim a question tool it
    # cannot actually service and silently misclassifies ACP as TUI.
    if explicit_headless:
        detected_headless = True
        interaction = "blocked"
        reason = "宿主显式声明 ACP/headless，无可用原生单选回调"
    elif explicit_channel in {"question", "native", "native-question"}:
        interaction = "native-question"
        reason = "宿主显式声明原生单选能力"
    elif explicit_channel in {"tty", "tty-radio", "radio"}:
        interaction = "tty-radio"
        reason = "宿主显式声明 TTY 单选能力"
    elif explicit_channel in {"text", "text-menu"}:
        interaction = "text-menu"
        reason = "宿主显式声明文本菜单"
    elif explicit_channel in {"none", "blocked", "headless"}:
        interaction = "blocked"
        reason = "宿主显式声明无交互能力"
    else:
        if host in NATIVE_QUESTION_HOSTS and host_observed and not detected_headless:
            interaction = "native-question"
            reason = "从宿主 TUI 进程树识别到原生交互宿主"
        elif stdin and stdout:
            interaction = "text-menu" if _bool_env(env, "BUGX_TEXT_MENU") else "tty-radio"
            reason = "当前进程拥有真实 TTY"
        else:
            interaction = "blocked"
            reason = "没有宿主原生选择能力，也没有真实 TTY"

    interface = "headless"
    if host in NATIVE_QUESTION_HOSTS and host_observed and not detected_headless:
        interface = "tui"
    elif stdin and stdout:
        interface = "tty"
    elif detected_headless:
        interface = "acp" if any(
            token in {"acp", "--acp"}
            for command in ancestry
            for token in _tokens(command.casefold())
        ) or explicit_transport == "acp" or _bool_env(env, "BUGX_ACP") else "headless"

    native_question = interaction == "native-question"
    command_line = tuple(ancestry[:3])
    if i18n is not None:
        locale = i18n.resolve_locale(environ=env)
    else:  # pragma: no cover - only malformed installations
        locale = None
    return StartupEnvironment(
        host=host or "unknown",
        host_source=source,
        interface=interface,
        interaction=interaction,
        stdin_tty=stdin,
        stdout_tty=stdout,
        term=env.get("TERM", ""),
        command_line=command_line,
        native_question=native_question,
        reason=reason,
        requested_locale=locale.requested_locale if locale else None,
        resolved_locale=locale.resolved_locale if locale else "en-US",
        locale_source=locale.source if locale else "fallback",
        locale_fallback_reason=locale.fallback_reason if locale else "i18n unavailable",
        git=_git_snapshot_for_detect(str(root), env.get("PATH", "")),
    )


def clear_cache() -> None:
    """清空本模块的进程内探测缓存（进程树祖先 + Git 快照）。

    缓存生命周期是"一个进程"：宿主进程树、TTY 能力与 Git 就绪状态在一次会话里
    不会改变，因此正常启动流程无需清理。测试、或需要在同一进程内强制重新探测
    时调用；清理后下一次 ``detect()`` 会重新执行 git / ps 子进程。
    """
    _process_ancestry_cached.cache_clear()
    _git_snapshot_cached.cache_clear()
