#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 v3（含 OP.GG 特征）胜率模型：OOF 相关 + AUC + 逐分钟 AUC。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lgbmini import LgbTextModel  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def auc_from_ranks(y: np.ndarray, score: np.ndarray) -> float:
    """ROC-AUC = (正样本秩和 - n1(n1+1)/2) / (n1*n0)，**同分取平均秩**。

    同分很常见（同一批特征落在同一个叶子），按行号给秩会让 AUC 依赖 CSV 的行
    顺序；这里按并列组取平均秩，等价于 sklearn 的 roc_auc_score。
    """
    y = np.asarray(y, dtype=bool)
    score = np.asarray(score, dtype=np.float64)
    n1, n0 = int(y.sum()), int((~y).sum())
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()

    started = time.time()
    df = pd.read_csv(args.dataset, dtype={"role": str})
    print(f"rows={len(df)} elapsed={time.time()-started:.1f}s")

    model = LgbTextModel(args.model_dir / "model.txt")
    print(f"model: trees={model.num_trees} features={len(model.feature_names)}")
    print("features:", model.feature_names)

    missing = [f for f in model.feature_names if f not in df.columns]
    if missing:
        print(f"CSV 缺少特征: {missing}")
        return 1

    xdf = df[model.feature_names].copy()
    if "role" in xdf.columns:
        from live_features import ROLE_CODES

        xdf["role"] = xdf["role"].map(lambda r: float(ROLE_CODES.get(str(r), 0.0)))
    x = xdf.to_numpy(dtype=np.float64)
    prob = model.predict_proba(x)
    y = df["ownWin"].to_numpy(dtype=bool)

    auc = auc_from_ranks(y, prob)
    print(f"最终模型全量 AUC: {auc:.4f} (旧基线 OOF=0.8272)")

    oof_path = args.model_dir / "oof_predictions.csv"
    if oof_path.is_file():
        oof = pd.read_csv(oof_path)
        corr = np.corrcoef(prob, oof["probability"].to_numpy())[0, 1]
        mae = float(np.mean(np.abs(prob - oof["probability"].to_numpy())))
        print(f"最终模型 vs OOF: pearson={corr:.6f} mean|diff|={mae:.6f}")

    per_minute = {}
    for minute, group in df.groupby("minute"):
        if len(group) < 100:
            continue
        m = df["minute"].to_numpy() == minute
        per_minute[int(minute)] = auc_from_ranks(y[m], prob[m])
    print("per-minute AUC (前 20):", {k: round(v, 3) for k, v in list(sorted(per_minute.items()))[:20]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
