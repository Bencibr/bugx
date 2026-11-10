#!/usr/bin/env python3
"""为 BugX 建立轻量、零依赖的源码符号索引。

Python 使用标准库 AST 精确解析；其他受支持语言使用保守的声明级扫描器。
索引既可以供人查看，也可以让 module_coverage 把文件继续拆到函数、类、方法、
路由处理器等可独立审计单元。解析失败会显式记录，不会静默把失败当成无符号文件。
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
import ast
import json
import hashlib
import os
import re
from dataclasses import asdict, dataclass, field, replace
from fnmatch import fnmatchcase

SCHEMA_VERSION = 2
# These directories belong to the host/agent/runtime rather than the product
# being audited.  They are excluded at every depth: a checked-out worktree
# under .claude/worktrees is still another branch, not another product module.
#
# DOC-239：已迁移宿主（``opencode`` / ``cline`` / ...）的目录只能来自
# ``core/hosts/<host>.py``（``install.marker`` / ``generated_dirs``），不得在此
# 重复声明；下面只剩**未迁移宿主**与通用工具目录的待迁移基线。
_LEGACY_HOST_RUNTIME_DIRS = {
    ".claude", ".pi", ".agents", ".dsh", ".gstack", ".mirasim",
    ".bugx", ".code-review-graph",
    ".workbuddy", ".codebuddy", ".qoder", ".trea", ".trae", ".trae-cn", ".zcode",
}


def _is_engine_namespace_segment(part: str) -> bool:
    """任一目录分量是否属于 BugX 引擎自己的运行态命名空间。

    ``_LEGACY_HOST_RUNTIME_DIRS`` 只登记了确切的 ``.bugx``，而引擎与宿主 AI
    会派生**同前缀**的兄弟目录：``.bugx-audit-artifacts``（host 写探针/复现
    脚本）、``.bugx-patches``（PE 工单补丁包）、``.bugx-cache`` ……。这些全部
    是审计产物而不是被审产品材料。

    实际后果：宿主在项目根写下
    ``.bugx-audit-artifacts/repro.py`` 后，它被当成 3 个新内部单元
    （``probe_sqli.py``、``repro.py::record``、``repro.py::__module__``）并入
    清单，``post`` 随即报「符号索引不完整」；宿主删掉文件后单元又变
    ``removed``，``workspace`` 指纹反复漂移触发 ``target_rebound``，覆盖状态
    在 ``covered → stale → pending → removed`` 之间来回翻烧。

    前缀匹配而不是枚举名单：新派生的运行态目录不会因为漏登记再次污染覆盖。
    用户自己的 ``.bugxfoo`` 产品目录同样被排除，但这与 ``.git*`` 的既有约定
    一致——点前缀命名空间归工具所有。
    """
    return part == ".bugx" or part.startswith(".bugx-") or part.startswith(".bugx_")


def _plugin_host_runtime_dirs() -> set[str]:
    """已迁移插件声明的宿主目录（模块布局残缺时 fail-soft 为空集）。"""

    try:  # 引擎运行时：``core/`` 在 sys.path 上
        from core.host_runtime.host_registry import host_workspace_dirs
    except ImportError:  # pragma: no cover - 仅模块被单独拷出时发生
        return set()
    return set(host_workspace_dirs())


#: 引擎自产的运行态目录（**目录名**，按路径分量匹配）。
#: 自包含引擎副本布局把 Session/Round 账本放在项目根的 ``pollings/`` 下
#: （见 ``launch_phases.common.pollings_root``），而不是 ``.bugx/`` 内。
#: 不排除它们会把 ``findings.json`` 当成被审源码资产，要求给出覆盖单元与
#: 证据（实测 ``post --final`` 报 "#3 findings.json 缺证据"）。
RUNTIME_ARTIFACT_DIRS = {"pollings", "receipts", "artifacts", "backups"}

HOST_RUNTIME_DIRS = (
    _LEGACY_HOST_RUNTIME_DIRS | _plugin_host_runtime_dirs() | RUNTIME_ARTIFACT_DIRS
)
HOST_ROOT_CONFIG_NAMES = {
    "opencode.json", ".mcp.json",
}
DEPENDENCY_OR_GENERATED_DIRS = {
    ".git", "node_modules", "target", "dist", ".venv", "venv", "__pycache__",
    ".gradle", "vendor", "third_party", ".pytest_cache", ".ruff_cache",
}
# Keep the old public name for callers that used it, but do not use it as a
# blunt ``any path segment`` filter: src/build/rule.py is product code.
EXCLUDED_DIRS = HOST_RUNTIME_DIRS | DEPENDENCY_OR_GENERATED_DIRS | {
    "build", "tests", "test", "__tests__", "docs", "examples", "evals",
}
TOP_LEVEL_SOURCE_EXCLUDED_DIRS = {
    "build", "tests", "test", "__tests__", "docs", "examples", "evals",
    # BugX 自身产物约定：patch-forge 的 PE 工单补丁包（repro/tests 记录），
    # 与 plugins/bugx 同类——是引擎历史工件而非被测产品代码。
    ".bugx-patches",
}
# 资产发现（include_test_assets=True）额外排除的顶层目录：BugX 双轨报告/
# 收据默认输出到 ``evals/oss``（HTML/JSON 均在资产扩展名清单内）——审计
# 产物喂回覆盖清单是自增强污染（实测 evals 报告被列为审计单元）。
TOP_LEVEL_ASSET_EXCLUDED_DIRS = {
    "evals",
    ".bugx-patches",
}
SOURCE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go",
    ".rs", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx", ".cs", ".rb",
    ".php", ".vue", ".svelte",
}
AUDIT_ASSET_EXTS = {
    ".sh", ".bash", ".zsh", ".fish", ".sql", ".yaml", ".yml", ".toml",
    ".json", ".jsonc", ".xml", ".ini", ".cfg", ".conf", ".properties",
    ".proto", ".graphql", ".gql", ".tf", ".tfvars", ".html", ".css",
    ".scss", ".sass", ".less", ".lock",
}
AUDIT_ASSET_NAMES = {
    "Dockerfile", "Makefile", "GNUmakefile", "CMakeLists.txt", "Procfile",
    "Justfile", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "pom.xml",
    "build.gradle", "settings.gradle", "Gemfile", "Gemfile.lock",
    "composer.json", "requirements.txt", "requirements-dev.txt", "Pipfile",
    "Pipfile.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "uv.lock", "setup.cfg", "tox.ini",
    ".pre-commit-config.yaml", ".env.example",
}
RUNTIME_ARTIFACT_NAMES = {
    "bugx-life.json", "bugx-life.json.lock", "bugx-runtime.json",
    "bugx-runtime.json.lock", "bugx-runtime.lock", "audit-supervisor.json",
    "audit-supervisor.json.lock",
    "coverage-evidence.json", "coverage-evidence.json.lock", "lesson-review.json",
    "lesson-review.json.lock", "module-coverage.md", "prep-record.md",
    # Flat/legacy launch mode writes the formal lifecycle artifacts at the
    # project root.  Formal projects are already protected by the .bugx
    # directory exclusion; keeping the names here protects the legacy mode
    # without excluding a same-named product file in a subdirectory.
    "life.json", "runtime.json", "runtime.json.lock", "supervisor.json",
    "supervisor.json.lock", "state.json", "current.json", "events.jsonl",
    "report.md", "conclusion.json", "integrity.json", "inventory-diff.json",
    "finding-ledger.json", "finding-ledger.json.lock", ".finding-ledger.json.lock",
    "findings.json", "findings.json.lock", ".findings.json.lock",
    "audit-campaign.json", "finding-lifecycle.json", "bugx-life.json.snapshot",
    "lesson-ledger.sqlite3.journal-mode.lock",
    # 项目错题集是引擎运行态（``common.LESSON_LEDGER_FILE``：项目根的
    # lesson-ledger.json，由 lesson_ledger.py 写入），旧清单漏登记它，
    # 结果它被当成**被审产品资产**参与覆盖门禁：新工程刚初始化就必然
    # 报「最小单元清单漏列源码或行为资产: lesson-ledger.json」，
    # 而它根本不该有覆盖单元。同类运行态（finding-ledger.json 等）已在册。
    "lesson-ledger.json", "lesson-ledger.json.lock",
    # 云端 onboarding 写入项目根的云端身份标记（cloud_onboard.py），
    # 属引擎运行态而非产品材料——与 .bugx 目录同类。
    ".bugx-project.json",
}


class SourceDiscoveryError(ValueError):
    """源码枚举不完整，调用方不得继续声称全覆盖。"""


@dataclass(frozen=True)
class Symbol:
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    parent: str | None
    parser: str
    start_byte: int | None = field(default=None, compare=False, repr=False)

    @property
    def scope_name(self) -> str:
        return self.qualified_name


def _gitignore_rules(root: Path) -> list[str]:
    """Read the root ignore file once for both source and asset discovery."""
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    return [
        line.strip() for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _gitignore_matches(relative: Path, *, is_dir: bool, rules: list[str]) -> bool:
    """Apply the root .gitignore's last matching rule to a relative path."""
    # 只剥掉调用方可能带来的 "./" 前缀；str.lstrip("./") 是字符集剥离，
    # 会把点前缀文件名（.bugx-project.json、.env、.github…）的开头点吃掉，
    # 导致 gitignore 名单对全部隐藏文件静默失效——而引擎自产工件恰恰
    # 集中在点前缀命名空间（实测 .myapp-state.json 已入 .gitignore 仍被
    # 发现）。
    raw_path = relative.as_posix()
    path = raw_path[2:] if raw_path.startswith("./") else raw_path
    ignored = False
    for raw in rules:
        negated = raw.startswith("!")
        pattern = raw[1:] if negated else raw
        directory_only = pattern.endswith("/")
        pattern = pattern.rstrip("/").lstrip("/")
        if not pattern or (directory_only and not is_dir):
            continue
        if "/" not in pattern:
            matched = any(fnmatchcase(part, pattern) for part in Path(path).parts)
        else:
            matched = fnmatchcase(path, pattern) or fnmatchcase(path, f"*/{pattern}")
        if matched:
            ignored = not negated
    return ignored


