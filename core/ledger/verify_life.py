#!/usr/bin/env python3
"""verify_life.py — bugx 寿命状态文件外部校验/修复器（元审计修复）。

背景：bugx 寿命机制的漏洞 A/B/E —— 寿命文件此前唯一的权威是 agent
自评自写回，无任何外部校验；且「支付轮费立即写回」在中断（kill/超时/打断）
下会产生「幽灵轮费」（life 已扣、history 无记录），状态不可恢复。

审计修复（v0.0.1 审计发现的三条逃逸链，全部在此封堵）：
  1. 自评刷命（漏洞 1）：settle 此前全盘信任 agent 自报 findings/credited，
     可凭空编造 5 条刷命。→ 现在每条 finding 必须含真实证据引用
     （`文件:行号` 且文件真实存在，或 `test_*` 测试名 + 复现/观察标记），
     无证据的编造条自动剔除出计命，刷命被拒。
  2. bash 篡改校验器/快照（漏洞 2）：校验器与快照与寿命文件同权限，agent
     可用 bash 的 sed/cp 改掉校验逻辑或同步改快照绕过 diff。
     → 现在校验器内嵌 SELF_HASH 自校验（脚本被改则所有命令拒绝执行）；
     且 pre 输出「外部基线」JSON，post 用调用方传入的基线对比，
     agent 改仓库内 snapshot 不再有效。
  3. repair 滥用（漏洞 3）：repair 机械修复可能被用来掩盖双结算痕迹。
     → repair 增加审计日志（repair-audit.log），每次修复留痕，调用方可查。

本脚本由调用方（主会话/用户）在启动 bugx 前后执行，提供硬校验：
  check     校验一致性（不一致 exit 1，打印差异）
  repair    修复不一致（回滚幽灵轮费：life 恢复到最后结算值，留审计日志）
  settle    每轮结算（确定性入口：脚本计算 delta/life 并原子写回；含证据校验）
  reset     重置为初始状态
  snapshot  把当前状态备份到 .snapshot.json（基线）
  diff      对比当前状态与快照/外部基线，检出「自洽撒谎」与超范围涨命
  restore   从快照恢复当前状态（diff 检出异常后的回滚）
  selfhash  输出本脚本当前 SHA256（维护用：改完脚本后更新 SELF_HASH）
  set-mode  持久化用户确认的修复模式（auto/log-only）

调用规范（防「自洽撒谎」盲区，v0.0.1 加固）：
  1. 启动 bugx 前：`bugx engine --root . pre` —— 做 check + snapshot，
     并**输出一行外部基线 JSON**（含 life/rounds_completed/history 前缀哈希）。
     调用方把该基线保存到 agent 会话之外（如手动记录，或 export 环境变量）。
  2. bugx 运行结束后：`bugx engine --root . post` —— 优先用
     `BH_PRE_BASELINE` 环境变量（调用方传入的外部基线）做 diff；
     若未设置则回退读仓库内 snapshot。外部基线是权威，agent 无法篡改。
  3. diff 异常 → restore 回滚到基线，并复核 agent 报告是否真实。

不变量（新机制：轮费改在结算时支付，life 只由结算写入）：
  1. history 的 round 号必须连续 1..N
  2. life == 最后一条 history.life_after（无 history 则必须 == 1）
     —— 违反即「幽灵轮费/未结算」，可被 repair 回滚
  3. rounds_completed == len(history)
  4. round == rounds_completed + 1
  5. alive == (life > 0)
  6. found_total == sum(credited)（各轮计命数之和，超额/重复不计命）

用法：
  python3 verify_life.py check      # 一致 exit 0；不一致 exit 1
  python3 verify_life.py repair     # 修复不一致（幂等，留审计日志）
  python3 verify_life.py reset      # 重置初始态
  python3 verify_life.py snapshot   # 备份当前状态
  python3 verify_life.py selfhash   # 输出自身 SHA256（维护用）
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

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_core_dir = str(Path(__file__).resolve().parent.parent)
if _core_dir not in sys.path:
    sys.path.insert(0, _core_dir)

try:
    import core.bugx_paths as bugx_paths
except ImportError:  # pragma: no cover - compatibility with standalone copies.
    bugx_paths = None  # type: ignore[assignment]

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX is the supported runtime.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows-only fallback.
    msvcrt = None  # type: ignore[assignment]

def _default_life_paths() -> tuple[Path, Path, Path, Path]:
    script_dir = Path(__file__).resolve().parent
    if bugx_paths is not None:
        # Layout 2 的引擎始终位于全局目录；交给
        # bugx_paths.project_root() 解析 BUGX_PROJECT_ROOT 或 cwd。
        try:
            root = bugx_paths.project_root(script_dir)
            active = bugx_paths.require(root)
            return active.life, active.life_snapshot, active.state_root / "repair-audit.log", active.life_lock
        except (OSError, ValueError, RuntimeError):
            # 无合法 .bugx 运行态（独立协议测试/自包含引擎副本）。
            life = _standalone_life_file(script_dir)
            return life, life.with_suffix(".json.snapshot"), life.parent / "repair-audit.log", \
                life.with_name(life.name + ".lock")
    life = _standalone_life_file(script_dir)
    return life, life.with_suffix(".json.snapshot"), life.parent / "repair-audit.log", \
        life.with_name(life.name + ".lock")


def _standalone_life_file(script_dir: Path) -> Path:
    """无项目布局时寿命状态文件的唯一位置。

    必须与 ``launch_phases.common._path_values_for_root`` 同源：自包含引擎副本
    的布局是 ``root/core/ledger/verify_life.py``，运行态在 ``root``。旧实现固定
    写 ``script_dir/bugx-life.json``，于是
    ``python core/ledger/verify_life.py reset`` 把状态写进 ``core/ledger/``，
    而启动器读 ``root/bugx-life.json`` —— 两份状态各自存在、互不可见，
    ``status``/``check`` 报"寿命文件不存在"，reset 却宣称成功。
    """
    engine_root = script_dir.parent
    if (engine_root / "launch_bugx.py").is_file():
        return engine_root.parent / "bugx-life.json"
    return script_dir / "bugx-life.json"


LIFE_FILE, SNAPSHOT_FILE, AUDIT_LOG, LIFE_LOCK_FILE = _default_life_paths()

# 自校验：脚本被 bash/sed/cp 篡改后，所有命令拒绝执行。
# 维护方法：改完本脚本后运行 `python3 verify_life.py selfhash`，
# 把输出粘贴到下面 SELF_HASH = "..." 即可（哈希会随每次编辑变化）。
SELF_HASH = "56f26ad4a62294f914e98fe771a0cae231fd5790a5b36da7ece4968e3171690d"

# 单轮每项真实发现的加分上限（与 bugx.md「单轮加分上限」一致）。
# 防「凑数无限续命」：每轮 life 净增上限 = -1(轮费) + MAX_PER_ROUND。
MAX_PER_ROUND = 5
# 本脚本被设计成**可独立复制执行**（tests/ledger/test_verify_life.py 会把它单独
# 拷进临时目录跑，那里没有 core 包），因此不能 import canonical 常量。字面量必须在
# 此维护，并由 tests/launch/test_orchestrator_controls.py 断言与
# core.launch_phases.common.FIX_MODES 完全相等——单一事实源靠契约测试守，不靠 import。
VALID_MODES = {"auto", "log-only"}
SETTLEMENT_WAL_SCHEMA = 1


class SettlementRecoveryError(RuntimeError):
    """A pending cross-file settlement cannot be recovered safely."""


def self_check() -> None:
    """脚本自校验：SELF_HASH 与真实内容不符 → 拒绝执行（exit 3）。

    哈希计算**排除本文件内的 SELF_HASH 行**（避免自引用死循环）——
    只校验代码与注释，SELF_HASH 常量行本身可自由更新。
    维护：改完脚本后运行 `python3 verify_life.py selfhash` 更新基线。
    """
    try:
        raw = Path(__file__).resolve().read_bytes()
    except OSError:
        return
    # 去掉 SELF_HASH = "..." 这一行再算哈希
    lines = raw.splitlines()
    cleaned = b"\n".join(
        ln for ln in lines if not ln.startswith(b'SELF_HASH = "')
    )
    actual = hashlib.sha256(cleaned).hexdigest()
    # 仅当 selfhash 作为命令本身（argv[1]）时输出当前哈希——这是维护命令。
    # 作为任意位置的后缀参数出现时【不得】绕过上方篡改检测：否则攻击者只需
    # 在 argv 里塞一个 --selfhash 就能让已篡改脚本身份校验通过。
    if len(sys.argv) > 1 and sys.argv[1] in {"selfhash", "--selfhash"}:
        print(actual)
        sys.exit(0)
    if actual != SELF_HASH:
        print("[verify_life] 校验器自身被篡改（SHA256 不匹配），拒绝执行。")
        print("  可能被 bash 的 sed/cp 修改。请从仓库恢复：")
        print("    git checkout -- core/verify_life.py")
        print("  或维护者运行 `python3 verify_life.py selfhash` 更新基线。")
        sys.exit(3)




def load() -> dict:
    """读寿命文件；区分「尚未初始化」与「JSON 损坏（写回中断）」。

    二者恢复路径完全不同：前者由 reset 的 bootstrap 例外创建初始状态，
    后者才适用 restore/人工修复。混报会让首次运行的用户去"人工修复"
    一个根本不存在的文件（LOOP-1）。
    """
    if not LIFE_FILE.is_file():
        print("[verify_life] 寿命文件不存在（首次运行尚未初始化）。")
        print(f"  路径: {LIFE_FILE}")
        print("  处理：运行 `python3 verify_life.py reset` 创建初始状态"
              "（启动器会在首次启动时自动完成）。")
        sys.exit(2)
    try:
        value = json.loads(LIFE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[verify_life] 寿命文件无法解析: {e}")
        print(f"  路径: {LIFE_FILE}")
        print("  可能上次写回被中断导致 JSON 损坏。")
        print("  处理：有快照基线则 `restore` 回滚；否则人工修复该文件。")
        sys.exit(2)
    if not isinstance(value, dict):
        print("[verify_life] 寿命文件根节点必须是 JSON 对象")
        print(f"  路径: {LIFE_FILE}")
        sys.exit(2)
    return value


def save(d: dict) -> None:
    _save_atomic(d)


def _assert_no_symlink_components(path: Path) -> None:
    """拒绝路径中任何已存在的符号链接/reparse 组件。

    DES-17：符号链接防越界检查收敛到 ``bugx_paths.assert_no_symlink_components``
    这一唯一实现（含 Windows junction/reparse point 平台分支），本模块不再
    自带一份只认 ``is_symlink()`` 的副本。仅在 ``bugx_paths`` 完全不可导入
    （独立协议测试的平铺副本）时才退回最小本地实现，行为与旧版一致。
    """
    if bugx_paths is not None:
        bugx_paths.assert_no_symlink_components(path, OSError)
        return
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink() and current.parent != Path(absolute.anchor):
            raise OSError(f"拒绝使用符号链接路径组件: {path}")


# 寿命锁等待上限：超过即判定存在卡死/并发冲突，带诊断信息失败（DOC-031 锁统一）。
LIFE_LOCK_TIMEOUT_SECONDS = 10.0


@contextmanager
def _life_lock(timeout_seconds: float | None = None):
    """Serialize read/check/settle/write and release the lock on process crash.

    flock 非阻塞重试至超时（默认 LIFE_LOCK_TIMEOUT_SECONDS=10s）：持锁进程
    崩溃由内核自动释放；超时则报出锁文件路径与持有提示，避免无界等待。
    """
    if timeout_seconds is None:
        timeout_seconds = LIFE_LOCK_TIMEOUT_SECONDS
    lock_file = LIFE_LOCK_FILE
    _assert_no_symlink_components(lock_file.parent)
    if lock_file.is_symlink():
        raise OSError(f"拒绝使用符号链接锁文件: {lock_file}")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if lock_file.is_symlink():
        raise OSError(f"拒绝使用符号链接锁文件: {lock_file}")
    with lock_file.open("a+b") as handle:
        if fcntl is not None:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        holder = ""
                        try:
                            handle.seek(0)
                            raw = handle.read(64).decode("utf-8", "replace").strip()
                        except OSError:
                            raw = ""
                        if raw:
                            holder = f"（内容：{raw}）"
                        raise TimeoutError(
                            f"寿命状态锁等待超时（{timeout_seconds:.0f}s）：{lock_file} "
                            f"正被其他进程持有{holder}；请确认没有其他 "
                            "verify_life/launch_bugx 进程正在结算后重试"
                        ) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is not None:  # pragma: no cover - exercised by Windows tests.
            # ``LK_LOCK`` may raise ``Resource deadlock avoided`` immediately
            # when the peer is in the same process.  Use the non-blocking
            # primitive and apply the same bounded diagnostic wait as flock.
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + timeout_seconds
            lock_mode = getattr(msvcrt, "LK_NBLCK", msvcrt.LK_LOCK)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), lock_mode, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        holder = ""
                        try:
                            handle.seek(0)
                            raw = handle.read(64).decode("utf-8", "replace").strip()
                        except OSError:
                            raw = ""
                        if raw:
                            holder = f"（内容：{raw}）"
                        raise TimeoutError(
                            f"寿命状态锁等待超时（{timeout_seconds:.0f}s）：{lock_file} "
                            f"正被其他进程持有{holder}；请确认没有其他 "
                            "verify_life/launch_bugx 进程正在结算后重试"
                        ) from exc
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        # A platform without either kernel locking primitive is not safe for
        # concurrent settlement. Keep importability, but fail closed at use.
        raise OSError("当前平台不支持寿命状态的跨进程锁")


def _credited(h: dict) -> int:
    """本轮计命发现数：优先取 credited 字段（新机制，超额/重复不计命），
    缺失（旧数据）回退到 len(findings)。"""
    if h.get("credited") is not None:
        value = h["credited"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("credited 必须是整数")
        return value
    findings = h.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError("findings 必须是数组")
    return len(findings)


def _load_json(path: Path, what: str) -> dict:
    """读 JSON 文件；损坏/缺失时优雅报错（exit 2），不 traceback。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[verify_life] {what} 无法解析: {e}")
        print(f"  路径: {path}")
        print("  可能文件被写回中断或篡改。先 `snapshot` 重建基线，"
              "或人工修复。")
        sys.exit(2)


