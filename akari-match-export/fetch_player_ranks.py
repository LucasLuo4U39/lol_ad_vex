#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓每名玩家的当前排位（tier/division/LP），折算成可比较的分数。

对 2,298 局里出现过的 puuid 逐个调用 LCU `/lol-ranked/v1/ranked-stats/{puuid}`，
输出 `analysis/player_ranks.json`：{puuid: {tier, division, lp, score}}。

分数规则（单调即可，量级取整方便看）：
    IRON 0 / BRONZE 500 / SILVER 1000 / GOLD 1500 / PLATINUM 2000 /
    EMERALD 2500 / DIAMOND 3000（+ 小段 100×k）/ MASTER 4000 / GM 5000 / CHALLENGER 6000（+ LP）

复用公共客户端模块 lcu_client（三路 LCU 认证发现，--lcu-port/--lcu-token
可手动覆盖）。LCU 需在线。

D3：每次抓取成功、写完 `player_ranks.json` 后，把本次**新抓到**的记录追加到
`~/.lol-advisor/rank_snapshots/YYYY-MM-DD.jsonl`（每行带 fetchedAt 的段位快照），
为「赛后快照 vs 赛前实时」的时点问题留下历史。JSONL 只追加、不覆盖，单日
文件超过 50MB 就跳过并警告；写快照失败绝不影响抓取结果。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from lcu_client import LcuAuth, LcuClient, LcuError, discover_lcu_auth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
SUMMARIES = BASE.parent / "analysis" / "game_summaries.jsonl"
OUT = BASE.parent / "analysis" / "player_ranks.json"
# D3：段位历史快照（只追加，一天一个文件；用于事后还原「某时点」的段位）。
SNAPSHOT_ROOT = Path.home() / ".lol-advisor" / "rank_snapshots"
SNAPSHOT_MAX_BYTES = 50 * 1024 * 1024  # 单日文件上限，超过就不再追加

TIER_BASE = {
    "IRON": 0,
    "BRONZE": 500,
    "SILVER": 1000,
    "GOLD": 1500,
    "PLATINUM": 2000,
    "EMERALD": 2500,
    "DIAMOND": 3000,
    "MASTER": 4000,
    "GRANDMASTER": 5000,
    "CHALLENGER": 6000,
}
DIVISION_STEP = {"IV": 0, "III": 100, "II": 200, "I": 300, "NA": 0, "": 0}


def rank_score(tier: str, division: str, lp: int) -> int:
    tier = (tier or "").upper()
    base = TIER_BASE.get(tier, 0)
    if tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return base + int(lp)
    return base + DIVISION_STEP.get((division or "NA").upper(), 0) + int(lp) // 10


def snapshot_path(
    when: Optional[datetime] = None, root: Optional[Path] = None
) -> Path:
    """当日快照文件路径：<root>/YYYY-MM-DD.jsonl（按本地日期分文件）。"""
    directory = Path(root) if root is not None else SNAPSHOT_ROOT
    stamp = when or datetime.now()
    return directory / f"{stamp:%Y-%m-%d}.jsonl"


