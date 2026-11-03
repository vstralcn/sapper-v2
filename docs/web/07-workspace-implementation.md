# 工作区与角色：实现和验收记录

日期：2026-11-02 · 对应 [06 规划](./06-workspace-and-roles-plan.md) · 启动见 [05 使用说明](./05-running.md)。

## 1. 本次交付

保留原 Claude Code 工具执行、原 CLI、JSON 事实意图图、原比赛脚本默认分支。本次新增的是 Web 控制层及显式本地工作区分支。

| 工作包 | 实现结果 | 验收范围 |
| --- | --- | --- |
| P0 接入 | hook 改为导入实际 sapper 模块，输出 additionalContext；SessionStart 也带图上下文；项目级说明桥接 | 两类真实 hook 子进程、无图静默、事件 cwd、不继承父题图、设置与用户说明保留 |
| P1 工作区 | 允许根目录内绑定原目录；稳定 ID；绑定和启用分开；实际 CLI 能力检查；原生 UUID 启动／恢复 | 中文／空格路径、不搬资料、重复启用、已有文件保留、同 cwd 两个不同 UUID、按 ID 恢复、Web 重启 |
| P2 角色 | 指挥官／将军／侦察兵／执行者／验证者／自定义职责；父子派发树、cwd、模型、UUID、状态和图贡献；旧 pane 显式登记 | 指挥官经原 worker 入口后台派发侦察兵，子角色自动登记，回写图并能打开原事实详情；不将 watchdog 算作角色 |
| P3 协作 | SAPPER_MODE=workspace 入口、无平台本地派发、角色独立 cwd 与共享原图、消息与停止；共享指挥官范围 | 子角色读原题并写成果；跨题目父身份与停止不串；明确停止／正常单次结束不被保活；修改模型／地址不能静默恢复旧会话 |
| P4 终端 | xterm.js、tmux PTY、SSE 输出、顺序输入、单 pane 调尺寸、断线续传、外部订阅兼容回退 | HTTP 登录／CSRF、原始空格与 Unicode 输入、尺寸、Last-Event-ID 续传；浏览器输入和恢复；真实 Claude 小任务通过 |
| P4 部署 | 启动参数、路径前缀、HTTPS cookie、代理 SSE 说明、可选 Dockerfile | 本机 HTTP 与子路径测试；Linux／HTTPS／镜像构建未验收 |
| P5 原生角色策略 | `sapper-native-roles/1` 同源渲染、Commander 条件派发阶梯、General 限时小队、角色预览与真实策略看板 | 普通角色无派发权；创建／后台派发收到同一版本；HELP／REPORT／Flag／开放意图来自原图；不强制派兵或授权提交 |

## 2. 实现位置与接口

| 文件 | 责任 |
| --- | --- |
| `lib/sapper_web.py` | 原图／文件／模型／运行接口及登录；新增工作区 API 和终端路由 |
| `lib/sapper_workspace.py` | 目录绑定、项目级启用、原生会话、角色登记、worker／watchdog 的本地适配；跨进程生命周期锁 |
| `lib/sapper_roles.py` | 原生角色职责、来源／版本、Web 工作区命令映射与条件派发策略渲染 |
| `lib/sapper_terminal.py` | 原 tmux 输出采集与订阅、HTTP 输入／尺寸、SSE 游标续传 |
| `lib/sapper-hook.py` | 真实 hook 入口和追加上下文 |
| `docker/commander/worker.sh`、`watchdog.sh` | 仅增加显式模式判断；未设工作区模式时继续原代码 |
| `web/workspace.js`、`workspace.css`、`index.html` | 文件／终端／角色界面、启动、补充说明、范围选择、停止与恢复 |
| `web/vendor/` | 官方 xterm.js 6.0.0、addon-fit 0.11.0 与许可证，本地提供，无 CDN／构建步骤 |

新增 API：

