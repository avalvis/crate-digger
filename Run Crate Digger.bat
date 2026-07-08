@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ─────────────────────────────────────────────────────────────────────
REM  Crate Digger — Windows launcher
REM  Double-click to set up (first run) and start the app.
REM ─────────────────────────────────────────────────────────────────────

title Crate Digger
cd /d "%~dp0"

set "APP_DIR=%CD%"
set "VENV_PY=%APP_DIR%\.venv\Scripts\python.exe"
set "VENV_PIP=%APP_DIR%\.venv\Scripts\pip.exe"
set "MAIN=%APP_DIR%\main.py"
set "REQ=%APP_DIR%\requirements.txt"

echo.
echo  ========================================
echo   Crate Digger
echo  ========================================
echo.

REM ── 1. Locate a suitable Python (3.11+) ──────────────────────────────

set "PY_BOOT="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    for %%V in (3.14 3.13 3.12 3.11) do (
        if not defined PY_BOOT (
            py -%%V -c "import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
            if !ERRORLEVEL!==0 set "PY_BOOT=py -%%V"
        )
    )
    if not defined PY_BOOT set "PY_BOOT=py -3"
)

if not defined PY_BOOT (
    where python >nul 2>&1
    if %ERRORLEVEL%==0 (
        python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
        if !ERRORLEVEL!==0 set "PY_BOOT=python"
    )
)

if not defined PY_BOOT (
    echo  [ERROR] Python 3.11 or newer was not found.
    echo.
    echo  Install Python from https://www.python.org/downloads/
    echo  During setup, enable "Add python.exe to PATH".
    echo  The Windows "py" launcher is recommended.
    echo.
    pause
    exit /b 1
)

echo  Using Python: %PY_BOOT%

REM ── 2. Create virtual environment (first run) ────────────────────────

if not exist "%VENV_PY%" (
    echo.
    echo  First run — creating local virtual environment...
    %PY_BOOT% -m venv "%APP_DIR%\.venv"
    if errorlevel 1 (
        echo  [ERROR] Could not create .venv
        pause
        exit /b 1
    )
    echo  Virtual environment created.
)

if not exist "%VENV_PY%" (
    echo  [ERROR] Virtual environment is missing: %VENV_PY%
    pause
    exit /b 1
)

REM ── 3. Upgrade pip (quiet) ───────────────────────────────────────────

"%VENV_PY%" -m pip install --upgrade pip --quiet 2>nul

REM ── 4. Check / install dependencies ────────────────────────────────

if not exist "%REQ%" (
    echo  [ERROR] requirements.txt not found in %APP_DIR%
    pause
    exit /b 1
)

set "PYTHONPATH=%APP_DIR%"
"%VENV_PY%" "%APP_DIR%\scripts\preflight_check.py" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  Installing dependencies ^(first run may take several minutes^)...
    echo  This includes audio analysis and optional stem separation.
    echo.
    "%VENV_PIP%" install -r "%REQ%"
    if errorlevel 1 (
        echo.
        echo  [ERROR] pip install failed. See messages above.
        echo  Try running this file again, or check your internet connection.
        pause
        exit /b 1
    )
)

"%VENV_PY%" "%APP_DIR%\scripts\preflight_check.py"
if errorlevel 2 (
    echo.
    echo  [ERROR] Python in .venv is too old. Delete the .venv folder and run again.
    pause
    exit /b 1
)
if errorlevel 1 (
    echo.
    echo  [ERROR] Dependencies still missing after install.
    pause
    exit /b 1
)

REM ── 5. Launch ────────────────────────────────────────────────────────

echo.
echo  Starting Crate Digger...
echo  Log file: %LOCALAPPDATA%\CrateDigger\cratedigger.log
echo.

set "PYTHONPATH=%APP_DIR%"
"%VENV_PY%" "%MAIN%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo  Crate Digger exited with error code %EXIT_CODE%.
    echo  Check the log file for details.
    pause
    exit /b %EXIT_CODE%
)

endlocal
exit /b 0
