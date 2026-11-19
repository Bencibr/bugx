# BugX 文档治理标准（Documentation Governance Standard）

> **文档性质**：全仓文档体系的**唯一顶层规范**。任何目录、任何文档的命名、分区、状态、索引与维护责任，以本文件为准。
> **适用范围**：`docs/` 全域 + 仓库根层文档（`README.md`、`AGENTS.md`、`CHANGELOG.md`）+ `docs/08_internal/` 全部资产。
> **维护责任**：结构变更（新增目录、移动文件、更换命名族）必须先改本文件，再改文件系统。
> **最后审计**：2026-11-07（对应 `DOC-259`）

---

## 0. 一句话原则

> **目录结构本身必须能回答问题。人不需要先读 README 才知道"发布规范在哪"。**

`README` 是索引与导航，**不是**弥补结构混乱的补丁。如果一个目录需要靠 README 才能看懂，说明结构错了。

---

## 1. 两层分区：读者决定位置（Layer / Audience）

`docs/` 只分两层，**不允许第三层**（`docs/a/b/c/file.md` 一律禁止，见 §8）：

| 层 | 路径 | 读者 | 判定问题 |
|---|---|---|---|
| **公开层** | `docs/<分类>/` | 终端用户、外部集成者、AI 宿主 | 「外部人能看吗？」能 → 公开层 |
| **内部层** | `docs/08_internal/<分类>/` | 核心维护者、框架开发者 | 「含工单号、历史决策、未实现规划吗？」含 → 内部层 |

**硬边界（AGENTS.md 三受众隔离）**：公开层文档**严禁**出现 `DOC-xxx`、`BUG-xxx`、`PE-xxxxxx-xxx` 等内部工单编号；严禁泄露未实现规划。

---

## 2. 公开层分类（`docs/`）

| 序号 | 目录 | 放什么 | 判定标准 |
|---|---|---|---|
| 01 | `01_guides/` | 操作指南：怎么装、怎么用、命令怎么敲 | **读者要"照着做"** |
| 02 | `02_architecture/` | 架构、设计规格、流程时序、命名与治理规约 | **读者要"理解系统"** |
| 03 | `03_ai/` | AI 宿主运行时上下文与协议契约 | **读者是 AI Agent** |
| 04 | `04_reference/` | 速查表等参考型资料（`CHANGELOG.md` 按宪法留在仓库根） | **读者要"查一个值"** |
| 05 | `05_release-notes/` | 微版本流水账本（自动生成） | **CI 与追溯用**，不面向人阅读 |
| 06 | `06_diagrams/` | 图表资源（`.svg`/`.png`/`.mmd`） | **被引用的资源**，非阅读对象 |
| 07 | `07_distribution/` | 分发说明与打包资产说明 | **发布链路用** |
| 08 | `08_internal/` | 内部研发资产 | **禁止外传给用户** |

`docs/` 根层只允许：`README.md`、`README_EN.md`（2026-11-10：本文件已迁至 `02_architecture/07_`——原根层 `03_` 序号与 `03_ai/` 目录撞号，根层不再有规范文件）。

---

## 3. 内部层分类（`docs/08_internal/`）

**2026-11-07 收敛**：以当前架构为基线，内部层只保留**代码或门禁真实读取**的资产。
提案、审计、研究、任务等演进过程资产已全部删除——它们记录的是「当时怎么想」，
而当前架构真值由 `docs/02_architecture/` 承担。

| 目录 | 放什么 | 生命周期 | 编号族 |
|---|---|---|---|
| `08_standards/` | **开发规范**：写代码、写测试、迁移宿主时必须遵守 | 长期稳定 | `<序号>_<主题>` |

仅此一个目录。判断是否该新增子目录的标准只有一条：**是否有代码或门禁会读取它**。
仅被人查阅、不参与任何自动化校验的文档，写进 `docs/02_architecture/` 对应章节即可。

历史演进记录（`02_prd` / `03_core-requirements` / `04_proposals` / `05_defects` /
`06_todos` / `07_tasks` / `10_audits` / `11_research` / `12_drafts` / `13_history`）
统一走 git 历史追溯：`git log --all -- docs/08_internal/`。

---

## 4. 命名规范

