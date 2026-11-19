"""Native wire, human questions, targeted orders and companion lifecycle."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_chat import ChatJournal, NativeConversation
from sapper_workspace import WorkspaceError
from sapper_web import Console
import test_sapper_workspaces as workspace_fixture


CHAT_FIXTURE = r'''#!/usr/bin/env python3
import json,os,sys,time,subprocess
from pathlib import Path
args=sys.argv[1:]
if '--help' in args:
    print('--mcp-config --strict-mcp-config --plugin-dir --setting-sources --settings --session-id --resume --add-dir --append-system-prompt --input-format --output-format --replay-user-messages')
    raise SystemExit(0)
if '--version' in args:
    print('2.1.318 (fixture)')
    raise SystemExit(0)
sid=args[args.index('--resume' if '--resume' in args else '--session-id')+1]
if '--input-format' not in args:
    print('NATIVE_READY '+sid,flush=True)
    for line in sys.stdin: print('child '+line,flush=True)
    raise SystemExit(0)
def emit(event): print(json.dumps(event,ensure_ascii=False),flush=True)
def respond(value):
    emit({'type':'assistant','message':{'id':str(time.time_ns()),'content':[{'type':'text','text':value}]}})
    emit({'type':'result','subtype':'success','result':value,'is_error':False})
for line in sys.stdin:
    frame=json.loads(line)
    if frame['type']=='control_request':
        emit({'type':'control_response','response':{'subtype':'success','request_id':frame['request_id'],'response':{}}})
    elif frame['type']=='user':
        emit(frame)
        value=frame['message']['content']
        if value=='ask':
            emit({'type':'assistant','message':{'id':'question-call','content':[{'type':'tool_use','id':'q1','name':'AskUserQuestion','input':{}}]}})
            emit({'type':'control_request','request_id':'decision-1','request':{'subtype':'can_use_tool','tool_name':'AskUserQuestion','input':{'questions':[{'question':'采用哪条路径？','header':'路径','multiSelect':False,'options':[{'label':'路径 A','description':'第一条'},{'label':'路径 B','description':'第二条'}]}]}}})
        elif value=='permission':
            emit({'type':'control_request','request_id':'permission-1','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'echo fixture'}}})
        elif '[Sapper wake:' in value:
            worker=Path(os.environ['SAPPER_TEST_REPO'])/'docker/commander/worker.sh'
            subprocess.run(['bash',str(worker),'board','--json'],stdout=subprocess.DEVNULL,check=True)
            respond('已读板并继续调度')
        else: respond('收到：'+value)
    elif frame['type']=='control_response':
        response=frame['response']['response']
        if frame['response']['request_id']=='decision-1':
            emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'q1','content':'answered'}]}})
        respond('回答已接收：'+json.dumps(response,ensure_ascii=False))
'''


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = ChatJournal(self.temp.name, "a" * 16)

    def test_retry_uses_message_identity_and_uncertain_delivery_is_not_replayed(self):
        rid = "b" * 16
        self.journal.begin(rid, "goal")
        mid = str(uuid.uuid4())
        self.journal.enqueue("correction", mid)
        self.journal.enqueue("correction", mid)
        with self.assertRaisesRegex(Exception, "不同内容"):
            self.journal.enqueue("different", mid)
        frames = self.journal.outgoing(rid)
        self.assertEqual([f["message"]["content"] for f in frames], ["goal", "correction"])
        self.journal.begin("c" * 16, "resume", resume=True)
        frames = self.journal.outgoing("c" * 16)
        self.assertEqual([f["message"]["content"] for f in frames], ["resume"])
        self.assertEqual(len(self.journal.view()["messages"]), 2)

    def test_question_answer_is_correlated_and_correction_stays_queued(self):
        rid, sid = "b" * 16, str(uuid.uuid4())
        self.journal.begin(rid, "goal")
        self.journal.outgoing(rid)
        self.journal.receive(rid, sid, {"type":"control_request", "request_id":"q", "request":{
            "subtype":"can_use_tool", "tool_name":"AskUserQuestion", "input":{"questions":[{"question":"Which?"}]}}})
        self.journal.enqueue("keep running unrelated work")
        self.assertEqual(self.journal.outgoing(rid), [])
        with self.assertRaisesRegex(Exception, "每个问题"):
            self.journal.answer("q", rid, answers={})
        self.journal.answer("q", rid, answers={"Which?":"A"})
        frames = self.journal.outgoing(rid)
        self.assertEqual(frames[0]["response"]["request_id"], "q")
        self.assertEqual(frames[0]["response"]["response"]["updatedInput"]["answers"], {"Which?":"A"})
        self.assertEqual(frames[1]["message"]["content"], "keep running unrelated work")
        with self.assertRaisesRegex(Exception, "问题已结束"):
            self.journal.answer("q", rid, answers={"Which?":"B"})

    def test_inbox_is_private_and_pagination_retains_old_messages(self):
        for i in range(105):
            self.journal.enqueue(str(i))
        other = ChatJournal(self.temp.name, "b" * 16)
        self.assertEqual(other.inbox(), [])
        view = self.journal.view()
        self.assertTrue(view["has_more"])
        older = self.journal.view(view["messages"][0]["id"])
        self.assertEqual([m["text"] for m in older["messages"]], [str(i) for i in range(5)])
        self.assertEqual(len(self.journal.inbox()), 105)
        self.assertEqual(self.journal.inbox(), [])


class LegacyConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = Console(self.base / "tasks", self.base / "state")
        self.app.panes = lambda: []
        self.project = self.app.create_project({"name":"old", "goal":"preserve old history"})

    def historical_agent(self, number, *, project=None):
        project = project or self.project
        sid = str(uuid.uuid4())
        record = {"id":f"{number:016x}","workspace_id":project["id"],"workspace_scope":[project["id"]],
            "role":"commander","name":f"old {number}","desired_state":"stopped","execution_mode":"tui",
            "cwd":project["path"],"claude_session_id":sid,"claude_config_dir":str(self.base / "native"),
            "created_at":f"2026-11-0{number}T00:00:00Z","run_ids":[]}
        self.app.workspaces.save_agent(record)
        path = self.base / "native/projects/exact" / (sid + ".jsonl")
        path.parent.mkdir(parents=True,exist_ok=True)
        events = [
            {"type":"user","uuid":f"u-{number}","message":{"content":f"OLD_USER_{number}"}},
            {"type":"assistant","uuid":f"a-{number}","message":{"id":"shared-id","content":[{"type":"text","text":f"OLD_ANSWER_{number}"}]}},
            {"type":"user","message":{"content":[{"type":"tool_result","content":"SECRET_TOOL_OUTPUT"}]}},
            {"type":"assistant","uuid":f"b-{number}","message":{"id":"shared-id","content":[{"type":"text","text":f"SECOND_PART_{number}"}]}},
            {"type":"assistant","isSidechain":True,"message":{"content":[{"type":"text","text":"SIDECHAIN"}]}},
            {"type":"user","isMeta":True,"message":{"content":"INTERNAL_CONTEXT"}},
        ]
        path.write_text("".join(json.dumps({"sessionId":sid,"timestamp":record["created_at"],**e})+"\n" for e in events))
        return record,path

    def test_open_stopped_history_without_primary_pointer_or_starting_process(self):
        old,path = self.historical_agent(1)
        original = path.read_bytes()
        with mock.patch.object(self.app,"start") as start:
            view = self.app.workspaces.chat_view(self.project["id"])
            start.assert_not_called()
        self.assertEqual([m["text"] for m in view["messages"]],["OLD_USER_1","OLD_ANSWER_1","SECOND_PART_1"])
        self.assertTrue(view["read_only"])
        self.assertTrue(view["can_resume"])
        self.assertIsNone(view["primary_agent"])
        self.assertEqual(view["agent"]["id"],old["id"])
        self.assertEqual(path.read_bytes(),original)
        self.assertFalse((self.app.state / "main-agents").exists())

    def test_history_selection_stays_in_workspace_and_observes_native_appends(self):
        old,path = self.historical_agent(1)
        newer,_ = self.historical_agent(2)
        view = self.app.workspaces.chat_view(self.project["id"])
        self.assertEqual(view["agent"]["id"],newer["id"])
        view = self.app.workspaces.chat_view(self.project["id"],agent_id=old["id"])
        self.assertEqual(view["messages"][0]["text"],"OLD_USER_1")
        with path.open("a") as file:
            file.write(json.dumps({"type":"assistant","uuid":"new-part","sessionId":old["claude_session_id"],
                "message":{"content":[{"type":"text","text":"APPENDED_RESULT"}]}})+"\n")
        self.assertEqual(self.app.workspaces.chat_view(self.project["id"],agent_id=old["id"])["messages"][-1]["text"],"APPENDED_RESULT")
        other = self.app.create_project({"name":"foreign", "goal":"another workspace"})
        foreign,_ = self.historical_agent(3,project=other)
        with self.assertRaisesRegex(WorkspaceError,"不属于"):
            self.app.workspaces.chat_view(self.project["id"],agent_id=foreign["id"])

    def test_explicit_legacy_resume_imports_history_once_without_replaying_old_user_turns(self):
        old,_ = self.historical_agent(1)
        with mock.patch.object(self.app.workspaces,"create_session",return_value={"id":"fixture"}):
            self.app.workspaces.resume(old["id"],{"execution_mode":"chat"})
            self.app.workspaces.resume(old["id"],{"execution_mode":"chat"})
        journal = ChatJournal(self.app.state,old["id"])
        self.assertEqual(len(journal.view()["messages"]),3)
        journal.begin("run","continue",resume=True)
        frames = journal.outgoing("run")
        self.assertEqual([f["message"]["content"] for f in frames],["continue"])

    def test_native_question_and_its_answer_remain_visible_but_other_tool_output_is_hidden(self):
        old,path = self.historical_agent(1)
        events = [
            {"type":"assistant","uuid":"question","message":{"content":[{"type":"tool_use","id":"q1","name":"AskUserQuestion","input":{"questions":[{"question":"OLD_QUESTION","options":[{"label":"A"}]}]}}]}},
            {"type":"user","uuid":"answer","message":{"content":[{"type":"tool_result","tool_use_id":"q1","content":"HUMAN_ANSWER_A"}]}},
        ]
        with path.open("a") as file:
            for event in events:
                file.write(json.dumps({"sessionId":old["claude_session_id"],**event})+"\n")
        view = self.app.workspaces.chat_view(self.project["id"])
        self.assertEqual([m["text"] for m in view["messages"][-2:]],["OLD_QUESTION\nA","HUMAN_ANSWER_A"])
        self.assertNotIn("SECRET_TOOL_OUTPUT",json.dumps(view))


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class ChatLifecycleTests(unittest.TestCase):
    setUp = workspace_fixture.NativeWorkspaceTests.setUp
    tearDown = workspace_fixture.NativeWorkspaceTests.tearDown
    until = workspace_fixture.NativeWorkspaceTests.until

    def start_chat(self):
        self.bin.write_text(CHAT_FIXTURE)
        run = self.app.workspaces.dispatch(self.pid, {"role":"commander", "execution_mode":"chat", "request_id":str(uuid.uuid4())})
        self.until(lambda: any(m["role"] == "assistant" for m in self.app.workspaces.chat_view(self.pid)["messages"]))
        return run

    def test_resume_updates_and_preserves_permission_setting(self):
        run = self.app.workspaces.dispatch(self.pid, {"role": "commander"})
        self.until(lambda: "NATIVE_READY" in self.app.log(run["id"])["text"])
        aid = run["agent_id"]
        self.app.workspaces.stop_agent(aid)
        enabled = self.app.workspaces.dispatch(self.pid, {"role": "commander", "skip_permissions": True})
        self.until(lambda: "NATIVE_READY" in self.app.log(enabled["id"])["text"])
        self.assertEqual(enabled["claude_session_id"], run["claude_session_id"])
        self.assertIn("--dangerously-skip-permissions", enabled["command"])
        self.assertTrue(self.app.workspaces.agent(aid)["skip_permissions"])
        calls = [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]
        self.assertIn("[Sapper 指挥官职责 · commander]", calls[-1]["argv"][-1])
        self.assertIn("不得亲自查询比赛平台", calls[-1]["argv"][-1])
        self.assertIn("原任务：", calls[-1]["argv"][-1])
        self.app.workspaces.stop_agent(aid)
        retained = self.app.workspaces.resume(aid, {})
        self.until(lambda: "NATIVE_READY" in self.app.log(retained["id"])["text"])
        self.assertIn("--dangerously-skip-permissions", retained["command"])
        self.app.workspaces.stop_agent(aid)
        disabled = self.app.workspaces.resume(aid, {"skip_permissions": False})
        self.until(lambda: "NATIVE_READY" in self.app.log(disabled["id"])["text"])
        self.assertNotIn("--dangerously-skip-permissions", disabled["command"])
        self.assertFalse(self.app.workspaces.agent(aid)["skip_permissions"])

    def test_native_question_answer_and_restart_preserve_one_main_and_watchdog(self):
        run = self.start_chat()
        main = run["agent_id"]
        self.assertEqual(len(self.app.workspaces.services(self.pid)), 1)
        again = self.app.workspaces.dispatch(self.pid, {"role":"commander", "execution_mode":"chat"})
        self.assertEqual(again["id"], run["id"])
        basis = self.app.store(self.pid).add_fact("supplied file", "user")["id"]
        child = self.app.workspaces.create_session(self.pid, {"parent_agent_id":main, "from":basis, "mission":"independent task"})
        self.app.workspaces.chat_message(self.pid, {"text":"ask"})
        self.until(lambda: self.app.workspaces.chat_view(self.pid)["requests"])
        reloaded = Console(self.app.root,self.app.state,self.socket,claude_bin=str(self.bin))
        self.assertEqual(reloaded.workspaces.chat_view(self.pid)["requests"][0]["id"], "decision-1")
        self.assertEqual(self.app.workspaces.agent(child["agent_id"])["desired_state"], "running")
        self.app.workspaces.chat_message(self.pid, {"question_id":"decision-1", "answers":{"采用哪条路径？":"路径 B"}})
        self.until(lambda: any(m["role"] == "assistant" and "回答已接收" in m["text"] and "路径 B" in m["text"] for m in self.app.workspaces.chat_view(self.pid)["messages"]))
        self.assertEqual(self.app.run_info(child["id"])["status"], "running")
        self.app.workspaces.stop_agent(main)
        self.assertTrue(all(s["status"] == "stopped" for s in self.app.workspaces.services(self.pid)))
        resumed = self.app.workspaces.dispatch(self.pid, {"role":"commander", "execution_mode":"chat"})
        self.assertEqual(resumed["claude_session_id"], run["claude_session_id"])
        self.assertEqual(resumed["agent_id"], main)
        launch = json.loads((self.app.state / "runs" / resumed["id"] / "launch.json").read_text())
        self.assertIn("[Sapper 指挥官职责 · commander]", launch["initial_prompt"])
        self.assertIn("不得亲自查询比赛平台", launch["initial_prompt"])
        self.assertIn("原任务：", launch["initial_prompt"])
        self.until(lambda: self.app.workspaces.chat_view(self.pid)["phase"] == "idle")
        self.assertEqual(sum(s["status"] in ("starting","running") for s in self.app.workspaces.services(self.pid)), 1)
        with self.assertRaisesRegex(Exception, "问题已结束"):
            self.app.workspaces.chat_message(self.pid, {"question_id":"decision-1", "answers":{"采用哪条路径？":"路径 A"}})

    def test_permission_is_explicit_and_watchdog_failure_stops_its_main(self):
        run = self.start_chat()
        self.app.workspaces.chat_message(self.pid, {"text":"permission"})
        self.until(lambda: self.app.workspaces.chat_view(self.pid)["requests"])
        self.app.workspaces.chat_message(self.pid, {"question_id":"permission-1", "allow":False})
        self.until(lambda: "deny" in self.app.workspaces.chat_view(self.pid)["messages"][-1]["text"])
        service = self.app.workspaces.services(self.pid)[0]
        self.app._stop(service["run_id"])  # Simulate losing only the companion.
        self.until(lambda: self.app.run_info(run["id"])["status"] == "stopped", timeout=16)
        self.assertEqual(self.app.workspaces.agent(run["agent_id"])["desired_state"], "stopped")

    def test_terminal_dispatch_reuses_identity_and_keeps_children_running(self):
        previous = self.start_chat()
        basis = self.app.store(self.pid).add_fact("supplied file", "user")["id"]
        child = self.app.workspaces.create_session(self.pid, {"parent_agent_id":previous["agent_id"], "from":basis, "mission":"continue independently"})
        run = self.app.workspaces.dispatch(self.pid, {"role":"commander"})
        self.until(lambda: "NATIVE_READY" in self.app.log(run["id"])["text"])
        self.assertEqual(run["execution_mode"], "tui")
        self.assertEqual(run["agent_id"], previous["agent_id"])
        self.assertEqual(run["claude_session_id"], previous["claude_session_id"])
        self.assertEqual(self.app.run_info(child["id"])["status"], "running")
        self.assertEqual(sum(s["status"] in ("starting", "running") for s in self.app.workspaces.services(self.pid)), 1)
        again = self.app.workspaces.dispatch(self.pid, {"role":"worker", "mission":"inspect another input"})
        self.assertEqual(again["id"], run["id"])
        self.until(lambda: "inspect another input" in self.app.log(run["id"])["text"])
        self.assertEqual(len(self.app.workspaces.agents(self.pid)), 2)

    def test_failed_watchdog_launch_rolls_back_main_process(self):
        self.bin.write_text(CHAT_FIXTURE)
        with mock.patch.object(self.app.workspaces, "start_watchdog", side_effect=WorkspaceError("fixture watchdog failure")):
            with self.assertRaisesRegex(WorkspaceError, "watchdog failure"):
                self.app.workspaces.dispatch(self.pid, {"role":"commander", "execution_mode":"chat"})
        main = self.app.workspaces.main_agent(self.pid)
        self.assertEqual(main["desired_state"], "stopped")
        self.assertFalse(main["pane_id"])

    def test_workspaces_and_command_relationships_reject_foreign_targets(self):
        run = self.start_chat()
        other = self.app.create_project({"name":"other", "goal":"other project"})
        self.assertIsNone(self.app.workspaces.chat_view(other["id"])["agent"])
        with self.assertRaisesRegex(WorkspaceError, "先派发"):
            self.app.workspaces.chat_message(other["id"], {"text":"foreign message"})
        with self.assertRaisesRegex(WorkspaceError, "当前工作区"):
            self.app.workspaces.create_session(self.pid, {"role":"worker", "workspace_scope":[self.pid,other["id"]]})
        self.app.workspaces.stop_workspace(other["id"])
        self.assertEqual(self.app.run_info(run["id"])["status"], "running")
