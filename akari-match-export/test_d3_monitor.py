#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3 监控件离线自测：补丁体检 + 特征偏移 + 段位历史快照 + /api/home 接线。

不启动服务器、不开 GUI、不需要游戏客户端：
- model_monitor 用临时目录造 model.txt / metrics.json；
- 快照函数注入临时 root（不碰 ~/.lol-advisor）；
- desktop_app 只调 _model_info_field / _drift_field / _home_payload（I/O 打桩），
  不会起 8789 / 8788 端口。

临时目录用工作区 _d3_monitor_tmp，测完删除。
用法：python test_d3_monitor.py
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent
TMP = BASE_DIR / "_d3_monitor_tmp"
PASS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        raise AssertionError(f"{name}: {detail}")


def make_model_dir(
    directory: Path,
    *,
    metrics: Dict[str, Any] | None = None,
    model_txt: bool = True,
    mtime_days_ago: float | None = None,
) -> Path:
    """造一个模型目录：model.txt（可选）+ metrics.json（可选）。"""
    directory.mkdir(parents=True, exist_ok=True)
    if model_txt:
        model_path = directory / "model.txt"
        model_path.write_text("tree\nversion=v4\n", encoding="utf-8")
        if mtime_days_ago is not None:
            stamp = time.time() - mtime_days_ago * 86400.0
            os.utime(model_path, (stamp, stamp))
    if metrics is not None:
        (directory / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False), encoding="utf-8"
        )
    return directory


# ---------------------------------------------------------------- A. model_info
def test_model_info() -> None:
    from model_monitor import MODEL_AGE_WARN_DAYS, model_info

    print("[1] model_info 补丁/年龄体检")

    fresh = make_model_dir(
        TMP / "live_modelX",
        metrics={
            "patch": "16.22",
            "trainedAt": "2026-11-10T06:57:00Z",
            "features": ["minute", "ownGold"],
        },
    )
    info = model_info(fresh)
    check("name=目录名", info["name"] == "live_modelX", str(info.get("name")))
    check("mtime 非空", isinstance(info["mtime"], str) and "T" in info["mtime"], str(info.get("mtime")))
    check(
        "ageDays 合理（新建≈0）",
        isinstance(info["ageDays"], (int, float)) and 0.0 <= float(info["ageDays"]) < 1.0,
        str(info.get("ageDays")),
    )
    check("patch 读到 16.22", info["patch"] == "16.22", str(info.get("patch")))
    check("trainedAt 读到", info["trainedAt"] == "2026-11-10T06:57:00Z", str(info.get("trainedAt")))
    check("features 读到列表", info["features"] == ["minute", "ownGold"], str(info.get("features")))
    check("featureCount=2", info["featureCount"] == 2, str(info.get("featureCount")))
    check("新鲜且有 patch → 无 warning", info["warning"] is None, str(info.get("warning")))

    no_metrics = make_model_dir(TMP / "live_no_meta", metrics=None)
    info2 = model_info(no_metrics)
    check("无 metrics.json → patch is None", info2["patch"] is None, str(info2.get("patch")))
    check("无 metrics.json → trainedAt is None", info2["trainedAt"] is None, str(info2.get("trainedAt")))
    check("无 metrics.json → features is None", info2["features"] is None, str(info2.get("features")))
    check(
        "无 patch → 有 warning 且点明 patch",
        bool(info2["warning"]) and "patch" in str(info2["warning"]),
        str(info2.get("warning")),
    )

    broken = TMP / "live_broken_metrics"
    make_model_dir(broken, metrics=None)
    (broken / "metrics.json").write_text("{ not json", encoding="utf-8")
    info3 = model_info(broken)
    check("metrics.json 损坏也不抛错", info3["patch"] is None and info3["warning"] is not None)

    old = make_model_dir(
        TMP / "live_old",
        metrics={"patch": "16.20", "features": 51},
        mtime_days_ago=MODEL_AGE_WARN_DAYS + 10,
    )
    info4 = model_info(old)
    check(
        f"ageDays≈{MODEL_AGE_WARN_DAYS + 10}",
        isinstance(info4["ageDays"], (int, float))
        and MODEL_AGE_WARN_DAYS + 9 < float(info4["ageDays"]) < MODEL_AGE_WARN_DAYS + 11,
        str(info4.get("ageDays")),
    )
    check(
        "超 90 天 → warning 提到 90",
        bool(info4["warning"]) and "90" in str(info4["warning"]),
        str(info4.get("warning")),
    )
    check("features 为 int 时原样返回且 featureCount 一致", info4["features"] == 51 and info4["featureCount"] == 51)

    no_model = TMP / "live_no_model"
    no_model.mkdir(parents=True, exist_ok=True)
    (no_model / "metrics.json").write_text(json.dumps({"patch": "16.22"}), encoding="utf-8")
    info5 = model_info(no_model)
    check("缺 model.txt → ageDays is None", info5["ageDays"] is None, str(info5.get("ageDays")))
    check(
        "缺 model.txt → warning 点明",
        bool(info5["warning"]) and "model.txt" in str(info5["warning"]),
        str(info5.get("warning")),
    )
    check("不存在的目录也安全", model_info(TMP / "nope")["ageDays"] is None)


