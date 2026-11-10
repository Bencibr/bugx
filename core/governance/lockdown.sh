#!/usr/bin/env bash
# lockdown.sh — 加固 bugx 外部防线文件（同权限逃逸的 OS 层缓解）
#
# 背景：bugx 拥有完整 bash + 文件读权限，理论上可复制/篡改校验器
# 脚本（cp verify_life.py /tmp/x.py 修改后运行）或改写快照基线，从而绕过
# 外部防线。opencode 的 permission 挡「诚实越界」，但挡不住「恶意 + 全权限」。
#
# 与 opencode.json 的 deny 面保持镜像（本文件仅记录该面，不做执行）：
#   - read/grep/glob 目录级拒绝 ~/.bugx/**，并显式列出高价值单文件：
#       ~/.bugx/cloud-credentials.json、~/.bugx/identity/installation-secret
#       （32-hex 身份密钥，report_client.py）、~/.bugx/cloud-device-id、
#       ~/.bugx/cloud-config.json（可改写 base_url 指向攻击者服务器）
#   - bash 命令字符串 glob 覆盖上述全部文件名，而非只覆盖 credentials 文件
# 诚实声明：该 deny 面**不约束在 MCP server 进程内部发起的文件读取**
# （bugx_mcp_server.py 的工具直接调用 open()/read_text()，不经过宿主
# permission 层），因此它只是「威慑 + 防误操作」层，**不得被当作安全边界**；
# 真正的读取约束必须落在 Python 侧的统一敏感路径守卫上。
#
# 本脚本把校验器/启动器/覆盖门禁/准备门禁/快照设为「只读」，作为文件系统层防线：
#   - verify_life.py / launch_bugx.py / module_coverage.py / tools_kb.py /
#     prep_validate.py / symbol_index.py / project_graph.py / audit_campaign.py /
#     agent_eval.py / model_runner.py / finding_report.py / finding_lifecycle.py /
#     platform_adapter.py / codebase_memory_probe.py / codebase_memory_bridge.py /
#     efficiency_probe.py /
#     finding_confirmation.py / coverage_intelligence.py / behavior_graph.py /
#     test_strategy.py
#     lesson_ledger.py
#     → chmod 444（只读）
#   - bugx-life.json.snapshot（基线）→ chmod 444（只读，防改写基线）
#   - bugx-life.json（agent 结算）→ 保持可写
#
# 注意：单用户环境（owner=运行者）下 chmod 可被同用户 chmod 回去，本脚本是
# 威慑+防误操作层；真正的硬隔离需要把 agent 跑在不同用户/容器/沙箱里，
# 此时本脚本 + 文件属主不同即构成真实防线。
set -euo pipefail
cd "$(dirname "$0")"

for f in verify_life.py launch_bugx.py module_coverage.py symbol_index.py project_graph.py audit_campaign.py tools_kb.py prep_validate.py agent_eval.py model_runner.py finding_report.py finding_confirmation.py coverage_intelligence.py behavior_graph.py test_strategy.py finding_lifecycle.py platform_adapter.py codebase_memory_probe.py codebase_memory_bridge.py efficiency_probe.py lesson_ledger.py; do
    if [[ -f "$f" ]]; then
        chmod a-w "$f"
        echo "  ✓ $f → 只读"
    fi
done
if [[ -f bugx-life.json.snapshot ]]; then
    chmod a-w bugx-life.json.snapshot
    echo "  ✓ bugx-life.json.snapshot → 只读（基线锁定）"
fi
echo
echo "外部防线文件已锁定为只读；bugx-life.json 保持可写（agent 结算用）。"
echo "解锁（后续维护）：chmod u+w verify_life.py launch_bugx.py module_coverage.py symbol_index.py project_graph.py audit_campaign.py tools_kb.py prep_validate.py agent_eval.py model_runner.py finding_report.py finding_confirmation.py coverage_intelligence.py behavior_graph.py test_strategy.py finding_lifecycle.py platform_adapter.py codebase_memory_probe.py codebase_memory_bridge.py efficiency_probe.py lesson_ledger.py"
