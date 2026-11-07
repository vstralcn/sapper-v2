"""Role routing tests use isolated state and never call a remote provider."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_web import Console, WebError, private_json
from sapper_workspace import WorkspaceError, worker


class RoleModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="role-models-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = Console(self.base / "tasks", self.base / "state")
        self.project = self.app.create_project({"name": "fixture", "goal": "test routing"})
        self.pid = self.project["id"]
        self.app.workspaces.enable(self.pid, {})
        self.app.workspaces._capabilities = dict(available=True, session_id=True, resume=True, append_system=True, mcp_config=True, strict_mcp_config=True, plugin_dir=True, setting_sources=True, settings=True, skill_isolation=True)
        self.glm = self.app.save_profile({"name": "GLM", "model": "glm-fixture", "api_key": "fixture-private-key"})
        self.ds = self.app.save_profile({"name": "DS", "model": "ds-fixture", "protocol": "chat_completions"})
        self.counter = 0
        self.start = mock.patch.object(self.app, "start", side_effect=self.fake_start).start()
        mock.patch.object(self.app.workspaces, "start_watchdog", return_value={}).start()
        self.addCleanup(mock.patch.stopall)
        self.app.panes = lambda: []

    def fake_start(self, pid, data, native):
        self.counter += 1
        return {"id": f"{self.counter:016x}", "agent_id": native["agent_id"]}

    def assign(self, assignments):
        return self.app.save_role_models({"assignments": assignments, "expected_revision": self.app.role_models()["revision"]})

    def launch(self, **data):
        run = self.app.workspaces.create_session(self.pid, {"mission": "routing fixture", **data})
        return self.app.workspaces.agent(run["agent_id"])

    def test_global_mapping_persists_without_exposing_credentials(self):
        self.assertEqual(self.app.role_models()["assignments"], {})
        assignments = dict(commander=self.glm["id"], general=self.glm["id"], worker=self.ds["id"], scout=self.ds["id"])
        self.assign(assignments)
        reloaded = Console(self.app.root, self.app.state).role_models()
        self.assertEqual(reloaded["assignments"], assignments)
        self.assertEqual(len(reloaded["roles"]), 6)
        self.assertNotIn("fixture-private-key", json.dumps(reloaded))
        self.assertEqual(self.app.config_path.stat().st_mode & 0o777, 0o600)

    def test_manual_override_role_default_parent_and_environment(self):
        parent = self.launch(role="commander", profile_id=self.glm["id"])
        self.assign(dict(scout=self.ds["id"], worker=""))
        basis = self.app.store(self.pid).add_fact("fixture supplied", "user")["id"]
        for role, explicit, expected in [
            ("scout", {}, self.ds["id"]), ("general", {}, self.glm["id"]),
            ("worker", {}, ""), ("scout", {"profile_id": self.glm["id"]}, self.glm["id"]),
            ("scout", {"profile_id": ""}, "")]:
            with self.subTest(role=role, explicit=explicit):
                child = self.launch(role=role, parent_agent_id=parent["id"], **{"from": basis}, **explicit)
                self.assertEqual(child["profile_id"], expected)
                self.assertEqual(self.start.call_args.args[1]["profile_id"], expected)
                argv = self.start.call_args.kwargs["native"]["argv"]
                if expected:
                    profile = next(p for p in self.app.profiles() if p["id"] == expected)
                    self.assertEqual(argv[argv.index("--model") + 1], profile["model"])
                else:
                    self.assertNotIn("--model", argv)
        self.assertEqual(self.launch(role="scout")["profile_id"], self.ds["id"])
        self.assertEqual(self.launch(role="custom")["profile_id"], "")

    def test_resume_retains_original_profile_including_empty_profile(self):
        for original in ("", self.glm["id"]):
            agent = self.launch(role="scout", profile_id=original)
            self.assign({"scout": self.ds["id"]})
            self.app.workspaces.resume(agent["id"], {"profile_id": self.ds["id"]})
            self.assertEqual(self.start.call_args.args[1]["profile_id"], original)
            self.assertEqual(self.app.workspaces.agent(agent["id"])["claude_session_id"], agent["claude_session_id"])
        self.app.save_profile({**self.glm, "model": "changed-model"})
        with self.assertRaisesRegex(WorkspaceError, "不能静默改变"):
            self.app.workspaces.resume(agent["id"], {})

    def test_resume_error_identifies_original_route_and_current_workspace_default(self):
        agent = self.launch(role="commander", profile_id="")
        self.app.save_role_models({"assignments":{"commander":self.glm["id"]},
            "expected_revision":self.app.role_models()["revision"]}, self.pid)
        model = self.app.workspaces.resume_model_info(agent)
        self.assertEqual(model["original_profile_id"], "")
        self.assertEqual(model["current_profile_id"], self.glm["id"])
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(self.app, "start", wraps=Console.start.__get__(self.app)), \
                mock.patch.object(self.app, "tmux") as tmux:
            with self.assertRaises(WebError) as error:
                self.app.workspaces.resume(agent["id"], {"profile_id":self.glm["id"]})
        message = str(error.exception)
        for expected in ("沿用原模型配置“继承原环境”", "GLM · glm-fixture", "用于新建会话", "API Key"):
            self.assertIn(expected, message)
        self.assertNotIn("fixture-private-key", message)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.app.workspaces.agent(agent["id"])["profile_id"], "")
        tmux.assert_not_called()

    def test_explicit_new_main_uses_current_default_and_preserves_old_identity(self):
        old = self.launch(role="commander", profile_id="")
        self.app.workspaces.mark_stopped(old["id"])
        private_json(self.app.run_path(old["run_id"]) / "run.json", {
            "id":old["run_id"], "project_id":self.pid, "status":"stopped", "created_at":old["created_at"]})
        self.app.save_role_models({"assignments":{"commander":self.glm["id"]},
            "expected_revision":self.app.role_models()["revision"]}, self.pid)
        run = self.app.workspaces.dispatch(self.pid, {"role":"commander", "new_session":True})
        new = self.app.workspaces.agent(run["agent_id"])
        self.assertNotEqual(new["id"], old["id"])
        self.assertNotEqual(new["claude_session_id"], old["claude_session_id"])
        self.assertEqual(new["profile_id"], self.glm["id"])
        self.assertEqual(self.app.workspaces.agent(old["id"])["claude_session_id"], old["claude_session_id"])
        self.assertEqual(self.app.workspaces.agent(old["id"])["desired_state"], "stopped")

    def test_invalid_mapping_and_stale_revision_are_atomic(self):
        old = self.assign({"scout": self.ds["id"]})
        wrapper = self.app.save_profile({"name": "wrapper", "model": "fixture", "engine_prefix": "fixture-cli"})
        for assignments in ({"unknown": ""}, {"scout": "missing"}, {"worker": None}, [], {"worker": wrapper["id"]}):
            with self.subTest(assignments=assignments), self.assertRaises(WebError):
                self.assign(assignments)
            self.assertEqual(self.app.role_models(), old)
        with self.assertRaises(WebError) as error:
            self.app.save_role_models({"assignments": {}, "expected_revision": 0})
        self.assertEqual(error.exception.status, 409)
        with self.assertRaisesRegex(WebError, "先在角色默认模型"):
            self.app.delete_profile(self.ds["id"])
        self.assign({})
        self.app.delete_profile(self.ds["id"])
        self.assertNotIn(self.ds["id"], [p["id"] for p in self.app.profiles()])

    def test_workspace_overrides_global_and_inherits_when_cleared(self):
        other = self.app.create_project({"name": "other", "goal": "separate defaults"})
        self.assign({"scout": self.ds["id"], "general": self.ds["id"]})
        config = self.app.save_role_models({"assignments": {"scout": self.glm["id"], "worker": ""},
                                           "expected_revision": self.app.role_models()["revision"]}, self.pid)
        self.assertEqual(config["global_assignments"]["scout"], self.ds["id"])
        self.assertEqual(self.launch(role="scout")["profile_id"], self.glm["id"])
        self.assertEqual(self.launch(role="general")["profile_id"], self.ds["id"])
        self.assertEqual(self.launch(role="scout", profile_id="")["profile_id"], "")
        self.assertEqual(self.app.role_profile_id("scout", {}, workspace_id=other["id"]), self.ds["id"])
        self.assertEqual(Console(self.app.root, self.app.state).role_models(self.pid)["assignments"], config["assignments"])
        with self.assertRaisesRegex(WebError, "先在角色默认模型"):
            self.app.delete_profile(self.glm["id"])
        with self.assertRaises(WebError) as stale:
            self.app.save_role_models({"assignments": {}, "expected_revision": config["revision"] - 1}, self.pid)
        self.assertEqual(stale.exception.status, 409)
        self.app.save_role_models({"assignments": {}, "expected_revision": config["revision"]}, self.pid)
        self.assertEqual(self.launch(role="scout")["profile_id"], self.ds["id"])

    def test_automatic_child_uses_target_workspace_model(self):
        other = self.app.create_project({"name": "target", "goal": "target workspace"})
        self.app.workspaces.enable(other["id"], {})
        self.assign({"scout": self.glm["id"]})
        self.app.save_role_models({"assignments": {"scout": self.ds["id"]},
                                  "expected_revision": self.app.role_models()["revision"]}, other["id"])
        run = self.app.workspaces.create_session(other["id"], {"role":"commander", "profile_id":self.glm["id"]})
        parent = self.app.workspaces.agent(run["agent_id"])
        basis = self.app.store(other["id"]).add_fact("target evidence", "user")["id"]
        env = {"SAPPER_AGENT_ID": parent["id"], "SAPPER_WORKSPACE_ID": other["id"]}
        with mock.patch.dict(os.environ, env), mock.patch("sapper_workspace.console_from_environment", return_value=self.app):
            with contextlib.redirect_stdout(io.StringIO()):
                worker(["spawn", other["id"], "--from", basis, "--role", "scout"])
            self.assertEqual(self.start.call_args.args[1]["profile_id"], self.ds["id"])
            with contextlib.redirect_stdout(io.StringIO()):
                worker(["spawn", other["id"], "--from", basis, "--role", "scout", "--model", "target-override"])
            chosen = next(p for p in self.app.profiles() if p["id"] == self.start.call_args.args[1]["profile_id"])
            self.assertEqual(chosen["protocol"], "chat_completions")

    def test_worker_cli_uses_target_role_and_preserves_explicit_flags(self):
        parent = self.launch(role="commander", profile_id=self.glm["id"])
        self.assign({"scout": self.ds["id"], "general": self.ds["id"]})
        basis = self.app.store(self.pid).add_fact("fixture supplied", "user")["id"]
        env = {"SAPPER_AGENT_ID": parent["id"], "SAPPER_WORKSPACE_ID": self.pid}
        with mock.patch.dict(os.environ, env), mock.patch("sapper_workspace.console_from_environment", return_value=self.app):
            for flags, expected in [(["--role", "scout"], self.ds["id"]),
                                    (["--general"], self.ds["id"]),
                                    (["--role", "scout", "--profile", self.glm["id"]], self.glm["id"]),
                                    (["--role", "scout", "--profile", ""], "")]:
                with self.subTest(flags=flags), contextlib.redirect_stdout(io.StringIO()):
                    worker(["spawn", self.pid, "--from", basis, *flags])
                    self.assertEqual(self.start.call_args.args[1]["profile_id"], expected)
            with contextlib.redirect_stdout(io.StringIO()):
                worker(["spawn", self.pid, "--from", basis, "--role", "scout", "--model", "ds-override"])
            profile_id = self.start.call_args.args[1]["profile_id"]
            chosen = next(p for p in self.app.profiles() if p["id"] == profile_id)
            self.assertEqual((chosen["model"], chosen["protocol"]), ("ds-override", "chat_completions"))


if __name__ == "__main__":
    unittest.main()
