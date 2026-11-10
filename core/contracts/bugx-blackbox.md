> 运行前置：BugX 核心要求 Python `>=3.11`。本文中的 `python3` 命令只表示已通过版本
> 校验的解释器；系统 Python 3.9 不满足契约，请先按 `core/contracts/AGENT_GUIDE.md` 设置
> `BUGX_PYTHON`，或优先使用 `bugx` CLI。

## 黑盒模式（成品软件测试）

当你面对**成品软件/二进制**（本仓库构建的 `dist/*.whl`、`.venv/bin/neuro`、
外部可执行程序），或用户明确要求黑盒测试时，**不读目标源码**，把它当作
不透明对象：只通过公开接口喂输入、观察行为，用「行为 oracle」判定错误。
（白盒=读源码追链路；黑盒=成品即真相，跑出来的行为才算数。）

### 工具清单与场景匹配（开工先排查，工欲善其事）

**⓪ 按测试类型配工具（元认知总纲，先想清这层再选具体工具）**：
换任何项目类型都能答，靠的不是背工具清单，而是先按**测试类型**拆工具类别，
再按**项目类型**选具体工具——两层思维，缺一不可：

| 测试类型 | 需要哪些工具类别 | 项目类型 → 具体工具举例 |
|---------|-----------------|------------------------|
| **黑盒测试**（不读源码，喂输入看行为） | 接口/协议测试、UI 驱动、交互驱动、数据构造、模糊、性能、超时 | Web/API → postmcp；UI → playwright；TUI → agent-tty/pexpect；数据 → redis-cli/psql；模糊 → fuzz_input；性能 → ab |
| **白盒测试**（读源码，追逻辑/覆盖率/诊断） | 构建、单测框架、覆盖率、静态分析、反编译、运行时诊断 | Java → mvn/gradle+JUnit+JaCoCo+SpotBugs+jstack；Python → pytest+coverage+ruff；Go → go test+cover+vet；Rust → cargo+tarpaulin+clippy |
| **自动化测试**（回归/批量/持续验证） | 测试编排、批量并发、断言、最小化、差分对照、种子语料 | 回归 → `./mvnw test`/`pytest`/`go test`；批量 → 脚本并发；最小化 → minimize_repro；差分 → 等价实现对照；语料 → corpus_fetch |

**用法**：① 先按目标判断"这轮是黑盒、白盒还是自动化"（或组合）→ ② 从对应
行确定需要的工具类别 → ③ 再按项目类型/语言选具体工具 → ④ 只装当前项目
需要且缺的。**换项目类型只影响第③步，①②步的框架不变**——这就是"换项目
也能答"的原因。

**① 本仓库自研工具（优先用，都是磨好的刀）**：

| 工具 | 场景 | 怎么用 |
|------|------|--------|
| `fuzz_input.py` | 变异模糊找异常（解析器/格式/接口输入）；`--evolve` 行为引导跨轮进化种子 | `--input 合法样本 --cmd 目标 --out 异常目录 --count N --jobs P [--evolve 进化目录]` |
| `differential_probe.py` | 同语料差分对照目标/参考实现，找单侧看不出的语义分歧；产出最高级 Oracle 工件 | `run --target-cmd ... --reference-cmd ... --inputs 语料 --out 报告目录 [--repeat 3]` |
| `minimize_repro.py` | 异常输入缩到最小复现（根因集中） | `--cmd 目标 --input 异常 --check 复现判定` |
| `corpus_fetch.py` | 并发搜开源项目扩充种子语料（支持按项目类型/仓库定制） | `--lang python --query "json parser"` / `--lang rust --repo serde-rs/json` |
| `pattern_probe.py` | 把错题模式编译成可执行扫描，输出「嫌疑文件:行」线索清单（有界） | `scan --ledger <账本> --root <目标> [--query …]` |
| `differential_probe.py` 的参考供给 | corpus_fetch 定位同能力开源参考实现候选（star/license/活跃度） | `--find-reference --lang <语言> --query "<能力>"` |
| `seed_drill.py` | 种子缺陷演练：已知缺陷靶场 + 发现率评分 + 知识开/关 A/B 对比 | `build --out <目录>` / `verify …` / `compare --a --b` |
| `fix_watchlist.py` | 修复回归看护：巡查 incident 修后片段是否仍在目标中 | `check --ledger <账本> --root <目标>` |
| `guide_budget.py` | 指南上下文预算度量（分层/章节 token 成本） | `--top 15` |
| `regression_pack.py` | 已修 bug 资产化为可执行回归重放包（可入 CI；红=疑似回归） | `build --ledger … --root … --out …` / `run --pack …` |
| `finding_report.py diff` | 两轮 findings 三分类对比（新发/依旧/消失） | `diff --input a.json --input-b b.json` |
| `setup_ui_env.py` | UI 面环境自检/补装 | `check` / `install` |
| `platform_probe.py` | 桌面/Android/iOS 只读环境预检 | `check desktop/android/ios` / `status all --json` |
| `memory_probe.py` | 原生/Python/JVM/Electron/Android/Apple 内存工具只读预检 | `check <target>` / `status all --json` |
| `module_coverage.py` | 模块覆盖外部门禁（防清单漏列/伪造 100%）；`hits` 用实测 findings 校正命中列 | `status` / `check` / `final-check` / `hits --findings findings.json` |
| `coverage_intelligence.py` | 归一行/分支/条件/异常/状态/变异覆盖并映射最小单元 | `import` / `merge` / `gaps` / `gate` |
| `behavior_graph.py` | 从路由、事件、数据库、配置、权限和并发事实发现系统行为流 | `scan` / `flows` |
| `test_strategy.py` | 风险优先、最终全覆盖的测试技术与调度计划 | `plan` |
| `finding_confirmation.py` | 候选→观察→复现→根因→确认/驳回的外部门禁 | `validate` / `gate` / `transition` |
| `finding_report.py` | 只导出已确认 Bug，未决候选阻止最终报告 | `validate` / `export` |
| `tools_kb.py` | 本地工具知识库 30 天有效期检查 | `check` |
| `prep_validate.py` | 开工准备记录门禁 | `--record .bugx/pollings/<polling-id>/lifecycles/<lifecycle-id>/state/prep-record.md` |
| `verify_life.py` | 寿命/防舞弊（机制，非挖掘） | `check`/`settle` 等 |
| `launch_bugx.py` | 运行监督与启动协议（机制） | `status` / `heartbeat`；调用方另用 `pre/post/pause/resume/abort` |
| `agent_eval.py` | Agent 行为评测（发现率/误报/证据/复现） | 调用方或 CI：`score --strict --json` |
| `lesson_ledger.py` | 结构化复盘账本、经验检索、cycle-review 门禁、命中反馈和效果指标；工具归因反馈与经验等级、数据驱动场景→工具矩阵、错题集快照生成 | `validate` / `retrieve` / `review` / `feedback` / `metrics` / `tool-feedback` / `tool-metrics` / `tool-lookup` / `tool-matrix` / `export-book` |

 **② MCP 工具（PostMCP/Playwright 已授权）**：

