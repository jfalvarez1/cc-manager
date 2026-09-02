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

# Per-session state Claude Code injects into the environment of everything it
# spawns.  If cc-manager is itself started from inside a Claude Code session,
# these would be inherited by the session it launches, which then believes it
# is a child of ours:
#
#   CLAUDE_CODE_CHILD_SESSION=1 turns transcript saving OFF -- the new session
#   runs but records nothing -- and the session id, bridge id and messaging
#   token would point at the wrong session entirely.
#
# All of these are process-scoped markers, never user configuration, so
# removing them cannot discard anything deliberately set. Variables a user
# does set (CLAUDE_CONFIG_DIR, ANTHROPIC_API_KEY, ...) are left alone.
INHERITED_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)

# Claude Code also shapes the environment for the *tools* it runs, and those
# settings are wrong for a real interactive terminal:
#
#   NO_COLOR=1 is the no-color.org opt-out. It is set so tool output comes back
#   as clean text instead of ANSI escapes, but inherited into a new session it
#   turns off all syntax highlighting and the coloured status text.
#
#   WT_SESSION / WT_PROFILE_ID identify the Windows Terminal session we were
#   launched from. Passing them to a new terminal hands it a stale identity;
#   the new one should publish its own.
#
# Set CC_MANAGER_KEEP_ENV=1 to inherit the environment verbatim instead.
TOOL_CONTEXT_VARS = (
    "NO_COLOR",
    "WT_SESSION",
    "WT_PROFILE_ID",
)


def clean_env(mode: str | None = None) -> dict[str, str]:
    """The environment a freshly launched session should start from.

    Removes the markers and tool-context settings Claude Code injects into
    everything it spawns; leaves deliberate configuration (CLAUDE_CONFIG_DIR,
    API keys, PATH, ...) untouched.

    For a Windows Terminal launch it also advertises 24-bit colour.  Windows
    Terminal renders truecolor but does not set ``COLORTERM`` itself -- that is
    a Unix convention -- so a program checking it sees only basic colour
    support.  Basic colour is enough for a red/green diff, which is why diffs
    still look right while richer syntax highlighting falls back to plain text.
    Not set for conhost, whose colour support is genuinely more limited.
    """
    env = dict(os.environ)
    if env.get("CC_MANAGER_KEEP_ENV") == "1":
        return env
    for name in INHERITED_SESSION_VARS + TOOL_CONTEXT_VARS:
        env.pop(name, None)
    if mode in ("tab", "tab-here", "wt") and not env.get("COLORTERM"):
        env["COLORTERM"] = "truecolor"
    return env


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

    if meta.is_background:
        # --resume refuses while the job is running; attach is the way in.
        args = ["attach", meta.job_id]
    else:
        args = ["--resume", meta.session_id] + (["--fork-session"] if fork else [])

    if mode in ("tab", "tab-here", "wt"):
        wt = find_wt()
        if wt:
            if mode in ("tab", "tab-here"):
                # -w is what makes this a tab rather than another window.
                # "0" means the most recently used window -- normally the one
                # you are looking at -- while a name gets a dedicated window
                # that every launch joins.
                target = "0" if mode == "tab-here" else WT_WINDOW
                title = (meta.title or meta.short_id)[:40]
                return [wt, "-w", target, "new-tab",
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
        subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         env=clean_env(mode))
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
        proc = subprocess.run(cmd, cwd=workdir, env=clean_env())
    except FileNotFoundError as exc:
        raise LauncherError(f"failed to launch {cmd[0]}: {exc}") from exc
    except KeyboardInterrupt:
        return 130
    return proc.returncode
