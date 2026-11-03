"use strict";

let contextEditorRevision=0;

function contextStatus(c){
  if(!c.latest_revision)return "共享环境未配置";
  if(!c.enabled)return "共享环境已停用";
  const seen=Math.max(c.launch_revision||0,c.hook_revision||0);
  return seen>=c.latest_revision?`共享环境 v${seen} · 已提供`:`共享环境 ${seen?"v"+seen:"未加载"} · v${c.latest_revision} 待更新`;
}

async function openProject(project,tab="workspace"){
  if(typeof project==="string")project=state.projects.find(p=>p.id===project);
  if(!project)throw new Error("工作区已不可用，请刷新概览");
  disconnectNative();
  state.view="project";state.project=project;state.folder=".";state.log=null;state.workspaceFilesPid=null;
  state.agentSignature=null;state.nativePaneSignature=null;
  localStorage.setItem("sapper-project",project.id);
  $("overview").hidden=true;$("empty-state").hidden=true;$("project-workspace").hidden=false;$("log-panel").hidden=true;
  $("home-button").classList.remove("active");
  renderProjects();await refreshTask();setTab(tab);
  if(tab==="files")await loadFiles();
}

async function showOverview(){
  disconnectNative();state.view="overview";state.project=null;state.log=null;
  $("project-workspace").hidden=true;$("empty-state").hidden=true;$("overview").hidden=false;
  $("home-button").classList.add("active");$("breadcrumb").textContent="总体概览";
  renderProjects();await refreshOverview();
}

async function refreshOverview(){
  if(state.view!=="overview")return;
  const data=await api("/overview");
  if(state.view!=="overview")return;
  state.overview=data;state.projects=data.projects;renderProjects();
  $("breadcrumb").textContent="总体概览";
  const signature=JSON.stringify(data);
  if(state.overviewSignature===signature)return;
  state.overviewSignature=signature;
  $("overview-summary").replaceChildren(...[[data.summary.workspaces,"工作区"],[data.summary.active_agents,"活动角色"],[data.summary.open_intents,"待办意图"],[data.summary.facts,"事实记录"]].map(([count,label])=>{
    const n=element("div","overview-stat");n.append(element("strong","",count),element("span","",label));return n;
  }));
  const projects=$("overview-projects");projects.replaceChildren();
  if(!data.projects.length)projects.append(empty("还没有工作区。打开已有题目目录，或新建一个任务。"));
  for(const p of data.projects){
    const row=element("div","overview-project-row");
    const info=element("div","overview-project-info");info.append(button(p.name,"overview-project-name",()=>guarded(()=>openProject(p))),element("p","",p.goal),element("code","",p.path));
    const status=p.status==="completed"?"已完成":!p.enabled?"待接入":p.active_agents?`${p.active_agents} 个活动角色`:"暂无活动角色";
    const detail=element("div","overview-project-meta");detail.append(element("span","badge",status),element("span","",`${p.facts} 条事实 · ${p.open_intents} 条待办`));
    row.append(info,detail,button("打开工作区","text-button",()=>guarded(()=>openProject(p))));projects.append(row);
  }
  const agents=[...data.agents].sort((a,b)=>Number(!!b.pane_id)-Number(!!a.pane_id)||b.created_at.localeCompare(a.created_at)).slice(0,6);
  $("overview-agents").replaceChildren(...(agents.length?agents.map(a=>{
    const row=element("div","overview-agent-row");
    const info=element("div","");const p=data.projects.find(p=>p.id===a.workspace_id)||data.projects.find(p=>a.workspace_scope?.includes(p.id));
    info.append(element("strong","",a.name||a.role_label),element("p","",`${a.role_label} · ${p?.name||"原工作区"} · ${a.profile_name}`),element("p","agent-context",contextStatus(a.shared_context)));
    row.append(info,element("span","badge",formatAgentStatus(a)));
    if(p)row.append(button("查看","text-button",()=>guarded(()=>openProject(p))));return row;
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
$("home-button").onclick=()=>guarded(showOverview);
$("overview-bind").onclick=()=>guarded(openBind);
$("overview-create").onclick=()=>$("create-dialog").showModal();
$("context-button").onclick=$("overview-context-edit").onclick=()=>guarded(openContext);
$("context-reload").onclick=()=>guarded(loadContextEditor);
submit("context-form",async()=>{
  const result=await api("/context",{method:"POST",body:{expected_revision:contextEditorRevision,content:$("context-content").value,
    enabled:$("context-enabled").checked,prompt_enabled:$("context-prompt").checked,hook_enabled:$("context-hook").checked}});
  contextEditorRevision=result.revision;$("context-dialog").close();
  await refreshTask();toast(`全局配置 v${result.revision} 已保存；新会话及后续 hooks 将按配置加载`);
});
document.querySelectorAll("#context-dialog .dialog-close").forEach(n=>n.onclick=()=>n.closest("dialog").close());
