"""MCP runtime lock validation, hashing, and runtime verification."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import sys
from pathlib import Path
from typing import Any, Callable

import core.mcp_runtime_support.common as _c

McpRuntimeError = _c.McpRuntimeError
canonical_json_bytes = _c.canonical_json_bytes
_fsync_directory = _c._fsync_directory
_check_venv_shebang = _c._check_venv_shebang
_read_pyvenv_version = _c._read_pyvenv_version
_allowed_minors = _c._allowed_minors
_normalize_python_spec = _c._normalize_python_spec
parse_python_requires = _c.parse_python_requires
check_python_requires = _c.check_python_requires
read_json_file = _c.read_json_file
reject_symlink_components = _c.reject_symlink_components
sha256_file = _c.sha256_file
sha256_hex = _c.sha256_hex
utc_now = _c.utc_now
write_json_atomic = _c.write_json_atomic
_is_world_writable = _c._is_world_writable
BYTECODE_GUARD_NAME = _c.BYTECODE_GUARD_NAME
CURRENT_LINK = _c.CURRENT_LINK
DEFAULT_MIN_NODE = _c.DEFAULT_MIN_NODE
ECOSYSTEMS = _c.ECOSYSTEMS
ECOSYSTEM_NPM = _c.ECOSYSTEM_NPM
ECOSYSTEM_PYPI = _c.ECOSYSTEM_PYPI
FILEHASH_NAME = _c.FILEHASH_NAME
HEX64_RE = _c.HEX64_RE
HISTORY_LIMIT = _c.HISTORY_LIMIT
HISTORY_NAME = _c.HISTORY_NAME
INTEGRITY_RE = _c.INTEGRITY_RE
LOCK_SCHEMA_VERSION = _c.LOCK_SCHEMA_VERSION
MANIFEST_KIND = _c.MANIFEST_KIND
MANIFEST_NAME = _c.MANIFEST_NAME
MANIFEST_SCHEMA_VERSION = _c.MANIFEST_SCHEMA_VERSION
MAX_REPORTED_MISMATCHES = _c.MAX_REPORTED_MISMATCHES
NODE_VERSION_RE = _c.NODE_VERSION_RE
PACKAGE_KEY_RE = _c.PACKAGE_KEY_RE
PYPI_ENVIRONMENTS = _c.PYPI_ENVIRONMENTS
PYPI_INSTALL_FLAGS = _c.PYPI_INSTALL_FLAGS
PYPI_REQUIREMENTS_NAME = _c.PYPI_REQUIREMENTS_NAME
PYPI_SECTION = _c.PYPI_SECTION
PYTHON_REQUIRES_OPERATORS = _c.PYTHON_REQUIRES_OPERATORS
SHA256_HASH_RE = _c.SHA256_HASH_RE
STAGING_PREFIX = _c.STAGING_PREFIX
TREE_HASH_EXCLUDE = _c.TREE_HASH_EXCLUDE
VENV_DIRNAME = _c.VENV_DIRNAME
VERSION_RE = _c.VERSION_RE
WORLD_WRITE_BITS = _c.WORLD_WRITE_BITS

def load_lock(lock_path: Path) -> dict:
    lock_path = reject_symlink_components(Path(lock_path))
    lock = read_json_file(lock_path, "MCP lock")
    if not isinstance(lock, dict):
        raise McpRuntimeError("MCP lock 顶层必须是 JSON 对象")
    allowed = {"schema_version", "packages", "servers", PYPI_SECTION}
    unknown = sorted(set(lock) - allowed)
    if unknown:
        raise McpRuntimeError(f"MCP lock 含未知字段: {', '.join(unknown)}")
    if lock.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise McpRuntimeError(
            f"MCP lock schema_version 必须为 {LOCK_SCHEMA_VERSION}；"
            "v2 及更早版本不再支持（包键须带 npm:/pypi: 生态前缀），"
            f"实际为 {lock.get('schema_version')!r}"
        )
    packages = lock.get("packages")
    servers = lock.get("servers")
    if not isinstance(packages, dict) or not packages:
        raise McpRuntimeError("MCP lock 缺少非空 packages 对象")
    if not isinstance(servers, dict) or not servers:
        raise McpRuntimeError("MCP lock 缺少非空 servers 对象")
    for key, meta in packages.items():
        match = PACKAGE_KEY_RE.match(str(key))
        if match is None:
            raise McpRuntimeError(
                f"lock 包键必须为 <ecosystem>:<name>（npm:/pypi:）: {key!r}"
            )
        if match.group(1) != ECOSYSTEM_NPM:
            raise McpRuntimeError(
                f"lock 顶层 packages 只承载 npm 包；pypi 包必须放在 "
                f"pypi.{PYPI_ENVIRONMENTS}.<env>.packages 下: {key!r}"
            )
        if not isinstance(meta, dict):
            raise McpRuntimeError(f"lock 包 {key} 元数据必须是对象")
        version = meta.get("version")
        if not isinstance(version, str) or not VERSION_RE.match(version):
            raise McpRuntimeError(f"lock 包 {key} 版本非法: {version!r}")
        if meta.get("ecosystem") != ECOSYSTEM_NPM:
            raise McpRuntimeError(
                f"lock 包 {key} 的 ecosystem 字段必须与键前缀一致（{ECOSYSTEM_NPM!r}）"
            )
        integrity = meta.get("integrity")
        if not isinstance(integrity, str) or not INTEGRITY_RE.match(integrity):
            raise McpRuntimeError(
                f"lock 包 {key} integrity 非法（需 sha512-…): {integrity!r}"
            )
        unknown = sorted(set(meta) - {"ecosystem", "version", "integrity"})
        if unknown:
            raise McpRuntimeError(f"lock 包 {key} 含未知字段: {', '.join(unknown)}")
    section = lock.get(PYPI_SECTION)
    pypi_servers = [
        server for server, meta in servers.items()
        if isinstance(meta, dict)
        and str(meta.get("package", "")).startswith(f"{ECOSYSTEM_PYPI}:")
    ]
    if pypi_servers and section is None:
        raise McpRuntimeError("lock 含 pypi server，必须提供顶层 pypi 段")
    if not pypi_servers and section is not None:
        raise McpRuntimeError("lock 未含 pypi server，但声明了顶层 pypi 段")
    environments: dict[str, dict[str, dict]] = {}
    if section is not None:
        if not isinstance(section, dict):
            raise McpRuntimeError("lock pypi 段必须是对象")
        unknown = sorted(set(section) - {"python_requires", PYPI_ENVIRONMENTS})
        if unknown:
            raise McpRuntimeError(f"lock pypi 段含未知字段: {', '.join(unknown)}")
        spec = section.get("python_requires")
        if not isinstance(spec, str) or not spec.strip():
            raise McpRuntimeError("lock pypi.python_requires 必须为非空字符串")
        parse_python_requires(spec)
        raw_environments = section.get(PYPI_ENVIRONMENTS)
        if not isinstance(raw_environments, dict) or not raw_environments:
            raise McpRuntimeError(
                f"lock pypi.{PYPI_ENVIRONMENTS} 必须为非空对象"
            )
        for env_id, env_meta in raw_environments.items():
            if not isinstance(env_meta, dict):
                raise McpRuntimeError(f"pypi 环境 {env_id} 元数据必须是对象")
            unknown = sorted(set(env_meta) - {"packages", "python"})
            if unknown:
                raise McpRuntimeError(
                    f"pypi 环境 {env_id} 含未知字段: {', '.join(unknown)}"
                )
            python_spec = env_meta.get("python")
            if python_spec is not None:
                # 允许 **约束**（">=3.12,<3.14"）或 "3.12" 前缀式；不再写死小版本。
                if not isinstance(python_spec, str) or not python_spec.strip():
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的 python 必须是非空字符串，"
                        f"实际为 {python_spec!r}"
                    )
                normalized = _normalize_python_spec(python_spec)
                try:
                    parse_python_requires(normalized)
                except McpRuntimeError as exc:
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的 python 约束非法: {exc}"
                    ) from exc
                if not _allowed_minors(python_spec):
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的 python 约束 {python_spec!r} 无可行版本"
                    )
            env_packages = env_meta.get("packages")
            if not isinstance(env_packages, dict) or not env_packages:
                raise McpRuntimeError(f"pypi 环境 {env_id} 缺少非空 packages")
            validated: dict[str, dict] = {}
            for pkg_name, pkg_meta in env_packages.items():
                if not isinstance(pkg_meta, dict):
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的包 {pkg_name} 元数据必须是对象"
                    )
                unknown = sorted(set(pkg_meta) - {"version", "hashes"})
                if unknown:
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的包 {pkg_name} 含未知字段: "
                        f"{', '.join(unknown)}"
                    )
                version = pkg_meta.get("version")
                if not isinstance(version, str) or not VERSION_RE.match(version):
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的包 {pkg_name} 版本非法: {version!r}"
                    )
                hashes = pkg_meta.get("hashes")
                if not isinstance(hashes, (list, tuple)) or not hashes:
                    raise McpRuntimeError(
                        f"pypi 环境 {env_id} 的包 {pkg_name} 缺少非空 hashes"
                    )
                for item in hashes:
                    if not isinstance(item, str) or not SHA256_HASH_RE.match(item):
                        raise McpRuntimeError(
                            f"pypi 环境 {env_id} 的包 {pkg_name} 的 hash 非法"
                            f"（需 sha256:<64hex>): {item!r}"
                        )
                validated[str(pkg_name)] = {
                    "version": version, "hashes": list(hashes),
                }
            environments[str(env_id)] = validated

    for server, meta in servers.items():
        if not isinstance(meta, dict):
            raise McpRuntimeError(f"lock server {server} 元数据必须是对象")
        package = meta.get("package")
        if not isinstance(package, str):
            raise McpRuntimeError(f"lock server {server} 的 package 非法: {package!r}")
        match = PACKAGE_KEY_RE.match(package)
        if match is None:
            raise McpRuntimeError(
                f"lock server {server} 的 package 必须带生态前缀（npm:/pypi:）: {package!r}"
            )
        ecosystem, package_name = match.group(1), match.group(2)
        if ecosystem == ECOSYSTEM_NPM:
            if package not in packages:
                raise McpRuntimeError(
                    f"lock server {server} 引用了未锁定的包: {package!r}"
                )
            unknown = sorted(set(meta) - {"package", "enabled", "disabled_reason"})
            if unknown:
                raise McpRuntimeError(
                    f"lock server {server} 含未知字段: {', '.join(unknown)}"
                )
        else:
            unknown = sorted(set(meta) - {"package", "enabled", "entry", "venv", "args", "disabled_reason"})
            if unknown:
                raise McpRuntimeError(
                    f"lock server {server} 含未知字段: {', '.join(unknown)}"
                )
            args = meta.get("args")
            if args is not None:
                if not isinstance(args, list) or not args or any(
                    not isinstance(item, str) or not item.strip() for item in args
                ):
                    raise McpRuntimeError(
                        f"lock server {server} 的 args 必须是非空字符串数组"
                    )
            entry = meta.get("entry")
            if (
                not isinstance(entry, str) or not entry
                or "/" in entry or "\\" in entry or entry.startswith(".")
            ):
                raise McpRuntimeError(
                    f"lock server {server} 为 pypi 服务，必须声明 entry"
                    "（venv 内 console script 名，不得含路径分隔符）"
                )
            env_id = meta.get("venv", server)
            if not isinstance(env_id, str) or not env_id:
                raise McpRuntimeError(f"lock server {server} 的 venv 必须是非空字符串")
            if env_id not in environments:
                raise McpRuntimeError(
                    f"lock server {server} 引用了不存在的 pypi 环境: {env_id!r}"
                )
            if package_name not in environments[env_id]:
                raise McpRuntimeError(
                    f"lock server {server} 的包 {package_name!r} 不在 pypi 环境 "
                    f"{env_id!r} 中"
                )
        enabled = meta.get("enabled", True)
        if not isinstance(enabled, bool):
            raise McpRuntimeError(f"lock server {server} enabled 必须是布尔值")
        reason = meta.get("disabled_reason")
        if reason is not None:
            # 停用必须有**可审计的理由**（数据事实），否则运行时只能猜或硬编码。
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 240:
                raise McpRuntimeError(
                    f"lock server {server} 的 disabled_reason 非法"
                    "（需 1..240 个字符的字符串）"
                )
            if enabled is not False:
                raise McpRuntimeError(
                    f"lock server {server} 声明了 disabled_reason 但 enabled 不是 false"
                )

    return lock


def parse_python_requires(spec: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """解析 ``python_requires``（支持 ``>=``/``>``/``<=``/``<``/``==``/``!=`` 逗号列表）。

    **fail-closed**：任何无法解析的片段直接抛 ``McpRuntimeError``（DOC-235 设计项 3）。
    """
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw in str(spec).split(","):
        clause = raw.strip()
        if not clause:
            continue
        operator = ""
        for candidate in PYTHON_REQUIRES_OPERATORS:
            if clause.startswith(candidate):
                operator = candidate
                break
        if not operator:
            raise McpRuntimeError(f"python_requires 片段缺少合法运算符: {clause!r}")
        version = clause[len(operator):].strip()
        parts = version.split(".")
        if not parts or any(not part.isdigit() for part in parts):
            raise McpRuntimeError(f"python_requires 版本号非法: {version!r}")
        clauses.append((operator, tuple(int(part) for part in parts)))
    if not clauses:
        raise McpRuntimeError("python_requires 不能为空")
    return tuple(clauses)


def check_python_requires(
    spec: str, version: tuple[int, ...] | None = None,
) -> tuple[bool, str]:
    """校验解释器版本是否满足 ``python_requires``；返回 (是否满足, 说明)。"""
    clauses = parse_python_requires(spec)
    current = tuple(version) if version is not None else tuple(sys.version_info[:3])
    shown = ".".join(str(item) for item in current)
    for operator, wanted in clauses:
        have = current[: len(wanted)]
        satisfied = {
            ">=": have >= wanted,
            ">": have > wanted,
            "<=": have <= wanted,
            "<": have < wanted,
            "==": have == wanted,
            "!=": have != wanted,
        }[operator]
        if not satisfied:
            return False, (
                f"python_requires {spec!r} 不满足：当前解释器 {shown} "
                f"（lock 由更高版本解析，禁止降级解析）"
            )
    return True, f"python_requires {spec!r} 满足（当前 {shown}）"


def lock_hash(lock: dict) -> str:
    return sha256_hex(canonical_json_bytes(lock))


# ---------------------------------------------------------------------------
# 文件树清单与完整性
# ---------------------------------------------------------------------------


def build_file_map(root: Path) -> dict[str, dict[str, object]]:
    """Return {relpath: {"sha256": …, "size": …}} over a regular-file tree."""
    files: dict[str, dict[str, object]] = {}
    root = Path(root)
    stack = [root]
    while stack:
        directory = stack.pop()
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                rel = entry.relative_to(root).as_posix()
                raise McpRuntimeError(f"runtime 内出现符号链接，fail-closed: {rel}")
            if entry.is_dir():
                stack.append(entry)
                continue
            if not entry.is_file():
                raise McpRuntimeError(
                    f"runtime 内出现非常规文件，fail-closed: "
                    f"{entry.relative_to(root).as_posix()}"
                )
            rel = entry.relative_to(root).as_posix()
            files[rel] = {
                "sha256": sha256_file(entry),
                "size": entry.stat().st_size,
            }
    return {rel: files[rel] for rel in sorted(files) if rel not in TREE_HASH_EXCLUDE}


def compute_tree_hash(files: dict[str, dict[str, object]]) -> str:
    rows = [[rel, str(meta["sha256"]), int(meta["size"])]
            for rel, meta in sorted(files.items())]
    return sha256_hex(canonical_json_bytes(rows))


def chmod_tree(root: Path, executable_relpaths: set[str]) -> None:
    """规范化权限：目录 0755、文件 0644，但**保留可执行位**。

    实测缺陷：``semgrep`` 的 wheel 在 ``site-packages/semgrep/bin/semgrep-core``
    放了一个 169MB 原生二进制，权限由 wheel 元数据决定（0755）。若只按
    ``venv/bin`` 白名单决定 +x，该二进制会被压成 0644 ⇒ 运行时
    ``PermissionError: [Errno 13]``（semgrep 无法启动）。
    因此：白名单 **或** 安装树里本来就带可执行位 ⇒ 保持 0755。
    """
    exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    os.chmod(root, 0o755)
    for directory, _dirnames, filenames in os.walk(root):
        dir_path = Path(directory)
        os.chmod(dir_path, 0o755)
        for filename in filenames:
            candidate = dir_path / filename
            rel = candidate.relative_to(root).as_posix()
            try:
                already_executable = bool(candidate.lstat().st_mode & exec_bits)
            except OSError:
                already_executable = False
            executable = rel in executable_relpaths or already_executable
            os.chmod(candidate, 0o755 if executable else 0o644)


# ---------------------------------------------------------------------------
# Node 兼容性
# ---------------------------------------------------------------------------


def find_node(override: str | None = None) -> Path:
    candidate = override or os.environ.get("BUGX_NODE_BIN") or shutil.which("node")
    if not candidate:
        raise McpRuntimeError(
            "找不到 node 可执行文件；MCP runtime 启动需要本地 Node.js"
        )
    node = Path(candidate)
    if node.is_symlink() or not node.is_file():
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else str(node)
        if not resolved:
            raise McpRuntimeError(f"node 可执行文件不可用: {candidate}")
        node = Path(resolved)
    return node


def parse_node_version(output: str) -> tuple[int, int, int] | None:
    match = NODE_VERSION_RE.match(output.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def node_version_tuple(node_bin: Path) -> tuple[int, int, int]:
    try:
        result = subprocess.run(
            [str(node_bin), "--version"], capture_output=True, text=True,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise McpRuntimeError(f"无法获取 node 版本: {exc}") from exc
    if result.returncode != 0:
        raise McpRuntimeError(
            f"node --version 失败: {(result.stderr or result.stdout).strip()[:200]}"
        )
    parsed = parse_node_version(result.stdout)
    if parsed is None:
        raise McpRuntimeError(f"无法解析 node 版本输出: {result.stdout.strip()[:100]}")
    return parsed


def engines_requirement_floor(requirement: str) -> tuple[int, int, int] | None:
    text = requirement.strip()
    match = re.match(r"^>=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?$", text)
    if match:
        parts = [match.group(i) or "0" for i in (1, 2, 3)]
        return tuple(int(part) for part in parts)  # type: ignore[return-value]
    if VERSION_RE.match(text):
        pieces = [piece for piece in text.split(".") if piece.isdigit()]
        if pieces:
            while len(pieces) < 3:
                pieces.append("0")
            return tuple(int(piece) for piece in pieces[:3])  # type: ignore[return-value]
    return None


def check_node_compatibility(
    engines: dict[str, str | None], node_version: tuple[int, int, int],
) -> tuple[bool, str]:
    problems: list[str] = []
    checked = 0
    for package, requirement in engines.items():
        if not requirement:
            continue
        floor = engines_requirement_floor(requirement)
        if floor is None:
            # 无法解析的语义化范围不猜测；记录为已观察约束但不阻断。
            continue
        checked += 1
        if node_version < floor:
            problems.append(f"{package} 要求 node {requirement}")
    if problems:
        joined = "v%d.%d.%d" % node_version + "；" + "；".join(problems)
        return False, f"Node 兼容性不足：当前 {joined}"
    if checked == 0:
        return True, f"Node v%d.%d.%d 满足默认下限 >=%d" % (
            *node_version, DEFAULT_MIN_NODE[0],
        )
    return True, f"Node v%d.%d.%d 满足全部 engines 约束" % node_version


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def required_manifest_fields(manifest: dict) -> list[str]:
    required = (
        "schema_version", "kind", "lock_hash", "created_at", "packages",
        "servers", "tree_hash",
    )
    return [field for field in required if field not in manifest]


def load_manifest(runtime_dir: Path) -> dict:
    manifest = read_json_file(runtime_dir / MANIFEST_NAME, "runtime-manifest.json")
    if not isinstance(manifest, dict):
        raise McpRuntimeError("runtime-manifest.json 顶层必须是 JSON 对象")
    missing = required_manifest_fields(manifest)
    if missing:
        raise McpRuntimeError(f"runtime-manifest.json 缺少字段: {', '.join(missing)}")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise McpRuntimeError(
            f"runtime-manifest schema_version 必须为 {MANIFEST_SCHEMA_VERSION}"
        )
    if manifest["kind"] != MANIFEST_KIND:
        raise McpRuntimeError(f"runtime-manifest kind 非法: {manifest['kind']!r}")
    if not isinstance(manifest["packages"], dict) or not isinstance(manifest["servers"], dict):
        raise McpRuntimeError("runtime-manifest packages/servers 必须是对象")
    if not HEX64_RE.match(str(manifest["lock_hash"])) or \
            not HEX64_RE.match(str(manifest["tree_hash"])):
        raise McpRuntimeError("runtime-manifest 哈希字段格式非法")
    return manifest


def resolve_server_entry(manifest: dict, server: str) -> tuple[str, str]:
    servers = manifest["servers"]
    meta = servers.get(server)
    if not isinstance(meta, dict):
        known = ", ".join(sorted(servers)) or "无"
        raise McpRuntimeError(f"未知 MCP server: {server}（可用：{known}）")
    if meta.get("enabled") is False:
        # 理由必须来自 lock（数据事实），不得在此硬编码：pypi 用 --only-binary
        # 安装，与 npm 的 install script 风险模型不同，套用同一句话会误导排查。
        reason = str(meta.get("disabled_reason") or "lock 未给出停用理由").strip()
        raise McpRuntimeError(
            f"MCP server {server} 在批准 lock 中标记为 disabled：{reason}；"
            "如需启用，先完成供应链证明并更新 lock"
        )
    entry = meta.get("entry")
    expected = meta.get("entry_sha256")
    if not isinstance(entry, str) or not entry or \
            not isinstance(expected, str) or not HEX64_RE.match(expected):
        raise McpRuntimeError(f"server {server} 在 manifest 中缺少合法入口记录")
    if entry.startswith("/") or ".." in Path(entry).parts:
        raise McpRuntimeError(f"server {server} 入口路径越界: {entry}")
    return entry, expected


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def verify_runtime(
    runtime_dir: Path,
    *,
    expect_hash: str | None = None,
    check_dirname: bool = True,
    node_override: str | None = None,
    published_dir: Path | None = None,
    fast: bool = False,
) -> dict:
    """深度校验一个 runtime 目录；返回结构化报告（ok/checks/issues）。"""
    checks: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    runtime_dir = Path(runtime_dir)
    if runtime_dir.is_symlink() or not runtime_dir.is_dir():
        record("runtime 目录", False, f"缺失、不是目录或为符号链接: {runtime_dir}")
        return {"ok": False, "checks": checks, "issues": ["runtime 目录缺失"]}
    record("runtime 目录", True, str(runtime_dir))

    try:
        manifest = load_manifest(runtime_dir)
    except McpRuntimeError as exc:
        record("runtime manifest", False, str(exc))
        return {"ok": False, "checks": checks, "issues": [str(exc)]}
    record("runtime manifest", True,
           f"lock={str(manifest['lock_hash'])[:12]} 创建于 {manifest['created_at']}")

    dirname_ok = (not check_dirname) or runtime_dir.name == manifest["lock_hash"]
    detail = f"目录名 {runtime_dir.name} vs manifest {manifest['lock_hash']}"
    if expect_hash is not None and manifest["lock_hash"] != expect_hash:
        dirname_ok = False
        detail += f" vs 期望 {expect_hash}"
    record("lock hash 一致", dirname_ok, detail)

    issues: list[str] = []
    entry_failures = 0
    active_servers = 0
    for server in sorted(manifest["servers"]):
        server_meta = manifest["servers"][server]
        if isinstance(server_meta, dict) and server_meta.get("enabled") is False:
            # lock 停用的服务不校验入口启动面；其文件仍受树完整性约束。
            continue
        active_servers += 1
        try:
            entry_rel, expected = resolve_server_entry(manifest, server)
        except McpRuntimeError as exc:
            entry_failures += 1
            issues.append(str(exc))
            continue
        entry_path = runtime_dir / entry_rel
        if entry_path.is_symlink() or not entry_path.is_file():
            entry_failures += 1
            message = f"server {server} 入口缺失或不是普通文件: {entry_rel}"
            issues.append(message)
            continue
        actual = sha256_file(entry_path)
        mode = entry_path.stat().st_mode
        if actual != expected:
            entry_failures += 1
            issues.append(
                f"server {server} 入口被篡改: {entry_rel} 期望 "
                f"{expected[:16]}… 实际 {actual[:16]}…"
            )
            continue
        if _is_world_writable(mode):
            entry_failures += 1
            issues.append(
                f"server {server} 入口权限过宽（world-writable）: {entry_rel}"
            )
            continue
        # P0 不变量（DOC-235 §6）：pypi 入口是带 shebang 的 console script，
        # 其解释器必须**存在且位于本 runtime 内**（staging 路径会随 rename 消失，
        # 直接执行必然报 "No such file or directory"）。
        if server_meta.get("ecosystem") == ECOSYSTEM_PYPI:
            shebang_issue = _check_venv_shebang(
                runtime_dir, entry_path, entry_rel, published_dir=published_dir,
            )
            if shebang_issue:
                entry_failures += 1
                issues.append(f"server {server} {shebang_issue}")
    if entry_failures:
        record("入口完整性", False, "；".join(issues[-entry_failures:])[:500])
    else:
        record("入口完整性", True,
               f"{active_servers} 个启用入口 hash 与 manifest 一致"
               + (f"（{len(manifest['servers']) - active_servers} 个按 lock 停用）"
                  if active_servers < len(manifest["servers"]) else ""))

    # Node 兼容性只对 npm 包有意义（DOC-235：pypi-only runtime 无 npm 包时跳过）。
    npm_engines: dict[str, str | None] = {
        str(name): (meta.get("engines_node") if isinstance(meta, dict) else None)
        for name, meta in manifest["packages"].items()
        if not isinstance(meta, dict)
        or meta.get("ecosystem", ECOSYSTEM_NPM) == ECOSYSTEM_NPM
    }
    if not npm_engines:
        record("Node 兼容性", True, "未检查（本 runtime 无 npm 包）")
    else:
        node_ok = True
        node_detail = ""
        try:
            node_bin = find_node(node_override)
            version = node_version_tuple(node_bin)
            node_ok, node_detail = check_node_compatibility(npm_engines, version)
            if not node_ok:
                issues.append(node_detail)
        except McpRuntimeError as exc:
            node_ok = False
            node_detail = str(exc)
            issues.append(node_detail)
        record("Node 兼容性", node_ok, node_detail)

    tree_ok = True
    tree_detail = ""
    try:
        stored = read_json_file(runtime_dir / FILEHASH_NAME, FILEHASH_NAME)
        if not isinstance(stored, dict):
            raise McpRuntimeError(f"{FILEHASH_NAME} 顶层必须是对象")
        if fast:
            actual_tree = compute_tree_hash(stored)
            if actual_tree != manifest["tree_hash"]:
                mismatches = [
                    f"tree_hash: 期望 {manifest['tree_hash'][:16]}… 实际 {actual_tree[:16]}…"
                ]
                tree_ok = False
                tree_detail = "；".join(mismatches)
                issues.extend(mismatches)
            else:
                tree_detail = (
                    f"快速校验：{len(stored)} 个文件存储树哈希一致；"
                    f"tree_hash={actual_tree[:12]}"
                )
        else:
            fresh = build_file_map(runtime_dir)
            current_files = dict(fresh)
            mismatches = []
            for rel, meta in sorted(stored.items()):
                live = current_files.pop(rel, None)
                if live is None:
                    mismatches.append(f"{rel}: 文件缺失")
                elif live["sha256"] != meta.get("sha256"):
                    mismatches.append(
                        f"{rel}: 期望 {str(meta.get('sha256'))[:16]}… "
                        f"实际 {live['sha256'][:16]}…"
                    )
            for rel in sorted(current_files):
                mismatches.append(f"{rel}: 多出的未登记文件")
            # 树哈希必须基于当前真实内容重算：
            actual_tree = compute_tree_hash(fresh)
            if actual_tree != manifest["tree_hash"]:
                mismatches.append(
                    f"tree_hash: 期望 {manifest['tree_hash'][:16]}… 实际 {actual_tree[:16]}…"
                )
            if mismatches:
                tree_ok = False
                shown = "；".join(mismatches[:MAX_REPORTED_MISMATCHES])
                more = "" if len(mismatches) <= MAX_REPORTED_MISMATCHES else \
                    f"（共 {len(mismatches)} 处）"
                tree_detail = f"{shown}{more}"
                issues.extend(mismatches)
            else:
                tree_detail = (
                    f"{len(fresh)} 个文件全部与登记 hash 一致；"
                    f"tree_hash={actual_tree[:12]}"
                )
    except McpRuntimeError as exc:
        tree_ok = False
        tree_detail = str(exc)
        issues.append(tree_detail)
    record("文件树完整性", tree_ok, tree_detail)

    # pypi 环境解释器一致性（DOC-235 §6）：manifest 记录的解释器版本必须满足
    # python_requires，且与 venv 内 pyvenv.cfg 一致（fail-closed，可发现篡改）。
    env_meta = manifest.get("pypi_environments")
    requires = manifest.get("python_requires")
    if isinstance(env_meta, dict) and env_meta and isinstance(requires, str):
        env_issues: list[str] = []
        for env_id, meta in sorted(env_meta.items()):
            recorded = str((meta or {}).get("python_version") or "")
            if not recorded:
                env_issues.append(f"{env_id}: manifest 缺少 python_version")
                continue
            ok, detail = check_python_requires(
                requires, tuple(int(part) for part in recorded.split(".")),
            )
            if not ok:
                env_issues.append(f"{env_id}: {detail}")
            live = _read_pyvenv_version(
                runtime_dir / VENV_DIRNAME / env_id / "pyvenv.cfg",
            )
            if live and live != recorded:
                env_issues.append(
                    f"{env_id}: venv 解释器 {live} 与 manifest {recorded} 不一致"
                )
        record(
            "pypi 环境解释器", not env_issues,
            "；".join(env_issues) or f"{len(env_meta)} 个环境解释器版本一致且满足 {requires}",
        )
        if env_issues:
            issues.extend(env_issues)

    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "issues": issues,
        "lock_hash": str(manifest["lock_hash"]),
    }


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


def load_history(runtime_root: Path) -> list[str]:
    history_path = runtime_root / HISTORY_NAME
    if not history_path.exists():
        return []
    history = read_json_file(history_path, "runtime 历史")
    if not isinstance(history, dict):
        raise McpRuntimeError("history.json 顶层必须是对象")
    entries = history.get("approved")
    if not isinstance(entries, list) or not all(
        isinstance(item, str) and HEX64_RE.match(item) for item in entries
    ):
        raise McpRuntimeError("history.json approved 列表非法")
    return entries


def update_history(runtime_root: Path, lock_hash_value: str) -> list[str]:
    entries = load_history(runtime_root)
    if not entries or entries[-1] != lock_hash_value:
        entries.append(lock_hash_value)
    entries = entries[-HISTORY_LIMIT:]
    write_json_atomic(
        runtime_root / HISTORY_NAME,
        {"schema_version": 1, "approved": entries}, 0o644,
    )
    return entries


def switch_current(runtime_root: Path, lock_hash_value: str) -> None:
    target = runtime_root / lock_hash_value
    if target.is_symlink() or not target.is_dir():
        raise McpRuntimeError(f"拒绝把 current 指向非法目标: {target}")
    link = runtime_root / CURRENT_LINK
    temporary = runtime_root / f".current-{uuid.uuid4().hex[:8]}"
    os.symlink(lock_hash_value, temporary)
    os.replace(temporary, link)
    _fsync_directory(runtime_root)


def read_current(runtime_root: Path) -> str | None:
    link = runtime_root / CURRENT_LINK
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    name = Path(target).name
    if not HEX64_RE.match(name):
        raise McpRuntimeError(f"current 指向非法目标: {target}")
    return name

