"use strict";

/* Live workspace view. No simulator, additional poller, or agent lifecycle owner. */
class AgentTopologyView {
  constructor(root, renderCard, dispatch) {
    this.root = root;
    this.renderCard = renderCard;
    this.dispatch = dispatch;
    this.model = AgentTopologyModel;
    this.workspaces = new Map();
    this.rows = [];
    this.graph = {nodes: [], edges: [], parent: new Map(), matched: 0};
    this.elements = new Map();
    this.connected = true;
    root.innerHTML = `
      <div class="at-heading"><div><h2>角色与协作</h2><p>查看当前分工、真实派发关系与运行观测。</p></div><div class="at-heading-tools"><span data-at="connection" class="at-connection">正在读取工作区</span><button data-at="dispatch" class="button primary">＋ 派发角色</button></div></div>
      <div data-at="metrics" class="at-metrics" aria-label="当前工作区完整 Agent 统计"></div>
      <div class="at-attention"><button data-at="attention" class="text-button"></button><span data-at="totals"></span></div>
      <div class="at-monitor">
        <section class="at-graph-shell" aria-label="Agent 派发拓扑">
          <div class="at-toolbar"><div class="at-scopes" aria-label="显示范围"><button data-at-scope="online" aria-pressed="false">活跃优先</button><button data-at-scope="active" aria-pressed="true">仅活跃</button><button data-at-scope="all" aria-pressed="false">显示全部</button></div><div class="at-search-wrap"><input data-at="search" type="search" placeholder="搜索名称、角色或任务" aria-label="搜索工作区 Agent" autocomplete="off" aria-expanded="false" aria-controls="at-search-results"><div data-at="results" id="at-search-results" class="at-search-results" hidden></div></div></div>
          <div class="at-filters"><label>状态<select data-at="status" aria-label="筛选 Agent 状态"><option value="all">全部状态</option><option value="running">运行中</option><option value="waiting">等待中</option><option value="error">异常</option><option value="unknown">状态未确认</option><option value="idle">在线空闲</option><option value="offline">已离线</option></select></label><label>视图<select data-at="grouping" aria-label="Agent 分组方式"><option value="hierarchy">指挥关系</option><option value="role">按角色分组</option><option value="task">按任务说明分组</option></select></label><span></span><button data-at="expand">展开全部</button><button data-at="layout" title="状态刷新不会重排；此按钮主动重新排列">重新布局</button></div>
          <div data-at="canvas" class="at-canvas" tabindex="0" aria-label="Agent 拓扑画布，拖动空白平移，滚轮缩放，方向键平移，加减键缩放，Home 适应画布">
            <div data-at="world" class="at-world"><svg class="at-edges" aria-hidden="true"><defs><marker id="at-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0 10 5 0 10Z" fill="var(--at-edge)"/></marker></defs><g data-at="edges"></g></svg><div data-at="nodes"></div></div>
            <div data-at="canvas-label" class="at-canvas-label"></div>
            <div data-at="empty" class="at-empty" hidden></div>
            <div class="at-canvas-tools"><button data-at="zoom-out" aria-label="缩小拓扑">−</button><button data-at="zoom" title="恢复 100%">100%</button><button data-at="zoom-in" aria-label="放大拓扑">＋</button><button data-at="fit">适应画布</button></div>
            <svg data-at="minimap" class="at-minimap" viewBox="0 0 130 84" aria-label="拓扑缩略图，点击定位"></svg>
          </div>
          <div class="at-graph-foot"><span><i></i>任务派发</span><span>拖动节点调整 · 滚轮缩放 · 空白处平移</span></div>
        </section>
        <aside class="at-detail" aria-label="所选 Agent 详情"><div class="at-detail-head">Agent 详情<span data-at="selection"></span></div><div data-at="detail" class="at-detail-body"></div><div class="at-detail-actions"><button data-at="focus">聚焦关联</button><button data-at="reveal">定位节点</button></div></aside>
      </div>
      <div class="at-footnote">在线性与执行状态分别观测；未确认状态不会因静默被判为离线。进度只按可靠数据展示。当前连线表示已登记的派发关系。</div>
      <details class="at-help"><summary>状态与分组说明</summary><p>“仅活跃”保留运行、等待、异常以及状态未确认的 Agent；“活跃优先”还包含在线空闲；“显示全部”包含离线历史。总览统计始终覆盖全部记录。虚线边框节点是筛选后保留的上级上下文，不计入匹配数量。任务分组按相同的任务说明聚合，分组连线为真实派发关系的汇总。没有登记的协作关系不会生成虚线连线。预算剩余时间不代表任务完成进度。</p></details>
      <div data-at="services" class="at-services"></div>`;
    this.el = name => root.querySelector(`[data-at="${name}"]`);
    this.bind();
    this.resizeObserver = new ResizeObserver(() => {
      if (this.current && this.el("canvas").clientWidth) {
        if (this.current.needsFit) this.fit();
        else this.renderMinimap();
      }
    });
    this.resizeObserver.observe(this.el("canvas"));
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  }

