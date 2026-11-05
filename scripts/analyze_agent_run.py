#!/usr/bin/env python3
"""Offline analysis of Sapper / Claude Code transcripts; Python stdlib only.

Never executes logged commands, contacts a model, or writes to the source graph.
See docs/agent-run-analysis.md for metric definitions and evidence limitations.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict, deque
import csv
from datetime import datetime, timezone
import difflib
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sys


USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
GRAPH_KEYS = ("facts", "hints", "open", "done")
COUNTS = re.compile(r"counts:\s*(\d+) facts,\s*(\d+) open /\s*(\d+) done intents,\s*(\d+) hints")
HOOK_START = re.compile(r"A fact-graph working memory exists in this project|\[fact-graph\] current state of the working memory:")
HOOK_END = re.compile(r"\n(?:SessionStart|UserPromptSubmit) hook additional context:|</system-reminder>|\nToday's date is|so the NEXT turn can recall it\.")
SAPPER = re.compile(r"(?:^|[\s/])sapper(?:\.py)?\s+(?:--store\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s+)?(fact|done|flag|status|graph|dispatch|recall|doc)\b")
TIMEOUT = re.compile(r"(?:timed?\s*out|TimeoutError|ETIMEDOUT|超时)", re.I)
EXIT = re.compile(r"^(?:Exit code:|Process exited with code|Command exited with (?:code|status))\s*(-?\d+)", re.M)


def stamp(value):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def texts(value):
    """Visible text only: no thinking, image data, arbitrary dict fields, or inputs."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)
    elif isinstance(value, dict):
        if value.get("type") == "text":
            yield from texts(value.get("text"))
        elif value.get("type") == "tool_result":
            yield from texts(value.get("content"))


def graph_parts(text):
    """Recognized hook regions, bounded separately even when both hooks coexist."""
    starts = list(HOOK_START.finditer(text))
    for i, start in enumerate(starts):
        limit = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        end = HOOK_END.search(text, start.end(), limit)
        stop = end.end() if end and end.group().startswith("so the NEXT") else end.start() if end else limit
        yield text[start.start():stop]


def command_info(args, fallback_cwd):
    if not isinstance(args, dict):
        return None, fallback_cwd
    command = args.get("command", args.get("cmd"))
    cwd = args.get("cwd", args.get("workdir", fallback_cwd))
    return command if isinstance(command, str) else None, cwd


def result_status(block, meta, name):
    """Count structured failures, not the word 'error' in a successful file read."""
    meta = meta if isinstance(meta, dict) else {}
    text = "\n".join(texts(block.get("content")))
    code = next((meta[k] for k in ("exitCode", "exit_code", "returncode") if isinstance(meta.get(k), int)), None)
    if code is None and name.lower() in ("bash", "shell", "exec_command"):
        found = EXIT.search(text)
        code = int(found[1]) if found else None
    explicit_timeout = any(meta.get(k) is True for k in ("timed_out", "timedOut", "timeout"))
    error = block.get("is_error") is True or (code is not None and code != 0) or explicit_timeout
    # A timeout of a command is different from its configured timeout or a tool wait.
    timeout = explicit_timeout or (error and bool(TIMEOUT.search(text)))
    evidence = "structured_timeout" if explicit_timeout else "failed_result_text" if timeout else None
    return error, timeout, code, evidence


