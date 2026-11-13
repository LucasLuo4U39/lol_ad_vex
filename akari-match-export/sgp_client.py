#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共 SGP 客户端模块（标准库实现）。

SGP = 客户端后端对局历史服务（match-history-query）。本模块统一：

  * SGP 服务器表（源自 League Akari builtin.ts；腾讯 shard 端口 21019，
    国际服 443）
  * region / sub_id 推导（Tencent 前缀 + 国际服 region 别名）
  * Bearer 鉴权 + 超时 + 连接级重试 + 统一错误类型（SgpError）

标准库实现（http.client）；线程安全（每次请求独立连接）。
"""

from __future__ import annotations

import http.client
import json
import ssl
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from lcu_client import USER_AGENT

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class SgpError(RuntimeError):
    """SGP 请求失败（携带用户可读信息）。"""


@dataclass(frozen=True)
class SgpServer:
    match_history: str
    common: str
    is_tencent: bool
    region_path_param: Optional[str] = None
    port: int = 443


# Copied from League Akari's built-in SGP league-server config
# (src/main/shards/akari-api/builtin.ts). Tencent shards listen on 21019,
# Riot shards on 443.
SGP_SERVERS: Dict[str, SgpServer] = {
    "TENCENT_HN1": SgpServer(
        "https://hn1-k8s-sgp.lol.qq.com",
        "https://hn1-k8s-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_HN10": SgpServer(
        "https://hn10-k8s-sgp.lol.qq.com",
        "https://hn10-k8s-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_TJ100": SgpServer(
        "https://tj100-sgp.lol.qq.com",
        "https://tj100-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_TJ101": SgpServer(
        "https://tj101-sgp.lol.qq.com",
        "https://tj101-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_NJ100": SgpServer(
        "https://nj100-sgp.lol.qq.com",
        "https://nj100-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_GZ100": SgpServer(
        "https://gz100-sgp.lol.qq.com",
        "https://gz100-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_CQ100": SgpServer(
        "https://cq100-sgp.lol.qq.com",
        "https://cq100-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_BGP2": SgpServer(
        "https://bgp2-k8s-sgp.lol.qq.com",
        "https://bgp2-k8s-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_PBE": SgpServer(
        "https://pbe-sgp.lol.qq.com",
        "https://pbe-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TENCENT_PREPBE": SgpServer(
        "https://prepbe-sgp.lol.qq.com",
        "https://prepbe-sgp.lol.qq.com",
        True,
        port=21019,
    ),
    "TW2": SgpServer(
        "https://apse1-red.pp.sgp.pvp.net",
        "https://tw2-red.lol.sgp.pvp.net",
        False,
    ),
    "SG2": SgpServer(
        "https://apse1-red.pp.sgp.pvp.net",
        "https://sg2-red.lol.sgp.pvp.net",
        False,
    ),
    "PH2": SgpServer(
        "https://apse1-red.pp.sgp.pvp.net",
        "https://ph2-red.lol.sgp.pvp.net",
        False,
    ),
    "VN2": SgpServer(
        "https://apse1-red.pp.sgp.pvp.net",
        "https://vn2-red.lol.sgp.pvp.net",
        False,
    ),
    "PBE": SgpServer(
        "https://usw2-red.pp.sgp.pvp.net",
        "https://pbe-red.lol.sgp.pvp.net",
        False,
        "PBE1",
    ),
    "EUW": SgpServer(
        "https://euc1-red.pp.sgp.pvp.net",
        "https://euw-red.lol.sgp.pvp.net",
        False,
        "EUW1",
    ),
    "JP": SgpServer(
        "https://apne1-red.pp.sgp.pvp.net",
        "https://jp-red.lol.sgp.pvp.net",
        False,
        "JP1",
    ),
    "RU": SgpServer(
        "https://euc1-red.pp.sgp.pvp.net",
        "https://ru-red.lol.sgp.pvp.net",
        False,
    ),
    "BR1": SgpServer(
        "https://usw2-red.pp.sgp.pvp.net",
        "https://br-red.lol.sgp.pvp.net",
        False,
    ),
    "OC1": SgpServer(
        "https://apse1-red.pp.sgp.pvp.net",
        "https://oce-red.lol.sgp.pvp.net",
        False,
    ),
    "TR1": SgpServer(
        "https://euc1-red.pp.sgp.pvp.net",
        "https://tr-red.lol.sgp.pvp.net",
        False,
    ),
    "LA1": SgpServer(
        "https://usw2-red.pp.sgp.pvp.net",
        "https://lan-red.lol.sgp.pvp.net",
        False,
    ),
    "LA2": SgpServer(
        "https://usw2-red.pp.sgp.pvp.net",
        "https://las-red.lol.sgp.pvp.net",
        False,
    ),
    "NA1": SgpServer(
        "https://usw2-red.pp.sgp.pvp.net",
        "https://na-red.lol.sgp.pvp.net",
        False,
    ),
    "TH2": SgpServer(
        "https://apse1-red.pp.sgp.pvp.net",
        "https://th2-red.lol.sgp.pvp.net",
        False,
    ),
    "KR": SgpServer(
        "https://apne1-red.pp.sgp.pvp.net",
        "https://kr-red.lol.sgp.pvp.net",
        False,
    ),
}

REGION_ALIASES = {
    "NA": "NA1",
    "BR": "BR1",
    "TR": "TR1",
    "LAN": "LA1",
    "LAS": "LA2",
    "OCE": "OC1",
    "EUW1": "EUW",
    "JP1": "JP",
}


def get_sgp_server_id(region: str, rso_platform_id: str) -> str:
    region_upper = (region or "").upper()
    rso_upper = (rso_platform_id or "").upper()

    if not region_upper:
        return ""

    if region_upper == "TENCENT":
        return f"TENCENT_{rso_upper}".upper()

    return REGION_ALIASES.get(region_upper, region_upper).upper()


def sgp_sub_id(sgp_server_id: str, server: SgpServer) -> str:
    if server.region_path_param:
        return server.region_path_param
    if sgp_server_id.startswith("TENCENT_"):
        return sgp_server_id.split("_", 1)[1]
    return sgp_server_id


def server_from_host(host: str, port: int = 21019, sub_id: Optional[str] = None) -> SgpServer:
    """从裸 host（如 nj100-sgp.lol.qq.com）构造 SgpServer，便于旧参数兼容。"""
    host = (host or "").strip().rstrip("/")
    if "://" in host:
        host = host.split("://", 1)[1]
    return SgpServer(
        match_history=f"https://{host}",
        common=f"https://{host}",
        is_tencent="lol.qq.com" in host,
        region_path_param=sub_id,
        port=port,
    )


class SgpClient:
    """标准库 SGP 客户端：Bearer 鉴权 + 超时 + 连接级重试 + 统一错误。

    线程安全：每次请求独立连接，无共享可变状态。
    """

    def __init__(
        self,
        server: SgpServer,
        access_token: str,
        insecure: bool = False,
        timeout: float = 30.0,
        retries: int = 0,
    ):
        host = (server.match_history or "").strip().rstrip("/")
        if "://" in host:
            host = host.split("://", 1)[1]
        self._host = host
        self._port = server.port
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": USER_AGENT,
        }
        self._context = (
            ssl._create_unverified_context()
            if insecure
            else ssl.create_default_context()
        )
        self.timeout = timeout
        self.retries = max(0, retries)

    def _get(self, path: str) -> Any:
        attempts = self.retries + 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            conn: Optional[http.client.HTTPSConnection] = None
            try:
                conn = http.client.HTTPSConnection(
                    self._host,
                    self._port,
                    timeout=self.timeout,
                    context=self._context,
                )
                conn.request("GET", path, headers=self._headers)
                response = conn.getresponse()
                data = response.read()

                if response.status >= 400:
                    text = data.decode("utf-8", errors="replace")[:300].replace(
                        "\n", " "
                    )
                    raise SgpError(f"SGP GET {path} -> {response.status}: {text}")

                return json.loads(data.decode("utf-8", errors="replace"))
            except TimeoutError as error:
                last_error = TimeoutError(f"SGP GET {path} 请求超时: {error}")
            except (OSError, ssl.SSLError) as error:
                last_error = SgpError(f"SGP 连接失败 GET {path}: {error}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))

        assert last_error is not None
        raise last_error

    def summary(self, puuid: str, count: int, start_index: int = 0) -> Dict[str, Any]:
        path = (
            f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY"
            f"?startIndex={start_index}&count={count}"
        )
        return self._get(path)

    def details(self, game_id: Any, sub_id: str) -> Dict[str, Any]:
        path = f"/match-history-query/v1/products/lol/{sub_id}_{game_id}/DETAILS"
        return self._get(path)

    def game_summary(self, game_id: Any, sub_id: str) -> Dict[str, Any]:
        path = f"/match-history-query/v1/products/lol/{sub_id}_{game_id}/SUMMARY"
        return self._get(path)
