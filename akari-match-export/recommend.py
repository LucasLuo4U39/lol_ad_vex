#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Synthesize kill/gank model outputs into a short recommendation.

Reads one row from event_dataset.csv and the five event models, then applies
simple rules. This is the deterministic layer that an LLM can later explain.

Example:
  python recommend.py --dataset ...\event_dataset.csv --game-id 901127125498 --t 1200 --side blue
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import lightgbm as lgb
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LABELS = (
    "kill_30s",
    "blue_kill_30s",
    "red_kill_30s",
    "blue_gank_60s",
    "red_gank_60s",
)


def mmss(timestamp: float) -> str:
    total = int(timestamp)
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


def load_models(model_directory: Path, feature_columns: List[str]):
    models = {}

    for label in LABELS:
        path = model_directory / f"model_{label}.txt"
        if not path.is_file():
            raise FileNotFoundError(f"缺少模型: {path}")
        models[label] = lgb.Booster(model_file=str(path))

    return models


def predict(models, row: pd.Series, feature_columns: List[str], patches: List[str]):
    frame = pd.DataFrame([{column: row[column] for column in feature_columns}])

    for column in feature_columns:
        if column == "patch":
            frame[column] = pd.Categorical(
                frame[column].astype(str), categories=patches
            )
        else:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return {
        label: float(models[label].predict(frame)[0])
        for label in LABELS
    }


def synthesize(
    row: pd.Series,
    probabilities: Dict[str, float],
    side: str,
) -> Dict[str, Any]:
    blue = side == "blue"

    our_kill = probabilities["blue_kill_30s" if blue else "red_kill_30s"]
    enemy_kill = probabilities["red_kill_30s" if blue else "blue_kill_30s"]
    our_gank = probabilities["blue_gank_60s" if blue else "red_gank_60s"]
    enemy_gank = probabilities["red_gank_60s" if blue else "blue_gank_60s"]

    gold_diff = float(row.get("gold_diff", 0))
    if not blue:
        gold_diff = -gold_diff

    advice: List[str] = []

    if enemy_gank >= 0.45 and enemy_gank > our_gank + 0.05:
        advice.append("敌方打野更可能来抓，注意后退、补视野，不要压线。")
    elif our_gank >= 0.45 and our_gank > enemy_gank + 0.05:
        advice.append("我方打野更可能来抓，可以适度压线、找机会。")
    else:
        advice.append("双方打野压力接近，保持正常对线。")

    if our_kill >= 0.65 and our_kill > enemy_kill + 0.05:
        advice.append("30 秒内我方有击杀机会，可以接战。")
    elif enemy_kill >= 0.65 and enemy_kill > our_kill + 0.05:
        advice.append("30 秒内敌方有击杀机会，小心被开。")

    if gold_diff >= 2000:
        advice.append(f"经济领先 {int(gold_diff)}，不要主动送节奏。")
    elif gold_diff <= -2000:
        advice.append(f"经济落后 {int(abs(gold_diff))}，避战发育、等对手失误。")

    minute = int(row["minute"])
    since_dragon = float(row.get("since_dragon", 9999))
    since_baron = float(row.get("since_baron", 9999))
    since_tower = float(row.get("since_tower", 9999))

    if minute >= 5 and (since_dragon >= 300 or since_dragon == 9999):
        advice.append("小龙已进入可争夺时间，注意龙区视野。")
    if minute >= 20 and (since_baron >= 360 or since_baron == 9999):
        advice.append("大龙已进入可争夺时间，注意大龙区视野和抱团。")
    if since_tower != 9999 and since_tower >= 120:
        advice.append("近期有防御塔被摧毁，注意兵线和边线压力。")

    return {
        "time": mmss(float(row["t"])),
        "minute": int(row["minute"]),
        "side": side,
        "models": {
            "kill30": probabilities["kill_30s"],
            "ourKill30": our_kill,
            "enemyKill30": enemy_kill,
            "ourGank60": our_gank,
            "enemyGank60": enemy_gank,
        },
        "state": {
            "goldDiff": gold_diff,
            "xpDiff": float(row.get("xp_diff", 0)),
            "killsLast180": float(row.get("kills_last_180", 0)),
            "ganksLast300": float(row.get("ganks_last_300", 0)),
        },
        "advice": advice,
        "summary": " ".join(advice),
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把击杀/gank 模型输出合成成建议",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--game-id", type=int, required=True)
    parser.add_argument("--t", type=float, required=True, help="时间点（秒）")
    parser.add_argument("--side", choices=["blue", "red"], default="blue")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    dataframe = pd.read_csv(args.dataset, encoding="utf-8-sig")
    game_rows = dataframe[dataframe["gameId"].astype(int) == args.game_id]

    if game_rows.empty:
        print(f"[错误] 找不到 gameId={args.game_id}", file=sys.stderr)
        return 1

    index = (game_rows["t"].astype(float) - args.t).abs().idxmin()
    row = dataframe.loc[index]

    model_directory = args.model_dir or (args.dataset.parent / "event_model")
    metadata = json.loads(
        (model_directory / "metrics.json").read_text(encoding="utf-8")
    )
    feature_columns = metadata["features"]
    patches = sorted(dataframe["patch"].astype(str).unique())

    models = load_models(model_directory, feature_columns)
    probabilities = predict(models, row, feature_columns, patches)
    result = synthesize(row, probabilities, args.side)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[{result['time']}] {args.side} 视角")
        print(
            f"  击杀 30s: 我方 {result['models']['ourKill30']:.1%} / "
            f"敌方 {result['models']['enemyKill30']:.1%}"
        )
        print(
            f"  Gank 60s: 我方 {result['models']['ourGank60']:.1%} / "
            f"敌方 {result['models']['enemyGank60']:.1%}"
        )
        print(
            f"  经济差: {result['state']['goldDiff']:+.0f}  "
            f"经验差: {result['state']['xpDiff']:+.0f}"
        )
        print("  建议:")
        for item in result["advice"]:
            print(f"    - {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
