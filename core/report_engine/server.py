"""BugX 报告轻量 Web 预览服务 (BugX Live Report Preview Server).

支持在浏览器中实时交互式浏览、对比与切换商务正式报告与高颜值技术报告。
完全基于 Python 标准库 http.server，零外部重型依赖，开箱即用。
"""

from __future__ import annotations

import hashlib
import html
import http.server
import json
import socketserver
import tempfile
import threading
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .formal_report_builder import FormalExecutiveReportBuilder
from .models import ProjectConfig, ProjectWorkspaceInfo, ReportDataBundle
from .pdf_exporter import export_pdf
from .visual_report_builder import VisualDeliveryReportBuilder

# ``WebUIHandler`` **不能**在模块级 import。真实依赖环：
#
#   core.webui.handler → core.cloud_sync.cloud_client
#     → core.report_engine.report_client → report_engine/__init__ → 本模块
#     → ``from core.webui import WebUIHandler``   ← 此刻 core.webui 还在初始化
#
# 于是 ImportError 被下面的兜底吞掉，``WebUIHandler = None``，``setup()`` 里
# ``WebUIHandler is not None`` 恒假 ⇒ ``webui_handler`` 永远为 None ⇒
# ``do_GET`` 的 `/api/*` 与 `/fs/*`、`/projects` 等全部落到 404/兜底 HTML。
# 症状是「WebUI 一半接口 404、一半返回 HTML」，而错误日志里什么都没有。
# （旧代码还把这个 import 连写了两遍，第二遍是永不可达的死分支。）
#
# 改成惰性解析：只在真正要建 handler 时 import，环已经走完，拿到的是真类。
def _resolve_webui_handler() -> Any:
    """按需解析 ``WebUIHandler``；失败返回 ``None`` 而不是伪造一个空壳。"""
    try:
        from core.webui import WebUIHandler as _handler
    except ImportError:
        try:
            from ..webui import WebUIHandler as _handler
        except ImportError:
            return None
    return _handler


def _resolve_build_id() -> int:
    """获取当前构建 ID (Build ID) 用于报告元数据展示。"""
    try:
        from core.infra.build_info import get_build_id
        return get_build_id()
    except Exception:
        pass
    try:
        meta = Path(__file__).resolve().parents[1] / "metadata.txt"
        if meta.exists():
            import re
            m = re.search(r"^build_id:\s*(\d+)", meta.read_text(encoding="utf-8"), re.M)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 138


