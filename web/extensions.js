"use strict";

let extensionLibrary = null, extensionWorkspace = null, agentExtensionRequest = 0;
let workspaceModelConfig = null, workspaceSettingsRequest = 0;
let workspaceResourcesDirty = false, workspaceModelsDirty = false;

function resetExtensionForm(id) { $(id).reset(); $(id).elements.id.value = ""; }

function extensionOptions(target, library, selection, disabled = false) {
  const groups = [];
  for (const [key, field, title] of [["mcp", "mcp_ids", "MCP 服务器"], ["skills", "skill_ids", "Skills"]]) {
    const group = element("div", "extension-group", "");
    group.dataset.extensionGroup = field;
    group.dataset.title = title;
    group.dataset.total = library[key].length;
    group.append(element("p", "field-help", `${title} · ${selection[field].length} / ${library[key].length}`));
    for (const resource of library[key]) {
      const label = element("label", "inline-check extension-choice", "");
      const input = document.createElement("input");
      input.type = "checkbox"; input.dataset.extensionField = field; input.value = resource.id;
      input.checked = selection[field].includes(resource.id); input.disabled = disabled;
      label.append(input, document.createTextNode(resource.name)); group.append(label);
    }
    if (!library[key].length) group.append(element("p", "field-help", "资源库中还没有此类资源，请先在全局资源库添加。"));
    groups.push(group);
  }
  $(target).replaceChildren(...groups);
}

function readExtensionSelection(target) {
  const selection = {mcp_ids: [], skill_ids: []};
  $(target).querySelectorAll("input:checked").forEach(input => selection[input.dataset.extensionField].push(input.value));
  return selection;
}

async function loadAgentExtensions() {
  const request = ++agentExtensionRequest, pid = state.project.id;
  $("agent-extension-status").textContent = "正在读取资源…";
  $("agent-extension-options").replaceChildren();
  $("agent-extensions-default").checked = true;
  $("agent-extensions-default").disabled = true;
  const [data, models] = await Promise.all([api(`/workspaces/${pid}/extensions`), api(`/workspaces/${pid}/role-models`)]);
  if (request !== agentExtensionRequest || pid !== state.project?.id || !$("agent-dialog").open) return;
  agentWorkspaceModels = {pid, ...models};
  refreshAgentModelOptions();
  extensionOptions("agent-extension-options", data, data.selection, true);
  $("agent-extensions-default").disabled = false;
  $("agent-extension-status").textContent = data.claude.skill_isolation
    ? "会话保存到本工作区 .sapper/sessions/；认证使用 API Key。"
    : "当前 Claude CLI 尚未满足资源隔离要求，需要 2.1.318+ 及 MCP、插件和设置来源参数。";
}

