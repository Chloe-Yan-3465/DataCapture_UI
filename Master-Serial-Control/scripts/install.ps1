# 为 Master-Serial-Control 创建独立 Python 3.11/3.12 虚拟环境并安装依赖。
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = $null
$PythonPrefix = @()

$Launcher = Get-Command py -ErrorAction SilentlyContinue
if ($Launcher) {
    foreach ($Version in @("3.12", "3.11")) {
        & $Launcher.Source "-$Version" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == ($($Version.Replace('.', ','))) and sys.maxsize > 2**32 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $Launcher.Source
            $PythonPrefix = @("-$Version")
            break
        }
    }
}

if (-not $PythonExe) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        & $PythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) and sys.maxsize > 2**32 else 1)"
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $PythonCommand.Source
        }
    }
}

if (-not $PythonExe) {
    throw "Python 3.11 or 3.12 (64-bit) was not found."
}

$VenvPath = Join-Path $ProjectRoot ".venv"
Write-Host "Using Python:" (& $PythonExe @PythonPrefix -c "import sys; print(sys.executable, sys.version)")
& $PythonExe @PythonPrefix -m venv $VenvPath

$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")

Write-Host "Environment ready at $VenvPath"
