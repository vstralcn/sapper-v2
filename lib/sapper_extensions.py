"""Selected MCP/skills and immutable launch bundles for native workspace sessions.

The library is Web-owned; sessions own copies, never links back to mutable skills.
MCP credentials are environment references, not values stored in workspace files.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import urlsplit

import sapper
import sapper_context

REPO = Path(__file__).resolve().parent.parent
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
REQUIRED_FLAGS = ("mcp_config", "strict_mcp_config", "plugin_dir", "setting_sources", "settings")
EMPTY = {"mcp_ids": [], "skill_ids": []}


class ExtensionError(sapper.FGError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def string(value, label, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ExtensionError(f"{label} 必须是非空文本（最多 {limit} 字符）")
    return value


def mcp_config(value):
    if not isinstance(value, dict):
        raise ExtensionError("MCP 配置必须为 JSON 对象")
    kind = value.get("type", "stdio")
    allowed = {"type", "command", "args", "env"} if kind == "stdio" else {"type", "url", "headers"}
    if kind not in ("stdio", "http", "sse") or set(value) - allowed:
        raise ExtensionError("MCP 支持 stdio / http / sse，请检查配置字段")
    result = {"type": kind}
    if kind == "stdio":
        result["command"] = string(value.get("command"), "command")
        args = value.get("args", [])
        if not isinstance(args, list) or len(args) > 100 or any(not isinstance(a, str) or "\x00" in a or len(a) > 4096 for a in args):
            raise ExtensionError("args 必须是最多 100 项的文本数组")
        result["args"] = args
    else:
        url = string(value.get("url"), "url")
        u = urlsplit(url)
        if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ExtensionError("MCP URL 必须是无凭据、查询参数或片段的 HTTP(S) 地址；认证请使用 headers 环境变量引用")
        result["url"] = url
    field = "env" if kind == "stdio" else "headers"
    entries = value.get(field, {})
    if not isinstance(entries, dict) or len(entries) > 100:
        raise ExtensionError(f"{field} 必须是对象，最多 100 项")
    for key, item in entries.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) or not isinstance(item, str):
            raise ExtensionError(f"{field} 名称或值不正确")
        # Allow a header prefix (e.g. Bearer), but never inline a credential.
        if not re.fullmatch(r"(?:Bearer )?\$\{[A-Za-z_][A-Za-z0-9_]*\}", item):
            raise ExtensionError(f"{field}.{key} 请使用 ${{环境变量名}} 引用；Bearer 前缀可用于 headers")
    if entries:
        result[field] = entries
    return result


def skill_files(source, expected_name=None):
    source = Path(source).expanduser().resolve()
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise ExtensionError("请选择含 SKILL.md 的 skill 目录")
    files, size = [], 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ExtensionError("skill 内部含符号链接，请先整理为独立目录后导入")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ExtensionError("skill 只能包含普通文件和目录")
        files.append(path)
        size += path.stat().st_size
        if len(files) > 2000 or size > 32 * 1024 * 1024:
            raise ExtensionError("单个 skill 超过 2000 文件或 32 MiB")
    body = (source / "SKILL.md").read_text(encoding="utf-8")
    parts = body.split("\n---", 1)
    if not body.startswith("---\n") or len(parts) != 2 or not re.search(r"^description:[ \t]*\S", parts[0], re.M):
        raise ExtensionError("SKILL.md 需要 YAML frontmatter 和 description")
    declared = re.search(r"^name:[ \t]*(.+)$", parts[0], re.M)
    if declared and expected_name and declared[1].strip().strip("\"'") != expected_name:
        raise ExtensionError("资源名称必须与 SKILL.md 的 name 一致，避免加载后名称不符")
    return source, files


def hashes(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ExtensionError("会话配置快照不能包含符号链接", 409)
        if path.is_file():
            result[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def workspace_instructions(root):
    """Snapshot workspace memory and standalone @imports without enabling skills.

    Only workspace entry files and their explicit imports are read. Imports inside
    fenced code examples are not instructions. Bounds match the CLI import depth.
    """
    seen, sections = set(), []
    total = 0

    def visit(path, depth=0):
        nonlocal total
        path = path.expanduser().resolve()
        if path in seen:
            return
        if depth > 5:
            raise ExtensionError("工作区说明的 @ 导入超过 5 层，请整理后启动")
        seen.add(path)
        if not path.is_file():
            raise ExtensionError(f"工作区说明导入文件不存在：{path}")
        if path.stat().st_size > 256 * 1024 - total:
            raise ExtensionError("工作区说明及导入合计超过 256 KiB，请精简说明或转为按需 skill")
        content = path.read_text(encoding="utf-8")
        total += len(content.encode())
        sections.append(f"[Workspace instructions: {path}]\n{content}")
        fence = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                fence = None if fence == marker else marker if fence is None else fence
            if fence is None and stripped.startswith("@"):
                imported = Path(stripped[1:].strip()).expanduser()
                visit(imported if imported.is_absolute() else path.parent / imported, depth + 1)

    for path in (root / "CLAUDE.md", root / ".claude/CLAUDE.md", root / "CLAUDE.local.md"):
        if path.is_file():
            visit(path)
    return "\n\n".join(sections)


def clean_environment(env):
    """Do not import an OAuth account or another agent's Claude runtime switches."""
    for key in list(env):
        if key.startswith("CLAUDE_") or key == "CLAUDECODE" or key == "DISABLE_DOCTOR_COMMAND":
            env.pop(key, None)
    # Profiles are applied after this, so the local protocol bridge still works.
    return env


