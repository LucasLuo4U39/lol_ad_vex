"""D2（T60/T61）回归测试：目标物按时间截断、事件提醒闸门、阈值预算口径、策略文件加载。

全部用假数据，不依赖 LCU / 训练 CSV，由 run-tests.cmd 调用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from compute_event_thresholds import budget_threshold  # noqa: E402
from live_features import (  # noqa: E402
    DEFAULT_EVENT_THRESHOLDS,
    EventAlertGate,
    advice,
    load_event_policy,
    objective_counts,
)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}  {detail}")


def sample_payload() -> dict:
    return {
        "allPlayers": [
            {"summonerName": "A", "team": "ORDER"},
            {"summonerName": "B", "team": "CHAOS"},
        ],
        "events": {
            "Events": [
                {"EventName": "DragonKill", "KillerName": "A", "EventTime": 300},
                {"EventName": "BaronKill", "KillerName": "A", "EventTime": 1500},
                # 回放 /eventdata 的时间是毫秒
                {"EventName": "TurretKilled", "KillerName": "B", "EventTime": 1500000},
                {"EventName": "HeraldKill", "KillerName": "B", "EventTime": 480},
            ]
        },
    }


def test_objective_truncation() -> None:
    print("[1] 目标物按对局时间截断（T61）")
    payload = sample_payload()
    at_10min = objective_counts(payload, 600)
    check("10 分钟时已拿到小龙", at_10min["ORDER"]["dragons"] == 1, str(at_10min["ORDER"]))
    check("10 分钟时还没有大龙", at_10min["ORDER"]["barons"] == 0, str(at_10min["ORDER"]))
    check("毫秒时间戳的先锋按秒计（480s）", at_10min["CHAOS"]["heralds"] == 1, str(at_10min["CHAOS"]))
    check("毫秒时间戳的防御塔 1500s 不计入 10 分钟", at_10min["CHAOS"]["towers"] == 0, str(at_10min["CHAOS"]))
    full = objective_counts(payload)
    check("不给时间时统计全部（兼容旧行为）",
          full["ORDER"]["barons"] == 1 and full["CHAOS"]["towers"] == 1, str(full))


def test_alert_gate() -> None:
    print("[2] 事件提醒闸门：确认 + 最小差 + 冷却（T60）")
    gate = EventAlertGate(
        thresholds={"own_kill_30s": 0.35, "enemy_kill_30s": 0.35,
                    "own_gank_60s": 0.39, "enemy_gank_60s": 0.39},
        min_gap=0.10, confirmations=2, cooldown_seconds=180,
    )
    probs = {"own_kill_30s": 0.52, "enemy_kill_30s": 0.20,
             "own_gank_60s": 0.20, "enemy_gank_60s": 0.20}
    first = gate.update(probs, 600)
    check("第一次只满足条件不提醒（需连续 2 次确认）", first["own_kill_30s"] is False, str(first))
    second = gate.update(probs, 605)
    check("第二次确认后提醒", second["own_kill_30s"] is True, str(second))
    third = gate.update(probs, 610)
    check("冷却期内不重复提醒", third["own_kill_30s"] is False, str(third))
    after = gate.update(probs, 800)
    check("冷却 180 秒后可以再次提醒", after["own_kill_30s"] is True, str(after))

    # 最小差：双方都高、差距不足时不提醒
    gate2 = EventAlertGate(
        thresholds={"own_kill_30s": 0.35, "enemy_kill_30s": 0.35,
                    "own_gank_60s": 0.39, "enemy_gank_60s": 0.39},
        min_gap=0.15, confirmations=1, cooldown_seconds=0,
    )
    close = gate2.update({"own_kill_30s": 0.45, "enemy_kill_30s": 0.40,
                          "own_gank_60s": 0.20, "enemy_gank_60s": 0.20}, 600)
    check("双方概率接近（差 0.05 < minGap 0.15）不提醒", close["own_kill_30s"] is False, str(close))
    clear = gate2.update({"own_kill_30s": 0.60, "enemy_kill_30s": 0.40,
                          "own_gank_60s": 0.20, "enemy_gank_60s": 0.20}, 605)
    check("差距足够时提醒", clear["own_kill_30s"] is True, str(clear))

    # 新一局（时间回退）自动重置
    gate3 = EventAlertGate(confirmations=1, cooldown_seconds=180,
                           thresholds={"own_kill_30s": 0.35})
    gate3.update(probs, 900)
    rewound = gate3.update(probs, 20)
    check("对局时间回退视为新局，重新计数",
          rewound["own_kill_30s"] is True, str(rewound))


def test_advice_with_gate() -> None:
    print("[3] 建议只输出闸门放行的事件（T60）")
    features = {"killDiff": 0, "csDiff": 0, "levelSumDiff": 0, "dragonDiff": 0, "baronDiff": 0}
    gate = EventAlertGate(
        thresholds={"own_kill_30s": 0.35, "enemy_kill_30s": 0.35,
                    "own_gank_60s": 0.39, "enemy_gank_60s": 0.39},
        min_gap=0.10, confirmations=2, cooldown_seconds=180,
    )
    probs = {"own_kill_30s": 0.52, "enemy_kill_30s": 0.20,
             "own_gank_60s": 0.20, "enemy_gank_60s": 0.20}
    first = advice(features, 0.55, probs, alert_gate=gate, game_time=600)
    second = advice(features, 0.55, probs, alert_gate=gate, game_time=605)
    check("第一次没有事件建议", not any("击杀" in t for t in first), str(first))
    check("第二次出现我方击杀建议",
          any("我方更可能拿到击杀" in t for t in second), str(second))
    check("没有反过来建议敌方", not any("敌方更可能拿到击杀" in t for t in second), str(second))


def test_policy_loading() -> None:
    print("[4] 策略文件加载")
    path = BASE.parent / "dataset" / "live_dataset" / "live_event_model" / "event_thresholds.json"
    if not path.is_file():
        check("策略文件存在", False, str(path))
        return
    policy = load_event_policy(path)
    check("阈值来自文件", abs(policy["thresholds"]["own_kill_30s"] - 0.352111) < 1e-6,
          str(policy["thresholds"]))
    check("读取 minGap", float(policy["minGap"]) == 0.10, str(policy["minGap"]))
    check("读取 confirmations", int(policy["confirmations"]) == 2, str(policy["confirmations"]))
    check("读取 cooldownSeconds", float(policy["cooldownSeconds"]) == 180.0, str(policy["cooldownSeconds"]))
    missing = load_event_policy(BASE / "不存在.json")
    check("文件缺失时回退默认阈值", missing["thresholds"] == DEFAULT_EVENT_THRESHOLDS, str(missing["thresholds"]))
    gate = EventAlertGate.from_policy(policy)
    check("从策略构造闸门", gate.cooldown == 180.0 and gate.confirmations == 2, f"{gate.cooldown}/{gate.confirmations}")


def test_budget_threshold() -> None:
    print("[5] 预算口径阈值（T60）")
    scores = np.linspace(0.0, 1.0, 1000)
    labels = (scores > 0.7).astype(int)
    result = budget_threshold(labels, scores, alerts_per_game=8, rows_per_game=250)
    check("触发率不超过预算（8/250=3.2%）",
          result["alertRate"] <= 0.032 + 1e-9, str(result["alertRate"]))
    check("阈值取在预算内最高召回处", result["threshold"] >= 0.95, str(result["threshold"]))
    tiny = budget_threshold(labels, scores, alerts_per_game=0, rows_per_game=250)
    check("预算为 0 时只保留最像的一条", tiny["alertRate"] <= 0.001 + 1e-9, str(tiny["alertRate"]))
    empty = budget_threshold(np.zeros(10, dtype=int), np.linspace(0, 1, 10), 8, 250)
    check("无正样本时安全返回", empty["threshold"] == 1.0, str(empty))


def main() -> int:
    test_objective_truncation()
    test_alert_gate()
    test_advice_with_gate()
    test_policy_loading()
    test_budget_threshold()
    print("=" * 60)
    print(f"  汇总: {PASSED}/{PASSED + FAILED} 通过")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
