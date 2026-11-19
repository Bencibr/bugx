# Design System：大厂设计规范基准（DESIGN.md）

> **定位**：AI 宿主生成 UI 时的设计规范对照基准。BugX 的 UI 设计质量审计（第六维度）以这些 DESIGN.md 为基准文件，对照 AI 生成的截图/页面逐项检查。
> **来源**：[voltagent/awesome-design-md](https://github.com/voltagent/awesome-design-md)（Google Stitch DESIGN.md 概念，167 个真实网站的提取版）。
> **维护责任**：新增品牌 DESIGN.md 时同步本索引。

## DESIGN.md 是什么

Google Stitch 提出的概念：一个纯 Markdown 设计系统文档，AI coding agent 读它来生成视觉一致的 UI。不需要 Figma 导出、JSON schema 或特殊工具——放进项目根目录，任何 AI agent 都能理解你的 UI 应该长什么样。

| 文件 | 读者 | 定义 |
|------|-------------|-----------------|
| `AGENTS.md` | 编码 agent | 怎么构建项目 |
| `DESIGN.md` | 设计 agent | 项目应该长什么样 |

## 已收录设计规范

| 品牌 | 文件大小 | 设计特色 |
|---|---|---|
| **Apple** | 37KB | Human Interface Guidelines 精髓：清晰/遵从/深度，SF 字体层级，系统色彩 |
| **Airbnb** | 30KB | Cereal 字体体系，对话式设计语言，地图优先布局 |
| **Vercel** | 41KB | 极简黑白，Geist 字体，暗色优先，原子化间距 |
| **Notion** | 35KB | 块编辑器设计语言，极简边框，灰阶主导 |
| **Stripe** | 24KB | 渐变+代码高亮，文档级排版规范 |
| **Linear** | 24KB | 高密度信息流，键盘优先交互，暗色主题 |
| **Figma** | 32KB | 设计工具设计语言，画布交互模式 |
| **Uber** | 34KB | 地图出行设计语言，品牌色 C display |
| **Spotify** | 13KB | 音频流媒体设计语言，高饱和品牌绿 |

## 外部规范链接

| 规范 | URL | 适用场景 |
|---|---|---|
| **Apple HIG（中文）** | https://developer.apple.com/cn/design/human-interface-guidelines | iOS/macOS 原生 UI |
| **Material Design 3** | https://m3.material.io | Android/Web 通用 |
| **美的 IoT 设计规范** | https://iot.midea.com/docs/development-specification/design-specification.html | IoT/硬件 UI |
| **知乎：UI 设计规范汇总** | https://zhuanlan.zhihu.com/p/669861422 | 中文设计规范综述 |
| **Awesome DESIGN.md** | https://github.com/voltagent/awesome-design-md | 167 个真实网站 DESIGN.md |

## AI 宿主使用方式

将对应品牌的 `DESIGN.md` 放入目标项目根目录，AI coding agent 读它来生成一致 UI：

```
项目根/
├── AGENTS.md        ← 编码规范
├── DESIGN.md        ← 设计规范（从本目录复制）
└── src/...
```

## BugX UI 设计质量审计的对照基准

`bugx probe ui --design <DESIGN.md> --url <URL>` 读取设计正文并生成浏览器收据。
可在目标项目的设计文档中加入 `bugx-ui` 围栏，声明 token、组件和状态规则。使用
`token_source` 可复用项目既有 DTCG 或 Style Dictionary JSON，也可以通过 `--storybook`
审计 Storybook 组件场景。使用
`--vision-model <本地视觉模型>` 时，探针把截图和正文交给本机模型，分别记录层级、对齐、
排版、密度和一致性点评。具体输入与修复流程见
[Web UI 审计卡](../../../core/report_engine/web-conformance.md#项目-ui-规则)。
