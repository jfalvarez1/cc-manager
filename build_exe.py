"""Build the standalone CCManager.exe.

    python build_exe.py

Produces dist/CCManager/CCManager.exe -- a real executable with its own icon,
rather than a script run by pythonw.exe. That distinction is the whole point:
scripts all share the interpreter's identity, so Windows groups unrelated
Python apps under one generic icon and pinning pins python itself.

One-dir rather than one-file, matching the LEDStudio build alongside it:
it starts faster and does not unpack to a temp directory on every launch.
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ICON = BASE / "cc_manager.ico"
DIST = BASE / "dist"
NAME = "CCManager"


def main() -> int:
    if not ICON.is_file():
        print("icon missing - run: python make_icon.py", file=sys.stderr)
        return 1

    for stale in (DIST / NAME, BASE / "build"):
        shutil.rmtree(stale, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--windowed",                  # no console window behind the GUI
        "--name", NAME,
        "--icon", str(ICON),
        # ship the .ico so the window and Alt-Tab show it too, not just the exe
        "--add-data", f"{ICON}{';' if sys.platform == 'win32' else ':'}.",
        "--paths", str(BASE),
        "--hidden-import", "cc_manager.gui",
        "--distpath", str(DIST),
        "--workpath", str(BASE / "build"),
        "--specpath", str(BASE / "build"),
        str(BASE / "app_main.py"),
    ]
    print(" ".join(cmd), "\n")
    result = subprocess.run(cmd, cwd=str(BASE))
    if result.returncode != 0:
        return result.returncode

    exe = DIST / NAME / f"{NAME}.exe"
    if not exe.is_file():
        print(f"build reported success but {exe} is missing", file=sys.stderr)
        return 1
    print(f"\nbuilt {exe}  ({exe.stat().st_size / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
