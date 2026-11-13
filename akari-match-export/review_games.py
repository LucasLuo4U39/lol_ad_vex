#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史对局回填：从 live_dataset_v3.csv + game_champs.json 生成「全位置复盘」。

一条命令产出 N 局的复盘报告：

    python review_games.py --games 901121900494,901118041203 --out ../analysis/game-reviews

每局输出 `<out>/<gameId>/summary.json`、`report.md`、`report.html`，并在根目录生成
`index.md` / `index.html`。可选 `--llm` 调用 OpenAI 兼容接口写点评（需要 key），
没有 key 时用规则判词兜底 —— 数据部分（表格/曲线/事件）两种模式完全一致。

数据口径
--------
- 来自 Live Client Data 的「玩家视角」数据集：每局每分钟 10 行（5 路 × 双方），
  每行的 own* 是该玩家自己能看到的数值；
- 因此**每人金币 = 该玩家兜里现金**（currentGold），不是团队总金币，也不含已出装；
- 目标（龙/先锋/塔…）由 `*Diff` 逐分钟增量还原，同一分钟内双方各拿一条会抵消，属已知误差。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from lgbmini import LgbTextModel
from live_features import ROLE_CODES
from match_review import ROLE_LABELS, ROLE_ORDER, SIDE_LABELS, clock, render_html, render_markdown
from player_elo import blend_win_prob, elo_probability, load_elo_map, team_elos

BASE = Path(__file__).resolve().parent
DATASET = BASE.parent / "dataset" / "live_dataset" / "live_dataset_v3.csv"
CHAMPS = BASE / "game_champs.json"
MODEL_DIR = BASE.parent / "dataset" / "live_dataset" / "live_model3"
OUT_DIR = BASE.parent / "analysis" / "game-reviews"

ID_COLUMNS = [
    "gameId", "minute", "role", "ownWin", "ownLevel", "ownGold", "ownCS",
    "ownKills", "ownDeaths", "ownAssists", "teamKills", "teamDeaths", "teamAssists",
    "teamCS", "teamLevelSum", "dragonDiff", "baronDiff", "heraldDiff", "grubDiff",
    "atakhanDiff", "towerDiff", "inhibitorDiff",
]
OBJECTIVE_KEYS = [
    ("dragons", "dragonDiff", "小龙"),
    ("barons", "baronDiff", "大龙"),
    ("heralds", "heraldDiff", "峡谷先锋"),
    ("grubs", "grubDiff", "虚空巢虫"),
    ("atakhan", "atakhanDiff", "厄塔汗"),
    ("towers", "towerDiff", "防御塔"),
    ("inhibitors", "inhibitorDiff", "水晶"),
]


# --------------------------------------------------------------------------
# 读数据
# --------------------------------------------------------------------------
def load_dataset(games: List[str], features: List[str]) -> pd.DataFrame:
    columns = ID_COLUMNS + [c for c in features if c not in ID_COLUMNS]
    frame = pd.read_csv(DATASET, usecols=columns, dtype={"role": str})
    frame = frame[frame["gameId"].astype(str).isin(set(games))].copy()
    if frame.empty:
        raise SystemExit(f"数据集中没有这些对局: {games}")
    frame["side"] = np.where(
        frame["ownWin"] == 1,
        frame["gameId"].map(lambda gid: _winner_of(gid)),
        frame["gameId"].map(lambda gid: 300 - _winner_of(gid)),
    )
    return frame


_CHAMPS: Optional[Dict[str, Any]] = None


def champs() -> Dict[str, Any]:
    global _CHAMPS
    if _CHAMPS is None:
        _CHAMPS = json.loads(CHAMPS.read_text(encoding="utf-8"))
    return _CHAMPS


def _winner_of(game_id: Any) -> int:
    entry = champs().get(str(game_id), {})
    return int(entry.get("winner") or 100)


def _champion_of(game_id: Any, side: int, role: str) -> str:
    entry = champs().get(str(game_id), {})
    return str((entry.get(str(side)) or {}).get(role) or "?")


# --------------------------------------------------------------------------
# 摘要
# --------------------------------------------------------------------------
def _side_rows(frame: pd.DataFrame, side: int) -> pd.DataFrame:
    return frame[frame["side"] == side]


def _per_minute(frame: pd.DataFrame, side: int) -> Dict[int, Dict[str, Any]]:
    rows = _side_rows(frame, side).sort_values("minute")
    per_minute: Dict[int, Dict[str, Any]] = {}
    for minute, group in rows.groupby("minute"):
        per_minute[int(minute)] = {
            "kills": int(group["teamKills"].max()),
            "deaths": int(group["teamDeaths"].max()),
            "assists": int(group["teamAssists"].max()),
            "cs": int(group["teamCS"].max()),
            "levelSum": int(group["teamLevelSum"].max()),
        }
    return per_minute


