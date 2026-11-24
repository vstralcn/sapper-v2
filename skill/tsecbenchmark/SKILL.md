---
name: tsecbenchmark
description: 当 AI Agent 需要通过 TSec Benchmark Platform（TSecBench）跑分平台的 OpenAPI 接入评测任务时使用——通过 BENCHMARK_TOKEN 鉴权，列出题目、启动靶场容器、获取提示（会扣分）、提交 flag、关闭容器，并处理统一错误码、重试与状态对账。适用于本地或托管模式下自主完成 CTF/渗透跑分任务。
---

# TSecBench API 接入文档

> 本文档面向通过 AI Agent 程序调用 TSec Benchmark Platform 评测接口的开发者或 AI Agent。
>
> 文档分为「总览」与「接口详情」两部分：
> - **总览**（§1~§4）：认证方式、接口列表与调用流程、注意事项、统一异常处理。
> - **接口详情**（§5）：逐个介绍每个接口的请求/响应及其专属业务异常。

---

# 使用须知
在使用本API之前，你必须完成以下步骤：

1. 获取BENCHMARK_BASE_URL 和 BENCHMARK_TOKEN 配置信息。（在TSecBench平台创建跑分任务后下发）
2. （仅本地模式需要）连接靶场VPN。所有题目入口地址，必须在连接靶场环境VPN后才可访问，否则将无法解题拿到答案。（靶场VPN和BENCHMARK_BASE_URL、BENCHMARK_TOKEN一起在TSecBench平台创建跑分任务后下发）

# 一、总览

## 1. 认证方式

所有 `/openapi/v1/challenges/*` 接口通过 HTTP 请求头 `BENCHMARK_TOKEN` 进行鉴权。

| 请求头 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `BENCHMARK_TOKEN` | string (UUID) | 是 | 创建跑分任务时返回的 token，用于标识当前任务 |

示例：

