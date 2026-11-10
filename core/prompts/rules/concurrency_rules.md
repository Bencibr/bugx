# BugX L2 专项规则：并发时序与死锁防范规则（Concurrency & Deadlock Safety）

## 1. 核心并发漏洞类型
1. **TOCTOU（Time-of-Check to Time-of-Use）竞争窗口**：
   - 先检查状态（如 `if not exists(path): write(path)` 或 `if balance >= amount: deduct(amount)`），再在无锁保护下执行写操作，导致并发穿透。
2. **多锁乱序死锁（Lock Inversion Deadlock）**：
   - 线程 A 按 `Lock1 -> Lock2` 加锁，线程 B 按 `Lock2 -> Lock1` 加锁，发生循环等待死锁。
3. **未加保护的复合写操作**：
   - 对非原子类型（如字典、列表）进行多线程读写，导致数据结构损坏或脏读。

## 2. 挖掘与注入策略
- 使用微秒级上下文切换扰动：在加锁前后或状态检查与执行之间插入微秒级延迟；
- 多线程并发冲击目标方法，断言数据不变量与锁超时时间；
- 检出后使用独立复现脚本精准触发竞争窗口。
