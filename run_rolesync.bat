@echo off
title RoleSync Autonomous Job Lifecycle Agent
cd /d "%~dp0"

echo ============================================================
echo   RoleSync - 1-Click Launcher
echo ============================================================
echo.

REM 1. Ensure the virtual environment exists
if not exist ".venv\Scripts\python.exe" (
    echo [*] Creating virtual environment (.venv)...
    python -m venv .venv
    if errorlevel 1 (
        echo [!] Failed to create .venv. Please make sure Python is installed.
        pause
        exit /b 1
    )
)

REM 2. Verify if required dependencies are installed
echo [*] Checking dependencies...
".venv\Scripts\python.exe" -c "import dotenv, PyQt6, docx" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [*] First-time setup detected: Installing required packages...
    echo [*] This will take a couple of minutes. Please wait...
    echo.
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [!] Failed to install dependencies from requirements.txt.
        pause
        exit /b 1
    )
    echo [*] Installing Playwright Chromium browser...
    ".venv\Scripts\python.exe" -m playwright install chromium
    echo.
    echo [✓] All packages installed successfully!
    echo.
)

REM 3. Launch the desktop application
echo [*] Starting RoleSync Desktop Overlay...
".venv\Scripts\python.exe" main.py

if errorlevel 1 (
    echo.
    echo [!] Application exited with an error.
    echo Press any key to close this window.
    pause >nul
)
