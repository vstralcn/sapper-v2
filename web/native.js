"use strict";

let nativeCatalog,nativePreview,nativeConfigVersion,nativeConfigProject,nativeConfigPath;
let nativeCommand,nativeGroup,nativePageScope;
let nativePreset={},nativeOutputGeneration=0,nativePreviewRevision=0;
const nativeControls=new Map();
const nativeGroups = [
  {label:"事实与意图", names:["status","graph","validate","frontier","pick","fact","intent","hint","show","doc","recall","claim","release","done","complete","confirm","brainstorm-pick"]},
  {label:"调度与推进", names:["dispatch","auto","command","fleet","sweep","family"]},
  {label:"角色通信", names:["peers","send","capture"]},
  {label:"接入与撤销", names:["init","setup","teardown"]},
  {label:"Flag 记账", names:["flag"]},
  {label:"其他", names:["view","kb"]},
];
const nativeActionNames={add:"新增",search:"搜索",show:"查看详情",list:"列出",import:"导入 Markdown",root:"查看根目录",
  init:"初始化",up:"启动编队",down:"停止编队",status:"查看状态",hire:"派发原 auto worker",fire:"停止原 worker",workers:"列出 workers"};

function invalidateNative(){
  nativePreviewRevision++;
  nativePreview=null;
  $("native-execute").disabled=true;
  $("native-execute").hidden=true;
  $("native-preview").hidden=true;
}
function nativeCommands(group){
  const catalog = nativeCatalog.commands;
  const commands = group.names.map(name => catalog.find(c => c.name === name)).filter(Boolean);
  if(group === nativeGroups.at(-1)){
    commands.push(...catalog.filter(c => !nativeGroups.some(g => g.names.includes(c.name))));
  }
  return commands;
}
function renderNativeCategories(){
  nativeGroup = null;
  $("native-categories").replaceChildren(...nativeGroups.flatMap(group => {
    const commands = nativeCommands(group);
    if(!commands.length)return [];
    const item = button(group.label, "", () => {
      if(nativeGroup !== group)selectNativeCommand(commands[0].name);
    });
    item.dataset.category = group.label;
    item.append(element("span","",commands.length));
    return [item];
  }));
}
function selectNativeCommand(operation,preset={},preferred=$("native-project").value){
  nativeCommand = nativeCatalog.commands.find(command => command.name === operation);
  const group = nativeGroups.find(g => g.names.includes(operation)) || nativeGroups.at(-1);
  if(nativeGroup !== group){
    nativeGroup = group;
    $("native-categories").querySelectorAll("button").forEach(item => {
      item.setAttribute("aria-pressed",String(item.dataset.category === group.label));
    });
    $("native-commands").replaceChildren(...nativeCommands(group).map(command => {
      const item = button("", "", () => {
        if(nativeCommand !== command)selectNativeCommand(command.name);
      });
      item.dataset.operation = command.name;
      item.append(element("span","",command.label),element("code","",command.name));
      return item;
    }));
    $("native-commands").scrollTop = 0;
  }
  $("native-commands").querySelectorAll("button").forEach(item => {
    if(item.dataset.operation === operation)item.setAttribute("aria-current","true");
    else item.removeAttribute("aria-current");
  });
  nativePreset = {...nativeDefaults(operation),...preset};
  updateNativeProjects(preferred);
  renderNativeFields();
}
function nativeDefaults(operation){
  if(operation==="kb")return {action:"list"};
  if(operation==="dispatch")return {target:"reason"};
  if(["command","sweep"].includes(operation))return {root:nativeCatalog.roots[0]};
  return {};
}
function resetNativeConfig(){
  $("native-config-content").value="";
  $("native-config-content").disabled=true;
  $("native-config-save").disabled=true;
}
function updateNativeProjects(preferred){
  const select=$("native-project");
  const previous=select.value;
  const locked=select.disabled;
  const projects=locked?[state.project]:state.projects;
  const nodes=projects.map(p=>new Option(p.name,p.id));
  if(nativeCommand.name==="kb"&&!locked)nodes.unshift(new Option("全局知识库","global"));
  select.replaceChildren(...(nodes.length?nodes:[new Option("暂无工作区","")]));
  if(nodes.some(n=>n.value===preferred))select.value=preferred;
  $("native-preview-button").disabled=!select.value;
  $("native-workbench").querySelector(".native-config").hidden=!select.value||select.value==="global";
  if(select.value!==previous)resetNativeConfig();
}
function refreshNativeProfiles(){
  const selected=$("native-profile").value;
  $("native-profile").replaceChildren(...$("run-profile").cloneNode(true).children);
  if(Array.from($("native-profile").options).some(option=>option.value===selected))$("native-profile").value=selected;
  if($("native-profile").value!==selected)invalidateNative();
}
function mountNativeWorkbench(container,operation,preset,project,locked){
  container.append($("native-workbench"));
  $("native-project").disabled=locked;
  $("native-profile").value="";
  refreshNativeProfiles();
  renderNativeCategories();
  selectNativeCommand(operation,preset,project);
  resetNativeConfig();
}
async function updateNativePage(){
  const pid=state.view==="project"&&state.tab==="native"?state.project.id:null;
  const key=pid||(state.view==="native"?"global":null);
  $("tab-native").hidden=!key;
  $("native-page-title").hidden=!!pid;
  if(key===(nativePageScope?.key??null))return;
  const scope=key?{key}:null;
  nativePageScope=scope;
  if(!$("native-dialog").open)invalidateNative();
  if(!scope)return;
  const loading=$("native-loading"),content=$("native-page-content");
  loading.textContent="正在读取命令目录…";
  loading.hidden=false;content.hidden=true;
  try{
    nativeCatalog ??= await api("/native/catalog");
  }catch(error){
    if(nativePageScope!==scope)return;
    nativePageScope=null;
    loading.textContent="命令目录读取失败，请重新打开原生功能。";
    throw error;
  }
  if(nativePageScope!==scope||$("native-dialog").open)return;
  mountNativeWorkbench(content,"status",{},pid,!!pid);
  loading.hidden=true;content.hidden=false;
}
async function openNative(operation="status",preset={}){
  const navigation=state.navigation;
  nativeCatalog ??= await api("/native/catalog");
  if(!state.csrf||navigation!==state.navigation)return;
  mountNativeWorkbench($("native-dialog"),operation,preset,operation==="kb"?"global":state.project?.id,false);
  if(!$("native-dialog").open)$("native-dialog").showModal();
}
function renderNativeFields(){
  invalidateNative();nativeControls.clear();
  const spec=nativeCommand,op=spec.name;
  $("native-command-name").textContent=op;
  $("native-command-title").textContent=spec.label;
  $("native-parameter-count").textContent=`${spec.fields.length} 个参数`;
  const help=$("native-command-help");
  help.textContent=op==="flag"?"仅记入本地 Flag 台账。":op==="fleet"?"先准备 fleet.json。独立编队通过 fleet down 停止。":op==="teardown"?"撤销项目接入。选择 purge 会永久删除图数据。":spec.help;
  help.hidden=!help.textContent;
  const fields=$("native-fields");fields.replaceChildren();
  for(const f of spec.fields){
    const label=element("label","native-field"),heading=element("span","native-field-label",f.label);
    if(f.required)heading.append(element("span","native-field-tag required","必填"));
    if(f.repeated)heading.append(element("span","native-field-tag","可重复"));
    label.append(heading);
    let input;
    if(f.type==="boolean"){
      input=document.createElement("input");input.type="checkbox";input.checked=nativePreset[f.name]??!!f.default;
      label.classList.add("native-check");
    }else if(f.choices){
      input=document.createElement("select");
      if(!f.required){const n=element("option","",f.default==null?"使用原默认值":`默认：${f.default}`);n.value="";input.append(n);}
      for(const choice of f.choices){const n=element("option","",f.name==="action"?nativeActionNames[choice]||choice:choice);n.value=choice;input.append(n);}
      if(nativePreset[f.name]!=null)input.value=nativePreset[f.name];
    }else{
      const multiline=f.repeated||["description","doc","detail","message","set","note"].includes(f.name);
      input=document.createElement(multiline?"textarea":"input");
      if(multiline)input.rows=["doc","detail","set"].includes(f.name)?2:1;
      else input.type=f.type==="integer"?"number":"text";
      input.placeholder=f.default!=null?`默认：${f.default}`:f.repeated?"每行填写一项":f.flag||"";
      if(nativePreset[f.name]!=null)input.value=Array.isArray(nativePreset[f.name])?nativePreset[f.name].join("\n"):nativePreset[f.name];
      if(op==="dispatch"&&f.name==="target"){
        const list=element("datalist","");list.id="native-dispatch-targets";
        for(const value of ["reason","verify","brainstorm","blindspot",...(state.graph?.open_intents||[]).map(i=>i.id)]){const option=element("option","");option.value=value;list.append(option);}
        label.append(list);input.setAttribute("list",list.id);
      }
    }
    input.required=f.required&&f.type!=="boolean";
    input.id="native-field-"+f.name;input.name=f.name;
    input.addEventListener("input",invalidateNative);input.addEventListener("change",invalidateNative);
    label.append(input);
    if(f.help)label.append(element("small","field-help",f.help));
    fields.append(label);nativeControls.set(f.name,{field:f,input});
  }
  updateNativePermissionWarning();
}
function updateNativePermissionWarning(){
  const op=nativeCommand.name;
  const skip=nativeControls.get("skip_permissions")?.input.checked;
  const dry=nativeControls.get("dry_run")?.input.checked;
  const target=nativeControls.get("target")?.input.value.trim()||"";
  const worker=op==="auto"||op==="dispatch"&&/^i\d+$/.test(target);
  const warning=AgentTopologyModel.permissionWarning(skip,worker&&!dry);
  $("native-permission-warning").textContent=warning;
  $("native-permission-warning").hidden=!warning;
}
function nativeRequest(){
  const values={};
  for(const [name,{field:f,input}] of nativeControls){
    if(f.type==="boolean"){if(input.checked)values[name]=true;continue;}
    if(input.value==="")continue;
    values[name]=f.repeated&&f.flag?input.value.split("\n").filter(s=>s.trim()):input.value;
  }
  return {operation:nativeCommand.name,values,profile_id:$("native-profile").value};
}
async function previewNative(){
  if(!$("native-form").reportValidity())return;
  invalidateNative();
  const revision=nativePreviewRevision;
  const pid=$("native-project").value,request=nativeRequest();
  const p=await api(`/native/${pid}/preview`,{method:"POST",body:request});
  if(revision!==nativePreviewRevision)return;
  nativePreview={...p,request,pid,request_id:crypto.randomUUID()};
  $("native-preview-cwd").textContent="工作目录："+p.cwd+(p.profile?` · 模型配置：${p.profile.name} / ${p.profile.model}`:" · 继承原环境");
  $("native-preview-command").textContent=p.command;
  $("native-preview-fleet").hidden=!p.fleet_config;
  $("native-preview-fleet").textContent=p.fleet_config?JSON.stringify(p.fleet_config,null,2):"";
  $("native-preview-note").textContent=p.destructive?"此操作会覆盖数据、撤销接入或停止指定编队。执行时需确认以上目标。":"执行原命令后可查看输出与退出码。LLM 命令使用原循环及权限选项，dry-run 可先检查提示词。";
  $("native-preview").hidden=false;$("native-execute").hidden=false;$("native-execute").disabled=false;
}
async function openNativeOutput(run){
  const generation=++nativeOutputGeneration;$("native-output-text").textContent="正在读取原命令输出…";
  $("native-output-download").hidden=false;$("native-output-download").href=`api/runs/${run.id}/log?download=1`;
  if(!$("native-output-dialog").open)$("native-output-dialog").showModal();
  for(let tries=0;tries<60&&$("native-output-dialog").open&&generation===nativeOutputGeneration;tries++){
    const [log,runs]=await Promise.all([api(`/runs/${run.id}/log`),api("/runs")]);
    if(generation!==nativeOutputGeneration)return;
    const current=runs.find(r=>r.id===run.id);
    $("native-output-text").textContent=log.text+(current?`\n\n状态：${names[current.status]||current.status}${current.exit_code!=null?" · 退出码 "+current.exit_code:""}`:"");
    if(current&&["exited","failed","stopped","interrupted"].includes(current.status))return;
    await new Promise(resolve=>setTimeout(resolve,300));
  }
}
async function loadNativeConfig(){
  const pid=$("native-project").value,path=$("native-config-path").value;
  const r=await api(`/native/${pid}/config?path=${encodeURIComponent(path)}`);
  nativeConfigVersion=r.version;nativeConfigProject=pid;nativeConfigPath=path;
  $("native-config-content").value=r.content;$("native-config-content").disabled=false;$("native-config-save").disabled=false;
  toast(r.exists?"已读取工作区配置":"显示原模板／示例，保存后才写入工作区");
}
$("native-project").onchange=()=>{
  invalidateNative();
  resetNativeConfig();
  $("native-workbench").querySelector(".native-config").hidden=$("native-project").value==="global";
};
$("native-profile").onchange=invalidateNative;
$("native-fields").addEventListener("input",updateNativePermissionWarning);
$("native-fields").addEventListener("change",updateNativePermissionWarning);
$("native-preview-button").onclick=()=>guarded(previewNative);
$("native-reset").onclick=renderNativeFields;
$("native-button").onclick=()=>guarded(()=>showOverview("native"));
$("workspace-native").onclick=()=>{setTab("native");$("native-tab").focus();};
$("native-dialog").addEventListener("close",()=>{
  invalidateNative();
  nativePageScope=null;
  if(state.csrf&&!$("tab-native").hidden)guarded(updateNativePage);
});
$("native-config-load").onclick=()=>guarded(loadNativeConfig);
$("native-config-path").onchange=()=>{$("native-config-save").disabled=true;$("native-config-content").disabled=true;};
$("native-config-save").onclick=()=>guarded(async()=>{
  if(nativeConfigProject!==$("native-project").value||nativeConfigPath!==$("native-config-path").value)throw new Error("请先读取当前工作区的配置");
  const r=await api(`/native/${nativeConfigProject}/config`,{method:"POST",body:{path:nativeConfigPath,content:$("native-config-content").value,version:nativeConfigVersion}});
  nativeConfigVersion=r.version;invalidateNative();toast("已保存工作区原生配置");
});
submit("native-form",async()=>{
  const p=nativePreview;if(!p)throw new Error("请先预览原命令");
  if(p.destructive&&!await confirmAction(`确认执行：\n${p.command}\n工作目录：${p.cwd}\n该操作可能停止会话、覆盖或删除图数据。`))return;
  const r=await api(`/native/${p.pid}/execute`,{method:"POST",body:{...p.request,preview_token:p.preview_token,acknowledged:p.destructive,request_id:p.request_id}});
  invalidateNative();
  if($("native-dialog").open)$("native-dialog").close();
  if(p.pid==="global"){await openNativeOutput(r);if(state.view==="overview")await refreshOverview();}
  else{await refreshProjects();await openProject(p.pid,"console");await openLog(r);}
  toast("原命令已启动，可查看输出和退出码");
});

