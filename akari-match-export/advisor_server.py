#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live 实时助手服务器：本地 Web 仪表盘 + 可选 LLM 建议层 + 快照回放模式。

纯标准库 + numpy + lgbmini 实现（不需要 lightgbm / pandas / requests）：

- 实时模式（默认）：轮询 Live Client Data (127.0.0.1:2999)，
  用 live_model3 / live_model5 预测胜率（按段位解析可用性自动切换）、
  live_event_model 预测击杀/gank 概率，
  提供 http://127.0.0.1:8765 仪表盘。
- 回放模式（--snapshots）：按 gameTime 加速回放 replay_collector 采集的
  snapshots.jsonl，用于不开启对局时端到端测试。用 --player 指定自己。
- LLM 层（--llm）：把结构化状态发给 OpenAI 兼容接口生成自然语言建议；
  未配置时自动退回规则建议。配置方式：
    ADVISOR_LLM_BASE_URL（默认 https://api.deepseek.com）
    ADVISOR_LLM_API_KEY（必需）
    ADVISOR_LLM_MODEL （默认 deepseek-chat）
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from lgbmini import LgbTextModel
from live_features import (
    EVENT_LABELS,
    ROLES,
    EventAlertGate,
    advice as rule_advice,
    event_killer,
    event_name,
    extract_features,
    feature_row,
    load_event_policy,
    player_team_by_name,
    rank_fallback_required,
    validate_opgg_stats,
)
from attribution import FEATURE_CN, attribute_win_prob
from rank_lookup import RankResolver, complete_payload_ranks, discover_lcu_psutil
from sgp_gold import SgpGoldBridge

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ENDPOINT_PATH = "/liveclientdata/allgamedata"
_UNVERIFIED_CONTEXT: Optional[ssl.SSLContext] = None

OBJECTIVE_LABELS = {
    "DragonKill": "小龙",
    "BaronKill": "大龙",
    "HeraldKill": "峡谷先锋",
    "HordeKill": "虚空巢虫",
    "AtakhanKill": "厄塔汗",
    "TurretKilled": "防御塔",
    "InhibKilled": "水晶",
}
FEED_LIMIT = 40
HISTORY_LIMIT = 2400

DEFAULT_MODEL_DIR = (
    Path(__file__).resolve().parent.parent / "dataset" / "live_dataset" / "live_model2"
)
LLM_ENV_FILE = Path(__file__).resolve().parent / "advisor.llm.env"

# 新界面（v2.3）：serve ui/next/dashboard.html；旧 advisor_dashboard.html 保留作回退
_UI_DIR = Path(__file__).resolve().parent / "ui"
DASHBOARD_HTML = _UI_DIR / "next" / "dashboard.html"
if not DASHBOARD_HTML.is_file():
    DASHBOARD_HTML = Path(__file__).resolve().parent / "advisor_dashboard.html"
DESIGN_CSS = _UI_DIR / "design-system.css"
DEMO_JS = _UI_DIR / "next" / "demo-data.js"


def load_env_file(path: Path) -> Dict[str, str]:
    """Read KEY=VALUE lines (ignores comments/blank lines)."""
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "***"
    return f"{key[:8]}****{key[-4:]}"


LCU_LOCKFILE_CANDIDATES = [
    Path("E:/网络游戏/英雄联盟/LeagueClient/lockfile_"),
    Path("E:/网络游戏/英雄联盟/LeagueClient/lockfile"),
    Path("C:/Program Files/League of Legends/lockfile_"),
    Path("C:/Program Files/League of Legends/lockfile"),
    Path("C:/Program Files (x86)/League of Legends/lockfile"),
    Path("C:/Riot Games/League of Legends/lockfile"),
]


def discover_lcu() -> Optional[tuple[int, str]]:
    """从 LCU lockfile（LeagueClient:port:password:https）读取端口和 token。"""
    for path in LCU_LOCKFILE_CANDIDATES:
        try:
            parts = path.read_text(encoding="utf-8").strip().split(":")
            if len(parts) >= 3 and parts[0] == "LeagueClient":
                return int(parts[1]), parts[2]
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# live fetch (stdlib http.client, self-signed cert is expected on 2999)
# --------------------------------------------------------------------------
def _unverified_context() -> ssl.SSLContext:
    global _UNVERIFIED_CONTEXT
    if _UNVERIFIED_CONTEXT is None:
        _UNVERIFIED_CONTEXT = ssl._create_unverified_context()
    return _UNVERIFIED_CONTEXT


