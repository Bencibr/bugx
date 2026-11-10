#!/usr/bin/env python3
"""impact_pruner.py — 增量 AST 变更波及剪枝引擎（DESIGN-E1）。

用途：
  结合 Git diff 与抽象语法树（AST）符号引用图，精确定位变更代码及受其波及的
  1-hop 与 2-hop 调用者/被调用者扇入扇出网络（Blast Radius），从而为 SAST、
  Fuzzer、单测和动态验证精准剪枝，避免在大型代码库中对 90%+ 未受影响的文件
  进行耗时扫描与测试。

核心流程：
  1. git diff 检测：获取相对 base 引用（默认 HEAD~1）的修改文件列表；
  2. AST 符号提取：提取变更范围内的函数、方法、类及顶层定义；
  3. 波及图计算：结合项目依赖图与导入关系，计算深度为 depth 的双向波及网络；
  4. 剪枝过滤：输出 retained（保留）与 pruned（剪除）文件列表及详细清单。

CLI 用法：
  # 生成变更波及清单
  python3 impact_pruner.py run --root . [--base HEAD~1] [--depth 2] [--out impact.json] [--json]

  # 依据清单过滤输入文件列表（支持管道与命令行参数）
  python3 impact_pruner.py filter --manifest impact.json file1.py file2.py ...
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

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
import copy
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

#: 内存波及网络计算缓存：key=(root, base, depth) -> (token, payload)
_IMPACT_NETWORK_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}


def clear_impact_network_cache() -> None:
    """清空波及网络缓存（测试或强制重算时使用）。"""
    _IMPACT_NETWORK_CACHE.clear()


def _git_fast_token(root: Path) -> str:
    """零子进程快速提取 Git 状态版本 token；非 git 仓库或读取失败返回空串。"""
    try:
        dot_git = root / ".git"
        git_dir = dot_git
        if dot_git.is_file():
            marker = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
            if marker.startswith("gitdir:"):
                candidate = Path(marker.split(":", 1)[1].strip())
                git_dir = candidate if candidate.is_absolute() else (root / candidate)
        if not git_dir.is_dir():
            return ""
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = git_dir / ref
            val = ref_path.read_text(encoding="utf-8", errors="ignore").strip() if ref_path.is_file() else "<unborn>"
            return f"{ref}={val}"
        return head
    except OSError:
        return ""


@dataclass(frozen=True)
class ChangeEntry:
    path: str
    status: str
    old_path: str | None = None


def git_changed_files(root: Path, base: str = "HEAD~1") -> list[ChangeEntry]:
    """运行 git diff 检测相对 base 的文件变更集合。若非 git 仓库则返回空列表。"""
    root = root.resolve()
    # 尝试 git status 确保位于合法 git 仓库
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root, capture_output=True, text=True, check=False, timeout=10,
        )
        if check.returncode != 0:
            return []
    except (OSError, subprocess.TimeoutExpired):
        return []

    changes: dict[str, ChangeEntry] = {}

    # 1. 检查已提交变更 (base...HEAD 或 base)
    diff_cmd = ["git", "diff", "--name-status", "-M", base]
    try:
        res = subprocess.run(
            diff_cmd, cwd=root, capture_output=True, text=True, check=False, timeout=20,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.strip().split("\t")
                if not parts:
                    continue
                status_code = parts[0]
                if status_code.startswith(("R", "C")) and len(parts) >= 3:
                    old_path, new_path = parts[1].strip(), parts[2].strip()
                    changes[new_path] = ChangeEntry(
                        path=new_path,
                        status="renamed" if status_code.startswith("R") else "copied",
                        old_path=old_path,
                    )
                else:
                    path = parts[-1].strip()
                    status_name = {
                        "A": "added", "M": "modified", "D": "deleted",
                        "T": "type_changed", "U": "unmerged",
                    }.get(status_code[:1], "modified")
                    changes[path] = ChangeEntry(path=path, status=status_name)
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 2. 检查未跟踪的新增文件 (untracked files)
    try:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=False, timeout=15,
        )
        if untracked.returncode == 0:
            for line in untracked.stdout.splitlines():
                path = line.strip()
                if path and path not in changes:
                    changes[path] = ChangeEntry(path=path, status="untracked")
    except (OSError, subprocess.TimeoutExpired):
        pass

    return sorted(changes.values(), key=lambda c: c.path)


class _SymbolCollector(ast.NodeVisitor):
    """收集 Python 源码中声明的类与函数符号。"""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.symbols: list[str] = []
        self.parents: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        name = node.name
        self.symbols.append(f"{self.rel_path}::{'.'.join([*self.parents, name])}")
        self.parents.append(name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        name = node.name
        self.symbols.append(f"{self.rel_path}::{'.'.join([*self.parents, name])}")
        self.parents.append(name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        name = node.name
        self.symbols.append(f"{self.rel_path}::{'.'.join([*self.parents, name])}")
        self.parents.append(name)
        self.generic_visit(node)
        self.parents.pop()


def extract_symbols_from_file(root: Path, rel_path: str) -> list[str]:
    """提取单个 Python 文件的主要顶层及嵌套符号。"""
    full_path = root / rel_path
    if not full_path.is_file() or not rel_path.endswith(".py"):
        return [rel_path]
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(content, filename=rel_path)
    except Exception:
        return [rel_path]

    collector = _SymbolCollector(rel_path)
    collector.visit(tree)
    return collector.symbols or [rel_path]


def _build_simple_dependency_graph(root: Path, py_files: Iterable[Path]) -> dict[str, set[str]]:
    """构建轻量 Python 模块引用邻接表（基于 import 与 from ... import）。"""
    # 建立 module_name -> relative_path 映射
    module_to_rel: dict[str, str] = {}
    rel_to_module: dict[str, str] = {}
    for p in py_files:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        mod = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        module_to_rel[mod] = rel
        rel_to_module[rel] = mod

    neighbors: dict[str, set[str]] = defaultdict(set)

    for p in py_files:
        try:
            rel = p.relative_to(root).as_posix()
            source = p.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel)
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target_rel = module_to_rel.get(alias.name)
                    if target_rel and target_rel != rel:
                        neighbors[rel].add(target_rel)
                        neighbors[target_rel].add(rel)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                target_rel = module_to_rel.get(mod)
                if target_rel and target_rel != rel:
                    neighbors[rel].add(target_rel)
                    neighbors[target_rel].add(rel)

    return neighbors


def compute_impact_network(
    root: Path,
    base: str = "HEAD~1",
    depth: int = 2,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """计算增量变更集并计算 2-hop 波及面（Blast Radius）。支持 Git Token 级极速内存缓存。"""
    root = root.resolve()
    cache_key = f"{root}::{base}::{depth}"
    token = _git_fast_token(root) if use_cache else ""
    if use_cache and token:
        cached = _IMPACT_NETWORK_CACHE.get(cache_key)
        if cached is not None and cached[0] == token:
            return copy.deepcopy(cached[1])

    change_entries = git_changed_files(root, base=base)
    changed_paths = sorted({entry.path for entry in change_entries})

    # 枚举仓库所有 Python 文件以构建依赖关系
    all_py_files: list[Path] = []
    excluded_dirs = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        for f in filenames:
            if f.endswith(".py"):
                all_py_files.append(Path(dirpath) / f)

    all_rel_paths = sorted({p.relative_to(root).as_posix() for p in all_py_files})

    # 若未检出任何变更（如全新环境或非 git），回退为包含所有文件以防漏扫
    if not changed_paths:
        fallback_res = {
            "schema_version": SCHEMA_VERSION,
            "root": str(root),
            "base": base,
            "depth": depth,
            "fallback_all": True,
            "changed_files": [],
            "changed_symbols": [],
            "impacted_files": all_rel_paths,
            "impacted_symbols": [],
            "retained_count": len(all_rel_paths),
            "pruned_count": 0,
            "reduction_ratio": 0.0,
        }
        if use_cache and token:
            _IMPACT_NETWORK_CACHE[cache_key] = (token, fallback_res)
        return fallback_res

    # 提取变更文件内的顶层/方法级符号
    changed_symbols: list[str] = []
    for cpath in changed_paths:
        changed_symbols.extend(extract_symbols_from_file(root, cpath))

    # 构建关系图并扩展波及面
    neighbors = _build_simple_dependency_graph(root, all_py_files)

    impacted: set[str] = set(changed_paths)
    frontier: set[str] = set(changed_paths)

    for _ in range(max(1, depth)):
        next_frontier: set[str] = set()
        for f in frontier:
            for n in neighbors.get(f, set()):
                if n not in impacted:
                    next_frontier.add(n)
        impacted.update(next_frontier)
        frontier = next_frontier

    # 规范化：波及集必须包含匹配的对应测试文件（如 core/xyz.py -> tests/test_xyz.py）
    for cpath in list(impacted):
        stem = Path(cpath).stem
        for candidate in all_rel_paths:
            if candidate.startswith("tests/") and (f"test_{stem}" in candidate or stem in candidate):
                impacted.add(candidate)

    impacted_files = sorted(impacted)
    retained_count = len(impacted_files)
    total_count = len(all_rel_paths)
    pruned_count = max(0, total_count - retained_count)
    reduction_ratio = round(pruned_count / total_count, 4) if total_count > 0 else 0.0

    result = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "base": base,
        "depth": depth,
        "fallback_all": False,
        "changed_files": changed_paths,
        "changed_symbols": changed_symbols,
        "impacted_files": impacted_files,
        "retained_count": retained_count,
        "pruned_count": pruned_count,
        "reduction_ratio": reduction_ratio,
    }

    if use_cache and token:
        _IMPACT_NETWORK_CACHE[cache_key] = (token, result)

    return result


def filter_paths_with_manifest(manifest: dict[str, Any], candidate_paths: Iterable[str]) -> list[str]:
    """使用波及清单过滤文件路径列表，仅保留属于波及集合的文件。"""
    if manifest.get("fallback_all"):
        return list(candidate_paths)

    impacted = set(manifest.get("impacted_files", []))
    # 支持绝对路径与相对路径比对
    root_str = manifest.get("root", "")
    retained: list[str] = []

    for path_str in candidate_paths:
        cleaned = path_str.strip()
        if not cleaned:
            continue
        rel = cleaned
        if root_str:
            try:
                # 真实路径边界判断：字符串 startswith 会把 /a/project-x 误当作 /a/proj 之下
                rel = str(Path(cleaned).resolve().relative_to(Path(root_str).resolve()))
            except ValueError:
                rel = cleaned
        rel = rel.replace("\\", "/")
        if rel in impacted or cleaned in impacted:
            retained.append(cleaned)

    return retained


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    # run 子命令
    p_run = sub.add_parser("run", help="计算变更波及面并生成剪枝清单")
    p_run.add_argument("--root", type=Path, default=Path.cwd(), help="项目根目录（默认当前目录）")
    p_run.add_argument("--base", default="HEAD~1", help="基准 Git 提交或分支引用（默认 HEAD~1）")
    p_run.add_argument("--depth", type=int, default=2, help="依赖扩展跳数（默认 2 跳）")
    p_run.add_argument("--out", type=Path, default=None, help="输出清单保存路径")
    p_run.add_argument("--json", action="store_true", help="以 JSON 格式输出至 stdout")

    # filter 子命令
    p_filter = sub.add_parser("filter", help="根据清单过滤输入文件列表")
    p_filter.add_argument("--manifest", type=Path, required=True, help="波及清单 JSON 文件路径")
    p_filter.add_argument("paths", nargs="*", help="待过滤的文件路径（留空则从标准输入逐行读取）")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.action == "run":
        manifest = compute_impact_network(args.root, base=args.base, depth=args.depth)
        formatted_json = json.dumps(manifest, ensure_ascii=False, indent=2)

        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(formatted_json, encoding="utf-8")
            print(f"[impact_pruner] 波及清单已写入: {args.out}")

        if args.json or not args.out:
            print(formatted_json)
        else:
            print(f"[impact_pruner] 变更文件: {len(manifest['changed_files'])} 个；"
                  f"波及保留: {manifest['retained_count']} 个；"
                  f"剪除率: {manifest['reduction_ratio'] * 100:.1f}%")
        return 0

    elif args.action == "filter":
        if not args.manifest.is_file():
            print(f"[impact_pruner] 错误: 清单文件不存在: {args.manifest}", file=sys.stderr)
            return 2
        try:
            manifest_data = json.loads(args.manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[impact_pruner] 错误: 无法解析清单 JSON: {exc}", file=sys.stderr)
            return 2

        candidates = args.paths
        if not candidates:
            candidates = [line.strip() for line in sys.stdin if line.strip()]

        retained = filter_paths_with_manifest(manifest_data, candidates)
        for path in retained:
            print(path)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