class Analyzer:
    def __init__(self, bytes_per_token=4.0, near_threshold=0.9, near_window=80, include_inputs=False, graph=None):
        self.bytes_per_token = bytes_per_token
        self.near_threshold = near_threshold
        self.near_window = near_window
        self.include_inputs = include_inputs
        self.flag_values = {n["id"]: (n.get("data") or {}).get("value") for n in graph.nodes["facts"]
                            if n.get("kind") == "flag"} if graph else {}
        self.sessions = {}
        self.sources = []
        self.diagnostics = Counter()
        self.tools = {}
        self.steps = {}
        self.blobs = {}
        self.shapes = {}
        self.previous = {}
        self.request_lists = {}
        self.assistant_content = {}
        self.user_content = {}
        self.requests = {}
        self.seen_events = set()
        self.pending_results = {}

    def session(self, sid):
        return self.sessions.setdefault(sid, {"session_id": sid, "sources": set(), "times": [], "steps": [],
                                              "tools": [], "api_retries": 0, "compactions": 0, "snapshots": []})

    def blob_metrics(self, sid, message):
        hook_bytes = read_bytes = 0
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if message.get("role") in ("user", "system") and block.get("type") == "text":
                hook_bytes += sum(len(part.encode()) for part in graph_parts(block.get("text", "")))
            if block.get("type") == "tool_result":
                call = self.tools.get((sid, block.get("tool_use_id")))
                if call and call.get("graph_read"):
                    read_bytes += sum(len(t.encode()) for t in texts(block.get("content")))
        return hook_bytes, read_bytes

    def read(self, path):
        path = Path(path).resolve()
        source = {"path": str(path), "bytes_at_start": path.stat().st_size, "lines": 0, "invalid_json_lines": 0}
        self.sources.append(source)
        # Stop at the initial byte boundary so a growing log yields a finite snapshot.
        with path.open("rb") as file:
            while file.tell() < source["bytes_at_start"]:
                raw = file.readline(source["bytes_at_start"] - file.tell())
                source["lines"] += 1
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    source["invalid_json_lines"] += 1
                    continue
                if not isinstance(event, dict):
                    self.diagnostics["non_object_rows"] += 1
                    continue
                self.event(event, str(path), source["lines"])

    def event(self, e, path, line):
        sid = str(e.get("sessionId") or e.get("session_id") or Path(path).stem)
        if e.get("isSidechain") or "/subagents/" in path:
            sid += ":" + str(e.get("agentId") or Path(path).stem)
        kind = e.get("type")
        # Request deltas are file-local and must be replayed even for overlapping exports.
        stream = (path, sid)
        if kind == "api-request-blob":
            self.blobs[(stream, e.get("hash"))] = self.blob_metrics(sid, e.get("message") or {})
            return
        if kind == "api-request-shape":
            self.shapes[(stream, e.get("shapeHash"))] = self.blob_metrics(sid, {
                "role": "system", "content": (e.get("shape") or {}).get("system", [])})
            return
        if kind == "api-request":
            self.user_content[stream] = []
            previous = (self.request_lists.get((stream, e["base"])) if e.get("base")
                        else self.previous.get(stream, []))
            keep, tail = e.get("keep"), e.get("tail")
            valid = previous is not None and isinstance(keep, int) and 0 <= keep <= len(previous) and isinstance(tail, list) and all(isinstance(x, str) for x in tail)
            hashes = previous[:keep] + tail if valid else []
            self.previous[stream] = hashes
            self.request_lists[(stream, e.get("id"))] = hashes if valid else None
            metrics = [self.blobs.get((stream, h)) for h in hashes]
            shape_metrics = self.shapes.get((stream, e.get("shapeHash")))
            complete = valid and all(m is not None for m in metrics) and shape_metrics is not None
            self.requests[(sid, e.get("id"))] = {
                "request_timestamp": e.get("timestamp"), "request_source": e.get("querySource"),
                "graph_context_evidence": "request_snapshot" if complete else "incomplete_request_snapshot",
                "graph_hook_bytes": sum(m[0] for m in metrics) + shape_metrics[0] if complete else None,
                "graph_read_output_bytes": sum(m[1] for m in metrics) if complete else None,
            }
            if not complete:
                self.diagnostics["incomplete_request_snapshots"] += 1
            return
        if kind not in ("assistant", "user", "system", "attachment", "result"):
            return
        native_message = e.get("message") or {}
        if isinstance(native_message, dict) and kind == "user":
            blob = {k: native_message[k] for k in ("role", "content") if k in native_message}
            self.blobs[(stream, digest(canonical(blob)))] = self.blob_metrics(sid, blob)
            content = native_message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if isinstance(content, list):
                merged = self.user_content.setdefault(stream, [])
                merged.extend(content)
                blob = {"role": "user", "content": merged}
                self.blobs[(stream, digest(canonical(blob)))] = self.blob_metrics(sid, blob)
        if isinstance(native_message, dict) and kind == "assistant":
            self.user_content[stream] = []
            # Native request logs omit blobs already recoverable from message rows.
            # Match their canonical SHA-256 before trusting a hash reference. Split
            # tool blocks share an id; the request uses their combined content.
            mid = native_message.get("id") or e.get("uuid") or f"{path}:{line}"
            parts = self.assistant_content.setdefault((stream, mid), {})
            blocks = native_message.get("content", [])
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                block_key = e.get("apiBlockIndex") if len(blocks) == 1 and e.get("apiBlockIndex") is not None else digest(canonical(block))
                parts[block_key] = block
            blob = {"role": "assistant", "content": list(parts.values())}
            self.blobs[(stream, digest(canonical(blob)))] = (0, 0)
        uid = e.get("uuid")
        if uid and (sid, uid) in self.seen_events:
            self.diagnostics["duplicate_event_rows"] += 1
            return
        if uid:
            self.seen_events.add((sid, uid))
        session = self.session(sid)
        session["sources"].add(path)
        at = stamp(e.get("timestamp"))
        if at is not None:
            session["times"].append(at)
        if kind == "system":
            session["api_retries"] += e.get("subtype") == "api_retry"
            session["compactions"] += e.get("subtype") == "compact_boundary"
        if kind == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "hook_additional_context":
                for text in texts(a.get("content")):
                    self.snapshot(session, text, at, "hook_attachment")
            return
        message = e.get("message") or {}
        if not isinstance(message, dict):
            return
        content = message.get("content", [])
        if not isinstance(content, list):
            content = []
        step = None
        if kind == "assistant":
            if message.get("model") == "<synthetic>" or e.get("isApiErrorMessage"):
                self.diagnostics["synthetic_assistant_rows"] += 1
                return
            mid = message.get("id") or uid or f"{path}:{line}"
            key = (sid, mid)
            if key not in self.steps:
                step = {"session_id": sid, "message_id": mid, "timestamp": e.get("timestamp"),
                        "end_timestamp": e.get("timestamp"), "model": message.get("model"),
                        "source": path, "line": line, "request_ref": e.get("requestRef"),
                        "usage": {}, "calls": []}
                self.steps[key] = step
                session["steps"].append(step)
            step = self.steps[key]
            if at is not None and (stamp(step["end_timestamp"]) is None or at > stamp(step["end_timestamp"])):
                step["end_timestamp"] = e.get("timestamp")
            if not message.get("id"):
                self.diagnostics["assistant_rows_without_message_id"] += 1
            usage = message.get("usage") or {}
            for field in USAGE:
                value = usage.get(field)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    step["usage"][field] = max(step["usage"].get(field, 0), value)
        for block in content:
            if not isinstance(block, dict):
                continue
            if step is not None and block.get("type") == "tool_use":
                tid = block.get("id") or f"{path}:{line}:{len(step['calls'])}"
                if (sid, tid) in self.tools:
                    continue
                args = block.get("input", {})
                command, cwd = command_info(args, e.get("cwd"))
                match = SAPPER.search(command or "")
                call = {"session_id": sid, "tool_id": tid, "name": str(block.get("name", "unknown")),
                        "args": args, "command": command, "cwd": cwd, "timestamp": e.get("timestamp"),
                        "source": path, "line": line, "step_object": step, "result_received": False,
                        "error": None, "timeout": None, "exit_code": None, "timeout_evidence": None,
                        "fact_ids": [], "flag_ids": [], "submitted_flag_ids": [], "observed_flag_ids": [],
                        "graph_action": match[1] if match else None,
                        "graph_read": bool(match and match[1] in ("status", "graph", "dispatch", "recall", "doc"))}
                self.tools[(sid, tid)] = call
                session["tools"].append(call)
                step["calls"].append(call)
                pending = self.pending_results.pop((sid, tid), None)
                if pending:
                    self.result(call, *pending)
            elif block.get("type") == "tool_result":
                result = (block, e.get("toolUseResult"), e.get("timestamp"), path, line)
                call = self.tools.get((sid, block.get("tool_use_id")))
                if call:
                    self.result(call, *result)
                else:
                    self.pending_results[(sid, block.get("tool_use_id"))] = result

    def snapshot(self, session, text, at, evidence):
        if at is None:
            return
        for found in COUNTS.finditer(text):
            facts, opened, done, hints = map(int, found.groups())
            session["snapshots"].append({"at": at, "facts": facts, "hints": hints,
                                          "open": opened, "done": done, "evidence": evidence})

    def result(self, call, block, meta, timestamp, path, line):
        if call["result_received"]:
            return
        error, timeout, code, evidence = result_status(block, meta, call["name"])
        call.update(result_received=True, error=error, timeout=timeout, exit_code=code,
                    timeout_evidence=evidence, result_timestamp=timestamp, result_source=path, result_line=line)
        text = "\n".join(texts(block.get("content")))
        if not call["graph_read"]:
            args_text = canonical(call["args"])
            call["observed_flag_ids"] = [fid for fid, value in self.flag_values.items()
                                         if isinstance(value, str) and value and value in text and value not in args_text]
        if call["graph_read"] and not error:
            self.snapshot(self.session(call["session_id"]), text, stamp(timestamp), "graph_tool_result")
        action = call["graph_action"]
        if action in ("fact", "done", "flag"):
            # Only known CLI acknowledgements, not arbitrary mentions of IDs or flags.
            if action == "fact" and not error:
                call["fact_ids"] = re.findall(r"^(f\d+)\s*$", text, re.M)
            if action == "done" and not error:
                call["fact_ids"] = re.findall(r"^i\d+ -> (f\d+)\s+\(done\)", text, re.M)
            if action == "flag":
                call["flag_ids"] = re.findall(r"^(f\d+)\s+recorded\b", text, re.M)
                call["submitted_flag_ids"] = re.findall(r"^(f\d+)\s+submitted:", text, re.M)
                call["fact_ids"] = call["flag_ids"][:]

    def finish(self, graph=None):
        all_steps, all_calls, sessions = [], [], []
        for sid, s in self.sessions.items():
            if not s["steps"]:
                continue
            s["steps"].sort(key=lambda row: (stamp(row["timestamp"]) or float("inf"), row["source"], row["line"]))
            start = min(s["times"]) if s["times"] else None
            end = max(s["times"]) if s["times"] else None
            snapshots = sorted(s["snapshots"], key=lambda row: row["at"])
            snapshot_times = [row["at"] for row in snapshots]
            for number, row in enumerate(s["steps"], 1):
                row["step"] = number
                request = self.requests.get((sid, row["request_ref"]), {})
                row.update(request)
                row.setdefault("graph_context_evidence", "unavailable")
                row.setdefault("graph_hook_bytes", None)
                row.setdefault("graph_read_output_bytes", None)
                usage = row.pop("usage")
                row.update({field: usage.get(field) for field in USAGE})
                row["total_input_tokens"] = (sum(usage.get(k, 0) for k in USAGE[:3]) if "input_tokens" in usage else None)
                row["graph_hook_tokens_estimate"] = self.estimate(row["graph_hook_bytes"])
                row["graph_read_tokens_estimate"] = self.estimate(row["graph_read_output_bytes"])
                row["graph_hook_share_estimate"] = (row["graph_hook_tokens_estimate"] / row["total_input_tokens"]
                                                     if row["graph_hook_tokens_estimate"] is not None and row["total_input_tokens"] else None)
                if row["graph_hook_share_estimate"] is not None and row["graph_hook_share_estimate"] > 1:
                    self.diagnostics["estimated_graph_share_above_one"] += 1
                at = stamp(row["request_timestamp"] if row.get("request_timestamp") else row["timestamp"])
                row["elapsed_seconds"] = round(at - start, 3) if at is not None and start is not None else None
                counts = graph.counts(at) if graph else {key: None for key in GRAPH_KEYS}
                row.update(counts)
                row["graph_size_evidence"] = "surviving_nodes_reconstruction" if graph and at is not None else "unavailable"
                pos = bisect.bisect_right(snapshot_times, at) - 1 if at is not None else -1
                observed = snapshots[pos] if pos >= 0 else {}
                for key in GRAPH_KEYS:
                    row["observed_" + key] = observed.get(key)
                row["observed_age_seconds"] = round(at - observed["at"], 3) if observed else None
                row["observed_evidence"] = observed.get("evidence")
                calls = row.pop("calls")
                row["tool_calls"] = len(calls)
                row["tool_results"] = sum(c["result_received"] for c in calls)
                row["tool_errors"] = sum(c["error"] is True for c in calls)
                row["tool_timeouts"] = sum(c["timeout"] is True for c in calls)
                for call in calls:
                    call["step"] = number
                    call.pop("step_object")
                all_steps.append(row)
            all_calls.extend(s["tools"])
            summary = {"session_id": sid, "sources": sorted(s["sources"]), "steps": len(s["steps"]),
                       "started_at": iso(start), "ended_at": iso(end), "api_retries": s["api_retries"],
                       "compactions": s["compactions"], **tool_rates(s["tools"])}
            summary["steps_with_usage"] = sum(r["total_input_tokens"] is not None for r in s["steps"])
            summary["steps_with_graph_request_snapshot"] = sum(r["graph_hook_bytes"] is not None for r in s["steps"])
            summary["total_input_tokens_known"] = sum(r["total_input_tokens"] or 0 for r in s["steps"])
            summary.update(token_totals(s["steps"]))
            summary["milestones"] = session_milestones(s, graph, start, end)
            sessions.append(summary)
        self.diagnostics["unmatched_tool_results"] = len(self.pending_results)
        duplicates = find_duplicates(all_calls, self.near_threshold, self.near_window)
        for s in sessions:
            s.update(duplicate_totals([d for d in duplicates if d["session_id"] == s["session_id"]]))
        by_tool = defaultdict(list)
        for call in all_calls:
            by_tool[call["name"]].append(call)
        public_calls = []
        for c in all_calls:
            public = {k: v for k, v in c.items() if k not in ("args", "command", "cwd", "graph_read")}
            public["input_sha256"] = digest(canonical(c["args"]))
            if self.include_inputs:
                public["input"] = c["args"]
                public["cwd"] = c["cwd"]
            public_calls.append(public)
        return {"schema": "sapper-agent-analysis/1", "generated_at": iso(datetime.now(timezone.utc).timestamp()),
                "definitions": definitions(self), "sources": self.sources, "diagnostics": dict(self.diagnostics),
                "graph": graph.summary() if graph else None, "sessions": sessions, "steps": all_steps,
                "tool_calls": public_calls, "duplicates": duplicates,
                "tools": [{"name": name, **tool_rates(calls)} for name, calls in sorted(by_tool.items())],
                "summary": {"sessions": len(sessions), "steps": len(all_steps), **tool_rates(all_calls),
                            "total_input_tokens_known": sum(r["total_input_tokens"] or 0 for r in all_steps),
                            "steps_with_usage": sum(r["total_input_tokens"] is not None for r in all_steps),
                            "steps_with_graph_request_snapshot": sum(r["graph_hook_bytes"] is not None for r in all_steps),
                            "duplicate_pairs": len(duplicates), **duplicate_totals(duplicates), **token_totals(all_steps)}}

    def estimate(self, size):
        return math.ceil(size / self.bytes_per_token) if size is not None else None


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z") if at is not None else None


