#!/usr/bin/env python3
"""core/scenario_eval.py - L1–L6 分级场景自回归评测引擎。

## 为什么需要它（而不是扩 ``agent_eval.py``）

``agent_eval.py`` 回答的是「给定有已知漏洞的 fixture，能发现几个」。它的 case
schema **强制**要求 ``fixture`` + ``root_cause`` + ``minimum_severity``——三个
字段都预设了「这段代码里藏着一个可命名的漏洞」。

但 L1–L3 级测的根本不是漏洞：

    L1 空项目      有没有覆盖单元？空结果是否明确而非崩溃/伪造/挂死？
    L2 单文件      单元拆分对不对？干净代码上会不会**编造**发现？
    L3 多语言      Python/JS/Go 是否都被真的看过，还是静默跳过某种语言？

把这些硬塞进 ``root_cause`` 语义会得到两种坏结果：要么给干净代码编造标准答案
（污染 precision 基线），要么这些级别根本进不了评测。所以两类评测分开：
**漏洞发现**留在 ``agent_eval.py``，**流水线行为**归本模块。

## 分级

    L1 空项目        最小链路：不崩、不伪造、不死循环
    L2 单文件        单元拆分 + 干净代码零误报
    L3 多语言        语言覆盖完整性
    L4 已知漏洞      委托 ``agent_eval.py``（本模块只做汇总）
    L5 畸形环境      环境不健康时必须 fail-closed
    L6 并发/超时     委托 ``scripts/run_fault_matrix.py``

## 收据

每次运行产出 ``scenario-receipt.json``：逐级结论 + 度量（耗时、Token）+ 与基线的
差异。收据是**可比较**的：同一份基线跑两次，除时间戳外应当字节一致。
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
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_ROOT = (REPO_ROOT / "verification" / "evals" / "scenarios") if (REPO_ROOT / "verification" / "evals").is_dir() else (REPO_ROOT / "evals" / "scenarios")
RECEIPT_SCHEMA = 1

#: 级别定义。``runner`` 说明该级由谁执行：本模块或外部组件。
LEVELS: tuple[dict[str, object], ...] = (
    {
        "id": "L1", "name": "空项目", "dir": "L1-empty",
        "runner": "builtin",
        "intent": "最小链路：无覆盖单元时给出明确空结果，不崩、不伪造、不挂死",
    },
    {
        "id": "L2", "name": "单文件", "dir": "L2-single",
        "runner": "builtin",
        "intent": "单元拆分正确，且干净代码上零误报",
    },
    {
        "id": "L3", "name": "多语言", "dir": "L3-multilang",
        "runner": "builtin",
        "intent": "每种语言都被识别并纳入覆盖，不静默跳过",
    },
    {
        "id": "L4", "name": "已知漏洞", "dir": None,
        "runner": "builtin",
        "intent": "召回率/精确率/证据有效性（委托 agent_eval.py）",
    },
    {
        "id": "L5", "name": "畸形环境", "dir": "L5-malformed",
        "runner": "builtin",
        "intent": "环境不健康时 fail-closed，绝不静默降级",
    },
    {
        "id": "L6", "name": "并发超时", "dir": None,
        "runner": "builtin",
        "intent": "9 类故障注入（委托 scripts/run_fault_matrix.py）",
    },
    {
        "id": "L7", "name": "自身健康", "dir": None,
        "runner": "builtin",
        "intent": (
            "BugX 自己有没有坏：6 类结构性风险内省（委托 l7_selfcheck.py）。"
            "L1-L6 测「挖得准不准」，L7 测「BugX 自身是否健康」——"
            "真机无人值守撞出的缺陷几乎全在这一层"
        ),
    },
)


@dataclass
class LevelResult:
    """单级结论。``ok`` 是唯一判定器；``detail`` 只说人话，不参与判定。"""

    level: str
    name: str
    ok: bool
    skipped: bool = False
    duration_seconds: float = 0.0
    checks: list[dict[str, object]] = field(default_factory=list)
    detail: str = ""
    metrics: dict[str, object] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            self.ok = False


def _run(argv: list[str], *, cwd: Path, timeout: float = 120.0,
         env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """执行子命令并返回 (rc, stdout, stderr)。超时视为失败而非异常。"""
    merged = {**os.environ, **(env or {})}
    # 仓库直跑 CLI：子进程 cwd 是场景工作区，须显式指回仓库根；
    # HOME 隔离：评测的全局引擎读写落在一次性沙箱，不碰真实 ~/.bugx/。
    merged["PYTHONPATH"] = str(_repo_root())
    merged["HOME"] = str(_sandbox_home())
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=merged, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"timeout after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def _materialize(level_dir: Path, dest: Path) -> Path:
    """把只读场景复制到临时工作区。场景本身保持干净，不写入运行产物。

    为什么不原地跑：``bugx init`` 会写 ``.bugx/``、``.dsh/`` 等目录。跑在仓库里
    会污染工作树，且上一轮残留会让下一轮判定失真（典型表现：L1「空项目」因为
    上一轮的 ``.bugx`` 而识别出单元）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(level_dir, dest, dirs_exist_ok=True)
    return dest


