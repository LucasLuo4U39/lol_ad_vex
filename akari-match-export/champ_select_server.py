#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选人助手（网页版）：选人阶段实时预估阵容胜率 + 按位置推荐英雄。

数据来源
--------
- LCU `/lol-champ-select/v1/session`：双方阵容、我的位置、倒计时、ban；
- `assets/draft_model.json`：用 2,298 局训练的「纯 bp」逻辑回归（OOF AUC 0.69）；
- `opgg_stats.json`：版本胜率 / 对位胜率；
- `rank_lookup.py`：双方段位分（段位可见时）；
- 可选：个人最近战绩（SGP，用于推荐理由里的「你最近 x 胜 y 负」）。

接口
----
    GET /                页面
    GET /api/state        当前选人状态 + 推荐 + 胜率历史（前端 1s 轮询）
    GET /icon/<champId>  英雄头像（代理 LCU 资源，带缓存）

用法
----
    python champ_select_server.py            # 连 LCU，自动识别选人阶段
    python champ_select_server.py --demo     # 假数据演示（不开客户端也能看布局）
    python champ_select_server.py --port 8788
"""

from __future__ import annotations

import argparse
import base64
import http.client
import http.server
import json
import math
import queue
import re
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rank_lookup import RankResolver, discover_lcu_psutil
from attribution import FEATURE_CN_DRAFT, attribute_linear_top

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if getattr(sys, "frozen", False):
    # 打包进 exe 时：所有数据文件都在 sys._MEIPASS/assets/ 下
    # （prepare_assets.py 把 opgg_stats.json 也复制到这里），
    # 不能用 __file__ 推断路径（PyInstaller 里 __file__ 指向 _internal/）。
    BASE = Path(sys._MEIPASS)
    ASSETS = BASE / "assets"
    OPGG = ASSETS / "opgg_stats.json"
    DRAFT_MODEL = ASSETS / "draft_model.json"
    UI = BASE / "ui"
else:
    BASE = Path(__file__).resolve().parent
    ASSETS = BASE / "assets" if (BASE / "assets").is_dir() else BASE.parent / "assets"
    OPGG = BASE / "opgg_stats.json"
    DRAFT_MODEL = ASSETS / "draft_model.json"
    UI = BASE / "ui"
CURVES = ASSETS / "curves.json"
# 新界面（v2.3）：serve ui/next/champ_select.html；老页面保留作回退
NEW_HTML = UI / "next" / "champ_select.html"
HTML = NEW_HTML if NEW_HTML.is_file() else ASSETS / "champ_select.html"
DESIGN_CSS = UI / "design-system.css"
DEMO_JS = UI / "next" / "demo-data.js"
DEBUG_SESSION = Path(__file__).resolve().parent / "analysis" / "_draft" / "last_session.json"

DEFAULT_RANK = 4000.0
ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_CN = {"TOP": "上单", "JUNGLE": "打野", "MIDDLE": "中单", "BOTTOM": "下路", "UTILITY": "辅助"}
LCU_ROLE = {
    "top": "TOP",
    "jungle": "JUNGLE",
    "middle": "MIDDLE",
    "bottom": "BOTTOM",
    "utility": "UTILITY",
    "": "",
}
POS = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


class DraftModel:
    def __init__(self, path: Path = DRAFT_MODEL, opgg_path: Path = OPGG) -> None:
        payload = _load(path)
        model = payload["model"]
        self.features: List[str] = model["features"]
        self.mean = model["mean"]
        self.scale = model["scale"]
        self.coef = model["coef"]
        self.intercept = model["intercept"]
        self.auc = model.get("auc")
        # T58：线性归因用的模型元信息（features/mean/scale/coef/intercept 全块）
        self.model_meta = model
        self.champ_stats: Dict[str, Dict[str, dict]] = payload.get("champStats") or {}
        self.champ_names: Dict[str, str] = payload.get("champNames") or {}
        self.id_by_key: Dict[str, int] = {}
        self.opgg = _load(opgg_path) if opgg_path.is_file() else {"stats": {}, "matchups": {}}
        # T23/T24：胜率-时间曲线（curves.json 缺失时各项返回 null）
        try:
            self.curves = _load(CURVES) if CURVES.is_file() else None
        except Exception:  # noqa: BLE001
            self.curves = None

    # ------------------------------------------------------------ features
    def op_wr(self, role: str, champ: str) -> Optional[float]:
        entry = (self.opgg.get("stats", {}).get(POS[role]) or {}).get(normalize(champ))
        return float(entry["winRate"]) if entry else None

    def matchup_wr(self, role: str, own: str, enemy: str) -> Optional[float]:
        entry = ((self.opgg.get("matchups", {}).get(POS[role]) or {}).get(normalize(own)) or {}).get(
            normalize(enemy)
        )
        return float(entry["winRate"]) if entry else None

    def cohort(self, role: str, champ: str) -> Optional[dict]:
        return (self.champ_stats.get(role) or {}).get(normalize(champ))

    # ------------------------------------------------------------ 线性归因（T58）
    def draft_attribution(
        self, lineup: Dict[str, str], enemy: Dict[str, str], ranks: Dict[str, float]
    ) -> Optional[List[dict]]:
        """选人模型线性归因 top3：[{key, nameCn, delta}]；无特征/异常时 None。"""
        if not getattr(self, "model_meta", None):
            return None
        try:
            x_row = self.features_for(lineup, enemy, ranks)
            top = attribute_linear_top(self.model_meta, x_row, 3)
            return [
                {
                    "key": item["key"],
                    "nameCn": FEATURE_CN_DRAFT.get(item["key"], item["key"]),
                    "delta": float(item["delta"]),
                    "pp": float(item.get("pp", round(float(item["delta"]) * 100.0, 2))),
                }
                for item in top
            ]
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ 敌方智能分路（T23）
    def lane_pick_rates(self, champ_key: str) -> Dict[str, float]:
        """该英雄在五路的 pickRate（缺失的路不出现）。"""
        key = normalize(champ_key)
        rates: Dict[str, float] = {}
        stats = self.opgg.get("stats") or {}
        for role in ROLES:
            entry = (stats.get(POS[role]) or {}).get(key)
            if entry and entry.get("pickRate") is not None:
                rates[role] = float(entry["pickRate"])
        return rates

    def assign_enemy_lanes(self, rows: List[dict]) -> List[dict]:
        """对敌方**已锁**英雄做最优分派：枚举剩余位置排列，最大化 pickRate 和。

        无 OP.GG 数据的英雄保留客户端 assignedPosition；我方分路不受影响。
        每行新增 laneProb（0-1 或 None）。
        """
        stats = self.opgg.get("stats") or {}
        if not stats:
            return rows
        locked = [r for r in rows if r.get("champKey") and r.get("locked")]
        if not locked:
            return rows
        from itertools import permutations

        rates_map: Dict[int, Dict[str, float]] = {}
        with_data: List[dict] = []
        for row in locked:
            rates = self.lane_pick_rates(row["champKey"])
            rates_map[id(row)] = rates
            if rates:
                with_data.append(row)
        used_roles = {
            r["role"]
            for r in locked
            if not rates_map.get(id(r)) and r.get("role")
        }
        pool = [role for role in ROLES if role not in used_roles]
        best: Optional[tuple] = None
        best_score = -1.0
        for perm in permutations(pool, len(with_data)):
            score = sum(rates_map[id(r)].get(perm[i], 0.0) for i, r in enumerate(with_data))
            if score > best_score:
                best_score, best = score, perm
        if best:
            for i, row in enumerate(with_data):
                role = best[i]
                row["role"] = role
                row["roleCn"] = ROLE_CN.get(role, "补位")
                row["laneProb"] = rates_map[id(row)].get(role)
        for row in locked:
            if "laneProb" not in row:
                rates = rates_map.get(id(row)) or {}
                row["laneProb"] = rates.get(row["role"]) if row.get("role") else None
        return rows

    # ------------------------------------------------------------ counter 推荐（T23）
    def _strength(self, role: str, champ_key: str) -> Optional[float]:
        """英雄强度：stats[分路] 的 winRate 与 pickRate 综合（无数据返回 None）。"""
        entry = (self.opgg.get("stats") or {}).get(POS[role]) or {}
        entry = entry.get(normalize(champ_key))
        if not entry:
            return None
        wr = entry.get("winRate")
        pr = entry.get("pickRate")
        if wr is None and pr is None:
            return None
        wr = float(wr) if wr is not None else 0.5
        pr = float(pr) if pr is not None else 0.0
        return wr + 0.3 * min(pr, 0.25)

    def matchup_entry(self, role: str, own: str, enemy: str) -> Optional[dict]:
        """matchups[lane][own][enemy] = {winRate, games}；缺失返回 None。"""
        entry = (
            ((self.opgg.get("matchups") or {}).get(POS[role]) or {})
            .get(normalize(own) or "")
            or {}
        ).get(normalize(enemy))
        if not entry or entry.get("winRate") is None:
            return None
        return entry

    def counter_candidates(
        self,
        role: str,
        enemy: Dict[str, str],
        limit: int = 6,
    ) -> Optional[List[dict]]:
        """对位克制推荐：本路对位为主 + 对敌方其余 4 人加成（games 加权），
        克制候选按强度（winRate+pickRate）排序取前 6；无对位数据返回 None
        （调用方回退现有 draft 模型推荐）。candidates 字段形状兼容。
        """
        rival = enemy.get(role)
        enemy_others = [enemy.get(r) for r in ROLES if r != role and enemy.get(r)]
        if not rival and not enemy_others:
            return None
        matchups_lane = (self.opgg.get("matchups") or {}).get(POS[role]) or {}
        if not matchups_lane:
            return None

        def confidence(games: Optional[float]) -> float:
            return 1.0 + min(float(games or 0), 200) / 200.0

        scored: List[dict] = []
        for own_key, enemies in matchups_lane.items():
            direct_wr: Optional[float] = None
            direct_games = 0
            direct_name = ""
            if rival:
                entry = enemies.get(normalize(rival))
                if entry and entry.get("winRate") is not None:
                    direct_wr = float(entry["winRate"])
                    direct_games = float(entry.get("games") or 0)
                    direct_name = self.champ_names.get(normalize(rival), rival)
            total = 0.0
            weight = 0.0
            for champ2 in enemy_others:
                entry = enemies.get(normalize(champ2))
                if not entry or entry.get("winRate") is None:
                    continue
                w = confidence(entry.get("games"))
                total += (float(entry["winRate"]) - 0.5) * w
                weight += w
            if direct_wr is not None:
                # 本路对位为主（权重更高），其余 4 人做小加成
                score = (direct_wr - 0.5) * confidence(direct_games) * 2.0
                if weight:
                    score += total / weight
                countered = direct_name
                counter_wr = direct_wr
            else:
                if not weight:
                    continue
                score = total / weight
                countered = "敌方阵容"
                counter_wr = 0.5 + score
            if score <= 0:
                continue  # 只保留克制（>50%）
            strength = self._strength(role, own_key)
            scored.append(
                {
                    "key": own_key,
                    "score": score,
                    "counter_wr": min(max(counter_wr, 0.0), 1.0),
                    "countered": countered,
                    "strength": strength,
                }
            )
        if not scored:
            return None
        scored.sort(key=lambda item: -item["score"])
        top = scored[:8]  # 先按克制强度取前 8
        top.sort(key=lambda item: -(item["strength"] if item["strength"] is not None else -1.0))
        results: List[dict] = []
        for item in top[:limit]:
            champ = self.champ_names.get(item["key"]) or item["key"]
            display = getattr(self, "display_by_key", {}).get(item["key"], champ)
            reason = f"克制 {item['countered']}（对位胜率 {item['counter_wr'] * 100:.1f}%）"
            if item["strength"] is not None:
                reason += f" · 强度 {item['strength'] * 100:.1f}%"
            results.append(
                {
                    "key": item["key"],
                    "champ": display,
                    "champKey": champ,
                    "champId": self.id_by_key.get(item["key"], 0),
                    "prob": round(item["counter_wr"], 4),
                    "opgg": round(item["strength"], 4) if item["strength"] is not None else None,
                    "cohort": None,
                    "cohortGames": None,
                    "matchup": round(item["counter_wr"], 4),
                    "countered": item["countered"],
                    "personal": None,
                    "score": round(item["score"], 4),
                    "reason": reason,
                    "recSource": "counter",
                }
            )
        return results

    def counter_candidates_any_role(self, enemy: Dict[str, str], limit: int = 6) -> Optional[List[dict]]:
        """T27：我方位置未知（自定义/盲选）时，按敌方已锁分路逐路找克制候选，
        合并去重（score 累加）后按强度排序。无候选返回 None。"""
        merged: Dict[str, dict] = {}
        for role in ROLES:
            if not enemy.get(role):
                continue
            candidates = self.counter_candidates(role, enemy)
            if not candidates:
                continue
            for candidate in candidates:
                key = candidate["key"]
                if key in merged:
                    merged[key]["score"] += candidate["score"]
                    merged[key]["lanes"].add(role)
                else:
                    item = dict(candidate)
                    item["lanes"] = {role}
                    merged[key] = item
        if not merged:
            return None
        items = list(merged.values())
        # 输出项口径：强度=opgg、对位胜率=matchup、克制对象=countered
        items.sort(key=lambda item: -(item["opgg"] if item["opgg"] is not None else -1.0))
        results: List[dict] = []
        for item in items[:limit]:
            lanes = "、".join(sorted(item.pop("lanes")))
            reason = f"克制 {item['countered']}（对位胜率 {item['matchup'] * 100:.1f}%·{lanes}）"
            if item["opgg"] is not None:
                reason += f" · 强度 {item['opgg'] * 100:.1f}%"
            item["reason"] = reason
            results.append(item)
        return results

    # ------------------------------------------------------------ 胜率-时间曲线（T23 ⑤ / T24 schema）
    def _curve_minutes(self) -> List[int]:
        try:
            count = int((self.curves.get("meta") or {}).get("minutes") or 40)
        except Exception:  # noqa: BLE001
            count = 40
        return list(range(1, count + 1))  # win[i] 对应第 i+1 分钟

    def _dict_win_values(self, entry: Any, count: int) -> Optional[List[float]]:
        """同构字典 {"1": 0.49, ..., "40": 0.51} → 每分钟 win 列表。"""
        if not isinstance(entry, dict):
            return None
        try:
            values = [float(entry.get(str(minute), float("nan"))) for minute in range(1, count + 1)]
        except Exception:  # noqa: BLE001
            return None
        if any(math.isnan(v) for v in values):
            return None
        return values

    def _role_win_values(self, role: str) -> Optional[List[float]]:
        if not self.curves:
            return None
        count = len(self._curve_minutes())
        entry = (self.curves.get("roles") or {}).get(role)
        values = self._dict_win_values(entry, count) if isinstance(entry, dict) else None
        if values is None:
            values = self._dict_win_values(self.curves.get("baseline") or {}, count)
        return values

    def lineup_curve(self, lineup: Dict[str, str], anchor: Optional[float] = None) -> Optional[dict]:
        """当前我方阵容的胜率-时间曲线：5 英雄按样本量（games）加权平均，
        样本不足（fallback/缺失）的英雄用分路基线并以 minGames 作为先验权重；
        末端线性缩放到当前 draft winProb（anchor）。curves.json 缺失返回 None。
        """
        if not self.curves:
            return None
        minutes = self._curve_minutes()
        count = len(minutes)
        try:
            min_games = float((self.curves.get("meta") or {}).get("minGames") or 50)
        except Exception:  # noqa: BLE001
            min_games = 50.0
        heroes = self.curves.get("heroes") or {}
        merged = [0.0] * count
        total_weight = 0.0
        for role in ROLES:
            champ = lineup.get(role)
            values: Optional[List[float]] = None
            weight = 0.0
            if champ:
                hero_entry = (heroes.get(normalize(champ)) or {}).get(role)
                if isinstance(hero_entry, dict):
                    values = hero_entry.get("win") if isinstance(hero_entry.get("win"), list) else None
                    if values is not None:
                        weight = float(hero_entry.get("games") or 0)
            if values is None:
                values = self._role_win_values(role)
                weight = min_games if values is not None else 0.0  # 分路基线按先验权重参与
            if values is None:
                values = [0.5] * count
            values = values[:count] + [0.5] * (count - len(values))
            for i in range(count):
                merged[i] += values[i] * weight
            total_weight += weight
        if total_weight > 0:
            merged = [v / total_weight for v in merged]
        else:
            merged = [sum(merged) / count for _ in range(count)]  # 全 fallback：基线均值
        if anchor is not None and merged:
            # T36：末端锚定用形状保持的加法平移（禁止乘法缩放——
            # 等比缩放会在末端值很小时把前段顶到 1.0），clamp 0.02-0.98
            anchor = min(max(float(anchor), 0.0), 1.0)
            shift = anchor - merged[-1]
            merged = [min(max(v + shift, 0.02), 0.98) for v in merged]
        return {"minutes": minutes, "winProb": [round(v, 4) for v in merged]}

    def team_curves(self, my_team: List[dict]) -> Optional[Dict[str, dict]]:
        """T32：己方已锁英雄各自分路的胜率-时间曲线（未锁英雄省略；
        curves.json 缺失返回 None）。形状 {champKey: {minutes, winProb}}，
        champKey 与 myTeam 行的 champKey 一致（英文别名）。"""
        if not self.curves:
            return None
        minutes = self._curve_minutes()
        count = len(minutes)
        heroes = self.curves.get("heroes") or {}
        out: Dict[str, dict] = {}
        for row in my_team:
            display_key = row.get("champKey") or ""
            role = row.get("role") or ""
            if not display_key or not role:
                continue
            values: Optional[List[float]] = None
            hero_entry = (heroes.get(normalize(display_key)) or {}).get(role)
            if isinstance(hero_entry, dict):
                win = hero_entry.get("win")
                if isinstance(win, list) and win:
                    values = [float(v) for v in win]
            if values is None:
                values = self._role_win_values(role)
            if values is None:
                values = [0.5] * count
            values = values[:count] + [0.5] * (count - len(values))
            out[display_key] = {"minutes": minutes, "winProb": [round(v, 4) for v in values]}
        return out or None

    def hero_curves(self, candidates: List[dict], my_role: str) -> Optional[Dict[str, dict]]:
        """前 6 个推荐候选英雄在我方分路的胜率-时间曲线。

        T32 修正形状：champKey → {minutes, winProb} 的 MAP（键与页面 heroSel 的
        candidates[].champKey 一致，即中文显示名）；fallback 用分路基线；
        curves.json 缺失返回 None。
        """
        if not self.curves or not my_role or not candidates:
            return None
        minutes = self._curve_minutes()
        count = len(minutes)
        heroes = self.curves.get("heroes") or {}
        out: Dict[str, dict] = {}
        for candidate in candidates[:6]:
            display_key = candidate.get("champKey") or candidate.get("champ") or ""
            lookup = normalize(candidate.get("key") or display_key or "")
            if not display_key:
                continue
            values: Optional[List[float]] = None
            hero_entry = (heroes.get(lookup) or {}).get(my_role)
            if isinstance(hero_entry, dict):
                win = hero_entry.get("win")
                if isinstance(win, list) and win:
                    values = [float(v) for v in win]
            if values is None:
                values = self._role_win_values(my_role)
            if values is None:
                values = [0.5] * count
            values = values[:count] + [0.5] * (count - len(values))
            out[display_key] = {"minutes": minutes, "winProb": [round(v, 4) for v in values]}
        return out or None

    def features_for(self, lineup: Dict[str, str], enemy: Dict[str, str], ranks: Dict[str, float]) -> List[float]:
        values: List[float] = []
        rank_diffs: List[float] = []
        own_op = enemy_op = own_co = enemy_co = 0.0
        own_rank_sum = enemy_rank_sum = 0.0
        for role in ROLES:
            mine = lineup.get(role)
            theirs = enemy.get(role)
            op_m = self.op_wr(role, mine) if mine else None
            op_t = self.op_wr(role, theirs) if theirs else None
            co_m = self.cohort(role, mine) if mine else None
            co_t = self.cohort(role, theirs) if theirs else None
            if mine and theirs:
                values.append((op_m - op_t) if (op_m is not None and op_t is not None) else 0.0)
                mu = self.matchup_wr(role, mine, theirs)
                values.append((mu - 0.5) if mu is not None else 0.0)
                values.append((co_m["wr"] - co_t["wr"]) if (co_m and co_t) else 0.0)
                known = bool(ranks.get(f"known:{role}"))
                diff = ranks.get(f"me:{role}", DEFAULT_RANK) - ranks.get(f"them:{role}", DEFAULT_RANK)
                values.append(diff / 100.0 if known else 0.0)
                if known:
                    rank_diffs.append(diff / 100.0)
            else:
                values.extend([0.0, 0.0, 0.0, 0.0])
            if op_m is not None:
                own_op += op_m - 0.5
            if op_t is not None:
                enemy_op += op_t - 0.5
            if co_m:
                own_co += co_m["wr"] - 0.5
            if co_t:
                enemy_co += co_t["wr"] - 0.5
            own_rank_sum += (ranks.get(f"me:{role}") or DEFAULT_RANK) / 1000.0
            enemy_rank_sum += (ranks.get(f"them:{role}") or DEFAULT_RANK) / 1000.0
        values.append(sum(rank_diffs) / len(rank_diffs) if rank_diffs else 0.0)
        values.append(own_op / 5.0)
        values.append(enemy_op / 5.0)
        values.append(own_co / 5.0)
        values.append(enemy_co / 5.0)
        values.append(own_rank_sum / 5.0)
        values.append(enemy_rank_sum / 5.0)
        return values

    def probability(self, lineup: Dict[str, str], enemy: Dict[str, str], ranks: Dict[str, float]) -> float:
        x = self.features_for(lineup, enemy, ranks)
        z = sum(((v - m) / s) * c for v, m, s, c in zip(x, self.mean, self.scale, self.coef))
        return 1.0 / (1.0 + math.exp(-(z + self.intercept)))

    def factors(self, lineup: Dict[str, str], enemy: Dict[str, str], ranks: Dict[str, float]) -> Dict[str, Any]:
        """给前端展示的三个可解释指标（段位均值 / 版本强度差 / 对位差）。"""
        my_ranks = [ranks.get(f"me:{role}") for role in ROLES if ranks.get(f"me:{role}")]
        their_ranks = [ranks.get(f"them:{role}") for role in ROLES if ranks.get(f"them:{role}")]
        own_op = [self.op_wr(role, champ) for role, champ in lineup.items() if self.op_wr(role, champ) is not None]
        enemy_op = [self.op_wr(role, champ) for role, champ in enemy.items() if self.op_wr(role, champ) is not None]
        lanes = []
        for role, mine in lineup.items():
            theirs = enemy.get(role)
            if theirs:
                mu = self.matchup_wr(role, mine, theirs)
                if mu is not None:
                    lanes.append(mu - 0.5)
        return {
            "myRank": round(sum(my_ranks) / len(my_ranks)) if my_ranks else None,
            "theirRank": round(sum(their_ranks) / len(their_ranks)) if their_ranks else None,
            "champEdge": round((sum(own_op) / len(own_op) - sum(enemy_op) / len(enemy_op)) * 100, 1)
            if own_op and enemy_op else None,
            "laneEdge": round(sum(lanes) / len(lanes) * 100, 1) if lanes else None,
            "picked": len(lineup),
            "enemyPicked": len(enemy),
        }

    # ------------------------------------------------------------ candidates
    def candidates(
        self,
        role: str,
        lineup: Dict[str, str],
        enemy: Dict[str, str],
        ranks: Dict[str, float],
        personal: Dict[str, dict],
        limit: int = 6,
    ) -> List[dict]:
        keys = set(self.champ_stats.get(role, {})) | {
            normalize(name) for name in (self.opgg.get("stats", {}).get(POS[role]) or {})
        }
        keys.discard(normalize(lineup.get(role, "")))
        rival = enemy.get(role)
        results = []
        for key in keys:
            champ = self.champ_names.get(key) or key
            display = getattr(self, "display_by_key", {}).get(key) or champ
            trial = dict(lineup)
            trial[role] = champ
            prob = self.probability(trial, enemy, ranks)
            op = self.op_wr(role, champ)
            co = self.cohort(role, champ)
            mu = self.matchup_wr(role, champ, rival) if rival else None
            mine = personal.get(key)
            score = prob
            if mine and mine.get("games"):
                games = int(mine["games"])
                wr = mine.get("wr")
                if wr is None:
                    wr = (float(mine.get("wins") or 0) / games) if games else 0.5
                score += 0.002 * (float(wr) - 0.5) * min(6, games)
            results.append(
                {
                    "key": key,
                    "champ": display,
                    "champKey": champ,
                    "champId": self.id_by_key.get(key, 0),
                    "prob": round(prob, 4),
                    "opgg": round(op, 4) if op is not None else None,
                    "cohort": round(co["wr"], 4) if co else None,
                    "cohortGames": co["games"] if co else None,
                    "matchup": round(mu, 4) if mu is not None else None,
                    "personal": mine,
                    "score": round(score, 4),
                }
            )
        results.sort(key=lambda item: -item["score"])
        top = results[:limit]
        for item in top:
            # T27：梯度回退显式标注（透明化）
            item["reason"] = "【梯度】" + (self._reason(role, item) or "样本不足")
            item["recSource"] = "tier"
        return top

    def _reason(self, role: str, item: dict) -> str:
        parts = []
        if item["opgg"] is not None:
            parts.append(f"版本 {item['opgg'] * 100:.1f}%")
        if item["matchup"] is not None:
            parts.append(f"对线 {item['matchup'] * 100:.0f}%")
        if item["cohort"] is not None and item["cohortGames"]:
            parts.append(f"高分段 {item['cohort'] * 100:.1f}%（{item['cohortGames']} 场）")
        mine = item.get("personal")
        if mine and mine.get("games"):
            parts.append(f"你 {mine['wins']} 胜 {mine['games'] - mine['wins']} 负")
        return " · ".join(parts) if parts else "样本不足"


class DraftService:
    """把 LCU 选人 session 转成前端要的结构（含 demo）。"""

    def __init__(self, model: DraftModel, demo: bool = False) -> None:
        self.model = model
        self.demo = demo
        self.lock = threading.RLock()
        self.history: List[dict] = []
        self.last_signature = ""
        self.champion_by_id: Dict[int, str] = {}
        self.alias_by_id: Dict[int, str] = {}
        self.display_by_key: Dict[str, str] = {}
        self.id_by_key: Dict[str, int] = {}
        self.rank_cache: Dict[str, float] = {}
        self.name_cache: Dict[str, str] = {}
        self.personal_cache: Dict[str, Any] = {"at": 0.0, "data": {}}
        self.personal_loading = False
        self.frozen: Optional[dict] = None
        self.last_session_at = 0.0
        self._game_name: Optional[str] = None
        self._latest_payload: Optional[dict] = None
        self._latest_at = 0.0
        self._rebuilding = False
        self._rebuild_again = False
        self._last_rec_log = ""
        self._ws_session: Optional[dict] = None
        self._ws_phase = ""
        self._ws_stop = threading.Event()
        self._sse_queues: List["queue.Queue[Optional[dict]]"] = []
        self.lcu: Optional[Tuple[int, str]] = None
        self.demo_started = time.time()
        if demo:
            self._seed_demo_history()
        else:
            self.history.append({"t": 0.0, "p": 0.5})
        self.model.id_by_key = self.id_by_key
        self.model.display_by_key = self.display_by_key
        self._load_champion_cache()
        self.lcu = discover_lcu_psutil()
        self.resolver = None
        if self.lcu and not demo:
            try:
                self.resolver = RankResolver(self.lcu[0], self.lcu[1], cache_path=None)
            except Exception:
                self.resolver = None
        if self.lcu:
            threading.Thread(target=self._refresh_champions, daemon=True).start()
        if self.lcu and not demo:
            threading.Thread(target=self._ws_loop, daemon=True).start()
        if not demo:
            # 启动时先建一次状态（可能没有客户端，返回等待态）
            threading.Thread(target=self.rebuild_payload, daemon=True).start()

    # ------------------------------------------------------------ SSE / 推送
    def subscribe(self) -> "queue.Queue[Optional[dict]]":
        q: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=4)
        with self.lock:
            self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Optional[dict]]") -> None:
        with self.lock:
            if q in self._sse_queues:
                self._sse_queues.remove(q)

    def _push(self, payload: dict) -> None:
        with self.lock:
            queues = list(self._sse_queues)
        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except Exception:
                        pass
                q.put_nowait(payload)
            except Exception:
                pass

    # ------------------------------------------------------------ LCU websocket（阿卡丽同款：订阅 OnJsonApiEvent）
    def _ws_loop(self) -> None:
        import websocket  # websocket-client

        while not self._ws_stop.is_set():
            if not self.lcu and not self._rediscover():
                self._ws_stop.wait(2.0)
                continue
            port, token = self.lcu
            url = f"wss://riot:{token}@127.0.0.1:{port}"
            header = ["Authorization: Basic " + base64.b64encode(f"riot:{token}".encode()).decode()]
            ws = None
            try:
                ws = websocket.create_connection(
                    url,
                    sslopt={"cert_reqs": ssl.CERT_NONE},
                    timeout=30,
                    header=header,
                    suppress_origin=True,
                )
                ws.send(json.dumps([5, "OnJsonApiEvent"]))
                print(f"LCU websocket connected {port}", flush=True)
                # 初始拉取（事件之外兜底一次 REST）
                session = self._lcu_json("/lol-champ-select/v1/session")
                phase = self._lcu_json("/lol-gameflow/v1/gameflow-phase") or ""
                if session is not None:
                    self._ws_session = session
                self._ws_phase = phase
                self.rebuild_payload()
                ws.settimeout(2.0)
                while not self._ws_stop.is_set():
                    try:
                        msg = ws.recv()
                        if msg:
                            self._handle_ws_message(msg)
                    except websocket.WebSocketTimeoutException:
                        # 定期兜底：事件漏了也能对齐（刷新后立即重建/推送，
                        # 否则只能等页面 4s 轮询 + 5s 新鲜度窗口，实时性变差）
                        session = self._lcu_json("/lol-champ-select/v1/session")
                        phase = self._lcu_json("/lol-gameflow/v1/gameflow-phase") or ""
                        if session is not None:
                            self._ws_session = session
                        if phase:
                            self._ws_phase = phase
                        self.rebuild_payload()
                        continue
                    except Exception:
                        break
            except Exception:
                pass
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            self._ws_stop.wait(2.0)

    def _handle_ws_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            if not isinstance(msg, list) or len(msg) < 3 or msg[1] != "OnJsonApiEvent":
                return
            evt = msg[2]
            uri = evt.get("uri") or ""
            if uri == "/lol-champ-select/v1/session":
                self._ws_session = evt.get("data") if evt.get("eventType") != "Delete" else None
                self.rebuild_payload()
            elif uri == "/lol-gameflow/v1/gameflow-phase":
                self._ws_phase = evt.get("data") or ""
                self.rebuild_payload()
        except Exception:
            pass

    # ------------------------------------------------------------ LCU
    def _rediscover(self) -> bool:
        """客户端重启会换端口/token，这里自动重新发现，避免一直「等待」。"""
        now = time.time()
        if now - getattr(self, "_lcu_retry_at", 0.0) < 3.0:
            return False
        self._lcu_retry_at = now
        found = discover_lcu_psutil()
        if not found or found == self.lcu:
            return False
        self.lcu = found
        try:
            self.resolver = RankResolver(found[0], found[1], cache_path=None)
        except Exception:
            self.resolver = None
        self.rank_cache.clear()
        self.name_cache.clear()
        threading.Thread(target=self._refresh_champions, daemon=True).start()
        return True

    def _lcu_request(self, path: str) -> Optional[bytes]:
        try:
            ctx = ssl._create_unverified_context()
            connection = http.client.HTTPSConnection("127.0.0.1", self.lcu[0], timeout=3, context=ctx)
            auth = "Basic " + base64.b64encode(f"riot:{self.lcu[1]}".encode()).decode()
            connection.request("GET", path, headers={"Authorization": auth})
            response = connection.getresponse()
            body = response.read()
            connection.close()
            return body if response.status == 200 else None
        except Exception:
            return None

    def _lcu_bytes(self, path: str) -> Optional[bytes]:
        if not self.lcu and not self._rediscover():
            return None
        raw = self._lcu_request(path) if self.lcu else None
        if raw is None and self._rediscover():
            raw = self._lcu_request(path)
        return raw

    def _lcu_json(self, path: str) -> Optional[Any]:
        raw = self._lcu_bytes(path)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _load_champion_cache(self) -> None:
        path = ASSETS / "champions.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        for cid, item in data.items():
            alias = item.get("alias") or ""
            display = item.get("name") or item.get("title") or alias
            if not alias:
                continue
            key = normalize(alias)
            self.champion_by_id[int(cid)] = display
            self.alias_by_id[int(cid)] = alias
            self.display_by_key[key] = display
            self.id_by_key[key] = int(cid)

    def _refresh_champions(self) -> None:
        data = self._lcu_json("/lol-game-data/assets/v1/champion-summary.json")
        if not isinstance(data, list):
            return
        with self.lock:
            for item in data:
                cid = item.get("id")
                if not cid or cid <= 0:
                    continue
                alias = item.get("alias") or item.get("name") or ""
                key = normalize(alias)
                display = self.display_by_key.get(key) or item.get("name") or alias
                if not alias:
                    continue
                key = normalize(alias)
                self.champion_by_id[int(cid)] = display
                self.alias_by_id[int(cid)] = alias
                self.display_by_key[key] = display
                self.id_by_key[key] = int(cid)

    def champion_icon(self, champ_id: int) -> Optional[bytes]:
        return self._lcu_bytes(f"/lol-game-data/assets/v1/champion-icons/{champ_id}.png")

    def _summoner_name(self, summoner_id: Any) -> str:
        if not summoner_id:
            return ""
        key = str(summoner_id)
        if key in self.name_cache:
            return self.name_cache[key]
        payload = self._lcu_json(f"/lol-summoner/v1/summoners/{summoner_id}") or {}
        game_name = payload.get("gameName") or payload.get("displayName") or ""
        tag = payload.get("tagLine") or ""
        name = f"{game_name}#{tag}" if (game_name and tag) else game_name
        if name:
            self.name_cache[key] = name
        return name

    def _rank_for_summoner(self, summoner_id: Any) -> Optional[float]:
        if not self.resolver or not summoner_id:
            return None
        key = str(summoner_id)
        if key in self.rank_cache:
            return self.rank_cache[key]
        summoner = self.resolver._get(f"/lol-summoner/v1/summoners/{summoner_id}") or {}
        puuid = summoner.get("puuid")
        score = self.resolver.score_for_puuid(puuid) if puuid else None
        if score is not None:
            self.rank_cache[key] = float(score)
        return float(score) if score is not None else None

    def _personal(self) -> Dict[str, dict]:
        """个人最近战绩：后台线程拉取，接口永远先用缓存，避免 BP 时卡住。"""
        now = time.time()
        if now - self.personal_cache["at"] < 600:
            return self.personal_cache["data"]
        if not self.personal_loading and self.lcu and self.resolver:
            self.personal_loading = True
            threading.Thread(target=self._personal_worker, daemon=True).start()
        return self.personal_cache["data"] or {}

    def _personal_worker(self) -> None:
        result: Dict[str, dict] = {}
        try:
            from player_elo import SgpClient

            me = self.resolver._get("/lol-summoner/v1/current-summoner") or {}
            puuid = me.get("puuid")
            if puuid:
                client = SgpClient(self.lcu[0], self.lcu[1])
                for game in client.history(puuid, count=30):
                    parts = game.get("participants") or []
                    mine = next((p for p in parts if p.get("puuid") == puuid), None)
                    if not mine:
                        continue
                    name = mine.get("championName") or ""
                    if not name:
                        continue
                    key = normalize(name)
                    bucket = result.setdefault(key, {"games": 0, "wins": 0})
                    bucket["games"] += 1
                    bucket["wins"] += 1 if mine.get("win") else 0
        except Exception:
            result = result or {}
        finally:
            self.personal_cache = {"at": time.time(), "data": result}
            self.personal_loading = False

    # ------------------------------------------------------------ state
    def _html_version(self) -> int:
        try:
            return int(HTML.stat().st_mtime)
        except Exception:
            return 0

    def state(self) -> dict:
        if self.demo:
            return self._demo_state()
        latest = self._latest_payload
        if latest is not None and time.time() - self._latest_at < 5:
            return latest
        return self.rebuild_payload()

    def rebuild_payload(self) -> dict:
        """按最新缓存（websocket 事件驱动）重建页面数据，并推给 SSE。"""
        if self._rebuilding:
            # 正在重建：标记需要再来一次，调用方先拿最近结果
            self._rebuild_again = True
            return self._latest_payload or self._waiting_payload()
        self._rebuilding = True
        self._rebuild_again = False
        try:
            while True:
                self._rebuild_again = False
                payload = self._rebuild_once()
                if not self._rebuild_again:
                    return payload
        finally:
            self._rebuilding = False

    def _rebuild_once(self) -> dict:
            v = self._html_version()
            session = self._ws_session
            if session is None:
                # websocket 没拿到时兜底 REST 一次
                session = self._lcu_json("/lol-champ-select/v1/session")
                if session is not None:
                    self._ws_session = session
            if session:
                payload = self._live_state(session)
                payload["v"] = v
                with self.lock:
                    self._latest_payload = payload
                    self._latest_at = time.time()
                    self.frozen = json.loads(json.dumps(payload, ensure_ascii=False))
                    self.last_session_at = time.time()
                self._push(payload)
                return payload
            # 没有选人 session：如果刚结束选人（进游戏），把最后阵容冻结住，避免回退到“等待”
            phase = self._ws_phase or (self._lcu_json("/lol-gameflow/v1/gameflow-phase") or "")
            with self.lock:
                frozen = self.frozen
                last_at = self.last_session_at
            if frozen and (
                phase in ("InProgress", "WaitingForStats", "Reconnect", "PreEndOfGame", "EndOfGame")
                or time.time() - last_at < 600
            ):
                frozen = json.loads(json.dumps(frozen, ensure_ascii=False))
                if phase in ("InProgress", "Reconnect"):
                    frozen["phase"] = "IN_GAME"
                elif phase in ("WaitingForStats", "PreEndOfGame", "EndOfGame"):
                    frozen["phase"] = "END_OF_GAME"
                else:
                    frozen["phase"] = "LAST_DRAFT"
                frozen["left"] = 0
                frozen["frozen"] = True
                frozen["source"] = "in_game"
                frozen["v"] = v
                self._fill_from_2999(frozen)
                with self.lock:
                    self._latest_payload = frozen
                    self._latest_at = time.time()
                    self.frozen = frozen
                self._push(frozen)
                return frozen
            payload = self._waiting_payload()
            with self.lock:
                self._latest_payload = payload
                self._latest_at = time.time()
            self._push(payload)
            return payload

    def _waiting_payload(self) -> dict:
        return {
            "ok": False,
            "source": "live",
            "message": "等待进入选人阶段…",
            "lcu": {"connected": bool(self.lcu), "port": self.lcu[0] if self.lcu else None},
            "v": self._html_version(),
        }

    def _liveclient_players(self) -> Optional[List[dict]]:
        """2999 Live Client Data：先 playerlist，失败退回 allgamedata.allPlayers。"""
        for path, key in (("/liveclientdata/playerlist", None), ("/liveclientdata/allgamedata", "allPlayers")):
            try:
                ctx = ssl._create_unverified_context()
                connection = http.client.HTTPSConnection("127.0.0.1", 2999, timeout=2, context=ctx)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                connection.close()
                if response.status != 200:
                    continue
                data = json.loads(body)
                if key:
                    data = data.get(key)
                if isinstance(data, list) and data:
                    return data
            except Exception:
                continue
        return None

    def _my_game_name(self) -> str:
        if getattr(self, "_game_name", None) is not None:
            return self._game_name
        self._game_name = ""
        if self.resolver:
            me = self.resolver._get("/lol-summoner/v1/current-summoner") or {}
            self._game_name = me.get("gameName") or me.get("displayName") or ""
        return self._game_name

    @staticmethod
    def _norm_name(name: Any) -> str:
        # 去掉 #TAG 和空格统一比较（LCU 是 gameName，游戏内可能是 gameName#TAG）
        text = (str(name or "").strip().lower())
        return text.split("#")[0].strip()

    def _fill_from_2999(self, payload: dict) -> bool:
        """进游戏后用 Live Client Data 补全双方英雄（bot 的选人在 session 里没有）。"""
        players = self._liveclient_players()
        if not players:
            print("[2999] no players (接口不可用)", flush=True)
            if not self._fill_from_gameflow(payload):
                return False
            return True
        me = self._norm_name(self._my_game_name())
        mine_team = ""
        if me:
            for p in players:
                for field in ("summonerName", "riotId", "gameName"):
                    if self._norm_name(p.get(field)) == me:
                        mine_team = p.get("team") or ""
                        break
                if mine_team:
                    break
        if not mine_team:
            print(f"[2999] cannot match self name {me!r} among {[self._norm_name(p.get('summonerName') or p.get('riotId')) for p in players][:10]}", flush=True)
            if not self._fill_from_gameflow(payload):
                return False
            return True
        my_pool = [p for p in players if p.get("team") == mine_team]
        en_pool = [p for p in players if p.get("team") != mine_team]
        print(f"[2999] players={len(players)} mine_team={mine_team} my={len(my_pool)} enemy={len(en_pool)}", flush=True)

        def apply(rows: List[dict], pool: List[dict]) -> None:
            used: set = set()
            for row in rows:
                if row.get("champ"):
                    continue
                want = normalize(row.get("champKey") or "")
                for idx, p in enumerate(pool):
                    if idx in used:
                        continue
                    cname = p.get("championName") or ""
                    if not cname:
                        continue
                    pkey = normalize(cname)
                    if want and pkey != want:
                        continue
                    row["champKey"] = cname
                    row["champ"] = self.display_by_key.get(pkey, cname)
                    row["champId"] = self.id_by_key.get(pkey, 0)
                    row["locked"] = True
                    row["name"] = p.get("summonerName") or p.get("riotId") or row.get("name") or ""
                    pos = LCU_ROLE.get(str(p.get("position") or "").lower(), "")
                    if pos and not row.get("role"):
                        row["role"] = pos
                        row["roleCn"] = ROLE_CN.get(pos, "补位")
                    used.add(idx)
                    break

        apply(payload.get("myTeam") or [], my_pool)
        apply(payload.get("theirTeam") or [], en_pool)
        return True

    def _fill_from_gameflow(self, payload: dict) -> bool:
        """2999 不可用时的兜底：LCU gameflow session 的 teamOne/teamTwo 里有 championId。"""
        try:
            gf = self._lcu_json("/lol-gameflow/v1/session") or {}
        except Exception:
            return False
        team_one = gf.get("teamOne") or []
        team_two = gf.get("teamTwo") or []
        me_id = None
        if self.resolver:
            me = self.resolver._get("/lol-summoner/v1/current-summoner") or {}
            me_id = me.get("summonerId")
        mine_side = ""
        for side, team in (("teamOne", team_one), ("teamTwo", team_two)):
            for player in team:
                if player.get("summonerId") and me_id and int(player.get("summonerId") or 0) == int(me_id or 0):
                    mine_side = side
                    break
            if mine_side:
                break
        if not mine_side:
            print(f"[gameflow] cannot locate self (me_id={me_id}, t1={len(team_one)}, t2={len(team_two)})", flush=True)
            return False
        my_team = team_one if mine_side == "teamOne" else team_two
        en_team = team_two if mine_side == "teamOne" else team_one

        def champ_of(player: dict) -> str:
            key = self.alias_by_id.get(int(player.get("championId") or 0), "")
            return key

        def apply(rows: List[dict], pool: List[dict]) -> None:
            used: set = set()
            for row in rows:
                if row.get("champ"):
                    continue
                for idx, p in enumerate(pool):
                    if idx in used:
                        continue
                    cname = champ_of(p)
                    if not cname:
                        continue
                    pkey = normalize(cname)
                    row["champKey"] = cname
                    row["champ"] = self.display_by_key.get(pkey, cname)
                    row["champId"] = self.id_by_key.get(pkey, 0)
                    row["locked"] = True
                    row["name"] = row.get("name") or ""
                    used.add(idx)
                    break

        apply(payload.get("myTeam") or [], my_team)
        apply(payload.get("theirTeam") or [], en_team)
        print(f"[gameflow] filled my={len(my_team)} enemy={len(en_team)}", flush=True)
        return True

    def _log_once(self, message: str) -> None:
        """仅当消息变化时打一行日志（避免每次 rebuild 刷屏）。"""
        if getattr(self, "_last_rec_log", None) == message:
            return
        self._last_rec_log = message
        print(message, file=sys.stderr, flush=True)

    def _recommend(
        self,
        my_role: str,
        lineup: Dict[str, str],
        enemy: Dict[str, str],
        ranks: Dict[str, float],
        personal: Dict[str, dict],
    ) -> Tuple[List[dict], str, str]:
        """T27 counter 触发规则：敌方任一英雄已知 → 必须 counter；
        仅敌方全空才回退梯度榜。返回 (candidates, recSource, recNote)。"""
        enemy_known = any(enemy.values())
        candidates: List[dict] = []
        if enemy_known:
            if my_role:
                candidates = self.model.counter_candidates(my_role, enemy) or []
            else:
                candidates = self.model.counter_candidates_any_role(enemy) or []
            if candidates:
                source = "counter"
                note = f"敌方已锁 {len(enemy)} 路，counter 推荐"
                if not my_role:
                    note += "（我方位置未知，跨分路克制）"
            else:
                source = "tier"
                note = "敌方英雄已知但无对位数据，已回退梯度推荐"
                if my_role:
                    candidates = self.model.candidates(my_role, lineup, enemy, ranks, personal)
        else:
            source = "tier"
            note = "敌方选人不可见（自定义/盲选未公开），已回退梯度推荐"
            if my_role:
                candidates = self.model.candidates(my_role, lineup, enemy, ranks, personal)
        self._log_once(f"[rec] source={source} · {note}")
        return candidates, source, note

    def _parse_team(
        self,
        team: List[dict],
        mine: bool,
        ranks: Dict[str, float],
        action_picks: Optional[Dict[int, int]] = None,
    ) -> List[dict]:
        rows = []
        action_picks = action_picks or {}
        for player in team:
            cell_id = int(player.get("cellId") or -1)
            role = LCU_ROLE.get(str(player.get("assignedPosition") or "").lower(), "") or \
                LCU_ROLE.get(str(player.get("selectedPosition") or "").lower(), "") or \
                LCU_ROLE.get(str(player.get("position") or "").lower(), "")
            # T27：championId 缺失时用 actions 重建表兜底（自定义房渠道）
            cid = int(player.get("championId") or 0) or action_picks.get(cell_id, 0)
            intent = int(player.get("championPickIntent") or 0)
            pick_id = cid or intent
            key = self.alias_by_id.get(pick_id, "")
            display = self.display_by_key.get(normalize(key), key) if key else ""
            if role:
                score = self._rank_for_summoner(player.get("summonerId"))
                if mine:
                    ranks[f"me:{role}"] = score if score is not None else DEFAULT_RANK
                    ranks[f"known:{role}"] = 1 if score is not None else 0
                else:
                    ranks[f"them:{role}"] = score if score is not None else DEFAULT_RANK
                    if score is not None and not ranks.get(f"known:{role}"):
                        ranks[f"known:{role}"] = 0
            rows.append(
                {
                    "role": role,
                    "roleCn": ROLE_CN.get(role, "补位"),
                    "champ": display or key,
                    "champKey": key,
                    "champId": pick_id,
                    "locked": bool(cid),
                    "laneProb": None,
                    "rank": self._rank_for_summoner(player.get("summonerId")),
                    "name": self._summoner_name(player.get("summonerId")),
                    "isMe": bool(mine and player.get("cellId") == getattr(self, "_me_cell", None)),
                }
            )
        return rows

    def _live_state(self, session: dict) -> dict:
        try:
            DEBUG_SESSION.write_text(json.dumps(session, ensure_ascii=False, indent=1)[:400000], encoding="utf-8")
        except Exception:
            pass
        with self.lock:
            self._me_cell = session.get("localPlayerCellId")
            ranks: Dict[str, float] = {}
            # T27：从 actions 重建双方已锁阵容（自定义房 theirTeam 可能为空）
            action_picks: Dict[int, int] = {}
            for group in session.get("actions") or []:
                for act in group or []:
                    if not isinstance(act, dict):
                        continue
                    if str(act.get("type") or "").lower() != "pick":
                        continue
                    if "completed" in act and not act.get("completed"):
                        continue
                    champion_id = int(act.get("championId") or 0)
                    cell_id = int(act.get("actorCellId") or -1)
                    if champion_id > 0 and cell_id >= 0:
                        action_picks[cell_id] = champion_id
            my_cells = session.get("myTeam") or []
            their_cells = session.get("theirTeam") or []
            if not their_cells and action_picks and my_cells:
                # 自定义房兜底：theirTeam 为空时按 cellId 归属重建（我方 cellId 之外的 pick 归敌方）
                my_ids = {int(c.get("cellId") or -1) for c in my_cells}
                rebuilt = [
                    {"cellId": cid, "championId": cid_champ, "assignedPosition": "",
                     "selectedPosition": "", "summonerId": None}
                    for cid, cid_champ in sorted(action_picks.items())
                    if cid not in my_ids
                ]
                if rebuilt:
                    their_cells = rebuilt
                    self._log_once(f"[actions] theirTeam 为空，按 actions 重建 {len(rebuilt)} 人")
            my_team = self._parse_team(my_cells, True, ranks, action_picks)
            their_team = self._parse_team(their_cells, False, ranks, action_picks)
            # T23：敌方已锁英雄用 OP.GG 五路 pickRate 做最优分派（覆盖 role/roleCn/laneProb）
            their_team = self.model.assign_enemy_lanes(their_team)
            lineup: Dict[str, str] = {}
            enemy: Dict[str, str] = {}
            for row in my_team:
                if row["role"] and row["champKey"] and (row["role"] not in lineup or row["isMe"]):
                    lineup[row["role"]] = row["champKey"]
            for row in their_team:
                if row["role"] and row["champKey"] and row["role"] not in enemy:
                    enemy[row["role"]] = row["champKey"]
            my_role = next((row["role"] for row in my_team if row["isMe"]), "") or ""
            timer = session.get("timer") or {}
            left = float(timer.get("adjustedTimeLeftInPhase") or 0) / 1000.0
            phase = str(timer.get("phase") or "")
            bans = session.get("bans") or {}
            my_bans = [
                {"id": int(x), "name": self.champion_by_id.get(int(x), "")}
                for x in (bans.get("myTeamBans") or [])
            ]
            their_bans = [
                {"id": int(x), "name": self.champion_by_id.get(int(x), "")}
                for x in (bans.get("theirTeamBans") or [])
            ]
            probability = self.model.probability(lineup, enemy, ranks) if (lineup or enemy) else 0.5
            personal = self._personal() if my_role else {}
            candidates, rec_source, rec_note = self._recommend(my_role, lineup, enemy, ranks, personal)
            curve = self.model.lineup_curve(lineup, anchor=probability)
            hero_curves = self.model.hero_curves(candidates, my_role) if my_role else None
            team_curves = self.model.team_curves(my_team)
            attribution = self.model.draft_attribution(lineup, enemy, ranks) if (lineup or enemy) else None
            return self._assemble(
                source="live",
                phase=phase,
                left=left,
                my_role=my_role,
                my_team=my_team,
                their_team=their_team,
                bans={"mine": my_bans, "theirs": their_bans},
                probability=probability,
                candidates=candidates,
                lineup=lineup,
                enemy=enemy,
                ranks=ranks,
                curve=curve,
                hero_curves=hero_curves,
                team_curves=team_curves,
                attribution=attribution,
                rec_source=rec_source,
                rec_note=rec_note,
            )

    def _display(self, champ_key: str) -> str:
        if not champ_key:
            return ""
        return self.display_by_key.get(normalize(champ_key), champ_key)

    # ------------------------------------------------------------ demo
    def _demo_ranks(self, lineup: Dict[str, str], enemy: Dict[str, str]) -> Dict[str, float]:
        ranks: Dict[str, float] = {}
        for role in ROLES:
            if role in lineup:
                ranks[f"me:{role}"] = 5000 + 60 * ROLES.index(role)
                ranks[f"known:{role}"] = 1
            if role in enemy:
                ranks[f"them:{role}"] = 4950 + 55 * ROLES.index(role)
                ranks[f"known:{role}"] = 1
        return ranks

    def _seed_demo_history(self) -> None:
        """演示模式：把整段选人过程的胜率预先算好，曲线一打开就有形状。"""
        for step in self._demo_script():
            lineup = dict(step.get("my") or {})
            enemy = dict(step.get("their") or {})
            if lineup or enemy:
                probability = self.model.probability(lineup, enemy, self._demo_ranks(lineup, enemy))
            else:
                probability = 0.5
            self.history.append({"t": round(24.0 - step["at"], 1), "p": round(float(probability), 4)})

    def _demo_script(self) -> List[dict]:
        return [
            {"at": 0, "me": "MIDDLE", "my": {"MIDDLE": "Ahri"}, "their": {}},
            {"at": 3, "my": {"TOP": "Garen", "MIDDLE": "Ahri"}, "their": {"TOP": "KSante"}},
            {"at": 6, "my": {"TOP": "Garen", "JUNGLE": "LeeSin", "MIDDLE": "Ahri"}, "their": {"TOP": "KSante", "JUNGLE": "Vi"}},
            {"at": 9, "my": {"TOP": "Garen", "JUNGLE": "LeeSin", "MIDDLE": "Ahri"}, "their": {"TOP": "KSante", "JUNGLE": "Vi", "MIDDLE": "Syndra"}},
            {"at": 12, "my": {"TOP": "Garen", "JUNGLE": "LeeSin", "MIDDLE": "Ahri", "BOTTOM": "Jinx"}, "their": {"TOP": "KSante", "JUNGLE": "Vi", "MIDDLE": "Syndra", "BOTTOM": "Caitlyn"}},
            {"at": 15, "my": {"TOP": "Garen", "JUNGLE": "LeeSin", "MIDDLE": "Ahri", "BOTTOM": "Jinx", "UTILITY": "Thresh"}, "their": {"TOP": "KSante", "JUNGLE": "Vi", "MIDDLE": "Syndra", "BOTTOM": "Caitlyn", "UTILITY": "Lulu"}},
        ]

    def _demo_state(self) -> dict:
        elapsed = (time.time() - self.demo_started) % 24.0
        script = self._demo_script()
        current = script[0]
        for step in script:
            if elapsed >= step["at"]:
                current = step
        lineup = dict(current.get("my") or {})
        enemy = dict(current.get("their") or {})
        ranks: Dict[str, float] = {}
        for role in ROLES:
            if role in lineup:
                ranks[f"me:{role}"] = 5000 + 60 * ROLES.index(role)
                ranks[f"known:{role}"] = 1
            if role in enemy:
                ranks[f"them:{role}"] = 4950 + 55 * ROLES.index(role)
                ranks[f"known:{role}"] = 1
        probability = self.model.probability(lineup, enemy, ranks) if (lineup or enemy) else 0.5
        my_team = [
            {"role": role, "roleCn": ROLE_CN[role],
             "champ": self._display(lineup.get(role, "")),
             "champKey": lineup.get(role, ""),
             "champId": self.id_by_key.get(normalize(lineup.get(role, "")), 0),
             "locked": bool(lineup.get(role)), "rank": ranks.get(f"me:{role}"),
             "name": ("DemoUser#8888" if role == (current.get("me") or "MIDDLE") else f"蓝方{ROLE_CN[role]}#1000"),
             "isMe": role == (current.get("me") or "MIDDLE")}
            for role in ROLES
        ]
        their_team = [
            {"role": role, "roleCn": ROLE_CN[role],
             "champ": self._display(enemy.get(role, "")),
             "champKey": enemy.get(role, ""),
             "champId": self.id_by_key.get(normalize(enemy.get(role, "")), 0),
             "locked": bool(enemy.get(role)), "laneProb": None, "rank": ranks.get(f"them:{role}"),
             "name": f"红方{ROLE_CN[role]}#2000",
             "isMe": False}
            for role in ROLES
        ]
        # 演示模式同样跑敌方智能分路（与 live 口径一致）
        their_team = self.model.assign_enemy_lanes(their_team)
        candidates, rec_source, rec_note = self._recommend("MIDDLE", lineup, enemy, ranks, {})
        curve = self.model.lineup_curve(lineup, anchor=probability)
        hero_curves = self.model.hero_curves(candidates, "MIDDLE")
        team_curves = self.model.team_curves(my_team)
        attribution = self.model.draft_attribution(lineup, enemy, ranks) if (lineup or enemy) else None
        left = 24.0 - (elapsed % 24.0)
        return self._assemble(
            source="demo",
            phase="BAN_PICK",
            left=left,
            my_role="MIDDLE",
            my_team=my_team,
            their_team=their_team,
            bans={
                "mine": [{"id": self.id_by_key.get("zed", 238), "name": self._display("Zed")}],
                "theirs": [{"id": self.id_by_key.get("yasuo", 157), "name": self._display("Yasuo")}],
            },
            probability=probability,
            candidates=candidates,
            lineup=lineup,
            enemy=enemy,
            ranks=ranks,
            curve=curve,
            hero_curves=hero_curves,
            team_curves=team_curves,
            attribution=attribution,
            rec_source=rec_source,
            rec_note=rec_note,
        )

    # ------------------------------------------------------------ assemble
    def _rank_status_payload(self) -> Dict[str, Any]:
        """段位解析器诊断快照（T18：透传给前端，新增字段向后兼容）。"""
        if self.resolver is None:
            return {
                "state": "unconnected", "port": None, "source": None,
                "lastError": None, "lastHttpStatus": None, "lastNote": None,
                "queries": 0, "errors": 0, "unranked": 0, "lookupReasons": {},
            }
        try:
            status = self.resolver.status()
        except Exception:  # noqa: BLE001
            status = {}
        if status.get("lastError") or status.get("lastNote"):
            state = "failed"
        elif self.rank_cache or status.get("queries"):
            state = "ok"
        else:
            state = "resolving"
        status["state"] = state
        return status

    def _rank_error_text(self) -> Optional[str]:
        if self.resolver is None:
            return "LCU 未连接"
        try:
            status = self.resolver.status()
        except Exception:  # noqa: BLE001
            return None
        return status.get("lastError") or status.get("lastNote")

    def _assemble(self, **kwargs: Any) -> dict:
        signature = json.dumps(
            {
                "my": kwargs["lineup"],
                "enemy": kwargs["enemy"],
                "bans": kwargs["bans"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        with self.lock:
            if kwargs["source"] != "demo" and signature != self.last_signature:
                self.last_signature = signature
                self.history.append(
                    {
                        "t": round(kwargs["left"], 1),
                        "p": round(float(kwargs["probability"]), 4),
                    }
                )
                self.history = self.history[-40:]
            history = list(self.history)
        return {
            "ok": True,
            "source": kwargs["source"],
            "phase": kwargs["phase"],
            "left": round(kwargs["left"], 1),
            "myRole": kwargs["my_role"],
            "myRoleCn": ROLE_CN.get(kwargs["my_role"], ""),
            "myTeam": kwargs["my_team"],
            "theirTeam": kwargs["their_team"],
            "bans": kwargs["bans"],
            "winProb": round(float(kwargs["probability"]), 4),
            "factors": self.model.factors(kwargs["lineup"], kwargs["enemy"], kwargs["ranks"]),
            "history": history,
            "candidates": kwargs["candidates"],
            "modelAuc": self.model.auc,
            "curve": kwargs.get("curve"),
            "heroCurves": kwargs.get("hero_curves"),
            "teamCurves": kwargs.get("team_curves"),
            "attribution": kwargs.get("attribution"),
            "recSource": kwargs.get("rec_source", "tier"),
            "recNote": kwargs.get("rec_note", ""),
            "lcu": {"connected": bool(self.lcu), "port": self.lcu[0] if self.lcu else None},
            "rankStatus": self._rank_status_payload(),
            "rankError": self._rank_error_text(),
        }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DraftAdvisor/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._serve_file(HTML, "text/html; charset=utf-8")
        elif parsed.path == "/design-system.css":
            self._serve_file(DESIGN_CSS, "text/css; charset=utf-8")
        elif parsed.path == "/demo-data.js":
            self._serve_file(DEMO_JS, "application/javascript; charset=utf-8")
        elif parsed.path == "/api/state":
            service: DraftService = self.server.service  # type: ignore[attr-defined]
            try:
                payload = json.dumps(service.state(), ensure_ascii=False).encode("utf-8")
                status = 200
            except Exception:
                traceback.print_exc()
                payload = json.dumps(
                    {
                        "ok": False,
                        "source": "live",
                        "message": "服务端异常（已写入日志）",
                        "error": traceback.format_exc(limit=3),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                status = 500
            self._send(payload, "application/json; charset=utf-8", status=status)
        elif parsed.path == "/api/events":
            service = self.server.service  # type: ignore[attr-defined]
            self._sse(service)
        elif parsed.path.startswith("/icon/"):
            match = re.fullmatch(r"/icon/(\d+)\.png", parsed.path)
            service = self.server.service  # type: ignore[attr-defined]
            data = service.champion_icon(int(match.group(1))) if match else None
            if data:
                self._send(data, "image/png", cache=True)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def _sse(self, service: "DraftService") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = service.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if payload is None:
                    continue
                data = json.dumps(payload, ensure_ascii=False)
                self.wfile.write(("data: " + data + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except Exception:
            pass
        finally:
            service.unsubscribe(q)

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            self._send(path.read_bytes(), content_type)
        except Exception:
            self.send_error(404)

    def _send(self, payload: bytes, content_type: str, cache: bool = False, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # noqa: A003
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="选人助手（网页版）")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--demo", action="store_true", help="假数据演示，自动循环选人")
    args = parser.parse_args(argv)

    model = DraftModel()
    service = DraftService(model, demo=args.demo)
    server = Server((args.host, args.port), Handler)
    server.service = service  # type: ignore[attr-defined]
    print(f"选人助手: http://{args.host}:{args.port}  ({'演示模式' if args.demo else 'LCU 实时模式'})")
    print(f"模型: draft_model (AUC {model.auc}) · 英雄 {len(model.champ_names)} 个")
    if not args.demo:
        print("等待进入选人阶段…（LCU：" + ("已连接" if service.lcu else "未找到") + "）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
