# Sapper Harness 优化建议（来自 TSecBench 63 题实战）

> 来源：2026-11-24 TSecBench 跑分任务，63 题 / 74 flag / 23000 分，最终 18 flag / 4500 分。
> 以下按「影响面 × 可实施性」排序，每条都附实测证据。

---

## P0 — 直接决定吞吐量

### 1. worker 撞 API 错误后无限卡死，需外部注入才能恢复（已实现 workspace 版）

**现象**：`API Error: 400 ...` 或 `400 上游返回 HTTP 500` 后，Claude Code 的 agent 循环退出，停在输入提示符。mission 里写"等 30 秒重试"完全无效——它已经不在循环里了。

**实测代价**：4 条并行线，几乎每轮都有 1-3 条卡死，每次浪费数分钟到十几分钟。

**2026-11-24 实现**：
- 工作区 watchdog 通过 `WorkerMonitor` 增量读取 worker 的原生 JSONL；只有 `isApiErrorMessage` 结束、当前空闲且输入框为空时才注入恢复输入。正常 `end_turn`、工具运行、AskUserQuestion、用户正在输入和停止／超时状态均不触发。
- 不用 `output.log` mtime 猜测状态，不重启健康进程，也不由 watchdog 选择解题路线。恢复提示要求 worker 读取原 mission、当前 intent、共享图和已有产物，**自主选择并实际执行一个具体工具动作**。
- 唤醒编号与原生 user 事件关联；只回复"收到／继续"不算恢复，收到对应该次唤醒的真实 `tool_use` 才标记 `work_confirmed`。
- 无响应或再次 API 错误时按 120、240、480 秒退避，之后封顶 900 秒，不设永久停发的次数上限。真实用户输入优先并取消待恢复状态。
- 本轮范围是 `SAPPER_MODE=workspace` 的受管 TUI worker；原比赛 shell 分支和 print/native_operation 工作流未增加这套恢复。实现与验证见 `docs/rsi-log.md` 005。

### 2. 网关限流为账户级 2 req/s，但错误形态误导

**实测**：
```bash
# 并发 6 请求
2×200 + 4×429 RATE_LIMIT_EXCEEDED {"limit":2,"limit_type":"request_count","scope":"account"}
```
- 限流真实形态是 **429**，但 Claude Code 常展示为 `400 无法连接模型服务`
- 上游间歇性 **500** 与会话大小无关（新会话 10 分钟/31KB 也复现，同时单发 curl 200 正常）

**建议**：
- 在 `config.json` 的 profile 里增加 `rate_limit` 字段（req/s），supervisor 据此做**客户端限速**，而不是让 worker 撞墙。
- 区分错误类型：429（限流，应退避重试）/ 500（上游故障，应等待）/ 网络错误（应换通道），给不同的重试策略。

### 3. 多 worker 共享平台资源时缺少配额仲裁

**现象**：Agent 会自行"抢槽"（5 秒轮询 `arena start`），并互相建议"你去停别人的容器"。

**建议**：
- 平台侧配额（如"同时最多 3 个容器"）应作为 workspace 级配置，由 `worker.sh` 仲裁：`start` 请求排队，而不是让 worker 自己抢。
- 提供 `worker.sh quota --status` 让 worker 查询当前谁占了哪些资源。

---

## P1 — 显著提升单题产出

### 4. 缺少结构化的"负结果"沉淀

**现象**：worker 记录 `xx 未解` fact 时，内容质量差异极大。好的记录（"services.php 在 b-03 是 404，无 LFI，攻击面只剩 /admin/login.php，已排除 SQLi/弱口令/Cookie 绕过"）能让后续 agent 直接跳过；差的只有"未解"两个字。

**建议**：
- 给 `sapper fact` 的 `--kind note` 增加 **未解模板**：必须填「已试路径 / 排除依据 / 剩余可能 / 建议下一步」四段，否则拒绝写入。
- 或者：`sapper done <intent> --unsolved --tried "..." --ruled-out "..." --remaining "..."` 专用子命令。

### 5. worker 上下文膨胀后产出质量下降，但换人缺少标准流程

**实测**：1.4MB+ 日志的会话仍能工作，但效率明显下降；反复 500 才是硬信号。

**建议**：
- `worker.sh rotate <agent-id>` 一条命令完成：保存交接摘要 → kill → spawn 新 agent 带着摘要和同一容器。
- 交接摘要自动从 graph 里提取该 agent 产出的 facts + 未解记录。

### 6. 缺少"无槽期准备"的机制支持

**现象**：a-13 的成功很大程度因为等槽的 25 分钟里完成了利用链制备（本地 mock 环境调 payload）。但这是巧合，不是设计。

**建议**：
- 当 `arena start` 返回配额满时，`worker.sh` 可主动建议："当前无容器，建议先做 X"（读题面 / 写利用脚本 / 本地复现）。
- 或者提供 `sapper prepare <challenge>` 骨架：创建 `challenges/<code>/{notes.md,exploit.py,recon/}` 目录结构。

---

## P2 — 工程质量

### 7. `sapper arena challenges` 在平台返回顶层 list 时崩溃

```
AttributeError: 'list' object has no attribute 'get'
```
`lib/sapper.py:2161` 假设响应是 `{"result": {...}}`，实际平台返回顶层数组。

**建议**：兼容三种形态（list / `{"result":...}` / `{"data":...}`）。

### 8. 凭据注入路径不统一

**现象**：`lib/arena.py` 读环境变量或 `.sapper/arena.json`；但 web 启动的 commander 进程环境里没有 `BENCHMARK_*`，导致初始完全无从下手（浪费了第一轮）。

**建议**：创建 workspace 时，若用户提供了平台凭据，直接写 `.sapper/arena.json`（600 权限），并在 commander 的 system prompt 里明示"凭据已就绪"。

### 9. 知识库缺少"从实战回灌"的捷径

**建议**：`sapper kb add --from-fact fNNN`，把已有 fact 的 doc 直接转成 KB 条目，减少手工整理成本。

---

## 附：本次实战的有效做法（值得保留）

| 做法 | 效果 |
|---|---|
| 三线并行 + 每线独占 1 容器 | 容器利用率 100%，无明显争抢 |
| 每题 12-15 分钟时间盒 | 阻止了多次 50-90 分钟的无谓消耗 |
| 按题面线索直打（XXE/SSRF/SSTI 等） | a-07、a-16 等题几分钟内命中 |
| 负结果写清"已排除什么" | b-03 卡点被准确记录，后续无人重复 |
| 无槽期做利用链制备 | a-13 一次成功（八进制 cookie + pydash 原型链 + 正则绕过） |
| flag 逐题提交不攒 | 避免了容器失效导致的丢失 |
