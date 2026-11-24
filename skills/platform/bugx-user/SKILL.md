---
name: bugx-user
description: 引导 BugX 用户进行云端连接、项目初始化、防御性代码审查与修复、错题与经验同步及按症状排障。适用于首次使用、云端登录或绑定、非预期 Local 模式排查、知识同步缺失或失败、以及解读 BugX 运行状态与审计结果。
---

# BugX 用户上手与排障

先弄清用户当前卡在哪一步。首次接入走完整主线，已有项目从当前状态继续。面向用户优先引导云端登录、项目绑定、推拉错题和经验，再用这些知识帮助定位、修复和复验项目代码的问题。只有用户明确要求离线时才引导 Local。云端失败时处理失败原因，保留现有状态、账本和队列。

使用同一个明确的项目根，命令中的 `.` 指已确认的仓库根。只在用户授权的仓库、账号和数据范围内工作。先展示全量同步的数据范围与敏感策略，获得授权后再执行云端写入。对宿主交互请求使用该请求的真实选项和用户选择。

## 先走与症状对应的步骤

用户明确要求只同步一部分时，在同步流程的联网或修改授权之前直接答复：“BugX 项目云同步按全量范围执行。当前仅授权部分数据，本次不执行同步。”

已有错误输出时用它判断下一步。有云同步或记录缺失时进入「云端同步和经验找不到」。卡在宿主、Provider 或运行态时进入「宿主、交互和运行态恢复」。其余常见问题查症状表。只有首次接入才从安装清单开始。

## 从安装到第一次同步

1. 用 `bugx --version` 确认 CLI。缺少 CLI 时从选定包源安装 `npm install -g bugx-cli`（Node.js 18+ 和 Python 3.11+）或 `python -m pip install bugx-cli`（Python 3.11+）。用 `bugx hosts --root . --json` 确认宿主标识。已有项目先看 `bugx engine --root . status --json` 和 `bugx cloud status --root . --scope project --json`。保留运行中的项目和待恢复状态。
2. 确认账号、端点和数据范围。 `bugx cloud config --root . --json` 显示 Base URL 及其来源。已有登录先核验账号。首次或认证失效时用 `bugx auth login --root .`，用户在验证页完成设备授权。无法自动打开时按终端给出的地址和用户码操作。用 `bugx auth status --root . --verify --json` 查正确账号和 `auth_verified=true`，因为该查询可能在验证失败时也退出 0。BugX 云账号与宿主模型凭据分别检查。
3. 标准云同步按全量范围执行，包含项目登记与画像、项目知识和账号全局知识。用户只授权其中一部分时，直接拒绝该同步请求。包括仅同步当前项目、仅同步知识或排除项目画像的请求。保留现有项目、账本和队列。详情粒度和敏感策略按当前配置执行。新项目使用 `init --cloud-sync sync` 前先核对用户级 `cloud-config.json` 中的 `sync_detail_mode` 和 `sensitive_policy`。若设置了 `BUGX_CLOUD_CONFIG`，以该路径为准。`cloud config --json` 用于核对端点和来源，详情及敏感策略要读有效配置中的对应字段。若用户授权更新默认值，用 `bugx config set cloud.sync_detail_mode <MODE>` 和 `bugx config set cloud.sensitive_policy <POLICY>`。初始化可能立即同步，并会记录用户级数据分享同意。
   - 全量范围已授权且是全新项目时，运行 `bugx init --root . --cloud-sync sync`。核对初始化是否已输出本轮两层的推拉和 ACK 成功证据。尚待首轮同步时，运行 `bugx cloud sync --root . --scope all`。
   - 已有项目，包括 `onboarding=local` 或首次同步失败，保留账本。全量范围已授权后执行 `bugx cloud sync --root . --scope all`。包含 project 的同步成功会置 `onboarding=done`。设备间用同一账号和仓库已提交的 `.bugx-project.json`，分别登录并核对云项目绑定。
4. 检查本次同步各层 baseline pull、push、post-push pull 的返回，再用 `bugx cloud status --root . --scope all --json` 对照项目 ID、游标、最近错误和 outbox、死信、冲突。 `cloud status` 读本地同步状态。 `done` 表示已启用同步或进入恢复轨道，实际推拉结果以本次网络输出为准。

