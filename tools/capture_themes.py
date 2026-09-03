"""Capture a screenshot of the window in every theme.

    python tools/capture_themes.py            # synthetic sessions (default)
    python tools/capture_themes.py --real     # your own sessions

Writes docs/<theme>.png. It grabs the real window rather than mocking one, so
the shots stay honest about what the app looks like -- including the animated
banner, which is given a moment to run before each capture.

The sessions in it are fabricated by default. Real ones would put working
directories, project names, session ids and the first line of every prompt into
a public README.
"""
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from PIL import ImageGrab  # noqa: E402

from tools.demo_store import build_demo_store, fake_liveness  # noqa: E402

OUT = BASE / "docs"
OUT.mkdir(exist_ok=True)


def main() -> int:
    demo_root = None
    if "--real" not in sys.argv:
        demo_root = build_demo_store()      # sets CLAUDE_CONFIG_DIR
        fake_liveness()
        print(f"using a synthetic store at {demo_root}")

    # Imported after the store is in place so paths resolve to it.
    from cc_manager.gui import THEMES, CCManagerGUI

    app = CCManagerGUI()
    app.animated.set(True)
    app.geometry("1180x680+120+90")
    app.update()

    # Wait for the background scan so the list has real rows in it.
    for _ in range(200):
        app.update()
        if app.sessions:
            break
        time.sleep(0.05)

    app.attributes("-topmost", True)
    app.lift()
    app.update()
    time.sleep(0.6)

    for name in THEMES:
        app.apply_theme(name)
        app._set_awake(True)
        # Let the banner animate a little so the motion shows in the shot.
        for _ in range(45):
            app._set_awake(True)
            app.update()
            time.sleep(0.02)

        app.update_idletasks()
        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        dest = OUT / f"{name}.png"
        img.save(dest)
        print(f"{name:<10} {img.size[0]}x{img.size[1]}  -> docs/{dest.name}")

    app.attributes("-topmost", False)
    app.quit_app()
    if demo_root is not None:
        shutil.rmtree(demo_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
