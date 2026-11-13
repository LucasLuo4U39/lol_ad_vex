#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evaluate_calibration.py 的回归测试（合成数据，不依赖真实数据集）。

覆盖：
  1. 完美校准的预测 → ECE ≈ 0
  2. 系统性高估 → ECE 明显 > 0.05，且偏差方向正确
  3. 单调失真数据上 isotonic 交叉拟合降低 ECE
  4. AUC 同分取平均秩（全并列 = 0.5）
  5. 缺 minute 列时 perMinute 为空且不报错
  6. 有 gameId 时按局交叉拟合 / bootstrap
  7. CLI 端到端（写入 JSON / Markdown）

运行：python test_calibration.py
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

import evaluate_calibration as ec

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL

    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def perfect_dataset() -> Tuple[np.ndarray, np.ndarray]:
    """每个十分位箱内实际正率恰好等于预测值。"""
    labels: List[int] = []
    probabilities: List[float] = []

    for step in range(1, 11):
        probability = step / 10.0
        positives = step * 10
        probabilities.extend([probability] * 100)
        labels.extend([1] * positives + [0] * (100 - positives))

    return np.asarray(labels, dtype=float), np.asarray(probabilities, dtype=float)


def overestimate_dataset() -> Tuple[np.ndarray, np.ndarray]:
    """真实概率 q 生成标签，但预测统一报 q + 0.2（截断到 1）。"""
    labels: List[int] = []
    probabilities: List[float] = []

    for step in range(1, 14):
        truth = step * 0.05
        predicted = min(1.0, truth + 0.2)
        positives = int(round(truth * 200))
        probabilities.extend([predicted] * 200)
        labels.extend([1] * positives + [0] * (200 - positives))

    return np.asarray(labels, dtype=float), np.asarray(probabilities, dtype=float)


def distorted_dataset() -> Tuple[np.ndarray, np.ndarray]:
    """真实概率 p，预测为 p^1.8（单调但系统性低估）。"""
    labels: List[int] = []
    probabilities: List[float] = []

    for step in range(1, 20):
        truth = step * 0.05
        predicted = truth**1.8
        positives = int(round(truth * 300))
        probabilities.extend([predicted] * 300)
        labels.extend([1] * positives + [0] * (300 - positives))

    return np.asarray(labels, dtype=float), np.asarray(probabilities, dtype=float)


def write_predictions(
    path: Path,
    labels: Sequence[float],
    probabilities: Sequence[float],
    minutes: Sequence[float] | None = None,
    games: Sequence[str] | None = None,
) -> None:
    fieldnames = ["y", "probability"]
    if minutes is not None:
        fieldnames.append("minute")
    if games is not None:
        fieldnames.append("gameId")

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, (label, probability) in enumerate(zip(labels, probabilities)):
            row: Dict[str, Any] = {"y": int(label), "probability": probability}
            if minutes is not None:
                row["minute"] = int(minutes[index])
            if games is not None:
                row["gameId"] = games[index]
            writer.writerow(row)


def test_perfect_calibration() -> None:
    print("[1] 完美校准")
    labels, probabilities = perfect_dataset()
    result = ec.evaluate_binary(labels, probabilities, bins=10)

    check("ECE < 0.01", result["ece"] < 0.01, f"ece={result['ece']}")
    check(
        "对照用的等宽 ECE 也 < 0.01",
        result["eceEqualWidth"] < 0.01,
        f"eceEqualWidth={result['eceEqualWidth']}",
    )
    check("最大分箱偏差 < 0.01", result["maxBinDeviation"] < 0.01, str(result["maxBinDeviation"]))
    check("平均预测 = 实际正率", abs(result["bias"]) < 1e-9, str(result["bias"]))
    table = ec.reliability_table(labels, probabilities)
    check(
        "可靠性表每箱都有样本",
        len(table) == 10 and all(row["count"] > 0 for row in table),
        str([row["count"] for row in table]),
    )
    check(
        "Brier 接近 p(1-p) 期望",
        abs(result["brier"] - float(np.mean(probabilities * (1 - probabilities)))) < 0.02,
        str(result["brier"]),
    )


def test_systematic_overestimate() -> None:
    print("[2] 系统性高估")
    labels, probabilities = overestimate_dataset()
    result = ec.evaluate_binary(labels, probabilities, bins=10)

    check("ECE > 0.05", result["ece"] > 0.05, f"ece={result['ece']}")
    check("预测均值 > 实际正率", result["meanProbability"] > result["actualWinRate"],
          f"{result['meanProbability']} vs {result['actualWinRate']}")
    check("偏差方向为正", result["bias"] > 0.05, f"bias={result['bias']}")
    check("MCE >= ECE", result["maxBinDeviation"] >= result["ece"] - 1e-12)


