"""Small formatting helpers shared by the CLI and the TUI."""

from __future__ import annotations

from datetime import datetime, timezone


def format_relative(when: datetime | None, *, now: datetime | None = None) -> str:
    """Compact human age, e.g. ``4m``, ``3h ago``, ``Aug 30``."""
    if when is None:
        return "-"
    now = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    delta = (now - when).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta // 86400)}d ago"
    return when.astimezone().strftime("%b %d")


def format_absolute(when: datetime | None) -> str:
    if when is None:
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"
