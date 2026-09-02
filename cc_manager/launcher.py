"""Handing the terminal over to ``claude --resume``."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class LauncherError(RuntimeError):
    pass


def find_claude() -> str:
    """Locate the ``claude`` executable."""
    override = os.environ.get("CC_MANAGER_CLAUDE_BIN")
    if override and (Path(override).is_file() or shutil.which(override)):
        return override

    found = shutil.which("claude")
    if found:
        return found

    for cand in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / ".claude" / "local" / "claude",
    ):
        if cand.is_file():
            return str(cand)

    raise LauncherError(
        "could not find the 'claude' executable on PATH; "
        "set CC_MANAGER_CLAUDE_BIN to its full path"
    )


def resume_command(session_id: str, *, fork: bool = False) -> list[str]:
    cmd = [find_claude(), "--resume", session_id]
    if fork:
        cmd.append("--fork-session")
    return cmd


def resume(meta, *, fork: bool = False) -> int:
    """Run ``claude --resume`` for a session, inheriting this terminal.

    Blocks until the session exits, then returns its exit code.  The child
    runs in the directory the session was originally started in so that
    relative paths and CLAUDE.md discovery behave as they did before.
    """
    cmd = resume_command(meta.session_id, fork=fork)

    workdir = meta.cwd if meta.cwd and Path(meta.cwd).is_dir() else None
    if workdir is None:
        workdir = str(Path.cwd())

    try:
        proc = subprocess.run(cmd, cwd=workdir)
    except FileNotFoundError as exc:
        raise LauncherError(f"failed to launch {cmd[0]}: {exc}") from exc
    except KeyboardInterrupt:
        return 130
    return proc.returncode
