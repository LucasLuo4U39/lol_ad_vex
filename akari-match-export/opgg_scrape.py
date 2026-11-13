#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取 OP.GG 当前版本 (16.22) 的英雄胜率与对位胜率 + 打野路线。

- champions?position={pos} 页面（SSR 表格）：每个英雄在该位置的 胜率/登场率/禁用率
- /lol/champions/{slug}/counters/{position} 页面（SSR 列表）：该英雄对每个对位英雄的
  对位胜率 + 场次
- /lol/champions/{slug}/jungle-paths/jungle 页面（Next.js 内嵌 RSC 流）：
  该英雄在蓝/红两边的开野路线（营地顺序 + 场次 + 排名 + 首选营地率）

输出 opgg_stats.json：
{
  "patch": "16.22", "region": "kr", "fetchedAt": ...,
  "stats": {"mid": {"Syndra": {"winRate": 0.51, "pickRate": 0.05, "banRate": 0.03}}, ...},
  "matchups": {"mid": {"Syndra": {"Sylas": {"winRate": 0.5132, "games": 606}}, ...}, ...}
}

输出 assets/jungle_routes.json（--jungle-paths）：
{
  "patch": "16.22", "region": "kr", "tier": "emerald_plus", "fetchedAt": ...,
  "heroes": {"leesin": {"slug": "leesin", "blue": {...}, "red": {...}}, ...},
  "skipped": {"champKey": "原因"}, "counts": {"requested": N, "heroes": M, "skipped": K}
}
注意：OP.GG 该页面不提供逐路线胜率与营地时间戳，routes 只有 camps/games/rank。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://www.op.gg"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
POSITIONS = ("top", "jungle", "mid", "adc", "support")
PATCH = "16.22"
REGION = "kr"
TIER = "emerald_plus"

# OP.GG 页面营地代码 -> 规范 camp（<camp>_<side>，见 T52 格式对齐）
CAMP_CODE_MAP = {
    "BLUE": "blue",
    "RED": "red",
    "WOLVES": "wolves",
    "RAPTORS": "raptors",
    "GROMP": "gromp",
    "KRUG": "krugs",
}
# 小地图约定：blue 方野区在下半图、red 方野区在上半图
SIDE_HALF = {"blue": "bottom", "red": "top"}


def _soup(html: str):
    """惰性导入 bs4（--self-test 与路线抓取不依赖它）。"""
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "html.parser")

_session_pool = {}


