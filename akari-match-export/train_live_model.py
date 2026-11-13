#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train a win model using only Live Client Data-feasible features.

Input: live_dataset.csv (one row = one player perspective at one minute).
Label: ownWin (that player's team won).

No enemy gold and no map positions are used.

D3 训练口径（默认开启）：
  * 按局等权：sample_weight = 1 / 该局行数，避免长局后期主导损失；
  * 指标按局给 bootstrap 区间、按分钟给 AUC/Brier/ECE；
  * 记录 patch / trainedAt 元数据，便于发现补丁漂移；
  * ``--drop-rank-features`` 一键剔除赛后快照的 rank* 特征（诚实版本）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

from pathutil import portable_path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EVENT_LABELS = {
    "own_kill_30s",
    "enemy_kill_30s",
    "own_gank_60s",
    "enemy_gank_60s",
}
# patch 是数据集元数据（版本可能漂移），不是每一行可观测的实时特征。
DROP_COLUMNS = {"gameId", "ownWin", "ownTeam", "patch"} | EVENT_LABELS
CATEGORICAL_COLUMNS = {"role"}
RANK_FEATURES = ("ownRank", "teamRank", "enemyRank", "rankDiff", "ownRankVsTeam")
BOOTSTRAP_REPLICATES = 1000

RANK_FEATURE_NOTE = (
    "rank* 特征来自赛后快照（含未来信息），指标会偏乐观；"
    "训练线上诚实版本请加 --drop-rank-features。"
)


def build_features(
    dataframe: pd.DataFrame,
    drop_rank_features: bool = False,
) -> Tuple[pd.DataFrame, List[str], List[str]]:
    """返回 (特征帧, 特征列, 被剔除的段位列)；patch 等元数据列不参与训练。"""
    columns = [column for column in dataframe.columns if column not in DROP_COLUMNS]
    dropped: List[str] = []
    if drop_rank_features:
        dropped = [column for column in columns if column in RANK_FEATURES]
        columns = [column for column in columns if column not in RANK_FEATURES]

    features = dataframe[columns].copy()

    for column in columns:
        if column in CATEGORICAL_COLUMNS:
            features[column] = features[column].astype("category")
        else:
            features[column] = pd.to_numeric(features[column], errors="coerce")

    return features, columns, dropped


def game_sample_weights(dataframe: pd.DataFrame) -> np.ndarray:
    """每行权重 = 1 / 该局行数，使每局在损失里等权。

    整体再除以均值（即归一化到均值 1）：相对权重完全不变，但避免把 LightGBM
    的 reg_lambda / min_sum_hessian 等绝对尺度正则放大 ``平均局行数`` 倍
    （实测 252 行/局时原始 1/n 会让 OOF AUC 从 0.983 掉到 0.956）。
    """
    counts = dataframe["gameId"].map(dataframe["gameId"].value_counts())
    inverse_rows = 1.0 / np.maximum(counts.to_numpy(dtype=float), 1.0)
    mean_weight = float(inverse_rows.mean()) if inverse_rows.size else 0.0
    if mean_weight > 0.0 and np.isfinite(mean_weight):
        inverse_rows = inverse_rows / mean_weight
    return inverse_rows


def resolve_patch(
    dataframe: pd.DataFrame, patch_argument: str
) -> Tuple[str, List[str]]:
    """数据集有 patch 列时取唯一值；否则回退到 --patch 参数。"""
    if "patch" in dataframe.columns:
        values = sorted(
            {
                str(value).strip()
                for value in dataframe["patch"].dropna().tolist()
                if str(value).strip()
            }
        )
        if values:
            if len(values) == 1:
                return values[0], values
            return ", ".join(values), values

    return str(patch_argument or "").strip(), []


def metrics(y_true: np.ndarray, probabilities: np.ndarray) -> Dict[str, float]:
    try:
        auc = float(roc_auc_score(y_true, probabilities))
    except ValueError:
        auc = float("nan")

    return {
        "auc": auc,
        "logLoss": float(log_loss(y_true, probabilities)),
        "brier": float(brier_score_loss(y_true, probabilities)),
        "accuracy": float(accuracy_score(y_true, (probabilities >= 0.5).astype(int))),
    }


def _safe_block(name: str, producer: Any) -> Any:
    """计算附加指标；失败只记录 error，绝不中断训练。"""
    try:
        return producer()
    except Exception as error:  # 附加指标失败不能影响模型产出
        detail = f"{type(error).__name__}: {error}"
        print(f"[警告] {name}失败: {detail}", file=sys.stderr)
        return {"error": detail}


def train(
    dataset_path: Path,
    output_directory: Path,
    folds: int,
    seed: int,
    game_weight: bool = False,
    patch: str = "",
    drop_rank_features: bool = False,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
    bins: int = 10,
) -> int:
    dataframe = pd.read_csv(dataset_path, encoding="utf-8-sig")

    if "ownWin" not in dataframe.columns:
        print("[错误] 缺少 ownWin 列", file=sys.stderr)
        return 1
    if "gameId" not in dataframe.columns:
        print("[错误] 缺少 gameId 列（按局等权与 bootstrap 都需要）", file=sys.stderr)
        return 1

    y = dataframe["ownWin"].astype(int).to_numpy()
    try:
        groups = dataframe["gameId"].astype(int).to_numpy()
    except (TypeError, ValueError):
        groups = dataframe["gameId"].astype(str).to_numpy()

    features, feature_columns, dropped_features = build_features(
        dataframe, drop_rank_features=drop_rank_features
    )
    if not feature_columns:
        print("[错误] 剔除后没有可用特征列", file=sys.stderr)
        return 1

    sample_weights = game_sample_weights(dataframe) if game_weight else None
    patch_value, patch_values = resolve_patch(dataframe, patch)
    rank_columns_present = [
        column for column in dataframe.columns if column in RANK_FEATURES
    ]

    unique_games = len(set(groups))
    folds = max(2, min(folds, unique_games))

    print(f"Rows: {len(dataframe)}")
    print(f"Games: {unique_games}")
    print(f"Features: {len(feature_columns)}")
    print(f"Folds: {folds}")
    print(
        "Weighting: "
        + ("1/game rows (normalized to mean 1)" if game_weight else "row-level")
    )
    print(f"Patch: {patch_value or '(unset)'}")
    if dropped_features:
        print(f"Dropped rank features: {', '.join(dropped_features)}")

    oof = np.full(len(dataframe), np.nan)
    fold_metrics: List[Dict[str, Any]] = []
    best_iterations: List[int] = []
    importances = np.zeros(len(feature_columns))

    splitter = GroupKFold(n_splits=folds)

    for fold, (train_index, valid_index) in enumerate(
        splitter.split(features, y, groups), start=1
    ):
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
            random_state=seed,
            verbose=-1,
        )
        fit_kwargs: Dict[str, Any] = {}
        if sample_weights is not None:
            fit_kwargs["sample_weight"] = sample_weights[train_index]
            fit_kwargs["eval_sample_weight"] = [sample_weights[valid_index]]

        model.fit(
            features.iloc[train_index],
            y[train_index],
            eval_set=[(features.iloc[valid_index], y[valid_index])],
            eval_metric="binary_logloss",
            callbacks=[lgb.early_stopping(30, verbose=False), lgb.log_evaluation(0)],
            **fit_kwargs,
        )

        probabilities = model.predict_proba(features.iloc[valid_index])[:, 1]
        oof[valid_index] = probabilities
        best_iterations.append(int(model.best_iteration_ or model.n_estimators))
        importances += model.booster_.feature_importance(importance_type="gain")

        item = metrics(y[valid_index], probabilities)
        item["fold"] = fold
        fold_metrics.append(item)
        print(
            f"  fold {fold}: AUC={item['auc']:.4f} "
            f"logLoss={item['logLoss']:.4f} Brier={item['brier']:.4f}"
        )

    overall = metrics(y, oof)
    overall["rows"] = int(len(dataframe))
    overall["baselineBrier"] = 0.25

    minute_values = (
        dataframe["minute"].to_numpy() if "minute" in dataframe.columns else None
    )

    # 校准评估（等频 ECE / 可靠性分箱 / 分分钟）；失败只记录 error，不中断训练。
    def calibration_block() -> Any:
        from evaluate_calibration import calibration_snapshot, to_jsonable

        return to_jsonable(
            calibration_snapshot(y, oof, bins=bins, minute=minute_values)
        )

    calibration: Any = _safe_block("校准评估", calibration_block)

    # 按局 bootstrap（1000 次重采样）看指标区间，而不是给 579,790 行算一个点估计。
    def bootstrap_block() -> Any:
        from evaluate_calibration import game_bootstrap, to_jsonable

        return to_jsonable(
            game_bootstrap(
                y,
                oof,
                dataframe["gameId"].to_numpy(),
                bins=bins,
                replicates=bootstrap_replicates,
                seed=seed,
            )
        )

    bootstrap: Any = _safe_block("按局 bootstrap", bootstrap_block)

    def per_minute_block() -> Any:
        from evaluate_calibration import per_minute_table, to_jsonable

        return to_jsonable(
            per_minute_table(y, oof, minute_values, bins=bins)
        )

    per_minute: Any = _safe_block("按分钟指标", per_minute_block)

    output_directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "gameId": dataframe["gameId"],
            "minute": dataframe["minute"],
            "role": dataframe["role"],
            "y": y,
            "probability": oof,
        }
    ).to_csv(
        output_directory / "oof_predictions.csv",
        index=False,
        encoding="utf-8-sig",
    )

    importance_frame = pd.DataFrame(
        {
            "feature": feature_columns,
            "gain": importances / max(1, len(fold_metrics)),
        }
    ).sort_values("gain", ascending=False)
    importance_frame.to_csv(
        output_directory / "feature_importance.csv",
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
        random_state=seed,
        verbose=-1,
    )
    final_model.fit(
        features,
        y,
        sample_weight=sample_weights if sample_weights is not None else None,
    )
    final_model.booster_.save_model(str(output_directory / "model.txt"))

    # 特征基线（D3）：每列 mean/std 落盘，供 model_monitor 做 z-score 漂移检测
    feature_stats: Dict[str, Any] = {}
    try:
        for column in feature_columns:
            if column in CATEGORICAL_COLUMNS:
                continue
            series = pd.to_numeric(features[column], errors="coerce")
            feature_stats[column] = {
                "mean": float(series.mean()),
                "std": float(series.std(ddof=0) or 0.0),
                "count": int(series.notna().sum()),
            }
    except Exception as error:  # noqa: BLE001
        feature_stats = {"error": f"{type(error).__name__}: {error}"}

    report: Dict[str, Any] = {
        "dataset": portable_path(dataset_path, output_directory),
        "rows": int(len(dataframe)),
        "games": int(unique_games),
        "features": len(feature_columns),
        "featureColumns": feature_columns,
        "categoricalColumns": sorted(
            column for column in feature_columns if column in CATEGORICAL_COLUMNS
        ),
        "patch": patch_value,
        "patches": patch_values,
        "trainedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weighting": {
            "byGame": bool(game_weight),
            "normalizedToMeanOne": bool(game_weight),
            "note": (
                "sample_weight ∝ 1 / 该局行数（整体归一化到均值 1），每局等权，"
                "长局不再主导损失；归一化是为了不让小权重放大 reg_lambda"
                if game_weight
                else "行级等权（未按局归一；长局后期会主导损失）"
            ),
        },
        "overall": overall,
        "calibration": calibration,
        "featureStats": feature_stats,
        "bootstrap": bootstrap,
        "perMinute": per_minute,
        "droppedFeatures": dropped_features,
        "rankFeatureRisk": (
            "post-hoc" if rank_columns_present and not drop_rank_features else "none"
        ),
        "rankFeatureNote": (
            RANK_FEATURE_NOTE
            if rank_columns_present and not drop_rank_features
            else "未使用赛后快照的 rank* 特征。"
        ),
        "foldMetrics": fold_metrics,
        "finalEstimators": final_estimators,
    }

    try:
        from evaluate_calibration import to_jsonable

        report = to_jsonable(report)
    except Exception:  # 指标全是内建 float 时无需清洗
        pass

    (output_directory / "metrics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(
        f"OOF AUC={overall['auc']:.4f} "
        f"logLoss={overall['logLoss']:.4f} "
        f"Brier={overall['brier']:.4f} "
        f"accuracy={overall['accuracy']:.4f}"
    )
    if isinstance(calibration, dict) and "overall" in calibration:
        print(f"OOF ECE={calibration['overall']['ece']:.4f}")
    if isinstance(bootstrap, dict) and bootstrap.get("intervals"):
        interval = bootstrap["intervals"].get("auc")
        if interval:
            print(
                f"AUC 95% CI (by game, {bootstrap['replicates']} 次): "
                f"[{interval['low']:.4f}, {interval['high']:.4f}]"
            )
    print()
    print("Top features:")
    print(importance_frame.head(10).to_string(index=False))
    print()
    print(f"Output: {output_directory}")

    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="训练 Live Client Data 可观测特征的胜率模型",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--game-weight",
        dest="game_weight",
        action="store_true",
        help=(
            "按局等权训练（sample_weight 归一化到均值 1）。默认关："
            "2,298 局实测 AUC 无提升（0.8551→0.8554），ECE 反而 0.0081→0.0192"
        ),
    )
    parser.add_argument(
        "--no-game-weight",
        dest="game_weight",
        action="store_false",
        help="兼容旧参数：显式关闭按局等权（与默认一致）",
    )
    parser.set_defaults(game_weight=False)
    parser.add_argument(
        "--drop-rank-features",
        action="store_true",
        help=(
            "剔除赛后快照的段位特征（"
            + "/".join(RANK_FEATURES)
            + "），训练线上诚实版本"
        ),
    )
    parser.add_argument(
        "--patch",
        default="",
        help="数据集没有 patch 列时记录到 metrics.json 的版本号，如 16.22",
    )
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=BOOTSTRAP_REPLICATES,
        help="按局 bootstrap 重采样次数（0 表示关闭）",
    )
    parser.add_argument("--bins", type=int, default=10, help="校准等频分箱数量")
    args = parser.parse_args(argv)

    if args.bootstrap < 0:
        parser.error("--bootstrap 不能为负数")
    if args.bins < 2:
        parser.error("--bins 必须 >= 2")

    output = args.output or (args.dataset.parent / "live_model")

    return train(
        args.dataset,
        output,
        args.folds,
        args.seed,
        game_weight=args.game_weight,
        patch=args.patch,
        drop_rank_features=args.drop_rank_features,
        bootstrap_replicates=args.bootstrap,
        bins=args.bins,
    )


if __name__ == "__main__":
    raise SystemExit(main())
