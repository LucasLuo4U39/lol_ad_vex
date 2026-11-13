#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a 10-second event dataset for kill / gank prediction.

Important: the source (SGP DETAILS) only has 60-second frames. The 10-second
grid therefore forward-fills the latest 1-minute state and adds exact event
recency/countdown features. Labels use exact event timestamps, so label timing
is 10-second accurate; the underlying player state is still minute-resolution.

Labels:
  kill_30s         any champion kill in (t, t+30]
  blue_kill_30s    blue team kill in (t, t+30]
  red_kill_30s     red team kill in (t, t+30]
  blue_gank_60s    blue jungler involved in a lane kill in (t, t+60]
  red_gank_60s     red jungler involved in a lane kill in (t, t+60]
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

DROP_ROW_COLUMNS = {
    "win",
    "blue_win",
    "gold_diff_plus5",
    "side",
    "game_mode",
    "queue_id",
}
DROP_ROW_COLUMNS |= set(dataset.CHAMPION_COLUMNS)

LABELS = (
    "kill_30s",
    "blue_kill_30s",
    "red_kill_30s",
    "blue_gank_60s",
    "red_gank_60s",
)


def seconds(ms: Any) -> float:
    try:
        return float(ms) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def since(times: List[float], timestamp: float) -> float:
    index = bisect.bisect_right(times, timestamp) - 1
    return 9999.0 if index < 0 else timestamp - times[index]


def count_between(times: List[float], start: float, end: float) -> int:
    return bisect.bisect_right(times, end) - bisect.bisect_right(times, start)


def event_arrays(
    timeline: Dict[str, Any],
) -> Tuple[Dict[str, List[float]], Dict[str, List[float]]]:
    players_team = dataset.player_team_map(timeline)
    roles = dataset.role_map(timeline)
    junglers = {
        team: pid
        for pid, team in players_team.items()
        if roles.get(pid) == "JUNGLE" and team in (100, 200)
    }

    kills: List[Tuple[float, Optional[int]]] = []
    ganks: List[Tuple[float, Optional[int]]] = []
    dragon: List[float] = []
    baron: List[float] = []
    herald: List[float] = []
    grub: List[float] = []
    atakhan: List[float] = []
    tower: List[float] = []

    for event in timeline.get("events") or []:
        event_type = event.get("type")
        timestamp = seconds(event.get("timestamp"))
        actor_id = event.get("actorId")
        team = players_team.get(actor_id) if isinstance(actor_id, int) else None

        if event_type == "CHAMPION_KILL":
            kills.append((timestamp, team))

            x = event.get("x")
            y = event.get("y")
            lane = None
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                lane = dataset.classify_gank_lane(float(x), float(y))

            if lane in ("top", "mid", "bot"):
                involved = {actor_id} | set(event.get("assistIds") or [])
                for gank_team, jungler_id in junglers.items():
                    if jungler_id in involved:
                        ganks.append((timestamp, gank_team))

        elif event_type == "ELITE_MONSTER_KILL":
            monster = event.get("monsterType")
            if monster == "DRAGON":
                dragon.append(timestamp)
            elif monster == "BARON_NASHOR":
                baron.append(timestamp)
            elif monster == "RIFTHERALD":
                herald.append(timestamp)
            elif monster == "HORDE":
                grub.append(timestamp)
            elif monster == "ATAKHAN":
                atakhan.append(timestamp)

        elif event_type == "BUILDING_KILL":
            tower.append(timestamp)

    times: Dict[str, List[float]] = {
        "kill": sorted(timestamp for timestamp, _ in kills),
        "blue_kill": sorted(timestamp for timestamp, team in kills if team == 100),
        "red_kill": sorted(timestamp for timestamp, team in kills if team == 200),
        "gank": sorted(timestamp for timestamp, _ in ganks),
        "blue_gank": sorted(timestamp for timestamp, team in ganks if team == 100),
        "red_gank": sorted(timestamp for timestamp, team in ganks if team == 200),
        "dragon": sorted(dragon),
        "baron": sorted(baron),
        "herald": sorted(herald),
        "grub": sorted(grub),
        "atakhan": sorted(atakhan),
        "tower": sorted(tower),
    }

    return times, {}


