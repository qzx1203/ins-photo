@echo off
setlocal
cd /d "%~dp0"
if exist "dist\Shiguang\Shiguang.exe" (
    start "" "dist\Shiguang\Shiguang.exe"
    exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
    echo Please run setup.cmd first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -c "import PySide6, instaloader, PIL, requests" >nul 2>&1
if errorlevel 1 (
    echo Dependencies are missing. Please run setup.cmd first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "main.py"
