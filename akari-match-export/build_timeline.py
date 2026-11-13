#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a readable match timeline from an exported details.jsonl.

The exporter already stores the SGP DETAILS payload, whose `frames` contain:

  * `timestamp`
  * `events`             - kills, item purchases, wards, buildings, monsters...
  * `participantFrames`  - level / currentGold / totalGold / xp / cs / position

This script merges those into:

  * timeline.json         machine-readable frames + normalized events
  * timeline_events.csv   one row per event, with actor/victim gold + xp
  * timeline_frames.csv   one row per player per minute
  * timeline.md           human-readable event log + economy table
  * summary.txt           counts
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


EVENT_LABELS = {
    "CHAMPION_KILL": "击杀",
    "CHAMPION_SPECIAL_KILL": "特殊击杀",
    "BUILDING_KILL": "摧毁建筑",
    "ELITE_MONSTER_KILL": "史诗野怪",
    "TURRET_PLATE_DESTROYED": "镀层",
    "WARD_PLACED": "插眼",
    "WARD_KILL": "排眼",
    "ITEM_PURCHASED": "购买装备",
    "ITEM_SOLD": "出售装备",
    "ITEM_DESTROYED": "消耗/移除装备",
    "ITEM_UNDO": "撤销购买",
    "SKILL_LEVEL_UP": "技能升级",
    "LEVEL_UP": "升级",
    "DRAGON_SOUL_GIVEN": "龙魂",
    "GAME_END": "游戏结束",
    "PAUSE_END": "暂停结束",
}

MONSTER_LABELS = {
    "DRAGON": "小龙",
    "BARON_NASHOR": "大龙",
    "RIFTHERALD": "峡谷先锋",
    "HORDE": "虚空巢虫",
    "ATAKHAN": "厄塔汗",
}

BUILDING_LABELS = {
    "TOWER_BUILDING": "防御塔",
    "INHIBITOR_BUILDING": "水晶",
}

TOWER_LABELS = {
    "OUTER_TURRET": "外塔",
    "INNER_TURRET": "二塔",
    "BASE_TURRET": "高地塔",
    "NEXUS_TURRET": "门牙塔",
}

LANE_LABELS = {
    "TOP_LANE": "上路",
    "MID_LANE": "中路",
    "BOT_LANE": "下路",
}

WARD_LABELS = {
    "YELLOW_TRINKET": "黄色饰品",
    "BLUE_TRINKET": "蓝色饰品",
    "CONTROL_WARD": "控制守卫",
    "SIGHT_WARD": "假眼",
    "TEEMO_MUSHROOM": "提莫蘑菇",
}

KILL_LABELS = {
    "KILL_FIRST_BLOOD": "一血",
    "KILL_MULTI": "多杀",
    "KILL_ACE": "团灭",
}

SKILL_LABELS = {1: "Q", 2: "W", 3: "E", 4: "R"}


def mmss(timestamp_ms: Any) -> str:
    try:
        total = int(float(timestamp_ms) / 1000)
    except (TypeError, ValueError):
        return "00:00"

    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def find_export_files(export_directory: Path) -> Tuple[Path, Path]:
    summary = export_directory / "summary.json"
    details = export_directory / "details.jsonl"

    if not summary.is_file():
        raise FileNotFoundError(f"找不到 summary.json: {summary}")
    if not details.is_file():
        raise FileNotFoundError(f"找不到 details.jsonl: {details}")

    return summary, details


def load_details(details_path: Path, game_id: Optional[int]) -> Dict[str, Any]:
    with details_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue

            record = json.loads(line)
            current_id = record.get("gameId")

            if game_id is None or current_id == game_id:
                details = record.get("details") or {}
                data = details.get("json") or {}
                return {
                    "gameId": current_id,
                    "metadata": details.get("metadata") or {},
                    "json": data,
                }

    raise ValueError(
        f"details.jsonl 中没有找到 gameId={game_id or '任意'}"
    )


def find_summary_game(summary: Dict[str, Any], game_id: int) -> Dict[str, Any]:
    for game in summary.get("games") or []:
        game_json = game.get("json") or {}
        if game_json.get("gameId") == game_id:
            return game_json

    raise ValueError(f"summary.json 中没有找到 gameId={game_id}")


