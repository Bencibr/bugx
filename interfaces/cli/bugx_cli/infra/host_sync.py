#!/usr/bin/env python3
"""src/bugx_cli/infra/host_sync.py - 宿主适配器同步、插件分发与安装包校验 (DOC-070)。"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import sysconfig
import time
from pathlib import Path
from typing import Any

from bugx_cli import __version__
from .engine_installer import (
    RUNTIME_NAMES,
    _distribution_located_path,
    global_engine_root,
)
from .security import (
    CliError,
    _assert_no_symlink_components,
    _find_repo_root,
    _guard_plugin_write_target,
    _i18n_exc,
    _safe_record_target,
    _sha256_file,
    _write_host_resource,
)

PLUGIN_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[-+][0-9A-Za-z.-]+)?$")
CORE_API_RE = re.compile(r"^>=(\d+)\.(\d+),<(\d+)\.(\d+)$")


def pi_extension_root() -> Path:
    """Return the bundled complete Pi adapter, without loading or executing it."""
    repository_root = _find_repo_root(Path(__file__))
    candidates = [
        repository_root / "ecosystem" / "integrations" / "pi" / "extensions" / "bugx",
        repository_root / "integrations" / "pi" / "extensions" / "bugx",
        Path(sysconfig.get_path("data")) / "share" / "bugx-agent" /
        "pi" / "extensions" / "bugx",
    ]
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        for item in distribution.files or []:
            text = item.as_posix()
            if text.endswith("share/bugx-agent/pi/extensions/bugx/index.ts"):
                located = _distribution_located_path(distribution.locate_file(item))
                if located is not None:
                    candidates.append(located.parent)
    except importlib.metadata.PackageNotFoundError:
        pass
    required = ("index.ts", "manifest.json", "README.md")
    for candidate in dict.fromkeys(path.resolve() for path in candidates):
        if candidate.is_dir() and all((candidate / name).is_file() for name in required):
            return candidate
    raise CliError(_i18n_exc("common.pi_extension_missing", "找不到 Pi BugX Extension 资源；请重新安装 BugX"))


def pi_skill_root() -> Path:
    """Return the bundled complete Pi Skill, without generating a variant."""
    repository_root = _find_repo_root(Path(__file__))
    candidates = [
        repository_root / "ecosystem" / "integrations" / "pi" / "skills" / "bugx",
        repository_root / "integrations" / "pi" / "skills" / "bugx",
        Path(sysconfig.get_path("data")) / "share" / "bugx-agent" /
        "pi" / "skills" / "bugx",
    ]
    try:
        distribution = importlib.metadata.distribution("bugx-cli")
        for item in distribution.files or []:
            text = item.as_posix()
            if text.endswith("share/bugx-agent/pi/skills/bugx/SKILL.md"):
                located = _distribution_located_path(distribution.locate_file(item))
                if located is not None:
                    candidates.append(located.parent)
    except importlib.metadata.PackageNotFoundError:
        pass
    for candidate in dict.fromkeys(path.resolve() for path in candidates):
        skill = candidate / "SKILL.md"
        if candidate.is_dir() and skill.is_file() and not skill.is_symlink():
            return candidate
    raise CliError(_i18n_exc("common.pi_skill_missing", "找不到 Pi BugX Skill 资源；请重新安装 BugX"))


def _copy_pi_extension(root: Path, force: bool) -> tuple[int, int, list[Path]]:
    """Install the complete Pi extension bundle as static project resources."""
    source = pi_extension_root()
    destination = root.resolve() / ".pi" / "extensions" / "bugx"
    copied = skipped = 0
    backups: list[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise CliError(_i18n_exc("common.pi_extension_symlink_refused", f"Pi Extension 资源不得包含符号链接: {path}", path=path))
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source)
        target = destination / relative
        copied_count, skipped_count, backup = _write_host_resource(
            root, target, path.read_bytes(), "pi",
        )
        copied += copied_count
        skipped += skipped_count
        if backup is not None:
            backups.append(backup)
    return copied, skipped, backups


def _plugin_manifest(
    source: Path, expected_name: str | None = None, *, validate_payload: bool = True,
) -> tuple[Path, dict[str, object]]:
    path = source / "plugin.json"
    if source.is_symlink() or path.is_symlink():
        raise CliError(_i18n_exc("common.plugin_symlink_refused", f"插件目录和清单不得是符号链接: {source}", source=source))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(_i18n_exc("common.plugin_manifest_invalid", f"插件清单无效: {path}: {exc}", path=path, exc=exc)) from exc
    name = payload.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", name):
        raise CliError(_i18n_exc("common.plugin_name_invalid", f"插件名称无效: {path}", path=path))
    version = payload.get("version")
    if not isinstance(version, str) or not PLUGIN_VERSION_RE.fullmatch(version):
        raise CliError(_i18n_exc("common.plugin_version_invalid", f"插件版本无效: {path}", path=path))
    if expected_name and name != expected_name:
        raise CliError(_i18n_exc("common.plugin_name_mismatch", f"插件名称与请求不一致: 期望 {expected_name}，实际 {name}", expected_name=expected_name, name=name))
    if payload.get("autoload") is not False or payload.get("private") is not True:
        raise CliError(_i18n_exc("common.plugin_autoload_constraint_violated", f"插件清单违反按需加载约束: {path}", path=path))
    core_api = payload.get("core_api")
    match = CORE_API_RE.fullmatch(core_api) if isinstance(core_api, str) else None
    if not match:
        raise CliError(_i18n_exc("common.plugin_core_api_missing", f"插件缺少有效 core_api（需要 >=major.minor,<major.minor）: {path}", path=path))
    lower = (int(match.group(1)), int(match.group(2)))
    upper = (int(match.group(3)), int(match.group(4)))
    current_match = re.match(r"^(\d+)\.(\d+)", __version__)
    current = (int(current_match.group(1)), int(current_match.group(2))) if current_match else (0, 0)
    if not lower <= current < upper:
        raise CliError(_i18n_exc("common.plugin_core_api_incompatible", f"插件 core_api={core_api} 与核心 {__version__} 不兼容；拒绝加载: {path}", core_api=core_api, version=__version__, path=path))
    payload_root = payload.get("payload_root", "payload")
    if (not isinstance(payload_root, str) or Path(payload_root).is_absolute()
            or ".." in Path(payload_root).parts):
        raise CliError(_i18n_exc("common.plugin_payload_root_must_be_relative", f"插件 payload_root 必须是相对路径: {path}", path=path))
    for field in ("agent", "guide"):
        value = payload.get(field)
        if not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts:
            raise CliError(_i18n_exc("common.plugin_field_must_be_relative", f"插件 {field} 必须是相对路径: {path}", field=field, path=path))
        candidate = (source / value).resolve()
        try:
            candidate.relative_to(source.resolve())
        except ValueError as exc:
            raise CliError(_i18n_exc("common.plugin_field_escapes", f"插件 {field} 不得逃出插件目录: {path}", field=field, path=path)) from exc
        if validate_payload and (not candidate.is_file() or candidate.is_symlink()):
            raise CliError(_i18n_exc("common.plugin_field_insecure", f"插件 {field} 不存在或不安全: {candidate}", field=field, candidate=candidate))
    return path, payload


def _plugin_store(root: Path) -> Path:
    return root.resolve() / ".bugx" / "plugins"


def _copy_plugin(
    source: Path, destination: Path, force: bool, *, project_root: Path | None = None,
) -> tuple[int, int, dict[str, object]]:
    manifest_path, manifest = _plugin_manifest(source)
    name = str(manifest["name"])
    payload_root_value = manifest.get("payload_root", "payload")
    payload_root = (source / str(payload_root_value)).resolve()
    try:
        payload_root.relative_to(source.resolve())
    except ValueError as exc:
        raise CliError(_i18n_exc("common.plugin_payload_root_escapes", "插件 payload_root 不得逃出插件目录")) from exc
    if not payload_root.is_dir():
        raise CliError(_i18n_exc("common.plugin_payload_root_not_found", f"插件 payload_root 不存在: {payload_root}", payload_root=payload_root))
    operations: list[tuple[Path, Path]] = []
    copied = skipped = 0
    for path in sorted(payload_root.rglob("*")):
        if path.is_symlink():
            raise CliError(_i18n_exc("common.plugin_resource_symlink_refused", f"插件资源目录不得包含符号链接: {path}", path=path))
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(payload_root)
        if path.name in RUNTIME_NAMES or path.name.startswith("findings_round"):
            continue
        target = destination / relative
        _guard_plugin_write_target(target)
        if target.exists() and not force:
            skipped += 1
            continue
        operations.append((path, target))

    owner_root = (project_root or destination.parent.parent).resolve()
    installed_manifest = _plugin_store(owner_root) / name / "plugin.json"
    if installed_manifest.exists() and not force:
        skipped += 1
    else:
        _guard_plugin_write_target(installed_manifest)
        operations.append((manifest_path, installed_manifest))

    transaction_root = destination.parent / f".plugin-install-{name}-{os.getpid()}-{time.time_ns()}"
    backup_root = _plugin_store(owner_root) / ".backups" / name / str(time.time_ns())
    staged: list[tuple[Path, Path]] = []
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        transaction_root.mkdir(parents=True, exist_ok=False)
        for index, (source_path, target) in enumerate(operations):
            staged_path = transaction_root / str(index)
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, staged_path)
            staged.append((staged_path, target))
        for index, (_, target) in enumerate(staged):
            _guard_plugin_write_target(target)
            if target.exists() or target.is_symlink():
                backup = backup_root / str(index)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
                moved.append((backup, target))
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged[index][0], target)
            created.append(target)
            copied += 1
    except (OSError, shutil.Error) as exc:
        for target in reversed(created):
            try:
                target.unlink()
            except OSError:
                pass
        for backup, target in reversed(moved):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            except OSError:
                pass
        raise CliError(_i18n_exc("common.plugin_install_rollback", f"插件安装失败，已回滚: {exc}", exc=exc)) from exc
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)
    manifest["_installed_targets"] = [
        target for target in created if target != installed_manifest
    ]
    return copied, skipped, manifest


def _installed_plugin_agent(root: Path, manifest: dict[str, object]) -> Path | None:
    """Map the source-relative agent path to the installed project payload."""
    payload_root = manifest.get("payload_root", "payload")
    agent = manifest.get("agent")
    if not isinstance(payload_root, str) or not isinstance(agent, str):
        return None
    payload = Path(payload_root)
    agent_path = Path(agent)
    try:
        relative = agent_path.relative_to(payload)
    except ValueError:
        return None
    return (root / payload / relative).resolve(strict=False)


def _relative_project_file(root: Path, path: Path) -> str:
    """Return a validated project-relative file name for an install record."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CliError(_i18n_exc("common.file_escapes_project_root", f"BugX 文件不得逃出项目根目录: {path}", path=path)) from exc
    resolved_root = root.resolve()
    engine_root = global_engine_root().resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve()
            if not target.is_relative_to(resolved_root) and not target.is_relative_to(engine_root):
                raise CliError(_i18n_exc("common.file_escapes_project_root", f"BugX 文件不得逃出项目根目录: {path}", path=path))
    return relative.as_posix()


