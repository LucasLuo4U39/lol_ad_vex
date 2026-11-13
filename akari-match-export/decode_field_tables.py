#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Correlate ROFL keyframe packet fields with known SGP 1-minute values.

For every (netId, packetId, block-sequence) we try simple decodings
(u8/u16/u32/u64/f32/f64, LE/BE, byte offsets) and score Pearson
correlation against known per-participant series:
  x, y, level, currentGold, totalGold, xp, cs

【已判死（16.22）】本工具是探索/复现用，不作为任何下游数据来源：
- 单局能出 r≈0.9 的候选，但换同 patch 第二场全部对不上（特征/玩家均不匹配），
  是单调趋势造成的假阳性（见 analysis/akari-friend-replay-unpacked/rofl-packet-notes.md）；
- 16.22 的 keyframe payload 经过逐字节变换（XOR/置换），且 exe 被 ACE 壳加密，
  rofl-parser 式静态 decoder 无法生成，字段级解码（x/y/金币）暂无可行路径。

输入要求：--blocks 必须用 dump_rofl_blocks.py 生成的 blocks_full.jsonl.gz
（含完整 payloadHex）；unpack_replay.py 的 blocks.jsonl.gz 只有 64 字节预览，
本脚本会直接报错。
"""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PACKETS = (772, 555, 798, 576, 734, 769)

FEATURES = ("x", "y", "level", "currentGold", "totalGold", "xp", "cs")


def load_blocks(path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if "payloadHex" not in row:
                raise ValueError(
                    "blocks 文件缺少 payloadHex 字段。请使用 dump_rofl_blocks.py "
                    "生成的 blocks_full.jsonl.gz（unpack_replay.py 的 "
                    "blocks.jsonl.gz 只有前 64 字节预览 payloadHexPreview）。"
                )
            if row["chunkType"] != "keyframe":
                continue
            if row["packetId"] not in PACKETS:
                continue
            row["payload"] = bytes.fromhex(row["payloadHex"])
            rows.append(row)
    return rows


def chunk_minutes(rows: List[Dict[str, Any]]) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for row in rows:
        if row["chunkId"] not in result:
            result[row["chunkId"]] = round(row["timestamp"] / 60.0)
    return result


def sgp_series(timeline_path: str) -> Dict[str, Dict[str, List[Optional[float]]]]:
    timeline = json.load(open(timeline_path, encoding="utf-8"))
    frames = timeline["frames"]
    participants = defaultdict(lambda: {feature: [None] * len(frames) for feature in FEATURES})

    for minute, frame in enumerate(frames):
        for pid, state in frame["participants"].items():
            for feature in FEATURES:
                participants[pid][feature][minute] = state.get(feature)

    return participants


DECODERS: List[Tuple[str, str, int]] = []

for width, fmt in ((1, "B"), (2, "H"), (4, "I"), (8, "Q")):
    for endian in ("<", ">"):
        DECODERS.append((endian + fmt, "u", width))
for width, fmt in ((2, "h"), (4, "i"), (8, "q")):
    for endian in ("<", ">"):
        DECODERS.append((endian + fmt, "i", width))
for endian in ("<", ">"):
    DECODERS.append((endian + "f", "f", 4))
    DECODERS.append((endian + "d", "f", 8))


def decode_value(payload: bytes, offset: int, fmt: str, width: int) -> Optional[float]:
    if offset < 0 or offset + width > len(payload):
        return None
    try:
        return float(struct.unpack(fmt, payload[offset : offset + width])[0])
    except struct.error:
        return None


def pearson(values_a: List[float], values_b: List[Optional[float]]) -> Tuple[float, int]:
    pairs = [(a, b) for a, b in zip(values_a, values_b) if b is not None and np.isfinite(b)]
    if len(pairs) < 8:
        return 0.0, 0

    array_a = np.array([pair[0] for pair in pairs])
    array_b = np.array([pair[1] for pair in pairs])

    if np.std(array_a) == 0 or np.std(array_b) == 0:
        return 0.0, len(pairs)

    return float(np.corrcoef(array_a, array_b)[0, 1]), len(pairs)


def analyze(
    blocks_path: str,
    timeline_path: str,
    output: str,
    top_n: int = 15,
) -> int:
    rows = load_blocks(blocks_path)
    print(f"keyframe blocks (target packets): {len(rows)}")
    minutes = chunk_minutes(rows)
    participants = sgp_series(timeline_path)

    # group: packetId -> param -> chunkId -> sorted blocks
    grouped: Dict[int, Dict[int, Dict[int, List[Dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )

    for row in rows:
        grouped[row["packetId"]][row["param"]][row["chunkId"]].append(row)

    findings: List[Dict[str, Any]] = []

    # Player netIds: params of packet 772 that recur ~376 times per keyframe.
    player_net_ids: List[int] = []
    counts_772: Dict[int, List[int]] = defaultdict(list)
    for row in rows:
        if row["packetId"] == 772:
            counts_772[row["param"]].append(row["chunkId"])
    for param, chunks in counts_772.items():
        unique_chunks = len(set(chunks))
        if unique_chunks >= 10 and len(chunks) / unique_chunks > 300:
            player_net_ids.append(param)
    player_net_ids.sort()
    print(f"player netIds: {[hex(value) for value in player_net_ids]}")

    # Plausible value ranges per feature.
    plausible = {
        "x": (0.0, 16000.0),
        "y": (0.0, 16000.0),
        "level": (0.0, 19.0),
        "currentGold": (0.0, 30000.0),
        "totalGold": (0.0, 30000.0),
        "xp": (0.0, 40000.0),
        "cs": (0.0, 500.0),
    }

    # Pre-build SGP matrix: (participant, feature, minute) -> value or NaN
    participant_ids = list(participants)
    n_minutes = max(len(participants[pid]["x"]) for pid in participant_ids)
    target_matrix = np.full(
        (len(participant_ids), len(FEATURES), n_minutes), np.nan
    )
    for index, participant_id in enumerate(participant_ids):
        for feature_index, feature in enumerate(FEATURES):
            for minute, value in enumerate(participants[participant_id][feature]):
                if value is not None and minute < n_minutes:
                    target_matrix[index, feature_index, minute] = value

    for packet_id in PACKETS:
        for param, by_chunk in grouped.get(packet_id, {}).items():
            if param not in player_net_ids:
                continue
            chunks = sorted(by_chunk)
            # align blocks by sequence index within (param, chunk)
            seq_series: Dict[int, Dict[int, bytes]] = defaultdict(dict)
            for chunk in chunks:
                blocks = sorted(by_chunk[chunk], key=lambda b: b["blockIndex"])
                minute = minutes.get(chunk, -1)
                for seq, block in enumerate(blocks):
                    seq_series[seq][minute] = block["payload"]

            for seq, series_by_minute in seq_series.items():
                samples: List[Tuple[int, bytes]] = sorted(series_by_minute.items())
                if len(samples) < 8:
                    continue

                payload_lengths = {len(payload) for _, payload in samples}
                minutes_list = [
                    minute for minute, _ in samples if 0 <= minute < n_minutes
                ]
                samples = [
                    (minute, payload)
                    for minute, payload in samples
                    if 0 <= minute < n_minutes
                ]
                if len(samples) < 8:
                    continue

                for fmt, kind, width in DECODERS:
                    max_offset = max(payload_lengths) - width
                    for offset in range(0, max_offset + 1):
                        decoded: List[float] = []
                        for _, payload in samples:
                            value = decode_value(payload, offset, fmt, width)
                            if value is None:
                                decoded.append(np.nan)
                            else:
                                decoded.append(value)

                        vector = np.array(decoded, dtype=np.float64)
                        if np.isnan(vector).any():
                            continue
                        if np.max(vector) == np.min(vector):
                            continue
                        if np.max(np.abs(vector)) > 1e9:
                            continue
                        if kind == "u" and np.max(vector) > 2_000_000_000:
                            continue
                        if kind == "i" and (
                            np.min(vector) < -2_000_000_000
                            or np.max(vector) > 2_000_000_000
                        ):
                            continue

                        # targets at the same minutes
                        targets = target_matrix[:, :, minutes_list]  # (P, F, m)
                        finite = np.isfinite(targets)
                        vector_centered = vector - vector.mean()

                        # compute correlation for all (P, F) at once
                        target_centered = targets - np.nanmean(targets, axis=2, keepdims=True)
                        numerator = np.einsum("m,pfm->pf", vector_centered, np.where(finite, target_centered, 0.0))
                        denominator = np.sqrt(
                            vector_centered @ vector_centered
                            * np.nansum(np.where(finite, target_centered**2, 0.0), axis=2)
                        )
                        correlation = numerator / np.maximum(denominator, 1e-12)
                        count = finite.sum(axis=2)

                        valid = (np.abs(correlation) >= 0.9) & (count >= 15)
                        rows_idx, columns = np.nonzero(valid)
                        for row, column in zip(rows_idx, columns):
                            feature = FEATURES[column]
                            lower, upper = plausible[feature]
                            if np.min(vector) < lower or np.max(vector) > upper:
                                continue
                            if feature == "level" and not np.allclose(
                                vector, np.round(vector)
                            ):
                                continue
                            findings.append(
                                {
                                    "packetId": packet_id,
                                    "param": param,
                                    "paramHex": hex(param),
                                    "seq": seq,
                                    "decoder": fmt,
                                    "offset": offset,
                                    "participant": participant_ids[row],
                                    "feature": feature,
                                    "correlation": round(float(correlation[row, column]), 4),
                                    "samples": int(count[row, column]),
                                    "samplePayloads": [samples[0][1].hex(), samples[-1][1].hex()],
                                }
                            )

    findings.sort(key=lambda item: -abs(item["correlation"]))

    unique: List[Dict[str, Any]] = []
    seen = set()
    for item in findings:
        key = (
            item["packetId"],
            item["param"],
            item["seq"],
            item["decoder"],
            item["offset"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= top_n * 4:
            break

    output_path = output
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"totalFindings": len(findings), "top": unique[: top_n]},
            handle,
            ensure_ascii=False,
            indent=2,
        )

    print(f"total candidate correlations: {len(findings)}")
    print(f"Top {top_n}:")
    for item in unique[:top_n]:
        print(
            f"  pkt={item['packetId']} netId={item['paramHex']} seq={item['seq']} "
            f"{item['decoder']}@{item['offset']} -> participant={item['participant']} "
            f"{item['feature']} r={item['correlation']} n={item['samples']} "
            f"payloads={item['samplePayloads'][0]}..{item['samplePayloads'][1]}"
        )

    print(f"Output: {output_path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ROFL 字段表与 SGP 已知值关联分析",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--blocks", required=True, help="blocks_full.jsonl.gz")
    parser.add_argument("--timeline", required=True, help="timeline.json")
    parser.add_argument("--output", default="field_correlations.json")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args(argv)

    try:
        return analyze(args.blocks, args.timeline, args.output, args.top)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"[错误] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
