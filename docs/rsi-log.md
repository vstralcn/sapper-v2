# Sapper-v2 RSI 迭代记录

每次项目变更记一条，说明改了什么、为什么改、如何验证。涉及递归自我改进（Recursive Self-Improvement，RSI）时，再记录这次的结果如何影响下一轮对 Agent 记忆、上下文或工作流的调整。

## 记录方式

- 按时间追加，普通变更用几行记下实际改动、证据和状态。有参考价值的失败尝试和回退也保留，方便后续查到原始结果。
- 改动关联提交、PR 或文件路径。验证关联测试命令、运行记录或产物，并标明验证到了哪一层，例如测试通过、运行环境已采用、实际行为已观察。缺少数据时写「待验证」。
- 新一轮如果沿用或推翻了旧结论，写明引用的记录编号和依据。长日志留在原处，条目保留摘要和证据入口。[女娲测试与 Agent 架构设计参考](memory/nvwa-test-agent-architecture.md)

## RSI 起步方式

先让 Agent 根据以往记录提出一项可检验的改动，由人确认目标和采纳结果。每轮优先改一个变量，用同一组任务、模型版本、预算和验收条件比较改动前后，记录成功数和耗时或成本。比如要验证一份项目文档索引能否让 Agent 更容易找到正确入口，可以用固定的文档查找任务，检查最终引用是否正确，再比较两版的耗时。

