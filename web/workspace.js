"use strict";

state.agents = [];
state.services = [];
state.workspaceFolder = ".";
state.roots = [];
state.nativePane = "";
state.nativeControl = false;
state.nativeStream = null;
let terminal, terminalFit, inputBuffer = "", inputTimer, messageAgent, inputQueue = Promise.resolve();
let roleInfoRequest = 0;
let dispatchRequestId;
let directoryRequest = 0;
let selectedDirectory = "";
let agentTopology;
const flagSettingsRequests = new Set();
const roleNames = {worker:"执行者",commander:"指挥官",general:"将军",scout:"侦察兵",verifier:"验证者",custom:"自定义职责"};
const baseRefreshTask = refreshTask;
const baseSetTab = setTab;

function workspaceApi(action, options = {}) { return api(`/workspaces/${state.project.id}/${action}`, options); }
function formatAgentStatus(a,panes=state.panes) {
  return AgentTopologyModel.labels[(a.observation||AgentTopologyModel.observe(a,panes)).status];
}

refreshTask = async function () {
  if (state.view !== "project") return baseRefreshTask();
  const pid = state.project?.id;
  if (!pid) return;
  const navigation = state.navigation;
  try {
    if (agentTopology) agentTopology.switchWorkspace(pid);
    const [, roles, inspection] = await Promise.all([
      baseRefreshTask(), api(`/workspaces/${pid}/agents`), api(`/workspaces/${pid}/inspect`)
    ]);
    if (state.view !== "project" || pid !== state.project?.id || navigation !== state.navigation) return;
    state.agents = roles.agents;
    state.services = roles.services;
    state.inspection = inspection;
    state.mainAgent = roles.primary_agent;
    $("workspace-permissions").textContent=AgentTopologyModel.permissionSummary(state.agents,state.panes);
    if($("agent-dialog").open)refreshDispatchState();
    if (!flagSettingsRequests.has(pid)) {
      $("auto-submit-flags").checked = inspection.auto_submit_flags;
      $("auto-submit-flags").disabled = !inspection.graph;
    }
    const ready = inspection.graph && inspection.instructions && Object.values(inspection.hooks).every(Boolean);
    $("project-status").textContent = state.graph.project.status === "completed" ? "目标已完成" : ready ? "工作区已接入" : "待启用 sapper";
    $("enable-workspace").textContent = ready ? "检查／更新接入" : "启用 sapper";
    $("workspace-readiness").textContent = ready ? "题目原目录 · hooks 与共享图已接入" : "启用后合并项目级 hooks 与说明，保留已有内容";
    renderAgents();
    renderNativePanes();
    if (state.workspaceFilesPid !== pid && state.tab === "workspace") {
      state.workspaceFolder = ".";
      state.workspaceFilesPid = pid;
      await workspaceFiles();
    }
  } catch (error) {
    if (state.view === "project" && pid === state.project?.id && navigation === state.navigation) agentTopology?.setConnection(false);
    throw error;
  }
};

setTab = function (tab) {
  if(tab==="chat")tab="workspace";
  $("tab-agents").hidden = tab !== "agents";
  if (tab !== "workspace") {
    $("tab-workspace").hidden = true;
    baseSetTab(tab);
    if (tab === "agents" && agentTopology) requestAnimationFrame(() => {
      if (agentTopology.current.needsFit) agentTopology.fit();
      else agentTopology.transform();
    });
    return;
  }
  state.tab = tab;
  for (const name of ["memory","console","files"]) $("tab-"+name).hidden = true;
  $("tab-workspace").hidden = false;
  document.querySelectorAll("[data-tab]").forEach(n => {
    n.classList.toggle("active",n.dataset.tab===tab);
    n.setAttribute("aria-selected",String(n.dataset.tab===tab));
  });
  ensureTerminal();
  if (state.project) {
    state.workspaceFilesPid = state.project.id;
    guarded(workspaceFiles);
  }
  if (terminalFit) requestAnimationFrame(() => terminalFit.fit());
};

