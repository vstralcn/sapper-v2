"""Control-plane routing ledger tests — frozen spec v1.0 四."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from sapper_routing import RoutingError, RoutingLedger, ack_deadline


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = RoutingLedger(self.temp.name, "workspace-1")
        self.ledger.set_top("commander-1")

    def tearDown(self):
        self.temp.cleanup()

    def emit(self, superior="c" * 16, source="worker-1", kind="crashed"):
        return self.ledger.emit(kind=kind, generation=3, source_agent_id=source,
                                superior_id=superior, cwd="/tmp/task",
                                side_effect_warning="may_have_written_files",
                                level="worker", evidence={"exit_code": 1})

    def test_deadline_by_level_and_kind(self):
        self.assertLess(ack_deadline("worker", "startup_timeout"),
                        ack_deadline("worker", "crashed"))
        self.assertLess(ack_deadline("worker", "crashed"),
                        ack_deadline("commander", "crashed"))

    def test_primary_notice_and_no_business_text(self):
        eid = self.emit()
        view = self.ledger.view(eid)
        self.assertEqual(view["owner_agent_id"], "c" * 16)
        self.assertEqual(view["superior_agent_id"], "c" * 16)
        self.assertEqual(view["status"], "pending")
        self.assertEqual(view["generation"], 3)
        self.assertEqual(view["evidence"]["exit_code"], 1)
        # evidence is structural; no mission/description field exists
        self.assertNotIn("description", view)
        self.assertNotIn("mission", view)

    def test_audit_list_is_readonly_newest_first_and_carries_lifecycle(self):
        primary = self.emit()
        self.ledger.delivered(primary)
        self.ledger.claim(primary, "c" * 16)
        items = self.ledger.audit_list()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertTrue(item["audit_only"])
        self.assertEqual(item["status"], "audit")
        self.assertEqual(item["owner_agent_id"], "commander-1")
        self.assertEqual(item["source_agent_id"], "worker-1")
        self.assertEqual(item["kind"], "crashed")
        actions = [step["action"] for step in item["trace"]]
        self.assertIn("created", actions)
        self.assertIn("delivered", actions)
        self.assertIn("claimed", actions)
        # Reading the audit list never claims, closes or escalates anything.
        self.assertEqual(self.ledger.view(primary)["status"], "claimed")
        self.assertEqual(len(self.ledger.audit_list(limit=0)), 0)

    def test_only_current_owner_may_confirm(self):
        eid = self.emit()
        self.ledger.delivered(eid)
        with self.assertRaisesRegex(RoutingError, "只有当前事件归属者"):
            self.ledger.claim(eid, "worker-1")  # source cannot ack its own event
        self.ledger.claim(eid, "c" * 16)
        self.assertEqual(self.ledger.view(eid)["status"], "claimed")

    def test_late_confirmation_after_upshift_is_rejected(self):
        eid = self.emit()
        self.ledger.delivered(eid)
        self.ledger.escalate(eid, reason="ack_timeout")
        self.assertEqual(self.ledger.view(eid)["owner_agent_id"], "commander-1")
        with self.assertRaisesRegex(RoutingError, "已上移"):
            self.ledger.claim(eid, "c" * 16)  # displaced owner is late
        self.ledger.claim(eid, "commander-1")

    def test_reassign_and_close_confirm_via_resolves_event_id(self):
        eid = self.emit()
        self.ledger.delivered(eid)
        self.ledger.claim(eid, "c" * 16)
        self.ledger.close(eid, resolves_event_id=eid, actor="c" * 16)
        self.assertEqual(self.ledger.view(eid)["status"], "closed")

    def test_delivery_failure_escalate_immediately(self):
        eid = self.emit()
        # Not marked delivered; escalation can still be forced on delivery failure
        self.ledger.escalate(eid, reason="primary_delivery_failed")
        self.assertEqual(self.ledger.view(eid)["owner_agent_id"], "commander-1")

    def test_due_requires_delivery_and_elapsed_deadline(self):
        eid = self.emit()
        self.assertNotIn(eid, self.ledger.due(now=time.time() + 10000))  # not delivered
        self.ledger.delivered(eid)
        self.assertIn(eid, self.ledger.due(now=time.time() + 10000))

    def test_upshift_on_superior_failure_moves_all_active_events(self):
        e1 = self.emit(superior="c" * 16)
        e2 = self.emit(superior="c" * 16, source="worker-2")
        moved = self.ledger.upshift("c" * 16, "commander-1")
        self.assertIn(e1, moved)
        self.assertIn(e2, moved)
        for eid in (e1, e2):
            self.assertEqual(self.ledger.view(eid)["owner_agent_id"], "commander-1")

    def test_trace_records_full_lifecycle(self):
        eid = self.emit()
        self.ledger.delivered(eid)
        self.ledger.claim(eid, "c" * 16)
        actions = [r["action"] for r in self.ledger.trace(eid)]
        self.assertEqual(actions, ["created", "delivered", "claimed"])

    def test_claim_freezes_escalation_clock(self):
        eid = self.emit()
        self.ledger.delivered(eid)
        self.ledger.claim(eid, "c" * 16)
        # Frozen spec v1.0 四: confirmation stops the escalation clock.
        self.assertNotIn(eid, self.ledger.due(now=time.time() + 10000))

    def test_emit_writes_read_only_audit_copy_owned_by_top(self):
        eid = self.emit()
        copy = self.ledger.view("audit-" + eid)
        self.assertTrue(copy["audit_only"])
        self.assertEqual(copy["parent_event_id"], eid)
        self.assertEqual(copy["status"], "audit")
        self.assertEqual(copy["owner_agent_id"], "commander-1")
        # The audit copy is never an actionable todo.
        self.assertNotIn("audit-" + eid, self.ledger.due(now=time.time() + 10000))

    def test_escalation_walks_real_ancestor_chain(self):
        # Agent records: worker -> general -> commander(top).
        agents = self.ledger.state / "agents"
        agents.mkdir(exist_ok=True)
        (agents / ("w" * 16 + ".json")).write_text(json.dumps({"parent_agent_id": "g" * 16}))
        (agents / ("g" * 16 + ".json")).write_text(json.dumps({"parent_agent_id": "cmd" * 16}))
        self.ledger.set_top("cmd" * 16)
        eid = self.ledger.emit(kind="crashed", generation=1, source_agent_id="w" * 16,
                               superior_id="g" * 16, cwd="/t", level="worker",
                               side_effect_warning="dirty")
        self.ledger.delivered(eid)
        self.ledger.escalate(eid, reason="ack_timeout")
        self.assertEqual(self.ledger.view(eid)["owner_agent_id"], "cmd" * 16)
        # Second escalation to the top freezes the clock (no churn).
        self.assertTrue(self.ledger.view(eid).get("escalation_frozen"))

    def test_unrelated_confirmation_is_rejected(self):
        e1 = self.emit(source="s1")
        e2 = self.emit(source="s2")
        self.ledger.delivered(e1)
        self.ledger.delivered(e2)
        # Owner of e1 may not close unrelated e2 via e1's resolves id.
        with self.assertRaisesRegex(RoutingError, "无关联"):
            self.ledger.close(e2, resolves_event_id=e1, actor="c" * 16)


class FailureWiringTests(unittest.TestCase):
    """classify_exit -> _report_failure -> emit + delivered (frozen v1.0 三/四)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        (self.state / "agents").mkdir(parents=True)
        (self.state / "main-agents").mkdir(parents=True)
        (self.state / "runs" / ("a" * 16)).mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, path, data):
        path.write_text(json.dumps(data))

    def test_crashed_exit_emits_and_delivers_structural_event(self):
        from sapper_web import _report_failure
        rid = "a" * 16
        run_dir = self.state / "runs" / rid
        # host.json marks a headless run; role + generation come from it.
        self._write(run_dir / "host.json", {"pid": 999999, "generation": 5,
                    "role": "worker", "cwd": "/task", "startup_timeout": 90})
        # No process tree -> reap confirms physical cleanup (nothing to kill).
        self._write(run_dir / "run.json", {"id": rid, "status": "running"})
        self._write(self.state / "agents" / ("b" * 16 + ".json"),
                    {"id": "b" * 16, "role": "worker", "parent_agent_id": "c" * 16})
        self._write(self.state / "main-agents" / "workspace-1.json", {"agent_id": "commander-1"})
        info = {"id": rid, "agent_id": "b" * 16, "project_id": "workspace-1",
                "role": "worker", "cwd": "/task", "exit_code": 1}
        verdict = _report_failure(run_dir, info, state=str(self.state), ready=True,
                                  last_heartbeat=None, started=time.monotonic() - 5,
                                  reason=None)
        self.assertEqual(verdict["status"], "crashed")
        self.assertTrue(info.get("routing_event_id"))
        ledger = RoutingLedger(self.state, "workspace-1")
        event = ledger.view(info["routing_event_id"])
        self.assertEqual(event["kind"], "crashed")
        self.assertEqual(event["owner_agent_id"], "c" * 16)  # primary -> superior
        self.assertEqual(event["generation"], 5)
        self.assertTrue(event["delivered"])
        # Audit copy is separate and read-only; never an actionable notice.
        self.assertEqual(event["audit_only"], False)
        self.assertTrue(ledger.trace(event["event_id"]))

    def test_clean_stop_does_not_emit_failure(self):
        from sapper_web import _report_failure
        rid = "a" * 16
        run_dir = self.state / "runs" / rid
        self._write(run_dir / "host.json", {"pid": 999999, "generation": 5,
                    "role": "worker", "cwd": "/task"})
        info = {"id": rid, "agent_id": "b" * 16, "project_id": "workspace-1",
                "role": "worker", "cwd": "/task", "exit_code": -15}
        verdict = _report_failure(run_dir, info, state=str(self.state), ready=True,
                                  last_heartbeat=None, started=time.monotonic() - 5,
                                  reason="stopped")
        self.assertIsNone(verdict)

    def test_delivery_failure_escalates_immediately_in_production_path(self):
        from unittest import mock
        from sapper_web import _report_failure
        from sapper_chat import ChatJournal
        rid = "a" * 16
        run_dir = self.state / "runs" / rid
        self._write(run_dir / "host.json", {"pid": 999999, "generation": 5,
                    "role": "worker", "cwd": "/task", "startup_timeout": 90})
        self._write(run_dir / "run.json", {"id": rid, "status": "running"})
        # worker(b) -> general(c) -> commander(d top)
        self._write(self.state / "agents" / ("b" * 16 + ".json"),
                    {"id": "b" * 16, "role": "worker", "parent_agent_id": "c" * 16})
        self._write(self.state / "agents" / ("c" * 16 + ".json"),
                    {"id": "c" * 16, "role": "general", "parent_agent_id": "d" * 16})
        self._write(self.state / "main-agents" / "workspace-1.json", {"agent_id": "d" * 16})
        info = {"id": rid, "agent_id": "b" * 16, "project_id": "workspace-1",
                "role": "worker", "cwd": "/task", "exit_code": 1}
        real_enqueue = ChatJournal.enqueue
        attempts = []
        def flaky(self_obj, *args, **kwargs):
            attempts.append(self_obj.root.name)
            if self_obj.root.name == "c" * 16:  # first owner: delivery fails
                raise OSError("delivery failed")
            return real_enqueue(self_obj, *args, **kwargs)
        with mock.patch.object(ChatJournal, "enqueue", flaky):
            verdict = _report_failure(run_dir, info, state=str(self.state), ready=True,
                                      last_heartbeat=None, started=time.monotonic() - 5,
                                      reason=None)
        self.assertEqual(verdict["status"], "crashed")
        ledger = RoutingLedger(self.state, "workspace-1")
        event = ledger.view(info["routing_event_id"])
        # Escalated past the failing general to the top commander, delivered.
        self.assertEqual(event["owner_agent_id"], "d" * 16)
        self.assertTrue(event["delivered"])
        self.assertIn("c" * 16, attempts)
        self.assertIn("d" * 16, attempts)


if __name__ == "__main__":
    unittest.main()