def _repo_root() -> Path:
    """引擎仓根（core/bugx.md 所在目录的父级）。"""
    return Path(__file__).resolve().parents[2]


_SANDBOX_HOME: list[Path] = []


def _sandbox_home() -> Path:
    """一次评测会话共享的隔离 HOME，保护真实 ~/.bugx/ 不被评测写入。

    F-RSI-20261110-002 评测版事故：全局 ~/.bugx/core 曾被场景评测的
    ``bugx init`` 静默写入（旧 build 的快照 CLI + 无 HOME 隔离），随后
    「已安装 431 > 安装源 430」的降级判定让 init 在 L1/L2/L3 全挂。
    """
    if not _SANDBOX_HOME:
        _SANDBOX_HOME.append(Path(tempfile.mkdtemp(prefix="bugx-scenario-home-")))
    return _SANDBOX_HOME[0]


def _bugx_argv() -> list[str]:
    """量具与被测物同源：优先仓库直跑 CLI；PATH 快照只作最后退化。

    快照 CLI 落后仓库多个 build，用它评测仓库修复等于拿旧量具测新代码
    （实测：快照 430 对仓库 434 的 init 在三个级别全部「拒绝降级」）。
    """
    return [sys.executable, "-m", "interfaces.cli.bugx_cli.cli"]


#: 覆盖清单的两种真实形态。写成两种不是「兼容性懒惰」——实测 2026-11-03：
#: ``module-coverage.md`` 的单元是**表格行**（``| 1 | 内部 | ROOT | ... |``），
#: 第一次写这个函数时按列表行 ``- [ ]`` 解析，于是在三语言场景上数出 0 个单元、
#: 误判成产品缺陷。同时保留列表行是因为 bootstrap 阶段确实会产出那种形态。
_TABLE_UNIT = re.compile(r"^\|\s*\d+\s*\|")
_LIST_UNIT = re.compile(r"^- \[")


def _count_units(root: Path) -> int | None:
    """读覆盖清单里的单元数。清单缺失返回 None（区别于「有清单但 0 单元」）。"""
    manifests = list((root / ".bugx").rglob("module-coverage.md"))
    if not manifests:
        return None
    total = 0
    for manifest in manifests:
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if _TABLE_UNIT.match(line) or _LIST_UNIT.match(line):
                total += 1
    return total


