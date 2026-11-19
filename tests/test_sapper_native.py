"""Native CLI coverage and real isolated-tmux integration, without a platform."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
from sapper_web import Console, WebError
from sapper_native import EXCLUDED, NativeError

ENGINE = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
prompt=sys.stdin.read()
with Path(__file__).with_suffix('.calls').open('a') as f:
 f.write(json.dumps({'cwd':os.getcwd(),'argv':sys.argv,'model':os.environ.get('SAPPER_MODEL'),'token':os.environ.get('ANTHROPIC_AUTH_TOKEN')})+'\n')
if 'FIELD COMMANDER' in prompt:
 payload={'accepted':True,'data':{'action':'work','target':os.environ['NATIVE_TEST_TARGET'],'minutes':1}}
else:
 payload={'accepted':True,'data':{'complete':{'from':['f001'],'description':'fixture verified completion'}}}
print(json.dumps({'result':json.dumps(payload)}))
'''


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native contract ")
        self.base = Path(self.temp.name).resolve()
        self.app = Console(self.base / "tasks", self.base / "private")
        self.project = self.app.create_project({"name": "question", "goal": "native fixture"})
        self.pid = self.project["id"]
        self.root = Path(self.project["path"])

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_covers_every_non_platform_command_and_original_parameter(self):
        parser = sapper.build_parser()
        native = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
        catalog = {c["name"]: c for c in self.app.native.catalog()["commands"]}
        self.assertEqual(set(catalog), set(native) - EXCLUDED)
        self.assertEqual(len(catalog), 32)
        for name, command in catalog.items():
            expected = {a.dest for a in native[name]._actions if not isinstance(a, argparse._HelpAction)}
            if name == "flag": expected.remove("submit")
            self.assertEqual({f["name"] for f in command["fields"]}, expected, name)

    def test_preview_checks_original_parser_literal_arguments_and_scope(self):
        request = {"operation": "fact", "values": {"description": "--literal $(touch nope)", "data": ["port=80", "host=local"]}}
        p = self.app.native.prepare(self.pid, request)
        self.assertEqual(p["argv"][-1], request["values"]["description"])
        self.assertIn("--data=port=80", p["argv"])
        for op in ("arena", "bench", "web"):
            with self.subTest(op=op), self.assertRaises(NativeError):
                self.app.native.prepare(self.pid, {"operation": op})
        with self.assertRaises(NativeError):
            self.app.native.prepare(self.pid, {"operation": "flag", "values": {"value": "flag{x}", "submit": True}})
        with self.assertRaises(NativeError):
            self.app.native.prepare(self.pid, {"operation": "dispatch", "values": {"target": "reason", "timeout": "bad"}})
        with self.assertRaises(NativeError):
            self.app.native.prepare(self.pid, {"operation": "doc", "values": {"id": "f001", "set_file": "/etc/passwd"}})
        with self.assertRaises(NativeError):
            self.app.native.prepare("global", {"operation": "status"})

    def test_templates_are_local_complete_and_optimistically_saved(self):
        template = self.app.native.read_config(self.pid, "templates/reason.md")
        self.assertFalse(template["exists"])
        (self.root / "templates").mkdir()
        (self.root / "templates/explore.md").write_text("USER EXPLORE")
        content = template["content"] + "\nUSER LOCAL ADDITION\n"
        result = self.app.native.save_config(self.pid, {"path": template["path"], "content": content, "version": None})
        self.assertEqual((self.root / "templates/explore.md").read_text(), "USER EXPLORE")
        self.assertTrue((self.root / "templates/verify.md").exists())
        self.assertIn("USER LOCAL ADDITION", result["content"])
        with self.assertRaises(NativeError):
            self.app.native.save_config(self.pid, {"path": template["path"], "content": "stale", "version": None})
        with self.assertRaises((NativeError, WebError)):
            self.app.native.save_config(self.pid, {"path": ".sapper/project.json", "content": "{}", "version": None})

    def test_graph_lifecycle_keeps_confirmation_and_worker_guards(self):
        fact = self.app.graph_action(self.pid, "fact", {"description": "confirmed endpoint", "kind": "service", "data": {"port": 8080}})
        self.assertEqual(fact["data"]["port"], "8080")
        intent = self.app.graph_action(self.pid, "intent", {"from": [fact["id"]], "description": "verify endpoint", "requires_confirmation": True})
        before = len(self.app.store(self.pid).list_facts())
        with self.assertRaises(sapper.FGError):
            self.app.graph_action(self.pid, "done", {"id": intent["id"], "description": "premature", "worker": "one"})
        self.assertEqual(len(self.app.store(self.pid).list_facts()), before)
        self.app.graph_action(self.pid, "confirm", {"id": intent["id"]})
        self.app.graph_action(self.pid, "claim", {"id": intent["id"], "worker": "one"})
        with self.assertRaises(sapper.FGError):
            self.app.graph_action(self.pid, "release", {"id": intent["id"], "worker": "other"})
        self.app.graph_action(self.pid, "release", {"id": intent["id"], "worker": "one"})
        self.app.graph_action(self.pid, "claim", {"id": intent["id"], "worker": "one"})
        result = self.app.graph_action(self.pid, "done", {"id": intent["id"], "description": "confirmed result", "worker": "one", "doc": "evidence"})
        self.app.graph_action(self.pid, "complete", {"from": [result["id"]], "note": "verified", "worker": "one"})
        self.assertEqual(sapper.validate_graph(self.app.store(self.pid)), [])

    def test_preview_rejects_changed_fleet_target_and_requires_destructive_ack(self):
        cfg = {"session": "owned-fleet", "root": str(self.app.root), "panes": [{"type": "shell", "command": "echo fixture"}]}
        file = self.app.native.save_config(self.pid, {"path": "fleet.json", "content": json.dumps(cfg), "version": None})
        data = {"operation": "fleet", "values": {"action": "down"}}
        p = self.app.native.prepare(self.pid, data)
        with self.assertRaises(NativeError):
            self.app.native.execute(self.pid, {**data, "preview_token": p["preview_token"]})
        self.app.native.save_config(self.pid, {"path": "fleet.json", "content": json.dumps({**cfg, "session": "changed-fleet"}), "version": file["version"]})
        with self.assertRaises(NativeError):
            self.app.native.execute(self.pid, {**data, "preview_token": p["preview_token"], "acknowledged": True})

    def test_brainstorm_selection_uses_original_batch_semantics(self):
        store = self.app.store(self.pid)
        seed = store.add_fact("seed", "test")
        opts = [store.add_intent([seed["id"]], f"option {i}", "test", requires_confirmation=True, doc="[brainstorm batch: native-test]") for i in range(2)]
        self.app.graph_action(self.pid, "brainstorm-pick", {"id": opts[0]["id"]})
        self.assertTrue(store.get_intent(opts[0]["id"])["confirmed_at"])
        self.assertIsNotNone(store.get_intent(opts[1]["id"])["to"])
        self.assertEqual(sapper.validate_graph(store), [])

    def test_commander_fleet_context_respects_roots_and_invalidates_preview(self):
        cfg = {"session": "fixture-fleet", "root": str(self.app.root),
               "panes": [{"name": "board", "type": "shell", "command": "true"}]}
        path = self.root / "fleet.json"
        path.write_text(json.dumps(cfg))
        request = {"operation": "command", "values": {"root": str(self.app.root), "minutes": 1}}
        old = self.app.native.prepare(self.pid, request)
        path.write_text(json.dumps({**cfg, "session": "changed-fleet"}))
        self.assertNotEqual(old["preview_token"], self.app.native.prepare(self.pid, request)["preview_token"])
        path.write_text(json.dumps({**cfg, "root": str(self.base / "outside")}))
        with self.assertRaises((NativeError, WebError)):
            self.app.native.prepare(self.pid, request)


@unittest.skipUnless(shutil.which("tmux"), "tmux unavailable")
class NativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native integration ")
        self.base = Path(self.temp.name).resolve()
        self.socket = "sapper-native-test-" + uuid.uuid4().hex[:10]
        self.kb = self.base / "shared kb"
        self.environment = mock.patch.dict(os.environ, {"SAPPER_KB_ROOT": str(self.kb), "NATIVE_TEST_TARGET": "question with spaces"})
        self.environment.start()
        self.app = Console(self.base / "tasks", self.base / "state", self.socket)
        question = self.app.root / "question with spaces"
        question.mkdir()
        sapper.Store(question / ".sapper").init("test", "native original fixture")
        self.project = self.app.workspaces.bind({"path": str(question)})
        self.pid = self.project["id"]
        self.root = Path(self.project["path"])
        self.engine = self.base / "fake model cli"
        self.engine.write_text(ENGINE);self.engine.chmod(0o700)
        self.profile = self.app.save_profile({"name": "native-test", "model": "fixture-model", "engine_prefix": shlex.quote(str(self.engine)), "api_key": "synthetic-native-key"})

    def tearDown(self):
        self.app.tmux("kill-server")
        self.environment.stop()
        self.temp.cleanup()

    def until(self, fn, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = fn()
            if value:return value
            time.sleep(.07)
        self.fail("native integration timed out")

    def execute(self, operation, values=None, *, pid=None, profile=False):
        pid = pid or self.pid
        request = {"operation": operation, "values": values or {}, "request_id": uuid.uuid4().hex}
        if profile:request["profile_id"] = self.profile["id"]
        preview = self.app.native.prepare(pid, request)
        request.update(preview_token=preview["preview_token"], acknowledged=preview["destructive"])
        run = self.app.native.execute(pid, request)
        self.assertEqual(self.app.native.execute(pid, request)["id"], run["id"])
        result = self.until(lambda: r if (r := self.app.run_info(run["id"]))["status"] in ("exited", "failed", "stopped") else None)
        self.assertEqual(result.get("exit_code"), 0, self.app.log(run["id"])["text"])
        return result, self.app.log(run["id"])["text"]

    def test_native_fact_intent_recall_doc_flag_and_graph_export(self):
        self.execute("fact", {"description": "literal $(not-a-command)", "kind": "service", "data": ["port=8080"], "doc": "ORIGINAL EVIDENCE"})
        self.execute("intent", {"description": "verify service", "from_": "f001"})
        self.execute("claim", {"id": "i001", "worker": "native-user"})
        self.execute("release", {"id": "i001", "worker": "native-user"})
        self.execute("done", {"id": "i001", "fact": "confirmed native result", "worker": "native-user"})
        _, output = self.execute("recall", {"query": "literal", "json": True})
        self.assertIn("f001", output)
        _, output = self.execute("doc", {"id": "f001"})
        self.assertIn("ORIGINAL EVIDENCE", output)
        self.execute("doc", {"id": "f001", "set": "UPDATED EVIDENCE"})
        self.execute("flag", {"value": "flag{local-only}"})
        self.assertEqual(next(f for f in self.app.store(self.pid).list_facts() if f.get("kind") == "flag")["data"]["status"], "recorded")
        self.execute("view", {"out": "native graph.html"})
        self.assertTrue((self.root / "native graph.html").exists())
        self.execute("validate")
        self.assertFalse((self.root / "not-a-command").exists())

    def test_global_knowledge_add_import_show_and_root_use_original_store(self):
        self.execute("kb", {"action": "add", "target": "native entry", "detail": "ORIGINAL KNOWLEDGE", "tag": ["debug"]}, pid="global")
        markdown = self.root / "knowledge source.md"
        markdown.write_text("# Imported knowledge\n\n> source description\n\nMarkdown details\n")
        self.execute("kb", {"action": "import", "path": str(markdown), "tag": ["native"]}, pid="global")
        _, output = self.execute("kb", {"action": "show", "target": "kb001"}, pid="global")
        self.assertIn("ORIGINAL KNOWLEDGE", output)
        _, output = self.execute("kb", {"action": "search", "target": "Imported"}, pid="global")
        self.assertIn("kb002", output)
        self.execute("kb", {"action": "root"}, pid="global")
        self.assertTrue((self.kb / "entries/kb002.json").exists())
        self.assertFalse((self.app.root / ".sapper").exists())

    def test_original_peer_discovery_authorization_send_capture_and_remove(self):
        peer = "native-peer-" + uuid.uuid4().hex[:6]
        self.app.tmux("new-session", "-d", "-s", peer, "-c", str(self.root), "cat")
        _, output = self.execute("peers", {"discover": True, "json": True})
        self.assertIn(peer, output)
        self.assertEqual(sapper._load_peers(self.app.store(self.pid))["peers"], {})
        self.execute("peers", {"add": "test-peer", "target": peer + ":0.0"})
        self.execute("send", {"peer": "test-peer", "message": "literal native peer hello"})
        _, output = self.execute("capture", {"peer": "test-peer", "lines": 120})
        self.assertIn("literal native peer hello", output)
        self.assertIn("literal native peer hello", (self.root / ".sapper/ai-channel.txt").read_text())
        self.execute("peers", {"remove": "test-peer"})
        self.assertEqual(sapper._load_peers(self.app.store(self.pid))["peers"], {})

    def test_teardown_preserves_user_instructions_graph_and_binding(self):
        (self.root / "CLAUDE.md").write_text("USER INSTRUCTIONS\n")
        self.app.workspaces.enable(self.pid, {})
        self.execute("teardown")
        self.assertIn("USER INSTRUCTIONS", (self.root / "CLAUDE.md").read_text())
        self.assertNotIn("sapper-workspace:start", (self.root / "CLAUDE.md").read_text())
        self.assertFalse((self.root / ".claude/sapper-workspace.md").exists())
        self.assertTrue((self.root / ".sapper/project.json").exists())
        self.execute("teardown", {"purge": True})
        self.assertFalse((self.root / ".sapper").exists())
        self.assertEqual(self.app.project(self.pid)["status"], "not_enabled")
        self.execute("init", {"goal": "restored native graph"})

    def test_original_auto_dispatch_sweep_and_command_without_platform(self):
        self.app.store(self.pid).add_fact("verified fixture evidence", "test")
        for operation, values in [("auto", {"minutes": 1, "max_steps": 1}),
                                  ("sweep", {"root": str(self.app.root), "minutes": 1, "minutes_per": 1, "max_steps": 1}),
                                  ("command", {"root": str(self.app.root), "minutes": 1, "poll_seconds": 0, "max_steps": 1})]:
            if self.app.store(self.pid).load_project()["status"] == "completed":
                self.execute("init", {"goal": "native fixture", "force": True})
                self.app.store(self.pid).add_fact("verified fixture evidence", "test")
            run, output = self.execute(operation, values, profile=True)
            self.assertEqual(self.app.store(self.pid).load_project()["status"], "completed", output)
            actor = self.app.workspaces.agent(run["agent_id"])
            self.assertIsNone(actor["claude_session_id"])
            self.assertEqual(actor["execution_mode"], "cli")
        calls = [json.loads(line) for line in self.engine.with_suffix(".calls").read_text().splitlines()]
        self.assertTrue(all(c["token"] == "synthetic-native-key" for c in calls))

    def test_dispatch_all_step_types_preserve_original_dry_run_and_execution(self):
        store = self.app.store(self.pid)
        seed = store.add_fact("verified fixture evidence", "test")
        intent = store.add_intent([seed["id"]], "verify fixture", "test")
        store.conclude_with_new_fact(intent["id"], "verified result", "test")
        for target in ("reason", "brainstorm", "blindspot", "verify"):
            values = {"target": target, "dry_run": True}
            if target == "verify": values["intent"] = intent["id"]
            _, output = self.execute("dispatch", values, profile=True)
            self.assertIn("verified result" if target == "verify" else "native original fixture", output)
        self.execute("dispatch", {"target": "reason"}, profile=True)
        self.assertEqual(store.load_project()["status"], "completed")

    def test_native_fleet_config_preview_status_up_hire_workers_fire_down(self):
        cfg = {"session": "native-fleet-" + uuid.uuid4().hex[:8], "root": str(self.app.root),
               "panes": [{"name": "board", "type": "shell", "command": "echo ORIGINAL_FLEET; sleep 30"}]}
        self.app.native.save_config(self.pid, {"path": "formation.json", "content": json.dumps(cfg), "version": None})
        self.execute("fleet", {"action": "status", "file": "formation.json"})
        self.execute("fleet", {"action": "up", "file": "formation.json"}, profile=True)
        screen = self.app.tmux("capture-pane", "-p", "-t", cfg["session"] + ":board").stdout
        self.assertIn("ORIGINAL_FLEET", screen)
        self.app.store(self.pid).add_fact("verified fixture evidence", "test")
        self.execute("fleet", {"action": "hire", "file": "formation.json", "target": self.project["name"], "minutes": 1, "max_steps": 1}, profile=True)
        self.until(lambda: self.app.store(self.pid).load_project()["status"] == "completed")
        _, output = self.execute("fleet", {"action": "workers", "file": "formation.json"})
        self.assertIn(self.project["name"], output)
        self.execute("fleet", {"action": "fire", "file": "formation.json", "target": self.project["name"]})
        self.execute("fleet", {"action": "down", "file": "formation.json"})
        self.assertNotEqual(self.app.tmux("has-session", "-t", cfg["session"]).returncode, 0)
