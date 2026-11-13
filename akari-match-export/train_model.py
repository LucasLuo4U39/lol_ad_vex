#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train a 1-minute win-probability baseline with LightGBM.

Input: dataset_blue.csv produced by build_dataset.py.
Output: model.txt, metrics.json, feature_importance.csv, oof_predictions.csv.

This is a pipeline-validation trainer: with a few dozen games the metrics are
unstable, but the whole flow (features -> grouped CV -> calibration-ready
probabilities -> saved model) is the same one used at larger scale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from pathutil import portable_path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    "blue_champ_TOP",
    "blue_champ_JUNGLE",
    "blue_champ_MIDDLE",
    "blue_champ_BOTTOM",
    "blue_champ_UTILITY",
    "red_champ_TOP",
    "red_champ_JUNGLE",
    "red_champ_MIDDLE",
    "red_champ_BOTTOM",
    "red_champ_UTILITY",
}

CATEGORICAL_COLUMNS = CHAMPION_COLUMNS | {"patch"}

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
CAMP_NAMES = ("blue", "red", "wolves", "raptors")

KILL_EXTRA_COLUMNS = {
    f"{counter}_diff_{role}"
    for role in ROLES
    for counter in ("kills", "deaths", "assists")
} | {
    f"kill_lane_{lane}_diff" for lane in ("top", "mid", "bot", "jungle")
} | {"first_blood"}

JUNGLE_EXTRA_COLUMNS = {
    f"jungler_{kind}_{lane}_diff"
    for kind in ("zone", "near", "gank")
    for lane in ("top", "mid", "bot")
} | {
    f"{side}_jungle_start_{camp}"
    for side in ("blue", "red")
    for camp in CAMP_NAMES
} | {"blue_jungle_start_invade", "red_jungle_start_invade"}


def build_features(
    dataframe: pd.DataFrame, include_champions: bool, feature_groups: str
):
    excluded = set(DROP_COLUMNS)
    if not include_champions:
        excluded |= CHAMPION_COLUMNS

    if feature_groups == "base":
        excluded |= KILL_EXTRA_COLUMNS | JUNGLE_EXTRA_COLUMNS
    elif feature_groups == "kills":
        excluded |= JUNGLE_EXTRA_COLUMNS
    elif feature_groups == "jungle":
        excluded |= KILL_EXTRA_COLUMNS

    feature_columns = [
        column for column in dataframe.columns if column not in excluded
    ]
    features = dataframe[feature_columns].copy()

    for column in feature_columns:
        if column in CATEGORICAL_COLUMNS:
            features[column] = features[column].astype("category")
        else:
            features[column] = pd.to_numeric(features[column], errors="coerce")

    return features, feature_columns


def metrics_for(y_true: np.ndarray, probabilities: np.ndarray) -> Dict[str, float]:
    try:
        auc = float(roc_auc_score(y_true, probabilities))
    except ValueError:
        auc = float("nan")

    return {
        "auc": auc,
        "logLoss": float(log_loss(y_true, probabilities)),
        "brier": float(brier_score_loss(y_true, probabilities)),
        "accuracy": float(accuracy_score(y_true, (probabilities >= 0.5).astype(int))),
        "rows": int(len(y_true)),
    }


