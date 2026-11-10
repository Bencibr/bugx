#!/usr/bin/env python3
"""core/dev_harness/interaction_preset.py - 测试预设交互应答匹配与执行器。

注意：本模块仅在本地开发与 Alpha 测试通道中使用，用于支撑端到端无头/自动化测试；
在生产发布（Stable）构建流水线中会被物理剥离，严禁作为生产业务的强依赖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence

PRESET_FILENAME = "test-interaction-preset.json"
PRESET_ENV_VAR = "BUGX_TEST_INTERACTION_FILE"


def _locate_preset_file(root: Path | None = None) -> Path | None:
    """寻找预设配置文件；优先从环境变量读取，其次从 <root>/.bugx/ 目录探测。"""
    env_path = os.environ.get(PRESET_ENV_VAR, "").strip()
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.is_file() and not p.is_symlink():
            return p
        return None

    candidate_roots: list[Path] = []
    if root is not None:
        candidate_roots.append(root)
    else:
        env_root = os.environ.get("BUGX_PROJECT_ROOT", "").strip()
        if env_root:
            candidate_roots.append(Path(env_root))
        try:
            candidate_roots.append(Path.cwd())
        except (OSError, RuntimeError):
            pass

    for r in candidate_roots:
        try:
            resolved_root = r.expanduser().resolve()
            candidate = resolved_root / ".bugx" / PRESET_FILENAME
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except (OSError, RuntimeError, ValueError):
            continue

    return None


def _read_preset_raw_payload(root: Path | None = None) -> dict[str, Any]:
    """安全读取测试预设文件原始内容；文件不存在或权限不安全时 fail-closed 返回空字典。"""
    preset_file = _locate_preset_file(root)
    if not preset_file:
        return {}

    try:
        st = preset_file.stat()
        if bool(st.st_mode & 0o002):  # world-writable 防御
            return {}

        payload = json.loads(preset_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return payload
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return {}


def get_preset_runtime_config(root: Path | None = None) -> dict[str, Any]:
    """读取并归一化测试预设运行调度与环境参数。

    返回字段包含（若配置）：
    - round_duration_seconds: int (10-86400)
    - initial_life: int (1-50)
    - max_rounds: int (1-100)
    - max_total_wall_seconds: int (10-604800)
    - max_worker_concurrency: int (1-32)
    - auto_install_tools: bool
    - missing_tools_action: str ("install" / "skip" / "fallback" / "defer")
    - agent_timeout: int (5-3600)
    - cloud_sync: str ("sync" / "local" / "skip")
    - data_sharing: bool
    - detail_mode: str ("full" / "summary")
    - privacy: str ("standard" / "high")
    - commit_policy: str ("round" / "life" / "manual")
    - review_mode: str ("auto" / "interactive")
    """
    raw = _read_preset_raw_payload(root)
    if not raw:
        return {}

    cfg: dict[str, Any] = {}

    def _parse_int(key: str, min_val: int, max_val: int) -> None:
        if key in raw:
            try:
                v = int(raw[key])
                cfg[key] = max(min_val, min(max_val, v))
            except (ValueError, TypeError):
                pass

    def _parse_bool(key: str) -> None:
        if key in raw:
            v = raw[key]
            if isinstance(v, bool):
                cfg[key] = v
            elif isinstance(v, (int, float)):
                cfg[key] = bool(v)
            elif isinstance(v, str):
                cfg[key] = v.strip().lower() in ("true", "1", "yes", "on")

    def _parse_str(key: str, allowed: set[str] | None = None) -> None:
        if key in raw:
            v = raw[key]
            if isinstance(v, str) and v.strip():
                val = v.strip().lower()
                if allowed is None or val in allowed:
                    cfg[key] = val

    # 1. 生命周期与预算调度
    _parse_int("round_duration_seconds", 10, 86400)
    _parse_int("initial_life", 1, 50)
    _parse_int("max_rounds", 1, 100)
    _parse_int("max_total_wall_seconds", 10, 604800)
    _parse_int("max_worker_concurrency", 1, 32)
    _parse_int("agent_timeout", 5, 3600)

    # 2. 工具与 MCP 自愈
    _parse_bool("auto_install_tools")
    _parse_str("missing_tools_action", {"install", "skip", "fallback", "defer"})

    # 3. 云同步与隐私安全
    _parse_str("cloud_sync", {"sync", "local", "skip"})
    _parse_bool("data_sharing")
    _parse_str("detail_mode", {"full", "summary"})
    _parse_str("privacy", {"standard", "high"})

    # 4. 报告与提交
    # DOC-236 C6：报告处置改由 L1 `report_policy` 承接，旧预设键 `round_share`
    # 已随死交互 id 一并删除；`report_policy` **不是**预设键（采集面为
    # `_STARTUP_DEFAULTS` / 作战方案 / `pre --report-policy` / `config.json`），
    # 在此新增解析会变成无人消费的幽灵参数。
    if "commit_policy" in raw:
        cp = str(raw["commit_policy"]).strip().lower()
        if cp in ("auto", "round"):
            cfg["commit_policy"] = "round"
        elif cp in ("life", "manual"):
            cfg["commit_policy"] = cp
    _parse_str("review_mode", {"auto", "interactive"})

    # 保留其他原始字符串与布尔键值供扩展消费
    for k, v in raw.items():
        if k not in cfg and isinstance(k, str) and k.strip():
            cfg[k.strip()] = v

    return cfg


def load_preset_map(root: Path | None = None) -> dict[str, str]:
    """读取并校验预置应答映射表。格式为 {"question_id": "value"}。"""
    payload = _read_preset_raw_payload(root)
    if not payload:
        return {}

    sanitized: dict[str, str] = {}
    for k, v in payload.items():
        if isinstance(k, str) and k.strip():
            k_clean = k.strip()
            if isinstance(v, str) and v.strip():
                sanitized[k_clean] = v.strip()
            elif isinstance(v, bool):
                sanitized[k_clean] = "true" if v else "false"
            elif isinstance(v, (int, float)):
                sanitized[k_clean] = str(v)
    return sanitized


def resolve_preset_answer(
    question_id: str,
    options: Sequence[Any],
    root: Path | None = None,
) -> Any | None:
    """在有效选项中匹配预设答案，返回匹配到的选项对象；无匹配或非法时 fail-closed 返回 None。"""
    preset_map = load_preset_map(root)
    if not preset_map:
        return None

    target_val = preset_map.get(question_id)
    if not target_val:
        return None

    target_lower = target_val.casefold()

    def _opt_fields(opt: Any) -> tuple[str, str, str]:
        val = getattr(opt, "value", None)
        if val is None and isinstance(opt, dict):
            val = opt.get("value")
        val_str = str(val or "")

        label = getattr(opt, "label", None)
        if label is None and isinstance(opt, dict):
            label = opt.get("label")
        elif label is None and isinstance(opt, (tuple, list)) and len(opt) > 0:
            label = opt[0]
        label_str = str(label or "")

        action = getattr(opt, "action", None)
        if action is None and isinstance(opt, dict):
            action = opt.get("action")
        action_str = str(action or "")

        return val_str, label_str, action_str

    # 1. 精确值匹配
    for opt in options:
        val_str, _, action_str = _opt_fields(opt)
        if val_str == target_val or action_str == target_val:
            return opt

    # 2. 语义别名与场景化选项匹配
    for opt in options:
        val_str, label_str, action_str = _opt_fields(opt)
        label_lower = label_str.casefold()

        # Git 初始环境
        if question_id == "git_environment":
            if target_lower in {"init", "repair", "1"}:
                if val_str in {"init", "repair", "1"} or action_str in {"git_init", "repair_git"}:
                    return opt

        # 云端同步
        elif question_id == "cloud_sync":
            if target_lower in {"sync", "cloud", "1", "true"}:
                if val_str in {"sync", "1"} or action_str == "cloud_sync_all" or "同步" in label_lower:
                    return opt
            elif target_lower in {"local", "skip", "2", "false"}:
                if val_str in {"local", "2"} or action_str == "set_cloud_local" or "本地" in label_lower:
                    return opt

        # 启动最终动作
        elif question_id == "startup_action":
            if target_lower in {"start", "begin", "1"}:
                if val_str == "1" or "开始" in label_lower or "手动" in label_lower:
                    return opt
            elif target_lower in {"restart"}:
                if "重启" in label_lower:
                    return opt
            elif target_lower in {"later", "stop", "exit", "2", "3"}:
                if "暂不" in label_lower or "停止" in label_lower or "下次" in label_lower:
                    return opt

        # 策略计划审批
        elif question_id == "strategy_plan":
            if target_lower in {"approve", "approve_and_start", "start", "1"}:
                if val_str in {"approve", "approve_and_start", "1"} or "批准" in label_lower or "开始" in label_lower:
                    return opt

        # 中断会话恢复/重置
        elif question_id == "aborted_session":
            if target_lower in {"reset", "new", "1"}:
                if val_str in {"reset", "1"} or "重置" in label_lower or "新" in label_lower:
                    return opt
            elif target_lower in {"resume", "continue", "2"}:
                if val_str in {"resume", "2"} or "继续" in label_lower:
                    return opt

        # 覆盖率基线初始化
        elif question_id == "coverage_bootstrap":
            if target_lower in {"bootstrap", "init", "1"}:
                if val_str in {"bootstrap", "1"} or "生成" in label_lower or "初始" in label_lower:
                    return opt
            elif target_lower in {"skip", "2"}:
                if val_str in {"skip", "2"} or "跳过" in label_lower:
                    return opt

        # Supervisor MCP 安装/配置
        elif question_id == "supervisor_mcp":
            if target_lower in {"install", "init", "1"}:
                if val_str == "1" or "安装" in label_lower or "初始化" in label_lower:
                    return opt
            elif target_lower in {"manual", "2"}:
                if val_str == "2" or "手动" in label_lower:
                    return opt
            elif target_lower in {"later", "3"}:
                if val_str == "3" or "下次" in label_lower:
                    return opt

        # 降级工具处置 (tooling:batch 或 tooling:<name>)
        elif "tooling" in question_id:
            if target_lower in {"install", "all", "1"}:
                if "安装" in label_lower or val_str == "1":
                    return opt
            elif target_lower in {"skip", "fallback", "2"}:
                if "跳过" in label_lower or "回退" in label_lower or val_str == "2":
                    return opt

        # 代码提交策略
        elif question_id == "commit_policy":
            if target_lower in {"auto", "round", "1"}:
                if val_str in {"round", "auto", "1"} or "自动" in label_lower or "round" in label_lower:
                    return opt
            elif target_lower in {"life"}:
                if val_str == "life" or "life" in label_lower:
                    return opt
            elif target_lower in {"manual", "skip", "2"}:
                if val_str in {"manual", "2"} or "手动" in label_lower or "跳过" in label_lower:
                    return opt

        # 审查模式
        elif question_id == "review_mode":
            if target_lower in {"auto", "1"}:
                if val_str in {"auto", "1"} or "自动" in label_lower:
                    return opt
            elif target_lower in {"interactive", "manual", "2"}:
                if val_str in {"interactive", "manual", "2"} or "交互" in label_lower:
                    return opt

        # 3. 兜底标签包含匹配
        if target_lower in label_lower:
            return opt

    return None


def execute_preset_action(
    question_id: str,
    matched_option: Any,
    root: Path,
) -> bool:
    """对命中的预设应答执行安全受控的测试环境自愈动作。"""
    action = getattr(matched_option, "action", None)
    if action is None and isinstance(matched_option, dict):
        action = matched_option.get("action")

    val = getattr(matched_option, "value", None)
    if val is None and isinstance(matched_option, dict):
        val = matched_option.get("value")

    # 1. Git 初始化与基线自愈（仅限测试环境）
    if action in {"git_init", "repair_git"} or (question_id == "git_environment" and val in {"init", "repair"}):
        try:
            git_dir = root / ".git"
            if not git_dir.exists():
                subprocess.run(
                    ["git", "init"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            head_check = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if head_check.returncode != 0:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                subprocess.run(
                    [
                        "git",
                        "-c", "user.name=BugX Test Harness",
                        "-c", "user.email=test@bugx.local",
                        "commit",
                        "--allow-empty",
                        "-m", "chore: initial test baseline",
                    ],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    # 2. 云同步预设模式执行
    if question_id == "cloud_sync":
        if val in {"local", "skip"}:
            try:
                from core.cloud_sync.cloud_onboard import set_state
                set_state(root, "local")
                return True
            except (ImportError, OSError, ValueError, RuntimeError):
                return False
        elif val in {"sync", "cloud"}:
            try:
                from core.cloud_sync.cloud_onboard import set_state
                set_state(root, "done")
                return True
            except (ImportError, OSError, ValueError, RuntimeError):
                return False

    return True
