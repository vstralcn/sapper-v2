import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import sapper
import sapper_family


class FamilyPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = sapper.Store(self.root / ".sapper")
        self.store.init("test", "solve the authorized lab")
        self.seed = self.store.add_fact("authorized lab material is available", "tester")
        with self.store.locked():
            sapper_family.create(self.store, "parser", "Parser state machine",
                "length validation bypass", "valid_inputs", 8, 10.0, 2, "higher", "tester")

    def tearDown(self):
        self.tmp.cleanup()

    def make_intent(self, key="parser", label="test one hypothesis"):
        return self.store.add_intent([self.seed["id"]], label, "tester", family=key)

    def finish_metric(self, intent, value, metric_name="valid_inputs"):
        self.store.claim_intent(intent["id"], "worker")
        return self.store.conclude_with_new_fact(intent["id"], "measured metric", "worker",
            kind="note", data={"metric": value, "metric_name": metric_name},
            evidence={"source": "reproducible local fixture"})

    def test_intent_claim_and_verified_metric_are_accounted(self):
        intent = self.make_intent()
        family = sapper_family.get(self.store, "parser")
        self.assertEqual(len(family["attempts"]), 1)
        self.assertEqual(family["attempts"][0]["state"], "open")
        fact, _ = self.finish_metric(intent, 3)
        family = sapper_family.get(self.store, "parser")
        self.assertEqual(family["metric"], 3.0)
        self.assertEqual(family["metric_fact"], fact["id"])
        self.assertEqual(family["state"], "promising")
        self.assertEqual(family["attempts"][0]["state"], "done")
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_wrong_metric_name_rejected_before_fact_commit(self):
        intent = self.store.add_intent([self.seed["id"]], "bad metric", "tester", family="parser")
        self.store.claim_intent(intent["id"], "worker")
        with self.assertRaisesRegex(sapper.FGError, "metric must be named"):
            self.store.conclude_with_new_fact(intent["id"], "wrong metric", "worker",
                data={"metric": 3, "metric_name": "other"},
                evidence={"source": "test"})
        self.assertIsNone(self.store.get_intent(intent["id"])["to"])
        self.assertFalse(any(f.get("source_intent") == intent["id"] for f in self.store.list_facts()))

    def test_stall_stop_loss_and_explicit_resume(self):
        baseline = self.store.add_fact("baseline", "tester", kind="note",
            data={"metric": 5, "metric_name": "valid_inputs"},
            evidence={"source": "local fixture"})
        with self.store.locked():
            sapper_family.set_metric(self.store, "parser", baseline["id"], "tester")
        for n in range(2):
            intent = self.make_intent(label=f"no progress {n}")
            self.finish_metric(intent, 5)
        family = sapper_family.get(self.store, "parser")
        self.assertEqual(family["state"], "parked")
        self.assertEqual(family["stop_reason"], "stall limit reached without verified metric improvement")
        with self.assertRaisesRegex(sapper.FGError, "parked"):
            self.make_intent(label="must not dispatch")
        with self.store.locked():
            family = sapper_family.decide(self.store, "parser", "probing", "new falsifiable test", "operator")
        self.assertEqual(family["state"], "probing")
        self.assertEqual(family["no_progress"], 0)

    def test_active_limit_attempt_budget_and_terminal_park(self):
        with self.store.locked():
            sapper_family.decide(self.store, "parser", "parked", "test active limit", "operator")
            family = sapper_family.decide(self.store, "parser", "probing", "resume bounded test", "operator")
        i1 = self.make_intent(label="one")
        i2 = self.make_intent(label="two")
        with self.assertRaisesRegex(sapper.FGError, "active-intent limit"):
            self.make_intent(label="third")
        self.assertEqual(len(sapper_family.get(self.store, "parser")["attempts"]), 2)
        with self.store.locked():
            sapper_family.decide(self.store, "parser", "parked", "operator pause", "operator")
        with self.assertRaisesRegex(sapper.FGError, "parked"):
            self.make_intent(label="parked")
        self.assertEqual(i2["family"], "parser")

    def test_attempt_budget_parks_after_reserved_work_finishes(self):
        with self.store.locked():
            sapper_family.create(self.store, "tiny", "Tiny", "one test", "checks", 1, 10.0, 3, "higher", "tester")
        intent = self.make_intent(key="tiny")
        self.store.claim_intent(intent["id"], "worker")
        self.store.conclude_with_new_fact(intent["id"], "no measurable improvement", "worker")
        family = sapper_family.get(self.store, "tiny")
        self.assertEqual(family["state"], "parked")
        self.assertEqual(family["stop_reason"], "attempt budget exhausted")
        self.assertTrue(family["review_due"])
        with self.assertRaisesRegex(sapper.FGError, "parked"):
            self.make_intent(key="tiny")

    def test_spend_budget_parks_and_stop_can_be_reviewed(self):
        with self.store.locked():
            sapper_family.add_spend(self.store, "parser", 10.0, "measured test spend", "operator")
        family = sapper_family.get(self.store, "parser")
        self.assertEqual(family["state"], "parked")
        self.assertEqual(family["spend_usd"], 10.0)
        with self.store.locked():
            family = sapper_family.set_budget(self.store, "parser", 8, 12.0, "approved additional spend", "operator")
            family = sapper_family.decide(self.store, "parser", "probing", "approved new budget", "operator")
        self.assertEqual(family["state"], "probing")
        self.assertEqual(family["budget_usd"], 12.0)

    def test_prompt_summary_is_bounded_and_does_not_dump_family_history(self):
        family = sapper_family.get(self.store, "parser")
        family["metric_history"] = [{"fact_id": f"f{i:03d}", "value": i} for i in range(500)]
        with self.store.locked():
            sapper_family._save(self.store, family)
        block = sapper.render_family_block(self.store, limit=12)
        self.assertIn("parser: Parser state machine", block)
        self.assertNotIn("f499", block)
        self.assertLess(len(block), 500)


if __name__ == "__main__":
    unittest.main()
