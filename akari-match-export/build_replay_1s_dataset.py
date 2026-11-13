#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge replay Live Client Data 1-second snapshots with SGP 1-minute state.

Rows: one per player per second.
Live-observable features (no enemy gold): level/KDA/CS/items/wards/death/respawn,
team aggregates, objective diffs, event recency.
Merged from SGP at 1-minute granularity: position x/y, currentGold, totalGold, xp.
Labels (computed from the complete event list): own/enemy kill in 30s, own/enemy
jungler gank in 60s.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FEATURES = [
    "gameId", "t", "minute", "role", "team", "ownWin",
    "ownLevel", "ownKills", "ownDeaths", "ownAssists", "ownCS", "ownWardScore",
    "ownItemsCount", "ownItemGold", "ownGold", "ownTotalGold", "ownXp",
    "ownX", "ownY", "isDead", "respawnTimer",
    "teamKills", "teamDeaths", "teamAssists", "teamCS", "teamLevelSum", "teamItemGold",
    "enemyKills", "enemyDeaths", "enemyAssists", "enemyCS", "enemyLevelSum", "enemyItemGold",
    "killDiff", "deathDiff", "assistDiff", "csDiff", "levelSumDiff", "itemGoldDiff",
    "dragonDiff", "baronDiff", "heraldDiff", "grubDiff", "atakhanDiff",
    "towerDiff", "inhibitorDiff",
    "sinceKill", "sinceOwnKill", "sinceEnemyKill",
    "sinceGank", "sinceOwnGank", "sinceEnemyGank",
    "sinceDragon", "sinceBaron", "sinceTower",
    "killsLast60", "killsLast180", "ownKillsLast180", "enemyKillsLast180",
    "ganksLast300", "ownGanksLast300", "enemyGanksLast300",
    "ownKill30", "enemyKill30", "ownGank60", "enemyGank60",
]


