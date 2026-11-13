#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线构建「英雄×分路 胜率-游戏时间曲线」，输出 assets/curves.json。

数据源：
- game_champs.json：2298 局双方阵容（{"100": {role: champ}, "200": {...}, "winner": 100|200}）
- dataset/live_dataset/live_dataset.csv：每行 = 某玩家某分钟的视角（ownWin = 该玩家所在队胜负）

口径（视角对齐）：
- 每一行的 champion = 该行玩家（ownWin→胜/负方 → teamId → game_champs[gameId][teamId][role]）
  所选的英雄；mean(ownWin) 即「选了该英雄的队伍」在该分钟的胜率——英雄在哪一队
  就取哪一队视角，无需再做蓝红翻转。
- 曲线值 win[m-1] = 第 m 分钟的 mean ownWin（等价于「对局至少打到第 m 分钟」的
  样本里的胜率；对局越早结束，后面的分钟就退出样本）。
- 样本门槛：同（英雄,分路）样本 < min-games 局时并入该分路总体曲线
  （heroes 条目只留 {"games": n, "fallback": true}，UI 画 roles[role]）。
- 每分钟全局基线 baseline 与分路曲线 roles 一并输出；两者都是「有曲线英雄
  （games ≥ min-games）的 games 加权平均曲线」——不能直接平均所有行，
  每局双方行数相等、胜/负抵消会恒等于 0.5 平线。

只依赖标准库，运行：
    python -B build_curves.py [--dataset ...] [--champs ...] [--out ...] [--min-games 50]

输出后内置抽样验证（3 个英雄与原始 CSV 手算比对 + 门槛/fallback 不变量）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
DEFAULT_CHAMPS = BASE / "game_champs.json"
DEFAULT_DATASET = BASE.parent / "dataset" / "live_dataset" / "live_dataset.csv"
DEFAULT_OUT = BASE / "assets" / "curves.json"
DEFAULT_MIN_GAMES = 50
MINUTES = 40  # live_dataset 的 minute 上限为 40；数组索引 i 对应第 i+1 分钟
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

sys.path.insert(0, str(BASE))
from live_features import normalize_champ  # noqa: E402


def minute_index(minute: int) -> int:
    """第 minute 分钟 → 数组索引（1..MINUTES，超出并入最后一格）。"""
    return min(MINUTES - 1, max(0, int(minute) - 1))


