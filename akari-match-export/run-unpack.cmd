@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist ".venv\Scripts\python.exe" (
    echo [akari-match-export] .venv not found. Run setup.cmd first.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Usage: run-unpack.cmd "path\to\replay.rofl" [--output DIR]
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "unpack_replay.py" %*
exit /b %ERRORLEVEL%
