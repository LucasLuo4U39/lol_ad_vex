#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feature-group ablation for the 1-minute win-probability model.

Compares:
  base            existing 48 features
  base + kills    per-role KDA, kill lane, first blood
  base + jungle   jungler zone/near/gank, start camp
  base + all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
CAMP_NAMES = ("blue", "red", "wolves", "raptors")

DROP_COLUMNS = {
    "gameId",
    "side",
    "win",
    "blue_win",
    "gold_diff_plus5",
    "game_mode",
    "queue_id",
}

CHAMPION_COLUMNS = {
    f"{side}_champ_{role}"
    for side in ("blue", "red")
    for role in ROLES
}

ROLE_KDA_COLUMNS = [
    f"{counter}_diff_{role}"
    for role in ROLES
    for counter in ("kills", "deaths", "assists")
]
KILL_LANE_COLUMNS = [
    f"kill_lane_{lane}_diff" for lane in ("top", "mid", "bot", "jungle")
]
KILL_COLUMNS = ROLE_KDA_COLUMNS + KILL_LANE_COLUMNS + ["first_blood"]

JUNGLER_COLUMNS = [
    f"jungler_{kind}_{lane}_diff"
    for kind in ("zone", "near", "gank")
    for lane in ("top", "mid", "bot")
] + [
    f"{side}_jungle_start_{camp}"
    for side in ("blue", "red")
    for camp in CAMP_NAMES
] + ["blue_jungle_start_invade", "red_jungle_start_invade"]


def metrics(y_true: np.ndarray, probabilities: np.ndarray) -> Dict[str, float]:
    try:
        auc = float(roc_auc_score(y_true, probabilities))
    except ValueError:
        auc = float("nan")

    return {
        "auc": auc,
        "logLoss": float(log_loss(y_true, probabilities)),
        "brier": float(brier_score_loss(y_true, probabilities)),
    }


def evaluate(
    dataframe: pd.DataFrame,
    feature_columns: List[str],
    folds: int,
    seed: int,
) -> Dict[str, float]:
    y = dataframe["blue_win"].astype(int).to_numpy()
    groups = dataframe["gameId"].astype(int).to_numpy()
    features = dataframe[feature_columns].copy()

    for column in feature_columns:
        if column == "patch":
            features[column] = features[column].astype("category")
        else:
            features[column] = pd.to_numeric(features[column], errors="coerce")

    oof = np.full(len(dataframe), np.nan)
    splitter = GroupKFold(n_splits=folds)

    for train_index, valid_index in splitter.split(features, y, groups):
        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=400,
            learning_rate=0.05,
            num_leaves=7,
            min_child_samples=50,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            random_state=seed,
            verbose=-1,
        )
        model.fit(
            features.iloc[train_index],
            y[train_index],
            eval_set=[(features.iloc[valid_index], y[valid_index])],
            eval_metric="binary_logloss",
            callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
        )
        oof[valid_index] = model.predict_proba(features.iloc[valid_index])[:, 1]

    result = metrics(y, oof)
    result["features"] = len(feature_columns)
    return result


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="对比基础特征 / 击杀特征 / 打野特征",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="dataset_blue.csv 路径",
    )
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    dataframe = pd.read_csv(args.dataset, encoding="utf-8-sig")
    all_columns = [
        column
        for column in dataframe.columns
        if column not in DROP_COLUMNS and column not in CHAMPION_COLUMNS
    ]

    base_columns = [
        column
        for column in all_columns
        if column not in ROLE_KDA_COLUMNS
        and column not in KILL_LANE_COLUMNS
        and column != "first_blood"
        and column not in JUNGLER_COLUMNS
    ]

    variants = {
        "base": base_columns,
        "base+kills": base_columns + KILL_COLUMNS,
        "base+jungle": base_columns + JUNGLER_COLUMNS,
        "base+all": base_columns + KILL_COLUMNS + JUNGLER_COLUMNS,
    }

    results: Dict[str, Dict[str, float]] = {}

    for name, columns in variants.items():
        print(f"Training {name} ({len(columns)} features)...")
        results[name] = evaluate(dataframe, columns, args.folds, args.seed)
        item = results[name]
        print(
            f"  AUC={item['auc']:.4f} logLoss={item['logLoss']:.4f} "
            f"Brier={item['brier']:.4f}"
        )

    print()
    print(f"{'variant':14s} {'features':>8s} {'AUC':>7s} {'LogLoss':>8s} {'Brier':>7s}")

    for name, item in results.items():
        print(
            f"{name:14s} {int(item['features']):8d} {item['auc']:7.4f} "
            f"{item['logLoss']:8.4f} {item['brier']:7.4f}"
        )

    output = args.dataset.parent / "feature_ablation.json"
    output.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(f"Output: {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
