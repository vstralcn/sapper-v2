"""Native role doctrine and workspace rendering contracts."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper_roles


class RolePromptTests(unittest.TestCase):
    def render(self, role):
        return sapper_roles.render(
            role, root=Path("/tmp/question with spaces"),
            worker_script=Path("/opt/sapper/docker/commander/worker.sh"),
            workspace_id="0123456789abcdef", workspace_name="fixture challenge",
            workspace_scope=[{"id": "0123456789abcdef", "name": "fixture challenge"}])

    def test_commander_owns_dialogue_and_chooses_dispatch_from_evidence(self):
        prompt = self.render("commander")
        self.assertIn("AskUserQuestion", prompt)
        self.assertIn("A user question does not pause all workers", prompt)
        self.assertIn("If the inputs are sufficient, dispatch work", prompt)
        for fixed_strategy in ("bounded 4-minute scout", "0-10 minutes", "10-20 minutes", "at most two decisive"):
            self.assertNotIn(fixed_strategy, prompt)
        self.assertIn("configured resource limits", prompt)
        self.assertIn("board --json", prompt)
        self.assertIn("0123456789abcdef", prompt)

    def test_general_has_bounded_squads_and_mandatory_harvest(self):
        prompt = self.render("general")
        self.assertIn("Keep at most two squads active", prompt)
        self.assertIn("--role scout --p --minutes 5", prompt)
        self.assertIn("--role verifier --p --minutes 5", prompt)
        self.assertIn("inspect it with `peek`", prompt)
        self.assertIn("`kill` the completed child", prompt)

    def test_permission_block_is_execution_block_and_never_self_authorizes(self):
        prompt = self.render("commander")
        self.assertIn("execution blocker, not a dispatch failure", prompt)
        self.assertIn("keep scheduling independent work", prompt)
        self.assertIn("Never enable skip-permissions yourself", prompt)
        self.assertIn("genuinely requires a user reply or authorization", prompt)

    def test_commander_briefing_distinguishes_goal_from_personal_execution(self):
        prompt = self.render("commander")
        self.assertIn("组织将军或 worker 完成", prompt)
        self.assertIn("不得转为自己解题", prompt)
        self.assertIn("不得亲自查询比赛平台", prompt)
        for role in ("general", "worker", "scout", "verifier", "custom"):
            self.assertNotIn("[Sapper 指挥官职责", self.render(role))

    def test_non_leaders_do_not_receive_spawn_authority(self):
        for role in ("worker", "scout", "verifier", "custom"):
            prompt = self.render(role)
            self.assertNotIn("spawn <workspace-id-or-name>", prompt)
            self.assertIn("must not create children", prompt)

    def test_catalog_and_prompt_expose_traceable_version(self):
        catalog = {row["id"]: row for row in sapper_roles.catalog()}
        self.assertEqual(set(catalog), set(sapper_roles.ROLES))
        self.assertEqual(catalog["general"]["prompt_version"], sapper_roles.PROMPT_VERSION)
        self.assertIn(sapper_roles.PROMPT_VERSION, self.render("general"))


if __name__ == "__main__":
    unittest.main()
