@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist ".venv\Scripts\python.exe" (
    echo [akari-match-export] .venv not found.
    echo Run setup.cmd first, or:
    echo   setup.cmd "C:\Path\To\python.exe"
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "match_export.py" %*
set "CODE=%ERRORLEVEL%"

if "%~1"=="" (
    echo.
    pause
)

exit /b %CODE%
