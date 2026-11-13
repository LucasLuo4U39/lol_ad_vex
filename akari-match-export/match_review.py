#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""赛后复盘渲染层：把一局的结构化摘要渲染成 Markdown / HTML。

两种数据源共用同一份摘要 schema：

- `sgp-1min`：历史对局回填（live_dataset_v3.csv + game_champs.json），
  10 名玩家 × 每分钟的等级/补刀/KDA/兜里金币 + 团队差值；
- `live`：实时录制（advisor_server 每 5 秒采样后落盘）。

摘要 schema
-----------
```
{
  "gameId": "901121900494", "source": "sgp-1min", "duration": 2472, "winner": 100,
  "teams": {"100": {"kills":..,"deaths":..,"assists":..,"cs":..,"levelSum":..,
                    "towers":..,"inhibitors":..,"dragons":..,"barons":..,
                    "heralds":..,"grubs":..,"atakhan":..}, "200": {...}},
  "roles": [{"role":"TOP","side":100,"champion":"Ambessa",
             "kda":[3,6,5],"cs":190,"csPerMin":7.6,"gold":1200,"level":16,
             "at":{"10":{"level":8,"cs":70,"kda":[1,3,1]}, ...},
             "opp":{"champion":"Jayce","kda":[..],"cs":..,"csPerMin":..,"gold":..,"level":..,
                    "at":{...}}}],
  "minutes": [{"m":1,"p100":0.52,"kills":[0,0],"cs":[10,10],"level":[5,5]}],
  "events": [{"t":480,"side":100,"kind":"dragon","text":"蓝方拿下小龙"}],
  "swings": [{"m":22,"delta":-0.092,"p":0.31}],
  "notes": ["金币为兜里现金"]
}
```

`render_markdown()` 只做事实陈述（表格、曲线、事件），解析性文字放在可选的
`narrative` 参数里（LLM 或人工撰写）；没给 narrative 时用规则判词兜底。
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

ROLE_LABELS = {
    "TOP": "上路",
    "JUNGLE": "打野",
    "MIDDLE": "中路",
    "BOTTOM": "下路",
    "UTILITY": "辅助",
}
ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

OBJECTIVE_LABELS = {
    "dragons": "小龙",
    "barons": "大龙",
    "heralds": "峡谷先锋",
    "grubs": "虚空巢虫",
    "atakhan": "厄塔汗",
    "towers": "防御塔",
    "inhibitors": "水晶",
}

SIDE_LABELS = {100: "蓝方", 200: "红方"}
BLOCKS = "▁▂▃▄▅▆▇█"


def clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def sparkline(values: List[float], width: int = 56) -> str:
    """把 0..1 的序列压成一行方块字符，用于 Markdown 里画走势。"""
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[min(len(values) - 1, int(i * step))] for i in range(width)]
    chars = []
    for value in values:
        clamped = min(1.0, max(0.0, float(value)))
        chars.append(BLOCKS[min(len(BLOCKS) - 1, int(clamped * len(BLOCKS)))])
    return "".join(chars)


def _pct(value: Optional[float]) -> str:
    return "--" if value is None else f"{value * 100:.0f}%"


def _fmt(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.1f}" if abs(value - round(value)) > 1e-9 else f"{int(round(value))}"
    return str(value)


def _role_row(entry: Dict[str, Any]) -> str:
    own = entry
    opp = entry.get("opp") or {}
    label = ROLE_LABELS.get(entry.get("role", ""), entry.get("role", "?"))
    kda = "/".join(str(int(x)) for x in own.get("kda", [0, 0, 0]))
    opp_kda = "/".join(str(int(x)) for x in opp.get("kda", [0, 0, 0]))
    own_cs = f"{_fmt(own.get('cs'))}（{_fmt(own.get('csPerMin'))}/分）"
    opp_cs = f"{_fmt(opp.get('cs'))}（{_fmt(opp.get('csPerMin'))}/分）"
    return (
        f"| {label} | {own.get('champion', '?')} | {kda} | {own_cs} | {_fmt(own.get('level'))} "
        f"| {opp.get('champion', '?')} | {opp_kda} | {opp_cs} | {_fmt(opp.get('level'))} |"
    )


