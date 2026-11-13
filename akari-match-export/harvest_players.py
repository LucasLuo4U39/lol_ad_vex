#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harvest recent ranked-solo games for the top Challenger players.

For each player:
  * resolve PUUID from the LCU apex ladder
  * fetch recent queue-420 matches from SGP
  * keep the newest N Summoner's Rift games
  * fetch DETAILS and build a timeline export

Output is compatible with `build_dataset.py`:
  <output>/game_<gameId>/{summary.json, details.jsonl, timeline/...}
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import build_timeline
import match_export as exporter

APEX_PATH = "/lol-ranked/v1/apex-leagues/RANKED_SOLO_5x5/CHALLENGER"
GRANDMASTER_PATH = "/lol-ranked/v1/apex-leagues/RANKED_SOLO_5x5/GRANDMASTER"
QUEUE_SOLO = 420
T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def with_retry(operation: Callable[[], T], attempts: int = 3) -> T:
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            last_error = error
            time.sleep(1 + attempt)

    assert last_error is not None
    raise last_error


def get_ladder(
    lcu: exporter.LcuClient,
    top_n: int,
    include_grandmaster: bool = False,
) -> List[Dict[str, Any]]:
    tiers = [("CHALLENGER", APEX_PATH)]
    if include_grandmaster:
        tiers.append(("GRANDMASTER", GRANDMASTER_PATH))

    standings: List[Dict[str, Any]] = []

    for tier, path in tiers:
        data = lcu.get_json(path)
        for division in data.get("divisions") or []:
            for standing in division.get("standings") or []:
                standing["_tier"] = tier
                standings.append(standing)

    # Challenger always ranks above Grandmaster; inside a tier, higher LP first.
    standings.sort(
        key=lambda item: (
            0 if item.get("_tier") == "CHALLENGER" else 1,
            -(item.get("leaguePoints") or 0),
        )
    )

    players: List[Dict[str, Any]] = []

    for index, standing in enumerate(standings[:top_n], start=1):
        puuid = standing.get("puuid")
        if not puuid:
            continue

        game_name = ""
        tag_line = ""

        try:
            info = lcu.get_json(f"/lol-summoner/v2/summoners/puuid/{puuid}")
            game_name = info.get("gameName") or info.get("displayName") or ""
            tag_line = info.get("tagLine") or ""
        except Exception:
            pass

        players.append(
            {
                "position": index,
                "tier": standing.get("_tier"),
                "leaguePoints": standing.get("leaguePoints"),
                "wins": standing.get("wins"),
                "losses": standing.get("losses"),
                "puuid": puuid,
                "gameName": game_name or standing.get("summonerName") or "",
                "tagLine": tag_line,
            }
        )

    return players


def is_ranked_sr(game: Dict[str, Any]) -> bool:
    return (
        game.get("queueId") == QUEUE_SOLO
        and game.get("gameMode") == "CLASSIC"
        and game.get("mapId") == 11
        and int(game.get("gameDuration") or 0) >= 600
        and game.get("endOfGameResult") in (None, "GameComplete")
    )


def fetch_player_games(
    sgp: exporter.SgpClient,
    puuid: str,
    want: int,
) -> List[Dict[str, Any]]:
    summary = with_retry(
        lambda: sgp.summary(puuid, min(100, max(want * 4, 20)))
    )
    games: List[Dict[str, Any]] = []

    for item in summary.get("games") or []:
        game = item.get("json")
        if not isinstance(game, dict):
            continue
        if not is_ranked_sr(game):
            continue
        games.append(game)
        if len(games) >= want:
            break

    return games


