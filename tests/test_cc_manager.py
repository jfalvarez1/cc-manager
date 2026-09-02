"""Self-contained checks for cc-manager.

Builds synthetic transcripts in a temporary CLAUDE_CONFIG_DIR so the crash,
truncation and park paths can be exercised without touching real sessions.

Run with:  python tests/test_cc_manager.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


# --------------------------------------------------------------- fixture setup

def ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def user_prompt(text: str, *, sid: str, branch="main", cwd="/proj", legacy=False):
    rec = {
        "type": "user",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        "timestamp": ts(10),
        "sessionId": sid,
        "cwd": cwd,
        "gitBranch": branch,
        "version": "2.1.251",
    }
    if not legacy:
        rec["origin"] = {"kind": "human"}
    return rec


def tool_result(sid: str):
    return {
        "type": "user",
        "isSidechain": False,
        "origin": None,
        "toolUseResult": {"stdout": "x"},
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        "timestamp": ts(9),
        "sessionId": sid,
        "gitBranch": "main",
    }


def assistant(text: str, sid: str, *, tool_use=False):
    content = ([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]
               if tool_use else [{"type": "text", "text": text}])
    return {
        "type": "assistant",
        "isSidechain": False,
        "message": {"role": "assistant", "content": content},
        "timestamp": ts(8),
        "sessionId": sid,
        "gitBranch": "main",
    }


def write_session(pdir: Path, sid: str, records: list[dict], *, truncate=False) -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        if truncate:
            # Exactly what a power cut mid-append leaves: half a JSON object.
            fh.write('{"type":"assistant","message":{"role":"assist')
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ccmgr-test-"))
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)

    from cc_manager import park as park_mod
    from cc_manager.parser import EndState, read_session, summarize
    from cc_manager.paths import encode_cwd, project_dir_for
    from cc_manager.scanner import scan

    pdir = tmp / "projects" / "-proj"

    try:
        print("\n[paths]")
        check("windows cwd encoding", encode_cwd(r"C:\Users\alice"), "C--Users-alice")
        check("nested cwd encoding",
              encode_cwd(r"C:\Users\alice\OneDrive\Desktop"),
              "C--Users-alice-OneDrive-Desktop")
        check("posix cwd encoding", encode_cwd("/home/me/src/app"), "-home-me-src-app")
        check("spaces and dots",
              encode_cwd(r"G:\Program Files\Snap-on Inc"),
              "G--Program-Files-Snap-on-Inc")

        print("\n[summaries]")
        check("two sentence cap",
              summarize("First one. Second one. Third one.", max_sentences=2),
              "First one. Second one.")
        check("whitespace collapse", summarize("a\n\n  b"), "a b")
        check("code fence stripped",
              summarize("do this ```py\nx=1\n``` now"), "do this now")

        print("\n[end states]")
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        write_session(pdir, sid, [
            user_prompt("Refactor the parser please. It is slow.", sid=sid),
            assistant("done", sid),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("clean end", m.end_state, EndState.CLEAN)
        check("first prompt", m.first_prompt, "Refactor the parser please. It is slow.")
        check("summary is 2 sentences", m.summary,
              "Refactor the parser please. It is slow.")
        check("branch", m.branch, "main")

        sid = "aaaaaaaa-0000-0000-0000-000000000002"
        write_session(pdir, sid, [user_prompt("hi", sid=sid), assistant("y", sid)],
                      truncate=True)
        m = read_session(pdir / f"{sid}.jsonl")
        check("truncated tail detected", m.truncated, True)
        check("truncated => crashed", m.end_state, EndState.CRASHED)

        sid = "aaaaaaaa-0000-0000-0000-000000000003"
        write_session(pdir, sid, [
            user_prompt("run the build", sid=sid),
            assistant("", sid, tool_use=True),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("tool call never returned", m.end_state, EndState.INTERRUPTED)

        sid = "aaaaaaaa-0000-0000-0000-000000000004"
        write_session(pdir, sid, [
            user_prompt("first", sid=sid),
            assistant("ok", sid),
            user_prompt("never answered", sid=sid),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("prompt never answered", m.end_state, EndState.INTERRUPTED)

        sid = "aaaaaaaa-0000-0000-0000-000000000005"
        write_session(pdir, sid, [
            user_prompt("built it", sid=sid),
            assistant("", sid, tool_use=True),
            tool_result(sid),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("tool result closes the turn", m.end_state, EndState.CLEAN)

        print("\n[noise filtering]")
        sid = "aaaaaaaa-0000-0000-0000-000000000006"
        write_session(pdir, sid, [
            user_prompt("<local-command-caveat>Caveat: blah", sid=sid),
            user_prompt("<command-name>/clear</command-name>", sid=sid),
            user_prompt("<task-notification>agent done", sid=sid),
            user_prompt("[Image: original 2560x1440, displayed 1000x800]", sid=sid),
            user_prompt("[Image: x] the real question", sid=sid),
            assistant("ok", sid),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("machinery skipped", m.first_prompt, "the real question")

        sid = "aaaaaaaa-0000-0000-0000-000000000007"
        write_session(pdir, sid, [
            tool_result(sid),
            user_prompt("legacy transcript prompt", sid=sid, legacy=True),
            assistant("ok", sid),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("legacy record without origin", m.first_prompt, "legacy transcript prompt")

        print("\n[titles]")
        sid = "aaaaaaaa-0000-0000-0000-000000000008"
        write_session(pdir, sid, [
            user_prompt("do a thing", sid=sid),
            {"type": "ai-title", "aiTitle": "Old title", "sessionId": sid},
            assistant("ok", sid),
            {"type": "ai-title", "aiTitle": "Newer title", "sessionId": sid},
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("last ai-title wins", m.title, "Newer title")

        write_session(pdir, sid, [
            user_prompt("do a thing", sid=sid),
            {"type": "ai-title", "aiTitle": "Auto", "sessionId": sid},
            {"type": "custom-title", "customTitle": "Mine", "sessionId": sid},
            assistant("ok", sid),
        ])
        m = read_session(pdir / f"{sid}.jsonl")
        check("custom-title beats ai-title", m.title, "Mine")

        print("\n[park / rename]")
        m = read_session(pdir / f"{sid}.jsonl")
        applied = park_mod.park(m, "hiking trip")
        check("park title", applied, "parked-hiking-trip")
        again = read_session(pdir / f"{sid}.jsonl")
        check("park visible in transcript", again.title, "parked-hiking-trip")
        check("park state recorded", sid in park_mod.load_state(), True)

        restored = park_mod.unpark(again)
        check("unpark restores title", restored, "Mine")
        check("park state cleared", sid in park_mod.load_state(), False)
        check("transcript title restored",
              read_session(pdir / f"{sid}.jsonl").title, "Mine")

        park_mod.rename(read_session(pdir / f"{sid}.jsonl"), "Hand named")
        check("rename applied", read_session(pdir / f"{sid}.jsonl").title, "Hand named")

        # Every line must still be valid JSON after all that appending.
        lines = [l for l in (pdir / f"{sid}.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()]
        ok = all(isinstance(json.loads(l), dict) for l in lines)
        check("transcript still fully parseable", ok, True)

        print("\n[append onto a truncated file]")
        sid = "aaaaaaaa-0000-0000-0000-000000000009"
        p = write_session(pdir, sid, [user_prompt("x", sid=sid)], truncate=True)
        before = len([l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])
        park_mod.append_title(p, sid, "recovered")
        after = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("damaged line not merged", len(after), before + 1)
        check("appended record parses", json.loads(after[-1])["customTitle"], "recovered")

        print("\n[scan]")
        sessions = scan([pdir])
        check("all sessions found", len(sessions), 9)
        order = [s.last_activity for s in sessions]
        check("sorted newest first", order == sorted(order, reverse=True), True)
        check("project dir resolves", project_dir_for("/proj").name, "-proj")

        print("\n[empty + damaged inputs]")
        broken = pdir / "bbbbbbbb-0000-0000-0000-000000000001.jsonl"
        broken.write_text("not json at all\n", encoding="utf-8")
        m = read_session(broken)
        check("garbage file does not raise", m.session_id[:8], "bbbbbbbb")
        check("garbage falls back to mtime", m.last_activity is not None, True)

        empty = pdir / "cccccccc-0000-0000-0000-000000000001.jsonl"
        empty.write_text("", encoding="utf-8")
        m = read_session(empty)
        check("empty file handled", m.title.startswith("(no prompt)"), True)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
