"""Check a competition board through the real authenticated Web routes."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import uuid

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import sapper
from sapper_web import Console, WebServer, private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        app = Console(base / "tasks", base / "state")
        first = app.create_project({"name": "local-competition", "goal": "Fixture"})
        second = app.create_project({"name": "api-competition", "goal": "Fixture"})
        (Path(first["path"]) / "round" / "Web" / "Mirror").mkdir(parents=True)
        (Path(first["path"]) / "round" / "Pwn" / "Echo").mkdir(parents=True)
        private_json(Path(second["path"]) / "competition.json", {
            "source": "比赛 API", "tasks": [{"id": "web-01", "name": "Library", "category": "Web"}]
        })
        app.auth.initialize("admin", "fixture browser password 123")
        server = WebServer(("127.0.0.1", 0), app, prefix="/sapper")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/sapper/")
                page.locator("#login-username").fill("admin")
                page.locator("#login-password").fill("fixture browser password 123")
                page.locator("#login-form button[type=submit]").click()
                page.locator(".project-item").get_by_text("local-competition").click()
                expect(page.locator("#tab-progress")).to_be_visible()
                expect(page.locator("#competition-board")).to_be_visible()
                expect(page.locator("#competition-source")).to_contain_text("尚未导入题单")
                expect(page.locator("#competition-roster")).to_be_hidden()
                page.locator("#competition-import").click()
                page.locator("#competition-browse").click()
                page.locator("#directory-list").get_by_role("button", name="round 打开").click()
                page.locator("#directory-select").click()
                expect(page.locator("#competition-directory")).to_have_value(str(Path(first["path"]) / "round"))
                page.locator("#competition-import-form [name=complete]").check()
                page.locator("#competition-import-form button[type=submit]").click()
                rows = page.locator("#competition-rows tr")
                expect(rows).to_have_count(2)
                expect(page.locator("#competition-score")).to_have_text("0/ 2")
                page.locator("#competition-directions").get_by_role("button", name="Web 0 / 1").click()
                expect(rows).to_have_count(1)
                rows.get_by_role("button", name="Mirror Web/Mirror · Web").click()
                page.locator("#competition-create-intent").click()
                page.locator("#record-form [name=description]").fill("Review the fixture")
                page.locator("#record-form button[type=submit]").click()
                expect(page.locator("#record-dialog")).to_be_hidden()
                store = app.store(first["id"])
                assignment, = store.list_intents()
                assert assignment["competition_challenge"] == "Web/Mirror"

                agent_id, run_id = "a" * 16, "b" * 16
                store.claim_intent(assignment["id"], agent_id)
                page.evaluate("refreshTask()")
                expect(rows).to_contain_text("已派发")
                started = sapper.now_iso()
                session = str(uuid.uuid4())
                app.workspaces.save_agent({"id": agent_id, "workspace_id": first["id"], "name": "Fixture Agent",
                    "role": "worker", "created_at": started, "intent_id": assignment["id"],
                    "claude_session_id": session, "execution_mode": "tui", "run_id": run_id,
                    "claude_config_dir": str(base / "claude"), "profile_snapshot": {}})
                private_json(app.run_path(run_id) / "run.json", {"id": run_id, "kind": "claude-native",
                    "project_id": first["id"], "status": "exited", "intent_id": assignment["id"],
                    "session": "fixture-ended", "created_at": started})
                transcript = base / "claude" / "projects" / "fixture" / f"{session}.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text(json.dumps({"type": "assistant", "sessionId": session,
                    "timestamp": sapper.now_iso(), "message": {"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "Fixture output"}]}}) + "\n", encoding="utf-8")
                page.evaluate("refreshTask()")
                expect(rows).to_contain_text("Fixture Agent")
                expect(rows).to_contain_text("已开工")
                expect(rows).to_contain_text("已离线")

                flag = store.add_fact("fixture flag", "test", kind="flag",
                    data={"challenge": "Web/Mirror", "status": "recorded"})
                store.update_flag_fact(flag["id"], "submitted", '{"correct":true}')
                page.evaluate("refreshTask()")
                expect(rows).to_contain_text("已通过")
                expect(page.locator("#competition-score")).to_have_text("1/ 2")
                page.locator(".project-item").get_by_text("api-competition").click()
                expect(rows).to_have_count(1)
                expect(rows).to_contain_text("Library")
                expect(page.locator("#competition-score-label")).to_have_text("已解 / 已知题数")
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    print("Competition board browser checks passed.")


if __name__ == "__main__":
    main()
