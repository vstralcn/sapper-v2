"""Workspace environment variables: storage, write-only editing, launch injection."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_web import Console, WebError, private_json


class WorkspaceEnvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sapper workspace env ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.app = Console(self.base / "tasks", self.base / "state")
        self.project = self.app.create_project({"name": "fixture", "goal": "env"})
        self.pid = self.project["id"]

    def save(self, vars, remove=None, revision=None):
        if revision is None:
            revision = self.app.workspace_env(self.pid)["revision"]
        return self.app.save_workspace_env(
            {"vars": vars, "remove": remove or [], "expected_revision": revision}, self.pid)

    def test_create_project_stores_env_and_view_is_write_only(self):
        project = self.app.create_project({"name": "with-env", "goal": "g",
                                           "env": {"MCP_KEY": "create-secret", "EMPTY_OK": ""}})
        view = self.app.workspace_env(project["id"])
        self.assertEqual(view["vars"], [{"name": "EMPTY_OK", "has_value": False},
                                        {"name": "MCP_KEY", "has_value": True}])
        self.assertNotIn("create-secret", json.dumps(view))
        stored = self.app.config()["workspace_env"][project["id"]]
        self.assertEqual(stored["MCP_KEY"], "create-secret")

    def test_bind_stores_env(self):
        folder = self.base / "bound"
        folder.mkdir()
        record = self.app.workspaces.bind({"path": str(folder), "env": {"MCP_KEY": "bind-secret"}})
        self.assertEqual(self.app.config()["workspace_env"][record["id"]]["MCP_KEY"], "bind-secret")
        self.assertEqual(self.app.workspace_env(record["id"])["vars"],
                         [{"name": "MCP_KEY", "has_value": True}])

    def test_validation_rejects_bad_names_reserved_and_limits(self):
        for bad in ({"1BAD": "x"}, {"has-dash": "x"}, {"SAPPER_ROOT": "x"},
                    {"CLAUDE_CONFIG_DIR": "x"}, {"ANTHROPIC_API_KEY": "x"}, {"PATH": "x"},
                    {"A" * 65: "x"}, {"OK": "v" * 4097}, {"OK": "nul\x00"},
                    {f"V{i}": "x" for i in range(51)}):
            with self.subTest(bad=bad), self.assertRaises(WebError):
                self.app.validate_workspace_env(bad)
        with self.assertRaises(WebError):
            self.app.validate_workspace_env(["not", "a", "dict"])
        self.assertEqual(self.app.validate_workspace_env({"MCP_KEY": "v"}), {"MCP_KEY": "v"})

    def test_create_project_rejects_invalid_env_without_creating(self):
        before = self.app.projects()
        with self.assertRaises(WebError):
            self.app.create_project({"name": "bad-env", "goal": "g", "env": {"PATH": "x"}})
        self.assertEqual(self.app.projects(), before)

    def test_save_keeps_empty_value_removes_and_conflicts(self):
        self.save({"MCP_KEY": "first", "OTHER": "second"})
        # OTHER empty keeps its stored value; NEW is added; MCP_KEY is removed
        # (a removed row is absent from vars, matching the write-only UI).
        view = self.save({"OTHER": "", "NEW": "third"}, remove=["MCP_KEY"])
        names = {v["name"] for v in view["vars"]}
        self.assertEqual(names, {"OTHER", "NEW"})
        stored = self.app.config()["workspace_env"][self.pid]
        self.assertEqual(stored["OTHER"], "second")
        self.assertEqual(stored["NEW"], "third")
        # Keeping a value that was never set is rejected.
        with self.assertRaises(WebError):
            self.save({"GHOST": ""})
        # Stale revision is rejected.
        with self.assertRaises(WebError):
            self.save({"MCP_KEY": "x"}, revision=0)

    def test_delete_workspace_clears_env(self):
        self.save({"MCP_KEY": "x"})
        self.assertIn(self.pid, self.app.config()["workspace_env"])
        self.app.workspaces.delete_workspace(self.pid, {"confirm": self.project["name"]})
        self.assertNotIn(self.pid, self.app.config().get("workspace_env", {}))

    def test_env_value_is_redacted_from_run_records(self):
        self.save({"MCP_KEY": "ws-redaction-secret"})
        rid = "e" * 16
        private_json(self.app.run_path(rid) / "run.json",
                     {"id": rid, "project_id": self.pid, "status": "exited",
                      "created_at": "2026-01-01", "command": "ws-redaction-secret"})
        result = json.dumps(self.app.runs(), ensure_ascii=False)
        self.assertNotIn("ws-redaction-secret", result)
        self.assertIn("[已隐藏密钥]", result)


@unittest.skipUnless(shutil.which("python3"), "python3 required")
class WorkspaceEnvLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sapper workspace env launch ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.app = Console(self.base / "tasks", self.base / "state")
        self.project = self.app.create_project({"name": "fixture", "goal": "env launch"})
        self.pid = self.project["id"]

    def tearDown(self):
        self.app.stop_all_runs()
        self.temp.cleanup()

    def until(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("timed out waiting for run")

    def finished(self, rid):
        return self.until(lambda: (r if (r := self.app.run_info(rid))["status"] in
                                   {"exited", "failed", "stopped", "timed_out"} else None))

    def native_print(self, code, **native):
        return self.app.start(self.pid, {"kind": "claude-native"}, native={
            "argv": [sys.executable, "-u", "-c", code], "cwd": self.project["path"],
            "execution_mode": "print", "output_format": "stream-json", "timeout_seconds": 8, **native})

    def test_missing_mcp_env_ref_blocks_until_workspace_env_set(self):
        code = "import os,json;print(json.dumps({'type':'result','subtype':'success','result':os.environ['WS_MCP_KEY']}),flush=True)"
        with self.assertRaisesRegex(WebError, "MCP 所需环境变量未配置"):
            self.native_print(code, mcp_env_refs=["WS_MCP_KEY"])
        self.app.save_workspace_env({"vars": {"WS_MCP_KEY": "launch-secret"}, "expected_revision": 0}, self.pid)
        run = self.native_print(code, mcp_env_refs=["WS_MCP_KEY"])
        info = self.finished(run["id"])
        # Exit 0 proves the variable reached the process; the value is redacted.
        self.assertEqual(info["exit_code"], 0)
        self.assertIn("[已隐藏密钥]", self.app.log(run["id"])["text"])
        self.assertNotIn("launch-secret", self.app.log(run["id"])["text"])

    def test_workspace_env_injected_into_custom_run(self):
        self.app.save_workspace_env({"vars": {"WS_PLAIN": "plain-value"}, "expected_revision": 0}, self.pid)
        code = "import os;print(os.environ['WS_PLAIN'],flush=True)"
        run = self.app.start(self.pid, {"kind": "custom", "command": shlex.join([sys.executable, "-u", "-c", code])})
        self.assertEqual(self.finished(run["id"])["exit_code"], 0)

    def test_workspace_env_overrides_service_environment(self):
        self.app.save_workspace_env({"vars": {"WS_OVERRIDE": "workspace-wins"}, "expected_revision": 0}, self.pid)
        code = "import os;print(os.environ['WS_OVERRIDE'],flush=True)"
        with mock.patch.dict(os.environ, {"WS_OVERRIDE": "service-loses"}):
            run = self.app.start(self.pid, {"kind": "custom", "command": shlex.join([sys.executable, "-u", "-c", code])})
        self.assertEqual(self.finished(run["id"])["exit_code"], 0)
        # The workspace value is redacted; the service value must not leak, which
        # proves the workspace layer won over the service environment.
        self.until(lambda: "[已隐藏密钥]" in self.app.log(run["id"])["text"])
        self.assertNotIn("service-loses", self.app.log(run["id"])["text"])


if __name__ == "__main__":
    unittest.main()