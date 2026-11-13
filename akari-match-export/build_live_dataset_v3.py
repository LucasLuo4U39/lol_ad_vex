#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 OP.GG 英雄/对位胜率拼进 live_dataset.csv，生成 live_dataset_v3.csv。

新增特征（对每行"玩家视角"）：
- own_champ_wr    自己英雄在其分路的版本胜率（缺省 0.5）
- enemy_champ_wr  对位敌方英雄的分路胜率（缺省 0.5）
- matchup_wr      自己英雄 vs 对位英雄的对位胜率（OP.GG counters 页原值，缺省 0.5）
- matchup_games_log  log1p(对位样本场次)
- team_wr_diff    我方 5 人分路胜率均值 - 敌方均值
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_features import POSITION_MAP, normalize_champ  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path,
                        default=Path(__file__).resolve().parent.parent / "dataset" / "live_dataset" / "live_dataset.csv")
    parser.add_argument("--game-champs", type=Path,
                        default=Path(__file__).resolve().parent / "game_champs.json")
    parser.add_argument("--opgg", type=Path,
                        default=Path(__file__).resolve().parent / "opgg_stats.json")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent.parent / "dataset" / "live_dataset" / "live_dataset_v3.csv")
    args = parser.parse_args(argv)

    dataframe = pd.read_csv(args.dataset)
    print(f"rows={len(dataframe)} games={dataframe['gameId'].nunique()}")

    rosters = json.loads(args.game_champs.read_text(encoding="utf-8"))
    opgg = json.loads(args.opgg.read_text(encoding="utf-8"))
    stats: Dict[str, Dict[str, Dict[str, float]]] = opgg.get("stats") or {}
    matchups: Dict[str, Dict[str, Dict[str, Dict[str, object]]]] = opgg.get("matchups") or {}
    print(f"roster 局数={len(rosters)} OP.GG patch={opgg.get('patch')}")

    def champ_wr(pos: str, champ: str) -> float:
        entry = stats.get(pos, {}).get(normalize_champ(champ))
        return float(entry["winRate"]) if entry else 0.5

    def matchup_entry(pos: str, own: str, enemy: str) -> Optional[Dict[str, object]]:
        return matchups.get(pos, {}).get(normalize_champ(own), {}).get(normalize_champ(enemy))

    own_wr = np.full(len(dataframe), 0.5)
    enemy_wr = np.full(len(dataframe), 0.5)
    matchup_wr = np.full(len(dataframe), 0.5)
    matchup_games_log = np.zeros(len(dataframe))
    team_wr_diff = np.zeros(len(dataframe))
    missing_games: set = set()
    skipped = 0

    for idx in dataframe.index:
        game_id = str(int(dataframe.at[idx, "gameId"]))
        roster = rosters.get(game_id)
        if not roster or not roster.get("100") or not roster.get("200"):
            missing_games.add(game_id)
            skipped += 1
            continue
        winner = roster.get("winner", 100)
        loser = 200 if winner == 100 else 100
        role = dataframe.at[idx, "role"]
        own_win = int(dataframe.at[idx, "ownWin"])
        pos = POSITION_MAP.get(role)
        if not pos:
            skipped += 1
            continue
        side = str(winner if own_win else loser)
        other = str(loser if own_win else winner)

        own_champ = roster[side].get(role) or ""
        enemy_champ = roster[other].get(role) or ""
        if not own_champ or not enemy_champ:
            skipped += 1
            continue

        own_wr[idx] = champ_wr(pos, own_champ)
        enemy_wr[idx] = champ_wr(pos, enemy_champ)
        entry = matchup_entry(pos, own_champ, enemy_champ)
        if entry:
            matchup_wr[idx] = float(entry["winRate"])
            matchup_games_log[idx] = np.log1p(float(entry.get("games") or 0))

        own_avg = 0.0
        enemy_avg = 0.0
        for r, p in POSITION_MAP.items():
            c_own = roster[side].get(r)
            c_enemy = roster[other].get(r)
            if c_own:
                own_avg += champ_wr(p, c_own)
            if c_enemy:
                enemy_avg += champ_wr(p, c_enemy)
        team_wr_diff[idx] = (own_avg - enemy_avg) / 5.0

    dataframe["own_champ_wr"] = own_wr
    dataframe["enemy_champ_wr"] = enemy_wr
    dataframe["matchup_wr"] = matchup_wr
    dataframe["matchup_games_log"] = matchup_games_log
    dataframe["team_wr_diff"] = team_wr_diff
    print(f"无阵容信息的对局: {len(missing_games)}（这些局的行将被丢弃）")

    # 没有阵容的行无法提供英雄/对位特征，直接丢弃而不是用中性值污染训练
    drop_mask = dataframe["gameId"].astype(str).isin(missing_games)
    dropped_rows = int(drop_mask.sum())
    dataframe = dataframe.loc[~drop_mask].reset_index(drop=True)
    print(f"丢弃行数: {dropped_rows} / {len(dataframe) + dropped_rows}")

    dataframe.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"输出: {args.output} ({args.output.stat().st_size/1024/1024:.0f} MB)")
    print("新特征统计:")
    print(dataframe[["own_champ_wr", "enemy_champ_wr", "matchup_wr", "team_wr_diff"]].describe().round(4).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
