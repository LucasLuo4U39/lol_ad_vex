#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a Live Client Data-feasible win dataset from SGP exports.

Live Client Data exposes:
  * your own current gold
  * every player's champion, level, KDA, CS, items, ward score, team
  * team objectives / tower events

It does NOT expose other players' gold or map positions. So this dataset uses
only observables:

  own: level, gold, CS, KDA
  team/enemy: kills, deaths, assists, CS sum, level sum
  objectives: dragons, barons, heralds, grubs, atakhans, towers, inhibitors

One row = one player perspective at one minute. Label = that player's team win.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import build_dataset as dataset

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

METRICS = (
    "kills",
    "deaths",
    "assists",
    "dragons",
    "barons",
    "heralds",
    "grubs",
    "atakhans",
    "towers",
    "inhibitors",
)

COLUMNS = [
    "gameId",
    "minute",
    "role",
    "ownWin",
    "ownLevel",
    "ownGold",
    "ownCS",
    "ownKills",
    "ownDeaths",
    "ownAssists",
    "teamKills",
    "teamDeaths",
    "teamAssists",
    "teamCS",
    "teamLevelSum",
    "enemyKills",
    "enemyDeaths",
    "enemyAssists",
    "enemyCS",
    "enemyLevelSum",
    "killDiff",
    "deathDiff",
    "assistDiff",
    "csDiff",
    "levelSumDiff",
    "dragonDiff",
    "baronDiff",
    "heraldDiff",
    "grubDiff",
    "atakhanDiff",
    "towerDiff",
    "inhibitorDiff",
    "since_kill",
    "since_own_kill",
    "since_enemy_kill",
    "since_gank",
    "since_own_gank",
    "since_enemy_gank",
    "since_dragon",
    "since_baron",
    "since_tower",
    "kills_last_60",
    "kills_last_180",
    "own_kills_last_180",
    "enemy_kills_last_180",
    "ganks_last_300",
    "own_ganks_last_300",
    "enemy_ganks_last_300",
    "own_kill_30s",
    "enemy_kill_30s",
    "own_gank_60s",
    "enemy_gank_60s",
]


def event_bucket(timestamp: Any) -> int:
    try:
        return (int(timestamp) + 59999) // 60000
    except (TypeError, ValueError):
        return -1


def add(array: List[int], index: int) -> None:
    if 0 <= index < len(array):
        array[index] += 1


def build_counters(
    timeline: Dict[str, Any],
    players_team: Dict[int, int],
    max_minute: int,
) -> Tuple[Dict[Tuple[int, str], List[int]], Dict[Tuple[int, str], List[int]]]:
    team_counters: Dict[Tuple[int, str], List[int]] = {}
    player_counters: Dict[Tuple[int, str], List[int]] = {}

    def bump(
        mapping: Dict[Tuple[int, str], List[int]],
        key: Tuple[int, str],
        bucket: int,
    ) -> None:
        if bucket < 0 or bucket > max_minute:
            return
        if key not in mapping:
            mapping[key] = [0] * (max_minute + 1)
        mapping[key][bucket] += 1

    for event in timeline.get("events") or []:
        event_type = event.get("type")
        bucket = event_bucket(event.get("timestamp"))
        if bucket < 0:
            continue

        if event_type == "CHAMPION_KILL":
            killer_id = event.get("actorId")
            victim_id = event.get("victimId")
            killer_team = players_team.get(killer_id) if isinstance(killer_id, int) else None
            victim_team = players_team.get(victim_id) if isinstance(victim_id, int) else None

            if killer_team in (100, 200):
                bump(team_counters, (killer_team, "kills"), bucket)
            if victim_team in (100, 200):
                bump(team_counters, (victim_team, "deaths"), bucket)

            if isinstance(killer_id, int):
                bump(player_counters, (killer_id, "kills"), bucket)
            if isinstance(victim_id, int):
                bump(player_counters, (victim_id, "deaths"), bucket)

            for assist_id in event.get("assistIds") or []:
                if not isinstance(assist_id, int):
                    continue
                assist_team = players_team.get(assist_id)
                if assist_team in (100, 200):
                    bump(team_counters, (assist_team, "assists"), bucket)
                bump(player_counters, (assist_id, "assists"), bucket)

        elif event_type == "ELITE_MONSTER_KILL":
            team = event.get("teamId")
            if team not in (100, 200):
                actor_id = event.get("actorId")
                team = players_team.get(actor_id) if isinstance(actor_id, int) else None
            if team not in (100, 200):
                continue

            mapping = {
                "DRAGON": "dragons",
                "BARON_NASHOR": "barons",
                "RIFTHERALD": "heralds",
                "HORDE": "grubs",
                "ATAKHAN": "atakhans",
            }
            metric = mapping.get(event.get("monsterType"))
            if metric:
                bump(team_counters, (team, metric), bucket)

        elif event_type == "BUILDING_KILL":
            team = event.get("teamId")
            if team not in (100, 200):
                actor_id = event.get("actorId")
                team = players_team.get(actor_id) if isinstance(actor_id, int) else None
            if team not in (100, 200):
                continue

            building_type = event.get("buildingType")
            if building_type == "TOWER_BUILDING":
                bump(team_counters, (team, "towers"), bucket)
            elif building_type == "INHIBITOR_BUILDING":
                bump(team_counters, (team, "inhibitors"), bucket)

    return team_counters, player_counters


