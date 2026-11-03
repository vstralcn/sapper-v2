"""Shared-context protocol and original-engine compatibility checks."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
import sapper_context as context
from sapper_web import Console, WebError


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.app = Console(self.base / "tasks", self.base / "private")
        self.data = {"expected_revision": 0, "enabled": True, "prompt_enabled": True, "hook_enabled": True,
                     "content": "Linux x86_64 debugging server: ssh x64-debug"}

    def tearDown(self):
        self.temp.cleanup()

    def test_versions_conflicts_and_private_storage_preserve_other_configuration(self):
        self.app.save_profile({"name": "unchanged", "model": "fixture"})
        before = self.app.config_path.read_bytes()
        saved = self.app.save_context(self.data)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(self.app.config_path.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.app.context_path.stat().st_mode), 0o600)
        self.assertEqual(self.app.save_context({**self.data, "expected_revision": 1})["revision"], 1)
        with self.assertRaises(WebError):
            self.app.save_context({**self.data, "content": "stale edit"})
        for changes in ({"content": "x" * 12001}, {"enabled": "yes"}, {"prompt_enabled": False, "hook_enabled": False}):
            with self.subTest(changes=changes.keys()), self.assertRaises(WebError):
                self.app.save_context({**self.data, "expected_revision": 1, **changes})
        reloaded = Console(self.app.root, self.app.state)
        self.assertEqual(reloaded.global_context()["content"], self.data["content"])

    def test_real_hooks_are_opt_in_deduplicate_and_read_updates_without_a_graph(self):
        self.app.save_context(self.data)
        hook = Path(sapper.__file__).with_name("sapper-hook.py")
        env = {**os.environ, context.CONTEXT_ENV: str(self.app.context_path), "SAPPER_AGENT_ID": "ab" * 8,
               context.PROMPT_ENV: ""}
        def invoke(event):
            p = subprocess.run([sys.executable, str(hook), event], input=json.dumps({"cwd": str(self.base)}),
                               cwd=self.base, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout
        for event in ("sessionstart", "userpromptsubmit"):
            self.assertIn("ssh x64-debug", invoke(event))
        env[context.PROMPT_ENV] = "1"
        self.assertEqual(invoke("sessionstart"), "")
        receipt = context.receipt(self.app.state, "ab" * 8)
        self.assertEqual(receipt["source"], "startup-prompt")
        self.app.save_context({**self.data, "expected_revision": 1, "content": "UPDATED_RESOURCE"})
        self.assertIn("UPDATED_RESOURCE", invoke("userpromptsubmit"))
        self.app.save_context({**self.data, "expected_revision": 2, "enabled": False})
        self.assertEqual(invoke("userpromptsubmit"), "")

    def fake_cli(self):
        cli = self.base / "fake-cli"
        cli.write_text("#!/usr/bin/env python3\nimport sys,json,os\nfrom pathlib import Path\n"
                       "Path(__file__+'.args').write_text(json.dumps({'argv':sys.argv,'input':sys.stdin.read(),'revision':os.environ.get('SAPPER_CONTEXT_PROMPT_REVISION')}))\n"
                       "print(json.dumps({'result':'original-result'}))\n")
        cli.chmod(0o700)
        return cli

    def test_original_dispatch_preserves_protocol_and_stdin_with_global_prompt(self):
        self.app.save_context(self.data)
        cli = self.fake_cli()
        with mock.patch.dict(os.environ, {context.CONTEXT_ENV: str(self.app.context_path), "SAPPER_BIN": str(cli)}):
            result = sapper.run_claude("original task", model="fixture", timeout=5, skip_permissions=False,
                                       append_system="ORIGINAL_ROLE_AND_JSON_PROTOCOL", cwd=self.base)
        self.assertEqual(result["result"], "original-result")
        captured = json.loads(Path(str(cli) + ".args").read_text())
        appended = captured["argv"][captured["argv"].index("--append-system-prompt") + 1]
        self.assertIn("ORIGINAL_ROLE_AND_JSON_PROTOCOL", appended)
        self.assertIn("ssh x64-debug", appended)
        self.assertEqual(captured["input"], "original task")
        self.assertEqual(captured["revision"], "1")

    def test_legacy_launcher_merges_hooks_and_keeps_configured_text_literal(self):
        marker = self.base / "must-not-exist"
        literal = f"Resource info: $(touch {marker}); `touch {marker}`"
        self.app.save_context({**self.data, "content": literal})
        settings = self.base / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo original-hook"}]}]}}))
        cli = self.fake_cli()
        p = subprocess.run([sys.executable, str(Path(context.__file__)), "exec", str(cli), "--append-system-prompt", "ORIGINAL"],
                           input="stdin preserved", cwd=self.base, env={**os.environ, context.CONTEXT_ENV: str(self.app.context_path)},
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        args = json.loads(Path(str(cli) + ".args").read_text())
        self.assertIn(literal, args["argv"][-1])
        self.assertIn("ORIGINAL", args["argv"][-1])
        self.assertFalse(marker.exists())
        merged = json.loads(settings.read_text())
        self.assertEqual(merged["permissions"]["allow"], ["Read"])
        self.assertIn("echo original-hook", settings.read_text())
        self.assertIn("UserPromptSubmit", merged["hooks"])

    def test_context_injection_preserves_credentials_rotated_between_retries(self):
        self.app.save_context(self.data)
        cli = self.base / "retry-cli"
        cli.write_text("#!/usr/bin/env python3\nimport os,json,sys\n"
                       "if os.environ.get('ANTHROPIC_AUTH_TOKEN') == 'synthetic-old':\n"
                       " print('quota exceeded',file=sys.stderr);sys.exit(1)\n"
                       "print(json.dumps({'result':os.environ.get('ANTHROPIC_AUTH_TOKEN')}))\n")
        cli.chmod(0o700)
        def rotate():
            os.environ["ANTHROPIC_AUTH_TOKEN"] = "synthetic-new"
            return True
        with mock.patch.dict(os.environ, {context.CONTEXT_ENV: str(self.app.context_path), "SAPPER_BIN": str(cli),
                                         "SAPPER_RETRY_MODEL": "1", "ANTHROPIC_AUTH_TOKEN": "synthetic-old"}), \
                mock.patch.object(sapper, "_rotate_key", side_effect=rotate) as rotation:
            result = sapper.run_claude("original task", model="fixture", timeout=5, skip_permissions=False,
                                       append_system=None, cwd=self.base)
        self.assertEqual(result["result"], "synthetic-new")
        rotation.assert_called_once()