def fetch_item_names() -> Dict[str, str]:
    """Best-effort item name lookup from the local client."""
    try:
        import match_export as exporter

        auth = exporter.discover_lcu_auth()
        lcu = exporter.LcuClient(auth)
        items = lcu.get_json("/lol-game-data/assets/v1/items.json")
    except Exception:
        return {}

    names: Dict[str, str] = {}

    if isinstance(items, dict):
        iterable = items.items()
    elif isinstance(items, list):
        iterable = ((item.get("id"), item) for item in items if isinstance(item, dict))
    else:
        return names

    for item_id, item in iterable:
        if item_id is None or not isinstance(item, dict):
            continue

        if item.get("name"):
            names[str(item_id)] = str(item["name"])

    return names


def build_players(
    summary_game: Dict[str, Any], details_data: Dict[str, Any]
) -> Dict[int, Dict[str, Any]]:
    players: Dict[int, Dict[str, Any]] = {}

    for participant in summary_game.get("participants") or []:
        participant_id = participant.get("participantId")
        if not isinstance(participant_id, int):
            continue

        game_name = participant.get("riotIdGameName") or participant.get("summonerName") or ""
        tag_line = participant.get("riotIdTagline") or ""
        display_name = f"{game_name}#{tag_line}" if tag_line else game_name

        players[participant_id] = {
            "participantId": participant_id,
            "puuid": participant.get("puuid"),
            "name": display_name or f"Player {participant_id}",
            "champion": participant.get("championName") or "",
            "teamId": participant.get("teamId"),
            "position": participant.get("teamPosition")
            or participant.get("individualPosition")
            or "",
        }

    # details.participants only carries participantId + puuid; use it to fill gaps.
    for participant in details_data.get("participants") or []:
        participant_id = participant.get("participantId")
        if not isinstance(participant_id, int):
            continue

        player = players.setdefault(
            participant_id,
            {
                "participantId": participant_id,
                "puuid": participant.get("puuid"),
                "name": f"Player {participant_id}",
                "champion": "",
                "teamId": None,
                "position": "",
            },
        )

        if not player.get("puuid"):
            player["puuid"] = participant.get("puuid")

    return players


