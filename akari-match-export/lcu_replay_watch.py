#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LCU 助手：查询回放目录、触发"观看回放"。

只使用客户端自己的本地 LCU REST API（与客户端 UI 点"观看回放"同一接口），
不接触游戏进程、不读内存。

复用公共客户端模块 lcu_client；--port/--token 缺省时自动三路探测
（psutil→PowerShell→wmic）。

用法：
    python lcu_replay_watch.py --game-id 901127125498 --show-path
    python lcu_replay_watch.py --game-id 901127125498 --watch
    python lcu_replay_watch.py --info
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from lcu_client import LcuAuth, LcuClient, LcuError, discover_lcu_auth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def resolve_auth(port: Optional[int], token: Optional[str]) -> LcuAuth:
    if port and token:
        return LcuAuth(port=port, token=token)
    if port or token:
        raise LcuError("--port 与 --token 必须同时提供")
    return discover_lcu_auth()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LCU 回放助手")
    parser.add_argument("--port", type=int, default=None, help="LCU 端口（缺省自动探测）")
    parser.add_argument("--token", default=None, help="LCU remoting token（缺省自动探测）")
    parser.add_argument("--game-id", required=True, help="对局 ID（必填）")
    parser.add_argument("--show-path", action="store_true", help="打印回放目录")
    parser.add_argument("--watch", action="store_true", help="POST 观看回放")
    parser.add_argument("--info", action="store_true", help="查询当前登录账号")
    args = parser.parse_args()

    try:
        auth = resolve_auth(args.port, args.token)
    except LcuError as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1

    client = LcuClient(auth)

    if args.info:
        try:
            summoner = client.get_json("/lol-summoner/v1/current-summoner") or {}
            print(json.dumps(summoner, ensure_ascii=False)[:300])
        except (LcuError, TimeoutError) as error:
            print(f"[错误] summoner 查询失败: {error}", file=sys.stderr)
            return 1

    if args.show_path:
        try:
            print(client.get_text("/lol-replays/v1/rofls/path"))
        except (LcuError, TimeoutError) as error:
            print(f"[错误] rofls/path 查询失败: {error}", file=sys.stderr)
            return 1

    if args.watch:
        try:
            client.post_json(f"/lol-replays/v1/rofls/{args.game_id}/watch", {})
            print(f"已触发观看回放: gameId={args.game_id}")
        except (LcuError, TimeoutError) as error:
            print(f"[错误] 触发观看回放失败: {error}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