async function openBind() {
  const opening=++directoryRequest;
  $("bind-path").value="";
  $("directory-select").disabled=true;
  $("directory-up").disabled=true;
  $("directory-status").textContent="正在读取文件夹…";
  $("bind-dialog").showModal();
  const result = await api("/directories");
  if (opening!==directoryRequest || !$("bind-dialog").open) return;
  state.roots = result.roots;
  $("directory-roots").replaceChildren(...result.roots.map((root,index) => {
    const label=root==="/"?"文件系统":index===0?"任务文件夹":root.split("/").filter(Boolean).pop()||root;
    const entry=button(label,"button",()=>guarded(()=>browseDirectory(root)));
    entry.title=root;
    return entry;
  }));
  await browseDirectory(selectedDirectory||state.roots[0]);
}
function directoryBreadcrumbs(path) {
  const parts=path.split("/").filter(Boolean);
  let current="";
  const crumbs=[button("文件系统","text-button directory-crumb",()=>guarded(()=>browseDirectory("/")))];
  for (const part of parts) {
    current+="/"+part;
    const target=current;
    crumbs.push(element("span","directory-separator","/"),
      button(part,"text-button directory-crumb",()=>guarded(()=>browseDirectory(target))));
  }
  crumbs[crumbs.length-1].setAttribute("aria-current","location");
  $("directory-breadcrumbs").replaceChildren(...crumbs);
}
async function browseDirectory(path) {
  const request=++directoryRequest;
  $("bind-path").value="";
  $("directory-select").disabled=true;
  $("directory-status").textContent="正在读取文件夹…";
  $("directory-list").setAttribute("aria-busy","true");
  try {
    const data = await api("/directories?path="+encodeURIComponent(path));
    if (request!==directoryRequest) return;
    selectedDirectory=data.path;
    $("bind-path").value=data.path;
    directoryBreadcrumbs(data.path);
    $("directory-up").disabled=!data.parent;
    $("directory-up").onclick=()=>guarded(()=>browseDirectory(data.parent));
    $("directory-list").replaceChildren(...(data.directories.length?data.directories.map(dir => {
      const entry=button("","text-button directory-item",()=>guarded(()=>browseDirectory(dir.path)));
      const icon=element("span","directory-icon","▱");
      icon.setAttribute("aria-hidden","true");
      entry.append(icon,element("span","directory-name",dir.name),
        element("span","directory-meta",dir.has_graph?"已有图 · 打开":"打开"));
      return entry;
    }):[empty("此文件夹没有子文件夹，可以直接选择。")]));
    $("directory-status").textContent="当前选择："+data.path;
    $("directory-select").disabled=false;
  } catch (error) {
    if (request!==directoryRequest) return;
    $("directory-status").textContent="无法打开此文件夹，请从常用位置或上一级重新选择。";
    throw error;
  } finally {
    if (request===directoryRequest) $("directory-list").removeAttribute("aria-busy");
  }
}
async function workspaceFiles() {
  const pid=state.project.id, folder=state.workspaceFolder, navigation=state.navigation;
  const files=await taskApi("files?path="+encodeURIComponent(folder));
  if (pid!==state.project?.id || folder!==state.workspaceFolder || navigation!==state.navigation) return;
  $("workspace-file-path").textContent=folder==="."?"工作区根目录":folder;
  $("workspace-file-list").replaceChildren(...(files.length?files.map(file=>button((file.directory?"▱ ":"▤ ")+file.name,"text-button workspace-file",()=>guarded(async()=>{
    if(file.directory){state.workspaceFolder=file.path;await workspaceFiles();return;}
    const data=await taskApi("file?path="+encodeURIComponent(file.path));
    $("viewer-title").textContent=file.name;
    $("viewer-content").textContent=data.text;
    $("viewer-download").href=`api/projects/${pid}/file?path=${encodeURIComponent(file.path)}&download=1`;
    $("viewer-dialog").showModal();
  }))):[element("p","workspace-empty","目录中暂无可显示的文件。")])) ;
}