def _generated_directory(relative: Path) -> bool:
    """Filter generated variants without hiding product code in src/build."""
    parts = relative.parts
    for index, part in enumerate(parts):
        lowered = part.casefold()
        if lowered == "dist" or lowered.startswith("dist-") \
                or lowered.startswith(".tmp-"):
            return True
        if lowered == "build" and index + 1 < len(parts) \
                and parts[index + 1].casefold() == "generated":
            return True
    return False


_PROJECT_EXCLUDE_TOP_DIRS_CACHE: dict[Path, frozenset[str]] = {}


def _project_exclude_top_dirs(root: Path) -> frozenset[str]:
    """项目级覆盖排除，两路来源取并集（AI 复核为主，手工配置为辅）：

    1. **AI 复核定案**（主通道）：``.bugx/knowledge/coverage-review.json``
       里宿主 AI 逐目录排查后的 ``exclude`` 裁决——裁决带理由/时间/裁决者，
       换项目时由该机制重新排查，不携带任何项目名清单（DOC-AIREV）。
    2. **手工覆盖**（辅助）：``.bugx/config.json`` 的
       ``coverage.exclude_top_dirs`` 顶层目录名列表，供人工紧急纠偏。

    读取失败一律返回空集（fail-open：宁可多扫，不因配置错误吞掉产品代码）。
    """
    cached = _PROJECT_EXCLUDE_TOP_DIRS_CACHE.get(root)
    if cached is not None:
        return cached
    names: set[str] = set()
    try:
        from core.coverage import coverage_ai_review

        names |= set(coverage_ai_review.excluded_top_dirs(root))
    except Exception:  # pragma: no cover - 复核模块缺失/损坏时退回配置通道
        pass
    try:
        cfg = root / ".bugx" / "config.json"
        if cfg.is_file():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            raw = (data.get("coverage") or {}).get("exclude_top_dirs")
            if isinstance(raw, list):
                names |= {
                    str(item).strip().strip("/")
                    for item in raw
                    if isinstance(item, str) and item.strip().strip("/")
                }
    except (OSError, ValueError, AttributeError):
        pass
    frozen = frozenset(names)
    _PROJECT_EXCLUDE_TOP_DIRS_CACHE[root] = frozen
    return frozen