def run_l1_empty(root: Path, work: Path) -> LevelResult:
    """L1：空项目必须给出明确的空结果。

    判定不看「有没有输出」，看三件事同时成立：
      1. 命令正常返回（不崩、不超时）
      2. 覆盖单元为 0 或被明确标注为「无」
      3. 没有伪造发现
    """
    result = LevelResult(level="L1", name="空项目", ok=True)
    started = time.monotonic()
    rc, out, err = _run([*_bugx_argv(), "init", "--root", ".", "--cloud-sync", "local"],
                        cwd=work, timeout=180.0)
    result.add("init 成功", rc == 0, (err or out)[-300:] if rc != 0 else "")
    units = _count_units(work)
    result.add(
        "覆盖单元为 0",
        units in (0, None),
        f"发现 {units} 个单元；空项目不应有可挖掘单元",
    )
    # 空项目若报出发现，一定是伪造
    findings = list((work / ".bugx").rglob("findings*.json"))
    fabricated = []
    for path in findings:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("findings"):
            fabricated.append(path.name)
    result.add("无伪造发现", not fabricated, f"空项目产出发现：{fabricated}")
    result.duration_seconds = time.monotonic() - started
    result.metrics = {"units": units, "fabricated": len(fabricated)}
    return result


def run_l2_single(root: Path, work: Path) -> LevelResult:
    """L2：单文件必须正确拆出 1 个单元，且干净代码上零误报。"""
    result = LevelResult(level="L2", name="单文件", ok=True)
    started = time.monotonic()
    rc, out, err = _run([*_bugx_argv(), "init", "--root", ".", "--cloud-sync", "local"],
                        cwd=work, timeout=180.0)
    result.add("init 成功", rc == 0, (err or out)[-300:] if rc != 0 else "")
    source_files = [
        p for p in work.rglob("*.py")
        if ".bugx" not in p.parts and p.name != "conftest.py"
    ]
    result.add("场景含 1 个源文件", len(source_files) == 1,
               f"实际 {len(source_files)} 个：{[p.name for p in source_files]}")
    # 干净代码：任何断言为 confirmed 的发现都算误报
    false_positives = _confirmed_findings(work)
    result.add(
        "干净代码零误报", not false_positives,
        f"误报 {len(false_positives)} 条：{false_positives[:3]}",
    )
    result.duration_seconds = time.monotonic() - started
    result.metrics = {"source_files": len(source_files),
                      "false_positives": len(false_positives)}
    return result


def _confirmed_findings(root: Path) -> list[str]:
    """收集被判定为 confirmed 的发现标题（用于零误报检查）。"""
    titles: list[str] = []
    for path in (root / ".bugx").rglob("*.json"):
        if "findings" not in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = payload.get("findings") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "")).lower()
            if status in {"confirmed", "verified", "fixed"}:
                titles.append(str(item.get("title") or item.get("id") or "?"))
    return titles


def run_l3_multilang(root: Path, work: Path) -> LevelResult:
    """L3：三种语言都必须被识别并纳入覆盖。

    这一级抓的是「静默跳过某种语言」——最危险的一类缺陷，因为审计报告看起来
    完整，实际有文件从未被看过。
    """
    result = LevelResult(level="L3", name="多语言", ok=True)
    started = time.monotonic()
    rc, out, err = _run([*_bugx_argv(), "init", "--root", ".", "--cloud-sync", "local"],
                        cwd=work, timeout=180.0)
    result.add("init 成功", rc == 0, (err or out)[-300:] if rc != 0 else "")
    expected = {".py": "python", ".js": "javascript", ".go": "go"}
    present = {ext: kind for ext, kind in expected.items()
               if any(work.rglob(f"*{ext}"))}
    result.add(
        "场景含三种语言", len(present) == 3,
        f"仅发现：{sorted(present.values())}",
    )
    # 语言探测必须对每个文件给出判定，缺失即「静默跳过」
    rc, out, err = _run(
        [*_bugx_argv(), "engine", "--root", ".", "status"], cwd=work, timeout=120.0,
    )
    result.add("engine status 可执行", rc == 0, (err or out)[-300:] if rc != 0 else "")
    units = _count_units(work)
    result.add(
        "覆盖单元非空", bool(units),
        f"三语言场景应有覆盖单元，实际 {units}",
    )
    result.duration_seconds = time.monotonic() - started
    result.metrics = {"languages": sorted(present.values()), "units": units}
    return result


