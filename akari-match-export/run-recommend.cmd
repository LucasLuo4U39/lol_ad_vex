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
    echo Usage: run-recommend.cmd --dataset "path\to\event_dataset.csv" --game-id 123 --t 1200 --side blue
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "recommend.py" %*
exit /b %ERRORLEVEL%
