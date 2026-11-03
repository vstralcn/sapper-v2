# Web 工作区：启动与使用

版本：v0.9 · 2026-11-03 · 原 sapper-v2 的操作与观察层。

## 1. 本机启动

需要 Python 3.10+、tmux、已配置可用的原 Claude Code CLI。无需 pip、npm 或前端构建；xterm.js 及许可证随仓库提供。原工具、skills、MCP 和模型登录继续使用原环境。

在仓库根目录运行：

```bash
python3 lib/sapper.py web
```

题目目录在网页文件夹选择器中点选后直接绑定，可选择执行机上当前服务用户有权限访问的已有目录，无需预先配置允许范围，也不要求复制到 `runs/`。文件夹选择器提供任务根目录、用户主目录、文件系统根目录和已绑定目录入口，支持点击面包屑或“上一级”导航；打开目标文件夹后点击“选择此文件夹”，无需填写路径。默认 `--root` 为仓库 `runs/`，用于新建任务和自动发现；旧参数 `--workspace-root` 继续兼容，作为可选目录快捷入口。Web 私有配置目录及其内部目录仍不能绑定或浏览。

打开 <http://127.0.0.1:8766/>，在另一个终端读取口令并登录：

```bash
cat .sapper-web/token
```

登录、刷新或点击“总体概览”后先显示所有工作区与运行情况；点击具体工作区后进入文件／终端／角色界面。侧栏“全局上下文”提供全角色共享环境配置。

侧栏／工作区“原生功能”可管理 31 个非平台原生命令：选择工作区、命令和模型配置，填写原参数，预览实际命令再执行。原自主循环使用 `auto`，多任务指挥官／轮转使用 `command/sweep`，原编队使用 `fleet`；配置和提示词模板可在同一窗口编辑。日志进入运行记录。原生 CLI 循环显示“原 CLI 流程”，独立编队用 `fleet down/fire` 停止。知识库可不选工作区直接新增、导入和查看。完整覆盖与边界见[原生功能说明](./09-native-capability-coverage.md)。

已安装原 CLI 时等价入口为 `sapper web`；也可运行 `python3 lib/sapper_web.py`。使用其他 Claude 可执行文件时增加 `--claude-bin /absolute/path/to/claude`，服务会检查其会话参数，不自动升级它。

## 2. 打开已有题目

1. 点击“打开题目目录”，在文件夹列表中逐级打开目标文件夹，点击“选择此文件夹”。此步骤只保存 Web 关联，不修改题目。
2. 点击“启用 sapper”。复用原 `sapper setup --agents` 创建／保留 `.sapper`，合并项目级 hooks，并用受管导入连接原 sapper 说明。
3. 点击“打开 Claude Code”，选择角色、模型、任务和目录，然后启动。
4. 左侧看题目文件，中间操作真实终端，右侧管理角色；“事实与意图”“运行记录”“文件与附件”保留原基础操作。

主执行者默认 cwd 就是题目目录。辅助角色默认在 `题目/.sapper-agents/<agent-id>/`，`.sapper` 指向题目原图，使用原 CLI 的 `--add-dir` 访问题目资料。启动表单也允许选择共用题目根目录。目录分开用于协作与恢复，不是文件访问沙箱；同目录并行需要任务分工。

启用过程保留已有 `AGENTS.md`、`CLAUDE.md` 正文、MCP 和其他 hooks，不修改全局设置。生成 `.claude/sapper-workspace.md`，在 `CLAUDE.md` 添加带标记的导入；重复执行不重复追加。损坏设置、不完整受管块和逃逸符号链接会先报错。原题面、代码和附件保持原位置。

原任务仍可通过 `--root /path/to/existing/runs` 自动发现（根目录及四层以内有 `.sapper/project.json` 的目录）。外部已有 tmux 会话可登记角色和已知 UUID；不会重新启动或猜测“最近会话”，未知 UUID 的旧会话不可一键精确恢复。

本机路径指运行服务的机器。远程浏览器的本地文件路径不能作为服务器目录使用；需要上传资料或先将目录放在执行机。Web、CLI 与 tmux 必须能访问同一文件系统和 socket；需要独立 socket 时指定 `--tmux-socket NAME`。

## 3. 角色、派发与共享图

| 角色／组件 | 行为 |
| --- | --- |
| 执行者 | 默认在题目根目录，亲自使用原 Claude Code 工具解决任务 |
| 指挥官 | 读板、派发、协调、维护交接；选择明确的可派发工作区范围 |
| 将军 | 亲自攻坚，可继续派发子任务 |
| 侦察兵 | 执行明确的侦察任务，可用限时单次模式 |
| 验证者 | 核验已有结论与产物；需要时显式创建，不虚构常驻进程 |
| 自定义职责 | 按用户给定的职责与任务运行 |
| Watchdog | 独立脚本服务，保活异常退出的登记会话，不消耗模型 |

