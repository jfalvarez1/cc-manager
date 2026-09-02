@echo off
rem Launch the cc-manager desktop window with no console attached.
rem Uses pythonw.exe so no black terminal flashes behind the GUI.

setlocal
set "CCM_HOME=%USERPROFILE%\.claude\tools\cc-manager"

if not exist "%CCM_HOME%\cc_manager\gui.py" (
  echo cc-manager: package not found at "%CCM_HOME%" 1>&2
  exit /b 1
)

if defined PYTHONPATH (
  set "PYTHONPATH=%CCM_HOME%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%CCM_HOME%"
)

where /q pythonw.exe && (
  start "" pythonw -m cc_manager --gui %*
  exit /b 0
)

where /q python.exe && (
  start "" python -m cc_manager --gui %*
  exit /b 0
)

echo cc-manager: no python interpreter found on PATH 1>&2
exit /b 1
