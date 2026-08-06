param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$uiRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $uiRoot
$blePython = Join-Path $projectRoot 'BLE-TimeSync\.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $blePython) {
    $python = $blePython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python was not found. Install Python 3.11/3.12 or prepare BLE-TimeSync\.venv first.'
    }
    $python = $pythonCommand.Source
}

$arguments = @('-u', (Join-Path $uiRoot 'app.py'), '--host', '127.0.0.1', '--port', $Port)
if ($NoBrowser) { $arguments += '--no-browser' }

& $python @arguments
