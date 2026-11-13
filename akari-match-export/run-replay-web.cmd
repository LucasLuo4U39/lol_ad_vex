@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if "%~1"=="" (
    echo Usage: run-replay-web.cmd "path\to\snapshots.jsonl" "player-fragment" [speed]
    echo   例如: run-replay-web.cmd "..\analysis\akari-replay-live\session_20261109_015738\snapshots.jsonl" future 30
    pause
    exit /b 1
)

rem Pick Python: project .venv -> local DSH runtime -> py launcher -> python on PATH
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set PY="%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (where py >nul 2>nul && set PY=py -3)
if not defined PY set PY=python

set "SPEED=30"
if not "%~3"=="" set "SPEED=%~3"

%PY% "advisor_server.py" --snapshots "%~1" --player "%~2" --speed %SPEED% --port 8765
exit /b %ERRORLEVEL%