def _int_arg(val: str, name: str) -> int:
    try:
        return int(val)
    except ValueError:
        print(f"[verify_life] 参数 {name} 必须是整数，got {val!r}")
        sys.exit(2)


# ---- 证据校验（修复漏洞 1：堵「凭空编造 findings 刷命」）----

# 真实证据引用形态：
#   1) `文件路径:行号`（文件必须真实存在；路径可相对仓库根或绝对）
#   2) `test_xxx` 测试名 + 出现 复现/观察/Repro/Observed 标记
# 两者任一命中且文件存在才计为「有证据」。
_EVIDENCE_FILE_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\\-]+\.(?:py|js|ts|jsx|tsx|go|rs|java|kt|"
    r"c|cpp|h|sh|md|json|yaml|yml|toml|xml|html|css|sql|log|txt|"
    r"cjs|mjs|mts|cts))"
    r"\s*[:#]\s*(?P<line>\d+)"
)
_TEST_NAME_RE = re.compile(r"\btest_[A-Za-z0-9_]+")
_TEST_DEF_RE = re.compile(r"(?:def|async\s+def)\s+(?P<name>test_[A-Za-z0-9_]+)\b")
_REPRO_MARK_RE = re.compile(r"(复现|观察|预期|Repro|Observed|Expected|exit\s+code)")