def tool_rates(calls):
    results = sum(c["result_received"] for c in calls)
    errors = sum(c["error"] is True for c in calls)
    timeouts = sum(c["timeout"] is True for c in calls)
    return {"tool_calls": len(calls), "tool_results": results, "tool_pending": len(calls) - results,
            "tool_errors": errors, "tool_timeouts": timeouts,
            "tool_error_rate": errors / results if results else None,
            "tool_timeout_rate": timeouts / results if results else None}


def token_totals(steps):
    covered = [s for s in steps if s["graph_hook_tokens_estimate"] is not None and s["total_input_tokens"] is not None]
    estimated = sum(s["graph_hook_tokens_estimate"] for s in covered)
    denominator = sum(s["total_input_tokens"] for s in covered)
    return {"graph_hook_tokens_estimate_known": estimated if covered else None,
            "input_tokens_for_graph_estimate_known": denominator if covered else None,
            "graph_hook_weighted_share_estimate": estimated / denominator if denominator else None}


def duplicate_totals(pairs):
    return {f"{kind}_{match}_repeat_calls": len({(d["session_id"], d["tool_id"]) for d in pairs
                                               if d["kind"] == kind and d["match"] == match})
            for kind in ("tool", "command") for match in ("exact", "near")}


class Graph:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not (self.path / "project.json").is_file():
            raise ValueError(f"Not a Sapper graph: {self.path}")
        self.nodes = {k: [] for k in ("facts", "hints", "intents")}
        self.invalid = []
        self.times = {k: [] for k in (*GRAPH_KEYS, "intents")}
        self.unknown_done = []
        for kind, nodes in self.nodes.items():
            for file in sorted((self.path / kind).glob("*.json")):
                try:
                    node = json.loads(file.read_text())
                    if not isinstance(node, dict) or stamp(node.get("created_at")) is None:
                        raise ValueError("missing timestamp")
                except (ValueError, OSError):
                    self.invalid.append(str(file))
                    continue
                nodes.append(node)
                created = stamp(node["created_at"])
                self.times[kind].append(created)
                if kind == "intents" and node.get("to") is not None:
                    concluded = stamp(node.get("concluded_at"))
                    if concluded is None or concluded < created:
                        self.unknown_done.append(created)
                    else:
                        self.times["done"].append(concluded)
        for times in self.times.values():
            times.sort()

    def counts(self, at):
        if at is None:
            return {k: None for k in GRAPH_KEYS}
        bad = any(t <= at for t in self.unknown_done) or bool(self.invalid)
        done = bisect.bisect_right(self.times["done"], at)
        return {"facts": bisect.bisect_right(self.times["facts"], at) if not self.invalid else None,
                "hints": bisect.bisect_right(self.times["hints"], at) if not self.invalid else None,
                "open": bisect.bisect_right(self.times["intents"], at) - done if not bad else None,
                "done": done if not bad else None}

    def summary(self):
        return {"path": str(self.path), "scope": "shared_project", "evidence": "surviving_nodes_reconstruction",
                "current_counts": {k: len(v) for k, v in self.nodes.items()}, "invalid_files": self.invalid,
                "intents_missing_conclusion_time": len(self.unknown_done)}


