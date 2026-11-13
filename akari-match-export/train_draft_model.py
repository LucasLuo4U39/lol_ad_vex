#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""训练「选人阶段」的阵容胜率模型（只有 bp 信息，没有场上数据）。

只用 2,298 局历史对局的阵容 + 段位，学习：
- 各位置双方英雄的版本胜率差（OP.GG 16.22/KR）
- 各位置对位胜率差（OP.GG counters）
- 各位置英雄在这批高分段对局里的胜率差（留一法，避免自己算自己）
- 各位置双方段位分差（player_ranks.json × game_summaries.jsonl）

输出 `assets/draft_model.json`：标准化参数 + 逻辑回归权重 + 英雄统计表。
5 折交叉验证的 AUC 会打印出来（选人阶段信息有限，0.55–0.60 属于正常）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_features import normalize_champ  # noqa: E402

BASE = Path(__file__).resolve().parent
CHAMPS = BASE / "game_champs.json"
OPGG = BASE / "opgg_stats.json"
SUMMARIES = BASE.parent / "analysis" / "game_summaries.jsonl"
RANKS = BASE.parent / "analysis" / "player_ranks.json"
OUT = BASE / "assets" / "draft_model.json"

ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
POS = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}
DEFAULT_RANK = 4000.0
SMOOTH = 12.0


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_cohort(champs: Dict[str, dict]) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """(role, champ) -> (games, wins)，按阵营无关统计。"""
    stats: Dict[Tuple[str, str], List[float]] = {}
    for entry in champs.values():
        winner = int(entry.get("winner") or 0)
        for side in ("100", "200"):
            lineup = entry.get(side) or {}
            won = 1.0 if int(side) == winner else 0.0
            for role, champ in lineup.items():
                key = (role, normalize_champ(champ))
                bucket = stats.setdefault(key, [0.0, 0.0])
                bucket[0] += 1
                bucket[1] += won
    return {k: (v[0], v[1]) for k, v in stats.items()}


def smoothed(wins: float, games: float, prior: float = 0.5, k: float = SMOOTH) -> float:
    return (wins + k * prior) / (games + k)


