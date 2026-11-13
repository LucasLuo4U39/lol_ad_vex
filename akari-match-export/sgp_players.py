#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询指定对局的 SGP DETAILS 并列出玩家分路。

复用公共客户端模块（lcu_client / sgp_client）；LCU 认证默认三路自动探测
（psutil→PowerShell→wmic），也可用 --lcu-port/--lcu-token 手动指定。
SGP shard 用 --sgp-server 指定（默认 TENCENT_NJ100，端口从服务器表取）。

用法：
    python sgp_players.py --game-id 901128442938
    python sgp_players.py --game-id 901128442938 --sgp-server TENCENT_TJ100
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional

from lcu_client import LcuAuth, LcuClient, LcuError, discover_lcu_auth
from sgp_client import (
    SGP_SERVERS,
    SgpClient,
    SgpError,
    SgpServer,
    server_from_host,
    sgp_sub_id,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def resolve_server(
    server_id: Optional[str], host: Optional[str], port: Optional[int]
) -> SgpServer:
    """--sgp-host 优先（旧参数兼容），否则查服务器表。"""
    if host:
        return server_from_host(host, port or 21019, None)
    if not server_id or server_id not in SGP_SERVERS:
        supported = ", ".join(sorted(SGP_SERVERS))
        raise SgpError(
            f"未知 SGP 服务器 {server_id or '?'}。可用: {supported}"
        )
    return SGP_SERVERS[server_id]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None, help="LCU 端口（缺省自动探测）")
    parser.add_argument("--token", default=None, help="LCU remoting token（缺省自动探测）")
    parser.add_argument("--game-id", required=True, help="对局 ID（必填）")
    parser.add_argument("--sgp-server", default="TENCENT_NJ100", help="SGP 服务器 ID")
    parser.add_argument("--sgp-host", default=None, help="SGP host 覆盖（旧参数兼容）")
    parser.add_argument("--sgp-port", type=int, default=None, help="SGP 端口覆盖")
    parser.add_argument("--sub-id", default=None, help="DETAILS 路径 sub_id 覆盖")
    args = parser.parse_args(argv)

    try:
        if args.port and args.token:
            auth = LcuAuth(port=args.port, token=args.token)
        elif args.port or args.token:
            print("--port 与 --token 必须同时提供", file=sys.stderr)
            return 1
        else:
            auth = discover_lcu_auth()
    except LcuError as error:
        print(f"找不到 LCU：{error}", file=sys.stderr)
        return 1

    lcu = LcuClient(auth)

    try:
        summoner = lcu.get_json("/lol-summoner/v1/current-summoner") or {}
    except (LcuError, TimeoutError) as error:
        print(f"summoner 失败: {error}", file=sys.stderr)
        return 1
    puuid = summoner.get("puuid")
    print(f"账号: {summoner.get('gameName')}#{summoner.get('tagLine')} puuid={puuid}")

    try:
        entitlements = lcu.get_json("/entitlements/v1/token") or {}
    except (LcuError, TimeoutError) as error:
        print(f"entitlements 失败: {error}", file=sys.stderr)
        return 1
    access = entitlements.get("accessToken", "")
    print(f"entitlements token 长度: {len(access)}")
    if not access:
        print("entitlements accessToken 为空（客户端没登录？）", file=sys.stderr)
        return 1

    try:
        server = resolve_server(args.sgp_server, args.sgp_host, args.sgp_port)
        sub_id = args.sub_id or sgp_sub_id(args.sgp_server, server)
        sgp = SgpClient(server, access)
        details = sgp.details(args.game_id, sub_id)
    except (SgpError, TimeoutError) as error:
        print(f"SGP 失败: {error}", file=sys.stderr)
        return 1

    print(f"\ngameId={details.get('gameId')} 时长={details.get('gameDuration')}s")
    print(f"{'分路':10s} {'队伍':6s} {'名字':26s} {'英雄'}")
    for p in details.get("participants") or []:
        name = p.get("riotId") or p.get("summonerName") or p.get("gameName") or "?"
        team = p.get("teamId")
        print(
            f"{str(p.get('teamPosition') or '?'):10s} {str(team):6s} "
            f"{str(name):26s} {p.get('championName') or p.get('championId')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
