"use strict";
const assert = require("node:assert/strict");
const stream = require("../web/turn-stream.js");

const events = [
  {type: "user_input", turn_id: 1, seq: 1, text: "开始"},
  {type: "tool_call", turn_id: 1, seq: 2, call_id: "a", name: "Read", input: {}},
  {type: "tool_call", turn_id: 1, seq: 3, call_id: "b", name: "Bash", input: {}},
  {type: "tool_result", turn_id: 1, seq: 4, call_id: "a", text: "ok"},
  {type: "turn_settlement", turn_id: 1, seq: 5, terminal_state: "crashed", usage: {}},
  {type: "assistant_text", turn_id: 2, seq: 6, text: "下一轮"},
];
const turns = stream.foldEvents(events);
assert.equal(turns.length, 2);
assert.equal(turns[0].id, 1);
assert.equal(turns[0].settlement.terminal_state, "crashed");
assert.equal(turns[1].settlement, null);

const pairs = stream.pairTools(turns[0]);
assert.equal(pairs.length, 2);
assert.equal(pairs[0].result.text, "ok");
assert.equal(pairs[0].label, "已返回");
assert.equal(pairs[1].result, null);
assert.equal(pairs[1].label, "随轮次中止/无返回");
assert.equal(stream.unpairedLabel(null), "执行中");
assert.equal(events.filter(e => e.type === "tool_result").length, 1, "frontend must not fabricate results");

console.log("turn-stream logic OK");
