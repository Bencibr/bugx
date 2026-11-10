# Stage 00: Scope（移动端审计范围与形态确认）
<!-- L1:START -->
1. 用 `mobile` 探针确认审计能力：读取 `status` 与每个通道的 `devices`，**两者都要看**。
2. 记录形态事实：`forms=mobile`、构建目标（android/ios）、包名/Bundle ID、是否含原生代码。
3. 明确审计边界：源码仓、已构建产物（APK/IPA）、或两者兼有。
4. 产出 `scope-manifest.json`（含能力状态与缺口）。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 先跑 `python3 core/probes/mobile_probe.py --root <项目>`；`status=DEGRADED` 时把缺失通道写入 scope，而不是跳过阶段。
- **`status=READY` 不等于有设备**：探针的 READY 只表示"存在某个可执行通道"（例如仅装了 Maestro/Appium CLI）。动态取证必须要求 `channels[].devices` 非空，否则 40 阶段仍标 `UNVERIFIED`。
- 通道优先级：ADB/xcrun（Tier 1，零依赖）→ maestro（Tier 2）→ appium（Tier 3）。任一通道有设备即可进入 40 阶段。
- 只有 APK/IPA 时，10 阶段改用反编译路径（jadx/apktool/ipsw），并标注"无源码"。
<!-- L2:END -->
