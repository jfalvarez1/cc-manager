@echo off
rem Launcher for cc-manager. Copy or symlink this onto your PATH (e.g. into
rem %USERPROFILE%\.local\bin) and "cc-manager" works from Windows PowerShell,
rem pwsh, cmd and git-bash alike -- no shell profile needed.

setlocal
set "CCM_HOME=%USERPROFILE%\.claude\tools\cc-manager"

if not exist "%CCM_HOME%\cc_manager\__main__.py" (
  echo cc-manager: package not found at "%CCM_HOME%" 1>&2
  exit /b 1
)

if defined PYTHONPATH (
  set "PYTHONPATH=%CCM_HOME%;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%CCM_HOME%"
)

if defined CC_MANAGER_PYTHON (
  "%CC_MANAGER_PYTHON%" -m cc_manager %*
  exit /b %ERRORLEVEL%
)

where /q python.exe && (
  python -m cc_manager %*
  exit /b %ERRORLEVEL%
)

where /q py.exe && (
  py -m cc_manager %*
  exit /b %ERRORLEVEL%
)

echo cc-manager: no python interpreter found on PATH 1>&2
exit /b 1