| 工具面 | 场景 | 覆盖目标 |
|--------|------|---------|
| **postmcp**（`postmcp_*`） | API 契约轰炸 | REST/GraphQL/WebSocket 接口：状态码、参数边界、越权、注入、断言 |
| **playwright**（`playwright_*`） | Web UI 视觉/交互 | Web 页面：布局、焦点、状态、断点、对比度；不等同于原生桌面/移动驱动 |

**②.5 数据库 MCP（数据构建/内部观测/异常数据，黑盒测试的关键底座）**：
许多黑盒测试需要造数据、查内部状态、验证持久化——这依赖数据库访问，
**在工具准备阶段就要按项目备好对应的数据库 MCP 或 CLI**（因地制宜，项目
用什么库配什么）：

| 数据库 | 工具 | 用途 |
|--------|------|------|
| Redis | redis MCP 或 `redis-cli` | 查缓存/会话/验证码/token；测过期、注入、key 设计 |
| PostgreSQL / MySQL | pgsql MCP / mysql MCP 或 `psql`/`mysql` CLI | 查/造数据、验证持久化、测约束、异常数据构造 |
| MongoDB / 其他 | 对应 MCP 或 CLI | 同上 |
| 文件型（SQLite 等） | 直接读文件 + `sqlite3` | 同上 |

**数据库工具的职责**（通用，不绑定单一场景）：
1. **造测试数据**：黑盒测接口前，在 DB 里预置场景数据（用户、订单、配置、
   边界值）——让接口可测、覆盖到数据相关的分支。
2. **构造异常数据**：往 DB 塞脏数据（坏 JSON、超长、类型错位、残缺行），
   验证应用读脏数据时是否健壮（不崩溃、有容错）——这是高命中挖掘点。
3. **内部状态观测**：验证码/token/缓存值、事务结果、写入是否落库——用
   DB 观测验证行为，比纯黑盒"猜"更准。
4. **验证持久化**：接口写操作后查 DB 确认数据是否正确落库（幂等/部分写入
   是隐性 bug）。
5. **清场/恢复**：测试造的脏数据测试后清理（不污染后续）。

**数据库工具纪律**：
- **按项目实际需要配**（因地制宜）：项目用 PostgreSQL 就配 pgsql，用 Redis
  就配 redis，不用不装。
- **只读观测 + 造数据是合法的**；**不删改他人/生产数据**，造的测试数据
  测试后清理（遵守安全与范围守则）。
- 无对应 MCP 时用 CLI（`redis-cli`/`psql`/`sqlite3`）等价完成。
- 数据库是**测试的数据底座**，不是被测对象本身——用它造数据、验证结果，
  数据层自身的 bug（约束缺失/冗余/索引）也是可挖点。

**②.5 TUI/终端交互工具（专治 TUI/REPL/向导，优先用现成）**：

| 工具 | 场景 | 说明 |
|------|------|------|
| **`agent-tty`**（首选） | TUI 交互轰炸（terminal 版 Playwright） | `npm install -g agent-tty@0.5.0`；给 agent 真实终端，驱动 nvim/htop 等交互应用，返回**快照/截图/录像可复核**。要求 Node≥24 且 <27。**优先用它替代自研 PTY harness** |
| **`pexpect`**（Python） | TUI 按键序列/断言 | `pip install pexpect==4.9.0`；成熟 PTY 交互（spawn/expect/sendline），自带超时与断言 |
| **`expectrl`**（Rust） | Rust 侧 PTY 交互 | `cargo add expectrl` |
| **`expect`**（系统） | 快速 TUI 自动化 | `/usr/bin/expect`，Tcl 脚本驱动交互应用 |
| 自研独立 harness | 前四个都用不了时的 fallback | 仅当目标环境无 Node/Python/expect 才自研（见接口面 2 纪律） |

**TUI 工具选择铁律**：① 有 `agent-tty`/`pexpect` 就不用自研 harness（成熟工具是安全观测仪器，不属于被测仓库，复用不污染同源性）；② 自研 harness 是最后手段，不是默认；③ 工具缺失先按项目固定版本安装（`agent-tty@0.5.0` / `pexpect==4.9.0`）再打。

**③ 系统级工具（`which` 先排查，缺则先装）**：
- `timeout` — 给所有目标套超时（防挂起卡死）
- `objdump`/`nm`/`strings`/`hexdump`/`xxd` — 二进制静态侦查（L3 逆向，读符号/字符串/字节）
- `curl`/`jq`/`nc` — 手动 HTTP 探测、响应解析、TCP 端口探测
- `ab` — HTTP 压测（找性能/并发/资源泄漏）
- `coverage`(Python)/`pytest` — 覆盖率引导 + 跑测试
- 插桩类 `strace`/`gdb`/`valgrind` — **可能缺失，先 `which` 确认；缺则装**
  （`brew install strace gdb` 或改用系统自带 `lldb`/`dtrace` 替代）

