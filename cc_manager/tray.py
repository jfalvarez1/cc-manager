"""System tray support.

Lets the window disappear to the notification area instead of closing, so the
session list stays a click away without keeping a window on screen.

pystray owns its own message loop, so the icon runs on a background thread and
every callback hops back to the Tk thread via ``after``. Calling into tkinter
from the tray thread is the classic way to deadlock or crash an app like this.

Everything here degrades quietly: if pystray or the icon file is unavailable,
tray support simply reports itself unsupported and the window behaves normally.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable


def available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


class Tray:
    """A tray icon bound to a Tk window."""

    def __init__(self, app, icon_file: Path | None, on_show: Callable,
                 on_quit: Callable, on_rescan: Callable | None = None):
        self.app = app
        self.icon_file = icon_file
        self.on_show = on_show
        self.on_quit = on_quit
        self.on_rescan = on_rescan
        self._icon = None
        self._thread: threading.Thread | None = None

    # Callbacks arrive on pystray's thread; marshal them onto the Tk thread.
    def _post(self, fn) -> None:
        try:
            self.app.after(0, fn)
        except Exception:
            pass

    def _image(self):
        from PIL import Image, ImageDraw

        if self.icon_file and Path(self.icon_file).is_file():
            try:
                img = Image.open(self.icon_file)
                # .ico files hold several frames; take one a tray can use.
                try:
                    img.size = (32, 32)
                    img.load()
                except Exception:
                    pass
                return img.convert("RGBA")
            except Exception:
                pass

        img = Image.new("RGBA", (32, 32), (11, 15, 20, 255))
        d = ImageDraw.Draw(img)
        for i, colour in enumerate(((74, 222, 128), (251, 191, 36), (107, 114, 128))):
            y = 7 + i * 9
            d.ellipse([5, y, 11, y + 6], fill=colour)
            d.rectangle([14, y + 1, 27, y + 4], fill=(200, 205, 215))
        return img

    def show(self) -> bool:
        """Create and run the tray icon. Returns False if unsupported."""
        if self._icon is not None:
            return True
        if not available():
            return False
        try:
            import pystray
        except Exception:
            return False

        items = [pystray.MenuItem("Show cc-manager",
                                  lambda *_: self._post(self.on_show),
                                  default=True)]
        if self.on_rescan is not None:
            items.append(pystray.MenuItem("Rescan",
                                          lambda *_: self._post(self.on_rescan)))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Quit", lambda *_: self._post(self.on_quit)))

        try:
            self._icon = pystray.Icon("cc-manager", self._image(),
                                      "cc-manager — Claude Code sessions",
                                      pystray.Menu(*items))
        except Exception:
            self._icon = None
            return False

        # run_detached avoids a second thread where the backend supports it.
        try:
            self._icon.run_detached()
            return True
        except Exception:
            pass
        try:
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
            return True
        except Exception:
            self._icon = None
            return False

    def hide(self) -> None:
        """Remove the icon."""
        icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass
        self._thread = None

    @property
    def visible(self) -> bool:
        return self._icon is not None