def get(url: str, retries: int = 3, timeout: float = 20.0) -> Optional[str]:
    import threading

    tid = threading.get_ident()
    opener = _session_pool.get(tid)
    if opener is None:
        opener = urllib.request.build_opener()
        opener.addheaders = list(UA.items())
        _session_pool[tid] = opener
    for attempt in range(retries):
        try:
            with opener.open(url, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as error:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  fetch 失败 {url}: {type(error).__name__}", file=sys.stderr)
                return None
            time.sleep(1.0 * (attempt + 1))
    return None


def parse_tier_page(html: str) -> List[Dict[str, object]]:
    soup = _soup(html)
    rows = []
    for tr in soup.select("table tbody tr"):
        link = tr.select_one("a[href*='/build/']")
        name_el = tr.select_one("a strong")
        if not link or not name_el:
            continue
        href = link.get("href", "")
        match = re.search(r"/lol/champions/([^/]+)/build/([a-z]+)", href)
        if not match:
            continue
        slug, pos = match.group(1), match.group(2)
        nums = [td.get_text(strip=True) for td in tr.select("td.tabular-nums")]
        if len(nums) < 2:
            continue
        try:
            win_rate = float(nums[0].rstrip("%")) / 100.0
            pick_rate = float(nums[1].rstrip("%")) / 100.0
        except ValueError:
            continue
        rows.append(
            {
                "name": name_el.get_text(strip=True),
                "slug": slug,
                "position": pos,
                "winRate": win_rate,
                "pickRate": pick_rate,
            }
        )
    return rows


def parse_counters_page(html: str) -> List[Dict[str, object]]:
    soup = _soup(html)
    rows = []
    for li in soup.select("li.border-b"):
        img = li.select_one("img[alt]")
        name = img.get("alt") if img else None
        strong = li.select_one("strong")
        spans = li.select("span.text-gray-600")
        if not name or not strong or not spans:
            continue
        try:
            win_rate = float(strong.get_text(strip=True).rstrip("%")) / 100.0
            games = int(spans[0].get_text(strip=True).replace(",", ""))
        except (ValueError, IndexError):
            continue
        rows.append({"name": name, "winRate": win_rate, "games": games})
    return rows


def normalize(name: str) -> str:
    """名字归一化：去非字母数字 + 小写 + 别名映射（OP.GG slug 与 SGP championName 对齐）。"""
    aliases = {
        "monkeyking": "wukong",
        "nunu&willump": "nunu",
        "renataglasc": "renata",
        "chogath": "chogath",
    }
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return aliases.get(normalized, normalized)


def scrape(output: Path, workers: int = 10, patch: str = PATCH) -> int:
    stats: Dict[str, Dict[str, Dict[str, float]]] = {}
    champion_rows: Dict[str, List[Dict[str, object]]] = {}

    print("1/2 抓取各位置英雄胜率页 ...")
    for pos in POSITIONS:
        url = f"{BASE}/champions?region={REGION}&position={pos}"
        html = get(url)
        if not html:
            print(f"  位置 {pos} 页面失败")
            continue
        rows = parse_tier_page(html)
        champion_rows[pos] = rows
        stats.setdefault(pos, {})
        for row in rows:
            stats[pos][normalize(row["name"])] = {
                "winRate": row["winRate"],
                "pickRate": row["pickRate"],
            }
        print(f"  {pos}: {len(rows)} 个英雄")

    print("2/2 抓取对位胜率页 ...")
    tasks = []
    for pos, rows in champion_rows.items():
        for row in rows:
            tasks.append((pos, row["slug"]))

    matchups: Dict[str, Dict[str, Dict[str, Dict[str, object]]]] = {pos: {} for pos in POSITIONS}
    done = 0

    def fetch_one(task):
        pos, slug = task
        html = get(f"{BASE}/lol/champions/{slug}/counters/{pos}?region={REGION}")
        rows = parse_counters_page(html) if html else []
        return pos, slug, rows

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for pos, slug, rows in pool.map(fetch_one, tasks):
            done += 1
            subject = normalize(slug)
            matchups[pos].setdefault(subject, {})
            for row in rows:
                enemy = normalize(row["name"])
                if enemy == subject:
                    continue
                matchups[pos][subject][enemy] = {
                    "winRate": row["winRate"],
                    "games": row["games"],
                }
            if done % 50 == 0:
                print(f"  {done}/{len(tasks)}")

    result = {
        "source": "op.gg",
        "patch": patch,
        "region": REGION,
        "fetchedAt": datetime.now().astimezone().isoformat(),
        "stats": stats,
        "matchups": matchups,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成: {output} ({output.stat().st_size/1024:.0f} KB)")
    return 0


def check_output(output: Path, expected_patch: Optional[str] = None) -> int:
    """加载侧校验：检查 opgg_stats.json 的 patch/fetchedAt 并打印告警。

    供抓取后自检（python opgg_scrape.py --check）；实时推理侧的加载告警
    用 live_features.validate_opgg_stats（纯 stdlib，可被 advisor_server 引用）。
    """
    from live_features import validate_opgg_stats

    if not output.is_file():
        print(f"[检查] {output} 不存在")
        return 1
    try:
        opgg = json.loads(output.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        print(f"[检查] {output} 读取失败: {error}")
        return 1
    warnings = validate_opgg_stats(opgg, current_patch=expected_patch)
    if warnings:
        for warning in warnings:
            print(f"[告警] {warning}")
        return 1
    patch = opgg.get("patch")
    fetched_at = opgg.get("fetchedAt")
    print(f"[检查] {output} OK（patch={patch} fetchedAt={fetched_at}）")
    return 0


def parse_jungle_paths(html: str) -> Optional[Dict[str, object]]:
    """解析 jungle-paths 页面内嵌 RSC 流，提取蓝/红两边开野路线。

    页面数据形如（转义在 self.__next_f.push 的字符串字面量里）：
      {"teamName":"BLUE","data":{"team_key":"BLUE","routes":[
         {"route":["RAPTORS","RED","KRUG","WOLVES","BLUE","GROMP"],"play":71629,"rank":1},...],
       "play":223562},"rate":{"team_key":"BLUE","blue":26,"red":10,"raptors":70},"mode":"ranked"}

    返回 {side: {"totalGames": int, "firstCampRates": {...}, "routes": [...], "mode": str}}
    或 None（未找到/结构不符）。注意：该页面无逐营地时间戳与逐路线胜率。
    """
    text = html.replace('\\"', '"')
    result: Dict[str, object] = {}

    for team in ("BLUE", "RED"):
        match = re.search(
            r'"teamName":"' + team + r'","data":(\{.*?\}),"rate":(\{.*?\}),"mode":"(\w+)"',
            text,
            re.S,
        )
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            rate = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue

        routes: List[Dict[str, object]] = []
        for item in data.get("routes") or []:
            if not isinstance(item, dict):
                continue
            camps = [str(c) for c in (item.get("route") or [])]
            routes.append(
                {
                    "camps": camps,
                    "games": int(item.get("play") or 0),
                    "rank": int(item.get("rank") or 0),
                }
            )
        if not routes:
            continue

        result[team.lower()] = {
            "totalGames": int(data.get("play") or 0),
            "mode": match.group(3),
            "firstCampRates": {
                str(key): int(value)
                for key, value in rate.items()
                if key != "team_key" and isinstance(value, (int, float))
            },
            "routes": routes,
        }

    return result or None


def build_routes_output(
    champ_key: str,
    slug: str,
    parsed: Dict[str, object],
    top_routes: int,
) -> Dict[str, object]:
    """把 parse_jungle_paths 的结果转成 T52 约定的 route 结构（zone=<camp>_<side>）。

    返回 {"slug", "blue", "red", "routes"}；blue/red 为裁剪到 top_routes 的
    侧信息（totalGames/firstCampRates/routes），避免全量 124+ 条路线撑大文件。
    """
    routes: List[Dict[str, object]] = []
    sides: Dict[str, object] = {}

    for side in ("blue", "red"):
        side_data = parsed.get(side)
        if not isinstance(side_data, dict):
            continue
        top = (side_data.get("routes") or [])[:top_routes]
        for item in top:
            steps = [
                {"zone": f"{CAMP_CODE_MAP.get(c, c)}_{side}", "t": None}
                for c in item["camps"]
            ]
            routes.append(
                {
                    "id": f"{side}_r{item['rank']}",
                    "side": side,
                    "games": item["games"],
                    "rank": item["rank"],
                    "steps": steps,
                }
            )
        sides[side] = {
            "totalGames": side_data.get("totalGames"),
            "mode": side_data.get("mode"),
            "firstCampRates": side_data.get("firstCampRates"),
            "routes": top,
        }

    return {"slug": slug, **sides, "routes": routes}


def _jungle_slugs_from_page(html: str) -> List[str]:
    """从 champions?position=jungle 页面提取打野英雄 slug 列表。

    优先复用 bs4 的 parse_tier_page；无 bs4 时回退到 href 正则（等效于
    parse_tier_page 的链接过滤），保证批量抓取不依赖 bs4。
    """
    try:
        rows = parse_tier_page(html)
        slugs = [str(row["slug"]) for row in rows]
        if slugs:
            return slugs
    except Exception:  # noqa: BLE001
        pass
    slugs = re.findall(r"/lol/champions/([^/\"'?]+)/build/jungle", html)
    return list(dict.fromkeys(slugs))


def scrape_jungle_routes(
    output: Path,
    champs: Optional[List[str]] = None,
    workers: int = 6,
    patch: str = PATCH,
    tier: str = TIER,
    top_routes: int = 20,
    limit: Optional[int] = None,
) -> int:
    """抓取全部（或指定）打野英雄的 jungle-paths 页面，输出 T52 格式 json。"""
    if champs is None:
        print("获取打野英雄列表 ...")
        html = get(f"{BASE}/champions?region={REGION}&position=jungle")
        if not html:
            print("[错误] 打野位置页抓取失败", file=sys.stderr)
            return 1
        champs = _jungle_slugs_from_page(html)
    # 去重保序
    champs = list(dict.fromkeys(champs))
    if limit:
        champs = champs[:limit]

    print(f"目标打野英雄: {len(champs)} 个（tier={tier} patch={patch} top_routes={top_routes}）")

    heroes: Dict[str, object] = {}
    skipped: Dict[str, str] = {}

    def fetch_one(slug: str):
        url = (
            f"{BASE}/lol/champions/{slug}/jungle-paths/jungle"
            f"?region={REGION}&tier={tier}&patch={patch}"
        )
        html = get(url)
        if not html:
            return slug, None, "页面抓取失败"
        parsed = parse_jungle_paths(html)
        if not parsed:
            return slug, None, "未解析到路线数据（页面结构变化或非打野英雄）"
        return slug, parsed, None

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for slug, parsed, reason in pool.map(fetch_one, champs):
            done += 1
            key = normalize(slug)
            if parsed is not None:
                heroes[key] = build_routes_output(key, slug, parsed, top_routes)
            else:
                skipped[key] = reason or "未知原因"
            if done % 10 == 0 or done == len(champs):
                print(f"  {done}/{len(champs)}（成功 {len(heroes)} 跳过 {len(skipped)}）")

    camps: Dict[str, Dict[str, str]] = {
        f"{camp}_{side}": {"camp": camp, "side": side, "half": SIDE_HALF[side]}
        for camp in CAMP_CODE_MAP.values()
        for side in ("blue", "red")
    }

    result = {
        "meta": {
            "source": "op.gg",
            "patch": patch,
            "region": REGION,
            "tier": tier,
            "fetchedAt": datetime.now().astimezone().isoformat(),
            "topRoutes": top_routes,
            "zoneKey": "<camp>_<side>",
            "campCodeMap": CAMP_CODE_MAP,
            "notes": [
                "t 恒为 null：OP.GG jungle-paths 页面只提供营地顺序+场次(play)+排名(rank)，无逐营地时间戳",
                "无逐路线胜率；games=该路线样本局数(play)",
                "side 取自页面 team_key，即该阵营视角的开野路线（营地均在己方野区）",
                "half 按小地图约定：blue 方野区=bottom，red 方野区=top",
            ],
        },
        "camps": camps,
        "routes": {key: value["routes"] for key, value in heroes.items()},
        "heroes": heroes,
        "skipped": skipped,
        "counts": {
            "requested": len(champs),
            "heroes": len(heroes),
            "skipped": len(skipped),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"完成: {output}（成功 {len(heroes)} / 跳过 {len(skipped)}）")
    for key, reason in sorted(skipped.items()):
        print(f"  [跳过] {key}: {reason}")
    return 0


def check_jungle_routes(output: Path, expected_patch: Optional[str] = None) -> int:
    """加载侧校验：jungle_routes.json 的 patch/fetchedAt/缺失计数。"""
    if not output.is_file():
        print(f"[检查] {output} 不存在")
        return 1
    try:
        data = json.loads(output.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        print(f"[检查] {output} 读取失败: {error}")
        return 1

    warnings: List[str] = []
    meta = data.get("meta") or {}
    if expected_patch and meta.get("patch") != expected_patch:
        warnings.append(f"patch={meta.get('patch')} != 期望 {expected_patch}")
    if not meta.get("fetchedAt"):
        warnings.append("缺少 fetchedAt")
    routes = data.get("routes") or {}
    skipped = data.get("skipped") or {}
    if not routes:
        warnings.append("routes 为空")
    if skipped:
        warnings.append(f"{len(skipped)} 个英雄被跳过: {list(skipped)[:5]}...")
    for warning in warnings:
        print(f"[告警] {warning}")
    if warnings:
        return 1
    print(
        f"[检查] {output} OK（patch={meta.get('patch')} fetchedAt={meta.get('fetchedAt')} "
        f"heroes={len(routes)} skipped={len(skipped)}）"
    )
    return 0


def self_test() -> int:
    """离线自检：合成 jungle-paths 页面片段验证解析与 T52 格式转换。"""
    payload = {
        "teamName": "BLUE",
        "data": {
            "team_key": "BLUE",
            "routes": [
                {"route": ["RAPTORS", "RED", "KRUG", "WOLVES", "BLUE", "GROMP"],
                 "play": 71629, "rank": 1},
                {"route": ["BLUE", "GROMP", "WOLVES", "RAPTORS", "RED", "KRUG"],
                 "play": 51958, "rank": 2},
            ],
            "play": 223562,
        },
        "rate": {"team_key": "BLUE", "blue": 26, "red": 10, "raptors": 70},
        "mode": "ranked",
    }
    red_payload = {
        "teamName": "RED",
        "data": {
            "team_key": "RED",
            "routes": [
                {"route": ["RAPTORS", "RED", "KRUG", "WOLVES", "BLUE", "GROMP"],
                 "play": 71842, "rank": 1},
            ],
            "play": 207835,
        },
        "rate": {"team_key": "RED", "blue": 11, "red": 13, "raptors": 76},
        "mode": "ranked",
    }

    def wrap(obj):
        escaped = json.dumps(obj, separators=(",", ":")).replace('"', '\\"')
        return (
            "<script>self.__next_f.push([1,\"6d:["
            + escaped
            + "]\n\"])</script>"
        )

    html = wrap(payload) + wrap(red_payload)

    failures: List[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(("PASS  " if condition else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
        if not condition:
            failures.append(name)

    parsed = parse_jungle_paths(html)
    check("解析出 blue/red 两侧", parsed is not None and set(parsed) == {"blue", "red"},
          str(list(parsed or {})))
    if parsed:
        blue = parsed["blue"]
        check("blue totalGames", blue["totalGames"] == 223562, str(blue["totalGames"]))
        check("blue 首条路线营地为 6 个", len(blue["routes"][0]["camps"]) == 6)
        check("blue rank1 games", blue["routes"][0]["games"] == 71629)
        check("blue firstCampRates", blue["firstCampRates"] == {"blue": 26, "red": 10, "raptors": 70},
              str(blue["firstCampRates"]))

    check("空页面解析返回 None", parse_jungle_paths("<html>no data</html>") is None)
    check("结构损坏页面返回 None", parse_jungle_paths(wrap({"teamName": "BLUE"})) is None)

    out = build_routes_output("leesin", "leesin", parsed or {}, top_routes=20)
    routes = out["routes"]
    check("T52 routes 条数（1+2）", len(routes) == 3, str(len(routes)))
    if routes:
        first = routes[0]
        check("id 命名", first["id"] == "blue_r1", str(first["id"]))
        check("zone 规范键", first["steps"][0]["zone"] == "raptors_blue",
              str(first["steps"][0]))
        check("t 恒为 null", first["steps"][0]["t"] is None)
        check("games=play", first["games"] == 71629)
        red_first = next(r for r in routes if r["side"] == "red")
        check("red side zone", red_first["steps"][0]["zone"] == "raptors_red",
              str(red_first["steps"][0]))

    if failures:
        print(f"\nFAILURES: {failures}")
        return 1
    print("\nOPGG SELF-TEST PASSED")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="抓取 OP.GG 英雄/对位胜率 + 打野路线")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "opgg_stats.json")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--patch", default=PATCH, help=f"目标版本，默认 {PATCH}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="不抓取，只加载校验 --output 的 patch/fetchedAt 并告警",
    )
    parser.add_argument(
        "--jungle-paths",
        action="store_true",
        help="抓取打野路线（默认全部打野英雄），输出 --routes-output",
    )
    parser.add_argument(
        "--champs",
        default=None,
        help="只抓指定英雄（逗号分隔 slug，如 leesin,graves,viego），默认全部打野",
    )
    parser.add_argument(
        "--routes-output",
        type=Path,
        default=Path(__file__).resolve().parent / "assets" / "jungle_routes.json",
        help="打野路线输出文件",
    )
    parser.add_argument("--top-routes", type=int, default=20, help="每侧保留的前 N 条路线")
    parser.add_argument("--limit", type=int, default=None, help="只抓前 N 个英雄（调试用）")
    parser.add_argument("--tier", default=TIER, help=f"OP.GG tier 参数，默认 {TIER}")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="离线自检（合成页面数据，不发网络请求）",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.jungle_paths:
        champs = (
            [c.strip() for c in args.champs.split(",") if c.strip()]
            if args.champs
            else None
        )
        return scrape_jungle_routes(
            args.routes_output,
            champs=champs,
            workers=args.workers,
            patch=args.patch,
            tier=args.tier,
            top_routes=args.top_routes,
            limit=args.limit,
        )
    if args.check:
        return check_output(args.output, expected_patch=args.patch)
    return scrape(args.output, args.workers, args.patch)


if __name__ == "__main__":
    raise SystemExit(main())
