[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ActivateScript = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path -LiteralPath $ActivateScript)) {
    throw "Virtual environment not found. Run scripts\install.ps1 after dependency installation is approved."
}

$PreviousLocation = Get-Location
try {
    Set-Location -LiteralPath $ProjectRoot
    . $ActivateScript
    python -m app.main run
}
finally {
    Set-Location -LiteralPath $PreviousLocation
}
