# BugX 阶段 2：深变异与差分推演 Harness（Stage Mutate Protocol）

## 1. 阶段目标与攻击向量
本阶段针对覆盖清单中的关键单元与业务链路，实施多维变异冲击与差分对抗，挖掘边界缺陷、时序竞态与状态机逻辑漏洞。

## 2. 三大核心变异武器
1. **语法感知与结构化 Payload 突变（Syntax-Aware Fuzzing）**：
   - 使用 `fuzz_input.py` 对函数输入参数、JSON 报文、CLI 选项进行树形变异；
   - 包含极限边界值（MAX_INT、NaN、超长递归嵌套）、特殊编码（Unicode 溢出、零宽字符、BOM 投毒）。
2. **并发竞态与死锁时序扰动（Concurrency Race & Deadlock Fuzzer）**：
   - 使用 `concurrency_fuzzer.py` 对多线程/多协程接口注入微秒级调度抖动（Schedule Perturbation）；
   - 探寻时间窗口检查即使用（TOCTOU）漏洞、共享变量未同步双写及锁乱序循环等待死锁。
3. **基于业务行为图谱的状态机推演（Stateful Sequence Fuzzer）**：
   - 使用 `stateful_fuzzer.py` 驱动 API 反常序调用（跳步跃迁、重入执行、撤销后批准、并发双扣）；
   - 突破常规状态校验与前置权限拦截。

## 3. 沙盒双跑差分验证（Differential Oracle）
- 对比**变异前后行为与响应**：
  - 是否抛出未捕获内部 500 异常；
  - 是否发生静默数据篡改或不一致（双跑结果不同）；
  - 是否产生超时卡死或锁等待挂起。
- 确认异常属于真实 Bug 而非预期拒绝后，立即采集堆栈轨迹、最小输入 Payload 与环境上下文。

### 3.1 标准立案证据包范式（Golden Few-Shot）
符合 `finding_confirmation` 硬门禁的最小立案示例：
```json
{
  "id": "FINDING-RACE-001",
  "title": "并发双扣导致余额穿透为负数",
  "severity": "high",
  "evidence": [{"path": "core/wallet.py", "line": 42}],
  "root_cause": "扣款未在原子锁内校验余额：if balance >= amount 与 balance -= amount 存在竞争窗口",
  "confidence": 0.95,
  "reproducible": true,
  "reproduction": "python3 .bugx/evidence/repro_race.py"
}
```

## 4. 完成判定与下一步跃迁
- 一旦捕获可稳定复现的异常断言，立即保存现场并进入 **阶段 3：`core/prompts/stages/stage_repair.md`**；
- 若当前批次变异全部通过，继续下一组高危单元，直至覆盖预算或轮次耗尽。

