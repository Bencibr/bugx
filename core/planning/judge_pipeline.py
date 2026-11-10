#!/usr/bin/env python3
"""judge_pipeline.py — 判读式并发审计的确定性取证与收口（DOC-257）。

角色拆分（2026-11-03 性能复盘的根因修复）：

- **机器做流程**：claim、探针执行、artifact、sha256、execution_receipt、
  checkpoint 全部由本模块在宿主进程内确定性完成，毫秒级，零模型回合。
- **AI 只判断**：子代理拿到的不是任务包而是**判读包**（代码切片 + 边界
  电池实测结果），唯一职责是回答「这份数据里有没有真缺陷」，一个推理
  回合输出裁决 JSON。子代理不碰 claim/heartbeat/checkpoint 协议。

实测背景：旧 worker 路径每个动作都烧一个模型回合（35 回合 ≈ 18 分钟，
探针本身仅 0.4 秒）；判读式路径 = 取证秒级 + N 个 judge 并行一回合 +
收口秒级，墙钟收敛到最慢一个 judge 的推理时间。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

PACKAGE_VERSION = 1
_PROBE_TIMEOUT_SECONDS = 8.0
_MAX_SOURCE_CHARS = 6000
_MAX_OUTPUT_CHARS = 900
_MAX_PROBES_PER_UNIT = 8

_VERDICT_STATUSES = ("clean", "bug", "insufficient_data")


def unit_parts(unit_id: str) -> tuple[str, str]:
    """从覆盖 unit 提取 (file_path, symbol)；与 task_scheduler 的口径一致。"""
    text = str(unit_id).strip()
    for prefix in ("内部:", "业务:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    parts = text.split(":")
    if len(parts) > 1 and parts[0].isdigit():
        text = ":".join(parts[1:])
    if "::" in text:
        fp, sym = text.split("::", 1)
        return fp.strip(), sym.strip()
    return text.strip(), ""


def symbol_source(source: str, symbol: str) -> str:
    """提取 symbol 的源码切片（含 class 全体方法）；miss 时返回全文。"""
    if not symbol:
        return source
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    lines = source.splitlines()
    for node in ast.walk(tree):
        names = getattr(node, "name", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and names == symbol:
            span = list(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if isinstance(node, ast.ClassDef):
                for child in ast.walk(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and child.lineno not in span:
                        span.extend(range(child.lineno, (child.end_lineno or child.lineno) + 1))
            selected = [lines[i - 1] for i in sorted(set(span)) if 0 < i <= len(lines)]
            return "\n".join(selected)
    return source


# ---------------------------------------------------------------------------
# 边界电池：确定性探针生成与执行
# ---------------------------------------------------------------------------

def _module_name(file_path: str) -> str:
    """文件路径 → 可导入模块名。

    ``src/__init__.py`` 对应包名 ``src``（不是 ``src.__init__``——后者不可导入，
    实测导致包初始化单元永久 unverified）；其余按点号连接。
    """
    base = file_path[:-3] if file_path.endswith(".py") else file_path
    if base.endswith("/__init__"):
        base = base[: -len("/__init__")]
    return base.replace("/", ".")


def _battery_probes(file_path: str, symbol: str, root: Path) -> list[dict[str, str]]:
    """为 python 符号单元生成边界值探针（纯静态推导，无模型参与）。

    输入表刻意覆盖：None / 空串 / 零与负数 / 空容器 / 类型错位 / 极端长串。
    """
    probes: list[dict[str, str]] = []
    values = ["None", "''", "0", "-1", "[]", "{}", "'x' * 2000"]
    mod = _module_name(file_path)
    # dotted 符号（Class.method）经 reduce(getattr,...) 解析；解析失败打印
    # symbol-missing 而不是 AttributeError 噪声——那是电池的盲点不是缺陷证据。
    parts = symbol.split(".") if symbol else []
    resolve = (
        f"import importlib,functools,inspect; m=importlib.import_module({mod!r}); "
        + (f"f=functools.reduce(getattr, {parts!r}, m)" if parts else "f=None")
    )
    if symbol:
        # L6 注解感知合法入参探针：按 type hints 构造合法参数，让 typed
        # 函数拿到 ≥1 条 passed 证据（此前 amount_cents: int 被 None/'' 打爆，
        # 全 error → 单元永久 unverified，实测 billing 需人工重跑才 covered）。
        probes.append({
            "desc": f"valid-input {symbol}",
            "code": (
                "import importlib,functools,inspect,typing,types,sys\n"
                f"m=importlib.import_module({mod!r})\n"
                f"try: f=functools.reduce(getattr, {parts!r}, m)\n"
                f"except AttributeError: print('symbol-missing: {symbol}'); raise SystemExit\n"
                "_MAP={str:'x',int:1,float:1.0,bool:True,bytes:b'x'}\n"
                "_ORIGIN={list:[],dict:{},set:set(),tuple:()}\n"
                "def build(p):\n"
                "    a=p.annotation\n"
                "    if a is inspect._empty: return 'x' if p.default is inspect._empty else p.default\n"
                "    origin=typing.get_origin(a)\n"
                "    if origin in (typing.Union, types.UnionType):\n"
                "        args=[x for x in typing.get_args(a) if x is not type(None)]\n"
                "        a=args[0] if args else type(None)\n"
                "        origin=typing.get_origin(a)\n"
                "    if a in _MAP: return _MAP[a]\n"
                "    if origin in _ORIGIN: return _ORIGIN[origin]\n"
                "    if a is type(None): return None\n"
                "    raise TypeError('unresolvable annotation %r' % (a,))\n"
                "sig=inspect.signature(f)\n"
                "try:\n"
                "    args=[build(p) for p in sig.parameters.values()]\n"
                "except TypeError as e:\n"
                "    print('valid-input-skip: %s' % e); sys.exit(42)\n"
                "print(repr(f(*args))[:400])\n"
            ),
        })
        probes.append({
            "desc": f"inspect {symbol}",
            "code": (
                f"import importlib,functools,inspect\n"
                f"m=importlib.import_module({mod!r})\n"
                f"try: f=functools.reduce(getattr, {parts!r}, m)\n"
                f"except AttributeError: print('symbol-missing: {symbol}'); raise SystemExit\n"
                "print(type(f).__name__, inspect.signature(f) if callable(f) else repr(f)[:200])"
            ),
        })
        for v in values[:_MAX_PROBES_PER_UNIT - 1]:
            probes.append({
                "desc": f"{symbol}(*[{v}] * n_params)",
                "code": (
                    f"import importlib,functools,inspect\n"
                    f"m=importlib.import_module({mod!r})\n"
                    f"try: f=functools.reduce(getattr, {parts!r}, m)\n"
                    f"except AttributeError: print('symbol-missing: {symbol}'); raise SystemExit\n"
                    f"n=max(0,len(inspect.signature(f).parameters))\n"
                    f"print(repr(f(*([{v}] * n)))[:400])"
                ),
            })
    else:
        probes.append({
            "desc": f"import {file_path}",
            "code": f"import importlib; importlib.import_module({mod!r});"
                    f" print('import ok')",
        })
        probes.append({
            "desc": "module members",
            "code": f"import importlib,inspect; m=importlib.import_module({mod!r});"
                    f" print(sorted(n for n,v in vars(m).items() if not n.startswith('_'))[:40])",
        })
    return probes[:_MAX_PROBES_PER_UNIT]


_SAFE_CALLS = {
    "print", "str", "int", "float", "bool", "len", "repr", "abs", "min",
    "max", "sum", "sorted", "reversed", "enumerate", "range", "isinstance",
    "format", "round", "divmod", "zip", "map", "filter",
}
_SAFE_MODULES = {"math", "datetime", "itertools", "functools", "typing", "dataclasses"}
_RULES_FILE_NAME = "judge-rules.json"
_PROMOTE_THRESHOLD = 3
_MAX_ACTIVE_ADDITIONS = 20


def default_rules() -> dict[str, Any]:
    return {
        "safe_calls": set(_SAFE_CALLS),
        "safe_modules": set(_SAFE_MODULES),
        "artifact_markers": set(_ARTIFACT_MARKERS),
        "risk_tokens": {},
    }


def load_rules(state_root: Path) -> dict[str, Any]:
    """代码默认 < 项目规则文件（.bugx/knowledge/judge-rules.json）。

    文件是唯一动态增量层：宿主/用户可直接编辑，judge 反哺经
    harvest_feedback 写入。缺省/损坏时回退纯代码默认（fail-safe）。
    """
    rules = default_rules()
    path = Path(state_root) / "knowledge" / _RULES_FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        active = data.get("active") or {}
        for key, cast in (
            ("safe_calls", set), ("safe_modules", set),
            ("artifact_markers", set),
        ):
            extra = active.get(key) or []
            if isinstance(extra, list):
                rules[key] |= {str(x) for x in extra[:_MAX_ACTIVE_ADDITIONS]}
        tokens = active.get("risk_tokens") or {}
        if isinstance(tokens, dict):
            for token, label in list(tokens.items())[:_MAX_ACTIVE_ADDITIONS]:
                rules["risk_tokens"][str(token)] = str(label)
    except (OSError, ValueError):
        pass
    return rules


def _valid_token(value: object) -> str | None:
    text = value if isinstance(value, str) else None
    if text is None or not (4 <= len(text.strip()) <= 64):
        return None
    return text.strip()


def harvest_feedback(
    verdicts: list[Mapping[str, Any]], state_root: Path,
) -> dict[str, int]:
    """收 judge 裁决里的 rule_feedback 并按风险不对称策略落盘。

    - risk_tokens（更严方向）：校验后即时进入 active；
    - safe_calls / artifact_markers（更松方向）：进 candidates 计数，
      同 token 独立出现 ≥3 次才晋升 active（防单条错误反馈关单放走缺陷）。
    """
    path = Path(state_root) / "knowledge" / _RULES_FILE_NAME
    data: dict[str, Any] = {"schema_version": 1, "active": {
        "safe_calls": [], "safe_modules": [],
        "artifact_markers": [], "risk_tokens": {},
    }, "candidates": {}}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            data = existing
            data.setdefault("active", {"risk_tokens": {}})
            data.setdefault("candidates", {})
    except (OSError, ValueError):
        pass
    active = data["active"]
    candidates: dict[str, dict[str, Any]] = data["candidates"]
    counts = {"risk_tokens": 0, "safe_calls_new": 0,
              "artifact_markers_new": 0, "promoted": 0, "rejected": 0}
    base = default_rules()
    for verdict in verdicts or []:
        fb = verdict.get("rule_feedback") if isinstance(verdict, Mapping) else None
        if not isinstance(fb, Mapping):
            continue
        unit = str(verdict.get("unit_id") or "unknown")
        for token in (fb.get("risk_tokens") or [])[:5]:
            token = _valid_token(token)
            if token is None:
                counts["rejected"] += 1
                continue
            table = active.setdefault("risk_tokens", {})
            if token not in table and token not in base["risk_tokens"]:
                table[token] = f"judge-feedback@{unit}"
                counts["risk_tokens"] += 1
        for key, counter in (("safe_calls", "safe_calls_new"),
                             ("artifact_markers", "artifact_markers_new")):
            for token in (fb.get(key) or [])[:5]:
                token = _valid_token(token)
                if token is None or token in base[key]:
                    counts["rejected"] += 1
                    continue
                slot = candidates.setdefault(key, {})
                entry = slot.setdefault(token, {"count": 0, "sources": []})
                if unit not in entry["sources"]:
                    entry["sources"].append(unit)
                    entry["count"] += 1
                    counts[counter] += 1
                if entry["count"] >= _PROMOTE_THRESHOLD:
                    lst = active.setdefault(key, [])
                    if token not in lst and len(lst) < _MAX_ACTIVE_ADDITIONS:
                        lst.append(token)
                        counts["promoted"] += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return counts


_ARTIFACT_MARKERS = (
    "symbol-missing",
    "AttributeError: module",
    "missing 1 required positional",
    "missing required positional",
    "valid-input-skip",
    # 以下为 2026-11-04 实测挖掘会话沉淀的确定性探针伪影族（build.393 内置）：
    # 这些输出只证明「目标符号在电池上下文不可探测」，不构成任何行为证据。
    "the 'package' argument is required to perform a relative import",
    "ModuleNotFoundError: No module named",
    "positional arguments but",
    "is not a callable object",
    "no signature found for builtin type",
)


def static_profile(source: str, rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """AST 白名单静态画像：平凡单元（print("hello") 类）不需要任何模型回合。

    trivial=True 要求全部调用在安全白名单内、导入在安全模块表内、
    无风险信号——任何一个不满足都 fail-toward-judge（宁可多判读，不误关单）。
    """
    rules = rules or default_rules()
    signals: list[str] = []
    trivial = True
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"trivial": False, "signals": ["syntax-error"]}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else ""
            )
            if name not in rules["safe_calls"]:
                trivial = False
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in rules["safe_modules"]:
                    trivial = False
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or alias_root(node)).split(".")[0] if node.module else ""
            if node.level:
                trivial = False  # 相对导入涉及包结构，交给 judge
            elif root not in rules["safe_modules"]:
                trivial = False
    lowered = source.lower()
    tokens: dict[str, str] = {
        "shell=true": "shell-exec", "os.system": "shell-exec",
        "os.popen": "shell-exec", "subprocess": "shell-exec",
        "eval(": "code-exec", "exec(": "code-exec",
        "pickle.loads": "deserialization", "input(": "raw-input",
        "execute(f'": "sql-concat", "execute(f\"": "sql-concat",
        "execute(\" select": "sql-concat", "execute(' select": "sql-concat",
        "execute(\" +": "sql-concat", "execute(' +": "sql-concat",
    }
    tokens.update(rules["risk_tokens"])
    for token, label in tokens.items():
        if token in lowered:
            signals.append(label)
    # 变量中转 SQL 拼接（AST 级，堵子串盲区）：`q = f"...{kw}"`（或
    # `%`/`.format()`/+= 拼接）后 `execute(q)` —— execute 调用点看不到
    # f-string 子串，2026-11-08 实测 search_users 由此漏报 sql-concat。
    if _dynamic_sql_execute(tree):
        signals.append("sql-concat")
    if signals:
        trivial = False
    return {"trivial": trivial, "signals": sorted(set(signals))}


def _dynamic_sql_execute(tree: ast.AST) -> bool:
    """检测「f-string/格式化拼接 → 变量 → execute」的动态 SQL 构造。"""
    dynamic_vars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if _is_dynamic_text(node.value):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        dynamic_vars.add(tgt.id)
        elif isinstance(node, ast.AugAssign) and _is_dynamic_text(node.value):
            if isinstance(node.target, ast.Name):
                dynamic_vars.add(node.target.id)
    if not dynamic_vars:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        if name not in ("execute", "executemany", "executescript") or not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Name) and arg.id in dynamic_vars:
            return True
        if isinstance(arg, ast.JoinedStr):
            return True
    return False


def _is_dynamic_text(node: ast.AST) -> bool:
    """值表达式是否含运行期插值（f-string 槽位 / % 格式化 / 字符串拼接）。"""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format":
        return True
    return False


def alias_root(node: ast.Import) -> str:
    return node.names[0].name if node.names else ""


def dynamic_clean(
    evidence: list[dict[str, Any]], rules: dict[str, Any] | None = None,
) -> bool:
    """动态全过判定：非零探针必须全部可由已知伪影标记解释。"""
    if not evidence:
        return False
    markers = (rules or default_rules())["artifact_markers"]
    for ev in evidence:
        if ev.get("exit_code") == 0:
            continue
        blob = (ev.get("_stdout_excerpt") or "") + (ev.get("_stderr_excerpt") or "")
        if any(marker in blob for marker in markers):
            continue
        return False
    return True


def sentinel_fired(evidence: list[dict[str, Any]]) -> bool:
    """差分哨兵实证：任一 differential 探针输出 SENTINEL=True。"""
    return any(
        "SENTINEL=True" in (ev.get("_stdout_excerpt") or "")
        for ev in evidence
        if "differential-sentinel" in ev.get("command", "")
    )


def classify_unit(
    source: str, evidence: list[dict[str, Any]],
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """机器初审分类器（DOC-257 pre-screen）。

    - auto_clean：静态平凡 + 动态全过 → collect 内直接 checkpoint，零模型回合
    - judge + machine_flag：SENTINEL 实证 → judge L1 秒确认并写 finding 文案
    - judge：其余
    """
    rules = rules or default_rules()
    profile = static_profile(source, rules)
    fired = sentinel_fired(evidence)
    # artifact-only：全部探针输出都可由已知伪影标记解释 ⇒ 单元在电池上下文
    # 不可探测，行为证据为零信息量——送 judge 是纯浪费（受控验证显示
    # 中 5 个）。标记集合含内置确定性伪影族与项目反哺晋升的标记。
    markers = (rules or default_rules())["artifact_markers"]
    artifact_only = bool(evidence) and all(
        any(marker in ((ev.get("_stdout_excerpt") or "") + (ev.get("_stderr_excerpt") or ""))
            for marker in markers)
        for ev in evidence
    )
    if artifact_only or (profile["trivial"] and dynamic_clean(evidence, rules)):
        return {"route": "auto_clean", "signals": profile["signals"],
                "sentinel": fired, "artifact_only": artifact_only}
    if fired:
        return {"route": "judge", "machine_flag": "sentinel-confirmed", "signals": profile["signals"], "sentinel": True}
    return {"route": "judge", "signals": profile["signals"], "sentinel": False}


# 差分哨兵触发语料（build.395 扩充）：命令/代码执行族之外补齐
# 反序列化族（pickle/yaml.load/marshal）与进程替换族（execv/getoutput/
# pty.spawn）。项目可经 judge 反哺 risk_tokens 或人工编辑 judge-rules.json
# 追加（即时生效），无需等引擎发版。
_EXEC_SIGNALS = (
    "shell=True", "subprocess", "os.system", "os.popen", "eval(", "exec(",
    "os.execv", "commands.getoutput", "subprocess.getoutput",
    "subprocess.getstatusoutput", "pty.spawn",
    "pickle.loads", "yaml.load(", "marshal.loads",
)


def has_exec_signal(
    source: str, dep_sources: list[str] | None = None,
    rules: dict[str, Any] | None = None,
) -> bool:
    """单元或其依赖切片出现命令/代码执行信号时才启用差分哨兵探针。

    采集层反哺（DOC-257 续）：除硬编码信号外，judge 反哺的 risk_tokens
    也参与判定——新注入形态（os.execv、commands.getoutput 等）一经反哺，
    下一批取证即触发差分哨兵，而不只是影响静态画像。
    """
    corpus = source + "\n" + "\n".join(dep_sources or [])
    if any(sig in corpus for sig in _EXEC_SIGNALS):
        return True
    extra = (rules or {}).get("risk_tokens") or {}
    return any(token and token in corpus for token in extra)


def risk_grade(
    source: str, dep_sources: list[str] | None = None,
    rules: dict[str, Any] | None = None,
) -> str:
    """三级风险分级（单一真相源，供 collect 分组/AI 兜底/排序共用）。

    - ``dangerous``：命中风险 token 标签（shell-exec/code-exec/deserialization/
      raw-input/sql-concat，含项目反哺）或执行信号语料（含反哺）——AI 兜底必审级；
    - ``unknown``：非平凡（存在白名单外调用/导入）但无危险信号——可机器关单；
    - ``trivial``：静态平凡——机器直关。

    反哺 risk_tokens 同时进入 static_profile 标签表与执行语料，因此一条
    反哺即刻把同形态单元从 unknown/dangerous 升级，无需改引擎。
    """
    profile = static_profile(source, rules)
    if profile["signals"] or has_exec_signal(source, dep_sources=dep_sources, rules=rules):
        return "dangerous"
    return "trivial" if profile["trivial"] else "unknown"


def _differential_probes(file_path: str, symbol: str) -> list[dict[str, str]]:
    """哨兵差分探针：一条脚本内完成 基线→载荷→哨兵检查 的完整对照实验。

    输出结构化行 BASE_RC / PAYLOAD_RC / SENTINEL，宿主确定性执行；
    judge 拿到的是已完成的对照结论，不再是"rc!=0 无法区分"的歧义数据。
    载荷仅 touch 哨兵文件（无破坏性）；SENTINEL=True 即注入实证。
    """
    if not symbol:
        return []
    mod = file_path[:-3].replace("/", ".")
    parts = symbol.split(".")
    payloads = [
        ("semicolon", "'x; touch ' + SENTINEL"),
        ("command-sub", "'x$(touch ' + SENTINEL + ')'"),
        ("sql-union", "'x' + chr(39) + ' UNION SELECT ' + chr(39) + 'SENTINEL' "
                      "+ chr(39) + ',' + chr(39) + 'SENTINEL' + chr(39) + ' -- '"),
    ]
    probes = []
    for name, payload_expr in payloads:
        probes.append({
            "desc": f"differential-sentinel[{name}] {symbol}",
            "code": (
                "import importlib,functools,inspect,os,tempfile\n"
                f"m=importlib.import_module({mod!r})\n"
                f"try: f=functools.reduce(getattr, {parts!r}, m)\n"
                f"except AttributeError: print('symbol-missing: {symbol}'); raise SystemExit\n"
                "SENTINEL=os.path.join(tempfile.mkdtemp(prefix='bugx-diff-'),'sentinel')\n"
                "n=max(0,len(inspect.signature(f).parameters))\n"
                "def rc(v):\n"
                "    try: return f(*([v]*n)), None\n"
                "    except BaseException as e: return None, type(e).__name__\n"
                "b=rc('x'); print('BASE_RET=%r BASE_EXC=%s' % (b[0], b[1]))\n"
                f"pl=rc({payload_expr}); print('PAYLOAD_RET=%r PAYLOAD_EXC=%s' % (pl[0], pl[1]))\n"
                "hit=os.path.exists(SENTINEL) or ('SENTINEL' in str(pl[0]))\n"
                "print('SENTINEL=' + ('True' if hit else 'False'))\n"
            ),
        })
    return probes


def run_battery(
    root: Path,
    unit_id: str,
    artifact_root: Path,
    lease_id: str,
    execution_id: str,
    worker_id: str,
    run_id: str,
    target_fingerprint: str,
    round_no: int = 1,
    rules: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """执行边界电池并把每个探针落成带收据的 artifact（与 finalize 兼容）。

    返回 evidence 条目数组（status=passed 表示探针本身执行成功——含抛出
    异常的探针，异常正是判读数据；探针进程崩溃/超时才记 error）。
    """
    root = Path(root).resolve()
    artifact_root = (root / artifact_root).resolve() if not Path(artifact_root).is_absolute() else Path(artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    file_path, symbol = unit_parts(unit_id)
    target = root / file_path
    if not target.is_file():
        return []
    try:
        unit_source = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        unit_source = ""
    all_probes = _battery_probes(file_path, symbol, root)
    rules = load_rules(root / ".bugx") if rules is None else rules
    # L2：执行信号语料 = 本单元源码 + 依赖切片——被调方有 shell 时，
    # 调用方单元也应有差分探针（此前只看本文件导致哨兵漏触发）。
    try:
        dep_sources = [d["source"] for d in dependency_slices(root, file_path, unit_source)]
    except Exception:
        dep_sources = []
    if not file_path.endswith(".py"):
        # 非 Python 资产（yaml/json/md/toml…）：文本完整性探针——能解析即为
        # passed 证据，否则该单元会因全是 import 伪影而永久 unverified。
        suffix = file_path.rsplit(".", 1)[-1].lower()
        parser = {"yaml": "yaml.safe_load", "yml": "yaml.safe_load",
                  "json": "json.loads"}.get(suffix)
        code = f"import pathlib\nraw = pathlib.Path({file_path!r}).read_text(encoding='utf-8', errors='ignore')\nassert raw.strip(), 'empty asset'\nprint('asset_bytes=%d' % len(raw))"
        if parser:
            body = ("import importlib\n"
                    f"parser = lambda t: __import__({{{suffix!r}: 'json'}}.get({suffix!r}, 'yaml')).{'loads' if suffix=='json' else 'safe_load'}(t)\n")
            code = (f"import pathlib\nraw = pathlib.Path({file_path!r}).read_text(encoding='utf-8', errors='ignore')\nassert raw.strip(), 'empty asset'\n"
                    + ("import json\nimport json as _p\nprint('parsed_items=%d' % len(json.loads(raw)))\n" if suffix == "json" else
                       "import yaml\ndata = yaml.safe_load(raw)\nassert data is not None, 'unparsable asset'\nprint('parsed_type=%s' % type(data).__name__)\n"))
        all_probes = all_probes + [{"desc": f"asset-integrity[{suffix or 'text'}] {file_path}", "code": code}]
    if has_exec_signal(unit_source, dep_sources=dep_sources, rules=rules):
        all_probes = all_probes + _differential_probes(file_path, symbol)
    evidence: list[dict[str, Any]] = []
    # 并发修复：探针彼此独立（各起子进程、各写自己的 artifact），串行
    # 8 探针/单元会让大项目 collect 线性膨胀（实测 15 单元 12-14s）。
    # 用线程池并行执行、按 probe 顺序回填，行为与串行逐字节一致。
    def _execute(index_probe: tuple[int, dict[str, str]]) -> tuple[int, dict[str, Any]]:
        index, probe = index_probe
        argv = [sys.executable, "-c", probe["code"]]
        started = time.time()
        try:
            proc = subprocess.run(
                argv, cwd=str(root), capture_output=True, text=True,
                timeout=_PROBE_TIMEOUT_SECONDS, check=False,
            )
            exit_code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
            status = "passed" if proc.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            exit_code, out, err, status = 124, "", "probe timeout", "error"
        record = {
            "schema_version": 1,
            "argv": argv,
            "command": " ".join(argv[:2]) + f" -c <probe:{probe['desc']}>",
            "desc": probe["desc"],
            "exit_code": exit_code,
            "stdout": out[:2000],
            "stderr": err[-2000:],
            "duration_ms": round((time.time() - started) * 1000, 2),
            "execution_id": execution_id,
            "lease_id": lease_id,
            "worker_id": worker_id,
            "run_id": run_id,
            "round_no": round_no,
            "unit_id": unit_id,
            "status": "success" if status == "passed" else "error",
            "timestamp": time.time(),
            "target_fingerprint": target_fingerprint,
        }
        name = f"probe-{hashlib.sha256(probe['desc'].encode()).hexdigest()[:32]}.json"
        path = artifact_root / name
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
        rel = path.relative_to(root).as_posix()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return index, {
            "_unit_id": unit_id,
            "command": record["command"],
            "exit_code": exit_code,
            "artifact_path": rel,
            "artifact_sha256": sha,
            "target_fingerprint": target_fingerprint,
            "timestamp": record["timestamp"],
            "worker_id": worker_id,
            "run_id": run_id,
            "round_no": round_no,
            "lease_id": lease_id,
            "status": status,
            "execution_receipt": {
                "schema_version": 1,
                "execution_id": execution_id,
                "worker_id": worker_id,
                "lease_id": lease_id,
                "command": record["command"],
                "exit_code": exit_code,
                "artifact_path": rel,
                "artifact_sha256": sha,
                "status": status,
            },
            "_stdout_excerpt": out[:_MAX_OUTPUT_CHARS],
            "_stderr_excerpt": err[-_MAX_OUTPUT_CHARS:],
        }

    indexed = list(enumerate(all_probes))
    max_workers = min(8, max(1, len(indexed)))
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for _index, entry in sorted(pool.map(_execute, indexed)):
                evidence.append(entry)
    except Exception:
        # 线程池异常时回退串行，保证取证不中断
        for pair in indexed:
            _index, entry = _execute(pair)
            evidence.append(entry)
    return evidence


# ---------------------------------------------------------------------------
# 判读包（judge package）：给 AI 的唯一输入
# ---------------------------------------------------------------------------

def dependency_slices(root: Path, file_path: str, source: str) -> list[dict[str, str]]:
    """AST 解析 from-import，附上被引用符号的源码切片（确定性，零模型回合）。

    跨单元归因断链的修复：PoC/调用方单元的缺陷实体在被调方文件里，
    judge 只看本单元切片时无法归因（实测：poc_cmdi 的命令注入在
    ops.ping_service，judge 正确拒判 insufficient_data）。本函数把项目内
    直接依赖的符号切片附进判读包，上限 2 个文件 / 共 4000 字符。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    slices: list[dict[str, str]] = []
    budget = 4000
    for node in ast.walk(tree):
        if len(slices) >= 2 or budget <= 0:
            break
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        dep_path = node.module.replace(".", "/") + ".py"
        dep_file = Path(root) / dep_path
        if not dep_file.is_file():
            # 同目录相对导入（from . import x / from .mod import x）
            if node.level:
                dep_path = str(Path(file_path).parent / node.module) + ".py"
                dep_file = Path(root) / dep_path
            if not dep_file.is_file():
                continue
        try:
            dep_source = dep_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for alias in node.names:
            if len(slices) >= 4 or budget <= 0:
                break
            name = alias.name if alias.name != "*" else ""
            piece = symbol_source(dep_source, name)[:budget] if name else dep_source[:budget]
            if not piece.strip():
                continue
            slices.append({"file": dep_path, "symbol": name, "source": piece[:budget]})
            budget -= min(len(piece), budget)
    return slices


