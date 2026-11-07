"""Workspace/session adapter for the original Claude Code and sapper tools.

No model API implementation lives here. The worker/watchdog command adapters
are enabled only by SAPPER_MODE=workspace; original competition paths remain.
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import sapper
import sapper_context
import sapper_roles
from sapper_permission_audit import permission_audit
from sapper_extensions import AgentExtensions, ExtensionError, REQUIRED_FLAGS
from sapper_commander import CommanderMonitor, WatchdogLog
from sapper_chat import ChatJournal, NativeConversation

REPO = Path(__file__).resolve().parent.parent
ROLES = sapper_roles.ROLES
START_MARK = "<!-- sapper-workspace:start -->"
END_MARK = "<!-- sapper-workspace:end -->"
TERMINAL_STATES = {"exited", "failed", "stopped", "interrupted", "timed_out"}


class WorkspaceError(sapper.FGError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def text(data, key, default="", limit=20000):
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise WorkspaceError(f"{key} 必须为文本（最多 {limit} 字符）")
    return value.strip()


def serialized(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self.lifecycle():
            return method(self, *args, **kwargs)
    return call


class Workspaces:
    def __init__(self, console):
        self.app = console
        self._capabilities = None
        self._in_lifecycle = False
        self._execution_observations = {}
        self.commander = CommanderMonitor(self)
        self.extensions = AgentExtensions(self)
        self.conversations = NativeConversation()

    @contextlib.contextmanager
    def lifecycle(self):
        # Web requests, worker children and watchdog are separate processes.
        # Serialize their launches/stops, including nested calls in this manager.
        with self.app.lock:
            if self._in_lifecycle:
                yield
                return
            with self.app.config_lock():
                self._in_lifecycle = True
                try:
                    yield
                finally:
                    self._in_lifecycle = False

    def allowed(self, path):
        path = Path(path).expanduser().resolve()
        if path.is_relative_to(self.app.state):
            raise WorkspaceError("Web 私有凭据目录不能绑定为题目工作区", 403)
        if not path.is_dir():
            raise WorkspaceError("执行机上的目录不存在", 404)
        return path

    def bindings(self):
        return self.app.config().get("workspaces", [])

    def bound_projects(self):
        projects = []
        for binding in self.bindings():
            try:
                path = self.allowed(binding["path"])
                store = sapper.Store(path / ".sapper")
                item = {**binding, "name": binding.get("name") or path.name,
                        "relative_path": str(path), "status": "not_enabled", "facts": 0,
                        "open_intents": 0, "enabled": False, "mode": "workspace"}
                if store.exists():
                    self.app.inside(store.root, path)
                    with store.locked():
                        project = store.load_project()
                        item.update(goal=project["goal"], status=project["status"], enabled=True,
                                    facts=sum(f["id"] != "goal" for f in store.list_facts()),
                                    open_intents=sum(sapper.is_open(i) for i in store.list_intents()))
                projects.append(item)
            except (WorkspaceError, sapper.FGError, OSError):
                continue
        return projects

    def directories(self, path=""):
        if not path:
            # Starting points for the picker, not an allowlist. Binding an
            # existing directory is an authenticated Web action.
            roots = dict.fromkeys([self.app.root, Path.home().resolve(),
                                   Path(self.app.root.anchor), *self.app.workspace_roots])
            available = []
            for root in roots:
                try:
                    available.append(str(self.allowed(root)))
                except (WorkspaceError, OSError):
                    continue
            return {"roots": available}
        directory = self.allowed(path)
        children = []
        for p in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if p.name.startswith(".") or not p.is_dir():
                continue
            try:
                resolved = self.allowed(p)
                children.append({"name": p.name, "path": str(resolved),
                                 "has_graph": (resolved / ".sapper/project.json").exists()})
            except (WorkspaceError, OSError):
                continue
            if len(children) >= 200:
                break
        return {"path": str(directory),
                "parent": str(directory.parent) if directory.parent != directory else None,
                "directories": children}

    def bind(self, data):
        value = text(data, "path")
        if not value:
            raise WorkspaceError("请选择执行机上的目录")
        path = self.allowed(value)
        with self.app.config_lock():
            cfg = self.app.config()
            existing = next((p for p in cfg.get("workspaces", []) if p["path"] == str(path)), None)
            if existing:
                return existing
            discovered = next((p for p in self.app._scan_projects() if p["path"] == str(path)), None)
            record = {"id": discovered["id"] if discovered else uuid.uuid4().hex[:16],
                      "path": str(path), "name": text(data, "name", path.name, 100) or path.name,
                      "goal": text(data, "goal", path.name) or path.name,
                      "created_at": sapper.now_iso()}
            cfg.setdefault("workspaces", []).append(record)
            sapper.atomic_write_json(self.app.config_path, cfg)
        self.app.audit("workspace.bound", workspace=record["id"], path=str(path))
        return record

    def inspect(self, pid):
        project = self.app.project(pid)
        root = Path(project["path"])
        settings = root / ".claude/settings.json"
        hooks = read(settings, {}).get("hooks", {})
        hook_file = str(REPO / "lib/sapper-hook.py")
        installed = {event: any(hook_file in h.get("command", "")
                                for group in hooks.get(event, []) for h in group.get("hooks", []))
                     for event in ("SessionStart", "UserPromptSubmit")}
        bridge = root / ".claude/sapper-workspace.md"
        return {"workspace": project, "graph": (root / ".sapper/project.json").is_file(),
                "auto_submit_flags": self.auto_submit_flags(pid),
                "hooks": installed, "instructions": bridge.exists(),
                "claude_md": (root / "CLAUDE.md").exists(), "agents_md": (root / "AGENTS.md").exists(),
                "claude": self.capabilities(), "path": str(root),
                "role_catalog": sapper_roles.catalog(),
                "role_prompt_version": sapper_roles.PROMPT_VERSION}

    def auto_submit_flags(self, pid):
        store = self.app.store(pid)
        return store.exists() and store.load_project().get("auto_submit_flags") is True

    @serialized
    def save_settings(self, pid, data):
        if set(data) != {"auto_submit_flags"} or type(data["auto_submit_flags"]) is not bool:
            raise WorkspaceError("auto_submit_flags 必须为布尔值")
        store = self.app.store(pid)
        store.require()
        with store.locked():
            project = store.load_project()
            project["auto_submit_flags"] = data["auto_submit_flags"]
            store.save_project(project)
        self.app.audit("workspace.settings", workspace=pid, auto_submit_flags=data["auto_submit_flags"])
        return {"auto_submit_flags": data["auto_submit_flags"]}

    @serialized
    def enable(self, pid, data):
        project = self.app.project(pid)
        root = Path(project["path"])
        # Check every managed path before invoking setup. Never follow a bridge
        # symlink out of the selected workspace or replace a user-owned file.
        for rel in (".sapper", ".claude", ".claude/settings.json", "AGENTS.md", "CLAUDE.md", ".claude/sapper-workspace.md"):
            self.app.inside(root / rel, root)
        bridge = root / ".claude/sapper-workspace.md"
        if bridge.exists() and not bridge.read_text().startswith("<!-- generated by sapper web -->"):
            raise WorkspaceError(".claude/sapper-workspace.md 已有用户内容，请先选择其他文件名或移开该文件", 409)
        claude = root / "CLAUDE.md"
        old = claude.read_text() if claude.exists() else ""
        if (START_MARK in old) != (END_MARK in old):
            raise WorkspaceError("CLAUDE.md 的 sapper 受管块不完整，请先修复标记", 409)
        settings = root / ".claude/settings.json"
        if settings.exists():
            sapper._load_settings_patch().validate(settings)
        goal = text(data, "goal", project["goal"]) or project["goal"]
        proc = subprocess.run([sys.executable, str(REPO / "lib/sapper.py"), "--store", str(root / ".sapper"),
                               "setup", "--goal", goal, "--agents"], cwd=root,
                              capture_output=True, text=True, timeout=15)
        if proc.returncode:
            raise WorkspaceError("项目级启用失败：" + proc.stderr.strip())
        agents = root / "AGENTS.md"
        same = agents.exists() and claude.exists() and agents.samefile(claude)
        spec = sapper._AGENTS_SPEC.format(store=str(root / ".sapper"), marker=sapper._AGENTS_MARKER)
        content = "<!-- generated by sapper web -->\n"
        if not same:
            content += "@../AGENTS.md\n\n"
        if sapper._AGENTS_MARKER not in agents.read_text():
            content += spec
        else:
            content += "Use the original sapper instructions and shared graph above.\n"
        bridge.write_text(content)
        managed = f"{START_MARK}\n@.claude/sapper-workspace.md\n{END_MARK}"
        if START_MARK in old:
            if END_MARK not in old:
                raise WorkspaceError("CLAUDE.md 的 sapper 受管块不完整，请先修复标记", 409)
            new = re.sub(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), lambda m: managed, old, flags=re.S)
        else:
            new = old + ("\n\n" if old else "") + managed + "\n"
        if new != old:
            claude.write_text(new)
        self.app.audit("workspace.enabled", workspace=pid)
        return self.inspect(pid)

    def capabilities(self):
        if self._capabilities is not None:
            return self._capabilities
        try:
            proc = subprocess.run([self.app.claude_bin, "--help"], capture_output=True, text=True, timeout=5)
            output = proc.stdout + proc.stderr
            self._capabilities = {"available": proc.returncode == 0, "binary": self.app.claude_bin,
                                  **{name: flag in output for name, flag in (
                                      ("session_id", "--session-id"), ("resume", "--resume"),
                                      ("add_dir", "--add-dir"), ("append_system", "--append-system-prompt"),
                                      ("stream_input", "--input-format"), ("replay_user", "--replay-user-messages"),
                                      ("system_snapshot", "--system-prompt-snapshot"),
                                      ("mcp_config", "--mcp-config"), ("strict_mcp_config", "--strict-mcp-config"),
                                      ("plugin_dir", "--plugin-dir"), ("setting_sources", "--setting-sources"),
                                      ("settings", "--settings"))}}
            version = subprocess.run([self.app.claude_bin, "--version"], capture_output=True, text=True, timeout=5)
            match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version.stdout)
            self._capabilities["version"] = match.group(0) if match else "unknown"
            # The full settings/skills isolation contract is tested at this floor.
            self._capabilities["skill_isolation"] = bool(match and tuple(map(int, match.groups())) >= (2, 1, 318))
        except (OSError, subprocess.TimeoutExpired):
            self._capabilities = {"available": False, "binary": self.app.claude_bin}
        return self._capabilities

    def agent_path(self, aid):
        if not re.fullmatch(r"[a-f0-9]{16}", aid):
            raise WorkspaceError("agent ID 不正确")
        return self.app.state / "agents" / (aid + ".json")

    def agent(self, aid, *, include_deleted=False):
        record = read(self.agent_path(aid))
        if record is None or (record.get("deleted_at") and not include_deleted):
            raise WorkspaceError("角色会话不存在", 404)
        return record

    def save_agent(self, record):
        sapper.atomic_write_json(self.agent_path(record["id"]), record)

    def main_agent(self, pid, *, agents=None):
        """One explicit primary identity per workspace, independent of run history."""
        rows = [a for a in (self.agents(pid) if agents is None else agents)
                if a["role"] == "commander" and not a.get("legacy")]
        saved = read(self.app.state / "main-agents" / f"{pid}.json", {})
        current = next((a for a in rows if a["id"] == saved.get("agent_id")), None)
        active = [a for a in rows if a.get("desired_state") == "running"]
        if len(active) > 1:
            raise WorkspaceError("工作区有多个活动指挥官，请先停止多余角色后选择主 agent", 409)
        return active[0] if active else current

    def resume_model_info(self, agent, *, config=None):
        config = self.app.config() if config is None else config
        profiles = {p["id"]: p for p in config["profiles"]}
        original_id = agent.get("profile_id", "")
        defaults = {**config.get("role_profiles", {}),
                    **config.get("workspace_role_profiles", {}).get(agent["workspace_id"], {})}
        current_id = defaults.get(agent["role"], "")

        def label(pid, snapshot=None):
            if not pid:
                return "继承原环境"
            profile = snapshot or profiles.get(pid)
            if not profile:
                return "已移除的模型配置（" + pid[:8] + "）"
            return profile["name"] + " · " + profile["model"]

        return {"original_profile_id": original_id,
                "original_label": label(original_id, agent.get("profile_snapshot")),
                "current_profile_id": current_id, "current_label": label(current_id)}

    def chat_view(self, pid, before=None, agent_id=None):
        rows = [a for a in self.agents(pid) if a["role"] == "commander" and not a.get("legacy")]
        rows.sort(key=lambda a: a.get("created_at", ""), reverse=True)
        error = None
        try:
            main = self.main_agent(pid)
        except WorkspaceError as exc:
            main, error = None, str(exc)
        selected = next((a for a in rows if a["id"] == agent_id), None) if agent_id else main or (rows[0] if rows else None)
        if agent_id and selected is None:
            raise WorkspaceError("历史会话不属于当前工作区", 403)
        fields = ("id", "name", "status", "desired_state", "pane_id", "execution_mode", "created_at")
        view = {"agent": None, "messages": [], "requests": [], "phase": "offline", "has_more": False}
        if selected:
            if selected.get("execution_mode") == "chat":
                view.update(ChatJournal(self.app.state, selected["id"]).view(before))
            else:
                try:
                    view.update(self.conversations.page(self.conversations.messages(selected), before))
                except OSError:
                    error = "无法读取该会话的原生历史文件"
            view["agent"] = {k: selected.get(k) for k in fields}
            view["control"] = self.commander.view(selected["id"])
        view["primary_agent"] = {k: main.get(k) for k in fields} if main else None
        view["sessions"] = [{k: a.get(k) for k in fields} for a in rows]
        view["read_only"] = bool(selected and (not main or selected["id"] != main["id"] or selected.get("execution_mode") != "chat"))
        view["can_resume"] = bool(selected and selected.get("can_resume") and
                                   not (main and main["id"] != selected["id"] and main.get("pane_id")))
        if not selected or not selected.get("pane_id") or view["read_only"] or selected.get("desired_state") != "running":
            view.update(phase="offline", requests=[])
        if error:
            view["error"] = error
        return json.loads(self.app.redact(json.dumps(view, ensure_ascii=False)))

    @serialized
    def chat_message(self, pid, data):
        main = self.main_agent(pid)
        if not main or not main.get("pane_id") or main.get("desired_state") != "running":
            raise WorkspaceError("请先派发或恢复主 agent；输入内容可以继续保留", 409)
        if main.get("execution_mode") != "chat":
            raise WorkspaceError("该会话使用原生交互，请在“终端与文件”中输入消息", 409)
        journal = ChatJournal(self.app.state, main["id"])
        if "question_id" in data:
            result = journal.answer(text(data, "question_id", limit=200), main["run_id"],
                                    answers=data.get("answers"), allow=data.get("allow"))
        else:
            result = journal.enqueue(text(data, "text"), data.get("message_id"))
        if self.commander.view(main["id"])["control"] in ("waiting_user", "completed"):
            self.commander.configure(main["id"], "active", "用户已回复主 agent")
        self.app.audit("chat.queued", workspace=pid, agent=main["id"], message=result["id"])
        return result

    @serialized
    def dispatch(self, pid, data):
        """Web dispatch requests go through the primary; it chooses child strategy."""
        data = {**data, "request_id": data.get("request_id") or uuid.uuid4().hex}
        project = self.app.project(pid)
        role = data.get("role", "commander")
        if role not in ROLES:
            raise WorkspaceError("角色不正确")
        mode = data.get("execution_mode", "tui")
        if mode not in ("tui", "chat"):
            raise WorkspaceError("主 agent 需要交互式会话")
        new_session = data.get("new_session", False)
        if type(new_session) is not bool:
            raise WorkspaceError("new_session 必须为布尔值")
        if new_session and role != "commander":
            raise WorkspaceError("新建主会话时请选择主 agent／指挥官")
        if data.get("workspace_scope", [pid]) != [pid]:
            raise WorkspaceError("角色只能管理当前工作区", 403)
        if role != "commander" and not text(data, "mission"):
            raise WorkspaceError("请填写派发任务说明")
        main = self.main_agent(pid)
        if new_session:
            if data.get("request_id"):
                previous = next((r for r in self.app.runs(pid)
                                 if r.get("request_id") == data["request_id"] and r.get("agent_id")), None)
                if previous:
                    return previous
            if main:
                if main.get("pane_id") or main.get("status") not in TERMINAL_STATES:
                    raise WorkspaceError("主 agent 仍在运行，请先停止后再新建会话", 409)
                self.mark_stopped(main["id"])
                main = None
        info = self.inspect(pid)
        if not (info["graph"] and info["instructions"] and all(info["hooks"].values())):
            self.enable(pid, {})
        if main:
            if main.get("execution_mode") != mode and main.get("pane_id"):
                self.stop_agent(main["id"])
                main = self.agent(main["id"])
            if not main.get("pane_id") or main.get("desired_state") != "running":
                resume_data = {"execution_mode": mode, "request_id": data["request_id"]}
                if "skip_permissions" in data:
                    resume_data["skip_permissions"] = data["skip_permissions"]
                run = self.resume(main["id"], resume_data)
            else:
                self.start_watchdog(pid)
                run = self.app.run_info(main["run_id"])
        else:
            run = self.create_session(pid, {**data, "role": "commander", "parent_agent_id": None,
                "execution_mode": mode, "workspace_scope": [pid],
                "mission": text(data, "mission", project["goal"]) if role == "commander" else project["goal"], "name": "主 agent"})
        message = None
        if role != "commander":
            mission = text(data, "mission")
            if not mission:
                raise WorkspaceError("请填写派发任务说明")
            message = f"派发请求：希望由{ROLES[role]}处理以下任务。请核对目标、依据和现有执行者后决定如何派发，有需我决定的问题先提问。\n{mission}"
        elif main and text(data, "mission") and text(data, "mission") != main.get("mission"):
            message = "请核对并调整当前任务：\n" + text(data, "mission")
        if message:
            self.message(run["agent_id"], {"text": message, "message_id": data.get("request_id")})
        return run

    def execution_view(self, agent):
        """Observe native phases without giving the status reader scheduling authority."""
        with self.app.lock:
            data = self._execution_observations.setdefault(agent["id"], {})
            try:
                self.commander.observe(agent, data)
            except (OSError, ValueError):
                data.update(phase="unknown", caught_up=False)
            return {"phase": data.get("phase", "unknown") if data.get("caught_up") else "unknown",
                    "last_event_at": data.get("last_event_at")}

    def agents(self, pid=None, *, run_records=None, pane_records=None):
        all_panes = self.app.panes() if pane_records is None else pane_records
        panes = {p["session"]: p for p in all_panes}
        runs = {r["id"]: r for r in (self.app.runs() if run_records is None else run_records)}
        results = []
        hidden_panes = set()
        context = self.app.global_context()
        config = self.app.config()
        for path in (self.app.state / "agents").glob("*.json"):
            a = read(path)
            if a.get("deleted_at"):
                # Unregistering an attached terminal must not rediscover it as
                # an unlabelled role. The terminal itself remains accessible.
                hidden_panes.update(p["id"] for p in all_panes
                                    if p["id"] == a.get("external_pane")
                                    and p["session"] == a.get("external_session"))
                continue
            if pid and pid != a["workspace_id"]:
                continue
            run = runs.get(a.get("run_id"), {})
            if not run and a.get("run_id"):
                try:
                    run = self.app.run_info(a["run_id"])
                except sapper.FGError:
                    pass
            pane = panes.get(run.get("session") or a.get("external_session"))
            if a.get("external_pane"):
                pane = next((p for p in all_panes if p["id"] == a["external_pane"]), None)
            if pane and a.get("external_pane") and pane["id"] != a["external_pane"]:
                pane = None
            results.append({**a, "status": run.get("status", "unknown"), "exit_code": run.get("exit_code"),
                            "process_skip_permissions": run.get("effective_skip_permissions", a.get("skip_permissions")),
                            "activity": run.get("activity", {}), "deadline_epoch": run.get("deadline_epoch"),
                            "stop_reason": run.get("stop_reason"),
                            "timeout_seconds": run.get("timeout_seconds"), "supervisor_version": run.get("supervisor_version"),
                            "pane_id": pane["id"] if pane else None,
                            "role_label": ROLES.get(a["role"], a["role"]),
                            "can_resume": bool(a.get("claude_session_id")) and not pane})
            if results[-1]["can_resume"]:
                results[-1]["resume_model"] = self.resume_model_info(a, config=config)
            if a["role"] == "commander" and not a.get("legacy") and a.get("execution_mode") in ("tui", "chat"):
                results[-1]["commander_monitor"] = self.commander.view(a["id"])
            if (a.get("execution_mode") == "tui" and a.get("claude_session_id") and pane
                    and not pane.get("dead") and run.get("status") not in TERMINAL_STATES):
                results[-1]["execution_observation"] = self.execution_view(a)
            checked = sapper_context.receipt(self.app.state, a["id"])
            results[-1]["shared_context"] = {"launch_revision": a.get("context_revision", 0),
                "hook_revision": checked.get("revision", 0) if checked else 0,
                "latest_revision": context["revision"], "enabled": context["enabled"],
                "hook_checked_at": checked.get("checked_at") if checked else None}
            if a.get("external_session") and pane:
                results[-1]["status"] = "running" if not pane["dead"] else "exited"
        known_panes = {a.get("pane_id") for a in results} | hidden_panes
        service_sessions = {r.get("session") for r in runs.values() if r.get("role") == "watchdog"}
        for pane in all_panes:
            if pane["id"] in known_panes or pane["session"] in service_sessions or (pid and pane["project_id"] != pid):
                continue
            results.append({"id": "legacy-" + pane["id"][1:], "workspace_id": pid or pane["project_id"],
                            "role": pane.get("role", "unassigned"), "role_label": "原指挥官" if pane.get("role") == "commander" else "角色未标注",
                            "name": pane["name"], "profile_name": "原配置（未登记）", "execution_mode": "tui",
                            "cwd": pane["path"], "pane_id": pane["id"], "mission": "已有终端，未推断其职责或父子关系",
                            "claude_session_id": None, "created_at": "", "legacy": True, "can_resume": False,
                            "status": "exited" if pane["dead"] else "running"})
        return sorted(results, key=lambda a: a["created_at"])

    @serialized
    def attach(self, pid, data):
        pane = self.app.pane(text(data, "pane_id"))
        if pane["project_id"] != pid:
            raise WorkspaceError("该终端不属于所选工作区", 403)
        if any(a.get("pane_id") == pane["id"] and not a["id"].startswith("legacy-") for a in self.agents()):
            raise WorkspaceError("该终端已经登记，无需重复接入", 409)
        role = data.get("role", "worker")
        if role not in ROLES:
            raise WorkspaceError("角色不正确")
        sid = text(data, "claude_session_id") or None
        if sid:
            try:
                sid = str(uuid.UUID(sid))
            except ValueError as exc:
                raise WorkspaceError("原生会话 ID 应为 UUID；未知时留空，不猜测最近会话") from exc
        record = {"id": uuid.uuid4().hex[:16], "workspace_id": pid, "workspace_scope": [pid],
                  "role": role, "role_source": "user-attached", "legacy": True,
                  "name": text(data, "name", pane["name"], 100), "parent_agent_id": None,
                  "cwd": pane["path"], "claude_session_id": sid, "external_session": pane["session"],
                  "task_label": text(data, "task_label", limit=18),
                  "external_pane": pane["id"], "execution_mode": "tui", "mission": text(data, "mission", "已有会话，用户显式登记"),
                  "created_at": sapper.now_iso(), "run_ids": [], "profile_id": "", "profile_name": "原会话配置",
                  "desired_state": "running", "minutes": 10}
        self.save_agent(record)
        return record

    def role_prompt(self, role, project, scope=None):
        scope = [project["id"]]
        projects = [self.app.project(wid) for wid in scope]
        prompt = sapper_roles.render(
            role, root=Path(project["path"]),
            worker_script=REPO / "docker/commander/worker.sh",
            workspace_id=project["id"], workspace_name=project["name"],
            workspace_scope=[{"id": p["id"], "name": p["name"]} for p in projects])
        policies = [f"Workspace {p['id']} ({p['name']}):\n" + sapper.flag_submission_policy(
            {"auto_submit_flags": self.auto_submit_flags(p["id"])}) for p in projects]
        return prompt + "\n\n" + "\n\n".join(policies)

    def role_info(self, pid, role, scope=None):
        if role not in ROLES:
            raise WorkspaceError("角色不正确")
        project = self.app.project(pid)
        scope = list(dict.fromkeys(scope or [pid]))
        if scope != [pid]:
            raise WorkspaceError("角色只能管理当前工作区", 403)
        for wid in scope:
            self.app.project(wid)
        prompt = self.role_prompt(role, project, scope)
        spec = sapper_roles.ROLE_SPECS[role]
        return {"id": role, "label": spec.label, "summary": spec.summary,
                "sources": list(spec.sources), "prompt_version": sapper_roles.PROMPT_VERSION,
                "prompt": prompt}

    def strategy_board(self, scope):
        """Real workspace evidence needed by the native commander doctrine.

        This is deliberately an observation adapter, not a scheduler: it reports
        graph and process state without inferring that a quiet role is stuck.
        """
        agents = []
        seen = set()
        for wid in scope:
            for agent in self.agents(wid):
                if agent["id"] in seen:
                    continue
                seen.add(agent["id"])
                agents.append(agent)
        compact_agents = []
        for agent in agents:
            row = {k: agent.get(k) for k in (
                "id", "workspace_id", "workspace_scope", "parent_agent_id", "role",
                "role_label", "name", "task_label", "status", "pane_id", "execution_mode", "intent_id",
                "created_at", "minutes", "desired_state", "timeout_seconds", "deadline_epoch", "activity", "stop_reason")}
            row["mission"] = (agent.get("mission") or "")[:500]
            compact_agents.append(row)
        workspaces = []
        for wid in scope:
            project = self.app.project(wid)
            try:
                graph = self.app.graph(wid)
                facts, intents = graph["facts"], graph["intents"]
                by_intent = {i["id"]: i for i in intents}
                for agent in compact_agents:
                    if agent["workspace_id"] == wid and agent["intent_id"] in by_intent:
                        assignment = by_intent[agent["intent_id"]]
                        agent.update(intent_status=assignment["status"], result_fact=assignment["to"])
                kinds, flags = {}, {}
                help_notes, reports, latest = [], [], ""
                for fact in facts:
                    if fact.get("id") == "goal":
                        continue
                    kind = fact.get("kind") or "untyped"
                    kinds[kind] = kinds.get(kind, 0) + 1
                    if kind == "flag":
                        status = (fact.get("data") or {}).get("status") or "recorded"
                        flags[status] = flags.get(status, 0) + 1
                    created = fact.get("created_at") or ""
                    latest = max(latest, created)
                    searchable = " ".join((str(fact.get("title") or ""),
                                           str(fact.get("description") or ""),
                                           json.dumps(fact.get("data") or {}, ensure_ascii=False)))
                    row = {"id": fact.get("id"), "creator": fact.get("creator"),
                           "created_at": created,
                           "summary": (fact.get("title") or fact.get("description") or "")[:300]}
                    if "[HELP]" in searchable:
                        help_notes.append(row)
                    if "[REPORT]" in searchable:
                        reports.append(row)
                open_intents = []
                for intent in intents:
                    latest = max(latest, intent.get("created_at") or "",
                                 intent.get("claimed_at") or "", intent.get("concluded_at") or "")
                    if sapper.is_open(intent):
                        open_intents.append({k: intent.get(k) for k in (
                            "id", "title", "description", "status", "worker",
                            "creator", "created_at", "requires_confirmation", "confirmed_at", "from")})
                graph_data = {"status": graph["project"]["status"], "goal": graph["project"]["goal"],
                              "auto_submit_flags": graph["project"].get("auto_submit_flags") is True,
                              "facts_total": sum(f.get("id") != "goal" for f in facts),
                              "kinds": kinds, "flags": flags, "open_intents": open_intents[:20],
                              "help": help_notes[-10:], "reports": reports[-10:],
                              "last_graph_activity": latest or None}
            except (sapper.FGError, OSError, KeyError, TypeError) as exc:
                graph_data = {"status": "unavailable", "goal": project.get("goal", ""),
                              "error": str(exc)}
            workspaces.append({"workspace_id": wid, "name": project["name"],
                               "path": project["path"], **graph_data,
                               "agents": [a for a in compact_agents if wid in a.get("workspace_scope", [a["workspace_id"]])]})
        try:
            maximum = int(os.environ.get("SAPPER_MAX_WORKERS", "8"))
        except ValueError:
            maximum = None
        return {"generated_at": sapper.now_iso(), "workspaces": workspaces,
                "agents": compact_agents,
                "limits": {"max_workers": maximum, "global_budget": None},
                "observation_note": "进程存活、终端输出和图进展分别报告；本看板不把安静状态自动判为卡死。"}

    @serialized
    def create_session(self, pid, data, existing=None, *, inherited_permission_source=None):
        data = {**data, "request_id": data.get("request_id") or uuid.uuid4().hex}
        previous_prompt = (existing or {}).get("role_prompt_sha256")
        project = self.app.project(pid)
        root = Path(project["path"])
        if not (root / ".sapper/project.json").is_file():
            raise WorkspaceError("请先在工作区点击“启用 sapper”，再启动角色会话", 409)
        if not (root / ".claude/sapper-workspace.md").exists():
            raise WorkspaceError("请先启用工作区接入，以载入原 sapper 的 hooks 与说明", 409)
        if data.get("request_id") and not existing:
            for run in self.app.runs(pid):
                if run.get("request_id") == data["request_id"] and run.get("agent_id"):
                    return run
        cap = self.capabilities()
        if not cap.get("available") or not cap.get("session_id") or not cap.get("resume"):
            raise WorkspaceError("当前 Claude CLI 缺少明确会话 ID／恢复能力，请选择支持这些参数的原 CLI；不会自动升级", 409)
        if not cap.get("append_system"):
            raise WorkspaceError("该 Claude CLI 不支持 --append-system-prompt，无法可靠加载角色基础职责；请使用支持该参数的原 CLI", 409)
        isolated = not existing or bool(existing.get("extension_mode") or existing.get("session_storage"))
        if isolated and (not cap.get("skill_isolation") or any(not cap.get(k) for k in REQUIRED_FLAGS)):
            raise WorkspaceError("会话资源隔离需要 Claude CLI 2.1.318+ 及 --mcp-config、--strict-mcp-config、--plugin-dir、--setting-sources、--settings；不会静默降级或自动升级", 409)
        role = (existing or {}).get("role") or data.get("role", "worker")
        if role not in ROLES:
            raise WorkspaceError("角色不正确")
        mode = (data.get("execution_mode") if existing and role == "commander" else None) or (existing or {}).get("execution_mode") or data.get("execution_mode", "tui")
        if mode not in ("tui", "print", "chat") or (mode == "chat" and role != "commander"):
            raise WorkspaceError("执行形式不正确")
        if role == "commander" and mode == "print":
            raise WorkspaceError("主 agent 需要持续会话，请使用网页聊天或交互式会话")
        skip_permissions = data.get("skip_permissions", (existing or {}).get("skip_permissions", False))
        if type(skip_permissions) is not bool:
            raise WorkspaceError("skip_permissions 必须为布尔值")
        permissions = permission_audit(data, existing, skip_permissions,
                                       inherited_source=inherited_permission_source)
        if mode == "chat" and not all(cap.get(k) for k in ("stream_input", "replay_user")):
            raise WorkspaceError("当前 Claude CLI 缺少网页对话所需的结构化输入与消息回执能力", 409)
        parent = (existing or {}).get("parent_agent_id") or data.get("parent_agent_id") or None
        if parent:
            parent_record = self.agent(parent)
            if parent_record.get("desired_state") != "running":
                raise WorkspaceError("父角色已停止，不再派发新的子任务", 409)
            if pid != parent_record["workspace_id"]:
                raise WorkspaceError("父角色不属于这个工作区", 403)
            if not existing:
                try:
                    maximum = int(os.environ.get("SAPPER_MAX_WORKERS", "8"))
                except ValueError as exc:
                    raise WorkspaceError("SAPPER_MAX_WORKERS 必须是整数") from exc
                active = [a for a in self.agents(pid) if a.get("pane_id") and a["role"] != "commander" and not a["id"].startswith("legacy-")]
                if len(active) >= maximum:
                    raise WorkspaceError("原 worker 并发上限已达到", 409)
        aid = existing["id"] if existing else uuid.uuid4().hex[:16]
        if role == "commander":
            if parent:
                raise WorkspaceError("主 agent 不能作为子角色派发")
            others = [a for a in self.agents(pid) if a["role"] == "commander"
                      and not a.get("legacy") and a["id"] != aid
                      and a.get("desired_state") == "running"]
            if others:
                raise WorkspaceError("工作区已有主 agent，请使用现有终端或恢复原会话", 409)
        sid = existing["claude_session_id"] if existing else str(uuid.uuid4())
        # A resume retains its original route, including an empty environment
        # profile. Role defaults only select routes for newly created sessions.
        profile_id = existing.get("profile_id", "") if existing else self.app.role_profile_id(
            role, data, parent_record.get("profile_id", "") if parent else "", workspace_id=pid)
        profile = next((p for p in self.app.config()["profiles"] if p["id"] == profile_id), None)
        if profile_id and profile is None:
            raise WorkspaceError("角色模型配置已不存在，请先选择配置", 409)
        if profile and profile.get("engine_prefix"):
            raise WorkspaceError("原 -p 包装器不一定支持交互式 Claude；此入口请选择原环境或普通模型配置", 409)
        minutes = data.get("minutes", (existing or {}).get("minutes", 10))
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
            raise WorkspaceError("minutes 必须在 1–1440 之间")
        snapshot = {k: profile.get(k, "anthropic" if k == "protocol" else "") for k in ("name", "model", "base_url", "protocol")} if profile else None
        if existing and existing.get("profile_snapshot"):
            old = existing["profile_snapshot"]
            if not snapshot or any(old.get(k, "anthropic" if k == "protocol" else "") != snapshot[k] for k in ("model", "base_url", "protocol")):
                raise WorkspaceError("模型、协议或网关地址已变化，不能静默改变原会话；请还原配置或新建角色会话", 409)
        if existing:
            for a in self.agents():
                if a["claude_session_id"] == sid and a.get("pane_id"):
                    if a.get("run_id"):
                        if role == "commander":
                            self.start_watchdog(pid)
                        return self.app.run_info(a["run_id"])
                    raise WorkspaceError("已接入的原会话仍在运行", 409)
        location = data.get("location", "root" if role == "worker" and not parent else "agent")
        cwd = Path(existing["cwd"]) if existing else root if location == "root" else root / ".sapper-agents" / aid
        self.app.inside(cwd, root)
        scope = data.get("workspace_scope", [pid]) if not existing else [pid]
        if not isinstance(scope, list) or any(not isinstance(i, str) for i in scope):
            raise WorkspaceError("工作区范围格式不正确")
        for wid in scope:
            self.app.project(wid)
        if scope != [pid]:
            raise WorkspaceError("角色只能管理当前工作区", 403)
        iid = existing.get("intent_id") if existing else text(data, "intent_id")
        from_ids = [] if existing else data.get("from", [])
        if isinstance(from_ids, str):
            from_ids = sapper.parse_from(text(data, "from"))
        if not isinstance(from_ids, list) or any(not isinstance(f, str) for f in from_ids):
            raise WorkspaceError("派单依据必须是 fact ID 列表")
        if iid and from_ids:
            raise WorkspaceError("派单请选择已有 intent 或依据 fact，两者只需一种")
        if parent and not existing and not (iid or from_ids):
            raise WorkspaceError("派单需要 --intent iNNN 或 --from fNNN；首次探索请先记录题面或附件等已知事实")
        store = self.app.store(pid)
        intent = store.get_intent(iid) if iid else None
        mission = (existing or {}).get("mission") or text(data, "mission")
        if intent and not existing:
            briefing = mission if mission != intent["description"] else ""
            mission = intent["description"] + ("\n派发补充：" + briefing if briefing else "")
        mission = mission or project["goal"]
        context = self.app.global_context()
        shared = sapper_context.prompt(context)
        role_prompt = self.role_prompt(role, project, scope)
        record = existing or {"id": aid, "workspace_id": pid, "workspace_scope": scope,
                              "role": role, "role_source": "original-launch-parameters",
                              "parent_agent_id": parent, "claude_session_id": sid,
                              "execution_mode": mode, "cwd": str(cwd), "mission": mission,
                              "task_label": text(data, "task_label", limit=18),
                              "profile_id": profile_id, "profile_name": profile["name"] if profile else "继承原环境",
                              "created_at": sapper.now_iso(), "run_ids": [],
                              "name": text(data, "name", ROLES[role], 100), "profile_snapshot": snapshot,
                              "skip_permissions": skip_permissions,
                              "minutes": minutes}
        record.setdefault("claude_config_dir", str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")))
        record["workspace_scope"] = [pid]
        record["execution_mode"] = mode
        if isolated:
            record["extension_mode"] = "isolated-v1"
        record["desired_state"] = "running"
        record["role_prompt_version"] = sapper_roles.PROMPT_VERSION
        record["role_prompt_sources"] = list(sapper_roles.ROLE_SPECS[role].sources)
        record["context_revision"] = context["revision"] if shared and cap.get("append_system") else 0
        record["minutes"] = minutes
        for key in ("external_session", "external_pane"):
            record.pop(key, None)
        # Keep the same graph identity across supervised runs. Process exit
        # alone never concludes an intent.
        active_intent = None
        if from_ids:
            active_intent = store.add_intent(from_ids, mission, parent or "user", assigned_worker=aid)
        elif iid and not (existing and intent["to"] is not None):
            active_intent = store.claim_intent(iid, aid)
        try:
            extension_argv, extension_env, instructions = self.extensions.prepare(record, data, resume=bool(existing)) if isolated else ([], {}, "")
            if active_intent:
                iid = record["intent_id"] = active_intent["id"]
                role_prompt += (f"\nAssigned graph intent: {iid}; source facts: {', '.join(active_intent['from'])}. "
                                "Read these before work. SAPPER_INTENT_ID identifies your assigned result.")
            elif iid:
                role_prompt += f"\nPrevious graph intent {iid} already concluded to {intent['to']}."
            record["role_prompt_sha256"] = hashlib.sha256(role_prompt.encode()).hexdigest()
            if existing and cap.get("system_snapshot") and previous_prompt != record["role_prompt_sha256"]:
                # Newer native CLIs otherwise keep the old append-system prompt
                # across resume, even when an explicit role update was supplied.
                record["system_prompt_snapshot"] = "off"
            argv = [self.app.claude_bin, "--resume" if existing else "--session-id", sid]
            if record.get("system_prompt_snapshot") and cap.get("system_snapshot"):
                argv += ["--system-prompt-snapshot", record["system_prompt_snapshot"]]
            argv += extension_argv
            if profile:
                argv += ["--model", profile["model"]]
            if skip_permissions:
                argv += ["--dangerously-skip-permissions"]
            if cwd != root and cap.get("add_dir"):
                argv += ["--add-dir", str(root)]
            argv += ["--append-system-prompt", "\n\n".join(p for p in (role_prompt, shared, instructions) if p)]
            if mode == "print":
                argv += ["-p", "--output-format", "stream-json", "--verbose"]
            if mode == "chat":
                argv += ["-p", "--output-format", "stream-json", "--input-format", "stream-json",
                         "--permission-prompt-tool", "stdio", "--replay-user-messages", "--verbose"]
            continuation = mission
            if not existing or mode in ("print", "chat") or role == "commander":
                continuation = mission
                if existing:
                    continuation = (text(data, "continuation") if "continuation" in data else
                                    "继续原任务。先核对共享图和已保存文件，保留有效成果，从未完成的步骤继续；不要仅因恢复而重复探索。\n原任务：" + mission)
                if mode != "chat":
                    argv += [continuation]
            if cwd != root and not existing:
                cwd.mkdir(parents=True, exist_ok=False)
                (cwd / ".sapper").symlink_to(root / ".sapper", target_is_directory=True)
                # Hooks are opt-in at exactly this cwd; do not rely on ancestor discovery.
                if not isolated:
                    settings = cwd / ".claude/settings.json"
                    sapper._load_settings_patch().add(settings, str(REPO / "lib/sapper-hook.py"))
                # Relative import avoids whitespace parsing in absolute paths.
                (cwd / "CLAUDE.md").write_text("@../../CLAUDE.md\n")
            self.save_agent(record)
            env = {"SAPPER_MODE": "workspace", "SAPPER_WEB_STATE": str(self.app.state),
                   "SAPPER_INTENT_ID": active_intent["id"] if active_intent else "",
                   "SAPPER_WORKSPACE_ID": pid, "SAPPER_WORKSPACE_PATH": str(root), "SAPPER_AGENT_ID": aid,
                   "SAPPER_PARENT_AGENT_ID": parent or "", "SAPPER_WORKSPACE_ALLOWED_ROOTS": json.dumps([str(r) for r in self.app.workspace_roots]),
                   "SAPPER_CLAUDE_BIN": self.app.claude_bin, "SAPPER_TMUX_SOCKET": self.app.tmux_socket or ""}
            env.update(extension_env)
            if not isolated:
                env["CLAUDE_CONFIG_DIR"] = record["claude_config_dir"]
            env[sapper_context.PROMPT_ENV] = str(record["context_revision"]) if record["context_revision"] else ""
            run = self.app.start(pid, {**data, "kind": "claude-native", "profile_id": profile_id}, native={
                "argv": argv, "cwd": str(cwd), "env": env, "agent_id": aid, "claude_session_id": sid,
                "role": role, "execution_mode": mode, "intent_id": iid or None,
                "permission_audit": permissions,
                "companion_required": role == "commander",
                "isolated_extensions": isolated, "skip_hook_install": isolated,
                "mcp_env_refs": record.get("mcp_env_refs", []),
                "output_format": "stream-json" if mode in ("print", "chat") else None,
                "initial_prompt": continuation if mode == "chat" else None,
                "resume_chat": bool(existing),
                "timeout_seconds": int(record["minutes"]) * 60 if mode == "print" else None})
        except Exception as exc:
            if active_intent:
                store.release_intent(iid, aid, if_owned=True)
            record["desired_state"] = "stopped"
            self.save_agent(record)
            if active_intent:
                raise WorkspaceError(f"启动失败，请查看任务 {iid}；开放任务可用 --intent {iid} 重派：{exc}",
                                     getattr(exc, "status", 409)) from exc
            raise
        record.update(run_id=run["id"], updated_at=sapper.now_iso(), skip_permissions=skip_permissions)
        record["run_ids"].append(run["id"])
        self.save_agent(record)
        if role == "commander":
            sapper.atomic_write_json(self.app.state / "main-agents" / f"{pid}.json", {"agent_id": aid})
            try:
                self.start_watchdog(pid)
            except Exception:
                self.stop_agent(aid)
                raise
        self.app.audit("agent.resumed" if existing else "agent.started", agent=aid, role=role,
                       workspace=pid, run=run["id"], **permissions)
        if not existing and role != "commander":
            self.app.audit("agent.dispatched", agent=aid, role=role, workspace=pid,
                           parent_agent=parent, intent=iid or None, run=run["id"], **permissions)
        return run

    @serialized
    def resume(self, aid, data):
        with self.app.lock:
            a = self.agent(aid)
            if not a.get("claude_session_id"):
                raise WorkspaceError("原生会话 ID 未知，不能猜测恢复；请使用原会话入口", 409)
            if a["role"] == "commander" and a.get("execution_mode") != "chat" and data.get("execution_mode") == "chat":
                ChatJournal(self.app.state, aid).import_history(self.conversations.messages(a))
            try:
                return self.create_session(a["workspace_id"], data, existing=a)
            except sapper.FGError as exc:
                model = self.resume_model_info(a)
                route = f"恢复会话 {aid[:8]} 沿用原模型配置“{model['original_label']}”。"
                route += f"当前角色默认“{model['current_label']}”用于新建会话；恢复时保持原配置。"
                # Keep resource/model error types and HTTP status for existing callers.
                exc.args = (route + "\n" + str(exc),)
                raise

    @serialized
    def extend(self, aid, data):
        a = self.agent(aid)
        if not a.get("run_id"):
            raise WorkspaceError("该角色没有受管运行可续时", 409)
        return self.app.extend(a["run_id"], data)

    def mark_stopped(self, aid):
        # Historical run controls may still refer to a deleted role.
        a = self.agent(aid, include_deleted=True)
        if a.get("deleted_at"):
            return
        a.update(desired_state="stopped", stopped_at=sapper.now_iso())
        self.save_agent(a)

    @serialized
    def stop_agent(self, aid):
        a = self.agent(aid)
        self.mark_stopped(aid)
        if a.get("external_session") and not a.get("run_id"):
            row = next((r for r in self.agents(a["workspace_id"]) if r["id"] == aid), {})
            if row.get("pane_id"):
                return self.app.send(row["pane_id"], {"key": "C-c"}) | {"message": "已发送原中断键；旧 watchdog 仍按原规则运行"}
            return {"status": "not_running"}
        result = self.app._stop(a["run_id"]) if a.get("run_id") else {"status": "not_running"}
        if a["role"] == "commander":
            self.stop_watchdog(a["workspace_id"])
        return result

    @serialized
    def delete_agent(self, aid):
        a = self.agent(aid, include_deleted=True)
        if a.get("deleted_at"):
            return {"deleted_agent": aid}
        # Reuse managed process-group shutdown; external terminals are only
        # unregistered because their lifecycle is not owned by this service.
        if a.get("run_id"):
            self.stop_agent(aid)
        else:
            self.mark_stopped(aid)
        a = self.agent(aid)
        if a.get("intent_id"):
            self.app.store(a["workspace_id"]).release_intent(a["intent_id"], aid, if_owned=True)
        a.update(deleted_at=sapper.now_iso(), desired_state="stopped")
        self.save_agent(a)
        # Surviving children become independent roles and remain resumable.
        # Retain the original relationship for historical attribution.
        detached = []
        for path in (self.app.state / "agents").glob("*.json"):
            child = read(path)
            if (child.get("parent_agent_id") == aid and child.get("workspace_id") == a["workspace_id"]
                    and not child.get("deleted_at")):
                child.update(parent_agent_id=None, deleted_parent_agent_id=aid)
                self.save_agent(child)
                detached.append(child["id"])
        self.app.audit("agent.deleted", agent=aid, workspace=a["workspace_id"], detached_children=detached)
        return {"deleted_agent": aid}

    def message(self, aid, data):
        a = next((a for a in self.agents() if a["id"] == aid), None)
        if a and a.get("execution_mode") == "chat":
            if data.get("sender_id"):
                item = ChatJournal(self.app.state, aid).enqueue(
                    f"[来自工作区角色 {data['sender_id']} 的汇报；作为证据核对]\n" + text(data, "text"), internal=True)
                return {"status": "queued", "message": "汇报已进入主 agent 消息队列", "id": item["id"]}
            return self.chat_message(a["workspace_id"], data)
        if a and a.get("native_operation"):
            self.app.store(a["workspace_id"]).add_hint(text(data, "text"), "web-user")
            return {"status": "saved", "message": "已写入原 hint，后续原生调度步骤读取；不表示当前模型已采用"}
        if a and a.get("execution_mode") == "print":
            item = ChatJournal(self.app.state, aid).enqueue(text(data, "text"))
            return {"status": "queued", "message": "已保存到该角色的定向收件箱", "id": item["id"]}
        if not a or not a.get("pane_id"):
            raise WorkspaceError("该角色当前没有活动终端，请先恢复会话或保存为 hint", 409)
        return self.app.send(a["pane_id"], {"text": text(data, "text")})

    def pause_commander_input(self, pane):
        """A user's native interrupt is also an explicit exit from auto turns."""
        for path in (self.app.state / "agents").glob("*.json"):
            a = read(path)
            if (a.get("role") == "commander" and a.get("execution_mode") == "tui"
                    and a.get("run_id") and not a.get("deleted_at") and not a.get("legacy")
                    and self.app.run_info(a["run_id"]).get("session") == pane["session"]):
                self.commander.configure(a["id"], "paused", "用户通过原生终端中断；可在角色卡片启用续轮")

    @serialized
    def start_watchdog(self, pid):
        if not any(a["role"] == "commander" and not a.get("legacy")
                   and a.get("desired_state") == "running" and a.get("pane_id")
                   for a in self.agents(pid)):
            raise WorkspaceError("watchdog 与主 agent 同时启动，请先派发或恢复主 agent", 409)
        for service in self.services(pid):
            if service["status"] in ("running", "starting"):
                return service
        service_id = uuid.uuid4().hex[:16]
        env = {"SAPPER_MODE": "workspace", "SAPPER_WEB_STATE": str(self.app.state),
               "SAPPER_WORKSPACE_ID": pid, "SAPPER_TMUX_SOCKET": self.app.tmux_socket or "",
               "SAPPER_WORKSPACE_ALLOWED_ROOTS": json.dumps([str(r) for r in self.app.workspace_roots]),
               "SAPPER_CLAUDE_BIN": self.app.claude_bin}
        run = self.app.start(pid, {"kind": "workspace-watchdog"}, native={
            "argv": ["bash", str(REPO / "docker/commander/watchdog.sh")],
            "cwd": self.app.project(pid)["path"], "env": env, "role": "watchdog"})
        service = {"id": service_id, "workspace_id": pid, "run_id": run["id"], "role": "watchdog", "type": "script"}
        sapper.atomic_write_json(self.app.state / "services" / (service_id + ".json"), service)
        return service

    def stop_watchdog(self, pid):
        for service in self.services(pid):
            if service["status"] not in TERMINAL_STATES:
                self.app._stop(service["run_id"])

    def services(self, pid, *, run_records=None, pane_records=None):
        runs = {r["id"]: r for r in (self.app.runs() if run_records is None else run_records)}
        panes = {p["session"]: p["id"] for p in (self.app.panes() if pane_records is None else pane_records)}
        result = []
        for path in (self.app.state / "services").glob("*.json"):
            service = read(path)
            if service["workspace_id"] != pid:
                continue
            run = runs.get(service["run_id"], {})
            if not run:
                try:
                    run = self.app.run_info(service["run_id"])
                except sapper.FGError:
                    pass
            result.append({**service, "status": run.get("status", "unknown"), "pane_id": panes.get(run.get("session"))})
        return result

    @serialized
    def stop_workspace(self, pid):
        # Stop keep-alive first so it cannot race with explicit cancellation.
        for s in self.services(pid):
            self.app._stop(s["run_id"])
        stopped = []
        for a in self.agents(pid):
            if not re.fullmatch(r"[a-f0-9]{16}", a["id"]):
                continue
            self.stop_agent(a["id"])
            stopped.append(a["id"])
        return {"stopped_agents": stopped}


