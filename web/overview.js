"use strict";

let contextEditorRevision=0;

function contextStatus(c){
  if(!c.latest_revision)return "共享环境未配置";
  if(!c.enabled)return "共享环境已停用";
  const seen=Math.max(c.launch_revision||0,c.hook_revision||0);
  return seen>=c.latest_revision?`共享环境 v${seen} · 已提供`:`共享环境 ${seen?"v"+seen:"未加载"} · v${c.latest_revision} 待更新`;
}

const workspaceTabs = new Set(["progress", "workspace", "agents", "memory", "console", "files", "native"]);
const mainViews = {overview:["home-button","总体概览"], workspaces:["workspaces-button","工作区"], native:["native-button","原生功能"]};
const agentStatusLabels = {running:"运行中", waiting:"等待中", idle:"在线空闲", error:"异常", offline:"已离线", unknown:"状态未确认"};

function workspaceLocation() {
  if (state.view === "project" && state.project) return `#workspace/${state.project.id}/${state.tab}`;
  return state.view === "overview" ? "" : "#" + state.view;
}

function rememberWorkspaceLocation() {
  const url = location.pathname + workspaceLocation();
  if (url !== location.pathname + location.search + location.hash) history.replaceState(null, "", url);
}

async function restoreWorkspaceLocation() {
  if (location.hash === "#workspaces") return showOverview("workspaces");
  if (location.hash === "#native") return showOverview("native");
  const match = /^#workspace\/([a-f0-9]{16})\/([a-z]+)$/.exec(location.hash);
  const project = match && state.projects.find(p => p.id === match[1]);
  if (project) return openProject(project, workspaceTabs.has(match[2]) ? match[2] : "progress");
  return showOverview();
}

async function openProject(project,tab="progress"){
  if(typeof project==="string")project=state.projects.find(p=>p.id===project);
  if(!project)throw new Error("工作区已不可用，请刷新概览");
  state.navigation=(state.navigation||0)+1;
  disconnectNative();
  state.view="project";state.project=project;state.folder=".";state.workspaceFolder=".";state.log=null;state.workspaceFilesPid=null;
  state.graph={project, facts:[], intents:[], open_intents:[], hints:[]};
  state.panes=[];state.runs=[];state.agents=[];state.services=[];state.memorySignature=null;
  renderUserInputRequests([]);
  state.agentSignature=null;state.nativePaneSignature=null;
  state.mainAgent=null;state.workspaceObservation=null;state.progressSignature=null;state.graphLoad="loading";
  try {localStorage.setItem("sapper-project",project.id);} catch {}
  $("overview").hidden=true;$("workspaces-page").hidden=true;$("empty-state").hidden=true;$("project-workspace").hidden=false;$("log-panel").hidden=true;
  renderMainNavigation();
  renderConnection(null);
  $("breadcrumb").textContent=project.name;$("project-name").textContent=project.name;
  $("project-goal").textContent=project.goal;$("project-path").textContent=project.path;
  $("project-status").textContent="正在读取工作区…";
  $("fact-count").textContent=project.facts;$("intent-count").textContent=project.open_intents;
  $("hint-count").textContent="…";$("pane-count").textContent="…";
  renderProjects();renderMemory();renderPanes();renderRuns();
  agentTopology?.switchWorkspace(project.id);
  $("agent-list").replaceChildren(empty("正在读取角色…"));$("service-list").replaceChildren();
  $("workspace-file-list").replaceChildren(empty("正在读取文件…"));
  $("files-list").replaceChildren(empty("正在读取文件…"));
  setTab(tab);
  await refreshTask();
}