function resourceDetail(kind, resource) {
  if (kind === "skills") return resource.path;
  const config = resource.config;
  return config.type === "http" || config.type === "sse" ? config.url : [config.command, ...(config.args || [])].join(" ");
}
function editExtension(kind, resource = null) {
  const prefix = kind === "mcp" ? "extensions-mcp" : "extensions-skill";
  resetExtensionForm(`${prefix}-form`);
  const form = $(`${prefix}-form`);
  if (resource) {
    form.elements.id.value = resource.id; form.elements.name.value = resource.name;
    if (kind === "mcp") form.elements.config.value = JSON.stringify(resource.config, null, 2);
    else form.elements.path.value = resource.path;
  }
  $(`${prefix}-editor`).open = true;
  form.elements.name.focus();
}
function renderResourceLists() {
  if (!extensionLibrary) return;
  for (const [kind, prefix] of [["mcp", "extensions-mcp"], ["skills", "extensions-skill"]]) {
    const query = $(`${prefix}-search`).value.trim().toLowerCase();
    const resources = extensionLibrary[kind].filter(r => `${r.name} ${resourceDetail(kind, r)}`.toLowerCase().includes(query))
      .sort((a, b) => a.name.localeCompare(b.name));
    const rows = resources.map(resource => {
      const row = element("div", "resource-row");
      const icon = element("span", "resource-icon", kind === "mcp" ? "⌘" : "▤"); icon.setAttribute("aria-hidden", "true");
      const info = element("div", "resource-info");
      info.append(element("strong", "", resource.name), element("p", "resource-detail", resourceDetail(kind, resource)));
      const field = kind === "mcp" ? "mcp_ids" : "skill_ids";
      const users = state.projects.filter(p => extensionLibrary.workspaces[p.id]?.[field].includes(resource.id));
      const scope = button(users.length ? `${users.length} 个工作区使用` : "尚未启用", "resource-scope", () => guarded(() => openSettings("workspaces")));
      scope.title = users.length ? users.map(p => p.name).join("、") : "在工作区配置中启用此资源";
      const actions = element("div", "resource-actions");
      actions.append(button("编辑", "text-button", () => editExtension(kind, resource)), button("移除", "text-button danger", () => guarded(async () => {
        if (!await confirmAction(`从全局资源库移除 ${resource.name}？已有会话快照会保留。`)) return;
        await saveExtensionAction({action: "delete", kind, id: resource.id});
        if ($(`${prefix}-form`).elements.id.value === resource.id) { resetExtensionForm(`${prefix}-form`); $(`${prefix}-editor`).open = false; }
      })));
      row.append(icon, info, scope, actions); return row;
    });
    $(`${prefix}-list`).replaceChildren(...(rows.length ? rows : [empty(query ? "没有匹配的资源。" : kind === "mcp" ? "还没有 MCP 服务器。添加一次，即可供多个工作区使用。" : "还没有 Skills。添加包含 SKILL.md 的本地目录即可开始。")]));
  }
}
function renderExtensionLibrary(data, preserve = true) {
  extensionLibrary = data;
  $("settings-mcp-count").textContent = data.mcp.length;
  $("settings-skill-count").textContent = data.skills.length;
  $("extensions-status").textContent = `全局资源库 · 配置版本 ${data.revision} · 修改用于新启动的角色`;
  renderResourceLists();
  if (extensionWorkspace) {
    let selection = preserve && workspaceResourcesDirty ? readExtensionSelection("extensions-default-options") : data.workspaces[extensionWorkspace] || {mcp_ids: [], skill_ids: []};
    selection = {mcp_ids: selection.mcp_ids.filter(id => data.mcp.some(r => r.id === id)), skill_ids: selection.skill_ids.filter(id => data.skills.some(r => r.id === id))};
    extensionOptions("extensions-default-options", data, selection);
  }
}
async function reloadExtensionLibrary(preserve = true) {
  const data = await api("/extensions");
  if (!preserve) workspaceResourcesDirty = false;
  renderExtensionLibrary(data, preserve);
  if (!workspaceResourcesDirty) $("extensions-selection-status").textContent = "已读取保存的资源选择。";
}
function showSettingsTab(tab) {
  for (const name of ["models", "mcp", "skills", "workspaces"]) {
    const active = name === tab;
    $(`settings-${name}-panel`).hidden = !active;
    const nav = document.querySelector(`[data-settings-tab="${name}"]`);
    if (active) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current");
  }
}
async function openSettings(tab = "models", pid = null) {
  if (!$("profiles-dialog").open) {
    $("profiles-dialog").showModal();
    $("extensions-status").textContent = "正在读取全局配置…";
    await Promise.all([openModels("connections", false), reloadExtensionLibrary()]);
    const select = $("settings-workspace");
    select.replaceChildren(...state.projects.map(p => profileOption(p.id, p.name)));
    const target = pid || state.project?.id || extensionWorkspace || state.projects[0]?.id;
    select.value = state.projects.some(p => p.id === target) ? target : state.projects[0]?.id || "";
    await loadWorkspaceSettings(select.value);
  } else if (pid && pid !== extensionWorkspace) {
    if (!await canChangeWorkspace()) return;
    $("settings-workspace").value = pid;
    await loadWorkspaceSettings(pid);
  }
  showSettingsTab(tab);
}
async function canChangeWorkspace() {
  return !(workspaceModelsDirty || workspaceResourcesDirty) || await confirmAction("当前工作区有未保存的配置。放弃这些更改并切换工作区？");
}
async function loadWorkspaceSettings(pid) {
  const request = ++workspaceSettingsRequest;
  extensionWorkspace = pid || null; workspaceModelConfig = null;
  workspaceResourcesDirty = false; workspaceModelsDirty = false;
  $("settings-workspace-content").hidden = true;
  $("settings-workspace-empty").hidden = !!pid;
  $("settings-workspace-path").textContent = state.projects.find(p => p.id === pid)?.path || "";
  if (!pid) return;
  const data = await api(`/workspaces/${pid}/role-models`);
  if (request !== workspaceSettingsRequest || pid !== extensionWorkspace) return;
  workspaceModelConfig = data;
  renderWorkspaceModels();
  if (extensionLibrary) extensionOptions("extensions-default-options", extensionLibrary, extensionLibrary.workspaces[pid] || {mcp_ids: [], skill_ids: []});
  $("workspace-model-status").textContent = "已读取工作区角色模型。";
  $("extensions-selection-status").textContent = "已读取工作区资源选择。";
  $("settings-workspace-content").hidden = false;
}
function renderWorkspaceModels(preserve = false) {
  if (!workspaceModelConfig) return;
  const prior = new Map([...$("workspace-model-rows").querySelectorAll("select")].map(s => [s.name, s.value]));
  $("workspace-model-rows").replaceChildren(...workspaceModelConfig.roles.map(role => {
    const row = element("div", "role-model-row");
    const label = element("label", "", role.label);
    const select = document.createElement("select"); select.id = `workspace-model-${role.id}`; select.name = role.id; label.htmlFor = select.id;
    const globalId = workspaceModelConfig.global_assignments[role.id];
    const globalProfile = state.profiles.find(p => p.id === globalId);
    const globalLabel = globalId === undefined ? "原有继承规则" : globalId === "" ? "原环境" : globalProfile ? modelConnectionLabel(globalProfile) : "连接已失效";
    select.append(profileOption(ROLE_RULE, `继承全局：${globalLabel}`), profileOption("", "此工作区继承原环境"));
    for (const profile of state.profiles.filter(p => !p.engine_prefix)) select.append(profileOption(profile.id, modelConnectionLabel(profile)));
    const value = preserve && prior.has(role.id) ? prior.get(role.id) : workspaceModelConfig.assignments[role.id] ?? ROLE_RULE;
    if (![...select.options].some(o => o.value === value)) select.append(profileOption(value, "连接已失效，请重新分配"));
    select.value = value;
    select.onchange = () => { workspaceModelsDirty = true; $("workspace-model-status").textContent = "有未保存的工作区模型。"; };
    row.append(label, select); return row;
  }));
}
async function saveExtensionAction(data) {
  if (!extensionLibrary) throw new Error("请先等待资源读取完成");
  const updated = await api("/extensions", {method: "POST", body: {...data, expected_revision: extensionLibrary.revision}});
  renderExtensionLibrary(updated);
  toast("全局资源库已保存");
}