def build_package(
    unit_id: str,
    root: Path,
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """把代码切片 + 依赖切片 + 电池实测压成一个自包含判读包（token 有界）。"""
    file_path, symbol = unit_parts(unit_id)
    target = Path(root) / file_path
    if not target.is_file():
        return None
    try:
        source = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    sliced = symbol_source(source, symbol)[:_MAX_SOURCE_CHARS]
    deps = dependency_slices(root, file_path, source)
    observations = []
    for ev in evidence:
        observations.append({
            "probe": ev.get("command", ""),
            "exit_code": ev.get("exit_code"),
            "stdout": ev.get("_stdout_excerpt") or "",
            "stderr": ev.get("_stderr_excerpt") or "",
        })
    return {
        "schema_version": PACKAGE_VERSION,
        "unit_id": unit_id,
        "file": file_path,
        "symbol": symbol,
        "source": sliced,
        "dependencies": deps,
        "observations": observations,
    }


def package_prompt(package: Mapping[str, Any]) -> str:
    """judge 子代理的完整提示词：数据进、裁决出，一个回合。"""
    payload = json.dumps(package, ensure_ascii=False)
    return (
        "你是 BugX 判读员（judge）。下面是一个覆盖单元的判读包：源码切片、"
        "依赖切片与边界电池的实测输出。你的唯一职责是【判断这份数据】——"
        "不运行命令、不读文件、不碰任何协议，直接给出裁决 JSON。\n\n"
        "裁决优先级阶梯（按此顺序检查，命中即止，禁止跳级展开）：\n"
        "L1 结构化行定案：differential 探针输出 SENTINEL=True ⇒ bug（注入实证，"
        "无需其他论证）；SENTINEL=False 且 BASE/PAYLOAD 行为一致 ⇒ 该面 clean。\n"
        "L2 契约对照定案：全部探针 exit_code=0 且返回值符合 docstring/参数名契约"
        "⇒ clean。\n"
        "L3 异常归因：崩溃/数据损坏/语义违背 ⇒ bug；异常可由探针伪影解释 ⇒ clean。\n"
        "L4 以上都不成立 ⇒ insufficient_data（注明缺哪条证据）。\n"
        "探针伪影（不是缺陷证据）：解绑调用 Class 方法导致的 AttributeError；"
        "symbol-missing；无参函数被边界值等价重复调用产生的一串相同结果。\n"
        "自证文本规则：PoC 源码里硬编码的 'VULNERABILITY CONFIRMED' 类字样"
        "不算独立证据，必须引用实测输出本身。\n"
        "确定性要求：唯一依据是本判读包；同一判读包重复判读必须得到同一裁决。\n"
        "可选反哺：顶层可加 rule_feedback 对象（各≤5项，字符串4-64字符）："
        "risk_tokens=[新风险token...]（更严，即时生效）；"
        "safe_calls=[可安全直关的调用名...]、artifact_markers=[新探针伪影标记...]（更松，"
        "独立出现≥3次才生效）。只在有把握时提供。\n"
        "findings 的 reproduction 必须是一条可直接执行的 python3 单行命令"
        "（可基于 observations 中已验证的探针改写），不得拼接说明文字；"
        "证据行号写源码切片中的真实行号。\n"
        "若判读包含 unit_group 数组，表示该文件的全部这些单元共用本包"
        "证据：verdict 适用于整组，输出 unit_id 用组内主要单元即可"
        "（宿主会按组落账）。\n"
        "若判据不足以定论（verdict=insufficient_data）且缺的是【可机器执行】的"
        "验证，可附 \"probe_requests\"（≤3 条）描述要跑什么："
        "每条 {\"desc\":\"...\",\"code\":\"...\"}，code 为不含 shell 字符的 "
        "python 代码（用换行分隔语句、chr() 构造载荷）；宿主会确定性执行并把"
        "结果作为增广证据回给你复审一次（至多一跳）。本包若已带 probe_round 标记，"
        "禁止再提 probe_requests，必须给出终态裁决。\n"
        "若判 bug，请把两条 assert 型验证命令放进对应 finding 内部"
        "（verification_positive/verification_negative 字段，供宿主引擎双跑实证）。"
        "宿主会自动包装 python3 -c，因此只给【纯代码体】，禁止带 python3 -c 外壳："
        "\"verification_positive\"=漏洞存在时先 print 一行失败观测"
        "（如 print('BUGX_POSITIVE_FAILURE: marker')）再以恰为 1 的退出码退出"
        "（raise SystemExit(1)）的 python 代码（例如断言注入产生的副作用/返回行数）；"
        "\"verification_negative\"=安全输入下以 0 退出（raise SystemExit(0)）的一条代码。"
        "负对照的语义：证明探针本身可靠（普通输入走正常路径不受影响）——"
        "它必须能在【当前带缺陷的代码】上通过，禁止假设修复后的行为或调用"
        "不存在的机制/环境变量/参数（实测坑：为不存在的 env 覆盖写 reload "
        "断言导致负对照必败、整批确认被拒）。"
        "两条代码均不得含 shell 字符（; && || ` $( > <），"
        "请用换行分隔语句、chr() 构造载荷。\n\n"
        "只输出如下 JSON（无其他文字）。JSON 字符串内禁止裸换行等控制字符，"
        "必须用 \\n 转义（宿主按严格 JSON 解析）：\n"
        '{"unit_id":"<原样返回>","verdict":"clean|bug|insufficient_data",'
        '"findings":[{"fingerprint":"<file::symbol::短slug>","title":"...",'
        '"severity":"low|medium|high","root_cause":"...",'
        '"evidence":[{"path":"<file>","line":<int>}],'
        '"reproduction":"python3 -c \'...\'"}],'
        '"notes":"<=200字"}\n\n'
        f"判读包（dependencies 是本单元直接调用的项目内符号切片，归因时可作依据）：\n{payload}"
    )


# ---------------------------------------------------------------------------
# 裁决收口：verdict → checkpoint（含复现命令的确定性实跑取证）
# ---------------------------------------------------------------------------

def run_battery_probe(
    root: Path,
    unit_id: str,
    artifact_root: Path,
    lease_id: str,
    execution_id: str,
    worker_id: str,
    run_id: str,
    target_fingerprint: str,
    code: str,
    round_no: int = 1,
    desc: str = "custom",
) -> dict[str, Any] | None:
    """执行单条自定义探针并落成带收据的 evidence（业务流冒烟等复用）。"""
    import subprocess
    import time as _t
    root = Path(root).resolve()
    artifact_root = (root / artifact_root).resolve() if not Path(artifact_root).is_absolute() else Path(artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-c", code]
    started = _t.time()
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT_SECONDS, check=False)
        exit_code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return None
    record = {
        "schema_version": 1, "argv": argv, "command": f"{sys.executable} -c <probe:{desc}>",
        "desc": desc, "exit_code": exit_code, "stdout": out[:2000],
        "stderr": err[-2000:], "duration_ms": round((_t.time() - started) * 1000, 2),
        "execution_id": execution_id, "lease_id": lease_id, "worker_id": worker_id,
        "run_id": run_id, "round_no": round_no, "unit_id": unit_id,
        "status": "success" if exit_code == 0 else "error",
        "timestamp": _t.time(), "target_fingerprint": target_fingerprint,
    }
    name = f"probe-{hashlib.sha256((unit_id + desc).encode()).hexdigest()[:32]}.json"
    path = artifact_root / name
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)
    rel = path.relative_to(root).as_posix()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    status = "passed" if exit_code == 0 else "error"
    return {
        "_unit_id": unit_id,
        "command": record["command"], "exit_code": exit_code,
        "artifact_path": rel, "artifact_sha256": sha,
        "target_fingerprint": target_fingerprint, "timestamp": record["timestamp"],
        "worker_id": worker_id, "run_id": run_id, "round_no": round_no,
        "lease_id": lease_id, "status": status,
        "execution_receipt": {
            "schema_version": 1, "execution_id": execution_id, "worker_id": worker_id,
            "lease_id": lease_id, "command": record["command"], "exit_code": exit_code,
            "artifact_path": rel, "artifact_sha256": sha, "status": status,
        },
        "_stdout_excerpt": out[:_MAX_OUTPUT_CHARS],
        "_stderr_excerpt": err[-_MAX_OUTPUT_CHARS:],
    }


