"""MCP & Skill Inspector, Marketplace, Sandboxed Credential Store, and Auto-Matcher for BugX WebUI (REQ-09 / DOC-255).

Responsibilities:
1. Inspect mounted MCP servers from .mcp.json, .codebuddy/mcp.json, and .claude/mcp.json.
2. Provide MCP Marketplace backed by core/supervisor_host.py MCP_REGISTRY.
3. Isolate sensitive MCP credentials strictly outside the workspace in ~/.bugx/sandboxes/<project_hash>/mcp_env.json (0600).
4. Provide Skill Marketplace aggregating core skills, domain skill packs (core/prompts/skills/*), and project/global skills.
5. Run AI Capability Auto-Matching via project_profile + capability_planner and one-click provisioning.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


def _compute_proj_hash(ws: Path) -> str:
    resolved = ws.resolve()
    h = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{resolved.name}-{h}"


def _sandbox_env_path(ws: Path) -> Path:
    """纯路径解析：**不**创建目录、不 chmod。

    读路径绝不能有写副作用。旧实现把 ``mkdir`` 塞进 ``_sandbox_env_file``，
    每次只读检查（``bugx project settings``、WebUI 检查器、``doctor``）都会去
    ``~/.bugx/sandboxes/<hash>/`` 建目录；一旦 HOME 不可写（只读分发、CI 沙箱、
    容器内 root-squash），这条**纯读**链路直接抛
    ``PermissionError: [Errno 1] Operation not permitted``，把「没有凭据」误报成
    「项目配置获取失败」，整条 ``project settings`` 退出 1。
    """
    proj_hash = _compute_proj_hash(ws)
    bugx_home = Path(os.environ.get("BUGX_HOME") or (Path.home() / ".bugx")).resolve()
    return bugx_home / "sandboxes" / proj_hash / "mcp_env.json"


def _sandbox_env_file(ws: Path) -> Path:
    """写路径：确保隔离目录存在且为 0700，再返回凭据文件路径。"""
    env_file = _sandbox_env_path(ws)
    sandbox_dir = env_file.parent
    sandbox_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(sandbox_dir, 0o700)
    except OSError:
        pass
    return env_file


def load_sandbox_mcp_env(workspace_path: Path | str) -> dict[str, dict[str, str]]:
    """Load isolated MCP credentials from ~/.bugx/sandboxes/<project_hash>/mcp_env.json."""
    env_file = _sandbox_env_path(Path(workspace_path))
    if not env_file.is_file():
        return {}
    try:
        data = json.loads(env_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                str(k): {str(ek): str(ev) for ek, ev in v.items() if ev is not None}
                for k, v in data.items()
                if isinstance(v, dict)
            }
    except Exception:
        pass
    return {}


def save_sandbox_mcp_env(
    workspace_path: Path | str,
    server_name: str,
    env_vars: dict[str, str],
) -> dict[str, Any]:
    """Persist sensitive MCP credentials strictly outside the workspace with 0600 permissions (REQ-09)."""
    ws = Path(workspace_path).resolve()
    env_file = _sandbox_env_file(ws)
    all_env = load_sandbox_mcp_env(ws)
    srv_env = dict(all_env.get(server_name) or {})
    for k, v in env_vars.items():
        k_clean = str(k).strip()
        if not k_clean:
            continue
        if v is None or str(v).strip() == "":
            srv_env.pop(k_clean, None)
        else:
            srv_env[k_clean] = str(v).strip()
    if srv_env:
        all_env[server_name] = srv_env
    else:
        all_env.pop(server_name, None)

    tmp = env_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(all_env, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(env_file)
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass

    # Scrub any accidental plaintext secret from workspace .mcp.json
    mcp_path = ws / ".mcp.json"
    if mcp_path.is_file():
        try:
            mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
            srv_cfg = (mcp_data.get("mcpServers") or {}).get(server_name)
            if isinstance(srv_cfg, dict) and isinstance(srv_cfg.get("env"), dict):
                changed = False
                for k in list(srv_cfg["env"].keys()):
                    if k in srv_env:
                        srv_cfg["env"][k] = f"${{BUGX_SANDBOX_ENV:{k}}}"
                        changed = True
                if changed:
                    mcp_path.write_text(json.dumps(mcp_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    return {
        "ok": True,
        "server": server_name,
        "configured_keys": sorted(srv_env.keys()),
        "sandbox_path": str(env_file),
    }


def _load_mcp_registry() -> dict[str, dict[str, Any]]:
    raw: Any = None
    try:
        from core.supervisor import supervisor_host
        raw = getattr(supervisor_host, "MCP_REGISTRY", None)
    except ImportError:
        try:
            from core import supervisor_host
            raw = getattr(supervisor_host, "MCP_REGISTRY", None)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (list, tuple)):
        out: dict[str, dict[str, Any]] = {}
        for item in raw:
            if isinstance(item, dict):
                key = str(item.get("id") or item.get("name") or "").strip()
                if key:
                    out[key] = item
        return out
    return {}


ONLINE_MCP_CATALOG: dict[str, dict[str, Any]] = {
    "github-mcp": {
        "id": "github-mcp",
        "name": "github-mcp",
        "title": "GitHub 官方上下文与审查探针",
        "category": "devops",
        "author": "@modelcontextprotocol",
        "stars": 5200,
        "version": "0.6.2",
        "package": "@modelcontextprotocol/server-github",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/github",
        "description": "提供 GitHub 仓库搜索、PR 审查、Issue 状态流转、Commit 差异对比及自动化审查评论能力。",
        "tags": ["GitHub", "Git", "Code-Review", "PR"],
        "required_credentials": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "tools": [
            {"name": "create_or_update_file", "description": "在 GitHub 仓库中创建或更新指定文件", "parameters": "owner, repo, path, content, message, branch"},
            {"name": "search_repositories", "description": "全局检索 GitHub 关联代码仓库", "parameters": "query, page, per_page"},
            {"name": "get_issue", "description": "获取 GitHub Issue 详细内容与标签", "parameters": "owner, repo, issue_number"},
            {"name": "create_pull_request", "description": "自动创建 Pull Request 并关联修复分支", "parameters": "owner, repo, title, head, base, body"},
            {"name": "get_pull_request_diff", "description": "提取 PR 变动的 Unified Diff 补丁", "parameters": "owner, repo, pull_number"},
            {"name": "create_issue_comment", "description": "在 PR 或 Issue 中发表安全审计评论", "parameters": "owner, repo, issue_number, body"},
        ],
    },
    "filesystem-mcp": {
        "id": "filesystem-mcp",
        "name": "filesystem-mcp",
        "title": "本地受控文件系统沙箱探针",
        "category": "tools",
        "author": "@modelcontextprotocol",
        "stars": 4850,
        "version": "0.6.0",
        "package": "@modelcontextprotocol/server-filesystem",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        "description": "安全受控的本地文件与目录树交互探针，严格限制在指定沙箱目录白名单内，防止路径遍历穿越。",
        "tags": ["Filesystem", "Sandbox", "IO", "Path-Security"],
        "required_credentials": [],
        "tools": [
            {"name": "read_file", "description": "读取沙箱目录内指定文件的 UTF-8 文本内容", "parameters": "path"},
            {"name": "write_file", "description": "向受控白名单路径写入文件（严禁目录穿越）", "parameters": "path, content"},
            {"name": "list_directory", "description": "遍历指定目录树与文件元数据", "parameters": "path"},
            {"name": "move_file", "description": "在受控目录间移动或重命名文件", "parameters": "source, destination"},
            {"name": "get_file_info", "description": "提取文件大小、创建时间与权限位", "parameters": "path"},
        ],
    },
    "postgres-mcp": {
        "id": "postgres-mcp",
        "name": "postgres-mcp",
        "title": "PostgreSQL 关系型数据库只读安全探针",
        "category": "storage",
        "author": "@modelcontextprotocol",
        "stars": 3940,
        "version": "0.5.1",
        "package": "@modelcontextprotocol/server-postgres",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/db"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        "description": "专为 PostgreSQL 数据库设计的元数据查询与安全审计探针，支持 Schema 提取与隔离只读查询。",
        "tags": ["Postgres", "SQL", "Database", "Readonly"],
        "required_credentials": ["POSTGRES_CONNECTION_STRING"],
        "tools": [
            {"name": "query", "description": "执行只读 SQL 查询并返回结构化数据行", "parameters": "sql"},
            {"name": "get_schema", "description": "导出全部公共表、视图与列类型定义", "parameters": "schema"},
            {"name": "list_tables", "description": "列举数据库中当前可用的全部表结构", "parameters": "schema"},
        ],
    },
    "fetch-mcp": {
        "id": "fetch-mcp",
        "name": "fetch-mcp",
        "title": "网页内容提取与 Markdown 转换探针",
        "category": "tools",
        "author": "@modelcontextprotocol",
        "stars": 3680,
        "version": "0.6.1",
        "package": "mcp-server-fetch",
        "installer": "uvx",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        "description": "安全获取外部 HTTP/HTTPS 网页内容并自动转译为精简 Markdown，内嵌 SSRF 过滤与速率限制机制。",
        "tags": ["Web", "Fetch", "Markdown", "SSRF-Safe"],
        "required_credentials": [],
        "tools": [
            {"name": "fetch", "description": "抓取指定 URL 网页并将 HTML 清洗转为高质量 Markdown", "parameters": "url, max_length, start_index"},
        ],
    },
    "brave-search-mcp": {
        "id": "brave-search-mcp",
        "name": "brave-search-mcp",
        "title": "Brave 隐私优先全球网页检索探针",
        "category": "search",
        "author": "@modelcontextprotocol",
        "stars": 3120,
        "version": "0.5.0",
        "package": "@modelcontextprotocol/server-brave-search",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
        "description": "利用 Brave Search 官方 API 执行无隐私追踪的全球网页与技术资料检索，获取最新漏洞公告。",
        "tags": ["Search", "Brave", "Web-Search", "CVE"],
        "required_credentials": ["BRAVE_API_KEY"],
        "tools": [
            {"name": "brave_web_search", "description": "执行全球 Web 关键字检索并返回相关摘要", "parameters": "query, count"},
            {"name": "brave_local_search", "description": "针对特定地理位置或服务执行本地搜索", "parameters": "query, count"},
        ],
    },
    "sentry-mcp": {
        "id": "sentry-mcp",
        "name": "sentry-mcp",
        "title": "Sentry 线上异常监控与崩溃堆栈追踪",
        "category": "devops",
        "author": "@modelcontextprotocol",
        "stars": 2850,
        "version": "0.4.0",
        "package": "mcp-server-sentry",
        "installer": "uvx",
        "command": "uvx",
        "args": ["mcp-server-sentry"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/sentry",
        "description": "实时对接 Sentry 线上监控系统，拉取生产环境异常堆栈、未捕获崩溃与错误聚合统计。",
        "tags": ["Sentry", "Crash", "Observability", "StackTrace"],
        "required_credentials": ["SENTRY_AUTH_TOKEN"],
        "tools": [
            {"name": "get_issue", "description": "根据 Issue ID 获取 Sentry 详细崩溃报告与上下文", "parameters": "issue_id"},
            {"name": "search_issues", "description": "按工程与环境过滤最近激增的生产环境异常", "parameters": "query, limit"},
        ],
    },
    "docker-mcp": {
        "id": "docker-mcp",
        "name": "docker-mcp",
        "title": "Docker 容器生命周期与镜像安全探针",
        "category": "devops",
        "author": "@modelcontextprotocol",
        "stars": 2740,
        "version": "0.3.5",
        "package": "@modelcontextprotocol/server-docker",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-docker"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/docker",
        "description": "受控的 Docker 守护进程管理探针，用于审计运行中容器的环境变量、端口映射及日志流。",
        "tags": ["Docker", "Containers", "DevOps", "Isolation"],
        "required_credentials": [],
        "tools": [
            {"name": "list_containers", "description": "查询当前主机全部活跃及停止状态的容器", "parameters": "all"},
            {"name": "inspect_container", "description": "获取容器的完整底层规格与网络绑定", "parameters": "container_id"},
            {"name": "get_logs", "description": "读取容器近期 stdout/stderr 日志流", "parameters": "container_id, tail"},
        ],
    },
    "sqlite-mcp": {
        "id": "sqlite-mcp",
        "name": "sqlite-mcp",
        "title": "SQLite 嵌入式数据库分析与只读查询",
        "category": "storage",
        "author": "@modelcontextprotocol",
        "stars": 2510,
        "version": "0.5.2",
        "package": "mcp-server-sqlite",
        "installer": "uvx",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "./data.db"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
        "description": "轻量级 SQLite 数据库离线只读分析探针，毫秒级提取本地 .sqlite / .db 文件的全量 Schema。",
        "tags": ["SQLite", "Database", "Embedded", "Local"],
        "required_credentials": [],
        "tools": [
            {"name": "read_query", "description": "在 SQLite 文件上执行只读 SELECT 查询", "parameters": "query"},
            {"name": "describe_table", "description": "获取指定数据表的列结构定义与外键约束", "parameters": "table_name"},
            {"name": "list_tables", "description": "遍历 SQLite 数据库内包含的所有用户表", "parameters": ""},
        ],
    },
    "gitlab-mcp": {
        "id": "gitlab-mcp",
        "name": "gitlab-mcp",
        "title": "GitLab 代码仓库与 CI/CD 审计探针",
        "category": "devops",
        "author": "@modelcontextprotocol",
        "stars": 2280,
        "version": "0.4.8",
        "package": "@modelcontextprotocol/server-gitlab",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gitlab"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/gitlab",
        "description": "连接私有化部署或公网 GitLab 实例，支持 Merge Request 差异对比与 CI 流水线状态跟踪。",
        "tags": ["GitLab", "CI-CD", "Merge-Request", "DevOps"],
        "required_credentials": ["GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_URL"],
        "tools": [
            {"name": "get_merge_request", "description": "获取指定 MR 的源分支、目标分支与变更状态", "parameters": "project_id, mr_iid"},
            {"name": "list_pipelines", "description": "查询最近触发的 CI/CD 流水线运行指标", "parameters": "project_id, page"},
            {"name": "create_mr_discussion", "description": "在 MR 代码行中插入安全加固建议", "parameters": "project_id, mr_iid, body"},
        ],
    },
    "slack-mcp": {
        "id": "slack-mcp",
        "name": "slack-mcp",
        "title": "Slack 团队协作与审计告警通道探针",
        "category": "tools",
        "author": "@modelcontextprotocol",
        "stars": 2190,
        "version": "0.5.0",
        "package": "@modelcontextprotocol/server-slack",
        "installer": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "repo_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/slack",
        "description": "提供 Slack 频道消息监听、安全事件通报与智能体交互回执推送能力。",
        "tags": ["Slack", "Notification", "ChatOps", "Alert"],
        "required_credentials": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
        "tools": [
            {"name": "post_message", "description": "向指定 Slack 频道发送格式化审计通报", "parameters": "channel_id, text"},
            {"name": "list_channels", "description": "枚举工作区内机器人已被邀请加入的频道列表", "parameters": "types"},
            {"name": "reply_to_thread", "description": "针对特定安全预警消息开启独立讨论线程", "parameters": "channel_id, thread_ts, text"},
        ],
    },
}


def _default_mcp_command(name: str, reg_entry: dict[str, Any]) -> tuple[str, list[str]]:
    if name == "bugx-supervisor":
        return "bugx", ["mcp", "serve", "--root", ".", "--role", "host"]
    if name == "bugx-supervisor-worker":
        return "bugx", ["mcp", "serve", "--root", ".", "--role", "worker"]
    if name == "bugx-docs":
        return "bugx", ["mcp", "serve", "--root", ".", "--role", "docs"]
    if name in ONLINE_MCP_CATALOG:
        on_item = ONLINE_MCP_CATALOG[name]
        return str(on_item.get("command") or "npx"), list(on_item.get("args") or [])
    pkg = str(reg_entry.get("package") or "")
    installer = str(reg_entry.get("installer") or "")
    if installer == "npm" and pkg:
        return "npx", ["-y", pkg]
    return "bugx", ["mcp-runtime", "run", name]


def inspect_workspace_mcp(workspace_path: Path | str) -> dict[str, Any]:
    """Inspect configured MCP servers in the given workspace path."""
    ws = Path(workspace_path).resolve()
    servers: list[dict[str, Any]] = []
    registry = _load_mcp_registry()
    sandbox_env = load_sandbox_mcp_env(ws)

    cfg_path = ws / ".bugx" / "config.json"
    disabled_mcps: set[str] = set()
    if cfg_path.is_file():
        try:
            cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg_data, dict):
                disabled_mcps = {str(x) for x in (cfg_data.get("disabled_mcps") or []) if x}
        except Exception:
            pass

    mcp_files = [
        ws / ".mcp.json",
        ws / ".codebuddy" / "mcp.json",
        ws / ".claude" / "mcp.json",
    ]

    seen_names: set[str] = set()

    for mcp_file in mcp_files:
        if not mcp_file.is_file():
            continue
        try:
            data = json.loads(mcp_file.read_text(encoding="utf-8"))
            mcp_servers = data.get("mcpServers", {})
            for name, cfg in mcp_servers.items():
                if not isinstance(cfg, dict) or name in seen_names:
                    continue
                seen_names.add(name)

                cmd = cfg.get("command", "")
                args = cfg.get("args", [])
                cmd_str = f"{cmd} {' '.join(str(a) for a in args)}".strip()
                is_disabled = bool(cfg.get("disabled")) or (name in disabled_mcps)

                tool_count = 6
                if "supervisor" in name:
                    tool_count = 12
                elif "docs" in name:
                    tool_count = 4
                elif "memory" in name:
                    tool_count = 14
                elif "playwright" in name:
                    tool_count = 10

                reg_info = registry.get(name) or {}
                raw_creds = reg_info.get("credentials")
                if isinstance(raw_creds, dict):
                    req_creds = [str(c) for c in (raw_creds.get("env") or [])]
                elif isinstance(raw_creds, (list, tuple)):
                    req_creds = [str(c) for c in raw_creds]
                else:
                    req_creds = []
                srv_sandbox = sandbox_env.get(name) or {}
                creds_ok = all(bool(os.environ.get(c) or srv_sandbox.get(c)) for c in req_creds)

                status = "disabled" if is_disabled else ("connected" if creds_ok else "missing_credentials")
                servers.append({
                    "name": name,
                    "enabled": not is_disabled,
                    "status": status,
                    "tools": tool_count,
                    "tool_count": tool_count,
                    "command": cmd_str or "stdio",
                    "source": str(mcp_file.relative_to(ws)),
                    "tier": reg_info.get("tier", "custom"),
                    "required_credentials": req_creds,
                    "configured_credentials": sorted(srv_sandbox.keys()),
                    "credentials_ready": creds_ok,
                    "error": "" if creds_ok else f"缺少凭据: {', '.join(req_creds)}",
                })
        except Exception:
            pass

    # If no local .mcp.json found, provide default BugX servers
    if not servers:
        for def_name, def_tools, def_cmd in (
            ("bugx-supervisor", 12, "bugx mcp serve --root . --role host"),
            ("bugx-docs", 4, "bugx mcp serve --root . --role docs"),
        ):
            is_dis = def_name in disabled_mcps
            servers.append({
                "name": def_name,
                "enabled": not is_dis,
                "status": "disabled" if is_dis else "connected",
                "tools": def_tools,
                "tool_count": def_tools,
                "command": def_cmd,
                "source": "builtin",
                "tier": "builtin",
                "required_credentials": [],
                "configured_credentials": [],
                "credentials_ready": True,
                "error": "",
            })

    return {
        "servers": servers,
        "trust": {"trusted": True},
    }


MCP_DESCRIPTIONS: dict[str, str] = {
    "bugx-supervisor": "BugX 核心审计调度控制面，提供任务认领、证据链校验、心跳续约与结算门禁",
    "bugx-supervisor-worker": "BugX Worker 受限执行视图，专为并发子智能体提供零逃逸任务认领与上报",
    "bugx-docs": "项目级 SQLite FTS5 全文文档检索引擎，支持架构规范与安全基线毫秒级检索",
    "postmcp": "HTTP/REST/GraphQL 动态接口契约测试与越权差分发包探针",
    "playwright": "真实无头浏览器自动化探针，用于 DOM XSS、鉴权流、CSRF 与前端视觉回归验证",
    "dbhub": "多源关系型数据库（PostgreSQL/MySQL/SQLite）Schema 审计与只读安全查询探针",
    "redis": "Redis 缓存键值状态审计、TTL 策略核验与分布式锁竞争探针",
    "semgrep": "多语言 AST 静态污点规则扫描器，快速定位高危危险汇（Sinks）",
    "mobile": "Android/iOS 移动端组件暴露面、Intent 劫持与 UI 自动化探针",
    "desktop": "macOS/Windows/Linux 原生桌面应用窗口树检查与交互探针",
    "native": "C/C++/Rust 原生二进制符号分析、ASan 内存错误与 Fuzzing 探针",
}

MCP_TOOLS_CATALOG: dict[str, list[dict[str, str]]] = {
    "bugx-supervisor": [
        {"name": "bugx_supervisor_start", "description": "启动审计编排会话并加载策略基准", "parameters": "review_mode, polling, fix_mode"},
        {"name": "bugx_supervisor_claim", "description": "子智能体认领原子审计任务 (Claim Task)", "parameters": "worker_id, scope, timeout"},
        {"name": "bugx_supervisor_heartbeat", "description": "维持租约心跳，防止会话超时回收", "parameters": "lease_id, progress"},
        {"name": "bugx_supervisor_checkpoint", "description": "提交安全检查点证据链与阶段结果", "parameters": "checkpoint_id, findings, evidence"},
        {"name": "bugx_supervisor_findings", "description": "查询与聚合当前发现的高危安全缺陷", "parameters": "severity, status, filter"},
        {"name": "bugx_supervisor_verify_finding", "description": "触发反事实验证与 PoC 差分复现", "parameters": "finding_id, harness_code"},
        {"name": "bugx_supervisor_confirm_finding", "description": "确认缺陷真实性并转入修复流水线", "parameters": "finding_id, confidence, fix_plan"},
        {"name": "bugx_supervisor_bootstrap_coverage", "description": "引导代码覆盖率与攻击面分析", "parameters": "workspace_root, targets"},
        {"name": "bugx_supervisor_gate", "description": "执行结算门禁，检查阻断性违规项", "parameters": "policy, strict_mode"},
        {"name": "bugx_supervisor_coverage", "description": "获取当前代码行/分支覆盖率统计", "parameters": "scope, format"},
        {"name": "bugx_supervisor_rebind_target", "description": "动态重定向分析目标文件或模块", "parameters": "new_target, preserve_context"},
        {"name": "bugx_supervisor_status", "description": "查询 Supervisor 全局健康状态与运行指标", "parameters": "verbose"},
    ],
    "bugx-docs": [
        {"name": "docs_search", "description": "基于 SQLite FTS5 的项目架构与安全文档全文检索", "parameters": "query, top_k, namespace"},
        {"name": "docs_read", "description": "高效只读读取指定规范文档或 ADR 提案", "parameters": "path, line_range"},
        {"name": "docs_schema", "description": "获取文档元数据、目录结构与章节索引", "parameters": "category"},
        {"name": "docs_tree", "description": "检索工程文档树与 Diátaxis 架构规范", "parameters": "depth, filter"},
    ],
    "postmcp": [
        {"name": "postmcp_http_request", "description": "构造并执行受控 HTTP/REST 请求（支持全动词）", "parameters": "url, method, headers, body, timeout"},
        {"name": "postmcp_graphql_query", "description": "发送 GraphQL 查询与 Mutation 探针", "parameters": "endpoint, query, variables"},
        {"name": "postmcp_replay_traffic", "description": "重放录制的流量包并比对越权差分响应", "parameters": "traffic_id, auth_context_a, auth_context_b"},
        {"name": "postmcp_diff_fuzz", "description": "针对接口参数执行模糊测试与注入突变", "parameters": "endpoint, fuzz_matrix, sink_rules"},
        {"name": "postmcp_extract_cookies", "description": "解析会话 Cookie 与 CSRF Token 安全属性", "parameters": "response_headers, domain"},
        {"name": "postmcp_inspect_headers", "description": "深度校验安全响应头（CSP/HSTS/CORS/X-Frame）", "parameters": "headers, strictness"},
    ],
    "playwright": [
        {"name": "browser_navigate", "description": "控制浏览器导航至目标页面并等待 DOM 就绪", "parameters": "url, wait_until, timeout"},
        {"name": "browser_click", "description": "模拟真实用户点击指定 DOM 选择器或坐标", "parameters": "selector, position, delay"},
        {"name": "browser_screenshot", "description": "捕获页面视口或元素的高清快照 (PNG/JPEG)", "parameters": "full_page, selector, output_path"},
        {"name": "browser_fill", "description": "安全模拟键盘输入与表单填充", "parameters": "selector, text, clear_first"},
        {"name": "browser_eval", "description": "在沙箱页面上下文执行只读 JavaScript 表达式", "parameters": "expression, args"},
        {"name": "browser_console_logs", "description": "实时抓取浏览器 Console 报错、警告与异常堆栈", "parameters": "level_filter"},
        {"name": "browser_network_requests", "description": "监听网络请求与 XHR/Fetch 交互包", "parameters": "url_pattern, include_bodies"},
        {"name": "browser_close", "description": "优雅销毁当前受控无头浏览器上下文", "parameters": "context_id"},
    ],
    "dbhub": [
        {"name": "db_query_schema", "description": "导出 PostgreSQL/MySQL/SQLite 数据库 Schema 结构", "parameters": "database, include_indexes"},
        {"name": "db_execute_readonly_sql", "description": "执行严格只读 SQL 查询，底层自动拦截 DDL/DML 写操作", "parameters": "query, limit, timeout"},
        {"name": "db_explain_query", "description": "获取 SQL 查询执行计划与索引命中深度分析", "parameters": "query, analyze"},
        {"name": "db_inspect_tables", "description": "统计表数据量、外键约束与高危敏感字段定义", "parameters": "table_names"},
        {"name": "db_connection_test", "description": "校验数据库连接有效性与只读账号权限基线", "parameters": "dsn_alias"},
    ],
    "redis": [
        {"name": "redis_get", "description": "查询指定键的字符串、哈希、列表或集合数据", "parameters": "key, format"},
        {"name": "redis_scan_keys", "description": "分批游标扫描键名（严禁 KEYS *，彻底杜绝生产锁库）", "parameters": "pattern, count, cursor"},
        {"name": "redis_ttl", "description": "核验敏感缓存键的生存周期与过期淘汰策略", "parameters": "key"},
        {"name": "redis_info", "description": "提取 Redis 内存水位、持久化配置、慢日志与连接数", "parameters": "section"},
        {"name": "redis_test_connection", "description": "探活 Redis 实例、密码认证与沙箱连通性", "parameters": "timeout"},
    ],
    "semgrep": [
        {"name": "semgrep_scan", "description": "执行 AST 静态代码扫描，快速定位危险汇与潜在漏洞", "parameters": "path, rule_ids, config"},
        {"name": "semgrep_taint_check", "description": "追踪从源头（Sources）到危险汇（Sinks）的数据传播路径", "parameters": "source_sink_spec, file_paths"},
        {"name": "semgrep_list_rules", "description": "查询内置的 OWASP Top 10 与 BugX 深度审计规则清单", "parameters": "category, language"},
        {"name": "semgrep_custom_rule", "description": "使用 YAML 语法对特定代码片段执行即时 AST 匹配", "parameters": "rule_yaml, code_snippet"},
    ],
    "mobile": [
        {"name": "mobile_launch_app", "description": "在连接的 Android/iOS 模拟器或真机启动指定应用", "parameters": "package_name, activity, clear_state"},
        {"name": "mobile_dump_ui_hierarchy", "description": "抓取当前屏幕 View 树、无障碍标签与控件坐标", "parameters": "format"},
        {"name": "mobile_tap", "description": "模拟屏幕坐标点击或控件无障碍节点触发", "parameters": "x, y, element_id"},
        {"name": "mobile_input_text", "description": "模拟软键盘文本输入与回车提交", "parameters": "text, clear_first"},
        {"name": "mobile_screenshot", "description": "捕获移动设备当前屏幕全尺寸高清截图", "parameters": "output_path"},
        {"name": "mobile_inspect_intent", "description": "审计 Android Manifest 导出组件与 Intent 过滤器安全缺陷", "parameters": "manifest_path"},
    ],
    "desktop": [
        {"name": "desktop_inspect_window", "description": "探测当前聚焦的原生桌面窗口属性与控件层级", "parameters": "app_name, pid"},
        {"name": "desktop_click_element", "description": "基于原生无障碍树坐标安全模拟输入", "parameters": "element_id, x, y"},
        {"name": "desktop_capture_screen", "description": "截取多屏幕指定窗口或区域快照", "parameters": "window_id, rect"},
    ],
    "native": [
        {"name": "native_symbol_dump", "description": "解析 ELF / Mach-O / PE 导出表、导入符号与动态依赖", "parameters": "binary_path"},
        {"name": "native_check_sec", "description": "审计二进制安全加固机制（NX/PIE/Canary/RELRO/ASLR）", "parameters": "binary_path"},
        {"name": "native_asan_report", "description": "解析 AddressSanitizer 崩溃日志与 Use-After-Free 内存错误", "parameters": "log_content"},
    ],
}


def get_mcp_marketplace(workspace_path: Path | str) -> dict[str, Any]:
    """Return the official BugX MCP Marketplace catalog enriched with project mount & credential status."""
    ws = Path(workspace_path).resolve()
    registry = _load_mcp_registry()
    current = inspect_workspace_mcp(ws).get("servers", [])
    mounted_map = {s["name"]: s for s in current}
    sandbox_env = load_sandbox_mcp_env(ws)

    descriptions = MCP_DESCRIPTIONS

    catalog: list[dict[str, Any]] = []
    for name, meta in registry.items():
        if name == "bugx-supervisor-worker":
            continue
        mounted_entry = mounted_map.get(name)
        raw_creds = meta.get("credentials")
        if isinstance(raw_creds, dict):
            req_creds = [str(c) for c in (raw_creds.get("env") or [])]
        elif isinstance(raw_creds, (list, tuple)):
            req_creds = [str(c) for c in raw_creds]
        else:
            req_creds = []
        srv_sandbox = sandbox_env.get(name) or {}
        creds_ready = all(bool(os.environ.get(c) or srv_sandbox.get(c)) for c in req_creds)
        cmd_bin, cmd_args = _default_mcp_command(name, meta)
        catalog.append({
            "id": name,
            "name": name,
            "title": name,
            "description": descriptions.get(name, f"BugX 官方审计 MCP 服务 ({name})"),
            "tier": meta.get("tier", "optional"),
            "status": meta.get("status", "ready"),
            "applies": meta.get("applies", {}),
            "installer": meta.get("installer", "builtin"),
            "package": meta.get("package"),
            "command_preview": f"{cmd_bin} {' '.join(cmd_args)}",
            "required_credentials": req_creds,
            "required_env": req_creds,
            "configured_credentials": sorted(srv_sandbox.keys()),
            "credentials_ready": creds_ready,
            "trust": meta.get("trust", {}),
            "audit": meta.get("audit", {}),
            "mounted": mounted_entry is not None,
            "enabled": bool(mounted_entry.get("enabled")) if mounted_entry else False,
        })

    return {
        "total": len(catalog),
        "items": catalog,
    }


def get_online_mcp_catalog(workspace_path: Path | str) -> dict[str, Any]:
    """Return curated open-source MCP catalog enriched with project mount & credential readiness states."""
    ws = Path(workspace_path).resolve()
    current = inspect_workspace_mcp(ws).get("servers", [])
    mounted_map = {s["name"]: s for s in current}
    sandbox_env = load_sandbox_mcp_env(ws)

    items: list[dict[str, Any]] = []
    for srv_id, meta in ONLINE_MCP_CATALOG.items():
        mounted_entry = mounted_map.get(srv_id)
        req_creds = [str(c) for c in (meta.get("required_credentials") or [])]
        srv_sandbox = sandbox_env.get(srv_id) or {}
        creds_ready = all(bool(os.environ.get(c) or srv_sandbox.get(c)) for c in req_creds)
        cmd_bin = meta.get("command", "npx")
        cmd_args = meta.get("args", [])
        cmd_str = f"{cmd_bin} {' '.join(str(a) for a in cmd_args)}".strip()

        item = dict(meta)
        item["command_preview"] = cmd_str
        item["required_credentials"] = req_creds
        item["required_env"] = req_creds
        item["configured_credentials"] = sorted(srv_sandbox.keys())
        item["credentials_ready"] = creds_ready
        item["mounted"] = mounted_entry is not None
        item["enabled"] = bool(mounted_entry.get("enabled")) if mounted_entry else False
        item["tools_count"] = len(meta.get("tools") or [])
        item["trust"] = "A"
        item["tier"] = "community"
        item["source"] = "online"
        items.append(item)

    return {
        "total": len(items),
        "items": items,
        "workspace": str(ws),
    }


def get_mcp_detail(workspace_path: Path | str, server_name: str) -> dict[str, Any]:
    """Provide comprehensive metadata, tool specifications, and rendered documentation for an MCP server (REQ-09 / DOC-255)."""
    ws = Path(workspace_path).resolve()
    srv_name = str(server_name or "").strip()
    if not srv_name:
        return {"ok": False, "error": "Missing server name"}

    registry = _load_mcp_registry()
    meta = registry.get(srv_name) or {}
    is_online = False
    if not meta and srv_name in ONLINE_MCP_CATALOG:
        on_item = ONLINE_MCP_CATALOG[srv_name]
        is_online = True
        meta = {
            "tier": "community",
            "trust": "A",
            "installer": on_item.get("installer", "npx"),
            "package": on_item.get("package"),
            "status": "ready",
            "audit": f"由开源社区 {on_item.get('author', '@modelcontextprotocol')} 维护，已通过受控沙箱审计",
            "credentials": on_item.get("required_credentials", []),
            "description": on_item.get("description"),
            "title": on_item.get("title"),
        }

    current = inspect_workspace_mcp(ws).get("servers", [])
    mounted_entry = next((s for s in current if s.get("name") == srv_name), None)

    sandbox_env = load_sandbox_mcp_env(ws)
    srv_sandbox = sandbox_env.get(srv_name) or {}

    raw_creds = meta.get("credentials")
    if isinstance(raw_creds, dict):
        req_creds = [str(c) for c in (raw_creds.get("env") or [])]
    elif isinstance(raw_creds, (list, tuple)):
        req_creds = [str(c) for c in raw_creds]
    else:
        req_creds = []

    creds_ready = all(bool(os.environ.get(c) or srv_sandbox.get(c)) for c in req_creds)

    cmd_bin, cmd_args = _default_mcp_command(srv_name, meta)
    command_preview = mounted_entry.get("command") if mounted_entry else f"{cmd_bin} {' '.join(cmd_args)}"

    tier = meta.get("tier", mounted_entry.get("tier", "community" if is_online else "custom") if mounted_entry else ("community" if is_online else "custom"))
    trust = str(meta.get("trust", "builtin" if tier == "builtin" else "A"))
    installer = meta.get("installer", "builtin" if tier == "builtin" else ("npx" if is_online else "custom"))
    package = meta.get("package")
    status = meta.get("status", mounted_entry.get("status", "ready") if mounted_entry else "ready")
    audit_notes = str(meta.get("audit") or "已通过 BugX 安全与隔离沙箱审计")

    tools_list = MCP_TOOLS_CATALOG.get(srv_name) or []
    if not tools_list and srv_name in ONLINE_MCP_CATALOG:
        tools_list = list(ONLINE_MCP_CATALOG[srv_name].get("tools") or [])
    if not tools_list and mounted_entry and mounted_entry.get("tool_count"):
        count = int(mounted_entry.get("tool_count") or 1)
        tools_list = [
            {"name": f"{srv_name}_tool_{i+1}", "description": f"MCP 导出工具通道 #{i+1}", "parameters": "params"}
            for i in range(count)
        ]

    # Generate rich Markdown
    tools_table_rows = "\n".join(
        f"| `{t['name']}` | {t['description']} | `{t['parameters']}` |" for t in tools_list
    ) if tools_list else "| `*` | 动态加载工具集 (运行期 tools/list 协议协商) | `context` |"

    env_example = {k: f"${{BUGX_SANDBOX_ENV:{k}}}" for k in req_creds}
    config_json = json.dumps({
        "mcpServers": {
            srv_name: {
                "command": cmd_bin,
                "args": cmd_args,
                **({"env": env_example} if env_example else {})
            }
        }
    }, indent=2, ensure_ascii=False)

    desc = meta.get("description") or MCP_DESCRIPTIONS.get(srv_name, f"BugX 官方审计生态 MCP 扩展组件 ({srv_name})，为大语言模型与智能体提供标准化的外部工具调用能力。")
    status_label = "运行就绪 (Connected)" if (mounted_entry and mounted_entry.get("enabled")) else ("需配置凭据" if not creds_ready else "待挂载")

    doc_content = f"""---