def session_milestones(session, graph, start, end):
    facts = {n["id"]: n for n in graph.nodes["facts"] if n.get("id") != "goal"} if graph else {}
    for call in session["tools"]:
        for fid in call["fact_ids"]:
            if facts.get(fid, {}).get("kind") == "flag" and fid not in call["flag_ids"]:
                call["flag_ids"].append(fid)
    milestones = {}
    for metric, field in (("first_fact", "fact_ids"), ("first_flag_observed", "observed_flag_ids"),
                          ("first_flag_recorded", "flag_ids"),
                          ("first_flag_submitted", "submitted_flag_ids")):
        found = []
        for c in session["tools"]:
            for fid in c[field]:
                node = facts.get(fid)
                created = stamp(node.get("created_at")) if node else None
                # A duplicate flag acknowledgement / stale read is not a new discovery.
                if metric != "first_flag_submitted" and created is not None and start is not None and created < start:
                    continue
                found.append((c["step"], c, fid))
        if found:
            step, call, fid = min(found, key=lambda item: item[0])
            at = stamp(call.get("result_timestamp"))
            milestones[metric] = {"step": step, "fact_id": fid,
                                  "evidence": "known_ledger_value_in_tool_output" if metric == "first_flag_observed" else "cli_acknowledgement",
                                  "source": call["result_source"], "line": call["result_line"],
                                  "elapsed_seconds": round(at - start, 3) if at is not None and start is not None else None}
        else:
            milestones[metric] = None
    documented = []
    for c in session["tools"]:
        for fid in c["fact_ids"]:
            node = facts.get(fid, {})
            if node.get("doc") and (start is None or stamp(node.get("created_at")) >= start):
                documented.append((c["step"], fid))
    milestones["first_fact_with_doc_now"] = ({"step": min(documented)[0], "fact_id": min(documented)[1],
                                              "evidence": "current_doc_not_historical_validation"} if documented else None)
    # Time-aligned shared graph progress is explicitly separate from agent attribution.
    shared = {}
    for metric, predicate in (("first_fact", lambda n: True), ("first_flag_recorded", lambda n: n.get("kind") == "flag")):
        candidates = [n for n in facts.values() if start is not None and end is not None
                      and start <= stamp(n["created_at"]) <= end and predicate(n)]
        if candidates:
            node = min(candidates, key=lambda n: n["created_at"])
            at = stamp(node["created_at"])
            started = [r for r in session["steps"] if stamp(r.get("request_timestamp") or r["timestamp"]) is not None
                       and stamp(r.get("request_timestamp") or r["timestamp"]) <= at]
            shared[metric] = {"fact_id": node["id"], "steps_started_by_event": len(started),
                              "elapsed_seconds": round(at - start, 3), "evidence": "shared_graph_time_only"}
        else:
            shared[metric] = None
    milestones["shared_project_during_session"] = shared
    return milestones


