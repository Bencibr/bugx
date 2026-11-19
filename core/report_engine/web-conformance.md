# Web 标准合规审计卡

> 运行前置：BugX 核心要求 Python `>=3.11`。本文中的 `python3` 命令只表示已通过版本
> 校验的解释器；系统 Python 3.9 不满足契约，请先按 `core/contracts/AGENT_GUIDE.md` 设置
> `BUGX_PYTHON`，或优先使用 `bugx` CLI。

## 适用性

项目被识别为 Web 后，`web-standards` 是必选审计轨道；非 Web 项目返回
`NOT_APPLICABLE`。这里的“必选”表示必须给出标准审计结论，不表示无条件安装
所有浏览器或第三方工具。

标准 Oracle 固定为：

- WCAG 2.2 AA；
- WAI-ARIA 1.2；
- HTML/WHATWG 语义一致性；
- 项目自己的设计稿、浏览器兼容矩阵和业务契约可以增加约束。

## 执行顺序

1. 先执行内置静态基线：

   ```bash
   python3 "$(bugx engine path --name web_conformance.py)" check --root . --json
   ```

   它检查 `lang`、图片 `alt`、表单控件和交互元素的可访问名称、重复 ID、ARIA ID 引用。
   静态规则失败要形成 Finding；扫描不完整不能报告为通过。

2. 使用 `bugx probe ui --root . --url http://127.0.0.1:8080 --journeys`。探针在浏览器中
   采集截图、DOM/ARIA、缩放、键盘步骤和业务状态，运行随包发布的 axe-core。收据记录
   规则 ID、影响、元素定位和待确认项。axe-core 的 `incomplete` 对应 `UNVERIFIED`。

3. 执行 `web_conformance.py check --root . --dynamic-evidence <file> --strict`，其中
   `<file>` 为第 2 步生成的 `dynamic-evidence.json`。它引用同目录的 `manifest.json`，
   核对项目根、run ID、清单哈希、视口与 PNG 尺寸以及实际截图哈希，并重新计算动态结论。
   旧式手填 `status/evidence` 或移动、替换任一产物会得到 `UNVERIFIED`，需重新生成同轮收据。

## 结果语义

- `PASS`：静态和动态检查均有证据，表示本轮基线通过，不表示取得外部认证。
- `FAIL`：存在可定位的标准违例，进入既有 Finding 确认流程。
- `BLOCKED`：缺少浏览器、页面、权限或动态证据，保留覆盖债务，不得伪造通过。
- `UNVERIFIED`：证据格式、复现或标准对应关系不足，需要补证据。
- `NOT_APPLICABLE`：项目没有 Web 交互面，不产生 Web 覆盖债务。

缺少 Playwright 时不能把静态检查结果升级为完整 Web 合规；只能报告静态结果和动态
`BLOCKED/UNVERIFIED`。主观配色或设计偏好没有项目契约、标准条款和复现证据时，不立案。

## 项目 UI 规则

在目标项目的 `DESIGN.md` 中写设计意图，并用一个 `bugx-ui` JSON 围栏声明可执行部分。
页面选择器使用 CSS。这个例子声明了一个加载场景，`error`、`empty` 和 `disabled` 按同样
方式各自提供场景。禁用场景至少断言一个 `"disabled": true`。

```bugx-ui
{
  "version": 1,
  "tokens": {"--space-control": "16px", "--ink-primary": "#222222", "--surface": null},
  "components": [
    {"id": "button-padding", "selector": ".action", "styles": {"padding-top": "--space-control"}},
    {"id": "card-alignment", "selector": ".cards > article", "align": "top", "tolerance_px": 2,
     "profiles": ["desktop", "tablet"]}
  ],
  "contrast": [
    {"id": "search-focus", "selector": "#search", "property": "outline-color",
     "against": "parent", "focus": true}
  ],
  "reflow": [
    {"id": "results", "selector": "#results"},
    {"id": "search", "selector": "#search", "interactive": true}
  ],
  "journeys": [
    {"id": "results-loading", "state": "loading", "path": "/qa/results?fixture=loading",
     "actions": [{"type": "click", "selector": "#load"}],
     "expect": [{"selector": "#feedback", "text": "Loading",
                 "attributes": {"role": "status", "aria-busy": "true"}}]}
  ],
  "keyboard": [
    {"id": "dialog", "path": "/qa/results",
     "steps": [{"key": "Tab", "focus": "#open"},
               {"key": "Enter", "focus": "#close"},
               {"key": "Tab", "focus": "#confirm"},
               {"key": "Shift+Tab", "focus": "#close"},
               {"key": "Escape", "focus": "#open"}]}
  ]
}
```

- `tokens` 检查根节点 CSS 自定义属性。值为 `null` 时只检查存在性。组件 `styles` 可以引用
  组件作用域中的 token，也可以直接声明预期 CSS 值。`align` 支持 `left`、`top`、`right`、
  `center-x`，以至少两个可见实例计算偏差。
