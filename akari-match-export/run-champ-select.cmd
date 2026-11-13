@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem 选择 Python：项目 .venv -> 本地 DSH 运行时 -> py 启动器 -> PATH 里的 python
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set PY="%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (where py >nul 2>nul && set PY=py -3)
if not defined PY set PY=python

rem 2 秒后自动打开浏览器（服务在前台启动，Ctrl+C 退出）
start "" cmd /c "timeout /t 2 >nul & start "" http://127.0.0.1:8788"

%PY% "champ_select_server.py" --port 8788 %*
exit /b %ERRORLEVEL%
