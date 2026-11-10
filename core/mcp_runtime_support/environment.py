"""Python venv and npm package manager installation environments for MCP runtime."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import core.mcp_runtime_support.common as _c

McpRuntimeError = _c.McpRuntimeError
BYTECODE_GUARD_NAME = _c.BYTECODE_GUARD_NAME
NPM_TIMEOUT_SECONDS = _c.NPM_TIMEOUT_SECONDS
PACKAGE_KEY_RE = _c.PACKAGE_KEY_RE
PYPI_INSTALL_FLAGS = _c.PYPI_INSTALL_FLAGS
PYPI_REQUIREMENTS_NAME = _c.PYPI_REQUIREMENTS_NAME
VENV_DIRNAME = _c.VENV_DIRNAME
STAGING_PREFIX = _c.STAGING_PREFIX
PYPI_SECTION = _c.PYPI_SECTION
read_json_file = _c.read_json_file
write_json_atomic = _c.write_json_atomic
check_python_requires = _c.check_python_requires
_normalize_python_spec = _c._normalize_python_spec
_allowed_minors = _c._allowed_minors
_read_pyvenv_version = _c._read_pyvenv_version
_check_venv_shebang = _c._check_venv_shebang


def _get_mr_attr(name: str, fallback: Any) -> Any:
    mod = sys.modules.get("core.skills.mcp_runtime")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return fallback

def _npm_executable(override: str | None) -> str:
    candidate = override or os.environ.get("BUGX_NPM_BIN") or shutil.which("npm")
    if not candidate:
        raise McpRuntimeError(
            "找不到 npm；安装离线 MCP runtime 需要 npm（仅在安装阶段联网）"
        )
    return candidate


def _resolve_bins(package_meta: dict, package: str, server: str) -> tuple[str, str]:
    bin_field = package_meta.get("bin")
    candidates: dict[str, str] = {}
    if isinstance(bin_field, str):
        candidates[Path(package).name] = bin_field
    elif isinstance(bin_field, dict):
        for key, value in bin_field.items():
            if isinstance(key, str) and isinstance(value, str):
                candidates[key] = value
    if not candidates:
        raise McpRuntimeError(f"包 {package} 未声明 bin 入口，无法作为 MCP 启动")
    unscoped = Path(package).name
    chosen_key = (
        server if server in candidates
        else unscoped if unscoped in candidates
        else sorted(candidates)[0]
    )
    relative = candidates[chosen_key]
    entry = (Path("node_modules") / package / relative).as_posix()
    if ".." in Path(entry).parts or entry.startswith("/"):
        raise McpRuntimeError(f"包 {package} 的 bin 路径越界: {relative}")
    return chosen_key, entry


def _install_npm_packages(
    staging: Path, npm_packages: dict, *, npm_bin: str | None, timeout: int,
) -> dict:
    """按 lock 的 npm 段做受限安装；返回 package-lock.json 的 packages 映射。

    安全不变量：安装期联网但强制 ``--ignore-scripts``（禁任意代码执行）。
    """
    dependencies = {
        key.split(":", 1)[1]: str(meta["version"])
        for key, meta in npm_packages.items()
    }
    write_json_atomic(staging / "package.json", {
        "name": "bugx-mcp-runtime",
        "private": True,
        "version": "0.0.0",
        "description": "BugX offline MCP runtime; generated from mcp-lock.json",
        "dependencies": dependencies,
    }, 0o644)
    npm = _npm_executable(npm_bin)
    command = [
        npm, "install", "--ignore-scripts", "--no-audit", "--no-fund",
        "--no-bin-links", "--loglevel=error",
    ]
    environment = dict(os.environ)
    environment["npm_config_ignore_scripts"] = "true"
    environment["npm_config_bin_links"] = "false"
    try:
        completed = subprocess.run(
            command, cwd=str(staging), capture_output=True, text=True,
            check=False, timeout=timeout, env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise McpRuntimeError(
            f"npm install 超时（{timeout}s）；已放弃本次安装: {exc}"
        ) from exc
    if completed.returncode != 0:
        tail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-800:]
        raise McpRuntimeError(f"npm install 失败（exit {completed.returncode}）：{tail}")
    installed_lock = read_json_file(staging / "package-lock.json", "package-lock.json")
    if not isinstance(installed_lock, dict):
        raise McpRuntimeError("package-lock.json 顶层必须是对象")
    locked = installed_lock.get("packages")
    if not isinstance(locked, dict):
        raise McpRuntimeError("package-lock.json 缺少 packages 映射")
    return locked


def _venv_dir(staging: Path, env_id: str) -> Path:
    return staging / VENV_DIRNAME / env_id


def _venv_bin_dir(staging: Path, env_id: str) -> Path:
    return _venv_dir(staging, env_id) / ("Scripts" if os.name == "nt" else "bin")


def _python_version_of(executable: str) -> str:
    """执行解释器取 ``X.Y.Z``；不可用返回空串。"""
    try:
        completed = subprocess.run(
            [executable, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    version = (completed.stdout or "").strip()
    return version if completed.returncode == 0 else ""


def _normalize_python_spec(spec: str) -> str:
    """把 ``"3.12"`` 归一化为 ``"==3.12"``（前缀匹配）；约束原样返回。"""
    text = str(spec).strip()
    if re.fullmatch(r"\d+\.\d+", text):
        return f"=={text}"
    return text


def _allowed_minors(spec: str) -> list[tuple[int, int]]:
    """枚举满足约束的 ``(major, minor)`` 候选，升序。

    用于"本机该找哪些解释器"以及"需要自愈时该装哪个版本"；
    上界来自约束本身（如 ``<3.14`` ⇒ 最高 3.13），因此**不写死**小版本。
    """
    normalized = _normalize_python_spec(spec)
    results: list[tuple[int, int]] = []
    for major in (3,):
        for minor in range(8, 20):
            ok, _detail = check_python_requires(normalized, (major, minor, 0))
            if ok:
                results.append((major, minor))
    return results


def _uv_managed_python(minor_spec: str) -> str:
    """用 uv **自愈**获取指定 ``X.Y`` 解释器（本地已有则复用，缺失才联网安装）。

    环境问题必须自愈而非让人工装解释器：``uv`` 已是引擎安装链路的既有依赖
    （venv 的 ensurepip 回退路径就在用它），它能落 cpython-<X.Y> 到用户私有目录
    并返回路径。只有 uv 不可用且本地确实没有匹配解释器时才 fail-closed。
    """
    uv = shutil.which("uv")
    if not uv:
        return ""
    for attempt in ("find", "install"):
        if attempt == "install":
            try:
                subprocess.run(
                    [uv, "python", "install", minor_spec],
                    capture_output=True, text=True, check=False, timeout=900,
                )
            except (OSError, subprocess.TimeoutExpired):
                return ""
        try:
            found = subprocess.run(
                [uv, "python", "find", minor_spec],
                capture_output=True, text=True, check=False, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        candidate = (found.stdout or "").strip()
        if found.returncode == 0 and candidate and Path(candidate).is_file():
            version = _python_version_of(candidate)
            if version.startswith(minor_spec + "."):
                return candidate
    return ""


def _interpreter_candidates(spec: str) -> list[tuple[str, str, str]]:
    """列出本机**可用且满足约束**的解释器：``(路径, 版本, 来源)``，按版本降序。"""
    normalized = _normalize_python_spec(spec)
    found: dict[str, tuple[str, str, str]] = {}

    def consider(path: str, source: str) -> None:
        resolved = str(Path(path))
        if resolved in found or not Path(resolved).is_file():
            return
        version = _python_version_of(resolved)
        if not version:
            return
        parts = tuple(int(part) for part in version.split(".") if part.isdigit())
        ok, _detail = check_python_requires(normalized, parts)
        if ok:
            found[resolved] = (resolved, version, source)

    for major, minor in _allowed_minors(spec):
        on_path = shutil.which(f"python{major}.{minor}")
        if on_path:
            consider(on_path, "host")
    pyenv_root = Path(os.environ.get("PYENV_ROOT") or (Path.home() / ".pyenv"))
    versions_dir = pyenv_root / "versions"
    if versions_dir.is_dir():
        for candidate in sorted(versions_dir.glob("3.*")):
            executable = candidate / ("python.exe" if os.name == "nt" else "bin/python")
            if executable.is_file():
                consider(str(executable), "pyenv")
    uv_root = Path.home() / ".local" / "share" / "uv" / "python"
    if uv_root.is_dir():
        for candidate in sorted(uv_root.glob("cpython-3.*")):
            executable = candidate / ("python.exe" if os.name == "nt" else "bin/python3")
            if not executable.is_file():
                executable = candidate / ("python.exe" if os.name == "nt" else "bin/python")
            if executable.is_file():
                consider(str(executable), "uv-managed")

    def sort_key(item: tuple[str, str, str]) -> tuple[int, ...]:
        return tuple(int(part) for part in item[1].split(".") if part.isdigit())

    return sorted(found.values(), key=sort_key, reverse=True)


def _resolve_env_python(env_id: str, spec: str | None) -> tuple[str, str, str]:
    """解析 pypi 环境使用的解释器；返回 ``(可执行路径, 'X.Y.Z', 来源)``。

    ``spec`` 可以是 **约束**（``">=3.12,<3.14"``）或 ``"3.12"``（前缀匹配），
    ``None`` ⇒ 引擎自身解释器。**不写死小版本**：在本机满足约束的解释器里挑
    **最高**的一个（3.13 可用就用 3.13），一个都没有才用 uv 自愈安装
    **约束允许的最高版本**，全部失败才 fail-closed。

    来源 ∈ ``{"host", "pyenv", "uv-managed"}`` 并写入安装报告，便于审计
    "解释器是主机自带的，还是引擎自愈获取的"。
    """
    if spec is None:
        executable = sys.executable or "python3"
        version = "%d.%d.%d" % tuple(sys.version_info[:3])
        return executable, version, "host"
    candidates_fn = _get_mr_attr("_interpreter_candidates", _interpreter_candidates)
    candidates = candidates_fn(spec)
    if candidates:
        return candidates[0]
    allowed_fn = _get_mr_attr("_allowed_minors", _allowed_minors)
    allowed = allowed_fn(spec)
    uv_fn = _get_mr_attr("_uv_managed_python", _uv_managed_python)
    pyver_fn = _get_mr_attr("_python_version_of", _python_version_of)
    for major, minor in reversed(allowed):
        healed = uv_fn(f"{major}.{minor}")
        if healed:
            return healed, pyver_fn(healed), "uv-managed"
    shown = ", ".join(f"{major}.{minor}" for major, minor in allowed) or "（约束无可行解）"
    # 反事实：allowed 为空时绝不能索引 allowed[-1]（会抛 IndexError 而不是可读的
    # McpRuntimeError，等同于绕过 fail-closed 门禁）。
    if allowed:
        hint = f"请执行 `uv python install {allowed[-1][0]}.{allowed[-1][1]}` 后重试"
    else:
        hint = "请检查 python 约束写法（形如 '>=3.12,<3.14'）后重试"
    raise McpRuntimeError(
        f"pypi 环境 {env_id} 的 python 约束 {spec!r} 在本机无可用解释器"
        f"（可行小版本：{shown}），且无法通过 uv 自动获取；{hint}"
    )


def _materialize_venv_links(venv_dir: Path) -> int:
    """把 venv 解释器符号链接替换为**真实副本**（等价 venv ``--copies``）。

    实测：``uv venv`` 会在 ``bin/python*`` 留下指向**树外**解释器的符号链接，
    而 runtime 的 ``build_file_map`` 对树内符号链接 **fail-closed** ⇒ 必须实体化。
    """
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    materialized = 0
    for candidate in sorted(bin_dir.glob("python*")):
        if not candidate.is_symlink():
            continue
        try:
            target = candidate.resolve(strict=True)
        except OSError:
            continue
        if not target.is_file():
            continue
        payload = target.read_bytes()
        candidate.unlink()
        candidate.write_bytes(payload)
        candidate.chmod(0o755)
        materialized += 1
    return materialized


def _create_venv(
    venv_dir: Path, python_bin: str, python_spec: str | None,
) -> None:
    """创建 venv（优先解释器自带 ``-m venv --copies``；ensurepip 坏时回退 uv）。

    实测背景：uv 管理的解释器其 ``ensurepip`` 可能自身损坏（``-m venv`` 直接失败），
    此时用 ``uv venv --python <spec> --seed`` 建 venv 并**实体化解释器链接**。
    两条路径都必须产出**无符号链接**的树（由后续断言兜底）。
    """
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    primary = [python_bin, "-m", "venv", "--copies", str(venv_dir)]
    try:
        created = subprocess.run(
            primary, capture_output=True, text=True, check=False, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        created = None
        primary_error = f"{type(exc).__name__}: {exc}"
    else:
        primary_error = ((created.stderr or "") + (created.stdout or "")).strip()[-400:]
        if created.returncode == 0 and (venv_dir / "pyvenv.cfg").is_file():
            return
        shutil.rmtree(venv_dir, ignore_errors=True)
    uv = shutil.which("uv")
    if not uv:
        raise McpRuntimeError(
            f"创建 venv 失败且未找到 uv 回退：{primary_error}"
        )
    fallback = [
        uv, "venv", "--seed",
        "--python", python_spec or python_bin,
        str(venv_dir),
    ]
    try:
        created = subprocess.run(
            fallback, capture_output=True, text=True, check=False, timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise McpRuntimeError(f"uv venv 超时/失败: {exc}") from exc
    if created.returncode != 0 or not (venv_dir / "pyvenv.cfg").is_file():
        tail = ((created.stderr or "") + (created.stdout or "")).strip()[-400:]
        raise McpRuntimeError(
            f"创建 venv 失败（自带 venv: {primary_error}；uv: {tail}）"
        )
    _materialize_venv_links(venv_dir)


def _install_pypi_environment(
    staging: Path, lock: dict, env_id: str, packages: dict, *, timeout: int,
    python_bin: str | None = None, python_spec: str | None = None,
) -> set[str]:
    """为**单个 pypi 环境**建 venv 并做受限安装（DOC-235），返回可执行相对路径集合。

    安全不变量（与 npm 分支等价，DOC-235 §2.3）：
      * ``--only-binary=:all:`` 只接受 wheel ⇒ **绝不构建 sdist**（无 setup.py /
        PEP-517 build hook 执行）= ``--ignore-scripts`` 的等价物；
      * ``--no-deps`` 不做额外解析，装什么完全由 lock 决定；
      * ``--require-hashes`` 对**每个**包做 sha256 强校验 ≈ npm integrity。

    **强制条件（设计项 3）**：解释器不满足 lock 的 ``python_requires`` 立即阻断。

    **一环境一 venv（v4）**：pypi 是单一扁平依赖环境（npm 才是嵌套解析），
    两个 pypi MCP 的 pin 冲突不可解 ⇒ 必须按 server 隔离 venv（DOC-235 §5.2）。

    **venv 必须用 ``--copies``**（实测）：默认 venv 会创建指向**树外解释器**的符号链接，
    而 runtime 的 ``build_file_map`` 对树内符号链接 **fail-closed**。
    """
    spec = str(lock[PYPI_SECTION]["python_requires"])
    executable = python_bin or sys.executable or "python3"
    actual = (
        tuple(int(part) for part in python_spec.split("."))
        if python_spec else tuple(sys.version_info[:3])
    )
    satisfied, detail = check_python_requires(spec, actual)
    if not satisfied:
        raise McpRuntimeError(f"pypi 环境 {env_id} 安装被拒绝：{detail}")
    requirements = staging / f"{env_id}.{PYPI_REQUIREMENTS_NAME}"
    lines: list[str] = []
    for name, meta in sorted(packages.items()):
        hashes = " ".join(f"--hash={item}" for item in meta["hashes"])
        lines.append(f"{name}=={meta['version']} {hashes}")
    requirements.write_text("\n".join(lines) + "\n", encoding="utf-8")

    python = executable
    venv_dir = _venv_dir(staging, env_id)
    _create_venv(venv_dir, python, python_spec)
    pip = _venv_bin_dir(staging, env_id) / "pip"
    if not pip.is_file() or pip.is_symlink():
        raise McpRuntimeError(f"venv 未提供普通文件形式的 pip，拒绝受限安装: {pip}")
    command = [str(pip), "install", *PYPI_INSTALL_FLAGS, "-r", str(requirements)]
    try:
        installed = subprocess.run(
            command, cwd=str(staging), capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise McpRuntimeError(
            f"pip install 超时（{timeout}s）；已放弃本次安装: {exc}"
        ) from exc
    if installed.returncode != 0:
        tail = ((installed.stderr or "") + (installed.stdout or "")).strip()[-800:]
        raise McpRuntimeError(
            f"pip install 失败（exit {installed.returncode}）：{tail}"
        )

    symlinks = [
        path.relative_to(staging).as_posix()
        for path in sorted(venv_dir.rglob("*")) if path.is_symlink()
    ]
    if symlinks:
        raise McpRuntimeError(
            "pypi 安装树出现符号链接，拒绝发布（完整性模型要求树内无符号链接）: "
            + ", ".join(symlinks[:5])
        )
    executable_rels: set[str] = set()
    bin_dir = _venv_bin_dir(staging, env_id)
    if bin_dir.is_dir():
        for candidate in sorted(bin_dir.iterdir()):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                executable_rels.add(candidate.relative_to(staging).as_posix())
    # 字节码闸门必须在**计算文件树/哈希之前**落地并预编译，
    # 否则首次运行写入的 .pyc 会让 tree_hash 漂移、verify 永久失败。
    _install_bytecode_guard(staging, env_id)
    return executable_rels


def _install_bytecode_guard(staging: Path, env_id: str) -> Path:
    """让 runtime 树对**任何**解释器调用都保持不变（禁止落盘字节码）。

    实测缺陷：runtime 树以 tree_hash 为唯一真值；在 venv 里执行一次
    ``python -c "import semgrep"`` 就会写下 ``site-packages/**/__pycache__/*.pyc``
    ⇒ verify 报"多出的未登记文件"、tree_hash 漂移 ⇒ verify 永久失败，连
    already-installed 自愈路径也被 verify 失败挡住（只能人工删目录）。

    两道闸门，缺一不可：
      1. **预编译整棵 venv**（``compileall``）：venv 的 ``site.py`` 在
         ``sitecustomize`` **之前**就 import ``_virtualenv``，仅靠 sitecustomize
         仍会在启动阶段写入 ``_virtualenv.cpython-3XX.pyc``；基线里先备好所有
         ``.pyc``，这些 import 就无需再写。
      2. **``sitecustomize`` 闸门**：``sys.dont_write_bytecode = True``，覆盖用户/
         第三方在 venv 内新放的模块，以及任何绕过 ``bugx mcp-runtime start``
         （无 ``PYTHONDONTWRITEBYTECODE``）的直接调用。
    """
    venv_dir = staging / VENV_DIRNAME / env_id
    candidates = sorted(venv_dir.glob("lib/python*/site-packages"))
    if not candidates:
        candidates = sorted(venv_dir.glob("Lib/site-packages"))
    if not candidates:
        raise McpRuntimeError(
            f"pypi 环境 {env_id} 缺少 site-packages，无法安装字节码闸门: {venv_dir}"
        )
    guard = candidates[0] / BYTECODE_GUARD_NAME
    guard.write_text(
        '"""BugX MCP runtime: 树内容必须保持不变（tree_hash 校验），禁止落盘字节码。"""\n'
        "import sys\n\n"
        "sys.dont_write_bytecode = True\n",
        encoding="utf-8",
    )
    interpreter = _venv_bin_dir(staging, env_id) / "python"
    for arguments, label in (
        (["-m", "py_compile", str(guard)], "闸门预编译"),
        (["-m", "compileall", "-q", str(venv_dir)], "整棵 venv 预编译"),
    ):
        try:
            completed = subprocess.run(
                [str(interpreter), *arguments],
                capture_output=True, text=True, check=False, timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise McpRuntimeError(
                f"pypi 环境 {env_id} 的{label}失败（首次运行会污染树）: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise McpRuntimeError(
                f"pypi 环境 {env_id} 的{label}失败: "
                + ((completed.stderr or completed.stdout or "").strip()[-300:])
            )
    return guard


def _resolve_pypi_entry(staging: Path, env_id: str, entry_name: str) -> str:
    """把 lock 声明的 console script 名解析为 venv 内相对路径（fail-closed）。"""
    candidate = _venv_bin_dir(staging, env_id) / entry_name
    if candidate.is_symlink() or not candidate.is_file():
        raise McpRuntimeError(
            f"pypi server 的 console script 缺失或非普通文件: "
            f"{VENV_DIRNAME}/{env_id}/bin/{entry_name}"
        )
    if not os.access(candidate, os.X_OK):
        raise McpRuntimeError(
            f"pypi server 的 console script 不可执行: "
            f"{VENV_DIRNAME}/{env_id}/bin/{entry_name}"
        )
    return candidate.relative_to(staging).as_posix()


def _read_pyvenv_version(config_path: Path) -> str:
    """从 venv 的 ``pyvenv.cfg`` 读取解释器版本（``version_info = X.Y.Z``）。"""
    try:
        text = config_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version_info", "version"):
            return value.strip()
    return ""


def _check_venv_shebang(
    runtime_dir: Path, entry_path: Path, entry_rel: str,
    published_dir: Path | None = None,
) -> str | None:
    """校验 pypi 入口的 shebang 解释器存在且位于本 runtime 内；返回问题描述或 None。

    实测缺陷（P0）：pip 写入的 shebang 是**安装期 staging 绝对路径**，
    发布（rename）后该路径消失 ⇒ 入口直接执行报 "No such file or directory"。
    这里把不变量固化为引擎级 fail-closed 校验。
    """
    try:
        with entry_path.open("rb") as handle:
            first_line = handle.readline(4096)
    except OSError as exc:
        return f"入口无法读取: {entry_rel}（{exc}）"
    if not first_line.startswith(b"#!"):
        return f"入口缺少 shebang（pypi console script 必须有）: {entry_rel}"
    interpreter = first_line[2:].strip().decode("utf-8", "replace")
    if not interpreter or " " in interpreter:
        return f"入口 shebang 非法: {entry_rel} -> {interpreter!r}"
    interpreter_path = Path(interpreter)
    if not interpreter_path.is_absolute():
        return f"入口 shebang 必须是绝对路径: {entry_rel} -> {interpreter!r}"
    if any(part.startswith(STAGING_PREFIX) for part in interpreter_path.parts):
        return (
            f"入口 shebang 指向 staging 路径（发布后必然失效）: "
            f"{entry_rel} -> {interpreter}"
        )
    resolved_runtime = runtime_dir.resolve()
    # 安装期自检在 **staging** 上跑，而 shebang 已被改写到**发布后路径**
    # （必须先改，否则 rename 之后入口直接失效）。因此把发布路径映射回
    # staging 再校验——不变量不变（解释器必须存在且位于本 runtime 内），
    # 只是允许目标尚未 rename。
    candidates = [interpreter_path]
    if published_dir is not None:
        prefix = str(published_dir)
        text = str(interpreter_path)
        if text == prefix or text.startswith(prefix + os.sep):
            candidates.append(
                resolved_runtime / os.path.relpath(text, prefix)
            )
    for candidate in candidates:
        try:
            candidate.resolve(strict=True).relative_to(resolved_runtime)
        except (OSError, ValueError):
            continue
        return None
    return (
        f"入口 shebang 解释器缺失或越出本 runtime: "
        f"{entry_rel} -> {interpreter}"
    )


def _rewrite_venv_staging_paths(staging: Path, final_dir: Path, env_id: str) -> int:
    """把 venv 内脚本 shebang 的 **staging 前缀**改写为最终发布路径。

    **实测缺陷（P0）**：pip 生成的 console script 写入的是**安装期绝对路径**
    （``#!<staging>/venv/bin/python3``），而 runtime 发布靠 ``os.rename``
    把 staging 改名 ⇒ 该路径随即消失，入口**直接执行必然报
    ``No such file or directory``**（用 ``venv/bin/python <entry>`` 才碰巧能跑）。
    因此发布前必须把 shebang 改写到最终目录，使入口**自洽可执行**。

    只改 ``venv/bin/*`` 的首行（shebang）与 ``pyvenv.cfg``，不做全树文本替换
    （避免误伤二进制与大规模 I/O）。
    """
    old = _venv_dir(staging, env_id).as_posix().encode()
    new = _venv_dir(final_dir, env_id).as_posix().encode()
    rewritten = 0
    bin_dir = _venv_bin_dir(staging, env_id)
    if bin_dir.is_dir():
        for candidate in sorted(bin_dir.iterdir()):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                raw = candidate.read_bytes()
            except OSError:
                continue
            if not raw.startswith(b"#!") or old not in raw.split(b"\n", 1)[0]:
                continue
            head, separator, rest = raw.partition(b"\n")
            candidate.write_bytes(head.replace(old, new) + separator + rest)
            rewritten += 1
    config = _venv_dir(staging, env_id) / "pyvenv.cfg"
    if config.is_file() and not config.is_symlink():
        try:
            raw = config.read_bytes()
        except OSError:
            raw = b""
        if old in raw:
            config.write_bytes(raw.replace(old, new))
    return rewritten
