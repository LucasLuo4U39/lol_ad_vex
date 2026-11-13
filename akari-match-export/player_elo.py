#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选手 Elo：用每名玩家最近 N 场（默认 25）的战绩算一个实力分，再折算成赛前胜率。

设计要点
--------
1. **赛前信息**：Elo 只使用「本场比赛之前」的 25 场，按时间顺序重放，避免用未来数据；
2. **对手也在算**：每场拿到的 SGP SUMMARY 含 10 名玩家的 puuid，所以不只算这 10 个人，
   同场的对手也会被计入评分网络（陌生玩家从 1500 起步、前 10 场用更大的 K）；
3. **只打排位**：默认过滤 queue 420/440、时长 ≥ 10 分钟，避免大乱斗/重开局污染；
4. **换算胜率**：`p_elo = 1 / (1 + 10^(-(R_blue - R_red) / 400))`；
5. **接入曲线**：`blend_win_prob()` 在 logit 空间把模型胜率和 Elo 先验混合，
   权重随时间指数衰减（开局信 Elo，越往后越信模型）：

       w(t) = weight * exp(-t / tau)
       logit(p) = w * logit(p_elo) + (1 - w) * logit(p_model)

离线时只有纯逻辑部分会被测试；SGP 拉取需要客户端在跑（LCU 端口/token + entitlements）。

用法
----
```bat
:: 1) 给某局算 Elo（需要客户端登录；写出 elo-<gameId>.json）
python player_elo.py --game-id 901127125498 --count 25 --out ../analysis/elo-901127125498.json

:: 2) 复盘时注入
python review_games.py --games 901127125498 --elo ../analysis/elo-901127125498.json
```
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import math
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SGP_HOST = "nj100-sgp.lol.qq.com"
DEFAULT_SUB_ID = "NJ100"
DEFAULT_QUEUE_IDS = (420, 440)
MIN_GAME_SECONDS = 600
BASE_RATING = 1500.0
K_NORMAL = 32.0
K_PROVISIONAL = 48.0
PROVISIONAL_GAMES = 10
ELO_SCALE = 400.0

_CTX = ssl._create_unverified_context()


# --------------------------------------------------------------------------
# 纯逻辑：Elo
# --------------------------------------------------------------------------
def expected_score(rating_a: float, rating_b: float, scale: float = ELO_SCALE) -> float:
    return 1.0 / (1.0 + 10 ** (-(rating_a - rating_b) / scale))


def update_pair(
    rating_a: float, rating_b: float, score_a: float, k: float = K_NORMAL
) -> Tuple[float, float]:
    expected = expected_score(rating_a, rating_b)
    delta = k * (score_a - expected)
    return rating_a + delta, rating_b - delta


def compute_ratings(
    matches: Iterable[Dict[str, Any]],
    initial: float = BASE_RATING,
    k: float = K_NORMAL,
    k_provisional: float = K_PROVISIONAL,
    provisional_games: int = PROVISIONAL_GAMES,
) -> Dict[str, Any]:
    """按时间顺序重放对局，返回 {puuid: rating} 与元信息。

    match = {"gameId", "creation", "winner": 100|200, "teams": {100: [puuid...], 200: [...]}}
    同一 gameId 只算一次（多个玩家抓到的同一局会重复出现）。
    """
    ordered = sorted(
        (m for m in matches if m.get("teams") and m.get("winner") in (100, 200)),
        key=lambda m: (m.get("creation") or 0, str(m.get("gameId"))),
    )
    seen_games: set = set()
    ratings: Dict[str, float] = {}
    games_played: Dict[str, int] = {}
    applied = 0

    for match in ordered:
        game_id = str(match.get("gameId"))
        if game_id in seen_games:
            continue
        seen_games.add(game_id)
        teams = {100: list(match["teams"].get(100) or []), 200: list(match["teams"].get(200) or [])}
        if not teams[100] or not teams[200]:
            continue
        for puuid in teams[100] + teams[200]:
            ratings.setdefault(puuid, initial)
            games_played.setdefault(puuid, 0)

        blue_avg = sum(ratings[p] for p in teams[100]) / len(teams[100])
        red_avg = sum(ratings[p] for p in teams[200]) / len(teams[200])
        expected_blue = expected_score(blue_avg, red_avg)
        score_blue = 1.0 if int(match["winner"]) == 100 else 0.0

        for team in (100, 200):
            expected_team = expected_blue if team == 100 else 1.0 - expected_blue
            actual = score_blue if team == 100 else 1.0 - score_blue
            for puuid in teams[team]:
                # 新号/样本少的玩家用更大的 K，收敛更快
                player_k = (
                    k_provisional if games_played[puuid] < provisional_games else k
                )
                ratings[puuid] += player_k * (actual - expected_team)
                games_played[puuid] += 1
        applied += 1

    return {
        "ratings": ratings,
        "games": applied,
        "players": len(ratings),
        "gamesPlayed": games_played,
    }


