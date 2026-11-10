#!/usr/bin/env python3
"""Deterministic, bounded graph orchestration for native BugX agents.

The engine owns graph state, immutable input snapshots, artifact receipts, and
controlled checks.  It does not launch native agents or call host UI APIs; the
Host MCP channel binds native agent identities and dispatches the returned
Task prompt through its own native interface.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl.
    msvcrt = None  # type: ignore[assignment]

try:
    import core.bugx_paths as bugx_paths
except ImportError:
    from core import bugx_paths

try:
    import core.secure_atomic as secure_atomic
except ImportError:
    from core import secure_atomic


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3
MAX_CONCURRENCY = 16
MAX_SPEC_BYTES = 64 * 1024
MAX_TASK_TEXT_LENGTH = 8 * 1024
MAX_TARGET_FILES = 100
MAX_SNAPSHOT_FILES = 100_000
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024
MAX_CONTEXT_FILE_BYTES = 512 * 1024
MAX_CONTEXT_BYTES = 4 * 1024 * 1024
MAX_ENVELOPE_BYTES = 128 * 1024
MAX_ATTACHMENT_BYTES = 256 * 1024
MAX_ATTACHMENTS_BYTES = 300 * 1024
MAX_CHECK_FILES = 20
MAX_CHECK_TIMEOUT_SECONDS = 300
MAX_CHECK_OUTPUT_BYTES = 32 * 1024
GRAPH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NATIVE_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
STAGES = ("Recon", "Repro", "Review", "Patch", "Check", "Report")
ACTIVE_TASK_STATES = frozenset({"assigned", "running"})
RESERVED_TASK_STATES = frozenset({"offered", *ACTIVE_TASK_STATES})
TERMINAL_TASK_STATES = frozenset({"complete", "failed", "cancelled", "superseded"})
IGNORED_NAMES = frozenset({
    ".bugx", ".git", ".hg", ".svn", ".DS_Store", "node_modules", "venv",
    ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
})

STAGE_INSTRUCTIONS = {
    "Recon": (
        "Inspect the immutable project snapshot and identify concrete candidate behavior. "
        "Do not modify source files. Keep observations separate from hypotheses."
    ),
    "Repro": (
        "Produce a minimal, repeatable reproduction for the candidate using the snapshot. "
        "Do not modify source files and preserve exact commands, inputs, and observed output."
    ),
    "Review": (
        "Independently review the candidate and reproduction artifacts. Verify the claimed "
        "cause, scope, and evidence. Do not rely on the producer's conclusion alone."
    ),
    "Patch": (
        "Apply the smallest change needed to address the reviewed issue, staying inside the "
        "declared modification and ownership boundaries. Preserve all stated invariants."
    ),
    "Check": (
        "Submit a bounded CheckPlan with tests under tests/ using graph_run_check. Do not "
        "claim a check passed without its returned Observation."
    ),
    "Report": (
        "Compile the final report from checkpointed artifacts and Observations. Clearly mark "
        "unsupported or unverified claims; do not invent host or runtime evidence."
    ),
}


class GraphError(RuntimeError):
    """Invalid graph transition, identity, or bounded input."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _text(value: object, name: str, *, maximum: int = MAX_TASK_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GraphError(f"{name} 必须是非空字符串")
    if len(value) > maximum:
        raise GraphError(f"{name} 过长（>{maximum}）")
    return value


def _string_list(
    value: object, name: str, *, minimum: int = 0, maximum: int = 64,
) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum or len(value) > maximum:
        raise GraphError(f"{name} 必须是 {minimum}..{maximum} 项字符串数组")
    result = []
    for index, item in enumerate(value):
        result.append(_text(item, f"{name}[{index}]"))
    return result


def _safe_relative_path(value: object, name: str = "path") -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise GraphError(f"{name} 必须是非空相对路径")
    if "\\" in value or "\x00" in value or ":" in value:
        raise GraphError(f"{name} 格式无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GraphError(f"{name} 必须是规范化的项目相对路径")
    if any(part in IGNORED_NAMES for part in path.parts):
        raise GraphError(f"{name} 位于被排除的项目目录")
    normalized = path.as_posix()
    if normalized != value:
        raise GraphError(f"{name} 必须是规范化的项目相对路径")
    return normalized


def _normalize_task_input(arguments: Mapping[str, object]) -> dict[str, object]:
    target_files = _string_list(
        arguments.get("target_files"), "target_files", minimum=1,
        maximum=MAX_TARGET_FILES,
    )
    normalized_paths = [_safe_relative_path(item, "target_files") for item in target_files]
    if len(set(normalized_paths)) != len(normalized_paths):
        raise GraphError("target_files 不得包含重复路径")
    patch_requested = arguments.get("patch_requested", False)
    if not isinstance(patch_requested, bool):
        raise GraphError("patch_requested 必须是布尔值")
    concurrency = arguments.get("max_concurrency", 1)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int):
        raise GraphError("max_concurrency 必须是整数")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise GraphError(f"max_concurrency 必须位于 1..{MAX_CONCURRENCY}")
    value: dict[str, object] = {
        "target": _text(arguments.get("target"), "target", maximum=2048),
        "goal": _text(arguments.get("goal"), "goal"),
        "modifications": _string_list(arguments.get("modifications"), "modifications", minimum=1),
        "invariants": _string_list(arguments.get("invariants"), "invariants", minimum=1),
        "ownership_boundaries": _string_list(
            arguments.get("ownership_boundaries"), "ownership_boundaries", minimum=1,
        ),
        "acceptance_criteria": _string_list(
            arguments.get("acceptance_criteria"), "acceptance_criteria", minimum=1,
        ),
        "target_files": normalized_paths,
        "patch_requested": patch_requested,
        "max_concurrency": concurrency,
    }
    if len(_canonical_json(value)) > MAX_SPEC_BYTES:
        raise GraphError(f"Task Spec 超过 {MAX_SPEC_BYTES} 字节")
    return value


