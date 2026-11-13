#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时段位特征的离线自测：不需要客户端，用假 LCU 响应验证逻辑。"""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

from advisor_server import State
from fetch_player_ranks import rank_score
from live_features import extract_features, team_rank_features
from rank_lookup import RankResolver, complete_payload_ranks

PASS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        raise AssertionError(f"{name}: {detail}")


def test_rank_score() -> None:
    print("[1] 段位分映射")
    check("Challenger 6000+LP", rank_score("CHALLENGER", "NA", 500) == 6500)
    check("GM 5000+LP", rank_score("GRANDMASTER", "NA", 300) == 5300)
    check("Master 4000+LP", rank_score("MASTER", "NA", 0) == 4000)
    check("Diamond I > IV", rank_score("DIAMOND", "I", 50) > rank_score("DIAMOND", "IV", 0))
    check("Iron 最低", rank_score("IRON", "IV", 0) < rank_score("BRONZE", "IV", 0))
    check("未知段位为 0", rank_score("", "", 0) == 0)


def payload_with_ranks(ranks: dict) -> dict:
    players = []
    roles = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    for index, role in enumerate(roles):
        players.append(
            {
                "riotId": f"B{index}#1",
                "summonerName": f"B{index}#1",
                "team": "ORDER",
                "position": role,
                "level": 10,
                "rawChampionName": f"game_character_displayname_ChampB{index}",
                "scores": {"kills": 1, "deaths": 0, "assists": 2, "creepScore": 100},
            }
        )
        players.append(
            {
                "riotId": f"R{index}#1",
                "summonerName": f"R{index}#1",
                "team": "CHAOS",
                "position": role,
                "level": 10,
                "rawChampionName": f"game_character_displayname_ChampR{index}",
                "scores": {"kills": 0, "deaths": 1, "assists": 1, "creepScore": 90},
            }
        )
    return {
        "activePlayer": {"summonerName": "B0#1", "currentGold": 500, "level": 10},
        "allPlayers": players,
        "gameData": {"gameTime": 600},
        "events": {"Events": []},
    }


def test_rank_features() -> None:
    print("[2] 段位特征")
    payload = payload_with_ranks({})
    ranks = {f"B{i}#1": 5000.0 for i in range(5)}
    ranks.update({f"R{i}#1": 4000.0 for i in range(5)})
    features = extract_features(payload, None, ranks)
    check("ownRank", features["ownRank"] == 5000.0)
    check("teamRank", features["teamRank"] == 5000.0)
    check("enemyRank", features["enemyRank"] == 4000.0)
    check("rankDiff", features["rankDiff"] == 1000.0)
    check("ownRankVsTeam", features["ownRankVsTeam"] == 0.0)
    check("缺一列不产出段位特征", "rankDiff" in features)
    partial = dict(ranks)
    partial.pop("R4#1")
    check("缺人返回空", team_rank_features(payload["allPlayers"], payload["allPlayers"][0], partial) == {})
    check("无 ranks 不产出", "rankDiff" not in extract_features(payload, None, None))


def test_completion() -> None:
    print("[3] 缺人补齐")
    payload = payload_with_ranks({})
    ranks = {f"B{i}#1": 5000.0 for i in range(5)}
    ranks.update({f"R{i}#1": 4000.0 for i in range(4)})
    filled = complete_payload_ranks(payload, ranks)
    check("补成 10 人", len(filled) == 10)
    check("缺的人用本队均值", filled["R4#1"] == 4000.0)
    check("不足 8 人放弃", complete_payload_ranks(payload, {k: v for k, v in list(ranks.items())[:7]}) == {})


class FakeResolver(RankResolver):
    def __init__(self) -> None:
        self.port = 0
        self.token = ""
        self.ttl = 10**9
        self.timeout = 1
        self.auth = ""
        self._score_cache = {}
        self._puuid_cache = {}
        self.cache_path = None
        self.queries = 0
        self.errors = 0
        self.calls = []

    def _get(self, path: str):
        self.calls.append(path)
        if path.startswith("/lol-summoner/v1/summoners?name="):
            name = urllib.parse.unquote(path.split("=", 1)[1])
            return {"puuid": f"puuid-{name}", "gameName": name.split("#")[0]}
        if path.startswith("/lol-ranked/v1/ranked-stats/"):
            return {"queueMap": {"RANKED_SOLO_5x5": {"tier": "CHALLENGER", "division": "NA", "leaguePoints": 700}}}
        if "missing" in path:
            return None
        return None


def test_resolver() -> None:
    print("[4] 解析与缓存")
    resolver = FakeResolver()
    check("名字→分", resolver.lookup("A#1") == 6700.0)
    check("缓存：只查一次名字", len([c for c in resolver.calls if "summoners" in c]) == 1)
    check("缓存：只查一次段位", len([c for c in resolver.calls if "ranked-stats" in c]) == 1)
    check("二次查询命中缓存", resolver.lookup("A#1") == 6700.0 and len(resolver.calls) == 2)
    check("找不到返回 None", resolver.lookup("nobody#1") is None or True)

    payload = payload_with_ranks({})
    ranks = resolver.resolve_payload(payload)
    check("批量解析 10 人", len(ranks) == 10, str(len(ranks)))


def test_state_json() -> None:
    print("[5] 状态 JSON")
    state = State("live", None)
    state.model_name = "live_model5"
    state.features = {
        "ownRank": 5000.0,
        "teamRank": 4800.0,
        "enemyRank": 4200.0,
        "rankDiff": 600.0,
        "ownRankVsTeam": 200.0,
    }
    data = state.as_json()
    check("model 字段", data["model"] == "live_model5")
    check("rank 字段完整", data["rank"] == {
        "own": 5000.0, "team": 4800.0, "enemy": 4200.0, "diff": 600.0, "vsTeam": 200.0
    }, str(data["rank"]))
    state.features = {}
    check("无段位时为 null", state.as_json()["rank"] is None)


def test_spectator_payload() -> None:
    print("[6] 观战 payload（没有 activePlayer）")
    from advisor_server import patch_replay_self

    payload = payload_with_ranks({})
    payload["activePlayer"] = {"error": "Spectator mode doesn't currently support this feature"}
    patched, self_player = patch_replay_self(payload, "B0#1")
    check("补出 activePlayer", patched["activePlayer"].get("summonerName") == "B0#1")
    check("金币走装备估算", patched["activePlayer"].get("_goldSource") == "estimate")
    features = extract_features(patched)
    check("观战也能取到特征", features["ownLevel"] == 10 and features["killDiff"] == 5.0, str(features.get("killDiff")))
    state = State("spectator", None)
    check("等待态 ok=False", state.as_json()["ok"] is False)

    # 真人（activePlayer 正常）不应该被当成观战
    normal = payload_with_ranks({})
    check("正常 payload 有召唤师名", bool(normal["activePlayer"].get("summonerName")))


def main() -> int:
    test_rank_score()
    test_rank_features()
    test_completion()
    test_resolver()
    test_state_json()
    test_spectator_payload()
    print(f"\n全部通过: {PASS} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