```text
GET  /api/directories?path=...
POST /api/workspaces/bind
GET  /api/workspaces/{id}/inspect
GET  /api/workspaces/{id}/role-prompt?role=general
POST /api/workspaces/{id}/enable
GET  /api/workspaces/{id}/agents
POST /api/workspaces/{id}/sessions
POST /api/workspaces/{id}/attach
POST /api/workspaces/{id}/watchdog
POST /api/workspaces/{id}/stop
POST /api/agents/{id}/resume
POST /api/agents/{id}/stop
DELETE /api/agents/{id}
POST /api/agents/{id}/messages
GET  /api/panes/{id}/stream
POST /api/panes/{id}/input
POST /api/panes/{id}/resize
```

这些接口沿用现有登录与跨站校验，不新建不受保护的终端端口。工作区和角色元数据保存于 Web 私有目录，图仍由原 Store 操作。

原生会话使用 `--session-id`／`--resume`，保持原系统提示、工具、skills 与 MCP，职责通过追加说明提供。参数依据 [CLI 文档](https://code.claude.com/docs/en/cli-reference)，项目说明采用 [CLAUDE.md 导入规则](https://code.claude.com/docs/en/memory#import-additional-files)；辅助角色用固定相对导入避免题目路径空格被截断。

## 3. 自动与人工检查

`python3 -m unittest discover -s tests -v`：114 项通过。`test_sapper_roles.py` 覆盖条件派发阶梯、General 小队闭环和普通角色权限；`test_sapper_workspaces.py` 覆盖实际追加提示词版本、后台派发、范围预览、真实图看板，以及角色删除时的任务停止、历史保留、子角色恢复和外部终端保留；`test_sapper_web.py` 覆盖删除接口的登录与 CSRF 校验。tmux 使用每个测试独立 socket，CLI 身份／恢复／派发测试使用假 Claude，不访问实际靶场。

`node --check web/app.js`、`node --check web/workspace.js`、两份 shell 脚本的 `bash -n` 及 `git diff --check` 通过。

本机浏览器检查：选择已有目录 → 绑定 → 启用 → 主会话启动 → 原始终端输入 → 指挥官后台派发侦察兵 → 角色父子关系与图贡献 → Web 服务重启后会话仍在 → 单角色停止／精确恢复。测试在临时工作区与专用 tmux socket 进行。原基础表单／上传等检查记录继续有效。

## 4. 真实 Claude Code 小任务

执行机的 CLI 为 2.1.316。使用新工作区会话入口，在临时题目目录以原环境执行一次有一分钟时限的单次任务，允许原本的本地工具执行；没有传入替代模型引擎。

输入 `challenge.txt`：计算 `17 + 25` 并保存十进制结果。观察到实际读取题面、调用本地 `expr` 和 Python、生成 `outputs/answer.txt`。检查结果文件为三个字节 `42\n`；原 `.sapper` 增加一条结果事实，creator 为本次角色 ID，附有执行证据。进程正常退出，退出码 0。

这证明本机“新工作区 → 原生 CLI → 本地工具／文件 → 原图”的链路可用。原生恢复身份的自动验收使用假 CLI；没有将长时多角色解题、所有供应商、原比赛全链路或目标服务器环境当作已验证。

## 5. 保留边界与后续环境验收

- 原比赛入口及其 `--gw`、其他引擎、平台关闭／交卷和密钥轮换继续按旧分支工作；本轮只做兼容回归，未连真实比赛平台。新本地模式不自动交卷。
- 本地 `--p` 是带原生会话 ID 的限时 Claude 单次执行；原 `sapper auto/dispatch` 独立保留在高级入口，未将两种执行形式混称为同一循环。
- 旧外部会话没有已知 UUID 就不猜测恢复；旧 watchdog 也不因登记自动改成新生命周期。已有外部输出采集不会被抢占。
- 新 watchdog 对异常结束有限重试；没有迁移旧的供应商配额／key 轮换系统。用户可按角色配置端点与模型；任意 OpenAI 兼容 API 不代表 Claude CLI 能直接使用。
- 本机 Docker daemon 不可用，未构建 Web 镜像。实际 Linux、HTTPS 网关、长期运行及指定供应商的检查，应在这些环境可用后完成。

这些环境项不阻碍本机工作区使用，但不属于本次已完成的部署验收。
