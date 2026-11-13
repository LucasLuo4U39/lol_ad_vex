#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""胜率模型归因引擎：逐特征置回中性值，用 lgbmini 重预测得 ΔwinProb。

用法：
    from attribution import attribute_win_prob, build_neutral_row, FEATURE_CN
    top3 = attribute_win_prob(model, row, feature_names)   # -> [{key, delta}]
    cn = FEATURE_CN.get(top3[0]["key"], top3[0]["key"])

中性值口径（依据，逐条写明）：
- rank 等级类（ownRank/teamRank/enemyRank）→ 4000：与训练侧 build_live_dataset_v5
  的 DEFAULT_RANK=4000 及 feature_row 缺省一致；
- rank 差值类（rankDiff/ownRankVsTeam）→ 0：训练数据缺人时差值为 0，
  填 4000 会注入极端离群值（feature_row 缺省一致）；
- since_*（9 个）→ 9999：训练侧编码「从未发生」的中性哨兵
  （build_live_dataset.py 的 since()），不是 0（0 表示「刚发生」）；
- *_last_* 计数 → 0：无事件的中性值（与 feature_row 缺省一致）；
- 胜率类（own_champ_wr/enemy_champ_wr/matchup_wr）→ 0.5：训练侧查不到
  OP.GG 条目时的缺省中性；matchup_games_log → 0（log1p(0)）；team_wr_diff → 0；
- 差值类（*Diff）→ 0：双方对称中性（训练均值实测亦为 0）；
- 数值观测类（minute/ownLevel/ownGold/ownCS/ownKills/ownDeaths/ownAssists/
  团队与敌方各合计）→ 该特征训练均值：重预测扰动最小的中性值；
- role（分类）→ 0（BOTTOM 码）：与 feature_row 对未知 role 的缺省一致
  （各分路样本数相等，均为 115,958，无占优类别）。

TRAIN_MEANS 计算方式：live_dataset.csv 全部 579,790 行的列均值
（2026-11-12 用 compute 脚本复核，见 attribution.py --self-test）。

自检：python -B attribution.py --self-test
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DATASET_DIR = BASE.parent / "dataset" / "live_dataset"

# 训练均值（live_dataset.csv，579,790 行，2026-11-12 计算）——数值观测类的中性值。
TRAIN_MEANS: Dict[str, float] = {
    "minute": 13.848876,
    "ownLevel": 8.732012,
    "ownGold": 585.013783,
    "ownCS": 88.477074,
    "ownKills": 2.401347,
    "ownDeaths": 2.410621,
    "ownAssists": 3.117053,
    "teamKills": 12.006735,
    "teamDeaths": 12.053105,
    "teamAssists": 15.585264,
    "teamCS": 442.385372,
    "teamLevelSum": 43.660058,
    "enemyKills": 12.006735,
    "enemyDeaths": 12.053105,
    "enemyAssists": 15.585264,
    "enemyCS": 442.385372,
    "enemyLevelSum": 43.660058,
}

# 语义中性值（与上面「中性值口径」逐条对应）。
NEUTRAL_SEMANTIC: Dict[str, float] = {
    "ownRank": 4000.0,
    "teamRank": 4000.0,
    "enemyRank": 4000.0,
    "rankDiff": 0.0,
    "ownRankVsTeam": 0.0,
    "own_champ_wr": 0.5,
    "enemy_champ_wr": 0.5,
    "matchup_wr": 0.5,
    "matchup_games_log": 0.0,
    "team_wr_diff": 0.0,
}

# 差值类（对称中性 0；训练均值实测为 0）。
DIFF_KEYS = (
    "killDiff", "deathDiff", "assistDiff", "csDiff", "levelSumDiff",
    "dragonDiff", "baronDiff", "heraldDiff", "grubDiff", "atakhanDiff",
    "towerDiff", "inhibitorDiff",
)

# since_*（训练侧「从未发生」哨兵 9999）。
SINCE_KEYS = (
    "since_kill", "since_own_kill", "since_enemy_kill",
    "since_gank", "since_own_gank", "since_enemy_gank",
    "since_dragon", "since_baron", "since_tower",
)

