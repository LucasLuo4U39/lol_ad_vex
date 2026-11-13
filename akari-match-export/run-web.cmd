@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Pick Python: project .venv -> local DSH runtime -> py launcher -> python on PATH
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set PY="%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (where py >nul 2>nul && set PY=py -3)
if not defined PY set PY=python

%PY% "advisor_server.py" %*
exit /b %ERRORLEVEL%
