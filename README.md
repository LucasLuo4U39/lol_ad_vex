# LoLAdvisor

英雄联盟对局实时助手：桌面端 + Web 一体化界面，基于本地客户端（LCU）数据给出
**当前胜率估计、胜率-时间曲线、事件概率、可执行建议、打野雷达**，并提供左下角半透明悬浮窗。

> 本仓库是源码存档（v2.3.17）。训练数据集（CSV）与游戏导出文件体积过大，未包含；
> 数据如何重新生成见下文「数据管线」。

## 主要能力

- **实时**：每 5 秒从 LCU 拉一次对局数据，LightGBM 文本模型 + 纯 numpy 解释器（`lgbmini.py`）推理，不依赖 lightgbm 运行时。
- **选人**：BP 阶段双方阵容、禁用、阵容胜率、候选英雄（对位克制 + 强度）、胜率-时间曲线。
- **打野雷达**：小地图 1fps 抓帧 + 英雄识别 + OP.GG 开野路线推断（统计推测，非真实视野）。
- **悬浮窗**：纯 Win32（ctypes）半透明置顶窗口，鼠标穿透，对局中显示胜率/趋势/预测/建议。
- **模型体检**：`/api/home` 透出模型年龄、patch、特征漂移，便于发现补丁漂移。

## 目录

```
akari-match-export/     主工程（Python）
  desktop_app.py        桌面版入口：本地 HTTP 服务 + Edge 窗口 + 悬浮窗 + 打野雷达
  advisor_server.py     对局状态、模型封装、规则建议、LLM 建议层
  champ_select_server.py 选人助手服务（8788）
  live_features.py      实时特征抽取（与训练口径一致）+ 事件提醒闸门
  lgbmini.py            纯 numpy LightGBM 文本模型解释器
  overlay.py            左下角悬浮窗（ctypes / Win32）
  model_monitor.py      模型年龄 + 特征漂移监控
  ui/                   Web 界面（app.html / app.js / design-system.css / previews）
  assets/               随包资源（模型 / OP.GG 数据 / 开野路线 / 图标）
  train_*.py build_*.py 数据构建与训练脚本
  test_*.py             回归测试（run-tests.cmd --all）
  docs/                 校准实测报告等
dataset/                模型文件与 metrics（不含训练 CSV）与导出/回放样例说明
```

## 快速开始

环境：Windows 10/11，Python 3.12（3.11+ 应可），英雄联盟客户端已登录。

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install lightgbm==4.7.0 pandas scikit-learn   :: 仅训练/测试需要

:: Web 仪表盘
run-web.cmd

:: 桌面版（Edge 一体化单页 + 悬浮窗）
.venv\Scripts\python desktop_app.py
```

打包 exe：

```bat
build_exe.cmd      :: 产出 dist\LoLAdvisor\LoLAdvisor.exe 与 dist\LoLAdvisor-win64.zip
```

## 数据管线

```
exports/（run-harvest.cmd 采集，每局一个 game_<id>/）
  → run-live-dataset.cmd / build_live_dataset.py    live_dataset.csv
  → build_live_dataset_v3.py                        + OP.GG 特征
  → build_elo_features.py / build_live_dataset_v4.py + 赛前 Elo
  → build_live_dataset_v5.py                        + 段位特征
  → train_live_model.py                             model.txt / metrics.json / oof_predictions.csv
  → evaluate_calibration.py                         校准复评（ECE / 可靠性曲线 / 分分钟）
```

- 训练前先跑 `python check_dataset.py --dataset <csv>`，会检查列/局/标签一致性、段位泄漏提示，并打印建议命令。
- 训练默认**不按局等权**（实测 AUC 无提升、校准变差），需要实验时加 `--game-weight`。
- 段位特征来自赛后快照（线上用赛前段位），可用 `--drop-rank-features` 训练部署口径更保守的版本。
- 详细实测见 [`akari-match-export/docs/calibration-report-2026-11-14.md`](akari-match-export/docs/calibration-report-2026-11-14.md)。

## 测试

```bat
run-tests.cmd --all
```

覆盖：特征口径、模型推理、段位解析、复盘、Elo、数据构建、事件提醒闸门、目标物时间截断、
校准评估、训练集成、监控与快照。当前状态：13 个测试脚本全部通过（约 500 项断言）。

## 模型诚实说明

- 模型基于 **16.22 / 韩服 / 2,298 局** 训练；补丁更新后特征分布会漂移，`model_monitor` 会提示模型年龄与 patch。
- 已验证的校准：ECE ≈ 0.008（2,298 局按局 bootstrap），不需要额外校准器。
- `live_model5` 比 `live_model3` 高的 AUC 全部来自段位特征（含赛后快照时点），
  线上可复现大部分（赛前段位），但不能把 0.855 当成纯对局态势预测。
- 第 1 分钟 AUC：含段位 0.70、不含段位 0.55 —— 早段给「倾向」可以，给 0.1% 精度不合适。

## 未包含 / 注意

- 训练 CSV、游戏导出（`exports/`）、`.venv`、以及任何 API key 均不在仓库内；
- **`analysis/` 未包含**：里面是回放/复盘样例与玩家数据（含第三方 puuid、召唤师名），
  属于个人数据，不随公开仓库发布；需要时用 `run-harvest.cmd` / `run-replay-collect.cmd` 自行采集；
- 采集需要本机登录客户端（LCU），各服 shard 不同（峡谷之巅需登录 BGP2）；
- 本仓库未指定开源许可证；如需开源请自行选择并补充 LICENSE。

## 版本

见 `akari-match-export/VERSION.txt`（当前 v2.3.17，含 2.3.x 历史修复记录）。