# 计数类（无事件中性 0）。
COUNT_KEYS = (
    "kills_last_60", "kills_last_180",
    "own_kills_last_180", "enemy_kills_last_180",
    "ganks_last_300", "own_ganks_last_300", "enemy_ganks_last_300",
)

FEATURE_CN: Dict[str, str] = {
    "minute": "对局分钟",
    "role": "分路",
    "ownLevel": "自己等级",
    "ownGold": "自己金币",
    "ownCS": "自己补刀",
    "ownKills": "自己击杀",
    "ownDeaths": "自己死亡",
    "ownAssists": "自己助攻",
    "teamKills": "团队击杀",
    "teamDeaths": "团队死亡",
    "teamAssists": "团队助攻",
    "teamCS": "团队补刀",
    "teamLevelSum": "团队等级和",
    "enemyKills": "敌方击杀",
    "enemyDeaths": "敌方死亡",
    "enemyAssists": "敌方助攻",
    "enemyCS": "敌方补刀",
    "enemyLevelSum": "敌方等级和",
    "killDiff": "击杀差",
    "deathDiff": "死亡差",
    "assistDiff": "助攻差",
    "csDiff": "补刀差",
    "levelSumDiff": "等级和差",
    "dragonDiff": "小龙差",
    "baronDiff": "大龙差",
    "heraldDiff": "先锋差",
    "grubDiff": "巢虫差",
    "atakhanDiff": "厄塔汗差",
    "towerDiff": "防御塔差",
    "inhibitorDiff": "水晶差",
    "since_kill": "距上次击杀秒数",
    "since_own_kill": "距上次我方击杀秒数",
    "since_enemy_kill": "距上次敌方击杀秒数",
    "since_gank": "距上次 gank 秒数",
    "since_own_gank": "距上次我方打野 gank 秒数",
    "since_enemy_gank": "距上次敌方打野 gank 秒数",
    "since_dragon": "距上次小龙秒数",
    "since_baron": "距上次大龙秒数",
    "since_tower": "距上次推塔秒数",
    "kills_last_60": "近 60 秒击杀数",
    "kills_last_180": "近 180 秒击杀数",
    "own_kills_last_180": "我方近 180 秒击杀数",
    "enemy_kills_last_180": "敌方近 180 秒击杀数",
    "ganks_last_300": "近 300 秒 gank 数",
    "own_ganks_last_300": "我方近 300 秒 gank 数",
    "enemy_ganks_last_300": "敌方近 300 秒 gank 数",
    "own_champ_wr": "自己英雄版本胜率",
    "enemy_champ_wr": "对位英雄版本胜率",
    "matchup_wr": "对位胜率",
    "matchup_games_log": "对位样本量(log)",
    "team_wr_diff": "阵容胜率差",
    "ownRank": "自己段位分",
    "teamRank": "队伍段位分",
    "enemyRank": "敌方段位分",
    "rankDiff": "段位分差",
    "ownRankVsTeam": "自己相对队伍段位",
}


def neutral_value(name: str) -> float:
    """单特征中性值（口径见模块 docstring）。"""
    if name == "role":
        return 0.0
    if name in NEUTRAL_SEMANTIC:
        return NEUTRAL_SEMANTIC[name]
    if name in TRAIN_MEANS:
        return TRAIN_MEANS[name]
    if name in DIFF_KEYS or name in COUNT_KEYS:
        return 0.0
    if name in SINCE_KEYS:
        return 9999.0
    return 0.0


def build_neutral_row(feature_names: Sequence[str]) -> List[float]:
    """按模型特征顺序构造中性行。"""
    return [neutral_value(name) for name in feature_names]


