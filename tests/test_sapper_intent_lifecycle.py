"""Workspace stop, transfer and resume races use isolated graphs and processes."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
from sapper_web import Console, private_json
from sapper_workspace import WorkspaceError, worker
from sapper_processes import RunProcesses
import test_sapper_workspaces as fixtures


class IntentLifecycleTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBindingTests.setUp
    tearDown = fixtures.WorkspaceBindingTests.tearDown

    def prepare(self):
        tmux = mock.patch.object(self.app, "tmux", return_value=SimpleNamespace(returncode=1, stdout="", stderr=""))
        tmux.start()
        self.addCleanup(tmux.stop)
        self.manager = self.app.workspaces
        self.pid = self.manager.bind({"path": str(self.question)})["id"]
        self.manager.enable(self.pid, {})
        self.manager._capabilities = dict(available=True, session_id=True, resume=True, append_system=True)
        self.store = self.app.store(self.pid)
        self.basis = self.store.add_fact("supplied input", "user")["id"]
        self.source, self.target = self.agent(1), self.agent(2)

    def agent(self, number, **extra):
        record = {"id": f"{number:016x}", "workspace_id": self.pid, "role": "worker",
            "desired_state": "stopped", "created_at": sapper.now_iso(), "run_ids": [],
            "claude_session_id": str(uuid.uuid4()), "execution_mode": "tui", "profile_id": "",
            "cwd": str(self.question), "mission": "old assignment", "skip_permissions": False, **extra}
        self.manager.save_agent(record)
        return record

    def assign(self, record=None, *, tracked=True):
        record = record or self.source
        intent = self.store.add_intent([self.basis], "inspect supplied input", "user", assigned_worker=record["id"])
        if tracked:
            record["intent_id"] = intent["id"]
            self.manager.save_agent(record)
        return intent

    def run_record(self, status="running"):
        rid = "b" * 16
        run = {"id": rid, "project_id": self.pid, "agent_id": self.source["id"],
               "role": "worker", "session": "isolated-fixture", "status": status, "created_at": sapper.now_iso()}
        private_json(self.app.run_path(rid) / "run.json", run)
        self.source.update(run_id=rid, desired_state="running" if status == "running" else "stopped")
        self.manager.save_agent(self.source)
        return run

    def test_stop_releases_all_owned_unfinished_intents_only_after_confirmed_exit(self):
        self.prepare()
        first, second = self.assign(), self.assign(tracked=False)
        completed = self.assign(tracked=False)
        self.store.conclude_with_new_fact(completed["id"], "verified", self.source["id"])
        other = self.assign(self.target)
        run = self.run_record()

        def low_stop(rid, **kwargs):
            self.assertEqual(self.manager.agent(self.source["id"])["desired_state"], "stopped")
            if self.manager.agent(self.source["id"]).get("stop_phase") != "confirmed":
                self.assertEqual(self.store.get_intent(first["id"])["worker"], self.source["id"])
            private_json(self.app.run_path(rid) / "run.json", {**run, "status": "stopped"})
            return {**run, "status": "stopped"}

        with mock.patch.object(self.app, "_stop_run", side_effect=low_stop), \
             mock.patch.object(self.app, "tmux", return_value=SimpleNamespace(returncode=1)), \
             mock.patch("sapper_workspace.RunProcesses") as tracker:
            tracker.return_value.finish.return_value = False
            self.assertEqual(self.manager.stop_agent(self.source["id"])["status"], "stop_pending")
            self.assertEqual(self.store.get_intent(first["id"])["worker"], self.source["id"])
            tracker.return_value.finish.return_value = True
            stopped = self.app._stop(run["id"])
            self.assertEqual(set(stopped["released_intents"]), {first["id"], second["id"]})
            self.assertEqual(self.manager.stop_agent(self.source["id"])["released_intents"], [])
        self.assertEqual(self.store.get_intent(other["id"])["worker"], self.target["id"])
        self.assertIsNotNone(self.store.get_intent(completed["id"])["to"])
        self.manager.delete_agent(self.source["id"])
        self.manager.delete_agent(self.source["id"])

    def test_external_interrupt_retains_claim_until_pane_exits_and_delete_waits(self):
        self.prepare()
        self.source.update(external_session="external", external_pane="%1", desired_state="running", legacy=True)
        self.manager.save_agent(self.source)
        intent = self.assign()
        panes = [{"session": "external", "id": "%1", "dead": False}]
        with mock.patch.object(self.app, "panes", side_effect=lambda: list(panes)), \
             mock.patch.object(self.app, "send") as send:
            self.assertEqual(self.manager.stop_agent(self.source["id"])["message"], "等待确认停止")
            self.assertEqual(self.manager.delete_agent(self.source["id"])["status"], "stop_pending")
            send.assert_called_once_with("%1", {"key": "C-c"})
            self.assertEqual(self.store.get_intent(intent["id"])["worker"], self.source["id"])
            with self.assertRaisesRegex(WorkspaceError, "等待确认停止"):
                self.manager.resume(self.source["id"], {})
            with self.assertRaisesRegex(WorkspaceError, "等待确认停止"):
                self.manager.release_intent(self.pid, intent["id"], expected_owner=self.source["id"], reason="retire")
            panes.clear()
            self.assertEqual(self.manager.stop_agent(self.source["id"])["released_intents"], [intent["id"]])
            self.manager.delete_agent(self.source["id"])
        self.assertIsNone(self.store.get_intent(intent["id"])["worker"])

    def test_reassign_checks_expected_owner_and_rolls_back_target_on_write_failure(self):
        self.prepare()
        intent = self.assign()
        with self.assertRaisesRegex(WorkspaceError, "持有者已变化"):
            self.manager.reassign_intent(self.pid, intent["id"], self.target["id"], expected_owner=None, reason="retry")
        with mock.patch.object(sapper.Store, "save_intent", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.manager.reassign_intent(self.pid, intent["id"], self.target["id"],
                                             expected_owner=self.source["id"], reason="retry")
        self.assertEqual(self.manager.agent(self.target["id"]), self.target)
        self.assertEqual(self.store.get_intent(intent["id"])["worker"], self.source["id"])

    def test_two_concurrent_transfers_have_one_winner_and_source_cannot_resume(self):
        self.prepare()
        intent = self.assign()
        third = self.agent(3)
        other_app = Console(self.app.root, self.app.state)
        barrier = threading.Barrier(2)

        def transfer(manager, target):
            barrier.wait(timeout=5)
            try:
                return manager.reassign_intent(self.pid, intent["id"], target,
                    expected_owner=self.source["id"], reason="replace stopped worker")["worker"]
            except WorkspaceError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(transfer, self.manager, self.target["id"])
            second = pool.submit(transfer, other_app.workspaces, third["id"])
            results = [first.result(timeout=10), second.result(timeout=10)]
        winner = self.store.get_intent(intent["id"])["worker"]
        self.assertEqual(sum(r == winner for r in results), 1)
        self.assertTrue(any("持有者已变化" in r for r in results))
        with mock.patch.object(self.app, "start") as start:
            with self.assertRaisesRegex(WorkspaceError, "已转派"):
                self.manager.resume(self.source["id"], {})
            start.assert_not_called()
        self.assertEqual(self.manager.agent(winner)["intent_id"], intent["id"])

    def test_stop_and_transfer_are_serialized_and_stale_owner_is_rejected(self):
        self.prepare()
        intent = self.assign()
        run = self.run_record()
        other_app = Console(self.app.root, self.app.state)
        entered, exit_allowed, transfer_entered = threading.Event(), threading.Event(), threading.Event()

        def low_stop(rid, **kwargs):
            entered.set()
            if not exit_allowed.wait(5):
                raise AssertionError("test did not release stop")
            private_json(self.app.run_path(rid) / "run.json", {**run, "status": "stopped"})
            return {**run, "status": "stopped"}

        def transfer():
            transfer_entered.set()
            return other_app.workspaces.reassign_intent(self.pid, intent["id"], self.target["id"],
                expected_owner=self.source["id"], reason="replace")

        with mock.patch.object(self.app, "_stop_run", side_effect=low_stop), \
             mock.patch.object(self.app, "tmux", return_value=SimpleNamespace(returncode=1)), \
             ThreadPoolExecutor(max_workers=2) as pool:
            stopped = pool.submit(self.manager.stop_agent, self.source["id"])
            self.assertTrue(entered.wait(5))
            pending = pool.submit(transfer)
            self.assertTrue(transfer_entered.wait(5))
            self.assertFalse(pending.done())
            self.assertEqual(self.store.get_intent(intent["id"])["worker"], self.source["id"])
            exit_allowed.set()
            self.assertEqual(stopped.result(timeout=10)["released_intents"], [intent["id"]])
            with self.assertRaisesRegex(WorkspaceError, "持有者已变化"):
                pending.result(timeout=10)
        self.assertIsNone(self.store.get_intent(intent["id"])["worker"])

    def test_resume_reclaims_released_assignment_and_historical_stop_keeps_new_run(self):
        self.prepare()
        intent = self.assign()
        self.manager.release_intent(self.pid, intent["id"], expected_owner=self.source["id"], reason="restart")
        self.source = self.manager.agent(self.source["id"])
        with mock.patch.object(self.app, "start", return_value={"id": "new-run"}), \
             mock.patch.object(self.app, "panes", return_value=[]):
            self.manager.resume(self.source["id"], {})
        self.assertEqual(self.store.get_intent(intent["id"])["worker"], self.source["id"])
        old = {"id": "c" * 16, "project_id": self.pid, "agent_id": self.source["id"],
               "status": "stopped", "session": "old", "created_at": sapper.now_iso()}
        private_json(self.app.run_path(old["id"]) / "run.json", old)
        self.app.stop(old["id"])
        self.assertEqual(self.manager.agent(self.source["id"])["desired_state"], "running")
        self.assertEqual(self.store.get_intent(intent["id"])["worker"], self.source["id"])

    def test_cli_release_and_reassign_validate_scope_relationship_reason_and_owner(self):
        self.prepare()
        parent = self.agent(9, role="commander", desired_state="running")
        for a in (self.source, self.target):
            a["parent_agent_id"] = parent["id"]
            self.manager.save_agent(a)
        intent = self.assign()
        with mock.patch("sapper_workspace.console_from_environment", return_value=self.app), \
             mock.patch.dict(os.environ, {"SAPPER_WORKSPACE_ID": self.pid, "SAPPER_AGENT_ID": parent["id"]}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(WorkspaceError, "reason"):
                worker(["release", intent["id"]])
            worker(["reassign", intent["id"], "--to", self.target["id"], "--expected-owner", self.source["id"], "--reason", "replace"])
            with self.assertRaisesRegex(WorkspaceError, "持有者已变化"):
                worker(["release", intent["id"], "--expected-owner", self.source["id"], "--reason", "old observation"])
            worker(["release", intent["id"], "--reason", "retire"])
        self.assertIsNone(self.store.get_intent(intent["id"])["worker"])

    def test_stale_workspace_result_cannot_complete_released_intent(self):
        self.prepare()
        intent = self.assign()
        self.manager.release_intent(self.pid, intent["id"], expected_owner=self.source["id"], reason="retire")
        with mock.patch.dict(os.environ, {"SAPPER_MODE": "workspace", "SAPPER_AGENT_ID": self.source["id"]}):
            for worker_id in (self.source["id"], "agent"):
                with self.assertRaisesRegex(sapper.FGError, "stale workspace result"):
                    self.store.conclude_with_new_fact(intent["id"], "late output", worker_id)
        self.assertIsNone(self.store.get_intent(intent["id"])["to"])

    def test_live_owner_completed_intent_and_busy_receiver_cannot_be_transferred(self):
        self.prepare()
        intent = self.assign()
        with self.assertRaisesRegex(WorkspaceError, "指挥官／将军"):
            self.manager.release_intent(self.pid, intent["id"], expected_owner=self.source["id"], reason="retire", actor=self.source["id"])
        self.source["desired_state"] = "running"
        self.manager.save_agent(self.source)
        with self.assertRaisesRegex(WorkspaceError, "尚未停止"):
            self.manager.release_intent(self.pid, intent["id"], expected_owner=self.source["id"], reason="retire")
        self.source["desired_state"] = "stopped"
        self.manager.save_agent(self.source)
        self.assign(self.target)
        with self.assertRaisesRegex(WorkspaceError, "其他未完成意图"):
            self.manager.reassign_intent(self.pid, intent["id"], self.target["id"],
                                         expected_owner=self.source["id"], reason="replace")
        self.store.conclude_with_new_fact(intent["id"], "already done", self.source["id"])
        with self.assertRaisesRegex(WorkspaceError, "已完成"):
            self.manager.release_intent(self.pid, intent["id"], expected_owner=self.source["id"], reason="retire")

    def test_stop_reaps_real_detached_tool_before_releasing_assignment(self):
        self.prepare()
        intent = self.assign()
        run = self.run_record()
        script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); print(p.pid,flush=True); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            tool = int(process.stdout.readline())
            run.update(wrapper_pid=process.pid, pid=process.pid, process_group=process.pid)
            private_json(self.app.run_path(run["id"]) / "run.json", run)

            def early_result(rid, **kwargs):
                self.assertEqual(self.store.get_intent(intent["id"])["worker"], self.source["id"])
                private_json(self.app.run_path(rid) / "run.json", {**run, "status": "stopped"})
                return {**run, "status": "stopped"}

            with mock.patch.object(self.app, "_stop_run", side_effect=early_result):
                result = self.manager.stop_agent(self.source["id"])
            self.assertEqual(result["released_intents"], [intent["id"]])
            process.wait(timeout=5)
            self.assertFalse(RunProcesses({"pid": tool}).remaining())
            self.assertIsNone(self.store.get_intent(intent["id"])["worker"])
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


class ProcessExitTests(unittest.TestCase):
    def test_leaderless_reused_group_is_unconfirmed_and_never_signalled(self):
        reused = {88888: {"parent": 1, "group": 98765, "birth": "Sat Nov  7 14:00:00 2026"}}
        with mock.patch("sapper_processes.process_table", return_value=reused), mock.patch("sapper_processes.os.kill") as kill:
            with self.assertRaisesRegex(OSError, "无法确认旧进程组归属"):
                RunProcesses({"wrapper_pid": 98765, "pid": 98765, "process_group": 98765,
                              "process_births": {"98765": "Fri Nov  6 14:00:00 2026"}})
            kill.assert_not_called()

    def test_low_level_stop_does_not_signal_a_reused_persisted_group(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="sapper-old-group-") as directory:
            app = Console.__new__(Console)
            info = {"id": "a" * 16, "status": "running", "session": "absent-fixture",
                    "wrapper_pid": 98765, "pid": 98765, "process_group": 98765,
                    "process_births": {"98765": "Fri Nov  6 14:00:00 2026"}}
            app.run_info = mock.Mock(side_effect=lambda rid: dict(info))
            app.run_path = mock.Mock(return_value=Path(directory))
            app.tmux = mock.Mock(return_value=SimpleNamespace(returncode=1))
            app.audit = mock.Mock()
            reused = {98765: {"parent": 1, "group": 98765, "birth": "Sat Nov  7 14:00:00 2026"}}
            with mock.patch("sapper_processes.process_table", return_value=reused), \
                 mock.patch("sapper_processes.os.kill") as kill, mock.patch("os.killpg") as killpg:
                app._stop_run(info["id"])
                kill.assert_not_called()
                killpg.assert_not_called()

    def test_pid_reuse_between_initial_process_snapshots_is_not_adopted(self):
        original = {98765: {"parent": 1, "group": 98765, "birth": "Fri Nov  6 14:00:00 2026"}}
        reused = {98765: {"parent": 1, "group": 98765, "birth": "Sat Nov  7 14:00:00 2026"}}
        snapshots = iter([original, reused])
        with mock.patch("sapper_processes.process_table", side_effect=lambda: next(snapshots, reused)), \
             mock.patch("sapper_processes.os.kill") as kill:
            tracker = RunProcesses({"wrapper_pid": 98765, "pid": 98765, "process_group": 98765,
                                    "process_births": {"98765": original[98765]["birth"]}})
            self.assertTrue(tracker.finish())
            kill.assert_not_called()

    def test_reused_process_identity_is_never_signalled(self):
        table = {98765: {"parent": 1, "group": 98765, "birth": "Sat Nov  7 14:00:00 2026"}}
        with mock.patch("sapper_processes.process_table", return_value=table), mock.patch("sapper_processes.os.kill") as kill:
            tracker = RunProcesses({"wrapper_pid": 98765, "pid": 98765, "process_group": 98765,
                                    "process_births": {"98765": "Fri Nov  6 14:00:00 2026"}})
            self.assertTrue(tracker.finish())
            kill.assert_not_called()

    def test_detached_tool_is_reaped_and_unrelated_process_survives(self):
        script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); print(p.pid,flush=True); time.sleep(30)"
        parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        try:
            tool = int(parent.stdout.readline())
            tracker = RunProcesses({"wrapper_pid": parent.pid, "pid": parent.pid, "process_group": parent.pid})
            self.assertIn(tool, tracker.remaining())
            self.assertTrue(tracker.finish())
            parent.wait(timeout=5)
            self.assertIsNone(unrelated.poll())
            self.assertFalse(tracker.remaining())
        finally:
            parent.stdout.close()
            for process in (parent, unrelated):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
