"""Native streaming observations are distinct from completion and progress."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_run import RunOutput
from sapper_web import private_json


class RunOutputTests(unittest.TestCase):
    def test_phases_track_pending_tools_questions_and_round_end(self):
        with tempfile.TemporaryDirectory() as temp:
            output = RunOutput(Path(temp), private_json)
            def emit(event, phase):
                output.observe(json.dumps(event))
                self.assertEqual(output.activity["phase"], phase)
            emit({"type":"system", "subtype":"init"}, "unknown")
            emit({"type":"assistant", "message":{"content":[{"type":"tool_use", "id":"read", "name":"Read"}]}}, "tool_running")
            emit({"type":"assistant", "message":{"content":[{"type":"tool_use", "id":"ask", "name":"AskUserQuestion"}]}}, "waiting_user")
            emit({"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"ask"}]}}, "tool_running")
            emit({"type":"user", "message":{"content":[{"type":"tool_result", "tool_use_id":"read"}]}}, "responding")
            emit({"type":"assistant", "message":{"stop_reason":"end_turn", "content":[{"type":"text", "text":"done"}]}}, "idle")

    def test_tool_events_retries_text_and_final_error_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output = RunOutput(directory, private_json)
            events = [
                {"type": "system", "subtype": "init"},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "题目.txt"}},
                    {"type": "text", "text": "准备核验"}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "r1", "is_error": True}]}},
                {"type": "system", "subtype": "api_retry", "error": "overloaded", "attempt": 2},
                {"type": "result", "is_error": True, "subtype": "error_max_turns", "result": "未完成"},
            ]
            for event in events:
                output.observe(json.dumps(event, ensure_ascii=False).encode())
            activity = json.loads((directory / "activity.json").read_text())
            self.assertEqual(activity["events"], 5)
            self.assertEqual(activity["last_tool"], "Read")
            self.assertTrue(activity["result_received"])
            self.assertTrue(activity["result_is_error"])
            log = (directory / "activity.log").read_text()
            for expected in ("题目.txt", "工具错误 Read", "overloaded", "准备核验", "未完成"):
                self.assertIn(expected, log)

    def test_plain_warnings_and_sparse_events_do_not_manufacture_a_result(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output = RunOutput(directory, private_json)
            output.observe(b"[claude-code:unrecognized_model] fixture\n")
            output.observe(b'{"type":"assistant","message":{"content":"unexpected"}}')
            output.observe(b'{"type":"future_event"}')
            activity = json.loads((directory / "activity.json").read_text())
            self.assertFalse(activity["result_received"])
            self.assertNotIn("latest_message", activity)
            self.assertIn("unrecognized_model", (directory / "activity.log").read_text())


if __name__ == "__main__":
    unittest.main()
