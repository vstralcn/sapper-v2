"""Structured-output stream transport over SSE and literal HTTP input.

Frozen spec v1.0: there is no tmux and no PTY.  A "pane" here is a presentation
shim over a managed headless run.  Output is the run's structured stdout file;
input is delivered to that run's ChatJournal or as a control signal.  The wire
protocol stays compatible with the existing xterm frontend until the phase-4 UI
switch, but no terminal state is invented or injected into an agent context.
"""
import base64
import contextlib
import json
import time

from sapper_workspace import WorkspaceError
from sapper_auth import AuthError


class TerminalBridge:
    def __init__(self, app):
        self.app = app

    def _run(self, pane):
        rid = pane[4:] if pane.startswith("run-") else pane
        try:
            info = self.app.run_info(rid)
        except Exception as exc:
            raise WorkspaceError("终端不存在或已结束", 404) from exc
        return rid, info

    def dimensions(self, pane):
        from sapper_web import read_json
        rid, _ = self._run(pane)
        stored = read_json(self.app.run_path(rid) / "terminal-dims.json", {})
        # No PTY: logical dimensions are presentation-only state.
        return {"cols": int(stored.get("cols", 80)), "rows": int(stored.get("rows", 24)),
                "x": 0, "y": 0, "pid": "0", "pipe": True, "pane_count": 1}

    def prepare(self, pane):
        rid, info = self._run(pane)
        return {"path": self.app.run_path(rid) / "output.log", "owned": True,
                "mode": "stream", **self.dimensions(pane)}

    def input(self, pane, data):
        value = data.get("data")
        if not isinstance(value, str) or not value or len(value) > 32768 or "\x00" in value:
            raise WorkspaceError("终端输入不正确")
        if "\x03" in value or value == "\x1b":
            # A native interrupt is also an explicit pause of commander turns.
            # Work from the pane dict so this guard never needs a live run.
            self.app.workspaces.pause_commander_input(self.app.pane(pane))
            return {"status": "interrupted"}
        rid, info = self._run(pane)
        if info.get("execution_mode") == "chat" and info.get("agent_id"):
            from sapper_chat import ChatJournal
            ChatJournal(self.app.state, info["agent_id"]).enqueue(value)
        else:
            # Non-chat runs receive literal input through the run's inbox, which
            # the supervisor relays into the child's stdin pipe.
            inbox = self.app.run_path(rid) / "stdin-inbox"
            with inbox.open("ab") as handle:
                handle.write(value.encode("utf-8"))
        return {"status": "sent"}

    def resize(self, pane, data):
        cols, rows = data.get("cols"), data.get("rows")
        if (isinstance(cols, bool) or isinstance(rows, bool) or not isinstance(cols, int)
                or not isinstance(rows, int) or not 20 <= cols <= 400 or not 5 <= rows <= 150):
            raise WorkspaceError("终端尺寸不正确")
        # No PTY to resize; dimensions are presentational state only.
        rid, _ = self._run(pane)
        from sapper import atomic_write_json
        atomic_write_json(self.app.run_path(rid) / "terminal-dims.json", {"cols": cols, "rows": rows})
        return {"cols": cols, "rows": rows}

    def snapshot(self, pane):
        path = self.prepare(pane)["path"]
        text = ""
        if path.exists():
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 200 * 1024))
                text = handle.read().decode("utf-8", "replace")
        return {"text": self.app.redact(text), **self.dimensions(pane)}

    def stream(self, handler, pane, session, cursor=""):
        with self.app.lock:
            source = self.prepare(pane)
        path = source["path"]
        rid = pane[4:] if pane.startswith("run-") else pane
        # A cursor is scoped to one run so a reused ID can never skip another
        # session's output.
        stream_id = "run-%s" % rid
        previous = handler.headers.get("Last-Event-ID") or cursor
        offset = 0
        continuing = False
        if previous and previous.startswith(stream_id + ":"):
            with contextlib.suppress(ValueError):
                offset = int(previous.rsplit(":", 1)[1])
                continuing = offset >= 0
        size = path.stat().st_size if path.exists() else 0
        if offset > size:
            continuing = False
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()

        def send(payload, event_id=None):
            prefix = f"id: {event_id}\n" if event_id else ""
            handler.wfile.write((prefix + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
            handler.wfile.flush()

        try:
            send({"type": "mode", "mode": source["mode"], "cols": source["cols"], "rows": source["rows"]})
            if not continuing:
                if size <= 2 * 1024 * 1024:
                    offset = 0
                    send({"type": "reset"})
                else:
                    offset = size
                    send({"type": "snapshot", **self.snapshot(pane)}, f"{stream_id}:{offset}")
            heartbeat = time.monotonic()
            last_check = 0
            while handler.session() == session:
                with path.open("rb") as file:
                    file.seek(offset)
                    chunk = file.read(65536)
                if chunk:
                    offset += len(chunk)
                    send({"type": "output", "data": base64.b64encode(chunk).decode()}, f"{stream_id}:{offset}")
                else:
                    time.sleep(0.08)
                now = time.monotonic()
                if now - last_check > 1:
                    try:
                        if self.app.run_info(rid)["status"] in self.app_finished():
                            if not chunk:
                                break
                    except WorkspaceError:
                        break
                    last_check = now
                if now - heartbeat >= 10:
                    handler.wfile.write(b": heartbeat\n\n")
                    handler.wfile.flush()
                    heartbeat = now
            send({"type": "ended"})
        except (AuthError, BrokenPipeError, ConnectionResetError, OSError, WorkspaceError):
            pass

    @staticmethod
    def app_finished():
        from sapper_web import FINISHED
        return FINISHED