角色卡片的“停止”保留角色供后续恢复；“删除角色”会先停止其受管任务，再从列表和看板移除，watchdog 不再恢复它。文件、运行日志、原生会话历史和共享图记录保留；已有子角色独立保留并可继续运行、恢复。关联多个工作区的角色删除后会在所有关联工作区消失。对于显式接入的外部终端，删除只移除角色登记，终端继续运行并可从终端选择框访问。

网页记录角色来源、父角色、任务、模型、cwd、原生 Claude UUID、run ID 和实际 pane。后台通过原 `worker.sh` 派出的角色同样登记；图贡献按原事实／意图的 creator 展示，详情仍读原图。没有可靠模型事件时只显示“进程存活”，不猜测“思考中／卡死”。

新建角色使用 `sapper-native-roles/1` 基础职责。Commander 按原版条件执行“4 分钟 scout → 普通 worker／General → 不同角度升级 → 回收”；General 亲自攻坚，并在存在独立侦察、验证或 fuzz 工作时可派最多两个约 5 分钟小队。它不是固定开局人数：已有证据或无独立工作时可以不派发。创建窗口可展开查看实际追加提示词；角色卡显示版本和摘要。正在运行的旧角色不会被热修改，显式恢复后加载当前版本。

本地工作区自动设置 `SAPPER_MODE=workspace`。角色使用原入口：

```bash
bash /path/to/sapper-v2/docker/commander/worker.sh board
bash /path/to/sapper-v2/docker/commander/worker.sh spawn <工作区名或ID> --role scout --p --minutes 3 --mission '检查附件并记录证据'
bash /path/to/sapper-v2/docker/commander/worker.sh spawn <工作区名或ID> --general --tui --profile <配置ID> --mission '攻坚并按需派发子任务'
bash /path/to/sapper-v2/docker/commander/worker.sh msg <agent-id> '新的补充信息'
```

这些命令在 Web 启动的角色环境中运行，继承必要的工作区元数据。不要把上述本地示例当作原比赛模式语法的新承诺：默认原分支未变。

本地分支支持 `spawn/list/board/hint/msg/peek/kill`、角色、父身份、`--general`、`--tui`、`--p`、分钟数、任务、名称、模型与配置。`list` 返回登记角色；`board --json` 还返回图摘要、开放意图、Flag 状态计数、HELP／REPORT、最近图活动及父子角色，不返回 Flag 值，也不把安静状态推断成卡死。它不要求比赛平台，不启动／关闭靶场，不自动交卷。`--p` 在此新分支明确表示一次限时的原生 `claude --print`，有会话 ID；原比赛 `--p` 的 `sapper auto` 循环保持原样，可从网页“原生功能”选择 `auto/dispatch`，通过原参数运行。

本地新分支当前面向 Claude Code；原 `--gw` 供应商选择、其他引擎和轮换规则仍在旧比赛入口中。它没有替代原比赛 commander 的全部平台调度策略。工作区指挥官沿用读板、派发与交接分工，按本地任务提供相应说明。

## 4. 终端、日志与恢复

终端使用 xterm.js，PTY 仍由原 tmux 持有。输出走带登录校验的 SSE，输入／尺寸调整走带 CSRF 的 HTTP；无需 WebSocket。默认只读，点击“接管输入”后可直接打字、发送方向键、Ctrl-C、粘贴和响应原权限提示。输入按顺序传递，不在管理审计中记录键入内容。

支持 ANSI、UTF-8、原始控制键、单 pane 窗口尺寸调整和带游标的断线续传。已有外部 `pipe-pane` 订阅不会被替换，无法共享时明确回退到快照；多 pane 窗口保留原布局，由原 tmux 调整。接入前的完整日志不能补造。

新会话使用 `--session-id UUID`，恢复用 `--resume UUID`，不用当前目录的最近会话。网页重启后保留角色／图／运行元数据，原生历史由原 CLI 保存；两者都需保留。模型或网关地址变化时不静默切换旧会话，需还原配置或创建新角色。

关闭浏览器或 Web 服务不停止角色。重启使用相同 `--root`、`--state` 与 tmux socket，已绑定的外部目录从 Web 配置恢复，重新登录即可查看原会话。“停止”登记明确停止意愿再结束对应进程，工作区 watchdog 不会复活它；正常结束的单次任务也不自动重跑。Watchdog 对异常结束按原 UUID 有限重试；受管交互式指挥官另有[续轮及真实读板确认](./14-commander-liveness.md)，可在角色卡片暂停，不提供新的供应商轮换系统。

“停止本工作区”先停止该题 watchdog，再停止该题登记角色；跨题目指挥官仅移除该题范围，保留其他题目，最后一个范围停止时结束指挥官。外部未登记的旧 pane 不批量结束；登记的旧 pane 仅发送原中断键，旧 watchdog 仍按原规则运行。Ctrl-C 与完整结束进程的语义不同。