function openAgent(role,parent="") {
  dispatchRequestId=crypto.randomUUID();
  role = role || (state.mainAgent?.pane_id ? "worker" : "commander");
  $("agent-form").reset();
  $("agent-form").elements.skip_permissions.checked=Boolean(state.mainAgent?.skip_permissions);
  $("dispatch-error").textContent="";
  $("agent-role").value=role;
  $("agent-role").closest("label").hidden=!state.mainAgent?.pane_id;
  $("agent-location").value="agent";
  $("agent-mission").value=state.graph?.project.goal||state.project.goal;
  refreshAgentModelOptions(false);
  $("agent-parent").replaceChildren(element("option","","无／独立主会话"));
  $("agent-parent").firstChild.value="";
  for(const a of state.agents.filter(a=>!a.id.startsWith('legacy-'))){const opt=element("option","",agentOptionLabel(a));opt.value=a.id;$("agent-parent").append(opt);}
  $("agent-parent").value=parent;
  updateAgentModelHint();
  updateRoleFields();
  $("agent-dialog").showModal();
  guarded(loadAgentExtensions);
}
function originalModelLabel(agent){
  if(agent.resume_model)return agent.resume_model.original_label;
  if(!agent.profile_id)return "继承原环境";
  const profile=agent.profile_snapshot||state.profiles.find(p=>p.id===agent.profile_id);
  return profile?`${profile.name} · ${profile.model}`:agent.profile_name||"原模型配置";
}
function dispatchCreatesSession(){
  return !state.mainAgent||(!state.mainAgent.pane_id&&$("dispatch-session-mode").value==="new");
}
function refreshDispatchState(){
  const main=state.mainAgent, offline=main&&!main.pane_id, fresh=dispatchCreatesSession();
  $("dispatch-session-choice").hidden=!offline;
  $("dispatch-settings").hidden=!fresh;
  $("agent-permissions").hidden=Boolean(main?.pane_id);
  $("dispatch-submit").textContent=fresh?(main?"新建主会话":"派发主 agent"):offline?"恢复原会话":"交给主 agent";
  $("dispatch-help").textContent=fresh?"启动新的主 agent，在原生终端中澄清目标并安排执行。":offline?"恢复本工作区的原生会话，继续已有任务。":"派发请求会交给本工作区的主 agent，由它核对任务并安排执行。";
  const selected=$("agent-profile").value;
  const profile=state.profiles.find(p=>p.id===selected);
  const defaultModel=Object.hasOwn(effectiveRoleAssignments(),"commander")?roleDefaultLabel("commander"):"继承原环境";
  const current=selected==="__role_default__"?defaultModel:profile?modelConnectionLabel(profile):"继承原环境";
  $("dispatch-model-summary").textContent=fresh?`新会话使用：${current}。${main?"旧会话、工作文件和共享图保留。":""}`:
    offline?`恢复使用：${originalModelLabel(main)}。当前角色默认：${defaultModel}，用于新建会话；要使用此配置，请选择“按当前配置新建”。`:
    `当前主 agent 使用：${originalModelLabel(main)}。`;
}
function updateRoleFields(){
  refreshAgentModelOptions();
  guarded(loadRoleInfo);
}
async function loadRoleInfo(){
  const request=++roleInfoRequest,role=$("agent-role").value;
  const known=state.inspection?.role_catalog?.find(item=>item.id===role);
  $("agent-role-summary").textContent=known?.summary||"正在读取角色基础职责…";
  $("agent-role-source").textContent=known?`${known.prompt_version} · ${known.sources.join("；")}`:"";
  $("agent-role-prompt").textContent="正在读取实际提示词…";
  const selected=[state.project.id];
  const params=new URLSearchParams({role,scope:[...new Set(selected)].join(",")});
  const info=await workspaceApi("role-prompt?"+params);
  if(request!==roleInfoRequest||role!==$("agent-role").value)return;
  $("agent-role-summary").textContent=info.summary;
  $("agent-role-source").textContent=`${info.prompt_version} · ${info.sources.join("；")}`;
  $("agent-role-prompt").textContent=info.prompt;
}
function renderAgents() {
  if (!agentTopology) agentTopology = new AgentTopologyView($("agent-topology"), renderAgentCard, () => openAgent());
  agentTopology.update(state.project.id, state.agents, state.panes, state.graph);
  const signature=JSON.stringify([state.project.id,state.agents,state.panes,state.services,state.graph.facts,state.graph.intents]);
  if(state.agentSignature===signature)return;
  state.agentSignature=signature;
  const list=$("agent-list");list.replaceChildren();
  const rows=AgentTopologyModel.normalize(state.agents,state.panes).sort(AgentTopologyModel.compare);
  $("workspace-agent-count").textContent=String(rows.length);
  for (const a of rows) {
    const row=button("","agent-preview-row at-status-"+a.observation.status,()=>guarded(()=>openAgentOutput(a)));
    row.dataset.roleAgent=a.id;
    row.setAttribute("aria-pressed",String(Boolean(a.pane_id)&&a.pane_id===state.nativePane));
    row.title=a.observation.evidence;
    const info=element("span","agent-preview-info");
    info.append(element("strong","",a.name||a.role_label),element("span","agent-preview-task",AgentTopologyModel.shortGoal(a)));
    row.append(element("i",""),info,element("span","agent-preview-status",AgentTopologyModel.labels[a.observation.status]));
    list.append(row);
  }
  if(!rows.length)list.append(element("p","workspace-empty","还没有登记的角色。点击“派发角色”启动主 agent。"));
  list.append(button(`打开协作拓扑 · ${rows.length} 个 Agent`,"text-button agent-preview-link",()=>setTab("agents")));
  renderAgentServices($("service-list"));
  renderAgentServices(agentTopology.el("services"));
}

