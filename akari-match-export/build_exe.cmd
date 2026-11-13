@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem pick python: .venv -> DSH bundled -> py -3 -> python
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set PY="%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (where py >nul 2>nul && set PY=py -3)
if not defined PY set PY=python

rem deps
%PY% -m pip install --quiet pyinstaller numpy psutil websocket-client mss pillow || goto :fail

rem assets (draft model / champions / html / opgg)
%PY% prepare_assets.py || goto :fail

rem build onedir
%PY% -m PyInstaller --noconfirm --clean --onedir --windowed ^
    --name LoLAdvisor ^
    --icon "assets\app.ico" ^
    --add-data "assets;assets" ^
    --add-data "build_ui;ui" ^
    --hidden-import fetch_player_ranks ^
    --hidden-import websocket ^
    --hidden-import psutil ^
    --hidden-import attribution ^
    --hidden-import overlay ^
    --hidden-import model_monitor ^
    --distpath dist --workpath build ^
    desktop_app.py || goto :fail

copy /y "assets\app.ico" "dist\LoLAdvisor\app.ico" >nul
copy /y "使用说明.txt" "dist\LoLAdvisor\使用说明.txt" >nul 2>nul
copy /y "VERSION.txt" "dist\LoLAdvisor\VERSION.txt" >nul 2>nul

rem 清理历史 zip，只保留固定名 LoLAdvisor-win64.zip
if exist "dist\LoLAdvisor-v2-*.zip" del /q "dist\LoLAdvisor-v2-*.zip"

powershell -NoProfile -Command "Compress-Archive -Path 'dist\LoLAdvisor' -DestinationPath 'dist\LoLAdvisor-win64.zip' -Force" || goto :fail

rem 自动自检：模型加载/推理必须通过才算构建成功（失败 exit 1）
rem 窗口版 exe 是 GUI 子系统，必须 start /wait 才能拿到退出码
if exist "dist\LoLAdvisor\selftest.log" del /q "dist\LoLAdvisor\selftest.log"
start /wait "" "dist\LoLAdvisor\LoLAdvisor.exe" --selftest
if errorlevel 1 goto :fail
findstr /c:"SELFTEST OK" "dist\LoLAdvisor\selftest.log" >nul || goto :fail

echo.
echo Build done.
echo   dir: %~dp0dist\LoLAdvisor\LoLAdvisor.exe
echo   zip: %~dp0dist\LoLAdvisor-win64.zip
echo selftest: PASSED（详见 dist\LoLAdvisor\selftest.log）
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