const originalRecordNode=recordNode;
recordNode=function(record,intent){
  const node=originalRecordNode(record,intent);
  if(intent&&!record.to){
    const actions=element("div","intent-native-actions");
    function action(label,fn){actions.append(button(label,"text-button",event=>{event.stopPropagation();guarded(fn);}));}
    action(record.worker?"释放":"领取",()=>openIntentAction(record.worker?"release":"claim",record));
    action("记录完成",()=>openIntentAction("done",record));
    action("原执行",()=>openNative("dispatch",{target:record.id,claim:true}));
    if(record.requires_confirmation&&(record.doc||"").includes("[brainstorm batch:"))action("选此方案",async()=>{
      if(!await confirmAction(`选择 ${record.id}，确认此方案并放弃同一 brainstorm 批次的其他方案？`))return;
      await taskApi("brainstorm-pick",{method:"POST",body:{id:record.id}});await refreshTask();
    });
    node.append(actions);
  }
  return node;
};
function openIntentAction(action,record){
  const form=$("intent-action-form");form.reset();form.elements.action.value=action;form.elements.id.value=record.id;
  form.elements.worker.value=record.worker||"web-user";
  $("intent-action-title").textContent=`${{claim:"领取",release:"释放",done:"完成"}[action]}意图 ${record.id}`;
  $("intent-result-fields").hidden=action!=="done";form.elements.description.required=action==="done";
  $("intent-action-dialog").showModal();
}
submit("intent-action-form",async fd=>{
  const data=Object.fromEntries(fd),kind=data.kind;delete data.action;
  data.kind=kind||null;data.data={};
  for(const line of (fd.get("data")||"").split("\n").filter(s=>s.trim())){const at=line.indexOf("=");if(at<1)throw new Error("结构化数据每行需要 key=value");data.data[line.slice(0,at).trim()]=line.slice(at+1);}
  await taskApi(fd.get("action"),{method:"POST",body:data});$("intent-action-dialog").close();await refreshTask();toast("已通过原 Store 更新意图");
});
submit("recall-form",async()=>{
  const pid=state.project.id,matches=await taskApi("recall?q="+encodeURIComponent($("recall-query").value));if(pid!==state.project?.id)return;
  $("recall-results").hidden=false;
  $("recall-results").replaceChildren(...(matches.length?matches.map(m=>button(`${m.id} · ${m.title||m.description} · 相似度 ${m.score}`,"text-button",()=>guarded(async()=>{
    await refreshTask();const record=[...state.graph.facts,...state.graph.intents].find(n=>n.id===m.id);if(record)openDetail(record);
  }))):[empty("原生 recall 未找到匹配记录。")])) ;
});
$("native-validate").onclick=()=>guarded(async()=>{
  const r=await taskApi("validate");$("native-output-text").textContent=r.valid?"原生校验通过：图结构、引用和项目状态一致。":r.issues.join("\n");
  $("native-output-download").hidden=true;$("native-output-dialog").showModal();
});
$("native-frontier").onclick=()=>guarded(async()=>{const rows=await taskApi("frontier");$("native-output-text").textContent=rows.length?rows.map(i=>`${i.id} · ${i.title||i.description} · ${i.worker||"未领取"}`).join("\n"):"原生 frontier：当前没有就绪意图。";$("native-output-download").hidden=true;$("native-output-dialog").showModal();});
$("native-brainstorm").onclick=()=>guarded(()=>openNative("dispatch",{target:"brainstorm",max_options:4}));
$("native-complete").onclick=()=>guarded(()=>openNative("complete",{worker:"web-user"}));
function nativeKnowledge(action){$("knowledge-dialog").close();return openNative("kb",{action});}
$("kb-native-add").onclick=()=>guarded(()=>nativeKnowledge("add"));
$("kb-native-import").onclick=()=>guarded(()=>nativeKnowledge("import"));
$("kb-native-all").onclick=()=>guarded(()=>nativeKnowledge("list"));
document.querySelectorAll("#native-dialog .dialog-close,#intent-action-dialog .dialog-close,#native-output-dialog .dialog-close").forEach(n=>n.onclick=()=>n.closest("dialog").close());