def cumulative(
    counters: Dict[Tuple[int, str], List[int]],
    key: Tuple[int, str],
    minute: int,
) -> int:
    values = counters.get(key)
    if not values:
        return 0
    return sum(values[: minute + 1])


def seconds(timestamp: Any) -> float:
    try:
        return float(timestamp) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def since(times: List[float], timestamp: float) -> float:
    index = bisect.bisect_right(times, timestamp) - 1
    return 9999.0 if index < 0 else timestamp - times[index]


def count_between(times: List[float], start: float, end: float) -> int:
    return bisect.bisect_right(times, end) - bisect.bisect_right(times, start)


def build_event_times(
    timeline: Dict[str, Any],
    players_team: Dict[int, int],
) -> Dict[str, List[float]]:
    roles = dataset.role_map(timeline)
    junglers = {
        team: pid
        for pid, team in players_team.items()
        if roles.get(pid) == "JUNGLE" and team in (100, 200)
    }

    kills = {"100": [], "200": []}
    ganks = {"100": [], "200": []}
    dragon: List[float] = []
    baron: List[float] = []
    tower: List[float] = []

    for event in timeline.get("events") or []:
        event_type = event.get("type")
        timestamp = seconds(event.get("timestamp"))

        if event_type == "CHAMPION_KILL":
            actor_id = event.get("actorId")
            team = players_team.get(actor_id) if isinstance(actor_id, int) else None
            if team in (100, 200):
                kills[str(team)].append(timestamp)

            involved = {actor_id} | set(event.get("assistIds") or [])
            for gank_team, jungler_id in junglers.items():
                if jungler_id in involved:
                    ganks[str(gank_team)].append(timestamp)

        elif event_type == "ELITE_MONSTER_KILL":
            if event.get("monsterType") == "DRAGON":
                dragon.append(timestamp)
            elif event.get("monsterType") == "BARON_NASHOR":
                baron.append(timestamp)

        elif event_type == "BUILDING_KILL":
            if event.get("buildingType") == "TOWER_BUILDING":
                tower.append(timestamp)

    return {
        "kill": sorted(kills["100"] + kills["200"]),
        "blue_kill": sorted(kills["100"]),
        "red_kill": sorted(kills["200"]),
        "gank": sorted(ganks["100"] + ganks["200"]),
        "blue_gank": sorted(ganks["100"]),
        "red_gank": sorted(ganks["200"]),
        "dragon": sorted(dragon),
        "baron": sorted(baron),
        "tower": sorted(tower),
    }