_PROBE_REQUEST_SHELL_TOKENS = (";", "&&", "||", "`", "$(", ">", "<")


def validate_probe_requests(requests: object) -> list[dict[str, str]]:
    """校验 judge 的 probe_requests：纯 python 代码、禁 shell 字符、有界数量。"""
    if not isinstance(requests, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for item in list(requests)[:3]:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        desc = str(item.get("desc") or "probe")[:120]
        if not code or any(tok in code for tok in _PROBE_REQUEST_SHELL_TOKENS):
            continue
        out.append({"desc": desc, "code": code})
    return out


def run_probe_requests(
    root: Path,
    unit_id: str,
    artifact_root: Path,
    lease_id: str,
    execution_id: str,
    worker_id: str,
    run_id: str,
    target_fingerprint: str,
    requests: object,
    round_no: int = 1,
) -> list[dict[str, Any]]:
    """确定性执行 judge 指定的定向探针，产出带收据的 evidence。

    judge 只描述"要跑什么"；执行、sha256、receipt 全在宿主完成，
    结果作为增广证据回给 judge 复审（至多一跳）。
    """
    evidence: list[dict[str, Any]] = []
    for req in validate_probe_requests(requests):
        ev = run_battery_probe(
            root, unit_id, artifact_root, lease_id, execution_id,
            worker_id, run_id, target_fingerprint,
            req["code"], round_no=round_no, desc=f"judge-req {req['desc']}",
        )
        if ev is not None:
            evidence.append(ev)
    return evidence


def execute_reproduction(
    root: Path,
    command: str,
    artifact_root: Path,
    lease_id: str,
    execution_id: str,
    worker_id: str,
    run_id: str,
    target_fingerprint: str,
    round_no: int = 1,
) -> dict[str, Any] | None:
    """宿主确定性实跑 judge 给出的复现命令，产出真实收据。

    这是判读式路径的反伪造锚点：finding 的复现不是「judge 说能跑」，
    而是宿主真跑出的 exit_code + artifact。命令必须以 python3 开头，
    其余一律拒绝（防注入）。
    """
    command = str(command).strip()
    if not command.startswith("python3 ") and not command.startswith("python3\t"):
        return None
    root = Path(root).resolve()
    artifact_root = (root / artifact_root).resolve() if not Path(artifact_root).is_absolute() else Path(artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    argv = command.split(" ", 1)
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, argv[1]] if argv[0] == "python3" else argv,
            cwd=str(root), capture_output=True, text=True,
            timeout=_PROBE_TIMEOUT_SECONDS, check=False,
        )
        exit_code, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return None
    record = {
        "schema_version": 1, "argv": [sys.executable, argv[1]] if len(argv) > 1 else [sys.executable],
        "command": command, "exit_code": exit_code,
        "stdout": out[:2000], "stderr": err[-2000:],
        "duration_ms": round((time.time() - started) * 1000, 2),
        "execution_id": execution_id, "lease_id": lease_id,
        "worker_id": worker_id, "run_id": run_id, "round_no": round_no,
        "status": "success" if exit_code == 0 else "error",
        "timestamp": time.time(), "target_fingerprint": target_fingerprint,
    }
    name = f"probe-{uuid.uuid4().hex}.json"
    path = artifact_root / name
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)
    rel = path.relative_to(root).as_posix()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    status = "passed" if exit_code == 0 else "failed"
    return {
        "command": command, "exit_code": exit_code,
        "artifact_path": rel, "artifact_sha256": sha,
        "target_fingerprint": target_fingerprint,
        "timestamp": record["timestamp"], "worker_id": worker_id,
        "run_id": run_id, "round_no": round_no, "lease_id": lease_id,
        "status": status,
        "execution_receipt": {
            "schema_version": 1, "execution_id": execution_id,
            "worker_id": worker_id, "lease_id": lease_id,
            "command": command, "exit_code": exit_code,
            "artifact_path": rel, "artifact_sha256": sha, "status": status,
        },
        "_stdout_excerpt": out[:_MAX_OUTPUT_CHARS],
        "_stderr_excerpt": err[-_MAX_OUTPUT_CHARS:],
    }


