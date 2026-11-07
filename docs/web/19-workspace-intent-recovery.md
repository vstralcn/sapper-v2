# 工作区停止、释放和转派（阶段 C）

阶段 C 不引入工作区级权限设置。新建和恢复仍按原权限解析；需要显式开启时，现有认证 API 的 resume 请求可传 skip_permissions=true，Web 角色卡恢复按钮和 worker.sh resume 仍未提供权限覆盖。

## 停止和退出确认

角色停止先写 desired_state=stopped，禁止工作区 watchdog 恢复，然后停止受管运行。运行列表的 stop 和内部 _stop 也走同一角色停止逻辑。历史 run 的停止不会停止该角色后来的新 run。

退出确认观察 wrapper、模型进程、进程组及停止前可见的工具子进程，包含已创建独立进程组的工具。运行记录保存进程出生标识，停止期间重查以避免误伤复用 PID。旧记录按启动时间验证进程标识。确认进程退出后才在图锁内释放该角色持有的所有未完成 intent，不只依赖 agent.intent_id。已完成 intent 和其他角色的认领保持原样。重复 stop/delete 幂等。

无法确认退出时返回 stop_pending、消息“等待确认停止”，保留认领。旧进程组只剩无法确认归属的成员时，不按数字 PGID 发送信号，也不释放任务，需核实或等待这些进程退出后重试。外部登记的终端只发送一次 Ctrl-C，不等同确认退出；在原 pane 仍存活期间，停止、释放、转派和持有任务角色的删除都会保留认领。外部 pane 退出后重试停止即可确认。没有任务的外部登记仍可按原行为直接注销。

这些是受管进程的生命周期检查；进程采样无法取代操作系统容器隔离。

## 显式命令

仅 SAPPER_MODE=workspace 使用以下命令：

```bash
bash docker/commander/worker.sh release i003 --reason '该路线已退役'
bash docker/commander/worker.sh reassign i003 --to <接收角色ID> --expected-owner <旧角色ID> --reason '更换执行者'
# 意图已开放时显式使用 --expected-owner none。
```

释放/转派检查意图未完成、原持有者已停止或无登记，并再次核对原进程确已退出。工作区和指挥关系均需匹配；角色内执行由指挥官/将军负责。用户 CLI 没有角色身份时使用显式工作区上下文。

--expected-owner 可明确绑定先前观察；省略时 CLI 在动作前读取持有者，并在图锁内比较。预期与实际不符会报出两者，不改变所有权。转派接收者须离线且没有其他未完成意图，避免运行进程仍持有旧任务的 SAPPER_INTENT_ID；转派绑定新任务和说明，再显式 resume 接收角色。写入失败会回滚接收角色记录，意图在最后一步保存，因此所有权仍能从图中确定。

工作区原生 release 表单及图 release API 也走相同检查；普通比赛 CLI 的 release 行为保留。已释放任务的迟到受管结果会被拒绝，避免重新完成已交回的任务。

## 恢复

resume 先核对旧 intent：仍开放则在启动前重新领取；仍归自己则延续；已分配给其他角色则报“已转派，不能继续旧分配”；已完成则保留既有完成记录。stop→resume 现在会经历释放→重新领取，双方之间若发生转派，旧角色不能抢回。

## 模式和验证

docker/commander/worker.sh 和 watchdog.sh 的比赛分支未修改；新命令只由现有 workspace 分流进入 Python 适配器。测试均使用临时图、独立测试 tmux socket 或临时无害进程，不操作真实比赛工作区。

tests/test_sapper_intent_lifecycle.py 覆盖停止后释放全部任务、外部终端等待、旧结果拒绝、开放任务恢复、写入失败回滚、两个转派竞争、stop/转派互斥、历史 run 停止、新进程组工具清理及 PID 复用。既有完整测试继续验证终止宽限、保留成果和其他工作区不受影响。
