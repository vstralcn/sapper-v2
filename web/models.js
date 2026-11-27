"use strict";

const ROLE_RULE = "__inherit_rule__";
const WORKSPACE_MODEL_ENV = "__workspace_env__";
let roleModelConfig = null;
let agentWorkspaceModels = null;
let roleModelsDirty = false;

function effectiveRoleAssignments() {
  const local = agentWorkspaceModels?.pid === state.project?.id ? agentWorkspaceModels : null;
  if (local && Object.hasOwn(local.assignments, "main")) return local.effective_assignments;
  return {...(roleModelConfig?.assignments || {}), ...(local?.assignments || {})};
}

function profileOption(value, label) {
  const option = element("option", "", label);
  option.value = value;
  return option;
}
function modelConnectionLabel(profile) {
  return `${profile.name} · ${profile.model}`;
}

function renderWorkspaceModelFields(container, config, profiles, {creating = false, preserve = false} = {}) {
  const prefix = container.id.replace(/-rows$/, "");
  const prior = preserve ? readWorkspaceModelFields(container) : config.assignments;
  const noMain = creating ? "" : ROLE_RULE;
  const choices = profiles.filter(profile => !profile.engine_prefix);
  const appendChoices = (select, value) => {
    for (const profile of choices) select.append(profileOption(profile.id, modelConnectionLabel(profile)));
    if (![...select.options].some(option => option.value === value)) {
      select.append(profileOption(value, "连接已失效，请重新选择"));
    }
    select.value = value;
  };

  const label = element("label", "", "主模型");
  const main = document.createElement("select");
  main.name = "main";
  main.id = `${prefix}-main`;
  main.required = creating;
  main.append(profileOption(noMain, creating ? "请选择主模型" : "沿用全局角色模型"),
    profileOption(WORKSPACE_MODEL_ENV, "使用执行机环境"));
  appendChoices(main, prior.main === "" ? WORKSPACE_MODEL_ENV : prior.main ?? noMain);
  label.append(main);

  const overrides = element("details", "workspace-model-overrides");
  overrides.id = `${prefix}-overrides`;
  overrides.open = !creating && Boolean(container.querySelector("details")?.open);
  overrides.append(element("summary", "", "单独设置角色模型"));
  const inheritedOptions = [];
  for (const role of config.roles) {
    const row = element("div", "role-model-row");
    const roleLabel = element("label", "", role.label);
    const select = document.createElement("select");
    select.id = `${prefix}-${role.id}`;
    select.name = role.id;
    roleLabel.htmlFor = select.id;
    const globalId = config.global_assignments[role.id];
    const globalProfile = profiles.find(profile => profile.id === globalId);
    const globalLabel = globalId === undefined ? "原有继承规则"
      : globalId === "" ? "原环境" : globalProfile ? modelConnectionLabel(globalProfile) : "连接已失效";
    const option = profileOption(ROLE_RULE, "");
    inheritedOptions.push({option, globalLabel});
    select.append(option, profileOption("", "使用执行机环境"));
    appendChoices(select, prior[role.id] ?? ROLE_RULE);
    row.append(roleLabel, select);
    overrides.append(row);
  }
  const updateInheritance = () => {
    overrides.hidden = creating && main.value === "";
    for (const {option, globalLabel} of inheritedOptions) {
      option.textContent = main.value === ROLE_RULE ? `继承全局：${globalLabel}` : "继承主模型";
    }
  };
  main.addEventListener("change", updateInheritance);
  updateInheritance();
  container.replaceChildren(label, overrides);
}

function readWorkspaceModelFields(container) {
  return Object.fromEntries([...container.querySelectorAll("select")].filter(select =>
    select.value !== ROLE_RULE && (select.name !== "main" || select.value !== "")
  ).map(select => [select.name, select.value === WORKSPACE_MODEL_ENV ? "" : select.value]));
}

// Workspace environment variables: dynamic name/value rows with an "add" button.
// Creation dialogs collect plaintext values; the settings panel edits write-only
// (existing values render blank with a "keep" placeholder, empty value = keep).
const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const ENV_RESERVED_PREFIX = /^(SAPPER_|CLAUDE_|ANTHROPIC_)/;
const ENV_RESERVED_NAMES = new Set(["PATH", "PYTHONUNBUFFERED", "CLAUDECODE", "IS_DEMO",
  "DISABLE_DOCTOR_COMMAND", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "BENCHMARK_TOKEN"]);