## 完成一轮并在下轮复用经验

1. 宿主入口还未装配时用 `bugx install --root . --agent <HOST>`，按提示重启宿主。用 `bugx doctor --root . --strict --json` 核对引擎、宿主入口和 MCP，再在宿主中提交项目、模块或变更范围。遇到已有会话，从状态的下一步续跑。需要解释 CLI 的阶段时按用户指南说明，先核对目标宿主的能力。
2. 跟踪 `bugx engine --root . status --json`。启动通过后还要看到实际处理进度、覆盖清单和验证证据。逐项看发现位置、影响和复验结论；用户授权修复时按顺序完成变更检查和定向回归。查看收尾状态及 `bugx report --root . --source project --format both`；如果有未决 Finding、未验证范围或缺失的收尾证据，明确对应项和恢复动作。
3. 在账本中确认已有可共享的错题、事件或工具经验。保留问题症状、已验证原因、修复和复验结果，引导宿主使用当前流程的账本入口。用户明确要求录入时，从 `bugx help lesson` 核对当前版本的 `record`、`incident-add` 或 `tool-feedback` 输入约束，先审查记录内容。用 `bugx knowledge status --root .` 查看账本，再用 `bugx lesson retrieve --root . --query "<症状或模块名>" --json` 查看错题，按同样的 `--root`、`--query` 和 `--json` 参数使用 `lesson incident-lookup` 查故障案例，使用 `lesson tool-lookup` 查工具经验。条件满足时账本提交可后台调度同步。按已确认的全量范围同步并检查实际响应，以确认本轮结果。
4. 下一轮按项目、模块或错误签名召回经验，优先检查相关代码和已知误判。云端经验是定位线索，确认和修复依赖当轮代码与证据。跨设备验收按下文核对两端记录与同步结果。

## 按症状排障

| 现象 | 核对与恢复 |
|---|---|
| 命令找不到、版本和预期不符 | Windows PowerShell 用 `Get-Command bugx -All`，POSIX shell 用 `command -v bugx` 确认命令来源，再用 `bugx --version` 核对 Build。只有问题指向引擎或安装资产时，再用 `bugx doctor --root . --installation-only --json`。 |
| `init` 完成后找不到 `$bugx-user` | 项目 Skill 自动安装自 `0.1.2+build.17` 引入。先看实际运行的 Build 和项目根，再查 `.agents/skills/bugx-user/SKILL.md`。旧 Build 先找与安装来源匹配且已确认包含模板的制品。有支持版本但文件缺失时，按原授权云策略重跑一次普通 `init` 并核对打印路径。文件已存在时，检查宿主是否发现该目录并重新加载会话。 |
| Windows 更新报 `WinError 32`，或失败后 CLI 报模块缺失 | 先用 `Get-Command bugx -All` 和 `python -m pip show bugx-cli` 确认启动器和包的状态，核查占用进程。现有 Codex 会话继续使用原运行进程，文件锁释放前暂停对同一解释器反复重装。急用时可将已核验的 wheel 安装到独立虚拟环境，使用其中的 BugX 启动器。锁释放后用原解释器重装已验证版本，再核对 `bugx --version` 与 `bugx help`。 |
| 自动进入 Local 或 `sync_pending` | 检查云状态、最近 init/pre 参数和 profile/preset。新项目的 TTY init 省略选项、无策略的 Dev 或未决自治启动、未登录的无交互未决启动都能进入 Local。已启用项目遇到部分网络故障也能继续本地执行并保留队列，此时云端仍待同步。若看到的是 `report_policy=local`，那是报告分享策略，项目知识同步仍看 onboarding 和推拉结果。登录正确账号后，按上面的项目身份与数据范围要求恢复同步，保留账本和队列。 |
| 已登录但同步失败 | 手头没有远端核验结果时，运行 `bugx auth status --root . --verify --json`。成功以 `auth_verified=true` 和正确账号为准。已经收到 401 时，直接用 `bugx auth login --root .` 重新授权再核验。TLS 或网络不可达时优先修端点与连通性。403 要核对权限和错误详情。 |
| 云端配置值非法，或 JSON 无法读取 | 能解析 JSON 且错误只点名某个值时，只改已确认的字段。例如用户允许 `summary` 后运行 `bugx config set cloud.sync_detail_mode summary`，再用 `bugx cloud config --root . --json` 验证配置可载入。若整个 JSON 损坏，先备份原配置，再恢复可解析的版本并核对现有端点与策略。直接 `config set` 可能用新配置覆盖坏文件里的其他字段。 |
| `data_sharing_revoked` | 保留队列并暂停推拉。得到用户明确重新授权后，可用 `bugx config set telemetry.data_sharing true` 恢复用户级许可，再按已授权的全量范围同步。 |
| 项目未绑定、绑定到另一项目或读不到另一设备的记录 | 对照账号、project_id、仓库根和 `.bugx-project.json`。先用 `cloud status` 和失败摘要确认，再按身份提示绑定。检查记录所属层、同步错误和检索关键词；避免删除状态、手改绑定或创建重复身份。 |