def api_environment(env):
    # Interactive Claude requires explicit approval for an environment API key.
    # Store only its normal last-20-character marker in the private native data
    # directory; the credential itself remains launch-environment-only.
    env.pop("SAPPER_CLAUDE_API_KEY", None)
    key = env.get("ANTHROPIC_API_KEY")
    if not key or env.get("ANTHROPIC_AUTH_TOKEN") or not env.get("CLAUDE_CONFIG_DIR"):
        return env
    directory = Path(env["CLAUDE_CONFIG_DIR"])
    path = directory / ".claude.json"
    if path.is_symlink():
        raise ExtensionError("会话 Claude 配置不能为符号链接", 409)
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ExtensionError("无法读取会话 Claude 配置；请检查原生数据目录", 409) from exc
    responses = config.setdefault("customApiKeyResponses", {"approved": [], "rejected": []})
    if not isinstance(responses, dict) or any(not isinstance(responses.get(k), list) for k in ("approved", "rejected")):
        raise ExtensionError("会话 Claude 密钥批准状态格式不正确", 409)
    marker = key[-20:]
    responses["approved"] = list(dict.fromkeys([*responses["approved"], marker]))
    responses["rejected"] = [item for item in responses["rejected"] if item != marker]
    config["hasCompletedOnboarding"] = True
    sapper_context.atomic_private(path, config)
    return env