def test_isotonic_reduces_ece() -> None:
    print("[3] isotonic 交叉拟合")
    labels, probabilities = distorted_dataset()
    raw = ec.evaluate_binary(labels, probabilities, bins=10)

    calibrated, meta = ec.cross_fit_calibrate(
        labels, probabilities, method="isotonic", folds=5, seed=0
    )

    calibrated_result = ec.evaluate_binary(labels, calibrated, bins=10)

    check("原始 ECE > 0.05", raw["ece"] > 0.05, f"ece={raw['ece']}")
    check("校准后 ECE 明显下降", calibrated_result["ece"] < raw["ece"] * 0.5,
          f"{raw['ece']} -> {calibrated_result['ece']}")
    check("校准后 ECE < 0.03", calibrated_result["ece"] < 0.03, str(calibrated_result["ece"]))
    check("校准后偏差接近 0", abs(calibrated_result["bias"]) < 0.02, str(calibrated_result["bias"]))
    check("分 5 折", meta["folds"] == 5 and meta["foldedBy"] == "row", str(meta))
    check("校准后概率仍在 [0, 1]", calibrated.min() >= 0 and calibrated.max() <= 1)

    platt_calibrated, _ = ec.cross_fit_calibrate(
        labels, probabilities, method="platt", folds=5, seed=0
    )
    platt_result = ec.evaluate_binary(labels, platt_calibrated, bins=10)
    check("Platt 也能降低 ECE", platt_result["ece"] < raw["ece"] * 0.5,
          f"{raw['ece']} -> {platt_result['ece']}")


def test_auc_ties() -> None:
    print("[4] AUC 同分处理")
    labels = np.array([0, 0, 1, 1], dtype=float)
    check("全并列 AUC = 0.5", abs(ec.auc_score(labels, np.full(4, 0.5)) - 0.5) < 1e-12,
          str(ec.auc_score(labels, np.full(4, 0.5))))
    check(
        "部分并列 AUC",
        abs(ec.auc_score(np.array([1, 0, 0, 1]), np.array([0.3, 0.3, 0.3, 0.7])) - 0.75) < 1e-12,
        str(ec.auc_score(np.array([1, 0, 0, 1]), np.array([0.3, 0.3, 0.3, 0.7]))),
    )
    check(
        "单类返回 nan 不抛异常",
        np.isnan(ec.auc_score(np.ones(5), np.linspace(0.1, 0.9, 5))),
    )
    check("平均秩", np.allclose(ec.average_ranks([0.2, 0.2, 0.9]), [1.5, 1.5, 3.0]))


def test_missing_minute() -> None:
    print("[5] 缺少 minute 列")
    labels, probabilities = perfect_dataset()

    block = ec.calibration_snapshot(labels, probabilities, bins=10, minute=None)
    check("perMinute 为空列表", block["perMinute"] == [], str(block["perMinute"]))
    check("仍含 overall / reliability", "overall" in block and len(block["reliability"]) == 10)
    check(
        "可严格 JSON 序列化",
        json.loads(json.dumps(block, allow_nan=False))["overall"]["ece"] < 0.01,
    )

    minutes = np.repeat(np.arange(1, 11), 100)
    with_minute = ec.calibration_snapshot(labels, probabilities, bins=10, minute=minutes)
    check("有 minute 时生成 10 行", len(with_minute["perMinute"]) == 10,
          str(len(with_minute["perMinute"])))
    first = with_minute["perMinute"][0]
    check("按分钟表含 AUC 与 ECE", "auc" in first and "ece" in first and first["minute"] == 1)


def build_grouped_dataset() -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    labels: List[int] = []
    probabilities: List[float] = []
    games: List[str] = []

    for game in range(20):
        for index in range(50):
            probability = 0.2 + 0.6 * (index % 10) / 9.0
            label = 1 if (index * 7 + game * 3) % 10 < probability * 10 else 0
            probabilities.append(probability)
            labels.append(label)
            games.append(f"9{game:09d}")

    return (
        np.asarray(labels, dtype=float),
        np.asarray(probabilities, dtype=float),
        np.asarray(games, dtype=object),
    )


