#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a 1-minute granularity dataset from exported match timelines.

Input: one or more League Akari export folders created by `match_export.py`,
each containing:

    summary.json
    timeline/timeline.json

Output:

    dataset/dataset_blue.csv    one row per game-minute, blue-side perspective
    dataset/dataset_sides.csv   two rows per game-minute (blue + red perspective)
    dataset/data_dictionary.md
    dataset/split.json          gameId-based train/val/test split

Filters default to ranked Summoner's Rift (queue 420/440, CLASSIC, map 11).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_QUEUES = (420, 440)
MIN_DURATION_SECONDS = 600
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

# Same camp anchors and lane classifiers as League Akari's jungle analysis
# (src/shared/data-adapter/analysis/player/utils/geometry.ts).
JUNGLE_CAMPS = [
    (3830, 7880, "blue", "blue"),
    (3800, 6440, "wolves", "blue"),
    (7760, 4010, "red", "blue"),
    (6970, 5460, "raptors", "blue"),
    (10990, 7000, "blue", "red"),
    (11020, 8440, "wolves", "red"),
    (7060, 10870, "red", "red"),
    (7850, 9420, "raptors", "red"),
]
CAMP_NAMES = ("blue", "red", "wolves", "raptors")
START_CAMP_COLUMNS = [
    f"blue_jungle_start_{camp}" for camp in CAMP_NAMES
] + [f"red_jungle_start_{camp}" for camp in CAMP_NAMES] + [
    "blue_jungle_start_invade",
    "red_jungle_start_invade",
]

# All difference columns are "row side - enemy side". A red-perspective row
# simply negates these values.
DIFF_COLUMNS = [
    "gold_diff",
    "current_gold_diff",
    "xp_diff",
    "level_diff",
    "cs_diff",
    "kills_diff",
    "deaths_diff",
    "assists_diff",
    "tower_diff",
    "inhibitor_diff",
    "dragon_diff",
    "elder_dragon_diff",
    "baron_diff",
    "herald_diff",
    "grub_diff",
    "atakhan_diff",
    "plate_diff",
    "wards_placed_diff",
    "wards_killed_diff",
    "gold_diff_slope_5m",
    "xp_diff_slope_5m",
    "kills_last_5m",
    "towers_last_5m",
    "objectives_last_5m",
    "kills_last_min",
    "objectives_last_min",
    "kill_lane_top_diff",
    "kill_lane_mid_diff",
    "kill_lane_bot_diff",
    "kill_lane_jungle_diff",
    "first_blood",
    "jungler_zone_top_diff",
    "jungler_zone_mid_diff",
    "jungler_zone_bot_diff",
    "jungler_near_top_diff",
    "jungler_near_mid_diff",
    "jungler_near_bot_diff",
    "jungler_gank_top_diff",
    "jungler_gank_mid_diff",
    "jungler_gank_bot_diff",
]

for _role in ROLES:
    DIFF_COLUMNS.extend(
        [
            f"gold_diff_{_role}",
            f"xp_diff_{_role}",
            f"level_diff_{_role}",
            f"cs_diff_{_role}",
            f"kills_diff_{_role}",
            f"deaths_diff_{_role}",
            f"assists_diff_{_role}",
        ]
    )

CHAMPION_COLUMNS = [f"blue_champ_{role}" for role in ROLES] + [
    f"red_champ_{role}" for role in ROLES
]

BASE_COLUMNS = [
    "gameId",
    "minute",
    "patch",
    "queue_id",
    "game_mode",
    "side",
    "win",
    "blue_win",
    "gold_diff_plus5",
]

FEATURE_COLUMNS = DIFF_COLUMNS + CHAMPION_COLUMNS + START_CAMP_COLUMNS


def normalize_position(value: str) -> str:
    value = (value or "").upper()

    if value in ("MID", "MIDDLE"):
        return "MIDDLE"
    if value in ("BOT", "BOTTOM", "ADC"):
        return "BOTTOM"
    if value in ("SUPPORT", "UTILITY"):
        return "UTILITY"
    if value in ("JUNGLE", "JGL"):
        return "JUNGLE"
    if value in ("TOP", "TOPLANE"):
        return "TOP"

    return value


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def find_exports(root: Path) -> List[Path]:
    exports: List[Path] = []

    if (root / "summary.json").is_file() and (root / "timeline" / "timeline.json").is_file():
        exports.append(root)

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "summary.json").is_file() and (child / "timeline" / "timeline.json").is_file():
            exports.append(child)

    return exports


