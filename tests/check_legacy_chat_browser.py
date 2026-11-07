"""Offline native roles stay visible while their original history is preserved."""
import argparse
import json
from pathlib import Path
import sys
import threading
from unittest import mock

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_sapper_chat import LegacyConversationTests
from sapper_web import WebServer, private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    fixture = LegacyConversationTests()
    fixture.setUp()
    app = fixture.app
    app.workspaces._capabilities = {"available":False}
    first,path = fixture.historical_agent(1)
    second,_ = fixture.historical_agent(2)
    for agent in (first,second):
        agent["run_id"] = agent["id"]
        app.workspaces.save_agent(agent)
        private_json(app.run_path(agent["id"]) / "run.json",{"id":agent["id"],"project_id":fixture.project["id"],
            "status":"stopped","session":"stopped-"+agent["id"],"created_at":agent["created_at"]})
    with path.open("a") as file:
        for i in range(120):
            file.write(json.dumps({"type":"assistant","uuid":f"old-{i}","sessionId":first["claude_session_id"],
                "timestamp":"2026-11-03T00:00:00Z","message":{"content":[{"type":"text","text":f"OLDER_STEP_{i}"}]}})+"\n")
    original = path.read_bytes()
    other = app.create_project({"name":"other", "goal":"another workspace"})
    server = WebServer(("127.0.0.1",0),app,prefix="/sapper")
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    errors = []
    try:
        with mock.patch.object(app,"start") as start, sync_playwright() as p:
            browser = p.chromium.launch(headless=True,executable_path=args.browser)
            try:
                page = browser.new_page()
                page.on("pageerror",lambda error:errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/sapper/")
                page.locator("#access-token").fill(app.token)
                page.locator("#login-form button[type=submit]").click()
                expect(page.locator(".project-item")).to_have_count(2)
                page.wait_for_load_state("networkidle")
                page.evaluate("id=>openProject(id)",fixture.project["id"])
                expect(page.locator("#tab-workspace")).to_be_visible()
                expect(page.locator('[data-tab="chat"]')).to_have_count(0)
                expect(page.locator(".agent-preview-row")).to_have_count(2)
                expect(page.locator("#agent-list")).to_contain_text("已离线")
                page.evaluate("id=>openProject(id)",other["id"])
                expect(page.locator(".agent-preview-row")).to_have_count(0)
                page.evaluate("id=>openProject(id)",fixture.project["id"])
                expect(page.locator(".agent-preview-row")).to_have_count(2)
                page.reload();page.wait_for_load_state("networkidle")
                page.evaluate("id=>openProject(id)",fixture.project["id"])
                expect(page.locator(".agent-preview-row")).to_have_count(2)
                page.locator('[data-tab="agents"]').click()
                expect(page.locator('[data-at-scope="all"]')).to_have_attribute("aria-pressed","true")
                assert page.evaluate("agentTopology.graph.nodes.length") == 2
                start.assert_not_called()
                assert path.read_bytes() == original
                assert not (app.state / "main-agents").exists()
                assert not errors,errors
                print("PASS: terminal default, all offline roles, reload, workspace isolation, offline topology, unchanged history, no model launches")
            finally:
                if errors:print("Browser errors:",errors)
                browser.close()
    finally:
        server.shutdown();server.server_close();thread.join();fixture.doCleanups()


if __name__ == "__main__":
    main()
