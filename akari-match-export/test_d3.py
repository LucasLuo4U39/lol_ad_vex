"""D3 回归测试：按局等权、按局 bootstrap、按分钟指标、patch 元数据、段位特征可剔除。

全部用合成 CSV，不依赖真实数据集 / 已训练模型，可直接运行：

    python test_d3.py

训练参数很小（30 局 × 12 行），几秒内完成。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import lightgbm as lgb
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from train_live_model import (  # noqa: E402
    RANK_FEATURES,
    game_sample_weights,
    resolve_patch,
    train,
)

PASSED = 0
FAILED = 0

GAMES = 30
ROWS_PER_GAME = 12
ROLES = ("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")
OUT = Path(__file__).parent / "_d3_tmp"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}  {detail}")


def build_dataset(path: Path) -> Path:
    """30 局 × 12 行：含 gameId/ownWin/minute/role、数值特征、rank 列与 patch 列。"""
    rng = np.random.default_rng(20261114)
    rows: list[Dict[str, Any]] = []

    for game in range(GAMES):
        game_id = 900000 + game
        winner_side = int(rng.integers(0, 2))  # 本局获胜方：偶数行视角 / 奇数行视角
        for row_index in range(ROWS_PER_GAME):
            minute = row_index + 1
            won = 1 if (row_index % 2) == winner_side else 0
            win_bonus = 80.0 if won else 0.0
            rows.append(
                {
                    "gameId": game_id,
                    "minute": minute,
                    "role": ROLES[row_index % len(ROLES)],
                    "ownWin": won,
                    "ownGold": 400.0 + 45.0 * minute + win_bonus + rng.normal(0, 40),
                    "ownCS": 20.0 + 7.0 * minute + (12.0 if won else 0.0) + rng.normal(0, 8),
                    "killDiff": float(rng.integers(-4, 5)) + (1.0 if won else -1.0),
                    "levelSumDiff": float(rng.integers(-6, 7)) + (2.0 if won else -2.0),
                    "dragonDiff": float(rng.integers(-2, 3)),
                    "ownRank": 4000.0 + 50.0 * (game % 7),
                    "teamRank": 3900.0 + 40.0 * (game % 5),
                    "enemyRank": 3950.0 + 30.0 * (game % 6),
                    "rankDiff": 0.0,
                    "ownRankVsTeam": 0.0,
                    "patch": "16.22",
                }
            )

    frame = pd.DataFrame(rows)
    frame["rankDiff"] = frame["teamRank"] - frame["enemyRank"]
    frame["ownRankVsTeam"] = frame["ownRank"] - frame["teamRank"]
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def run_training(
    dataset: Path, name: str, **kwargs: Any
) -> Tuple[int, Dict[str, Any], Path]:
    output = OUT / name
    code = train(dataset, output, 3, 42, **kwargs)
    metrics_path = output / "metrics.json"
    metrics: Dict[str, Any] = {}
    if metrics_path.is_file():
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    return code, metrics, output


def check_model_loadable(output: Path, name: str) -> None:
    model_path = output / "model.txt"
    check(f"[{name}] model.txt 存在", model_path.is_file(), str(model_path))
    if not model_path.is_file():
        return
    try:
        booster = lgb.Booster(model_file=str(model_path))
    except Exception as error:  # noqa: BLE001 - 测试要把异常报成 FAIL
        check(f"[{name}] model.txt 可加载", False, f"{type(error).__name__}: {error}")
        return
    check(f"[{name}] model.txt 可加载", booster.num_feature() > 0, str(booster.num_feature()))


def test_default_training(dataset: Path) -> None:
    print("[1] 默认训练（不等权）+ bootstrap + perMinute + patch + calibration")
    try:
        code, metrics, output = run_training(dataset, "default")
    except Exception as error:  # noqa: BLE001
        check("训练不抛异常", False, f"{type(error).__name__}: {error}")
        return

    check("train() 返回 0", code == 0, str(code))
    check("metrics.json 已写出", bool(metrics))
    if not metrics:
        return

    weighting = metrics.get("weighting") or {}
    check("默认不等权（weighting.byGame == False）", weighting.get("byGame") is False, str(weighting))
    check("weighting.note 非空", bool(str(weighting.get("note", "")).strip()), str(weighting))

    bootstrap = metrics.get("bootstrap")
    check(
        "bootstrap 存在且无 error",
        isinstance(bootstrap, dict) and "error" not in bootstrap,
        str(bootstrap)[:200],
    )
    intervals = bootstrap.get("intervals") if isinstance(bootstrap, dict) else None
    check(
        "bootstrap 含 AUC 95% 区间",
        isinstance(intervals, dict) and isinstance(intervals.get("auc"), dict),
        str(intervals)[:200],
    )
    if isinstance(intervals, dict) and isinstance(intervals.get("auc"), dict):
        auc_interval = intervals["auc"]
        check(
            "bootstrap AUC 区间 low<=high 且在 [0,1]",
            0.0 <= auc_interval["low"] <= auc_interval["high"] <= 1.0,
            str(auc_interval),
        )

    per_minute = metrics.get("perMinute")
    check("perMinute 为按分钟列表", isinstance(per_minute, list) and len(per_minute) > 0,
          str(type(per_minute)))
    if isinstance(per_minute, list) and per_minute:
        keys = {"minute", "rows", "auc", "brier", "ece"}
        check(
            "perMinute 每行含 minute/rows/AUC/Brier/ECE",
            all(keys.issubset(row) for row in per_minute),
            str(sorted(per_minute[0].keys())),
        )
        check("perMinute 覆盖 12 个分钟", len(per_minute) == ROWS_PER_GAME,
              f"{len(per_minute)} != {ROWS_PER_GAME}")

    calibration = metrics.get("calibration")
    check(
        "calibration 存在且无 error",
        isinstance(calibration, dict)
        and "error" not in calibration
        and "overall" in calibration,
        str(calibration)[:200],
    )

    check("patch 取自数据集列 == 16.22", metrics.get("patch") == "16.22",
          str(metrics.get("patch")))
    check("patches 唯一值列表", metrics.get("patches") == ["16.22"],
          str(metrics.get("patches")))
    check("trainedAt 非空", bool(str(metrics.get("trainedAt", "")).strip()),
          str(metrics.get("trainedAt")))

    check("rankFeatureRisk == post-hoc（未 drop 段位列）",
          metrics.get("rankFeatureRisk") == "post-hoc",
          str(metrics.get("rankFeatureRisk")))
    check("默认不剔除特征", metrics.get("droppedFeatures") == [],
          str(metrics.get("droppedFeatures")))
    check("overall 向后兼容（行级 AUC/Brier）",
          isinstance(metrics.get("overall"), dict)
          and "auc" in metrics["overall"]
          and "brier" in metrics["overall"],
          str(metrics.get("overall"))[:160])

    check_model_loadable(output, "default")


def test_drop_rank_features(dataset: Path) -> None:
    print("[2] --drop-rank-features：剔除 5 个段位列且训练成功")
    try:
        code, metrics, output = run_training(
            dataset, "drop_rank", drop_rank_features=True
        )
    except Exception as error:  # noqa: BLE001
        check("训练不抛异常", False, f"{type(error).__name__}: {error}")
        return

    check("train() 返回 0", code == 0, str(code))
    check("metrics.json 已写出", bool(metrics))
    if not metrics:
        return

    dropped = metrics.get("droppedFeatures") or []
    check("droppedFeatures 恰为 5 个 rank 列",
          sorted(dropped) == sorted(RANK_FEATURES),
          str(dropped))
    check("featureColumns 不含 rank 列",
          not any(column in RANK_FEATURES for column in metrics.get("featureColumns", [])),
          str(metrics.get("featureColumns")))
    check("rankFeatureRisk == none", metrics.get("rankFeatureRisk") == "none",
          str(metrics.get("rankFeatureRisk")))
    check_model_loadable(output, "drop_rank")


def test_game_weight(dataset: Path) -> None:
    print("[3] --game-weight：显式开启按局等权")
    try:
        code, metrics, output = run_training(
            dataset, "game_weight", game_weight=True
        )
    except Exception as error:  # noqa: BLE001
        check("训练不抛异常", False, f"{type(error).__name__}: {error}")
        return

    check("train() 返回 0", code == 0, str(code))
    weighting = metrics.get("weighting") or {}
    check("开启时 weighting.byGame == True", weighting.get("byGame") is True, str(weighting))
    check("note 非空", bool(str(weighting.get("note", "")).strip()), str(weighting))
    check_model_loadable(output, "game_weight")


def test_weighting_and_patch_helpers(dataset: Path) -> None:
    print("[4] 口径辅助函数：每局权重相等、均值归一为 1、patch 回退 --patch")
    frame = pd.read_csv(dataset, encoding="utf-8-sig")
    weights = game_sample_weights(frame)
    sums = (
        pd.Series(weights)
        .groupby(frame["gameId"].to_numpy())
        .sum()
        .to_numpy()
    )
    check("每局 sample_weight 之和相等（按局等权）",
          bool(np.allclose(sums, sums[0])), str(sums[:5]))
    check("权重整体归一化到均值 1",
          abs(float(weights.mean()) - 1.0) < 1e-12, str(float(weights.mean())))

    no_patch = frame.drop(columns=["patch"])
    patch_value, patch_values = resolve_patch(no_patch, "16.99")
    check("无 patch 列时回退 --patch", patch_value == "16.99", repr(patch_value))
    check("无 patch 列时 patches 为空列表", patch_values == [], repr(patch_values))


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        dataset = build_dataset(OUT / "synthetic_live_dataset.csv")
        test_default_training(dataset)
        test_drop_rank_features(dataset)
        test_game_weight(dataset)
        test_weighting_and_patch_helpers(dataset)
    finally:
        shutil.rmtree(OUT, ignore_errors=True)

    print("=" * 60)
    print(f"  汇总: {PASSED}/{PASSED + FAILED} 通过")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
