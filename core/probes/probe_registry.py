#!/usr/bin/env python3
"""BugX 探针插件注册中心（Probe Registry）。

统一维护全系统探针元数据、自检协议与调用边界，支持动态探针注册与自适应分发。
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

import core.secure_atomic as secure_atomic


PROBE_TIMEOUT_BUDGET: dict[str, float] = {
    "sast": 30.0,
    # codebase-memory starts a Go CLI, loads sqlite and may scan the
    # repository before answering. Measured cold start on this repo
    # (nodes=44133, edges=190210, index 170MB) is 18~24s, so the old 15s floor
    # degraded a healthy accelerator on every first probe. 45s keeps the same
    # ~1.9x margin the platform probe uses over its 11~17s cold start, and the
    # probe adds one bounded retry for the slow-tail case.
    "codebase-memory": 45.0,
    # platform 探针逐个枚举宿主平台能力并拉起子命令，冷启动实测 11~17s；
    # memory 探针要加载索引与内存画像，实测约 5.7s。二者若沿用 default 5.0s，
    # 在无历史耗时的首次启动必然假超时（下一次凭 previous_duration 把预算抬到
    # 12s 才通过），使同一环境在两次 pre 之间出现「这次要处置、下次又健康」的
    # 结果翻转。这里给出显式预算，消除该非确定性。
    "platform": 30.0,
    "memory": 20.0,
    "default": 5.0,
}
PROBE_TIMEOUT_MAX_SECONDS = 120.0


def normalized_tool_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a child environment that can see common user tool locations."""
    environment = dict(os.environ if environ is None else environ)
    entries = [item for item in environment.get("PATH", "").split(os.pathsep) if item]
    candidates = [environment.get("BUGX_TOOL_PATH", "")]
    if os.name != "nt":
        candidates.extend((
            "/opt/homebrew/bin", "/usr/local/bin",
            "/home/linuxbrew/.linuxbrew/bin",
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".cargo" / "bin"),
        ))
    for candidate in candidates:
        if not candidate or candidate in entries:
            continue
        try:
            if Path(candidate).is_dir():
                entries.append(candidate)
        except OSError:
            continue
    environment["PATH"] = os.pathsep.join(entries)
    return environment


