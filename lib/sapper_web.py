#!/usr/bin/env python3
"""A local Web control surface for the existing sapper CLI and tmux sessions.

Standard library only. Graphs remain in .sapper; the original agent is unchanged.
Run: python3 lib/sapper_web.py --root runs
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import errno
import fcntl
import hashlib
import hmac
import http.cookies
import json
import mimetypes
import os
import platform
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sapper
import sapper_competition as competition
import sapper_context
import sapper_evidence
from kb import KnowledgeBase, KBError
from sapper_workspace import Workspaces, WorkspaceError, ROLES, agent_summary, graph_summary
from sapper_native import NativeSurface, NativeError
from sapper_permission_audit import FIELDS as PERMISSION_AUDIT_FIELDS
from sapper_extensions import ExtensionError, clean_environment, api_environment
import sapper_models
from sapper_turns import _setting, iter_frames, read_events
from sapper_auth import AuthError, AuthStore, SESSION_SECONDS, manage_credentials

REPO = Path(__file__).resolve().parent.parent
SAPPER = REPO / "lib" / "sapper.py"
STATIC = REPO / "web"
MAX_BODY = 16 * 1024 * 1024
STAGING_TTL = 24 * 60 * 60
# Frozen spec v1.0 二: 'crashed' and 'startup_timeout' are declared here as
# terminal statuses. No code path sets them until the host lands (step 4-5);
# declaring them early keeps downstream FINISHED checks behavior-inert.
FINISHED = {"exited", "stopped", "failed", "interrupted", "timed_out",
            "crashed", "startup_timeout"}
PORT_IN_USE_EXIT = 3
CLAUDE_STARTUP_WARNING = "警告：Claude Code "
INTERRUPT_GRACE = 10
TERMINATE_GRACE = 3
MAX_RUN_SECONDS = 1440 * 60
# Frozen spec v1.0 阶段四: attachment range reads enforce BOTH dimensions so a
# single multi-megabyte JSON line cannot blow up a request or the page.
ATTACHMENT_MAX_LINES = _setting("SAPPER_ATTACHMENT_MAX_LINES", 2000, 1, 100000)
ATTACHMENT_MAX_BYTES = _setting("SAPPER_ATTACHMENT_MAX_BYTES", 256 * 1024, 1, 64 * 1024 * 1024)
ATTACHMENT_PAGE_DEFAULT = _setting("SAPPER_ATTACHMENT_PAGE_DEFAULT", 200, 1, 100000)
KEYS = {"Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right",
        "C-c", "C-d", "C-u", "C-l", "BSpace", "Space"}
HIDDEN = {".git", ".claude", ".codex", ".sapper-web", "node_modules", "__pycache__",
          ".venv", "pane-env", ".sapper-agents"}
# Workspace environment variables: user-supplied values injected into every
# agent launch in that workspace (e.g. MCP ${KEY} references). Harness control
# and model-credential routing stay single-authority, so those names are
# rejected at save time rather than silently overridden at launch.
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
ENV_RESERVED_PREFIXES = ("SAPPER_", "CLAUDE_", "ANTHROPIC_")
ENV_RESERVED_NAMES = {"PATH", "PYTHONUNBUFFERED", "CLAUDECODE", "IS_DEMO",
                      "DISABLE_DOCTOR_COMMAND", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                      "BENCHMARK_TOKEN"}
ENV_MAX_VARS = 50
ENV_MAX_VALUE = 4096
ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


class WebError(sapper.FGError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def require_text(data, key, limit=20000, default=None):
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise WebError(f"{key} 必须为非空文本（最多 {limit} 字符）")
    return value.strip()


def number(data, key, default, low, high):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WebError(f"{key} 必须在 {low}–{high} 之间")
    return value


def query_number(raw, key, default, low, high):
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise WebError(f"{key} 必须是整数")
    if not low <= value <= high:
        raise WebError(f"{key} 必须在 {low}–{high} 之间")
    return value


def attachment_name(name):
    name = Path(name.replace("\\", "/")).name
    if not name or name in (".", "..") or "\x00" in name or len(name) > 180 or len(name.encode()) > 246:
        raise WebError("文件名不正确")
    if name.endswith(".env"):
        raise WebError("环境凭据文件不通过普通文件接口提供", 403)
    return secrets.token_hex(4) + "_" + name


def private_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WebError(f"无法读取状态文件 {path.name}: {exc}") from exc


@contextlib.contextmanager
def run_lock(directory):
    """Serialize budget changes and the runner's decision to begin shutdown."""
    with (directory / "control.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Console:
    def __init__(self, root: Path, state: Path, tmux_socket=None, *, workspace_roots=None, claude_bin=None, initialize=True):
        self.root = root.resolve()
        self.state = state.resolve()
        self.tmux_socket = tmux_socket
        self._workspace_roots = list(dict.fromkeys([self.root] + [Path(p).expanduser().resolve() for p in (workspace_roots or [])]))
        self.claude_bin = claude_bin or os.environ.get("SAPPER_CLAUDE_BIN", "claude")
        self.config_path = self.state / "config.json"
        self.context_path = self.state / "global-context.json"
        self.lock = threading.RLock()
        if initialize:
            self.root.mkdir(parents=True, exist_ok=True)
            self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.state, 0o700)
            self.auth = AuthStore(self.state)
            bindir = self.state / "bin"
            bindir.mkdir(exist_ok=True)
            shim = bindir / "sapper"
            shim.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(SAPPER))} \"$@\"\n")
            shim.chmod(0o700)
        self.workspaces = Workspaces(self)
        self.native = NativeSurface(self)

    @contextlib.contextmanager
    def config_lock(self):
        with self.lock, (self.state / ".lock").open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def config(self):
        return read_json(self.config_path, {"profiles": []})

    @property
    def workspace_roots(self):
        # Web bindings are the persistent source of selected directories. Read
        # them here so worker/watchdog processes also see later bindings.
        selected = [Path(w["path"]).expanduser().resolve()
                    for w in self.config().get("workspaces", [])]
        return list(dict.fromkeys([*self._workspace_roots, *selected]))

    def global_context(self):
        try:
            return sapper_context.load(self.context_path, strict=True)
        except ValueError as exc:
            raise WebError(str(exc), 409) from exc

    def save_context(self, data):
        content = data.get("content", "")
        if not isinstance(content, str) or len(content) > sapper_context.MAX_CONTENT or "\x00" in content:
            raise WebError(f"共享文本最多 {sapper_context.MAX_CONTENT} 字符，不能包含空字符")
        keys = ("enabled", "prompt_enabled", "hook_enabled")
        if any(type(data.get(k)) is not bool for k in keys):
            raise WebError("请明确选择是否启用及注入方式")
        if data["enabled"] and (not content.strip() or not (data["prompt_enabled"] or data["hook_enabled"])):
            raise WebError("启用时需填写共享文本，并至少选择一种注入方式")
        with self.config_lock():
            old = self.global_context()
            expected = data.get("expected_revision")
            if type(expected) is not int or expected != old["revision"]:
                raise WebError("配置已被其他页面更新，请重新读取后再保存；当前输入仍保留", 409)
            changed = content != old["content"] or any(data[k] != old[k] for k in keys)
            result = {**old, **{k: data[k] for k in keys}, "content": content}
            if changed:
                result.update(revision=old["revision"] + 1, updated_at=sapper.now_iso())
                sapper_context.atomic_private(self.context_path, result)
        self.audit("context.saved", revision=result["revision"], enabled=result["enabled"])
        return result

    def overview(self):
        projects = self.projects()
        runs = self.runs(reconcile=False)
        panes = self.panes()
        agents = [a for a in self.workspaces.agents(run_records=runs, pane_records=panes) if not a["id"].startswith("legacy-")]
        context = self.global_context()
        by_workspace = {}
        for agent in agents:
            by_workspace.setdefault(agent["workspace_id"], []).append(agent)
        for project in projects:
            project["agent_summary"] = agent_summary(by_workspace.get(project["id"], []))
            project["active_agents"] = project["agent_summary"]["online"]
        recent = [{k: r.get(k) for k in ("id", "project_id", "kind", "native_operation", "role", "profile_name", "status", "exit_code", "created_at")}
                  for r in runs[:8]]
        return {"projects": projects, "agents": agents, "panes": panes, "recent_runs": recent,
                "summary": {"workspaces": len(projects), "active_agents": agent_summary(agents)["online"],
                            "open_intents": sum(p["open_intents"] for p in projects),
                            "facts": sum(p["facts"] for p in projects)},
                "context": {k: context[k] for k in ("revision", "enabled", "prompt_enabled", "hook_enabled", "updated_at")},
                "environment": {"system": "macOS" if platform.system() == "Darwin" else platform.system(), "architecture": platform.machine(),
                                "python": platform.python_version(),
                                "claude": self.workspaces.capabilities(), "workspace_roots": [str(p) for p in self.workspace_roots]}}

    def redaction_keys(self):
        """Collect fresh credentials once for a batch, never cache across requests."""
        cfg = self.config()
        keys = [p.get("api_key", "") for p in cfg["profiles"]]
        for path in (self.state / "model-bridges").glob("*/ready.json"):
            with contextlib.suppress(OSError, ValueError):
                keys.append(json.loads(path.read_text()).get("token", ""))
        keys += [os.environ.get(k, "") for k in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "SAPPER_CLAUDE_API_KEY", "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY", "BENCHMARK_TOKEN")]
        keys.extend(self.workspaces.extensions.credential_values())
        # Workspace env values may satisfy MCP ${KEY} refs without being present
        # in os.environ, so credential_values() alone would miss them.
        for values in cfg.get("workspace_env", {}).values():
            keys.extend(values.values())
        return tuple(dict.fromkeys(key for key in keys if key))

    def redact(self, value, *, keys=None):
        text = str(value)
        if keys is None:
            keys = self.redaction_keys()
        for key in keys:
            if key:
                text = text.replace(key, "[已隐藏密钥]")
        return text

    def audit(self, action, **details):
        entry = {"at": sapper.now_iso(), "action": action, **details}
        with self.lock, (self.state / "actions.jsonl").open("a") as f:
            os.chmod(f.name, 0o600)
            f.write(self.redact(json.dumps(entry, ensure_ascii=False)) + "\n")

    def inside(self, path: Path, parent=None):
        path = path.resolve()
        if not path.is_relative_to(parent or self.root):
            raise WebError("路径超出已配置的工作根目录", 403)
        if path.is_relative_to(self.state):
            raise WebError("不能通过任务文件接口访问 Web 凭据目录", 403)
        return path

    def _scan_projects(self):
        found, seen = [], set()
        for directory, dirs, _ in os.walk(self.root, followlinks=False):
            here = Path(directory)
            dirs[:] = sorted(d for d in dirs if d not in HIDDEN and not d.startswith("."))
            if len(here.relative_to(self.root).parts) >= 4:
                dirs[:] = []
            store_path = here / ".sapper"
            if not (store_path / "project.json").is_file() or store_path.is_symlink():
                continue
            try:
                resolved = self.inside(store_path)
                if resolved in seen:
                    continue
                seen.add(resolved)
                store = sapper.Store(resolved)
                with store.locked():
                    project = store.load_project()
                    summary = graph_summary(store)
                rel = str(here.relative_to(self.root))
                title = project.get("name")
                found.append({"id": hashlib.sha256(rel.encode()).hexdigest()[:16],
                              "name": title.strip() if isinstance(title, str) and title.strip() else here.name,
                              "directory": here.name, "path": str(here), "relative_path": rel,
                              "goal": project["goal"], "status": project["status"],
                              "created_at": project.get("created_at"),
                              **summary})
            except (WebError, sapper.FGError, OSError):
                continue
            if len(found) >= 200:
                break
        return sorted(found, key=lambda p: p.get("created_at") or "", reverse=True)

    def projects(self):
        # Deleting a workspace without its files hides the scanned directory;
        # binding the same directory again makes it visible.
        hidden = set(self.config().get("hidden_workspaces", []))
        found = {p["path"]: {**p, "enabled": True, "mode": "legacy"}
                 for p in self._scan_projects() if p["path"] not in hidden}
        for bound in self.workspaces.bound_projects():
            found[bound["path"]] = bound
        for p in found.values():
            p["inside_root"] = self.inside_root(p["path"])
        return sorted(found.values(), key=lambda p: p.get("created_at") or "", reverse=True)

    def inside_root(self, path):
        """True for a directory strictly below the work root, safe to remove."""
        try:
            here, root = Path(path), self.root.resolve()
            resolved = here.resolve()
        except OSError:
            return False
        return (not here.is_symlink() and resolved != root and resolved.is_relative_to(root)
                and not resolved.is_relative_to(self.state.resolve()))

    def project(self, pid):
        for p in self.projects():
            if p["id"] == pid:
                return p
        raise WebError("任务不存在，或不在工作根目录中", 404)

    def store(self, pid):
        root = Path(self.project(pid)["path"])
        return sapper.Store(self.inside(root / ".sapper", root))

    def evidence(self, store, pid, data):
        """Translate a graph-action payload into fact provenance.

        Mirrors the CLI: reproduction commands are stored as text and never
        executed, and artifacts must be regular files inside the bound project
        directory, copied into the graph under their SHA-256.
        """
        source = data.get("evidence_source")
        raw_commands = data.get("repro_commands") or []
        raw_artifacts = data.get("artifacts") or []
        if source is not None and not isinstance(source, str):
            raise WebError("证据来源必须为文本")
        for name, value in (("repro_commands", raw_commands), ("artifacts", raw_artifacts)):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise WebError("复现命令与工件必须是文本列表")
            if len(value) > 32:
                raise WebError("单条事实最多记录 32 条复现命令和 32 个工件")
        commands = [item.strip() for item in raw_commands if item.strip()]
        artifacts = [item.strip() for item in raw_artifacts if item.strip()]
        if not (source and source.strip()) and not commands and not artifacts:
            return None
        cwd = Path(self.project(pid)["path"]).resolve()
        try:
            items = sapper_evidence.ingest_artifacts(store.root, artifacts, cwd=cwd)
        except ValueError as exc:
            raise WebError(str(exc)) from exc
        return sapper_evidence.provenance(source, commands, items, cwd)

    def create_project(self, data, *, session_hash=None):
        # `title` is the workspace name shown in the UI; `name` is the directory.
        # Either may be omitted: the title falls back to the directory name and
        # the directory is derived from the title when left empty.
        title = require_text(data, "title", 64) if data.get("title") else ""
        if data.get("name"):
            name = require_text(data, "name", 64)
        elif title:
            name = re.sub(r"[^A-Za-z0-9_.-]+", "-", title).strip("-_.")[:64].rstrip("-_.")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
                name = "workspace-" + secrets.token_hex(4)
        else:
            raise WebError("请填写工作区名称")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise WebError("目录名请使用英文字母、数字、点、横线或下划线")
        title = title or name
        goal = require_text(data, "goal")
        path = self.inside(self.root / name)
        context = require_text(data, "context") if data.get("context") else ""
        with self.config_lock(), contextlib.ExitStack() as rollback:
            if path.exists():
                raise WebError("目录已存在；已有图会自动出现在任务列表，请勿重复初始化", 409)
            cfg = self.config()
            assignments = self.new_workspace_models(cfg, data)
            env_vars = self.validate_workspace_env(data["env"]) if "env" in data else None
            extensions = self.workspaces.extensions
            if "extensions" in data:
                library = extensions.read()
                selection = extensions.select(library, data["extensions"])
            ids = data.get("attachment_ids", [])
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
                raise WebError("暂存附件列表不正确")
            staged = [self.staged_file(session_hash, token) for token in ids]
            path.mkdir()
            rollback.callback(shutil.rmtree, path)
            claimed = []
            if staged:
                target = path / "attachments"
                target.mkdir()
                for source in staged:
                    destination = target / source.name
                    if destination.exists():
                        raise WebError("附件名称冲突，请重新选择文件", 409)
                    shutil.copy2(source, destination)
                    directory = source.parent
                    consumed = directory.rename(directory.with_suffix(".claimed"))
                    rollback.callback(consumed.rename, directory)
                    claimed.append(consumed)
            store = sapper.Store(path / ".sapper")
            store.init("web", goal)
            if title != name:
                with store.locked():
                    meta = store.load_project()
                    meta["name"] = title
                    store.save_project(meta)
            if context:
                store.add_hint(context, "web-user")
            project = next(p for p in self.projects() if p["path"] == str(path))
            if "extensions" in data and (any(selection.values()) or project["id"] in library["workspaces"]):
                defaults = {**library["workspaces"], project["id"]: selection}
                private_json(extensions.path, {**library, "workspaces": defaults, "revision": library["revision"] + 1})
                rollback.callback(private_json, extensions.path, library)
            if assignments is not None:
                self.set_workspace_models(cfg, project["id"], assignments)
            if env_vars is not None:
                self.set_workspace_env(cfg, project["id"], env_vars)
            if assignments is not None or env_vars is not None:
                private_json(self.config_path, cfg)
            rollback.pop_all()
            for directory in claimed:
                shutil.rmtree(directory, ignore_errors=True)
        self.audit("project.created", name=name, title=title, env_names=sorted(env_vars or {}))
        return project

    def graph(self, pid):
        project = self.project(pid)
        root = Path(project["path"])
        store = sapper.Store(self.inside(root / ".sapper", root))
        if not store.exists():
            graph = {"project": {"id": "proj", "goal": project["goal"], "status": "not_enabled"},
                     "facts": [], "intents": [], "hints": [], "open_intents": [], "done_intents": [], "fact_ids": []}
        else:
            with store.locked():
                graph = sapper.build_graph_view(store)
        try:
            roster = competition.read_roster(root)
            if roster is not None:
                graph["competition"] = competition.with_graph(roster, graph)
        except sapper.FGError as exc:
            graph["competition"] = {"error": self.redact(str(exc))}
        return graph

    def import_competition(self, pid, data):
        root = Path(self.project(pid)["path"])
        complete = data.get("complete", False)
        if type(complete) is not bool:
            raise WebError("完整题单选项必须为布尔值")
        directory = self.workspaces.allowed(root / Path(require_text(data, "directory", 4096)).expanduser())
        roster = competition.scan_directory(directory, complete=complete)
        with self.config_lock():
            existing = competition.read_roster(root)
            if existing:
                tasks = {row["id"]: row for row in existing["tasks"]}
                tasks.update((row["id"], row) for row in roster["tasks"])
                roster = competition.normalize_roster({**roster, "tasks": list(tasks.values())})
            sapper.atomic_write_json(root / competition.FILENAME, roster)
        self.audit("competition.imported", workspace=pid, tasks=len(roster["tasks"]))
        return roster

    def graph_action(self, pid, action, data):
        store = self.store(pid)
        for field in ("title", "doc", "kind"):
            if data.get(field) is not None and not isinstance(data[field], str):
                raise WebError(f"{field} 必须为文本")
        if "requires_confirmation" in data and not isinstance(data["requires_confirmation"], bool):
            raise WebError("requires_confirmation 必须为布尔值")
        if action == "hint":
            result = store.add_hint(require_text(data, "content"), "web-user")
        elif action == "fact":
            result = store.add_fact(require_text(data, "description"), "web-user",
                                    source_intent=data.get("from_intent") or None,
                                    title=data.get("title"), doc=data.get("doc"), kind=data.get("kind"),
                                    data=data.get("data"), evidence=self.evidence(store, pid, data))
        elif action == "intent":
            topic = data.get("competition_challenge")
            from_ids = data.get("from", [])
            description = require_text(data, "description")
            if topic is not None:
                topic = require_text(data, "competition_challenge", 160)
                roster = competition.read_roster(Path(self.project(pid)["path"]))
                task = next((task for task in (roster or {}).get("tasks", []) if task["id"] == topic), None)
                if task is None:
                    raise WebError("题号不在当前工作区题单中")
                if from_ids == []:
                    store.require()
                    with self.config_lock():
                        fact = next((f for f in store.list_facts()
                                     if f.get("kind") == "note" and (f.get("data") or {}).get("competition_challenge") == topic), None)
                        if fact is None:
                            fact = store.add_fact(f"题单登记 {topic}", "web-user", title=task["name"], kind="note",
                                                  data={"competition_challenge": topic})
                        from_ids = [fact["id"]]
            result = store.add_intent(from_ids, description, "web-user",
                                      requires_confirmation=bool(data.get("requires_confirmation")),
                                      title=data.get("title"), doc=data.get("doc"), competition_challenge=topic)
        elif action == "confirm":
            result = store.confirm_intent(require_text(data, "id", 64))
        elif action == "claim":
            result = store.claim_intent(require_text(data, "id", 64), require_text(data, "worker", 100))
        elif action == "release":
            result = self.workspaces.release_intent(pid, require_text(data, "id", 64),
                expected_owner=require_text(data, "worker", 100), reason=require_text(data, "reason", 1000, "用户释放意图"))
        elif action == "done":
            fact, intent = store.conclude_with_new_fact(require_text(data, "id", 64), require_text(data, "description"),
                require_text(data, "worker", 100), title=data.get("title"), doc=data.get("doc"), kind=data.get("kind"),
                data=data.get("data"), evidence=self.evidence(store, pid, data))
            result = {**fact, "concluded_intent": intent["id"]}
        elif action == "complete":
            result = store.complete_project(data.get("from", []), require_text(data, "note"), require_text(data, "worker", 100))
        elif action == "brainstorm-pick":
            proc = subprocess.run([sys.executable, str(SAPPER), "--store", str(store.root), "brainstorm-pick", require_text(data, "id", 64)],
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode:
                raise WebError(proc.stderr.strip())
            result = store.get_intent(data["id"])
        elif action == "doc":
            if "expected_doc" not in data:
                raise WebError("编辑文档需要提供读取时的版本")
            result = store.update_doc(require_text(data, "id", 64), data.get("doc"),
                                      expected_doc=data["expected_doc"])
        else:
            raise WebError("不支持的图操作")
        self.audit("graph." + action, project=pid, node=result["id"])
        return result

    def profiles(self):
        return [{k: v for k, v in p.items() if k != "api_key"} | {"protocol": sapper_models.protocol(p), "has_key": bool(p.get("api_key"))}
                for p in self.config()["profiles"]]

    def role_models(self, pid=None):
        if pid is not None:
            self.project(pid)
        cfg = self.config()
        return {"assignments": cfg.get("workspace_role_profiles", {}).get(pid, {}) if pid is not None else cfg.get("role_profiles", {}),
                "global_assignments": cfg.get("role_profiles", {}),
                "effective_assignments": self.role_model_defaults(cfg, pid),
                "revision": cfg.get("role_profiles_revision", 0),
                "roles": [{"id": role, "label": label} for role, label in ROLES.items()]}

    @staticmethod
    def validate_role_assignments(cfg, assignments, *, workspace=False):
        allowed = set(ROLES)
        if workspace:
            allowed.add("main")
        if not isinstance(assignments, dict) or assignments.keys() - allowed:
            raise WebError("角色模型配置格式不正确")
        profiles = {p["id"]: p for p in cfg["profiles"]}
        for value in assignments.values():
            if not isinstance(value, str):
                raise WebError("请选择已保存的模型连接")
            if value and value not in profiles:
                raise WebError("模型连接已不存在，请重新选择", 409)
            if value and profiles[value].get("engine_prefix"):
                raise WebError("默认模型不能使用 CLI 包装器，请选择普通模型连接")

    def new_workspace_models(self, cfg, data):
        if "model_assignments" not in data:
            return None
        assignments = data["model_assignments"]
        self.validate_role_assignments(cfg, assignments, workspace=True)
        if "main" not in assignments:
            raise WebError("请选择工作区主模型")
        return assignments

    @staticmethod
    def set_workspace_models(cfg, pid, assignments):
        cfg.setdefault("workspace_role_profiles", {})[pid] = assignments
        cfg["role_profiles_revision"] = cfg.get("role_profiles_revision", 0) + 1

    @staticmethod
    def role_model_defaults(cfg, workspace_id):
        local = cfg.get("workspace_role_profiles", {}).get(workspace_id, {})
        base = dict.fromkeys(ROLES, local["main"]) if "main" in local else cfg.get("role_profiles", {})
        return {**base, **{role: value for role, value in local.items() if role != "main"}}

    def save_role_models(self, data, pid=None):
        if pid is not None:
            self.project(pid)
        assignments = data.get("assignments")
        with self.config_lock():
            cfg = self.config()
            revision = cfg.get("role_profiles_revision", 0)
            if type(data.get("expected_revision")) is not int or data["expected_revision"] != revision:
                raise WebError("角色模型已被其他页面更新，请重新读取后再保存；当前选择仍保留", 409)
            self.validate_role_assignments(cfg, assignments, workspace=pid is not None)
            # Missing entries follow the original inheritance rule. Empty entries
            # explicitly use the environment, even for children of another model.
            if pid is None:
                cfg["role_profiles"] = assignments
                cfg["role_profiles_revision"] = revision + 1
            else:
                self.set_workspace_models(cfg, pid, assignments)
            private_json(self.config_path, cfg)
        self.audit("role_models.saved", workspace=pid, revision=revision + 1, assignments=assignments)
        return self.role_models(pid)

    @staticmethod
    def validate_workspace_env(env):
        if not isinstance(env, dict) or len(env) > ENV_MAX_VARS:
            raise WebError(f"环境变量须为对象，且最多 {ENV_MAX_VARS} 个")
        for name, value in env.items():
            if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
                raise WebError("环境变量名须以字母或下划线开头，仅含字母、数字、下划线，最多 64 字符")
            if name.startswith(ENV_RESERVED_PREFIXES) or name in ENV_RESERVED_NAMES:
                raise WebError(f"{name} 为 sapper 运行控制或模型凭据保留变量，请改用其他名称")
            if not isinstance(value, str) or len(value) > ENV_MAX_VALUE or "\x00" in value:
                raise WebError(f"{name} 的值须为最多 {ENV_MAX_VALUE} 字符的文本，不能包含空字符")
        return dict(env)

    @staticmethod
    def set_workspace_env(cfg, pid, env):
        cfg.setdefault("workspace_env", {})[pid] = env
        cfg["workspace_env_revision"] = cfg.get("workspace_env_revision", 0) + 1

    def workspace_env(self, pid):
        """Write-only view: names and whether a value is set, never the value."""
        self.project(pid)
        cfg = self.config()
        stored = cfg.get("workspace_env", {}).get(pid, {})
        return {"vars": [{"name": name, "has_value": bool(value)} for name, value in sorted(stored.items())],
                "revision": cfg.get("workspace_env_revision", 0)}

    def save_workspace_env(self, data, pid):
        self.project(pid)
        updates = data.get("vars")
        remove = data.get("remove", [])
        if not isinstance(updates, dict):
            raise WebError("vars 须为对象")
        if not isinstance(remove, list) or any(not isinstance(n, str) for n in remove):
            raise WebError("remove 须为变量名列表")
        with self.config_lock():
            cfg = self.config()
            revision = cfg.get("workspace_env_revision", 0)
            if type(data.get("expected_revision")) is not int or data["expected_revision"] != revision:
                raise WebError("环境变量已被其他页面更新，请重新读取后再保存；当前输入仍保留", 409)
            stored = cfg.get("workspace_env", {}).get(pid, {})
            merged = {name: value for name, value in stored.items() if name not in remove}
            for name, value in updates.items():
                if value == "":
                    # Write-only editing: an empty value keeps the stored secret.
                    if name not in merged:
                        raise WebError(f"{name} 尚未设置，请填写值")
                else:
                    merged[name] = value
            merged = self.validate_workspace_env(merged)
            self.set_workspace_env(cfg, pid, merged)
            private_json(self.config_path, cfg)
        self.audit("workspace.env.saved", workspace=pid, revision=revision + 1,
                   names=sorted(merged), removed=sorted(set(remove) & set(stored)))
        return self.workspace_env(pid)

    def role_profile_id(self, role, data, fallback="", *, workspace_id=None):
        cfg = self.config()
        defaults = self.role_model_defaults(cfg, workspace_id)
        value = data["profile_id"] if "profile_id" in data else defaults.get(role, fallback)
        if not isinstance(value, str):
            raise WebError("模型连接 ID 必须为文本")
        return value

    def save_profile(self, data):
        name = require_text(data, "name", 100)
        model = require_text(data, "model", 200)
        base_url = str(data.get("base_url", "")).strip()
        if base_url:
            parsed = urllib.parse.urlsplit(base_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise WebError("API 地址必须是无内嵌凭据的 HTTP(S) 地址")
        prefix = str(data.get("engine_prefix", "")).strip()
        if prefix:
            try:
                if not shlex.split(prefix):
                    raise ValueError()
            except ValueError as exc:
                raise WebError("CLI 包装命令格式不正确") from exc
        with self.config_lock():
            cfg = self.config()
            pid = data.get("id") or secrets.token_hex(8)
            old = next((p for p in cfg["profiles"] if p["id"] == pid), None)
            if data.get("id") and old is None:
                raise WebError("模型配置不存在", 404)
            key = data.get("api_key")
            if key is not None and (not isinstance(key, str) or len(key) > 4096 or any(c in key for c in "\x00\r\n")):
                raise WebError("密钥格式不正确")
            profile = {"id": pid, "name": name, "model": model, "base_url": base_url,
                       "engine_prefix": prefix, "api_key": key if key else (old or {}).get("api_key", "")}
            profile.update({k: data.get(k, (old or {}).get(k, default)) for k, default in
                            (("protocol", "anthropic"), ("timeout_seconds", 120), ("max_output_tokens", 0), ("token_field", "max_completion_tokens"))})
            profile = sapper_models.validate(profile)
            if data.get("clear_key"):
                profile["api_key"] = ""
            cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != pid] + [profile]
            private_json(self.config_path, cfg)
        self.audit("profile.saved", profile=pid)
        return next(p for p in self.profiles() if p["id"] == pid)

    def profile_probe(self, data, action):
        old = next((p for p in self.config()["profiles"] if p["id"] == data.get("id")), None)
        if data.get("id") and old is None:
            raise WebError("模型配置不存在", 404)
        draft = {**(old or {}), **{k: v for k, v in data.items() if k != "api_key"}}
        key = data.get("api_key")
        if key is not None and (not isinstance(key, str) or len(key) > 4096 or any(c in key for c in "\x00\r\n")):
            raise WebError("密钥格式不正确")
        draft["api_key"] = "" if data.get("clear_key") else key or (old or {}).get("api_key", "")
        draft = sapper_models.validate(draft, require_model=action != "models")
        result = sapper_models.list_models(draft) if action == "models" else sapper_models.test_connection(draft)
        self.audit("profile." + action, profile=data.get("id", "draft"), protocol=draft["protocol"])
        key = sapper_models.resolved({**draft, "model": draft.get("model") or "discovery"})["api_key"]
        def clean(value):
            if isinstance(value, str):
                return self.redact(value.replace(key, "[已隐藏]") if key else value)
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            return value
        return clean(result)

    def delete_profile(self, pid):
        with self.config_lock():
            cfg = self.config()
            if any(models.get("main") == pid for models in cfg.get("workspace_role_profiles", {}).values()):
                raise WebError("该连接用于工作区主模型，请先在工作区配置中重新分配", 409)
            used = [label for role, label in ROLES.items()
                    if cfg.get("role_profiles", {}).get(role) == pid or any(
                        assignments.get(role) == pid for assignments in cfg.get("workspace_role_profiles", {}).values())]
            if used:
                raise WebError("该连接用于角色默认模型：" + "、".join(used) + "；请先在角色默认模型中重新分配", 409)
            cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != pid]
            private_json(self.config_path, cfg)
        self.audit("profile.deleted", profile=pid)

    def panes(self):
        """Presentation shim: one pseudo-pane per live managed headless run.

        There is no terminal multiplexer.  Print runs still consume this
        pane-shaped read API until their dedicated phase-5 cleanup.
        """
        result = []
        for path in (self.state / "runs").glob("*/run.json"):
            run = read_json(path, {})
            rid = run.get("id") or path.parent.name
            info = self.run_info(rid)
            if info["status"] in FINISHED:
                continue
            if not (path.parent / "host.json").exists():
                continue
            session = "sapper-web-" + rid
            agent = self.workspaces.agent(run.get("agent_id"), include_deleted=True) if run.get("agent_id") else {}
            result.append({
                "id": "run-" + rid,
                "session": session,
                "name": agent.get("role") or run.get("kind") or rid,
                "path": run.get("cwd") or "",
                "dead": False,
                "command": run.get("command") or "",
                "project_id": run.get("project_id"),
                "role": agent.get("role"),
            })
        return result

    def pane(self, pane_id):
        for pane in self.panes():
            if pane["id"] == pane_id:
                return pane
        raise WebError("终端不存在或已离开受管任务目录", 404)

    def capture(self, pane_id):
        from sapper_terminal import TerminalBridge
        self.pane(pane_id)
        text = TerminalBridge(self).snapshot(pane_id)["text"]
        return {"text": text, "captured_at": sapper.now_iso()}

    def send(self, pane_id, data, *, target_agent=None, kind="terminal_input", message_id=None):
        from sapper_terminal import TerminalBridge
        pane = self.pane(pane_id)
        if data.get("key") in ("C-c", "Escape"):
            self.workspaces.pause_commander_input(pane)
        if data.get("key") and data["key"] not in KEYS:
            raise WebError("不支持的按键")
        # Real managed panes carry a session = "sapper-web-<rid>"; the text is
        # delivered through that run's inbox/journal. Mocked/legacy panes with
        # no run session are still audited but have no structured input channel.
        session = pane.get("session") or ""
        rid = session[len("sapper-web-"):] if session.startswith("sapper-web-") else ""
        if rid:
            result = TerminalBridge(self).input("run-" + rid, {"data": data.get("text") or ""})
        elif data.get("key") in ("C-c", "Escape"):
            result = TerminalBridge(self).input(pane_id, {"data": "\x03"})
        else:
            result = {"status": "sent"}
        self.audit("terminal.sent", pane=pane_id, project=pane["project_id"], key=data.get("key"),
                   target_agent=target_agent, kind=kind, message_id=message_id or secrets.token_hex(16))
        return {"status": result["status"], "message": "已送入结构化收件箱；是否采用请查看原 agent 回应"}

    def run_path(self, rid):
        if not re.fullmatch(r"[a-f0-9]{16}", rid):
            raise WebError("运行 ID 不正确")
        return self.state / "runs" / rid

    def run_info(self, rid, *, redaction_keys=None):
        directory = self.run_path(rid)
        info = read_json(directory / "run.json")
        if not info:
            raise WebError("运行不存在", 404)
        control = read_json(directory / "control.json", {})
        if "timeout_seconds" in control:
            info["timeout_seconds"] = control["timeout_seconds"]
        info["activity"] = json.loads(self.redact(json.dumps(read_json(directory / "activity.json", {}), ensure_ascii=False), keys=redaction_keys))
        if info.get("timeout_seconds") is not None and info.get("started_epoch") is not None:
            info["deadline_epoch"] = info["started_epoch"] + info["timeout_seconds"]
        # Frozen spec v1.0 二: expose the spawn generation/epoch when a host
        # record exists; older runs default to 0 without changing other fields.
        info.setdefault("generation", 0)
        info.setdefault("epoch", 0)
        return info

    def extend(self, rid, data):
        minutes = number(data, "minutes", None, 1, 1440)
        directory = self.run_path(rid)
        self.run_info(rid)  # Validate existence before opening its lock.
        with run_lock(directory):
            info = self.run_info(rid)
            if info.get("supervisor_version") != 1 or info.get("timeout_seconds") is None:
                raise WebError("该运行不支持动态续时；新启动的限时原生会话可续时", 409)
            if info["status"] not in ("starting", "running") or (directory / "stop_requested").exists():
                raise WebError("运行已结束或开始停止，请按原会话 ID 恢复", 409)
            total = info["timeout_seconds"] + minutes * 60
            if total > MAX_RUN_SECONDS:
                raise WebError("单次运行累计预算不能超过 1440 分钟")
            private_json(directory / "control.json", {"timeout_seconds": total})
        self.audit("run.extended", run=rid, minutes=minutes, timeout_seconds=total)
        return self.run_info(rid)

    def stop_all_runs(self):
        """Stop and physically reap every run this service manages.

        Control-plane-only cleanup: only runs recorded under our own state are
        touched, never an unrelated process or a shared tmux server.
        """
        from sapper_host import SubprocessHost
        for path in (self.state / "runs").glob("*/run.json"):
            try:
                info = read_json(path, {})
            except (OSError, ValueError):
                continue
            run_dir = path.parent
            with run_lock(run_dir):
                (run_dir / "stop_requested").touch()
            pid = info.get("wrapper_pid")
            if isinstance(pid, int) and pid > 1:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            if (run_dir / "host.json").exists():
                try:
                    SubprocessHost(run_dir, role=info.get("role") or "worker",
                                   generation=int(info.get("generation") or 0)).reap(force=True)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass

    def host_alive(self, rid):
        """Public liveness predicate for a managed run (identity by PID birth)."""
        return self._host_alive(self.run_info(rid))

    def kill_host(self, rid):
        """Force-kill a managed run's process group to simulate a crash (test/ops)."""
        from sapper_host import SubprocessHost
        info = self.run_info(rid)
        run_dir = self.run_path(rid)
        if (run_dir / "host.json").exists():
            try:
                SubprocessHost(run_dir, role=info.get("role") or "worker",
                               generation=int(info.get("generation") or 0)).reap(force=True)
            except OSError:
                # SIGKILL was already delivered; a bare/reused PGID only means
                # the post-kill recheck cannot reconfirm ownership.
                pass
        return info

    def _host_alive(self, info):
        """Liveness from the host's PID birth, never from a bare tmux session name.

        Legacy runs without a host record are left to their own supervisor so a
        reaper never mistakes a reused PID for the original process.
        """
        host_path = self.run_path(info["id"]) / "host.json"
        if not host_path.exists():
            return True
        try:
            record = json.loads(host_path.read_text())
        except (OSError, ValueError):
            return True
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            return False
        try:
            from sapper_processes import process_table
            row = process_table().get(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            return True
        if not row:
            return False
        birth = record.get("birth")
        return bool(birth) and row["birth"] == birth

    @staticmethod
    def _close_orphan_turn(directory, info):
        """Crash takeover: a dead supervisor cannot settle its own turn."""
        if not (directory / "frames.jsonl").exists() or not (directory / "turns.jsonl").exists():
            return
        from sapper_turns import TurnStream
        try:
            stream = TurnStream(directory)
            if stream.settled:
                return
            status = info.get("status") or info.get("stop_reason") or ""
            if status in ("timed_out", "startup_timeout"):
                terminal = "timeout"
            elif status == "crashed":
                terminal = "crashed"
            elif status in ("stopped", "interrupted"):
                terminal = "interrupted"
            else:
                terminal = "failed"
            stream.settle(terminal)
        except (OSError, ValueError):
            pass

    def runs(self, pid=None, *, reconcile=True):
        result = []
        keys = self.redaction_keys()
        for path in (self.state / "runs").glob("*/run.json"):
            info = read_json(path)
            if pid and info.get("project_id") != pid:
                continue
            if reconcile and info["status"] not in FINISHED:
                if not self._host_alive(info):
                    # Do not overwrite a concurrent final result written by the runner.
                    with run_lock(path.parent):
                        current = read_json(path)
                        if current["status"] not in FINISHED:
                            current.update(status=current.get("stop_reason", "interrupted"), ended_at=sapper.now_iso())
                            private_json(path, current)
                            self._close_orphan_turn(path.parent, current)
            info = self.run_info(info["id"], redaction_keys=keys)
            result.append(json.loads(self.redact(json.dumps(info, ensure_ascii=False), keys=keys)))
        route_due_events(self.state)
        return sorted(result, key=lambda r: r["created_at"], reverse=True)[:100]

    def workspace_status(self, pid):
        runs, panes = self.runs(reconcile=False), self.panes()
        agents = self.workspaces.agents(pid, run_records=runs, pane_records=panes)
        try:
            primary, error = self.workspaces.main_agent(pid, agents=agents), None
        except WorkspaceError as exc:
            primary, error = None, str(exc)
        from sapper_routing import RoutingLedger
        # Frozen spec v1.0 阶段四: the read-only global audit copy is carried on
        # the existing commander status poll — no independent timer or endpoint.
        fault_audit = RoutingLedger(self.state, pid).audit_list()
        return {"agents": agents, "primary_agent": primary, "primary_error": error,
                "summary": agent_summary(agents), "observed_at": sapper.now_iso(),
                "fault_audit": fault_audit,
                "services": self.workspaces.services(pid, run_records=runs, pane_records=panes)}

    def start(self, pid, data, *, native=None):
        project = (native or {}).get("project_override") or self.project(pid)
        kind = data.get("kind", "auto")
        if kind == "claude":
            return self.workspaces.create_session(pid, data)
        context = self.global_context()
        if context["enabled"] and context["hook_enabled"] and not (native or {}).get("skip_hook_install"):
            try:
                sapper_context.ensure_hooks(Path(native["cwd"] if native else project["path"]))
            except (OSError, ValueError) as exc:
                raise WebError("无法接入全局上下文 hooks：" + str(exc), 409) from exc
        profile = None
        if data.get("profile_id"):
            profile = next((p for p in self.config()["profiles"] if p["id"] == data["profile_id"]), None)
            if profile is None:
                raise WebError("模型配置不存在")
        # Snapshot this service's launch environment, not an older tmux daemon's.
        env = dict(os.environ)
        if (native or {}).get("isolated_extensions"):
            clean_environment(env)
        # Workspace-supplied variables sit above the service environment but
        # below sapper's own control variables and the session's native env, so
        # users can provide MCP ${KEY} values without overriding the harness.
        env.update(self.config().get("workspace_env", {}).get(pid, {}))
        env.update(PATH=str(self.state / "bin") + os.pathsep + os.environ.get("PATH", ""),
                   PYTHONUNBUFFERED="1", SAPPER_ROOT=str(self.root),
                   SAPPER_GLOBAL_CONTEXT_FILE=str(self.context_path), SAPPER_CONTEXT_PROMPT_REVISION="")
        if "SAPPER_KB_ROOT" in env:
            env["SAPPER_KB_ROOT"] = str(KnowledgeBase.default_root().resolve())
        if profile and sapper_models.protocol(profile) != "anthropic":
            env["SAPPER_BIN"] = shlex.quote(self.claude_bin)
        sapper_models.apply_profile(env, profile, self.state)
        if (native or {}).get("isolated_extensions") and not (env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("SAPPER_CLAUDE_API_KEY")):
            source = f"模型配置“{profile['name']} · {profile['model']}”" if profile else "“继承原环境”"
            remedy = "请为此模型配置保存密钥，或为 Web 服务设置 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN。" if profile else (
                "请为 Web 服务设置 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN；要使用已配置的工作区模型，请按当前配置新建会话。")
            raise WebError(f"独立会话仅支持 API Key；本次实际采用{source}，未找到可用密钥。" + remedy, 409)
        argv = [sys.executable, str(SAPPER), "--store", str(Path(project["path"]) / ".sapper")]
        if native is not None:
            argv = native["argv"]
            env.update(native.get("env", {}))
            missing = [name for name in native.get("mcp_env_refs", []) if not env.get(name)]
            if missing:
                raise WebError("MCP 所需环境变量未配置：" + ", ".join(missing)
                               + "。请在工作区配置 → 环境变量中填写，或为 Web 服务进程设置后重启", 409)
            if native.get("isolated_extensions"):
                api_environment(env)
        elif kind in ("auto", "dispatch"):
            argv += [kind]
            if kind == "auto":
                argv += ["--minutes", str(number(data, "minutes", 10, 1, 1440)),
                         "--max-steps", str(number(data, "max_steps", 8, 1, 1000))]
                if data.get("verify"):
                    argv += ["--verify"]
            else:
                argv += [require_text(data, "target", 64, "reason")]
            if profile:
                argv += ["--model", profile["model"]]
            if data.get("skip_permissions"):
                argv += ["--skip-permissions"]
            if data.get("dry_run"):
                argv += ["--dry-run"]
        elif kind == "shell":
            argv = [os.environ.get("SHELL", "/bin/sh"), "-l"]
        elif kind == "custom":
            try:
                argv = shlex.split(require_text(data, "command", 8000))
            except ValueError as exc:
                raise WebError("命令引号不匹配") from exc
            if not argv:
                raise WebError("命令不能为空")
        elif kind == "worker":
            action = data.get("worker_action", "spawn")
            if action not in ("spawn", "kill", "list", "board"):
                raise WebError("不支持的 worker 操作")
            argv = ["bash", str(REPO / "docker/commander/worker.sh"), action]
            # worker.sh overloads SAPPER_BIN as the sapper executable. Preserve
            # the caller's setting; do not export "sapper" as a model CLI to TUI children.
            if "SAPPER_BIN" in os.environ:
                env["SAPPER_BIN"] = os.environ["SAPPER_BIN"]
            else:
                env.pop("SAPPER_BIN", None)
            if action in ("spawn", "kill"):
                target = require_text(data, "worker_target", 100)
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", target):
                    raise WebError("worker 目标格式不正确")
                argv += [target]
            if action == "spawn":
                argv += ["--tui", "--minutes", str(number(data, "minutes", 10, 1, 1440))]
                if data.get("mission"):
                    argv += ["--mission", require_text(data, "mission")]
                if profile:
                    argv += ["--model", profile["model"]]
            if action == "kill" and data.get("close_instance"):
                argv += ["--close"]
        else:
            raise WebError("不支持的运行方式")
        executable = argv[0]
        if not shutil.which(executable, path=env["PATH"]):
            raise WebError(f"找不到命令 {executable}；请检查当前服务所在环境", 409)
        if kind in ("auto", "dispatch") and not data.get("dry_run"):
            engine = shlex.split(env.get("SAPPER_BIN") or "claude")[0]
            if not shutil.which(engine, path=env["PATH"]):
                raise WebError(f"找不到原 agent 引擎 {engine}，请先在当前环境安装或选择原 CLI 包装器", 409)
        request_id = data.get("request_id")
        with self.lock:
            if request_id:
                for info in self.runs(pid):
                    if info.get("request_id") == request_id:
                        return info
            rid = secrets.token_hex(8)
            env["SAPPER_RUN_ID"] = rid
            run_dir = self.run_path(rid)
            run_dir.mkdir(parents=True, mode=0o700)
            session = "sapper-web-" + rid
            cwd = native.get("cwd", project["path"]) if native else project["path"]
            info = {"id": rid, "project_id": pid, "kind": kind, "status": "starting",
                    "session": session, "created_at": sapper.now_iso(), "request_id": request_id,
                    "command": shlex.join(argv), "cwd": cwd,
                    "profile_name": profile["name"] if profile else "继承原环境"}
            if native:
                info.update({k: native[k] for k in ("agent_id", "claude_session_id", "intent_id", "role", "timeout_seconds", "native_operation", "execution_mode", "output_format", "companion_required") if k in native})
                info.update({k: native["permission_audit"][k] for k in PERMISSION_AUDIT_FIELDS
                             if k in native.get("permission_audit", {})})
            # Frozen spec v1.0 二: every spawn gets a monotonic generation code.
            # Allocate it in the control plane before the child exists so the
            # ready handshake can carry it and late acks from old spawns are
            # discardable.
            from sapper_host import SubprocessHost, next_generation
            generation = next_generation(self.state)
            info["generation"] = generation
            info["epoch"] = generation
            info["supervisor_version"] = 1
            # Keep the (non-secret) initial prompt in the durable run record so
            # a resume's briefing can be audited without the transient, secret-
            # bearing launch.json.
            if (native or {}).get("initial_prompt"):
                info["initial_prompt"] = native["initial_prompt"]
            private_json(run_dir / "run.json", info)
            private_json(run_dir / "launch.json", {"argv": argv, "env": env,
                         "initial_prompt": (native or {}).get("initial_prompt"),
                         "resume_chat": (native or {}).get("resume_chat", False)})
            # Frozen spec v1.0 二: host the supervisor directly in its own
            # process group.  No tmux/PTY: stdout is the structured NDJSON
            # stream, stderr is isolated to its own file, and terminal/mux env
            # is stripped inside SubprocessHost.spawn.
            wrapper = [sys.executable, str(Path(__file__).resolve()), "_runner", str(run_dir)]
            try:
                host = SubprocessHost(run_dir, role=info.get("role") or "worker",
                                      generation=generation)
                child, host_record = host.spawn(
                    wrapper, cwd, env,
                    stdout_path=run_dir / "output.log",
                    stderr_path=run_dir / "host-stderr.log")
                host.close_stdin(child)
                host.close()
                (run_dir / "ready").touch()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                info.update(status="failed", ended_at=sapper.now_iso())
                private_json(run_dir / "run.json", info)
                raise WebError("无法启动无头宿主: " + str(exc), 503) from exc
        self.audit("run.started", project=pid, run=rid, kind=kind)
        return info

    def stop(self, rid):
        with self.workspaces.lifecycle():
            info = self.run_info(rid)
            if info.get("role") == "watchdog":
                for a in self.workspaces.agents(info["project_id"]):
                    if a.get("role") == "commander" and not a.get("legacy"):
                        self.workspaces.stop_agent(a["id"])
            return self._stop(rid)

    def _stop(self, rid):
        with self.workspaces.lifecycle():
            info = self.run_info(rid)
            if info.get("agent_id"):
                agent = self.workspaces.agent(info["agent_id"], include_deleted=True)
                if agent.get("run_id") == rid:
                    return self.workspaces.stop_agent(agent["id"])
            return self._stop_run(rid)

    def _stop_run(self, rid, *, processes=None):
        """Low-level shutdown; workspace callers must confirm exit before release."""
        info = self.run_info(rid)
        if info.get("agent_id"):
            agent = self.workspaces.agent(info["agent_id"], include_deleted=True)
            if agent.get("run_id") == rid:
                self.workspaces.mark_stopped(info["agent_id"])
        if info["status"] in FINISHED:
            return info
        if processes is None:
            from sapper_processes import RunProcesses
            try:
                processes = RunProcesses(info)
            except (OSError, ValueError, subprocess.SubprocessError):
                processes = None
        run_dir = self.run_path(rid)
        with run_lock(run_dir):
            (run_dir / "stop_requested").touch()
        if info.get("supervisor_version") == 1 and info.get("execution_mode") in ("print", "chat"):
            # Let the owner interrupt Claude and drain its final output first.
            end = time.monotonic() + INTERRUPT_GRACE + TERMINATE_GRACE + 4
            while time.monotonic() < end:
                current = self.run_info(rid)
                if current["status"] in FINISHED:
                    # Wait for the host's wrapper to actually exit so a resume
                    # cannot rediscover a still-live process group.
                    for _ in range(20):
                        if not self._host_alive(info):
                            break
                        time.sleep(0.05)
                    self.audit("run.stopped", run=rid)
                    return current
                time.sleep(0.1)
        if processes is not None:
            # Never signal raw persisted PGIDs: they may now name another run.
            # The observer includes both original groups and detached tools.
            try:
                processes.signal(signal.SIGTERM)
                time.sleep(0.2)
                processes.signal(signal.SIGKILL)
            except (OSError, ValueError, subprocess.SubprocessError):
                # Still close the run's own tmux session. The workspace owner
                # will retain claims unless its observer can confirm exit.
                pass
        # Frozen spec v1.0 三: physically reap only this run's own process tree.
        # No global kill.  Reaping is best-effort here because the supervisor may
        # already have finished; identity is confirmed by PID birth.
        if (run_dir / "host.json").exists():
            from sapper_host import SubprocessHost
            try:
                SubprocessHost(run_dir, role=info.get("role") or "worker",
                               generation=int(info.get("generation") or 0)).reap(force=True)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        info = self.run_info(rid)
        info.update(status="stopped", ended_at=sapper.now_iso())
        private_json(run_dir / "run.json", info)
        self.audit("run.stopped", run=rid)
        return info

    @staticmethod
    def _render_turn_event(event):
        kind = event.get("type")
        if kind == "tool_call":
            detail = json.dumps({"name": event.get("name"), "input": event.get("input", {})},
                                ensure_ascii=False)
        elif kind == "tool_result":
            detail = event.get("text") or event.get("attachment_path") or json.dumps(
                {"media": event.get("media", []), "total_bytes": event.get("total_bytes", 0),
                 "total_lines": event.get("total_lines", 0)}, ensure_ascii=False)
        elif kind == "turn_settlement":
            detail = json.dumps({"terminal_state": event.get("terminal_state"),
                                 "usage": event.get("usage", {}),
                                 "result": event.get("result_preview", "")}, ensure_ascii=False)
        else:
            detail = event.get("text", "")
        return "[%s turn=%s seq=%s] %s" % (kind, event.get("turn_id"), event.get("seq"), detail)

    def _turn_log_text(self, directory):
        events = []
        after = 0
        while True:
            page = read_events(directory / "turns.jsonl", after, 1000)
            events.extend(page["events"])
            if not page["has_more"] or not page["events"]:
                break
            after = page["next_after_seq"]
        return "\n".join(self._render_turn_event(event) for event in events)

    def log(self, rid, offset=0):
        info = self.run_info(rid)
        directory = self.run_path(rid)
        if (directory / "turns.jsonl").exists():
            # Frozen spec v1.0 阶段三: for structured runs the normalized event
            # stream is the only consumed source, even through the old read API.
            raw = self._turn_log_text(directory).encode("utf-8")
            size = len(raw)
            offset = min(max(0, offset), size)
            chunk = raw[offset:offset + 128 * 1024]
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            decoded = decoder.decode(chunk, final=info["status"] in FINISHED and offset + len(chunk) >= size)
            pending = decoder.getstate()[0]
            return {"text": self.redact(ANSI.sub("", decoded)),
                    "offset": offset + len(chunk) - len(pending), "size": size}
        path = directory / "activity.log"
        if not path.exists():
            path = directory / "output.log"
        size = path.stat().st_size if path.exists() else 0
        offset = min(max(0, offset), size)
        if not path.exists():
            return {"text": "", "offset": 0, "size": 0}
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(128 * 1024)
        # A chunk boundary must not replace half of a Chinese character. Leave
        # an incomplete trailing sequence for the next poll while the run lives.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        decoded = decoder.decode(chunk, final=info["status"] in FINISHED and offset + len(chunk) >= size)
        pending = decoder.getstate()[0]
        return {"text": self.redact(ANSI.sub("", decoded)),
                "offset": offset + len(chunk) - len(pending), "size": size}

    def turn_events(self, rid, after_seq=0, limit=None):
        """Stage three canonical read: strict seq cursor over turns.jsonl."""
        self.run_info(rid)
        # Same credential boundary as the legacy log: disk keeps the original
        # payload, but every read API must redact configured secrets.
        keys = self.redaction_keys()
        result = read_events(self.run_path(rid) / "turns.jsonl", after_seq, limit)
        return json.loads(self.redact(json.dumps(result, ensure_ascii=False), keys=keys))

    def attachment_range(self, rid, relative, start_line=1, limit=None):
        """Read a sed-aligned line range from a run attachment, with dual caps."""
        directory = self.run_path(rid)
        relative = str(relative or "")
        parts = Path(relative).parts
        if Path(relative).is_absolute() or not parts or any(p in ("", ".", "..") for p in parts):
            raise WebError("附件路径不合法", 400)
        if any("\x00" in part for part in parts):
            raise WebError("附件路径不合法", 400)
        root = directory.resolve()
        resolved = (directory / relative).resolve()
        if not resolved.is_relative_to(root):
            raise WebError("附件路径不得逃出运行目录", 403)
        if not resolved.is_file():
            raise WebError("附件不存在", 404)
        start_line = min(max(1, int(start_line)), 10 ** 9)
        limit = ATTACHMENT_PAGE_DEFAULT if limit is None else min(max(1, int(limit)), ATTACHMENT_MAX_LINES)

        lines = []
        used = 0
        end_line = start_line - 1
        total_lines = 0
        has_more = False
        with resolved.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                total_lines = number
                if number < start_line:
                    continue
                if len(lines) >= limit or used >= ATTACHMENT_MAX_BYTES:
                    has_more = True
                    continue
                data = raw.rstrip(b"\n")
                remaining = ATTACHMENT_MAX_BYTES - used
                line_truncated = False
                if len(data) > remaining:
                    data = data[:remaining]
                    line_truncated = True
                try:
                    value = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    # A byte cap may cut a multi-byte code point: keep only the
                    # complete prefix instead of emitting U+FFFD replacement.
                    value = data[:exc.start].decode("utf-8")
                    line_truncated = True
                lines.append((number, value, line_truncated))
                end_line = number
                used += len(data)
        text = "\n".join(value for _number, value, _cut in lines)
        text = self.redact(text)
        return {
            "path": relative,
            "start_line": start_line if lines else end_line,
            "end_line": end_line,
            "bytes": len(text.encode("utf-8")),
            "text": text,
            "line_truncated": any(cut for _number, _value, cut in lines),
            "total_lines": total_lines,
            "has_more": has_more,
        }

    def file(self, pid, relative):
        project_dir = Path(self.project(pid)["path"])
        relative = str(relative)
        parts = Path(relative).parts
        if Path(relative).is_absolute() or any(p in HIDDEN or p == ".." for p in parts):
            raise WebError("文件路径不允许访问", 403)
        if any(p.startswith(".env") or p.endswith(".env") for p in parts):
            raise WebError("环境凭据文件不通过普通文件接口提供", 403)
        resolved = self.inside(project_dir / relative, project_dir)
        if resolved.is_relative_to((project_dir / ".sapper/sessions").resolve()):
            raise WebError("会话历史与配置不通过普通工作区文件接口提供", 403)
        if resolved.is_relative_to(self.state):
            raise WebError("Web 私有运行与凭据目录不通过工作区文件接口提供", 403)
        return resolved

    def files(self, pid, relative="."):
        path = self.file(pid, relative)
        if not path.is_dir():
            raise WebError("目录不存在", 404)
        project_dir = Path(self.project(pid)["path"])
        result = []
        for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            rel = str(item.relative_to(project_dir))
            try:
                resolved = self.file(pid, rel)
                result.append({"name": item.name, "path": rel, "directory": resolved.is_dir(),
                               "size": resolved.stat().st_size if resolved.is_file() else 0})
            except (WebError, OSError):
                continue
            if len(result) >= 500:
                break
        return result

    def stage_attachment(self, session_hash, name, body):
        filename = attachment_name(name)
        root = self.state / "staged-attachments"
        with self.config_lock():
            root.mkdir(mode=0o700, exist_ok=True)
            cutoff = time.time() - STAGING_TTL
            for entry in root.iterdir():
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry)
            token = secrets.token_hex(16)
            directory = root / (session_hash + "-" + token)
            directory.mkdir(mode=0o700)
            try:
                fd = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(body)
            except OSError:
                shutil.rmtree(directory)
                raise
        return {"id": token}

    def staged_file(self, session_hash, token):
        if not session_hash or not re.fullmatch(r"[0-9a-f]{32}", token):
            raise WebError("暂存附件编号不正确")
        directory = self.state / "staged-attachments" / (session_hash + "-" + token)
        if directory.is_dir() and directory.stat().st_mtime >= time.time() - STAGING_TTL:
            files = list(directory.iterdir())
            if len(files) == 1 and files[0].is_file():
                return files[0]
        raise WebError("暂存附件不存在或已过期，请重新上传", 404)

    def upload(self, pid, name, body):
        relative = "attachments/" + attachment_name(name)
        path = self.file(pid, relative)
        path.parent.mkdir(exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        self.store(pid).add_hint(f"用户上传附件：{relative}（{len(body)} bytes）；请按任务需要读取。", "web-user")
        self.audit("attachment.saved", project=pid, path=relative, size=len(body))
        return {"path": relative, "size": len(body), "message": "文件已保存并记入 hint，未自动发送到其他会话"}


def route_due_events(state):
    """Control-plane tick: escalate failure events past their confirmation deadline.

    Ownership moves up one level on each timeout; a notice that cannot be
    delivered is escalated at emit time (see _report_failure).  No business text
    is interpreted here — only event ids and structural fields.
    """
    if not state:
        return
    root = Path(state) / "routing"
    if not root.exists():
        return
    from sapper_routing import RoutingLedger
    from sapper_chat import ChatJournal
    for workspace in root.iterdir():
        if not workspace.is_dir():
            continue
        ledger = RoutingLedger(state, workspace.name)
        for event_id in ledger.due():
            try:
                event = ledger.view(event_id)
                ledger.escalate(event_id, reason="ack_timeout")
                updated = ledger.view(event_id)
                new_owner = updated.get("owner_agent_id")
                if new_owner:
                    ChatJournal(state, new_owner).enqueue(
                        "[Sapper 故障待办升级] kind=%s event_id=%s source=%s generation=%s" % (
                            event.get("kind"), event_id, event.get("source_agent_id"),
                            event.get("generation")),
                        message_id=event_id, internal=True)
                    ledger.delivered(event_id)
            except (OSError, ValueError, RuntimeError):
                continue


def _report_failure(directory, info, *, state, ready, last_heartbeat, started, reason):
    """Classify a supervisor exit and route the structural failure event.

    Control-plane only: the ledger stores ids/generation/status/deadlines and
    the structural evidence, never business text.  Decisions about what a
    failure means for the task stay with the owning agent.
    """
    from sapper_host import classify_exit
    from sapper_routing import RoutingLedger
    result = classify_exit(directory, info.get("exit_code", 1), ready=ready,
                           last_heartbeat=last_heartbeat, started_monotonic=started,
                           reason=reason)
    if not result or result.get("cleanup_unconfirmed"):
        # Physical cleanup could not be confirmed: do not emit a terminal
        # failure or release the slot (frozen spec v1.0 三).
        return None
    agent_id = info.get("agent_id")
    if not state or not agent_id:
        return None
    agent_path = Path(state) / "agents" / f"{agent_id}.json"
    try:
        agent = json.loads(agent_path.read_text())
    except (OSError, ValueError):
        agent = {}
    superior_id = agent.get("parent_agent_id")
    role = info.get("role") or agent.get("role") or "worker"
    level = "commander" if role == "commander" else "general" if role == "general" else "worker"
    if role == "commander" or not superior_id:
        try:
            top = json.loads((Path(state) / "main-agents" / f"{info['project_id']}.json").read_text())
            superior_id = top.get("commander_id") or superior_id
        except (OSError, ValueError):
            pass
    if not superior_id:
        return None
    evidence = result.get("evidence") or {}
    try:
        ledger = RoutingLedger(state, info["project_id"])
        event_id = ledger.emit(kind=result["status"], generation=result["generation"],
                               source_agent_id=agent_id, superior_id=superior_id,
                               cwd=info.get("cwd") or "", level=level,
                               side_effect_warning=evidence.get("side_effect_warning", ""),
                               evidence=evidence)
        # Frozen spec v1.0 四: deliver the primary notice to the current owner;
        # a delivery failure escalates IMMEDIATELY (no timeout wait) up the
        # chain, re-noticing each new owner, until one accepts or the top is
        # reached.  Bounded by the chain depth to avoid an infinite loop.
        from sapper_chat import ChatJournal
        def _notice(owner):
            return ChatJournal(state, owner).enqueue(
                "[Sapper 故障待办] kind=%s event_id=%s source=%s generation=%s cwd=%s" % (
                    result["status"], event_id, agent_id, result["generation"], info.get("cwd") or ""),
                message_id=event_id, internal=True)
        try:
            _notice(superior_id)
            ledger.delivered(event_id)
        except (OSError, ValueError, RuntimeError):
            for _ in range(16):
                ledger.escalate(event_id, reason="primary_delivery_failed")
                new_owner = ledger.view(event_id)["owner_agent_id"]
                try:
                    _notice(new_owner)
                    ledger.delivered(event_id)
                    break
                except (OSError, ValueError, RuntimeError):
                    if ledger.view(event_id).get("escalation_frozen"):
                        break
    except (OSError, ValueError, RuntimeError):
        return None
    info["routing_event_id"] = event_id
    return result


def run_child(directory):
    """Supervise the headless command; all runs host structured stream pipes."""
    directory = Path(directory)
    info = read_json(directory / "run.json")
    launch = read_json(directory / "launch.json")
    # Credentials are needed to create the child, not as a persistent run record.
    (directory / "launch.json").unlink()
    for _ in range(100):
        if (directory / "ready").exists():
            break
        time.sleep(0.05)
    else:
        info.update(status="failed", ended_at=sapper.now_iso(), error="日志采集未就绪")
        private_json(directory / "run.json", info)
        return 1
    env = launch["env"]
    child = None
    stop_signal = None
    reader = None
    output = None
    chat_mode = info.get("execution_mode") == "chat"
    managed_print = info.get("execution_mode") in ("print", "chat")
    # Frozen spec v1.0 二: headless children must not see terminal/multiplexer
    # variables (faking them enables interactive-only CLI behavior).  TUI runs
    # still live inside tmux until the cutover, so keep their env intact.
    if managed_print:
        from sapper_host import clean_headless_environment
        env = clean_headless_environment(env)
    else:
        for key in ("TERM", "TMUX", "TMUX_PANE"):
            if key in os.environ:
                env[key] = os.environ[key]
    journal = None
    initialized = threading.Event()
    # Frozen spec v1.0 二/三: readiness for headless stream-json runs is the
    # CLI's system/init frame (or the generation ack for chat).  Until it is
    # seen the process is NOT ready, so STARTUP_TIMEOUT stays reachable for
    # non-chat runs too.
    process_ready = threading.Event()
    init_error = []
    # Frozen spec v1.0 二: the generation is the spawn's monotonic identity;
    # older run records default to 0 so this stays behavior-inert until the
    # host seeds it.
    generation = int(info.get("generation") or 0)
    from sapper_host import startup_timeout_for
    host_record = read_json(directory / "host.json", {})
    startup_timeout = int(host_record.get("startup_timeout")
                          or startup_timeout_for(info.get("role") or "worker"))
    # Frozen spec v1.0 二: one monotonic clock from the spawn instant.  The
    # host records it before the wrapper Popen; the supervisor starts later, so
    # it must measure against that origin (covering wrapper + ready wait) rather
    # than a fresh clock at CLI-child Popen.
    spawn_monotonic = host_record.get("spawn_monotonic")
    if chat_mode:
        from sapper_chat import ChatJournal
        journal = ChatJournal(env["SAPPER_WEB_STATE"], info["agent_id"])
        journal.begin(info["id"], launch["initial_prompt"], resume=launch.get("resume_chat", False),
                      generation=generation)
    if info.get("output_format") == "stream-json":
        from sapper_run import RunOutput
        # Frozen spec v1.0 阶段三: only the bidirectional headless (chat) mode
        # joins the structured contract.  One-way print runs keep their log.
        output = RunOutput(directory, private_json,
                           structured=info.get("execution_mode") == "chat")

    def note(message):
        # Frozen spec v1.0 二: stdout is reserved for the structured NDJSON
        # stream; operational status goes to stderr (isolated from that stream).
        print(message, file=sys.stderr, flush=True)
        if output:
            output.note(message)

    def request_stop(signum, frame):
        nonlocal stop_signal
        stop_signal = signum

    def child_signal(signum):
        with contextlib.suppress(ProcessLookupError):
            if managed_print:
                os.killpg(child.pid, signum)
            elif child.poll() is None:
                child.send_signal(signum)

    def relay():
        try:
            # Frozen spec v1.0 三: a single oversized protocol frame must not be
            # truncated by line buffering; unterminated tail fragments are
            # isolated into diagnostics instead of entering the ledger.
            for line in iter_frames(child.stdout):
                if not line.endswith(b"\n"):
                    if output and output.structured:
                        output.stream.diagnose(line, "crash_fragment", "stdout")
                    continue
                event = None
                if output and output.structured:
                    # Validate before forwarding: protocol noise must not reach
                    # the host stdout capture (output.log) or the old /log read
                    # path. It is isolated into diagnostics only.
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        event = None
                    if not (isinstance(event, dict) and isinstance(event.get("type"), str)):
                        output.stream.diagnose(line, "invalid_json" if event is None else "not_a_protocol_frame", "stdout")
                        continue
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                output.observe(line)
                if event is None and (output or journal):
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                # Any valid structured frame proves the headless CLI is alive
                # and speaking the protocol — the readiness floor for
                # STARTUP_TIMEOUT (system/init is the stronger signal).
                if isinstance(event, dict):
                    process_ready.set()
                if journal:
                    journal.receive(info["id"], info.get("claude_session_id", ""), event)
                    # Frozen spec v1.0 二: only an ack echoed with the current
                    # generation may mark the process ready; a late ack from a
                    # superseded generation is discarded.
                    from sapper_host import validate_init_ack
                    ack = validate_init_ack(event, generation)
                    if ack == "error":
                        init_error.append(event["response"].get("error", "初始化失败"))
                    elif ack == "ok":
                        initialized.set()
                        process_ready.set()
                    elif ack == "stale":
                        note("[sapper-web] 丢弃旧代号就绪信号")
                    elif (event.get("type") == "system" and event.get("subtype") == "init"
                          and event.get("session_id") == info.get("claude_session_id")):
                        # Real Claude Code emits a system/init frame on startup
                        # but does not answer our custom initialize control_request.
                        # It is session-bound to this run, so it is a safe ready
                        # signal when the generation-carrying ack never arrives.
                        initialized.set()
                        process_ready.set()
                    elif event.get("type") == "control_request" and event.get("request", {}).get("subtype") != "can_use_tool":
                        # Unknown requests must receive an explicit error, not hang.
                        with journal.locked() as data:
                            data.setdefault("responses", []).append({"type": "control_response", "response": {
                                "subtype": "error", "request_id": event["request_id"],
                                "error": "Unsupported control request"}})
        finally:
            child.stdout.close()

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    rc = 1
    reason = None
    stderr_fp = None
    try:
        options = {"start_new_session": True} if managed_print else {}
        # Every headless child gets a stdin pipe owned by this supervisor.  The
        # wrapper's own stdin was closed by the host (no PTY), so literal input
        # from the web is relayed through an inbox file into this pipe.
        options["stdin"] = subprocess.PIPE
        if output:
            # Frozen spec v1.0 二: stdout carries only the NDJSON turn stream;
            # stderr is isolated to its own file so CLI warnings/hook errors
            # cannot corrupt the stream or block on an undrained PIPE.
            options["stdout"] = subprocess.PIPE
            stderr_path = directory / "stderr.log"
            stderr_fp = stderr_path.open("ab")
            options["stderr"] = stderr_fp
        child = subprocess.Popen(launch["argv"], cwd=info["cwd"], env=env, **options)
        started = float(spawn_monotonic) if isinstance(spawn_monotonic, (int, float)) else time.monotonic()
        info.update(status="running", started_at=sapper.now_iso(), pid=child.pid,
                    started_epoch=time.time(),
                    wrapper_pid=os.getpid(), process_group=os.getpgrp())
        if managed_print:
            info["child_process_group"] = child.pid
        from sapper_processes import process_table
        try:
            births = process_table()
            info["process_births"] = {str(pid): births[pid]["birth"] for pid in (os.getpid(), child.pid) if pid in births}
        except (OSError, subprocess.SubprocessError):
            info["process_observation"] = "unavailable"
        with run_lock(directory):
            private_json(directory / "run.json", info)
        note("[sapper-web] 原命令已启动，输出从此处开始采集。")
        if output:
            reader = threading.Thread(target=relay, daemon=True)
            reader.start()
        if journal:
            from sapper_host import init_request_id
            child.stdin.write((json.dumps({"type": "control_request",
                "request_id": init_request_id(generation),
                "request": {"subtype": "initialize", "hooks": None}}) + "\n").encode())
            child.stdin.flush()
        phase_end = None
        stage = None
        stdin_offset = 0
        warned_budget = None
        companion_checked = 0
        companion_missing = None
        latest_companion = ("", "")
        while child.poll() is None:
            now = time.monotonic()
            watchdog_lost = False
            if reason is None and info.get("companion_required") and now - companion_checked >= 1:
                companion_checked = now
                alive = False
                for path in (Path(env["SAPPER_WEB_STATE"]) / "services").glob("*.json"):
                    service = read_json(path, {})
                    if service.get("workspace_id") != info["project_id"]:
                        continue
                    companion_id = service["run_id"]
                    run = read_json(Path(env["SAPPER_WEB_STATE"]) / "runs" / companion_id / "run.json", {})
                    if run:
                        latest_companion = max(latest_companion, (run.get("created_at") or "", companion_id))
                    if run.get("status") == "starting" and now - started < 10:
                        alive = True
                    elif run.get("status") == "running" and run.get("wrapper_pid"):
                        try:
                            os.kill(run["wrapper_pid"], 0)
                            alive = True
                        except ProcessLookupError:
                            pass
                companion_missing = None if alive else companion_missing or now
                watchdog_lost = companion_missing is not None and now - started > 10 and now - companion_missing >= 2
            if journal and reason is None and not watchdog_lost:
                if init_error:
                    raise OSError("Claude Code 聊天初始化失败：" + init_error[0])
                if not initialized.is_set() and now - started > startup_timeout:
                    raise OSError("Claude Code 聊天初始化超时")
                if initialized.is_set():
                    frames = journal.outgoing(info["id"])
                    for frame in frames:
                        if frame["type"] == "user":
                            frame["session_id"] = info["claude_session_id"]
                        child.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode())
                    child.stdin.flush()
                    journal.sent(info["id"], frames)
            if output and not journal and reason is None and not watchdog_lost:
                # Frozen spec v1.0 二/三: a headless stream-json run that never
                # speaks the protocol within startup_timeout is a STARTUP_TIMEOUT
                # (hard-kill, no graceful exit), not a forced-ready success.
                if not process_ready.is_set() and now - started > startup_timeout:
                    raise OSError("无头进程启动就绪超时")
            if not chat_mode and reason is None:
                # Literal web input for non-chat runs: the inbox file is the
                # control-plane boundary; the supervisor owns the actual stdin.
                inbox = directory / "stdin-inbox"
                if inbox.exists():
                    data = inbox.read_bytes()
                    if len(data) > stdin_offset:
                        chunk = data[stdin_offset:]
                        stdin_offset = len(data)
                        # No PTY line discipline: a carriage return from the web
                        # terminal must become a newline to terminate a line.
                        chunk = chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                        # The old send-keys path always appended Enter for text
                        # input.  Without a PTY we must terminate the line here.
                        if chunk and not chunk.endswith(b"\n"):
                            chunk += b"\n"
                        with contextlib.suppress(OSError, ValueError):
                            child.stdin.write(chunk)
                            child.stdin.flush()
            with run_lock(directory):
                control = read_json(directory / "control.json", {})
                budget = control.get("timeout_seconds", info.get("timeout_seconds"))
                if reason is None:
                    if (directory / "stop_requested").exists():
                        reason = "stopped"
                    elif stop_signal:
                        reason = "interrupted"
                    elif budget is not None and now - started >= budget:
                        reason = "timed_out"
                    elif watchdog_lost:
                        reason = "stopped"
                        info.update(stop_source="watchdog_lost", watchdog_run_id=latest_companion[1] or None)
                    if reason:
                        info.update(status="stopping", stop_reason=reason, stopping_at=sapper.now_iso())
                        private_json(directory / "run.json", info)
            if reason and stage is None:
                if info.get("stop_source") == "watchdog_lost":
                    note("[sapper-web] watchdog 已退出，主 agent 同步停止。")
                    agent_path = Path(env["SAPPER_WEB_STATE"]) / "agents" / f"{info['agent_id']}.json"
                    agent = read_json(agent_path, {})
                    if agent.get("run_id") == info["id"]:
                        agent["desired_state"] = "stopped"
                        private_json(agent_path, agent)
                else:
                    note("[sapper-web] " + ("预算耗尽" if reason == "timed_out" else "收到停止请求") + "，正在中断并等待本轮退出。")
                child_signal(signal.SIGINT if managed_print else signal.SIGTERM)
                stage = "interrupt"
                phase_end = now + (INTERRUPT_GRACE if managed_print else TERMINATE_GRACE)
            elif reason and now >= phase_end:
                if stage == "interrupt":
                    note("[sapper-web] 宽限期结束，发送 SIGTERM。")
                    child_signal(signal.SIGTERM)
                    stage = "terminate"
                    phase_end = now + TERMINATE_GRACE
                else:
                    note("[sapper-web] 退出宽限期结束，清理剩余进程。")
                    child_signal(signal.SIGKILL)
                    break
            elif reason is None and budget is not None and budget - (now - started) <= min(60, budget / 5) and warned_budget != budget:
                warned_budget = budget
                note("[sapper-web] 本次运行预算即将耗尽；可续时，成果请持续写入原图和文件。")
            time.sleep(0.1)
        rc = child.wait()
        if rc == 0 and info.get("native_operation") == "teardown":
            from sapper_native import cleanup_bridge
            cleanup_bridge(Path(info["cwd"]))
    except (OSError, KeyboardInterrupt) as exc:
        rc = 1
        note(f"[sapper-web] {exc}")
        if child and child.poll() is None:
            child_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=3)
    finally:
        if stderr_fp is not None:
            with contextlib.suppress(OSError):
                stderr_fp.close()
        if child and child.poll() is None:
            child_signal(signal.SIGKILL)
            child.wait()
        if child and managed_print:
            # Also reap tools left behind by an exited or uncooperative CLI.
            child_signal(signal.SIGTERM)
            if reader:
                reader.join(timeout=TERMINATE_GRACE)
            child_signal(signal.SIGKILL)
        if reader:
            reader.join(timeout=2)
        if journal:
            journal.end(info["id"])
        with run_lock(directory):
            # Explicit cancellation wins a race with an otherwise normal exit.
            reason = reason or ("stopped" if (directory / "stop_requested").exists() else
                                "interrupted" if stop_signal else None)
            info.update(status=reason or "exited", ended_at=sapper.now_iso(), exit_code=rc)
            if reason:
                info["stop_reason"] = reason
            # Frozen spec v1.0 三/四: classify STARTUP_TIMEOUT vs CRASHED for
            # host-backed runs and route the event before persisting the final
            # status.  Legacy tmux runs have no host.json -> no classification.
            verdict = None
            try:
                last_heartbeat = read_json(directory / "activity.json", {}).get("last_event_at")
                # Frozen spec v1.0 三: readiness is a real protocol signal, never
                # forced True.  Stream-json runs use system/init (or gen ack);
                # plain commands have no protocol and their clean launch is the
                # only available signal.
                if output:
                    ready = bool(process_ready.is_set() or initialized.is_set())
                else:
                    ready = child is not None
                verdict = _report_failure(
                    directory, info, state=env.get("SAPPER_WEB_STATE", ""),
                    ready=ready, last_heartbeat=last_heartbeat,
                    started=started, reason=reason)
                if verdict:
                    info["status"] = verdict["status"]
                    info["generation"] = verdict["generation"]
                    info["failure_evidence"] = verdict.get("evidence")
            except (OSError, ValueError, RuntimeError) as exc:
                note("[sapper-web] 故障上报失败：" + str(exc))
            # Frozen spec v1.0 阶段三: never leave a zombie in-progress turn.
            # A normal result frame already settled the turn; every other exit
            # path must synthesize an explicit abnormal terminal state.
            if output and not output.settled:
                status = verdict.get("status") if isinstance(verdict, dict) else None
                if reason == "timed_out" or status == "startup_timeout":
                    terminal = "timeout"
                elif status == "crashed":
                    terminal = "crashed"
                elif reason in ("stopped", "interrupted"):
                    terminal = "interrupted"
                elif rc not in (0, None):
                    terminal = "failed"
                else:
                    terminal = "completed"
                try:
                    output.settle(terminal)
                except (OSError, ValueError) as exc:
                    note("[sapper-web] 异常轮次结算补写失败：" + str(exc))
            private_json(directory / "run.json", info)
        # Credential-bearing launch data is no longer needed after this command.
        with contextlib.suppress(FileNotFoundError):
            (directory / "launch.json").unlink()
    return rc


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # A page requests many scripts/styles concurrently, including after reload.
    request_queue_size = 64

    def __init__(self, address, console, public_url="", prefix=""):
        self.console = console
        self.prefix = prefix.rstrip("/")
        self.secure_cookie = public_url.startswith("https://")
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1", address[0]}
        if public_url:
            self.allowed_hosts.add(urllib.parse.urlsplit(public_url).hostname)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "SapperWeb/0.1"

    def log_message(self, *args):
        pass  # Do not log credential-bearing URLs, bodies or terminal input.

    def reply(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(payload)

    def body(self, raw=False):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise WebError("Content-Length 不正确") from exc
        if not 0 <= size <= MAX_BODY:
            raise WebError("请求过大，单次最多 16 MiB", 413)
        data = self.rfile.read(size)
        if len(data) != size:
            raise WebError("请求内容不完整，请重试")
        if raw:
            return data
        if self.headers.get_content_type() != "application/json":
            raise WebError("需要 JSON 请求", 415)
        try:
            obj = json.loads(data)
            if not isinstance(obj, dict):
                raise ValueError()
            return obj
        except ValueError as exc:
            raise WebError("JSON 请求不正确") from exc

    def session(self):
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        item = cookie.get("sapper_web")
        if not item:
            return None
        return self.server.console.auth.session(item.value)

    def dispatch(self, method):
        app = self.server.console
        try:
            host = urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname
            if host not in self.server.allowed_hosts:
                raise WebError("Host 未获允许，请通过本机地址或 --public-url 访问", 403)
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                raise WebError("拒绝跨站操作", 403)
            parsed = urllib.parse.urlsplit(self.path)
            prefix = self.server.prefix
            if prefix and parsed.path == prefix:
                return self.reply(302, b"", headers={"Location": prefix + "/"})
            if prefix and not parsed.path.startswith(prefix + "/"):
                raise WebError("页面不存在", 404)
            path = parsed.path[len(prefix):]
            query = urllib.parse.parse_qs(parsed.query)
            q = lambda name, default="": query.get(name, [default])[0]
            static_names = {"/", "/theme.js", "/app.js", "/style.css", "/workspace.js", "/extensions.js", "/workspace.css", "/overview.js", "/overview.css", "/native.js", "/native.css",
                            "/agent-topology.js", "/agent-topology-model.js", "/agent-topology.css", "/models.js", "/models.css", "/settings.css", "/competition.js", "/competition.css",
                            "/vendor/xterm.js", "/vendor/xterm.css", "/vendor/addon-fit.js"}
            if path in static_names and method == "GET":
                filename = "index.html" if path == "/" else path[1:]
                data = (STATIC / filename).read_bytes()
                return self.reply(200, data, mimetypes.guess_type(filename)[0] + "; charset=utf-8")
            if path == "/api/auth" and method == "GET":
                return self.reply(200, {"configured": app.auth.configured()})
            if path == "/api/session" and method == "POST":
                body = self.body()
                sid, result = app.auth.login(body.get("username"), body.get("password"), self.client_address[0])
                cookie = f"sapper_web={sid}; HttpOnly; SameSite=Strict; Path={prefix or '/'}; Max-Age={SESSION_SECONDS}"
                if self.server.secure_cookie:
                    cookie += "; Secure"
                return self.reply(200, result, headers={"Set-Cookie": cookie})
            session = self.session()
            if not session:
                raise WebError("请先使用用户名和密码登录", 401)
            if method != "GET" and not hmac.compare_digest(self.headers.get("X-Sapper-CSRF", ""), session["csrf"]):
                raise WebError("请求校验失败，请刷新后重试", 403)
            if path == "/api/session" and method == "GET":
                return self.reply(200, {"authenticated": True, **{k: session[k] for k in ("csrf", "username", "role")}})
            if path == "/api/account/password" and method == "POST":
                body = self.body()
                app.auth.change_password(session, body.get("current_password"), body.get("new_password"), self.client_address[0])
                return self.reply(200, {"ok": True}, headers={"Set-Cookie": self.expired_cookie()})
            if path == "/api/logout" and method == "POST":
                app.auth.logout(session)
                return self.reply(200, {"ok": True}, headers={"Set-Cookie": self.expired_cookie()})
            if path == "/api/health" and method == "GET":
                return self.reply(200, {"root": str(app.root), "state": str(app.state),
                                       "claude": bool(shutil.which("claude")),
                                       "sapper": str(SAPPER), "worker_script": str(REPO / "docker/commander/worker.sh"),
                                       "workspace_roots": [str(p) for p in app.workspace_roots]})
            if path == "/api/directories" and method == "GET":
                return self.reply(200, app.workspaces.directories(q("path"), for_skill=q("kind") == "skill"))
            if path == "/api/overview" and method == "GET":
                return self.reply(200, app.overview())
            if path == "/api/context":
                if method == "GET":
                    return self.reply(200, app.global_context())
                if method == "POST":
                    return self.reply(200, app.save_context(self.body()))
            if path == "/api/native/catalog" and method == "GET":
                return self.reply(200, app.native.catalog())
            if path.startswith("/api/native/"):
                parts = path.split("/")
                if len(parts) == 5:
                    pid, action = parts[3:]
                    if action == "config":
                        if method == "GET":
                            return self.reply(200, app.native.read_config(pid, q("path")))
                        if method == "POST":
                            return self.reply(200, app.native.save_config(pid, self.body()))
                    if method == "POST":
                        data = self.body()
                        if action == "preview":
                            return self.reply(200, app.native.prepare(pid, data))
                        if action == "execute":
                            return self.reply(201, app.native.execute(pid, data))
            if path == "/api/workspaces/bind" and method == "POST":
                return self.reply(201, app.workspaces.bind(self.body()))
            if path.startswith("/api/workspaces/"):
                parts = path.split("/")
                if len(parts) == 5:
                    wid, action = parts[3:]
                    if method == "GET":
                        if action == "role-models":
                            return self.reply(200, app.role_models(wid))
                        if action == "env":
                            return self.reply(200, app.workspace_env(wid))
                        if action == "extensions":
                            return self.reply(200, app.workspaces.extensions.view(wid))
                        if action == "inspect":
                            return self.reply(200, app.workspaces.inspect(wid))
                        if action == "agents":
                            return self.reply(200, app.workspace_status(wid))
                        if action == "role-prompt":
                            scope = [item for item in q("scope").split(",") if item] or None
                            return self.reply(200, app.workspaces.role_info(wid, q("role") or "worker", scope))
                        if action == "chat":
                            return self.reply(200, app.workspaces.chat_view(wid, q("before") or None, q("agent_id") or None))
                    elif method == "POST":
                        data = self.body()
                        if action == "role-models":
                            return self.reply(200, app.save_role_models(data, wid))
                        if action == "env":
                            return self.reply(200, app.save_workspace_env(data, wid))
                        if action == "extensions":
                            return self.reply(200, app.workspaces.extensions.update(wid, data))
                        if action == "enable":
                            return self.reply(200, app.workspaces.enable(wid, data))
                        if action == "settings":
                            return self.reply(200, app.workspaces.save_settings(wid, data))
                        if action == "competition":
                            return self.reply(200, app.import_competition(wid, data))
                        if action == "sessions":
                            return self.reply(201, app.workspaces.create_session(wid, data))
                        if action == "dispatch":
                            return self.reply(201, app.workspaces.dispatch(wid, data))
                        if action == "chat":
                            return self.reply(201, app.workspaces.chat_message(wid, data))
                        if action == "attach":
                            return self.reply(200, app.workspaces.attach(wid, data))
                        if action == "watchdog":
                            return self.reply(201, app.workspaces.start_watchdog(wid))
                        if action == "stop":
                            return self.reply(200, app.workspaces.stop_workspace(wid))
                        if action == "delete":
                            return self.reply(200, app.workspaces.delete_workspace(wid, data))
            if re.fullmatch(r"/api/agents/[a-f0-9]{16}", path) and method == "DELETE":
                return self.reply(200, app.workspaces.delete_agent(path.rsplit("/", 1)[1]))
            if path.startswith("/api/agents/") and method == "POST":
                parts = path.split("/")
                if len(parts) == 5:
                    aid, action = parts[3:]
                    data = self.body()
                    if action == "resume":
                        return self.reply(201, app.workspaces.resume(aid, data))
                    if action == "extend":
                        return self.reply(200, app.workspaces.extend(aid, data))
                    if action == "control":
                        return self.reply(200, app.workspaces.commander.configure(
                            aid, data.get("control"), require_text(data, "reason", 1000, "用户设置")))
                    if action == "stop":
                        return self.reply(200, app.workspaces.stop_agent(aid))
                    if action == "messages":
                        return self.reply(200, app.workspaces.message_from_web(aid, data))
            if path == "/api/projects":
                if method == "GET":
                    return self.reply(200, app.projects())
                if method == "POST":
                    return self.reply(201, app.create_project(self.body(), session_hash=session["token_hash"]))
            if path == "/api/staged-attachments" and method == "POST":
                return self.reply(201, app.stage_attachment(session["token_hash"], q("name"), self.body(raw=True)))
            if re.fullmatch(r"/api/staged-attachments/[0-9a-f]{32}", path) and method == "DELETE":
                with app.config_lock():
                    staged = app.staged_file(session["token_hash"], path.rsplit("/", 1)[1])
                    shutil.rmtree(staged.parent)
                return self.reply(200, {"ok": True})
            if path == "/api/extensions/skill-metadata" and method == "GET":
                return self.reply(200, app.workspaces.extensions.preview_skill(q("path")))
            if path == "/api/extensions":
                if method == "GET":
                    return self.reply(200, app.workspaces.extensions.view())
                if method == "POST":
                    return self.reply(200, app.workspaces.extensions.update(None, self.body()))
            if path == "/api/profiles":
                if method == "GET":
                    return self.reply(200, app.profiles())
                if method == "POST":
                    return self.reply(200, app.save_profile(self.body()))
            if path == "/api/role-models":
                if method == "GET":
                    return self.reply(200, app.role_models())
                if method == "POST":
                    return self.reply(200, app.save_role_models(self.body()))
            if path in ("/api/profiles/models", "/api/profiles/test") and method == "POST":
                return self.reply(200, app.profile_probe(self.body(), path.rsplit("/", 1)[1]))
            if path == "/api/profiles/bridges" and method == "GET":
                return self.reply(200, sapper_models.bridge_operation(app.state))
            if re.fullmatch(r"/api/profiles/bridges/[a-f0-9]{24}/stop", path) and method == "POST":
                return self.reply(200, sapper_models.bridge_operation(app.state, path.split("/")[-2]))
            if path.startswith("/api/profiles/") and method == "DELETE":
                app.delete_profile(path.rsplit("/", 1)[1])
                return self.reply(200, {"ok": True})
            if path == "/api/panes" and method == "GET":
                return self.reply(200, app.panes())
            if path.startswith("/api/panes/"):
                parts = path.split("/")
                pane_id = urllib.parse.unquote(parts[3])
                if len(parts) == 4 and method == "GET":
                    return self.reply(200, app.capture(pane_id))
                if len(parts) == 5 and parts[4] == "send" and method == "POST":
                    return self.reply(200, app.send(pane_id, self.body()))
                if len(parts) == 5 and parts[4] in ("stream", "input", "resize"):
                    from sapper_terminal import TerminalBridge
                    terminal = TerminalBridge(app)
                    if parts[4] == "stream" and method == "GET":
                        return terminal.stream(self, pane_id, session, q("cursor"))
                    if parts[4] == "input" and method == "POST":
                        return self.reply(200, terminal.input(pane_id, self.body()))
                    if parts[4] == "resize" and method == "POST":
                        return self.reply(200, terminal.resize(pane_id, self.body()))
            if path == "/api/runs" and method == "GET":
                return self.reply(200, app.runs(q("project") or None, reconcile=False))
            if path.startswith("/api/runs/"):
                parts = path.split("/")
                rid = parts[3]
                if len(parts) == 5 and parts[4] == "extend" and method == "POST":
                    return self.reply(200, app.extend(rid, self.body()))
                if len(parts) == 5 and parts[4] == "stop" and method == "POST":
                    self.body()
                    return self.reply(200, app.stop(rid))
                if len(parts) == 5 and parts[4] == "log" and method == "GET":
                    if q("download") == "1":
                        app.run_info(rid)
                        log = app.run_path(rid) / "output.log"
                        if not log.exists():
                            raise WebError("尚未采集到日志", 404)
                        if log.stat().st_size > MAX_BODY:
                            raise WebError("日志超过 16 MiB，请从服务端私有日志目录获取", 413)
                        text = app.redact(ANSI.sub("", log.read_text(errors="replace")))
                        return self.reply(200, text, "text/plain; charset=utf-8", headers={
                            "Content-Disposition": f'attachment; filename="sapper-{rid}.log"'})
                    return self.reply(200, app.log(rid, int(q("offset", "0"))))
                if len(parts) == 5 and parts[4] == "turns" and method == "GET":
                    after_seq = query_number(q("after_seq"), "after_seq", 0, 0, 2 ** 31 - 1)
                    limit = query_number(q("limit"), "limit", None, 1, 1000)
                    return self.reply(200, app.turn_events(rid, after_seq, limit))
                if len(parts) == 5 and parts[4] == "attachment" and method == "GET":
                    path = q("path", "")
                    start_line = query_number(q("start_line", "1"), "start_line", 1, 1, 10 ** 9)
                    limit = query_number(q("limit"), "limit", None, 1, ATTACHMENT_MAX_LINES)
                    return self.reply(200, app.attachment_range(rid, path, start_line, limit))
            if path == "/api/knowledge" and method == "GET":
                kb = KnowledgeBase.default_root()
                knowledge = KnowledgeBase(kb)
                return self.reply(200, knowledge.search(q("q"), limit=20) if q("q") else knowledge.list_entries())
            if path.startswith("/api/knowledge/") and method == "GET":
                entry = KnowledgeBase(KnowledgeBase.default_root()).get(path.rsplit("/", 1)[1])
                headers = {"Content-Disposition": f'attachment; filename="{entry["id"]}.json"'} if q("download") == "1" else None
                return self.reply(200, entry, headers=headers)
            if path.startswith("/api/projects/"):
                parts = path.split("/")
                if len(parts) < 5:
                    raise WebError("接口不存在", 404)
                pid, action = parts[3], parts[4]
                if method == "GET":
                    if action == "recall":
                        store = app.store(pid)
                        store.require()
                        with store.locked():
                            matches = sapper.recall(store, q("q"), limit=min(50, max(1, int(q("limit", "10")))))
                        return self.reply(200, matches)
                    if action == "validate":
                        store = app.store(pid)
                        store.require()
                        with store.locked():
                            issues = sapper.validate_graph(store)
                        return self.reply(200, {"valid": not issues, "issues": issues})
                    if action == "frontier":
                        store = app.store(pid)
                        store.require()
                        with store.locked():
                            ready = sapper.frontier(store)
                        return self.reply(200, ready)
                    if action == "graph":
                        return self.reply(200, app.graph(pid))
                    if action == "graph-view":
                        store = app.store(pid)
                        with store.locked():
                            html = sapper._render_static_html(store)
                        return self.reply(200, html, "text/html; charset=utf-8")
                    if action == "files":
                        return self.reply(200, app.files(pid, q("path", ".")))
                    if action == "file":
                        file = app.file(pid, q("path"))
                        if not file.is_file():
                            raise WebError("文件不存在", 404)
                        if file.stat().st_size > MAX_BODY:
                            raise WebError("文件超过 16 MiB，请通过原终端处理", 413)
                        data = file.read_bytes()
                        if q("download") == "1":
                            return self.reply(200, data, "application/octet-stream", headers={
                                "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(file.name)})
                        return self.reply(200, {"path": q("path"), "text": app.redact(data.decode("utf-8", errors="replace"))})
                elif method == "POST":
                    if action == "upload":
                        return self.reply(201, app.upload(pid, q("name"), self.body(raw=True)))
                    if action == "run":
                        return self.reply(201, app.start(pid, self.body()))
                    if action in ("hint", "fact", "intent", "confirm", "doc", "claim", "release", "done", "complete", "brainstorm-pick"):
                        return self.reply(200, app.graph_action(pid, action, self.body()))
            raise WebError("接口不存在", 404)
        except (AuthError, WebError, WorkspaceError, NativeError, ExtensionError) as exc:
            self.reply(exc.status, {"error": str(exc)})
        except (sapper.FGError, KBError, ValueError, TypeError, KeyError) as exc:
            self.reply(400, {"error": app.redact(str(exc))})
        except (OSError, subprocess.SubprocessError) as exc:
            self.reply(500, {"error": app.redact(str(exc))})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def expired_cookie(self):
        cookie = f"sapper_web=; HttpOnly; SameSite=Strict; Path={self.server.prefix or '/'}; Max-Age=0"
        return cookie + ("; Secure" if self.server.secure_cookie else "")


def warn_root_startup():
    if os.geteuid() == 0:
        print("警告：Sapper Web 使用 root 权限。Claude CLI 会拒绝 --dangerously-skip-permissions。"
              "请关闭「跳过逐次审批」，或使用普通用户启动 Sapper Web。", file=sys.stderr, flush=True)


def warn_claude_startup(binary):
    try:
        version = subprocess.run([binary, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, errors="replace", timeout=5)
    except FileNotFoundError:
        reason = "找不到可执行文件"
    except subprocess.TimeoutExpired:
        reason = "--version 执行超时（5 秒）"
    except OSError as exc:
        reason = f"无法执行（{exc.strerror}）"
    else:
        if version.returncode == 0 and re.search(r"\b\d+\.\d+\.\d+\b", version.stdout):
            return
        reason = f"--version 退出码 {version.returncode}" if version.returncode else "未返回有效版本号"
    print(f"{CLAUDE_STARTUP_WARNING}检测失败（{binary!r}）：{reason}。"
          "请确认运行 Sapper Web 的用户已安装 Claude Code，且可通过服务的 PATH 找到，"
          "或用 SAPPER_CLAUDE_BIN 指定可执行文件的绝对路径。", file=sys.stderr, flush=True)


def main(argv=None):
    if argv is None and len(sys.argv) >= 3 and sys.argv[1] == "_runner":
        return run_child(sys.argv[2])
    parser = argparse.ArgumentParser(description="在网页操作现有 sapper，不替换 agent 引擎")
    parser.add_argument("--root", type=Path, default=REPO / "runs", help="受管任务根目录（自动发现四层内的 .sapper）")
    parser.add_argument("--state", type=Path, default=REPO / ".sapper-web", help="Web 私有配置和日志目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--public-url", default="", help="反向代理公开地址，用于 Host 校验和安全 cookie")
    parser.add_argument("--url-prefix", default="", help="例如 /sapper（反向代理保留此前缀）")
    parser.add_argument("--workspace-root", action="append", type=Path, default=[], help="目录浏览快捷入口（可选，可重复指定；也可直接在网页选择目录）")
    parser.add_argument("--claude-bin", default=os.environ.get("SAPPER_CLAUDE_BIN", "claude"), help="原 Claude Code 可执行文件")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--init-admin", action="store_true", help="在本机终端初始化 Web 管理员")
    credentials.add_argument("--reset-password", action="store_true", help="在本机终端重置 Web 管理员密码并撤销旧会话")
    parser.add_argument("--username", help="初始化或重置密码的管理员用户名")
    args = parser.parse_args(argv)
    if args.init_admin or args.reset_password:
        try:
            manage_credentials(args.state.expanduser().resolve(), "auth-init" if args.init_admin else "password-reset", args.username)
        except (AuthError, OSError, EOFError) as exc:
            parser.exit(1, f"{exc}\n")
        return 0
    if args.username:
        parser.error("--username 需与 --init-admin 或 --reset-password 一起使用")
    if args.url_prefix and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", args.url_prefix):
        parser.error("--url-prefix 必须是 /sapper 形式的路径")
    if args.public_url:
        parsed = urllib.parse.urlsplit(args.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            parser.error("--public-url 必须是 HTTP(S) 地址")
    warn_root_startup()
    app = Console(args.root, args.state, workspace_roots=args.workspace_root, claude_bin=args.claude_bin)
    try:
        server = WebServer((args.host, args.port), app, args.public_url, args.url_prefix)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"无法监听 {args.host}:{args.port}：{exc}", file=sys.stderr)
        return PORT_IN_USE_EXIT
    warn_claude_startup(app.claude_bin)
    print(f"Sapper Web: http://{args.host}:{server.server_port}{args.url_prefix}/", flush=True)
    print(f"任务目录: {app.root}", flush=True)
    print(f"认证数据库: {app.auth.path}", flush=True)
    if not app.auth.configured():
        print("请先在本机初始化管理员：" + shlex.join([sys.executable, str(Path(__file__).resolve()), "--state", str(app.state), "--init-admin"]), flush=True)
    print("原 agent 进程独立运行；Web 服务重启不会停止已拉起的无头进程组。", flush=True)
    # Frozen spec v1.0 四: a control-plane daemon sweeps confirmation deadlines
    # so escalation happens even when nobody is polling the web UI.
    import threading as _threading
    def _route_tick():
        while True:
            try:
                route_due_events(app.state)
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(5)
    _threading.Thread(target=_route_tick, name="sapper-routing", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
