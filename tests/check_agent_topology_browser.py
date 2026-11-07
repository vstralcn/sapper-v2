"""Optional integrated browser check: isolated real Web API, no model or real tmux.

Run with a Python environment containing Playwright:
  python tests/check_agent_topology_browser.py --browser '/path/to/chrome'
The browser executable may be omitted when Playwright Chromium is installed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from sapper_web import Console, WebServer, private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=None)
    parser.add_argument("--screenshot", type=Path, default=None)
    args = parser.parse_args()
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    with tempfile.TemporaryDirectory(prefix="sapper-topology-browser-") as directory:
        base = Path(directory)
        app = Console(base / "tasks", base / "state")
        first = app.create_project({"name": "topology", "goal": "Isolated topology fixture"})
        second = app.create_project({"name": "empty", "goal": "Empty workspace fixture"})
        panes, records = [], {}
        # Existing lifecycle/process behavior has separate tmux tests. These observations
        # are an explicit fixture; the full production HTTP serializer is exercised.
        app.panes = lambda: [dict(p) for p in panes]
        app.tmux = lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr="")
        app.workspaces.capabilities = lambda: {"available": False, "reason": "isolated fixture"}
        created = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        def add(i, parent=None):
            aid, rid, session = f"{i:016x}", f"{i + 1000:016x}", f"fixture-{i}"
            record = {"id": aid, "workspace_id": first["id"], "workspace_scope": [first["id"]],
                      "name": f"Agent {i:02}", "role": "commander" if i in (1, 2) else "general" if i == 3 else ["worker", "scout", "verifier"][i % 3],
                      "parent_agent_id": parent if parent is not None else f"{max(1, (i - 1) // 4):016x}" if i > 1 else None,
                      "created_at": created, "updated_at": created, "run_id": rid,
                      "mission": "Independent task " + str(i % 4), "execution_mode": "tui" if i in (1, 2) else "print",
                      "profile_name": "fixture", "cwd": first["path"], "desired_state": "running",
                      "skip_permissions": i % 2 == 0,
                      "claude_session_id": None}
            if i == 1:
                record["task_label"] = "统筹解题"
            if i == 9:
                record["name"] = '<img src=x onerror="window.topologyInjected=true">'
                record["mission"] = "<script>window.topologyInjected=true</script>"
            if i == 8:
                record["stop_phase"] = "waiting_confirmation"
            records[i] = record
            app.workspaces.save_agent(record)
            info = {"id": rid, "project_id": first["id"], "session": session, "status": "exited" if i == 6 else "running",
                    "role": record["role"], "created_at": created, "profile_name": "fixture", "exit_code": 0 if i == 6 else None}
            private_json(app.run_path(rid) / "run.json", info)
            activity = {"last_event_at": created}
            if i == 4:
                activity.update(result_received=True, result_is_error=True, latest_message="An explicit fixture error")
            if i == 5:
                activity.update(result_received=True, result_is_error=False)
            private_json(app.run_path(rid) / "activity.json", activity)
            if i in (1, 2):
                private_json(app.workspaces.commander.path(aid), {"control": "active", "phase": "responding" if i == 1 else "waiting_user",
                             "last_event_at": datetime.now(timezone.utc).timestamp(), "verification": "board_confirmed"})
            if i != 8:
                panes.append({"id": f"%{i}", "session": session, "project_id": first["id"], "name": f"fixture-{i}",
                              "dead": i == 7, "command": "fixture", "path": first["path"]})
            return aid

        for i in range(1, 25):
            add(i)
        server = WebServer(("127.0.0.1", 0), app, prefix="/sapper")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=args.browser)
                context = browser.new_context(viewport={"width": 1536, "height": 1100})
                page = context.new_page()
                page.emulate_media(color_scheme="light")
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                # No original terminal session is touched by this browser run.
                page.route("**/api/panes/*/stream", lambda route: route.fulfill(status=200, content_type="text/event-stream", body='data: {"type":"mode","mode":"stream","cols":80,"rows":24}\n\n'))
                url = f"http://127.0.0.1:{server.server_port}/sapper/"
                page.goto(url)
                theme = lambda value: page.locator(f'[data-theme-choice="{value}"]:visible')
                check("initial theme follows the light system preference", page.locator("html").get_attribute("data-theme") == "light")
                page.emulate_media(color_scheme="dark")
                page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
                check("system theme updates before an explicit choice", theme("dark").get_attribute("aria-pressed") == "true")
                theme("light").click()
                check("login offers the same global theme control", page.locator("html").get_attribute("data-theme") == "light")
                page.locator("#access-token").fill(app.token)
                page.locator("#login-form button").click()
                page.wait_for_function("state.csrf && state.projects.length === 2")
                page.evaluate("pid => openProject(pid, 'agents')", first["id"])
                page.wait_for_selector(".at-node")
                ev = page.evaluate
                at = lambda name: page.locator(f'[data-at="{name}"]')
                aid = lambda i: f"{i:016x}"
                check("assets served behind URL prefix and real API returns 24 agents", ev("agentTopology.rows.length === 24"))
                check("header counts only live processes by recorded permission", page.locator("#workspace-permissions").inner_text() == "10 个角色跳过审批 / 11 个需审批")
                check("role card shows process permission", at("detail").locator(".agent-process-permissions").inner_text() in ("进程权限：需审批", "进程权限：跳过审批"))
                check("pending external stop preserves a visible confirmation state", ev("id=>renderAgentCard(state.agents.find(a=>a.id===id)).textContent.includes('等待确认停止；确认进程退出前保留意图认领')", aid(8)))
                node_goal = page.locator(f'[data-agent-id="{aid(1)}"] .at-node-mission')
                check("node shows short goal and retains full mission", node_goal.inner_text() == "统筹解题" and node_goal.get_attribute("title") == records[1]["mission"])
                check("terminal picker shows the same goal", page.locator('#native-pane option[value="%1"]').inner_text() == "Agent 01 · 统筹解题 · 000000 · 运行中")
                tools = page.locator("#project-tools summary")
                check("workspace actions have one header entry", tools.is_visible() and page.locator("#native-button").is_hidden() and page.locator("#advanced-run").is_hidden() and page.locator("#dispatch-role").is_visible())
                tools.focus()
                page.keyboard.press("Enter")
                check("workspace menu groups native tools and workspace management", page.locator("#project-tools [role=group]").count() == 2 and page.locator("#enable-workspace").is_visible())
                submission = page.locator("#auto-submit-flags")
                check("proactive flag submission defaults off", not submission.is_checked())
                submission.check()
                page.wait_for_function("!document.querySelector('#auto-submit-flags').disabled")
                check("workspace flag switch persists to the real API", app.store(first["id"]).load_project().get("auto_submit_flags") is True)
                page.evaluate("pid => openProject(pid, 'agents')", second["id"])
                page.wait_for_function("!document.querySelector('#auto-submit-flags').checked")
                check("flag authorization stays in the selected workspace", not app.workspaces.auto_submit_flags(second["id"]))
                page.evaluate("pid => openProject(pid, 'agents')", first["id"])
                page.wait_for_function("document.querySelector('#auto-submit-flags').checked")
                if not page.locator("#project-tools").evaluate("e => e.open"):
                    tools.click()
                submission.uncheck()
                page.wait_for_function("!document.querySelector('#auto-submit-flags').disabled")
                check("flag switch can revoke proactive submission", app.store(first["id"]).load_project().get("auto_submit_flags") is False)
                check("switching policy never submits recorded flags itself", not (app.store(first["id"]).root / ".flag-submit.lock").exists())
                page.keyboard.press("Escape")
                check("Escape closes tools and restores focus", ev("!document.querySelector('#project-tools').open && document.activeElement.matches('#project-tools summary')"))
                for action, dialog in (("dispatch-role", "agent-dialog"), ("workspace-native", "native-dialog"), ("advanced-run", "run-dialog"), ("stop-workspace", "confirm-dialog")):
                    if action != "dispatch-role":
                        tools.click()
                    page.locator("#" + action).click()
                    page.wait_for_selector("#" + dialog + "[open]")
                    check(action + " opens its original dialog", not ev("document.querySelector('#project-tools').open"))
                    if action == "stop-workspace":
                        page.locator("#confirm-cancel").click()
                    else:
                        page.keyboard.press("Escape")
                page.evaluate("openRun('auto')")
                warning=page.locator("#run-permission-warning")
                check("unattended worker warns about repeated permission denials", warning.is_visible() and "被拒绝并反复重试" in warning.inner_text())
                page.locator('#run-form input[name="skip_permissions"]').check()
                check("permission warning tracks checkbox without changing submission", warning.is_hidden())
                page.keyboard.press("Escape")
                page.evaluate("openNative('auto')")
                page.wait_for_selector("#native-field-skip_permissions")
                check("native form explains unattended denial", page.locator("#native-permission-warning").is_visible())
                page.locator("#native-field-skip_permissions").check()
                check("native warning tracks permission", page.locator("#native-permission-warning").is_hidden())
                check("all three forms explain permission consequences", page.locator('label').filter(has_text="允许角色执行命令、修改文件等操作时跳过逐次审批").count() == 3)
                page.keyboard.press("Escape")
                check("unknown executions are not displayed as running", ev("AgentTopologyModel.counts(agentTopology.rows).running === 1"))
                check("online idle remains online", ev("agentTopology.rows.find(a=>a.id.endsWith('5')).observation.presence === 'online'"))
                check("offline and uncertain presence are distinct", ev("AgentTopologyModel.counts(agentTopology.rows).offline === 2 && AgentTopologyModel.counts(agentTopology.rows).presenceUnknown === 1"))
                check("workspace sidebar includes every agent", page.locator(".agent-preview-row").count() == 24)
                check("no simulator or mock dataset controls in production page", page.locator("#auto-demo,#dataset,#change-state").count() == 0)
                check("existing stop and log controls are retained", at("detail").get_by_text("停止", exact=True).count() == 1 and at("detail").get_by_text("查看活动", exact=True).count() == 1)
                at("detail").get_by_text("停止", exact=True).click()
                check("stop still requires original confirmation", page.locator("#confirm-dialog").evaluate("e=>e.open"))
                page.locator("#confirm-cancel").click()
                page.locator('[data-at-scope="all"]').click()
                at("expand").click()
                check("all 24 actual records can be shown", ev("agentTopology.graph.nodes.length === 24"))
                check("task and agent text cannot inject HTML", ev("!window.topologyInjected") and page.locator(".at-node img,.at-node script").count() == 0)
                def compact_pair():
                    return ev("""(() => {
                        const v = agentTopology, box = v.el('canvas').getBoundingClientRect();
                        return v.graph.nodes.length === 2 && v.bounds().h < 220 &&
                            v.graph.edges.length === 1 &&
                            v.graph.nodes.every(n => {
                                const r = v.elements.get(n.id).getBoundingClientRect();
                                return r.left >= box.left && r.right <= box.right &&
                                    r.top >= box.top && r.bottom <= box.bottom;
                            });
                    })()""")

                at("status").select_option("waiting")
                check("status filtering compacts parent and child and fits both on canvas", compact_pair())
                page.locator('[data-at-scope="all"]').click()
                at("search").fill(aid(6)); at("results").locator("button").first.click()
                check("search reveals offline records", ev("agentTopology.current.selected.endsWith('6') && agentTopology.elements.has(agentTopology.current.selected)"))
                at("search").fill(aid(1)); at("search").press("Enter")
                check("search keyboard selects agent", ev("agentTopology.current.selected === '0000000000000001'"))
                page.locator('[data-at-scope="online"]').click()
                positions = ev("JSON.stringify([...agentTopology.current.positions])")
                page.locator('[data-agent-id="0000000000000001"]').evaluate("e=>e.dataset.preserved='yes'")
                private_json(app.workspaces.commander.path(aid(1)), {"control": "active", "phase": "idle", "last_event_at": datetime.now(timezone.utc).timestamp()})
                page.wait_for_function("agentTopology.rows.find(a=>a.id==='0000000000000001').observation.status === 'idle'")
                check("existing polling updates real status", page.locator('[data-at-metric="running"] strong').inner_text() == "0")
                check("polling preserves all node coordinates", positions == ev("JSON.stringify([...agentTopology.current.positions])"))
                check("polling preserves node DOM identity", page.locator('[data-agent-id="0000000000000001"]').get_attribute("data-preserved") == "yes")
                at("reveal").click()
                node = page.locator('[data-agent-id="0000000000000001"]')
                box = node.bounding_box(); before = ev("({...agentTopology.current.positions.get('0000000000000001')})")
                page.mouse.move(box["x"] + 80, box["y"] + 25); page.mouse.down()
                page.mouse.move(box["x"] + 115, box["y"] + 55, steps=4); page.mouse.up()
                check("drag moves selected node", ev("agentTopology.current.positions.get('0000000000000001').x") > before["x"])
                before = ev("JSON.stringify([...agentTopology.current.positions])")
                add(25, parent=aid(4))
                page.wait_for_function("agentTopology.rows.length === 25")
                check("new lifecycle record appears without moving existing agents", before == ev("JSON.stringify([...agentTopology.current.positions].filter(([id])=>id!=='0000000000000019'))"))
                private_json(app.run_path(records[25]["run_id"]) / "run.json", {"id": records[25]["run_id"], "session": "fixture-25", "project_id": first["id"], "status": "exited", "exit_code": 0, "created_at": created, "profile_name": "fixture", "role": "worker"})
                page.wait_for_function("agentTopology.rows.find(a=>a.id==='0000000000000019').observation.status === 'offline'")
                check("exit updates graph and global counts", ev("AgentTopologyModel.counts(agentTopology.rows).offline === 3"))
                at("focus").click()
                check("focus dims unrelated agents and highlights actual edges", page.locator(".at-node.at-dim").count() > 0 and page.locator(".at-edge.at-highlight").count() > 0)
                at("focus").click()
                zoom = ev("agentTopology.current.zoom"); at("zoom-in").click()
                check("zoom control works", ev("agentTopology.current.zoom") > zoom)
                at("canvas").focus(); x = ev("agentTopology.current.pan.x"); page.keyboard.press("ArrowLeft")
                check("keyboard canvas pan works", ev("agentTopology.current.pan.x") > x)
                at("grouping").select_option("role")
                check("real role groups aggregate accurately", ev("agentTopology.graph.nodes.every(n=>n.type==='group') && agentTopology.graph.nodes.reduce((s,n)=>s+n.members.length,0)===agentTopology.graph.matched"))
                page.locator(".at-group").first.click()
                check("role groups expand", ev("agentTopology.graph.nodes.some(n=>n.type==='agent')"))
                at("focus").click()
                check("grouped focus shows real upstream and downstream agents", ev("agentTopology.current.grouping==='hierarchy' && agentTopology.elements.has(agentTopology.current.selected) && !agentTopology.current.collapsed.has(agentTopology.current.selected)"))
                at("focus").click()
                at("grouping").select_option("task")
                check("task groups use real mission text", ev("agentTopology.graph.nodes.every(n=>n.members.every(a=>a.mission===n.key))"))
                theme("dark").click()
                check("only the global theme control remains", at("theme").count() == 0)
                check("global dark theme works", page.locator("html").get_attribute("data-theme") == "dark")
                check("dark heading has an actual dark background", page.locator("#agent-topology").evaluate("e=>getComputedStyle(e).backgroundColor==='rgb(21, 36, 49)'"))
                check("legacy context text follows dark panel contrast", page.locator(".at-detail .agent-context").first.evaluate("e=>getComputedStyle(e).color==='rgb(154, 175, 189)'"))
                check("header and controls share the dark palette", ev("getComputedStyle(document.querySelector('.topbar')).backgroundColor==='rgb(28, 48, 63)' && getComputedStyle(document.querySelector('[data-at=search]')).backgroundColor==='rgb(28, 48, 63)'"))
                # Original dialogs are reused, not prototype-only buttons.
                page.locator("#dispatch-role").click(); check("original dispatch form opens", page.locator("#agent-dialog").evaluate("e=>e.open"))
                check("dialogs and form fields follow the global theme", ev("getComputedStyle(document.querySelector('#agent-dialog')).backgroundColor==='rgb(28, 48, 63)' && getComputedStyle(document.querySelector('#agent-dialog textarea')).backgroundColor==='rgb(28, 48, 63)'"))
                page.keyboard.press("Escape")
                page.evaluate("pid=>openProject(pid,'agents')", second["id"])
                check("empty workspace cannot inherit previous agents", ev("agentTopology.rows.length === 0 && agentTopology.graph.nodes.length === 0"))
                check("empty workspace offers dispatch", "派发角色" in at("empty").inner_text() and page.locator("#dispatch-role").is_visible())
                page.evaluate("pid=>openProject(pid,'agents')", first["id"])
                check("workspace-specific grouping and selection restore", ev("agentTopology.current.grouping === 'task' && agentTopology.current.selected==='0000000000000001'"))
                page.route("**/api/workspaces/*/agents", lambda route: route.abort())
                page.wait_for_function("!agentTopology.connected")
                check("failed refresh retains observed graph with stale indicator", ev("agentTopology.rows.length===25") and "更新中断" in at("connection").inner_text())
                page.unroute("**/api/workspaces/*/agents")
                page.wait_for_function("agentTopology.connected")
                check("polling recovers after connection failure", "已更新" in at("connection").inner_text())
                for i in range(26, 61):
                    add(i)
                page.wait_for_function("agentTopology.rows.length===60")
                at("grouping").select_option("hierarchy")
                page.locator('[data-at-scope="all"]').click()
                if ev("agentTopology.current.collapsed.size"):
                    at("expand").click()
                at("layout").click()
                check("60 live API records expand", ev("agentTopology.graph.nodes.length===60"))
                check("60-agent automatic layout has no overlapping cards", ev("agentTopology.graph.nodes.every((a,i)=>agentTopology.graph.nodes.slice(i+1).every(b=>{const p=agentTopology.position(a.id),q=agentTopology.position(b.id);return Math.abs(p.x-q.x)>=218||Math.abs(p.y-q.y)>=109}))"))
                page.locator('[data-at-fold="0000000000000001"]').click()
                check("folding the root recenters the remaining node", ev("agentTopology.graph.nodes.length===1 && agentTopology.current.zoom>=1"))
                page.locator('[data-at-fold="0000000000000001"]').click()
                check("unfolding restores and fits all descendants", ev("agentTopology.graph.nodes.length===60 && agentTopology.current.zoom<1"))
                page.locator('[data-at-scope="online"]').click()
                for i in range(3, 61):
                    path = app.run_path(records[i]["run_id"]) / "run.json"
                    info = json.loads(path.read_text())
                    private_json(path, {**info, "status": "exited", "exit_code": 0})
                page.wait_for_function("AgentTopologyModel.counts(agentTopology.rows).offline===58")
                check("polling compacts the graph when agents become hidden", compact_pair())
                page.locator('[data-at-scope="all"]').click()
                page.locator('[data-at-scope="active"]').click()
                check("hiding inactive history compacts the graph and preserves parent context", compact_pair() and ev("agentTopology.graph.nodes.find(n=>n.id==='0000000000000001').context"))
                page.locator('[data-at-scope="all"]').click()
                at("grouping").select_option("role")
                at("status").select_option("waiting")
                check("group filtering compacts and fits the remaining group", ev("agentTopology.graph.nodes.length===1 && agentTopology.bounds().h<220 && agentTopology.current.zoom>=1"))
                page.locator('[data-at-scope="all"]').click()
                for width in (2560, 5120):
                    page.set_viewport_size({"width": width, "height": 1100})
                    check(f"{width}px main content fills the available width", ev("Math.abs(document.querySelector('.main').getBoundingClientRect().right-innerWidth)<=1 && Math.abs(document.querySelector('.topbar').getBoundingClientRect().right-innerWidth)<=1 && Math.abs(document.querySelector('#agent-topology').getBoundingClientRect().right-(innerWidth-36))<=1"))
                    check(f"{width}px viewport does not overflow", ev("document.documentElement.scrollWidth<=innerWidth"))
                page.set_viewport_size({"width": 1536, "height": 1100})
                if args.screenshot:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    page.set_viewport_size({"width": 2560, "height": 1100})
                    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                    page.screenshot(path=str(args.screenshot), full_page=False)
                page.set_viewport_size({"width": 1100, "height": 850})
                check("desktop layout does not overflow", ev("document.documentElement.scrollWidth<=innerWidth"))
                page.set_viewport_size({"width": 390, "height": 844})
                check("narrow fallback does not overflow", ev("document.documentElement.scrollWidth<=innerWidth"))
                tools.click()
                check("narrow workspace menu fits the viewport", ev("document.querySelector('.project-tools-panel').getBoundingClientRect().left >= 0 && document.querySelector('.project-tools-panel').getBoundingClientRect().right <= innerWidth"))
                page.locator("#project-goal").click()
                check("clicking outside dismisses workspace menu", not ev("document.querySelector('#project-tools').open"))
                check("global theme switch remains visible on narrow screens", theme("light").is_visible() and theme("dark").is_visible())
                theme("light").click()
                check("light topology follows the global theme without overflow", ev("getComputedStyle(document.querySelector('.at-metrics')).backgroundColor==='rgb(255, 255, 255)' && document.documentElement.scrollWidth<=innerWidth"))
                page.locator('[data-tab="memory"]').click()
                check("original memory tab still works and hides topology", page.locator("#tab-agents").is_hidden() and page.locator("#tab-memory").is_visible())
                page.set_viewport_size({"width": 1536, "height": 1100})
                page.locator('[data-tab="workspace"]').click()
                page.wait_for_function("!!terminal")
                check("native terminal starts with the selected light palette", ev("terminal.options.theme.background==='#f8fafb' && getComputedStyle(document.querySelector('.workspace-terminal')).backgroundColor==='rgb(248, 250, 251)'"))
                theme("dark").click()
                check("existing terminal updates immediately without recreation", ev("terminal.options.theme.background==='#152431' && terminal.options.theme.foreground==='#d3e3ea'"))
                for tab in ("memory", "console", "files"):
                    page.locator(f'[data-tab="{tab}"]').click()
                    check(f"{tab} view retains the global dark theme", ev("document.documentElement.dataset.theme==='dark' && getComputedStyle(document.body).backgroundColor==='rgb(21, 36, 49)'"))
                tools.click()
                with page.expect_event("framenavigated", predicate=lambda frame: frame.url.endswith("/graph-view")):
                    page.locator("#graph-button").click()
                page.wait_for_function("document.querySelector('#graph-frame').contentDocument?.readyState==='complete' && document.querySelector('#graph-frame').contentDocument?.getElementById('sapper-page-theme')")
                check("embedded native graph shares the global palette", ev("getComputedStyle(document.querySelector('#graph-frame').contentDocument.body).backgroundColor==='rgb(21, 36, 49)'"))
                page.keyboard.press("Escape")
                theme("light").click()
                tools.click()
                with page.expect_event("framenavigated", predicate=lambda frame: frame.url.endswith("/graph-view")):
                    page.locator("#graph-button").click()
                page.wait_for_function("document.querySelector('#graph-frame').contentDocument?.readyState==='complete' && document.querySelector('#graph-frame').contentDocument?.getElementById('sapper-page-theme')")
                check("embedded native graph also supports the global light palette", ev("getComputedStyle(document.querySelector('#graph-frame').contentDocument.body).backgroundColor==='rgb(243, 246, 248)'"))
                page.keyboard.press("Escape")
                theme("dark").click()
                page.emulate_media(color_scheme="light")
                check("manual choice takes precedence over system preference", ev("document.documentElement.dataset.theme==='dark'"))
                page.reload()
                page.wait_for_selector("#overview:visible")
                check("overview retains access to native tools", page.locator("#native-button").is_visible())
                check("theme persists across reload and on the overview", ev("document.documentElement.dataset.theme==='dark' && localStorage.getItem('sapper-theme')==='dark' && getComputedStyle(document.querySelector('.overview-context')).backgroundColor==='rgb(32, 53, 67)'"))
                other = page.context.new_page()
                other.on("pageerror", lambda error: errors.append(str(error)))
                other.goto(url)
                other.locator('[data-theme-choice="light"]:visible').click()
                page.wait_for_function("document.documentElement.dataset.theme==='light'")
                check("theme choice synchronizes across open tabs", theme("light").get_attribute("aria-pressed") == "true")
                other.close()
                fallback = browser.new_page(color_scheme="dark")
                fallback.on("pageerror", lambda error: errors.append(str(error)))
                fallback.add_init_script("Storage.prototype.getItem=Storage.prototype.setItem=()=>{throw new Error('disabled storage')}")
                fallback.goto(url)
                check("disabled storage still permits system theme", fallback.locator("html").get_attribute("data-theme") == "dark")
                fallback.locator('[data-theme-choice="light"]:visible').click()
                check("disabled storage still permits manual theme selection", fallback.locator("html").get_attribute("data-theme") == "light")
                fallback.close()
                check("no browser JavaScript errors", not errors)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
