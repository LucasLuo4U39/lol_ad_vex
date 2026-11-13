#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量拉取对局 SUMMARY（含 10 名玩家 puuid / 队伍 / 胜负），给 Elo 特征做底料。

输入：gameId 列表（默认取 game_champs.json 的全部 2298 局）
输出：`analysis/game_summaries.jsonl`（每行一局，可断点续跑）

用法：
    python fetch_game_summaries.py --limit 20 --workers 4
    python fetch_game_summaries.py                # 全量
    python fetch_game_summaries.py --server TENCENT_TJ100   # 手动指定 SGP shard

说明：
- 复用公共客户端模块（lcu_client / sgp_client），只依赖标准库；
- LCU 需在线（entitlements token）；SGP shard 默认从 LCU 进程命令行推导
  （region + rso-platform-id），可用 --server 覆盖，不再硬编码 NJ100；
- 单局 SUMMARY 约 120 KB，2,298 局约 280 MB，按 6 并发约 3–5 分钟；
- 结果按 gameId 去重，重跑会跳过已抓的局。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from lcu_client import LcuAuth, LcuClient, LcuError, discover_lcu_auth
from sgp_client import (
    SGP_SERVERS,
    SgpClient,
    SgpError,
    get_sgp_server_id,
    sgp_sub_id,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DEFAULT_GAMES = BASE / "game_champs.json"
DEFAULT_OUT = BASE.parent / "analysis" / "game_summaries.jsonl"

_lock = threading.Lock()


def load_done(path: Path) -> set:
    done = set()
    if path.is_file():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("gameId"):
                    done.add(str(record["gameId"]))
    return done


def summarize(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 SGP SUMMARY 压成精简记录（只留 Elo 需要的字段）。"""
    inner = (raw or {}).get("json") or {}
    game_id = inner.get("gameId")
    participants = inner.get("participants") or []
    if not game_id or len(participants) < 10:
        return None
    players = []
    for participant in participants:
        puuid = participant.get("puuid")
        team = participant.get("teamId")
        if not puuid or team not in (100, 200):
            return None
        players.append(
            {
                "puuid": puuid,
                "team": int(team),
                "win": bool(participant.get("win")),
                "champion": participant.get("championName"),
                "position": participant.get("teamPosition") or participant.get("individualPosition"),
            }
        )
    return {
        "gameId": str(game_id),
        "creation": int(inner.get("gameCreation") or 0),
        "duration": int(inner.get("gameDuration") or 0),
        "queueId": int(inner.get("queueId") or 0),
        "players": players,
    }


def fetch_one(client: SgpClient, sub_id: str, game_id: str) -> Optional[Dict[str, Any]]:
    try:
        raw = client.game_summary(game_id, sub_id)
    except (SgpError, TimeoutError) as error:
        print(f"  ! {game_id} {error}")
        return None
    if not raw:
        return None
    return summarize(raw)


def resolve_auth(lcu_port: Optional[int], lcu_token: Optional[str]) -> LcuAuth:
    """--lcu-port/--lcu-token 优先；否则三路探测（psutil→PowerShell→wmic）。"""
    if lcu_port and lcu_token:
        return LcuAuth(port=lcu_port, token=lcu_token)
    if lcu_port or lcu_token:
        raise LcuError("--lcu-port 与 --lcu-token 必须同时提供")
    return discover_lcu_auth()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="批量拉取对局 SUMMARY")
    parser.add_argument("--games", type=Path, default=DEFAULT_GAMES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--lcu-port", type=int, default=None)
    parser.add_argument("--lcu-token", default=None)
    parser.add_argument("--server", default=None, help="手动指定 SGP 服务器 ID，例如 TENCENT_TJ100 / NA1")
    args = parser.parse_args(argv)

    try:
        auth = resolve_auth(args.lcu_port, args.lcu_token)
    except LcuError as error:
        print(f"找不到 LCU 端口/token：{error}", file=sys.stderr)
        return 1

    lcu = LcuClient(auth)
    try:
        entitlements = lcu.get_json("/entitlements/v1/token")
    except (LcuError, TimeoutError) as error:
        print(f"读取 entitlements token 失败: {error}", file=sys.stderr)
        return 1
    access = (entitlements or {}).get("accessToken") or ""
    if not access:
        print("entitlements accessToken 为空（客户端没登录？）", file=sys.stderr)
        return 1

    server_id = args.server or get_sgp_server_id(auth.region, auth.rso_platform_id)
    server = SGP_SERVERS.get(server_id)
    if not server:
        supported = ", ".join(sorted(SGP_SERVERS))
        print(
            f"不支持的 SGP 服务器 {server_id}（--server 可手动指定）。可用: {supported}",
            file=sys.stderr,
        )
        return 1
    sub_id = sgp_sub_id(server_id, server)

    client = SgpClient(server, access, retries=3)
    all_games = sorted(json.loads(args.games.read_text(encoding="utf-8")).keys())
    if args.limit:
        all_games = all_games[: args.limit]
    done = load_done(args.out)
    todo = [g for g in all_games if g not in done]
    print(f"目标 {len(all_games)} 局，已完成 {len(done)}，本次 {len(todo)} 局，{args.workers} 并发（SGP={server_id}）")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    ok = fail = 0
    with args.out.open("a", encoding="utf-8") as sink, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, client, sub_id, game_id): game_id for game_id in todo}
        for index, future in enumerate(as_completed(futures), start=1):
            game_id = futures[future]
            try:
                record = future.result()
            except Exception as error:  # noqa: BLE001
                record = None
                print(f"  ! {game_id} {error}")
            if record:
                with _lock:
                    sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                    sink.flush()
                ok += 1
            else:
                fail += 1
                print(f"  × {game_id} 失败")
            if index % 100 == 0:
                elapsed = time.time() - started
                rate = index / max(elapsed, 1e-9)
                print(f"  {index}/{len(todo)}  ok={ok} fail={fail}  {rate:.1f}/s  剩余约 {(len(todo) - index) / max(rate, 1e-9) / 60:.1f} 分钟")
    print(f"完成：ok={ok} fail={fail} 用时 {(time.time() - started) / 60:.1f} 分钟 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
