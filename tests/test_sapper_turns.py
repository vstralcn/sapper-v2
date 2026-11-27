"""Stage three: structured turn-stream persistence contract."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from sapper_turns import TurnStream, iter_frames, read_events, sed_lines


def assistant(content, mid="a1", usage=None, uuid=None):
    message = {"id": mid, "content": content}
    if usage is not None:
        message["usage"] = usage
    event = {"type": "assistant", "message": message}
    if uuid:
        event["uuid"] = uuid
    return json.dumps(event, ensure_ascii=False).encode("utf-8")


def tool_result(call_id, content, is_error=False, mid="u1"):
    return json.dumps({"type": "user", "uuid": mid, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}
    ]}}, ensure_ascii=False).encode("utf-8")


class TurnStreamTests(unittest.TestCase):
    def events(self, directory):
        return read_events(Path(directory) / "turns.jsonl", 0, 1000)["events"]

    def test_ledger_admission_is_graded_and_payload_is_untruncated(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            big = "保留完整载荷" * 2000
            self.assertTrue(stream.ingest(assistant([{"type": "text", "text": big}])))
            for noise in (b"not-json", b"[]", b'{"type": 7}'):
                self.assertFalse(stream.ingest(noise))
            frames = (directory / "frames.jsonl").read_text().splitlines()
            self.assertEqual(len(frames), 1)
            self.assertIn(big, frames[0])
            diagnostics = [json.loads(line) for line in (directory / "diagnostics.jsonl").read_text().splitlines()]
            self.assertEqual([d["kind"] for d in diagnostics],
                             ["invalid_json", "not_a_protocol_frame", "not_a_protocol_frame"])
            self.assertFalse((directory / "activity.log").exists())

    def test_one_frame_projects_to_six_enveloped_events_and_non_dialogue_stays_in_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([
                {"type": "thinking", "thinking": "内部推理完整保留"},
                {"type": "text", "text": "给用户的正文"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.txt"}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
            ], mid="msg-7"))
            events = self.events(directory)
            self.assertEqual([e["type"] for e in events], [
                "assistant_thinking", "assistant_text", "tool_call", "tool_call"])
            self.assertTrue(any("内部推理完整保留" in e["text"] for e in events))
            for index, event in enumerate(events, 1):
                self.assertEqual(event["schema_version"], 1)
                self.assertEqual(event["seq"], index)
                self.assertEqual(event["source_frame_seq"], 1)
                self.assertEqual(event["turn_id"], 1)
                self.assertEqual(event["message_id"], "msg-7")
                self.assertTrue(event["timestamp"])
            for frame in ({"type": "system", "subtype": "init"},
                          {"type": "stream_event", "event": {"delta": {"text": "partial"}}},
                          {"type": "control_request", "request_id": "1"}):
                stream.ingest(json.dumps(frame).encode())
            self.assertEqual(len(self.events(directory)), 4)
            self.assertEqual(len((directory / "frames.jsonl").read_text().splitlines()), 4)

    def test_mixed_user_frame_splits_and_tool_result_never_enters_user_input(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
            ]))
            mixed = json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "第二结果"},
                {"type": "text", "text": "这是真实用户输入"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "第一结果", "is_error": True},
            ]}}, ensure_ascii=False).encode()
            stream.ingest(mixed)
            events = self.events(directory)
            results = [e for e in events if e["type"] == "tool_result"]
            users = [e for e in events if e["type"] == "user_input"]
            self.assertEqual([e["call_id"] for e in results], ["t2", "t1"])
            self.assertEqual([e["status"] for e in results], ["ok", "error"])
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["text"], "这是真实用户输入")
            self.assertNotIn("call_id", users[0])
            self.assertNotIn("第二结果", json.dumps(users, ensure_ascii=False))

    def test_normal_settlement_uses_authoritative_usage_and_closes_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([{"type": "text", "text": "hi"}],
                                    usage={"input_tokens": 10, "output_tokens": 2}))
            stream.ingest(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                      "result": "全部完成", "usage": {"input_tokens": 99, "output_tokens": 7}}).encode())
            settlement = self.events(directory)[-1]
            self.assertEqual(settlement["type"], "turn_settlement")
            self.assertEqual(settlement["terminal_state"], "completed")
            self.assertTrue(settlement["authoritative"])
            self.assertEqual(settlement["usage"], {"input_tokens": 99, "output_tokens": 7})
            self.assertEqual(settlement["result_preview"], "全部完成")
            self.assertTrue(stream.settled)
            self.assertFalse(stream.settle("crashed"))

    def test_abnormal_settlement_dedups_usage_by_message_id(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([{"type": "text", "text": "一"}], mid="m1",
                                    usage={"input_tokens": 10, "output_tokens": 1}))
            stream.ingest(assistant([{"type": "text", "text": "二"}], mid="m1",
                                    usage={"input_tokens": 20, "output_tokens": 2}))
            stream.ingest(assistant([{"type": "text", "text": "三"}], mid="m2",
                                    usage={"input_tokens": 5, "output_tokens": 3}))
            self.assertTrue(stream.settle("crashed"))
            settlement = self.events(directory)[-1]
            self.assertEqual(settlement["terminal_state"], "crashed")
            self.assertFalse(settlement["authoritative"])
            self.assertEqual(settlement["usage"], {"input_tokens": 25, "output_tokens": 5})
            self.assertEqual(len([e for e in self.events(directory) if e["type"] == "turn_settlement"]), 1)

    def test_large_payload_three_elements_utf8_and_sed_line_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory, bytes_threshold=10, lines_threshold=1000, preview_chars=4)
            text = "第一行🚀\n第二行\n末行无换行"
            stream.ingest(tool_result("t1", [{"type": "text", "text": text}]))
            event = self.events(directory)[0]
            self.assertNotIn("text", event)
            for key in ("head_preview", "tail_preview", "total_lines", "total_bytes", "attachment_path"):
                self.assertIn(key, event)
            self.assertEqual(event["head_preview"], text[:4])
            self.assertEqual(event["tail_preview"], text[-4:])
            for preview in (event["head_preview"], event["tail_preview"]):
                self.assertNotIn("\ufffd", preview)
                self.assertEqual(preview.encode("utf-8").decode("utf-8"), preview)
            self.assertEqual(event["total_lines"], sed_lines(text))
            self.assertEqual(event["total_lines"], 3)
            self.assertEqual(event["total_bytes"], len(text.encode("utf-8")))
            path = Path(event["attachment_path"])
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            attachment = directory / path
            self.assertEqual(attachment.read_text(encoding="utf-8"), text)
            self.assertEqual(attachment.stat().st_mode & 0o777, 0o400)
            self.assertEqual(attachment.read_text().split("\n")[:event["total_lines"]], text.split("\n"))

    def test_small_payload_stays_inline_and_sed_lines_match_empty_and_trailing_newline(self):
        self.assertEqual(sed_lines(""), 0)
        self.assertEqual(sed_lines("a\n"), 1)
        self.assertEqual(sed_lines("a\nb"), 2)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory, bytes_threshold=1000, lines_threshold=1000)
            stream.ingest(tool_result("t1", [{"type": "text", "text": "短结果"}]))
            event = self.events(directory)[0]
            self.assertEqual(event["text"], "短结果")
            self.assertEqual(event["total_lines"], 1)
            self.assertFalse(list((directory / "attachments").glob("*.txt")))

    def test_binary_result_keeps_only_placeholder_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory, bytes_threshold=1)
            payload = json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [
                    {"type": "text", "text": "说明"},
                    {"type": "image", "media_type": "image/png", "data": "QUJDRA=="},
                ]}
            ]}}, ensure_ascii=False).encode()
            stream.ingest(payload)
            event = self.events(directory)[0]
            self.assertEqual(event["media"][0]["media_type"], "image/png")
            self.assertEqual(event["media"][0]["source_frame_seq"], 1)
            self.assertNotIn("QUJDRA==", json.dumps(event, ensure_ascii=False))
            for attachment in (directory / "attachments").iterdir():
                self.assertNotIn("QUJDRA==", attachment.read_text())
            self.assertIn("QUJDRA==", (directory / "frames.jsonl").read_text())

    def test_crash_fragments_are_truncated_and_sequences_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([{"type": "text", "text": "完整一"}]))
            with (directory / "frames.jsonl").open("ab") as handle:
                handle.write(b'{"type":"assistant","mess')
            with (directory / "turns.jsonl").open("ab") as handle:
                handle.write(b'{"schema_version":1,"seq":2')
            resumed = TurnStream(directory)
            self.assertEqual(resumed.frame_seq, 1)
            self.assertEqual(resumed.seq, 1)
            diagnostics = [json.loads(line) for line in (directory / "diagnostics.jsonl").read_text().splitlines()]
            self.assertEqual(len(diagnostics), 2)
            self.assertTrue(all(d["kind"] == "crash_fragment" for d in diagnostics))
            resumed.ingest(assistant([{"type": "text", "text": "完整二"}], mid="a2"))
            events = self.events(directory)
            self.assertEqual([e["seq"] for e in events], [1, 2])
            self.assertEqual(events[-1]["source_frame_seq"], 2)

    def test_write_order_is_attachment_then_ledger_then_events(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            order = []
            original_append = TurnStream._append_bytes
            original_replace = os.replace

            def record_append(path, payload):
                order.append(Path(path).name)
                return original_append(path, payload)

            def record_replace(source, target):
                order.append("attachment")
                return original_replace(source, target)

            with mock.patch.object(TurnStream, "_append_bytes", staticmethod(record_append)), \
                 mock.patch("sapper_turns.os.replace", record_replace):
                TurnStream(directory, bytes_threshold=1).ingest(
                    tool_result("t1", [{"type": "text", "text": "超阈值"}]))
            self.assertEqual(order, ["attachment", "frames.jsonl", "turns.jsonl"])

    def test_iter_frames_accepts_huge_single_frame_and_isolates_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            huge = json.dumps({"type": "system", "subtype": "init", "blob": "z" * 200000})
            source = io.BytesIO((huge + "\n[claude-code:noise]\n").encode())
            lines = list(iter_frames(source, chunk_size=1024))
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].endswith(b"\n"))
            stream = TurnStream(directory)
            self.assertTrue(stream.ingest(lines[0]))
            self.assertFalse(stream.ingest(lines[1]))
            self.assertEqual(len((directory / "frames.jsonl").read_text().splitlines()), 1)
            self.assertEqual(len((directory / "diagnostics.jsonl").read_text().splitlines()), 1)
            self.assertFalse(self.events(directory))

    def test_cursor_is_a_strict_open_interval_with_paging(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            for index in range(5):
                stream.ingest(assistant([{"type": "text", "text": f"t{index}"}], mid=f"a{index}"))
            first = stream.read(after_seq=0, limit=2)
            self.assertEqual([e["seq"] for e in first["events"]], [1, 2])
            self.assertEqual(first["next_after_seq"], 2)
            self.assertTrue(first["has_more"])
            second = stream.read(after_seq=2, limit=2)
            self.assertEqual([e["seq"] for e in second["events"]], [3, 4])
            last = stream.read(after_seq=4, limit=2)
            self.assertEqual([e["seq"] for e in last["events"]], [5])
            self.assertFalse(last["has_more"])
            empty = stream.read(after_seq=99)
            self.assertEqual(empty["events"], [])
            self.assertEqual(empty["next_after_seq"], 99)
            self.assertEqual(len(read_events(directory / "turns.jsonl", 0, 1000)["events"]), 5)

    def test_valid_frame_with_bad_field_type_is_admitted_and_consumer_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            bad_image = json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "t",
                "content": [{"type": "image", "data": 42}]}]}}).encode()
            self.assertTrue(stream.ingest(bad_image))
            self.assertTrue(stream.ingest(assistant([{"type": "text", "text": "继续"}], mid="a2")))
            self.assertEqual(len((directory / "frames.jsonl").read_text().splitlines()), 2)
            events = self.events(directory)
            self.assertEqual([e["type"] for e in events], ["tool_result", "assistant_text"])
            self.assertEqual(events[0]["media"][0]["byte_size"], 2)

    def test_projection_failure_is_diagnosed_without_losing_the_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            with mock.patch.object(TurnStream, "_plan", side_effect=RuntimeError("bad field")):
                self.assertTrue(stream.ingest(assistant([{"type": "text", "text": "x"}])))
            self.assertTrue(stream.ingest(assistant([{"type": "text", "text": "y"}], mid="a2")))
            self.assertEqual(len((directory / "frames.jsonl").read_text().splitlines()), 2)
            self.assertIn("projection_failed", (directory / "diagnostics.jsonl").read_text())
            self.assertEqual([e["text"] for e in self.events(directory)], ["y"])

    def test_stale_attachment_tmp_is_cleaned_and_does_not_block_spill(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stale = directory / "attachments" / "frame-1-deadbeef.txt.tmp"
            stale.parent.mkdir(parents=True)
            stale.write_text("leftover")
            stream = TurnStream(directory, bytes_threshold=1)
            self.assertFalse(stale.exists())
            self.assertIn("attachment_tmp_fragment", (directory / "diagnostics.jsonl").read_text())
            stream.ingest(tool_result("t1", [{"type": "text", "text": "x" * 200}]))
            self.assertTrue(any(p.suffix == ".txt" for p in stale.parent.iterdir()))

    def test_newline_terminated_invalid_tail_line_is_truncated_and_diagnosed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([{"type": "text", "text": "ok"}]))
            with (directory / "frames.jsonl").open("ab") as handle:
                handle.write(b"{broken json}\n")
            resumed = TurnStream(directory)
            self.assertEqual(resumed.frame_seq, 1)
            self.assertEqual(len((directory / "frames.jsonl").read_text().splitlines()), 1)
            self.assertIn("invalid_tail_line", (directory / "diagnostics.jsonl").read_text())
            resumed.ingest(assistant([{"type": "text", "text": "next"}], mid="a2"))
            self.assertEqual(self.events(directory)[-1]["source_frame_seq"], 2)

    def test_usage_snapshots_survive_takeover_for_abnormal_settlement(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([{"type": "text", "text": "a"}], mid="m1",
                                    usage={"input_tokens": 7, "output_tokens": 2}))
            resumed = TurnStream(directory)
            self.assertEqual(resumed.usage, {"m1": {"input_tokens": 7, "output_tokens": 2}})
            resumed.settle("crashed")
            settlement = self.events(directory)[-1]
            self.assertEqual(settlement["usage"], {"input_tokens": 7, "output_tokens": 2})
            self.assertEqual(settlement["terminal_state"], "crashed")

    def test_unprojected_ledger_frame_is_replayed_on_takeover(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([{"type": "text", "text": "first"}]))
            stream._append_bytes(stream.frames_path, (
                json.dumps({"type": "assistant", "message": {"id": "a2", "content": [
                    {"type": "text", "text": "lost"}]}}) + "\n").encode())
            resumed = TurnStream(directory)
            events = self.events(directory)
            self.assertEqual([e["text"] for e in events], ["first", "lost"])
            self.assertEqual([e["seq"] for e in events], [1, 2])
            self.assertEqual(events[-1]["source_frame_seq"], 2)

    def test_half_written_one_to_many_frame_is_completed_on_takeover(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory)
            stream.ingest(assistant([
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
                {"type": "tool_use", "id": "t3", "name": "Grep", "input": {}},
            ]))
            lines = (directory / "turns.jsonl").read_text().splitlines()
            (directory / "turns.jsonl").write_text(lines[0] + "\n")
            resumed = TurnStream(directory)
            calls = [e["call_id"] for e in self.events(directory) if e["type"] == "tool_call"]
            self.assertEqual(calls, ["t1", "t2", "t3"])

    def test_attachment_is_readonly_and_media_size_is_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory, bytes_threshold=1)
            stream.ingest(json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "t", "content": [
                    {"type": "text", "text": "x" * 200},
                    {"type": "image", "media_type": "image/png", "data": "QUJDRA=="},
                ]}]}}, ensure_ascii=False).encode())
            event = self.events(directory)[0]
            self.assertEqual(event["media"][0]["byte_size"], 4)
            attachment = directory / event["attachment_path"]
            self.assertEqual(attachment.stat().st_mode & 0o777, 0o400)
            if os.geteuid() != 0:
                with self.assertRaises(PermissionError):
                    attachment.write_text("tamper")

    def test_private_files_and_directory_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            stream = TurnStream(directory, bytes_threshold=1)
            stream.ingest(tool_result("t1", [{"type": "text", "text": "x" * 100}]))
            self.assertEqual((directory / "attachments").stat().st_mode & 0o777, 0o700)
            for name in ("frames.jsonl", "turns.jsonl", "diagnostics.jsonl"):
                self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
            for attachment in (directory / "attachments").iterdir():
                self.assertEqual(attachment.stat().st_mode & 0o777, 0o400)


if __name__ == "__main__":
    unittest.main()
