# Stage 30: Component Exposure（导出组件、深度链接与 IPC）
<!-- L1:START -->
1. 枚举导出面：`exported=true` 的 Activity/Service/Receiver/Provider，及带 intent-filter 的组件。
2. 对每个导出面判定：是否需要权限保护、输入是否校验、是否返回敏感数据。
3. 深度链接：scheme/host 是否可被任意应用唤起、参数是否直接进入特权操作（WebView、文件、登录）。
4. iOS：URL Types、Universal Links、`application:openURL:` 处理逻辑。
5. 产出 `exported-component-findings`、`deeplink-findings`。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- Android 侧重点（对应 MASTG/MASVS-PLATFORM）：`PendingIntent` 可变性、`FileProvider` 路径范围、隐式 Intent 泄漏 extras、动态注册 Receiver。
- 只报告"能被第三方应用触发且无鉴权/无校验"的路径；内部组件不算。
- 深度链接必须给出**完整触发命令**（`adb shell am start -a android.intent.action.VIEW -d "scheme://host/path?..."`），无设备时标注 UNVERIFIED。
<!-- L2:END -->
