"""Which sessions are actually running right now?

Claude Code writes ``~/.claude/sessions/<pid>.json`` for every live session and
deletes it on a clean exit.  That gives us two useful facts:

* an entry whose process is **alive** -> the session is open right now, so
  resuming it would collide with the running copy;
* an entry whose process is **gone** -> the session died without cleaning up,
  i.e. a crash, a killed terminal, or a power loss.

PIDs get recycled, so "alive" additionally requires that the process really is
a ``claude`` process rather than whatever inherited the number later.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import registry_dir


@dataclass(frozen=True)
class LiveEntry:
    """One ``<pid>.json`` record from the session registry."""

    pid: int
    session_id: str
    cwd: str | None
    name: str | None
    name_source: str | None
    status: str | None       # idle | busy | shell | ...
    kind: str | None         # interactive | bg | ...
    version: str | None
    # Present only while the session is connected to Remote Control; it is the
    # id of the matching session at claude.ai/code.
    bridge_session_id: str | None
    updated_at: float | None  # epoch seconds
    alive: bool
    path: Path

    @property
    def crashed(self) -> bool:
        """Registry entry left behind by a session that never shut down."""
        return not self.alive


def _running_pids() -> dict[int, str] | None:
    """Map pid -> process image name, or None if we cannot enumerate."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    procs: dict[int, str] = {}
    for line in out.splitlines():
        # "image.exe","1234","Console","1","12,345 K"
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            procs[int(parts[1])] = parts[0].strip('"').lower()
        except ValueError:
            continue
    return procs


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def load_live_sessions() -> dict[str, LiveEntry]:
    """Read the registry and resolve liveness.  Keyed by session id.

    Enumerating processes costs one subprocess call for the whole registry
    rather than one per session.
    """
    reg = registry_dir()
    if not reg.is_dir():
        return {}

    procs = _running_pids()
    out: dict[str, LiveEntry] = {}

    for f in sorted(reg.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId")
        pid = data.get("pid")
        if not sid or not isinstance(pid, int):
            continue

        if procs is None:
            alive = _pid_alive_posix(pid)
        else:
            # Guard against PID reuse: the number must still be a claude process.
            alive = "claude" in procs.get(pid, "")

        updated = data.get("updatedAt")
        entry = LiveEntry(
            pid=pid,
            session_id=sid,
            cwd=data.get("cwd"),
            name=data.get("name"),
            name_source=data.get("nameSource"),
            status=data.get("status"),
            kind=data.get("kind"),
            version=data.get("version"),
            bridge_session_id=data.get("bridgeSessionId"),
            updated_at=(updated / 1000.0) if isinstance(updated, (int, float)) else None,
            alive=alive,
            path=f,
        )
        # Prefer a live entry if the same session somehow has two records.
        prev = out.get(sid)
        if prev is None or (entry.alive and not prev.alive):
            out[sid] = entry
    return out


def prune_stale(entries: dict[str, LiveEntry]) -> list[Path]:
    """Delete registry files whose process is gone.  Returns removed paths.

    Only ever called explicitly by the user; a stale entry is the evidence
    that a session crashed, so it is not swept up automatically.
    """
    removed: list[Path] = []
    for entry in entries.values():
        if entry.alive:
            continue
        try:
            entry.path.unlink()
            removed.append(entry.path)
        except OSError:
            pass
    return removed