## 云端同步和经验找不到

先读本轮最终结果，再定位第一个仍未通过的阶段。若 CLI 已自动恢复，最终推拉和 ACK 已成功且状态无对应错误，直接核对目标记录。全量范围还未授权时，按开头直接拒绝。已有全量授权时的标准重试是 `bugx cloud sync --root . --scope all`。状态只读查询可用 `bugx cloud status --root . --scope all --json`，其中 `scope_state`、outbox、inbox 和 conflicts 用于定位当前层。

| 阶段 | 先看什么 | 下一步与成功证据 |
|---|---|---|
| 入账 | 手头只有报告或 Finding，还是已有账本写入结果和记录 ID | 先在源设备用对应的 `lesson` 查询验证记录。若还没入账，按实际记录类型和现有治理流程处理。源设备能返回目标 ID 后再查同步。 |
| 过滤或拒绝 | 项目种子、记录状态、详情策略、敏感策略，以及 push 的跳过、拒绝或冲突输出 | 用目标 ID 核对记录类别和策略审计。修正实际不合规的记录或配置，再按授权全量同步。 |
| 上传 | 本轮出错阶段和 outbox 的 pending / inflight / dead-letter | 先处理本轮错误，保留 operation ID、错误码和队列。恢复后核对对应层的 push 结果。队列为零时仍要核对本轮回执。 |
| 远端确认 | push 有无失败、冲突或远端版本 | 对照本轮 push 结果和状态。批次成功只说明该批处理结果，要证明一条记录应在接收端核对同一 ID。 |
| 下载及 ACK | 接收端实际 project_id、pull、inbox、游标和 ACK | 正常时核对导入和游标。最终结果仍显示已提交但 ACK 失败时，先用目标 ID 查本地导入，进入下表的 ACK 分支。 |
| 检索 | 已导入，当前查询为空、只命中相似标题，或使用了不适用的检索入口 | 用原主题、故障签名或工具分类查询对应类型，比较结果内 ID。同名卡片不能证明目标记录已到账。涉及下一轮 Agent 使用时，还需实际召回的独立证据。 |

| 已定位的故障 | 恢复动作和终点 |
|---|---|
| ACK 失败，pull 可能已提交 | 保留记录和游标，先处理错误指向的认证、网络或服务端拒绝。全量授权下重试一次标准同步。复核目标 ID 仍在、对应 ACK 正常且错误清除。仍有 ACK 失败时给出 `RECOVERY_REQUIRED`。 |
| 有 open conflict | 按实际层运行 `bugx cloud conflicts list --root . --scope <SCOPE> --json`，定位具体冲突 ID 和双方内容。明确得到选边决定后才按 CLI 契约处理，远端选边可能联网。检查目标冲突已关闭并核对最终记录。尚无选边决定时保留冲突。 |
| 有 dead-letter | 按实际层运行 `bugx cloud dead-letter list --root . --scope <SCOPE> --json`，查该条的错误、身份和重试原因。先处理根因。若修复后依然不能重放，保存错误和队列并升级处理。清理是单独授权的管理动作。 |
| 明确 cursor / baseline 失配 | 先核对目标账号、绑定和错误码，查当前 CLI 的 `--calibrate` 影响范围。仅在已确认是可恢复基线失配且有对应授权时考虑校准。 |

