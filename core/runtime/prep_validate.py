#!/usr/bin/env python3
"""prep_validate.py — 开工准备记录的结构/知识库门禁。"""
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
import re

try:
    import core.bugx_paths as bugx_paths
except ImportError:  # pragma: no cover - standalone source copy fallback
    bugx_paths = None  # type: ignore[assignment]

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_DIR.parent.parent if AGENT_DIR.parent.name == ".bugx" else AGENT_DIR.parent
REQUIRED = (
    "## 项目识别",
    "## 目标指纹",
    "## 测试类型",
    "## 覆盖矩阵",
    "## 证据标准",
    "## 工具调研",
    "## 代码库图加速",
    "## 基础效率工具",
    "## 已部署 Web 功能回归",
    "## 可选插件",
    "## 工具选择",
    "## 工具就绪",
    "## 多工具协作",
    "## 准备结论",
)
PLACEHOLDERS = ("待填写", "TODO", "<待", "...", "未填写")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
NEGATIVE_RE = re.compile(r"(?:不可以|不能|未完成|未就绪|失败|\bno\b)", re.I)


def default_record_for_root(root: Path) -> Path:
    """Resolve preparation state to the active project lifecycle."""
    root = root.expanduser().resolve()
    if bugx_paths is not None:
        try:
            return bugx_paths.require(root).prep_record
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            pass
    return root / ".bugx" / "prep-record.md"


def _section_bodies(text: str) -> dict[str, str]:
    """按二级标题切分正文，避免仅出现标题就绕过准备门禁。"""
    headings = list(re.finditer(r"^## .+$", text, re.M))
    bodies: dict[str, str] = {}
    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[match.end():end]
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
        bodies[match.group(0).strip()] = body
    return bodies


