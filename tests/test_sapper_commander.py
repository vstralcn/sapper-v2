"""Commander liveness must be proven by a new, correlated real board read."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
from sapper_web import Console, private_json
from sapper_commander import WatchdogLog


class WatchdogLogTests(unittest.TestCase):
    def test_state_changes_are_immediate_and_unchanged_heartbeats_are_bounded(self):
        log = WatchdogLog()
        view = {'control': 'active', 'phase': 'responding', 'verification': 'input_accepted'}
        self.assertIn('[watchdog] state ', log.line('agent', view, now=0))
        self.assertIsNone(log.line('agent', {**view, 'last_event_at': 1}, now=3))
        self.assertIsNone(log.line('agent', view, now=59))
        self.assertIn('[watchdog] heartbeat ', log.line('agent', view, now=60))
        changed = {**view, 'verification': 'board_confirmed'}
        self.assertIn('board_confirmed', log.line('agent', changed, now=61))
        self.assertIsNone(log.line('agent', changed, now=62))

    def test_logs_exclude_prompt_text_and_include_the_actual_stop_reason(self):
        view = {'control': 'active', 'phase': 'idle', 'verification': 'responded',
                'reason': 'private human input', 'latest_message': 'private model text',
                'attention': '连续唤醒未收到读板回执，请人工检查',
                'wake': {'id': 'wake-id', 'status': 'accepted', 'attempt': 2}}
        line = WatchdogLog().line('agent', view, now=0)
        self.assertIn('连续唤醒未收到读板回执', line)
        self.assertIn('"attempt": 2', line)
        self.assertNotIn('private', line)


class CommanderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="commander monitor ")
        self.root = Path(self.temp.name)
        self.app = Console(self.root / "tasks", self.root / "state")
        self.project = self.app.create_project({"name": "fixture", "goal": "verify native cycle"})
        self.manager = self.app.workspaces
        self.monitor = self.manager.commander
        self.agent = {"id": "a" * 16, "workspace_id": self.project["id"],
                      "workspace_scope": [self.project["id"]], "role": "commander",
                      "desired_state": "running", "execution_mode": "tui",
                      "run_id": "b" * 16, "claude_session_id": str(uuid.uuid4()),
                      "claude_config_dir": str(self.root / "claude"),
                      "updated_at": self.iso(900), "created_at": self.iso(900),
                      "name": "fixture commander", "cwd": self.project["path"], "mission": "verify cycle"}
        self.manager.save_agent(self.agent)
        self.run = {"id": self.agent["run_id"], "status": "running", "session": "fixture-session",
                    "project_id": self.project["id"], "created_at": self.iso(900)}
        private_json(self.app.run_path(self.run["id"]) / "run.json", self.run)
        self.transcript = self.root / "claude/projects/exact-directory" / (self.agent["claude_session_id"] + ".jsonl")
        self.transcript.parent.mkdir(parents=True)
        self.send = mock.patch.object(self.app, "send", return_value={"status": "sent"}).start()
        mock.patch.object(self.app, "capture", return_value={"text": "❯\n"}).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.temp.cleanup()

    def iso(self, at):
        return datetime.fromtimestamp(at, timezone.utc).isoformat()

    def event(self, kind, at, content, stop=None, **extra):
        event = {"type": kind, "timestamp": self.iso(at), "sessionId": self.agent["claude_session_id"],
                 "message": {"content": content, "stop_reason": stop}, **extra}
        with self.transcript.open("a") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")

    def idle(self, at=1000):
        self.event("assistant", at, [{"type": "text", "text": "60 秒后继续"}], "end_turn")

    def rows(self):
        return [{**self.agent, "pane_id": "%123", "status": "running"}]

    def tick(self, at=1070, rows=None):
        self.monitor.tick(self.agent["id"], self.rows() if rows is None else rows, now=at)

    def wake(self):
        return self.monitor.view(self.agent["id"])["wake"]

    def accept(self, at=1071):
        self.event("user", at, self.monitor.prompt(self.wake()["id"]))

    def test_ended_turn_is_woken_and_real_board_confirms_exact_wake(self):
        self.idle()
        self.tick(1050)
        self.send.assert_not_called()
        self.tick()
        self.assertEqual(self.wake()["status"], "sent")
        self.tick(1072)
        self.send.assert_called_once()
        self.accept()
        board = self.manager.strategy_board([self.project["id"]])
        self.monitor.record_board(self.agent["id"], self.agent["run_id"], board)
        view = self.monitor.view(self.agent["id"])
        self.assertEqual(view["verification"], "board_confirmed")
        self.assertEqual(view["last_board"]["wake_id"], self.wake()["id"])

    def test_two_watchdogs_send_only_one_wake(self):
        self.idle()
        other = Console(self.app.root, self.app.state)
        with mock.patch.object(other, 'capture', return_value={'text': '❯\n'}), \
             mock.patch.object(other, 'send', return_value={'status': 'sent'}) as other_send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(monitor.tick, self.agent['id'], self.rows(), 1070)
                           for monitor in (self.monitor, other.workspaces.commander)]
                for future in futures:
                    future.result(timeout=5)
        self.assertEqual(self.send.call_count + other_send.call_count, 1)

    def test_native_terminal_interrupt_pauses_automatic_turns(self):
        from sapper_terminal import TerminalBridge
        pane = {'id': '%123', 'session': self.run['session'], 'project_id': self.project['id']}
        with mock.patch.object(self.app, 'pane', return_value=pane), \
             mock.patch.object(self.app, 'tmux', return_value=mock.Mock(returncode=0)):
            TerminalBridge(self.app).input('%123', {'data': '\x03'})
        self.idle()
        self.tick(2000)
        self.send.assert_not_called()
        self.assertEqual(self.monitor.view(self.agent['id'])['control'], 'paused')

    def test_old_or_foreign_board_cannot_acknowledge_a_new_wake(self):
        self.idle()
        self.tick()
        self.monitor.record_board(self.agent["id"], "c" * 16, {"agents": []})
        self.assertNotIn("last_board", self.monitor.view(self.agent["id"]))
        self.monitor.record_board(self.agent["id"], self.agent["run_id"], {"agents": []})
        self.assertIsNone(self.monitor.view(self.agent["id"])["last_board"]["wake_id"])
        self.assertEqual(self.wake()["status"], "sent")

    def test_unrelated_new_prompt_or_unreadable_graph_cannot_confirm_wake(self):
        self.idle()
        self.tick()
        self.accept()
        self.monitor.record_board(self.agent["id"], self.agent["run_id"],
                                  {"agents": [], "workspaces": [{"status": "unavailable"}]})
        self.assertNotIn("last_board", self.monitor.view(self.agent["id"]))
        self.event("user", 1073, "different manual instruction")
        self.monitor.record_board(self.agent["id"], self.agent["run_id"], {"agents": []})
        self.assertIsNone(self.monitor.view(self.agent["id"])["last_board"]["wake_id"])

    def test_missing_transcript_after_a_previous_idle_event_does_not_reuse_idle(self):
        self.idle()
        self.tick(1050)
        self.transcript.unlink()
        self.tick(1100)
        self.send.assert_not_called()

    def test_api_error_turn_is_idle_and_cannot_be_mistaken_for_recovery(self):
        self.event("assistant", 1000, [{"type": "text", "text": "API Error: upstream HTTP 502"}],
                   "stop_sequence", isApiErrorMessage=True)
        self.tick()
        self.assertEqual(self.monitor.view(self.agent["id"])["verification"], "wake_sent")
        self.accept()
        self.event("assistant", 1080, [{"type": "text", "text": "API Error"}],
                   "stop_sequence", isApiErrorMessage=True)
        self.tick(1191)
        self.accept(1192)
        self.event("assistant", 1200, [{"type": "text", "text": "API Error"}],
                   "stop_sequence", isApiErrorMessage=True)
        self.tick(1400)
        self.assertEqual(self.send.call_count, 2)
        self.assertNotEqual(self.monitor.view(self.agent["id"])["verification"], "board_confirmed")

    def test_large_history_is_scanned_incrementally_before_any_action(self):
        self.idle()
        with self.transcript.open('a') as file:
            for _ in range(5):
                file.write(json.dumps({'type': 'progress', 'data': 'x' * (1024 * 1024)}) + '\n')
        self.tick()
        self.send.assert_not_called()
        self.tick()
        self.send.assert_called_once()

    def test_wake_that_returns_without_board_is_retried_only_twice(self):
        self.idle()
        self.tick()
        self.accept()
        self.idle(1080)
        self.tick(1191)
        self.assertEqual(self.wake()["attempt"], 2)
        self.accept(1192)
        self.idle(1193)
        self.tick(1312)
        self.tick(2000)
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("连续唤醒未收到读板回执", self.monitor.view(self.agent["id"])["attention"])

    def test_busy_tool_and_model_wait_are_not_interrupted_after_ack_timeout(self):
        for tool in (None, "Bash", "AskUserQuestion"):
            with self.subTest(tool=tool):
                self.monitor.configure(self.agent["id"], "active")
                self.idle(2000)
                self.tick(2070)
                self.accept(2071)
                if tool:
                    self.event("assistant", 2072, [{"type": "tool_use", "id": "t1", "name": tool}], "tool_use")
                before = self.send.call_count
                with mock.patch.object(self.manager, "resume") as resume:
                    self.tick(3000)
                    self.assertEqual(self.send.call_count, before)
                    resume.assert_not_called()
                self.assertIn("尚无读板", self.monitor.view(self.agent["id"])["attention"])
                # Return the tool before the next independent subcase.
                if tool:
                    self.event("user", 3001, [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}])

    def test_pause_wait_for_user_and_complete_survive_monitor_recreation(self):
        self.idle()
        for control in ("paused", "waiting_user", "completed"):
            self.monitor.configure(self.agent["id"], control, "explicit decision")
            self.tick(2000)
            self.assertEqual(type(self.monitor)(self.manager).view(self.agent["id"])["control"], control)
        self.send.assert_not_called()

    def test_stop_and_deleted_agent_cannot_be_woken(self):
        self.idle()
        self.agent["desired_state"] = "stopped"
        self.manager.save_agent(self.agent)
        self.tick(2000)
        self.send.assert_not_called()
        self.agent.update(desired_state="running", deleted_at=self.iso(1500))
        self.manager.save_agent(self.agent)
        with self.assertRaises(sapper.FGError):
            self.tick(2000)
        self.send.assert_not_called()

    def test_stopping_timeout_or_stop_marker_is_not_resumed(self):
        self.idle()
        for status in ("stopping", "timed_out", "stopped"):
            private_json(self.app.run_path(self.run["id"]) / "run.json", {**self.run, "status": status})
            self.tick(2000, [])
        private_json(self.app.run_path(self.run["id"]) / "run.json", self.run)
        (self.app.run_path(self.run["id"]) / "stop_requested").touch()
        self.tick(2000)
        self.send.assert_not_called()

    def test_completed_workspace_disables_automatic_turns(self):
        store = self.app.store(self.project["id"])
        project = store.load_project()
        store.save_project({**project, "status": "completed"})
        self.idle()
        self.tick(2000)
        self.assertEqual(self.monitor.view(self.agent["id"])["control"], "completed")
        self.send.assert_not_called()

    def test_input_being_typed_is_not_overwritten(self):
        self.idle()
        with mock.patch.object(self.app, "capture", return_value={"text": "❯ my unsent instruction\n"}):
            self.tick()
        self.send.assert_not_called()

    def test_new_child_completion_wakes_before_fallback_timer(self):
        self.idle()
        rows = self.rows() + [{"id": "c" * 16, "parent_agent_id": self.agent["id"], "status": "running"}]
        with self.monitor.locked(self.agent["id"]) as data:
            data["event_signature"] = self.monitor.event_signature(self.agent, rows)
        rows[-1]["status"] = "exited"
        self.tick(1003, rows)
        self.send.assert_called_once()

    def test_new_graph_fact_wakes_before_fallback_timer(self):
        self.idle()
        with self.monitor.locked(self.agent["id"]) as data:
            data["event_signature"] = self.monitor.event_signature(self.agent, self.rows())
        self.app.store(self.project["id"]).add_fact("[HELP] concrete blocker", "child")
        self.tick(1003)
        self.send.assert_called_once()

    def test_unknown_missing_partial_and_sidechain_data_do_not_trigger_wakes(self):
        self.tick()
        self.send.assert_not_called()
        self.event("assistant", 1000, [], "end_turn", isSidechain=True)
        self.tick()
        self.send.assert_not_called()
        with self.transcript.open("a") as file:
            file.write('{"type":')
        self.tick()
        self.send.assert_not_called()

    def test_user_interrupt_pauses_and_real_user_continue_releases_it(self):
        self.idle()
        self.event("user", 1020, "[Request interrupted by user]")
        self.tick()
        self.send.assert_not_called()
        self.assertEqual(self.monitor.view(self.agent["id"])["control"], "paused")
        self.event("user", 1100, "continue")
        self.idle(1120)
        self.tick(1200)
        self.send.assert_called_once()

    def test_crash_recovery_is_bounded_and_not_reported_as_board_success(self):
        self.idle()
        private_json(self.app.run_path(self.run["id"]) / "run.json", {**self.run, "status": "failed"})
        with mock.patch.object(self.manager, "resume", return_value={"id": "c" * 16}) as resume:
            self.tick(1070, [])
            self.tick(1200, [])
            self.tick(1400, [])
        self.assertEqual(resume.call_count, 2)
        self.assertIn("[Sapper wake:", resume.call_args.args[1]["continuation"])
        self.assertEqual(self.monitor.view(self.agent["id"])["verification"], "process_started")

    def test_actual_worker_board_writes_receipt_and_child_cannot_ack_parent(self):
        self.idle()
        self.tick()
        self.accept()
        env = {**os.environ, "SAPPER_MODE": "workspace", "SAPPER_WEB_STATE": str(self.app.state),
               "SAPPER_ROOT": str(self.app.root), "SAPPER_AGENT_ID": self.agent["id"],
               "SAPPER_RUN_ID": self.run["id"], "SAPPER_WORKSPACE_ID": self.project["id"],
               "SAPPER_TMUX_SOCKET": "commander-nonexistent-" + uuid.uuid4().hex[:8]}
        result = subprocess.run(["bash", str(Path(sapper.__file__).resolve().parents[1] / "docker/commander/worker.sh"), "board", "--json"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workspaces", json.loads(result.stdout))
        self.assertEqual(self.monitor.view(self.agent["id"])["verification"], "board_confirmed")
        child = {**self.agent, "id": "d" * 16, "role": "worker", "parent_agent_id": self.agent["id"]}
        self.manager.save_agent(child)
        before = self.monitor.view(self.agent["id"])["last_board"]
        self.monitor.record_board(child["id"], self.run["id"], {"agents": []})
        self.assertEqual(self.monitor.view(self.agent["id"])["last_board"], before)


if __name__ == "__main__":
    unittest.main()
