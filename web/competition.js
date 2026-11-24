"use strict";

const competitionStages = {solved: "已解", started: "已开工", assigned: "已派发", open: "未派发"};
const competitionFilter = {workspace: null, category: "", stage: "", signature: null};

function competitionRows(tasks) {
  const intents = new Map(state.graph.intents.map(intent => [intent.id, intent]));
  const agents = new Map();
  const runs = new Map();
  for (const agent of state.agents) {
    if (agent.legacy || !agent.intent_id) continue;
    if (!agents.has(agent.intent_id)) agents.set(agent.intent_id, []);
    agents.get(agent.intent_id).push(agent);
  }
  for (const run of state.runs) {
    if (!run.intent_id) continue;
    if (!runs.has(run.intent_id)) runs.set(run.intent_id, []);
    runs.get(run.intent_id).push(run);
  }
  return tasks.map(task => {
    const assignments = task.intent_ids.map(id => intents.get(id)).filter(Boolean);
    const attached = assignments.flatMap(intent => (agents.get(intent.id) || [])
      .filter(agent => intent.to || intent.worker === agent.id));
    const history = task.intent_ids.flatMap(id => runs.get(id) || []);
    const running = attached.some(agent => agent.observation?.status === "running");
    const observedWork = attached.some(agent => ["running", "idle"].includes(agent.observation?.execution)
      || agent.observation?.needs_user_input);
    const ranBefore = history.some(run => ["responding", "tool_running", "idle"].includes(run.activity?.phase)
      || run.activity?.last_tool_at || run.activity?.result_received);
    const started = observedWork || ranBefore || assignments.some(intent => intent.to && intent.worker);
    const assigned = attached.length || history.length || assignments.some(intent => intent.worker);
    const stage = task.receipt?.status === "submitted" ? "solved" : started ? "started" : assigned ? "assigned" : "open";
    const roles = Array.from(new Map(attached.map(agent => [agent.id, {
      id: agent.id, name: agent.name || agent.role_label, status: formatAgentStatus(agent)
    }])).values());
    return {...task, assignments, roles, stage, label: stage === "started" && running ? "进行中" : competitionStages[stage]};
  });
}

function competitionCounts(rows) {
  const counts = {solved: 0, started: 0, assigned: 0, open: 0};
  for (const row of rows) counts[row.stage]++;
  return counts;
}

function competitionSegments(counts) {
  return Object.entries(counts).filter(([, count]) => count > 0).map(([stage, count]) => {
    const segment = element("span");
    segment.dataset.stage = stage;
    segment.style.flexGrow = count;
    return segment;
  });
}

function competitionFilterButton(label, classes, key, value) {
  const control = button(label, classes, () => {
    competitionFilter[key] = key === "stage" && value === competitionFilter[key] ? "" : value;
    renderCompetition();
    Array.from(document.querySelectorAll(`[data-competition-filter="${key}"]`))
      .find(next => next.value === value)?.focus({preventScroll: true});
  });
  control.dataset.competitionFilter = key;
  control.value = value;
  control.setAttribute("aria-pressed", String(competitionFilter[key] === value));
  return control;
}

function renderCompetition() {
  if (state.project.id !== competitionFilter.workspace) {
    Object.assign(competitionFilter, {workspace: state.project.id, category: "", stage: "", signature: null});
  }
  const board = state.graph.competition;
  $("competition-board").hidden = false;
  $("competition-board").classList.toggle("competition-unconfigured", !board);
  $("competition-error").hidden = !board?.error;
  $("competition-error").textContent = board?.error || "";
  $("competition-roster").hidden = !board || Boolean(board.error);
  if (!board) {
    const messages = {
      loading: "正在读取题单…",
      error: "暂时无法读取题单，请刷新后重试。",
      ready: "尚未导入题单。可以选择本地题目目录，或让主 Agent 将题单写入 competition.json。"
    };
    $("competition-source").textContent = messages[state.graphLoad] || messages.ready;
  } else {
    $("competition-source").textContent = board.error ? "" : [board.complete ? "完整题单" : "已知题单", board.source].join(" · ");
  }
  if (!board || board.error) {
    competitionFilter.signature = null;
    return;
  }
  const rows = competitionRows(board.tasks);
  const categories = new Map();
  for (const row of rows) {
    if (!categories.has(row.category)) categories.set(row.category, []);
    categories.get(row.category).push(row);
  }
  if (!categories.has(competitionFilter.category)) competitionFilter.category = "";
  const {category, stage} = competitionFilter;
  const signature = JSON.stringify([rows, category, stage, board.complete]);
  if (signature === competitionFilter.signature) return;
  competitionFilter.signature = signature;
  const counts = competitionCounts(rows);
  $("competition-score-label").textContent = board.complete ? "已解 / 总题数" : "已解 / 已知题数";
  $("competition-score").replaceChildren(element("strong", "", counts.solved), element("span", "", `/ ${rows.length}`));
  $("competition-track").replaceChildren(...competitionSegments(counts));
  $("competition-filters").replaceChildren(...Object.entries(competitionStages).map(([value, label]) => {
    const control = competitionFilterButton("", "competition-filter", "stage", value);
    control.dataset.stage = value;
    control.append(element("span", "competition-dot"), element("span", "", label), element("b", "", counts[value]));
    return control;
  }));
  $("competition-directions").replaceChildren(...[["", rows], ...categories].map(([key, group]) => {
    const totals = competitionCounts(group);
    const control = competitionFilterButton("", "competition-direction", "category", key);
    control.append(element("span", "", key || "全部方向"), element("small", "", `${totals.solved} / ${group.length}`));
    if (key) {
      const progress = element("span", "competition-track");
      progress.setAttribute("aria-hidden", "true");
      progress.append(...competitionSegments(totals));
      control.append(progress);
    }
    return control;
  }));
  const shown = (categories.get(category) || rows).filter(row => !stage || row.stage === stage);
  $("competition-list-heading").textContent = category || "全部题目";
  $("competition-visible").textContent = `${shown.length} 道`;
  $("competition-clear").hidden = !stage;
  $("competition-table").hidden = !shown.length;
  $("competition-empty").hidden = Boolean(shown.length);
  $("competition-empty").textContent = rows.length ? "没有符合当前筛选的题目。" : "题单中还没有题目。";
  $("competition-rows").replaceChildren(...shown.map(renderCompetitionRow));
}