async function showOverview(view="overview"){
  state.navigation=(state.navigation||0)+1;
  disconnectNative();state.view=view;state.project=null;state.log=null;
  renderUserInputRequests([]);
  $("project-workspace").hidden=true;$("empty-state").hidden=true;$("overview").hidden=view!=="overview";
  $("workspaces-page").hidden=view!=="workspaces";
  renderMainNavigation();
  $("breadcrumb").textContent=mainViews[view][1];
  $("breadcrumb-section").hidden=true;
  renderConnection(state.overview?.summary.active_agents ?? null);
  rememberWorkspaceLocation();
  if(view==="workspaces")renderWorkspaceTable(state.overview?.projects||state.projects);
  renderProjects();
  await Promise.all([updateNativePage(),refreshOverview()]);
}

function renderMainNavigation(){
  for(const [view,[id]] of Object.entries(mainViews)){
    const link=$(id);
    link.classList.toggle("active",state.view===view);
    if(state.view===view)link.setAttribute("aria-current","page");
    else link.removeAttribute("aria-current");
  }
}

function projectStatus(p) {
  return p.status === "completed" ? "已完成" : p.enabled ? "进行中" : "待接入";
}

function workspaceAttention(p) {
  const agents = p.agent_summary || {};
  return (p.pending_confirmation || 0) + (agents.waiting || 0) + (agents.attention || 0);
}

function renderWorkspaceTable(projects) {
  const query = $("workspaces-search").value.trim().toLowerCase();
  const filter = $("workspaces-filter").value;
  const rows = projects.filter(p => {
    const matches = [p.name, p.id, p.goal].some(value => String(value || "").toLowerCase().includes(query));
    if (!matches || filter === "all") return matches;
    if (filter === "attention") return workspaceAttention(p) > 0;
    return (p.enabled ? p.status : "not_enabled") === filter;
  }).map(p => {
    const row = element("tr"), title = element("td");
    title.append(button(p.name,"overview-project-name",()=>guarded(()=>openProject(p))),element("p","muted",p.goal));
    const agents = p.agent_summary || {};
    const pending = [p.pending_confirmation && `${p.pending_confirmation} 项待确认`, agents.waiting && `${agents.waiting} 个角色等待`, agents.attention && `${agents.attention} 个角色需检查`].filter(Boolean);
    row.append(title, element("td","",projectStatus(p)), element("td","",pending.join(" · ") || "—"),
      element("td","",p.last_evidence_at ? time(p.last_evidence_at) : "—"));
    const actions = element("td");
    const remove = button("删除","text-button danger",()=>openDeleteWorkspace(p));
    remove.setAttribute("aria-label",`删除工作区 ${p.name}`);
    actions.append(remove);row.append(actions);
    return row;
  });
  if (!rows.length) {
    const row = element("tr"), cell = element("td","muted",projects.length ? "没有符合筛选条件的工作区。" : "还没有工作区。");
    cell.colSpan = 5;
    row.append(cell);
    rows.push(row);
  }
  $("workspaces-rows").replaceChildren(...rows);
}

function shellArg(value) {
  return "'" + String(value).replaceAll("'", "'\\''") + "'";
}