def find_duplicates(calls, threshold, window):
    """Exact matches across selected sessions; bounded near matches in each cwd/tool."""
    seen = {"tool": {}, "command": {}}
    recent = defaultdict(lambda: deque(maxlen=window))
    out = []
    for c in sorted(calls, key=lambda r: (stamp(r["timestamp"]) or float("inf"), r["session_id"], r["step"], r["line"])):
        args = c["args"]
        if c["command"] is not None and isinstance(args, dict):
            args = {k: v for k, v in args.items() if k != "description"}
        values = [("tool", canonical(args))]
        if c["command"] is not None:
            values.append(("command", c["command"].strip()))
        for kind, value in values:
            scope = (kind, c["name"] if kind == "tool" else "shell", c["cwd"])
            key = (scope, digest(value))
            prior = seen[kind].get(key)
            similarity = 1.0
            match_type = "exact"
            if prior is None and window and len(value) <= 12000:
                for old_value, old in reversed(recent[scope]):
                    if not value or min(len(value), len(old_value)) / max(len(value), len(old_value)) < threshold:
                        continue
                    matcher = difflib.SequenceMatcher(None, old_value, value, autojunk=False)
                    if matcher.quick_ratio() >= threshold:
                        ratio = matcher.ratio()
                        if ratio >= threshold and (prior is None or ratio > similarity):
                            prior, similarity, match_type = old, ratio, "near"
            if prior:
                out.append({"kind": kind, "match": match_type, "similarity": round(similarity, 4),
                            "session_id": c["session_id"], "step": c["step"], "tool_id": c["tool_id"],
                            "previous_session_id": prior["session_id"], "previous_step": prior["step"],
                            "previous_tool_id": prior["tool_id"], "same_session": c["session_id"] == prior["session_id"],
                            "source": c["source"], "line": c["line"], "previous_source": prior["source"],
                            "previous_line": prior["line"], "input_sha256": key[1]})
            seen[kind][key] = c
            if len(value) <= 12000:
                recent[scope].append((value, c))
    return out


