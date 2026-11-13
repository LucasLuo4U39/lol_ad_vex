"""v2.3.13 回归测试（用假 LCU 数据驱动，不依赖真实客户端；由 run-tests.cmd 调用）。

覆盖：
  A. _profile_payload：participants 无 puuid、身份在 participantIdentities 时能否解析出 champId/win/KDA
  B. _solo_rank_snapshot：段位快照与 rank_score
  C. _home_payload：未开局时 rankStatus 是否从 resolving 变为 ok（只填 own）
  D. _jungler_payload：champCn/zoneCn/campCn/threatLaneCn 与完整路线节点
  E. _proxy_profile_icon：JPEG→PNG 转码 + no-store 响应头
"""
import io
import json
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))

import desktop_app as d  # noqa: E402

FAKE_PUUID = "puuid-test-0001"
FAKE_SUMMONER = {
    "summonerId": 123456,
    "puuid": FAKE_PUUID,
    "gameName": "Tester",
    "tagLine": "KR1",
    "profileIconId": 5,
    "summonerLevel": 187,
}
FAKE_RANKED = {
    "queues": [
        {
            "queueType": "RANKED_SOLO_5x5",
            "tier": "DIAMOND",
            "division": "I",
            "leaguePoints": 75,
            "wins": 136,
            "losses": 120,
        }
    ]
}
FAKE_HISTORY = {
    "games": {
        "games": [
            {
                "gameCreation": 1763000000000,
                "gameDuration": 1800,
                "gameMode": "CLASSIC",
                "participants": [
                    {
                        "participantId": 11,
                        "championId": 91,
                        "stats": {"win": True, "kills": 2, "deaths": 13, "assists": 16},
                        "teamId": 100,
                    }
                ],
                "participantIdentities": [
                    {"participantId": 11, "player": {"puuid": FAKE_PUUID, "summonerId": 123456}}
                ],
            }
        ]
    }
}

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (" | " + detail if detail else ""))


def fake_lcu_json(path):
    if path.startswith("/lol-summoner/v1/current-summoner"):
        return FAKE_SUMMONER
    if path.startswith("/lol-ranked/v1/current-ranked-stats"):
        return FAKE_RANKED
    if path.startswith("/lol-match-history/"):
        return FAKE_HISTORY
    return None


