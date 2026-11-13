#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""特征对齐回归测试：训练/推理特征口径一致性（T6 修复的正式化验证）。

只依赖标准库 + numpy（lgbmini 需要 numpy），捆绑 Python 可直接跑：
    python -B validate_features_alignment.py

校验内容：
1. 合成 events.Events（击杀/gank/龙/大龙/塔、窗口边界、召唤师名/英雄名归队、
   打野判定）喂 extract_features，16 个 since_*/last_* 特征与手算口径一致
   （since=0 表刚发生、9999 表从未发生；(t-窗口, t] 左开右闭）。
2. feature_row 对 live_model3(51)/live_model5(56)/live_event_model(46)/
   model_base(48) 的 feature_names 全列覆盖；段位完整时零缺失。
3. rank 特征缺失时缺省 ownRank/teamRank/enemyRank=4000、rankDiff/ownRankVsTeam=0、
   其余 0。
4. rank_fallback_required：partial ranks=True，完整/未提供=False。
5. validate_opgg_stats：同 patch 无告警，异 patch/空数据/缺 fetchedAt 有告警。

不写任何文件（含 __pycache__，配合 -B）；只读模型 .txt 与 metrics.json。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

BASE = Path(__file__).resolve().parent
DATASET = BASE.parent / "dataset" / "live_dataset"
DATASET_V2 = BASE.parent / "dataset" / "dataset_v2"

sys.path.insert(0, str(BASE))
from live_features import (  # noqa: E402
    extract_features,
    feature_row,
    rank_fallback_required,
    validate_opgg_stats,
)
from lgbmini import LgbTextModel  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0
RANK_KEYS = {"ownRank", "teamRank", "enemyRank", "rankDiff", "ownRankVsTeam"}
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def build_payload(game_time: float = 250.0, events: List[dict] | None = None) -> dict:
    """10 人 payload：ORDER=B 队、CHAOS=R 队；打野 LeeSin(ORDER)/Khazix(CHAOS)。"""
    champs_order = ["Garen", "LeeSin", "Ahri", "Jinx", "Thresh"]
    champs_chaos = ["Kennen", "Khazix", "Syndra", "Caitlyn", "Lulu"]
    players: List[dict] = []
    for i in range(5):
        players.append(
            {
                "riotId": f"B{i}#1",
                "summonerName": f"B{i}#1",
                "team": "ORDER",
                "position": ROLES[i],
                "level": 6,
                "rawChampionName": f"game_character_displayname_{champs_order[i]}",
                "scores": {"kills": i, "deaths": 0, "assists": 1, "creepScore": 50},
            }
        )
        players.append(
            {
                "riotId": f"R{i}#1",
                "summonerName": f"R{i}#1",
                "team": "CHAOS",
                "position": ROLES[i],
                "level": 6,
                "rawChampionName": f"game_character_displayname_{champs_chaos[i]}",
                "scores": {"kills": 0, "deaths": 1, "assists": 1, "creepScore": 45},
            }
        )
    if events is None:
        events = [
            {"EventID": 0, "EventName": "GameStart", "EventTime": 0.0},
            # 60s：ORDER 打野(英雄名 LeeSin)杀敌方中单，己方上单助攻 -> own kill + own gank
            {"EventID": 1, "EventName": "ChampionKill", "EventTime": 60.0,
             "KillerName": "LeeSin", "VictimName": "Syndra", "Assisters": ["Garen"]},
            # 90s：CHAOS 打野(英雄名 Khazix)杀我方中单 -> enemy kill + enemy gank
            {"EventID": 2, "EventName": "ChampionKill", "EventTime": 90.0,
             "KillerName": "Khazix", "VictimName": "Ahri", "Assisters": ["Kennen"]},
            {"EventID": 3, "EventName": "DragonKill", "EventTime": 100.0,
             "KillerName": "LeeSin"},
            {"EventID": 4, "EventName": "TurretKilled", "EventTime": 110.0,
             "KillerName": "Jinx"},
            # 200s：我方 ADC 杀敌方辅助（无打野参与）-> own kill，无 gank
            {"EventID": 5, "EventName": "ChampionKill", "EventTime": 200.0,
             "KillerName": "Jinx", "VictimName": "Lulu", "Assisters": ["Thresh"]},
            {"EventID": 6, "EventName": "BaronKill", "EventTime": 250.0,
             "KillerName": "Syndra"},
        ]
    return {
        "activePlayer": {"summonerName": "B2#1", "currentGold": 800, "level": 6},
        "allPlayers": players,
        "gameData": {"gameTime": game_time},
        "events": {"Events": events},
    }