def attribute_all(
    model: Any,
    row: Sequence[float],
    feature_names: Sequence[str],
    neutral_row: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    """逐特征置回中性值并重预测，返回按 |Δ| 降序的完整归因。

    delta = 原始概率 - 该特征置中性后的概率（正值=该特征当前取值拉高胜率）。
    """
    row_array = np.asarray(row, dtype=np.float64)
    if row_array.ndim != 1 or len(row_array) != len(feature_names):
        raise ValueError("row 长度必须等于 feature_names 长度")
    neutral = (
        list(neutral_row)
        if neutral_row is not None
        else build_neutral_row(feature_names)
    )
    if len(neutral) != len(feature_names):
        raise ValueError("neutral_row 长度必须等于 feature_names 长度")
    base = float(model.predict_proba(row_array.reshape(1, -1))[0])
    items: List[Dict[str, Any]] = []
    for index, name in enumerate(feature_names):
        if abs(float(row[index]) - float(neutral[index])) < 1e-9:
            continue  # 已处于中性值，无归因可做
        changed = row_array.copy()
        changed[index] = float(neutral[index])
        probability = float(model.predict_proba(changed.reshape(1, -1))[0])
        delta = base - probability
        items.append(
            {
                "key": name,
                "delta": round(delta, 5),
                # T58：同时给出 pp（百分点）版本，前端不必再猜单位
                "pp": round(delta * 100.0, 2),
                "value": float(row[index]),
                "neutral": float(neutral[index]),
            }
        )
    items.sort(key=lambda item: -abs(item["delta"]))
    return {"winProb": round(base, 5), "attribution": items}


def attribute_win_prob(
    model: Any,
    feature_row: Sequence[float],
    feature_names: Sequence[str],
    neutral_row: Optional[Sequence[float]] = None,
) -> List[Dict[str, Any]]:
    """|Δ| 前 3 项 [{key, delta, pp}]（delta 为概率差，如 0.021=+2.1pp；pp 为百分数版）。"""
    result = attribute_all(model, feature_row, feature_names, neutral_row)
    top = result["attribution"][:3]
    return [
        {"key": item["key"], "delta": item["delta"], "pp": item.get("pp", round(item["delta"] * 100.0, 2))}
        for item in top
    ]


# --------------------------------------------------------------------------
# 选人模型（线性 sigmoid）归因
# --------------------------------------------------------------------------
FEATURE_CN_DRAFT: Dict[str, str] = {
    "opgg_TOP": "上单版本胜率差",
    "mu_TOP": "上单对位胜率",
    "cohort_TOP": "上单高分段胜率差",
    "rank_TOP": "上单段位差",
    "opgg_JUNGLE": "打野版本胜率差",
    "mu_JUNGLE": "打野对位胜率",
    "cohort_JUNGLE": "打野高分段胜率差",
    "rank_JUNGLE": "打野段位差",
    "opgg_MIDDLE": "中单版本胜率差",
    "mu_MIDDLE": "中单对位胜率",
    "cohort_MIDDLE": "中单高分段胜率差",
    "rank_MIDDLE": "中单段位差",
    "opgg_BOTTOM": "下路版本胜率差",
    "mu_BOTTOM": "下路对位胜率",
    "cohort_BOTTOM": "下路高分段胜率差",
    "rank_BOTTOM": "下路段位差",
    "opgg_UTILITY": "辅助版本胜率差",
    "mu_UTILITY": "辅助对位胜率",
    "cohort_UTILITY": "辅助高分段胜率差",
    "rank_UTILITY": "辅助段位差",
    "rank_avg": "平均段位差",
    "own_opgg": "我方版本强度",
    "enemy_opgg": "敌方版本强度",
    "own_cohort": "我方高分段强度",
    "enemy_cohort": "敌方高分段强度",
    "own_rank": "我方段位均值",
    "enemy_rank": "敌方段位均值",
}


def draft_probability(model: Dict[str, Any], x: Sequence[float]) -> float:
    """draft_model.json 的 model 块 + 原始特征行 → sigmoid 概率。

    与 champ_select_server.DraftModel.probability 同一公式：
    z = Σ ((x_i − mean_i)/scale_i)·coef_i + intercept；p = 1/(1+e^(−z))。
    """
    names = model["features"]
    mean = model["mean"]
    scale = model["scale"]
    coef = model["coef"]
    intercept = float(model.get("intercept") or 0.0)
    if not (len(x) == len(names) == len(mean) == len(scale) == len(coef)):
        raise ValueError("x 长度必须等于模型 features/mean/scale/coef 长度")
    z = sum(
        (float(xi) - float(mi)) / float(si) * float(ci)
        for xi, mi, si, ci in zip(x, mean, scale, coef)
    ) + intercept
    return 1.0 / (1.0 + math.exp(-z))


def attribute_linear(
    model: Dict[str, Any],
    x: Sequence[float],
) -> Dict[str, Any]:
    """选人线性 sigmoid 模型归因（无近似误差）。

    方法：线性模型各特征无交互，第 i 特征对 logit 的贡献
      c_i = ((x_i − mean_i)/scale_i)·coef_i
    （mean_i 即标准化中性点，z_i=0 为中性）。把该特征置回中性后
      p_minus_i = sigmoid(z − c_i)，delta_i = p − p_minus_i。
    因 sigmoid 单调，delta_i 的符号与大小排序和 c_i 完全一致；正值 =
    该特征当前取值把胜率拉高（如 0.021 = +2.1pp）。
    """
    names = model["features"]
    mean = model["mean"]
    scale = model["scale"]
    coef = model["coef"]
    intercept = float(model.get("intercept") or 0.0)
    if not (len(x) == len(names) == len(mean) == len(scale) == len(coef)):
        raise ValueError("x 长度必须等于模型 features/mean/scale/coef 长度")
    contributions = [
        (float(xi) - float(mi)) / float(si) * float(ci)
        for xi, mi, si, ci in zip(x, mean, scale, coef)
    ]
    z = sum(contributions) + intercept
    probability = 1.0 / (1.0 + math.exp(-z))
    items = []
    for index, name in enumerate(names):
        contribution = contributions[index]
        delta = probability - (1.0 / (1.0 + math.exp(-(z - contribution))))
        items.append(
            {
                "key": name,
                "contribution": round(contribution, 5),
                "delta": round(delta, 5),
                "pp": round(delta * 100.0, 2),
                "value": float(x[index]),
                "neutral": float(mean[index]),
            }
        )
    items.sort(key=lambda item: -abs(item["delta"]))
    return {
        "winProb": round(probability, 5),
        "intercept": intercept,
        "attribution": items,
    }


def attribute_linear_top(
    model: Dict[str, Any],
    x: Sequence[float],
    k: int = 3,
) -> List[Dict[str, Any]]:
    """线性归因 |Δ| 前 k 项 [{key, delta, pp}]（供 champ_select 页展示）。"""
    result = attribute_linear(model, x)
    top = result["attribution"][:k]
    return [
        {"key": item["key"], "delta": item["delta"], "pp": item.get("pp", round(item["delta"] * 100.0, 2))}
        for item in top
    ]


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def build_probe_features() -> Dict[str, Any]:
    """构造偏离中性的确定性特征（含段位差/击杀差/OP.GG 对位优势）。"""
    from live_features import extract_features

    players = []
    roles = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    champs_own = ["Garen", "LeeSin", "Ahri", "Jinx", "Thresh"]
    champs_enemy = ["Kennen", "Khazix", "Syndra", "Caitlyn", "Lulu"]
    for index in range(5):
        players.append(
            {
                "riotId": f"B{index}#1", "summonerName": f"B{index}#1", "team": "ORDER",
                "position": roles[index], "level": 13,
                "rawChampionName": f"game_character_displayname_{champs_own[index]}",
                "scores": {"kills": 6, "deaths": 2, "assists": 8, "creepScore": 220},
            }
        )
        players.append(
            {
                "riotId": f"R{index}#1", "summonerName": f"R{index}#1", "team": "CHAOS",
                "position": roles[index], "level": 12,
                "rawChampionName": f"game_character_displayname_{champs_enemy[index]}",
                "scores": {"kills": 3, "deaths": 5, "assists": 5, "creepScore": 180},
            }
        )
    events = [
        {"EventID": 1, "EventName": "ChampionKill", "EventTime": 60.0,
         "KillerName": "LeeSin", "VictimName": "Syndra", "Assisters": ["Garen"]},
        {"EventID": 2, "EventName": "DragonKill", "EventTime": 300.0, "KillerName": "LeeSin"},
        {"EventID": 3, "EventName": "TurretKilled", "EventTime": 420.0, "KillerName": "Jinx"},
        {"EventID": 4, "EventName": "ChampionKill", "EventTime": 840.0,
         "KillerName": "Jinx", "VictimName": "Lulu", "Assisters": ["Thresh"]},
    ]
    payload = {
        "activePlayer": {"summonerName": "B2#1", "currentGold": 1500, "level": 13},
        "allPlayers": players,
        "gameData": {"gameTime": 900},
        "events": {"Events": events},
    }
    opgg = {
        "stats": {
            "mid": {"ahri": {"winRate": 0.54}},
            "jungle": {"leesin": {"winRate": 0.52}, "khazix": {"winRate": 0.50}},
            "top": {"garen": {"winRate": 0.51}, "kennen": {"winRate": 0.49}},
            "adc": {"jinx": {"winRate": 0.53}, "caitlyn": {"winRate": 0.50}},
            "support": {"thresh": {"winRate": 0.52}, "lulu": {"winRate": 0.50}},
        },
        "matchups": {"mid": {"ahri": {"syndra": {"winRate": 0.56, "games": 800}}}},
    }
    ranks = {f"B{index}#1": 5200.0 for index in range(5)}
    ranks.update({f"R{index}#1": 4000.0 for index in range(5)})
    return extract_features(payload, opgg, ranks)


def run_selftest() -> int:
    from live_features import feature_row
    from lgbmini import LgbTextModel

    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}  {detail}")

    print("[1] 中性值口径")
    names5 = LgbTextModel(DATASET_DIR / "live_model5" / "model.txt").feature_names
    neutral5 = build_neutral_row(names5)
    by_name = dict(zip(names5, neutral5))
    check("ownRank/teamRank/enemyRank 中性 4000", all(by_name[k] == 4000.0 for k in ("ownRank", "teamRank", "enemyRank")), str(by_name.get("ownRank")))
    check("rankDiff/ownRankVsTeam 中性 0", by_name["rankDiff"] == 0.0 and by_name["ownRankVsTeam"] == 0.0)
    check("since_* 中性 9999", all(by_name[k] == 9999.0 for k in SINCE_KEYS), str(by_name.get("since_kill")))
    check("计数类中性 0", all(by_name[k] == 0.0 for k in COUNT_KEYS))
    check("胜率类中性 0.5", all(by_name[k] == 0.5 for k in ("own_champ_wr", "enemy_champ_wr", "matchup_wr")), str(by_name.get("matchup_wr")))
    check("差值类中性 0", all(by_name[k] == 0.0 for k in DIFF_KEYS))
    check("minute 中性=训练均值 13.8489", abs(by_name["minute"] - 13.848876) < 1e-9, str(by_name["minute"]))
    check("FEATURE_CN 覆盖 model5 全部 56 特征", all(k in FEATURE_CN for k in names5), str([k for k in names5 if k not in FEATURE_CN]))

    print("[2] 归因输出形状与稳定性")
    features = build_probe_features()
    model5 = LgbTextModel(DATASET_DIR / "live_model5" / "model.txt")
    row5 = feature_row(features, names5)
    top3 = attribute_win_prob(model5, row5, names5)
    check("attribute_win_prob 返回 3 项", len(top3) == 3, str(top3))
    check("|Δ| 降序", abs(top3[0]["delta"]) >= abs(top3[1]["delta"]) >= abs(top3[2]["delta"]), str(top3))
    top3_again = attribute_win_prob(model5, row5, names5)
    check("重复运行结果一致", top3 == top3_again, f"{top3} vs {top3_again}")
    print(f"  model5 winProb={attribute_all(model5, row5, names5)['winProb']:.4f} top3={top3}")

    print("[3] 归因一致性：top3 |Δ| 占比 + 与 feature_importance 重合")
    full = attribute_all(model5, row5, names5)["attribution"]
    total_abs = sum(abs(item["delta"]) for item in full)
    top3_abs = sum(abs(item["delta"]) for item in full[:3])
    ratio = top3_abs / max(total_abs, 1e-12)
    top3_str = ["%s=%+.4f" % (item["key"], item["delta"]) for item in full[:3]]
    print(f"  top3/total |Δ| = {ratio:.3f}（top3={top3_str}）")
    check("top3 占全部 |Δ| 之比 >= 0.25", ratio >= 0.25, str(ratio))
    importance_path = DATASET_DIR / "live_model5" / "feature_importance.csv"
    if importance_path.is_file():
        import csv as csv_module

        with importance_path.open("r", encoding="utf-8-sig", newline="") as handle:
            top_importance = [row["feature"] for row in list(csv_module.DictReader(handle))[:10]]
        overlap = {item["key"] for item in top3} & set(top_importance)
        check("top3 归因与 feature_importance 前 10 重合 >= 1", len(overlap) >= 1, f"overlap={overlap} importance={top_importance}")
        print(f"  overlap={overlap}")
    else:
        print(f"  （无 feature_importance.csv，跳过重合检查: {importance_path}）")

    print("[4] model3 归因冒烟")
    model3 = LgbTextModel(DATASET_DIR / "live_model3" / "model.txt")
    names3 = model3.feature_names
    row3 = feature_row(features, names3)
    top3_v3 = attribute_win_prob(model3, row3, names3)
    check("model3 top3 返回 3 项", len(top3_v3) == 3, str(top3_v3))
    print(f"  model3 top3={top3_v3}")

    print("[5] 线性 sigmoid 归因（draft 模型）")
    tiny = {
        "features": ["a", "b"],
        "mean": [0.0, 0.0],
        "scale": [1.0, 1.0],
        "coef": [1.0, 0.5],
        "intercept": 0.0,
    }
    p_tiny = draft_probability(tiny, [1.0, 2.0])
    check(
        "合成模型概率=sigmoid(2)=0.8808",
        abs(p_tiny - (1.0 / (1.0 + math.exp(-2.0)))) < 1e-9,
        str(p_tiny),
    )
    att_tiny = attribute_linear(tiny, [1.0, 2.0])
    check("贡献与系数一致（a=1.0, b=1.0）", [i["contribution"] for i in att_tiny["attribution"]] == [1.0, 1.0], str(att_tiny))
    check("delta 符号与 contribution 一致", all(i["delta"] * i["contribution"] > 0 for i in att_tiny["attribution"]), str(att_tiny["attribution"]))
    check("全部置中性后概率=sigmoid(intercept)=0.5", abs(1.0 / (1.0 + math.exp(-0.0)) - 0.5) < 1e-9)
    check("top3 与 |contribution| 排序一致", attribute_linear_top(tiny, [1.0, 2.0], 2)[0]["key"] in ("a", "b"))

    draft_path = BASE / "assets" / "draft_model.json"
    if draft_path.is_file():
        payload = json.loads(draft_path.read_text(encoding="utf-8"))
        model = payload["model"]
        # 构造与 champ_select.features_for 同口径的满阵容特征行（段位差 +1000）
        roles = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
        x = []
        for _role in roles:
            x += [0.02, 0.03, 0.01, 10.0]  # opgg 差 / mu-0.5 / cohort 差 / rank 差(÷100)
        x += [10.0, 0.02, -0.01, 0.01, -0.005, 5.0, 4.0]
        p_draft = draft_probability(model, x)
        att = attribute_linear(model, x)
        top3_draft = attribute_linear_top(model, x)
        check("draft 特征行长度=27", len(x) == len(model["features"]) == 27, str(len(x)))
        check("draft 概率在 (0,1)", 0.0 < p_draft < 1.0, str(p_draft))
        z_check = sum(i["contribution"] for i in att["attribution"]) + att["intercept"]
        check(
            "Σ贡献+截距=z（自洽，允许 5 位小数舍入）",
            abs(z_check - math.log(p_draft / (1.0 - p_draft))) < 1e-3,
            f"diff={z_check - math.log(p_draft / (1.0 - p_draft)):.6f}",
        )
        check("delta 与 contribution 同号", all(i["delta"] * i["contribution"] > 0 for i in att["attribution"]), str(att["attribution"][:2]))
        check("FEATURE_CN_DRAFT 覆盖 27 特征", all(name in FEATURE_CN_DRAFT for name in model["features"]), str([n for n in model["features"] if n not in FEATURE_CN_DRAFT]))
        check("段位差贡献进 top3（rank_avg）", "rank_avg" in {i["key"] for i in top3_draft}, str(top3_draft))
        print(f"  draft winProb={p_draft:.4f} top3={top3_draft}")
        check("与树模型路径互不影响（attribute_win_prob 仍可用）", len(top3) == 3, str(top3))
    else:
        print(f"  （跳过：{draft_path} 不存在）")

    print(f"\n结果: PASS={passed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(run_selftest())
    print("用法: python -B attribution.py --self-test")
    raise SystemExit(2)
