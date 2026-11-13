@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Pick Python: project .venv -> local DSH runtime -> py launcher -> python on PATH
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set PY="%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY (where py >nul 2>nul && set PY=py -3)
if not defined PY set PY=python

echo ============================================================
echo  akari-match-export regression tests
echo  Python: %PY%
echo ============================================================

set /a PASS=0
set /a FAIL=0
set /a SKIP=0
set ALL=
if /i "%~1"=="--all" set ALL=1
if not defined ALL (
    echo   Usage: run-tests.cmd [--all]
    echo          no args = quick suite: test_advisor + test_rank + test_review
    echo          --all   = also run test_elo / test_build_dataset / test_build_timeline
    echo                   feature-alignment validator validate_features_alignment.py
    echo                   and pipeline tests test_match_export / test_unpack_replay
    echo                   skipped automatically when deps are missing, plus docs gate test_docs.py
)

rem ---- quick suite (always run) ----
call :run test_advisor.py
call :run test_rank.py
call :run test_review.py

if not defined ALL goto :summary

rem ---- --all: data / feature regression ----
%PY% -c "import pandas" >nul 2>nul
if errorlevel 1 (
    call :skip test_elo.py pandas-missing
) else (
    call :run test_elo.py
)
call :run test_build_dataset.py
call :run test_build_timeline.py

rem ---- --all: feature alignment regression (T6; expected PASS=58 FAIL=0) ----
call :run validate_features_alignment.py

rem ---- --all: pipeline regression (needs requests / zstandard) ----
%PY% -c "import requests" >nul 2>nul
if errorlevel 1 (
    call :skip test_match_export.py requests-missing
) else (
    call :run test_match_export.py
)
%PY% -c "import zstandard" >nul 2>nul
if errorlevel 1 (
    call :skip test_unpack_replay.py zstandard-missing
) else (
    call :run test_unpack_replay.py
)

rem ---- --all: v2.3.13 回归（假 LCU 数据驱动，不需要游戏客户端） ----
call :run test_v2313.py

rem ---- --all: D2 口径回归（事件闸门 / 目标物截断 / 阈值预算 / 校准评估） ----
call :run test_d2.py
if exist test_calibration.py call :run test_calibration.py

rem ---- --all: D3 训练口径与监控（局权重 / 指标 / patch / 漂移 / 段位快照） ----
if exist test_d3.py call :run test_d3.py
if exist test_d3_monitor.py call :run test_d3_monitor.py

rem ---- --all: docs gate (stdlib only, always runs) ----
call :run test_docs.py

:summary
echo ------------------------------------------------------------
echo   Result: PASS=%PASS% FAIL=%FAIL% SKIP=%SKIP%
echo ------------------------------------------------------------
if %FAIL% GTR 0 (
    echo [run-tests] FAILED
    exit /b 1
)
echo [run-tests] ALL PASS
exit /b 0

:run
echo.
echo ---- %~1 ----
%PY% "%~1"
if errorlevel 1 (
    echo [FAIL] %~1
    set /a FAIL+=1
) else (
    echo [PASS] %~1
    set /a PASS+=1
)
exit /b 0

:skip
echo [SKIP] %~1 (%~2)
set /a SKIP+=1
exit /b 0
