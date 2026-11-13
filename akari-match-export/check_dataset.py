#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""训练前数据集自检：列/行/局/标签/泄漏提示（D3 配套）。

用法：
    python check_dataset.py --dataset ..\dataset\live_dataset\live_dataset_v5.csv
    python check_dataset.py --dataset live_dataset_v3.csv --oof live_model3\oof_predictions.csv

检查项（失败不阻断，全部打印后按严重度汇总）：
  1. 文件存在、可解析、行数/局数/每局行数中位数
  2. 必需列：gameId / ownWin / minute / role
  3. 标签：ownWin ∈ {0,1}；同一局 ownWin 唯一（否则标签有歧义）
  4. minute：范围 1..60（超过 60 分钟按训练口径会被截断，提示确认）
  5. 一行一视角：重复键 (gameId, minute, role, ownWin) 统计
  6. 段位列（model5）：存在即提示「赛后快照 → 建议 --drop-rank-features 或按赛前时点重算」
  7. patch 列：缺失时提示模型 patch 元数据只能靠 --patch 手填
  8. --oof：行数一致、y/probability 列齐全、概率落在 [0,1]
输出：问题清单 + 建议训练命令 + 退出码（0 = 无阻断问题，1 = 有阻断问题）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

RANK_COLUMNS = {"ownRank", "teamRank", "enemyRank", "rankDiff", "ownRankVsTeam"}
REQUIRED_COLUMNS = {"gameId", "ownWin", "minute", "role"}
BLOCKING: list[str] = []
WARNINGS: list[str] = []


def say(message: str = "") -> None:
    print(message)


def block(message: str) -> None:
    BLOCKING.append(message)
    say(f"  [阻断] {message}")


def warn(message: str) -> None:
    WARNINGS.append(message)
    say(f"  [提示] {message}")


def check_dataset(path: Path) -> Optional[pd.DataFrame]:
    say(f"== 数据集 {path}")
    if not path.is_file():
        block(f"文件不存在：{path}")
        return None
    try:
        data = pd.read_csv(path, dtype={"gameId": str, "role": str})
    except Exception as error:  # noqa: BLE001
        block(f"读取失败：{type(error).__name__}: {error}")
        return None

    rows = len(data)
    games = data["gameId"].nunique() if "gameId" in data.columns else 0
    say(f"  行 {rows:,} / 局 {games:,} / 列 {len(data.columns)}")
    if rows == 0:
        block("数据集为空")
        return data

    missing = REQUIRED_COLUMNS - set(data.columns)
    if missing:
        block(f"缺少必需列：{sorted(missing)}")
        return data

    per_game = data.groupby("gameId").size()
    say(f"  每局行数：中位 {per_game.median():.0f}，最小 {per_game.min()}，最大 {per_game.max()}")
    if per_game.median() < 20:
        warn("每局行数偏少（<20），可能采样间隔过大或导出不全")

    label_values = set(pd.to_numeric(data["ownWin"], errors="coerce").dropna().unique())
    if not label_values <= {0, 1}:
        block(f"ownWin 出现非 0/1 值：{sorted(label_values)[:5]}")
    # 玩家视角数据集：同一局本来就同时有胜方 5 人和负方 5 人（ownWin 0/1 各半），
    # 所以不能要求「每局 ownWin 唯一」；只检查每局是否两种标签都存在。
    per_game_labels = data.groupby("gameId")["ownWin"].nunique()
    single_class = int((per_game_labels < 2).sum())
    if single_class:
        warn(f"{single_class} 局只有一种 ownWin（10 视角应胜负各半，可能是导出不全）")

    minutes = pd.to_numeric(data["minute"], errors="coerce")
    if minutes.isna().any():
        warn(f"minute 有 {int(minutes.isna().sum())} 个空值")
    say(f"  minute 范围 {int(minutes.min())}–{int(minutes.max())}（中位 {minutes.median():.0f}）")
    if int(minutes.max()) > 60:
        warn("存在 >60 分钟的分钟数，训练口径按 1..60，请确认是否截断")

    duplicates = int(data.duplicated(subset=["gameId", "minute", "role", "ownWin"]).sum())
    if duplicates:
        warn(f"重复键 (gameId,minute,role,ownWin) {duplicates} 行（可能是重复导出）")

    rank_present = sorted(RANK_COLUMNS & set(data.columns))
    if rank_present:
        warn(
            "存在段位特征 " + ", ".join(rank_present)
            + "：训练侧是「赛后快照」段位，含未来信息。建议 --drop-rank-features，"
              "或按赛前时点重算后再训练"
        )
    if "patch" in data.columns:
        patches = sorted({str(value) for value in data["patch"].dropna().unique()})
        say(f"  patch 列：{patches[:5]}")
    else:
        warn("没有 patch 列：模型元数据只能靠训练时 --patch 手填")

    numeric = data.select_dtypes("number").shape[1]
    if numeric < 10:
        warn(f"数值特征只有 {numeric} 列，疑似导出字段不全")
    return data


def check_oof(path: Path, dataset_rows: int) -> None:
    say(f"== OOF 预测 {path}")
    if not path.is_file():
        block(f"OOF 文件不存在：{path}")
        return
    try:
        oof = pd.read_csv(path)
    except Exception as error:  # noqa: BLE001
        block(f"OOF 读取失败：{error}")
        return
    missing = {"y", "probability"} - set(oof.columns)
    if missing:
        block(f"OOF 缺少列：{sorted(missing)}")
        return
    if len(oof) != dataset_rows:
        warn(f"OOF 行数 {len(oof):,} 与数据集 {dataset_rows:,} 不一致（交叉验证一般应相等）")
    probability = pd.to_numeric(oof["probability"], errors="coerce")
    out_of_range = int(((probability < 0) | (probability > 1)).sum())
    if out_of_range:
        block(f"{out_of_range} 行概率越界（应在 0–1）")
    labels = set(pd.to_numeric(oof["y"], errors="coerce").dropna().unique())
    if not labels <= {0, 1}:
        block(f"y 出现非 0/1 值：{sorted(labels)[:5]}")
    say(f"  行 {len(oof):,} / 概率均值 {probability.mean():.4f} / 正例率 {pd.to_numeric(oof['y']).mean():.4f}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="训练前数据集自检")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--oof", type=Path, default=None, help="可选：训练产出的 oof_predictions.csv")
    args = parser.parse_args(argv)

    data = check_dataset(args.dataset)
    rows = 0 if data is None else len(data)
    if args.oof is not None:
        check_oof(args.oof, rows)

    say()
    say(f"结果：阻断 {len(BLOCKING)} 项，提示 {len(WARNINGS)} 项")
    if BLOCKING:
        say("请先修掉阻断项，再训练。")
        return 1
    say("建议训练命令（按需取一）：")
    say(f'  .venv\\Scripts\\python.exe train_live_model.py --dataset "{args.dataset}" '
        "--output ..\\dataset\\live_dataset\\live_model_next --patch 16.22 --drop-rank-features")
    say(f'  .venv\\Scripts\\python.exe train_live_model.py --dataset "{args.dataset}" '
        "--output ..\\dataset\\live_dataset\\live_model_next --patch 16.22   # 保留段位特征（会记录 post-hoc 风险）")
    say("训练后校准复评：")
    say('  .venv\\Scripts\\python.exe evaluate_calibration.py '
        '--predictions ..\\dataset\\live_dataset\\live_model_next\\oof_predictions.csv '
        '--out ..\\dataset\\live_dataset\\live_model_next\\calibration_report.json '
        '--out-md ..\\dataset\\live_dataset\\live_model_next\\calibration_report.md')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
