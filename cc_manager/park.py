"""Parking: shelve a session without losing it.

There is no ``claude --rename`` flag, so renaming is done the way Claude Code
does it internally -- by appending a ``custom-title`` record to the
transcript::

    {"type":"custom-title","customTitle":"...","sessionId":"..."}

The last such record wins, which makes the rename a pure append: no existing
byte in the transcript is ever rewritten.  cc-manager also keeps its own
sidecar state so a park can be undone and the original title restored, and so
parking still works in ``--safe`` mode where transcripts are left untouched.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import state_dir

PARK_PREFIX = "parked-"
_SLUG = re.compile(r"[^a-z0-9]+")


def _state_file() -> Path:
    return state_dir() / "parked.json"


@dataclass
class ParkRecord:
    session_id: str
    parked_at: str
    note: str
    prev_title: str | None
    applied_title: str | None


def load_state() -> dict[str, ParkRecord]:
    path = _state_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}

    out: dict[str, ParkRecord] = {}
    for sid, rec in sessions.items():
        if not isinstance(rec, dict):
            continue
        out[sid] = ParkRecord(
            session_id=sid,
            parked_at=str(rec.get("parked_at") or ""),
            note=str(rec.get("note") or ""),
            prev_title=rec.get("prev_title"),
            applied_title=rec.get("applied_title"),
        )
    return out


def save_state(state: dict[str, ParkRecord]) -> None:
    """Write the park list atomically so a crash cannot corrupt it."""
    payload = {
        "version": 1,
        "sessions": {
            sid: {
                "parked_at": r.parked_at,
                "note": r.note,
                "prev_title": r.prev_title,
                "applied_title": r.applied_title,
            }
            for sid, r in state.items()
        },
    }
    target = _state_file()
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        tmp = Path(tmp_name)
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.replace(target)
        tmp = None
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)


def slugify(text: str, fallback: str) -> str:
    slug = _SLUG.sub("-", (text or "").strip().lower()).strip("-")
    return slug[:48] or fallback


def append_title(path: Path, session_id: str, title: str) -> None:
    """Append a ``custom-title`` record to a transcript.

    If the file does not end in a newline -- which is what a power cut during
    a write leaves behind -- a newline is written first so the damaged record
    is not silently merged with ours.
    """
    line = json.dumps(
        {"type": "custom-title", "customTitle": title, "sessionId": session_id},
        ensure_ascii=False,
    )
    with path.open("r+b") as fh:
        fh.seek(0, 2)
        if fh.tell() > 0:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
        fh.write(line.encode("utf-8") + b"\n")
        fh.flush()


def park(meta, note: str = "", *, write_titles: bool = True) -> str:
    """Park a session.  Returns the title that was applied."""
    state = load_state()
    prev_title = meta.custom_title or meta.ai_title
    base = note.strip() or meta.title
    applied = f"{PARK_PREFIX}{slugify(base, meta.short_id)}"

    if write_titles:
        append_title(meta.path, meta.session_id, applied)

    state[meta.session_id] = ParkRecord(
        session_id=meta.session_id,
        parked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        note=note.strip(),
        prev_title=prev_title,
        applied_title=applied if write_titles else None,
    )
    save_state(state)
    return applied


def unpark(meta, *, write_titles: bool = True) -> str | None:
    """Unpark a session, restoring its previous title.  Returns that title."""
    state = load_state()
    rec = state.pop(meta.session_id, None)
    restored = None

    if write_titles:
        restored = (rec.prev_title if rec else None) or meta.ai_title
        if restored:
            append_title(meta.path, meta.session_id, restored)

    save_state(state)
    return restored


def rename(meta, title: str, *, write_titles: bool = True) -> None:
    """Set an explicit title on a session."""
    title = title.strip()
    if not title:
        return
    if write_titles:
        append_title(meta.path, meta.session_id, title)
    # Keep a park record's idea of the current title in sync.
    state = load_state()
    if meta.session_id in state:
        state[meta.session_id].applied_title = title
        save_state(state)
