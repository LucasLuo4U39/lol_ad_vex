#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时段位解析：名字 → puuid → 段位分，喂给 live_model5 的 5 个特征。

链路（全部走本机 LCU，无需 SGP）：

    riotId "名字#TAG"
      → GET /lol-summoner/v1/summoners?name=名字#TAG   （拿 puuid）
      → GET /lol-ranked/v1/ranked-stats/{puuid}        （拿 tier/division/LP）
      → rank_score()                                   （折算成可比较的分数）

设计：
- 进程内缓存（默认 6 小时），一局里 10 个人只查一次；断线重连也不会重复查；
- 单个失败不影响其它人：只要 10 人里缺一个，`resolve_payload()` 就不返回完整结果，
  上层会退回不带段位特征的 live_model3，而不是拿 0 分瞎猜；
- LCU 端口/token 探测：先用 psutil 读 `LeagueClientUx.exe` 命令行，
  失败时回退读 LCU lockfile（不受进程提权影响，无需 psutil）；
- **不静默失败**：连接失败/HTTP 错误/无段位记录分别记录到
  `last_error` / `last_http_status` / `unranked` / `last_lookup_reasons`，
  供上层区分「客户端未连接 / 正在解析 / 解析失败原因」三种文案；
  连接失败时会自动重新探测端口/token 并重试一次（客户端重启后自愈）。

自检（无需真实客户端，注入假 LCU 数据验证链路）：
    python rank_lookup.py --self-test
