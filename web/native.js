"use strict";

let nativeCatalog,nativePreview,nativeConfigVersion,nativeConfigProject,nativeConfigPath;
let nativePreset={},nativeOutputGeneration=0;
const nativeControls=new Map();
const nativeActionNames={add:"新增",search:"搜索",show:"查看详情",list:"列出",import:"导入 Markdown",root:"查看根目录",
  init:"初始化",up:"启动编队",down:"停止编队",status:"查看状态",hire:"派发原 auto worker",fire:"停止原 worker",workers:"列出 workers"};

function invalidateNative(){nativePreview=null;$("native-execute").disabled=true;$("native-preview").hidden=true;}
function updateNativeProjects(preferred){
  const op=$("native-operation").value;
  const nodes=state.projects.map(p=>{const n=element("option","",p.name);n.value=p.id;return n;});
  if(op==="kb"){const n=element("option","","全局知识库（无需工作区）");n.value="global";nodes.unshift(n);}
  $("native-project").replaceChildren(...nodes);
  if(nodes.some(n=>n.value===preferred))$("native-project").value=preferred;
  $("native-preview-button").disabled=!nodes.length;
  $("native-dialog").querySelector(".native-config").hidden=$("native-project").value==="global"||!nodes.length;
}
async function openNative(operation="status",preset={}){
  nativeCatalog=await api("/native/catalog");nativePreset=preset;
  if(["command","sweep"].includes(operation)&&nativePreset.root==null)nativePreset.root=nativeCatalog.roots[0];
  $("native-operation").replaceChildren(...nativeCatalog.commands.map(c=>{const n=element("option","",`${c.label} · ${c.name}`);n.value=c.name;return n;}));
  $("native-operation").value=operation;
  $("native-profile").replaceChildren(...$("run-profile").cloneNode(true).children);
  updateNativeProjects(operation==="kb"?"global":state.project?.id||state.projects[0]?.id);
  renderNativeFields();
  $("native-config-content").value="";$("native-config-content").disabled=true;$("native-config-save").disabled=true;
  $("native-dialog").showModal();
}
function renderNativeFields(){
  invalidateNative();nativeControls.clear();const op=$("native-operation").value;
  const spec=nativeCatalog.commands.find(c=>c.name===op);
  $("native-command-help").textContent=op==="flag"?"只记入原本地 Flag 台账，不提交比赛平台。":op==="fleet"?"原 tmux 编队：先准备 fleet.json，再执行 up／hire／status／down。原编队独立会话需通过 fleet down 停止。":op==="teardown"?"撤销原项目接入；默认保留图，purge 会永久删除图数据。建议先停止相关运行。":spec.help;
  const fields=$("native-fields");fields.replaceChildren();
  for(const f of spec.fields){
    const label=element("label","native-field"),heading=element("span","",f.label+(f.required?" *":""));
    if(f.flag)heading.append(element("code","",f.flag));label.append(heading);
    let input;
    if(f.type==="boolean"){
      input=document.createElement("input");input.type="checkbox";input.checked=nativePreset[f.name]??!!f.default;
      label.classList.add("native-check");label.prepend(input);
    }else if(f.choices){
      input=document.createElement("select");
      if(!f.required){const n=element("option","",f.default==null?"使用原默认值":`默认：${f.default}`);n.value="";input.append(n);}
      for(const choice of f.choices){const n=element("option","",f.name==="action"?nativeActionNames[choice]||choice:choice);n.value=choice;input.append(n);}
      if(nativePreset[f.name]!=null)input.value=nativePreset[f.name];
    }else{
      const multiline=f.repeated||["description","doc","detail","message","set","note"].includes(f.name);
      input=document.createElement(multiline?"textarea":"input");
      if(multiline){input.rows=["doc","detail","set"].includes(f.name)?5:3;label.classList.add("native-wide");}
      else input.type=f.type==="integer"?"number":"text";
      input.placeholder=f.default==null?"":`原默认值：${f.default}`;
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
    if(f.type!=="boolean")label.append(input);
    label.append(element("small","field-help",f.help));fields.append(label);nativeControls.set(f.name,{field:f,input});
  }
  nativePreset={};
  updateNativePermissionWarning();
}
function updateNativePermissionWarning(){
  const op=$("native-operation").value;
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
  return {operation:$("native-operation").value,values,profile_id:$("native-profile").value};
}
async function previewNative(){
  if(!$("native-form").reportValidity())return;
  const pid=$("native-project").value,request=nativeRequest();
  const p=await api(`/native/${pid}/preview`,{method:"POST",body:request});
  nativePreview={...p,request,pid,request_id:crypto.randomUUID()};
  $("native-preview-cwd").textContent="工作目录："+p.cwd+(p.profile?` · 模型配置：${p.profile.name} / ${p.profile.model}`:" · 继承原环境");
  $("native-preview-command").textContent=p.command;
  $("native-preview-fleet").hidden=!p.fleet_config;
  $("native-preview-fleet").textContent=p.fleet_config?JSON.stringify(p.fleet_config,null,2):"";
  $("native-preview-note").textContent=p.destructive?"此操作会覆盖数据、撤销接入或停止指定编队。执行时需确认以上目标。":"执行原命令后可查看输出与退出码。LLM 命令使用原循环及权限选项，dry-run 可先检查提示词。";
  $("native-preview").hidden=false;$("native-execute").disabled=false;
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
$("native-operation").onchange=()=>{const op=$("native-operation").value;nativePreset=op==="kb"?{action:"list"}:op==="dispatch"?{target:"reason"}:["command","sweep"].includes(op)?{root:nativeCatalog.roots[0]}:{};updateNativeProjects($("native-project").value);renderNativeFields();};
$("native-project").onchange=()=>{invalidateNative();$("native-dialog").querySelector(".native-config").hidden=$("native-project").value==="global";$("native-config-save").disabled=true;$("native-config-content").disabled=true;};
$("native-profile").onchange=invalidateNative;
$("native-fields").addEventListener("input",updateNativePermissionWarning);
$("native-fields").addEventListener("change",updateNativePermissionWarning);
$("native-preview-button").onclick=()=>guarded(previewNative);
$("native-button").onclick=$("workspace-native").onclick=()=>guarded(()=>openNative());
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
  $("native-dialog").close();
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
