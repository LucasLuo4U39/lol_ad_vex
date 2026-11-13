#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3 模型监控：静态模型的「补丁年龄」体检 + 线上特征相对训练均值的偏移。

两件事，对应 D3 的两个盲区：

1. `model_info(model_dir)`：模型名 + `model.txt` 修改时间/ageDays，以及同目录
   `metrics.json` 里的 `patch` / `trainedAt` / `features`。模型是静态训练的，
   补丁一换特征分布就变；没有 patch 或模型超过 90 天就拼一句话 warning，
   让「不知道漂到哪了」至少变成「界面上看得见」。

2. `feature_drift(features, train_means, train_scales=None, top=5)`：逐特征算
   `|value - mean| / scale`，取平均后按 0.5 / 1.0 分级（ok / warn / alert），
   并给出偏差最大的 top N。空输入、全非数值时安全返回 ok/0.0/[]。

scale 口径（重要，别把它当 p 值）
--------------------------------
训练均值唯一真源是 `attribution.TRAIN_MEANS`（live_dataset.csv 579,790 行的
列均值）。训练侧**没有**落盘标准差：全仓检索不到 `TRAIN_SCALES`
（attribution.py 只有 TRAIN_MEANS）。因此 `train_scales` 缺省时 scale=1.0，
此时的 `score` 是「相对训练均值的平均绝对偏移」，单位跟随各特征的原始量纲
（金币、秒数、段位分、补刀……），**不是**严格 z-score，也不代表分布显著性：
量级大的特征（ownGold、since_*、teamCS）天然占主导，单局对局中 minute 或
ownGold 本来就偏离「全样本均值」，score 可能天然偏高。它的用途是**趋势监控**
（同一模型下 score 突然抬升 = 输入分布变了），而不是绝对值判死刑。
将来训练脚本把每列 std 落进 metrics.json / TRAIN_SCALES 后，把 dict 传进
`train_scales` 即可无痛升级为真正的 z-score，0.5/1.0 阈值那时才有统计含义。

自检：由 test_d3_monitor.py 覆盖（不依赖客户端，不开 GUI）。
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 模型年龄告警线：超过这个天数，特征分布很可能已经跨版本漂移。
MODEL_AGE_WARN_DAYS = 90

# 偏移分级阈值（平均绝对偏差，scale 缺省=1.0 时单位为特征原始量纲）。
DRIFT_WARN = 0.5
DRIFT_ALERT = 1.0


def _read_json(path: Path) -> Optional[Any]:
    """读 JSON；文件缺失/损坏/非 UTF-8 一律 None（监控件不能把宿主带崩）。"""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def model_info(model_dir: Path) -> Dict[str, Any]:
    """模型体检：名字、文件时间、ageDays、metrics.json 元数据与 warning。

    返回字段（JSON 可序列化）：
        name          模型目录名（如 live_model5）
        dir           目录绝对路径
        mtime         model.txt 修改时间，UTC ISO 8601；文件缺失为 None
        ageDays       距今天数（保留 2 位），无法取值时为 None
        patch         metrics.json["patch"]，缺失为 None
        trainedAt     metrics.json["trainedAt"]，缺失为 None
        features      metrics.json["features"]，缺失为 None（本仓现为特征数 int）
        featureCount  features 为列表时取长度；为 int 时原样；否则 None
        warning       无 patch / model.txt 缺失 / ageDays>90 时的一句话，否则 None

    metrics.json 缺失或损坏不抛错：patch/trainedAt/features 全 None，并在
    warning 里点明「缺 patch 元数据」——这正是当前仓库的现状（assets 下的
    模型目录只有 model.txt）。
    """
    directory = Path(model_dir)
    model_path = directory / "model.txt"
    mtime: Optional[str] = None
    age_days: Optional[float] = None
    try:
        stat = model_path.stat()
        stamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        mtime = stamp.isoformat(timespec="seconds")
        age_days = round(
            max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0),
            2,
        )
    except OSError:
        pass

    metrics = _read_json(directory / "metrics.json")
    patch: Any = metrics.get("patch") if isinstance(metrics, dict) else None
    trained_at: Any = metrics.get("trainedAt") if isinstance(metrics, dict) else None
    features: Any = metrics.get("features") if isinstance(metrics, dict) else None
    if isinstance(features, (list, tuple)):
        feature_count: Optional[int] = len(features)
    elif isinstance(features, int) and not isinstance(features, bool):
        feature_count = features
    else:
        feature_count = None

    reasons: List[str] = []
    if not model_path.is_file():
        reasons.append("model.txt 缺失，无法确认模型文件与年龄")
    if patch is None or (isinstance(patch, str) and not patch.strip()):
        reasons.append("metrics.json 缺少 patch 元数据，无法判断训练补丁与线上补丁的差距")
    if age_days is not None and age_days > MODEL_AGE_WARN_DAYS:
        reasons.append(
            f"模型已 {age_days:.0f} 天未更新（>{MODEL_AGE_WARN_DAYS} 天），"
            "特征分布可能已随补丁漂移"
        )
    warning = (("；".join(reasons) + "。") if reasons else None)

    return {
        "name": directory.name or str(directory),
        "dir": str(directory),
        "mtime": mtime,
        "ageDays": age_days,
        "patch": patch,
        "trainedAt": trained_at,
        "features": features,
        "featureCount": feature_count,
        "warning": warning,
    }


