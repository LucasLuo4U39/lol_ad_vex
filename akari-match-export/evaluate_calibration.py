#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Win-probability calibration evaluation (standalone CLI + reusable helpers).

只依赖 numpy 与标准库（不依赖 pandas/sklearn），因此可以在训练环境、CI 和
临时机器上直接复跑。

输入一份预测 CSV（至少含 ``y`` 与 ``probability``；可选 ``minute``、``gameId``），
输出：

  * Brier / LogLoss / AUC（秩和实现，同分取平均秩）
  * ECE（等频分箱）与最大分箱偏差（MCE）
  * 每箱样本数、平均预测、实际正率
  * 按 minute 的 ECE / AUC 表（有 minute 列时）
  * 按 gameId 的 bootstrap 置信区间（有 gameId 列时）
  * 可选 isotonic / Platt 校准后的指标（``--apply``）

用法示例::

    python evaluate_calibration.py --predictions oof_predictions.csv \
        --out calibration_eval.json --out-md calibration_eval.md

    python evaluate_calibration.py --predictions oof_predictions.csv \
        --apply isotonic --out calibration_eval.json

`train_live_model.py` 复用 :func:`calibration_snapshot` 把校准块写进 metrics.json。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EPS = 1e-15
PROBABILITY_ALIASES = ("probability", "prob", "p", "prediction", "predicted")
LABEL_ALIASES = ("y", "label", "target", "ownwin")
MINUTE_ALIASES = ("minute", "game_minute", "gameminute")
GAME_ALIASES = ("gameid", "game_id", "matchid", "match_id")


# ---------------------------------------------------------------------------
# Basic metrics
# ---------------------------------------------------------------------------


