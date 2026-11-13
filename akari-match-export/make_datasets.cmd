@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PY=.venv\Scripts\python.exe"
set "ROOT=%~dp0.."
set "ANALYSIS=%ROOT%\analysis"
set "OUTDIR=%ROOT%\dataset\live_dataset"

if not exist "%PY%" (
    echo [make_datasets] 找不到 .venv：先运行 setup.cmd 装依赖
    exit /b 1
)
if "%~1"=="" (
    echo Usage: make_datasets.cmd "path\to\exports-folder" [output-dir]
    echo   例: make_datasets.cmd "C:\Users\Administrator\Documents\akari-top300-exports"
    echo   说明: exports 目录由 run-harvest.cmd 生成（每个对局一个 game_^<gameId^>^/）
    exit /b 1
)
set "EXPORTS=%~1"
if not "%~2"=="" set "OUTDIR=%~2"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo ============================================================
echo  exports: %EXPORTS%
echo  output : %OUTDIR%
echo ============================================================

echo [1/5] live_dataset.csv（每分钟/每视角基础表）
"%PY%" build_live_dataset.py --export-root "%EXPORTS%" --output "%OUTDIR%\live_dataset.csv" || goto :fail

echo [2/5] live_dataset_v3.csv（+ OP.GG 英雄/对位胜率）
"%PY%" build_live_dataset_v3.py --dataset "%OUTDIR%\live_dataset.csv" --output "%OUTDIR%\live_dataset_v3.csv" || goto :fail

echo [3/5] elo_features.csv / elo_player_games.csv（赛前选手 Elo）
"%PY%" build_elo_features.py --summaries "%ANALYSIS%\game_summaries.jsonl" --out "%ANALYSIS%\elo_features.csv" --players-out "%ANALYSIS%\elo_players.json" || goto :fail

echo [4/5] live_dataset_v4.csv（+ 选手 Elo）
"%PY%" build_live_dataset_v4.py --v3 "%OUTDIR%\live_dataset_v3.csv" --elo-games "%ANALYSIS%\elo_features.csv" --elo-players "%ANALYSIS%\elo_player_games.csv" --out "%OUTDIR%\live_dataset_v4.csv" || goto :fail

echo [5/5] live_dataset_v5.csv（+ 段位特征，注意：赛后快照口径）
"%PY%" build_live_dataset_v5.py --v3 "%OUTDIR%\live_dataset_v3.csv" --player-games "%ANALYSIS%\elo_player_games.csv" --ranks "%ANALYSIS%\player_ranks.json" --out "%OUTDIR%\live_dataset_v5.csv" || goto :fail

echo.
echo 全部生成完毕，做一次训练前自检：
"%PY%" check_dataset.py --dataset "%OUTDIR%\live_dataset_v5.csv"
exit /b %ERRORLEVEL%

:fail
echo.
echo [make_datasets] 失败（上一步退出码非 0），请查看上面的报错。
exit /b 1
