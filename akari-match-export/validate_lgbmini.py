#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate lgbmini against LightGBM artifacts shipped in the project.

Checks:
1. live_model2: prediction vs saved OOF (oof_predictions.csv) correlation,
   and AUC vs y (expected >= 0.82, slightly higher than OOF 0.8272 since the
   final model saw all data).
2. The four live event models: AUC on live_dataset.csv vs the metrics.json
   OOF ROC-AUC values (final model should be a bit higher).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from lgbmini import LgbTextModel

LIVE = Path(__file__).resolve().parent.parent / "dataset" / "live_dataset"


def auc_from_ranks(y: np.ndarray, score: np.ndarray) -> float:
    """ROC-AUC = (正样本秩和 - n1(n1+1)/2) / (n1*n0)，**同分取平均秩**。

    同分很常见（同一批特征落在同一个叶子），按行号给秩会让 AUC 依赖 CSV 的行
    顺序；这里按并列组取平均秩，等价于 sklearn 的 roc_auc_score。
    """
    y = np.asarray(y, dtype=bool)
    score = np.asarray(score, dtype=np.float64)
    n1 = int(y.sum())
    n0 = int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(score, kind="mergesort")
    _, inverse, counts = np.unique(score[order], return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    starts = ends - counts
    average_ranks = (starts + 1 + ends) / 2.0
    ranks = np.empty(len(score), dtype=np.float64)
    ranks[order] = average_ranks[inverse]
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def main() -> int:
    started = time.time()
    csv_path = LIVE / "live_dataset.csv"
    print(f"读取 {csv_path} ...")
    df = pd.read_csv(csv_path, dtype={"role": str})
    print(f"rows={len(df)} cols={len(df.columns)} elapsed={time.time()-started:.1f}s")

    # ---- 1. live_model2 vs OOF ----------------------------------------
    model = LgbTextModel(LIVE / "live_model2" / "model.txt")
    print(f"\nlive_model2: trees={model.num_trees} features={len(model.feature_names)}")

    missing = [f for f in model.feature_names if f not in df.columns]
    if missing:
        print(f"CSV 缺少特征: {missing}")
        return 1

    xdf = df[model.feature_names].copy()
    if "role" in xdf.columns:
        from live_features import ROLE_CODES

        xdf["role"] = xdf["role"].map(lambda r: float(ROLE_CODES.get(str(r), 0.0)))
    x = xdf.to_numpy(dtype=np.float64)
    print("预测中 ...")
    prob = model.predict_proba(x)
    print(f"预测完成 elapsed={time.time()-started:.1f}s")

    oof = pd.read_csv(LIVE / "live_model2" / "oof_predictions.csv")
    print(f"oof rows={len(oof)}")

    same_order = bool(
        (df["gameId"].to_numpy() == oof["gameId"].to_numpy()).all()
        and (df["minute"].to_numpy() == oof["minute"].to_numpy()).all()
        and (df["role"].to_numpy() == oof["role"].to_numpy()).all()
    )
    print(f"行顺序与 OOF 一致: {same_order}")
    if not same_order:
        print("行顺序不一致，跳过逐行对比")
        return 1

    y = oof["y"].to_numpy(dtype=bool)
    oof_prob = oof["probability"].to_numpy(dtype=np.float64)

    corr = np.corrcoef(prob, oof_prob)[0, 1]
    mae = float(np.mean(np.abs(prob - oof_prob)))
    auc = auc_from_ranks(y, prob)
    print(f"\n[live_model2 验证]")
    print(f"  pearson(最终模型, OOF)  = {corr:.6f}  (期望 > 0.9)")
    print(f"  mean|diff|               = {mae:.6f}")
    print(f"  AUC(最终模型, 全量数据)   = {auc:.4f}  (期望 >= 0.83)")
    print(f"  mean prob                = {prob.mean():.4f}  y 正率 = {y.mean():.4f}")

    per_minute = {}
    for minute in sorted(set(oof["minute"].to_numpy())):
        m = oof["minute"].to_numpy() == minute
        if m.sum() < 100:
            continue
        per_minute[int(minute)] = auc_from_ranks(y[m], prob[m])
    print("  per-minute AUC (前 20):", {k: round(v, 3) for k, v in list(sorted(per_minute.items()))[:20]})

    # ---- 2. event models ----------------------------------------------
    event_metrics = json.loads(
        (LIVE / "live_event_model" / "metrics.json").read_text(encoding="utf-8")
    )
    labels = ["own_kill_30s", "enemy_kill_30s", "own_gank_60s", "enemy_gank_60s"]
    print(f"\n[live_event_model 验证]")
    for label in labels:
        path = LIVE / "live_event_model" / f"model_{label}.txt"
        if not path.is_file():
            continue
        em = LgbTextModel(path)
        exdf = df[em.feature_names].copy()
        if "role" in exdf.columns:
            from live_features import ROLE_CODES

            exdf["role"] = exdf["role"].map(lambda r: float(ROLE_CODES.get(str(r), 0.0)))
        ex = exdf.to_numpy(dtype=np.float64)
        ep = em.predict_proba(ex)
        y_label = df[label].to_numpy(dtype=bool)
        e_auc = auc_from_ranks(y_label, ep)
        reported = event_metrics["results"][label]["rocAuc"]
        print(
            f"  {label:18s}  AUC(最终模型)={e_auc:.4f}  "
            f"metrics(OOF)={reported:.4f}  (期望高于 OOF)"
        )

    print(f"\n总耗时 {time.time()-started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
