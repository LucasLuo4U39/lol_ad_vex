#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LoL 实时助手（桌面版）：Web 渲染主窗口（Edge app 模式加载 ui/next/home.html）。

复用 advisor_server 的采集/模型/LLM 逻辑，进程内起两个本地服务：
- home 服务（默认 127.0.0.1:8789）：serve ui/next/home.html + GET /api/home
  （按 ui/next/contracts.md 第 4 节契约，含 state/lcu/draft/dashboard/settings/selftest/diag）；
- 选人助手服务（127.0.0.1:8788）：serve ui/next/champ_select.html + /api/state + SSE。

打包：build_exe.cmd（PyInstaller onedir，assets/ 与 ui/ 一并打包）
自检：LoLAdvisor.exe --selftest
"""

from __future__ import annotations

import argparse
import base64
import datetime
import http.client
import http.server
import io
import json
import os
import queue
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from advisor_server import (
    FeedBuilder,
    LlmWorker,
    ModelSet,
    State,
    fetch_payload,
    load_env_file,
    mask_key,
    patch_replay_self,
    tick,
)
from rank_lookup import RankResolver, complete_payload_ranks, discover_lcu_psutil
from live_features import rank_fallback_required

APP_TITLE = "LoLAdvisor"
APP_VERSION = "v2.3.17 · 2026-11-14"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
)

DRAFT_PORT = 8788  # 选人助手内嵌服务端口
DASHBOARD_PORT = 8765  # Web 仪表盘端口（advisor_server，桌面版默认不启动）


def asset_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "assets"
    return Path(__file__).resolve().parent / "assets"


def repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return asset_root()
    return Path(__file__).resolve().parent


def config_dir() -> Path:
    """配置文件（advisor.llm.env）放在 exe 旁边；开发时放脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_assets() -> Dict[str, Path]:
    base = asset_root() if asset_root().is_dir() else repo_root()
    dataset = base / "live_dataset"
    if not dataset.is_dir():
        dataset = base.parent / "dataset" / "live_dataset"
    return {
        "model3": dataset / "live_model3",
        "model5": dataset / "live_model5",
        "events": dataset / "live_event_model",
        "opgg": base / "opgg_stats.json",
    }


# ---------------------------------------------------------------- 选人助手（内嵌服务 + 独立窗口）
_DRAFT_SERVER: Optional[threading.Thread] = None


def start_draft_server(port: int = 8788) -> None:
    """在进程内起选人助手服务（LCU WebSocket 驱动），返回后页面即可访问。"""
    global _DRAFT_SERVER
    if _DRAFT_SERVER is not None and _DRAFT_SERVER.is_alive():
        return

    def run() -> None:
        try:
            from champ_select_server import DraftModel, DraftService, Handler, Server

            model = DraftModel()
            service = DraftService(model, demo=False)
            server = Server(("127.0.0.1", port), Handler)
            server.service = service  # type: ignore[attr-defined]
            print(f"选人助手内嵌服务: http://127.0.0.1:{port}", flush=True)
            server.serve_forever()
        except OSError:
            # 端口被占用：已有实例在跑，直接复用
            print("选人助手服务已在运行（端口复用）", flush=True)
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()

    _DRAFT_SERVER = threading.Thread(target=run, daemon=True)
    _DRAFT_SERVER.start()


def open_home_window(url: str) -> None:
    """打开主窗口：优先 Edge 应用模式（无地址栏的独立窗口），失败退回默认浏览器。"""
    for edge in EDGE_CANDIDATES:
        if os.path.isfile(edge):
            try:
                subprocess.Popen(
                    [edge, f"--app={url}", "--window-size=1180,760", "--window-position=80,60"],
                    close_fds=True,
                )
                return
            except Exception:  # noqa: BLE001
                continue
    webbrowser.open(url)


def ui_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "ui"
    return Path(__file__).resolve().parent / "ui"


UI_HOME_HTML = ui_root() / "next" / "home.html"
UI_APP_HTML = ui_root() / "next" / "app.html"
UI_APP_JS = ui_root() / "next" / "app.js"
UI_SHELL_HTML = ui_root() / "next" / "shell.html"
UI_SHELL_JS = ui_root() / "next" / "shell.js"
UI_CHAMP_HTML = ui_root() / "next" / "champ_select.html"
UI_DASH_HTML = ui_root() / "next" / "dashboard.html"
UI_DESIGN_CSS = ui_root() / "design-system.css"
UI_DEMO_JS = ui_root() / "next" / "demo-data.js"

# /draft/* 反向代理目标映射（T23：一体化单页同源代理选人服务）
_DRAFT_PROXY_MAP = {
    "/draft/state": "/api/state",
    "/draft/events": "/api/events",
}

# ---------------------------------------------------------------- 主窗口 home 服务（/api/home 契约）
_SELFTEST_CACHE: Dict[str, Any] = {"passed": None, "at": None, "lines": []}
_LCU_CACHE: Dict[str, Any] = {"at": 0.0, "info": {"connected": False, "port": None}}
_SERVICE_PROBE_CACHE: Dict[int, Dict[str, Any]] = {}
_DRAFT_RANK_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_PROFILE_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_SOLO_RANK_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_PROFILE_ICON_CACHE: Dict[int, Tuple[bytes, str]] = {}
_CHAMPIONS_CACHE: Dict[str, Any] = {"at": 0.0, "data": {}}
_HOME_CONTEXT: Dict[str, Any] = {}

# ---------------------------------------------------------------- 打野雷达（T54）
_JUNGLE_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_JUNGLE_STATE: Dict[str, Any] = {
    "stop": False,
    "tracker": None,
    "champ_key": None,
    "champ_id": 0,
    "frame": None,
    "frame_at": 0.0,
    "error": None,
    "last_zone": None,
}
_JUNGLE_LOCK = threading.Lock()
_ENEMY_JUNGLER_CACHE: Dict[str, Any] = {"at": 0.0, "key": None, "champ_id": 0}


def _enemy_jungler() -> Tuple[Optional[str], int]:
    """从选人服务 /api/state 的 theirTeam 取 role=JUNGLE 的 champKey/champId（5s 缓存）。"""
    now = time.time()
    if now - _ENEMY_JUNGLER_CACHE["at"] < 5.0:
        return _ENEMY_JUNGLER_CACHE["key"], _ENEMY_JUNGLER_CACHE["champ_id"]
    key: Optional[str] = None
    champ_id = 0
    try:
        connection = http.client.HTTPConnection("127.0.0.1", DRAFT_PORT, timeout=1.5)
        connection.request("GET", "/api/state")
        response = connection.getresponse()
        if response.status == 200:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            for row in payload.get("theirTeam") or []:
                if row.get("role") == "JUNGLE" and row.get("champKey"):
                    key = str(row["champKey"])
                    champ_id = int(row.get("champId") or 0)
                    break
        connection.close()
    except Exception:  # noqa: BLE001
        key, champ_id = None, 0
    _ENEMY_JUNGLER_CACHE.update(at=now, key=key, champ_id=champ_id)
    return key, champ_id


def _jungle_loop() -> None:
    """T54：1fps 屏幕采集 + 敌方打野追踪线程（优雅降级，绝不抛到主线程）。

    region 可用环境变量 LOLADVISOR_CAPTURE_REGION="left,top,width,height" 配置；
    mss 缺失/全屏独占/region 越界 → captureUnavailable + 原因，接口照常返回。
    """
    import jungler_tracker as jt
    import screen_capture as sc

    region = sc.parse_region(os.environ.get("LOLADVISOR_CAPTURE_REGION") or "")
    templates: Dict[str, Any] = {}
    templates_at = 0.0
    while not _JUNGLE_STATE["stop"]:
        try:
            frame = sc.capture_game_region(region=region)
            state = _HOME_CONTEXT.get("state")
            game_time = float(state.game_time) if state is not None else 0.0
            champ_key, champ_id = _enemy_jungler()
            if champ_key:
                if _JUNGLE_STATE["champ_key"] != champ_key:
                    try:
                        tracker = jt.build_tracker(
                            asset_root() / "jungle_routes.json", champ_key, side=None
                        )
                        _JUNGLE_STATE["tracker"] = tracker
                        _JUNGLE_STATE["champ_key"] = champ_key
                        _JUNGLE_STATE["champ_id"] = champ_id
                        templates = {}
                        templates_at = 0.0
                    except Exception as error:  # noqa: BLE001
                        _JUNGLE_STATE["error"] = f"开野路线加载失败: {error}"
                if champ_id and time.time() - templates_at > 300.0:
                    found = discover_lcu_psutil()
                    if found:
                        try:
                            loaded, _ = sc.fetch_icon_templates(found[0], found[1], [champ_id])
                            if loaded:
                                templates = loaded
                        except Exception:  # noqa: BLE001
                            pass
                    templates_at = time.time()
                if templates:
                    results = sc.detect_minimap_enemies(
                        frame, templates, region or (1440, 810, 480, 270)
                    )
                    entry = results.get(str(champ_id))
                    if entry and entry.get("visible"):
                        tracker = _JUNGLE_STATE.get("tracker")
                        if tracker is not None:
                            tracker.observe(str(entry.get("quadrant") or ""), game_time)
                            _JUNGLE_STATE["last_zone"] = entry.get("quadrant")
            with _JUNGLE_LOCK:
                _JUNGLE_STATE["frame"] = frame
                _JUNGLE_STATE["frame_at"] = time.time()
                _JUNGLE_STATE["error"] = None
        except Exception as error:  # noqa: BLE001
            with _JUNGLE_LOCK:
                _JUNGLE_STATE["error"] = str(error)
        time.sleep(1.0)


