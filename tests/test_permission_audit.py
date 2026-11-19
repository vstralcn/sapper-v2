"""Permission observability must describe, never change, the existing decision."""
import json
import contextlib
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_permission_audit import FIELDS, permission_audit
from sapper_workspace import worker
import test_sapper_workspaces as fixtures


class PermissionAuditTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBindingTests.setUp
    tearDown = fixtures.WorkspaceBindingTests.tearDown

    def prepare(self):
        credentials = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fixture-no-network"})
        credentials.start()
        self.addCleanup(credentials.stop)
        self.manager = self.app.workspaces
        self.pid = self.manager.bind({"path": str(self.question)})["id"]
        self.manager.enable(self.pid, {})
        self.manager._capabilities = dict(available=True, session_id=True, resume=True, append_system=True,
            mcp_config=True, strict_mcp_config=True, plugin_dir=True, setting_sources=True, settings=True,
            skill_isolation=True)

    def test_decision_branches_preserve_absence_false_and_parent_provenance(self):
        for data, old, effective, inherited, expected in (
            ({}, None, False, None, (False, None, None, "default")),
            ({"skip_permissions": False}, {"skip_permissions": True}, False, None,
             (True, False, True, "request")),
            ({}, {"skip_permissions": True}, True, None, (False, None, True, "existing_agent")),
            ({"skip_permissions": True}, None, True, "parent_agent", (False, None, None, "parent_agent")),
            ({"skip_permissions": False}, None, False, "default", (False, None, None, "default")),
        ):
            with self.subTest(expected=expected):
                fields = permission_audit({**data, "request_id": "correlated"}, old, effective,
                                          inherited_source=inherited)
                self.assertEqual(tuple(fields[k] for k in ("input_present", "input_skip_permissions",
                                                           "previous_skip_permissions", "permission_source")), expected)
                self.assertEqual(fields["effective_skip_permissions"], effective)
                self.assertEqual(fields["request_id"], "correlated")

    def test_run_is_audited_before_spawn_and_events_match_exact_launch(self):
        self.prepare()
        seen = []

        def tmux(*args, **kwargs):
            if args[0] == "new-session":
                files = list((self.app.state / "runs").glob("*/run.json"))
                run = json.loads(max(files, key=lambda p: p.stat().st_mtime_ns).read_text())
                self.assertTrue(set(FIELDS) <= set(run))
                seen.append(run)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.app, "tmux", side_effect=tmux), mock.patch.object(self.app, "panes", return_value=[]):
            run = self.manager.create_session(self.pid, {"role": "scout", "execution_mode": "tui", "skip_permissions": True,
                                                       "request_id": "first"})
            agent = self.manager.agent(run["agent_id"])
            self.assertTrue(agent["skip_permissions"])
            inherited = self.manager.resume(agent["id"], {"request_id": "second"})
            disabled = self.manager.resume(agent["id"], {"skip_permissions": False, "request_id": "third"})
        self.assertEqual([r["request_id"] for r in seen], ["first", "second", "third"])
        self.assertIn("--dangerously-skip-permissions", inherited["command"])
        self.assertNotIn("--dangerously-skip-permissions", disabled["command"])
        rows = [json.loads(s) for s in (self.app.state / "actions.jsonl").read_text().splitlines()]
        events = [r for r in rows if r["action"] in ("agent.started", "agent.resumed", "agent.dispatched")]
        for event in events:
            actual = next(r for r in seen if r["id"] == event["run"])
            self.assertEqual({k: event[k] for k in FIELDS}, {k: actual[k] for k in FIELDS})
        self.assertEqual([r["permission_source"] for r in seen], ["request", "existing_agent", "request"])
        self.assertIsNone(seen[0]["previous_skip_permissions"])
        self.assertEqual(seen[1]["input_skip_permissions"], None)
        self.assertTrue(seen[2]["previous_skip_permissions"])
        self.assertEqual(sum(e["action"] == "agent.dispatched" for e in events), 1)

    def test_cli_spawn_records_parent_source_but_not_an_explicit_permission_input(self):
        self.prepare()
        parent = {"id": "a" * 16, "workspace_id": self.pid, "role": "commander",
                  "desired_state": "running", "skip_permissions": True, "profile_id": "",
                  "created_at": "2026-11-07T00:00:00Z"}
        self.manager.save_agent(parent)
        basis = self.app.store(self.pid).add_fact("supplied archive", "user")["id"]
        with mock.patch("sapper_workspace.console_from_environment", return_value=self.app), \
             mock.patch.dict(os.environ, {"SAPPER_WORKSPACE_ID": self.pid, "SAPPER_AGENT_ID": parent["id"]}), \
             mock.patch.object(self.app, "tmux", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(self.app, "panes", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as output:
            worker(["spawn", self.pid, "--from", basis, "--request-id", "cli-dispatch"])
        run = json.loads(output.getvalue())
        self.assertEqual(run["permission_source"], "worker_policy")
        self.assertFalse(run["input_present"])
        self.assertIsNone(run["input_skip_permissions"])
        self.assertTrue(run["effective_skip_permissions"])
        self.assertEqual(run["request_id"], "cli-dispatch")

    def test_worker_policy_applies_to_new_and_resumed_sessions(self):
        self.prepare()
        with mock.patch.object(self.app, "tmux", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
             mock.patch.object(self.app, "panes", return_value=[]):
            run = self.manager.create_session(self.pid, {"role": "worker", "execution_mode": "print",
                                                       "skip_permissions": False})
            resumed = self.manager.resume(run["agent_id"], {"skip_permissions": False})
        for result in (run, resumed):
            self.assertIn("--dangerously-skip-permissions", result["command"])
            self.assertTrue(result["effective_skip_permissions"])
            self.assertEqual(result["permission_source"], "worker_policy")


if __name__ == "__main__":
    unittest.main()