def _file_record(root: Path, path: Path) -> dict[str, str] | None:
    if not path.is_file() or path.is_symlink():
        return None
    return {"path": _relative_project_file(root, path), "sha256": _sha256_file(path)}


def _records_for_paths(root: Path, paths: list[Path]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        record = _file_record(root, path)
        if record is None or record["path"] in seen:
            continue
        seen.add(record["path"])
        records.append(record)
    return records


def _shared_asset_records(root: Path, source: Path) -> list[dict[str, str]]:
    """Return no project records; executable assets live in the global engine."""
    return []


def _host_bundle_checks(root: Path, target_hosts: set[str] | None = None) -> tuple[list[tuple[str, bool, str]], set[str]]:
    """Validate only host bundles explicitly installed by BugX in this project."""
    from bugx_cli.host_adapters import get_adapter

    def safe_file(relative: str) -> bool:
        candidate = root / relative
        if candidate.is_file() and not candidate.is_symlink():
            return True
        if not candidate.is_symlink():
            return False
        try:
            resolved = candidate.resolve(strict=True)
            trusted = (global_engine_root(),)
            return resolved.is_file() and any(
                resolved == base / candidate.name or base in resolved.parents
                for base in trusted
            )
        except OSError:
            return False

    record_path = root / ".bugx" / "hosts.json"
    if not (root / ".bugx" / "manifest.json").is_file():
        return [], set()

    def missing_registry(detail: str) -> tuple[list[tuple[str, bool, str]], set[str]]:
        from bugx_cli.host_adapters import discover_hosts

        try:
            detected = discover_hosts(root)
        except Exception as exc:
            return [("宿主安装清单缺失或损坏", False, f"{detail}；宿主探测失败：{exc}")], set()
        names = {
            str(item["name"]) for item in detected
            if isinstance(item, dict) and item.get("installed") and isinstance(item.get("name"), str)
        }
        if target_hosts:
            names = names & target_hosts
        return [("宿主安装清单缺失或损坏", False, detail)], names

    if not record_path.is_file() or record_path.is_symlink():
        return missing_registry("hosts.json 不存在或为软链接；已按本机探测结果继续检查 MCP 配置")
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        checks, names = missing_registry(f"hosts.json 无法读取：{exc}；已按本机探测结果继续检查 MCP 配置")
        return checks, names
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, list):
        return missing_registry("hosts.json 缺少 installed 数组；已按本机探测结果继续检查 MCP 配置")
    checks: list[tuple[str, bool, str]] = []
    names: set[str] = set()
    for item in installed:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            checks.append(("宿主适配包", False, "hosts.json 包含无效宿主记录"))
            continue
        name = str(item["name"])
        if target_hosts and name not in target_hosts:
            continue
        names.add(name)
        try:
            adapter = get_adapter(name)
        except ValueError as exc:
            checks.append((f"{name} 适配包", False, str(exc)))
            continue
        if adapter.name == "zcode":
            # 0.7.22 起主/worker 子智能体收敛为仅用户级注册
            # （storage.dir/agents，默认 ~/.zcode/agents）：项目级
            # install_path 只是卸载清扫与旧副本比对的路径事实源，不再落盘，
            # 完整性改查用户级注册面的主/worker 两个文件。函数内延迟导入，
            # 避免 infra → commands 的模块级循环依赖。
            from bugx_cli.commands.plugin_zcode import _zcode_user_agents_dir

            user_agents_dir = _zcode_user_agents_dir()
            missing_user_agents = [
                filename for filename in ("bugx.md", "bugx-worker.md")
                if not (user_agents_dir / filename).is_file()
            ]
            if missing_user_agents:
                checks.append((
                    f"{adapter.label} 完整适配包", False,
                    "缺少用户级子智能体 " + "、".join(missing_user_agents)
                    + f"（{user_agents_dir}，重跑 bugx install --agent zcode 补齐）",
                ))
                continue
            missing = []
        elif adapter.name == "claude":
            target_path = root / adapter.install_path
            if not target_path.exists() and not target_path.is_symlink():
                checks.append((
                    f"{adapter.label} 完整适配包", False,
                    f"缺少宿主入口：{adapter.install_path}",
                ))
                continue
            from bugx_cli.commands.plugin_claude import _claude_user_agents_dir

            user_agents_dir = _claude_user_agents_dir()
            missing_user_agents = [
                filename for filename in ("bugx.md", "bugx-worker.md")
                if not (user_agents_dir / filename).is_file()
            ]
            if missing_user_agents:
                checks.append((
                    f"{adapter.label} 完整适配包", False,
                    "缺少用户级智能体 " + "、".join(missing_user_agents)
                    + f"（{user_agents_dir}，重跑 bugx install --agent claude 补齐）",
                ))
                continue
            required = (adapter.install_path, *adapter.required_paths)
            missing = [
                path for path in required
                if not safe_file(path)
            ]
        elif adapter.name == "qoder":
            target_path = root / adapter.install_path
            if not target_path.exists() and not target_path.is_symlink():
                checks.append((
                    f"{adapter.label} 完整适配包", False,
                    f"缺少宿主入口：{adapter.install_path}",
                ))
                continue
            from bugx_cli.commands.plugin_qoder import _qoder_user_agents_dir

            user_agents_dir = _qoder_user_agents_dir()
            missing_user_agents = [
                filename for filename in ("bugx.md", "bugx-worker.md")
                if not (user_agents_dir / filename).is_file()
            ]
            if missing_user_agents:
                checks.append((
                    f"{adapter.label} 完整适配包", False,
                    "缺少用户级智能体 " + "、".join(missing_user_agents)
                    + f"（重跑 bugx install --agent qoder 补齐）",
                ))
                continue
            required = (adapter.install_path, *adapter.required_paths)
            missing = [
                path for path in required
                if not safe_file(path)
            ]
        else:
            target_path = root / adapter.install_path
            if not target_path.exists() and not target_path.is_symlink():
                checks.append((
                    f"{adapter.label} 完整适配包", False,
                    f"缺少宿主入口：{adapter.install_path}",
                ))
                continue
            required = (adapter.install_path, *adapter.required_paths)
            missing = [
                path for path in required
                if not safe_file(path)
            ]
        drift: list[str] = []
        records = item.get("files")
        if name != "opencode":
            if not isinstance(records, list) or not records:
                drift.append("缺少安装文件哈希清单")
            else:
                for record in records:
                    if not isinstance(record, dict):
                        drift.append("安装文件哈希记录无效")
                        continue
                    relative = record.get("path")
                    expected_hash = record.get("sha256")
                    if relative == ".agents/plugins/marketplace.json":
                        continue
                    if not isinstance(relative, str) or not isinstance(expected_hash, str):
                        drift.append("安装文件哈希记录无效")
                        continue
                    try:
                        target = _safe_record_target(root, relative)
                        if target.is_symlink() or not target.is_file():
                            drift.append(f"缺少 {relative}")
                        elif _sha256_file(target) != expected_hash:
                            drift.append(f"哈希漂移 {relative}")
                    except (CliError, OSError) as exc:
                        drift.append(f"无法校验 {relative}: {exc}")
        if name == "opencode":
            expected_entry = global_engine_root() / Path(adapter.install_path).name
            expected_alt = global_engine_root() / "contracts" / "bugx-opencode.md"
            if not expected_alt.is_file():
                expected_alt = global_engine_root() / "bugx-opencode.md"
            entry = root / adapter.install_path
            try:
                resolved_entry = entry.resolve(strict=True)
                valid_targets = {expected_entry.resolve(strict=True)}
                if expected_alt.is_file():
                    valid_targets.add(expected_alt.resolve(strict=True))
                linked = (entry.is_symlink()
                          and resolved_entry in valid_targets)
                # 无符号链接特权（Windows 非管理员/非开发者模式）时安装会发布
                # 字节一致的副本；哈希命中同样视为有效入口。
                copied = (not entry.is_symlink() and entry.is_file()
                          and any(_sha256_file(entry) == _sha256_file(t)
                                  for t in tuple(valid_targets)))
                if not (linked or copied):
                    drift.append("项目入口未指向当前全局引擎")
            except OSError:
                drift.append("项目入口是悬空或不可解析的符号链接")
        if missing:
            checks.append((
                f"{adapter.label} 完整适配包", False,
                "缺少：" + ", ".join(missing),
            ))
        elif drift:
            checks.append((
                f"{adapter.label} 完整适配包", False,
                "；".join(drift[:8]),
            ))
        else:
            checks.append((
                f"{adapter.label} 完整适配包", True,
                "Skill、Extension/入口和清单均存在",
            ))
    return checks, names


def _remove_empty_parents(root: Path, paths: list[str]) -> None:
    """Tidy only directories made empty by uninstall, never remove project dirs."""
    root = root.resolve()
    candidates: set[Path] = set()
    for relative in paths:
        clean_rel = relative.split("（")[0].strip()
        try:
            target_path = Path(clean_rel)
            if target_path.is_absolute():
                target = target_path.resolve().parent
                if not target.is_relative_to(root):
                    continue
            else:
                target = (root / target_path).resolve().parent
                if not target.is_relative_to(root):
                    continue
        except (ValueError, OSError):
            continue
        while target != root and target != root.parent and target != target.parent:
            candidates.add(target)
            target = target.parent
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

