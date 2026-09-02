"""Extract session metadata from ``.jsonl`` transcripts.

Transcripts are append-only JSON Lines.  They also get *big* -- half a
gigabyte is not unusual -- so nothing here ever reads a whole file.  Every
session is summarised from two bounded reads:

* a **head scan** that stops as soon as the first human prompt is found
  (normally within the first few kilobytes), and
* a **tail scan** of the last few hundred kilobytes, which carries the most
  recent timestamp, branch, and title records.

Both are tolerant of a half-written final line, which is exactly what a power
cut leaves behind -- and is itself recorded as evidence the session died
uncleanly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Bounded read budgets.
_HEAD_BUDGET = 2 * 1024 * 1024          # give up looking for a first prompt
_HEAD_CHUNK = 128 * 1024
_TAIL_WINDOWS = (256 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024, 32 * 1024 * 1024)

# Records that look like user turns but are machinery, not something typed.
_NOISE_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "<system-reminder>", "<user-prompt-submit-hook>",
    "<task-notification>",
    "Caveat: The messages below",
    "[Request interrupted",
    "This session is being continued from a previous",
)

# A pasted image renders as a bare "[Image: ...]" marker; on its own it is not
# a summary of anything, so it is stripped before a prompt is considered.
_IMAGE_MARKER = re.compile(r"^\s*\[Image:[^\]]*\]\s*")

_WS = re.compile(r"\s+")
_FENCE = re.compile(r"```.*?```", re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class EndState:
    """How a session's transcript ends."""

    LIVE = "live"                # process is running right now
    CRASHED = "crashed"          # died without cleaning up / truncated write
    INTERRUPTED = "interrupted"  # ends mid-turn: prompt or tool never answered
    CLEAN = "clean"


@dataclass
class SessionMeta:
    """Everything the UI needs about one session."""

    session_id: str
    path: Path
    project_dir: str
    size: int = 0
    mtime: float = 0.0

    last_activity: datetime | None = None
    first_prompt: str | None = None
    ai_title: str | None = None
    custom_title: str | None = None
    git_branch: str | None = None
    cwd: str | None = None
    version: str | None = None

    # Set by the scanner: is the session's cwd inside a git work tree at all?
    in_git_repo: bool | None = None

    truncated: bool = False
    dangling_turn: bool = False

    # Filled in by the scanner from the live-session registry.
    live_name: str | None = None
    live_status: str | None = None
    live_pid: int | None = None
    is_live: bool = False
    stale_registry: bool = False
    bridge_session_id: str | None = None

    # Filled in from cc-manager's own state.
    parked: bool = False
    park_note: str | None = None

    errors: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- derived

    @property
    def end_state(self) -> str:
        if self.is_live:
            return EndState.LIVE
        if self.truncated or self.stale_registry:
            return EndState.CRASHED
        if self.dangling_turn:
            return EndState.INTERRUPTED
        return EndState.CLEAN

    @property
    def title(self) -> str:
        """Best available human-readable name for the session."""
        for cand in (self.custom_title, self.ai_title, self.live_name):
            if cand and cand.strip():
                return cand.strip()
        if self.first_prompt:
            return summarize(self.first_prompt, max_sentences=1, max_chars=90)
        return f"(no prompt) {self.session_id[:8]}"

    @property
    def summary(self) -> str:
        """One-to-two sentence gist, taken from the first thing you typed."""
        if self.first_prompt:
            return summarize(self.first_prompt, max_sentences=2, max_chars=220)
        return self.title

    @property
    def branch(self) -> str:
        """Branch recorded during the session.

        Claude Code writes the literal ``HEAD`` both for a detached head and
        for a directory that is not a repository at all, so that value only
        becomes "detached" once the scanner confirms a work tree exists.
        """
        b = (self.git_branch or "").strip()
        if b and b != "HEAD":
            return b
        return "detached" if self.in_git_repo else "-"

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def remote_control(self) -> bool:
        """Is this session currently connected to Remote Control?"""
        return bool(self.bridge_session_id)

    @property
    def remote_url(self) -> str | None:
        if not self.bridge_session_id:
            return None
        return f"https://claude.ai/code/{self.bridge_session_id}"


