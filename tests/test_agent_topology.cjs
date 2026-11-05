"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const M = require("../web/agent-topology-model.js");

const at = "2026-11-03T03:00:00Z";
const current = Date.parse(at) / 1000 + 60;
function agent(id, extra = {}) {
  return {id, role: "worker", mission: "任务 " + id, status: "running", pane_id: "%" + id, created_at: at, ...extra};
}
function normalize(rows) { return M.normalize(rows, rows.map(a => ({id: a.pane_id, dead: false}))); }
function options(extra = {}) { return {scope: "all", status: "all", collapsed: new Set(), collapsedGroups: new Set(), ...extra}; }

test("running process does not prove task execution; quiet online agents stay online", () => {
  const a = agent("1", {activity: {last_event_at: "2020-01-01", last_tool: "Bash"}});
  const o = M.observe(a, [{id: "%1", dead: false}]);
  assert.equal(o.presence, "online");
  assert.equal(o.status, "unknown");
});

test("confirmed idle commander remains online", () => {
  const a = agent("1", {commander_monitor: {phase: "idle", last_event_at: current}});
  assert.deepEqual([M.observe(a, [{id: "%1", dead: false}]).presence, M.observe(a, [{id: "%1", dead: false}]).status], ["online", "idle"]);
});

test("missing pane is unconfirmed, never an invented offline event", () => {
  assert.equal(M.observe(agent("1"), []).presence, "unknown");
  assert.equal(M.observe(agent("1"), []).status, "unknown");
  const idle = agent("1", {commander_monitor: {phase: "idle", last_event_at: current}});
  assert.equal(M.observe(idle, []).execution, "idle");
  assert.equal(M.observe(idle, []).status, "unknown", "last idle event cannot prove current online presence");
});

test("a dead pane and terminal run prove exit, even with a living wrapper shell", () => {
  assert.equal(M.observe(agent("1"), [{id: "%1", dead: true}]).status, "offline");
  assert.equal(M.observe(agent("1", {status: "exited"}), [{id: "%1", dead: false}]).presence, "offline");
});

test("fresh observed responding and pending tools map to execution", () => {
  for (const phase of ["responding", "tool_running"]) {
    const a = agent("1", {commander_monitor: {phase, last_event_at: current}});
    assert.equal(M.observe(a, [{id: "%1", dead: false}]).status, "running");
  }
});

test("restored run cannot inherit a previous monitor phase", () => {
  const a = agent("1", {updated_at: "2026-11-03T04:00:00Z", commander_monitor: {phase: "responding", last_event_at: current}});
  assert.equal(M.observe(a, [{id: "%1", dead: false}]).status, "unknown");
});

test("explicit waiting and stopping have attributable evidence", () => {
  for (const extra of [{commander_monitor: {control: "waiting_user", reason: "需要决定"}}, {status: "stopping"}]) {
    assert.equal(M.observe(agent("1", extra), [{id: "%1", dead: false}]).status, "waiting");
  }
});

test("received result is a round outcome, not a fabricated task percentage", () => {
  assert.equal(M.observe(agent("1", {activity: {result_received: true}}), [{id: "%1", dead: false}]).execution, "idle");
  assert.equal(M.observe(agent("1", {activity: {result_received: true, result_is_error: true}}), [{id: "%1", dead: false}]).status, "error");
});

test("offline errors retain execution cause without inflating current error totals", () => {
  const rows = normalize([agent("1", {status: "failed", exit_code: 1}), agent("2")]);
  assert.equal(rows[0].observation.execution, "error");
  assert.equal(rows[0].observation.attention, false);
  assert.equal(M.counts(rows).offline, 1);
  assert.equal(M.counts(rows).error, 0);
});

test("monitor attention stays separate from an asserted execution failure", () => {
  const a = normalize([agent("1", {commander_monitor: {phase: "idle", last_event_at: current, attention: "续轮未确认"}})])[0];
  assert.equal(a.observation.status, "idle");
  assert.equal(a.observation.attention, true);
});

test("summary buckets are mutually exclusive and cover every agent", () => {
  const rows = normalize([
    agent("1"), agent("2", {status: "exited"}),
    agent("3", {commander_monitor: {phase: "idle", last_event_at: current}}),
    agent("4", {activity: {result_is_error: true}}),
    agent("5", {commander_monitor: {phase: "responding", last_event_at: current}}),
    agent("6", {status: "stopping"})
  ]);
  const counts = M.counts(rows);
  assert.equal(counts.total, 6);
  assert.equal(["unknown", "offline", "idle", "error", "running", "waiting"].reduce((s, k) => s + counts[k], 0), counts.total);
});

test("active filter retains unconfirmed agents and excludes confirmed idle", () => {
  const rows = normalize([agent("1"), agent("2", {activity: {result_received: true}})]);
  assert.equal(M.matches(rows[0], "active", "all"), true);
  assert.equal(M.matches(rows[1], "active", "all"), false);
  assert.equal(M.matches(rows[1], "online", "all"), true);
});