def test_grouped_and_bootstrap() -> None:
    print("[6] 按局交叉拟合与 bootstrap")
    labels, probabilities, games = build_grouped_dataset()

    calibrated, meta = ec.cross_fit_calibrate(
        labels, probabilities, groups=games, method="isotonic", folds=5, seed=1
    )
    check("按 gameId 分折", meta["foldedBy"] == "gameId", str(meta))
    check("折数 5", meta["folds"] == 5, str(meta["folds"]))
    check("全部样本被校准", not np.isnan(calibrated).any())

    folds, _ = ec.assign_folds(len(labels), groups=games, folds=5, seed=1)
    leakage = any(
        len(set(folds[games == game].tolist())) != 1 for game in set(games.tolist())
    )
    check("同一局只落在一个折", not leakage)

    interval = ec.game_bootstrap(
        labels, probabilities, games, bins=10, replicates=60, seed=3
    )
    check("bootstrap 覆盖 20 局", interval is not None and interval["games"] == 20,
          str(interval))
    ece_interval = interval["intervals"]["ece"]
    check("ECE 置信区间有效", ece_interval["low"] <= ece_interval["high"],
          str(ece_interval))


def test_cli(tmp_path: Path) -> None:
    print("[7] CLI 端到端")
    labels, probabilities, games = build_grouped_dataset()
    minutes = np.tile(np.repeat(np.arange(1, 11), 5), 20)
    csv_path = tmp_path / "predictions.csv"
    write_predictions(csv_path, labels, probabilities, minutes=minutes, games=games)

    json_path = tmp_path / "nested" / "calibration_eval.json"
    md_path = tmp_path / "nested" / "calibration_eval.md"

    with redirect_stdout(io.StringIO()) as captured:
        code = ec.main(
            [
                "--predictions",
                str(csv_path),
                "--out",
                str(json_path),
                "--out-md",
                str(md_path),
                "--apply",
                "isotonic",
                "--bootstrap",
                "20",
                "--bins",
                "8",
            ]
        )

    check("退出码 0", code == 0, str(code))
    check("JSON 已写入", json_path.is_file())
    check("Markdown 已写入", md_path.is_file())

    report = json.loads(json_path.read_text(encoding="utf-8"))
    check("含四个顶层块", all(key in report for key in ("overall", "perMinute", "reliability", "calibrated")))
    check("bins 生效", report["binning"]["bins"] == 8)
    check("perMinute 非空", len(report["perMinute"]) == 10, str(len(report["perMinute"])))
    check("校准块有指标", report["calibrated"]["overall"]["ece"] is not None)
    check("bootstrap 已写入", report["bootstrap"]["games"] == 20)
    check("stdout 有 ECE 摘要", "ECE=" in captured.getvalue())
    check("Markdown 有中文标题", "概率校准评估" in md_path.read_text(encoding="utf-8"))

    no_extra_path = tmp_path / "plain.csv"
    write_predictions(no_extra_path, labels, probabilities)
    plain_json = tmp_path / "plain.json"
    with redirect_stdout(io.StringIO()):
        code = ec.main(["--predictions", str(no_extra_path), "--out", str(plain_json)])

    plain_report = json.loads(plain_json.read_text(encoding="utf-8"))
    check("无 minute → perMinute 为空", code == 0 and plain_report["perMinute"] == [])
    check("无 gameId → bootstrap 为 null", plain_report["bootstrap"] is None)
    check("apply=none → calibrated 为 null", plain_report["calibrated"] is None)

    same_json = tmp_path / "same.json"
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        code = ec.main(
            [
                "--predictions",
                str(csv_path),
                "--fit-predictions",
                str(csv_path),
                "--apply",
                "platt",
                "--out",
                str(same_json),
                "--bootstrap",
                "0",
            ]
        )
    same_report = json.loads(same_json.read_text(encoding="utf-8"))
    check(
        "同一份 --fit-predictions 退回交叉拟合",
        code == 0 and same_report["calibrated"]["fit"] == "cross-fit",
        str(same_report["calibrated"].get("fit")),
    )


def test_usage_and_errors(tmp_path: Path) -> None:
    print("[8] 用法与错误处理")
    with redirect_stdout(io.StringIO()) as captured:
        code = ec.main([])
    check("无参数退出码 0", code == 0, str(code))
    check("无参数打印用法", "usage:" in captured.getvalue().lower())

    with redirect_stderr(io.StringIO()):
        code = ec.main(["--predictions", str(tmp_path / "missing.csv")])
    check("文件缺失退出码 1", code == 1, str(code))

    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("y,probability\n0,1.5\n1,0.2\n", encoding="utf-8")
    with redirect_stderr(io.StringIO()):
        code = ec.main(["--predictions", str(bad_csv), "--out", str(tmp_path / "x.json")])
    check("概率越界退出码 1", code == 1, str(code))


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        tmp_path = Path(temporary)

        test_perfect_calibration()
        test_systematic_overestimate()
        test_isotonic_reduces_ece()
        test_auc_ties()
        test_missing_minute()
        test_grouped_and_bootstrap()
        test_cli(tmp_path)
        test_usage_and_errors(tmp_path)

    print()
    if FAIL:
        print(f"失败: {FAIL} 项，通过: {PASS} 项")
        return 1

    print(f"全部通过: {PASS} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