  role(a) { return a.role_label || roleNames[a.role] || "角色未标注"; }
  name(a) { return a.name || this.role(a); }
  presence(a) { return {online: "在线", offline: "已离线", unknown: "在线性未确认"}[a.observation.presence]; }
  tag(a) { return `<span class="at-status at-status-${a.observation.status}"><i></i>${this.model.labels[a.observation.status]}</span>`; }

  switchWorkspace(id) {
    if (this.workspaceId === id) return;
    this.workspaceId = id;
    if (!this.workspaces.has(id)) this.workspaces.set(id, {
      scope: "active", status: "all", grouping: "hierarchy", positions: new Map(), groupPositions: new Map(),
      collapsed: new Set(), collapsedGroups: new Set(), selected: "", focused: false,
      zoom: 1, pan: {x: 0, y: 0}, initialized: false, needsFit: true
    });
    this.current = this.workspaces.get(id);
    this.rows = [];
    this.elements.clear();
    this.el("nodes").replaceChildren();
    this.el("edges").replaceChildren();
    this.el("search").value = "";
    this.hideSearch();
    this.detailSignature = "";
    this.graph = {nodes: [], edges: [], parent: new Map(), matched: 0};
    this.el("detail").replaceChildren();
    this.el("connection").textContent = "正在读取工作区";
    this.el("services").replaceChildren();
    this.el("metrics").replaceChildren();
    this.el("attention").textContent = "";
    this.el("totals").textContent = "";
    this.el("minimap").replaceChildren();
    this.el("canvas-label").textContent = "正在读取角色";
  }

  update(id, agents, panes, graph) {
    this.switchWorkspace(id);
    const c = this.current;
    this.rows = this.model.normalize(agents, panes);
    this.workspaceGraph = graph;
    this.setConnection(true);
    if (!this.rows.some(a => a.id === c.selected)) {
      c.selected = [...this.rows].sort(this.model.compare)[0]?.id || "";
      c.focused = false;
    }
    if (!c.initialized) {
      const parent = this.model.parents(this.rows);
      if (this.rows.length > 14) {
        for (const a of this.rows) if (parent.get(a.id) && this.model.descendants(a.id, parent).size >= 3) c.collapsed.add(a.id);
        this.expandAncestors(c.selected, parent);
      }
      c.initialized = this.rows.length > 0;
      this.rebuild(true);
    } else this.rebuild();
  }

  setConnection(connected) {
    this.connected = connected;
    this.el("connection").classList.toggle("at-disconnected", !connected);
    this.el("connection").textContent = connected
      ? "约 2 秒刷新 · 已更新 " + new Date().toLocaleTimeString("zh-CN", {hour12: false})
      : "更新中断 · 当前显示上次成功观测";
  }

  expandAncestors(id, parent = this.model.parents(this.rows)) {
    let p = parent.get(id);
    while (p) { this.current.collapsed.delete(p); p = parent.get(p); }
  }

  positions() { return this.current.grouping === "hierarchy" ? this.current.positions : this.current.groupPositions; }
  position(id) { return this.positions().get(id); }
  selected() { return this.rows.find(a => a.id === this.current.selected); }

