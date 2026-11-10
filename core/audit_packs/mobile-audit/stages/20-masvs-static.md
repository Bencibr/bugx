# Stage 20: MASVS Static（静态规则：MASVS 映射）
<!-- L1:START -->
1. 复用已锁定的 semgrep 引擎执行仓库内置规则，**不要**自建扫描器：
   - Android：`core/rules/mastg/`（OWASP MASTG，50 条，消息自带 `[MASVS-*]` 标签）
   - iOS（Swift）：`core/rules/mobsfscan-swift/`（MobSF 规则子集，22 条）+ `core/rules/ios/`（13 条：ATS/Keychain/弱算法/日志泄露）
   - 微信小程序：`core/rules/miniprogram/`（8 条：存储凭据、明文请求、动态跳转、web-view 动态 src、urlCheck 关闭、eval）
2. 命令：`<已校验的 pysemgrep 绝对路径> --config <规则目录> --metrics=off --json --no-git-ignore <目标>`
   - `.plist` / `.wxml` 不是 semgrep 识别的语言扩展名，对应规则用 `languages: [generic]` + `paths.include` 才能在原文件上命中（已验证）。
3. 每条命中必须补"可达性"：该代码路径是否真的会执行、输入是否可控。
4. 产出 `masvs-findings`，按 MASVS 控制项归并。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 必须使用 BugX 锁定的 `pysemgrep 1.182.0`（权威值：随 CLI 发布的 `integrations/opencode/mcp-lock.json` 中 `pypi.environments.semgrep.packages.semgrep.version`）；`semgrep` shim 和普通 PATH 上的 `pysemgrep` 都可能指向宿主版本。
- 每次扫描前运行 `bugx mcp-runtime start --root <项目根目录> --server semgrep --print-command`，只读取成功返回 JSON 的 `runtime_dir` 与 `lock_hash`。在该目录读取 `runtime-manifest.json`，要求其 `lock_hash` 与命令输出相同，且 `packages["pypi:semgrep:semgrep"].version` 精确等于 `1.182.0`。由 `runtime_dir` 推导绝对可执行文件：macOS/Linux 为 `venv/semgrep/bin/pysemgrep`，Windows 为 `venv/semgrep/Scripts/pysemgrep.exe`；对这个绝对路径执行 `--version`，也必须精确等于 `1.182.0`。扫描只调用该绝对路径。
- 若 runtime 缺失、完整性检查失败、manifest lock hash 不一致、包版本不符、可执行文件缺失或实际版本不符，只允许尝试一次 `bugx mcp-runtime install --root <项目根目录> --json`，再重新运行 `start --print-command` 并完整复核一次。此安装命令按当前锁文件重建并切换**整套共享 MCP runtime**，不是仅安装 Semgrep；它会影响后续 MCP server 启动。安装或复核仍失败时将本阶段标记为 `UNVERIFIED` 并停止，不得使用 PATH 中的其他 `pysemgrep`/`semgrep` 降级，也不得报告扫描无发现。
- 规则文件**只读**：发现规则缺口时记录到审计文档，不就地修改（避免产生衍生作品）。
- 静态命中 ≠ 缺陷：`allowBackup=true` 只有在应用确实存敏感数据且无服务端兜底时才升级为 Finding。
- 缺失的规则覆盖（如 iOS 覆盖较薄）必须显式写进 `masvs-findings` 的 coverage 字段，不得默认"无问题"。
<!-- L2:END -->
