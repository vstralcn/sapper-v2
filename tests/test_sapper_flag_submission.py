"""Flag submission policy and lifecycle, with no model or remote platform calls."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper


class FlagSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.temp.name) / ".sapper")
        self.store.init("test", "capture fixture flag")
        self.client = mock.Mock()
        self.client.submit_flag.return_value = {"correct": True, "awarded": 100}
        self.loader = mock.patch.object(sapper, "_arena_client", return_value=self.client)
        self.loader.start()

    def tearDown(self):
        self.loader.stop()
        self.temp.cleanup()

    def enable(self, enabled=True):
        with self.store.locked():
            project = self.store.load_project()
            project["auto_submit_flags"] = enabled
            self.store.save_project(project)

    def run_flag(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sapper.main(["--store", str(self.store.root), "flag", *argv])

    def flag(self):
        return next(f for f in self.store.list_facts() if f.get("kind") == "flag")

    def test_default_off_records_without_network(self):
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 0)
        self.client.submit_flag.assert_not_called()
        self.assertEqual(self.flag()["data"]["status"], "recorded")

    def test_enabled_records_then_submits_and_saves_business_reply(self):
        self.enable()
        def submit(code, value):
            self.assertEqual(self.flag()["data"]["status"], "recorded")
            self.assertEqual(self.flag()["data"]["submission_uncertain"], "true")
            return {"correct": True, "awarded": 100}
        self.client.submit_flag.side_effect = submit
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 0)
        self.client.submit_flag.assert_called_once_with("c01", "fixture-flag")
        self.assertEqual(self.flag()["data"]["status"], "submitted")
        self.assertIn('"correct": true', self.flag()["data"]["result"])
        self.assertNotIn("submission_uncertain", self.flag()["data"])

    def test_per_command_override_in_both_directions(self):
        self.assertEqual(self.run_flag("explicit", "-c", "c01", "--submit"), 0)
        self.enable()
        self.assertEqual(self.run_flag("capture-only", "-c", "c01", "--no-submit"), 0)
        self.client.submit_flag.assert_called_once_with("c01", "explicit")
        self.enable(False)
        self.assertEqual(self.run_flag("after-disable", "-c", "c01"), 0)
        self.assertEqual(self.client.submit_flag.call_count, 1)

    def test_submit_preexisting_by_id_and_deduplicate_accepted_flag(self):
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 0)
        fid = self.flag()["id"]
        self.enable()
        self.assertEqual(self.run_flag("--id", fid), 0)
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 0)
        self.assertEqual(self.run_flag("--id", fid, "--submit"), 0)
        self.client.submit_flag.assert_called_once()
        self.assertEqual(len(self.store.list_facts()), 2)

    def test_preexisting_value_can_be_explicitly_submitted_when_off(self):
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 0)
        self.assertEqual(self.run_flag("fixture-flag", "--submit"), 0)
        self.client.submit_flag.assert_called_once_with("c01", "fixture-flag")

    def test_rejected_flag_does_not_automatically_retry(self):
        self.enable()
        self.client.submit_flag.return_value = {"status": 200, "correct": False}
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 1)
        self.assertEqual(self.flag()["data"]["status"], "rejected")
        self.assertEqual(self.run_flag("--id", self.flag()["id"]), 1)
        self.client.submit_flag.assert_called_once()
        self.client.submit_flag.return_value = {"correct": True}
        self.assertEqual(self.run_flag("--id", self.flag()["id"], "--submit"), 0)
        self.assertEqual(self.client.submit_flag.call_count, 2)

    def test_transport_failure_and_unrecognized_reply_stay_uncertain(self):
        self.enable()
        self.client.submit_flag.side_effect = TimeoutError("fixture timeout")
        self.assertEqual(self.run_flag("fixture-flag", "-c", "c01"), 1)
        self.assertEqual(self.flag()["data"]["status"], "recorded")
        self.assertEqual(self.run_flag("--id", self.flag()["id"]), 1)
        self.client.submit_flag.assert_called_once()
        self.client.submit_flag.side_effect = None
        self.client.submit_flag.return_value = {"status": 200}
        self.assertEqual(self.run_flag("--id", self.flag()["id"], "--submit"), 1)
        self.assertEqual(self.flag()["data"]["status"], "recorded")
        self.assertEqual(self.flag()["data"]["submission_uncertain"], "true")

    def test_missing_config_or_challenge_preserves_capture(self):
        self.enable()
        self.assertEqual(self.run_flag("fixture-flag"), 1)
        self.client.submit_flag.assert_not_called()
        with mock.patch.object(sapper, "_arena_client", side_effect=sapper.FGError("missing platform config")):
            self.assertEqual(self.run_flag("--id", self.flag()["id"], "-c", "c01"), 1)
        self.assertEqual(self.flag()["data"]["status"], "recorded")
        self.assertNotIn("submission_uncertain", self.flag()["data"])

    def test_challenge_mismatch_and_non_flag_id_do_not_submit(self):
        self.run_flag("fixture-flag", "-c", "c01")
        self.assertEqual(self.run_flag("--id", self.flag()["id"], "-c", "different", "--submit"), 1)
        self.assertEqual(self.run_flag("--id", "goal", "--submit"), 1)
        self.assertEqual(self.run_flag("fixture-flag", "--id", self.flag()["id"], "--submit"), 1)
        self.client.submit_flag.assert_not_called()

    def test_two_workers_capture_and_submit_once(self):
        self.enable()
        def submit(*_):
            time.sleep(0.03)  # Force overlap while the first worker awaits a platform response.
            return {"correct": True}
        self.client.submit_flag.side_effect = submit
        args = sapper.build_parser().parse_args(["--store", str(self.store.root), "flag", "fixture-flag", "-c", "c01"])
        with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: sapper.cmd_flag(args), range(2)))
        self.assertEqual(results, [0, 0])
        self.client.submit_flag.assert_called_once()
        self.assertEqual(len(self.store.list_facts()), 2)


if __name__ == "__main__":
    unittest.main()
