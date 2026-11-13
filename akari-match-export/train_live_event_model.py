#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train Live Client Data-feasible kill / gank models.

Labels are player-perspective:
  own_kill_30s, enemy_kill_30s, own_gank_60s, enemy_gank_60s

Features exclude enemy gold and map positions. One row = one player at one
minute (same rows as live_dataset.csv).
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
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_recall_curve,
    roc_auc_score,
)
from sklearn.model_selection import GroupKFold

from pathutil import portable_path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LABELS = (
    "own_kill_30s",
    "enemy_kill_30s",
    "own_gank_60s",
    "enemy_gank_60s",
)
NON_FEATURES = {"gameId", "ownWin"} | set(LABELS)
CATEGORICAL_COLUMNS = {"role"}


def build_features(dataframe: pd.DataFrame) -> tuple[pd.DataFrame, List[str]]:
    columns = [column for column in dataframe.columns if column not in NON_FEATURES]
    features = dataframe[columns].copy()

    for column in columns:
        if column in CATEGORICAL_COLUMNS:
            features[column] = features[column].astype("category")
        else:
            features[column] = pd.to_numeric(features[column], errors="coerce")

    return features, columns


def evaluate_label(
    dataframe: pd.DataFrame,
    features: pd.DataFrame,
    feature_columns: List[str],
    label: str,
    folds: int,
    seed: int,
    output_directory: Path,
) -> Dict[str, Any]:
    y = dataframe[label].astype(int).to_numpy()
    groups = dataframe["gameId"].astype(int).to_numpy()

    positive = int(y.sum())
    positive_rate = positive / max(1, len(y))
    oof = np.full(len(dataframe), np.nan)
    importances = np.zeros(len(feature_columns))
    best_iterations: List[int] = []

    splitter = GroupKFold(n_splits=folds)

    for train_index, valid_index in splitter.split(features, y, groups):
        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=100,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            is_unbalance=True,
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
        best_iterations.append(int(model.best_iteration_ or model.n_estimators))
        importances += model.booster_.feature_importance(importance_type="gain")

    precision, recall, thresholds = precision_recall_curve(y, oof)
    f1 = 2 * precision * recall / np.maximum(precision + recall, 1e-12)
    best_index = int(np.argmax(f1))
    best_threshold = float(
        thresholds[min(best_index, len(thresholds) - 1)]
        if len(thresholds)
        else 0.5
    )

    metrics = {
        "label": label,
        "rows": int(len(dataframe)),
        "positive": positive,
        "positiveRate": positive_rate,
        "baselineAP": positive_rate,
        "ap": float(average_precision_score(y, oof)),
        "rocAuc": float(roc_auc_score(y, oof)),
        "brier": float(brier_score_loss(y, oof)),
        "bestF1": float(f1[best_index]),
        "bestThreshold": best_threshold,
        "bestPrecision": float(precision[best_index]),
        "bestRecall": float(recall[best_index]),
        "bestIterationMean": int(np.mean(best_iterations)),
    }

    importance_frame = pd.DataFrame(
        {
            "feature": feature_columns,
            "gain": importances / max(1, folds),
        }
    ).sort_values("gain", ascending=False)
    importance_frame.to_csv(
        output_directory / f"feature_importance_{label}.csv",
        index=False,
        encoding="utf-8-sig",
    )

    final_estimators = min(300, max(50, int(np.mean(best_iterations))))
    final_model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=final_estimators,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=100,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        is_unbalance=True,
        random_state=seed,
        verbose=-1,
    )
    final_model.fit(features, y)
    final_model.booster_.save_model(str(output_directory / f"model_{label}.txt"))

    return metrics


def train(dataset_path: Path, output_directory: Path, folds: int, seed: int) -> int:
    dataframe = pd.read_csv(dataset_path, encoding="utf-8-sig")

    missing = [label for label in LABELS if label not in dataframe.columns]
    if missing:
        print(f"[错误] 缺少标签列: {missing}", file=sys.stderr)
        return 1

    print(f"Rows: {len(dataframe)}")
    print(f"Games: {dataframe['gameId'].nunique()}")

    features, feature_columns = build_features(dataframe)
    print(f"Features: {len(feature_columns)}")
    print()

    output_directory.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Dict[str, Any]] = {}

    for label in LABELS:
        print(f"Training {label}...")
        results[label] = evaluate_label(
            dataframe,
            features,
            feature_columns,
            label,
            folds,
            seed,
            output_directory,
        )
        item = results[label]
        print(
            f"  positive={item['positive']} rate={item['positiveRate']:.3f} "
            f"AP={item['ap']:.4f} (baseline {item['baselineAP']:.4f}) "
            f"ROC-AUC={item['rocAuc']:.4f} Brier={item['brier']:.4f} "
            f"bestF1={item['bestF1']:.4f} @ {item['bestThreshold']:.3f}"
        )

    (output_directory / "metrics.json").write_text(
        json.dumps(
            {
                "dataset": portable_path(dataset_path, output_directory),
                "rows": int(len(dataframe)),
                "games": int(dataframe["gameId"].nunique()),
                "features": feature_columns,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(
        f"{'label':18s} {'posRate':>7s} {'AP':>7s} {'baseAP':>7s} "
        f"{'ROC-AUC':>8s} {'Brier':>7s} {'F1':>7s}"
    )
    for label in LABELS:
        item = results[label]
        print(
            f"{label:18s} {item['positiveRate']:7.3f} {item['ap']:7.4f} "
            f"{item['baselineAP']:7.4f} {item['rocAuc']:8.4f} "
            f"{item['brier']:7.4f} {item['bestF1']:7.4f}"
        )

    print()
    print(f"Output: {output_directory}")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="训练 Live Client Data 可观测的击杀/gank 模型",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    output = args.output or (args.dataset.parent / "live_event_model")

    return train(args.dataset, output, args.folds, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
