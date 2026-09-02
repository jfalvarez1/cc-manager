"""Smoke checks for the desktop window and the terminal launch commands.

Builds the real window against the real session store, drives a scan, exercises
filtering and selection, then tears it down -- without ever opening a terminal.

Run with:  python tests/test_gui.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILURES.append(label)


def check_true(label: str, value) -> None:
    check(label, bool(value), True)


def main() -> int:
    from cc_manager.launcher import terminal_command
    from cc_manager.parser import SessionMeta

    print("\n[terminal commands]")
    fake = SessionMeta(
        session_id="1234abcd-0000-0000-0000-000000000000",
        path=Path("x.jsonl"), project_dir="p", cwd=str(Path.home()),
    )

    iso = terminal_command(fake, mode="isolated")
    check("isolated uses conhost", iso[0], "conhost.exe")
    check_true("isolated keeps the window open", "-NoExit" in iso)
    check_true("isolated passes the session id", fake.session_id in " ".join(iso))
    check_true("isolated cds to the session dir", "Set-Location" in " ".join(iso))

    pwsh = terminal_command(fake, mode="pwsh")
    check_true("pwsh mode picks a powershell", "pwsh" in pwsh[0].lower()
               or "powershell" in pwsh[0].lower())

    fork = terminal_command(fake, mode="isolated", fork=True)
    check_true("fork adds --fork-session", "--fork-session" in " ".join(fork))

    # Test the quoting directly: terminal_command deliberately falls back to
    # the real cwd when a session's directory no longer exists, so a fake path
    # never reaches the quoter.
    from cc_manager.launcher import _ps_quote

    check("apostrophe doubled", _ps_quote("C:\\it's a path"), "'C:\\it''s a path'")
    check("plain path wrapped", _ps_quote(r"C:\src"), r"'C:\src'")
    check("empty string safe", _ps_quote(""), "''")

    missing = terminal_command(
        SessionMeta(session_id="s", path=Path("x"), project_dir="p",
                    cwd="C:\\definitely\\not\\here"),
        mode="isolated")
    check_true("vanished cwd falls back to a real directory",
               "definitely" not in " ".join(missing))

    wt = terminal_command(fake, mode="wt")
    check_true("wt mode resolves to something runnable",
               wt[0].endswith("wt.exe") or wt[0] == "conhost.exe")

    tab = terminal_command(fake, mode="tab")
    if tab[0].endswith("wt.exe"):
        check_true("tab mode targets a named window", "-w" in tab)
        check("tab mode joins the cc-manager window",
              tab[tab.index("-w") + 1], "cc-manager")
        check_true("tab mode asks for a new tab", "new-tab" in tab)
    else:
        print("  --   wt.exe unavailable, tab mode fell back to conhost")
        check("fallback is conhost", tab[0], "conhost.exe")

    print("\n[inherited session markers]")
    import os as _os
    import subprocess as _sp
    import tempfile as _tf

    from cc_manager.launcher import INHERITED_SESSION_VARS, clean_env

    saved = {n: _os.environ.get(n) for n in INHERITED_SESSION_VARS}
    try:
        # Pretend cc-manager was started from inside a Claude Code session.
        for name in INHERITED_SESSION_VARS:
            _os.environ[name] = "poison"
        _os.environ["CLAUDE_CONFIG_DIR"] = "keep-me"

        _os.environ["NO_COLOR"] = "1"
        _os.environ["WT_SESSION"] = "stale-terminal-id"

        env = clean_env()
        leaked = [n for n in INHERITED_SESSION_VARS if n in env]
        check("no session markers survive clean_env", leaked, [])
        check("CLAUDE_CODE_CHILD_SESSION removed",
              "CLAUDE_CODE_CHILD_SESSION" in env, False)
        check("NO_COLOR removed", "NO_COLOR" in env, False)
        check("stale WT_SESSION removed", "WT_SESSION" in env, False)
        check("user config preserved", env.get("CLAUDE_CONFIG_DIR"), "keep-me")

        check("truecolor advertised for a WT tab",
              clean_env("tab").get("COLORTERM"), "truecolor")
        check("truecolor advertised for a WT window",
              clean_env("wt").get("COLORTERM"), "truecolor")
        check("conhost is not told it has truecolor",
              clean_env("isolated").get("COLORTERM"), None)

        _os.environ["CC_MANAGER_KEEP_ENV"] = "1"
        check("escape hatch inherits verbatim",
              clean_env().get("CLAUDE_CODE_CHILD_SESSION"), "poison")
        _os.environ.pop("CC_MANAGER_KEEP_ENV", None)
        check_true("the rest of the environment is intact", len(env) > 5)
        check("os.environ itself untouched",
              _os.environ.get("CLAUDE_CODE_CHILD_SESSION"), "poison")

        # End to end: a really spawned child must not see them either.
        probe = Path(_tf.gettempdir()) / "ccm_env_test.txt"
        probe.unlink(missing_ok=True)
        _sp.Popen(["cmd.exe", "/c",
                   f'set CLAUDE > "{probe}" 2>&1 || echo NONE > "{probe}"'],
                  close_fds=True, env=clean_env()).wait(timeout=60)
        text = probe.read_text(encoding="utf-8", errors="replace") if probe.exists() else ""
        check("spawned child sees no markers",
              [n for n in INHERITED_SESSION_VARS if f"{n}=" in text], [])
        probe.unlink(missing_ok=True)
    finally:
        for name, value in saved.items():
            if value is None:
                _os.environ.pop(name, None)
            else:
                _os.environ[name] = value
        for extra in ("CLAUDE_CONFIG_DIR", "NO_COLOR", "WT_SESSION"):
            _os.environ.pop(extra, None)

    print("\n[settings writer]")
    import json as _json
    import os
    import shutil as _shutil
    import tempfile

    from cc_manager import config as cc_config

    real = os.environ.get("CLAUDE_CONFIG_DIR")
    tmp = Path(tempfile.mkdtemp(prefix="ccm-cfg-"))
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
    try:
        original = {"permissions": {"defaultMode": "bypassPermissions"},
                    "hooks": {"Stop": [{"hooks": []}]}, "model": "opus[1m]"}
        (tmp / "settings.json").write_text(_json.dumps(original, indent=2),
                                           encoding="utf-8")

        cc_config.set_key("remoteControlAtStartup", True)
        after = _json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        check("setting written", after.get("remoteControlAtStartup"), True)
        check("permissions preserved", after.get("permissions"), original["permissions"])
        check("hooks preserved", after.get("hooks"), original["hooks"])
        check("model preserved", after.get("model"), original["model"])

        check("toggle flips to False", cc_config.toggle("remoteControlAtStartup"), False)
        check("toggle flips back", cc_config.toggle("remoteControlAtStartup"), True)

        raised = False
        try:
            cc_config.set_key("permissions", {"defaultMode": "wideOpen"})
        except KeyError:
            raised = True
        check("refuses keys outside the allow-list", raised, True)
        still = _json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        check("refused write changed nothing",
              still.get("permissions"), original["permissions"])
        check_true("a backup was made", list(tmp.glob("settings.json.bak-*")))
    finally:
        if real is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = real
        _shutil.rmtree(tmp, ignore_errors=True)

    print("\n[window]")
    try:
        from cc_manager.gui import CCManagerGUI
    except Exception as exc:
        print(f"  FAIL import gui: {exc}")
        FAILURES.append("import gui")
        return 1

    app = CCManagerGUI()
    try:
        app.update()
        check_true("window built", app.winfo_exists())

        deadline = time.time() + 30
        while time.time() < deadline and not app.sessions:
            app.update()
            time.sleep(0.05)
        check_true("scan populated sessions", app.sessions)
        check("rows match filter", len(app.rows),
              len([m for m in app.sessions if app._matches(m)]))
        check_true("projects listed", app.projects.size() > 1)

        app.search_var.set("zzz-definitely-no-match")
        app.update()
        check("nonsense search clears the table", len(app.rows), 0)
        check("detail handles empty selection",
              app.current(), None)

        app.search_var.set("")
        app.update()
        check_true("clearing search restores rows", app.rows)
        check_true("a row is selected", app.current() is not None)

        # Selecting a project must narrow the list, never widen it.
        total = len(app.rows)
        names = [n for n in app._project_names if n]
        if names:
            app.project = names[0]
            app.refresh_sessions()
            app.update()
            check_true("project filter narrows", len(app.rows) <= total)
            check_true("filtered rows all in project",
                       all(app._label_for(m) == names[0] for m in app.rows))
            app.project = None
            app.refresh_sessions()

        app.show_parked.set(True)
        app.update()
        with_parked = len(app.rows)
        app.show_parked.set(False)
        app.update()
        check_true("parked toggle changes the view", with_parked >= len(app.rows))

        print("\n[double-open guard]")
        target = app.rows[0]
        was_live = target.is_live
        target.is_live = False
        app.launched.pop(target.session_id, None)
        check("not open before launching", app.is_open(target), False)

        app.launched[target.session_id] = time.time()
        check("counts as open right after launch", app.is_open(target), True)
        check("shows as opening until registered", app.is_opening(target), True)

        target.is_live = True
        check("live session counts as open", app.is_open(target), True)
        check("live session is no longer 'opening'", app.is_opening(target), False)

        app.launched[target.session_id] = time.time() - 10_000
        target.is_live = False
        check("stale launch record expires", app.is_open(target), False)

        app.launched.pop(target.session_id, None)
        target.is_live = was_live

        app.refresh_sessions()
        app.update()
        sel = app.current()
        if sel is not None and app.is_open(sel):
            check("open session disables the button",
                  str(app.go["state"]), "disabled")
    finally:
        app.destroy()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
