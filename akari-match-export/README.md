# LoLAdvisor — 英雄联盟对局分析工具链

赛前 / 赛中 / 赛后的一站式分析：选人阶段阵容胜率与按位推荐（选人助手）、
对局中实时胜率曲线 / 事件概率 / 建议 / 口袋金币（Web 仪表盘 + 桌面版）、
赛后全位置复盘（review_games），底层数据来自从 League Akari `match-export`
模块抽出的独立导出管线（LCU REST + SGP + 回放解析）。

- **推理零依赖**：模型推理只用标准库 + numpy（`lgbmini.py` 纯 Python 解释器），
  不需要 lightgbm / pandas / requests
- **桌面版**：`dist\LoLAdvisor\LoLAdvisor.exe`（`build_exe.cmd` 一键打包为
  `dist\LoLAdvisor-win64.zip`，onedir 约 62 MB / zip 约 24 MB）
- **网页版**：`run-web.cmd`（实时仪表盘 http://127.0.0.1:8765）、
  `run-replay-web.cmd`（快照回放）、`run-champ-select.cmd`（选人助手 http://127.0.0.1:8788）
- **运行前提**：英雄联盟客户端运行并已登录（LCU REST + Live Client Data 2999 + SGP 战绩服务）
- 所有 `.cmd` 启动器的 Python 探测顺序：项目 `.venv` →
  `%USERPROFILE%\.dsh\dsh-runtimes\...\python.exe` → `py -3` → PATH 里的 `python`
- 当前版本见 `VERSION.txt`（v2.3.16；一体化单页 + 深色电竞主题 + 主页视图，胜率归因 + 打野雷达 + 左下角悬浮窗）

## 快速开始

```bat
:: 桌面版（推荐）：双击 dist\LoLAdvisor\LoLAdvisor.exe（主界面点「打开选人助手」进入 BP 推荐窗口）
:: 实时 Web 仪表盘
run-web.cmd

:: 选人助手（网页版，--demo 用假数据预览布局）
run-champ-select.cmd
```

> 个人数据导出工具只读取当前账号自己的对局，仍使用 SGP 内部接口，
> 相关合规风险与 League Akari 本体一致，请自行评估。

## 历史：数据导出工具（从 League Akari match-export 模块抽出）

本仓库最早是 League Akari 的 `match-export` 模块抽出来的独立命令行工具，负责导出战绩
（JSON / CSV / JSONL）与下载 `.rofl` 回放，是全套分析管线（数据集 / 训练 / 复盘）的数据来源，
至今保留并继续使用。

### 目录

```
akari-match-export/
├── match_export.py     # 导出工具主程序
├── requirements.txt    # 分「推理必需 / 训练可选」两节（详见 lgbmini 章节）
├── setup.cmd           # 重建 .venv 并安装依赖
├── run.cmd             # 导出工具启动入口
└── ...                 # 其余为分析/模型/界面脚本，见下文各章节
```

### 环境

- Windows
- Python 3.10+（当前 `.venv` 使用本机 Python 3.12 创建）
- 依赖：`requests`、`psutil`、`zstandard`（已安装到 `.venv`）

如果 `.venv` 丢失或损坏：

```bat
cd /d "本仓库目录\akari-match-export"
setup.cmd "C:\Path\To\python.exe"
```

### 启动命令

在 `cmd.exe` 中：

```bat
cd /d "本仓库目录\akari-match-export"
run.cmd --count 100
```

只导数据（默认，不下载回放）：

```bat
run.cmd --count 100
```

数据 + 最近 100 场回放：

```bat
run.cmd --count 100 --replays
```

只导最近 20 场、不要详情：

```bat
run.cmd --count 20 --no-details
```

指定输出父目录：

```bat
run.cmd --count 100 --output "D:\LoLExports"
```

### 参数

| 参数 | 说明 |
|---|---|
| `--count N` | 最近 N 场，1–200，默认 100 |
| `--no-details` | 不拉取每场详情（跳过 `details.jsonl`） |
| `--replays` | 下载回放文件，体积可能很大 |
| `--output DIR` | 输出父目录，默认 `%USERPROFILE%\Downloads\LeagueAkari` |
| `--puuid UUID` | 指定 PUUID，默认当前登录账号 |
| `--server ID` | 手动指定 SGP 服务器，如 `TENCENT_TJ100`、`NA1` |
| `--region REGION` | 手动指定 LCU region |
| `--lcu-port PORT` | 手动指定 LCU 端口（跳过进程发现） |
| `--lcu-token TOKEN` | 手动指定 LCU remoting token |
| `--insecure-sgp` | 忽略 SGP TLS 证书校验 |
| `--quiet` | 减少逐条进度输出 |

### 输出

```
LeagueAkari-<名字>-<tag>-<时间戳>/
├── manifest.json      # 元信息、成功/失败计数
├── summary.json       # SGP 原始战绩列表
├── matches.csv        # 每场一行（仅本人），UTF-8 BOM，Excel 直接打开
├── details.jsonl      # 每行一场完整 DETAILS（除非 --no-details）
├── errors.json        # 失败项（仅在有错误时生成）
└── replays/
    └── <gameId>.rofl  # 回放（仅 --replays）
```

## 采集高分段对局 + 训练基线

### 1. 采集 Challenger 前 N 名

```bat
run-harvest.cmd --top 10 --games 10 --output "C:\Users\Administrator\Documents\akari-top10-exports"
```

- 从 LCU `/lol-ranked/v1/apex-leagues/RANKED_SOLO_5x5/CHALLENGER` 取前 N。
- 每个玩家拉最近 N 场 420（排位单双）。
- 自动去重：高分段玩家经常排到同一局。
- 每个对局输出 `game_<gameId>/`，包含 `summary.json`、`details.jsonl`、`timeline/`。

Challenger 上限是 200 人。要凑 300 人，加上 Grandmaster 前 100：

```bat
run-harvest.cmd --top 300 --games 20 --include-grandmaster --workers 8 --output "...\akari-top300-exports"
```

> 峡谷之巅是 BGP2，和当前登录的 NJ100 不是同一个 shard。用 NJ100 的 token 请求 BGP2
> 会返回 `Invalid shard NJ100`，所以只能先跑当前大区。要采集峡谷之巅，需要把客户端登录到 BGP2 再运行。

