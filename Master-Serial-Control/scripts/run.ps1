# 激活 Master-Serial-Control 独立虚拟环境并启动持续串口控制模式。
[CmdletBinding()]
param(
    [string]$Port = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ActivateScript = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path -LiteralPath $ActivateScript)) {
    throw "Virtual environment not found. Run scripts\install.ps1 first."
}

$PreviousLocation = Get-Location
try {
    Set-Location -LiteralPath $ProjectRoot
    . $ActivateScript
    if ($Port) {
        python -m app.main --port $Port run
    }
    else {
        python -m app.main run
    }
}
finally {
    Set-Location -LiteralPath $PreviousLocation
}
