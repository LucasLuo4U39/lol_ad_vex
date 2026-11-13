#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛后复盘渲染层自测：不依赖数据集，只用合成摘要验证 Markdown/HTML 输出。

运行：python test_review.py（只需要标准库）
"""

from __future__ import annotations

from pathlib import Path

from match_review import (
    _cs_trajectory,
    _event_lines,
    _event_weight,
    _role_verdict,
    clock,
    markdown_to_html,
    render_html,
    render_markdown,
    sparkline,
)

PASS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        raise AssertionError(f"{name}: {detail}")


def sample_summary() -> dict:
    minutes = []
    probs = [0.54, 0.60, 0.72, 0.66, 0.58, 0.44, 0.51, 0.63, 0.77, 0.83, 0.90, 0.95]
    for index, prob in enumerate(probs, start=1):
        minutes.append(
            {
                "m": index,
                "p100": prob,
                "kills": [index % 3, 1],
                "cs": [index * 20, index * 18],
                "level": [index * 2, index * 2 - 1],
            }
        )
    roles = [
        {
            "role": "TOP",
            "side": 100,
            "champion": "Ambessa",
            "kda": [3, 6, 5],
            "cs": 190,
            "csPerMin": 7.6,
            "gold": 1200,
            "level": 16,
            "at": {
                "10": {"w": {"cs": 78}, "l": {"cs": 82}},
                "15": {"w": {"cs": 112}, "l": {"cs": 120}},
                "20": {"w": {"cs": 150}, "l": {"cs": 160}},
            },
            "opp": {
                "champion": "Jayce",
                "kda": [6, 3, 9],
                "cs": 233,
                "csPerMin": 7.8,
                "gold": 1500,
                "level": 17,
            },
        },
        {
            "role": "JUNGLE",
            "side": 100,
            "champion": "Viego",
            "kda": [11, 2, 7],
            "cs": 238,
            "csPerMin": 8.5,
            "gold": 2200,
            "level": 17,
            "at": {},
            "opp": {
                "champion": "Khazix",
                "kda": [4, 9, 4],
                "cs": 127,
                "csPerMin": 4.6,
                "gold": 900,
                "level": 13,
            },
        },
    ]
    return {
        "gameId": "999000000001",
        "source": "SGP 1 分钟帧（10 名玩家视角回填）",
        "model": "live_model3（150 棵树）",
        "duration": 12 * 60,
        "winner": 100,
        "teams": {
            "100": {"kills": 32, "deaths": 18, "assists": 70, "cs": 2100, "levelSum": 70,
                    "towers": 9, "inhibitors": 2, "dragons": 3, "barons": 1, "heralds": 1,
                    "grubs": 4, "atakhan": 0},
            "200": {"kills": 18, "deaths": 32, "assists": 40, "cs": 1900, "levelSum": 66,
                    "towers": 3, "inhibitors": 0, "dragons": 1, "barons": 0, "heralds": 0,
                    "grubs": 0, "atakhan": 0},
        },
        "roles": roles,
        "minutes": minutes,
        "events": [
            {"t": 120, "side": 100, "kind": "kill", "text": "蓝方拿到一血"},
            {"t": 300, "side": 200, "kind": "objective", "text": "红方拿下小龙"},
            {"t": 900, "side": 100, "kind": "objective", "text": "蓝方拿下大龙"},
        ],
        "swings": [{"m": 6, "delta": -0.14, "p": 0.44}, {"m": 9, "delta": 0.12, "p": 0.77}],
        "notes": ["金币是各玩家兜里的现金（currentGold），不是团队总经济"],
        "advice": ["负方上路补刀落后 43，下一局先换线。", "拿完大龙要立刻转线推塔。"],
    }


def test_helpers() -> None:
    print("[1] 基础工具")
    check("clock 格式", clock(0) == "00:00" and clock(2499) == "41:39", f"{clock(2499)}")
    line = sparkline([0.0, 0.5, 1.0], width=10)
    check("sparkline 只含方块字符", set(line) <= set("▁▂▃▄▅▆▇█"), line)
    check("sparkline 不超宽", len(sparkline([0.3] * 200, width=40)) == 40)
    check("空序列安全", sparkline([]) == "")


def test_events() -> None:
    print("[2] 事件筛选")
    events = []
    for index in range(60):
        events.append({"t": index * 60, "side": 100, "kind": "kill", "text": f"蓝方击杀 {index}"})
    events[10] = {"t": 600, "side": 100, "kind": "kill", "text": "蓝方拿到一血"}
    events[30] = {"t": 1800, "side": 200, "kind": "objective", "text": "红方拿下大龙"}
    lines = _event_lines({"events": events}, limit=24)
    check("限制条数", len(lines) == 24, str(len(lines)))
    joined = "\n".join(lines)
    check("保留一血", "一血" in joined)
    check("保留大龙", "大龙" in joined)
    check("按时间排序", lines == sorted(lines), "order")
    check("权重：大龙 > 塔 > 击杀",
          _event_weight({"text": "红方拿下大龙"})
          > _event_weight({"text": "红方拿下防御塔"})
          > _event_weight({"text": "红方一分钟内击杀 2 人"}))


def test_markdown(summary: dict) -> None:
    print("[3] Markdown")
    md = render_markdown(summary, narrative="这是一段人工点评。")
    check("标题含对局与胜方", "# 999000000001 · 12:00 · 蓝方胜" in md, md.splitlines()[0])
    check("包含人工点评", "这是一段人工点评。" in md)
    check("包含曲线", "蓝方胜率" in md and "▁" in md or "█" in md)
    check("角色表 5 列表头", "| 位置 | 胜方英雄 |" in md)
    check("两条角色", md.count("\n| 上路 |") == 1 and md.count("\n| 打野 |") == 1)
    check("补刀轨迹", "补刀轨迹（胜方:负方）：10′ 78:82" in md)
    check("建议列表", "负方上路补刀落后 43" in md)
    check("团队数据", "小龙 3" in md and "大龙 1" in md)


def test_html(summary: dict) -> None:
    print("[4] HTML")
    html = render_html(summary, narrative="**粗体** 与 `代码`。")
    check("doctype", html.startswith("<!doctype html>"))
    check("标题转义", "<h1>999000000001" in html)
    check("表格渲染", "<table>" in html and "<th>位置</th>" in html)
    check("粗体渲染", "<strong>粗体</strong>" in html)
    check("行内代码", "<code>代码</code>" in html)
    check("代码块闭合", html.count("<pre>") == html.count("</pre>"))
    converted = markdown_to_html("## 小标题\n\n- 一项\n- 两项\n")
    check("二级标题", "<h2>小标题</h2>" in converted)
    check("列表", converted.count("<li>") == 2)


def test_role_verdict() -> None:
    print("[5] 位置判词")
    entry = {
        "role": "JUNGLE",
        "champion": "Viego",
        "kda": [11, 1, 7],
        "cs": 238,
        "level": 17,
        "opp": {"champion": "Khazix", "kda": [4, 9, 4], "cs": 127, "level": 13},
        "at": {"10": {"w": {"cs": 70}, "l": {"cs": 57}}},
    }
    line = _role_verdict(entry)
    check("胜方占优判定", "胜方占优" in line, line)
    check("补刀差值", "补刀 +111" in line, line)
    check("轨迹函数", _cs_trajectory(entry) == "10′ 70:57", _cs_trajectory(entry))


def test_real_reports() -> None:
    print("[6] 已生成的 10 份复盘（存在则校验结构）")
    root = Path(__file__).resolve().parent.parent / "analysis" / "game-reviews"
    reports = sorted(root.glob("*/report.md"))
    if not reports:
        print("  SKIP 还没有生成报告")
        return
    for path in reports:
        text = path.read_text(encoding="utf-8")
        check(f"{path.parent.name} 结构完整",
              "## 结论" in text and "## 全位置对比" in text and text.count("\n| ") >= 6)
        check(f"{path.parent.name} 有 5 个位置",
              sum(1 for label in ("上路", "打野", "中路", "下路", "辅助") if f"| {label} |" in text) == 5)


def main() -> int:
    summary = sample_summary()
    test_helpers()
    test_events()
    test_markdown(summary)
    test_html(summary)
    test_role_verdict()
    test_real_reports()
    print(f"\n全部通过: {PASS} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