  rebuild(layout = false) {
    if (!this.current) return;
    const c = this.current;
    this.graph = c.grouping === "hierarchy" ? this.model.graph(this.rows, c) : this.model.grouped(this.rows, c);
    if (layout) {
      if (c.grouping === "hierarchy") this.model.layout(this.graph, c.positions);
      else this.layoutGroups();
    } else this.model.placeMissing(this.graph, this.positions());
    this.renderMetrics();
    this.renderNodes();
    this.renderEdges();
    this.renderDetail();
    this.syncControls();
    if (c.needsFit && this.el("canvas").clientWidth) this.fit();
    else this.transform();
  }

  layoutGroups() {
    const c = this.current, groups = this.graph.nodes.filter(n => n.type === "group");
    const {width, height, gapY} = this.model;
    if (groups.every(g => c.collapsedGroups.has(g.key))) {
      const cols = Math.max(1, Math.min(3, Math.floor((this.el("canvas").clientWidth - 50) / (width + 45))));
      groups.forEach((g, i) => c.groupPositions.set(g.id, {x: 40 + i % cols * (width + 50), y: 75 + Math.floor(i / cols) * (height + 65)}));
    } else {
      let y = 65;
      for (const group of groups) {
        c.groupPositions.set(group.id, {x: 40, y});
        const members = this.graph.nodes.filter(n => n.group === group.key).sort((a, b) => this.model.compare(a.agent, b.agent));
        members.forEach((n, i) => c.groupPositions.set(n.id, {x: 40 + (i % 3 + 1) * (width + 48), y: y + Math.floor(i / 3) * (height + gapY)}));
        y += Math.max(1, Math.ceil(members.length / 3)) * (height + gapY) + 32;
      }
    }
  }

  renderMetrics() {
    const counts = this.model.counts(this.rows);
    const metrics = [["total", "Agent 总数"], ["running", "运行中"], ["waiting", "等待中"], ["error", "异常"], ["idle", "在线空闲"], ["offline", "已离线"], ["unknown", "状态未确认"]];
    this.el("metrics").innerHTML = metrics.map(([key, label]) => `<div class="at-metric at-status-${key}" data-at-metric="${key}"><strong>${counts[key]}</strong><span>${key === "total" ? "" : "<i></i>"}${label}</span></div>`).join("");
    const attention = this.rows.filter(a => a.observation.attention);
    this.el("attention").textContent = attention.length ? `${attention.length} 个 Agent 需要关注 · 查看详情` : "当前没有已确认异常";
    this.el("attention").disabled = !attention.length;
    this.el("totals").textContent = `确认在线 ${counts.online} · 在线性未确认 ${counts.presenceUnknown} · 统计不受筛选影响`;
  }

  related() {
    const id = this.current.selected, result = new Set([id]);
    if (this.graph.parent.get(id)) result.add(this.graph.parent.get(id));
    for (const [child, parent] of this.graph.parent) if (parent === id) result.add(child);
    return result;
  }

