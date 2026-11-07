"""Observe and reap only a managed run's process tree before releasing work."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from datetime import datetime


def process_table():
    # No command arguments or environments: those may contain prompts/secrets.
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
                            capture_output=True, text=True, timeout=5, check=True,
                            env={**os.environ, "LC_ALL": "C"})
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and fields[0].isdigit():
            pid, parent, group = map(int, fields[:3])
            if "Z" not in fields[3]:
                rows[pid] = {"parent": parent, "group": group, "birth": fields[4].strip()}
    return rows


class RunProcesses:
    def __init__(self, info):
        self.identities = {}
        rows = process_table()
        saved = info.get("process_births", {})
        def original(pid):
            if pid not in rows:
                return False
            if str(pid) in saved:
                return rows[pid]["birth"] == saved[str(pid)]
            if not info.get("created_at"):
                return True
            # Old run records have no birth identity. A reused leader born after
            # launch must never be treated as the old CLI or wrapper.
            birth = time.mktime(time.strptime(rows[pid]["birth"], "%a %b %d %H:%M:%S %Y"))
            created = datetime.fromisoformat(info["created_at"].replace("Z", "+00:00")).timestamp()
            launched = info.get("started_epoch", created + 10)
            return created - 1 <= birth <= launched + 1

        self.roots = {p for p in (info.get("wrapper_pid"), info.get("pid"))
                      if isinstance(p, int) and p > 1 and p != os.getpid() and original(p)}
        self.groups = {g for g in (info.get("process_group"), info.get("child_process_group"))
                       if isinstance(g, int) and g > 1 and g != os.getpgrp()
                       and (g not in rows or original(g))}
        self.identities = {pid: rows[pid]["birth"] for pid in self.roots}
        self.group_births = {g: saved.get(str(g)) or rows.get(g, {}).get("birth") for g in self.groups}
        self.observe(rows)

    def remaining(self):
        return self.observe(process_table())

    def observe(self, rows):
        # Birth identity prevents a reused PID from being signalled by this stop.
        live = {pid for pid, birth in self.identities.items()
                if pid in rows and rows[pid]["birth"] == birth}
        for group in list(self.groups):
            if group in rows and rows[group]["birth"] != self.group_births[group]:
                self.groups.discard(group)
        for group in self.groups:
            members = {pid for pid, row in rows.items() if row["group"] == group}
            if members and not members.intersection(live):
                # A bare, leaderless PGID is insufficient ownership evidence: it
                # may have been reused after both the old and new leader exited.
                raise OSError("无法确认旧进程组归属；保留意图认领")
            live.update(members)
        while True:
            children = {pid for pid, row in rows.items() if row["parent"] in live}
            if children <= live:
                break
            live.update(children)
        for pid in live:
            if pid != os.getpid():
                self.identities.setdefault(pid, rows[pid]["birth"])
        return {pid for pid in live if pid != os.getpid() and rows[pid]["birth"] == self.identities[pid]}

    def signal(self, sig):
        for pid in self.remaining():
            # Recheck immediately before signalling, including detached tools.
            row = process_table().get(pid)
            if row and row["birth"] == self.identities[pid]:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass

    def finish(self, grace=1.0):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            self.signal(sig)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not self.remaining():
                    return True
                time.sleep(0.05)
        return not self.remaining()