def main() -> int:
    champs = load_json(CHAMPS)
    opgg = load_json(OPGG)
    ranks = load_json(RANKS)
    summaries: Dict[str, dict] = {}
    with SUMMARIES.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            summaries[str(record["gameId"])] = record

    cohort = build_cohort(champs)
    stats_op = opgg.get("stats") or {}
    matchups = opgg.get("matchups") or {}

    def op_wr(role: str, champ: str) -> float:
        entry = (stats_op.get(POS[role]) or {}).get(normalize_champ(champ))
        return float(entry["winRate"]) if entry else 0.5

    def mu_wr(role: str, own: str, enemy: str) -> float:
        entry = ((matchups.get(POS[role]) or {}).get(normalize_champ(own)) or {}).get(
            normalize_champ(enemy)
        )
        return float(entry["winRate"]) if entry else 0.5

    def cohort_wr(role: str, champ: str, exclude_game: str | None) -> float:
        games, wins = cohort.get((role, normalize_champ(champ)), (0.0, 0.0))
        if exclude_game is not None:
            entry = champs.get(exclude_game) or {}
            winner = int(entry.get("winner") or 0)
            for side in ("100", "200"):
                if normalize_champ((entry.get(side) or {}).get(role, "")) == normalize_champ(champ):
                    games -= 1
                    wins -= 1.0 if int(side) == winner else 0.0
        return smoothed(wins, games)

    def rank_of(game_id: str, role: str, team: int) -> float:
        record = summaries.get(game_id) or {}
        for player in record.get("players") or []:
            if int(player.get("team") or 0) != team:
                continue
            position = (player.get("position") or "").upper()
            if position and position != role:
                continue
            if not position and role != "TOP":
                continue
            return float((ranks.get(player.get("puuid")) or {}).get("score") or DEFAULT_RANK)
        return DEFAULT_RANK

    feature_names: List[str] = []
    for role in ROLES:
        feature_names += [f"opgg_{role}", f"mu_{role}", f"cohort_{role}", f"rank_{role}"]
    feature_names += [
        "rank_avg",
        "own_opgg",
        "enemy_opgg",
        "own_cohort",
        "enemy_cohort",
        "own_rank",
        "enemy_rank",
    ]

    rows = []
    labels = []
    game_ids = []
    for game_id, entry in champs.items():
        blue = entry.get("100") or {}
        red = entry.get("200") or {}
        if len(blue) < 5 or len(red) < 5:
            continue
        values = []
        rank_diffs = []
        own_opgg, enemy_opgg = [], []
        own_cohort, enemy_cohort = [], []
        own_rank, enemy_rank = [], []
        for role in ROLES:
            b, r = blue.get(role), red.get(role)
            if not b or not r:
                values.extend([0.0, 0.0, 0.0, 0.0])
                rank_diffs.append(0.0)
                continue
            b_wr, r_wr = op_wr(role, b), op_wr(role, r)
            values.append(b_wr - r_wr)
            values.append(mu_wr(role, b, r) - 0.5)
            b_co, r_co = cohort_wr(role, b, game_id), cohort_wr(role, r, game_id)
            values.append(b_co - r_co)
            b_rank = rank_of(game_id, role, 100)
            r_rank = rank_of(game_id, role, 200)
            diff = b_rank - r_rank
            values.append(diff / 100.0)
            rank_diffs.append(diff / 100.0)
            own_opgg.append(b_wr - 0.5)
            enemy_opgg.append(r_wr - 0.5)
            own_cohort.append(b_co - 0.5)
            enemy_cohort.append(r_co - 0.5)
            own_rank.append(b_rank / 1000.0)
            enemy_rank.append(r_rank / 1000.0)
        values.append(float(np.mean(rank_diffs)) if rank_diffs else 0.0)
        values.append(float(np.mean(own_opgg)) if own_opgg else 0.0)
        values.append(float(np.mean(enemy_opgg)) if enemy_opgg else 0.0)
        values.append(float(np.mean(own_cohort)) if own_cohort else 0.0)
        values.append(float(np.mean(enemy_cohort)) if enemy_cohort else 0.0)
        values.append(float(np.mean(own_rank)) if own_rank else DEFAULT_RANK / 1000.0)
        values.append(float(np.mean(enemy_rank)) if enemy_rank else DEFAULT_RANK / 1000.0)
        rows.append(values)
        labels.append(1.0 if int(entry.get("winner") or 100) == 100 else 0.0)
        game_ids.append(str(game_id))

    X = np.array(rows, dtype=np.float64)
    y = np.array(labels, dtype=np.float64)
    print(f"样本 {len(y)} 局，特征 {X.shape[1]}，蓝方胜率 {y.mean():.3f}")

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import KFold
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler

    kf = KFold(n_splits=5, shuffle=True, random_state=42)
    oof = np.zeros_like(y)
    for train_idx, valid_idx in kf.split(X):
        scaler = StandardScaler().fit(X[train_idx])
        model = LogisticRegression(max_iter=2000, C=0.5)
        model.fit(scaler.transform(X[train_idx]), y[train_idx])
        oof[valid_idx] = model.predict_proba(scaler.transform(X[valid_idx]))[:, 1]
    auc = roc_auc_score(y, oof)
    print(f"5 折 OOF AUC = {auc:.4f}")

    scaler = StandardScaler().fit(X)
    model = LogisticRegression(max_iter=2000, C=0.5).fit(scaler.transform(X), y)
    weights = {
        "features": feature_names,
        "mean": [round(float(v), 6) for v in scaler.mean_],
        "scale": [round(float(v), 6) for v in scaler.scale_],
        "coef": [round(float(v), 6) for v in model.coef_[0]],
        "intercept": round(float(model.intercept_[0]), 6),
        "auc": round(float(auc), 4),
        "rows": int(len(y)),
        "baseRate": round(float(y.mean()), 4),
        "trainedAt": "2026-11-10",
    }

    champ_stats = {}
    for (role, champ), (games, wins) in sorted(cohort.items(), key=lambda kv: -kv[1][0]):
        if games < 20:
            continue
        champ_stats.setdefault(role, {})[champ] = {
            "games": int(games),
            "wr": round(smoothed(wins, games), 4),
        }

    names = {}
    for entry in champs.values():
        for side in ("100", "200"):
            for champ in (entry.get(side) or {}).values():
                key = normalize_champ(champ)
                names.setdefault(key, champ)

    payload = {
        "model": weights,
        "champStats": champ_stats,
        "champNames": names,
        "positions": POS,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {OUT}（{OUT.stat().st_size / 1024:.0f} KB）")
    print("top features:", sorted(zip(feature_names, model.coef_[0]), key=lambda kv: -abs(kv[1]))[:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