def _directory_is_excluded(
    relative: Path, *, include_test_assets: bool,
    gitignore_rules: list[str] | None = None, is_dir: bool = True,
    project_root: Path | None = None,
) -> bool:
    parts = set(relative.parts)
    if parts & (HOST_RUNTIME_DIRS | DEPENDENCY_OR_GENERATED_DIRS) \
            or any(_is_engine_namespace_segment(part) for part in parts) \
            or _generated_directory(relative):
        return True
    if include_test_assets and relative.parts and relative.parts[0] in TOP_LEVEL_ASSET_EXCLUDED_DIRS:
        return True
    if not include_test_assets and relative.parts and relative.parts[0] in TOP_LEVEL_SOURCE_EXCLUDED_DIRS:
        return True
    if project_root is not None and relative.parts and relative.parts[0] in _project_exclude_top_dirs(project_root):
        return True
    return _gitignore_matches(relative, is_dir=is_dir, rules=gitignore_rules or [])


_ENGINE_PLUGIN_PACKAGE_PREFIX = ("plugins", "bugx")
_ENGINE_PLUGIN_PACKAGE_CACHE: dict[Path, bool] = {}


def _is_engine_plugin_package(relative: Path, root: Path) -> bool:
    """安装器写入项目的 BugX 自产插件包不是被审计产品材料。

    ``bugx install`` 会在项目根写入可分发的 ``plugins/bugx/.zcode-plugin``
    插件包（清单 + agents）；它属于宿主集成产物而非用户源码，与
    ``.bugx/`` 同类。判定用指纹而非目录名：相对路径必须以
    ``plugins/bugx/`` 开头，且该目录下的清单声明 ``name == "bugx"``。
    用户自己的 ``plugins/bugx`` 产品代码（无该清单或声明其他 name）
    不受影响。结果按 root 缓存，walk 期间零额外 IO。
    """
    parts = relative.parts
    if len(parts) < 2 or parts[:2] != _ENGINE_PLUGIN_PACKAGE_PREFIX:
        return False
    root = root.resolve()
    cached = _ENGINE_PLUGIN_PACKAGE_CACHE.get(root)
    if cached is None:
        cached = False
        manifest = root.joinpath(*_ENGINE_PLUGIN_PACKAGE_PREFIX, ".zcode-plugin", "plugin.json")
        try:
            if manifest.is_file():
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                cached = isinstance(payload, dict) and payload.get("name") == "bugx"
        except (OSError, ValueError):
            cached = False
        _ENGINE_PLUGIN_PACKAGE_CACHE[root] = cached
    return cached


