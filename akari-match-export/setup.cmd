@echo off
setlocal
cd /d "%~dp0"

rem Pick a base Python to build the venv with:
rem   explicit arg -> local DSH runtime -> py -3 -> python on PATH
rem (same probe order as run-web.cmd / run-tests.cmd)
set "PY=%~1"
if "%PY%"=="" if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if "%PY%"=="" (where py >nul 2>nul && set "PY=py -3")
if "%PY%"=="" (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if "%PY%"=="" (
    echo [akari-match-export] Python 3.10+ not found.
    echo Install Python, or pass the interpreter path:
    echo   setup.cmd "C:\Path\To\python.exe"
    exit /b 1
)

echo [1/2] Creating .venv with: %PY%
%PY% -m venv .venv
if errorlevel 1 (
    echo Failed to create .venv.
    exit /b 1
)

rem [2/2] Install from requirements.txt (sections, see that file):
rem   [inference]        numpy / requests / psutil / zstandard
rem   [train/validate]   pandas / lightgbm==4.7.0 / scikit-learn
echo [2/2] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Failed to install dependencies.
    exit /b 1
)

echo.
echo Done. Start with:
echo   run.cmd --count 100
exit /b 0
