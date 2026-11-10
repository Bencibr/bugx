#!/usr/bin/env python3
"""core/spec_harvester.py - BugX 规约多源抽取与源码事实反向反哺引擎 (DOC-245).

职责:
1. 规范资产多源发现与解析 (机器可读 OpenAPI/Swagger/Prisma 优先，Markdown PRD/README/Spec 补充);
2. 提取核心用户旅程 (Core Journeys)、显式设计不变量 (Explicit Invariants) 与排他边界 (Scope Exclusions);
3. 源码事实反向反哺 (Code-to-Spec Inversion): 交叉校准源码路由与 AST，消除陈旧文档造成的盲区;
4. 编译落盘为 .bugx/state/business-spec.json 业务真理账本。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"

# 显式不变量规则词根提取模式 (覆盖身份、尺寸、速率、只读、角色权限与状态机单向约束)
_INVARIANT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:免密|匿名|无需认证|无需鉴权|无需登录|公开|public|allow[_\s-]?anonymous|unauthenticated)", re.IGNORECASE), "ALLOW_ANONYMOUS"),
    (re.compile(r"(?:探活|心跳|健康检查|health[_\s-]?check|ping|liveness|readiness)", re.IGNORECASE), "HEALTHCHECK_ENDPOINT"),
    (re.compile(r"(?:上限|最大|限制|不超过|max[_\s-]?size|limit[_\s-]?to)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?\s*(?:mb|kb|gb|bytes?))", re.IGNORECASE), "SIZE_LIMIT"),
    (re.compile(r"(?:只读|禁止写入|只允许查询|read[_\s-]?only)", re.IGNORECASE), "READ_ONLY"),
    (re.compile(r"(?:速率限制|防刷|限流|rate[_\s-]?limit)\s*[:=]?\s*([0-9]+\s*/\s*[a-zA-Z]+)", re.IGNORECASE), "RATE_LIMITED"),
    (re.compile(r"(?:角色|权限|管理员|admin[_\s-]?only|role[_\s-]?required|rbac|forbidden[_\s-]?for[_\s-]?user)", re.IGNORECASE), "ROLE_PERMISSION"),
    (re.compile(r"(?:单向|不可回退|幂等|idempotent|state[_\s-]?invariant|immutable)", re.IGNORECASE), "STATE_INVARIANT"),
)

# 排他范围模式
_EXCLUSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:非本期|后续支持|暂不支持|未来规划|out\s+of\s+scope|not\s+supported|deprecated|已废弃)", re.IGNORECASE),
)


@dataclass(frozen=True)
class ExplicitInvariant:
    """显式设计不变量规则。"""

    target: str
    rule: str
    source_file: str
    line_number: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessSpec:
    """业务真理账本。"""

    schema_version: str = SCHEMA_VERSION
    core_journeys: list[dict[str, Any]] = field(default_factory=list)
    explicit_invariants: list[ExplicitInvariant] = field(default_factory=list)
    scope_exclusions: list[str] = field(default_factory=list)
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    domain_contexts: dict[str, Any] = field(default_factory=dict)
    source_inversions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "core_journeys": self.core_journeys,
            "explicit_invariants": [inv.to_dict() for inv in self.explicit_invariants],
            "scope_exclusions": self.scope_exclusions,
            "endpoints": self.endpoints,
            "domain_contexts": self.domain_contexts,
            "source_inversions": self.source_inversions,
        }


def _safe_read_text(path: Path, max_bytes: int = 512 * 1024) -> str:
    try:
        if path.is_file() and not path.is_symlink():
            content = path.read_text(encoding="utf-8", errors="replace")
            return content[:max_bytes]
    except (OSError, UnicodeError):
        pass
    return ""


def _parse_openapi_spec(file_path: Path) -> dict[str, Any]:
    """解析 OpenAPI / Swagger 规范资产。"""
    endpoints: list[dict[str, Any]] = []
    invariants: list[ExplicitInvariant] = []
    text = _safe_read_text(file_path)
    if not text:
        return {"endpoints": [], "invariants": []}

    data: dict[str, Any] = {}
    try:
        if file_path.suffix.lower() == ".json":
            data = json.loads(text)
        elif file_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
                data = yaml.safe_load(text) or {}
            except ImportError:
                # 兼容零第三方依赖场景：正则提取简易路由
                for match in re.finditer(r"^\s{2,4}(/[a-zA-Z0-9_{}/-]+):\s*$", text, re.MULTILINE):
                    endpoints.append({"path": match.group(1), "methods": ["GET"], "auth_required": True})
                return {"endpoints": endpoints, "invariants": invariants}
    except Exception:
        return {"endpoints": [], "invariants": []}

    paths = data.get("paths") if isinstance(data, dict) else {}
    if isinstance(paths, dict):
        for path_key, methods_dict in paths.items():
            if not isinstance(methods_dict, dict):
                continue
            for method, details in methods_dict.items():
                if method.lower() not in {"get", "post", "put", "delete", "patch", "options", "head"}:
                    continue
                auth_required = True
                if isinstance(details, dict):
                    security = details.get("security", data.get("security"))
                    if security is not None and len(security) == 0:
                        auth_required = False
                    # 检查是否包含 public 或健康检查标签
                    tags = details.get("tags", [])
                    summary = str(details.get("summary", ""))
                    if not auth_required or "health" in path_key.lower() or "public" in str(tags).lower():
                        invariants.append(
                            ExplicitInvariant(
                                target=str(path_key),
                                rule="ALLOW_ANONYMOUS" if not auth_required else "HEALTHCHECK_ENDPOINT",
                                source_file=str(file_path.name),
                                detail=summary,
                            )
                        )
                endpoints.append({
                    "path": str(path_key),
                    "method": method.upper(),
                    "auth_required": auth_required,
                })

    return {"endpoints": endpoints, "invariants": invariants}


def _parse_markdown_spec(file_path: Path) -> dict[str, Any]:
    """从 Markdown 文档 (PRD / README / DESIGN_SPEC) 抽取不变量与旅程。"""
    invariants: list[ExplicitInvariant] = []
    exclusions: list[str] = []
    text = _safe_read_text(file_path)
    if not text:
        return {"invariants": [], "exclusions": []}

    lines = text.splitlines()
    for idx, line in enumerate(lines, 1):
        line_clean = line.strip()
        if not line_clean:
            continue

        # 检查显式不变量
        for pattern, rule_tag in _INVARIANT_PATTERNS:
            match = pattern.search(line_clean)
            if match:
                # 尝试提取目标端点或实体
                endpoint_match = re.search(r"(/[a-zA-Z0-9_\-\./]+|`[^`]+`)", line_clean)
                target = endpoint_match.group(1).strip("`") if endpoint_match else "GLOBAL"
                detail = match.group(0)
                invariants.append(
                    ExplicitInvariant(
                        target=target,
                        rule=rule_tag,
                        source_file=str(file_path.name),
                        line_number=idx,
                        detail=detail,
                    )
                )

        # 检查排他范围
        for excl_pat in _EXCLUSION_PATTERNS:
            if excl_pat.search(line_clean):
                exclusions.append(f"{file_path.name}:{idx}: {line_clean}")

    return {"invariants": invariants, "exclusions": exclusions}


def _extract_web_routes_from_ast(py_file: Path, project_root: Path) -> list[dict[str, Any]]:
    """从源码 AST 中逆向提取 Web 路由定义 (FastAPI/Flask/Starlette 常见装饰器)。"""
    routes: list[dict[str, Any]] = []
    code_text = _safe_read_text(py_file, max_bytes=256 * 1024)
    if not code_text:
        return routes
    try:
        tree = ast.parse(code_text, filename=str(py_file))
    except SyntaxError:
        return routes

    http_methods = {"get", "post", "put", "delete", "patch", "options", "head"}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for decorator in node.decorator_list:
            route_path = None
            methods = []

            # 形式 1: @app.get("/path"), @router.post("/path")
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                attr_name = decorator.func.attr.lower()
                if attr_name in http_methods:
                    methods.append(attr_name.upper())
                    if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                        route_path = decorator.args[0].value
                elif attr_name == "route":
                    # Flask 形式: @bp.route("/path", methods=["GET", "POST"])
                    if decorator.args and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str):
                        route_path = decorator.args[0].value
                    for kw in decorator.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            for elt in kw.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    methods.append(elt.value.upper())
                    if not methods:
                        methods.append("GET")

            if route_path:
                docstring = ast.get_docstring(node) or ""
                summary = docstring.strip().split("\n")[0] if docstring else node.name
                # 判断鉴权要求：健康检查通常免密，其他默认需鉴权或根据依赖判断
                is_health = any(h in route_path.lower() for h in ("health", "ping", "live", "ready"))
                auth_req = not is_health
                for method in (methods or ["GET"]):
                    routes.append({
                        "path": route_path,
                        "method": method,
                        "auth_required": auth_req,
                        "handler": node.name,
                        "summary": summary,
                        "file": str(py_file.relative_to(project_root)),
                        "lineno": node.lineno,
                    })

    return routes


def _scan_source_code_facts(project_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """扫描源码 AST 与路由事实，用于反哺陈旧文档 (Code-to-Spec Inversion)。"""
    detected_facts: list[dict[str, Any]] = []
    ast_routes: list[dict[str, Any]] = []

    # 扫描常见 python 源码路由与关键认证函数
    for py_file in project_root.rglob("*.py"):
        if any(ignored in py_file.parts for ignored in {".git", ".bugx", "venv", ".venv", "node_modules", "tests"}):
            continue
        code_text = _safe_read_text(py_file, max_bytes=256 * 1024)
        if not code_text:
            continue
        try:
            tree = ast.parse(code_text, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_name = node.name.lower()
                # 识别用户与认证关键函数
                for intent in ("login", "register", "signup", "recover", "reset_password", "verify_token"):
                    if intent in fn_name:
                        detected_facts.append({
                            "type": "function",
                            "name": node.name,
                            "intent": intent,
                            "file": str(py_file.relative_to(project_root)),
                            "lineno": node.lineno,
                            "trust_level": "untrusted_code",
                        })

        # 扫描 Web 路由定义
        routes = _extract_web_routes_from_ast(py_file, project_root)
        ast_routes.extend(routes)

    return detected_facts, ast_routes


def harvest_specification(project_root: Path) -> BusinessSpec:
    """执行多源规约抽取、反向反哺并构建业务真理账本。"""
    spec = BusinessSpec()

    # 1. 扫描机器可读 API 规约
    openapi_candidates = (
        "openapi.yaml", "openapi.yml", "openapi.json",
        "swagger.yaml", "swagger.yml", "swagger.json",
        "docs/openapi.yaml", "docs/swagger.json",
    )
    for rel_path in openapi_candidates:
        candidate = project_root / rel_path
        if candidate.is_file():
            res = _parse_openapi_spec(candidate)
            spec.endpoints.extend(res.get("endpoints", []))
            spec.explicit_invariants.extend(res.get("invariants", []))

    # 2. 扫描权威设计文档与 README
    doc_candidates = (
        "README.md",
        "docs/02_architecture/DESIGN_SPEC.md",
        "docs/02_architecture/ARCHITECTURE.md",
        "docs/02_architecture/BUGX_AUDIT_FLOW.md",
        "docs/03_ai/AI_REFERENCE.md",
    )
    for rel_path in doc_candidates:
        candidate = project_root / rel_path
        if candidate.is_file():
            res = _parse_markdown_spec(candidate)
            spec.explicit_invariants.extend(res.get("invariants", []))
            spec.scope_exclusions.extend(res.get("exclusions", []))

    # 3. 源码事实反哺 (Code-to-Spec Inversion)
    source_facts, ast_routes = _scan_source_code_facts(project_root)
    spec.source_inversions.extend(source_facts)

    # 4. 路由融合 (端点去重合并与隐式不变量提炼)
    existing_endpoints = {(ep.get("path"), ep.get("method")) for ep in spec.endpoints}
    for r in ast_routes:
        key = (r.get("path"), r.get("method"))
        if key not in existing_endpoints:
            spec.endpoints.append(r)
            existing_endpoints.add(key)
            # 对探活/免密端点自动生成不变量
            if not r.get("auth_required", True):
                rule_tag = "HEALTHCHECK_ENDPOINT" if any(h in r.get("path", "").lower() for h in ("health", "ping")) else "ALLOW_ANONYMOUS"
                spec.explicit_invariants.append(
                    ExplicitInvariant(
                        target=r.get("path", ""),
                        rule=rule_tag,
                        source_file=r.get("file", "source_ast"),
                        line_number=r.get("lineno", 0),
                        detail=f"AST 逆向抽取不变量: {r.get('summary', '')}",
                    )
                )

    # 5. 构建核心用户旅程 (Core Journeys)
    # 汇总文档与源码中出现的实体事实
    auth_intents = {fact.get("intent") for fact in source_facts}
    auth_flow_steps = []
    if "register" in auth_intents or "signup" in auth_intents:
        auth_flow_steps.append("register")
    if "login" in auth_intents or any("login" in ep.get("path", "").lower() for ep in spec.endpoints):
        auth_flow_steps.append("login")
    if "recover" in auth_intents or "reset_password" in auth_intents:
        auth_flow_steps.append("password_reset")

    if auth_flow_steps:
        spec.core_journeys.append({
            "name": "identity_lifecycle",
            "domain": "auth",
            "steps": auth_flow_steps,
            "criticality": "HIGH",
        })

    return spec


def export_feature_inventory(spec: BusinessSpec, project_name: str = "Target Project") -> str:
    """反向生成 Markdown 格式的完整功能清单 (Feature Inventory)."""
    lines = [
        f"# {project_name} - 完整功能清单与规约反哺视图 (Feature Inventory)",
        "",
        "> 由 BugX Spec Harvester 逆向源码与规约全量自动提取编译",
        "",
        "## 1. 核心用户旅程 (Core Journeys)",
    ]
    if spec.core_journeys:
        for cj in spec.core_journeys:
            steps_str = " -> ".join(cj.get("steps", []))
            lines.append(f"- **{cj.get('name')}** (领域: `{cj.get('domain')}`, 关键等级: {cj.get('criticality')}): `{steps_str}`")
    else:
        lines.append("- *(未检测到显式旅程)*")

    lines.extend([
        "",
        "## 2. API 端点全量清单 (API Endpoints Inventory)",
        "| Method | Path | Auth Required | Handler | Summary | Source |",
        "| :--- | :--- | :---: | :--- | :--- | :--- |",
    ])
    if spec.endpoints:
        for ep in spec.endpoints:
            m = ep.get("method", "GET")
            p = ep.get("path", "")
            auth = "✓ 是" if ep.get("auth_required", True) else "✗ 否 (公开/探活)"
            handler = ep.get("handler", "-")
            summary = ep.get("summary", "-")
            src = ep.get("file", "openapi")
            lines.append(f"| `{m}` | `{p}` | {auth} | `{handler}` | {summary} | `{src}` |")
    else:
        lines.append("| - | - | - | - | - | - |")

    lines.extend([
        "",
        "## 3. 显式设计不变量 (Explicit Invariants)",
    ])
    if spec.explicit_invariants:
        for inv in spec.explicit_invariants:
            lines.append(f"- **[{inv.rule}]** 目标: `{inv.target}` | 来源: `{inv.source_file}:{inv.line_number}` | 详情: {inv.detail}")
    else:
        lines.append("- *(无显式不变量)*")

    return "\n".join(lines)


def save_business_spec(project_root: Path, spec: BusinessSpec) -> Path:
    """原子落盘 business-spec.json 账本与 feature-inventory.md 清单。"""
    state_dir = project_root / ".bugx" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "business-spec.json"
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(spec.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(out_path)

    # 同步落盘人类可读的 Markdown 功能清单
    inv_md = export_feature_inventory(spec, project_name=project_root.name)
    inv_path = state_dir / "feature-inventory.md"
    tmp_inv = inv_path.with_suffix(".tmp")
    tmp_inv.write_text(inv_md, encoding="utf-8")
    tmp_inv.replace(inv_path)

    return out_path


def load_business_spec(project_root: Path) -> BusinessSpec | None:
    """安全读取 business-spec.json 账本。"""
    spec_path = project_root / ".bugx" / "state" / "business-spec.json"
    if not spec_path.is_file():
        return None
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        invariants = [
            ExplicitInvariant(**inv) if isinstance(inv, dict) else inv
            for inv in data.get("explicit_invariants", [])
        ]
        return BusinessSpec(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            core_journeys=data.get("core_journeys", []),
            explicit_invariants=invariants,
            scope_exclusions=data.get("scope_exclusions", []),
            endpoints=data.get("endpoints", []),
            domain_contexts=data.get("domain_contexts", []),
            source_inversions=data.get("source_inversions", []),
        )
    except Exception:
        return None
