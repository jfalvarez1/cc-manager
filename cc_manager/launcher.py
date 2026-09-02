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


# Name of the Windows Terminal window that "open in a tab" targets.  Any
# launch naming the same window joins it instead of creating a new one.
WT_WINDOW = "cc-manager"


def find_wt() -> str | None:
    """Locate Windows Terminal, which is often only an App Execution Alias."""
    found = shutil.which("wt")
    if found:
        return found
    alias = (Path(os.environ.get("LOCALAPPDATA", ""))
             / "Microsoft" / "WindowsApps" / "wt.exe")
    return str(alias) if alias.exists() else None


def _ps_quote(value: str) -> str:
    """Quote for a PowerShell single-quoted string."""
    return "'" + value.replace("'", "''") + "'"


def terminal_command(meta, *, fork: bool = False, mode: str = "wt") -> list[str]:
    """Build a command that opens a NEW terminal window running --resume.

    ``mode`` picks the host:

    * ``tab``      -- a new tab in one shared, named Windows Terminal window,
                      so opening ten sessions gives ten tabs rather than ten
                      windows.  Targeting the window by *name* means the first
                      launch creates it and every later one joins it.
    * ``wt``       -- a separate Windows Terminal window each time.  Note that
                      Windows Terminal is single-instance by default, so these
                      windows still share one process: if it wedges, they all
                      appear to hang at once.
    * ``isolated`` -- a private conhost window per session.  Plainer, but one
                      frozen window cannot take the others down with it.
    * ``pwsh``     -- PowerShell 7 in its own window.
    """
    claude = find_claude()
    workdir = meta.cwd if meta.cwd and Path(meta.cwd).is_dir() else str(Path.cwd())
    args = ["--resume", meta.session_id] + (["--fork-session"] if fork else [])

    if mode in ("tab", "wt"):
        wt = find_wt()
        if wt:
            if mode == "tab":
                # --title keeps the tab readable; -w <name> is what makes this
                # a tab rather than another window.
                title = (meta.title or meta.short_id)[:40]
                return [wt, "-w", WT_WINDOW, "new-tab",
                        "--title", title, "-d", workdir, claude, *args]
            return [wt, "-d", workdir, claude, *args]
        mode = "isolated"

    inner = (
        f"Set-Location -LiteralPath {_ps_quote(workdir)}; "
        f"& {_ps_quote(claude)} " + " ".join(_ps_quote(a) for a in args)
    )
    if mode == "pwsh":
        shell = shutil.which("pwsh") or "powershell.exe"
        return [shell, "-NoLogo", "-NoExit", "-Command", inner]
    return ["conhost.exe", "powershell.exe", "-NoLogo", "-NoExit", "-Command", inner]


def spawn_terminal(meta, *, fork: bool = False, mode: str = "wt"):
    """Open a session in its own terminal window and return immediately."""
    cmd = terminal_command(meta, fork=fork, mode=mode)
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    except OSError as exc:
        raise LauncherError(f"could not open a terminal: {exc}") from exc
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
