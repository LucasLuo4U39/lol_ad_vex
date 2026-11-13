#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共 LCU 客户端模块（标准库实现）。

供 match_export / fetch_game_summaries / fetch_player_ranks / sgp_gold /
sgp_players / lcu_replay_watch / harvest_players / build_timeline 复用，统一：

  * LCU 认证发现（三路回退：psutil -> PowerShell -> wmic）
  * Basic 鉴权（riot:<remoting-auth-token>，客户端自签证书一律不校验）
  * 超时 / 连接级重试 / 统一错误类型（LcuError）

只用标准库；psutil 可选（缺失时自动回退到 PowerShell/wmic 探测）。
"""

from __future__ import annotations

import base64
import http.client
import json
import re
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

APP_PORT_RE = re.compile(r"--app-port=([0-9]+)")
REMOTING_AUTH_RE = re.compile(r"--remoting-auth-token=([\w\-_]+)")
APP_PID_RE = re.compile(r"--app-pid=([0-9]+)")
RSO_PLATFORM_RE = re.compile(r"--rso[_-]platform[_-]id=([\w\-_]+)", re.IGNORECASE)
REGION_RE = re.compile(r"--region=([\w\-_]+)")


class LcuError(RuntimeError):
    """LCU 请求/认证失败（携带用户可读信息）。"""


@dataclass
class LcuAuth:
    port: int
    token: str
    pid: Optional[int] = None
    region: str = ""
    rso_platform_id: str = ""


def parse_command_line(command_line: str) -> Optional[LcuAuth]:
    port = APP_PORT_RE.search(command_line)
    token = REMOTING_AUTH_RE.search(command_line)

    if not port or not token:
        return None

    pid = APP_PID_RE.search(command_line)
    rso = RSO_PLATFORM_RE.search(command_line)
    region = REGION_RE.search(command_line)

    return LcuAuth(
        port=int(port.group(1)),
        token=token.group(1),
        pid=int(pid.group(1)) if pid else None,
        region=region.group(1) if region else "",
        rso_platform_id=rso.group(1) if rso else "",
    )


def _cmdlines_from_psutil() -> Iterable[str]:
    try:
        import psutil
    except ImportError:
        return

    try:
        for process in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (process.info.get("name") or "").lower()
                if name not in ("leagueclientux.exe", "leagueclient.exe"):
                    continue

                cmdline = process.info.get("cmdline") or []
                if cmdline:
                    yield " ".join(cmdline)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        return


def _cmdlines_from_powershell() -> Iterable[str]:
    script = (
        "(Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\")"
        ".CommandLine"
    )
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            timeout=20,
            text=True,
            errors="ignore",
        )
    except Exception:
        return

    if completed.returncode != 0:
        return

    for line in completed.stdout.splitlines():
        line = line.strip()
        if line:
            yield line


def _cmdlines_from_wmic() -> Iterable[str]:
    try:
        completed = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "name='LeagueClientUx.exe'",
                "get",
                "CommandLine",
                "/value",
            ],
            capture_output=True,
            timeout=20,
            text=True,
            errors="ignore",
        )
    except Exception:
        return

    if completed.returncode != 0:
        return

    for line in completed.stdout.splitlines():
        if line.startswith("CommandLine="):
            value = line[len("CommandLine="):].strip()
            if value:
                yield value


def discover_lcu_auth() -> LcuAuth:
    sources = (_cmdlines_from_psutil(), _cmdlines_from_powershell(), _cmdlines_from_wmic())

    for source in sources:
        for command_line in source:
            auth = parse_command_line(command_line)
            if auth:
                return auth

    raise LcuError(
        "未找到正在运行的英雄联盟客户端（LeagueClientUx.exe）。\n"
        "请先启动客户端并登录，然后重新运行本工具。"
    )


def discover_lcu_port_token() -> Tuple[int, str]:
    auth = discover_lcu_auth()
    return auth.port, auth.token


class LcuClient:
    """标准库 LCU 客户端：Basic 鉴权 + 超时 + 连接级重试 + 统一错误。

    线程安全：每次请求独立连接，无共享可变状态。
    """

    def __init__(self, auth: LcuAuth, timeout: float = 20.0, retries: int = 0):
        self._port = auth.port
        self._auth_header = "Basic " + base64.b64encode(
            f"riot:{auth.token}".encode("utf-8")
        ).decode("ascii")
        self.timeout = timeout
        self.retries = max(0, retries)

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> bytes:
        headers = {"Authorization": self._auth_header, "Accept": "application/json"}
        payload: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")

        attempts = self.retries + 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            conn: Optional[http.client.HTTPSConnection] = None
            try:
                conn = http.client.HTTPSConnection(
                    "127.0.0.1",
                    self._port,
                    timeout=timeout or self.timeout,
                    context=ssl._create_unverified_context(),
                )
                conn.request(method, path, body=payload, headers=headers)
                response = conn.getresponse()
                data = response.read()

                if response.status >= 400:
                    text = data.decode("utf-8", errors="replace")[:300].replace(
                        "\n", " "
                    )
                    raise LcuError(f"LCU {method} {path} -> {response.status}: {text}")

                return data
            except TimeoutError as error:
                last_error = TimeoutError(f"LCU {method} {path} 请求超时: {error}")
            except OSError as error:
                last_error = LcuError(f"LCU 连接失败 {method} {path}: {error}")
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

    def get_json(self, path: str, timeout: Optional[float] = None) -> Any:
        data = self._request("GET", path, timeout=timeout)
        if not data:
            return None
        return json.loads(data.decode("utf-8", errors="replace"))

    def post_json(
        self, path: str, payload: Any, timeout: Optional[float] = None
    ) -> Any:
        data = self._request("POST", path, body=payload, timeout=timeout)
        if not data:
            return None
        return json.loads(data.decode("utf-8", errors="replace"))

    def get_text(self, path: str, timeout: Optional[float] = None) -> str:
        data = self._request("GET", path, timeout=timeout)
        return data.decode("utf-8", errors="replace") if data else ""