function envVarRow(name = "", value = "", {masked = false, onRemove = null} = {}) {
  const row = element("div", "env-var-row");
  const nameInput = document.createElement("input");
  nameInput.name = "env-name";
  nameInput.placeholder = "变量名，如 MCP_KEY";
  nameInput.value = name;
  nameInput.maxLength = 64;
  nameInput.spellcheck = false;
  nameInput.autocomplete = "off";
  nameInput.readOnly = masked && Boolean(name);
  const valueInput = document.createElement("input");
  valueInput.name = "env-value";
  valueInput.type = "password";
  valueInput.placeholder = masked && name ? "已设置，留空保留" : "值";
  valueInput.value = value;
  valueInput.maxLength = 4096;
  valueInput.autocomplete = "new-password";
  const remove = button("×", "text-button env-var-remove", () => {
    if (onRemove) onRemove(name);
    row.remove();
  });
  remove.type = "button";
  remove.setAttribute("aria-label", "删除此环境变量");
  row.append(nameInput, valueInput, remove);
  return row;
}

function renderEnvRows(container, rows = [], options = {}) {
  container.replaceChildren(...rows.map(([name, value]) => envVarRow(name, value, options)));
}

function addEnvRow(container, options = {}) {
  const row = envVarRow("", "", options);
  container.append(row);
  row.querySelector('[name="env-name"]').focus();
  return row;
}

function readEnvRows(container) {
  const result = {};
  for (const row of container.querySelectorAll(".env-var-row")) {
    const name = row.querySelector('[name="env-name"]').value.trim();
    const value = row.querySelector('[name="env-value"]').value;
    if (!name && !value) continue;
    if (!ENV_NAME_PATTERN.test(name)) throw new Error(`环境变量名“${name}”不正确：以字母或下划线开头，仅含字母、数字、下划线，最多 64 字符。`);
    if (ENV_RESERVED_PREFIX.test(name) || ENV_RESERVED_NAMES.has(name)) throw new Error(`${name} 为系统保留变量，请改用其他名称。`);
    if (Object.hasOwn(result, name)) throw new Error(`环境变量 ${name} 重复。`);
    result[name] = value;
  }
  return result;
}

async function newWorkspaceModelOptions() {
  const [config, profiles] = await Promise.all([api("/role-models"), api("/profiles")]);
  return {config: {...config, assignments: {}}, profiles};
}