- `token_source` 从项目内 JSON 读取期望值。`format` 为 `dtcg` 或 `style-dictionary`，
  `map` 将根 CSS 变量指向点号分隔的 token 路径。它可与不同名称的内联 `tokens` 共用。
  支持常见 CSS 标量、sRGB 颜色、带单位尺寸、时长、字体族与同文件别名。DTCG
  `$ref` 支持同文件 JSON Pointer；组 `$extends` 和无法表示为单个 CSS 值的复合类型会报告输入错误。
- `contrast` 检查指定控件的 `background-color`、`border-top-color` 或 `outline-color`。
  `against` 为 `parent` 或 `self`，以实际相邻表面为准。探针另外测量视口内的常规 DOM
  文字，合成可确定的透明图层。渐变、图像背景、不可分辨的覆盖层和嵌入内容保留
  `UNVERIFIED`。请用显式状态路径覆盖需要单独检查的页面。
- `reflow` 每个选择器应指向一个在所有审计视口保持可见的元素。`interactive` 同时检查点击
  可操作性，使用 Playwright 的试运行模式。原生缩放以 1280×1024 为基线，在 200% 和
  400% 下验证实际 CSS 视口、页面溢出与关键元素。报告属于本次页面和选择器范围。
- `journeys` 每条从独立上下文开始。`path` 为同源路径，可省略。动作支持 `click`、`fill`、
  `press`、`select_option`、`wait_for`，断言支持 `text`、`attributes`、`visible` 和
  `disabled`。短暂状态应提供可重复的测试数据或 QA 路径。
- 在 Storybook 模式，`journeys` 和 `keyboard` 也可用 `story` 指定已渲染场景的 ID。
  `story` 和 `path` 二选一；状态仍由项目断言判定。
- `keyboard` 在独立浏览器上下文里逐步发出真实按键，记录预期和实际焦点，并检查声明元素的
  outline、阴影、边框或背景样式变化。可用 `indicator` 指定承载焦点样式的 CSS 选择器，默认
  为 `focus` 元素。弹窗内的正反向遍历、退出和恢复由项目步骤声明。没有旅程或缺少可用的
  未聚焦样式样本时，键盘结果为 `UNVERIFIED`。焦点颜色的定量检查由 `contrast` 声明。
- 本机 Ollama 配置有视觉模型时，可用 `--vision-model <本地模型标签>` 生成绑定图片哈希的
  `ADVISORY` 点评。标题层级、对齐、排版、密度和一致性分别评审，机器门禁仍以测量为准。
- `--repair-css tokens.css` 预览已声明值的修复补丁。目标是项目内只有一个 `:root` 块的
  token 样式表。附加 `--apply` 后，回环地址服务须加载这份 CSS。探针保存原文件、复测
  六个默认视口及已启用的状态，回归时恢复原字节。文件在复测期间被另一个进程编辑时，
  使用 `repair.json` 中的备份和补丁核对，状态为 `RECOVERY_REQUIRED`。

## 接入 Storybook 和设计 token

Storybook 以已运行或已构建并提供 HTTP 服务的站点为输入。先列出可渲染条目，再按精确 ID 审计。

```bash
bugx probe ui --storybook http://127.0.0.1:6006 --list-stories
bugx probe ui --root . --storybook http://127.0.0.1:6006 \
  --story button--default --journeys --strict
```

探针读取 `index.json` v4/v5，打开对应的 `iframe.html` 画布并核对所选 ID。axe-core
检查画布范围。收据包含 index 哈希、story ID 和预览 URL。Storybook 模式下，
`--reference-url` 指向参考 Storybook 的基地址，并在两端匹配同一个 ID。

在现有 `bugx-ui` JSON 中加入类似字段。项目按自己的生成规则映射 CSS 变量名称。

```json
{
  "token_source": {
    "file": "design/tokens.json",
    "format": "dtcg",
    "map": {"--space-control": "space.control", "--ink-primary": "color.primary"}
  }
}
```

Style Dictionary 传统 JSON 使用 `"format": "style-dictionary"`。令牌文件的路径、格式、映射
和 SHA-256 写入设计收据。CSS 期望值由输入 JSON 解析，实际值来自浏览器的计算样式。

## 参考页面视觉对照

在同一浏览器版本、视口、缩放和主题下渲染认可的参考页面与当前页面。两个地址需要能执行
同样的已声明场景。可重复指定 `--visual-mask` 屏蔽时间戳等动态内容。

```bash
bugx probe ui --root . --url http://127.0.0.1:8080 \
  --reference-url http://127.0.0.1:8081 --journeys \
  --visual-mask '[data-volatile]' --visual-max-diff 0.001 --strict
```

任一 RGB 通道变化超过 16 的像素计为变化。`--visual-max-diff` 是允许变化的像素比例，
默认为 0.001，即 0.1%。`visual-diff/` 保存差异图、边界和掩码，证据不足标记
`UNVERIFIED`。同一参考收据可用于 `--repair-css ... --apply` 的复测。某个场景新增的差异
超过阈值时回滚，即使总差异数量减少。无法比较全部场景时，修复在写入前阻断。

> 运行前置：BugX 核心要求 Python `>=3.11`。本文中的 `python3` 命令只表示已通过版本
> 校验的解释器；系统 Python 3.9 不满足契约，请先按 `core/contracts/AGENT_GUIDE.md` 设置
> `BUGX_PYTHON`，或优先使用 `bugx` CLI。
