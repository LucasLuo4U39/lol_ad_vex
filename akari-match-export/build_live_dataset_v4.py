#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 live_dataset_v3 加「选手 Elo」特征，产出 live_dataset_v4。

新增列（全部是**赛前**信息，不含本局结果）：
- `ownElo`：这一行代表的那名玩家的赛前 Elo；
- `teamElo` / `enemyElo`：所在队伍/对手队伍 5 人平均 Elo；
- `eloDiff`：teamElo - enemyElo；
- `ownEloVsTeam`：ownElo - teamElo（"大腿/短板"指数）。

行 → 玩家 的映射：`ownWin` 决定行属于胜方还是负方，`role` 即分路，
与 `game_summaries.jsonl` 里的 `teamPosition` 对齐。

用法：
    python build_live_dataset_v4.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Tuple

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DATASET_V3 = BASE.parent / "dataset" / "live_dataset" / "live_dataset_v3.csv"
ELO_GAMES = BASE.parent / "analysis" / "elo_features.csv"
ELO_PLAYERS = BASE.parent / "analysis" / "elo_player_games.csv"
CHAMPS = BASE / "game_champs.json"
OUT = BASE.parent / "dataset" / "live_dataset" / "live_dataset_v4.csv"


def load_elo_players(path: Path) -> Dict[Tuple[str, str, str], float]:
    frame = pd.read_csv(path, dtype={"gameId": str, "puuid": str, "position": str})
    mapping: Dict[Tuple[str, str, str], float] = {}
    for row in frame.itertuples(index=False):
        mapping[(str(row.gameId), str(row.team), str(row.position))] = float(row.eloBefore)
    return mapping


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成带选手 Elo 的训练集")
    parser.add_argument("--v3", type=Path, default=DATASET_V3)
    parser.add_argument("--elo-games", type=Path, default=ELO_GAMES)
    parser.add_argument("--elo-players", type=Path, default=ELO_PLAYERS)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    print("读取数据集 v3 ...")
    data = pd.read_csv(args.v3, dtype={"gameId": str, "role": str})
    print(f"  {len(data)} 行 / {data['gameId'].nunique()} 局")

    elo_games = pd.read_csv(args.elo_games, dtype={"gameId": str})
    team_elo = {
        str(row.gameId): (float(row.elo100), float(row.elo200), float(row.eloDiff))
        for row in elo_games.itertuples(index=False)
    }
    player_elo = load_elo_players(args.elo_players)
    champs = json.loads(CHAMPS.read_text(encoding="utf-8"))

    winner_by_game = {gid: int(entry.get("winner") or 100) for gid, entry in champs.items()}

    own_team = []
    for game_id, own_win in zip(data["gameId"], data["ownWin"]):
        winner = winner_by_game.get(game_id, 100)
        own_team.append(winner if int(own_win) == 1 else 300 - winner)
    data["ownTeam"] = own_team

    own = []
    team_avg = []
    enemy_avg = []
    diff = []
    for game_id, side, role in zip(data["gameId"], data["ownTeam"], data["role"]):
        blue, red, delta = team_elo.get(game_id, (1500.0, 1500.0, 0.0))
        own_elo = player_elo.get((game_id, str(side), str(role)), 1500.0)
        team = blue if side == 100 else red
        enemy = red if side == 100 else blue
        own.append(own_elo)
        team_avg.append(team)
        enemy_avg.append(enemy)
        diff.append(team - enemy)

    data["ownElo"] = own
    data["teamElo"] = team_avg
    data["enemyElo"] = enemy_avg
    data["eloDiff"] = diff
    data["ownEloVsTeam"] = data["ownElo"] - data["teamElo"]

    coverage = (data["ownElo"] != 1500.0).mean()
    data.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"写出 {args.out}（{len(data)} 行，{len(data.columns)} 列）")
    print(f"ownElo 非默认覆盖率 {coverage:.1%}；eloDiff 标准差 {data['eloDiff'].std():.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
