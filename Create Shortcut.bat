@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_launch_shortcut.ps1"
if errorlevel 1 (
    echo.
    echo  Could not create shortcut. See message above.
    pause
    exit /b 1
)
echo.
echo  Done. Use "Crate Digger.lnk" for the vinyl icon shortcut.
echo.
pause
