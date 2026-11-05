# Agent 级 MCP、Skills 与会话存储

顶部或侧栏 **全局设置** 统一管理模型、MCP 服务器和 Skills。MCP / Skills 使用可搜索的全局列表，展示名称、命令或目录及使用中的工作区数量；通过“添加”或“编辑”展开配置表单。

**全局设置 → 工作区配置** 选择工作区并保存其角色模型和资源选择；角色模型与资源各自保存，互不覆盖。也可从“工作区操作 → 工作区使用配置”直接打开当前工作区。资源支持全部启用／全部关闭；切换工作区前若有未保存的更改，会提示保留或放弃。启动角色弹窗展示这些选择，取消“使用工作区默认选择”后可单独勾选；全部取消表示不加载 MCP 或自定义 skills。

资源库可在未创建或选中工作区时管理。`GET/POST /api/extensions` 管理全局资源，`GET/POST /api/workspaces/:id/extensions` 读取或保存某工作区的选择；两者沿用同一个配置版本防止过期写入。已被工作区选中的资源须先取消选择才能移除。

资源库在 Web 私有目录的 `extensions.json` 中保存，默认选择按工作区 ID 区分。子角色自动派发时使用**目标工作区**的默认资源，不复制指挥官的资源选择。手动启动可通过请求体 `extensions: {mcp_ids: [], skill_ids: []}` 覆盖默认值。

## MCP

支持 stdio、HTTP、SSE server 配置，也可导入标准 `{"mcpServers": {...}}` JSON；导入中的同名 server 更新原条目。保存/导入不会执行 server，创建会话时才交给 Claude CLI 加载。

```json
{
  "type": "http",
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer ${MY_MCP_KEY}"}
}
```

`env` 和 `headers` 只接受环境变量引用（headers 可带 `Bearer ` 前缀），实际值来自 Web 服务进程环境，启动前检查是否存在。不要把密钥写入 command、args 或 URL。URL 不接受嵌入凭据、查询参数和片段。命令及参数保持 argv 形式；stdio server 的相对路径以 Agent 的 cwd 为准，建议使用绝对路径。

每个会话都传入 `--mcp-config <snapshot>/mcp.json --strict-mcp-config`，空选择也生成空 `mcpServers`，不回退到系统或共享项目 MCP 配置。

## Skills 与工作区说明

在 Web 中登记执行机上含 `SKILL.md` 的目录；名称须与 frontmatter 的 `name` 一致（如果声明了 name）。每次新建会话复制所选 skill 的完整目录，包括脚本、参考资料和执行权限，通过独立插件 `--plugin-dir` 加载，调用名为 `/sapper:技能名`。不修改源文件，不链接到可变源目录。内部符号链接、特殊文件、单 skill 超过 2000 文件或 32 MiB 时拒绝导入。

新会话使用 `--setting-sources ""` 关闭 user/project/local 配置来源，独立 `CLAUDE_CONFIG_DIR`，并关闭 bundled skills；对 2.1.318 仍暴露的 `plugin-authoring` 和 `doctor` 另设 `skillOverrides: off`。真实 CLI 验收会比较完整 skills 清单，空选择要求清单为空。内置 CLI 命令仍可用；这是 Claude 配置加载的隔离，不是文件系统沙箱或新的工具权限。组织托管策略的优先级不会被 Sapper 覆盖。

关闭配置来源也会关闭自动读取项目说明，因此 Sapper 将工作区根的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md` 和其中独占一行的显式 `@文件路径` 导入展开、快照并追加到系统提示词。支持至多 5 层导入、合计 256 KiB；代码围栏里的 `@` 不作为导入。不自动读取工作区外的祖先说明；工作区说明中的显式绝对路径导入仍可读取。Sapper 的 SessionStart/UserPromptSubmit hooks 从会话专属 settings 显式加载，共享上下文和事实图保持原有机制。

## 存储与恢复

```text
<workspace>/.sapper/sessions/<claude-session-uuid>/
  manifest.json
  bundle/
    mcp.json
    settings.json
    instructions.md
    plugin/
      .claude-plugin/plugin.json
      skills/<name>/...
  claude/
    projects/<cli-project-key>/<session-uuid>.jsonl
    ...
```

`CLAUDE_CONFIG_DIR` 指向该会话的 `claude/`。cwd 与存储目录分离，执行者仍可在题目根目录工作。Web Agent 索引继续保存在 Web 私有状态目录，记录 snapshot 路径、清单摘要、原生 UUID 和生效资源列表。

新会话仅使用模型配置/API 环境变量，不继承 OAuth 登录态或其他 Claude 会话的运行开关。支持现有 API 网关与协议桥接；没有 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` 或模型配置提供的凭据时，在启动进程前报错。不复制 `~/.claude` 的凭据、设置、插件或历史。

独立数据目录下的 TUI 使用文档支持的 `IS_DEMO=1` 跳过每个新会话都会出现的首次设置向导；该变量还会隐藏终端头部的账号信息。使用 `ANTHROPIC_API_KEY` 时，启动器会在该会话私有的 `claude/.claude.json` 中写入 Claude 原生要求的密钥后 20 位批准标记和初始化完成状态，不保存完整密钥；`ANTHROPIC_AUTH_TOKEN` 不需要此标记。工具权限仍由原来的启动参数控制，不因此添加权限跳过参数。

恢复始终传入原 UUID、原数据目录和原配置快照。清单或快照文件被修改、删除，或变成符号链接时拒绝恢复，不重新从资源库生成。编辑/删除资源库条目不改变已有快照；凭据环境变量可以轮换。指挥官读取 transcript 的路径跟随 agent 记录，分析脚本的 `--project` 同时发现工作区 session 和旧的全局历史。

旧会话不自动迁移，仍按原路径和原启动规则恢复。删除角色保留会话历史。普通工作区文件接口不提供 sessions 目录或其符号链接别名，避免把原始会话内容当普通附件下载。使用 Git 管理外部工作区时也应将 `.sapper/sessions/` 加入该工作区的忽略规则。

## 兼容与验收

当前隔离组合的已验证最低版本为 Claude CLI **2.1.318**。能力探测检查实际配置的可执行文件、版本及 `--mcp-config`、`--strict-mcp-config`、`--plugin-dir`、`--setting-sources`、`--settings`。不满足时在新会话启动前报错，不自动升级或取消隔离。升级 CLI 后需重启 Web 后端刷新进程内能力缓存。

```sh
python3 -m unittest discover -s tests -p 'test_agent_extensions.py' -q
```

该测试使用临时工作区和模拟启动，验证选中/未选中 MCP 与 skills、hooks、说明、落盘和精确恢复，不调用真实模型或 tmux。

官方接口参考：[CLI 参数](https://code.claude.com/docs/en/cli-reference)、[Skills 加载来源](https://code.claude.com/docs/en/skills)、[配置与环境变量](https://code.claude.com/docs/en/env-vars)。
