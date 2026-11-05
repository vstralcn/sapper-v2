"""Exercise the actual hook entry point, not only its settings registration."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper

HOOK = Path(sapper.__file__).with_name("sapper-hook.py")


class HookProcessTests(unittest.TestCase):
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
