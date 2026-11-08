@echo off
title RoleSync 1-Tap Web Studio
cd /d "%~dp0"

echo ============================================================
echo   RoleSync - 1-Tap Web Studio Launcher
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [*] Setting up virtual environment...
    py -3.11 -m venv .venv 2>nul || python -m venv .venv
)

echo [*] Launching RoleSync Web Studio...
echo [*] Opening in your default browser at http://localhost:8501
echo.

".venv\Scripts\python.exe" -m streamlit run web_app.py --server.port 8501

pause
