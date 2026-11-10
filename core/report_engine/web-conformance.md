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

2. 再使用 Playwright 或目标项目的浏览器自动化执行动态检查：键盘/焦点、Accessibility Tree、
   最终样式对比度、200%/400% 重排和 loading/error/empty/disabled 状态。截图、DOM/可访问性树、
   网络和控制台记录必须绑定页面、视口、浏览器版本和复现步骤。

3. 如需将外部动态证据纳入机器门禁，使用最小 JSON：

   ```json
   {
     "checks": {
       "keyboard-focus": {"status": "PASS", "evidence": "playwright:..."},
       "aria-tree": {"status": "PASS", "evidence": "playwright:..."},
       "contrast": {"status": "PASS", "evidence": "playwright:..."},
       "reflow-zoom": {"status": "PASS", "evidence": "playwright:..."},
       "state-feedback": {"status": "PASS", "evidence": "playwright:..."}
     }
   }
   ```

   然后执行 `web_conformance.py check --dynamic-evidence <file> --strict`。

## 结果语义

- `PASS`：静态和动态检查均有证据，表示本轮基线通过，不表示取得外部认证。
- `FAIL`：存在可定位的标准违例，进入既有 Finding 确认流程。
- `BLOCKED`：缺少浏览器、页面、权限或动态证据，保留覆盖债务，不得伪造通过。
- `UNVERIFIED`：证据格式、复现或标准对应关系不足，需要补证据。
- `NOT_APPLICABLE`：项目没有 Web 交互面，不产生 Web 覆盖债务。

缺少 Playwright 时不能把静态检查结果升级为完整 Web 合规；只能报告静态结果和动态
`BLOCKED/UNVERIFIED`。主观配色或设计偏好没有项目契约、标准条款和复现证据时，不立案。
> 运行前置：BugX 核心要求 Python `>=3.11`。本文中的 `python3` 命令只表示已通过版本
> 校验的解释器；系统 Python 3.9 不满足契约，请先按 `core/contracts/AGENT_GUIDE.md` 设置
> `BUGX_PYTHON`，或优先使用 `bugx` CLI。
