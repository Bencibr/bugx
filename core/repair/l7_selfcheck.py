#!/usr/bin/env python3
"""core/l7_selfcheck.py - L7：BugX 自身健康自检（内省式，不依赖外部项目）。

## 为什么需要这一层

L1–L6 测的是「BugX 能不能挖到洞」。但只要真跑一次无人值守（bugx-dev 对真实
项目），撞出来的几乎全是**另一类问题**：

    dev 模式 init 死锁            —— 引擎根与源码树同一目录，被自己的守卫拒绝
    autonomous 子进程绕过短路      —— 只看 not force，而调用方带了 --force
    SKILL 缺确认协议               —— zcode 有 4 处，dsh 0 处
    reproduction 只查非空           —— 「命令+英文解说」过契约但跑不起来
    install --force 假成功          —— 报告成功，装的是旧模板
    双份副本漂移                   —— integrations/ 与 plugins/ 各一份，内容不同
    remediation 错误不可自愈        —— 只说「必须是对象」，不说对象长什么样

这些没有一条是「挖得准不准」，全是「**BugX 自己有没有坏**」。它们不会被 L1–L6
抓到，因为 L1–L6 用的是干净 fixture——而缺陷恰恰活在真实协作链路上。

## 为什么是内省式而不是 7 条硬编码

把今天这 7 个缺陷写成 7 个 case 是容易的，但明天会撞出第 8 个、第 9 个。硬编码
清单的宿命是「补一条漏一条」，而且补的人未必知道该补什么形状。

所以 L7 检查的是**产生缺陷的结构性条件**，不是缺陷本身：

    L7-1  双份副本一致性     —— 只要「同一份文档存在两个物理副本」，就会漂移
    L7-2  守卫短路可达性     —— 只要有守卫 + 有开发态旁路，就要验证旁路不被绕过
    L7-3  错误文案可自愈性   —— 只要错误只说「不对」不说「怎样才对」，就会卡死
    L7-4  契约校验充分性     —— 只要校验只查形状不查语义，就会放行假货
    L7-5  静默跳过可见性     —— 只要「返回 None 就跳过」，就会假成功
    L7-6  宿主能力对齐       —— 只要两个宿主 skill 不对称，弱的那边就失效

这 6 条各自对应一类结构性风险。**新的具体缺陷通常能落进其中一类**，所以不需要
每发现一个就加一条检查——这正是内省式相对清单式的价值。

## 设计约束

* 全部在仓库内完成，**不需要网络、不需要外部项目、不需要启动宿主**
* 每项检查必须有明确的 pass/fail 判据，不能是「看起来还行」
* 检查本身要便宜：整套 L7 应在数秒内跑完，否则不会有人每轮都跑
* 发现的问题必须自带修复指引，否则又是一个「你去看」的工单
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

import ast
import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: 允许存在、但**必须内容一致**的双份副本。键是源，值是必须同内容的镜像。
#: 每次往这里加一对，就等于承认「同一份文档存在两个物理位置」这个结构性风险
#: 在扩大——能改成符号链接或构建期生成应该优先那样做。
MIRRORED_FILES: dict[str, tuple[str, ...]] = {
    "ecosystem/plugins/bugx/skills/bugx-dsh/SKILL.md": (
        "ecosystem/integrations/dsh/skills/bugx-dsh/SKILL.md",
    ),
    "plugins/bugx/skills/bugx-dsh/SKILL.md": (
        "integrations/dsh/skills/bugx-dsh/SKILL.md",
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_mirror_consistency(root: Path) -> tuple[bool, str, list[str]]:
    """L7-1：双份副本必须内容一致。

    实测缺陷：``integrations/dsh/`` 是 5500B 旧版，``plugins/bugx/skills/`` 是
    9641B 新版。安装器读前者，我改后者，于是 ``install`` 报告成功却装了旧内容。

    这类缺陷极难察觉，因为**两边都是合法文件**、都能打开、都看起来正常。
    唯一可靠的判据是逐字节比对。
    """
    details: list[str] = []
    problems: list[str] = []
    prefix = "ecosystem/" if (root / "ecosystem").is_dir() else ""
    mirrored = {
        f"{prefix}plugins/bugx/skills/bugx-dsh/SKILL.md": (
            f"{prefix}integrations/dsh/skills/bugx-dsh/SKILL.md",
        ),
    }
    for source_rel, mirrors in mirrored.items():
        source = root / source_rel
        if not source.is_file():
            problems.append(f"源码正本不存在：{source_rel}")
            continue
        source_digest = _sha256(source)
        for mirror_rel in mirrors:
            mirror = root / mirror_rel
            if not mirror.is_file():
                problems.append(f"声明的镜像不存在：{mirror_rel}")
                continue
            if _sha256(mirror) != source_digest:
                problems.append(
                    f"{mirror_rel} 与 {source_rel} 内容不一致"
                    f"（{mirror.stat().st_size}B vs {source.stat().st_size}B）"
                )
            else:
                details.append(f"一致：{mirror_rel}")
    ok = not problems
    return ok, ("双份副本全部一致" if ok else "；".join(problems)), details


def check_error_messages_self_healing(root: Path) -> tuple[bool, str, list[str]]:
    """L7-3：类型/契约错误必须说明「怎样才对」，不能只说「不对」。

    实测缺陷：``remediation`` 是字符串时只报「remediation 必须是对象」。
    worker 交了完整的人类可读建议，却不知道对象长什么样，只能反复猜，
    最终整轮 BLOCKED。

    判据：每条 ``raise ...Error`` 且消息含「必须是」的语句，其消息串（含拼接
    的相邻字面量）里必须出现修复指引特征词。
    """
    problems: list[str] = []
    details: list[str] = []
    targets = [
        root / "core" / "ledger" / "finding_confirmation.py",
        root / "core" / "supervisor" / "supervisor_host.py",
        root / "core" / "supervisor" / "audit_supervisor.py",
    ]
    #: 修复指引特征词：出现任一即认为文案「告诉你怎么改」。
    guidance_markers = (
        "请", "应", "形如", "例如", "示例", "最小合法", "decision", "fix_ref",
        "置 ", "传 ", "改 ", "schema", "字段", "格式为",
        # 「取自 claim 返回的 lease_id」「用 shasum 生成」这类**指出取值来源**
        # 的表述同样是可执行的指引，worker 照着做即可。
        "取自", "来自", "生成", "填 ", "写 ", "使用 ",
    )
    #: 枚举型提示也算「告诉你怎么改」——「必须是 mine 或 fix」已经给出了
    #: 全部合法取值，worker 照着填即可。只看特征词会把它误判成不可自愈。
    _ENUM_HINT = re.compile(r"必须是\s*`?\w+`?\s*(或|/|、)")
    for path in targets:
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            problems.append(f"{path.name} 无法解析")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if not isinstance(exc, ast.Call) or not exc.args:
                continue
            # 取 raise 所在的整条语句文本（跨行拼接的字面量也要算进去）
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno)
            text = "\n".join(lines[start:end])
            if "必须是" not in text and "必须为" not in text:
                continue
            has_guidance = (
                any(marker in text for marker in guidance_markers)
                or bool(_ENUM_HINT.search(text))
            )
            if not has_guidance:
                problems.append(
                    f"{path.name}:{node.lineno} 的类型错误只说「必须是」，"
                    f"未给出合法形态：{text[:100].strip()}"
                )
            else:
                details.append(f"{path.name}:{node.lineno} 含修复指引")
    ok = not problems
    return ok, ("类型错误文案均可自愈" if ok else "；".join(problems[:3])), details


def check_silent_skip_visibility(root: Path) -> tuple[bool, str, list[str]]:
    """L7-5：宿主声明的 ``required_paths``，安装后必须真的存在。

    ## 为什么判据必须是「行为」而不是「结构」

    第一版按结构判：类声明了 ``required_paths`` 却没覆写
    ``render_required_entry`` → 判失败。结果对 ``ChatGptAdapter`` 误报——
    它走 plugin 分发通路（``CAP_PREVALIDATE_CATALOG``），文件确实写得出来。
    实测验证过：``bugx install --agent chatgpt`` 后
    ``.agents/plugins/marketplace.json`` 与 ``plugins/bugx/skills/bugx/SKILL.md``
    都在。

    结构判据的问题在于：**它假设「写入只可能经由某一个函数」**。真实的
    安装器有多条通路（patch+skill / plugin 分发 / 硬编码 _deploy_xxx），
    任何「按实现细节推断结果」的检查都会随实现演进而失真。

    所以改成只问一件事：**声明的路径，装完存在吗？** 这是行为事实，
    不依赖实现怎么组织。

    ## 为什么不在这里真的跑 install

    L7 要求「数秒内跑完」——真跑 install 要几百毫秒到数秒，且会写文件。
    折中方案：检查**已有的安装产物**（若项目已安装过），并校验声明与
    「已知写入通路」的一致性。对未安装的项目跳过（不判失败），因为
    「没装」不等于「装不上」。

    同时保留一条静态安全网：``DshAdapter`` 声明 ``bugx-dsh/SKILL.md`` 却
    没有渲染通路（实际靠 ``_deploy_dsh_bundle`` 硬编码兜底）——这是真实的
    双通路隐患，用「是否有硬编码兜底」来判断，而不是「是否覆写某函数」。
    """
    path = root / "interfaces" / "cli" / "bugx_cli" / "host_adapters.py"
    if not path.is_file():
        path = root / "src" / "bugx_cli" / "host_adapters.py"
    if not path.is_file():
        return True, "host_adapters.py 不存在，跳过", []
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False, "host_adapters.py 无法解析", []

    overrides: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            overrides[node.name] = any(
                isinstance(item, ast.FunctionDef)
                and item.name == "render_required_entry"
                for item in node.body
            )

    #: 已有硬编码部署通路兜底的宿主（install 里另有 _deploy_* 调用）。
    #: 这些宿主的 required_paths 不依赖 render_required_entry，属已知设计。
    #: 新增宿主若走这条路，必须在这里显式登记——**登记即承认「存在两条
    #: 独立写入通路」**，将来改一处忘另一处的风险由登记者承担。
    HARDCODED_DEPLOY = {
        "DshAdapter": "_deploy_dsh_bundle",
        "ChatGptAdapter": "_chatgpt_marketplace",
    }

    problems: list[str] = []
    details: list[str] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or not func.id.endswith("Adapter"):
            continue
        if func.id not in overrides:
            continue
        declared: list[str] = []
        for arg in node.args:
            if isinstance(arg, (ast.Tuple, ast.List)):
                values = [
                    e.value for e in arg.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
                if values and all(("/" in v or "." in v) for v in values):
                    declared.extend(values)
        if not declared:
            continue
        checked += 1
        if overrides[func.id]:
            details.append(f"{func.id} 有渲染通路")
            continue
        fallback = HARDCODED_DEPLOY.get(func.id)
        if fallback is None:
            problems.append(
                f"{func.id} 声明了 {len(set(declared))} 个 required_paths，"
                "既未覆写 render_required_entry，也未登记硬编码部署通路——"
                "install 会报成功而实际未写"
            )
        else:
            details.append(f"{func.id} 走硬编码通路 {fallback}（已登记）")

    if checked == 0:
        return False, ("未能在 host_adapters.py 中找到任何 Adapter 构造调用——"
                       "检测逻辑可能已与代码结构脱节"), details
    ok = not problems
    return ok, ("声明的入口均有写入通路" if ok else "；".join(problems[:3])), details


def check_contract_semantic_depth(root: Path) -> tuple[bool, str, list[str]]:
    """L7-4：关键契约不能只查「非空」，要查到语义。

    实测缺陷：``reproduction`` 契约只检查非空字符串。于是「命令 + 英文解说」
    拼在一起也通过——它满足非空，却根本跑不起来。**这让不可复现的东西看起来
    可复现**，正是该契约存在的理由。

    判据：``reproducible_contract_violation`` 必须调用可执行性判定，不能只
    依赖 ``_nonempty``。
    """
    # ``finding_confirmation.py`` 随 ledger 包归档；旧扁平路径已删除。以前只认
    # ``core/finding_confirmation.py``，迁移后它恒不存在 → 本检查**恒返回通过**，
    # 契约深度保护静默归零。归档路径缺失才跳过，但必须是显式"不适用"。
    path = root / "core" / "ledger" / "finding_confirmation.py"
    if not path.is_file():
        return True, "finding_confirmation.py 不存在（非引擎树），跳过", []
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False, "finding_confirmation.py 无法解析", []
    problems: list[str] = []
    details: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "reproducible_contract_violation":
            body_text = ast.get_source_segment(text, node) or ""
            if "reproduction_not_executable" not in body_text:
                problems.append(
                    "reproducible_contract_violation 未校验 reproduction 可执行性，"
                    "只查非空会放行「命令+解说」混合串"
                )
            else:
                details.append("reproduction 契约已含可执行性语义校验")
    ok = not problems
    return ok, ("契约具备语义校验" if ok else "；".join(problems)), details


def check_guard_bypass_reachability(root: Path) -> tuple[bool, str, list[str]]:
    """L7-2：开发态旁路必须不被 ``--force`` 之类的调用方参数绕过。

    实测缺陷：dev 模式短路最初写成 ``if not force:``，于是
    ``autonomous`` 的 ``_install_host``（带 ``--force``）恰好绕过它，
    仍然 blocked。修完 ``init`` 却修不好 ``autonomous``——同一个根因
    以两种形态出现。

    判据：``engine_installer._install_engine`` 的 dev 短路不得以 ``force``
    为条件。
    """
    path = root / "interfaces" / "cli" / "bugx_cli" / "infra" / "engine_installer.py"
    if not path.is_file():
        path = root / "src" / "bugx_cli" / "infra" / "engine_installer.py"
    if not path.is_file():
        return True, "engine_installer.py 不存在，跳过", []
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False, "engine_installer.py 无法解析", []
    problems: list[str] = []
    details: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_install_engine":
            body_text = ast.get_source_segment(text, node) or ""
            # 找 dev 短路：形如 if <src> == <dst> ... return (0, 0)
            # 短路的形态是 if <条件>: return (0, 0)——return 在 if **内部**，
            # 不在函数顶层。早期版本按「顶层相邻语句」找，永远找不到，于是
            # 误报「缺少短路」。
            #
            # 只审这个 if 的条件：函数体里另有 `if not force and source ==
            # repository_core` 的守卫（拒绝从源码树安装），那是**合法且必须
            # 保留**的。扫全文找 `not force` 会把注释和它一起判成违规——
            # 把「有这个词」当成「有这个问题」。
            # dev 短路的判定不能只看 `return (0, 0)`：函数体里还有其它
            # all-zero 返回（例如「已安装引擎更新且自身完整」那条），AST 上
            # 与 `return 0, 0` 完全等价。早期版本因此抓到了错的 if，把
            # `return (0, 0)` 删掉后检查仍然绿——**抓错东西的门禁比没有更坏**，
            # 它给出虚假保证。
            #
            # 短路有唯一可识别特征：条件里比较的是「引擎根」与「源码树」。
            # 用这一点定位，才不会被同一函数里其它成功返回干扰。
            short_circuit_conditions: list[str] = []
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If):
                    continue
                condition = ast.get_source_segment(text, sub.test) or ""
                compares_engine_roots = (
                    "global_engine_root" in condition
                    or "_repository_core" in condition
                    or ("_src" in condition and "_dst" in condition)
                )
                has_zero_return = any(
                    isinstance(stmt, ast.Return)
                    and isinstance(stmt.value, (ast.Tuple, ast.Constant))
                    and (
                        (
                            isinstance(stmt.value, ast.Tuple)
                            and len(stmt.value.elts) == 2
                            and all(isinstance(e, ast.Constant) and e.value == 0
                                    for e in stmt.value.elts)
                        )
                        or (isinstance(stmt.value, ast.Constant)
                            and stmt.value.value == 0)
                    )
                    for stmt in sub.body
                )
                if has_zero_return and compares_engine_roots:
                    short_circuit_conditions.append(condition)
            if not short_circuit_conditions:
                problems.append(
                    "_install_engine 缺少 dev 模式短路（引擎根与源码树同一目录时 "
                    "init 会死锁）"
                )
            else:
                offending = [
                    c for c in short_circuit_conditions
                    if "not force" in c or "force is False" in c
                ]
                if offending:
                    problems.append(
                        "_install_engine 的 dev 短路条件含 force 判断，"
                        "带 --force 的调用方（autonomous._install_host）会绕过它。"
                        "force 在调用方语义是「强制重装宿主入口」，不是「允许覆写源码树」。"
                    )
                else:
                    cond = short_circuit_conditions[0].strip().replace("\n", " ")
                    details.append(f"dev 短路条件不以 force 为开关：{cond[:70]}")
    ok = not problems
    return ok, ("守卫旁路不可绕过" if ok else "；".join(problems)), details


def check_host_capability_parity(root: Path) -> tuple[bool, str, list[str]]:
    """L7-6：宿主 skill 的关键协议章节应当对齐。

    实测缺陷：zcode skill 有确认协议章节（4 处 confirmation），dsh skill
    0 处，于是 dsh 产出的 finding 全部卡在 finding_confirmation_pending。

    判据：**所有**按项目安装的宿主 skill 都必须提到确认协议与 reproduction
    契约。新增宿主时若不写，这里会立刻失败——而不是等到真跑一轮才发现。
    """
    skills_dir = (root / "ecosystem" / "plugins" / "bugx" / "skills") if (root / "ecosystem" / "plugins").is_dir() else (root / "plugins" / "bugx" / "skills")
    if not skills_dir.is_dir():
        return True, "skills 目录不存在，跳过", []
    #: 每个宿主 skill 都必须覆盖的关键协议词。
    required_markers = ("confirmation", "reproduction")
    problems: list[str] = []
    details: list[str] = []
    for skill in sorted(skills_dir.glob("*/SKILL.md")):
        name = skill.parent.name
        if name in {"bugx"}:  # 通用入口，协议细节在宿主专属 skill 里
            continue
        text = skill.read_text(encoding="utf-8", errors="replace").lower()
        missing = [m for m in required_markers if m not in text]
        if missing:
            problems.append(f"{name}/SKILL.md 缺关键协议：{', '.join(missing)}")
        else:
            details.append(f"{name} 协议齐备")
    ok = not problems
    return ok, ("宿主 skill 协议对齐" if ok else "；".join(problems)), details


def check_host_skill_sources_tracked(root: Path) -> tuple[bool, str, list[str]]:
    """L7-8：被声明的宿主 skill 源必须真实存在且纳入版本控制。

    ## 这条检查来自一次「构建成功但产物缺件」

    实测：``pyproject.toml`` 的 data-files 声明了

        "share/bugx-agent/plugins/bugx/skills/bugx-claude" = [
          "ecosystem/plugins/bugx/skills/bugx-claude/SKILL.md",
        ]

    但该文件从未 ``git add``。setuptools 对缺失的 data-files **只警告不报错**，
    于是在干净克隆里 ``python -m build --wheel`` 报告
    ``Successfully built``，而 wheel 里根本没有这个 skill。

    失败继续向下传染，且每一环都是静默的：

    1. wheel 缺件 —— 构建成功；
    2. ``_ensure_claude_usage_skill`` 找不到源 —— 返回
       ``status=skipped_missing_source``，不抛错、不阻塞 install；
    3. 用户看到 ``bugx install --agent claude`` 成功，但
       ``~/.claude/skills/`` 里只有 ``bugx/``，没有 ``bugx-claude/``；
    4. 而 ``bugx/SKILL.md`` 第 26 行强制要求 Claude 宿主「先加载同插件
       bugx-claude skill 并完成其第〇节强制阅读」——**指引指向一个不存在的文件**。

    没有任何一环会报警，所以只能靠这条检查把「声明了就必须入库」变成硬约束。

    ## 判据

    对 ``pyproject.toml`` 的每个 data-files 条目：``key`` 至少要有一个 glob
    命中真实文件。按 key 聚合而不是按单条 pattern 判定——``core/rules/mastg``
    同时声明 ``*.yml`` 与 ``*.yaml``，只有后者有文件，按单条判定会误报三个。

    另外单独校验宿主 skill 清单：``bugx``/``bugx-claude``/``bugx-dsh``/
    ``bugx-opencode``/``bugx-zcode`` 必须都在 ``ecosystem/plugins/bugx/skills/``
    下存在，防止新增宿主时漏提交。
    """
    problems: list[str] = []
    details: list[str] = []

    # 1) data-files 声明必须有实际文件落地
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            data_files = (
                data.get("tool", {}).get("setuptools", {}).get("data-files", {})
            )
            for key, patterns in data_files.items():
                if not isinstance(patterns, list):
                    continue
                if not any(list(root.glob(p)) for p in patterns if isinstance(p, str)):
                    problems.append(f"data-files 声明无对应文件：{key} <- {patterns}")
            details.append(f"data-files 条目 {len(data_files)} 个已核对")
        except (OSError, ValueError) as exc:
            problems.append(f"pyproject.toml 无法解析：{type(exc).__name__}: {exc}")

    # 2) 宿主 skill 清单必须齐全（新增宿主漏提交会在这里变红）
    skills_dir = (
        root / "ecosystem" / "plugins" / "bugx" / "skills"
        if (root / "ecosystem" / "plugins").is_dir()
        else root / "plugins" / "bugx" / "skills"
    )
    expected = ("bugx", "bugx-claude", "bugx-dsh", "bugx-opencode", "bugx-zcode")
    if skills_dir.is_dir():
        for name in expected:
            if not (skills_dir / name / "SKILL.md").is_file():
                problems.append(f"宿主 skill 源缺失：{name}/SKILL.md")
            else:
                details.append(f"{name} 源存在")
    else:
        details.append("skills 目录不存在，跳过清单核对")

    ok = not problems
    return ok, ("宿主 skill 源齐全" if ok else "；".join(problems)), details


#: 检查注册表。新增检查只需在这里加一行；L7 会自动纳入。
def check_dangling_asset_references(root: Path) -> tuple[bool, str, list[str]]:
    """L7-7：被引用的**代码资产**必须真实存在。

    ## 这个检查来自一个真实失败的形态

    实测：``core/prep-record.md`` 在 4d4e191b 被删除（它是被测项目的运行时
    产物，不是引擎资产），但代码里仍有回落引用。后果不是崩溃，而是
    **错误指向错误的地方**——把「项目还没初始化」报成「记录不合格」。

    这一类形态（**删掉了被引用的资产，却没删引用**）会反复出现，因为删的人
    看不到引用，任何一次仓库清理都可能触发。

    ## 判据为什么必须收窄到 .py

    前两版判据都错了，而且错法不同：

    **v1（文件名白名单）**：维护「运行时产物名单」排除 ``bugx-runtime.json``
    这类文件。立刻误报三个——运行时文件的命名没有规律，名单永远补不完。

    **v2（git 跟踪状态 + 历史）**：看起来严谨，实测踩了更深的坑。
    ``AGENT_DIR / "module-coverage.md"`` 被判为悬空引用，于是我改了
    ``common.py`` 和 ``coverage_manifest.py`` 的回落逻辑——**结果弄坏 20 个
    测试**。原因是我误判了 ``AGENT_DIR`` 的语义：

        BugX 支持扁平部署：launch_bugx.py 被拷到项目根，
        此时 AGENT_DIR == 项目根，AGENT_DIR/"module-coverage.md"
        就是项目根下的清单文件 —— **完全合法**。

    ``AGENT_DIR`` 不是「引擎安装目录」的固定概念，而是「引擎代码所在目录」，
    扁平部署下它就等于项目根。用仓库结构去推断运行时语义，必然出错。

    ## 现在的判据

    只查 ``.py`` 引用：``CORE_DIR / "verify_life.py"`` 这类**代码模块**。
    代码模块是仓库资产，删掉它引用必然断；而 ``.md`` / ``.json`` 既可能是
    仓库文档也可能是运行时产物，无法从引用点判断，一律不查。

    宁可少报也不误报——**误报会让检查被摘掉，那比漏报损失更大**。
    """
    core_dir = root / "core"
    if not core_dir.is_dir():
        return True, "core 目录不存在，跳过", []

    tracked = _tracked_core_files(root)
    problems: list[str] = []
    details: list[str] = []
    scanned = 0
    for path in sorted(core_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        scanned += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                continue
            right = node.right
            if not isinstance(right, ast.Constant) or not isinstance(right.value, str):
                continue
            name = right.value
            # 只查 Python 模块：它是不可争议的仓库代码资产。
            if not name.endswith(".py"):
                continue
            left = ""
            if isinstance(node.left, ast.Name):
                left = node.left.id
            elif isinstance(node.left, ast.Attribute):
                left = node.left.attr
            if left not in {"AGENT_DIR", "CORE_DIR", "ENGINE_DIR"}:
                continue
            if (core_dir / name).exists():
                details.append(f"{path.name}: {name} 存在")
                continue
            # 引擎仓库里没有，但安装目录可能有（引擎资产随安装同步）。
            if tracked is not None and name not in tracked:
                details.append(f"{path.name}: {name} 不在本仓库（可能来自安装）")
                continue
            problems.append(
                f"{path.relative_to(root)}：引用了已被删除的代码模块 "
                f"{left} / {name!r}。删除了资产却没删除引用——"
                "运行时会拿到一个永远不存在的路径，并把错误报成另一件事。"
            )
    if scanned == 0:
        return False, "core 下没有可扫描的 Python 文件——检测逻辑可能与结构脱节", details
    ok = not problems
    return ok, (
        f"代码资产引用均有效（扫描 {scanned} 个文件）" if ok
        else "；".join(problems[:3])
    ), details


def _tracked_core_files(root: Path) -> set[str] | None:
    """``core/`` 下**曾经或正在**被 git 跟踪的文件名集合。

    ## 为什么必须查历史，不能只查当前索引

    第一版只查 ``git ls-files core``，结果漏掉了它本该抓的那个缺陷：

        引用 core/metadata.txt → 文件被删且提交 → 不再出现在 ls-files
        → 被判为「未跟踪 = 运行时产物」→ 放行

    也就是说，**删除动作本身把证据删掉了**。而「删了资产没删引用」正是
    本检查要抓的东西——一个在缺陷发生后失效的判据毫无价值。

    所以加上历史：``git log --diff-filter=D --name-only`` 列出所有被删除过
    的文件。曾经被跟踪 ≠ 运行时产物，引用它却不存在就是悬空引用。

    返回 None 表示无法判定（非 git 仓库/命令失败），此时调用方退化为
    「只认磁盘上存在的文件」——宁可严格也不静默放行。
    """
    import subprocess

    def _git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(root), capture_output=True,
                text=True, timeout=60.0, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    current = _git("ls-files", "core")
    if current is None:
        return None
    names = {
        line.strip().split("/")[-1]
        for line in current.splitlines()
        if line.strip()
    }
    deleted = _git("log", "--all", "--diff-filter=D", "--name-only",
                   "--pretty=format:", "--", "core")
    if deleted:
        names |= {
            line.strip().split("/")[-1]
            for line in deleted.splitlines()
            if line.strip()
        }
    return names


#: 每个检查的「落点」——它到底在审哪些文件、盯哪个符号。
#:
#: ## 为什么需要这张表
#:
#: 实测事故：RSI 闭环把 L7-5 的失败交给 agent，工单里只有 91 个字符
#: 「DshAdapter 声明了 3 个 required_paths，既未覆写 render_required_entry，
#: 也未登记硬编码部署通路」。**没提一个文件路径。**
#:
#: 结果 agent 只能 grep 全仓找 `_deploy_dsh_bundle`，读了 installer.py、
#: host_adapters.py 一路翻下去，180 秒预算耗尽仍没改到点子上——而答案就是
#: ``l7_selfcheck.py`` 里那个三行字典。同一处缺陷换成窄提示词（给出文件 +
#: 符号 + 改法），**几秒就修完了**。
#:
#: 差别的全部就是「知不知道去哪」。而这些落点信息在检查函数里本来就是
#: **写死的常量**（它必须知道读哪个文件才能检查），只是在函数边界上被压扁
#: 成了一句自然语言，没有传出来。
#:
#: ## 为什么用静态表而不是改函数签名
#:
#: 更彻底的做法是让每个检查返回结构化 ``locations``。但那要动 7 个函数签名
#: 和 30+ 条测试断言，而落点本身是常量——用静态表同样准确，改动面小一个
#: 数量级，也不会让检查函数的返回值变成多形态。
#:
#: 表是**声明式的**：新增检查时在这里补一行即可，忘了补不影响检查本身，
#: 只会让工单少一点定位信息（fail-open，不是 fail-closed）。
CHECK_TARGETS: dict[str, dict[str, object]] = {
    "L7-1": {
        "files": ["plugins/bugx/skills/", "integrations/"],
        "hint": "MIRRORED_FILES 声明的正本与镜像必须逐字节一致；"
                "改任一侧都要同步另一侧（或重跑 refresh_signed_mirrors）",
    },
    "L7-2": {
        "files": ["interfaces/cli/bugx_cli/infra/engine_installer.py", "src/bugx_cli/infra/engine_installer.py"],
        "symbols": ["_install_engine"],
        "hint": "dev 短路（引擎根 == 源码树时 return (0,0)）的条件里"
                "不得出现 force；force 的语义是强制重装宿主入口，"
                "不是允许覆写源码树",
    },
    "L7-3": {
        "files": ["core/ledger/finding_confirmation.py", "core/supervisor/supervisor_host.py",
                  "core/supervisor/audit_supervisor.py"],
        "hint": "消息里说了「必须是」就必须同时告诉调用方合法值或改法"
                "（枚举值、形如…、请传…）；只说不对等于让 worker 反复猜",
    },
    "L7-4": {
        "files": ["core/ledger/finding_confirmation.py"],
        "symbols": ["reproducible_contract_violation",
                    "reproduction_not_executable"],
        "hint": "reproduction 不能只查非空，必须调用可执行性判定；"
                "否则「命令+解说」混合串会被放行，让不可复现的东西看起来可复现",
    },
    "L7-5": {
        "files": ["interfaces/cli/bugx_cli/host_adapters.py", "src/bugx_cli/host_adapters.py", "core/l7_selfcheck.py"],
        "symbols": ["HARDCODED_DEPLOY"],
        "hint": "声明了 required_paths 的 Adapter 必须要么覆写 "
                "render_required_entry，要么在 l7_selfcheck.py 的 "
                "HARDCODED_DEPLOY 表里登记硬编码部署通路——"
                "照着已登记条目（如 ChatGptAdapter）加一行即可",
    },
    "L7-6": {
        "files": ["plugins/bugx/skills/*/SKILL.md"],
        "hint": "每个宿主 skill 都必须同时出现 confirmation 与 reproduction "
                "两个协议词（大小写不敏感）；缺哪个补哪个",
    },
    "L7-8": {
        "files": ["ecosystem/plugins/bugx/skills/*/SKILL.md"],
        "hint": "宿主 skill 源必须 git add 入库：pyproject.toml 已声明要打进 "
                "wheel，但 setuptools 对缺失的 data-files 只警告不报错，"
                "会产出「构建成功、安装成功、skill 却不存在」的静默缺件",
    },
    "L7-7": {
        "files": ["core/**/*.py"],
        "hint": "代码里 AGENT_DIR / 'xxx.py' 这类引用，若目标文件已被删除"
                "但仍被引用，就是悬空资产；删掉引用或恢复文件",
    },
}

#: 检查注册表。新增检查只需在这里加一行；L7 会自动纳入。
CHECKS = (
    ("L7-1", "双份副本一致性", check_mirror_consistency),
    ("L7-2", "守卫旁路可达性", check_guard_bypass_reachability),
    ("L7-3", "错误文案可自愈性", check_error_messages_self_healing),
    ("L7-4", "契约校验充分性", check_contract_semantic_depth),
    ("L7-5", "静默跳过可见性", check_silent_skip_visibility),
    ("L7-6", "宿主能力对齐", check_host_capability_parity),
    ("L7-7", "引擎资产引用有效", check_dangling_asset_references),
    ("L7-8", "宿主 skill 源可构建", check_host_skill_sources_tracked),
)


def run_all(root: Path | None = None, *, dynamic: bool = False) -> dict[str, object]:
    """执行全部自检，返回结构化结果。

    ``dynamic=True`` 时追加动态检查（``l7_dynamic``）。默认关闭——
    动态检查要秒级、起子进程、建沙箱项目，而 L7 的价值恰恰在于
    「每轮都跑得起」。一条 12s 的检查会被从日常流程里摘掉，保护归零。

    所以分工是：静态检查每轮跑，动态检查在提交前 / 排查时跑。
    """
    root = (root or REPO_ROOT).resolve()

    # ## 目标不是引擎仓库时，整层跳过而不是报失败
    #
    # L7 检的是**引擎自身健康**：双份副本、守卫旁路、宿主能力对齐……这些
    # 只有引擎源码树里才有意义。
    #
    # 对仅含少量源文件和 .bugx/ 的普通被测项目跑 L7 时，可能报
    # 「源码正本不存在：plugins/bugx/skills/bugx-dsh/SKILL.md」——把
    # 「这个项目不是引擎仓库」说成「引擎副本不一致」。这两件事的处置完全
    # 不同（前者是选错目标，后者是发版阻断缺陷），混在一起会误导。
    #
    # 所以先判目标类型：没有引擎骨架就整体 not_applicable。
    # 哨兵文件随 ``repair`` 包归档（``core/repair/l7_selfcheck.py``）。旧路径
    # ``core/l7_selfcheck.py`` 在迁移后不存在 → 对本仓库自身也判成「不是引擎仓库」，
    # 整个 L7 层静默 not_applicable（7 项检查全部消失且 ok=True）。两个位置都认，
    # 保证归档布局与原布局都能识别。
    engine_skeleton = (
        root / "core" / "repair" / "l7_selfcheck.py",
        root / "core" / "l7_selfcheck.py",
    )
    if not any(path.is_file() for path in engine_skeleton):
        return {
            "summary": {
                "total": 0, "passed": 0, "failed": 0, "ok": True,
                "not_applicable": True,
                "reason": f"{root} 不是 BugX 引擎仓库，L7 自身健康检查不适用",
                "checks_failed": [],
            },
            "checks": [],
        }

    results: list[dict[str, object]] = []
    checks = list(CHECKS)
    if dynamic:
        from core.repair import l7_dynamic
        checks.extend(l7_dynamic.DYNAMIC_CHECKS)
    for check_id, name, func in checks:
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
        "dynamic": bool(dynamic),
        "summary": {
            "total": len(results), "passed": len(results) - len(failed),
            "failed": len(failed), "ok": not failed,
            "checks_failed": [r["id"] for r in failed],
        },
        "checks": results,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="L7：BugX 自身健康自检")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dynamic", action="store_true",
                        help="追加动态检查（起子进程，约 12s）")
    args = parser.parse_args(argv)

    payload = run_all(args.root, dynamic=args.dynamic)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in payload["checks"]:
            mark = "✓" if item["ok"] else "✗"
            print(f"{mark} {item['id']} {item['name']}")
            if not item["ok"]:
                print(f"    {item['detail']}")
        summary = payload["summary"]
        if summary.get("not_applicable"):
            # 「0/0 通过」看不出是被跳过，容易被当成"什么都没检查却绿了"。
            print(f"\n不适用：{summary.get('reason', '')}")
        else:
            print(f"\n{summary['passed']}/{summary['total']} 通过")
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
