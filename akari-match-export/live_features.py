#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stdlib-only feature extraction for the live advisor.

Canonical copy of the helpers that used to live inside live_advisor.py, so
advisor_server.py (which must not import lightgbm/pandas/requests) can share
them. live_advisor.py remains the legacy console entry point.

Feature order and semantics follow live_dataset.csv / live_model2 exactly.
"""

from __future__ import annotations

import bisect
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
EVENT_LABELS = (
    "own_kill_30s",
    "enemy_kill_30s",
    "own_gank_60s",
    "enemy_gank_60s",
)

# 事件建议阈值：训练时 OOF 预测（GroupKFold 5, gameId）的 precision_recall_curve
# argmax F1（不是 0.5）。来源与审计见
# dataset/live_dataset/live_event_model/event_thresholds.json
# （由 compute_event_thresholds.py 生成，值取自 live_event_model/metrics.json）。
DEFAULT_EVENT_THRESHOLDS = {
    "own_kill_30s": 0.352111,
    "enemy_kill_30s": 0.350137,
    "own_gank_60s": 0.391506,
    "enemy_gank_60s": 0.389624,
}

# 事件提醒策略（T60）：阈值只决定「概率够不够高」，这里再管「该不该真的弹出来」。
# - minGap：要比对方同一类事件高出多少才提醒（避免双方都 0.36 时来回弹）
# - confirmations：连续多少次刷新都满足才提醒（5 秒一次 → 2 次约 10 秒确认）
# - cooldownSeconds：同一类提醒的最小间隔（防刷屏）
DEFAULT_EVENT_POLICY = {
    "minGap": 0.10,
    "confirmations": 2,
    "cooldownSeconds": 180.0,
    "alertBudgetPerGame": 8,
}


def load_event_policy(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 event_thresholds.json 的阈值与 policy；文件缺失/损坏时回退默认值。"""
    policy: Dict[str, Any] = {
        "thresholds": dict(DEFAULT_EVENT_THRESHOLDS),
        **DEFAULT_EVENT_POLICY,
    }
    if path is None:
        return policy
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return policy
    for label, item in (data.get("labels") or {}).items():
        try:
            value = float(item.get("threshold"))
        except (TypeError, ValueError):
            continue
        if 0.0 < value < 1.0:
            policy["thresholds"][label] = value
    block = data.get("policy") or {}
    for key in ("minGap", "confirmations", "cooldownSeconds", "alertBudgetPerGame"):
        if key in block:
            try:
                policy[key] = float(block[key]) if key != "confirmations" else int(block[key])
            except (TypeError, ValueError):
                pass
    return policy


class EventAlertGate:
    """事件提醒闸门：阈值 + 最小差 + 连续确认 + 冷却，避免「大半时间都在报警」。

    advisor_server 每轮刷新调用 update()，只对返回 True 的事件出建议；
    同一局内持续有效，检测到 game_time 回退（新的一局）会自动重置。
    """

    def __init__(
        self,
        thresholds: Optional[Dict[str, float]] = None,
        min_gap: float = 0.10,
        confirmations: int = 2,
        cooldown_seconds: float = 180.0,
    ):
        self.thresholds = dict(thresholds or DEFAULT_EVENT_THRESHOLDS)
        self.min_gap = float(min_gap)
        self.confirmations = max(1, int(confirmations))
        self.cooldown = float(cooldown_seconds)
        self._streak: Dict[str, int] = {}
        self._last_fire: Dict[str, float] = {}
        self._last_time: Optional[float] = None

    @classmethod
    def from_policy(cls, policy: Dict[str, Any]) -> "EventAlertGate":
        return cls(
            thresholds=policy.get("thresholds"),
            min_gap=float(policy.get("minGap", 0.10)),
            confirmations=int(policy.get("confirmations", 2)),
            cooldown_seconds=float(policy.get("cooldownSeconds", 180.0)),
        )

    def reset(self) -> None:
        self._streak.clear()
        self._last_fire.clear()
        self._last_time = None

    def update(
        self, event_probabilities: Dict[str, float], game_time: float
    ) -> Dict[str, bool]:
        moment = float(game_time or 0.0)
        if self._last_time is not None and moment + 1.0 < self._last_time:
            self.reset()  # 对局时间回退 = 新的一局
        self._last_time = moment
        pairs = {
            "own_kill_30s": "enemy_kill_30s",
            "enemy_kill_30s": "own_kill_30s",
            "own_gank_60s": "enemy_gank_60s",
            "enemy_gank_60s": "own_gank_60s",
        }
        fired: Dict[str, bool] = {}
        for label, other in pairs.items():
            probability = float(event_probabilities.get(label) or 0.0)
            opponent = float(event_probabilities.get(other) or 0.0)
            threshold = float(self.thresholds.get(label, 0.5))
            qualifies = probability >= threshold and probability >= opponent + self.min_gap
            self._streak[label] = self._streak.get(label, 0) + 1 if qualifies else 0
            last = self._last_fire.get(label)
            cooled = last is None or (moment - last) >= self.cooldown
            if qualifies and self._streak[label] >= self.confirmations and cooled:
                fired[label] = True
                self._last_fire[label] = moment
            else:
                fired[label] = False
        return fired

