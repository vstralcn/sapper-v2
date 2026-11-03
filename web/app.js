"use strict";

const $ = (id) => document.getElementById(id);
const state = {csrf: "", projects: [], project: null, graph: null, profiles: [], panes: [],
  runs: [], view: "overview", tab: "memory", folder: ".", log: null, logOffset: 0, detail: null, polling: false};
const names = {starting: "启动中", running: "运行中", stopping: "正在停止", timed_out: "预算耗尽", exited: "已退出", stopped: "已停止", failed: "启动失败", interrupted: "状态中断"};
let toastTimer;

function element(tag, classes, text) {
  const node = document.createElement(tag);
  if (classes) node.className = classes;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function button(text, classes, action) {
  const node = element("button", classes || "text-button", text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function empty(text) { return element("p", "empty-list", text); }
function time(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"});
}
function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 6500 : 3800);
}
function confirmAction(message) {
  $("confirm-message").textContent = message;
  return new Promise(resolve => {
    const finish = accepted => {
      $("confirm-dialog").close();
      resolve(accepted);
    };
    $("confirm-cancel").onclick = () => finish(false);
    $("confirm-accept").onclick = () => finish(true);
    $("confirm-dialog").oncancel = event => { event.preventDefault(); finish(false); };
    $("confirm-dialog").showModal();
  });
}
async function api(path, options = {}) {
  const headers = {...options.headers};
  if (options.body && typeof options.body !== "string" && !(options.body instanceof ArrayBuffer)) {
    options.body = JSON.stringify(options.body);
    headers["Content-Type"] = "application/json";
  }
  if (options.method && options.method !== "GET") headers["X-Sapper-CSRF"] = state.csrf;
  const response = await fetch("api" + path, {...options, headers, credentials: "same-origin", cache: "no-store"});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) showLogin();
    throw new Error(data.error || `请求失败 (${response.status})`);
  }
  return data;
}
function taskApi(action, options = {}) {
  if (!state.project) throw new Error("请先选择任务");
  return api(`/projects/${state.project.id}/${action}`, options);
}
function showLogin() {
  state.csrf = "";
  $("workspace").hidden = true;
  $("login").hidden = false;
  if (typeof disconnectNative === "function") disconnectNative();
}
async function showWorkspace() {
  state.view = "overview";
  state.project = null;
  $("login").hidden = true;
  $("workspace").hidden = false;
  const [health, profiles] = await Promise.all([api("/health"), api("/profiles")]);
  state.profiles = profiles;
  $("root-path").textContent = health.root;
  $("environment-status").textContent = health.tmux ? "tmux 可用 · 本地执行" : "未安装 tmux";
  $("environment-status").title = health.root;
  refreshProfiles();
  await refreshProjects();
}