  renderNodes() {
    const c = this.current, valid = new Set(), near = c.focused ? this.related() : null;
    for (const n of this.graph.nodes) {
      valid.add(n.id);
      let el = this.elements.get(n.id);
      if (!el) {
        el = document.createElement("div"); el.dataset.agentId = n.id; el.tabIndex = 0;
        el.setAttribute("role", "button");
        this.elements.set(n.id, el); this.el("nodes").append(el);
      }
      const p = this.position(n.id), a = n.agent;
      el.style.left = p.x + "px"; el.style.top = p.y + "px";
      let html, label;
      if (n.type === "group") {
        const errors = n.members.filter(a => a.observation.attention).length;
        const labelText = c.grouping === "role" ? roleNames[n.key] || n.members[0]?.role_label || n.key : n.key;
        label = `${labelText}，${n.members.length} 个 Agent，点击${c.collapsedGroups.has(n.key) ? "展开" : "折叠"}`;
        el.className = "at-node at-group";
        html = `<div class="at-node-header"><span class="at-avatar">${n.members.length}</span><strong title="${this.escape(labelText)}">${this.escape(labelText)}</strong></div><div class="at-node-sub">${n.members.length} 个匹配${errors ? " · " + errors + " 个需关注" : ""}</div><p class="at-node-mission">${c.collapsedGroups.has(n.key) ? "＋ 展开分组" : "− 折叠分组"}</p>`;
      } else {
        const hasChildren = this.rows.some(child => this.graph.parent.get(child.id) === a.id);
        const hidden = c.collapsed.has(a.id) ? [...this.model.descendants(a.id, this.graph.parent)].filter(id => this.rows.some(child => child.id === id && this.model.matches(child, c.scope, c.status))).length : 0;
        el.className = `at-node at-status-${a.observation.status}${a.id === c.selected ? " at-selected" : ""}${n.context ? " at-context" : ""}${near && !near.has(a.id) ? " at-dim" : ""}`;
        label = `${this.name(a)}，${this.role(a)}，${this.presence(a)}，${this.model.labels[a.observation.status]}，${a.mission || "任务未提供"}`;
        html = `<div class="at-node-header"><span class="at-avatar">${this.escape(({commander: "C", general: "G", scout: "S", worker: "W", verifier: "V"})[a.role] || "A")}</span><strong>${this.escape(this.name(a))}</strong><span class="at-presence ${a.observation.presence === "online" ? "at-online" : ""}">${this.presence(a)}</span></div><div class="at-node-sub"><span>${this.escape(this.role(a))}${n.context ? " · 上级上下文" : ""}</span>${this.tag(a)}</div><p class="at-node-mission" title="${this.escape(a.mission)}">${this.escape(a.mission || "任务未提供")}</p>${c.grouping === "hierarchy" && hasChildren ? `<button class="at-fold" data-at-fold="${this.escape(a.id)}" aria-label="${c.collapsed.has(a.id) ? "展开" : "折叠"}${this.escape(this.name(a))}的下级" aria-expanded="${!c.collapsed.has(a.id)}">${c.collapsed.has(a.id) ? "+" : "−"}</button>${c.collapsed.has(a.id) ? `<span class="at-fold-count">${hidden} 个匹配的下级已折叠</span>` : ""}` : ""}`;
      }
      el.setAttribute("aria-label", label);
      el.setAttribute("aria-pressed", String(n.id === c.selected));
      if (el.contentHTML !== html) {
        const active = document.activeElement, wasFold = active?.closest("[data-at-fold]") && el.contains(active);
        el.innerHTML = html; el.contentHTML = html;
        if (wasFold) el.querySelector("[data-at-fold]")?.focus({preventScroll: true});
      }
    }
    for (const [id, el] of this.elements) if (!valid.has(id)) { el.remove(); this.elements.delete(id); }
    const shown = this.graph.nodes.filter(n => n.type === "agent" && !n.context).length;
    const contexts = this.graph.nodes.filter(n => n.context).length;
    this.el("canvas-label").textContent = `${c.grouping === "hierarchy" ? "派发拓扑" : "分组视图"}　${shown} / ${this.graph.matched} 个匹配 Agent${contexts ? " · " + contexts + " 个上级上下文" : ""}`;
    const empty = this.el("empty");
    empty.hidden = this.graph.matched > 0;
    empty.replaceChildren();
    if (!empty.hidden) {
      empty.append(element("strong", "", this.rows.length ? "没有符合筛选条件的 Agent" : "还没有登记的 Agent"));
      empty.append(element("p", "", this.rows.length ? "可调整状态或显示范围。" : "打开原会话或派发角色后，真实关系会显示在这里。"));
      empty.append(button(this.rows.length ? "清除筛选" : "＋ 派发角色", "button", () => {
        if (!this.rows.length) this.dispatch();
        else { c.scope = "all"; c.status = "all"; this.rebuild(); this.fit(); }
      }));
    }
  }

  renderEdges() {
    const {width, height} = this.model, near = this.current.focused ? this.related() : null;
    this.el("edges").innerHTML = this.graph.edges.map(e => {
      const s = this.position(e.source), t = this.position(e.target);
      if (!s || !t) return "";
      const x = s.x + width, y = s.y + height / 2, tx = t.x, ty = t.y + height / 2;
      const offset = Math.max(40, Math.abs(tx - x) / 2);
      const path = tx > x ? `M${x},${y}C${x + offset},${y} ${tx - offset},${ty} ${tx},${ty}`
        : `M${s.x + width / 2},${s.y + height}C${s.x + width / 2},${s.y + height + 42} ${t.x + width / 2},${t.y - 42} ${t.x + width / 2},${t.y}`;
      return `<path class="at-edge${near ? near.has(e.source) && near.has(e.target) ? " at-highlight" : " at-dim" : ""}" d="${path}" marker-end="url(#at-arrow)"><title>任务派发${e.count > 1 ? " · 聚合 " + e.count + " 条关系" : ""}</title></path>`;
    }).join("");
  }

