"use strict";

let extensionLibrary = null, extensionWorkspace = null, agentExtensionRequest = 0;

function resetExtensionForm(id) { $(id).reset(); $(id).elements.id.value = ""; }

function extensionOptions(target, library, selection, disabled = false) {
  const groups = [];
  for (const [key, field, title] of [["mcp", "mcp_ids", "MCP servers"], ["skills", "skill_ids", "Skills"]]) {
    const group = element("div", "extension-group", "");
    group.append(element("p", "field-help", title));
    for (const resource of library[key]) {
      const label = element("label", "inline-check", "");
      const input = document.createElement("input");
      input.type = "checkbox"; input.dataset.extensionField = field; input.value = resource.id;
      input.checked = selection[field].includes(resource.id); input.disabled = disabled;
      label.append(input, document.createTextNode(resource.name)); group.append(label);
    }
    if (!library[key].length) group.append(element("p", "field-help", "尚未配置；不加载此类资源"));
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
  const data = await api(`/workspaces/${pid}/extensions`);
  if (request !== agentExtensionRequest || pid !== state.project?.id || !$("agent-dialog").open) return;
  extensionOptions("agent-extension-options", data, data.selection, true);
  $("agent-extensions-default").disabled = false;
  $("agent-extension-status").textContent = data.claude.skill_isolation
    ? "会话保存到本工作区 .sapper/sessions/；认证使用 API Key。"
    : "当前 Claude CLI 尚未满足资源隔离要求，需要 2.1.318+ 及 MCP、插件和设置来源参数。";
}

function renderExtensionLibrary(data) {
  extensionLibrary = data;
  extensionOptions("extensions-default-options", data, data.selection);
  $("extensions-status").textContent = `配置版本 ${data.revision} · Claude CLI ${data.claude.version || "未知"}`;
  for (const [kind, target, formId] of [["mcp", "extensions-mcp-list", "extensions-mcp-form"], ["skills", "extensions-skill-list", "extensions-skill-form"]]) {
    $(target).replaceChildren(...data[kind].map(resource => {
      const row = element("div", "extension-resource", "");
      row.append(element("span", "", resource.name));
      row.append(button("编辑", "text-button", () => {
        const form = $(formId);
        form.elements.id.value = resource.id; form.elements.name.value = resource.name;
        if (kind === "mcp") form.elements.config.value = JSON.stringify(resource.config, null, 2);
        else form.elements.path.value = resource.path;
        form.elements.name.focus();
      }));
      row.append(button("移除", "text-button", () => guarded(() => saveExtensionAction({action: "delete", kind, id: resource.id}))));
      return row;
    }));
  }
}

async function reloadExtensionLibrary() {
  const pid = extensionWorkspace;
  const data = await api(`/workspaces/${pid}/extensions`);
  if (pid === extensionWorkspace && $("extensions-dialog").open) renderExtensionLibrary(data);
}

async function openExtensions() {
  extensionWorkspace = state.project.id; extensionLibrary = null;
  resetExtensionForm("extensions-mcp-form"); resetExtensionForm("extensions-skill-form");
  $("extensions-default-options").replaceChildren(); $("extensions-mcp-list").replaceChildren(); $("extensions-skill-list").replaceChildren();
  $("extensions-status").textContent = "正在读取资源…";
  $("extensions-dialog").showModal();
  await reloadExtensionLibrary();
}

async function saveExtensionAction(data) {
  if (!extensionLibrary) throw new Error("请先等待资源读取完成");
  const pid = extensionWorkspace;
  const updated = await api(`/workspaces/${pid}/extensions`, {method: "POST", body: {...data, expected_revision: extensionLibrary.revision}});
  if (pid === extensionWorkspace) renderExtensionLibrary(updated);
  toast("资源配置已保存；已有会话快照保持不变");
  if ($("agent-dialog").open && state.project?.id === pid && $("agent-extensions-default").checked) await loadAgentExtensions();
}

$("agent-extensions-default").onchange = () => {
  $("agent-extension-options").querySelectorAll("input").forEach(input => input.disabled = $("agent-extensions-default").checked);
};
$("workspace-extensions").onclick = () => guarded(openExtensions);
$("agent-extension-settings").onclick = () => guarded(openExtensions);
$("extensions-reload").onclick = () => guarded(reloadExtensionLibrary);
$("extensions-mcp-reset").onclick = () => resetExtensionForm("extensions-mcp-form");
$("extensions-skill-reset").onclick = () => resetExtensionForm("extensions-skill-form");
$("extensions-mcp-import").onclick = () => guarded(async () => {
  await saveExtensionAction({action: "import_mcp", config: JSON.parse($("extensions-mcp-form").elements.config.value)});
  resetExtensionForm("extensions-mcp-form");
});
submit("extensions-defaults-form", async () => saveExtensionAction({action: "defaults", selection: readExtensionSelection("extensions-default-options")}));
submit("extensions-mcp-form", async form => {
  await saveExtensionAction({...Object.fromEntries(form), action: "save", kind: "mcp", config: JSON.parse(form.get("config"))});
  resetExtensionForm("extensions-mcp-form");
});
submit("extensions-skill-form", async form => {
  await saveExtensionAction({...Object.fromEntries(form), action: "save", kind: "skills"});
  resetExtensionForm("extensions-skill-form");
});