def feature_drift(
    features: Dict[str, Any],
    train_means: Dict[str, Any],
    train_scales: Optional[Dict[str, Any]] = None,
    top: int = 5,
) -> Dict[str, Any]:
    """当前特征相对训练均值的标准化偏移。

    只比较 **两边都存在** 的特征；值/均值/scale 任一非数值或非有限（NaN/inf）
    就跳过，因此 role 这类分类特征天然不参与。
    scale 口径：`train_scales` 有该特征且 >0 时用它（真正的 z-score）；否则用
    `max(|mean|, 1)` 做**相对偏移**——避免 ownGold/since_* 这类大额特征把平均
    偏差拉到几百、恒定 alert。两种口径都用 0.5 / 1.0 分级，语义是「平均偏离
    训练均值 50% / 100%」，用于看趋势，不是显著性检验。

    返回：
        {"score": 平均偏差(float，4 位),
         "level": "ok" | "warn" | "alert"（score <0.5 / <1.0 / >=1.0）,
         "top":   [(feature, 偏差), ...] 按偏差降序，最多 top 项}

    空输入 / 没有可比特征 → {"score": 0.0, "level": "ok", "top": []}。
    """
    empty = {"score": 0.0, "level": "ok", "top": []}
    if not isinstance(features, dict) or not isinstance(train_means, dict):
        return empty
    if not features or not train_means:
        return empty
    scales = train_scales if isinstance(train_scales, dict) else {}

    deviations: List[Tuple[str, float]] = []
    for name in sorted(str(key) for key in train_means):
        if name not in features:
            continue
        try:
            value = float(features[name])
            mean = float(train_means[name])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(value) and math.isfinite(mean)):
            continue
        scale = 1.0
        raw_scale = scales.get(name)
        has_scale = False
        if raw_scale is not None:
            try:
                parsed = float(raw_scale)
            except (TypeError, ValueError):
                parsed = 0.0
            if math.isfinite(parsed) and parsed > 0.0:
                scale = parsed
                has_scale = True
        if not has_scale:
            # 没有落盘 std 时用 |mean|（下限 1）做相对尺度：
            # 否则 ownGold(数千) 这类大额特征会把平均偏差拉到几百，恒为 alert。
            scale = max(abs(mean), 1.0)
        deviations.append((name, abs(value - mean) / scale))

    if not deviations:
        return empty

    score = sum(item[1] for item in deviations) / len(deviations)
    if score >= DRIFT_ALERT:
        level = "alert"
    elif score >= DRIFT_WARN:
        level = "warn"
    else:
        level = "ok"
    ranked = sorted(deviations, key=lambda item: (-item[1], item[0]))
    try:
        limit = max(0, int(top))
    except (TypeError, ValueError):
        limit = 5
    return {
        "score": round(score, 4),
        "level": level,
        "top": [(name, round(z, 4)) for name, z in ranked[:limit]],
    }


# --------------------------------------------------------------------------
# 训练侧基线（单一真源：attribution.py）
# --------------------------------------------------------------------------
def train_means() -> Dict[str, float]:
    """attribution.TRAIN_MEANS 的副本；导入失败返回 {}（调用方按无基线处理）。"""
    try:
        from attribution import TRAIN_MEANS

        return {str(key): float(value) for key, value in TRAIN_MEANS.items()}
    except Exception:  # noqa: BLE001
        return {}


def train_scales() -> Optional[Dict[str, float]]:
    """attribution.TRAIN_SCALES（当前仓库不存在 → None，即用相对尺度）。"""
    try:
        from attribution import TRAIN_SCALES  # type: ignore[attr-defined]

        if isinstance(TRAIN_SCALES, dict) and TRAIN_SCALES:
            return {str(key): float(value) for key, value in TRAIN_SCALES.items()}
        return None
    except Exception:  # noqa: BLE001
        return None


def model_stats(model_dir: Path) -> Tuple[Dict[str, float], Optional[Dict[str, float]]]:
    """模型自带的特征基线：优先 metrics.json 的 featureStats（mean/std）。

    训练脚本（v2.3.15 起）会把每列 mean/std 落进 metrics.json；有它时 drift 就是
    真正的 z-score。没有（旧模型）则回退 attribution.TRAIN_MEANS + 相对尺度。
    """
    metrics = _read_json(Path(model_dir) / "metrics.json")
    stats = metrics.get("featureStats") if isinstance(metrics, dict) else None
    means: Dict[str, float] = {}
    scales: Dict[str, float] = {}
    if isinstance(stats, dict):
        for key, item in stats.items():
            if not isinstance(item, dict):
                continue
            try:
                mean = float(item.get("mean"))
                std = float(item.get("std"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(mean):
                means[str(key)] = mean
            if math.isfinite(std) and std > 0:
                scales[str(key)] = std
        if means:
            return means, (scales or None)
    return train_means(), None
