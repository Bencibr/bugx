# Module: android（Android 原生）

## 适用
存在 `AndroidManifest.xml` / `build.gradle*` / `*.apk` 时启用。

## 检查面
- **Manifest**：`exported`、`permission`、`intent-filter`、`allowBackup`、`debuggable`、`usesCleartextTraffic`、`networkSecurityConfig`、`requestLegacyExternalStorage`。
- **存储**：`SharedPreferences` 明文、`getExternalFilesDir`、`MODE_WORLD_READABLE`、数据库明文。
- **IPC**：`PendingIntent` 可变性、`FileProvider` 路径、隐式 Intent extras、动态注册 Receiver。
- **WebView**：`setJavaScriptEnabled` + `addJavascriptInterface`、`setAllowFileAccess`、`onReceivedSslError`。
- **密码学**：`ECB`、`DES`、`SecureRandom` 误用、硬编码密钥。

## 工具
- 静态：`core/rules/mastg/`（50 条，MASVS 标签）
- 反编译：`jadx -d out app.apk`（无源码时）
- 设备：`adb shell am start/…`，证据需 `uiautomator dump` 或 logcat

## 输出
`manifest-facts`、`exported-component-findings`，每条附文件:行 或 adb 命令。
