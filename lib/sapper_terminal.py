"""Authenticated terminal transport over SSE output and literal HTTP input.

The PTY remains owned by tmux. Existing external pipe-pane subscriptions are
never replaced; those panes use an explicitly labelled snapshot fallback.
"""
import base64
import contextlib
import json
import shlex
import time

from sapper_workspace import WorkspaceError
from sapper_auth import AuthError


class TerminalBridge:
    def __init__(self, app):
        self.app = app

    def dimensions(self, pane):
        self.app.pane(pane)
        result = self.app.tmux("display-message", "-p", "-t", pane,
                               "#{pane_width} #{pane_height} #{cursor_x} #{cursor_y} #{pane_pid} #{pane_pipe} #{window_panes}")
        if result.returncode:
            raise WorkspaceError("终端已结束", 404)
        cols, rows, x, y, pid, pipe, count = result.stdout.strip().split()
        return {"cols": int(cols), "rows": int(rows), "x": int(x), "y": int(y),
                "pid": pid, "pipe": pipe == "1", "pane_count": int(count)}

    def prepare(self, pane):
        owner = self.app.pane(pane)
        dims = self.dimensions(pane)
        for path in (self.app.state / "runs").glob("*/run.json"):
            run = json.loads(path.read_text())
            if run.get("session") == owner["session"]:
                return {"path": path.parent / "output.log", "owned": True, "mode": "stream", **dims}
        stream_dir = self.app.state / "terminal-streams"
        stream_dir.mkdir(exist_ok=True, mode=0o700)
        path = stream_dir / f"pane-{pane[1:]}-{dims['pid']}.log"
        if dims["pipe"]:
            # Reuse only our own manifest for this exact pane process.
            if path.exists():
                return {"path": path, "owned": False, "mode": "stream", **dims}
            return {"path": None, "owned": False, "mode": "snapshot", **dims}
        path.touch(mode=0o600)
        result = self.app.tmux("pipe-pane", "-o", "-t", pane, "exec cat >> " + shlex.quote(str(path)))
        if result.returncode:
            raise WorkspaceError("无法采集原终端输出", 409)
        return {"path": path, "owned": False, "mode": "stream", **dims}

    def input(self, pane, data):
        owner = self.app.pane(pane)
        value = data.get("data")
        if not isinstance(value, str) or not value or len(value) > 32768 or "\x00" in value:
            raise WorkspaceError("终端输入不正确")
        if "\x03" in value or value == "\x1b":
            self.app.workspaces.pause_commander_input(owner)
        result = self.app.tmux("send-keys", "-t", pane, "-l", "--", value)
        if result.returncode:
            raise WorkspaceError("终端已结束", 409)
        # Do not record typed text or passwords in the management audit.
        return {"status": "sent"}

    def resize(self, pane, data):
        dimensions = self.dimensions(pane)
        cols, rows = data.get("cols"), data.get("rows")
        if (isinstance(cols, bool) or isinstance(rows, bool) or not isinstance(cols, int)
                or not isinstance(rows, int) or not 20 <= cols <= 400 or not 5 <= rows <= 150):
            raise WorkspaceError("终端尺寸不正确")
        if dimensions["pane_count"] != 1:
            raise WorkspaceError("该窗口有多个 pane，保留原布局；请使用原 tmux 调整", 409)
        window = self.app.tmux("display-message", "-p", "-t", pane, "#{window_id}").stdout.strip()
        result = self.app.tmux("resize-window", "-t", window, "-x", str(cols), "-y", str(rows))
        if result.returncode:
            raise WorkspaceError("终端尺寸调整失败", 409)
        return {"cols": cols, "rows": rows}

    def snapshot(self, pane):
        result = self.app.tmux("capture-pane", "-p", "-e", "-t", pane)
        if result.returncode:
            raise WorkspaceError("终端已结束", 404)
        return {"text": self.app.redact(result.stdout), **self.dimensions(pane)}

    def stream(self, handler, pane, session, cursor=""):
        with self.app.lock:
            source = self.prepare(pane)
        path = source["path"]
        # A stream ID includes the pane process and log name. A cursor from a
        # reused tmux pane/server can never accidentally skip another session.
        stream_id = f"{source['pid']}-{path.parent.name if path else 'snapshot'}"
        previous = handler.headers.get("Last-Event-ID") or cursor
        offset = 0
        continuing = False
        if previous and previous.startswith(stream_id + ":"):
            with contextlib.suppress(ValueError):
                offset = int(previous.rsplit(":", 1)[1])
                continuing = offset >= 0
        size = path.stat().st_size if path and path.exists() else 0
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
                if source["owned"] and size <= 2 * 1024 * 1024:
                    offset = 0
                    send({"type": "reset"})
                else:
                    offset = size
                    send({"type": "snapshot", **self.snapshot(pane)}, f"{stream_id}:{offset}")
            previous_snapshot = None
            heartbeat = time.monotonic()
            last_check = 0
            # Persistent sessions are loaded as fresh records, not shared objects.
            # Recheck on each cycle so logout, reset and expiry also close SSE.
            while handler.session() == session:
                if source["mode"] == "snapshot":
                    snap = self.snapshot(pane)
                    if snap["text"] != previous_snapshot:
                        send({"type": "snapshot", **snap})
                        previous_snapshot = snap["text"]
                    time.sleep(0.8)
                else:
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
                        current = self.dimensions(pane)
                        if current["pid"] != source["pid"]:
                            break
                    except WorkspaceError:
                        # The final log chunk may arrive just after the PTY exits.
                        if path and path.exists() and path.stat().st_size > offset:
                            continue
                        break
                    last_check = now
                if now - heartbeat >= 10:
                    handler.wfile.write(b": heartbeat\n\n")
                    handler.wfile.flush()
                    heartbeat = now
            send({"type": "ended"})
        except (AuthError, BrokenPipeError, ConnectionResetError, OSError, WorkspaceError):
            pass