class AgentExtensions:
    def __init__(self, manager):
        self.manager = manager
        self.app = manager.app
        self.path = self.app.state / "extensions.json"

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {
            "revision": 0, "mcp": [], "skills": [], "workspaces": {}}

    def view(self, pid=None):
        if pid is not None:
            self.app.project(pid)
        cfg = self.read()
        return {"revision": cfg["revision"], "mcp": cfg["mcp"], "skills": cfg["skills"],
                "selection": cfg["workspaces"].get(pid, EMPTY), "workspaces": cfg["workspaces"],
                "claude": self.manager.capabilities()}

    def select(self, cfg, selection):
        if not isinstance(selection, dict) or set(selection) != set(EMPTY):
            raise ExtensionError("资源选择需要 mcp_ids 和 skill_ids 数组")
        for field, collection in (("mcp_ids", "mcp"), ("skill_ids", "skills")):
            ids = selection[field]
            known = {r["id"] for r in cfg[collection]}
            if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or set(ids) - known:
                raise ExtensionError("选中的 MCP / skill 已不存在或选择格式不正确", 409)
        return {key: list(selection[key]) for key in EMPTY}

    def update(self, pid, data):
        with self.manager.lifecycle():
            if pid is not None:
                self.app.project(pid)
            cfg = self.read()
            if type(data.get("expected_revision")) is not int or data["expected_revision"] != cfg["revision"]:
                raise ExtensionError("资源配置已变化，请重新读取后保存", 409)
            action = data.get("action")
            if action == "defaults":
                if pid is None:
                    raise ExtensionError("请选择要配置的工作区")
                cfg["workspaces"][pid] = self.select(cfg, data.get("selection"))
            elif action == "import_mcp":
                servers = data.get("config", {}).get("mcpServers") if isinstance(data.get("config"), dict) else None
                if not isinstance(servers, dict) or not servers or len(servers) > 100:
                    raise ExtensionError("导入需要包含 1–100 个 server 的 mcpServers 对象")
                for name, config in servers.items():
                    if not NAME.fullmatch(name):
                        raise ExtensionError("MCP 名称使用 1–64 位英文字母、数字、下划线或连字符")
                    previous = next((r for r in cfg["mcp"] if r["name"] == name), None)
                    item = {"id": previous["id"] if previous else uuid.uuid4().hex[:16], "name": name, "config": mcp_config(config)}
                    if previous:
                        cfg["mcp"][cfg["mcp"].index(previous)] = item
                    else:
                        cfg["mcp"].append(item)
            elif action in ("save", "delete"):
                kind = data.get("kind")
                if kind not in ("mcp", "skills"):
                    raise ExtensionError("资源类型不正确")
                identity = data.get("id")
                previous = next((r for r in cfg[kind] if r["id"] == identity), None)
                if identity and previous is None:
                    raise ExtensionError("资源不存在", 404)
                if action == "delete":
                    if not previous:
                        raise ExtensionError("请选择要移除的资源")
                    field = "mcp_ids" if kind == "mcp" else "skill_ids"
                    if any(identity in s[field] for s in cfg["workspaces"].values()):
                        raise ExtensionError("请先从工作区默认选择中取消此资源；已有会话快照不受影响", 409)
                    cfg[kind].remove(previous)
                else:
                    name = data.get("name", "")
                    if not isinstance(name, str) or not NAME.fullmatch(name):
                        raise ExtensionError("资源名称使用 1–64 位英文字母、数字、下划线或连字符")
                    if any(r["name"] == name and r is not previous for r in cfg[kind]):
                        raise ExtensionError("同类型资源名称不能重复")
                    item = {"id": identity or uuid.uuid4().hex[:16], "name": name}
                    if kind == "mcp":
                        item["config"] = mcp_config(data.get("config"))
                    else:
                        source, _ = skill_files(string(data.get("path"), "skill 路径"), name)
                        if source.is_relative_to(self.app.state):
                            raise ExtensionError("不能从 Web 私有凭据目录导入 skill", 403)
                        item["path"] = str(source)
                    if previous:
                        cfg[kind][cfg[kind].index(previous)] = item
                    else:
                        cfg[kind].append(item)
            else:
                raise ExtensionError("资源操作不正确")
            cfg["revision"] += 1
            sapper_context.atomic_private(self.path, cfg)
            self.app.audit("extensions.updated", workspace=pid, operation=action, revision=cfg["revision"])
            return self.view(pid)

    def prepare(self, record, data, *, resume=False):
        root = Path(self.app.project(record["workspace_id"])["path"])
        directory = root / ".sapper/sessions" / record["claude_session_id"]
        self.app.inside(directory, root)
        # Do not follow even an in-workspace alias to another session's files.
        for path in (root / ".sapper", directory.parent, directory):
            if path.is_symlink():
                raise ExtensionError("会话存储目录不能为符号链接", 409)
        if resume:
            try:
                manifest_path = directory / "manifest.json"
                if manifest_path.is_symlink() or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != record["extension_manifest_sha256"]:
                    raise ValueError("manifest changed")
                manifest = json.loads(manifest_path.read_text())
                if manifest["agent_id"] != record["id"] or manifest["session_id"] != record["claude_session_id"]:
                    raise ValueError("identity changed")
                if hashes(directory / "bundle") != manifest["files"]:
                    raise ValueError("bundle changed")
            except (OSError, ValueError, KeyError) as exc:
                raise ExtensionError("会话配置快照缺失或已变化；请还原快照或新建会话", 409) from exc
        else:
            cfg = self.read()
            selection = self.select(cfg, data.get("extensions", cfg["workspaces"].get(record["workspace_id"], EMPTY)))
            mcp = [r for r in cfg["mcp"] if r["id"] in selection["mcp_ids"]]
            skills = [r for r in cfg["skills"] if r["id"] in selection["skill_ids"]]
            sources = [(s, *skill_files(s["path"], s["name"])) for s in skills]
            instructions = workspace_instructions(root)
            directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            directory.parent.chmod(0o700)
            bundle = directory / "bundle"
            bundle.mkdir(mode=0o700)
            try:
                plugin = bundle / "plugin"
                sapper_context.atomic_private(plugin / ".claude-plugin/plugin.json", {"name": "sapper", "version": "1.0.0"})
                for item, source, files in sources:
                    dest = plugin / "skills" / item["name"]
                    for path in files:
                        target = dest / path.relative_to(source)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target, follow_symlinks=False)
                sapper_context.atomic_private(bundle / "mcp.json", {"mcpServers": {s["name"]: s["config"] for s in mcp}})
                patch = sapper._load_settings_patch()
                settings = {"disableBundledSkills": True, "disableClaudeAiConnectors": True,
                            "skillOverrides": {"plugin-authoring": "off", "doctor": "off"},
                            "hooks": {event: [{"hooks": [{"type": "command", "command": patch._cmd(str(REPO / "lib/sapper-hook.py"), arg)}]}]
                                      for event, arg in patch.HOOK_EVENTS}}
                sapper_context.atomic_private(bundle / "settings.json", settings)
                (bundle / "instructions.md").write_text(instructions, encoding="utf-8")
                manifest = {"schema": 1, "agent_id": record["id"], "session_id": record["claude_session_id"],
                            "workspace_id": record["workspace_id"], "library_revision": cfg["revision"],
                            "selection": selection, "mcp": [{"id": s["id"], "name": s["name"]} for s in mcp],
                            "skills": skills, "files": hashes(bundle), "created_at": sapper.now_iso()}
                sapper_context.atomic_private(directory / "manifest.json", manifest)
                (directory / "claude").mkdir(mode=0o700)
            except Exception:
                shutil.rmtree(directory)
                raise
            record["extension_manifest_sha256"] = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
            record["extensions"] = {"mcp": manifest["mcp"], "skills": [{"id": s["id"], "name": s["name"]} for s in skills],
                                    "library_revision": cfg["revision"]}
            record["session_storage"] = str(directory)
        config_dir = directory / "claude"
        if config_dir.is_symlink() or not config_dir.is_dir():
            raise ExtensionError("会话 Claude 数据目录缺失或为符号链接", 409)
        record["claude_config_dir"] = str(config_dir)
        bundle = directory / "bundle"
        record["mcp_env_refs"] = sorted(set(ENV_REF.findall((bundle / "mcp.json").read_text())))
        argv = ["--setting-sources", "", "--settings", str(bundle / "settings.json"),
                "--strict-mcp-config", "--mcp-config", str(bundle / "mcp.json"), "--plugin-dir", str(bundle / "plugin")]
        env = {"CLAUDE_CONFIG_DIR": str(config_dir), "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
               "DISABLE_DOCTOR_COMMAND": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
               "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
               # Documented CLI switch: skip first-run onboarding for each new
               # config directory; also hides account identity in the TUI header.
               "IS_DEMO": "1"}
        return argv, env, (bundle / "instructions.md").read_text(encoding="utf-8")

    def credential_values(self):
        """Used only for redaction, never returned through the library API."""
        for item in self.read()["mcp"]:
            for name in ENV_REF.findall(json.dumps(item["config"])):
                if os.environ.get(name):
                    yield os.environ[name]
        # Deleted/edited library entries must not remove redaction for old sessions.
        for path in (self.app.state / "agents").glob("*.json"):
            for name in json.loads(path.read_text()).get("mcp_env_refs", []):
                if os.environ.get(name):
                    yield os.environ[name]
