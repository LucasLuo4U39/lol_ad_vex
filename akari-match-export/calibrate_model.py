#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probability calibration for the 1-minute win-probability model.

Reads `oof_predictions.csv` (out-of-fold probabilities), then evaluates:

  * raw
  * isotonic regression
  * Platt scaling (logistic on the logit)

Calibration is cross-fitted by gameId so the calibrator is never evaluated on
the same games it was fitted on. A final calibrator is fitted on all OOF rows
for deployment and saved as JSON.

Outputs:
  calibration_report.json
  reliability_curve.csv
  calibrated_predictions.csv
  calibrator_isotonic.json
  calibrator_platt.json
  summary.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from pathutil import portable_path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def logit(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(probabilities, 1e-6, 1 - 1e-6)
    return np.log(clipped / (1 - clipped))


def sigmoid(values: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-values))


def reliability_table(
    y_true: np.ndarray, probabilities: np.ndarray, bins: int = 10
) -> List[Dict[str, Any]]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    indices = np.clip(np.digitize(probabilities, edges) - 1, 0, bins - 1)
    rows: List[Dict[str, Any]] = []

    for index in range(bins):
        mask = indices == index
        count = int(mask.sum())

        if count == 0:
            continue

        mean_probability = float(probabilities[mask].mean())
        actual = float(y_true[mask].mean())
        rows.append(
            {
                "bin": index,
                "lower": float(edges[index]),
                "upper": float(edges[index + 1]),
                "count": count,
                "meanProbability": mean_probability,
                "actualWinRate": actual,
                "gap": abs(mean_probability - actual),
            }
        )

    return rows


def expected_calibration_error(
    y_true: np.ndarray, probabilities: np.ndarray, bins: int = 10
) -> Tuple[float, float]:
    rows = reliability_table(y_true, probabilities, bins)

    if not rows:
        return 0.0, 0.0

    total = sum(row["count"] for row in rows)
    ece = sum(row["count"] / total * row["gap"] for row in rows)
    mce = max(row["gap"] for row in rows)
    return float(ece), float(mce)


def metrics(y_true: np.ndarray, probabilities: np.ndarray) -> Dict[str, Any]:
    try:
        auc = float(roc_auc_score(y_true, probabilities))
    except ValueError:
        auc = float("nan")

    ece, mce = expected_calibration_error(y_true, probabilities)

    return {
        "auc": auc,
        "logLoss": float(log_loss(y_true, probabilities)),
        "brier": float(brier_score_loss(y_true, probabilities)),
        "ece": ece,
        "mce": mce,
        "rows": int(len(y_true)),
        "meanProbability": float(probabilities.mean()),
        "actualWinRate": float(y_true.mean()),
    }


def fit_isotonic(
    probabilities: np.ndarray, y_true: np.ndarray
) -> IsotonicRegression:
    model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    model.fit(probabilities, y_true)
    return model


def fit_platt(probabilities: np.ndarray, y_true: np.ndarray) -> LogisticRegression:
    model = LogisticRegression(max_iter=1000)
    model.fit(logit(probabilities).reshape(-1, 1), y_true)
    return model


def apply_isotonic(model: IsotonicRegression, probabilities: np.ndarray) -> np.ndarray:
    return np.clip(model.predict(probabilities), 0.0, 1.0)


def apply_platt(model: LogisticRegression, probabilities: np.ndarray) -> np.ndarray:
    return model.predict_proba(logit(probabilities).reshape(-1, 1))[:, 1]


def group_folds(groups: np.ndarray, folds: int) -> List[np.ndarray]:
    from sklearn.model_selection import GroupKFold

    splitter = GroupKFold(n_splits=folds)
    return [
        valid_index
        for _, valid_index in splitter.split(np.zeros(len(groups)), groups=groups)
    ]


