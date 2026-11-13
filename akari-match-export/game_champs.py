#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 live_dataset.csv 里的每一局反查 10 人阵容（英雄/分路/队伍/胜方）。

DETAILS 只有 puuid；先取 DETAILS 的任一参与者 puuid，再查该玩家的 SUMMARY
找到这局，从 SUMMARY participants 得到 championName/teamPosition/teamId/win。

输出 game_champs.json:
{"901128442938": {"100": {"TOP": "Zaahen", ...}, "200": {...}, "winner": 100}, ...}
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_CTX = ssl._create_unverified_context()

LOCKFILE_CANDIDATES = [
    Path("E:/网络游戏/英雄联盟/LeagueClient/lockfile"),
    Path("C:/Program Files/League of Legends/lockfile"),
    Path("C:/Program Files (x86)/League of Legends/lockfile"),
    Path("C:/Riot Games/League of Legends/lockfile"),
]


def discover_lcu() -> Optional[tuple[int, str]]:
    for path in LOCKFILE_CANDIDATES:
        try:
            parts = path.read_text(encoding="utf-8").strip().split(":")
            if len(parts) >= 3 and parts[0] == "LeagueClient":
                return int(parts[1]), parts[2]
        except Exception:
            continue
    return None


class Client:
    def __init__(self, port: int, token: str, sgp_host: str = "nj100-sgp.lol.qq.com", sub_id: str = "NJ100"):
        self.port = port
        self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
        self.sgp_host = sgp_host
        self.sub_id = sub_id
        self.access_token: Optional[str] = None

    def _request(self, host: str, path: str, headers: Dict[str, str], port: int, timeout: float) -> Optional[dict]:
        try:
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_CTX)
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def lcu(self, path: str, timeout: float = 10.0) -> Optional[dict]:
        return self._request("127.0.0.1", path, {"Authorization": self.auth}, self.port, timeout)

    def ensure_token(self) -> bool:
        if self.access_token:
            return True
        entitlements = self.lcu("/entitlements/v1/token")
        if not entitlements or not entitlements.get("accessToken"):
            return False
        self.access_token = entitlements["accessToken"]
        return True

    def sgp(self, path: str, timeout: float = 25.0) -> Optional[dict]:
        return self._request(
            self.sgp_host, path,
            {"Accept": "application/json", "Authorization": f"Bearer {self.access_token}"},
            21019, timeout,
        )


def game_roster(client: Client, game_id: str) -> Optional[Dict[str, Any]]:
    details = client.sgp(
        f"/match-history-query/v1/products/lol/{client.sub_id}_{game_id}/DETAILS"
    )
    if not details:
        return None
    puuids = [
        p.get("puuid")
        for p in (details.get("json") or {}).get("participants") or []
        if p.get("puuid")
    ]
    if not puuids:
        return None

    for puuid in puuids[:5]:
        summary = client.sgp(
            f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY"
            f"?startIndex=0&count=200"
        )
        if not summary:
            continue
        for game in summary.get("games") or []:
            body = game.get("json") or {}
            if str(body.get("gameId")) != str(game_id):
                continue
            participants = body.get("participants") or []
            roster: Dict[str, Any] = {"100": {}, "200": {}}
            for p in participants:
                team = str(p.get("teamId") or "")
                role = str(p.get("teamPosition") or "")
                champion = p.get("championName") or ""
                if team in roster and role:
                    roster[team][role] = champion
                if p.get("win") is True and team:
                    roster["winner"] = int(team)
            if roster["100"] and roster["200"]:
                return roster
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path,
                        default=Path(__file__).resolve().parent.parent / "dataset" / "live_dataset" / "live_dataset.csv")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "game_champs.json")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args(argv)

    port, token = args.port, args.token
    if not (port and token):
        found = discover_lcu()
        if not found:
            print("LCU 未就绪（缺 --port/--token 且未找到 lockfile）", file=sys.stderr)
            return 1
        port, token = found

    dataframe = pd.read_csv(args.dataset, usecols=["gameId"])
    game_ids = sorted({str(g) for g in dataframe["gameId"].dropna().astype(int)})
    print(f"对局数: {len(game_ids)}")

    cache: Dict[str, Dict[str, Any]] = {}
    if args.output.is_file():
        cache = json.loads(args.output.read_text(encoding="utf-8"))
    pending = [g for g in game_ids if g not in cache or not cache[g].get("100")]
    print(f"已缓存 {len(game_ids) - len(pending)}，待查 {len(pending)}")

    lock = __import__("threading").Lock()
    failed = 0

    def work(game_id: str) -> tuple[str, Optional[Dict[str, Any]]]:
        client = Client(port, token)
        if not client.ensure_token():
            return game_id, None
        for attempt in range(3):
            roster = game_roster(client, game_id)
            if roster:
                return game_id, roster
            time.sleep(1.0 * (attempt + 1))
        return game_id, None

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for game_id, roster in pool.map(work, pending):
            done += 1
            with lock:
                if roster:
                    cache[game_id] = roster
                if done % 100 == 0:
                    args.output.write_text(
                        json.dumps(cache, ensure_ascii=False), encoding="utf-8"
                    )
                    print(f"  {done}/{len(pending)} (缓存 {len(cache)})", flush=True)
            if roster is None:
                failed += 1

    args.output.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"完成: 成功 {len(cache)}/{len(game_ids)}，失败 {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
