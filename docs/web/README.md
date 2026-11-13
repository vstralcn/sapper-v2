# 原 Agent 的 Web 操作与观察层开发方案

文档版本：v0.9 · 2026-11-02 · 状态：非平台原生功能、工作区／角色、原生角色派发策略，以及模型协议选择、模型发现和连接测试已实现；Linux／HTTPS 环境验收待进行。

## 目标与约束

为 sapper-v2 增加便于使用的网页，让任务启动、运行观察、补充资料、终端操作、日志、模型配置和文件管理尽可能在浏览器完成。保留原 agent 的能力与运行机制。

| 项目 | 已确认要求 |
| --- | --- |
| 能力边界 | 不改变原 agent 的能力；本轮是使用界面和管理入口增强 |
| 任务范围 | CTF、安全分析、代码与研究任务；使用时不强制依赖 TSecBench |
| 原有运行方式 | 保留原 CLI、指挥官／worker、tmux、watchdog、工具环境和会话机制 |
| 原有记忆与工具 | `.sapper/` JSON 事实意图图继续作为权威来源；保留知识库、hooks、现有工具和通信能力 |
| 模型配置 | 网页管理多组 API 地址、密钥和模型，按原引擎支持的方式分配给 agent；OpenAI／Anthropic 兼容接入须实际验证 |
| 使用与部署 | 个人使用，先本机，再单台 Linux 服务器经域名／网关访问 |
| 自主程度 | 保留任务授权范围内的自动执行；沿用已有确认机制，不因网页包装增加逐命令审批 |
| 本次交付 | 实现原 sapper-v2 基础功能的网页控制，保留原 agent 行为 |

网页后台负责把操作映射到现有命令和会话，读取原有状态并记录管理操作。不会用自写模型工具循环替换原 CLI，也不迁移事实图到数据库。适配通用任务时，仅增加平台可选分支、任务资料与配置入口，保留原比赛模式。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [终端、文件与 Agent 状态](./17-main-agent-chat.md) | 工作区默认终端、全部角色状态、原生交互与精确恢复 |
| [Agent MCP、Skills 与会话存储](./16-agent-resources.md) | Web 资源选择、会话隔离、API Key 启动与精确恢复 |
| [角色与协作拓扑](./15-agent-topology.md) | 实时拓扑、状态依据、分组交互、布局稳定性与验证 |
| [产品需求与交互](./01-product-requirements.md) | 网页管理范围、操作语义、保留能力与验收 |
| [技术架构与迁移](./02-architecture.md) | 原功能与网页操作的映射、模型配置、终端、日志及状态所有权 |
| [开发计划与验收](./03-development-plan.md) | 接入现有 agent 的开发顺序、工作包与回归检查 |
| [待补充信息与决策记录](./04-open-questions.md) | 已确定约束、兼容性待验证项与环境信息 |
| [启动与使用](./05-running.md) | 当前版本的真实启动命令、已实现功能、容器接入及限制 |
| [下一阶段：工作区与角色](./06-workspace-and-roles-plan.md) | 绑定题目目录、原 Claude Code 会话、角色还原、真实 hook 缺口与分阶段验收 |
| [全局上下文与总体概览](./08-global-context-and-overview.md) | 全角色共享环境配置、注入方式／版本、首页与最新验收 |
| [工作区实现与验收记录](./07-workspace-implementation.md) | 本次实现、实际验证、原模式兼容边界与剩余环境验收 |
| [非平台原生功能覆盖](./09-native-capability-coverage.md) | 31 个原生命令、配置与模板、通信、原调度／编队、覆盖范围和最新验收 |
| [模型协议与连接测试](./10-model-protocols.md) | Messages／Responses／Chat Completions、获取模型、测试生成、实际工具调用与适配服务 |
| [原生角色提示词对齐与实现](./11-native-role-prompts-plan.md) | 默认角色职责、原生条件派发策略、真实看板、创建预览与验收记录 |
| [恢复后继续执行规划](./12-resume-continuation-plan.md) | 已确认的恢复后等待输入缺陷、后台任务边界、推荐交互与验收条件 |
| [限时会话生命周期修复](./13-print-run-lifecycle.md) | print 流式活动、运行中续时、分阶段停止、超时状态与原 UUID 恢复 |
| [指挥官续轮与恢复确认](./14-commander-liveness.md) | 空闲后唤醒、真实读板回执、暂停／等待用户／完成与有限恢复 |

## 当前已完成与未完成

登录与刷新默认进入总体概览，汇总全部工作区、活动角色和最近运行。侧栏“全局上下文”可配置服务器、本机工具等共享信息，通过启动提示词和会话 hooks 传给各角色，显示启动／hook 版本；详见[新功能说明](./08-global-context-and-overview.md)。

