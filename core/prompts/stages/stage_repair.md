# BugX 阶段 3：TDD 修复与结算复盘 Harness（Stage Repair & Settle Protocol）

## 1. 阶段目标与闭环原则
本阶段将捕获的缺陷固化为最小自包含复现证据，驱动 TDD 最小修复与定向回归，并最终通过结算门禁更新复盘账本。

## 2. 独立复现脚本自动合成（Standalone Repro Synthesizer）
1. 调用 `minimize_repro.py` 合成单文件验证脚本：
   ```bash
   python3 "$(bugx engine path --name minimize_repro.py)" --synthesize-standalone --finding-id <ID> --output .bugx/evidence/repro_<ID>.py
   ```
2. **硬约束标准**：
   - 单文件 `.py`，零外部第三方依赖（仅用标准库），自包含 `assert` 断言；
   - 修复前运行：退出码为 0（证明 Bug 存在）；
   - 修复后运行：退出码为 1（证明已闭环）。
3. **退出码方向必须与补丁包红绿回放一致**：`bugx patch-forge` 的 manifest 校验硬性要求
   `repro[].before_exit=0 / after_exit=1`（以当前补丁包 schema 为准）。方向写反的 repro 会在
   Phase 1 被判「Repro 预检失败 (未复现缺陷)」，整包 fail-closed 拒绝。

### 2.1 黄金自包含复现脚本范式（Golden Repro Template）
```python
#!/usr/bin/env python3
# repro_<finding_id>.py: 退出码 0 代表复现成功（缺陷仍在），退出码 1 代表修复转绿闭环
import sys

def verify():
    # 构造最小输入触发缺陷
    from core.wallet import Wallet
    w = Wallet(balance=100)
    w.deduct_concurrent(100, 100)
    assert w.balance >= 0, f"余额非法穿透: {w.balance}"

if __name__ == "__main__":
    try:
        verify()
        print("✓ CLOSED: 缺陷已消除，闭环通过")
        sys.exit(1)   # 修复后：无法复现
    except AssertionError as err:
        print(f"✗ REPRO: 稳定触发预期缺陷 -> {err}")
        sys.exit(0)   # 修复前：复现成功
```

## 3. TDD 最小代码修复纪律（DOC-194）
1. **定向修复原则**：只改与该 Bug 直接相关的最小代码行，严禁借机扩大重构范围。
2. **严禁修复 Agent 跑全量（硬红线）**：
   - **禁止**修复 Agent 运行任何全仓/全量测试（如 `python3 tests/run_tests.py`、`unittest discover` 等）；
   - 修复 Agent 仅需且只能运行：
     ```bash
     # 自动解析当前工作树/改动所受影响的最小测试子集并执行
     bugx test --affected --run
     # 语法编译检查
     python3 -m py_compile <修改的文件路径>
     ```
   - **全量测试职责归属**：全量回归测试**由宿主在全部修复 Agent 工作收敛、所有补丁合并落盘后，统一集中执行一次**。
3. **防止回归**：将自包含复现脚本加入当前项目的回归守护套件。

## 4. BugX 引擎补丁打包、红绿沙箱复验与结算闭环（Patch Forge & Settle）
当最小代码修复完成且定向单测通过后，必须通过 `bugx patch-forge` 完成自包含补丁打包与闭环验证，**严禁使用任何服务端 MCP 旁路进行补丁分发或代码拉取**：

### 4.1 引擎自身缺陷闭环（kind: engine）
若挖掘过程中发现 BugX 自身（`core/`、`src/`、`tests/`、`docs/`、`scripts/`、`plugins/` 等 BugX 命名空间）的代码缺陷，闭环终点是**官方发布通道统一发版**；目标项目缺陷只保留为 finding，不得生成或应用为 BugX 补丁：
1. **生成标准化引擎补丁包并显式提交服务端独立沙箱复验**：
   ```bash
   bugx patch-forge \
       --from-issue <SPEC.md> \
       --kind engine \
       --diff fix.patch \
       --repro repro.py \
       --test tests/test_engine.py \
       --output .bugx/patches/ \
       --submit
   ```
2. 服务端在隔离沙箱中独立执行红绿回放与不变量审计（Phase 1 复现缺陷 → Phase 2 应用 diff → Phase 3 缺陷消除 + 回归测试；并校验实际改动集合与 `manifest.diff.files` 一致），经人工审核合并后发布新版本；
3. 客户端在 Session 边界（Session 结束后）执行 `bugx update` 升级引擎，**严禁在 Session 运行中热更 `core/` 或篡改 `engine-integrity.json`**。

## 5. Post 终局结算与知识复盘（Life & Round Settle）
执行结算命令：
```bash
bugx engine --root . post [--final]
```
结算门禁将自动完成：
1. **Finding 审查与计命**：核验 Finding 的三要素（复现步骤、预期断言、根因分析），有效 Finding 计入寿命分值；已生成 PR 的 Finding 记录 PR 引用以防止重复挖掘。
2. **复盘账本更新**：自动提取失败教训与修复经验，沉淀至项目层与全局层 `lesson-ledger.json`。
3. **生成审计报告**：输出 `report.md`，并在授权范围内处理多设备/团队匿名共享。
4. **清理与收敛**：释放 Worker 租约与锁资源，优雅完成本轮会话。
