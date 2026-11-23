"""Web adapter tests. Real CLI/tmux tests use an isolated socket and fake model CLI."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
from sapper_web import Console, WebError, WebServer, main, private_json


class WebStartupTests(unittest.TestCase):
    def test_startup_checks_configured_claude_version(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            binary = base / "custom claude"
            args = ["--root", str(base / "tasks"), "--state", str(base / "state"),
                    "--port", "0", "--claude-bin", str(binary)]

            def start():
                stderr = io.StringIO()
                with mock.patch.object(WebServer, "serve_forever"), \
                        redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args), 0)
                return stderr.getvalue()

            self.assertIn("找不到可执行文件", start())
            binary.write_text("#!/bin/sh\nexit 42\n")
            binary.chmod(0o700)
            self.assertIn("退出码 42", start())
            binary.write_text('#!/bin/sh\n[ "$1" = --version ] || exit 42\necho "2.1.318 (Claude Code)"\n')
            self.assertNotIn("Claude Code 检测失败", start())

    def test_only_root_startup_warns_about_claude_permission_bypass(self):
        for uid in (0, 1000):
            with self.subTest(uid=uid), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                stderr = io.StringIO()
                with mock.patch("sapper_web.os.geteuid", return_value=uid), \
                        mock.patch.object(WebServer, "serve_forever"), \
                        redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--root", str(base / "tasks"),
                                           "--state", str(base / "state"), "--port", "0"]), 0)
                self.assertEqual("--dangerously-skip-permissions" in stderr.getvalue(), uid == 0)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.app = Console(self.base / "tasks", self.base / "state")
        self.project = self.app.create_project({"name": "first", "goal": "Verify the existing agent"})
        self.pid = self.project["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_tmux_timeout_reports_operation_without_showing_command_payload(self):
        self.app.tmux_socket = "fixture-socket"
        with mock.patch("sapper_web.subprocess.run", side_effect=subprocess.TimeoutExpired("private", 2)):
            with self.assertRaisesRegex(WebError, "tmux 响应超时.*send-keys.*fixture-socket.*2") as caught:
                self.app.tmux("send-keys", "private", timeout=2)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("private", str(caught.exception))

    def test_tmux_query_failure_is_distinct_from_confirmed_absence(self):
        rid = "a" * 16
        path = self.app.run_path(rid) / "run.json"
        private_json(path, {"id": rid, "project_id": self.pid, "status": "running",
                            "session": "fixture", "created_at": sapper.now_iso()})
        failure = subprocess.CompletedProcess([], 1, "", "error connecting to /tmp/fixture (Permission denied)")
        with mock.patch.object(self.app, "tmux", return_value=failure), \
                mock.patch("sapper_web.shutil.which", return_value="/fixture/tmux"):
            with self.assertRaisesRegex(WebError, "list-panes"):
                self.app.panes()
            with self.assertRaisesRegex(WebError, "has-session"):
                self.app.runs()
        self.assertEqual(self.app.run_info(rid)["status"], "running")
        no_session = subprocess.CompletedProcess([], 1, "", "can't find session: fixture")
        with mock.patch.object(self.app, "tmux", return_value=no_session):
            self.assertEqual(self.app.runs()[0]["status"], "interrupted")
        no_server = subprocess.CompletedProcess([], 1, "", "no server running on /tmp/fixture")
        with mock.patch.object(self.app, "tmux", return_value=no_server), \
                mock.patch("sapper_web.shutil.which", return_value="/fixture/tmux"):
            self.assertEqual(self.app.panes(), [])

    def test_create_project_uses_workspace_name_as_title(self):
        named = self.app.create_project({"title": "登录绕过分析", "goal": "Find the bypass"})
        self.assertEqual(named["name"], "登录绕过分析")
        self.assertRegex(named["directory"], r"^workspace-[0-9a-f]{8}$")
        explicit = self.app.create_project({"title": "Web 审计", "name": "web-audit", "goal": "Audit"})
        self.assertEqual((explicit["name"], explicit["directory"]), ("Web 审计", "web-audit"))
        derived = self.app.create_project({"title": "Reverse Lab", "goal": "Reverse"})
        self.assertEqual(derived["directory"], "Reverse-Lab")
        self.assertEqual((self.project["name"], self.project["directory"]), ("first", "first"))
        reloaded = Console(self.app.root, self.app.state).project(named["id"])
        self.assertEqual(reloaded["name"], "登录绕过分析")
        with self.assertRaises(WebError):
            self.app.create_project({"goal": "no name"})

    def test_delete_workspace_hides_or_removes_files_and_unbinds_external(self):
        from sapper_workspace import WorkspaceError
        temp = self.app.create_project({"title": "临时", "name": "temp", "goal": "remove me"})
        with self.app.config_lock():
            cfg = self.app.config()
            self.app.set_workspace_models(cfg, temp["id"], {"main": ""})
            private_json(self.app.config_path, cfg)
        result = self.app.workspaces.delete_workspace(temp["id"], {})
        self.assertFalse(result["files_deleted"])
        self.assertTrue(Path(temp["path"]).is_dir())
        self.assertNotIn(temp["id"], [p["id"] for p in self.app.projects()])
        self.assertNotIn(temp["id"], self.app.config().get("workspace_role_profiles", {}))
        rebound = self.app.workspaces.bind({"path": temp["path"]})
        self.assertEqual(rebound["id"], temp["id"])
        self.assertIn(temp["id"], [p["id"] for p in self.app.projects()])
        self.assertNotIn(temp["path"], self.app.config().get("hidden_workspaces", []))

        with self.assertRaises(WorkspaceError):
            self.app.workspaces.delete_workspace(temp["id"], {"delete_files": True, "confirm_name": "wrong"})
        self.assertTrue(Path(temp["path"]).is_dir())
        self.app.workspaces.delete_workspace(temp["id"], {"delete_files": True, "confirm_name": "临时"})
        self.assertFalse(Path(temp["path"]).exists())
        self.assertNotIn(temp["id"], [p["id"] for p in self.app.projects()])

        outside = self.base / "outside"
        outside.mkdir()
        bound = self.app.workspaces.bind({"path": str(outside)})
        listed = next(p for p in self.app.projects() if p["id"] == bound["id"])
        self.assertFalse(listed["inside_root"])
        with self.assertRaises(WorkspaceError):
            self.app.workspaces.delete_workspace(bound["id"], {"delete_files": True, "confirm_name": listed["name"]})
        self.app.workspaces.delete_workspace(bound["id"], {})
        self.assertTrue(outside.is_dir())
        self.assertNotIn(bound["id"], [p["id"] for p in self.app.projects()])
        self.assertIn(self.pid, [p["id"] for p in self.app.projects()])

    def test_graph_writes_use_original_schema_and_confirmation(self):
        fact = self.app.graph_action(self.pid, "fact", {"description": "confirmed input", "kind": "note"})
        intent = self.app.graph_action(self.pid, "intent", {"from": [fact["id"]], "description": "check result", "requires_confirmation": True})
        self.assertFalse(sapper.is_ready(intent))
        confirmed = self.app.graph_action(self.pid, "confirm", {"id": intent["id"]})
        self.assertTrue(sapper.is_ready(confirmed))
        store = self.app.store(self.pid)
        self.assertEqual(sapper.validate_graph(store), [])
        self.assertTrue((store.facts_dir / f"{fact['id']}.json").is_file())
        self.app.graph_action(self.pid, "doc", {"id": fact["id"], "doc": "evidence", "expected_doc": None})
        with self.assertRaises(sapper.FGError):
            self.app.graph_action(self.pid, "doc", {"id": fact["id"], "doc": "stale", "expected_doc": None})

    def test_graph_actions_record_evidence_without_executing_commands(self):
        root = Path(self.app.project(self.pid)["path"])
        (root / "response.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
        digest = hashlib.sha256((root / "response.txt").read_bytes()).hexdigest()
        fact = self.app.graph_action(self.pid, "fact", {
            "description": "endpoint answered 200", "kind": "service",
            "evidence_source": "authorized local lab",
            "repro_commands": ["curl -i http://127.0.0.1/", "   "],
            "artifacts": ["response.txt"],
        })
        evidence = fact["evidence"]
        self.assertEqual(evidence["source"], "authorized local lab")
        self.assertEqual([c["command"] for c in evidence["reproduction"]], ["curl -i http://127.0.0.1/"])
        self.assertEqual(evidence["reproduction"][0]["cwd"], str(root.resolve()))
        artifact = evidence["artifacts"][0]
        self.assertEqual(artifact["sha256"], digest)
        self.assertEqual(artifact["path"], "artifacts/" + digest)
        self.assertTrue((self.app.store(self.pid).root / artifact["path"]).is_file())
        self.assertEqual(sapper.validate_graph(self.app.store(self.pid)), [])
        self.assertIsNone(self.app.graph_action(self.pid, "fact", {"description": "no provenance"}).get("evidence"))
        intent = self.app.graph_action(self.pid, "intent", {"from": [fact["id"]], "description": "confirm 200"})
        self.app.graph_action(self.pid, "claim", {"id": intent["id"], "worker": "web-worker"})
        done = self.app.graph_action(self.pid, "done", {
            "id": intent["id"], "description": "replayed the request", "worker": "web-worker",
            "repro_commands": ["curl -i http://127.0.0.1/"],
        })
        self.assertEqual(done["concluded_intent"], intent["id"])
        self.assertEqual(done["evidence"]["reproduction"][0]["command"], "curl -i http://127.0.0.1/")
        self.assertEqual(sapper.validate_graph(self.app.store(self.pid)), [])

    def test_graph_action_evidence_rejects_escapes_and_bad_payloads(self):
        root = Path(self.app.project(self.pid)["path"])
        outside = self.base / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(WebError, "inside current project"):
            self.app.graph_action(self.pid, "fact", {"description": "x", "artifacts": [str(outside)]})
        (root / "escape.txt").symlink_to(outside)
        with self.assertRaisesRegex(WebError, "symlinks"):
            self.app.graph_action(self.pid, "fact", {"description": "x", "artifacts": ["escape.txt"]})
        with self.assertRaisesRegex(WebError, "does not exist"):
            self.app.graph_action(self.pid, "fact", {"description": "x", "artifacts": ["missing.txt"]})
        with self.assertRaisesRegex(WebError, "文本列表"):
            self.app.graph_action(self.pid, "fact", {"description": "x", "repro_commands": "curl -i http://127.0.0.1/"})
        with self.assertRaisesRegex(WebError, "证据来源必须为文本"):
            self.app.graph_action(self.pid, "fact", {"description": "x", "evidence_source": ["lab"]})
        store = self.app.store(self.pid)
        self.assertEqual([f["id"] for f in store.list_facts()], ["goal"])
        self.assertEqual(sapper.validate_graph(store), [])

    def test_discovery_preserves_existing_graph_and_skips_shared_alias(self):
        path = self.app.root / "existing"
        original = sapper.Store(path / ".sapper")
        original.init("original", "preserve me")
        before = original.project_file.read_bytes()
        alias = self.app.root / "existing-w2"
        alias.mkdir()
        (alias / ".sapper").symlink_to(original.root, target_is_directory=True)
        projects = self.app.projects()
        self.assertEqual(len(projects), 2)
        self.assertEqual(original.project_file.read_bytes(), before)
        with self.assertRaises(WebError):
            self.app.create_project({"name": "existing", "goal": "overwrite"})

    def test_files_reject_traversal_credentials_and_escaping_symlinks(self):
        project = Path(self.project["path"])
        (project / "escape").symlink_to(self.base / "state", target_is_directory=True)
        for name in ("../state/token", "/etc/passwd", "escape/token", ".env", ".claude/settings.json"):
            with self.subTest(path=name), self.assertRaises(WebError):
                self.app.file(self.pid, name)
        result = self.app.upload(self.pid, "../线索.txt", "attachment".encode())
        self.assertEqual(self.app.file(self.pid, result["path"]).read_text(), "attachment")
        self.assertIn(result["path"], self.app.graph(self.pid)["hints"][0]["content"])

    def test_multiple_profiles_do_not_return_keys_or_modify_global_environment(self):
        before = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        one = self.app.save_profile({"name": "one", "model": "m1", "api_key": "synthetic-secret-one"})
        two = self.app.save_profile({"name": "two", "model": "m2", "api_key": "synthetic-secret-two"})
        self.assertTrue(one["has_key"])
        self.assertNotIn("api_key", one)
        self.assertNotIn("synthetic-secret", json.dumps(self.app.profiles()))
        self.assertEqual(os.environ.get("ANTHROPIC_AUTH_TOKEN"), before)
        self.app.save_profile({"id": one["id"], "name": "updated", "model": "m3", "api_key": ""})
        stored = next(p for p in self.app.config()["profiles"] if p["id"] == one["id"])
        self.assertEqual(stored["api_key"], "synthetic-secret-one")
        self.assertEqual(stat.S_IMODE(self.app.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.app.auth.path.stat().st_mode), 0o600)
        self.app.delete_profile(two["id"])
        self.assertEqual(len(self.app.profiles()), 1)

    def test_live_budget_updates_are_serialized_bounded_and_do_not_rewrite_runner_state(self):
        rid = "a" * 16
        directory = self.app.run_path(rid)
        info = {"id": rid, "status": "running", "timeout_seconds": 60,
                "supervisor_version": 1, "started_epoch": 100, "execution_mode": "print"}
        private_json(directory / "run.json", info)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.app.extend(rid, {"minutes": 1}), range(4)))
        current = self.app.run_info(rid)
        self.assertEqual(current["timeout_seconds"], 300)
        self.assertEqual(current["deadline_epoch"], 400)
        self.assertEqual(json.loads((directory / "run.json").read_text()), info)
        for minutes in (True, None, 0, 1440):
            with self.assertRaises(WebError):
                self.app.extend(rid, {"minutes": minutes})
        private_json(directory / "run.json", {**info, "status": "stopping"})
        with self.assertRaises(WebError) as error:
            self.app.extend(rid, {"minutes": 1})
        self.assertEqual(error.exception.status, 409)
        private_json(directory / "run.json", {**info, "supervisor_version": None})
        with self.assertRaises(WebError):
            self.app.extend(rid, {"minutes": 1})

    def test_readable_log_chunks_preserve_unicode_and_activity_uses_existing_redaction(self):
        rid = "a" * 16
        directory = self.app.run_path(rid)
        private_json(directory / "run.json", {"id": rid, "status": "running"})
        self.app.save_profile({"name": "test", "model": "fixture", "api_key": "synthetic-activity-secret"})
        private_json(directory / "activity.json", {"latest_message": "synthetic-activity-secret"})
        self.assertNotIn("synthetic-activity-secret", json.dumps(self.app.run_info(rid)))
        expected = "a" * (128 * 1024 - 1) + "中文活动\n"
        (directory / "activity.log").write_text(expected)
        first = self.app.log(rid)
        second = self.app.log(rid, first["offset"])
        self.assertEqual(first["text"] + second["text"], expected)
        self.assertEqual(second["offset"], len(expected.encode()))

    def test_run_list_collects_credentials_once_and_refreshes_them_next_batch(self):
        self.app.save_profile({"name": "test", "model": "fixture", "api_key": "fixture-profile-secret"})
        private_json(self.app.state / "agents" / ("b" * 16 + ".json"),
                     {"mcp_env_refs": ["SAPPER_TEST_MCP_SECRET"], "deleted_at": "historical"})
        private_json(self.app.state / "model-bridges" / "fixture" / "ready.json",
                     {"token": "fixture-bridge-secret"})
        for i in range(4):
            directory = self.app.run_path(f"{i:016x}")
            private_json(directory / "run.json", {"id": f"{i:016x}", "project_id": self.pid,
                         "status": "exited", "created_at": str(i), "command": "fixture-profile-secret"})
            private_json(directory / "activity.json", {"latest_message":
                         "fixture-profile-secret fixture-bridge-secret fixture-mcp-old fixture-mcp-new"})
        extensions = self.app.workspaces.extensions
        for secret in ("fixture-mcp-old", "fixture-mcp-new"):
            with mock.patch.dict(os.environ, {"SAPPER_TEST_MCP_SECRET": secret}), \
                    mock.patch.object(extensions, "credential_values", wraps=extensions.credential_values) as collect:
                records = self.app.runs()
            collect.assert_called_once()
            self.assertEqual(len(records), 4)
            result = json.dumps(records)
            for hidden in ("fixture-profile-secret", "fixture-bridge-secret", secret):
                self.assertNotIn(hidden, result)

    def test_workspace_status_and_overview_share_run_queries(self):
        aid, rid = "a" * 16, "b" * 16
        private_json(self.app.run_path(rid) / "run.json", {"id": rid, "project_id": self.pid,
                     "session": "fixture", "status": "exited", "created_at": "2026-01-01", "exit_code": 0})
        self.app.workspaces.save_agent({"id": aid, "workspace_id": self.pid, "role": "worker",
                                       "run_id": rid, "created_at": "2026-01-01"})
        private_json(self.app.state / "services" / "fixture.json",
                     {"id": "fixture", "workspace_id": self.pid, "run_id": rid})
        with mock.patch.object(self.app, "runs", wraps=self.app.runs) as runs, \
                mock.patch.object(self.app, "panes", return_value=[]) as panes:
            status = self.app.workspace_status(self.pid)
            self.assertEqual(status["agents"][0]["status"], "exited")
            self.assertEqual(status["agents"][0]["exit_code"], 0)
            self.assertEqual(status["services"][0]["status"], "exited")
            runs.assert_called_once_with(reconcile=False)
            panes.assert_called_once_with()
        with mock.patch.object(self.app, "runs", wraps=self.app.runs) as runs, \
                mock.patch.object(self.app, "panes", return_value=[]), \
                mock.patch.object(self.app.workspaces, "capabilities", return_value={"available": False}):
            overview = self.app.overview()
            self.assertEqual(overview["agents"][0]["id"], aid)
            self.assertEqual(overview["recent_runs"][0]["id"], rid)
            runs.assert_called_once_with(reconcile=False)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.app = Console(base / "tasks", base / "state")
        self.app.auth.initialize("admin", "fixture web password 123")
        self.server = WebServer(("127.0.0.1", 0), self.app, prefix="/sapper")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        combined = {"Cookie": self.cookie, "X-Sapper-CSRF": self.csrf, **(headers or {})}
        body = None
        if isinstance(data, bytes):
            body = data
            combined["Content-Type"] = "application/octet-stream"
        elif data is not None:
            body = json.dumps(data)
            combined["Content-Type"] = "application/json"
        conn.request(method, "/sapper" + path, body=body, headers=combined)
        response = conn.getresponse()
        value = response.read()
        result = (response.status, dict(response.getheaders()), value)
        conn.close()
        return result

    def login(self):
        status, headers, body = self.request("/api/session", "POST", {"username": "admin", "password": "fixture web password 123"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(body)["csrf"]

    def test_staged_uploads_follow_only_the_creating_workspace(self):
        self.login()
        endpoint = "/api/staged-attachments?name=" + urllib.parse.quote("../资料.bin")
        tokens = []
        for content in (b"first", b"second\x00"):
            status, _, body = self.request(endpoint, "POST", content)
            self.assertEqual(status, 201, body)
            tokens.append(json.loads(body)["id"])
        self.assertEqual(self.app.projects(), [])
        payload = {"name": "draft", "goal": "Review files", "attachment_ids": tokens}
        original = self.cookie, self.csrf
        self.login()
        self.assertEqual(self.request("/api/projects", "POST", payload)[0], 404)
        self.cookie, self.csrf = original
        self.assertEqual(self.request("/api/projects", "POST", {**payload, "goal": ""})[0], 400)
        status, _, body = self.request("/api/projects", "POST", payload)
        self.assertEqual(status, 201, body)
        pid = json.loads(body)["id"]
        files = self.app.files(pid, "attachments")
        self.assertEqual(len(files), 2)
        self.assertEqual({self.app.file(pid, f["path"]).read_bytes() for f in files}, {b"first", b"second\x00"})
        self.assertTrue(all(f["name"].endswith("资料.bin") for f in files))
        self.assertTrue(all(stat.S_IMODE(self.app.file(pid, f["path"]).stat().st_mode) == 0o600 for f in files))
        self.assertEqual(self.request("/api/projects", "POST", {**payload, "name": "other"})[0], 404)
        self.assertFalse((self.app.root / "other").exists())

    def test_discarded_attachment_cannot_be_claimed(self):
        self.login()
        status, _, body = self.request("/api/staged-attachments?name=note.txt", "POST", b"note")
        self.assertEqual(status, 201, body)
        token = json.loads(body)["id"]
        self.assertEqual(self.request("/api/staged-attachments/" + token, "DELETE")[0], 200)
        status, _, _ = self.request("/api/projects", "POST", {
            "name": "draft", "goal": "Review", "attachment_ids": [token],
        })
        self.assertEqual(status, 404)
        self.assertEqual(self.app.projects(), [])

    def test_workspace_and_fact_status_agree_between_cli_and_web(self):
        self.login()
        project = self.app.create_project({"name": "parity", "goal": "Review a document"})
        pid, aid, rid = project["id"], "a" * 16, "b" * 16
        run_path = self.app.run_path(rid) / "run.json"
        private_json(run_path, {"id": rid, "project_id": pid, "status": "running",
                                "created_at": sapper.now_iso(), "session": "fixture-missing-pane"})
        self.app.workspaces.save_agent({"id": aid, "workspace_id": pid, "role": "worker",
                                       "run_id": rid, "created_at": sapper.now_iso()})
        self.app.tmux_socket = "fixture-" + uuid.uuid4().hex
        repo = Path(__file__).resolve().parents[1]
        cli = [sys.executable, str(repo / "lib/sapper.py"), "--store", str(Path(project["path"]) / ".sapper")]
        result = subprocess.check_output([*cli, "fact", "Reviewed the draft", "--evidence-source", "Review notes"], text=True)
        fact_id = result.strip()
        _, _, body = self.request(f"/api/projects/{pid}/graph")
        fact = next(f for f in json.loads(body)["facts"] if f["id"] == fact_id)
        self.assertEqual(fact["evidence"]["source"], "Review notes")
        _, _, body = self.request(f"/api/projects/{pid}/fact", "POST", {"description": "Approved the outline"})
        web_fact = json.loads(body)
        cli_graph = json.loads(subprocess.check_output([*cli, "graph", "--format", "json"], text=True))
        self.assertIn(web_fact, cli_graph["facts"])

        _, _, body = self.request(f"/api/workspaces/{pid}/agents")
        web = json.loads(body)
        cli_view = json.loads(subprocess.check_output([sys.executable, str(repo / "sapperctl"), "workspace-status",
            "--state", str(self.app.state), "--root", str(self.app.root), "--tmux-socket", self.app.tmux_socket,
            "--workspace", project["path"], "--json"], text=True))
        self.assertEqual(cli_view["project"]["id"], pid)
        self.assertEqual(cli_view["summary"], web["summary"])
        self.assertEqual(cli_view["agents"][0]["observation"], web["agents"][0]["observation"])
        self.assertEqual(cli_view["agents"][0]["observation"]["presence"], "unknown")
        self.assertEqual(json.loads(run_path.read_text())["status"], "running")
        self.assertFalse((run_path.parent / "control.lock").exists())

    def test_password_login_rejects_legacy_token_and_sets_private_cookie(self):
        (self.app.state / "token").write_text("legacy-fixture-token-that-is-no-longer-accepted")
        self.assertEqual(self.request("/api/auth")[0], 200)
        self.assertEqual(self.request("/api/session", "POST", {"token": (self.app.state / "token").read_text()})[0], 401)
        self.server.secure_cookie = True
        status, headers, body = self.request("/api/session", "POST", {"username": "admin", "password": "fixture web password 123"})
        self.assertEqual(status, 200)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/sapper", "Secure"):
            self.assertIn(attribute, headers["Set-Cookie"])
        self.assertEqual(json.loads(body)["username"], "admin")
        self.assertNotIn(b"password", body)

    def test_password_change_requires_session_csrf_and_current_password(self):
        data = {"current_password": "fixture web password 123", "new_password": "abc123"}
        self.assertEqual(self.request("/api/account/password", "POST", data)[0], 401)
        self.login()
        cookie = self.cookie
        self.assertEqual(self.request("/api/account/password", "POST", data, {"X-Sapper-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.request("/api/account/password", "POST", {**data, "current_password": "wrong"})[0], 400)
        self.assertEqual(self.request("/api/account/password", "POST", {**data, "new_password": "short"})[0], 400)
        status, headers, _ = self.request("/api/account/password", "POST", data)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/session", headers={"Cookie": cookie})[0], 401)
        self.assertEqual(self.request("/api/session", "POST", {"username": "admin", "password": data["new_password"]})[0], 200)

    def test_existing_cookie_remains_valid_after_console_reload(self):
        self.login()
        self.server.console = Console(self.app.root, self.app.state)
        status, _, body = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf"], self.csrf)

    def test_role_models_require_auth_csrf_and_persist_under_subpath(self):
        self.assertEqual(self.request("/api/role-models")[0], 401)
        self.assertEqual(self.request("/api/role-models", "POST", {})[0], 401)
        self.login()
        profile = self.app.save_profile({"name": "routing fixture", "model": "test-model", "api_key": "private-routing-key"})
        data = {"assignments": {"commander": profile["id"], "scout": ""}, "expected_revision": 0}
        self.assertEqual(self.request("/api/role-models", "POST", data, {"X-Sapper-CSRF": ""})[0], 403)
        status, _, body = self.request("/api/role-models", "POST", data)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["assignments"], data["assignments"])
        self.assertNotIn(b"private-routing-key", body)
        self.assertEqual(self.request("/api/role-models", "POST", data)[0], 409)
        self.assertEqual(self.request("/api/profiles/" + profile["id"], "DELETE")[0], 409)
        self.assertEqual(self.request("/models.js")[0], 200)
        self.assertEqual(self.request("/models.css")[0], 200)

    def test_auth_csrf_origin_and_subpath(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/api/projects")[0], 401)

        self.login()
        self.assertEqual(self.request("/api/projects")[0], 200)
        self.assertEqual(self.request("/api/projects", "POST", {"name": "x", "goal": "x"}, {"X-Sapper-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.request("/api/projects", "POST", {"name": "x", "goal": "x"}, {"Origin": "https://untrusted.invalid"})[0], 403)
        status, _, body = self.request("/api/projects", "POST", {"name": "x", "goal": "new task"})
        self.assertEqual(status, 201)
        pid = json.loads(body)["id"]
        self.assertEqual(self.request(f"/api/projects/{pid}/graph")[0], 200)
        self.assertEqual(self.request("/api/health", headers={"Host": "untrusted.invalid"})[0], 403)
        self.assertEqual(self.request("/api/logout", "POST", {})[0], 200)
        self.assertEqual(self.request("/api/projects")[0], 401)

    def test_authenticated_fact_action_records_provenance_and_rejects_escapes(self):
        self.login()
        p = self.app.create_project({"name": "evidence", "goal": "web evidence fixture"})
        root = Path(p["path"])
        (root / "capture.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
        body = {"description": "captured 200", "kind": "service", "evidence_source": "local lab",
                "repro_commands": ["curl -i http://127.0.0.1/"], "artifacts": ["capture.txt"]}
        status, _, raw = self.request(f"/api/projects/{p['id']}/fact", "POST", body)
        self.assertEqual(status, 200)
        fact = json.loads(raw)
        digest = hashlib.sha256((root / "capture.txt").read_bytes()).hexdigest()
        self.assertEqual(fact["evidence"]["artifacts"][0]["path"], "artifacts/" + digest)
        self.assertEqual(fact["evidence"]["reproduction"][0]["command"], "curl -i http://127.0.0.1/")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        code, _, raw = self.request(f"/api/projects/{p['id']}/fact", "POST",
                                    {"description": "escape", "artifacts": [str(outside)]})
        self.assertEqual(code, 400)
        self.assertIn("inside current project", json.loads(raw)["error"])
        self.assertEqual(self.request(f"/api/projects/{p['id']}/fact", "POST",
                                      {"description": "bad", "repro_commands": "curl -i http://127.0.0.1/"})[0], 400)
        graph = json.loads(self.request(f"/api/projects/{p['id']}/graph")[2])
        self.assertEqual([f["id"] for f in graph["facts"] if f["id"] != "goal"], [fact["id"]])

    def test_web_picker_binds_external_directory_without_startup_options(self):
        question = self.app.root.parent / "external 题目"
        question.mkdir()
        (question / "evidence.txt").write_text("original evidence")
        bind = "/api/workspaces/bind"
        browse = "/api/directories?" + urllib.parse.urlencode({"path": str(question.parent)})
        self.assertEqual(self.request(browse)[0], 401)
        self.assertEqual(self.request(bind, "POST", {"path": str(question)})[0], 401)
        self.login()
        status, _, body = self.request(browse)
        self.assertEqual(status, 200)
        self.assertIn(str(question), [d["path"] for d in json.loads(body)["directories"]])
        self.assertEqual(self.request(bind, "POST", {"path": str(question)}, {"X-Sapper-CSRF": "bad"})[0], 403)
        status, _, body = self.request(bind, "POST", {"path": str(question)})
        self.assertEqual(status, 201)
        pid = json.loads(body)["id"]
        self.assertEqual(list(question.iterdir()), [question / "evidence.txt"])
        status, _, body = self.request(f"/api/projects/{pid}/file?path=evidence.txt")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "original evidence")
        self.assertEqual(self.request(bind, "POST", {"path": str(self.app.state)})[0], 403)

    def test_skill_metadata_reads_manifest_without_saving(self):
        skill = self.app.root.parent / "different-folder-name"
        skill.mkdir()
        manifest = skill / "SKILL.md"
        manifest.write_text(
            '---\nname: "declared-name" # "comment"\ndescription: >-\n'
            '  第一行\n\n  第二行\n---\n正文\n', encoding="utf-8")
        endpoint = "/api/extensions/skill-metadata?"

        def preview(path):
            return self.request(endpoint + urllib.parse.urlencode({"path": str(path)}))

        self.assertEqual(preview(skill)[0], 401)
        self.login()
        status, _, body = preview(skill)
        self.assertEqual((status, json.loads(body)), (200, {
            "name": "declared-name", "description": "第一行 第二行"}))
        self.assertEqual(self.app.workspaces.extensions.read()["revision"], 0)
        save = {"action": "save", "expected_revision": 0, "kind": "skills",
                "name": skill.name, "path": str(skill)}
        self.assertEqual(self.request("/api/extensions", "POST", save)[0], 400)
        save["name"] = "declared-name"
        self.assertEqual(self.request("/api/extensions", "POST", save)[0], 200)

    def test_skill_metadata_rejects_missing_or_private_manifest(self):
        skill = self.app.root.parent / "empty-skill"
        skill.mkdir()
        private_manifest = self.app.state / "SKILL.md"
        private_manifest.write_text("---\nname: private\ndescription: private\n---\n")
        self.login()
        endpoint = "/api/extensions/skill-metadata?"
        browse = endpoint + urllib.parse.urlencode({"path": str(skill)})
        self.assertEqual(self.request(browse)[0], 400)
        self.assertEqual(self.request(endpoint + urllib.parse.urlencode({"path": str(self.app.state)}))[0], 403)
        (skill / "SKILL.md").symlink_to(private_manifest)
        self.assertEqual(self.request(browse)[0], 400)

    def test_workspace_submission_settings_require_auth_and_csrf_and_persist(self):
        project = self.app.create_project({"name": "submission-policy", "goal": "fixture"})
        other = self.app.create_project({"name": "other-policy", "goal": "fixture"})
        path = f"/api/workspaces/{project['id']}/settings"
        self.assertEqual(self.request(path, "POST", {"auto_submit_flags": True})[0], 401)
        self.login()
        self.assertEqual(self.request(path, "POST", {"auto_submit_flags": True}, {"X-Sapper-CSRF": "bad"})[0], 403)
        self.assertFalse(self.app.workspaces.inspect(project["id"])["auto_submit_flags"])
        for value in ("true", 1, None):
            self.assertEqual(self.request(path, "POST", {"auto_submit_flags": value})[0], 400)
        status, _, body = self.request(path, "POST", {"auto_submit_flags": True})
        self.assertEqual((status, json.loads(body)), (200, {"auto_submit_flags": True}))
        reloaded = Console(self.app.root, self.app.state)
        self.assertTrue(reloaded.workspaces.inspect(project["id"])["auto_submit_flags"])
        self.assertFalse(reloaded.workspaces.inspect(other["id"])["auto_submit_flags"])
        prompt = reloaded.workspaces.role_info(project["id"], "worker")["prompt"]
        self.assertIn("ON: the user authorizes proactive", prompt)
        board = reloaded.workspaces.strategy_board([project["id"], other["id"]])
        self.assertEqual([w["auto_submit_flags"] for w in board["workspaces"]], [True, False])
        self.assertEqual(self.request(path, "POST", {"auto_submit_flags": False})[0], 200)
        self.assertIn("OFF: record flags", reloaded.workspaces.role_info(project["id"], "worker")["prompt"])

    def test_delete_role_requires_auth_and_csrf_and_removes_it_from_lists(self):
        project = self.app.create_project({"name": "delete-role", "goal": "test deletion"})
        aid = "a" * 16
        self.app.workspaces.save_agent({"id": aid, "workspace_id": project["id"],
            "role": "worker", "created_at": sapper.now_iso(), "desired_state": "running"})
        path = f"/api/agents/{aid}"
        self.assertEqual(self.request(path, "DELETE")[0], 401)
        self.login()
        self.assertEqual(self.request(path, "DELETE", headers={"X-Sapper-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.request(path, "DELETE", headers={"Origin": "https://untrusted.invalid"})[0], 403)
        self.assertEqual(self.app.workspaces.agent(aid)["desired_state"], "running")
        status, _, body = self.request(path, "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["deleted_agent"], aid)
        status, _, body = self.request(f"/api/workspaces/{project['id']}/agents")
        self.assertEqual(status, 200)
        self.assertFalse(any(a["id"] == aid for a in json.loads(body)["agents"]))
        self.assertEqual(self.request(path + "/resume", "POST", {})[0], 404)
        self.assertEqual(self.request(path, "DELETE")[0], 200)
        self.assertEqual(self.request("/api/agents/" + "b" * 16, "DELETE")[0], 404)

    def test_delete_workspace_over_http_stops_roles_and_respects_csrf(self):
        project = self.app.create_project({"title": "删除我", "name": "delete-ws", "goal": "test workspace deletion"})
        aid = "c" * 16
        self.app.workspaces.save_agent({"id": aid, "workspace_id": project["id"],
            "role": "worker", "created_at": sapper.now_iso(), "desired_state": "running"})
        path = f"/api/workspaces/{project['id']}/delete"
        self.assertEqual(self.request(path, "POST", {})[0], 401)
        self.login()
        self.assertEqual(self.request(path, "POST", {}, headers={"X-Sapper-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.request(path, "POST", {"delete_files": True, "confirm_name": "wrong"})[0], 400)
        status, _, body = self.request(path, "POST", {"delete_files": True, "confirm_name": "删除我"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted_agents"], [aid])
        self.assertFalse(Path(project["path"]).exists())
        self.assertTrue(self.app.workspaces.agent(aid, include_deleted=True)["deleted_at"])
        status, _, body = self.request("/api/projects")
        self.assertFalse(any(p["id"] == project["id"] for p in json.loads(body)))

    def test_run_extension_requires_auth_csrf_and_active_budget(self):
        rid = "a" * 16
        path = f"/api/runs/{rid}/extend"
        self.assertEqual(self.request(path, "POST", {"minutes": 1})[0], 401)
        self.login()
        self.assertEqual(self.request(path, "POST", {"minutes": 1}, {"X-Sapper-CSRF": "bad"})[0], 403)
        private_json(self.app.run_path(rid) / "run.json", {"id": rid, "status": "running",
                     "supervisor_version": 1, "execution_mode": "print", "timeout_seconds": 60})
        status, _, body = self.request(path, "POST", {"minutes": 2})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["timeout_seconds"], 180)
        self.assertEqual(self.request(path, "POST", {"minutes": -1})[0], 400)
        (self.app.run_path(rid) / "stop_requested").touch()
        self.assertEqual(self.request(path, "POST", {"minutes": 1})[0], 409)

    def test_commander_control_requires_auth_csrf_and_valid_role(self):
        project = self.app.create_project({"name": "commander-control", "goal": "verify pause"})
        aid = "e" * 16
        record = {"id": aid, "role": "commander", "execution_mode": "tui",
                  "workspace_id": project["id"], "created_at": sapper.now_iso()}
        self.app.workspaces.save_agent(record)
        path = f"/api/agents/{aid}/control"
        data = {"control": "paused", "reason": "human review"}
        self.assertEqual(self.request(path, "POST", data)[0], 401)
        self.login()
        self.assertEqual(self.request(path, "POST", data, {"X-Sapper-CSRF": "bad"})[0], 403)
        code, _, body = self.request(path, "POST", data)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["control"], "paused")
        self.assertEqual(self.request(path, "POST", {"control": []})[0], 400)
        self.app.workspaces.save_agent({**record, "role": "worker"})
        self.assertEqual(self.request(path, "POST", data)[0], 400)

    def test_model_discovery_and_probe_require_auth_and_csrf(self):
        self.assertEqual(self.request("/api/profiles/models", "POST", {})[0], 401)
        self.login()
        draft = {"name": "draft", "protocol": "responses", "model": "fixture-model"}
        self.assertEqual(self.request("/api/profiles/models", "POST", draft, {"X-Sapper-CSRF": "bad"})[0], 403)
        with mock.patch("sapper_models.list_models", return_value={"models": [{"id": "model-one", "name": "One"}], "truncated": False}):
            code, _, raw = self.request("/api/profiles/models", "POST", draft)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["models"][0]["id"], "model-one")
        with mock.patch("sapper_models.test_connection", return_value={"ok": True, "reply": "OK"}):
            self.assertEqual(self.request("/api/profiles/test", "POST", draft)[0], 200)
        self.assertEqual(self.request("/api/profiles", "POST", {**draft, "protocol": "wrong"})[0], 400)

    def test_global_context_and_overview_require_auth_and_preserve_summary(self):
        self.assertEqual(self.request("/api/context")[0], 401)
        self.assertEqual(self.request("/api/overview")[0], 401)
        self.login()
        self.app.workspaces._capabilities = {"available": False}
        p = self.app.create_project({"name": "summary", "goal": "overview fixture"})
        self.app.graph_action(p["id"], "fact", {"description": "summary evidence"})
        data = {"expected_revision": 0, "content": "x64 resource", "enabled": True, "prompt_enabled": True, "hook_enabled": True}
        self.assertEqual(self.request("/api/context", "POST", data, {"X-Sapper-CSRF": "bad"})[0], 403)
        status, _, raw = self.request("/api/context", "POST", data)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["revision"], 1)
        status, _, raw = self.request("/api/overview")
        self.assertEqual(status, 200)
        overview = json.loads(raw)
        self.assertEqual(overview["summary"]["workspaces"], 1)
        self.assertEqual(overview["summary"]["facts"], 1)
        self.assertEqual(overview["summary"]["active_agents"], 0)
        self.assertEqual(overview["context"]["revision"], 1)
        self.assertNotIn("content", overview["context"])
        self.assertTrue(overview["environment"]["architecture"])

    def test_native_catalog_preview_and_config_remain_authenticated_and_scoped(self):
        self.assertEqual(self.request("/api/native/catalog")[0], 401)
        self.login()
        p = self.app.create_project({"name": "native-api", "goal": "native HTTP fixture"})
        pid = p["id"]
        status, _, raw = self.request("/api/native/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)["commands"]), 32)
        body = {"operation": "fact", "values": {"description": "HTTP fixture"}}
        self.assertEqual(self.request(f"/api/native/{pid}/preview", "POST", body, {"X-Sapper-CSRF": "bad"})[0], 403)
        status, _, raw = self.request(f"/api/native/{pid}/preview", "POST", body)
        self.assertEqual(status, 200)
        preview = json.loads(raw)
        body.update(preview_token="stale-token")
        self.assertEqual(self.request(f"/api/native/{pid}/execute", "POST", body)[0], 409)
        self.assertEqual(self.request(f"/api/native/{pid}/config?path=.sapper/project.json")[0], 403)
        self.assertEqual(self.request(f"/api/native/{pid}/preview", "POST", {"operation": "bench"})[0], 400)
        self.assertEqual(preview["operation"], "fact")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class TmuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.app = Console(self.base / "tasks", self.base / "state", "sapper-web-test-" + uuid.uuid4().hex[:10])
        self.project = self.app.create_project({"name": "test-task", "goal": "verify original CLI"})
        self.pid = self.project["id"]

    def tearDown(self):
        self.app.tmux("kill-server")
        self.temp.cleanup()

    def until(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("timed out waiting for isolated tmux run")

    def finished(self, rid):
        return self.until(lambda: (r if (r := self.app.run_info(rid))["status"] in {"exited", "failed", "stopped", "timed_out"} else None))

    def print_run(self, code, timeout=1):
        return self.app.start(self.pid, {"kind": "claude-native"}, native={
            "argv": [sys.executable, "-u", "-c", code], "cwd": self.project["path"],
            "execution_mode": "print", "output_format": "stream-json", "timeout_seconds": timeout})

    def test_stream_activity_and_live_extension_survive_original_deadline_and_reload(self):
        code = """import json,signal,time