def append_rank_snapshot(
    records: Dict[str, Dict[str, Any]],
    fetched_at: Optional[datetime] = None,
    root: Optional[Path] = None,
    max_bytes: Optional[int] = None,
) -> Optional[Path]:
    """把本次成功抓到的段位追加进当日 jsonl（每行一个带时间戳的玩家快照）。

    D3 根因：抓取侧一直只存「当前值」（player_ranks.json），没有历史，导致
    训练用赛后快照段位、线上用赛前实时段位时不可追溯。这里只做**追加**：
    每行 {"fetchedAt": ISO, "puuid", "tier", "division", "lp", "score"}。

    - 只追加不覆盖：player_ranks.json 的缓存格式与返回值都不动；
    - 只记本次新抓到的记录：全命中缓存时没有新观测，不追加（不伪造时间点）；
    - 任何异常（目录不可写、磁盘满、超上限）只打警告并返回 None，绝不让抓取失败；
    - 当日文件已 >= max_bytes（默认 SNAPSHOT_MAX_BYTES=50MB）时跳过并警告；
    - root/max_bytes 可注入，便于自测写到临时目录。
    """
    if not records:
        return None
    limit = SNAPSHOT_MAX_BYTES if max_bytes is None else int(max_bytes)
    stamp = fetched_at or datetime.now().astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()  # naive 按本地时区理解，ISO 里带偏移
    iso = stamp.isoformat(timespec="seconds")
    path = snapshot_path(stamp, root)
    try:
        if path.is_file() and path.stat().st_size >= limit:
            print(
                f"[snapshot] 警告: {path} 已达 {limit} 字节，跳过本次快照",
                file=sys.stderr,
            )
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for puuid, record in records.items():
            if not isinstance(record, dict):
                continue
            row = {
                "fetchedAt": iso,
                "puuid": str(puuid),
                "tier": record.get("tier"),
                "division": record.get("division"),
                "lp": record.get("lp"),
                "score": record.get("score"),
            }
            lines.append(json.dumps(row, ensure_ascii=False))
        if not lines:
            return None
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        return path
    except Exception as error:  # noqa: BLE001 —— 快照永远不能影响抓取结果
        print(f"[snapshot] 警告: 写入失败 {path}: {error}", file=sys.stderr)
        return None


def fetch_rank(client: LcuClient, puuid: str) -> Optional[Dict[str, Any]]:
    try:
        payload = client.get_json(f"/lol-ranked/v1/ranked-stats/{puuid}")
    except (LcuError, TimeoutError):
        # 404 / 连接失败均视为该玩家无段位数据
        return None
    if not payload:
        return None
    entry = (payload.get("queueMap") or {}).get("RANKED_SOLO_5x5") or {}
    tier = entry.get("tier") or ""
    division = entry.get("division") or "NA"
    lp = int(entry.get("leaguePoints") or 0)
    if not tier:
        return None
    return {
        "tier": tier,
        "division": division,
        "lp": lp,
        "score": rank_score(tier, division, lp),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="抓玩家当前排位分")
    parser.add_argument("--summaries", type=Path, default=SUMMARIES)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--lcu-port", type=int, default=None)
    parser.add_argument("--lcu-token", default=None)
    args = parser.parse_args(argv)

    if args.lcu_port and args.lcu_token:
        auth = LcuAuth(port=args.lcu_port, token=args.lcu_token)
    elif args.lcu_port or args.lcu_token:
        print("--lcu-port 与 --lcu-token 必须同时提供", file=sys.stderr)
        return 1
    else:
        try:
            auth = discover_lcu_auth()
        except LcuError as error:
            print(f"找不到 LCU（客户端没开？）：{error}", file=sys.stderr)
            return 1

    client = LcuClient(auth, retries=2)

    puuids = set()
    with args.summaries.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            for player in record.get("players") or []:
                if player.get("puuid"):
                    puuids.add(player["puuid"])
    print(f"玩家 {len(puuids)} 人")

    existing: Dict[str, Any] = {}
    if args.out.is_file():
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    todo = [p for p in sorted(puuids) if p not in existing]
    print(f"已缓存 {len(existing)}，本次 {len(todo)}")

    started = time.time()
    ok = fail = 0
    fresh: Dict[str, Dict[str, Any]] = {}  # D3：本次真正抓到的，写缓存后追加快照
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_rank, client, puuid): puuid for puuid in todo}
        for index, future in enumerate(as_completed(futures), start=1):
            puuid = futures[future]
            record = future.result()
            if record:
                existing[puuid] = record
                fresh[puuid] = record
                ok += 1
            else:
                fail += 1
            if index % 500 == 0:
                print(f"  {index}/{len(todo)} ok={ok} fail={fail} {index / max(time.time() - started, 1e-9):.1f}/s")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    # D3：写缓存成功后追加历史快照（失败只警告，不影响抓取结果）。
    if fresh:
        append_rank_snapshot(fresh)

    from collections import Counter

    tiers = Counter(v.get("tier") for v in existing.values())
    print(f"完成 ok={ok} fail={fail} 用时 {(time.time() - started) / 60:.1f} 分钟")
    print("段位分布:", dict(tiers.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
