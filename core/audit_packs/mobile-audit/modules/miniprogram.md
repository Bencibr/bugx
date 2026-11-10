# Module: miniprogram（微信小程序）

## 适用
`project.config.json` 存在，或仓库含 `*.wxml` 页面且 `app.json` 定义 `pages` 时启用（框架标记 `miniprogram`）。

## 与原生 App 的差异（不要套用 Android/iOS 检查项）
- 没有 `AndroidManifest.xml`；权限在 `app.json` 的 `permission` / `requiredPrivateInfos`，以及 `project.config.json`。
- 安全边界主要在**云函数 / 服务端接口**与**前端信任假设**，不在系统 IPC。

## 检查面
- **配置**：`app.json` 的 `permission`（`scope.userLocation` 等）、`requiredPrivateInfos`、`networkTimeout`；`project.config.json` 的 `urlCheck`（关闭校验会掩盖未配置的合法域名）。
- **网络**：`wx.request` 是否硬编码 http、是否在前端拼接可被篡改的业务参数（价格/权限/租户）；请求签名是否可重放。
- **存储**：`wx.setStorageSync` 是否存 token/身份证/手机号；`wx.getStorageSync` 读取是否做来源校验。
- **跳转 / 参数**：`wx.navigateTo` 的 `url` 是否拼接用户输入（路径穿越 / 任意页面）；`onLoad(options)` 是否直接信任 `options`。
- **WebView**：`web-view` 组件的 `src` 是否可被参数控制（开放重定向 / 钓鱼）；`bindmessage` 是否校验来源。
- **云函数**：是否只依赖前端传来的 `openid`/`role`（越权）；数据库规则是否为"所有用户可读"。
- **敏感信息**：`AppSecret`、云开发 `envId` + 密钥、地图/支付私钥是否出现在前端包内。

## 工具
- 静态：`core/rules/miniprogram/`（8 条 semgrep 规则：`setStorageSync` 存凭据、`wx.request` 明文、模板字符串拼 URL、`web-view` 动态 src、`urlCheck` 关闭、`eval`/`new Function`、前端 AppSecret）。用 `languages: [generic]` + `paths.include` 覆盖 `.wxml`。
- 动态：需要微信开发者工具自动化（`miniprogram-automator`）。仓库**未装配**该类 MCP
  （`wechat-devtools` 为社区个人项目 trust=C，按注册表规则只能 `planned`），
  因此动态结论必须标 `UNVERIFIED`，不得凭代码猜测"已复现"。

## 输出
`miniprogram-config-facts`、`cloud-function-authz-findings`，每条附文件:行 + 可复现的请求/参数。
