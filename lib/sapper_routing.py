"""Control-plane failure-event routing for the frozen v1.0 spec.

The ledger routes structural failure events (STARTUP_TIMEOUT / CRASHED) by
event_id.  It stores NO business text: only ids, ownership, generation, status
and deadlines.  It never decides what a failure means for the task — that
stays with the上级 Agent.

Frozen spec v1.0 四:
- primary notice (待办) -> the assigning superior; read-only audit copy -> top
  commander (non-actionable, full lifecycle trace).
- explicit confirmation via claim / reassign / close, all keyed on event_id or
  resolves_event_id; the control plane only checks that the CURRENT owner
  confirmed, not what the confirmation means.
- ack deadline configured by level x failure type; escalation starts when the
  primary notice is delivered; a delivery failure escalates immediately.
- single ownership: only the current owner's confirmation is accepted; a late
  confirmation from a displaced owner is rejected as "已上移".
- mid-level superior failure: all its subordinates' events and ownership move
  up to its superior, ultimately the top commander.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
from pathlib import Path
import time
import uuid

import sapper

# Ack deadlines by level x failure type (seconds).  STARTUP_TIMEOUT is short
# (no side effects, safe to redispatch); CRASHED is longer (scene may be dirty).
ACK_DEADLINE = {
    ("worker", "startup_timeout"): 60,
    ("worker", "crashed"): 180,
    ("general", "startup_timeout"): 90,
    ("general", "crashed"): 240,
    ("commander", "startup_timeout"): 120,
    ("commander", "crashed"): 300,
}
DEFAULT_ACK_DEADLINE = 120

ACTIVE = ("pending", "claimed")
TERMINAL = ("closed", "abandoned")


def ack_deadline(level, kind):
    try:
        return int(ACK_DEADLINE.get((level, kind), DEFAULT_ACK_DEADLINE))
    except (TypeError, ValueError):
        return DEFAULT_ACK_DEADLINE


class RoutingError(sapper.FGError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class RoutingLedger:
    """One append-only event log per state root; all writes under an flock."""

    def __init__(self, state, workspace_id):
        self.state = Path(state)
        self.workspace_id = workspace_id
        self.root = Path(state) / "routing" / workspace_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "events.jsonl"
        self.lock = self.root / "events.lock"
        self.top = self.root / "top.json"
        self.audit = self.root / "audit.jsonl"

    @contextlib.contextmanager
    def locked(self):
        with self.lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield self._load()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self):
        events = {}
        if self.path.exists():
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(item, dict) and item.get("event_id"):
                        events[item["event_id"]] = item
        return events

    def _append(self, event):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    @staticmethod
    def _audit_event(event, action, actor, detail=None):
        return {"event_id": event["event_id"], "action": action,
                "at": sapper.now_iso(), "actor": actor, "detail": detail or {}}

    def _write_audit(self, record):
        with self.audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def set_top(self, commander_id):
        with self.locked() as _:
            sapper.atomic_write_json(self.top, {"commander_id": commander_id})

    def _top_id(self):
        try:
            return json.loads(self.top.read_text()).get("commander_id")
        except (OSError, ValueError):
            return None

    def _parent_of(self, agent_id):
        """Read the real parent_agent_id from the control-plane agent record.

        Frozen spec v1.0 四: escalation walks the true command chain.  The
        ledger never interprets business text — it only reads the structural
        parent pointer the control plane already maintains.
        """
        if not agent_id:
            return None
        try:
            record = json.loads((self.state / "agents" / (agent_id + ".json")).read_text())
        except (OSError, ValueError):
            return None
        return record.get("parent_agent_id")

    def _ancestors(self, agent_id):
        """Chain from agent_id upward, with a cycle guard; never includes self."""
        seen = set()
        cursor = self._parent_of(agent_id)
        while cursor and cursor not in seen and cursor != agent_id:
            seen.add(cursor)
            yield cursor
            cursor = self._parent_of(cursor)

    def emit(self, *, kind, generation, source_agent_id, superior_id, cwd,
             side_effect_warning, level="worker", evidence=None):
        """Create a failure event with a primary notice + read-only audit copy.

        Returns the event_id.  Only structural metadata is stored.
        """
        if kind not in ("startup_timeout", "crashed"):
            raise RoutingError("不支持的事件类型")
        event_id = uuid.uuid4().hex
        now = time.time()
        event = {
            "event_id": event_id,
            "parent_event_id": None,
            "owner_agent_id": superior_id,
            "superior_agent_id": superior_id,
            "source_agent_id": source_agent_id,
            "generation": int(generation),
            "kind": kind,
            "level": level,
            "status": "pending",
            "created_at": sapper.now_iso(),
            "created_epoch": now,
            "ack_deadline_epoch": now + ack_deadline(level, kind),
            "cwd": cwd,
            "side_effect_warning": side_effect_warning,
            "evidence": evidence or {},
            "delivered": False,
            "audit_only": False,
        }
        with self.locked() as events:
            events[event_id] = event
            self._append(event)
            self._write_audit(self._audit_event(event, "created", source_agent_id))
            # Frozen spec v1.0 四: a read-only global audit copy goes to the top
            # commander.  It is a non-actionable record (status "audit", never
            # ACTIVE) that carries the full lifecycle, linked to the primary
            # event by parent_event_id so it never creates a todo or a second
            # decision point.
            top = self._top_id()
            if top:
                audit_copy = {
                    "event_id": "audit-" + event_id,
                    "parent_event_id": event_id,
                    "owner_agent_id": top,
                    "superior_agent_id": top,
                    "source_agent_id": source_agent_id,
                    "generation": int(generation),
                    "kind": kind,
                    "level": level,
                    "status": "audit",
                    "audit_only": True,
                    "created_at": sapper.now_iso(),
                    "created_epoch": now,
                    "side_effect_warning": side_effect_warning,
                    "evidence": evidence or {},
                    "cwd": cwd,
                }
                events[audit_copy["event_id"]] = audit_copy
                self._append(audit_copy)
                self._write_audit(self._audit_event(audit_copy, "audit_copy_created", "control_plane",
                                                    {"top": top, "primary": event_id}))
        # Best-effort push of the non-actionable audit copy into the top
        # commander's journal, idempotent by a deterministic message id.  Only
        # valid agent ids can own a ChatJournal; the ledger record above is the
        # authoritative audit copy regardless.
        self._push_audit_copy(event_id)
        return event_id

    def _push_audit_copy(self, event_id):
        """Publish the read-only audit copy to the top commander's journal."""
        import re
        top = self._top_id()
        if not top or not re.fullmatch(r"[a-f0-9]{16}", str(top)):
            return None
        try:
            from sapper_chat import ChatJournal
            copy_id = "audit-" + event_id
            # A stable UUID derived from the primary event id keeps re-emission
            # idempotent.  The text is structural only — no business content.
            token = uuid.uuid5(uuid.NAMESPACE_OID, copy_id)
            return ChatJournal(self.state, top).enqueue(
                "[Sapper 只读审计副本] event_id=%s primary=%s kind=%s" % (
                    copy_id, event_id, self.view(event_id)["kind"]),
                message_id=str(token), internal=True)
        except (OSError, ValueError, RuntimeError, KeyError):
            return None

    def delivered(self, event_id):
        with self.locked() as events:
            event = events.get(event_id)
            if event is None:
                raise RoutingError("事件不存在", 404)
            if event["status"] not in ACTIVE:
                raise RoutingError("事件已终结")
            # Frozen spec v1.0 四: escalation clock starts when the primary
            # notice is delivered, not when the event was created.
            event["delivered"] = True
            event["delivered_at"] = sapper.now_iso()
            event["ack_deadline_epoch"] = time.time() + ack_deadline(event["level"], event["kind"])
            self._append(event)
            self._write_audit(self._audit_event(event, "delivered", event["owner_agent_id"]))

    def _require_owner(self, events, event_id, actor):
        event = events.get(event_id)
        if event is None:
            raise RoutingError("事件不存在", 404)
        if event["status"] not in ACTIVE:
            raise RoutingError("事件已终结")
        if event["owner_agent_id"] != actor:
            raise RoutingError("已上移：只有当前事件归属者可以确认", 409)
        return event

    def claim(self, event_id, actor):
        with self.locked() as events:
            event = self._require_owner(events, event_id, actor)
            event["status"] = "claimed"
            event["claimed_at"] = sapper.now_iso()
            # Frozen spec v1.0 四: an explicit confirmation stops the escalation
            # clock.  Claim is a confirmation that the current owner has taken
            # the event, so it must not keep ticking toward an up-shift.
            event["acknowledged"] = True
            event["escalation_frozen"] = True
            self._append(event)
            self._write_audit(self._audit_event(event, "claimed", actor))

    def _related(self, events, target_id, resolves_event_id):
        """Frozen spec v1.0 四: a confirmation must resolve an event related to
        the one it mutates — itself, an ancestor/descendant, or a sibling from
        the same source.  This stops an owner of event A from confirming an
        unrelated event B and silently taking over B.
        """
        if resolves_event_id == target_id:
            return True
        target = events.get(target_id) or {}
        resolves = events.get(resolves_event_id) or {}
        if not target or not resolves:
            return False
        if target.get("source_agent_id") and target["source_agent_id"] == resolves.get("source_agent_id"):
            return True
        # Walk the parent_event_id chain in both directions.
        for start in (target_id, resolves_event_id):
            seen = set()
            cursor = events.get(start, {}).get("parent_event_id")
            while cursor and cursor not in seen:
                if cursor == (resolves_event_id if start == target_id else target_id):
                    return True
                seen.add(cursor)
                cursor = events.get(cursor, {}).get("parent_event_id")
        return False

    def _confirm_via(self, events, event_id, resolves_event_id, actor):
        """reassign/close carry resolves_event_id and confirm implicitly."""
        event = self._require_owner(events, resolves_event_id, actor)
        if not self._related(events, event_id, resolves_event_id):
            raise RoutingError("resolves_event_id 与目标事件无关联", 409)
        return event

    def reassign(self, event_id, *, resolves_event_id, actor, new_owner_id):
        with self.locked() as events:
            self._confirm_via(events, event_id, resolves_event_id, actor)
            event = events.get(event_id)
            if event is None or event["status"] not in ACTIVE:
                raise RoutingError("目标事件不存在或已终结", 404)
            event["owner_agent_id"] = new_owner_id
            event["status"] = "pending"
            event["ack_deadline_epoch"] = time.time() + ack_deadline(event["level"], event["kind"])
            self._append(event)
            self._write_audit(self._audit_event(event, "reassigned", actor,
                                                {"new_owner": new_owner_id,
                                                 "resolves_event_id": resolves_event_id}))

    def close(self, event_id, *, resolves_event_id, actor):
        with self.locked() as events:
            self._confirm_via(events, event_id, resolves_event_id, actor)
            event = events.get(event_id)
            if event is None or event["status"] not in ACTIVE:
                raise RoutingError("目标事件不存在或已终结", 404)
            event["status"] = "closed"
            event["closed_at"] = sapper.now_iso()
            self._append(event)
            self._write_audit(self._audit_event(event, "closed", actor,
                                                {"resolves_event_id": resolves_event_id}))

    def escalate(self, event_id, *, reason):
        """Move ownership up one level to the superior, rejecting late acks."""
        with self.locked() as events:
            event = events.get(event_id)
            if event is None or event["status"] not in ACTIVE:
                raise RoutingError("事件不存在或已终结", 404)
            # Frozen spec v1.0 四: escalation follows the REAL ancestor chain
            # from the current owner, one level at a time.  The emit-time
            # superior_agent_id and the top commander are only fallbacks when
            # the chain is unavailable (e.g. the owner was deleted).
            owner = event["owner_agent_id"]
            chain = list(self._ancestors(owner))
            if chain:
                superior = chain[0]
            else:
                superior = event["superior_agent_id"]
                if not superior or superior == owner:
                    superior = self._top_id() or owner
            previous = owner
            event["owner_agent_id"] = superior
            event["status"] = "pending"
            # Frozen spec v1.0 四: a notice that goes undelivered to an inferior
            # is escalated immediately, but repeated ticks must not keep churning
            # the same ownership.  Once the event reaches the top commander no
            # further escalation is meaningful — keep it owned there.
            top = self._top_id()
            at_top = bool(top) and superior == top
            event["escalation_depth"] = int(event.get("escalation_depth", 0)) + (0 if at_top else 1)
            if at_top:
                # The top commander is the final owner.  Stop the escalation
                # clock so repeated ticks never churn the same event.
                event["escalation_frozen"] = True
            else:
                event["ack_deadline_epoch"] = time.time() + ack_deadline(event["level"], event["kind"])
            self._append(event)
            self._write_audit(self._audit_event(event, "escalated", "control_plane",
                                                {"from": previous, "reason": reason,
                                                 "at_top": at_top}))

    def upshift(self, failed_agent_id, new_owner_id):
        """Failed superior: move all its owned active events up atomically."""
        moved = []
        with self.locked() as events:
            for event in events.values():
                if event["status"] not in ACTIVE:
                    continue
                if event["owner_agent_id"] == failed_agent_id or \
                        event["superior_agent_id"] == failed_agent_id:
                    previous = event["owner_agent_id"]
                    event["owner_agent_id"] = new_owner_id
                    event["superior_agent_id"] = new_owner_id
                    event["status"] = "pending"
                    event["ack_deadline_epoch"] = time.time() + ack_deadline(event["level"], event["kind"])
                    self._append(event)
                    self._write_audit(self._audit_event(event, "upshifted", "control_plane",
                                                        {"from": previous, "to": new_owner_id}))
                    moved.append(event["event_id"])
        return moved

    def due(self, now=None):
        """Active events whose ack deadline has elapsed (for escalation)."""
        now = time.time() if now is None else now
        with self.locked() as events:
            return [e["event_id"] for e in events.values()
                    if e["status"] in ACTIVE and e.get("delivered")
                    and not e.get("escalation_frozen")
                    and e["ack_deadline_epoch"] <= now]

    def view(self, event_id):
        with self.locked() as events:
            event = events.get(event_id)
            if event is None:
                raise RoutingError("事件不存在", 404)
            return dict(event)

    def trace(self, event_id):
        """Full lifecycle trace of one event (produce -> deliver -> confirm -> escalate)."""
        result = []
        if not self.audit.exists():
            return result
        with self.audit.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if record.get("event_id") == event_id:
                    result.append(record)
        return result
