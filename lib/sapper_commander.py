"""Observe and drive managed workspace commanders, without running their strategy.

Native session JSONL is read incrementally and treated as observation, not orders.
Unknown formats fail closed. A wake is verified only by the real board command.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import sapper
from sapper_context import atomic_private

IDLE_SECONDS = 60
ACK_SECONDS = 120
MAX_ATTEMPTS = 2
CONTROLS = {"active", "paused", "waiting_user", "completed"}
WAKE = re.compile(r"\[Sapper wake:([a-f0-9]{32})\]")


class WatchdogLog:
    """State changes immediately, unchanged observations once a minute."""

    def __init__(self):
        self.previous = {}

    def line(self, aid, view, now=None):
        now = time.monotonic() if now is None else now
        wake = view.get("wake") or {}
        board = view.get("last_board") or {}
        summary = {"control": view.get("control"), "phase": view.get("phase"),
                   "verification": view.get("verification"), "wake_id": wake.get("id"),
                   "wake_status": wake.get("status"), "attempt": wake.get("attempt", 0),
                   "last_board_at": board.get("at"), "board_wake_id": board.get("wake_id"),
                   "recoveries": view.get("recoveries", 0), "attention": view.get("attention")}
        signature = json.dumps(summary, sort_keys=True)
        previous = self.previous.get(aid)
        unchanged = previous is not None and previous[1] == signature
        if unchanged and now - previous[0] < 60:
            return None
        self.previous[aid] = (now, signature)
        kind = "heartbeat" if unchanged else "state"
        return "[watchdog] " + kind + " " + json.dumps(
            {"at": sapper.now_iso(), "agent_id": aid, **summary}, ensure_ascii=False)


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


class CommanderMonitor:
    def __init__(self, manager):
        self.manager = manager
        self.app = manager.app

    def path(self, aid):
        self.manager.agent_path(aid)  # Validate before constructing any path.
        return self.app.state / "commander-monitor" / (aid + ".json")

    @contextlib.contextmanager
    def locked(self, aid):
        path = self.path(aid)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.with_suffix(".lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            try:
                data = json.loads(path.read_text()) if path.exists() else {
                    "control": "active", "phase": "unknown", "recoveries": 0}
                yield data
                atomic_private(path, data)
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def view(self, aid):
        path = self.path(aid)
        if not path.exists():
            return {"control": "active", "phase": "unknown", "verification": "not_observed"}
        data = json.loads(path.read_text())
        return {k: data[k] for k in (
            "control", "reason", "phase", "last_event_at", "last_board", "wake",
            "verification", "attention", "recoveries") if k in data}

    def configure(self, aid, control, reason=""):
        if not isinstance(control, str) or control not in CONTROLS:
            raise sapper.FGError("指挥官状态必须为 active / paused / waiting_user / completed")
        if not isinstance(reason, str) or len(reason) > 1000 or "\x00" in reason:
            raise sapper.FGError("状态说明必须为最多 1000 字符的文本")
        with self.manager.lifecycle():
            agent = self.manager.agent(aid)
            if agent["role"] != "commander" or agent.get("legacy") or agent.get("execution_mode") != "tui":
                raise sapper.FGError("仅支持受管指挥官的续轮控制")
            with self.locked(aid) as data:
                data.update(control=control, reason=reason)
                data.pop("attention", None)
                data.pop("wake", None)
                data["recoveries"] = 0
        self.app.audit("commander.control", agent=aid, control=control)
        return self.view(aid)

    def transcript(self, agent):
        sid = agent.get("claude_session_id", "")
        if not re.fullmatch(r"[a-f0-9-]{36}", sid):
            return None
        root = Path(agent.get("claude_config_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        # UUID is authoritative. Never use the newest session in a directory.
        paths = list(root.glob("*/" + sid + ".jsonl"))
        return paths[0] if len(paths) == 1 else None

    def observe(self, agent, data):
        path = self.transcript(agent)
        if path is None:
            data.update(phase="unknown", caught_up=False, verification="transcript_unavailable",
                        attention="未找到唯一的原生会话记录，无法确认空闲；未自动发送或重启")
            return
        stat = path.stat()
        source = [str(path), stat.st_dev, stat.st_ino, agent.get("run_id"), 1]
        if data.get("source") != source or data.get("offset", 0) > stat.st_size:
            data.update(source=source, offset=0, phase="unknown", tools={}, caught_up=False)
            data.pop("accepted_wake_id", None)
            if data.get("wake", {}).get("run_id") not in (None, agent.get("run_id")):
                data.pop("wake", None)
                data["verification"] = "not_observed"
        with path.open("rb") as file:
            file.seek(data["offset"])
            # Bounded work per sweep; never act on a partially scanned history.
            limit = data["offset"] + 4 * 1024 * 1024
            while file.tell() < limit:
                start = file.tell()
                line = file.readline()
                if not line or not line.endswith(b"\n"):
                    file.seek(start)
                    break
                data["offset"] = file.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    data.update(phase="unknown", verification="unreadable_transcript_event")
                    continue
                self.event(agent, data, event)
            data["caught_up"] = file.tell() == path.stat().st_size

    def event(self, agent, data, event):
        if not isinstance(event, dict) or event.get("isSidechain"):
            return
        if event.get("sessionId", agent["claude_session_id"]) != agent["claude_session_id"]:
            return
        kind = event.get("type")
        if kind not in ("user", "assistant"):
            return
        at = timestamp(event.get("timestamp"))
        if at is None:
            data.update(phase="unknown", verification="missing_event_time")
            return
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return
        blocks = message.get("content", [])
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        if not isinstance(blocks, list):
            return
        data["last_event_at"] = at
        tools = data.setdefault("tools", {})
        for block in blocks:
            if not isinstance(block, dict):
                continue
            typ = block.get("type")
            if kind == "user" and typ == "text":
                value = block.get("text", "")
                if not isinstance(value, str):
                    continue
                data.update(phase="responding", tools={})
                data.pop("accepted_wake_id", None)
                tools = data["tools"]
                # Native interrupt receipts must never be auto-continued.
                if value.startswith("[Request interrupted by user"):
                    if at >= (timestamp(agent.get("updated_at")) or 0):
                        data.update(control="paused", reason="用户中断了原生会话", interrupted_at=at)
                elif (data.get("reason") == "用户中断了原生会话" and not WAKE.search(value)
                      and at > data.get("interrupted_at", at)):
                    data.update(control="active", reason="用户已在原生会话中继续")
                wake = data.get("wake", {})
                if (WAKE.search(value) and WAKE.search(value).group(1) == wake.get("id")
                        and at >= wake.get("sent_at", float("inf"))):
                    wake.update(accepted_at=at, status="accepted")
                    data["accepted_wake_id"] = wake["id"]
                    data["verification"] = "input_accepted"
            elif kind == "assistant" and typ == "tool_use":
                tools[block.get("id", "unknown")] = block.get("name", "unknown")
                data["phase"] = "waiting_user" if "AskUserQuestion" in tools.values() else "tool_running"
            elif kind == "user" and typ == "tool_result":
                tools.pop(block.get("tool_use_id"), None)
                data["phase"] = "tool_running" if tools else "responding"
        if kind == "assistant":
            if data.get("accepted_wake_id") and data.get("verification") == "input_accepted":
                data["verification"] = "responded"
            if event.get("isApiErrorMessage"):
                data.update(phase="idle", idle_since=at, tools={}, attention="上一轮因 API 错误结束；续轮仍需真实读板回执确认")
            elif message.get("stop_reason") in ("end_turn", "stop_sequence") and not tools:
                data.update(phase="idle", idle_since=at)
            elif not tools:
                data["phase"] = "responding"

    def event_signature(self, agent, rows):
        scope = agent.get("workspace_scope", [agent["workspace_id"]])
        items = [(a["id"], a.get("run_id"), a.get("status")) for a in rows
                 if a.get("parent_agent_id") == agent["id"]]
        for wid in scope:
            root = Path(self.app.project(wid)["path"]) / ".sapper"
            # Directory metadata catches new HELP/REPORT facts; existing-node edits
            # are caught by the periodic 60s fallback. No graph dumps every 3s.
            for name in ("facts", "hints", "project.json"):
                path = root / name
                items.append((wid, name, path.stat().st_mtime_ns if path.exists() else None))
        return hashlib.sha256(json.dumps(sorted(items, key=repr), sort_keys=True).encode()).hexdigest()

    def record_board(self, aid, run_id, board):
        agent = self.manager.agent(aid)
        if agent["role"] != "commander" or run_id != agent.get("run_id"):
            return
        with self.locked(aid) as data:
            self.observe(agent, data)
            if any(w.get("status") == "unavailable" for w in board.get("workspaces", [])):
                data["attention"] = "看板含不可读取的工作区，尚未确认恢复读板"
                return
            wake = data.get("wake", {})
            verified = (data.get("caught_up") and wake.get("accepted_at") is not None
                        and data.get("accepted_wake_id") == wake.get("id")
                        and (wake.get("run_id") == run_id or wake.get("status") == "accepted" and not wake.get("run_id")))
            receipt = {"agent_id": aid, "run_id": run_id, "at": time.time(),
                       "wake_id": wake.get("id") if verified else None}
            data["last_board"] = receipt
            data["event_signature"] = self.event_signature(agent, board["agents"])
            if verified:
                wake.update(status="board_confirmed", confirmed_at=receipt["at"])
                wake["run_id"] = run_id
                data.update(verification="board_confirmed", recoveries=0)
                data.pop("attention", None)
        self.app.audit("commander.board", agent=aid, run=run_id, wake=receipt["wake_id"])

    def prompt(self, wake_id):
        return (f"[Sapper wake:{wake_id}] 自动续轮。先执行原 worker.sh board --json；"
                "按原任务、范围和授权核对结果并继续调度，不重复已完成工作。"
                "需要用户决定时先执行 worker.sh control waiting_user --reason '具体问题'；"
                "任务完成时执行 worker.sh control completed --reason '完成依据'。"
                "不要只承诺稍后继续。外部 watchdog 负责本轮结束后的下一次唤醒。")

    def tick(self, aid, rows, now=None):
        now = time.time() if now is None else now
        # Same lifecycle lock as stop/delete/launch: cancellation wins before send.
        with self.manager.lifecycle():
            agent = self.manager.agent(aid)
            row = next((a for a in rows if a["id"] == aid), {})
            if row and row.get("run_id") != agent.get("run_id"):
                return  # A concurrent manual resume replaced this snapshot.
            if (agent.get("desired_state") != "running" or agent.get("legacy")
                    or agent.get("execution_mode") != "tui"):
                return
            with self.locked(aid) as data:
                self.observe(agent, data)
                if data.get("control") != "active":
                    return
                scope = agent.get("workspace_scope", [agent["workspace_id"]])
                if scope and all(self.app.project(w)["status"] == "completed" for w in scope):
                    data.update(control="completed", reason="关联工作区均已完成")
                    return
                run = self.app.run_info(agent["run_id"])
                if run["status"] in ("stopping", "stopped", "timed_out") or (self.app.run_path(run["id"]) / "stop_requested").exists():
                    return
                if not row.get("pane_id"):
                    if run["status"] in ("failed", "interrupted", "exited"):
                        if data.get("recoveries", 0) >= MAX_ATTEMPTS:
                            data["attention"] = "进程恢复已达重试上限，请人工检查"
                            return
                        token = uuid.uuid4().hex
                        data["recoveries"] = data.get("recoveries", 0) + 1
                        # Do not hold the monitor lock while launching a process
                        # that may immediately run the board command.
                        data["wake"] = {"id": token, "sent_at": now, "attempt": 1, "status": "launching"}
                    else:
                        return
                else:
                    if not data.get("caught_up"):
                        return
                    wake = data.get("wake", {})
                    pending = wake and wake.get("status") != "board_confirmed"
                    if pending and now - wake["sent_at"] < ACK_SECONDS:
                        return
                    if data.get("phase") != "idle":
                        if pending and now - wake["sent_at"] >= ACK_SECONDS:
                            data["attention"] = "已唤醒但尚无读板回执；正在执行或等待输入，不自动重启"
                        return
                    if pending and wake.get("attempt", 1) >= MAX_ATTEMPTS:
                        data["attention"] = "连续唤醒未收到读板回执，请人工检查"
                        return
                    signature = self.event_signature(agent, rows)
                    changed = data.get("event_signature") not in (None, signature)
                    if not pending and not changed and now - data.get("idle_since", now) < IDLE_SECONDS:
                        return
                    # Never overwrite an unfinished user input line. Transcript
                    # evidence determines idleness; the pane is only a send guard.
                    pane = self.app.capture(row["pane_id"])["text"]
                    prompts = re.findall(r"^\s*❯(.*)$", pane, re.M)
                    if not prompts or prompts[-1].strip():
                        data["attention"] = "等待空闲输入框；未发送或重启"
                        return
                    token = uuid.uuid4().hex
                    data["wake"] = {"id": token, "run_id": run["id"], "sent_at": now,
                                    "attempt": wake.get("attempt", 0) + 1 if pending else 1,
                                    "status": "sent"}
                    data.update(verification="wake_sent", event_signature=signature)
                    # Persist before input can execute board in a separate process.
                    atomic_private(self.path(aid), data)
                    self.app.send(row["pane_id"], {"text": self.prompt(token)})
                    self.app.audit("commander.wake_sent", agent=aid, wake=token)
                    return
            # Crash recovery is separate from healthy-process wakeup.
            resumed = self.manager.resume(aid, {"continuation": self.prompt(token)})
            with self.locked(aid) as data:
                data["wake"]["run_id"] = resumed["id"]
                if data["wake"].get("status") == "launching":
                    data["wake"]["status"] = "sent"
                    data["verification"] = "process_started"
            self.app.audit("commander.recovery_requested", agent=aid, run=resumed["id"], wake=token)
