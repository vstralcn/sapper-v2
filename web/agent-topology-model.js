/* Observation and layout helpers shared by the browser and dependency-free tests. */
(function (root, factory) {
  const model = factory();
  if (typeof module === "object" && module.exports) module.exports = model;
  else root.AgentTopologyModel = model;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const labels = {running: "运行中", waiting: "等待中", idle: "在线空闲", error: "异常", offline: "已离线", unknown: "状态未确认"};
  const rank = {error: 0, running: 1, waiting: 2, unknown: 3, idle: 4, offline: 5};
  const terminal = new Set(["exited", "failed", "stopped", "interrupted", "timed_out"]);
  const width = 218, height = 109, gapX = 76, gapY = 24;

  function timestamp(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value * 1000 : 0;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function observe(agent, panes = []) {
    const pane = panes.find(p => p.id === agent.pane_id);
    // A terminal run is evidence that the agent exited, even if its tmux shell remains.
    const presence = terminal.has(agent.status) || pane?.dead === true ? "offline"
      : pane?.dead === false ? "online" : "unknown";
    const monitor = agent.commander_monitor || {}, activity = agent.activity || {};
    const last = Math.max(timestamp(activity.last_event_at), timestamp(monitor.last_event_at));
    // A restored run must not inherit an old monitor phase before new events arrive.
    const currentMonitor = timestamp(monitor.last_event_at) > 0 &&
      timestamp(monitor.last_event_at) >= Math.max(timestamp(agent.updated_at), timestamp(agent.created_at));
    let execution = "unknown", evidence = "只有运行／进程记录，尚无可确认的执行阶段";
    const failed = agent.status === "failed" || agent.status === "timed_out" ||
      (terminal.has(agent.status) && typeof agent.exit_code === "number" && agent.exit_code !== 0) ||
      activity.result_is_error === true;
    if (failed) {
      execution = "error";
      evidence = agent.stop_reason || (agent.status === "timed_out" ? "运行预算耗尽" : "运行失败、非零退出码或明确的结果错误");
    } else if (currentMonitor && ["responding", "tool_running"].includes(monitor.phase)) {
      execution = "running";
      evidence = monitor.phase === "tool_running" ? "原生会话记录：工具执行中" : "原生会话记录：模型处理中";
    } else if ((currentMonitor && monitor.phase === "waiting_user") || monitor.control === "waiting_user") {
      execution = "waiting";
      evidence = monitor.reason || "原生会话或显式调度控制：等待用户输入";
    } else if (currentMonitor && monitor.phase === "idle") {
      execution = "idle";
      evidence = "原生会话记录：本轮已结束；在线性另行观测";
    } else if (activity.result_received === true) {
      execution = "idle";
      evidence = "已收到本轮结果；不代表整个任务完成";
    } else if (agent.status === "stopping") {
      execution = "waiting";
      evidence = "已请求停止，正在等待进程退出";
    }
    const status = presence === "offline" ? "offline" : presence === "unknown" ? "unknown" : execution;
    return {presence, execution, status, evidence, last,
      attention: presence !== "offline" && (failed || Boolean(monitor.attention)),
      attentionReason: monitor.attention || (failed ? evidence : "")};
  }

  function normalize(agents, panes) {
    const seen = new Set();
    return agents.filter(a => typeof a.id === "string" && a.id && !seen.has(a.id) && seen.add(a.id))
      .map(a => ({...a, observation: observe(a, panes)}));
  }

  function compare(a, b) {
    return Number(b.observation.attention) - Number(a.observation.attention) ||
      rank[a.observation.status] - rank[b.observation.status] || a.id.localeCompare(b.id);
  }

  function counts(rows) {
    const result = {total: rows.length, running: 0, waiting: 0, idle: 0, error: 0, offline: 0, unknown: 0, online: 0, presenceUnknown: 0};
    for (const a of rows) {
      result[a.observation.status]++;
      if (a.observation.presence === "online") result.online++;
      if (a.observation.presence === "unknown") result.presenceUnknown++;
    }
    return result;
  }

  function matches(a, scope, status) {
    const s = a.observation.status;
    // Unconfirmed states need visibility: never silently discard an unobserved agent.
    return (scope === "all" || s !== "offline") &&
      (scope !== "active" || s !== "idle") && (status === "all" || s === status);
  }

  function parents(rows) {
    const map = new Map(rows.map(a => [a.id, a])), result = new Map();
    for (const a of rows) result.set(a.id, map.has(a.parent_agent_id) && a.parent_agent_id !== a.id ? a.parent_agent_id : null);
    // Break only the offending edge of a cycle. Every agent remains selectable.
    const done = new Set();
    for (const a of rows) {
      let id = a.id;
      const trail = new Set();
      while (id && !done.has(id)) {
        if (trail.has(id)) { result.set(id, null); break; }
        trail.add(id); id = result.get(id);
      }
      for (const id of trail) done.add(id);
    }
    return result;
  }

  function graph(rows, options) {
    const {scope = "active", status = "all", collapsed = new Set()} = options;
    const parent = parents(rows), byId = new Map(rows.map(a => [a.id, a]));
    const direct = new Set(rows.filter(a => matches(a, scope, status)).map(a => a.id));
    const all = new Set(direct);
    for (const id of direct) {
      let p = parent.get(id);
      while (p) { all.add(p); p = parent.get(p); }
    }
    const shown = new Set([...all].filter(id => {
      let p = parent.get(id);
      while (p) { if (collapsed.has(p)) return false; p = parent.get(p); }
      return true;
    }));
    const nodes = [...shown].map(id => ({id, agent: byId.get(id), context: !direct.has(id), type: "agent"}));
    const edges = nodes.filter(n => shown.has(parent.get(n.id))).map(n => ({source: parent.get(n.id), target: n.id}));
    return {nodes, edges, parent, matched: direct.size};
  }

  function descendants(id, parent) {
    const found = new Set(), queue = [id];
    while (queue.length) {
      const p = queue.shift();
      for (const [child, owner] of parent) if (owner === p && !found.has(child)) { found.add(child); queue.push(child); }
    }
    return found;
  }

  function groupKey(agent, mode) {
    return mode === "role" ? agent.role || "unassigned" : (agent.mission || "未分配任务").trim();
  }

  function grouped(rows, options) {
    const groups = new Map(), mapping = new Map(), nodes = [], edges = new Map();
    const filtered = rows.filter(a => matches(a, options.scope, options.status));
    for (const a of filtered) {
      const key = groupKey(a, options.grouping);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    for (const [key, members] of groups) {
      const id = "group:" + options.grouping + ":" + key;
      nodes.push({id, key, members, type: "group"});
      for (const a of members) {
        mapping.set(a.id, options.collapsedGroups.has(key) ? id : a.id);
        if (!options.collapsedGroups.has(key)) nodes.push({id: a.id, agent: a, group: key, type: "agent"});
      }
    }
    const parent = parents(rows);
    for (const a of filtered) {
      const source = mapping.get(parent.get(a.id)), target = mapping.get(a.id);
      if (!source || source === target) continue;
      const key = JSON.stringify([source, target]);
      if (edges.has(key)) edges.get(key).count++;
      else edges.set(key, {source, target, count: 1});
    }
    return {nodes, edges: [...edges.values()], parent, matched: filtered.length};
  }

  function layout(graph, positions) {
    const lanes = new Map(), depth = new Map(), visible = new Set(graph.nodes.map(n => n.id));
    const getDepth = id => {
      if (depth.has(id)) return depth.get(id);
      const p = graph.parent.get(id), d = p && visible.has(p) ? getDepth(p) + 1 : 0;
      depth.set(id, d); return d;
    };
    for (const n of graph.nodes) {
      const d = getDepth(n.id);
      if (!lanes.has(d)) lanes.set(d, []);
      lanes.get(d).push(n);
    }
    const max = Math.max(1, ...[...lanes.values()].map(l => l.length)), order = new Map();
    for (const [d, lane] of [...lanes].sort((a, b) => a[0] - b[0])) {
      lane.sort((a, b) => (order.get(graph.parent.get(a.id)) || 0) - (order.get(graph.parent.get(b.id)) || 0) || compare(a.agent, b.agent));
      lane.forEach((n, i) => {
        order.set(n.id, i);
        positions.set(n.id, {x: 40 + d * (width + gapX), y: 55 + (max - lane.length) * (height + gapY) / 2 + i * (height + gapY)});
      });
    }
  }

  function placeMissing(graph, positions) {
    for (const n of graph.nodes) {
      if (positions.has(n.id)) continue;
      const p = positions.get(graph.parent.get(n.id));
      const candidate = {x: p ? p.x + width + gapX : 40, y: p ? p.y : 55};
      while ([...positions].some(([id, q]) =>
        id !== n.id && Math.abs(q.x - candidate.x) < width + 20 && Math.abs(q.y - candidate.y) < height + gapY
      )) candidate.y += height + gapY;
      positions.set(n.id, candidate);
    }
  }

  return {labels, width, height, gapX, gapY, timestamp, observe, normalize, compare, counts, matches, parents, graph, descendants, groupKey, grouped, layout, placeMissing};
});