def main():
    original = d._lcu_json
    d._lcu_json = fake_lcu_json
    try:
        # ---------------- A. profile matches ----------------
        d._PROFILE_CACHE.update(at=0.0, data=None)
        profile = d._profile_payload()
        matches = profile.get("matches") or []
        check("A1 profile.ok", profile.get("ok") is True, str(profile.get("message")))
        check("A2 matches 非空", len(matches) == 1, f"len={len(matches)}")
        if matches:
            m = matches[0]
            check("A3 champId=91", m.get("champId") == 91, str(m.get("champId")))
            check("A4 win=True", m.get("win") is True, str(m.get("win")))
            check("A5 KDA=2/13/16", (m.get("kills"), m.get("deaths"), m.get("assists")) == (2, 13, 16),
                  f"{m.get('kills')}/{m.get('deaths')}/{m.get('assists')}")
            check("A6 champ 中文名非空", bool(m.get("champ")), str(m.get("champ")))
        check("A7 solo=钻石 I 75LP",
              (profile.get("solo") or {}).get("tier") == "DIAMOND"
              and (profile.get("solo") or {}).get("lp") == 75,
              str(profile.get("solo")))

        # ---------------- B. solo rank snapshot ----------------
        d._SOLO_RANK_CACHE.update(at=0.0, data=None)
        snap = d._solo_rank_snapshot()
        check("B1 snapshot 有值", bool(snap), str(snap))
        if snap:
            check("B2 tier/division/lp", (snap["tier"], snap["division"], snap["lp"]) == ("DIAMOND", "I", 75), str(snap))
            check("B3 score>0", float(snap.get("score") or 0) > 0, str(snap.get("score")))

        # ---------------- C. home payload rankStatus ----------------
        d._HOME_CONTEXT["state"] = None
        d._draft_rank_bridge = lambda: None  # 不依赖选人服务
        home = d._home_payload()
        state = home.get("state") or {}
        check("C1 /api/home 版本与 APP_VERSION 一致", str(home.get("version", "")) == d.APP_VERSION,
              f"{home.get('version')} vs {d.APP_VERSION}")
        check("C2 rank.own 有值", (state.get("rank") or {}).get("own") is not None, str(state.get("rank")))
        check("C3 rankStatus.state=ok", (state.get("rankStatus") or {}).get("state") == "ok",
              str(state.get("rankStatus")))
        check("C4 未伪造 team/enemy",
              (state.get("rank") or {}).get("team") is None and (state.get("rank") or {}).get("enemy") is None,
              str(state.get("rank")))

        # ---------------- F. 回归项（回归审查发现的问题） ----------------
        # F1/F2：participantIdentities 里混入非 dict、participantId 一边 str 一边 int
        bad_history = {
            "games": {
                "games": [
                    {
                        "gameCreation": 1763000000000,
                        "gameDuration": 1500,
                        "participants": [
                            {"participantId": 1, "championId": 1, "stats": {"win": True}}
                        ],
                        "participantIdentities": [
                            "garbage",
                            {"participantId": "1", "player": {"puuid": FAKE_PUUID}},
                        ],
                    }
                ]
            }
        }

        def lcu_bad(path):
            return bad_history if path.startswith("/lol-match-history/") else fake_lcu_json(path)

        d._lcu_json = lcu_bad
        d._PROFILE_CACHE.update(at=0.0, data=None)
        try:
            prof2 = d._profile_payload()
            m2 = (prof2.get("matches") or [{}])[0]
            check("F1 脏 identities 不崩且按字符串对齐", m2.get("champId") == 1, str(m2.get("champId")))
        except Exception as error:  # noqa: BLE001
            check("F1 脏 identities 不崩且按字符串对齐", False, repr(error))

        # F2/F3：LCU 用 NONE 表示未定级
        ranked_none = {"queues": [{"queueType": "RANKED_SOLO_5x5", "tier": "NONE", "leaguePoints": 0}]}

        def lcu_none(path):
            return ranked_none if path.startswith("/lol-ranked/") else fake_lcu_json(path)

        d._lcu_json = lcu_none
        d._PROFILE_CACHE.update(at=0.0, data=None)
        d._SOLO_RANK_CACHE.update(at=0.0, data=None)
        prof3 = d._profile_payload()
        check("F2 tier=NONE 视为未定级", (prof3.get("solo") or {}).get("unranked") is True,
              str(prof3.get("solo")))
        check("F3 tier=NONE 时快照为 None", d._solo_rank_snapshot() is None,
              str(d._solo_rank_snapshot()))

        # F4/F5：小地图象限与大小写区域键
        check("F4 象限中文", d._zone_cn("TOP_LEFT") == "左上区域", d._zone_cn("TOP_LEFT"))
        check("F5 区域键大小写不敏感", "蓝BUFF" in d._zone_cn("Blue_Blue"), d._zone_cn("Blue_Blue"))

        # F6：畸形 tracker（config 非 dict、routes 元素非 dict、steps 非序列）
        class BrokenTracker:
            config = ["not", "a", "dict"]
            routes = ["nope", {"id": "x", "steps": "bad"}, {}]

            def predict(self, game_time):
                return {
                    "currentZone": "red_blue",
                    "predictedNextZone": "gromp_blue",
                    "etaSeconds": 30,
                    "threatLane": "bot",
                    "routeHypothesis": "x",
                }

        try:
            nodes = d._jungle_route_nodes(BrokenTracker(), {"routeHypothesis": "x"}, 100.0)
            check("F6 畸形路线数据不抛异常", isinstance(nodes, list), repr(nodes)[:80])
        except Exception as error:  # noqa: BLE001
            check("F6 畸形路线数据不抛异常", False, repr(error))

        # ---------------- D. jungler payload ----------------
        class FakeTracker:
            config = {"time_scale": 1.0}
            routes = [
                {
                    "id": "red_first",
                    "steps": [
                        ("raptors_red", 95),
                        ("red_red", 125),
                        ("krugs_red", 160),
                        ("wolves_red", 210),
                        ("blue_blue", 245),
                        ("gromp_blue", 280),
                    ],
                }
            ]

            def predict(self, game_time):
                return {
                    "currentZone": "raptors_red",
                    "predictedNextZone": "red_red",
                    "etaSeconds": 30,
                    "confidence": 0.62,
                    "threatLane": "top",
                    "lastSeen": {"zone": "raptors_red", "secondsAgo": 12},
                    "routeHypothesis": "red_first",
                }

        d._JUNGLE_STATE.update(tracker=FakeTracker(), champ_key="LeeSin", champ_id=64, error=None)
        d._JUNGLE_CACHE.update(at=0.0, data=None)
        jg = d._jungler_payload()
        expected_cn = d._champion_display(64)
        check("D1 champCn 与 champion 表一致",
              bool(expected_cn) and (jg.get("jungler") or {}).get("champCn") == expected_cn,
              "got=%r expect=%r" % ((jg.get("jungler") or {}).get("champCn"), expected_cn))
        check("D2 champ 仍为机器键", (jg.get("jungler") or {}).get("champ") == "LeeSin",
              str((jg.get("jungler") or {}).get("champ")))
        check("D3 threatLaneCn=上路", jg.get("threatLaneCn") == "上路", str(jg.get("threatLaneCn")))
        check("D4 lastSeen.zoneCn 中文",
              "锋喙鸟" in str((jg.get("lastSeen") or {}).get("zoneCn")),
              str((jg.get("lastSeen") or {}).get("zoneCn")))
        check("D5 predicted.zoneCn 中文",
              "红" in str((jg.get("predicted") or {}).get("zoneCn")),
              str((jg.get("predicted") or {}).get("zoneCn")))
        route = jg.get("route") or []
        check("D6 完整路线 5-6 节点", len(route) >= 5, f"len={len(route)}")
        check("D7 每节点有 campCn", all(n.get("campCn") for n in route), str([n.get("campCn") for n in route]))
        check("D8 current 只标记一个", sum(1 for n in route if n.get("current")) == 1,
              str([(n["time"], n["current"]) for n in route]))

        # ---------------- E. profile icon ----------------
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (200, 30, 40)).save(buf, format="JPEG")
        jpeg = buf.getvalue()
        d._lcu_request_bytes = lambda path, timeout=4.0: jpeg

        class DummyHandler:
            _is_head = False

            def __init__(self):
                self.wfile = io.BytesIO()
                self.status = None
                self.headers = {}

            def send_response(self, code, message=None):
                self.status = code

            def send_header(self, key, value):
                self.headers[key] = value

            def end_headers(self):
                pass

            def send_error(self, code, message=None, explain=None):
                self.status = code

        handler = DummyHandler()
        # _proxy_profile_icon 定义在 HomeHandler 上，用未绑定调用喂给假 handler
        d.HomeHandler._proxy_profile_icon(handler, 5)
        body = handler.wfile.getvalue()
        check("E1 status=200", handler.status == 200, str(handler.status))
        check("E2 返回真 PNG（魔数）", body[:4] == b"\x89PNG", body[:8].hex())
        check("E3 Content-Type=image/png", handler.headers.get("Content-Type") == "image/png",
              str(handler.headers.get("Content-Type")))
        check("E4 Cache-Control=no-store", handler.headers.get("Cache-Control") == "no-store",
              str(handler.headers.get("Cache-Control")))
        # ---------------- G. 左下角悬浮窗数据（T59） ----------------
        from advisor_server import State

        st = State("live", None)
        st.waiting = False
        st.win_prob = 0.62
        st.game_time = 700.0
        st.clock = "11:40"
        st.model_name = "live_model5"
        st.rule_tips = ["控龙"]
        st.event_probs = {"enemy_gank_60s": 0.42, "own_kill_30s": 0.2}
        st.history["t"] = [600.0, 700.0]
        st.history["winProb"] = [0.58, 0.62]
        payload = d._overlay_payload(st)
        check("G1 对局中返回悬浮窗数据",
              isinstance(payload, dict) and payload.get("winProb") == 0.62, str(payload))
        check("G2 60 秒趋势 +4pp",
              abs(float((payload or {}).get("trendPp") or 0.0) - 4.0) < 0.01,
              str((payload or {}).get("trendPp")))
        check("G3 建议透传", (payload or {}).get("advice") == "控龙", str((payload or {}).get("advice")))
        check("G4 预测取最高事件概率",
              ((payload or {}).get("prediction") or {}).get("label") == "敌方 60 秒内 gank"
              and abs(float(((payload or {}).get("prediction") or {}).get("prob") or 0) - 0.42) < 1e-6,
              str((payload or {}).get("prediction")))
        st.waiting = True
        check("G5 未开局时隐藏（返回 None）", d._overlay_payload(st) is None)
    finally:
        d._lcu_json = original

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n汇总: %d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("失败项: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