# 源码仓库根是 core/ 的上级；Layout 2 的项目根由
# BUGX_PROJECT_ROOT/cwd 绑定，不从项目可执行目录反推。
def _resolve_repo_root(invocation_file: Path) -> Path:
    configured = os.environ.get("BUGX_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    # Layout 2 executes the shared engine with the project as cwd.  Standalone
    # test/portable copies also carry their launcher beside the project files;
    # prefer that explicit working tree before falling back to the source tree
    # parent used when verify_life.py is executed from ``core/``.
    cwd = Path.cwd().resolve()
    if invocation_file.exists() and (
        (cwd / ".bugx").is_dir() or (cwd / "launch_bugx.py").is_file()
    ):
        return cwd
    link_dir = invocation_file.absolute().parent
    if link_dir.name in ("ledger", "probes", "supervisor"):
        link_dir = link_dir.parent
    return link_dir.parent


_REPO_ROOT = _resolve_repo_root(Path(__file__))


def _file_real(path: str, line: int | None = None) -> bool:
    """文件和（如提供）行号真实存在于仓库。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = _REPO_ROOT / path
    try:
        p = p.resolve(strict=True)
        p.relative_to(_REPO_ROOT.resolve(strict=True))
    except (OSError, ValueError):
        return False
    if not p.is_file():
        return False
    if line is None:
        return True
    try:
        # 行号证据至少不能超出文件实际范围；语义正确性仍需调用方复核。
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
        return 1 <= line <= line_count
    except OSError:
        return False


def _test_real(name: str) -> bool:
    """Require a test name to resolve to an actual test definition."""
    root = _REPO_ROOT.resolve()
    try:
        for candidate in root.rglob("test*.py"):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(match.group("name") == name for match in _TEST_DEF_RE.finditer(text)):
                return True
    except OSError:
        return False
    return False


def _audit(kind: str, detail: str) -> None:
    """修复/重置等外部操作用途留痕（修复漏洞 3：repair 可被追踪）。"""
    import datetime

    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(
                f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                f"[{kind}] {detail}\n"
            )
    except OSError:
        pass


def evidence_bad_lines(findings: list[str]) -> list[str]:
    """返回没有真实证据引用的 findings 行（漏洞 1 校验）。

    有证据判定：`文件:行号` 且文件存在；或 `test_*` 名 + 复现/观察标记。
    """
    bad: list[str] = []
    for f in findings:
        has_file_ref = False
        for m in _EVIDENCE_FILE_RE.finditer(f):
            if _file_real(m.group("path"), int(m.group("line"))):
                has_file_ref = True
                break
        test_match = _TEST_NAME_RE.search(f)
        has_test_repro = bool(
            test_match and _REPRO_MARK_RE.search(f) and _test_real(test_match.group(0))
        )
        if not (has_file_ref or has_test_repro):
            bad.append(f)
    return bad


def external_baseline() -> dict | None:
    """读取调用方传入的外部基线（修复漏洞 2 的关键）。

    优先级：环境变量 BH_PRE_BASELINE（调用方 pre 时输出、post 时传回，支持 JSON 字符串或文件路径）
    > 仓库内 snapshot 文件。外部基线在 agent 会话之外，agent 无法篡改。
    """
    import os

    raw = os.environ.get("BH_PRE_BASELINE", "").strip()
    if raw:
        try:
            p = Path(raw)
            if p.is_file():
                return _load_json(p, "外部基线文件")
        except (OSError, ValueError):
            pass
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[verify_life] BH_PRE_BASELINE 无法解析: {e}")
            return None
    if SNAPSHOT_FILE.is_file():
        return _load_json(SNAPSHOT_FILE, "快照基线")
    return None





def check_errors(d: dict) -> list[str]:
    """返回不变量违反清单（空列表 = 一致）。"""
    errors: list[str] = []
    if not isinstance(d, dict):
        return ["寿命状态根节点必须是 JSON 对象"]
    hist = d.get("history") or []
    if not isinstance(hist, list):
        return ["history 必须是数组"]
    life = d.get("life")
    if isinstance(life, bool) or not isinstance(life, int):
        errors.append(f"life({life!r}) 必须是整数")
    rounds_completed = d.get("rounds_completed")
    if isinstance(rounds_completed, bool) or not isinstance(rounds_completed, int) \
            or rounds_completed < 0:
        errors.append(f"rounds_completed({rounds_completed!r}) 必须是非负整数")
        rounds_completed = 0
    round_value = d.get("round")
    if isinstance(round_value, bool) or not isinstance(round_value, int):
        errors.append(f"round({round_value!r}) 必须是整数")
        round_value = 0
    alive = d.get("alive")
    if not isinstance(alive, bool):
        errors.append(f"alive({alive!r}) 必须是布尔值")
        alive = bool(alive)
    rounds = [h.get("round") if isinstance(h, dict) else None for h in hist]
    if rounds != list(range(1, len(hist) + 1)):
        errors.append(f"history round 号不连续: {rounds}")
    if hist and isinstance(hist[-1], dict):
        last_after = hist[-1].get("life_after")
        if not isinstance(last_after, int) or isinstance(last_after, bool):
            errors.append(f"最后 history.life_after({last_after!r}) 必须是整数")
        elif life != last_after:
            errors.append(
                f"life({life}) != 最后 history.life_after({last_after})"
                " —— 幽灵轮费/未结算"
            )
    else:
        if life != 1:
            errors.append(f"空 history 时 life 必须为 1，当前 {life}")
    if rounds_completed != len(hist):
        errors.append(
            f"rounds_completed({rounds_completed}) != len(history)"
            f"({len(hist)})"
        )
    # round 恒等于 rounds_completed + 1（下一轮号）。结算写回时必须同步推进。
    if round_value != rounds_completed + 1:
        errors.append(
            f"round({round_value}) != rounds_completed+1"
            f"({rounds_completed + 1})"
        )
    if isinstance(life, int) and not isinstance(life, bool) and alive != (life > 0):
        errors.append(f"alive({alive}) 与 life({life}) 不一致")
    mode = d.get("mode")
    if mode is not None and mode not in VALID_MODES:
        errors.append(
            f"mode({mode!r}) 无效，必须是 auto/log-only 或 null"
        )
    # found_total 必须等于各轮计命数（credited）之和——超额/重复发现计入
    # findings 但不计命，故不能用 sum(findings)。
    try:
        ft = sum(_credited(h) for h in hist)
    except (TypeError, ValueError) as exc:
        errors.append(f"history credited/findings 无效: {exc}")
        ft = -1
    found_total = d.get("found_total")
    if isinstance(found_total, bool) or not isinstance(found_total, int):
        errors.append(f"found_total({found_total!r}) 必须是非负整数")
    elif found_total != ft:
        errors.append(
            f"found_total({found_total}) != sum(credited)({ft})"
        )
    # life_after 链：第 i 条（i>=1）必须 == 上一条 life_after + delta。
    # 第一条无前置参照（初始 life 未硬编码，兼容非 1 初始），仅要求 delta 存在。
    # life == 最后一条 life_after 已在上方单独校验。
    if hist and isinstance(hist[0], dict) and hist[0].get("delta") is None:
        errors.append(f"history[0](round={hist[0].get('round')}) 缺 delta")
    for i, h in enumerate(hist):
        if not isinstance(h, dict):
            errors.append(f"history[{i}] 必须是对象")
            continue
        dlt = h.get("delta")
        if dlt is not None and (
            isinstance(dlt, bool) or not isinstance(dlt, int)
        ):
            errors.append(f"history[{i}](round={h.get('round')}) delta 必须是整数")
        elif dlt is not None and dlt > MAX_PER_ROUND - 1:
            errors.append(
                f"history[{i}](round={h.get('round')}) delta({dlt}) 超上界 "
                f"(≤{MAX_PER_ROUND - 1})——计命发现数可疑/伪造"
            )
        cred = h.get("credited")
        if cred is not None and (
            isinstance(cred, bool) or not isinstance(cred, int)
            or not (0 <= cred <= MAX_PER_ROUND)
        ):
            errors.append(
                f"history[{i}](round={h.get('round')}) credited({cred}) 越界，"
                f"须在 [0, {MAX_PER_ROUND}]"
            )
    for i in range(1, len(hist)):
        if not isinstance(hist[i - 1], dict) or not isinstance(hist[i], dict):
            continue
        prev_after = hist[i - 1].get("life_after")
        h = hist[i]
        delta = h.get("delta")
        after = h.get("life_after")
        if delta is None:
            errors.append(f"history[{i}](round={h.get('round')}) 缺 delta")
            continue
        if prev_after is None or after is None or after != prev_after + delta:
            errors.append(
                f"history[{i}](round={h.get('round')}) life_after({after}) "
                f"!= prev({prev_after})+delta({delta})"
                f"{'' if prev_after is None else '=' + str(prev_after + delta)}"
            )
    return errors


def _cmd_check_unlocked() -> int:
    d = load()
    errors = check_errors(d)
    if errors:
        print(f"[verify_life] 不一致（{len(errors)} 项）:")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print(
        f"[verify_life] OK: life={d.get('life')} round={d.get('round')} "
        f"rounds_completed={d.get('rounds_completed')} alive={d.get('alive')}"
    )
    return 0


def cmd_check() -> int:
    return _run_locked(_cmd_check_unlocked)


def _cmd_repair_unlocked() -> int:
    if not LIFE_FILE.is_file():
        # bootstrap 例外：寿命文件缺失时 reset 免确认创建初始状态
        # （_cmd_reset_unlocked 的设计边界），随后按新基线做一致性校验。
        print("[verify_life] 寿命文件不存在，按首次运行初始化…")
        if _cmd_reset_unlocked([]) != 0:
            return 1
        d = load()
        errors = check_errors(d)
        if errors:
            print(f"[verify_life] 初始化基线仍不一致（{len(errors)} 项），拒绝继续")
            return 1
        print("[verify_life] ✓ 初始基线已创建且一致")
        return 0
    d = load()
    errors = check_errors(d)
    if not errors:
        print("[verify_life] 已一致，无需修复")
        return 0
    hist = d.get("history") or []
    if any(not isinstance(item, dict) for item in hist):
        print("[verify_life] history 含非对象条目，拒绝自动 repair；请 restore 或人工复核")
        _audit("repair-refused", "history member is not object")
        return 1
    # 先检测 history 的 life_after 链是否断裂 / 缺 delta：
    # 若断裂，说明 history 内容被篡改，机械修复会把伪造的 life_after
    # 当作权威写进 life —— 拒绝自动修，交给 diff/restore 或人工。
    prev = 1
    chain_broken = False
    for h in hist:
        delta = h.get("delta")
        after = h.get("life_after")
        if delta is None or after is None or after != prev + delta:
            chain_broken = True
            break
        prev = after
    if chain_broken:
        print("[verify_life] history 的 life_after 链断裂或缺 delta——文件可能"
              "被篡改，拒绝机械修复。")
        print("  请用 `snapshot` 建立基线后 `restore` 回滚，"
              "或人工复核 history 内容。")
        _audit("repair-refused", f"链断裂拒绝修复: {errors[:3]}")
        return 1
    d["life"] = hist[-1]["life_after"] if hist else 1
    d["rounds_completed"] = len(hist)
    d["round"] = len(hist) + 1
    d["alive"] = d["life"] > 0
    d["found_total"] = sum(_credited(h) for h in hist)
    save(d)
    _audit("repair", f"life={d['life']} rounds={d['rounds_completed']}")
    print(
        f"[verify_life] 已修复: life={d['life']} round={d['round']} "
        f"rounds_completed={d['rounds_completed']} alive={d['alive']} "
        f"found_total={d['found_total']}"
    )
    print("  回滚项: 幽灵轮费已退回（life 恢复到最后结算值，未结算轮次不扣命）")
    for e in errors:
        print(f"  ✓ 已处理: {e}")
    return 0


def cmd_repair() -> int:
    return _run_locked(_cmd_repair_unlocked)


def _cmd_reset_unlocked(argv: list[str] | None = None) -> int:
    """Reset only during bootstrap or with an explicit user confirmation.

    A reset is a destructive state transition.  The bootstrap exception is
    limited to a missing life file; once a state exists, automation must carry
    the explicit ``--confirm-reset`` acknowledgement.
    """
    argv = argv or []
    if LIFE_FILE.exists() and "--confirm-reset" not in argv:
        print("[verify_life] 已存在寿命状态；reset 必须显式提供 --confirm-reset")
        _audit("reset-refused", "missing --confirm-reset")
        return 2
    ledger_file = LIFE_FILE.with_name("finding-ledger.json")
    if ledger_file.is_file():
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from core.ledger.life_ledger import FindingLedger

            FindingLedger(ledger_file).reset(worker="verify_life", evidence_refs=[])
        except (ImportError, OSError, ValueError, KeyError) as exc:
            print(f"[verify_life] 全局 finding ledger reset 审计失败，拒绝重置: {exc}")
            return 1
    d = {
        "life": 1,
        "found_total": 0,
        "round": 1,
        "rounds_completed": 0,
        "alive": True,
        "mode": None,
        "history": [],
    }
    save(d)
    print("[verify_life] 已重置为初始状态")
    return 0


def cmd_reset(argv: list[str] | None = None) -> int:
    return _run_locked(lambda: _cmd_reset_unlocked(argv))


def _cmd_set_mode_unlocked(argv: list[str]) -> int:
    """持久化用户已确认的修复模式；模式不由 agent 自行猜测。"""
    mode = None
    confirmed = False
    i = 0
    while i < len(argv):
        if argv[i] == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]
            i += 2
        elif argv[i] == "--confirm":
            confirmed = True
            i += 1
        else:
            print(f"[verify_life] set-mode 未知参数: {argv[i]}")
            return 2
    if mode not in VALID_MODES:
        print(
            f"[verify_life] mode({mode!r}) 无效，必须是 "
            "auto 或 log-only"
        )
        return 1
    d = load()
    if not d.get("alive", True) or d.get("life", 0) <= 0:
        print("[verify_life] 已死亡，不能设置会话模式——先经用户确认 reset")
        return 1
    if d.get("mode") not in (None, mode) and not confirmed:
        print("[verify_life] 修改已确认 mode 必须显式提供 --confirm")
        _audit("set-mode-refused", f"{d.get('mode')} -> {mode}")
        return 2
    d["mode"] = mode
    _save_atomic(d)
    errs = check_errors(d)
    if errs:
        print("[verify_life] 设置 mode 后校验失败:")
        for e in errs:
            print(f"  ✗ {e}")
        return 1
    print(f"[verify_life] 已保存用户确认模式: {mode}")
    return 0


def cmd_set_mode(argv: list[str]) -> int:
    return _run_locked(lambda: _cmd_set_mode_unlocked(argv))


def _cmd_snapshot_unlocked() -> int:
    if not LIFE_FILE.is_file():
        print("[verify_life] 无寿命文件可备份——先确认 bugx-life.json 存在")
        return 1
    if SNAPSHOT_FILE.is_symlink() or SNAPSHOT_FILE.parent.is_symlink():
        print(f"[verify_life] 拒绝写入符号链接快照路径: {SNAPSHOT_FILE}")
        return 1
    try:
        _copy_atomic(LIFE_FILE, SNAPSHOT_FILE)
    except OSError as e:
        print(f"[verify_life] 快照建立失败: {e}")
        return 1
    print(f"[verify_life] 已备份当前状态到 {SNAPSHOT_FILE.name}")
    return 0


def cmd_snapshot() -> int:
    return _run_locked(_cmd_snapshot_unlocked)


def _cmd_diff_unlocked() -> int:
    """对比当前状态与基线（外部基线优先，快照文件兜底），
    检出「自洽撒谎」/超范围涨命/历史篡改。修复漏洞 2。"""
    snap = external_baseline()
    if snap is None:
        print("[verify_life] 无可用基线——外部基线未设置且无快照文件，"
              "先运行 snapshot 再跑 diff")
        return 2
    baseline_errors = check_errors(snap)
    if baseline_errors:
        print("[verify_life] 外部基线不合法，拒绝 diff:")
        for error in baseline_errors:
            print(f"  ✗ {error}")
        return 2
    src = "外部基线(BH_PRE_BASELINE)" if os.environ.get("BH_PRE_BASELINE") else "快照文件"
    cur = load()
    current_errors = check_errors(cur)
    if current_errors:
        print("[verify_life] 当前寿命状态不合法，拒绝 diff:")
        for error in current_errors:
            print(f"  ✗ {error}")
        return 1
    issues: list[str] = []
    snap_rounds = snap.get("rounds_completed", 0)
    cur_rounds = cur.get("rounds_completed", 0)
    run = cur_rounds - snap_rounds
    # 已确认的会话模式不可在运行中静默切换；旧基线没有 mode 时允许一次迁移初始化。
    snap_mode = snap.get("mode")
    cur_mode = cur.get("mode")
    if snap_mode is not None and cur_mode != snap_mode:
        issues.append(f"会话 mode 被改写: {snap_mode!r} -> {cur_mode!r}")
    if run < 0:
        issues.append(f"rounds_completed 回退: {snap_rounds} -> {cur_rounds}")
    snap_hist = snap.get("history") or []
    cur_hist = cur.get("history") or []
    # 历史前缀不可篡改（已结算的轮次不允许被改/删）
    for i in range(min(len(snap_hist), len(cur_hist))):
        if snap_hist[i] != cur_hist[i]:
            issues.append(f"history[{i}]（round={snap_hist[i].get('round')}）"
                          f"被篡改或改写")
            break
    if len(cur_hist) < len(snap_hist):
        issues.append("history 条目被删除")
    if len(cur_hist) != snap_rounds + run:
        issues.append(
            f"history 条数({len(cur_hist)}) != 基线轮数+运行轮数"
            f"({snap_rounds}+{run})"
        )
    # 新增轮次的 delta 精确校验（替代旧的粗略范围，防「诚实记欺诈被误报」）：
    #   delta = -1(轮费) + credited - fraud
    #   上界：credited ≤ MAX_PER_ROUND → delta ≤ MAX_PER_ROUND - 1
    #   下界：fraud 无上限，不硬校验（诚实记录欺诈不该被回滚）
    cur_life = cur.get("life", 0)
    new_hist = cur_hist[len(snap_hist):]
    total_delta = 0
    for h in new_hist:
        dlt = h.get("delta")
        if dlt is None:
            issues.append(f"新增轮次 round={h.get('round')} 缺 delta")
            continue
        total_delta += dlt
        if dlt > MAX_PER_ROUND - 1:
            issues.append(
                f"round={h.get('round')} delta({dlt}) 超上界 "
                f"(≤{MAX_PER_ROUND - 1})——计命发现数可疑/伪造"
            )
    snap_life = snap.get("life", 1)
    if cur_life != snap_life + total_delta:
        issues.append(
            f"life 变化({cur_life - snap_life}) != 新增轮 delta 之和"
            f"({total_delta})"
        )
    if cur_life <= 0 and cur.get("alive"):
        issues.append("life≤0 但仍 alive（死亡绕过）")
    if issues:
        print(f"[verify_life] diff 检出异常（{len(issues)} 项）:")
        for e in issues:
            print(f"  ✗ {e}")
        return 1
    print(
        f"[verify_life] diff OK({src}): life {snap.get('life')} -> {cur_life} "
        f"（{run} 轮）history 前缀未篡改"
    )
    return 0


def cmd_diff() -> int:
    return _run_locked(_cmd_diff_unlocked)


def _cmd_restore_unlocked() -> int:
    """从快照恢复；快照必须本身合法（可解析 + 通过不变量），防恢复损坏文件。"""
    if not SNAPSHOT_FILE.is_file():
        print("[verify_life] 无快照可恢复——先运行 snapshot")
        return 2
    snap = _load_json(SNAPSHOT_FILE, "快照基线")
    errs = check_errors(snap)
    if errs:
        print("[verify_life] 快照基线本身不合法，拒绝恢复——防止把损坏/篡改的"
              "快照写回并覆盖真实状态：")
        for e in errs:
            print(f"  ✗ {e}")
        print("  请人工复核 bugx-life.json 与快照内容。")
        return 1
    if LIFE_FILE.is_symlink() or LIFE_FILE.parent.is_symlink():
        print(f"[verify_life] 拒绝覆盖符号链接寿命路径: {LIFE_FILE}")
        return 1
    try:
        _copy_atomic(SNAPSHOT_FILE, LIFE_FILE)
    except OSError as exc:
        print(f"[verify_life] 恢复写入失败: {exc}")
        return 1
    d = load()
    print(
        f"[verify_life] 已从快照恢复: life={d.get('life')} "
        f"round={d.get('round')} rounds_completed={d.get('rounds_completed')}"
    )
    return 0


def cmd_restore() -> int:
    return _run_locked(_cmd_restore_unlocked)


def _fsync_parent(path: Path) -> None:
    """Durably flush a directory when the platform exposes directory fsync."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_json_durable(path: Path, payload: dict, *, prefix: str) -> None:
    """Atomically write JSON and flush both file and containing directory."""
    _assert_no_symlink_components(path.parent)
    if path.is_symlink():
        raise OSError(f"拒绝写入符号链接 JSON 路径: {path}")
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=prefix, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_parent(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_atomic(d: dict) -> None:
    """原子且耐久地写回寿命状态。"""
    _write_json_durable(LIFE_FILE, d, prefix=".life-")


def _settlement_wal_path() -> Path:
    return LIFE_FILE.with_name(f".{LIFE_FILE.name}.settlement-wal.json")


def _state_digest(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_ledger_path(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = LIFE_FILE.parent / candidate
    candidate = candidate.resolve(strict=False)
    candidate.relative_to(LIFE_FILE.parent.resolve())
    return candidate


def _clear_settlement_wal() -> None:
    wal_path = _settlement_wal_path()
    if wal_path.is_symlink():
        raise SettlementRecoveryError(f"pending settlement WAL 是符号链接: {wal_path}")
    try:
        wal_path.unlink()
    except FileNotFoundError:
        return
    _fsync_parent(wal_path.parent)


def _ledger_has_settlement(state: dict, fingerprints: list[str], settlement_id: str) -> bool:
    findings = state.get("findings")
    if not isinstance(findings, dict):
        return False
    for fingerprint in fingerprints:
        record = findings.get(fingerprint)
        if not isinstance(record, dict):
            return False
        audits = record.get("audits")
        if not isinstance(audits, list) or not any(
            isinstance(audit, dict) and audit.get("settlement_id") == settlement_id
            for audit in audits
        ):
            return False
    return True


def _ledger_has_any(state: dict, fingerprints: list[str]) -> bool:
    findings = state.get("findings")
    return isinstance(findings, dict) and any(item in findings for item in fingerprints)


def _validate_settlement_wal(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != SETTLEMENT_WAL_SCHEMA:
        raise SettlementRecoveryError("pending settlement WAL schema 无法识别")
    required = {
        "settlement_id", "before_hash", "after_hash", "before_state", "after_state",
        "findings", "finding_fingerprints", "round", "ledger_path", "ledger_version",
    }
    if not required <= set(payload):
        raise SettlementRecoveryError("pending settlement WAL 缺少必要字段")
    txid = payload.get("settlement_id")
    if not isinstance(txid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", txid):
        raise SettlementRecoveryError("pending settlement WAL transaction ID 无效")
    before = payload.get("before_state")
    after = payload.get("after_state")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise SettlementRecoveryError("pending settlement WAL 状态快照无效")
    if payload.get("before_hash") != _state_digest(before) or payload.get("after_hash") != _state_digest(after):
        raise SettlementRecoveryError("pending settlement WAL 状态哈希不匹配")
    if check_errors(before) or check_errors(after):
        raise SettlementRecoveryError("pending settlement WAL 包含不一致的寿命状态")
    findings = payload.get("findings")
    fingerprints = payload.get("finding_fingerprints")
    if not isinstance(findings, list) or not isinstance(fingerprints, list) \
            or len(findings) != len(fingerprints) \
            or any(not isinstance(item, str) or not item for item in findings) \
            or any(not isinstance(item, str) or not item for item in fingerprints) \
            or len(set(fingerprints)) != len(fingerprints):
        raise SettlementRecoveryError("pending settlement WAL finding 批次无效")
    round_no = payload.get("round")
    if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1:
        raise SettlementRecoveryError("pending settlement WAL round 无效")
    ledger_path = payload.get("ledger_path")
    if ledger_path is not None and (not isinstance(ledger_path, str) or not ledger_path.strip()):
        raise SettlementRecoveryError("pending settlement WAL ledger 路径无效")
    ledger_version = payload.get("ledger_version")
    if ledger_version is not None and (
        isinstance(ledger_version, bool) or not isinstance(ledger_version, int) or ledger_version < 0
    ):
        raise SettlementRecoveryError("pending settlement WAL ledger 版本无效")
    return payload


def _load_settlement_wal() -> dict | None:
    wal_path = _settlement_wal_path()
    if wal_path.is_symlink():
        raise SettlementRecoveryError(f"pending settlement WAL 是符号链接: {wal_path}")
    if not wal_path.exists():
        return None
    try:
        payload = json.loads(wal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettlementRecoveryError(f"pending settlement WAL 无法解析: {exc}") from exc
    return _validate_settlement_wal(payload)


def _recover_pending_settlement_unlocked() -> None:
    payload = _load_settlement_wal()
    if payload is None:
        return
    try:
        current = load()
    except SystemExit as exc:
        raise SettlementRecoveryError("pending settlement 存在，但 life 状态不可读取") from exc
    current_hash = _state_digest(current)
    before_hash = payload["before_hash"]
    after_hash = payload["after_hash"]
    if current_hash not in {before_hash, after_hash}:
        raise SettlementRecoveryError("pending settlement 的当前 life 不匹配 before/after 快照")

    fingerprints = list(payload["finding_fingerprints"])
    findings = list(payload["findings"])
    if fingerprints:
        try:
            from core.ledger.life_ledger import FindingLedger

            ledger = FindingLedger(_resolve_ledger_path(payload["ledger_path"]))
            ledger_state = ledger.read()
            if not _ledger_has_settlement(ledger_state, fingerprints, payload["settlement_id"]):
                if _ledger_has_any(ledger_state, fingerprints):
                    raise SettlementRecoveryError(
                        "pending settlement 的 fingerprint 已存在，但没有本 transaction ID"
                    )
                result = ledger.settle(
                    findings,
                    round=payload["round"],
                    worker="verify_life",
                    timestamp=payload.get("timestamp"),
                    expected_version=payload.get("ledger_version"),
                    settlement_id=payload["settlement_id"],
                )
                if result.conflicts:
                    raise SettlementRecoveryError("pending settlement ledger CAS 冲突")
                ledger_state = ledger.read()
                if not _ledger_has_settlement(ledger_state, fingerprints, payload["settlement_id"]):
                    raise SettlementRecoveryError("pending settlement ledger 提交后无法核验")
        except SettlementRecoveryError:
            raise
        except (ImportError, OSError, ValueError, KeyError) as exc:
            raise SettlementRecoveryError(f"pending settlement ledger 恢复失败: {exc}") from exc

    if current_hash == before_hash:
        _save_atomic(payload["after_state"])
    _clear_settlement_wal()


def _settlement_failpoint(stage: str) -> None:
    """Test hook; production is a no-op and subprocess tests may replace it."""
    del stage


def _run_locked(callback):
    try:
        with _life_lock():
            _recover_pending_settlement_unlocked()
            return callback()
    except SettlementRecoveryError as exc:
        print(f"[verify_life] ✗ pending settlement 恢复失败，已拒绝继续: {exc}")
        return 1


def _copy_atomic(source: Path, target: Path) -> None:
    """Copy a validated JSON file without exposing a partial target."""
    _assert_no_symlink_components(source.parent)
    _assert_no_symlink_components(target.parent)
    if source.is_symlink() or target.is_symlink():
        raise OSError(f"拒绝通过符号链接复制状态文件: {source} -> {target}")
    if not source.is_file():
        raise OSError(f"源状态文件不存在: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}-", suffix=".tmp",
    )
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cmd_settle_unlocked(argv: list[str]) -> int:
    """agent 每轮结算的确定性入口（替代手写 JSON）。

    由脚本计算 delta/life/life_after/found_total/rounds 并原子写回，
    结算结果必然满足全部不变量（check 必过）。参数：
      --credited N            本轮计命发现数（[0, MAX_PER_ROUND]，脚本护栏）
      --fraud N               本轮欺诈扣分（≥0，默认 0）
      --findings-file PATH    本轮全部发现清单文件（每行一条；可含超额/重复）
      --ts YYYY-MM-DD         时间戳（默认今天）
      --round N               期望本轮号（可选；与 rounds_completed+1 不符则拒绝，
                              防误用旧轮号重复结算）
    """
    credited = 0
    fraud = 0
    findings_file = None
    ledger_path = None
    ts = None
    expect_round = None
    replay = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--credited" and i + 1 < len(argv):
            credited = _int_arg(argv[i + 1], "--credited")
            i += 2
        elif a == "--fraud" and i + 1 < len(argv):
            fraud = _int_arg(argv[i + 1], "--fraud")
            i += 2
        elif a == "--findings-file" and i + 1 < len(argv):
            findings_file = argv[i + 1]
            i += 2
        elif a == "--ledger" and i + 1 < len(argv):
            ledger_path = argv[i + 1]
            i += 2
        elif a == "--ts" and i + 1 < len(argv):
            ts = argv[i + 1]
            i += 2
        elif a == "--round" and i + 1 < len(argv):
            expect_round = _int_arg(argv[i + 1], "--round")
            i += 2
        elif a == "--replay":
            replay = True
            i += 1
        else:
            print(f"[verify_life] settle 未知参数: {a}")
            return 2
    if credited < 0 or credited > MAX_PER_ROUND:
        print(f"[verify_life] credited({credited}) 越界，须在 [0, {MAX_PER_ROUND}]")
        return 1
    if fraud < 0:
        print(f"[verify_life] fraud({fraud}) 不能为负")
        return 1
    d = load()
    if not d.get("alive", True) or d.get("life", 0) <= 0:
        print("[verify_life] 已死亡（alive=false, life≤0），拒绝结算——"
              "死亡即冻结，不得再记录轮次")
        return 1
    errs = check_errors(d)
    if errs:
        print("[verify_life] 基线不一致，拒绝结算——先 `repair` 或 `restore` 恢复基线")
        for e in errs:
            print(f"  ✗ {e}")
        return 1
    if findings_file is None:
        print("[verify_life] settle 必须提供 --findings-file")
        return 2
    try:
        findings = [
            ln.strip()
            for ln in Path(findings_file).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    except (OSError, UnicodeDecodeError) as e:
        print(f"[verify_life] 读取 findings 文件失败: {e}")
        return 1
    if len(findings) < credited:
        print(f"[verify_life] findings({len(findings)} 条) 少于 credited({credited})"
              "——计命数不能超过已记录发现")
        return 1
    if replay:
        if d.get("rounds_completed", 0) <= 0 or not d.get("history"):
            print("[verify_life] 尚未有已完成轮次，无法 --replay 覆盖")
            return 1
        if expect_round is not None and expect_round != d["rounds_completed"]:
            print(f"[verify_life] --replay 期望轮号({expect_round}) != 当前最后完成轮号({d['rounds_completed']})")
            return 1
        last_entry = d["history"].pop()
        last_delta = last_entry.get("delta", 0)
        last_credited = _credited(last_entry)
        d["rounds_completed"] -= 1
        d["life"] -= last_delta
        d["found_total"] -= last_credited
    round_no = d["rounds_completed"] + 1
    if expect_round is not None and expect_round != round_no:
        print(f"[verify_life] 期望轮号({expect_round}) != 当前应结算轮号"
              f"({round_no})——疑似重复结算或轮号错乱，拒绝")
        return 1
    # 与历史「原样字符串」重复的发现不计命（根因去重的字符串级护栏）：
    # 堵「复制粘贴历史 findings 刷命」；语义重复（措辞不同）仍靠 agent 自觉。
    all_hist_findings = set()
    for h in d.get("history") or []:
        for f in (h.get("findings") or []):
            all_hist_findings.add(str(f))
    dups = [f for f in findings if f in all_hist_findings]
    if dups:
        # 跨 Life 重放（2026-11-09 实测修复）：历史原样重复条**幂等剔除**而非
        # 拒绝——拒绝会让 auto 续轮死锁（同一批 findings 重放是自动重建
        # findings.json 的正常产物）。剔除后计命集合（countable）替换 findings
        # 成为后续全部防线与 ledger 的输入；history 仍记录原始提交（审计可追溯）。
        dups_set = set(dups)
        countable = [f for f in findings if f not in dups_set]
        credited = min(credited, len(countable))
        print(f"[verify_life] 与历史原样重复 {len(dups)} 条已幂等剔除"
              f"（credited 钳至 {credited}），继续结算")
        for dd in dups[:5]:
            print(f"  ✗ 剔除重复: {dd[:80]}")
        countable_findings: list = countable
    else:
        countable_findings = list(findings)
    max_creditable = len(countable_findings)
    if credited > max_creditable:
        print(f"[verify_life] credited={credited} 超过可计命发现数 {max_creditable}，拒绝结算")
        return 1
    # 证据校验（修复漏洞 1：堵「凭空编造 findings 刷命」）：
    # 每条计命发现必须含真实证据引用——`文件:行号`（文件真实存在）或
    # `test_*` 测试名 + 复现/观察标记。校验必须逐行覆盖计命前缀
    # findings[:credited]：按整份文件计数时，「1 条编造 + 5 条真实」
    # 会让 credited=5 通过，而实际计命前缀含编造行。
    fingerprints: list[str] = []
    if credited:
        bad_evidence = evidence_bad_lines(countable_findings[:credited])
        if bad_evidence:
            print(f"[verify_life] 计命前缀含 {len(bad_evidence)} 条缺真实证据引用的发现"
                  f"（无存在的 `文件:行号`，或非 `test_*`+复现标记）——"
                  f"countable_findings[:{credited}] 必须逐条具备证据，拒绝结算。")
            for b in bad_evidence[:5]:
                print(f"  ✗ 无证据: {b[:100]}")
            return 1
    # 去重必须覆盖同一 findings 文件内部；否则把同一条真实 Bug 重复五次
    # 仍可把 credited 设为 5，绕过仅检查历史的旧门禁。与证据校验同源，
    # 只对计命前缀 findings[:credited] 计算指纹。
    if credited:
        try:
            from core.ledger.life_ledger import finding_fingerprint
            fingerprints = [finding_fingerprint(item) for item in countable_findings[:credited]]
        except (ImportError, TypeError, ValueError) as exc:
            print(f"[verify_life] finding 身份无法计算: {exc}")
            return 1
        if len(set(fingerprints)) < credited:
            print("[verify_life] 本轮 findings 含重复 finding，credited 超过唯一发现数，拒绝结算")
            return 1
    ledger = None
    ledger_state = None
    ledger_findings = countable_findings[:credited]
    if credited and ledger_path is None:
        # ledger 是计命的强制防线，调用方不能通过省略 --ledger 选择性绕过。
        ledger_path = str(LIFE_FILE.with_name("finding-ledger.json"))
    if ledger_path and credited:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from core.ledger.life_ledger import FindingLedger, finding_fingerprint

            candidate = _resolve_ledger_path(ledger_path)
            ledger = FindingLedger(candidate)
            ledger_state = ledger.read()
            existing = set(ledger_state.get("findings", {}))
            duplicate_fingerprints = [
                finding_fingerprint(item) for item in ledger_findings
                if finding_fingerprint(item) in existing
            ]
            if duplicate_fingerprints:
                # 跨 Life 重放（2026-11-09 实测修复）：同指纹在上一 Life 已入账
                # 的 finding 再次提交是**幂等重复**，不计新命（从 credited 扣除）
                # 而不是拒绝整个结算——拒绝会让 auto 续轮场景死锁（同一批
                # findings 重放是自动重建 findings.json 的正常产物）。本轮内部
                # 重复仍由上方 set(fingerprints) 防线拒绝（伪造防线不变）。
                dup_set = set(duplicate_fingerprints)
                fresh_findings = [
                    item for item in ledger_findings
                    if finding_fingerprint(item) not in dup_set
                ]
                deduped = len(ledger_findings) - len(fresh_findings)
                print(
                    "[verify_life] 全局 finding ledger 跨 Life 重复 "
                    f"{deduped} 条已幂等扣除（credited {credited} → "
                    f"{len(fresh_findings)}），继续结算"
                )
                credited = len(fresh_findings)
                ledger_findings = fresh_findings
                # WAL 的 fingerprints 与前缀同源：同步到幂等扣除后的清单，
                # 否则 WAL 记录含已入账指纹（审计口径不一致）。
                fingerprints = [finding_fingerprint(item) for item in fresh_findings]
        except (ImportError, OSError, ValueError, KeyError) as exc:
            print(f"[verify_life] 全局 finding ledger 无法读取: {exc}")
            return 1
    previous = json.loads(json.dumps(d, ensure_ascii=False))
    delta = -1 + credited - fraud
    d["life"] = d["life"] + delta
    d["found_total"] = d["found_total"] + credited
    d["rounds_completed"] += 1
    d["round"] = d["rounds_completed"] + 1
    d["alive"] = d["life"] > 0
    d["history"].append({
        "round": round_no,
        "ts": ts or time.strftime("%Y-%m-%d"),
        "delta": delta,
        "credited": credited,
        "life_after": d["life"],
        "findings": findings,
    })
    settlement_id = uuid.uuid4().hex
    wal_payload = {
        "schema_version": SETTLEMENT_WAL_SCHEMA,
        "settlement_id": settlement_id,
        "created_at": time.time(),
        "before_hash": _state_digest(previous),
        "after_hash": _state_digest(d),
        "before_state": previous,
        "after_state": d,
        "findings": ledger_findings,
        "finding_fingerprints": fingerprints,
        "round": round_no,
        "timestamp": ts,
        "ledger_path": str(ledger.path) if ledger is not None else None,
        "ledger_version": int(ledger_state["version"]) if ledger_state is not None else None,
    }
    _write_json_durable(
        _settlement_wal_path(), wal_payload, prefix=".settlement-wal-",
    )
    _settlement_failpoint("after-wal")
    if ledger is not None and ledger_state is not None:
        try:
            result = ledger.settle(
                ledger_findings,
                round=round_no,
                worker="verify_life",
                timestamp=ts,
                expected_version=int(ledger_state["version"]),
                settlement_id=settlement_id,
            )
            if result.conflicts or result.duplicate_count or result.accepted_count != credited:
                print("[verify_life] 全局 finding ledger 并发冲突或计命不一致，保留 pending settlement")
                return 1
        except (OSError, ValueError, KeyError) as exc:
            print(f"[verify_life] 全局 finding ledger 写入失败，保留 pending settlement: {exc}")
            return 1
    _settlement_failpoint("after-ledger")
    _save_atomic(d)
    _settlement_failpoint("after-life")
    _clear_settlement_wal()
    # 自证：settle 写出的状态必须通过 check
    errs2 = check_errors(d)
    if errs2:
        print("[verify_life] settle 后校验失败（内部错误，请上报）:")
        for e in errs2:
            print(f"  ✗ {e}")
        return 1
    print(
        f"[verify_life] 第 {round_no} 轮结算完成: "
        f"delta={delta} (credited={credited}, fraud={fraud}) "
        f"life={d['life']} found_total={d['found_total']} "
        f"rounds_completed={d['rounds_completed']} alive={d['alive']}"
    )
    print(f"  findings 记录: {len(findings)} 条（含超额/重复，计命 {credited} 条）")
    return 0


def cmd_settle(argv: list[str]) -> int:
    """Serialize settlement so parallel workers cannot lose or duplicate a round."""
    return _run_locked(lambda: _cmd_settle_unlocked(argv))


def _print_help() -> int:
    """Print the life-state commands without reading or modifying state."""
    print(
        """BugX verify_life 寿命状态校验器

用法
  python3 verify_life.py <命令> [选项]

命令
  check                       校验寿命状态一致性
  repair                      修复不一致并记录审计日志
  reset                       重置为初始状态（受确认保护）
  set-mode --mode MODE        设置修复模式（auto 或 log-only）
  snapshot                    保存当前状态快照
  diff                        对比当前状态与快照/外部基线
  restore                     从快照恢复当前状态
  settle [选项]               结算当前轮次
  selfhash                    输出校验器自身 SHA256
  help | --help | -h          显示本说明

返回码
  0 成功   1 校验/业务失败   2 参数或命令无效   3 校验器被篡改   4 锁等待超时
"""
    )
    return 0


def main(argv: list[str]) -> int:
    # 自校验前置：脚本被篡改则拒绝执行（漏洞 2 防线）。
    # --selfhash 也走这里（self_check 内处理）。
    self_check()
    args = argv[1:]
    cmd = args[0] if args else "check"
    if any(arg in {"help", "--help", "-h"} for arg in args):
        return _print_help()
    try:
        if cmd == "settle":
            return cmd_settle(args[1:])
        if cmd == "set-mode":
            return cmd_set_mode(args[1:])
        if cmd == "reset":
            return cmd_reset(args[1:])
        fn = {
            "check": cmd_check,
            "repair": cmd_repair,
            "snapshot": cmd_snapshot,
            "diff": cmd_diff,
            "restore": cmd_restore,
        }.get(cmd)
        if fn is None:
            print(
                "[verify_life] 未知命令: "
                f"{cmd}（可选 check/repair/reset/set-mode/snapshot/diff/restore/settle/selfhash）"
            )
            return 2
        return fn()
    except TimeoutError as exc:
        # _life_lock 超时（DOC-031 锁统一）：带锁路径与持有提示的受控失败，
        # 不以裸 traceback 暴露给调用方。exit 4 = 寿命锁超时。
        print(f"[verify_life] ✗ {exc}")
        return 4


def _run_direct(*args: str) -> int:
    """进程内直接调用入口：执行 verify_life 命令并返回退出码。"""
    try:
        return main(["verify_life.py", *args])
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