def patch_replay_self(
    payload: Dict[str, Any], fragment: str, gold_for=None
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """正在播放的回放没有 activePlayer：从 allPlayers 里按片段找到"自己"并注入。

    金币优先用 gold_for(name, game_time)（SGP 1 分钟帧的口袋金币 currentGold）；
    拿不到时回退到装备总价估算。来源标记在 activePlayer["_goldSource"]。
    """
    players = payload.get("allPlayers") or []
    lowered = fragment.strip().lower()
    matches = []
    for player in players:
        name = str(player.get("riotId") or player.get("summonerName") or "")
        if lowered in name.lower() or lowered in champion_display(player).lower():
            matches.append(player)

    if not matches:
        candidates = ", ".join(
            f"{p.get('riotId') or p.get('summonerName')}({champion_display(p)})"
            for p in players
        )
        raise ValueError(f"找不到 --player {fragment!r}。可选: {candidates}")

    self_player = matches[0]
    self_name = str(self_player.get("riotId") or self_player.get("summonerName") or "")

    gold_source = "estimate"
    gold = 0.0
    for item in self_player.get("items") or []:
        gold += float(item.get("price") or 0) * float(item.get("count") or 1)

    if gold_for is not None:
        game_time = float((payload.get("gameData") or {}).get("gameTime") or 0)
        try:
            pocket = gold_for(self_name, game_time)
            if pocket is not None:
                gold = float(pocket)
                gold_source = "sgp"
        except Exception:
            pass

    patched = dict(payload)
    patched["activePlayer"] = {
        "summonerName": self_name,
        "level": self_player.get("level"),
        "currentGold": gold,
        "_goldSource": gold_source,
    }
    return patched, self_player


def fetch_payload(timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        conn = http.client.HTTPSConnection(
            "127.0.0.1", 2999, timeout=timeout, context=_unverified_context()
        )
        conn.request("GET", ENDPOINT_PATH)
        response = conn.getresponse()
        if response.status != 200:
            conn.close()
            return None
        body = response.read()
        conn.close()
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# replay snapshot source
# --------------------------------------------------------------------------
def champion_display(player: Dict[str, Any]) -> str:
    raw = str(player.get("rawChampionName") or "")
    if raw.startswith("game_character_displayname_"):
        return raw.split("game_character_displayname_", 1)[1]
    return str(player.get("champion") or player.get("championName") or "?")


class SnapshotSource:
    """Replays a snapshots.jsonl file at game-time pace * speed."""

    def __init__(self, path: Path, player_fragment: str, speed: float):
        self.snapshots: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    self.snapshots.append(json.loads(line))
        if not self.snapshots:
            raise ValueError(f"{path} 没有快照")

        self.players = (self.snapshots[0].get("data") or {}).get("players") or []
        self.self_player = self._resolve_player(player_fragment)
        self.speed = speed
        self.index = 0
        self.base_wall: Optional[float] = None
        self.base_game = 0.0
        self.ended = False

    def _resolve_player(self, fragment: str) -> Dict[str, Any]:
        lowered = fragment.strip().lower()
        if not lowered:
            raise ValueError("回放模式必须用 --player 指定自己（riotId 或英雄名片段）")

        matches = []
        for player in self.players:
            riot_id = str(player.get("riotId") or "")
            champ = champion_display(player)
            if lowered in riot_id.lower() or lowered in champ.lower():
                matches.append(player)

        if not matches:
            candidates = ", ".join(
                f"{p.get('riotId')}({champion_display(p)})" for p in self.players
            )
            raise ValueError(f"找不到 --player {fragment!r}。可选: {candidates}")

        player = matches[0]
        print(f"回放视角: {player.get('riotId')} ({champion_display(player)}) 队伍 {player.get('team')}")
        return player

    def _item_gold(self, player: Dict[str, Any]) -> float:
        total = 0.0
        for item in player.get("items") or []:
            total += float(item.get("price") or 0) * float(item.get("count") or 1)
        return total

    def to_payload(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        data = snapshot.get("data") or {}
        all_players = []
        for player in data.get("players") or []:
            scores = player.get("scores") or {}
            all_players.append(
                {
                    "riotId": player.get("riotId"),
                    "summonerName": player.get("riotId"),
                    "team": player.get("team"),
                    "position": player.get("position") or "NONE",
                    "level": player.get("level"),
                    "isDead": player.get("isDead"),
                    "respawnTimer": player.get("respawnTimer"),
                    "rawChampionName": player.get("rawChampionName"),
                    "champion": champion_display(player),
                    "scores": {
                        "kills": scores.get("kills"),
                        "deaths": scores.get("deaths"),
                        "assists": scores.get("assists"),
                        "creepScore": scores.get("creepScore"),
                        "wardScore": scores.get("wardScore"),
                    },
                    "items": player.get("items"),
                }
            )

        self_name = str(self.self_player.get("riotId") or "")
        # 用当前快照里对应的玩家数据（等级/金币/装备随对局推进变化）
        current_self = self.self_player
        for player in data.get("players") or []:
            if str(player.get("riotId") or "") == self_name:
                current_self = player
                break
        return {
            "activePlayer": {
                "summonerName": self_name,
                "level": current_self.get("level"),
                # 回放没有 currentGold：用装备总价估算（与 build_replay_1s_dataset 一致）
                "currentGold": self._item_gold(current_self),
            },
            "allPlayers": all_players,
            "gameData": {"gameTime": snapshot.get("gameTime")},
            "events": {"Events": snapshot.get("events") or []},
        }

    def next_payload(self) -> Optional[Dict[str, Any]]:
        """None when the next snapshot is not due yet or the file ended."""
        if self.ended or self.index >= len(self.snapshots):
            return None
        snapshot = self.snapshots[self.index]
        game_time = float(snapshot.get("gameTime") or 0)
        if self.base_wall is None:
            self.base_wall = time.monotonic()
            self.base_game = game_time
        elapsed_game = (time.monotonic() - self.base_wall) * self.speed
        if self.base_game + elapsed_game < game_time:
            return None
        self.index += 1
        if self.index >= len(self.snapshots):
            self.ended = True
        return self.to_payload(snapshot)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
class ModelSet:
    def __init__(self, model_dir: Path, event_model_dir: Optional[Path]):
        self.win = LgbTextModel(model_dir / "model.txt")
        self.win_names = self.win.feature_names
        self.event_models: Dict[str, LgbTextModel] = {}
        self.event_policy = load_event_policy(
            (event_model_dir / "event_thresholds.json") if event_model_dir else None
        )
        if event_model_dir and event_model_dir.is_dir():
            for label in EVENT_LABELS:
                path = event_model_dir / f"model_{label}.txt"
                if path.is_file():
                    self.event_models[label] = LgbTextModel(path)

    def predict(self, features: Dict[str, Any]) -> Dict[str, float]:
        row = np.array([feature_row(features, self.win_names)], dtype=np.float64)
        win_prob = float(self.win.predict_proba(row)[0])
        result = {"win": win_prob}
        for label, model in self.event_models.items():
            row = np.array([feature_row(features, model.feature_names)], dtype=np.float64)
            result[label] = float(model.predict_proba(row)[0])
        return result


# --------------------------------------------------------------------------
# event feed
# --------------------------------------------------------------------------
class FeedBuilder:
    def __init__(self):
        self.seen: Dict[int, int] = {}  # EventID -> count
        self.recent: List[Dict[str, Any]] = []  # cumulative window, newest last

    def build(
        self, payload: Dict[str, Any], own_team: Optional[str], game_time: float
    ) -> List[Dict[str, Any]]:
        players = payload.get("allPlayers") or []
        name_to_champ = {}
        for player in players:
            name_to_champ[str(player.get("riotId") or player.get("summonerName") or "")] = (
                champion_display(player)
            )

        def display(name: Optional[str]) -> str:
            if not name:
                return "?"
            base = name.split("#")[0]
            for key, champ in name_to_champ.items():
                if key == name or key.split("#")[0] == base:
                    return champ
            return base

        items: List[Dict[str, Any]] = []
        for event in (payload.get("events") or {}).get("Events") or []:
            t = float(event.get("EventTime") or 0)
            if t > game_time + 1e-6:
                continue
            eid = event.get("EventID")
            if eid is not None and self.seen.get(eid, 0) > 0:
                continue
            if eid is not None:
                self.seen[eid] = self.seen.get(eid, 0) + 1

            name = event_name(event)
            text: Optional[str] = None
            side: Optional[str] = None

            if name == "ChampionKill":
                killer = display(event_killer(event))
                victim = display(event.get("VictimName"))
                killer_team = player_team_by_name(players, event_killer(event))
                side = "own" if killer_team == own_team else (
                    "enemy" if killer_team else None
                )
                text = f"{killer} 击杀 {victim}"
            elif name in OBJECTIVE_LABELS:
                killer_team = player_team_by_name(players, event_killer(event))
                if killer_team == own_team:
                    side, prefix = "own", "我方"
                elif killer_team:
                    side, prefix = "enemy", "敌方"
                else:
                    side, prefix = None, ""
                text = f"{prefix}拿下{OBJECTIVE_LABELS[name]}"

            if text:
                items.append({"t": t, "text": text, "side": side})

        items.sort(key=lambda item: item["t"])
        self.recent.extend(items)
        if len(self.recent) > FEED_LIMIT:
            del self.recent[: len(self.recent) - FEED_LIMIT]
        return list(self.recent)


# --------------------------------------------------------------------------
# LLM layer
# --------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是英雄联盟实时教练。只基于用户提供的结构化数据给 2-4 条简短、可执行的建议，"
    '每条一行，以 "- " 开头，全部建议不超过 120 字。不要编造数据中没有的信息，'
    "不要解释模型原理，不要重复胜率数字本身。"
    "金币指玩家兜里未花费、可用于购买装备的钱，推荐装备时只看这个数。"
)


def build_llm_prompt(
    clock: str,
    probability: float,
    features: Dict[str, Any],
    event_probabilities: Optional[Dict[str, float]],
    feed: List[Dict[str, Any]],
    gold_label: str = "金币",
) -> str:
    lines = [
        f"对局时间: {clock}",
        f"胜率模型(我方): {probability:.1%}",
        (
            f"自己: 等级{int(features['ownLevel'])} "
            f"KDA {int(features['ownKills'])}/{int(features['ownDeaths'])}/"
            f"{int(features['ownAssists'])} 补刀{int(features['ownCS'])} "
            f"{gold_label}{int(features['ownGold'])}"
        ),
        (
            f"团队差: 击杀{int(features['killDiff']):+d} "
            f"助攻{int(features['assistDiff']):+d} "
            f"补刀{int(features['csDiff']):+d} "
            f"等级和{int(features['levelSumDiff']):+d}"
        ),
        (
            f"目标差: 小龙{int(features['dragonDiff']):+d} "
            f"大龙{int(features['baronDiff']):+d} "
            f"先锋{int(features['heraldDiff']):+d} "
            f"巢虫{int(features['grubDiff']):+d} "
            f"厄塔汗{int(features['atakhanDiff']):+d} "
            f"塔{int(features['towerDiff']):+d} "
            f"水晶{int(features['inhibitorDiff']):+d}"
        ),
    ]
    if event_probabilities:
        lines.append(
            f"事件概率: 30秒内我方击杀{event_probabilities['own_kill_30s']:.0%} / "
            f"敌方击杀{event_probabilities['enemy_kill_30s']:.0%}；"
            f"60秒内我方gank{event_probabilities['own_gank_60s']:.0%} / "
            f"敌方gank{event_probabilities['enemy_gank_60s']:.0%}"
        )
    if "rankDiff" in features:
        lines.append(
            f"赛前实力(段位分): 我方 {features['teamRank']:.0f} / 敌方 {features['enemyRank']:.0f}"
            f"（差 {features['rankDiff']:+.0f}；自己 {features['ownRank']:.0f}，"
            f"相对队伍 {features['ownRankVsTeam']:+.0f}）"
        )
    if feed:
        recent = "；".join(
            f"[{int(item['t']) // 60:02d}:{int(item['t']) % 60:02d}]"
            f"{'我方' if item['side'] == 'own' else '敌方' if item['side'] == 'enemy' else ''}"
            f"{item['text']}"
            for item in feed[-6:]
        )
        lines.append(f"近 2 分钟事件: {recent}")
    return "\n".join(lines)


def call_llm(base_url: str, api_key: str, model: str, prompt: str, timeout: float = 8.0) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            # deepseek-flash/v4-pro 是带推理的模型：max_tokens 必须覆盖
            # reasoning_tokens，200 会被推理吃光导致内容截断/为空
            "max_tokens": 800,
            "effort": "low",
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return str(payload["choices"][0]["message"]["content"])


def parse_llm_advice(text: str) -> List[str]:
    tips = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = line[2:].strip() if line.startswith("- ") else line.lstrip("- ").strip()
        if line:
            tips.append(line)
    return tips[:4]


class LlmWorker:
    """Background thread; only the newest request is kept, failures degrade to rules."""

    def __init__(self, base_url: str, api_key: str, model: str, interval: float):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.interval = interval
        self.pending: Optional[Dict[str, Any]] = None
        self.lock = threading.Lock()
        self.result: Dict[str, Any] = {"tips": [], "lastAt": None, "error": None}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def request(self, game_time: float, prompt: str) -> None:
        with self.lock:
            if self.pending is not None:
                return  # busy; skip this window
            self.pending = {"gameTime": game_time, "prompt": prompt}

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.result)

    def _run(self) -> None:
        while True:
            with self.lock:
                pending = self.pending
                self.pending = None
            if pending is None:
                time.sleep(0.5)
                continue
            try:
                text = call_llm(
                    self.base_url, self.api_key, self.model, pending["prompt"]
                )
                tips = parse_llm_advice(text)
                with self.lock:
                    self.result = {
                        "tips": tips,
                        "lastAt": pending["gameTime"],
                        "error": None,
                    }
            except Exception as error:  # noqa: BLE001
                with self.lock:
                    self.result = {
                        "tips": [],
                        "lastAt": pending["gameTime"],
                        "error": str(error),
                    }
                time.sleep(2.0)


# --------------------------------------------------------------------------
# state + http
# --------------------------------------------------------------------------
def rank_status_payload(resolver: Optional[Any]) -> Dict[str, Any]:
    """段位解析器诊断快照（形状与 champ_select_server 的 T18 实现一致）。"""
    if resolver is None:
        return {
            "state": "unconnected", "port": None, "source": None,
            "lastError": None, "lastHttpStatus": None, "lastNote": None,
            "queries": 0, "errors": 0, "unranked": 0, "lookupReasons": {},
        }
    try:
        status = resolver.status()
    except Exception:  # noqa: BLE001
        status = {}
    if status.get("lastError") or status.get("lastNote"):
        state = "failed"
    elif status.get("queries"):
        state = "ok"
    else:
        state = "resolving"
    status["state"] = state
    return status


def rank_error_text(resolver: Optional[Any]) -> Optional[str]:
    if resolver is None:
        return "LCU 未连接"
    try:
        status = resolver.status()
    except Exception:  # noqa: BLE001
        return None
    return status.get("lastError") or status.get("lastNote")


class State:
    def __init__(self, source: str, llm: Optional[LlmWorker], replay_self: Optional[Dict[str, Any]] = None):
        self.lock = threading.Lock()
        self.source = source
        self.llm = llm
        self.waiting = True
        self.ended = False
        self.game_time = 0.0
        self.clock = "--:--"
        self.win_prob: Optional[float] = None
        self.features: Dict[str, Any] = {}
        self.event_probs: Dict[str, float] = {}
        self.rule_tips: List[str] = []
        self.feed: List[Dict[str, Any]] = []
        self.history: Dict[str, List[float]] = {
            "t": [], "winProb": [], "killDiff": [], "csDiff": [], "levelSumDiff": [],
            "ownKill30": [], "enemyKill30": [], "ownGank60": [], "enemyGank60": [],
        }
        self.own_team: Optional[str] = None
        self.self_name = (replay_self or {}).get("riotId") or ""
        self.self_champion = champion_display(replay_self) if replay_self else ""
        self.gold_source = "live" if source == "live" else "estimate"
        self.model_name = ""
        # 段位解析器（供 rankStatus/rankError 透传；desktop_app 可改用 provider 回调）
        self.resolver: Optional[Any] = None
        self._resolver_provider: Optional[Any] = None
        # T49：胜率归因 top3（无模型/无特征时为 None）
        self.attribution: Optional[List[Dict[str, Any]]] = None
        # T60：事件提醒闸门（同一局内做连续确认与冷却；新一局由时间回退自动重置）
        self.alert_gate: Optional[EventAlertGate] = None

    def set_attribution(self, attribution: Optional[List[Dict[str, Any]]]) -> None:
        with self.lock:
            self.attribution = attribution

    def _current_resolver(self) -> Optional[Any]:
        resolver = self.resolver
        if resolver is None and self._resolver_provider is not None:
            try:
                resolver = self._resolver_provider()
            except Exception:  # noqa: BLE001
                resolver = None
        return resolver

    def set_self(self, player: Dict[str, Any]) -> None:
        """直播回放模式下，首次拿到 payload 后设置视角信息。"""
        with self.lock:
            self.self_name = str(
                player.get("riotId") or player.get("summonerName") or ""
            )
            self.self_champion = champion_display(player)
            self.gold_source = "estimate"

    def update(
        self,
        payload: Dict[str, Any],
        predictions: Dict[str, float],
        features: Dict[str, Any],
        tips: List[str],
        feed: List[Dict[str, Any]],
        own_team: Optional[str],
        self_name: str,
        self_champion: Optional[str] = None,
    ) -> None:
        game_time = float((payload.get("gameData") or {}).get("gameTime") or 0)
        with self.lock:
            self.waiting = False
            self.game_time = game_time
            self.clock = f"{int(game_time) // 60:02d}:{int(game_time) % 60:02d}"
            self.win_prob = predictions["win"]
            self.features = features
            self.event_probs = {
                label: predictions.get(label)
                for label in EVENT_LABELS
                if label in predictions
            }
            self.rule_tips = tips
            self.feed = feed
            self.own_team = own_team
            self.self_name = self_name
            if self_champion:
                self.self_champion = self_champion
            gold_src = (payload.get("activePlayer") or {}).get("_goldSource")
            if gold_src in ("sgp", "estimate", "live"):
                self.gold_source = gold_src

            history = self.history
            history["t"].append(game_time)
            history["winProb"].append(predictions["win"])
            history["killDiff"].append(float(features["killDiff"]))
            history["csDiff"].append(float(features["csDiff"]))
            history["levelSumDiff"].append(float(features["levelSumDiff"]))
            history["ownKill30"].append(float(self.event_probs.get("own_kill_30s", 0.0)))
            history["enemyKill30"].append(float(self.event_probs.get("enemy_kill_30s", 0.0)))
            history["ownGank60"].append(float(self.event_probs.get("own_gank_60s", 0.0)))
            history["enemyGank60"].append(float(self.event_probs.get("enemy_gank_60s", 0.0)))
            for key in history:
                if len(history[key]) > HISTORY_LIMIT:
                    del history[key][: len(history[key]) - HISTORY_LIMIT]

    def as_json(self) -> Dict[str, Any]:
        with self.lock:
            llm_state: Dict[str, Any] = {"enabled": False, "lastAt": None, "error": None, "tips": []}
            if self.llm is not None:
                snap = self.llm.snapshot()
                llm_state = {"enabled": True, **snap}
            advice_items = [{"source": "rule", "text": t} for t in self.rule_tips]
            advice_items += [{"source": "llm", "text": t} for t in llm_state["tips"]]
            rank_block = None
            if "rankDiff" in self.features:
                rank_block = {
                    "own": float(self.features.get("ownRank") or 0),
                    "team": float(self.features.get("teamRank") or 0),
                    "enemy": float(self.features.get("enemyRank") or 0),
                    "diff": float(self.features.get("rankDiff") or 0),
                    "vsTeam": float(self.features.get("ownRankVsTeam") or 0),
                }
            resolver = self._current_resolver()
            return {
                "ok": not self.waiting,
                "source": self.source,
                "model": self.model_name,
                "rank": rank_block,
                "rankStatus": rank_status_payload(resolver),
                "rankError": rank_error_text(resolver),
                "attribution": self.attribution,
                "waiting": self.waiting,
                "ended": self.ended,
                "clock": self.clock,
                "gameTime": self.game_time,
                "winProb": self.win_prob,
                "ownTeam": self.own_team,
                "selfName": self.self_name,
                "selfChampion": self.self_champion,
                "goldSource": self.gold_source,
                "goldEstimated": self.gold_source == "estimate",
                "own": {
                    "level": float(self.features.get("ownLevel", 0) or 0),
                    "kills": float(self.features.get("ownKills", 0) or 0),
                    "deaths": float(self.features.get("ownDeaths", 0) or 0),
                    "assists": float(self.features.get("ownAssists", 0) or 0),
                    "cs": float(self.features.get("ownCS", 0) or 0),
                    "gold": float(self.features.get("ownGold", 0) or 0),
                },
                "teamDiff": {
                    "kills": float(self.features.get("killDiff", 0) or 0),
                    "deaths": float(self.features.get("deathDiff", 0) or 0),
                    "assists": float(self.features.get("assistDiff", 0) or 0),
                    "cs": float(self.features.get("csDiff", 0) or 0),
                    "level": float(self.features.get("levelSumDiff", 0) or 0),
                    "dragons": float(self.features.get("dragonDiff", 0) or 0),
                    "barons": float(self.features.get("baronDiff", 0) or 0),
                    "heralds": float(self.features.get("heraldDiff", 0) or 0),
                    "grubs": float(self.features.get("grubDiff", 0) or 0),
                    "atakhan": float(self.features.get("atakhanDiff", 0) or 0),
                    "towers": float(self.features.get("towerDiff", 0) or 0),
                    "inhibitors": float(self.features.get("inhibitorDiff", 0) or 0),
                },
                "events": self.event_probs,
                "advice": advice_items,
                "feed": self.feed[-FEED_LIMIT:],
                "history": self.history,
                "llm": llm_state,
            }


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Advisor/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._serve_dashboard()
        elif self.path == "/api/state":
            self._serve_state()
        elif self.path == "/design-system.css":
            self._serve_file(DESIGN_CSS, "text/css; charset=utf-8")
        elif self.path == "/demo-data.js":
            self._serve_file(DEMO_JS, "application/javascript; charset=utf-8")
        else:
            self.send_error(404)

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            payload = path.read_bytes()
        except Exception:  # noqa: BLE001
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_dashboard(self) -> None:
        try:
            html = DASHBOARD_HTML.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            html = "<html><body><h1>dashboard.html 缺失</h1></body></html>"
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_state(self) -> None:
        state = self.server.advisor_state.as_json()  # type: ignore[attr-defined]
        payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


class AdvisorHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state: State):
        super().__init__(address, DashboardHandler)
        self.advisor_state = state


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------
def tick(
    payload: Dict[str, Any],
    models: ModelSet,
    feed_builder: FeedBuilder,
    state: State,
    llm: Optional[LlmWorker],
    llm_interval: float,
    quiet: bool,
    opgg: Optional[Dict[str, Any]] = None,
    ranks: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    features = extract_features(payload, opgg, ranks)
    predictions = models.predict(features)
    event_probs: Optional[Dict[str, float]] = None
    if any(label in predictions for label in EVENT_LABELS):
        event_probs = {label: predictions[label] for label in EVENT_LABELS}
    game_time = float((payload.get("gameData") or {}).get("gameTime") or 0)
    if event_probs:
        if state.alert_gate is None:
            state.alert_gate = EventAlertGate.from_policy(models.event_policy)
        tips = rule_advice(
            features,
            predictions["win"],
            event_probs,
            alert_gate=state.alert_gate,
            game_time=game_time,
        )
    else:
        tips = rule_advice(features, predictions["win"], None)

    # T49：胜率归因 top3（反事实：逐特征置回中性值重预测；失败时归 None，不影响主流程）
    attribution: Optional[List[Dict[str, Any]]] = None
    try:
        win_row = feature_row(features, models.win.feature_names)
        top3 = attribute_win_prob(models.win, win_row, models.win.feature_names)
        attribution = [
            {
                "key": item["key"],
                "nameCn": FEATURE_CN.get(item["key"], item["key"]),
                "delta": float(item["delta"]),
                "pp": float(item.get("pp", round(float(item["delta"]) * 100.0, 2))),
            }
            for item in top3
        ]
    except Exception:  # noqa: BLE001
        attribution = None
    state.set_attribution(attribution)

    game_time = float((payload.get("gameData") or {}).get("gameTime") or 0)
    own_team = (payload.get("activePlayer") or {}).get("summonerName")
    active_team = None
    active_champion = ""
    for player in payload.get("allPlayers") or []:
        name = player.get("riotId") or player.get("summonerName") or ""
        if name == own_team or name.split("#")[0] == (own_team or "").split("#")[0]:
            active_team = player.get("team")
            active_champion = champion_display(player)
            break

    feed = feed_builder.build(payload, active_team, game_time)
    state.update(
        payload, predictions, features, tips, feed, active_team,
        own_team or "", self_champion=active_champion,
    )

    if llm is not None and llm_interval > 0:
        snap = llm.snapshot()
        last_at = snap.get("lastAt")
        if last_at is None or game_time - float(last_at) >= llm_interval:
            gold_src = (payload.get("activePlayer") or {}).get("_goldSource")
            gold_label = (
                "兜里金币" if gold_src in ("sgp", "live") else "金币(装备估算)"
            )
            prompt = build_llm_prompt(
                state.clock, predictions["win"], features, event_probs, feed,
                gold_label=gold_label,
            )
            llm.request(game_time, prompt)

    if not quiet:
        evt = ""
        if event_probs:
            evt = (
                f" | 击杀30s 我{event_probs['own_kill_30s']:.0%}/敌{event_probs['enemy_kill_30s']:.0%}"
                f" Gank60s 我{event_probs['own_gank_60s']:.0%}/敌{event_probs['enemy_gank_60s']:.0%}"
            )
        print(
            f"[{state.clock}] 胜率 {predictions['win']:.1%}"
            f" | 击杀差 {int(features['killDiff']):+d}"
            f" 补刀差 {int(features['csDiff']):+d}"
            f" 兜里金币 {int(features['ownGold'])}{evt}"
        )
    return predictions, features


def run(args: argparse.Namespace) -> int:
    model_dir = Path(args.model_dir)
    event_model_dir = Path(args.event_model_dir) if args.event_model_dir else None
    if event_model_dir is None:
        candidate = model_dir.parent / "live_event_model"
        event_model_dir = candidate if candidate.is_dir() else None

    auto_model = str(args.model_dir) == str(DEFAULT_MODEL_DIR)
    model3_dir = DEFAULT_MODEL_DIR.parent / "live_model3"
    model5_dir = DEFAULT_MODEL_DIR.parent / "live_model5"

    resolver: Optional[RankResolver] = None
    ranks: Dict[str, float] = {}
    ranks_done = False
    if not args.no_ranks:
        found = discover_lcu_psutil()
        if found:
            try:
                resolver = RankResolver(found[0], found[1], cache_path=args.rank_cache)
                print(f"段位解析: LCU 已连接（端口 {found[0]}），优先使用 live_model5")
            except Exception as error:  # noqa: BLE001
                print(f"段位解析不可用: {error}")
        else:
            print("段位解析: 找不到 LCU（客户端没开？），使用不带段位特征的模型")

    if auto_model and resolver is not None and model5_dir.is_dir():
        model_dir = model5_dir
    elif auto_model and model3_dir.is_dir():
        model_dir = model3_dir
    rank_ready_model = model_dir.name == "live_model5"

    models = ModelSet(model_dir, event_model_dir)
    print(f"胜率模型: {model_dir} ({models.win.num_trees} 棵树)")
    if rank_ready_model and resolver is None:
        print("提示: live_model5 需要段位特征，但 LCU 不可用——预测会退化成中性值")
    if models.event_models:
        print(f"事件模型: {event_model_dir} ({len(models.event_models)} 个)")
    else:
        print("事件模型: 未找到（建议提示会少一些）")

    opgg: Optional[Dict[str, Any]] = None
    opgg_path = Path(__file__).resolve().parent / "opgg_stats.json"
    if opgg_path.is_file():
        try:
            opgg = json.loads(opgg_path.read_text(encoding="utf-8"))
            print(
                f"OP.GG 特征: patch {opgg.get('patch')} region {opgg.get('region')} "
                f"（英雄/对位胜率已并入模型特征）"
            )
        except Exception as error:  # noqa: BLE001
            print(f"OP.GG 数据读取失败（英雄/对位特征用中性 0.5）: {error}")
    else:
        print("OP.GG 数据未找到（英雄/对位特征用中性 0.5；运行 opgg_scrape.py 生成）")

    if opgg:
        for warning in validate_opgg_stats(opgg):
            print(f"OP.GG 告警: {warning}")

    llm: Optional[LlmWorker] = None
    api_key = args.llm_api_key
    if args.no_llm:
        if args.llm:
            print("--llm 与 --no-llm 同时给出，LLM 层已关闭")
    elif api_key:
        llm = LlmWorker(args.llm_base_url, api_key, args.llm_model, args.llm_interval)
        llm.start()
        source_text = {
            "flag": "--llm-api-key 参数",
            "env": "环境变量 ADVISOR_LLM_API_KEY",
            "file": "advisor.llm.env",
        }.get(args.llm_key_source, "未知来源")
        if not args.llm:
            print("已自动启用 LLM 层（检测到 API key，--no-llm 可关闭）")
        print(
            f"LLM 层: {args.llm_model} @ {args.llm_base_url}"
            f" · key {mask_key(api_key)}（来源: {source_text}）"
            f" · 每 {args.llm_interval}s 一次"
        )
    elif args.llm:
        print(
            "--llm 需要 API key（--llm-api-key、环境变量 ADVISOR_LLM_API_KEY "
            "或 advisor.llm.env），已关闭 LLM 层"
        )

    source: Optional[SnapshotSource] = None
    live_replay_player: Optional[str] = None
    gold_bridge: Optional[SgpGoldBridge] = None
    gold_for = None
    if args.snapshots:
        source = SnapshotSource(Path(args.snapshots), args.player, args.speed)
        state = State("replay", llm, replay_self=source.self_player)
        if llm is not None:
            print(
                f"提示: 回放模式下 LLM 实际调用频率约为每 "
                f"{args.llm_interval / args.speed:.2f} 秒一次（对局内间隔/speed）"
            )
    elif args.player:
        # 直播回放模式：正在播放的回放没有 activePlayer，用 --player 注入视角
        live_replay_player = args.player
        state = State("replay", llm)
        print(f"直播回放模式: 等待 2999（视角 = --player {args.player}）")

        if args.game_id:
            lcu_port, lcu_token = args.lcu_port, args.lcu_token
            if not (lcu_port and lcu_token):
                discovered = discover_lcu()
                if discovered:
                    lcu_port, lcu_token = discovered
            if lcu_port and lcu_token:
                try:
                    gold_bridge = SgpGoldBridge(
                        lcu_port, lcu_token, args.game_id
                    )
                    gold_for = gold_bridge.pocket_gold
                    print(
                        f"金币桥已启用: {gold_bridge.describe()}"
                        f"（回放金币用 SGP 1 分钟口袋金币）"
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"金币桥不可用（回退装备估算）: {error}")
            else:
                print(
                    "金币桥不可用（缺 --lcu-port/--lcu-token 且未找到 lockfile），"
                    "回退装备估算"
                )
    else:
        state = State("live", llm)

    state.resolver = resolver
    state.model_name = model_dir.name

    server = AdvisorHttpServer((args.host, args.port), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"仪表盘: http://{args.host}:{args.port}")

    feed_builder = FeedBuilder()
    processed = 0
    last_game_time = 0.0

    try:
        while True:
            payload: Optional[Dict[str, Any]] = None
            if source is not None:
                payload = source.next_payload()
                if payload is None and source.ended:
                    state.ended = True
                    print("回放结束，服务器继续提供最后状态，Ctrl+C 退出。")
                    while True:
                        time.sleep(1)
            else:
                payload = fetch_payload()
                if payload is not None and live_replay_player:
                    try:
                        payload, self_player = patch_replay_self(
                            payload, live_replay_player, gold_for=gold_for
                        )
                        if not state.self_name:
                            state.set_self(self_player)
                    except ValueError as error:
                        print(f"找不到视角: {error}")
                        payload = None
                        if args.once:
                            return 1

            if payload is not None:
                game_time = float((payload.get("gameData") or {}).get("gameTime") or 0)
                if game_time + 300 < last_game_time:
                    # 新的一局：段位要重新解析（换人了）
                    ranks = {}
                    ranks_done = False
                last_game_time = max(last_game_time, game_time)

                if (
                    resolver is not None
                    and not ranks_done
                    and len(payload.get("allPlayers") or []) >= 10
                ):
                    ranks = resolver.resolve_payload(payload)
                    ranks_done = True
                    if 0 < len(ranks) < 10:
                        filled = complete_payload_ranks(payload, ranks)
                        if filled:
                            print(
                                f"段位: 直接查到 {len(ranks)} 人，"
                                f"其余 {len(filled) - len(ranks)} 人按队伍均值补齐"
                            )
                            ranks = filled
                    if ranks:
                        print(f"段位已解析（{len(ranks)} 人，缓存 {resolver.queries} 次查询）")
                        if state.model_name:
                            print(f"胜率模型: {state.model_name}")
                    else:
                        print("段位解析不完整，回退到 live_model3（不带段位特征）")
                        if auto_model and model_dir.name == "live_model5" and model3_dir.is_dir():
                            model_dir = model3_dir
                            models = ModelSet(model_dir, event_model_dir)
                            state.model_name = model_dir.name

                try:
                    active = payload.get("activePlayer") or {}
                    if not active.get("summonerName") or active.get("error"):
                        # 观战/回放没有 activePlayer：挑一个视角，否则后面全部取不到数据
                        fragment = None
                        if resolver is not None:
                            fragment = resolver.current_summoner()
                        players = payload.get("allPlayers") or []
                        if not fragment and players:
                            fragment = players[0].get("riotId") or players[0].get("summonerName")
                        if fragment:
                            patched, self_player = patch_replay_self(payload, fragment)
                            payload = patched
                            state.source = "spectator"
                            state.set_self(self_player)
                            print(f"观战视角: {self_player.get('riotId')}（{self_player.get('team')}，金币按装备估算）")
                except ValueError as error:
                    print(f"观战视角解析失败: {error}")

                try:
                    _predictions, tick_features = tick(
                        payload, models, feed_builder, state, llm,
                        args.llm_interval, args.quiet, opgg, ranks or None,
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"解析失败: {error}")
                    if args.once:
                        return 1
                else:
                    # 段位已提供但 10 人没凑齐（partial ranks）：
                    # live_model5 的 rank 特征不全，强制回退 live_model3。
                    if (
                        rank_fallback_required(tick_features)
                        and model_dir.name == "live_model5"
                        and model3_dir.is_dir()
                    ):
                        model_dir = model3_dir
                        models = ModelSet(model_dir, event_model_dir)
                        state.model_name = model_dir.name
                        print("段位解析不完整，回退到 live_model3（不带段位特征）")
                    processed += 1
                    if args.once or (args.ticks and processed >= args.ticks):
                        break

            if source is not None:
                time.sleep(0.1)
            else:
                if payload is None:
                    state.waiting = True
                    print("等待对局（2999 未响应）...")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        if resolver is not None:
            resolver.save()
        server.shutdown()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live 实时助手（Web 仪表盘 + LLM 建议层）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--event-model-dir", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=5.0, help="实时模式轮询间隔（秒）")
    parser.add_argument("--snapshots", type=Path, default=None, help="回放模式：snapshots.jsonl")
    parser.add_argument("--player", default="", help="回放视角：自己的 riotId/英雄名片段（快照回放或正在播放的回放）")
    parser.add_argument("--speed", type=float, default=10.0, help="回放模式：回放速度倍数")
    parser.add_argument("--game-id", default=None, help="直播回放的金币桥：对局 ID（配合 SGP 补口袋金币）")
    parser.add_argument("--lcu-port", type=int, default=None, help="金币桥：LCU 端口（默认自动读 lockfile）")
    parser.add_argument("--lcu-token", default=None, help="金币桥：LCU remoting token（默认自动读 lockfile）")
    parser.add_argument("--llm", action="store_true", help="启用 LLM 建议层")
    parser.add_argument("--no-llm", action="store_true", help="关闭 LLM 层（覆盖自动启用）")
    parser.add_argument("--llm-base-url", default="https://api.deepseek.com")
    parser.add_argument("--llm-api-key", default=None)
    parser.add_argument("--llm-model", default="deepseek-flash")
    parser.add_argument("--llm-interval", type=float, default=20.0, help="LLM 调用间隔（对局秒）")
    parser.add_argument("--once", action="store_true", help="处理一个快照后退出")
    parser.add_argument("--ticks", type=int, default=0, help="处理 N 个 tick 后退出")
    parser.add_argument(
        "--no-ranks",
        action="store_true",
        help="关闭段位解析（默认开：需要客户端在线；关闭后只用不带段位特征的模型）",
    )
    parser.add_argument(
        "--rank-cache",
        type=Path,
        default=Path(__file__).resolve().parent / "rank_cache.json",
        help="段位缓存文件（默认 6 小时有效）",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # 模型选择在 run() 里做：能解析段位就用 live_model5，否则 live_model3

    if args.llm_base_url:
        args.llm_base_url = args.llm_base_url or "https://api.deepseek.com"

    # key 解析顺序：命令行 > 环境变量 > advisor.llm.env 文件
    file_config = load_env_file(LLM_ENV_FILE)
    args.llm_key_source = "flag"
    if not args.llm_api_key:
        args.llm_api_key = os.environ.get("ADVISOR_LLM_API_KEY") or ""
        if args.llm_api_key:
            args.llm_key_source = "env"
    if not args.llm_api_key and file_config.get("ADVISOR_LLM_API_KEY"):
        args.llm_api_key = file_config["ADVISOR_LLM_API_KEY"]
        args.llm_key_source = "file"
    args.llm_base_url = (
        os.environ.get("ADVISOR_LLM_BASE_URL")
        or args.llm_base_url
        or file_config.get("ADVISOR_LLM_BASE_URL", "")
        or "https://api.deepseek.com"
    )
    args.llm_model = (
        os.environ.get("ADVISOR_LLM_MODEL")
        or args.llm_model
        or file_config.get("ADVISOR_LLM_MODEL", "")
        or "deepseek-flash"
    )

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