### 验收经验跨设备复用

选一条已确认可共享、目标设备尚未有的非种子记录。A 与 B 分别初始化自己的项目运行态，通过受控仓库获得同一 `.bugx-project.json`。两个设备分别登录同一云账号，并核对同一云项目 ID。

1. A 入账后用实际类型查询，保存记录 ID（如返回的 `pattern_id` 或 `incident_id`）和可安全共享的查询词。B 同样查询，留下一次目标 ID 尚未出现的结果。
2. 全量授权下在 A 执行标准同步。确认本轮项目绑定、两层推拉与错误状态。再在 B 同步并确认项目层拉取、导入及 ACK 结果。
3. 在 B 用相同类型的 `lesson` 命令检索，比较返回的记录 ID 和预期内容。工具经验按实际条目的工具名和分类核对。同一标题、旧游标或上一次的 ACK 只可作为线索。
4. 分别报告入账、同步、另一端检索以及下一轮实际消费的结果。可复用的通过证据是 B 同步前无此记录，同步后返回 A 的同一 ID。若还需证明 Agent 真正在下一轮使用它，进一步查看实际召回或消费记录。

## 宿主、交互和运行态恢复

从已有的结构化状态或日志选一行。仅缺证据时查该行的首个命令，复核同一项目和运行身份。

| 现象 | 最小核对 | 恢复与复核 |
|---|---|---|
| `pre` 等待用户选择 | `bugx interaction list --root . --json`。有问题 ID 时用 `bugx interaction show <ID> --root . --json` | 由用户选实际选项，按宿主给出的动作或通过 `bugx interaction answer <ID> --value <VALUE> --root . --json` 提交真实选择。复核请求已消费且门禁推进。 |
| MCP 工具缺失，或只有独立 stdio 的 tools/list | 用 `bugx doctor --root . --agent <HOST> --strict --json` 核对目标宿主和项目入口 | 刚改配置时先重载目标宿主。确认入口缺失才用 `bugx install --root . --agent <HOST>`。验收以真实宿主会话中正确的项目、角色、session proof 和结构化调用为准。接管和清理只用于已核实的过期实例。 |
| 宿主 Provider 429 | 已有返回时使用给出的等待窗口。缺证据时查 `bugx log --root . --errors --timing --json` | 等待限流窗口结束后查 `bugx engine --root . status --json` 并按 next_action 续接。复核旧限流已解除、同一运行链路有新回执。BugX 云账号有独立认证检查。 |
| 有 claim 但没有 worker-start | `bugx supervisor --root . status --json`，对照 Host 的实际 Task 分派和 Worker 身份 | 原宿主按真实任务状态续接。出现匹配的 worker-start 和心跳后再认定它在运行。 |
| heartbeat 丢失、lease 过期 | `bugx supervisor --root . status --json`，核对租约和最后接受的 checkpoint | 确认无活跃执行且状态要求恢复时，由 Host 用 `bugx supervisor --root . recover`。核对旧 lease 回收、已接受的 checkpoint 一致，随后按新状态动作继续。 |
| checkpoint 超时或宿主输出截断 | 先查原 checkpoint_id 的持久化回执和当前租约。没有可信回执时核对完整原请求 | 同一租约仍有效且未确认提交时，由原角色用同一 checkpoint_id 和原负载重试。租约已失效交 Host 恢复。复核同一操作 ID 和原始结构化回执，输出仍不全时标 `UNVERIFIED`。 |
| post 被拦或报告未认证 | `bugx engine --root . status --json`，再按实际缺口查 `bugx supervisor --root . gate --json` | 先处理未结 Finding、coverage 或证据缺口。以正式收尾状态和结构化产物复核。 |

交付时给出用户当前阶段、证据、故障原因或未定位项、已采取的动作及复核结果。分享诊断时只提供脱敏的状态、错误码和必要日志片段。保留 `BLOCKED`、`RECOVERY_REQUIRED` 或 `UNVERIFIED` 等真实状态。

需要核对当前版本参数时，使用 `bugx help <命令>`，例如 `bugx help cloud`、`bugx help lesson` 和 `bugx help report`。