**③.5 语言专项工具（白盒，按项目语言配，因地制宜）**：白盒测试要
"读源码 + 跑单测 + 覆盖率 + 静态分析 + 运行时诊断"，**按项目语言补齐对应
工具链**（项目用什么语言就配什么，不全部装）：

| 语言 | 构建 | 单测框架 | 覆盖率 | 静态分析/反编译 |
|------|------|---------|--------|----------------|
| **Java** | mvn/gradle | JUnit 5（`./mvnw test`/`gradle test`） | JaCoCo（`gradle jacocoTestReport`/mvn jacoco） | SpotBugs/PMD/Checkstyle；反编译用 CFR（比 javap 强） |
| **Python** | — | pytest | coverage | ruff/mypy/bandit |
| **Go** | go build | go test | `go test -cover` | go vet/staticcheck |
| **Rust** | cargo | cargo test | tarpaulin | clippy/miri/geiger；cargo-fuzz/honggfuzz-rs 模糊 |
| **JS/TS** | npm/yarn | vitest/jest | c8 | eslint/tsc |

**白盒 Java 必备（本仓库工具未装全，按需补）**：
- 构建：`mvn`（Maven 项目必装，或项目自带 `./mvnw` wrapper）/ `gradle`（已装）
- 单测：JUnit 5（随项目依赖，`./mvnw test` 或 `gradle test` 自动拉取）
- 覆盖率：JaCoCo（配进构建插件的 `jacocoTestReport` 任务，跑完出
  `build/reports/jacoco/` 报告——**未覆盖分支 = 定向挖点**，与白盒读码配合）
- 静态分析：SpotBugs/PMD（扫坏味道/空指针/资源泄漏候选）
- 运行时诊断：`jps`/`jstack`（线程 dump 找死锁/挂起）、`jmap`（堆 dump）、
  `jstat`（GC/内存趋势）、`jcmd`（JVM 命令）
- 反编译：`javap`（已装，够用）+ CFR（`brew install cfr-decompiler`，看
  更完整的类逻辑）
**工具纪律**：白盒工具有的已装、有的按项目补——Maven 项目没 mvn 就用
`./mvnw` wrapper（项目自带）；没有 wrapper 才 `brew install maven`。JVM
诊断工具（jps/jstack/jmap/jstat/jcmd）随 JDK 自带，`which` 确认即可。

**Rust 桌面/GUI 专项（白盒+黑盒，按 GUI 框架选）**：
- **原生 GUI（egui/iced/slint）**：
  - `egui-driver`（GitHub: ryo33/egui-driver）— egui 应用自动化/测试专用，
    **egui 项目的首选 GUI 驱动**（发事件/断言控件状态）
  - `enigo` — 跨平台键盘/鼠标模拟（通用兜底）
  - 截图对比（macOS `screencapture`）
- **Tauri（Rust + Web 前端混合）**：
  - 优先使用项目测试 + 官方 `tauri-driver` 当前明确支持的平台；固定 driver、
    WebDriver 与系统版本，并用 Rust/前端测试覆盖 `#[tauri::command]` 和 IPC。
  - macOS WKWebView 缺官方 WebDriver 支持时，使用 OS 自动化或经验证的第三方
    驱动，并把限制写入覆盖矩阵；不得默认声称 Playwright 可直接驱动 Tauri。
- **Rust 模糊/鲁棒性挖掘**：
  - `cargo-fuzz`（libFuzzer 原生 fuzz，**解析逻辑首选**）
  - `honggfuzz-rs`（GitHub: rust-fuzz/honggfuzz-rs）— 谷歌 Honggfuzz 的 Rust 绑定
  - `cargo miri` — unsafe 块未定义行为检查（Rust 特有的内存安全验证）
  - `geiger`（`cargo geiger`）— 统计 crate 中 unsafe 代码量（定位 unsafe 热点）
  - `proptest` — 属性测试（随机生成输入验证不变量）
**工具纪律**：GUI 框架决定驱动工具（egui→egui-driver、Tauri→官方
`tauri-driver` 支持平台或 OS 自动化、其他→框架/OS 驱动），模糊用
cargo-fuzz/honggfuzz-rs，unsafe 审查用
cargo-miri+geiger。**先调研项目用的 GUI 框架再选具体工具**（因地制宜）。

**④ 场景→工具速查（数据优先：先查账本实测，再用静态默认）**：

先运行 `"$BUGX_PYTHON" "$(bugx engine path --name lesson_ledger.py)" tool-matrix` —— 它按本项目
tool_usage 实测命中输出「问题类别 → 工具」排序表（如 `api-contract →
postmcp×3`）。**有实测数据时以 tool-matrix 为准**修正下面的静态默认；
`tools_kb.py experience` 可一并查看每个工具的目录新鲜度 + 经验等级
（unproven/candidate/active，按独立 lifecycle 命中晋级）。静态速查只是
冷启动默认，不代表本项目实测最优：
- 找解析器/格式崩溃 → `fuzz_input --evolve`（行为引导进化）+ `minimize_repro` + 种子语料
- **找隐性语义分歧 → `differential_probe`（同语料对照参考实现；repeat>1 剔除 flaky；报告可直接作 differential Oracle 引用）**
- 找 API 契约/越权/注入 → postmcp（导入 Swagger 全接口 + 参数边界 + 断言）
- 找 UI 布局/焦点/状态 → playwright（多断点截图 + 几何断言）
- **找 TUI/REPL 交互 bug → agent-tty（截图/录像）或 pexpect（按键序列+断言）**
- **造测试数据/验证持久化/观测内部状态 → 数据库 MCP 或 redis-cli/psql/sqlite3**
- **构造异常数据测健壮性 → 数据库 MCP 塞脏数据（坏 JSON/超长/残缺行）→ 验证应用容错**
- 找二进制内存/符号 → objdump/nm/strings/hexdump
- **白盒读码挖洞（Java）→ javap/CFR 反编译 + SpotBugs/PMD 静态扫描 + JaCoCo 覆盖率定位未覆盖分支**
- **白盒跑单测/回归（Java）→ `./mvnw test` 或 `gradle test`（JUnit 5）**
- **找 Java 挂起/死锁 → jstack 线程 dump + jps 定位 PID**
- **找 Java 内存泄漏/堆问题 → jmap 堆 dump + jstat GC 趋势**
- **白盒挖 Rust unsafe/UB → cargo miri + cargo geiger（定位 unsafe 热点）**
- **Rust 解析逻辑深度模糊 → cargo-fuzz / honggfuzz-rs（libFuzzer）**
- **egui 桌面自动化 → egui-driver（GitHub ryo33/egui-driver）**
- **Tauri E2E 自动化 → 官方 `tauri-driver` 支持平台；macOS 记录 WebDriver 缺口并走 OS 自动化/经验证第三方方案**
- **桌面 GUI → Electron API、XCTest UI、Appium Mac2、FlaUI/Windows driver、AT-SPI/dogtail，按平台选**
- **Android/iOS → 原生测试优先；共享黑盒旅程用 Appium/Maestro，真机边界单独举证**
- **应用内存安全/泄漏/OOM → 读取 `memory-testing.md`；Sanitizer/heap/持有链 + RSS/PSS/资源趋势交叉举证**
- 找性能/资源泄漏 → ab 压测 + `/proc` 观测
- 找挂起/卡死 → `timeout` 包裹 + 超时观测