def calibrate(
    oof_path: Path,
    output_directory: Path,
    folds: int = 5,
) -> int:
    dataframe = pd.read_csv(oof_path, encoding="utf-8-sig")

    required = {"gameId", "minute", "y", "probability"}
    if not required.issubset(dataframe.columns):
        print(
            f"[错误] oof_predictions.csv 缺少列: {sorted(required - set(dataframe.columns))}",
            file=sys.stderr,
        )
        return 1

    dataframe = dataframe.dropna(subset=["y", "probability"]).reset_index(drop=True)
    y = dataframe["y"].astype(int).to_numpy()
    raw = np.clip(dataframe["probability"].astype(float).to_numpy(), 1e-6, 1 - 1e-6)
    groups = dataframe["gameId"].astype(int).to_numpy()

    unique_games = len(set(groups))
    folds = max(2, min(folds, unique_games))

    calibrated_isotonic = np.full(len(dataframe), np.nan)
    calibrated_platt = np.full(len(dataframe), np.nan)
    fold_metrics: List[Dict[str, Any]] = []

    for fold, valid_index in enumerate(group_folds(groups, folds), start=1):
        train_mask = np.ones(len(dataframe), dtype=bool)
        train_mask[valid_index] = False

        iso = fit_isotonic(raw[train_mask], y[train_mask])
        platt = fit_platt(raw[train_mask], y[train_mask])

        calibrated_isotonic[valid_index] = apply_isotonic(iso, raw[valid_index])
        calibrated_platt[valid_index] = apply_platt(platt, raw[valid_index])

        fold_metrics.append(
            {
                "fold": fold,
                "raw": metrics(y[valid_index], raw[valid_index]),
                "isotonic": metrics(
                    y[valid_index], calibrated_isotonic[valid_index]
                ),
                "platt": metrics(y[valid_index], calibrated_platt[valid_index]),
            }
        )

        print(
            f"  fold {fold}: raw Brier={fold_metrics[-1]['raw']['brier']:.4f} "
            f"ECE={fold_metrics[-1]['raw']['ece']:.4f} -> "
            f"isotonic Brier={fold_metrics[-1]['isotonic']['brier']:.4f} "
            f"ECE={fold_metrics[-1]['isotonic']['ece']:.4f}"
        )

    valid_mask = ~np.isnan(calibrated_isotonic)
    report = {
        "dataset": portable_path(oof_path, output_directory),
        "rows": int(len(dataframe)),
        "games": int(unique_games),
        "folds": folds,
        "raw": metrics(y[valid_mask], raw[valid_mask]),
        "isotonic": metrics(y[valid_mask], calibrated_isotonic[valid_mask]),
        "platt": metrics(y[valid_mask], calibrated_platt[valid_mask]),
        "foldMetrics": fold_metrics,
        "reliability": {
            "raw": reliability_table(y[valid_mask], raw[valid_mask]),
            "isotonic": reliability_table(
                y[valid_mask], calibrated_isotonic[valid_mask]
            ),
            "platt": reliability_table(y[valid_mask], calibrated_platt[valid_mask]),
        },
    }

    output_directory.mkdir(parents=True, exist_ok=True)

    (output_directory / "calibration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    pd.DataFrame(
        {
            "gameId": dataframe["gameId"],
            "minute": dataframe["minute"],
            "side": dataframe.get("side", "blue"),
            "y": y,
            "raw": raw,
            "isotonic": calibrated_isotonic,
            "platt": calibrated_platt,
        }
    ).to_csv(
        output_directory / "calibrated_predictions.csv",
        index=False,
        encoding="utf-8-sig",
    )

    reliability_rows: List[Dict[str, Any]] = []
    for method in ("raw", "isotonic", "platt"):
        for row in report["reliability"][method]:
            item = dict(row)
            item["method"] = method
            reliability_rows.append(item)

    pd.DataFrame(reliability_rows).to_csv(
        output_directory / "reliability_curve.csv",
        index=False,
        encoding="utf-8-sig",
    )

    final_iso = fit_isotonic(raw[valid_mask], y[valid_mask])
    final_platt = fit_platt(raw[valid_mask], y[valid_mask])

    (output_directory / "calibrator_isotonic.json").write_text(
        json.dumps(
            {
                "type": "isotonic",
                "x": [float(value) for value in final_iso.X_thresholds_],
                "y": [float(value) for value in final_iso.y_thresholds_],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (output_directory / "calibrator_platt.json").write_text(
        json.dumps(
            {
                "type": "platt",
                "a": float(final_platt.coef_[0][0]),
                "b": float(final_platt.intercept_[0]),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Per-minute calibration comparison.
    per_minute: List[Dict[str, Any]] = []
    minute_series = dataframe["minute"].astype(int)

    for minute in sorted(minute_series.unique()):
        mask = (minute_series == minute).to_numpy() & valid_mask

        if mask.sum() < 50:
            continue

        per_minute.append(
            {
                "minute": int(minute),
                "rows": int(mask.sum()),
                "raw": metrics(y[mask], raw[mask]),
                "isotonic": metrics(y[mask], calibrated_isotonic[mask]),
                "platt": metrics(y[mask], calibrated_platt[mask]),
            }
        )

    report["perMinute"] = per_minute
    (output_directory / "calibration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary_lines = [
        f"Rows: {len(dataframe)}",
        f"Games: {unique_games}",
        f"Folds: {folds}",
        "",
        f"{'method':10s} {'AUC':>7s} {'LogLoss':>8s} {'Brier':>7s} {'ECE':>7s} {'MCE':>7s}",
    ]

    for method in ("raw", "isotonic", "platt"):
        item = report[method]
        summary_lines.append(
            f"{method:10s} {item['auc']:7.4f} {item['logLoss']:8.4f} "
            f"{item['brier']:7.4f} {item['ece']:7.4f} {item['mce']:7.4f}"
        )

    summary_lines.extend(["", "Per minute Brier (raw -> isotonic):"])

    for item in per_minute[:: max(1, len(per_minute) // 10)]:
        summary_lines.append(
            f"  minute {item['minute']:2d}: {item['raw']['brier']:.4f} -> "
            f"{item['isotonic']['brier']:.4f}  "
            f"(ECE {item['raw']['ece']:.4f} -> {item['isotonic']['ece']:.4f})"
        )

    (output_directory / "summary.txt").write_text(
        "\n".join(summary_lines) + "\n", encoding="utf-8"
    )

    print()
    print("\n".join(summary_lines))
    print()
    print(f"Output: {output_directory}")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="对 1 分钟胜率模型做概率校准（isotonic / Platt）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--oof",
        type=Path,
        required=True,
        help="oof_predictions.csv 路径",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出目录，默认是 <oof 所在目录>/calibration",
    )
    parser.add_argument("--folds", type=int, default=5, help="按 gameId 的校准交叉验证折数")
    args = parser.parse_args(argv)

    output = args.output or (args.oof.parent / "calibration")

    return calibrate(args.oof, output, args.folds)


if __name__ == "__main__":
    raise SystemExit(main())
