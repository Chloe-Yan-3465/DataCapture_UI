param(
    [double]$TrackerRate = 120
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'toolkit_common.ps1')
$uv = Find-UvExecutable
$roleMap = Join-Path $PSScriptRoot 'tracker_roles.json'
if (-not (Test-Path -LiteralPath $roleMap)) {
    throw 'tracker_roles.json is missing. Run the Tracker role binding script first.'
}

$arguments = @(
    'run', '--script', (Join-Path $PSScriptRoot 'collect_openxr_tracker_poses_and_triggers.py'),
    '--role-map', $roleMap,
    '--output-root', (Join-Path $PSScriptRoot 'vr_captures'),
    '--tracker-rate', $TrackerRate
)

& $uv @arguments
if ($LASTEXITCODE -ne 0) {
    throw "VR collection failed with exit code $LASTEXITCODE"
}
