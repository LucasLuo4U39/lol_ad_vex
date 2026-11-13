#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 live_dataset_v3 加「玩家当前段位分」特征，产出 live_dataset_v5。

比 Elo 更直接的「人的强度」：Challenger/GM/Master 的段位分差，来自
`fetch_player_ranks.py`（LCU ranked-stats，3,077 名玩家，6 分钟抓完）。

新增列：
- `ownRank`：这一行代表玩家的段位分（Master 4000 / GM 5000 / Challenger 6000+LP）；
- `teamRank` / `enemyRank`：队伍 5 人平均；
- `rankDiff`：teamRank - enemyRank；
- `ownRankVsTeam`：ownRank - teamRank（大腿/短板指数）。

行 → 玩家：`ownWin` 决定胜/负方，`role` 即分路，与 `elo_player_games.csv`
（gameId, puuid, team, position）对齐。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DATASET_V3 = BASE.parent / "dataset" / "live_dataset" / "live_dataset_v3.csv"
PLAYER_GAMES = BASE.parent / "analysis" / "elo_player_games.csv"
RANKS = BASE.parent / "analysis" / "player_ranks.json"
CHAMPS = BASE / "game_champs.json"
OUT = BASE.parent / "dataset" / "live_dataset" / "live_dataset_v5.csv"
DEFAULT_RANK = 4000.0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="生成带段位特征的训练集")
    parser.add_argument("--v3", type=Path, default=DATASET_V3)
    parser.add_argument("--player-games", type=Path, default=PLAYER_GAMES)
    parser.add_argument("--ranks", type=Path, default=RANKS)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    print("读取数据集 v3 ...")
    data = pd.read_csv(args.v3, dtype={"gameId": str, "role": str})
    print(f"  {len(data)} 行 / {data['gameId'].nunique()} 局")

    ranks: Dict[str, dict] = json.loads(args.ranks.read_text(encoding="utf-8"))
    rank_score = {puuid: float(item.get("score") or DEFAULT_RANK) for puuid, item in ranks.items()}

    # (gameId, team, position) -> puuid
    players = pd.read_csv(args.player_games, dtype={"gameId": str, "puuid": str, "position": str})
    puuid_of: Dict[Tuple[str, str, str], str] = {
        (str(row.gameId), str(row.team), str(row.position)): str(row.puuid)
        for row in players.itertuples(index=False)
    }

    champs = json.loads(CHAMPS.read_text(encoding="utf-8"))
    winner_by_game = {gid: int(entry.get("winner") or 100) for gid, entry in champs.items()}

    own_team = []
    for game_id, own_win in zip(data["gameId"], data["ownWin"]):
        winner = winner_by_game.get(game_id, 100)
        own_team.append(winner if int(own_win) == 1 else 300 - winner)
    data["ownTeam"] = own_team

    # 先按 (game, team, role) 取每位玩家的分数，再算队伍均值
    per_row: list[float] = []
    team_sum: Dict[Tuple[str, int], float] = {}
    team_count: Dict[Tuple[str, int], int] = {}
    for game_id, team, role in zip(data["gameId"], data["ownTeam"], data["role"]):
        puuid = puuid_of.get((game_id, str(team), str(role)))
        score = rank_score.get(puuid, DEFAULT_RANK) if puuid else DEFAULT_RANK
        per_row.append(score)
        key = (game_id, int(team))
        team_sum[key] = team_sum.get(key, 0.0) + score
        team_count[key] = team_count.get(key, 0) + 1

    team_avg = {key: team_sum[key] / max(1, team_count[key]) for key in team_sum}

    own: list[float] = []
    team_rank: list[float] = []
    enemy_rank: list[float] = []
    for game_id, team, score in zip(data["gameId"], data["ownTeam"], per_row):
        own.append(score)
        team_rank.append(team_avg.get((game_id, int(team)), DEFAULT_RANK))
        enemy_rank.append(team_avg.get((game_id, 300 - int(team)), DEFAULT_RANK))

    data["ownRank"] = own
    data["teamRank"] = team_rank
    data["enemyRank"] = enemy_rank
    data["rankDiff"] = data["teamRank"] - data["enemyRank"]
    data["ownRankVsTeam"] = data["ownRank"] - data["teamRank"]

    coverage = (data["ownRank"] != DEFAULT_RANK).mean()
    data.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"写出 {args.out}（{len(data)} 行，{len(data.columns)} 列）")
    print(
        f"段位分覆盖 {coverage:.1%}；rankDiff 标准差 {data['rankDiff'].std():.1f}；"
        f"ownRankVsTeam 标准差 {data['ownRankVsTeam'].std():.1f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
