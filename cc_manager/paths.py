"""Locating Claude Code's on-disk session storage.

Claude Code stores each CLI session as a ``.jsonl`` transcript under::

    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

The directory name is the session's working directory with every character
that is not ``[A-Za-z0-9]`` replaced by ``-``.  For example::

    C:\\Users\\alice              -> C--Users-alice
    C:\\Users\\alice\\src\\app      -> C--Users-alice-src-app
    /home/alice/src/app          -> -home-alice-src-app

The encoding is lossy (``-`` and ``\\`` and ``:`` all collapse to ``-``), so it
can be computed but never reliably reversed.  To display a real path we read
the ``cwd`` field recorded inside the transcript instead.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def claude_home() -> Path:
    """Root of the Claude Code config directory (honours CLAUDE_CONFIG_DIR)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def projects_dir() -> Path:
    """Directory holding one subdirectory of transcripts per project."""
    return claude_home() / "projects"


def registry_dir() -> Path:
    """Directory of ``<pid>.json`` files describing running sessions."""
    return claude_home() / "sessions"


def state_dir() -> Path:
    """Where cc-manager keeps its own state (park list, cache)."""
    d = claude_home() / "cc-manager"
    d.mkdir(parents=True, exist_ok=True)
    return d


def encode_cwd(cwd: str | os.PathLike[str]) -> str:
    """Encode a working directory the way Claude Code names its project dirs."""
    return _NON_ALNUM.sub("-", str(cwd))


def project_dir_for(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Return the transcript directory for ``cwd`` (defaults to the real CWD).

    Tries the exact encoding first, then a case-insensitive match against the
    directories that actually exist -- drive-letter case differs between
    shells on Windows (``c:\\users`` vs ``C:\\Users``) and would otherwise
    produce a second, empty project directory.
    """
    raw = Path(cwd) if cwd is not None else Path.cwd()
    root = projects_dir()

    # Try the resolved path first, then the literal one; either may be what
    # Claude Code saw depending on how the shell spelled it.
    candidates: list[str] = []
    try:
        candidates.append(str(raw.resolve()))
    except OSError:
        pass
    candidates.append(str(raw))

    for cand in candidates:
        encoded = encode_cwd(cand)
        exact = root / encoded
        if exact.is_dir():
            return exact

    if root.is_dir():
        wanted = {encode_cwd(c).lower() for c in candidates}
        for entry in root.iterdir():
            if entry.is_dir() and entry.name.lower() in wanted:
                return entry

    # Nothing on disk yet; return the canonical name so callers can report it.
    return root / encode_cwd(candidates[0])


def all_project_dirs() -> list[Path]:
    """Every project directory that currently holds at least one transcript."""
    root = projects_dir()
    if not root.is_dir():
        return []
    out = [d for d in root.iterdir() if d.is_dir() and any(d.glob("*.jsonl"))]
    return sorted(out, key=lambda d: d.name.lower())


def session_files(project: Path) -> list[Path]:
    """Transcripts in one project directory, newest first."""
    if not project.is_dir():
        return []
    files = list(project.glob("*.jsonl"))
    files.sort(key=lambda p: _safe_mtime(p), reverse=True)
    return files


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0
