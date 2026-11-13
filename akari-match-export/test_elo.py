#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选手 Elo 与「Elo 介入胜率曲线」的自测。

- 纯逻辑（Elo 更新、期望胜率、混合、缓存/JSON 读取）不依赖网络；
- 用合成对局验证「按时间重放、同局去重、新号 K 更大」；
- 用真实 game_champs.json + 合成 DataFrame 验证 build_summary 的混合结果。

运行：python test_elo.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from match_review import render_markdown
from player_elo import (
    BASE_RATING,
    blend_weight,
    blend_win_prob,
    compute_ratings,
    elo_probability,
    expected_score,
    filter_history,
    load_elo_map,
    match_from_summary,
    team_elos,
    team_rating,
    update_pair,
)
from review_games import build_summary

PASS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        raise AssertionError(f"{name}: {detail}")


def synthetic_match(game_id: str, creation: int, winner: int, blue, red) -> dict:
    return {
        "gameId": game_id,
        "creation": creation,
        "queueId": 420,
        "duration": 1800,
        "winner": winner,
        "teams": {100: list(blue), 200: list(red)},
    }


def test_elo_math() -> None:
    print("[1] Elo 数学")
    check("同分期望 0.5", abs(expected_score(1500, 1500) - 0.5) < 1e-12)
    check("+200 期望 ≈ 0.76", abs(expected_score(1700, 1500) - 0.7597469) < 1e-6, str(expected_score(1700, 1500)))
    check("互补", abs(expected_score(1500, 1700) + expected_score(1700, 1500) - 1.0) < 1e-12)
    a, b = update_pair(1500, 1500, 1.0)
    check("赢家涨分、总分守恒", a > 1500 > b and abs((a + b) - 3000) < 1e-9, f"{a},{b}")
    check("K 影响幅度", abs(update_pair(1500, 1500, 1.0, k=48)[0] - 1524) < 1e-9)


def test_compute_ratings() -> None:
    print("[2] 对局重放")
    blue = [f"b{i}" for i in range(5)]
    red = [f"r{i}" for i in range(5)]
    matches = [synthetic_match(f"g{i}", 1000 + i, 100, blue, red) for i in range(10)]
    result = compute_ratings(matches)
    ratings = result["ratings"]
    check("10 局全部计入", result["games"] == 10, str(result["games"]))
    check("蓝方整体涨分", team_rating(ratings, blue) > 1500 > team_rating(ratings, red))

    single = compute_ratings([synthetic_match("x1", 1, 100, blue, red)])["ratings"]
    check("新号首场 K=48", abs(single["b0"] - 1524) < 1e-9, str(single["b0"]))

    duplicated = matches + matches[:3]
    dedup = compute_ratings(duplicated)
    check("同一 gameId 去重", dedup["games"] == 10, str(dedup["games"]))
    check("去重后评分一致", abs(dedup["ratings"]["b0"] - ratings["b0"]) < 1e-9)

    # 顺序不影响结果：倒序输入后按 creation 重排
    shuffled = list(reversed(matches))
    check("输入顺序无关", abs(compute_ratings(shuffled)["ratings"]["b0"] - ratings["b0"]) < 1e-9)

    # 第 11 场之后 K 回到 32
    more = matches + [synthetic_match("g10", 1010, 100, blue, red)]
    r2 = compute_ratings(more)["ratings"]
    delta = r2["b0"] - ratings["b0"]
    check("第 11 场按 K=32", 0 < delta < 32, str(delta))


def test_probability() -> None:
    print("[3] Elo → 胜率")
    check("同分 50%", abs(elo_probability(0) - 0.5) < 1e-12)
    check("+200 ≈ 76%", abs(elo_probability(200) - 0.7597469) < 1e-6)
    check("对称", abs(elo_probability(137) + elo_probability(-137) - 1.0) < 1e-12)


def test_blend() -> None:
    print("[4] 介入曲线")
    check("t=0 权重等于设定值", abs(blend_weight(0, 0.5, 480) - 0.5) < 1e-12)
    check("权重随时间衰减", blend_weight(480, 0.5, 480) < 0.5 * 0.4)
    check("tau<=0 不介入", blend_weight(60, 0.5, 0) == 0.0)

    model_p, elo_p = 0.40, 0.62
    at0 = blend_win_prob(model_p, elo_p, 0, 0.5, 480)
    at60 = blend_win_prob(model_p, elo_p, 60, 0.5, 480)
    late = blend_win_prob(model_p, elo_p, 600, 0.5, 480)
    expected0 = 1 / (1 + math.exp(-(0.5 * math.log(elo_p / (1 - elo_p)) + 0.5 * math.log(model_p / (1 - model_p)))))
    check("t=0 等于 logit 混合", abs(at0 - expected0) < 1e-12, f"{at0} vs {expected0}")
    check("随时间回到模型", at0 > at60 > late and abs(late - model_p) < 1e-3, f"{at0},{at60},{late}")
    check("相同输入不变", abs(blend_win_prob(0.7, 0.7, 300, 0.8, 480) - 0.7) < 1e-12)
    check("不穿越边界", 0 < blend_win_prob(0.01, 0.99, 0, 1.0, 480) < 1)


