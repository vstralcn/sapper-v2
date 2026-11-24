"""Worker recovery wakes only API-error-idle sessions and confirms real tool work."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_web import Console, private_json
from sapper_workspace import watch


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worker recovery ")
        self.root = Path(self.temp.name)
        self.app = Console(self.root / "tasks", self.root / "state")
        self.project = self.app.create_project({"name": "fixture", "goal": "recover actual work"})
        self.manager = self.app.workspaces
        self.monitor = self.manager.worker
        self.agent = {"id": "d" * 16, "workspace_id": self.project["id"],
                      "role": "worker", "desired_state": "running", "execution_mode": "tui",
                      "run_id": "e" * 16, "claude_session_id": str(uuid.uuid4()),
                      "claude_config_dir": str(self.root / "claude"),
                      "updated_at": self.iso(900), "created_at": self.iso(900),
                      "name": "fixture worker", "cwd": self.project["path"],
                      "mission": "advance the assigned challenge"}
        self.manager.save_agent(self.agent)
        self.run = {"id": self.agent["run_id"], "status": "running", "session": "worker-fixture",
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

    def api_error(self, at=1000):
        self.event("assistant", at, [{"type": "text", "text": "API Error: upstream HTTP 500"}],
                   "stop_sequence", isApiErrorMessage=True)

    def rows(self):
        return [{**self.agent, "pane_id": "%worker", "status": "running"}]

    def tick(self, at=1070, rows=None):
        self.monitor.tick(self.agent["id"], self.rows() if rows is None else rows, now=at)

    def wake(self):
        return self.monitor.view(self.agent["id"])["wake"]

    def accept(self, at=1071):
        self.event("user", at, self.monitor.prompt(self.wake()["id"]))

    def tool(self, at=1072):
        self.event("assistant", at, [{"type": "tool_use", "id": "work", "name": "Bash"}], "tool_use")

    def test_normal_idle_does_not_trigger_recovery(self):
        self.event("assistant", 1000, [{"type": "text", "text": "done"}], "end_turn")
        self.tick(5000)
        self.send.assert_not_called()

    def test_api_error_prompt_lets_worker_choose_and_tool_confirms(self):
        self.api_error()
        self.tick()
        self.assertEqual(self.wake()["status"], "sent")
        prompt = self.send.call_args.kwargs["target_agent"], self.send.call_args.args[1]["text"]
        self.assertEqual(prompt[0], self.agent["id"])
        self.assertIn("自主选择", prompt[1])
        self.assertIn("调用工具", prompt[1])
        self.accept()
        self.tool()
        self.event("user", 1073, [{"type": "tool_result", "tool_use_id": "work", "content": "ok"}])
        self.event("assistant", 1074, [{"type": "text", "text": "work advanced"}], "end_turn")
        self.tick(5000)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.monitor.view(self.agent["id"])["verification"], "work_confirmed")

    def test_acknowledgement_is_not_recovery_and_retries_have_no_hard_cap(self):
        self.api_error()
        self.tick()
        self.accept()
        self.event("assistant", 1080, [{"type": "text", "text": "我会继续"}], "end_turn")
        self.tick(1189)
        self.assertEqual(self.send.call_count, 1)
        self.tick(1190)
        self.assertEqual(self.wake()["attempt"], 2)
        self.accept(1191)
        self.api_error(1192)
        self.tick(1429)
        self.assertEqual(self.send.call_count, 2)
        self.tick(1430)
        self.assertEqual(self.wake()["attempt"], 3)
        self.accept(1431)
        self.api_error(1432)
        self.tick(1910)
        self.assertEqual(self.wake()["attempt"], 4)
        self.assertEqual(self.send.call_count, 4)

    def test_running_tool_is_not_interrupted(self):
        self.api_error()
        self.tick()
        self.accept()
        self.tool()
        self.tick(10000)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.monitor.view(self.agent["id"])["phase"], "tool_running")

    def test_real_user_input_supersedes_pending_recovery(self):
        self.api_error()
        self.event("user", 1001, "先解释当前状态，不要执行工具")
        self.event("assistant", 1002, [{"type": "text", "text": "当前状态"}], "end_turn")
        self.tick(5000)
        self.send.assert_not_called()

    def test_unsent_user_input_is_not_overwritten(self):
        self.api_error()
        with mock.patch.object(self.app, "capture", return_value={"text": "❯ 正在输入\n"}):
            self.tick()
        self.send.assert_not_called()
        self.assertIn("未发送恢复指令", self.monitor.view(self.agent["id"])["attention"])

    def test_stopped_or_timed_out_worker_is_not_woken(self):
        self.api_error()
        self.agent["desired_state"] = "stopped"
        self.manager.save_agent(self.agent)
        self.tick()
        self.agent["desired_state"] = "running"
        self.manager.save_agent(self.agent)
        private_json(self.app.run_path(self.run["id"]) / "run.json", {**self.run, "status": "timed_out"})
        self.tick()
        self.send.assert_not_called()

    def test_watch_loop_invokes_worker_monitor(self):
        main = {**self.agent, "id": "f" * 16, "role": "commander", "parent_agent_id": None, "status": "running"}
        worker = self.rows()[0]
        worker["parent_agent_id"] = main["id"]
        rows = [main, worker]
        with mock.patch.dict(os.environ, {"SAPPER_WORKSPACE_ID": self.project["id"]}), \
                mock.patch("sapper_workspace.console_from_environment", return_value=self.app), \
                mock.patch.object(self.manager, "agents", return_value=rows), \
                mock.patch.object(self.manager, "main_agent", return_value=main), \
                mock.patch.object(self.manager.commander, "tick") as commander_tick, \
                mock.patch.object(self.monitor, "tick") as worker_tick, \
                mock.patch("sapper_workspace.time.sleep", side_effect=RuntimeError("one sweep")):
            with self.assertRaisesRegex(RuntimeError, "one sweep"):
                watch()
        commander_tick.assert_called_once_with(main["id"], rows)
        worker_tick.assert_called_once_with(self.agent["id"], rows)


if __name__ == "__main__":
    unittest.main()
