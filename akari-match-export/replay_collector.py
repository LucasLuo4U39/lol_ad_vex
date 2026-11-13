#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect 1-second snapshots from a running replay/game via Live Client Data.

Works both in a live game and in a replay. In replay/spectator mode
`activePlayer` is unavailable, but `allPlayers` still exposes champion, team,
level, KDA, CS, items, ward score, death/respawn state.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import urllib3

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://127.0.0.1:2999/liveclientdata"


def get_json(path: str) -> Optional[Any]:
    try:
        response = requests.get(BASE + path, verify=False, timeout=3)
        if response.status_code != 200:
            return None
        return response.json()
    except Exception:
        return None


def normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    players = []
    for player in payload.get("allPlayers") or []:
        items = []
        for item in player.get("items") or []:
            items.append(
                {
                    "itemID": item.get("itemID"),
                    "count": item.get("count"),
                    "price": item.get("price"),
                    "displayName": item.get("displayName"),
                }
            )

        scores = player.get("scores") or {}
        players.append(
            {
                "riotId": player.get("riotId") or player.get("summonerName"),
                "champion": player.get("championName"),
                "rawChampionName": player.get("rawChampionName"),
                "team": player.get("team"),
                "position": player.get("position"),
                "level": player.get("level"),
                "isDead": player.get("isDead"),
                "respawnTimer": player.get("respawnTimer"),
                "scores": {
                    "kills": scores.get("kills"),
                    "deaths": scores.get("deaths"),
                    "assists": scores.get("assists"),
                    "creepScore": scores.get("creepScore"),
                    "wardScore": scores.get("wardScore"),
                },
                "items": items,
            }
        )

    return {"players": players}


def collect(output: Path, interval: float, duration: float) -> int:
    session = output / f"session_{datetime.now():%Y%m%d_%H%M%S}"
    session.mkdir(parents=True, exist_ok=True)
    path = session / "snapshots.jsonl"
    started = time.time()
    count = 0

    print(f"采集中: {session}  间隔={interval}s  时长={duration}s")

    with path.open("w", encoding="utf-8") as handle:
        while time.time() - started < duration:
            game = get_json("/gamestats")
            all_data = get_json("/allgamedata")
            events = get_json("/eventdata")

            if not game or not all_data:
                print("2999 无响应，等待 replay/game...")
                time.sleep(1)
                continue

            record = {
                "collectedAt": datetime.now().astimezone().isoformat(),
                "gameTime": game.get("gameTime"),
                "gameMode": game.get("gameMode"),
                "mapName": game.get("mapName"),
                "data": normalize(all_data),
                "events": (events or {}).get("Events") or [],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            count += 1

            if count % 10 == 0:
                print(
                    f"  {count} 个快照  gameTime={float(record['gameTime'] or 0):.1f}s"
                )

            time.sleep(interval)

    meta = {
        "startedAt": started,
        "duration": duration,
        "interval": interval,
        "snapshots": count,
    }
    (session / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"完成: {count} 个快照 -> {session}")
    return 0 if count else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="从 replay/game 的 Live Client Data 采集 1 秒快照",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output", type=Path, default=Path.cwd() / "replay_live_data")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args(argv)

    return collect(args.output, args.interval, args.duration)


if __name__ == "__main__":
    raise SystemExit(main())