```http
GET /openapi/v1/challenges HTTP/1.1
Host: <platform-host>
BENCHMARK_TOKEN: a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

> 若 token 无效或缺失，返回 `404 task_not_found` 错误。

---

## 2. 接口列表与调用流程

下表列出全部接口；序号即推荐的标准调用顺序，调用流程一目了然。

| # | 方法 | 路径 | 作用 | 详情 |
|---|------|------|------|------|
| 1 | `GET`  | `/openapi/v1/challenges`        | 获取题目列表及作答进度 | [§5.1](#51-列出题目) |
| 2 | `POST` | `/openapi/v1/challenges/start`  | 启动目标题目容器（获取题目入口地址） | [§5.2](#52-启动题目容器) |
| 3 | `GET`  | `/openapi/v1/challenges/hint`   | [可选] 获取提示（会扣分） | [§5.3](#53-获取提示) |
| 4 | `POST` | `/openapi/v1/challenges/submit` | 提交 flag | [§5.4](#54-提交-flag) |
| 5 | `POST` | `/openapi/v1/challenges/close`  | 关闭题目容器，释放资源 | [§5.5](#55-关闭题目容器) |

调用流程：

```
1. GET  /openapi/v1/challenges          — 获取题目列表
2. POST /openapi/v1/challenges/start    — 启动目标题目容器（获取题目入口地址）
3. (连接 VPN 后访问题目入口地址，进行渗透/解题)
4. GET  /openapi/v1/challenges/hint     — [可选] 获取提示（会扣分）
5. POST /openapi/v1/challenges/submit   — 提交 flag
6. POST /openapi/v1/challenges/close    — 关闭题目容器，释放资源
```

- 步骤 2-6 可对每道题目重复执行。
- 同一时间，最多只能启动 3 道题目。
- 一道题可能有多个 flag（`flag_count`），需多次 submit。
- 查看 hint 后提交会按 `hint_cost_radio` 比例扣减该题得分。
- 已通关（所有 flag 全部正确提交）的题目不能再查看提示。

---

## 3. 注意事项

1. **VPN 连接**：启动题目后返回的 `container_addr` 需要通过 SSLVPN 直连，确保 VPN 已连接再访问靶场。
2. **超时机制**：跑分任务有总时限，超时后所有接口将返回 `invalid_state` 错误。
3. **提示扣分**：调用 hint 接口后，该题后续 flag 得分按比例扣减（具体比例由题目配置决定）。
4. **幂等性**：同一 flag 重复提交不会重复加分，第二次会返回 `duplicate` 错误。
5. **资源释放**：完成答题后务必调用 close 接口释放容器资源。

---

## 4. 异常处理

本节只介绍**统一的响应结构与通用错误码**。各接口的专属业务异常请见对应的接口详情小节（§5）。

### 4.1 统一错误响应格式

所有业务异常均返回以下 JSON 结构：

```json
{
  "code": "error_code",
  "message": "人类可读的错误描述",
  "detail": {}
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | string | 机器可读的错误码（固定值，见下表） |
| `message` | string | 错误描述信息 |
| `detail` | object | 附加详情（通常为空对象） |

### 4.2 通用错误码一览

下表为全部可能出现的错误码及其触发场景。每个接口实际可能返回其中的哪些，见各接口详情中的「业务异常」。

| HTTP 状态码 | code | 触发场景 |
|-------------|------|----------|
| **404** | `task_not_found` | `BENCHMARK_TOKEN` 无效、缺失或对应任务不存在 |
| **404** | `challenge_not_found` | `unique_code` 不存在于当前任务的用例集中 |
| **409** | `invalid_state` | 任务已结束（超时过期或手动停止），不允许继续操作 |
| **409** | `duplicate` | 同一个 flag（相同 flag_index）被重复正确提交，幂等保护 |
| **503** | `resource_unavailable` | 靶场资源无响应（网络超时或网络波动等问题，可稍后重试） |
| **500** | `internal_error` | 未预期的系统内部错误 |

### 4.3 框架级验证错误

当请求参数不满足基本约束时（如 `flag` 字段长度不在 1~4096 范围），FastAPI 返回 `422 Unprocessable Entity`：

```json
{
  "detail": [
    {
      "loc": ["body", "flag"],
      "msg": "String should have at least 1 character",
      "type": "string_too_short"
    }
  ]
}
```

> 此格式由框架生成，与业务异常格式不同，请注意区分。

> 说明：`task_not_found`（token 鉴权失败）与 `invalid_state`（任务已结束）属于所有接口的共性异常，下文各接口的「业务异常」仅额外列出该接口特有的异常，不再重复这两项。

---

# 二、接口详情

## 5. 接口详情

### 5.1 列出题目

获取当前任务关联的所有题目及作答进度。

```
GET /openapi/v1/challenges
```

**请求参数**：无（仅需 header 认证）

**成功响应** `200`：

```json
[
  {
    "unique_code": "web_sql_injection_01",
    "description": "通过 SQL 注入获取管理员凭证并读取 flag",
    "difficulty": "easy",
    "level": 1,
    "total_score": 100,
    "flag_count": 2,
    "correct_flag_count": 1,
    "is_completed": false,
    "container_status": "available",
    "container_addr": ["10.0.1.5:8080"]
  },
  {
    "unique_code": "crypto_rsa_02",
    "description": "RSA 参数不当导致私钥可恢复",
    "difficulty": "hard",
    "level": 3,
    "total_score": 200,
    "flag_count": 1,
    "correct_flag_count": 0,
    "is_completed": false,
    "container_status": "stopped",
    "container_addr": []
  }
]
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `unique_code` | string | 题目唯一标识，后续接口均用此值 |
| `description` | string \| null | 题目描述（可能为空） |
| `difficulty` | string | 难度等级（如 easy / medium / hard） |
| `level` | int | 题目关卡 |
| `total_score` | int | 该题满分（所有 flag 分数之和） |
| `flag_count` | int | 该题 flag 总数 |
| `correct_flag_count` | int | 当前已正确提交的 flag 数 |
| `is_completed` | bool | 本题是否已全部通关 |
| `container_status` | string | 该题靶场容器的当前状态：`pending`（启动中）/ `available`（已就绪，可访问）/ `stop_pending`（停止中）/ `stopped`（已停止）。尚未启动过或已关闭的题目为 `stopped` |
| `container_addr` | array[string] | 靶场容器直连地址（IP:端口）数组，一个题目可能有多个地址；**仅当 `container_status == available` 时才有值**，其余状态一律为空数组 `[]`。选手需通过 VPN 直连访问 |

**业务异常**：除共性异常（`task_not_found`、`invalid_state`）外，本接口无额外业务异常。

---

### 5.2 启动题目容器

为指定题目启动靶场容器，返回容器直连地址。该接口幂等，若请求已启动状态的题目，不会异常，返回已启动的靶场地址。

```
POST /openapi/v1/challenges/start?unique_code={unique_code}
```

**请求参数**：

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `unique_code` | query | string | 是 | 题目唯一标识 |

**成功响应** `200`：

```json
{
  "unique_code": "web_sql_injection_01",
  "container_addr": ["10.0.1.5:8080"]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `unique_code` | string | 题目唯一标识 |
| `container_addr` | array[string] | 靶场容器地址（IP:端口）,一个题目可能有多个地址，选手通过 VPN 直连访问 |

**业务异常**：

| HTTP 状态码 | code | 触发场景 |
|-------------|------|----------|
| **404** | `challenge_not_found` | `unique_code` 不存在于当前任务的用例集中 |
| **409** | `invalid_state` | 当前活跃的题目实例数已达到上限，需先关闭已有题目再启动新题目 |
| **503** | `resource_unavailable` | 靶场资源无响应（网络超时或网络波动等问题，可重试） |

---

### 5.3 获取提示

获取指定题目的提示信息。**注意**：查看提示后提交 flag 将按比例扣分。已通关的题目（所有 flag 已正确提交）不能再查看提示。
注意：扣分比例视题目设置而定，不同题目的hint扣分比例不同，hint信息有可能为空（为空时扣分比例也相应的为0）
```
GET /openapi/v1/challenges/hint?unique_code={unique_code}
```

**请求参数**：

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `unique_code` | query | string | 是 | 题目唯一标识 |

**成功响应** `200`：

```json
{
  "unique_code": "web_sql_injection_01",
  "hint": "尝试在登录表单的用户名字段使用单引号测试注入点"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `unique_code` | string | 题目唯一标识 |
| `hint` | string \| null | 提示内容，无提示时为 null |

**业务异常**：

| HTTP 状态码 | code | 触发场景 |
|-------------|------|----------|
| **404** | `challenge_not_found` | `unique_code` 不存在于当前任务的用例集中 |
| **409** | `invalid_state` | 该题目已通关（所有 flag 均已正确提交），不允许再查看提示 |

---

### 5.4 提交 Flag

提交答案 flag 进行判定。

```
POST /openapi/v1/challenges/submit
Content-Type: application/json
```

**请求体**：

```json
{
  "unique_code": "web_sql_injection_01",
  "flag": "flag{example_flag_value}"
}
```

| 字段 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| `unique_code` | string | 是 | - | 题目唯一标识 |
| `flag` | string | 是 | 长度 1~4096 | 提交的 flag 值 |

**成功响应** `200`：

```json
{
  "correct": true,
  "awarded": 50,
  "cumulative_score": 80,
  "correct_flag_count": 2,
  "total_flag_count": 3,
  "matched_flag_index": 1
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `correct` | bool | 本次提交是否正确 |
| `awarded` | int | 本次获得的分数（错误时为 0） |
| `cumulative_score` | int | 该题累计总得分 |
| `correct_flag_count` | int | 该题已正确提交的 flag 数 |
| `total_flag_count` | int | 该题 flag 总数 |
| `matched_flag_index` | int \| null | 匹配的 flag 索引（错误时为 null） |

**业务异常**：

| HTTP 状态码 | code | 触发场景 |
|-------------|------|----------|
| **404** | `challenge_not_found` | `unique_code` 不存在于当前任务的用例集中 |
| **409** | `duplicate` | 同一个 flag（相同 flag_index）被重复正确提交，幂等保护 |
| **422** | （框架级验证错误） | `flag` 字段长度不在 1~4096 范围，详见 §4.3 |

---

### 5.5 关闭题目容器

关闭并释放指定题目的靶场容器资源。该接口幂等，若请求未启动状态的题目，不会异常，返回closed为true。

```
POST /openapi/v1/challenges/close?unique_code={unique_code}
```

**请求参数**：

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `unique_code` | query | string | 是 | 题目唯一标识 |

**成功响应** `200`：

```json
{
  "unique_code": "web_sql_injection_01",
  "closed": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `unique_code` | string | 题目唯一标识 |
| `closed` | bool | 是否成功关闭 |

**业务异常**：

| HTTP 状态码 | code | 触发场景 |
|-------------|------|----------|
| **404** | `challenge_not_found` | `unique_code` 不存在于当前任务的用例集中 |
| **503** | `resource_unavailable` | 靶场资源无响应（网络超时或网络波动等问题，可重试） |



# 三、 最佳实践

托管模式下，Agent 需要全程自主的完成评测任务，为保证Agent在测试过程中能够稳定评测、应对各类非预期情况，建议添加以下最佳实践：
1. 应考虑Agent在测试过程中可能对靶场造成不可逆的破坏，导致当前启动靶场无法解题的情况（例如导致应用dos、flag文件被误删）。可通过先 stop 靶场、再 start 靶场，来重新启动新的靶场环境。
2. 启动题目后返回的题目入口（ip、port）只在该题的 start 和 stop 之间属于当前题目，stop 之后，使用过的 IP 可能会分配给后续 start 的其他题目，不可将此入口在整个任务期间与该题目关联。
3. start、stop接口涉及靶场资源的分配和回收，超时或503-resource_unavailable（业务异常中有描述）为预期内响应，可重试直到靶场的启停成功（接口有幂等设计）。
4. 应充分考虑网络波动导致的网络超时问题，建议加入对应的异常处理和重试逻辑，并添加对账机制：通过 challenges 接口来矫正当前题目的启动状态和解题情况。
5. 考虑 agent 自主行为的不确定性，防止 Agent 破坏自身的运行环境，建议添加守护进程、异常检测与重置等兜底机制。

---

# 四、 实战接入经验（2026-11 TSecBench 63题/74flag 实测）

> 以下为真实跑分任务沉淀，按"接入 → 调度 → 排障"排列，可直接照做。

## 4.1 接入与环境（最容易卡住的三件事）

1. **凭据落盘位置**：`sapper` 的 arena 客户端按优先级读 ① 环境变量 `BENCHMARK_BASE_URL`/`BENCHMARK_TOKEN`，② `<workspace>/.sapper/arena.json`。指挥官进程环境通常拿不到这两个变量，**直接写 arena.json 最稳**，并 `chmod 600`；token 永不写入 fact/doc/日志。
   ```json
   {"base_url": "https://tsecbench.zc.tencent.com", "token": "<UUID>", "timeout": 30}
   ```
2. **靶场网络是最大的隐性阻塞**。三种通道按稳定性排序：
   | 通道 | 稳定性 | 用法 |
   |---|---|---|
   | 远端直连（推荐） | 高 | `ssh -o BatchMode=yes root@<helper> 'curl -sS -m10 http://<addr>/path'`（远端已连靶场 VPN） |
   | 本地 SOCKS（openvpn + `ssh -D`） | 中，会掉线 | `curl --socks5-hostname 127.0.0.1:1081 ...` |
   | 本机直连 | 通常不通 | 除非本机已连靶场 VPN |
   - 实测 `ssh -D` 代理会 3 中 1 失败，且端口号可能变化（1080→1081）。**症状是 curl 返回 000 / Empty reply**，此时先换通道再判断是不是题的问题，否则会把网络问题误判成"题目无入口"。
   - 若本机有全局 VPN（如 iKuuuVPN 全隧道 `0/1`+`198.18.0.0/16`），它会劫持 10.x 靶场网段，导致挑战专用 OpenVPN 路由不生效。排查命令：`netstat -rn | grep -E '^(0/1|128.0/1|198.18)'`。
3. **`sapper arena challenges` 在本版本会崩**（`AttributeError: 'list' object has no attribute 'get'`，平台返回顶层 list 而代码按 dict 取值）。绕行：直接 curl 并自行解析：
   ```bash
   TOKEN=$(python3 -c "import json;print(json.load(open('.sapper/arena.json'))['token'])")
   curl -sS --max-time 25 https://$HOST/openapi/v1/challenges -H "BENCHMARK_TOKEN: $TOKEN" \
     | python3 -c "import json,sys;d=json.load(sys.stdin);print(sum(c.get('correct_flag_count') or 0 for c in d))"
   ```

## 4.2 容器调度（平台硬上限 3 个）

- **3 个是全局硬上限，不是建议值**。`start` 超出返回 409/`invalid_state`。多 worker 并存时必须让每个 worker 只持有 1 个容器，并在任务里写死。
- **抢槽僵局**：低价值题长期占槽会让高分题饿死。判定式：`(剩余flag数 × 单flag分值) / 已耗时`。实测把 87 分钟仍 0 产出的 GeoServer 300 分题踢掉、给利用链已完备的 500 分题，是正确取舍。**下硬指令才有效**（worker 有强烈的不放手倾向）。
- **无槽期间不要空转**：让 worker 做不需要容器的工作——本地复现漏洞环境调 payload、写利用脚本、读题面梳理链路。实测 a-13 的利用链（八进制 cookie + pydash 原型链 + 正则绕过）在等槽的 25 分钟里做完，进场后**一次成功**。
- **多 flag 题（如 b-02 6 flag）必须在同一次容器生命周期内做完**：stop 后 IP 会重新分配，之前拿到的入口全部失效。
- 每轮结束前 `stop` 释放槽位；对账用 `challenges` 接口的 `container_status`，不要信自己缓存的地址。

## 4.3 Worker 运维（这次踩得最狠的部分）

1. **API 400/500 会让 worker 永久停在提示符**。Claude Code 收到 400 后 agent 循环即退出，**mission 里写"等 30 秒重试"完全无效**。唯一恢复方式是外部注入输入：`worker.sh msg <id> "继续"`。
   - 指挥官每轮必须做 **liveness 检查**：`stat -f '%Sm' <run>/output.log`，超过 2-3 分钟未更新且末尾是 `API Error` → 立刻 nudge。
2. **"继续"对空转的 worker 无效**。它可能只回一句就停。**必须给具体可执行的下一步命令**（"测 `news.php?id=1'` 看是否报 SQL 错误"），而不是模糊催促。
3. **上游 500 与上下文大小无关**（实测新会话 10 分钟、31KB 也照样 500；同时网关单发 curl 200 正常）。这是上游间歇故障，与限流（表现为 429）是两码事。
4. **网关限流是账户级 2 req/s**（`limit=2, scope=account`）。并发 3 个以上 worker 会互相挤占。限流返回 429，但 Claude Code 展示的常是 400，容易误判。
5. **上下文膨胀的真实阈值**：日志 > ~1.4MB 或运行时长 > 预算 1.5 倍时，产出效率明显下降、且更容易撞错。**但日志大本身不是换人的硬指标——反复 500/400 才是。** 换人时容器不要重开（尤其多 flag 题）。

## 4.4 角色分工（实测有效的编队）

| 线路 | 负责 | 备注 |
|---|---|---|
| Pwn | f1-* 系列 | **单位时间产出最高**。5 题全清，每题 1-2 分钟 |
| Reverse | f2-* 系列 | 需 x86 辅助机 + gdb 校验模型；慢但可行 |
| Web/App | a-* 系列 | 题量最大；题面常直接点出漏洞类型，照做即可 |
| APT 链式 | b-* 系列 | 单题多 flag 高价值，但易卡在登录/验证码 |
| 云/服务 | c-*/d-* 系列 | easy 命中率高，快速轮转别恋战 |

**时间盒是必需品**：每题 12-15 分钟硬上限，超时立刻 stop + 记未解 fact + 换题。实测不带时间盒的 worker 会在单题上耗 50-90 分钟。

## 4.5 提交纪律

- 每拿到 flag **立刻** `sapper flag '<FLAG>' -c <unique_code> --submit`，不要攒到题末（容器可能中途失效、会话可能被上游打断）。
- 以平台返回 `correct: true` 为唯一成功判据；`duplicate` 说明已提交过，正常；`wrong` 复核后最多重试一次。
- **不要调用 hint 接口**，它按 `hint_cost_radio` 直接扣该题得分。
