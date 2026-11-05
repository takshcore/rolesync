@echo off
title RoleSync Autonomous Job Lifecycle Agent
cd /d "%~dp0"

echo ============================================================
echo   RoleSync - 1-Click Launcher
echo ============================================================
echo.

REM 1. Check if virtual environment python exists
if exist ".venv\Scripts\python.exe" goto check_deps

echo [*] Virtual environment not found. Creating .venv using Python 3.11...
py -3.11 -m venv .venv 2>nul || python -m venv .venv
if errorlevel 1 goto venv_error

:check_deps
echo [*] Checking dependencies...
".venv\Scripts\python.exe" -c "import dotenv, PyQt6, docx" >nul 2>&1
if not errorlevel 1 goto launch_app

echo.
echo [*] First-time setup detected: Installing required packages...
echo [*] This will take a few minutes. Please wait...
echo.
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto install_error

echo [*] Installing Playwright Chromium browser...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto playwright_error

echo.
echo [*] All packages installed successfully!
echo.

:launch_app
echo [*] Starting RoleSync Desktop Overlay...
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto app_error

echo.
echo [*] RoleSync closed cleanly.
goto finish

:venv_error
echo.
echo [!] ERROR: Failed to create virtual environment.
echo Please verify that Python is installed and available in PATH.
goto finish

:install_error
echo.
echo [!] ERROR: Failed installing packages from requirements.txt.
goto finish

:playwright_error
echo.
echo [!] ERROR: Failed installing Playwright browser.
goto finish

:app_error
echo.
echo [!] ERROR: RoleSync encountered an error while running.
goto finish

:finish
echo.
echo ============================================================
echo Press any key to exit this window...
echo ============================================================
pause >nul