def console_from_environment():
    from sapper_web import Console
    state = os.environ.get("SAPPER_WEB_STATE")
    if not state:
        raise WorkspaceError("工作区模式需要 SAPPER_WEB_STATE")
    roots = json.loads(os.environ.get("SAPPER_WORKSPACE_ALLOWED_ROOTS", "[]"))
    return Console(Path(os.environ.get("SAPPER_ROOT", str(REPO / "runs"))), Path(state),
                   os.environ.get("SAPPER_TMUX_SOCKET") or None,
                   workspace_roots=[Path(p) for p in roots],
                   claude_bin=os.environ.get("SAPPER_CLAUDE_BIN", "claude"))


def worker(argv):
    parser = argparse.ArgumentParser(description="原 worker CLI 的显式本地工作区接入")
    parser.add_argument("action", choices=["spawn", "kill", "msg", "peek", "list", "board", "hint", "extend", "resume", "control", "inbox"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("body", nargs="?")
    parser.add_argument("--mission", default="")
    parser.add_argument("--task-label", default="")
    assignment = parser.add_mutually_exclusive_group()
    assignment.add_argument("--from", dest="from_ids", default="", help="依据的 fact ID，以逗号分隔")
    assignment.add_argument("--intent", default="", help="领取已有 intent ID")
    parser.add_argument("--role", choices=list(ROLES), default=None)
    parser.add_argument("--general", action="store_true")
    parser.add_argument("--minutes", type=int)
    parser.add_argument("--model")
    parser.add_argument("--profile")
    parser.add_argument("--name")
    parser.add_argument("--gw")
    parser.add_argument("--engine", default="claude")
    parser.add_argument("--close", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--request-id", default=None)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--p", "-p", action="store_true")
    modes.add_argument("--tui", action="store_true")
    args = parser.parse_args(argv)
    app = console_from_environment()
    manager = app.workspaces
    wid = os.environ.get("SAPPER_WORKSPACE_ID")
    parent = os.environ.get("SAPPER_AGENT_ID") or None
    if parent and manager.agent(parent)["workspace_id"] != wid:
        raise WorkspaceError("当前角色不属于所选工作区", 403)
    scope = [wid]
    if args.action == "inbox":
        if not parent:
            raise WorkspaceError("inbox 需要当前受管角色身份")
        if manager.agent(parent)["role"] == "commander":
            raise WorkspaceError("主 agent 的消息由网页对话通道投递，请在会话中处理")
        print(json.dumps(ChatJournal(app.state, parent).inbox(), ensure_ascii=False))
        return 0
    if args.action == "list":
        rows, seen = [], set()
        for workspace_id in scope:
            for agent in manager.agents(workspace_id):
                if agent["id"] not in seen:
                    rows.append(agent)
                    seen.add(agent["id"])
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if args.action == "board":
        board = manager.strategy_board(scope)
        print(json.dumps(board, ensure_ascii=False), flush=True)
        if parent:
            manager.commander.record_board(parent, os.environ.get("SAPPER_RUN_ID"), board)
        return 0
    if args.action == "control":
        if not parent:
            raise WorkspaceError("control 需要当前受管指挥官身份")
        print(json.dumps(manager.commander.configure(parent, args.target, args.reason), ensure_ascii=False))
        return 0
    if args.action == "spawn":
        if args.engine != "claude" or args.gw:
            raise WorkspaceError("本地工作区请用角色模型配置；原 --gw/其他引擎分支仍可在原比赛模式使用")
        candidates = [p for p in app.projects() if p["id"] in scope and args.target in (p["id"], p["name"])]
        if len(candidates) != 1:
            raise WorkspaceError("派发目标不在当前指挥官的明确工作区范围内")
        if not (args.from_ids or args.intent):
            raise WorkspaceError("spawn 需要 --from fNNN 或 --intent iNNN。首次派单请先记录题面等已知事实。")
        role = "general" if args.general else args.role or "worker"
        selection = {"profile_id": args.profile} if args.profile is not None else {}
        if args.model:
            base_id = app.role_profile_id(role, selection, manager.agent(parent).get("profile_id", "") if parent else "", workspace_id=candidates[0]["id"])
            base_profile = next((p for p in app.config()["profiles"] if p["id"] == base_id), {})
            if base_id and not base_profile:
                raise WorkspaceError("角色模型配置已不存在，请先选择配置", 409)
            chosen = app.save_profile({**base_profile, "id": "", "name": "worker " + args.model, "model": args.model})
            selection = {"profile_id": chosen["id"]}
        run = manager.create_session(candidates[0]["id"], {
            "role": role, "parent_agent_id": parent,
            "execution_mode": "print" if args.p else "tui", "minutes": args.minutes if args.minutes is not None else 10,
            "task_label": args.task_label, "mission": args.mission, "from": args.from_ids, "intent_id": args.intent, "name": args.name or "",
            **selection,
            "request_id": args.request_id or uuid.uuid4().hex,
            "skip_permissions": manager.agent(parent).get("skip_permissions", False) if parent else False},
            inherited_permission_source="parent_agent" if parent and "skip_permissions" in manager.agent(parent) else "default")
        print(json.dumps(run, ensure_ascii=False))
        return 0
    if args.action == "hint":
        targets = [p for p in app.projects() if p["id"] in scope and args.target in (p["id"], p["name"])]
        if len(targets) != 1:
            raise WorkspaceError("hint 目标不在工作区范围内")
        app.store(targets[0]["id"]).add_hint(args.body or "", parent or "commander")
        return 0
    a = manager.agent(args.target) if args.target else None
    if a and a["workspace_id"] not in scope:
        raise WorkspaceError("不能操作其他工作区角色")
    if a and parent and a["id"] != parent:
        ancestor = a.get("parent_agent_id")
        seen = set()
        while ancestor and ancestor not in seen and ancestor != parent:
            seen.add(ancestor)
            record = manager.agent(ancestor)
            if record["workspace_id"] != wid:
                ancestor = None
                break
            ancestor = record.get("parent_agent_id")
        reporting_up = args.action == "msg" and manager.agent(parent).get("parent_agent_id") == a["id"]
        if ancestor != parent and not reporting_up:
            raise WorkspaceError("目标角色不在当前角色的指挥关系中", 403)
    if args.action == "extend":
        print(json.dumps(manager.extend(a["id"], {"minutes": args.minutes}), ensure_ascii=False))
    elif args.action == "resume":
        data = {"minutes": args.minutes} if args.minutes is not None else {}
        data["request_id"] = args.request_id or uuid.uuid4().hex
        if args.mission:
            data["continuation"] = args.mission
        print(json.dumps(manager.resume(a["id"], data), ensure_ascii=False))
    elif args.action == "kill":
        if args.close:
            raise WorkspaceError("本地工作区没有平台实例，不执行 --close 或自动交卷")
        print(json.dumps(manager.stop_agent(a["id"])))
    elif args.action == "msg":
        print(json.dumps(manager.message(a["id"], {"text": args.body or "", "sender_id": parent}), ensure_ascii=False))
    elif args.action == "peek":
        row = next(r for r in manager.agents() if r["id"] == a["id"])
        if a.get("execution_mode") == "print" and a.get("run_id"):
            print(json.dumps({k: row.get(k) for k in ("id", "status", "deadline_epoch", "activity")}, ensure_ascii=False))
            print(app.log(a["run_id"])["text"])
        else:
            print(app.capture(row["pane_id"])["text"] if row.get("pane_id") else app.log(a["run_id"])["text"])
    return 0


def watch():
    app = console_from_environment()
    wid = os.environ["SAPPER_WORKSPACE_ID"]
    retries = {}
    status_log = WatchdogLog()
    print(f"[watchdog] workspace={wid} poll=3s heartbeat=60s; 状态变化立即记录", flush=True)
    while True:
        rows = app.workspaces.agents(wid)
        main = app.workspaces.main_agent(wid, agents=rows)
        if not main or main.get("desired_state") != "running":
            print("[watchdog] 主 agent 已停止，watchdog 同步退出", flush=True)
            return 0
        commanders = 0
        for a in rows:
            if wid != a["workspace_id"]:
                continue
            if a["role"] == "commander" and a["id"] != main["id"]:
                continue
            if a["role"] == "commander" and not a.get("legacy") and a.get("execution_mode") in ("tui", "chat"):
                commanders += 1
                if (not a.get("pane_id") and a["status"] in TERMINAL_STATES
                        and app.workspaces.commander.view(a["id"])["control"] != "active"):
                    app.workspaces.mark_stopped(a["id"])
                    print("[watchdog] 主 agent 已离线且处于等待或结束状态，同步退出", flush=True)
                    return 0
                try:
                    app.workspaces.commander.tick(a["id"], rows)
                    line = status_log.line(a["id"], app.workspaces.commander.view(a["id"]))
                    if line:
                        print(line, flush=True)
                except (sapper.FGError, OSError, ValueError) as exc:
                    print(f"[watchdog] commander {a['id']}: {exc}", flush=True)
                if not a.get("pane_id") and app.workspaces.commander.view(a["id"]).get("recoveries", 0) >= 2:
                    app.workspaces.mark_stopped(a["id"])
                    print("[watchdog] 主 agent 恢复失败，同步退出", flush=True)
                    return 1
                continue
            if a.get("native_operation"):
                continue  # Original CLI workflows have no single native session to resume.
            if a.get("desired_state") != "running" or a.get("pane_id"):
                continue
            if a["status"] in ("stopped", "timed_out"):
                continue
            if a["status"] == "exited" and a.get("exit_code") == 0:
                continue
            if a["status"] not in TERMINAL_STATES or retries.get(a["id"], 0) >= 2:
                continue
            retries[a["id"]] = retries.get(a["id"], 0) + 1
            try:
                request_id = uuid.uuid4().hex
                app.workspaces.resume(a["id"], {"request_id": request_id})
                app.audit("watchdog.resumed", agent=a["id"], workspace=wid, request_id=request_id)
            except (sapper.FGError, OSError) as exc:
                print(f"[watchdog] {a['id']}: {exc}", flush=True)
        if not commanders:
            line = status_log.line("workspace-" + wid, {"phase": "no_managed_commander"})
            if line:
                print(line, flush=True)
        time.sleep(3)


if __name__ == "__main__":
    try:
        raise SystemExit(watch() if sys.argv[1:2] == ["watch"] else worker(sys.argv[2:]))
    except (sapper.FGError, OSError) as exc:
        print(f"[workspace] {exc}", file=sys.stderr)
        raise SystemExit(1)
