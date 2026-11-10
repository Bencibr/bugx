#!/usr/bin/env python3
"""探测 codebase-memory-mcp 安装、版本和当前仓库索引状态。

它是功能拆分和业务链路的首选语义增强层，但不是全量覆盖的硬依赖：缺失或索引
异常返回 DEGRADED，调用方必须取得用户的回退决定；本地 symbol_index/project_graph
仍负责不漏文件、不漏配置资产的覆盖事实。审计必须在准备记录中保存本命令结果。
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
import json
import os
import re
import signal
import shutil
import subprocess
from pathlib import Path

if __package__:
    from ..runtime.tool_install_targets import OPTIONAL_TOOL_PACKAGE_SPECS
else:
    from core.runtime.tool_install_targets import OPTIONAL_TOOL_PACKAGE_SPECS

PROJECT_URL = "https://github.com/DeusData/codebase-memory-mcp"
RELEASE_URL = f"{PROJECT_URL}/releases"
SOURCE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs",
    ".swift", ".c", ".h", ".cc", ".cpp", ".cs", ".rb", ".php", ".vue",
}
EXCLUDED = {".git", ".opencode", "node_modules", "dist", "build", "vendor", ".venv"}
# The graph is an optional accelerator. A cold CLI start loads sqlite and
# repository metadata; measured 18~24s on this repo (170MB index), so the
# floor must sit above the observed cold start or a healthy install flips to
# DEGRADED. The registry may raise this further from telemetry.
CLI_TIMEOUT_SECONDS = 45
CLI_TIMEOUT_MAX_SECONDS = 120
MAX_VERSION_CANDIDATES = 8


def _cli_timeout_seconds() -> float:
    raw = (
        os.environ.get("BUGX_CODEBASE_MEMORY_TIMEOUT_SECONDS")
        or os.environ.get("BUGX_PROBE_TIMEOUT_CODEBASE_MEMORY")
        or os.environ.get("BUGX_PROBE_TIMEOUT_SECONDS")
    )
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return min(CLI_TIMEOUT_MAX_SECONDS, max(0.5, value))
        except ValueError:
            pass
    return CLI_TIMEOUT_SECONDS


def _probe_environment() -> dict[str, str]:
    try:
        try:
            from .probe_registry import normalized_tool_environment
        except (ImportError, ValueError):
            try:
                from .probe_registry import normalized_tool_environment
            except ImportError:
                from core.probes.probe_registry import normalized_tool_environment
        return normalized_tool_environment()
    except ImportError:
        return dict(os.environ)


def _pinned_cli_version() -> str | None:
    spec = OPTIONAL_TOOL_PACKAGE_SPECS.get("codebase-memory", "").strip()
    match = re.fullmatch(r"codebase-memory-mcp==(?P<version>\d+(?:\.\d+){1,3})", spec)
    return match.group("version") if match else None


def _reported_cli_version(value: str) -> str | None:
    match = re.fullmatch(
        r"\s*codebase-memory-mcp\s+(?P<version>\d+(?:\.\d+){1,3})\s*",
        value,
    )
    return match.group("version") if match else None


def _binary_candidates(
    environment: dict[str, str], binary: str | None = None,
) -> tuple[list[str], bool]:
    """Return bounded, deduplicated candidates, preferring managed user bin dirs.

    A caller-supplied binary is an explicit selection and remains authoritative.
    Otherwise, inspect the configured uv/XDG executable directories before
    arbitrary PATH entries so an unrelated workspace binary cannot shadow the
    installed tool.
    """
    if binary:
        return [str(binary)], False

    home = Path(environment.get("HOME") or str(Path.home()))
    directories: list[str] = []
    for value in (
        environment.get("UV_TOOL_BIN_DIR"),
        environment.get("XDG_BIN_HOME"),
        str(Path(environment["XDG_DATA_HOME"]).parent / "bin")
        if environment.get("XDG_DATA_HOME") else None,
        str(home / ".local" / "bin"),
        *environment.get("PATH", "").split(os.pathsep),
    ):
        if not value:
            continue
        directory = str(Path(value).expanduser())
        if directory not in directories:
            directories.append(directory)

    candidates: list[str] = []
    seen: set[str] = set()
    truncated = False
    for directory in directories:
        candidate = shutil.which("codebase-memory-mcp", path=directory)
        if not candidate:
            continue
        identity = os.path.normcase(os.path.realpath(candidate))
        if identity in seen:
            continue
        if len(candidates) >= MAX_VERSION_CANDIDATES:
            truncated = True
            break
        seen.add(identity)
        candidates.append(candidate)
    return candidates, truncated


def _resolve_binary(
    environment: dict[str, str], expected_version: str, binary: str | None = None,
    *, timeout_seconds: float = 10.0,
) -> tuple[str | None, str | None, list[dict[str, str]], bool]:
    """Find an executable whose strict ``--version`` output matches the pin."""
    candidates, truncated = _binary_candidates(environment, binary)
    observations: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            result = _run(
                [candidate, "--version"], timeout=timeout_seconds, env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            observations.append({"path": candidate, "version": "", "detail": str(exc)})
            continue
        version_text = (result.stdout or result.stderr).strip()
        reported_version = _reported_cli_version(version_text)
        if result.returncode == 0 and reported_version == expected_version:
            return candidate, version_text, observations, truncated
        observations.append({
            "path": candidate,
            "version": reported_version or "",
            "detail": version_text or f"--version exited {result.returncode}",
        })
    return None, None, observations, truncated


def resolve_compatible_binary(
    *, environment: dict[str, str] | None = None,
    expected_version: str | None = None,
    binary: str | None = None,
    timeout_seconds: float = 10.0,
) -> str | None:
    """Return only a CLI that reports the repository's exact pinned version."""
    environment = environment or _probe_environment()
    expected_version = expected_version or _pinned_cli_version()
    if not expected_version:
        return None
    executable, _, _, _ = _resolve_binary(
        environment, expected_version, binary, timeout_seconds=timeout_seconds,
    )
    return executable