_CAMP_CN = {
    "blue": "蓝BUFF", "red": "红BUFF", "wolves": "三狼",
    "raptors": "锋喙鸟", "gromp": "蛤蟆", "krugs": "石头人",
}
_SIDE_CN = {"blue": "蓝方", "red": "红方"}
_LANE_CN = {"top": "上路", "mid": "中路", "bot": "下路"}
# 小地图象限（screen_capture.detect_minimap_enemies 的 quadrant）：只做方位描述，不臆测成野区
_QUADRANT_CN = {
    "TOP_LEFT": "左上区域",
    "TOP_RIGHT": "右上区域",
    "BOTTOM_LEFT": "左下区域",
    "BOTTOM_RIGHT": "右下区域",
}


def _zone_cn(zone: Any) -> str:
    """区域键 `<camp>_<side>` → 中文（未知键原样返回，不抛异常）。"""
    key = str(zone or "").strip()
    if not key:
        return ""
    if key in _LANE_CN:
        return _LANE_CN[key]
    quadrant = _QUADRANT_CN.get(key.upper())
    if quadrant:
        return quadrant
    parts = key.rsplit("_", 1)
    if len(parts) == 2:
        camp_cn = _CAMP_CN.get(parts[0].lower())
        side_cn = _SIDE_CN.get(parts[1].lower())
        if camp_cn and side_cn:
            return f"{camp_cn}（{side_cn}）"
    return key


def _lane_cn(lane: Any) -> Optional[str]:
    """分路 top/mid/bot → 中文；None 保持 None，未知原样返回。"""
    if lane is None:
        return None
    return _LANE_CN.get(str(lane), str(lane))


