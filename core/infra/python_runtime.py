#!/usr/bin/env python3
"""core/python_runtime.py — BugX 跨平台 Python 运行时自适应与 Standalone 兜底引擎。

本模块采用高兼容标准库（兼容 Python 2.7 / 3.6+）编写，严禁在顶层导入任何
Python 3.11 特有模块（如 tomllib），以确保在系统低版本 Python 下能无错启动、
多源勘测并透明自举（os.execv）重拉为系统最高版本 Python。
"""
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile

from .client_metadata import tls_context

MIN_PYTHON_VERSION = (3, 11)
REEXEC_ENV_VAR = "_BUGX_PYTHON_REEXEC_ATTEMPT"


def parse_python_version(executable_path):
    # type: (str) -> tuple[int, ...] | None
    """探测指定 Python 可执行文件的版本元组 (major, minor, micro)。"""
    if not executable_path:
        return None
    try:
        resolved = os.path.expanduser(str(executable_path).strip())
        if not os.path.isfile(resolved) and not shutil.which(resolved):
            return None
        # 兼容全版本 Python 的版本输出脚本
        cmd = [resolved, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode != 0:
            return None
        output = proc.stdout.strip()
        parts = [int(p) for p in output.split(".") if p.isdigit()]
        return tuple(parts) if len(parts) >= 2 else None
    except Exception:
        return None


def get_default_standalone_dir():
    # type: () -> str
    """返回默认的全局内置 Standalone Python 存储路径 ~/.bugx/python。"""
    override = os.environ.get("BUGX_PYTHON_RUNTIME_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    home = os.path.expanduser("~")
    return os.path.join(home, ".bugx", "python")


def find_python_candidates(extra_paths=None):
    # type: (list[str] | None) -> list[str]
    """跨平台多源勘测系统中可能存在的 Python 解释器候选路径列表（保序去重）。"""
    candidates = []
    seen = set()

    def add_candidate(path):
        if not path:
            return
        clean = os.path.expanduser(str(path).strip())
        if clean and clean not in seen:
            seen.add(clean)
            candidates.append(clean)

    # 1. 显式环境变量 BUGX_PYTHON
    if os.environ.get("BUGX_PYTHON"):
        add_candidate(os.environ["BUGX_PYTHON"])

    # 2. 传入的外部优先搜索路径
    if extra_paths:
        for p in extra_paths:
            add_candidate(p)

    # 3. 活跃虚拟环境与 Conda 环境
    for venv_var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        vdir = os.environ.get(venv_var)
        if vdir:
            if sys.platform == "win32":
                add_candidate(os.path.join(vdir, "Scripts", "python.exe"))
                add_candidate(os.path.join(vdir, "python.exe"))
            else:
                add_candidate(os.path.join(vdir, "bin", "python3"))
                add_candidate(os.path.join(vdir, "bin", "python"))

    # 4. 全局内置 Standalone Python 目录 ~/.bugx/python
    st_dir = get_default_standalone_dir()
    if sys.platform == "win32":
        add_candidate(os.path.join(st_dir, "python.exe"))
        add_candidate(os.path.join(st_dir, "python", "python.exe"))
    else:
        add_candidate(os.path.join(st_dir, "bin", "python3"))
        add_candidate(os.path.join(st_dir, "python", "bin", "python3"))

    # 5. 跨平台版本化命令列表 (优先检查高版本)
    version_cmds = [
        "python3.14", "python3.13", "python3.12", "python3.11",
        "python3", "python",
    ]
    for cmd in version_cmds:
        resolved = shutil.which(cmd)
        if resolved:
            add_candidate(resolved)

    # 6. Windows 特有路径探测
    if sys.platform == "win32":
        # py 启动器支持
        py_launcher = shutil.which("py")
        if py_launcher:
            for ver_flag in ("-3.14", "-3.13", "-3.12", "-3.11", "-3"):
                try:
                    res = subprocess.run(
                        [py_launcher, ver_flag, "-c", "import sys; print(sys.executable)"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0, check=False,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        add_candidate(res.stdout.strip())
                except Exception:
                    pass
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base_progs = os.path.join(local_app_data, "Programs", "Python")
            if os.path.isdir(base_progs):
                try:
                    for entry in sorted(os.listdir(base_progs), reverse=True):
                        p_exe = os.path.join(base_progs, entry, "python.exe")
                        if os.path.isfile(p_exe):
                            add_candidate(p_exe)
                except Exception:
                    pass
        for sys_drive in ("C:\\", "D:\\"):
            for v_name in ("Python314", "Python313", "Python312", "Python311"):
                p_exe = os.path.join(sys_drive, v_name, "python.exe")
                if os.path.isfile(p_exe):
                    add_candidate(p_exe)

    # 7. macOS / Linux 典型包管理器与虚拟环境工具目录扫描
    home = os.path.expanduser("~")
    unix_search_patterns = [
        # Homebrew (macOS Apple Silicon & Intel)
        "/opt/homebrew/bin/python3.14",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3.11",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.14",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3.11",
        "/usr/local/bin/python3",
        # pyenv
        os.path.join(home, ".pyenv", "shims", "python3.14"),
        os.path.join(home, ".pyenv", "shims", "python3.13"),
        os.path.join(home, ".pyenv", "shims", "python3.12"),
        os.path.join(home, ".pyenv", "shims", "python3.11"),
        os.path.join(home, ".pyenv", "shims", "python3"),
        # uv python toolchain
        os.path.join(home, ".local", "bin", "python3.14"),
        os.path.join(home, ".local", "bin", "python3.13"),
        os.path.join(home, ".local", "bin", "python3.12"),
        os.path.join(home, ".local", "bin", "python3.11"),
        os.path.join(home, ".local", "bin", "python3"),
        # Linuxbrew
        "/home/linuxbrew/.linuxbrew/bin/python3",
    ]
    for upath in unix_search_patterns:
        if os.path.isfile(upath):
            add_candidate(upath)

    # 动态扫描 uv python 目录: ~/.local/share/uv/python/*/bin/python3
    uv_python_dir = os.path.join(home, ".local", "share", "uv", "python")
    if os.path.isdir(uv_python_dir):
        try:
            for item in sorted(os.listdir(uv_python_dir), reverse=True):
                sub_bin = os.path.join(uv_python_dir, item, "bin", "python3")
                if os.path.isfile(sub_bin):
                    add_candidate(sub_bin)
        except Exception:
            pass

    # 8. 当前执行进程自身的 sys.executable 作为候选之一
    if sys.executable:
        add_candidate(sys.executable)

    return candidates


def find_best_python(min_version=MIN_PYTHON_VERSION, extra_paths=None):
    # type: (tuple[int, int], list[str] | None) -> tuple[str, tuple[int, ...]] | None
    """在系统中搜索所有可用 Python，筛选 >= min_version，并按版本号降序排序返回最优项。

    返回: (最佳可执行文件路径, 版本元组) 或 None。
    """
    candidates = find_python_candidates(extra_paths=extra_paths)
    valid_interpreters = []

    for cand in candidates:
        ver = parse_python_version(cand)
        if ver and ver >= min_version:
            # 标准化路径
            try:
                real_cand = os.path.realpath(cand)
            except Exception:
                real_cand = cand
            valid_interpreters.append((ver, real_cand))

    if not valid_interpreters:
        return None

    # 按版本降序排序 (例如 3.14.6 > 3.11.11)
    valid_interpreters.sort(key=lambda item: item[0], reverse=True)
    best_ver, best_path = valid_interpreters[0]
    return (best_path, best_ver)


def _sanitize_reexec_env(python_path):
    # type: (str) -> dict[str, str]
    """构建安全净化的自举重拉子进程环境，移除跨版本污染变量，保留系统关键变量。"""
    env = os.environ.copy()
    # Windows 环境变量大小写不敏感，统一排查小写匹配
    polluting_keys = {
        "pythonpath", "pythonhome", "pythonstartup",
        "__pyvenv_launcher__", "pythonnoenter",
    }
    keys_to_remove = [k for k in env if k.lower() in polluting_keys]
    for k in keys_to_remove:
        env.pop(k, None)
    env["BUGX_PYTHON"] = python_path
    env[REEXEC_ENV_VAR] = "1"
    return env


def transparent_reexec_if_needed(min_version=MIN_PYTHON_VERSION, script_path=None):
    # type: (tuple[int, int], str | None) -> None
    """若当前解释器低于 min_version，则自动搜寻系统最新 Python 并透明原地自举重拉进程。

    安全红线：通过 REEXEC_ENV_VAR 环境变量防止递归重拉死循环。
    """
    if sys.version_info >= min_version:
        return

    # 防死循环检查
    if os.environ.get(REEXEC_ENV_VAR):
        actual_str = ".".join(map(str, sys.version_info[:3]))
        req_str = ".".join(map(str, min_version))
        sys.stderr.write(
            f"[BugX] 错误：自举重拉后解释器仍然不兼容 (当前 Python {actual_str} < {req_str})。\n"
            f"请检查 BUGX_PYTHON 环境变量是否指向了正确的 Python >= {req_str} 解释器。\n"
        )
        sys.exit(2)

    best = find_best_python(min_version=min_version)
    if best is not None:
        best_path, best_ver = best
        original_python = os.environ.get("BUGX_PYTHON")
        original_reexec = os.environ.get(REEXEC_ENV_VAR)
        os.environ["BUGX_PYTHON"] = best_path
        os.environ[REEXEC_ENV_VAR] = "1"

        try:
            # 构建新的命令行参数：若通过 -m 模块方式启动，保留 -m 形式
            main_mod = sys.modules.get("__main__")
            main_spec = getattr(main_mod, "__spec__", None) if main_mod else None
            if main_spec is not None and getattr(main_spec, "name", None):
                new_argv = [best_path, "-m", main_spec.name] + sys.argv[1:]
            else:
                target_script = script_path or sys.argv[0]
                new_argv = [best_path, target_script] + sys.argv[1:]

            if sys.platform == "win32":
                # Windows 上 os.execv 产生子进程并退出当前进程
                clean_env = _sanitize_reexec_env(best_path)
                try:
                    os.execv(best_path, new_argv)
                    return
                except OSError:
                    ret = subprocess.call(new_argv, env=clean_env)
                    sys.exit(ret)
            else:
                os.execv(best_path, new_argv)
                return
        except Exception as exc:
            sys.stderr.write(f"[BugX] 自举重拉启动失败 ({best_path}): {exc}\n")
            sys.exit(2)
        finally:
            if original_python is None:
                os.environ.pop("BUGX_PYTHON", None)
            else:
                os.environ["BUGX_PYTHON"] = original_python
            if original_reexec is None:
                os.environ.pop(REEXEC_ENV_VAR, None)
            else:
                os.environ[REEXEC_ENV_VAR] = original_reexec

    # 若全系统均无可用的 Python >= min_version，输出友好诊断
    actual_str = ".".join(map(str, sys.version_info[:3]))
    req_str = ".".join(map(str, min_version))
    sys.stderr.write(
        f"[BugX] 当前 Python {actual_str} 不受支持；需要 Python >= {req_str}。\n"
        f"[BugX] 全系统勘测完成，未找到符合条件的 Python >= {req_str} 解释器。\n"
        f"[BugX] 建议解决方案：\n"
        f"  1. 安装 Python {req_str}+ (macOS: 'brew install python'；Linux: 'apt/dnf install python3.11')；\n"
        f"  2. 或设置环境变量 BUGX_PYTHON 指向受支持的 Python 路径；\n"
        # 路径与执行方式都必须可用：该模块顶层有 ``from .client_metadata import ...``，
        # 直接 ``python core/infra/python_runtime.py`` 会因 ``__package__`` 为空
        # ImportError，只能以模块方式执行；旧的扁平路径也已不存在。
        f"  3. 或安装/引导 BugX 独立 Standalone 便携运行时：python3 -m core.infra.python_runtime install-standalone\n"
    )
    sys.exit(2)


def get_platform_target_key():
    # type: () -> str | None
    """获取当前平台的 Standalone CPython key (例如 darwin-arm64, linux-x64, win32-x64)。"""
    plat = sys.platform
    machine = platform.machine().lower()

    if plat == "darwin":
        return "darwin-arm64" if machine in ("arm64", "aarch64") else "darwin-x64"
    elif plat.startswith("linux"):
        return "linux-arm64" if machine in ("arm64", "aarch64") else "linux-x64"
    elif plat == "win32":
        return "win32-x64"
    return None


def get_runtime_lock_data():
    # type: () -> dict | None
    """读取 scripts/runtime.lock.json 中的 Standalone Python 清单。"""
    infra_dir = os.path.dirname(os.path.abspath(__file__))
    core_dir = os.path.dirname(infra_dir)
    repo_root = os.path.dirname(core_dir)
    lock_path = os.path.join(repo_root, "scripts", "runtime.lock.json")
    if not os.path.isfile(lock_path):
        # 兼容打包发布环境（lock 文件可能随 metadata 一同分发）
        alt_path = os.path.join(core_dir, "runtime.lock.json")
        if os.path.isfile(alt_path):
            lock_path = alt_path
        else:
            return None
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def ensure_standalone_python(dest_dir=None, force=False):
    # type: (str | None, bool) -> str
    """引导并安装当前系统对应的 Standalone 独立便携 CPython。

    返回安装就绪的 Python 可执行文件绝对路径。
    """
    target_key = get_platform_target_key()
    if not target_key:
        raise RuntimeError(f"不支持自动安装 Standalone Python 的平台架构: {sys.platform}-{platform.machine()}")

    lock_data = get_runtime_lock_data()
    if not lock_data or "targets" not in lock_data or target_key not in lock_data["targets"]:
        raise RuntimeError(f"未在 runtime.lock.json 中找到平台 {target_key} 的配置数据")

    target_cfg = lock_data["targets"][target_key]
    url = target_cfg["url"]
    expected_sha256 = target_cfg["sha256"]
    bin_rel = target_cfg["bin"]

    dest_dir = dest_dir or get_default_standalone_dir()
    os.makedirs(dest_dir, exist_ok=True)

    expected_bin = os.path.join(dest_dir, bin_rel)
    manifest_path = os.path.join(dest_dir, "python-manifest.json")

    if not force and os.path.isfile(expected_bin) and os.path.isfile(manifest_path):
        ver = parse_python_version(expected_bin)
        if ver and ver >= MIN_PYTHON_VERSION:
            return expected_bin

    # 执行下载并解压
    import urllib.request
    tmp_archive = os.path.join(dest_dir, f".download-{os.getpid()}.tar.gz")
    try:
        sys.stderr.write(f"[BugX] 正在下载内置便携 Python 3.11 ({target_key})...\n")
        sys.stderr.write(f"[BugX] 资源来源: {url}\n")
        req = urllib.request.Request(url, headers={"User-Agent": "BugX-Python-Runtime-Bootstrap"})
        hasher = hashlib.sha256()

        with urllib.request.urlopen(req, timeout=60, context=tls_context()) as response, open(tmp_archive, "wb") as out_file:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
                out_file.write(chunk)

        digest = hasher.hexdigest()
        if digest != expected_sha256:
            raise RuntimeError(f"下载文件 SHA256 校验失败：期望 {expected_sha256}，实测 {digest}")

        # 安全解压
        sys.stderr.write("[BugX] 校验通过，正在安全解压便携运行时...\n")
        with tarfile.open(tmp_archive, "r:gz") as tar:
            # 严格防止路径穿越与链接成员逃逸
            import pathlib
            base_resolved = pathlib.Path(dest_dir).resolve()
            members = tar.getmembers()
            for member in members:
                # 设备/FIFO 等特殊成员一律拒绝
                if member.isdev():
                    raise RuntimeError(f"检测到非法压缩包设备成员，已拒绝解压: {member.name}")
                dest_path = (base_resolved / member.name).resolve()
                try:
                    # 真实路径边界判断：startswith 会误放行 ../python-evil 这类同级目录
                    dest_path.relative_to(base_resolved)
                except ValueError:
                    raise RuntimeError(f"检测到非法压缩包成员路径逃逸: {member.name}")
                if member.issym() or member.islnk():
                    # 官方归档含内部相对软链（如 python/bin/python3 -> python3.11），
                    # 故只拒绝指向目标目录之外的链接，避免误伤正常安装。
                    if os.path.isabs(member.linkname):
                        raise RuntimeError(f"检测到非法压缩包链接成员逃逸: {member.name}")
                    link_base = base_resolved if member.islnk() else dest_path.parent
                    try:
                        (link_base / member.linkname).resolve().relative_to(base_resolved)
                    except ValueError:
                        raise RuntimeError(f"检测到非法压缩包链接成员逃逸: {member.name}")
            # 显式指定解压过滤器：Python 3.12+ 支持 filter="data"（拒绝链接/设备/绝对路径成员）。
            # 更老的解释器没有该参数，回退为逐成员手动解压；成员已在上方完成边界与链接校验。
            if sys.version_info >= (3, 12):
                tar.extractall(dest_dir, filter="data")
            else:
                for member in members:
                    tar.extract(member, dest_dir)

        if os.path.isfile(tmp_archive):
            os.remove(tmp_archive)

        if not os.path.isfile(expected_bin):
            # 尝试查找解压出的可执行文件
            for root_p, _, files in os.walk(dest_dir):
                for f in files:
                    if f in ("python3", "python.exe") and not f.startswith("."):
                        cand = os.path.join(root_p, f)
                        if os.path.isfile(cand):
                            expected_bin = cand
                            break

        # 确保可执行权限
        try:
            os.chmod(expected_bin, 0o755)
        except Exception:
            pass

        manifest_content = {
            "type": "standalone",
            "platform_key": target_key,
            "cpython_version": lock_data.get("cpython", "3.11.11"),
            "bin_path": expected_bin,
            "sha256": digest,
        }
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest_content, mf, indent=2, ensure_ascii=False)

        sys.stderr.write(f"[BugX] ✓ 内置便携 Python 就绪: {expected_bin}\n")
        return expected_bin
    except Exception:
        if os.path.isfile(tmp_archive):
            try:
                os.remove(tmp_archive)
            except Exception:
                pass
        raise


def main():
    """CLI 入口，支持独立诊断或管理内置 Python。"""
    import argparse
    parser = argparse.ArgumentParser(prog="python_runtime", description="BugX Python 运行时勘测与管理工具")
    subparsers = parser.add_subparsers(dest="action", required=True)

    detect_p = subparsers.add_parser("detect", help="全系统勘测可用 Python 并输出最优项")
    detect_p.add_argument("--json", action="store_true", help="以 JSON 格式输出勘测详情")

    subparsers.add_parser("install-standalone", help="为当前系统引导并安装内置便携 Standalone Python")

    args = parser.parse_args()

    if args.action == "detect":
        candidates = find_python_candidates()
        details = []
        for c in candidates:
            ver = parse_python_version(c)
            details.append({
                "path": c,
                "version": ".".join(map(str, ver)) if ver else None,
                "compatible": (ver >= MIN_PYTHON_VERSION) if ver else False,
            })
        best = find_best_python()
        result = {
            "system_executable": sys.executable,
            "system_version": ".".join(map(str, sys.version_info[:3])),
            "best_python": best[0] if best else None,
            "best_version": ".".join(map(str, best[1])) if best else None,
            "candidates": details,
        }
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"当前 Python: {result['system_executable']} ({result['system_version']})")
            if best:
                print(f"系统推荐最优 Python: {best[0]} ({result['best_version']})")
            else:
                print("未在系统中检测到符合 >= 3.11 的 Python 解释器。")
            print(f"已扫描候选数量: {len(candidates)}")
    elif args.action == "install-standalone":
        installed = ensure_standalone_python()
        print(f"[OK] 内置便携 Python 安装就绪: {installed}")


if __name__ == "__main__":
    main()