# ---------------------------------------------------------------- B. feature_drift
def test_feature_drift() -> None:
    from model_monitor import feature_drift

    print("[2] feature_drift 偏移分级")
    means = {"minute": 13.848876, "ownLevel": 8.732012, "teamCS": 442.385372}

    empty = feature_drift({}, means)
    check("空 features 安全", empty == {"score": 0.0, "level": "ok", "top": []}, str(empty))
    check("空 means 安全", feature_drift({"minute": 1.0}, {}) == {"score": 0.0, "level": "ok", "top": []})
    check(
        "全是非数值 → 安全",
        feature_drift({"role": "MIDDLE", "x": None}, means) == {"score": 0.0, "level": "ok", "top": []},
    )
    check(
        "NaN/inf 跳过 → 安全",
        feature_drift({"minute": float("nan"), "ownLevel": float("inf")}, means)
        == {"score": 0.0, "level": "ok", "top": []},
    )

    same = feature_drift(dict(means), means)
    check("等于训练均值 → score 0/ok", same["score"] == 0.0 and same["level"] == "ok", str(same))
    check("top 只含有共同特征", len(same["top"]) == 3, str(same["top"]))

    single = feature_drift({"minute": means["minute"] + 0.6, "ownLevel": means["ownLevel"]}, means)
    check(
        "单特征偏 0.6 被 2 个共同特征摊薄、按 |mean| 归一 → ≈0.0217/ok",
        abs(single["score"] - 0.0217) < 0.001 and single["level"] == "ok", str(single),
    )
    warn2 = feature_drift({k: v * 1.6 for k, v in means.items()}, means)
    check("整体偏离 60% → warn", warn2["level"] == "warn" and abs(warn2["score"] - 0.6) < 0.001, str(warn2))
    alert = feature_drift({k: v * 2.5 for k, v in means.items()}, means)
    check("整体偏离 150% → alert", alert["level"] == "alert" and abs(alert["score"] - 1.5) < 0.001, str(alert))

    z_ok = feature_drift(
        {"teamCS": means["teamCS"] + 10.0, "minute": means["minute"]},
        means,
        train_scales={"teamCS": 100.0},
    )
    check("提供 scale 时按其标准化（10/100，2 个共同特征）", z_ok["score"] == round(0.1 / 2, 4), str(z_ok))
    z_bad = feature_drift(
        {"teamCS": means["teamCS"] + 10.0, "minute": means["minute"]},
        means,
        train_scales={"teamCS": 0.0},
    )
    check("scale<=0 回退相对尺度（10/442.4，2 个特征）",
          abs(z_bad["score"] - 0.0113) < 0.001, str(z_bad))

    many_means = {f"f{i:02d}": 0.0 for i in range(10)}
    many_features = {f"f{i:02d}": float(i + 1) for i in range(10)}
    capped = feature_drift(many_features, many_means, top=3)
    check("top=3 截断", len(capped["top"]) == 3, str(capped["top"]))
    check("top 降序且取最大", [name for name, _ in capped["top"]] == ["f09", "f08", "f07"], str(capped["top"]))
    check("top 项为 (feature, z)", capped["top"][0][1] == 10.0, str(capped["top"][0]))
    check("top=0 → 空列表", feature_drift(many_features, many_means, top=0)["top"] == [])

    print("[2b] 训练基线接的是 attribution.TRAIN_MEANS")
    from model_monitor import train_means, train_scales

    real = train_means()
    check("TRAIN_MEANS 可用", isinstance(real, dict) and len(real) >= 17, str(len(real)))
    check("TRAIN_MEANS 含 minute/ownGold", "minute" in real and "ownGold" in real, str(sorted(real)[:3]))
    check("当前无 TRAIN_SCALES → None（scale 用 1.0）", train_scales() is None)