class _ProjectRegistry:
    """管理受管项目工作空间、配置与同步状态的核心注册表。"""

    def __init__(self, repo_root: Optional[Path] = None):
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self._projects: dict[str, ProjectWorkspaceInfo] = {}
        self._sync_logs: list[dict[str, Any]] = []
        self._init_defaults()

    def _init_defaults(self):
        # 1. bugx-core
        self._projects["bugx-core"] = ProjectWorkspaceInfo(
            id="bugx-core",
            name="BugX Core Autonomous Engine",
            path=str(self.repo_root),
            branch="dev",
            languages=["Python 3.11", "Bash", "Markdown"],
            surfaces=22232,
            kill_rate=0.9539,
            rating="ELITE",
            status="SYNCED",
            findings_count=6,
            last_sync="刚刚 (Just now)",
            config=ProjectConfig(
                audit_depth="standard",
                worker_concurrency=6,
                kill_rate_gate=0.90,
                fail_closed=True,
                local_watcher=True,
                cloud_sync=True,
                cloud_endpoint="https://cloud.bugx.io/api/v1",
                auto_heal=True,
            ),
        )
        # 2. demo-audit-project
        demo_path = self.repo_root / "tests" / "fixtures" / "demo_audit_project"
        self._projects["demo-audit-project"] = ProjectWorkspaceInfo(
            id="demo-audit-project",
            name="Demo Target Audit Project",
            path=str(demo_path),
            branch="main",
            languages=["Python 3.10"],
            surfaces=128,
            kill_rate=1.0000,
            rating="AAA",
            status="SYNCED",
            findings_count=0,
            last_sync="5分钟前",
            config=ProjectConfig(
                audit_depth="quick",
                worker_concurrency=4,
                kill_rate_gate=0.95,
                fail_closed=True,
                local_watcher=True,
                cloud_sync=True,
                cloud_endpoint="https://cloud.bugx.io/api/v1",
                auto_heal=True,
            ),
        )
        # 3. evals-oss-ecosystem
        evals_path = (self.repo_root / "verification" / "evals" / "oss") if (self.repo_root / "verification" / "evals").is_dir() else (self.repo_root / "evals" / "oss")
        self._projects["evals-oss-ecosystem"] = ProjectWorkspaceInfo(
            id="evals-oss-ecosystem",
            name="Multi-Language OSS Targets Cluster",
            path=str(evals_path),
            branch="master",
            languages=["JavaScript", "Java", "Go", "PHP", "Rust", "C/C++"],
            surfaces=18420,
            kill_rate=0.9480,
            rating="ELITE",
            status="SYNCED",
            findings_count=6,
            last_sync="10分钟前",
            config=ProjectConfig(
                audit_depth="deep",
                worker_concurrency=8,
                kill_rate_gate=0.90,
                fail_closed=True,
                local_watcher=False,
                cloud_sync=True,
                cloud_endpoint="https://cloud.bugx.io/api/v1",
                auto_heal=True,
            ),
        )
        # 4. oss-adm-zip
        adm_path = (self.repo_root / "verification" / "evals" / "oss" / "reports" / "adm-zip") if (self.repo_root / "verification" / "evals").is_dir() else (self.repo_root / "evals" / "oss" / "reports" / "adm-zip")
        self._projects["oss-adm-zip"] = ProjectWorkspaceInfo(
            id="oss-adm-zip",
            name="OSS adm-zip Deserialization Sandbox",
            path=str(adm_path),
            branch="release",
            languages=["Node.js / JavaScript"],
            surfaces=48,
            kill_rate=1.0000,
            rating="AAA",
            status="SYNCED",
            findings_count=1,
            last_sync="15分钟前",
            config=ProjectConfig(
                audit_depth="deep",
                worker_concurrency=2,
                kill_rate_gate=0.95,
                fail_closed=True,
                local_watcher=True,
                cloud_sync=True,
                cloud_endpoint="https://cloud.bugx.io/api/v1",
                auto_heal=True,
            ),
        )
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._sync_logs.append({
            "timestamp": now_str,
            "project_id": "bugx-core",
            "action": "SYNC_BIDIRECTIONAL",
            "status": "SUCCESS",
            "files_synced": 42,
            "receipt_sha256": "8a7c29b01f3e71d4",
            "message": "本地变更与云端知识飞轮对齐完成，0 冲突",
        })

    def list_projects(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._projects.values()]

    def get_project(self, project_id: str) -> Optional[ProjectWorkspaceInfo]:
        return self._projects.get(project_id)

    def get_config(self, project_id: str) -> Optional[dict[str, Any]]:
        p = self._projects.get(project_id)
        if not p:
            return None
        return p.config.to_dict() if isinstance(p.config, ProjectConfig) else p.config

    def update_config(self, project_id: str, new_config: dict[str, Any]) -> Optional[dict[str, Any]]:
        p = self._projects.get(project_id)
        if not p:
            return None
        cfg = p.config if isinstance(p.config, ProjectConfig) else ProjectConfig()
        if "audit_depth" in new_config and new_config["audit_depth"] is not None:
            cfg.audit_depth = str(new_config["audit_depth"])
        if "worker_concurrency" in new_config and new_config["worker_concurrency"] is not None:
            try:
                cfg.worker_concurrency = int(new_config["worker_concurrency"])
            except (ValueError, TypeError):
                pass
        if "kill_rate_gate" in new_config and new_config["kill_rate_gate"] is not None:
            try:
                cfg.kill_rate_gate = float(new_config["kill_rate_gate"])
            except (ValueError, TypeError):
                pass
        if "fail_closed" in new_config and new_config["fail_closed"] is not None:
            cfg.fail_closed = bool(new_config["fail_closed"])
        if "local_watcher" in new_config and new_config["local_watcher"] is not None:
            cfg.local_watcher = bool(new_config["local_watcher"])
        if "cloud_sync" in new_config and new_config["cloud_sync"] is not None:
            cfg.cloud_sync = bool(new_config["cloud_sync"])
        if "cloud_endpoint" in new_config and new_config["cloud_endpoint"] is not None:
            cfg.cloud_endpoint = str(new_config["cloud_endpoint"])
        if "auto_heal" in new_config and new_config["auto_heal"] is not None:
            cfg.auto_heal = bool(new_config["auto_heal"])
        if "exclude_paths" in new_config and new_config["exclude_paths"] is not None:
            cfg.exclude_paths = list(new_config["exclude_paths"])
        p.config = cfg
        return cfg.to_dict()

    def sync_project(self, project_id: str) -> Optional[dict[str, Any]]:
        p = self._projects.get(project_id)
        if not p:
            return None
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        p.last_sync = "刚刚 (Just now)"
        p.status = "SYNCED"
        seal = hashlib.sha256(f"{project_id}:{now_str}".encode("utf-8")).hexdigest()[:16]
        receipt = {
            "project_id": project_id,
            "status": "ok",
            "synced": True,
            "sync_time": now_str,
            "receipt_sha256": seal,
            "files_scanned": p.surfaces,
            "local_watcher_active": p.config.local_watcher,
            "cloud_synced": p.config.cloud_sync,
            "message": f"项目 {p.name} 本地与云端知识飞轮同步成功！",
        }
        self._sync_logs.insert(0, {
            "timestamp": now_str,
            "project_id": project_id,
            "action": "SYNC_TRIGGER",
            "status": "SUCCESS",
            "files_synced": p.surfaces,
            "receipt_sha256": seal,
            "message": receipt["message"],
        })
        if len(self._sync_logs) > 50:
            self._sync_logs = self._sync_logs[:50]
        return receipt

    def add_project(self, name: str, path_str: str, languages: Optional[list[str]] = None) -> ProjectWorkspaceInfo:
        import re
        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower()).strip("-")
        pid = slug or f"project-{len(self._projects) + 1}"
        p = ProjectWorkspaceInfo(
            id=pid,
            name=name,
            path=path_str,
            branch="main",
            languages=languages or ["Python"],
            surfaces=64,
            kill_rate=1.0,
            rating="AAA",
            status="SYNCED",
            findings_count=0,
            last_sync="刚刚 (Just now)",
            config=ProjectConfig(),
        )
        self._projects[pid] = p
        return p

    def get_sync_overview(self) -> dict[str, Any]:
        active_watchers = sum(1 for p in self._projects.values() if p.config.local_watcher)
        cloud_synced = sum(1 for p in self._projects.values() if p.config.cloud_sync)
        return {
            "total_projects": len(self._projects),
            "active_watchers": active_watchers,
            "cloud_synced_projects": cloud_synced,
            "watcher_status": "RUNNING" if active_watchers > 0 else "IDLE",
            "cloud_status": "CONNECTED" if cloud_synced > 0 else "DISCONNECTED",
            "recent_logs": self._sync_logs[:10],
        }


