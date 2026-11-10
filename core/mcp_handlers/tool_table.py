#!/usr/bin/env python3
"""core/mcp_handlers/tool_table.py - MCP 工具元数据表与输入 Schema 声明。"""

from __future__ import annotations

import sys
from typing import Any
from core.planning import unit_kind
from core.mcp_handlers import (
    HANDLER_REGISTRY,
    MAX_ARTIFACT_PATH_LENGTH,
    MAX_COMMAND_LENGTH,
    MAX_COVERAGE_PER_CHECKPOINT,
    MAX_EVIDENCE_PER_CHECKPOINT,
    MAX_FINDING_FIELD_LENGTH,
    MAX_FINDING_FINGERPRINT_LENGTH,
    MAX_FINDINGS_PER_CHECKPOINT,
    MAX_MCP_JSON_DEPTH,
    MAX_MCP_JSON_KEYS,
    MAX_MCP_REQUEST_BYTES,
    MAX_UNITS_PER_CLAIM,
)
from core.supervisor import audit_supervisor
from core.supervisor.supervisor_models import (
    VALID_COVERAGE_STATES,
    VALID_EVIDENCE_STATES,
)
LEASE_SECONDS_MAX = audit_supervisor.MAX_LEASE_SECONDS

_HOST_ONLY = frozenset({"host"})
_WORKER_ONLY = frozenset({"worker"})
_SHARED_ROLES = frozenset({"host", "worker"})

UNIT_BUSINESS_PATTERN = f"^{unit_kind.UnitKind.BUSINESS.value}:.+"


def _finding_severity_levels() -> tuple[str, ...]:
    """finding 严重度单一真源（BX-ENGINE-18）；委托给唯一访问器。"""
    try:
        from core.supervisor.supervisor_models import finding_severity_levels

        return finding_severity_levels()
    except ImportError:
        return ("info", "low", "medium", "high", "critical")


_FINDING_SEVERITY_LEVELS = _finding_severity_levels()