async function openAgentOutput(a){
  if(a.execution_mode==="print"&&a.run_id){setTab("console");await openLog({id:a.run_id,profile_name:a.profile_name});}
  else if(a.pane_id){setTab("workspace");selectNative(a.pane_id);}
  else{setTab("agents");agentTopology.select(a.id,true);}
}

function renderAgentCard(a) {
  const node=element("article","agent-card"+(a.parent_agent_id?" child":""));
  const title=element("div","agent-title");
  title.append(element("span","",a.name||a.role_label),element("span","badge",formatAgentStatus(a)));
  node.append(title,element("p","agent-sub",`${a.role_label} · ${a.execution_mode==="chat"?"网页对话":a.execution_mode==="cli"?"原 CLI 流程":a.execution_mode==="print"?"限时单次":"原生交互"} · ${a.profile_name}`));
  const permission=element("p","agent-context agent-process-permissions",`进程权限：${AgentTopologyModel.permissionLabel(a)}`);
  permission.title="根据该角色本次或最近一次运行的启动记录显示";
  node.append(permission);
  if(a.resume_model)node.append(element("p","agent-context",`恢复使用：${a.resume_model.original_label}。当前角色默认：${a.resume_model.current_label}，用于新建会话。`));
  if(a.parent_agent_id)node.append(element("p","agent-parent","派发自 "+a.parent_agent_id.slice(0,8)));
  if(a.intent_id)node.append(element("p","agent-parent","派单意图 "+a.intent_id));
  if(a.shared_context){const c=a.shared_context;node.append(element("p","agent-context",contextStatus(c)));}
  node.append(element("p","agent-mission",a.mission));
  if(a.commander_monitor){
    const m=a.commander_monitor;
    const controls={active:"允许自动续轮",paused:"自动续轮已暂停",waiting_user:"等待用户决定",completed:"调度任务已完成"};
    const phases={idle:"本轮已结束",responding:"模型处理中",tool_running:"工具执行中",waiting_user:"等待用户输入",unknown:"执行状态未确认"};
    const verification={not_observed:"尚未观测",transcript_unavailable:"无法读取会话记录",wake_sent:"续轮指令已发送",process_started:"恢复进程已启动",input_accepted:"原生会话已接收指令",responded:"模型已响应",board_confirmed:"已确认恢复读板"};
    node.append(element("p","agent-context",`${controls[m.control]||m.control} · ${phases[m.phase]||m.phase}${m.last_board?" · 最近读板："+time(new Date(m.last_board.at*1000).toISOString()):" · 暂无读板回执"}`));
    node.append(element("p","agent-context",verification[m.verification]||m.verification||"尚未确认续轮"));
    if(m.attention)node.append(element("p","agent-context",m.attention));
    if(m.reason)node.append(element("p","agent-context",m.reason));
  }
  appendRunActivity(node,a);
  const details=element("details","agent-details");
  details.append(element("summary","","会话与目录"),element("p","","Claude ID: "+(a.native_operation?"原 CLI 多步骤流程，无单一会话 ID":a.claude_session_id||"未知，未推断")),element("p","","cwd: "+a.cwd));
  if(a.extensions){
    details.append(element("p","",`MCP: ${a.extensions.mcp.map(r=>r.name).join("、")||"无"}`),
      element("p","",`Skills: ${a.extensions.skills.map(r=>r.name).join("、")||"无"}`),
      element("p","","会话存储: "+a.session_storage));
  }
  if(a.shared_context){const c=a.shared_context;details.append(element("p","",`全局配置 v${c.latest_revision}；启动注入：${c.launch_revision?"v"+c.launch_revision:"无"}；最近 hook 检查：${c.hook_revision?"v"+c.hook_revision:"暂无回执"}${c.hook_checked_at?"（"+time(c.hook_checked_at)+"）":""}`));}
  details.append(element("p","",a.role_prompt_version?`基础职责：${a.role_prompt_version} · ${a.role_prompt_sha256?.slice(0,12)||"未记录摘要"}`:"基础职责：旧会话未记录版本"));
  if(a.workspace_scope?.length>1)details.append(element("p","","工作区范围: "+a.workspace_scope.map(id=>state.projects.find(p=>p.id===id)?.name||id).join("、")));
  const facts=state.graph.facts.filter(f=>f.creator===a.id);
  const intents=state.graph.intents.filter(i=>i.creator===a.id);
  details.append(element("p","",a.native_operation?"图记录保留原 CLI 的 reason/explore 等来源标签，不伪装为 Web 会话贡献。":`共享图记录：${facts.length} 条事实 · ${intents.length} 条意图`));
  for(const f of facts.slice(-5))details.append(button(`${f.id} · ${f.title||f.description}`,"text-button",()=>openDetail(f)));
  const actions=element("div","agent-actions");
  if(a.commander_monitor&&a.desired_state==="running")actions.append(button(a.commander_monitor.control==="active"?"暂停续轮":"启用续轮","text-button",()=>guarded(async()=>{
    const control=a.commander_monitor.control==="active"?"paused":"active";
    await api(`/agents/${a.id}/control`,{method:"POST",body:{control,reason:"用户在角色卡片设置"}});
    if(control==="active")await workspaceApi("watchdog",{method:"POST",body:{}});
    await refreshTask();
  })));
  if(a.execution_mode==="print"&&a.run_id){
    actions.append(button("查看活动","text-button",()=>guarded(()=>{setTab("console");return openLog({id:a.run_id,profile_name:a.profile_name});})));
    if(a.supervisor_version===1&&["starting","running"].includes(a.status))actions.append(button("延长预算","text-button",()=>openRunBudget(a.run_id)));
  }
  if(a.pane_id){
    if(a.id.startsWith('legacy-')){
      actions.append(button("打开终端","text-button",()=>{setTab("workspace");selectNative(a.pane_id);}),button("登记角色","text-button",()=>{
        $("attach-agent-form").reset();$("attach-agent-form").elements.pane_id.value=a.pane_id;$("attach-agent-form").elements.name.value=a.name;$("attach-agent-dialog").showModal();
      }));
      node.append(details,actions);return node;
    }
    actions.append(button("查看终端","text-button",()=>{setTab("workspace");selectNative(a.pane_id);}));
    if(a.role==="commander"&&a.execution_mode==="chat")actions.append(button("使用交互终端","text-button",()=>guarded(async()=>{
      await workspaceApi("dispatch",{method:"POST",body:{role:"commander",execution_mode:"tui"}});
      await refreshTask();setTab("workspace");if(state.mainAgent?.pane_id)selectNative(state.mainAgent.pane_id);
    })));
    actions.append(button("停止","text-button danger",()=>guarded(async()=>{
      if(!await confirmAction("停止该角色的原会话及其进程？watchdog 不会将这次明确停止当作异常重启。"))return;
      await api(`/agents/${a.id}/stop`,{method:"POST",body:{}});await refreshTask();
    })));
  } else if(a.can_resume)actions.append(button("恢复此会话","text-button",()=>guarded(async()=>{
    await api(`/agents/${a.id}/resume`,{method:"POST",body:a.role==="commander"?{execution_mode:"tui"}:{}});await refreshTask();setTab("workspace");const resumed=state.agents.find(row=>row.id===a.id);if(resumed?.pane_id)selectNative(resumed.pane_id);toast("已按原生会话恢复");
  })));
  if(a.can_resume&&a.id===state.mainAgent?.id)actions.append(button("按当前配置新建","text-button",()=>{
    openAgent("commander");$("dispatch-session-mode").value="new";refreshDispatchState();
  }));
  if(a.native_operation)actions.append(button("原流程参数","text-button",()=>guarded(async()=>{await openNative(a.native_request.operation,a.native_request.values||{});$("native-profile").value=a.profile_id||"";})));
  if(!a.id.startsWith("legacy-"))actions.append(button("删除角色","text-button danger",()=>guarded(async()=>{
    const effect=a.external_session&&!a.run_id?"将移除角色登记，已有外部终端继续运行。":"将停止该角色当前任务并从角色列表移除，watchdog 不会恢复它。";
    const scope=a.workspace_scope?.length>1?"此角色关联多个工作区，删除会在所有关联工作区生效。":"";
    if(!await confirmAction(`删除“${a.name||a.role_label}”？${effect}${scope}已有子角色继续运行，文件、日志和共享图记录保留。`))return;
    await api(`/agents/${a.id}`,{method:"DELETE"});state.nativePaneSignature=null;await refreshTask();toast("角色已删除");
  })));
  node.append(details,actions);return node;
}