**自研工具与系统工具配合**：fuzz 筛出的异常样本 → `minimize_repro` 最小化 →
objdump/strings 看内部 → 根因链。自研工具是主武器，系统工具是辅助解剖刀。

**多工具协作（复杂项目）**：复合材质项目（Web+API+CLI+TUI+DB+桌面/移动+
内存专项）单工具打不全，
必须多工具分工——
- **按模块分配**：前端模块 → Playwright，后端 API → postmcp，CLI → timeout/
  objdump/fuzz，TUI 管理端 → agent-tty/pexpect，桌面/移动客户端 → 原生测试优先、
  Appium/Maestro 补共享黑盒旅程，配合 `module-coverage.md`
  分工追踪，每模块一个主工具。
- **内存横切分工**：业务旅程驱动负责稳定复现，平台 sampler 负责 RSS/PSS/资源
  时序，heap/Sanitizer/profiler 负责分配栈或持有链；三者共享同一轮次与时间戳，
  不能各跑一套不可对齐的场景。
- **编排成流水线**：上游产出喂下游——postmcp 测出接口异常 → Playwright 验证
  前端是否受影响（横向互证）；fuzz 异常样本 → minimize 缩最小 → objdump 看
  内部 → 根因链。
- **共享输入统一放**：种子语料/异常样本/覆盖清单放公共位置，不各建一套；
  有依赖的工具按序执行，无依赖并发（应并发尽并发）。
- **一模块一主工具**：避免两工具做同一件事互相干扰；复杂项目"谁负责什么"
  写清，工具打架 = 编排失败。

### 目标获取（先搭出真·成品运行环境）

- 本仓库 wheel：`python3 -m venv /tmp/bb-venv && /tmp/bb-venv/bin/pip install dist/<version>.whl`
  → 得到独立的 `neuro` 可执行（与源码解耦，测的是发布物不是工作区）。
- 交互式目标：直接跑 `neuro` / `interaction/cli.py`，走真实 PTY。
- 外部二进制：`which` 定位 + 版本确认（`--version` / `--help`）。
- **UI/Web 目标**：本地 `npm run dev` / `python -m http.server` 起服务
  拿到 `http://127.0.0.1:<port>`，或用 Playwright 打开线上 URL。确认
  渲染引擎（Chromium/WebKit）与视口尺寸，固定为复现环境。
- **已上线 Web 目标**：公开核心只执行明确授权的页面可用性和业务旅程回归，固定
  部署版本、观察窗口、账号角色和只读路径；超出授权的生产防护与主动验证不能由公开
  核心代执行。
- **桌面/移动目标**：固定安装包/应用版本、SHA-256、平台、设备/模拟器、OS、
  签名/构建类型、账号、服务和初始状态；开工前读取
  全局引擎 `platform-testing.md`，并运行对应只读预检：
  `"$BUGX_PYTHON" "$(bugx engine path --name platform_probe.py)" check desktop|android|ios`。
  预检为 `BLOCKED` 时不得执行该平台测试或输出 PASS，也不得自行安装 driver、
  SDK、Xcode、Maestro、设备 Agent 或系统自动化软件。

### 七接口面（黑盒只通过这几个口子打）

1. **单次 CLI**：`timeout 30 <bin> <args> < input > /tmp/out 2> /tmp/err`
   —— 必须捕获：exit code、stdout、stderr；非零退出 / traceback / 挂起都是信号。
2. **交互式（TUI / REPL / 向导）**：**优先用现成 TUI 工具**（`agent-tty`/
   `pexpect`/`expectrl`/系统 `expect`，见「工具清单与场景匹配·②.5」）——它们是
   成熟观测仪器、不属于被测仓库，复用**不污染同源性**，且自带截图/断言/超时。
   工具缺失先装（`npm install -g agent-tty@0.5.0` / `pip install pexpect==4.9.0`）再打。
   **自研独立 harness 只是 fallback**（仅当现成工具都不适用时），且必须：
   - 只用 Python 标准库（`pty` / `os` / `select` / `fcntl` / `subprocess`）
   - 写在仓库之外（`/tmp/bb-harness/`），**不 import 目标任何模块/文件**
   - 自己实现 spawn / 发送按键 / 读输出 / 超时 / 屏幕快照 / 退出状态捕获
   - 自带 harness 自身错误检测（发送后无响应、PTY 异常断开、字节流损坏），
     把「harness 坏」和「目标坏」分开记录
   每轮新写或增补这个独立 harness；harness 自身也是质疑对象。
   **同源边界**：无论是现成工具还是自研 harness，**一律禁止复用被测仓库自带
   的测试基建**（`scripts/smoke/acp_pty_acceptance.py`、`tests/infra/test_run_tests.py`
   全都是被测仓库的一部分）——复用 = harness 与目标同源，harness 的 bug 无法
   与目标的 bug 区分，黑盒失去独立性（观测仪器不能长在被测对象身上）。
   自研 harness 必须始终留在仓库之外（`/tmp/bb-harness/`）；一旦被写进仓库
   （`tools/`、`tests/`、`src/` 等），它就成为被测对象的一部分，不得再用于
   黑盒（黑盒复用自己进过仓库的工具 = 间接复用被测仓库）。