  renderDetail() {
    const a = this.selected(), detail = this.el("detail");
    this.el("focus").disabled = this.el("reveal").disabled = !a;
    this.el("selection").textContent = a ? this.presence(a) : "";
    if (!a) { detail.replaceChildren(element("p", "workspace-empty", "选择一个 Agent 查看任务、状态依据与操作。")); this.detailSignature = ""; return; }
    const relations = this.rows.filter(child => this.graph.parent.get(child.id) === a.id);
    const parent = this.rows.find(p => p.id === this.graph.parent.get(a.id));
    const facts = (this.workspaceGraph?.facts || []).filter(f => f.creator === a.id);
    const intents = (this.workspaceGraph?.intents || []).filter(i => i.creator === a.id);
    const visible = this.elements.has(a.id);
    const signature = JSON.stringify([a, parent?.name, relations.map(r => [r.id, this.name(r), r.observation.status]), facts, intents, visible]);
    if (signature === this.detailSignature) return;
    const same = this.detailId === a.id, scroll = same ? detail.scrollTop : 0;
    this.detailId = a.id; this.detailSignature = signature;
    const card = this.renderCard(a);
    card.classList.remove("child");
    card.querySelector(".agent-title .badge")?.remove();
    const observation = element("div", "at-observation");
    observation.innerHTML = `<div class="at-detail-badges">${this.tag(a)}<span>${this.presence(a)}</span></div><p>${this.escape(a.observation.evidence)}</p><dl><dt>最近活动</dt><dd>${a.observation.last ? this.escape(time(new Date(a.observation.last).toISOString())) : "暂无可核验时间"}</dd>${a.observation.presence === "offline" ? `<dt>退出前状态</dt><dd>${this.model.labels[a.observation.execution]}</dd>` : ""}</dl>${a.observation.attentionReason ? `<p class="at-warning">${this.escape(a.observation.attentionReason)}</p>` : ""}${!visible ? '<p class="at-warning">当前节点被筛选或折叠隐藏，可用“定位节点”显示。</p>' : ""}`;
    card.querySelector(".agent-title")?.after(observation);
    // The reused card owns all existing control, resume, stop and delete behavior.
    detail.replaceChildren(card);
    const rel = element("section", "at-detail-section");
    rel.append(element("h3", "", "派发关系"));
    rel.append(element("p", "muted", "上级"));
    if (parent) rel.append(button(this.name(parent), "at-relation", () => this.select(parent.id, true)));
    else rel.append(element("p", "", a.parent_agent_id ? "原上级未登记或不可见；未推断替代关系" : "独立会话，未登记上级"));
    rel.append(element("p", "muted", `直接下级 · ${relations.length}`));
    const children = element("div", "at-relations");
    for (const child of relations) children.append(button(this.name(child), "at-relation", () => this.select(child.id, true)));
    rel.append(children);
    detail.append(rel);
    const progress = element("section", "at-detail-section");
    progress.append(element("h3", "", "任务进度"), element("p", "muted", "当前未提供可核验的完成进度，不估算百分比。"));
    detail.append(progress);
    const log = element("section", "at-detail-section");
    log.append(element("h3", "", "最近活动摘要"));
    log.append(element("p", "at-log", a.activity?.latest_message || a.commander_monitor?.reason || a.stop_reason || "暂无活动摘要；可查看原终端或运行日志。"));
    detail.append(log);
    detail.scrollTop = scroll;
  }

