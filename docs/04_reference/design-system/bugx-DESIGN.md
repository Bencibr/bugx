# BugX Studio DESIGN（自有设计规范 · 2026-11-12）

> 本文档是 BugX WebUI（`core/webui/static/`）的**唯一设计规范**，与同目录 9 个
> 品牌 DESIGN.md（Apple HIG / Linear / Vercel 等）同为维度⑥审计的对照基准。
> 全部数值由 `design-tokens.css` 代码真值提取（value-equal），规范变更必须
> 先改 token 再改本文档（文档是 token 的镜像，禁止反向漂移）。

## 1. 字号（11 档，rem）

| Token | 值 | 语义 |
|-------|-----|------|
| `--fs-micro` | 0.625rem | 徽标 / kbd / 时间戳等 10px 级微文本 |
| `--fs-caption` | 0.72rem | 辅助说明、表格元数据 |
| `--fs-label-sm` | 0.75rem | 小标签、下拉项 |
| `--fs-label` | 0.78rem | 表单标签、菜单项 |
| `--fs-body` | 0.82rem | 正文默认 |
| `--fs-body-lg` | 0.85rem | 强调正文、输入框 |
| `--fs-emphasis` | 0.90rem | 重点强调 |
| `--fs-display-sm` | 1.05rem | 卡片标题 |
| `--fs-display` | 1.15rem | 区块标题 |
| `--fs-display-lg` | 1.40rem | 页面标题 |
| `--fs-display-xl` | 1.60rem | 主视觉标题 |

**规则**：`font-size` 禁止散装值（px/rem 直写）；文本 6 档（caption..emphasis）
相邻间距 ≥0.03rem（轮 72 定稿）；`em` 仅限相对父级语义场景。

## 2. 间距（2px 线性坡道）

`--sp-1..16` = 2..32px（步进 2px）+ 延伸位 `--sp-18`(36px) / `--sp-24`(48px)。

**规则**：`padding/margin` 必须 var(--sp-*) 消费；离坡奇数值禁止新增
（1px 线宽与负 margin 为保留例外）。

## 3. 圆角

`--radius-xs/sm/md/lg/xl` = 4/6/8/10/12px；`--radius-pill`(999px) /
`--radius-circle`(50%)。组件按尺寸就近取档，禁止散装奇数值。

## 4. 色彩语义

| 组 | Token（示例） | 用途 |
|----|--------------|------|
| 品牌 | `--bx-apple-blue` #0a84ff / `--bx-apple-blue-deep` #0071e3 / `--bx-sky-600` #0284c7 | 主操作、链接、焦点环 |
| 中性 | `--bx-slate-50..900` / `--bx-apple-gray-*` | 文本、边框、背景 |
| 功能 | `--bx-emerald-500/600` 成功、`--bx-apple-red` 危险 | 状态反馈 |
| 玻璃 | `--bx-glass-05..12` | 拟态面板 alpha 梯度 |
| 阴影 | `--bx-black-04/06/08` | 分层投影 |

## 5. 交互状态（四态强制）

| 状态 | 要求 | 现状（轮 97 守卫） |
|------|------|------------------|
| hover | 可交互元素必须有 hover 反馈 | ✓ 135+ 处 |
| focus | **键盘焦点必须可见**：`:focus-visible` 焦点环，禁止裸 `outline: none` | ✓ 全局守卫：`outline: 2px solid var(--bx-sky-400); offset 2px`（Playwright Tab 5/5 验证） |
| disabled | 禁用必须可辨识 | ✓ 全局守卫：`opacity 0.5 + cursor: not-allowed`（含 `[aria-disabled]`） |
| active | 点击按压反馈 | ⚠ 仅 2 处（低优先级遗留） |

**规则**：新增 `outline: none` 必须同行补 `:focus-visible` 替代（审计红线）。

## 6. z-index 语义层级

`--z-hidden(-9999)` → `--z-raised(1)` → 表头(2) → 单元悬停(3) → 菜单(10)
→ 详情栏(20) → 视图容器(50) → tooltip(100) → dropdown/modal(9999) →
toast/popover(100000) → backdrop(100001) → confirm(100005) → settings(10⁷)。
**禁止裸数字 z-index**。

## 7. 审计 Checklist（机械可查）

1. `font-size` 散装值 = 0（构建产物 css 除外）；
2. `padding/margin` 离坡值 = 0（保留例外白名单）；
3. 全局 `:focus-visible` 键盘焦点守卫在位（轮 97 采用单点全局守卫模式——
   逐元素 focus 规则不要求，焦点环由全局规则覆盖全部可交互元素）；
4. 裸 `outline: none`（无 :focus-visible 补偿）= 0；
5. 可交互元素禁用态有全局降级；
6. z-index 裸数字 = 0；
7. 新增 token 必须先落 `design-tokens.css` 再消费。
8. dark 主题文本 ink on slate-900 ≥4.5（F-RSI-20261115-119）；三 ink
   必须与亮色显式分流，暗色沿用亮色值即违规（守卫脚本 C9 自动断言）。
9. `!important` 数量不得超过各文件棘轮预算（F-RSI-20261110-023，守卫 C10）；
   新增 `!important` 前必须先证明「删了会改变渲染」，否则视为架构回退。

## 8. 已知范围约束