# ---------------------------------------------------------------- text helpers

def summarize(text: str, max_sentences: int = 2, max_chars: int = 220) -> str:
    """Squash a prompt down to a one-line mini-summary."""
    if not text:
        return ""
    cleaned = _FENCE.sub(" ", text)
    cleaned = _WS.sub(" ", cleaned).strip()
    if not cleaned:
        return ""

    parts = _SENTENCE.split(cleaned)
    out = " ".join(parts[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


def extract_text(content: Any) -> str:
    """Pull plain text out of a message ``content`` field.

    Content is either a bare string or a list of typed blocks; only ``text``
    blocks carry something a person actually typed.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    chunks.append(t)
        return "\n".join(chunks)
    return ""


def prompt_text(rec: dict) -> str | None:
    """Return the text of a prompt the user actually typed, else ``None``.

    Newer transcripts tag real input with ``origin.kind == "human"``.  Older
    ones have no ``origin`` at all, so fall back to shape: a user record with
    text content, no tool result, not a subagent, not machinery.
    """
    if rec.get("type") != "user" or rec.get("isSidechain"):
        return None
    if rec.get("toolUseResult") is not None:
        return None

    origin = rec.get("origin")
    if isinstance(origin, dict):
        if origin.get("kind") != "human":
            return None
    elif "origin" in rec and origin is not None:
        return None

    message = rec.get("message")
    if not isinstance(message, dict):
        return None
    text = extract_text(message.get("content")).strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return None

    while True:
        stripped = _IMAGE_MARKER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = text.strip()
    return text or None


def is_human_prompt(rec: dict) -> bool:
    """True if this record is a prompt the user actually typed."""
    return prompt_text(rec) is not None


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ------------------------------------------------------------------ file reads

def _iter_json(lines: Iterable[bytes]) -> Iterable[dict]:
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _head_records(path: Path) -> tuple[list[dict], bool]:
    """Read from the start until the first human prompt, or the budget runs out."""
    records: list[dict] = []
    buf = b""
    read = 0
    found = False
    try:
        with path.open("rb") as fh:
            while read < _HEAD_BUDGET:
                chunk = fh.read(_HEAD_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                buf += chunk
                *complete, buf = buf.split(b"\n")
                for rec in _iter_json(complete):
                    records.append(rec)
                    if is_human_prompt(rec):
                        found = True
                if found:
                    break
            if not found and buf.strip():
                records.extend(_iter_json([buf]))
    except OSError:
        return records, found
    return records, found


def _tail_records(path: Path, size: int) -> tuple[list[dict], bool]:
    """Read the end of the file, growing the window until records appear.

    Returns the parsed records plus a flag saying the *final* line of the file
    is not valid JSON -- i.e. the process was killed mid-write.
    """
    last_line_bad = False
    for window in _TAIL_WINDOWS:
        offset = max(0, size - window)
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                blob = fh.read()
        except OSError:
            return [], False

        lines = blob.split(b"\n")
        if offset > 0 and lines:
            lines = lines[1:]  # first line is a fragment of an earlier record
        nonempty = [ln for ln in lines if ln.strip()]
        if not nonempty:
            if offset == 0:
                return [], False
            continue

        records = list(_iter_json(nonempty))
        try:
            json.loads(nonempty[-1])
        except ValueError:
            last_line_bad = True

        if records or offset == 0:
            return records, last_line_bad
    return [], last_line_bad


def _ends_mid_turn(records: list[dict]) -> bool:
    """Did the transcript stop before the current turn finished?

    Two shapes count: the last conversational record is a prompt that was
    never answered, or an assistant message whose tool call never returned.
    """
    for rec in reversed(records):
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue
        if rec.get("isSidechain"):
            continue
        if kind == "user":
            # A tool result means the assistant's call did come back.
            return rec.get("toolUseResult") is None and is_human_prompt(rec)
        message = rec.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                return any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content
                )
        return False
    return False


# --------------------------------------------------------------------- scanning

@dataclass
class Turn:
    """One conversational turn, for reading back a session's recent output."""

    role: str  # user | assistant | tool
    timestamp: datetime | None
    text: str


def read_recent_turns(path: Path, limit: int = 12) -> list[Turn]:
    """Recent conversation from the end of a transcript.

    Lets you read what a session has been doing when its terminal is not
    available -- the window is gone or frozen, but the transcript is not.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []

    records, _ = _tail_records(path, size)
    turns: list[Turn] = []

    for rec in records:
        if rec.get("isSidechain"):
            continue
        kind = rec.get("type")
        when = parse_ts(rec.get("timestamp"))

        if kind == "user":
            typed = prompt_text(rec)
            if typed:
                turns.append(Turn("user", when, typed))
            continue

        if kind != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        text = extract_text(content).strip()
        if text:
            turns.append(Turn("assistant", when, text))
            continue
        if isinstance(content, list):
            tools = [
                b.get("name") for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")
            ]
            if tools:
                turns.append(Turn("tool", when, "→ " + ", ".join(tools)))

    return turns[-limit:] if limit else turns


def read_session(path: Path, project_dir: str | None = None) -> SessionMeta:
    """Summarise a single transcript using bounded head and tail reads."""
    meta = SessionMeta(
        session_id=path.stem,
        path=path,
        project_dir=project_dir or path.parent.name,
    )
    try:
        st = path.stat()
        meta.size = st.st_size
        meta.mtime = st.st_mtime
    except OSError as exc:
        meta.errors.append(f"stat failed: {exc}")
        return meta

    head, _ = _head_records(path)
    for rec in head:
        if meta.first_prompt is None:
            typed = prompt_text(rec)
            if typed:
                meta.first_prompt = typed
        if meta.cwd is None and isinstance(rec.get("cwd"), str):
            meta.cwd = rec["cwd"]
        if meta.version is None and isinstance(rec.get("version"), str):
            meta.version = rec["version"]
        if meta.git_branch is None and isinstance(rec.get("gitBranch"), str):
            meta.git_branch = rec["gitBranch"]
        if rec.get("type") == "ai-title" and rec.get("aiTitle"):
            meta.ai_title = str(rec["aiTitle"])
        elif rec.get("type") == "custom-title" and rec.get("customTitle"):
            meta.custom_title = str(rec["customTitle"])

    tail, truncated = _tail_records(path, meta.size)
    meta.truncated = truncated

    latest: datetime | None = None
    last_branch_any: str | None = None
    last_branch_named: str | None = None
    for rec in tail:
        ts = parse_ts(rec.get("timestamp"))
        if ts and (latest is None or ts > latest):
            latest = ts
        kind = rec.get("type")
        # Later records win: these are rewritten as the session evolves.
        if kind == "ai-title" and rec.get("aiTitle"):
            meta.ai_title = str(rec["aiTitle"])
        elif kind == "custom-title" and rec.get("customTitle"):
            meta.custom_title = str(rec["customTitle"])
        branch = rec.get("gitBranch")
        if isinstance(branch, str) and branch:
            last_branch_any = branch
            if branch != "HEAD":
                last_branch_named = branch
        # cwd is deliberately not overwritten from the tail: a session that
        # wandered with `cd` must still be resumed in the directory it was
        # started in, or Claude Code files the continuation under a different
        # project hash and the transcript is effectively split in two.
        if meta.cwd is None and isinstance(rec.get("cwd"), str):
            meta.cwd = rec["cwd"]
        if isinstance(rec.get("version"), str):
            meta.version = rec["version"]

    # "HEAD" means git could not name a branch; a real branch seen anywhere in
    # the tail is a more useful answer than that fallback.
    if last_branch_named or last_branch_any:
        meta.git_branch = last_branch_named or last_branch_any

    # mtime is still accurate after a power cut, so it backstops a lost tail.
    meta.last_activity = latest or datetime.fromtimestamp(meta.mtime, timezone.utc)
    meta.dangling_turn = _ends_mid_turn(tail)
    return meta
