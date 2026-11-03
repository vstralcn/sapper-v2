# 模型配置：协议、模型发现与连接测试

版本：v0.8 · 2026-11-02。

## 使用方式

重启 Web 服务后，打开侧栏“模型配置”。每组配置可以选择：

| 协议 | 实际接口 | 运行方式 |
| --- | --- | --- |
| Anthropic Messages | `/v1/messages` | 原 Claude Code 直接使用所选网关 |
| OpenAI Responses | `/responses` | 本机 HTTP 协议适配，原 Claude Code 继续执行工具 |
| OpenAI Chat Completions | `/chat/completions` | 同上，转换函数调用和结果 |

本次 `response/completion` 对应 Responses／Chat Completions；不包括无法表达标准工具调用的旧式文本 `/completions`。

1. 填写名称、协议、API 地址和密钥。OpenAI 地址一般以 `/v1` 结尾，也接受完整 `/responses` 或 `/chat/completions` 地址。Messages 地址支持服务根、`/v1` 或完整 `/v1/messages`，运行时统一为 Claude 所需的根地址。
2. 点击“获取模型”：直接使用当前表单，无需先保存或先填模型 ID。后端请求相应模型列表，支持 Anthropic 分页。列表可筛选、点击选择，模型 ID 仍可手动输入；未发布模型列表的网关也能手动配置。
3. 点击“测试连接”：对所选模型做一次短文本生成，显示耗时和返回文本；会消耗相应服务的请求额度。该操作验证生成接口，不代表已验证所有工具、上下文长度和多模态能力。
4. 保存配置，在启动角色或“原生功能”中选择。不同角色可使用不同协议／模型；配置列表与选择器显示协议。

编辑时密钥留空保留原值，勾选“清除已保存密钥”才清除。未配置密钥时按协议读取执行机环境：Messages 使用 `ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY`；另两种使用 `OPENAI_API_KEY`。地址为空时分别使用 `ANTHROPIC_BASE_URL/OPENAI_BASE_URL` 或官方默认地址。完全沿用原环境的工作区角色仍可直接选“继承原环境”，保留原登录与设置。

高级设置提供 HTTP 超时（模型列表、连接测试和 OpenAI 适配请求）、适配输出 token 上限、Chat Completions 的 token 参数名。上限 0 表示跟随原 agent；老网关只接受 `max_tokens` 时可手动选择该参数，默认使用 `max_completion_tokens`。Messages 原 CLI 的请求超时／输出行为继续由原 CLI 控制。自定义 `SAPPER_BIN` 仅适用于 Messages 原参数包装器，新协议使用原 Claude Code。

## 保留原 agent 的运行方式

适配层只处理 HTTP 消息格式：system／回合内 system／user／assistant、函数工具定义、调用 ID、参数、结果、文本与图像／文件输入、最终停止原因和 token 使用量。不执行工具，也不另建模型决策循环。原 Claude Code、hooks、skills、MCP、工作区、原 sapper 图与角色派发机制继续工作。

OpenAI 的流式文本转为原 Messages SSE；函数参数收齐且 JSON 校验通过后才交给原 agent。连接中断、缺少完成事件或参数不完整会显示错误，不会用成功事件掩盖，也不执行残缺工具调用。模型返回供应商专用工具或不支持的内容块时明确报错，不静默删除它们。

真实接入发现 `settings.json` 的 `env` 会覆盖进程里的供应商地址与密钥。显式选定网关／密钥或 OpenAI 协议时设置官方 `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`，让网页所选路由生效；不修改用户设置文件，不卸载项目 hooks／工具。无网关／密钥的默认 Messages 配置仍保持原环境行为。已在 Claude Code 2.1.316 验证；旧 CLI 若不支持宿主管理路由，需升级或检查其配置优先级。

配置变更不会热切换已有会话。恢复已有角色时同时检查模型、地址和协议；改变协议后须还原配置或新建会话，避免把原会话静默切换到另一种模型协议。

