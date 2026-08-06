param(
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'toolkit_common.ps1')
$uv = Find-UvExecutable
& $uv run --script (Join-Path $PSScriptRoot 'bind_tracker_roles.py') `
    --output (Join-Path $PSScriptRoot 'tracker_roles.json')
if ($LASTEXITCODE -ne 0) { throw "Role binding failed with exit code $LASTEXITCODE" }
Write-Host "Saved: $(Join-Path $PSScriptRoot 'tracker_roles.json')"
if (-not $NonInteractive) {
    Read-Host 'Press Enter to close'
}