### 2. 生成数据集

```bat
run-dataset.cmd --export-root "C:\Users\Administrator\Documents\akari-top10-exports" --output "...\dataset"
```

### 3. 训练

```bat
run-train.cmd --dataset "...\dataset\dataset_blue.csv" --output "...\dataset\model"
```

- `GroupKFold` 按 `gameId` 切分。
- 默认**不包含** championId（小数据集会记英雄而不是学走势）；需要时加 `--include-champs`。
- 输出 `model.txt`、`metrics.json`、`feature_importance.csv`、`oof_predictions.csv`。

## 打野 / 击杀特征与消融

`build_dataset.py` 现在会额外生成：

- 分位置累计 KDA：`kills_diff_<ROLE>` / `deaths_diff_<ROLE>` / `assists_diff_<ROLE>`
- 击杀发生路：`kill_lane_top/mid/bot/jungle_diff`
- 一血：`first_blood`（+1 蓝 / -1 红）
- 打野当前位置：`jungler_zone_<LANE>_diff`
- 打野靠近该路：`jungler_near_<LANE>_diff`
- 打野参与该路击杀：`jungler_gank_<LANE>_diff`
- 首清营地：`blue_jungle_start_<CAMP>` / `red_jungle_start_<CAMP>`
- 首清入侵：`*_jungle_start_invade`

但这些特征在 1 分钟粒度上**没有提升**，消融结果：

| 特征组 | 特征数 | AUC | Brier |
|---|---:|---:|---:|
| base | 48 | 0.8223 | 0.1710 |
| base + kills | 68 | 0.8214 | 0.1715 |
| base + jungle | 67 | 0.8215 | 0.1715 |
| base + all | 87 | 0.8203 | 0.1721 |

原因：

- 1 分钟位置太粗，无法还原真实刷野路线；
- 击杀信息已经被 `gold_diff` / `xp_diff` / `kills_diff` 覆盖；
- 首清营地是每局常量，容易记住对局而不是学走势。

因此 `train_model.py` 默认 `--feature-groups base`，保留旧模型效果。需要实验时：

```bat
run-train.cmd --dataset "...\dataset_blue.csv" --feature-groups kills
run-train.cmd --dataset "...\dataset_blue.csv" --feature-groups jungle
run-train.cmd --dataset "...\dataset_blue.csv" --feature-groups all
```

重新跑消融：

```bat
run-ablation.cmd --dataset "...\dataset_blue.csv"
```

## OP.GG 英雄/对位胜率特征（v3 模型）

把 OP.GG 当前版本（16.22）的英雄分路胜率和对位胜率作为新特征加入模型：

| 新特征 | 含义 |
|---|---|
| `own_champ_wr` | 自己英雄在其分路的版本胜率（缺省 0.5） |
| `enemy_champ_wr` | 对位敌方英雄的分路胜率（缺省 0.5） |
| `matchup_wr` | 自己英雄 vs 对位英雄的对位胜率（OP.GG counters 原值） |
| `matchup_games_log` | log1p(对位样本场次) |
| `team_wr_diff` | 我方 5 人分路胜率均值 − 敌方均值 |

### 流程

```bat
:: 1. 抓 OP.GG（5 个位置 × 英雄胜率页 + 239 个对位页，输出 opgg_stats.json）
python opgg_scrape.py --workers 10

:: 2. 给每局补英雄阵容（DETAILS 取 puuid → SUMMARY 取 roster，断点续跑）
python game_champs.py --workers 16

:: 3. 拼接特征（live_dataset.csv + game_champs.json + opgg_stats.json）
python build_live_dataset_v3.py

:: 4. 重训（GroupKFold，与 live_model2 完全相同的超参）
python train_live_model.py --dataset "...\live_dataset_v3.csv" --output "...\live_model3"

:: 5. 验证（lgbmini 解释器 vs OOF + AUC）
python validate_v3.py --dataset "...\live_dataset_v3.csv" --model-dir "...\live_model3"
```

### 结果

**公平对比（1,279 局 / 322,020 行：同一批数据、同样的折、同样超参）**

| 模型 | 特征 | OOF AUC | LogLoss | Brier | Accuracy |
|---|---:|---:|---:|---:|---:|
| live_model2（旧） | 46 | 0.8277 | 0.5017 | 0.1689 | 73.52% |
| live_model3（+OP.GG） | 51 | **0.8317** | **0.4978** | **0.1672** | **74.10%** |

**全量数据（2,298 局 / 579,790 行，live_model2 的原始训练数据）**

| 模型 | OOF AUC | LogLoss | Brier | Accuracy |
|---|---:|---:|---:|---:|
| live_model2 | 0.8272 | 0.5031 | 0.1693 | 73.56% |
| **live_model3** | **0.8286** | **0.5027** | **0.1688** | **73.85%** |

`team_wr_diff` 重要性稳定排前 4（全特征），新特征合计约 11–12% 的重要性。
`advisor_server.py` 默认自动优先使用 `live_model3`（存在时），推理零额外依赖。

注意：OP.GG 是 KR 区数据，国服英雄强度有差异；对位胜率方向语义为
「主体英雄对敌方英雄的胜率」（已用正反两个方向的互补性验证过）。

## Live Client Data 实时管线

### 真实对局能看到什么

Live Client Data（`https://127.0.0.1:2999/liveclientdata/allgamedata`）：

| 数据 | 是否可见 |
|---|---|
| 自己的金币 `activePlayer.currentGold` | ✅ |
| 其他玩家的金币 | ❌ |
| 所有人的等级 / KDA / 补刀 / 装备 / 眼分 | ✅ |
| 地图坐标（其他玩家） | ❌ |
| 击杀 / 龙 / 塔等事件 | ✅ |

所以旧的 `gold_diff` / `xp_diff` 模型**不能直接上线**。实时模型改用：

- 自己：等级、金币、补刀、KDA
- 双方：击杀/死亡/助攻、补刀总和、等级总和
- 目标：小龙/大龙/先锋/巢虫/厄塔汗/塔/水晶
- 最近击杀/gank/龙/塔的时间与数量