def _objective_events(frame: pd.DataFrame, side: int) -> List[Dict[str, Any]]:
    """由 diff 的逐分钟增量还原目标事件（side 视角：+1 是自己拿的）。"""
    rows = _side_rows(frame, side).sort_values("minute")
    diff_by_minute: Dict[int, Dict[str, int]] = {}
    for minute, row in rows.drop_duplicates("minute").set_index("minute").iterrows():
        diff_by_minute[int(minute)] = {
            key: int(row.get(diff_key) or 0) for key, diff_key, _ in OBJECTIVE_KEYS
        }
    events: List[Dict[str, Any]] = []
    previous = {key: 0 for key, _, _ in OBJECTIVE_KEYS}
    for minute in sorted(diff_by_minute):
        current = diff_by_minute[minute]
        for key, _, label in OBJECTIVE_KEYS:
            delta = current[key] - previous[key]
            if delta:
                taker = side if delta > 0 else 300 - side
                events.append(
                    {
                        "t": minute * 60,
                        "side": taker,
                        "kind": "objective",
                        "text": f"{SIDE_LABELS[taker]}拿下{label}"
                        + (f" ×{abs(delta)}" if abs(delta) > 1 else ""),
                    }
                )
        previous = current
    return events


def _kill_events(frame: pd.DataFrame, side: int = 100) -> List[Dict[str, Any]]:
    rows = _side_rows(frame, 100).sort_values("minute").drop_duplicates("minute")
    kills_100 = {int(m): int(k) for m, k in zip(rows["minute"], rows["teamKills"])}
    enemy_rows = _side_rows(frame, 200).sort_values("minute").drop_duplicates("minute")
    kills_200 = {int(m): int(k) for m, k in zip(enemy_rows["minute"], enemy_rows["teamKills"])}
    events: List[Dict[str, Any]] = []
    prev = {100: 0, 200: 0}
    first_blood_done = False
    for minute in sorted(kills_100):
        counts = {100: kills_100.get(minute, 0), 200: kills_200.get(minute, 0)}
        for team in (100, 200):
            delta = counts[team] - prev[team]
            if delta <= 0:
                continue
            if not first_blood_done:
                events.append(
                    {"t": minute * 60, "side": team, "kind": "kill",
                     "text": f"{SIDE_LABELS[team]}拿到一血"}
                )
                first_blood_done = True
            elif delta >= 2:
                events.append(
                    {"t": minute * 60, "side": team, "kind": "kill",
                     "text": f"{SIDE_LABELS[team]}一分钟内击杀 {delta} 人"}
                )
        prev = counts
    return events


def _player_series(frame: pd.DataFrame, role: str, side: int) -> Dict[int, Dict[str, Any]]:
    rows = frame[(frame["role"] == role) & (frame["side"] == side)].sort_values("minute")
    series = {}
    for _, row in rows.iterrows():
        series[int(row["minute"])] = {
            "level": int(row["ownLevel"]),
            "cs": int(row["ownCS"]),
            "gold": int(row["ownGold"]),
            "kda": [int(row["ownKills"]), int(row["ownDeaths"]), int(row["ownAssists"])],
        }
    return series


