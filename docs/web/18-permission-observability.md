# 工作区权限观测（阶段 A）

本阶段保留现有启动行为：请求明确提供 skip_permissions 时使用请求值；否则恢复使用原角色记录值；新角色缺省为 false。工作区 worker CLI 的 spawn 仍把父角色值传给子角色。尚未引入工作区级权限设置。

agent.started、agent.resumed、agent.dispatched 与启动前的 run.json 记录相同的 request_id、input_present、input_skip_permissions、previous_skip_permissions、effective_skip_permissions、permission_source。未提供的输入和新角色的旧值为 null。CLI spawn 的自动继承记为 parent_agent，缺少父角色权限记录时记为 default，不伪装成显式请求。其余来源是 request、existing_agent、default。新建执行角色产生 dispatched 事件，包含父角色（可为空）、intent 和 run ID。

Web、worker CLI、watchdog 和指挥官监控器传递同一请求 ID；CLI 可使用 --request-id 关联调用。历史运行不回填、未知权限不当作 false。权限观测表示启动记录中的有效选择；不监控用户在原 CLI 内随后切换模式或原生 settings 对运行行为的影响。

头部统计当前有存活进程的角色，角色卡显示本次或最近一次运行的权限。非交互执行表单在未跳过审批时提示“遇到需要审批的操作会被拒绝并反复重试”。本阶段没有实现连续拒绝熔断。

已启动 worker 的权限受阻属于执行阻塞。指挥官检查证据后可重派、换路线、降级或请求具体决定；只有推进依赖用户回复或授权时进入 waiting_user，独立任务继续。指挥官不得自行开启跳过审批来解除阻塞。

## 模式差异

SAPPER_MODE=workspace 的 worker.sh/watchdog.sh 走 Python 工作区适配器。docker/commander 的比赛分支继续使用原跳过审批参数、自动确认和恢复流程；本阶段不改变它们的行为。

## 验证入口

- tests/test_permission_audit.py：真实元数据在 tmux 创建前已经落盘，事件与运行字段一致，CLI 继承来源和显式 false/缺省可区分。
- tests/test_sapper_commander.py：恢复请求与 wake ID 关联。
- tests/test_agent_topology.cjs、tests/check_agent_topology_browser.py：存活角色计数、未知权限、角色卡和表单提示。