def parse_verdict(text: str, unit_id: str) -> dict[str, Any]:
    """从 judge 的回复文本解析裁决 JSON（宽容前后噪声，fail-closed 默认）。"""
    text = str(text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {"unit_id": unit_id, "verdict": "insufficient_data", "findings": [],
                "notes": "judge output contained no JSON"}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        # judge 自写裁决常见裸控制字符（字符串里嵌字面换行）——宽容复解析，
        # 避免"judge output JSON unparsable"静默降级吞掉真实裁决。
        try:
            data = json.loads(text[start:end + 1], strict=False)
        except ValueError:
            return {"unit_id": unit_id, "verdict": "insufficient_data", "findings": [],
                    "notes": "judge output JSON unparsable"}
    verdict = str(data.get("verdict") or "insufficient_data")
    if verdict not in _VERDICT_STATUSES:
        verdict = "insufficient_data"
    data["verdict"] = verdict
    data["unit_id"] = unit_id
    data.setdefault("findings", [])
    data.setdefault("notes", "")
    return data


def checkpoint_payload_from_verdict(
    claim: Mapping[str, Any],
    verdict: Mapping[str, Any],
    battery_evidence: list[dict[str, Any]],
    reproduction_evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """把裁决映射成 supervisor checkpoint payload（机器构造，无手搓）。

    - clean/insufficient_data → 全单元 covered（电池 evidence 即结构化证据）
    - bug → 单元 covered，findings 附带，且每条 finding 的复现收据
      （宿主实跑）优先作为该单元 evidence；无复现收据时回退电池 evidence，
      finding 标记 reproducible=false。
    """
    unit_ids = list(claim.get("unit_ids") or [])
    if not unit_ids:
        return None
    findings: list[dict[str, Any]] = []
    verify_hints = verdict.get("verification") if isinstance(
        verdict.get("verification"), Mapping) else {}
    for item in (verdict.get("findings") or [])[:5]:
        if not isinstance(item, Mapping):
            continue
        finding = {
            "fingerprint": str(item.get("fingerprint") or "")[:256] or None,
            "title": str(item.get("title") or "")[:2000],
            "severity": str(item.get("severity") or "low"),
            "root_cause": str(item.get("root_cause") or "")[:4000],
            "evidence": [dict(e) for e in (item.get("evidence") or []) if isinstance(e, Mapping)][:5],
            "reproducible": bool(reproduction_evidence),
            # PE-20261109-002：finding 必须携带来源单元链接。判读包契约是
            # "verdict 适用于整组"——poc 型证据的 covered 单元在结算校验
            # （checkpoint_sync/post_coverage_sync）里靠 unit_id/target_units
            # 找关联 finding；缺失时整轮 post 被 fail-closed 拒绝（实测
            # 变体归并后可能导致结算死锁）。
            "target_units": list(unit_ids),
        }
        if item.get("reproduction"):
            finding["reproduction"] = str(item["reproduction"])[:4000]
        pos = verify_hints.get("verification_positive") or item.get("verification_positive")
        neg = verify_hints.get("verification_negative") or item.get("verification_negative")
        if pos and neg:
            finding["verification_hints"] = {
                "positive": str(pos)[:4000], "negative": str(neg)[:4000],
            }
        if finding["fingerprint"] and finding["title"]:
            findings.append(finding)
    evidence: dict[str, dict[str, Any]] = {}
    primary_unit = str(claim.get("primary_unit") or (unit_ids[0] if unit_ids else ""))
    for unit in unit_ids:
        # 每单元独立 artifact：优先本单元自己的 passed 探针；复现收据只能
        # 给主单元（单文件单 artifact，不能跨单元复用——validator 硬约束）。
        own = [
            e for e in battery_evidence
            if e.get("_unit_id") == unit
            and e.get("status") == "passed" and e.get("exit_code") == 0
        ]
        chosen = None
        if unit == primary_unit and reproduction_evidence is not None:
            chosen = reproduction_evidence
        elif own:
            chosen = own[0]
        elif len(unit_ids) == 1:
            chosen = reproduction_evidence or next(
                (e for e in battery_evidence if e.get("status") == "passed"), None)
        if chosen is None:
            continue
        entry = {k: v for k, v in chosen.items() if not k.startswith("_")}
        # 复现命令按设计失败（exit!=0）时声明 reproduction_poc，并挂接 finding
        if (entry.get("exit_code") != 0
                and chosen is reproduction_evidence
                and findings):
            entry["evidence_type"] = "reproduction_poc"
            for finding in findings:
                finding.setdefault("unit_id", unit)
        evidence[unit] = entry
    if len(evidence) < len(unit_ids) or str(verdict.get("verdict")) == "insufficient_data":
        # insufficient_data 是 judge 的明确拒判：单元只能记 unverified，
        # 不得以电池探针通过冒充已覆盖（实测踩过：poc_cmdi 判据不足被记 covered）。
        # 例外：artifact-only（全部探针均为已知伪影，单元在电池上下文不可探测）
        # 记 not_applicable 终态——记 unverified 会被 pending 过滤器永久重领，
        # 每轮白烧一遍电池（实测 2026-11-04 会话）。
        if bool(verdict.get("artifact_only")):
            coverage = {unit: "not_applicable" for unit in unit_ids}
        else:
            coverage = {unit: "unverified" for unit in unit_ids}
        evidence = {}
    else:
        coverage = {unit: "covered" for unit in unit_ids}
    tokens = verdict.get("tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
        tokens = None
    if tokens:
        # judge 判读员的 token 消耗由宿主写进 verdict["tokens"]（来自 Agent
        # 结果的 subagent_tokens）；checkpoint_sync 从 evidence 条目的
        # tokens_consumed 汇总进 claim 账本——只记主单元一次，多单元组重复
        # 记账会被 summation 放大。无 evidence 条目（artifact-only /
        # insufficient_data）时无处挂账，诚实丢弃。
        primary = next(iter(evidence), None)
        if primary is not None:
            evidence[primary]["tokens_consumed"] = int(tokens)
    return {
        "checkpoint_id": f"judge-{claim.get('worker_id')}-{uuid.uuid4().hex[:8]}",
        "coverage": coverage,
        "evidence": evidence,
        "findings": findings,
        "life_delta": 0,
    }


__all__ = [
    "unit_parts", "symbol_source", "run_battery", "build_package",
    "package_prompt", "parse_verdict", "execute_reproduction",
    "checkpoint_payload_from_verdict",
]