def build_summary(
    frame: pd.DataFrame,
    game_id: str,
    probabilities: np.ndarray,
    model_name: str,
    elo: Optional[Dict[str, Any]] = None,
    elo_weight: float = 0.5,
    elo_tau: float = 900.0,
) -> Dict[str, Any]:
    frame = frame[frame["gameId"].astype(str) == str(game_id)].copy()
    frame["prob"] = probabilities
    winner = _winner_of(game_id)
    loser = 300 - winner
    last_minute = int(frame["minute"].max())
    duration = last_minute * 60

    per_minute = {side: _per_minute(frame, side) for side in (100, 200)}
    prob100 = (
        frame[frame["side"] == 100].groupby("minute")["prob"].mean().sort_index()
    )
    elo_block: Optional[Dict[str, Any]] = None
    p_elo = 0.5
    if elo:
        blue_elo, red_elo = team_elos(elo)
        p_elo = elo_probability(blue_elo - red_elo)
        elo_block = {
            "teamElo": {"100": round(blue_elo, 1), "200": round(red_elo, 1)},
            "prob": round(p_elo, 4),
            "weight": elo_weight,
            "tau": elo_tau,
            "source": elo.get("source", "elo"),
            "players": elo.get("players"),
        }

    minutes = []
    previous_kills = {100: 0, 200: 0}
    for minute in sorted(per_minute[100]):
        kills = [per_minute[100][minute]["kills"] - previous_kills[100],
                 per_minute[200][minute]["kills"] - previous_kills[200]]
        previous_kills = {
            100: per_minute[100][minute]["kills"],
            200: per_minute[200][minute]["kills"],
        }
        p_model = float(prob100.get(minute, 0.5))
        p_final = (
            blend_win_prob(p_model, p_elo, minute, elo_weight, elo_tau)
            if elo_block
            else p_model
        )
        minutes.append(
            {
                "m": minute,
                "pModel": round(p_model, 6),
                "p100": round(p_final, 6),
                "kills": [max(0, kills[0]), max(0, kills[1])],
                "cs": [per_minute[100][minute]["cs"], per_minute[200][minute]["cs"]],
                "level": [per_minute[100][minute]["levelSum"], per_minute[200][minute]["levelSum"]],
            }
        )

    teams: Dict[str, Dict[str, int]] = {}
    for side in (100, 200):
        team = {
            "kills": per_minute[side][last_minute]["kills"],
            "deaths": per_minute[side][last_minute]["deaths"],
            "assists": per_minute[side][last_minute]["assists"],
            "cs": per_minute[side][last_minute]["cs"],
            "levelSum": per_minute[side][last_minute]["levelSum"],
        }
        for key, _, _ in OBJECTIVE_KEYS:
            team[key] = sum(max(0, delta) for delta in _diff_deltas(frame, side, key))
        teams[str(side)] = team

    roles = []
    for role in ROLE_ORDER:
        series_winner = _player_series(frame, role, winner)
        series_loser = _player_series(frame, role, loser)
        if not series_winner or not series_loser:
            continue
        final_w, final_l = series_winner[max(series_winner)], series_loser[max(series_loser)]
        at = {}
        for mark in (10, 15, 20):
            if mark in series_winner and mark in series_loser:
                at[str(mark)] = {
                    "w": series_winner[mark],
                    "l": series_loser[mark],
                }
        roles.append(
            {
                "role": role,
                "side": winner,
                "champion": _champion_of(game_id, winner, role),
                "kda": final_w["kda"],
                "cs": final_w["cs"],
                "csPerMin": round(final_w["cs"] / max(1, last_minute), 2),
                "gold": final_w["gold"],
                "level": final_w["level"],
                "at": at,
                "opp": {
                    "champion": _champion_of(game_id, loser, role),
                    "kda": final_l["kda"],
                    "cs": final_l["cs"],
                    "csPerMin": round(final_l["cs"] / max(1, last_minute), 2),
                    "gold": final_l["gold"],
                    "level": final_l["level"],
                },
            }
        )

    events = _objective_events(frame, 100) + _kill_events(frame)
    events.sort(key=lambda item: (item["t"], item["kind"] != "kill"))

    swings = []
    for index in range(1, len(minutes)):
        delta = minutes[index]["p100"] - minutes[index - 1]["p100"]
        if abs(delta) >= 0.08:
            swings.append({"m": minutes[index]["m"], "delta": round(delta, 4), "p": minutes[index]["p100"]})
    swings.sort(key=lambda item: -abs(item["delta"]))

    summary: Dict[str, Any] = {
        "gameId": str(game_id),
        "source": "SGP 1 分钟帧（10 名玩家视角回填）",
        "model": model_name,
        "duration": duration,
        "winner": winner,
        "teams": teams,
        "roles": roles,
        "minutes": minutes,
        "events": events,
        "swings": swings[:5],
        "elo": elo_block,
        "notes": [
            "金币是各玩家兜里的现金（currentGold），不是团队总经济",
            "目标由差值增量还原：同一分钟双方各拿一条会互相抵消，最后一分钟的建筑可能未计入",
            "短于 28 分钟且人头差距很大的对局通常是投降结束，塔数不代表推塔能力",
        ],
        "advice": _rule_advice(teams, roles, swings, winner, last_minute),
    }
    return summary


def _diff_deltas(frame: pd.DataFrame, side: int, key: str) -> List[int]:
    diff_key = dict((k, d) for k, d, _ in OBJECTIVE_KEYS)[key]
    rows = _side_rows(frame, side).sort_values("minute").drop_duplicates("minute")
    values = [int(v or 0) for v in rows[diff_key]]
    deltas = [values[0]] + [values[i] - values[i - 1] for i in range(1, len(values))]
    return deltas