def recency_features(times: Dict[str, List[float]], timestamp: float) -> Dict[str, Any]:
    epic_times = sorted(
        times["dragon"]
        + times["baron"]
        + times["herald"]
        + times["grub"]
        + times["atakhan"]
    )

    return {
        "since_kill": since(times["kill"], timestamp),
        "since_blue_kill": since(times["blue_kill"], timestamp),
        "since_red_kill": since(times["red_kill"], timestamp),
        "since_gank": since(times["gank"], timestamp),
        "since_blue_gank": since(times["blue_gank"], timestamp),
        "since_red_gank": since(times["red_gank"], timestamp),
        "since_dragon": since(times["dragon"], timestamp),
        "since_baron": since(times["baron"], timestamp),
        "since_herald": since(times["herald"], timestamp),
        "since_tower": since(times["tower"], timestamp),
        "kills_last_60": count_between(times["kill"], timestamp - 60, timestamp),
        "kills_last_180": count_between(times["kill"], timestamp - 180, timestamp),
        "blue_kills_last_180": count_between(
            times["blue_kill"], timestamp - 180, timestamp
        ),
        "red_kills_last_180": count_between(
            times["red_kill"], timestamp - 180, timestamp
        ),
        "ganks_last_300": count_between(times["gank"], timestamp - 300, timestamp),
        "blue_ganks_last_300": count_between(
            times["blue_gank"], timestamp - 300, timestamp
        ),
        "red_ganks_last_300": count_between(
            times["red_gank"], timestamp - 300, timestamp
        ),
        "epics_last_300": count_between(epic_times, timestamp - 300, timestamp),
    }


def label_features(times: Dict[str, List[float]], timestamp: float) -> Dict[str, int]:
    return {
        "kill_30s": int(count_between(times["kill"], timestamp, timestamp + 30) > 0),
        "blue_kill_30s": int(
            count_between(times["blue_kill"], timestamp, timestamp + 30) > 0
        ),
        "red_kill_30s": int(
            count_between(times["red_kill"], timestamp, timestamp + 30) > 0
        ),
        "blue_gank_60s": int(
            count_between(times["blue_gank"], timestamp, timestamp + 60) > 0
        ),
        "red_gank_60s": int(
            count_between(times["red_gank"], timestamp, timestamp + 60) > 0
        ),
    }


def build(
    export_paths: List[Path],
    output: Path,
    interval: int,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)

    writer: Optional[csv.DictWriter] = None
    handle = None
    rows_written = 0
    games_used = 0
    positive_counts = {label: 0 for label in LABELS}
    skipped: List[str] = []

    try:
        for export in export_paths:
            try:
                summary = dataset.load_json(export / "summary.json")
                timeline = dataset.load_json(export / "timeline" / "timeline.json")
            except Exception as error:
                skipped.append(f"{export}: {error}")
                continue

            game = dataset.pick_game(summary, timeline)
            if not game:
                skipped.append(f"{export}: no matching game")
                continue

            if (
                game.get("queueId") not in (420, 440)
                or game.get("gameMode") != "CLASSIC"
                or game.get("mapId") != 11
            ):
                continue

            duration = int(game.get("gameDuration") or 0)
            if duration < 600:
                continue

            minute_rows = dataset.build_game_rows(game, timeline)
            if not minute_rows:
                continue

            rows_by_minute = {int(row["minute"]): row for row in minute_rows}
            max_minute = max(rows_by_minute)
            times, _ = event_arrays(timeline)

            for timestamp in range(60, duration - interval + 1, interval):
                minute = timestamp // 60
                if minute < 1 or minute > max_minute:
                    continue

                row = rows_by_minute.get(minute)
                if not row:
                    continue

                event_row = dict(row)
                for column in DROP_ROW_COLUMNS:
                    event_row.pop(column, None)

                event_row["t"] = timestamp
                event_row["secondsSinceMinute"] = timestamp - minute * 60
                event_row.update(recency_features(times, timestamp))
                event_row.update(label_features(times, timestamp))

                if writer is None:
                    handle = output.open("w", encoding="utf-8-sig", newline="")
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=list(event_row.keys()),
                        extrasaction="ignore",
                    )
                    writer.writeheader()

                writer.writerow(event_row)
                rows_written += 1

                for label in LABELS:
                    positive_counts[label] += int(event_row[label])

            games_used += 1

            if games_used % 100 == 0:
                print(f"  games={games_used} rows={rows_written}")
    finally:
        if handle:
            handle.close()

    if writer is None:
        print("[错误] 没有生成任何样本", file=sys.stderr)
        return 1

    manifest = {
        "games": games_used,
        "rows": rows_written,
        "intervalSeconds": interval,
        "labels": {
            label: {
                "positive": positive_counts[label],
                "rate": positive_counts[label] / max(1, rows_written),
            }
            for label in LABELS
        },
        "skipped": skipped[:50],
    }
    (output.parent / "event_dataset_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"Games: {games_used}")
    print(f"Rows: {rows_written}")
    for label in LABELS:
        print(
            f"  {label:16s} positive={positive_counts[label]:6d} "
            f"rate={positive_counts[label] / max(1, rows_written):.4f}"
        )
    print(f"Output: {output}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 10 秒网格的击杀/gank 事件数据集",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--interval", type=int, default=10, choices=[5, 10])
    args = parser.parse_args(argv)

    exports = dataset.find_exports(args.export_root)
    if not exports:
        print("[错误] 没有找到导出目录", file=sys.stderr)
        return 1

    output = args.output or (args.export_root / "event_dataset" / "event_dataset.csv")
    if output.is_dir():
        output = output / "event_dataset.csv"

    return build(exports, output, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
