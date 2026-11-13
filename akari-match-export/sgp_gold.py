#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SGP 金币桥：把回放里缺失的"口袋金币"(currentGold) 从 SGP 1 分钟帧补回来。

回放的 Live Client Data 没有金币，之前的方案是用装备总价估算——那是"已经
花掉的钱"，不是兜里可支配的现金。SGP DETAILS 的 frames[].participantFrames
每 60 秒一帧，含真实的 currentGold / totalGold。本模块：

1. 用 SUMMARY 建立 名字 -> participantId 映射；
2. 用 DETAILS 帧建立 participantId -> 按时间的 currentGold 序列；
3. pocket_gold(name, game_time) 用二分查找返回该时刻最近一帧的口袋金币。

复用公共客户端模块（lcu_client / sgp_client），失败时抛异常由调用方回退
到装备估算；pocket_gold 查不到时返回 None。
"""

from __future__ import annotations

import bisect
import sys
from typing import Dict, List, Optional, Tuple

from lcu_client import LcuAuth, LcuClient
from sgp_client import SgpClient, SgpServer, server_from_host

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class SgpGoldBridge:
    """name -> currentGold(t) 查询，数据一次性拉取后本地查询。"""

    def __init__(
        self,
        lcu_port: int,
        lcu_token: str,
        game_id: str,
        sgp_host: str = "nj100-sgp.lol.qq.com",
        sub_id: str = "NJ100",
        sgp_port: int = 21019,
        insecure: bool = False,
    ):
        self.game_id = game_id
        self.name_to_pid: Dict[str, int] = {}
        self.frames: List[Tuple[int, Dict[int, float]]] = []  # (timestamp_ms, pid->currentGold)
        self.frame_times: List[int] = []

        server: SgpServer = server_from_host(sgp_host, sgp_port, None)
        lcu = LcuClient(LcuAuth(port=lcu_port, token=lcu_token))

        entitlements = lcu.get_json("/entitlements/v1/token")
        if not entitlements or not entitlements.get("accessToken"):
            raise RuntimeError("无法获取 entitlements token")
        access = entitlements["accessToken"]

        summoner = lcu.get_json("/lol-summoner/v1/current-summoner") or {}
        puuid = summoner.get("puuid") or ""

        sgp = SgpClient(server, access, insecure=insecure)
        summary = sgp.summary(puuid, 30)
        if not summary:
            raise RuntimeError("SUMMARY 拉取失败")

        target = None
        for game in summary.get("games") or []:
            if str((game.get("json") or {}).get("gameId")) == str(game_id):
                target = game
                break
        if target is None:
            raise RuntimeError(f"SUMMARY 中没有对局 {game_id}")

        for p in (target.get("json") or {}).get("participants") or []:
            pid = p.get("participantId")
            if pid is None:
                continue
            game_name = str(p.get("riotIdGameName") or p.get("summonerName") or "")
            tag = str(p.get("riotIdTagline") or "")
            full = f"{game_name}#{tag}" if tag else game_name
            self.name_to_pid[full] = int(pid)
            if game_name:
                self.name_to_pid.setdefault(game_name, int(pid))

        details = sgp.details(game_id, sub_id)
        if not details:
            raise RuntimeError("DETAILS 拉取失败")

        for frame in (details.get("json") or {}).get("frames") or []:
            ts = int(frame.get("timestamp") or 0)
            golds: Dict[int, float] = {}
            for pid_str, pf in (frame.get("participantFrames") or {}).items():
                if "currentGold" in pf:
                    golds[int(pid_str)] = float(pf["currentGold"])
            self.frames.append((ts, golds))
        self.frames.sort(key=lambda item: item[0])
        self.frame_times = [ts for ts, _ in self.frames]

        if not self.name_to_pid or not self.frames:
            raise RuntimeError("SUMMARY/DETAILS 数据不完整")

    def pocket_gold(self, player_name: str, game_time: float) -> Optional[float]:
        pid = None
        for key in (str(player_name), str(player_name).split("#")[0]):
            if key in self.name_to_pid:
                pid = self.name_to_pid[key]
                break
        if pid is None:
            # 前缀匹配兜底（riotId 大小写/空白差异）
            lowered = str(player_name).split("#")[0].lower()
            for key, value in self.name_to_pid.items():
                if key.split("#")[0].lower() == lowered:
                    pid = value
                    break
        if pid is None:
            return None

        target_ms = int(game_time * 1000)
        # 二分定位最后一帧（ts <= target_ms + 1s），向前扫描找含该 pid 的帧。
        index = bisect.bisect_right(self.frame_times, target_ms + 1000) - 1
        while index >= 0:
            ts, golds = self.frames[index]
            if pid in golds:
                return golds[pid]
            index -= 1
        return None

    def describe(self) -> str:
        return f"SgpGoldBridge(game={self.game_id}, frames={len(self.frames)})"
