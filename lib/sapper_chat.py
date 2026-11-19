"""Durable Web messages for the native Claude Code bidirectional JSON stream.

This is a transport, not an agent loop. Claude still owns tools, reasoning,
questions and task strategy. The runner is the only writer to Claude's stdin.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

import sapper
from sapper_context import atomic_private


def native_transcript(agent):
    """Resolve only the recorded native UUID, never the most recent file."""
    sid = agent.get("claude_session_id", "")
    if not re.fullmatch(r"[a-f0-9-]{36}", sid):
        return None
    root = Path(agent.get("claude_config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    paths = list(root.glob("*/" + sid + ".jsonl"))
    return paths[0] if len(paths) == 1 else None


class NativeConversation:
    """Read old native conversations without starting or modifying a session."""
    def __init__(self):
        self.cache = {}

    def messages(self, agent):
        path = native_transcript(agent)
        if path is None:
            return []
        stat = path.stat()
        signature = (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        previous = self.cache.get(agent["id"])
        if previous and previous[0] == signature:
            return previous[1]
        messages = []
        questions = set()
        with path.open("rb") as file:
            for line_number, line in enumerate(file, 1):
                if not line.endswith(b"\n"):
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (not isinstance(event, dict) or event.get("isSidechain") or event.get("isMeta")
                        or event.get("isCompactSummary")
                        or event.get("sessionId", agent["claude_session_id"]) != agent["claude_session_id"]
                        or event.get("type") not in ("user", "assistant")):
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                blocks = message.get("content", [])
                parts = [blocks] if isinstance(blocks, str) else []
                for block in blocks if isinstance(blocks, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif event["type"] == "assistant" and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                        questions.add(block.get("id"))
                        for question in (block.get("input") or {}).get("questions", []):
                            parts.append(question.get("question", ""))
                            parts.extend(option.get("label", "") for option in question.get("options", []))
                    elif event["type"] == "user" and block.get("type") == "tool_result" and block.get("tool_use_id") in questions:
                        answer = block.get("content", "")
                        if isinstance(answer, str):
                            parts.append(answer)
                        elif isinstance(answer, list):
                            parts.extend(b["text"] for b in answer if isinstance(b, dict) and isinstance(b.get("text"), str))
                value = "\n".join(parts)
                if not value.strip() or (event["type"] == "user" and value.startswith("[Sapper wake:")):
                    continue
                messages.append({"id": "native-" + str(event.get("uuid") or line_number),
                    "role": event["type"], "text": value,
                    "at": event.get("timestamp") or agent.get("created_at", ""),
                    "status": "received", "source": "native"})
        self.cache[agent["id"]] = (signature, messages)
        return messages

    @staticmethod
    def page(messages, before=None):
        if before:
            index = next((i for i, m in enumerate(messages) if m["id"] == before), None)
            if index is None:
                raise sapper.FGError("历史消息游标不存在")
            messages = messages[:index]
        return {"messages": messages[-100:], "has_more": len(messages) > 100}


class ChatJournal:
    def __init__(self, state, aid):
        if not re.fullmatch(r"[a-f0-9]{16}", aid):
            raise sapper.FGError("agent ID 不正确")
        self.root = Path(state) / "chats" / aid
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.json"
        self.events = self.root / "events.jsonl"

    @contextlib.contextmanager
    def locked(self):
        with (self.root / "state.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text()) if self.path.exists() else {
                    "messages": [], "requests": {}, "phase": "starting"}
                yield data
                atomic_private(self.path, data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def message(data, text, *, internal=False, message_id=None):
        mid = message_id or str(uuid.uuid4())
        old = next((m for m in data["messages"] if m["id"] == mid), None)
        if old:
            if old["text"] != text:
                raise sapper.FGError("同一消息 ID 不能用于不同内容")
            return old
        item = {"id": mid, "role": "internal" if internal else "user", "text": text,
                "at": sapper.now_iso(), "status": "queued"}
        data["messages"].append(item)
        return item

    def enqueue(self, text, message_id=None, *, internal=False):
        if not isinstance(text, str) or not text.strip() or len(text) > 20000 or "\x00" in text:
            raise sapper.FGError("消息需要 1–20000 字符的文本")
        if message_id is not None:
            try:
                message_id = str(uuid.UUID(message_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise sapper.FGError("消息 ID 应为 UUID") from exc
        with self.locked() as data:
            return dict(self.message(data, text.strip(), internal=internal, message_id=message_id))

    def begin(self, rid, initial, *, resume=False):
        with self.locked() as data:
            for item in data["messages"]:
                if item["status"] in ("sending", "sent"):
                    item["status"] = "delivery_unknown"
            data.update(run_id=rid, phase="starting", requests={}, responses=[])
            self.message(data, initial, internal=resume)

    def import_history(self, messages):
        with self.locked() as data:
            known = {m["id"] for m in data["messages"]}
            history = [dict(m) for m in messages if m["id"] not in known]
            data["messages"] = history + data["messages"]

    def inbox(self):
        """Receipt means included in model context, not that an order was obeyed."""
        with self.locked() as data:
            pending = [m for m in data["messages"] if m["status"] == "queued"]
            for m in pending:
                m["status"] = "provided"
            return [dict(m) for m in pending]

    def view(self, before=None):
        with self.locked() as data:
            visible = [m for m in data["messages"] if m["role"] != "internal"]
            if before:
                index = next((i for i, m in enumerate(visible) if m["id"] == before), None)
                if index is None:
                    raise sapper.FGError("历史消息游标不存在")
                visible = visible[:index]
            return {"messages": visible[-100:], "has_more": len(visible) > 100,
                    "requests": list(data["requests"].values()), "phase": data["phase"],
                    "run_id": data.get("run_id"), "error": data.get("error")}

    def answer(self, request_id, run_id, *, answers=None, allow=None):
        with self.locked() as data:
            req = data["requests"].get(request_id)
            if not req or req["run_id"] != run_id or data.get("run_id") != run_id:
                raise sapper.FGError("问题已结束或会话已恢复，请刷新后回答")
            original = req["input"]
            if req["tool_name"] == "AskUserQuestion":
                questions = original.get("questions", [])
                expected = {q["question"] for q in questions}
                if (not questions or not isinstance(answers, dict) or set(answers) != expected
                        or any(not isinstance(v, str) or not v.strip() or len(v) > 20000
                               or "\x00" in v for v in answers.values())):
                    raise sapper.FGError("请回答本次提问中的每个问题")
                response = {"behavior": "allow", "updatedInput": {**original, "answers": answers}}
                summary = "\n".join(f"{q}\n{v}" for q, v in answers.items())
            else:
                if type(allow) is not bool:
                    raise sapper.FGError("请选择允许或拒绝")
                response = ({"behavior": "allow", "updatedInput": original} if allow else
                            {"behavior": "deny", "message": "用户拒绝了本次工具操作"})
                summary = ("允许：" if allow else "拒绝：") + req["tool_name"]
            # Removing the pending request and queuing its response are one write.
            data.setdefault("responses", []).append({"type": "control_response", "response": {
                "subtype": "success", "request_id": request_id, "response": response}})
            del data["requests"][request_id]
            item = self.message(data, summary)
            item.update(status="answered", response_only=True, run_id=run_id)
            return dict(item)

    def outgoing(self, rid):
        """Claim once before writing; a lost process must not replay uncertain input."""
        with self.locked() as data:
            if data.get("run_id") != rid:
                return []
            frames = data.pop("responses", [])
            if frames:
                data["phase"] = "responding"
            # A correction stays queued while a tool question awaits an answer.
            if data["requests"]:
                return frames
            for item in data["messages"]:
                if item["role"] in ("user", "internal") and item["status"] == "queued":
                    item.update(status="sending", run_id=rid)
                    data["phase"] = "responding"
                    frames.append({"type": "user", "uuid": item["id"],
                                   "message": {"role": "user", "content": item["text"]},
                                   "parent_tool_use_id": None, "session_id": ""})
            return frames

    def sent(self, rid, frames):
        ids = {f["uuid"] for f in frames if f["type"] == "user"}
        if ids:
            with self.locked() as data:
                for item in data["messages"]:
                    if item["id"] in ids and item.get("run_id") == rid and item["status"] == "sending":
                        item["status"] = "sent"

    def receive(self, rid, sid, event):
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        at = sapper.now_iso()
        with self.locked() as data:
            if data.get("run_id") != rid:
                return
            # Persist raw protocol separately from the rendered conversation.
            with self.events.open("a", encoding="utf-8") as file:
                file.write(json.dumps({**event, "timestamp": at, "sessionId": sid,
                                       "runId": rid}, ensure_ascii=False) + "\n")
            if kind == "user":
                for item in data["messages"]:
                    if item["id"] == event.get("uuid"):
                        item["status"] = "accepted"
            elif kind == "assistant":
                msg = event.get("message") or {}
                blocks = msg.get("content", [])
                value = "\n".join(b.get("text", "") for b in blocks
                                  if isinstance(b, dict) and b.get("type") == "text")
                if value:
                    mid = str(msg.get("id") or event.get("uuid") or uuid.uuid4())
                    old = next((m for m in data["messages"] if m["id"] == mid), None)
                    item = {"id": mid, "role": "assistant", "text": value, "at": at,
                            "status": "received", "run_id": rid}
                    if old:
                        old.update(item)
                    else:
                        data["messages"].append(item)
                data["phase"] = "responding"
            elif kind == "result":
                data["phase"] = "idle"
                if event.get("is_error"):
                    data["error"] = event.get("result") or "本轮执行失败，请查看运行记录"
                else:
                    data.pop("error", None)
            elif kind == "control_request":
                req = event.get("request", {})
                if req.get("subtype") == "can_use_tool":
                    key = event["request_id"]
                    data["requests"][key] = {"id": key, "run_id": rid, "at": at,
                        "tool_name": req.get("tool_name", "unknown"), "input": req.get("input", {})}
                    data["phase"] = "waiting_user"
                    if req.get("tool_name") == "AskUserQuestion":
                        questions = req.get("input", {}).get("questions", [])
                        value = "\n\n".join(q.get("question", "") + "\n" +
                            "\n".join(o.get("label", "") for o in q.get("options", [])) for q in questions)
                        data["messages"].append({"id": "question-" + key, "role": "assistant", "text": value,
                                                 "at": at, "status": "received", "run_id": rid})
            elif kind == "control_cancel_request":
                data["requests"].pop(event.get("request_id"), None)

    def end(self, rid):
        with self.locked() as data:
            if data.get("run_id") == rid:
                data.update(phase="offline", requests={}, responses=[])