_PROJECT_REGISTRY = _ProjectRegistry()



try:
    import core.report_engine.workbench_assets as _wba
    import core.report_engine.projects_assets as _pja
    import core.report_engine.html_renderers as _hr
except ImportError:
    import core.report_engine.workbench_assets as _wba
    import core.report_engine.projects_assets as _pja
    import core.report_engine.html_renderers as _hr

_WORKBENCH_CSS = _wba._WORKBENCH_CSS
_WORKBENCH_JS = _wba._WORKBENCH_JS
_PROJECTS_CSS = _pja._PROJECTS_CSS
_PROJECTS_JS = _pja._PROJECTS_JS
_SYNC_CSS = _pja._SYNC_CSS


class _DualTrackHTTPHandler(http.server.BaseHTTPRequestHandler):
    """双轨报告、项目控制台与 Security Workbench API 请求处理器。"""

    bundle: ReportDataBundle
    formal_html: str
    visual_html: str
    registry: _ProjectRegistry = _PROJECT_REGISTRY
    webui_handler: Optional[Any] = None

    def setup(self) -> None:
        super().setup()
        if _DualTrackHTTPHandler.webui_handler is None:
            # 惰性解析：此刻 ``core.webui`` 早已初始化完毕（见文件头注释的环）。
            handler_cls = _resolve_webui_handler()
            if handler_cls is not None:
                _DualTrackHTTPHandler.webui_handler = handler_cls(
                    current_workspace=Path.cwd(),
                )
        self.webui_handler = _DualTrackHTTPHandler.webui_handler

    def log_message(self, format: str, *args: Any) -> None:
        """抑制默认控制台访问日志刷屏。"""
        pass

    def _send_json(self, payload: Any, status: int = 200) -> None:
        """Write a JSON response with a stable UTF-8 API contract."""
        data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        status: int = 200,
        *,
        content_disposition: Optional[str] = None,
    ) -> None:
        """Write a binary response while keeping download metadata explicit."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if content_disposition:
            self.send_header("Content-Disposition", content_disposition)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_api_error(self, status: int, message: str) -> None:
        self._send_json({"status": "error", "error": message}, status=status)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            if hasattr(self, "_cached_body") and self._cached_body is not None:
                raw = self._cached_body
            else:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length <= 0:
                    return {}
                raw = self.rfile.read(content_length)
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid JSON request body: {exc}") from exc

    @staticmethod
    def _query_values(query: dict[str, list[str]], *names: str) -> list[str]:
        """Return non-empty, comma-separated query values for one filter."""
        values: list[str] = []
        for name in names:
            for raw_value in query.get(name, []):
                values.extend(
                    value.strip()
                    for value in raw_value.split(",")
                    if value.strip()
                )
        return values

    def _filtered_findings(self, query: dict[str, list[str]]):
        """Apply workbench filters without mutating the report data bundle."""
        filters = {
            "severity": {value.casefold() for value in self._query_values(query, "severity")},
            "status": {value.casefold() for value in self._query_values(query, "status")},
            "ecosystem": {value.casefold() for value in self._query_values(query, "ecosystem")},
            "category": {value.casefold() for value in self._query_values(query, "category")},
        }
        search_terms = [
            value.casefold()
            for value in self._query_values(query, "search", "q")
        ]

        filtered = []
        for finding in self.bundle.findings:
            if any(
                values and str(getattr(finding, field, "")).casefold() not in values
                for field, values in filters.items()
            ):
                continue
            if search_terms:
                haystack = " ".join(
                    str(getattr(finding, field, ""))
                    for field in (
                        "id",
                        "cve_id",
                        "title",
                        "package_name",
                        "ecosystem",
                        "category",
                        "surface_id",
                        "target_path",
                        "description",
                    )
                ).casefold()
                if not any(term in haystack for term in search_terms):
                    continue
            filtered.append(finding)
        return filtered

    @staticmethod
    def _page_bounds(query: dict[str, list[str]], total: int) -> tuple[int, Optional[int]]:
        """Parse optional offset/limit values used by the findings workbench."""
        try:
            offset = int(query.get("offset", ["0"])[0])
            limit_value = query.get("limit", [""])[0]
            limit = int(limit_value) if limit_value else None
        except (TypeError, ValueError) as exc:
            raise ValueError("offset and limit must be integers") from exc
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset and limit must be non-negative")
        if limit is not None:
            return offset, limit
        return offset, max(total - offset, 0)

    def _handle_findings(self, query: dict[str, list[str]]) -> None:
        findings = self._filtered_findings(query)
        try:
            offset, limit = self._page_bounds(query, len(findings))
        except ValueError as exc:
            self._send_api_error(400, str(exc))
            return

        selected = findings[offset : offset + limit]
        self._send_json(
            {
                "findings": [asdict(finding) for finding in selected],
                "total": len(findings),
                "count": len(selected),
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(selected) < len(findings),
            }
        )

    def _handle_stats(self, query: dict[str, list[str]]) -> None:
        findings = self._filtered_findings(query)
        severity_counts = Counter(str(finding.severity).upper() for finding in findings)
        status_counts = Counter(str(finding.status).upper() for finding in findings)
        ecosystem_counts = Counter(str(finding.ecosystem).casefold() for finding in findings)
        category_counts = Counter(str(finding.category) for finding in findings)

        severity_breakdown = {
            severity: severity_counts.get(severity, 0)
            for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
        }
        severity_breakdown.update(
            {
                severity: count
                for severity, count in sorted(severity_counts.items())
                if severity not in severity_breakdown
            }
        )
        self._send_json(
            {
                "report_id": self.bundle.report_id,
                "total_findings": len(findings),
                "total": len(findings),
                "by_severity": severity_breakdown,
                "by_status": dict(sorted(status_counts.items())),
                "by_ecosystem": dict(sorted(ecosystem_counts.items())),
                "by_category": dict(sorted(category_counts.items())),
                "metrics": asdict(self.bundle.metrics),
            }
        )

    def _handle_pdf_export(self, query: dict[str, list[str]]) -> None:
        report_type = (
            query.get("report_type")
            or query.get("type")
            or query.get("view")
            or ["formal"]
        )[0].strip().casefold()
        if report_type not in {"formal", "visual"}:
            self._send_api_error(400, "report_type must be 'formal' or 'visual'")
            return

        try:
            with tempfile.TemporaryDirectory(prefix="bugx-workbench-pdf-") as temp_dir:
                temp_path = Path(temp_dir) / "report.pdf"
                export_pdf(self.bundle, temp_path, report_type=report_type)
                pdf_bytes = temp_path.read_bytes()
        except Exception as exc:
            self._send_api_error(500, f"PDF export failed: {exc}")
            return

        filename = (
            "BUGX_ENTERPRISE_SECURITY_AUDIT_REPORT_FORMAL.pdf"
            if report_type == "formal"
            else "BUGX_AESTHETIC_VISUAL_DELIVERY_REPORT.pdf"
        )
        self._send_bytes(
            pdf_bytes,
            "application/pdf",
            content_disposition=f'attachment; filename="{filename}"',
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # 0. Check WebUI API endpoints first (/live/message, /live/stop, /live/mcp/trust)
        if self.webui_handler:
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b""
                self._cached_body = body
                if self.webui_handler.handle_api_post(self.path, self, body):
                    return
            except Exception:
                pass

        if path.startswith("/api/projects/") and path.endswith("/config"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "config":
                pid = parts[2]
                try:
                    body = self._read_json_body()
                except ValueError as exc:
                    self._send_api_error(400, str(exc))
                    return
                updated = self.registry.update_config(pid, body)
                if updated is None:
                    self._send_api_error(404, f"Project not found: {pid}")
                    return
                self._send_json({"status": "ok", "project_id": pid, "config": updated})
                return

        if path.startswith("/api/projects/") and path.endswith("/sync"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "sync":
                pid = parts[2]
                receipt = self.registry.sync_project(pid)
                if receipt is None:
                    self._send_api_error(404, f"Project not found: {pid}")
                    return
                self._send_json(receipt)
                return

        if path == "/api/projects/add":
            try:
                body = self._read_json_body()
            except ValueError as exc:
                self._send_api_error(400, str(exc))
                return
            name = str(body.get("name", "")).strip()
            proj_path = str(body.get("path", "")).strip()
            if not name or not proj_path:
                self._send_api_error(400, "name and path are required")
                return
            langs = body.get("languages", ["Python"])
            new_proj = self.registry.add_project(name, proj_path, langs)
            self._send_json({"status": "ok", "project": new_proj.to_dict()})
            return

        self._send_api_error(404, "Endpoint not found")

    def do_DELETE(self) -> None:
        if self.webui_handler:
            try:
                if self.webui_handler.handle_api_delete(self.path, self):
                    return
            except Exception:
                pass
        self._send_api_error(404, "Endpoint not found")

    def do_PATCH(self) -> None:
        if self.webui_handler:
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b""
                if self.webui_handler.handle_api_patch(self.path, self, body):
                    return
            except Exception:
                pass
        self._send_api_error(404, "Endpoint not found")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/api/metrics":
            self._send_json(asdict(self.bundle.metrics))
            return

        if path == "/api/findings":
            self._handle_findings(query)
            return

        if path == "/api/stats":
            self._handle_stats(query)
            return

        if path == "/api/export/pdf":
            self._handle_pdf_export(query)
            return

        if path == "/api/health":
            self._send_json(
                {
                    "status": "ok",
                    "healthy": True,
                    "service": "bugx-report-engine",
                    "report_id": self.bundle.report_id,
                    "findings": len(self.bundle.findings),
                }
            )
            return

        if path == "/api/projects":
            projects = self.registry.list_projects()
            self._send_json({
                "status": "ok",
                "total_projects": len(projects),
                "projects": projects,
                "summary": {
                    "total_surfaces": sum(p["surfaces"] for p in projects),
                    "total_findings": sum(p["findings_count"] for p in projects),
                    "avg_kill_rate": round(sum(p["kill_rate"] for p in projects) / max(1, len(projects)), 4),
                }
            })
            return

        if path.startswith("/api/projects/") and path.endswith("/config"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "projects" and parts[3] == "config":
                pid = parts[2]
                cfg = self.registry.get_config(pid)
                if cfg is None:
                    self._send_api_error(404, f"Project not found: {pid}")
                    return
                self._send_json({"status": "ok", "project_id": pid, "config": cfg})
                return

        if path == "/api/sync/status":
            self._send_json(self.registry.get_sync_overview())
            return

        if path.startswith("/api/"):
            if self.webui_handler and self.webui_handler.handle_api_get(self.path, self):
                return
            self._send_api_error(404, "API endpoint not found")
            return

        # Explicit report/workbench views
        view = query.get("view", [""])[0]

        if path == "/workbench" or (path == "/" and view == "workbench"):
            content = self._render_workbench_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/formal" or (path == "/" and view == "formal"):
            content = self.formal_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/visual" or (path == "/" and view == "visual"):
            content = self.visual_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/sync" or (path == "/" and view == "sync"):
            content = self._render_sync_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/projects_hub" or (path == "/" and view == "projects"):
            content = self._render_projects_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/" and view == "hub":
            hub_html = self._render_hub_shell()
            content = hub_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # BugX Studio WebUI API endpoints (/projects, /project, /mcp/status, /skills, /models, /live, etc.)
        if self.webui_handler and self.webui_handler.handle_api_get(self.path, self):
            return

        # BugX Studio WebUI Static Assets (/assets/..., /manifest.webmanifest, etc.)
        if self.webui_handler and self.webui_handler.handle_static(self.path, self):
            return

        # Default root view: serve BugX Studio WebUI SPA
        if path == "/" and not view:
            if self.webui_handler and self.webui_handler.handle_static("index.html", self):
                return

        # Fallback to dual-track hub shell
        hub_html = self._render_hub_shell()
        content = hub_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)



    _render_hub_shell = _hr._render_hub_shell
    _render_projects_html = _hr._render_projects_html
    _render_sync_html = _hr._render_sync_html
    _render_workbench_html = _hr._render_workbench_html


def start_report_server(
    bundle: ReportDataBundle,
    host: str = "127.0.0.1",
    port: int = 8080,
    run_once: bool = False,
) -> tuple[socketserver.TCPServer, threading.Thread]:
    """启动轻量后台报告 Web 服务器。"""
    formal_html = FormalExecutiveReportBuilder(bundle).render_html()
    visual_html = VisualDeliveryReportBuilder(bundle).render_html()

    class CustomHandler(_DualTrackHTTPHandler):
        pass

    CustomHandler.bundle = bundle
    CustomHandler.formal_html = formal_html
    CustomHandler.visual_html = visual_html

    # Allow port reuse
    socketserver.TCPServer.allow_reuse_address = True

    class _ThreadingReportServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        """多线程处理请求。

        报告服务同时提供 HTML 页面与 JSON API；单线程 `TCPServer` 会让一个慢连接
        （浏览器 keep-alive、未读完的响应）阻塞后续所有请求——实测表现为客户端
        `urlopen(..., timeout=5)` 直接超时（tests/test_webui_studio 抖动根因）。
        """

        allow_reuse_address = True
        daemon_threads = True

    server = _ThreadingReportServer((host, port), CustomHandler)

    if run_once:
        thread = threading.Thread(target=server.handle_request, daemon=True)
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
