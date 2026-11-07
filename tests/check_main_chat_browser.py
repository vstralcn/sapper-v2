"""Check terminal-first dispatch, native input, files and every agent's status."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_sapper_workspaces import NativeWorkspaceTests, COMMANDER_FIXTURE
from sapper_web import WebServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    fixture = NativeWorkspaceTests()
    fixture.setUp()
    app = fixture.app
    fixture.bin.write_text(COMMANDER_FIXTURE)
    other = app.create_project({"name":"other-browser", "goal":"另一个独立目标"})
    server = WebServer(("127.0.0.1", 0), app, prefix="/sapper")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors, chat_requests = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=args.browser)
            try:
                page = browser.new_page(viewport={"width":1280,"height":960})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: chat_requests.append(request.url) if "/chat" in request.url else None)
                page.on("requestfailed", lambda request: errors.append(f"{request.resource_type}: {request.failure}") if request.resource_type in ("script","stylesheet") else None)
                page.goto(f"http://127.0.0.1:{server.server_port}/sapper/")
                page.locator("#access-token").fill(app.token)
                page.locator("#login-form button[type=submit]").click()
                expect(page.locator(".project-item")).to_have_count(2)
                page.wait_for_load_state("networkidle")
                page.evaluate("id => openProject(id)", fixture.pid)
                expect(page.locator("#tab-workspace")).to_be_visible()
                expect(page.locator('[data-tab="chat"]')).to_have_count(0)
                expect(page.locator("#tab-chat")).to_have_count(0)
                expect(page.get_by_role("button",name="派发角色",exact=True)).to_have_count(1)
                page.locator("#dispatch-role").click()
                expect(page.locator("#agent-role")).to_have_value("commander")
                page.locator("#dispatch-submit").click()
                page.wait_for_function("state.mainAgent?.pane_id && state.nativePane === state.mainAgent.pane_id")
                main = app.workspaces.main_agent(fixture.pid)
                assert main["execution_mode"] == "tui"
                assert len(app.workspaces.services(fixture.pid)) == 1
                page.wait_for_function("Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()).join('\\n').includes('NATIVE_READY')")
                page.locator("#terminal-control").click()
                page.wait_for_function("state.nativeControl && document.activeElement === terminal.textarea")
                page.keyboard.type("USER_DIRECTION_FROM_TERMINAL")
                page.keyboard.press("Enter")
                transcript = app.workspaces.commander.transcript(app.workspaces.agent(main["id"]))
                fixture.until(lambda: "USER_DIRECTION_FROM_TERMINAL" in transcript.read_text())
                page.locator("#terminal-control").click()
                assert page.evaluate("terminal.options.disableStdin")
                page.locator(".workspace-file").filter(has_text="challenge.txt").click()
                expect(page.locator("#viewer-content")).to_have_text("read this original question")
                page.locator("#viewer-dialog .dialog-close").click()

                basis = app.store(fixture.pid).add_fact("fixture independent input", "user")["id"]
                children = []
                for number in range(5):
                    run = app.workspaces.create_session(fixture.pid, {"parent_agent_id":main["id"], "from":basis,
                        "mission":f"Inspect supplied file {number}", "name":f"Worker {number}"})
                    fixture.until(lambda: "NATIVE_READY" in app.log(run["id"])["text"])
                    children.append(app.workspaces.agent(run["agent_id"]))
                for agent, tool in zip(children, ("AskUserQuestion", "Read")):
                    path = app.workspaces.commander.transcript(agent)
                    with path.open("a") as file:
                        file.write(json.dumps({"type":"assistant", "sessionId":agent["claude_session_id"],
                            "timestamp":datetime.now(timezone.utc).isoformat(), "message":{"content":[
                                {"type":"tool_use", "id":"pending", "name":tool}]}}) + "\n")
                app.workspaces.stop_agent(children[-1]["id"])
                page.evaluate("refreshTask()")
                expect(page.locator(".agent-preview-row")).to_have_count(6)
                row = lambda agent: page.locator(f'#agent-list [data-role-agent="{agent["id"]}"]')
                expect(row(children[0])).to_contain_text("等待中")
                expect(row(children[1])).to_contain_text("运行中")
                expect(row(children[2])).to_contain_text("在线空闲")
                expect(row(children[-1])).to_contain_text("已离线")
                row(children[0]).click()
                page.wait_for_function("id => state.nativePane === state.agents.find(a=>a.id===id).pane_id", arg=children[0]["id"])
                expect(row(children[0])).to_have_attribute("aria-pressed", "true")
                page.locator("#dispatch-role").click()
                expect(page.locator("#agent-role")).to_have_value("worker")
                page.locator("#agent-mission").fill("DISPATCH_ANOTHER_INPUT")
                page.locator("#dispatch-submit").click()
                fixture.until(lambda: "DISPATCH_ANOTHER_INPUT" in transcript.read_text())
                assert len(app.workspaces.agents(fixture.pid)) == 6
                page.wait_for_function("state.nativePane === state.mainAgent.pane_id")
                page.reload()
                page.wait_for_load_state("networkidle")
                page.evaluate("id => openProject(id)", fixture.pid)
                expect(page.locator(".agent-preview-row")).to_have_count(6)
                app.workspaces.stop_agent(main["id"])
                page.evaluate("refreshTask()")
                expect(row(main)).to_contain_text("已离线")
                row(main).click()
                page.get_by_role("button",name="恢复此会话",exact=True).click()
                expect(page.locator("#tab-workspace")).to_be_visible()
                fixture.until(lambda: app.workspaces.main_agent(fixture.pid)["pane_id"])
                resumed = app.workspaces.main_agent(fixture.pid)
                assert resumed["claude_session_id"] == main["claude_session_id"]
                assert resumed["execution_mode"] == "tui"
                assert app.run_info(children[1]["run_id"])["status"] == "running"
                page.evaluate("id => openProject(id)", other["id"])
                expect(page.locator(".agent-preview-row")).to_have_count(0)
                expect(page.locator("#native-pane")).to_have_value("")
                assert page.evaluate("terminal.options.disableStdin")
                assert not page.evaluate("Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()).join('\\n').includes('NATIVE_READY')")
                page.evaluate("id => openProject(id)", fixture.pid)
                expect(page.locator(".agent-preview-row")).to_have_count(6)
                page.set_viewport_size({"width":390,"height":844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page overflows"
                # A changed workspace default is an explicit new-session choice.
                app.workspaces.stop_agent(main["id"])
                app.workspaces.commander.configure(main["id"], "paused", "stopped historical fixture")
                profile = app.save_profile({"name":"GLM browser fixture", "model":"fixture-glm", "api_key":"fixture-profile-key"})
                app.save_role_models({"assignments":{"commander":profile["id"]},
                    "expected_revision":app.role_models()["revision"]}, fixture.pid)
                for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SAPPER_CLAUDE_API_KEY"):
                    os.environ.pop(key, None)
                page.set_viewport_size({"width":1280,"height":960})
                page.reload();page.wait_for_load_state("networkidle")
                page.evaluate("id => openProject(id)", fixture.pid)
                page.locator("#dispatch-role").click()
                expect(page.locator("#dispatch-session-mode")).to_have_value("resume")
                expect(page.locator("#dispatch-model-summary")).to_contain_text("恢复使用：继承原环境")
                expect(page.locator("#dispatch-model-summary")).to_contain_text("GLM browser fixture · fixture-glm")
                page.locator("#dispatch-submit").click()
                expect(page.locator("#dispatch-error")).to_contain_text("沿用原模型配置“继承原环境”")
                expect(page.locator("#dispatch-error")).to_contain_text("GLM browser fixture · fixture-glm")
                expect(page.locator("#dispatch-error")).to_contain_text("API Key")
                expect(page.locator("#dispatch-error")).not_to_contain_text("fixture-profile-key")
                page.locator("#dispatch-session-mode").select_option("new")
                expect(page.locator("#dispatch-model-summary")).to_contain_text("新会话使用：GLM browser fixture · fixture-glm")
                expect(page.locator("#dispatch-settings")).not_to_have_attribute("hidden", "")
                page.locator("#dispatch-submit").click()
                page.wait_for_function("old => state.mainAgent?.id !== old && state.mainAgent?.pane_id", arg=main["id"])
                fresh = app.workspaces.main_agent(fixture.pid)
                assert fresh["profile_id"] == profile["id"]
                assert fresh["claude_session_id"] != main["claude_session_id"]
                assert app.workspaces.agent(main["id"])["profile_id"] == ""
                assert app.run_info(children[1]["run_id"])["status"] == "running"
                assert not chat_requests, chat_requests
                assert not errors, errors
                print("PASS: terminal/input/files/status, exact UUID resume, original model and changed default explained, persistent auth error, explicit new-session model, children continue, isolation, mobile layout, no chat requests or browser errors")
            finally:
                if errors:
                    print("Browser errors:", errors)
                browser.close()
    finally:
        server.shutdown();server.server_close();thread.join()
        fixture.tearDown()


if __name__ == "__main__":
    main()