# ---------------------------------------------------------------- C. 快照
def test_model_stats() -> None:
    from model_monitor import model_stats

    print("[2b] model_stats 优先用 metrics.json 的 featureStats")
    with_stats = make_model_dir(
        TMP / "stats_model",
        metrics={
            "featureStats": {
                "ownGold": {"mean": 8000.0, "std": 2500.0, "count": 100},
                "minute": {"mean": 14.0, "std": 0.0, "count": 100},
            }
        },
    )
    means, scales = model_stats(with_stats)
    check("解析 mean", means.get("ownGold") == 8000.0 and means.get("minute") == 14.0, str(means))
    check("std>0 进尺度、std=0 不进", scales is not None and "ownGold" in scales and "minute" not in scales,
          str(scales))
    check("std 值正确", scales is not None and abs(scales["ownGold"] - 2500.0) < 1e-9, str(scales))

    without = make_model_dir(TMP / "stats_empty", metrics={})
    fallback_means, fallback_scales = model_stats(without)
    check("无 featureStats 时回退 TRAIN_MEANS", bool(fallback_means) and fallback_scales is None,
          f"{len(fallback_means)}/{fallback_scales}")


def test_rank_snapshots() -> None:
    import fetch_player_ranks as fpr

    print("[3] 段位历史快照（JSONL 追加）")
    root = TMP / "rank_snapshots"
    records = {
        "puuid-a": {"tier": "DIAMOND", "division": "I", "lp": 50, "score": 3350},
        "puuid-b": {"tier": "MASTER", "division": "NA", "lp": 120, "score": 4120},
    }
    path = fpr.append_rank_snapshot(records, fetched_at=datetime(2026, 11, 14, 12, 0, 0), root=root)
    check("返回写入路径", path is not None, str(path))
    check("文件按日期命名", path.name == "2026-11-14.jsonl", str(path))
    check("文件存在", path.is_file(), str(path))

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("两行 = 两条记录", len(lines) == 2, str(len(lines)))
    rows = [json.loads(line) for line in lines]
    expected = {"fetchedAt", "puuid", "tier", "division", "lp", "score"}
    check("每行 JSON 可解析且字段齐全", all(set(row) == expected for row in rows), str(rows))
    check("fetchedAt 为 ISO 带偏移", rows[0]["fetchedAt"].startswith("2026-11-14T12:00:00"), rows[0]["fetchedAt"])
    by_puuid = {row["puuid"]: row for row in rows}
    check("字段值正确", by_puuid["puuid-a"]["score"] == 3350 and by_puuid["puuid-b"]["tier"] == "MASTER", str(by_puuid))

    fpr.append_rank_snapshot({"puuid-c": {"tier": "GOLD", "division": "II", "lp": 10, "score": 1620}}, root=root)
    lines2 = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("追加不覆盖（旧行仍在）", len(lines2) == 3 and json.loads(lines2[0])["puuid"] == "puuid-a", str(len(lines2)))

    before = path.read_text(encoding="utf-8")
    skipped = fpr.append_rank_snapshot(records, root=root, max_bytes=1)
    check("超过单日上限 → 返回 None（跳过）", skipped is None, str(skipped))
    check("超限时文件未变", path.read_text(encoding="utf-8") == before)

    blocked = TMP / "blocked"
    blocked.write_text("i am a file", encoding="utf-8")
    check("root 不可写 → 返回 None 不抛错", fpr.append_rank_snapshot(records, root=blocked) is None)
    check("空记录 → None", fpr.append_rank_snapshot({}, root=root) is None)
    check("默认目录在 ~/.lol-advisor/rank_snapshots", fpr.SNAPSHOT_ROOT.parts[-2:] == (".lol-advisor", "rank_snapshots"), str(fpr.SNAPSHOT_ROOT))
    check("单日上限=50MB", fpr.SNAPSHOT_MAX_BYTES == 50 * 1024 * 1024, str(fpr.SNAPSHOT_MAX_BYTES))

    main_src = inspect.getsource(fpr.main)
    check(
        "main 在写缓存后调用快照（且只传本次新抓的 fresh）",
        "append_rank_snapshot(fresh)" in main_src and "fresh[puuid] = record" in main_src,
        main_src[-600:],
    )
    cache_src = inspect.getsource(fpr.main)
    check("未改缓存写法（仍 json.dumps(existing)）", "json.dumps(existing" in cache_src)


