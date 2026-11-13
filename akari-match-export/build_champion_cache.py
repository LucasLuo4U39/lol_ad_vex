#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 LCU 抓英雄 id / 英文名 / 中文名，缓存到 assets/champions.json。

选人助手用它把 championId 转成可读名字（中文用 title 字段，如 103 -> 阿狸），
并给 /icon/<id>.png 提供 id 映射。抓一次即可，之后离线也能用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rank_lookup import RankResolver, discover_lcu_psutil  # noqa: E402

OUT = Path(__file__).resolve().parent / "assets" / "champions.json"


def main() -> int:
    found = discover_lcu_psutil()
    if not found:
        print("找不到 LCU（客户端没开？）")
        return 1
    resolver = RankResolver(found[0], found[1])
    summary = resolver._get("/lol-game-data/assets/v1/champion-summary.json") or []
    if not isinstance(summary, list):
        print("champion-summary 拉取失败")
        return 1

    result = {}
    for item in summary:
        cid = item.get("id")
        if not cid or cid <= 0:
            continue
        detail = resolver._get(f"/lol-game-data/assets/v1/champions/{cid}.json") or {}
        result[str(cid)] = {
            "id": int(cid),
            "alias": item.get("alias") or detail.get("alias") or "",
            "name": detail.get("title") or item.get("name") or "",
            "title": detail.get("name") or "",
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {len(result)} 个英雄 -> {OUT}")
    for cid in ("103", "64", "86"):
        if cid in result:
            print(" ", result[cid])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