"""

from __future__ import annotations

import base64
import http.client
import json
import ssl
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_CTX = ssl._create_unverified_context()
DEFAULT_TTL = 6 * 3600
DEFAULT_RANK = 4000.0

# LCU lockfile 候选路径（含国服默认安装位置）。
LCU_LOCKFILE_CANDIDATES = (
    Path("E:/网络游戏/英雄联盟/LeagueClient/lockfile"),
    Path("C:/Program Files/League of Legends/lockfile"),
    Path("C:/Program Files (x86)/League of Legends/lockfile"),
    Path("C:/Riot Games/League of Legends/lockfile"),
)


def discover_lcu() -> Optional[tuple]:
    """读 LCU lockfile（LeagueClient:port:token:https）。

    不依赖 psutil、不受客户端提权影响；客户端未运行时返回 None。
    """
    for path in LCU_LOCKFILE_CANDIDATES:
        try:
            parts = path.read_text(encoding="utf-8").strip().split(":")
            if len(parts) >= 3 and parts[0] == "LeagueClient":
                return int(parts[1]), parts[2]
        except (OSError, ValueError):
            continue
    return None


def discover_lcu_psutil() -> Optional[tuple]:
    """用 psutil 读 LeagueClientUx 命令行拿 --app-port / --remoting-auth-token。

    读不到（psutil 缺失、进程提权 cmdline 不可读、客户端未启动）时回退
    discover_lcu()（lockfile）。返回 (port, token) 或 None（此刻确实没找到）。
    """
    try:
        import psutil
    except Exception:
        return discover_lcu()

    try:
        for process in psutil.process_iter():
            try:
                if process.name() != "LeagueClientUx.exe":
                    continue
            except Exception:
                continue
            try:
                args_list = process.cmdline()
            except Exception:
                continue
            port = next(
                (a.split("=", 1)[1] for a in args_list if a.startswith("--app-port=")),
                None,
            )
            token = next(
                (
                    a.split("=", 1)[1]
                    for a in args_list
                    if a.startswith("--remoting-auth-token=")
                ),
                None,
            )
            if port and token:
                return int(port), token
    except Exception:
        pass
    return discover_lcu()


class RankResolver:
    def __init__(
        self,
        port: int,
        token: str,
        ttl: float = DEFAULT_TTL,
        timeout: float = 6.0,
        cache_path: Optional[Path] = None,
        source: str = "external",
    ) -> None:
        self.port = port
        self.token = token
        self.ttl = ttl
        self.timeout = timeout
        self.source = source
        self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()
        self._score_cache: Dict[str, tuple] = {}  # puuid -> (score, timestamp)
        self._puuid_cache: Dict[str, str] = {}
        self.cache_path = cache_path
        self.queries = 0
        self.errors = 0
        self.unranked = 0
        # 诊断状态（供上层展示三种文案：客户端未连接 / 正在解析 / 解析失败原因）
        self.last_error: Optional[str] = None
        self.last_http_status: Optional[int] = None
        self.last_note: Optional[str] = None
        self.last_lookup_reasons: Dict[str, str] = {}
        self._rediscover_at = 0.0
        self._warn_at = 0.0
        if cache_path is not None and cache_path.is_file():
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                for name, item in (raw.get("names") or {}).items():
                    if float(item.get("ts") or 0) + ttl > time.time():
                        self._score_cache[name] = (float(item["score"]), float(item["ts"]))
            except Exception:
                pass

    # ---------------------------------------------------------------- 诊断
    def _ensure_diag(self) -> None:
        """诊断字段懒初始化（兼容不经过 __init__ 的测试子类，如 test_rank.FakeResolver）。"""
        for attr, value in (
            ("last_error", None),
            ("last_http_status", None),
            ("last_note", None),
            ("last_lookup_reasons", {}),
            ("unranked", 0),
            ("_rediscover_at", 0.0),
            ("_warn_at", 0.0),
        ):
            if not hasattr(self, attr):
                setattr(self, attr, value)

    def _warn(self, message: str) -> None:
        """把失败原因打到 stderr（30 秒冷却，避免每 tick 刷屏）。"""
        self._ensure_diag()
        now = time.time()
        if now - self._warn_at >= 30.0:
            self._warn_at = now
            print(f"[rank_lookup] {message}", file=sys.stderr)

    def redetect(self) -> bool:
        """客户端重启后端口/token 会变：重新发现并更新连接（5 秒冷却）。

        返回 True 表示当前端口/token 仍可用或已更新（可重试请求）。
        """
        self._ensure_diag()
        now = time.time()
        if now - self._rediscover_at < 5.0:
            return False
        self._rediscover_at = now
        found = discover_lcu_psutil()
        if not found:
            self._warn("LCU 未找到（psutil 与 lockfile 都没读到，客户端未运行？）")
            return False
        port, token = found
        if port == self.port and token == self.token:
            return True
        self.port, self.token = port, token
        self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()
        self.source = "rediscovered"
        print(f"[rank_lookup] LCU 端口/token 已更新（新端口 {port}）", file=sys.stderr)
        return True

    def status(self) -> Dict[str, Any]:
        """给上层展示的诊断快照（区分三种状态用）。"""
        self._ensure_diag()
        return {
            "port": self.port,
            "source": self.source,
            "lastError": self.last_error,
            "lastHttpStatus": self.last_http_status,
            "lastNote": self.last_note,
            "queries": getattr(self, "queries", 0),
            "errors": getattr(self, "errors", 0),
            "unranked": self.unranked,
            "lookupReasons": dict(self.last_lookup_reasons),
        }

    # ---------------------------------------------------------------- HTTP
    def _request_once(self, path: str) -> Optional[Any]:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", self.port, timeout=self.timeout, context=_CTX
        )
        try:
            connection.request("GET", path, headers={"Authorization": self.auth})
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        self.last_http_status = response.status
        if response.status == 200:
            self.last_error = None
            return json.loads(body)
        self.last_error = f"LCU HTTP {response.status}（{path}）"
        self.errors += 1
        self._warn(self.last_error)
        return None

    def _get(self, path: str) -> Optional[Any]:
        """GET LCU 端点。失败不抛异常，但记录 last_error/last_http_status。

        连接失败或 401/403（客户端重启导致端口/token 变化）时自动
        redetect() 一次并重试。
        """
        self._ensure_diag()
        try:
            result = self._request_once(path)
        except Exception as error:  # 连接失败：可能是客户端刚重启
            self.last_http_status = None
            self.last_error = f"LCU 连接失败 {type(error).__name__}（{path}）"
            self.errors += 1
            self._warn(self.last_error)
            if self.redetect():
                try:
                    return self._request_once(path)
                except Exception as error_retry:
                    self.last_error = (
                        f"LCU 重连后仍失败 {type(error_retry).__name__}（{path}）"
                    )
                    self._warn(self.last_error)
            return None
        if result is None and self.last_http_status in (401, 403):
            if self.redetect():
                try:
                    return self._request_once(path)
                except Exception as error_retry:
                    self.last_error = (
                        f"LCU 鉴权重连后仍失败 {type(error_retry).__name__}（{path}）"
                    )
                    self._warn(self.last_error)
        return result

    # ---------------------------------------------------------------- lookup
    def current_summoner(self) -> Optional[str]:
        """当前登录账号的 riotId（游戏里没有 activePlayer 时用它挑视角）。"""
        payload = self._get("/lol-summoner/v1/current-summoner") or {}
        name = payload.get("gameName") or payload.get("displayName")
        tag = payload.get("tagLine") or ""
        if not name:
            return None
        return f"{name}#{tag}" if tag else name

    def puuid_for(self, name: str) -> Optional[str]:
        self._ensure_diag()
        if name in self._puuid_cache:
            return self._puuid_cache[name]
        payload = self._get("/lol-summoner/v1/summoners?name=" + urllib.parse.quote(name))
        puuid = (payload or {}).get("puuid")
        if puuid:
            self._puuid_cache[name] = str(puuid)
            return str(puuid)
        self.last_note = f"查不到召唤师（名字不匹配或非本区）: {name}"
        return None

    def score_for_puuid(self, puuid: str) -> Optional[float]:
        self._ensure_diag()
        cached = self._score_cache.get(puuid)
        if cached and cached[1] + self.ttl > time.time():
            return cached[0]
        payload = self._get(f"/lol-ranked/v1/ranked-stats/{puuid}")
        if payload is None:
            return None  # 原因已记在 last_error / last_http_status
        entry = (payload.get("queueMap") or {}).get("RANKED_SOLO_5x5") or {}
        tier = entry.get("tier") or ""
        if not tier:
            self.unranked += 1
            self.last_note = "无单双排段位记录（未定级或只打灵活排位）"
            return None
        from fetch_player_ranks import rank_score

        score = float(
            rank_score(tier, entry.get("division") or "NA", int(entry.get("leaguePoints") or 0))
        )
        self._score_cache[puuid] = (score, time.time())
        self.queries += 1
        self.last_note = None
        return score

    def lookup(self, name: str) -> Optional[float]:
        """名字 → 段位分；失败返回 None（不抛异常），原因见 last_lookup_reasons/status()。"""
        self._ensure_diag()
        puuid = self.puuid_for(name)
        if not puuid:
            self.errors += 1
            self.last_lookup_reasons[name] = (
                self.last_error or self.last_note or "名字查不到 puuid"
            )
            return None
        score = self.score_for_puuid(puuid)
        if score is None:
            self.last_lookup_reasons[name] = (
                self.last_error or self.last_note or "段位查询失败"
            )
        else:
            self.last_lookup_reasons.pop(name, None)
        return score

    def resolve_payload(self, payload: Dict[str, Any]) -> Dict[str, float]:
        """把 allPlayers 的 riotId 解析成段位分（可能少于 10 人，缺人交给上层补齐）。"""
        players = payload.get("allPlayers") or []
        ranks: Dict[str, float] = {}
        for player in players:
            name = str(player.get("riotId") or player.get("summonerName") or "")
            if not name:
                continue
            score = self.lookup(name)
            if score is not None:
                ranks[name] = score
        return ranks

    def save(self) -> None:
        if self.cache_path is None:
            return
        try:
            names = {
                puuid: {"score": score, "ts": ts}
                for puuid, (score, ts) in self._score_cache.items()
            }
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"names": names}), encoding="utf-8")
        except Exception:
            pass


def complete_payload_ranks(
    payload: Dict[str, Any], ranks: Dict[str, float]
) -> Dict[str, float]:
    """10 人里有一部分查不到时，用本队已查到的人均值补齐。

    淘汰/改名玩家很常见，缺 1–2 个不至于让整局退回 model3。
    用队伍均值补不会改变队伍均值本身，所以 `rankDiff` 仍然准确；
    只有当事人的 `ownRank` 会变成队伍均值（`ownRankVsTeam` = 0）。
    """
    players = payload.get("allPlayers") or []
    by_team: Dict[str, List[float]] = {}
    for player in players:
        name = str(player.get("riotId") or player.get("summonerName") or "")
        if name in ranks:
            by_team.setdefault(str(player.get("team")), []).append(ranks[name])
    if len(ranks) < 8:
        return {}
    filled = dict(ranks)
    for player in players:
        name = str(player.get("riotId") or player.get("summonerName") or "")
        if name in filled:
            continue
        values = by_team.get(str(player.get("team"))) or []
        if len(values) >= 3:
            filled[name] = sum(values) / len(values)
    return filled if len(filled) >= 10 else {}


def resolve_once(players: List[str], ttl: float = DEFAULT_TTL) -> Dict[str, float]:
    """命令行小工具：给一串 riotId 返回段位分。"""
    found = discover_lcu_psutil()
    if not found:
        raise RuntimeError("找不到 LCU（客户端没开？）")
    resolver = RankResolver(found[0], found[1], ttl=ttl)
    return {name: resolver.lookup(name) for name in players}


# --------------------------------------------------------------------------
# 自检：注入假 LCU 数据，验证「名字→puuid→段位」链路、缓存、失败分类与自愈
# --------------------------------------------------------------------------
def run_selftest() -> int:
    """python rank_lookup.py --self-test（无需真实客户端）。"""
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}  {detail}")

    # [1] 名字→puuid→段位链路 + 进程内缓存
    print("[1] 查询链路与缓存")
    resolver = RankResolver(12345, "token-1", ttl=6 * 3600)
    responses = {
        "/lol-summoner/v1/summoners?name=A%231": {"puuid": "puuid-a"},
        "/lol-ranked/v1/ranked-stats/puuid-a": {
            "queueMap": {"RANKED_SOLO_5x5": {"tier": "DIAMOND", "division": "I", "leaguePoints": 50}}
        },
        "/lol-summoner/v1/summoners?name=B%231": {"puuid": "puuid-b"},
        "/lol-ranked/v1/ranked-stats/puuid-b": {"queueMap": {}},
        "/lol-summoner/v1/summoners?name=C%231": None,
    }
    calls: List[str] = []
    resolver._get = lambda path: (calls.append(path), responses.get(path))[1]  # type: ignore[method-assign]
    check("DIAMOND I 50LP → 3305", resolver.lookup("A#1") == 3305.0, str(resolver.lookup("A#1")))
    check("二次查询命中缓存（无新请求）", resolver.lookup("A#1") == 3305.0 and len(calls) == 2, str(calls))
    check("无段位记录 → None 且 unranked=1", resolver.lookup("B#1") is None and resolver.unranked == 1, str(resolver.status()))
    check("未定级原因记录", "无单双排段位记录" in resolver.status()["lastNote"], str(resolver.status()["lastNote"]))
    check("查无此人 → None 且记录原因", resolver.lookup("C#1") is None and "查不到召唤师" in resolver.last_lookup_reasons.get("C#1", ""), str(resolver.last_lookup_reasons))

    # [2] 缓存过期（6h TTL）
    print("[2] 缓存过期")
    resolver._score_cache["puuid-a"] = (3305.0, time.time() - 7 * 3600)
    before = len(calls)
    check("过期后重新查询段位", resolver.lookup("A#1") == 3305.0 and len(calls) == before + 1, str(calls[before:]))

    # [3] HTTP 错误分类（假 HTTPS 连接）
    print("[3] HTTP 错误分类与重连自愈")
    import http.client as hc

    class FakeResponse:
        def __init__(self, status: int, body: str = ""):
            self.status = status
            self._body = body.encode("utf-8")

        def read(self):
            return self._body

    class FakeConnection:
        broken = False

        def __init__(self, host, port, timeout=None, context=None):
            self.port = port

        def request(self, method, path, headers=None):
            self.path = path

        def getresponse(self):
            if FakeConnection.broken:
                raise OSError("connection refused")
            return FakeResponse(500, "internal error")

        def close(self):
            pass

    orig_conn = hc.HTTPSConnection
    hc.HTTPSConnection = FakeConnection
    try:
        resolver2 = RankResolver(12345, "token-1")
        check("HTTP 500 → None 且记录状态", resolver2.lookup("A#1") is None and resolver2.last_http_status == 500, str(resolver2.status()))
        check("last_error 含 HTTP 500", "HTTP 500" in (resolver2.last_error or ""), str(resolver2.last_error))

        # 连接失败 → 自动 redetect（发现新端口/token）→ 重试成功。
        # 注意：以脚本方式运行时 RankResolver 位于 __main__ 模块，必须 patch
        # 类所在模块的全局 discover_lcu_psutil，而不是重新 import 的副本。
        mod = sys.modules[RankResolver.__module__]
        resolver3 = RankResolver(11111, "old-token")
        orig_discover = mod.discover_lcu_psutil
        mod.discover_lcu_psutil = lambda: (22222, "new-token")
        FakeConnection.broken = True
        try:
            # 第一次 _request_once 抛连接异常 → _get 里 redetect → 重试 _request_once
            class _Boom:
                first = True

                def __call__(self, path):
                    if _Boom.first:
                        _Boom.first = False
                        raise OSError("connection refused")
                    return {"ok": True}

            boom = _Boom()
            resolver3._request_once = boom  # type: ignore[method-assign]
            result = resolver3._get("/x")
            check(
                "连接失败自动重连并重试成功",
                result == {"ok": True} and resolver3.port == 22222,
                str(resolver3.status()),
            )
        finally:
            mod.discover_lcu_psutil = orig_discover
        FakeConnection.broken = False
    finally:
        hc.HTTPSConnection = orig_conn

    # [4] LCU 探测：lockfile 回退
    print("[4] LCU 探测（psutil 缺失时回退 lockfile）")
    import tempfile

    mod = sys.modules[RankResolver.__module__]
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "lockfile"
        lock.write_text("LeagueClient:9999:abc-token:https", encoding="utf-8")

        orig_candidates = mod.LCU_LOCKFILE_CANDIDATES
        mod.LCU_LOCKFILE_CANDIDATES = (lock,)
        try:
            check("discover_lcu 解析 lockfile", mod.discover_lcu() == (9999, "abc-token"), str(mod.discover_lcu()))

            import builtins

            orig_import = builtins.__import__

            def no_psutil(name, *args, **kwargs):
                if name == "psutil":
                    raise ImportError("psutil not available")
                return orig_import(name, *args, **kwargs)

            builtins.__import__ = no_psutil
            try:
                check("psutil 不可用时 discover_lcu_psutil 回退 lockfile", mod.discover_lcu_psutil() == (9999, "abc-token"), str(mod.discover_lcu_psutil()))
            finally:
                builtins.__import__ = orig_import
        finally:
            mod.LCU_LOCKFILE_CANDIDATES = orig_candidates

    # [5] partial ranks 补齐
    print("[5] complete_payload_ranks")
    payload = {
        "allPlayers": [
            {"riotId": f"B{i}#1", "team": "ORDER"} for i in range(5)
        ] + [
            {"riotId": f"R{i}#1", "team": "CHAOS"} for i in range(5)
        ]
    }
    ranks = {f"B{i}#1": 5000.0 for i in range(5)}
    ranks.update({f"R{i}#1": 4000.0 for i in range(4)})
    filled = complete_payload_ranks(payload, ranks)
    check("缺 1 人补齐为 10 人", len(filled) == 10, str(len(filled)))
    check("缺的人用本队均值", filled["R4#1"] == 4000.0, str(filled.get("R4#1")))
    check("不足 8 人放弃补齐", complete_payload_ranks(payload, {f"B{i}#1": 5000.0 for i in range(3)}) == {})

    print(f"\n结果: PASS={passed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--self-test":
        raise SystemExit(run_selftest())
    if not args:
        print("用法: python rank_lookup.py 名字#TAG [...]  或  python rank_lookup.py --self-test")
        raise SystemExit(2)
    result = resolve_once(args)
    for name, score in result.items():
        print(f"{name}\t{score}")
