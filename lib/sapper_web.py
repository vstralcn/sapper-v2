#!/usr/bin/env python3
"""A local Web control surface for the existing sapper CLI and tmux sessions.

Standard library only. Graphs remain in .sapper; the original agent is unchanged.
Run: python3 lib/sapper_web.py --root runs
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
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
import sapper_context
import sapper_evidence
from kb import KnowledgeBase, KBError
from sapper_workspace import Workspaces, WorkspaceError, ROLES
from sapper_native import NativeSurface, NativeError
from sapper_extensions import ExtensionError, clean_environment, api_environment
import sapper_models

REPO = Path(__file__).resolve().parent.parent
SAPPER = REPO / "lib" / "sapper.py"
STATIC = REPO / "web"
MAX_BODY = 16 * 1024 * 1024
FINISHED = {"exited", "stopped", "failed", "interrupted", "timed_out"}
INTERRUPT_GRACE = 10
TERMINATE_GRACE = 3
MAX_RUN_SECONDS = 1440 * 60
KEYS = {"Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right",
        "C-c", "C-d", "C-u", "C-l", "BSpace", "Space"}
HIDDEN = {".git", ".claude", ".codex", ".sapper-web", "node_modules", "__pycache__",
          ".venv", "pane-env", ".sapper-agents"}
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
    def __init__(self, root: Path, state: Path, tmux_socket=None, *, workspace_roots=None, claude_bin=None):
        self.root = root.resolve()
        self.state = state.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        self.tmux_socket = tmux_socket
        self._workspace_roots = list(dict.fromkeys([self.root] + [Path(p).expanduser().resolve() for p in (workspace_roots or [])]))
        self.claude_bin = claude_bin or os.environ.get("SAPPER_CLAUDE_BIN", "claude")
        self.config_path = self.state / "config.json"
        self.context_path = self.state / "global-context.json"
        self.lock = threading.RLock()
        self.sessions = {}
        self.login_failures = {}
        self.token_path = self.state / "token"
        if not self.token_path.exists():
            fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
        os.chmod(self.token_path, 0o600)
        self.token = self.token_path.read_text().strip()
        if len(self.token) < 20:
            raise WebError("登录 token 至少需要 20 个字符，请更换 token 文件")
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
        agents = [a for a in self.workspaces.agents() if not a["id"].startswith("legacy-")]
        runs = self.runs()
        context = self.global_context()
        for project in projects:
            project["active_agents"] = sum(bool(a.get("pane_id")) for a in agents
                                           if project["id"] in a.get("workspace_scope", [a["workspace_id"]]))
        recent = [{k: r.get(k) for k in ("id", "project_id", "kind", "native_operation", "role", "profile_name", "status", "exit_code", "created_at")}
                  for r in runs[:8]]
        return {"projects": projects, "agents": agents, "recent_runs": recent,
                "summary": {"workspaces": len(projects), "active_agents": sum(bool(a.get("pane_id")) for a in agents),
                            "open_intents": sum(p["open_intents"] for p in projects),
                            "facts": sum(p["facts"] for p in projects)},
                "context": {k: context[k] for k in ("revision", "enabled", "prompt_enabled", "hook_enabled", "updated_at")},
                "environment": {"system": "macOS" if platform.system() == "Darwin" else platform.system(), "architecture": platform.machine(),
                                "python": platform.python_version(), "tmux": shutil.which("tmux") is not None,
                                "claude": self.workspaces.capabilities(), "workspace_roots": [str(p) for p in self.workspace_roots]}}

    def redact(self, value):
        text = str(value)
        keys = [p.get("api_key", "") for p in self.config()["profiles"]]
        for path in (self.state / "model-bridges").glob("*/ready.json"):
            with contextlib.suppress(OSError, ValueError):
                keys.append(json.loads(path.read_text()).get("token", ""))
        keys += [os.environ.get(k, "") for k in (
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "SAPPER_CLAUDE_API_KEY", "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY", "BENCHMARK_TOKEN")]
        keys.extend(self.workspaces.extensions.credential_values())
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
                    facts, intents = store.list_facts(), store.list_intents()
                rel = str(here.relative_to(self.root))
                found.append({"id": hashlib.sha256(rel.encode()).hexdigest()[:16],
                              "name": here.name, "path": str(here), "relative_path": rel,
                              "goal": project["goal"], "status": project["status"],
                              "created_at": project.get("created_at"),
                              "facts": sum(f["id"] != "goal" for f in facts),
                              "open_intents": sum(sapper.is_open(i) for i in intents)})
            except (WebError, sapper.FGError, OSError):
                continue
            if len(found) >= 200:
                break
        return sorted(found, key=lambda p: p.get("created_at") or "", reverse=True)

    def projects(self):
        found = {p["path"]: {**p, "enabled": True, "mode": "legacy"} for p in self._scan_projects()}
        for bound in self.workspaces.bound_projects():
            found[bound["path"]] = bound
        return sorted(found.values(), key=lambda p: p.get("created_at") or "", reverse=True)

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

    def create_project(self, data):
        name = require_text(data, "name", 64)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise WebError("目录名请使用英文字母、数字、点、横线或下划线")
        goal = require_text(data, "goal")
        path = self.inside(self.root / name)
        if path.exists():
            raise WebError("目录已存在；已有图会自动出现在任务列表，请勿重复初始化", 409)
        path.mkdir()
        store = sapper.Store(path / ".sapper")
        store.init("web", goal)
        if data.get("context"):
            store.add_hint(require_text(data, "context"), "web-user")
        self.audit("project.created", name=name)
        return next(p for p in self.projects() if p["path"] == str(path))

    def graph(self, pid):
        store = self.store(pid)
        if not store.exists():
            project = self.project(pid)
            return {"project": {"id": "proj", "goal": project["goal"], "status": "not_enabled"},
                    "facts": [], "intents": [], "hints": [], "open_intents": [], "done_intents": [], "fact_ids": []}
        with store.locked():
            return sapper.build_graph_view(store)

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
            result = store.add_intent(data.get("from", []), require_text(data, "description"),
                                      "web-user", requires_confirmation=bool(data.get("requires_confirmation")),
                                      title=data.get("title"), doc=data.get("doc"))
        elif action == "confirm":
            result = store.confirm_intent(require_text(data, "id", 64))
        elif action == "claim":
            result = store.claim_intent(require_text(data, "id", 64), require_text(data, "worker", 100))
        elif action == "release":
            result = store.release_intent(require_text(data, "id", 64), require_text(data, "worker", 100))
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
                "revision": cfg.get("role_profiles_revision", 0),
                "roles": [{"id": role, "label": label} for role, label in ROLES.items()]}

    def save_role_models(self, data, pid=None):
        if pid is not None:
            self.project(pid)
        assignments = data.get("assignments")
        roles = ROLES
        if not isinstance(assignments, dict) or any(role not in roles for role in assignments):
            raise WebError("角色模型配置格式不正确")
        if any(not isinstance(value, str) for value in assignments.values()):
            raise WebError("请选择已保存的模型连接")
        with self.config_lock():
            cfg = self.config()
            revision = cfg.get("role_profiles_revision", 0)
            if type(data.get("expected_revision")) is not int or data["expected_revision"] != revision:
                raise WebError("角色模型已被其他页面更新，请重新读取后再保存；当前选择仍保留", 409)
            profiles = {p["id"]: p for p in cfg["profiles"]}
            for value in assignments.values():
                if value and value not in profiles:
                    raise WebError("模型连接已不存在，请重新选择", 409)
                if value and profiles[value].get("engine_prefix"):
                    raise WebError("角色默认模型不能使用 CLI 包装器，请选择普通模型连接")
            # Missing entries follow the original inheritance rule. Empty entries
            # explicitly use the environment, even for children of another model.
            if pid is None:
                cfg["role_profiles"] = assignments
            else:
                cfg.setdefault("workspace_role_profiles", {})[pid] = assignments
            cfg["role_profiles_revision"] = revision + 1
            private_json(self.config_path, cfg)
        self.audit("role_models.saved", workspace=pid, revision=revision + 1, assignments=assignments)
        return self.role_models(pid)

    def role_profile_id(self, role, data, fallback="", *, workspace_id=None):
        cfg = self.config()
        defaults = {**cfg.get("role_profiles", {}), **cfg.get("workspace_role_profiles", {}).get(workspace_id, {})}
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
            used = [label for role, label in ROLES.items()
                    if cfg.get("role_profiles", {}).get(role) == pid or any(
                        assignments.get(role) == pid for assignments in cfg.get("workspace_role_profiles", {}).values())]
            if used:
                raise WebError("该连接用于角色默认模型：" + "、".join(used) + "；请先在角色默认模型中重新分配", 409)
            cfg["profiles"] = [p for p in cfg["profiles"] if p["id"] != pid]
            private_json(self.config_path, cfg)
        self.audit("profile.deleted", profile=pid)

    def tmux(self, *args, timeout=10):
        prefix = ["tmux"] + (["-L", self.tmux_socket] if self.tmux_socket else [])
        try:
            return subprocess.run(prefix + list(args), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise WebError("未安装 tmux，请先安装后使用运行与终端功能", 503) from exc
        except subprocess.TimeoutExpired as exc:
            raise WebError("tmux 响应超时", 503) from exc

    def panes(self):
        if not shutil.which("tmux"):
            return []
        fmt = "#{pane_id}\t#{session_name}\t#{window_name}\t#{pane_current_path}\t#{pane_dead}\t#{pane_current_command}"
        proc = self.tmux("list-panes", "-a", "-F", fmt)
        if proc.returncode:
            return []
        projects = sorted(self.projects(), key=lambda p: len(p["path"]), reverse=True)
        managed = {r["session"]: r["project_id"] for f in (self.state / "runs").glob("*/run.json")
                   if (r := read_json(f, {})).get("session")}
        result = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 6:
                continue
            pane, session, name, cwd, dead, command = parts
            path = Path(cwd).resolve()
            linked_store = None
            for parent in (path, *path.parents):
                if not any(parent.is_relative_to(r) for r in self.workspace_roots):
                    break
                if (parent / ".sapper" / "project.json").is_file():
                    candidate = (parent / ".sapper").resolve()
                    if any(candidate.is_relative_to(r) for r in self.workspace_roots):
                        linked_store = candidate
                    break
            for p in projects:
                if (path.is_relative_to(Path(p["path"])) or managed.get(session) == p["id"]
                        or linked_store == (Path(p["path"]) / ".sapper").resolve()):
                    result.append({"id": pane, "session": session, "name": name, "path": cwd,
                                   "dead": dead == "1", "command": command, "project_id": p["id"]})
                    break
            else:
                if session == "sapper-cmd" and name == "commander" and path.is_relative_to(self.root):
                    result.append({"id": pane, "session": session, "name": name, "path": cwd,
                                   "dead": dead == "1", "command": command, "project_id": None,
                                   "role": "commander"})
        return result

    def pane(self, pane_id):
        if not re.fullmatch(r"%[0-9]+", pane_id):
            raise WebError("终端 ID 不正确")
        for pane in self.panes():
            if pane["id"] == pane_id:
                return pane
        raise WebError("终端不存在或已离开受管任务目录", 404)

    def capture(self, pane_id):
        self.pane(pane_id)
        proc = self.tmux("capture-pane", "-p", "-J", "-t", pane_id, "-S", "-1000")
        if proc.returncode:
            raise WebError("终端已结束，请查看运行日志", 404)
        return {"text": self.redact(proc.stdout), "captured_at": sapper.now_iso()}

    def send(self, pane_id, data):
        pane = self.pane(pane_id)
        if data.get("key") in ("C-c", "Escape"):
            self.workspaces.pause_commander_input(pane)
        if data.get("key"):
            key = data["key"]
            if key not in KEYS:
                raise WebError("不支持的按键")
            proc = self.tmux("send-keys", "-t", pane_id, key)
        else:
            message = require_text(data, "text")
            proc = self.tmux("send-keys", "-t", pane_id, "-l", "--", message)
            if proc.returncode == 0 and data.get("submit", True):
                # Same literal-input + separate Enter mechanism as worker.sh.
                time.sleep(0.1)
                proc = self.tmux("send-keys", "-t", pane_id, "Enter")
        if proc.returncode:
            raise WebError("发送失败，终端可能已结束", 409)
        self.audit("terminal.sent", pane=pane_id, project=pane["project_id"], key=data.get("key"))
        return {"status": "sent", "message": "已送入终端；是否采用请查看原 agent 回应"}

    def run_path(self, rid):
        if not re.fullmatch(r"[a-f0-9]{16}", rid):
            raise WebError("运行 ID 不正确")
        return self.state / "runs" / rid

    def run_info(self, rid):
        directory = self.run_path(rid)
        info = read_json(directory / "run.json")
        if not info:
            raise WebError("运行不存在", 404)
        control = read_json(directory / "control.json", {})
        if "timeout_seconds" in control:
            info["timeout_seconds"] = control["timeout_seconds"]
        info["activity"] = json.loads(self.redact(json.dumps(read_json(directory / "activity.json", {}), ensure_ascii=False)))
        if info.get("timeout_seconds") is not None and info.get("started_epoch") is not None:
            info["deadline_epoch"] = info["started_epoch"] + info["timeout_seconds"]
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

    def runs(self, pid=None):
        result = []
        for path in (self.state / "runs").glob("*/run.json"):
            info = read_json(path)
            if pid and info.get("project_id") != pid:
                continue
            if info["status"] not in FINISHED:
                alive = self.tmux("has-session", "-t", info["session"]).returncode == 0
                if not alive:
                    # Do not overwrite a concurrent final result written by the runner.
                    with run_lock(path.parent):
                        current = read_json(path)
                        if current["status"] not in FINISHED:
                            current.update(status=current.get("stop_reason", "interrupted"), ended_at=sapper.now_iso())
                            private_json(path, current)
            info = self.run_info(info["id"])
            result.append(json.loads(self.redact(json.dumps(info, ensure_ascii=False))))
        return sorted(result, key=lambda r: r["created_at"], reverse=True)[:100]

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
        env.update(PATH=str(self.state / "bin") + os.pathsep + os.environ.get("PATH", ""),
                   PYTHONUNBUFFERED="1", SAPPER_ROOT=str(self.root),
                   SAPPER_GLOBAL_CONTEXT_FILE=str(self.context_path), SAPPER_CONTEXT_PROMPT_REVISION="")
        env["SAPPER_TMUX_SOCKET"] = self.tmux_socket or ""
        if "SAPPER_KB_ROOT" in env:
            env["SAPPER_KB_ROOT"] = str(KnowledgeBase.default_root().resolve())
        if profile and sapper_models.protocol(profile) != "anthropic":
            env["SAPPER_BIN"] = shlex.quote(self.claude_bin)
        sapper_models.apply_profile(env, profile, self.state)
        if (native or {}).get("isolated_extensions") and not (env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("SAPPER_CLAUDE_API_KEY")):
            raise WebError("独立会话仅支持 API Key：请在模型配置保存密钥，或为 Web 服务设置 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN；不使用 Claude 订阅登录", 409)
        argv = [sys.executable, str(SAPPER), "--store", str(Path(project["path"]) / ".sapper")]
        if native is not None:
            argv = native["argv"]
            env.update(native.get("env", {}))
            missing = [name for name in native.get("mcp_env_refs", []) if not env.get(name)]
            if missing:
                raise WebError("MCP 所需环境变量未配置：" + ", ".join(missing), 409)
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
                info.update({k: native[k] for k in ("agent_id", "claude_session_id", "intent_id", "role", "timeout_seconds", "native_operation", "execution_mode", "output_format") if k in native})
            info["supervisor_version"] = 1
            private_json(run_dir / "run.json", info)
            private_json(run_dir / "launch.json", {"argv": argv, "env": env})
            wrapper = shlex.join([sys.executable, str(Path(__file__).resolve()), "_runner", str(run_dir)])
            try:
                proc = self.tmux("new-session", "-d", "-s", session, "-c", cwd, wrapper)
                if proc.returncode:
                    raise WebError("无法创建 tmux 会话: " + proc.stderr.strip(), 503)
                log = run_dir / "output.log"
                log.touch(mode=0o600)
                pipe = "exec cat >> " + shlex.quote(str(log))
                proc = self.tmux("pipe-pane", "-t", session + ":0.0", pipe)
                if proc.returncode:
                    raise WebError("无法启用输出采集: " + proc.stderr.strip(), 503)
                (run_dir / "ready").touch()
            except WebError:
                self.tmux("kill-session", "-t", session)
                info.update(status="failed", ended_at=sapper.now_iso())
                private_json(run_dir / "run.json", info)
                raise
        self.audit("run.started", project=pid, run=rid, kind=kind)
        return info

    def stop(self, rid):
        with self.workspaces.lifecycle():
            return self._stop(rid)

    def _stop(self, rid):
        info = self.run_info(rid)
        if info.get("agent_id"):
            self.workspaces.mark_stopped(info["agent_id"])
        if info["status"] in FINISHED:
            return info
        run_dir = self.run_path(rid)
        with run_lock(run_dir):
            (run_dir / "stop_requested").touch()
        if info.get("supervisor_version") == 1 and info.get("execution_mode") == "print":
            # Let the owner interrupt Claude and drain its final output first.
            end = time.monotonic() + INTERRUPT_GRACE + TERMINATE_GRACE + 4
            while time.monotonic() < end:
                current = self.run_info(rid)
                if current["status"] in FINISHED:
                    # Do not let an immediate resume rediscover the exiting pane.
                    for _ in range(20):
                        if self.tmux("has-session", "-t", info["session"]).returncode:
                            break
                        time.sleep(0.05)
                    self.audit("run.stopped", run=rid)
                    return current
                time.sleep(0.1)
            # A broken wrapper still must not orphan its detached print group.
            child_group = self.run_info(rid).get("child_process_group")
            if child_group and child_group != os.getpgrp():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child_group, signal.SIGKILL)
        group = info.get("process_group")
        wrapper = info.get("wrapper_pid")
        if group and group == wrapper and group != os.getpgrp():
            # A managed tmux pane is a separate process group. Include its tools.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGTERM)
            time.sleep(0.2)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        # Only the session this service created is affected. No global tmux kill.
        self.tmux("kill-session", "-t", info["session"])
        info = self.run_info(rid)
        info.update(status="stopped", ended_at=sapper.now_iso())
        private_json(run_dir / "run.json", info)
        self.audit("run.stopped", run=rid)
        return info

    def log(self, rid, offset=0):
        info = self.run_info(rid)
        directory = self.run_path(rid)
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

    def upload(self, pid, name, body):
        name = Path(name.replace("\\", "/")).name
        if not name or name in (".", "..") or len(name) > 180 or "\x00" in name:
            raise WebError("文件名不正确")
        relative = "attachments/" + secrets.token_hex(4) + "_" + name
        path = self.file(pid, relative)
        path.parent.mkdir(exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        self.store(pid).add_hint(f"用户上传附件：{relative}（{len(body)} bytes）；请按任务需要读取。", "web-user")
        self.audit("attachment.saved", project=pid, path=relative, size=len(body))
        return {"path": relative, "size": len(body), "message": "文件已保存并记入 hint，未自动发送到其他会话"}


def run_child(directory):
    """Supervise the original command in tmux; only print runs use stream pipes."""
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
    for key in ("TERM", "TMUX", "TMUX_PANE"):
        if key in os.environ:
            env[key] = os.environ[key]
    child = None
    stop_signal = None
    reader = None
    output = None
    managed_print = info.get("execution_mode") == "print"
    if info.get("output_format") == "stream-json":
        from sapper_run import RunOutput
        output = RunOutput(directory, private_json)

    def note(message):
        print(message, flush=True)
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
            for line in child.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                output.observe(line)
        finally:
            child.stdout.close()

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    rc = 1
    reason = None
    try:
        options = {"start_new_session": True, "stdin": subprocess.DEVNULL} if managed_print else {}
        if output:
            options.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        child = subprocess.Popen(launch["argv"], cwd=info["cwd"], env=env, **options)
        started = time.monotonic()
        info.update(status="running", started_at=sapper.now_iso(), pid=child.pid,
                    started_epoch=time.time(),
                    wrapper_pid=os.getpid(), process_group=os.getpgrp())
        if managed_print:
            info["child_process_group"] = child.pid
        with run_lock(directory):
            private_json(directory / "run.json", info)
        note("[sapper-web] 原命令已启动，输出从此处开始采集。")
        if output:
            reader = threading.Thread(target=relay, daemon=True)
            reader.start()
        phase_end = None
        stage = None
        warned_budget = None
        while child.poll() is None:
            now = time.monotonic()
            with run_lock(directory):
                control = read_json(directory / "control.json", {})
                budget = control.get("timeout_seconds", info.get("timeout_seconds"))
                if reason is None:
                    if stop_signal or (directory / "stop_requested").exists():
                        reason = "stopped"
                    elif budget is not None and now - started >= budget:
                        reason = "timed_out"
                    if reason:
                        info.update(status="stopping", stop_reason=reason, stopping_at=sapper.now_iso())
                        private_json(directory / "run.json", info)
            if reason and stage is None:
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
        with run_lock(directory):
            # Explicit cancellation wins a race with an otherwise normal exit.
            reason = reason or ("stopped" if stop_signal or (directory / "stop_requested").exists() else None)
            info.update(status=reason or "exited", ended_at=sapper.now_iso(), exit_code=rc)
            if reason:
                info["stop_reason"] = reason
            private_json(directory / "run.json", info)
        # Credential-bearing launch data is no longer needed after this command.
        with contextlib.suppress(FileNotFoundError):
            (directory / "launch.json").unlink()
    return rc


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

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
        session = self.server.console.sessions.get(item.value)
        if session and session["expires"] > time.time():
            return session
        return None

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
                            "/agent-topology.js", "/agent-topology-model.js", "/agent-topology.css", "/models.js", "/models.css", "/settings.css",
                            "/vendor/xterm.js", "/vendor/xterm.css", "/vendor/addon-fit.js"}
            if path in static_names and method == "GET":
                filename = "index.html" if path == "/" else path[1:]
                data = (STATIC / filename).read_bytes()
                return self.reply(200, data, mimetypes.guess_type(filename)[0] + "; charset=utf-8")
            if path == "/api/session" and method == "POST":
                body = self.body()
                ip = self.client_address[0]
                now = time.time()
                attempts = [t for t in app.login_failures.get(ip, []) if now - t < 60]
                if len(attempts) >= 10:
                    raise WebError("登录尝试过多，请一分钟后重试", 429)
                if not isinstance(body.get("token"), str) or not hmac.compare_digest(body["token"], app.token):
                    app.login_failures[ip] = attempts + [now]
                    raise WebError("访问口令不正确", 401)
                sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                app.sessions[sid] = {"csrf": csrf, "expires": now + 43200}
                cookie = f"sapper_web={sid}; HttpOnly; SameSite=Strict; Path={prefix or '/'}; Max-Age=43200"
                if self.server.secure_cookie:
                    cookie += "; Secure"
                return self.reply(200, {"authenticated": True, "csrf": csrf}, headers={"Set-Cookie": cookie})
            session = self.session()
            if not session:
                raise WebError("请先输入 Web 访问口令登录", 401)
            if method != "GET" and not hmac.compare_digest(self.headers.get("X-Sapper-CSRF", ""), session["csrf"]):
                raise WebError("请求校验失败，请刷新后重试", 403)
            if path == "/api/session" and method == "GET":
                return self.reply(200, {"authenticated": True, "csrf": session["csrf"]})
            if path == "/api/logout" and method == "POST":
                session["expires"] = 0
                return self.reply(200, {"ok": True}, headers={"Set-Cookie": f"sapper_web=; Path={prefix or '/'}; Max-Age=0"})
            if path == "/api/health" and method == "GET":
                return self.reply(200, {"root": str(app.root), "state": str(app.state),
                                       "tmux": bool(shutil.which("tmux")), "claude": bool(shutil.which("claude")),
                                       "sapper": str(SAPPER), "worker_script": str(REPO / "docker/commander/worker.sh"),
                                       "workspace_roots": [str(p) for p in app.workspace_roots]})
            if path == "/api/directories" and method == "GET":
                return self.reply(200, app.workspaces.directories(q("path")))
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
                        if action == "extensions":
                            return self.reply(200, app.workspaces.extensions.view(wid))
                        if action == "inspect":
                            return self.reply(200, app.workspaces.inspect(wid))
                        if action == "agents":
                            return self.reply(200, {"agents": app.workspaces.agents(wid), "services": app.workspaces.services(wid)})
                        if action == "role-prompt":
                            scope = [item for item in q("scope").split(",") if item] or None
                            return self.reply(200, app.workspaces.role_info(wid, q("role") or "worker", scope))
                    elif method == "POST":
                        data = self.body()
                        if action == "role-models":
                            return self.reply(200, app.save_role_models(data, wid))
                        if action == "extensions":
                            return self.reply(200, app.workspaces.extensions.update(wid, data))
                        if action == "enable":
                            return self.reply(200, app.workspaces.enable(wid, data))
                        if action == "settings":
                            return self.reply(200, app.workspaces.save_settings(wid, data))
                        if action == "sessions":
                            return self.reply(201, app.workspaces.create_session(wid, data))
                        if action == "attach":
                            return self.reply(200, app.workspaces.attach(wid, data))
                        if action == "watchdog":
                            return self.reply(201, app.workspaces.start_watchdog(wid))
                        if action == "stop":
                            return self.reply(200, app.workspaces.stop_workspace(wid))
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
                        return self.reply(200, app.workspaces.message(aid, data))
            if path == "/api/projects":
                if method == "GET":
                    return self.reply(200, app.projects())
                if method == "POST":
                    return self.reply(201, app.create_project(self.body()))
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
                return self.reply(200, app.runs(q("project") or None))
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
        except (WebError, WorkspaceError, NativeError, ExtensionError) as exc:
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


def main(argv=None):
    if argv is None and len(sys.argv) >= 3 and sys.argv[1] == "_runner":
        return run_child(sys.argv[2])
    parser = argparse.ArgumentParser(description="在网页操作现有 sapper 和 tmux，不替换 agent 引擎")
    parser.add_argument("--root", type=Path, default=REPO / "runs", help="受管任务根目录（自动发现四层内的 .sapper）")
    parser.add_argument("--state", type=Path, default=REPO / ".sapper-web", help="Web 私有配置和日志目录")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--public-url", default="", help="反向代理公开地址，用于 Host 校验和安全 cookie")
    parser.add_argument("--url-prefix", default="", help="例如 /sapper（反向代理保留此前缀）")
    parser.add_argument("--tmux-socket", default=None, help="可选 tmux -L socket 名称")
    parser.add_argument("--workspace-root", action="append", type=Path, default=[], help="目录浏览快捷入口（可选，可重复指定；也可直接在网页选择目录）")
    parser.add_argument("--claude-bin", default=os.environ.get("SAPPER_CLAUDE_BIN", "claude"), help="原 Claude Code 可执行文件")
    args = parser.parse_args(argv)
    if args.url_prefix and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", args.url_prefix):
        parser.error("--url-prefix 必须是 /sapper 形式的路径")
    if args.public_url:
        parsed = urllib.parse.urlsplit(args.public_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            parser.error("--public-url 必须是 HTTP(S) 地址")
    app = Console(args.root, args.state, args.tmux_socket, workspace_roots=args.workspace_root, claude_bin=args.claude_bin)
    server = WebServer((args.host, args.port), app, args.public_url, args.url_prefix)
    print(f"Sapper Web: http://{args.host}:{server.server_port}{args.url_prefix}/", flush=True)
    print(f"任务目录: {app.root}", flush=True)
    print(f"访问口令保存在: {app.token_path}（使用 cat 在本机查看）", flush=True)
    print("原 agent 进程独立运行；退出此 Web 服务不会停止已有 tmux 会话。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
