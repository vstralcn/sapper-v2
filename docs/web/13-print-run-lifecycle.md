# 限时原生会话：观测、续时、停止与恢复

日期：2026-11-02。

## 边界

修复位于现有 Web／本地工作区适配层。角色仍由原 Claude Code 执行，通过原 `worker.sh` 接口派发、同一 `.sapper` 图共享证据，并用已登记的 UUID 恢复。没有新增模型客户端、解题循环或自动调度策略。原 `sapper auto/dispatch` 的单 JSON envelope、比赛模式默认脚本分支和 TUI 启动方式保留。

新限时角色使用 `-p --output-format stream-json --verbose`，不默认打开逐 token 事件。`lib/sapper_run.py` 只观察事件，不接管工具执行。工具、skills、MCP、模型配置及网关仍沿原入口生效。输出参数和中断语义参见 [Claude Code 官方文档](https://code.claude.com/docs/en/headless)。

## 运行观测

- `output.log` 继续由原 tmux `pipe-pane` 采集原始输出，包含原始 JSON、stderr 警告及 supervisor 提示；网页可下载。
- `activity.log` 提供可读的工具调用／返回、文本、重试和最终结果；网页“查看活动”及 `worker.sh peek` 默认读取它。其他类型和历史运行仍读原 `output.log`。
- `activity.json` 保存最近事件、最近工具、消息摘要、是否收到最终结果。角色卡片和 `board --json` 展示观测数据；事件活动不等于有效进展，静默也不等于卡住。
- 原事实图及产物文件继续提供实际成果证据；角色职责版本 `/2` 要求在有意义的阶段保存证据、脚本、已排除路径和下一步，不把全部交接押在最终回答上。
- print 会话的“补充说明”通过原共享 hint 保存，下一次读图时可见，不保证立即被模型采用。TUI 仍使用原终端输入。

## 预算与停止

预算从子进程实际启动时计算，supervisor 用单调时钟计时。网页显示剩余时间；预算将耗尽时记录提示。这是给操作者的提示，不伪称已经向运行中的模型发送新指令。

新运行的预算控制保存于独立 `control.json`，通过文件锁串行处理续时与停止决定，避免 Web 和后台指挥官同时续时时丢失更新。`run.json` 由 runner 记录生命周期；接口合并显示当前预算。续时不会改写任务、模型或会话身份，每次必须增加整数分钟，单次运行累计不超过 1440 分钟；恢复后的新 run 单独计算预算。

预算耗尽或明确停止 print 角色时，先进入 `stopping`：

1. 对该 print 会话的独立进程组发 SIGINT，最多等待 10 秒，并继续采集输出。
2. 未退出则发 SIGTERM，再等 3 秒。
3. 仍未退出则发 SIGKILL，并清理该组遗留工具；不向其他 tmux 会话广播信号。

预算耗尽最终标记 `timed_out`，用户停止标记 `stopped`；退出码保留实际值，是否收到最终结果另行记录，不能把中断后的退出码 0 当成任务完成。SIGINT 不保证模型补写总结；进程退出后的清理及输出排空可能增加少量收尾时间。TUI 保持原 PTY 和停止方式，不新增运行预算。

watchdog 不自动恢复 `timed_out` 或明确停止的角色；指挥官根据当前证据决定续跑、换方向或结束。运行中的 `resume` 仍返回现有 run，不能用来续时。已开始停止或已结束的 run 拒绝续时。

## 操作入口

网页角色卡片和运行记录均提供“延长预算”；结束后“恢复此会话”继续使用已登记的 UUID。print 恢复会发送明确的继续消息，默认先核对原图和文件，再执行未完成步骤；TUI 恢复后的输入行为见 [12 恢复规划](./12-resume-continuation-plan.md)。

在显式工作区环境下，沿用原 worker 入口：

```bash
bash docker/commander/worker.sh peek <agent-id>
bash docker/commander/worker.sh extend <agent-id> --minutes 15
bash docker/commander/worker.sh resume <agent-id> --minutes 30 --mission "核对已有脚本和证据，继续未完成的验证"
```

接口继续使用现有登录、CSRF 和工作区范围校验：

```text
POST /api/runs/{id}/extend       {"minutes": 15}
POST /api/agents/{id}/extend     {"minutes": 15}
POST /api/agents/{id}/resume     {"minutes": 30, "continuation": "从已保存步骤继续"}
```

## 生效与验收

Web 服务需重新加载代码，网页需刷新。已经启动的旧 runner 不会热升级，旧 run 不支持动态续时；不会为了应用修复而自动中断它们。旧任务结束后按原 UUID 恢复，将建立采用新 supervisor 的 run。原本的 10 分钟默认预算保留，可按任务调整，未强制所有任务使用 30–45 分钟。

自动验收使用临时目录、独立 tmux socket 和假 Claude CLI，不调用真实模型或靶场。覆盖实时工具事件、中文输出、并发续时、越过原截止时间、服务对象重建后预算保留、停止后的续时拒绝、SIGINT 最终结果及产物保留、强制清理不响应信号的工具、其他会话隔离、超时不被 watchdog 重启、原 UUID 恢复和 worker 命令接入。完整 `python3 -m unittest discover -s tests -q`：123 项通过。随后补充中文分块保真／活动摘要脱敏检查，修正停止审计及恢复消息的长度处理，再复测 Web 20 项、工作区 23 项，全部通过。Python 编译、两份前端脚本语法、shell 语法及 `git diff --check` 通过。

Chromium 无头浏览器在隔离服务中完成登录、查看实时工具与剩余预算、通过表单增加预算、打开可读活动日志、将补充说明写入 hint、停止并点击恢复同一原生会话；没有页面脚本错误，没有生成截图。

没有将假 CLI 验收等同于所有模型／网关的兼容性验证，也未修改现有正在运行的任务。