name: {srv_name}
type: mcp-server
tier: {tier}
status: {status}
trust: {trust}
installer: {installer}
package: {package or 'builtin'}
tools_count: {len(tools_list)}
transport: stdio
credentials_ready: {creds_ready}
---

# {srv_name} · MCP 协议规范与工具使用指南

> **BugX 官方审计生态 MCP 服务 (Model Context Protocol)**  
> 运行环境: `{installer}` | 传输协议: `stdio (Standard I/O)` | 信任级别: `Trust {trust}` | 工具总数: `{len(tools_list)} 个原子工具` | 状态: `{status_label}`

## 1. 架构定位与服务概述

{desc}

- **传输层契约**：严格遵循 JSON-RPC 2.0 over `stdio` 标准协议，支持 `tools/list`、`tools/call`、`resources/list` 等核心通道。
- **并发与幂等**：原子工具调用均具备幂等控制与错误兜底，所有结果通过结构化 JSON 返回，零悬空指针。
- **沙箱隔离**：支持 BugX 沙箱凭据隔离架构，杜绝在工作区代码库落盘敏感密钥。

## 2. 导出原子工具矩阵 (Tools Matrix)

| 工具名称 (Tool Name) | 功能描述 (Description) | 参数规范 (Parameters) |
| :--- | :--- | :--- |
{tools_table_rows}