def full_ranks() -> Dict[str, float]:
    ranks = {f"B{i}#1": 5000.0 for i in range(5)}
    ranks.update({f"R{i}#1": 4000.0 for i in range(5)})
    return ranks


# --------------------------------------------------------------------------
# [1] 事件时序特征口径（手算对照）
# --------------------------------------------------------------------------
print("[1] 事件时序特征口径（t=250，击杀 60/90/200，龙 100，塔 110，大龙 250）")
f = extract_features(build_payload())
expected = {
    "since_kill": 50.0, "since_own_kill": 50.0, "since_enemy_kill": 160.0,
    "since_gank": 160.0, "since_own_gank": 190.0, "since_enemy_gank": 160.0,
    "since_dragon": 150.0, "since_baron": 0.0, "since_tower": 140.0,
    "kills_last_60": 1, "kills_last_180": 2, "own_kills_last_180": 1,
    "enemy_kills_last_180": 1, "ganks_last_300": 2, "own_ganks_last_300": 1,
    "enemy_ganks_last_300": 1,
}
for name, want in expected.items():
    check(f"{name}={want}", abs(float(f[name]) - float(want)) < 1e-9, str(f[name]))

print("[1a] 窗口边界：(t-w, t] 左开右闭")
boundary = build_payload(
    game_time=250.0,
    events=[
        {"EventName": "ChampionKill", "EventTime": 190.0,
         "KillerName": "Jinx", "VictimName": "Lulu", "Assisters": []},
        {"EventName": "ChampionKill", "EventTime": 250.0,
         "KillerName": "Caitlyn", "VictimName": "Jinx", "Assisters": []},
    ],
)
fb = extract_features(boundary)
check("t=190 不计入 kills_last_60（开区间）", fb["kills_last_60"] == 1, str(fb["kills_last_60"]))
check("t=250 计入（闭区间）且 since_kill=0", fb["since_kill"] == 0.0, str(fb["since_kill"]))

print("[1b] 召唤师名（无 #TAG）归队")
summoner_events = build_payload(
    game_time=100.0,
    events=[
        {"EventName": "ChampionKill", "EventTime": 70.0,
         "KillerName": "R0", "VictimName": "B2", "Assisters": []},
    ],
)
fs = extract_features(summoner_events)
check("tag-less 召唤师名归到敌方", abs(fs["since_enemy_kill"] - 30.0) < 1e-9, str(fs["since_enemy_kill"]))
check("我方无击杀 since_own_kill=9999", fs["since_own_kill"] == 9999.0)

print("[1c] 空事件：since=9999 / count=0")
f0 = extract_features(build_payload(events=[]))
check("since_kill=9999", f0["since_kill"] == 9999.0, str(f0["since_kill"]))
check("ganks_last_300=0", f0["ganks_last_300"] == 0)

# --------------------------------------------------------------------------
# [2] feature_row 全列覆盖
# --------------------------------------------------------------------------
print("[2] feature_row 覆盖各模型 feature_names")
f_full = extract_features(build_payload(), None, full_ranks())
model_entries: List[tuple] = [
    (DATASET / "live_model3" / "model.txt", 51, True),
    (DATASET / "live_model5" / "model.txt", 56, True),
    (DATASET / "live_event_model" / "model_own_kill_30s.txt", 46, True),
    (DATASET / "live_event_model" / "model_enemy_kill_30s.txt", 46, True),
    (DATASET / "live_event_model" / "model_own_gank_60s.txt", 46, True),
    (DATASET / "live_event_model" / "model_enemy_gank_60s.txt", 46, True),
]
for path, want_cols, has_model_txt in model_entries:
    if has_model_txt and not path.is_file():
        check(f"{path.parent.name}/{path.name} 存在", False, str(path))
        continue
    model = LgbTextModel(path)
    row = feature_row(f, model.feature_names)
    check(
        f"{path.parent.name}/{path.name}: 行覆盖 {len(model.feature_names)} 列",
        len(row) == want_cols == len(model.feature_names),
        f"row={len(row)} want={want_cols}",
    )
    missing = [n for n in model.feature_names if n not in f_full and n != "role"]
    check(
        f"{path.parent.name}/{path.name}: 段位完整时特征零缺失",
        not missing,
        f"missing={missing}",
    )

