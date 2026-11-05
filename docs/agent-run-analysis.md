# Agent 运行数据分析

入口：[`scripts/analyze_agent_run.py`](../scripts/analyze_agent_run.py)。Python 3.10+，仅标准库，离线读取，不调用模型、不执行日志中的命令、不修改 Agent、原日志或共享图。

## 运行

分析整个项目（包含该项目的 `.sapper-agents` 会话）：

```bash
python3 scripts/analyze_agent_run.py \
  --project ~/project \
  --out .sapper-web/analysis/project
```

分析指定会话：

```bash
python3 scripts/analyze_agent_run.py \
  --transcript /absolute/path/session.jsonl \
  --store /absolute/path/project/.sapper \
  --out /absolute/path/report-directory
```

- `--transcript` 可重复，兼容原生 JSONL 和 `stream-json` 格式的 `output.log`。优先用原生会话：原始 stream-json 通常缺少时间戳、hook 和 API 请求快照。
- `--project` 自动在 `$CLAUDE_CONFIG_DIR/projects` 或 `~/.claude/projects` 查找；可用 `--claude-projects /path/to/projects` 指定导出的日志目录。会核对记录中的 cwd，防止目录名前缀碰撞。
- `--store` 默认 `PROJECT/.sapper`。没有图时也可以统计 token、工具、重复和 CLI 成果回报；图重建和已知 flag 匹配不可用。
- 同一个 session ID 的续跑合并分析；这里的“开始”是该会话选中日志的最早可用时间，不是最近一次进程启动。子 Agent 分开统计。需要单次运行时，传入那次运行的 stream-json 日志，并接受其缺失字段。
- `--near-threshold 0.9` 是近似阈值；`--near-window 80` 是每种工具/工作目录最近比较数量。`--near-window 0` 仅做完全重复检测。
- `--bytes-per-token 4` 调整离线估算系数，不能让它变成模型精确计数。
- 默认不导出原始工具参数或 flag 值。需要查看命令正文时加 `--include-inputs`，它会把原始参数写入本地 `tool_calls.csv` / `report.json`，可能包含凭证或 flag。默认报告仍包含本地来源路径。

输出目录包含：

| 文件 | 用途 |
| --- | --- |
| `report.html` | 离线打开，切换会话查看 token 和 facts/hints/open/done 曲线；内置 SVG，无外部依赖 |
| `report.json` | 完整机器可读结果、定义、覆盖率、诊断信息 |
| `steps.csv` | 每步输入 token、缓存、图注入估算与占比、图规模、工具结果数量 |
| `sessions.csv` | 每个会话的汇总、重复调用数量、fact/flag 步数与耗时 |
| `tool_calls.csv` | 每次调用的结果、错误、超时、来源文件/行号和参数哈希 |
| `tools.csv` | 按工具名称汇总错误率、超时率 |
| `duplicates.csv` | 完全/近似重复对应的两次调用、会话、步数、相似度、来源行号 |

报告目录新建时权限为 700，报告文件为 600。再次运行覆盖同名报告；源文件只读。读取活动日志时，每个文件限定在打开时的字节长度；它不是所有 Agent 同一瞬间的事务快照。

## 指标口径

### 1. 步数与输入 token

**一步 = 一次模型响应的 `message.id`**，从 1 开始。一次响应拆出的 text/thinking/tool_use 记录合并；相同事件 UUID、工具 ID 去重。一次响应可以调用多个工具。无响应的内部请求、API 重试、最终累计 result 不额外算步，也不再次累加 token。

