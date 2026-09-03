"""Build a synthetic Claude Code session store.

Used by the screenshot tool so the images in the README show a plausible
workspace instead of whoever generated them: no real paths, project names,
prompts or session ids. Everything below is invented.

    from tools.demo_store import build_demo_store
    tmp = build_demo_store()          # sets CLAUDE_CONFIG_DIR
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# (project dir, session title, first prompt, branch, minutes ago, live, remote, kb)
SESSIONS = [
    ("/src/checkout-api", "Refund flow returns 500 on partial refunds",
     "The refund endpoint 500s when the amount is less than the original charge. "
     "Can you find where that path diverges and fix it?",
     "main", 0, True, True, 42_000),
    ("/src/checkout-api", "Add idempotency keys to the payments endpoint",
     "We need idempotency keys on POST /payments so a retried request cannot "
     "double-charge.", "feature/idempotency", 2, True, True, 18_400),
    ("/src/dashboard", "Rewrite the metrics panel with server components",
     "Move the metrics panel to server components and drop the client-side "
     "fetch waterfall.", "main", 4, True, True, 96_800),
    ("/src/dashboard", "Dark mode contrast audit",
     "Audit every surface in dark mode for contrast and list anything under "
     "4.5:1.", "feature/theming", 19, False, False, 7_300),
    ("/src/ingest-pipeline", "Backfill job runs out of memory at 10M rows",
     "The nightly backfill OOMs somewhere past ten million rows. Find out why "
     "it is holding everything in memory.", "main", 26, True, False, 132_500),
    ("/src/ingest-pipeline", "Parquet writer drops null timestamps",
     "Null timestamps vanish when we write parquet. Reproduce it and fix.",
     "main", 71, False, False, 21_900),
    ("/src/cli-tools", "Ship the release script as a single binary",
     "Package the release script so it does not need a python install on the "
     "build agents.", "main", 128, False, False, 15_600),
    ("/src/cli-tools", "Flaky test in the retry suite",
     "test_retry_backoff fails about one run in twenty on CI. Track it down.",
     "fix/flaky-retry", 240, False, False, 4_800),
    ("/src/docs-site", "Restructure the getting-started guide",
     "The getting-started guide buries installation under three headings. "
     "Restructure it so the first thing you see is how to install.",
     "main", 400, False, False, 9_100),
    ("/src/docs-site", "Broken anchor links after the URL change",
     "Every deep link in the docs broke when we changed the URL scheme.",
     "main", 1_500, False, False, 2_400),
    ("/src/sandbox", "Spike: replace the scheduler with a work queue",
     "Rough out what it would take to replace the cron scheduler with a proper "
     "work queue.", "spike/work-queue", 2_900, False, False, 33_700),
    ("/src/sandbox", "Compare three diffing libraries",
     "Benchmark three diffing libraries on our largest documents and recommend "
     "one.", "", 5_800, False, False, 1_200),
]

# Deterministic, obviously-fake ids.
def _sid(n: int) -> str:
    h = f"{n:08x}"
    return f"{h}-1111-4222-8333-444455556666"


def _encode(path: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def build_demo_store() -> Path:
    """Create the store, point CLAUDE_CONFIG_DIR at it, return its path."""
    root = Path(tempfile.mkdtemp(prefix="ccm-demo-"))
    (root / "projects").mkdir()
    (root / "sessions").mkdir()
    now = datetime.now(timezone.utc)

    for i, (cwd, title, prompt, branch, mins, live, remote, kb) in enumerate(SESSIONS):
        sid = _sid(i + 1)
        pdir = root / "projects" / _encode(cwd)
        pdir.mkdir(exist_ok=True)
        when = now - timedelta(minutes=mins)
        stamp = when.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        common = {"sessionId": sid, "cwd": cwd, "gitBranch": branch,
                  "version": "2.1.258", "isSidechain": False}
        records = [
            {"type": "user", "origin": {"kind": "human"}, "timestamp": stamp,
             "message": {"role": "user", "content": prompt}, **common},
            {"type": "assistant", "timestamp": stamp,
             "message": {"role": "assistant", "model": "claude-opus-5",
                         "content": [{"type": "text", "text": "Working on it."}]},
             **common},
            {"type": "permission-mode", "permissionMode": "bypassPermissions",
             "sessionId": sid},
            {"type": "ai-title", "aiTitle": title, "sessionId": sid},
        ]

        path = pdir / f"{sid}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
            # Pad so the size column shows a realistic spread.
            pad = {"type": "attachment", "timestamp": stamp, "sessionId": sid,
                   "attachment": {"note": "x" * 200}}
            line = json.dumps(pad) + "\n"
            for _ in range(max(0, (kb * 1024) // len(line))):
                fh.write(line)
            # Repeat the records the tail scan reads. Padding pushes the
            # originals out of the window, which left model and permission
            # mode showing as unknown.
            for rec in (records[1], records[2], records[3]):
                fh.write(json.dumps(rec) + "\n")

        os.utime(path, (when.timestamp(), when.timestamp()))

        if live:
            pid = 30000 + i
            entry = {"pid": pid, "sessionId": sid, "cwd": cwd,
                     "name": title[:28], "nameSource": "user",
                     "status": "idle", "kind": "interactive",
                     "version": "2.1.258",
                     "updatedAt": int(when.timestamp() * 1000),
                     "statusUpdatedAt": int(when.timestamp() * 1000)}
            if remote:
                entry["bridgeSessionId"] = f"session_demo{i:02d}"
            (root / "sessions" / f"{pid}.json").write_text(
                json.dumps(entry, indent=2), encoding="utf-8")

    os.environ["CLAUDE_CONFIG_DIR"] = str(root)
    return root


def fake_liveness() -> None:
    """Report the demo registry's pids as live claude processes.

    Liveness normally requires a real running process, which a fabricated
    store cannot have; without this every demo session would render as
    crashed.
    """
    from cc_manager import registry

    registry._running_pids = lambda: {pid: "claude.exe"
                                      for pid in range(30000, 30100)}