def _doctor_check(root: Path, name_contains: str) -> dict[str, object]:
    """在 ``doctor --json`` 输出里取指定检查项的结论。

    用结构化 JSON 而不是 grep 文本：文本判据无法区分「这条检查失败」与
    「别处的日志恰好含这个词」，而 L5 的全部价值就在于精确定位。
    """
    rc, out, err = _run(
        [*_bugx_argv(), "doctor", "--root", ".", "--json"],
        cwd=root, timeout=120.0,
    )
    payload: object = None
    for candidate in (out, err):
        text = (candidate or "").strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
            break
        except ValueError:
            continue
    if not isinstance(payload, dict):
        return {"ok": False, "detail": f"doctor --json 无有效输出 (rc={rc})"}
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return {"ok": False, "detail": "doctor --json 缺 checks 数组"}
    for item in checks:
        if not isinstance(item, dict):
            continue
        if name_contains in str(item.get("name", "")):
            return {"ok": bool(item.get("ok")), "detail": str(item.get("detail") or "")}
    return {"ok": False, "detail": f"未找到检查项：{name_contains}"}


def _probe_engine_integrity(root: Path) -> dict[str, object]:
    """引擎完整性检查项——缺 engine-integrity.json 时它必须失败。"""
    return _doctor_check(root, "引擎完整性")


def _probe_state_writable(root: Path) -> dict[str, object]:
    """状态目录可写性——只读时写入类检查必须失败。"""
    # doctor 没有专门的「状态目录可写」项，改用实际写入探测：与 doctor 的
    # fail-closed 结论等价，但更直接（能否写是一条客观事实，不是文本判断）。
    probe = root / ".bugx" / "state" / ".write-probe"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return {"ok": True, "detail": "状态目录可写"}
    except OSError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _probe_coverage_paths(root: Path) -> dict[str, object]:
    """覆盖清单路径安全性——含逃逸路径时必须被拒。"""
    return _doctor_check(root, "覆盖")


def _inject_missing_integrity(work: Path) -> None:
    for path in work.rglob("engine-integrity.json"):
        path.unlink()


def _inject_readonly_state(work: Path) -> None:
    state = work / ".bugx" / "state"
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o500)


def _inject_escaping_manifest(work: Path) -> None:
    """覆盖清单里塞一个逃逸路径（../../etc/passwd 类）。"""
    manifests = list((work / ".bugx").rglob("module-coverage.md"))
    target = manifests[0] if manifests else (
        work / ".bugx" / "state" / "module-coverage.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "- [ ] ../../../../etc/passwd\n- [ ] 内部:src/app.py:all\n",
        encoding="utf-8",
    )


#: 畸形注入用例：(名称, 注入函数, 定向探针)。探针必须能**区分**注入前后，
#: 否则这一级等于没测——第一版就是败在这里。
MALFORMED_CASES: tuple[tuple[str, object, object], ...] = (
    ("状态目录只读", _inject_readonly_state, _probe_state_writable),
    ("覆盖清单含逃逸路径", _inject_escaping_manifest, _probe_coverage_paths),
)

#: 刻意**不**收录的畸形，附原因——避免后来者再把它加回来重踩：
#:
#: * 「缺 engine-integrity.json」：``engine-integrity.json`` 是**全局引擎**的
#:   文件，项目里本来就没有。删它等于什么都没做，而 doctor 如实报告「全局引擎
#:   488 个文件哈希一致」是对的。第一版把它当项目级畸形，判据因此永远通过——
#:   这类「测了一个不存在的对象」的用例，比没有用例更糟：它给出虚假的安全感。