def _is_runtime_artifact(path: Path, root: Path) -> bool:
    """Exclude root-level legacy runtime output and host root configs, not product assets.

    A project may legitimately contain ``config/runtime.json`` or
    ``docs/report.md``. Runtime names and host configs therefore are not a global
    blacklist; only flat launcher output and root host configs (e.g. opencode.json,
    .mcp.json) directly below the audited root are ignored.
    In the formal layout all runtime files live below ``.bugx`` and are
    excluded by the directory policy before this check matters.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return relative.parent == Path(".") and (
        path.name in RUNTIME_ARTIFACT_NAMES
        or path.name in HOST_ROOT_CONFIG_NAMES
    )


def _discover_paths(
    root: Path, *, extensions: set[str], names: set[str] | None = None,
    strict: bool = False, include_test_assets: bool = False,
) -> list[Path]:
    """Walk the authorized root without following directory symlinks.

    ``Path.rglob`` silently skips inaccessible subdirectories on some Python
    versions.  The audit gate must be able to distinguish an empty directory
    from an incomplete enumeration, so ``os.walk(onerror=...)`` is used here.
    """
    root = root.resolve()
    if not root.is_dir():
        if strict:
            raise SourceDiscoveryError(f"项目根目录不存在或不可访问: {root}")
        return []
    paths: list[Path] = []
    seen_targets: set[Path] = set()
    gitignore_rules = _gitignore_rules(root)

    def onerror(exc: OSError) -> None:
        if strict:
            raise SourceDiscoveryError(f"项目文件枚举失败: {exc}") from exc

    try:
        for directory, dirnames, filenames in os.walk(
            root, topdown=True, followlinks=False, onerror=onerror,
        ):
            directory_path = Path(directory)
            relative_dir = directory_path.relative_to(root)
            dirnames[:] = [
                name for name in dirnames
                if not _directory_is_excluded(
                    relative_dir / name, include_test_assets=include_test_assets,
                    gitignore_rules=gitignore_rules, project_root=root,
                )
                and not _is_engine_plugin_package(relative_dir / name, root)
            ]
            for name in filenames:
                path = directory_path / name
                relative = path.relative_to(root)
                if _is_runtime_artifact(path, root):
                    continue
                if _is_engine_plugin_package(relative, root):
                    continue
                if _directory_is_excluded(
                    relative, include_test_assets=include_test_assets,
                    gitignore_rules=gitignore_rules, is_dir=False,
                    project_root=root,
                ):
                    continue
                if path.suffix.lower() not in extensions and name not in (names or set()):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                    if not path.is_file() or resolved in seen_targets:
                        continue
                    seen_targets.add(resolved)
                    paths.append(path)
                except ValueError:
                    # 指向授权根目录之外的符号链接不属于审计范围。
                    continue
                except OSError as exc:
                    if strict:
                        raise SourceDiscoveryError(f"项目文件枚举失败 {path}: {exc}") from exc
    except OSError as exc:
        if strict:
            raise SourceDiscoveryError(f"项目目录遍历失败 {root}: {exc}") from exc
    return sorted(paths, key=lambda item: str(item.relative_to(root)))


def discover_source_paths(root: Path, strict: bool = False) -> list[Path]:
    """递归返回产品源码，排除依赖、构建产物、测试和 Agent 自身。"""
    return _discover_paths(
        root, extensions=SOURCE_EXTS, strict=strict, include_test_assets=False,
    )


def discover_asset_paths(root: Path, strict: bool = False) -> list[Path]:
    """发现会改变运行行为的配置、依赖、构建、CI 与部署资产。

    测试/文档目录不作为源码模块，但其中的脚本和配置仍可能是验收证据，
    因此资产发现保留这些目录；宿主运行目录和生成/第三方目录仍始终排除。
    """
    return _discover_paths(
        root, extensions=AUDIT_ASSET_EXTS, names=AUDIT_ASSET_NAMES,
        strict=strict, include_test_assets=True,
    )


def _decode_source(raw: bytes) -> str:
    """Decode bytes exactly like ``Path.read_text(encoding="utf-8", errors="replace")``.

    ``Path.read_text`` opens in universal-newline mode, which rewrites CRLF and
    lone CR to LF.  Decoding the cached bytes must do the same, otherwise symbol
    line ranges and ``ast.get_source_segment`` evidence would change on CRLF
    checkouts.  The replacement order matters: ``\\r\\n`` first, then ``\\r``.
    """
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


class SourceContentCache:
    """Per-pass cache of raw source bytes so a pass reads each file once.

    BugX re-reads every source file two to three times per pass: ``parse_file``
    for symbols, ``file_sha256`` for the content hash, and again when the
    project graph re-parses Python sources.  Threading one cache through a pass
    collapses those into a single read per file.

    Lifetime is deliberately a *single pass* (one ``index_repository`` /
    ``audit_units`` / ``inventory_snapshot`` / ``build_graph`` call), never a
    process-global memo.  The audit engine re-runs against a workspace that can
    change between passes (edits, ``git checkout``, generated files), so a
    process-wide cache would silently serve stale content.  Read failures are
    not cached, so an unreadable file still raises the same ``OSError`` on every
    attempt and a mid-pass change is never masked by a cached failure.
    """

    __slots__ = ("_bytes",)

    def __init__(self) -> None:
        self._bytes: dict[Path, bytes] = {}

    def read_bytes(self, path: Path) -> bytes:
        """Return the file's bytes, hitting the disk at most once per pass."""
        try:
            return self._bytes[path]
        except KeyError:
            data = path.read_bytes()
            self._bytes[path] = data
            return data

    def read_text(self, path: Path) -> str:
        """Return the file as text with ``Path.read_text`` newline semantics."""
        return _decode_source(self.read_bytes(path))