def team_rating(ratings: Dict[str, float], puuids: Sequence[str], default: float = BASE_RATING) -> float:
    values = [ratings.get(p, default) for p in puuids]
    return sum(values) / len(values) if values else default


def team_elo_diff(ratings: Dict[str, float], blue: Sequence[str], red: Sequence[str]) -> float:
    return team_rating(ratings, blue) - team_rating(ratings, red)


def elo_probability(rating_diff: float, scale: float = ELO_SCALE) -> float:
    return 1.0 / (1.0 + 10 ** (-rating_diff / scale))


# --------------------------------------------------------------------------
# 纯逻辑：接入胜率曲线
# --------------------------------------------------------------------------
def blend_weight(minute: float, weight: float, tau_seconds: float) -> float:
    if tau_seconds <= 0:
        return 0.0
    return max(0.0, weight) * math.exp(-max(0.0, minute) / tau_seconds)


def _logit(p: float) -> float:
    p = min(1 - 1e-6, max(1e-6, float(p)))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def blend_win_prob(
    model_prob: float,
    elo_prob: float,
    minute: float,
    weight: float = 0.5,
    tau_seconds: float = 900.0,
) -> float:
    """模型胜率与 Elo 先验在 logit 空间混合；Elo 权重随时间指数衰减。"""
    w = blend_weight(minute * 60.0, weight, tau_seconds)
    if w <= 0:
        return float(model_prob)
    if w >= 1:
        return float(elo_prob)
    return _sigmoid(w * _logit(elo_prob) + (1 - w) * _logit(model_prob))


# --------------------------------------------------------------------------
# SGP 抓取
# --------------------------------------------------------------------------
class SgpClient:
    """标准库直连 LCU + SGP（与项目其它脚本同一套鉴权方式）。"""

    def __init__(
        self,
        lcu_port: int,
        lcu_token: str,
        sgp_host: str = DEFAULT_SGP_HOST,
        sub_id: str = DEFAULT_SUB_ID,
        timeout: float = 20.0,
    ) -> None:
        self.lcu_port = lcu_port
        self.lcu_token = lcu_token
        self.sgp_host = sgp_host
        self.sub_id = sub_id
        self.timeout = timeout
        self.access_token = self._fetch_access_token()

    def _lcu(self, path: str) -> Optional[dict]:
        try:
            conn = http.client.HTTPSConnection(
                "127.0.0.1", self.lcu_port, timeout=self.timeout, context=_CTX
            )
            auth = "Basic " + base64.b64encode(
                f"riot:{self.lcu_token}".encode("utf-8")
            ).decode("ascii")
            conn.request("GET", path, headers={"Authorization": auth})
            response = conn.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if response.status == 200 else None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _fetch_access_token(self) -> str:
        payload = self._lcu("/entitlements/v1/token") or {}
        token = payload.get("accessToken") or ""
        if not token:
            raise RuntimeError("拿不到 entitlements accessToken（客户端没登录？）")
        return token

    def _sgp(self, path: str) -> Optional[dict]:
        try:
            conn = http.client.HTTPSConnection(
                self.sgp_host, 21019, timeout=self.timeout, context=_CTX
            )
            conn.request(
                "GET",
                path,
                headers={"Accept": "application/json", "Authorization": f"Bearer {self.access_token}"},
            )
            response = conn.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if response.status == 200 else None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def history(self, puuid: str, count: int = 25, start: int = 0) -> List[dict]:
        payload = self._sgp(
            f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY"
            f"?startIndex={start}&count={count}"
        ) or {}
        return [game.get("json") or {} for game in payload.get("games") or []]

    def game_details(self, game_id: str) -> Optional[dict]:
        payload = self._sgp(
            f"/match-history-query/v1/products/lol/{self.sub_id}_{game_id}/DETAILS"
        )
        return (payload or {}).get("json") or payload


