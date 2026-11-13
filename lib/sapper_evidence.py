"""Evidence capture helpers for Sapper facts.

Artifacts are copied into the project graph and content-addressed. Reproduction
commands are stored verbatim with a cwd; this module never executes them.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def ingest_artifacts(store, paths: list[str] | None, *, cwd: Path | None = None) -> list[dict]:
    store_root = Path(store) if isinstance(store, (str, os.PathLike)) else Path(store.root)
    base = (cwd or Path.cwd()).resolve()
    out = []
    for raw in paths or []:
        source = Path(raw).expanduser()
        if not source.is_absolute():
            source = base / source
        if source.is_symlink():
            raise ValueError(f"artifact symlinks are not accepted: {raw}")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"artifact does not exist or cannot be resolved: {raw}") from exc
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"artifact must be inside current project directory: {raw}") from exc
        if not resolved.is_file():
            raise ValueError(f"artifact is not a regular file: {raw}")
        size = resolved.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact exceeds {MAX_ARTIFACT_BYTES} byte limit: {raw}")
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        destination = store_root / "artifacts" / sha
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            fd, tmp_name = tempfile.mkstemp(prefix=f".{sha}.", suffix=".tmp", dir=destination.parent)
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                shutil.copyfile(resolved, tmp)
                check = hashlib.sha256()
                with tmp.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        check.update(chunk)
                if check.hexdigest() != sha:
                    raise ValueError(f"artifact changed while being ingested: {raw}")
                try:
                    os.link(tmp, destination)
                except FileExistsError:
                    pass
            finally:
                tmp.unlink(missing_ok=True)
        if destination.stat().st_size != size:
            raise ValueError(f"stored artifact has unexpected size: {raw}")
        stored_hash = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                stored_hash.update(chunk)
        if stored_hash.hexdigest() != sha:
            raise ValueError(f"stored artifact failed SHA-256 verification: {raw}")
        out.append({"id": sha, "path": destination.relative_to(store_root).as_posix(),
                    "name": resolved.name, "sha256": sha, "size": size,
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return out


def provenance(source: str | None, commands: list[str] | None,
               artifacts: list[dict] | None, cwd: Path | None = None) -> dict | None:
    commands = [command.strip() for command in (commands or []) if command.strip()]
    artifacts = artifacts or []
    if not source and not commands and not artifacts:
        return None
    return {"source": source.strip() if source and source.strip() else None,
            "reproduction": [{"command": command,
                              "cwd": str((cwd or Path.cwd()).resolve())}
                             for command in commands],
            "artifacts": artifacts}
