# 指挥官续轮与恢复确认

日期：2026-11-02。范围：显式 workspace 模式的受管交互式 commander。

## 修复的问题

指挥官说“60 秒后继续”后可能返回 `end_turn`，而没有执行等待工具。Claude TUI 此时仍存活，旧 watchdog 会跳过它，直到用户手动输入 `continue`。提示词不是定时器，恢复进程也不代表模型已恢复调度。

新增 `lib/sapper_commander.py`，由已有工作区 watchdog 每轮调用。指挥官仍负责策略、派发与回收；监控器只负责状态观测、续轮输入及异常进程恢复。原比赛脚本分支、其他角色和正常结束的 print 任务不增加自动续轮。

## 可验证的状态

- 只按已登记的 Claude UUID 查找唯一的原生 JSONL 会话，不选“最近会话”。支持记录的 config 目录，以及旧角色的默认配置目录。
- 增量读取主会话的 user / assistant 事件，忽略 sidechain。工具尚未返回、模型处理中、等待 AskUserQuestion 时不插入续轮指令。静默不等于卡死。
- `end_turn` / `stop_sequence` 且没有未返回工具时，视为本轮结束；CLI 标记 `isApiErrorMessage` 的结束响应单独记录为 API 错误后的空闲，仍不代表恢复成功。新子角色状态、新 fact / hint 可提前唤醒；否则结束约 60 秒后兜底唤醒。已有 fact 文件的原地修改由周期轮询兜底。
- 每次唤醒生成唯一编号，依次记录发送、原生用户消息接收、模型响应、真实读板确认。发送成功不代表恢复成功。
- 原 `worker.sh board --json` 在成功生成并输出看板后，写入 agent、当前 run、时间和对应唤醒编号的回执。其他角色、历史 run、未被接收的唤醒、被新用户消息替换的唤醒和不可读取的看板不能确认该次续轮。
- 当前轮仍在执行时不重启。没有读板回执超过 120 秒会显示提示；只有再次确认空闲才允许补发，总共最多两次。达到上限后保留异常供人工检查。迟到的有效回执仍可确认恢复。
- 进程真正退出时，按原 UUID 有限恢复，并通过新的继续消息触发读板；`process_started` 仍需真实回执才能变成 `board_confirmed`。未验证成功前最多恢复两次，计数跨 watchdog 重启保存。

监控状态保存在 Web 私有目录 `commander-monitor/<agent-id>.json`，使用文件锁串行更新。停止、删除、恢复、续轮共用原生命周期锁；多 watchdog 同时观察一个跨工作区指挥官时不会重复发送同一次唤醒。回执证明进入了读板流程，不证明后续策略质量。

Watchdog 输出启动配置、立即发生的状态变化，以及无变化时每 60 秒一条心跳（检查仍为每 3 秒一次）。日志包含指挥官身份、阶段、确认状态、当前唤醒编号／次数、最近读板回执和停止重试的原因，不输出任务文本或模型内容。服务卡片的“查看日志”打开该次 watchdog 运行；重启后应选择最新的运行中服务，旧日志保持原样。只有最初启动行的旧 watchdog 不代表没有检查，它可能还没有加载本次日志增强。

## 用户控制

角色卡片展示执行阶段、确认状态、最近读板时间及异常，并提供“暂停续轮 / 启用续轮”。启用会确保当前工作区 watchdog 已启动。

```bash
bash /path/to/sapper-v2/docker/commander/worker.sh control paused --reason '人工检查中'
bash /path/to/sapper-v2/docker/commander/worker.sh control waiting_user --reason '需要选择下一步方向'
bash /path/to/sapper-v2/docker/commander/worker.sh control completed --reason '任务完成依据'
bash /path/to/sapper-v2/docker/commander/worker.sh control active
```

命令需要当前受管指挥官的 workspace 环境。网页接口为 `POST /api/agents/{id}/control`，沿用现有登录与 CSRF 校验，正文为 `{"control":"paused","reason":"人工检查中"}`。这些控制只影响指挥官自动续轮，不停止子角色。

用户停止、删除、运行进入 stopping / stopped / timed_out、明确暂停、等待用户或任务完成时不自动续轮。原生记录中的用户中断也会暂停；随后真正的用户继续输入可解除该次中断暂停。全部关联图标记 completed 时自动停止续轮。不能只凭一段自然语言总结判定整项任务完成；新提示词及每次唤醒都要求指挥官在需要决策或完成时显式设置状态。

如果找不到唯一会话文件、格式不能识别、记录未写完或历史还没有扫描完，则不猜测空闲。终端空输入框只作为最后的发送保护，不能覆盖用户正在输入的文本，也不用于证明模型正在执行。JSONL 是当前本地 CLI 的观测适配；不同版本或存储布局需要验证，未支持时页面显示无法确认。

## 生效和验证

需重载 Web 服务，并重新启动已运行的旧 workspace watchdog；主指挥官 TUI 不需要重启。现有 commander 可通过按 UUID 读取记录接入；新创建 / 恢复的角色获得 `/3` 提示词。未开启 watchdog 时不会出现后台自动续轮。检查本身不调用模型，真正唤醒会产生新的模型调用。

自动测试不调用真实模型或靶场。覆盖结束后无 sleep 的续轮、编号及 run 隔离、真实 worker 回执、等待工具和用户、停止及完成、缺失 / 部分 / 大历史记录、有限重试、子角色和图事件，以及隔离 tmux 中原地唤醒和进程丢失后按同一 UUID 恢复并确认真实读板。

Claude 事件语义参考 [官方 hooks 文档](https://code.claude.com/docs/en/hooks)：Stop 表示一次响应结束。这里没有阻止 Stop 的 hook，也没有通过无限阻止结束来实现调度。

本轮完整回归 `python3 -m unittest discover -s tests -q`：145 项通过；随后补充多 watchdog 并发去重与原生终端中断两个用例，指挥官专项 21 项全部通过。Python 编译、前端 JS、shell 语法及 `git diff --check` 通过。隔离 tmux 验证了原地唤醒与崩溃后同 UUID 恢复，两者均取得实际 worker 读板回执；未使用截图或调用测试模型。
