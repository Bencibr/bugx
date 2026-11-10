#!/usr/bin/env python3
"""执行声明式平台测试适配器，并生成可追溯的命令与证据记录。"""
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
import sys
from pathlib import Path


import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
import platform as platform_module

SCHEMA_VERSION = 1
PLATFORMS = {"desktop", "android", "ios", "memory"}
HOST_PLATFORMS = {"macos", "windows", "linux"}
TARGET_KINDS = {"host", "device", "simulator", "process", "source"}
SAFE_ENV = ("PATH", "HOME", "TMPDIR", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")
DEFAULT_OUTPUT_LIMIT = 5 * 1024 * 1024
MAX_OUTPUT_LIMIT = 50 * 1024 * 1024


class AdapterError(ValueError):
    """适配器清单或运行协议错误。"""


def parse_adb_devices(output: str) -> list[str]:
    """从 adb devices 输出中提取处于 device 状态的设备序列号列表。"""
    devices: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def parse_booted_simulators(output: str) -> list[str]:
    """从 xcrun simctl list --json devices 输出中提取已启动可用的模拟器 UDID 列表。"""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    booted: list[str] = []
    for devices in payload.get("devices", {}).values():
        for device in devices:
            if isinstance(device, dict) and device.get("state") == "Booted" and device.get("isAvailable", True):
                val = str(device.get("udid") or device.get("name") or "")
                if val:
                    booted.append(val)
    return booted




def _relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AdapterError(f"{field} 必须是非空相对路径")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise AdapterError(f"{field} 不得是绝对路径或越过根目录")
    return value


def _strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AdapterError(f"{field} 必须是非空字符串数组")
    if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise AdapterError(f"{field} 必须是非空字符串数组")
    return value


def validate_manifest(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise AdapterError("manifest 根节点必须是对象")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise AdapterError(f"仅支持 schema_version={SCHEMA_VERSION}")
    adapter_id = payload.get("id")
    if not isinstance(adapter_id, str) or not NAME_RE.fullmatch(adapter_id):
        raise AdapterError("manifest.id 格式无效")
    if payload.get("platform") not in PLATFORMS:
        raise AdapterError(f"manifest.platform 必须是 {sorted(PLATFORMS)} 之一")
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        raise AdapterError("manifest.execution 必须是对象")
    host_platforms = execution.get("host_platforms")
    if not isinstance(host_platforms, list) or not host_platforms or any(
        item not in HOST_PLATFORMS for item in host_platforms
    ):
        raise AdapterError(
            f"execution.host_platforms 必须是 {sorted(HOST_PLATFORMS)} 的非空子集"
        )
    if len(set(host_platforms)) != len(host_platforms):
        raise AdapterError("execution.host_platforms 不得重复")
    target_platform = execution.get("target_platform")
    if not _nonempty_string(target_platform):
        raise AdapterError("execution.target_platform 必须是非空字符串")
    target_kind = execution.get("target_kind")
    if target_kind not in TARGET_KINDS:
        raise AdapterError(f"execution.target_kind 必须是 {sorted(TARGET_KINDS)} 之一")
    fingerprint_variable = execution.get("target_fingerprint_variable")
    if fingerprint_variable is not None and (
        not isinstance(fingerprint_variable, str)
        or not NAME_RE.fullmatch(fingerprint_variable)
    ):
        raise AdapterError("execution.target_fingerprint_variable 格式无效")
    variables = payload.get("variables", {})
    if not isinstance(variables, dict):
        raise AdapterError("manifest.variables 必须是对象")
    for name, spec in variables.items():
        if not NAME_RE.fullmatch(str(name)) or not isinstance(spec, dict):
            raise AdapterError("变量名称或定义无效")
        if "default" in spec and not isinstance(spec["default"], str):
            raise AdapterError(f"变量 {name}.default 必须是字符串")
        if "required" in spec and not isinstance(spec["required"], bool):
            raise AdapterError(f"变量 {name}.required 必须是布尔值")
    if fingerprint_variable is not None:
        spec = variables.get(fingerprint_variable)
        if not isinstance(spec, dict) or spec.get("required") is not True:
            raise AdapterError(
                "target_fingerprint_variable 必须引用 required=true 的变量"
            )
    if target_kind in {"device", "simulator", "process"} and fingerprint_variable is None:
        raise AdapterError(f"target_kind={target_kind} 必须声明目标指纹变量")
    if target_kind in {"host", "source"} and fingerprint_variable is not None:
        raise AdapterError(f"target_kind={target_kind} 不应声明目标指纹变量")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise AdapterError("manifest.steps 必须是非空数组")
    seen: set[str] = set()
    allowed_placeholders = {"root", "artifact_dir", *variables}
    for index, step in enumerate(steps):
        field = f"steps[{index}]"
        if not isinstance(step, dict):
            raise AdapterError(f"{field} 必须是对象")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not NAME_RE.fullmatch(step_id):
            raise AdapterError(f"{field}.id 格式无效")
        if step_id in seen:
            raise AdapterError(f"重复 step id: {step_id}")
        seen.add(step_id)
        command = _strings(step.get("command"), f"{field}.command")
        timeout = step.get("timeout_seconds", 60)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
                or not 0 < timeout <= 3600:
            raise AdapterError(f"{field}.timeout_seconds 必须在 (0, 3600] 内")
        output_limit = step.get("max_output_bytes", DEFAULT_OUTPUT_LIMIT)
        if not isinstance(output_limit, int) or isinstance(output_limit, bool) \
                or not 1024 <= output_limit <= MAX_OUTPUT_LIMIT:
            raise AdapterError(
                f"{field}.max_output_bytes 必须在 1024..{MAX_OUTPUT_LIMIT}"
            )
        if "cwd" in step:
            _relative_path(step["cwd"], f"{field}.cwd")
        env = step.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not NAME_RE.fullmatch(key)
            or not isinstance(value, str) or "\x00" in value
            for key, value in env.items()
        ):
            raise AdapterError(f"{field}.env 必须是字符串映射")
        if "capture_stdout" in step:
            _relative_path(step["capture_stdout"], f"{field}.capture_stdout")
        for evidence in step.get("evidence_paths", []):
            _relative_path(evidence, f"{field}.evidence_paths")
        if not isinstance(step.get("evidence_paths", []), list):
            raise AdapterError(f"{field}.evidence_paths 必须是数组")
        texts = [*command, *env.values()]
        unknown = {
            name for text in texts for name in PLACEHOLDER_RE.findall(text)
            if name not in allowed_placeholders
        }
        if unknown:
            raise AdapterError(f"{field} 使用未声明变量: {sorted(unknown)}")
    return payload


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def actual_host_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _host_fingerprint(host_platform: str) -> str:
    identity = "|".join((
        host_platform,
        platform_module.node(),
        platform_module.machine(),
        platform_module.release(),
    )).encode("utf-8", errors="replace")
    return "sha256:" + hashlib.sha256(identity).hexdigest()


def _execution_context(manifest: dict, values: dict[str, str]) -> dict:
    execution = manifest["execution"]
    actual_host = actual_host_platform()
    target_kind = execution["target_kind"]
    expected_hosts = execution["host_platforms"]
    reasons: list[str] = []
    if actual_host not in expected_hosts:
        reasons.append(
            f"实际宿主 {actual_host} 不在允许宿主 {expected_hosts} 中"
        )
    fingerprint_variable = execution.get("target_fingerprint_variable")
    if target_kind == "host":
        target_fingerprint = _host_fingerprint(actual_host)
    elif target_kind == "source":
        target_fingerprint = "repository-source"
    else:
        target_fingerprint = values.get(fingerprint_variable or "")
        if not _nonempty_string(target_fingerprint):
            reasons.append("目标指纹缺失")
        elif target_kind == "simulator" and target_fingerprint.strip().lower() == "booted":
            reasons.append("booted 是易变别名，不是可追溯的 Simulator UDID")
    target_platform = execution["target_platform"]
    if target_kind == "host" and target_platform != actual_host:
        reasons.append(
            f"目标平台 {target_platform} 与实际宿主 {actual_host} 不一致"
        )
    return {
        "actual_host_platform": actual_host,
        "allowed_host_platforms": expected_hosts,
        "target_platform": target_platform,
        "target_kind": target_kind,
        "target_fingerprint": target_fingerprint,
        "target_executed": not reasons,
        "verification_level": (
            "source-only" if target_kind == "source"
            else "native-target" if not reasons
            else "not-executed"
        ),
        "blocked_reasons": reasons,
    }


def load_manifest(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"无法读取 manifest: {exc}") from exc
    return validate_manifest(payload)


def _values(manifest: dict, root: Path, artifact_dir: Path,
            supplied: dict[str, str] | None) -> dict[str, str]:
    supplied = supplied or {}
    unknown = set(supplied) - set(manifest.get("variables", {}))
    if unknown:
        raise AdapterError(f"传入了未声明变量: {sorted(unknown)}")
    values = {"root": str(root), "artifact_dir": str(artifact_dir)}
    for name, spec in manifest.get("variables", {}).items():
        value = supplied.get(name, spec.get("default"))
        if value is None and spec.get("required", False):
            raise AdapterError(f"缺少必需变量: {name}")
        if value is not None:
            values[name] = value
    return values


def _render(text: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise AdapterError(f"变量没有值: {name}")
        return values[name]
    return PLACEHOLDER_RE.sub(replace, text)


def _digest(data: bytes) -> dict:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _digest_file(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _artifact_file(artifact_dir: Path, relative: str) -> Path:
    """Return an artifact path without following pre-existing symlinks."""
    _relative_path(relative, "artifact path")
    candidate = artifact_dir / relative
    current = artifact_dir
    for part in Path(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise AdapterError(f"artifact 路径包含符号链接: {relative}")
    if candidate.is_symlink():
        raise AdapterError(f"artifact 路径是符号链接: {relative}")
    try:
        candidate.resolve(strict=False).relative_to(artifact_dir)
    except ValueError as exc:
        raise AdapterError(f"artifact 路径越过 artifact_dir: {relative}") from exc
    return candidate


def _write_artifact(artifact_dir: Path, relative: str, data: bytes) -> None:
    path = _artifact_file(artifact_dir, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | nofollow, 0o600)
    except OSError as exc:
        raise AdapterError(f"无法安全写入 artifact: {relative}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _evidence(path: Path, artifact_dir: Path) -> dict:
    relative_path = path.relative_to(artifact_dir)
    resolved_path = _artifact_file(artifact_dir, relative_path.as_posix())
    resolved = resolved_path.resolve()
    try:
        relative = resolved.relative_to(artifact_dir)
    except ValueError as exc:
        raise AdapterError(f"证据路径越过 artifact_dir: {path}") from exc
    if not resolved.is_file():
        return {"path": relative.as_posix(), "exists": False}
    return {"path": relative.as_posix(), "exists": True, **_digest_file(resolved)}


class _OutputCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.digest = hashlib.sha256()
        self.prefix = bytearray()
        self.total = 0

    def collect(self, stream: object) -> None:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            self.total += len(chunk)
            self.digest.update(chunk)
            if len(self.prefix) < self.limit:
                self.prefix.extend(chunk[: self.limit - len(self.prefix)])

    def result(self) -> tuple[bytes, dict]:
        return bytes(self.prefix), {
            "bytes": self.total,
            "sha256": self.digest.hexdigest(),
            "truncated": self.total > self.limit,
        }


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            process.send_signal(getattr(subprocess, "CTRL_BREAK_EVENT", 1))
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass


def _run_bounded(command: list[str], cwd: Path, env: dict[str, str], timeout: float,
                 output_limit: int) -> tuple[int, bool, bytes, dict, bytes, dict]:
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt", creationflags=creationflags,
            close_fds=os.name != "nt",
        )
    except OSError as exc:
        data = str(exc).encode("utf-8", errors="replace")
        empty = {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(), "truncated": False}
        return 127, False, b"", empty, data, {**_digest(data), "truncated": False}
    stdout_collector = _OutputCollector(output_limit)
    stderr_collector = _OutputCollector(output_limit)
    readers = [
        threading.Thread(target=stdout_collector.collect, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr_collector.collect, args=(process.stderr,), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
    finally:
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    stdout, stdout_meta = stdout_collector.result()
    stderr, stderr_meta = stderr_collector.result()
    return 124 if timed_out else (process.returncode or 0), timed_out, stdout, stdout_meta, stderr, stderr_meta


def run_adapter(manifest: dict, root: Path, artifact_dir: Path,
                supplied: dict[str, str] | None = None) -> dict:
    manifest = validate_manifest(manifest)
    root = root.resolve()
    if artifact_dir.exists() and artifact_dir.is_symlink():
        raise AdapterError("artifact_dir 不得是符号链接")
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    values = _values(manifest, root, artifact_dir, supplied)
    started = time.time()
    execution = _execution_context(manifest, values)
    if execution["blocked_reasons"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "adapter_id": manifest["id"],
            "platform": manifest["platform"],
            "started_epoch": started,
            "duration_seconds": round(time.time() - started, 3),
            "status": "BLOCKED",
            "execution": execution,
            "steps": [],
        }
    records = []
    overall_ok = True
    for step in manifest["steps"]:
        command = [_render(arg, values) for arg in step["command"]]
        cwd = (root / step.get("cwd", ".")).resolve()
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise AdapterError(f"step {step['id']} cwd 越过项目根目录") from exc
        env = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
        env.update({key: _render(value, values) for key, value in step.get("env", {}).items()})
        step_started = time.monotonic()
        output_limit = int(step.get("max_output_bytes", DEFAULT_OUTPUT_LIMIT))
        (exit_code, timed_out, stdout, stdout_record, stderr,
         stderr_record) = _run_bounded(
            command, cwd, env, float(step.get("timeout_seconds", 60)), output_limit,
        )
        duration = round(time.monotonic() - step_started, 3)
        if step.get("capture_stdout"):
            _write_artifact(artifact_dir, step["capture_stdout"], stdout)
            stdout_record["capture_path"] = step["capture_stdout"]
        elif stdout:
            log_name = f"{step['id']}.stdout.log"
            _write_artifact(artifact_dir, log_name, stdout)
            stdout_record["capture_path"] = log_name
        if stderr:
            log_name = f"{step['id']}.stderr.log"
            _write_artifact(artifact_dir, log_name, stderr)
            stderr_record["capture_path"] = log_name
        evidence = [_evidence(artifact_dir / path, artifact_dir)
                    for path in step.get("evidence_paths", [])]
        required_evidence_ok = all(item["exists"] for item in evidence)
        ok = exit_code == 0 and required_evidence_ok
        allowed_failure = bool(step.get("allow_failure", False))
        overall_ok = overall_ok and (ok or allowed_failure)
        records.append({
            "id": step["id"], "command": command, "cwd": str(cwd),
            "exit_code": exit_code, "timed_out": timed_out,
            "duration_seconds": duration, "ok": ok,
            "allow_failure": allowed_failure, "stdout": stdout_record,
            "stderr": stderr_record, "evidence": evidence,
            "output_limit_bytes": output_limit,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_id": manifest["id"],
        "platform": manifest["platform"],
        "started_epoch": started,
        "duration_seconds": round(time.time() - started, 3),
        "status": "PASS" if overall_ok else "FAIL",
        "execution": execution,
        "steps": records,
    }


def _parse_variables(items: list[str]) -> dict[str, str]:
    values = {}
    for item in items:
        if "=" not in item:
            raise AdapterError(f"--var 必须是 NAME=VALUE: {item}")
        name, value = item.split("=", 1)
        if not NAME_RE.fullmatch(name):
            raise AdapterError(f"变量名无效: {name}")
        values[name] = value
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "run"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifacts", type=Path, default=Path("bugx-artifacts"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--var", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "validate":
            print(f"[platform_adapter] ✓ {manifest['id']}")
            return 0
        report = run_adapter(manifest, args.root, args.artifacts, _parse_variables(args.var))
        content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            sys.stdout.write(content)
        return 0 if report["status"] == "PASS" else 1
    except (AdapterError, OSError) as exc:
        print(f"[platform_adapter] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
