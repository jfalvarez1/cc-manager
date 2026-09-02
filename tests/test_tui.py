"""Headless checks that the TUI mounts, lists, filters and switches views.

Run with:  python tests/test_tui.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_manager.app import VIEW_PARKED, CCManager  # noqa: E402
from textual.widgets import DataTable, Input, Static  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


async def settle(pilot, tries: int = 60) -> None:
    """Wait for the background scan worker to populate the table."""
    for _ in range(tries):
        await pilot.pause()
        if pilot.app.sessions:
            await pilot.pause()
            return
        await asyncio.sleep(0.05)


async def run() -> None:
    app = CCManager(all_projects=True, safe=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot)

        table = app.query_one("#table", DataTable)
        print("\n[mount]")
        check("sessions loaded", len(app.sessions) > 0, True)
        check("rows rendered", table.row_count == len(app.filtered), True)
        check("columns", len(table.columns), 5)

        print("\n[detail pane]")
        current = app.current()
        check("a row is selected", current is not None, True)
        detail = str(app.query_one("#detail", Static).content)
        check("detail shows the selected session", current.short_id in detail, True)

        print("\n[navigation]")
        first = app.current().session_id
        await pilot.press("down")
        await pilot.pause()
        check("arrow key moves selection", app.current().session_id != first, True)

        print("\n[search]")
        app.query_one("#search", Input).value = "zzzz-no-such-session"
        await pilot.pause()
        check("nonsense filters everything out", len(app.filtered), 0)

        target = app.sessions[0].short_id
        app.query_one("#search", Input).value = target
        await pilot.pause()
        check("search by id finds it", len(app.filtered) >= 1, True)

        app.query_one("#search", Input).value = ""
        await pilot.pause()
        check("clearing restores list", len(app.filtered), len(app.sessions))

        print("\n[typing in search must not fire hotkeys]")
        await pilot.press("slash")
        await pilot.pause()
        check("slash focuses search", app.query_one("#search", Input).has_focus, True)
        for key in ("p", "a", "r", "q", "n", "v", "f", "c"):
            await pilot.press(key)
        await pilot.pause()
        check("keys typed, no modal opened", len(app.screen_stack), 1)
        check("search captured the text", app.query_one("#search", Input).value, "parqnvfc")
        check("app still running", app.is_running, True)
        await pilot.press("escape")
        await pilot.pause()
        check("escape clears and leaves search", app.query_one("#search", Input).value, "")

        print("\n[views]")
        app.view = VIEW_PARKED
        app.refresh_table()
        await pilot.pause()
        parked = sum(1 for s in app.sessions if s.parked)
        check("parked view shows only parked", len(app.filtered), parked)

        await pilot.press("v")
        await pilot.pause()
        check("v cycles the view", app.view != VIEW_PARKED, True)

        print("\n[modals]")
        await pilot.press("n")
        await pilot.pause()
        check("rename modal opens", len(app.screen_stack) > 1, True)
        await pilot.press("escape")
        await pilot.pause()
        check("escape closes modal", len(app.screen_stack), 1)

        await pilot.press("p")
        await pilot.pause()
        check("park modal opens", len(app.screen_stack) > 1, True)
        await pilot.press("escape")
        await pilot.pause()

        print("\n[rescan]")
        await pilot.press("r")
        await settle(pilot)
        check("rescan repopulates", len(app.sessions) > 0, True)


def main() -> int:
    asyncio.run(run())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
