#!/usr/bin/env python3
"""launch_bugx.py — bugx 启动协议执行器（真实落地的外部防线门面）。

本模块为外部与测试提供稳定的顶层门面，内部逻辑已按照生命周期阶段模块化拆解至：
  - launch_phases.common: 常量、路径与底层执行工具
  - launch_phases.session_runtime: 状态机管理与 runtime.json 事务
  - launch_phases.startup_gates: 启动门禁、版本核验与交互决策
  - launch_phases.supervisor_bridge: Supervisor 探活、MCP 通道与降级探针
  - launch_phases.round_orchestrator: Round 调度、覆盖率清单、发现结算与报告分享
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

import sys
from pathlib import Path

# Keep the standalone launcher readable on unsupported interpreters.
MIN_PYTHON = (3, 11)
if sys.version_info < MIN_PYTHON:
    try:
        _core = Path(__file__).resolve().parent
        if str(_core) not in sys.path:
            sys.path.insert(0, str(_core))
        from core.infra import python_runtime
        python_runtime.transparent_reexec_if_needed(min_version=MIN_PYTHON, script_path=str(Path(__file__).resolve()))
    except Exception:
        pass
    actual = ".".join(str(part) for part in sys.version_info[:3])
    required = ".".join(str(part) for part in MIN_PYTHON)
    print(
        f"[BugX] 当前 Python {actual} 不受支持；需要 Python >= {required}。"
        "请切换到受支持的 Python 后重试。",
        file=sys.stderr,
    )
    raise SystemExit(2)

# Execution caches disabled
sys.dont_write_bytecode = True

# Ensure core directory and its parent are on sys.path for launch_phases & core.* imports
CORE_DIR = Path(__file__).resolve().parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(CORE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CORE_DIR.parent))

from core.launch_phases import common
from core.launch_phases import session_runtime
from core.launch_phases import startup_gates
from core.launch_phases import supervisor_bridge
from core.launch_phases import lesson_context
from core.launch_phases import lifecycle_reporter
from core.launch_phases import finding_settlement
from core.launch_phases import report_sharing
from core.launch_phases import round_orchestrator
from core.launch_phases import worker_orchestrator
from core.launch_phases import pre_phase
from core.launch_phases import post_phase

_SUBMODULES = [
    common,
    session_runtime,
    startup_gates,
    supervisor_bridge,
    lesson_context,
    lifecycle_reporter,
    finding_settlement,
    report_sharing,
    round_orchestrator,
    worker_orchestrator,
    pre_phase,
    post_phase,
]

# Export all symbols from all submodules to this module's globals
for _mod in _SUBMODULES:
    for _k, _v in _mod.__dict__.items():
        if not _k.startswith("__"):
            globals()[_k] = _v

# If launched without a project layout, align runtime asset paths.
#
# This must delegate to ``common._path_values_for_root`` — the single authority
# for "where do executables live vs. where does state live".  The previous
# hand-maintained copy rebound only AGENT_DIR/VERIFY/RUNTIME*/
# SUPERVISOR_STATE/COVERAGE_EVIDENCE and silently omitted
# ``LESSON_LEDGER_FILE``, ``TOOLS_KB``, ``PREP_VALIDATE``,
# ``SUPERVISOR_SCRIPT`` and ``LESSON_LEDGER_SCRIPT``.  It also pointed state at
# ``CORE_DIR`` while the in-process counterpart
# (``isolated_runtime_scope``) points it at the project root, so the two
# execution lanes disagreed: ``python core/launch_bugx.py pre`` read
# ``core/lesson-ledger.json`` while ``run_in_process`` read
# ``<root>/lesson-ledger.json``.  A copy of a path table is a defect magnet;
# keep exactly one.
if not common.uses_project_layout():
    _project_root = Path.cwd().resolve()
    _aligned = common._path_values_for_root(_project_root)
    for _sub in _SUBMODULES:
        for _key, _value in _aligned.items():
            if hasattr(_sub, _key):
                setattr(_sub, _key, _value)
    for _key, _value in _aligned.items():
        globals()[_key] = _value
    del _key, _value
    del _aligned, _project_root

if __name__ == "__main__":
    sys.exit(round_orchestrator.main(sys.argv[1:]))