def _as_arrays(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """返回 (0/1 标签, 概率) 的一维数组；丢弃非有限值。"""
    y = np.asarray(y_true, dtype=float).reshape(-1)
    p = np.asarray(probabilities, dtype=float).reshape(-1)

    if y.size != p.size:
        raise ValueError(f"标签与概率长度不一致: {y.size} != {p.size}")

    mask = np.isfinite(y) & np.isfinite(p)
    y = y[mask]
    p = p[mask]

    if y.size == 0:
        raise ValueError("没有有效样本（y / probability 全为空或非数值）")

    if p.min() < 0.0 or p.max() > 1.0:
        raise ValueError(f"probability 必须落在 [0, 1]，实际为 [{p.min()}, {p.max()}]")

    labels = np.where(y >= 0.5, 1.0, 0.0)
    return labels, p


def average_ranks(values: Sequence[float] | np.ndarray) -> np.ndarray:
    """1 基平均秩（并列值取平均秩）。"""
    values = np.asarray(values, dtype=float).reshape(-1)
    n = values.size
    order = np.argsort(values, kind="mergesort")
    sorted_values = values[order]
    ranks = np.empty(n, dtype=float)

    i = 0
    while i < n:
        j = i
        while j + 1 < n and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        ranks[order[i : j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1

    return ranks


def auc_score(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
) -> float:
    """秩和（Mann-Whitney）AUC；并列预测取平均秩，全并列时为 0.5。"""
    y = np.asarray(y_true, dtype=float).reshape(-1)
    p = np.asarray(probabilities, dtype=float).reshape(-1)

    if y.size != p.size:
        raise ValueError(f"标签与概率长度不一致: {y.size} != {p.size}")
    if y.size == 0:
        return float("nan")

    positive = y >= 0.5
    n_positive = int(positive.sum())
    n_negative = int(positive.size - n_positive)

    if n_positive == 0 or n_negative == 0:
        return float("nan")

    ranks = average_ranks(p)
    rank_sum = float(ranks[positive].sum())
    return float(
        (rank_sum - n_positive * (n_positive + 1) / 2.0) / (n_positive * n_negative)
    )


def log_loss_score(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    eps: float = EPS,
) -> float:
    y = np.asarray(y_true, dtype=float).reshape(-1)
    p = np.clip(np.asarray(probabilities, dtype=float).reshape(-1), eps, 1.0 - eps)
    if y.size == 0:
        return float("nan")
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def brier_score(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
) -> float:
    y = np.asarray(y_true, dtype=float).reshape(-1)
    p = np.asarray(probabilities, dtype=float).reshape(-1)
    if y.size == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


# ---------------------------------------------------------------------------
# Reliability (equal-frequency bins) and ECE
# ---------------------------------------------------------------------------


def equal_frequency_edges(
    probabilities: Sequence[float] | np.ndarray, bins: int = 10
) -> np.ndarray:
    """按预测分布的分位数切箱（等频），返回去重后的升序边界。"""
    p = np.asarray(probabilities, dtype=float).reshape(-1)
    p = p[np.isfinite(p)]
    bins = max(1, int(bins))

    if p.size == 0:
        return np.array([0.0, 1.0], dtype=float)

    edges = np.unique(np.quantile(p, np.linspace(0.0, 1.0, bins + 1)))
    if edges.size < 2:
        return np.array([0.0, 1.0], dtype=float)
    return edges


def reliability_table(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    bins: int = 10,
    edges: Optional[np.ndarray] = None,
) -> List[Dict[str, Any]]:
    """等频分箱的可靠性表；空箱自动跳过。"""
    y, p = _as_arrays(y_true, probabilities)

    if edges is None:
        edges = equal_frequency_edges(p, bins)
    edges = np.asarray(edges, dtype=float)
    if edges.size < 2:
        edges = np.array([0.0, 1.0], dtype=float)

    indices = np.searchsorted(edges[1:-1], p, side="right")
    rows: List[Dict[str, Any]] = []

    for index in range(edges.size - 1):
        mask = indices == index
        count = int(mask.sum())
        if count == 0:
            continue

        mean_probability = float(p[mask].mean())
        actual = float(y[mask].mean())
        rows.append(
            {
                "bin": index,
                "lower": float(edges[index]),
                "upper": float(edges[index + 1]),
                "count": count,
                "share": count / p.size,
                "meanProbability": mean_probability,
                "actualWinRate": actual,
                "gap": abs(mean_probability - actual),
                "signedGap": mean_probability - actual,
            }
        )

    return rows


def calibration_error(
    rows: Sequence[Dict[str, Any]]
) -> Tuple[float, float]:
    """由可靠性表算 ECE 与最大分箱偏差（MCE）。"""
    if not rows:
        return 0.0, 0.0

    total = sum(row["count"] for row in rows)
    if total == 0:
        return 0.0, 0.0

    ece = sum(row["count"] / total * row["gap"] for row in rows)
    mce = max(row["gap"] for row in rows)
    return float(ece), float(mce)


def evaluate_binary(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    bins: int = 10,
    edges: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """单组预测的完整指标（含可靠性表汇总值）。"""
    y, p = _as_arrays(y_true, probabilities)
    rows = reliability_table(y, p, bins=bins, edges=edges)
    ece, mce = calibration_error(rows)
    mean_probability = float(p.mean())
    actual = float(y.mean())

    # 额外给一个固定 [0, 1] 等宽分箱 ECE，便于和 README / calibrate_model.py
    # 的历史数字（等宽分箱）对照；主指标仍是上面的等频 ECE。
    width_rows = reliability_table(
        y, p, bins=max(2, int(bins)), edges=np.linspace(0.0, 1.0, max(2, int(bins)) + 1)
    )
    ece_equal_width, _ = calibration_error(width_rows)

    return {
        "rows": int(p.size),
        "positives": int(y.sum()),
        "auc": auc_score(y, p),
        "logLoss": log_loss_score(y, p),
        "brier": brier_score(y, p),
        "ece": ece,
        "maxBinDeviation": mce,
        "eceEqualWidth": ece_equal_width,
        "meanProbability": mean_probability,
        "actualWinRate": actual,
        "bias": mean_probability - actual,
    }


def per_minute_table(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    minutes: Optional[Sequence[float] | np.ndarray],
    bins: int = 10,
) -> List[Dict[str, Any]]:
    """按 minute 的 AUC / ECE 表；缺少 minute 时返回空列表。"""
    if minutes is None:
        return []

    y = np.asarray(y_true, dtype=float).reshape(-1)
    p = np.asarray(probabilities, dtype=float).reshape(-1)
    minute_array = np.asarray(minutes, dtype=float).reshape(-1)
    if minute_array.size != y.size:
        raise ValueError(
            f"minute 长度不一致: {minute_array.size} != {y.size}"
        )

    rows: List[Dict[str, Any]] = []
    for minute in sorted(set(minute_array[np.isfinite(minute_array)].tolist())):
        mask = minute_array == minute
        count = int(mask.sum())
        if count == 0:
            continue

        minute_bins = max(2, min(int(bins), count))
        item = evaluate_binary(y[mask], p[mask], bins=minute_bins)
        item["minute"] = int(minute) if float(minute).is_integer() else float(minute)
        rows.append(item)

    return rows


# ---------------------------------------------------------------------------
# Calibrators (numpy only): isotonic (PAVA) and Platt scaling
# ---------------------------------------------------------------------------


def logit(probabilities: Sequence[float] | np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(probabilities, dtype=float).reshape(-1), 1e-6, 1.0 - 1e-6)
    return np.log(p / (1.0 - p))


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    output = np.empty_like(values)
    positive = values >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exp_values = np.exp(values[~positive])
    output[~positive] = exp_values / (1.0 + exp_values)
    return output


def _pava(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Pool Adjacent Violators，返回与 *values* 等长的单调不减拟合值。"""
    blocks: List[List[float]] = []  # [加权和, 权重和, 计数]

    for value, weight in zip(values, weights):
        blocks.append([float(value) * float(weight), float(weight), 1.0])
        while len(blocks) >= 2:
            left_mean = blocks[-2][0] / blocks[-2][1]
            right_mean = blocks[-1][0] / blocks[-1][1]
            if left_mean <= right_mean:
                break
            right = blocks.pop()
            left = blocks.pop()
            blocks.append(
                [left[0] + right[0], left[1] + right[1], left[2] + right[2]]
            )

    fitted: List[float] = []
    for total, weight, count in blocks:
        fitted.extend([total / weight] * int(round(count)))
    return np.asarray(fitted, dtype=float)


def fit_isotonic(
    probabilities: Sequence[float] | np.ndarray,
    y_true: Sequence[float] | np.ndarray,
) -> Dict[str, np.ndarray]:
    """拟合保序回归，返回 ``{"x": 唯一预测值, "y": 拟合概率}``。"""
    y, p = _as_arrays(y_true, probabilities)

    order = np.argsort(p, kind="mergesort")
    sorted_p = p[order]
    sorted_y = y[order]

    unique_x, starts = np.unique(sorted_p, return_index=True)
    sums = np.add.reduceat(sorted_y, starts)
    counts = np.diff(np.append(starts, sorted_p.size))
    means = sums / counts
    fitted = _pava(means, counts)

    return {"x": unique_x, "y": fitted}


def apply_isotonic(
    model: Dict[str, np.ndarray],
    probabilities: Sequence[float] | np.ndarray,
) -> np.ndarray:
    p = np.asarray(probabilities, dtype=float).reshape(-1)
    x = np.asarray(model["x"], dtype=float)
    fitted = np.asarray(model["y"], dtype=float)

    if x.size == 1:
        return np.clip(np.full(p.shape, float(fitted[0])), 0.0, 1.0)

    return np.clip(np.interp(p, x, fitted), 0.0, 1.0)


def fit_platt(
    probabilities: Sequence[float] | np.ndarray,
    y_true: Sequence[float] | np.ndarray,
) -> Dict[str, float]:
    """在 logit(p) 上拟合 Platt scaling（带 Platt 目标平滑的阻尼牛顿法）。"""
    y, p = _as_arrays(y_true, probabilities)
    x = logit(p)

    positives = float(y.sum())
    negatives = float(y.size - positives)
    # Platt 的平滑目标，避免 0/1 过拟合
    targets = np.where(
        y > 0.5,
        (positives + 1.0) / (positives + 2.0),
        1.0 / (negatives + 2.0),
    )

    a = 0.0
    b = math.log((positives + 1.0) / (negatives + 1.0)) if positives and negatives else 0.0

    def loss(weight: float, bias: float) -> float:
        z = weight * x + bias
        q = _sigmoid(z)
        return float(
            -np.mean(
                targets * np.log(np.clip(q, 1e-12, 1.0))
                + (1.0 - targets) * np.log(np.clip(1.0 - q, 1e-12, 1.0))
            )
        )

    current = loss(a, b)
    ridge = 1e-9

    for _ in range(200):
        q = _sigmoid(a * x + b)
        weights = q * (1.0 - q)
        gradient_a = float(np.sum((q - targets) * x))
        gradient_b = float(np.sum(q - targets))
        hessian_aa = float(np.sum(weights * x * x)) + ridge
        hessian_ab = float(np.sum(weights * x))
        hessian_bb = float(np.sum(weights)) + ridge
        determinant = hessian_aa * hessian_bb - hessian_ab * hessian_ab

        if not np.isfinite(determinant) or determinant <= 0:
            break

        step_a = (gradient_a * hessian_bb - gradient_b * hessian_ab) / determinant
        step_b = (gradient_b * hessian_aa - gradient_a * hessian_ab) / determinant
        norm = math.hypot(step_a, step_b)
        if norm > 10.0:
            step_a *= 10.0 / norm
            step_b *= 10.0 / norm

        step_scale = 1.0
        improved = False
        for _ in range(25):
            candidate = loss(a - step_scale * step_a, b - step_scale * step_b)
            if candidate <= current + 1e-12:
                improved = True
                break
            step_scale *= 0.5

        if not improved:
            break

        a -= step_scale * step_a
        b -= step_scale * step_b
        current = candidate

        if max(abs(step_scale * step_a), abs(step_scale * step_b)) < 1e-8:
            break

    return {"a": float(a), "b": float(b)}


def apply_platt(
    model: Dict[str, float],
    probabilities: Sequence[float] | np.ndarray,
) -> np.ndarray:
    p = np.asarray(probabilities, dtype=float).reshape(-1)
    return np.clip(_sigmoid(float(model["a"]) * logit(p) + float(model["b"])), 0.0, 1.0)


def fit_calibrator(
    method: str,
    probabilities: Sequence[float] | np.ndarray,
    y_true: Sequence[float] | np.ndarray,
) -> Dict[str, Any]:
    if method == "isotonic":
        return fit_isotonic(probabilities, y_true)
    if method == "platt":
        return fit_platt(probabilities, y_true)
    raise ValueError(f"未知校准方法: {method}")


def apply_calibrator(
    method: str,
    model: Dict[str, Any],
    probabilities: Sequence[float] | np.ndarray,
) -> np.ndarray:
    if method == "isotonic":
        return apply_isotonic(model, probabilities)
    if method == "platt":
        return apply_platt(model, probabilities)
    raise ValueError(f"未知校准方法: {method}")


def assign_folds(
    size: int,
    groups: Optional[Sequence[Any] | np.ndarray] = None,
    folds: int = 5,
    seed: int = 42,
) -> Tuple[np.ndarray, str]:
    """返回 (每行的折号, 分折依据)；有 groups 时按组整体分折。"""
    size = int(size)
    if size < 2:
        raise ValueError("至少需要 2 行才能做交叉拟合")

    if groups is not None:
        group_array = np.asarray(groups)
        if group_array.size != size:
            raise ValueError(
                f"gameId 长度不一致: {group_array.size} != {size}"
            )

        unique_groups, first_index = np.unique(group_array, return_index=True)
        if unique_groups.size < 2:
            raise ValueError("至少需要 2 个 gameId 才能按局交叉拟合")

        unique_groups = unique_groups[np.argsort(first_index)]
        fold_count = max(2, min(int(folds), int(unique_groups.size)))
        rng = np.random.default_rng(seed)
        order = rng.permutation(unique_groups.size)
        fold_of_group = np.empty(unique_groups.size, dtype=int)
        fold_of_group[order] = np.arange(unique_groups.size) % fold_count
        mapping = dict(zip(unique_groups.tolist(), fold_of_group.tolist()))
        return np.asarray([mapping[group] for group in group_array.tolist()], dtype=int), "gameId"

    fold_count = max(2, min(int(folds), size))
    rng = np.random.default_rng(seed)
    fold_ids = np.empty(size, dtype=int)
    fold_ids[rng.permutation(size)] = np.arange(size) % fold_count
    return fold_ids, "row"


def cross_fit_calibrate(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    groups: Optional[Sequence[Any] | np.ndarray] = None,
    method: str = "isotonic",
    folds: int = 5,
    seed: int = 42,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """K 折交叉拟合：每折的校准器只在其余折上拟合。"""
    y, p = _as_arrays(y_true, probabilities)
    fold_ids, folded_by = assign_folds(len(p), groups=groups, folds=folds, seed=seed)

    calibrated = np.full(len(p), np.nan, dtype=float)
    fold_count = int(fold_ids.max()) + 1

    for fold in range(fold_count):
        valid = fold_ids == fold
        train = ~valid
        if not valid.any():
            continue

        model = fit_calibrator(method, p[train], y[train])
        calibrated[valid] = apply_calibrator(method, model, p[valid])

    if np.isnan(calibrated).any():
        raise RuntimeError("交叉拟合存在未覆盖的样本")

    meta = {
        "method": method,
        "fit": "cross-fit",
        "foldedBy": folded_by,
        "folds": fold_count,
        "seed": int(seed),
    }
    return calibrated, meta


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


@dataclass
class PredictionData:
    y: np.ndarray
    probability: np.ndarray
    minute: Optional[np.ndarray]
    gameId: Optional[np.ndarray]
    rows: int
    skipped: int
    path: str


def _resolve_column(fieldnames: Sequence[str], aliases: Sequence[str]) -> Optional[str]:
    lookup = {name.strip().lower(): name for name in fieldnames if name}
    for alias in aliases:
        if alias in lookup:
            return lookup[alias]
    return None


def load_predictions(path: str | Path) -> PredictionData:
    """读取预测 CSV；列名大小写不敏感，并兼容常见别名。"""
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ValueError(f"预测文件不存在: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        label_column = _resolve_column(fieldnames, LABEL_ALIASES)
        probability_column = _resolve_column(fieldnames, PROBABILITY_ALIASES)

        if label_column is None or probability_column is None:
            raise ValueError(
                "预测 CSV 至少需要 y 与 probability 列，实际列: "
                + ", ".join(fieldnames)
            )

        minute_column = _resolve_column(fieldnames, MINUTE_ALIASES)
        game_column = _resolve_column(fieldnames, GAME_ALIASES)

        labels: List[float] = []
        probabilities: List[float] = []
        minutes: List[float] = []
        games: List[str] = []
        skipped = 0
        row_index = 0

        for record in reader:
            row_index += 1
            try:
                label = float(str(record.get(label_column, "")).strip())
                probability = float(str(record.get(probability_column, "")).strip())
            except (TypeError, ValueError):
                skipped += 1
                continue

            if not (math.isfinite(label) and math.isfinite(probability)):
                skipped += 1
                continue

            labels.append(label)
            probabilities.append(probability)

            if minute_column is not None:
                raw_minute = str(record.get(minute_column, "")).strip()
                try:
                    minutes.append(float(raw_minute))
                except (TypeError, ValueError):
                    minutes.append(float("nan"))
            else:
                minutes.append(float("nan"))

            if game_column is not None:
                raw_game = str(record.get(game_column, "")).strip()
                games.append(raw_game if raw_game else f"__row_{row_index}")
            else:
                games.append(f"__row_{row_index}")

    if not labels:
        raise ValueError("预测 CSV 没有可用数据行")

    label_array = np.asarray(labels, dtype=float)
    probability_array = np.asarray(probabilities, dtype=float)

    if probability_array.min() < 0.0 or probability_array.max() > 1.0:
        raise ValueError(
            f"probability 必须落在 [0, 1]，实际为 "
            f"[{probability_array.min()}, {probability_array.max()}]"
        )

    unique_labels = set(np.unique(label_array).tolist())
    if not unique_labels.issubset({0.0, 1.0}):
        raise ValueError(f"y 必须是 0/1，实际出现: {sorted(unique_labels)}")

    return PredictionData(
        y=label_array,
        probability=probability_array,
        minute=np.asarray(minutes, dtype=float) if minute_column is not None else None,
        gameId=np.asarray(games, dtype=object) if game_column is not None else None,
        rows=len(labels),
        skipped=skipped,
        path=str(csv_path),
    )


# ---------------------------------------------------------------------------
# Bootstrap / report
# ---------------------------------------------------------------------------


def game_bootstrap(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    games: Sequence[Any] | np.ndarray,
    bins: int = 10,
    replicates: int = 200,
    seed: int = 42,
    alpha: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """按局重采样（有放回）的 bootstrap 置信区间。"""
    y, p = _as_arrays(y_true, probabilities)
    game_array = np.asarray(games)
    if game_array.size != y.size:
        raise ValueError(f"gameId 长度不一致: {game_array.size} != {y.size}")

    order = np.argsort(game_array, kind="mergesort")
    sorted_games = game_array[order]
    boundaries = np.flatnonzero(sorted_games[1:] != sorted_games[:-1]) + 1
    chunks = np.split(order, boundaries)
    game_count = len(chunks)

    if game_count < 2 or replicates <= 0:
        return None

    rng = np.random.default_rng(seed)
    metrics: Dict[str, List[float]] = {"auc": [], "ece": [], "brier": []}

    for _ in range(int(replicates)):
        picked = rng.integers(0, game_count, game_count)
        indices = np.concatenate([chunks[index] for index in picked])
        sample_y = y[indices]
        sample_p = p[indices]

        metrics["auc"].append(auc_score(sample_y, sample_p))
        rows = reliability_table(sample_y, sample_p, bins=bins)
        metrics["ece"].append(calibration_error(rows)[0])
        metrics["brier"].append(brier_score(sample_y, sample_p))

    intervals: Dict[str, Any] = {}
    for name, values in metrics.items():
        array = np.asarray(values, dtype=float)
        array = array[np.isfinite(array)]
        if array.size == 0:
            intervals[name] = None
            continue
        intervals[name] = {
            "low": float(np.quantile(array, alpha / 2.0)),
            "high": float(np.quantile(array, 1.0 - alpha / 2.0)),
            "median": float(np.median(array)),
        }

    return {
        "games": int(game_count),
        "replicates": int(replicates),
        "seed": int(seed),
        "confidence": float(1.0 - alpha),
        "intervals": intervals,
    }


def _calibration_block(
    y: np.ndarray,
    p: np.ndarray,
    minute: Optional[np.ndarray],
    bins: int,
) -> Dict[str, Any]:
    rows = reliability_table(y, p, bins=bins)
    return {
        "overall": evaluate_binary(y, p, bins=bins),
        "reliability": rows,
        "perMinute": per_minute_table(y, p, minute, bins=bins),
    }


def build_report(
    data: PredictionData,
    bins: int = 10,
    bootstrap_replicates: int = 200,
    seed: int = 42,
    apply: str = "none",
    fit_data: Optional[PredictionData] = None,
    folds: int = 5,
) -> Dict[str, Any]:
    """组装结构化评估报告（可直接 json.dump）。"""
    y = data.y
    p = data.probability
    minute = data.minute

    raw_block = _calibration_block(y, p, minute, bins)
    report: Dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "predictions": data.path,
        "rows": int(y.size),
        "skippedRows": int(data.skipped),
        "binning": {"method": "equal-frequency", "bins": int(bins)},
        "overall": raw_block["overall"],
        "reliability": raw_block["reliability"],
        "perMinute": raw_block["perMinute"],
        "bootstrap": None,
        "calibrated": None,
    }

    if data.gameId is not None:
        report["bootstrap"] = game_bootstrap(
            y,
            p,
            data.gameId,
            bins=bins,
            replicates=bootstrap_replicates,
            seed=seed,
        )

    if apply != "none":
        if fit_data is not None:
            model = fit_calibrator(apply, fit_data.probability, fit_data.y)
            calibrated = apply_calibrator(apply, model, p)
            meta: Dict[str, Any] = {
                "method": apply,
                "fit": "external",
                "fitPredictions": fit_data.path,
                "fitRows": int(fit_data.y.size),
            }
        else:
            calibrated, meta = cross_fit_calibrate(
                y, p, groups=data.gameId, method=apply, folds=folds, seed=seed
            )
            meta["fit"] = "cross-fit"

        calibrated_block = _calibration_block(y, calibrated, minute, bins)
        calibrated_block["method"] = apply
        calibrated_block.update(meta)
        calibrated_block["delta"] = {
            "ece": calibrated_block["overall"]["ece"] - report["overall"]["ece"],
            "maxBinDeviation": calibrated_block["overall"]["maxBinDeviation"]
            - report["overall"]["maxBinDeviation"],
            "brier": calibrated_block["overall"]["brier"] - report["overall"]["brier"],
            "logLoss": calibrated_block["overall"]["logLoss"] - report["overall"]["logLoss"],
            "auc": calibrated_block["overall"]["auc"] - report["overall"]["auc"],
        }
        report["calibrated"] = calibrated_block

    return to_jsonable(report)


def to_jsonable(value: Any) -> Any:
    """把 numpy 标量 / NaN / Inf 转成严格 JSON 可序列化的值。"""
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return [to_jsonable(item) for item in value.tolist()]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if value is None or isinstance(value, str):
        return value
    return str(value)


def calibration_snapshot(
    y_true: Sequence[float] | np.ndarray,
    probabilities: Sequence[float] | np.ndarray,
    bins: int = 10,
    minute: Optional[Sequence[float] | np.ndarray] = None,
) -> Dict[str, Any]:
    """供 train_live_model.py 复用：返回 metrics.json 里的 calibration 块。"""
    y, p = _as_arrays(y_true, probabilities)
    if minute is not None:
        minute_array = np.asarray(minute, dtype=float).reshape(-1)
        if minute_array.size != y.size:
            minute_array = None
    else:
        minute_array = None

    block = _calibration_block(y, p, minute_array, bins)
    block["binning"] = {"method": "equal-frequency", "bins": int(bins)}
    return to_jsonable(block)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _format_metric(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def render_markdown(report: Dict[str, Any]) -> str:
    overall = report["overall"]
    lines: List[str] = [
        "# 概率校准评估",
        "",
        f"- 预测文件: `{report['predictions']}`",
        f"- 样本: {report['rows']} 行（跳过 {report.get('skippedRows', 0)} 行）",
        f"- 分箱: 等频 {report['binning']['bins']} 箱",
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| AUC | {_format_metric(overall['auc'])} |",
        f"| LogLoss | {_format_metric(overall['logLoss'])} |",
        f"| Brier | {_format_metric(overall['brier'])} |",
        f"| ECE | {_format_metric(overall['ece'])} |",
        f"| 最大分箱偏差 | {_format_metric(overall['maxBinDeviation'])} |",
        f"| 平均预测 | {_format_metric(overall['meanProbability'])} |",
        f"| 实际正率 | {_format_metric(overall['actualWinRate'])} |",
        f"| 偏差（预测-实际） | {overall['bias']:+.4f} |",
        "",
        "## 可靠性曲线（等频分箱）",
        "",
        "| 箱 | 区间 | n | 平均预测 | 实际正率 | 偏差 |",
        "|---:|---|---:|---:|---:|---:|",
    ]

    for row in report["reliability"]:
        lines.append(
            f"| {row['bin']} | [{row['lower']:.3f}, {row['upper']:.3f}] | "
            f"{row['count']} | {row['meanProbability']:.4f} | "
            f"{row['actualWinRate']:.4f} | {row['gap']:.4f} |"
        )

    lines.extend(["", "## 按分钟", ""])
    per_minute = report.get("perMinute") or []
    if per_minute:
        lines.extend(
            [
                "| 分钟 | n | AUC | ECE | 平均预测 | 实际正率 |",
                "|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in per_minute:
            lines.append(
                f"| {row['minute']} | {row['rows']} | {_format_metric(row['auc'])} | "
                f"{_format_metric(row['ece'])} | {row['meanProbability']:.4f} | "
                f"{row['actualWinRate']:.4f} |"
            )
    else:
        lines.append("（无 minute 列，跳过按分钟表）")

    bootstrap = report.get("bootstrap")
    if bootstrap:
        intervals = bootstrap.get("intervals") or {}
        lines.extend(
            [
                "",
                "## 按局 Bootstrap 95% CI",
                "",
                f"重采样 {bootstrap['replicates']} 次，覆盖 {bootstrap['games']} 局。",
                "",
                "| 指标 | 2.5% | 中位数 | 97.5% |",
                "|---|---:|---:|---:|",
            ]
        )
        for name in ("auc", "ece", "brier"):
            interval = intervals.get(name)
            if not interval:
                continue
            lines.append(
                f"| {name} | {interval['low']:.4f} | {interval['median']:.4f} | "
                f"{interval['high']:.4f} |"
            )

    calibrated = report.get("calibrated")
    if calibrated:
        lines.extend(
            [
                "",
                f"## 校准后（{calibrated.get('method')}）",
                "",
                "| 方法 | AUC | LogLoss | Brier | ECE |",
                "|---|---:|---:|---:|---:|",
                f"| raw | {_format_metric(overall['auc'])} | "
                f"{_format_metric(overall['logLoss'])} | "
                f"{_format_metric(overall['brier'])} | "
                f"{_format_metric(overall['ece'])} |",
                f"| {calibrated.get('method')} | "
                f"{_format_metric(calibrated['overall']['auc'])} | "
                f"{_format_metric(calibrated['overall']['logLoss'])} | "
                f"{_format_metric(calibrated['overall']['brier'])} | "
                f"{_format_metric(calibrated['overall']['ece'])} |",
                "",
                f"ECE 变化: {calibrated['delta']['ece']:+.4f}；"
                f"Brier 变化: {calibrated['delta']['brier']:+.4f}。",
            ]
        )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估胜率预测的校准质量（ECE / 可靠性曲线 / 分分钟 / bootstrap）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        help="预测 CSV（至少含 y 与 probability；可选 minute、gameId）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出 JSON 路径，默认是 <predictions 同目录>/calibration_eval.json",
    )
    parser.add_argument("--bins", type=int, default=10, help="等频分箱数量")
    parser.add_argument("--out-md", type=Path, default=None, help="可选：输出 Markdown 报告")
    parser.add_argument(
        "--apply",
        choices=("none", "isotonic", "platt"),
        default="none",
        help="是否拟合并应用校准器",
    )
    parser.add_argument(
        "--fit-predictions",
        type=Path,
        default=None,
        help="用于拟合校准器的另一份 CSV；省略时在同一份 CSV 上按行/按局交叉拟合",
    )
    parser.add_argument("--folds", type=int, default=5, help="交叉拟合折数")
    parser.add_argument("--seed", type=int, default=42, help="交叉拟合与 bootstrap 随机种子")
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=200,
        help="按 gameId 的 bootstrap 重采样次数（0 表示关闭）",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    if not arguments:
        parser.print_help()
        return 0

    args = parser.parse_args(arguments)

    if args.predictions is None:
        parser.error("--predictions 是必填参数（无参数运行可查看用法）")
        return 2
    if args.bins < 2:
        parser.error("--bins 必须 >= 2")
    if args.folds < 2:
        parser.error("--folds 必须 >= 2")
    if args.bootstrap < 0:
        parser.error("--bootstrap 不能为负数")

    try:
        data = load_predictions(args.predictions)
        fit_data = (
            load_predictions(args.fit_predictions)
            if args.fit_predictions is not None
            else None
        )
    except (OSError, ValueError) as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1

    if fit_data is not None and args.apply == "none":
        print("[提示] 指定了 --fit-predictions 但 --apply none，忽略拟合数据", file=sys.stderr)
        fit_data = None

    if fit_data is not None:
        same_file = Path(fit_data.path).resolve() == Path(data.path).resolve()
        if same_file:
            print(
                "[提示] --fit-predictions 与 --predictions 相同，改用交叉拟合避免自校准",
                file=sys.stderr,
            )
            fit_data = None

    try:
        report = build_report(
            data,
            bins=args.bins,
            bootstrap_replicates=args.bootstrap,
            seed=args.seed,
            apply=args.apply,
            fit_data=fit_data,
            folds=args.folds,
        )
    except (ValueError, RuntimeError, FloatingPointError) as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1

    output_path = args.out or (args.predictions.parent / "calibration_eval.json")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        if args.out_md is not None:
            args.out_md.parent.mkdir(parents=True, exist_ok=True)
            args.out_md.write_text(render_markdown(report), encoding="utf-8")
    except OSError as error:
        print(f"[错误] 写入输出失败: {error}", file=sys.stderr)
        return 1

    overall = report["overall"]
    print(f"Rows: {report['rows']}  (skipped {report['skippedRows']})")
    print(
        f"AUC={_format_metric(overall['auc'])} "
        f"LogLoss={_format_metric(overall['logLoss'])} "
        f"Brier={_format_metric(overall['brier'])} "
        f"ECE={_format_metric(overall['ece'])} "
        f"MCE={_format_metric(overall['maxBinDeviation'])}"
    )
    print(f"Bias(meanPred-actual)={overall['bias']:+.4f}")

    calibrated = report.get("calibrated")
    if calibrated:
        item = calibrated["overall"]
        print(
            f"{calibrated['method']}: AUC={_format_metric(item['auc'])} "
            f"Brier={_format_metric(item['brier'])} "
            f"ECE={_format_metric(item['ece'])} "
            f"(ΔECE {calibrated['delta']['ece']:+.4f})"
        )

    print(f"Output: {output_path}")
    if args.out_md is not None:
        print(f"Markdown: {args.out_md}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