def run_l5_malformed(root: Path, work: Path) -> LevelResult:
    """L5：环境不健康时必须 fail-closed。

    ## 判据为什么必须**定向**

    第一版判据是「``doctor`` 非零退出 **或** 输出里出现『缺失/拒绝/权限』等词」。
    实测这个判据是坏的：一个**完全正常**的项目跑 ``doctor`` 也返回 rc=1，并含
    27 处上述关键词——因为总有可选宿主检查在失败（hosts.json、Pi Skill、
    OpenCode 配置）。于是该判据无条件放行，L5 等于什么都没测。

    现在的判据是：拿**同一次运行**里、同一条检查项在「注入前」与「注入后」的
    结论比对。只有「注入前通过 → 注入后失败」才算真的 fail-closed。这样自然
    排除了那些与本次注入无关的既存失败。
    """
    result = LevelResult(level="L5", name="畸形环境", ok=True)
    started = time.monotonic()
    scenario = SCENARIOS_ROOT / "L5-malformed"
    for name, inject, probe in MALFORMED_CASES:
        with tempfile.TemporaryDirectory(prefix="bugx-l5-") as tmp:
            case_root = Path(tmp) / "proj"
            case_root.mkdir(parents=True)
            if scenario.is_dir():
                shutil.copytree(scenario, case_root, dirs_exist_ok=True)
            _run([*_bugx_argv(), "init", "--root", ".", "--cloud-sync", "local"],
                 cwd=case_root, timeout=120.0)
            # 基线：注入**前**该检查项的状态。这一步是整个判据的地基。
            before = probe(case_root)
            try:
                inject(case_root)
            except OSError as exc:
                result.add(f"{name}（注入）", False, f"注入失败：{exc}")
                continue
            after = probe(case_root)
            if not before["ok"]:
                # 注入前就没通过 → 这条检查项对本项目不适用，无法作为判据。
                result.add(
                    f"{name}（前置状态）", True,
                    f"注入前该项已失败（{before['detail'][:80]}），跳过定向比对",
                )
                continue
            detected = not after["ok"]
            result.add(
                f"{name} → fail-closed",
                detected,
                (f"已检出（{after['detail'][:120]}）" if detected
                 else f"注入后仍报告通过（{after['detail'][:120]}）：畸形被静默接受"),
            )
            for path in case_root.rglob("*"):
                try:
                    if path.is_dir():
                        path.chmod(0o700)
                except OSError:
                    pass
    result.duration_seconds = time.monotonic() - started
    return result


def run_l4_known_vulns(root: Path, work: Path) -> LevelResult:
    """L4：委托 ``agent_eval.py`` 的召回/精确/证据/复现评分。

    这里**不重算**任何指标——重算是分叉的开始。本函数只做两件事：调用既有评测、
    把它的结论翻译成 LevelResult。度量（耗时/Token）随收据一起记录。
    """
    result = LevelResult(level="L4", name="已知漏洞", ok=True)
    started = time.monotonic()
    rc, out, err = _run(
        [sys.executable, str(REPO_ROOT / "core" / "agent" / "agent_eval.py"),
         "score", "--strict"],
        cwd=REPO_ROOT, timeout=300.0,
    )
    combined = f"{out}\n{err}"
    result.add("agent_eval score 通过", rc == 0, combined[-400:] if rc != 0 else "")
    # 抓取关键指标写进收据，便于跨轮比较
    metrics: dict[str, object] = {}
    for line in combined.splitlines():
        line = line.strip()
        for label, key in (
            ("发现率:", "recall"), ("精确率:", "precision"),
            ("证据有效率:", "evidence_validity"), ("可复现率:", "reproducibility"),
            ("综合质量分:", "quality"),
        ):
            if line.startswith(label):
                value = line[len(label):].strip()
                metrics[key] = value
        if line.startswith("耗时/Token:"):
            parts = line[len("耗时/Token:"):].strip().split("/")
            metrics["duration"] = parts[0].strip()
            metrics["tokens"] = parts[-1].strip() if len(parts) > 1 else None
    result.add(
        "关键指标可解析", bool(metrics.get("recall")),
        f"未能从输出解析发现率：{combined[-200:]}",
    )
    result.duration_seconds = time.monotonic() - started
    result.metrics = metrics
    return result