### 4.1 通用硬规则

| 规则 | 说明 |
|---|---|
| **禁止中文文件名** | 破坏跨平台路径解析与 `git ls-files` 输出（历史上已踩坑：含中文名的提案曾被脚本漏扫） |
| **禁止空格** | 用 `_` 连接 |
| **大写为主** | 编号族与专题名一律大写下划线；仅 `02_prd/`、`05_defects/` 等目录名用单数小写 |
| **编号单调递增、不复用** | 废弃保留原号 + 标 `SUPERSEDED`，不得回收 |
| **编号与文件名一致** | `DOC-259_XXX.md` 正文首行必须是 `# DOC-259：...` |

### 4.2 各编号族格式

```
001_PRD-001_DOC_GOVERNANCE_TAXONOMY.md      PRD-<三位>_<大写下划线短名>.md
001_CORE-000_INDEX.md                       CORE-<三位>_<大写下划线短名>.md
249_DOC-259_DOCS_INFORMATION_ARCHITECTURE_REFACTOR.md
DEF-0001_FOREIGN_BUGX_AUTO_HEAL.md      DEF-<四位>_<大写下划线短名>.md
001_TODO-001_cline_sandbox_gc.md            TODO-<三位>_<小写下划线短名>.md
<主题>_<YYYY-MM-DD>.md                   审计/预研/草稿（无编号族）
```

> **为什么 `DEF-` 用四位而其余三位**：缺陷数量级预期高于提案，四位留足空间避免过早进位。

---

> **与代码命名规范的关系**：本文件 §4 只规定 `docs/` 下的**文档**命名。`core/`、`tests/`、`interfaces/` 等**代码与测试模块**命名由 [`05_ARCH_NOMENCLATURE_GUIDE.md`](05_ARCH_NOMENCLATURE_GUIDE.md) 定义。两者互不越界。

## 5. 状态字段规范

### 5.1 唯一载体

状态必须以 **YAML 机器字段**出现，位于文档头部：

```markdown
> status: AUDITED_OK
> status_note: 可选补充说明（不进词表校验）
```

**禁止**使用 `状态：X`、`> 状态：X`、`- **状态**: X` 等变体——历史上曾出现 5 种写法混用，导致 51 篇提案无法被机器识别。

### 5.2 词表（`scripts/verify_doc_status.py` 为唯一权威）

| 状态 | 适用 | 含义 |
|---|---|---|
| `PROPOSED` | proposals | 提案中，未审计 |
| `DRAFT` | prd | 草案，尚可推翻 |
| `AUDITED_OK` | 全部 | 审计通过，**未合入代码** |
| `ACCEPTED` | proposals | 已接受，未排期 |
| `NEEDS_CHANGE` | 全部 | 审计结论：设计需修改 |
| `IMPLEMENTED` | proposals | 已合入**且有验收证据** |
| `SUPERSEDED` | 全部 | 已被取代，须注明替代基线 |
| `UNKNOWN` | 存量 | 缺失状态，须登记 `01_index/02_DOC_STATUS_BACKLOG.md` |
| `OPEN`/`FIXED`/`VERIFIED`/`CLOSED`/`REOPENED`/`WONTFIX` | defects | 缺陷生命周期 |
| `REJECTED` | prd | 需求被否决 |

> **红线**：未合入并验收前标 `IMPLEMENTED` 属**虚标**，违反 AGENTS.md「严禁虚标完成」。

---

## 6. 目录与文件必须带序号

> **命名排序即可读顺序。** 禁止纯字母序排列——人打开目录看到 `ai/ architecture/ diagrams/` 无法判断先看哪个。