def test_summary_parsing() -> None:
    print("[5] SGP 解析与过滤")
    summary_game = {
        "gameId": 123,
        "gameCreation": 1700000000000,
        "queueId": 420,
        "gameDuration": 1800,
        "participants": [
            {"participantId": i + 1, "teamId": 100 if i < 5 else 200, "stats": {"win": i < 5}}
            for i in range(10)
        ],
        "participantIdentities": [
            {"participantId": i + 1, "player": {"puuid": f"p{i}"}} for i in range(10)
        ],
    }
    parsed = match_from_summary(summary_game)
    check("解析出 5v5", parsed and len(parsed["teams"][100]) == 5 and len(parsed["teams"][200]) == 5)
    check("胜方 100", parsed["winner"] == 100)
    check("缺少 identities 返回 None", match_from_summary({"participants": []}) is None)

    games = [
        {"gameId": 1, "queueId": 420, "gameDuration": 1800},
        {"gameId": 2, "queueId": 450, "gameDuration": 1800},
        {"gameId": 3, "queueId": 420, "gameDuration": 300},
    ]
    kept = filter_history(games)
    check("只保留排位且 ≥10 分钟", [g["gameId"] for g in kept] == [1], str(kept))

    from player_elo import filter_before

    before = filter_before(
        [{"creation": 100}, {"creation": 200}, {"creation": 300}], 250
    )
    check("只保留赛前对局", [m["creation"] for m in before] == [100, 200], str(before))
    check("不给 as_of 时全保留", len(filter_before([{"creation": 1}], None)) == 1)


def test_json_io(tmp_path: Path) -> None:
    print("[6] JSON 读写")
    single = tmp_path / "single.json"
    single.write_text(
        json.dumps(
            {"gameId": "999", "teamElo": {"100": 1600, "200": 1400}, "prob": 0.76,
             "players": {"100": [{"name": "A", "elo": 1600}], "200": []}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = load_elo_map(single)
    check("单局 JSON 包成 map", list(loaded) == ["999"], str(list(loaded)))
    blue, red = team_elos(loaded["999"])
    check("队伍 Elo 读取", (blue, red) == (1600.0, 1400.0))
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps({"1": {"teamElo": {"100": 1500, "200": 1500}}}), encoding="utf-8")
    check("多局 JSON", list(load_elo_map(multi)) == ["1"])


def test_build_summary_with_elo() -> None:
    print("[7] 复盘摘要注入 Elo")
    game_id = "901093877862"  # 存在于 game_champs.json
    rows = []
    for minute in (1, 2, 3):
        for side, win in ((100, 1), (200, 0)):
            for role in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"):
                rows.append(
                    {
                        "gameId": int(game_id),
                        "minute": minute,
                        "role": role,
                        "ownWin": win,
                        "ownLevel": minute + 1,
                        "ownGold": 300.0,
                        "ownCS": 10.0 * minute,
                        "ownKills": 0,
                        "ownDeaths": 0,
                        "ownAssists": 0,
                        "teamKills": minute,
                        "teamDeaths": minute,
                        "teamAssists": minute,
                        "teamCS": 50 * minute,
                        "teamLevelSum": 5 * (minute + 1),
                        "dragonDiff": 0,
                        "baronDiff": 0,
                        "heraldDiff": 0,
                        "grubDiff": 0,
                        "atakhanDiff": 0,
                        "towerDiff": 0,
                        "inhibitorDiff": 0,
                        "side": side,
                        "prob": 0.45 if side == 100 else 0.55,
                    }
                )
    frame = pd.DataFrame(rows)
    elo = {"teamElo": {"100": 1700, "200": 1300}, "source": "test", "players": {"100": [{"name": "B1", "elo": 1700}], "200": []}}
    summary = build_summary(frame, game_id, frame["prob"].to_numpy(), "test-model", elo=elo, elo_weight=0.5, elo_tau=480)
    check("摘要带 elo 块", summary["elo"] is not None and summary["elo"]["prob"] > 0.9, str(summary["elo"]))
    first = summary["minutes"][0]
    check("保留模型原始胜率", abs(first["pModel"] - 0.45) < 1e-9, str(first["pModel"]))
    check("第 1 分钟被拉高", first["p100"] > 0.55, str(first["p100"]))
    check("末分钟更接近模型", summary["minutes"][-1]["p100"] < first["p100"])
    report = render_markdown(summary, narrative="示例点评。")
    check("报告含 Elo 段", "## 赛前实力（Elo）" in report)
    check("曲线标注模型+Elo", "蓝方胜率（模型+Elo）" in report)
    check("赛前胜率写在报告里", "91%" in report)


def main() -> int:
    import tempfile

    test_elo_math()
    test_compute_ratings()
    test_probability()
    test_blend()
    test_summary_parsing()
    with tempfile.TemporaryDirectory() as tmp:
        test_json_io(Path(tmp))
    test_build_summary_with_elo()
    print(f"\n全部通过: {PASS} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
