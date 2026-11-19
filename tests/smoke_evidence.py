#!/usr/bin/env python3
"""Evidence-provenance smoke monitor for sapper-v2.

Exercises the three real entry points against a throwaway project:
  1. CLI  (`lib/sapper.py`)      fact/done with --evidence-source/--repro-command/--artifact
  2. Web console API (`Console`) graph_action fact/done through the same Store helpers
  3. Live HTTP server (`WebServer`) authenticated POST to /api/projects/<id>/fact

Every iteration also checks the negative cases: out-of-project artifacts and
non-list command payloads must be rejected, and reproduction commands must be
stored as text instead of being executed.

Usage:
    python3 tests/smoke_evidence.py [--repo PATH] [--repeat N] [--interval SEC] [--json]

Exit code is non-zero if any iteration fails.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MARKER = "sapper-smoke-should-not-exist"


def _ok(name: str) -> dict:
    return {"check": name, "ok": True}


def _fail(name: str, detail: str) -> dict:
    return {"check": name, "ok": False, "detail": detail}


def check_cli(repo: Path, results: list) -> None:
    sapper = repo / "lib" / "sapper.py"
    with tempfile.TemporaryDirectory(prefix="sapper-smoke-cli-") as tmp:
        project = Path(tmp) / "challenge"
        project.mkdir()
        capture = project / "response.txt"
        capture.write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
        digest = hashlib.sha256(capture.read_bytes()).hexdigest()

        def run(*args, expect=0):
            proc = subprocess.run([sys.executable, str(sapper), "--store", str(project / ".sapper"), *args],
                                  cwd=project, capture_output=True, text=True, timeout=120)
            if proc.returncode != expect:
                raise AssertionError(f"`sapper {' '.join(args)}` exited {proc.returncode}: {proc.stderr.strip()[:300]}")
            return proc.stdout

        run("init", "--goal", "smoke: evidence provenance")
        fact_id = run("fact", "endpoint returned 200", "-k", "service",
                      "--evidence-source", "authorized local lab",
                      # A command that would be visible if Sapper ever executed it.
                      "--repro-command", f"touch {MARKER}",
                      "--artifact", "response.txt").strip()
        run("validate")
        fact_file = project / ".sapper" / "facts" / f"{fact_id}.json"
        fact = json.loads(fact_file.read_text(encoding="utf-8"))
        evidence = fact.get("evidence") or {}
        results.append(_ok("cli.fact.evidence-recorded") if evidence.get("source") == "authorized local lab"
                       else _fail("cli.fact.evidence-recorded", json.dumps(evidence)[:300]))
        artifact = (evidence.get("artifacts") or [{}])[0]
        results.append(_ok("cli.artifact.content-addressed")
                       if artifact.get("sha256") == digest and (project / ".sapper" / artifact.get("path", "x")).is_file()
                       else _fail("cli.artifact.content-addressed", json.dumps(artifact)[:300]))
        results.append(_ok("cli.command.not-executed")
                       if not (project / MARKER).exists() and not (Path.cwd() / MARKER).exists()
                       else _fail("cli.command.not-executed", "reproduction command was executed"))
        # tamper detection
        (project / ".sapper" / artifact["path"]).write_bytes(b"tampered")
        tampered = subprocess.run([sys.executable, str(sapper), "--store", str(project / ".sapper"), "validate"],
                                  cwd=project, capture_output=True, text=True, timeout=120)
        results.append(_ok("cli.validate.detects-tamper")
                       if tampered.returncode != 0 and "mismatch" in (tampered.stdout + tampered.stderr)
                       else _fail("cli.validate.detects-tamper", (tampered.stdout + tampered.stderr).strip()[:300]))
        # restore and complete an intent with evidence
        (project / ".sapper" / artifact["path"]).write_bytes(capture.read_bytes())
        intent_id = run("intent", "--from", fact_id, "verify again").strip()
        run("claim", intent_id, "--worker", "smoke")
        run("done", intent_id, "--worker", "smoke", "--fact", "replayed", "--repro-command", "curl -i http://127.0.0.1/")
        run("validate")
        concluded = json.loads((project / ".sapper" / "facts" / "f002.json").read_text(encoding="utf-8"))
        results.append(_ok("cli.done.evidence-recorded")
                       if (concluded.get("evidence") or {}).get("reproduction") else
                       _fail("cli.done.evidence-recorded", json.dumps(concluded.get("evidence"))[:300]))
        # negative: artifact outside the project
        outside = Path(tmp) / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        rejected = subprocess.run([sys.executable, str(sapper), "--store", str(project / ".sapper"),
                                   "fact", "escape", "--artifact", str(outside)],
                                  cwd=project, capture_output=True, text=True, timeout=120)
        results.append(_ok("cli.rejects-outside-artifact")
                       if rejected.returncode != 0 and "inside current project" in (rejected.stdout + rejected.stderr)
                       else _fail("cli.rejects-outside-artifact", (rejected.stdout + rejected.stderr).strip()[:300]))


def check_console(repo: Path, results: list) -> None:
    sys.path.insert(0, str(repo / "lib"))
    import sapper  # noqa: F401  (imported for validate_graph)
    from sapper_web import Console, WebError

    with tempfile.TemporaryDirectory(prefix="sapper-smoke-web-") as tmp:
        base = Path(tmp)
        app = Console(base / "tasks", base / "state")
        project = app.create_project({"name": "web", "goal": "smoke: web evidence"})
        root = Path(project["path"])
        (root / "capture.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
        digest = hashlib.sha256((root / "capture.txt").read_bytes()).hexdigest()
        fact = app.graph_action(project["id"], "fact", {
            "description": "web captured 200", "kind": "service",
            "evidence_source": "web smoke",
            "repro_commands": [f"touch {MARKER}"],
            "artifacts": ["capture.txt"]})
        evidence = fact.get("evidence") or {}
        results.append(_ok("console.fact.evidence-recorded")
                       if evidence.get("source") == "web smoke" and evidence.get("artifacts") else
                       _fail("console.fact.evidence-recorded", json.dumps(evidence)[:300]))
        results.append(_ok("console.artifact.content-addressed")
                       if (evidence.get("artifacts") or [{}])[0].get("sha256") == digest
                       else _fail("console.artifact.content-addressed", json.dumps(evidence)[:300]))
        results.append(_ok("console.command.not-executed")
                       if not (root / MARKER).exists() and not (Path.cwd() / MARKER).exists()
                       else _fail("console.command.not-executed", "reproduction command was executed"))
        legacy = app.graph_action(project["id"], "fact", {"description": "legacy fact"})
        results.append(_ok("console.legacy-fact-compatible") if legacy.get("evidence") is None
                       else _fail("console.legacy-fact-compatible", json.dumps(legacy)[:300]))
        intent = app.graph_action(project["id"], "intent", {"from": [fact["id"]], "description": "confirm"})
        app.graph_action(project["id"], "claim", {"id": intent["id"], "worker": "smoke"})
        done = app.graph_action(project["id"], "done", {
            "id": intent["id"], "description": "replayed", "worker": "smoke",
            "repro_commands": ["curl -i http://127.0.0.1/"]})
        results.append(_ok("console.done.evidence-recorded")
                       if (done.get("evidence") or {}).get("reproduction") else
                       _fail("console.done.evidence-recorded", json.dumps(done.get("evidence"))[:300]))
        outside = base / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        try:
            app.graph_action(project["id"], "fact", {"description": "escape", "artifacts": [str(outside)]})
            results.append(_fail("console.rejects-outside-artifact", "no error raised"))
        except WebError as exc:
            results.append(_ok("console.rejects-outside-artifact") if "inside current project" in str(exc)
                           else _fail("console.rejects-outside-artifact", str(exc)))
        try:
            app.graph_action(project["id"], "fact", {"description": "bad", "repro_commands": "curl -i http://127.0.0.1/"})
            results.append(_fail("console.rejects-bad-payload", "no error raised"))
        except WebError:
            results.append(_ok("console.rejects-bad-payload"))
        errors = sapper.validate_graph(app.store(project["id"]))
        results.append(_ok("console.graph-valid") if not errors else _fail("console.graph-valid", "; ".join(errors)[:300]))


def check_http(repo: Path, results: list) -> None:
    sys.path.insert(0, str(repo / "lib"))
    from sapper_web import Console, WebServer

    with tempfile.TemporaryDirectory(prefix="sapper-smoke-http-") as tmp:
        base = Path(tmp)
        app = Console(base / "tasks", base / "state")
        server = WebServer(("127.0.0.1", 0), app, prefix="/sapper")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cookie = {"value": ""}
        csrf = {"value": ""}

        def request(path, method="GET", data=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            combined = {"Cookie": cookie["value"], "X-Sapper-CSRF": csrf["value"], **(headers or {})}
            body = None
            if data is not None:
                body = json.dumps(data)
                combined["Content-Type"] = "application/json"
            conn.request(method, "/sapper" + path, body=body, headers=combined)
            response = conn.getresponse()
            raw = response.read()
            result = (response.status, dict(response.getheaders()), raw)
            conn.close()
            return result

        try:
            status, headers, raw = request("/api/session", "POST", {"token": app.token})
            if status != 200:
                results.append(_fail("http.login", f"status {status}"))
                return
            cookie["value"] = headers["Set-Cookie"].split(";", 1)[0]
            csrf["value"] = json.loads(raw)["csrf"]
            project = app.create_project({"name": "http", "goal": "smoke: http evidence"})
            root = Path(project["path"])
            (root / "capture.txt").write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
            body = {"description": "http captured 200", "kind": "service",
                    "evidence_source": "http smoke", "repro_commands": ["curl -i http://127.0.0.1/"],
                    "artifacts": ["capture.txt"]}
            status, _, raw = request(f"/api/projects/{project['id']}/fact", "POST", body)
            fact = json.loads(raw) if status == 200 else {}
            results.append(_ok("http.fact.evidence-recorded")
                           if status == 200 and (fact.get("evidence") or {}).get("artifacts")
                           else _fail("http.fact.evidence-recorded", f"status {status}: {raw[:300]!r}"))
            outside = base / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            status, _, raw = request(f"/api/projects/{project['id']}/fact", "POST",
                                     {"description": "escape", "artifacts": [str(outside)]})
            results.append(_ok("http.rejects-outside-artifact")
                           if status == 400 and b"inside current project" in raw
                           else _fail("http.rejects-outside-artifact", f"status {status}: {raw[:300]!r}"))
            status, _, raw = request(f"/api/projects/{project['id']}/graph")
            graph = json.loads(raw) if status == 200 else {"facts": []}
            results.append(_ok("http.graph-exposes-evidence")
                           if any((f.get("evidence") or {}).get("artifacts") for f in graph.get("facts", []))
                           else _fail("http.graph-exposes-evidence", raw[:300].decode("utf-8", "replace")))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def one_round(repo: Path) -> list:
    results: list = []
    for name, check in (("cli", check_cli), ("console", check_console), ("http", check_http)):
        try:
            check(repo, results)
        except Exception as exc:  # keep the monitor running for the other lanes
            results.append(_fail(f"{name}.crashed", f"{type(exc).__name__}: {exc}"))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evidence provenance smoke monitor")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                        help="sapper-v2 checkout (default: this repository)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", help="print one JSON object per round")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if not (repo / "lib" / "sapper.py").is_file():
        print(f"not a sapper checkout: {repo}", file=sys.stderr)
        return 2
    failures = 0
    for round_index in range(1, max(1, args.repeat) + 1):
        started = time.time()
        results = one_round(repo)
        failed = [r for r in results if not r["ok"]]
        failures += len(failed)
        if args.json:
            print(json.dumps({"round": round_index, "seconds": round(time.time() - started, 2),
                              "checks": len(results), "failed": len(failed), "results": results}, ensure_ascii=False))
        else:
            status = "OK" if not failed else f"FAILED ({len(failed)})"
            print(f"round {round_index}: {len(results) - len(failed)}/{len(results)} checks, {status}, "
                  f"{time.time() - started:.2f}s")
            for item in failed:
                print(f"  - {item['check']}: {item['detail']}")
        if round_index < args.repeat and args.interval:
            time.sleep(args.interval)
    print("SMOKE PASS" if not failures else f"SMOKE FAIL ({failures} failing checks)")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