def interrupted(*_):
    print(json.dumps({'type':'result','subtype':'interrupted','result':'保存完成'}),flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGINT,interrupted)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool1','name':'Read','input':{'file_path':'题目.txt'}}]}}),flush=True)
while True: time.sleep(.1)
"""
        run = self.print_run(code, timeout=0.7)
        rid = run["id"]
        self.until(lambda: self.app.run_info(rid)["activity"].get("last_tool") == "Read")
        self.assertIn("题目.txt", self.app.log(rid)["text"])
        self.assertEqual(self.app.run_info(rid)["status"], "running")
        extended = self.app.extend(rid, {"minutes": 1})
        time.sleep(0.9)
        reloaded = Console(self.app.root, self.app.state, self.app.tmux_socket)
        current = reloaded.run_info(rid)
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["deadline_epoch"], extended["deadline_epoch"])
        self.assertEqual(current["timeout_seconds"], 60.7)
        stopped = reloaded.stop(rid)
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(stopped["activity"]["result_received"])
        self.assertIn("保存完成", self.app.log(rid)["text"])
        self.until(lambda: '"type": "assistant"' in (self.app.run_path(rid) / "output.log").read_text())

    def test_timeout_interrupt_preserves_final_result_and_file_without_killing_other_panes(self):
        marker = self.base / "checkpoint.txt"
        code = f"""import json,signal,time
