"""Resource selection, snapshot isolation and failure/recovery boundaries."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_web import Console, WebError
from sapper_workspace import WorkspaceError
from sapper_extensions import ExtensionError, REQUIRED_FLAGS, api_environment, clean_environment


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sapper extensions ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.app = Console(self.base / "tasks", self.base / "state")
        self.project = self.app.create_project({"name": "fixture", "goal": "extensions"})
        self.pid = self.project["id"]
        self.root = Path(self.project["path"])
        self.manager = self.app.workspaces
        self.manager.enable(self.pid, {})
        self.manager._capabilities = {k: True for k in (*REQUIRED_FLAGS, "available", "session_id", "resume", "append_system", "skill_isolation")}
        self.ext = self.manager.extensions
        self.source = self.base / "my-skill"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("---\nname: selected\ndescription: Test selected skill\n---\nRead the fixture.\n")
        (self.source / "reference.txt").write_text("original reference")
        self.app.panes = lambda: []
        self.launches = []
        self.start = mock.patch.object(self.app, "start", side_effect=self.capture).start()
        self.addCleanup(mock.patch.stopall)

    def capture(self, pid, data, native):
        self.launches.append(native)
        return {"id": f"{len(self.launches):016x}", "agent_id": native["agent_id"]}

    def update(self, **data):
        return self.ext.update(self.pid, {"expected_revision": self.ext.read()["revision"], **data})

    def resources(self):
        self.update(action="save", kind="skills", name="selected", path=str(self.source))
        self.update(action="save", kind="mcp", name="chosen", config={"command": "fixture-server", "env": {"API_KEY": "${EXTENSION_TEST_KEY}"}})
        cfg = self.ext.read()
        selection = {"mcp_ids": [cfg["mcp"][0]["id"]], "skill_ids": [cfg["skills"][0]["id"]]}
        self.update(action="defaults", selection=selection)
        return selection

    def launch(self, **data):
        run = self.manager.create_session(self.pid, {"mission": "fixture", **data})
        return self.manager.agent(run["agent_id"])

    def test_global_library_without_workspace_and_independent_selections(self):
        app = Console(self.base / "empty-tasks", self.base / "empty-state")
        ext = app.workspaces.extensions
        app.workspaces._capabilities = {"available": False}
        self.assertEqual(app.projects(), [])
        data = ext.update(None, {"action": "save", "kind": "mcp", "name": "global-server",
                                 "config": {"command": "fixture"}, "expected_revision": 0})
        self.assertEqual(data["workspaces"], {})
        self.assertEqual(len(ext.view()["mcp"]), 1)
        with self.assertRaisesRegex(ExtensionError, "选择要配置的工作区"):
            ext.update(None, {"action": "defaults", "selection": {"mcp_ids": [], "skill_ids": []}, "expected_revision": 1})
        first = app.create_project({"name": "first", "goal": "first"})
        second = app.create_project({"name": "second", "goal": "second"})
        selection = {"mcp_ids": [data["mcp"][0]["id"]], "skill_ids": []}
        ext.update(first["id"], {"action": "defaults", "selection": selection, "expected_revision": 1})
        self.assertEqual(ext.view(first["id"])["selection"], selection)
        self.assertEqual(ext.view(second["id"])["selection"]["mcp_ids"], [])
        with self.assertRaisesRegex(ExtensionError, "取消此资源"):
            ext.update(None, {"action": "delete", "kind": "mcp", "id": selection["mcp_ids"][0], "expected_revision": 2})
        ext.update(first["id"], {"action": "defaults", "selection": {"mcp_ids": [], "skill_ids": []}, "expected_revision": 2})
        ext.update(None, {"action": "delete", "kind": "mcp", "id": selection["mcp_ids"][0], "expected_revision": 3})
        self.assertEqual(ext.view()["mcp"], [])

    def test_distinct_sessions_override_defaults_and_resume_original_snapshot(self):
        selection = self.resources()
        before = (self.root / ".claude/settings.json").read_bytes()
        one = self.launch()
        two = self.launch(extensions={"mcp_ids": [], "skill_ids": []})
        self.assertNotEqual(one["claude_config_dir"], two["claude_config_dir"])
        directory = self.root / ".sapper/sessions" / one["claude_session_id"]
        self.assertEqual(one["session_storage"], str(directory))
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads((directory / "bundle/mcp.json").read_text())["mcpServers"]["chosen"]["env"]["API_KEY"], "${EXTENSION_TEST_KEY}")
        self.assertEqual(two["extensions"]["skills"], [])
        self.assertEqual(json.loads((Path(two["session_storage"]) / "bundle/mcp.json").read_text()), {"mcpServers": {}})
        argv = self.launches[0]["argv"]
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertTrue(self.launches[0]["skip_hook_install"])
        settings = json.loads((directory / "bundle/settings.json").read_text())
        self.assertEqual(set(settings["hooks"]), {"SessionStart", "UserPromptSubmit"})
        self.assertTrue(settings["disableBundledSkills"])
        (self.source / "reference.txt").write_text("changed after launch")
        self.update(action="defaults", selection={"mcp_ids": [], "skill_ids": []})
        self.update(action="delete", kind="skills", id=selection["skill_ids"][0])
        self.manager.resume(one["id"], {"extensions": {"mcp_ids": [], "skill_ids": []}})
        self.assertEqual(self.launches[-1]["env"]["CLAUDE_CONFIG_DIR"], one["claude_config_dir"])
        self.assertEqual(self.launches[-1]["claude_session_id"], one["claude_session_id"])
        self.assertEqual((directory / "bundle/plugin/skills/selected/reference.txt").read_text(), "original reference")
        self.assertEqual((self.root / ".claude/settings.json").read_bytes(), before)
        reloaded = Console(self.app.root, self.app.state)
        self.assertEqual(reloaded.workspaces.agent(one["id"])["extensions"], one["extensions"])

    def test_changed_or_missing_snapshot_cannot_silently_resume(self):
        self.resources()
        for target in ("bundle/mcp.json", "manifest.json", "bundle/plugin/skills/selected/reference.txt"):
            with self.subTest(target=target):
                a = self.launch()
                (Path(a["session_storage"]) / target).write_text("changed")
                count = len(self.launches)
                with self.assertRaisesRegex(ExtensionError, "快照"):
                    self.manager.resume(a["id"], {})
                self.assertEqual(len(self.launches), count)

    def test_missing_capability_fails_before_creating_snapshot_or_claiming_intent(self):
        basis = self.app.store(self.pid).add_fact("fixture", "user")["id"]
        for capability in (*REQUIRED_FLAGS, "skill_isolation"):
            with self.subTest(capability=capability):
                self.manager._capabilities[capability] = False
                with self.assertRaisesRegex(WorkspaceError, "隔离"):
                    self.launch(**{"from": basis})
                self.manager._capabilities[capability] = True
        self.assertFalse((self.root / ".sapper/sessions").exists())
        self.assertEqual(self.app.store(self.pid).list_intents(), [])

    def test_sources_symlinks_stale_writes_and_secrets(self):
        self.resources()
        with self.assertRaisesRegex(ExtensionError, "重新读取"):
            self.ext.update(self.pid, {"expected_revision": 0, "action": "defaults", "selection": {"mcp_ids": [], "skill_ids": []}})
        for config in ({"command": "server", "env": {"TOKEN": "plaintext-secret"}}, {"type": "http", "url": "https://example.test/?token=secret"}):
            with self.assertRaises(ExtensionError):
                self.update(action="save", kind="mcp", name="unsafe", config=config)
        (self.source / "external").symlink_to(self.base / "state")
        with self.assertRaisesRegex(ExtensionError, "符号链接"):
            self.launch()
        with mock.patch.dict(os.environ, {"EXTENSION_TEST_KEY": "fixture-mcp-secret"}):
            self.assertNotIn("fixture-mcp-secret", self.app.redact("error fixture-mcp-secret"))
            self.assertNotIn("fixture-mcp-secret", json.dumps(self.ext.view(self.pid)))
        self.assertEqual(self.ext.path.stat().st_mode & 0o777, 0o600)

    def test_child_uses_target_workspace_defaults_and_legacy_keeps_original_directory(self):
        parent = self.launch(role="commander")
        self.resources()
        basis = self.app.store(self.pid).add_fact("fixture", "user")["id"]
        child = self.launch(parent_agent_id=parent["id"], **{"from": basis})
        self.assertEqual(child["extensions"]["skills"][0]["name"], "selected")
        self.assertEqual(parent["extensions"]["skills"], [])
        old = self.launch()
        old.pop("session_storage"); old.pop("extension_mode"); old.pop("extension_manifest_sha256")
        old["claude_config_dir"] = str(self.base / "old-claude")
        self.manager.save_agent(old)
        self.manager.resume(old["id"], {})
        self.assertNotIn("--plugin-dir", self.launches[-1]["argv"])
        self.assertEqual(self.launches[-1]["env"]["CLAUDE_CONFIG_DIR"], old["claude_config_dir"])

    def test_raw_session_files_blocked_including_aliases(self):
        a = self.launch()
        for path in (f".sapper/sessions/{a['claude_session_id']}/manifest.json", ".sapper/sessions"):
            with self.assertRaises(WebError):
                self.app.file(self.pid, path)
        (self.root / "alias").symlink_to(a["session_storage"], target_is_directory=True)
        with self.assertRaises(WebError):
            self.app.file(self.pid, "alias/manifest.json")

    def test_clean_environment_keeps_api_route_but_not_oauth_or_other_sessions(self):
        env = {"CLAUDE_CONFIG_DIR": "old", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDECODE": "1", "ANTHROPIC_API_KEY": "api", "ANTHROPIC_BASE_URL": "http://localhost", "PATH": "/bin"}
        self.assertEqual(clean_environment(env), {"ANTHROPIC_API_KEY": "api", "ANTHROPIC_BASE_URL": "http://localhost", "PATH": "/bin"})

    def test_api_key_approval_uses_private_truncated_native_marker(self):
        directory = self.base / "native"
        directory.mkdir()
        path = directory / ".claude.json"
        key = "prefix-not-persisted-secret-key-1234567890"
        path.write_text(json.dumps({"projects": {"/fixture": {"history": ["keep"]}},
                                    "customApiKeyResponses": {"approved": [], "rejected": [key[-20:]]}}))
        env = {"ANTHROPIC_API_KEY": key, "CLAUDE_CONFIG_DIR": str(directory)}
        self.assertIs(api_environment(env), env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], key)
        data = json.loads(path.read_text())
        self.assertEqual(data["customApiKeyResponses"], {"approved": [key[-20:]], "rejected": []})
        self.assertEqual(data["projects"], {"/fixture": {"history": ["keep"]}})
        self.assertTrue(data["hasCompletedOnboarding"])
        self.assertNotIn(key, path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_api_only_and_missing_mcp_variables_fail_before_starting_process(self):
        self.resources()
        self.launch()
        native = self.launches[-1]
        with mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "CLAUDE_CODE_OAUTH_TOKEN": "must-not-be-used"}, clear=True):
            with self.assertRaisesRegex(WebError, "API Key"):
                Console.start(self.app, self.pid, {}, native=native)
            os.environ["ANTHROPIC_API_KEY"] = "local-fixture-key"
            with self.assertRaisesRegex(WebError, "EXTENSION_TEST_KEY"):
                Console.start(self.app, self.pid, {}, native=native)
        self.assertEqual(self.app.runs(), [])

    def test_workspace_instruction_imports_are_snapshotted(self):
        (self.root / "guide.md").write_text("IMPORTED_INSTRUCTION")
        (self.root / "CLAUDE.md").write_text("PROJECT_INSTRUCTION\n@guide.md\n```\n@not-a-file\n```\n")
        a = self.launch()
        argv = self.launches[-1]["argv"]
        prompt = argv[argv.index("--append-system-prompt") + 1]
        self.assertIn("IMPORTED_INSTRUCTION", prompt)
        (self.root / "guide.md").write_text("changed")
        self.manager.resume(a["id"], {})
        argv = self.launches[-1]["argv"]
        self.assertIn("IMPORTED_INSTRUCTION", argv[argv.index("--append-system-prompt") + 1])


if __name__ == "__main__":
    unittest.main()