### 采集器

对局进行中运行：

```bat
run-live-collect.cmd --interval 5 --output "...\live_data"
```

输出 `live_data/session_*/snapshots.jsonl`，每 5 秒一个完整 `allgamedata` 快照。

### 可观测数据集 + 胜率模型

```bat
run-live-dataset.cmd --export-root "...\akari-top300-exports"

run-live-train.cmd --dataset "...\live_dataset\live_dataset.csv"
```

2,298 局 / 579,790 行（10 个玩家视角 × 每分钟）：

| 模型 | 特征 | AUC | LogLoss | Brier |
|---|---:|---:|---:|---:|
| 旧模型（含敌方金币） | 48 | 0.8223 | 0.5099 | 0.1710 |
| **Live 可观测模型** | **46** | **0.8272** | **0.5031** | **0.1693** |

注意：live 数据集是“玩家视角”，每局 10 行/分钟，AUC 和旧模型不是严格同分布；
但说明 **不用敌方金币也能达到相近水平**，主要靠击杀差、等级差、补刀差、目标差。

### Live 击杀 / Gank 模型

```bat
run-live-event-train.cmd --dataset "...\live_dataset\live_dataset.csv"
```

| 标签 | 正样本率 | AP | 基线 AP | ROC-AUC |
|---|---:|---:|---:|---:|
| own_kill_30s | 0.327 | 0.4293 | 0.3273 | 0.6193 |
| enemy_kill_30s | 0.327 | 0.4301 | 0.3273 | 0.6213 |
| own_gank_60s | 0.354 | 0.4693 | 0.3543 | 0.6373 |
| enemy_gank_60s | 0.354 | 0.4680 | 0.3543 | 0.6364 |

没有敌方金币、没有位置，gank 预测只能靠团队状态和事件节奏，ROC-AUC 0.64 左右。

### 实时建议

旧版控制台版（依赖 lightgbm）：

```bat
run-live-advisor.cmd --model-dir "...\live_dataset\live_model2"
```

每 5 秒打印：

- 胜率
- 自己的 KDA / CS / 金币
- 团队击杀差 / 等级差 / 补刀差
- 击杀 30s、Gank 60s 的我方/敌方概率
- 规则建议

事件模型默认从 `live_model2` 同级的 `live_event_model` 读取。

### 实时助手 Web 仪表盘（推荐，零依赖）

`advisor_server.py` 把上面这套升级成浏览器仪表盘 + 可选 LLM 建议层。
**只需要标准库 + numpy + 自带模型**，不需要 lightgbm / pandas / requests。

```bat
:: 实时模式（对局进行中，客户端登录状态）
run-web.cmd
:: 打开 http://127.0.0.1:8765

:: 直播回放模式：先在客户端打开一局回放播放，然后（1x 速度，一局等于对局时长）
run-web.cmd --player future --interval 1 --game-id 901128442938

:: 快照回放模式（不开客户端，加速回放已采集的快照做演示/测试）
run-replay-web.cmd "..\analysis\akari-replay-live\session_20261109_015738\snapshots.jsonl" future 30
```

仪表盘内容：

- 胜率大数字 + 全程走势曲线（SVG）
- 自己：等级 / KDA / 补刀 / 金币（回放模式下金币为装备估算）
- 团队差：击杀/死亡/助攻/补刀/等级和/小龙/大龙/先锋/巢虫/厄塔汗/塔/水晶
- 事件概率：30 秒击杀（我/敌）、60 秒 Gank（我/敌）
- 建议列表（规则建议 + LLM 建议分栏）
- 最近事件流（击杀/目标，按阵营着色）

接口：

- `GET /` 仪表盘页面
- `GET /api/state` 最新状态 JSON（含历史曲线，最多 2400 点）

自动打开回放：`lcu_replay_watch.py --port <LCU端口> --token <token> --game-id <id> --watch`
走客户端自己的"观看回放"接口（POST `/lol-replays/v1/rofls/{gameId}/watch`），
与在客户端里手动点"观看回放"完全等价；LCU 端口/token 从 LeagueClientUx 的命令行参数获取。

参数：

| 参数 | 说明 |
|---|---|
| `--snapshots PATH` | 回放模式：`snapshots.jsonl`（`replay_collector.py` 的产物） |
| `--player FRAGMENT` | 回放视角：自己的 riotId 或英雄名片段，如 `future`、`Samira`（快照回放与直播回放都适用） |
| `--game-id ID` | 直播回放的金币桥：对局 ID，从 SGP 1 分钟帧补真实**口袋金币**（currentGold） |
| `--lcu-port N` / `--lcu-token T` | 金币桥所需的 LCU 端口/token，默认自动从 lockfile 读取 |
| `--speed N` | 回放速度倍数，默认 10 |
| `--port N` | 仪表盘端口，默认 8765 |
| `--interval N` | 实时模式轮询间隔，默认 5 秒 |
| `--once` / `--ticks N` | 处理 1 / N 个 tick 后退出（测试用） |

金币说明：回放不暴露金币。加了 `--game-id` 后，金币用 SGP 每 60 秒一帧的
`currentGold`（兜里未花费的钱，推荐装备看这个）；查不到才回退到装备总价估算。
仪表盘会标明当前是「SGP 1分钟口袋金币」还是「装备估算」。

#### LLM 建议层（可选）

配置方式（按优先级）：

1. **`advisor.llm.env` 文件**（推荐，自动加载）：
   ```bat
   :: 编辑 advisor.llm.env 填入自己的 key，然后直接启动即可
   run-web.cmd
   ```
2. 环境变量：
   ```bat
   set ADVISOR_LLM_API_KEY=sk-...
   set ADVISOR_LLM_BASE_URL=https://api.deepseek.com   :: 可选，OpenAI 兼容接口
   set ADVISOR_LLM_MODEL=deepseek-flash                :: 可选，账号内可用模型
   run-web.cmd
   ```

- 检测到 key 就**自动启用** LLM 层（启动日志会打印模型/接口和**遮罩后的 key**，
  如 `sk-6ce0****571c`，不会打印完整 key）；`--no-llm` 可临时关闭。
