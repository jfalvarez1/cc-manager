<#
.SYNOPSIS
    Launcher for cc-manager (PowerShell).

    Adds the package directory to PYTHONPATH so cc-manager can live anywhere
    without being pip-installed, then hands every argument through.
#>

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$root;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $root
}

$python = if ($env:CC_MANAGER_PYTHON) { $env:CC_MANAGER_PYTHON }
          elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' }
          else { 'python' }

& $python -m cc_manager @args
exit $LASTEXITCODE
