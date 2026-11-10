#!/usr/bin/env python3
"""core/stateful_fuzzer.py - 基于业务行为图谱的状态机序列突变推演器。

从业务调用图谱/操作字典中识别状态机跃迁，自动生成反常序调用序列
（跳步、重入、撤销后执行、未授权重放），并配合不变量检查器捕获业务逻辑漏洞。
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

import argparse
from collections import defaultdict
import itertools
import json
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_CORE_DIR = Path(__file__).resolve().parents[1]
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


@dataclass(frozen=True)
class Transition:
    action: str
    from_state: str
    to_state: str
    requires_auth: bool = False


class ModelBasedSequenceGenerator:
    """基于状态跃迁图拓扑的模型化定向攻击序列生成器。"""

    def __init__(self, transitions: Sequence[Transition]):
        self.transitions = list(transitions)
        self.actions = [t.action for t in transitions]
        self.state_graph: dict[str, list[Transition]] = defaultdict(list)
        self.all_states: set[str] = set()
        self.terminal_states: set[str] = set()

        for t in self.transitions:
            self.state_graph[t.from_state].append(t)
            self.all_states.add(t.from_state)
            self.all_states.add(t.to_state)

        terminal_names = {
            "cancelled", "canceled", "refunded", "closed", "deleted",
            "failed", "rejected", "finished", "terminal",
        }
        for s in self.all_states:
            if not self.state_graph.get(s) or s.lower() in terminal_names:
                self.terminal_states.add(s)

    def generate_attack_sequences(self, max_sequences: int = 20) -> list[dict[str, Any]]:
        """生成模型化攻击序列：
        1. auth_bypass: 未授权/越权跳步序列
        2. rollback_leak: 终结态后继续执行业务操作
        3. cycle_exhaustion: 跃迁回路重入探测
        """
        attacks: list[dict[str, Any]] = []

        # 1. Auth bypass 序列
        for t in self.transitions:
            if t.requires_auth:
                attacks.append({
                    "type": "auth_bypass",
                    "sequence": [t.action],
                    "target_action": t.action,
                    "intent": f"未满足前置授权状态直接调用特权动作: {t.action}",
                })
                other_actions = [o.action for o in self.transitions if o.action != t.action]
                if other_actions:
                    attacks.append({
                        "type": "auth_bypass_skip_step",
                        "sequence": [other_actions[0], t.action],
                        "target_action": t.action,
                        "intent": f"在非特权动作 {other_actions[0]} 后越权跃迁特权动作: {t.action}",
                    })

        # 2. Rollback leak 终态后操作序列
        for t in self.transitions:
            if t.to_state in self.terminal_states:
                active_actions = [
                    o.action for o in self.transitions
                    if o.to_state not in self.terminal_states and o.action != t.action
                ]
                for act in active_actions[:3]:
                    attacks.append({
                        "type": "rollback_leak",
                        "sequence": [t.action, act],
                        "terminal_state": t.to_state,
                        "intent": f"进入终态 {t.to_state} 后再次调用业务动作 {act}（测试状态机闭锁）",
                    })

        # 3. Cycle exhaustion 循环重入序列
        for t1 in self.transitions:
            for t2 in self.state_graph.get(t1.to_state, ()):
                if t2.to_state == t1.from_state and t1.action != t2.action:
                    attacks.append({
                        "type": "cycle_reentrancy",
                        "sequence": [t1.action, t2.action, t1.action, t2.action],
                        "cycle": f"{t1.from_state} <-> {t1.to_state}",
                        "intent": f"对状态回路 {t1.from_state} <-> {t1.to_state} 进行深度重入探测",
                    })

        return attacks[:max_sequences]


class StatefulSequenceGenerator:
    """业务状态机序列生成与反常序变异器。"""

    def __init__(self, transitions: Sequence[Transition] | None = None):
        self.transitions = list(transitions or [])

    def add_transition(self, action: str, from_state: str, to_state: str, requires_auth: bool = False) -> None:
        self.transitions.append(Transition(action, from_state, to_state, requires_auth))

    def generate_anomalous_sequences(self, max_length: int = 4) -> list[list[str]]:
        """生成反常序调用序列：
        1. 跳步（Skip Step）：跳过中间必要状态直接跃迁终态
        2. 重入（Re-entrancy / Duplicate）：同一个操作连续调用
        3. 逆序/撤销后执行（Post-Cancellation）：进入终结/取消态后再次调用业务动作
        """
        all_actions = [t.action for t in self.transitions]
        unique_actions = sorted(set(all_actions))
        anomalies: list[list[str]] = []

        # 1. 连续重复调用（Duplicate Execution）
        for act in unique_actions:
            anomalies.append([act, act])

        # 2. 逆序与乱序配对
        if len(unique_actions) >= 2:
            for seq in itertools.permutations(unique_actions, min(len(unique_actions), max_length)):
                anomalies.append(list(seq))

        # 3. 乱序随机抽样（保持固定随机种子以确保确定性复现）
        rng = random.Random(42)
        for _ in range(20):
            seq_len = rng.randint(2, max_length)
            anomalies.append([rng.choice(unique_actions) for _ in range(seq_len)])

        # 去重保持顺序
        seen = set()
        deduped = []
        for seq in anomalies:
            key = tuple(seq)
            if key not in seen:
                seen.add(key)
                deduped.append(seq)
        return deduped


class StatefulOracle:
    """状态机推演与不变量校验执行器。"""

    def __init__(self, sequence_generator: StatefulSequenceGenerator):
        self.generator = sequence_generator

    def test_target(
        self,
        dispatch_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
        initial_state_factory: Callable[[], dict[str, Any]],
        invariant_checker: Callable[[list[str], dict[str, Any]], tuple[bool, str]],
        *,
        max_sequences: int = 50,
    ) -> dict[str, Any]:
        """驱动目标分发函数执行反常序序列，并校验状态不变量。"""
        sequences = self.generator.generate_anomalous_sequences()
        for idx, seq in enumerate(sequences[:max_sequences], 1):
            ctx = initial_state_factory()
            execution_log = []
            for action in seq:
                res = dispatch_fn(action, ctx)
                execution_log.append({"action": action, "result": res})
                ok, reason = invariant_checker(seq, ctx)
                if not ok:
                    return {
                        "vulnerability_found": True,
                        "type": "STATE_MACHINE_INVARIANT_VIOLATION",
                        "failing_sequence": seq,
                        "broken_at_step": action,
                        "violation_reason": reason,
                        "execution_log": execution_log,
                        "final_state": ctx,
                    }

        return {
            "vulnerability_found": False,
            "sequences_tested": min(len(sequences), max_sequences),
            "details": "所有反常序测试序列均通过业务不变量校验",
        }


def fuzz_transitions(
    transitions_data: list[dict[str, Any]],
    *,
    max_length: int = 4,
    max_sequences: int = 20,
) -> dict[str, Any]:
    """生成并分析针对指定状态跃迁字典的反常序变异序列及模型化攻击序列。"""
    transitions = [
        Transition(
            action=str(t.get("action", "")),
            from_state=str(t.get("from_state", "")),
            to_state=str(t.get("to_state", "")),
            requires_auth=bool(t.get("requires_auth", False)),
        )
        for t in transitions_data
        if t.get("action")
    ]
    gen = StatefulSequenceGenerator(transitions)
    anomalous = gen.generate_anomalous_sequences(max_length=max_length)

    model_gen = ModelBasedSequenceGenerator(transitions)
    model_attacks = model_gen.generate_attack_sequences(max_sequences=max_sequences)

    # 优先将模型攻击序列置于推荐序列前部
    attack_seqs = [a["sequence"] for a in model_attacks]
    combined_seqs = []
    seen = set()
    for seq in attack_seqs + anomalous:
        key = tuple(seq)
        if key not in seen:
            seen.add(key)
            combined_seqs.append(seq)

    return {
        "transitions_count": len(transitions),
        "total_anomalies_generated": len(combined_seqs),
        "recommended_test_sequences": combined_seqs[:max_sequences],
        "model_attacks": model_attacks,
        "terminal_states": sorted(list(model_gen.terminal_states)),
        "categories": {
            "model_attacks": attack_seqs,
            "duplicate_calls": [seq for seq in combined_seqs if len(seq) == 2 and seq[0] == seq[1]],
            "permutations": [seq for seq in combined_seqs if len(seq) >= 2 and seq[0] != seq[1]],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BugX 状态机序列突变推演器")
    parser.add_argument("action", nargs="?", default="check", choices=["check", "run"], help="探针动作")
    parser.add_argument("--root", type=str, default=".", help="项目根目录")
    parser.add_argument("--transitions", type=str, default="", help="跃迁定义 JSON 字符串或文件路径")
    parser.add_argument("--max-length", type=int, default=4, help="序列最大深度")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    if args.transitions:
        raw = args.transitions.strip()
        data = None
        if raw.startswith("[") or raw.startswith("{"):
            try:
                data = json.loads(raw)
            except Exception:
                pass
        if data is None and Path(raw).is_file():
            data = json.loads(Path(raw).read_text(encoding="utf-8"))
        if isinstance(data, dict) and "transitions" in data:
            data = data["transitions"]
        if not isinstance(data, list):
            data = []
        res = fuzz_transitions(data, max_length=args.max_length)
    elif args.action == "check":
        res = {
            "status": "READY",
            "checks": [
                {
                    "name": "stateful_sequence_generator",
                    "ok": True,
                    "detail": "状态机反常序突变生成器就绪",
                }
            ],
            "capabilities": ["stateful_fuzzer", "anomaly_sequence_generation"],
            "fallbacks": {},
            "warnings": [],
        }
    else:
        gen = StatefulSequenceGenerator([
            Transition("create", "none", "created"),
            Transition("pay", "created", "paid"),
            Transition("cancel", "created", "cancelled"),
            Transition("refund", "paid", "refunded"),
        ])
        oracle = StatefulOracle(gen)

        def buggy_order_dispatcher(action: str, state: dict[str, Any]) -> dict[str, Any]:
            if action == "create":
                state["status"] = "created"
            elif action == "cancel":
                state["status"] = "cancelled"
            elif action == "pay":
                state["paid"] = True
                state["balance"] -= 100
            return {"status": state.get("status")}

        def initial_order():
            return {"status": "none", "balance": 1000, "paid": False}

        def order_invariant(seq: list[str], state: dict[str, Any]) -> tuple[bool, str]:
            if state.get("status") == "cancelled" and state.get("paid"):
                return False, "已取消的订单发生了扣款支付（状态机逻辑越权/跳步漏洞）"
            return True, "ok"

        res = oracle.test_target(buggy_order_dispatcher, initial_order, order_invariant)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if "status" in res:
            print(f"[StatefulProbe] status={res['status']}")
        else:
            print(f"[StatefulFuzzer] 结果: {res.get('vulnerability_found', res.get('total_anomalies_generated'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