def load_snapshots(snapshot_dir: Path) -> List[Dict[str, Any]]:
    files = sorted(glob.glob(str(snapshot_dir / "snapshots.jsonl")))
    if not files:
        files = sorted(glob.glob(str(snapshot_dir / "*" / "snapshots.jsonl")))
    rows: List[Dict[str, Any]] = []
    for file in files:
        for line in open(file, encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_event_index(snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the most complete cumulative event list plus helper lookups."""
    best = None
    best_time = -1
    for snapshot in snapshots:
        game_time = float(snapshot.get("gameTime") or 0)
        if game_time >= best_time:
            best = snapshot
            best_time = game_time
    events = (best or {}).get("events") or []
    return {"events": events, "gameTime": best_time}


def event_team(event: Dict[str, Any], riot_team: Dict[str, str]) -> Optional[int]:
    name = event.get("KillerName") or event.get("VictimName")
    if not name:
        return None
    team = riot_team.get(name)
    if team == "ORDER":
        return 100
    if team == "CHAOS":
        return 200
    return None


def team_of(name: Optional[str], riot_team: Dict[str, str]) -> Optional[int]:
    if not name:
        return None
    team = riot_team.get(name)
    if team == "ORDER":
        return 100
    if team == "CHAOS":
        return 200
    return None


def build(
    snapshot_dir: Path,
    timeline_path: Path,
    summary_path: Path,
    output: Path,
) -> int:
    snapshots = load_snapshots(snapshot_dir)
    if not snapshots:
        print("[错误] 没有快照", file=sys.stderr)
        return 1

    timeline = json.load(open(timeline_path, encoding="utf-8"))
    summary = json.load(open(summary_path, encoding="utf-8"))

    game = None
    for entry in summary.get("games") or []:
        game_json = entry.get("json") or entry
        game = game_json
        break

    game_id = timeline.get("gameId")
    winning_team = timeline.get("winningTeam")

    # riotId -> team
    riot_team: Dict[str, str] = {}

    # participantId -> role
    pid_role: Dict[int, str] = {}

    for player in timeline.get("players") or []:
        pid_role[int(player["participantId"])] = player.get("position") or ""

    # LCU fallbacks: lane and team per participantId
    pid_lane: Dict[int, str] = {}
    pid_team: Dict[int, int] = {}
    pid_smite: set = set()

    if isinstance(game, dict):
        for participant in game.get("participants") or []:
            pid = participant.get("participantId")
            if pid is None:
                continue
            pid_team[int(pid)] = participant.get("teamId")
            timeline_info = participant.get("timeline") or {}
            lane = timeline_info.get("lane") or timeline_info.get("role") or ""
            if lane and lane != "NONE":
                pid_lane[int(pid)] = lane
            spells = [participant.get("spell1Id"), participant.get("spell2Id")]
            if 11 in spells:
                pid_smite.add(int(pid))

    def role_of(pid: Optional[int]) -> str:
        if pid is None:
            return ""
        return pid_role.get(pid) or pid_lane.get(pid) or ""

    # timeline frames by minute: participantId -> {x,y,gold,xp,cs,level}
    frames = timeline.get("frames") or []
    frame_interval = timeline.get("frameInterval", 60000)
    frame_times = [int(frame.get("timestamp") or 0) for frame in frames]

    # 快路径仅在帧严格对齐 0/60/120... 秒网格时使用直索引；
    # 否则（异常帧间隔 / 首帧偏移 / 空帧表）改按时间戳二分定位，避免错位或崩溃。
    grid_ok = (
        bool(frame_times)
        and frame_interval == 60000
        and abs(frame_times[0]) <= 500
        and all(
            abs(timestamp - index * 60000) <= 500
            for index, timestamp in enumerate(frame_times)
        )
    )

    def frame_index_for_minute(minute: int) -> int:
        if not frame_times:
            return -1
        if grid_ok:
            return min(max(minute, 0), len(frame_times) - 1)
        target_ms = minute * 60000
        return bisect.bisect_right(frame_times, target_ms) - 1

    def sgp_state(participant_id: int, minute: int) -> Dict[str, Optional[float]]:
        index = frame_index_for_minute(minute)
        if index < 0:
            return {}
        state = frames[index].get("participants", {}).get(str(participant_id)) or {}
        return {
            "x": state.get("x"),
            "y": state.get("y"),
            "currentGold": state.get("currentGold"),
            "totalGold": state.get("totalGold"),
            "xp": state.get("xp"),
            "cs": state.get("cs"),
            "level": state.get("level"),
        }

    event_index = build_event_index(snapshots)
    events = event_index["events"]

    kill_times: List[float] = []
    own_kill_times: Dict[int, List[float]] = {100: [], 200: []}
    gank_times: Dict[int, List[float]] = {100: [], 200: []}
    dragon_times: Dict[int, List[float]] = {100: [], 200: []}
    baron_times: Dict[int, List[float]] = {100: [], 200: []}
    herald_times: Dict[int, List[float]] = {100: [], 200: []}
    grub_times: Dict[int, List[float]] = {100: [], 200: []}
    atakhan_times: Dict[int, List[float]] = {100: [], 200: []}
    tower_times: Dict[int, List[float]] = {100: [], 200: []}
    inhibitor_times: Dict[int, List[float]] = {100: [], 200: []}

    # riotId -> participantId
    riot_pid: Dict[str, int] = {}

    if isinstance(game, dict):
        for identity in game.get("participantIdentities") or []:
            player = identity.get("player") or {}
            name = player.get("gameName") or ""
            tag = player.get("tagLine") or ""
            riot_id = f"{name}#{tag}"
            riot_pid[riot_id] = identity.get("participantId")

        # riotId -> team from snapshot players (team available live)
        if snapshots:
            for player in snapshots[0].get("data", {}).get("players") or []:
                riot_id = player.get("riotId") or player.get("summonerName")
                team = player.get("team")
                if riot_id and team in ("ORDER", "CHAOS"):
                    riot_team[riot_id] = team

    # role by participantId from timeline players already loaded

    def junglers() -> Dict[int, int]:
        result: Dict[int, int] = {}
        for pid in pid_smite:
            team = pid_team.get(pid)
            if team in (100, 200):
                result[team] = pid
        return result

    jungle_pids = junglers()

    for event in events:
        name = event.get("EventName")
        game_time = float(event.get("EventTime") or 0)
        killer = event.get("KillerName")
        victim = event.get("VictimName")
        assisters = event.get("Assisters") or []

        if name == "ChampionKill":
            kill_times.append(game_time)
            team = team_of(killer, riot_team)
            if team in own_kill_times:
                own_kill_times[team].append(game_time)

            # gank: jungler of killer's team among killer/assisters
            if team in jungle_pids:
                jungler_pid = jungle_pids[team]
                jungler_riot = next(
                    (rid for rid, pid in riot_pid.items() if pid == jungler_pid), None
                )
                involved = [killer] + assisters
                if jungler_riot in involved:
                    gank_times[team].append(game_time)

        elif name == "DragonKill":
            team = team_of(killer, riot_team)
            if team in dragon_times:
                dragon_times[team].append(game_time)
        elif name == "BaronKill":
            team = team_of(killer, riot_team)
            if team in baron_times:
                baron_times[team].append(game_time)
        elif name == "HeraldKill":
            team = team_of(killer, riot_team)
            if team in herald_times:
                herald_times[team].append(game_time)
        elif name in ("HordeKill", "GrubKill"):
            team = team_of(killer, riot_team)
            if team in grub_times:
                grub_times[team].append(game_time)
        elif name == "AtakhanKill":
            team = team_of(killer, riot_team)
            if team in atakhan_times:
                atakhan_times[team].append(game_time)
        elif name == "TurretKilled":
            team = team_of(killer, riot_team)
            if team in tower_times:
                tower_times[team].append(game_time)
        elif name == "InhibKilled":
            team = team_of(killer, riot_team)
            if team in inhibitor_times:
                inhibitor_times[team].append(game_time)

    for bucket in (own_kill_times, gank_times, dragon_times, baron_times, herald_times, grub_times, atakhan_times, tower_times, inhibitor_times):
        for team in (100, 200):
            bucket[team].sort()

    kill_times.sort()

    def count_between(times: List[float], start: float, end: float) -> int:
        return bisect.bisect_right(times, end) - bisect.bisect_right(times, start)

    def since(times: List[float], timestamp: float) -> float:
        index = bisect.bisect_right(times, timestamp) - 1
        return 9999.0 if index < 0 else timestamp - times[index]

    rows_written = 0
    seconds_written = set()

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURES)
        writer.writeheader()

        for snapshot in snapshots:
            game_time = float(snapshot.get("gameTime") or 0)
            timestamp = int(round(game_time))
            if timestamp in seconds_written:
                continue
            seconds_written.add(timestamp)
            minute = timestamp // 60

            players = snapshot.get("data", {}).get("players") or []
            if len(players) != 10:
                continue

            team_stats: Dict[int, Dict[str, float]] = {
                100: {"kills": 0, "deaths": 0, "assists": 0, "cs": 0, "level": 0, "gold": 0},
                200: {"kills": 0, "deaths": 0, "assists": 0, "cs": 0, "level": 0, "gold": 0},
            }

            player_rows = []

            for player in players:
                riot_id = player.get("riotId") or player.get("summonerName") or ""
                team_str = player.get("team")
                team = 100 if team_str == "ORDER" else 200 if team_str == "CHAOS" else None
                if team is None:
                    continue

                scores = player.get("scores") or {}
                items = player.get("items") or []
                item_gold = sum(float(item.get("price") or 0) * float(item.get("count") or 0) for item in items)

                pid = riot_pid.get(riot_id)
                sgp = sgp_state(pid, minute) if pid is not None else {}

                row = {
                    "gameId": game_id,
                    "t": timestamp,
                    "minute": minute,
                    "role": role_of(pid),
                    "team": team,
                    "ownWin": 1 if team == winning_team else 0,
                    "ownLevel": float(player.get("level") or 0),
                    "ownKills": float(scores.get("kills") or 0),
                    "ownDeaths": float(scores.get("deaths") or 0),
                    "ownAssists": float(scores.get("assists") or 0),
                    "ownCS": float(scores.get("creepScore") or 0),
                    "ownWardScore": float(scores.get("wardScore") or 0),
                    "ownItemsCount": float(len(items)),
                    "ownItemGold": item_gold,
                    "ownGold": sgp.get("currentGold") if sgp.get("currentGold") is not None else "",
                    "ownTotalGold": sgp.get("totalGold") if sgp.get("totalGold") is not None else "",
                    "ownXp": sgp.get("xp") if sgp.get("xp") is not None else "",
                    "ownX": sgp.get("x") if sgp.get("x") is not None else "",
                    "ownY": sgp.get("y") if sgp.get("y") is not None else "",
                    "isDead": 1 if player.get("isDead") else 0,
                    "respawnTimer": float(player.get("respawnTimer") or 0),
                }
                team_stats[team]["kills"] += row["ownKills"]
                team_stats[team]["deaths"] += row["ownDeaths"]
                team_stats[team]["assists"] += row["ownAssists"]
                team_stats[team]["cs"] += row["ownCS"]
                team_stats[team]["level"] += row["ownLevel"]
                team_stats[team]["gold"] += item_gold
                player_rows.append(row)

            for row in player_rows:
                team = row["team"]
                enemy = 200 if team == 100 else 100
                own_ts = team_stats[team]
                enemy_ts = team_stats[enemy]

                row["teamKills"] = own_ts["kills"]
                row["teamDeaths"] = own_ts["deaths"]
                row["teamAssists"] = own_ts["assists"]
                row["teamCS"] = own_ts["cs"]
                row["teamLevelSum"] = own_ts["level"]
                row["teamItemGold"] = own_ts["gold"]
                row["enemyKills"] = enemy_ts["kills"]
                row["enemyDeaths"] = enemy_ts["deaths"]
                row["enemyAssists"] = enemy_ts["assists"]
                row["enemyCS"] = enemy_ts["cs"]
                row["enemyLevelSum"] = enemy_ts["level"]
                row["enemyItemGold"] = enemy_ts["gold"]

                row["killDiff"] = own_ts["kills"] - enemy_ts["kills"]
                row["deathDiff"] = enemy_ts["deaths"] - own_ts["deaths"]
                row["assistDiff"] = own_ts["assists"] - enemy_ts["assists"]
                row["csDiff"] = own_ts["cs"] - enemy_ts["cs"]
                row["levelSumDiff"] = own_ts["level"] - enemy_ts["level"]
                row["itemGoldDiff"] = own_ts["gold"] - enemy_ts["gold"]

                def diff(bucket: Dict[int, List[float]], timestamp_sec: float) -> float:
                    return float(count_between(bucket[team], 0, timestamp_sec + 1e-6) - count_between(bucket[enemy], 0, timestamp_sec + 1e-6))

                row["dragonDiff"] = diff(dragon_times, timestamp)
                row["baronDiff"] = diff(baron_times, timestamp)
                row["heraldDiff"] = diff(herald_times, timestamp)
                row["grubDiff"] = diff(grub_times, timestamp)
                row["atakhanDiff"] = diff(atakhan_times, timestamp)
                row["towerDiff"] = diff(tower_times, timestamp)
                row["inhibitorDiff"] = diff(inhibitor_times, timestamp)

                row["sinceKill"] = since(kill_times, timestamp)
                row["sinceOwnKill"] = since(own_kill_times[team], timestamp)
                row["sinceEnemyKill"] = since(own_kill_times[enemy], timestamp)
                row["sinceGank"] = since(sorted(gank_times[100] + gank_times[200]), timestamp)
                row["sinceOwnGank"] = since(gank_times[team], timestamp)
                row["sinceEnemyGank"] = since(gank_times[enemy], timestamp)
                row["sinceDragon"] = since(sorted(dragon_times[100] + dragon_times[200]), timestamp)
                row["sinceBaron"] = since(sorted(baron_times[100] + baron_times[200]), timestamp)
                row["sinceTower"] = since(sorted(tower_times[100] + tower_times[200]), timestamp)

                row["killsLast60"] = count_between(kill_times, timestamp - 60, timestamp)
                row["killsLast180"] = count_between(kill_times, timestamp - 180, timestamp)
                row["ownKillsLast180"] = count_between(own_kill_times[team], timestamp - 180, timestamp)
                row["enemyKillsLast180"] = count_between(own_kill_times[enemy], timestamp - 180, timestamp)
                row["ganksLast300"] = count_between(sorted(gank_times[100] + gank_times[200]), timestamp - 300, timestamp)
                row["ownGanksLast300"] = count_between(gank_times[team], timestamp - 300, timestamp)
                row["enemyGanksLast300"] = count_between(gank_times[enemy], timestamp - 300, timestamp)

                row["ownKill30"] = int(count_between(own_kill_times[team], timestamp, timestamp + 30) > 0)
                row["enemyKill30"] = int(count_between(own_kill_times[enemy], timestamp, timestamp + 30) > 0)
                row["ownGank60"] = int(count_between(gank_times[team], timestamp, timestamp + 60) > 0)
                row["enemyGank60"] = int(count_between(gank_times[enemy], timestamp, timestamp + 60) > 0)

                writer.writerow(row)
                rows_written += 1

    print(f"Snapshots: {len(snapshots)} -> rows: {rows_written}")
    print(f"Output: {output}")
    return 0 if rows_written else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="回放 1 秒快照 + SGP 1 分钟状态合并",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--snapshots", type=Path, required=True, help="replay_collector 会话目录")
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    return build(args.snapshots, args.timeline, args.summary, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
