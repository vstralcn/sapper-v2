"""Read a competition roster and project its existing graph records."""
from __future__ import annotations

import json
import re
from pathlib import Path

import sapper


FILENAME = "competition.json"
MAX_TASKS = 1000
MAX_BYTES = 1024 * 1024
DIRECTORY_CATEGORIES = {
    name.casefold(): name
    for name in ("Web", "Pwn", "Reverse", "Crypto", "Misc", "Forensics", "OSINT", "Mobile", "Blockchain")
} | {"re": "Reverse", "rev": "Reverse"}


def roster_text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        raise sapper.FGError(f"题单的{label}必须为非空文本（最多 {limit} 字符）")
    return value.strip()


def normalize_roster(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise sapper.FGError("competition.json 需要包含 tasks 列表")
    if len(raw["tasks"]) > MAX_TASKS or type(raw.get("complete", False)) is not bool:
        raise sapper.FGError(f"题单最多支持 {MAX_TASKS} 道题，complete 必须为布尔值")
    source = roster_text(raw.get("source", "competition.json"), "来源", 500)
    tasks = {}
    for row in raw["tasks"]:
        if not isinstance(row, dict):
            raise sapper.FGError("题单的每道题必须为 JSON 对象")
        code = roster_text(row.get("id"), "题号", 160)
        if code in tasks:
            raise sapper.FGError(f"题单含重复题号：{code}")
        tasks[code] = {"id": code, "name": roster_text(row.get("name"), "题名", 160),
                       "category": roster_text(row.get("category", "未分类"), "方向", 80)}
    return {"source": source, "complete": raw.get("complete", False), "tasks": list(tasks.values())}


def read_roster(root: Path):
    path = root / FILENAME
    if path.is_symlink():
        raise sapper.FGError("competition.json 必须是工作区内的普通文件")
    if not path.exists():
        return None
    if not path.is_file():
        raise sapper.FGError("competition.json 必须是工作区内的普通文件")
    try:
        with path.open("rb") as source:
            content = source.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise sapper.FGError("competition.json 超过 1 MiB")
        return normalize_roster(json.loads(content))
    except (OSError, ValueError, RecursionError) as exc:
        raise sapper.FGError("无法读取 competition.json，请检查 JSON 格式") from exc


def scan_directory(directory: Path, *, complete: bool):
    def folders(base):
        return sorted((entry for entry in base.iterdir()
                       if not entry.name.startswith(".") and not entry.is_symlink() and entry.is_dir()),
                      key=lambda entry: entry.name.casefold())

    tasks = []
    for folder in folders(directory):
        category = DIRECTORY_CATEGORIES.get(folder.name.casefold())
        if category:
            tasks.extend({"id": child.relative_to(directory).as_posix(), "name": child.name, "category": category}
                         for child in folders(folder))
        else:
            tasks.append({"id": folder.name, "name": folder.name, "category": "未分类"})
    if not tasks:
        raise sapper.FGError("所选目录没有题目子目录")
    return normalize_roster({"source": f"本地目录 {directory.name or directory}", "complete": complete, "tasks": tasks})


def with_graph(roster, graph):
    """Associate exact IDs and expose receipt metadata without flag values."""
    tasks = {task["id"]: {**task, "intent_ids": [], "receipt": None} for task in roster["tasks"]}
    fact_topics = {}
    for fact in graph["facts"]:
        data = fact.get("data") or {}
        topic = data.get("competition_challenge", data.get("challenge"))
        if topic in tasks:
            fact_topics[fact["id"]] = topic
        if fact.get("kind") != "flag" or data.get("challenge") not in tasks:
            continue
        status = data.get("status")
        if status not in ("submitted", "rejected"):
            continue
        task = tasks[data["challenge"]]
        receipt = task["receipt"]
        stamp = data.get("submission_at")
        if not isinstance(stamp, str):
            stamp = None
        order = (stamp or fact.get("created_at") or "", fact["id"])
        if receipt is None or (status == "submitted", order) > (receipt["status"] == "submitted", receipt["order"]):
            task["receipt"] = {"status": status, "fact_id": fact["id"], "at": stamp, "order": order}

    for intent in sorted(graph["intents"], key=lambda row: (row["created_at"], len(row["id"]), row["id"])):
        topic = intent.get("competition_challenge")
        if topic is None:
            candidates = {fact_topics[fid] for fid in intent["from"] if fid in fact_topics}
            if len(candidates) == 1:
                topic = next(iter(candidates))
        if topic in tasks:
            tasks[topic]["intent_ids"].append(intent["id"])
            if intent["to"] and intent["to"] != "goal":
                fact_topics.setdefault(intent["to"], topic)

    for task in tasks.values():
        if task["receipt"]:
            del task["receipt"]["order"]
    return {**roster, "tasks": list(tasks.values())}