def _build_tool_table() -> tuple[tuple[frozenset[str], dict[str, object]], ...]:
    """构建 MCP 工具元数据表 —— 角色归属与 JSON schema 的唯一声明处。

    DES-08：每个工具的角色与其 ``inputSchema`` 在此同处声明，使「新增一个工具」
    只需改这一处；``HOST_TOOLS`` / ``WORKER_TOOLS`` / ``READ_ONLY_TOOLS``
    由本表派生，``HANDLER_REGISTRY`` 由 ``_assert_tool_registry_consistent()``
    在导入期强制校验。
    """
    string_array = {"type": "array", "items": {"type": "string"}}
    finding_schema = {
        "type": "array", "maxItems": 50,
        "items": {
            "type": "object", "required": ["fingerprint"],
            "properties": {
                "fingerprint": {"type": "string", "minLength": 1, "maxLength": 256},
                "id": {"type": "string", "minLength": 1, "maxLength": 256},
                "case_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "title": {"type": "string", "minLength": 1, "maxLength": 4096},
                "root_cause": {"type": "string", "minLength": 1, "maxLength": 4096},
                "severity": {
                    # BX-ENGINE-18：由 finding 严重度单一真源渲染 enum，
                    # 消除「schema 无 enum / 门禁有词表」的漂移面。
                    "type": "string", "maxLength": 32,
                    "enum": list(_FINDING_SEVERITY_LEVELS),
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": {
                    "type": "array", "minItems": 1, "maxItems": 50,
                    "items": {"type": "object", "required": ["path"],
                              "properties": {
                                  "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                                  "line": {"type": "integer", "minimum": 1},
                              }, "additionalProperties": True},
                },
                "reproducible": {"type": "boolean"},
                "reproduction": {"type": "string", "maxLength": 4096},
                # PE-20261005-003 集成：结构化复现三件套（W3），自动裁决据此
                # 认定"真实复现步骤"；非空字符串才入 state。
                "poc_cmd": {"type": "string", "minLength": 1, "maxLength": 4096},
                "poc_input": {"type": "string", "minLength": 1, "maxLength": 4096},
                "poc_negative_input": {"type": "string", "maxLength": 4096},
            },
            # Supervisor performs the shared bounded JSON validation; unknown
            # finding fields remain forward-compatible with agent_eval.
            "additionalProperties": True,
        },
    }
    evidence_schema = {
        "type": "object",
        "maxProperties": MAX_EVIDENCE_PER_CHECKPOINT,
        "additionalProperties": {
            "type": "object",
            "required": [
                "command", "exit_code", "artifact_path", "artifact_sha256",
                "target_fingerprint", "timestamp", "worker_id", "run_id",
                "round_no", "lease_id", "status",
            ],
            "properties": {
                "command": {
                    "type": "string", "minLength": 1,
                    "maxLength": MAX_COMMAND_LENGTH,
                    "description": "真实执行的验证命令，不是计划命令。",
                },
                "exit_code": {
                    "type": "integer",
                    "description": "真实验证命令退出码；status=passed 时必须为 0。",
                },
                "artifact_path": {
                    "type": "string", "minLength": 1,
                    "maxLength": MAX_ARTIFACT_PATH_LENGTH,
                    "description": "当前 claim artifact_root 内、相对仓库根的文件路径。",
                },
                "artifact_sha256": {
                    "type": "string", "pattern": "^[0-9a-fA-F]{64}$",
                    "description": "artifact 文件实际 SHA-256。",
                },
                "target_fingerprint": {"type": "string", "minLength": 1},
                "timestamp": {"type": "number", "exclusiveMinimum": 0},
                "worker_id": {"type": "string", "minLength": 1},
                "run_id": {"type": "string", "minLength": 1},
                "round_no": {"type": "integer", "minimum": 1},
                "lease_id": {"type": "string", "minLength": 1},
                "status": {
                    "type": "string", "enum": sorted(VALID_EVIDENCE_STATES),
                    "description": (
                        "证据状态。covered 是 coverage 值，不是 evidence 值；"
                        "覆盖通过时使用 passed。"
                    ),
                },
                "execution_receipt": {
                    "type": "object",
                    "required": [
                        "schema_version", "execution_id", "worker_id", "lease_id",
                        "command", "exit_code", "artifact_path", "artifact_sha256", "status",
                    ],
                    "properties": {
                        "schema_version": {"type": "integer", "const": 1},
                        "execution_id": {"type": "string", "minLength": 1},
                        "worker_id": {"type": "string", "minLength": 1},
                        "lease_id": {"type": "string", "minLength": 1},
                        "command": {"type": "string", "minLength": 1, "maxLength": MAX_COMMAND_LENGTH},
                        "exit_code": {"type": "integer"},
                        "artifact_path": {"type": "string", "minLength": 1, "maxLength": MAX_ARTIFACT_PATH_LENGTH},
                        "artifact_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                        "status": {"type": "string", "enum": sorted(VALID_EVIDENCE_STATES)},
                    },
                    "additionalProperties": False,
                },
            },
        },
    }
    table: tuple[tuple[frozenset[str], dict[str, object]], ...] = (
        (_HOST_ONLY, {
            "name": "bugx_supervisor_start",
            "description": "启动或幂等复用当前 BugX Session 的本地 Supervisor。",
            "inputSchema": {"type": "object", "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "run_id": {"type": "string"}, "life_score": {"type": "number"},
                "max_rounds": {"type": "integer", "minimum": 1},
                "max_wall_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_round_wall_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_tool_calls": {"type": "integer", "minimum": 0},
                "max_cost_units": {"type": "number", "minimum": 0},
                "max_concurrency": {
                    "type": "integer", "minimum": 1,
                    "description": "省略时按当前机器承载能力动态解析",
                },
                "allow_serial": {"type": "boolean"}, "serial_reason": {"type": "string"},
                "require_confirmation": {"type": "boolean", "const": True, "default": True},
                "project_id": {"type": "string"}, "session_id": {"type": "string"},
                "round_id": {"type": "string"}, "life_id": {"type": "string"},
                "plan_hash": {"type": "string"}, "life_kind": {"type": "string", "enum": ["mine", "fix"]},
                "clean_aborted": {
                    "type": "boolean", "default": False,
                    "description": "无人值守清理 aborted runtime gate 后启动新 run；省略时保持交互阻断",
                },
            }, "required": ["caller_role"]},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_findings",
            "description": "DOC-230 3.8 第 4 项：修复历史 finding 的 severity（脏数据出口）。只改 severity 字段，绝不改动确认状态；值必须命中写侧词表；真实变更写 finding_severity_repaired 审计事件；幂等调用不推进 revision。角色归属以工具表为单一真源（DOC-230 3.6）：handler 与 schema 均为 host-only。",
            "inputSchema": {"type": "object", "required": ["caller_role", "fix_severity", "finding", "severity"], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "fix_severity": {"type": "boolean", "const": True, "description": "当前唯一支持的写操作；显式声明 type 让客户端拿到布尔类型信息（裸 const 会被部分宿主序列化成字符串后拒收，实测 2026-11-09）"},
                "finding": {"type": "string", "minLength": 1, "maxLength": 512, "description": "finding_key / fingerprint / id / finding_id 任一"},
                "severity": {"type": "string", "enum": list(_FINDING_SEVERITY_LEVELS)},
                "reason": {"type": "string", "minLength": 1, "maxLength": 512, "description": "合法值之间改级的理由（C1/A12：severity 参与 Life 奖励与评分判定，静默改级会成为刷分/藏缺陷工具）；修复非法值无需提供"},
                "expected_revision": {"type": "integer", "minimum": 0},
            }},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_reset_budget",
            "description": "预算耗尽时开启新的预算 epoch；保留 findings、coverage 和 history，必要时 force 回收残留租约。角色归属以工具表为单一真源（DOC-230 3.6）：handler 与 schema 均为 host-only。",
            "inputSchema": {"type": "object", "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "expected_revision": {"type": "integer", "minimum": 0},
                "force": {"type": "boolean", "default": False},
                "renew_wall_clock": {"type": "boolean", "default": False},
                "max_rounds": {"type": "integer", "minimum": 1},
                "max_wall_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_round_wall_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_tool_calls": {"type": "integer", "minimum": 0},
                "max_cost_units": {"type": "number", "minimum": 0},
                "max_concurrency": {"type": "integer", "minimum": 1},
                "allow_serial": {"type": "boolean"},
                "serial_reason": {"type": "string"},
                "require_confirmation": {"type": "boolean", "const": True, "default": True},
            }, "required": ["caller_role"]},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_rebind_target",
            "description": "覆盖计划已重建且 workspace fingerprint 发生变化时，显式重绑 Supervisor 目标并保留历史证据。角色归属以工具表为单一真源（DOC-230 3.6）：handler 与 schema 均为 host-only。",
            "inputSchema": {"type": "object", "required": ["caller_role", "reason"], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 512},
                "expected_revision": {"type": "integer", "minimum": 0},
            }},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_sync_finding_decisions",
            "description": "校验活动 lifecycle 的 findings.json 绑定、现有候选 key、确认工件与 remediation 后，将审核决策同步到 Supervisor 权威状态。可显式请求把相同 key 候选的 title/root_cause/location/description 恢复为 Supervisor 原值；fingerprint/id 不匹配仍拒绝。",
            "inputSchema": {"type": "object", "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "expected_revision": {"type": "integer", "minimum": 0},
                "canonicalize_report_identity": {
                    "type": "boolean", "default": False,
                    "description": "仅修复相同 key 候选的非 key 身份字段；未知 key、fingerprint/id 不匹配仍拒绝。",
                },
            }, "required": ["caller_role"]},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_verify_finding",
            "description": "宿主受控验证一条 Finding：引擎执行两次独立正例和固定负对照，记录原始输出与 hash；Worker 自报字段不具备确认资格。",
            "inputSchema": {"type": "object", "required": ["caller_role", "finding", "recipe"], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "finding": {"type": "string", "minLength": 1, "maxLength": 512},
                "recipe": {"type": "object", "description": "预先固定的 positive/negative_control argv、cwd、oracle_reference、oracle_hash 与判据。"},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "expected_revision": {"type": "integer", "minimum": 0},
            }},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_confirm_finding",
            "description": (
                "宿主原子确认或驳回一个 Finding：证据由引擎核验 receipt 派生并自检"
                "（confirmed 必须已有 bugx_supervisor_verify_finding 的 CONFIRMED receipt），"
                "缺料即拒绝，不再写入不可收敛的 confirmation。"
            ),
            "inputSchema": {"type": "object", "required": [
                "caller_role", "finding_id", "decision", "reason",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "finding_id": {"type": "string", "minLength": 1},
                "decision": {"type": "string", "enum": ["confirmed", "rejected"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                "expected_revision": {"type": "integer", "minimum": 0},
                "rejection_class": {
                    "type": "string", "description": "rejected 必填：expected_behavior/environment/tool_failure/duplicate/insufficient_evidence/flaky/not_reproducible。",
                },
                "root_cause_verified": {
                    "type": "boolean", "description": "confirmed 必填 true：宿主对根因的显式断言（引擎记录，不代为生成）。",
                },
                "oracle": {
                    "type": "object",
                    "description": "confirmed 需要 tier（requirement/contract/test/invariant/differential）与 provenance_kind（user-approved/repository-contract/external-spec/independent-implementation/runtime-invariant）；verified 与 independent_from_change 由引擎按 oracle_hash 与证据文件核算，不接受声明。",
                    "properties": {
                        "tier": {"type": "string"},
                        "provenance_kind": {"type": "string"},
                        "reference": {"type": "string", "description": "可选断言：必须与核验 recipe 的 oracle_reference 一致。"},
                    },
                },
                "target_platform": {"type": "string", "description": "可选：目标平台标识，缺省 source。"},
                "remediation": {
                    "type": "object",
                    "description": "可选修复记录：decision=fixed 需 fix_ref（commit_ref/test_ref）+ verification_passed=true；deferred 需 proposal/verification_plan/blast_radius。",
                },
            }},
        }),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_bootstrap_coverage",
            "description": "在用户明确选择‘运行引导创建’后，安全创建当前生命周期覆盖清单；不改写活动 Supervisor。",
            "inputSchema": {"type": "object", "required": ["caller_role", "confirmed"], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "confirmed": {"type": "boolean", "description": "宿主原生单选已返回运行引导创建"},
            }},
        }),
        (_SHARED_ROLES, {"name": "bugx_supervisor_status", "description": "读取状态、回收过期租约并返回下一动作；支持传入 since_revision 进行增量轮询。", "inputSchema": {"type": "object", "properties": {"since_revision": {"type": "integer", "minimum": 0, "description": "增量游标：若传入且与当前 revision 一致，返回最小未变更快照"}}}},),
        (_SHARED_ROLES, {"name": "bugx_supervisor_gate", "description": "检查当前 Life 是否满足并发门禁；支持传入 since_revision 进行增量轮询。", "inputSchema": {"type": "object", "properties": {"since_revision": {"type": "integer", "minimum": 0, "description": "增量游标：若传入且与当前 revision 一致，返回最小未变更快照"}}}},),
        (_SHARED_ROLES, {"name": "bugx_supervisor_coverage", "description": "按 unit_id、status_filter 或 revision 增量读取覆盖状态，避免传输全量 coverage。", "inputSchema": {"type": "object", "properties": {"unit_id": {"type": "string", "minLength": 1}, "status_filter": {"type": "string", "enum": sorted(VALID_COVERAGE_STATES)}, "since_revision": {"type": "integer", "minimum": 0}}}},),
        (_HOST_ONLY, {
            "name": "bugx_supervisor_reopen_finding",
            "description": "宿主有审计理由地重开误报/拒绝记录；仅退回待验证，保留旧证据，不直接确认。",
            "inputSchema": {"type": "object", "required": ["caller_role", "finding_id", "reason", "expected_revision"],
                            "properties": {"caller_role": {"type": "string", "enum": ["host"]},
                                           "finding_id": {"type": "string", "minLength": 1},
                                           "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                                           "expected_revision": {"type": "integer", "minimum": 0}}},
        }),
        (_HOST_ONLY, {"name": "bugx_supervisor_admit_business", "description": "宿主显式授权高风险业务链路提前进入候选队列；不改变覆盖完成判定。", "inputSchema": {"type": "object", "required": ["caller_role", "unit_id", "risk_score", "reason"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "unit_id": {"type": "string", "pattern": UNIT_BUSINESS_PATTERN}, "risk_score": {"type": "number", "minimum": 70, "maximum": 100}, "reason": {"type": "string", "minLength": 1, "maxLength": 512}, "expected_revision": {"type": "integer"}}}},),
        (_WORKER_ONLY, {"name": "bugx_supervisor_claim", "description": "为一个 Worker 领取独立覆盖单元/亲和任务包、资源租约和专属 artifact_root。提交后可用 continue_worker=true 由同一身份继续领取未完成单元，也可在同一 Round 接手 expired/released worker。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id"], "properties": {"caller_role": {"type": "string", "enum": ["worker"]}, "worker_id": {"type": "string"}, "lease_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": LEASE_SECONDS_MAX}, "unit_ids": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "string"}}, "allow_bundle": {"type": "boolean", "default": False}, "continue_worker": {"type": "boolean", "default": False}, "process_pid": {"type": "integer", "minimum": 1, "description": "真实 Worker/执行进程 PID；不能填 Supervisor、MCP 服务或时间戳等占位 PID，否则会被僵尸检测回收。"}, "resource_keys": string_array, "supersede_worker_id": {"type": "string", "minLength": 1}, "recover_expired": {"type": "boolean", "default": False, "description": "PE-20261005-003：同名单 worker 已终态（expired/released）时一键接手——继承 unit_ids/artifact_root 重新计时，返回 recovered_from；宿主随后走 worker_start（无 PID）+ checkpoint 代交成品。"}, "expected_revision": {"type": "integer"}, "worker_role": {"type": "string"}, "preferred_affinity": {"type": "string"}}}},),
        (_WORKER_ONLY, {"name": "bugx_supervisor_worker_start", "description": "确认 Worker 已由宿主真实启动；只有确认后才计入并发重叠。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id"], "properties": {"caller_role": {"type": "string", "enum": ["worker"]}, "worker_id": {"type": "string"}, "provider": {"type": "string", "minLength": 1, "maxLength": 128}, "execution_id": {"type": "string"}, "process_pid": {"type": "integer", "minimum": 1, "description": "真实且当前存活的 Worker/执行进程 PID；不要传 MCP 服务进程或虚构 PID。"}, "expected_revision": {"type": "integer"}}}},),
        (_WORKER_ONLY, {"name": "bugx_supervisor_tool_call", "description": "记录 Worker 已执行的一次工具调用及成本；不会执行命令。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id"], "properties": {"caller_role": {"type": "string", "enum": ["worker"]}, "worker_id": {"type": "string"}, "cost_units": {"type": "number", "minimum": 0}, "tool_name": {"type": "string"}, "expected_revision": {"type": "integer"}}}},),
        (_WORKER_ONLY, {"name": "bugx_supervisor_heartbeat", "description": "续租 Worker lease；不会执行工具或改变生命值。带 PID 的租约必须回显 claim 返回的 heartbeat_nonce。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id"], "properties": {"caller_role": {"type": "string", "enum": ["worker"]}, "worker_id": {"type": "string"}, "lease_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": LEASE_SECONDS_MAX}, "heartbeat_nonce": {"type": "string", "minLength": 1, "maxLength": 128}, "expected_revision": {"type": "integer"}}}},),
        (_WORKER_ONLY, {"name": "bugx_supervisor_checkpoint", "description": "提交 Worker 的发现、覆盖、证据、错误签名和断点手记；支持同任务包内最多 5 个亲和单元批量提交。coverage/evidence/notes 的 key 必须逐字来自本次 claim 的 unit_ids，禁止 summary/finding/knowledge 等自由 key；coverage 使用 covered/unverified 等覆盖状态；evidence.status 必须使用 passed/failed/error/skipped/blocked，不能填 covered。life_delta 必须是数字 0。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id", "checkpoint_id"], "properties": {"caller_role": {"type": "string", "enum": ["worker"]}, "worker_id": {"type": "string"}, "checkpoint_id": {"type": "string", "minLength": 1, "description": "同一有效载荷重试必须复用同一个 checkpoint_id"}, "findings": finding_schema, "coverage": {"type": "object", "maxProperties": 5, "additionalProperties": {"type": "string", "enum": ["pending", "covered", "blocked", "unverified", "partial", "not_applicable", "stale", "removed", "superseded", "corrupted"]}}, "evidence": evidence_schema, "signatures": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1, "maxLength": 512}}, "notes": {"type": "object", "maxProperties": 5, "description": "仅可写当前 claim 的精确 unit_id -> 单元断点手记；禁止 summary/finding/knowledge 等自由 key。", "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 2000}}, "life_delta": {"type": "number", "const": 0, "default": 0, "description": "必须为 JSON 数字 0，不能写成字符串。"}, "expected_revision": {"type": "integer"}}}},),
        (_HOST_ONLY, {"name": "bugx_supervisor_salvage_checkpoint", "description": "DOC-203 D2：宿主代交 state=expired 的 worker claim 已固化 checkpoint（PE-20261005-003 恢复闭环的落账通道）。与 worker 通道同强度校验：五元组绑定（worker_id/run_id/round_no/lease_id/target_fingerprint）+ 工件盘上 sha256 重算 + 工件 mtime 不早于本 claim 领取时刻 + findings 不得携带 confirmation 块 + 每 claim 一次性（salvaged_at）。熔断（worker_attempts_exhausted）不放宽：salvage 只落账已完成证据，不产生新 claim。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id", "checkpoint_id"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "worker_id": {"type": "string"}, "checkpoint_id": {"type": "string", "minLength": 1}, "findings": finding_schema, "coverage": {"type": "object", "maxProperties": 5, "additionalProperties": {"type": "string", "enum": ["pending", "covered", "blocked", "unverified", "partial", "not_applicable", "stale", "removed", "superseded", "corrupted"]}}, "evidence": evidence_schema, "signatures": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1, "maxLength": 512}}, "notes": {"type": "object", "maxProperties": 5, "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 2000}}, "life_delta": {"type": "number", "const": 0, "default": 0}, "expected_revision": {"type": "integer"}}}},),
        (_WORKER_ONLY, {"name": "bugx_supervisor_begin_long_call", "description": "DOC-203 D3：为一次不可分的长工具调用（如 10-20 分钟探针）声明保活窗口。租约推进到 max(当前值, min(now+expected_seconds+60, total_lease_deadline))——绝不缩短现有租约；窗口内不回收，窗口结束照常回收（活性语义保留）。每 claim 最多 5 次；调用同时刷新心跳活性。", "inputSchema": {"type": "object", "required": ["caller_role", "worker_id", "expected_seconds"], "properties": {"caller_role": {"type": "string", "enum": ["worker"]}, "worker_id": {"type": "string"}, "expected_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 1800}, "expected_revision": {"type": "integer"}}}},),
        (_WORKER_ONLY, {
            "name": "bugx_supervisor_run_sandboxed_probe",
            "description": "按已启动 claim 的 lease_id/execution_id 以 shell=False 执行单条 argv，自动保存输出并返回 evidence（含 SHA-256 和 execution_receipt），可原样用于 checkpoint；不自动判定覆盖。这是有界执行，不是文件系统隔离沙箱。",
            "inputSchema": {
                "type": "object",
                "required": ["caller_role", "worker_id", "lease_id", "execution_id", "command"],
                "properties": {
                    "caller_role": {"type": "string", "enum": ["worker"]},
                    "worker_id": {"type": "string"},
                    "lease_id": {"type": "string", "minLength": 1},
                    "execution_id": {"type": "string", "minLength": 1},
                    "command": {
                        "description": "argv 参数数组；不启 shell。每次只执行一条命令，由 MCP 捕获 stdout/stderr。",
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "timeout_seconds": {"type": "number", "minimum": 0.5, "maximum": 60.0, "default": 10.0},
                    "env": {"type": "object"},
                    "expected_revision": {"type": "integer"},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_supervisor_submit_finding",
            "description": "强类型提交漏洞发现与双向差分证据对；强校验 Epoch 租约代数与反事实双向证据。",
            "inputSchema": {
                "type": "object",
                "required": ["caller_role", "worker_id", "finding", "differential_evidence"],
                "properties": {
                    "caller_role": {"type": "string", "enum": ["worker"]},
                    "worker_id": {"type": "string"},
                    "epoch": {"type": "integer", "minimum": 1},
                    "finding": {
                        "type": "object",
                        "required": ["fingerprint", "title", "severity", "root_cause"],
                        "properties": {
                            "fingerprint": {"type": "string", "minLength": 1, "maxLength": 256},
                            "id": {"type": "string"},
                            "title": {"type": "string", "minLength": 1},
                            "severity": {"type": "string", "enum": list(_FINDING_SEVERITY_LEVELS)},
                            "cwe": {"type": "string"},
                            "cvss": {"type": "number", "minimum": 0, "maximum": 10},
                            "root_cause": {"type": "string", "minLength": 1},
                            "remediation": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                    "differential_evidence": {
                        "type": "object",
                        "required": ["baseline_payload", "baseline_response", "exploit_payload", "exploit_response"],
                        "properties": {
                            "baseline_payload": {"type": "string"},
                            "baseline_response": {"type": "string"},
                            "exploit_payload": {"type": "string"},
                            "exploit_response": {"type": "string"},
                            "assertion_differential": {"type": "string"},
                        },
                        "additionalProperties": True,
                    },
                    "expected_revision": {"type": "integer"},
                },
            },
        }),
        (_HOST_ONLY, {"name": "bugx_supervisor_next_round", "description": "在所有 Worker 提交且并发门禁满足后推进下一 Round。gate_failure_mode=report 时门禁未满足返回结构化 GATE_UNSATISFIED（只读、无副作用），供自治宿主程序化决策；缺省使用 report，交互宿主可显式选择 prompt_user。", "inputSchema": {"type": "object", "required": ["caller_role"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "expected_revision": {"type": "integer"}, "gate_failure_mode": {"type": "string", "enum": ["prompt_user", "report"], "default": "report"}}}},),
        (_HOST_ONLY, {"name": "bugx_supervisor_bind_context", "description": "由宿主把控制面绑定到下一 Life 的 Project/Session/Round/plan（不创建新 Round）。", "inputSchema": {"type": "object", "required": ["caller_role"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "project_id": {"type": "string"}, "session_id": {"type": "string"}, "round_id": {"type": "string"}, "life_id": {"type": "string"}, "plan_hash": {"type": "string"}}}},),
        (_HOST_ONLY, {"name": "bugx_supervisor_finish", "description": "由宿主在覆盖、证据和并发门禁完成后结束 Supervisor run。", "inputSchema": {"type": "object", "required": ["caller_role"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "coverage_complete": {"type": ["boolean", "null"]}, "expected_revision": {"type": "integer"}}}},),
        (_HOST_ONLY, {"name": "bugx_supervisor_fail", "description": "由宿主记录不可恢复的 Supervisor 失败并释放租约。", "inputSchema": {"type": "object", "required": ["caller_role", "reason"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "reason": {"type": "string"}, "expected_revision": {"type": "integer"}}}},),
        (_HOST_ONLY, {"name": "bugx_supervisor_recover", "description": "由宿主从主状态或合法备份恢复并返回当前状态；可显式隔离历史 worker 尝试计数，损坏时明确失败。", "inputSchema": {"type": "object", "required": ["caller_role"], "properties": {"caller_role": {"type": "string", "enum": ["host"]}, "reset_worker_attempts": {"type": "boolean", "default": False, "description": "将 expired/released/submitted 历史 claim fence 到旧代际，解除全 Run 尝试硬顶"}}}},),
        (_HOST_ONLY, {
            "name": "graph_start",
            "description": "建立私有项目快照和确定性 Recon→Repro→Review→(Patch)→Check→Report 图；由 Host 自行启动原生 Agent。",
            "inputSchema": {
                "type": "object",
                "required": [
                    "caller_role", "target", "goal", "modifications", "invariants",
                    "ownership_boundaries", "acceptance_criteria", "target_files",
                ],
                "properties": {
                    "caller_role": {"type": "string", "enum": ["host"]},
                    "target": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "goal": {"type": "string", "minLength": 1, "maxLength": 8192},
                    "modifications": {"type": "array", "minItems": 1, "maxItems": 64,
                                      "items": {"type": "string", "minLength": 1, "maxLength": 8192}},
                    "invariants": {"type": "array", "minItems": 1, "maxItems": 64,
                                   "items": {"type": "string", "minLength": 1, "maxLength": 8192}},
                    "ownership_boundaries": {"type": "array", "minItems": 1, "maxItems": 64,
                                             "items": {"type": "string", "minLength": 1, "maxLength": 8192}},
                    "acceptance_criteria": {"type": "array", "minItems": 1, "maxItems": 64,
                                            "items": {"type": "string", "minLength": 1, "maxLength": 8192}},
                    "target_files": {"type": "array", "minItems": 1, "maxItems": 100,
                                     "items": {"type": "string", "minLength": 1, "maxLength": 512}},
                    "include_patch": {"type": "boolean", "default": False},
                    "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
                },
            },
        }),
        (_HOST_ONLY, {
            "name": "graph_next",
            "description": "返回依赖就绪且容量允许的下一 GraphTask，并保留该任务直至 Host 绑定 Agent。",
            "inputSchema": {"type": "object", "required": ["caller_role", "graph_id"], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            }},
        }),
        (_HOST_ONLY, {
            "name": "graph_bind_agent",
            "description": "登记 Host 已启动的原生子 Agent 身份；Review 身份必须不同于 Recon/Repro 生产者。",
            "inputSchema": {"type": "object", "required": [
                "caller_role", "graph_id", "task_id", "native_agent_id",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "task_id": {"type": "string", "minLength": 1},
                "native_agent_id": {"type": "string", "minLength": 1, "maxLength": 256},
            }},
        }),
        (_HOST_ONLY, {
            "name": "graph_cancel",
            "description": "Fence 图中未终结任务并回收私有快照；不声称能从引擎直接停止宿主原生 Agent。",
            "inputSchema": {"type": "object", "required": [
                "caller_role", "graph_id", "reason",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "task_id": {"type": "string"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2048},
            }},
        }),
        (_HOST_ONLY, {
            "name": "graph_finalize",
            "description": "Host 对终态图收尾裁决并固化不可覆盖的最终报告账本。",
            "inputSchema": {"type": "object", "required": [
                "caller_role", "graph_id", "decision", "summary",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["host"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "decision": {"type": "string", "enum": ["accepted", "rejected", "inconclusive"]},
                "summary": {"type": "string", "minLength": 1, "maxLength": 8192},
            }},
        }),
        (_WORKER_ONLY, {
            "name": "graph_context",
            "description": "Worker 依绑定 Task 读取声明的快照文件和父工件；必须匹配 Host 注入的 BUGX_NATIVE_AGENT_ID 与 fencing token。",
            "inputSchema": {"type": "object", "required": [
                "caller_role", "graph_id", "task_id", "native_agent_id", "fencing_token",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["worker"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "task_id": {"type": "string", "minLength": 1},
                "native_agent_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "fencing_token": {"type": "integer", "minimum": 1},
            }},
        }),
        (_WORKER_ONLY, {
            "name": "graph_run_check",
            "description": "Worker 提交受限 CheckPlan；引擎只运行 tests/ 下的 test_*.py unittest 并返回 Observation。",
            "inputSchema": {"type": "object", "required": [
                "caller_role", "graph_id", "task_id", "native_agent_id", "fencing_token", "plan",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["worker"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "task_id": {"type": "string", "minLength": 1},
                "native_agent_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "fencing_token": {"type": "integer", "minimum": 1},
                "plan": {"type": "object", "required": ["test_files"], "additionalProperties": False,
                         "properties": {
                             "test_files": {"type": "array", "minItems": 1, "maxItems": 20,
                                            "items": {"type": "string", "minLength": 1, "maxLength": 512}},
                             "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60},
                             "plan_id": {"type": "string", "maxLength": 128},
                         }},
            }},
        }),
        (_WORKER_ONLY, {
            "name": "graph_checkpoint",
            "description": "Worker 提交 bounded artifact envelope 与 SHA-256 内容寻址附件；每阶段最多 3 次 attempt。",
            "inputSchema": {"type": "object", "required": [
                "caller_role", "graph_id", "task_id", "native_agent_id", "fencing_token", "envelope",
            ], "properties": {
                "caller_role": {"type": "string", "enum": ["worker"]},
                "graph_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "task_id": {"type": "string", "minLength": 1},
                "native_agent_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "fencing_token": {"type": "integer", "minimum": 1},
                "envelope": {"type": "object", "required": ["outcome", "summary"],
                             "properties": {
                                 "outcome": {"type": "string", "enum": ["complete", "needs_revision", "failed"]},
                                 "summary": {"type": "string", "minLength": 1, "maxLength": 8192},
                                 "reason": {"type": "string", "maxLength": 8192},
                                 "context": {"type": "object", "maxProperties": 5000},
                                 "retry_stage": {"type": "string", "enum": ["Recon", "Repro"]},
                                 "result": {"type": "object"},
                             }},
                "attachments": {"type": "array", "maxItems": 50, "items": {
                    "type": "object", "required": ["filename", "sha256", "content_base64"],
                    "properties": {
                        "filename": {"type": "string", "minLength": 1, "maxLength": 512},
                        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        "content_base64": {"type": "string"},
                    },
                }},
            }},
        }),
        (_SHARED_ROLES, {"name": "bugx_knowledge_route", "description": "按项目分类、阶段和错误签名自动判断是否需要检索；高风险时复用有界错题/incident 检索，低风险不检索，不注入历史正文。", "inputSchema": {"type": "object", "properties": {"trigger": {"type": "string", "enum": ["preflight", "strategy", "finding", "failure"], "default": "preflight"}, "query": {"type": "string", "maxLength": 512}, "category": {"type": "string", "maxLength": 96}, "signatures": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1, "maxLength": 512}}, "context": {"type": "object", "maxProperties": 12}}}},),
        (_SHARED_ROLES, {"name": "bugx_harness_status", "description": "读取当前项目会话的共享验证 harness 注册表；只读，不安装依赖、不执行命令。", "inputSchema": {"type": "object", "properties": {}}},),
        (_SHARED_ROLES, {"name": "bugx_oracle_suggest", "description": "按单元/分类读取或提取有界 repository-contract Oracle 候选；结果仅供参考，不能直接确认 Finding。", "inputSchema": {"type": "object", "properties": {"unit": {"type": "string", "maxLength": 512}, "query": {"type": "string", "maxLength": 512}, "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5}}}},),
        (_SHARED_ROLES, {"name": "bugx_lesson_retrieve", "description": "按当前分类/查询按需检索最多 3 张错题经验卡（只读，600 token 上限）；只用于快速碰撞，不是提示词注入。", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 3}, "token_budget": {"type": "integer", "minimum": 100, "maximum": 600}}}},),
        (_SHARED_ROLES, {"name": "bugx_tool_matrix", "description": "读取实测「问题类别 → 工具」命中矩阵，校正场景选型（只读）。", "inputSchema": {"type": "object", "properties": {}}},),
        (_SHARED_ROLES, {"name": "bugx_incident_hints", "description": "有真实错误签名时按需检索最多 2 个 incident 做快速碰撞（只读、有界，不注入完整卡片）。", "inputSchema": {"type": "object", "properties": {"signatures": string_array, "limit": {"type": "integer", "minimum": 1, "maximum": 2}}}},),
        (_SHARED_ROLES, {"name": "bugx_request_token_renewal", "description": "针对复杂长 PoC 或深度调用链申请动态 Token 续期（单次 +400 Tokens，单生命周期最多 2 次，上限 2400 Tokens）。", "inputSchema": {"type": "object", "required": ["reason"], "properties": {"reason": {"type": "string", "maxLength": 128}, "needed": {"type": "integer", "minimum": 100, "maximum": 800, "default": 400}}}},),
        (_SHARED_ROLES, {"name": "bugx_efficiency_card", "description": "读取最近几轮的效率/校准卡片（time/credited、注入卡片利用率；只读）。", "inputSchema": {"type": "object", "properties": {}}},),
        (_SHARED_ROLES, {
            "name": "bugx_get_stage_harness",
            "description": "获取 BugX 2.0 阶段专精系统提示词与规则切片（按阶段按需获取，支持 gate/explore/mutate/repair/l0 及规则切片）。",
            "inputSchema": {
                "type": "object",
                "required": ["stage"],
                "properties": {
                    "stage": {
                        "type": "string",
                        "description": "阶段标识符：gate / explore / mutate / repair / l0",
                        "enum": ["gate", "explore", "mutate", "repair", "l0"],
                    },
                    "unit_id": {
                        "type": "string",
                        "description": "可选：当前认领的覆盖单元 ID，用于自动提取匹配的 L2 规则片段",
                    },
                    "rule": {
                        "type": "string",
                        "description": "可选：按需加载的 L2 专项规则库 (concurrency / memory_leak / prompt_injection / anti_patterns)",
                        "enum": ["concurrency", "memory_leak", "prompt_injection", "anti_patterns"],
                    },
                    "context": {
                        "type": "object",
                        "description": "可选：动态注入的上下文事实键值对",
                    },
                    "source_code": {
                        "type": "string",
                        "description": "可选：单元源码片段，供 AST 静态推断更精准的 L2 专项规则切片",
                    },
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_distill_evidence",
            "description": "结构化长堆栈与 Fuzz 输出证据蒸馏器；抽取关键异常、行号定位与断言差异至 ≤15 行，支持文本或 JSON 格式（只读）。",
            "inputSchema": {
                "type": "object",
                "required": ["raw_output"],
                "properties": {
                    "raw_output": {
                        "type": "string",
                        "description": "待蒸馏的原始终端输出、Traceback 或 Fuzz 日志",
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 15,
                        "description": "最大输出行数，默认 15",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "default": "text",
                        "description": "输出格式：text（纯文本提要）或 json（结构化字典）",
                    },
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_inspect_source",
            "description": "安全提取源文件切片与代码视窗（具备越界自愈、UTF-8 容错、沙箱防逃逸与透明符号自愈切片路由）。",
            "inputSchema": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "目标文件相对或绝对路径"},
                    "symbol": {"type": "string", "description": "可选符号名，指定时自动路由至高密度语法自愈切片"},
                    "method": {"type": "string", "description": "可选方法名别名"},
                    "kind": {"type": "string", "description": "可选种类过滤 (function/method/class)"},
                    "start_line": {"type": "integer", "minimum": 1, "default": 1, "description": "起始行号（1-based）"},
                    "end_line": {"type": "integer", "minimum": 1, "default": 200, "description": "结束行号（1-based）"},
                    "numbered": {"type": "boolean", "default": True, "description": "是否返回带行号的文本"},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 500, "description": "单次最大行数上限"},
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_scheduling_telemetry",
            "description": "读取当前 Supervisor 调度遥测指标与并发利用率看板（AIMD 容量、排队/执行时延、抢占与活跃 Worker）。",
            "inputSchema": {"type": "object", "properties": {}},
        }),
        (_SHARED_ROLES, {
            "name": "bugx_global_telemetry",
            "description": "查询跨项目的全局时间与 Token 计量治理看板、项目 ROI 排行及每日消耗趋势（DOC-049）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 365,
                        "default": 30,
                        "description": "统计时间窗口天数（默认 30 天）",
                    },
                    "project_filter": {
                        "type": "string",
                        "description": "可选项目名称过滤关键词",
                    },
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_retrieval_metrics",
            "description": "查询与记录混合检索与结构化切片管线的 P50/P99 时延与 Token 密度遥测指标（DOC-234）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "description": "可选待上报检索原语名称：rrf, slice, memory, ego",
                    },
                    "duration_ms": {
                        "type": "number",
                        "description": "执行耗时（毫秒）",
                    },
                    "tokens": {
                        "type": "number",
                        "description": "上下文估算 Token 数",
                    },
                    "output_bytes": {
                        "type": "number",
                        "description": "产物字节数",
                    },
                    "samples": {
                        "type": "array",
                        "description": "批量上报样本数组",
                        "items": {"type": "object"},
                    },
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_fuzz_concurrency",
            "description": "并发竞态与时序死锁专项扰动工具：注入微秒级上下文切换抖动并检测数据竞态或死锁。",
            "inputSchema": {
                "type": "object",
                "required": ["target_cmd"],
                "properties": {
                    "target_cmd": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "要并发压测的命令 argv 数组（如 [\"python3\",\"-m\",\"unittest\",\"tests/test_x.py\"]）；不接受字符串命令",
                    },
                    "threads": {"type": "integer", "minimum": 1, "maximum": 32, "default": 8},
                    "iterations": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
                    "timeout": {"type": "number", "minimum": 0.5, "maximum": 30.0, "default": 2.0},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_fuzz_stateful",
            "description": "状态机反常序突变推演工具：自动生成跳步、重入、撤销后执行变异调用序列，用于挖掘业务逻辑漏洞。",
            "inputSchema": {
                "type": "object",
                "required": ["transitions"],
                "properties": {
                    "transitions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["action", "from_state", "to_state"],
                            "properties": {
                                "action": {"type": "string"},
                                "from_state": {"type": "string"},
                                "to_state": {"type": "string"},
                                "requires_auth": {"type": "boolean"},
                            },
                        },
                    },
                    "max_length": {"type": "integer", "minimum": 2, "maximum": 6, "default": 4},
                    "max_sequences": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_verify_differential_poc",
            "description": "确定性沙盒双跑差分验证器：在两个独立临时沙盒中双跑 PoC 并清洗地址/临时目录噪音，裁决 CONFIRMED_DETERMINISTIC / FLAKY_JITTER / FALSE_POSITIVE。",
            "inputSchema": {
                "type": "object",
                "required": ["command_template", "poc_input"],
                "properties": {
                    "command_template": {"type": "string", "description": "命令模板（含 {input} 占位符）"},
                    "poc_input": {
                        "description": "PoC 输入数据内容、项目内文件路径，或 JSON 对象/数组；结构化值按 JSON 文件传给命令",
                        "anyOf": [
                            {"type": "string", "minLength": 1},
                            {"type": "object"},
                            {"type": "array"},
                        ],
                    },
                    "timeout": {"type": "number", "minimum": 1.0, "maximum": 30.0, "default": 10.0},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_ingest_coverage",
            "description": "真实测试覆盖率报告回填工具：解析测试套件生成的 lcov/cobertura/coveragepy 报告，自动将对应物理单元与端点标记为已覆盖。",
            "inputSchema": {
                "type": "object",
                "required": ["report_path"],
                "properties": {
                    "report_path": {"type": "string", "description": "测试覆盖报告文件相对或绝对路径"},
                    "format_hint": {"type": "string", "enum": ["lcov", "jacoco", "coveragepy", "generic"], "description": "可选指定格式"},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_inspect_mobile_ui",
            "description": "移动端 UI 几何与无障碍 Oracle 质检工具：执行 Overlap、Clipping、Touch Target、Safe Area 等机械判定并产出 Failure Pack。",
            "inputSchema": {
                "type": "object",
                "required": ["dump_path_or_content"],
                "properties": {
                    "dump_path_or_content": {"type": "string", "description": "UI 视图树 XML 或 WDA JSON 路径/文本内容"},
                    "is_ios": {"type": "boolean", "default": False},
                    "density": {"type": "number", "default": 1.0},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_fuzz_input",
            "description": "目标程序变异模糊测试工具：对 CLI、解析器、端点注入变异输入序列，并检测崩溃、挂起与异常签名。",
            "inputSchema": {
                "type": "object",
                "required": ["cmd"],
                "properties": {
                    "cmd": {"type": "string", "description": "目标执行命令（可含 {input} 占位符或作为 stdin 输入源）"},
                    "input_content": {"type": "string", "description": "初始变异种子内容（纯文本，不再被当作文件路径）"},
                    "input_path": {"type": "string", "description": "项目内的种子文件相对路径（必须位于项目根目录内，不含符号链接）；与 input_content 互斥"},
                    "iterations": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
                    "timeout": {"type": "number", "minimum": 0.1, "maximum": 30.0, "default": 2.0},
                    "language": {"type": "string", "enum": ["python", "go", "java", "generic"], "description": "语言异常规则调度"},
                    "seed": {"type": "integer", "description": "随机数种子（用于确定性重放）"},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_minimize_repro",
            "description": "PoC 失败样本分层 delta-debugging 最小化工具：对崩溃触发样本做行级/语法/字节块切除，提取最小复现证据。",
            "inputSchema": {
                "type": "object",
                "required": ["cmd"],
                "properties": {
                    "cmd": {"type": "string", "description": "复现验证命令（含 {input} 占位符）"},
                    "input_data": {"type": "string", "description": "待切除最小化的崩溃输入文本（纯内容，不再被当作文件路径）"},
                    "input_path": {"type": "string", "description": "项目内的样本文件相对路径（必须位于项目根目录内，不含符号链接）；与 input_data 二选一"},
                    "timeout": {"type": "number", "minimum": 0.5, "maximum": 30.0, "default": 5.0},
                    "hierarchical": {"type": "boolean", "default": True, "description": "是否开启行级优先两阶段分层切除"},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_query_behavior_graph",
            "description": "行为图谱与逻辑异常推断工具：查询系统调用拓扑并自动推断无鉴权暴露端点、孤儿死事件与跨模块配置分歧。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_filter": {"type": "string", "description": "按文件相对路径过滤图谱节点"},
                    "include_anomalies": {"type": "boolean", "default": True, "description": "是否同时执行行为异常拓扑推断"},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_audit_web_conformance",
            "description": "Web 标准合规与契约质检工具：静态扫描项目前端与 API，检测 HTTP 动词安全、CORS/CSP 头、IDOR 边界与分页参数合规。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}, "description": "指定扫描的目标文件相对路径列表"},
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_mining_explore",
            "description": "全息可挖掘功能主体勘测工具：AST 扫描代码库，自动识别 HTTP 路由、CLI 分支、并发临界区、反序列化与状态流转，映射威胁模型与推荐 Oracle。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subpath": {"type": "string", "description": "相对项目根目录的子路径或目标文件过滤"},
                    "risk_level": {
                        "type": "string",
                        "enum": ["ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"],
                        "default": "ALL",
                        "description": "过滤指定风险等级的功能主体",
                    },
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_mining_audit",
            "description": "漏洞挖掘方案与用例四维量化审计工具：从准确性、完整性、有效性、全面性四维度 100 分制量化评估挖掘规划，输出等级、门禁结论与违规清单。DEGRADED 不等于通过：返回 status=blocked 时必须先取得用户决策，再显式传 allow_degraded=true 重审。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan": {"type": "object", "description": "内存中待审计的挖掘方案 JSON 对象"},
                    "plan_file": {"type": "string", "description": "相对项目根目录的方案文件路径（如 .bugx/campaign.json）"},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 100, "default": 80.0, "description": "及格线分数"},
                    "strict": {"type": "boolean", "default": False, "description": "是否开启严格阻断模式（有任何硬违规即判定失败）"},
                    "allow_degraded": {"type": "boolean", "default": False, "description": "是否显式接受降级（DEGRADED）方案；默认 false，此时降级方案判为 blocked"},
                },
            },
        }),
        (_HOST_ONLY, {
            "name": "bugx_mining_harden",
            "description": "挖掘方案自动加固与 Oracle 强化工具：自动注入代码库中遗漏的高危功能主体，补全 DAG 依赖并将弱断言升级为深度状态不变式。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan": {"type": "object", "description": "内存中待加固的原始方案 JSON 对象"},
                    "plan_file": {"type": "string", "description": "相对项目根目录的输入方案文件路径"},
                    "output_file": {"type": "string", "description": "加固后方案另存为的项目相对路径（可选）"},
                },
            },
        }),
        (_WORKER_ONLY, {
            "name": "bugx_test_verify",
            "description": "测试用例杀伤力变异沙箱验证工具：在受控隔离沙箱中对目标代码注入 6 类 AST 语义变异，实测测试用例真实击杀率 (Mutation Score) 并评级。",
            "inputSchema": {
                "type": "object",
                "required": ["target_path", "test_target"],
                "properties": {
                    "target_path": {"type": "string", "description": "被测 Python 源码文件相对路径"},
                    "test_target": {"type": "string", "description": "测试命令（如 python3 -m unittest tests/test_calc.py）或测试文件路径"},
                    "max_mutants": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20, "description": "最大变异体评估上限"},
                    "timeout_sec": {"type": "number", "minimum": 0.5, "maximum": 10.0, "default": 2.0, "description": "单个变异体看门狗超时（秒）"},
                    "operators": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ROR", "AOR", "LCR", "BCR", "EOR", "SVR"]},
                        "description": "启用的变异算子白名单过滤",
                    },
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_test_mutate",
            "description": "AST 语义变异体只读预览工具：针对目标 Python 文件生成可能的微观逻辑变异列表，供编写高杀伤力断言参考（不执行测试命令）。",
            "inputSchema": {
                "type": "object",
                "required": ["target_path"],
                "properties": {
                    "target_path": {"type": "string", "description": "目标 Python 源码文件相对路径"},
                    "max_mutants": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30, "description": "最大生成变异体上限"},
                    "operators": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["ROR", "AOR", "LCR", "BCR", "EOR", "SVR"]},
                        "description": "启用的变异算子白名单过滤",
                    },
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_retrieve_slice",
            "description": "结构化检索代码块切片（支持按语言、符号名、种类有界提取与语法自愈）。",
            "inputSchema": {
                "type": "object",
                "required": ["target"],
                "properties": {
                    "target": {"type": "string", "description": "目标源文件相对路径"},
                    "symbol": {"type": "string", "description": "可选符号名过滤（如函数或类名）"},
                    "kind": {"type": "string", "enum": ["function", "method", "class", "type", "module", "fragment"], "description": "可选切片种类过滤"},
                    "to_executable": {"type": "boolean", "default": False, "description": "是否对切片生成自愈后的可直接解析执行视图"},
                    "include_context": {"type": "boolean", "default": False, "description": "是否附带文件导入头与局部拓扑的阶梯渐进上下文"},
                    "max_slices": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "最大返回切片数"},
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_extract_ego_subgraph",
            "description": "提取指定中心节点的 k-hop 局部有界调用/依赖拓扑子图（防上下文爆炸）。",
            "inputSchema": {
                "type": "object",
                "required": ["center_node"],
                "properties": {
                    "center_node": {"type": "string", "description": "中心节点标识（如符号名或节点 ID）"},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 2, "default": 1, "description": "跳数深度（1 或 2）"},
                    "direction": {"type": "string", "enum": ["both", "inbound", "outbound"], "default": "both", "description": "遍历方向"},
                    "graph": {"type": "object", "description": "可选自定义子图映射"},
                },
            },
        }),
        (_SHARED_ROLES, {
            "name": "bugx_memory_query",
            "description": "在隔离命名空间内执行确定性记忆检索与经验知识召回（FTS5 BM25 排序与敏感凭据物理隔离）。",
            "inputSchema": {
                "type": "object",
                "required": ["namespace", "query"],
                "properties": {
                    "namespace": {"type": "string", "description": "记忆命名空间（如 repo:default 或 framework:lessons）"},
                    "query": {"type": "string", "description": "检索文本"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "最大召回条目数"},
                    "db_path": {"type": "string", "description": "可选指定数据库路径，默认使用会话内 SQLite"},
                },
            },
        }),
    )
    # Keep the large checkpoint schema readable above while making the
    # transport retry contract explicit in one place.  The engine accepts an
    # omitted id for direct legacy callers; MCP does not, because a lost
    # response must be replayable without duplicating findings.
    # DES-08：该补丁在**建表时一次性应用**，故 ``_TOOL_TABLE`` 已是终态。
    # 旧实现把 definitions 建在函数内、每次调用都重新追加一次描述后缀。
    for _roles, definition in table:
        if definition["name"] == "bugx_supervisor_checkpoint":
            schema = definition["inputSchema"]
            required = list(schema.get("required", []))
            if "checkpoint_id" not in required:
                required.append("checkpoint_id")
            schema["required"] = required
            schema["properties"]["checkpoint_id"] = {
                "type": "string", "minLength": 1, "maxLength": 128,
            }
            definition["description"] = (
                str(definition["description"])
                + " 重试必须复用同一 checkpoint_id。"
            )
            break
    return table


_TOOL_TABLE = _build_tool_table()
_TOOL_ROLES = {definition["name"]: roles for roles, definition in _TOOL_TABLE}

# 三个角色集合由单一真源派生（原先为三份手工维护的字面量）。
HOST_TOOLS = frozenset(d["name"] for roles, d in _TOOL_TABLE if roles == _HOST_ONLY)
WORKER_TOOLS = frozenset(d["name"] for roles, d in _TOOL_TABLE if roles == _WORKER_ONLY)
READ_ONLY_TOOLS = frozenset(d["name"] for roles, d in _TOOL_TABLE if roles == _SHARED_ROLES)


def _get_active_tool_table() -> Any:
    mod = sys.modules.get("core.skills.bugx_mcp_server")
    table = getattr(mod, "_TOOL_TABLE", None) if mod is not None else None
    if table is not None:
        return table
    return _TOOL_TABLE


def _assert_tool_registry_consistent() -> None:
    """DES-08 漂移守卫：定义表与 handler 分发表必须**逐名一致**。

    导入期 fail-closed 校验：任一侧新增/改名而未同步另一侧时模块直接拒绝加载，
    而不是静默提供一个「被宣告但无法分发」或「能分发但未被宣告」的工具。
    """
    table = _get_active_tool_table()
    defined = frozenset(definition["name"] for _roles, definition in table)
    handlers = frozenset(HANDLER_REGISTRY)
    if defined != handlers:
        missing = sorted(defined - handlers)
        orphan = sorted(handlers - defined)
        raise RuntimeError(
            "MCP 工具元数据与 handler 注册表不一致："
            f"缺少 handler 的定义={missing}；缺少定义的 handler={orphan}"
        )


_assert_tool_registry_consistent()


_WORKER_CORE_TOOL_NAMES = frozenset({
    "bugx_supervisor_claim",
    "bugx_supervisor_worker_start",
    "bugx_supervisor_heartbeat",
    "bugx_supervisor_begin_long_call",
    "bugx_inspect_source",
    "bugx_supervisor_run_sandboxed_probe",
    "bugx_test_verify",
    "bugx_supervisor_submit_finding",
    "bugx_supervisor_checkpoint",
    "bugx_supervisor_status",
})


def _tool_definitions(role: str | None = None) -> list[dict[str, object]]:
    """返回 MCP 工具定义列表，可按角色过滤。

    DES-08：本函数渲染 ``_TOOL_TABLE``。
    DOC-241 A5：Worker 角色严格仅暴露 9 个核心原子工具（<= 10），
    消除 32 个冗余工具对大模型 Context 的浪费与幻觉。
    """
    table = _get_active_tool_table()
    if role is None:
        return [definition for _roles, definition in table]
    if role == "worker":
        return [definition for roles, definition in table if definition["name"] in _WORKER_CORE_TOOL_NAMES]
    return [definition for roles, definition in table if role in roles]


