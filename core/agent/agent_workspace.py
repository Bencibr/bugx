"""Private, content-addressed working copies for autonomous agents.

The caller supplies the engine's private directory, a run UUID, and the local
project to inspect.  The origin is only opened for reading; all execution
copies live below ``runs/<run_uuid>/workspace``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterator, Mapping


class AgentWorkspaceError(RuntimeError):
    """Base error for fail-closed AgentWorkspace operations."""


class TargetDriftError(AgentWorkspaceError):
    """Raised when the snapshotted project changes during a run."""


class OriginWriteViolationError(AgentWorkspaceError):
    """Raised when any entry in the origin project differs from its baseline."""


class WorkspaceOwnershipError(AgentWorkspaceError):
    """Raised when cleanup cannot prove that a path belongs to this instance."""


class AgentWorkspace:
    """Create and manage isolated project copies for one agent run.

    ``engine_private_dir`` must already exist and be private to the engine.
    The instance creates ``runs/<run_uuid>/workspace`` with mode ``0700`` and
    stores the selected project snapshot, a baseline execution copy, and patch
    attempts below it.  Patch attempts always derive from the snapshot, never
    from another mutable execution copy.
    """

    EXCLUDED_DIRECTORIES = frozenset(
        {
            ".git",
            ".bugx",
            "__pycache__",
            "node_modules",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "venv",
            ".cache",
            "build",
            "dist",
            "target",
            "coverage",
            "htmlcov",
            "artifacts",
            "bugx-artifacts",
            ".history",
            "history",
            "test-results",
            "benchmark-results",
        }
    )
    EXCLUDED_FILE_NAMES = frozenset(
        {
            ".DS_Store",
            "Thumbs.db",
            ".coverage",
        }
    )
    EXCLUDED_FILE_PATTERNS = (
        "*.pyc",
        "*.pyo",
        "*.log",
        "*.log.*",
        "*.swp",
        "*.swo",
    )
    OWNER_MARKER = ".agent-workspace-owner.json"
    CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        origin_root: Path | str,
        engine_private_dir: Path | str,
        run_uuid: str,
    ) -> None:
        if not isinstance(run_uuid, str):
            raise TypeError("run_uuid must be a canonical UUID string")
        try:
            normalized_run_uuid = str(uuid.UUID(run_uuid))
        except (ValueError, AttributeError) as exc:
            raise ValueError("run_uuid must be a canonical UUID string") from exc
        if run_uuid.lower() != normalized_run_uuid:
            raise ValueError("run_uuid must be a canonical UUID string")

        origin_input = Path(origin_root).expanduser()
        if origin_input.is_symlink() or not origin_input.is_dir():
            raise ValueError("origin_root must be an existing, non-symlink directory")
        engine_input = Path(engine_private_dir).expanduser()
        if engine_input.is_symlink() or not engine_input.is_dir():
            raise ValueError("engine_private_dir must be an existing, non-symlink directory")

        self.origin_root = origin_input.resolve(strict=True)
        self.engine_private_dir = engine_input.resolve(strict=True)
        self._engine_private_identity = self._identity(self.engine_private_dir)
        self.run_uuid = normalized_run_uuid
        self.runs_root = self.engine_private_dir / "runs"
        self.run_root = self.runs_root / self.run_uuid
        self.workspace_root = self.run_root / "workspace"
        self.snapshot_root = self.workspace_root / "snapshot"
        self.baseline_root = self.workspace_root / "baseline"
        self.patched_root = self.workspace_root / "patched"

        if self._paths_overlap(self.origin_root, self.workspace_root):
            raise ValueError("workspace_root and origin_root must be separate trees")
        if self.runs_root.is_symlink() or (self.runs_root.exists() and not self.runs_root.is_dir()):
            raise WorkspaceOwnershipError("engine runs path must be a real directory")

        self._owner_token = uuid.uuid4().hex
        self._workspace_identity: tuple[int, int] | None = None
        self._run_root_identity: tuple[int, int] | None = None
        self._runs_root_identity: tuple[int, int] | None = None
        self._baseline_identity: tuple[int, int] | None = None
        self._patch_identities: dict[str, tuple[int, int]] = {}
        self._closed = False
        self._aborted = False
        self._origin_violation_detail: str | None = None
        self._run_root_created = False
        self.snapshot_manifest: Mapping[str, str] = MappingProxyType({})
        self._snapshot_integrity_manifest: dict[str, str] = {}
        self._origin_integrity_manifest: dict[str, str] = {}

        if not self.runs_root.exists():
            self.runs_root.mkdir(mode=0o700)
        self._runs_root_identity = self._identity(self.runs_root)
        try:
            self.run_root.mkdir(mode=0o700)
            self._run_root_created = True
            self._run_root_identity = self._identity(self.run_root)
            self.workspace_root.mkdir(mode=0o700)
            os.chmod(self.workspace_root, 0o700)
            self._workspace_identity = self._identity(self.workspace_root)
            self._write_owner_marker()

            initial_origin = self._scan_origin_integrity()
            copied_manifest = self._copy_origin_to_snapshot()
            current_source_tree = self._scan_project_integrity(self.origin_root, project_only=True)
            snapshot_tree = self._scan_project_integrity(self.snapshot_root, project_only=False)
            current_source_files = {
                key.removeprefix("file:"): digest
                for key, digest in current_source_tree.items()
                if key.startswith("file:")
            }
            if copied_manifest != current_source_files or current_source_tree != snapshot_tree:
                raise TargetDriftError(
                    self._format_manifest_diff(
                        current_source_tree, snapshot_tree, "project changed while snapshotting"
                    )
                )
            current_origin = self._scan_origin_integrity()
            if initial_origin != current_origin:
                raise OriginWriteViolationError(
                    self._format_manifest_diff(
                        initial_origin, current_origin, "origin changed while snapshotting"
                    )
                )
            self._origin_integrity_manifest = initial_origin
            self._snapshot_integrity_manifest = snapshot_tree
            self.snapshot_manifest = MappingProxyType(dict(copied_manifest))
            self._protect_snapshot()
            self.verify_before_start()
            self.create_baseline()
        except BaseException:
            self._cleanup_after_initialization_failure()
            raise

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        return first == second or first.is_relative_to(second) or second.is_relative_to(first)

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceOwnershipError(f"expected a real directory: {path}")
        return info.st_dev, info.st_ino

    @staticmethod
    def _file_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _write_owner_marker(self) -> None:
        marker = self.workspace_root / self.OWNER_MARKER
        payload = json.dumps(
            {"run_uuid": self.run_uuid, "owner_token": self._owner_token},
            sort_keys=True,
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(marker, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as marker_file:
                marker_file.write(payload)
                marker_file.flush()
                os.fsync(marker_file.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    @classmethod
    def _is_excluded(cls, relative_path: str, is_directory: bool) -> bool:
        parts = PurePosixPath(relative_path).parts
        if any(part in cls.EXCLUDED_DIRECTORIES for part in parts):
            return True
        name = parts[-1]
        if is_directory:
            return False
        if name in cls.EXCLUDED_FILE_NAMES:
            return True
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in cls.EXCLUDED_FILE_PATTERNS)

    @classmethod
    def _walk_tree(
        cls, root: Path, *, project_only: bool
    ) -> Iterator[tuple[str, str, Path, os.stat_result]]:
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"project root is missing or is a symbolic link: {root}")
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                raise OSError(f"cannot inspect project directory {current}: {exc}") from exc

            directories: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                info = entry.stat(follow_symlinks=False)
                kind = (
                    "symlink"
                    if stat.S_ISLNK(info.st_mode)
                    else "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else "special"
                )
                if project_only and cls._is_excluded(relative, kind == "directory"):
                    continue
                yield relative, kind, path, info
                if kind == "directory":
                    directories.append(path)
                elif kind == "special":
                    raise ValueError(f"unsupported special project entry: {relative}")
            pending.extend(reversed(directories))

    @classmethod
    def _sha256_file(cls, path: Path) -> str:
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"expected a regular file: {path}")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                while True:
                    block = source.read(cls.CHUNK_SIZE)
                    if not block:
                        break
                    digest.update(block)
                after = os.fstat(source.fileno())
                if cls._file_signature(before) != cls._file_signature(after):
                    raise OSError(f"file changed while hashing: {path}")
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        return digest.hexdigest()

    @classmethod
    def _scan_project_integrity(cls, root: Path, *, project_only: bool) -> dict[str, str]:
        manifest: dict[str, str] = {}
        for relative, kind, path, _info in cls._walk_tree(root, project_only=project_only):
            if kind == "symlink":
                raise ValueError(f"source snapshot does not accept symbolic links: {relative}")
            if kind == "directory":
                manifest[f"directory:{relative}"] = "directory"
            elif kind == "file":
                manifest[f"file:{relative}"] = cls._sha256_file(path)
        return manifest

    def _copy_origin_to_snapshot(self) -> dict[str, str]:
        self.snapshot_root.mkdir(mode=0o700)
        manifest: dict[str, str] = {}
        for relative, kind, source_path, source_info in self._walk_tree(
            self.origin_root, project_only=True
        ):
            destination = self.snapshot_root.joinpath(*PurePosixPath(relative).parts)
            if kind == "symlink":
                raise ValueError(f"source snapshot does not accept symbolic links: {relative}")
            if kind == "directory":
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

            open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source_path, open_flags)
            destination_fd: int | None = None
            digest = hashlib.sha256()
            try:
                opened_info = os.fstat(source_fd)
                if not stat.S_ISREG(opened_info.st_mode):
                    raise ValueError(f"source changed to a non-file during snapshot: {relative}")
                destination_fd = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with (
                    os.fdopen(source_fd, "rb") as source_file,
                    os.fdopen(destination_fd, "wb") as destination_file,
                ):
                    source_fd = -1
                    destination_fd = None
                    while True:
                        block = source_file.read(self.CHUNK_SIZE)
                        if not block:
                            break
                        digest.update(block)
                        destination_file.write(block)
                    destination_file.flush()
                    os.fsync(destination_file.fileno())
                    after_info = os.fstat(source_file.fileno())
                    if self._file_signature(opened_info) != self._file_signature(after_info):
                        raise TargetDriftError(
                            f"source changed while copying snapshot file: {relative}"
                        )
                snapshot_mode = (stat.S_IMODE(source_info.st_mode) | 0o400) & 0o777
                os.chmod(destination, snapshot_mode)
                manifest[relative] = digest.hexdigest()
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
                if destination_fd is not None:
                    os.close(destination_fd)
        return manifest

    def _scan_origin_integrity(self) -> dict[str, str]:
        manifest: dict[str, str] = {}
        for relative, kind, path, _info in self._walk_tree(self.origin_root, project_only=False):
            if kind == "directory":
                manifest[f"directory:{relative}"] = "directory"
            elif kind == "file":
                manifest[f"file:{relative}"] = self._sha256_file(path)
            elif kind == "symlink":
                target = os.readlink(path)
                manifest[f"symlink:{relative}"] = hashlib.sha256(os.fsencode(target)).hexdigest()
        return manifest

    def _protect_snapshot(self) -> None:
        """Remove write bits from the canonical tree used to seed attempts."""
        for current, directory_names, file_names in os.walk(
            self.snapshot_root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            for directory_name in directory_names:
                child = current_path / directory_name
                if child.is_symlink():
                    raise WorkspaceOwnershipError(f"snapshot contains a symbolic link: {child}")
                os.chmod(child, 0o500)
            for file_name in file_names:
                child = current_path / file_name
                if child.is_symlink() or not child.is_file():
                    raise WorkspaceOwnershipError(f"snapshot contains an unsafe file: {child}")
                os.chmod(child, stat.S_IMODE(child.stat().st_mode) & ~0o222)
        os.chmod(self.snapshot_root, 0o500)

    def _assert_snapshot_unchanged(self) -> None:
        try:
            current = self._scan_project_integrity(self.snapshot_root, project_only=False)
        except (OSError, ValueError) as exc:
            self._aborted = True
            raise WorkspaceOwnershipError(f"cannot verify canonical snapshot: {exc}") from exc
        if current != self._snapshot_integrity_manifest:
            self._aborted = True
            raise WorkspaceOwnershipError(
                self._format_manifest_diff(
                    self._snapshot_integrity_manifest,
                    current,
                    "canonical snapshot was modified",
                )
            )

    @staticmethod
    def _format_manifest_diff(
        expected: Mapping[str, str],
        actual: Mapping[str, str],
        heading: str,
    ) -> str:
        expected_paths = set(expected)
        actual_paths = set(actual)
        added = sorted(actual_paths - expected_paths)
        removed = sorted(expected_paths - actual_paths)
        modified = sorted(
            key for key in expected_paths & actual_paths if expected[key] != actual[key]
        )
        details: list[str] = []
        for label, paths in (("added", added), ("removed", removed), ("modified", modified)):
            if paths:
                sample = ", ".join(paths[:8])
                suffix = " ..." if len(paths) > 8 else ""
                details.append(f"{label}={sample}{suffix}")
        return heading if not details else f"{heading}: " + "; ".join(details)

    def _ensure_active(self) -> None:
        if self._closed:
            raise AgentWorkspaceError("workspace is already closed")
        if self._aborted:
            raise TargetDriftError("workspace is aborted after an integrity violation")

    def verify_target_unchanged(self, stage: str = "") -> None:
        """Raise ``TargetDriftError`` unless source-file contents match the snapshot."""
        self._ensure_active()
        try:
            current = self._scan_project_integrity(self.origin_root, project_only=True)
        except (OSError, ValueError) as exc:
            self._aborted = True
            raise TargetDriftError(
                f"cannot verify target at {stage or 'checkpoint'}: {exc}"
            ) from exc
        if current != self._snapshot_integrity_manifest:
            self._aborted = True
            heading = f"target drift at {stage}" if stage else "target drift"
            raise TargetDriftError(
                self._format_manifest_diff(self._snapshot_integrity_manifest, current, heading)
            )

    def verify_before_start(self) -> None:
        """Check source identity immediately before starting a run."""
        self.verify_target_unchanged("before_start")

    def verify_after_sampling(self) -> None:
        """Check that sampling did not race with an origin-project change."""
        self.verify_target_unchanged("after_sampling")

    def verify_before_stage(self, stage: str) -> None:
        """Check target drift before executing a named stage."""
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("stage must be a non-empty string")
        self.verify_target_unchanged(stage.strip())

    def verify_origin_untouched(self) -> None:
        """Raise ``OriginWriteViolationError`` if any origin entry changed."""
        if self._closed:
            raise AgentWorkspaceError("workspace is already closed")
        if self._origin_violation_detail is not None:
            raise OriginWriteViolationError(self._origin_violation_detail)
        try:
            current = self._scan_origin_integrity()
        except (OSError, ValueError) as exc:
            self._aborted = True
            self._origin_violation_detail = f"cannot prove origin project untouched: {exc}"
            raise OriginWriteViolationError(self._origin_violation_detail) from exc
        if current != self._origin_integrity_manifest:
            self._aborted = True
            self._origin_violation_detail = self._format_manifest_diff(
                self._origin_integrity_manifest,
                current,
                "origin project write violation",
            )
            raise OriginWriteViolationError(self._origin_violation_detail)

    def create_baseline(self) -> Path:
        """Return the independently writable baseline execution copy."""
        self._ensure_active()
        self.verify_before_stage("baseline-copy")
        if self._baseline_identity is not None:
            self._assert_owned_child(self.baseline_root, self._baseline_identity)
            return self.baseline_root
        self.baseline_root.mkdir(mode=0o700)
        self._baseline_identity = self._identity(self.baseline_root)
        try:
            self._copy_snapshot_to_execution_tree(self.baseline_root)
            self.verify_target_unchanged("baseline-copy-complete")
            return self.baseline_root
        except BaseException:
            self._remove_owned_child(self.baseline_root, self._baseline_identity)
            self._baseline_identity = None
            raise

    def create_patched_copy(self, patch_digest: str) -> Path:
        """Create a fresh writable ``patched/<sha256>`` copy for one patch.

        Reusing a digest in this instance removes only that instance's prior
        attempt and recreates it from the immutable-at-source snapshot.  A
        pre-existing path that this instance did not create is never removed.
        """
        self._ensure_active()
        if not isinstance(patch_digest, str) or len(patch_digest) != 64:
            raise ValueError("patch_digest must be a 64-character SHA-256 hex digest")
        if any(character not in "0123456789abcdef" for character in patch_digest.lower()):
            raise ValueError("patch_digest must be a 64-character SHA-256 hex digest")
        digest = patch_digest.lower()
        self.verify_before_stage(f"patch:{digest}")
        if not self.patched_root.exists():
            self.patched_root.mkdir(mode=0o700)
        elif self.patched_root.is_symlink() or not self.patched_root.is_dir():
            raise WorkspaceOwnershipError("patched path must be a real directory")
        destination = self.patched_root / digest

        previous_identity = self._patch_identities.get(digest)
        if destination.exists() or destination.is_symlink():
            if previous_identity is None:
                raise WorkspaceOwnershipError(
                    f"refusing to replace a patch directory not owned by this instance: {digest}"
                )
            self._assert_owned_child(destination, previous_identity)
            self._remove_owned_child(destination, previous_identity)
            self._patch_identities.pop(digest, None)

        destination.mkdir(mode=0o700)
        identity = self._identity(destination)
        self._patch_identities[digest] = identity
        try:
            self._copy_snapshot_to_execution_tree(destination)
            self.verify_target_unchanged(f"patch-copy-complete:{digest}")
            return destination
        except BaseException:
            self._remove_owned_child(destination, identity)
            self._patch_identities.pop(digest, None)
            raise

    def _copy_snapshot_to_execution_tree(self, destination: Path) -> None:
        self._assert_workspace_owned()
        self._assert_snapshot_unchanged()
        shutil.copytree(
            self.snapshot_root, destination, dirs_exist_ok=True, copy_function=shutil.copy2
        )
        copied_integrity = self._scan_project_integrity(destination, project_only=False)
        if copied_integrity != self._snapshot_integrity_manifest:
            raise WorkspaceOwnershipError("execution copy does not match the canonical snapshot")
        for current, directory_names, file_names in os.walk(
            destination, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            for directory_name in directory_names:
                child = current_path / directory_name
                if child.is_symlink() or not child.is_dir():
                    raise WorkspaceOwnershipError(
                        f"snapshot copy contains an unsafe directory: {child}"
                    )
                os.chmod(child, (stat.S_IMODE(child.stat().st_mode) | 0o700) & 0o777)
            for file_name in file_names:
                child = current_path / file_name
                if child.is_symlink() or not child.is_file():
                    raise WorkspaceOwnershipError(f"snapshot copy contains an unsafe file: {child}")
                os.chmod(child, (stat.S_IMODE(child.stat().st_mode) | 0o200) & 0o777)
        os.chmod(destination, 0o700)
        self._assert_snapshot_unchanged()

    def _assert_workspace_owned(self) -> None:
        if (
            self.engine_private_dir.is_symlink()
            or self._identity(self.engine_private_dir) != self._engine_private_identity
        ):
            raise WorkspaceOwnershipError("engine private directory identity changed")
        if (
            self.runs_root.is_symlink()
            or not self.runs_root.is_dir()
            or self._runs_root_identity is None
            or self._identity(self.runs_root) != self._runs_root_identity
        ):
            raise WorkspaceOwnershipError("engine runs directory is missing or replaced")
        if (
            self._run_root_identity is None
            or self._identity(self.run_root) != self._run_root_identity
        ):
            raise WorkspaceOwnershipError("run directory identity changed")
        if self.workspace_root.is_symlink() or not self.workspace_root.is_dir():
            raise WorkspaceOwnershipError("workspace directory is missing or replaced")
        workspace_info = self.workspace_root.lstat()
        if (
            self._workspace_identity is None
            or (workspace_info.st_dev, workspace_info.st_ino) != self._workspace_identity
        ):
            raise WorkspaceOwnershipError("workspace directory identity changed")
        if stat.S_IMODE(workspace_info.st_mode) != 0o700:
            os.chmod(self.workspace_root, 0o700)
        marker_path = self.workspace_root / self.OWNER_MARKER
        try:
            marker_info = marker_path.lstat()
            if not stat.S_ISREG(marker_info.st_mode):
                raise WorkspaceOwnershipError("workspace ownership marker is not a regular file")
            descriptor = os.open(
                marker_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as marker_file:
                marker = json.loads(marker_file.read(4096).decode("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise WorkspaceOwnershipError(
                "workspace ownership marker is missing or invalid"
            ) from exc
        if marker != {"owner_token": self._owner_token, "run_uuid": self.run_uuid}:
            raise WorkspaceOwnershipError("workspace ownership marker does not match this instance")

    def _assert_owned_child(self, path: Path, identity: tuple[int, int]) -> None:
        if not self._is_within(path, self.workspace_root) or path == self.workspace_root:
            raise WorkspaceOwnershipError(f"refusing to operate outside workspace: {path}")
        if path.is_symlink() or not path.is_dir() or self._identity(path) != identity:
            raise WorkspaceOwnershipError(f"owned workspace directory was replaced: {path}")
        self._assert_workspace_owned()

    def _remove_owned_child(self, path: Path, identity: tuple[int, int]) -> None:
        self._assert_owned_child(path, identity)
        self._assert_safe_removal_tree(path)
        self._make_tree_removable(path)
        shutil.rmtree(path)

    @staticmethod
    def _assert_safe_removal_tree(root: Path) -> None:
        """Refuse recursive cleanup across mounts or externally linked files."""
        root_info = root.lstat()
        root_device = root_info.st_dev
        for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for directory_name in directory_names:
                child = current_path / directory_name
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode):
                    continue
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_dev != root_device
                    or os.path.ismount(child)
                ):
                    raise WorkspaceOwnershipError(
                        f"refusing cleanup across a mounted or replaced directory: {child}"
                    )
            for file_name in file_names:
                child = current_path / file_name
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode):
                    continue
                if info.st_dev != root_device or info.st_nlink > 1:
                    raise WorkspaceOwnershipError(
                        f"refusing cleanup of an externally linked file: {child}"
                    )

    @staticmethod
    def _make_tree_removable(root: Path) -> None:
        """Restore owner traversal/write bits without following symlinks."""
        for current, directory_names, _file_names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for directory_name in directory_names:
                child = current_path / directory_name
                if child.is_symlink():
                    continue
                os.chmod(child, (stat.S_IMODE(child.stat().st_mode) | 0o700) & 0o777)
        if root.is_dir() and not root.is_symlink():
            os.chmod(root, (stat.S_IMODE(root.stat().st_mode) | 0o700) & 0o777)

    def _cleanup_after_initialization_failure(self) -> None:
        if self._workspace_identity is not None:
            try:
                anchors_are_owned = (
                    self._identity(self.engine_private_dir) == self._engine_private_identity
                    and self._runs_root_identity is not None
                    and self._identity(self.runs_root) == self._runs_root_identity
                    and self._run_root_identity is not None
                    and self._identity(self.run_root) == self._run_root_identity
                    and self._identity(self.workspace_root) == self._workspace_identity
                )
                if anchors_are_owned:
                    self._assert_safe_removal_tree(self.workspace_root)
                    self._make_tree_removable(self.workspace_root)
                    shutil.rmtree(self.workspace_root)
            except (OSError, WorkspaceOwnershipError):
                pass
        if self._run_root_created and self._run_root_identity is not None:
            try:
                if self._identity(self.run_root) == self._run_root_identity:
                    self.run_root.rmdir()
            except (OSError, WorkspaceOwnershipError):
                pass

    def cleanup(self) -> None:
        """Remove only this instance's workspace and an empty owned run directory.

        No process is signalled or terminated.  If the origin changed, the
        owned workspace is still safely removed and the violation is raised
        after cleanup so callers cannot mistake the run for a clean one.
        """
        if self._closed:
            return
        origin_violation: OriginWriteViolationError | None = None
        try:
            self.verify_origin_untouched()
        except OriginWriteViolationError as exc:
            origin_violation = exc

        self._assert_workspace_owned()
        self._assert_safe_removal_tree(self.workspace_root)
        self._make_tree_removable(self.workspace_root)
        shutil.rmtree(self.workspace_root)
        self._closed = True
        if self._run_root_created and self._run_root_identity is not None:
            try:
                if self._identity(self.run_root) == self._run_root_identity:
                    self.run_root.rmdir()
            except OSError:
                # A non-owned sibling file prevents removal; leave it in place.
                pass
        if origin_violation is not None:
            raise origin_violation

    def __enter__(self) -> AgentWorkspace:
        self._ensure_active()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            self.cleanup()
        except OriginWriteViolationError as violation:
            if isinstance(exc, BaseException):
                exc.add_note(str(violation))
            else:
                raise
        return False


__all__ = [
    "AgentWorkspace",
    "AgentWorkspaceError",
    "OriginWriteViolationError",
    "TargetDriftError",
    "WorkspaceOwnershipError",
]