test("filtered children retain parent context; roles never prescribe hierarchy", () => {
  const rows = normalize([agent("v", {role: "verifier", activity: {result_received: true}}), agent("w", {parent_agent_id: "v"})]);
  const graph = M.graph(rows, options({scope: "active"}));
  assert.equal(graph.matched, 1);
  assert.equal(graph.nodes.find(n => n.id === "v").context, true);
  assert.deepEqual(graph.edges, [{source: "v", target: "w"}]);
});

test("missing parents, self-parent and cycles cannot hide agents or loop layout", () => {
  const rows = normalize([agent("1", {parent_agent_id: "2"}), agent("2", {parent_agent_id: "1"}), agent("3", {parent_agent_id: "missing"}), agent("4", {parent_agent_id: "4"})]);
  const graph = M.graph(rows, options()), positions = new Map();
  M.layout(graph, positions);
  assert.equal(graph.nodes.length, 4);
  assert.equal(positions.size, 4);
  assert.equal(graph.parent.get("3"), null);
  assert.equal(graph.parent.get("4"), null);
  assert.ok(graph.edges.length < 3);
});

test("collapsed subtrees keep real parents and can be expanded completely", () => {
  const rows = normalize([agent("1"), agent("2", {parent_agent_id: "1"}), agent("3", {parent_agent_id: "2"})]);
  assert.equal(M.graph(rows, options({collapsed: new Set(["2"])})).nodes.length, 2);
  assert.equal(M.graph(rows, options()).nodes.length, 3);
});

test("60-agent layout is complete and has no overlapping cards", () => {
  const rows = normalize(Array.from({length: 60}, (_, i) => agent(String(i), {parent_agent_id: i ? String(Math.floor((i - 1) / 4)) : null})));
  const graph = M.graph(rows, options()), positions = new Map(); M.layout(graph, positions);
  assert.equal(positions.size, 60);
  for (const [id, p] of positions) for (const [other, q] of positions) {
    if (other !== id) assert.ok(Math.abs(p.x - q.x) >= M.width || Math.abs(p.y - q.y) >= M.height);
  }
});

test("new nodes ignore hidden historical coordinates and avoid visible cards", () => {
  const rows = normalize([agent("1"), agent("2", {parent_agent_id: "1"}), agent("3", {parent_agent_id: "1"})]);
  const graph = M.graph(rows, options()), positions = new Map([["1", {x: 40, y: 55}], ["3", {x: 334, y: 55}]]);
  for (let i = 1; i <= 60; i++) positions.set("hidden-" + i, {x: 334, y: 55 + i * (M.height + M.gapY)});
  const before = structuredClone(positions.get("1"));
  M.placeMissing(graph, positions);
  assert.deepEqual(positions.get("1"), before);
  assert.deepEqual(positions.get("3"), {x: 334, y: 55});
  assert.deepEqual(positions.get("2"), {x: 334, y: 55 + M.height + M.gapY});
});

test("group IDs remain stable when filters remove a preceding group", () => {
  const rows = normalize([agent("1", {role: "scout", activity: {result_received: true}}), agent("2", {role: "worker"})]);
  const all = M.grouped(rows, options({grouping: "role", collapsedGroups: new Set(["scout", "worker"])}));
  const active = M.grouped(rows, options({scope: "active", grouping: "role", collapsedGroups: new Set(["scout", "worker"])}));
  assert.equal(all.nodes.find(n => n.key === "worker").id, active.nodes.find(n => n.key === "worker").id);
});

test("collapsed groups aggregate actual dispatch edges without invented collaborations", () => {
  const rows = normalize([agent("1", {role: "general"}), agent("2", {parent_agent_id: "1"}), agent("3", {parent_agent_id: "1"})]);
  const graph = M.grouped(rows, options({grouping: "role", collapsedGroups: new Set(["general", "worker"])}));
  assert.equal(graph.nodes.length, 2);
  assert.equal(graph.edges.length, 1);
  assert.equal(graph.edges[0].count, 2);
});

test("task groups use exact missions and normalized IDs are unique", () => {
  const rows = normalize([agent("1", {mission: "同一任务"}), agent("1"), agent("2", {mission: "同一任务"}), agent("3")]);
  const graph = M.grouped(rows, options({grouping: "task", collapsedGroups: new Set(["同一任务", "任务 3"])}));
  assert.equal(rows.length, 3);
  assert.equal(graph.nodes.find(n => n.key === "同一任务").members.length, 2);
});

test("compact goals prefer assignment labels and shorten older missions", () => {
  assert.equal(M.shortGoal({task_label:"验证登录绕过", mission:"详细步骤"}), "验证登录绕过");
  assert.equal(M.shortGoal({mission:"Scout only（侦察）：题目 Crypto #2，先确认格式"}), "Crypto #2");
  assert.equal(M.shortGoal({mission:"Pwn #11「Sign In」 (challenge_id=11)"}), "Pwn #11「Sign In」");
  assert.equal(Array.from(M.shortGoal({mission:"查".repeat(28)})).length, 18);
  assert.equal(M.shortGoal({}), "目标未记录");
});
