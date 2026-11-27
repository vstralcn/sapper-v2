"""Headless process host for the frozen v1.0 control plane.

The control plane owns processes, resources, transport, liveness detection and
structural routing.  It never reads agent business text.  This module is
intentionally tmux-free: a run is one independent process group launched with
subprocess.Popen, its stdout carrying the structured protocol stream and its
stderr isolated to a separate file.

Phase-2 rollout: import and unit-test this module while Console.start still
uses tmux.  The cutover (flip default_execution_mode to chat and call
SubprocessHost.spawn from Console.start) is a single later step.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import sapper


def next_generation(state):
    """Allocate a monotonically increasing spawn generation (epoch) code.

    Frozen spec v1.0 二: every spawn gets a new, monotonically increasing code;
    a late ready signal from an older code is discarded.  The counter lives in
    the control plane's private state and is incremented under an flock so a
    restarted web service never reuses a generation.
    """
    path = Path(state) / "generation.counter"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            try:
                current = int((handle.read().strip() or "0"))
            except ValueError:
                current = 0
            value = current + 1
            handle.seek(0)
            handle.truncate()
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
            return value
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

# Frozen spec v1.0 二: startup timeout is configured per worker type, measured
# from spawn (Popen) not from a later ready marker.
STARTUP_TIMEOUT = {
    "commander": 120,
    "general": 120,
    "worker": 90,
    "scout": 60,
    "verifier": 90,
    "custom": 90,
}
DEFAULT_STARTUP_TIMEOUT = 90

# Terminal env must not leak into a headless child: faking TERM/TMUX makes the
# CLI believe it owns a PTY and enables interactive-only behavior.
STRIP_ENV = ("TERM", "TMUX", "TMUX_PANE", "SAPPER_TMUX_SOCKET")

HANDSHAKE_REQUEST_ID = "sapper-generation-ready"
# Claude Code echoes request_id verbatim in control_response but does NOT echo
# arbitrary request-body fields.  The generation therefore lives in the
# request_id itself: "sapper-initialize:<generation>".  This is the only
# reliable generation-carrying channel on both the fixture and the real CLI.
INIT_PREFIX = "sapper-initialize"


def init_request_id(generation):
    return "%s:%d" % (INIT_PREFIX, int(generation))


def validate_init_ack(event, generation):
    """Classify an init control_response for the current generation.

    Frozen spec v1.0 二: only a response echoing the CURRENT generation is a
    valid ready signal; a legacy/unGeneration'd ack is NOT accepted, and a
    late ack from an older generation is explicitly "stale".  The generation
    lives in request_id (the only field the CLI echoes verbatim).

    Returns "ok" | "stale" | "error" | None (not our init ack at all).  An
    unrelated control_request failure must never abort the session, so the
    request_id is checked before the subtype/error classification.
    """
    if not isinstance(event, dict) or event.get("type") != "control_response":
        return None
    response = event.get("response") or {}
    rid = response.get("request_id")
    if not isinstance(rid, str) or not rid.startswith(INIT_PREFIX + ":"):
        return None  # unrelated request_id — never an init error
    if response.get("subtype") == "error":
        return "error"
    try:
        echoed = int(rid.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return "stale"
    return "ok" if echoed == int(generation) else "stale"


def clean_headless_environment(env):
    """Return env with terminal/multiplexer variables removed."""
    result = dict(env)
    for key in STRIP_ENV:
        result.pop(key, None)
    result["NO_COLOR"] = "1"
    return result


def startup_timeout_for(role):
    try:
        return int(STARTUP_TIMEOUT.get(role, DEFAULT_STARTUP_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_STARTUP_TIMEOUT


class SubprocessHost:
    """Launch and reap one headless Claude process in its own process group."""

    def __init__(self, run_dir, *, role="worker", generation=1):
        self.run_dir = Path(run_dir)
        self.role = role
        self.generation = int(generation)
        self.path = self.run_dir / "host.json"
        self._files = []  # stdout/stderr fds held open by this host instance

    def spawn(self, argv, cwd, env, *, stdout_path, stderr_path):
        """Popen a child in a new process group with split output streams.

        stdout_path carries the NDJSON turn stream; stderr_path is a separate
        file so CLI warnings/hook errors can never corrupt the stream or block
        on an undrained PIPE.  Identity is persisted atomically for later liveness
        and physical reaping via RunProcesses.
        """
        cwd = Path(cwd)
        child_env = clean_headless_environment(env)
        stdout_path = Path(stdout_path)
        stderr_path = Path(stderr_path)
        stdout_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Run logs may contain NDJSON with prompts/tool args: keep them 0o600.
        def private_append(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            return os.fdopen(fd, "ab")
        stdout_fp = private_append(stdout_path)
        stderr_fp = private_append(stderr_path)
        try:
            child = subprocess.Popen(
                list(argv), cwd=str(cwd), env=child_env,
                stdin=subprocess.PIPE,
                stdout=stdout_fp, stderr=stderr_fp,
                start_new_session=True,
            )
        except BaseException:
            stdout_fp.close()
            stderr_fp.close()
            raise
        self._files.extend((stdout_fp, stderr_fp))
        started = time.time()
        # Frozen spec v1.0 二: the startup timeout is measured from the spawn
        # instant.  Persist a monotonic origin here so the supervisor, which
        # starts later, measures against the SAME clock and includes the
        # wrapper Popen + ready-marker wait.
        record = {
            "pid": child.pid,
            "pgid": child.pid,  # start_new_session makes the child its leader
            "generation": self.generation,
            "role": self.role,
            "started_epoch": started,
            "spawn_monotonic": time.monotonic(),
            "startup_timeout": startup_timeout_for(self.role),
            "cwd": str(cwd),
        }
        try:
            from sapper_processes import process_table
            births = process_table()
            if child.pid in births:
                record["birth"] = births[child.pid]["birth"]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        sapper.atomic_write_json(self.path, record)
        return child, record

    def record(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def ready_payload(self):
        """The reserved control_request body carrying the current generation."""
        return {
            "type": "control_request",
            "request_id": HANDSHAKE_REQUEST_ID,
            "request": {"subtype": "initialize", "hooks": None,
                        "generation": self.generation},
        }

    def is_ready(self, event):
        """True only for an ack of the reserved request with the current generation."""
        if not isinstance(event, dict) or event.get("type") != "control_response":
            return False
        response = event.get("response") or {}
        if response.get("request_id") != HANDSHAKE_REQUEST_ID:
            return False
        if response.get("subtype") == "error":
            return False
        body = response.get("response")
        if isinstance(body, dict):
            generation = body.get("generation")
        else:
            generation = None
        try:
            return generation is not None and int(generation) == self.generation
        except (TypeError, ValueError):
            return False

    def reap(self, *, grace=1.0, force=False):
        """Physically reap the process group; return evidence of what was cleared.

        Frozen spec v1.0 三: STARTUP_TIMEOUT hard-kills immediately (force=True
        sends SIGKILL first, no SIGTERM grace).  Ownership cannot be confirmed
        from a bare/reused PGID: RunProcesses raises in that case and we let it
        propagate rather than emit false "cleaned" evidence.
        """
        record = self.record()
        # Frozen spec v1.0 三: reap the WHOLE tree, including the detached child
        # process group and its tool descendants.  host.json owns the wrapper;
        # run.json (written by the supervisor) adds the child PGID and every
        # PID birth.  Merge both so no orphan survives physical cleanup.
        run_path = self.run_dir / "run.json"
        run = {}
        if run_path.exists():
            try:
                run = json.loads(run_path.read_text())
            except (OSError, ValueError):
                run = {}
        births = dict(run.get("process_births") or {})
        if record.get("birth"):
            births.setdefault(str(record["pid"]), record["birth"])
        info = {
            "wrapper_pid": run.get("wrapper_pid") or record.get("pid"),
            "pid": run.get("pid") or record.get("pid"),
            "process_group": run.get("process_group") or record.get("pgid"),
            "child_process_group": run.get("child_process_group"),
            "process_births": births,
            "created_at": run.get("created_at") or sapper.now_iso(),
            "started_epoch": record.get("started_epoch"),
        }
        evidence = {
            "generation": self.generation,
            "cwd": record.get("cwd") or "",
            "orphan_count": 0,
            "exit_signal": signal.SIGKILL if force else None,
            "physically_cleared": False,
        }
        from sapper_processes import RunProcesses
        processes = RunProcesses(info)  # may raise on unconfirmed ownership
        remaining = processes.remaining()
        evidence["orphan_count"] = max(0, len(remaining) - 1)
        if force:
            processes.signal(signal.SIGKILL)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and processes.remaining():
                time.sleep(0.02)
        else:
            processes.finish(grace=grace)
        evidence["physically_cleared"] = not processes.remaining()
        if not evidence["physically_cleared"]:
            raise OSError("无法确认物理清理完成；不归还槽位")
        evidence["orphan_count_reaped"] = evidence["orphan_count"]
        self.close()
        return evidence

    def close_stdin(self, child):
        """Close the stdin write-end when no more frames will be sent."""
        if child is not None and child.stdin is not None:
            with contextlib.suppress(OSError):
                child.stdin.close()

    def close(self):
        for handle in self._files:
            with contextlib.suppress(OSError):
                handle.close()
        self._files = []

    def crash_evidence(self, returncode, *, last_heartbeat=None):
        """Build the CRASHED evidence payload from control-plane observations only."""
        code = None
        sig = None
        if isinstance(returncode, int):
            if returncode < 0:
                sig = -returncode
            else:
                code = returncode
        evidence = {
            "generation": self.generation,
            "exit_code": code,
            "signal": sig,
            "last_heartbeat": last_heartbeat,
            "cwd": self.record().get("cwd") or "",
            # Frozen spec v1.0 三: scene is frozen before physical cleanup —
            # the working directory and its artifacts are preserved for the
            # superior to inspect; this marker records that contract.
            "scene_frozen": True,
            "cwd_preserved": self.record().get("cwd") or "",
            "side_effect_warning": "orphans_may_have_written_files_or_called_external_apis_before_cleanup",
        }
        # Frozen spec v1.0 三: CRASHED hard-kills residual orphans (no graceful
        # SIGTERM first) so a half-executed task cannot keep running.
        evidence.update(self.reap(force=True))
        return evidence

    def startup_timeout_evidence(self):
        """STARTUP_TIMEOUT: hard-kill immediately, then report zero-side-effect warning."""
        evidence = self.reap(force=True)
        evidence.update({
            "generation": self.generation,
            "exit_signal": signal.SIGKILL,
            "cwd": self.record().get("cwd") or "",
            "side_effect_warning": "task_never_started_retry_safe",
        })
        return evidence


def classify_exit(run_dir, returncode, *, ready, last_heartbeat=None,
                  started_monotonic=None, elapsed=None, reason=None):
    """Classify a supervisor exit into the frozen v1.0 failure semantics.

    Only runs with a host record (headless SubprocessHost) are classified;
    legacy tmux-backed runs have no host.json and return None so their status
    is untouched.  STARTUP_TIMEOUT requires the configured timeout to have
    ACTUALLY elapsed before ready — an early exit is a CRASHED, not a timeout.
    CRASHED = was ready then exited abnormally.  Clean exit = None.
    """
    host_path = Path(run_dir) / "host.json"
    if not host_path.exists():
        return None
    # An explicit stop / budget timeout / interrupt is a control-plane action,
    # never a STARTUP_TIMEOUT or CRASHED failure event.  The supervisor signalled
    # the child on purpose, so a non-zero rc here is expected.
    if reason in ("stopped", "timed_out", "interrupted"):
        return None
    try:
        record = json.loads(host_path.read_text())
        generation = int(record.get("generation", 0))
        startup_timeout = int(record.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT))
    except (OSError, ValueError, TypeError):
        return None
    # Reconstruct the host with the RECORD's generation so evidence matches run.json.
    host = SubprocessHost(run_dir, role=record.get("role") or "worker", generation=generation)
    try:
        if not ready:
            if elapsed is None and started_monotonic is not None:
                elapsed = time.monotonic() - started_monotonic
            if elapsed is not None and elapsed < startup_timeout:
                # Never ready but killed before the deadline: an early abnormal
                # exit, not a startup timeout.
                return {"status": "crashed",
                        "evidence": host.crash_evidence(returncode, last_heartbeat=last_heartbeat),
                        "generation": generation, "auto_retry": False}
            evidence = host.startup_timeout_evidence()
            evidence["last_heartbeat"] = last_heartbeat
            return {"status": "startup_timeout", "evidence": evidence,
                    "generation": generation, "auto_retry": False}
        code = None
        sig = None
        if isinstance(returncode, int) and returncode < 0:
            sig = -returncode
        elif isinstance(returncode, int) and returncode != 0:
            code = returncode
        if code is None and sig is None:
            return None  # clean exit
        evidence = host.crash_evidence(returncode, last_heartbeat=last_heartbeat)
        return {"status": "crashed", "evidence": evidence,
                "generation": generation, "auto_retry": False}
    except OSError:
        # Physical cleanup could not be confirmed: do not emit false evidence
        # and do not release the slot. Mark for external handling.
        return {"status": "crashed", "evidence": None, "generation": generation,
                "cleanup_unconfirmed": True, "auto_retry": False}
