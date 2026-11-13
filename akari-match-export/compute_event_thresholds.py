#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件模型阈值计算与审计：生成 event_thresholds.json（可复跑、可审计）。

口径：
- 部署阈值（threshold）来自训练时记录的 OOF 最优 F1 阈值：
  train_live_event_model.py 用 GroupKFold(5, gameId) 的 OOF 预测跑
  precision_recall_curve 取 argmax F1，结果存于
  dataset/live_dataset/live_event_model/metrics.json 的 results[*]。
  本脚本读取该存根、验证内部一致性（F1 = 2PR/(P+R)），并作为部署值落盘。
- 参考值（inSampleThreshold）：用「最终模型 + live_dataset.csv 每 10 行抽样」
  重新计算的 in-sample argmax-F1 阈值。最终模型见过全部训练数据，该值是
  乐观上界，只用于审计对照（预期 in-sample 阈值 ≤ OOF 阈值或接近）。
- 不重训、不改模型 .txt、不改 dataset CSV；只用 lgbmini 做推理。

运行：
    python -B compute_event_thresholds.py [--metrics ...] [--dataset ...] [--out ...]

输出：
    dataset/live_dataset/live_event_model/event_thresholds.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DATASET_DIR = BASE.parent / "dataset" / "live_dataset"
DEFAULT_METRICS = DATASET_DIR / "live_event_model" / "metrics.json"
DEFAULT_DATASET = DATASET_DIR / "live_dataset.csv"
DEFAULT_OUT = DATASET_DIR / "live_event_model" / "event_thresholds.json"

LABELS = ("own_kill_30s", "enemy_kill_30s", "own_gank_60s", "enemy_gank_60s")
IN_SAMPLE_STRIDE = 10

sys.path.insert(0, str(BASE))
from live_features import ROLE_CODES  # noqa: E402
from lgbmini import LgbTextModel  # noqa: E402

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def best_f1_threshold(y: np.ndarray, prob: np.ndarray) -> Dict[str, float]:
    """argmax-F1 阈值（按预测分数降序累计 tp/fp，等价于 precision_recall_curve 的取法）。"""
    order = np.argsort(-prob)
    y_sorted = y[order].astype(np.float64)
    n = len(y_sorted)
    n_pos = float(y_sorted.sum())
    if n_pos == 0:
        return {"threshold": 1.0, "f1": 0.0, "precision": 0.0, "recall": 0.0}
    tp = np.cumsum(y_sorted)
    fp = np.arange(1, n + 1, dtype=np.float64) - tp
    precision = tp / np.maximum(tp + fp, 1e-12)
    recall = tp / n_pos
    f1 = 2.0 * precision * recall / np.maximum(precision + recall, 1e-12)
    index = int(np.argmax(f1))
    return {
        "threshold": float(prob[order][index]),
        "f1": float(f1[index]),
        "precision": float(precision[index]),
        "recall": float(recall[index]),
    }


