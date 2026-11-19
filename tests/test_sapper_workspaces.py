"""Workspace, native-session and real worker-adapter integration fixtures."""
import contextlib
import base64
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
import sapper_roles
from sapper_web import Console, WebServer, WebError
from sapper_workspace import WorkspaceError, START_MARK
from sapper_terminal import TerminalBridge
from sapper_chat import ChatJournal

FIXTURE = r'''#!/usr/bin/env python3
import json,os,sys,subprocess,signal,time
from pathlib import Path
args=sys.argv[1:]
if '--help' in args:
    print('--mcp-config --strict-mcp-config --plugin-dir --setting-sources --settings --session-id --resume --add-dir --append-system-prompt --input-format --replay-user-messages --init-only')
    raise SystemExit(0)
if '--version' in args:
    print('2.1.318 (fixture)')
    raise SystemExit(0)
flag='--resume' if '--resume' in args else '--session-id'
sid=args[args.index(flag)+1]
history=Path(__file__).parent/'history'
history.mkdir(exist_ok=True)
if flag=='--resume' and not (history/sid).exists():
    print('missing session',flush=True)
    raise SystemExit(4)
(history/sid).touch()
calls=Path(__file__).parent/'calls.jsonl'
with calls.open('a') as f:
    f.write(json.dumps({'sid':sid,'flag':flag,'cwd':os.getcwd(),'agent':os.environ.get('SAPPER_AGENT_ID'),'model':os.environ.get('SAPPER_MODEL'),'argv':args})+'\n')
root=Path(os.environ['SAPPER_WORKSPACE_PATH'])
aid=os.environ['SAPPER_AGENT_ID']
def check_hook():
    hook=Path(os.environ['SAPPER_TEST_REPO'])/'lib/sapper-hook.py'
    p=subprocess.run([sys.executable,str(hook),'userpromptsubmit'],input=json.dumps({'cwd':os.getcwd()}),capture_output=True,text=True,check=True)
    with (Path(__file__).parent/'hook-results.jsonl').open('a') as f:
        f.write(json.dumps({'agent':aid,'output':p.stdout})+'\n')
check_hook()
if args[-1]=='SPAWN_SCOUT':
    worker=Path(os.environ['SAPPER_TEST_REPO'])/'docker/commander/worker.sh'
    seed=subprocess.check_output(['sapper','fact','challenge.txt is supplied','--creator',aid],text=True).strip()
    second=subprocess.check_output(['sapper','fact','workspace contains one challenge directory','--creator',aid],text=True).strip()
    subprocess.run(['bash',str(worker),'spawn',root.name,'--from',seed+','+second,'--role','scout','--p','--minutes','1','--mission','RECORD_EVIDENCE'],check=True)
if '-p' in args:
    (root/'outputs').mkdir(exist_ok=True)
    (root/'outputs'/aid).write_text((root/'challenge.txt').read_text())
    iid=os.environ.get('SAPPER_INTENT_ID')
    if iid and args[-1]!='WAIT_FOR_INTERRUPT':
        subprocess.run(['sapper','done',iid,'--worker',aid,'--fact','fixture read the actual challenge'],check=True)
    else:
        subprocess.run(['sapper','fact','fixture read the actual challenge','--creator',aid],check=True)
    if args[-1]=='WAIT_FOR_INTERRUPT':
        def interrupted(*_):
            print(json.dumps({'type':'result','subtype':'interrupted','result':'saved original evidence'}),flush=True)
            raise SystemExit(0)
        signal.signal(signal.SIGINT,interrupted)
        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'r1','name':'Read','input':{'file_path':'challenge.txt'}}]}}),flush=True)
        while True: time.sleep(.1)
    print(json.dumps({'type':'result','subtype':'success','result':'recorded'}),flush=True)
    raise SystemExit(0)
print('NATIVE_READY '+sid,flush=True)
try:
    for line in sys.stdin:
        if line.strip()=='exit': break
        check_hook()
        print('RECEIVED:'+line.rstrip('\n'),flush=True)
except KeyboardInterrupt:
    pass
'''

COMMANDER_FIXTURE = r'''#!/usr/bin/env python3
import json,os,sys,subprocess,time
from datetime import datetime,timezone
from pathlib import Path
args=sys.argv[1:]
if '--help' in args:
    print('--mcp-config --strict-mcp-config --plugin-dir --setting-sources --settings --session-id --resume --add-dir --append-system-prompt --input-format --replay-user-messages')
    raise SystemExit(0)
if '--version' in args:
    print('2.1.318 (fixture)')
    raise SystemExit(0)
sid=args[args.index('--resume' if '--resume' in args else '--session-id')+1]
path=Path(os.environ['CLAUDE_CONFIG_DIR'])/'projects'/'fixture'/(sid+'.jsonl')
path.parent.mkdir(parents=True,exist_ok=True)
def event(kind,content,stop=None):
    with path.open('a') as f:
        f.write(json.dumps({'type':kind,'sessionId':sid,'timestamp':datetime.now(timezone.utc).isoformat(),
                           'message':{'content':content,'stop_reason':stop}},ensure_ascii=False)+'\n')
def respond(prompt):
    event('user',prompt)
    if '[Sapper wake:' in prompt:
        event('assistant',[{'type':'tool_use','id':'board','name':'Bash'}],'tool_use')
        worker=Path(os.environ['SAPPER_TEST_REPO'])/'docker/commander/worker.sh'
        subprocess.run(['bash',str(worker),'board','--json'],check=True,stdout=subprocess.DEVNULL)
        event('user',[{'type':'tool_result','tool_use_id':'board','content':'real board succeeded'}])
    event('assistant',[{'type':'text','text':'60 秒后继续'}],'end_turn')
    print('NATIVE_READY '+sid,flush=True)
    print('❯',flush=True)
respond(args[-1])
for line in sys.stdin:
    respond(line.strip())
'''


class WorkspaceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workspace test ")
        self.base = Path(self.temp.name).resolve()
        self.question = self.base / "questions" / "题目 with spaces"
        self.question.mkdir(parents=True)
        (self.question / "challenge.txt").write_text("original evidence")
        self.app = Console(self.base / "runs", self.base / "state")

    def tearDown(self):
        self.temp.cleanup()

    def test_assignment_launch_failure_releases_same_intent_for_retry(self):
        manager = self.app.workspaces
        pid = manager.bind({"path": str(self.question)})["id"]
        manager.enable(pid, {})
        manager._capabilities = dict(available=True, session_id=True, resume=True, append_system=True, mcp_config=True, strict_mcp_config=True, plugin_dir=True, setting_sources=True, settings=True, skill_isolation=True)
        store = self.app.store(pid)
        basis = store.add_fact("fixture archive supplied", "user")["id"]
        with mock.patch.object(manager, "agent", return_value={"workspace_id": pid, "desired_state": "running"}):
            with self.assertRaisesRegex(WorkspaceError, "派单需要"):
                manager.create_session(pid, {"parent_agent_id": "parent", "mission": "inspect archive"})
        with self.assertRaisesRegex(sapper.FGError, "does not exist"):
            manager.create_session(pid, {"from": "f998", "mission": "inspect archive"})
        self.assertEqual(store.list_intents(), [])
        with mock.patch.object(self.app, "start", side_effect=WebError("no tmux", 503)):
            with self.assertRaisesRegex(WorkspaceError, "--intent i001"):
                manager.create_session(pid, {"from": basis, "mission": "inspect archive"})
        intent, = store.list_intents()
        self.assertEqual((intent["status"], intent["worker"], intent["to"]), ("open", None, None))
        with mock.patch.object(self.app, "start", return_value={"id": "test-run"}) as start:
            manager.create_session(pid, {"intent_id": intent["id"], "mission": "check the header first", "task_label": "检查文件头"})
            native = start.call_args.kwargs["native"]
            self.assertEqual(native["env"]["SAPPER_INTENT_ID"], intent["id"])
            self.assertEqual(manager.agent(native["agent_id"])["task_label"], "检查文件头")
            self.assertEqual(native["argv"][-1], "inspect archive\n派发补充：check the header first")
            with self.assertRaisesRegex(sapper.FGError, "claimed by"):
                manager.create_session(pid, {"intent_id": intent["id"]})
            gated = store.add_intent([basis], "requires approval", "user", requires_confirmation=True)
            with self.assertRaisesRegex(sapper.FGError, "requires confirmation"):
                manager.create_session(pid, {"intent_id": gated["id"]})
            start.assert_called_once()
        with contextlib.redirect_stdout(None):
            self.assertEqual(sapper.main(["--store", str(store.root), "flag", "fixture-flag",
                                          "--from-intent", intent["id"], "--creator", native["agent_id"]]), 0)
        result = store.get_fact(store.get_intent(intent["id"])["to"])
        self.assertEqual((result["kind"], result["source_intent"]), ("flag", intent["id"]))
        self.assertEqual(sapper.validate_graph(store), [])

    def test_binding_is_stable_and_does_not_write_to_existing_question(self):
        before = list(self.question.iterdir())
        p = self.app.workspaces.bind({"path": str(self.question), "goal": "solve fixture"})
        self.assertEqual(self.app.workspaces.bind({"path": str(self.question)})["id"], p["id"])
        self.assertEqual(list(self.question.iterdir()), before)
        self.assertEqual(self.app.project(p["id"])["status"], "not_enabled")
        self.assertEqual(self.app.graph(p["id"])["facts"], [])

    def test_external_binding_persists_and_updates_existing_process_roots(self):
        observer = Console(self.app.root, self.app.state)
        self.assertNotIn(self.question, observer.workspace_roots)
        p = self.app.workspaces.bind({"path": str(self.question)})
        self.assertIn(self.question, observer.workspace_roots)
        self.assertEqual(observer.project(p["id"])["path"], str(self.question))
        preview = observer.native.prepare(p["id"], {"operation": "doc", "values": {
            "id": "goal", "set_file": str(self.question / "challenge.txt")}})
        self.assertIn("--set-file=" + str(self.question / "challenge.txt"), preview["argv"])
        reloaded = Console(self.app.root, self.app.state)
        self.assertEqual(reloaded.project(p["id"])["id"], p["id"])
        self.assertEqual(reloaded.workspaces.bind({"path": str(self.question)})["id"], p["id"])

    def test_directory_picker_navigates_outside_startup_roots(self):
        roots = self.app.workspaces.directories()["roots"]
        self.assertIn(str(self.app.root), roots)
        self.assertIn(str(Path.home().resolve()), roots)
        self.assertIn(self.base.anchor, roots)
        listing = self.app.workspaces.directories(str(self.question.parent))
        self.assertEqual(listing["parent"], str(self.base))
        self.assertIn(str(self.question), [d["path"] for d in listing["directories"]])
        alias = self.base / "question-alias"
        alias.symlink_to(self.question, target_is_directory=True)
        self.assertEqual(self.app.workspaces.directories(str(alias))["path"], str(self.question))

    def test_picker_and_binding_reject_private_state_and_invalid_directories(self):
        alias = self.base / "private-alias"
        alias.symlink_to(self.app.state, target_is_directory=True)
        for path in (self.app.state, self.app.state / "agents", alias):
            with self.subTest(path=path):
                for action in (lambda: self.app.workspaces.bind({"path": str(path)}),
                               lambda: self.app.workspaces.directories(str(path))):
                    with self.assertRaises(WorkspaceError) as error:
                        action()
                    self.assertEqual(error.exception.status, 403)
        listing = self.app.workspaces.directories(str(self.base))
        self.assertNotIn(str(alias), [d["path"] for d in listing["directories"]])
        for path in (self.base / "missing", self.question / "challenge.txt"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError) as error:
                self.app.workspaces.bind({"path": str(path)})
            self.assertEqual(error.exception.status, 404)
        with self.assertRaises(WorkspaceError):
            self.app.workspaces.bind({"path": " "})

    def test_strategy_board_reports_real_graph_evidence_without_stuck_inference(self):
        p = self.app.workspaces.bind({"path": str(self.question), "goal": "solve fixture"})
        store = sapper.Store(self.question / ".sapper")
        store.init("test", "solve fixture")
        finding = store.add_fact("[REPORT] confirmed parser format", "worker-1", kind="note")
        store.add_fact("[HELP] need an independent decoder check", "worker-1", kind="note")
        store.add_fact("captured candidate", "worker-1", kind="flag", data={"status": "recorded"})
        store.add_intent([finding["id"]], "verify parser behavior", "worker-1")
        board = self.app.workspaces.strategy_board([p["id"]])
        row = board["workspaces"][0]
        self.assertEqual(row["flags"]["recorded"], 1)
        self.assertEqual(len(row["help"]), 1)
        self.assertEqual(len(row["reports"]), 1)
        self.assertEqual(row["open_intents"][0]["description"], "verify parser behavior")
        self.assertIn("不把安静状态自动判为卡死", board["observation_note"])

    def test_role_preview_uses_selected_commander_scope_only(self):
        current = self.app.workspaces.bind({"path": str(self.question), "goal": "solve fixture"})
        other_dir = self.question.parent / "other-preview"
        other_dir.mkdir()
        other = self.app.workspaces.bind({"path": str(other_dir), "goal": "other"})
        for role in ("commander", "general", "worker"):
            with self.assertRaisesRegex(WorkspaceError, "当前工作区"):
                self.app.workspaces.role_info(current["id"], role, [current["id"], other["id"]])
        preview = self.app.workspaces.role_info(current["id"], "commander")
        self.assertNotIn(other['id'], preview["prompt"])

    def test_enable_preserves_user_content_and_is_idempotent(self):
        claude = self.question / "CLAUDE.md"
        agents = self.question / "AGENTS.md"
        claude.write_text("User instructions: keep all existing evidence.\n")
        agents.write_text("User agent rules, never overwrite.\n")
        settings = self.question / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "echo user-hook"}]}]}}))
        p = self.app.workspaces.bind({"path": str(self.question)})
        result = self.app.workspaces.enable(p["id"], {})
        self.assertTrue(all(result["hooks"].values()))
        first = {f: f.read_bytes() for f in (claude, agents, settings)}
        self.app.workspaces.enable(p["id"], {})
        self.assertEqual({f: f.read_bytes() for f in first}, first)
        self.assertEqual(agents.read_text(), "User agent rules, never overwrite.\n")
        self.assertTrue(claude.read_text().startswith("User instructions:"))
        self.assertEqual(claude.read_text().count(START_MARK), 1)
        merged = json.loads(settings.read_text())
        self.assertEqual(merged["permissions"]["allow"], ["Read"])
        self.assertIn("echo user-hook", settings.read_text())
        self.assertEqual((self.question / "challenge.txt").read_text(), "original evidence")
        hook = Path(sapper.__file__).with_name("sapper-hook.py")
        proc = subprocess.run([sys.executable, str(hook), "userpromptsubmit"], input=json.dumps({"cwd": str(self.question)}), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("additionalContext", json.loads(proc.stdout)["hookSpecificOutput"])

    def test_rejects_instruction_symlink_escape_before_setup(self):
        external = self.base / "outside.md"
        external.write_text("unchanged")
        (self.question / "CLAUDE.md").symlink_to(external)
        p = self.app.workspaces.bind({"path": str(self.question)})
        with self.assertRaises(Exception):
            self.app.workspaces.enable(p["id"], {})
        self.assertFalse((self.question / ".sapper").exists())
        self.assertEqual(external.read_text(), "unchanged")

    def test_malformed_instruction_block_is_rejected_before_setup(self):
        (self.question / "CLAUDE.md").write_text(START_MARK)
        p = self.app.workspaces.bind({"path": str(self.question)})
        with self.assertRaises(WorkspaceError):
            self.app.workspaces.enable(p["id"], {})
        self.assertFalse((self.question / ".sapper").exists())

    def test_private_state_is_hidden_even_inside_a_bound_parent(self):
        app = Console(self.app.root, self.app.root / "custom-private")
        p = app.workspaces.bind({"path": str(app.root)})
        # A custom state directory name must receive the same protection as .sapper-web.
        with self.assertRaises(WebError):
            app.file(p["id"], "custom-private/token")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class NativeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native ws ")
        self.base = Path(self.temp.name).resolve()
        self.bin = self.base / "fixture-claude"
        self.bin.write_text(FIXTURE)
        self.bin.chmod(0o700)
        self.question = self.base / "questions" / "challenge"
        self.question.mkdir(parents=True)
        (self.question / "challenge.txt").write_text("read this original question")
        self.socket = "sapper-workspace-test-" + uuid.uuid4().hex[:10]
        self.env = dict(os.environ)
        os.environ["ANTHROPIC_API_KEY"] = "workspace-fixture-only"
        os.environ["SAPPER_TEST_REPO"] = str(Path(sapper.__file__).resolve().parents[1])
        self.app = Console(self.base / "runs", self.base / "state", self.socket, claude_bin=str(self.bin))
        self.project = self.app.workspaces.bind({"path": str(self.question), "goal": "read fixture"})
        self.pid = self.project["id"]
        self.app.workspaces.enable(self.pid, {})

    def tearDown(self):
        # tmux death signals runners asynchronously. Wait for their final writes
        # before removing the state directory (not just the server socket).
        runners = [json.loads(p.read_text()).get('wrapper_pid')
                   for p in (self.app.state / 'runs').glob('*/run.json')]
        self.app.tmux("kill-server")
        until = time.monotonic() + 5
        while runners and time.monotonic() < until:
            alive = []
            for pid in runners:
                if not pid:
                    continue
                try:
                    os.kill(pid, 0)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
            runners = alive
            if runners:
                time.sleep(.05)
        os.environ.clear()
        os.environ.update(self.env)
        self.temp.cleanup()

    def until(self, predicate, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            time.sleep(0.08)
        self.fail("timed out waiting for workspace fixture")

    def launch(self, **data):
        run = self.app.workspaces.create_session(self.pid, {"mission": "read fixture", **data})
        self.until(lambda: "NATIVE_READY" in self.app.log(run["id"])["text"] or self.app.run_info(run["id"])["status"] in ("exited", "failed"))
        return run

    def test_same_directory_sessions_have_distinct_native_ids_and_resume_exactly(self):
        one = self.launch(location="root")
        two = self.launch(location="root")
        self.assertNotEqual(one["claude_session_id"], two["claude_session_id"])
        self.assertEqual(one["cwd"], str(self.question))
        a = self.app.workspaces.agent(one["agent_id"])
        self.app.workspaces.stop_agent(a["id"])
        resumed = self.app.workspaces.resume(a["id"], {})
        self.until(lambda: "NATIVE_READY" in self.app.log(resumed["id"])["text"])
        self.assertEqual(resumed["claude_session_id"], one["claude_session_id"])
        self.assertIn("--resume", resumed["command"])
        self.assertNotIn("--continue", resumed["command"])
        self.assertEqual(self.app.tmux("has-session", "-t", two["session"]).returncode, 0)
        reloaded = Console(self.app.root, self.app.state, self.socket, workspace_roots=[self.question.parent], claude_bin=str(self.bin))
        self.assertEqual(len([a for a in reloaded.workspaces.agents(self.pid) if a.get("pane_id")]), 2)

    def test_worker_status_reads_native_phases_without_scheduling_or_rewriting_history(self):
        self.bin.write_text(COMMANDER_FIXTURE)
        run = self.launch()
        agent = self.app.workspaces.agent(run["agent_id"])
        path = self.app.workspaces.commander.transcript(agent)
        original = path.read_bytes()
        self.assertEqual(self.app.workspaces.agents(self.pid)[0]["execution_observation"]["phase"], "idle")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.app.workspaces.commander.path(agent["id"]).exists())
        event = {"type":"assistant", "sessionId":agent["claude_session_id"], "timestamp":sapper.now_iso(),
                 "message":{"content":[{"type":"tool_use", "id":"ask", "name":"AskUserQuestion"}]}}
        encoded = json.dumps(event) + "\n"
        with path.open("a") as file:
            file.write(encoded[:20])
        self.assertEqual(self.app.workspaces.agents(self.pid)[0]["execution_observation"]["phase"], "unknown")
        with path.open("a") as file:
            file.write(encoded[20:])
        self.assertEqual(self.app.workspaces.agents(self.pid)[0]["execution_observation"]["phase"], "waiting_user")
        self.assertEqual(self.app.workspaces.services(self.pid), [])

    def test_idle_commander_wakes_in_place_and_crash_resumes_with_verified_board(self):
        self.bin.write_text(COMMANDER_FIXTURE)
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.base / 'native-transcripts')
        run = self.launch(role='commander')
        aid = run['agent_id']
        monitor = self.app.workspaces.commander
        with mock.patch('sapper_commander.IDLE_SECONDS', 0):
            monitor.tick(aid, self.app.workspaces.agents(self.pid))
        self.until(lambda: monitor.view(aid).get('verification') == 'board_confirmed')
        self.assertEqual(self.app.workspaces.agent(aid)['run_ids'], [run['id']])
        self.assertEqual(monitor.view(aid)['last_board']['run_id'], run['id'])
        # Actual process loss, not a merely quiet model/tool.
        self.app.tmux('kill-session', '-t', run['session'])
        self.until(lambda: not next(a for a in self.app.workspaces.agents(self.pid) if a['id'] == aid)['pane_id'])
        monitor.tick(aid, self.app.workspaces.agents(self.pid))
        new_run = self.app.workspaces.agent(aid)['run_id']
        self.assertNotEqual(new_run, run['id'])
        self.until(lambda: monitor.view(aid).get('last_board', {}).get('run_id') == new_run)
        self.assertEqual(monitor.view(aid)['verification'], 'board_confirmed')
        self.assertEqual(self.app.run_info(new_run)['claude_session_id'], run['claude_session_id'])
        self.assertEqual(monitor.view(aid)['recoveries'], 0)

    def test_resume_rejects_a_changed_model_protocol(self):
        profile = self.app.save_profile({"name": "original", "model": "fixture-model"})
        run = self.launch(profile_id=profile["id"])
        self.app.workspaces.stop_agent(run["agent_id"])
        self.app.save_profile({**profile, "protocol": "responses"})
        with self.assertRaises(WorkspaceError):
            self.app.workspaces.resume(run["agent_id"], {})

    def test_timed_out_print_role_is_not_auto_restarted_and_resumes_exact_session(self):
        start = self.app.start
        def short_budget(pid, data, *, native):
            return start(pid, data, native={**native, "timeout_seconds": 1})
        with mock.patch.object(self.app, "start", side_effect=short_budget):
            store = self.app.store(self.pid)
            basis = store.add_fact("fixture input exists", "user")["id"]
            run = self.app.workspaces.create_session(self.pid, {"from": basis, "execution_mode": "print", "mission": "WAIT_FOR_INTERRUPT"})
        rid, aid = run["id"], run["agent_id"]
        self.until(lambda: self.app.run_info(rid)["status"] == "timed_out")
        self.until(lambda: self.app.tmux("has-session", "-t", run["session"]).returncode != 0)
        self.launch(role="commander")
        time.sleep(3.2)
        self.assertEqual(self.app.workspaces.agent(aid)["run_ids"], [rid])
        self.assertEqual((self.question / "outputs" / aid).read_text(), "read this original question")
        self.assertIsNone(store.get_intent(run["intent_id"])["to"])
        resumed = self.app.workspaces.resume(aid, {"minutes": 2, "continuation": "VERIFY_EXISTING_EVIDENCE"})
        self.until(lambda: self.app.run_info(resumed["id"])["status"] == "exited")
        self.assertEqual(resumed["claude_session_id"], run["claude_session_id"])
        intent, = store.list_intents()
        self.assertEqual(intent["id"], resumed["intent_id"])
        self.assertEqual(store.get_fact(intent["to"])["source_intent"], run["intent_id"])
        self.assertEqual(resumed["timeout_seconds"], 120)
        calls = [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls[-1]["flag"], "--resume")
        self.assertEqual(calls[-1]["argv"][-1], "VERIFY_EXISTING_EVIDENCE")
        self.assertEqual(calls[-1]["argv"][calls[-1]["argv"].index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", calls[-1]["argv"])

    def test_worker_entry_extends_print_budget_and_receives_targeted_orders(self):
        run = self.app.workspaces.create_session(self.pid, {"execution_mode": "print", "mission": "WAIT_FOR_INTERRUPT", "minutes": 1})
        aid = run["agent_id"]
        self.until(lambda: self.app.run_info(run["id"])["activity"].get("last_tool") == "Read")
        result = self.app.workspaces.message(aid, {"text": "preserve current scripts"})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.app.graph(self.pid)["hints"], [])
        self.assertEqual(ChatJournal(self.app.state, aid).inbox()[0]["text"], "preserve current scripts")
        env = {**os.environ, "SAPPER_MODE": "workspace", "SAPPER_WEB_STATE": str(self.app.state),
               "SAPPER_ROOT": str(self.app.root), "SAPPER_TMUX_SOCKET": self.socket,
               "SAPPER_WORKSPACE_ID": self.pid, "SAPPER_CLAUDE_BIN": str(self.bin),
               "SAPPER_WORKSPACE_ALLOWED_ROOTS": json.dumps([str(self.question.parent)])}
        worker = Path(sapper.__file__).resolve().parents[1] / "docker/commander/worker.sh"
        extended = subprocess.run(["bash", str(worker), "extend", aid, "--minutes", "2"], env=env,
                                  capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(extended.stdout)["timeout_seconds"], 180)
        peek = subprocess.run(["bash", str(worker), "peek", aid], env=env, capture_output=True, text=True, check=True)
        self.assertIn("工具调用 Read", peek.stdout)
        self.app.workspaces.stop_agent(aid)
        resumed = subprocess.run(["bash", str(worker), "resume", aid, "--minutes", "1", "--mission", "CHECKPOINT_CONTINUATION"],
                                 env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(resumed.stdout)["claude_session_id"], run["claude_session_id"])

    def test_delete_running_role_preserves_other_workspace_and_evidence(self):
        basis = self.app.store(self.pid).add_fact("challenge supplied", "operator")["id"]
        run = self.launch(role="general", location="agent", **{"from": [basis]})
        aid = run["agent_id"]
        directory = Path(self.app.workspaces.agent(aid)["cwd"])
        (directory / "evidence.txt").write_text("keep this result")
        fact = self.app.store(self.pid).add_fact("recorded evidence", aid)
        other = self.question.parent / "unrelated"
        other.mkdir()
        project = self.app.workspaces.bind({"path": str(other)})
        self.app.workspaces.enable(project["id"], {})
        survivor = self.app.workspaces.create_session(project["id"], {"mission": "other"})
        self.until(lambda: "NATIVE_READY" in self.app.log(survivor["id"])["text"])

        self.app.workspaces.delete_agent(aid)
        released = self.app.store(self.pid).get_intent(run["intent_id"])
        self.assertEqual((released["worker"], released["status"], released["to"]), (None, "open", None))
        self.assertEqual(self.app.run_info(run["id"])["status"], "stopped")
        self.assertNotEqual(self.app.tmux("has-session", "-t", run["session"]).returncode, 0)
        self.assertEqual(self.app.tmux("has-session", "-t", survivor["session"]).returncode, 0)
        self.assertFalse(any(a["id"] == aid for a in self.app.workspaces.agents()))
        self.assertFalse(any(a["id"] == aid for a in self.app.overview()["agents"]))
        self.assertEqual((directory / "evidence.txt").read_text(), "keep this result")
        self.assertIn(fact, self.app.store(self.pid).list_facts())
        self.assertIn("NATIVE_READY", self.app.log(run["id"])["text"])
        with self.assertRaises(WorkspaceError) as raised:
            self.app.workspaces.resume(aid, {})
        self.assertEqual(raised.exception.status, 404)
        with self.assertRaises(WorkspaceError):
            self.app.workspaces.create_session(self.pid, {"parent_agent_id": aid})
        # Repeat deletion and historical run controls cannot revive the role.
        self.app.workspaces.delete_agent(aid)
        self.app.stop(run["id"])
        reloaded = Console(self.app.root, self.app.state, self.socket,
                           workspace_roots=[self.question.parent], claude_bin=str(self.bin))
        self.assertFalse(any(a["id"] == aid for a in reloaded.workspaces.agents()))

    def test_delete_parent_keeps_child_running_and_resumable(self):
        parent = self.launch(role="general")
        basis = self.app.store(self.pid).add_fact("fixture present", "test")["id"]
        child = self.launch(parent_agent_id=parent["agent_id"], **{"from": basis})
        self.app.workspaces.delete_agent(parent["agent_id"])
        a = self.app.workspaces.agent(child["agent_id"])
        self.assertIsNone(a["parent_agent_id"])
        self.assertEqual(a["deleted_parent_agent_id"], parent["agent_id"])
        self.assertEqual(self.app.tmux("has-session", "-t", child["session"]).returncode, 0)
        self.app.workspaces.stop_agent(a["id"])
        resumed = self.app.workspaces.resume(a["id"], {})
        self.until(lambda: "NATIVE_READY" in self.app.log(resumed["id"])["text"])
        self.assertEqual(resumed["claude_session_id"], child["claude_session_id"])

    def test_delete_finished_role_is_not_restarted_by_watchdog(self):
        run = self.launch(role="scout", execution_mode="print")
        self.until(lambda: self.app.run_info(run["id"])["status"] == "exited")
        self.app.workspaces.delete_agent(run["agent_id"])
        main = self.launch(role="commander")
        time.sleep(3.4)
        record = self.app.workspaces.agent(run["agent_id"], include_deleted=True)
        self.assertEqual(record["desired_state"], "stopped")
        self.assertTrue(record["deleted_at"])
        self.assertEqual(len(record["run_ids"]), 1)
        self.assertFalse(any(a["id"] == run["agent_id"] for a in self.app.workspaces.agents(self.pid)))

    def test_delete_attached_role_keeps_external_terminal_without_rediscovery(self):
        session = "external-delete-fixture"
        self.assertEqual(self.app.tmux("new-session", "-d", "-s", session,
                                       "-c", str(self.question), "sleep 60").returncode, 0)
        pane = next(p for p in self.app.panes() if p["session"] == session)
        a = self.app.workspaces.attach(self.pid, {"pane_id": pane["id"], "role": "worker"})
        self.app.workspaces.delete_agent(a["id"])
        self.assertEqual(self.app.tmux("has-session", "-t", session).returncode, 0)
        self.assertFalse(any(a.get("pane_id") == pane["id"] for a in self.app.workspaces.agents(self.pid)))
        self.assertEqual(self.app.pane(pane["id"])["session"], session)

    def test_commander_background_spawn_registers_scout_and_shared_graph(self):
        commander = self.launch(role="commander", mission="SPAWN_SCOUT")
        def completed_child():
            children = [a for a in self.app.workspaces.agents(self.pid) if a.get("parent_agent_id") == commander["agent_id"]]
            return next((a for a in children if a["status"] == "exited"), None)
        scout = self.until(completed_child)
        self.assertEqual(scout["role"], "scout")
        self.assertEqual(scout["execution_mode"], "print")
        outputs = [json.loads(line) for line in (self.base / "hook-results.jsonl").read_text().splitlines()]
        commander_hook = next(row["output"] for row in outputs if row["agent"] == commander["agent_id"])
        scout_hook = next(row["output"] for row in outputs if row["agent"] == scout["id"])
        self.assertIn("[Sapper 指挥官职责", commander_hook)
        self.assertNotIn("[Sapper 指挥官职责", scout_hook)
        self.assertNotEqual(scout["cwd"], str(self.question))
        self.assertEqual((Path(scout["cwd"]) / ".sapper").resolve(), self.question / ".sapper")
        self.assertEqual((Path(scout["cwd"]) / "CLAUDE.md").read_text(), "@../../CLAUDE.md\n")
        self.assertEqual((self.question / "outputs" / scout["id"]).read_text(), "read this original question")
        facts = self.app.store(self.pid).list_facts()
        intent, = self.app.store(self.pid).list_intents()
        result = next(f for f in facts if f.get("creator") == scout["id"])
        self.assertEqual(intent["from"], ["f001", "f002"])
        self.assertEqual((intent["creator"], intent["worker"]), (commander["agent_id"], scout["id"]))
        self.assertEqual((intent["id"], intent["to"], intent["status"]), (scout["intent_id"], result["id"], "done"))
        self.assertEqual(result["source_intent"], intent["id"])
        agent = next(a for a in self.app.workspaces.strategy_board([self.pid])["agents"] if a["id"] == scout["id"])
        self.assertEqual((agent["intent_status"], agent["result_fact"]), ("done", result["id"]))
        self.assertEqual(sapper.validate_graph(self.app.store(self.pid)), [])

    def test_terminal_raw_input_resize_and_log_source(self):
        run = self.launch()
        agent = next(a for a in self.app.workspaces.agents(self.pid) if a["id"] == run["agent_id"])
        bridge = TerminalBridge(self.app)
        prepared = bridge.prepare(agent["pane_id"])
        self.assertTrue(prepared["owned"])
        self.assertEqual(prepared["mode"], "stream")
        bridge.resize(agent["pane_id"], {"cols": 91, "rows": 31})
        self.assertEqual(bridge.dimensions(agent["pane_id"])["cols"], 91)
        bridge.input(agent["pane_id"], {"data": "  unicode 线索  \r"})
        self.until(lambda: "RECEIVED:  unicode 线索  " in self.app.log(run["id"])["text"])

    def test_watchdog_does_not_restart_explicitly_stopped_agent(self):
        run = self.launch(role="general")
        self.launch(role="commander")
        self.assertEqual(len(self.app.workspaces.agents(self.pid)), 2)
        self.app.workspaces.stop_agent(run["agent_id"])
        time.sleep(3.4)
        record = self.app.workspaces.agent(run["agent_id"])
        self.assertEqual(record["desired_state"], "stopped")
        self.assertEqual(len(record["run_ids"]), 1)

    def test_new_commander_survives_a_paused_historical_commander(self):
        old = self.launch(role="commander")
        self.app.workspaces.stop_agent(old["agent_id"])
        self.app.workspaces.commander.configure(old["agent_id"], "paused", "历史会话已停止")
        profile = self.app.save_profile({"name":"lv-glm-fixture", "model":"fixture-model", "api_key":"fixture-only"})
        self.app.save_role_models({"assignments":{"commander":profile["id"]},
            "expected_revision":self.app.role_models()["revision"]}, self.pid)
        request = {"role":"commander", "new_session":True, "request_id":str(uuid.uuid4())}
        run = self.app.workspaces.dispatch(self.pid, request)
        self.until(lambda: "NATIVE_READY" in self.app.log(run["id"])["text"])
        service = next(s for s in self.app.workspaces.services(self.pid) if s["status"] in ("starting", "running"))
        self.until(lambda: run["agent_id"] in self.app.log(service["run_id"])["text"])
        self.assertEqual(self.app.workspaces.dispatch(self.pid, request)["id"], run["id"])
        with self.assertRaisesRegex(WorkspaceError, "仍在运行"):
            self.app.workspaces.dispatch(self.pid, {"role":"commander", "new_session":True})
        time.sleep(13)  # Past the companion's startup grace and two watchdog sweeps.
        self.assertEqual(self.app.run_info(run["id"])["status"], "running")
        self.assertEqual(self.app.run_info(service["run_id"])["status"], "running")
        self.assertEqual(self.app.workspaces.agent(run["agent_id"])["profile_id"], profile["id"])
        self.assertEqual(self.app.workspaces.agent(old["agent_id"])["desired_state"], "stopped")

    def test_model_change_requires_explicit_new_session(self):
        profile = self.app.save_profile({"name": "one", "model": "fixture-v1"})
        run = self.launch(profile_id=profile["id"])
        self.app.workspaces.stop_agent(run["agent_id"])
        self.app.save_profile({"id": profile["id"], "name": "one", "model": "fixture-v2"})
        with self.assertRaises(WorkspaceError):
            self.app.workspaces.resume(run["agent_id"], {})

    def test_shared_context_reaches_background_roles_and_running_session_updates(self):
        config = {"expected_revision": 0, "enabled": True, "prompt_enabled": True, "hook_enabled": True,
                  "content": "SERVER_ALPHA: Linux x86_64 via ssh x64-debug"}
        self.app.save_context(config)
        commander = self.launch(role="commander", mission="SPAWN_SCOUT")
        primary = self.launch()
        self.launch(role="verifier")
        self.until(lambda: len(self.app.workspaces.agents(self.pid)) == 4)
        calls = [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 4)
        for call in calls:
            appended = call["argv"][call["argv"].index("--append-system-prompt") + 1]
            self.assertIn("SERVER_ALPHA", appended)
            self.assertIn("Sapper native role standing orders", appended)
            self.assertIn(sapper_roles.PROMPT_VERSION, appended)
            self.assertEqual(appended.count("Sapper shared environment"), 2)  # opening and closing markers
        children = [a for a in self.app.workspaces.agents(self.pid) if a.get("parent_agent_id") == commander["agent_id"]]
        self.assertEqual(children[0]["context_revision"], 1)
        self.assertEqual(children[0]["role_prompt_version"], sapper_roles.PROMPT_VERSION)
        self.assertEqual(len(children[0]["role_prompt_sha256"]), 64)
        self.app.save_context({**config, "expected_revision": 1, "content": "SERVER_BETA: updated shared environment"})
        self.app.workspaces.message(primary["agent_id"], {"text": "reload-shared-context"})
        def updated():
            a = next(a for a in self.app.workspaces.agents(self.pid) if a["id"] == primary["agent_id"])
            return a if a["shared_context"]["hook_revision"] == 2 else None
        observed = self.until(updated)
        self.assertEqual(observed["context_revision"], 1)
        outputs = [json.loads(line) for line in (self.base / "hook-results.jsonl").read_text().splitlines()]
        latest = next(o for o in reversed(outputs) if o["agent"] == primary["agent_id"])
        self.assertIn("SERVER_BETA", latest["output"])
        self.assertNotIn("SERVER_ALPHA", latest["output"])
        self.app.workspaces.stop_agent(primary["agent_id"])
        resumed = self.app.workspaces.resume(primary["agent_id"], {})
        self.until(lambda: "NATIVE_READY" in self.app.log(resumed["id"])["text"])
        self.assertEqual(resumed["claude_session_id"], primary["claude_session_id"])
        self.assertEqual(self.app.workspaces.agent(primary["agent_id"])["context_revision"], 2)

    def test_hook_only_context_reaches_role_without_duplication_in_launch_prompt(self):
        self.app.save_context({"expected_revision": 0, "enabled": True, "prompt_enabled": False, "hook_enabled": True,
                               "content": "HOOK_ONLY_RESOURCE"})
        run = self.launch(role="general")
        self.assertNotIn("HOOK_ONLY_RESOURCE", run["command"])
        a = next(a for a in self.app.workspaces.agents(self.pid) if a["id"] == run["agent_id"])
        self.assertEqual(a["shared_context"]["launch_revision"], 0)
        self.assertEqual(a["shared_context"]["hook_revision"], 1)
        results = [json.loads(line) for line in (self.base / "hook-results.jsonl").read_text().splitlines()]
        self.assertIn("HOOK_ONLY_RESOURCE", results[-1]["output"])

    def test_scoped_commander_and_stop_do_not_cross_workspaces(self):
        other = self.question.parent / "other"
        other.mkdir()
        p = self.app.workspaces.bind({"path": str(other)})
        self.app.workspaces.enable(p["id"], {})
        with self.assertRaisesRegex(WorkspaceError, "当前工作区"):
            self.launch(role="commander", workspace_scope=[self.pid, p["id"]])
        commander = self.launch(role="commander")
        first = self.launch()
        basis = self.app.store(p["id"]).add_fact("other workspace input", "test")["id"]
        with self.assertRaisesRegex(WorkspaceError, "不属于"):
            self.app.workspaces.create_session(p["id"], {"mission": "other", "from": basis, "parent_agent_id": commander["agent_id"]})
        second = self.app.workspaces.create_session(p["id"], {"mission": "other"})
        self.until(lambda: "NATIVE_READY" in self.app.log(second["id"])["text"])
        self.app.workspaces.stop_workspace(self.pid)
        self.assertEqual(self.app.run_info(first["id"])["status"], "stopped")
        self.assertEqual(self.app.workspaces.agent(commander["agent_id"])["desired_state"], "stopped")
        self.assertEqual(self.app.tmux("has-session", "-t", second["session"]).returncode, 0)
        self.assertEqual(self.app.workspaces.agent(second["agent_id"])["desired_state"], "running")

    def test_authenticated_terminal_http_and_sse_reconnect(self):
        run = self.launch()
        pane = next(a["pane_id"] for a in self.app.workspaces.agents(self.pid) if a["id"] == run["agent_id"])
        server = WebServer(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"/api/panes/{pane.replace('%', '%25')}"
        def request(path, method="GET", body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            c.request(method, path, json.dumps(body) if body is not None else None,
                      headers={"Content-Type": "application/json", **(headers or {})})
            return c, c.getresponse()
        try:
            c, r = request(url + "/stream")
            self.assertEqual(r.status, 401)
            r.read(); c.close()
            self.app.auth.initialize("admin", "fixture stream password 123")
            c, r = request("/api/session", "POST", {"username": "admin", "password": "fixture stream password 123"}, {"Content-Type": "application/json"})
            headers = {"Cookie": r.getheader("Set-Cookie").split(";", 1)[0]}
            csrf = json.loads(r.read())["csrf"]; c.close()
            c, r = request(url + "/input", "POST", {"data": "blocked"}, headers)
            self.assertEqual(r.status, 403)
            r.read(); c.close()
            def output(h):
                c, r = request(url + "/stream", headers=h)
                self.assertEqual(r.status, 200)
                self.assertEqual(r.getheader("X-Accel-Buffering"), "no")
                cursor = ""
                while True:
                    raw = r.readline()
                    self.assertTrue(raw, "terminal stream closed before delivering output")
                    line = raw.decode().strip()
                    if line.startswith("id: "): cursor = line[4:]
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if event["type"] == "output":
                            r.close(); c.close()
                            return cursor, base64.b64decode(event["data"]).decode()
            cursor, initial = output(headers)
            self.assertIn("NATIVE_READY", initial)
            c, r = request(url + "/input", "POST", {"data": "reconnect-proof\r"}, {**headers, "X-Sapper-CSRF": csrf})
            self.assertEqual(r.status, 200); r.read(); c.close()
            self.until(lambda: "RECEIVED:reconnect-proof" in self.app.log(run["id"])["text"])
            _, following = output({**headers, "Last-Event-ID": cursor})
            self.assertIn("RECEIVED:reconnect-proof", following)
            self.assertNotIn("NATIVE_READY", following)
            c, r = request(url + "/stream", headers=headers)
            self.assertEqual(r.status, 200)
            while not r.readline().startswith(b"data: "):
                pass
            logout, response = request("/api/logout", "POST", {}, {**headers, "X-Sapper-CSRF": csrf})
            self.assertEqual(response.status, 200); response.read(); logout.close()
            self.assertIn('"type": "ended"', r.read().decode())
            c.close()
            sid, _ = self.app.auth.login("admin", "fixture stream password 123", "fixture")
            reset_headers = {"Cookie": f"sapper_web={sid}"}
            c, r = request(url + "/stream", headers=reset_headers)
            self.assertEqual(r.status, 200)
            while not r.readline().startswith(b"data: "):
                pass
            self.app.auth.reset_password("admin", "fixture replacement stream password 456")
            self.assertIn('"type": "ended"', r.read().decode())
            c.close()
            c, r = request(url + "/stream", headers=reset_headers)
            self.assertEqual(r.status, 401)
            r.read(); c.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