def _jungle_route_nodes(
    tracker: Any, prediction: Dict[str, Any], game_time: float
) -> List[Dict[str, Any]]:
    """完整开野路线节点（优先用 tracker 已算好的路线 steps；无数据返回 []）。

    节点字段：time/done/current（兼容旧契约）+ camp（机器键）+ campCn（中文）。
    """
    steps: List[Any] = []
    hypothesis = prediction.get("routeHypothesis")
    try:
        routes = [r for r in (getattr(tracker, "routes", None) or []) if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        routes = []
    for route in routes:
        try:
            if hypothesis and str(route.get("id") or "") == str(hypothesis):
                steps = list(route.get("steps") or [])
                break
        except Exception:  # noqa: BLE001
            continue
    if not steps and routes:
        try:
            steps = list(routes[0].get("steps") or [])
        except Exception:  # noqa: BLE001
            steps = []
    try:
        scale = float((getattr(tracker, "config", None) or {}).get("time_scale") or 1.0)
    except Exception:  # noqa: BLE001
        scale = 1.0
    nodes: List[Dict[str, Any]] = []
    for step in steps[:6]:
        try:
            zone = str(step[0] or "")
            step_time = float(step[1]) * scale
        except Exception:  # noqa: BLE001
            continue
        if not zone:
            continue
        nodes.append(
            {
                "time": round(step_time, 0),
                "camp": zone,
                "campCn": _zone_cn(zone),
                "done": step_time <= game_time,
                "current": False,
            }
        )
    for node in nodes:
        if not node["done"]:
            node["current"] = True
            break
    return nodes


def _jungler_payload() -> Dict[str, Any]:
    now = time.time()
    if now - _JUNGLE_CACHE["at"] < 1.0:
        return _JUNGLE_CACHE["data"]
    with _JUNGLE_LOCK:
        tracker = _JUNGLE_STATE.get("tracker")
        champ_key = _JUNGLE_STATE.get("champ_key")
        champ_id = _JUNGLE_STATE.get("champ_id") or 0
        error = _JUNGLE_STATE.get("error")
        frame_at = _JUNGLE_STATE.get("frame_at") or 0.0
    state = _HOME_CONTEXT.get("state")
    game_time = float(state.game_time) if state is not None else 0.0
    prediction = tracker.predict(game_time) if tracker is not None else None
    # T58：前端契约形状（app.js renderJungler 按这些键渲染）
    if prediction is not None:
        never_seen = bool(prediction.get("neverSeen"))
        last_seen = prediction.get("lastSeen")
        if never_seen:
            state_label = "unseen"
        elif last_seen and float(last_seen.get("secondsAgo") or 0) <= 30.0:
            state_label = "spotted"
        else:
            state_label = "tracking"
        last_seen_payload = None
        if last_seen:
            last_seen_zone = last_seen.get("zone")
            last_seen_payload = {
                "zone": last_seen_zone,
                "zoneCn": _zone_cn(last_seen_zone),
                "ago": round(float(last_seen.get("secondsAgo") or 0), 1),
                "secondsAgo": round(float(last_seen.get("secondsAgo") or 0), 1),
            }
        eta = float(prediction.get("etaSeconds") or 0)
        predicted_zone = prediction.get("predictedNextZone") or prediction.get("currentZone")
        predicted_payload = {
            "zone": predicted_zone,
            "zoneCn": _zone_cn(predicted_zone),
            "eta": round(eta, 1),
            "etaSeconds": round(eta, 1),
            "confidence": prediction.get("confidence") or 0.0,
            "timeBasis": prediction.get("timeBasis"),
        }
        # D：完整开野路线（通常 5-6 个营地节点）；拿不到路线退回「当前 + 下一」。
        route = _jungle_route_nodes(tracker, prediction, game_time)
        current_zone = prediction.get("currentZone")
        next_zone = prediction.get("predictedNextZone")
        if not route:
            if current_zone:
                route.append(
                    {
                        "time": round(game_time, 0),
                        "camp": current_zone,
                        "campCn": _zone_cn(current_zone),
                        "done": True,
                        "current": False,
                    }
                )
            if next_zone:
                route.append(
                    {
                        "time": round(game_time + eta, 0),
                        "camp": next_zone,
                        "campCn": _zone_cn(next_zone),
                        "done": False,
                        "current": True,
                    }
                )
    else:
        state_label = "unseen"
        last_seen_payload = None
        predicted_payload = None
        route = []
    data: Dict[str, Any] = {
        "ok": tracker is not None,
        "state": state_label,
        "capture": {
            "available": not bool(error),
            "url": "/capture/frame.jpg",
            "fps": 1,
            "reason": error or None,
        },
        "jungler": {
            "champ": champ_key or "",
            "champCn": _champion_display(champ_id) if champ_id else "",
            "champId": champ_id,
            "name": _champion_display(champ_id) if champ_id else "",
        },
        "threatLane": prediction.get("threatLane") if prediction else None,
        "threatLaneCn": _lane_cn(prediction.get("threatLane")) if prediction else None,
        "lastSeen": last_seen_payload,
        "predicted": predicted_payload,
        "route": route,
        "note": "统计推测：基于小地图现身与开野路线数据的概率推断，非实时视野",
        # 向后兼容旧字段
        "champKey": champ_key,
        "captureUnavailable": bool(error),
        "captureReason": error or None,
        "lastFrameAt": round(frame_at, 1) if frame_at else None,
        "prediction": prediction,
        "gameTime": round(game_time, 1),
    }
    _JUNGLE_CACHE.update(at=now, data=data)
    return data


def _capture_frame_jpeg() -> Optional[bytes]:
    with _JUNGLE_LOCK:
        frame = _JUNGLE_STATE.get("frame")
    if frame is None:
        return None
    try:
        from PIL import Image

        buffer = io.BytesIO()
        Image.fromarray(frame).save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001
        return None

_TIER_CN = {
    "IRON": "黑铁", "BRONZE": "青铜", "SILVER": "白银", "GOLD": "黄金",
    "PLATINUM": "铂金", "EMERALD": "翡翠", "DIAMOND": "钻石",
    "MASTER": "大师", "GRANDMASTER": "宗师", "CHALLENGER": "王者",
}
_UNRANKED_TIERS = {"", "NONE", "UNRANKED", "NA"}

# T62：战绩检查用的队列 / 位置中文名
_QUEUE_CN = {
    420: "单双排",
    440: "灵活排位",
    400: "匹配",
    430: "匹配",
    490: "快速匹配",
    450: "大乱斗",
    900: "无限火力",
    1700: "斗魂竞技场",
}
_POSITION_CN = {
    "TOP": "上路",
    "JUNGLE": "打野",
    "MIDDLE": "中路",
    "BOTTOM": "下路",
    "UTILITY": "辅助",
}


def _match_summary(matches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """最近 N 局汇总：胜负、胜率、KDA、场均补刀/时长、连胜/连败。"""
    games = [m for m in matches if isinstance(m, dict)]
    total = len(games)
    if not total:
        return {
            "games": 0, "wins": 0, "losses": 0, "winRate": 0.0,
            "kdaRatio": 0.0, "avgCsPerMin": 0.0, "avgDurationMin": 0.0,
            "streak": 0, "streakKind": "",
        }
    wins = sum(1 for m in games if m.get("win"))
    kills = sum(int(m.get("kills") or 0) for m in games)
    deaths = sum(int(m.get("deaths") or 0) for m in games)
    assists = sum(int(m.get("assists") or 0) for m in games)
    minutes = sum(float(m.get("gameTime") or 0) for m in games) / 60.0
    cs = sum(int(m.get("cs") or 0) for m in games)
    streak_kind = "win" if games[0].get("win") else "loss"
    streak = 0
    for match in games:
        if bool(match.get("win")) == (streak_kind == "win"):
            streak += 1
        else:
            break
    return {
        "games": total,
        "wins": wins,
        "losses": total - wins,
        "winRate": round(wins * 100.0 / total, 1),
        "kdaRatio": round((kills + assists) / max(1, deaths), 2),
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "avgCsPerMin": round(cs / minutes, 1) if minutes > 0 else 0.0,
        "avgDurationMin": round(minutes / total, 1),
        "streak": streak,
        "streakKind": streak_kind,
    }


def _ranked_tier(tier: Any) -> Optional[str]:
    """LCU 有时用 NONE/UNRANKED 表示未定级，统一归一成 None（不显示「已解析 0」）。"""
    value = str(tier or "").strip()
    if not value or value.upper() in _UNRANKED_TIERS:
        return None
    return value


def _lcu_request_bytes(path: str, timeout: float = 4.0) -> Optional[bytes]:
    """带认证的 LCU GET（端口/token 从 psutil/lockfile 发现，失败返回 None）。"""
    found = discover_lcu_psutil()
    if not found:
        return None
    port, token = found
    try:
        connection = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=timeout, context=ssl._create_unverified_context()
        )
        connection.request(
            "GET", path,
            headers={"Authorization": "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()},
        )
        response = connection.getresponse()
        body = response.read() if response.status == 200 else None
        connection.close()
        return body
    except Exception:  # noqa: BLE001
        return None


def _lcu_json(path: str) -> Optional[Any]:
    raw = _lcu_request_bytes(path)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def _champion_display(champ_id: int) -> str:
    """champions.json 的 id→中文名映射（10 分钟缓存）。"""
    now = time.time()
    if now - _CHAMPIONS_CACHE["at"] > 600:
        try:
            path = asset_root() / "champions.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            mapping = {
                int(cid): item.get("name") or item.get("alias") or ""
                for cid, item in data.items()
            }
            _CHAMPIONS_CACHE.update(at=now, data=mapping)
        except Exception:  # noqa: BLE001
            pass
    return _CHAMPIONS_CACHE["data"].get(champ_id, "")


def _solo_rank_snapshot() -> Optional[Dict[str, Any]]:
    """当前账号段位快照（单双优先、灵活回退；60s 缓存）。

    与 /api/profile 同源（/lol-ranked/v1/current-ranked-stats），给 /api/home
    在「未开局 / 从未查询过」时提供 already-known 的本人段位，避免
    rankStatus 永远停在 resolving（P6）。未连接、未定级或取不到 → None。
    """
    now = time.time()
    if now - _SOLO_RANK_CACHE["at"] < 60.0:
        return _SOLO_RANK_CACHE["data"]
    data: Optional[Dict[str, Any]] = None
    ranked = _lcu_json("/lol-ranked/v1/current-ranked-stats") or {}
    queues = {
        q.get("queueType"): q
        for q in (ranked.get("queues") or [])
        if isinstance(q, dict)
    }
    entry = queues.get("RANKED_SOLO_5x5") or queues.get("RANKED_FLEX_SR") or {}
    tier = _ranked_tier(entry.get("tier"))
    if tier:
        score = 0.0
        try:
            from fetch_player_ranks import rank_score

            score = float(
                rank_score(
                    str(tier),
                    entry.get("division") or "NA",
                    int(entry.get("leaguePoints") or 0),
                )
            )
        except Exception:  # noqa: BLE001
            score = 0.0
        data = {
            "tier": tier,
            "tierCn": _TIER_CN.get(str(tier), ""),
            "division": entry.get("division") or "",
            "lp": int(entry.get("leaguePoints") or 0),
            "queueType": entry.get("queueType") or "RANKED_SOLO_5x5",
            "score": score,
        }
    _SOLO_RANK_CACHE.update(at=now, data=data)
    return data


def _profile_payload() -> Dict[str, Any]:
    """T34：个人主页数据（召唤师 + 单双/灵活段位 + 最近 10 局），10s 缓存。

    无客户端/未登录 → {ok:false, message, detail}（contracts.md 第 9 节）。
    """
    now = time.time()
    if now - _PROFILE_CACHE["at"] < 10.0:
        return _PROFILE_CACHE["data"]
    data: Dict[str, Any] = {
        "ok": False,
        "message": "客户端未连接",
        "detail": "请启动并登录英雄联盟客户端",
    }
    summoner = _lcu_json("/lol-summoner/v1/current-summoner") or {}
    if summoner.get("summonerId") or summoner.get("displayName") or summoner.get("gameName"):
        name = summoner.get("gameName") or summoner.get("displayName") or ""
        tag = summoner.get("tagLine") or ""
        if name and tag:
            name = f"{name}#{tag}"
        profile: Dict[str, Any] = {
            "ok": True,
            "summoner": {
                "name": name,
                "level": int(summoner.get("summonerLevel") or 0),
                "iconId": int(summoner.get("profileIconId") or 0),
            },
            "solo": None,
            "flex": None,
            "matches": [],
        }
        puuid = summoner.get("puuid")
        ranked = _lcu_json("/lol-ranked/v1/current-ranked-stats") or {}
        queues = {q.get("queueType"): q for q in (ranked.get("queues") or []) if isinstance(q, dict)}

        def map_queue(entry: Optional[dict]) -> Optional[Dict[str, Any]]:
            # 队列条目存在但没有段位数据（tier 为空/NONE/UNRANKED）→ 未定级
            if not entry:
                return None
            tier = _ranked_tier(entry.get("tier"))
            if not tier:
                return None
            return {
                "tier": tier,
                "tierCn": _TIER_CN.get(str(tier), ""),
                "division": entry.get("division") or "",
                "lp": int(entry.get("leaguePoints") or 0),
                "wins": int(entry.get("wins") or 0),
                "losses": int(entry.get("losses") or 0),
                "unranked": False,
            }

        def unranked_entry() -> Dict[str, Any]:
            return {"tier": None, "tierCn": "", "division": "", "lp": 0,
                    "wins": 0, "losses": 0, "unranked": True}

        profile["solo"] = map_queue(queues.get("RANKED_SOLO_5x5")) or unranked_entry()
        profile["flex"] = map_queue(queues.get("RANKED_FLEX_SR")) or unranked_entry()

        history = _lcu_json("/lol-match-history/v1/products/lol/current-summoner/matches") or {}
        games = (history.get("games") or {}).get("games") or []
        matches: List[Dict[str, Any]] = []
        summoner_id = summoner.get("summonerId")
        for game in games[:10]:
            if not isinstance(game, dict):
                continue
            raw_participants = [
                p for p in (game.get("participants") or []) if isinstance(p, dict)
            ]
            participant = None
            # LCU 该接口的 participants 只有本人 1 条、且不带 puuid/summonerId：
            # 身份在 participantIdentities[].player，先用 puuid 对齐 participantId。
            my_participant_id = None
            for identity in game.get("participantIdentities") or []:
                if not isinstance(identity, dict):
                    continue
                player = identity.get("player") or {}
                if not isinstance(player, dict):
                    continue
                if (puuid and player.get("puuid") == puuid) or (
                    summoner_id and player.get("summonerId") == summoner_id
                ):
                    my_participant_id = identity.get("participantId")
                    break
            if my_participant_id is not None:
                # LCU 里同一字段可能是 int 也可能被序列化成 str，比较前统一成字符串
                wanted = str(my_participant_id)
                participant = next(
                    (
                        p
                        for p in raw_participants
                        if str(p.get("participantId")) == wanted
                    ),
                    None,
                )
            if participant is None:
                participant = next(
                    (
                        p
                        for p in raw_participants
                        if (puuid and p.get("puuid") == puuid)
                        or (summoner_id and p.get("summonerId") == summoner_id)
                    ),
                    None,
                )
            if participant is None and len(raw_participants) == 1:
                participant = raw_participants[0]
            stats = (participant or {}).get("stats") or {}
            champ_id = int(
                (participant or {}).get("championId") or game.get("championId") or 0
            )
            played_at = ""
            try:
                played_at = datetime.datetime.fromtimestamp(
                    int(game.get("gameCreation") or 0) / 1000.0
                ).isoformat()
            except Exception:  # noqa: BLE001
                played_at = ""
            queue_id = int(game.get("queueId") or 0)
            duration = int(game.get("gameDuration") or 0)
            minions = int(stats.get("totalMinionsKilled") or 0) + int(
                stats.get("neutralMinionsKilled") or 0
            )
            position = str(
                (participant or {}).get("teamPosition")
                or (participant or {}).get("individualPosition")
                or ""
            )
            matches.append({
                "champId": champ_id,
                "champ": _champion_display(champ_id),
                "win": bool(stats.get("win")),
                "kills": int(stats.get("kills") or 0),
                "deaths": int(stats.get("deaths") or 0),
                "assists": int(stats.get("assists") or 0),
                "gameTime": duration,
                "playedAt": played_at,
                # T62：战绩检查补全（队列 / 补刀 / 经济 / 伤害 / 视野 / 位置 / 重开）
                "queueId": queue_id,
                "queueCn": _QUEUE_CN.get(queue_id, "其他"),
                "cs": minions,
                "gold": int(stats.get("goldEarned") or 0),
                "damage": int(stats.get("totalDamageDealtToChampions") or 0),
                "vision": int(stats.get("visionScore") or 0),
                "positionCn": _POSITION_CN.get(position.upper(), ""),
                "remake": bool(duration and duration < 300),
            })
        profile["matches"] = matches
        profile["matchSummary"] = _match_summary(matches)
        data = profile
    _PROFILE_CACHE.update(at=now, data=data)
    return data


def _draft_rank_bridge() -> Optional[Dict[str, Any]]:
    """BP/预选阶段从选人服务（8788）桥接段位（T21）。

    advisor 的对局进行中链路在 BP 阶段没数据，而选人服务 /api/state 的
    factors.myRank/theirRank 已有双方段位。5s 缓存 + 1.5s 超时，
    选人服务未运行/不在 BP/出错时返回 None，不影响 /api/home 整体。
    """
    cache = _DRAFT_RANK_CACHE
    now = time.time()
    if now - cache["at"] < 5.0:
        return cache["data"]
    data: Optional[Dict[str, Any]] = None
    if draft_running():
        try:
            connection = http.client.HTTPConnection("127.0.0.1", DRAFT_PORT, timeout=1.5)
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            if response.status == 200:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
                factors = payload.get("factors") or {}
                my_rank = factors.get("myRank")
                their_rank = factors.get("theirRank")
                if payload.get("ok") and my_rank is not None and their_rank is not None:
                    data = {
                        "rank": {
                            "own": None,
                            "team": float(my_rank),
                            "enemy": float(their_rank),
                            "diff": float(my_rank) - float(their_rank),
                            "vsTeam": None,
                        },
                        "rankStatus": {
                            "state": "ok",
                            "source": "draft",
                            "note": "选人阶段段位",
                        },
                    }
            connection.close()
        except Exception:  # noqa: BLE001
            data = None
    cache.update(at=now, data=data)
    return data


def _service_alive(port: int) -> bool:
    """探测本机 HTTP 服务是否在跑（10s 缓存；任何 HTTP 响应都算在跑）。"""
    cache = _SERVICE_PROBE_CACHE.setdefault(port, {"at": 0.0, "alive": False})
    now = time.time()
    if now - cache["at"] < 10.0:
        return bool(cache["alive"])
    alive = False
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        connection.request("GET", "/api/state")
        response = connection.getresponse()
        alive = response.status < 500
        connection.close()
    except Exception:  # noqa: BLE001
        alive = False
    cache.update(at=now, alive=alive)
    return alive


def draft_running() -> bool:
    """选人助手服务是否可用：进程内线程存活，或 DRAFT_PORT 有外部实例。"""
    if _DRAFT_SERVER is not None and _DRAFT_SERVER.is_alive():
        return True
    return _service_alive(DRAFT_PORT)


def _inject_head(html: str, script: str) -> str:
    marker = "<head>"
    if marker in html:
        return html.replace(marker, marker + script, 1)
    return marker + script + "</head>" + html


def _version_asset_refs(html: str) -> str:
    """T43：静态资源引用加版本参数——URL 随版本变化，浏览器必取新文件，
    彻底击穿 Edge 对旧 app.js/design-system.css 的缓存。"""
    version = urllib.parse.quote(APP_VERSION)
    html = html.replace('href="../design-system.css"', f'href="../design-system.css?v={version}"')
    html = html.replace('src="demo-data.js"', f'src="demo-data.js?v={version}"')
    html = html.replace('src="app.js"', f'src="app.js?v={version}"')
    html = html.replace('src="shell.js"', f'src="shell.js?v={version}"')
    return html


def _app_html_bytes() -> bytes:
    """serve 一体化单页 app.html（资源引用带版本参数）。"""
    try:
        html = UI_APP_HTML.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return "<html><body><h1>app.html 缺失</h1></body></html>".encode("utf-8")
    return _version_asset_refs(html).encode("utf-8")


def _shell_html_bytes() -> bytes:
    """serve shell.html 并在 <head> 注入 window.LOL_SHELL（contracts.md 第 6 节）。"""
    try:
        html = UI_SHELL_HTML.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return "<html><body><h1>shell.html 缺失</h1></body></html>".encode("utf-8")
    cfg = {
        "version": APP_VERSION,
        "defaultView": "home",
        "home": {"url": "home.html"},
        "draft": {"url": f"http://127.0.0.1:{DRAFT_PORT}", "running": draft_running()},
        "dashboard": {
            "url": f"http://127.0.0.1:{DASHBOARD_PORT}",
            "running": _service_alive(DASHBOARD_PORT),
        },
    }
    script = "<script>window.LOL_SHELL = " + json.dumps(cfg, ensure_ascii=False) + ";</script>"
    return _inject_head(_version_asset_refs(html), script).encode("utf-8")


def _home_html_bytes() -> bytes:
    """serve home.html 并注入 window.lolAdvisor 桥：入口改为 shell 视图切换，
    不再 window.open 新窗口（getHome 直接走 /api/home）。"""
    try:
        html = UI_HOME_HTML.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return "<html><body><h1>home.html 缺失</h1></body></html>".encode("utf-8")
    bridge = (
        "<script>window.lolAdvisor = {"
        "getHome: function(){ return fetch('/api/home', {cache:'no-store'}).then(function(r){ return r.json(); }); },"
        "openDraft: function(){ try { window.parent.location.assign('/?view=draft'); }"
        f" catch(e) {{ window.open('http://127.0.0.1:{DRAFT_PORT}', '_blank'); }} }},"
        "openDashboard: function(){ try { window.parent.location.assign('/?view=dashboard'); }"
        f" catch(e) {{ window.open('http://127.0.0.1:{DASHBOARD_PORT}', '_blank'); }} }}"
        "};</script>"
    )
    return _inject_head(_version_asset_refs(html), bridge).encode("utf-8")


def _overlay_payload(state: Any) -> Optional[Dict[str, Any]]:
    """左下角悬浮窗数据（T59）：对局中返回快照，未开局/已结束返回 None（窗口隐藏）。

    只读 State，不加锁太久；趋势用 60 秒前的胜率做对比（pp/分）。
    """
    if state is None:
        return None
    try:
        with state.lock:
            if state.waiting or state.ended:
                return None
            win_prob = state.win_prob
            game_time = float(state.game_time or 0.0)
            clock = state.clock or "--:--"
            model = state.model_name or ""
            source = state.source or "live"
            advice = (state.rule_tips[0] if state.rule_tips else "") or ""
            history_prob = list(state.history.get("winProb") or [])
            history_t = list(state.history.get("t") or [])
            event_probs = dict(state.event_probs or {})
    except Exception:  # noqa: BLE001
        return None
    trend = None
    if win_prob is not None and len(history_prob) >= 2:
        base = history_prob[0]
        for index, moment in enumerate(history_t):
            if moment <= game_time - 60.0:
                base = history_prob[index]
            else:
                break
        try:
            trend = (float(win_prob) - float(base)) * 100.0
        except (TypeError, ValueError):
            trend = None
    prediction = None
    if event_probs:
        label_cn = {
            "own_kill_30s": "你 30 秒内击杀",
            "enemy_kill_30s": "你 30 秒内被击杀",
            "own_gank_60s": "你方 60 秒内 gank",
            "enemy_gank_60s": "敌方 60 秒内 gank",
        }
        key, value = max(event_probs.items(), key=lambda kv: float(kv[1] or 0.0))
        try:
            prob_value = float(value)
        except (TypeError, ValueError):
            prob_value = None
        if prob_value is not None:
            prediction = {"label": label_cn.get(key, key), "prob": prob_value}
    return {
        "winProb": None if win_prob is None else float(win_prob),
        "trendPp": trend,
        "clock": clock,
        "model": model,
        "source": source,
        "advice": advice,
        "prediction": prediction,
    }


def _lcu_info() -> Dict[str, Any]:
    """LCU 连接状态（带 10s 缓存，避免 /api/home 每 2 秒扫一遍进程）。

    含段位解析器 resolver.status() 的诊断快照（T18：自愈重试 + 状态透传）。
    """
    now = time.time()
    if now - _LCU_CACHE["at"] < 10.0:
        return _LCU_CACHE["info"]
    poller = _HOME_CONTEXT.get("poller")
    resolver: Optional[RankResolver] = poller.resolver if poller is not None else None
    found = discover_lcu_psutil()
    info: Dict[str, Any] = {
        "connected": bool(found) or resolver is not None,
        "port": (found[0] if found else None) or (resolver.port if resolver else None),
    }
    if resolver is not None:
        try:
            info.update(resolver.status())
        except Exception:  # noqa: BLE001
            pass
    _LCU_CACHE.update(at=now, info=info)
    return info


# ---------------------------------------------------------------- D3 模型监控（/api/home）
# modelInfo：模型名/年龄/patch 元数据；drift：最近一次特征相对训练均值的偏移。
# 两者都是「锦上添花」字段：任何异常都静默为 None，绝不影响 /api/home 本体。
_MODEL_INFO_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
MODEL_INFO_TTL = 60.0


def _monitored_model_dir() -> Optional[Path]:
    """要体检的模型目录：优先 model5（56 特征），退到 model3。"""
    try:
        assets = resolve_assets()
        for key in ("model5", "model3"):
            directory = assets.get(key)
            if directory is not None and Path(directory).is_dir():
                return Path(directory)
    except Exception:  # noqa: BLE001
        return None
    return None


def _model_info_field() -> Optional[Dict[str, Any]]:
    """D3：model_monitor.model_info（60s 缓存；缺模块/缺目录/出错 → None）。"""
    now = time.time()
    if now - _MODEL_INFO_CACHE["at"] < MODEL_INFO_TTL:
        return _MODEL_INFO_CACHE["data"]
    data: Optional[Dict[str, Any]] = None
    try:
        from model_monitor import model_info

        directory = _monitored_model_dir()
        if directory is not None:
            data = model_info(directory)
    except Exception:  # noqa: BLE001
        data = None
    _MODEL_INFO_CACHE.update(at=now, data=data)
    return data


def _drift_field() -> Optional[Dict[str, Any]]:
    """D3：state.features（最近一次 update）相对 TRAIN_MEANS 的偏移；无特征 → None。

    只在锁内做一次浅拷贝，计算在锁外；任何异常都返回 None。
    """
    try:
        from model_monitor import feature_drift, model_stats

        state: Optional[State] = _HOME_CONTEXT.get("state")
        if state is None:
            return None
        with state.lock:
            features = dict(state.features or {})
        if not features:
            return None
        directory = _monitored_model_dir()
        if directory is None:
            return None
        means, scales = model_stats(directory)
        if not means:  # 没有训练基线就不硬编一个假的 ok
            return None
        return feature_drift(features, means, scales)
    except Exception:  # noqa: BLE001
        return None


def _home_payload() -> Dict[str, Any]:
    state: Optional[State] = _HOME_CONTEXT.get("state")
    args = _HOME_CONTEXT.get("args")
    state_json = state.as_json() if state is not None else {"ok": False}
    # T21：BP/预选阶段 advisor 的对局链路还没数据，从选人服务桥接段位，
    # 让主页「赛前实力」卡与选人页一致地显示段位。
    if not state_json.get("rank"):
        bridge = _draft_rank_bridge()
        if bridge:
            state_json["rank"] = bridge["rank"]
            state_json["rankStatus"] = bridge["rankStatus"]
            state_json["rankError"] = None
    # P6：未开局时 as_json 的 rankStatus 恒为 resolving（段位只在打野链路里查）。
    # 这里用 LCU 段位快照给出「本人段位已解析」，不伪造 team/enemy 均值。
    if not state_json.get("rank"):
        snapshot = _solo_rank_snapshot()
        if snapshot:
            state_json["rank"] = {"own": snapshot["score"]}
            status = dict(state_json.get("rankStatus") or {})
            status.update(
                {
                    "state": "ok",
                    "source": "lcu-ranked",
                    "tier": snapshot["tier"],
                    "tierCn": snapshot["tierCn"],
                    "division": snapshot["division"],
                    "lp": snapshot["lp"],
                    "queueType": snapshot.get("queueType"),
                    "lastError": None,
                    "lastNote": None,
                }
            )
            state_json["rankStatus"] = status
            state_json["rankError"] = None
    return {
        "ok": True,
        "appTitle": APP_TITLE,
        "version": APP_VERSION,
        "lcu": _lcu_info(),
        "draft": {
            "available": True,
            "running": draft_running(),
            "url": f"http://127.0.0.1:{DRAFT_PORT}",
        },
        "dashboard": {
            "available": True,
            "running": _service_alive(DASHBOARD_PORT),
            "url": f"http://127.0.0.1:{DASHBOARD_PORT}",
        },
        "settings": {
            "interval": float(getattr(args, "interval", 5.0) or 5.0),
            "llm": not bool(getattr(args, "no_llm", False)),
            "llmInterval": float(getattr(args, "llm_interval", 20.0) or 20.0),
            "ranks": not bool(getattr(args, "no_ranks", False)),
            "autoOpenDraft": False,
            "rankCache": str(getattr(args, "rank_cache", "") or ""),
        },
        "selftest": _SELFTEST_CACHE,
        "diag": {"at": _HOME_CONTEXT.get("diag_at")},
        # D3：补丁漂移 + 特征偏移（失败为 null，不影响其它字段）
        "modelInfo": _model_info_field(),
        "drift": _drift_field(),
        "state": state_json,
    }


class HomeHandler(http.server.BaseHTTPRequestHandler):
    server_version = "LoLAdvisorHome/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/app"):
            # T23：主窗口 = 一体化单页 app.html（不再 shell 三视图）；T43：资源引用带版本参数
            self._send(_app_html_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/app.js":
            self._serve_file(UI_APP_JS, "application/javascript; charset=utf-8")
        elif parsed.path == "/home.html":
            self._send(_home_html_bytes(), "text/html; charset=utf-8")
        elif parsed.path in ("/shell", "/shell.html"):
            # shell 保留为兼容入口
            self._send(_shell_html_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/shell.js":
            self._serve_file(UI_SHELL_JS, "application/javascript; charset=utf-8")
        elif parsed.path == "/champ_select.html":
            self._serve_file(UI_CHAMP_HTML, "text/html; charset=utf-8")
        elif parsed.path == "/dashboard.html":
            self._serve_file(UI_DASH_HTML, "text/html; charset=utf-8")
        elif parsed.path == "/api/home":
            try:
                payload = json.dumps(_home_payload(), ensure_ascii=False, default=str).encode("utf-8")
                self._send(payload, "application/json; charset=utf-8", cache=False)
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                self.send_error(500)
        elif parsed.path == "/design-system.css":
            self._serve_file(UI_DESIGN_CSS, "text/css; charset=utf-8")
        elif parsed.path == "/demo-data.js":
            self._serve_file(UI_DEMO_JS, "application/javascript; charset=utf-8")
        elif parsed.path in _DRAFT_PROXY_MAP:
            # T23：一体化单页同源代理选人服务（/draft/state、/draft/events）
            if parsed.path == "/draft/events":
                self._proxy_draft_sse()
            else:
                self._proxy_draft_json(_DRAFT_PROXY_MAP[parsed.path])
        elif parsed.path == "/api/profile":
            try:
                payload = json.dumps(_profile_payload(), ensure_ascii=False, default=str).encode("utf-8")
                self._send(payload, "application/json; charset=utf-8", cache=False)
            except Exception:  # noqa: BLE001
                self.send_error(500)
        elif parsed.path == "/api/jungler":
            try:
                payload = json.dumps(_jungler_payload(), ensure_ascii=False, default=str).encode("utf-8")
                self._send(payload, "application/json; charset=utf-8", cache=False)
            except Exception:  # noqa: BLE001
                self.send_error(500)
        elif parsed.path == "/capture/frame.jpg":
            jpeg = _capture_frame_jpeg()
            if jpeg is None:
                self.send_error(404)
                return
            self._send(jpeg, "image/jpeg", cache=False)
        elif parsed.path.startswith("/icon/profile/"):
            match = re.fullmatch(r"/icon/profile/(\d+)\.png", parsed.path)
            if match:
                self._proxy_profile_icon(int(match.group(1)))
            else:
                self.send_error(404)
        elif parsed.path.startswith("/icon/"):
            # 选人板块头像：代理到选人服务（同源，无 CORS）
            self._proxy_draft_json("/" + parsed.path.lstrip("/"))
        else:
            self.send_error(404)

    def _proxy_profile_icon(self, icon_id: int) -> None:
        """T34：LCU 召唤师头像代理（/lol-game-data/assets/v1/profile-icons/<id>.jpg），失败 404。

        P2：LCU 只提供 JPEG，但路由契约是 .png；用 Pillow 转成真正的 PNG，
        并把缓存头改为 no-store（与 VERSION 的「一律 no-store」一致）。
        转码失败时回退原 JPEG 字节 + image/jpeg，保证仍可显示。
        页面每 15s 重绘头像，这里按 icon_id 缓存转码结果，避免重复解码/编码。
        """
        cached = _PROFILE_ICON_CACHE.get(icon_id)
        if cached:
            raw, content_type = cached
        else:
            raw = _lcu_request_bytes(
                f"/lol-game-data/assets/v1/profile-icons/{icon_id}.jpg", timeout=4
            )
            if not raw:
                self.send_error(404)
                return
            content_type = "image/jpeg"
            try:
                from PIL import Image

                buffer = io.BytesIO()
                with Image.open(io.BytesIO(raw)) as image:
                    image.convert("RGB").save(buffer, format="PNG")
                png = buffer.getvalue()
                if png:
                    raw = png
                    content_type = "image/png"
            except Exception:  # noqa: BLE001
                content_type = "image/jpeg"
            if len(_PROFILE_ICON_CACHE) >= 32:
                _PROFILE_ICON_CACHE.clear()
            _PROFILE_ICON_CACHE[icon_id] = (raw, content_type)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_is_head", False):
            self.wfile.write(raw)

    def _proxy_draft_json(self, target: str) -> None:
        """GET 直透代理到选人服务；失败返回 502（页面据此显示未启动空态）。"""
        try:
            connection = http.client.HTTPConnection("127.0.0.1", DRAFT_PORT, timeout=3)
            connection.request("GET", target)
            response = connection.getresponse()
            body = response.read()
            content_type = response.getheader("Content-Type") or "application/json; charset=utf-8"
            connection.close()
        except Exception:  # noqa: BLE001
            self.send_error(502)
            return
        self.send_response(response.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_is_head", False):
            self.wfile.write(body)

    def _proxy_draft_sse(self) -> None:
        """SSE 透传：连 8788 /api/events 并用 read1 逐块即时转发
        （read 会阻塞攒满缓冲；上游每 15s 有 ping 保活，20s 超时容错）。
        断线后客户端 EventSource 会自动重连/降级 4s 轮询。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if getattr(self, "_is_head", False):
            return
        try:
            connection = http.client.HTTPConnection("127.0.0.1", DRAFT_PORT, timeout=20)
            connection.request("GET", "/api/events")
            response = connection.getresponse()
            if response.status != 200:
                connection.close()
                return
            try:
                while True:
                    chunk = response.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
            connection.close()
        except Exception:  # noqa: BLE001
            pass

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            self._send(path.read_bytes(), content_type)
        except Exception:  # noqa: BLE001
            self.send_error(404)

    def _send(self, payload: bytes, content_type: str, cache: bool = False) -> None:
        # T30：所有页面/静态资源一律 no-store——Edge app 窗口会缓存旧页面
        # （max-age 曾导致「设置打不开、左侧栏缺失」的旧版页面被反复加载）。
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store" if not cache else "public, max-age=3600")
        self.end_headers()
        if not getattr(self, "_is_head", False):
            self.wfile.write(payload)

    def do_HEAD(self) -> None:  # noqa: N802
        """HEAD 与 GET 同路由但只回头发（供 curl -I 校验响应头）。"""
        self._is_head = True
        try:
            self.do_GET()
        finally:
            self._is_head = False

    def log_message(self, *args: Any) -> None:  # noqa: A003
        pass


def start_home_server(context: Dict[str, Any], port: int = 8789) -> Tuple[http.server.ThreadingHTTPServer, int]:
    """进程内 home 服务；端口被占用时自动换系统临时端口。"""
    global _HOME_CONTEXT
    _HOME_CONTEXT = context
    for attempt in (0, 1):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), HomeHandler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server, port
        except OSError:
            if attempt == 0:
                port = 0
                continue
            raise
    raise RuntimeError("无法启动 home 服务")


def demo_payload(second: int) -> Dict[str, Any]:
    """--demo 用的假对局：不开游戏也能看界面。"""
    minute = max(1, second // 60)
    players = []
    roles = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    for index, role in enumerate(roles):
        blue_kills = max(0, minute // 6 + index % 2)
        red_kills = max(0, minute // 8 - index % 2)
        players.append({
            "riotId": f"蓝色玩家{index}#1000", "summonerName": f"蓝色玩家{index}#1000",
            "team": "ORDER", "position": role, "level": min(18, 3 + minute // 3),
            "rawChampionName": f"game_character_displayname_Blue{index}",
            "scores": {"kills": blue_kills, "deaths": red_kills,
                       "assists": minute // 4, "creepScore": 20 + minute * 8},
        })
        players.append({
            "riotId": f"红色玩家{index}#2000", "summonerName": f"红色玩家{index}#2000",
            "team": "CHAOS", "position": role, "level": min(18, 2 + minute // 3),
            "rawChampionName": f"game_character_displayname_Red{index}",
            "scores": {"kills": red_kills, "deaths": blue_kills,
                       "assists": minute // 5, "creepScore": 18 + minute * 7},
        })
    events = []
    if second >= 240:
        events.append({"EventID": 1, "EventName": "ChampionKill", "EventTime": 240,
                       "KillerName": "蓝色玩家2", "VictimName": "红色玩家2"})
    if second >= 600:
        events.append({"EventID": 2, "EventName": "DragonKill", "EventTime": 600,
                       "KillerName": "蓝色玩家1", "DragonType": "Fire"})
    return {
        "activePlayer": {"summonerName": "蓝色玩家2#1000", "currentGold": 500 + minute * 60, "level": min(18, 3 + minute // 3)},
        "allPlayers": players,
        "gameData": {"gameTime": second},
        "events": {"Events": events},
    }


class Poller(threading.Thread):
    """后台轮询 2999，写入 State；UI 只读 State。"""

    def __init__(self, state: State, assets: Dict[str, Path], args: argparse.Namespace):
        super().__init__(daemon=True)
        self.state = state
        self.assets = assets
        self.args = args
        self.stop_event = threading.Event()
        self.models: Optional[ModelSet] = None
        self.model_name = ""
        self.resolver: Optional[RankResolver] = None
        self.opgg: Optional[Dict[str, Any]] = None
        self.llm: Optional[LlmWorker] = None
        self.feed = FeedBuilder()
        self.messages: "queue.Queue[str]" = queue.Queue()
        self._spectator_announced = False
        self._resolver_retry_at = 0.0  # LCU discover 失败后的冷却重试
        self._rank_retry_at = 0.0  # 段位解析空结果后的冷却重试

    # ---------------------------------------------------------------- setup
    def prepare(self) -> None:
        if not self.args.no_ranks:
            found = discover_lcu_psutil()
            if found:
                try:
                    self.resolver = RankResolver(found[0], found[1], cache_path=self.args.rank_cache)
                except Exception:
                    self.resolver = None
        model_dir = self.assets["model5"] if self.assets["model5"].is_dir() and (self.resolver or self.args.demo) else self.assets["model3"]
        self.models = ModelSet(model_dir, self.assets["events"])
        self.model_name = model_dir.name
        self.state.model_name = self.model_name

        if self.assets["opgg"].is_file():
            try:
                self.opgg = json.loads(self.assets["opgg"].read_text(encoding="utf-8"))
            except Exception:
                self.opgg = None

        if not self.args.no_llm:
            key = load_env_file(config_dir() / "advisor.llm.env").get("ADVISOR_LLM_API_KEY")
            if key:
                self.llm = LlmWorker(
                    "https://api.deepseek.com", key, "deepseek-flash", 20.0
                )
                self.llm.start()
                self.state.llm = self.llm
                self.messages.put(f"LLM: 已启用（{mask_key(key)}）")
            else:
                self.messages.put("LLM: 未配置 key，只用规则建议")

    # ---------------------------------------------------------------- loop
    def _spectator_self(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """观战/回放没有 activePlayer：挑一个视角，返回打补丁后的 payload。"""
        fragment = self.args.player or None
        if not fragment and self.resolver is not None:
            fragment = self.resolver.current_summoner()
        players = payload.get("allPlayers") or []
        if not fragment and players:
            fragment = players[0].get("riotId") or players[0].get("summonerName")
        if not fragment:
            return None
        try:
            patched, self_player = patch_replay_self(payload, fragment)
        except ValueError:
            return None
        self.state.source = "spectator"
        self.state.set_self(self_player)
        if not self._spectator_announced:
            self._spectator_announced = True
            self.messages.put(
                f"观战视角: {self_player.get('riotId')}（{self_player.get('team')}，金币按装备估算）"
            )
        return patched

    def run(self) -> None:
        try:
            self.prepare()
        except Exception as error:  # noqa: BLE001
            self.messages.put(f"初始化失败: {error}")
            return
        self.messages.put(f"模型: {self.model_name}" + ("（段位已接管）" if self.resolver else "（无段位）"))

        ranks: Dict[str, float] = {}
        ranks_done = False
        last_game_time = 0.0
        demo_second = 0
        while not self.stop_event.is_set():
            if self.args.demo:
                payload = demo_payload(demo_second)
                demo_second += 10
                if not ranks_done:
                    ranks = {f"蓝色玩家{i}#1000": 5000.0 for i in range(5)}
                    ranks.update({f"红色玩家{i}#2000": 4000.0 for i in range(5)})
                    ranks_done = True
            else:
                payload = fetch_payload()
            if payload is None:
                self.state.waiting = True
                time.sleep(max(1.0, self.args.interval))
                continue

            active = payload.get("activePlayer") or {}
            if not active.get("summonerName") or active.get("error"):
                patched = self._spectator_self(payload)
                if patched is None:
                    self.state.waiting = True
                    self.messages.put("2999 没有 activePlayer（观战/回放），且找不到可用视角")
                    time.sleep(max(1.0, self.args.interval))
                    continue
                payload = patched
            game_time = float((payload.get("gameData") or {}).get("gameTime") or 0)
            if game_time + 300 < last_game_time:
                ranks = {}
                ranks_done = False
            last_game_time = max(last_game_time, game_time)

            # 自愈：启动时没找到 LCU 不一次性放弃，带冷却重试 discover
            if (
                self.resolver is None
                and not self.args.no_ranks
                and time.time() >= self._resolver_retry_at
            ):
                self._resolver_retry_at = time.time() + 15.0
                found = discover_lcu_psutil()
                if found:
                    try:
                        self.resolver = RankResolver(
                            found[0], found[1], cache_path=self.args.rank_cache
                        )
                        self.messages.put(f"段位: LCU 已连接（端口 {found[0]}）")
                        if self.model_name == "live_model3" and self.assets["model5"].is_dir():
                            self.models = ModelSet(self.assets["model5"], self.assets["events"])
                            self.model_name = "live_model5"
                            self.state.model_name = self.model_name
                    except Exception as error:  # noqa: BLE001
                        self.messages.put(f"段位解析不可用: {error}")

            if (
                self.resolver is not None
                and not ranks_done
                and time.time() >= self._rank_retry_at
                and len(payload.get("allPlayers") or []) >= 10
            ):
                found_ranks = self.resolver.resolve_payload(payload)
                if 0 < len(found_ranks) < 10:
                    found_ranks = complete_payload_ranks(payload, found_ranks)
                if found_ranks:
                    ranks_done = True
                    ranks = found_ranks
                    if self.model_name == "live_model3" and self.assets["model5"].is_dir():
                        self.models = ModelSet(self.assets["model5"], self.assets["events"])
                        self.model_name = "live_model5"
                        self.state.model_name = self.model_name
                        self.messages.put("段位: 已解析 10 人（已切回 live_model5）")
                    else:
                        self.messages.put("段位: 已解析 10 人")
                else:
                    # 空结果：不标记 done，冷却后重试（客户端可能刚开/网络抖动）
                    self._rank_retry_at = time.time() + 20.0
                    if self.model_name == "live_model5" and self.assets["model3"].is_dir():
                        self.models = ModelSet(self.assets["model3"], self.assets["events"])
                        self.model_name = "live_model3"
                        self.state.model_name = self.model_name
                        self.messages.put("段位解析不足，已回退 live_model3（稍后自动重试）")

            try:
                _predictions, tick_features = tick(
                    payload, self.models, self.feed, self.state, self.llm,
                    self.args.llm_interval, True, self.opgg, ranks or None,
                )
                if (
                    self.model_name == "live_model5"
                    and self.assets["model3"].is_dir()
                    and rank_fallback_required(tick_features)
                ):
                    # 段位已提供但 10 人没凑齐：live_model5 的 rank 特征不全，
                    # 强制回退 live_model3（不带段位特征）。
                    self.models = ModelSet(self.assets["model3"], self.assets["events"])
                    self.model_name = "live_model3"
                    self.state.model_name = self.model_name
                    self.messages.put("段位解析不完整，已回退 live_model3（不带段位特征）")
            except Exception as error:  # noqa: BLE001
                self.messages.put(f"解析失败: {error}")
            time.sleep(max(0.5, self.args.interval))

    def stop(self) -> None:
        self.stop_event.set()
        if self.resolver is not None:
            self.resolver.save()



def _enforce_single_instance() -> None:
    """T41：单实例互斥——枚举本机其它 LoLAdvisor 进程并结束它们。

    背景：多实例/旧窗口并存时，旧窗口内存跑旧 JS（主页绑定缺失而设置正常），
    新旧资源混跑导致页面错乱。策略：新实例接管，旧实例全部结束（stderr 记录）。
    只在生产 GUI 启动路径调用（--selftest/--diag/--demo 不杀运行中的实例）。
    """
    import psutil

    my_pid = os.getpid()
    terminated: List[int] = []
    try:
        for process in psutil.process_iter(["pid", "name"]):
            name = (process.info.get("name") or "").lower()
            if not name.startswith("loladvisor"):
                continue
            pid = process.info.get("pid")
            if not pid or pid == my_pid:
                continue
            try:
                process.terminate()
                terminated.append(int(pid))
            except Exception:  # noqa: BLE001
                pass
    except Exception as error:  # noqa: BLE001
        print(f"[single-instance] 枚举进程失败: {error}", file=sys.stderr, flush=True)
        return
    for pid in terminated:
        print(f"[single-instance] 已结束旧实例 PID={pid}", file=sys.stderr, flush=True)
    if terminated:
        # 等旧实例释放端口/资源后再绑定 8788/8789
        time.sleep(0.6)


def run_diag() -> int:
    """把运行环境/LCU/段位解析结果写到 diag.log（排查打包问题用）。"""
    lines: List[str] = []

    def say(message: str) -> None:
        lines.append(str(message))
        try:
            print(message)
        except Exception:
            pass

    say(f"version: {APP_VERSION}")
    try:
        import psutil

        say(f"psutil: {psutil.__version__}")
        total = 0
        league = []
        for process in psutil.process_iter(["pid", "name"]):
            total += 1
            name = process.info.get("name") or ""
            if "League" in name:
                try:
                    args = process.cmdline()
                    league.append((process.info.get("pid"), name, len(args), " ".join(args)[:60]))
                except Exception as error:  # noqa: BLE001
                    league.append((process.info.get("pid"), name, -1, f"{type(error).__name__}"))
        say(f"processes: {total}; league: {len(league)}")
        for item in league[:8]:
            say(f"  {item}")
    except Exception as error:  # noqa: BLE001
        say(f"psutil import failed: {error}")
    try:
        from rank_lookup import RankResolver, discover_lcu_psutil

        found = discover_lcu_psutil()
        say(f"LCU: {found[0] if found else None}")
        if found:
            resolver = RankResolver(found[0], found[1])
            me = resolver.current_summoner()
            say(f"current summoner: {me}")
            if me:
                say(f"rank: {resolver.lookup(me)}")
    except Exception as error:  # noqa: BLE001
        say(f"LCU check failed: {type(error).__name__}: {error}")
    try:
        say("edge: " + str([p for p in EDGE_CANDIDATES if os.path.isfile(p)] or "not found"))
    except Exception as error:  # noqa: BLE001
        say(f"edge check failed: {error}")
    say("ui: " + str({p.name: p.is_file() for p in (UI_HOME_HTML, UI_DESIGN_CSS, UI_DEMO_JS)}))
    say("assets: " + str({k: str(v) for k, v in resolve_assets().items()}))
    try:
        (config_dir() / "diag.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        os._exit(0)  # 同 run_selftest：窗口版 exe 必须用 os._exit 保证可靠退出
    return 0


def _selftest_lines() -> List[str]:
    """验证模型/特征/推理都能跑，返回输出行（供 run_selftest 与 home 页自检卡片复用）。"""
    lines: List[str] = []

    def say(message: str) -> None:
        lines.append(str(message))

    assets = resolve_assets()
    say(f"version: {APP_VERSION}")
    say("assets: " + str({k: str(v) for k, v in assets.items()}))
    models = ModelSet(assets["model3"], assets["events"])
    say(f"live_model3: {models.win.num_trees} 棵树, {len(models.win.feature_names)} 特征")
    if assets["model5"].is_dir():
        models5 = ModelSet(assets["model5"], assets["events"])
        say(f"live_model5: {models5.win.num_trees} 棵树, {len(models5.win.feature_names)} 特征")

    players = []
    for index, role in enumerate(["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]):
        players.append({
            "riotId": f"B{index}#1", "summonerName": f"B{index}#1", "team": "ORDER",
            "position": role, "level": 11, "rawChampionName": f"game_character_displayname_ChampB{index}",
            "scores": {"kills": 2, "deaths": 1, "assists": 3, "creepScore": 120},
        })
        players.append({
            "riotId": f"R{index}#1", "summonerName": f"R{index}#1", "team": "CHAOS",
            "position": role, "level": 10, "rawChampionName": f"game_character_displayname_ChampR{index}",
            "scores": {"kills": 1, "deaths": 2, "assists": 2, "creepScore": 110},
        })
    payload = {
        "activePlayer": {"summonerName": "B0#1", "currentGold": 900, "level": 11},
        "allPlayers": players,
        "gameData": {"gameTime": 900},
        "events": {"Events": []},
    }
    from live_features import extract_features
    features = extract_features(payload)
    say("model3 胜率: " + str(round(models.predict(features)["win"], 4)))
    if assets["model5"].is_dir():
        ranks = {f"B{i}#1": 5000.0 for i in range(5)}
        ranks.update({f"R{i}#1": 4000.0 for i in range(5)})
        features5 = extract_features(payload, None, ranks)
        say("model5 胜率(段位差 +1000): " + str(round(models5.predict(features5)["win"], 4)))
    say("SELFTEST OK")
    return lines


def selftest_ok(lines: List[str]) -> bool:
    return bool(lines) and lines[-1] == "SELFTEST OK"


def run_selftest() -> int:
    """不开窗口，验证打包后的模型/特征/推理都能跑（结果写 selftest.log）。

    frozen（打包版）模式下：任何异常都写进日志，然后用 os._exit 直接退出。
    窗口版 exe 的未捕获异常会弹错误对话框卡住，且解释器关闭可能被残留
    非 daemon 线程（如 PyInstaller 的 multiprocessing rthook 线程池）卡住，
    os._exit 跳过这两者，保证 --selftest 一定可靠退出。
    """
    try:
        lines = _selftest_lines()
    except Exception as error:  # noqa: BLE001
        lines = [
            f"version: {APP_VERSION}",
            f"自检异常: {type(error).__name__}: {error}",
            traceback.format_exc(limit=5),
        ]
    ok = selftest_ok(lines)
    if getattr(sys, "frozen", False):
        try:
            (config_dir() / "selftest.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass
        os._exit(0 if ok else 1)
    for line in lines:
        try:
            print(line)
        except Exception:
            pass
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LoLAdvisor 桌面版（Web 渲染）")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔（秒）")
    parser.add_argument("--no-ranks", action="store_true", help="不解析段位（用 live_model3）")
    parser.add_argument("--no-llm", action="store_true", help="不启用 LLM 建议")
    parser.add_argument("--llm-interval", type=float, default=20.0)
    parser.add_argument(
        "--rank-cache", type=Path,
        default=Path.home() / ".lol-advisor" / "rank_cache.json",
        help="段位缓存位置",
    )
    parser.add_argument("--selftest", action="store_true", help="不开窗口，检查模型与推理")
    parser.add_argument("--demo", action="store_true", help="演示模式：静态样例数据渲染界面（不开游戏也能看）")
    parser.add_argument("--player", default="", help="观战/回放时指定视角（名字或英雄片段）")
    parser.add_argument("--diag", action="store_true", help="写环境诊断到 diag.log 后退出")
    parser.add_argument("--home-port", type=int, default=8789, help="主窗口本地服务端口")
    parser.add_argument(
        "--no-overlay", action="store_true",
        help="关闭左下角悬浮窗（默认对局中显示；需游戏为无边框/窗口模式）",
    )
    parser.add_argument(
        "--overlay-demo", action="store_true",
        help="只预览左下角悬浮窗（8 秒假数据，便于截图/调样式）",
    )
    args = parser.parse_args(argv)

    if args.overlay_demo:
        from overlay import main as overlay_main
        return overlay_main(["--demo"])

    if args.selftest:
        return run_selftest()
    if args.diag:
        return run_diag()

    if not args.demo:
        # T41：单实例互斥（服务绑定前；--demo 预览不干扰运行中的实例）
        _enforce_single_instance()

    assets = resolve_assets()
    if not (assets["model3"].is_dir() or assets["model5"].is_dir()):
        print("找不到模型文件（assets/live_dataset/...）")
        return 1

    if args.demo:
        # 静态演示模式：直接打开带 ?demo=1 的一体化单页（不发任何网络请求）。
        demo_url = UI_APP_HTML.as_uri() + "?demo=1"
        if not UI_APP_HTML.is_file():
            print(f"找不到一体化页面: {UI_APP_HTML}")
            return 1
        print(f"演示模式: {demo_url}")
        open_home_window(demo_url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    # 启动前自检一次（供 home 页「设置与自检」卡片显示）
    try:
        lines = _selftest_lines()
        _SELFTEST_CACHE.update(
            passed=selftest_ok(lines),
            at=time.strftime("%Y-%m-%d %H:%M"),
            lines=lines,
        )
    except Exception as error:  # noqa: BLE001
        _SELFTEST_CACHE.update(
            passed=False,
            at=time.strftime("%Y-%m-%d %H:%M"),
            lines=[f"自检异常: {error}"],
        )

    state = State("live", None)
    poller = Poller(state, assets, args)
    poller.start()
    # 段位状态透传：as_json 的 rankStatus 通过 provider 动态读 poller.resolver
    state._resolver_provider = lambda: poller.resolver

    # 选人助手内嵌服务（127.0.0.1:8788），home 页「打开选人助手」入口依赖它
    start_draft_server()

    # T54：打野雷达采集线程（1fps，mss/Pillow 缺失或全屏独占时优雅降级）
    threading.Thread(target=_jungle_loop, daemon=True).start()

    # T59：左下角半透明悬浮窗（对局中显示胜率/趋势/建议；纯外部窗口，不注入游戏）
    overlay_ctl = None
    if not args.no_overlay:
        try:
            from overlay import OverlayController

            overlay_ctl = OverlayController(lambda: _overlay_payload(state))
            overlay_ctl.start()
            print("[overlay] 左下角悬浮窗已启用（游戏需无边框/窗口模式）", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"[overlay] 启动失败，已跳过：{error}", file=sys.stderr, flush=True)
            overlay_ctl = None

    server, port = start_home_server(
        {"state": state, "args": args, "poller": poller}, port=args.home_port
    )
    print(f"主窗口: http://127.0.0.1:{port}", flush=True)
    # 生产入口不带 ?demo=1；带版本参数双保险，规避 Edge 对旧页面的缓存
    open_home_window(f"http://127.0.0.1:{port}/?v={urllib.parse.quote(APP_VERSION)}")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("已退出")
    finally:
        _JUNGLE_STATE["stop"] = True
        if overlay_ctl is not None:
            overlay_ctl.stop()
        poller.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except Exception:
        if getattr(sys, "frozen", False):
            # 打包版：未捕获异常写 crash.log 后直接退出，避免错误对话框卡死
            try:
                (config_dir() / "crash.log").write_text(traceback.format_exc() + "\n", encoding="utf-8")
            except Exception:
                pass
            os._exit(1)
        raise
    if getattr(sys, "frozen", False):
        os._exit(int(code))  # 跳过解释器关闭（残留非 daemon 线程可能导致挂起）
    raise SystemExit(int(code))
