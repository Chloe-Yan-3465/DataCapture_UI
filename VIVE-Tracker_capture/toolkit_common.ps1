Set-StrictMode -Version 2.0

function Find-UvExecutable {
    $command = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw 'uv was not found. Install it from https://docs.astral.sh/uv/getting-started/installation/ and reopen PowerShell.'
}

function Find-SteamRoot {
    $candidates = @()
    foreach ($key in @(
        'HKCU:\Software\Valve\Steam',
        'HKLM:\Software\WOW6432Node\Valve\Steam',
        'HKLM:\Software\Valve\Steam'
    )) {
        try {
            $item = Get-ItemProperty $key -ErrorAction Stop
            foreach ($name in @('SteamPath', 'InstallPath')) {
                if ($item.PSObject.Properties.Name -contains $name) {
                    $candidates += $item.$name
                }
            }
        } catch {}
    }
    $candidates += @(
        (Join-Path ${env:ProgramFiles(x86)} 'Steam'),
        (Join-Path $env:ProgramFiles 'Steam')
    )
    foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
        $normalized = [System.IO.Path]::GetFullPath(($candidate -replace '/', '\'))
        if (Test-Path -LiteralPath (Join-Path $normalized 'steam.exe')) { return $normalized }
    }
    throw 'Steam installation was not found in the registry or standard locations.'
}

function Find-SteamVrRoot {
    param([Parameter(Mandatory=$true)][string]$SteamRoot)
    $candidates = @((Join-Path $SteamRoot 'steamapps\common\SteamVR'))

    foreach ($uninstallRoot in @(
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )) {
        foreach ($item in Get-ItemProperty $uninstallRoot -ErrorAction SilentlyContinue) {
            $names = $item.PSObject.Properties.Name
            if (($names -contains 'DisplayName') -and
                ($names -contains 'InstallLocation') -and
                $item.DisplayName -eq 'SteamVR' -and $item.InstallLocation) {
                $candidates += $item.InstallLocation
            }
        }
    }

    $libraryFile = Join-Path $SteamRoot 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $libraryFile) {
        foreach ($line in Get-Content -LiteralPath $libraryFile) {
            if ($line -match '"path"\s+"([^"]+)"') {
                $library = $Matches[1] -replace '\\\\', '\'
                $candidates += Join-Path $library 'steamapps\common\SteamVR'
            }
        }
    }
    foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
        $normalized = [System.IO.Path]::GetFullPath(($candidate -replace '/', '\'))
        if (Test-Path -LiteralPath (Join-Path $normalized 'bin\win64\vrstartup.exe')) {
            return $normalized
        }
    }
    throw 'SteamVR was not found. Install SteamVR in Steam, then rerun this script.'
}

function Find-ViveHubShortcut {
    $roots = @(
        (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'),
        (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs')
    )
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $shortcut = Get-ChildItem -LiteralPath $root -Recurse -Filter 'VIVE Hub.lnk' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($shortcut) { return $shortcut.FullName }
    }
    throw 'VIVE Hub shortcut was not found. Install VIVE Hub and the VIVE Ultimate Tracker service.'
}
