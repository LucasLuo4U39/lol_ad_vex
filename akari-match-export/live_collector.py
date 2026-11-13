#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poll the League Live Client Data API and save snapshots.

The endpoint is local-only:
  https://127.0.0.1:2999/liveclientdata/allgamedata

Run this while a game is in progress. Snapshots are JSONL, one per poll:

  live_data/session_<timestamp>/snapshots.jsonl
  live_data/session_<timestamp>/meta.json
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

ENDPOINT = "https://127.0.0.1:2999/liveclientdata/allgamedata"


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()


def fetch_snapshot() -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(ENDPOINT, verify=False, timeout=3)
        if response.status_code != 200:
            return None
        payload = response.json()
        if not isinstance(payload, dict) or "gameData" not in payload:
            return None
        return payload
    except Exception:
        return None


def collector(
    output_root: Path,
    interval: float,
    wait_minutes: float,
    once: bool,
) -> int:
    deadline = time.time() + wait_minutes * 60
    session_directory: Optional[Path] = None
    handle = None
    count = 0
    last_success = 0.0

    print(f"等待对局... endpoint={ENDPOINT}")

    try:
        while time.time() < deadline or session_directory is not None:
            snapshot = fetch_snapshot()

            if snapshot is None:
                if session_directory is None:
                    if once:
                        print("当前没有进行中的对局（Live Client Data 2999 未响应）")
                        return 1
                    time.sleep(min(interval, 3.0))
                    continue

                # Game ended and the endpoint disappeared.
                if time.time() - last_success > 30:
                    print("对局结束，停止采集")
                    break
                time.sleep(interval)
                continue

            if session_directory is None:
                session_directory = output_root / f"session_{datetime.now():%Y%m%d_%H%M%S}"
                session_directory.mkdir(parents=True, exist_ok=True)
                handle = (session_directory / "snapshots.jsonl").open(
                    "w", encoding="utf-8", newline=""
                )
                (session_directory / "meta.json").write_text(
                    json.dumps(
                        {
                            "startedAt": utc_now(),
                            "endpoint": ENDPOINT,
                            "gameData": snapshot.get("gameData"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"开始采集: {session_directory}")

            record = {
                "collectedAt": utc_now(),
                "gameTime": float((snapshot.get("gameData") or {}).get("gameTime") or 0),
                "data": snapshot,
            }
            assert handle is not None
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            count += 1
            last_success = time.time()

            if count % 12 == 0:
                game_time = record["gameTime"]
                print(
                    f"  snapshots={count} gameTime={int(game_time // 60):02d}:"
                    f"{int(game_time % 60):02d}"
                )

            if once:
                break

            time.sleep(interval)
    finally:
        if handle:
            handle.close()

    print(f"采集完成: {count} 个快照")
    if session_directory:
        print(f"输出: {session_directory}")

    return 0 if count else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="采集 Live Client Data 快照",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--output", type=Path, default=Path.cwd() / "live_data"
    )
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔（秒）")
    parser.add_argument("--wait", type=float, default=60.0, help="等待对局开始的分钟数")
    parser.add_argument("--once", action="store_true", help="只抓一次，用于测通")
    args = parser.parse_args(argv)

    return collector(args.output, args.interval, args.wait, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
