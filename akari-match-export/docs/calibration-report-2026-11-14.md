# 校准与训练口径实测报告（2026-11-14）

数据来源：原始 `live_dataset.csv`（2,298 局 / 579,790 行），用当前代码重建
`live_dataset_v3/v4/v5` 后训练。三条变体都在同一份数据、同一套 5 折 `GroupKFold(gameId)`
下完成；区间来自按局 bootstrap（200 次）。

## 结果

| 变体 | AUC [95% CI] | Brier | ECE | LogLoss | 第 1 分钟 AUC |
|---|---|---|---|---|---|
| A 旧口径（含段位、不等权） | 0.8551 [0.8416, 0.8682] | 0.1555 | **0.0081** | 0.4709 | 0.700 |
| B 等权（含段位） | 0.8554 [0.8421, 0.8676] | 0.1557 | 0.0192 | 0.4714 | 0.700 |
| C 等权 + 去段位 | 0.8292 [0.8176, 0.8420] | 0.1689 | 0.0192 | 0.5028 | 0.549 |
| 已发布 live_model5 | 0.8553 | 0.1553 | — | 0.4706 | — |
| 已发布 live_model3 | 0.8286 | 0.1688 | — | 0.5027 | — |

复现结论：A ≈ 已发布 model5、C ≈ 已发布 model3，说明重建的 v3/v4/v5 特征口径正确。

## 三个结论

1. **按局等权没有收益，反而伤害校准。** AUC 0.8551 → 0.8554（区间完全重叠），
   Brier 0.1555 → 0.1557，但 ECE 0.0081 → 0.0192（2.4 倍，两个区间基本不重叠）。
   因此 `train_live_model.py` 的默认改为**不等权**，`--game-weight` 仅在实验时开启。
2. **model5 相对 model3 的 +2.6pp AUC 全部来自段位特征。** 去段位后 AUC 0.8292、
   Brier 0.1689，与 model3（0.8286 / 0.1688）重合。其中大部分是合法的「赛前实力」信号
   （线上用赛前段位，可复现），小部分来自「训练用赛后快照段位」的时点污染，幅度约为
   一局 LP 变化；因此 model5 可以上线，但 0.855 应标注为「含段位特征」。
3. **开局并非掷硬币。** 含段位时第 1 分钟 AUC 0.70；去掉段位后降到 0.549。
   之前「第 1 分钟 0.52」的说法只适用于无段位特征的旧模型。

## 校准（isotonic，按局交叉拟合）

| 模型 | 原始 ECE | isotonic 后 ECE | Brier 变化 |
|---|---|---|---|
| A 旧口径 | 0.0081 | 0.0033 | +0.0003（略差） |
| B 等权 | 0.0192 | 0.0015 | −0.0002 |
| C 去段位 | 0.0192 | 0.0015 | −0.0003 |

原始概率已经校准得不错（ECE 0.008），**不建议上线校准器**；只有开启等权时才需要用
isotonic 把校准拉回来。

## 分分钟

- 第 1–5 分钟 ECE 0.014–0.031，AUC 0.70–0.77（含段位）；
- 分钟级 ECE 中位数 0.025，最大值 0.30 出现在 40 分钟以后（样本极少），不建议按分钟单独校准。

## 复现命令

```bat
:: 1) 基础数据集 -> v3/v4/v5（需要 exports 或原始 live_dataset.csv）
python build_live_dataset_v3.py --dataset live_dataset.csv --output live_dataset_v3.csv
python build_elo_features.py --summaries ..\analysis\game_summaries.jsonl --out ..\analysis\elo_features.csv --players-out ..\analysis\elo_players.json
python build_live_dataset_v4.py --v3 live_dataset_v3.csv --elo-games ..\analysis\elo_features.csv --elo-players ..\analysis\elo_player_games.csv --out live_dataset_v4.csv
python build_live_dataset_v5.py --v3 live_dataset_v3.csv --player-games ..\analysis\elo_player_games.csv --ranks ..\analysis\player_ranks.json --out live_dataset_v5.csv

:: 2) 三种口径
python train_live_model.py --dataset live_dataset_v5.csv --output model_a_old --patch 16.22 --bootstrap 200
python train_live_model.py --dataset live_dataset_v5.csv --output model_b_weighted --patch 16.22 --game-weight --bootstrap 200
python train_live_model.py --dataset live_dataset_v5.csv --output model_c_norank --patch 16.22 --drop-rank-features --bootstrap 200

:: 3) 校准复评
python evaluate_calibration.py --predictions model_a_old\oof_predictions.csv --out calib_a.json --apply isotonic
```
