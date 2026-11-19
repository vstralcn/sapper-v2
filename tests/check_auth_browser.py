"""Exercise password login, SQLite persistence and revocation using a real browser."""
import argparse
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from sapper_auth import AuthStore
from sapper_web import Console, WebServer

PASSWORD = "abc123"
NEW_PASSWORD = "def456"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sapper-auth-browser-") as directory:
        base = Path(directory)
        app = Console(base / "tasks", base / "state")
        app.create_project({"name": "fixture", "goal": "Authentication check"})
        app.panes = lambda: []
        app.workspaces._capabilities = {"available": False}
        server = WebServer(("127.0.0.1", 0), app, prefix="/sapper")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=args.browser)
                try:
                    first = browser.new_context()
                    second = browser.new_context()
                    page, other = first.new_page(), second.new_page()
                    for tab in (page, other):
                        tab.on("pageerror", lambda error: errors.append(str(error)))
                    url = f"http://127.0.0.1:{server.server_port}/sapper/"
                    page.goto(url)
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#login-help")).to_contain_text("auth-init")
                    app.auth.initialize("admin", PASSWORD)

                    def login(tab, password):
                        tab.locator("#login-username").fill("admin")
                        tab.locator("#login-password").fill(password)
                        tab.locator("#login-form button[type=submit]").click()

                    login(page, "wrong password")
                    expect(page.locator("#login-error")).to_have_text("用户名或密码错误")
                    login(page, PASSWORD)
                    expect(page.locator("#workspace")).to_be_visible()
                    expect(page.locator("#login-password")).to_have_value("")
                    other.goto(url)
                    other.wait_for_load_state("networkidle")
                    login(other, PASSWORD)
                    expect(other.locator("#workspace")).to_be_visible()

                    # Recreate the real HTTP server at the same origin, retaining only SQLite state.
                    port = server.server_port
                    server.shutdown()
                    server.server_close()
                    thread.join()
                    app = Console(app.root, app.state)
                    app.panes = lambda: []
                    app.workspaces._capabilities = {"available": False}
                    server = WebServer(("127.0.0.1", port), app, prefix="/sapper")
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#workspace")).to_be_visible()
                    page.locator("#profiles-button").click()
                    page.locator('[data-settings-tab="account"]').click()
                    expect(page.locator("#settings-account-panel")).to_be_visible()
                    expect(page.locator("#account-username")).to_have_value("admin")
                    form = page.locator("#password-form")
                    form.locator('[name="current_password"]').fill("wrong password")
                    form.locator('[name="new_password"]').fill(NEW_PASSWORD)
                    form.locator('[name="confirm_password"]').fill(NEW_PASSWORD)
                    form.locator('button[type="submit"]').click()
                    expect(page.locator("#password-error")).to_have_text("当前密码错误")
                    form.locator('[name="current_password"]').fill(PASSWORD)
                    form.locator('button[type="submit"]').click()
                    expect(page.locator("#login")).to_be_visible()
                    expect(page.locator("#profiles-dialog")).not_to_be_visible()
                    expect(page.locator("#login-error")).to_contain_text("密码已修改")
                    other.reload()
                    expect(other.locator("#login")).to_be_visible()
                    login(page, PASSWORD)
                    expect(page.locator("#login-error")).to_have_text("用户名或密码错误")
                    login(page, NEW_PASSWORD)
                    expect(page.locator("#workspace")).to_be_visible()

                    AuthStore(app.state).reset_password("admin", PASSWORD)
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#login")).to_be_visible()
                    login(page, PASSWORD)
                    expect(page.locator("#workspace")).to_be_visible()
                    page.locator("#logout").click()
                    expect(page.locator("#login")).to_be_visible()
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#login")).to_be_visible()
                    assert not errors, errors
                    print("PASS: local setup prompt, password login, server restart persistence, password change, cross-device revocation, local reset, logout, no browser errors")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    main()