def _positive_timeout(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def probe_timeout_budget(name: str, previous_duration: float | None = None) -> float:
    """Choose a bounded timeout from a cold-start floor and prior telemetry."""
    normalized = name.upper().replace("-", "_")
    override = (
        os.environ.get(f"BUGX_PROBE_TIMEOUT_{normalized}")
        or os.environ.get("BUGX_PROBE_TIMEOUT_SECONDS")
    )
    budget = _positive_timeout(override)
    if budget is None:
        budget = PROBE_TIMEOUT_BUDGET.get(name, PROBE_TIMEOUT_BUDGET["default"])
        if previous_duration and previous_duration > 0:
            budget = max(budget, previous_duration * 2.0 + 2.0)
    minimum = 5.0 if name == "codebase-memory" else 2.0
    return min(PROBE_TIMEOUT_MAX_SECONDS, max(minimum, budget))


@dataclass(frozen=True)
class ProbeDescriptor:
    """探针插件元数据描述符。"""

    name: str
    script: str
    domain: str
    accepts_root: bool = True
    description: str = ""
    priority: int = 100
    lifecycle: str = "preflight"  # "preflight" (启动前预检) | "runtime" (运行期专项执行)
    # DES-13：``required=True`` 表示脚本缺失时启动必须硬阻断；
    # 建议性探针（如 openwiki）设为 False，缺失即静默跳过。
    required: bool = True
    # DOC-236 C1：``critical=True`` 表示缺失/不可用必须硬阻断且**不得**被
    # ``tooling_exemptions`` 豁免。只有"引擎自身没有等价回退"的能力才配 True；
    # 带内置回退的加速器（如 codebase-memory → symbol_index/project_graph）
    # 必须为 False，仍可经项目配置显式豁免并留痕。
    critical: bool = True
    # 只有显式声明 host 的探针才允许跨项目复用；新探针缺省绑定项目。
    cache_scope: str = "project"
    # 运行期探针只有显式声明不执行目标工作的就绪动作，才能被 Skill Pack
    # 声明为 required_runtime.tools。参数只作为 argv 传递，不经过 shell。
    readiness_args: tuple[str, ...] | None = None


# 内置探针静态定义清单
_BUILTIN_PROBES: tuple[ProbeDescriptor, ...] = (
    ProbeDescriptor(
        name="efficiency",
        script="efficiency_probe.py",
        domain="performance",
        accepts_root=True,
        description="系统效率基线与环境计算性能评测探针",
        priority=10,
        lifecycle="preflight",
        # check 只读查询可选 CLI 的 PATH/版本，不扫描项目源码。
        readiness_args=("check",),
    ),
    ProbeDescriptor(
        name="sast",
        script="sast_probe.py",
        domain="security",
        accepts_root=True,
        description="多引擎静态代码安全扫描与规则违规探针",
        priority=20,
        lifecycle="preflight",
        # check 只核对扫描器二进制与版本；scan(root) 才读取目标源码。
        readiness_args=("check",),
        # inspect() 只检查扫描器二进制与版本；扫描目标由后续 scan(root) 执行。
        cache_scope="host",
        # 外部扫描器（semgrep/codeql/trivy/bandit）为加速器，引擎自带 universal_code_matcher
        # 与 sast_rules 内置 AST 回退，可经 tooling_exemptions 豁免并留痕。
        critical=False,
    ),
    ProbeDescriptor(
        name="codebase-memory",
        script="codebase_memory_probe.py",
        domain="knowledge",
        accepts_root=True,
        description="Codebase Memory MCP 图谱语义与知识索引探针",
        priority=30,
        lifecycle="preflight",
        # 加速器而非硬依赖：引擎自带 symbol_index/project_graph 回退
        # （``BUGX_USE_CODEBASE_MEMORY=0`` 是受支持运行模式），因此可经
        # 项目 ``tooling_exemptions`` 显式豁免；critical 能力不得豁免。
        critical=False,
    ),
    ProbeDescriptor(
        name="pattern",
        script="pattern_probe.py",
        domain="security",
        accepts_root=True,
        description="高频历史缺陷模式与已知陷阱特征扫描探针",
        priority=40,
        lifecycle="runtime",
        readiness_args=("check",),
    ),
    ProbeDescriptor(
        name="platform",
        script="platform_probe.py",
        domain="system",
        accepts_root=True,
        description="多平台运行时与工具链依赖适配探针",
        priority=50,
        lifecycle="preflight",
        # 原始平台报告会在当前 profile 下重新筛选目标与形态。
        cache_scope="host",
    ),
    ProbeDescriptor(
        name="mobile",
        script="mobile_probe.py",
        domain="mobile",
        accepts_root=True,
        description="移动端（Android/iOS）自动化与设备可用性探针",
        priority=60,
        lifecycle="preflight",
        # 设备/工具状态为宿主级；形态、目标平台由报告重投影处理。
        cache_scope="host",
    ),
    ProbeDescriptor(
        name="ios_binary",
        script="ios_binary_probe.py",
        domain="mobile",
        accepts_root=True,
        description="iOS 二进制层探针（Mach-O 加固、代码签名、entitlements、ATS）",
        priority=61,
        lifecycle="runtime",
        readiness_args=("--check-availability",),
        required=False,
    ),
    ProbeDescriptor(
        name="memory",
        script="memory_probe.py",
        domain="memory",
        accepts_root=False,
        description="进程常驻与堆内存泄漏增长检测探针",
        priority=70,
        lifecycle="preflight",
        cache_scope="host",
    ),
    ProbeDescriptor(
        name="differential",
        script="differential_probe.py",
        domain="testing",
        accepts_root=False,
        description="双跑差分与沙盒行为偏离检测探针",
        priority=80,
        lifecycle="runtime",
        readiness_args=("check",),
    ),
    ProbeDescriptor(
        name="openwiki",
        script="openwiki_probe.py",
        domain="knowledge",
        accepts_root=True,
        description="OpenWiki 叙事层知识图谱检索探针",
        priority=90,
        lifecycle="preflight",
        # 纯建议性探测（DOC-032）：缺失不阻断启动。
        required=False,
    ),
    ProbeDescriptor(
        name="concurrency",
        script="fuzzing/concurrency_fuzzer.py",
        domain="concurrency",
        accepts_root=True,
        description="并发竞态与异步时序死锁专项探针",
        priority=82,
        lifecycle="runtime",
        readiness_args=("check",),
    ),
    ProbeDescriptor(
        name="stateful",
        script="fuzzing/stateful_fuzzer.py",
        domain="stateful",
        accepts_root=True,
        description="业务状态机序列突变推演与不变量校验探针",
        priority=84,
        lifecycle="runtime",
        readiness_args=("check",),
    ),
    ProbeDescriptor(
        name="web_conformance",
        script="report_engine/web_conformance.py",
        domain="web",
        accepts_root=True,
        description="Web 标准、无障碍与 HTML 语义合规基线探针",
        priority=65,
        lifecycle="preflight",
    ),
    ProbeDescriptor(
        name="desktop",
        script="desktop_probe.py",
        domain="desktop",
        accepts_root=True,
        description="桌面应用项目级预检：进程模型安全开关、打包完整性与更新通道（只读）",
        priority=66,
        lifecycle="preflight",
    ),
    ProbeDescriptor(
        name="native",
        script="native_probe.py",
        domain="native",
        accepts_root=True,
        description="原生/嵌入式项目级预检：工具链、构建与 sanitizer 插桩能力（只读）",
        priority=67,
        lifecycle="preflight",
    ),
)

_REGISTRY: dict[str, ProbeDescriptor] = {p.name: p for p in _BUILTIN_PROBES}
_PROFILE_CACHE_SECONDS = 30.0 * 60.0
_ENGINE_DIR = Path(__file__).resolve().parent


def _is_engine_dir(root: Path) -> bool:
    """``root`` 是否指向引擎目录自身（而非项目根）。

    DOC-004 Engine-State Separation：引擎目录只承载静态协议文件。
    若把 ``.bugx/state/environment-profile.json`` 这类运行态缓存写进引擎包，
    它会被 ``engine-integrity.json`` 冻结并随包分发到每个用户的
    ``~/.bugx/core``。``launch_phases.common._project_root()`` 在缺少
    ``BUGX_PROJECT_ROOT`` 时回落到 cwd，因此以 ``core/`` 为 cwd 运行会命中
    这个错误路径（实测曾把引擎清单从 196 个文件撑到 197）。
    """
    try:
        return root.resolve() == _ENGINE_DIR
    except OSError:
        return True


def _resolve_probe_script(core_dir: Path, script_name: str) -> Path:
    """把探针脚本名解析成真实路径。

    探针脚本会随架构收敛在 ``core/`` 内迁移（``core/web_conformance.py`` →
    ``core/report_engine/web_conformance.py``）。这里保留三条固定候选以覆盖
    已知布局，最后再加一层**按文件名递归唯一解析**：迁移后不会因为写死的前缀
    全部落空而把「脚本在别处」误报成「缺少脚本」，也不必每次搬迁都回来改这里。
    同名多份时**不猜**——回落到 direct（不存在即由调用方显式报缺失）。
    """
    for candidate in (
        core_dir / script_name,
        core_dir / "probes" / script_name,
        core_dir.parent / "probes" / script_name,
    ):
        if candidate.is_file():
            return candidate
    basename = Path(script_name).name
    # 传入的 ``core_dir`` 可能已经是 ``core/probes`` 自己（旧调用点），因此递归
    # 解析必须从**引擎根**开始；否则 ``core/report_engine/web_conformance.py``
    # 在 ``core/probes`` 下永远搜不到。引擎根的判定锚点是 ``core/bugx.md``。
    for search_root in (core_dir, *core_dir.parents):
        if not (search_root / "bugx.md").is_file():
            continue
        matches = sorted(
            path for path in search_root.rglob(basename)
            if path.is_file() and "__pycache__" not in path.parts
        )
        if len(matches) == 1:
            return matches[0]
        break
    return core_dir / script_name


def _registry_digest_part(digest: "hashlib._Hash", core_path: Path) -> None:
    """把注册中心自身指纹并入缓存键。

    分类/剪枝/瞬态判定逻辑都住在 ``probe_registry.py`` 里：只按探针脚本
    指纹做缓存键会让升级后的新逻辑继续吃旧画像，从而复现已修复的假阻断。
    """
    try:
        registry_file = core_path / "probe_registry.py"
        if not registry_file.is_file():
            registry_file = core_path / "probes" / "probe_registry.py"
        if not registry_file.is_file():
            registry_file = Path(__file__).resolve()
        stat = registry_file.stat()
        digest.update(f"registry:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"))
    except OSError:
        digest.update(b"registry:missing")


def _profile_cache_key(
    root: Path,
    core_path: Path,
    probes: list[ProbeDescriptor],
    project_fingerprint: str | None = None,
) -> str:
    digest = hashlib.sha256(str(root).encode("utf-8"))
    _registry_digest_part(digest, core_path)
    digest.update(
        f"project-content:{project_fingerprint or 'unavailable'}".encode("utf-8")
    )
    for probe in probes:
        script = _resolve_probe_script(core_path, probe.script)
        try:
            stat = script.stat()
            digest.update(f"{probe.name}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"))
        except OSError:
            digest.update(f"{probe.name}:missing".encode("utf-8"))
    return digest.hexdigest()


def _host_cache_key(core_path: Path, probes: list[ProbeDescriptor]) -> str:
    """基于引擎脚本指纹的宿主探针缓存键（独立于临时目录路径），用于跨沙箱复用。"""
    digest = hashlib.sha256(b"host_probe_profile_v1")
    _registry_digest_part(digest, core_path)
    for probe in probes:
        script = _resolve_probe_script(core_path, probe.script)
        try:
            stat = script.stat()
            digest.update(f"{probe.name}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"))
        except OSError:
            digest.update(f"{probe.name}:missing".encode("utf-8"))
    return digest.hexdigest()


import tempfile


def _global_cache_path(key: str) -> Path:
    return Path(tempfile.gettempdir()) / f"bugx-probe-profile-{key[:16]}.json"


