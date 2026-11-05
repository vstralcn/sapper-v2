"""Local lifecycle regression using an isolated Web state, bridge and tmux socket."""
import gc
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
    def test_restart_preserves_jobs_and_full_stop_only_closes_owned_resources(self):
        with tempfile.TemporaryDirectory(prefix="sapper ctl ") as tmp:
            state, root = Path(tmp) / "state", Path(tmp) / "tasks"
            name = "ctl-" + uuid.uuid4().hex
            tmux = ["tmux", "-L", name]
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]

            def ctl(action, *args, check=True):
                return subprocess.run([sys.executable, str(REPO / "sapperctl"), action, "--state", str(state), *args],
                                      check=check, capture_output=True, text=True, timeout=40)

            try:
                ctl("start", "--port", str(port), "--root", str(root), "--tmux-socket", name)
                app = Console(root, state, name)
                project = app.create_project({"name": "fixture", "goal": "lifecycle"})
                run = app.start(project["id"], {"kind": "custom", "command": "sleep 120"})
                subprocess.run(tmux + ["new-session", "-d", "-s", "unrelated", "sleep 120"], check=True)
                with warnings.catch_warnings():
                    # ensure_bridge deliberately detaches the transport process;                    # dropping that Popen is expected, not a leak in this test.
                    warnings.simplefilter("ignore", ResourceWarning)
                    sapper_models.ensure_bridge(state, {"protocol": "responses", "base_url": "http://127.0.0.1:9",
                                                        "api_key": "fixture", "model": "fixture"})
                    gc.collect()
                status = ctl("status").stdout
                self.assertIn("受管 tmux 会话：1", status)
                self.assertIn("在线模型桥：1", status)
                self.assertIn("已在运行", ctl("start").stdout)
                ctl("stop-web")
                self.assertIn("未发现匹配进程", ctl("status").stdout)
                ctl("restart")
                self.assertIn("受管 tmux 会话：1", ctl("status").stdout)
                self.assertIn("在线模型桥：1", ctl("status").stdout)
                ctl("stop")
                self.assertEqual(subprocess.check_output(tmux + ["list-sessions", "-F", "#{session_name}"], text=True).strip(), "unrelated")
                self.assertEqual(sapper_models.bridge_operation(state), [])
                self.assertIn(app.run_info(run["id"])["status"], ("stopped", "exited"))
                ctl("stop")
                with socket.socket() as other:
                    other.bind(("127.0.0.1", port))
                    other.listen()
                    self.assertNotEqual(ctl("start", check=False).returncode, 0)
                    self.assertNotEqual(ctl("stop", check=False).returncode, 0)
            finally:
                ctl("stop", check=False)
                subprocess.run(tmux + ["kill-server"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