def _rule_advice(
    teams: Dict[str, Dict[str, int]],
    roles: List[Dict[str, Any]],
    swings: List[Dict[str, Any]],
    winner: int,
    last_minute: int,
) -> List[str]:
    """给「负方」的三条改进建议（数据可复核，不编故事）。"""
    loser = str(300 - winner)
    win = str(winner)
    tips: List[str] = []

    worst = max(
        roles,
        key=lambda role: (role["cs"] or 0) - (role["opp"]["cs"] or 0),
        default=None,
    )
    if worst is not None and (worst["cs"] or 0) - (worst["opp"]["cs"] or 0) > 15:
        gap = worst["cs"] - worst["opp"]["cs"]
        label = ROLE_LABELS.get(worst["role"], worst["role"])
        tips.append(
            f"负方{label}补刀落后 {gap}（{worst['opp']['champion']} 对 {worst['champion']}），"
            "下一局优先用回城节奏和线权换补刀，而不是硬拼。"
        )

    dragon_loser = int(teams[loser].get("dragons", 0))
    dragon_win = int(teams[win].get("dragons", 0))
    if dragon_win - dragon_loser >= 2:
        tips.append(
            f"小龙 {dragon_loser}:{dragon_win} 落后，敌方会在 5/10/15 分钟第一波回城后卡龙，"
            "提前 40 秒做河道视野，别等龙刷了才集合。"
        )

    if swings:
        biggest = max(swings, key=lambda item: abs(item["delta"]))
        tips.append(
            f"最大单分钟波动在 `{biggest['m']:02d}:00`（{biggest['delta'] * 100:+.0f}pp），"
            "那个时间点前后是最该避免接团、也最该抢视野的窗口。"
        )

    deaths_loser = int(teams[loser].get("deaths", 0))
    if deaths_loser >= 25:
        tips.append(
            f"负方全场阵亡 {deaths_loser} 次，超过 25 次基本等于把节奏送出去；"
            "下一局只打有视野的团，不要为一个人头换一条线。"
        )
    return tips[:4]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="从历史数据集生成全位置复盘")
    parser.add_argument("--games", default="", help="逗号分隔的 gameId；留空则用 --select")
    parser.add_argument("--select", type=Path, default=None, help="gameId 列表文件（每行一个）")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    parser.add_argument(
        "--elo",
        type=Path,
        default=None,
        help="player_elo.py 产出的 Elo JSON（按 gameId 索引或单局）",
    )
    parser.add_argument("--elo-weight", type=float, default=0.5, help="开局 Elo 权重（logit 混合）")
    parser.add_argument("--elo-tau", type=float, default=900.0, help="Elo 权重衰减时间常数（对局秒）")
    parser.add_argument(
        "--elo-fetch",
        action="store_true",
        help="复盘前现场抓每个玩家最近 N 场算 Elo（需要客户端登录）",
    )
    parser.add_argument("--elo-count", type=int, default=25, help="每人抓多少场历史")
    parser.add_argument(
        "--elo-cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "elo_cache",
    )
    args = parser.parse_args(argv)

    elo_map: Dict[str, Any] = {}
    if args.elo:
        elo_map = load_elo_map(args.elo)
        print(f"已加载 Elo: {args.elo}（{len(elo_map)} 局）")

    sgp_client = None
    if args.elo_fetch:
        from advisor_server import discover_lcu
        from player_elo import SgpClient

        found = discover_lcu()
        if not found:
            print("Elo 抓取跳过：找不到 LCU lockfile（客户端没开？），只用模型胜率")
            args.elo_fetch = False
        else:
            sgp_client = SgpClient(found[0], found[1])
            print(f"Elo 抓取已启用：每人最近 {args.elo_count} 场（LCU 端口 {found[0]}）")

    games: List[str] = []
    if args.games:
        games = [item.strip() for item in args.games.split(",") if item.strip()]
    elif args.select:
        games = [line.strip() for line in args.select.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not games:
        raise SystemExit("需要 --games 或 --select")

    model = LgbTextModel(args.model_dir / "model.txt")
    features = list(model.feature_names)
    started = time.time()
    frame = load_dataset(games, features)
    print(f"读取 {len(games)} 局 / {len(frame)} 行（{time.time() - started:.1f}s）")

    matrix = frame[features].copy()
    matrix["role"] = matrix["role"].map(lambda value: float(ROLE_CODES.get(str(value), 0.0)))
    probabilities = model.predict_proba(matrix.to_numpy(dtype=np.float64))
    print(f"模型推理完成（{time.time() - started:.1f}s）")

    out_root: Path = args.out
    out_root.mkdir(parents=True, exist_ok=True)
    index_rows = []
    model_name = f"{args.model_dir.name}（{model.num_trees} 棵树）"
    for game_id in games:
        mask = (frame["gameId"].astype(str) == game_id).to_numpy()
        elo_entry = elo_map.get(game_id)
        if elo_entry is None and sgp_client is not None:
            try:
                from player_elo import build_elo_for_game

                elo_entry = build_elo_for_game(
                    game_id,
                    sgp_client,
                    count=args.elo_count,
                    cache_dir=args.elo_cache_dir,
                )
                print(
                    f"  Elo: 蓝 {elo_entry['teamElo']['100']:.0f} / "
                    f"红 {elo_entry['teamElo']['200']:.0f}"
                    f"（{elo_entry['matches']} 局、{elo_entry['playersRated']} 人）"
                )
            except Exception as error:  # noqa: BLE001
                print(f"  Elo 抓取失败（{game_id}）: {error}")
        summary = build_summary(
            frame[mask],
            game_id,
            probabilities[mask],
            model_name,
            elo=elo_entry,
            elo_weight=args.elo_weight,
            elo_tau=args.elo_tau,
        )
        session = out_root / game_id
        session.mkdir(parents=True, exist_ok=True)
        (session / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        narrative_path = session / "narrative.md"
        narrative = narrative_path.read_text(encoding="utf-8") if narrative_path.is_file() else None
        (session / "report.md").write_text(render_markdown(summary, narrative), encoding="utf-8")
        (session / "report.html").write_text(render_html(summary, narrative), encoding="utf-8")
        winner = summary["winner"]
        probs = [row["p100"] for row in summary["minutes"]]
        winner_min = min(probs) if winner == 100 else min(1.0 - p for p in probs)
        index_rows.append(
            {
                "gameId": game_id,
                "duration": summary["duration"],
                "winner": winner,
                "minProb": winner_min,
                "narrative": bool(narrative),
            }
        )
        print(f"  ✓ {game_id} {clock(summary['duration'])} {SIDE_LABELS[winner]}胜")

    _write_index(out_root, index_rows)
    print(f"\n输出目录: {out_root}")
    return 0


def _write_index(out_root: Path, rows: List[Dict[str, Any]]) -> None:
    lines = ["# 对局复盘索引", "", f"共 {len(rows)} 局 · 数据源：live_dataset_v3（1 分钟 × 10 人视角）", ""]
    lines.append("| 对局 | 时长 | 胜方 | 胜方最低胜率 | 报告 |")
    lines.append("|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row['gameId']} | {clock(row['duration'])} | {SIDE_LABELS[row['winner']]} "
            f"| {row['minProb'] * 100:.0f}% | [markdown]({row['gameId']}/report.md) · "
            f"[html]({row['gameId']}/report.html) |"
        )
    (out_root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if (out_root / "OVERVIEW.md").is_file():
        (out_root / "index.md").write_text(
            (out_root / "index.md").read_text(encoding="utf-8")
            + "\n跨局总结见 [OVERVIEW.md](OVERVIEW.md)。\n",
            encoding="utf-8",
        )

    html_rows = "\n".join(
        f"<tr><td>{r['gameId']}</td><td>{clock(r['duration'])}</td>"
        f"<td>{SIDE_LABELS[r['winner']]}</td><td>{r['minProb'] * 100:.0f}%</td>"
        f"<td><a href='{r['gameId']}/report.html'>查看</a></td></tr>"
        for r in rows
    )
    template = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>对局复盘索引</title><style>
body{margin:40px auto;max-width:900px;background:#0b1220;color:#dbe4f3;
font:15px "Segoe UI","Microsoft YaHei",sans-serif}
table{border-collapse:collapse;width:100%}th,td{border:1px solid #22304a;padding:8px 10px;text-align:center}
th{background:#16233a}a{color:#f0c987}
</style></head><body>
<h1>对局复盘索引</h1><p>共 __COUNT__ 局 · 数据源：live_dataset_v3（1 分钟 × 10 人视角）</p>
<table><tr><th>对局</th><th>时长</th><th>胜方</th><th>胜方最低胜率</th><th>报告</th></tr>
__ROWS__
</table></body></html>
"""
    (out_root / "index.html").write_text(
        template.replace("__COUNT__", str(len(rows))).replace("__ROWS__", html_rows),
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