$("agent-extensions-default").onchange = () => {
  $("agent-extension-options").querySelectorAll("input").forEach(input => input.disabled = $("agent-extensions-default").checked);
};
$("workspace-extensions").onclick = $("agent-extension-settings").onclick = () => guarded(() => openSettings("workspaces", state.project.id));
document.querySelectorAll("[data-settings-tab]").forEach(node => node.onclick = () => guarded(() => openSettings(node.dataset.settingsTab)));
document.querySelectorAll("[data-resource-reload]").forEach(node => node.onclick = () => guarded(reloadExtensionLibrary));
$("settings-workspace").onchange = () => guarded(async () => {
  const pid = $("settings-workspace").value;
  if (!await canChangeWorkspace()) { $("settings-workspace").value = extensionWorkspace || ""; return; }
  await loadWorkspaceSettings(pid);
});
$("workspace-model-reload").onclick = () => guarded(async () => {
  const pid = extensionWorkspace;
  if (!pid) return;
  const config = await api(`/workspaces/${pid}/role-models`);
  if (pid !== extensionWorkspace) return;
  workspaceModelConfig = config; workspaceModelsDirty = false;
  renderWorkspaceModels();
  $("workspace-model-status").textContent = "已读取工作区角色模型。";
});
$("extensions-reload").onclick = () => guarded(() => reloadExtensionLibrary(false));
$("extensions-default-options").onchange = () => {
  workspaceResourcesDirty = true; $("extensions-selection-status").textContent = "有未保存的资源选择。";
  $("extensions-default-options").querySelectorAll(".extension-group").forEach(group => {
    group.firstChild.textContent = `${group.dataset.title} · ${group.querySelectorAll("input:checked").length} / ${group.dataset.total}`;
  });
};
for (const [id, checked] of [["extensions-select-all", true], ["extensions-select-none", false]]) {
  $(id).onclick = () => {
    $("extensions-default-options").querySelectorAll("input").forEach(input => input.checked = checked);
    $("extensions-default-options").dispatchEvent(new Event("change"));
  };
}
for (const [kind, prefix] of [["mcp", "extensions-mcp"], ["skills", "extensions-skill"]]) {
  $(`${prefix}-search`).oninput = renderResourceLists;
  $(`${prefix}-add`).onclick = $(`${prefix}-reset`).onclick = () => editExtension(kind);
}
$("extensions-mcp-import").onclick = () => guarded(async () => {
  await saveExtensionAction({action: "import_mcp", config: JSON.parse($("extensions-mcp-form").elements.config.value)});
  resetExtensionForm("extensions-mcp-form"); $("extensions-mcp-editor").open = false;
});
submit("extensions-defaults-form", async () => {
  const pid = extensionWorkspace;
  if (!pid || !extensionLibrary) throw new Error("请先选择工作区并等待读取完成");
  const data = await api(`/workspaces/${pid}/extensions`, {method: "POST", body: {action: "defaults", selection: readExtensionSelection("extensions-default-options"), expected_revision: extensionLibrary.revision}});
  if (pid === extensionWorkspace) { workspaceResourcesDirty = false; renderExtensionLibrary(data, false); $("extensions-selection-status").textContent = "工作区资源选择已保存。"; }
  toast("工作区资源已保存，将用于新启动的角色");
  if ($("agent-dialog").open && state.project?.id === pid && $("agent-extensions-default").checked) await loadAgentExtensions();
});
submit("workspace-model-form", async form => {
  const pid = extensionWorkspace;
  if (!pid || !workspaceModelConfig) throw new Error("请先等待读取工作区模型");
  const assignments = Object.fromEntries([...form].filter(([, value]) => value !== ROLE_RULE));
  const data = await api(`/workspaces/${pid}/role-models`, {method: "POST", body: {assignments, expected_revision: workspaceModelConfig.revision}});
  if (pid === extensionWorkspace) { workspaceModelConfig = data; workspaceModelsDirty = false; renderWorkspaceModels(); $("workspace-model-status").textContent = "工作区模型已保存。"; }
  if (state.project?.id === pid) { agentWorkspaceModels = {pid, ...data}; refreshAgentModelOptions(); }
  // The role-model revision is shared by global and workspace assignments.
  await loadRoleModels(true);
  toast("工作区模型已保存，将用于新启动的角色");
});
submit("extensions-mcp-form", async form => {
  await saveExtensionAction({...Object.fromEntries(form), action: "save", kind: "mcp", config: JSON.parse(form.get("config"))});
  resetExtensionForm("extensions-mcp-form"); $("extensions-mcp-editor").open = false;
});
submit("extensions-skill-form", async form => {
  await saveExtensionAction({...Object.fromEntries(form), action: "save", kind: "skills"});
  resetExtensionForm("extensions-skill-form"); $("extensions-skill-editor").open = false;
});