function renderAgentServices(list) {
  list.replaceChildren(...state.services.map(s=>{
    const card=element("div","service-card",`Watchdog · ${names[s.status]||s.status}\n进程保活与指挥官续轮检查；唤醒后会触发模型调用`);
    card.append(button("查看日志","text-button",()=>guarded(async()=>{setTab("console");await openLog({id:s.run_id,profile_name:"Watchdog"});})));
    return card;
  }));
}

function terminalTheme(){
  const style=getComputedStyle(document.documentElement);
  const color=name=>style.getPropertyValue(name).trim();
  const dark=document.documentElement.dataset.theme==="dark";
  return {background:color("--terminal"),foreground:color("--terminal-text"),cursor:color("--terminal-cursor"),
    selectionBackground:dark?"#79c0ce40":"#176c7b30",
    black:dark?"#304754":"#243746",red:color("--danger"),green:color("--success"),yellow:color("--warning"),
    blue:dark?"#8ab4f8":"#275dad",magenta:dark?"#d4a6e8":"#854ba2",cyan:color("--accent"),white:dark?"#bdcdd8":"#647887",
    brightBlack:dark?"#718590":"#536c7a",brightRed:color("--danger"),brightGreen:color("--success"),brightYellow:color("--warning"),
    brightBlue:dark?"#a5c7ff":"#275dad",brightMagenta:dark?"#e4c1f5":"#854ba2",brightCyan:color("--accent"),brightWhite:color("--terminal-text")};
}
window.addEventListener("sapper:themechange",()=>{if(terminal)terminal.options.theme=terminalTheme();});