def _role_verdict(entry: Dict[str, Any]) -> str:
    """用数据给每个位置一句中性判词（补刀轨迹 / 等级 / 阵亡），不评价操作。"""
    label = ROLE_LABELS.get(entry.get("role", ""), entry.get("role", "?"))
    own, opp = entry, entry.get("opp") or {}
    own_kda = own.get("kda") or [0, 0, 0]
    opp_kda = opp.get("kda") or [0, 0, 0]
    level_diff = float(own.get("level") or 0) - float(opp.get("level") or 0)
    own_deaths, opp_deaths = int(own_kda[1]), int(opp_kda[1])
    deaths_diff = own_deaths - opp_deaths

    if entry.get("role") == "UTILITY":
        # 辅助的补刀没有比较意义，改用助攻/阵亡/等级
        assist_diff = int(own_kda[2]) - int(opp_kda[2])
        score = assist_diff * 0.03 - deaths_diff * 0.1 + level_diff * 0.4
        parts = [
            f"助攻 {int(own_kda[2])} vs {int(opp_kda[2])}",
            f"等级 {level_diff:+.0f}",
            f"阵亡 {own_deaths} vs {opp_deaths}",
        ]
    else:
        own_cs, opp_cs = float(own.get("cs") or 0), float(opp.get("cs") or 0)
        cs_diff = own_cs - opp_cs
        score = cs_diff / 100.0 + level_diff * 0.6 - deaths_diff * 0.08
        parts = [
            f"补刀 {cs_diff:+.0f}（{own_cs:.0f} vs {opp_cs:.0f}）",
            f"等级 {level_diff:+.0f}",
            f"阵亡 {own_deaths} vs {opp_deaths}",
        ]

    tag = "胜方占优" if score > 0.25 else "负方占优" if score < -0.25 else "基本均势"
    trajectory = "" if entry.get("role") == "UTILITY" else _cs_trajectory(entry)
    suffix = f"补刀轨迹（胜方:负方）：{trajectory}。" if trajectory else ""
    return (
        f"- **{label}**（{tag}）：{own.get('champion', '?')} 对 {opp.get('champion', '?')}，"
        + "、".join(parts)
        + "。"
        + suffix
    )


def _cs_trajectory(entry: Dict[str, Any]) -> str:
    at = entry.get("at") or {}
    chunks = []
    for mark in ("10", "15", "20"):
        snapshot = at.get(mark)
        if not snapshot:
            continue
        winner_cs = (snapshot.get("w") or {}).get("cs")
        loser_cs = (snapshot.get("l") or {}).get("cs")
        if winner_cs is None or loser_cs is None:
            continue
        chunks.append(f"{mark}′ {winner_cs:.0f}:{loser_cs:.0f}")
    return " → ".join(chunks)


def _phase_lines(summary: Dict[str, Any]) -> List[str]:
    minutes = summary.get("minutes") or []
    if not minutes:
        return []
    phases = [(0, 10, "前期 0–10 分钟"), (10, 20, "中期 10–20 分钟"), (20, 10**6, "后期 20 分钟后")]
    lines = []
    for start, end, label in phases:
        window = [row for row in minutes if start < row["m"] <= end]
        if not window:
            continue
        first, last = window[0], window[-1]
        kills = [sum(row["kills"][0] for row in window), sum(row["kills"][1] for row in window)]
        prob_delta = (last["p100"] or 0) - (first["p100"] or 0)
        lines.append(
            f"- **{label}**：胜率 {_pct(first['p100'])} → {_pct(last['p100'])}"
            f"（{prob_delta * 100:+.0f}pp），击杀 {kills[0]}:{kills[1]}。"
        )
    return lines


def _event_weight(event: Dict[str, Any]) -> int:
    text = event.get("text", "")
    if "一血" in text:
        return 5
    if any(word in text for word in ("大龙", "厄塔汗", "水晶")):
        return 4
    if any(word in text for word in ("小龙", "先锋", "巢虫")):
        return 3
    if "防御塔" in text:
        return 2
    return 1


def _event_lines(summary: Dict[str, Any], limit: int = 24) -> List[str]:
    events = summary.get("events") or []
    if not events:
        return ["- （这段时间没有可用的击杀/目标事件）"]
    ordered = sorted(events, key=lambda e: e["t"])
    if len(ordered) > limit:
        # 按时间分桶，每个桶取权重最高的一条 —— 保证时间轴均匀、关键目标不丢
        picks: List[Dict[str, Any]] = []
        for index in range(limit):
            start = index * len(ordered) // limit
            end = (index + 1) * len(ordered) // limit
            bucket = ordered[start:end]
            if bucket:
                picks.append(max(bucket, key=_event_weight))
        ordered = picks
    return [f"- `{clock(e['t'])}` {e['text']}" for e in ordered]


