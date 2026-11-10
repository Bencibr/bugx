#!/usr/bin/env python3
"""core/context_assembler.py — 定向单元上下文装配（DOC-141 调度算法升级）。

为什么要替代"通用先验包全量渲染"
--------------------------------
``worker_orchestrator._build_unit_prior`` 每单元要做四趟全仓级遍历
（mining explore / behavior graph / pattern probe / sast），再把结果**全量
渲染**进提示词——实测约 1,100 字符/单元，其中大部分与被测单元无关：
token 花了，信息密度低，且在固定 token 预算下直接压缩了可覆盖单元数。

本模块按"少而准"重建每单元上下文，只保留四类高密度信息：

  1. **符号摘要**：AST 提取该单元自身的顶层定义与调用属性。挖掘的是这个
     文件，它的函数名/类名/被调属性就是缺陷特征的最相关词汇——远比全仓
     泛化数据有信息量；
  2. **攻击面标签**：命中 ``task_scheduler._SENSITIVE_SURFACES`` 即标注
     （auth/api/upload/route…），指引验证深度；
  3. **规则 ID 列表**：规则语料的文件名 ID（**只给 ID 不复制正文**，
     worker 按需自读，正文动辄数百字符）；
  4. **紧凑先验 digest**（默认关闭）：实测它占上下文装配 ~100% 的耗时
     与 ~89% 的字符，却**不携带任何类别线索**——先验包仍落盘于
      ``.bugx/state/priors/`` 供按需自读，默认不进提示词；确有需要的
     调用方可显式 ``include_prior_digest=True``。

输出典型 200–360 字符，信息密度高于原 1,100 字符的通用包；同一 token
预算下可覆盖更多单元（准确度 ↑），每单元 token 成本 ↓。

设计约束：本模块只做**装配**，不改变任何覆盖真相与门禁语义；装配失败
一律降级为空上下文（绝不阻断派发）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

try:
    import core.bugx_paths as bugx_paths
except Exception:  # pragma: no cover - 运行时缺失时退化为纯路径推导
    bugx_paths = None  # type: ignore[assignment]

# 符号摘要上限：再多对密度的边际收益急剧下降，而 token 线性增长。
DEFAULT_MAX_SYMBOLS = 14
DEFAULT_MAX_CHARS = 360
# A4：按验证深度分配符号预算。deep 单元要完整符号面（复现+反事实需要），
# triage 只做初筛——5 个符号足以判断"值不值得升级"，多给就是纯 token。
TIER_SYMBOL_BUDGET = {"deep": 14, "standard": 8, "triage": 5}
# 规则语料目录（相对 core/）：只取文件名作 ID。
RULE_CORPUS_RELATIVE = "prompts/rules"


def _core_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root).resolve()
    if bugx_paths is not None:
        try:
            return Path(bugx_paths.require(Path.cwd()).agent_dir)
        except Exception:
            pass
    return Path(__file__).resolve().parents[1]


def _unit_path(unit: str, root: Path) -> Path | None:
    """把 unit id（可能带 ::symbol 后缀）解析到真实文件。"""
    rel = str(unit).split("::", 1)[0].replace("\\", "/").lstrip("./")
    if not rel:
        return None
    candidate = root / rel
    if candidate.is_file():
        return candidate
    return None


def extract_symbols(path: Path, limit: int = DEFAULT_MAX_SYMBOLS) -> list[str]:
    """提取单元的高密度符号。

    覆盖五类（按信息密度排序，确定性顺序截断到 limit）：
      1. 顶层定义与类方法（缺陷大量存在于方法体内）；
      2. 被调用属性/函数名（行为线索）；
      3. **imports**（`sqlite3`/`threading` 直接指向注入/竞态类缺陷）；
      4. **基类**（继承关系暗示契约违例面）；
      5. **字符串常量样本**（错误信息/SQL 片段/配置键名）。
    解析失败返回空列表——上下文装配绝不禁派发。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return []
    symbols: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        cleaned = str(name).strip()
        if not cleaned or cleaned in seen or cleaned.startswith("_"):
            return
        if len(cleaned) > 48:
            return
        seen.add(cleaned)
        symbols.append(cleaned)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _add(node.name)
        if isinstance(node, ast.ClassDef):
            # 类方法同样是高密度符号（缺陷大量存在于方法体内）。
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _add(child.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            _add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            _add(node.func.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                _add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            _add(node.module.split(".")[0])
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    _add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # 字符串常量：只取有信息量的词片段（错误信息/SQL/配置键）。
            for word in node.value.replace("(", " ").replace(")", " ").split():
                if len(word) > 3 and word.isascii():
                    _add(word)
    return symbols[:limit]


def surface_labels(unit: str) -> list[str]:
    """命中 _SENSITIVE_SURFACES 的攻击面标签（供验证深度与上下文标注）。"""
    try:
        from core.supervisor import task_scheduler
        surfaces = getattr(task_scheduler, "_SENSITIVE_SURFACES", {}) or {}
    except Exception:
        surfaces = {}
    normalized = str(unit).casefold().replace("\\", "/")
    return sorted(
        key for key in surfaces if str(key).casefold() in normalized
    )


def rule_ids(engine_dir: Path | None = None) -> list[str]:
    """规则语料的 ID 列表（文件名去扩展名；不存在返回空）。

    规则语料属于**引擎侧**（``~/.bugx/core/prompts/rules``），不属于被审
    项目——因此默认取本模块所在目录（即 core/），``engine_dir`` 仅供测试
    注入。原实现误把项目根当 core 目录，导致真实链路上永远取不到规则 ID。
    """
    core_root = Path(engine_dir) if engine_dir is not None else Path(__file__).resolve().parents[1]
    corpus = core_root / RULE_CORPUS_RELATIVE
    if not corpus.is_dir():
        return []
    return sorted(path.stem for path in corpus.glob("*.md"))


def prior_digest(unit: str, root: Path | None = None) -> str:
    """紧凑先验 digest：仅条目来源与计数，不渲染条目体。"""
    try:
        from core.launch_phases import worker_orchestrator as wo
        payload = wo._build_unit_prior(unit, root=root)
    except Exception:
        return ""
    parts: list[str] = []
    for group in payload.get("items", []):
        if not isinstance(group, dict):
            continue
        source = str(group.get("source", ""))
        status = str(group.get("status", ""))
        count = len(group.get("items") or [])
        if count:
            parts.append(f"{source}:{count}({status})")
        else:
            parts.append(f"{source}:{status}")
    return ",".join(parts)


def symbols_for_tier(unit: str, tier: str | None = None) -> int:
    """该单元验证深度对应的符号预算（tier 未知时按 standard）。"""
    key = str(tier or "standard").casefold()
    return TIER_SYMBOL_BUDGET.get(key, TIER_SYMBOL_BUDGET["standard"])


def build_unit_context(
    unit: str,
    root: Path | None = None,
    *,
    max_symbols: int | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    include_prior_digest: bool = False,
    tier: str | None = None,
) -> str:
    """装配该单元的定向上下文（紧凑、高密度、确定性）。

    返回单行文本；任何一步失败都降级跳过，最终仍返回可用上下文
    （最坏情况只有符号摘要或只有攻击面标签）。
    """
    target_root = Path(root).resolve() if root is not None else _core_root(None)
    path = _unit_path(unit, target_root)
    # A4：未显式指定预算时，按该单元验证深度推导符号数。
    if max_symbols is None:
        if tier is None:
            try:
                from core.supervisor import task_scheduler as _ts
                tier = _ts.verification_tier(unit)
            except Exception:
                tier = "standard"
        max_symbols = symbols_for_tier(unit, tier)
    sections: list[str] = []

    if path is not None:
        symbols = extract_symbols(path, limit=max_symbols)
        if symbols:
            sections.append("symbols=" + ",".join(symbols))

    labels = surface_labels(unit)
    if labels:
        sections.append("surface=" + ",".join(labels))

    ids = rule_ids()  # 规则在引擎侧，与项目根无关
    if ids:
        sections.append("rules=" + ",".join(ids))

    if include_prior_digest:
        digest = prior_digest(unit, target_root)
        if digest:
            sections.append("prior=" + digest)

    text = " | ".join(sections)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip(" ,|") + "…"
    return text