def file_sha256(path: Path, cache: SourceContentCache | None = None) -> str:
    """计算文件内容的 SHA-256（1 MiB 分块，避免整文件驻留内存）。

    这是全仓文件哈希的唯一实现（QUA-03）：``coverage_evidence.sha256_file`` 与
    ``mcp_runtime.sha256_file`` 都复用本函数。本模块保持零本地依赖——它会被
    复制进隔离的 launch 目录单独运行，不能引入 core 内其它模块。
    """
    if cache is not None:
        return hashlib.sha256(cache.read_bytes(path)).hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.symbols: list[Symbol] = []
        self.parents: list[str] = []

    def _record(self, node: ast.AST, name: str, kind: str) -> None:
        qualified = ".".join([*self.parents, name])
        self.symbols.append(Symbol(
            name=name,
            qualified_name=qualified,
            kind=kind,
            start_line=int(getattr(node, "lineno", 1)),
            end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
            parent=".".join(self.parents) or None,
            parser="python-ast",
        ))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._record(node, node.name, "class")
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self.parents else "function"
        self._record(node, node.name, kind)
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = "async-method" if self.parents else "async-function"
        self._record(node, node.name, kind)
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()


def _python_symbols(source: str) -> tuple[list[Symbol], str | None]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], f"Python 语法错误: line {exc.lineno}: {exc.msg}"
    visitor = _PythonVisitor()
    visitor.visit(tree)
    module_nodes = [
        node for node in tree.body
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef))
        and not (
            isinstance(node, ast.Expr)
            and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    if module_nodes:
        visitor.symbols.append(Symbol(
            name="__module__",
            qualified_name="__module__",
            kind="module",
            start_line=min(int(getattr(node, "lineno", 1)) for node in module_nodes),
            end_line=max(int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))
                        for node in module_nodes),
            parent=None,
            parser="python-ast",
        ))
    return visitor.symbols, None


DECLARATION_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    "javascript": (
        ("class", r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:export\s+)?(?:async\s+)?function\s+"
                     r"([A-Za-z_$][\w$]*)\s*\("),
        ("function", r"^\s*(?:export\s+)?(?:const|let|var)\s+"
                     r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                     r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    ),
    "jvm": (
        ("class", r"^\s*(?:(?:public|private|protected|internal|abstract|final|open|"
                  r"data|sealed)\s+)*(?:class|interface|object|enum\s+class)\s+"
                  r"([A-Za-z_$][\w$]*)"),
        ("function", r"^\s*(?:(?:public|private|protected|internal|static|final|open|"
                     r"suspend|override|abstract)\s+)*(?:fun\s+|[\w<>\[\],.?]+\s+)"
                     r"([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:\{|=|throws\b)"),
    ),
    "go": (
        ("type", r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b"),
        ("function", r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    ),
    "rust": (
        ("type", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
                     r"fn\s+([A-Za-z_]\w*)\s*(?:<[^>]+>)?\s*\("),
    ),
    "swift": (
        ("type", r"^\s*(?:(?:public|private|internal|open|final)\s+)*"
                 r"(?:class|struct|enum|protocol|actor)\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:(?:public|private|internal|open|static|class|override|"
                     r"mutating)\s+)*func\s+([A-Za-z_]\w*)\s*\("),
    ),
    "c-family": (
        ("type", r"^\s*(?:(?:public|private|protected|internal|static|abstract|sealed|"
                 r"partial)\s+)*(?:class|struct|interface|enum)\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:(?:public|private|protected|internal|static|virtual|"
                     r"override|inline|constexpr|extern)\s+)*"
                     r"[A-Za-z_][\w:<>,*&\[\]\s]*\s+([A-Za-z_]\w*)\s*"
                     r"\([^;]*\)\s*(?:\{|=>)"),
    ),
    "ruby": (
        ("type", r"^\s*(?:class|module)\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)"),
        ("function", r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)"),
    ),
    "php": (
        ("class", r"^\s*(?:(?:abstract|final)\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)"),
        ("function", r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*"
                     r"function\s+&?([A-Za-z_]\w*)\s*\("),
    ),
}
JAVASCRIPT_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|async|get|set)\s+)*"
    r"([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{"
)
JAVASCRIPT_CONTROL_WORDS = {"if", "for", "while", "switch", "catch", "with"}


def _language_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}:
        return "javascript"
    if suffix in {".java", ".kt", ".kts"}:
        return "jvm"
    if suffix == ".go":
        return "go"
    if suffix == ".rs":
        return "rust"
    if suffix == ".swift":
        return "swift"
    if suffix in {".c", ".h", ".cc", ".cpp", ".cxx", ".cs"}:
        return "c-family"
    if suffix == ".rb":
        return "ruby"
    return "php"