def _run(args: list[str], *, stdin: str | None = None,
         timeout: float = 30, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the optional indexer in an isolated process group.

    ``codebase-memory-mcp cli`` may start or contact a daemon.  A plain
    ``subprocess.run(timeout=...)`` only terminates the CLI parent; a child
    daemon can keep the captured pipe open and make the caller wait forever.
    The probe is an optional accelerator, so a hung daemon must degrade
    quickly and never hold the BugX startup gate.
    """
    process = subprocess.Popen(
        args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=cwd, start_new_session=(os.name != "nt"),
        env=env or _probe_environment(),
    )
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        else:  # pragma: no cover - Windows does not support process groups here.
            process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            args, timeout, output=stdout, stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _git_head(root: Path) -> str | None:
    try:
        result = _run(["git", "rev-parse", "HEAD"], timeout=10, cwd=root)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _source_count(root: Path) -> int:
    count = 0
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES \
                and not any(part in EXCLUDED for part in path.parts):
            count += 1
    return count


def _list_projects_with_retry(
    executable: str, environment: dict[str, str],
) -> tuple[subprocess.CompletedProcess[str] | None, str, bool]:
    """Run the machine-readable project-list command with one bounded retry.

    A slow cold start is not a broken accelerator. Terminal outcomes such as a
    non-zero exit code or unparsable output are returned immediately so the
    caller reports them once instead of paying a second start. The third item
    is the machine-checkable ``transient`` flag the caller records.
    """
    base = _cli_timeout_seconds()
    retried = f"首次 list_projects 超时（>{base:.0f}s），已按双倍预算重试"
    for attempt, timeout in enumerate((base, min(CLI_TIMEOUT_MAX_SECONDS, base * 2.0)), start=1):
        try:
            result = list_projects_json(
                executable, environment=environment, timeout_seconds=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            if attempt == 1:
                continue
            return None, f"{retried}，仍然超时（>{timeout:.0f}s）：{exc}", True
        except OSError as exc:
            return None, f"list_projects 执行异常：{exc}", False
        return result, (retried if attempt > 1 else ""), False
    return None, retried, True


def list_projects_json(
    executable: str,
    *,
    environment: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Read the machine-readable project payload used by every BugX path."""
    timeout = _cli_timeout_seconds() if timeout_seconds is None else min(
        CLI_TIMEOUT_MAX_SECONDS, max(0.5, float(timeout_seconds)),
    )
    return _run(
        [
            executable, "cli", "--quiet", "list_projects",
            "--format", "json", "--detail", "stats",
        ],
        timeout=timeout, env=environment or _probe_environment(),
    )


def index_status_json(
    executable: str,
    project: str,
    *,
    environment: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Read structured index status without the outer MCP response envelope."""
    timeout = _cli_timeout_seconds() if timeout_seconds is None else min(
        CLI_TIMEOUT_MAX_SECONDS, max(0.5, float(timeout_seconds)),
    )
    return _run(
        [
            executable, "cli", "--quiet", "index_status",
            "--project", project, "--format", "json", "--diagnostics", "summary",
        ],
        timeout=timeout, env=environment or _probe_environment(),
    )


def _unsupported_output_detail(stdout: str, stderr: str) -> str:
    """Explain an unusable machine-readable ``list_projects`` payload.

    Do not infer a version boundary from an unexpected output: require the
    documented ``--format json`` projects contract and explain that requirement.
    """
    payload: object = None
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        return (
            "CLI 返回 MCP envelope，而探针需要 `list_projects --format json --detail stats` "
            "产生的项目及节点统计结构；请确认 CLI 支持这两个参数并使用机器可读格式"
        )
    return stderr.strip() or (
        "list_projects 未返回有效 JSON 项目结构；请确认 CLI 支持 `--format json --detail stats`"
    )


def inspect(root: Path, binary: str | None = None) -> dict:
    root = root.resolve()
    environment = _probe_environment()
    checks: list[dict] = []
    expected_version = _pinned_cli_version()
    if expected_version is None:
        checks.append({
            "name": "supported_version", "ok": False,
            "detail": "core/tool_install_targets.py 中的 codebase-memory 固定规格无效",
        })
        return _report(root, checks, None, None)
    candidates, _ = _binary_candidates(environment, binary)
    if not candidates:
        checks.append({"name": "binary", "ok": False, "detail": "未安装"})
        checks.append({
            "name": "supported_version", "ok": False,
            "detail": f"没有可执行候选；需要固定版本 {expected_version}。",
            "expected": expected_version,
        })
        return _report(root, checks, None, None)

    executable, version_text, observations, candidates_truncated = _resolve_binary(
        environment, expected_version, binary,
    )
    checks.append({
        "name": "binary", "ok": executable is not None,
        "detail": executable or f"发现 {len(candidates)} 个候选，但都未匹配固定版本",
    })
    if executable is None:
        first_version_text = next(
            (item["detail"] for item in observations if item.get("version")), None,
        )
        checks.append({
            "name": "validated_version",
            "ok": any(bool(item.get("version")) for item in observations),
            "detail": first_version_text or "所有候选均无法读取有效版本号",
            "expected": "codebase-memory-mcp <固定版本号>",
        })
        observed = "; ".join(
            f"{item['path']}={item['version'] or item['detail']}"
            for item in observations
        ) or "无候选版本信息"
        if candidates_truncated:
            observed += f"；候选数达到上限 {MAX_VERSION_CANDIDATES}，其余未执行"
        checks.append({
            "name": "supported_version", "ok": False,
            "detail": (
                f"没有候选匹配固定版本 {expected_version}（{observed}）。"
                "为避免调用不兼容参数，不执行 list_projects 或 index_repository。"
            ),
            "expected": expected_version,
        })
        return _report(root, checks, first_version_text, None)

    skipped = "; 跳过不匹配候选: " + ", ".join(
        f"{item['path']}={item['version'] or item['detail']}"
        for item in observations
    ) if observations else ""
    checks.extend((
        {"name": "validated_version", "ok": True, "detail": version_text},
        {
            "name": "supported_version", "ok": True,
            "detail": f"已选择匹配固定规格的可执行文件 {executable}{skipped}",
            "expected": expected_version,
        },
    ))
    projects_result, retry_note, transient = _list_projects_with_retry(executable, environment)
    if projects_result is None:
        checks.append({
            "name": "cli",
            "ok": False,
            "detail": retry_note or "list_projects 失败",
            "transient": transient,
        })
        # Keep the version that was already verified: dropping it made doctor
        # report "版本 未安装" for a binary that answers --version correctly.
        return _report(root, checks, version_text, None)
    if retry_note:
        checks.append({"name": "cli_retry", "ok": True, "detail": retry_note})
    payload: object = None
    try:
        payload = json.loads(projects_result.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
        # Unexpected, non-object, or malformed output is not an empty project
        # list: it cannot be trusted as evidence that the project is unindexed.
        checks.append({
            "name": "cli", "ok": False,
            "detail": _unsupported_output_detail(
                projects_result.stdout or "", projects_result.stderr or "",
            ),
        })
        return _report(root, checks, version_text, None)
    projects = payload["projects"]
    checks.append({
        "name": "cli", "ok": projects_result.returncode == 0,
        "detail": "list_projects 可用" if projects_result.returncode == 0
        else projects_result.stderr.strip(),
    })
    project = next(
        (
            item for item in projects
            if isinstance(item, dict)
            and Path(str(item.get("root_path", ""))).resolve() == root
        ),
        None,
    )
    index_failure = ""
    index_created = False
    if not project and executable:
        # 恶性设计治理 (DOC-180)：发现未建立索引，直接就地自动构建知识图谱，严禁消极降级回退
        try:
            idx_res = _run(
                [
                    executable, "cli", "--quiet", "index_repository",
                    "--repo-path", str(root.resolve()),
                ],
                timeout=60, env=environment,
            )
            if idx_res.returncode == 0:
                projects_result, retry_note, transient = _list_projects_with_retry(
                    executable, environment,
                )
                if projects_result is None:
                    index_failure = retry_note or "索引后读取项目列表失败"
                    if transient:
                        index_failure += "（瞬态超时）"
                elif projects_result.returncode != 0:
                    index_failure = (
                        projects_result.stderr.strip()
                        or f"索引后读取项目列表退出码 {projects_result.returncode}"
                    )
                else:
                    try:
                        projects_payload = json.loads(projects_result.stdout)
                    except (json.JSONDecodeError, TypeError):
                        projects_payload = None
                    if not isinstance(projects_payload, dict) or not isinstance(
                        projects_payload.get("projects"), list,
                    ):
                        index_failure = _unsupported_output_detail(
                            projects_result.stdout or "", projects_result.stderr or "",
                        )
                    else:
                        projects = projects_payload["projects"]
                        project = next(
                            (
                                item for item in projects
                                if isinstance(item, dict)
                                and Path(str(item.get("root_path", ""))).resolve() == root
                            ),
                            None,
                        )
                        index_created = project is not None
                        if retry_note:
                            checks.append({
                                "name": "cli_retry", "ok": True,
                                "detail": f"索引后{retry_note}",
                            })
                        if not project:
                            index_failure = "索引命令成功，但索引后项目列表仍不包含当前仓库"
            else:
                detail = (idx_res.stderr or idx_res.stdout or "").strip()
                index_failure = detail or f"index_repository 退出码 {idx_res.returncode}"
        except Exception as exc:
            index_failure = f"index_repository 调用失败：{type(exc).__name__}: {exc}"
    checks.append({
        "name": "project_index", "ok": project is not None,
        "detail": (
            "当前仓库索引已自动创建并复核" if index_created
            else "当前仓库索引记录已存在" if project
            else index_failure or "当前仓库尚未建立索引"
        ),
    })
    if project:
        nodes = project.get("nodes")
        edges = project.get("edges")
        stats_ok = type(nodes) is int and nodes >= 0 and type(edges) is int and edges >= 0
        if stats_ok:
            source_count = _source_count(root)
            graph_ok = nodes > 0 and not (source_count > 5 and nodes <= 2)
            graph_detail = f"nodes={nodes}, edges={edges}, source_files={source_count}"
        else:
            # list_projects omits counts unless detail=stats is requested. Missing
            # fields are unknown evidence, not proof that the graph has zero nodes.
            graph_ok = False
            graph_detail = "list_projects --detail stats 未返回有效的 nodes/edges，无法判定图规模"
        checks.append({
            "name": "graph_population", "ok": graph_ok,
            "detail": graph_detail,
        })
        current_head = _git_head(root)
        indexed_head = (project.get("git") or {}).get("head_sha")
        generation_ok = current_head is None or not indexed_head or indexed_head == current_head
        checks.append({
            "name": "index_generation", "ok": generation_ok,
            "detail": f"index={indexed_head or 'unknown'}, current={current_head or 'non-git'}",
        })
    return _report(root, checks, version_text, project)


def _report(root: Path, checks: list[dict], version: str | None,
            project: dict | None) -> dict:
    ready = all(check["ok"] for check in checks)
    return {
        "status": "READY" if ready else "DEGRADED",
        # required_check means the startup environment must inspect and report
        # this capability. required remains false because local enumeration is
        # a valid fallback for small/offline projects.
        "required_check": True,
        "required": False,
        "recommended": True,
        "hard_dependency": False,
        "role": "semantic-graph-enhancement",
        "required_for": [
            "functional-minimization",
            "business-flow-discovery",
            "dynamic-relationship-review",
        ],
        "root": str(root),
        "version": version,
        "validated_version": (
            "readable-version"
            if any(
                check.get("name") == "validated_version" and check.get("ok") is True
                for check in checks
            ) else None
        ),
        "project": project.get("name") if project else None,
        "checks": checks,
        "fallback": "symbol_index.py + project_graph.py",
        "install": f"pipx install --force {OPTIONAL_TOOL_PACKAGE_SPECS['codebase-memory']}",
        "configure": "codebase-memory-mcp install，然后重启 AI 编程工具并索引项目",
        "source": RELEASE_URL,
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(
        f"[{report['status']}] codebase-memory-mcp（环境必检；语义拆分首选，非全量硬依赖）"
    )
    for check in report["checks"]:
        print(f"  {'✓' if check['ok'] else '!'} {check['name']}: {check['detail']}")
    if report["status"] != "READY":
        print(f"  → 推荐：{report['install']}")
        print(f"  → 配置：{report['configure']}")
        print(f"  → 本轮回退：{report['fallback']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "status"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    report = inspect(args.root)
    print_report(report, args.json)
    return 1 if args.strict and report["status"] != "READY" else 0


if __name__ == "__main__":
    raise SystemExit(main())
