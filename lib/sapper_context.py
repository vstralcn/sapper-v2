"""Explicit, user-owned shared context for managed sapper processes.

Only the path exported by the console is read. No global Claude settings are
modified, and the configured text is never executed as a hook command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import sys
from datetime import datetime, timezone

CONTEXT_ENV = "SAPPER_GLOBAL_CONTEXT_FILE"
PROMPT_ENV = "SAPPER_CONTEXT_PROMPT_REVISION"
MAX_CONTENT = 12000


def defaults():
    return {"revision": 0, "enabled": False, "prompt_enabled": True,
            "hook_enabled": True, "content": "", "updated_at": None}


def load(path=None, *, strict=False):
    path = path or os.environ.get(CONTEXT_ENV)
    if not path:
        return defaults()
    try:
        file = Path(path)
        if not file.exists():
            return defaults()
        if file.stat().st_size > 100000:
            raise ValueError("全局上下文文件过大")
        data = json.loads(file.read_text())
        if (not isinstance(data, dict) or not isinstance(data.get("content"), str)
                or len(data["content"]) > MAX_CONTENT or "\x00" in data["content"]
                or type(data.get("revision")) is not int or data["revision"] < 0
                or any(type(data.get(k)) is not bool for k in ("enabled", "prompt_enabled", "hook_enabled"))):
            raise ValueError("全局上下文格式不正确")
        return {**defaults(), **data}
    except (OSError, ValueError, TypeError) as exc:
        if strict:
            raise ValueError("无法读取全局上下文配置，请修复配置文件") from exc
        return defaults()  # A configuration failure must not break a Claude hook.


def render(config):
    if not config["enabled"] or not config["content"].strip():
        return ""
    return (f"[Sapper shared environment · revision {config['revision']}]\n"
            "The following shared context was configured by the user for all managed roles. "
            "It describes available resources and environment, not proof of connectivity. "
            "Use this revision in place of older shared-environment revisions.\n"
            + config["content"].strip() + "\n[End Sapper shared environment]")


def atomic_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".context-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def prompt(config=None):
    config = config if config is not None else load()
    return render(config) if config["prompt_enabled"] else ""


def hook(event):
    config = load()
    content = render(config) if config["hook_enabled"] else ""
    if not content:
        return ""
    reused = os.environ.get(PROMPT_ENV) == str(config["revision"])
    identity = os.environ.get("SAPPER_AGENT_ID") or "run-" + os.environ.get("SAPPER_RUN_ID", "")
    if re.fullmatch(r"(?:run-)?[a-f0-9]{16}", identity):
        try:
            atomic_private(Path(os.environ[CONTEXT_ENV]).parent / "context-receipts" / (identity + ".json"), {
                "revision": config["revision"], "event": event,
                "source": "startup-prompt" if reused else "hook-output",
                "checked_at": datetime.now(timezone.utc).isoformat()})
        except OSError:
            pass
    # The same content is already in the persistent appended system prompt.
    # Changed revisions still reach the next hook in an existing process.
    return "" if reused else content


def receipt(state, identity):
    if not re.fullmatch(r"[a-f0-9]{16}", identity):
        return None
    try:
        return json.loads((Path(state) / "context-receipts" / (identity + ".json")).read_text())
    except (OSError, ValueError):
        return None


def ensure_hooks(directory):
    """Merge project hooks for an explicitly managed CLI, preserving user settings."""
    import _settings_patch
    directory = Path(directory).resolve()
    settings = directory / ".claude/settings.json"
    if not settings.resolve().is_relative_to(directory) or not settings.parent.resolve().is_relative_to(directory):
        raise ValueError("项目 hooks 路径不能指向工作目录之外")
    _settings_patch.validate(settings)
    _settings_patch.add(settings, str(Path(__file__).with_name("sapper-hook.py")))


def exec_cli(argv):
    if not argv:
        raise ValueError("缺少原 CLI 命令")
    config = load(strict=True)
    env = dict(os.environ)
    if env.get(CONTEXT_ENV) and config["enabled"] and config["hook_enabled"]:
        ensure_hooks(Path.cwd())
    shared = prompt(config)
    if shared:
        if "--append-system-prompt" in argv:
            index = argv.index("--append-system-prompt") + 1
            argv[index] += "\n\n" + shared
        else:
            argv += ["--append-system-prompt", shared]
    env[PROMPT_ENV] = str(config["revision"]) if shared else ""
    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    if sys.argv[1:2] != ["exec"]:
        raise SystemExit("usage: sapper_context.py exec <original Claude CLI and arguments>")
    try:
        exec_cli(sys.argv[2:])
    except (OSError, ValueError, IndexError) as exc:
        print(f"[sapper context] {exc}", file=sys.stderr)
        raise SystemExit(1)