3. **数据文件 / 配置 / 服务接口**：喂构造的畸形 JSON/YAML/DB/输入文件；
   若目标起服务（HTTP/RPC），用 curl / requests 打接口。
4. **UI 视觉/交互面（浏览器渲染）**：用 Playwright 打开目标页面，通过
   DOM 快照 + 截图 + 视口/交互操作观察渲染结果。**视觉即契约**——
   UI 的错误表现为：元素错位/遮挡/溢出、布局在断点处崩塌、文案截断/
   重叠、焦点陷阱/键盘不可达、点击无效/事件不触发、状态视觉反馈缺失
   （loading/error/empty/disabled 不分）、对比度不足、暗色/浅色模式异常。
   判定以「像素级截图 + DOM 几何断言」为准，不靠"看着怪"。
5. **API 服务/接口面（postmcp）**：用 **postmcp** MCP（`postmcp_*` 已授权）
   对 HTTP(REST)/GraphQL/WebSocket 接口做请求自动化测试。接口即契约——
   API 的错误表现为：状态码不符、响应结构错位（字段缺失/类型错）、参数校验
   缺失、越权/未鉴权可访问、注入（SQLi/命令/NoSQL）、超时/挂起、WebSocket
   断连或消息错乱。判定以「请求/响应断言 + JSONPath 校验」为准，不猜不编。
6. **桌面 GUI 面**：Electron 用项目测试 + Playwright Electron API；Tauri 用
   Rust/前端测试 + 官方 `tauri-driver` 支持平台；原生桌面按 OS 选择 XCTest UI/
   Appium Mac2、Windows driver/FlaUI、AT-SPI/dogtail。必须覆盖窗口、菜单、
   快捷键、文件/剪贴板、权限、安装升级、离线恢复和多窗口/单实例等产品相关状态，
   并以截图/录屏 + 可访问性树/窗口状态 + 日志或独立结果断言举证。
7. **移动 App 面**：Android 有源码时优先 JUnit/Robolectric、Espresso/Compose UI、
   UI Automator；iOS 优先 Swift Testing/XCTest + XCUIAutomation；成品共享旅程用
   Appium UiAutomator2/XCUITest 或 Maestro。Flutter/React Native 同时覆盖框架
   内逻辑与 Android/iOS 原生边界，WebView 单列壳、Web 内容和 bridge/IPC。
   模拟器、浏览器设备仿真和单平台 PASS 不得外推为真机或另一平台 PASS。

桌面和移动端的完整覆盖矩阵、工具路由、证据标准、真机边界与安全规则以
全局引擎 `platform-testing.md` 为硬门禁，不得只凭浏览器截图宣称完成。

### 内存专项（横跨七接口面，不是第八个接口）

目标涉及内存安全、泄漏、无界增长、峰值/OOM、生命周期对象、原生桥接或
线程/fd/mmap/GPU 等资源泄漏时，按全局引擎 `memory-testing.md` 执行：

1. 先用 `"$BUGX_PYTHON" "$(bugx engine path --name memory_probe.py)" check <target>` 验证采样/插桩/诊断通道；
2. 固定构建、符号、运行时/GC、分配器、设备与工作负载；
3. 按预热 → 稳定基线 → 重复旅程 → 静置/回收 → teardown → 独立重复实验采样；
4. 同时观察进程 RSS/PSS、托管堆、原生堆、对象和线程/fd/GPU 等资源；
5. 可疑趋势升级到 heap/core dump、allocation trace、Sanitizer 或 owner/GC root
   持有链，修复后用原工作负载复验。

内存 PASS 必须有原始时序数据、预先声明的预算/判定、足够轮次、teardown 回收
证据和独立视角交叉验证。固定的通用 MB 阈值、两个采样点或“任务管理器看起来
稳定”都不构成 PASS；RSS 高位不降也不能单独证明泄漏。

### API 接口挖掘实操（postmcp 标准流程）

postmcp 是本地 API 测试 MCP（数据存 `~/.postmcp/postmcp.db`）。独立流程：
0. **环境自检**：确认 postmcp 可用（`postmcp --version` 或工具在工具集）。
   若 `postmcp_*` 工具缺失 → 运行 `npx --yes @bencibro/postmcp@1.0.3` 安装，
   然后提示调用方重启会话（MCP 启动时加载）。
1. **项目 + 环境**：`project_create {name}` → `env_configure {name, baseUrl}`
   → `env_switch`。**发请求前必须先 `env_set_allowlist`** 配置目标域名白名单
   （空白名单拒绝所有出站请求；本地服务用 `private:localhost`）。
2. **接口探测**：`http_request {url, method}` 逐接口扫（GET 先，再 POST 变体）。
   用 `swagger_import`/`postman_import` 导入 OpenAPI/Postman 集合快速拿到
   全接口清单（接口 = 覆盖单元，配合 module-coverage 追踪）。
3. **契约轰炸（接口面核心）**：
   - **参数边界**：超长/空/负/NaN/超大数值、类型错位（数字传字符串）、
     缺失必填字段、多余字段、CJK/emoji/控制字符——断言状态码与响应结构。
   - **越权/安全**：无 token / 错 token / 他人资源 ID / 水平越权；SQLi/命令
     注入/路径穿越探测；敏感字段（密码/token/密钥）是否泄漏进响应。
   - **状态码契约**：成功该 2xx 却 4xx/5xx，错误该 4xx 却 200——响应码
     与业务语义不符就是 bug。
   - **响应结构**：`jsonpathFilter`/`assertions` 校验字段存在、类型正确、
     分页/列表结构完整。