function renderCompetitionRow(row) {
  const tr = element("tr");
  const lead = element("td");
  const name = button("", "competition-task-name", () => openCompetitionTask(row));
  name.append(element("strong", "", row.name), element("small", "", `${row.id} · ${row.category}`));
  lead.append(name);
  const stateCell = element("td");
  const badge = element("span", "competition-stage");
  badge.dataset.stage = row.stage;
  badge.append(element("span", "competition-dot"), document.createTextNode(row.label));
  stateCell.append(badge);
  const role = element("td");
  if (row.roles.length) {
    const first = row.roles[0];
    role.append(document.createTextNode(first.name), element("small", "", row.roles.length > 1 ? `另有 ${row.roles.length - 1} 位` : first.status));
  } else {
    role.textContent = "—";
  }
  const receipt = element("td");
  if (row.receipt) {
    const receiptStatus = row.receipt.status;
    const outcome = element("span", "competition-receipt", receiptStatus === "submitted" ? "已通过" : "未通过");
    outcome.dataset.receipt = receiptStatus;
    receipt.append(outcome);
    if (row.receipt.at) receipt.append(element("small", "", time(row.receipt.at)));
  } else {
    receipt.textContent = "—";
  }
  tr.append(lead, stateCell, role, receipt);
  return tr;
}

function openCompetitionTask(row) {
  const dialog = $("competition-detail");
  $("competition-detail-title").textContent = row.name;
  $("competition-detail-meta").textContent = `${row.id} · ${row.category}`;
  const addDetail = (label, content) => {
    const pair = element("div");
    const value = element("dd");
    value.append(...content);
    pair.append(element("dt", "", label), value);
    return pair;
  };
  const intentLinks = row.assignments.map(intent =>
    button(`${intent.id} · ${intent.title || intent.description}`, "text-button", () => {
      dialog.close();
      setTab("memory");
      openDetail(intent);
    }));
  const roleLinks = row.roles.map(role =>
    button(`${role.name} · ${role.status}`, "text-button", () => {
      dialog.close();
      setTab("agents");
      agentTopology?.select(role.id, true);
    }));
  const receipt = row.receipt;
  const receiptLabel = receipt
    ? [receipt.status === "submitted" ? "已通过" : "未通过", receipt.fact_id, time(receipt.at)].filter(Boolean).join(" · ")
    : "暂无";
  $("competition-detail-rows").replaceChildren(
    addDetail("题目进度", [row.label]),
    addDetail("关联意图", intentLinks.length ? intentLinks : ["暂无"]),
    addDetail("Agent", roleLinks.length ? roleLinks : ["暂无关联会话"]),
    addDetail("Flag 回执", [receiptLabel])
  );
  const create = $("competition-create-intent");
  create.disabled = state.graph.project.status === "not_enabled";
  create.textContent = create.disabled ? "启用 Sapper 后可创建题目意图" : "创建题目意图";
  create.onclick = () => {
    dialog.close();
    openRecord("intent", row);
  };
  dialog.showModal();
}

function openCompetitionImport() {
  $("project-tools").open = false;
  $("competition-import-form").reset();
  $("competition-import-dialog").showModal();
}

$("competition-import").onclick = openCompetitionImport;
$("competition-import-menu").onclick = openCompetitionImport;
$("competition-browse").onclick = () => guarded(() => openDirectoryPicker($("competition-directory"), "competition"));
$("competition-clear").onclick = () => {
  competitionFilter.stage = "";
  renderCompetition();
  $("competition-filters").querySelector("button")?.focus();
};
submit("competition-import-form", async data => {
  await workspaceApi("competition", {method: "POST", body: {
    directory: data.get("directory"), complete: data.has("complete")
  }});
  $("competition-import-dialog").close();
  setTab("progress");
  await refreshTask();
  toast("已导入比赛题单");
});