def load_rosters(path: Path) -> Dict[str, Dict[str, Any]]:
    """game_champs.json → {gameId(str): {"100": {role: norm_champ}, "200": {...}, "winner": int}}。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    rosters: Dict[str, Dict[str, Any]] = {}
    for game_id, entry in raw.items():
        roster: Dict[str, Any] = {"winner": int(entry.get("winner") or 100)}
        for side in ("100", "200"):
            lineup = entry.get(side) or {}
            roster[side] = {
                str(role): normalize_champ(champ)
                for role, champ in lineup.items()
                if role in ROLES and champ
            }
        if roster["100"] and roster["200"]:
            rosters[str(game_id)] = roster
    return rosters


class Accumulator:
    """(key) → 每分钟 win 和与样本数 + 局数集合。"""

    def __init__(self) -> None:
        self.win: Dict[Any, List[float]] = {}
        self.count: Dict[Any, List[int]] = {}
        self.games: Dict[Any, set] = {}

    def add(self, key: Any, minute: int, own_win: int, game_id: str) -> None:
        index = minute_index(minute)
        if key not in self.win:
            self.win[key] = [0.0] * MINUTES
            self.count[key] = [0] * MINUTES
            self.games[key] = set()
        self.win[key][index] += float(own_win)
        self.count[key][index] += 1
        self.games[key].add(game_id)

    def rate(self, key: Any, index: int) -> Optional[float]:
        total = self.count.get(key, [0] * MINUTES)[index]
        if total == 0:
            return None
        return self.win[key][index] / total

    def rates(self, key: Any) -> List[Optional[float]]:
        return [self.rate(key, index) for index in range(MINUTES)]

    def games_for(self, key: Any) -> int:
        return len(self.games.get(key, set()))


def build(
    champs_path: Path,
    dataset_path: Path,
    out_path: Path,
    min_games: int,
) -> Dict[str, Any]:
    rosters = load_rosters(champs_path)
    heroes: Dict[Tuple[str, str], Accumulator] = {}
    skipped = Counter()

    rows = 0
    with dataset_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            game_id = str(row.get("gameId") or "")
            roster = rosters.get(game_id)
            minute = int(row.get("minute") or 0)
            role = str(row.get("role") or "")
            own_win = int(float(row.get("ownWin") or 0))
            if rows % 100000 == 0:
                print(f"  rows={rows} pairs={len(heroes)}", flush=True)
            if not roster or role not in ROLES or minute < 1:
                skipped["row"] += 1
                continue

            winner = int(roster["winner"])
            own_team = winner if own_win == 1 else 300 - winner
            champion = roster.get(str(own_team), {}).get(role)
            if not champion:
                skipped["no_champ"] += 1
                continue
            key = (champion, role)
            heroes.setdefault(key, Accumulator()).add("all", minute, own_win, game_id)

    # ---- 组装曲线 ----
    # 基线/分路曲线：有曲线英雄（games ≥ min_games）的 games 加权平均。
    # 不能直接把所有行平均——每局双方行数相等、胜/负抵消，恒等于 0.5 平线。
    role_weighted: Dict[str, List[float]] = {role: [0.0] * MINUTES for role in ROLES}
    role_weight_sum: Dict[str, float] = {role: 0.0 for role in ROLES}
    all_weighted = [0.0] * MINUTES
    all_weight_sum = 0.0

    def weighted_map(weighted: List[float], weight_sum: float) -> Dict[str, float]:
        return {
            str(index + 1): round(weighted[index] / weight_sum, 4)
            for index in range(MINUTES)
        }

    hero_entries: Dict[str, Dict[str, Any]] = {}
    pairs_with_curve = 0
    pairs_fallback = 0
    for (champion, role), acc in sorted(heroes.items()):
        games = acc.games_for("all")
        entry = hero_entries.setdefault(champion, {})
        if games < min_games:
            entry[role] = {"games": games, "fallback": True}
            pairs_fallback += 1
        else:
            rates = [round(v, 4) if v is not None else None for v in acc.rates("all")]
            # 前段必然有样本（对局至少打到第 1 分钟），中间的 None 用左右邻补平
            for index in range(MINUTES):
                if rates[index] is not None:
                    continue
                left = next((rates[i] for i in range(index, -1, -1) if rates[i] is not None), None)
                right = next((rates[i] for i in range(index + 1, MINUTES) if rates[i] is not None), None)
                if left is not None and right is not None:
                    rates[index] = round((left + right) / 2.0, 4)
                else:
                    rates[index] = left if left is not None else (right if right is not None else 0.5)
            entry[role] = {"games": games, "win": rates}
            pairs_with_curve += 1

    # 有曲线英雄的 games 加权平均（英雄曲线本身保持不变）
    for champ_roles in hero_entries.values():
        for role, entry in champ_roles.items():
            if entry.get("fallback"):
                continue
            weight = float(entry["games"])
            win = entry["win"]
            all_weight_sum += weight
            role_weight_sum[role] += weight
            for index in range(MINUTES):
                value = float(win[index])
                all_weighted[index] += weight * value
                role_weighted[role][index] += weight * value

    payload: Dict[str, Any] = {
        "patch": "16.22",
        "meta": {
            "source": "game_champs.json + live_dataset.csv",
            "games": len(rosters),
            "rows": rows,
            "minutes": MINUTES,
            "minuteIndex": "数组索引 i 对应第 i+1 分钟（1..40）",
            "minGames": min_games,
            "heroPairs": pairs_with_curve + pairs_fallback,
            "heroesWithCurve": pairs_with_curve,
            "heroesFallback": pairs_fallback,
            "fallbackUsesRoleCurve": True,
            "baselineDef": "有曲线英雄（games>=minGames）的 games 加权平均曲线",
            "rolesDef": "该分路有曲线英雄的 games 加权平均曲线（fallback 英雄画此曲线）",
            "skippedRows": dict(skipped),
            "createdAt": datetime.now().astimezone().isoformat(),
        },
        "baseline": weighted_map(all_weighted, all_weight_sum),
        "roles": {
            role: weighted_map(role_weighted[role], role_weight_sum[role])
            for role in ROLES
        },
        "heroes": hero_entries,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"输出: {out_path}（{out_path.stat().st_size / 1024:.0f} KB）")
    return payload


def verify(payload: Dict[str, Any], champs_path: Path, dataset_path: Path, min_games: int) -> int:
    """抽样验证：3 个英雄与原始 CSV 手算比对 + 门槛/fallback 不变量。"""
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  PASS {name}")
        else:
            failed += 1
            print(f"  FAIL {name}  {detail}")

    print("[验证] 门槛与 fallback 不变量")
    violations: List[str] = []
    fallback_entries = 0
    curve_entries = 0
    for champ, entries in payload["heroes"].items():
        for role, entry in entries.items():
            if entry.get("fallback"):
                fallback_entries += 1
                if entry["games"] >= min_games:
                    violations.append(f"{champ}/{role} fallback 但 games={entry['games']}>={min_games}")
                if "win" in entry:
                    violations.append(f"{champ}/{role} fallback 却带 win")
            else:
                curve_entries += 1
                if entry["games"] < min_games:
                    violations.append(f"{champ}/{role} 有曲线但 games={entry['games']}<{min_games}")
                if len(entry["win"]) != MINUTES:
                    violations.append(f"{champ}/{role} win 长度 {len(entry['win'])} != {MINUTES}")
    check("fallback 数量与 meta 一致", fallback_entries == payload["meta"]["heroesFallback"], f"{fallback_entries} vs {payload['meta']['heroesFallback']}")
    check("有曲线数量与 meta 一致", curve_entries == payload["meta"]["heroesWithCurve"], f"{curve_entries} vs {payload['meta']['heroesWithCurve']}")
    check("fallback/门槛不变量无违规", not violations, "; ".join(violations[:5]))

    print("[验证] 抽样 3 个英雄与原始数据手算比对")
    sampled = sorted(
        (
            (champ, role, entry)
            for champ, entries in payload["heroes"].items()
            for role, entry in entries.items()
            if not entry.get("fallback")
        ),
        key=lambda item: -item[2]["games"],
    )[:3]
    rosters = load_rosters(champs_path)
    for champ, role, entry in sampled:
        win = [0.0] * MINUTES
        count = [0] * MINUTES
        games = set()
        with dataset_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                game_id = str(row.get("gameId") or "")
                roster = rosters.get(game_id)
                if not roster or str(row.get("role") or "") != role:
                    continue
                minute = int(row.get("minute") or 0)
                own_win = int(float(row.get("ownWin") or 0))
                winner = int(roster["winner"])
                own_team = winner if own_win == 1 else 300 - winner
                if roster.get(str(own_team), {}).get(role) != champ:
                    continue
                index = minute_index(minute)
                win[index] += float(own_win)
                count[index] += 1
                games.add(game_id)
        check(f"{champ}/{role} 局数一致 ({entry['games']})", len(games) == entry["games"], f"{len(games)} vs {entry['games']}")
        max_gap = 0.0
        for index in range(MINUTES):
            if count[index] == 0:
                continue
            want = win[index] / count[index]
            got = entry["win"][index]
            max_gap = max(max_gap, abs(want - got))
        check(f"{champ}/{role} 每分钟胜率一致 (max|Δ|={max_gap:.5f})", max_gap <= 1e-3, str(max_gap))

    print("[验证] 基线/分路曲线：非平线 + 加权均值手算一致")
    for name, curve in [("baseline", payload["baseline"])] + [
        (role, payload["roles"][role]) for role in ROLES
    ]:
        values = [float(curve[str(index + 1)]) for index in range(MINUTES)]
        spread = max(values) - min(values)
        check(
            f"{name} 非平线（min={min(values):.4f} max={max(values):.4f}）",
            spread > 1e-4,
            str((min(values), max(values))),
        )
    # 手算一致：从 payload['heroes'] 独立重组 games 加权均值并与输出逐分钟对比
    recomputed = {role: [0.0] * MINUTES for role in ROLES}
    recomputed_sum = {role: 0.0 for role in ROLES}
    recomputed_all = [0.0] * MINUTES
    recomputed_all_sum = 0.0
    for champ_roles in payload["heroes"].values():
        for role, entry in champ_roles.items():
            if entry.get("fallback"):
                continue
            weight = float(entry["games"])
            recomputed_all_sum += weight
            recomputed_sum[role] += weight
            for index in range(MINUTES):
                value = float(entry["win"][index])
                recomputed_all[index] += weight * value
                recomputed[role][index] += weight * value
    gap = 0.0
    for index in range(MINUTES):
        gap = max(
            gap,
            abs(recomputed_all[index] / recomputed_all_sum - float(payload["baseline"][str(index + 1)])),
        )
        for role in ROLES:
            gap = max(
                gap,
                abs(recomputed[role][index] / recomputed_sum[role] - float(payload["roles"][role][str(index + 1)])),
            )
    check("加权均值重组与输出逐分钟一致", gap <= 1e-3, f"max|Δ|={gap:.5f}")

    print("[验证] JSON 可加载 / 长度")
    check("baseline 覆盖 1..40 分钟", len(payload["baseline"]) == MINUTES, str(len(payload["baseline"])))
    check("roles 含 5 个分路", set(payload["roles"]) == set(ROLES), str(list(payload["roles"])))
    print(f"  基线 第1分钟={payload['baseline']['1']} 第20分钟={payload['baseline']['20']} 第40分钟={payload['baseline']['40']}")
    for role in ROLES:
        print(f"  {role} 第1分钟={payload['roles'][role]['1']} 第20分钟={payload['roles'][role]['20']} 第40分钟={payload['roles'][role]['40']}")
    print(f"\n验证结果: PASS={passed} FAIL={failed}")
    return 1 if failed else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="构建英雄×分路 胜率-时间曲线")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--champs", type=Path, default=DEFAULT_CHAMPS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-games", type=int, default=DEFAULT_MIN_GAMES, help="样本门槛（局）")
    args = parser.parse_args(argv)

    print(f"阵容: {args.champs}")
    print(f"数据集: {args.dataset}")
    payload = build(args.champs, args.dataset, args.out, args.min_games)
    print(
        f"英雄×分路: {payload['meta']['heroPairs']} 组，"
        f"有曲线 {payload['meta']['heroesWithCurve']}，"
        f"fallback {payload['meta']['heroesFallback']}"
    )
    return verify(payload, args.champs, args.dataset, args.min_games)


if __name__ == "__main__":
    raise SystemExit(main())