def definitions(analyzer):
    return {
        "step": "One distinct assistant message.id per session; split blocks and replayed UUIDs are deduplicated. Internal requests without an assistant response are not steps.",
        "input_tokens": "Provider usage: input_tokens + cache_creation_input_tokens + cache_read_input_tokens. Missing usage stays null. Max per field across split blocks, never sum.",
        "graph_tokens": f"OFFLINE ESTIMATE ONLY: ceil(UTF-8 bytes / {analyzer.bytes_per_token}). Recognized hook regions in reconstructed request messages. Not a model tokenizer or a billing measure.",
        "graph_read_tokens": "Separate estimate of retained tool outputs from sapper status/graph/dispatch/recall/doc commands (whole result, possibly mixed shell output). Not included in hook share.",
        "unknown": "null means unavailable/not observed, never zero. No snapshot means graph occupancy unknown, even if a hook attachment is visible.",
        "effective_fact": "First new non-goal fact acknowledged by this session's sapper CLI. Recorded does not mean independently verified. Shared graph milestones are separate and cannot establish agent attribution.",
        "flag": "first_flag_observed matches an exact value from the supplied flag ledger in a tool result (excludes graph reads, values already in tool input, and flags predating the session). This is observation, not acceptance. first_flag_recorded/submitted use CLI acknowledgements; prompts and guessed flag patterns never count.",
        "graph_curve": "Shared graph at request time (or assistant event time). Counts include goal. Reconstructed from surviving nodes' created_at/concluded_at; deleted nodes cannot be recovered. observed_* is the last logged count, with age, not an interpolated exact state.",
        "rates": "Errors/timeouts divided by received tool results; pending tools excluded and reported. API retries and process/run timeouts are not tool timeouts.",
        "duplicates": f"Candidates, not proof of waste. Exact canonical JSON/command text plus cwd; shell description ignored. Near: SequenceMatcher >= {analyzer.near_threshold}, last {analyzer.near_window} calls per scope, inputs <=12000 chars. Same command with changed external state may be justified.",
        "privacy": "Inputs and command bodies omitted unless --include-inputs; source/line and hashes remain. Source paths can still be private.",
    }