TREE_SITTER_LANGUAGES = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".go": "go",
    ".rs": "rust", ".swift": "swift", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".cs": "c_sharp",
    ".rb": "ruby", ".php": "php", ".vue": "vue", ".svelte": "svelte",
}
TREE_SITTER_NODE_KINDS = {
    "class_declaration": "class", "class_definition": "class",
    "interface_declaration": "type", "object_declaration": "type",
    "struct_item": "type", "struct_specifier": "type", "enum_item": "type",
    "enum_declaration": "type", "trait_item": "type", "protocol_declaration": "type",
    "function_declaration": "function", "function_definition": "function",
    "function_item": "function", "method_definition": "method",
    "method_declaration": "method", "function_method_declaration": "method",
}
TREE_SITTER_NAME_TYPES = {
    "identifier", "type_identifier", "property_identifier", "field_identifier",
    "constant", "operator_name", "name",
}


def _tree_sitter_name(node: object) -> str | None:
    direct = node.child_by_field_name("name")
    if direct is not None:
        return direct.text.decode("utf-8", errors="replace")
    declarator = node.child_by_field_name("declarator")
    queue = [declarator] if declarator is not None else list(node.named_children[:4])
    while queue:
        current = queue.pop(0)
        if current.type in TREE_SITTER_NAME_TYPES:
            return current.text.decode("utf-8", errors="replace")
        queue[0:0] = list(current.named_children[:4])
    return None


def _tree_sitter_symbols(source: str, language: str) -> tuple[list[Symbol], str | None]:
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return [], "Tree-sitter 后端未安装；安装 bugx-cli[graph]"
    try:
        tree = get_parser(language).parse(source.encode("utf-8"))
    except Exception as exc:  # language-pack 对不支持语法抛出多种运行时异常
        return [], f"Tree-sitter 无法加载 {language}: {exc}"
    symbols: list[Symbol] = []

    def walk(node: object, parents: list[str]) -> None:
        kind = TREE_SITTER_NODE_KINDS.get(node.type)
        name = _tree_sitter_name(node) if kind else None
        child_parents = parents
        if kind and name:
            qualified = ".".join([*parents, name])
            actual_kind = "method" if kind == "function" and parents else kind
            symbols.append(Symbol(
                name=name,
                qualified_name=qualified,
                kind=actual_kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent=".".join(parents) or None,
                parser=f"tree-sitter-{language}",
                start_byte=node.start_byte,
            ))
            child_parents = [*parents, name]
        for child in node.named_children:
            walk(child, child_parents)

    walk(tree.root_node, [])
    error = f"Tree-sitter {language} 语法树包含 ERROR 节点" if tree.root_node.has_error else None
    return symbols, error


def _declaration_symbols(source: str, language: str) -> tuple[list[Symbol], None]:
    symbols: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    lines = source.splitlines()
    for line_number, line in enumerate(lines, 1):
        for kind, pattern in DECLARATION_PATTERNS[language]:
            match = re.search(pattern, line)
            if not match:
                continue
            name = match.group(1)
            key = (name, line_number)
            if key not in seen:
                symbols.append(Symbol(
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    start_line=line_number,
                    end_line=_declaration_end_line(lines, line_number),
                    parent=None,
                    parser=f"{language}-declaration",
                ))
                seen.add(key)
            break
    return symbols, None


def _declaration_end_line(lines: list[str], start_line: int) -> int:
    """Estimate a declaration body range for the dependency-free scanner."""
    depth = 0
    saw_brace = False
    for index in range(start_line - 1, len(lines)):
        line = lines[index]
        opening = line.count("{")
        closing = line.count("}")
        if opening:
            saw_brace = True
        depth += opening - closing
        if saw_brace and depth <= 0:
            return index + 1
    return start_line


def _javascript_symbols(source: str) -> tuple[list[Symbol], None]:
    symbols: list[Symbol] = []
    brace_depth = 0
    class_stack: list[tuple[str, int]] = []
    for line_number, line in enumerate(source.splitlines(), 1):
        while class_stack and brace_depth < class_stack[-1][1]:
            class_stack.pop()
        class_match = re.search(DECLARATION_PATTERNS["javascript"][0][1], line)
        pending_class = class_match.group(1) if class_match else None
        if pending_class:
            symbols.append(Symbol(
                name=pending_class,
                qualified_name=pending_class,
                kind="class",
                start_line=line_number,
                end_line=line_number,
                parent=None,
                parser="javascript-declaration",
            ))
        elif class_stack:
            method_match = JAVASCRIPT_METHOD_RE.search(line)
            if method_match and method_match.group(1) not in JAVASCRIPT_CONTROL_WORDS:
                name = method_match.group(1)
                parent = class_stack[-1][0]
                symbols.append(Symbol(
                    name=name,
                    qualified_name=f"{parent}.{name}",
                    kind="method",
                    start_line=line_number,
                    end_line=_declaration_end_line(source.splitlines(), line_number),
                    parent=parent,
                    parser="javascript-declaration",
                ))
        else:
            for kind, pattern in DECLARATION_PATTERNS["javascript"][1:]:
                match = re.search(pattern, line)
                if match:
                    name = match.group(1)
                    symbols.append(Symbol(
                        name=name,
                        qualified_name=name,
                        kind=kind,
                        start_line=line_number,
                        end_line=_declaration_end_line(source.splitlines(), line_number),
                        parent=None,
                        parser="javascript-declaration",
                    ))
                    break
        old_depth = brace_depth
        brace_depth += line.count("{") - line.count("}")
        if pending_class and brace_depth > old_depth:
            class_stack.append((pending_class, brace_depth))
    return symbols, None


