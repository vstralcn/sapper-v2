import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import sapper
from sapper_evidence import ingest_artifacts, provenance


class FactEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = sapper.Store(self.project / ".sapper")
        self.store.init("test", "confirmed evidence")

    def tearDown(self):
        self.tmp.cleanup()

    def test_artifact_is_copied_under_sha256_and_fact_references_it(self):
        sample = self.project / "response.txt"
        sample.write_bytes(b"HTTP/1.1 200 OK\n")
        item = ingest_artifacts(self.store, ["response.txt"], cwd=self.project)[0]
        # The CLI passes the --store Path, while API callers may pass a Store instance.
        same_item = ingest_artifacts(self.store.root, ["response.txt"], cwd=self.project)[0]
        self.assertEqual(same_item["sha256"], item["sha256"])
        digest = hashlib.sha256(sample.read_bytes()).hexdigest()
        self.assertEqual(item["sha256"], digest)
        self.assertEqual((self.store.root / item["path"]).read_bytes(), sample.read_bytes())
        ev = provenance("capture from local lab", ["curl -i http://127.0.0.1/"], [item], self.project)
        fact = self.store.add_fact("endpoint returns 200", "tester", evidence=ev)
        self.assertEqual(fact["evidence"]["reproduction"][0]["command"], "curl -i http://127.0.0.1/")
        self.assertEqual(fact["evidence"]["artifacts"][0]["sha256"], digest)
        self.assertEqual(sapper.validate_graph(self.store), [])
        stored = self.store.root / item["path"]
        stored.write_bytes(b"tampered")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("hash mismatch" in error or "size mismatch" in error for error in errors))

    def test_artifacts_reject_outside_paths_symlinks_and_missing_files(self):
        outside = self.root / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inside current project"):
            ingest_artifacts(self.store, [str(outside)], cwd=self.project)
        link = self.project / "escape.txt"
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            ingest_artifacts(self.store, [str(link)], cwd=self.project)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ingest_artifacts(self.store, ["missing.txt"], cwd=self.project)

    def test_fact_evidence_schema_and_legacy_facts(self):
        old = self.store.add_fact("legacy", "tester")
        self.assertIsNone(old.get("evidence"))
        with self.assertRaisesRegex(sapper.FGError, "reproduction entry"):
            self.store.add_fact("bad", "tester", evidence={"reproduction": [{"argv": ["x"]}]})
        with self.assertRaisesRegex(sapper.FGError, "SHA-256"):
            self.store.add_fact("bad", "tester", evidence={"artifacts": [{"sha256": "no", "path": "artifacts/no", "size": 2}]})
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_done_atomically_records_evidence(self):
        seed = self.store.add_fact("initial service", "tester")
        intent = self.store.add_intent([seed["id"]], "verify response", "tester")
        self.store.claim_intent(intent["id"], "worker")
        fact, concluded = self.store.conclude_with_new_fact(
            intent["id"], "server returned 200", "worker",
            evidence={"source": "authorized local lab", "reproduction": [
                {"command": "curl -i http://127.0.0.1/", "cwd": str(self.project)}]},
        )
        self.assertEqual(concluded["to"], fact["id"])
        self.assertEqual(fact["evidence"]["source"], "authorized local lab")
        self.assertEqual(sapper.validate_graph(self.store), [])


if __name__ == "__main__":
    unittest.main()
