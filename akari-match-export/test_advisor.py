#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end self-test for advisor_server without a running game.

Uses the recorded replay snapshot session + shipped models. No lightgbm needed.
"""

from __future__ import annotations

import http.client
import http.server
import json
import threading
import time
from pathlib import Path

from advisor_server import (
    AdvisorHttpServer,
    EVENT_LABELS,
    FeedBuilder,
    LlmWorker,
    ModelSet,
    SnapshotSource,
    State,
    build_llm_prompt,
    load_env_file,
    mask_key,
    parse_llm_advice,
    patch_replay_self,
)
from live_features import advice as rule_advice
from live_features import extract_features
from live_features import normalize_champ

BASE = Path(__file__).resolve().parent
SNAP = BASE.parent / "analysis" / "akari-replay-live" / "session_20261109_015738" / "snapshots.jsonl"
MODELS = BASE.parent / "dataset" / "live_dataset"

PASS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        raise AssertionError(f"{name}: {detail}")


def get(path: str, port: int) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read().decode("utf-8", errors="replace")
    conn.close()
    return response.status, body


def test_snapshot_source() -> dict:
    print("[1] 快照源")
    source = SnapshotSource(SNAP, "future", speed=1e9)
    check("快照数量 > 100", len(source.snapshots) > 100, f"got {len(source.snapshots)}")
    check(
        "视角解析",
        "future" in str(source.self_player.get("riotId") or "").lower(),
        str(source.self_player.get("riotId")),
    )
    payload = source.to_payload(source.snapshots[0])
    check("payload 有 10 名玩家", len(payload["allPlayers"]) == 10)
    check("payload 有事件", bool(payload["events"]["Events"]))
    check(
        "activePlayer 金币估算 >= 0",
        float(payload["activePlayer"]["currentGold"]) >= 0,
    )
    return payload


def test_features_models(payload: dict):
    print("[2] 特征 + 模型预测")
    features = extract_features(payload)
    check("特征 minute >= 1", features["minute"] >= 1, str(features["minute"]))
    check("特征含 killDiff", "killDiff" in features)

    models = ModelSet(MODELS / "live_model2", MODELS / "live_event_model")
    check("胜率模型加载", models.win.num_trees == 186, f"trees={models.win.num_trees}")
    check("事件模型 4 个", len(models.event_models) == 4, str(len(models.event_models)))

    preds = models.predict(features)
    check("胜率在 (0,1)", 0.0 < preds["win"] < 1.0, str(preds["win"]))
    for label in EVENT_LABELS:
        check(f"{label} 在 (0,1)", 0.0 < preds[label] < 1.0, str(preds[label]))
    return models, features, preds


def test_feed_state(payload, models, features, preds):
    print("[3] 事件流 + 状态 JSON")
    feed_builder = FeedBuilder()
    feed = feed_builder.build(payload, "CHAOS", float(payload["gameData"]["gameTime"]))
    check("feed 有击杀事件", any("击杀" in item["text"] for item in feed), str(feed[:3]))

    tips = rule_advice(features, preds["win"], {l: preds[l] for l in EVENT_LABELS})
    check("规则建议非空", len(tips) > 0)

    state = State("replay", None)
    state.update(
        payload,
        preds,
        features,
        tips,
        feed,
        "CHAOS",
        str(payload["activePlayer"].get("summonerName") or ""),
    )
    data = state.as_json()
    check("state ok", data["ok"] is True)
    check("state source", data["source"] == "replay")
    check("state winProb", data["winProb"] is not None)
    check("state advice", len(data["advice"]) > 0)
    check("state history 有记录", len(data["history"]["winProb"]) == 1)
    check("state teamDiff 完整", len(data["teamDiff"]) == 12, str(data["teamDiff"]))
    return state


def test_http(state: State):
    print("[4] HTTP 服务")
    server = AdvisorHttpServer(("127.0.0.1", 0), state)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = get("/api/state", port)
        check("/api/state 200", status == 200)
        parsed = json.loads(body)
        check("/api/state 是 JSON", isinstance(parsed, dict) and parsed.get("ok") is True)
        status, body = get("/", port)
        check("/ 200", status == 200)
        # v2.3 起仪表盘为 ui/next/dashboard.html，品牌「实时仪表盘 · LoLAdvisor」
        # （旧 advisor_dashboard.html 标题「LoL 实时助手」仅作回退）
        check("仪表盘 HTML", "实时仪表盘" in body)
        status, _ = get("/nope", port)
        check("/nope 404", status == 404)
    finally:
        server.shutdown()


def test_llm():
    print("[5] LLM 层（本地桩服务）")
    stub = StubLlmServer()
    stub.start()
    try:
        worker = LlmWorker(
            base_url=f"http://127.0.0.1:{stub.port}",
            api_key="test-key",
            model="stub-model",
            interval=1.0,
        )
        worker.start()
        prompt = build_llm_prompt(
            "20:34",
            0.61,
            {
                "ownLevel": 11, "ownKills": 3, "ownDeaths": 1, "ownAssists": 4,
                "ownCS": 180, "ownGold": 5432,
                "killDiff": 5, "assistDiff": 6, "csDiff": 33, "levelSumDiff": 4,
                "dragonDiff": 1, "baronDiff": 0, "heraldDiff": 0, "grubDiff": 2,
                "atakhanDiff": 0, "towerDiff": 2, "inhibitorDiff": 0,
            },
            {
                "own_kill_30s": 0.42, "enemy_kill_30s": 0.51,
                "own_gank_60s": 0.44, "enemy_gank_60s": 0.53,
            },
            [{"t": 1170, "text": "Samira 击杀 Lux", "side": "own"}],
        )
        check("LLM prompt 含胜率", "61.0%" in prompt, prompt)
        worker.request(1234.0, prompt)
        for _ in range(60):
            snap = worker.snapshot()
            if snap["lastAt"] is not None:
                break
            time.sleep(0.1)
        snap = worker.snapshot()
        check("LLM 返回建议", snap["tips"] == ["注意小龙区视野", "别压线"], str(snap))
        check("LLM 无错误", snap["error"] is None, str(snap.get("error")))

        parsed = parse_llm_advice("- 后退\n- 补视野\n无关行")
        check("parse_llm_advice 宽容解析", parsed == ["后退", "补视野", "无关行"], str(parsed))
    finally:
        stub.stop()


class StubLlmServer:
    """Minimal OpenAI-compatible /chat/completions stub."""

    def __init__(self):
        self.port = 0
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _handler(self):
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                assert body["model"] == "stub-model"
                assert self.headers.get("Authorization") == "Bearer test-key"
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "- 注意小龙区视野\n- 别压线\n",
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # noqa: A003
                pass

        return Handler

    def start(self):
        self._server = http.server.HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()


def test_llm_config_helpers():
    print("[6] key 遮罩 + env 文件加载")
    key = "sk-abcdef1234567890abcdef1234567890"
    masked = mask_key(key)
    check("遮罩不以明文出现", key not in masked, masked)
    check("遮罩格式", masked == "sk-abcde****7890", masked)

    tmp = BASE / "_test_env_tmp.txt"
    tmp.write_text(
        "# 注释\nADVISOR_LLM_API_KEY=abc-123\nADVISOR_LLM_MODEL=test-model\n",
        encoding="utf-8",
    )
    try:
        loaded = load_env_file(tmp)
        check("env 文件解析 key", loaded.get("ADVISOR_LLM_API_KEY") == "abc-123", str(loaded))
        check("env 文件解析 model", loaded.get("ADVISOR_LLM_MODEL") == "test-model")
        check("env 文件不存在返回空", load_env_file(BASE / "nope.env") == {})
    finally:
        tmp.unlink(missing_ok=True)


def test_live_replay_patch():
    print("[7] 直播回放视角注入（patch_replay_self）")
    src = SnapshotSource(SNAP, "future", speed=1e9)
    snap = src.snapshots[0]
    data = snap["data"]
    all_players = []
    for p in data["players"]:
        scores = p.get("scores") or {}
        all_players.append(
            {
                "riotId": p.get("riotId"),
                "summonerName": p.get("riotId"),
                "team": p.get("team"),
                "position": p.get("position"),
                "level": p.get("level"),
                "rawChampionName": p.get("rawChampionName"),
                "championName": p.get("champion"),
                "scores": {
                    "kills": scores.get("kills"),
                    "deaths": scores.get("deaths"),
                    "assists": scores.get("assists"),
                    "creepScore": scores.get("creepScore"),
                    "wardScore": scores.get("wardScore"),
                },
                "items": p.get("items"),
            }
        )
    payload = {
        "activePlayer": {},
        "allPlayers": all_players,
        "gameData": {"gameTime": snap.get("gameTime")},
        "events": {"Events": snap.get("events") or []},
    }

    # 真实回放里 position 是真实分路字符串（如 JUNGLE），旧快照里是 NONE；
    # 强制给选中玩家一个真实分路，覆盖 feature_row 对字符串 role 的处理
    for p in all_players:
        if "future" in str(p.get("riotId") or "").lower():
            p["position"] = "JUNGLE"

    patched, self_player = patch_replay_self(payload, "future")
    check("activePlayer 已注入", bool(patched["activePlayer"].get("summonerName")))
    check(
        "注入的是选中玩家",
        patched["activePlayer"]["summonerName"] == self_player["riotId"],
    )
    check("金币为装备估算 > 0", float(patched["activePlayer"]["currentGold"]) > 0)
    check("无金币桥时来源为 estimate", patched["activePlayer"].get("_goldSource") == "estimate")

    # 有金币桥（SGP 口袋金币）时优先使用并标记 sgp
    patched2, _ = patch_replay_self(payload, "future", gold_for=lambda name, t: 999.0)
    check(
        "金币桥生效（口袋金币 999）",
        float(patched2["activePlayer"]["currentGold"]) == 999.0
        and patched2["activePlayer"].get("_goldSource") == "sgp",
        str(patched2["activePlayer"]),
    )
    # 金币桥查不到时回退装备估算
    patched3, _ = patch_replay_self(payload, "future", gold_for=lambda name, t: None)
    check(
        "金币桥查不到时回退 estimate",
        patched3["activePlayer"].get("_goldSource") == "estimate",
    )

    features = extract_features(patched)
    check("特征可提取", features["minute"] >= 1 and "killDiff" in features)
    check("真实分路字符串被正确处理", features["role"] == "JUNGLE", str(features["role"]))
    models = ModelSet(MODELS / "live_model2", MODELS / "live_event_model")
    preds = models.predict(features)
    check("预测可运行", 0.0 < preds["win"] < 1.0, str(preds["win"]))

    state = State("replay", None)
    state.set_self(self_player)
    data_json = state.as_json()
    check(
        "set_self 生效",
        data_json["selfName"] == self_player["riotId"]
        and data_json["goldEstimated"] is True,
    )

    try:
        patch_replay_self(payload, "不存在的玩家xyz")
        check("未知玩家应报错", False)
    except ValueError:
        check("未知玩家应报错", True)


def test_champ_features():
    print("[8] OP.GG 英雄/对位特征")
    import math

    payload = {
        "activePlayer": {"summonerName": "p1", "currentGold": 500, "level": 3},
        "allPlayers": [
            {"riotId": "p1", "team": "CHAOS", "position": "JUNGLE", "level": 3,
             "rawChampionName": "game_character_displayname_Samira",
             "scores": {"kills": 1, "deaths": 0, "assists": 1, "creepScore": 20}},
            {"riotId": "p2", "team": "ORDER", "position": "JUNGLE", "level": 3,
             "rawChampionName": "game_character_displayname_Vi",
             "scores": {"kills": 0, "deaths": 1, "assists": 0, "creepScore": 15}},
        ],
        "gameData": {"gameTime": 120},
        "events": {"Events": []},
    }
    features = extract_features(payload)
    check("无 OP.GG 时 own_champ_wr=0.5", features["own_champ_wr"] == 0.5, str(features["own_champ_wr"]))
    check("无 OP.GG 时 matchup_wr=0.5", features["matchup_wr"] == 0.5)
    check("无 OP.GG 时 matchup_games_log=0", features["matchup_games_log"] == 0.0)

    mock = {
        "stats": {"jungle": {"samira": {"winRate": 0.52}, "vi": {"winRate": 0.48}}},
        "matchups": {"jungle": {"samira": {"vi": {"winRate": 0.45, "games": 100}}}},
    }
    features = extract_features(payload, mock)
    check("own_champ_wr 查表", abs(features["own_champ_wr"] - 0.52) < 1e-9, str(features["own_champ_wr"]))
    check("enemy_champ_wr 查表", abs(features["enemy_champ_wr"] - 0.48) < 1e-9, str(features["enemy_champ_wr"]))
    check("matchup_wr 查表", abs(features["matchup_wr"] - 0.45) < 1e-9, str(features["matchup_wr"]))
    check(
        "matchup_games_log=log1p(100)",
        abs(features["matchup_games_log"] - math.log1p(100)) < 1e-9,
        str(features["matchup_games_log"]),
    )
    check(
        "team_wr_diff=(0.52-0.48)/5",
        abs(features["team_wr_diff"] - (0.52 - 0.48) / 5) < 1e-9,
        str(features["team_wr_diff"]),
    )
    check("normalize_champ 别名", normalize_champ("Nunu & Willump") == "nunu", normalize_champ("Nunu & Willump"))


def test_categorical_mask_split():
    print("[9] lgbmini 分类掩码分裂")
    import math
    import tempfile

    # 合成模型：单特征 role（类别码 0-4），单树两个分类节点（掩码分裂）
    # 节点0: bin0 掩码 6={1,2}；节点1: bin1 掩码 25={0,3,4}
    model_text = """tree