def train(
    dataset_path: Path,
    output_directory: Path,
    folds: int,
    seed: int,
    include_champions: bool = False,
    feature_groups: str = "base",
) -> int:
    dataframe = pd.read_csv(dataset_path, encoding="utf-8-sig")

    if dataframe.empty:
        print("[错误] dataset 为空", file=sys.stderr)
        return 1

    if "blue_win" not in dataframe.columns:
        print("[错误] 缺少 blue_win 列", file=sys.stderr)
        return 1

    y = dataframe["blue_win"].astype(int).to_numpy()
    groups = dataframe["gameId"].astype(int).to_numpy()

    features, feature_columns = build_features(
        dataframe, include_champions, feature_groups
    )

    unique_games = len(set(groups))
    if unique_games < folds:
        folds = max(2, unique_games)

    print(f"Rows: {len(dataframe)}")
    print(f"Games: {unique_games}")
    print(f"Features: {len(feature_columns)}")
    print(f"Folds: {folds}")

    oof = np.full(len(dataframe), np.nan)
    baseline_constant = np.full(len(dataframe), np.nan)
    baseline_gold = np.full(len(dataframe), np.nan)
    baseline_gold_xp = np.full(len(dataframe), np.nan)
    fold_metrics: List[Dict[str, Any]] = []
    best_iterations: List[int] = []
    importances = np.zeros(len(feature_columns))

    splitter = GroupKFold(n_splits=folds)

    for fold, (train_index, valid_index) in enumerate(
        splitter.split(features, y, groups), start=1
    ):
        x_train = features.iloc[train_index]
        x_valid = features.iloc[valid_index]
        y_train = y[train_index]
        y_valid = y[valid_index]

        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=400,
            learning_rate=0.05,
            num_leaves=7,
            min_child_samples=50,
            colsample_bytree=0.8,
            subsample=0.8,
            subsample_freq=1,
            reg_lambda=1.0,
            random_state=seed,
            verbose=-1,
        )

        model.fit(
            x_train,
            y_train,
            eval_set=[(x_valid, y_valid)],
            eval_metric="binary_logloss",
            callbacks=[
                lgb.early_stopping(30, verbose=False),
                lgb.log_evaluation(0),
            ],
        )

        probabilities = model.predict_proba(x_valid)[:, 1]
        oof[valid_index] = probabilities

        baseline_constant[valid_index] = float(y_train.mean())

        for columns, target in (
            (["gold_diff"], baseline_gold),
            (["gold_diff", "xp_diff", "minute"], baseline_gold_xp),
        ):
            pipeline = make_pipeline(
                StandardScaler(),
                LogisticRegression(max_iter=1000, random_state=seed),
            )
            pipeline.fit(x_train[columns], y_train)
            target[valid_index] = pipeline.predict_proba(x_valid[columns])[:, 1]

        fold_result = metrics_for(y_valid, probabilities)
        fold_result["fold"] = fold
        fold_result["bestIteration"] = int(model.best_iteration_ or model.n_estimators)
        fold_metrics.append(fold_result)
        best_iterations.append(fold_result["bestIteration"])

        importances += model.booster_.feature_importance(importance_type="gain")

        print(
            f"  fold {fold}: AUC={fold_result['auc']:.4f} "
            f"logLoss={fold_result['logLoss']:.4f} "
            f"Brier={fold_result['brier']:.4f} "
            f"acc={fold_result['accuracy']:.4f}"
        )

    valid_mask = ~np.isnan(oof)
    overall = metrics_for(y[valid_mask], oof[valid_mask])
    overall["baselineBrier"] = 0.25
    overall["baselineLogLoss"] = float(np.log(2))

    baselines = {
        "constant": metrics_for(y, baseline_constant),
        "goldDiffOnly": metrics_for(y, baseline_gold),
        "goldXpMinute": metrics_for(y, baseline_gold_xp),
    }

    per_minute: List[Dict[str, Any]] = []
    minute_series = dataframe["minute"].astype(int)

    for minute in sorted(minute_series.unique()):
        mask = (minute_series == minute).to_numpy() & valid_mask

        if mask.sum() < 20:
            continue

        y_minute = y[mask]
        p_minute = oof[mask]

        if len(set(y_minute)) < 2:
            continue

        item = metrics_for(y_minute, p_minute)
        item["minute"] = int(minute)
        per_minute.append(item)

    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "oof_predictions.csv").write_text(
        pd.DataFrame(
            {
                "gameId": dataframe["gameId"],
                "minute": dataframe["minute"],
                "side": dataframe.get("side", "blue"),
                "y": y,
                "probability": oof,
            }
        ).to_csv(index=False),
        encoding="utf-8-sig",
    )

    importance_frame = pd.DataFrame(
        {
            "feature": feature_columns,
            "gain": importances / max(1, len(fold_metrics)),
        }
    ).sort_values("gain", ascending=False)
    importance_frame.to_csv(
        output_directory / "feature_importance.csv", index=False, encoding="utf-8-sig"
    )

    final_estimators = min(400, max(50, int(np.mean(best_iterations))))
    final_model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=final_estimators,
        learning_rate=0.05,
        num_leaves=7,
        min_child_samples=50,
        colsample_bytree=0.8,
        subsample=0.8,
        subsample_freq=1,
        reg_lambda=1.0,
        random_state=seed,
        verbose=-1,
    )
    final_model.fit(features, y)
    final_model.booster_.save_model(str(output_directory / "model.txt"))

    metrics = {
        "dataset": portable_path(dataset_path, output_directory),
        "rows": int(len(dataframe)),
        "games": int(unique_games),
        "features": len(feature_columns),
        "featureGroups": feature_groups,
        "folds": folds,
        "overall": overall,
        "baselines": baselines,
        "perMinute": per_minute,
        "foldMetrics": fold_metrics,
        "finalEstimators": final_estimators,
        "featureColumns": feature_columns,
        "categoricalColumns": sorted(
            column for column in feature_columns if column in CATEGORICAL_COLUMNS
        ),
    }
    (output_directory / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(
        f"OOF AUC={overall['auc']:.4f} "
        f"logLoss={overall['logLoss']:.4f} "
        f"Brier={overall['brier']:.4f} "
        f"acc={overall['accuracy']:.4f}"
    )
    print()
    print("Baselines:")
    for name, item in baselines.items():
        print(
            f"  {name:14s} AUC={item['auc']:.4f} "
            f"logLoss={item['logLoss']:.4f} Brier={item['brier']:.4f}"
        )
    print()
    print("Per minute (Brier):")
    for item in per_minute[:: max(1, len(per_minute) // 10)]:
        print(
            f"  minute {item['minute']:2d}: "
            f"Brier={item['brier']:.4f} AUC={item['auc']:.4f} n={item['rows']}"
        )
    print()
    print("Top features:")
    print(importance_frame.head(10).to_string(index=False))
    print()
    print(f"Output: {output_directory}")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="训练 1 分钟胜率基线（LightGBM + GroupKFold）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="dataset_blue.csv 路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="模型输出目录，默认是 <dataset 所在目录>/model",
    )
    parser.add_argument("--folds", type=int, default=5, help="GroupKFold 折数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument(
        "--include-champs",
        action="store_true",
        help="把 championId 作为 categorical 特征（小数据集容易过拟合）",
    )
    parser.add_argument(
        "--feature-groups",
        choices=["base", "kills", "jungle", "all"],
        default="base",
        help="base=基础 48 特征；kills/jungle=附加击杀/打野特征；all=全部",
    )
    args = parser.parse_args(argv)

    output = args.output or (args.dataset.parent / "model")

    return train(
        args.dataset,
        output,
        args.folds,
        args.seed,
        args.include_champs,
        args.feature_groups,
    )


if __name__ == "__main__":
    raise SystemExit(main())