- 只做**表达层**：把胜率、团队差、目标差、事件概率、近 2 分钟事件等结构化状态
  发给 LLM，生成 2–4 条短建议；**LLM 不做预测**。
- 每 `--llm-interval`（对局秒）调用一次，后台线程执行，失败自动退回规则建议。
- **`advisor.llm.env` 含个人密钥，勿分享、勿打包、勿提交**（已在 .gitignore 中）。
- 若换成推理型模型（如 `deepseek-flash` / `deepseek-v4-pro`）后建议为空，
  是 `max_tokens` 被推理内容吃光所致；`call_llm()` 里已设置 `max_tokens=800` + `effort=low`，
  需要时可在那里调整。

#### 纯 Python LightGBM 解释器（lgbmini.py）

由于仪表盘要求**开箱即用**（不能强制所有人先装 lightgbm），`lgbmini.py` 实现了
LightGBM v4 文本模型（`model.txt`）的纯 numpy 解释器：解析树结构、数值分裂
（`value <= threshold` 走左子树）、sigmoid 输出。只支持本项目模型用到的
子集，遇到分类分裂/线性树等会直接报错而不是给错结果。训练侧仍然用真 lightgbm
（项目内 `akari-match-export\requirements.txt` 分「推理必需 / 训练可选」两节，
「训练可选」节 pin `lightgbm==4.7.0`；顶层 `loltestv1\requirements.txt` 是另一份
独立依赖清单），只有推理侧走 lgbmini。

正确性验证（`validate_lgbmini.py`，对 579,790 行 `live_dataset.csv`）：

| 检查 | 结果 |
|---|---|
| 最终模型 vs 保存的 OOF 预测 Pearson 相关 | **0.994** |
| live_model2 最终模型全量 AUC | 0.8406（OOF 为 0.8272，偏高属预期） |
| 逐分钟 AUC（第 1→20 分钟） | 0.52 → 0.90，单调上升 |
| 4 个事件模型最终 AUC | 0.644–0.654（OOF 为 0.619–0.637） |

`test_advisor.py` 是端到端自测（快照源/特征/预测/事件流/HTTP/LLM 桩/回放视角注入/
OP.GG 查表/lgbmini 分类分裂），61 项断言。
`test_review.py` 是复盘渲染层自测，48 项断言；`test_elo.py` 是 Elo 混合自测，38 项断言；
`test_rank.py` 是实时段位特征自测（含观战补 activePlayer），31 项断言。
运行环境：任何带 numpy 的 Python 3.10+（本项目用 DSH 自带 Python 运行）。

`run-web.cmd` / `run-replay-web.cmd` 的 Python 探测顺序：项目 `.venv` →
`%USERPROFILE%\.dsh\dsh-runtimes\...\python.exe` → `py -3` → PATH 里的 `python`。

## 赛后复盘（全位置点评）

`review_games.py` 把一局渲染成完整报告：胜负曲线、关键节点、五路对位表、分阶段走势、
胜率拐点、团队数据、改进建议。`match_review.py` 是渲染层（Markdown + HTML），
离线回填和将来的实时录制共用同一份 `summary` schema。

### 从历史数据集回填

```bat
python review_games.py --games 901121900494,901118041203 --out ..\analysis\game-reviews
python review_games.py --select games.txt --out ..\analysis\game-reviews
```

```text
analysis/game-reviews/
├── index.md / index.html          # 索引（胜方最低胜率、时长、跳转）
├── OVERVIEW.md                    # 跨局总结（可选）
└── <gameId>/
    ├── summary.json               # 结构化摘要（渲染层输入）
    ├── report.md / report.html    # 报告
    └── narrative.md               # 「结论」正文，存在则覆盖规则判词
```

- 数据来源：`live_dataset_v3.csv`（1 分钟 × 10 人视角）+ `game_champs.json`（阵容）；
- 胜率曲线用 `live_model3` 对每个玩家视角推理后按队伍取平均，标签为「蓝方胜率」；
- `narrative.md` 是点评层（人工或 LLM 生成）。目前没有接 LLM API（本机没有 key），
  数据部分与点评层已经完全解耦，接上后只需替换 `narrative.md` 的生成方式。

### 口径与已知误差

- 金币是各玩家**兜里的现金**（`currentGold`），不是团队总经济，也不含已出装；
- 目标由差值的逐分钟增量还原：同一分钟双方各拿一条会互相抵消，最后一分钟的建筑可能未计入；
- 短于 28 分钟且人头差距很大的对局通常是投降结束，塔数不代表推塔能力；
- 只有 1 分钟粒度，没有坐标/视野，结论最细只能到「哪一分钟发生了什么」。

示例产物：`analysis/game-reviews/`（10 局，含 4 局翻盘、3 局碾压、3 局拉锯）。

## 选手 Elo（人的胜率）

英雄胜率解释不了「谁在打」：同一套阵容，5 个状态好的玩家和 5 个连败的玩家，曲线完全不同。
`player_elo.py` 在复盘/开局前抓每名玩家**最近 25 场**排位，按时间重放算 Elo，再折成赛前胜率，
按权重随时间衰减地混进模型曲线。

```bat
:: 1) 先给某局算 Elo（需要客户端在线；结果缓存到 elo_cache/<gameId>.json）
python player_elo.py --game-id 901127125498 --count 25 --out ..\analysis\elo-901127125498.json

:: 2) 复盘时注入
python review_games.py --games 901127125498 --elo ..\analysis\elo-901127125498.json

:: 或者让复盘自己抓（同样需要客户端）
python review_games.py --games 901127125498 --elo-fetch
```

### 算法

- 只取 `queueId ∈ {420, 440}`、时长 ≥ 10 分钟的局；
- 每场 SGP SUMMARY 都能拿到 **10 名玩家的 puuid**，所以不只给这 10 个人算分，同场对手也进评分网络：
  陌生玩家从 1500 起步，前 10 场 `K=48`，之后 `K=32`；同一 `gameId` 只算一次；
- **不用未来数据**：只取该局 `gameCreation` 之前的比赛（`filter_before`），
  所以历史局算出来的是「当时的状态」，不是「现在回看」；