async function refreshProjects() {
  const projects = await api("/projects");
  state.projects = projects;
  const previous = state.project?.id;
  state.project = state.view === "project" ? projects.find(p => p.id === previous) || projects[0] || null : null;
  renderProjects();
  $("empty-state").hidden = true;
  $("overview").hidden = !!state.project;
  $("project-workspace").hidden = !state.project;
  $("home-button").classList.toggle("active", !state.project);
  if (state.project) await refreshTask();
  else {state.view = "overview"; await refreshOverview();}
}
function renderProjects() {
  const signature = JSON.stringify([state.project?.id, state.projects]);
  if (signature === state.projectListSignature) return;
  state.projectListSignature = signature;
  const list = $("project-list");
  list.replaceChildren();
  if (!state.projects.length) list.append(element("p", "sidebar-empty", "还没有任务。新建任务后，就可以在这里查看和操作。"));
  for (const project of state.projects) {
    const item = button("", "project-item" + (project.id === state.project?.id ? " active" : ""), async () => {
      await guarded(() => openProject(project));
    });
    item.append(element("span", "task-title", project.goal), element("span", "task-meta", `${project.name}  /  ${project.facts} 条事实`));
    item.title = project.goal;
    list.append(item);
  }
}
async function refreshTask() {
  if (state.view === "overview") return refreshOverview();
  const pid = state.project?.id;
  if (!pid) return;
  const [graph, panes, runs] = await Promise.all([taskApi("graph"), api("/panes"), api(`/runs?project=${pid}`)]);
  if (pid !== state.project?.id) return;
  state.graph = graph;
  const project = state.projects.find(p => p.id === pid);
  if (project) {
    const count = graph.facts.filter(f => f.id !== "goal").length;
    if (project.facts !== count) {project.facts = count; renderProjects();}
  }
  state.panes = panes.filter(p => p.project_id === pid || p.role === "commander");
  state.runs = runs;
  $("breadcrumb").textContent = state.project.name;
  $("project-name").textContent = state.project.name;
  $("project-goal").textContent = graph.project.goal;
  $("project-path").textContent = state.project.path;
  $("project-status").textContent = graph.project.status === "completed" ? "目标已完成" : "进行中";
  $("fact-count").textContent = graph.facts.filter(f => f.id !== "goal").length;
  $("intent-count").textContent = graph.open_intents.length;
  $("hint-count").textContent = graph.hints.length;
  $("pane-count").textContent = state.panes.filter(p => !p.dead).length;
  const memorySignature = pid + JSON.stringify(graph);
  if (state.memorySignature !== memorySignature) {
    state.memorySignature = memorySignature;
    renderMemory();
  }
  renderPanes();
  renderRuns();
}
function renderMemory() {
  const {facts, intents, hints} = state.graph;
  const records = facts.filter(f => f.id !== "goal").reverse();
  $("facts-total").textContent = records.length;
  $("facts-list").replaceChildren(...(records.length ? records.map(f => recordNode(f, false)) : [empty("记录已确认的观察，让下一步有据可循。")])) ;
  const ordered = [...intents].sort((a, b) => Number(!!a.to) - Number(!!b.to));
  $("intents-list").replaceChildren(...(ordered.length ? ordered.map(i => recordNode(i, true)) : [empty("还没有执行意图。可手动添加，也可让原 agent 规划。")])) ;
  $("hints-list").replaceChildren(...hints.slice(-10).map(h => {
    const node = element("div", "hint");
    node.append(element("p", "", h.content), element("span", "", `${h.id} · ${h.creator} · ${time(h.created_at)}`));
    return node;
  }));
}
function recordNode(record, intent) {
  const node = element("article", "record");
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  const top = element("div", "record-top");
  top.append(element("span", "node-id", record.id));
  if (record.kind) top.append(element("span", "kind-label", record.kind));
  if (intent) top.append(element("span", "badge" + (record.to ? " neutral" : ""), record.to ? "已结束" : record.requires_confirmation && !record.confirmed_at ? "待确认" : "待执行"));
  node.append(top, element("div", "record-title", record.title || record.description));
  if (record.title) node.append(element("p", "record-description", record.description));
  const foot = element("div", "record-footer");
  foot.append(element("span", "", intent ? `${record.from.join(", ")} ${record.to ? "→ " + record.to : ""}` : `${record.creator} · ${time(record.created_at)}`));
  if (intent && record.requires_confirmation && !record.confirmed_at && !record.to) {
    foot.append(button("确认意图", "text-button", async event => {
      event.stopPropagation();
      await guarded(async () => {
        await taskApi("confirm", {method: "POST", body: {id: record.id}});
        toast("已通过原接口确认意图");
        await refreshTask();
      });
    }));
  }
  node.append(foot);
  node.addEventListener("click", () => openDetail(record));
  node.addEventListener("keydown", event => { if (event.key === "Enter") openDetail(record); });
  return node;
}
function openDetail(record) {
  state.detail = record;
  $("detail-title").textContent = record.title || record.id;
  $("detail-meta").textContent = `${record.id} · ${record.creator} · ${time(record.created_at)}`;
  $("detail-description").textContent = record.description;
  $("detail-doc").value = record.doc || "";
  $("detail-dialog").showModal();
}
function openRecord(action) {
  $("record-form").reset();
  $("record-form").elements.action.value = action;
  $("record-heading").textContent = action === "fact" ? "添加已确认的事实" : "添加执行意图";
  $("record-from-label").hidden = action !== "intent";
  $("record-confirm-label").hidden = action !== "intent";
  $("record-kind-label").hidden = action !== "fact";
  $("record-from").replaceChildren(...state.graph.facts.filter(f => f.id !== "goal").map(f => {
    const option = element("option", "", `${f.id} · ${f.title || f.description}`);
    option.value = f.id;
    return option;
  }));
  $("record-dialog").showModal();
}
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll("[data-tab]").forEach(node => {
    const active = node.dataset.tab === tab;
    node.classList.toggle("active", active);
    node.setAttribute("aria-selected", String(active));
  });
  for (const name of ["memory", "console", "files"]) $("tab-" + name).hidden = name !== tab;
  if (tab === "console") guarded(refreshTerminal);
  if (tab === "files") guarded(loadFiles);
}
function renderPanes() {
  const signature = JSON.stringify(state.panes);
  if (state.paneSignature === signature) return;
  state.paneSignature = signature;
  const selected = $("pane-select").value;
  const options = state.panes.map(p => {
    const option = element("option", "", `${p.session} / ${p.name} · ${p.command}`);
    option.value = p.id;
    return option;
  });
  if (!options.length) {
    const option = element("option", "", "暂无终端"); option.value = ""; options.push(option);
    $("terminal-output").textContent = "当前没有活动终端。已退出会话的输出可在运行记录中查看。";
  }
  $("pane-select").replaceChildren(...options);
  if (state.panes.some(p => p.id === selected)) $("pane-select").value = selected;
}
async function refreshTerminal() {
  const pane = $("pane-select").value;
  if (!pane) return;
  const result = await api(`/panes/${encodeURIComponent(pane)}`);
  if (pane !== $("pane-select").value) return;
  const output = $("terminal-output");
  const follow = output.scrollHeight - output.scrollTop - output.clientHeight < 60;
  if (output.textContent !== result.text) {
    output.textContent = result.text || "终端尚无可见输出。";
    if (follow) output.scrollTop = output.scrollHeight;
  }
}
function appendRunActivity(node, run) {
  const a = run.activity || {};
  if (run.deadline_epoch && ["starting", "running"].includes(run.status)) {
    const seconds = Math.max(0, Math.ceil(run.deadline_epoch - Date.now() / 1000));
    node.append(element("p", "agent-sub", `预算剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`));
  }
  if (a.last_event_at) node.append(element("p", "agent-sub", `最近活动 ${time(a.last_event_at)}${a.last_tool ? " · 最近工具 " + a.last_tool : ""}（活动不代表有效进展）`));
  if (a.result_received) node.append(element("p", "agent-sub", a.result_is_error ? "已收到错误结果" : "已收到本轮结果"));
}
function openRunBudget(rid) {
  $("run-budget-form").reset();
  $("run-budget-form").elements.run_id.value = rid;
  $("run-budget-dialog").showModal();
}
$("run-budget-form").onsubmit = event => {
  event.preventDefault();
  guarded(async () => {
    const form = event.target;
    await api(`/runs/${form.elements.run_id.value}/extend`, {method: "POST", body: {minutes: Number(form.elements.minutes.value)}});
    $("run-budget-dialog").close();
    toast("本次运行预算已延长");
    await refreshTask();
  });
};
function renderRuns() {
  const list = $("runs-list");
  list.replaceChildren();
  if (!state.runs.length) list.append(empty("通过网页启动的原命令会记录在这里。"));
  for (const run of state.runs) {
    const item = element("article", "run-item");
    const title = element("h3");
    title.append(element("span", "", {auto: "自主推进", dispatch: "单步调度", shell: "交互终端", custom: "原命令", worker: "worker 操作"}[run.kind] || run.native_operation || run.kind), element("span", "badge" + (["failed", "interrupted"].includes(run.status) ? " error" : ""), names[run.status] || run.status));
    item.append(title, element("p", "", `${time(run.created_at)} · ${run.profile_name}`));
    appendRunActivity(item, run);
    if (run.exit_code !== undefined && run.exit_code !== null) item.append(element("p", "", `退出码 ${run.exit_code}`));
    const actions = element("div", "run-actions");
    actions.append(button("查看输出", "text-button", () => guarded(() => openLog(run))));
    const download = element("a", "text-button", "下载原始输出");
    download.href = `api/runs/${run.id}/log?download=1`;
    actions.append(download);
    if (run.supervisor_version === 1 && run.timeout_seconds != null && ["starting", "running"].includes(run.status)) actions.append(button("延长预算", "text-button", () => openRunBudget(run.id)));
    if (["starting", "running"].includes(run.status)) actions.append(button("停止此会话", "text-button danger", () => guarded(async () => {
      if (!await confirmAction("停止这个 Web 创建的会话及其中的进程？原指挥官另行创建的 worker 会话不会被同时关闭。")) return;
      await api(`/runs/${run.id}/stop`, {method: "POST", body: {}});
      toast("已停止指定会话");
      await refreshTask();
    })));
    item.append(actions);
    list.append(item);
  }
}
async function openLog(run) {
  state.log = run.id;
  state.logOffset = 0;
  $("log-output").textContent = "";
  $("log-title").textContent = `${run.profile_name} · 运行输出`;
  $("log-panel").hidden = false;
  await refreshLog();
}
async function refreshLog() {
  const rid = state.log;
  if (!rid) return;
  const result = await api(`/runs/${rid}/log?offset=${state.logOffset}`);
  if (rid !== state.log) return;
  state.logOffset = result.offset;
  if (result.text) $("log-output").textContent += result.text;
  if ($("log-output").textContent.length > 600000) $("log-output").textContent = "[页面只保留最近输出]\n" + $("log-output").textContent.slice(-500000);
  if ($("follow-log").checked) $("log-output").scrollTop = $("log-output").scrollHeight;
}
async function loadFiles() {
  const result = await taskApi("files?path=" + encodeURIComponent(state.folder));
  $("files-path").textContent = state.folder === "." ? "" : "/ " + state.folder;
  $("files-up").hidden = state.folder === ".";
  const list = $("files-list");
  list.replaceChildren();
  if (!result.length) list.append(empty("目录中还没有文件。可在右上角上传附件。"));
  for (const file of result) {
    const row = element("div", "file-row");
    const action = file.directory ? async () => { state.folder = file.path; await guarded(loadFiles); } : async () => guarded(async () => {
      const result = await taskApi("file?path=" + encodeURIComponent(file.path));
      $("viewer-title").textContent = file.name;
      $("viewer-content").textContent = result.text;
      $("viewer-download").href = `api/projects/${state.project.id}/file?path=${encodeURIComponent(file.path)}&download=1`;
      $("viewer-download").hidden = false;
      $("viewer-dialog").showModal();
    });
    row.append(element("span", "file-icon", file.directory ? "▰" : "▤"), button(file.name, "text-button file-name", action), element("span", "file-size", file.directory ? "目录" : file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`));
    if (!file.directory) {
      const link = element("a", "text-button", "下载");
      link.href = `api/projects/${state.project.id}/file?path=${encodeURIComponent(file.path)}&download=1`;
      row.append(link);
    }
    list.append(row);
  }
}
function refreshProfiles() {
  $("profile-count").textContent = state.profiles.length;
  const select = $("run-profile");
  const selected = select.value;
  select.replaceChildren(element("option", "", "继承原环境"));
  select.firstChild.value = "";
  for (const profile of state.profiles) {
    const option = element("option", "", `${profile.name} · ${profile.model} · ${profileProtocolName(profile.protocol)}`);
    option.value = profile.id;
    select.append(option);
  }
  if (state.profiles.some(p => p.id === selected)) select.value = selected;
  const list = $("profiles-list");
  list.replaceChildren();
  if (!state.profiles.length) list.append(empty("尚无模型配置。也可直接继承服务启动时的原环境。"));
  for (const profile of state.profiles) {
    const row = element("div", "profile-row");
    const info = element("div");
    info.append(element("strong", "", profile.name), element("p", "", `${profileProtocolName(profile.protocol)} · ${profile.model} · ${profile.has_key ? "已保存密钥" : "继承环境密钥"}`), element("p", "", profile.base_url || "继承协议环境／官方地址"));
    const actions = element("div", "profile-row-actions");
    actions.append(button("编辑", "text-button", () => {
      resetProfile();
      for (const name of ["id", "name", "model", "base_url", "engine_prefix", "protocol", "timeout_seconds", "max_output_tokens", "token_field"]) {
        if (profile[name] != null) $("profile-form").elements[name].value = profile[name];
      }
      updateProfileProtocol();
    }), button("删除", "text-button danger", () => guarded(async () => {
      if (!await confirmAction("删除该配置？已启动的原会话不会热切换或重启。")) return;
      await api(`/profiles/${profile.id}`, {method: "DELETE"});
      state.profiles = await api("/profiles");
      refreshProfiles();
      toast("配置已删除");
    })));
    row.append(info, actions);
    list.append(row);
  }
}
function openRun(kind = "auto") {
  $("run-form").reset();
  $("run-kind").value = kind;
  updateRunOptions();
  $("run-dialog").showModal();
}
function updateRunOptions() {
  const kind = $("run-kind").value;
  $("auto-options").hidden = kind !== "auto";
  $("dispatch-options").hidden = kind !== "dispatch";
  $("custom-options").hidden = kind !== "custom";
  $("agent-options").hidden = !["auto", "dispatch"].includes(kind);
}
async function startRun(data) {
  data.request_id = crypto.randomUUID();
  const result = await taskApi("run", {method: "POST", body: data});
  setTab("console");
  toast("已创建原命令会话");
  await refreshTask();
  await openLog(result);
}
async function loadKnowledge() {
  const result = await api("/knowledge?q=" + encodeURIComponent($("knowledge-query").value));
  $("knowledge-list").replaceChildren(...(result.length ? result.map(entry => {
    const node = element("article", "knowledge-item");
    node.append(element("h3", "", `${entry.id} · ${entry.title}`), element("p", "", entry.description || ""));
    if (entry.detail) node.append(element("p", "", entry.detail));
    node.append(button("查看完整条目", "text-button", () => guarded(async () => {
      const full = await api(`/knowledge/${entry.id}`);
      $("viewer-title").textContent = `${full.id} · ${full.title}`;
      $("viewer-content").textContent = [full.description, full.detail, ...(full.commands || []), "tags: " + full.tags.join(", "), full.source || ""].filter(Boolean).join("\n\n");
      $("viewer-download").href = `api/knowledge/${entry.id}?download=1`;
      $("viewer-dialog").showModal();
    })));
    for (const command of entry.commands || []) node.append(element("code", "", command));
    return node;
  }) : [empty("没有匹配的知识条目。")])) ;
}
async function guarded(action) {
  try { return await action(); }
  catch (error) { toast(error.message || "操作失败，请重试", true); }
}
function submit(id, action) {
  $(id).addEventListener("submit", async event => {
    event.preventDefault();
    const btn = event.submitter;
    if (btn) btn.disabled = true;
    try { await action(new FormData(event.currentTarget)); }
    catch (error) {
      if (id === "login-form") $("login-error").textContent = error.message;
      else toast(error.message, true);
    } finally { if (btn) btn.disabled = false; }
  });
}

submit("login-form", async () => {
  const result = await api("/session", {method: "POST", body: {token: $("access-token").value.trim()}});
  state.csrf = result.csrf;
  $("access-token").value = "";
  $("login-error").textContent = "";
  await showWorkspace();
});
submit("create-form", async data => {
  const result = await api("/projects", {method: "POST", body: Object.fromEntries(data)});
  localStorage.setItem("sapper-project", result.id);
  state.project = result;
  state.view = "project";
  $("create-dialog").close();
  $("create-form").reset();
  await refreshProjects();
  toast("任务已通过原 Store 初始化");
});
submit("hint-form", async () => {
  await taskApi("hint", {method: "POST", body: {content: $("hint-content").value}});
  $("hint-content").value = "";
  await refreshTask();
  toast("已保存至原 hint；尚不代表 agent 已读取");
});
submit("record-form", async data => {
  const payload = Object.fromEntries(data);
  const action = payload.action;
  payload.from = data.getAll("from");
  payload.requires_confirmation = data.has("requires_confirmation");
  if (!payload.kind) delete payload.kind;
  await taskApi(action, {method: "POST", body: payload});
  $("record-dialog").close();
  await refreshTask();
  toast("已通过原接口保存记录");
});
submit("terminal-form", async () => {
  const pane = $("pane-select").value;
  if (!pane) throw new Error("请先选择一个活动终端");
  const result = await api(`/panes/${encodeURIComponent(pane)}/send`, {method: "POST", body: {text: $("terminal-text").value}});
  $("terminal-text").value = "";
  toast(result.message);
  await refreshTerminal();
});
submit("run-form", async data => {
  const payload = Object.fromEntries(data);
  payload.minutes = Number(payload.minutes);
  payload.max_steps = Number(payload.max_steps);
  for (const name of ["skip_permissions", "verify", "dry_run"]) payload[name] = data.has(name);
  await startRun(payload);
  $("run-dialog").close();
});
submit("worker-form", async data => {
  const payload = {...Object.fromEntries(data), kind: "worker", close_instance: data.has("close_instance")};
  if (payload.close_instance && !await confirmAction("原 --close 会提交已记录的 flag 并关闭平台实例，继续？")) return;
  await startRun(payload);
  $("worker-dialog").close();
});
submit("profile-form", async data => {
  await api("/profiles", {method: "POST", body: Object.fromEntries(data)});
  resetProfile();
  state.profiles = await api("/profiles");
  refreshProfiles();
  toast("模型配置已保存；现有会话保持原配置");
});
submit("knowledge-form", loadKnowledge);

$("new-project").onclick = $("empty-create").onclick = () => $("create-dialog").showModal();
$("refresh-projects").onclick = () => guarded(refreshProjects);
$("start-run").onclick = () => openRun();
$("open-shell").onclick = () => openRun("shell");
$("run-kind").onchange = updateRunOptions;
$("add-fact").onclick = () => openRecord("fact");
$("add-intent").onclick = () => openRecord("intent");
$("worker-manage").onclick = () => $("worker-dialog").showModal();
$("pane-select").onchange = () => guarded(refreshTerminal);
$("refresh-terminal").onclick = () => guarded(refreshTerminal);
$("close-log").onclick = () => { state.log = null; $("log-panel").hidden = true; };
$("profiles-button").onclick = () => { resetProfile(); $("profiles-dialog").showModal(); };
$("reset-profile").onclick = resetProfile;
$("knowledge-button").onclick = () => guarded(async () => { await loadKnowledge(); $("knowledge-dialog").showModal(); });
function syncGraphTheme() {
  const doc = $("graph-frame").contentDocument;
  if (!doc?.head) return;
  const palette = getComputedStyle(document.documentElement);
  const tokens = {"--bg":"--bg", "--panel":"--paper", "--panel2":"--surface", "--line":"--line", "--txt":"--text", "--dim":"--muted", "--faint":"--muted"};
  for (const [target, source] of Object.entries(tokens)) doc.documentElement.style.setProperty(target, palette.getPropertyValue(source));
  doc.documentElement.style.colorScheme = document.documentElement.dataset.theme;
  let style = doc.getElementById("sapper-page-theme");
  if (!style) { style = doc.createElement("style"); style.id = "sapper-page-theme"; doc.head.append(style); }
  style.textContent = "header,.floating{background:var(--panel)}button:hover{background:var(--panel);border-color:var(--dim)}.floating{box-shadow:0 4px 20px #142c3920}";
}
$("graph-frame").addEventListener("load", syncGraphTheme);
window.addEventListener("sapper:themechange", syncGraphTheme);
$("graph-button").onclick = () => {
  $("graph-frame").src = `api/projects/${state.project.id}/graph-view`;
  $("graph-dialog").showModal();
};
$("save-doc").onclick = () => guarded(async () => {
  await taskApi("doc", {method: "POST", body: {id: state.detail.id, doc: $("detail-doc").value, expected_doc: state.detail.doc}});
  $("detail-dialog").close();
  await refreshTask();
  toast("文档已更新");
});
$("files-root").onclick = () => { state.folder = "."; guarded(loadFiles); };
$("files-up").onclick = () => { const parts = state.folder.split("/"); parts.pop(); state.folder = parts.join("/") || "."; guarded(loadFiles); };
$("upload-file").onchange = () => guarded(async () => {
  const file = $("upload-file").files[0];
  if (!file) return;
  if (file.size > 16 * 1024 * 1024) throw new Error("单个附件最多 16 MiB");
  const result = await taskApi("upload?name=" + encodeURIComponent(file.name), {method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: await file.arrayBuffer()});
  $("upload-file").value = "";
  toast(result.message);
  await Promise.all([loadFiles(), refreshTask()]);
});
$("logout").onclick = () => guarded(async () => { await api("/logout", {method: "POST", body: {}}); showLogin(); });
document.querySelectorAll("[data-tab]").forEach(node => { node.onclick = () => setTab(node.dataset.tab); });
document.querySelectorAll(".dialog-close").forEach(node => { node.onclick = () => node.closest("dialog").close(); });
document.querySelectorAll("[data-key]").forEach(node => { node.onclick = () => guarded(async () => {
  const pane = $("pane-select").value;
  if (!pane) throw new Error("请先选择终端");
  await api(`/panes/${encodeURIComponent(pane)}/send`, {method: "POST", body: {key: node.dataset.key}});
  await refreshTerminal();
}); });

setInterval(async () => {
  if (!state.csrf || state.polling || document.hidden) return;
  state.polling = true;
  try {
    await refreshTask();
    if (state.tab === "console") await Promise.all([refreshTerminal(), refreshLog()]);
    $("connection").textContent = "● 已连接";
    $("connection").classList.remove("offline");
  } catch (error) {
    $("connection").textContent = "● 连接中断，正在重试";
    $("connection").classList.add("offline");
  } finally { state.polling = false; }
}, 2000);

document.addEventListener("DOMContentLoaded", async () => {
  try { const session = await api("/session"); state.csrf = session.csrf; await showWorkspace(); }
  catch { showLogin(); }
}, {once:true});

const profileProtocolNames={anthropic:'Anthropic Messages',responses:'Responses',chat_completions:'Chat Completions'};
let profileModels=[],profileProbeGeneration=0;
function profileProtocolName(value){return profileProtocolNames[value||'anthropic']||value;}
function clearProfileProbe(){profileProbeGeneration++;profileModels=[];$("profile-models").replaceChildren();$("profile-model-options").replaceChildren();$("profile-model-picker").hidden=true;$("profile-probe-result").hidden=true;}
function resetProfile(){ $("profile-form").reset();clearProfileProbe();updateProfileProtocol(); }
function updateProfileProtocol(){
  const p=$("profile-protocol").value;
  $("profile-protocol-help").textContent=p==='anthropic'?'使用 Messages 服务根地址；留空继承 ANTHROPIC_BASE_URL。原配置默认保持此协议。':'填写 API 根地址（通常以 /v1 结尾），也可填写完整接口地址；留空继承 OPENAI_BASE_URL。密钥留空时继承 OPENAI_API_KEY。';
  $("profile-token-field-label").hidden=p!=='chat_completions';
  $("profile-engine-label").hidden=p!=='anthropic';
  $("profile-form").elements.engine_prefix.disabled=p!=='anthropic';
}
function profileDraft(){const draft=Object.fromEntries(new FormData($("profile-form")));if(draft.protocol!=='anthropic')draft.engine_prefix='';return draft;}
function showProfileProbe(message,error=false){const node=$("profile-probe-result");node.textContent=message;node.classList.toggle('error',error);node.hidden=false;}
function renderProfileModels(){
  const q=$("profile-model-filter").value.trim().toLowerCase();
  const matches=profileModels.filter(m=>`${m.id} ${m.name}`.toLowerCase().includes(q));
  $("profile-model-options").replaceChildren(...matches.map(m=>button(m.name===m.id?m.id:`${m.name} · ${m.id}`,'',()=>{$('profile-model').value=m.id;showProfileProbe(`已选择 ${m.id}，保存后可分配给角色。`);}))); 
  if(!matches.length)$("profile-model-options").append(empty('没有匹配的模型，仍可手动填写 ID。'));
}
async function probeProfile(action){
  const form=$("profile-form");
  for(const name of ['base_url','timeout_seconds','max_output_tokens',...(action==='test'?['model']:[])])if(!form.elements[name].reportValidity())return;
  const generation=++profileProbeGeneration;
  const btn=$(action==='models'?'profile-fetch-models':'profile-test');btn.disabled=true;
  showProfileProbe(action==='models'?'正在获取模型列表…':'正在测试所选模型的文本生成…');
  try{
    const result=await api(`/profiles/${action}`,{method:'POST',body:profileDraft()});
    if(generation!==profileProbeGeneration)return;
    if(action==='models'){
      profileModels=result.models;$("profile-models").replaceChildren(...profileModels.map(m=>{const n=element('option','',m.name);n.value=m.id;return n;}));
      $("profile-model-filter").value='';$("profile-model-picker").hidden=false;renderProfileModels();
      showProfileProbe(`获取到 ${profileModels.length} 个模型${result.truncated?'（列表达到分页上限）':''}。点击模型或手动填写 ID；可用列表不代表工具调用已验证。`);
    }else showProfileProbe(`文本生成测试通过 · ${result.latency_ms} ms\n${result.reply}\n${result.message}`);
  }catch(e){if(generation===profileProbeGeneration)showProfileProbe(e.message,true);}
  finally{btn.disabled=false;}
}
$("profile-protocol").onchange=()=>{clearProfileProbe();updateProfileProtocol();};
for(const name of ['base_url','api_key','clear_key','token_field','timeout_seconds','max_output_tokens'])$("profile-form").elements[name].addEventListener('input',clearProfileProbe);
$("profile-model").addEventListener('input',()=>{profileProbeGeneration++;$("profile-probe-result").hidden=true;});
$("profile-model-filter").oninput=renderProfileModels;
$("profile-fetch-models").onclick=()=>probeProfile('models');
$("profile-test").onclick=()=>probeProfile('test');
async function refreshModelBridges(){
 const bridges=await api('/profiles/bridges');
 $('profile-bridges-list').replaceChildren(...bridges.map(b=>{
  const row=element('div','profile-row');row.append(element('p','',`${profileProtocolName(b.protocol)} · ${b.model} · PID ${b.pid}`),button('停止适配服务','text-button danger',()=>guarded(async()=>{
   if(!await confirmAction(`停止 ${profileProtocolName(b.protocol)} / ${b.model} 的后台适配服务？使用该服务的原 agent 和编队模型请求会中断。`))return;
   await api(`/profiles/bridges/${b.id}/stop`,{method:'POST',body:{}});await refreshModelBridges();
  })));return row;
 }));
 if(!bridges.length)$('profile-bridges-list').append(empty('暂无运行中的协议适配服务。'));
}
$('profile-bridges-refresh').onclick=()=>guarded(refreshModelBridges);
