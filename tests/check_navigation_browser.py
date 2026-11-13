"""Check immediate navigation and stale responses against an isolated real API."""
import argparse
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from sapper_web import Console, WebServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        app = Console(base / "tasks", base / "state")
        first = app.create_project({"name": "first", "goal": "First workspace goal"})
        second = app.create_project({"name": "second", "goal": "Second workspace goal"})
        (Path(second["path"]) / "nested").mkdir()
        app.graph_action(second["id"], "fact", {"description": "Second workspace evidence"})
        app.panes = lambda: []
        app.workspaces.capabilities = lambda: {"available": False}
        blocked, release, roles_requested = threading.Event(), threading.Event(), threading.Event()
        original_graph, original_status = app.graph, app.workspace_status

        def graph(pid):
            result = original_graph(pid)
            if pid == first["id"] and not blocked.is_set():
                result["project"]["goal"] = "Stale first response"
                blocked.set()
                if not release.wait(15):
                    raise RuntimeError("Navigation check did not release the graph response")
            return result

        def status(pid):
            if pid == first["id"]:
                roles_requested.set()
            return original_status(pid)

        app.graph, app.workspace_status = graph, status
        server = WebServer(("127.0.0.1", 0), app, prefix="/sapper")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=args.browser)
                try:
                    page = browser.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("requestfailed", lambda request: errors.append(f"{request.resource_type}: {request.url.rsplit('/', 1)[-1]} {request.failure}"))
                    # Keep this check independent of the periodic refresh timer.
                    page.add_init_script("window.setInterval = () => 0")
                    url = f"http://127.0.0.1:{server.server_port}/sapper/"
                    page.goto(url)
                    app.auth.initialize("admin", "fixture browser password 123")
                    page.locator("#login-username").fill("admin")
                    page.locator("#login-password").fill("fixture browser password 123")
                    page.locator("#login-form button[type=submit]").click()
                    expect(page.locator(".project-item")).to_have_count(2)
                    page.wait_for_load_state("networkidle")
                    assert page.evaluate("typeof openProject === 'function'"), errors
                    page.evaluate("id => openProject(id, 'memory')", second["id"])
                    expect(page.locator("#facts-list")).to_contain_text("Second workspace evidence")
                    page.evaluate("async () => { state.workspaceFolder = 'nested'; await workspaceFiles(); }")
                    page.evaluate("id => { window.oldNavigation = openProject(id, 'agents').then(() => window.oldFinished = true); }", first["id"])
                    assert blocked.wait(5), "The graph request did not start"
                    assert roles_requested.wait(5), "Roles waited for the blocked graph request"
                    expect(page.locator("#tab-agents")).to_be_visible()
                    expect(page.locator("#project-name")).to_have_text("first")
                    assert page.evaluate("state.workspaceFolder") == "."
                    expect(page.locator("#facts-list")).not_to_contain_text("Second workspace evidence")
                    # Return to the same workspace before its first request completes.
                    page.evaluate("id => openProject(id, 'memory')", second["id"])
                    page.evaluate("id => openProject(id, 'workspace')", first["id"])
                    expect(page.locator("#project-goal")).to_have_text("First workspace goal")
                    release.set()
                    page.wait_for_function("window.oldFinished === true")
                    expect(page.locator("#project-goal")).to_have_text("First workspace goal")
                    assert page.evaluate("state.graph.project.goal") == "First workspace goal"
                    page.evaluate("showOverview()")
                    expect(page.locator("#overview")).to_be_visible()
                    assert not errors, errors
                    print("PASS: immediate tabs, parallel requests, cleared old data, stale-response rejection, overview, no browser errors")
                finally:
                    if errors:
                        print("Browser errors:", errors)
                    release.set()
                    browser.close()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
