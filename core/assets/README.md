# BugX Core Assets (品牌视觉与图形资产)

`core/assets/` 存放 BugX 官方品牌、头像图标、宣传横幅以及用于 WebUI、HTML 报告与 Markdown 文档内嵌的视觉图元。

---

## 1. 资产清单 (Assets Inventory)

| 文件名 | 类型 / 分辨率 | 用途与场景 |
| :--- | :--- | :--- |
| `bugx-avatar-1024.png` | 1024x1024 高清 PNG | 官方发布、应用商店图标、高清桌面通知徽章 |
| `bugx-avatar-512.png` | 512x512 PNG | 插件扩展商店（VS Code, JetBrains, Chrome）图标 |
| `bugx-avatar-tight.svg` | 纯矢量 SVG (紧凑边框) | 报告内嵌、终端徽标、轻量级 Web 图标 |
| `bugx-avatar.svg` | 纯矢量 SVG (标准呼吸留白) | 官方网页导航栏、暗色/亮色自适应图元 |
| `bugx-hero-cn.png` | 中文横幅高清 PNG | 中文 README、技术博客、社交媒体分享 Banner |
| `bugx-hero-en.png` | 英文横幅高清 PNG | 英文 README、国际开源社区分享 Banner |
| `project-layout.md` | 项目物理布局模板 | 项目初始化由 `bugx init` 复制至 `.bugx/README.md` 的运行数据说明 |

---

## 2. 规范与引用契约

- **内联嵌入优先**：在生成独立离线 HTML 报告时，优先将 `bugx-avatar-tight.svg` 进行 Base64 编码或原生内嵌，确保无外网时图标正常显示。
- **高对比度自适应**：SVG 采用自适应 CSS 变量或干净的纯色渐变，在暗色（Dark Mode）与亮色（Light Mode）模式下均具备极高的清晰度。