def pick_game(summary: Dict[str, Any], timeline: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    game_id = timeline.get("gameId")

    for game in summary.get("games") or []:
        game_json = game.get("json") or {}
        if game_json.get("gameId") == game_id:
            return game_json

    return None


def team_win(summary_game: Dict[str, Any], team_id: int) -> bool:
    for team in summary_game.get("teams") or []:
        if team.get("teamId") == team_id:
            return bool(team.get("win"))

    for participant in summary_game.get("participants") or []:
        if participant.get("teamId") == team_id:
            return bool(participant.get("win"))

    return False


def patch_of(game_version: Any) -> str:
    parts = str(game_version or "").split(".")

    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"

    return str(game_version or "")


def index_frames(timeline: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """minute -> participantFrames dict, last frame in that minute wins."""
    frames: Dict[int, Dict[str, Any]] = {}

    for frame in timeline.get("frames") or []:
        timestamp = int(frame.get("timestamp") or 0)
        minute = timestamp // 60000
        frames[minute] = frame.get("participants") or {}

    return frames


def build_state_series(
    frames: Dict[int, Dict[str, Any]], max_minute: int
) -> List[Dict[str, Dict[str, Any]]]:
    """Forward-fill participant state for every minute 0..max_minute."""
    series: List[Dict[str, Dict[str, Any]]] = []
    current: Dict[str, Dict[str, Any]] = {}

    for minute in range(max_minute + 1):
        if minute in frames:
            current = frames[minute]
        series.append(current)

    return series


def role_map(timeline: Dict[str, Any]) -> Dict[int, str]:
    mapping: Dict[int, str] = {}

    for player in timeline.get("players") or []:
        participant_id = player.get("participantId")
        if isinstance(participant_id, int):
            mapping[participant_id] = normalize_position(player.get("position") or "")

    return mapping


def champion_map(summary_game: Dict[str, Any]) -> Dict[int, int]:
    mapping: Dict[int, int] = {}

    for participant in summary_game.get("participants") or []:
        participant_id = participant.get("participantId")
        champion_id = participant.get("championId")
        if isinstance(participant_id, int) and isinstance(champion_id, int):
            mapping[participant_id] = champion_id

    return mapping


def player_team_map(timeline: Dict[str, Any]) -> Dict[int, int]:
    mapping: Dict[int, int] = {}

    for player in timeline.get("players") or []:
        participant_id = player.get("participantId")
        team_id = player.get("teamId")
        if isinstance(participant_id, int) and isinstance(team_id, int):
            mapping[participant_id] = team_id

    return mapping


def accumulate_events(
    timeline: Dict[str, Any],
    players_team: Dict[int, int],
    max_minute: int,
) -> Dict[Tuple[int, str], List[int]]:
    """Per-team, per-counter event deltas indexed by minute bucket."""
    counters: Dict[Tuple[int, str], List[int]] = {}

    def bump(team: Optional[int], counter: str, bucket: int) -> None:
        if team not in (100, 200):
            return
        if bucket < 0 or bucket > max_minute:
            return
        key = (team, counter)
        if key not in counters:
            counters[key] = [0] * (max_minute + 1)
        counters[key][bucket] += 1

    for event in timeline.get("events") or []:
        event_type = event.get("type")
        timestamp = int(event.get("timestamp") or 0)
        # Minute 1 covers (0, 60s], minute 2 covers (60s, 120s], ...
        bucket = (timestamp + 59999) // 60000

        if bucket > max_minute:
            continue
        actor_id = event.get("actorId")
        victim_id = event.get("victimId")
        actor_team = players_team.get(actor_id) if isinstance(actor_id, int) else None
        victim_team = players_team.get(victim_id) if isinstance(victim_id, int) else None

        if event_type == "CHAMPION_KILL":
            bump(actor_team, "kills", bucket)
            bump(victim_team, "deaths", bucket)

            for assist_id in event.get("assistIds") or []:
                if isinstance(assist_id, int):
                    bump(players_team.get(assist_id), "assists", bucket)

        elif event_type == "BUILDING_KILL":
            building_type = event.get("buildingType")
            counter = "inhibitors" if building_type == "INHIBITOR_BUILDING" else "towers"
            bump(actor_team, counter, bucket)

        elif event_type == "ELITE_MONSTER_KILL":
            monster = event.get("monsterType")
            if monster == "DRAGON":
                bump(actor_team, "dragons", bucket)
                if "ELDER" in str(event.get("monsterSubType") or "").upper():
                    bump(actor_team, "elder_dragons", bucket)
            elif monster == "BARON_NASHOR":
                bump(actor_team, "barons", bucket)
            elif monster == "RIFTHERALD":
                bump(actor_team, "heralds", bucket)
            elif monster == "HORDE":
                bump(actor_team, "grubs", bucket)
            elif monster == "ATAKHAN":
                bump(actor_team, "atakhans", bucket)

        elif event_type == "TURRET_PLATE_DESTROYED":
            bump(actor_team, "plates", bucket)

        elif event_type == "WARD_PLACED":
            bump(actor_team, "wards_placed", bucket)

        elif event_type == "WARD_KILL":
            bump(actor_team, "wards_killed", bucket)

    return counters


def cumulative(counters: Dict[Tuple[int, str], List[int]], team: int, counter: str, minute: int) -> int:
    values = counters.get((team, counter))

    if not values:
        return 0

    return sum(values[: minute + 1])


def window_sum(
    counters: Dict[Tuple[int, str], List[int]],
    team: int,
    counter: str,
    start_minute: int,
    end_minute: int,
) -> int:
    values = counters.get((team, counter))

    if not values:
        return 0

    start = max(0, start_minute)
    end = min(len(values) - 1, end_minute)

    if end < start:
        return 0

    return sum(values[start : end + 1])


def state_sums(
    state: Dict[str, Dict[str, Any]],
    players_team: Dict[int, int],
    roles: Dict[int, str],
    team_id: int,
    field: str,
) -> Dict[str, float]:
    result = {role: 0.0 for role in ROLES}
    total = 0.0

    for key, value in state.items():
        try:
            participant_id = int(key)
        except (TypeError, ValueError):
            continue

        if players_team.get(participant_id) != team_id:
            continue

        amount = value.get(field)
        if not isinstance(amount, (int, float)):
            continue

        total += float(amount)
        role = roles.get(participant_id)
        if role in result:
            result[role] += float(amount)

    result["TOTAL"] = total
    return result


def slope(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0

    n = len(values)
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
    denominator = sum((index - x_mean) ** 2 for index in range(n))

    if denominator == 0:
        return 0.0

    return numerator / denominator


def classify_map_zone(x: float, y: float) -> str:
    if x < 5000 and y > 9000:
        return "top"
    if x > 9000 and y < 5000:
        return "bot"

    dist_from_diag = abs(y - x)
    if dist_from_diag <= 3500:
        return "mid"

    return "top" if y > x else "bot"


def classify_gank_lane(x: float, y: float) -> Optional[str]:
    if x < 5000 and y > 9000:
        return "top"
    if x > 9000 and y < 5000:
        return "bot"

    dist_from_diag = abs(y - x)
    midpoint = (x + y) / 2
    if dist_from_diag < 4000 and 3000 < midpoint < 12000:
        return "mid"

    return None


def detect_start_camp(x: float, y: float) -> Tuple[str, str]:
    nearest = JUNGLE_CAMPS[0]
    best = float("inf")

    for camp_x, camp_y, camp, side in JUNGLE_CAMPS:
        distance = (x - camp_x) ** 2 + (y - camp_y) ** 2
        if distance < best:
            best = distance
            nearest = (camp_x, camp_y, camp, side)

    return nearest[2], nearest[3]


def event_bucket(timestamp: int) -> int:
    return (timestamp + 59999) // 60000


def build_role_counters(
    timeline: Dict[str, Any],
    players_team: Dict[int, int],
    roles: Dict[int, str],
    max_minute: int,
) -> Dict[Tuple[int, str, str], List[int]]:
    counters: Dict[Tuple[int, str, str], List[int]] = {}

    def bump(team: Optional[int], role: Optional[str], counter: str, bucket: int) -> None:
        if team not in (100, 200) or role not in ROLES:
            return
        if bucket < 0 or bucket > max_minute:
            return
        key = (team, role, counter)
        if key not in counters:
            counters[key] = [0] * (max_minute + 1)
        counters[key][bucket] += 1

    for event in timeline.get("events") or []:
        if event.get("type") != "CHAMPION_KILL":
            continue

        bucket = event_bucket(int(event.get("timestamp") or 0))
        killer_id = event.get("actorId")
        victim_id = event.get("victimId")

        if isinstance(killer_id, int):
            bump(players_team.get(killer_id), roles.get(killer_id), "kills", bucket)
        if isinstance(victim_id, int):
            bump(players_team.get(victim_id), roles.get(victim_id), "deaths", bucket)

        for assist_id in event.get("assistIds") or []:
            if isinstance(assist_id, int):
                bump(
                    players_team.get(assist_id),
                    roles.get(assist_id),
                    "assists",
                    bucket,
                )

    return counters


def build_kill_lane_counters(
    timeline: Dict[str, Any],
    players_team: Dict[int, int],
    max_minute: int,
) -> Dict[Tuple[int, str], List[int]]:
    counters: Dict[Tuple[int, str], List[int]] = {}

    for event in timeline.get("events") or []:
        if event.get("type") != "CHAMPION_KILL":
            continue

        actor_id = event.get("actorId")
        team = players_team.get(actor_id) if isinstance(actor_id, int) else None
        if team not in (100, 200):
            continue

        bucket = event_bucket(int(event.get("timestamp") or 0))
        if bucket < 0 or bucket > max_minute:
            continue

        x = event.get("x")
        y = event.get("y")
        lane = "jungle"
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            lane = classify_gank_lane(float(x), float(y)) or "jungle"

        key = (team, lane)
        if key not in counters:
            counters[key] = [0] * (max_minute + 1)
        counters[key][bucket] += 1

    return counters


def build_jungler_gank_counters(
    timeline: Dict[str, Any],
    players_team: Dict[int, int],
    roles: Dict[int, str],
    max_minute: int,
) -> Dict[Tuple[int, str], List[int]]:
    junglers = {
        team: pid
        for pid, team in players_team.items()
        if roles.get(pid) == "JUNGLE" and team in (100, 200)
    }
    counters: Dict[Tuple[int, str], List[int]] = {}

    for event in timeline.get("events") or []:
        if event.get("type") != "CHAMPION_KILL":
            continue

        bucket = event_bucket(int(event.get("timestamp") or 0))
        if bucket < 0 or bucket > max_minute:
            continue

        actor_id = event.get("actorId")
        assist_ids = event.get("assistIds") or []
        x = event.get("x")
        y = event.get("y")
        lane = None
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            lane = classify_gank_lane(float(x), float(y)) or classify_map_zone(
                float(x), float(y)
            )

        if lane not in ("top", "mid", "bot"):
            continue

        for team, jungler_id in junglers.items():
            if actor_id == jungler_id or jungler_id in assist_ids:
                key = (team, lane)
                if key not in counters:
                    counters[key] = [0] * (max_minute + 1)
                counters[key][bucket] += 1

    return counters


def cumulative_counter(
    counters: Dict[Tuple[Any, ...], List[int]],
    key: Tuple[Any, ...],
    minute: int,
) -> int:
    values = counters.get(key)
    if not values:
        return 0
    return sum(values[: minute + 1])


def build_game_rows(
    summary_game: Dict[str, Any],
    timeline: Dict[str, Any],
) -> List[Dict[str, Any]]:
    players_team = player_team_map(timeline)
    roles = role_map(timeline)
    champions = champion_map(summary_game)
    frames = index_frames(timeline)
    max_minute = max(frames) if frames else 0

    if max_minute < 1:
        return []

    state_series = build_state_series(frames, max_minute)
    counters = accumulate_events(timeline, players_team, max_minute)
    role_counters = build_role_counters(timeline, players_team, roles, max_minute)
    kill_lane_counters = build_kill_lane_counters(
        timeline, players_team, max_minute
    )
    jungler_gank_counters = build_jungler_gank_counters(
        timeline, players_team, roles, max_minute
    )

    junglers = {
        team: pid
        for pid, team in players_team.items()
        if roles.get(pid) == "JUNGLE" and team in (100, 200)
    }

    start_camps: Dict[int, Tuple[Optional[str], Optional[str]]] = {}

    if max_minute >= 1:
        for team, pid in junglers.items():
            state = state_series[1].get(str(pid)) or {}
            x = state.get("x")
            y = state.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                start_camps[team] = detect_start_camp(float(x), float(y))
            else:
                start_camps[team] = (None, None)

    first_blood = 0
    for event in sorted(
        timeline.get("events") or [], key=lambda item: item.get("timestamp", 0)
    ):
        if event.get("type") != "CHAMPION_KILL":
            continue
        actor_id = event.get("actorId")
        team = players_team.get(actor_id) if isinstance(actor_id, int) else None
        if team == 100:
            first_blood = 1
        elif team == 200:
            first_blood = -1
        break

    blue_win = team_win(summary_game, 100)
    patch = patch_of(summary_game.get("gameVersion"))
    queue_id = summary_game.get("queueId")
    game_mode = summary_game.get("gameMode")

    gold_diff_by_minute: List[float] = []

    for minute in range(max_minute + 1):
        state = state_series[minute]
        blue_gold = state_sums(state, players_team, roles, 100, "totalGold")
        red_gold = state_sums(state, players_team, roles, 200, "totalGold")
        gold_diff_by_minute.append(blue_gold["TOTAL"] - red_gold["TOTAL"])

    rows: List[Dict[str, Any]] = []

    for minute in range(1, max_minute + 1):
        state = state_series[minute]

        blue_gold = state_sums(state, players_team, roles, 100, "totalGold")
        red_gold = state_sums(state, players_team, roles, 200, "totalGold")
        blue_current = state_sums(state, players_team, roles, 100, "currentGold")
        red_current = state_sums(state, players_team, roles, 200, "currentGold")
        blue_xp = state_sums(state, players_team, roles, 100, "xp")
        red_xp = state_sums(state, players_team, roles, 200, "xp")
        blue_level = state_sums(state, players_team, roles, 100, "level")
        red_level = state_sums(state, players_team, roles, 200, "level")
        blue_cs = state_sums(state, players_team, roles, 100, "cs")
        red_cs = state_sums(state, players_team, roles, 200, "cs")

        row: Dict[str, Any] = {
            "gameId": timeline.get("gameId"),
            "minute": minute,
            "patch": patch,
            "queue_id": queue_id,
            "game_mode": game_mode,
            "side": "blue",
            "win": 1 if blue_win else 0,
            "blue_win": 1 if blue_win else 0,
            "gold_diff": round(blue_gold["TOTAL"] - red_gold["TOTAL"], 2),
            "current_gold_diff": round(blue_current["TOTAL"] - red_current["TOTAL"], 2),
            "xp_diff": round(blue_xp["TOTAL"] - red_xp["TOTAL"], 2),
            "level_diff": round(blue_level["TOTAL"] - red_level["TOTAL"], 2),
            "cs_diff": round(blue_cs["TOTAL"] - red_cs["TOTAL"], 2),
        }

        for counter, field in (
            ("kills", "kills_diff"),
            ("deaths", "deaths_diff"),
            ("assists", "assists_diff"),
            ("towers", "tower_diff"),
            ("inhibitors", "inhibitor_diff"),
            ("dragons", "dragon_diff"),
            ("elder_dragons", "elder_dragon_diff"),
            ("barons", "baron_diff"),
            ("heralds", "herald_diff"),
            ("grubs", "grub_diff"),
            ("atakhans", "atakhan_diff"),
            ("plates", "plate_diff"),
            ("wards_placed", "wards_placed_diff"),
            ("wards_killed", "wards_killed_diff"),
        ):
            row[field] = cumulative(counters, 100, counter, minute) - cumulative(
                counters, 200, counter, minute
            )

        start_5m = max(0, minute - 5)
        row["gold_diff_slope_5m"] = round(
            slope(gold_diff_by_minute[start_5m : minute + 1]), 2
        )

        xp_window: List[float] = []
        for window_minute in range(start_5m, minute + 1):
            window_state = state_series[window_minute]
            window_blue = state_sums(window_state, players_team, roles, 100, "xp")
            window_red = state_sums(window_state, players_team, roles, 200, "xp")
            xp_window.append(window_blue["TOTAL"] - window_red["TOTAL"])
        row["xp_diff_slope_5m"] = round(slope(xp_window), 2)

        row["kills_last_5m"] = window_sum(counters, 100, "kills", minute - 4, minute) - window_sum(
            counters, 200, "kills", minute - 4, minute
        )
        row["towers_last_5m"] = window_sum(
            counters, 100, "towers", minute - 4, minute
        ) - window_sum(counters, 200, "towers", minute - 4, minute)
        row["objectives_last_5m"] = sum(
            window_sum(counters, 100, counter, minute - 4, minute)
            - window_sum(counters, 200, counter, minute - 4, minute)
            for counter in ("dragons", "barons", "heralds", "grubs", "atakhans")
        )
        row["kills_last_min"] = window_sum(
            counters, 100, "kills", minute, minute
        ) - window_sum(counters, 200, "kills", minute, minute)
        row["objectives_last_min"] = sum(
            window_sum(counters, 100, counter, minute, minute)
            - window_sum(counters, 200, counter, minute, minute)
            for counter in ("dragons", "barons", "heralds", "grubs", "atakhans")
        )

        for role in ROLES:
            for counter in ("kills", "deaths", "assists"):
                row[f"{counter}_diff_{role}"] = cumulative_counter(
                    role_counters, (100, role, counter), minute
                ) - cumulative_counter(role_counters, (200, role, counter), minute)

        for lane in ("top", "mid", "bot", "jungle"):
            row[f"kill_lane_{lane}_diff"] = cumulative_counter(
                kill_lane_counters, (100, lane), minute
            ) - cumulative_counter(kill_lane_counters, (200, lane), minute)

        row["first_blood"] = first_blood

        jungler_zone: Dict[int, Optional[str]] = {100: None, 200: None}
        jungler_near: Dict[int, Dict[str, int]] = {
            100: {"top": 0, "mid": 0, "bot": 0},
            200: {"top": 0, "mid": 0, "bot": 0},
        }

        for team in (100, 200):
            pid = junglers.get(team)
            player_state = state.get(str(pid)) if pid else None
            if not player_state:
                continue

            x = player_state.get("x")
            y = player_state.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue

            zone = classify_map_zone(float(x), float(y))
            jungler_zone[team] = zone
            lane = classify_gank_lane(float(x), float(y))
            if lane in jungler_near[team]:
                jungler_near[team][lane] = 1

        for lane in ("top", "mid", "bot"):
            row[f"jungler_zone_{lane}_diff"] = (
                1 if jungler_zone[100] == lane else 0
            ) - (1 if jungler_zone[200] == lane else 0)
            row[f"jungler_near_{lane}_diff"] = (
                jungler_near[100][lane] - jungler_near[200][lane]
            )
            row[f"jungler_gank_{lane}_diff"] = cumulative_counter(
                jungler_gank_counters, (100, lane), minute
            ) - cumulative_counter(jungler_gank_counters, (200, lane), minute)

        for team, prefix, home_side in ((100, "blue", "blue"), (200, "red", "red")):
            camp, side = start_camps.get(team, (None, None))

            for camp_name in CAMP_NAMES:
                row[f"{prefix}_jungle_start_{camp_name}"] = (
                    1 if camp == camp_name else 0
                )

            row[f"{prefix}_jungle_start_invade"] = (
                1 if side and side != home_side else 0
            )

        for role in ROLES:
            row[f"gold_diff_{role}"] = round(
                blue_gold.get(role, 0) - red_gold.get(role, 0), 2
            )
            row[f"xp_diff_{role}"] = round(
                blue_xp.get(role, 0) - red_xp.get(role, 0), 2
            )
            row[f"level_diff_{role}"] = round(
                blue_level.get(role, 0) - red_level.get(role, 0), 2
            )
            row[f"cs_diff_{role}"] = round(
                blue_cs.get(role, 0) - red_cs.get(role, 0), 2
            )

        for role in ROLES:
            blue_pid = next(
                (
                    pid
                    for pid, team in players_team.items()
                    if team == 100 and roles.get(pid) == role
                ),
                None,
            )
            red_pid = next(
                (
                    pid
                    for pid, team in players_team.items()
                    if team == 200 and roles.get(pid) == role
                ),
                None,
            )
            row[f"blue_champ_{role}"] = champions.get(blue_pid) if blue_pid else ""
            row[f"red_champ_{role}"] = champions.get(red_pid) if red_pid else ""

        target_minute = minute + 5
        row["gold_diff_plus5"] = (
            round(gold_diff_by_minute[target_minute], 2)
            if target_minute <= max_minute
            else ""
        )

        rows.append(row)

    return rows


def flip_row(row: Dict[str, Any]) -> Dict[str, Any]:
    flipped = dict(row)
    flipped["side"] = "red"
    flipped["win"] = 1 - int(row["blue_win"])
    flipped["blue_win"] = int(row["blue_win"])

    for column in DIFF_COLUMNS:
        value = row.get(column)
        if isinstance(value, (int, float)):
            flipped[column] = -value

    value = row.get("gold_diff_plus5")
    if isinstance(value, (int, float)):
        flipped["gold_diff_plus5"] = -value

    for role in ROLES:
        flipped[f"blue_champ_{role}"] = row.get(f"red_champ_{role}", "")
        flipped[f"red_champ_{role}"] = row.get(f"blue_champ_{role}", "")

    for camp_name in CAMP_NAMES:
        flipped[f"blue_jungle_start_{camp_name}"] = row.get(
            f"red_jungle_start_{camp_name}", 0
        )
        flipped[f"red_jungle_start_{camp_name}"] = row.get(
            f"blue_jungle_start_{camp_name}", 0
        )

    flipped["blue_jungle_start_invade"] = row.get("red_jungle_start_invade", 0)
    flipped["red_jungle_start_invade"] = row.get("blue_jungle_start_invade", 0)

    return flipped


def write_csv(path: Path, rows: Iterable[Dict[str, Any]], columns: List[str]) -> int:
    count = 0

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()

        for row in rows:
            writer.writerow(row)
            count += 1

    return count


def build_split(game_ids: List[int], seed: int = 42) -> Dict[str, List[int]]:
    ids = sorted(set(game_ids))
    random.Random(seed).shuffle(ids)

    total = len(ids)

    if total < 3:
        return {"train": ids, "val": [], "test": []}
    train_end = int(total * 0.7)
    val_end = int(total * 0.85)

    train = ids[:train_end]
    val = ids[train_end:val_end]
    test = ids[val_end:]

    if total >= 3 and not val:
        val = [train.pop()]
    if total >= 3 and not test:
        test = [train.pop()]

    return {"train": train, "val": val, "test": test}


def data_dictionary() -> str:
    lines = [
        "# Dataset data dictionary",
        "",
        "一行 = 一局 × 一分钟。`side=blue` 时特征为蓝方减红方；`side=red` 时所有 `*_diff` 取反。",
        "",
        "## 标识与标签",
        "",
        "| 字段 | 说明 |",
        "|---|---|",
        "| gameId | 对局 ID |",
        "| minute | 第几分钟（1..N） |",
        "| patch | 版本，如 16.22 |",
        "| queue_id | 420 单双排 / 440 灵活排位 |",
        "| side | blue / red |",
        "| win | 该行视角是否获胜（0/1） |",
        "| blue_win | 蓝方是否获胜（0/1），便于核对 |",
        "| gold_diff_plus5 | 5 分钟后的经济差（该行视角），最后一帧不足 5 分钟时为空 |",
        "",
        "## 队伍级特征（该行视角 - 对方）",
        "",
        "| 字段 | 说明 |",
        "|---|---|",
    ]

    descriptions = {
        "gold_diff": "总金币差（totalGold 之和）",
        "current_gold_diff": "当前可用金币差",
        "xp_diff": "总经验差",
        "level_diff": "总等级差",
        "cs_diff": "总补刀差（含野怪）",
        "kills_diff": "累计击杀差",
        "deaths_diff": "累计死亡差",
        "assists_diff": "累计助攻差",
        "tower_diff": "累计推塔差",
        "inhibitor_diff": "累计水晶差",
        "dragon_diff": "累计小龙差",
        "elder_dragon_diff": "累计远古龙差",
        "baron_diff": "累计大龙差",
        "herald_diff": "累计先锋差",
        "grub_diff": "累计巢虫差",
        "atakhan_diff": "累计厄塔汗差",
        "plate_diff": "累计镀层差",
        "wards_placed_diff": "累计插眼差",
        "wards_killed_diff": "累计排眼差",
        "gold_diff_slope_5m": "最近 5 分钟经济差斜率",
        "xp_diff_slope_5m": "最近 5 分钟经验差斜率",
        "kills_last_5m": "最近 5 分钟击杀差",
        "towers_last_5m": "最近 5 分钟推塔差",
        "objectives_last_5m": "最近 5 分钟史诗野怪差",
        "kills_last_min": "本分钟击杀差",
        "objectives_last_min": "本分钟史诗野怪差",
        "kill_lane_top_diff": "击杀发生在上路的次数差",
        "kill_lane_mid_diff": "击杀发生中路的次数差",
        "kill_lane_bot_diff": "击杀发生下路的次数差",
        "kill_lane_jungle_diff": "击杀发生在野区的次数差",
        "first_blood": "一血方（+1 蓝 / -1 红 / 0 未知）",
        "jungler_zone_top_diff": "双方打野当前处于上区的差",
        "jungler_zone_mid_diff": "双方打野当前处于中区的差",
        "jungler_zone_bot_diff": "双方打野当前处于下区的差",
        "jungler_near_top_diff": "双方打野靠近上路的差",
        "jungler_near_mid_diff": "双方打野靠近中路的差",
        "jungler_near_bot_diff": "双方打野靠近下路的差",
        "jungler_gank_top_diff": "双方打野参与上路击杀的累计差",
        "jungler_gank_mid_diff": "双方打野参与中路击杀的累计差",
        "jungler_gank_bot_diff": "双方打野参与下路击杀的累计差",
    }

    for column in DIFF_COLUMNS:
        lines.append(f"| {column} | {descriptions.get(column, '分位置差值')} |")

    lines.extend(
        [
            "",
            "## 分位置特征",
            "",
            "`gold_diff_TOP` / `xp_diff_TOP` / `level_diff_TOP` / `cs_diff_TOP`，",
            "以及 JUNGLE、MIDDLE、BOTTOM、UTILITY。",
            "",
            "## 打野 / 击杀特征",
            "",
            "- `kills_diff_<ROLE>` / `deaths_diff_<ROLE>` / `assists_diff_<ROLE>`：分位置累计 KDA 差。",
            "- `kill_lane_<LANE>_diff`：击杀发生在上/中/下/野区的次数差。",
            "- `jungler_zone_<LANE>_diff`：双方打野当前分钟所在区域（上/中/下）的差。",
            "- `jungler_near_<LANE>_diff`：双方打野是否靠近该路（严格路线分类）的差。",
            "- `jungler_gank_<LANE>_diff`：双方打野参与该路击杀的累计次数差。",
            "- `blue_jungle_start_<CAMP>` / `red_jungle_start_<CAMP>`：首清营地 one-hot",
            "  （`blue` / `red` / `wolves` / `raptors`）。",
            "- `*_jungle_start_invade`：首清是否入侵对方野区。",
            "",
            "## 英雄",
            "",
            "`blue_champ_TOP` 等 10 列是 championId；训练时按 categorical 处理。",
            "",
            "## 切分",
            "",
            "`split.json` 按 gameId 分组切分，同一局不会同时出现在 train 和 val/test。",
        ]
    )

    return "\n".join(lines) + "\n"


def build(
    export_paths: List[Path],
    output_directory: Path,
    queues: Sequence[int],
    min_duration: int,
    sides: bool,
) -> int:
    all_rows: List[Dict[str, Any]] = []
    used_games: List[int] = []
    skipped: List[str] = []

    for export in export_paths:
        try:
            summary = load_json(export / "summary.json")
            timeline = load_json(export / "timeline" / "timeline.json")
        except Exception as error:
            skipped.append(f"{export}: {error}")
            continue

        game = pick_game(summary, timeline)
        if not game:
            skipped.append(f"{export}: no matching game in summary.json")
            continue

        queue_id = game.get("queueId")
        if queue_id not in queues:
            skipped.append(f"{export}: queue {queue_id} not in {list(queues)}")
            continue

        if game.get("gameMode") != "CLASSIC" or game.get("mapId") != 11:
            skipped.append(f"{export}: not Summoner's Rift CLASSIC")
            continue

        if int(game.get("gameDuration") or 0) < min_duration:
            skipped.append(f"{export}: duration < {min_duration}s")
            continue

        if game.get("endOfGameResult") not in (None, "GameComplete"):
            skipped.append(f"{export}: endOfGameResult={game.get('endOfGameResult')}")
            continue

        rows = build_game_rows(game, timeline)

        if not rows:
            skipped.append(f"{export}: no usable frames")
            continue

        all_rows.extend(rows)
        used_games.append(game["gameId"])

    if not all_rows:
        print("[错误] 没有符合条件的对局。", file=sys.stderr)
        for item in skipped[:20]:
            print(f"  - {item}", file=sys.stderr)
        return 1

    output_directory.mkdir(parents=True, exist_ok=True)

    columns = BASE_COLUMNS + FEATURE_COLUMNS
    blue_count = write_csv(output_directory / "dataset_blue.csv", all_rows, columns)

    side_count = 0
    if sides:
        def side_rows() -> Iterable[Dict[str, Any]]:
            for row in all_rows:
                yield row
                yield flip_row(row)

        side_count = write_csv(
            output_directory / "dataset_sides.csv", side_rows(), columns
        )

    split = build_split(used_games)
    (output_directory / "split.json").write_text(
        json.dumps(
            {
                "seed": 42,
                "gameCount": len(used_games),
                "train": split["train"],
                "val": split["val"],
                "test": split["test"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (output_directory / "data_dictionary.md").write_text(
        data_dictionary(), encoding="utf-8"
    )

    manifest = {
        "createdAt": datetime.now(tz=timezone.utc).isoformat(),
        "queues": list(queues),
        "minDurationSeconds": min_duration,
        "games": len(used_games),
        "blueRows": blue_count,
        "sideRows": side_count,
        "columns": columns,
        "skipped": skipped,
    }
    (output_directory / "dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Games: {len(used_games)}")
    print(f"Blue rows: {blue_count}")
    print(f"Side rows: {side_count}")
    print(f"Skipped: {len(skipped)}")
    print(f"Output: {output_directory}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="从导出目录生成 1 分钟颗粒度的训练数据集",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--export-root",
        type=Path,
        default=None,
        help="包含多个导出目录的根目录",
    )
    parser.add_argument(
        "--exports",
        type=Path,
        nargs="+",
        default=None,
        help="一个或多个具体导出目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出目录，默认是 <export-root>/dataset",
    )
    parser.add_argument(
        "--queues",
        default="420,440",
        help="逗号分隔的队列 ID",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=MIN_DURATION_SECONDS,
        help="最短对局时长（秒）",
    )
    parser.add_argument(
        "--no-sides",
        action="store_true",
        help="不生成红方视角的 dataset_sides.csv",
    )
    args = parser.parse_args(argv)

    queues = tuple(int(value) for value in args.queues.split(",") if value.strip())

    if args.exports:
        export_paths = args.exports
        base = args.exports[0].parent
    elif args.export_root:
        export_paths = find_exports(args.export_root)
        base = args.export_root
    else:
        print("[错误] 需要 --export-root 或 --exports", file=sys.stderr)
        return 1

    if not export_paths:
        print("[错误] 没有找到包含 summary.json + timeline/timeline.json 的目录", file=sys.stderr)
        return 1

    output = args.output or (base / "dataset")

    return build(
        export_paths,
        output,
        queues,
        args.min_duration,
        not args.no_sides,
    )


if __name__ == "__main__":
    raise SystemExit(main())
