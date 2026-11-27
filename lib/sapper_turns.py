"""Structured turn-stream persistence for headless stream-json runs.

One contract for the top commander and every headless worker.  Harness keeps
only previews/counts/paths in the normalized event stream; full payloads stay
on disk for the agent to slice with sed/grep/tail when it needs them.

Four physical sinks, each with one job:

* frames.jsonl    raw protocol ledger (valid JSON objects, untruncated)
* turns.jsonl     the only externally consumed normalized event stream
* attachments/    oversized tool-result text as unescaped, read-only files
* diagnostics.jsonl  non-protocol noise and crash fragments

Write order is always attachment -> ledger -> events, so an event can never
point at an attachment that does not exist yet.  The ledger is also the
rebuild source: on takeover, frames that were admitted but not fully projected
are replayed instead of leaving permanent holes in the event stream.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

import sapper

SCHEMA_VERSION = 1
EVENT_TYPES = frozenset({
    "user_input", "assistant_thinking", "assistant_text",
    "tool_call", "tool_result", "turn_settlement",
})
TERMINAL_STATES = frozenset({"completed", "failed", "interrupted", "timeout", "crashed"})


def _setting(name, default, low, high):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


BYTES_THRESHOLD = _setting("SAPPER_TURN_BYTES_THRESHOLD", 64 * 1024, 1, 1024 ** 4)
LINES_THRESHOLD = _setting("SAPPER_TURN_LINES_THRESHOLD", 2000, 1, 10 ** 7)
PREVIEW_CHARS = _setting("SAPPER_TURN_PREVIEW_CHARS", 200, 1, 100000)
PAGE_DEFAULT = _setting("SAPPER_TURN_PAGE_DEFAULT", 200, 1, 100000)
PAGE_MAX = _setting("SAPPER_TURN_PAGE_MAX", 1000, 1, 100000)


def sed_lines(text):
    """Logical line count matching sed: empty=0; trailing newline adds no line."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def iter_frames(stream, chunk_size=65536):
    """Yield whole lines from a binary stream, however large a single line is."""
    buffer = bytearray()
    # read1() returns whatever a single pipe read produced; read(n) would block
    # until n bytes arrive and stall a live stream on a short first frame.
    read_chunk = getattr(stream, "read1", stream.read)
    while True:
        chunk = read_chunk(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            end = buffer.find(b"\n")
            if end < 0:
                break
            yield bytes(buffer[:end + 1])
            del buffer[:end + 1]
    if buffer:
        yield bytes(buffer)  # unterminated fragment; caller must diagnose it


def read_events(path, after_seq=0, limit=None):
    """Pure cursor read: never creates, repairs or migrates any file."""
    after_seq = max(0, int(after_seq or 0))
    limit = PAGE_DEFAULT if limit is None else min(max(1, int(limit)), PAGE_MAX)
    found = []
    try:
        with Path(path).open("rb") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and isinstance(event.get("seq"), int) and event["seq"] > after_seq:
                    found.append(event)
    except FileNotFoundError:
        pass
    page = found[:limit]
    return {
        "events": page,
        "next_after_seq": page[-1]["seq"] if page else after_seq,
        "has_more": len(found) > len(page),
    }


def media_byte_size(data):
    """Exact decoded payload size, tolerant of unexpected JSON field types."""
    if isinstance(data, str):
        try:
            return len(base64.b64decode(data, validate=False))
        except (ValueError, TypeError):
            return len(data.encode("utf-8"))
    if isinstance(data, (bytes, bytearray)):
        return len(data)
    return len(str(data).encode("utf-8"))


class TurnStream:
    def __init__(self, directory, *, bytes_threshold=None, lines_threshold=None,
                 preview_chars=None):
        self.directory = Path(directory)
        self.frames_path = self.directory / "frames.jsonl"
        self.events_path = self.directory / "turns.jsonl"
        self.diagnostics_path = self.directory / "diagnostics.jsonl"
        self.attachments_dir = self.directory / "attachments"
        self.bytes_threshold = bytes_threshold if bytes_threshold is not None else BYTES_THRESHOLD
        self.lines_threshold = lines_threshold if lines_threshold is not None else LINES_THRESHOLD
        self.preview_chars = preview_chars if preview_chars is not None else PREVIEW_CHARS
        self.lock = threading.RLock()
        self.line_no = 0
        self.attachments_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.frames_path, self.events_path, self.diagnostics_path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
        self._cleanup_tmp_attachments()
        self.seq = 0
        self.turn_id = 0
        self.open_turn = False
        self.usage = {}
        (self.frame_seq, self.seq, self.turn_id, self.open_turn,
         self.usage) = self._recover()

    # ---- crash recovery -------------------------------------------------

    def _cleanup_tmp_attachments(self):
        """A crash before rename must not block a later spill with O_EXCL."""
        for tmp in sorted(self.attachments_dir.glob("*.tmp")):
            self._diagnose(tmp.read_bytes()[:4096], "attachment_tmp_fragment", "attachments")
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    @staticmethod
    def _truncate_tail(path, keep):
        data = path.read_bytes()
        lines = data.splitlines(keepends=True)
        if keep == len(lines):
            return
        boundary = sum(len(line) for line in lines[:keep])
        with path.open("r+b") as handle:
            handle.truncate(boundary)

    def _recover_tail(self, path):
        """Drop trailing half-written lines; keep frame_seq == physical line."""
        if not path.exists():
            return 0
        keep = 0
        for raw in path.read_bytes().splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                self._diagnose(raw, "crash_fragment", path.name)
                break
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                self._diagnose(raw, "invalid_tail_line", path.name)
                break
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                self._diagnose(raw, "invalid_tail_line", path.name)
                break
            keep += 1
        self._truncate_tail(path, keep)
        return keep

    def _recover_events(self):
        """Drop trailing bad event lines and return the surviving events."""
        keep = 0
        events = []
        for raw in self.events_path.read_bytes().splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                self._diagnose(raw, "crash_fragment", "turns.jsonl")
                break
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                self._diagnose(raw, "invalid_tail_line", "turns.jsonl")
                break
            if not isinstance(event, dict) or not isinstance(event.get("seq"), int):
                self._diagnose(raw, "invalid_tail_line", "turns.jsonl")
                break
            events.append(event)
            keep += 1
        self._truncate_tail(self.events_path, keep)
        return events

    def _recover(self):
        frame_seq = self._recover_tail(self.frames_path)
        events = self._recover_events()
        seq = max((e["seq"] for e in events if isinstance(e.get("seq"), int)), default=0)
        turn_id = max((e["turn_id"] for e in events if isinstance(e.get("turn_id"), int)), default=0)
        self.seq, self.turn_id = seq, turn_id
        last = events[-1] if events else None
        open_turn = bool(last and last.get("type") != "turn_settlement")

        # Rebuild usage from the raw ledger so a takeover still has the last
        # snapshot of every assistant message (never an empty abnormal usage).
        usage = {}
        frames = []
        for index, raw in enumerate(self.frames_path.read_bytes().splitlines(), 1):
            try:
                frame = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
                continue
            frames.append((index, frame))
            if frame.get("type") == "assistant":
                message = frame.get("message") if isinstance(frame.get("message"), dict) else {}
                snapshot = self._usage_snapshot(message)
                if snapshot:
                    usage[self._message_id(frame, message, index)] = snapshot
            elif frame.get("type") == "result":
                usage.clear()

        # Replay frames that were admitted but not fully projected.  A frame
        # with no conversational blocks legitimately has zero events; nothing
        # is re-emitted for it.  A half-written one-to-many frame is completed.
        existing = {}
        for event in events:
            existing.setdefault(event.get("source_frame_seq"), []).append(event)
        for frame_seq_value, frame in frames:
            try:
                plans = self._plan(frame, frame_seq_value)
            except Exception as exc:  # defensive: never lose the whole takeover
                self._diagnose(str(exc), "projection_replay_failed", "frames.jsonl")
                continue
            emit = [entry for entry in plans if entry[0] != "__usage__"]
            recorded = existing.get(frame_seq_value, [])
            for offset, entry in enumerate(emit):
                if offset < len(recorded) and self._same_event(recorded[offset], entry, frame_seq_value):
                    continue
                self.open_turn = open_turn
                self._emit(entry, frame_seq_value)
                open_turn = self.open_turn
            if any(self._is_settlement(entry[0]) for entry in emit):
                # Result frames close their turn even if already projected.
                if not self._has_authoritative_settlement(recorded):
                    open_turn = False
        self.usage = usage
        return frame_seq, seq, turn_id, open_turn, usage

    @staticmethod
    def _is_settlement(event_type):
        return event_type == "turn_settlement"

    @staticmethod
    def _has_authoritative_settlement(events):
        return any(e.get("type") == "turn_settlement" and e.get("authoritative") for e in events)

    @staticmethod
    def _same_event(recorded, entry, frame_seq):
        event_type, _payload, message_id = entry
        return (recorded.get("type") == event_type
                and recorded.get("source_frame_seq") == frame_seq
                and recorded.get("message_id") == message_id)

    # ---- low-level append ----------------------------------------------

    @staticmethod
    def _append_bytes(path, payload):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_json(self, path, event):
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        self._append_bytes(path, line.encode("utf-8"))

    def _diagnose(self, fragment, reason, source):
        if isinstance(fragment, bytes):
            fragment = fragment.decode("utf-8", errors="replace")
        self._append_json(self.diagnostics_path, {
            "at": sapper.now_iso(), "kind": reason, "source": source,
            "source_line": self.line_no, "fragment": str(fragment).rstrip("\r\n"),
        })

    def diagnose(self, fragment, reason="noise", source="stdout"):
        with self.lock:
            self.line_no += 1
            self._diagnose(fragment, reason, source)

    # ---- ingest ---------------------------------------------------------

    def ingest(self, raw):
        """Admit one raw frame: validate -> spill -> ledger -> events."""
        with self.lock:
            self.line_no += 1
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            line = line.rstrip("\r\n")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                self._diagnose(line, "invalid_json", "stdout")
                return False
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                self._diagnose(line, "not_a_protocol_frame", "stdout")
                return False
            frame_seq = self.frame_seq + 1
            # Field-level errors must never block取证 or consume the next frame.
            try:
                plans = self._plan(event, frame_seq)
            except Exception as exc:
                self._diagnose(f"{type(exc).__name__}: {exc}", "projection_failed", "stdout")
                plans = []
            # Ledger append is unconditional for an admitted frame.  Events can
            # be rebuilt from it on takeover, but a lost ledger frame cannot.
            self._append_bytes(self.frames_path, (line + "\n").encode("utf-8"))
            self.frame_seq = frame_seq
            for entry in plans:
                self._emit(entry, frame_seq)
            return True

    def _message_id(self, event, message, frame_seq):
        return str(message.get("id") or event.get("uuid") or f"m{frame_seq}")

    @staticmethod
    def _blocks(message):
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        return content if isinstance(content, list) else []

    @staticmethod
    def _usage_snapshot(message):
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return None
        keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
        return {key: usage[key] for key in keys
                if isinstance(usage.get(key), (int, float)) and not isinstance(usage[key], bool)}

    def _plan(self, event, frame_seq):
        """Compute events for one admitted frame; may spill attachments."""
        kind = event.get("type")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        mid = self._message_id(event, message, frame_seq)
        plans = []

        if kind == "assistant":
            snapshot = self._usage_snapshot(message)
            if snapshot:
                plans.append(("__usage__", {"message_id": mid, "usage": snapshot}, mid))
            for block in self._blocks(message):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in ("thinking", "redacted_thinking"):
                    text = block.get("thinking") if isinstance(block.get("thinking"), str) else block.get("data")
                    if isinstance(text, str) and text:
                        plans.append(("assistant_thinking", {"text": text}, mid))
                elif block_type == "text" and isinstance(block.get("text"), str) and block["text"]:
                    plans.append(("assistant_text", {"text": block["text"]}, mid))
                elif block_type == "tool_use":
                    plans.append(("tool_call", {
                        "call_id": str(block.get("id") or f"call-{frame_seq}-{len(plans)}"),
                        "name": str(block.get("name", "unknown")),
                        "input": block.get("input", {}),
                    }, mid))
        elif kind == "user":
            for block in self._blocks(message):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    plans.append(("tool_result", self._tool_result(block, frame_seq), mid))
                elif block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                    plans.append(("user_input", {"text": block["text"]}, mid))
        elif kind == "result":
            terminal = "failed" if event.get("is_error") else "completed"
            subtype = str(event.get("subtype") or "")
            if subtype in ("interrupted", "error_interrupted"):
                terminal = "interrupted"
            elif event.get("timed_out") is True:
                terminal = "timeout"
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else self._usage_snapshot(message)
            result_text = event.get("result")
            plans.append(("turn_settlement", {
                "terminal_state": terminal,
                "usage": {k: v for k, v in (usage or {}).items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)},
                "authoritative": True,
                "result_preview": str(result_text)[:self.preview_chars] if result_text else "",
            }, mid))
        # All other frames (system/init, control_*, attachment, stream_event,
        # partial deltas) are evidence only and stay in the raw ledger.
        return plans

    def _tool_result(self, block, frame_seq):
        texts = []
        media = []
        content = block.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif part.get("type") in ("image", "image_url", "document", "audio", "binary"):
                    source = part.get("source") if isinstance(part.get("source"), dict) else {}
                    data = part.get("data", source.get("data", ""))
                    media.append({
                        "media_type": str(part.get("media_type") or source.get("media_type") or part["type"]),
                        "byte_size": media_byte_size(data),
                        "source_frame_seq": frame_seq,
                    })
        merged = "\n".join(texts)
        total_bytes = len(merged.encode("utf-8"))
        total_lines = sed_lines(merged)
        payload = {
            "call_id": str(block.get("tool_use_id") or f"result-{frame_seq}"),
            "status": "error" if block.get("is_error") else "ok",
            "is_error": bool(block.get("is_error")),
            "media": media,
            "total_bytes": total_bytes,
            "total_lines": total_lines,
        }
        if total_bytes > self.bytes_threshold or total_lines > self.lines_threshold:
            name = f"frame-{frame_seq}-{hashlib.sha256(merged.encode('utf-8')).hexdigest()[:12]}.txt"
            final = self.attachments_dir / name
            if not final.exists():
                # Unique temp name: a stale .tmp from a previous crash must not
                # block takeover with O_EXCL.
                tmp = self.attachments_dir / f"{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                        handle.write(merged)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp, final)
                    os.chmod(final, 0o400)  # read-only after publish
                except OSError as exc:
                    self._diagnose(f"{type(exc).__name__}: {exc}", "attachment_spill_failed", "attachments")
                    payload["text"] = merged
            if "text" not in payload:
                payload["head_preview"] = merged[:self.preview_chars]
                if len(merged) > self.preview_chars:
                    payload["tail_preview"] = merged[-self.preview_chars:]
                payload["attachment_path"] = str(final.relative_to(self.directory))
        else:
            payload["text"] = merged
        return payload

    def _emit(self, entry, frame_seq):
        event_type, payload, message_id = entry
        if event_type == "__usage__":
            self.usage[payload["message_id"]] = payload["usage"]
            return
        if not self.open_turn:
            self.turn_id += 1
            self.open_turn = True
        self.seq += 1
        event = {
            "schema_version": SCHEMA_VERSION,
            "seq": self.seq,
            "source_frame_seq": frame_seq,
            "turn_id": self.turn_id,
            "message_id": message_id,
            "type": event_type,
            "timestamp": sapper.now_iso(),
        }
        event.update(payload)
        self._append_json(self.events_path, event)
        if event_type == "turn_settlement":
            self.open_turn = False
            self.usage.clear()

    # ---- terminal settlement -------------------------------------------

    @property
    def settled(self):
        with self.lock:
            return not self.open_turn

    def settle(self, terminal_state, *, authoritative=False, usage=None):
        """Close an open turn when the process died without a result frame."""
        with self.lock:
            if not self.open_turn:
                return False
            if terminal_state not in TERMINAL_STATES:
                raise ValueError(f"unknown terminal state: {terminal_state}")
            if authoritative and isinstance(usage, dict):
                total = {k: v for k, v in usage.items()
                         if isinstance(v, (int, float)) and not isinstance(v, bool)}
            else:
                # One snapshot per message_id: keep the LAST snapshot only.
                total = {}
                for snapshot in self.usage.values():
                    for key, value in snapshot.items():
                        total[key] = total.get(key, 0) + value
            self.seq += 1
            self._append_json(self.events_path, {
                "schema_version": SCHEMA_VERSION,
                "seq": self.seq,
                "source_frame_seq": self.frame_seq,
                "turn_id": self.turn_id or 1,
                "message_id": "",
                "type": "turn_settlement",
                "timestamp": sapper.now_iso(),
                "terminal_state": terminal_state,
                "usage": total,
                "authoritative": bool(authoritative),
            })
            self.open_turn = False
            self.usage.clear()
            return True

    # ---- cursor read ----------------------------------------------------

    def read(self, after_seq=0, limit=None):
        with self.lock:
            return read_events(self.events_path, after_seq, limit)