4. **WebSocket**：`ws_connect` → 发消息 → `ws_wait`（regex 等待期望响应），
   测断连重连、消息乱序、超大消息。
5. **链式套件**：`test_run_suite` 串多步场景（登录→取 token→带 token 访问
   资源），断言每步，动态变量注入——模拟真实调用链找契约断裂。
6. **历史复用**：`history_list`/`history_get` 查已跑请求的完整响应，避免
   重复调用（效率）。

**API 面纪律**：
- 发请求前必配 allowlist（安全底线）；不探测白名单外的域名。
- 断言驱动：每个接口至少配状态码 + 关键字段断言，不"看一眼就说 OK"。
- 接口 bug 与代码 bug 同门槛：可复现（固定请求+参数）、断言证据
  （期望 vs 实际状态码/结构）、根因可溯（后端逻辑/参数处理/鉴权缺失）。

### UI 视觉挖掘实操（Playwright 标准流程）

playwright MCP 已授权（`playwright_*` allow）。UI 面固定流程：
0. **环境自检**：`"$BUGX_PYTHON" "$(bugx engine path --name setup_ui_env.py)" check`——缺依赖先
   `install` 补装并提示重启会话；工具集无 `playwright_*` 但自检通过 → 宿主未
   加载 MCP，标 `fail: 环境不可用`（不伪造截图）。
1. **导航 + 基线**：`browser_navigate <url>` → `browser_snapshot` →
   `browser_network_requests`（查资源/API 报错）。
2. **多断点截图**：`browser_resize` 375/768/1024/1440 → 每次 fullPage
   `browser_take_screenshot` + snapshot，对比溢出/裁切/断点断裂。
3. **DOM 几何断言**：`browser_evaluate` 读 `getBoundingClientRect()` 数值
   （谁遮谁、溢出多少 px）——**截图 + 几何缺一不可**。
4. **塞边界内容**：`browser_fill_form`/`browser_type` 灌长文本/CJK/emoji/
   超长串/空值 → 重截图验证截断/重叠/撑破/崩溃。
5. **交互轰炸**：重复 `browser_click`、狂按 Tab（焦点陷阱/键盘不可达）、
   `browser_navigate_back`、dialog 处理——看事件触发/焦点/状态。
6. **状态覆盖**：触发 loading/error/empty/disabled → 截图确认视觉可区分；
   `browser_console_messages` 查 JS 报错与资源失败。
7. **主题对比**：暗/浅色切换复扫；`browser_evaluate` 取色算对比度（WCAG 4.5:1）。
8. **Web 标准合规基线**：Web 项目必须运行 `web_conformance.py check`；静态检查
   `lang`、`alt`、表单/交互名称、重复 ID 和 ARIA 引用。随后用 Playwright 补齐
   键盘/焦点、Accessibility Tree、最终样式对比度、200%/400% 重排和状态反馈；
   缺少动态证据只能记为 `BLOCKED/UNVERIFIED`，不能把截图或静态结果冒充完整合规。

**UI 面纪律**：固定视口/浏览器（可复现）；交互一律用 Playwright 工具不写进
仓库；视觉 bug 与代码 bug 同门槛（可复现 + 几何/像素证据 + 根因可溯）——只报
"看着怪" = 只看了表象。

Web 标准轨道的详细命令、证据格式和结果语义见全局引擎 `web-conformance.md`。

### 已部署 Web 功能回归

公开核心只用 Playwright 或目标项目已有测试验证页面、控制台错误、失败请求、截图
和只读业务旅程。超出授权、环境或工具边界的目标不在公开核心执行范围内，必须报告
`BLOCKED/UNVERIFIED`，不得使用通用测试结果冒充目标覆盖。

### 输入构造（穷尽手段，不靠灵感）

- **边界与对抗**：空输入、超长输入、CJK/emoji 宽字符、控制字符、
  非法编码（截断 UTF-8）、极端数值（0/负/巨大）、含 `\x00` 的字节流。
- **畸形数据**：坏 JSON / YAML（不闭合、重复键、类型错位）、截断文件、
  越界路径（`../`、绝对路径、超长路径）、权限异常、符号链接环。
- **状态序列**：交互会话里连续/乱序按键、重复提交、中途 Ctrl+C、kill -9、
  进程残留后再启动（检查脏状态）、单次 exec 与长会话交叉。
- **变异模糊（并发轰）**：取一份合法输入做逐字段变异（改类型/删字段/
  换编码/放大数值）批量喂入，用脚本**并发**跑 `timeout` 包裹的目标
  （`xargs -P` / subprocess 池），记录每次 exit code + 输出，筛出异常样本。
  **并行变异矩阵**：同轮同时对多份合法输入做多组变异，不同接口面并发轰，
  一次拿回全矩阵结果，别一组一组串行等。
  **工具化**：用全局引擎 `fuzz_input.py` 自动生成变异矩阵并发轰
  （`--count`/`--jobs`/`--seed`），异常样本落盘；再用
  全局引擎 `minimize_repro.py` 把异常缩到最小复现。fuzz 广撒网找
  异常，minimize 缩到根因最集中，二者配套。
  **种子扩充（真实起点，提升命中率）**：fuzz 起点越贴近真实代码，命中
  深层解析路径的概率越高。用 `"$BUGX_PYTHON" "$(bugx engine path --name corpus_fetch.py)" --lang <语言>`
  **并发搜索 GitHub 开源项目**（python/java/rust/swift/kotlin/c/cpp/
  typescript/javascript/css），拉取热门仓库的真实源码片段去重后纳入
  `seed_corpus/<lang>.txt`；错题集的「同类排查点」也可据此补全真实语料。
  种子库越丰富，fuzz 命中率越高——这是低成本的挖掘力杠杆。