def build_rows(game: Dict[str, Any], timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    frames = timeline.get("frames") or []
    players = timeline.get("players") or []
    if len(frames) < 2 or not players:
        return []

    frame_interval = timeline.get("frameInterval", 60000)
    frame_times = [int(frame.get("timestamp") or 0) for frame in frames]

    # 快路径仅在帧严格对齐 0/60/120... 秒网格时按分钟直索引；
    # 异常帧表（非 60s 间隔 / 首帧偏移）改按时间戳二分定位，避免静默错位。
    grid_ok = (
        bool(frame_times)
        and frame_interval == 60000
        and abs(frame_times[0]) <= 500
        and all(
            abs(timestamp - index * 60000) <= 500
            for index, timestamp in enumerate(frame_times)
        )
    )

    if grid_ok:
        max_minute = len(frames) - 1

        def frame_for_minute(minute: int) -> Optional[Dict[str, Any]]:
            return frames[minute] if 0 <= minute < len(frames) else None

    else:
        max_minute = int(frame_times[-1] // 60000)

        def frame_for_minute(minute: int) -> Optional[Dict[str, Any]]:
            index = bisect.bisect_right(frame_times, minute * 60000) - 1
            return frames[index] if index >= 0 else None

    winning_team = timeline.get("winningTeam")
    players_team = dataset.player_team_map(timeline)
    team_counters, player_counters = build_counters(timeline, players_team, max_minute)
    event_times = build_event_times(timeline, players_team)

    game_id = game.get("gameId") or timeline.get("gameId")
    rows: List[Dict[str, Any]] = []

    for minute in range(1, max_minute + 1):
        frame = frame_for_minute(minute)
        if frame is None:
            continue
        participants = frame.get("participants") or {}
        if not participants:
            continue

        team_stats: Dict[int, Dict[str, float]] = {
            100: {"cs": 0.0, "level": 0.0},
            200: {"cs": 0.0, "level": 0.0},
        }

        for player in players:
            pid = player.get("participantId")
            team = player.get("teamId")
            state = participants.get(str(pid))
            if team not in (100, 200) or not state:
                continue
            team_stats[team]["cs"] += float(state.get("cs") or 0)
            team_stats[team]["level"] += float(state.get("level") or 0)

        for player in players:
            pid = player.get("participantId")
            team = player.get("teamId")
            if team not in (100, 200):
                continue

            state = participants.get(str(pid))
            if not state:
                continue

            enemy = 200 if team == 100 else 100
            own_stats = team_stats[team]
            enemy_stats = team_stats[enemy]

            row = {
                "gameId": game_id,
                "minute": minute,
                "role": player.get("position") or "",
                "ownWin": 1 if team == winning_team else 0,
                "ownLevel": int(state.get("level") or 0),
                "ownGold": float(state.get("currentGold") or 0),
                "ownCS": float(state.get("cs") or 0),
                "ownKills": cumulative(player_counters, (pid, "kills"), minute),
                "ownDeaths": cumulative(player_counters, (pid, "deaths"), minute),
                "ownAssists": cumulative(player_counters, (pid, "assists"), minute),
                "teamKills": cumulative(team_counters, (team, "kills"), minute),
                "teamDeaths": cumulative(team_counters, (team, "deaths"), minute),
                "teamAssists": cumulative(team_counters, (team, "assists"), minute),
                "teamCS": own_stats["cs"],
                "teamLevelSum": own_stats["level"],
                "enemyKills": cumulative(team_counters, (enemy, "kills"), minute),
                "enemyDeaths": cumulative(team_counters, (enemy, "deaths"), minute),
                "enemyAssists": cumulative(team_counters, (enemy, "assists"), minute),
                "enemyCS": enemy_stats["cs"],
                "enemyLevelSum": enemy_stats["level"],
            }

            row["killDiff"] = row["teamKills"] - row["enemyKills"]
            row["deathDiff"] = row["enemyDeaths"] - row["teamDeaths"]
            row["assistDiff"] = row["teamAssists"] - row["enemyAssists"]
            row["csDiff"] = row["teamCS"] - row["enemyCS"]
            row["levelSumDiff"] = row["teamLevelSum"] - row["enemyLevelSum"]

            for metric, column in (
                ("dragons", "dragonDiff"),
                ("barons", "baronDiff"),
                ("heralds", "heraldDiff"),
                ("grubs", "grubDiff"),
                ("atakhans", "atakhanDiff"),
                ("towers", "towerDiff"),
                ("inhibitors", "inhibitorDiff"),
            ):
                row[column] = cumulative(team_counters, (team, metric), minute) - cumulative(
                    team_counters, (enemy, metric), minute
                )

            timestamp = float(minute * 60)
            own_kill_times = event_times["blue_kill" if team == 100 else "red_kill"]
            enemy_kill_times = event_times["blue_kill" if enemy == 100 else "red_kill"]
            own_gank_times = event_times["blue_gank" if team == 100 else "red_gank"]
            enemy_gank_times = event_times["blue_gank" if enemy == 100 else "red_gank"]

            row["since_kill"] = since(event_times["kill"], timestamp)
            row["since_own_kill"] = since(own_kill_times, timestamp)
            row["since_enemy_kill"] = since(enemy_kill_times, timestamp)
            row["since_gank"] = since(event_times["gank"], timestamp)
            row["since_own_gank"] = since(own_gank_times, timestamp)
            row["since_enemy_gank"] = since(enemy_gank_times, timestamp)
            row["since_dragon"] = since(event_times["dragon"], timestamp)
            row["since_baron"] = since(event_times["baron"], timestamp)
            row["since_tower"] = since(event_times["tower"], timestamp)
            row["kills_last_60"] = count_between(
                event_times["kill"], timestamp - 60, timestamp
            )
            row["kills_last_180"] = count_between(
                event_times["kill"], timestamp - 180, timestamp
            )
            row["own_kills_last_180"] = count_between(
                own_kill_times, timestamp - 180, timestamp
            )
            row["enemy_kills_last_180"] = count_between(
                enemy_kill_times, timestamp - 180, timestamp
            )
            row["ganks_last_300"] = count_between(
                event_times["gank"], timestamp - 300, timestamp
            )
            row["own_ganks_last_300"] = count_between(
                own_gank_times, timestamp - 300, timestamp
            )
            row["enemy_ganks_last_300"] = count_between(
                enemy_gank_times, timestamp - 300, timestamp
            )
            row["own_kill_30s"] = int(
                count_between(own_kill_times, timestamp, timestamp + 30) > 0
            )
            row["enemy_kill_30s"] = int(
                count_between(enemy_kill_times, timestamp, timestamp + 30) > 0
            )
            row["own_gank_60s"] = int(
                count_between(own_gank_times, timestamp, timestamp + 60) > 0
            )
            row["enemy_gank_60s"] = int(
                count_between(enemy_gank_times, timestamp, timestamp + 60) > 0
            )

            rows.append(row)

    return rows


def build(export_paths: List[Path], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    games = 0
    rows_written = 0

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()

        for export in export_paths:
            try:
                summary = dataset.load_json(export / "summary.json")
                timeline = dataset.load_json(export / "timeline" / "timeline.json")
            except Exception:
                continue

            game = dataset.pick_game(summary, timeline)
            if not game:
                continue

            if (
                game.get("queueId") not in (420, 440)
                or game.get("gameMode") != "CLASSIC"
                or game.get("mapId") != 11
            ):
                continue

            if int(game.get("gameDuration") or 0) < 600:
                continue

            rows = build_rows(game, timeline)
            if not rows:
                continue

            for row in rows:
                writer.writerow(row)

            rows_written += len(rows)
            games += 1

            if games % 100 == 0:
                print(f"  games={games} rows={rows_written}")

    manifest = {
        "games": games,
        "rows": rows_written,
        "columns": COLUMNS,
        "note": "Live-feasible: own gold only, no enemy gold, no positions.",
    }
    (output.parent / "live_dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"Games: {games}")
    print(f"Rows: {rows_written}")
    print(f"Output: {output}")
    return 0 if rows_written else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建 Live Client Data 可观测特征数据集",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    exports = dataset.find_exports(args.export_root)
    if not exports:
        print("[错误] 没有找到导出目录", file=sys.stderr)
        return 1

    output = args.output or (args.export_root / "live_dataset" / "live_dataset.csv")
    if output.is_dir():
        output = output / "live_dataset.csv"

    return build(exports, output)


if __name__ == "__main__":
    raise SystemExit(main())