侧栏／工作区“原生功能”现已覆盖原 CLI 的 31 个非平台命令及参数，可预览命令、选择配置并直接执行。包含原 `auto/dispatch/command/sweep/fleet`、授权通信、知识库新增／导入／正文、模板与编队配置编辑，以及图生命周期操作。排除 `arena/bench/flag --submit`；原 CLI 保留。具体能力和原生流程与交互会话的区别见[覆盖说明](./09-native-capability-coverage.md)。

模型配置可选择 Anthropic Messages、OpenAI Responses 或 Chat Completions，获取并筛选模型、手动填写 ID、测试文本生成。新协议通过本机 HTTP 适配连接原 Claude Code，不替换其工具循环。配置页可查看／停止后台适配服务；详见[模型配置说明](./10-model-protocols.md)。

基础版本已实现：登录、任务自动发现／创建、事实／意图／hint／文档、原图、原 CLI 运行、tmux 文字及按键控制、输出记录、附件与文件、多组原引擎模型配置、原 worker 操作入口和知识库。

新增工作区可绑定已有目录、项目级启用 hooks 与说明，在题目根目录打开原 Claude Code。辅助角色各用独立 cwd，共享原图；可选择同目录并行。每个新角色绑定原生 UUID，使用 `--resume UUID` 精确恢复。界面显示指挥官／将军／侦察兵／执行者／验证者、父子派发、模型、任务、目录与图贡献；watchdog 单独显示为脚本服务。

Web 新建角色加载 `sapper-native-roles/4`：Commander 按原 4.6／4.10 条件派发，General 可按原规则派最多两个短时侦察／验证小队，并负责查看和回收；新增阶段成果保存、续时与原会话恢复说明。`board --json` 提供真实图证据、HELP／REPORT、Flag 状态、父子角色和限时运行观测；不会新增强制派兵或平台提交授权。创建窗口可直接核对实际基础提示词和来源。

受管交互式指挥官恢复时会发送继续消息，并可由 watchdog 以真实读板回执确认续轮；详见[指挥官续轮](./14-commander-liveness.md)。其他 TUI 角色的恢复语义仍见[恢复规划](./12-resume-continuation-plan.md)，旧后台 shell 不会随历史恢复。限时 print 会话已补上流式观测、运行中续时、分阶段停止和恢复后的继续消息，详见[生命周期修复](./13-print-run-lifecycle.md)。

原 CLI 保留解题循环与图格式；增加 `web` 入口及显式受管环境下的上下文追加，没有迁移数据或替换工具集合。原 worker／watchdog 默认分支保留，新本地分支仅由 `SAPPER_MODE=workspace` 启用。真实 hooks 已修复并加入子进程回归。

首次运行 `./sapperctl auth-init` 设置管理员账号和密码，再执行 `./sapperctl start`，在 `http://127.0.0.1:8766/` 登录。账号与会话保存在 `.sapper-web/auth.sqlite3`。前台运行及服务管理详见[启动与使用](./05-running.md)。

终端已增加 xterm.js 与原 tmux PTY 流式输出、完整输入、尺寸调整、断线续传；不替换既有外部输出订阅。此前 79 项回归及真实 Claude 会话验证已通过；最新原生功能回归与真实 `auto` 验收见[覆盖说明](./09-native-capability-coverage.md)。Linux／HTTPS、Docker 构建及任意供应商兼容性仍未实测。

已有题目目录直接在网页文件夹选择器中点选并绑定，无需启动时增加允许目录。网页选择“打开题目目录 → 启用 sapper → 打开 Claude Code”，详见[启动与使用](./05-running.md)。

首版的核心验收是：相同任务和配置下，从网页与从原终端启动使用同一 agent、同一命令／工具／记忆与恢复机制；界面能管理和观察，不接管 agent 的解题策略。


工作区指挥官派单时，先在目标工作区用 `sapper graph` 选取已有依据，再运行 `worker.sh spawn <工作区> --from f001,f003 --mission "验证读入长度"`。已有合适的开放任务可用 `--intent i002` 领取。首次侦察先把确实收到的题面或附件记为起点。Web 启动子角色时在“派单依据”输入相同的 ID。工作区、依据和结果均使用原来的共享图。

子角色可从 `$SAPPER_INTENT_ID` 读取派单。中途的独立验证可另建 intent，最终用 `sapper done "$SAPPER_INTENT_ID" --worker "$SAPPER_AGENT_ID" --fact "已验证结果"` 结束本次任务。捕获 flag 本身就是交付时可用 `sapper flag VALUE -c CHALLENGE --from-intent "$SAPPER_INTENT_ID" --creator "$SAPPER_AGENT_ID"`。限时退出或暂停保留未完成任务供原会话恢复；改派时先停止原角色并用 `sapper release i002 --worker <原角色ID>` 释放，再用 `--intent i002` 派给新角色。发送消息、恢复旧会话不会创建新任务。