| 规则 | 说明 |
|---|---|
| **目录名以两位序号开头** | `01_guides/`、`02_architecture/`……补零保证 `ls` 排序稳定（`10_` 不会排到 `2_` 前） |
| **序号含义 = 阅读顺序** | 与 `docs/README.md` 的章节顺序**必须一致**，不允许两套顺序 |
| **`README.md`、`TODO.md` 不加序号** | 工具按固定文件名定位目录索引（`verify_directory_indices.py:226` 硬编码 `README.md`），加前缀会击穿 20 个目录的守护 |
| **目录下每个文件都带序号** | `01_USER_GUIDE.md`、`02_COMMANDS.md`……序号即阅读顺序 |
| **领域编号文件采用 `序号_原名` 双段式** | `023_DOC-024_xxx.md`：前段是目录内阅读序号，后段保留 `DOC-` 编号供跨文档溯源。两者职责不同，缺一不可 |
| **`05_release-notes/` 用三位序号** | `001_RELEASE_NOTES_0.1.0.md`……`599_`。三位补零是为了让 `ls` 的字母序**等于版本序**——版本号本身 `0.10.0` 会排在 `0.2.0` 前，光靠版本号看不出先后 |
| **`README.md` / `TODO.md` 恒不加序号** | 工具按固定名定位（`verify_directory_indices.py:226`、`dev_doc.py` 等） |
| **新增目录必须补号并重排** | 插入分类时整体重排，不留空号、不用 `01a_` 变体 |

**当前编号（`docs/`）**：`01_guides` → `02_architecture` → `03_ai` → `04_reference` → `05_release-notes` → `06_diagrams` → `07_distribution` → `08_internal`

**当前编号（`docs/08_internal/`）**：`01_index` → `02_prd` → `03_core-requirements` → `04_proposals` → `05_defects` → `06_todos` → `07_tasks` → `08_standards` → `09_release` → `10_audits` → `11_research` → `12_drafts` → `13_history`

> **顺序依据**：`08_internal` 按「先找入口（索引）→ 再提需求 → 看既有基线 → 写方案 → 记缺陷 → 排待办 → 查规范 → 发版 → 读审计/预研/草稿/归档」的生命周期排。

## 7. 每个目录必须有 README

### 6.1 必备六节

| 节 | 必须回答 |
|---|---|
| **定位** | 一句话：这个目录放什么，不放什么 |
| **命名规则** | 文件名怎么起，编号族是什么 |
| **状态机** | 条目有哪些状态，如何流转 |
| **必备段落** | 每份文档必须含哪些章节 |
| **索引** | 条目清单（或声明采用汇总索引，见 §7.3） |
| **维护责任** | 何时必须更新本 README |

### 6.2 "实时维护"的可执行定义

> **任何新增 / 删除 / 改名，必须在同一次提交内同步 README 索引。**

这不是承诺，是**门禁**：`scripts/verify_directory_indices.py --check` 会拒绝未同步的提交。

### 6.3 汇总索引豁免（大体量目录）

条目 ≥50 且高度同构的目录，可在 README 首部声明：

```markdown
<!-- index-policy: aggregate -->
<!-- 为何不逐文件罗列：<理由> -->
<!-- 条目去哪查：<可达入口> -->
```

**受白名单约束**（`AGGREGATE_ALLOWED`），白名单外使用会被判脏。当前白名单：
`05_release-notes/`、`08_internal/10_audits/`、`08_internal/04_proposals/`、`08_internal/03_core-requirements/`、`08_internal/11_research/`。

> 该机制存在的原因：601 篇微版本账本逐文件罗列会让 README 膨胀到不可读，且每次提交都要改——这反而会促使人关掉守护。**豁免必须显式且可审计，不能是静默跳过。**

---

## 8. 深度限制

> **`docs/` 下最大深度为 3 层**（`docs/08_internal/01_standards/X.md` 合法；`docs/08_internal/a/b/X.md` 非法）。

理由：超过 3 层后，人在终端里定位需要多次 `ls`，而 AI 需要多次 glob——两层都变慢。若某类内容需要第 4 层，说明该分类本身划分错误。

---

## 9. 五向溯源契约（Traceability）

```
PRD ──→ AC ──→ 测试模块
 │       │         │
 └──→ CORE   DEF ──┘
              │
         Release Notes
```

| 起点 | 必须能指向 | 载体 |
|---|---|---|
| `PRD-xxx` | 原子需求 `R-*` + 验收条件 `AC-*` | PRD 正文表格 |
| `CORE-xxx` | `AC-*` + 实现节点（代码路径 + 可观察产物） | CORE 正文 |
| `AC-*` | 测试模块（`tests.*`） | PRD/CORE 表格内「回归测试」列 |
| `DEF-xxxx` | 触发它的 `AC-*`/`PRD-*`/`CORE-*`/`DOC-*` | 缺陷「现象」段 |
| Release Notes | 本次关闭的 `DEF-*` | 发布说明 |