def assemble_task_prompt(
    task_input: Mapping[str, object], stage: str, attempt: int,
    feedback: Mapping[str, object] | None = None,
) -> str:
    """Assemble one canonical, self-contained native Task prompt.

    All caller-authored lines are included verbatim.  A single function is
    shared by initial tasks and revision attempts so neither path silently
    drops scope, invariants, ownership, or acceptance evidence.
    """
    if stage not in STAGE_INSTRUCTIONS:
        raise GraphError(f"未知 Graph 阶段: {stage}")
    sections = [
        "BugX native Task",
        f"Stage: {stage} (attempt {attempt}/{MAX_ATTEMPTS})",
        f"Stage objective: {STAGE_INSTRUCTIONS[stage]}",
        f"Target: {task_input['target']}",
        f"Goal: {task_input['goal']}",
        "Requested modifications (verbatim):",
        *[f"- {item}" for item in task_input["modifications"]],
        "Invariants (must remain true):",
        *[f"- {item}" for item in task_input["invariants"]],
        "Ownership boundaries:",
        *[f"- {item}" for item in task_input["ownership_boundaries"]],
        "Observable acceptance criteria:",
        *[f"- {item}" for item in task_input["acceptance_criteria"]],
        "Declared snapshot files:",
        *[f"- {item}" for item in task_input["target_files"]],
        "Execution boundaries:",
        "- Work only in the graph's private snapshot workspace.",
        "- Do not call host UI APIs or claim that a native host action occurred.",
        "- Do not invent observations, test results, identities, or artifact contents.",
        "- Return a checkpoint with concrete evidence and an explicit outcome.",
    ]
    if feedback is not None:
        sections.extend([
            "Revision feedback from the previous attempt (verbatim):",
            f"Reason: {feedback.get('reason', '')}",
            "Context:",
            json.dumps(feedback.get("context", {}), ensure_ascii=False, sort_keys=True),
        ])
    return "\n".join(sections)