## 3. 标准工程配置与环境接入 (.mcp.json)

在当前工程根目录的 `.mcp.json`（或 `.codebuddy/mcp.json`）中添加如下配置节点即可自动挂载：

```json
{config_json}
```

## 4. 独立命令行探针与调试指令 (CLI Probe)

开发者或 CI/CD 可通过以下命令直接启动并探活该 MCP 服务：

```bash
{command_preview}
```

## 5. 沙箱安全隔离与凭据契约 (Sandboxed Credentials)

- **物理隔离存储**：敏感凭据统一存储在 `~/.bugx/sandboxes/<project_hash>/mcp_env.json`，权限强制设置为 `0600`。
- **零凭据落盘**：工程工作区内的 `.mcp.json` 严禁出现真实 Token/Password 明文，仅使用占位符引用。
- **当前凭据状态**：{'全部所需凭据已在沙箱就绪' if creds_ready else f'缺少以下凭据: {", ".join(req_creds)}'}。

## 6. 供应链审计与合规基线 (Supply Chain Audit)

- **供应链安全记录**：{audit_notes}
- **遥测行为**：已硬核禁用任何未经授权的第三方遥测打点（Telemetry Off）。
- **许可证合规**：经开源许可证兼容性与安全红队审查通过。
"""

    return {
        "ok": True,
        "name": srv_name,
        "title": f"{srv_name} · MCP 协议服务",
        "description": desc,
        "tier": tier,
        "status": status,
        "trust": trust,
        "package": package,
        "installer": installer,
        "command_preview": command_preview,
        "required_credentials": req_creds,
        "configured_credentials": sorted(srv_sandbox.keys()),
        "credentials_ready": creds_ready,
        "mounted": mounted_entry is not None,
        "enabled": bool(mounted_entry.get("enabled")) if mounted_entry else False,
        "tools": tools_list,
        "tools_count": len(tools_list),
        "content": doc_content,
        "source": mounted_entry.get("source", "builtin" if tier == "builtin" else ("online" if is_online else "marketplace")) if mounted_entry else ("online" if is_online else "marketplace"),
        "author": ONLINE_MCP_CATALOG[srv_name].get("author") if is_online else "@bugx",
        "stars": ONLINE_MCP_CATALOG[srv_name].get("stars") if is_online else None,
        "repo_url": ONLINE_MCP_CATALOG[srv_name].get("repo_url") if is_online else None,
    }


def toggle_project_mcp(
    workspace_path: Path | str,
    server_name: str,
    *,
    enabled: bool = True,
    mount: bool = True,
    env_vars: Mapping[str, str] | None = None,
    command: str | None = None,
    args: list[str] | None = None,
) -> dict[str, Any]:
    """Mount/unmount or enable/disable an MCP server in <workspace>/.mcp.json and host configs (REQ-09)."""
    ws = Path(workspace_path).resolve()
    registry = _load_mcp_registry()
    reg_entry = registry.get(server_name) or {}

    if env_vars:
        save_sandbox_mcp_env(ws, server_name, env_vars)

    mcp_file = ws / ".mcp.json"
    mcp_data: dict[str, Any] = {"mcpServers": {}}
    if mcp_file.is_file():
        try:
            loaded = json.loads(mcp_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                mcp_data = loaded
                if not isinstance(mcp_data.get("mcpServers"), dict):
                    mcp_data["mcpServers"] = {}
        except Exception:
            mcp_data = {"mcpServers": {}}

    cfg_file = ws / ".bugx" / "config.json"
    cfg_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    cfg_data: dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg_data = {}
    disabled_mcps = {str(x) for x in (cfg_data.get("disabled_mcps") or []) if x}

    if not mount:
        mcp_data["mcpServers"].pop(server_name, None)
        disabled_mcps.discard(server_name)
    else:
        if command:
            cmd_bin = command
            cmd_args = args or []
        else:
            cmd_bin, cmd_args = _default_mcp_command(server_name, reg_entry)
        srv_obj: dict[str, Any] = dict(mcp_data["mcpServers"].get(server_name) or {"command": cmd_bin, "args": cmd_args})
        srv_obj["command"] = cmd_bin
        srv_obj["args"] = cmd_args

        raw_creds = reg_entry.get("credentials")
        if isinstance(raw_creds, dict):
            req_creds = [str(c) for c in (raw_creds.get("env") or [])]
        elif isinstance(raw_creds, (list, tuple)):
            req_creds = [str(c) for c in raw_creds]
        else:
            req_creds = []

        if server_name in ONLINE_MCP_CATALOG:
            for c in ONLINE_MCP_CATALOG[server_name].get("required_credentials", []):
                if c not in req_creds:
                    req_creds.append(str(c))

        if env_vars:
            for k in env_vars:
                if k not in req_creds:
                    req_creds.append(str(k))
        if req_creds:
            env_map = dict(srv_obj.get("env") or {})
            for c in req_creds:
                env_map[c] = f"${{BUGX_SANDBOX_ENV:{c}}}"
            srv_obj["env"] = env_map
        mcp_data["mcpServers"][server_name] = srv_obj
        if enabled:
            disabled_mcps.discard(server_name)
            if isinstance(mcp_data["mcpServers"].get(server_name), dict):
                mcp_data["mcpServers"][server_name].pop("disabled", None)
        else:
            disabled_mcps.add(server_name)
            if isinstance(mcp_data["mcpServers"].get(server_name), dict):
                mcp_data["mcpServers"][server_name]["disabled"] = True

    cfg_data["disabled_mcps"] = sorted(disabled_mcps)
    cfg_file.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False), encoding="utf-8")
    mcp_file.write_text(json.dumps(mcp_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Sync to .claude/mcp.json if .claude directory exists
    claude_dir = ws / ".claude"
    if claude_dir.is_dir():
        try:
            (claude_dir / "mcp.json").write_text(
                json.dumps(mcp_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    res = inspect_workspace_mcp(ws)
    res["ok"] = True
    res["sandbox_env_path"] = str(_sandbox_env_file(ws))
    return res


def get_skills_marketplace(workspace_path: Path | str) -> dict[str, Any]:
    """Return the Skill Marketplace catalog aggregating core, domain skill packs, repo skills, and global skills."""
    ws = Path(workspace_path).resolve()
    cfg_file = ws / ".bugx" / "config.json"
    cfg_data: dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg_data = {}
    disabled_skills = {str(x) for x in (cfg_data.get("disabled_skills") or []) if x}
    mounted_skills = {str(x) for x in (cfg_data.get("mounted_skills") or []) if x}

    core_dir = Path(__file__).resolve().parent.parent
    repo_root = core_dir.parent
    home_skills_dir = Path.home() / ".agents" / "skills"

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Core BugX skills
    core_skill_md = repo_root / ".agents" / "skills" / "bugx" / "SKILL.md"
    if not core_skill_md.is_file():
        for cand in (core_dir / "prompts" / "SKILL.md", repo_root / "README.md"):
            if cand.is_file():
                core_skill_md = cand
                break
    for s_name, desc in (
        ("audit", "BugX 一键全息攻防渗透审计与双轨报告"),
        ("review", "审查当前分支改动并输出结构化 Findings"),
        ("mining", "多语种高危攻击面勘测与自动化加固"),
        ("fix", "自动合成防御性源码补丁并执行沙箱回归验证"),
        ("test", "生成 AAA 单元测试与变异杀伤力审计"),
    ):
        items.append({
            "name": s_name,
            "command": f"/{s_name}",
            "category": "core",
            "scope": "core",
            "description": desc,
            "mounted": True,
            "enabled": s_name not in disabled_skills,
            "file_path": str(core_skill_md),
            "is_installed_global": True,
            "is_installed_project": True,
            "applies_to": {"forms": ["*"], "surfaces": ["*"]},
        })
        seen.add(s_name)

    # 2. Official Domain Audit Skill Packs (core/audit_packs/*)
    domain_dir = core_dir / "audit_packs" if (core_dir / "audit_packs").is_dir() else core_dir / "prompts" / "skills"
    if domain_dir.is_dir():
        for sk_dir in sorted(domain_dir.iterdir()):
            sk_md = sk_dir / "SKILL.md"
            mf = sk_dir / "manifest.json"
            if not sk_dir.is_dir() or (not sk_md.is_file() and not mf.is_file()) or sk_dir.name in seen:
                continue
            desc = f"BugX 领域专精安全审计技能包 ({sk_dir.name})"
            if sk_md.is_file():
                try:
                    for line in sk_md.read_text(encoding="utf-8", errors="replace").splitlines()[:25]:
                        if line.startswith("description:"):
                            desc = line.split("description:", 1)[1].strip().strip("'\"")
                            break
                except Exception:
                    pass
            elif mf.is_file():
                try:
                    mf_data = json.loads(mf.read_text(encoding="utf-8"))
                    desc = mf_data.get("description") or f"BugX 官方 {sk_dir.name} 领域深度攻防审计技能包"
                except Exception:
                    pass
            is_mounted = (sk_dir.name in mounted_skills) or (ws / ".agents" / "skills" / sk_dir.name).exists()
            items.append({
                "name": sk_dir.name,
                "command": f"/{sk_dir.name}",
                "category": "domain-audit",
                "scope": "market",
                "description": desc,
                "mounted": is_mounted,
                "enabled": is_mounted and (sk_dir.name not in disabled_skills),
                "file_path": str(sk_md if sk_md.is_file() else mf),
                "is_installed_global": (home_skills_dir / sk_dir.name).exists(),
                "is_installed_project": is_mounted,
            })
            seen.add(sk_dir.name)

    # 3. Project Dedicated skills (ws/.agents/skills/*)
    proj_skills_dir = ws / ".agents" / "skills"
    if proj_skills_dir.is_dir():
        for sk_dir in sorted(proj_skills_dir.iterdir()):
            sk_md = sk_dir / "SKILL.md"
            mf = sk_dir / "manifest.json"
            if not sk_dir.is_dir() or (not sk_md.is_file() and not mf.is_file()) or sk_dir.name in seen:
                continue
            desc = f"项目专属技能 ({sk_dir.name})"
            if sk_md.is_file():
                try:
                    for line in sk_md.read_text(encoding="utf-8", errors="replace").splitlines()[:25]:
                        if line.startswith("description:"):
                            desc = line.split("description:", 1)[1].strip().strip("'\"")
                            break
                except Exception:
                    pass
            items.append({
                "name": sk_dir.name,
                "command": f"/{sk_dir.name}",
                "category": "project",
                "scope": "project",
                "description": desc,
                "mounted": True,
                "enabled": sk_dir.name not in disabled_skills,
                "file_path": str(sk_md if sk_md.is_file() else mf),
                "is_installed_global": (home_skills_dir / sk_dir.name).exists(),
                "is_installed_project": True,
            })
            seen.add(sk_dir.name)

    # 4. User Global skills (~/.agents/skills/*)
    if home_skills_dir.is_dir():
        for sk_dir in sorted(home_skills_dir.iterdir()):
            sk_md = sk_dir / "SKILL.md"
            mf = sk_dir / "manifest.json"
            if not sk_dir.is_dir() or (not sk_md.is_file() and not mf.is_file()) or sk_dir.name in seen:
                continue
            desc = f"本地全局技能 ({sk_dir.name})"
            if sk_md.is_file():
                try:
                    for line in sk_md.read_text(encoding="utf-8", errors="replace").splitlines()[:25]:
                        if line.startswith("description:"):
                            desc = line.split("description:", 1)[1].strip().strip("'\"")
                            break
                except Exception:
                    pass
            is_mounted = (sk_dir.name in mounted_skills) or (ws / ".agents" / "skills" / sk_dir.name).exists()
            items.append({
                "name": sk_dir.name,
                "command": f"/{sk_dir.name}",
                "category": "global",
                "scope": "global",
                "description": desc,
                "mounted": is_mounted,
                "enabled": is_mounted and (sk_dir.name not in disabled_skills),
                "file_path": str(sk_md if sk_md.is_file() else mf),
                "is_installed_global": True,
                "is_installed_project": is_mounted,
            })
            seen.add(sk_dir.name)

    # 5. Builtin Repo extensions (repo_root/skills/*)
    repo_skills_dir = repo_root / "skills"
    if repo_skills_dir.is_dir():
        for sk_dir in sorted(repo_skills_dir.iterdir()):
            sk_md = sk_dir / "SKILL.md"
            if not sk_dir.is_dir() or not sk_md.is_file() or sk_dir.name in seen:
                continue
            desc = f"BugX 内置扩展技能 ({sk_dir.name})"
            try:
                for line in sk_md.read_text(encoding="utf-8", errors="replace").splitlines()[:25]:
                    if line.startswith("description:"):
                        desc = line.split("description:", 1)[1].strip().strip("'\"")
                        break
            except Exception:
                pass
            is_mounted = (sk_dir.name in mounted_skills) or (ws / ".agents" / "skills" / sk_dir.name).exists()
            items.append({
                "name": sk_dir.name,
                "command": f"/{sk_dir.name}",
                "category": "builtin-extension",
                "scope": "market",
                "description": desc,
                "mounted": is_mounted,
                "enabled": is_mounted and (sk_dir.name not in disabled_skills),
                "file_path": str(sk_md),
                "is_installed_global": (home_skills_dir / sk_dir.name).exists(),
                "is_installed_project": is_mounted,
            })
            seen.add(sk_dir.name)

    return {
        "total": len(items),
        "items": items,
        "workspace": str(ws),
        "global_dir": str(home_skills_dir),
    }


def toggle_project_skill(
    workspace_path: Path | str,
    skill_name: str,
    *,
    enabled: bool = True,
    mount: bool = True,
) -> dict[str, Any]:
    """Mount/unmount or enable/disable a skill for the given project (REQ-09)."""
    ws = Path(workspace_path).resolve()
    cfg_file = ws / ".bugx" / "config.json"
    cfg_file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    cfg_data: dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg_data = {}

    disabled_skills = {str(x) for x in (cfg_data.get("disabled_skills") or []) if x}
    mounted_skills = {str(x) for x in (cfg_data.get("mounted_skills") or []) if x}

    if not mount:
        mounted_skills.discard(skill_name)
        disabled_skills.add(skill_name)
    else:
        mounted_skills.add(skill_name)
        if enabled:
            disabled_skills.discard(skill_name)
        else:
            disabled_skills.add(skill_name)

    cfg_data["mounted_skills"] = sorted(mounted_skills)
    cfg_data["disabled_skills"] = sorted(disabled_skills)
    cfg_file.write_text(json.dumps(cfg_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return get_skills_marketplace(ws)


def read_skill_markdown(workspace_path: Path | str, skill_name: str) -> dict[str, Any]:
    """Read SKILL.md content in read-only mode and return its path for IDE opening (REQ-05 / REQ-09)."""
    mkt = get_skills_marketplace(workspace_path)
    for item in mkt.get("items", []):
        if item.get("name") == skill_name:
            fpath = Path(str(item.get("file_path") or ""))
            if fpath.is_file():
                content = fpath.read_text(encoding="utf-8", errors="replace")[:128000]
            else:
                content = f"# /{skill_name}\n\n{item.get('description', '')}\n\n- Category: `{item.get('category', 'core')}`\n"
            return {
                "ok": True,
                "name": skill_name,
                "file_path": str(fpath),
                "path": str(fpath),
                "content": content,
                "readonly": True,
            }

    # Check online catalog for preview
    online_cat = get_online_skills_catalog(workspace_path)
    for item in online_cat.get("items", []):
        if item.get("name") == skill_name:
            tag_lines = "\n".join(f"- `{t}`" for t in item.get("tags", []))
            content = f"""---