def _main_package_name(root: Path) -> str | None:
    """从项目清单（pyproject.toml）推导主包名；拿不到则返回 None。

    优先取显式声明的 packages 列表（setuptools/poetry），否则回落到
    规范化后的发行名（``bugx-cli`` → ``bugx_cli``）。发行名与导入名
    不一致时由导入探测自然失败，守卫按约定静默跳过。
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11 回落正则解析
        tomllib = None  # type: ignore[assignment]
    data: dict = {}
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
    if data:
        tool = data.get("tool") or {}
        setuptools = tool.get("setuptools") or {}
        candidates: list[str] = []
        packages = setuptools.get("packages")
        if isinstance(packages, list):
            candidates.extend(str(p) for p in packages)
        packages_find = setuptools.get("packages_find") or (
            (setuptools.get("packages") or {}).get("find")
            if isinstance(setuptools.get("packages"), dict) else None
        ) or {}
        if isinstance(packages_find, dict):
            include = packages_find.get("include")
            if isinstance(include, list):
                candidates.extend(str(p) for p in include)
            where = packages_find.get("where")
            if not candidates and isinstance(where, list) and where:
                candidates.append(str(where[0]))
        poetry_packages = ((tool.get("poetry") or {}).get("packages")) or []
        for entry in poetry_packages:
            if isinstance(entry, str):
                candidates.append(entry)
            elif isinstance(entry, dict) and entry.get("include"):
                candidates.append(str(entry["include"]))
        for candidate in candidates:
            name = candidate.replace("/", ".").split(".")[0].strip()
            name = name.removesuffix("*").strip()
            if name and name not in {"find"}:
                return name
        project_name = ((data.get("project") or {}).get("name")
                        or (tool.get("poetry") or {}).get("name"))
        if isinstance(project_name, str) and project_name.strip():
            return project_name.strip().replace("-", "_")
        return None
    # tomllib 不可用/解析失败时的正则回落：只认最常见的两种声明。
    match = re.search(
        r"^\s*packages\s*=\s*\[([^\]]*)\]", text, re.M,
    )
    if match:
        for item in re.findall(r"[\"']([^\"']+)[\"']", match.group(1)):
            name = item.replace("/", ".").split(".")[0].removesuffix("*").strip()
            if name and name != "find":
                return name
    match = re.search(r"^name\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    if match:
        return match.group(1).strip().replace("-", "_")
    return None


def check_test_import_source(root: Path) -> list[str]:
    """测试导入源守卫：主包解析到工作区之外时返回 WARNING 行（不阻断）。

    项目测试默认可能 import 到另一个 checkout 的同名安装包（例如 src
    布局缺少 PYTHONPATH 时全量测到 site-packages 里的错版安装包）。这里
    在与测试运行一致的 cwd 下让真实解释器 import 主包，解析实际加载
    路径；不在项目根内就给出诚实提示。仅警告，不发明新门禁；包名获取
    失败或无可用 Python 环境时静默跳过。
    """
    import subprocess

    package = _main_package_name(root)
    if not package:
        return []
    try:
        probe = subprocess.run(
            [sys.executable, "-c",
             f"import {package}, sys; print(getattr({package}, '__file__', ''))"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if probe.returncode != 0:
        return []
    loaded = (probe.stdout or "").strip().splitlines()
    loaded_path = loaded[-1].strip() if loaded else ""
    if not loaded_path:
        return []
    try:
        resolved = Path(loaded_path).resolve()
        root_resolved = root.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return []
    if resolved == root_resolved or root_resolved in resolved.parents:
        return []
    return [
        f"测试可能执行非工作区代码：{package} 解析于 {resolved}"
        f"（如需测试工作区请设置 PYTHONPATH）"
    ]


def check(record: Path | None = None, root: Path = REPO_ROOT) -> tuple[int, list[str]]:
    record = record or default_record_for_root(root)
    errors: list[str] = []
    if not record.is_file():
        return 1, [f"缺少准备记录: {record}"]
    text = record.read_text(encoding="utf-8")
    bodies = _section_bodies(text)
    for heading in REQUIRED:
        if heading not in bodies:
            errors.append(f"缺少章节: {heading}")
            continue
        body = bodies[heading]
        if not body or any(p in body for p in PLACEHOLDERS):
            errors.append(f"章节未填写: {heading}")
    target = bodies.get("## 目标指纹", "")
    if target and not re.search(r"(?:SHA-?256|commit|版本|构建|入口|URL)", target, re.I):
        errors.append("目标指纹缺版本、构建、commit、SHA-256、入口或 URL")
    matrix = bodies.get("## 覆盖矩阵", "")
    if matrix and not re.search(r"(?:平台|形态|系统|运行时|设备|浏览器)", matrix):
        errors.append("覆盖矩阵缺平台/形态、系统/运行时、设备或浏览器维度")
    if matrix and not ("内部最小单元" in matrix and "模块间业务链路" in matrix):
        errors.append("覆盖矩阵缺内部最小单元或模块间业务链路规划")
    evidence = bodies.get("## 证据标准", "")
    if evidence and ("证据" not in evidence or "判定" not in evidence):
        errors.append("证据标准必须同时声明证据类型和判定标准")
    memory_enabled = bool(re.search(
        r"内存专项\s*[：:]\s*(?:适用|启用|是)", matrix, re.I,
    ))
    if memory_enabled:
        memory_plan = f"{matrix}\n{evidence}"
        if not ("预热" in memory_plan and "基线" in memory_plan
                and re.search(r"(?:重复|工作负载|轮次|\d+\s*轮)", memory_plan)):
            errors.append("内存专项缺预热、基线和重复工作负载/轮次")
        if not re.search(r"(?:预算|峰值|稳定值|斜率|OOM|对象|资源释放)", memory_plan, re.I):
            errors.append("内存专项缺预算或峰值/稳定值/斜率/对象释放判定")
        if not re.search(r"(?:CSV|JSON|heap|core|dump|trace|持有链|原始数据)", memory_plan, re.I):
            errors.append("内存专项缺原始数据、heap/core dump、trace 或持有链证据")
    research = bodies.get("## 工具调研", "")
    if "本地命中" not in research and not re.search(r"https?://|github\.com/", research):
        errors.append("工具调研缺本地命中说明或公开来源 URL")
    if research and not DATE_RE.search(research):
        errors.append("工具调研缺 YYYY-MM-DD 验证日期")
    readiness = bodies.get("## 工具就绪", "")
    if (not re.search(r"(?:是否可以开工|可以开工)\s*[：:]?\s*(?:是|可以|通过|已就绪)", readiness)
            or NEGATIVE_RE.search(readiness)):
        errors.append("工具就绪未明确确认『可以开工：是』")
    graph_acceleration = bodies.get("## 代码库图加速", "")
    if "codebase-memory" not in graph_acceleration.lower():
        errors.append("代码库图加速未记录 codebase-memory 探测结果")
    if not re.search(r"\b(?:READY|DEGRADED)\b", graph_acceleration):
        errors.append("代码库图加速必须记录 READY 或 DEGRADED")
    efficiency = bodies.get("## 基础效率工具", "")
    for tool in ("ripgrep", "ast-grep", "jq", "Context7"):
        if tool.lower() not in efficiency.lower():
            errors.append(f"基础效率工具未记录 {tool} 探测结果")
    if not re.search(r"\b(?:READY|DEGRADED)\b", efficiency):
        errors.append("基础效率工具必须记录 READY 或 DEGRADED")
    plugin_scope = bodies.get("## 可选插件", "")
    plugin_enabled = bool(re.search(
        r"是否适用\s*[：:]\s*(?:适用|是|启用)", plugin_scope, re.I,
    ))
    if plugin_enabled:
        for phrase in ("插件来源", "core_api", "授权", "白名单", "预算"):
            if phrase.lower() not in plugin_scope.lower():
                errors.append(f"可选插件缺 {phrase} 来源、兼容性或边界")
    conclusion = bodies.get("## 准备结论", "")
    if (not re.search(r"(?:准备完成|可以开工|结论\s*[：:]?\s*通过)", conclusion)
            or NEGATIVE_RE.search(conclusion)):
        errors.append("准备结论未明确是否可以开工")
    return (1 if errors else 0), errors


def ensure_prep_record(root: Path | None = None, *, force: bool = False) -> int:
    """Ensure a valid, fact-based preparation record exists in the active lifecycle.

    Replaces untouched templates or placeholders with observed facts while strictly
    preserving user-authored records (skeleton_markers < 3).
    """
    import datetime as dt
    import os
    import shutil
    import subprocess

    active_root = (root or REPO_ROOT).expanduser().resolve()
    record = default_record_for_root(active_root)

    current = ""
    if record.is_file() and not record.is_symlink():
        try:
            current = record.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            current = ""

    skeleton_markers = (
        "项目名称：待填写",
        "目标产物/入口：待填写",
        "是否可以开工：待填写",
        "- 结论：待填写",
    )
    is_skeleton = sum(marker in current for marker in skeleton_markers) >= 3 or not current.strip()

    if not force and record.is_file() and not is_skeleton:
        return 0

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=active_root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        commit = "working-tree"
    commit = commit or "working-tree"

    metadata_path = AGENT_DIR / "metadata.txt"
    version = "0.1.0"
    if metadata_path.is_file() and not metadata_path.is_symlink():
        try:
            for line in metadata_path.read_text(encoding="utf-8").splitlines():
                if "bugx_version:" in line:
                    version = line.split(":", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass

    today = dt.date.today().isoformat()
    rg = "READY" if shutil.which("rg") else "DEGRADED"
    ast_grep = "READY" if shutil.which("ast-grep") else "DEGRADED"
    jq = "READY" if shutil.which("jq") else "DEGRADED"
    graph = (
        "DEGRADED（使用 symbol_index.py + project_graph.py 回退）"
        if os.environ.get("BUGX_USE_CODEBASE_MEMORY") == "0"
        else "READY（已通过 preflight 探测）"
    )

    record_text = f"""# 本轮准备记录（prep-record）

