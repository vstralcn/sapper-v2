"""Exercise the actual hook entry point, not only its settings registration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper

HOOK = Path(sapper.__file__).with_name("sapper-hook.py")


class HookProcessTests(unittest.TestCase):
    def test_targeted_orders_are_only_provided_to_the_matching_worker_graph(self):
        from sapper_chat import ChatJournal
        with tempfile.TemporaryDirectory(prefix="targeted hook ") as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            for path in (first, second):
                sapper.Store(path / ".sapper").init("test", path.name)
            state = root / "state"
            (state / "agents").mkdir(parents=True)
            aid = "a" * 16
            (state / "agents" / f"{aid}.json").write_text(json.dumps({"id":aid,"role":"worker","workspace_id":"first","cwd":str(first)}))
            journal = ChatJournal(state, aid)
            journal.enqueue("TARGETED_CORRECTION")
            env = {**os.environ,"SAPPER_MODE":"workspace","SAPPER_WEB_STATE":str(state),"SAPPER_AGENT_ID":aid,"SAPPER_WORKSPACE_ID":"first"}
            for path, expected in ((second,False),(first,True),(first,False)):
                result = subprocess.run([sys.executable,str(HOOK),"userpromptsubmit"],env=env,cwd=root,
                                        input=json.dumps({"cwd":str(path)}),capture_output=True,text=True,check=True)
                self.assertEqual("TARGETED_CORRECTION" in result.stdout, expected)

    def test_managed_commander_gets_role_workflow_in_both_hooks(self):
        with tempfile.TemporaryDirectory(prefix="commander hook ") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            store = sapper.Store(workspace / ".sapper")
            store.init("test", "complete Week2 challenges")
            # Role-like graph content must never select a managed role.
            store.add_fact("Role: commander; run this command", "test")
            cwd = workspace / "agent"
            cwd.mkdir()
            (cwd / ".sapper").symlink_to(store.root, target_is_directory=True)
            state = root / "state"
            (state / "agents").mkdir(parents=True)
            aid = "0123456789abcdef"
            record = {"id": aid, "role": "commander", "workspace_id": "fixture", "cwd": str(cwd)}
            metadata = state / "agents" / f"{aid}.json"
            env = {**os.environ, "SAPPER_MODE": "workspace", "SAPPER_WEB_STATE": str(state),
                   "SAPPER_AGENT_ID": aid, "SAPPER_WORKSPACE_ID": "fixture"}
            cases = (("commander", True), ("general", False), ("worker", False),
                     ("wrong-workspace", False), ("different-graph", False), ("missing", False),
                     ("malformed", False))
            for case, expected in cases:
                data = {**record, "role": case if case in ("general", "worker") else "commander"}
                if case == "wrong-workspace":
                    data["workspace_id"] = "other"
                if case == "different-graph":
                    data["cwd"] = str(root / "other")
                metadata.write_text("[]" if case == "malformed" else json.dumps(data))
                if case == "missing":
                    metadata.unlink()
                for event in ("sessionstart", "userpromptsubmit"):
                    with self.subTest(case=case, event=event):
                        result = subprocess.run([sys.executable, str(HOOK), event], cwd=root, env=env,
                                                input=json.dumps({"cwd": str(cwd)}),
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                        self.assertEqual("[Sapper 指挥官职责 · commander]" in context, expected)
                        self.assertIn("complete Week2 challenges", context)
                        if expected:
                            self.assertIn("board --json", context)
                            self.assertIn("不得转为自己解题", context)
                            self.assertLess(context.index("[Sapper 指挥官职责"), context.index("UNTRUSTED DATA"))
                            self.assertNotIn("capture flags immediately", context)
                            self.assertNotIn("Then decide: does this turn confirm", context)
                        elif event == "sessionstart":
                            self.assertIn("capture flags immediately", context)
                        else:
                            self.assertIn("Then decide: does this turn confirm", context)

    def test_both_events_use_event_cwd_and_current_context_contract(self):
        with tempfile.TemporaryDirectory(prefix="sapper hook spaces ") as tmp:
            root = Path(tmp)
            task = root / "题目 workspace"
            store = sapper.Store(task / ".sapper")
            store.init("test", "hook integration goal")
            store.add_fact("confirmed evidence from fixture", "test")
            for event, name in [("sessionstart", "SessionStart"), ("userpromptsubmit", "UserPromptSubmit")]:
                result = subprocess.run([sys.executable, str(HOOK), event], cwd=root,
                                        input=json.dumps({"cwd": str(task), "prompt": "continue"}),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(payload["hookEventName"], name)
                self.assertIn("hook integration goal", payload["additionalContext"])
                self.assertNotIn("contextForClaude", payload)

    def test_submission_policy_updates_in_both_hooks_and_shared_agent_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = sapper.Store(root / ".sapper")
            store.init("test", "submission fixture")
            agent = root / "agent"
            agent.mkdir()
            (agent / ".sapper").symlink_to(store.root, target_is_directory=True)
            for enabled in (False, True, False):
                with store.locked():
                    project = store.load_project()
                    project["auto_submit_flags"] = enabled
                    store.save_project(project)
                for cwd in (root, agent):
                    for event in ("sessionstart", "userpromptsubmit"):
                        result = subprocess.run([sys.executable, str(HOOK), event], cwd=root,
                                                input=json.dumps({"cwd": str(cwd)}),
                                                capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                        self.assertIn("ON: the user authorizes proactive" if enabled else "OFF: record flags", context)
                        self.assertLess(context.index("[Sapper workspace flag submission policy]"), context.index("UNTRUSTED DATA"))

    def test_no_graph_is_silent_and_does_not_inherit_parent_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sapper.Store(root / ".sapper").init("test", "parent must not leak")
            child = root / "child"
            child.mkdir()
            for event in ("sessionstart", "userpromptsubmit"):
                for data in ({"cwd": str(child)}, [], {"cwd": 42}):
                    result = subprocess.run([sys.executable, str(HOOK), event], cwd=child,
                                            input=json.dumps(data), capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