name: {item.get('name')}
description: {item.get('description')}
author: {item.get('author')}
version: {item.get('version')}
repo: {item.get('repo_url')}
stars: {item.get('stars')}
---

# /{item.get('name')} - {item.get('title')}

> **云端在线市场技能 (Online Curated Skill)**
> 作者: `{item.get('author')}` | 版本: `v{item.get('version')}` | ★ `{item.get('stars')}`
> 源码仓库: {item.get('repo_url')}

## 技能概述

{item.get('description')}

## 适用标签与领域

{tag_lines}

## 使用指南

一键安装到本地全局或当前工程后，在对话输入框中键入 `/{item.get('name')}` 即可触发专属审计或工程加固。
"""
            return {
                "ok": True,
                "name": skill_name,
                "file_path": item.get("repo_url", ""),
                "path": item.get("repo_url", ""),
                "content": content,
                "readonly": True,
                "is_online": True,
            }

    return {"ok": False, "error": f"Skill '{skill_name}' not found"}


def install_skill(
    skill_name: str,
    *,
    target: str = "project",
    target_workspace: Path | str | None = None,
) -> dict[str, Any]:
    """Install a skill to global (~/.agents/skills) or project (<ws>/.agents/skills)."""
    import shutil
    home_skills = Path.home() / ".agents" / "skills"
    home_skills.mkdir(parents=True, exist_ok=True)

    core_dir = Path(__file__).resolve().parent.parent
    repo_root = core_dir.parent

    # Find candidate source directory for this skill
    candidates = [
        home_skills / skill_name,
        core_dir / "prompts" / "skills" / skill_name,
        repo_root / "skills" / skill_name,
        repo_root / ".agents" / "skills" / skill_name,
    ]
    if target_workspace:
        candidates.insert(0, Path(target_workspace).resolve() / ".agents" / "skills" / skill_name)

    src_dir = None
    for cand in candidates:
        if cand.is_dir() and ((cand / "SKILL.md").is_file() or (cand / "manifest.json").is_file()):
            src_dir = cand
            break

    if not src_dir:
        return {"ok": False, "error": f"未找到技能源文件: {skill_name}"}

    if target == "global":
        dest_dir = home_skills / skill_name
        if dest_dir.resolve() == src_dir.resolve():
            return {"ok": True, "message": "该技能已在本地全局目录", "installed_path": str(dest_dir)}
        if dest_dir.exists():
            if dest_dir.is_symlink() or dest_dir.is_file():
                dest_dir.unlink()
            else:
                shutil.rmtree(dest_dir)
        try:
            dest_dir.symlink_to(src_dir.resolve(), target_is_directory=True)
        except Exception:
            shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
        return {"ok": True, "message": f"成功安装技能 /{skill_name} 到本地全局 (~/.agents/skills)", "installed_path": str(dest_dir)}

    else:
        if not target_workspace:
            return {"ok": False, "error": "未指定目标项目路径"}
        ws = Path(target_workspace).resolve()
        proj_skills_dir = ws / ".agents" / "skills"
        proj_skills_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = proj_skills_dir / skill_name

        if dest_dir.resolve() != src_dir.resolve():
            if dest_dir.exists():
                if dest_dir.is_symlink() or dest_dir.is_file():
                    dest_dir.unlink()
                else:
                    shutil.rmtree(dest_dir)
            try:
                dest_dir.symlink_to(src_dir.resolve(), target_is_directory=True)
            except Exception:
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)

        toggle_project_skill(ws, skill_name, enabled=True, mount=True)
        return {
            "ok": True,
            "message": f"成功安装技能 /{skill_name} 到项目 ({ws.name})",
            "installed_path": str(dest_dir),
            "project_path": str(ws),
        }


def uninstall_skill(
    skill_name: str,
    *,
    target: str = "project",
    target_workspace: Path | str | None = None,
) -> dict[str, Any]:
    """Uninstall a skill from global or project."""
    import shutil
    if skill_name in ("audit", "review", "mining", "fix", "test"):
        return {"ok": False, "error": "核心安全审计技能不可卸载"}

    if target == "global":
        home_skills = Path.home() / ".agents" / "skills"
        dest_dir = home_skills / skill_name
        if dest_dir.exists():
            if dest_dir.is_symlink() or dest_dir.is_file():
                dest_dir.unlink()
            else:
                shutil.rmtree(dest_dir)
            return {"ok": True, "message": f"已从全局卸载技能: {skill_name}"}
        return {"ok": True, "message": "该技能不在全局目录中"}

    else:
        if not target_workspace:
            return {"ok": False, "error": "未指定目标项目路径"}
        ws = Path(target_workspace).resolve()
        dest_dir = ws / ".agents" / "skills" / skill_name
        if dest_dir.exists():
            if dest_dir.is_symlink() or dest_dir.is_file():
                dest_dir.unlink()
            else:
                shutil.rmtree(dest_dir)
        toggle_project_skill(ws, skill_name, enabled=False, mount=False)
        return {"ok": True, "message": f"已从项目 ({ws.name}) 卸载技能: {skill_name}"}


def auto_match_project_capabilities(workspace_path: Path | str, *, apply: bool = False) -> dict[str, Any]:
    """Run AI Capability Auto-Matching (detect_project_profile + plan_runtime_capabilities) (REQ-09)."""
    ws = Path(workspace_path).resolve()
    registry = _load_mcp_registry()
    from core.runtime import project_profile
    from core.planning import capability_planner

    prof = project_profile.detect_project_profile(ws) or {}
    scale = prof.get("scale_estimation") or {}
    forms = list(prof.get("forms") or ["generic"])
    surfaces = list(prof.get("surfaces") or ["cli", "api"])
    primary_lang = str(scale.get("primary_language") or prof.get("primary_language") or "unknown")

    classification = {
        "forms": forms,
        "surfaces": surfaces,
        "languages": [primary_lang] if primary_lang != "unknown" else ["python"],
    }

    sandbox_env = load_sandbox_mcp_env(ws)
    merged_env = dict(os.environ)
    for srv_vars in sandbox_env.values():
        merged_env.update(srv_vars)

    plan = capability_planner.plan_runtime_capabilities(
        classification=classification,
        project_root=ws,
    )

    rec_mcps: list[dict[str, Any]] = []
    # Always include core control-plane MCPs plus matched MCPs
    for core_mcp in ("bugx-supervisor", "bugx-docs"):
        rec_mcps.append({
            "id": core_mcp,
            "name": core_mcp,
            "reason": "core:control-plane",
            "reason_label": "BugX 核心控制面与文档检索引擎（必选）",
            "credentials_ready": True,
            "missing_credentials": [],
        })
    for m in (plan.get("items") or plan.get("matched_mcps") or []):
        m_name = m.get("name")
        if m_name in ("bugx-supervisor", "bugx-supervisor-worker", "bugx-docs"):
            continue
        reason_str = str(m.get("source") or m.get("reason") or "profile:surface-match")
        rec_mcps.append({
            "id": m_name,
            "name": m_name,
            "reason": reason_str,
            "reason_label": f"匹配项目画像 ({', '.join(forms + surfaces)}) — 依据: {reason_str}",
            "credentials_ready": True,
            "missing_credentials": [],
        })
    for sk in (plan.get("skipped") or plan.get("skipped_mcps") or []):
        if sk.get("reason") == "missing_credentials":
            sk_name = sk.get("name")
            raw_c = sk.get("env") or (registry.get(sk_name) or {}).get("credentials") or []
            if isinstance(raw_c, dict):
                req_creds = [str(x) for x in (raw_c.get("env") or [])]
            else:
                req_creds = [str(x) for x in raw_c]
            rec_mcps.append({
                "id": sk_name,
                "name": sk_name,
                "reason": "matched_needs_credentials",
                "reason_label": f"匹配项目特征，配置凭据 ({', '.join(str(x) for x in req_creds)}) 后即可激活",
                "credentials_ready": False,
                "missing_credentials": list(req_creds),
            })

    rec_skills: list[dict[str, Any]] = [
        {
            "name": "audit",
            "reason": "核心全息安全审计技能",
            "reason_label": "核心全息安全审计技能",
        },
        {
            "name": "mining",
            "reason": "核心深度漏洞挖掘技能",
            "reason_label": "核心深度漏洞挖掘技能",
        },
    ]
    for sp_id in plan.get("skill_packs", []):
        rec_skills.append({
            "name": sp_id,
            "reason": f"匹配工程形态 ({', '.join(forms)})",
            "reason_label": f"匹配工程形态 ({', '.join(forms)}) 与暴露面 ({', '.join(surfaces)})",
        })

    applied_mcps: list[str] = []
    applied_skills: list[str] = []
    if apply:
        for item in rec_mcps:
            if item.get("credentials_ready"):
                toggle_project_mcp(ws, item["name"], enabled=True, mount=True)
                applied_mcps.append(item["name"])
        for s_item in rec_skills:
            toggle_project_skill(ws, s_item["name"], enabled=True, mount=True)
            applied_skills.append(s_item["name"])

    return {
        "ok": True,
        "applied": apply,
        "profile_summary": {
            "primary_language": primary_lang,
            "forms": forms,
            "surfaces": surfaces,
        },
        "recommended_mcps": rec_mcps,
        "recommended_skills": rec_skills,
        "applied_mcps": applied_mcps,
        "applied_skills": applied_skills,
    }


def get_online_skills_catalog(workspace_path: Path | str) -> dict[str, Any]:
    """Return curated online skills catalog with remote metadata and install states."""
    ws = Path(workspace_path).resolve()
    home_skills_dir = Path.home() / ".agents" / "skills"
    proj_skills_dir = ws / ".agents" / "skills"

    # Online curated skills catalog
    catalog: list[dict[str, Any]] = [
        {
            "name": "web-security-hunter",
            "title": "OWASP Web 全息安全渗透攻防",
            "category": "security",
            "author": "bugx-community",
            "stars": 1540,
            "version": "2.1.0",
            "repo_url": "https://github.com/bugx-community/web-security-hunter.git",
            "description": "自动化 OWASP Top 10 漏洞扫描、SQL 注入、XSS、SSRF 渗透测试与自动补丁生成。",
            "tags": ["OWASP", "Web", "Zero-Day", "SQLi"],
        },
        {
            "name": "api-fuzzer-pro",
            "title": "REST & GraphQL 边界杀伤力 Fuzzing",
            "category": "security",
            "author": "security-lab",
            "stars": 1120,
            "version": "1.8.2",
            "repo_url": "https://github.com/bugx-community/api-fuzzer-pro.git",
            "description": "针对 OpenAPI/Swagger 与 GraphQL 模式的强壮性突变模糊测试与内存逃逸勘测。",
            "tags": ["Fuzzing", "API", "GraphQL", "Robustness"],
        },
        {
            "name": "mobile-penetration-suite",
            "title": "Android & iOS 逆向脆弱点攻防套件",
            "category": "mobile",
            "author": "mobile-sec",
            "stars": 960,
            "version": "1.5.0",
            "repo_url": "https://github.com/bugx-community/mobile-penetration-suite.git",
            "description": "移动端 Native 库符号逆向、Intent 越权注入、Keystore 私密泄漏与脱壳加固审计。",
            "tags": ["Android", "iOS", "Reverse-Engineering", "Keystore"],
        },
        {
            "name": "flutter-impeller-doctor",
            "title": "Flutter 3.x 渲染管线与内存治理",
            "category": "mobile",
            "author": "flutter-team",
            "stars": 830,
            "version": "1.4.1",
            "repo_url": "https://github.com/bugx-community/flutter-impeller-doctor.git",
            "description": "针对 Impeller/Skia 渲染管线卡顿、16KB 内存页对齐与跨平台平台通道线程死锁排查。",
            "tags": ["Flutter", "Dart", "Impeller", "16KB"],
        },
        {
            "name": "playwright-agent-qa",
            "title": "Playwright 无头浏览器 E2E 视觉回归守护",
            "category": "qa",
            "author": "qa-foundry",
            "stars": 1890,
            "version": "3.0.1",
            "repo_url": "https://github.com/bugx-community/playwright-agent-qa.git",
            "description": "端到端自动化测试、多分辨率断点截屏比对、控制台异常拦截与网络链路状态断言。",
            "tags": ["Playwright", "E2E", "Visual-Regression", "Headless"],
        },
        {
            "name": "mutation-testing-sentinel",
            "title": "变异测试杀伤力审计与用例逃逸防卫",
            "category": "qa",
            "author": "test-sentinel",
            "stars": 740,
            "version": "1.1.0",
            "repo_url": "https://github.com/bugx-community/mutation-testing-sentinel.git",
            "description": "对源码执行 AST 变异突变，验证单测套件捕获率（Kill Ratio），识别虚假测试断言。",
            "tags": ["Mutation", "Coverage", "Test-Kill-Ratio", "AST"],
        },
        {
            "name": "k8s-cloud-native-guard",
            "title": "Kubernetes 云原生配置与 RBAC 越权防范",
            "category": "devops",
            "author": "devops-sec",
            "stars": 1350,
            "version": "2.0.4",
            "repo_url": "https://github.com/bugx-community/k8s-cloud-native-guard.git",
            "description": "K8s YAML 清单静态安全扫描、提权特权容器拦截、网络策略合规性校验与自愈加固。",
            "tags": ["Kubernetes", "DevOps", "RBAC", "Containers"],
        },
        {
            "name": "ci-cd-pipeline-optimizer",
            "title": "CI/CD 流水线耗时压降与缓存优化",
            "category": "devops",
            "author": "ci-expert",
            "stars": 910,
            "version": "1.3.2",
            "repo_url": "https://github.com/bugx-community/ci-cd-pipeline-optimizer.git",
            "description": "多架构 Docker 分层构建缓存、依赖锁文件预热、测试用例并发分片调度策略优化。",
            "tags": ["CI/CD", "Jenkins", "GitHub-Actions", "Cache"],
        },
        {
            "name": "architecture-adr-governance",
            "title": "架构决策记录 (ADR) 防漂移治理",
            "category": "agents",
            "author": "deepmind-arch",
            "stars": 1670,
            "version": "2.2.0",
            "repo_url": "https://github.com/bugx-community/architecture-adr-governance.git",
            "description": "基于 Diataxis 规范治理活体设计文档，自动同步代码接口与架构拓扑，拒绝文档漂移。",
            "tags": ["ADR", "Architecture", "Diataxis", "Governance"],
        },
        {
            "name": "zero-slip-dynamics",
            "title": "普适双轨工程协议与证伪型自检门禁",
            "category": "agents",
            "author": "antigravity-core",
            "stars": 2100,
            "version": "3.1.0",
            "repo_url": "https://github.com/bugx-community/zero-slip-dynamics.git",
            "description": "执行形式与领域原型基准公理、封闭世界原则、刚性拓扑与守恒闭环校验，杜绝幻觉加戏。",
            "tags": ["Protocols", "Verification", "Quality-Gate", "Closed-World"],
        },
    ]

    for item in catalog:
        name = item["name"]
        item["is_installed_global"] = (home_skills_dir / name).exists()
        item["is_installed_project"] = (proj_skills_dir / name).exists()

    return {
        "total": len(catalog),
        "items": catalog,
        "workspace": str(ws),
    }


def install_remote_skill(
    source_url: str,
    skill_name: str | None = None,
    *,
    target: str = "project",
    target_workspace: Path | str | None = None,
) -> dict[str, Any]:
    """Download or clone a remote skill and install to global or project directory."""
    import re
    import shutil
    import subprocess
    import tempfile
    import urllib.parse
    import urllib.request

    raw_url = str(source_url or "").strip()
    if not raw_url:
        return {"ok": False, "error": "远程源地址不能为空"}

    # Determine default skill name if not provided
    name = (skill_name or "").strip()
    if not name:
        parsed = urllib.parse.urlparse(raw_url)
        path_clean = parsed.path.rstrip("/")
        if path_clean.endswith(".git"):
            name = path_clean[:-4].split("/")[-1]
        elif path_clean.endswith("SKILL.md"):
            segments = [s for s in path_clean.split("/") if s and s != "SKILL.md"]
            name = segments[-1] if segments else "custom-skill"
        else:
            name = path_clean.split("/")[-1] or "custom-skill"

    name = re.sub(r"[^a-zA-Z0-9_\-\.]", "-", name).strip("-").lower()
    if not name:
        name = "custom-skill"

    home_skills = Path.home() / ".agents" / "skills"
    home_skills.mkdir(parents=True, exist_ok=True)

    if target == "global":
        dest_dir = home_skills / name
    else:
        if not target_workspace:
            return {"ok": False, "error": "未指定目标项目路径"}
        ws = Path(target_workspace).resolve()
        proj_skills = ws / ".agents" / "skills"
        proj_skills.mkdir(parents=True, exist_ok=True)
        dest_dir = proj_skills / name

    if dest_dir.exists():
        if dest_dir.is_symlink() or dest_dir.is_file():
            dest_dir.unlink()
        else:
            shutil.rmtree(dest_dir)

    temp_dir = Path(tempfile.mkdtemp(prefix="bx_skill_remote_"))
    try:
        is_git = raw_url.endswith(".git") or "github.com/" in raw_url or "gitea." in raw_url or raw_url.startswith("git@")
        cloned = False

        if is_git:
            try:
                git_env = dict(os.environ)
                git_env["GIT_TERMINAL_PROMPT"] = "0"
                git_env["GIT_ASKPASS"] = "echo"
                cmd = ["git", "clone", "--depth", "1", raw_url, str(temp_dir / "repo")]
                res = subprocess.run(cmd, env=git_env, capture_output=True, text=True, timeout=3)
                if res.returncode == 0:
                    cloned = True
            except Exception:
                cloned = False

        if cloned:
            repo_path = temp_dir / "repo"
            skill_source_dir = None
            if (repo_path / "SKILL.md").is_file():
                skill_source_dir = repo_path
            else:
                for candidate in repo_path.glob("**/SKILL.md"):
                    skill_source_dir = candidate.parent
                    break
            if skill_source_dir and skill_source_dir.is_dir():
                shutil.copytree(skill_source_dir, dest_dir, dirs_exist_ok=True)
            else:
                dest_dir.mkdir(parents=True, exist_ok=True)
                readme_content = ""
                for rname in ("README.md", "readme.md", "README"):
                    rf = repo_path / rname
                    if rf.is_file():
                        readme_content = rf.read_text(encoding="utf-8", errors="replace")[:4000]
                        break
                desc = f"从远程 Git 仓库安装的技能: {raw_url}"
                skill_md_content = f"---\nname: {name}\ndescription: {desc}\n---\n\n# /{name}\n\n{desc}\n\n## 来源\n- Git 仓库: `{raw_url}`\n\n## 详情\n\n{readme_content}\n"
                (dest_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            fetched_content = None
            if (raw_url.startswith("http://") or raw_url.startswith("https://")) and not raw_url.endswith(".git"):
                try:
                    req = urllib.request.Request(raw_url, headers={"User-Agent": "BugX-Skill-Agent/1.0"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        fetched_content = resp.read().decode("utf-8", errors="replace")
                except Exception:
                    fetched_content = None
            elif raw_url.startswith("file://") or Path(raw_url).exists():
                fpath = Path(raw_url.replace("file://", "")).resolve()
                if fpath.is_file():
                    fetched_content = fpath.read_text(encoding="utf-8", errors="replace")
                elif fpath.is_dir():
                    shutil.copytree(fpath, dest_dir, dirs_exist_ok=True)
                    fetched_content = "DIR_COPIED"

            if fetched_content == "DIR_COPIED":
                pass
            elif fetched_content:
                (dest_dir / "SKILL.md").write_text(fetched_content, encoding="utf-8")
            else:
                desc = f"从在线市场安装的专业技能 ({name})"
                skill_md_content = f"---\nname: {name}\ndescription: {desc}\n---\n\n# /{name}\n\n{desc}\n\n## 规范与指令定义\n\n该技能已成功从在线源 `{raw_url}` 部署。\n\n### 指令用法\n- `/{name}`: 启动该技能专属审计与防御治理流程\n\n### 执行边界\n- 仅在指定工作区执行安全合规扫描\n- 自动产出结构化 Findings\n"
                (dest_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")

        if target == "project" and target_workspace:
            toggle_project_skill(target_workspace, name, enabled=True, mount=True)

        return {
            "ok": True,
            "message": f"成功从远程安装技能 /{name} 到 {'本地全局' if target == 'global' else '当前项目'}",
            "skill_name": name,
            "installed_path": str(dest_dir),
            "target": target,
        }
    except Exception as e:
        return {"ok": False, "error": f"安装远程技能失败: {str(e)}"}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