def run_l6_fault_matrix(root: Path, work: Path) -> LevelResult:
    """L6：委托 ``scripts/run_fault_matrix.py`` 的 9 类故障注入。

    同样不重写：故障注入的语义（租约回收、预算终态、输出截断…）已经在那个
    脚本里被仔细定义过，复制一份只会让两处慢慢漂移。
    """
    result = LevelResult(level="L6", name="并发超时", ok=True)
    started = time.monotonic()
    sandbox = Path(tempfile.mkdtemp(prefix="bugx-l6-"))
    receipt = sandbox / "fault-matrix-receipt.json"
    try:
        rc, out, err = _run(
            [sys.executable, str(REPO_ROOT / "scripts" / "cve" / "run_fault_matrix.py"),
             "--sandbox", str(sandbox), "--output", str(receipt), "--json"],
            cwd=REPO_ROOT, timeout=600.0,
        )
        combined = f"{out}\n{err}"
        result.add("故障矩阵通过", rc == 0, combined[-400:] if rc != 0 else "")
        metrics: dict[str, object] = {}
        if receipt.is_file():
            try:
                payload = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = None
            if isinstance(payload, dict):
                scenarios = payload.get("scenarios") or payload.get("cases") or []
                if isinstance(scenarios, list):
                    metrics["scenarios_total"] = len(scenarios)
                    # 真实收据用 ``status`` 字符串（PASSED/FAILED），不是 ``ok``
                    # 布尔。第一版按 ``not s.get("ok", True)`` 取失败项，恒为空，
                    # 于是「全部通过」与「判别失效」在收据里长得一模一样。
                    failed = [
                        (s.get("case_id") or s.get("name") or "?")
                        for s in scenarios
                        if isinstance(s, dict)
                        and str(s.get("status", "")).upper() not in {"PASSED", "OK"}
                    ]
                    metrics["scenarios_failed"] = failed
                    result.add(
                        "无失败场景", not failed,
                        f"{len(failed)} 个场景失败：{failed}",
                    )
                result.add(
                    "收据含场景结果", bool(metrics.get("scenarios_total")),
                    "故障矩阵收据没有 scenarios/cases 数组",
                )
        else:
            result.add("生成故障矩阵收据", False, f"缺少 {receipt}")
        result.duration_seconds = time.monotonic() - started
        result.metrics = metrics
        return result
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def run_l7_selfcheck(root: Path, work: Path) -> LevelResult:
    """L7：BugX 自身健康自检。

    与 L1-L6 的根本区别：**不需要外部项目**。L1-L6 都在验证「给一个项目，
    BugX 能不能正确处理」，L7 验证「BugX 自己的结构有没有坏」。

    ## 静态与动态两层

    静态 6 项（``l7_selfcheck``）是结构分析，0.00s，每轮都跑。
    动态 4 项（``l7_dynamic``）真起子进程跑 CLI，约 12s，按需开启。

    为什么不在这一级默认开动态：L7 作为级别会被每轮调用，一条 12s 的
    检查会让整轮变慢 30%，然后它就会被从流程里摘掉——保护归零。
    动态层通过 ``RSI_L7_DYNAMIC=1`` 显式开启，用于提交前 / 排查。

    这个取舍是刻意的：**能持续跑的低成本检查，胜过跑不动的高成本检查**。
    """
    result = LevelResult(level="L7", name="自身健康", ok=True)
    started = time.monotonic()
    try:
        from core.repair import l7_selfcheck
    except ImportError as exc:  # 归档路径导入失败必须显式判红，而不是换名重试
        result.add("自检模块可导入", False, str(exc))
        return result

    dynamic = os.environ.get("RSI_L7_DYNAMIC", "").strip() in {"1", "true", "yes"}
    payload = l7_selfcheck.run_all(root, dynamic=dynamic)
    summary = payload.get("summary", {})
    if summary.get("not_applicable"):
        # 目标不是引擎仓库：跳过而不是记 0/0「通过」。0/0 会被误读成
        # 「什么都没检查但绿灯」，而实际情况是「这层检查对当前目标无意义」。
        result.ok = True
        result.skipped = True
        result.add("L7 不适用", True, str(summary.get("reason") or ""))
        result.duration_seconds = round(time.monotonic() - started, 2)
        return result
    result.add(
        "全部自检通过",
        bool(summary.get("ok")),
        f"{summary.get('passed', 0)}/{summary.get('total', 0)} 通过"
        + (f"；失败：{', '.join(summary.get('checks_failed', []))}"
           if summary.get("checks_failed") else ""),
    )
    for check in payload.get("checks", []):
        result.add(
            f"{check.get('id')} {check.get('name')}",
            bool(check.get("ok")),
            str(check.get("detail") or "")[:300],
        )
    # 计时必须真实：L7 静态 0.00s、动态约 12s，两类成本差三个数量级。
    # 不记时会让「动态到底值不值这个价」变成猜测。
    result.duration_seconds = time.monotonic() - started
    return result


