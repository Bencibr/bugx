# Module: webview（混合应用内嵌 WebView）

## 适用
原生壳 + WebView 的混合应用（Cordova/Capacitor/Flutter WebView/自研 JSBridge）。

## 检查面
- **JSBridge**：暴露给页面的方法是否按来源白名单校验；是否把任意 URL 交给 WebView 加载。
- **文件访问**：`allowFileAccess`/`allowUniversalAccessFromFileURLs` 开启即高危；`file://` 到 `http(s)://` 的桥接。
- **证书**：`onReceivedSslError` 是否无条件 `proceed()`；WebView 是否复用 App 的 pinning。
- **注入**：`evaluateJavascript`/`loadUrl("javascript:…")` 拼接外部输入。
- **Cookie**：`CookieManager` 与原生会话是否隔离；`setAcceptThirdPartyCookies`。

## 工具
- 静态读壳工程（Android/iOS 模块）+ 前端 H5 代码（h5 模块）。
- 动态：`mobile` MCP（若已装配）+ `playwright`（H5 侧）。

## 输出
`webview-bridge-findings`，每条附暴露方法名、来源校验位置与可复现的 `javascript:` 载荷。