def normalize_frames(
    details_data: Dict[str, Any], players: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = []

    for frame in details_data.get("frames") or []:
        timestamp = frame.get("timestamp", 0)
        participant_frames = frame.get("participantFrames") or {}
        participants: Dict[str, Any] = {}

        for key, value in participant_frames.items():
            try:
                participant_id = int(key)
            except (TypeError, ValueError):
                continue

            position = value.get("position") or {}

            participants[str(participant_id)] = {
                "participantId": participant_id,
                "name": players.get(participant_id, {}).get("name", f"Player {participant_id}"),
                "champion": players.get(participant_id, {}).get("champion", ""),
                "level": value.get("level"),
                "currentGold": value.get("currentGold"),
                "totalGold": value.get("totalGold"),
                "xp": value.get("xp"),
                "minionsKilled": value.get("minionsKilled"),
                "jungleMinionsKilled": value.get("jungleMinionsKilled"),
                "cs": (value.get("minionsKilled") or 0)
                + (value.get("jungleMinionsKilled") or 0),
                "x": position.get("x"),
                "y": position.get("y"),
            }

        frames.append(
            {
                "timestamp": timestamp,
                "time": mmss(timestamp),
                "participants": participants,
            }
        )

    frames.sort(key=lambda item: item["timestamp"])
    return frames


def state_at(
    frame_timestamps: List[int], frames: List[Dict[str, Any]], timestamp: int, participant_id: int
) -> Optional[Dict[str, Any]]:
    index = bisect_right(frame_timestamps, timestamp) - 1

    if index < 0:
        return None

    frame = frames[index]
    state = frame["participants"].get(str(participant_id))

    if not state:
        return None

    return {
        "frameTimestamp": frame["timestamp"],
        "frameTime": frame["time"],
        "level": state.get("level"),
        "currentGold": state.get("currentGold"),
        "totalGold": state.get("totalGold"),
        "xp": state.get("xp"),
        "cs": state.get("cs"),
    }


def normalize_events(
    details_data: Dict[str, Any],
    players: Dict[int, Dict[str, Any]],
    frames: List[Dict[str, Any]],
    item_names: Dict[str, str],
) -> List[Dict[str, Any]]:
    frame_timestamps = [frame["timestamp"] for frame in frames]
    raw_events: List[Dict[str, Any]] = []

    for frame in details_data.get("frames") or []:
        for event in frame.get("events") or []:
            raw_events.append(event)

    raw_events.sort(key=lambda item: item.get("timestamp", 0))

    events: List[Dict[str, Any]] = []

    for event in raw_events:
        event_type = str(event.get("type") or "UNKNOWN")
        timestamp = int(event.get("timestamp") or 0)

        actor_id = (
            event.get("killerId")
            or event.get("participantId")
            or event.get("creatorId")
            or 0
        )
        victim_id = event.get("victimId") or 0

        actor = players.get(actor_id, {})
        victim = players.get(victim_id, {})

        assist_ids = [
            int(value)
            for value in (event.get("assistingParticipantIds") or [])
            if isinstance(value, int)
        ]
        assist_names = [
            players.get(pid, {}).get("name", f"Player {pid}") for pid in assist_ids
        ]

        item_id = event.get("itemId")
        if item_id is None and event_type == "ITEM_UNDO":
            item_id = event.get("beforeId")

        item_name = item_names.get(str(item_id)) if item_id is not None else None

        position = event.get("position") or {}

        normalized = {
            "timestamp": timestamp,
            "time": mmss(timestamp),
            "type": event_type,
            "label": EVENT_LABELS.get(event_type, event_type),
            "actorId": actor_id or None,
            "actorName": actor.get("name") if actor_id else None,
            "actorChampion": actor.get("champion") if actor_id else None,
            "victimId": victim_id or None,
            "victimName": victim.get("name") if victim_id else None,
            "victimChampion": victim.get("champion") if victim_id else None,
            "assistIds": assist_ids,
            "assistNames": assist_names,
            "itemId": item_id,
            "itemName": item_name,
            "monsterType": event.get("monsterType") or None,
            "monsterSubType": event.get("monsterSubType") or None,
            "buildingType": event.get("buildingType") or None,
            "towerType": event.get("towerType") or None,
            "laneType": event.get("laneType") or None,
            "teamId": event.get("teamId") or event.get("killerTeamId") or None,
            "wardType": event.get("wardType") or None,
            "skillSlot": event.get("skillSlot"),
            "level": event.get("level"),
            "killType": event.get("killType") or None,
            "multiKillLength": event.get("multiKillLength"),
            "bounty": event.get("bounty"),
            "shutdownBounty": event.get("shutdownBounty"),
            "goldGain": event.get("goldGain"),
            "winningTeam": event.get("winningTeam"),
            "x": position.get("x"),
            "y": position.get("y"),
            "actorState": state_at(frame_timestamps, frames, timestamp, actor_id)
            if actor_id
            else None,
            "victimState": state_at(frame_timestamps, frames, timestamp, victim_id)
            if victim_id
            else None,
        }

        events.append(normalized)

    return events


def event_description(event: Dict[str, Any]) -> str:
    event_type = event["type"]
    actor = event.get("actorName") or "未知"
    actor_champion = event.get("actorChampion") or ""
    actor_label = f"{actor} ({actor_champion})" if actor_champion else actor

    if event_type == "CHAMPION_KILL":
        victim = event.get("victimName") or "未知"
        victim_champion = event.get("victimChampion") or ""
        victim_label = f"{victim} ({victim_champion})" if victim_champion else victim
        text = f"{actor_label} 击杀 {victim_label}"
        assists = event.get("assistNames") or []
        if assists:
            text += f"，助攻: {', '.join(assists)}"
        return text

    if event_type == "CHAMPION_SPECIAL_KILL":
        kill_label = KILL_LABELS.get(event.get("killType") or "", event.get("killType") or "")
        return f"{actor_label} 达成 {kill_label}"

    if event_type == "ITEM_PURCHASED":
        item = event.get("itemName") or f"item {event.get('itemId')}"
        return f"{actor_label} 购买 {item}"

    if event_type == "ITEM_SOLD":
        item = event.get("itemName") or f"item {event.get('itemId')}"
        return f"{actor_label} 出售 {item}"

    if event_type == "ITEM_UNDO":
        item = event.get("itemName") or f"item {event.get('beforeId')}"
        return f"{actor_label} 撤销购买 {item}，返还 {event.get('goldGain') or 0} 金币"

    if event_type == "ITEM_DESTROYED":
        item = event.get("itemName") or f"item {event.get('itemId')}"
        return f"{actor_label} 消耗/移除 {item}"

    if event_type == "BUILDING_KILL":
        building = BUILDING_LABELS.get(event.get("buildingType") or "", event.get("buildingType") or "建筑")
        tower = TOWER_LABELS.get(event.get("towerType") or "", event.get("towerType") or "")
        lane = LANE_LABELS.get(event.get("laneType") or "", event.get("laneType") or "")
        detail = " ".join(value for value in (tower, lane) if value)
        return f"{actor_label} 摧毁 {building}" + (f"（{detail}）" if detail else "")

    if event_type == "ELITE_MONSTER_KILL":
        monster = MONSTER_LABELS.get(
            event.get("monsterType") or "",
            event.get("monsterType") or "史诗野怪",
        )
        subtype = event.get("monsterSubType") or ""
        return f"{actor_label} 击杀 {monster}" + (f"（{subtype}）" if subtype else "")

    if event_type == "TURRET_PLATE_DESTROYED":
        lane = LANE_LABELS.get(event.get("laneType") or "", event.get("laneType") or "")
        return f"{actor_label} 摧毁镀层" + (f"（{lane}）" if lane else "")

    if event_type == "WARD_PLACED":
        ward = WARD_LABELS.get(event.get("wardType") or "", event.get("wardType") or "眼")
        return f"{actor_label} 插下 {ward}"

    if event_type == "WARD_KILL":
        ward = WARD_LABELS.get(event.get("wardType") or "", event.get("wardType") or "眼")
        return f"{actor_label} 排掉 {ward}"

    if event_type == "SKILL_LEVEL_UP":
        skill = SKILL_LABELS.get(event.get("skillSlot"), str(event.get("skillSlot")))
        return f"{actor_label} 升级技能 {skill}"

    if event_type == "LEVEL_UP":
        return f"{actor_label} 升到 {event.get('level')} 级"

    if event_type == "DRAGON_SOUL_GIVEN":
        return f"队伍 {event.get('teamId')} 获得 {event.get('name') or ''} 龙魂"

    if event_type == "GAME_END":
        return f"游戏结束，队伍 {event.get('winningTeam')} 获胜"

    return f"{actor_label} {EVENT_LABELS.get(event_type, event_type)}"


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build(
    summary_path: Path,
    details_path: Path,
    output_directory: Path,
    game_id: Optional[int],
    with_item_names: bool,
    item_names: Optional[Dict[str, str]] = None,
) -> int:
    summary = load_json(summary_path)
    details = load_details(details_path, game_id)
    details_data = details["json"]

    resolved_game_id = details["gameId"]
    summary_game = find_summary_game(summary, resolved_game_id)

    players = build_players(summary_game, details_data)
    frames = normalize_frames(details_data, players)
    # item_names 为 None 时保持旧行为（自行探测 LCU 拉取）；传入 dict
    # （即使为空）表示调用方已预取，直接复用、不再探测。
    if item_names is None:
        item_names = fetch_item_names() if with_item_names else {}
    events = normalize_events(details_data, players, frames, item_names)

    output_directory.mkdir(parents=True, exist_ok=True)

    game_duration = summary_game.get("gameDuration") or 0
    winning_team = next(
        (event.get("winningTeam") for event in events if event["type"] == "GAME_END"),
        None,
    )

    timeline = {
        "gameId": resolved_game_id,
        "gameVersion": summary_game.get("gameVersion"),
        "queueId": summary_game.get("queueId"),
        "gameMode": summary_game.get("gameMode"),
        "gameDurationMs": game_duration * 1000,
        "frameInterval": details_data.get("frameInterval", 60000),
        "winningTeam": winning_team,
        "players": sorted(
            players.values(), key=lambda player: player["participantId"]
        ),
        "frames": frames,
        "events": events,
    }
    write_json(output_directory / "timeline.json", timeline)

    # Frames CSV: one row per participant per minute.
    with (output_directory / "timeline_frames.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fieldnames = [
            "timeMs",
            "time",
            "participantId",
            "name",
            "champion",
            "teamId",
            "position",
            "level",
            "currentGold",
            "totalGold",
            "xp",
            "minionsKilled",
            "jungleMinionsKilled",
            "cs",
            "x",
            "y",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for frame in frames:
            for participant_id, state in sorted(
                frame["participants"].items(), key=lambda item: int(item[0])
            ):
                player = players.get(int(participant_id), {})
                writer.writerow(
                    {
                        "timeMs": frame["timestamp"],
                        "time": frame["time"],
                        "participantId": participant_id,
                        "name": state.get("name", ""),
                        "champion": state.get("champion", ""),
                        "teamId": player.get("teamId", ""),
                        "position": player.get("position", ""),
                        "level": state.get("level", ""),
                        "currentGold": state.get("currentGold", ""),
                        "totalGold": state.get("totalGold", ""),
                        "xp": state.get("xp", ""),
                        "minionsKilled": state.get("minionsKilled", ""),
                        "jungleMinionsKilled": state.get("jungleMinionsKilled", ""),
                        "cs": state.get("cs", ""),
                        "x": state.get("x", ""),
                        "y": state.get("y", ""),
                    }
                )

    # Events CSV: one row per event, with actor/victim economy snapshots.
    with (output_directory / "timeline_events.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        fieldnames = [
            "timeMs",
            "time",
            "type",
            "label",
            "actorId",
            "actorName",
            "actorChampion",
            "actorFrameTime",
            "actorLevel",
            "actorCurrentGold",
            "actorTotalGold",
            "actorXp",
            "actorCs",
            "victimId",
            "victimName",
            "victimChampion",
            "victimFrameTime",
            "victimLevel",
            "victimCurrentGold",
            "victimTotalGold",
            "victimXp",
            "victimCs",
            "assistNames",
            "itemId",
            "itemName",
            "monsterType",
            "monsterSubType",
            "buildingType",
            "towerType",
            "laneType",
            "teamId",
            "wardType",
            "skillSlot",
            "level",
            "killType",
            "multiKillLength",
            "bounty",
            "shutdownBounty",
            "goldGain",
            "winningTeam",
            "x",
            "y",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for event in events:
            actor_state = event.get("actorState") or {}
            victim_state = event.get("victimState") or {}
            writer.writerow(
                {
                    "timeMs": event["timestamp"],
                    "time": event["time"],
                    "type": event["type"],
                    "label": event["label"],
                    "actorId": event.get("actorId") or "",
                    "actorName": event.get("actorName") or "",
                    "actorChampion": event.get("actorChampion") or "",
                    "actorFrameTime": actor_state.get("frameTime", ""),
                    "actorLevel": actor_state.get("level", ""),
                    "actorCurrentGold": actor_state.get("currentGold", ""),
                    "actorTotalGold": actor_state.get("totalGold", ""),
                    "actorXp": actor_state.get("xp", ""),
                    "actorCs": actor_state.get("cs", ""),
                    "victimId": event.get("victimId") or "",
                    "victimName": event.get("victimName") or "",
                    "victimChampion": event.get("victimChampion") or "",
                    "victimFrameTime": victim_state.get("frameTime", ""),
                    "victimLevel": victim_state.get("level", ""),
                    "victimCurrentGold": victim_state.get("currentGold", ""),
                    "victimTotalGold": victim_state.get("totalGold", ""),
                    "victimXp": victim_state.get("xp", ""),
                    "victimCs": victim_state.get("cs", ""),
                    "assistNames": ";".join(event.get("assistNames") or []),
                    "itemId": event.get("itemId") or "",
                    "itemName": event.get("itemName") or "",
                    "monsterType": event.get("monsterType") or "",
                    "monsterSubType": event.get("monsterSubType") or "",
                    "buildingType": event.get("buildingType") or "",
                    "towerType": event.get("towerType") or "",
                    "laneType": event.get("laneType") or "",
                    "teamId": event.get("teamId") or "",
                    "wardType": event.get("wardType") or "",
                    "skillSlot": event.get("skillSlot") or "",
                    "level": event.get("level") or "",
                    "killType": event.get("killType") or "",
                    "multiKillLength": event.get("multiKillLength") or "",
                    "bounty": event.get("bounty") or "",
                    "shutdownBounty": event.get("shutdownBounty") or "",
                    "goldGain": event.get("goldGain") or "",
                    "winningTeam": event.get("winningTeam") or "",
                    "x": event.get("x") or "",
                    "y": event.get("y") or "",
                }
            )

    # Human-readable markdown.
    lines: List[str] = [
        f"# 对局时间线 {resolved_game_id}",
        "",
        f"- 游戏版本: {summary_game.get('gameVersion')}",
        f"- 模式: {summary_game.get('gameMode')} / 队列 {summary_game.get('queueId')}",
        f"- 时长: {mmss(game_duration * 1000)}",
        f"- 胜方: 队伍 {winning_team if winning_team is not None else '未知'}",
        "",
        "## 玩家",
        "",
        "| 队伍 | 位置 | 玩家 | 英雄 |",
        "|---:|---|---|---|",
    ]

    for player in sorted(players.values(), key=lambda item: (item.get("teamId") or 0, item["participantId"])):
        lines.append(
            f"| {player.get('teamId') or '-'} | {player.get('position') or '-'} | "
            f"{player.get('name')} | {player.get('champion') or '-'} |"
        )

    lines.extend(["", "## 事件", ""])

    for event in events:
        lines.append(f"- `[{event['time']}]` **{event['label']}** — {event_description(event)}")

        actor_state = event.get("actorState")
        if actor_state:
            lines.append(
                "  - "
                f"{event.get('actorName')}: Lv {actor_state.get('level')} · "
                f"当前金币 {actor_state.get('currentGold')} · "
                f"总金币 {actor_state.get('totalGold')} · "
                f"经验 {actor_state.get('xp')} · CS {actor_state.get('cs')}"
                f"（{actor_state.get('frameTime')} 帧）"
            )

        victim_state = event.get("victimState")
        if victim_state:
            lines.append(
                "  - "
                f"{event.get('victimName')}: Lv {victim_state.get('level')} · "
                f"当前金币 {victim_state.get('currentGold')} · "
                f"总金币 {victim_state.get('totalGold')} · "
                f"经验 {victim_state.get('xp')} · CS {victim_state.get('cs')}"
                f"（{victim_state.get('frameTime')} 帧）"
            )

    lines.extend(
        [
            "",
            "## 每分钟经济 / 经验",
            "",
            "| 时间 | 玩家 | 英雄 | 等级 | 当前金币 | 总金币 | 经验 | 补刀 |",
            "|---:|---|---|---:|---:|---:|---:|---:|",
        ]
    )

    for frame in frames:
        for participant_id, state in sorted(
            frame["participants"].items(), key=lambda item: int(item[0])
        ):
            lines.append(
                f"| {frame['time']} | {state.get('name')} | {state.get('champion')} | "
                f"{state.get('level')} | {state.get('currentGold')} | "
                f"{state.get('totalGold')} | {state.get('xp')} | {state.get('cs')} |"
            )

    (output_directory / "timeline.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    type_counts: Dict[str, int] = {}
    for event in events:
        type_counts[event["type"]] = type_counts.get(event["type"], 0) + 1

    summary_lines = [
        f"Game ID: {resolved_game_id}",
        f"Game version: {summary_game.get('gameVersion')}",
        f"Duration: {mmss(game_duration * 1000)}",
        f"Players: {len(players)}",
        f"Frames: {len(frames)}",
        f"Events: {len(events)}",
        "",
        "Event types:",
    ]

    for event_type, count in sorted(
        type_counts.items(), key=lambda item: item[1], reverse=True
    ):
        summary_lines.append(f"  {event_type:32s} {count}")

    (output_directory / "summary.txt").write_text(
        "\n".join(summary_lines) + "\n", encoding="utf-8"
    )

    print("\n".join(summary_lines))
    print()
    print(f"Output: {output_directory}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="从导出的 details.jsonl 生成对局时间线（事件 + 金币/经验）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--export",
        type=Path,
        default=None,
        help="导出目录（包含 summary.json 和 details.jsonl）",
    )
    parser.add_argument("--summary", type=Path, default=None, help="summary.json 路径")
    parser.add_argument("--details", type=Path, default=None, help="details.jsonl 路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出目录，默认是 <export>/timeline",
    )
    parser.add_argument("--game-id", type=int, default=None, help="指定 gameId")
    parser.add_argument(
        "--no-item-names",
        action="store_true",
        help="不向客户端查询装备名称",
    )
    args = parser.parse_args(argv)

    if args.summary and args.details:
        summary_path = args.summary
        details_path = args.details
        base_directory = args.export or args.details.parent
    elif args.export:
        summary_path, details_path = find_export_files(args.export)
        base_directory = args.export
    else:
        print(
            "[错误] 需要 --export，或同时提供 --summary 和 --details",
            file=sys.stderr,
        )
        return 1

    output_directory = args.output or (base_directory / "timeline")

    try:
        return build(
            summary_path,
            details_path,
            output_directory,
            args.game_id,
            not args.no_item_names,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
