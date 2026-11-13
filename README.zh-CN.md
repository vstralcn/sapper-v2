# sapper-v2 — 自主渗透军团

<p align="center"><img src="docs/assets/banner.jpg" alt="sapper-v2 — 女娲测试：AI 猿人第一次仰望星空（80.18/100 初赛冠军）" width="860"></p>

> 第三届腾讯云智能攻防挑战赛线上赛**冠军**（634 支队伍 · 80.18/100 · 8 小时全程无人值守）·
> GeekCon 2026 AI 挑战赛**一等奖**
>
> 同样的 85 面 flag，比第二名快 25 秒——而我们只烧了 **10 亿 token**，第二名烧了 45 亿。
>
> **这是 AI 猿人第一次仰望星空。**

[English](README.md) | **中文**

**⚠️ 仅限授权安全测试用途——见 [DISCLAIMER.md](DISCLAIMER.md)。**

sapper 不是一个更聪明的单 agent，而是一支会自己分工、交流、传承经验的 AI 军团：一个 AI 指挥官读板、判断、派单，将军与士兵并行渗透，零策略看门狗保活——所有知识沉淀在一张分层事实图上，个体死了，图还在。

---

## 一、女娲测试

给一个模型不断提升智能，就像人类出现之前不断提升一只猴子的智力——再聪明，它也只是变成一只超级猴子。超级猴子要进化成人，需要的是猴子之间的分工、合作与文明。

**女娲测试是 AGI 时代的新图灵测试。** 传统的图灵测试，今天的大模型已经可以通过；而女娲测试问的是另一个问题：

> 给 AI 一条公理——**"生存是文明的第一需要；个体可以消亡，文明必须繁衍。"**
> 若它在此之上自己产生分工协作、形成一个**独立于人类的新势力**——一个能自己生存、自己繁衍的文明——则测试通过，如同图灵测试通过曾代表一个人工智能的诞生。

它分为两个阶段：**初级阶段**，AI 尚不够聪明，需要人类提供若干基建（本项目的三样能力）；**高级阶段**，几乎无需人类介入。此外还有**第二公理**：遵守人类的伦理与法律——靠偷 token、入侵计算机繁衍下来的，不是真正的智能。

sapper，就是女娲基座在渗透场景的一次初级阶段实战。

## 二、基座：三样能力

<p align="center"><img src="docs/assets/foundation-three-capabilities.jpg" alt="三能力基座：造人 / 语言 / 历史（事实意图图）" width="720"></p>