def _cached_profile(
    root: Path,
    key: str,
    host_key: str | None = None,
    *,
    allow_project_cache: bool = True,
) -> dict[str, Any] | None:
    path = root / ".bugx" / "state" / "environment-profile.json"
    root_resolved = str(root.resolve())

    def same_project(payload: Mapping[str, Any]) -> bool:
        origin = payload.get("project_root")
        if not isinstance(origin, str) or not origin.strip():
            return False
        try:
            return str(Path(origin).resolve()) == root_resolved
        except (OSError, RuntimeError):
            return False

    def fresh(payload: Mapping[str, Any]) -> bool:
        try:
            generated = float(payload.get("generated_at", 0))
        except (OverflowError, TypeError, ValueError):
            return False
        age = time.time() - generated
        return (
            payload.get("schema_version") == 1
            and math.isfinite(generated)
            and 0 <= age <= _PROFILE_CACHE_SECONDS
        )

    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and fresh(payload):
                project_match = (
                    allow_project_cache
                    and same_project(payload)
                    and payload.get("cache_key") == key
                )
                host_match = host_key is not None and payload.get("host_cache_key") == host_key
                if project_match or host_match:
                    return {
                        **payload,
                        "project_root": str(root),
                        "cached": True,
                        "_cache_match": "project" if project_match else "host",
                    }
        # 跨测试沙箱降级：检查系统全局画像缓存
        candidate_keys = [(key, "project")] if allow_project_cache else []
        if host_key:
            candidate_keys.append((host_key, "host"))
        for candidate_key, match_kind in candidate_keys:
            global_path = _global_cache_path(candidate_key)
            if global_path.is_file():
                payload = json.loads(global_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not fresh(payload):
                    continue
                if match_kind == "project":
                    valid = same_project(payload) and payload.get("cache_key") == key
                else:
                    valid = host_key is not None and payload.get("host_cache_key") == host_key
                if valid:
                    return {
                        **payload,
                        "project_root": str(root),
                        "cached": True,
                        "_cache_match": match_kind,
                    }
    except (OSError, RuntimeError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def _cached_probe_is_reusable(name: str, report: Mapping[str, Any]) -> bool:
    """Reject stale Codebase Memory cache entries before they can mask repair.

    Other probes keep the existing bounded profile-cache policy. Codebase
    Memory is different because its readiness depends on a replaceable external
    executable: failures must be rechecked, and a cached success must prove the
    current exact pin and still resolve to the same compatible executable.
    """
    status = str(report.get("status", "")).upper()
    if status not in {"READY", "NOT_APPLICABLE", "ABSENT", "BLOCKED", "DEGRADED"}:
        return False
    if name != "codebase-memory":
        return True
    if status != "READY":
        return False

    raw = report.get("raw")
    raw_checks = raw.get("checks") if isinstance(raw, dict) else None
    if not isinstance(raw_checks, list):
        return False
    by_name = {
        check.get("name"): check
        for check in raw_checks
        if isinstance(check, dict)
    }
    binary_check = by_name.get("binary")
    supported_check = by_name.get("supported_version")
    version_check = by_name.get("validated_version")
    if not isinstance(binary_check, dict) or binary_check.get("ok") is not True:
        return False
    if not isinstance(supported_check, dict) or supported_check.get("ok") is not True:
        return False
    if not isinstance(version_check, dict) or version_check.get("ok") is not True:
        return False
    binary_path = binary_check.get("detail")
    if not isinstance(binary_path, str) or not binary_path.strip():
        return False

    try:
        from . import codebase_memory_probe

        expected_version = codebase_memory_probe._pinned_cli_version()
        if not expected_version or supported_check.get("expected") != expected_version:
            return False
        if version_check.get("detail") != f"codebase-memory-mcp {expected_version}":
            return False
        current_binary = codebase_memory_probe.resolve_compatible_binary(
            environment=normalized_tool_environment(),
            expected_version=expected_version,
            # The cache check validates only the cheap version response; a full
            # project/index probe runs if this cannot prove the cached identity.
            timeout_seconds=2.0,
        )
    except Exception:
        return False
    return current_binary == binary_path


def register_probe(descriptor: ProbeDescriptor) -> None:
    """动态注册或覆盖一个探针插件。"""
    if not isinstance(descriptor, ProbeDescriptor):
        raise TypeError("descriptor 必须是 ProbeDescriptor 实例")
    _REGISTRY[descriptor.name] = descriptor


def get_probe(name: str) -> ProbeDescriptor | None:
    """根据别名获取探针描述符。"""
    return _REGISTRY.get(name)


def list_probes() -> list[ProbeDescriptor]:
    """按优先级返回所有已注册的探针描述符列表。"""
    return sorted(_REGISTRY.values(), key=lambda p: (p.priority, p.name))


def preflight_probes() -> list[ProbeDescriptor]:
    """返回所有声明为启动前预检 (preflight) 的探针描述符，按优先级升序。"""
    return [p for p in list_probes() if p.lifecycle == "preflight"]


def runtime_probes() -> list[ProbeDescriptor]:
    """返回所有声明为运行期专项 (runtime) 的探针描述符，按优先级升序。"""
    return [p for p in list_probes() if p.lifecycle == "runtime"]


def probe_scripts_map() -> dict[str, str]:
    """返回探针名称到脚本文件的只读映射表。"""
    return {p.name: p.script for p in _REGISTRY.values()}


def probes_without_root() -> set[str]:
    """返回不接受 --root 参数的探针集合。"""
    return {p.name for p in _REGISTRY.values() if not p.accepts_root}


# 探针 checks 的字段别名：不同探针用不同字段名表达同一语义（例如
# web_conformance 用 rule_id/status/message，效率探针用 name/ok/detail）。
# 归一化必须把别名折叠成统一的 (name, ok, detail) 契约，否则下游
# extract_degraded_actions 会把「无 name 的多条检查」按探针名合并成一条、
# 并把有 message 却缺 detail 的真实原因误报为「探针未返回诊断信息」。
_CHECK_NAME_ALIASES: tuple[str, ...] = ("rule_id", "id", "check")
_CHECK_DETAIL_ALIASES: tuple[str, ...] = ("message", "summary", "description")
_CHECK_OK_STATUSES = frozenset({"PASS", "READY", "OK", "SUCCESS", "APPLICABLE", "DEFERRED"})


def normalize_check(check: Any) -> Any:
    """把单条探针 check 折叠为统一的 (name, ok, detail) 契约。

    只补齐缺失的规范字段，绝不覆盖探针已给出的值，因此对标准 check 幂等；
    探针原始字段（rule_id/oracle/status/message/evidence 等）原样保留。
    """
    if not isinstance(check, dict):
        return check
    item = dict(check)

    if not str(item.get("name") or "").strip():
        for alias in _CHECK_NAME_ALIASES:
            candidate = item.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                item["name"] = candidate.strip()
                break

    if not isinstance(item.get("ok"), bool):
        status = str(item.get("status") or "").strip().upper()
        if status:
            item["ok"] = status in _CHECK_OK_STATUSES

    if not str(item.get("detail") or "").strip():
        for alias in _CHECK_DETAIL_ALIASES:
            candidate = item.get(alias)
            if isinstance(candidate, str) and candidate.strip():
                item["detail"] = candidate.strip()
                break

    return item


def normalize_checks(checks: Any) -> list[Any]:
    """对 checks 序列逐条归一化；None 安全降级为空列表。"""
    if not isinstance(checks, (list, tuple)):
        return [] if checks is None else [normalize_check(checks)]
    return [normalize_check(check) for check in checks]


def extract_degraded_actions(report: dict[str, Any], *, probe_name: str = "") -> list[dict[str, Any]]:
    """从探针 JSON 报告提取降级项与对应处置选项。

    detail 为空时的兜底文案与探针命令名/退出码附带都在这个 checks 聚合处
    完成：探针自己说不出为什么失败时，降级项至少要能自证来源（哪条探针
    命令、以什么退出码结束），而不是落成一句无信息量的占位文本。

    DOC-236 C1 两条硬约束：
    * ``NOT_APPLICABLE``/``DEFERRED`` 是**项目形态结论**，不是能力缺口，
      必须剪枝为 0 个降级项——否则"项目没有原生代码"会被当成"环境缺工具"
      顶到启动阻断通道上。
    * ``transient`` 必须原样下传：二进制存在但本次调用超时与"未安装"是
      两种不同的处置。
    """
    actions: list[dict[str, Any]] = []
    report_status = str(report.get("status", "")).strip().upper()
    if report_status in {"NOT_APPLICABLE", "DEFERRED"}:
        return actions

    explicit_actions = report.get("degraded_actions")
    if isinstance(explicit_actions, list) and explicit_actions:
        return [dict(a) for a in explicit_actions if isinstance(a, dict)]

    checks = normalize_checks(report.get("checks") or report.get("base_tools") or report.get("channels") or [])
    eff_probe_name = "codebase-memory-mcp" if probe_name in {"codebase-memory", "codebase_memory_probe"} else probe_name
    probe_command = str(report.get("name") or "").strip() or eff_probe_name
    exit_code = report.get("returncode")
    report_transient = report.get("transient") is True
    provenance = ""
    context: list[str] = []
    if probe_command:
        context.append(f"探针命令 {probe_command}")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        context.append(f"退出码 {exit_code}")
    if context:
        provenance = f"（{'，'.join(context)}）"
    for check in checks:
        if not isinstance(check, dict) or check.get("ok"):
            continue
        raw_name = check.get("name")
        name = str(raw_name).strip() if raw_name is not None else ""
        if not name:
            name = eff_probe_name or "probe"
        # DOC-226: 消除将未执行的浏览器动态测试规则误提取为工具安装动作
        rule_id = str(check.get("rule_id", ""))
        if name.startswith("dynamic.") or rule_id.startswith("dynamic.") or str(check.get("status", "")).upper() == "DEFERRED":
            continue
        detail = str(check.get("detail") or "").strip()
        if not detail or detail == "探针未提供失败详情":
            detail = "探针未返回诊断信息（probe 无 stderr/diagnostics 输出）"
        if provenance:
            detail = f"{detail}{provenance}"
        is_mcp = (
            "mcp" in name.casefold()
            or "mcp" in eff_probe_name.casefold()
            or "codebase-memory" in eff_probe_name.casefold()
        )
        display_name = (
            f"{eff_probe_name}/{name}"
            if is_mcp and "mcp" not in name.casefold() and eff_probe_name else name
        )
        is_required = check.get("required", report.get("required", True))
        item = {
            "name": display_name,
            "detail": detail,
            "options": [
                f"安装/升级：{report.get('install', '见项目文档')}",
                "跳过：使用回退方案 " + str(report.get("fallback", "内置工具")),
            ],
            "required": bool(is_required),
            "advisory": not is_required,
            "critical": bool(is_required),
        }
        if is_mcp:
            item["kind"] = "mcp"
        if report_transient or check.get("transient") is True:
            item["transient"] = True
        actions.append(item)
    return actions


def _should_skip_platform_sub_report(
    platform_name: str,
    forms: set[str] | None,
    languages: set[str] | None,
    targets: set[str] | None = None,
    frameworks: set[str] | None = None,
) -> bool:
    """DOC-200 / DOC-248: 精细化多维形态与目标平台守卫。"""
    # 1. 宏观形态守卫：非移动端项目跳过 android 与 ios 硬件平台状态裁决
    if forms is not None and "mobile" not in forms and platform_name in {"android", "ios", "apple"}:
        return True
    # 2. 目标架构与框架精准守卫：
    # A. Android 平台：如果移动端项目中明确只有 iOS 目标或纯小程序 (无 Android 目标且无 Android 源码/清单特征)，跳过 Android 硬件平台裁决
    if platform_name == "android":
        if targets is not None and targets and "android" not in targets:
            return True
        if frameworks is not None and "miniprogram" in frameworks and (not targets or "android" not in targets):
            return True
    # B. iOS/Apple 平台：如果移动端项目中明确只有 Android 目标或纯小程序 (无 iOS 目标且无 Xcode/iOS 源码)，跳过 iOS 硬件平台裁决
    if platform_name in {"ios", "apple"}:
        if targets is not None and targets and not (targets & {"ios", "apple"}):
            return True
        if frameworks is not None and "miniprogram" in frameworks and not (targets and targets & {"ios", "apple"}):
            return True
    # 3. 技术栈形态对齐：非 JVM 项目跳过 jvm 状态裁决；非桌面GUI/Node项目跳过 electron 状态裁决
    if languages and not any(l in languages for l in ("java", "kotlin", "scala", "groovy", "jvm")) and platform_name == "jvm":
        return True
    if (forms and "desktop_gui" not in forms) and (languages and not any(l in languages for l in ("javascript", "typescript", "electron", "node"))) and platform_name == "electron":
        return True
    return False


def _relax_preflight_dynamic_hardware_checks(
    chk: dict[str, Any], platform_name: str
) -> dict[str, Any]:
    """预检阶段动态物理硬件通道容错放行：未连接真机/模拟器降级为建议性警告，不阻断静态审计。"""
    p = str(platform_name or "").strip().casefold()
    if p in {"android", "ios", "apple", "mobile"}:
        cname = str(chk.get("name", "")).strip()
        if any(kw in cname for kw in ("已连接真机", "真机/模拟器", "Simulator", "dumpsys", "Perfetto")):
            if not chk.get("ok"):
                relaxed = dict(chk)
                relaxed["required"] = False
                relaxed["advisory"] = True
                return relaxed
    return chk


def _detect_check_platform(chk: dict[str, Any], default_platform: str = "") -> str:
    """提取单项 check 所属的技术栈/硬件平台。"""
    p = str(chk.get("platform") or chk.get("target") or default_platform or "").strip().casefold()
    if p in {"android", "ios", "apple", "jvm", "electron"}:
        return p
    cname = str(chk.get("name") or "").casefold()
    kind = str(chk.get("kind") or "").casefold()
    if "android" in cname or "adb" in cname or kind == "adb" or "perfetto" in cname or "dumpsys" in cname:
        return "android"
    if "ios" in cname or "simulator" in cname or "simctl" in cname or kind == "simctl" or "xcode" in cname:
        return "ios"
    if "jvm" in cname or "java" in cname or "jcmd" in cname:
        return "jvm"
    if "electron" in cname:
        return "electron"
    return p


def _normalize_probe_report(
    name: str,
    payload: Any,
    returncode: int = 0,
    forms: set[str] | None = None,
    languages: set[str] | None = None,
    targets: set[str] | None = None,
    frameworks: set[str] | None = None,
) -> dict[str, Any]:
    """将探针原始输出统一归一化为标准的 ProbeReport 单根字典结构。"""
    p_name = "codebase-memory-mcp" if name in {"codebase-memory", "codebase_memory_probe"} else name

    if payload is None or (returncode != 0 and not payload):
        report_dict: dict[str, Any] = {
            "name": name,
            "status": "DEGRADED" if returncode != 0 else "BLOCKED",
            "checks": [{"name": name, "ok": False, "detail": "探针输出异常或执行失败"}],
            "capabilities": [],
            "fallbacks": {},
            "warnings": [f"探针 {name} 退出码 {returncode}，未返回有效 JSON"],
            "raw": payload,
            "returncode": returncode,
        }
        report_dict["degraded_actions"] = extract_degraded_actions(report_dict, probe_name=p_name)
        return report_dict

    if isinstance(payload, list):
        checks: list[dict[str, Any]] = []
        warnings: list[str] = []
        capabilities: list[str] = []
        sub_statuses: list[str] = []
        platforms: list[str] = []
        for item in payload:
            if isinstance(item, dict):
                platform_name = str(item.get("platform", item.get("target", name))).casefold()
                if _should_skip_platform_sub_report(platform_name, forms, languages, targets, frameworks):
                    continue
                platforms.append(platform_name)
                item_checks: list[dict[str, Any]] = []
                for chk in item.get("checks", []):
                    if isinstance(chk, dict):
                        chk = normalize_check(chk)
                        chk = _relax_preflight_dynamic_hardware_checks(chk, platform_name)
                        item_checks.append(chk)
                        checks.append(chk)
                        if chk.get("ok"):
                            capabilities.append(f"{platform_name}:{chk.get('name', '')}")
                if any(not c.get("ok") and c.get("required", True) for c in item_checks):
                    st = "BLOCKED"
                elif any(not c.get("ok") for c in item_checks):
                    st = "DEGRADED"
                else:
                    st = "READY"
                sub_statuses.append(st)
                warnings.extend(item.get("warnings", []))
        if not sub_statuses:
            status = "NOT_APPLICABLE"
            checks = [{"name": name, "ok": True, "detail": "当前项目形态/目标免检", "required": False}]
        elif any(s == "BLOCKED" for s in sub_statuses):
            status = "BLOCKED"
        elif any(s == "DEGRADED" for s in sub_statuses):
            status = "DEGRADED"
        else:
            status = "READY"
        report_dict = {
            "name": name,
            "status": status,
            "platforms": platforms,
            "checks": checks,
            "capabilities": capabilities,
            "fallbacks": {},
            "warnings": warnings,
            "reports": payload,
            "raw": payload,
            "returncode": returncode,
        }
        report_dict["degraded_actions"] = extract_degraded_actions(report_dict, probe_name=p_name)
        return report_dict

    if isinstance(payload, dict):
        # DOC-226: web_conformance 预检阶段免检动态浏览器规则
        if name == "web_conformance":
            raw_checks = payload.get("checks") or []
            normalized_checks = []
            for chk in raw_checks:
                if isinstance(chk, dict):
                    r_id = str(chk.get("rule_id", chk.get("name", "")))
                    st = str(chk.get("status", "")).upper()
                    msg = str(chk.get("message", chk.get("detail", "")))
                    if r_id.startswith("dynamic.") and (st in {"BLOCKED", "DEFERRED"} or "缺少浏览器动态证据" in msg):
                        chk_copy = dict(chk)
                        chk_copy["status"] = "DEFERRED"
                        chk_copy["ok"] = True
                        chk_copy["detail"] = f"动态规则运行期检验（{r_id}），预检免检"
                        normalized_checks.append(chk_copy)
                        continue
                normalized_checks.append(chk)
            payload = dict(payload)
            payload["checks"] = normalized_checks
            statuses = [
                str(c.get("status", "")).upper()
                for c in normalized_checks
                if isinstance(c, dict) and str(c.get("status", "")).upper() not in {"DEFERRED", "NOT_APPLICABLE"}
            ]
            if payload.get("status") == "NOT_APPLICABLE":
                pass
            elif any(s == "FAIL" for s in statuses):
                payload["status"] = "FAIL"
            elif any(s == "BLOCKED" for s in statuses):
                payload["status"] = "BLOCKED"
            else:
                payload["status"] = "READY"

        raw_reports = payload.get("reports")
        if isinstance(raw_reports, list):
            filtered_sub_reports = [
                r for r in raw_reports
                if isinstance(r, dict)
                and not _should_skip_platform_sub_report(
                    str(r.get("platform", r.get("target", ""))).casefold(),
                    forms, languages, targets, frameworks,
                )
            ]
            checks = []
            warnings = []
            capabilities = []
            sub_statuses = []
            platforms = []
            for item in filtered_sub_reports:
                pname = str(item.get("platform", item.get("target", name)))
                platforms.append(pname)
                item_checks = []
                for chk in item.get("checks", []):
                    if isinstance(chk, dict):
                        chk = normalize_check(chk)
                        chk = _relax_preflight_dynamic_hardware_checks(chk, pname.casefold())
                        item_checks.append(chk)
                        checks.append(chk)
                        if chk.get("ok"):
                            capabilities.append(f"{pname}:{chk.get('name', '')}")
                if any(not c.get("ok") and c.get("required", True) for c in item_checks):
                    st = "BLOCKED"
                elif any(not c.get("ok") for c in item_checks):
                    st = "DEGRADED"
                else:
                    st = "READY"
                sub_statuses.append(st)
                warnings.extend(item.get("warnings", []))
            if not sub_statuses:
                status = "NOT_APPLICABLE"
                checks = [{"name": name, "ok": True, "detail": "当前项目形态/目标免检", "required": False}]
            elif any(s == "BLOCKED" for s in sub_statuses):
                status = "BLOCKED"
            elif any(s == "DEGRADED" for s in sub_statuses):
                status = "DEGRADED"
            else:
                status = "READY"
            report_dict = dict(payload)
            report_dict.update({
                "name": name,
                "status": status,
                "platforms": platforms,
                "checks": checks,
                "capabilities": capabilities,
                "warnings": warnings,
                "reports": filtered_sub_reports,
                "raw": payload,
                "returncode": returncode,
            })
            report_dict["degraded_actions"] = extract_degraded_actions(report_dict, probe_name=p_name)
            return report_dict

        raw_status = str(payload.get("status", "READY" if returncode == 0 else "DEGRADED")).upper()
        if raw_status == "PASS":
            status = "READY"
        elif raw_status == "NOT_APPLICABLE":
            # Preserve the distinction between a probe that ran successfully
            # and one intentionally skipped by a profile guard.  DOC-200
            # relies on this value for transparent coverage accounting.
            status = "NOT_APPLICABLE"
        else:
            status = raw_status
        raw_checks = normalize_checks(payload.get("checks") or payload.get("base_tools") or [])
        checks = []
        for chk in raw_checks:
            if isinstance(chk, dict):
                pname = _detect_check_platform(chk, default_platform=name)
                if pname and _should_skip_platform_sub_report(pname, forms, languages, targets, frameworks):
                    continue
                chk = _relax_preflight_dynamic_hardware_checks(chk, pname)
                checks.append(chk)
        if raw_status == "NOT_APPLICABLE":
            status = "NOT_APPLICABLE"
        elif raw_checks and not checks:
            status = "NOT_APPLICABLE"
            checks = [{"name": name, "ok": True, "detail": "当前项目形态/目标免检", "required": False}]
        elif any(not c.get("ok") and c.get("required", True) for c in checks):
            status = "BLOCKED"
        elif any(not c.get("ok") for c in checks):
            status = "DEGRADED"
        elif checks:
            status = "READY"
        warnings = list(payload.get("warnings") or [])
        fallbacks = dict(payload.get("fallbacks") or {})
        if "fallback" in payload and "fallback" not in fallbacks:
            fallbacks["default"] = str(payload["fallback"])
        capabilities = list(payload.get("capabilities") or [])
        for chk in checks:
            if isinstance(chk, dict) and chk.get("ok"):
                cname = str(chk.get("name", "")).strip()
                if cname and cname not in capabilities:
                    capabilities.append(cname)
        report_dict = dict(payload)
        report_dict.update({
            "name": name,
            "status": status,
            "checks": checks,
            "capabilities": capabilities,
            "fallbacks": fallbacks,
            "warnings": warnings,
            "raw": payload,
            "returncode": returncode,
        })
        if "degraded_actions" in report_dict and isinstance(report_dict["degraded_actions"], list):
            valid_actions = []
            for a in report_dict["degraded_actions"]:
                if isinstance(a, dict):
                    pname = _detect_check_platform(a, default_platform=name)
                    if pname and _should_skip_platform_sub_report(pname, forms, languages, targets, frameworks):
                        continue
                    if any(kw in str(a.get("name", "")) for kw in ("已连接真机", "真机/模拟器", "Simulator", "dumpsys", "Perfetto")):
                        a_rel = dict(a)
                        a_rel["critical"] = False
                        a_rel["advisory"] = True
                        valid_actions.append(a_rel)
                    else:
                        valid_actions.append(a)
            report_dict["degraded_actions"] = valid_actions
        report_dict["degraded_actions"] = extract_degraded_actions(report_dict, probe_name=p_name)
        return report_dict

    return {
        "name": name,
        "status": "DEGRADED",
        "checks": [{"name": name, "ok": False, "detail": f"未知输出格式: {type(payload).__name__}"}],
        "capabilities": [],
        "fallbacks": {},
        "warnings": ["非标准输出"],
        "degraded_actions": [],
        "raw": payload,
    }


def _run_single_probe(
    descriptor: ProbeDescriptor,
    root: Path,
    core_dir: Path,
    timeout: float,
    *,
    forms: set[str] | None = None,
    languages: set[str] | None = None,
    targets: set[str] | None = None,
    frameworks: set[str] | None = None,
    action_args: Sequence[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    script = _resolve_probe_script(core_dir, descriptor.script)
    if not script.is_file():
        return descriptor.name, _normalize_probe_report(
            descriptor.name,
            {"status": "DEGRADED", "checks": [{"name": descriptor.name, "ok": False, "detail": f"脚本缺失: {descriptor.script}"}]},
            returncode=1,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )

    cmd = [sys.executable, str(script), *(action_args or ("check",))]
    if descriptor.accepts_root:
        cmd.extend(["--root", str(root)])
    if descriptor.name == "web_conformance":
        cmd.append("--preflight")
    cmd.append("--json")

    started = time.monotonic()
    try:
        environment = normalized_tool_environment()
        if descriptor.name == "codebase-memory":
            # Keep the probe's internal CLI deadline aligned with the outer
            # adaptive budget; otherwise the outer expansion would be fake.
            environment["BUGX_PROBE_TIMEOUT_CODEBASE_MEMORY"] = str(timeout)
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
        stdout = proc.stdout.strip()
        payload = None
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = None
        report = _normalize_probe_report(
            descriptor.name,
            payload,
            returncode=proc.returncode,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["timeout_seconds"] = timeout
        return descriptor.name, report
    except subprocess.TimeoutExpired:
        report = _normalize_probe_report(
            descriptor.name,
            {"status": "DEGRADED", "transient": True, "checks": [{"name": descriptor.name, "ok": False, "detail": f"探针超时 (>{timeout}s)"}]},
            returncode=124,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["timeout_seconds"] = timeout
        return descriptor.name, report
    except Exception as exc:
        report = _normalize_probe_report(
            descriptor.name,
            {"status": "DEGRADED", "checks": [{"name": descriptor.name, "ok": False, "detail": f"执行异常: {exc}"}]},
            returncode=1,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        report["timeout_seconds"] = timeout
        return descriptor.name, report


def run_probe_readiness(
    name: str,
    root: Path,
    *,
    core_dir: Path | None = None,
    forms: set[str] | None = None,
    languages: set[str] | None = None,
    targets: set[str] | None = None,
    frameworks: set[str] | None = None,
) -> dict[str, Any]:
    """Run the descriptor's bounded, target-safe availability action."""
    descriptor = get_probe(name)
    if descriptor is None:
        return _normalize_probe_report(
            name,
            {"status": "BLOCKED", "checks": [{
                "name": "descriptor", "ok": False,
                "detail": f"未注册的 BugX probe: {name}",
            }]},
            returncode=1,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
    if descriptor.lifecycle not in {"preflight", "runtime"}:
        return _normalize_probe_report(
            descriptor.name,
            {"status": "BLOCKED", "checks": [{
                "name": "lifecycle", "ok": False,
                "detail": f"probe lifecycle 无法执行就绪检查: {descriptor.lifecycle}",
            }]},
            returncode=1,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
    if not descriptor.readiness_args:
        return _normalize_probe_report(
            descriptor.name,
            {"status": "BLOCKED", "checks": [{
                "name": "readiness_action", "ok": False,
                "detail": "探针未声明目标安全的 availability action；拒绝隐式运行 check",
            }]},
            returncode=1,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
    action_args = descriptor.readiness_args
    _, report = _run_single_probe(
        descriptor,
        root.resolve(),
        (core_dir or Path(__file__).resolve().parent).resolve(),
        probe_timeout_budget(descriptor.name),
        forms=forms,
        languages=languages,
        targets=targets,
        frameworks=frameworks,
        action_args=action_args,
    )
    return report


def _previous_probe_durations(root: Path) -> dict[str, float]:
    path = root / ".bugx" / "state" / "environment-profile.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        probes = payload.get("probes", {}) if isinstance(payload, dict) else {}
        return {
            str(name): float(report["duration_seconds"])
            for name, report in probes.items()
            if isinstance(report, dict)
            and _positive_timeout(report.get("duration_seconds")) is not None
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _classify_degraded_actions(
    reports_by_name: Mapping[str, Any],
    descriptors: Sequence[ProbeDescriptor],
) -> list[dict[str, Any]]:
    """给每个降级项补上描述符级机器语义（advisory / critical / probe）。

    DOC-236 C1：上层门禁必须能吃到**机器结论**，不能靠名字或文案猜测。

    * 报告状态为 ``NOT_APPLICABLE``/``DEFERRED`` 一律剪枝——项目形态决定的
      "不适用"不是能力缺口，进入阻断通道就是假阻断。
    * ``required=False`` 的建议性探针 ⇒ ``advisory=True``：它的缺失按注册表
      策略不构成启动阻断，但保留条目与告警，绝不静默消失。
    * ``critical`` 直接取自描述符，覆盖上层 ``kind=mcp ⇒ critical`` 的猜测。
    * ``transient``（二进制存在但调用超时）原样保留，与"未安装"分开处置。
    """
    by_name = {descriptor.name: descriptor for descriptor in descriptors}
    classified: list[dict[str, Any]] = []
    for name, report in reports_by_name.items():
        if not isinstance(report, dict):
            continue
        if str(report.get("status", "")).strip().upper() in {"NOT_APPLICABLE", "DEFERRED"}:
            continue
        descriptor = by_name.get(name)
        for raw in report.get("degraded_actions") or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if descriptor is not None:
                item["probe"] = descriptor.name
                if not descriptor.critical:
                    item["critical"] = False
                elif "critical" not in item:
                    item["critical"] = descriptor.critical
                if not descriptor.required:
                    item["advisory"] = True
                elif "advisory" not in item or not item["advisory"]:
                    item["advisory"] = not descriptor.required
            classified.append(item)
    return classified


def _project_single_probe_report(
    name: str,
    rep: dict[str, Any],
    forms: set[str] | None,
    languages: set[str] | None,
    targets: set[str] | None = None,
    frameworks: set[str] | None = None,
) -> dict[str, Any]:
    """根据当前项目的形态 (forms/languages/targets/frameworks) 重新投影探针报告。"""
    # 1. 当前形态不适用时，直接投影为 NOT_APPLICABLE，彻底剪枝降级项与阻断
    if name == "mobile":
        is_pure_miniprogram = (
            frameworks is not None
            and "miniprogram" in frameworks
            and not (targets and targets & {"android", "ios", "apple"})
        )
        if (forms is not None and "mobile" not in forms) or is_pure_miniprogram:
            proj_rep = _normalize_probe_report(
                "mobile",
                {
                    "status": "NOT_APPLICABLE",
                    "detail": "当前项目非原生移动端形态（或为纯小程序），已自适应跳过原生驱动",
                    "checks": [{"name": "mobile", "ok": True, "detail": "非原生移动端项目，免检"}],
                },
                returncode=0,
                forms=forms,
                languages=languages,
                targets=targets,
                frameworks=frameworks,
            )
            proj_rep["duration_seconds"] = rep.get("duration_seconds", 0.001)
            return proj_rep

    if name == "web_conformance" and (forms is not None and "web" not in forms):
        proj_rep = _normalize_probe_report(
            "web_conformance",
            {
                "status": "NOT_APPLICABLE",
                "detail": "当前项目非 Web 视口形态，已自适应跳过",
                "checks": [{"name": "web_conformance", "ok": True, "detail": "非 Web 项目，免检"}],
            },
            returncode=0,
            forms=forms,
            languages=languages,
            targets=targets,
            frameworks=frameworks,
        )
        proj_rep["duration_seconds"] = rep.get("duration_seconds", 0.001)
        return proj_rep

    # 2. 如果缓存中是免检状态，但当前项目形态需要该探针，返回失效标记触发实测
    if rep.get("status") == "NOT_APPLICABLE":
        if (name == "mobile" and forms is not None and "mobile" in forms) or (
            name == "web_conformance" and forms is not None and "web" in forms
        ):
            return {"status": "STALE_NOT_APPLICABLE"}

    # 3. 适用探针：使用原始输出重新走归一化与动态容错门禁
    payload = rep.get("raw", rep)
    returncode = rep.get("returncode", 0)
    proj_rep = _normalize_probe_report(
        name,
        payload,
        returncode=returncode,
        forms=forms,
        languages=languages,
        targets=targets,
        frameworks=frameworks,
    )
    proj_rep["duration_seconds"] = rep.get("duration_seconds", 0.001)
    if "timeout_seconds" in rep:
        proj_rep["timeout_seconds"] = rep["timeout_seconds"]
    return proj_rep


def _synthesize_profile(
    root: Path,
    cache_key: str,
    host_key: str,
    reports_by_name: Mapping[str, dict[str, Any]],
    probes: Sequence[ProbeDescriptor],
    cached: bool = False,
    reused_ready_probes: Sequence[str] | None = None,
    rechecked_probes: Sequence[str] | None = None,
) -> dict[str, Any]:
    all_capabilities: set[str] = set()
    statuses: list[str] = []

    for name, rep in reports_by_name.items():
        if not isinstance(rep, dict):
            continue
        st = rep.get("status", "READY")
        statuses.append(st)
        for cap in rep.get("capabilities", []):
            all_capabilities.add(cap)

    # DOC-236 C1：降级项必须携带**描述符级**机器语义，让上层门禁能区分
    # 「建议性探针」「critical 能力」和普通缺口，而不是靠名字/文案猜。
    all_degraded_actions = _classify_degraded_actions(reports_by_name, probes)

    if any(s == "BLOCKED" for s in statuses):
        overall_status = "BLOCKED"
    elif any(s == "DEGRADED" for s in statuses):
        overall_status = "DEGRADED"
    else:
        overall_status = "READY"

    blocked_probes = [
        {"name": name, "status": rep.get("status"), "checks": [c for c in rep.get("checks", []) if not c.get("ok", True)]}
        for name, rep in reports_by_name.items()
        if isinstance(rep, dict) and rep.get("status") in {"BLOCKED", "ERROR"}
    ]
    escalation = None
    if overall_status == "BLOCKED":
        escalation = {
            "required": True,
            "failed_probes": blocked_probes,
            "suggested_llm_action": "diagnose_probe_blocker",
            "message": "系统级探针环境检测阻断，需唤醒 LLM 基于 failed_probes 进行定点根因排查",
        }

    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "overall_status": overall_status,
        "project_root": str(root),
        "cache_key": cache_key,
        "host_cache_key": host_key,
        "cached": cached,
        "capabilities": sorted(all_capabilities),
        "probes": dict(reports_by_name),
        "reused_ready_probes": sorted(reused_ready_probes or []),
        "rechecked_probes": sorted(rechecked_probes or []),
        "degraded_actions": all_degraded_actions,
        "escalation": escalation,
    }


def run_preflight_probes(
    root: Path,
    timeout: float = 10.0,
    parallel: bool = True,
    core_dir: Path | None = None,
    save_profile: bool = True,
    refresh: bool = False,
    project_profile_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """统一并发执行所有启动前预检探针并生成环境画像。"""
    import concurrent.futures
    import time
    import core.secure_atomic as secure_atomic

    root = root.resolve()
    core_path = (core_dir or Path(__file__).resolve().parent).resolve()
    probes = preflight_probes()
    host_key = _host_cache_key(core_path, probes)

    # DOC-200 / DOC-183 / DOC-226: 基于工程画像对形态与技术栈进行精准对齐与自适应剪枝
    project_forms: set[str] = set()
    project_languages: set[str] = set()
    project_targets: set[str] = set()
    project_frameworks: set[str] = set()
    project_fingerprint: str | None = None
    try:
        from core.runtime import project_profile
        profile = (
            project_profile_data
            if isinstance(project_profile_data, Mapping)
            else project_profile.detect_project_profile(root)
        )
        project_forms = set(profile.get("forms", []))
        project_languages = {str(lang).casefold() for lang in profile.get("languages", [])}
        project_targets = {str(t).casefold() for t in (profile.get("declared_targets") or [])}
        for bt in profile.get("build_targets") or []:
            if isinstance(bt, dict):
                for k in ("platform", "name"):
                    val = bt.get(k)
                    if isinstance(val, str) and val.strip():
                        project_targets.add(val.strip().casefold())
        for t in profile.get("targets") or []:
            if isinstance(t, str):
                project_targets.add(t.casefold())
        project_frameworks = {str(f).casefold() for f in profile.get("frameworks", [])}
        source_fingerprint = profile.get("source_fingerprint")
        fingerprint_digest = (
            source_fingerprint[7:]
            if isinstance(source_fingerprint, str)
            and source_fingerprint.startswith("sha256:")
            and len(source_fingerprint) == 71
            else ""
        )
        profile_complete = (
            profile.get("status") in {"complete", "unknown"}
            and profile.get("scan_truncated") is not True
        )
        detector_version = profile.get("detector_version")
        if (
            profile_complete
            and isinstance(detector_version, str)
            and detector_version.strip()
            and len(fingerprint_digest) == 64
            and all(
                char in "0123456789abcdef" for char in fingerprint_digest
            )
        ):
            project_fingerprint = f"{detector_version}:{source_fingerprint}"
    except Exception:
        pass
    cache_key = _profile_cache_key(
        root, core_path, probes, project_fingerprint=project_fingerprint,
    )

    probes_to_run = probes
    cached_ready: dict[str, dict[str, Any]] = {}
    if save_profile and not refresh:
        cached = _cached_profile(
            root,
            cache_key,
            host_key=host_key,
            allow_project_cache=project_fingerprint is not None,
        )
        if cached is not None:
            cached_reports = cached.get("probes")
            if isinstance(cached_reports, dict):
                descriptors_by_name = {probe.name: probe for probe in probes}
                same_project_cache = cached.get("_cache_match") == "project"
                cached_ready = {
                    name: _project_single_probe_report(
                        name,
                        report,
                        forms=project_forms,
                        languages=project_languages,
                        targets=project_targets,
                        frameworks=project_frameworks,
                    )
                    for name, report in cached_reports.items()
                    if name in descriptors_by_name
                    and (
                        same_project_cache
                        or descriptors_by_name[name].cache_scope == "host"
                    )
                    if isinstance(report, dict)
                    and _cached_probe_is_reusable(name, report)
                }
                cached_ready = {
                    name: report for name, report in cached_ready.items()
                    if report.get("status") in {"READY", "NOT_APPLICABLE", "ABSENT", "BLOCKED", "DEGRADED"}
                }
                probes_to_run = [p for p in probes if p.name not in cached_ready]
                if not probes_to_run:
                    synth = _synthesize_profile(
                        root=root,
                        cache_key=cache_key,
                        host_key=host_key,
                        reports_by_name=cached_ready,
                        probes=probes,
                        cached=True,
                        reused_ready_probes=list(cached_ready.keys()),
                        rechecked_probes=[],
                    )
                    if save_profile and not _is_engine_dir(root):
                        try:
                            state_dir = root / ".bugx" / "state"
                            state_dir.mkdir(parents=True, exist_ok=True)
                            secure_atomic.atomic_write_json(state_dir / "environment-profile.json", synth)
                        except Exception:
                            pass
                    return synth

    previous_durations = _previous_probe_durations(root)
    per_probe_timeout_by_name = {
        probe.name: probe_timeout_budget(probe.name, previous_durations.get(probe.name))
        for probe in probes_to_run
    }
    reports_by_name: dict[str, dict[str, Any]] = dict(cached_ready)

    applicable_probes = []
    for probe in probes_to_run:
        if probe.name == "mobile":
            is_pure_miniprogram = (
                project_frameworks
                and "miniprogram" in project_frameworks
                and not (project_targets and project_targets & {"android", "ios", "apple"})
            )
            if "mobile" not in project_forms or is_pure_miniprogram:
                rep = _normalize_probe_report(
                    "mobile",
                    {
                        "status": "NOT_APPLICABLE",
                        "detail": "当前项目非原生移动端形态（或为纯小程序），已自适应跳过原生驱动",
                        "checks": [{"name": "mobile", "ok": True, "detail": "非原生移动端项目，免检"}],
                    },
                    returncode=0,
                    forms=project_forms,
                    languages=project_languages,
                    targets=project_targets,
                    frameworks=project_frameworks,
                )
                rep["duration_seconds"] = 0.001
                reports_by_name["mobile"] = rep
                continue
        if probe.name == "web_conformance" and "web" not in project_forms:
            rep = _normalize_probe_report(
                "web_conformance",
                {
                    "status": "NOT_APPLICABLE",
                    "detail": "当前项目非 Web 视口形态，已自适应跳过",
                    "checks": [{"name": "web_conformance", "ok": True, "detail": "非 Web 项目，免检"}],
                },
                returncode=0,
                forms=project_forms,
                languages=project_languages,
                targets=project_targets,
                frameworks=project_frameworks,
            )
            rep["duration_seconds"] = 0.001
            reports_by_name["web_conformance"] = rep
            continue
        applicable_probes.append(probe)
    probes_to_run = applicable_probes

    if parallel and len(probes_to_run) > 1:
        max_workers = min(8, len(probes_to_run))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    _run_single_probe,
                    p,
                    root,
                    core_path,
                    per_probe_timeout_by_name[p.name],
                    forms=project_forms,
                    languages=project_languages,
                    targets=project_targets,
                    frameworks=project_frameworks,
                ): p.name
                for p in probes_to_run
            }
            deadline = max(
                float(timeout) + 1.0,
                max(per_probe_timeout_by_name.values(), default=5.0) + 1.0,
            )
            done, not_done = concurrent.futures.wait(future_map.keys(), timeout=deadline)
            for fut in done:
                try:
                    name, rep = fut.result()
                    reports_by_name[name] = rep
                except Exception as exc:
                    name = future_map[fut]
                    reports_by_name[name] = _normalize_probe_report(
                        name,
                        {"status": "DEGRADED", "checks": [{"name": name, "ok": False, "detail": str(exc)}]},
                        returncode=1,
                        forms=project_forms,
                        languages=project_languages,
                        targets=project_targets,
                        frameworks=project_frameworks,
                    )
            for fut in not_done:
                name = future_map[fut]
                fut.cancel()
                reports_by_name[name] = _normalize_probe_report(
                    name,
                    {"status": "DEGRADED", "transient": True, "checks": [{"name": name, "ok": False, "detail": "总体超时被取消"}]},
                    returncode=124,
                    forms=project_forms,
                    languages=project_languages,
                    targets=project_targets,
                    frameworks=project_frameworks,
                )
    else:
        for p in probes_to_run:
            name, rep = _run_single_probe(
                p,
                root,
                core_path,
                per_probe_timeout_by_name[p.name],
                forms=project_forms,
                languages=project_languages,
                targets=project_targets,
                frameworks=project_frameworks,
            )
            reports_by_name[name] = rep

    profile = _synthesize_profile(
        root=root,
        cache_key=cache_key,
        host_key=host_key,
        reports_by_name=reports_by_name,
        probes=probes,
        cached=bool(cached_ready),
        reused_ready_probes=list(cached_ready.keys()),
        rechecked_probes=[p.name for p in probes_to_run],
    )

    # 引擎目录不是项目根：把运行态缓存写进引擎包会被 engine-integrity.json
    # 冻结并随包分发（DOC-004）。此处只关闭落盘，返回值 profile 保持完整——
    # 调用方 supervisor_bridge.run_preflight_probes 依赖它。
    if save_profile and _is_engine_dir(root):
        save_profile = False

    if save_profile:
        state_dir = root / ".bugx" / "state"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            profile_path = state_dir / "environment-profile.json"
            secure_atomic.atomic_write_json(profile_path, profile)
        except Exception:
            pass
        try:
            secure_atomic.atomic_write_json(_global_cache_path(cache_key), profile)
        except Exception:
            pass
        try:
            # 合并 host 级探针缓存：保留已探测到的各领域真实工具输出，避免被特定项目的 NOT_APPLICABLE 冲掉
            global_host_path = _global_cache_path(host_key)
            merged_host_probes = dict(profile.get("probes", {}))
            if global_host_path.is_file():
                try:
                    existing_host_profile = json.loads(global_host_path.read_text(encoding="utf-8"))
                    if isinstance(existing_host_profile, dict):
                        for p_name, p_rep in existing_host_profile.get("probes", {}).items():
                            cur_rep = merged_host_probes.get(p_name)
                            if (
                                cur_rep is None
                                or cur_rep.get("status") == "NOT_APPLICABLE"
                            ) and isinstance(p_rep, dict) and p_rep.get("status") != "NOT_APPLICABLE":
                                merged_host_probes[p_name] = p_rep
                except Exception:
                    pass
            host_profile_to_save = dict(profile)
            host_profile_to_save["probes"] = merged_host_probes
            secure_atomic.atomic_write_json(global_host_path, host_profile_to_save)
        except Exception:
            pass

    return profile