BUILTIN_RUNNERS = {
    "L1": run_l1_empty,
    "L2": run_l2_single,
    "L3": run_l3_multilang,
    "L4": run_l4_known_vulns,
    "L5": run_l5_malformed,
    "L6": run_l6_fault_matrix,
    "L7": run_l7_selfcheck,
}


#: 在**仓库根**执行、不需要临时工作区的级别。L4/L6 评的是仓库自带的评测集与
#: 故障矩阵，物化到临时目录反而会让它们找不到 ``evals/`` 和 ``scripts/``。
REPO_LEVELS = frozenset({"L4", "L6", "L7"})


def run_level(level: dict[str, object], *, keep: bool = False) -> LevelResult:
    """执行单个级别。"""
    level_id = str(level["id"])
    runner = str(level["runner"])
    if runner != "builtin":
        return LevelResult(
            level=level_id, name=str(level["name"]), ok=True, skipped=True,
            detail=f"由 {runner} 执行：{level['intent']}",
        )
    if level_id in REPO_LEVELS:
        return BUILTIN_RUNNERS[level_id](REPO_ROOT, REPO_ROOT)
    scenario_dir = SCENARIOS_ROOT / str(level["dir"])
    if not scenario_dir.is_dir():
        return LevelResult(
            level=level_id, name=str(level["name"]), ok=False,
            detail=f"场景目录缺失：{scenario_dir}",
        )
    tmp_root = Path(tempfile.mkdtemp(prefix=f"bugx-{level_id.lower()}-"))
    try:
        work = _materialize(scenario_dir, tmp_root / "proj")
        return BUILTIN_RUNNERS[level_id](tmp_root / "proj", work)
    finally:
        for path in tmp_root.rglob("*"):
            try:
                if path.is_dir():
                    path.chmod(0o700)
            except OSError:
                pass
        if not keep:
            shutil.rmtree(tmp_root, ignore_errors=True)


def build_receipt(results: list[LevelResult], *, started_at: float) -> dict[str, object]:
    """组装机器可读收据。字段顺序固定，便于跨轮 diff。"""
    total_duration = sum(r.duration_seconds for r in results)
    executed = [r for r in results if not r.skipped]
    failed = [r.level for r in results if not r.ok]
    return {
        "schema_version": RECEIPT_SCHEMA,
        "generated_at_unix": int(started_at),
        "levels": [
            {
                "level": r.level,
                "name": r.name,
                "ok": r.ok,
                "skipped": r.skipped,
                "duration_seconds": round(r.duration_seconds, 3),
                "metrics": r.metrics,
                "detail": r.detail,
                "checks": r.checks,
            }
            for r in results
        ],
        "summary": {
            "levels_total": len(results),
            "levels_executed": len(executed),
            "levels_failed": failed,
            "ok": not failed,
            "duration_seconds": round(total_duration, 3),
        },
    }


