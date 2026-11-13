#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""League Akari standalone match exporter.

Exports the current account's recent League of Legends matches (summary, CSV,
optional per-game details, optional replay files) without launching the League
Akari Electron app.

It talks directly to:
  * the local League Client (LCU) REST API on 127.0.0.1:<app-port>
  * the regional SGP match-history service, using the client's entitlements token

Runtime dependencies: Python standard library plus the shared clients
(`lcu_client.py` / `sgp_client.py`, standard library only; `psutil` is used
optionally for LCU discovery).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lcu_client import (
    LcuAuth,
    LcuClient,
    LcuError,
    discover_lcu_auth,
    parse_command_line,
)
from sgp_client import (
    REGION_ALIASES,
    SGP_SERVERS,
    SgpClient,
    SgpError,
    SgpServer,
    get_sgp_server_id,
    sgp_sub_id,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APP_NAME = "League Akari"
EXPORTER_VERSION = "1.0.0"
MAX_COUNT = 200
DETAILS_CONCURRENCY = 3
REPLAY_POLL_INTERVAL = 1.0
REPLAY_DOWNLOAD_TIMEOUT = 5 * 60
DEFAULT_COUNT = 100

ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExportError(Exception):
    """A user-facing error that should stop the export."""


def log(message: str) -> None:
    print(message, flush=True)


def progress(label: str, done: int, total: int) -> None:
    print(f"\r  {label} {done}/{total}", end="", flush=True)


def finish_progress() -> None:
    print()


def sanitize_path_segment(value: str, fallback: str) -> str:
    value = ILLEGAL_FILENAME_RE.sub("_", value or "")
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value or fallback


def format_timestamp(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")


def iso_utc_from_millis(millis: Any) -> str:
    try:
        return datetime.fromtimestamp(float(millis) / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return ""


def build_csv_rows(games: List[Dict[str, Any]], puuid: str) -> List[List[Any]]:
    rows: List[List[Any]] = []
    header = [
        "gameId",
        "gameStartTime",
        "gameDurationSeconds",
        "queueId",
        "gameMode",
        "win",
        "championId",
        "championName",
        "teamId",
        "position",
        "kills",
        "deaths",
        "assists",
        "kda",
        "totalMinionsKilled",
        "goldEarned",
        "visionScore",
        "wardsPlaced",
        "totalDamageDealtToChampions",
        "totalDamageTaken",
        "doubleKills",
        "tripleKills",
        "quadraKills",
        "pentaKills",
    ]
    rows.append(header)

    for game in games:
        participant = next(
            (p for p in game.get("participants", []) if p.get("puuid") == puuid),
            None,
        )
        if not participant:
            continue

        kills = int(participant.get("kills") or 0)
        deaths = int(participant.get("deaths") or 0)
        assists = int(participant.get("assists") or 0)
        kda = (kills + assists) / max(1, deaths)

        rows.append(
            [
                game.get("gameId", ""),
                iso_utc_from_millis(game.get("gameCreation")),
                game.get("gameDuration", 0),
                game.get("queueId", 0),
                game.get("gameMode", ""),
                "true" if participant.get("win") else "false",
                participant.get("championId", 0),
                participant.get("championName", ""),
                participant.get("teamId", 0),
                participant.get("teamPosition")
                or participant.get("individualPosition")
                or "",
                kills,
                deaths,
                assists,
                f"{kda:.2f}",
                participant.get("totalMinionsKilled", 0),
                participant.get("goldEarned", 0),
                participant.get("visionScore", 0),
                participant.get("wardsPlaced", 0),
                participant.get("totalDamageDealtToChampions", 0),
                participant.get("totalDamageTaken", 0),
                participant.get("doubleKills", 0),
                participant.get("tripleKills", 0),
                participant.get("quadraKills", 0),
                participant.get("pentaKills", 0),
            ]
        )

    return rows


def write_csv(path: Path, rows: List[List[Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def find_replay_file(directory: Path, game_id: int, max_depth: int = 2) -> Optional[Path]:
    if not directory.is_dir():
        return None

    matcher = re.compile(rf"(^|[_-]){game_id}($|[_-])")

    for root, directories, files in os.walk(directory):
        relative = Path(root).relative_to(directory)
        depth = len(relative.parts)

        if depth >= max_depth:
            directories[:] = []

        for name in files:
            if not name.lower().endswith(".rofl"):
                continue
            if matcher.search(Path(name).stem):
                return Path(root) / name

    return None


class ReplayDownloader:
    def __init__(self, lcu: LcuClient, output_dir: Path):
        self._lcu = lcu
        self._output_dir = output_dir
        self._replays_dir = output_dir / "replays"

    def download_all(
        self, games: List[Dict[str, Any]], quiet: bool
    ) -> Tuple[int, int, List[Dict[str, Any]]]:
        errors: List[Dict[str, Any]] = []

        try:
            configuration = self._lcu.get_json("/lol-replays/v1/configuration")
        except Exception as error:
            errors.append(
                {"stage": "replays", "message": f"读取回放配置失败: {error}"}
            )
            return 0, len(games), errors

        if not configuration.get("isReplaysEnabled"):
            errors.append(
                {"stage": "replays", "message": "客户端当前未启用回放功能"}
            )
            return 0, len(games), errors

        try:
            rofls_path = self._lcu.get_json("/lol-replays/v1/rofls/path")
            # LCU returns either a plain string or {"path": "..."}.
            if isinstance(rofls_path, dict):
                replays_root = Path(rofls_path.get("path") or "")
            else:
                replays_root = Path(rofls_path or "")
        except Exception as error:
            errors.append(
                {"stage": "replays", "message": f"读取回放目录失败: {error}"}
            )
            return 0, len(games), errors

        self._replays_dir.mkdir(parents=True, exist_ok=True)

        downloaded = 0
        failed = 0

        for index, game in enumerate(games, start=1):
            game_id = game.get("gameId")
            if not isinstance(game_id, int):
                continue

            try:
                self._download_one(game, game_id, replays_root)
                downloaded += 1
            except Exception as error:
                failed += 1
                errors.append(
                    {
                        "stage": "replays",
                        "gameId": game_id,
                        "message": str(error),
                    }
                )
                if not quiet:
                    finish_progress()
                    log(f"  [replay] {game_id} 失败: {error}")

            if not quiet:
                progress("replays", index, len(games))

        if not quiet:
            finish_progress()

        return downloaded, failed, errors

    def _download_one(
        self, game: Dict[str, Any], game_id: int, replays_root: Path
    ) -> None:
        target = self._replays_dir / f"{game_id}.rofl"
        if target.exists() and target.stat().st_size > 0:
            return

        metadata = {
            "gameVersion": game.get("gameVersion", ""),
            "gameType": game.get("gameType", ""),
            "queueId": game.get("queueId", 0),
            "gameEnd": game.get("gameEndTimestamp", 0),
        }

        try:
            self._lcu.post_json(
                f"/lol-replays/v2/metadata/{game_id}/create",
                metadata,
                timeout=15,
            )
        except Exception:
            # Metadata may already exist; the download endpoint can still work.
            pass

        try:
            self._lcu.post_json(
                f"/lol-replays/v1/rofls/{game_id}/download",
                {"componentType": "replay-button_match-history"},
                timeout=30,
            )
        except TimeoutError:
            # The POST can stay pending until the download finishes; polling
            # below is the source of truth.
            pass

        deadline = time.monotonic() + REPLAY_DOWNLOAD_TIMEOUT
        state = None

        while time.monotonic() < deadline:
            source = find_replay_file(replays_root, game_id)
            if source:
                break

            try:
                metadata_response = self._lcu.get_json(
                    f"/lol-replays/v1/metadata/{game_id}"
                )
                state = metadata_response.get("state")
            except Exception:
                state = None

            if state == "watch":
                break
            if state == "incompatible":
                raise ExportError("回放与当前客户端版本不兼容")

            time.sleep(REPLAY_POLL_INTERVAL)

        source = find_replay_file(replays_root, game_id)
        if not source:
            raise ExportError("未找到下载后的回放文件（可能已过期或下载超时）")

        self._copy_with_retry(source, target)

    @staticmethod
    def _copy_with_retry(source: Path, target: Path) -> None:
        last_error: Optional[Exception] = None

        for _ in range(5):
            try:
                shutil.copy2(source, target)
                return
            except OSError as error:
                last_error = error
                time.sleep(0.5)

        raise ExportError(f"复制回放文件失败: {last_error}")


def fetch_details(
    sgp: SgpClient,
    sub_id: str,
    game_ids: List[int],
    details_path: Path,
    errors: List[Dict[str, Any]],
    quiet: bool,
) -> int:
    details_path.write_text("", encoding="utf-8")
    exported = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=DETAILS_CONCURRENCY) as pool:
        futures = {
            pool.submit(sgp.details, game_id, sub_id): game_id
            for game_id in game_ids
        }

        for future in as_completed(futures):
            game_id = futures[future]
            completed += 1

            try:
                details = future.result()
                with details_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"gameId": game_id, "details": details},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                exported += 1
            except Exception as error:
                errors.append(
                    {
                        "stage": "details",
                        "gameId": game_id,
                        "message": str(error),
                    }
                )
                if not quiet:
                    finish_progress()
                    log(f"  [details] {game_id} 失败: {error}")

            if not quiet:
                progress("details", completed, len(game_ids))

    if not quiet and game_ids:
        finish_progress()

    return exported


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "League Akari 独立战绩导出工具：导出当前账号最近的对局数据"
            "（可选下载回放），不依赖 League Akari 运行。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"导出最近多少场（1-{MAX_COUNT}）",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="不导出每场对局详情（details.jsonl）",
    )
    parser.add_argument(
        "--no-timeline",
        action="store_true",
        help="不生成 timeline/（事件 + 金币/经验）",
    )
    parser.add_argument(
        "--replays",
        action="store_true",
        help="额外下载回放文件（.rofl），体积可能很大",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出父目录，默认是系统下载目录/LeagueAkari",
    )
    parser.add_argument("--puuid", default=None, help="指定 PUUID，默认当前账号")
    parser.add_argument(
        "--server",
        default=None,
        help="手动指定 SGP 服务器 ID，例如 TENCENT_TJ100 / NA1",
    )
    parser.add_argument("--region", default=None, help="手动指定 LCU region")
    parser.add_argument("--lcu-port", type=int, default=None, help="手动指定 LCU 端口")
    parser.add_argument("--lcu-token", default=None, help="手动指定 LCU remoting token")
    parser.add_argument(
        "--insecure-sgp",
        action="store_true",
        help="忽略 SGP 的 TLS 证书校验（仅在证书异常时使用）",
    )
    parser.add_argument("--quiet", action="store_true", help="减少进度输出")
    return parser.parse_args(argv)


def resolve_output_dir(parent: Optional[Path], game_name: str, tag_line: str) -> Path:
    base = parent or (Path.home() / "Downloads" / "LeagueAkari")
    name = sanitize_path_segment(game_name, "summoner")
    tag = sanitize_path_segment(tag_line, "unknown")
    return base / f"LeagueAkari-{name}-{tag}-{format_timestamp()}"


def run(args: argparse.Namespace) -> int:
    count = max(1, min(MAX_COUNT, args.count))

    log(f"{APP_NAME} 独立战绩导出工具 v{EXPORTER_VERSION}")
    log("")

    if args.lcu_port and args.lcu_token:
        auth = LcuAuth(
            port=args.lcu_port,
            token=args.lcu_token,
            region=args.region or "",
            rso_platform_id="",
        )
    else:
        log("[1/4] 查找英雄联盟客户端...")
        auth = discover_lcu_auth()
        if args.region:
            auth.region = args.region

    log(f"      LCU 端口 {auth.port}，region={auth.region or '未知'}")

    lcu = LcuClient(auth)

    try:
        summoner = lcu.get_json("/lol-summoner/v1/current-summoner")
    except Exception as error:
        raise ExportError(f"读取当前召唤师失败: {error}") from error

    puuid = args.puuid or summoner.get("puuid")
    if not puuid:
        raise ExportError("无法获取当前账号的 PUUID，请确认客户端已登录")

    game_name = summoner.get("gameName") or summoner.get("displayName") or "unknown"
    tag_line = summoner.get("tagLine") or ""

    sgp_server_id = args.server or get_sgp_server_id(
        auth.region, auth.rso_platform_id
    )
    if not sgp_server_id:
        raise ExportError(
            "无法确定 SGP 服务器。请用 --server 手动指定，例如 TENCENT_TJ100 或 NA1。"
        )

    server = SGP_SERVERS.get(sgp_server_id)
    if not server:
        supported = ", ".join(sorted(SGP_SERVERS))
        raise ExportError(
            f"暂不支持 SGP 服务器 {sgp_server_id}。可用值: {supported}"
        )

    try:
        entitlements = lcu.get_json("/entitlements/v1/token")
    except Exception as error:
        raise ExportError(f"读取 entitlements token 失败: {error}") from error

    access_token = entitlements.get("accessToken")
    if not access_token:
        raise ExportError("SGP entitlements token 为空，请稍后重试")

    sub_id = sgp_sub_id(sgp_server_id, server)

    log(
        f"      召唤师 {game_name}#{tag_line}  "
        f"SGP={sgp_server_id}  subId={sub_id}"
    )

    output_dir = resolve_output_dir(args.output, game_name, tag_line)
    output_dir.mkdir(parents=True, exist_ok=True)

    log("")
    log(f"[2/4] 获取最近 {count} 场战绩...")

    sgp = SgpClient(server, access_token, insecure=args.insecure_sgp)
    summary = sgp.summary(puuid, count)
    games = [
        game
        for game in (summary.get("games") or [])
        if isinstance(game, dict) and isinstance(game.get("json"), dict)
    ]

    if not games:
        raise ExportError("SGP 未返回任何战绩（可能该账号没有对局记录）")

    summary_path = output_dir / "summary.json"
    write_json(
        summary_path,
        {
            "puuid": puuid,
            "gameName": game_name,
            "tagLine": tag_line,
            "region": auth.region,
            "rsoPlatformId": auth.rso_platform_id,
            "sgpServerId": sgp_server_id,
            "exportedAt": datetime.now(tz=timezone.utc).isoformat(),
            "count": len(games),
            "games": games,
        },
    )

    csv_rows = build_csv_rows([game["json"] for game in games], puuid)
    write_csv(output_dir / "matches.csv", csv_rows)

    log(f"      实际返回 {len(games)} 场")
    log(f"      -> {summary_path}")
    log(f"      -> {output_dir / 'matches.csv'}")

    errors: List[Dict[str, Any]] = []
    details_count = 0
    replays_downloaded = 0
    replays_failed = 0

    if not args.no_details:
        log("")
        log(f"[3/4] 获取对局详情（并发 {DETAILS_CONCURRENCY}）...")
        details_count = fetch_details(
            sgp,
            sub_id,
            [game["json"]["gameId"] for game in games],
            output_dir / "details.jsonl",
            errors,
            args.quiet,
        )
        log(f"      详情完成 {details_count}/{len(games)}")

        if not args.no_timeline:
            log("")
            log("[timeline] 生成事件 + 金币/经验时间线...")

            try:
                import build_timeline
            except Exception as error:
                errors.append({"stage": "timeline", "message": str(error)})
                log(f"  [timeline] 失败: {error}")
            else:
                build_timelines_for_export(build_timeline, output_dir, games, errors)

    if args.replays:
        log("")
        log("[4/4] 下载回放...")
        downloader = ReplayDownloader(lcu, output_dir)
        replays_downloaded, replays_failed, replay_errors = downloader.download_all(
            [game["json"] for game in games], args.quiet
        )
        errors.extend(replay_errors)
        log(
            f"      回放完成 {replays_downloaded}，失败 {replays_failed}"
        )

    manifest = {
        "app": f"{APP_NAME} standalone match exporter",
        "version": EXPORTER_VERSION,
        "exportedAt": datetime.now(tz=timezone.utc).isoformat(),
        "puuid": puuid,
        "gameName": game_name,
        "tagLine": tag_line,
        "region": auth.region,
        "rsoPlatformId": auth.rso_platform_id,
        "sgpServerId": sgp_server_id,
        "requestedCount": count,
        "summaryCount": len(games),
        "detailsCount": details_count,
        "replaysDownloaded": replays_downloaded,
        "replaysFailed": replays_failed,
        "includeDetails": not args.no_details,
        "includeReplays": bool(args.replays),
        "errors": errors,
    }
    write_json(output_dir / "manifest.json", manifest)

    if errors:
        write_json(output_dir / "errors.json", errors)

    total_size = sum(
        item.stat().st_size for item in output_dir.rglob("*") if item.is_file()
    )

    log("")
    log("完成。")
    log(f"  输出目录: {output_dir}")
    log(f"  对局数量: {len(games)}")
    log(f"  详情数量: {details_count}")
    log(f"  回放数量: {replays_downloaded}（失败 {replays_failed}）")
    log(f"  总大小:   {total_size / (1024 * 1024):.1f} MB")

    if errors:
        log(f"  错误数量: {len(errors)}（见 errors.json）")

    return 0


def build_timelines_for_export(
    build_timeline: Any,
    output_dir: Path,
    games: List[Dict[str, Any]],
    errors: List[Dict[str, Any]],
) -> None:
    """为导出的每一局生成时间线。

    单局：保持旧行为，输出到 <export>/timeline/；
    多局：每局一个 game_<gameId>/ 子目录（含单局 summary.json、details.jsonl
    和 timeline/），与 build_dataset.find_exports 的目录契约一致，
    且避免同一局被重复计数。
    """
    summary_path = output_dir / "summary.json"
    details_path = output_dir / "details.jsonl"

    if len(games) <= 1:
        try:
            build_timeline.build(
                summary_path, details_path, output_dir / "timeline", None, True
            )
        except Exception as error:
            errors.append({"stage": "timeline", "message": str(error)})
            log(f"  [timeline] 失败: {error}")
        return

    records: Dict[int, Dict[str, Any]] = {}
    try:
        with details_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                game_id = record.get("gameId")
                if isinstance(game_id, int):
                    records[game_id] = record
    except OSError as error:
        errors.append({"stage": "timeline", "message": str(error)})
        return

    # 多局场景只探测/拉取一次物品名，避免每局重复 LCU 调用
    # （fetch_item_names 内部失败时返回空 dict，不抛出）。
    item_names = build_timeline.fetch_item_names()

    total = len(games)
    for index, game_item in enumerate(games, start=1):
        game = game_item.get("json") or {}
        game_id = game.get("gameId")
        if not isinstance(game_id, int):
            continue

        record = records.get(game_id)
        if not record:
            errors.append(
                {
                    "stage": "timeline",
                    "gameId": game_id,
                    "message": "details.jsonl 中没有该局详情（详情抓取失败？）",
                }
            )
            log(f"  [timeline] game {game_id} 跳过（无详情）")
            continue

        folder = output_dir / f"game_{game_id}"
        folder.mkdir(parents=True, exist_ok=True)
        single_summary = folder / "summary.json"
        single_details = folder / "details.jsonl"
        single_summary.write_text(
            json.dumps({"games": [{"json": game}]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        single_details.write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        try:
            build_timeline.build(
                single_summary,
                single_details,
                folder / "timeline",
                game_id,
                True,
                item_names,
            )
        except Exception as error:
            errors.append(
                {"stage": "timeline", "gameId": game_id, "message": str(error)}
            )
            log(f"  [timeline] game {game_id} 失败: {error}")
            continue

        if index % 10 == 0 or index == total:
            log(f"  [timeline] {index}/{total} 局完成")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        return run(args)
    except (ExportError, LcuError, SgpError) as error:
        print("", file=sys.stderr)
        print(f"[错误] {error}", file=sys.stderr)
        return 1
    except ssl.SSLError as error:
        print("", file=sys.stderr)
        print(
            f"[错误] TLS 证书校验失败: {error}\n"
            "如果是 SGP 证书问题，可加 --insecure-sgp 重试。",
            file=sys.stderr,
        )
        return 1
    except (ConnectionError, TimeoutError) as error:
        print("", file=sys.stderr)
        print(f"[错误] 网络请求失败: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        print("[已取消] 已保留当前已完成的文件。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
