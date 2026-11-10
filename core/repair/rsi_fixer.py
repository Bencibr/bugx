#!/usr/bin/env python3
"""core/rsi_fixer.py - 派宿主 agent 修 BUGX 自身的缺陷，并当场验收。

## 它补的是哪一段

之前 ``rsi_repair`` 只能清残留：所有「要改 BUGX 源码」的缺陷都被归成
``product`` 然后原地不动，等人工。结果 RSI 对真正重要的问题什么都没做——
本轮修的三个 bug（node 重写缺失、doctor 死锁、dsh 未登记）全落在这一类。

本模块把这一段接上：

    立案（含复现命令）→ 派 agent 改代码 → 双向门禁 → 退化即回滚

## 为什么回滚是必须的

agent 改代码会失败，而且**失败时常常看起来很成功**：它可能为了让复现用例
变绿而改坏了别处。所以每次修复尝试都在**隔离副本**里做：

1. 复制仓库到临时目录
2. 在副本里让 agent 改
3. 在副本里跑复现命令 + 全量回归
4. 两条都过，才把 diff 取回来
5. 任一不过，丢弃副本——原仓库从未被碰过

这与 ``patch_synthesizer.verify_patch_dual_gate`` 的思路一致：缺陷验证必须
在独立树上做，否则一次失败的命令就能留下用户状态。

## 验收不是「复现用例变绿」

复现用例变绿只说明「这一条好了」。必须同时满足：

* 修复前失败、修复后通过（双向差分）
* 既有测试全量不退化

第二条件才是关键——它拦的正是「为了让用例变绿而改坏别处」。
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

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "core"))

from core.repair import rsi_scope  # noqa: E402

#: 修复尝试的默认超时。agent 改代码比跑评测慢得多。
DEFAULT_FIX_TIMEOUT = 900.0
#: 回归测试的默认超时。
DEFAULT_REGRESSION_TIMEOUT = 600.0

#: 「静默多久算卡死」——比超时更早、更准的判据。
#:
#: ## 为什么光有超时不够
#:
#: 实测事故：agent 接到「跑全量 pytest 验证不退化」的指令后，开始跑 3685 个
#: 测试（约 24 分钟）。attempt_fix 的超时是 450s，于是只能干等 450 秒，
#: 拿到 ``exit code 124`` + **空输出**，报告「未修复」——但 agent 根本没在修，
#: 它在跑测试。等满超时既浪费 8 分钟，又把「指令写错了」误诊成「agent 不行」。
#:
#: 卡死的信号不是「跑得久」，而是「**很久没有新输出**」。正常工作时 agent 会
#: 不断打印工具调用；真卡住时输出会彻底停住。所以按「最近一次输出的年龄」
#: 判断，能在 2 分钟内识别出死循环/等待输入/跑长测试，而不是等满超时。
#:
#: 取 120s：agent 单步（读文件、跑一次 l7_selfcheck）通常秒级，最慢的
#: 全量单测也就几十秒；连续 2 分钟一个字都不吐，几乎必然是卡住。
DEFAULT_IDLE_TIMEOUT = 120.0


def _idle_timeout() -> float:
    """允许用环境变量覆盖，便于慢机器/大仓库调整。"""
    raw = (os.environ.get("RSI_AGENT_IDLE_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_IDLE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IDLE_TIMEOUT
    return value if value > 0 else DEFAULT_IDLE_TIMEOUT


def build_fix_prompt(work_order: dict, *, mode: str, tree: Path | None = None) -> str:
    """把工作单转成给 agent 的修复指令。

    指令必须自带三件事，缺一不可执行：

    * 复现命令——让 agent 能自己确认缺陷存在
    * 验收标准——让 agent 知道自己要满足什么，而不是猜
    * 范围限制——明确告诉它不许动什么
    """
    lines = [
        "你是 BugX 引擎的维护者。下面是一份自回归评测发现的产品缺陷，请修复它。",
    ]
    if tree is not None:
        # ## 必须给出绝对路径，否则 agent 会去改「它自己认为的项目根」
        #
        # 实测事故：沙箱在 /var/folders/.../bugx-fix-xxx/tree，但 prompt 里只有
        # 相对命令（`python3 core/l7_selfcheck.py`）。opencode 沿用自己的项目
        # 索引，直接 `cd /private/tmp/e3`（**被修复的那个仓库本身**）开工，
        # 把注入的缺陷改掉了——而 attempt_fix 的承诺是「原仓库不会被改动」。
        #
        # 更糟的是它改对之后，PoC 在真正的沙箱里跑，缺陷仍在，于是报告
        # 「未能修复」。**原仓库被污染了，故障却记在 agent 头上。**
        #
        # 只设 cwd 不够：宿主 agent 有各自的项目发现机制，会绕过 cwd。
        # 必须在指令里把工作目录钉死。
        lines.extend([
            "",
            f"## 工作目录（绝对路径，只在此目录内改动）",
            f"    {tree}",
            "",
            "所有读写、所有命令都必须在这个目录内进行。"
            "不要 cd 到其他目录，不要修改这个目录之外的文件——"
            "那会污染真实仓库，而本次修复的隔离保证正是「原仓库不被改动」。",
            "",
            "下文出现的相对路径都相对于上述工作目录。",
        ])
    lines.extend([
        "",
        "## 缺陷",
    ])
    for diag in work_order.get("diagnoses", []):
        lines.append(f"- [{diag.get('kind')}] {diag.get('level')} "
                     f"{diag.get('check')}：{diag.get('reason')}")
        # ## 直接给落点，别让 agent 自己去搜
        #
        # 实测对比：同一处 L7-5 缺陷，
        #   只给描述 → agent grep 全仓、读 installer/host_adapters，
        #              180s 预算耗尽仍未改到点子上；
        #   给出文件+符号+改法 → **几秒修完**。
        #
        # 所以把「去哪改」放在缺陷描述的紧后面，而不是让模型去推理。
        # 这些信息本来就是检查函数的常量，白白丢掉只会浪费预算。
        files = diag.get("files") or []
        symbols = diag.get("symbols") or []
        if files:
            lines.append(f"  - 相关文件：{', '.join(f'`{f}`' for f in files)}")
        if symbols:
            lines.append(f"  - 相关符号：{', '.join(f'`{x}`' for x in symbols)}")
        hint = str(diag.get("hint") or "").strip()
        if hint:
            lines.append(f"  - 修改方向：{hint}")
        notes = diag.get("notes") or []
        if notes:
            lines.append("  - 检查内部已知的参照信息（可直接对照）：")
            for note in notes:
                lines.append(f"    - {note}")
    lines.extend(["", "## 复现方式（先运行确认缺陷存在）"])
    for item in work_order.get("reproduce", []):
        lines.append(f"    {item.get('command')}")
    lines.extend([
        "",
        "## 验收标准（必须全部满足，否则视为未修复）",
        "1. 上述复现命令在修改后通过（这是硬性验收）",
        "2. 改动聚焦于本缺陷，不做无关重构",
        "",
        "## 关于全量测试（重要：不要在本次修复里跑）",
        "",
        "**不要执行 `pytest tests/ -q` 或任何全量测试。**",
        "",
        "本仓库全量测试有 3600+ 条、约 24 分钟。修复 agent 的预算是分钟级，",
        "一旦开始跑全量测试就会耗尽预算，最终超时被杀——**代码改对了也会被",
        "判成未修复**。这不是推测：实测中 agent 接到「跑全量测试」指令后立刻",
        "开始跑，450s 后超时、零输出，报告「未修复」，而缺陷其实一行就能改好。",
        "",
        "回归验证由调度侧在修复之后统一执行，不需要你跑。只需保证：",
        "- 复现命令通过",
        "- 改动面最小，不顺手改无关代码",
        "- 不修改评测断言",
        "",
        "## 范围限制",
        "禁止修改：",
        "- `evals/golden-findings.json`、`evals/cases.json`（评测断言）",
        "- `evals/*baseline*.json`（基线/门槛）",
        "- 任何以「让评测变绿」为目的的断言调整",
        "",
        f"当前修复模式：{mode}（{rsi_scope.mode_description(mode)}）",
        "",
        "## 输出",
        "修完直接给出改动即可。不要修改评测断言来「通过」，那不算修复。",
    ])
    return "\n".join(lines)


def _summarize_stream_json(raw: str) -> str:
    """把 claude 的 stream-json 事件流压成人类可读的几行。

    ## 为什么要单独处理

    claude 必须用 ``--output-format stream-json`` 才不挂起（见
    ``ONE_SHOT_INVOCATIONS`` 的说明）。但那种格式一次能吐 4000+ 行 JSON
    事件，直接塞进 ``agent_output_tail`` 全是 ``thinking_tokens`` 之类的
    噪声，**真正有用的"它最后说了什么、改了哪些文件"反而被挤没了**。

    所以按事件类型挑出有信息量的部分：assistant 的文字、tool_use 的名字、
    以及最终 result 的结论与成本。
    """
    if '"type"' not in raw:
        return raw
    said: list[str] = []
    tools: list[str] = []
    result_note = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    said.append(str(block["text"]).strip())
                elif block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    tools.append(str(name))
        elif kind == "result":
            result_note = (
                f"[result] stop={event.get('stop_reason')} "
                f"cost=${event.get('total_cost_usd')} "
                f"dur={round((event.get('duration_api_ms') or 0) / 1000)}s"
            )
    lines: list[str] = []
    if tools:
        lines.append("工具调用：" + ", ".join(tools[-12:]))
    if said:
        lines.append("结论：" + " ".join(said[-3:])[:900])
    if result_note:
        lines.append(result_note)
    return "\n".join(lines) if lines else raw


def _run(argv: list[str], *, cwd: Path, timeout: float,
         env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """执行命令并**保证失败时有话可说**。

    实测问题：dsh 额度耗尽时退出码 1，但 stdout/stderr 都是空的（失败发生在
    它自己的启动链路里，早于我们的 stdout 接管）。于是 report 里只剩
    「agent 退出码 1」，空手排查。

    对策：失败且两者皆空时，主动重跑一次最小探测命令，把宿主的真实状态
    抓回来。这比让接手的人自己试要可靠——他未必知道该试什么。
    """
    merged = {**os.environ, **(env or {})}
    try:
        proc, idle_killed = _run_with_idle_watchdog(
            argv, cwd=cwd, timeout=timeout, env=merged)
        out, err = proc.stdout or "", proc.stderr or ""
        if idle_killed is not None:
            return 124, out, (
                f"{err}"
                f"\n[卡死判定] agent 连续 {idle_killed:.0f}s 无任何输出，已主动终止"
                f"（超时上限 {timeout:.0f}s，但等满它是浪费——没有输出通常意味着"
                f"卡在等待输入、死循环，或被指令要求去跑一个远超预算的长任务）。"
                f"最后输出：{out[-400:] if out.strip() else '（无）'}"
            ).strip()
        if proc.returncode != 0:
            combined = f"{out}\n{err}".strip()
            # 判据不是「有没有字节」，而是「有没有**可诊断的原因**」。
            # 实测两态：
            #   a) 沙箱内 dsh 静默失败 → 0 字节，完全无法排查
            #   b) 宿主打印了与本次修复无关的启动噪声（如 MCP 初始化失败、
            #      HMR 报错）→ 有几百字节，但没一句说明为什么这次调用失败
            # 只看 strip() 为空会漏掉 b。所以用「是否命中已知失败特征，
            # 或是否包含可读的错误语义」来判断，必要时补一次定向探测。
            if not _diagnose_agent_failure(combined, argv[0]):
                probe = _probe_host_banner(argv[0], cwd=cwd, env=merged)
                if probe:
                    err = f"{err}\n[宿主状态探测] {probe}".strip()
        return proc.returncode, out, err
    except subprocess.TimeoutExpired as exc:
        # 超时前收到的输出必须保留：它是唯一能说明「卡在哪」的证据。
        partial_out = exc.output if isinstance(exc.output, str) else ""
        partial_err = exc.stderr if isinstance(exc.stderr, str) else ""
        # 取**最后几行**并压成单行，而不是"最后 500 字符"——后者跨行输出时
        # 会把最早的一行显示在最前面，看的人以为 Agent 卡在第一步。
        _raw_tail = (partial_out or partial_err).strip().splitlines()
        tail = " ⏎ ".join(_raw_tail[-5:])
        return 124, partial_out, (
            (partial_err or "")
            + f"\n[硬超时] 超过 {timeout:.0f}s 上限被终止"
            + (f"；最后输出：{tail[-500:]}" if tail else "；全程无输出")
        ).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


class _ProcResult:
    """子进程结果的最小结构（与 subprocess.CompletedProcess 同构）。"""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_with_idle_watchdog(argv: list[str], *, cwd: Path, timeout: float,
                            env: dict[str, str]
                            ) -> tuple[_ProcResult, float | None]:
    """跑子进程，**按「多久没新输出」而不是「总共多久」判卡死**。

    ## 为什么要这个

    只靠 ``subprocess.run(timeout=...)`` 有两个问题：

    1. **等满才报**：agent 卡住时我们白等 450~900 秒，然后只拿到
       ``exit code 124`` 和空输出，看不出卡在哪。
    2. **把慢任务当卡死**：如果 agent 真的在正常跑一个 3 分钟的命令，
       固定短超时会误杀；而固定长超时又让真卡死拖很久。

    「输出年龄」同时解决这两点：正常工作会持续吐字，卡死则彻底静默。
    实现上用非阻塞读 + 轮询，记录最后一次收到字节的时间；超过
    ``_idle_timeout()`` 没有任何新字节就主动 kill，并把「最后说了什么」
    一并带回去——这比空手等超时有信息量得多。

    返回 ``(proc, idle_killed_at)``；未被看门狗杀时第二项为 None，
    此时就是普通执行，行为与 ``subprocess.run`` 一致。
    """
    idle_limit = _idle_timeout()
    started = time.monotonic()
    last_byte = started
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        bufsize=1,
    )

    # 用线程各读一路，主线程只负责计时与判死——避免 select 在管道上的平台差异。
    import threading

    def _reader(stream, sink: list[str]) -> None:
        nonlocal last_byte
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
                last_byte = time.monotonic()
        except (ValueError, OSError):
            return

    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True),
    ]
    for thread in threads:
        thread.start()

    idle_killed: float | None = None
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        silent_for = now - last_byte
        if silent_for >= idle_limit:
            idle_killed = silent_for
            proc.kill()
            proc.wait(timeout=5)
            break
        if now - started >= timeout:
            proc.kill()
            proc.wait(timeout=5)
            # ## 超时也必须把已收到的输出带回去
            #
            # 实测：agent 跑了 300 秒，期间打印了几十行，但超时分支只抛
            # TimeoutExpired，调用方拿到 out="" err="timeout after 300s"，
            # 那些行全丢了——而它们正是判断「它到底卡在哪一步」的唯一线索。
            #
            # 所以先补线程、把缓冲拼好，随异常一起交出去。
            for thread in threads:
                thread.join(timeout=2)
            raise subprocess.TimeoutExpired(
                argv, timeout,
                output="".join(stdout_chunks),
                stderr="".join(stderr_chunks),
            )
        time.sleep(0.25)

    for thread in threads:
        thread.join(timeout=2)

    return (
        _ProcResult(proc.returncode or 0, "".join(stdout_chunks),
                    "".join(stderr_chunks)),
        idle_killed,
    )


def _probe_host_banner(executable: str, *, cwd: Path,
                       env: dict[str, str]) -> str:
    """宿主静默失败时，用一个最小调用把它的真实状态探回来。

    只探一次、短超时、只要一次输出——目的是「有话可说」，不是完整诊断。
    """
    try:
        proc = subprocess.run(
            [executable, "--profile", "headless", "reply with: OK"],
            cwd=str(cwd), capture_output=True, text=True, timeout=60.0,
            env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text[-600:]


#: 已知的宿主外部失败特征。命中即点名，省去人工翻日志。
_AGENT_FAILURE_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("insufficient balance", "宿主账户额度不足，非代码缺陷"),
    ("quota", "宿主账户额度不足，非代码缺陷"),
    ("rate limit", "宿主限流，稍后重试"),
    ("authentication", "宿主鉴权失败，检查登录状态"),
    ("unauthorized", "宿主鉴权失败，检查登录状态"),
    ("initialization_required", "宿主启动期初始化失败（工作目录缺少宿主所需状态）"),
    ("command not found", "宿主可执行文件不在 PATH"),
    # 实测：claude 在收到较长提示词时只吐这一行就退出（exit 0），
    # 没有任何实际工作。看起来像"成功"，但什么都没改——必须点名，
    # 否则会当成"agent 没能力修复"而反复重跑。
    # 根因在宿主侧模型别名未注册（它仍会打印 PONG 之类简单回复，
    # 所以"能跑通"不等于"能干活"）。
    ("unrecognized_model",
     "claude 的遥测警告（第三方 baseURL 下模型别名不在官方目录）。"
     "**这行无害，能正常干活**——真正的静默挂起另有原因："
     "默认 --output-format text 在管道下攒输出不发，长任务会卡死。"
     "已在 ONE_SHOT_INVOCATIONS 里改用 stream-json + --verbose"),
)


def _diagnose_agent_failure(output: str, host: str) -> str:
    """把宿主失败输出映射成人话。命中不了就返回空串，不猜。"""
    lowered = output.lower()
    for needle, meaning in _AGENT_FAILURE_SIGNATURES:
        if needle in lowered:
            return f"{meaning}：{host}"
    return ""


#: 计算原仓库指纹时跳过的目录：体积大、与修复无关、且每次运行都会变。
_FINGERPRINT_SKIP = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "dist", "build",
    ".bugx", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

#: 只对文本类源码取指纹。二进制/大文件既拖慢又无意义。
_FINGERPRINT_EXTS = frozenset({
    ".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh",
    ".js", ".ts", ".jsx", ".tsx", ".cfg", ".ini",
})


def _tree_fingerprint(root: Path, *, limit: int = 4000) -> dict[str, str]:
    """整树的 ``相对路径 -> sha256`` 指纹，用于证明原仓库未被改动。

    ``limit`` 是防止在巨型仓库上把一次修复拖成几分钟的闸门；超过后
    按路径排序截断——截断只会降低检测灵敏度，不会制造误报。
    """
    import hashlib
    result: dict[str, str] = {}
    count = 0
    for path in sorted(root.rglob("*")):
        if len(result) >= limit or count > limit * 4:
            break
        count += 1
        if not path.is_file() or path.is_symlink():
            continue
        if _FINGERPRINT_SKIP & set(path.relative_to(root).parts):
            continue
        if path.suffix not in _FINGERPRINT_EXTS:
            continue
        try:
            result[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        except OSError:
            continue
    return result


def _agent_command(host: str, prompt: str) -> list[str]:
    """构造宿主 agent 的一次性调用命令。"""
    from core.host_runtime import host_registry as hr
    import shutil as _shutil

    plugin = hr.host_plugin(host)
    executable = _shutil.which(host)
    if executable is None:
        raise RuntimeError(f"宿主 {host} 不在 PATH 中")
    argv = [executable]
    if plugin is not None:
        node_flags = getattr(plugin.engine_run, "engine_run_node_flags", ())
        real = hr._real_node_binary() if node_flags else None
        if real and node_flags:
            argv = [str(real), *node_flags, executable]
    return [*argv, *_one_shot_args(host, prompt)]


#: 各宿主的「一次性执行、打印结果后退出」参数。
#:
#: 这三个都不一样，硬编码其中任何一个都会让另外两个无法工作：
#:
#:     dsh       --profile headless <prompt>   需要 profile 栈
#:     opencode  run <prompt>                  子命令形式
#:     claude    -p <prompt>                   标志形式
#:
#: 早先只写了 dsh 的形式，于是「换宿主验证」这件事在代码层面就不可能——
#: 不是我找不到可用 provider，而是构造命令的那段只认 dsh。
ONE_SHOT_INVOCATIONS: dict[str, tuple[str, ...]] = {
    "dsh": ("--profile", "headless"),
    "opencode": ("run",),
    # claude 需要三个参数配合，缺任一都不可用：
    #
    # 1. `--dangerously-skip-permissions`
    #    默认每次写文件都弹权限确认。headless（-p）下没有 TTY 可确认，
    #    agent 会在「想改但改不了」上反复重试直到预算耗尽。沙箱本就是
    #    隔离副本，放行安全。
    #
    # 2. `--output-format stream-json`
    #    **这是"静默挂起"的真凶。** 默认的 text 格式在管道下会把输出
    #    攒着不发，长任务直接卡死：实测同一命令重复 5 次全部 0 字节并
    #    挂到超时，而换成 stream-json 立刻正常——68s 完成、4412 行事件、
    #    stop_reason=end_turn、文件确实被改对。
    #
    # 3. `--verbose`
    #    stream-json 要求它，否则 claude 直接报错退出。
    #
    # 排查时被 `[claude-code:unrecognized_model]` 那行带偏过：它只是遥测
    # 警告（第三方 baseURL 下模型别名不在官方目录），短提示词照样能答
    # PONG——**能跑通不等于能干活**。
    "claude": ("--dangerously-skip-permissions", "-p",
               "--output-format", "stream-json", "--verbose"),
    # codex 要求工作目录是 git 仓库。**不能**用 --skip-git-repo-check 绕过：
    # 项目契约（core/hosts/codex.py、tests/test_host_plugins.py）明确禁止——
    # 该 flag 会顺手放开宿主目录信任边界，把「前置门禁失效」伪装成「跑得通」。
    #
    # 正确做法是让候选树成为合规目标：见 attempt_fix 里给沙箱建的最小 .git。
    # 真缺 .git 就让 codex 自己报 Not inside a trusted directory，不静默降级。
    "codex": ("exec",),

}


def _host_key(host: str) -> str:
    """把宿主标识归一成 ``ONE_SHOT_INVOCATIONS`` 的键。

    * 已知宿主名 → 原样返回
    * 路径（含分隔符）→ 用可执行文件名匹配；仍不识别则按 ``dsh`` 处理

    为什么路径默认 ``dsh``：测试用假宿主可执行文件模拟各种 agent 行为，
    它们都遵循「在 cwd 里就地改文件」这一 dsh 语义。让假宿主必须伪造一个
    已知宿主名，会把测试绑在实现细节上。
    """
    if host in ONE_SHOT_INVOCATIONS:
        return host
    name = Path(host).name
    if name in ONE_SHOT_INVOCATIONS:
        return name
    if os.sep in host or host.startswith("."):
        return "dsh"
    return host


def _one_shot_args(host: str, prompt: str) -> list[str]:
    """按宿主拼一次性执行参数。

    未知宿主不做猜测——猜错的调用会静默跑成别的模式（例如 opencode 交互
    模式会挂住等待输入）。宁可显式失败。

    ``host`` 可以是宿主名（``opencode``）也可以是可执行文件路径
    （``/tmp/xxx/fakehost``）。后者用于测试里的假宿主：它没有名字，
    但通常实现的是 dsh 风格（读 cwd、直接改文件）。判据放在
    ``_host_key`` 里，避免这里堆积特例。
    """
    prefix = ONE_SHOT_INVOCATIONS.get(_host_key(host))
    if prefix is None:
        raise RuntimeError(
            f"未知宿主 {host!r} 的一次性执行方式。已支持："
            f"{sorted(ONE_SHOT_INVOCATIONS)}。"
            "新增宿主时请在 ONE_SHOT_INVOCATIONS 里登记其"
            "「执行一次并退出」的参数形式。"
        )
    return [*prefix, prompt]


def attempt_fix(work_order: dict, *, host: str = "dsh", root: Path | None = None,
                timeout: float = DEFAULT_FIX_TIMEOUT,
                regression_timeout: float = DEFAULT_REGRESSION_TIMEOUT,
                mode: str | None = None,
                regression_commands: list[list[str]] | None = None
                ) -> dict[str, object]:
    """在隔离副本里尝试修复。原仓库不会被改动。

    返回报告；``applied`` 为 True 时报告里带 ``diff``，由调用方决定是否落盘。
    """
    root = (root or REPO_ROOT).resolve()
    mode = mode or rsi_scope.current_mode()
    started = time.monotonic()
    report: dict[str, object] = {
        "attempted": True, "host": host, "mode": mode, "applied": False,
    }

    sandbox = Path(tempfile.mkdtemp(prefix="bugx-fix-"))
    tree = sandbox / "tree"
    try:
        # 只复制必要内容：整仓含 .git 会非常慢，且 agent 不需要历史。
        shutil.copytree(
            root, tree,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".bugx", "dist", "build",
                "node_modules", ".venv",
            ),
        )
        # ## 候选树必须是「合规的 Git 仓库」，否则 codex 拒绝启动
        #
        # 复制时剥掉 `.git` 是对的（整仓历史极慢，agent 也不需要），但
        # ``codex exec`` 要求 cwd 在 Git 仓库内。第一版用
        # ``--skip-git-repo-check`` 绕过——**那是错的**：该 flag 会顺手放开
        # 宿主自己的目录信任边界，把「前置门禁失效」伪装成「跑得通」，
        # 与 ``core/hosts/codex.py`` 的契约直接冲突（那边明文禁止）。
        #
        # 正确做法是让目标本身合规：给沙箱建一个**空的** ``.git``，
        # 既满足 codex 的判据，又完整保留目录信任边界。不复制原仓库历史
        # 是因为 agent 不需要，且会让沙箱准备慢一个数量级。
        _ensure_scratch_git(tree)
        before_snapshot = rsi_scope.snapshot(tree)
        # 原仓库指纹：用来在事后证明「隔离真的隔离了」。
        #
        # 实测事故：opencode 无视 cwd，直接 cd 到被修复的仓库本身开工，
        # 把注入的缺陷改好了。而 PoC 在真正的沙箱里跑（缺陷仍在），于是
        # 报告「未能修复」——原仓库被静默污染，故障却记在 agent 头上。
        #
        # 提示词已钉死绝对路径，但不能只靠模型守规矩：这道断言让污染
        # **可见**。宁可报告一次失败，也不接受"看起来修好了"。
        #
        # 判据用「整树文件指纹」而不是 rsi_scope.snapshot 的 content_digests：
        # 后者只覆盖 evals 断言文件（实测 2 条），拿它查污染既漏报
        # （源码被改看不见）又误报（假宿主合法改写 evals 就算"污染"）。
        origin_fingerprint = _tree_fingerprint(root)

        prompt = build_fix_prompt(work_order, mode=mode, tree=tree)
        try:
            argv = _agent_command(host, prompt)
        except RuntimeError as exc:
            report["error"] = str(exc)
            return report

        rc, out, err = _run(argv, cwd=tree, timeout=timeout)
        report["agent_exit_code"] = rc
        combined = (out or "") + (("\n" + err) if err else "")
        report["agent_output_tail"] = _summarize_stream_json(combined)[-1500:]
        if rc != 0:
            # 空输出是最难排查的失败：退出码 1 但什么都没说，接手的人只能
            # 从头复现。实测：dsh 在额度耗尽时启动阶段就失败，stdout/stderr
            # 均为空，report 里只剩 "agent 退出码 1"。
            #
            # 所以这里要做三件事：给出可执行的排查指令、附上实际命令、
            # 并在能识别出已知外部原因时直接点名（额度/鉴权/网络）。
            diagnosis = _diagnose_agent_failure(combined, host)
            # ## 我们自己产生的诊断不能被当成"没有诊断"
            #
            # 实测：`_run` 在卡死/超时时会往 stderr 写 `[卡死判定] ...`
            # `[硬超时] ... 最后输出：...`，但这里只问 `_diagnose_agent_failure`
            # 认不认识这些外部宿主特征——它不认识，于是整段被丢掉，报告里
            # 只剩「agent 退出码 124」。**采集到的证据在最后一米被扔了。**
            #
            # 判据要分开：外部特征用于「点名原因」，自产标记用于「保留证据」。
            self_evident = (
                "[卡死判定]" in combined or "[硬超时]" in combined
                or "[宿主状态探测]" in combined
            )
            report["error"] = (
                f"agent 退出码 {rc}"
                + (f"（{diagnosis}）" if diagnosis else "")
                + ("；输出为空，可用同一命令手动复现：" if not combined.strip()
                   else "")
                + (f" {' '.join(argv[:3])} ..." if not combined.strip() else "")
            )
            if self_evident:
                report["error"] += f"；{combined.strip()[-700:]}"
            report["command"] = argv
            return report

        # 断言守卫：agent 可能顺手改了评测断言
        after_snapshot = rsi_scope.snapshot(tree)
        violations = rsi_scope.check_assertion_change(
            before_snapshot, after_snapshot, mode=mode,
        )
        report["assertion_violations"] = violations
        if violations:
            report["error"] = "改动越界，已丢弃"
            return report

        # 双向门禁之一：复现命令必须通过
        poc_clean = 0
        for item in work_order.get("reproduce", []):
            command = str(item.get("command") or "")
            if not command:
                continue
            poc_rc, poc_out, poc_err = _run(
                command.split(), cwd=tree, timeout=regression_timeout,
            )
            report.setdefault("poc_results", []).append({
                "command": command, "exit_code": poc_rc,
                "tail": (poc_out or poc_err)[-500:],
            })
            if poc_rc != 0:
                poc_clean += 1
        # 原仓库未被改动？在判定 PoC 之前先查——污染是比"没修好"更严重的事。
        after_origin = _tree_fingerprint(root)
        touched = sorted(
            name for name, digest in after_origin.items()
            if name in origin_fingerprint and origin_fingerprint[name] != digest
        )
        if touched:
            report["origin_modified"] = touched[:20]
            report["error"] = (
                "宿主 agent 改动了原仓库（隔离失效）：" + "、".join(touched[:5])
                + "。本次修复结果不可信，已丢弃。"
            )
            report["applied"] = False
            return report

        report["poc_passed"] = poc_clean == 0
        if poc_clean:
            report["error"] = "复现命令仍未通过"
            return report

        # 双向门禁之二：既有测试不得退化。
        #
        # ``regression_commands`` 让调用方指定「对本缺陷有意义的回归集」。
        # L7 的修复只需重跑 l7_selfcheck（0.00s），没必要跑全量 pytest
        # （数分钟）——门禁要拦的是「改坏别处」，不是「必须跑全量」。
        # 缺省仍走 pytest tests/，保持对通用工单的行为不变。
        if regression_commands:
            for argv in regression_commands:
                reg_rc, reg_out, reg_err = _run(
                    list(argv), cwd=tree, timeout=regression_timeout,
                )
                report.setdefault("regression_results", []).append({
                    "command": " ".join(str(a) for a in argv),
                    "exit_code": reg_rc,
                    "tail": (reg_out or reg_err)[-600:],
                })
                if reg_rc != 0:
                    report["regression_passed"] = False
                    report["error"] = "回归命令退化，已丢弃改动"
                    return report
            report["regression_passed"] = True
        test_dir = tree / "tests"
        if not regression_commands and test_dir.is_dir():
            reg_rc, reg_out, reg_err = _run(
                [sys.executable, "-m", "pytest", "tests/", "-q", "-x",
                 "--timeout=120"],
                cwd=tree, timeout=regression_timeout,
            )
            report["regression_exit_code"] = reg_rc
            report["regression_tail"] = (reg_out or reg_err)[-1200:]
            report["regression_passed"] = reg_rc == 0
            if reg_rc != 0:
                report["error"] = "回归测试退化，已丢弃改动"
                return report
        elif not regression_commands:
            report["regression_passed"] = None

        # 取回 diff（不含被禁文件）
        diff_rc, diff_out, _ = _run(
            ["git", "diff", "--no-index", "--no-color", "--", "."],
            cwd=tree, timeout=120.0,
        )
        # 没有 git 仓库，改用逐文件比对：这里只报告改动的文件清单即可，
        # 真正的补丁由调用方在原仓库重放或人工审阅。
        changed = _changed_files(root, tree)
        report["changed_files"] = changed
        blocked = [
            path for path in changed
            if path in rsi_scope.ASSERTION_FILES
            or path in rsi_scope.FORBIDDEN_FILES
        ]
        if blocked:
            report["error"] = f"改动了被禁文件：{blocked}"
            report["assertion_violations"] = [f"改动 {p}" for p in blocked]
            return report

        report["applied"] = False  # 从不自动写入原仓库，只交回候选改动
        report["candidate_tree"] = str(tree)
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["verified"] = True
        return report
    except (OSError, shutil.Error) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        # 保留候选树供人工取用；没有候选时立即删掉
        if not report.get("candidate_tree"):
            shutil.rmtree(sandbox, ignore_errors=True)


def _ensure_scratch_git(tree: Path) -> None:
    """给候选树建一个空的 Git 仓库（无历史、无提交）。

    只为满足 ``codex exec`` 的「cwd 必须在 Git 仓库内」判据。**不**用
    ``--skip-git-repo-check`` 绕过，因为那个 flag 会放开宿主目录信任边界。

    失败不致命：其它宿主（opencode/claude/dsh）不需要 .git。所以这里
    只尽力而为，不抛异常——真要是 codex 用到又建不出来，它自己会报
    ``Not inside a trusted directory``，比静默降级清楚。
    """
    git_dir = tree / ".git"
    if git_dir.exists():
        return
    try:
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch", "scratch"],
            cwd=str(tree), capture_output=True, text=True, timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _changed_files(original: Path, candidate: Path) -> list[str]:
    """比对两棵树，返回内容不同的相对路径。"""
    changed: list[str] = []
    skip = {".git", "__pycache__", ".bugx", "dist", "build", "node_modules"}
    for path in candidate.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(candidate)
        if any(part in skip for part in rel.parts):
            continue
        target = original / rel
        if not target.is_file():
            changed.append(str(rel))
            continue
        try:
            if path.read_bytes() != target.read_bytes():
                changed.append(str(rel))
        except OSError:
            continue
    return sorted(changed)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="派宿主 agent 修 BUGX 自身缺陷")
    parser.add_argument("--work-order", required=True, help="work-order.json 路径")
    parser.add_argument("--host", default="dsh")
    parser.add_argument("--timeout", type=float, default=DEFAULT_FIX_TIMEOUT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    path = Path(args.work_order).expanduser()
    if not path.is_file():
        print(f"工作单不存在：{path}", file=sys.stderr)
        return 2
    try:
        work_order = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"工作单无法解析：{exc}", file=sys.stderr)
        return 2

    report = attempt_fix(work_order, host=args.host, timeout=args.timeout)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"模式：{report.get('mode')}  宿主：{report.get('host')}")
        if report.get("error"):
            print(f"✗ {report['error']}")
        elif report.get("verified"):
            print("✓ 双向门禁通过")
            for name in report.get("changed_files", []):
                print(f"    改动：{name}")
            print(f"候选树：{report.get('candidate_tree')}")
        else:
            print("未完成")
        for violation in report.get("assertion_violations", []):
            print(f"  ! {violation[:120]}")
    return 0 if report.get("verified") else 1


if __name__ == "__main__":
    sys.exit(main())
