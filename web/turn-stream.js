"use strict";
/* Phase 4: structured turn-stream view. Only renders facts already persisted
 * by the backend; an unpaired tool_use is never fabricated into a result. */
(function (root) {
  const TERMINAL = new Set(["failed", "interrupted", "timeout", "crashed"]);
  const EVENT_LABELS = {user_input: "用户输入", assistant_thinking: "助手思考",
    assistant_text: "助手正文", tool_call: "工具调用", tool_result: "工具回显",
    turn_settlement: "轮次结算"};
  const expanded = new Set();

  let focus = null;
  let runId = "";
  let readOnly = true;
  let after = 0;
  let events = [];
  let timer = null;
  let stopped = false;
  let delay = 1200;
  let isTerminal = () => false;

  function foldEvents(list) {
    const byId = new Map();
    for (const event of list) {
      const id = event.turn_id == null ? 0 : event.turn_id;
      if (!byId.has(id)) byId.set(id, {id, events: [], settlement: null});
      const turn = byId.get(id);
      turn.events.push(event);
      if (event.type === "turn_settlement") turn.settlement = event;
    }
    return [...byId.values()].sort((a, b) => a.id - b.id);
  }

  function unpairedLabel(settlement) {
    return settlement && TERMINAL.has(settlement.terminal_state) ? "随轮次中止/无返回" : "执行中";
  }

  function pairTools(turn) {
    const results = new Map();
    for (const event of turn.events) {
      if (event.type === "tool_result") results.set(event.call_id, event);
    }
    return turn.events.filter(event => event.type === "tool_call").map(call => ({
      call, result: results.get(call.call_id) || null,
      label: results.has(call.call_id) ? "已返回" : unpairedLabel(turn.settlement)
    }));
  }

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }

  function usageText(usage) {
    if (!usage || typeof usage !== "object") return "";
    return Object.entries(usage).map(([key, value]) => `${key}: ${value}`).join(" · ");
  }

  function attachmentButton(relative, title) {
    const button = node("button", title || "查看原文");
    button.type = "button";
    let start = 1;
    button.addEventListener("click", async () => {
      try {
        const page = await api(`/runs/${encodeURIComponent(runId)}/attachment?path=${encodeURIComponent(relative)}&start_line=${start}&limit=200`);
        const panel = button.parentElement.querySelector(".attachment-preview");
        if (!panel) return;
        panel.replaceChildren(node("pre", page.text));
        if (page.has_more) {
          const more = node("button", "下一页");
          more.type = "button";
          more.addEventListener("click", () => { start = page.end_line + 1; button.click(); });
          panel.append(more);
        }
      } catch (error) {
        if (typeof toast === "function") toast(error.message, true);
      }
    });
    return button;
  }

  function renderEvent(event) {
    switch (event.type) {
      case "user_input": return node("p", event.text, "turn-user");
      case "assistant_thinking": {
        const details = node("details", undefined, "turn-thinking");
        details.append(node("summary", "助手思考（展开查看）"));
        details.append(node("pre", event.text));
        return details;
      }
      case "assistant_text": return node("p", event.text, "turn-assistant");
      case "tool_call": return null; // rendered together with its paired result
      case "tool_result": return null;
      case "turn_settlement": {
        const bar = node("div", `${event.terminal_state || "unknown"} · ${usageText(event.usage)}`, "turn-settlement");
        if (event.result_preview) bar.append(node("p", event.result_preview));
        return bar;
      }
      default: return null;
    }
  }

  function renderTool(pair) {
    const card = node("article", undefined, "turn-tool");
    card.append(node("header", `${pair.call.name || "工具"} · ${pair.label}`));
    if (pair.call.input !== undefined) card.append(node("pre", JSON.stringify(pair.call.input, null, 2)));
    const result = pair.result;
    if (result) {
      if (result.text !== undefined) card.append(node("pre", result.text));
      if (result.attachment_path) {
        const meta = `${result.total_lines} 行 · ${result.total_bytes} 字节 · ${result.attachment_path}`;
        card.append(node("p", meta));
        card.append(attachmentButton(result.attachment_path));
        card.append(node("div", undefined, "attachment-preview"));
      }
      if (Array.isArray(result.media) && result.media.length) {
        for (const item of result.media) {
          card.append(node("p", `${item.media_type} · ${item.byte_size} 字节 · 帧 ${item.source_frame_seq}`));
        }
      }
    }
    return card;
  }

  function buildTurnBody(turn) {
    const body = node("div", undefined, "turn-body");
    for (const event of turn.events) {
      const rendered = renderEvent(event);
      if (rendered) body.append(rendered);
    }
    for (const pair of pairTools(turn)) body.append(renderTool(pair));
    return body;
  }

  function render(container, list, options) {
    options = options || {};
    const turns = foldEvents(list);
    container.replaceChildren();
    if (!turns.length) {
      container.append(node("p", options.empty || "暂无轮次事件"));
      return;
    }
    turns.forEach((turn, index) => {
      const card = node("article", undefined, "turn-card");
      const header = node("header", `轮次 ${turn.id} · ${EVENT_LABELS[turn.settlement?.type] || "进行中"}`);
      header.type = "button";
      const recent = index >= turns.length - 2;
      const key = `${runId}:${turn.id}`;
      let open = recent || expanded.has(key);
      const bodyHolder = node("div", undefined, "turn-body-holder");
      const toggle = () => {
        open = !open;
        if (open) {
          expanded.add(key);
          // Long histories: inner card DOM is built only on first expansion.
          if (!bodyHolder.firstChild) bodyHolder.append(buildTurnBody(turn));
          bodyHolder.hidden = false;
        } else {
          expanded.delete(key);
          bodyHolder.hidden = true;
        }
      };
      header.addEventListener("click", toggle);
      card.append(header, bodyHolder);
      if (open) {
        bodyHolder.append(buildTurnBody(turn));
      } else {
        bodyHolder.hidden = true;
      }
      container.append(card);
    });
  }

  async function loadAll() {
    const collected = [];
    let cursor = 0;
    for (let page = 0; page < 2000; page++) {
      const result = await api(`/runs/${encodeURIComponent(runId)}/turns?after_seq=${cursor}&limit=1000`);
      collected.push(...result.events);
      if (!result.has_more || !result.events.length) break;
      cursor = result.next_after_seq;
    }
    return collected;
  }

  async function poll() {
    if (!focus || stopped || document.hidden) return;
    try {
      const result = await api(`/runs/${encodeURIComponent(runId)}/turns?after_seq=${after}&limit=1000`);
      if (result.events.length) {
        events.push(...result.events);
        after = result.next_after_seq;
        delay = 1200;
        const container = document.getElementById("turn-stream");
        if (container) render(container, events, {readOnly});
        if (typeof kickStatus === "function") kickStatus();
      } else {
        delay = 3000;
      }
      if (isTerminal() && !result.has_more) stopped = true;
    } catch (error) {
      delay = 3000;
      if (typeof renderConnection === "function") renderConnection(undefined, false);
    }
    schedule();
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    if (!focus || stopped || document.hidden) return;
    timer = setTimeout(poll, delay);
  }

  function kick() {
    if (!focus) return;
    delay = 1200;
    if (timer) clearTimeout(timer);
    timer = setTimeout(poll, 40);
  }

  async function open(agent, options) {
    options = options || {};
    close();
    focus = agent && agent.id;
    runId = options.runId || (agent && agent.run_id) || "";
    readOnly = options.readOnly !== false;
    isTerminal = options.isTerminal || (() => false);
    if (!focus || !runId) return;
    try {
      events = await loadAll();
      after = events.length ? Math.max(...events.map(event => event.seq)) : 0;
      const container = document.getElementById("turn-stream");
      if (container) render(container, events, {readOnly});
      if (isTerminal() && !(await api(`/runs/${encodeURIComponent(runId)}/turns?after_seq=${after}&limit=1`)).has_more) {
        stopped = true;
        return;
      }
    } catch (error) {
      const container = document.getElementById("turn-stream");
      if (container) render(container, [], {empty: error.message});
    }
    schedule();
  }

  function close() {
    if (timer) clearTimeout(timer);
    timer = null;
    focus = null;
    runId = "";
    events = [];
    after = 0;
    stopped = false;
    delay = 1200;
  }

  function resume() {
    if (!focus || stopped) return;
    kick();
  }

  function renderAudit(container, items) {
    if (!container) return;
    container.replaceChildren();
    if (!items || !items.length) {
      container.append(node("p", "暂无故障审计记录"));
      return;
    }
    for (const item of items) {
      const card = node("article", undefined, "audit-item");
      card.append(node("header", `${item.kind || "故障"} · ${item.source_agent_id || "未知来源"}`));
      card.append(node("p", `归属：${item.owner_agent_id || "—"} · ${item.created_at || ""}`));
      if (item.side_effect_warning) card.append(node("p", `副作用警示：${item.side_effect_warning}`));
      const trace = Array.isArray(item.trace) ? item.trace.map(step => step.action).join(" → ") : "";
      card.append(node("p", `轨迹：${trace || "—"}`));
      container.append(card);
    }
  }

  const apiRef = {
    foldEvents, pairTools, unpairedLabel, render, renderAudit,
    open, close, kick, resume, get focus() { return focus; }
  };
  root.TurnStreamUI = apiRef;
  if (typeof module !== "undefined" && module.exports) module.exports = apiRef;
})(typeof globalThis !== "undefined" ? globalThis : this);
