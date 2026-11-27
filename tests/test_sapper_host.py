"""Control-plane SubprocessHost tests — no tmux, no real model CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from sapper_host import (
    HANDSHAKE_REQUEST_ID, SubprocessHost, clean_headless_environment,
    startup_timeout_for, validate_init_ack, init_request_id,
)


class HostEnvironmentTests(unittest.TestCase):
    def test_terminal_env_is_stripped_and_no_color_set(self):
        env = {"TERM": "xterm", "TMUX": "/tmp/tmux", "TMUX_PANE": "%1",
               "SAPPER_TMUX_SOCKET": "abc", "PATH": "/usr/bin", "ANTHROPIC_AUTH_TOKEN": "secret"}
        clean = clean_headless_environment(env)
        for key in ("TERM", "TMUX", "TMUX_PANE", "SAPPER_TMUX_SOCKET"):
            self.assertNotIn(key, clean)
        self.assertEqual(clean["NO_COLOR"], "1")
        self.assertEqual(clean["ANTHROPIC_AUTH_TOKEN"], "secret")
        self.assertEqual(env["TERM"], "xterm")  # input untouched

    def test_startup_timeout_is_per_role_and_bounded(self):
        self.assertLess(startup_timeout_for("scout"), startup_timeout_for("commander"))
        self.assertIsInstance(startup_timeout_for("unknown"), int)
        self.assertGreater(startup_timeout_for("unknown"), 0)

    def test_spawn_records_monotonic_origin_for_single_clock(self):
        # Frozen spec v1.0 二: the startup timeout is measured from spawn with
        # one clock shared by the supervisor, not a fresh CLI-child clock.
        root = Path(tempfile.mkdtemp(prefix="spawn-clock "))
        try:
            host = SubprocessHost(root / "run", role="worker", generation=1)
            child, record = host.spawn([sys.executable, "-c", "import time; time.sleep(0.1)"],
                                       root, {"PATH": os.environ.get("PATH", "")},
                                       stdout_path=root / "run" / "out.log",
                                       stderr_path=root / "run" / "err.log")
            self.assertIsInstance(record.get("spawn_monotonic"), (int, float))
            self.assertGreater(record["spawn_monotonic"], 0)
            host.close_stdin(child)
            child.wait(); host.close()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_crash_evidence_marks_scene_frozen(self):
        # Frozen spec v1.0 三: the working directory is frozen/preserved before
        # physical cleanup, recorded explicitly for the superior.
        root = Path(tempfile.mkdtemp(prefix="scene-freeze "))
        try:
            host = SubprocessHost(root / "run", role="worker", generation=2)
            child, record = host.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                                       root, {"PATH": os.environ.get("PATH", "")},
                                       stdout_path=root / "run" / "out.log",
                                       stderr_path=root / "run" / "err.log")
            child.kill(); child.wait()
            host.close_stdin(child)
            evidence = host.crash_evidence(-signal.SIGKILL)
            self.assertTrue(evidence["scene_frozen"])
            self.assertEqual(evidence["cwd_preserved"], str(root))
            self.assertEqual(evidence["signal"], signal.SIGKILL)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host = SubprocessHost(self.root / "run", role="worker", generation=7)

    def tearDown(self):
        self.temp.cleanup()

    def test_ready_payload_carries_generation(self):
        payload = self.host.ready_payload()
        self.assertEqual(payload["request_id"], HANDSHAKE_REQUEST_ID)
        self.assertEqual(payload["request"]["generation"], 7)

    def test_ready_requires_matching_generation(self):
        good = {"type": "control_response", "response": {
            "request_id": HANDSHAKE_REQUEST_ID, "subtype": "success",
            "response": {"generation": 7}}}
        self.assertTrue(self.host.is_ready(good))
        stale = {"type": "control_response", "response": {
            "request_id": HANDSHAKE_REQUEST_ID, "subtype": "success",
            "response": {"generation": 6}}}
        self.assertFalse(self.host.is_ready(stale))
        wrong_id = {"type": "control_response", "response": {
            "request_id": "sapper-initialize", "subtype": "success",
            "response": {"generation": 7}}}
        self.assertFalse(self.host.is_ready(wrong_id))
        error = {"type": "control_response", "response": {
            "request_id": HANDSHAKE_REQUEST_ID, "subtype": "error",
            "response": {"generation": 7}}}
        self.assertFalse(self.host.is_ready(error))

    def test_ready_token_in_stderr_does_not_count(self):
        # is_ready only inspects structured events, never raw text lines.
        self.assertFalse(self.host.is_ready("control_response " + HANDSHAKE_REQUEST_ID))

    def test_init_ack_generation_validation(self):
        # request_id is what the real CLI echoes; generation lives there.
        self.assertEqual(init_request_id(5), "sapper-initialize:5")

        def ack(rid, subtype="success"):
            return {"type": "control_response", "response": {
                "request_id": rid, "subtype": subtype, "response": {}}}

        self.assertEqual(validate_init_ack(ack(init_request_id(5)), 5), "ok")
        self.assertEqual(validate_init_ack(ack(init_request_id(4)), 5), "stale")
        self.assertIsNone(validate_init_ack(ack("sapper-initialize"), 5))  # no gen: rejected
        self.assertIsNone(validate_init_ack(ack("other:5"), 5))
        self.assertEqual(validate_init_ack(ack(init_request_id(5), "error"), 5), "error")
        # unrelated request error must NOT be classified as an init error
        self.assertIsNone(validate_init_ack(ack("other-request", "error"), 5))
        self.assertIsNone(validate_init_ack({"type": "assistant"}, 5))


class SpawnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_spawn_uses_own_process_group_and_split_streams(self):
        host = SubprocessHost(self.root / "run", role="worker", generation=1)
        code = (
            "import os,sys,time;"
            "sys.stdout.write('{\\\"type\\\":\\\"assistant\\\"}\\n');sys.stdout.flush();"
            "sys.stderr.write('warning-line\\n');sys.stderr.flush();"
            "time.sleep(60)"
        )
        env = {"PATH": os.environ.get("PATH", ""), "TERM": "xterm",
               "TMUX": "/tmp/fake", "PYTHONDONTWRITEBYTECODE": "1"}
        child, record = host.spawn(
            [sys.executable, "-u", "-c", code], self.root / "work", env,
            stdout_path=self.root / "run" / "out.log",
            stderr_path=self.root / "run" / "err.log",
        )
        self.addCleanup(host.close_stdin, child)
        try:
            self.assertNotEqual(record["pgid"], os.getpgrp())
            self.assertGreater(record["pid"], 1)
            self.assertEqual(record["generation"], 1)
            stored = json.loads((self.root / "run" / "host.json").read_text())
            self.assertEqual(stored["pid"], child.pid)
            for _ in range(100):
                out = (self.root / "run" / "out.log").read_bytes()
                err = (self.root / "run" / "err.log").read_bytes()
                if out and err:
                    break
                import time
                time.sleep(0.05)
            self.assertIn(b'"type":"assistant"', out)
            self.assertIn(b"warning-line", err)
            self.assertNotIn(b"warning-line", out)
        finally:
            child.kill()
            child.wait()
            host.close()

    def test_crash_and_startup_evidence_have_required_fields(self):
        host = SubprocessHost(self.root / "run", role="scout", generation=3)
        child, _ = host.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                              self.root / "work", {"PATH": os.environ.get("PATH", "")},
                              stdout_path=self.root / "run" / "out.log",
                              stderr_path=self.root / "run" / "err.log")
        self.addCleanup(host.close_stdin, child)
        self.addCleanup(lambda: (child.kill() if child.poll() is None else None, child.wait()))
        crash = host.crash_evidence(-signal.SIGKILL, last_heartbeat="2026-01-01T00:00:00Z")
        for key in ("exit_code", "signal", "last_heartbeat", "cwd", "side_effect_warning",
                    "generation", "orphan_count"):
            self.assertIn(key, crash)
        self.assertEqual(crash["signal"], signal.SIGKILL)
        startup = host.startup_timeout_evidence()
        self.assertEqual(startup["side_effect_warning"], "task_never_started_retry_safe")
        self.assertEqual(startup["exit_signal"], signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