def harvest(
    output: Path,
    top_n: int,
    games_per_player: int,
    puuids: Optional[List[str]] = None,
    include_grandmaster: bool = False,
    workers: int = 6,
) -> int:
    auth = exporter.discover_lcu_auth()
    lcu = exporter.LcuClient(auth)
    entitlements = lcu.get_json("/entitlements/v1/token")
    access_token = entitlements.get("accessToken")

    if not access_token:
        print("[错误] entitlements token 为空", file=sys.stderr)
        return 1

    sgp_server_id = exporter.get_sgp_server_id(auth.region, auth.rso_platform_id)
    server = exporter.SGP_SERVERS.get(sgp_server_id)

    if not server:
        print(f"[错误] 不支持的 SGP 服务器: {sgp_server_id}", file=sys.stderr)
        return 1

    sgp = exporter.SgpClient(server, access_token)
    sub_id = exporter.sgp_sub_id(sgp_server_id, server)

    if puuids:
        players = [
            {
                "position": index + 1,
                "puuid": puuid,
                "gameName": "",
                "tagLine": "",
            }
            for index, puuid in enumerate(puuids)
        ]
    else:
        tiers = "Challenger + Grandmaster" if include_grandmaster else "Challenger"
        print(f"读取 {tiers} 前 {top_n} ...")
        players = get_ladder(lcu, top_n, include_grandmaster)

    if not players:
        print("[错误] 没有拿到排行榜数据", file=sys.stderr)
        return 1

    print(f"目标玩家: {len(players)}")
    for player in players:
        print(
            f"  #{player.get('position')} [{player.get('tier') or '?'}] "
            f"{player.get('gameName')}#{player.get('tagLine')} "
            f"LP={player.get('leaguePoints')} "
            f"{player.get('wins')}W/{player.get('losses')}L"
        )

    selected: Dict[int, Dict[str, Any]] = {}
    errors: List[Dict[str, Any]] = []

    for player in players:
        puuid = player["puuid"]
        try:
            games = fetch_player_games(sgp, puuid, games_per_player)
        except Exception as error:
            errors.append({"stage": "summary", "puuid": puuid, "error": str(error)})
            print(f"  [summary] {player.get('gameName')} 失败: {error}")
            continue

        print(f"  {player.get('gameName')}#{player.get('tagLine')}: {len(games)} 场")

        for game in games:
            game_id = game.get("gameId")
            if isinstance(game_id, int) and game_id not in selected:
                selected[game_id] = {"game": game, "owner": player}

    print(f"唯一对局: {len(selected)}")

    output.mkdir(parents=True, exist_ok=True)
    exported = 0
    pending = []

    for game_id, entry in sorted(selected.items()):
        game = entry["game"]
        owner = entry["owner"]
        folder = output / f"game_{game_id}"

        if (folder / "timeline" / "timeline.json").is_file():
            exported += 1
            continue

        folder.mkdir(parents=True, exist_ok=True)
        summary_path = folder / "summary.json"
        details_path = folder / "details.jsonl"

        summary = {
            "puuid": owner.get("puuid"),
            "gameName": owner.get("gameName"),
            "tagLine": owner.get("tagLine"),
            "exportedAt": utc_now(),
            "games": [{"json": game}],
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        pending.append(
            {
                "gameId": game_id,
                "game": game,
                "owner": owner,
                "folder": folder,
                "summaryPath": summary_path,
                "detailsPath": details_path,
            }
        )

    print(f"已完成: {exported}，待处理: {len(pending)}，并发: {workers}")

    def fetch_details(game_id: int) -> Any:
        return with_retry(lambda: sgp.details(game_id, sub_id))

    completed = 0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(fetch_details, item["gameId"]): item for item in pending
        }

        for future in as_completed(futures):
            item = futures[future]
            game_id = item["gameId"]
            game = item["game"]
            owner = item["owner"]
            folder = item["folder"]
            summary_path = item["summaryPath"]
            details_path = item["detailsPath"]
            completed += 1

            try:
                details = future.result()
            except Exception as error:
                errors.append(
                    {"stage": "details", "gameId": game_id, "error": str(error)}
                )
                print(f"  [details] {game_id} 失败: {error}")
                continue

            details_path.write_text(
                json.dumps(
                    {"gameId": game_id, "details": details}, ensure_ascii=False
                )
                + "\n",
                encoding="utf-8",
            )

            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    build_timeline.build(
                        summary_path,
                        details_path,
                        folder / "timeline",
                        game_id,
                        False,
                    )
            except Exception as error:
                errors.append(
                    {"stage": "timeline", "gameId": game_id, "error": str(error)}
                )
                print(f"  [timeline] {game_id} 失败: {error}")
                continue

            (folder / "owner.json").write_text(
                json.dumps(
                    {
                        "owner": owner,
                        "gameId": game_id,
                        "queueId": game.get("queueId"),
                        "gameVersion": game.get("gameVersion"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            exported += 1

            if completed % 25 == 0 or completed == len(pending):
                print(
                    f"  [{completed}/{len(pending)}] game {game_id} OK "
                    f"(exported {exported})"
                )

    manifest = {
        "createdAt": utc_now(),
        "sgpServerId": sgp_server_id,
        "players": players,
        "requestedGamesPerPlayer": games_per_player,
        "uniqueGames": len(selected),
        "exported": exported,
        "workers": workers,
        "errors": errors,
    }
    (output / "harvest_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"导出完成: {exported}/{len(selected)}")
    print(f"输出: {output}")

    return 0 if exported > 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="采集 Challenger 前 N 名玩家的最近排位对局",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--top", type=int, default=10, help="前 N 名")
    parser.add_argument(
        "--games",
        type=int,
        default=10,
        help="每人采集多少场 420",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "top-exports",
        help="输出根目录",
    )
    parser.add_argument(
        "--puuids",
        default=None,
        help="可选：逗号分隔的 PUUID 列表，直接指定玩家",
    )
    parser.add_argument(
        "--include-grandmaster",
        action="store_true",
        help="Challenger 不足 N 人时，用 Grandmaster 按 LP 补齐",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="并发抓取详情数",
    )
    args = parser.parse_args(argv)

    puuids = (
        [value.strip() for value in args.puuids.split(",") if value.strip()]
        if args.puuids
        else None
    )

    return harvest(
        args.output,
        args.top,
        args.games,
        puuids,
        args.include_grandmaster,
        args.workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
