"use strict";

const ROLE_RULE = "__inherit_rule__";
let roleModelConfig = null;

function profileOption(value, label) {
  const option = element("option", "", label);
  option.value = value;
  return option;
}
function modelConnectionLabel(profile) {
  return `${profile.name} · ${profile.model}`;
}
function roleDefaultLabel(role) {
  const assignments = roleModelConfig?.assignments || {};
  if (!(role in assignments)) return "沿用原有规则";
  if (!assignments[role]) return "继承原环境";
  const profile = state.profiles.find(p => p.id === assignments[role]);
  return profile ? modelConnectionLabel(profile) : "连接已失效，请重新分配";
}
async function loadRoleModels() {
  roleModelConfig = await api("/role-models");
  renderRoleModels(false);
  refreshAgentModelOptions();
  $("role-model-status").textContent = "已读取全局角色分配。";
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
    select.onchange = () => { $("role-model-status").textContent = "有未保存的角色分配。"; };
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
  const value = $("agent-profile").value;
  const role = $("agent-role").value;
  if (value !== "__role_default__") {
    $("agent-model-hint").textContent = "本次选择优先于角色默认配置。";
    return;
  }
  const assignments = roleModelConfig?.assignments || {};
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
async function openModels(tab = "connections") {
  const [profiles, config] = await Promise.all([api("/profiles"), api("/role-models")]);
  state.profiles = profiles;
  roleModelConfig = config;
  refreshProfiles();
  renderRoleModels(false);
  $("role-model-status").textContent = "已读取全局角色分配。";
  showModelTab(tab);
  if (!$("profiles-dialog").open) $("profiles-dialog").showModal();
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
$("topbar-models").onclick = () => guarded(() => openModels());
$("agent-model-settings").onclick = () => guarded(() => openModels("roles"));
$("agent-profile").onchange = updateAgentModelHint;
$("agent-parent").addEventListener("change", updateAgentModelHint);
$("reload-role-models").onclick = () => guarded(loadRoleModels);
submit("role-model-form", async form => {
  const assignments = Object.fromEntries([...form].filter(([, value]) => value !== ROLE_RULE));
  roleModelConfig = await api("/role-models", {method: "POST", body: {assignments, expected_revision: roleModelConfig.revision}});
  renderRoleModels(false);
  refreshAgentModelOptions();
  $("role-model-status").textContent = "角色分配已保存，将用于新启动的角色。";
  toast("角色默认模型已保存");
});
