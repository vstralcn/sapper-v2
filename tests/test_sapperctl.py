"""Local lifecycle regression using an isolated Web state."""
from contextlib import ExitStack, suppress
import gc
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
import warnings

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import sapper_models
from sapper_web import Console


@unittest.skipUnless(os.name == "posix" and shutil.which("tmux"), "requires local tmux")
class SapperCtlTests(unittest.TestCase):
    def test_start_and_restart_move_past_occupied_ports(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as sockets:
            state, root = Path(tmp) / "state", Path(tmp) / "tasks"
            for _ in range(20):
                with ExitStack() as attempt:
                    listeners = [attempt.enter_context(socket.socket()) for _ in range(4)]
                    listeners[0].bind(("127.0.0.1", 0))
                    port = listeners[0].getsockname()[1]
                    if port + 3 > 65535:
                        continue
                    try:
                        for offset, listener in enumerate(listeners[1:], 1):
                            listener.bind(("127.0.0.1", port + offset))
                    except OSError:
                        continue
                    sockets.enter_context(attempt.pop_all())
                    break
            else:
                self.fail("could not reserve four consecutive ports")
            for listener in listeners[:2]:
                listener.listen()
            for listener in listeners[2:]:
                listener.close()

            def ctl(action, *args):
                return subprocess.run([sys.executable, str(REPO / "sapperctl"), action, "--state", str(state), *args],
                                      capture_output=True, text=True, timeout=40)

            settings = state / "sapperctl.json"
            try:
                started = ctl("start", "--port", str(port), "--root", str(root))
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertIn(f"http://127.0.0.1:{port + 2}/", started.stdout)
                self.assertEqual(json.loads(settings.read_text())["port"], port + 2)
                self.assertIn("已在运行", ctl("start", "--port", str(port)).stdout)
                self.assertIn("Web 已停止", ctl("stop-web").stdout)

                with socket.socket() as blocker:
                    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    blocker.bind(("127.0.0.1", port + 2))
                    blocker.listen()
                    restarted = ctl("restart")
                    self.assertEqual(restarted.returncode, 0, restarted.stderr)
                    self.assertIn(f"http://127.0.0.1:{port + 3}/", restarted.stdout)
                    self.assertEqual(json.loads(settings.read_text())["port"], port + 3)
                    status = ctl("status").stdout
                    self.assertNotIn("未发现匹配进程", status)
                    self.assertIn(f"http://127.0.0.1:{port + 3}/", status)
                    self.assertIn("Web 已停止", ctl("stop-web").stdout)
                    self.assertIn("未发现匹配进程", ctl("status").stdout)
            finally:
                ctl("stop-web")

    def test_restart_preserves_jobs_and_full_stop_only_closes_owned_resources(self):
        with tempfile.TemporaryDirectory(prefix="sapper ctl ") as tmp:
            state, root = Path(tmp) / "state", Path(tmp) / "tasks"
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]

            def ctl(action, *args, check=True):
                return subprocess.run([sys.executable, str(REPO / "sapperctl"), action, "--state", str(state), *args],
                                      check=check, capture_output=True, text=True, timeout=40)

            try:
                started = ctl("start", "--port", str(port), "--root", str(root))
                self.assertEqual("--dangerously-skip-permissions" in started.stderr, os.geteuid() == 0)
                app = Console(root, state)
                project = app.create_project({"name": "fixture", "goal": "lifecycle"})
                run = app.start(project["id"], {"kind": "custom", "command": "sleep 120"})
                unrelated = subprocess.Popen(["sleep", "120"])
                with warnings.catch_warnings():
                    # ensure_bridge deliberately detaches the transport process;                    # dropping that Popen is expected, not a leak in this test.
                    warnings.simplefilter("ignore", ResourceWarning)
                    sapper_models.ensure_bridge(state, {"protocol": "responses", "base_url": "http://127.0.0.1:9",
                                                        "api_key": "fixture", "model": "fixture"})
                    gc.collect()
                status = ctl("status").stdout
                self.assertIn("受管无头进程：1", status)
                self.assertIn("在线模型桥：1", status)
                self.assertIn("已在运行", ctl("start").stdout)
                ctl("stop-web")
                self.assertIn("未发现匹配进程", ctl("status").stdout)
                ctl("restart")
                self.assertIn("受管无头进程：1", ctl("status").stdout)
                self.assertIn("在线模型桥：1", ctl("status").stdout)
                ctl("stop")
                self.assertIsNone(unrelated.poll(), "unrelated process must survive full stop")
                self.assertEqual(sapper_models.bridge_operation(state), [])
                self.assertIn(app.run_info(run["id"])["status"], ("stopped", "exited"))
                ctl("stop")
                with socket.socket() as other:
                    other.bind(("127.0.0.1", port))
                    other.listen()
                    self.assertNotEqual(ctl("stop", check=False).returncode, 0)
            finally:
                ctl("stop", check=False)
                if "unrelated" in locals():
                    unrelated.terminate()
                    with suppress(subprocess.TimeoutExpired):
                        unrelated.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
