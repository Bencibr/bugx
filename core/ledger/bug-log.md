# Bug 记录清单（bug-log）

> 机制：**只记录模式**（用户选择「不自动修复」）下，发现的 bug 在此追加记录，
> 不修改代码。此清单同时是**全模式的去重依据**——勘察时读清单建「跳过集」，
> 结算时清单内重复发现不计命。
>
> 格式：
> ```
> - [严重度] <一句话标题>
>   - 证据：<文件:行号 / 复现 / 观察>
>   - 根因：<推断>
>   - 状态：未修复-待后续
> ```

---

<!-- 新条目追加到此注释之后 -->

- [HIGH] 云同步 reset 后陷入 stale_version 冲突死循环
  - 证据：`core/cloud_sync/cloud_onboard.py:784` conflict 处理逻辑；`bugx reset` 后执行 `bugx cloud sync`
  - 根因：服务端返回冲突 `record_type="pattern"`（单数），但本地查询 `sync_states` 使用 `("patterns", id)`（复数），导致远端版本号无法载入，客户端持续用 v1 推送引发冲突死循环
  - 状态：已修复（`cloud_onboard.py` 归一化为 wire 格式）
