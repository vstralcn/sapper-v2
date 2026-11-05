"""Metric correctness against independently specified transcripts and graph times."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analyze_agent_run.py"
spec = importlib.util.spec_from_file_location("agent_run_analysis", SCRIPT)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def at(second):
    return f"2026-11-01T00:00:{second:02d}Z"


def assistant(mid, second, blocks=None, usage=None, sid="s1", **extra):
    return {"type": "assistant", "sessionId": sid, "timestamp": at(second), "cwd": "/project",
            "message": {"id": mid, "role": "assistant", "model": "fixture",
                        "content": blocks or [], "usage": usage or {}}, **extra}


def tool(tid, command, name="Bash", **args):
    return {"type": "tool_use", "id": tid, "name": name, "input": {"command": command, **args}}


def result(tid, second, content="", error=False, meta=None, sid="s1"):
    return {"type": "user", "sessionId": sid, "timestamp": at(second), "toolUseResult": meta,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content, "is_error": error}]}}


def feed(rows, **kwargs):
    a = analysis.Analyzer(**kwargs)
    for line, row in enumerate(rows, 1):
        a.event(row, "/tmp/fixture.jsonl", line)
    return a


class AnalysisTests(unittest.TestCase):
    def test_discovers_workspace_sessions_and_legacy_transcripts_without_prefix_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "workspace"
            native = project / ".sapper/sessions/session/claude/projects/encoded/session.jsonl"
            native.parent.mkdir(parents=True)
            native.write_text(json.dumps({"cwd": str(project), "type": "user"}) + "\n")
            legacy_root = root / "legacy"
            encoded = analysis.re.sub(r"[^a-zA-Z0-9]", "-", str(project.resolve()))
            legacy = legacy_root / encoded / "old.jsonl"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"cwd": str(project), "type": "user"}) + "\n")
            collision = legacy_root / (encoded + "-other") / "wrong.jsonl"
            collision.parent.mkdir()
            collision.write_text(json.dumps({"cwd": str(root / "workspace-other"), "type": "user"}) + "\n")
            self.assertEqual(set(analysis.discover(project, legacy_root)), {native, legacy})

    def graph(self, root):
        path = Path(root) / ".sapper"
        path.mkdir()
        (path / "project.json").write_text('{}')
        nodes = {
            "facts": [
                {"id": "goal", "created_at": at(0)},
                {"id": "f001", "created_at": at(1), "doc": "seed evidence"},
                {"id": "f002", "created_at": at(12), "doc": "new evidence"},
                {"id": "f003", "created_at": at(17), "kind": "flag", "data": {"status": "submitted"}},
            ],
            "hints": [{"id": "h001", "created_at": at(5)}],
            "intents": [{"id": "i001", "created_at": at(5), "concluded_at": at(15), "to": "f002"},
                        {"id": "i002", "created_at": at(10), "to": None, "status": "claimed"}],
        }
        for kind, rows in nodes.items():
            (path / kind).mkdir()
            for row in rows:
                (path / kind / (row["id"] + ".json")).write_text(json.dumps(row))
        return analysis.Graph(path)

    def test_message_blocks_cache_usage_and_replayed_rows_count_once(self):
        first = assistant("m1", 10, [tool("t1", "pwd")], {"input_tokens": 100, "cache_read_input_tokens": 700,
                         "cache_creation_input_tokens": 200, "output_tokens": 2}, uuid="a1")
        a = feed([first, first, assistant("m1", 11, [tool("t2", "ls")],
                       {"input_tokens": 100, "cache_read_input_tokens": 700,
                        "cache_creation_input_tokens": 200, "output_tokens": 9}), result("t1", 12),
                  assistant("m2", 14), {"type": "result", "usage": {"input_tokens": 123456}}])
        r = a.finish()
        self.assertEqual(r["summary"]["steps"], 2)
        self.assertEqual(r["summary"]["tool_calls"], 2)
        self.assertEqual(r["steps"][0]["total_input_tokens"], 1000)
        self.assertEqual(r["steps"][0]["output_tokens"], 9)
        self.assertIsNone(r["steps"][1]["total_input_tokens"])
        self.assertEqual(r["summary"]["total_input_tokens_known"], 1000)

    def test_reconstruct_request_keep_tail_and_compaction_not_cumulative(self):
        hook = "[fact-graph] current state of the working memory:\ncounts: 3 facts, 2 open / 1 done intents, 4 hints\nso the NEXT turn can recall it."
        rows = [
            {"type": "api-request-shape", "sessionId": "s1", "shapeHash": "shape", "shape": {}},
            {"type": "api-request-blob", "sessionId": "s1", "hash": "h", "message": {"role": "user", "content": [{"type": "text", "text": hook}]}},
            {"type": "api-request-blob", "sessionId": "s1", "hash": "plain", "message": {"role": "user", "content": "hello"}},
        ]
        for i, (keep, tail) in enumerate(((0, ["h"]), (1, ["plain"]), (0, ["plain"]))):
            rows.extend([{"type": "api-request", "sessionId": "s1", "id": str(i), "keep": keep, "tail": tail, "shapeHash": "shape", "timestamp": at(10 + i)},
                         assistant("m" + str(i), 10 + i, requestRef=str(i), usage={"input_tokens": 500})])
        r = feed(rows).finish()
        sizes = [s["graph_hook_bytes"] for s in r["steps"]]
        self.assertEqual(sizes, [len(hook.encode()), len(hook.encode()), 0])
        self.assertAlmostEqual(r["steps"][0]["graph_hook_share_estimate"], (len(hook.encode()) + 3) // 4 / 500)

    def test_missing_request_blob_not_zero_and_hook_boundaries(self):
        rows = [{"type": "api-request", "sessionId": "s1", "id": "r", "keep": 0, "tail": ["missing"]},
                assistant("m", 1, requestRef="r")]
        r = feed(rows).finish()
        self.assertIsNone(r["steps"][0]["graph_hook_bytes"])
        self.assertEqual(r["steps"][0]["graph_context_evidence"], "incomplete_request_snapshot")
        text = "A fact-graph working memory exists in this project abc\nUserPromptSubmit hook additional context: unrelated\n[fact-graph] current state of the working memory: xyz so the NEXT turn can recall it.\nToday's date is extra"
        parts = list(analysis.graph_parts(text))
        self.assertEqual(len(parts), 2)
        self.assertNotIn("unrelated", "".join(parts))
        self.assertNotIn("extra", "".join(parts))

    def test_native_omitted_blobs_system_hook_merged_results_and_explicit_base(self):
        hook = "[fact-graph] current state of the working memory: fixture"
        blocks = [tool("t1", "pwd"), tool("t2", "ls")]
        first_result = result("t1", 12, "one")
        second_result = result("t2", 13, "two")
        ahash = analysis.digest(analysis.canonical({"role": "assistant", "content": blocks}))
        uhash = analysis.digest(analysis.canonical({"role": "user", "content": first_result["message"]["content"] + second_result["message"]["content"]}))
        rows = [
            {"type": "api-request-shape", "sessionId": "s1", "shapeHash": "shape", "shape": {}},
            {"type": "api-request-blob", "sessionId": "s1", "hash": "h", "message": {"role": "system", "content": [{"type": "text", "text": hook}]}},
            {"type": "api-request", "sessionId": "s1", "id": "r1", "keep": 0, "tail": ["h"], "shapeHash": "shape"},
            assistant("m1", 10, [blocks[0]], apiBlockIndex=0, requestRef="r1"),
            assistant("m1", 11, [blocks[1]], apiBlockIndex=1, requestRef="r1"),
            first_result, second_result,
            {"type": "api-request", "sessionId": "s1", "id": "r2", "base": "r1", "keep": 1, "tail": [ahash, uhash], "shapeHash": "shape"},
            assistant("m2", 14, requestRef="r2"),
            {"type": "api-request", "sessionId": "s1", "id": "side", "keep": 0, "tail": [], "shapeHash": "shape"},
            {"type": "api-request", "sessionId": "s1", "id": "r3", "base": "r2", "keep": 3, "tail": [], "shapeHash": "shape"},
            assistant("m3", 15, requestRef="r3"),
        ]
        r = feed(rows).finish()
        self.assertEqual([s["graph_hook_bytes"] for s in r["steps"]], [len(hook)] * 3)
        self.assertEqual(r["summary"]["steps_with_graph_request_snapshot"], 3)

    def test_graph_tool_output_occupancy_separate_from_hook(self):
        response = result("t", 2, "counts: 2 facts, 1 open / 0 done intents, 0 hints")
        message_hash = analysis.digest(analysis.canonical(response["message"]))
        rows = [assistant("m1", 1, [tool("t", "sapper status")]), response,
                {"type": "api-request-shape", "sessionId": "s1", "shapeHash": "shape", "shape": {}},
                {"type": "api-request", "sessionId": "s1", "id": "r", "keep": 0, "tail": [message_hash], "shapeHash": "shape"},
                assistant("m2", 3, requestRef="r")]
        r = feed(rows).finish()
        self.assertEqual(r["steps"][1]["graph_hook_bytes"], 0)
        self.assertGreater(r["steps"][1]["graph_read_output_bytes"], 0)

    def test_tool_errors_timeouts_pending_and_false_positive_text(self):
        rows = [assistant("m1", 1, [tool("t1", "read"), tool("t2", "wait"), tool("t3", "timeout=20"),
                                   tool("t4", "pending"), tool("t5", "bad"), tool("t6", "slow")]),
                result("t1", 2, "README says: timeout and error are fields"),
                result("t2", 3, "Command timed out", error=True),
                result("t3", 4, "Still running", meta={"session_id": 123}),
                result("t5", 5, "Exit code: 2\nfailed"),
                result("t6", 6, meta={"timed_out": True}),
                {"type": "system", "sessionId": "s1", "subtype": "api_retry", "timestamp": at(7)}]
        r = feed(rows).finish()
        s = r["summary"]
        self.assertEqual((s["tool_calls"], s["tool_results"], s["tool_pending"]), (6, 5, 1))
        self.assertEqual((s["tool_errors"], s["tool_timeouts"]), (3, 2))
        self.assertEqual(s["tool_error_rate"], 3 / 5)
        self.assertEqual(s["tool_timeout_rate"], 2 / 5)
        self.assertEqual(r["sessions"][0]["api_retries"], 1)

    def test_duplicates_exact_near_workdir_and_description(self):
        rows = [assistant("m1", 1, [tool("t1", "curl -s http://local/a", description="a")]),
                assistant("m2", 2, [tool("t2", "curl -s http://local/a", description="b")]),
                assistant("m3", 3, [tool("t3", "curl -s http://local/b")]),
                assistant("m4", 4, [tool("t4", "curl -s http://local/a", cwd="/elsewhere")])]
        r = feed(rows).finish()
        self.assertTrue(any(d["tool_id"] == "t2" and d["match"] == "exact" for d in r["duplicates"]))
        self.assertTrue(any(d["tool_id"] == "t3" and d["match"] == "near" for d in r["duplicates"]))
        self.assertFalse(any(d["tool_id"] == "t4" for d in r["duplicates"]))

    def test_shared_graph_curve_and_attributed_milestones_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self.graph(tmp)
            self.assertEqual(g.counts(analysis.stamp(at(11))), {"facts": 2, "hints": 1, "open": 2, "done": 0})
            self.assertEqual(g.counts(analysis.stamp(at(18))), {"facts": 4, "hints": 1, "open": 1, "done": 1})
            rows = [assistant("m1", 10, [tool("t1", 'sapper fact "new"')]), result("t1", 12, "f002\n"),
                    assistant("m2", 16, [tool("t2", 'sapper flag "secret" --submit')]),
                    result("t2", 17, 'f003 recorded\nf003 submitted: {}'),
                    assistant("m3", 20), assistant("other", 11, sid="s2"), assistant("other2", 21, sid="s2")]
            r = feed(rows).finish(g)
            m = r["sessions"][0]["milestones"]
            self.assertEqual(m["first_fact"]["step"], 1)
            self.assertEqual(m["first_fact"]["elapsed_seconds"], 2)
            self.assertEqual(m["first_flag_recorded"]["step"], 2)
            self.assertEqual(m["first_flag_submitted"]["step"], 2)
            self.assertEqual(m["first_fact_with_doc_now"]["fact_id"], "f002")
            other = r["sessions"][1]["milestones"]
            self.assertIsNone(other["first_fact"])
            self.assertEqual(other["shared_project_during_session"]["first_fact"]["fact_id"], "f002")

    def test_goal_and_seed_and_plain_flag_mentions_not_achievements(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self.graph(tmp)
            r = feed([assistant("m", 10, [tool("t", "sapper graph")]),
                      result("t", 11, "f001\nflag{placeholder}\ncounts: 2 facts, 2 open / 0 done intents, 1 hints"),
                      assistant("n", 12)]).finish(g)
            m = r["sessions"][0]["milestones"]
            self.assertIsNone(m["first_fact"])
            self.assertIsNone(m["first_flag_recorded"])
            self.assertEqual(r["steps"][1]["observed_facts"], 2)
            self.assertEqual(r["steps"][1]["observed_age_seconds"], 1)

    def test_flag_first_output_before_recording_and_echo_not_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self.graph(tmp)
            next(n for n in g.nodes["facts"] if n["id"] == "f003")["data"]["value"] = "flag{known_value}"
            rows = [assistant("m1", 10, [tool("echo", "echo 'flag{known_value}'")]),
                    result("echo", 11, "flag{known_value}"),
                    assistant("m2", 12, [tool("solve", "python solve.py")]),
                    result("solve", 13, "flag{known_value}"),
                    assistant("m3", 16, [tool("record", "sapper flag 'flag{known_value}'")]),
                    result("record", 17, "f003 recorded")]
            r = feed(rows, graph=g).finish(g)
            m = r["sessions"][0]["milestones"]
            self.assertEqual(m["first_flag_observed"]["step"], 2)
            self.assertEqual(m["first_flag_recorded"]["step"], 3)
            self.assertNotIn("flag{known_value}", json.dumps(r))

    def test_unknown_conclusion_time_does_not_invent_done_curve(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self.graph(tmp)
            path = g.path / "intents/i001.json"
            node = json.loads(path.read_text())
            node.pop("concluded_at")
            path.write_text(json.dumps(node))
            g = analysis.Graph(g.path)
            self.assertIsNone(g.counts(analysis.stamp(at(20)))["done"])
            self.assertEqual(g.counts(analysis.stamp(at(2)))["done"], 0)

    def test_different_sessions_same_message_and_tool_ids_not_collapsed(self):
        r = feed([assistant("m", 1, [tool("t", "pwd")]), assistant("m", 2, [tool("t", "pwd")], sid="s2")]).finish()
        self.assertEqual(r["summary"]["sessions"], 2)
        self.assertEqual(r["summary"]["tool_calls"], 2)
        self.assertTrue(any(not d["same_session"] for d in r["duplicates"]))

    def test_cli_truncated_log_no_inputs_or_html_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            rows = [assistant("m", 1, [tool("t", "secret-credential")], sid="</script><script>bad()</script>"),
                    result("t", 2, sid="</script><script>bad()</script>")]
            path.write_text("\n".join(json.dumps(r) for r in rows) + '\n{"partial":')
            out = Path(tmp) / "report"
            proc = subprocess.run([sys.executable, str(SCRIPT), "--transcript", str(path), "--out", str(out)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["sources"][0]["invalid_json_lines"], 1)
            self.assertNotIn("secret-credential", (out / "report.json").read_text())
            self.assertNotIn("</script><script>bad", (out / "report.html").read_text())
            self.assertEqual((out / "report.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue((out / "steps.csv").is_file())

    def test_synthetic_error_and_nested_progress_do_not_create_model_steps(self):
        rows = [assistant("m", 1), {"type": "progress", "data": assistant("nested", 2)},
                assistant("error", 3, isApiErrorMessage=True)]
        self.assertEqual(feed(rows).finish()["summary"]["steps"], 1)


if __name__ == "__main__":
    unittest.main()