def _new_task(
    state: dict[str, Any], stage: str, attempt: int,
    dependencies: Sequence[str], feedback: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    graph_id = state["graph_id"]
    task_id = "task_" + hashlib.sha256(
        f"{graph_id}:{stage}:{attempt}".encode("utf-8")
    ).hexdigest()[:24]
    task_input = state["task_input"]
    return {
        "task_id": task_id,
        "stage": stage,
        "attempt": attempt,
        "dependencies": list(dependencies),
        "status": "pending",
        "native_agent_id": None,
        "fencing_token": 0,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "feedback": dict(feedback or {}),
        "prompt": assemble_task_prompt(task_input, stage, attempt, feedback),
        "check_observation": None,
        "checkpoint": None,
        "failure_context": None,
    }


def _copy_snapshot(source: Path, destination: Path) -> dict[str, object]:
    """Copy regular project files into a private workspace without following links."""
    if not source.is_dir() or source.is_symlink():
        raise GraphError(f"snapshot source is not a regular directory: {source}")
    source = source.resolve()
    destination = Path(os.path.abspath(str(destination)))
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise GraphError("graph private root must be outside the source project")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    count = 0
    total_bytes = 0
    excluded_links: list[str] = []
    try:
        for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
            current_path = Path(current)
            relative_dir = current_path.relative_to(source)
            kept_dirs: list[str] = []
            for name in sorted(dirs):
                if name in IGNORED_NAMES:
                    continue
                source_dir = current_path / name
                if source_dir.is_symlink():
                    if len(excluded_links) < 1000:
                        excluded_links.append((relative_dir / name).as_posix())
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            target_dir = destination / relative_dir
            target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name in sorted(files):
                if name in IGNORED_NAMES:
                    continue
                source_file = current_path / name
                relative = source_file.relative_to(source)
                if source_file.is_symlink():
                    if len(excluded_links) < 1000:
                        excluded_links.append(relative.as_posix())
                    continue
                try:
                    info = source_file.stat(follow_symlinks=False)
                except OSError as exc:
                    raise GraphError(f"无法安全读取 snapshot 文件 {relative}: {exc}") from exc
                if not source_file.is_file() or source_file.is_symlink():
                    continue
                if info.st_size < 0 or total_bytes + info.st_size > MAX_SNAPSHOT_BYTES:
                    raise GraphError(f"snapshot 超过 {MAX_SNAPSHOT_BYTES} 字节")
                count += 1
                if count > MAX_SNAPSHOT_FILES:
                    raise GraphError(f"snapshot 文件数超过 {MAX_SNAPSHOT_FILES}")
                target_file = destination / relative
                target_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                source_fd: int | None = None
                target_fd: int | None = None
                try:
                    source_fd = os.open(source_file, flags)
                    source_stat = os.fstat(source_fd)
                    if not stat.S_ISREG(source_stat.st_mode):
                        raise GraphError(f"snapshot 仅接受普通文件: {relative}")
                    target_fd = os.open(
                        target_file,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    with os.fdopen(source_fd, "rb", closefd=False) as src, os.fdopen(
                        target_fd, "wb", closefd=False,
                    ) as dst:
                        remaining = MAX_SNAPSHOT_BYTES - total_bytes
                        while True:
                            block = src.read(min(1024 * 1024, remaining + 1))
                            if not block:
                                break
                            total_bytes += len(block)
                            if total_bytes > MAX_SNAPSHOT_BYTES:
                                raise GraphError(f"snapshot 超过 {MAX_SNAPSHOT_BYTES} 字节")
                            dst.write(block)
                        dst.flush()
                        os.fsync(dst.fileno())
                    try:
                        os.chmod(target_file, 0o700 if source_stat.st_mode & 0o111 else 0o600)
                    except OSError:
                        pass
                finally:
                    if source_fd is not None:
                        os.close(source_fd)
                    if target_fd is not None:
                        os.close(target_fd)
        return {
            "file_count": count,
            "bytes": total_bytes,
            "excluded_symlinks": excluded_links,
            "excluded_symlinks_truncated": len(excluded_links) >= 1000,
        }
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}


class AgentOrchestrator:
    """Persist and advance one deterministic Recon→Report task graph."""

    def __init__(self, private_root: Path, graph_id: str) -> None:
        if not isinstance(graph_id, str) or not GRAPH_ID_RE.fullmatch(graph_id):
            raise GraphError("graph_id 格式无效")
        self.private_root = Path(private_root).expanduser()
        self.graph_id = graph_id
        self.run_dir = self.private_root / graph_id
        self.state_path = self.run_dir / "graph.json"
        self.lock_path = self.run_dir / "graph.lock"
        self.workspace = self.run_dir / "workspace"

    @classmethod
    def start(
        cls, source_root: Path, private_root: Path, task_input: Mapping[str, object],
    ) -> tuple["AgentOrchestrator", dict[str, Any]]:
        source = Path(source_root).expanduser().resolve()
        private = Path(private_root).expanduser()
        bugx_paths.assert_no_symlink_components(private)
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            private_resolved = private.resolve(strict=True)
            private_resolved.relative_to(source)
        except ValueError:
            pass
        else:
            raise GraphError("graph private root must be outside the source project")
        if private_resolved == source:
            raise GraphError("graph private root must be outside the source project")
        graph_id = uuid.uuid4().hex
        run_dir = private_resolved / graph_id
        run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        orchestrator = cls(private_resolved, graph_id)
        try:
            manifest = _copy_snapshot(source, orchestrator.workspace)
            for relative in task_input["target_files"]:
                target_path = orchestrator.workspace / relative
                if target_path.is_symlink() or not target_path.is_file():
                    raise GraphError(f"声明的 snapshot 文件缺失或不安全: {relative}")
            first_task = _new_task({"graph_id": graph_id, "task_input": dict(task_input)}, "Recon", 1, [])
            state: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "graph_id": graph_id,
                "source_root_sha256": hashlib.sha256(str(source).encode("utf-8")).hexdigest(),
                "workspace": str(orchestrator.workspace),
                "created_at": _now(),
                "updated_at": _now(),
                "revision": 1,
                "status": "running",
                "task_input": dict(task_input),
                "snapshot": manifest,
                "max_concurrency": int(task_input["max_concurrency"]),
                "tasks": [first_task],
                "events": [{"event": "graph_started", "at": _now()}],
                "final_report": None,
            }
            with orchestrator._file_lock():
                orchestrator._save_unlocked(state)
            return orchestrator, first_task
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise

    @classmethod
    def open(cls, private_root: Path, graph_id: str) -> "AgentOrchestrator":
        instance = cls(private_root, graph_id)
        bugx_paths.assert_no_symlink_components(instance.run_dir)
        if instance.run_dir.is_symlink() or not instance.run_dir.is_dir():
            raise GraphError(f"graph 不存在: {graph_id}")
        return instance

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        bugx_paths.assert_no_symlink_components(self.run_dir)
        if self.run_dir.is_symlink() or not self.run_dir.is_dir():
            raise GraphError(f"graph 不存在或不安全: {self.graph_id}")
        with _LOCKS_GUARD:
            thread_lock = _LOCKS.setdefault(self.lock_path, threading.RLock())
        with thread_lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
            except OSError as exc:
                raise GraphError(f"无法安全打开 graph lock: {exc}") from exc
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                elif msvcrt is not None:
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:  # pragma: no cover - unsupported platform.
                    raise GraphError("当前平台不支持 graph 跨进程锁")
                yield
            finally:
                try:
                    if fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(descriptor)

    def _load_unlocked(self) -> dict[str, Any]:
        if self.state_path.is_symlink() or not self.state_path.is_file():
            raise GraphError("graph state 缺失或不安全")
        try:
            raw = self.state_path.read_bytes()
            if len(raw) > 16 * 1024 * 1024:
                raise GraphError("graph state 超过大小限制")
            state = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphError(f"graph state 无法读取: {exc}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise GraphError("graph state schema 无效")
        if state.get("graph_id") != self.graph_id or not isinstance(state.get("tasks"), list):
            raise GraphError("graph state 身份或任务列表无效")
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        state["revision"] = int(state.get("revision", 0)) + 1
        state["updated_at"] = _now()
        try:
            secure_atomic.atomic_write_json(
                self.state_path, state, mode=0o600, parent_mode=0o700,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise GraphError(f"graph state 写入失败: {exc}") from exc

    def snapshot(self) -> dict[str, Any]:
        with self._file_lock():
            return self._load_unlocked()

    @staticmethod
    def _task(state: Mapping[str, Any], task_id: object) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id:
            raise GraphError("task_id 必须是非空字符串")
        for task in state["tasks"]:
            if task.get("task_id") == task_id:
                return task
        raise GraphError(f"task 不属于当前 graph: {task_id}")

    @staticmethod
    def _dependencies_complete(state: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
        by_id = {item["task_id"]: item for item in state["tasks"]}
        for dependency in task.get("dependencies", []):
            parent = by_id.get(dependency)
            if parent is None or parent.get("status") != "complete":
                return False
        return True

    def next_task(self) -> dict[str, Any] | None:
        with self._file_lock():
            state = self._load_unlocked()
            if state["status"] != "running":
                return None
            offered = next((
                task for task in state["tasks"]
                if task.get("status") == "offered"
                and self._dependencies_complete(state, task)
            ), None)
            if offered is not None:
                return dict(offered)
            active = sum(task.get("status") in RESERVED_TASK_STATES for task in state["tasks"])
            if active >= state["max_concurrency"]:
                return None
            order = {stage: index for index, stage in enumerate(STAGES)}
            candidates = [
                task for task in state["tasks"]
                if task.get("status") == "pending" and self._dependencies_complete(state, task)
            ]
            if not candidates:
                return None
            task = min(candidates, key=lambda item: (order.get(item["stage"], 99), item["attempt"]))
            task["status"] = "offered"
            task["fencing_token"] = int(task.get("fencing_token", 0)) + 1
            task["offered_at"] = _now()
            state["events"].append({
                "event": "task_offered", "task_id": task["task_id"],
                "stage": task["stage"], "fencing_token": task["fencing_token"],
                "at": task["offered_at"],
            })
            self._save_unlocked(state)
            return dict(task)

    def bind_agent(self, task_id: str, native_agent_id: str) -> dict[str, Any]:
        if not isinstance(native_agent_id, str) or not NATIVE_AGENT_ID_RE.fullmatch(native_agent_id):
            raise GraphError("native_agent_id 格式无效")
        with self._file_lock():
            state = self._load_unlocked()
            if state["status"] != "running":
                raise GraphError(f"graph 不可调度: {state['status']}")
            task = self._task(state, task_id)
            if task["status"] in ACTIVE_TASK_STATES:
                if task.get("native_agent_id") == native_agent_id:
                    return dict(task)
                raise GraphError("task 已绑定到其他 native_agent_id")
            if task["status"] != "offered" or not self._dependencies_complete(state, task):
                raise GraphError("task 尚未依赖就绪或已不可调度")
            active = sum(
                item.get("status") in RESERVED_TASK_STATES and item.get("task_id") != task_id
                for item in state["tasks"]
            )
            if active >= state["max_concurrency"]:
                raise GraphError("graph 并发容量已满")
            if task["stage"] == "Review":
                producers = {
                    item.get("native_agent_id") for item in state["tasks"]
                    if item.get("stage") in {"Recon", "Repro"}
                    and item.get("native_agent_id")
                }
                if native_agent_id in producers:
                    raise GraphError("Review 必须由不同于 Recon/Repro 生产者的 native_agent_id 执行")
            task["native_agent_id"] = native_agent_id
            task["status"] = "assigned"
            task["started_at"] = _now()
            state["events"].append({
                "event": "agent_bound", "task_id": task_id,
                "stage": task["stage"], "native_agent_id": native_agent_id,
                "fencing_token": task["fencing_token"], "at": _now(),
            })
            self._save_unlocked(state)
            return dict(task)

    def _verify_worker(
        self, state: Mapping[str, Any], task_id: str, native_agent_id: str,
        fencing_token: int, *, require_active: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(native_agent_id, str) or not NATIVE_AGENT_ID_RE.fullmatch(native_agent_id):
            raise GraphError("native_agent_id 格式无效")
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 1:
            raise GraphError("fencing_token 必须是正整数")
        task = self._task(state, task_id)
        if task.get("native_agent_id") != native_agent_id:
            raise GraphError("Worker 身份与 Host 绑定的 native_agent_id 不匹配")
        if task.get("fencing_token") != fencing_token:
            raise GraphError("Worker fencing_token 已失效")
        if require_active and task.get("status") not in ACTIVE_TASK_STATES:
            raise GraphError(f"task 不处于可执行状态: {task.get('status')}")
        return task

    def _assemble_file_context(self, relative: str, data: bytes, content: str) -> str:
        """Assemble Tiered Progressive Context (imports + 1-hop ego + slice) for code files."""
        try:
            from core.retrieval.slice_extractor import SliceExtractor
            from core.retrieval.slice_normalizer import SliceNormalizer
        except ImportError:
            try:
                from core.retrieval.slice_extractor import SliceExtractor
                from core.retrieval.slice_normalizer import SliceNormalizer
            except ImportError:
                return content

        try:
            extractor = SliceExtractor(
                project_id=self.graph_id,
                snapshot_digest=hashlib.sha256(data).hexdigest(),
                generation=1,
            )
            specs = extractor.extract(data, file=relative)
            if not specs:
                return content

            import_lines: list[str] = []
            for line in content.splitlines()[:60]:
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")) and not stripped.startswith("from ."):
                    import_lines.append(stripped)

            ego_graph = {
                "center_node": relative,
                "neighbors": {
                    "callers": [],
                    "callees": [s.symbol_name for s in specs[:5] if s.symbol_name],
                },
            }

            assembled_parts: list[str] = []
            for spec in specs[:3]:
                part = SliceNormalizer.assemble_context(
                    spec,
                    imports=import_lines[:10],
                    ego_graph=ego_graph,
                    include_header=True,
                )
                assembled_parts.append(part)

            return "\n\n---\n\n".join(assembled_parts)
        except Exception:
            return content

    def context(
        self, task_id: str, native_agent_id: str, fencing_token: int,
    ) -> dict[str, Any]:
        with self._file_lock():
            state = self._load_unlocked()
            task = self._verify_worker(state, task_id, native_agent_id, fencing_token)
            if task["status"] == "assigned":
                task["status"] = "running"
                state["events"].append({
                    "event": "worker_started", "task_id": task_id,
                    "native_agent_id": native_agent_id, "at": _now(),
                })
                self._save_unlocked(state)
            workspace = Path(state["workspace"])
            try:
                workspace.resolve(strict=True).relative_to(self.run_dir.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise GraphError("graph workspace 不在运行副本内") from exc
            files: list[dict[str, str]] = []
            total = 0
            for relative in state["task_input"]["target_files"]:
                path = workspace / relative
                try:
                    resolved = bugx_paths.confined_regular_file(
                        workspace, path, error_cls=GraphError,
                    )
                    data = resolved.read_bytes()
                except (OSError, UnicodeError, GraphError) as exc:
                    raise GraphError(f"无法读取声明的 snapshot 文件 {relative}: {exc}") from exc
                if len(data) > MAX_CONTEXT_FILE_BYTES:
                    raise GraphError(f"snapshot 文件超过上下文上限: {relative}")
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise GraphError(f"声明的 snapshot 文件不是 UTF-8 文本: {relative}") from exc
                assembled_content = self._assemble_file_context(relative, data, content)
                total += len(assembled_content.encode("utf-8"))
                if total > MAX_CONTEXT_BYTES:
                    raise GraphError("snapshot 文件总上下文超过大小上限")
                files.append({"path": relative, "content": assembled_content})
            parent_artifacts = self._parent_artifacts(state, task)
            total += sum(len(item.get("content_base64", "")) for item in parent_artifacts)
            if total > MAX_CONTEXT_BYTES:
                raise GraphError("父工件与 snapshot 总上下文超过大小上限")
            return {
                "graph_id": self.graph_id,
                "task_id": task_id,
                "stage": task["stage"],
                "attempt": task["attempt"],
                "native_agent_id": native_agent_id,
                "fencing_token": fencing_token,
                "task_spec": task["prompt"],
                "files": files,
                "parent_artifacts": parent_artifacts,
                "snapshot": state["snapshot"],
            }

    def _parent_artifacts(
        self, state: Mapping[str, Any], task: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        by_id = {item["task_id"]: item for item in state["tasks"]}
        results: list[dict[str, Any]] = []
        ordered: list[dict[str, Any]] = []
        visited: set[str] = set()

        def add_ancestors(task_id: str) -> None:
            if task_id in visited:
                return
            visited.add(task_id)
            parent = by_id.get(task_id)
            if parent is None:
                raise GraphError("graph dependency 引用缺失")
            for dependency in parent.get("dependencies", []):
                add_ancestors(dependency)
            ordered.append(parent)

        for dependency in task.get("dependencies", []):
            add_ancestors(dependency)
        for parent in ordered:
            if not parent or not parent.get("checkpoint"):
                continue
            checkpoint_path = self.run_dir / parent["checkpoint"]["path"]
            if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
                raise GraphError("父工件信封缺失或不安全")
            try:
                envelope = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise GraphError(f"父工件信封读取失败: {exc}") from exc
            for item in envelope.get("attachments", []):
                digest = item.get("sha256")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    raise GraphError("父工件附件哈希无效")
                artifact_path = self.run_dir / "artifacts" / "sha256" / digest[:2] / digest
                if artifact_path.is_symlink() or not artifact_path.is_file():
                    raise GraphError("父工件附件缺失或不安全")
                data = artifact_path.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise GraphError("父工件内容寻址校验失败")
                results.append({
                    "task_id": parent["task_id"], "stage": parent["stage"],
                    "filename": item["filename"], "sha256": digest,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                })
                if len(results) > 50:
                    raise GraphError("父工件数量超过限制")
        return results

    @staticmethod
    def _check_plan(plan: object) -> tuple[list[str], int, str]:
        if not isinstance(plan, Mapping):
            raise GraphError("CheckPlan 必须是对象")
        if set(plan) - {"test_files", "timeout_seconds", "plan_id"}:
            raise GraphError("CheckPlan 含有未支持字段")
        test_files = _string_list(
            plan.get("test_files"), "CheckPlan.test_files", minimum=1,
            maximum=MAX_CHECK_FILES,
        )
        normalized = []
        for item in test_files:
            path = _safe_relative_path(item, "CheckPlan.test_files")
            if not path.startswith("tests/") or not path.endswith(".py"):
                raise GraphError("CheckPlan 仅允许 tests/ 下的 Python 测试文件")
            if not PurePosixPath(path).name.startswith("test_"):
                raise GraphError("CheckPlan 仅允许 test_*.py 文件")
            normalized.append(path)
        if len(set(normalized)) != len(normalized):
            raise GraphError("CheckPlan.test_files 不得重复")
        timeout = plan.get("timeout_seconds", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise GraphError("CheckPlan.timeout_seconds 必须是整数")
        if not 1 <= timeout <= MAX_CHECK_TIMEOUT_SECONDS:
            raise GraphError(f"CheckPlan.timeout_seconds 必须位于 1..{MAX_CHECK_TIMEOUT_SECONDS}")
        plan_id = _text(plan.get("plan_id", "check"), "CheckPlan.plan_id", maximum=128)
        return normalized, timeout, plan_id

    def run_check(
        self, task_id: str, native_agent_id: str, fencing_token: int,
        plan: object,
    ) -> dict[str, Any]:
        test_files, timeout, plan_id = self._check_plan(plan)
        plan_record = {"test_files": test_files, "timeout_seconds": timeout, "plan_id": plan_id}
        plan_digest = hashlib.sha256(_canonical_json(plan_record)).hexdigest()
        with self._file_lock():
            state = self._load_unlocked()
            task = self._verify_worker(state, task_id, native_agent_id, fencing_token)
            if task["stage"] != "Check":
                raise GraphError("graph_run_check 仅允许 Check 阶段")
            existing = task.get("check_observation")
            if existing is not None:
                if existing.get("plan_sha256") == plan_digest:
                    return dict(existing)
                raise GraphError("当前 Check attempt 已执行其他 CheckPlan")
            reservation = task.get("check_plan")
            if reservation is not None:
                if reservation.get("sha256") == plan_digest:
                    raise GraphError("相同 CheckPlan 正在执行")
                raise GraphError("当前 Check attempt 已保留其他 CheckPlan")
            workspace = Path(state["workspace"])
            modules = []
            for relative in test_files:
                path = workspace / relative
                if path.is_symlink() or not path.is_file():
                    raise GraphError(f"CheckPlan 测试文件缺失或不安全: {relative}")
                modules.append(PurePosixPath(relative).with_suffix("").as_posix().replace("/", "."))
            command = [sys.executable, "-P", "-m", "unittest", *modules]
            temp_root = self.run_dir / "tmp"
            temp_root.mkdir(mode=0o700, exist_ok=True)
            task["check_plan"] = {"sha256": plan_digest, **plan_record}
            task["check_pid"] = None
            state["events"].append({
                "event": "check_started", "task_id": task_id,
                "plan_sha256": plan_digest, "at": _now(),
            })
            self._save_unlocked(state)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(workspace),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(temp_root),
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
        }
        if os.name == "nt":
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    env[name] = os.environ[name]
        started = time.monotonic()
        timed_out = False
        output = b""
        error_output = b""
        return_code: int | None = None
        process: subprocess.Popen[bytes] | None = None
        stdout_tail = bytearray()
        stderr_tail = bytearray()
        drain_threads: list[threading.Thread] = []

        def drain(stream: Any, tail: bytearray) -> None:
            while True:
                block = stream.read(8192)
                if not block:
                    return
                tail.extend(block)
                if len(tail) > MAX_CHECK_OUTPUT_BYTES:
                    del tail[:-MAX_CHECK_OUTPUT_BYTES]

        try:
            options: dict[str, Any] = {}
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(
                command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                **options,
            )
            cancelled_before_registration = False
            with self._file_lock():
                state = self._load_unlocked()
                current = self._task(state, task_id)
                if (
                    current.get("status") not in ACTIVE_TASK_STATES
                    or current.get("fencing_token") != fencing_token
                    or current.get("check_plan", {}).get("sha256") != plan_digest
                ):
                    cancelled_before_registration = True
                else:
                    current["check_pid"] = process.pid
                    self._save_unlocked(state)
            if cancelled_before_registration:
                self._terminate_check_process(process.pid)
            for stream, tail, name in (
                (process.stdout, stdout_tail, "graph-check-stdout"),
                (process.stderr, stderr_tail, "graph-check-stderr"),
            ):
                if stream is not None:
                    thread = threading.Thread(
                        target=drain, args=(stream, tail), name=name, daemon=True,
                    )
                    thread.start()
                    drain_threads.append(thread)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_check_process(process.pid)
                process.wait()
            for thread in drain_threads:
                thread.join(timeout=0.5)
            if any(thread.is_alive() for thread in drain_threads):
                self._terminate_check_process(process.pid)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                for thread in drain_threads:
                    thread.join(timeout=0.5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            output = bytes(stdout_tail)
            error_output = bytes(stderr_tail)
            return_code = process.returncode
        except OSError as exc:
            error_output = str(exc).encode("utf-8", errors="replace")
            return_code = -1
        with self._file_lock():
            state = self._load_unlocked()
            task = self._task(state, task_id)
            graph_cancelled = state.get("status") == "cancelled" or task.get("status") == "cancelled"
            if graph_cancelled:
                status = "cancelled"
            elif timed_out:
                status = "timeout"
            else:
                status = "passed" if return_code == 0 else "failed"
            observation = {
                "schema_version": 1,
                "observation_id": "obs_" + uuid.uuid4().hex,
                "graph_id": self.graph_id,
                "task_id": task_id,
                "native_agent_id": native_agent_id,
                "plan_id": plan_id,
                "plan_sha256": plan_digest,
                "command": command,
                "test_files": test_files,
                "status": status,
                "exit_code": return_code,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": output[-MAX_CHECK_OUTPUT_BYTES:].decode("utf-8", errors="replace"),
                "stderr": error_output[-MAX_CHECK_OUTPUT_BYTES:].decode("utf-8", errors="replace"),
                "timeout_seconds": timeout,
                "created_at": _now(),
            }
            task["check_observation"] = observation
            task["check_pid"] = None
            state["events"].append({
                "event": "check_observation", "task_id": task_id,
                "observation_id": observation["observation_id"],
                "status": observation["status"], "at": observation["created_at"],
            })
            self._save_unlocked(state)
            return observation

    @staticmethod
    def _terminate_check_process(pid: int) -> None:
        try:
            if os.name == "nt":
                os.kill(pid, signal.SIGTERM)
            else:
                os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass

    @staticmethod
    def _normalize_envelope(envelope: object) -> dict[str, Any]:
        if not isinstance(envelope, Mapping):
            raise GraphError("artifact envelope 必须是对象")
        allowed = {"outcome", "summary", "reason", "context", "retry_stage", "result"}
        unknown = set(envelope) - allowed
        if unknown:
            raise GraphError(f"artifact envelope 含有未支持字段: {sorted(unknown)}")
        outcome = envelope.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"complete", "needs_revision", "failed"}:
            raise GraphError("artifact envelope.outcome 必须是 complete/needs_revision/failed")
        summary = _text(envelope.get("summary"), "artifact envelope.summary")
        reason = envelope.get("reason", "")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 8192):
            raise GraphError("artifact envelope.reason 无效")
        context = envelope.get("context", {})
        if not isinstance(context, Mapping):
            raise GraphError("artifact envelope.context 必须是对象")
        if len(_canonical_json(context)) > MAX_ENVELOPE_BYTES:
            raise GraphError("artifact envelope.context 超过大小上限")
        retry_stage = envelope.get("retry_stage")
        if retry_stage is not None and (
            not isinstance(retry_stage, str) or retry_stage not in {"Recon", "Repro"}
        ):
            raise GraphError("Review retry_stage 仅支持 Recon/Repro")
        result = envelope.get("result", {})
        if not isinstance(result, Mapping) or len(_canonical_json(result)) > MAX_ENVELOPE_BYTES:
            raise GraphError("artifact envelope.result 必须是有界对象")
        normalized = {
            "outcome": outcome,
            "summary": summary,
            "reason": reason or "",
            "context": dict(context),
            "result": dict(result),
        }
        if retry_stage is not None:
            normalized["retry_stage"] = retry_stage
        if len(_canonical_json(normalized)) > MAX_ENVELOPE_BYTES:
            raise GraphError("artifact envelope 超过大小上限")
        return normalized

    @staticmethod
    def _decode_attachments(attachments: object) -> list[dict[str, Any]]:
        if not isinstance(attachments, list) or len(attachments) > 50:
            raise GraphError("attachments 必须是最多 50 项的数组")
        decoded: list[dict[str, Any]] = []
        total = 0
        for index, item in enumerate(attachments):
            if not isinstance(item, Mapping) or set(item) != {"filename", "sha256", "content_base64"}:
                raise GraphError(f"attachments[{index}] 字段无效")
            filename = _text(item["filename"], f"attachments[{index}].filename", maximum=512)
            if "/" in filename or "\\" in filename or filename in {".", ".."}:
                raise GraphError("attachment filename 必须是单个文件名")
            digest = item["sha256"]
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise GraphError("attachment sha256 格式无效")
            content_b64 = item["content_base64"]
            if not isinstance(content_b64, str):
                raise GraphError("attachment content_base64 必须是字符串")
            try:
                data = base64.b64decode(content_b64, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                raise GraphError("attachment base64 无效") from exc
            if len(data) > MAX_ATTACHMENT_BYTES:
                raise GraphError("单个 attachment 超过大小上限")
            total += len(data)
            if total > MAX_ATTACHMENTS_BYTES:
                raise GraphError("attachments 总大小超过上限")
            actual = hashlib.sha256(data).hexdigest()
            if actual != digest:
                raise GraphError("attachment sha256 与内容不匹配")
            decoded.append({
                "filename": filename, "sha256": digest,
                "content_base64": content_b64, "data": data,
            })
        return decoded

    def checkpoint(
        self, task_id: str, native_agent_id: str, fencing_token: int,
        envelope: object, attachments: object = None,
    ) -> dict[str, Any]:
        normalized = self._normalize_envelope(envelope)
        decoded = self._decode_attachments([] if attachments is None else attachments)
        request_digest = hashlib.sha256(_canonical_json({
            "envelope": normalized,
            "attachments": [
                {"filename": item["filename"], "sha256": item["sha256"]}
                for item in decoded
            ],
        })).hexdigest()
        with self._file_lock():
            state = self._load_unlocked()
            task = self._verify_worker(state, task_id, native_agent_id, fencing_token, require_active=False)
            previous = task.get("checkpoint")
            if previous is not None:
                if previous.get("request_sha256") == request_digest:
                    return dict(previous)
                raise GraphError("task 已有不同内容的 checkpoint，拒绝覆盖")
            if task["status"] not in ACTIVE_TASK_STATES:
                raise GraphError(f"task 不处于可 checkpoint 状态: {task.get('status')}")
            if normalized["outcome"] == "complete" and task["stage"] == "Check":
                observation = task.get("check_observation")
                if not isinstance(observation, Mapping):
                    raise GraphError("Check 阶段缺少 graph_run_check Observation")
                if observation.get("status") != "passed":
                    raise GraphError("Check Observation 未通过，不能 checkpoint complete")
            attachment_refs = []
            for item in decoded:
                digest = item["sha256"]
                artifact_dir = self.run_dir / "artifacts" / "sha256" / digest[:2]
                bugx_paths.assert_no_symlink_components(artifact_dir)
                artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                target = artifact_dir / digest
                if target.exists():
                    if target.is_symlink() or not target.is_file():
                        raise GraphError("content-addressed attachment path 不安全")
                    existing = target.read_bytes()
                    if hashlib.sha256(existing).hexdigest() != digest:
                        raise GraphError("既有 content-addressed attachment 内容损坏")
                else:
                    try:
                        secure_atomic.atomic_write_bytes(
                            target, item["data"], mode=0o600, parent_mode=0o700,
                        )
                    except OSError as exc:
                        raise GraphError(f"attachment 写入失败: {exc}") from exc
                attachment_refs.append({"filename": item["filename"], "sha256": digest})
            envelope_payload = {
                "schema_version": 1,
                "graph_id": self.graph_id,
                "task_id": task_id,
                "stage": task["stage"],
                "attempt": task["attempt"],
                "native_agent_id": native_agent_id,
                "fencing_token": fencing_token,
                "submitted_at": _now(),
                **normalized,
                "check_observation": task.get("check_observation"),
                "attachments": attachment_refs,
            }
            envelope_bytes = _canonical_json(envelope_payload)
            checkpoint_id = hashlib.sha256(envelope_bytes).hexdigest()
            envelope_path = self.run_dir / "checkpoints" / f"{task_id}.json"
            envelope_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                secure_atomic.atomic_write_bytes(
                    envelope_path, envelope_bytes, mode=0o600, parent_mode=0o700,
                )
            except OSError as exc:
                raise GraphError(f"checkpoint envelope 写入失败: {exc}") from exc
            receipt = {
                "checkpoint_id": checkpoint_id,
                "request_sha256": request_digest,
                "path": str(envelope_path.relative_to(self.run_dir)),
                "envelope_sha256": checkpoint_id,
                "attachments": attachment_refs,
            }
            task["checkpoint"] = receipt
            outcome = normalized["outcome"]
            if outcome == "complete":
                task["status"] = "complete"
                task["finished_at"] = _now()
                self._ensure_successor(state, task)
            else:
                task["status"] = "needs_revision"
                task["finished_at"] = _now()
                task["failure_context"] = {
                    "reason": normalized["reason"] or normalized["summary"],
                    "context": normalized["context"],
                    "checkpoint_id": checkpoint_id,
                }
                self._retry_or_fail(state, task, normalized)
            state["events"].append({
                "event": "task_checkpointed", "task_id": task_id,
                "stage": task["stage"], "attempt": task["attempt"],
                "outcome": outcome, "checkpoint_id": checkpoint_id, "at": _now(),
            })
            self._save_unlocked(state)
            return receipt

    def _ensure_successor(self, state: dict[str, Any], task: Mapping[str, Any]) -> None:
        stage = task["stage"]
        if stage == "Report":
            return
        next_stage = {
            "Recon": "Repro", "Repro": "Review", "Review": (
                "Patch" if state["task_input"]["patch_requested"] else "Check"
            ), "Patch": "Check", "Check": "Report",
        }.get(stage)
        if next_stage is None:
            return
        if any(
            item.get("stage") == next_stage
            and task["task_id"] in item.get("dependencies", [])
            and item.get("status") not in {"failed", "cancelled", "superseded"}
            for item in state["tasks"]
        ):
            return
        previous = [item for item in state["tasks"] if item.get("stage") == next_stage]
        attempt = max((int(item["attempt"]) for item in previous), default=0) + 1
        if attempt > MAX_ATTEMPTS:
            state["status"] = "failed"
            state["failure_reason"] = f"{next_stage} 超过最大 attempt 次数"
            return
        state["tasks"].append(_new_task(
            state, next_stage, attempt, [str(task["task_id"])],
            feedback=task.get("feedback") if task.get("feedback") else None,
        ))

    def _retry_or_fail(
        self, state: dict[str, Any], task: dict[str, Any], envelope: Mapping[str, Any],
    ) -> None:
        next_attempt = int(task["attempt"]) + 1
        if next_attempt > MAX_ATTEMPTS:
            task["status"] = "failed"
            state["status"] = "failed"
            state["failure_reason"] = task["failure_context"]["reason"]
            return
        if task["stage"] == "Review":
            retry_stage = envelope.get("retry_stage") or "Repro"
            producer_attempts = [
                item for item in state["tasks"] if item.get("stage") == retry_stage
            ]
            retry_number = max((int(item["attempt"]) for item in producer_attempts), default=0) + 1
            if retry_number > MAX_ATTEMPTS:
                task["status"] = "failed"
                state["status"] = "failed"
                state["failure_reason"] = f"{retry_stage} 已达到 {MAX_ATTEMPTS} 次 attempt 上限"
                return
            original = max(producer_attempts, key=lambda item: int(item["attempt"]))
            feedback = {
                "reason": envelope["reason"] or envelope["summary"],
                "context": envelope["context"],
                "from_stage": "Review",
                "from_task_id": task["task_id"],
            }
            retry = _new_task(
                state, retry_stage, retry_number,
                original.get("dependencies", []), feedback=feedback,
            )
            state["tasks"].append(retry)
            return
        retry = _new_task(
            state, task["stage"], next_attempt,
            task.get("dependencies", []),
            feedback={
                "reason": envelope["reason"] or envelope["summary"],
                "context": envelope["context"],
                "from_stage": task["stage"],
                "from_task_id": task["task_id"],
            },
        )
        state["tasks"].append(retry)

    def cancel(self, reason: str, task_id: str | None = None) -> dict[str, Any]:
        reason = _text(reason, "reason", maximum=2048)
        process_ids: list[int] = []
        with self._file_lock():
            state = self._load_unlocked()
            if task_id is not None:
                self._task(state, task_id)
            if state["status"] in {"finalized", "cancelled"}:
                return {"graph_id": self.graph_id, "status": state["status"], "cancelled": False}
            for task in state["tasks"]:
                if task.get("status") not in TERMINAL_TASK_STATES:
                    process_id = task.get("check_pid")
                    if isinstance(process_id, int) and process_id > 0:
                        process_ids.append(process_id)
                    if task.get("status") in RESERVED_TASK_STATES:
                        task["fencing_token"] = int(task.get("fencing_token", 0)) + 1
                    task["status"] = "cancelled"
                    task["finished_at"] = _now()
            state["status"] = "cancelled"
            state["cancel_reason"] = reason
            state["cancelled_at"] = _now()
            state["events"].append({
                "event": "graph_cancelled", "task_id": task_id,
                "reason": reason, "at": state["cancelled_at"],
            })
            self._save_unlocked(state)
        # Native agents are fenced by task state/token. The engine has no host
        # UI handle to stop them; it reclaims only the owned snapshot workspace.
        for process_id in process_ids:
            self._terminate_check_process(process_id)
        if self.workspace.exists() and not self.workspace.is_symlink():
            shutil.rmtree(self.workspace)
        return {"graph_id": self.graph_id, "status": "cancelled", "cancelled": True}

    def finalize(self, decision: str, summary: str) -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "inconclusive"}:
            raise GraphError("decision 必须是 accepted/rejected/inconclusive")
        summary = _text(summary, "summary", maximum=8192)
        with self._file_lock():
            state = self._load_unlocked()
            if state["status"] == "finalized":
                report = state.get("final_report")
                if report and report.get("decision") == decision and report.get("summary") == summary:
                    return dict(report)
                raise GraphError("graph 已最终固化，不能覆盖裁决")
            if any(task.get("status") in ACTIVE_TASK_STATES for task in state["tasks"]):
                raise GraphError("仍有已绑定或执行中的 task，不能 finalize")
            if state["status"] == "running":
                report_task = next((
                    item for item in state["tasks"]
                    if item.get("stage") == "Report" and item.get("status") == "complete"
                ), None)
                if report_task is None:
                    raise GraphError("Report 阶段未完成，不能 finalize")
            overall = "complete" if state["status"] == "running" else state["status"]
            payload = {
                "schema_version": 1,
                "graph_id": self.graph_id,
                "status": overall,
                "decision": decision,
                "summary": summary,
                "created_at": _now(),
                "source_root_sha256": state["source_root_sha256"],
                "snapshot": state["snapshot"],
                "tasks": [
                    {
                        "task_id": task["task_id"], "stage": task["stage"],
                        "attempt": task["attempt"], "status": task["status"],
                        "dependencies": task["dependencies"],
                        "native_agent_id": task["native_agent_id"],
                        "checkpoint": task["checkpoint"],
                        "check_observation": task["check_observation"],
                        "failure_context": task["failure_context"],
                    }
                    for task in state["tasks"]
                ],
                "events": state["events"],
                "failure_reason": state.get("failure_reason"),
                "cancel_reason": state.get("cancel_reason"),
            }
            report_path = self.run_dir / "final-report.json"
            try:
                secure_atomic.atomic_write_json(
                    report_path, payload, mode=0o600, parent_mode=0o700,
                )
            except (OSError, ValueError, TypeError) as exc:
                raise GraphError(f"final report 写入失败: {exc}") from exc
            report_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
            receipt = {
                "graph_id": self.graph_id,
                "status": overall,
                "decision": decision,
                "summary": summary,
                "report_path": str(report_path.relative_to(self.run_dir)),
                "report_sha256": report_sha256,
                "task_count": len(state["tasks"]),
                "at": payload["created_at"],
            }
            state["status"] = "finalized"
            state["final_report"] = receipt
            state["events"].append({
                "event": "graph_finalized", "report_sha256": report_sha256,
                "decision": decision, "at": payload["created_at"],
            })
            self._save_unlocked(state)
            return receipt


__all__ = [
    "AgentOrchestrator", "GraphError", "MAX_ATTEMPTS", "MAX_CHECK_TIMEOUT_SECONDS",
    "assemble_task_prompt",
]