async function openCreateWorkspace() {
  const dialog = $("create-dialog");
  const [{config, profiles}, resources] = await Promise.all([newWorkspaceModelOptions(), api("/extensions")]);
  if (dialog.open) return;
  renderWorkspaceModelFields($("create-model-rows"), config, profiles, {creating: true});
  extensionOptions("create-extension-options", resources, resources.selection, {compact: true});
  renderEnvRows($("create-env-rows"), []);
  dialog.showModal();
}
$("create-env-add").onclick = () => addEnvRow($("create-env-rows"));
function roleDefaultLabel(role) {
  const assignments = effectiveRoleAssignments();
  if (!(role in assignments)) return "沿用原有规则";
  if (!assignments[role]) return "继承原环境";
  const profile = state.profiles.find(p => p.id === assignments[role]);
  return profile ? modelConnectionLabel(profile) : "连接已失效，请重新分配";
}
async function loadRoleModels(preserve = false) {
  roleModelConfig = await api("/role-models");
  if (!preserve) roleModelsDirty = false;
  renderRoleModels(preserve && roleModelsDirty);
  refreshAgentModelOptions();
  $("role-model-status").textContent = roleModelsDirty ? "有未保存的角色分配。" : "已读取全局角色分配。";
}
function renderRoleModels(preserve = true) {
  if (!roleModelConfig) return;
  const prior = new Map([...document.querySelectorAll('#role-model-rows select')].map(select => [select.name, select.value]));
  $("role-model-rows").replaceChildren(...roleModelConfig.roles.map(role => {
    const row = element("div", "role-model-row");
    const label = element("label", "", role.label);
    const select = document.createElement("select");
    select.id = `role-model-${role.id}`;
    select.name = role.id;
    label.htmlFor = select.id;
    const name = element("div", "role-model-name");
    name.append(label, element("span", "muted", role.id));
    select.append(profileOption(ROLE_RULE, "沿用原有规则"), profileOption("", "继承原环境"));
    for (const profile of state.profiles.filter(p => !p.engine_prefix)) select.append(profileOption(profile.id, modelConnectionLabel(profile)));
    const value = preserve && prior.has(role.id) ? prior.get(role.id) : Object.hasOwn(roleModelConfig.assignments, role.id) ? roleModelConfig.assignments[role.id] : ROLE_RULE;
    if (![...select.options].some(option => option.value === value)) select.append(profileOption(value, "连接已失效，请重新分配"));
    select.value = value;
    select.onchange = () => { roleModelsDirty = true; $("role-model-status").textContent = "有未保存的角色分配。"; };
    row.append(name, select);
    return row;
  }));
}
function refreshAgentModelOptions(preserve = true) {
  const select = $("agent-profile");
  const previous = preserve ? select.value : "__role_default__";
  select.replaceChildren(profileOption("__role_default__", `按角色默认：${roleDefaultLabel($("agent-role").value)}`), profileOption("", "本次继承原环境"));
  for (const profile of state.profiles.filter(p => !p.engine_prefix)) select.append(profileOption(profile.id, modelConnectionLabel(profile)));
  select.value = [...select.options].some(option => option.value === previous) ? previous : "__role_default__";
  updateAgentModelHint();
}
function updateAgentModelHint() {
  if(typeof refreshDispatchState==="function")refreshDispatchState();
  const value = $("agent-profile").value;
  const role = $("agent-role").value;
  if (value !== "__role_default__") {
    $("agent-model-hint").textContent = "本次选择优先于角色默认配置。";
    return;
  }
  const assignments = effectiveRoleAssignments();
  if (role in assignments) {
    $("agent-model-hint").textContent = `将使用：${roleDefaultLabel(role)}`;
  } else {
    const parent = state.agents?.find(agent => agent.id === $("agent-parent").value);
    $("agent-model-hint").textContent = parent ? `将继承父角色：${parent.profile_name}` : "将继承执行机原环境。";
  }
}
function showModelTab(tab) {
  for (const name of ["connections", "roles"]) {
    const active = name === tab;
    $( `model-${name}-tab`).setAttribute("aria-selected", String(active));
    $( `model-${name}-tab`).tabIndex = active ? 0 : -1;
    $( `model-${name}-panel`).hidden = !active;
  }
}
async function openModels(tab = "connections", openSettingsPage = true) {
  if (openSettingsPage) {
    await openSettings("models");
    showModelTab(tab);
    return;
  }
  const [profiles, config] = await Promise.all([api("/profiles"), api("/role-models")]);
  state.profiles = profiles;
  roleModelConfig = config;
  roleModelsDirty = false;
  refreshProfiles();
  renderRoleModels(false);
  $("role-model-status").textContent = "已读取全局角色分配。";
  showModelTab(tab);
}
function editProfile(profile, focus = true) {
  resetProfile();
  for (const name of ["id", "name", "model", "base_url", "engine_prefix", "protocol", "timeout_seconds", "max_output_tokens", "token_field"]) {
    if (profile[name] != null) $("profile-form").elements[name].value = profile[name];
  }
  $("profile-editor-title").textContent = `编辑连接：${profile.name}`;
  $("profile-form").elements.api_key.placeholder = profile.has_key ? "已保存密钥，留空保留" : "留空继承环境密钥";
  updateProfileProtocol();
  document.querySelectorAll('[data-profile-id]').forEach(row => row.classList.toggle("selected", row.dataset.profileId === profile.id));
  if (focus) $("profile-form").elements.name.focus();
}

$("model-connections-tab").onclick = () => showModelTab("connections");
$("model-roles-tab").onclick = () => showModelTab("roles");
document.querySelector('.model-tabs').onkeydown = event => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tab = event.key === "Home" ? "connections" : event.key === "End" ? "roles" : $("model-connections-tab").getAttribute("aria-selected") === "true" ? "roles" : "connections";
  showModelTab(tab);
  $(`model-${tab}-tab`).focus();
};
$("new-profile").onclick = () => { resetProfile(); $("profile-form").elements.name.focus(); };
$("agent-model-settings").onclick = () => guarded(() => openSettings("workspaces", state.project.id));
$("agent-profile").onchange = updateAgentModelHint;
$("agent-parent").addEventListener("change", updateAgentModelHint);
$("reload-role-models").onclick = () => guarded(loadRoleModels);
submit("role-model-form", async form => {
  const assignments = Object.fromEntries([...form].filter(([, value]) => value !== ROLE_RULE));
  roleModelConfig = await api("/role-models", {method: "POST", body: {assignments, expected_revision: roleModelConfig.revision}});
  roleModelsDirty = false;
  renderRoleModels(false);
  refreshAgentModelOptions();
  if (extensionWorkspace) {
    const config = await api(`/workspaces/${extensionWorkspace}/role-models`);
    workspaceModelConfig = config;
    renderWorkspaceModels(workspaceModelsDirty);
    if (state.project?.id === extensionWorkspace) {
      agentWorkspaceModels = {pid: extensionWorkspace, ...config};
      refreshAgentModelOptions();
    }
  }
  $("role-model-status").textContent = "角色分配已保存，将用于新启动的角色。";
  toast("角色默认模型已保存");
});