> `DEF-*` 缺少回指会被 `verify_doc_status.py` 拒绝。豁免：`severity=low` 且无 AC 关联。

---

## 10. 破坏性重构：路径引用全量重写，无豁免

> **本仓处于破坏性改造期（AGENTS.md：允许破坏性变更，不考虑向后兼容）。**
> 不存在"历史文档不可改动"的豁免——**死链比历史叙述更糟**。

结构重构时，`docs/08_internal/10_audits/`、`04_proposals/`、`13_history/` 等时点文档的**路径引用必须一并重写**，与全仓同步。理由：

| 论据 | 说明 |
|---|---|
| 死链是真实缺陷 | 保留旧路径会让文档指向不存在的文件，读者与 AI 都会踩空 |
| 两套路径 = 两套权威 | 部分文档用新路径、部分用旧路径，检索结果不一致 |
| 维护成本 | 若保留旧路径，后续每次重构都要判断"这份算历史还是算活体" |

**唯一保留的约束**：可以改路径与链接，但**不得篡改结论本身**——审计的发现、裁定的对错、缺陷的定性，这些是事实记录，改了就变成伪造。路径是导航，结论是事实，二者不同。

| 允许改 | 禁止改 |
|---|---|
| 文档内引用的路径、链接、目录名 | 审计结论、设计裁定、缺陷定性 |
| 文档挪动位置 | 把"当时发现的问题"改写成"当时没问题" |
| 追加「后续结论：已被 X 推翻」 | 删除历史结论 |

## 11. 门禁工具（唯一的执行手段）

| 工具 | 守护什么 | 失败行为 |
|---|---|---|
| `verify_directory_indices.py --check` | README ⇄ 物理文件双向一致 | 非 0 退出并指名缺失 |
| `verify_doc_status.py` | 状态字段格式、词表、UNKNOWN 登记、DEF 溯源 | 非 0 退出并列出问题 |
| `dev_doc.py check --doc <path>` | 设计项 5+1 问审计门禁 | 门禁未闭合则拒绝进入实现 |
| `docskill status` | 死链、三受众隔离泄漏、健康分 | 报告告警 |
| `bump_version.py --check` | 33 处版本声明一致 | 非 0 退出 |

---

## 12. 新建文档的决策树

```
要写一份新文档
  ├─ 它是"新需求"吗？            → internal/prd/PRD-xxx（成熟后升 CORE-xxx）
  ├─ 它是"技术方案"吗？          → internal/proposals/DOC-xxx（先跑 dev_doc plan）
  ├─ 它是"某个 bug 的原因+修复"？ → internal/defects/DEF-xxxx
  ├─ 它是"一次审计的结论"？      → internal/audits/<主题>_<日期>.md
  ├─ 它是"要做但还没做"？        → internal/TODO.md 加一行 + internal/todos/TODO-xxx
  ├─ 它是"怎么执行某个流程"？    → internal/tasks/
  ├─ 它是"技术可行性调研"？      → internal/research/
  ├─ 它是"写代码必须遵守的约束"？ → internal/standards/
  ├─ 它是"发版流程"？            → internal/release/
  ├─ 它是"给用户看的操作指南"？  → guides/
  ├─ 它是"系统设计/架构"？       → architecture/
  ├─ 它是"给 AI 的上下文"？      → ai/
  └─ 它是"随手草稿"？            → internal/drafts/（必须写出口）
```

---

## 13. 违规处置

| 违规 | 处置 |
|---|---|
| 公开层出现内部工单号 | 立即改写为技术表述；`docskill status` 应报 0 告警 |
| 未合入却标 `IMPLEMENTED` | 改回 `AUDITED_OK`；`verify_doc_status.py` 拒绝 |
| 目录缺 README | `verify_directory_indices.py --check` 拒绝提交 |
| 新增文件未入索引 | 同上 |
| 历史审计被改写 | `git diff` 审查时拒绝；视为伪造结论 |
| 超过 3 层深度 | 重构分类，而非加深路径 |