## 5. 模型与数据边界

可保存多组模型配置，按角色选择 Messages／Responses／Chat Completions 协议、获取并筛选模型、手动填写模型 ID、测试文本生成。Messages 使用原网关变量，另两种通过本机 HTTP 协议适配连接原 Claude Code；没有新增 API 解题循环。未选配置时继承原环境。配置页提供后台适配服务状态和停止操作，细节见[模型协议说明](./10-model-protocols.md)。

原 `SAPPER_BIN` 包装器仍可用于 Messages 配置的高级 `auto/dispatch`，需符合原参数与 JSON 输出契约；新交互入口不把任意 `-p` 包装器当作支持交互和恢复的 Claude CLI。新协议已验证本地 HTTP 契约及真实 Claude 工具执行链路，任意远端供应商仍需自行测试。

Web 私有数据默认在 `.sapper-web/`，目录 700，凭据／启动环境／日志文件 600；配置密钥未额外静态加密。普通配置响应不返回 API Key。图仍是题目 `.sapper/` 的原 JSON，没有双写数据库。

日志在 `.sapper-web/runs/<run-id>/output.log`。文字日志接口遮蔽当前已知配置密钥；原始 PTY 流和原始捕获文件保留原终端内容，可能包含命令主动输出的敏感信息。文件下载保留原内容。普通文件接口排除管理私有目录及常见凭据路径。

## 6. Linux／域名网关

这是待目标环境验收的启动示例，不是已部署声明：

```bash
python3 lib/sapper.py web \
  --root /work/runs \
  --state /work/web-state --host 127.0.0.1 --port 8766 \
  --public-url https://sapper.example.com
```

由现有代理提供 HTTPS；若路径为 `/sapper/`，加 `--url-prefix /sapper`，代理保留前缀、Host 与 Origin。SSE 需关闭响应缓冲，并保留足够的长连接超时，例如 Nginx 对此路径设置 `proxy_buffering off; proxy_read_timeout 3600s;`。HTTP 心跳约每 10 秒发送一次。HTTPS URL 启用 Secure cookie，应通过配置域名访问。

已有 `sapper-agent:commander` 时可构建可选 Web 镜像：

```bash
docker build -f docker/Dockerfile.web -t sapper-agent:web .
docker run --rm -p 127.0.0.1:8766:8766 -v sapper-work:/work sapper-agent:web \
  --host 0.0.0.0 --root /work/runs --state /work/web-state
```

镜像复用原工具环境，并复制本次 worker／watchdog 适配入口；不自动启动原比赛指挥官。容器 token 为 `/work/web-state/token`。其他容器内已有的 tmux 不会因共挂文件卷而共享，需在实际执行容器中运行 Web。本机 Docker daemon 不可用，镜像尚未构建验证；Linux／HTTPS／跨供应商仍需在实际环境检查。

## 7. 验证

本机 109 项测试通过：原图与 CLI、基础 Web、真实 hook 子进程、绑定与设置保留、原生会话身份、后台派发、原生条件派发提示词、真实策略看板、共享图、跨工作区停止、终端输入／尺寸及 SSE 重连。浏览器已核对 General 的职责摘要、来源版本与实际追加提示词。

真实 Claude Code 2.1.316 已在临时题目目录完成 `17 + 25`：读取 `challenge.txt`、执行本地工具、写出只含 `42\n` 的结果文件，并通过原 sapper CLI 记录带角色 creator 的事实。本次小任务只证明接入链路，不代表长时解题效果或全部供应商兼容性。详细记录见[实现与验收](./07-workspace-implementation.md)。

```bash
python3 -m unittest discover -s tests -v
node --check web/app.js
node --check web/workspace.js
bash -n docker/commander/worker.sh docker/commander/watchdog.sh
```

## 8. 全局环境信息

点击侧栏“全局上下文”，填写服务器、本机架构／工具、SSH 别名和使用约定，启用并选择“启动提示词”“会话 hooks”后保存。共享配置作用于本 Web 管理的原 Claude 角色，包括后台派发和恢复，不修改用户全局配置。

启动提示词在新建／派发／恢复时加载；hooks 在开始会话／后续消息时检查最新版；该版本已在启动提示词中时不重复输出。仅启用提示词时，修改需新建或恢复会话才能加载。角色面板展示启动版本、最近 hook 检查和当前版本，未收到新版时显示待更新。

升级前已运行且未继承共享配置路径的旧会话需恢复或重新启动接入。停用停止后续注入，已有历史不会被删除。这里配置的是文本，不作为 shell 代码执行，也不自动验证 SSH 可达性。完整说明与新增验收见[全局上下文与总体概览](./08-global-context-and-overview.md)。