- **覆盖率引导（深度提升，从盲挖到定向挖）**：不知道哪条路径没测到就是
  盲挖。对 Python 目标用 `python -m coverage`（`coverage run` + `report`）
  定位未覆盖行/分支，**定向补测未覆盖路径**；对其他语言用目标自带的
  覆盖工具（go test -cover / cargo tarpaulin / JS c8）。**覆盖报告 = 未挖掘
  地图**——发现"某解析分支/错误路径 0 覆盖"就是下一个高命中靶点。

### 行为 oracle（无源码怎么判「这是错误」）

黑盒下，「真实错误」= 可稳定观察到的行为异常，任一命中即可立案：

- **崩溃**：非零退出码、traceback / 栈转储、段错误（`-11`）、abort。
- **挂起/活锁**：`timeout` 触发（进程不退出不响应），交互界面冻结。
- **数据损坏**：写入的文件 / DB 重启后错乱、丢失、半截；同一操作两次结果不同。
- **输出不符**：与 `--help`/README/规范声明相悖；同输入多次运行输出不稳定。
- **内存/资源异常**：按 `memory-testing.md` 的预热、稳定基线、重复旅程、静置与
  teardown 协议，出现 Sanitizer/确定性泄漏报告、OOM/Jetsam、关键对象持有链，
  或 live heap/PSS/对象/线程/fd/GPU 资源呈可重复无界增长。RSS 高位不降、两个
  采样点或没有对照的单次 heap snapshot 不足以单独立案。
- **安全**：读到本不应读的文件（路径穿越生效）、密钥/敏感信息泄漏到输出、
  注入（命令/格式化字符串）成功、越权执行。
- **状态机违例**：TUI 死屏/乱屏/光标异常、命令后状态自相矛盾、退出路径
  走到错误分支。
- **横向一致性（跨接口面互证，高价值判据）**：同一根因 bug 会在多个接口面
  同时体现——CLI 报错、API 返回异常、UI 行为错乱、数据文件损坏，往往指向
  同一个底层缺陷。**一面发现异常后，去其他接口面用等价输入验证是否同一
  根因**：① 互证（另一面也异常 = 更可能是真 bug 而非单面偶然）；② 合并
  （多面异常同根因 = 合并成一条发现，别拆成多单凑数）。跨面一致 = 正常；
  跨面矛盾（一面说有数据、另一面说无）= 状态不一致，也是 bug。
- **差分测试（differential，高价值判据）**：找**等价实现**对同一输入做对照
  ——当前版本 vs 上一版本、自研工具 vs 标准工具（`python -m json.tool` vs
  手写解析器）、同语言的两种实现、不同语言实现同一协议。**同输入产出应
  一致**；若两个等价实现行为分歧 = 至少一个是错的（或两者契约理解不同，
  需查证）。差分能抓到"单看目标看不错、对照才显形"的隐性 bug（编码差异、
  边界舍入、协议实现偏差）。
- **契约转断言（conformance，防标准漂移）**：测试前先把契约（`--help`/
  README/OpenAPI/版本说明）**转成显式断言清单**——"该 2xx 成功 / 该字段存在 /
  该参数必填 / 该命令应输出 X"。测时逐条对断言，而不是边测边临时想"这算
  不算错"。契约转成可执行断言 = 判断标准固定，不随测随漂移。
- **幂等/可逆性（隐性错误判据）**：同一操作**重复执行应产生相同结果**
  （幂等）；删除/撤销/重置操作**应可逆**（undo 后状态应恢复）。不幂等/不可逆
  往往是状态管理缺陷——"幂等"顺手就能验证（同一请求/命令跑两次对比）。
- **UI 视觉违例**（接口面 4/6/7）：任一命中即可立案；Web 用 DOM 几何，
  桌面/移动按全局引擎 `platform-testing.md` 使用窗口/安全区/软键盘
  几何和语义/可访问性树——
  - **布局崩塌**：元素互相遮挡/重叠/错位、内容溢出容器被裁、横向滚动条
    意外出现、flex/grid 在断点处（375/768/1024/1440）布局断裂。
  - **文案损坏**：长文本/中文/emoji/超长串导致截断、换行错乱、文字重叠
    或撑破容器。
  - **交互失效**：按钮/链接点击无响应、hover/focus 态不生效、焦点陷阱
    （键盘 Tab 困住出不来）、`aria-label` 缺失导致无障导航失效。
  - **状态反馈缺失**：loading/error/empty/disabled 视觉上无法区分、
    表单校验错误不展示、toast/弹窗出现又立即消失。
  - **对比度/主题**：前景背景对比不足（WCAG 4.5:1 失败）、暗色/浅色模式
    切换后某区域不可读或颜色串线。
  - **渲染异常**：闪烁/白屏/错位图片、元素尺寸跳变（CLS）、
    截图与 DOM 几何断言矛盾。
- **API 契约违例**（仅接口面 5，postmcp）：任一命中即可立案——
  - **状态码不符**：成功该 2xx 却 4xx/5xx，错误该 4xx/5xx 却 200——响应码
    与业务语义不符。
  - **响应结构错位**：字段缺失/多余、类型错（字符串当数字）、嵌套结构断裂、
    分页/列表结构不完整、JSONPath 断言失败。
  - **参数校验缺失**：必填缺失仍返回成功、超长/负值/NaN/类型错位被静默接受。
  - **越权/未鉴权**：无 token 可访问受保护资源、他人资源 ID 可读（水平越权）、
    token 过期仍有效、敏感字段（密码/token/密钥）泄漏进响应。
  - **注入成功**：SQLi/命令注入/路径穿越/NoSQL 注入在响应中可观察到。
  - **协议异常**：WebSocket 断连、消息乱序/丢失、超大消息崩溃、GraphQL 字段
    解析错误、OAuth2 流程异常。

### 举证/复现纪律（与白盒同一道生命门槛）

黑盒发现必须满足可复现，证据格式：

```
目标：<可执行路径 + 版本>
复现：<完整命令，含所有参数/输入/环境>
观察：<exit code / stderr / 屏幕快照 / 资源趋势>（附原文）
预期：<符合规范的行为>
复现次数：≥2 次确定性复现；输入已最小化（删除冗余仍触发）
```