from pathlib import Path
def interrupted(*_):
    Path({str(marker)!r}).write_text('saved evidence')
    print(json.dumps({{'type':'result','subtype':'interrupted','result':'checkpoint saved'}}),flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGINT,interrupted)
print(json.dumps({{'type':'system','subtype':'init'}}),flush=True)
while True: time.sleep(.1)
"""
        self.app.tmux("new-session", "-d", "-s", "unrelated-timeout", "sleep 20")
        run = self.print_run(code, timeout=0.4)
        info = self.finished(run["id"])
        self.assertEqual(info["status"], "timed_out")
        self.assertEqual(info["stop_reason"], "timed_out")
        self.assertEqual(info["exit_code"], 0)
        self.assertTrue(info["activity"]["result_received"])
        self.assertEqual(marker.read_text(), "saved evidence")
        self.assertIn("checkpoint saved", self.app.log(run["id"])["text"])
        self.until(lambda: "checkpoint saved" in (self.app.run_path(run["id"]) / "output.log").read_text())
        self.assertEqual(self.app.tmux("has-session", "-t", "unrelated-timeout").returncode, 0)
        with self.assertRaises(WebError):
            self.app.extend(run["id"], {"minutes": 1})

    def test_timeout_escalates_and_removes_uncooperative_print_tools(self):
        marker = self.base / "tool-heartbeat"
        tool = f"""import signal,time