- `assets/index-*.css` 为 **Vite 构建产物**（Tailwind 刻度），无源不入库，
  不在治理范围（F-023 结论）；其字号（16px/14px）不适用本规范。
- 构建产物的源工程入库后，本规范第 7 节 checklist 扩展到源工程。

## 8bis. `!important` 治理（F-RSI-20261110-023，2026-11-17 结案）

**加载序事实**：`index.html` 先引构建产物（第 35 行），再引 design-tokens +
`00..12` 手写分节（36-49 行）。故同一元素同一属性上，**同特异性由手写侧胜出**，
只有构建产物特异性严格更高时才需要 `!important`。

**实测收敛**：574 → 322 处（移除 252，-43.9%）。判定与验证工具：

| 工具 | 作用 |
|------|------|
| `scripts/verification/css_cascade_diff.py` | 级联差分：从 DOM 生产代码提取真实 class 共现集合（886 组）作为元素模型，逐元素 × 11 状态 × 逐属性求胜出声明，比较删 `!important` 前后的最终取值 |
| `scripts/verification/css_important_audit.py` | 特异性竞争审计：逐条判定「可删 / 承重」，输出承重项的构建产物竞争选择器 |

**保留的 322 处三类依据**（守卫 C10 的棘轮预算即由此得来）：

1. **承重**——构建产物在同一元素同属性上特异性更高。差分实证，例如
   `.mode-selector-trigger` 的 `height/min-height/border-radius/padding`：删除后
   被 `.input-footer .model-selector-trigger` 覆盖为 28px/44px/16px，触发器高度直接崩
   （与 F-RSI-20261112-101 的 218px 像素差分实测互相印证）。
2. **内联冲突**——JS 以 `el.style.X = ...` 写同名属性（本仓 41 个属性）。author
   `!important` 是样式表压制内联样式的唯一手段，删除即行为变更。
3. **不可建模**——选择器含 ID、右端无 class、或含动态拼装的类，无法做级联归属，
   一律保守保留。

**已被证伪的两条原主张**：

- 「0 个间距设计 token」——`design-tokens.css` 已有 18 个 `--sp-*`（2px 线性坡道
  + 18/24 延伸位），被消费 536 次，文件头即标注「F-023 v1」。
- 「仅 2 个媒体查询」——布局走 `min(720px,95vw)` / `%` / `vw` / flex 流式方案；
  实测无任何「硬宽 ≥360px 且无 `vw`/`%`/`max-width` 兜底」的元素，不存在可证的
  小视口溢出。**不凭空添加断点**（无证据的断点属设计变更，不属缺陷修复）。


## 9. 对比度（F-RSI-20261113-113，WCAG 2.1 AA）

**文本场景必须消费 ink 变体**（on white 实测）：

| Token | 值 | on white | 用途 |
|-------|-----|---------|------|
| `--bx-text-main` | slate-900 | 17.85 ✓ | 正文 |
| `--bx-text-muted` | slate-500 | 4.76 ✓ | 次要文本 |
| `--bx-text-tertiary` | slate-500 | 4.76 ✓ | 三级文本（替代 slate-400/gray-400 直用于文本） |
| `--bx-link-ink` | #0369a1 | 5.93 ✓ | 链接文本（sky-600 4.10 不达标，仅图形/大字） |
| `--bx-danger-ink` | #dc2626 | 4.83 ✓ | 危险文本（apple-red 3.55 仅图形/大字/badge） |
| `--bx-success-ink` | #047857 | 5.48 ✓ | 成功文本（emerald-500 2.54 仅图形/badge） |

品牌原色（apple-blue/sky-600/apple-red/emerald-500）保留用于图形、边框、
badge 背景、大字标题（≥3:1 大字豁免）；守卫脚本 C8 自动断言。

**dark 主题强制分流**（F-RSI-20261115-119）：亮色 ink（#0369a1/#dc2626/
#047857）on slate-900 仅 3.01/3.70/3.26，全部不达标；html.dark 块内必须
覆盖为深色高对比原色（实测 on slate-900）：

| Token | dark 值 | on slate-900 |
|-------|---------|-------------|
| `--bx-link-ink` | sky-400 #38bdf8 | 8.33 ✓ |
| `--bx-danger-ink` | apple-red #ff3b30 | 5.03 ✓ |
| `--bx-success-ink` | emerald-500 #10b981 | 7.04 ✓ |
| `--bx-text-main` | slate-50 | 17.06 ✓ |
| `--bx-text-muted` | slate-400 | 6.96 ✓ |

守卫脚本 C9 断言：dark cascade 末值（design-tokens 基底 + 00-base 覆盖）
五 token on slate-900 ≥4.5，且三 ink 与亮色定义显式分流。

**三 dark 通道同步义务**（F-RSI-20261115-121）：dark 语义有三条生效通道——
`html.dark`/`:root[data-theme=dark]`（JS 主动同步）、`@media
(prefers-color-scheme: dark)`（内联主题脚本异常降级或非标准入口的兜底）。
ink 覆盖必须在**每条通道**的 dark 块内各自声明（CSS 无跨选择器共享机制），
漏任何一条即该通道用户亮色 ink on slate-900 = 3.01/3.70/3.26 全不达标；
守卫 C9 断言通道级同步。