用固定的测试或独立验收结果判断收益。涉及记忆变更时，评测期间冻结各版本的记忆，各自按同一口径运行。把通过复核的经验连同适用条件交给下一轮使用，失败或证据不足的结论保留在记录里。参考 AIWiki《递归自我改进》《Harness》和 [RSIAgent 论文](https://arxiv.org/abs/2609.15364)。

## 条目模板

```markdown
### YYYY-MM-DD · 002 变更标题

- 问题与预期 — 观察到了什么，预期改善什么。适用时引用上一轮编号。
- 实际改动 — 文件、提交或 PR，以及具体变化。
- 验证与结果 — 命令或证据位置、前后结果、验证范围。未验证的部分标为待验证。
- 决策与下一轮 — 保留、回退或待验证。写明下一轮可复用的经验或要检验的问题。
```

## 迭代记录

### 2026-11-22 · 001 建立迭代记录

- **问题与预期**　后续项目变更需要一个可持续追加的入口，方便下次从改动追溯到证据和决策。
- **实际改动**　新增 `docs/rsi-log.md`，记录起步版本、追加规则和条目模板。
- **验证与结果**　起步提交为 `e7a5daa`，工作区当时已有未提交改动。本文作为记录起点，首轮能力对照待验证。
- **决策与下一轮**　从下一次实际变更开始追加。首个 RSI 实验先确定一组能重复运行的任务和验收标准。

### 2026-11-23 · 002 标出离线报告的步骤覆盖边界

- **问题与改动**　Geek2026 的部分会话在最后可见步骤后仍有请求和工具交互引用。`scripts/analyze_agent_run.py` 在会话摘要中列出最后关联请求、尾段快照数，以及末份上下文相对该请求新增的、仅见于请求 blob 的工具调用和结果引用。
- **验证与结果**　回放脱敏归档中的 21 个会话，按请求增量重建的 9,404 个上下文顺序均与导出一致。s007 的尾段为 67 份快照，新增调用／结果引用 0／1；s008 为 375、363／363；s017 为 622、613／613。可见步骤合计 8,294，已知输入合计 1,744,312,718 token。原生格式解析用 `tests/test_agent_run_analysis.py` 核对。
- **决策**　采纳覆盖指标用于解释运行记录。归档只能检验计数，跨归档表现和对任务完成率的影响待验证。

### 2026-11-24 · 003 修正看板回执触发的提前唤醒

- **问题与预期**　NSSCTF 本轮保存了两份原生会话，逐条工具回执确认了 10 个不同的通过判定。指挥官 41 次唤醒中，39 次匹配到同 ID 的读板回执。27 轮在上轮结束后不足 60 秒触发，且唯一的工具调用是读板。这 27 轮共 53 步，已知输入量为 3,244,421 token。各步可从快照重建的共享图计数均相同。该用量是观察值，实际节省量有待配对运行测量。
- **实际改动**　`Workspaces.strategy_board()` 在精简子角色状态时保留 `run_id`。读板回执和 watchdog 的定时检查据此用同一组 `id`、`run_id`、`status` 计算状态签名。旧版把缺失的 `run_id` 当作 `None`，对同一运行产生不同签名，绕过了 60 秒空闲兜底。新增一项通过真实看板构建路径复现的回归测试。
- **验证与结果**　旧投影取自 `b3bc022`，配对中仅替换 `strategy_board()`。每组使用相同初始状态、事件时间、3 秒检查间隔、确定性的本地校验文件响应和 SHA-256 验收。候选版和基线均按时交付 3/3，实际模型调用为 0。

| 配对用例 | 唤醒数，旧版 → 候选版 | 正确交付时刻，旧版 → 候选版 |
| --- | --- | --- |
| 原问题：子角色稳定，等待兜底 | 20 → 1 | 60 秒 → 60 秒 |
| 回归：新增待审事实 | 4 → 1 | 12 秒 → 12 秒 |
| 独立构造：子角色状态相同，运行实例更换 | 4 → 1 | 12 秒 → 12 秒 |

- **证据**　[本轮复盘与证据索引](../.sapper-web/analysis/workspace-7a19edd0-rsi-20261124/review.md)、[逐项配对结果](../.sapper-web/analysis/workspace-7a19edd0-rsi-20261124/board-wake-comparison.json)、[回归测试](../tests/test_sapper_commander.py)与[跨任务知识库条目](../kb/entries/kb057.json)。原始会话、图、交付物和逐文件校验和在本地私有归档中。
- **决策与下一轮**　保留为通过离线验收的候选。真实模型成本、任务完成率和独立真实留出任务的配对结果待验证。部署版本及发布决定待定。归档另有 6 条 HTTP 400，原始记录未提供可区分原因的错误码，本轮保留为待查线索。

### 2026-11-24 · 004 修复 TsecBench 三次指挥官停滞的策略根因

- **问题与预期**　TsecBench 运行（本地归档 `runs/TsecBench`、`.sapper-web/runs/0c269bd76b818dfa/output.log`、`actions.jsonl`）中指挥官进程全程未崩溃，但约 105 分钟停滞。① 04:07 指挥官自设 `waiting_user` 后用户在原生 TUI 回复凭据，control 无 TUI 恢复路径（chat 路径有 `sapper_workspace.py chat_message`），watchdog 尊重陈旧状态 74 分钟；② 唤醒验证链被任意 user 文本事件打断（含 Claude Code 注入的 `<task-notification>`），读板回执无法匹配 wake_id；③ 两次不可归因回执耗尽 `MAX_ATTEMPTS=2`，停发唤醒 18 分钟，仅用户手动 pause/unpause 后恢复。触发器为本地桥接 18765 全天 9+ 次 400 结束回合。预期：两次事故输入序列下指挥官空闲时间 ≤ 90 秒、无需人工干预。
- **实际改动**　`lib/sapper_commander.py` 三处：`configure()` 记录 `waiting_since`；`event()` 对合成事件（`<task-`/`<command-`/`<local-command` 前缀，经真实归档枚举确认）不断开 `accepted_wake_id`、不解除等待状态，并新增真实用户输入恢复 `waiting_user` 的对称路径；`record_board()` 将"唤醒已接收 + 同 agent/run 读板但被用户消息替换"的回执作为存活证据结束该轮唤醒（预算重置、不记 `board_confirmed`）。未改动：`MAX_ATTEMPTS` 上限、唤醒退避、API 错误快速重试、角色提示词 sleep 契约、桥接本身——均列为不确定项暂缓。
- **验证与结果**　`tests/test_sapper_commander.py` 新增 4 项回归（真实回复解除等待、合成事件不解锁、合成事件不断链、替换唤醒回执重置预算），指挥官专项 34 项全过；全量 `python3 -m unittest discover -s tests -q` 307 项通过。用真实事故 JSONL 回放：04:07:40 凭据回复后 control 恢复 active；`重试` 替换唤醒后回执结束该轮（预算重置）；`<task-notification>` 后回执直接 `board_confirmed`。未验证：真实模型长时间运行下的表现（需下一轮实战观察）。
- **证据**　watchdog 日志与审计（`.sapper-web/runs/0c269bd76b818dfa/`、`.sapper-web/actions.jsonl` 04:07–06:35 段）、事故时间线分析见本轮会话；行为契约同步更新 `docs/web/14-commander-liveness.md`。
- **决策与下一轮**　三项修复保留。下一轮候选（本轮明确暂缓）：唤醒失败退避替代硬上限、`isApiErrorMessage` 结尾的短间隔重试、角色提示词明确"主动等待用 sleep 工具"契约、桥接 400 根治。

### 2026-11-24 · 005 恢复 API 错误后空闲的 worker

- **问题与预期**　TsecBench 的 worker 进程仍在，但 400/500 使原生 agent 回合退出；mission 中的“稍后重试”无法驱动已经结束的循环。外部只发“继续”又可能得到一句承诺后再次空闲。预期只恢复明确的 API 错误空闲，不干扰正常完成、运行中的工具、主动等待或用户输入，也不让 watchdog 接管解题策略。
- **实际改动**　`lib/sapper_commander.py` 新增 `WorkerMonitor`，状态独立保存在 Web 私有目录 `worker-monitor/`。它复用原生 JSONL 增量观测，仅对 `isApiErrorMessage` 后的空闲且空输入框发送关联唤醒。提示让 worker 依据原 mission、当前 intent、共享图和已有产物，自主选择并立即执行一个具体工具动作；对应唤醒后的真实 `tool_use` 才确认 `work_confirmed`。无响应或再次失败按 120／240／480 秒退避，封顶 900 秒，无永久停发上限；真实用户输入优先。`lib/sapper_workspace.py` 将在线受管 TUI worker 接入现有 watch 循环，崩溃重启路径保持不变。
- **验证与结果**　`tests/test_sapper_worker_recovery.py` 新增 8 项：正常空闲不唤醒、工具确认、文字承诺不重确认、四次重试无硬上限、工具运行不中断、真实用户优先、未提交输入保护、停止／超时保护及 watch 接线。`tests/test_sapper_workspaces.py` 新增真实 fixture CLI＋tmux＋watchdog 集成：worker 首轮写入 API 错误，watchdog 实际注入一次恢复，收到工具事件后确认，run 与会话未重启。相关 74 项通过；全量 `python3 -m unittest discover -s tests -q` 316 项通过；`git diff --check`、Python 编译及 shell 语法检查通过。
- **决策与下一轮**　采纳为 workspace 受管 TUI worker 的恢复机制。原比赛 shell 分支、print 和 native_operation 未扩展。尚未验证真实模型长时间运行下的恢复率、误唤醒率及分数收益；下一轮用真实或归档故障序列比较“注入→真实工具→有效 fact／提交”的链路，而不是以发送成功或文字回复作为收益。

### 2026-11-27 · 006 无终端化架构阶段一至四（冻结规范 v1.0）

- **问题与预期**　全部 Claude Code 会话依赖 TUI 与 tmux，传输（send-keys）、唤醒、会话记忆、人工观察都绑在 pane 上，且角色权限与传输方式耦合、兄弟角色可互发消息。预期按《Sapper 无终端化与无头多 Agent 协作架构规范（冻结版 v1.0）》四阶段改造：控制面与上级 Agent 分权，宿主改为 `subprocess.Popen` 无头进程加 stream-json 双向管道，指挥官空闲唤醒与 worker 故障恢复形成闭环，网页端切到结构化轮次视图。
- **实际改动**
  - 阶段一、二 `fc3a26e`（阶段一改动并入）：`lib/sapper_workspace.py` 新增 `default_execution_mode()` 单一决策点，chat 模式对所有角色开放并按角色分级权限；新增 `lib/sapper_host.py`（SubprocessHost：独立进程组、代号握手、STARTUP_TIMEOUT／CRASHED、物理回收）与 `lib/sapper_routing.py`（RoutingLedger：event_id 路由、显式确认、超时升级、归属上移）。
  - 阶段三 `cac8624`：新增 `lib/sapper_turns.py` 结构化轮次流（`frames.jsonl`、`turns.jsonl`、`attachments/`、`diagnostics.jsonl`）与 `GET /api/runs/<rid>/turns`。
  - 阶段四 `12c5040`：新增 `web/turn-stream.js` 结构化视图与 chat tab，删除 xterm 终端面板、后端 tmux 二进制调用和三个 docker tmux 文件。附带两项修复：`lib/sapper_commander.py` 的 `not_before()` 按整秒比较回显时间与 `sent_at`；`lib/sapper-hook.py` 的 `_managed_messages()` 不再排空 chat 模式 agent 的收件队列。
- **验证与结果**
  - 自动化：`python3 -m unittest discover -s tests -p "test_*.py"` 373 项通过，`tests/*.cjs` 通过（2026-11-27）。
  - 真实 CLI 冒烟（claude 2.1.330 加自有模型网关，supervisor 与 watchdog 均为生产代码）：指挥官唤醒链实测 `wake_sent → responded → board_confirmed`，`last_board.wake_id` 与 wake id 一致。
  - 同秒回显未复现：6 个样本的回显延迟为 2.9–12.1 秒，全部落在发送的下一秒或更晚，新旧规则都通过。`not_before()` 在生产只是防御性的，它直接解决的是夹具同进程 1ms 回显导致的 4 项失败。
  - hook 修复经对照确认必要：去掉修复时 worker 的 mission 在 170ms 内由 `queued` 变 `provided`，此后没有任何一轮；保留修复时 `queued → sent → accepted` 并得到模型回复。
  - 中途撤销的尝试：曾为「外发帧不在 events.jsonl」加 `ChatJournal.record_outgoing()` 镜像外发 user 帧，未进入提交。生产 argv 自带 `--replay-user-messages`，CLI 自己会回显，镜像帧会造成重复并伪造 `input_accepted`。
  - 待验证：带工具调用的多角色长流程、浏览器可视验收、真实模型长时间运行。
- **决策与下一轮**　四阶段保留。未纳入本轮的清理项登记为阶段五：`print` 模式仍有活跃调用方未触碰，`docker/commander/*.sh` 内残留 tmux 分支，`worker.sh inbox` 的主动拉取在 chat 模式下仍会排空队列。本轮可复用的教训：夹具的时序不能代表真实 CLI，由夹具现象推出的根因需要真实 CLI 复核后才能写成结论。