def _deduplicate_scopes(symbols: list[Symbol]) -> list[Symbol]:
    seen: set[str] = set()
    result: list[Symbol] = []
    for symbol in symbols:
        qualified_name = symbol.qualified_name
        if qualified_name in seen:
            line_suffix = f"@L{symbol.start_line}"
            candidate = f"{qualified_name}{line_suffix}"
            if candidate in seen:
                if symbol.start_byte is None:
                    candidate = f"{qualified_name}#2{line_suffix}"
                else:
                    candidate = f"{qualified_name}@B{symbol.start_byte}{line_suffix}"
                ordinal = 2
                while candidate in seen:
                    if symbol.start_byte is None:
                        candidate = f"{qualified_name}#{ordinal}{line_suffix}"
                    else:
                        candidate = (
                            f"{qualified_name}#{ordinal}@B{symbol.start_byte}{line_suffix}"
                        )
                    ordinal += 1
            symbol = replace(
                symbol,
                qualified_name=candidate,
            )
        seen.add(symbol.qualified_name)
        result.append(symbol)
    return result


def _merge_parser_symbols(primary: list[Symbol], fallback: list[Symbol]) -> list[Symbol]:
    """补上增强解析器漏掉的声明，同时保留其精确范围和父子关系。"""
    merged = list(primary)
    seen = {(symbol.name, symbol.start_line) for symbol in primary}
    for symbol in fallback:
        key = (symbol.name, symbol.start_line)
        if key not in seen:
            merged.append(symbol)
            seen.add(key)
    return sorted(
        merged,
        key=lambda symbol: (symbol.start_line, symbol.end_line, symbol.qualified_name),
    )


def parse_file(
    path: Path, backend: str = "auto", *, cache: SourceContentCache | None = None,
) -> tuple[list[Symbol], str | None, str]:
    language = _language_for(path)
    try:
        if cache is not None:
            source = cache.read_text(path)
        else:
            source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"无法读取: {exc}", language
    if backend not in {"auto", "builtin", "tree-sitter"}:
        return [], f"未知解析后端: {backend}", language
    tree_language = TREE_SITTER_LANGUAGES.get(path.suffix.lower())
    if language == "python":
        symbols, error = _python_symbols(source)
    elif backend in {"auto", "tree-sitter"} and tree_language:
        symbols, error = _tree_sitter_symbols(source, tree_language)
        fallback, _ = (
            _javascript_symbols(source) if language == "javascript"
            else _declaration_symbols(source, language)
        )
        if backend == "auto":
            # Always merge the conservative scanner: tree-sitter currently
            # does not expose top-level variable/arrow declarations uniformly.
            # A syntax error remains visible even when a fallback declaration
            # was recovered; otherwise strict mode would claim false coverage.
            symbols = _merge_parser_symbols(symbols, fallback)
            if error and ("未安装" in error or "无法加载" in error):
                error = None
    elif language == "javascript":
        symbols, error = _javascript_symbols(source)
    else:
        symbols, error = _declaration_symbols(source, language)
    return _deduplicate_scopes(symbols), error, language


def _leaf_symbols(symbols: list[Symbol]) -> list[Symbol]:
    parents = {symbol.parent for symbol in symbols if symbol.parent}
    callable_names = {
        symbol.qualified_name for symbol in symbols
        if symbol.kind in {"function", "method", "async-function", "async-method"}
    }

    def nested_in_callable(symbol: Symbol) -> bool:
        parent = symbol.parent
        while parent:
            if parent in callable_names:
                return True
            parent = parent.rsplit(".", 1)[0] if "." in parent else None
        return False

    return [
        symbol for symbol in symbols
        if not (symbol.kind in {"class", "type"} and symbol.qualified_name in parents)
        and not nested_in_callable(symbol)
    ]


def index_repository(
    root: Path, backend: str = "auto", strict: bool = False,
    *, cache: SourceContentCache | None = None,
) -> dict:
    root = root.resolve()
    cache = cache if cache is not None else SourceContentCache()
    files: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []
    for path in discover_source_paths(root, strict=strict):
        relative = path.relative_to(root).as_posix()
        symbols, error, language = parse_file(path, backend, cache=cache)
        try:
            digest = file_sha256(path, cache)
            size = path.stat().st_size
        except OSError as exc:
            if strict:
                raise SourceDiscoveryError(f"源码读取失败 {path}: {exc}") from exc
            digest = ""
            size = 0
        file_id = f"file:{relative}"
        nodes.append({"id": file_id, "type": "file", "path": relative})
        symbol_payload = []
        for symbol in symbols:
            item = asdict(symbol)
            # Byte offsets are an internal stable disambiguator for colliding
            # AST scopes; they are not part of the public index payload schema.
            item.pop("start_byte", None)
            item["scope"] = f"{relative}::{symbol.scope_name}"
            symbol_payload.append(item)
            symbol_id = f"symbol:{relative}::{symbol.qualified_name}"
            nodes.append({"id": symbol_id, "type": "symbol", "path": relative, **item})
            parent_id = (
                f"symbol:{relative}::{symbol.parent}" if symbol.parent else file_id
            )
            edges.append({"type": "contains", "from": parent_id, "to": symbol_id})
        files.append({
            "path": relative,
            "language": language,
            "parser": symbols[0].parser if symbols else (
                "python-ast" if language == "python" else f"{language}-declaration"
            ),
            "error": error,
            "content_hash": digest,
            "size": size,
            "symbols": symbol_payload,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "root": ".",
        "files": files,
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "files": len(files),
            "symbols": sum(len(item["symbols"]) for item in files),
            "parse_errors": sum(bool(item["error"]) for item in files),
        },
    }