def match_from_summary(game: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 SGP SUMMARY 的一条 game 转成 {gameId, creation, winner, teams}。"""
    if not game:
        return None
    participants = game.get("participants") or []
    identities = game.get("participantIdentities") or []
    if not participants or not identities:
        return None

    puuid_by_pid = {}
    for identity in identities:
        pid = identity.get("participantId")
        puuid = (identity.get("player") or {}).get("puuid")
        if pid is not None and puuid:
            puuid_by_pid[int(pid)] = puuid

    teams: Dict[int, List[str]] = {100: [], 200: []}
    winner: Optional[int] = None
    for participant in participants:
        pid = participant.get("participantId")
        team = participant.get("teamId")
        puuid = puuid_by_pid.get(int(pid)) if pid is not None else None
        if puuid and team in (100, 200):
            teams[int(team)].append(puuid)
        stats = participant.get("stats") or {}
        if stats.get("win") is True and team in (100, 200):
            winner = int(team)

    if not teams[100] or not teams[200]:
        return None
    if winner is None:
        for team in game.get("teams") or []:
            if str(team.get("win")).lower() == "win":
                winner = int(team.get("teamId"))
    if winner not in (100, 200):
        return None

    creation = game.get("gameCreation") or game.get("gameCreationDate") or 0
    if isinstance(creation, str):
        creation = 0
    return {
        "gameId": str(game.get("gameId")),
        "creation": int(creation),
        "queueId": int(game.get("queueId") or 0),
        "duration": int(game.get("gameDuration") or 0),
        "winner": winner,
        "teams": teams,
    }


def filter_history(games: Iterable[Dict[str, Any]], queue_ids: Sequence[int] = DEFAULT_QUEUE_IDS) -> List[Dict[str, Any]]:
    kept = []
    for game in games:
        queue_id = int(game.get("queueId") or 0)
        duration = int(game.get("gameDuration") or 0)
        if queue_ids and queue_id not in queue_ids:
            continue
        if duration and duration < MIN_GAME_SECONDS:
            continue
        kept.append(game)
    return kept


def filter_before(matches: Iterable[Dict[str, Any]], as_of: Optional[int]) -> List[Dict[str, Any]]:
    """只保留 as_of 之前结束的对局，避免用未来比赛去算历史局的 Elo。"""
    if not as_of:
        return list(matches)
    return [m for m in matches if int(m.get("creation") or 0) < int(as_of)]


def build_elo_for_game(
    game_id: str,
    client: SgpClient,
    count: int = 25,
    queue_ids: Sequence[int] = DEFAULT_QUEUE_IDS,
    cache_dir: Optional[Path] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """对局 10 人 → 各自最近 N 场 → Elo → 队伍评分。"""
    if use_cache and cache_dir is not None:
        cached = cache_dir / f"{game_id}.json"
        if cached.is_file():
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except Exception:
                pass

    details = client.game_details(game_id) or {}
    as_of = details.get("gameCreation") or 0
    if isinstance(as_of, str):
        as_of = 0
    identities = details.get("participantIdentities") or []
    roster: Dict[int, List[Dict[str, Any]]] = {100: [], 200: []}
    for identity in identities:
        pid = identity.get("participantId")
        player = identity.get("player") or {}
        puuid = player.get("puuid")
        if not puuid:
            continue
        team = None
        for participant in details.get("participants") or []:
            if participant.get("participantId") == pid:
                team = participant.get("teamId")
                break
        if team in (100, 200):
            roster[int(team)].append(
                {
                    "puuid": puuid,
                    "name": player.get("gameName") or player.get("summonerName") or "",
                    "tag": player.get("tagLine") or "",
                }
            )
    if not roster[100] or not roster[200]:
        raise RuntimeError(f"对局 {game_id} 的阵容解析失败（DETAILS 缺 participantIdentities）")

    matches: List[Dict[str, Any]] = []
    histories: Dict[str, int] = {}
    for team in (100, 200):
        for player in roster[team]:
            games = filter_history(client.history(player["puuid"], count=count), queue_ids)
            histories[player["puuid"]] = len(games)
            for game in games:
                parsed = match_from_summary(game)
                if parsed:
                    matches.append(parsed)
    matches = filter_before(matches, int(as_of) or None)

    result = compute_ratings(matches)
    ratings: Dict[str, float] = result["ratings"]
    blue = [p["puuid"] for p in roster[100]]
    red = [p["puuid"] for p in roster[200]]
    blue_elo = team_rating(ratings, blue)
    red_elo = team_rating(ratings, red)

    payload = {
        "gameId": str(game_id),
        "source": "sgp-elo",
        "count": count,
        "queueIds": list(queue_ids),
        "matches": result["games"],
        "asOf": int(as_of) if as_of else None,
        "playersRated": result["players"],
        "teamElo": {"100": round(blue_elo, 1), "200": round(red_elo, 1)},
        "prob": round(elo_probability(blue_elo - red_elo), 4),
        "players": {
            str(team): [
                {
                    "name": p["name"],
                    "tag": p["tag"],
                    "puuid": p["puuid"],
                    "elo": round(ratings.get(p["puuid"], BASE_RATING), 1),
                    "history": histories.get(p["puuid"], 0),
                }
                for p in roster[team]
            ]
            for team in (100, 200)
        },
        "generatedAt": int(time.time()),
    }
    if use_cache and cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{game_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return payload


def load_elo_map(path: Path) -> Dict[str, Any]:
    """读取 Elo JSON：支持 {gameId: {...}} 与单局 {...} 两种形态。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "gameId" in payload and "teamElo" in payload:
        return {str(payload["gameId"]): payload}
    return {str(key): value for key, value in payload.items()}


def team_elos(entry: Dict[str, Any]) -> Tuple[float, float]:
    team = entry.get("teamElo") or {}
    return float(team.get("100") or BASE_RATING), float(team.get("200") or BASE_RATING)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="用最近 N 场战绩算选手 Elo")
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--lcu-port", type=int, default=None)
    parser.add_argument("--lcu-token", default=None)
    parser.add_argument("--sgp-host", default=DEFAULT_SGP_HOST)
    parser.add_argument("--sub-id", default=DEFAULT_SUB_ID)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).resolve().parent / "elo_cache")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--queues", default=",".join(str(q) for q in DEFAULT_QUEUE_IDS))
    args = parser.parse_args(argv)

    port, token = args.lcu_port, args.lcu_token
    if not (port and token):
        from advisor_server import discover_lcu  # 复用 lockfile 探测

        found = discover_lcu()
        if found:
            port, token = found
    if not (port and token):
        print("需要 --lcu-port/--lcu-token，或客户端 lockfile 可读")
        return 1

    queues = tuple(int(q) for q in args.queues.split(",") if q.strip())
    client = SgpClient(port, token, args.sgp_host, args.sub_id)
    payload = build_elo_for_game(
        str(args.game_id),
        client,
        count=args.count,
        queue_ids=queues,
        cache_dir=args.cache_dir,
        use_cache=not args.no_cache,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"已写出 {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