function renderProgress() {
  renderCompetition();
  const project = state.project;
  const observed = state.workspaceObservation;
  $("progress-observed").textContent = observed ? `状态观测于 ${new Date(observed).toLocaleString("zh-CN")}` : "正在读取角色状态…";
  const awaiting = state.graph.open_intents.filter(i => i.requires_confirmation && !i.confirmed_at);
  const facts = state.graph.facts.filter(f => f.id !== "goal" && f.evidence).slice(-5).reverse();
  const roles = state.agents.filter(a => !a.legacy);
  const concerns = roles.filter(a => a.observation?.status === "waiting" || a.observation?.attention);
  const signature = JSON.stringify([project.id, state.graphLoad, observed !== null, awaiting, facts,
    roles.map(a => [a.id,a.name,a.role_label,a.observation]),state.instance]);
  if (state.progressSignature === signature) return;
  state.progressSignature = signature;
  const details = record => {setTab("memory");openDetail(record);};
  const roleDetails = agent => {setTab("agents");agentTopology?.select(agent.id,true);};
  const recordRow = (record, label) => {
    const row = element("div","progress-row");
    row.append(button(record.title || record.description,"overview-project-name",()=>details(record)),element("small","muted",`${record.id} · ${label}`));
    return row;
  };
  const roleRow = agent => {
    const row = element("div","progress-row"), status = formatAgentStatus(agent);
    row.append(button(agent.name || agent.role_label,"overview-project-name",()=>roleDetails(agent)),element("small","muted",`${status} · ${agent.observation?.evidence || agent.role_label}`));
    return row;
  };
  const attention = [...awaiting.map(i=>recordRow(i,"待确认")), ...concerns.map(roleRow)];
  const graphMessage = state.graphLoad === "loading" ? "正在读取事实与意图…" : state.graphLoad === "error" ? "事实与意图暂不可用。" : "";
  $("progress-attention").replaceChildren(...(attention.length ? attention : [empty(graphMessage || "暂无待处理事项。") ]));
  $("progress-evidence").replaceChildren(...(facts.length ? facts.map(f=>recordRow(f,f.evidence.source || time(f.created_at))) : [empty(graphMessage || "暂无附带证据的事实。") ]));
  $("progress-agents").replaceChildren(...(roles.length ? roles.map(roleRow) : observed ? [empty("暂无登记角色。") ] : []));
  $("progress-cli-command").textContent = state.instance ? ["./sapperctl workspace-status", "--state", shellArg(state.instance.state),
    "--root", shellArg(state.instance.root), "--tmux-socket", shellArg(state.instance.tmux_socket || "default"), "--workspace", shellArg(project.id)].join(" ") : "";
}

async function refreshOverview(){
  if(state.view==="project")return;
  const navigation=state.navigation;
  const data=await api("/overview");
  if(state.view==="project"||navigation!==state.navigation)return;
  state.overview=data;state.projects=data.projects;renderProjects();
  renderUserInputRequests(data.agents);
  renderConnection(data.summary.active_agents);
  const signature=JSON.stringify(data);
  if(state.overviewSignature===signature)return;
  state.overviewSignature=signature;
  if (state.view === "workspaces") renderWorkspaceTable(data.projects);
  $("overview-summary").replaceChildren(...[[data.projects.filter(p=>workspaceAttention(p)>0).length,"需关注工作区"],[data.summary.workspaces,"工作区"],[data.summary.active_agents,"在线角色"],[data.summary.facts,"事实记录"]].map(([count,label])=>{
    const n=element("div","overview-stat");n.append(element("strong","",count),element("span","",label));return n;
  }));
  const projects=$("overview-projects");projects.replaceChildren();
  if(!data.projects.length)projects.append(empty("还没有工作区。打开已有题目目录，或新建一个任务。"));
  for(const p of [...data.projects].sort((a,b)=>workspaceAttention(b)-workspaceAttention(a)).slice(0,5)){
    const row=element("div","overview-project-row");
    const info=element("div","overview-project-info");info.append(button(p.name,"overview-project-name",()=>guarded(()=>openProject(p))),element("p","",p.goal),element("code","",p.path));
    const status=workspaceAttention(p)>0?`${projectStatus(p)} · 需关注`:projectStatus(p);
    const detail=element("div","overview-project-meta");detail.append(element("span","badge",status),element("span","",`${p.facts} 条事实 · ${p.open_intents} 条待办`));
    row.append(info,detail,button("打开工作区","text-button",()=>guarded(()=>openProject(p))));projects.append(row);
  }
  const agents=[...data.agents].sort((a,b)=>Number(!!b.pane_id)-Number(!!a.pane_id)||b.created_at.localeCompare(a.created_at))
    .filter((a,index)=>a.pane_id||index<6);
  $("overview-agents").replaceChildren(...(agents.length?agents.map(a=>{
    const row=element("div","overview-agent-row");
    const info=element("div","");const p=data.projects.find(p=>p.id===a.workspace_id)||data.projects.find(p=>a.workspace_scope?.includes(p.id));
    info.append(element("strong","",a.name||a.role_label),element("p","overview-assignment",`分配任务：${a.mission?.trim()||"未记录"}`));
    if(a.activity?.latest_message)info.append(element("p","overview-activity",`最近活动：${a.activity.latest_message}`));
    info.append(element("p","",`${a.role_label} · ${p?.name||"原工作区"} · ${a.profile_name}`),element("p","agent-context",contextStatus(a.shared_context)));
    row.append(info,element("span","badge",formatAgentStatus(a,data.panes)));
    if(p)row.append(button("查看角色","text-button",()=>guarded(async()=>{await openProject(p,"agents");agentTopology?.select(a.id,true);})));return row;
  }):[empty("暂无已登记的角色。进入工作区后启动 Claude Code 或派发协作角色。")])) ;
  $("overview-runs").replaceChildren(...(data.recent_runs.length?data.recent_runs.map(run=>{
    const row=element("div","overview-run-row"),p=data.projects.find(p=>p.id===run.project_id),info=element("div","");
    info.append(element("strong","",p?.name||(run.project_id==="global"?"全局知识库":"原工作区")),element("p","",`${roleNames[run.role]||run.native_operation||run.kind} · ${run.profile_name} · ${time(run.created_at)}`));
    row.append(info,element("span","badge",names[run.status]||run.status));
    row.append(button("查看输出","text-button",()=>guarded(async()=>{if(p){await openProject(p,"console");await openLog(run);}else await openNativeOutput(run);})));return row;
  }):[empty("运行记录会在启动角色或原命令后显示。")])) ;
  const c=data.context;
  $("overview-context-state").textContent=c.enabled?`已启用 · 版本 ${c.revision}`:c.revision?"已停用":"尚未配置";
  $("overview-context-description").textContent=c.enabled?[c.prompt_enabled?"启动时追加提示词":"",c.hook_enabled?"会话 hooks 检查更新":""].filter(Boolean).join("；"):"配置一次，统一提供给指挥官与各执行角色。";
  const env=data.environment,details=[["系统",`${env.system} / ${env.architecture}`],["Python",env.python],["tmux",env.tmux?"可用":"未安装"],["Claude Code",env.claude.available?"可用":"未找到或不可用"]];
  $("overview-machine").replaceChildren(...details.flatMap(([name,value])=>[element("dt","",name),element("dd","",value)]));
}

