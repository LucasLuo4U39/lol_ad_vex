#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""敌方打野追踪状态机：小地图现身事件 + OP.GG 开野路线 → 位置/下一区域/威胁路。

只依赖标准库（+可选 numpy，本实现不用）。可离线测试：
    python -B jungler_tracker.py --self-test

jungle_routes.json 期望格式（pipeline-dev 产出请按此对齐；本模块 loader 兼容
Path 与 dict 两种输入，并做字段校验）：
{
  "meta": {"source": "op.gg", "patch": "16.22", "notes": "...", "generatedAt": "..."},
  "camps": {                                   // 可选：区域名 → side/half/lane 提示
    "blue_blue": {"side": "blue", "half": "bottom"},   // half：blue 野区=bottom、red 野区=top
    "gromp_blue": {"lane": "top"}
  },
  "routes": {
    "leesin": [                                // champKey = 小写字母数字（normalize_champ 口径）
      {"id": "r1", "games": 812, "rank": 1, "steps": [    // games 用于加权；rank 仅记录
        {"zone": "blue_blue", "t": 95},        // t = 从对局开始算的秒（典型进入/清完时间）
        {"zone": "wolves_blue", "t": null},    // t 可为 null（OP.GG jungle-paths 无时间戳）：
        {"zone": "gromp_blue", "t": null}      //   按清野速度合成（首营 1:30、每营含跑图 30s）
      ]},
      {"id": "r2", "games": 188, "steps": [...]}
    ]
  }
}
注意：时间戳缺失时合成的时间是「按标准清野速度估算」，输出与 UI 需注明。

状态机口径：
- 初始化（无现身）：取该英雄 games 最多的路线，按 game_time 推演当前营地与下一营地
  （路线时间戳按 config.time_scale 对齐缩放，默认 1.0）；
- 每次 last-seen：以出现区域为锚；候选路线 = 含该区域的路线，权重 = games ×
  时间一致加成（|路线该步 t - 出现时间| ≤ TOL 记 1.0，≤ 2×TOL 记 0.5，否则 0.25）；
  predictedNextZone = 候选路线下一步的 games 加权众数，eta = 加权平均步间时长
  （从锚点时刻起算，随时间递减）；无路线包含该区域时按全路线加权外推、低置信；
- 未见超时：secondsAgo > DECAY_START 后 confidence 按 exp 衰减至 MIN_CONF 下限；
  从未出现过则从 game_time=0 起算（初始先验本身置信度就低于现身校准后）。

局限（UI 必须标注）：路线是统计推荐，不是真实意图；小地图现身区域由 pipeline
识别，可能有漏报/误报；超过衰减阈值后仅作「大概在野区哪一侧」的参考。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent

# 区域名别名（pipeline 的 region 命名 → 规范键）。规范键 = "<camp>_<side>"，
# camp ∈ {blue, red, wolves, raptors, gromp, krugs}，side ∈ {blue, red}。
ZONE_ALIASES = {
    "bluebuff": "blue",
    "redbuff": "red",
    "chickens": "raptors",
    "wolfs": "wolves",
    "gromph": "gromp",
    "crugs": "krugs",
    "topside": "top",
    "botside": "bot",
    "midside": "mid",
}

# 区域 → 邻接分路（side 感知；camps 表可覆盖）。
_LANE_BY_ZONE = {
    "blue_blue": "bot", "red_blue": "top", "wolves_blue": "mid", "raptors_blue": "mid",
    "gromp_blue": "top", "krugs_blue": "bot",
    "blue_red": "top", "red_red": "bot", "wolves_red": "mid", "raptors_red": "mid",
    "gromp_red": "bot", "krugs_red": "top",
}