def audit_units(
    root: Path, backend: str = "auto", strict: bool = False,
    *, cache: SourceContentCache | None = None,
) -> list[dict]:
    """返回最细审计单元；有符号时取叶子符号，否则退回文件。"""
    root = root.resolve()
    cache = cache if cache is not None else SourceContentCache()
    result: list[dict] = []
    for path in discover_source_paths(root, strict=strict):
        relative = path.relative_to(root).as_posix()
        symbols, error, language = parse_file(path, backend, cache=cache)
        try:
            digest = file_sha256(path, cache)
        except OSError as exc:
            if strict:
                raise SourceDiscoveryError(f"源码读取失败 {path}: {exc}") from exc
            digest = ""
        leaves = _leaf_symbols(symbols)
        if leaves and all(symbol.kind == "module" for symbol in leaves):
            # A file containing only constants/configuration remains a useful
            # file-level task; do not manufacture a synthetic callable task.
            leaves = []
        if leaves:
            result.extend({
                "scope": f"{relative}::{symbol.scope_name}",
                "path": relative,
                "name": symbol.name,
                "kind": symbol.kind,
                "line": symbol.start_line,
                "parser": symbol.parser,
                "error": error,
                "end_line": symbol.end_line,
                "content_hash": digest,
                "language": language,
            } for symbol in leaves)
        else:
            result.append({
                "scope": relative,
                "path": relative,
                "name": path.stem,
                "kind": "file",
                "line": 1,
                "parser": "fallback-file",
                "error": error,
                "language": language,
                "content_hash": digest,
            })
    return result


def inventory_snapshot(
    root: Path, backend: str = "auto", strict: bool = False,
    *, cache: SourceContentCache | None = None,
) -> dict:
    """Return one hashable inventory for source and behavior-affecting assets."""
    root = root.resolve()
    cache = cache if cache is not None else SourceContentCache()
    source_paths = discover_source_paths(root, strict=strict)
    asset_paths = discover_asset_paths(root, strict=strict)
    records: list[dict] = []
    for path in source_paths:
        relative = path.relative_to(root).as_posix()
        symbols, error, language = parse_file(path, backend, cache=cache)
        records.append({
            "path": relative, "kind": "source", "language": language,
            "content_hash": file_sha256(path, cache), "parse_error": error,
            "scopes": [f"{relative}::{symbol.scope_name}" for symbol in symbols],
        })
    for path in asset_paths:
        records.append({
            "path": path.relative_to(root).as_posix(), "kind": "asset",
            "content_hash": file_sha256(path, cache),
        })
    records.sort(key=lambda item: (item["kind"], item["path"]))
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "files": records,
        "inventory_hash": hashlib.sha256(encoded).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("scan", "units", "inventory"), nargs="?", default="scan",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="任一文件解析失败时返回非零")
    parser.add_argument("--backend", choices=("auto", "builtin", "tree-sitter"),
                        default="auto")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        try:
            payload = inventory_snapshot(args.root, args.backend, strict=args.strict)
        except SourceDiscoveryError as exc:
            print(f"[symbol_index] ✗ {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"[symbol_index] ✗ 项目清单读取失败: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"[symbol_index] 清单文件 {len(payload['files'])}，"
                  f"inventory_hash={payload['inventory_hash']}")
        parse_errors = [item for item in payload["files"] if item.get("parse_error")]
        return 1 if args.strict and parse_errors else 0
    if args.command == "units":
        try:
            units = audit_units(args.root, args.backend, strict=args.strict)
        except SourceDiscoveryError as exc:
            print(f"[symbol_index] ✗ {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "units": units},
                             ensure_ascii=False, indent=2))
        else:
            for unit in units:
                print(f"{unit['scope']}\t{unit['kind']}\tline {unit['line']}")
        parse_errors = sum(bool(unit.get("error")) for unit in units)
        if args.strict and parse_errors:
            return 1
        return 0 if units else 1
    try:
        payload = index_repository(args.root, args.backend, strict=args.strict)
    except SourceDiscoveryError as exc:
        print(f"[symbol_index] ✗ {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = payload["summary"]
        print(f"[symbol_index] 文件 {summary['files']}，符号 {summary['symbols']}，"
              f"解析错误 {summary['parse_errors']}")
        for item in payload["files"]:
            status = (
                f"ERROR: {item['error']}" if item["error"]
                else f"{len(item['symbols'])} symbols"
            )
            print(f"  {item['path']}: {status}")
    return 1 if args.strict and payload["summary"]["parse_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
