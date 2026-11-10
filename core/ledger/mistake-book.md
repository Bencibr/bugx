# BUG 错题集（Mistake Book）

> 本文件由 `lesson_ledger.py export-book` 从 SQLite 错题账本自动生成，**请勿手工编辑**。
> 历史事实源是账本本身：新增/复用经验走 cycle-review（`review`），工具归因走
> `tool-feedback`；本文件只是人读快照，修改这里不会影响检索与门禁。
> 每轮勘察前通读本文件（或 `retrieve` 检索），把「同类排查点」当作主动排查清单——
> 与 `history`（根因去重，管"别重复报"）互补：错题集管**往哪挖**。


- 生成时间：2026-08-20T05:36:14+00:00；state_hash：`d0be019b44ad26e0…`；模式总数：14（revision 0）

## 分类速查

| 类别 | 代表模式 | 状态 | 命中/使用 |
|---|---|---|---|
| api-contract | API 契约、权限或状态码违例 | active | 0/0 |
| concurrency | 并发导致状态自相矛盾 | active | 0/0 |
| contract | 生产者消费者契约键名不一致 | active | 0/0 |
| drift | 功能演进导致静态表项漂移 | active | 0/0 |
| input-boundary | 输入边界和正则吞掉合法内容 | active | 0/0 |
| oracle-integrity | 测不到目标或同源 Oracle 导致假绿 | active | 0/0 |
| ordering | 取到最旧记录而不是最新记录 | active | 0/0 |
| partial-failure | 单条脏数据拖垮整批处理 | active | 0/0 |
| regression | 修复引入回归或漏同步 | active | 0/0 |
| secrets | 嵌套敏感字段脱敏失效 | active | 0/0 |
| streaming | 流式解析过早返回丢最终结果 | active | 0/0 |
| type-assumption | 字段类型假设过窄导致解析崩溃 | active | 0/0 |
| ui | UI 视觉和交互状态违例 | active | 0/0 |
| validation | 只看校验表面导致静默放行 | active | 0/0 |

## 错题条目

### [api-contract] API 契约、权限或状态码违例
- 根因：接口契约和错误路径没有被断言驱动地验证
- 反思：接口判断必须是状态码、关键字段和权限边界的断言组合，不能只看请求成功。
- 同类排查点：状态码；响应结构；越权和未鉴权；幂等和重试
- 实例：mistake-book.md:127
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [concurrency] 并发导致状态自相矛盾
- 根因：并行路径缺少明确的仲裁和串行提交边界
- 反思：并发状态必须验证互斥、唯一性、事件循环和失败后的合并结果。
- 同类排查点：竞态；并行追加与覆盖；事件循环嵌套；仲裁平局
- 实例：mistake-book.md:64
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [contract] 生产者消费者契约键名不一致
- 根因：生产者和消费者没有共享或验证结构化契约
- 反思：跨模块数据流要同时检查正常、警告、降级和空结果分支。
- 同类排查点：生产者返回结构；消费者键名；warning/fallback 分支
- 实例：mistake-book.md:71
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [drift] 功能演进导致静态表项漂移
- 根因：手维护的枚举和补全表没有随功能同步
- 反思：所有可用项列表必须和真实注册表或路由建立一致性检查。
- 同类排查点：CLI 路由；补全表；白名单和枚举
- 实例：mistake-book.md:85
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [input-boundary] 输入边界和正则吞掉合法内容
- 根因：正则和切片逻辑没有边界断言
- 反思：定位触发词和实际切割范围必须分离，合法上下文要有负向样本。
- 同类排查点：词边界；邮箱和 @ 引用；中句触发词
- 实例：mistake-book.md:106
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [oracle-integrity] 测不到目标或同源 Oracle 导致假绿
- 根因：验证范围、Oracle 来源和异常状态没有进入统一状态模型
- 反思：命令成功、同源答案或解析为空都不能单独证明目标已验证和没有问题。
- 同类排查点：目标平台和指纹；独立 Oracle；异常上抛；PASS/complete 生成点
- 实例：mistake-book.md:134
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [ordering] 取到最旧记录而不是最新记录
- 根因：分页默认升序被误用于获取最新状态
- 反思：所有最近状态、恢复和回放路径都要验证排序方向与 limit 语义。
- 同类排查点：最近记录排序；LIMIT 与 ASC/DESC；恢复续接
- 实例：mistake-book.md:50
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [partial-failure] 单条脏数据拖垮整批处理
- 根因：批量处理遇到单条脏数据就终止整个集合
- 反思：批量输入必须逐条容错并显式告警，不能把部分失败伪装成全量失败或空结果。
- 同类排查点：逐条解析失败；坏行之后的好数据；回放和迁移
- 实例：mistake-book.md:113
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [regression] 修复引入回归或漏同步
- 根因：修复只覆盖首个坏例，缺少全仓同根因和合法输入回归
- 反思：修复既要挡住坏输入，也要证明合法输入没有被误伤，并同步所有同根因路径。
- 同类排查点：合法输入回归；同根因全仓搜索；修复前后差异
- 实例：mistake-book.md:78
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [secrets] 嵌套敏感字段脱敏失效
- 根因：敏感数据保护只覆盖表层且缺依赖时 fail-open
- 反思：导出、日志和序列化必须递归处理敏感字段，无法确认时拒绝输出。
- 同类排查点：递归脱敏；嵌套列表字典；缺依赖 fail-closed
- 实例：mistake-book.md:57
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [streaming] 流式解析过早返回丢最终结果
- 根因：多事件协议被错误简化为单个 JSON 响应
- 反思：流式响应必须区分通知和应答，不能遇到第一个 JSON 就提前返回。
- 同类排查点：progress 通知；最终 result；SSE 多事件
- 实例：mistake-book.md:99
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [type-assumption] 字段类型假设过窄导致解析崩溃
- 根因：解析层把单一调用方类型当成字段契约
- 反思：调用方当前传入的类型不是稳定契约，解析边界必须按多形态输入验证。
- 同类排查点：裸类型转换；多形态字段；脏输入
- 实例：mistake-book.md:43
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [ui] UI 视觉和交互状态违例
- 根因：视觉和交互状态没有可执行的几何与可达性断言
- 反思：UI 不能只验证能显示，要用不同尺寸、内容和状态验证功能性视觉契约。
- 同类排查点：多视口；长文本/CJK/emoji；截图和 DOM 几何
- 实例：mistake-book.md:120
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

### [validation] 只看校验表面导致静默放行
- 根因：校验停留在名称和语法，没有最小行为验证
- 反思：静态通过不能代表可用，注册和晋升路径必须有独立的行为验证。
- 同类排查点：真实最小调用；签名校验；批量坏数据隔离
- 实例：mistake-book.md:92
- 战绩：status=active，use=0，hit=0，miss=0，false_positive=0，rejected=0