POSITION_MAP = {
    "TOP": "top",
    "JUNGLE": "jungle",
    "MIDDLE": "mid",
    "BOTTOM": "adc",
    "UTILITY": "support",
}
_ALIASES = {"monkeyking": "wukong", "nunuwillump": "nunu", "renataglasc": "renata"}

# 事件时序特征（口径与训练侧 build_live_dataset.py 的 COLUMNS 完全一致）。
RECENCY_SINCE_FEATURES = (
    "since_kill",
    "since_own_kill",
    "since_enemy_kill",
    "since_gank",
    "since_own_gank",
    "since_enemy_gank",
    "since_dragon",
    "since_baron",
    "since_tower",
)
RECENCY_COUNT_FEATURES = (
    "kills_last_60",
    "kills_last_180",
    "own_kills_last_180",
    "enemy_kills_last_180",
    "ganks_last_300",
    "own_ganks_last_300",
    "enemy_ganks_last_300",
)

# 段位特征缺省中性值：与训练侧 build_live_dataset_v5 的 DEFAULT_RANK=4000 口径一致。
# 注意差值类特征（rankDiff/ownRankVsTeam）的中性值是 0 而不是 4000：
# 训练数据里缺人时 rankDiff=0，填 4000 会注入极端离群值。
RANK_FEATURE_DEFAULTS = {
    "ownRank": 4000.0,
    "teamRank": 4000.0,
    "enemyRank": 4000.0,
    "rankDiff": 0.0,
    "ownRankVsTeam": 0.0,
}


def normalize_champ(name: str) -> str:
    """英雄名归一化（OP.GG slug 与 SGP championName 对齐），纯 ASCII 键。"""
    normalized = "".join(ch for ch in str(name).lower() if ch.isalnum())
    return _ALIASES.get(normalized, normalized)


def champ_name(player: Dict[str, Any]) -> str:
    raw = str(player.get("rawChampionName") or "")
    if raw.startswith("game_character_displayname_"):
        return raw.split("game_character_displayname_", 1)[1]
    return str(player.get("championName") or player.get("champion") or "")