version=v4
num_class=1
num_tree_per_iteration=1
label_index=0
max_feature_idx=0
objective=binary sigmoid:1
feature_names=role
feature_infos=[0:4]
tree_sizes=12

Tree=0
num_leaves=3
num_cat=2
split_feature=0 0
split_gain=1 1
threshold=0 1
decision_type=1 1
left_child=1 -1
right_child=-2 -3
leaf_value=0.1 0.2 0.3
leaf_weight=1 1 1
leaf_count=1 1 1
internal_value=0 0
internal_weight=1 1
internal_count=1 1
is_linear=0
shrinkage=1
cat_boundaries=0 1 2
cat_threshold=6 25
"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        handle.write(model_text)
        path = handle.name
    try:
        from lgbmini import LgbTextModel
        import numpy as np

        model = LgbTextModel(path)
        x = np.array([[0.0], [1.0], [2.0], [3.0], [4.0]])
        prob = model.predict_proba(x)
        expected_raw = [0.2, 0.3, 0.3, 0.2, 0.2]  # 手算：c=1,2 经节点1走右→叶子2(0.3)；其余右→叶子1(0.2)
        for code, want in zip(range(5), expected_raw):
            got = float(prob[code, 0] if prob.ndim > 1 else prob[code])
            want_p = 1.0 / (1.0 + math.exp(-want))
            check(
                f"role 码 {code} 预测正确",
                abs(got - want_p) < 1e-9,
                f"got={got} want={want_p}",
            )
    finally:
        import os

        os.unlink(path)


def main() -> int:
    print(f"快照: {SNAP}")
    payload = test_snapshot_source()
    models, features, preds = test_features_models(payload)
    state = test_feed_state(payload, models, features, preds)
    test_http(state)
    test_llm()
    test_llm_config_helpers()
    test_live_replay_patch()
    test_champ_features()
    test_categorical_mask_split()
    print(f"\n全部通过: {PASS} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