## 后台适配服务

Responses／Chat Completions 按连接配置快照启动独立的本机进程，只监听 `127.0.0.1`，使用随机访问口令。相同快照复用服务，不同配置互相隔离。Web 或原 `fleet up` 控制命令退出后，服务仍能供原角色与独立编队使用。

“模型配置 → 后台协议适配服务 → 刷新服务状态”可查看协议、模型与 PID，并停止选定服务。停止会中断使用它的模型请求，页面会先确认；新启动或恢复的角色会按配置重新建立适配服务。修改或删除配置不会自动杀掉仍在使用旧快照的服务。

私有文件位于 `.sapper-web/model-bridges/<连接摘要>/`。启动凭据文件读取后即删除；服务记录的随机口令仅保存在私有文件中，不返回浏览器。密钥和适配口令加入文字日志遮蔽。HTTP 默认验证 TLS，拒绝携带凭据跟随重定向；不提供跳过证书校验选项。

## 验证与已知边界

- 103 项自动测试全部通过；Python 编译、四个前端 JS、原 commander 脚本语法检查及 `git diff --check` 通过。
- 本地 HTTP 测试覆盖三种协议的模型发现与文本生成、Anthropic 分页、密钥保留／清除、URL 规范化、错误响应遮蔽、禁止重定向、认证与 CSRF。
- 测试两种协议的流式／非流式函数调用、工具结果、图片、回合内系统消息、输出上限、异常中断、独立服务复用与停止、协议变更后的恢复拒绝。
- 真实 Claude Code 连接本地可控测试服务，经独立适配进程分别访问 `/v1/responses` 和 `/v1/chat/completions`。每种协议均收到两次请求：工具调用、工具结果回传；原 Claude 的 Write 工具实际生成 `protocol-proof.txt`，内容为 `ORIGINAL_CLAUDE_TOOL_OK`，两个运行退出码均为 0。这证明原 CLI 与协议适配／工具执行链路，不是远端供应商推理质量或兼容性证明。
- 浏览器验证已保存密钥的模型获取、模型筛选／选择、两种协议的连接测试、修改模型并保存、不可达服务错误反馈，以及后台服务查看／停止。

任意远端网关仍需使用自己的地址和模型做实际验证。供应商的思考签名、缓存标记及专用 API 能力不能跨协议等价转换；OpenAI 模型按其默认推理行为工作。Responses 的加密推理项在适配进程内按工具调用 ID 保留，停止适配服务后不保留这部分缓存。`count_tokens` 为本地上下文估算，非对应供应商的精确 tokenizer 或计费凭据。原生 Messages 通道不经过这些转换。

## 接口与实现

- 配置增加 `protocol/timeout_seconds/max_output_tokens/token_field`，旧配置未指定协议时默认为 `anthropic`；API 也接受 `response/completion` 别名。
- `POST /api/profiles/models`：以当前表单获取模型；`POST /api/profiles/test`：当前表单连接测试。提供已保存 `id` 时可复用其密钥，响应不返回密钥。
- `GET /api/profiles/bridges`：适配服务状态；`POST /api/profiles/bridges/{id}/stop`：停止选定服务，沿用认证与 CSRF。
- `lib/sapper_models.py` 负责协议与 HTTP 适配；原引擎未替换。只使用现有 Python 标准库，无新增 npm／pip 依赖。

接口依据：[OpenAI 模型列表](https://developers.openai.com/api/reference/resources/models/methods/list)、[OpenAI 函数调用](https://developers.openai.com/api/docs/guides/function-calling)、[OpenAI 流式响应](https://developers.openai.com/api/docs/guides/streaming-responses)、[Messages 流式事件](https://platform.claude.com/docs/en/build-with-claude/streaming)、[Claude Code 环境变量与宿主管理路由](https://code.claude.com/docs/en/env-vars)。