- 赛前胜率：`p_elo = 1 / (1 + 10^(-ΔElo / 400))`；
- 融合：`logit(p) = w·logit(p_elo) + (1-w)·logit(p_model)`，
  `w = --elo-weight × exp(-t / --elo-tau)`，默认 `0.5` / 900 秒——开局信 Elo，越往后越信模型。

赛前 Elo 差对曲线的影响（模型 50% 起步时）：

| 赛前 Elo 差 | 赛前胜率 | 1 分钟 | 5 分钟 | 10 分钟 | 20 分钟 | 30 分钟 |
|---|---|---|---|---|---|---|
| +50 | 57% | +3.4pp | +2.6pp | +1.8pp | +0.9pp | +0.5pp |
| +100 | 64% | +6.7pp | +5.1pp | +3.7pp | +1.9pp | +1.0pp |
| +200 | 76% | +13.1pp | +10.2pp | +7.3pp | +3.8pp | +1.9pp |
| +300 | 85% | +19.2pp | +15.2pp | +10.9pp | +5.7pp | +2.9pp |

### 限制

- 抓 SGP 需要客户端在线；离线只能用 `--elo` 喂 JSON；
- 25 场是「最近状态」，跨段位的旧局会稀释信号（没有做段位分层）；
- 这是**曲线级融合**，不是把 Elo 做成模型特征。后者要重训，前提是训练集里带玩家标识——
  目前 `live_dataset_v3.csv` 只有 `gameId`/分路，没有 puuid，所以模型本身还学不到「谁在打」。

### 把「谁在打」做成模型特征（v4 / v5 实验）

补上 puuid 之后，按三个步骤把玩家强度喂进训练集（全部脚本都在本目录）：

```bat
:: 1) 每局 10 人的 puuid（SGP 按 gameId 拉 SUMMARY，2,298 局约 1 分钟）
python fetch_game_summaries.py --workers 10

:: 2) 每位玩家当前段位分（LCU ranked-stats，3,077 人约 6 分钟）
python fetch_player_ranks.py --workers 8

:: 3) 两种强度口径各自落成特征列
python build_elo_features.py       :: 赛前 Elo（只用本局之前的对局）
python build_live_dataset_v4.py    :: v3 + ownElo/teamElo/enemyElo/eloDiff/ownEloVsTeam
python build_live_dataset_v5.py    :: v3 + ownRank/teamRank/enemyRank/rankDiff/ownRankVsTeam
```

三组同折、同超参的对比（2,298 局 / 579,790 行）：

| 模型 | 新增特征 | 特征数 | OOF AUC | LogLoss | Brier | Accuracy |
|---|---|---:|---:|---:|---:|---:|
| live_model3（基线） | — | 51 | 0.8286 | 0.5027 | 0.1688 | 73.85% |
| live_model4 | 赛前 Elo（5 列） | 56 | 0.8257 | 0.5068 | 0.1702 | 73.68% |
| **live_model5** | **当前段位分（5 列）** | 56 | **0.8553** | **0.4706** | **0.1553** | **76.92%** |

结论：

- **段位分是强信号**：单看 `rankDiff` 的局级 AUC 就有 **0.696**，进模型后排重要性第 3
  （仅次于 `killDiff`、`levelSumDiff`），AUC +0.027；
- **只靠场上 7.5 局的 Elo 是噪声**：Elo 单特征局级 AUC 仅 **0.53**，加进模型反而把 AUC 拉低到 0.8257。
  它需要足够长的历史（25 场以上）才有意义——这正是 `player_elo.py` 在**局前现场抓 25 场**的用途；
- 段位分是「今天」抓的，而数据集是 1–2 天前的对局：段位短期不会跳变，且**实时使用时就该用当前段位**，
  所以这个口径对上线是合理的（不是用未来信息作弊）。

#### 实时接入（已接上）

`advisor_server.py` 启动时会用 psutil 读 `LeagueClientUx.exe` 的 `--app-port/--remoting-auth-token`
（客户端提权也能读，不依赖 lockfile），对局开始后把 10 名玩家的段位解析出来：

```
allPlayers 的 riotId
  → /lol-summoner/v1/summoners?name=名字#TAG   （拿 puuid）
  → /lol-ranked/v1/ranked-stats/{puuid}        （拿 tier/division/LP）
  → rank_score()                                （Master 4000 / GM 5000 / Challenger 6000+LP）
```

- **模型自动选择**：段位齐全 → `live_model5`；缺 1–2 人 → 用本队已查到的人均值补齐
  （不影响队伍均值，`rankDiff` 仍准确）；不足 8 人 → 回退 `live_model3` 并打印原因；
- **缓存**：`rank_cache.json`，默认 6 小时，重开一局不会重复请求；
- **仪表盘**：新增「赛前实力（段位分）」卡片（我方均值 / 敌方均值 / 差值 / 自己），
  顶部状态栏显示当前模型；`/api/state` 新增 `model` 与 `rank` 字段；LLM 提示词里也会带段位差；
- 参数：`--no-ranks` 关闭段位解析，`--rank-cache PATH` 指定缓存文件。

实测（回放一局，8 人直查 + 2 人改名后用队伍均值补齐）：

| 模型 | 段位差 | 02:19 胜率 |
|---|---|---:|
| live_model3 | 不参与 | 59.4% |
| live_model5 | 我方 3142 vs 敌方 1681（+1461） | 85.8% |

### 桌面版（exe，Web 渲染主窗口）

`desktop_app.py` 是同一套采集/模型逻辑的**桌面版**（v2.3 起主窗口为 Web 渲染：Edge app
模式加载 `ui/next/shell.html` 单窗口外壳，左侧导航切换主页/选人助手/仪表盘三视图，
苹果风格浅色界面）。进程内起两个本地服务：home 服务（127.0.0.1:8789，serve shell 并注入
`window.LOL_SHELL` 配置、`GET /api/home` 按 `ui/next/contracts.md` 契约输出状态/设置/自检，
home 页注入 `window.lolAdvisor` 桥使入口改为视图切换）与选人助手服务（127.0.0.1:8788）。
`advisor_server.py` 仍然保留，供需要网页/接口的场景使用（外壳「仪表盘」视图内嵌它）。