DEFAULT_CONFIG = {
    "time_scale": 1.0,        # 路线时间戳缩放（如清野速度差异）
    "camp_first_time": 90.0,  # steps[].t 为 null 时合成：首个营地 1:30 刷出
    "camp_step_seconds": 30.0,  # 合成：每营地含跑图约 30s
    "max_routes": 20,         # 每英雄最多保留的路线条数（按 games 降序）
    "tol": 90.0,              # 时间一致加成窗口（秒）
    "tol2": 180.0,            # 半加成窗口（秒）
    "decay_start": 45.0,      # 超过该秒数未见后开始衰减
    "decay_tau": 120.0,       # 衰减时间常数（秒）
    "min_confidence": 0.08,   # 置信度下限
    "prior_base": 0.25,       # 未见先验：base + share 系数
    "prior_share": 0.35,
    "sight_base": 0.25,       # 现身校准：base + share 系数 + 时间加成
    "sight_share": 0.5,
    "sight_time": 0.2,
    "fallback_confidence": 0.2,  # 区域不在任何路线时的置信度
    "default_transition": 30.0,  # 无路线信息时的默认步间时长
}


def normalize_champ_key(name: Any) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def canonicalize_zone(name: Any) -> str:
    """region 字符串 → 规范键（小写、非字母数字转 _、应用别名）。"""
    text = str(name or "").lower().strip()
    if not text:
        return ""
    parts = [ch if ch.isalnum() else "_" for ch in text]
    text = "".join(parts).strip("_")
    for alias, replacement in ZONE_ALIASES.items():
        text = text.replace(alias, replacement)
    # 合并连续下划线并去头尾
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def normalize_route(
    raw: Dict[str, Any],
    camp_first_time: float = 90.0,
    camp_step_seconds: float = 30.0,
) -> Optional[Dict[str, Any]]:
    """校验并规范化一条路线。非法返回 None。

    steps[].t 可为 null（OP.GG jungle-paths 只有营地顺序，无时间戳）：
    - 全部为 null → 按标准清野速度合成：t_i = camp_first_time + i × camp_step_seconds；
    - 部分为 null → 缺失步用「前一步已知时间 + camp_step_seconds」补齐
      （无前一步时取后一步已知时间 − camp_step_seconds）。
    timeSource 标记 "data" / "synthesized" / "mixed"（时间=按清野速度估算）。
    """
    steps_raw = raw.get("steps")
    games = raw.get("games")
    if not isinstance(steps_raw, list) or not steps_raw:
        return None
    try:
        games = int(games)
    except (TypeError, ValueError):
        games = 1
    if games <= 0:
        return None

    def parse_time(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    collected: List[Tuple[str, Optional[float]]] = []
    for step in steps_raw:
        zone = canonicalize_zone(step.get("zone"))
        if not zone:
            continue
        collected.append((zone, parse_time(step.get("t"))))
    if not collected:
        return None

    has_null = any(time_value is None for _, time_value in collected)
    all_null = all(time_value is None for _, time_value in collected)

    resolved: List[Tuple[str, float]] = []
    last_time: Optional[float] = None
    for index, (zone, time_value) in enumerate(collected):
        if time_value is not None:
            resolved.append((zone, time_value))
            last_time = time_value
            continue
        if last_time is not None:
            value = last_time + camp_step_seconds
        else:
            next_known = next(
                (t for _, t in collected[index + 1:] if t is not None), None
            )
            value = (
                max(0.0, next_known - camp_step_seconds)
                if next_known is not None
                else camp_first_time + index * camp_step_seconds
            )
        resolved.append((zone, value))
        last_time = value

    resolved.sort(key=lambda item: item[1])
    time_source = "synthesized" if all_null else ("mixed" if has_null else "data")
    return {
        "id": str(raw.get("id") or ""),
        "side": str(raw.get("side") or ""),
        "games": games,
        "steps": resolved,
        "timeSource": time_source,
        "rank": raw.get("rank"),
    }


def load_jungle_routes(
    source: Any,
    camp_first_time: float = 90.0,
    camp_step_seconds: float = 30.0,
    max_routes: int = 20,
) -> Dict[str, Any]:
    """读 jungle_routes.json（Path 或 dict），返回 {"routes": {champ: [route...]}, "camps": {...}, "meta": {...}}。

    兼容：steps[].t 可为 null（按清野速度合成时间）、每侧最多 max_routes 条
    （按 games 降序截断）、meta.notes 原样保留。
    """
    payload = source
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("jungle_routes 数据必须是 JSON 对象")
    routes: Dict[str, List[Dict[str, Any]]] = {}
    for champ_key, raw_routes in (payload.get("routes") or {}).items():
        by_side: Dict[str, List[Dict[str, Any]]] = {}
        for raw in raw_routes:
            route = normalize_route(raw, camp_first_time, camp_step_seconds)
            if route:
                by_side.setdefault(str(route.get("side") or ""), []).append(route)
        normalized: List[Dict[str, Any]] = []
        for side_routes in by_side.values():
            side_routes.sort(key=lambda route: -route["games"])
            normalized.extend(side_routes[:max_routes])  # 每侧（含无 side 的合成数据）截断
        if normalized:
            routes[normalize_champ_key(champ_key)] = normalized
    return {
        "routes": routes,
        "camps": payload.get("camps") or {},
        "meta": payload.get("meta") or {},
    }


def threat_lane(zone: str, camps: Optional[Dict[str, Any]] = None) -> str:
    """区域 → 邻接分路（top/mid/bot）。

    优先级：camps 表的 lane 字段 > 区域名含 top/bot > 内置邻接表
    > camps 表的 half（blue 野区=bottom、red 野区=top）> mid。
    """
    if camps:
        entry = camps.get(zone) or {}
        lane = str(entry.get("lane") or "").lower()
        if lane in ("top", "mid", "bot"):
            return lane
    if "top" in zone:
        return "top"
    if "bot" in zone:
        return "bot"
    if zone in _LANE_BY_ZONE:
        return _LANE_BY_ZONE[zone]
    if camps:
        entry = camps.get(zone) or {}
        half = str(entry.get("half") or "").lower()
        if half == "bottom":
            return "bot"
        if half == "top":
            return "top"
    return "mid"


class JunglerTracker:
    """单英雄状态机。observe() 喂现身事件，predict() 输出当前状态。"""

    def __init__(
        self,
        champ_key: str,
        routes: Sequence[Dict[str, Any]],
        camps: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        side: Optional[str] = None,
    ) -> None:
        self.champ_key = normalize_champ_key(champ_key)
        self.side = str(side or "").lower() or None
        if self.side:
            routes = [route for route in routes if str(route.get("side") or "").lower() == self.side]
        self.routes = list(routes)
        self.camps = camps or {}
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.total_games = float(sum(route["games"] for route in self.routes)) or 1.0
        self.last_seen: Optional[Tuple[str, float]] = None
        self.anchor_next: Optional[Dict[str, float]] = None   # zone -> 加权和
        self.anchor_offsets: Optional[Dict[str, float]] = None  # zone -> 加权 offset 和
        self.anchor_conf: Optional[float] = None
        self.hypothesis: Optional[str] = None
        self.hypothesis_time_source: Optional[str] = None
        self.candidates: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ 内部
    def _route_positions(self, zone: str) -> List[Tuple[Dict[str, Any], int]]:
        found = []
        for route in self.routes:
            for index, (step_zone, _) in enumerate(route["steps"]):
                if step_zone == zone:
                    found.append((route, index))
                    break
        return found

    def _decay(self, seconds: float) -> float:
        if seconds <= self.config["decay_start"]:
            return 1.0
        return math.exp(
            -(seconds - self.config["decay_start"]) / self.config["decay_tau"]
        )

    def _clamp_conf(self, value: float) -> float:
        return max(self.config["min_confidence"], min(0.95, value))

    def _prior_state(self, game_time: float) -> Tuple[Optional[str], Optional[str], float]:
        """无现身事件：按 games 最多的路线推演。返回 (currentZone, nextZone, eta)。"""
        if not self.routes:
            return None, None, 0.0
        scale = self.config["time_scale"]
        route = self.routes[0]
        current: Optional[str] = None
        current_time = 0.0
        next_zone: Optional[str] = None
        next_time = None
        for zone, time_value in route["steps"]:
            scaled = time_value * scale
            if scaled <= game_time + 1e-9:
                current, current_time = zone, scaled
            else:
                next_zone, next_time = zone, scaled
                break
        if current is None:
            current, current_time = route["steps"][0][0], route["steps"][0][1] * scale
        eta = max(0.0, (next_time - game_time) if next_time is not None else 0.0)
        self.hypothesis = route["id"] or None
        self.hypothesis_time_source = route.get("timeSource")
        return current, next_zone, eta

    # ------------------------------------------------------------ API
    def observe(self, zone: Any, game_time: float) -> None:
        """喂入一次 last-seen（小地图识别出的区域 + 对局时间）。"""
        canonical = canonicalize_zone(zone)
        time_value = float(game_time)
        if not canonical:
            return
        self.last_seen = (canonical, time_value)

        positions = self._route_positions(canonical)
        config = self.config
        scale = config["time_scale"]
        next_weights: Dict[str, float] = {}
        offset_sums: Dict[str, float] = {}
        matched_games = 0.0
        best_bonus = 0.0
        for route, index in positions:
            steps = route["steps"]
            step_time = steps[index][1] * scale
            delta = abs(step_time - time_value)
            if delta <= config["tol"]:
                bonus = 1.0
            elif delta <= config["tol2"]:
                bonus = 0.5
            else:
                bonus = 0.25
            best_bonus = max(best_bonus, bonus)
            weight = float(route["games"]) * bonus
            matched_games += weight
            if index + 1 < len(steps):
                next_zone = steps[index + 1][0]
                offset = max(0.0, (steps[index + 1][1] - steps[index][1]) * scale)
                next_weights[next_zone] = next_weights.get(next_zone, 0.0) + weight
                offset_sums[next_zone] = offset_sums.get(next_zone, 0.0) + weight * offset

        if positions and next_weights:
            self.anchor_next = next_weights
            self.anchor_offsets = offset_sums
            share = matched_games / self.total_games
            self.anchor_conf = self._clamp_conf(
                config["sight_base"]
                + config["sight_share"] * share
                + config["sight_time"] * best_bonus
            )
            best = max(positions, key=lambda item: item[0]["games"] * (
                1.0 if abs(item[0]["steps"][item[1]][1] * scale - time_value) <= config["tol"] else 0.5
            ))
            self.hypothesis = best[0]["id"] or None
            self.hypothesis_time_source = best[0].get("timeSource")
        else:
            # 区域不在任何路线：以出现位置为锚，按全路线加权外推下一步
            self.anchor_next = None
            self.anchor_offsets = None
            self.anchor_conf = self._clamp_conf(config["fallback_confidence"])
            self.hypothesis = None
            self.hypothesis_time_source = None

    def predict(self, game_time: float) -> Dict[str, Any]:
        """当前状态。返回 schema 见模块 docstring/任务说明。"""
        time_value = float(game_time)
        config = self.config
        scale = config["time_scale"]

        if self.last_seen is not None:
            zone, seen_at = self.last_seen
            seconds_ago = max(0.0, time_value - seen_at)
            base_conf = self.anchor_conf if self.anchor_conf is not None else config["fallback_confidence"]
            confidence = self._clamp_conf(base_conf * self._decay(seconds_ago))
            current_zone = zone
            if self.anchor_next:
                next_zone = max(self.anchor_next, key=lambda key: self.anchor_next[key])
                total = self.anchor_next[next_zone]
                offset = (
                    self.anchor_offsets[next_zone] / total
                    if self.anchor_offsets and total > 0
                    else config["default_transition"]
                )
                eta = max(0.0, offset - seconds_ago)
                self.candidates = sorted(
                    (
                        {"zone": key, "weight": round(value / max(1.0, sum(self.anchor_next.values())), 4)}
                        for key, value in self.anchor_next.items()
                    ),
                    key=lambda item: -item["weight"],
                )
            else:
                # 无路线匹配：全路线中「下一步」的 games 加权众数
                weight_map: Dict[str, float] = {}
                for route in self.routes:
                    for index, (step_zone, step_time) in enumerate(route["steps"]):
                        if step_time * scale > time_value and index > 0:
                            weight_map[step_zone] = weight_map.get(step_zone, 0.0) + route["games"]
                            break
                if weight_map:
                    next_zone = max(weight_map, key=lambda key: weight_map[key])
                    eta = config["default_transition"]
                    self.candidates = sorted(
                        (
                            {"zone": key, "weight": round(value / max(1.0, sum(weight_map.values())), 4)}
                            for key, value in weight_map.items()
                        ),
                        key=lambda item: -item["weight"],
                    )
                else:
                    next_zone = None
                    eta = 0.0
                    self.candidates = []
        else:
            seconds_ago = max(0.0, time_value)
            current_zone, next_zone, eta = self._prior_state(time_value)
            if not self.routes:
                confidence = config["min_confidence"]
            else:
                share = self.routes[0]["games"] / self.total_games
                confidence = self._clamp_conf(
                    (config["prior_base"] + config["prior_share"] * share)
                    * self._decay(seconds_ago)
                )
            self.candidates = []

        return {
            "champKey": self.champ_key,
            "currentZone": current_zone,
            "predictedNextZone": next_zone,
            "etaSeconds": round(eta, 1),
            "threatLane": threat_lane(next_zone, self.camps) if next_zone else None,
            "lastSeen": (
                {"zone": self.last_seen[0], "secondsAgo": round(seconds_ago, 1)}
                if self.last_seen is not None
                else None
            ),
            "confidence": round(confidence, 4),
            "neverSeen": self.last_seen is None,
            "routeHypothesis": self.hypothesis,
            "timeBasis": self.hypothesis_time_source,
            "side": self.side,
            "candidates": self.candidates[:3],
        }

    def reset(self) -> None:
        self.last_seen = None
        self.anchor_next = None
        self.anchor_offsets = None
        self.anchor_conf = None
        self.hypothesis = None
        self.candidates = []


def build_tracker(
    source: Any,
    champ_key: str,
    side: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> JunglerTracker:
    """构造追踪器。side="blue"/"red" 时只保留该阵营视角的开野路线
    （OP.GG 的 side=team_key：营地均在己方野区；敌方在红方则传 "red"）。"""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    data = load_jungle_routes(
        source,
        camp_first_time=cfg["camp_first_time"],
        camp_step_seconds=cfg["camp_step_seconds"],
        max_routes=cfg["max_routes"],
    )
    routes = data["routes"].get(normalize_champ_key(champ_key), [])
    return JunglerTracker(champ_key, routes, camps=data["camps"], config=cfg, side=side)


# --------------------------------------------------------------------------
# 自检：合成路线 + 合成事件流
# --------------------------------------------------------------------------
def synthetic_routes() -> Dict[str, Any]:
    return {
        "routes": {
            "leesin": [
                {
                    "id": "r1", "games": 800,
                    "steps": [
                        {"zone": "blue_blue", "t": 95},
                        {"zone": "wolves_blue", "t": 150},
                        {"zone": "gromp_blue", "t": 210},
                    ],
                },
                {
                    "id": "r2", "games": 200,
                    "steps": [
                        {"zone": "red_blue", "t": 95},
                        {"zone": "raptors_blue", "t": 155},
                        {"zone": "krugs_blue", "t": 215},
                    ],
                },
            ],
            "vi": [
                {
                    "id": "v1", "games": 700,
                    "steps": [
                        {"zone": "blue_blue", "t": 95},
                        {"zone": "wolves_blue", "t": 150},
                        {"zone": "gromp_blue", "t": 210},
                    ],
                },
                {
                    "id": "v2", "games": 300,
                    "steps": [
                        {"zone": "blue_blue", "t": 95},
                        {"zone": "red_blue", "t": 150},
                        {"zone": "raptors_blue", "t": 205},
                    ],
                },
            ],
        },
        "camps": {"gromp_blue": {"lane": "top"}, "krugs_blue": {"lane": "bot"}},
    }


def run_selftest() -> int:
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

    print("[1] 无事件：按最常用路线推演（leesin r1=800 局）")
    tracker = build_tracker(synthetic_routes(), "leeSin")
    state = tracker.predict(180)
    check("currentZone=wolves_blue", state["currentZone"] == "wolves_blue", str(state["currentZone"]))
    check("predictedNextZone=gromp_blue", state["predictedNextZone"] == "gromp_blue", str(state["predictedNextZone"]))
    check("eta=30s", abs(state["etaSeconds"] - 30.0) < 0.5, str(state["etaSeconds"]))
    check("threatLane=top（gromp 邻接上路）", state["threatLane"] == "top", str(state["threatLane"]))
    check("lastSeen=None", state["lastSeen"] is None)
    check("confidence 在 (0,0.6)", 0.0 < state["confidence"] < 0.6, str(state["confidence"]))

    print("[2] 一次出现（匹配 r1）：校准 + 置信度提升")
    tracker2 = build_tracker(synthetic_routes(), "leesin")
    tracker2.observe("blue_blue", 95)
    state2 = tracker2.predict(105)
    check("currentZone=blue_blue", state2["currentZone"] == "blue_blue", str(state2))
    check("predictedNextZone=wolves_blue", state2["predictedNextZone"] == "wolves_blue")
    check("eta=55-10=45s", abs(state2["etaSeconds"] - 45.0) < 0.5, str(state2["etaSeconds"]))
    check("lastSeen 10s 前", state2["lastSeen"] is not None and abs(state2["lastSeen"]["secondsAgo"] - 10.0) < 0.1, str(state2["lastSeen"]))
    check("置信度高于无事件", state2["confidence"] > state["confidence"], f"{state2['confidence']} vs {state['confidence']}")

    print("[3] 两次出现（连续匹配）：锚点更新")
    tracker3 = build_tracker(synthetic_routes(), "leesin")
    tracker3.observe("blue_blue", 95)
    tracker3.observe("wolves_blue", 150)
    state3 = tracker3.predict(160)
    check("currentZone=wolves_blue", state3["currentZone"] == "wolves_blue", str(state3["currentZone"]))
    check("predictedNextZone=gromp_blue", state3["predictedNextZone"] == "gromp_blue")
    check("eta=60-10=50s", abs(state3["etaSeconds"] - 50.0) < 0.5, str(state3["etaSeconds"]))
    check("置信度 ≥ 一次出现", state3["confidence"] >= state2["confidence"], f"{state3['confidence']} vs {state2['confidence']}")

    print("[4] 路线冲突：现身匹配少数派路线 r2 → 假设切换")
    tracker4 = build_tracker(synthetic_routes(), "leesin")
    tracker4.observe("red_blue", 95)
    state4 = tracker4.predict(100)
    check("predictedNextZone=raptors_blue（r2 路线）", state4["predictedNextZone"] == "raptors_blue", str(state4["predictedNextZone"]))
    check("routeHypothesis=r2", state4["routeHypothesis"] == "r2", str(state4["routeHypothesis"]))

    print("[5] 未见超时：置信度单调衰减且不破下限")
    tracker5 = build_tracker(synthetic_routes(), "leesin")
    tracker5.observe("blue_blue", 95)
    confs = [tracker5.predict(t)["confidence"] for t in (105, 145, 200, 350, 500)]
    check("置信度随时间单调不增", all(confs[i] >= confs[i + 1] for i in range(len(confs) - 1)), str(confs))
    check("衰减至下限 0.08", confs[-1] >= 0.08 - 1e-9, str(confs[-1]))
    check("eta 随时间递减后归零", tracker5.predict(500)["etaSeconds"] == 0.0, str(tracker5.predict(500)["etaSeconds"]))

    print("[6] 未知区域出现：低置信降级但不崩溃")
    tracker6 = build_tracker(synthetic_routes(), "leesin")
    tracker6.observe("krugs_blue", 120)
    state6 = tracker6.predict(130)
    check("currentZone=krugs_blue", state6["currentZone"] == "krugs_blue", str(state6["currentZone"]))
    check("predictedNextZone 非空", bool(state6["predictedNextZone"]), str(state6["predictedNextZone"]))
    check("置信度低于路线匹配场景", state6["confidence"] < state2["confidence"], f"{state6['confidence']} vs {state2['confidence']}")
    check(
        "threatLane 与 predictedNextZone 的邻接一致",
        state6["threatLane"] == threat_lane(state6["predictedNextZone"], synthetic_routes()["camps"]),
        f"{state6['threatLane']} vs {state6['predictedNextZone']}",
    )

    print("[7] 加权众数：vi 双路线 700/300 都含 blue_blue")
    tracker7 = build_tracker(synthetic_routes(), "vi")
    tracker7.observe("blue_blue", 95)
    state7 = tracker7.predict(100)
    check("predictedNextZone=wolves_blue（700 局路线胜出）", state7["predictedNextZone"] == "wolves_blue", str(state7["predictedNextZone"]))
    check("eta=55-5=50s（现身 5 秒后查询）", abs(state7["etaSeconds"] - 50.0) < 0.5, str(state7["etaSeconds"]))
    check("candidates 首位权重=0.7", abs(state7["candidates"][0]["weight"] - 0.7) < 0.05, str(state7["candidates"]))

    print("[8] 数值合理性：路线时间戳与 loader 校验")
    data = load_jungle_routes(synthetic_routes())
    for champ_routes in data["routes"].values():
        for route in champ_routes:
            times = [t for _, t in route["steps"]]
            check(
                f"{route['id']} 步骤时间单调递增",
                all(times[i] < times[i + 1] for i in range(len(times) - 1)),
                str(times),
            )
            check(
                f"{route['id']} 营地时间在 [30,600]s 合理区间",
                all(30.0 <= t <= 600.0 for t in times),
                str(times),
            )
    check("champKey 归一化（leeSin→leesin）", build_tracker(synthetic_routes(), "leeSin").champ_key == "leesin")
    check("未知英雄无路线不崩溃", build_tracker(synthetic_routes(), "aatrox").predict(120)["confidence"] >= 0.0)

    print("[9] t=null：按清野速度合成时间（首营 1:30、每营含跑图 30s）")
    no_t_routes = {
        "meta": {"notes": "opgg jungle-paths, no timestamps"},
        "routes": {
            "sejuani": [
                {
                    "id": "s1", "games": 500,
                    "steps": [
                        {"zone": "blue_blue", "t": None},
                        {"zone": "gromp_blue", "t": None},
                        {"zone": "wolves_blue", "t": None},
                    ],
                }
            ]
        },
    }
    tracker9 = build_tracker(no_t_routes, "sejuani")
    check("合成时间=90/120/150", [t for _, t in tracker9.routes[0]["steps"]] == [90.0, 120.0, 150.0], str(tracker9.routes[0]["steps"]))
    check("timeSource=synthesized", tracker9.routes[0]["timeSource"] == "synthesized", tracker9.routes[0]["timeSource"])
    state9 = tracker9.predict(100)
    check("t=100 → current=blue_blue、next=gromp_blue", state9["currentZone"] == "blue_blue" and state9["predictedNextZone"] == "gromp_blue", str(state9))
    check("eta=120-100=20s", abs(state9["etaSeconds"] - 20.0) < 0.5, str(state9["etaSeconds"]))
    check("输出 timeBasis=synthesized（时间=按清野速度估算）", state9.get("timeBasis") == "synthesized", str(state9.get("timeBasis")))

    mixed_routes = {
        "routes": {
            "sejuani": [
                {
                    "id": "s1", "games": 500,
                    "steps": [
                        {"zone": "blue_blue", "t": 95},
                        {"zone": "gromp_blue", "t": None},
                        {"zone": "wolves_blue", "t": None},
                    ],
                }
            ]
        }
    }
    tracker_mixed = build_tracker(mixed_routes, "sejuani")
    check("混合补齐=95/125/155", [t for _, t in tracker_mixed.routes[0]["steps"]] == [95.0, 125.0, 155.0], str(tracker_mixed.routes[0]["steps"]))
    check("timeSource=mixed", tracker_mixed.routes[0]["timeSource"] == "mixed")

    many_routes = {
        "routes": {
            "sejuani": [
                {
                    "id": f"r{i}", "games": 100 - i,
                    "steps": [{"zone": "blue_blue", "t": None}, {"zone": "wolves_blue", "t": None}],
                }
                for i in range(25)
            ]
        }
    }
    tracker_many = build_tracker(many_routes, "sejuani")
    check("每侧路线按 games 截断至 20 条", len(tracker_many.routes) == 20, str(len(tracker_many.routes)))

    print("[10] 真实 assets/jungle_routes.json 冒烟（缺失则跳过）")
    real_path = BASE / "assets" / "jungle_routes.json"
    if real_path.is_file():
        data_real = load_jungle_routes(real_path)
        hero_count = len(data_real["routes"])
        check("真实数据 55 个英雄加载", hero_count == 55, str(hero_count))
        tracker_blue = build_tracker(real_path, "leesin", side="blue")
        tracker_red = build_tracker(real_path, "leesin", side="red")
        check("leesin 蓝方路线 20 条", len(tracker_blue.routes) == 20, str(len(tracker_blue.routes)))
        check("leesin 红方路线 20 条", len(tracker_red.routes) == 20, str(len(tracker_red.routes)))
        check(
            "蓝方路线营地均在己方野区（_blue）",
            all(zone.endswith("_blue") for route in tracker_blue.routes for zone, _ in route["steps"]),
        )
        check(
            "红方路线营地均在己方野区（_red）",
            all(zone.endswith("_red") for route in tracker_red.routes for zone, _ in route["steps"]),
        )
        check(
            "合成时间=90/120/150/180/210/240（6 步）",
            [t for _, t in tracker_blue.routes[0]["steps"]] == [90.0, 120.0, 150.0, 180.0, 210.0, 240.0],
            str(tracker_blue.routes[0]["steps"]),
        )
        state_real = tracker_blue.predict(100)
        check("t=100 先验 currentZone=raptors_blue（首选营地 70%）", state_real["currentZone"] == "raptors_blue", str(state_real["currentZone"]))
        check("先验 timeBasis=synthesized", state_real["timeBasis"] == "synthesized", str(state_real["timeBasis"]))
        tracker_blue.observe("raptors_blue", 90)
        state_seen = tracker_blue.predict(100)
        check("现身 raptors_blue 后 next=red_blue", state_seen["predictedNextZone"] == "red_blue", str(state_seen["predictedNextZone"]))
        check("eta=30-10=20s", abs(state_seen["etaSeconds"] - 20.0) < 0.5, str(state_seen["etaSeconds"]))
        check("输出 side=blue", state_seen.get("side") == "blue", str(state_seen.get("side")))
        check("置信度在 (0,1]", 0.0 < state_seen["confidence"] <= 1.0, str(state_seen["confidence"]))
    else:
        print(f"  （跳过：{real_path} 不存在）")

    print(f"\n结果: PASS={passed} FAIL={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(run_selftest())
    print("用法: python -B jungler_tracker.py --self-test")
    raise SystemExit(2)