# ---------------------------------------------------------------- D. /api/home 接线
def test_home_integration() -> None:
    import desktop_app

    print("[4] /api/home 的 modelInfo / drift（不打服务器）")
    check("_model_info_field 存在", callable(getattr(desktop_app, "_model_info_field", None)))
    check("_drift_field 存在", callable(getattr(desktop_app, "_drift_field", None)))
    check("modelInfo 缓存 60s", desktop_app.MODEL_INFO_TTL == 60.0, str(desktop_app.MODEL_INFO_TTL))

    desktop_app._MODEL_INFO_CACHE.update(at=time.time(), data={"cached": True})
    check("60s 内命中缓存", desktop_app._model_info_field() == {"cached": True})
    desktop_app._MODEL_INFO_CACHE.update(at=0.0, data=None)
    info = desktop_app._model_info_field()
    check(
        "真实模型目录可体检",
        isinstance(info, dict) and {"name", "ageDays", "patch", "warning"} <= set(info),
        str(info),
    )

    class FakeState:
        def __init__(self, features: Any):
            self.lock = threading.Lock()
            self.features = features

        def as_json(self) -> Dict[str, Any]:
            return {"ok": True}

    saved_state = desktop_app._HOME_CONTEXT.get("state")
    saved_io = {
        name: getattr(desktop_app, name)
        for name in ("_lcu_info", "_draft_rank_bridge", "_solo_rank_snapshot", "draft_running", "_service_alive")
    }
    try:
        from model_monitor import train_means

        means = train_means()
        desktop_app._HOME_CONTEXT["state"] = FakeState(dict(means))
        drift = desktop_app._drift_field()
        check(
            "最近一次特征 == 训练均值 → drift ok/0",
            isinstance(drift, dict) and drift["level"] == "ok" and drift["score"] == 0.0,
            str(drift),
        )

        desktop_app._HOME_CONTEXT["state"] = FakeState({})
        check("无特征 → drift=None", desktop_app._drift_field() is None)
        desktop_app._HOME_CONTEXT["state"] = FakeState(object())
        check("特征类型异常 → drift=None（静默）", desktop_app._drift_field() is None)

        desktop_app._HOME_CONTEXT["state"] = FakeState(dict(means))
        desktop_app._lcu_info = lambda: {"connected": False, "port": None}  # type: ignore[assignment]
        desktop_app._draft_rank_bridge = lambda: None  # type: ignore[assignment]
        desktop_app._solo_rank_snapshot = lambda: None  # type: ignore[assignment]
        desktop_app.draft_running = lambda: False  # type: ignore[assignment]
        desktop_app._service_alive = lambda port: False  # type: ignore[assignment]
        payload = desktop_app._home_payload()
        check("/api/home 含 modelInfo 字段", "modelInfo" in payload and payload["modelInfo"] is not None, str(payload.keys()))
        check("/api/home 含 drift 字段", "drift" in payload and payload["drift"] is not None, str(payload.keys()))
        check("/api/home 原字段仍在", payload["ok"] is True and "state" in payload and "selftest" in payload)
        json.dumps(payload)  # 序列化不炸

        # 失败必须静默：监控件炸了 /api/home 也得活着，字段为 null。
        desktop_app._MODEL_INFO_CACHE.update(at=0.0, data=None)
        saved_resolve = desktop_app.resolve_assets
        desktop_app.resolve_assets = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        try:
            check("体检异常 → modelInfo=None（静默）", desktop_app._model_info_field() is None)
        finally:
            desktop_app.resolve_assets = saved_resolve  # type: ignore[assignment]
        desktop_app._MODEL_INFO_CACHE.update(at=0.0, data=None)

        payload2 = desktop_app._home_payload()
        check("/api/home 在监控异常后仍返回 200 形状", payload2["ok"] is True and payload2["modelInfo"] is not None)
    finally:
        for name, value in saved_io.items():
            setattr(desktop_app, name, value)
        if saved_state is None:
            desktop_app._HOME_CONTEXT.pop("state", None)
        else:
            desktop_app._HOME_CONTEXT["state"] = saved_state
        desktop_app._MODEL_INFO_CACHE.update(at=0.0, data=None)

    src = inspect.getsource(desktop_app._home_payload)
    check("_home_payload 代码路径写入两个新字段", '"modelInfo"' in src and '"drift"' in src, src[-500:])


def main() -> int:
    start = time.time()
    shutil.rmtree(TMP, ignore_errors=True)
    try:
        test_model_info()
        test_feature_drift()
        test_model_stats()
        test_rank_snapshots()
        test_home_integration()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    check("临时目录已清理", not TMP.exists())
    print(f"\n结果: PASS={PASS} FAIL=0  用时 {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError:
        shutil.rmtree(TMP, ignore_errors=True)
        raise SystemExit(1)
