"""Observe native Claude stream-json output without replacing its agent loop."""
from __future__ import annotations

import json
import threading

import sapper


class RunOutput:
    """Keep the original PTY stream plus a readable log and bounded activity state.

    Activity is evidence of events, never an assertion of useful progress.
    The reader is the sole writer of activity.json; run.json belongs to the runner.
    """

    def __init__(self, directory, persist):
        self.directory = directory
        self.persist = persist
        self.activity = {"events": 0, "result_received": False}
        self.tools = {}
        self.lock = threading.Lock()
        (directory / "activity.log").touch(mode=0o600)
        self.persist(directory / "activity.json", self.activity)

    def note(self, message):
        with self.lock, (self.directory / "activity.log").open("a", encoding="utf-8") as log:
            log.write(message.rstrip() + "\n")

    def observe(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            self.note(line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line)
            return
        if not isinstance(event, dict):
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
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)
                    self.note(f"[{at}] 工具调用 {name}: {args[:1000]}")
                    self.activity.update(last_tool=name, last_tool_at=at)
                elif block.get("type") == "tool_result":
                    name = self.tools.pop(block.get("tool_use_id"), "tool")
                    status = "错误" if block.get("is_error") else "返回"
                    self.note(f"[{at}] 工具{status} {name}")
                elif block.get("type") == "text" and block.get("text"):
                    value = str(block["text"])
                    self.note(value)
                    self.activity["latest_message"] = value[-500:]
        elif kind == "result":
            self.activity.update(result_received=True, result_is_error=bool(event.get("is_error")))
            self.note(f"[{at}] 本轮结果（{event.get('subtype', 'result')}）")
            if event.get("result"):
                self.note(str(event["result"]))
        elif kind == "system":
            subtype = event.get("subtype", "system")
            self.note(f"[{at}] {subtype}" + (f" · {event.get('error')} · 第 {event.get('attempt')} 次" if subtype == "api_retry" else ""))
        self.persist(self.directory / "activity.json", self.activity)
