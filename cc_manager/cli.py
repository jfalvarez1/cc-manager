"""Command line entry point for cc-manager."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .launcher import LauncherError, resume
from .parser import EndState
from .paths import all_project_dirs, project_dir_for
from .scanner import orphan_registry_entries, scan
from .util import format_absolute, format_relative, format_size

_STATE_MARK = {
    EndState.LIVE: "live",
    EndState.CRASHED: "crashed",
    EndState.INTERRUPTED: "interrupted",
    EndState.CLEAN: "ok",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cc-manager",
        description="Browse, search, park and resume Claude Code sessions.",
    )
    p.add_argument("--version", action="version", version=f"cc-manager {__version__}")
    p.add_argument("-a", "--all", action="store_true",
                   help="include sessions from every project, not just this directory")
    p.add_argument("-C", "--cwd", metavar="PATH",
                   help="treat PATH as the project directory instead of the real CWD")
    p.add_argument("--safe", action="store_true",
                   help="never append title records to transcripts; park in sidecar state only")
    p.add_argument("-g", "--gui", action="store_true",
                   help="open the desktop window instead of the terminal UI")
    p.add_argument("-l", "--list", action="store_true",
                   help="print sessions and exit instead of opening the TUI")
    p.add_argument("--json", action="store_true",
                   help="print session metadata as JSON and exit")
    p.add_argument("--resume-last", action="store_true",
                   help="resume the most recently active session and exit")
    p.add_argument("--tail", metavar="SESSION",
                   help="print the recent conversation of a session (id or id prefix) "
                        "and exit; useful when its terminal is gone or frozen")
    p.add_argument("-n", "--lines", type=int, default=12, metavar="N",
                   help="how many turns --tail should show (default 12)")
    p.add_argument("--doctor", action="store_true",
                   help="report sessions that ended uncleanly")
    p.add_argument("--prune-stale", action="store_true",
                   help="with --doctor, delete leftover registry files for dead sessions")
    return p


def _dirs(args) -> list[Path]:
    return all_project_dirs() if args.all else [project_dir_for(args.cwd)]


def _print_list(sessions) -> None:
    if not sessions:
        print("no sessions found")
        return
    print(f"{'STATE':<12} {'BRANCH':<18} {'WHEN':<12} {'SIZE':>8}  {'ID':<9} SUMMARY")
    for m in sessions:
        mark = _STATE_MARK[m.end_state] + ("/parked" if m.parked else "")
        print(
            f"{mark:<12} {m.branch[:18]:<18} "
            f"{format_relative(m.last_activity):<12} {format_size(m.size):>8}  "
            f"{m.short_id:<9} {m.title[:70]}"
        )


def _print_json(sessions) -> None:
    payload = [
        {
            "session_id": m.session_id,
            "path": str(m.path),
            "project": m.project_dir,
            "cwd": m.cwd,
            "branch": m.branch,
            "title": m.title,
            "summary": m.summary,
            "first_prompt": m.first_prompt,
            "last_activity": format_absolute(m.last_activity),
            "size": m.size,
            "state": m.end_state,
            "live": m.is_live,
            "pid": m.live_pid,
            "parked": m.parked,
            "version": m.version,
            "model": m.model,
            "permission_mode": m.permission_mode,
            "background": m.is_background,
            "resume_command": m.resume_command,
        }
        for m in sessions
    ]
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _tail(sessions, needle: str, count: int) -> int:
    needle = needle.strip().lower()
    matches = [m for m in sessions if m.session_id.lower().startswith(needle)]
    if not matches:
        matches = [m for m in sessions if needle in (m.title or "").lower()]
    if not matches:
        print(f"no session matching {needle!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{needle!r} matches {len(matches)} sessions:", file=sys.stderr)
        for m in matches:
            print(f"  {m.short_id}  {m.title[:60]}", file=sys.stderr)
        return 1

    from .parser import read_recent_turns

    target = matches[0]
    live = " (running now)" if target.is_live else ""
    print(f"{target.title}  [{target.short_id}]{live}")
    print(f"{target.cwd or '?'}\n")

    turns = read_recent_turns(target.path, count)
    if not turns:
        print("(no readable conversation at the end of this transcript)")
        return 0

    label = {"user": ">>> you", "assistant": "claude", "tool": "  tool"}
    for turn in turns:
        stamp = turn.timestamp.astimezone().strftime("%H:%M:%S") if turn.timestamp else "--:--:--"
        body = " ".join(turn.text.split())
        if len(body) > 400:
            body = body[:399] + "…"
        print(f"[{stamp}] {label.get(turn.role, turn.role)}: {body}")
    return 0


def _doctor(sessions, prune: bool) -> int:
    bad = [m for m in sessions if m.end_state in (EndState.CRASHED, EndState.INTERRUPTED)]
    if not bad:
        print("all sessions closed cleanly")
    for m in bad:
        why = []
        if m.truncated:
            why.append("transcript ends mid-write")
        if m.stale_registry:
            why.append(f"registry entry left by dead pid {m.live_pid}")
        if m.dangling_turn:
            why.append("stopped mid-turn")
        print(f"{_STATE_MARK[m.end_state]:<12} {m.short_id}  {m.title[:60]}")
        print(f"{'':<12} {format_absolute(m.last_activity)} - {'; '.join(why) or 'unknown'}")
        print(f"{'':<12} resume with: claude --resume {m.session_id}")

    orphans = orphan_registry_entries()
    if orphans:
        print(f"\n{len(orphans)} orphaned registry file(s) in ~/.claude/sessions:")
        for e in orphans:
            print(f"  {e.path.name}  pid {e.pid}  {e.session_id[:8]}  {e.name or ''}")
        if prune:
            from .registry import load_live_sessions, prune_stale
            removed = prune_stale(load_live_sessions())
            print(f"removed {len(removed)} orphaned registry file(s)")
        else:
            print("re-run with --prune-stale to remove them")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Titles routinely contain non-ASCII; piping into another command drops to
    # the locale encoding on Windows and would mangle or crash on them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    # The GUI scans on its own thread, so skip the blocking scan below.
    if args.gui:
        from .gui import main as gui_main
        return gui_main()

    sessions = scan(_dirs(args))

    if args.json:
        _print_json(sessions)
        return 0
    if args.tail:
        return _tail(sessions, args.tail, args.lines)
    if args.doctor:
        return _doctor(sessions, args.prune_stale)
    if args.list:
        _print_list(sessions)
        return 0
    if args.resume_last:
        if not sessions:
            print("no sessions found", file=sys.stderr)
            return 1
        target = sessions[0]
        print(f"resuming {target.short_id} — {target.title}")
        try:
            return resume(target)
        except LauncherError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    from .app import CCManager
    CCManager(all_projects=args.all, safe=args.safe, cwd=args.cwd).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
