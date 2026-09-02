"""Build the full session list: transcripts + liveness + park state.

Every scan re-reads the filesystem from scratch.  Nothing is served from a
cache, so a session that was updated (or abandoned) by another terminal since
the last look is always reflected.  The reads are bounded and run in a thread
pool, which keeps a full rescan fast even across a gigabyte of transcripts.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import park as park_mod
from . import registry
from .parser import SessionMeta, read_session
from .paths import all_project_dirs, project_dir_for, session_files

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def scan(
    project_dirs: list[Path] | None = None,
    *,
    cwd: str | None = None,
    all_projects: bool = False,
    workers: int = 8,
) -> list[SessionMeta]:
    """Scan transcripts and return sessions, most recently active first."""
    if project_dirs is None:
        project_dirs = all_project_dirs() if all_projects else [project_dir_for(cwd)]

    files: list[tuple[Path, str]] = []
    for pdir in project_dirs:
        for f in session_files(pdir):
            files.append((f, pdir.name))

    if not files:
        return []

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(files)))) as pool:
        sessions = list(pool.map(lambda a: read_session(a[0], a[1]), files))

    _merge_registry(sessions)
    _merge_park_state(sessions)
    _merge_git_context(sessions)

    sessions.sort(key=lambda s: s.last_activity or _EPOCH, reverse=True)
    return sessions


def _merge_registry(sessions: list[SessionMeta]) -> None:
    """Mark which sessions are running, and which died without cleaning up."""
    try:
        live = registry.load_live_sessions()
    except Exception:
        return
    for meta in sessions:
        entry = live.get(meta.session_id)
        if entry is None:
            continue
        meta.live_pid = entry.pid
        meta.live_name = entry.name
        meta.live_status = entry.status
        meta.is_live = entry.alive
        meta.stale_registry = entry.crashed
        meta.bridge_session_id = entry.bridge_session_id
        meta.live_kind = entry.kind
        meta.job_id = entry.job_id


def _merge_park_state(sessions: list[SessionMeta]) -> None:
    """Apply the park list, and recognise parks whose sidecar state was lost."""
    state = park_mod.load_state()
    for meta in sessions:
        rec = state.get(meta.session_id)
        if rec is not None:
            meta.parked = True
            meta.park_note = rec.note or None
        elif (meta.custom_title or "").startswith(park_mod.PARK_PREFIX):
            # Title says parked even though our state file does not know it.
            meta.parked = True


@lru_cache(maxsize=256)
def _is_work_tree(cwd: str) -> bool:
    """Is this directory inside a git work tree?

    Walks up looking for ``.git`` rather than shelling out to git: it is a
    handful of stat calls, needs no subprocess per session, and still answers
    the only question we have -- whether a recorded ``HEAD`` means "detached"
    or "not a repository".
    """
    try:
        path = Path(cwd).resolve()
    except OSError:
        return False
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return True
    return False


def _merge_git_context(sessions: list[SessionMeta]) -> None:
    for meta in sessions:
        if meta.cwd:
            meta.in_git_repo = _is_work_tree(meta.cwd)


def orphan_registry_entries() -> list[registry.LiveEntry]:
    """Registry entries left behind by sessions that never shut down."""
    try:
        live = registry.load_live_sessions()
    except Exception:
        return []
    return [e for e in live.values() if e.crashed]