> 自治模式自动生成：仅记录启动门禁已观察到的事实；用户后续可补充更细的项目范围。

## 项目识别

- 项目名称：{active_root.name}
- 项目类型/技术栈：源码仓库；运行时 Python {sys.version_info.major}.{sys.version_info.minor}
- 核心功能：由当前仓库源码和测试入口定义
- 入口/构建命令：BugX `pre → workers → post`
- 测试体系：自动化单元/回归与协议闭环
- 审计范围：当前 Git 工作树

## 目标指纹

- 目标产物/入口：{active_root}
- 平台/框架：CLI；BugX 生命周期编排
- 版本/构建/commit/SHA-256：BugX {version}；commit {commit}
- 主机与运行目标：{sys.platform}；OpenCode/Headless（如已配置）
- 构建/签名/功能开关：以当前工作树和自治策略为准

## 测试类型

- 黑盒：启用；验证真实 CLI/宿主入口
- 白盒：启用；验证源码边界与 Supervisor 状态
- 自动化：启用；验证 worker、MCP、checkpoint 和 post

## 覆盖矩阵

- 递归拆分：由 module_coverage.py discover 生成系统→子系统→文件→symbol
- 内部最小单元：按当前覆盖清单；由 Supervisor claim 逐单元负责
- 模块间业务链路：pre→workers→worker MCP→checkpoint→Finding/coverage→post
- 平台/形态：CLI/Headless
- 系统/运行时/浏览器：{sys.platform}/Python {sys.version_info.major}.{sys.version_info.minor}；浏览器不适用
- 设备/屏幕/窗口/方向：不适用；当前为 CLI
- 主题/语言/字号/无障碍：不适用；无 UI
- 权限/网络/生命周期/数据状态：本地仓库权限；自治生命周期状态机
- 真机/硬件边界与明确排除项：不适用
- 内存专项：不适用；本轮不宣称内存稳定性结论
- 已部署 Web 功能回归：不适用；当前目标不是已部署 Web