function ensureTerminal(){
  if(terminal)return;
  terminal=new Terminal({cursorBlink:true,fontSize:12,fontFamily:'"SFMono-Regular",Consolas,monospace',scrollback:5000,disableStdin:true,theme:terminalTheme()});
  terminalFit=new FitAddon.FitAddon();terminal.loadAddon(terminalFit);terminal.open($("native-terminal"));
  terminal.writeln("点击“派发角色”启动主 agent，或选择已有角色终端。\r\n点击“接管输入”后，可以直接在终端中补充信息、回答问题和纠偏。");
  terminal.onData(data=>{
    if(!state.nativeControl||!state.nativePane)return;
    // tmux already answers device queries; do not inject the browser emulator's replies twice.
    if(/^\x1b\[(?:\?|>|=)?[0-9;]*[cRn]$/.test(data))return;
    inputBuffer+=data;clearTimeout(inputTimer);inputTimer=setTimeout(flushTerminalInput,20);
  });
  terminal.onResize(size=>{if(state.nativeControl&&state.nativePane)guarded(()=>api(`/panes/${encodeURIComponent(state.nativePane)}/resize`,{method:"POST",body:size}));});
  new ResizeObserver(()=>{if(state.tab==="workspace")terminalFit.fit();}).observe($("native-terminal"));
}
async function flushTerminalInput(){
  const data=inputBuffer,pane=state.nativePane;inputBuffer="";
  if(data&&pane){inputQueue=inputQueue.then(()=>guarded(()=>api(`/panes/${encodeURIComponent(pane)}/input`,{method:"POST",body:{data}})));await inputQueue;}
}
function agentOptionLabel(a){
  return `${a.name||a.role_label} · ${AgentTopologyModel.shortGoal(a)} · ${a.id.slice(0,6)}`;
}
function renderNativePanes(){
  const rolePanes=new Set(state.agents.map(a=>a.pane_id));
  const all=state.panes.filter(p=>!p.dead&&(p.project_id===state.project.id||rolePanes.has(p.id)));
  const options=all.map(p=>{
    const a=state.agents.find(a=>a.pane_id===p.id), service=state.services.find(s=>s.pane_id===p.id);
    return [p.id,a?`${agentOptionLabel(a)} · ${formatAgentStatus(a)}`:service?"Watchdog · 脚本服务":`已有会话 · ${p.name}`];
  });
  const sig=JSON.stringify(options);if(state.nativePaneSignature===sig)return;
  state.nativePaneSignature=sig;
  $("native-pane").replaceChildren(...options.map(([id,label])=>{const opt=element("option","",label);opt.value=id;return opt;}));
  const chosen=all.some(p=>p.id===state.nativePane)?state.nativePane:
    all.find(p=>p.id===state.mainAgent?.pane_id)?.id||all.find(p=>rolePanes.has(p.id))?.id||all[0]?.id||"";
  if(!all.length){const opt=element("option","","暂无活动角色终端");opt.value="";$("native-pane").append(opt);}
  $("native-pane").value=chosen;
  if(chosen!==state.nativePane)selectNative(chosen);
}
function selectNative(pane){
  if(state.nativePane===pane&&state.nativeStream)return;
  if(state.nativeStream)state.nativeStream.close();state.nativeStream=null;
  state.nativePane=pane;state.nativeControl=false;inputBuffer="";
  document.querySelectorAll("#agent-list [data-role-agent]").forEach(row=>row.setAttribute("aria-pressed",String(state.agents.some(a=>a.id===row.dataset.roleAgent&&a.pane_id===pane))));
  $("native-pane").value=pane;
  $("terminal-control").textContent="接管输入";$("terminal-control").classList.remove("controlling");
  ensureTerminal();terminal.options.disableStdin=true;
  if(!pane){$("native-mode").textContent="暂无活动终端，可派发角色或从角色列表恢复会话";return;}
  terminal.reset();$("native-mode").textContent="正在接入原终端…";
  const stream=new EventSource(`api/panes/${encodeURIComponent(pane)}/stream`);
  state.nativeStream=stream;
  stream.onmessage=event=>{
    if(state.nativeStream!==stream)return;
    const data=JSON.parse(event.data);
    if(data.type==="mode"){
      $("native-mode").textContent=data.mode==="stream"?"PTY 输出实时连接 · 默认只读":"已有输出订阅，使用快照兼容模式";
      terminal.resize(data.cols,data.rows);
    } else if(data.type==="reset")terminal.reset();
    else if(data.type==="output")terminal.write(Uint8Array.from(atob(data.data),c=>c.charCodeAt(0)));
    else if(data.type==="snapshot"){
      terminal.resize(data.cols,data.rows);terminal.reset();
      terminal.write(data.text.replace(/\r?\n/g,"\r\n")+`\x1b[${data.y+1};${data.x+1}H`);
    } else if(data.type==="ended"){
      stream.close();state.nativeStream=null;state.nativeControl=false;terminal.options.disableStdin=true;$("native-mode").textContent="原会话已结束，保留当前输出";
    }
  };
  stream.onerror=()=>{$("native-mode").textContent="连接中断，正在重连；原会话保持运行";};
}

function disconnectNative(){
  if(state.nativeStream)state.nativeStream.close();
  state.nativeStream=null;state.nativePane="";state.nativeControl=false;inputBuffer="";
  state.nativePaneSignature=null;
  if(terminal){terminal.options.disableStdin=true;terminal.reset();}
}

$("bind-workspace").onclick=()=>guarded(openBind);
$("bind-dialog").addEventListener("close",()=>{++directoryRequest;});
const projectTools=$("project-tools");
function closeProjectTools(restoreFocus=false){
  if(!projectTools.open)return;
  projectTools.open=false;
  if(restoreFocus)projectTools.querySelector("summary").focus();
}
document.addEventListener("click",event=>{
  if(!projectTools.contains(event.target))closeProjectTools();
});
projectTools.addEventListener("keydown",event=>{
  if(event.key==="Escape" && projectTools.open){event.preventDefault();closeProjectTools(true);}
});
// Close before the action opens its dialog so dismissing it returns to the disclosure.
projectTools.addEventListener("click",event=>{
  if(event.target.closest("button"))closeProjectTools(true);
},true);
projectTools.addEventListener("focusout",event=>{
  if(event.relatedTarget && !projectTools.contains(event.relatedTarget))closeProjectTools();
});
$("dispatch-role").onclick=()=>openAgent();
$("advanced-run").onclick=()=>openRun();
$("agent-role").onchange=updateRoleFields;
$("dispatch-session-mode").onchange=()=>{$("dispatch-error").textContent="";refreshDispatchState();};
$("enable-workspace").onclick=()=>guarded(async()=>{
  await workspaceApi("enable",{method:"POST",body:{}});await refreshProjects();toast("项目级 hooks 与说明已接入，用户内容已保留");
});
$("auto-submit-flags").onchange=()=>guarded(async()=>{
  const input=$("auto-submit-flags"), pid=state.project.id, enabled=input.checked;
  const hadFocus=document.activeElement===input;
  flagSettingsRequests.add(pid);
  $("auto-submit-flags").disabled=true;
  try {
    await api(`/workspaces/${pid}/settings`,{method:"POST",body:{auto_submit_flags:enabled}});
    if(state.project?.id===pid)toast(enabled?"已开启 AI 主动提交 flag；运行中角色下一轮读取新设置":"已关闭 AI 主动提交 flag");
  } finally {
    flagSettingsRequests.delete(pid);
    await refreshTask();
    if(state.project?.id===pid && hadFocus && projectTools.open && document.activeElement===document.body)input.focus();
  }
});
$("stop-workspace").onclick=()=>guarded(async()=>{
  if(!await confirmAction("停止本工作区登记的角色和 watchdog？其他工作区不受影响，原图与会话历史保留。"))return;
  await workspaceApi("stop",{method:"POST",body:{}});await refreshTask();toast("已停止本工作区角色");
});
$("workspace-file-up").onclick=()=>{const parts=state.workspaceFolder.split("/");parts.pop();state.workspaceFolder=parts.join("/")||".";guarded(workspaceFiles);};
$("native-pane").onchange=()=>selectNative($("native-pane").value);
$("terminal-control").onclick=()=>guarded(async()=>{
  if(!state.nativePane)throw new Error("请先选择活动终端");
  state.nativeControl=!state.nativeControl;terminal.options.disableStdin=!state.nativeControl;
  $("terminal-control").textContent=state.nativeControl?"只读查看":"接管输入";$("terminal-control").classList.toggle("controlling",state.nativeControl);
  if(state.nativeControl){terminalFit.fit();terminal.focus();await api(`/panes/${encodeURIComponent(state.nativePane)}/resize`,{method:"POST",body:{cols:terminal.cols,rows:terminal.rows}});}
});
document.querySelectorAll("[data-tab]").forEach(node=>{node.onclick=()=>setTab(node.dataset.tab);});
document.querySelectorAll(".dialog-close").forEach(node=>{node.onclick=()=>node.closest("dialog").close();});

submit("bind-form",async form=>{
  if (!form.get("path")) throw new Error("请先选择一个可访问的文件夹");
  const p=await api("/workspaces/bind",{method:"POST",body:Object.fromEntries(form)});
  state.project=p;localStorage.setItem("sapper-project",p.id);state.workspaceFilesPid=null;
  state.view="project";
  $("bind-dialog").close();await refreshProjects();setTab("workspace");openAgent("commander");toast("已打开工作区，请确认初步配置");
});
submit("agent-form",async form=>{
  const data=Object.fromEntries(form);data.minutes=Number(data.minutes);
  data.new_session=Boolean(state.mainAgent)&&dispatchCreatesSession();
  if(data.new_session)data.role="commander";
  if(data.profile_id==="__role_default__")delete data.profile_id;
  data.skip_permissions=form.has("skip_permissions");data.request_id=dispatchRequestId;
  if(!$("agent-extensions-default").checked)data.extensions=readExtensionSelection("agent-extension-options");
  data.execution_mode="tui";
  $("dispatch-error").textContent="";
  try{await workspaceApi("dispatch",{method:"POST",body:data});}
  catch(error){$("dispatch-error").textContent=error.message;throw error;}
  $("agent-dialog").close();setTab("workspace");await refreshTask();
  if(state.mainAgent?.pane_id)selectNative(state.mainAgent.pane_id);
  toast("已交给本工作区主 agent，可在终端查看和继续交流");
});
submit("agent-message-form",async()=>{
  const result=await api(`/agents/${messageAgent}/messages`,{method:"POST",body:{text:$("agent-message-text").value}});
  $("agent-message-dialog").close();toast(result.message);
});
submit("attach-agent-form",async form=>{
  await workspaceApi("attach",{method:"POST",body:Object.fromEntries(form)});$("attach-agent-dialog").close();await refreshTask();toast("已关联原会话，没有重启或移动目录");
});
state.tab="workspace";
