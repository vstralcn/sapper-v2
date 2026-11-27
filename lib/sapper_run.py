"""Observe native Claude stream-json output without replacing its agent loop.

Activity is evidence of events, never an assertion of useful progress.
The reader is the sole writer of activity.json; run.json belongs to the runner.
Stage three only applies to the headless bidirectional (chat) mode: those runs
persist frames/events/attachments.  One-shot print runs keep the original
readable activity.log and must not be dragged into the new data contract.
"""
from __future__ import annotations

import json
import threading

import sapper
from sapper_turns import TurnStream


class RunOutput:
    """Operational phase state plus either the structured stream or legacy log."""

    def __init__(self, directory, persist, *, structured=True):
        self.directory = directory
        self.persist = persist
        self.structured = structured
        self.activity = {"events": 0, "result_received": False, "phase": "unknown"}
        self.tools = {}
        self.lock = threading.Lock()
        if structured:
            self.stream = TurnStream(directory)
        else:
            (directory / "activity.log").touch(mode=0o600)
        self.persist(directory / "activity.json", self.activity)

    def note(self, message):
        if self.structured:
            # Operational status already goes to stderr; protocol noise goes
            # to diagnostics and payloads go to the normalized event stream.
            return
        with self.lock, (self.directory / "activity.log").open("a", encoding="utf-8") as log:
            log.write(message.rstrip() + "\n")

    @property
    def settled(self):
        return self.stream.settled if self.structured else True

    def settle(self, terminal_state):
        return self.stream.settle(terminal_state) if self.structured else False

    def observe(self, line):
        if self.structured:
            self.stream.ingest(line)
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            if not self.structured:
                self.note(line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line)
            return
        if not isinstance(event, dict):
            if not self.structured:
                self.note(str(event))
            return
        at = sapper.now_iso()
        self.activity.update(last_event_at=at, events=self.activity["events"] + 1)
        kind = event.get("type")
        if kind in ("assistant", "user"):
            message = event.get("message") or {}
            blocks = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(blocks, list):
                blocks = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name", "unknown"))
                    self.tools[block.get("id")] = name
                    if not self.structured:
                        args = json.dumps(block.get("input", {}), ensure_ascii=False)
                        self.note(f"[{at}] 工具调用 {name}: {args[:1000]}")
                    self.activity.update(last_tool=name, last_tool_at=at)
                elif block.get("type") == "tool_result":
                    name = self.tools.pop(block.get("tool_use_id"), "tool")
                    if not self.structured:
                        status = "错误" if block.get("is_error") else "返回"
                        self.note(f"[{at}] 工具{status} {name}")
                elif block.get("type") == "text" and block.get("text"):
                    value = str(block["text"])
                    if not self.structured:
                        self.note(value)
                    self.activity["latest_message"] = value[-500:]
            if blocks:
                phase = "waiting_user" if "AskUserQuestion" in self.tools.values() else "tool_running" if self.tools else "responding"
                if kind == "assistant" and message.get("stop_reason") in ("end_turn", "stop_sequence") and not self.tools:
                    phase = "idle"
                self.activity["phase"] = phase
        elif kind == "result":
            self.activity.update(result_received=True, result_is_error=bool(event.get("is_error")), phase="idle")
            if not self.structured:
                self.note(f"[{at}] 本轮结果（{event.get('subtype', 'result')}）")
                if event.get("result"):
                    self.note(str(event["result"]))
        elif kind == "control_request":
            self.activity["phase"] = "waiting_user"
        elif kind == "system" and not self.structured:
            subtype = event.get("subtype", "system")
            self.note(f"[{at}] {subtype}" + (f" · {event.get('error')} · 第 {event.get('attempt')} 次" if subtype == "api_retry" else ""))
        self.persist(self.directory / "activity.json", self.activity)