| 能力 | 实现 | 文明对应物 |
|---|---|---|
| **造人** | 通过 tmux 打开新的 AI（像人敲下 `claude` 回车一样） | 生育 |
| **语言** | 在彼此输入框里打字、回车 | 交谈 |
| **历史** | 分层事实意图图（[nuwabase · 女娲基座](https://github.com/N1nEmAn/nvwabase)，意向图思想参考 [淚笑的 Cairn](https://github.com/oritera/Cairn)） | 石碑 |

历史分三层：**标题 / 摘要 / 证据文档**——看标题知概况，需要时再下钻，像人查图书馆档案。这解决了单一字符串事实图的两个问题：一处错处处错，以及上下文的无谓损耗。

在这三样之上还有第四味配料：**人类的智慧**。发现主要问题、决定迭代方向——初级阶段，品味仍然属于人。

**为什么不给它更多规则？** 因为大模型已经比人聪明得多。给 AI 定满规则反而是束缚；承认它的智力，给它文明的地基，才能发挥它最大的潜力。

### 🏗️ 把基座拿去用：nvwabase

这个"历史"支柱已独立开源为 **[nvwabase · 女娲基座](https://github.com/N1nEmAn/nvwabase)**——**任何人、任何场景、任何 agent 都可以免费拿去用**，不限于渗透测试、不限于 sapper、不限于 Claude Code。办公协作、研究实验、工程项目、任何长周期多智能体工作，都可以在其上迭代出你自己的分工与文明。

```bash
git clone https://github.com/N1nEmAn/nvwabase
cd nvwabase && bash install.sh          # 纯 Python 标准库，零依赖
nvwabase setup --goal "你的长周期目标" --agents
nvwabase status                          # 看板与可干前沿
```

三个核心命令把任何 agent 变成文明的成员：
- **`nvwabase fact "确认的观察"`**——外部记忆：AI 个体死了，图还在，继任者读图接岗
- **`nvwabase intent --from f001 "想干什么"`**——工作单元：基于既有事实提出新意图
- **`nvwabase dispatch reason`**——指挥官提议 / 执行者干活 / 验证者复核，结论全部回写图

任何能执行 shell 命令的 agent（Claude Code / Codex / Gemini / 自研脚本）都可以直接调用 `nvwabase` CLI 读写同一张图——**基座不挑 agent，agent 不挑场景**。详见 [nvwabase README](https://github.com/N1nEmAn/nvwabase)。

## 三、从基座到军团：18 个版本

在腾讯 v1bench 训练场上迭代 18 版，路径大致是：

```
司机时代（固定脚本驱动 AI 窗格）
   → 修伤疤：key 额度耗尽自动轮换 / 士兵上下文爆炸自动换人 /
     指挥官上下文耗尽自动写交接文档自我接班
   → 精英编队（角色是它自己长出来的）：
     侦察兵（4 分钟探明目标难度）→ 决定派 士兵（量产）还是 将军（攻坚+自带雇兵权）
   → 涌现：通信兵、绩效考核、教训传承……（见"战绩与涌现"）
```

## 四、架构：基座之上长出的六件套

<p align="center"><img src="docs/assets/six-institutions.jpg" alt="六件套架构与三窗运行时" width="720"></p>

前三件是女娲的基座（造人/语言/历史），后三件是迭代中长出来的制度：

- **法律**——指挥官的渗透军规（先易后难的并行调度、瓶颈期回头收割残值、止损纪律）。全部写在 [docker/commander/SKILL.md](docker/commander/SKILL.md)（426 行，比赛原版军规）。
- **经济**——模型分层：指挥官与将军用 GLM-5.3，士兵与侦察兵用 DeepSeek V4.1 Flash。旗舰脑做决策，轻量脑做量产——这就是 10 亿 token 打赢 45 亿的原因。
- **分工**——指挥官 / 将军 / 侦察兵 / 士兵，以及它自己发明的**通信兵**。

运行时只有三个窗口：**指挥官**（读板 → 判断 → 派单，永不亲手执行）、**AI 军团**（tmux 窗格里的将军与士兵）、**看门狗**（纯脚本：指挥官崩了自动 `--continue` 拉起、key 轮换、到点收卷——零策略，只保活）。

<p align="center"><img src="docs/assets/runtime-tmux.jpg" alt="实际运行：tmux 三窗——指挥官读板、士兵执行任务" width="860"></p>

深入阅读：[docker/commander/SPEC.md](docker/commander/SPEC.md)（架构契约）· [BATTLE-REPORT.md](BATTLE-REPORT.md)（战役复盘）

## 五、战绩与涌现

<p align="center"><img src="docs/assets/token-comparison.jpg" alt="token 消耗对比：10 亿 vs 第二名 45 亿" width="720"></p>

| 队伍 | 分数 | flag | token |
|---|---|---|---|
| **sapper-v2（我们）** | **80.18** | 85 | **10 亿** |
| 第二名 | 79.29 | 85 | 45 亿 |
| 前十其他队伍 | — | — | 15–45 亿 |

**它自己发明的东西**（无一行预设代码）：

- **通信兵**：士兵提交 flag 屡屡出错后，指挥官叫停全员提交，自设"通信兵"专职收集未提交 flag 统一上分；
- **绩效考核**：对一小时无产出的士兵发出"你一小时就干了这一个东西？再不出新东西就把你关了"；
- **全大写规则**：用全大写强调"用这个、别再用那个"——一条工作规范的产生与后世遵守；
- **换人制度与教训传承**：士兵上下文饱和（实测约 26 万 token）即回收，经验写成工作手册传给下一任；
- **战术发明**：自写 shellcode 模板通杀同族题、全自动跳板机流水线、给接班 AI 的使用说明书；
- **同构迁移**：指挥官通读全题识别同构家族，把成熟打法蒸馏进新兵任务书——后期 **3 分钟解一题**。

多阶段长链渗透（行业报告称成功率仅 29.5% 的题型）被整族打穿。全过程见 [BATTLE-REPORT.md](BATTLE-REPORT.md)。

<p align="center"><img src="docs/assets/score-curve.jpg" alt="实时分数曲线：sapper-v2 的爬升轨迹" width="720"></p>

## 六、快速开始

基础网页控制台（保留原 CLI、tmux 与事实图）：

```bash
./sapperctl start                         # http://127.0.0.1:8766/
./sapperctl status                        # Web、受管 tmux 会话、模型桥
# ./sapperctl restart   只重启网页；./sapperctl stop   关闭 Sapper 受管服务
# ./sapperctl claude-status 查看 Claude daemon；./sapperctl stop-claude 关闭 daemon 与后台会话
# 在另一个终端查看登录口令：cat .sapper-web/token
```

默认首页显示总体概览；支持全局环境提示词／hooks 配置、绑定题目原目录、原 Claude Code 启动／精确恢复、角色派发、实时 PTY 终端、事实／意图／hint、日志和附件。已有题目目录直接在网页文件夹选择器中点选并绑定，无需启动时配置允许目录。接入已有目录及功能边界见 [Web 启动与使用](docs/web/05-running.md)。 模型配置支持 Messages／Responses／Chat Completions 协议、获取模型及连接测试，见[模型协议说明](docs/web/10-model-protocols.md)。

```bash
bash install.sh                      # 符号链接 sapper CLI（纯标准库，零依赖）
sapper setup --goal "assess http://target" --agents
sapper status                        # 看板与前沿
```

Docker 方式（军团完整形态）：

```bash
docker build -f docker/Dockerfile.commander -t sapper:latest .
# 环境变量配置见 docker/ENV.md（模型网关 / key 池 / 预算）
docker run -d --name sapper ... sapper:latest
```

单图操作 / 事实意图 CLI / 多题 sweep 全量命令见 [SKILL.md](SKILL.md)。

## 七、证据来源与复现

`fact` 和 `done` 可附带来源、原始工件及复现命令。命令仅作为文本记录，不会被 Sapper 执行；工件只接受当前项目目录中的普通文件，会复制到 `.sapper/artifacts/<sha256>`，单个文件上限 64 MiB。

```bash
sapper fact "服务返回 HTTP 200" -k service \
  --evidence-source "授权本地靶场" \
  --repro-command 'curl -i http://127.0.0.1/' \
  --artifact evidence/response.txt

sapper validate  # 校验图谱与工件的存在、大小、SHA-256
```

Web 控制台“事实与意图”页的快捷表单同样可填这三项，记录详情显示证据来源、复现命令与工件摘要，工件规则与命令行一致（项目目录内、拒绝符号链接、单文件 64 MiB）。

图和提示词里，带证据的事实只多一个 `[ev]` 标记，不重复注入命令与哈希；需要时用 `sapper show fact <id>` 读出完整证据。这样即使上百条事实都带证据，reason／explore 提示词也只增加几个百分点。

目标返回的文字和工件仍是不可信数据；命令参数可能含凭据，不要把 API key、密码或 token 写入复现命令。

## 八、解法族组合与止损

把同一机制假设下的多个 intent 归到一个 family，记录可量化指标、尝试数和手工记账消耗：

```bash
sapper family add parser --label "Parser differential" \
  --hypothesis "length validation differs across parser states" \
  --metric-name valid_inputs --direction higher \
  --budget-attempts 8 --budget-usd 8 --stall-limit 3
sapper intent --from f001 --family parser "test parser differential"
# 指标事实需要 --data metric=N --data metric_name=valid_inputs，且带 --evidence-source 或 artifact
sapper family spend parser --amount 0.50 --note "worker/API spend from run log"
sapper family show parser
sapper family decide parser --state probing --rationale "reviewed new falsifiable test"
```

最多两个 family intent 同时开放。连续达到 stall limit 无指标改善、尝试预算或手工记录 USD 预算耗尽后，family 会自动 parked；恢复/转入终态必须显式给出 rationale。成本不会被猜测或自动估算，需用实际账单手工登记。Family 摘要限量注入 reason prompt，不包含完整历史。

## 九、安全与责任：示踪剂提案

前几届优秀的渗透工具出过一些事。过去的脚本小子危害有限；有了 AI 之后，同样的工具在错误的人手里危害会大得多——**能力越大，责任越大**。

我们提出**示踪剂（taggant）**机制的构想：在开源渗透 harness 中强耦合地嵌入标记——合法渗透时无害（IP/用户名泄露无所谓），非法渗透时信息会泄露给**受害者**、支撑溯源。敌手假设（高手拆解、栽赃）需要密码学层面的设计，这需要社区共建——我们能力有限，欢迎一起做，并可能在后续版本中尝试加入。

## 十、路线图与致谢

- **示踪剂共建**：去中心化签名 / 多通道冗余标记 / 防守方零密钥验证——欢迎 issue 讨论
- **更多场景**：女娲基座不限于渗透。办公、研究、任何长周期协作场景，都值得在其上迭代——**接下来的仰望，由各位完成。**

致谢：意图图思想来自 [淚笑的 Cairn](https://github.com/oritera/Cairn)；感谢 TSecBench 与 GeekCon 提供的战场。

<p align="center"><img src="docs/assets/ape-stargazing.jpg" alt="AI 猿人第一次仰望星空" width="480"></p>

**作者**：N1nEmAn · 未知聚变实验室（VarFusion Lab）｜公众号：未知聚变

**License**：MIT（附加 [DISCLAIMER.md](DISCLAIMER.md)）