# model_base（1 分钟模型）只有 metrics.json 记录 featureColumns（无 model.txt 入库）
base_metrics = json.loads((DATASET_V2 / "model_base" / "metrics.json").read_text(encoding="utf-8"))
base_names = base_metrics["featureColumns"]
check("model_base featureColumns=48", len(base_names) == 48, str(len(base_names)))
base_row = feature_row(f, base_names)
check("model_base: 行覆盖 48 列（缺列按 0 缺省补齐）", len(base_row) == 48)
check(
    "model_base: 不在 extract_features 里的列缺省为 0",
    all(base_row[base_names.index(n)] == 0.0 for n in ("gold_diff", "xp_diff", "patch")),
    str([base_row[base_names.index(n)] for n in ("gold_diff", "xp_diff", "patch")]),
)

# --------------------------------------------------------------------------
# [3] 缺省值规则
# --------------------------------------------------------------------------
print("[3] rank 特征缺省值（未提供段位时 model5 视角）")
names5 = LgbTextModel(DATASET / "live_model5" / "model.txt").feature_names
row5 = feature_row(f, names5)
check("ownRank 缺省 4000", row5[names5.index("ownRank")] == 4000.0)
check("teamRank 缺省 4000", row5[names5.index("teamRank")] == 4000.0)
check("enemyRank 缺省 4000", row5[names5.index("enemyRank")] == 4000.0)
check("rankDiff 缺省 0", row5[names5.index("rankDiff")] == 0.0)
check("ownRankVsTeam 缺省 0", row5[names5.index("ownRankVsTeam")] == 0.0)
check(
    "未知特征名缺省 0",
    feature_row(f, ["nope_feature"])[0] == 0.0,
    str(feature_row(f, ["nope_feature"])[0]),
)
check(
    "空字符串按缺省处理（rankDiff→0 而非报错）",
    feature_row({"rankDiff": ""}, ["rankDiff"])[0] == 0.0,
)
check(
    "显式 0 值原样保留",
    feature_row({"kills_last_60": 0}, ["kills_last_60"])[0] == 0.0,
)

# --------------------------------------------------------------------------
# [4] partial-ranks 回退信号
# --------------------------------------------------------------------------
print("[4] rank_fallback_required 信号")
f_none = extract_features(build_payload())
check("未提供段位: _rankProvided=False", f_none["_rankProvided"] is False)
check("未提供段位: fallback=False", rank_fallback_required(f_none) is False)
check("10 人段位: _rankComplete=True", f_full["_rankComplete"] is True)
check("10 人段位: fallback=False", rank_fallback_required(f_full) is False)
check("10 人段位: ownRank=5000", f_full["ownRank"] == 5000.0)

partial = full_ranks()
partial.pop("R4#1")
f_partial = extract_features(build_payload(), None, partial)
check("缺 1 人: _rankProvided=True", f_partial["_rankProvided"] is True)
check("缺 1 人: _rankComplete=False", f_partial["_rankComplete"] is False)
check("缺 1 人: fallback=True", rank_fallback_required(f_partial) is True)
check("缺 1 人: 不产出 rankDiff", "rankDiff" not in f_partial)

# --------------------------------------------------------------------------
# [5] validate_opgg_stats
# --------------------------------------------------------------------------
print("[5] validate_opgg_stats")
fresh_now = datetime.now(timezone.utc).astimezone().isoformat()
opgg_ok = {"patch": "16.22", "fetchedAt": fresh_now, "stats": {}, "matchups": {}}
w_ok = validate_opgg_stats(opgg_ok, current_patch="16.22")
check("同 patch 无告警", w_ok == [], str(w_ok))
w_bad = validate_opgg_stats(opgg_ok, current_patch="16.23")
check("异 patch 有告警", any("不一致" in x for x in w_bad), str(w_bad))
w_none = validate_opgg_stats({})
check("空数据有告警", any("缺失或为空" in x for x in w_none), str(w_none))
w_nofetch = validate_opgg_stats({"patch": "16.22"})
check("缺 fetchedAt 有告警", any("fetchedAt" in x for x in w_nofetch), str(w_nofetch))

print()
print(f"结果: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