def find_active_player(
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    active = payload.get("activePlayer") or {}
    players = payload.get("allPlayers") or []

    active_name = active.get("summonerName") or active.get("riotId") or ""
    normalized = active_name.split("#")[0]

    for player in players:
        name = player.get("summonerName") or player.get("riotId") or ""
        if name == active_name or name.split("#")[0] == normalized:
            return active, player

    return active, None


def team_totals(players: List[Dict[str, Any]], team: str) -> Dict[str, float]:
    result = {
        "kills": 0.0,
        "deaths": 0.0,
        "assists": 0.0,
        "cs": 0.0,
        "level": 0.0,
    }

    for player in players:
        if player.get("team") != team:
            continue
        scores = player.get("scores") or {}
        result["kills"] += float(scores.get("kills") or 0)
        result["deaths"] += float(scores.get("deaths") or 0)
        result["assists"] += float(scores.get("assists") or 0)
        result["cs"] += float(scores.get("creepScore") or 0)
        result["level"] += float(player.get("level") or 0)

    return result


def team_of_name(
    players: List[Dict[str, Any]], name: Optional[str]
) -> Optional[str]:
    """事件里的名字 → 队伍。

    Live Client Data 的 KillerName/Assisters 是**英雄名**；快照回放
    （replay_collector）里是 riotId（可能没有 #TAG）。两种都要匹配：
    先比召唤师名（含去 #TAG 的 base 名），再比英雄名。
    """
    if not name:
        return None
    key = normalize_champ(name)
    base = name.split("#")[0]
    for player in players:
        team = player.get("team")
        if team is None:
            continue
        player_name = str(player.get("riotId") or player.get("summonerName") or "")
        if player_name and (
            player_name == name
            or player_name.split("#")[0] == base
            or normalize_champ(player_name) == key
        ):
            return team
        champion = champ_name(player)
        if champion and normalize_champ(champion) == key:
            return team
    return None


def player_team_by_name(
    players: List[Dict[str, Any]], name: Optional[str]
) -> Optional[str]:
    return team_of_name(players, name)


def event_name(event: Dict[str, Any]) -> str:
    """Live allgamedata uses `eventName`; replay /eventdata uses `EventName`."""
    return str(event.get("eventName") or event.get("EventName") or "")


def event_killer(event: Dict[str, Any]) -> Optional[str]:
    return event.get("KillerName") or event.get("killerName")


def event_seconds(event: Dict[str, Any]) -> float:
    """事件时间统一成秒：LCD allgamedata 给秒，回放 /eventdata 给毫秒。"""
    value = float(event.get("EventTime") or 0)
    return value / 1000.0 if value > 1e6 else value


def _since(times: List[float], timestamp: float) -> float:
    """距最近一次该类事件的秒数；从未发生返回 9999（与训练侧一致）。"""
    index = bisect.bisect_right(times, timestamp) - 1
    return 9999.0 if index < 0 else timestamp - times[index]


def _count_between(times: List[float], start: float, end: float) -> int:
    """(start, end] 内的事件数（与训练侧 count_between 一致）。"""
    return bisect.bisect_right(times, end) - bisect.bisect_right(times, start)


def event_recency_features(
    payload: Dict[str, Any],
    players: List[Dict[str, Any]],
    own_team: Optional[str],
    enemy_team: Optional[str],
    timestamp: float,
) -> Dict[str, float]:
    """用 LCD events.Events（整局累积）算 16 个事件时序特征。

    口径与训练侧 build_live_dataset.py 完全一致：
    - since_*：距最近一次该类事件的秒数，0=刚发生、9999=从未发生；
    - *_last_*：(t-窗口, t] 内的事件次数；
    - 击杀按 KillerName 归队（召唤师名/英雄名都匹配）；gank = 击杀且某队
      打野在 KillerName/Assisters 中（打野身份取 allPlayers 的 position）。
    position 缺失（老快照为 NONE）时无法识别打野，gank 特征退化为
    since=9999 / count=0（与训练中"尚未发生 gank"的中性态一致）。
    """
    events = (payload.get("events") or {}).get("Events") or []
    jungler_champs: Dict[Any, set] = {own_team: set(), enemy_team: set()}
    for player in players:
        team = player.get("team")
        if team not in jungler_champs:
            continue
        role = str(player.get("position") or player.get("teamPosition") or "").upper()
        if role == "JUNGLE":
            jungler_key = normalize_champ(champ_name(player) or "")
            if jungler_key:
                jungler_champs[team].add(jungler_key)

    own_kill: List[float] = []
    enemy_kill: List[float] = []
    own_gank: List[float] = []
    enemy_gank: List[float] = []
    dragon: List[float] = []
    baron: List[float] = []
    tower: List[float] = []

    for event in events:
        name = event_name(event)
        time_value = float(event.get("EventTime") or 0)
        if name == "ChampionKill":
            team = team_of_name(players, event_killer(event))
            if team == own_team:
                own_kill.append(time_value)
            elif team == enemy_team:
                enemy_kill.append(time_value)
            involved = {normalize_champ(event_killer(event) or "")}
            for assist in event.get("Assisters") or []:
                involved.add(normalize_champ(assist or ""))
            if involved & jungler_champs.get(own_team, set()):
                own_gank.append(time_value)
            if involved & jungler_champs.get(enemy_team, set()):
                enemy_gank.append(time_value)
        elif name == "DragonKill":
            dragon.append(time_value)
        elif name == "BaronKill":
            baron.append(time_value)
        elif name == "TurretKilled":
            tower.append(time_value)

    for bucket in (own_kill, enemy_kill, own_gank, enemy_gank, dragon, baron, tower):
        bucket.sort()
    kill_all = sorted(own_kill + enemy_kill)
    gank_all = sorted(own_gank + enemy_gank)

    return {
        "since_kill": _since(kill_all, timestamp),
        "since_own_kill": _since(own_kill, timestamp),
        "since_enemy_kill": _since(enemy_kill, timestamp),
        "since_gank": _since(gank_all, timestamp),
        "since_own_gank": _since(own_gank, timestamp),
        "since_enemy_gank": _since(enemy_gank, timestamp),
        "since_dragon": _since(dragon, timestamp),
        "since_baron": _since(baron, timestamp),
        "since_tower": _since(tower, timestamp),
        "kills_last_60": _count_between(kill_all, timestamp - 60, timestamp),
        "kills_last_180": _count_between(kill_all, timestamp - 180, timestamp),
        "own_kills_last_180": _count_between(own_kill, timestamp - 180, timestamp),
        "enemy_kills_last_180": _count_between(enemy_kill, timestamp - 180, timestamp),
        "ganks_last_300": _count_between(gank_all, timestamp - 300, timestamp),
        "own_ganks_last_300": _count_between(own_gank, timestamp - 300, timestamp),
        "enemy_ganks_last_300": _count_between(enemy_gank, timestamp - 300, timestamp),
    }


def objective_counts(
    payload: Dict[str, Any], timestamp: Optional[float] = None
) -> Dict[str, Dict[str, int]]:
    """按队伍统计目标物。

    T61：`timestamp`（对局秒数）给出时只统计**已经发生**的事件。LCD 实时只推
    已发生事件，但回放/离线批跑会把整局事件表一起喂进来，不截断就会「第 5 分钟
    看到大龙」——这是训练/推理口径不一致的隐患。
    """
    players = payload.get("allPlayers") or []
    events = (payload.get("events") or {}).get("Events") or []
    counts = {
        "ORDER": {
            "dragons": 0,
            "barons": 0,
            "heralds": 0,
            "grubs": 0,
            "atakhans": 0,
            "towers": 0,
            "inhibitors": 0,
        },
        "CHAOS": {
            "dragons": 0,
            "barons": 0,
            "heralds": 0,
            "grubs": 0,
            "atakhans": 0,
            "towers": 0,
            "inhibitors": 0,
        },
    }

    mapping = {
        "DragonKill": "dragons",
        "BaronKill": "barons",
        "HeraldKill": "heralds",
        "HordeKill": "grubs",
        "AtakhanKill": "atakhans",
        "TurretKilled": "towers",
        "InhibKilled": "inhibitors",
    }

    for event in events:
        metric = mapping.get(event_name(event))
        if not metric:
            continue
        if timestamp is not None and event_seconds(event) > float(timestamp) + 1e-6:
            continue  # 未来的事件不计入（回放/批跑安全）
        killer = event_killer(event)
        team = player_team_by_name(players, killer)
        if team in counts:
            counts[team][metric] += 1

    return counts


def extract_features(
    payload: Dict[str, Any],
    opgg: Optional[Dict[str, Any]] = None,
    ranks: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    game = payload.get("gameData") or {}
    game_time = float(game.get("gameTime") or 0)
    minute = max(1, int(game_time // 60))

    active, active_player = find_active_player(payload)
    if active_player is None:
        raise RuntimeError("找不到 activePlayer 对应的 allPlayers 记录")

    players = payload.get("allPlayers") or []
    own_team = active_player.get("team")
    enemy_team = "CHAOS" if own_team == "ORDER" else "ORDER"

    own_scores = active_player.get("scores") or {}
    own = team_totals(players, own_team)
    enemy = team_totals(players, enemy_team)
    objectives = objective_counts(payload, game_time)
    own_objectives = objectives.get(own_team, {})
    enemy_objectives = objectives.get(enemy_team, {})

    role = active_player.get("position") or active_player.get("teamPosition") or ""
    if role not in ROLES:
        role = ""

    result = {
        "minute": minute,
        "role": role,
        "ownLevel": float(active.get("level") or active_player.get("level") or 0),
        "ownGold": float(active.get("currentGold") or 0),
        "ownCS": float(own_scores.get("creepScore") or 0),
        "ownKills": float(own_scores.get("kills") or 0),
        "ownDeaths": float(own_scores.get("deaths") or 0),
        "ownAssists": float(own_scores.get("assists") or 0),
        "teamKills": own["kills"],
        "teamDeaths": own["deaths"],
        "teamAssists": own["assists"],
        "teamCS": own["cs"],
        "teamLevelSum": own["level"],
        "enemyKills": enemy["kills"],
        "enemyDeaths": enemy["deaths"],
        "enemyAssists": enemy["assists"],
        "enemyCS": enemy["cs"],
        "enemyLevelSum": enemy["level"],
        "killDiff": own["kills"] - enemy["kills"],
        "deathDiff": enemy["deaths"] - own["deaths"],
        "assistDiff": own["assists"] - enemy["assists"],
        "csDiff": own["cs"] - enemy["cs"],
        "levelSumDiff": own["level"] - enemy["level"],
        "dragonDiff": own_objectives.get("dragons", 0)
        - enemy_objectives.get("dragons", 0),
        "baronDiff": own_objectives.get("barons", 0)
        - enemy_objectives.get("barons", 0),
        "heraldDiff": own_objectives.get("heralds", 0)
        - enemy_objectives.get("heralds", 0),
        "grubDiff": own_objectives.get("grubs", 0)
        - enemy_objectives.get("grubs", 0),
        "atakhanDiff": own_objectives.get("atakhans", 0)
        - enemy_objectives.get("atakhans", 0),
        "towerDiff": own_objectives.get("towers", 0)
        - enemy_objectives.get("towers", 0),
        "inhibitorDiff": own_objectives.get("inhibitors", 0)
        - enemy_objectives.get("inhibitors", 0),
    }

    # ---- OP.GG 英雄/对位胜率特征（训练与推理必须用同一套查表逻辑）----
    stats = (opgg or {}).get("stats") or {}
    matchups = (opgg or {}).get("matchups") or {}

    def _champ_wr(pos: str, champion: str) -> float:
        entry = stats.get(pos, {}).get(normalize_champ(champion))
        return float(entry["winRate"]) if entry else 0.5

    def _matchup(pos: str, own_champion: str, enemy_champion: str):
        return matchups.get(pos, {}).get(normalize_champ(own_champion), {}).get(
            normalize_champ(enemy_champion)
        )

    pos = POSITION_MAP.get(role)
    own_champion = champ_name(active_player) or ""
    enemy_champion = ""
    if pos:
        for player in players:
            if player.get("team") != own_team:
                player_role = (
                    player.get("position") or player.get("teamPosition") or ""
                )
                if player_role == role:
                    enemy_champion = champ_name(player) or ""
                    break

    result["own_champ_wr"] = (
        _champ_wr(pos, own_champion) if pos and own_champion else 0.5
    )
    result["enemy_champ_wr"] = (
        _champ_wr(pos, enemy_champion) if pos and enemy_champion else 0.5
    )
    matchup = _matchup(pos, own_champion, enemy_champion) if pos and own_champion and enemy_champion else None
    result["matchup_wr"] = float(matchup["winRate"]) if matchup else 0.5
    result["matchup_games_log"] = float(
        __import__("math").log1p(float(matchup.get("games") or 0))
    ) if matchup else 0.0

    own_team_wr = 0.0
    enemy_team_wr = 0.0
    for player in players:
        player_role = player.get("position") or player.get("teamPosition") or ""
        player_pos = POSITION_MAP.get(player_role)
        if not player_pos:
            continue
        player_champ = champ_name(player)
        if not player_champ:
            continue
        wr = _champ_wr(player_pos, player_champ)
        if player.get("team") == own_team:
            own_team_wr += wr
        else:
            enemy_team_wr += wr
    result["team_wr_diff"] = (own_team_wr - enemy_team_wr) / 5.0

    # ---- 事件时序特征（since_*/last_*，口径与 build_live_dataset.py 一致）----
    result.update(
        event_recency_features(payload, players, own_team, enemy_team, game_time)
    )

    # ---- 段位特征 + 回退信号 ----
    # _rankProvided/_rankComplete 供 rank_fallback_required() 使用：
    # 段位已提供但 10 人没凑齐时，上层（advisor_server）应回退不带段位特征的模型。
    result["_rankProvided"] = bool(ranks)
    result["_rankComplete"] = False
    if ranks:
        rank_features = team_rank_features(players, active_player, ranks)
        if rank_features:
            result.update(rank_features)
            result["_rankComplete"] = True

    return result


ROLE_CODES = {role: index for index, role in enumerate(sorted(ROLES))}


def team_rank_features(
    players: List[Dict[str, Any]],
    active_player: Dict[str, Any],
    ranks: Dict[str, float],
) -> Dict[str, float]:
    """把「每人段位分」折成模型特征；10 人里缺一个就返回空 dict。

    键是 riotId（`Name#TAG`），与 Live Client Data 的 allPlayers 一致；
    也兼容只有名字（没有 #TAG）的写法。
    """

    def score_of(player: Dict[str, Any]) -> Optional[float]:
        name = str(player.get("riotId") or player.get("summonerName") or "")
        if name in ranks:
            return float(ranks[name])
        base = name.split("#")[0]
        if base in ranks:
            return float(ranks[base])
        return None

    own_team = active_player.get("team")
    own: List[float] = []
    enemy: List[float] = []
    for player in players:
        score = score_of(player)
        if score is None:
            continue
        if player.get("team") == own_team:
            own.append(score)
        else:
            enemy.append(score)
    if len(own) != 5 or len(enemy) != 5:
        return {}

    own_rank = score_of(active_player)
    if own_rank is None:
        return {}
    team_rank = sum(own) / 5.0
    enemy_rank = sum(enemy) / 5.0
    return {
        "ownRank": own_rank,
        "teamRank": team_rank,
        "enemyRank": enemy_rank,
        "rankDiff": team_rank - enemy_rank,
        "ownRankVsTeam": own_rank - team_rank,
    }


def feature_row(features: Dict[str, Any], feature_names: List[str]) -> List[float]:
    """Build one numeric row in model feature order.

    role 是分类特征：训练时 pandas astype("category") 按排序后的类别分配
    码（BOTTOM=0, JUNGLE=1, MIDDLE=2, TOP=3, UTILITY=4），推理必须用相同的码。
    v3 模型里有树会用到 role，旧模型不用它（码给错也无影响）。

    其余特征缺失时填中性缺省值：段位特征按训练侧 DEFAULT_RANK 口径
    （ownRank/teamRank/enemyRank=4000，rankDiff/ownRankVsTeam=0），其它填 0。
    """
    row = []
    for column in feature_names:
        if column == "role":
            row.append(float(ROLE_CODES.get(str(features.get(column, "")), 0.0)))
            continue
        value = features.get(column)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = RANK_FEATURE_DEFAULTS.get(column, 0.0)
        row.append(float(value))
    return row


def advice(
    features: Dict[str, Any],
    probability: float,
    event_probabilities: Optional[Dict[str, float]] = None,
    thresholds: Optional[Dict[str, float]] = None,
    alert_gate: Optional["EventAlertGate"] = None,
    game_time: Optional[float] = None,
) -> List[str]:
    """规则建议。

    事件提醒默认走「阈值 + 最小差 + 连续确认 + 冷却」的闸门（T60）；
    不传 alert_gate 时退回旧的单次阈值判断（供测试/离线复盘复用）。
    """
    tips: List[str] = []

    if probability >= 0.6:
        tips.append(f"胜率模型给 {probability:.1%}，优势，保持视野和节奏。")
    elif probability <= 0.4:
        tips.append(f"胜率模型给 {probability:.1%}，劣势，避战发育、等对手失误。")
    else:
        tips.append(f"胜率模型给 {probability:.1%}，均势，先做视野再决策。")

    if features["killDiff"] >= 3:
        tips.append(f"击杀领先 {int(features['killDiff'])}，可以主动找机会。")
    elif features["killDiff"] <= -3:
        tips.append(f"击杀落后 {int(abs(features['killDiff']))}，少接少打。")

    if features["csDiff"] <= -30:
        tips.append("团队补刀落后，注意兵线和经济。")
    if features["levelSumDiff"] <= -4:
        tips.append("团队等级落后，避免正面接团。")

    if features["dragonDiff"] > 0:
        tips.append("小龙领先，继续控龙区视野。")
    elif features["dragonDiff"] < 0:
        tips.append("小龙落后，下一波龙团要提前落位。")

    if features["baronDiff"] < 0:
        tips.append("大龙落后，注意大龙区视野。")

    if event_probabilities:
        if alert_gate is not None:
            fired = alert_gate.update(event_probabilities, float(game_time or 0.0))
            fire_enemy_gank = fired.get("enemy_gank_60s", False)
            fire_own_gank = fired.get("own_gank_60s", False)
            fire_enemy_kill = fired.get("enemy_kill_30s", False)
            fire_own_kill = fired.get("own_kill_30s", False)
        else:
            threshold_map = thresholds or DEFAULT_EVENT_THRESHOLDS
            own_gank = event_probabilities["own_gank_60s"]
            enemy_gank = event_probabilities["enemy_gank_60s"]
            own_kill = event_probabilities["own_kill_30s"]
            enemy_kill = event_probabilities["enemy_kill_30s"]
            fire_enemy_gank = (
                enemy_gank >= threshold_map.get("enemy_gank_60s", 0.5)
                and enemy_gank > own_gank + 0.05
            )
            fire_own_gank = (
                own_gank >= threshold_map.get("own_gank_60s", 0.5)
                and own_gank > enemy_gank + 0.05
            )
            fire_enemy_kill = (
                enemy_kill >= threshold_map.get("enemy_kill_30s", 0.5)
                and enemy_kill > own_kill + 0.05
            )
            fire_own_kill = (
                own_kill >= threshold_map.get("own_kill_30s", 0.5)
                and own_kill > enemy_kill + 0.05
            )

        if fire_enemy_gank:
            tips.append("60 秒内敌方打野更可能来抓，注意后退、补视野。")
        elif fire_own_gank:
            tips.append("60 秒内我方打野更可能来抓，可以找机会。")

        if fire_enemy_kill:
            tips.append("30 秒内敌方更可能拿到击杀，小心被开。")
        elif fire_own_kill:
            tips.append("30 秒内我方更可能拿到击杀，可以接。")

    return tips


def rank_fallback_required(features: Dict[str, Any]) -> bool:
    """段位已提供（_rankProvided）但 10 人没凑齐 → 上层应回退不带段位特征的模型。

    advisor_server 用法：选了 live_model5 且本函数为 True 时，切换回 live_model3；
    段位根本没提供（LCU 不可用）时不走这里，选模型阶段就应选 model3。
    """
    return bool(features.get("_rankProvided")) and not bool(
        features.get("_rankComplete")
    )


def validate_opgg_stats(
    opgg: Optional[Dict[str, Any]],
    current_patch: Optional[str] = None,
    max_age_days: float = 14.0,
) -> List[str]:
    """检查 opgg_stats.json 的新鲜度/版本，返回告警列表（空 = 无问题）。

    加载侧（advisor_server 启动时）调用并打印告警即可；current_patch 形如
    "16.22"，取不到（Live Client Data 不提供 gameVersion）时至少检查
    fetchedAt 的年龄。
    """
    warnings: List[str] = []
    if not opgg:
        warnings.append("opgg_stats.json 缺失或为空，英雄/对位特征将用中性 0.5")
        return warnings

    patch = str(opgg.get("patch") or "")
    if not patch:
        warnings.append("opgg_stats.json 缺少 patch 字段")
    elif current_patch and patch != current_patch:
        warnings.append(
            f"OP.GG 数据 patch={patch} 与当前对局 {current_patch} 不一致，特征可能过期"
        )

    fetched_at = str(opgg.get("fetchedAt") or "")
    if fetched_at:
        try:
            parsed = datetime.fromisoformat(fetched_at)
            age_days = (datetime.now(parsed.tzinfo) - parsed).total_seconds() / 86400.0
            if age_days > max_age_days:
                warnings.append(
                    f"OP.GG 数据已抓取 {age_days:.0f} 天（超过 {max_age_days:.0f} 天），"
                    "建议重跑 opgg_scrape.py"
                )
        except (TypeError, ValueError):
            warnings.append(f"opgg_stats.json 的 fetchedAt 无法解析: {fetched_at!r}")
    else:
        warnings.append("opgg_stats.json 缺少 fetchedAt 字段")
    return warnings