```bat
python desktop_app.py             :: 直接运行（开发机，Edge 窗口加载主界面）
python desktop_app.py --demo      :: 静态演示模式（?demo=1，不开游戏预览新界面）
python desktop_app.py --selftest  :: 只检查模型能否加载/推理
build_exe.cmd                     :: 打包 -> dist\LoLAdvisor.exe + dist\LoLAdvisor-win64.zip
```

主界面内容：胜率环 + 走势曲线、对局状态（时钟/KDA/补刀/金币）、赛前实力（双方段位分与差值）、
团队差、建议、最近事件、设置与自检卡片、「打开选人助手」与「打开 Web 仪表盘」入口。

- 双击 exe 即可，对局开始后自动识别；段位解析、缺人补齐、partial-ranks 回退 `live_model3` 的逻辑与网页版一致；
- **观战/回放**：这两种模式 2999 不提供 `activePlayer`（返回 `{"error": "Spectator mode..."}`），
  程序会自动挑一个视角（优先当前登录账号，其次蓝方第一个玩家，可用 `--player` 指定），
  金币显示为「装备估算」；没有这个兜底时整屏会全是 0；
- 没进对局时界面显示「等待对局…」的空态，不会显示一排 0；
- 主窗口关闭后程序仍在后台运行（本地服务由它托管），彻底退出请在任务管理器结束 `LoLAdvisor.exe`；
- 排查：`LoLAdvisor.exe --diag` 会把进程可见性 / LCU 连接 / Edge / 界面文件状态写到 `diag.log`；
- LLM 建议可选：把 `advisor.llm.env` 放到 **exe 同目录** 就会自动启用，没有就只用规则建议；
- 段位缓存写在 `%USERPROFILE%\.lol-advisor\rank_cache.json`（6 小时有效）；
- 打包：PyInstaller **onedir**（`dist\LoLAdvisor\`，约 62 MB；onedir 启动快、不依赖 `%TEMP%` 解压），
  模型和 OP.GG 数据放在 `assets/`（`prepare_assets.py` 从 `dataset/` 复制），
  新界面整个 `ui/` 目录随包（`ui/design-system.css` + `ui/next/*.html` + `demo-data.js`）；
  `build_exe.cmd` 会顺带压成 `dist\LoLAdvisor-win64.zip`（约 24 MB）并在结尾自动跑 selftest；
- exe 自检：`dist\LoLAdvisor\LoLAdvisor.exe --selftest`，结果写在同目录 `selftest.log`；
- 预览界面：`LoLAdvisor.exe --demo`（静态演示模式，生产入口不带 `?demo=1`）。

## 选人助手（网页版，BP 阶段）

`champ_select_server.py` + `ui/next/champ_select.html`：选人阶段实时预估阵容胜率、按你的位置推荐英雄。
页面是苹果风格浅色界面的 BP 布局：左右镜像 5 路，顶部胜率条与倒计时，底部胜率曲线 + 推荐列表。

桌面版（exe）主界面点「打开选人助手」即可打开（程序内嵌本机服务 127.0.0.1:8788，
新窗口打开页面）。手动使用：

```bat
run-champ-select.cmd            :: 连 LCU，进 BP 自动刷新（自动打开 http://127.0.0.1:8788）
run-champ-select.cmd --demo     :: 演示模式（假 BP 循环，不看客户端也能预览布局）
```

- **数据实时性**：服务端直连 LCU WebSocket（`OnJsonApiEvent`），选人/锁英雄/ban 即时推送，
  页面用 SSE 接收并即时渲染，另有 4 秒轮询兜底；客户端重启后自动重连；
- **进游戏后**：冻结开局前最后阵容（不会回退到「等待」），并用 2999 Live Client Data
  （失败时退回 LCU gameflow teamOne/teamTwo）补全敌方英雄；
- **阵容胜率**：`train_draft_model.py` 用 2,298 局训练的逻辑回归（27 个特征，5 折 OOF **AUC 0.69**）：
  双方各位置的版本胜率差 / 对位胜率差 / 高分段英雄胜率差 / 段位分差；
- **推荐英雄**：把候选英雄替换进我方阵容重算胜率，再叠加个人最近 30 场该英雄战绩微调，
  每行给出「版本 / 对线 / 高分段 / 你最近」四类理由；
- **英雄中文名与头像**：来自 LCU，`build_champion_cache.py` 缓存 257 个到 `assets/champions.json`；
- 数据文件：`assets/draft_model.json`（模型与英雄统计）、`assets/champions.json`（id/中英文名）。

限制：

- 只用 BP 信息，不含场上局势，数字反映的是「纸面阵容强弱」，不是实时战况；
- 敌方未锁的英雄按中性处理，早期推荐主要按版本强度排序；
- 位置来自 `assignedPosition`，补位/未分配时会没有推荐。

## 回放 1 秒旁路（ROFL 硬解受阻后的方案）

ROFL 16.22 包解码被加壳卡住（见 `rofl-packet-notes.md`）。旁路：

1. 回放进程暴露 Live Client Data（2999），1 秒级拿到 10 名玩家的等级/KDA/补刀/装备/眼分/复活/事件；
2. 金币/位置/经验从 SGP 1 分钟时间线合并；
3. 标签（未来 30 秒击杀、未来 60 秒 gank）用完整事件表事后计算。

### 流程

```bat
:: 1. 启动回放（把 .rofl 放进客户端回放目录后）
::    LCU POST /lol-replays/v1/rofls/{gameId}/watch
:: 2. 1 秒采集（1x 播放，一局约等于对局时长）
run-replay-collect.cmd --output "...\akari-replay-live" --interval 1 --duration 1700

:: 3. 合并成数据集
run-replay-1s.cmd --snapshots "...\session_xxx" ^
  --timeline "...\timeline\timeline.json" ^
  --summary "...\summary.json" ^
  --output "...\replay_1s.csv"

:: 4. 训练击杀/gank 模型
run-replay-train.cmd --dataset "...\replay_1s.csv" --output "...\replay_model"
```

限制：

- 回放模式没有 `activePlayer`，没有地图坐标，没有金币；金币用装备价格+补刀估算，坐标/金币用 SGP 1 分钟补齐；
- 播放速度无法通过键盘热键改（1x），一局采集等于对局时长；
- 训练需要多局；2 局只能验证管线。

## 击杀 / Gank 事件模型（10 秒网格）

### 重要限制

SGP `DETAILS` 只有 **60 秒帧**。10 秒网格的做法是：

- 特征：把最近一分钟的 `gold/xp/level/cs/position` 前向填充到 10 秒；
- 标签：用事件真实时间戳，所以 10 秒级标签是准确的；
- 额外特征：距离上次击杀/gank/龙/塔的秒数、最近 60/180/300 秒事件数。

**这不是真正的 10 秒状态数据。** 真正 10 秒级特征需要 Live Client Data 实时采集，
或解包 replay packet。

### 生成数据集

```bat
run-event-dataset.cmd --export-root "...\akari-top300-exports" --interval 10
```

输出：

```
event_dataset/
├── event_dataset.csv
└── event_dataset_manifest.json
```

标签：

- `kill_30s`：未来 30 秒内任意击杀
- `blue_kill_30s` / `red_kill_30s`：未来 30 秒内蓝/红方击杀
- `blue_gank_60s` / `red_gank_60s`：未来 60 秒内蓝/红打野参与线上击杀

### 训练

```bat
run-event-train.cmd --dataset "...\event_dataset\event_dataset.csv"
```

2,298 局 / 326,054 个样本的结果：

| 标签 | 正样本率 | AP | 基线 AP | ROC-AUC | 最佳 F1 |
|---|---:|---:|---:|---:|---:|
| kill_30s | 0.523 | 0.6349 | 0.5226 | 0.6233 | 0.6926 |
| blue_kill_30s | 0.343 | 0.4409 | 0.3435 | 0.6114 | 0.5239 |
| red_kill_30s | 0.348 | 0.4498 | 0.3484 | 0.6142 | 0.5300 |
| blue_gank_60s | 0.316 | 0.3983 | 0.3160 | 0.6057 | 0.4970 |
| red_gank_60s | 0.321 | 0.4066 | 0.3213 | 0.6070 | 0.5033 |

AP 相对基线有 21%–29% 提升，但 ROC-AUC 只有 0.61 左右。说明有信号，
但 1 分钟状态 + 10 秒网格还不足以做强预测。

### 合成建议

```bat
run-recommend.cmd --dataset "...\event_dataset\event_dataset.csv" --game-id 901093877862 --t 1200 --side blue
```

输出示例：

```
[20:00] blue 视角
  击杀 30s: 我方 59.0% / 敌方 63.9%
  Gank 60s: 我方 50.1% / 敌方 55.9%
  经济差: -1144  经验差: -1377
  建议:
    - 敌方打野更可能来抓，注意后退、补视野，不要压线。
    - 大龙已进入可争夺时间，注意大龙区视野和抱团。
```

这是确定性规则层；下一步可以让 LLM 读取结构化模型输出 + 计时器 + 经济差，
生成更自然的解释。

## 概率校准

对 `oof_predictions.csv` 做 isotonic / Platt 校准，并按 gameId 交叉验证：

```bat
run-calibrate.cmd --oof "...\dataset\model_nochamp\oof_predictions.csv"
```

输出：

```
calibration/
├── calibration_report.json
├── reliability_curve.csv
├── calibrated_predictions.csv
├── calibrator_isotonic.json
├── calibrator_platt.json
└── summary.txt
```

在 2,298 局 / 55,709 行上的结果：

| 方法 | AUC | LogLoss | Brier | ECE |
|---|---:|---:|---:|---:|
| raw | 0.8223 | 0.5099 | 0.1710 | 0.0064 |
| isotonic | 0.8200 | 0.5249 | 0.1719 | 0.0044 |
| Platt | 0.8207 | 0.5120 | 0.1718 | 0.0071 |

结论：**原始概率已经校准得很好（ECE 0.0064），不建议再套校准器。**
isotonic 虽然把 ECE 降到 0.0044，但 Brier/LogLoss 变差；在校准曲线已经接近对角线时，
校准器只会拟合噪声。分分钟 ECE 偏高（0.02–0.09）主要是样本变少，不要按分钟单独校准。

注意：校准是**人群相关**的。如果以后混入普通段位或跨大区数据，需要在新人群上重新校准。

## 数据集（1 分钟颗粒度）

把多个导出目录合成一张按“一局一分钟一行”的训练表：

```bat
run-dataset.cmd --export-root "C:\Users\Administrator\Documents\akari-exports"
```

默认只保留 **排位峡谷（420/440、CLASSIC、map 11、时长 ≥ 10 分钟）**。

输出：

```
<export-root>/dataset/
├── dataset_blue.csv       # 一行一局一分钟，蓝方视角
├── dataset_sides.csv      # 蓝/红各一行（红方所有 diff 取反）
├── data_dictionary.md     # 字段说明
├── split.json             # 按 gameId 分组的 train/val/test
└── dataset_manifest.json  # 统计与跳过原因
```

字段：

- 标识：`gameId, minute, patch, queue_id, game_mode, side`
- 标签：`win, blue_win, gold_diff_plus5`
- 队伍级：`gold_diff, current_gold_diff, xp_diff, level_diff, cs_diff, kills_diff, tower_diff, dragon_diff, baron_diff, ...`
- 分位置：`gold_diff_TOP / JUNGLE / MIDDLE / BOTTOM / UTILITY` 等
- 英雄：`blue_champ_* / red_champ_*`（championId，训练时按 categorical）

时间规则：

- 第 1 分钟覆盖 `(0, 60s]`，第 2 分钟覆盖 `(60s, 120s]`。
- 特征只使用当前分钟及之前的数据。
- `gold_diff_plus5` 是 5 分钟后的经济差；最后一帧不足 5 分钟时为空。

示例：

```bat
run-dataset.cmd --export-root "...\exports" --queues 420,440
run-dataset.cmd --exports "...\LeagueAkari-A-..." "...\LeagueAkari-B-..."
```

## 时间线（事件 + 金币 / 经验）

导出时默认会自动生成 `timeline/`（可用 `--no-timeline` 关闭）。也可以对已有导出目录单独生成：

```bat
run-timeline.cmd --export "C:\Users\Administrator\Downloads\LeagueAkari\LeagueAkari-...-20261108-233745"
```

输出：

```
<export>/timeline/
├── timeline.json         # 帧 + 规范化事件（机器可读）
├── timeline_events.csv   # 每个事件一行，带击杀者/被击杀者的金币、经验、等级
├── timeline_frames.csv   # 每分钟、每名玩家的等级/当前金币/总金币/经验/补刀
├── timeline.md           # 人类可读：事件日志 + 每分钟经济表
└── summary.txt           # 事件类型计数
```

事件来源是 SGP `details.jsonl` 的 `frames[].events`，包含：

- 击杀 / 一血 / 多杀
- 购买、出售、撤销、消耗装备（装备名从客户端 `items.json` 查询）
- 插眼 / 排眼
- 推塔、镀层、水晶
- 小龙、大龙、先锋、巢虫、厄塔汗
- 升级、技能加点
- 龙魂、游戏结束

**金币与经验的对齐方式**：

- 每个事件会附上 `actorState` / `victimState`，取该事件时间点**之前最近的一帧**（每分钟一帧）的
  `level / currentGold / totalGold / xp / cs`，并记录 `frameTimestamp`。
- 因为是每分钟一帧，这个值**最多可能滞后约 60 秒**，不代表事件瞬间的精确数值；需要精确的分钟级数据时
  直接看 `timeline_frames.csv`。
- CSV 里的 `actorFrameTime` / `victimFrameTime` 标明这份状态来自哪一帧。
- `timeline_frames.csv` 保存所有玩家每一分钟的快照，不依赖事件。
- 事件时间戳来自 SGP，单位毫秒；`time` 是 `mm:ss`。

示例（`timeline.md`）：

```
- `[01:47]` **击杀** — 此人是黑户#12713 (Yasuo) 击杀 少吃碳水多吃肉#55920 (Ahri)
  - 此人是黑户#12713: Lv 7 · 当前金币 500 · 总金币 3500 · 经验 4200 · CS 80
  - 少吃碳水多吃肉#55920: Lv 6 · 当前金币 300 · 总金币 2800 · 经验 3600 · CS 65
```

## 解包回放（ROFL2）

新版 `.rofl` 是 ROFL2 容器：`RIOT` + version 2 + 游戏版本 + 18 字节 chunk 头 +
zstd chunk + 256 字节签名 + 尾部 JSON 元数据。解包命令：

```bat
run-unpack.cmd "C:\Users\Administrator\Downloads\LeagueAkari\...\replays\901126500254.rofl"
```

指定输出目录：

```bat
run-unpack.cmd "D:\replays\xxx.rofl" --output "D:\replay-unpacked"
```

输出：

```
<replay>_unpacked/
├── metadata.json      # gameLength / lastGameChunkId / statsJson / stats
├── stats.json         # 10 名玩家的最终结算数据
├── chunks.json        # 95 个 chunk 的 id/type/offset/长度/sha256
├── chunks/            # 解压后的 chunk（startup / keyframe / game-data）
├── blocks.jsonl.gz    # 每个 packet 块：chunkId、timestamp、packetId、param、payload 预览
├── packets.json       # 按 packetId 聚合的次数/字节/时间范围
├── summary.txt        # 人类可读摘要
└── manifest.json
```

解包层次：

1. **容器层**：头、chunk 表、zstd 解压、尾部元数据 —— 已完整解出。
2. **块层**：每个块的 timestamp / packetId / param / payload —— 已解出；
   `packets.json` 是聚合，`blocks.jsonl.gz` 是明细。
3. **语义层**：把 payload 映射成伤害、装备、技能、击杀等事件 —— 需要与
   `League of Legends.exe` 完全匹配的 per-patch schema。本工具**不做**这一层；
   需要时可用 [rofl-parser](https://github.com/shyunku-libraries/rofl-parser)
   配合当前版本的 `League of Legends.exe` 生成解码器。

注意：

- `timestamp` 是 **chunk 内相对时间**；keyframe / startup 块通常从 0 开始，
  game-data 块才有连续时间线。
- `blocks.jsonl.gz` 未压缩时约 400 MB（30 分钟对局），gzip 后通常几十 MB；
  不需要时加 `--no-blocks`，只要 chunk 文件时加 `--no-chunks`。
- 解包只读取 `.rofl`，不会修改客户端文件。

## 工作原理

1. 用 `psutil` 读取 `LeagueClientUx.exe` 命令行，解析
   `--app-port` / `--remoting-auth-token` / `--region` / `--rso_platform_id`。
2. 用 `riot:<token>` Basic Auth 访问 `https://127.0.0.1:<port>`。
3. 读取 `/lol-summoner/v1/current-summoner` 和 `/entitlements/v1/token`。
4. 根据 region / rsoPlatformId 选择 SGP 端点，用 entitlements token 请求
   `/match-history-query/v1/products/lol/player/<puuid>/SUMMARY`。
5. 按 `DETAILS` 接口并发拉取每场详情。
6. `--replays` 时调用客户端自己的 `lol-replays` 下载接口，轮询到 `watch` 后，
   从客户端回放目录复制 `.rofl` 到输出目录（不会删除客户端原文件）。

## 常见问题

**找不到客户端**

> 未找到正在运行的英雄联盟客户端（LeagueClientUx.exe）。

先启动客户端并登录。若客户端以管理员权限运行而当前 cmd 不是，尝试用管理员
权限打开 cmd。

**SGP 证书错误**

加 `--insecure-sgp` 重试。

**提示不支持某个 SGP 服务器**

用 `--server` 手动指定，例如国服 `TENCENT_TJ100`。工具内置的服务器列表与
League Akari 1.5.1 一致。

**回放下载失败**

- 回放只对客户端仍保留的对局有效，太老的对局会 `incompatible` 或 404。
- 100 场回放可能有数 GB，建议先只导数据。
- 失败项会写进 `errors.json`，不会中断其他对局。

**SGP 没有返回战绩**

国服部分大区在晚间可能不稳定，稍后重试；或先用 League Akari 本体确认该账号
能正常加载战绩。
