# Creates a Windows shortcut with the vinyl icon, pointing at Run Crate Digger.bat.
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BatPath = Join-Path $ProjectRoot "Run Crate Digger.bat"
$IconPath = Join-Path $ProjectRoot "assets\crate-digger.ico"
$ShortcutPath = Join-Path $ProjectRoot "Crate Digger.lnk"

if (-not (Test-Path $BatPath)) {
    Write-Error "Launcher not found: $BatPath"
}

if (-not (Test-Path $IconPath)) {
    Write-Host "Icon missing - building assets first..."
    $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        $Python = "python"
    }
    & $Python (Join-Path $ProjectRoot "scripts\build_app_icon.py")
    if (-not (Test-Path $IconPath)) {
        Write-Error "Could not build icon at $IconPath"
    }
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $BatPath
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.IconLocation = "$IconPath,0"
$Shortcut.Description = "Crate Digger - sample discovery and MPC workflow"
$Shortcut.Save()

Write-Host "Shortcut created: $ShortcutPath"
