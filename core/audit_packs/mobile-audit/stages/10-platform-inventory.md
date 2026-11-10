# Stage 10: Platform Inventory（清单、权限与平台目标）
<!-- L1:START -->
1. 定位 `AndroidManifest.xml` / `Info.plist` 与构建脚本（`build.gradle*`、`Podfile`、`*.xcodeproj`）。
2. 抽取：`exported` 组件、`android:permission`、intent-filter（scheme/host）、`allowBackup`、`debuggable`、`usesCleartextTraffic`、`networkSecurityConfig`。
3. iOS 抽取：`LSApplicationQueriesSchemes`、`NSAppTransportSecurity`、URL Types、entitlements、`UIFileSharingEnabled`。
4. 产出 `manifest-facts` 与 `platform-targets`（minSdk/targetSdk、iOS deployment target）。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 有源码时直接读 XML/plist；只有 APK 时用 `jadx -d out app.apk` 或 `apktool d app.apk` 反编译后再读。
- **权限要配对使用场景**：危险权限本身不是缺陷，缺失运行时校验/导出组件无保护才是。
- `android:exported` 在 targetSdk ≥ 31 必须显式声明；缺失即记录为待确认项。
- 每个事实必须带文件路径 + 行号，禁止只写结论。
<!-- L2:END -->