from pathlib import Path
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
while True:
    Path({str(marker)!r}).write_text(str(time.time()))
    time.sleep(.1)
"""
        code = f"""import signal,time,subprocess,sys
signal.signal(signal.SIGINT,signal.SIG_IGN)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
subprocess.Popen([sys.executable,'-u','-c',{tool!r}])
print('uncooperative fixture ready',flush=True)
while True: time.sleep(.1)
"""
        run = self.print_run(code, timeout=0.5)
        rid = run["id"]
        self.until(lambda: self.app.run_info(rid)["status"] == "stopping")
        with self.assertRaises(WebError):
            self.app.extend(rid, {"minutes": 1})
        info = self.until(lambda: (r if (r := self.app.run_info(rid))["status"] == "timed_out" else None), timeout=20)
        self.assertEqual(info["exit_code"], -9)
        self.assertFalse(info["activity"]["result_received"])
        saved = marker.read_text()
        time.sleep(0.3)
        self.assertEqual(marker.read_text(), saved)
        self.assertIn("SIGTERM", self.app.log(rid)["text"])

    def test_original_dispatch_is_used_with_per_run_model_profile(self):
        self.app.graph_action(self.pid, "fact", {"description": "observed input"})
        engine = self.base / "fake-engine"
        engine.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
                          "Path(__file__ + '.received').write_text(json.dumps({'argv':sys.argv,'model':os.environ.get('SAPPER_MODEL'),'token':os.environ.get('ANTHROPIC_AUTH_TOKEN')}))\n"
                          "sys.stdin.read()\nprint(json.dumps({'result':json.dumps({'accepted':True,'data':{'intents':[{'from':['f001'],'description':'verify the input','title':'Verify'}]}})}))\n")
        engine.chmod(0o700)
        profile = self.app.save_profile({"name": "test", "model": "fake-model", "engine_prefix": str(engine), "api_key": "fake-token-value"})
        run = self.app.start(self.pid, {"kind": "dispatch", "target": "reason", "profile_id": profile["id"], "request_id": "one"})
        self.assertEqual(self.app.start(self.pid, {"kind": "dispatch", "request_id": "one", "profile_id": profile["id"]})["id"], run["id"])
        info = self.finished(run["id"])
        self.assertEqual(info["exit_code"], 0)
        received = json.loads(Path(str(engine) + ".received").read_text())
        self.assertEqual(received["model"], "fake-model")
        self.assertEqual(received["token"], "fake-token-value")
        self.assertIn("--output-format", received["argv"])
        self.assertEqual(len(self.app.store(self.pid).list_intents()), 1)
        self.assertEqual(sapper.validate_graph(self.app.store(self.pid)), [])
        self.assertFalse((self.app.run_path(run["id"]) / "launch.json").exists())

    def test_literal_terminal_input_and_independent_web_restart(self):
        code = "print('READY',flush=True); value=input(); print('RECEIVED:'+value,flush=True)"
        run = self.app.start(self.pid, {"kind": "custom", "command": shlex.join([sys.executable, "-u", "-c", code])})
        self.until(lambda: "READY" in self.app.log(run["id"])["text"])
        reloaded = Console(self.app.root, self.app.state, self.app.tmux_socket)
        panes = reloaded.panes()
        self.assertEqual(len(panes), 1)
        literal = "quote';$(touch should-not-exist)"
        reloaded.send(panes[0]["id"], {"text": literal})
        self.assertEqual(self.finished(run["id"])["exit_code"], 0)
        self.until(lambda: "RECEIVED:" + literal in self.app.log(run["id"])["text"])
        self.assertFalse((Path(self.project["path"]) / "should-not-exist").exists())

    def test_stop_only_managed_session_and_its_child_processes(self):
        child_marker = self.base / "child-survived"
        child_code = f"import time;from pathlib import Path;time.sleep(2);Path({str(child_marker)!r}).touch()"
        parent_code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child_code!r}]);print('READY',flush=True);time.sleep(20)"
        run = self.app.start(self.pid, {"kind": "custom", "command": shlex.join([sys.executable, "-u", "-c", parent_code])})
        self.until(lambda: "READY" in self.app.log(run["id"])["text"])
        other = self.app.tmux("new-session", "-d", "-s", "unrelated", "sleep 20")
        self.assertEqual(other.returncode, 0)
        self.app.stop(run["id"])
        self.assertEqual(self.app.run_info(run["id"])["status"], "stopped")
        self.assertEqual(self.app.tmux("has-session", "-t", "unrelated").returncode, 0)
        time.sleep(2.2)
        self.assertFalse(child_marker.exists())

    def test_shared_graph_worker_and_original_commander_are_discovered(self):
        sibling = self.app.root / "test-task-w2"
        sibling.mkdir()
        (sibling / ".sapper").symlink_to(self.app.store(self.pid).root, target_is_directory=True)
        commander = self.app.root / "commander-home"
        commander.mkdir()
        self.assertEqual(self.app.tmux("new-session", "-d", "-s", "sapper-cmd", "-n", "commander", "-c", str(commander), "sleep 10").returncode, 0)
        self.assertEqual(self.app.tmux("new-window", "-t", "sapper-cmd", "-n", "test-task-w2", "-c", str(sibling), "sleep 10").returncode, 0)
        panes = self.app.panes()
        self.assertEqual(len(panes), 2)
        self.assertEqual(next(p for p in panes if p["name"] == "test-task-w2")["project_id"], self.pid)
        self.assertEqual(next(p for p in panes if p["name"] == "commander")["role"], "commander")

    def test_launch_inherits_service_environment_instead_of_old_tmux_server(self):
        self.app.tmux("new-session", "-d", "-s", "old-environment", "sleep 10")
        code = "import os;print(os.environ['SAPPER_WEB_TEST_VALUE'],flush=True)"
        with mock.patch.dict(os.environ, {"SAPPER_WEB_TEST_VALUE": "fresh-service-value"}):
            run = self.app.start(self.pid, {"kind": "custom", "command": shlex.join([sys.executable, "-u", "-c", code])})
        self.assertEqual(self.finished(run["id"])["exit_code"], 0)
        self.until(lambda: "fresh-service-value" in self.app.log(run["id"])["text"])


if __name__ == "__main__":
    unittest.main()
