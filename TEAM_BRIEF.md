# 团队简报 — loltestv2（akari-match-export）智能体团队

> 本文件由上轮对话的项目成果整理而成，是四位团队成员（pipeline-dev、model-dev、ui-dev、qa-tester）的统一背景资料。
> 第一轮：各成员只读审查自己的域并向 lead 汇报；Lead 合并后统一分配第二轮实施任务。

## 1. 项目是什么

英雄联盟对局分析工具链：赛前（选人建议）/ 赛中（实时胜率曲线、事件概率、建议、口袋金币）/ 赛后（全位置复盘）。

- 项目目录（工作区相对路径）：`loltestv1/akari-match-export/`
- 顶层资料：`loltestv1/修复说明.md`、`loltestv1/打包说明.txt`、工作区根 `loltestv2-fixes.zip`

数据链路：

- ROFL 回放解析：`unpack_replay.py`、`match_export.py`、`dump_rofl_blocks.py`、`export_rofl_chunks.py`、`decode_field_tables.py`、`build_timeline.py`、`build_replay_1s_dataset.py`、`replay_collector.py`
- LCU 实时/回放观看：`live_collector.py`、`lcu_replay_watch.py`、`rank_lookup.py`、`champ_select_server.py`
- SGP 观战帧：`sgp_gold.py`、`sgp_players.py`
- OP.GG 抓取：`opgg_scrape.py`、`game_champs.py`（产出 `opgg_stats.json`、`game_champs.json`）
- 玩家身份/段位：`fetch_game_summaries.py`、`fetch_player_ranks.py`、`harvest_players.py`、`build_elo_features.py`、`player_elo.py`

模型（LightGBM 文本模型；推理不依赖 lightgbm，用纯 numpy 解释器 `lgbmini.py`，与真 lightgbm 误差为 0）：

- `train_model.py`（dataset_v2）、`train_event_model.py`、`train_live_event_model.py`、`train_replay_event_model.py`
- 实时：`live_model3`（OP.GG 特征，OOF AUC 0.8286）、`live_model4`（Elo，0.8257）、`live_model5`（段位，0.8553），由 `build_live_dataset_v3/v4/v5.py` + `train_live_model.py` 产出，`validate_v3.py`、`validate_lgbmini.py` 验证
- 选人：`train_draft_model.py`（配合 `champ_select_server.py`）

界面/打包：

- Web：`advisor_server.py` + `advisor_dashboard.html`（`run-web.cmd` / `run-replay-web.cmd` 启动）
- 选人页：`champ_select_server.py`（`run-champ-select.cmd`）
- 桌面：`desktop_app.py` → `build_exe.cmd` → `dist\LoLAdvisor\LoLAdvisor.exe`（onedir ~24MB，`LoLAdvisor.spec`）
- 复盘：`match_review.py`、`review_games.py`

## 2. 上轮收尾状态（v2.1，时间 2026-11-10）

- `test_advisor.py` 61/61 通过（Python 3.12.14 + numpy，无 lightgbm）；回放模式实跑正常。
- `validate_lgbmini.py` 全量 579,790 行：pearson(最终模型,OOF)=0.994034、全量 AUC 0.8406、事件模型 0.6439/0.6445/0.6544/0.6543。
- 9 份 `metrics.json` 已全部相对路径化（`pathutil.py`）：6 份运行时/打包侧指回随包 CSV（真实存在），3 份训练侧（dataset_v2 ×2、event_dataset ×1）指回训练 CSV（按打包说明有意不随包）。
- **模型与数据集上轮未重训、未改动**：第一轮审查不许重训、不许改 `dataset/*.csv` 与模型 `.txt`。
- 注意：`champ_select_server.py`（11/10 19:35）、`desktop_app.py`（19:43）、`train_draft_model.py`（15:45）等时间戳晚于 19:44 的打包说明，疑似上轮最后阶段的选人模型/桌面整合工作，完成度需要 ui-dev 与 model-dev 重点核实。

## 3. 团队分工（详见共享任务板 T1–T4）

| 成员 | 域 | 写范围（advisory） |
|---|---|---|
| pipeline-dev | 回放/LCU/SGP 采集解析 | `loltestv1/akari-match-export/` 下管线文件清单（任务 T1） |
| model-dev | 特征/数据集/训练/解释器 | 同上模型文件清单（任务 T2） |
| ui-dev | Web/桌面/打包/文档 | ui 文件清单（任务 T3） |
| qa-tester | 跨模块测试与验收 | test_*.py 与打包验收（任务 T4） |
| ui-designer | UI 重写（第三轮）：设计系统与新页面 | 仅新增 `loltestv1/akari-match-export/ui/**`（Phase A 不得改任何现有文件） |

## 5. 第三轮 UI 专项（进行中）

- 用户要求：三个界面（桌面主窗口 / 选人助手页 / Web 仪表盘）全部重写，统一 Web 技术，**苹果风格**，高标准交付。
- 权威文档：[loltestv1/UI_DESIGN_BRIEF.md](UI_DESIGN_BRIEF.md)（tokens、数据契约、质量红线、流程）。
- Phase A（ui-designer 并行）：设计系统 + 新页面（新目录 ui/，不动现有文件）；
  Phase B（第二轮完成后，ui-dev）：接入与打包 v2.3；
  Phase C（qa-tester）：按简报第 5 节验收。


## 4. 协作规则

1. 第一轮只读审查：按自己任务里的文件清单读代码，产出「现状 + 问题（按严重度）+ 下一步建议（按价值排序，标注工作量与依赖）」发给 lead（`send_message`，target 为 `lead`）。不修改项目文件。
2. 第二轮由 lead 统一分配：编辑前先 read；只动自己写范围内的文件；跨域改动先请示 lead。
3. 成员共享同一文件系统，编辑即时可见；避免与其他人同时改同一文件。
4. Windows 环境。Python 探测顺序：项目 `.venv` → `%USERPROFILE%\.dsh` → `py -3` → `python`；需要捆绑依赖时用 `load_workspace_dependencies`。第一轮不要跑训练、不要下载大数据。
5. 任务板：用 `team_task_list` 找到自己的任务，`team_task_get` 读详情，按当前 revision `claim`。
