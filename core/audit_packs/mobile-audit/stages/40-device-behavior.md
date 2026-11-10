# Stage 40: Device Behavior（真机/模拟器行为取证，可选）
<!-- L1:START -->
1. 仅在存在**非空 `channels[].devices`** 时执行；只有 CLI 通道（无设备）或 DEGRADED 时，本阶段整体标 `UNVERIFIED`，不得降级为"通过"。
2. 用 Tier 1 通道取证：`adb shell uiautomator dump` / `xcrun simctl` 截图与界面树。
3. 验证 30 阶段提出的导出面假设：实际唤起、实际返回内容。
4. 产出 `device-evidence`（每条带命令、设备标识、时间戳）。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 动作必须最小化：只做复现所需的最少交互，不安装未知应用、不改系统设置。
- 每次交互后重新 dump 界面树确认状态变化，禁止盲点。
- 崩溃取证：`adb logcat -d` 或 `xcrun simctl spawn booted log show --last 2m`。
- 无设备时把 `mobile_probe` 的 `status` 与 `actions` 原样写入证据，保持可复核。
<!-- L2:END -->
