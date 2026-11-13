#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live advisor using only Live Client Data-observable features.

Polls allgamedata, builds the same features as live_dataset.csv, predicts the
active player's team win probability, and prints a short recommendation.

Note: the live event models only use Live Client Data-observable features; the
event recency features (since_*/last_*) are computed by live_features.py from
events.Events with the same semantics as the training dataset.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
import requests
import urllib3

from live_features import DEFAULT_EVENT_THRESHOLDS, extract_features as extract_live_features

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENDPOINT = "https://127.0.0.1:2999/liveclientdata/allgamedata"
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
EVENT_LABELS = (
    "own_kill_30s",
    "enemy_kill_30s",
    "own_gank_60s",
    "enemy_gank_60s",
)


def fetch_payload() -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(ENDPOINT, verify=False, timeout=3)
        if response.status_code != 200:
            return None
        return response.json()
    except Exception:
        return None


def find_active_player(
    payload: Dict[str, Any],
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
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


def player_team_by_name(
    players: List[Dict[str, Any]], name: Optional[str]
) -> Optional[str]:
    if not name:
        return None
    normalized = name.split("#")[0]
    for player in players:
        player_name = player.get("summonerName") or player.get("riotId") or ""
        if player_name == name or player_name.split("#")[0] == normalized:
            return player.get("team")
    return None


def objective_counts(
    payload: Dict[str, Any],
) -> Dict[str, Dict[str, int]]:
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
        event_name = event.get("eventName") or ""
        metric = mapping.get(event_name)
        if not metric:
            continue

        killer = event.get("KillerName")
        team = player_team_by_name(players, killer)
        if team in counts:
            counts[team][metric] += 1

    return counts


def extract_features(payload: Dict[str, Any]) -> Dict[str, Any]:
    """委托 live_features.extract_features（含事件时序特征，口径与训练一致）。"""
    return extract_live_features(payload)


def build_frame(
    features: Dict[str, Any], feature_columns: List[str]
) -> pd.DataFrame:
    frame = pd.DataFrame(
        [{column: features.get(column, 0) for column in feature_columns}]
    )

    for column in feature_columns:
        if column == "role":
            frame[column] = pd.Categorical(
                frame[column].astype(str),
                categories=sorted(ROLES),
            )
        else:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return frame


def predict(
    model: lgb.Booster, features: Dict[str, Any], feature_columns: List[str]
) -> float:
    return float(model.predict(build_frame(features, feature_columns))[0])


def advice(
    features: Dict[str, Any],
    probability: float,
    event_probabilities: Optional[Dict[str, float]] = None,
    thresholds: Optional[Dict[str, float]] = None,
) -> List[str]:
    """规则建议；事件阈值默认用 OOF 最优 F1 阈值（DEFAULT_EVENT_THRESHOLDS）。"""
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
        threshold_map = thresholds or DEFAULT_EVENT_THRESHOLDS
        own_gank = event_probabilities["own_gank_60s"]
        enemy_gank = event_probabilities["enemy_gank_60s"]
        own_kill = event_probabilities["own_kill_30s"]
        enemy_kill = event_probabilities["enemy_kill_30s"]

        if (
            enemy_gank >= threshold_map.get("enemy_gank_60s", 0.5)
            and enemy_gank > own_gank + 0.05
        ):
            tips.append("60 秒内敌方打野更可能来抓，注意后退、补视野。")
        elif (
            own_gank >= threshold_map.get("own_gank_60s", 0.5)
            and own_gank > enemy_gank + 0.05
        ):
            tips.append("60 秒内我方打野更可能来抓，可以找机会。")

        if (
            enemy_kill >= threshold_map.get("enemy_kill_30s", 0.5)
            and enemy_kill > own_kill + 0.05
        ):
            tips.append("30 秒内敌方更可能拿到击杀，小心被开。")
        elif (
            own_kill >= threshold_map.get("own_kill_30s", 0.5)
            and own_kill > enemy_kill + 0.05
        ):
            tips.append("30 秒内我方更可能拿到击杀，可以接。")

    return tips


def run(
    model_directory: Path,
    interval: float,
    once: bool,
    event_model_directory: Optional[Path],
) -> int:
    metrics = json.loads(
        (model_directory / "metrics.json").read_text(encoding="utf-8")
    )
    feature_columns = metrics["featureColumns"]
    model = lgb.Booster(model_file=str(model_directory / "model.txt"))

    event_models: Dict[str, lgb.Booster] = {}
    event_feature_columns: List[str] = []

    if event_model_directory and (event_model_directory / "metrics.json").is_file():
        event_metrics = json.loads(
            (event_model_directory / "metrics.json").read_text(encoding="utf-8")
        )
        event_feature_columns = event_metrics["features"]

        for label in EVENT_LABELS:
            path = event_model_directory / f"model_{label}.txt"
            if path.is_file():
                event_models[label] = lgb.Booster(model_file=str(path))

    print(f"Live advisor 启动，胜率模型: {model_directory}")
    if event_models:
        print(f"事件模型: {event_model_directory} ({len(event_models)} 个)")
    print(f"轮询间隔: {interval}s")

    while True:
        payload = fetch_payload()
        if payload is None:
            print("等待对局（2999 未响应）...")
            if once:
                return 1
            time.sleep(interval)
            continue

        try:
            features = extract_features(payload)
        except Exception as error:
            print(f"解析失败: {error}")
            if once:
                return 1
            time.sleep(interval)
            continue

        probability = predict(model, features, feature_columns)

        event_probabilities: Optional[Dict[str, float]] = None
        if event_models and event_feature_columns:
            event_frame = build_frame(features, event_feature_columns)
            event_probabilities = {
                label: float(event_models[label].predict(event_frame)[0])
                for label in event_models
            }

        minute = int(features["minute"])
        score = f"{minute:02d}:{int((float(payload.get('gameData', {}).get('gameTime', 0)) % 60)):02d}"

        print(f"\n[{score}] 胜率 {probability:.1%}")
        print(
            f"  KDA {int(features['ownKills'])}/{int(features['ownDeaths'])}/"
            f"{int(features['ownAssists'])}  CS {int(features['ownCS'])}  "
            f"经济 {int(features['ownGold'])}"
        )
        print(
            f"  团队击杀差 {int(features['killDiff']):+d}  "
            f"等级差 {int(features['levelSumDiff']):+d}  "
            f"补刀差 {int(features['csDiff']):+d}"
        )
        if event_probabilities:
            print(
                f"  击杀 30s: 我方 {event_probabilities['own_kill_30s']:.1%} / "
                f"敌方 {event_probabilities['enemy_kill_30s']:.1%}  "
                f"Gank 60s: 我方 {event_probabilities['own_gank_60s']:.1%} / "
                f"敌方 {event_probabilities['enemy_gank_60s']:.1%}"
            )

        for tip in advice(features, probability, event_probabilities):
            print(f"  - {tip}")

        if once:
            return 0

        time.sleep(interval)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live Client Data 实时建议",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--event-model-dir", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    event_model_dir = args.event_model_dir
    if event_model_dir is None:
        candidate = args.model_dir.parent / "live_event_model"
        event_model_dir = candidate if candidate.is_dir() else None

    return run(args.model_dir, args.interval, args.once, event_model_dir)


if __name__ == "__main__":
    raise SystemExit(main())
