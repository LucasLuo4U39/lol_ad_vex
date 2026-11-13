#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 2,298 局的玩家阵容算「赛前队伍 Elo」，产出可直接并入训练集的 CSV。

输入：`analysis/game_summaries.jsonl`（fetch_game_summaries.py 的产物，含每局 10 个 puuid）
输出：
- `analysis/elo_features.csv`：gameId, creation, elo100, elo200, eloDiff, eloProb
- `analysis/elo_players.json`：每个 puuid 的最终评分（排查用）

口径：
- 按 gameCreation 时间顺序重放，**每局只用这局之前的评分**（无未来信息）；
- 新玩家 1500 起，前 10 场 K=48，之后 K=32；
- 队伍评分 = 5 人平均；`eloProb = 1/(1+10^(-Δ/400))`。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DEFAULT_SUMMARIES = BASE.parent / "analysis" / "game_summaries.jsonl"
DEFAULT_OUT = BASE.parent / "analysis" / "elo_features.csv"
DEFAULT_PLAYERS = BASE.parent / "analysis" / "elo_players.json"

INITIAL = 1500.0
K = 32.0
K_PROVISIONAL = 48.0
PROVISIONAL_GAMES = 10
SCALE = 400.0


def load_games(path: Path) -> List[Dict[str, Any]]:
    games: Dict[str, Dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if len(record.get("players") or []) != 10:
                continue
            games[str(record["gameId"])] = record
    return sorted(games.values(), key=lambda r: (r.get("creation") or 0, r["gameId"]))


def expected(a: float, b: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(a - b) / SCALE))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="从对局阵容算赛前队伍 Elo")
    parser.add_argument("--summaries", type=Path, default=DEFAULT_SUMMARIES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--players-out", type=Path, default=DEFAULT_PLAYERS)
    args = parser.parse_args(argv)

    games = load_games(args.summaries)
    print(f"对局 {len(games)} 局")

    ratings: Dict[str, float] = {}
    played: Dict[str, int] = defaultdict(int)
    rows = []
    player_rows = []
    skipped = 0
    for record in games:
        teams: Dict[int, List[str]] = {100: [], 200: []}
        winners = set()
        others: Dict[str, str] = {}
        for player in record["players"]:
            team = int(player["team"])
            if team not in teams:
                continue
            teams[team].append(player["puuid"])
            others[player["puuid"]] = player.get("position") or ""
            if player.get("win"):
                winners.add(team)
        if len(teams[100]) != 5 or len(teams[200]) != 5 or len(winners) != 1:
            skipped += 1
            continue
        winner = winners.pop()

        for puuid in teams[100] + teams[200]:
            ratings.setdefault(puuid, INITIAL)

        blue = sum(ratings[p] for p in teams[100]) / 5
        red = sum(ratings[p] for p in teams[200]) / 5
        rows.append(
            {
                "gameId": record["gameId"],
                "creation": int(record.get("creation") or 0),
                "elo100": round(blue, 2),
                "elo200": round(red, 2),
                "eloDiff": round(blue - red, 2),
                "eloProb": round(expected(blue, red), 6),
            }
        )
        for team in (100, 200):
            for puuid in teams[team]:
                player_rows.append(
                    {
                        "gameId": record["gameId"],
                        "puuid": puuid,
                        "team": team,
                        "position": others.get(puuid, ""),
                        "eloBefore": round(ratings[puuid], 2),
                    }
                )

        for team, score in ((100, 1.0 if winner == 100 else 0.0), (200, 0.0 if winner == 100 else 1.0)):
            team_expected = expected(blue, red) if team == 100 else 1.0 - expected(blue, red)
            for puuid in teams[team]:
                k = K_PROVISIONAL if played[puuid] < PROVISIONAL_GAMES else K
                ratings[puuid] += k * (score - team_expected)
                played[puuid] += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        handle.write("gameId,creation,elo100,elo200,eloDiff,eloProb\n")
        for row in rows:
            handle.write(
                f"{row['gameId']},{row['creation']},{row['elo100']},{row['elo200']},"
                f"{row['eloDiff']},{row['eloProb']}\n"
            )

    players = {puuid: round(rating, 1) for puuid, rating in ratings.items()}
    players_out = args.players_out
    player_games_out = players_out.with_name("elo_player_games.csv")
    with player_games_out.open("w", encoding="utf-8", newline="") as handle:
        handle.write("gameId,puuid,team,position,eloBefore\n")
        for row in player_rows:
            handle.write(
                f"{row['gameId']},{row['puuid']},{row['team']},{row['position']},{row['eloBefore']}\n"
            )
    print(f"每局选手 Elo -> {player_games_out}（{len(player_rows)} 行）")

    args.players_out.write_text(
        json.dumps(
            {
                "players": players,
                "games": len(rows),
                "skipped": skipped,
                "meanGamesPerPlayer": round(sum(played.values()) / max(1, len(played)), 2),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    diffs = [row["eloDiff"] for row in rows]
    print(f"写出 {len(rows)} 局（跳过 {skipped}）-> {args.out}")
    print(f"玩家 {len(players)} 人，人均 {sum(played.values()) / max(1, len(played)):.1f} 局")
    print(f"Elo 差: 最小 {min(diffs):.0f} / 中位 {sorted(diffs)[len(diffs)//2]:.0f} / 最大 {max(diffs):.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
