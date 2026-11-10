# Stage 30: Behavior (黑盒：外部行为与协议边界)
<!-- L1:START -->
1. 以**无源码依赖**方式驱动目标：CLI 参数、stdin、文件、网络、共享内存等输入面。
2. 构造畸形/超长/负值/截断输入，观测崩溃、静默截断、越界与信息泄露。
3. 校验错误路径：失败是否 fail-closed，是否泄露内部路径/版本/符号。
4. 完成至少一个真实动态用例后，按下方 schema 产出当前 run 的 `behavior-evidence`；静态推断不能代替运行结果。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 黑盒阶段**只依赖可执行产物**，不得引用内部符号名作为结论依据。
- 崩溃证据必须包含：输入载荷、运行命令、退出码/信号、可复现次数。
- 与 Stage 10/20 的静态结论**交叉印证**：黑盒命中优先，静态命中需黑盒可达才升级为确认。
- 需要真机/模拟器时，标注为需人工执行，不得伪造结果。
- 每个 Worker 独立写入 `.bugx/artifacts/behavior-evidence/<worker_id>.json`，其中 `<worker_id>` 必须逐字使用 harness context 的 `worker_id`；`run_id` 使用同一 context 的当前 Supervisor run。
- 仅在至少一个用例实际执行并记录观察结果后写入 `status: "completed"`。无法执行时提交 `blocked`/`unverified` checkpoint，不得创建伪造的 completed 产物。

```json
{
  "schema_version": 1,
  "stage_id": "30-behavior-black-box",
  "run_id": "<harness context run_id>",
  "worker_id": "<harness context worker_id>",
  "status": "completed",
  "cases": [
    {
      "case_id": "<stable case identifier>",
      "payload": "<exact input sent to the target>",
      "command": ["<executable>", "<arguments>"] ,
      "exit_code": 0,
      "signal": null,
      "replay_count": 1,
      "observed": "<observable target behavior>"
    }
  ]
}
```

`exit_code` 与 `signal` 字段都必须存在，且恰有一种结果有效：正常退出时 `exit_code` 为整数、`signal` 为 `null`；被信号终止时 `exit_code` 为 `null`、`signal` 为非空字符串。`replay_count` 必须为正整数，`observed` 必须描述运行时观察。`command` 的首项必须是非空可执行程序名，但后续参数允许空字符串（例如测试空输入）。引擎只接受当前 run、文件名与 `worker_id` 一致且 schema 完整的普通 JSON 文件。
<!-- L2:END -->