async function loadContextEditor(){
  const c=await api("/context");contextEditorRevision=c.revision;
  $("context-enabled").checked=c.enabled;$("context-prompt").checked=c.prompt_enabled;$("context-hook").checked=c.hook_enabled;
  $("context-content").value=c.content;$("context-revision").textContent=c.revision?`已保存 v${c.revision}`:"尚未配置";
}
async function openContext(){await loadContextEditor();$("context-dialog").showModal();}
$("home-button").onclick=$("breadcrumb-home").onclick=()=>guarded(showOverview);
$("workspaces-button").onclick=()=>guarded(()=>showOverview("workspaces"));
$("workspaces-bind").onclick=()=>guarded(openBind);
$("workspaces-search").oninput=$("workspaces-filter").onchange=()=>renderWorkspaceTable(state.overview?.projects||state.projects);
$("overview-bind").onclick=()=>guarded(openBind);
$("overview-create").onclick=()=>guarded(openCreateWorkspace);
$("context-button").onclick=$("overview-context-edit").onclick=()=>guarded(openContext);
$("context-reload").onclick=()=>guarded(loadContextEditor);
submit("context-form",async()=>{
  const result=await api("/context",{method:"POST",body:{expected_revision:contextEditorRevision,content:$("context-content").value,
    enabled:$("context-enabled").checked,prompt_enabled:$("context-prompt").checked,hook_enabled:$("context-hook").checked}});
  contextEditorRevision=result.revision;$("context-dialog").close();
  await refreshTask();toast(`全局配置 v${result.revision} 已保存；新会话及后续 hooks 将按配置加载`);
});
document.querySelectorAll("#context-dialog .dialog-close").forEach(n=>n.onclick=()=>n.closest("dialog").close());