`total_input_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens`。三个原始分量分别保留，这是上下文输入量，不是费用；缓存命中仍占上下文。口径依据 [Claude prompt caching 文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。每条响应分块中重复出现的 usage 按各字段最大已报告值合并，不求和。

统计信任日志中**供应方或本地协议适配器报告的 usage**，不声称独立核验供应方账单。缺失 usage 保留 `null`。没有 message ID 的旧记录退化为每条 assistant 一步，并在 diagnostics 标记；无时间戳则保留步数、耗时为空。

### 2. 图注入占比

原生日志若包含 `api-request-shape`、`api-request-blob`、`api-request`，按指定 `base` 请求的 `keep/tail` 重建每一步实际消息集合。日志省略的消息块，用合并后的 assistant 分块或连续 user 工具结果的规范 JSON SHA-256 匹配回填，不能匹配就保留未知。这样不会在上下文压缩后继续累计已经移除的图内容。

- `graph_hook_bytes`：请求中识别到的 Sapper SessionStart / UserPromptSubmit 图 hook 段 UTF-8 字节数，包含该段的引导说明与图内容；不包含无关的全局上下文。重复保留的历史注入会重复计入。
- `graph_hook_tokens_estimate = ceil(graph_hook_bytes / bytes_per_token)`：**离线估算，不是模型 tokenizer 实测**。中英文、代码及模型差异会影响误差；不调用网络计数 API。
- `graph_hook_share_estimate`：上述估算 / 本步 `total_input_tokens`。分母缺失或为零时为 `null`，不强行夹到 100%；超过 100% 会给诊断信号。
- `graph_read_tokens_estimate`：上下文中仍保留的 `sapper status/graph/dispatch/recall/doc` 工具返回量估算，单独列出，不混进 hook 占比。统计整段结果；复合 shell 命令可能混入其他输出。
- `graph_hook_weighted_share_estimate`：**仅在图内容和 usage 均可用的步骤**上求和后相除，不能把缺失步骤算成零。

旧日志只有 hook attachment、没有请求快照时，无法知道它在后续压缩、裁剪后还剩多少，所以注入占比为空。attachment 仍可提供图规模观测。未知格式或丢失的请求块会降低覆盖率，`steps_with_graph_request_snapshot / steps` 应和占比一起看。此统计识别当前 Sapper hook 标记，不涵盖任意手工粘贴或自定义格式的图。

### 3. 重复工具调用或命令

- 工具参数用解析后的 JSON 规范化后比较；工作目录不同不算同一次调用。shell 的展示用 `description` 不参与比较。
- 命令另建口径：只去除首尾空白，保留引号、换行、参数、顺序；不改写 shell 语义。相同命令仅改变工具的 timeout，也能在命令口径中检出。
- 近似检测为 `SequenceMatcher`，受阈值、最近窗口和 12,000 字符上限约束；超过长度上限仍检测完全重复。匹配会提供最近的完全重复，或窗口内相似度最高的候选。
- 支持跨选中会话检测；`same_session` 区分会话内/跨会话。工具和命令可能各产生一条匹配，不能把行数直接当作浪费次数；汇总提供各口径去重后的重复调用数。

**重复候选不等于无效操作**：轮询、外部状态变化后的验证、同命令读取已变化的文件都可能合理。可按来源行号回看实际证据，或按 `tool_id` 关联 `tool_calls.csv`。

### 4. 工具错误率与超时率

分母是**已收到结果的工具调用数**。未返回的调用单列 `tool_pending`，不会被算成成功或超时。

- 错误：`tool_result.is_error=true`、结构化非零 exit code，或已知 shell 工具的标准退出码行。
- 超时：结构化 timeout 标志，或已判定失败的结果含明确的 timed out / TimeoutError / ETIMEDOUT / 超时信号。
- 成功输出中出现“error/timeout”，例如读到源码中的异常处理文字，不算工具失败。
- `api_retries` 单列；进程预算耗尽不等于工具超时。异步工具返回“仍在运行”属于一次工具返回，不能据此断言后台进程已经完成。

工具没有明确输出错误信号的业务失败（例如 HTTP 200 返回错误业务码）不会自动归类。超时率反映**日志可识别的超时**，不是所有基础设施故障率。

### 5. 首个有效 fact 与 flag

用户确认的有效 fact 口径：**新建非 goal fact**，不要求带 doc。`first_fact` 用 `sapper fact/done/flag` 的创建回报定位调用步骤；若已有图记录显示它早于会话开始，则排除。`first_fact_with_doc_now` 另外列出当前带 doc 的首个 fact，不倒推 doc 当时就存在，也不等于独立验证。

Flag 分层报告：

- `first_flag_observed`：在工具输出中首次匹配到当前图 flag 台账中的精确值；排除图读取结果、工具输入已包含该值的回显、早于会话开始的旧 flag。用于定位“输出出现”到“记录入图”的延迟，不是对 flag 正确性的独立核验。未入台账的 flag 无法用这一口径识别。
- `first_flag_recorded`：`sapper flag` 的 recorded 回报，或新建 fact 在图中为 flag 类型。
- `first_flag_submitted`：CLI 的 submitted 回报，沿用当前 Sapper 的判定；不表示本脚本重新向平台验证了接受状态。

每项提供从会话开始的调用步数、秒数、fact ID 与来源行号；没观察到就为 `null`。不能把 `null` 解读成该 Agent 一定没有产出：直接写图、通过未识别的脚本或其他平台 CLI 产出可能没有可归因回报。

图是多个 Agent 共享的：`shared_project_during_session` 只表示会话时间范围内整个项目新增了 fact/flag，用 `steps_started_by_event` 对齐该会话时间轴，**不归功于当前 Agent**。已有存量不记为新产出；状态为 submitted 的当前图没有提交时间时，不能反推准确提交步骤。

### 6. 图规模曲线

每一步按请求开始时间（缺失时按 assistant 事件时间）对齐：

- `facts`、`hints`：现存节点 `created_at <= 步骤时间` 的数量；facts 包含 goal，与 `sapper status` 一致。
- `done`：已经有 `concluded_at` 且 `to != null` 的 intent；`open` 为已创建 intent 减去 done，包含 claimed 状态。
- 不把最终状态直接回填到所有历史步骤。已完成 intent 缺失结束时间时，受影响时段的 open/done 为未知。
- `observed_facts/hints/open/done`：日志中最近一次图计数的观测值，另列 `observed_age_seconds`，不假装它随时都是最新状态。

现存节点重建无法恢复已经删除的节点，也无法获知人工改写时间戳；旧状态不是完整事件审计。图内容与日志均可能继续变化，重跑得到的结果应保留生成时间和输入文件长度用于比较。

## 验证

```bash
python3 -m unittest discover \
  -s tests \
  -p test_agent_run_analysis.py -v
```

测试覆盖分块去重、缓存口径、差量/指定基准重建、未单独存 blob 的原生消息、压缩清除、未知数据、错误/超时分母、重复候选、共享图归因、flag 回显排除和输出格式。