def budget_threshold(
    y: np.ndarray, prob: np.ndarray, alerts_per_game: float, rows_per_game: float
) -> Dict[str, float]:
    """按「每局最多 alerts_per_game 次提醒」的预算取召回最高的阈值（T60）。

    与 argmax-F1 的区别：F1 同时最大化精确/召回，实际会把 82% 的行都判成提醒；
    这里改成预算驱动的 PPV 口径——先限制提醒总量，再在预算内要最高召回。
    """
    order = np.argsort(-prob)
    y_sorted = y[order].astype(np.float64)
    n = len(y_sorted)
    n_pos = float(y_sorted.sum())
    if n == 0 or n_pos == 0:
        return {"threshold": 1.0, "precision": 0.0, "recall": 0.0, "alertRate": 0.0}
    tp = np.cumsum(y_sorted)
    fp = np.arange(1, n + 1, dtype=np.float64) - tp
    precision = tp / np.maximum(tp + fp, 1e-12)
    recall = tp / n_pos
    alert_rate = (tp + fp) / float(n)
    target_rate = max(1e-6, float(alerts_per_game) / max(1.0, float(rows_per_game)))
    allowed = alert_rate <= target_rate
    index = int(np.argmax(np.where(allowed, recall, -1.0)))
    if not bool(allowed.any()):
        index = 0  # 预算比「只报最像的那 1 条」还紧：取风险最高的单个点
    return {
        "threshold": float(prob[order][index]),
        "precision": float(precision[index]),
        "recall": float(recall[index]),
        "alertRate": float(alert_rate[index]),
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="计算并审计事件模型 OOF 最优 F1 阈值")
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--alert-budget-per-game", type=float, default=8.0,
        help="每局最多几次事件提醒（预算口径；默认 8 次/局）",
    )
    args = parser.parse_args(argv)

    metrics = json.loads(args.metrics.read_text(encoding="utf-8"))
    results = metrics["results"]
    rows_total = int(metrics.get("rows") or 0)

    print(f"metrics: {args.metrics}")
    print(f"rows(训练样本数): {rows_total}")

    # ---- 1. 提取 OOF 阈值并审计一致性 ----
    labels_out: Dict[str, Dict[str, Any]] = {}
    for label in LABELS:
        item = results[label]
        threshold = float(item["bestThreshold"])
        f1 = float(item["bestF1"])
        precision = float(item["bestPrecision"])
        recall = float(item["bestRecall"])
        positive = int(item["positive"])
        positive_rate = float(item["positiveRate"])
        check(
            f"{label}: 阈值在 (0,1)（{threshold:.4f}）",
            0.0 < threshold < 1.0,
            str(threshold),
        )
        f1_recomputed = 2.0 * precision * recall / max(precision + recall, 1e-12)
        check(
            f"{label}: F1=2PR/(P+R) 一致（{f1:.4f} vs {f1_recomputed:.4f}）",
            abs(f1 - f1_recomputed) < 1e-4,
            f"{f1} vs {f1_recomputed}",
        )
        check(
            f"{label}: F1 > 基线 AP（{f1:.4f} > {positive_rate:.4f}）",
            f1 > positive_rate,
            f"{f1} vs {positive_rate}",
        )
        labels_out[label] = {
            "threshold": round(threshold, 6),
            "bestF1": round(f1, 6),
            "bestPrecision": round(precision, 6),
            "bestRecall": round(recall, 6),
            "positive": positive,
            "positiveRate": round(positive_rate, 6),
        }

    # ---- 2. in-sample 参考阈值 + 预算口径（需要 live_dataset.csv；缺失则跳过）----
    if not args.dataset.is_file():
        print(f"跳过 in-sample / 预算阈值：找不到数据集 {args.dataset}")
        for label in LABELS:
            labels_out[label]["budgetThreshold"] = None
    else:
        print("计算 in-sample 参考阈值（最终模型 + 每 10 行抽样，批量推理）...")
        models = {label: LgbTextModel(args.metrics.parent / f"model_{label}.txt") for label in LABELS}
        feature_names = metrics["features"]
        columns = list(feature_names) + list(LABELS)
        rows_batch: List[List[float]] = []
        n = 0
        with args.dataset.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader):
                if index % IN_SAMPLE_STRIDE != 0:
                    continue
                n += 1
                values = []
                for column in columns:
                    if column == "role":
                        values.append(float(ROLE_CODES.get(str(row.get(column, "")), 0.0)))
                    else:
                        try:
                            values.append(float(row.get(column) or 0.0))
                        except ValueError:
                            values.append(0.0)
                rows_batch.append(values)
        matrix = np.array(rows_batch, dtype=np.float64)
        x = matrix[:, : len(feature_names)]
        games = int(metrics.get("games") or 0) or 1
        rows_per_game = n / float(games)
        for label in LABELS:
            probability = models[label].predict_proba(x)
            y = matrix[:, len(feature_names) + LABELS.index(label)]
            result = best_f1_threshold(y, probability)
            labels_out[label]["inSampleThreshold"] = round(result["threshold"], 6)
            labels_out[label]["inSampleF1"] = round(result["f1"], 6)
            labels_out[label]["inSampleRows"] = n
            check(
                f"{label}: in-sample 阈值有界（{result['threshold']:.4f}）",
                0.0 < result["threshold"] < 1.0,
                str(result["threshold"]),
            )
            budget = budget_threshold(y, probability, args.alert_budget_per_game, rows_per_game)
            labels_out[label]["budgetThreshold"] = round(budget["threshold"], 6)
            labels_out[label]["budgetPrecision"] = round(budget["precision"], 6)
            labels_out[label]["budgetRecall"] = round(budget["recall"], 6)
            labels_out[label]["budgetAlertRate"] = round(budget["alertRate"], 6)
            print(
                f"  {label:18s} OOF阈值={labels_out[label]['threshold']:.4f} "
                f"F1={labels_out[label]['bestF1']:.4f} "
                f"inSample={result['threshold']:.4f} "
                f"预算阈值={budget['threshold']:.4f} "
                f"(P={budget['precision']:.3f} R={budget['recall']:.3f} "
                f"触发率={budget['alertRate']:.1%})"
            )

    # ---- 3. 落盘 ----
    payload = {
        "meta": {
            "computedBy": "compute_event_thresholds.py",
            "thresholdSource": (
                "train_live_event_model.py 训练时由 OOF 预测（GroupKFold 5, gameId）"
                "的 precision_recall_curve argmax F1 计算，存于 live_event_model/metrics.json"
            ),
            "inSampleSource": (
                f"最终模型对 live_dataset.csv 每 {IN_SAMPLE_STRIDE} 行抽样的 in-sample "
                "argmax F1 阈值（最终模型见过训练数据，为乐观上界，仅审计对照）"
            ),
            "usage": (
                "运行时的触发口径（T60）：probability >= threshold "
                "且连续 confirmations 次刷新都比对方同类事件高出 minGap，"
                "同一类提醒间隔不小于 cooldownSeconds；预算见 policy.alertBudgetPerGame"
            ),
            "rows": rows_total,
            "labels": list(LABELS),
            "generatedAt": datetime.now().astimezone().isoformat(),
        },
        "policy": {
            "minGap": 0.10,
            "confirmations": 2,
            "cooldownSeconds": 180,
            "alertBudgetPerGame": float(args.alert_budget_per_game),
            "note": (
                "阈值按 OOF 最优 F1 保留兼容；budgetThreshold 为预算口径"
                "（每局最多 alertBudgetPerGame 次）的参考值，需在拿到训练 CSV 后复算"
            ),
        },
        "labels": labels_out,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"输出: {args.out}")

    print(f"\n结果: PASS={passed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