def _swing_lines(summary: Dict[str, Any]) -> List[str]:
    swings = summary.get("swings") or []
    if not swings:
        return ["- 没有单分钟超过 8pp 的胜率波动。"]
    lines = []
    for item in swings[:3]:
        direction = "上升" if item["delta"] > 0 else "下滑"
        lines.append(
            f"- `{item['m']:02d}:00` 前后胜率{direction} {abs(item['delta']) * 100:.0f}pp"
            f"（{_pct(item['p'] - item['delta'])} → {_pct(item['p'])}）"
        )
    return lines


def _team_line(teams: Dict[str, Any], side: int) -> str:
    team = teams.get(str(side)) or teams.get(side) or {}
    parts = [f"{team.get('kills', 0)} 杀"]
    for key in ("dragons", "barons", "heralds", "grubs", "atakhan", "towers", "inhibitors"):
        count = int(team.get(key) or 0)
        if count:
            parts.append(f"{OBJECTIVE_LABELS[key]} {count}")
    return "·".join(parts)


def render_markdown(summary: Dict[str, Any], narrative: Optional[str] = None) -> str:
    game_id = summary.get("gameId", "?")
    winner = summary.get("winner")
    loser = 300 - int(winner) if winner in (100, 200) else None
    duration = float(summary.get("duration") or 0)
    minutes = summary.get("minutes") or []
    teams = summary.get("teams") or {}

    title = f"# {game_id} · {clock(duration)} · {SIDE_LABELS.get(winner, '?')}胜"
    out: List[str] = [title, ""]
    out.append(
        f"> 数据源：{summary.get('source', '?')} ｜ 模型：{summary.get('model', 'live_model3')}"
        f" ｜ 覆盖：10 名玩家 × 每分钟"
    )
    notes = summary.get("notes") or []
    if notes:
        out.append("> " + "；".join(notes))
    out.append("")

    out.append("## 结论")
    if narrative:
        out.append(narrative.strip())
    else:
        verdict = summary.get("verdict") or ""
        out.append(verdict or "（未生成点评：没有配置 LLM key，先看下面的数据拆解。）")
    out.append("")

    elo = summary.get("elo")
    if elo:
        team = elo.get("teamElo") or {}
        weight = float(elo.get("weight") or 0.0)
        tau = float(elo.get("tau") or 0.0)
        out.append("## 赛前实力（Elo）")
        out.append(
            f"- 队伍 Elo：蓝方 {_fmt(team.get('100'))} · 红方 {_fmt(team.get('200'))}"
            f" → 赛前胜率 {float(elo.get('prob') or 0.5) * 100:.0f}%"
        )
        if minutes:
            first = minutes[0]
            out.append(
                f"- 第 {first['m']} 分钟：模型 {_pct(first.get('pModel'))}，"
                f"混合后 {_pct(first['p100'])}（Elo 权重 {weight * 100:.0f}% 起、"
                f"{tau / 60:.0f} 分钟半衰）"
            )
        players = elo.get("players") or {}
        for side in ("100", "200"):
            roster = players.get(side) or []
            if not roster:
                continue
            text = " · ".join(
                f"{p.get('name') or p.get('puuid', '?')[:6]} {float(p.get('elo') or 0):.0f}"
                for p in roster
            )
            out.append(f"- {SIDE_LABELS.get(int(side), side)}：{text}")
        out.append("")

    if minutes:
        probs = [row["p100"] for row in minutes]
        low = min(minutes, key=lambda row: row["p100"])
        high = max(minutes, key=lambda row: row["p100"])
        out.append("## 胜负曲线")
        out.append("```")
        curve_label = "蓝方胜率（模型+Elo）" if elo else "蓝方胜率"
        out.append(f"{curve_label}  0% ┃{sparkline(probs)}┃ 100%")
        out.append(f"              {clock(0):>5} … {clock(duration)}")
        out.append("```")
        out.append(
            f"- 蓝方最低 {_pct(low['p100'])}（`{low['m']:02d}:00`），最高 {_pct(high['p100'])}（`{high['m']:02d}:00`）"
        )
        out.append("")

    out.append("## 关键节点")
    out.extend(_event_lines(summary))
    out.append("")

    out.append("## 全位置对比")
    out.append("")
    out.append("| 位置 | 胜方英雄 | 胜方 KDA | 胜方补刀（分均） | 胜方等级 | 负方英雄 | 负方 KDA | 负方补刀（分均） | 负方等级 |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for entry in sorted(summary.get("roles") or [], key=lambda r: ROLE_ORDER.index(r["role"]) if r["role"] in ROLE_ORDER else 99):
        out.append(_role_row(entry))
    out.append("")
    for entry in summary.get("roles") or []:
        out.append(_role_verdict(entry))
    out.append("")

    out.append("## 分阶段走势")
    out.extend(_phase_lines(summary))
    out.append("")
    out.append("## 胜率拐点")
    out.extend(_swing_lines(summary))
    out.append("")

    if teams:
        out.append("## 团队数据")
        out.append("")
        out.append("| 队伍 | 数据 |")
        out.append("|---|---|")
        out.append(f"| {SIDE_LABELS.get(100)} | {_team_line(teams, 100)} |")
        out.append(f"| {SIDE_LABELS.get(200)} | {_team_line(teams, 200)} |")
        out.append("")

    advice = summary.get("advice") or []
    out.append("## 下次可以做得更好")
    if advice:
        out.extend(f"- {item}" for item in advice)
    else:
        out.append("- （等 LLM 层或规则层补充）")
    out.append("")
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    """极简 Markdown → HTML：标题、列表、表格、粗体、行内代码、引用、代码块。"""
    lines = md.splitlines()
    out: List[str] = []
    in_code = in_table = False
    table_rows: List[str] = []

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if not table_rows:
            return
        out.append("<table>")
        for index, row in enumerate(table_rows):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            tag = "th" if index == 0 else "td"
            out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
        out.append("</table>")
        table_rows = []
        in_table = False

    def inline(text: str) -> str:
        escaped = html.escape(text)
        parts = escaped.split("`")
        merged = []
        for index, part in enumerate(parts):
            merged.append(f"<code>{part}</code>" if index % 2 else part)
        escaped = "".join(merged)
        while "**" in escaped:
            escaped = escaped.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
        return escaped

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                out.append("</pre>")
                in_code = False
            else:
                flush_table()
                out.append("<pre>")
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue
        if stripped.startswith("|"):
            in_table = True
            if not set(stripped.replace("|", "").replace("-", "").replace(":", "").strip()):
                continue
            table_rows.append(stripped)
            continue
        flush_table()
        if not stripped:
            out.append("")
        elif stripped.startswith("# "):
            out.append(f"<h1>{inline(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{inline(stripped[3:])}</h2>")
        elif stripped.startswith("> "):
            out.append(f"<blockquote>{inline(stripped[2:])}</blockquote>")
        elif stripped.startswith("- "):
            out.append(f"<li>{inline(stripped[2:])}</li>")
        else:
            out.append(f"<p>{inline(stripped)}</p>")
    if in_code:
        out.append("</pre>")
    flush_table()
    return "\n".join(out)


def render_html(summary: Dict[str, Any], narrative: Optional[str] = None, title: str = "对局复盘") -> str:
    body = markdown_to_html(render_markdown(summary, narrative))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · {html.escape(str(summary.get('gameId', '')))}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0 auto; max-width: 980px; padding: 28px 20px 80px; background: #0b1220;
  color: #dbe4f3; font: 15px/1.75 "Segoe UI", "Microsoft YaHei", sans-serif; }}
h1 {{ font-size: 22px; color: #f0c987; margin-bottom: 4px; }}
h2 {{ font-size: 17px; margin-top: 30px; border-left: 3px solid #c9a227; padding-left: 10px; }}
blockquote {{ margin: 10px 0; padding: 8px 12px; background: #121c2f; color: #93a4bf;
  border-left: 3px solid #33415c; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px; }}
th, td {{ border: 1px solid #22304a; padding: 6px 8px; text-align: center; }}
th {{ background: #16233a; color: #cbd6e8; }}
code {{ background: #16233a; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }}
pre {{ background: #0f1a2c; border: 1px solid #22304a; padding: 12px; overflow-x: auto;
  line-height: 1.35; font-size: 13px; }}
li {{ margin: 4px 0; }}
</style></head><body>
{body}
</body></html>
"""