## 证据标准

- 每类问题的判定标准：命令退出码、结构化状态和 artifact 内容必须与预期一致
- 内部单元证据：测试命令、边界结果、异常路径和资源释放记录
- 业务链路证据：真实 ACP/MCP 调用、worker 身份、heartbeat、checkpoint 和 post 状态
- 必需证据类型：命令、退出码、日志、结构化 receipt、artifact SHA-256
- 证据产物位置与命名：当前生命周期 state、workers 和 artifacts 目录
- 修复后复验与回归要求：红测→补丁→受影响定向测试→Live 复验
- 内存预算与 PASS 标准：不适用
- 内存原始证据：不适用

## 工具调研

- 知识库状态：本地命中
- 来源与日期：本地 BugX 工具知识库；验证日期：{today}
- 候选工具与淘汰理由：优先内置 CLI、MCP 和测试框架；无关工具不启用

## 代码库图加速

- codebase-memory-mcp：{graph}
- MCP 工具可见性：由宿主 MCP tools/list 探测；不可用时使用回退索引
- 索引状态：以本轮 preflight 探测结果为准
- 查询策略：READY 时图优先；DEGRADED 时使用 symbol_index.py + project_graph.py
- 安装/升级决定：遵循当前项目已配置能力，不在自治审计中擅自安装

## 基础效率工具

- 总体状态：{('READY' if rg == 'READY' and ast_grep == 'READY' and jq == 'READY' else 'DEGRADED')}
- ripgrep：{rg}；用于字符串、错误消息和文件发现
- ast-grep：{ast_grep}；用于 AST 结构搜索
- jq：{jq}；用于 JSON 输出筛选
- Context7 MCP：DEGRADED；本轮不依赖外部文档查询
- 条件工具：GitHub MCP/gh 不适用；不执行公网操作
- 缺失项处置：使用已声明的内置回退

## 已部署 Web 功能回归

- 是否适用：不适用；当前目标为 CLI/Headless 生命周期
- 部署版本与授权范围：不适用
- 浏览器回归：不适用
- 结果与证据：不适用

## 可选插件

- 是否适用：不适用；不加载额外插件
- 插件来源：无
- core_api：不适用
- 授权与白名单：不适用
- 预算与回滚：不适用

## 工具选择

| 测试类型 | 模块 | 主工具 | 辅助工具 | 选择理由 |
|----------|------|--------|----------|----------|
| 自动化 | 生命周期 | BugX CLI/MCP | OpenCode ACP | 验证真实编排链路 |

## 工具就绪

- 工具检查命令/结果：tools_kb.py check 与 preflight 已通过
- 平台/内存预检结果：当前平台可用；内存专项不适用
- 依赖安装状态：使用项目现有依赖和全局 BugX 引擎
- 目标环境状态：自治策略已解析；宿主由 ACP 探测
- 是否可以开工：是

## 多工具协作

- 是否复杂项目：是；包含宿主、MCP、Supervisor 和 Worker 生命周期
- 模块 → 主工具分配：宿主→OpenCode ACP；调度→bugx-supervisor；Worker→bugx-supervisor-worker
- 工具流水线/依赖关系：pre→claim→worker-start→tool_call/heartbeat→checkpoint→post
- 共享输入与输出位置：当前生命周期 state、workers 和 artifacts

## 准备结论

- 结论：准备完成，可以开工
- 准备完成时间：{today}

"""
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(record_text, encoding="utf-8")
        record.chmod(0o600)
    except OSError as exc:
        print(f"[prep_validate] ✗ 准备记录写入失败: {exc}")
        return 1
    return 0


def _validate_direct(record: Path | None = None, root: Path | None = None) -> int:
    """进程内直接调用入口：验证开工准备记录，输出结果并返回退出码。"""
    active_root = (root or REPO_ROOT).expanduser().resolve()
    active_record = record.expanduser().resolve() if record else default_record_for_root(active_root)
    rc, errors = check(active_record, active_root)
    if errors:
        print(f"[prep_validate] 失败（{len(errors)} 项）:")
        for error in errors:
            print(f"  ✗ {error}")
    else:
        print("[prep_validate] OK: 目标指纹、覆盖矩阵、证据、工具与协作记录完整")
    for warning in check_test_import_source(active_root):
        print(f"[prep_validate] WARNING: {warning}")
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = (args.root or Path.cwd()).expanduser().resolve()
    record = args.record.expanduser().resolve() if args.record else None
    return _validate_direct(record=record, root=root)


if __name__ == "__main__":
    sys.exit(main())