- 崩溃类：抓 traceback 全文或退出码；能用最小输入复现才算数——大输入先
  用 `minimize_repro.py` 自动最小化（最小输入 = 根因最集中）。
- 挂起类：记录 timeout 秒数与「进程未退出」证据，别把慢当挂起（先给足超时）。
- **内存/资源类**：给出目标/符号/运行时/GC/分配器指纹，预热与稳定基线，
  重复工作负载、采样频率、静置/GC/teardown、至少 2 次独立实验；附原始
  CSV/JSON 与 heap/core dump、trace、Sanitizer 或持有链等独立证据。必须报告
  基线/峰值/稳定值/斜率与预算来源，不能用固定通用 MB 阈值判所有应用。
- **UI 视觉类**：截图（`fullPage` 或元素级）+ 视口尺寸/断点 + DOM 几何断言
  （`getBoundingClientRect` 数值：谁遮挡谁、溢出多少 px、哪个断点触发）+
  复现步骤（打开哪个 URL、视口多大、点哪里）。截图与几何断言缺一不可——
  只有截图「看着怪」不立案，必须有可量化的几何/像素证据。
- **API 接口类**：请求（method + URL + 完整 headers/params/body）+ 断言
  （期望 vs 实际状态码、JSONPath 字段、响应结构）+ 响应原文（关键字段）。
  「复现」= 同请求重放，「预期」= 契约定义的行为。无契约可依时以 REST 语义
  惯例/OpenAPI 为准；纯风格差异（字段命名约定）不立案。
- **桌面/移动类**：目标构建指纹 + 主机/设备/Simulator + OS + 权限/网络/
  语言/初始状态 + 可重复旅程；UI 证据必须包含截图/录屏和语义/可访问性树或
  几何，崩溃/卡死必须包含日志、堆栈、进程状态或 trace，数据变化用独立 API/
  数据库/回执交叉证明。模拟器结果必须明确标注，不得写成真机 PASS。
- **harness 自检**：每个异常样本先独立复核「不是 harness 写的」——用最
  原始的命令行方式（不经过 harness）重跑同一输入，异常仍在才算目标的错；
  harness 自己的异常（spawn 失败、读超时逻辑错、字节解码错）记录为 harness
  bug，不得冒充目标 bug。
- 无法确定是否预期行为时：先查 README / `--help` / 版本说明确认契约，
  查不到契约的「疑似」不立案（避免把设计取舍当 bug）。
- **UI 视觉「疑似」处理**：布局/配色若与设计规范或 Figma 源有出入，
  先确认该规范是否为当前契约；无契约可依的「审美差异」不立案（避免把
  设计取舍当 bug），但**功能性视觉错误**（溢出裁切、焦点困住、状态无法
  区分）不依赖设计稿即可判定。

### 与多轮机制结合

- 每轮勘察时选择：白盒源码审计 或 黑盒成品测试，可轮换目标避免原地打转。
- **UI 轮**：接口面 4 的浏览器目标（Web 应用/设计系统/前端页面），
  用 Playwright 起页面 + 截图 + DOM 断言，跨断点/状态/主题扫 UI 视觉违例。
- **API 轮**：接口面 5 的服务目标（REST/GraphQL/WebSocket 后端），用
  postmcp 起接口测试——Swagger/Postman 导入全接口清单 → 契约轰炸（参数
  边界/越权/注入/状态码）→ 断言取证。可与 UI 轮配合：同一应用的 API 层
  （postmcp）与前端交互层（Playwright）各挖各的，发现互相印证。
- **桌面轮**：接口面 6，按 OS/框架路由原生驱动，覆盖窗口、系统集成、安装/
  升级与恢复状态；坐标点击只能作为记录理由的最后兜底。
- **移动轮**：接口面 7，Android/iOS 分别验证；模拟器快筛后，硬件、推送、
  生物识别、OEM/签名/温控等边界必须由代表性真机证明或列为 `BLOCKED/NOT_RUN`。
- **内存轮**：横跨已选接口面，用相同旅程对齐进程 sampler、托管/原生 heap 和
  资源计数；先趋势筛查，再对可疑点采分配栈/持有链，不能把 debug/模拟器结果
  直接外推到 release-like 真机或用户机器。
- 黑盒发现的真实错误同样 `life +1`；同样适用欺诈红线（编造输出/假装复现）。
- 跨轮避免重复测同一个目标/同一个样本；换版本、换接口面、换输入域。

### 安全与范围守则（授权边界的硬线）

- **敏感信息**：审计过程中读到的密钥/凭据/API key/个人数据（如
  `opencode.json` 的 apiKey、`.env`、配置文件）**禁止写入报告、日志、输出、
  findings**——只可记录「存在敏感信息泄漏」这个事实，不粘贴原文。
  违反即泄漏，按欺诈处理。
- **黑盒目标范围**：外部二进制/服务的黑盒测试限于本次审计范围（仓库相关
  成品、用户指定的目标）。不对范围外的系统做破坏性操作。
- **破坏性操作**：允许为验证 bug 做有限破坏（改代码、跑测试、kill 进程、
  起沙箱），但不得损坏仓库外的用户数据；验证完恢复现场。
- **设备操作**：清除应用数据、卸载、重置模拟器、撤销权限、抹除设备、安装
  证书、Root/越狱、Frida 注入、支付/发消息/生产写入都必须另行获得明确授权。
- **受限目标**：未获得明确授权、无法固定目标指纹或缺少必要工具的目标不得执行，
  公开核心只保留 `BLOCKED/UNVERIFIED` 边界记录。
> 运行前置：BugX 核心要求 Python `>=3.11`。本文中的 `python3` 命令只表示已通过版本
> 校验的解释器；系统 Python 3.9 不满足契约，请先按 `core/contracts/AGENT_GUIDE.md` 设置
> `BUGX_PYTHON`，或优先使用 `bugx` CLI。