def discover(project, root):
    project = Path(project).resolve()
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(project))
    candidates = []
    directories = [*Path(root).expanduser().glob(encoded + "*"),
                   *(project / ".sapper/sessions").glob("*/claude/projects/*")]
    for directory in directories:
        for file in directory.rglob("*.jsonl"):
            # Verify actual cwd, not a prefix collision in Claude's encoded directory names.
            with file.open("rb") as stream:
                for _, line in zip(range(120), stream):
                    try:
                        e = json.loads(line)
                        cwd = e.get("cwd") if isinstance(e, dict) else None
                        if cwd and Path(cwd).resolve().is_relative_to(project):
                            candidates.append(file)
                            break
                    except (ValueError, OSError):
                        pass
    return candidates


def write_file(path, content):
    with open(path, "w", encoding="utf-8", newline="") as file:
        os.chmod(path, 0o600)
        file.write(content)


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with open(path, "w", encoding="utf-8", newline="") as file:
        os.chmod(path, 0o600)
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            safe = {}
            for k, v in row.items():
                if isinstance(v, (dict, list)):
                    v = canonical(v)
                if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")):
                    v = "'" + v
                safe[k] = v
            writer.writerow(safe)


def render_html(report):
    # No raw messages, tool arguments, external JS, image rendering, or network requests.
    payload = json.dumps({"sessions": report["sessions"], "steps": report["steps"]}, ensure_ascii=False).replace("<", "\\u003c")
    notes = "".join(f"<li><b>{html.escape(k)}</b>: {html.escape(v)}</li>" for k, v in report["definitions"].items())
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sapper 运行分析</title><style>body{font:15px system-ui;margin:32px auto;max-width:1160px;padding:0 20px;background:#f6f7f9;color:#182330}select{max-width:100%;padding:8px}svg{width:100%;background:white;border:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin:8px 0}.legend{margin:10px 0}span{margin-right:20px}h2{font-size:19px}</style>
<h1>Sapper 运行分析</h1><p>步 = 一次模型响应；图 token 为离线估算。共享图曲线不表示单个 Agent 的贡献。数据缺失显示为空，不补零。</p>
<select id="session" aria-label="会话"></select><pre id="summary"></pre>
<h2>输入 token / 图 hook 注入估算</h2><div id="tokens"></div>
<h2>共享图规模：现存节点时间戳重建</h2><div id="graph"></div>
<h2>日志中最近一次图规模观测（可能陈旧）</h2><div id="observed"></div>
<h2>指标定义与限制</h2><ul>''' + notes + '''</ul><script type="application/json" id="data">''' + payload + '''</script><script>
const data=JSON.parse(document.getElementById('data').textContent),select=document.getElementById('session');
for(const s of data.sessions){const o=document.createElement('option');o.value=s.session_id;o.textContent=s.session_id+' · '+s.steps+' 步';select.append(o)}
function chart(id,rows,keys){const box=document.getElementById(id);box.replaceChildren();const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 1100 300');const colors=['#2764c6','#cf6335','#218b68','#8a50b6'];const values=rows.flatMap(r=>keys.map(k=>r[k]).filter(v=>v!=null));if(!values.length){box.textContent='无可用数据';return}const max=Math.max(1,...values),n=Math.max(2,rows.length);function label(x,y,t){const e=document.createElementNS(ns,'text');e.setAttribute('x',x);e.setAttribute('y',y);e.setAttribute('font-size','12');e.textContent=t;svg.append(e)}label(5,18,Math.round(max));label(5,270,0);label(50,292,'step 1');label(1000,292,'step '+rows.length);keys.forEach((k,j)=>{let d='',pen=false;rows.forEach((r,i)=>{if(r[k]==null){pen=false;return}const x=50+i*1020/(n-1),y=265-r[k]*240/max;d+=(pen?'L':'M')+x.toFixed(2)+','+y.toFixed(2);pen=true});const p=document.createElementNS(ns,'path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',colors[j]);p.setAttribute('stroke-width','2');svg.append(p)});box.append(svg);const legend=document.createElement('div');legend.className='legend';keys.forEach((k,i)=>{const s=document.createElement('span');s.style.color=colors[i];s.textContent=k;legend.append(s)});box.append(legend)}
function draw(){const s=data.sessions.find(s=>s.session_id===select.value);if(!s)return;const rows=data.steps.filter(r=>r.session_id===s.session_id);document.getElementById('summary').textContent=JSON.stringify(s,null,2);chart('tokens',rows,['total_input_tokens','graph_hook_tokens_estimate']);chart('graph',rows,['facts','hints','open','done']);chart('observed',rows,['observed_facts','observed_hints','observed_open','observed_done'])}select.onchange=draw;draw();
</script></html>'''


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_file(output / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for name in ("steps", "tool_calls", "duplicates", "sessions", "tools"):
        write_csv(output / (name + ".csv"), report[name])
    write_file(output / "report.html", render_html(report))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transcript", type=Path, action="append", default=[], help="JSONL or stream-json output.log; repeatable")
    p.add_argument("--project", type=Path, help="Discover native transcripts for this project and its agents")
    p.add_argument("--claude-projects", type=Path, default=Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects")
    p.add_argument("--store", type=Path, help="Optional .sapper graph (defaults to PROJECT/.sapper)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--bytes-per-token", type=float, default=4.0, help="Explicit offline estimate, not a tokenizer")
    p.add_argument("--near-threshold", type=float, default=0.9)
    p.add_argument("--near-window", type=int, default=80, help="0 disables near matching; exact matching remains")
    p.add_argument("--include-inputs", action="store_true", help="Include raw tool parameters; may contain secrets/flags")
    p.add_argument("--verbose", action="store_true", help="Print each input filename")
    args = p.parse_args(argv)
    if not math.isfinite(args.bytes_per_token) or args.bytes_per_token <= 0 or not 0 < args.near_threshold <= 1 or args.near_window < 0:
        p.error("Invalid estimate or similarity parameters")
    try:
        files = args.transcript + (discover(args.project, args.claude_projects) if args.project else [])
        files = sorted({f.resolve() for f in files})
        if not files:
            p.error("No transcripts found; provide --transcript or --project")
        store = args.store or (args.project / ".sapper" if args.project else None)
        graph = Graph(store) if store else None
        analyzer = Analyzer(args.bytes_per_token, args.near_threshold, args.near_window, args.include_inputs, graph)
        print(f"Analyzing {len(files)} transcript(s); source files and graph are read-only.", file=sys.stderr)
        for file in files:
            if args.verbose:
                print(f"Reading {file.name} ({file.stat().st_size:,} bytes)", file=sys.stderr)
            analyzer.read(file)
        report = analyzer.finish(graph)
        if not report["steps"]:
            p.error("No assistant steps found; activity.log/terminal screenshots are not supported transcripts")
        write_report(report, args.out)
        print(json.dumps({"output": str(args.out.resolve()), **report["summary"]}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        p.exit(2, f"analysis failed: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
