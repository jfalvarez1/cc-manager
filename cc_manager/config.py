"""Reading and writing Claude Code's own user settings.

Only a small, explicit set of keys is touched.  Every write backs the file up
first and is atomic, because this file also carries permissions and hooks --
corrupting it would break every session on the machine.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .paths import claude_home

# Keys the UI is allowed to toggle, with a human label and a one-line
# explanation.  Anything not listed here is never written.
TOGGLES = {
    "remoteControlAtStartup": (
        "Remote Control on every new session",
        "Auto-connect each interactive session to claude.ai so you can drive "
        "it from your phone or browser.",
    ),
    "alwaysThinkingEnabled": (
        "Always thinking",
        "Let Claude think before every response.",
    ),
}


def settings_path() -> Path:
    return claude_home() / "settings.json"


def load() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str):
    return load().get(key)


def set_key(key: str, value) -> None:
    """Set one key in settings.json, preserving everything else.

    Refuses keys outside TOGGLES so a UI bug cannot rewrite permissions or
    hooks.  Backs up, writes to a temp file, then replaces -- an interrupted
    write leaves the original intact.
    """
    if key not in TOGGLES:
        raise KeyError(f"{key!r} is not a settings key cc-manager may change")

    path = settings_path()
    data = load()
    if data.get(key) == value:
        return
    data[key] = value

    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, path.with_suffix(f".json.bak-{stamp}"))
        except OSError:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        tmp = Path(name)
        with open(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        tmp.replace(path)
        tmp = None
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink(missing_ok=True)


def toggle(key: str) -> bool:
    """Flip a boolean setting and return the new value."""
    new = not bool(get(key))
    set_key(key, new)
    return new