  syncControls() {
    const c = this.current;
    this.root.querySelectorAll("[data-at-scope]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.atScope === c.scope)));
    this.el("status").value = c.status;
    this.el("grouping").value = c.grouping;
    this.el("expand").textContent = c.grouping === "hierarchy" ? c.collapsed.size ? "展开全部" : "折叠分支" : c.collapsedGroups.size ? "展开分组" : "折叠分组";
    this.el("focus").textContent = c.focused ? "退出聚焦" : "聚焦关联";
    this.el("focus").setAttribute("aria-pressed", String(c.focused));
  }

  bounds(nodes = this.graph.nodes) {
    const ps = nodes.map(n => this.position(n.id)).filter(Boolean), {width, height} = this.model;
    if (!ps.length) return {x: 0, y: 0, w: 600, h: 400};
    const x = Math.min(...ps.map(p => p.x)) - 25, y = Math.min(...ps.map(p => p.y)) - 30;
    return {x, y, w: Math.max(...ps.map(p => p.x + width)) - x + 25, h: Math.max(...ps.map(p => p.y + height + 26)) - y + 25};
  }

  fit(nodes = this.graph.nodes) {
    const canvas = this.el("canvas");
    if (!canvas.clientWidth) { this.current.needsFit = true; return; }
    const b = this.bounds(nodes), c = this.current;
    c.zoom = Math.max(.15, Math.min(1.05, (canvas.clientWidth - 64) / b.w, (canvas.clientHeight - 72) / b.h));
    c.pan = {x: (canvas.clientWidth - b.w * c.zoom) / 2 - b.x * c.zoom, y: (canvas.clientHeight - b.h * c.zoom) / 2 - b.y * c.zoom - 9};
    c.needsFit = false;
    this.transform();
  }

  transform() {
    if (!this.current) return;
    const {pan, zoom} = this.current;
    this.el("world").style.transform = `translate(${pan.x}px,${pan.y}px) scale(${zoom})`;
    this.el("zoom").textContent = Math.round(zoom * 100) + "%";
    this.renderMinimap();
  }

  zoom(factor, x = this.el("canvas").clientWidth / 2, y = this.el("canvas").clientHeight / 2) {
    const c = this.current, next = Math.max(.15, Math.min(1.8, c.zoom * factor));
    c.pan.x = x - (x - c.pan.x) * next / c.zoom;
    c.pan.y = y - (y - c.pan.y) * next / c.zoom;
    c.zoom = next; this.transform();
  }

  renderMinimap() {
    if (!this.current) return;
    const b = this.bounds(), c = this.current, canvas = this.el("canvas"), {width, height} = this.model;
    const scale = Math.min(120 / b.w, 74 / b.h), ox = (130 - b.w * scale) / 2 - b.x * scale, oy = (84 - b.h * scale) / 2 - b.y * scale;
    this.mini = {scale, ox, oy};
    this.el("minimap").innerHTML = this.graph.nodes.map(n => {
      const p = this.position(n.id);
      return `<rect class="at-mini-node${n.id === c.selected ? " at-mini-selected" : ""}" x="${ox + p.x * scale}" y="${oy + p.y * scale}" width="${width * scale}" height="${height * scale}" rx="1"/>`;
    }).join("") + `<rect class="at-mini-viewport" x="${ox - c.pan.x / c.zoom * scale}" y="${oy - c.pan.y / c.zoom * scale}" width="${canvas.clientWidth / c.zoom * scale}" height="${canvas.clientHeight / c.zoom * scale}"/>`;
  }

  select(id, reveal = false) {
    const a = this.rows.find(a => a.id === id);
    if (!a) return;
    const c = this.current;
    c.selected = id;
    if (reveal) {
      if (c.focused) {
        // Focus is about real upstream/downstream agents, not synthetic group hubs.
        c.grouping = "hierarchy";
        c.collapsed.delete(id);
      }
      if (!this.model.matches(a, c.scope, c.status)) {
        c.scope = a.observation.status === "offline" ? "all" : "online"; c.status = "all";
        toast("已调整筛选以显示 " + this.name(a));
      }
      this.expandAncestors(id);
      c.collapsedGroups.delete(this.model.groupKey(a, c.grouping));
    }
    this.rebuild();
    if (reveal) {
      if (c.focused) this.fit(this.graph.nodes.filter(n => this.related().has(n.id)));
      else this.center(id);
    }
  }

  center(id) {
    if (!this.elements.has(id)) return;
    const c = this.current, p = this.position(id), {width, height} = this.model;
    c.zoom = Math.max(.7, Math.min(1.1, c.zoom));
    c.pan = {x: this.el("canvas").clientWidth / 2 - (p.x + width / 2) * c.zoom, y: this.el("canvas").clientHeight / 2 - (p.y + height / 2) * c.zoom};
    this.transform();
  }

  hideSearch() {
    this.el("results").hidden = true;
    this.el("search").setAttribute("aria-expanded", "false");
  }

  search() {
    const query = this.el("search").value.trim().toLowerCase(), results = this.el("results");
    results.replaceChildren();
    if (!query) { this.hideSearch(); return; }
    const found = this.rows.filter(a => [a.id, this.name(a), this.role(a), a.mission || ""].some(t => t.toLowerCase().includes(query))).sort(this.model.compare);
    for (const a of found.slice(0, 20)) {
      const b = button("", "at-search-result", () => { this.select(a.id, true); this.hideSearch(); this.el("search").focus(); });
      b.append(element("strong", "", this.name(a)), element("small", "", `${this.role(a)} · ${this.model.labels[a.observation.status]}`));
      results.append(b);
    }
    if (!found.length) results.append(element("p", "", "没有匹配的 Agent"));
    results.hidden = false;
    this.el("search").setAttribute("aria-expanded", "true");
  }

  clickNode(id) {
    const n = this.graph.nodes.find(n => n.id === id);
    if (!n) return;
    if (n.type === "group") {
      const set = this.current.collapsedGroups;
      if (set.has(n.key)) set.delete(n.key); else set.add(n.key);
      this.rebuild(true); this.fit();
    } else this.select(id);
  }

  bind() {
    const canvas = this.el("canvas");
    this.el("dispatch").onclick = this.dispatch;
    this.root.querySelectorAll("[data-at-scope]").forEach(b => b.onclick = () => {
      this.current.scope = b.dataset.atScope; this.current.status = "all"; this.rebuild();
    });
    this.el("status").onchange = e => {
      const c = this.current; c.status = e.target.value;
      if (c.status === "offline") c.scope = "all";
      if (c.status === "idle" && c.scope === "active") c.scope = "online";
      this.rebuild();
    };
    this.el("grouping").onchange = e => {
      const c = this.current; c.grouping = e.target.value; c.focused = false;
      if (c.grouping !== "hierarchy") c.collapsedGroups = new Set(this.rows.map(a => this.model.groupKey(a, c.grouping)));
      this.rebuild(true); this.fit();
    };
    this.el("expand").onclick = () => {
      const c = this.current;
      if (c.grouping === "hierarchy") c.collapsed = c.collapsed.size ? new Set() : new Set(this.rows.filter(a => this.graph.parent.get(a.id)).map(a => a.id));
      else c.collapsedGroups = c.collapsedGroups.size ? new Set() : new Set(this.rows.map(a => this.model.groupKey(a, c.grouping)));
      this.rebuild(true); this.fit();
    };
    this.el("layout").onclick = () => { this.rebuild(true); this.fit(); };
    this.el("fit").onclick = () => this.fit();
    this.el("zoom-in").onclick = () => this.zoom(1.2);
    this.el("zoom-out").onclick = () => this.zoom(1 / 1.2);
    this.el("zoom").onclick = () => this.zoom(1 / this.current.zoom);
    this.el("focus").onclick = () => {
      const c = this.current; c.focused = !c.focused;
      if (c.focused) this.select(c.selected, true); else { this.rebuild(); this.fit(); }
    };
    this.el("reveal").onclick = () => this.select(this.current.selected, true);
    this.el("attention").onclick = () => {
      const list = this.rows.filter(a => a.observation.attention).sort(this.model.compare);
      if (!list.length) return;
      const i = list.findIndex(a => a.id === this.current.selected);
      this.select(list[(i + 1) % list.length].id, true);
    };
    this.el("search").oninput = () => this.search();
    this.el("search").onfocus = () => { if (this.el("search").value) this.search(); };
    this.el("search").onkeydown = e => {
      if (e.key === "Escape") this.hideSearch();
      if (e.key === "ArrowDown") { e.preventDefault(); this.el("results").querySelector("button")?.focus(); }
      if (e.key === "Enter") { e.preventDefault(); this.el("results").querySelector("button")?.click(); }
    };
    this.root.addEventListener("focusout", e => { if (!e.relatedTarget?.closest(".at-search-wrap")) this.hideSearch(); });
    this.root.addEventListener("pointerdown", e => { if (!e.target.closest(".at-search-wrap")) this.hideSearch(); });
    this.el("nodes").addEventListener("click", e => {
      const fold = e.target.closest("[data-at-fold]");
      if (fold) {
        const set = this.current.collapsed, id = fold.dataset.atFold;
        if (set.has(id)) set.delete(id); else set.add(id);
        this.rebuild();
      }
    });
    this.el("nodes").addEventListener("keydown", e => {
      if (e.target.closest("[data-at-fold]")) return;
      if (e.key === "Enter" || e.key === " ") {
        const node = e.target.closest("[data-agent-id]");
        if (node) { e.preventDefault(); this.clickNode(node.dataset.agentId); this.elements.get(node.dataset.agentId)?.focus({preventScroll: true}); }
      }
    });
    canvas.addEventListener("pointerdown", e => {
      if (!this.current || e.button !== 0 || e.target.closest(".at-canvas-tools,.at-minimap,.at-fold,.at-empty")) return;
      const node = e.target.closest("[data-agent-id]");
      this.drag = {id: node?.dataset.agentId, pointerId: e.pointerId, x: e.clientX, y: e.clientY, moved: false,
        pan: {...this.current.pan}, position: node ? {...this.position(node.dataset.agentId)} : null};
      canvas.setPointerCapture(e.pointerId);
      canvas.classList.toggle("at-panning", !node);
    });
    canvas.addEventListener("pointermove", e => {
      const d = this.drag; if (!d) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) d.moved = true;
      if (!d.moved) return;
      if (d.id) {
        const p = {x: d.position.x + dx / this.current.zoom, y: d.position.y + dy / this.current.zoom};
        this.positions().set(d.id, p);
        const node = this.elements.get(d.id);
        if (node) { node.style.left = p.x + "px"; node.style.top = p.y + "px"; }
        this.renderEdges(); this.renderMinimap();
      } else {
        this.current.pan = {x: d.pan.x + dx, y: d.pan.y + dy}; this.transform();
      }
    });
    canvas.addEventListener("pointerup", e => {
      const d = this.drag; if (!d) return;
      this.drag = null; canvas.classList.remove("at-panning");
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
      if (!d.moved && d.id) this.clickNode(d.id);
    });
    canvas.addEventListener("pointercancel", () => { this.drag = null; canvas.classList.remove("at-panning"); });
    canvas.addEventListener("wheel", e => {
      if (!this.current) return;
      e.preventDefault(); const r = canvas.getBoundingClientRect();
      this.zoom(Math.exp(-e.deltaY * .0015), e.clientX - r.left, e.clientY - r.top);
    }, {passive: false});
    canvas.addEventListener("keydown", e => {
      if (e.target !== canvas || !this.current) return;
      const directions = {ArrowLeft: [35, 0], ArrowRight: [-35, 0], ArrowUp: [0, 35], ArrowDown: [0, -35]};
      if (directions[e.key]) { e.preventDefault(); this.current.pan.x += directions[e.key][0]; this.current.pan.y += directions[e.key][1]; this.transform(); }
      else if (["+", "=", "-"].includes(e.key)) { e.preventDefault(); this.zoom(e.key === "-" ? 1 / 1.2 : 1.2); }
      else if (e.key === "Home") { e.preventDefault(); this.fit(); }
      else if (e.key === "Escape" && this.current.focused) { this.current.focused = false; this.rebuild(); }
    });
    this.el("minimap").onpointerdown = e => e.stopPropagation();
    this.el("minimap").onclick = e => {
      if (!this.mini || !this.current) return;
      const r = this.el("minimap").getBoundingClientRect(), {scale, ox, oy} = this.mini;
      this.current.pan = {x: canvas.clientWidth / 2 - ((e.clientX - r.left) * 130 / r.width - ox) / scale * this.current.zoom,
        y: canvas.clientHeight / 2 - ((e.clientY - r.top) * 84 / r.height - oy) / scale * this.current.zoom};
      this.transform();
    };
  }
}