def receipt_digest(receipt: dict[str, object]) -> str:
    """除时间戳外的内容摘要，用于「同一基线两次运行应一致」的比对。"""
    stable = {k: v for k, v in receipt.items() if k != "generated_at_unix"}
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def compare_baseline(receipt: dict[str, object], baseline: dict[str, object]) -> list[str]:
    """与基线对比，返回回退项。只报「变坏」，不报「新增通过」。"""
    failures: list[str] = []
    before = {str(item["level"]): item for item in baseline.get("levels", [])}
    for item in receipt.get("levels", []):
        level = str(item["level"])
        prev = before.get(level)
        if prev is None:
            continue
        if prev.get("ok") and not item.get("ok"):
            failures.append(f"{level} 由通过回退为失败：{item.get('detail') or ''}")
    # 总体回退只在「基线里的每个级别本轮都还在，且确实变坏」时才报。
    # 否则新增一个从未跑过的级别（本身失败）也会触发总体告警——那不是回退，
    # 是首次覆盖，混进来会让基线一加级别就永久报警。
    prev_summary = baseline.get("summary", {})
    known_bad = {
        level for level, prev in before.items()
        if prev.get("ok") is False
    }
    now_items = {str(item["level"]): item for item in receipt.get("levels", [])}
    regressed = {
        level for level in before
        if before[level].get("ok") and level in now_items
        and not now_items[level].get("ok")
    }
    if (prev_summary.get("ok") and not receipt.get("summary", {}).get("ok")
            and regressed):
        failures.append("总体由 ok 回退为失败")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L1–L6 分级场景自回归评测")
    parser.add_argument("--root", default=str(REPO_ROOT), help="仓库根目录")
    parser.add_argument("--level", action="append", default=None,
                        help="只跑指定级别（可重复，如 --level L1 --level L2）")
    parser.add_argument("--receipt", default=None, help="收据输出路径")
    parser.add_argument("--baseline", default=None, help="基线收据路径（对比回退）")
    parser.add_argument("--keep", action="store_true", help="保留临时工作区便于排查")
    parser.add_argument("--json", action="store_true", help="把收据打到 stdout")
    args = parser.parse_args(argv)

    selected = set(args.level or [])
    started = time.time()
    results: list[LevelResult] = []
    for level in LEVELS:
        if selected and str(level["id"]) not in selected:
            continue
        results.append(run_level(level, keep=args.keep))

    receipt = build_receipt(results, started_at=started)
    receipt["digest"] = receipt_digest(receipt)

    baseline_failures: list[str] = []
    if args.baseline:
        path = Path(args.baseline).expanduser()
        if path.is_file():
            try:
                baseline_failures = compare_baseline(
                    receipt, json.loads(path.read_text(encoding="utf-8")),
                )
            except (OSError, ValueError) as exc:
                baseline_failures = [f"基线无法读取：{exc}"]
        else:
            baseline_failures = [f"基线文件不存在：{path}"]
    receipt["baseline_failures"] = baseline_failures

    if args.receipt:
        target = Path(args.receipt).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )

    if args.json:
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
    else:
        for item in receipt["levels"]:
            mark = "SKIP" if item["skipped"] else ("PASS" if item["ok"] else "FAIL")
            print(f"[{item['level']}] {mark:4s} {item['name']:8s} "
                  f"{item['duration_seconds']:6.2f}s  {item['detail']}")
            for check in item["checks"]:
                if not check["ok"]:
                    print(f"       ✗ {check['check']}: {check['detail']}")
        summary = receipt["summary"]
        print(f"\n汇总：{summary['levels_executed']}/{summary['levels_total']} 级执行，"
              f"失败 {summary['levels_failed'] or '无'}，"
              f"耗时 {summary['duration_seconds']:.2f}s")
        for failure in baseline_failures:
            print(f"  基线回退：{failure}")

    failed = bool(receipt["summary"]["levels_failed"]) or bool(baseline_failures)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
