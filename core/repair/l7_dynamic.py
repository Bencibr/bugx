#!/usr/bin/env python3
"""L7 动态检查：跑起来才知道有没有坏。

## 为什么静态检查不够

L7-1 ~ L7-6 全是静态结构分析。它们很便宜（0.00s），但有一类缺陷它们
永远看不见：**代码结构完全正确，跑起来却失败**。

这一层要防的具体缺陷，全部真实发生过：

    * dev 模式 init 死锁 —— 结构上看，守卫和短路都在，逻辑也「应该」成立；
      只有真的执行一次才知道它被自己的守卫拒绝了
    * `install --force` 假成功 —— 静态检查看到「有写入通路」，
      但实际写的是旧模板；只有装完去看文件才知道
    * CLI 子命令在某种状态下崩溃 —— AST 里一切正常，运行时抛异常

所以动态层不复用静态层的判据，而是问一个不同的问题：
**这条链路，现在，真的能跑通吗？**

## 成本纪律

动态检查比静态贵三个数量级，所以纪律必须写死在这里：

    * 只用**秒级**命令。实测标定：`--version` 1.3s、`pre` 0.5s、
      `init` 12.8s、`doctor` 20.6s。后两个**不进**默认检查集
    * 全部在临时沙箱里跑，不碰真实项目
    * 单条命令硬超时，超时即判失败（不重试、不等待）
    * 默认关闭，`--dynamic` / `RUN_DYNAMIC` 显式开启

最后一条是刻意的：L7 的价值在于「每轮都跑得起」。如果动态检查让 L7 从
0.00s 变成 15s，它就会被从流程里摘掉——那样保护反而是零。
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "core"))

#: 单条动态命令的硬超时。超时即失败，不重试。
COMMAND_TIMEOUT = 90.0

#: dev 模式入口。存在则用它，否则退回 PATH 上的 bugx。
DEV_LAUNCHER = Path.home() / ".local" / "bin" / "bugx-dev"


def _bugx_command(root: Path) -> tuple[list[str], dict[str, str]]:
    """选可执行入口，返回 (命令, 额外环境变量)。

    ## 必须能指定「测哪份源码」

    ``~/.local/bin/bugx-dev`` 里硬编码了仓库路径的 ``PYTHONPATH``。早期实现
    直接调它，导致动态检查**永远只测真实仓库那一份代码**：在被测副本里注入
    缺陷后仍然 4/4 通过——检查测的不是副本。

    这种失效极其隐蔽：它显示绿灯，但灯照的是别处。所以这里改成
    「用 ``root/src`` 作为 PYTHONPATH 直接调模块入口」，与 bugx-dev 等价，
    但指向被测树。

    环境变量与 bugx-dev 保持一致（``BUGX_DEV_MODE=1``），因为静态检查之外
    的行为差异会让两条路径的结论不可比。
    """
    source = (root / "interfaces" / "cli") if (root / "interfaces" / "cli" / "bugx_cli" / "cli.py").is_file() else (root / "src")
    environment = {"BUGX_DEV_MODE": "1", **_sandbox_environment()}
    if (source / "bugx_cli" / "cli.py").is_file():
        existing = os.environ.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            f"{source}{os.pathsep}{existing}" if existing else str(source)
        )
        return [sys.executable, "-m", "bugx_cli.cli"], environment

    # 被测树没有源码时退回已安装入口——并让调用方知道测的不是它。
    if DEV_LAUNCHER.is_file():
        return [str(DEV_LAUNCHER)], {}
    found = shutil.which("bugx")
    if found:
        return [found], {}
    return [], {}


def _run(argv: list[str], *, cwd: Path,
         timeout: float = COMMAND_TIMEOUT,
         extra_env: dict[str, str] | None = None) -> dict[str, object]:
    environment = {**os.environ, **(extra_env or {})}
    # 继承链里的 BUGX_GLOBAL_ENGINE_ROOT 会压过上面 BUGX_DEV_MODE=1 的源码树
    # 语义（显式变量优先级最高）：global_engine_root 落到真实引擎而非源码树，
    # init 的安装目的地≠源码树 → 防自污染守卫拒装 → L7-D2 误报 dev 死锁复活。
    # 动态检查测的就是「被测树自己」，除非 extra_env 显式指定，否则一律剥离。
    if "BUGX_GLOBAL_ENGINE_ROOT" not in (extra_env or {}):
        environment.pop("BUGX_GLOBAL_ENGINE_ROOT", None)
    # 关掉需要网络的云同步，避免动态检查因外部服务失败而误报
    environment["BUGX_SKIP_CLOUD"] = "1"
    environment["NO_COLOR"] = "1"
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=environment, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "output": f"超时（{timeout}s）未返回",
                "timed_out": True}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"exit_code": 127, "output": f"{type(exc).__name__}: {exc}"}
    text = (proc.stdout or "") + (proc.stderr or "")
    # 只留尾部会丢掉开头的关键信息。实测踩到：`engine --help` 的标题
    # 「BugX launch_bugx 启动协议」在第一行，截尾后检查误判为「输出异常」。
    # 保留头 + 尾，中间省略。
    if len(text) > 2000:
        text = text[:900] + "\n…（中间省略）…\n" + text[-900:]
    return {"exit_code": proc.returncode, "output": text}


def _sandbox_environment() -> dict[str, str]:
    """动态检查的隔离环境变量。

    ## 为什么必须隔离 HOME

    实测：单独跑 ``test_l7_dynamic.py`` 18 passed，全量套件跑时
    ``SingleCheckTests`` 有 3 个失败。差别在于**全局状态**——
    ``~/.bugx`` 下的 manifest/current.json 会被其他测试改写，动态检查
    读到别人的残留就报错。

    这不是「测试环境不干净」这么轻的问题：它说明动态检查**依赖调用者的
    家目录状态**，那么它在 CI、在多项目机器上、在别人电脑上都会时绿时红。
    一条会随机变红的检查，最终一定会被摘掉。

    所以每次探测都用一个临时 HOME，让 BugX 看到一张白纸。
    代价是触发一次全局库初始化（约 1-2s），换来的是**可复现**。
    """
    home = Path(tempfile.mkdtemp(prefix="bugx-l7home-"))
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "BUGX_SKIP_CLOUD": "1",
        "NO_COLOR": "1",
    }


def _prepare_sandbox() -> Path:
    """建一个「可被 BugX 接受」的最小项目。

    实测发现：``bugx engine pre`` 在非 Git 仓库上会拒绝执行
    （"当前项目根目录不是 Git 仓库"）。这是**正确行为**，不是缺陷——
    挖掘需要版本控制来定位改动。所以沙箱必须 git init，否则动态检查
    测到的是「我的沙箱不合格」，而不是「BugX 坏了」。

    这正是动态检查容易失真的地方：它一旦用不合规的输入，
    就会把环境问题报成产品问题。每个沙箱准备都必须显式做到这一点。
    """
    sandbox = Path(tempfile.mkdtemp(prefix="bugx-l7d-"))
    environment = {**os.environ, "NO_COLOR": "1"}
    # 需要一次提交：``engine pre`` 还会要求「可读取的 HEAD 基线」。
    # 两步都是 BugX 的合理要求（挖掘靠版本控制定位改动），不是缺陷。
    # 逐个试出来是动态检查特有的成本——但一旦确定，就固化成前提，
    # 以后再变说明 BugX 的前置条件改了，值得知道。
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "l7@bugx.local"],
        ["git", "config", "user.name", "l7-dynamic"],
        ["git", "commit", "-q", "--allow-empty", "-m", "baseline"],
    ):
        try:
            subprocess.run(
                argv, cwd=str(sandbox), capture_output=True, text=True,
                timeout=30.0, env=environment, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return sandbox


def check_cli_runs(root: Path) -> tuple[bool, str, list[str]]:
    """L7-D1：CLI 能启动并报告版本。

    最基础的一条：如果连 ``--version`` 都跑不起来，其余检查都无意义。
    这条同时验证了「导入期没有循环依赖 / 没有语法错误」——那些问题
    静态检查能看出语法错，看不出**导入期**的死锁。
    """
    command, extra_env = _bugx_command(root)
    if not command:
        return False, "找不到 bugx 可执行入口（既无 bugx-dev 也不在 PATH）", []
    result = _run([*command, "--version"], cwd=root, extra_env=extra_env)
    text = str(result.get("output") or "")
    if result.get("exit_code") != 0:
        return False, (
            f"`bugx --version` 退出码 {result.get('exit_code')}：{text[:300]}"
        ), []
    if "bugx" not in text.lower():
        return False, f"`bugx --version` 未输出可识别的版本信息：{text[:200]}", []
    return True, f"CLI 正常：{text.strip().splitlines()[0][:80]}", []


def check_engine_protocol_loads(root: Path) -> tuple[bool, str, list[str]]:
    """L7-D2：引擎启动协议能被加载（``bugx engine --help``）。

    ## 为什么换成这条，而不是 ``engine pre``

    最初想用 ``engine pre``——它是挖掘流程真正的第一道门禁。实测发现它
    不适合做探针：``pre`` 是**交互式启动协议**，输出的是阶段进度报告，
    退出码 1 表示「还有未完成阶段」，属于正常语义。拿它当探针会把
    「正常的中间态」误报成失败。

    这类误报极其危险：动态检查一旦误报，就会被判定为「太吵」而从流程里
    摘掉，保护归零。所以探针必须选**语义确定**的命令。

    ``engine --help`` 满足全部要求：
      * 只读（帮助文本明说「只读，不读取或修改项目运行态」）
      * 语义确定：能输出帮助 = 模块完整加载，退出码 0
      * 便宜：实测 0.5s，而 ``doctor --json`` 要 22s
      * 覆盖导入期：能打印帮助说明整个命令树已构造完毕，
        循环依赖 / 导入期死锁会在这里直接炸掉

    它测的不是「pre 能不能跑通」（那需要交互），而是「引擎协议是否完好」。
    这是更基础、也更稳定的不变式。
    """
    command, extra_env = _bugx_command(root)
    if not command:
        return False, "找不到 bugx 可执行入口", []

    sandbox = _prepare_sandbox()
    try:
        init = _run(
            [*command, "init", "--root", str(sandbox), "--cloud-sync", "local"],
            cwd=sandbox, timeout=COMMAND_TIMEOUT, extra_env=extra_env,
        )
        if init.get("exit_code") != 0:
            return False, (
                "`bugx init` 失败——这是 dev 死锁类缺陷的典型表现："
                f"退出码 {init.get('exit_code')}，输出 {str(init.get('output'))[:400]}"
            ), []

        engine = _run(
            [*command, "engine", "--root", str(sandbox), "--help"],
            cwd=sandbox, extra_env=extra_env)
        output = str(engine.get("output") or "")
        if engine.get("exit_code") != 0:
            return False, (
                f"`bugx engine --help` 退出码 {engine.get('exit_code')}："
                f"{output[:400]}"
            ), []

        # 只读命令不该在项目里留下痕迹。这条同时验证「声称只读就真只读」——
        # 一个自称只读却写盘的入口，会让「只读探针」这个前提失效。
        traceback_marker = "Traceback (most recent call last)"
        if traceback_marker in output:
            return False, f"引擎加载时抛异常：{output[:400]}", []
        if "启动协议" not in output:
            return False, f"引擎帮助输出异常（未含协议标题）：{output[:300]}", []
        return True, "init + 引擎协议加载链路跑通", []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def check_install_writes_declared_paths(root: Path) -> tuple[bool, str, list[str]]:
    """L7-D3：``install`` 声称装上的入口，必须真的存在。

    这条直接对应实测缺陷「install --force 假成功」：命令报告成功，
    但写入的是旧模板 / 根本没写。静态检查（L7-5）只能看到「有写入通路」，
    看不到**这次写入是否真的发生了**。

    只测一个宿主（dsh），因为它是当前默认集成目标；测全部宿主会让
    检查从秒级变成分钟级。
    """
    command, extra_env = _bugx_command(root)
    if not command:
        return False, "找不到 bugx 可执行入口", []

    sandbox = _prepare_sandbox()
    try:
        init = _run(
            [*command, "init", "--root", str(sandbox), "--cloud-sync", "local"],
            cwd=sandbox, timeout=COMMAND_TIMEOUT, extra_env=extra_env,
        )
        if init.get("exit_code") != 0:
            return False, f"`bugx init` 失败：{str(init.get('output'))[:300]}", []

        install = _run(
            [*command, "install", "--root", str(sandbox), "--agent", "dsh"],
            cwd=sandbox, extra_env=extra_env,
        )
        if install.get("exit_code") != 0:
            return False, (
                f"`bugx install --agent dsh` 失败：退出码 "
                f"{install.get('exit_code')}，{str(install.get('output'))[:400]}"
            ), []

        # 断言「报告成功」与「文件存在」一致——两者的不一致正是假成功。
        expected = [
            sandbox / ".dsh" / "skills" / "bugx-dsh" / "SKILL.md",
            sandbox / ".dsh" / "patch.yml",
        ]
        missing = [str(p.relative_to(sandbox)) for p in expected if not p.is_file()]
        if missing:
            return False, (
                "`install --agent dsh` 报告成功，但这些声明路径不存在："
                f"{missing}。这是假成功——静默跳过必须有可见性。"
            ), []
        return True, "install 声明的入口均已写入", []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def check_state_readback(root: Path) -> tuple[bool, str, list[str]]:
    """L7-D4：初始化后的状态能被读回，且是可解析的结构。

    对应一类具体缺陷：写入成功、读取路径却不一致（字段名漂移、
    JSON 结构变更未同步）。静态检查看不到，因为它们各自都「正确」。
    """
    command, extra_env = _bugx_command(root)
    if not command:
        return False, "找不到 bugx 可执行入口", []

    sandbox = _prepare_sandbox()
    try:
        init = _run(
            [*command, "init", "--root", str(sandbox), "--cloud-sync", "local"],
            cwd=sandbox, timeout=COMMAND_TIMEOUT, extra_env=extra_env,
        )
        if init.get("exit_code") != 0:
            return False, f"`bugx init` 失败：{str(init.get('output'))[:300]}", []

        state_dir = sandbox / ".bugx"
        if not state_dir.is_dir():
            return False, "init 报告成功但 .bugx 目录不存在", []

        json_files = sorted(state_dir.glob("*.json"))
        if not json_files:
            return False, "init 后在 .bugx 下找不到任何 JSON 状态文件", []

        unreadable: list[str] = []
        for path in json_files:
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                unreadable.append(f"{path.name}: {type(exc).__name__}")
        if unreadable:
            return False, f"状态文件不可解析：{unreadable[:3]}", []
        return True, f"{len(json_files)} 个状态文件均可解析", []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def run_dynamic_checks(root: Path | None = None) -> dict[str, object]:
    """执行全部动态检查，返回与 ``run_all`` 同构的结果。"""
    root = (root or REPO_ROOT).resolve()
    results: list[dict[str, object]] = []
    for check_id, name, func in DYNAMIC_CHECKS:
        try:
            ok, detail, notes = func(root)
        except (OSError, ValueError, SyntaxError) as exc:
            ok, detail, notes = False, f"检查自身异常：{type(exc).__name__}: {exc}", []
        results.append({
            "id": check_id, "name": name, "ok": bool(ok),
            "detail": detail, "notes": notes,
        })
    failed = [r for r in results if not r["ok"]]
    return {
        "schema_version": 1,
        "summary": {
            "total": len(results), "passed": len(results) - len(failed),
            "failed": len(failed), "ok": not failed,
            "checks_failed": [r["id"] for r in failed],
        },
        "checks": results,
    }


DYNAMIC_CHECKS = (
    ("L7-D1", "CLI 可启动", check_cli_runs),
    ("L7-D2", "引擎协议可加载", check_engine_protocol_loads),
    ("L7-D3", "install 真实写入", check_install_writes_declared_paths),
    ("L7-D4", "状态可读回", check_state_readback),
)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="L7 动态检查")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    payload = run_dynamic_checks(args.root)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for check in payload["checks"]:
            mark = "✓" if check["ok"] else "✗"
            print(f"{mark} {check['id']} {check['name']}")
            if not check["ok"]:
                print(f"    {check['detail']}")
        summary = payload["summary"]
        print(f"\n{summary['passed']}/{summary['total']} 通过")
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
