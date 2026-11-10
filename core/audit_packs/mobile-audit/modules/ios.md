# Module: ios（iOS 原生）

## 适用
存在 `*.xcodeproj` / `Podfile` / `Package.swift` / `*.ipa` 时启用。

## 检查面
- **Info.plist**：`NSAppTransportSecurity`（`NSAllowsArbitraryLoads`）、`LSApplicationQueriesSchemes`、`UIFileSharingEnabled`、URL Types、Universal Links entitlements。
- **存储**：Keychain 可访问级别（`kSecAttrAccessibleAlways`）、`UserDefaults` 存凭据、CoreData 明文、备份到 iCloud。
- **网络**：`URLSession` 证书校验回调、pinning 实现、`allowsInvalidSSLCertificate`。
- **WebView**：`WKWebView` 的 `javaScriptEnabled`、`allowFileAccessFromFileURLs`、`evaluateJavaScript` 注入。
- **加固**：越狱检测、反调试、`get-task-allow`（发布包必须为 false）。

## 工具
- 静态：`core/rules/mobsfscan-swift/`（22 条 Swift 规则）+ `core/rules/ios/`（13 条：ATS/`UIFileSharingEnabled`/`get-task-allow`/Keychain 可访问级别/UserDefaults 存凭据/弱算法/ECB/日志泄露/UIWebView）
- **二进制层（已构建产物）**：`python3 core/probes/ios_binary_probe.py --root <x.ipa|App.app> --json`
  - `Info.plist`（纯 Python 解析）：ATS、`UIFileSharingEnabled`、Bundle ID、MinimumOSVersion
  - `codesign`：签名身份、`get-task-allow`、app-sandbox
  - `otool`：PIE/ASLR、`cryptid`（是否加密需脱壳）、相对路径 dylib 依赖（劫持面）
  - `nm`：`__stack_chk_fail`（栈保护）
  - 工具缺失或主二进制非 Mach-O ⇒ 明确 `DEGRADED`/note，**不伪造**"未发现问题"
- 设备：`xcrun simctl`；真机/模拟器 UI 驾驶用 `mobile` MCP

## 覆盖缺口（必须写进报告）
- MASTG `rules/` 下 **0 条 iOS 规则**；现有 iOS 静态覆盖 = mobsfscan 22 条 + 自建 13 条，仍显著弱于 Android（50 条 + MASTG 全量），结论必须标注"规则覆盖有限"。
- 无 Xcode/证书时无法构建与运行，动态阶段标 `UNVERIFIED`。
