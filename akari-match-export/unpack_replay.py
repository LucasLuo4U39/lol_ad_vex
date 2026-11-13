#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ROFL2 replay unpacker.

Extracts the container layers of a modern League of Legends `.rofl` replay:

  * header / game version
  * footer metadata (including the final scoreboard `statsJson`)
  * chunk table
  * zstd-decompressed chunks
  * packet blocks (timestamp, packet id, param, payload)

It does NOT decode gameplay packet fields into named events. Those schemas are
patch- and executable-specific and require the matching `League of Legends.exe`
(or a decoder artifact built from it).

Usage:
    python unpack_replay.py <replay.rofl> [--output DIR]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import struct
import sys
import zstandard as zstd
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROFL_MAGIC = b"RIOT"
ROFL_VERSION = 2
SIGNATURE_SIZE = 256
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

CHUNK_TYPE_NAMES = {
    1: "game-data",
    2: "keyframe",
    3: "startup-data",
    4: "startup-end",
}


class ReplayFormatError(Exception):
    pass


@dataclass
class Chunk:
    sequence: int
    id: int
    next_id: int
    type: int
    header_size: int
    offset: int
    data_offset: int
    stored_length: int
    uncompressed_length: int
    compressed_length: int
    flags: Optional[int] = None
    data: bytes = b""
    decompressed: bytes = b""

    @property
    def type_name(self) -> str:
        return CHUNK_TYPE_NAMES.get(self.type, f"unknown-{self.type}")

    @property
    def compression(self) -> str:
        return "zstd" if self.compressed_length else "none"


@dataclass
class Block:
    chunk_id: int
    chunk_type: str
    block_index: int
    marker: int
    timestamp: float
    packet_id: int
    param: int
    payload_length: int
    payload_offset: int
    payload: bytes = field(repr=False)


@dataclass
class Container:
    game_version: str
    header_end: int
    metadata_start: int
    metadata_length: int
    signature_start: int
    metadata: Dict[str, Any]
    stats: List[Dict[str, Any]]
    chunk_header_size: int
    chunks: List[Chunk]


def u16(buffer: bytes, offset: int) -> int:
    if offset + 2 > len(buffer):
        raise ReplayFormatError(f"uint16 out of range at {offset}")
    return struct.unpack_from("<H", buffer, offset)[0]


def u32(buffer: bytes, offset: int) -> int:
    if offset + 4 > len(buffer):
        raise ReplayFormatError(f"uint32 out of range at {offset}")
    return struct.unpack_from("<I", buffer, offset)[0]


def f32(buffer: bytes, offset: int) -> float:
    if offset + 4 > len(buffer):
        raise ReplayFormatError(f"float32 out of range at {offset}")
    return struct.unpack_from("<f", buffer, offset)[0]


def parse_container_header(buffer: bytes) -> Tuple[str, int]:
    if len(buffer) < 33:
        raise ReplayFormatError("file is too small to be a ROFL2 replay")

    if buffer[:4] != ROFL_MAGIC:
        raise ReplayFormatError("missing RIOT magic bytes")

    version = u16(buffer, 4)
    if version != ROFL_VERSION:
        raise ReplayFormatError(
            f"unsupported ROFL container version {version} (only ROFL2 is supported)"
        )

    game_version_length = buffer[14]
    header_end = 15 + game_version_length
    if header_end > len(buffer):
        raise ReplayFormatError("game version string is truncated")

    game_version = buffer[15:header_end].decode("utf-8", errors="replace")
    return game_version, header_end


def parse_footer(
    buffer: bytes, header_end: int
) -> Tuple[int, int, int, Dict[str, Any], List[Dict[str, Any]]]:
    metadata_length = u32(buffer, len(buffer) - 4)
    metadata_start = len(buffer) - 4 - metadata_length
    signature_start = metadata_start - SIGNATURE_SIZE

    if metadata_length == 0 or signature_start < header_end:
        raise ReplayFormatError("footer or signature area is corrupted")

    metadata_text = buffer[metadata_start : len(buffer) - 4].decode(
        "utf-8", errors="replace"
    )

    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError as error:
        raise ReplayFormatError(f"metadata JSON is invalid: {error}") from error

    stats: List[Dict[str, Any]] = []
    stats_json = metadata.get("statsJson")

    if isinstance(stats_json, str) and stats_json:
        try:
            stats = json.loads(stats_json)
        except json.JSONDecodeError:
            stats = []

    return metadata_start, metadata_length, signature_start, metadata, stats


def parse_chunks(
    buffer: bytes, start: int, end: int, header_size: int
) -> List[Chunk]:
    chunks: List[Chunk] = []
    offset = start

    while offset < end:
        if offset + header_size > end:
            raise ReplayFormatError(
                f"chunk header at {offset} extends past payload end"
            )

        chunk_id = u32(buffer, offset)
        next_id = u32(buffer, offset + 4)
        chunk_type = buffer[offset + 8]
        uncompressed_length = u32(buffer, offset + 9)
        compressed_length = u32(buffer, offset + 13)
        flags = buffer[offset + 17] if header_size >= 18 else None

        data_offset = offset + header_size
        stored_length = compressed_length or uncompressed_length

        if data_offset + stored_length > end:
            raise ReplayFormatError(
                f"chunk {chunk_id} payload extends past payload end"
            )

        data = buffer[data_offset : data_offset + stored_length]

        if compressed_length and not data.startswith(ZSTD_MAGIC):
            raise ReplayFormatError(
                f"chunk {chunk_id} is marked zstd but has no zstd frame"
            )

        chunks.append(
            Chunk(
                sequence=len(chunks),
                id=chunk_id,
                next_id=next_id,
                type=chunk_type,
                header_size=header_size,
                offset=offset,
                data_offset=data_offset,
                stored_length=stored_length,
                uncompressed_length=uncompressed_length,
                compressed_length=compressed_length,
                flags=flags,
                data=data,
            )
        )

        offset = data_offset + stored_length

    if offset != end:
        raise ReplayFormatError(
            f"chunk table does not end at the payload boundary: {offset} != {end}"
        )

    if not chunks:
        raise ReplayFormatError("no chunks found")

    return chunks


def detect_chunks(
    buffer: bytes, start: int, end: int
) -> Tuple[int, List[Chunk]]:
    errors: List[str] = []

    # 16.22 uses an 18-byte chunk header (one extra flags byte). Older
    # ROFL2 builds used 17; keep both as candidates and validate by boundary.
    for header_size in (18, 17, 19):
        try:
            chunks = parse_chunks(buffer, start, end, header_size)
        except ReplayFormatError as error:
            errors.append(f"{header_size}: {error}")
            continue

        if chunks[0].id != 1:
            errors.append(f"{header_size}: first chunk id is {chunks[0].id}, not 1")
            continue

        return header_size, chunks

    raise ReplayFormatError(
        "unable to determine the chunk header size; " + "; ".join(errors)
    )


def decompress_chunk(chunk: Chunk) -> bytes:
    if not chunk.compressed_length:
        return chunk.data

    try:
        return zstd.ZstdDecompressor().decompress(chunk.data)
    except zstd.ZstdError:
        with zstd.ZstdDecompressor().stream_reader(io.BytesIO(chunk.data)) as reader:
            return reader.read()


def parse_blocks(chunk: Chunk) -> List[Block]:
    buffer = chunk.decompressed
    blocks: List[Block] = []
    offset = 0
    accumulated_time = 0.0
    previous_packet_id = 0
    previous_param = 0

    def need(length: int, label: str) -> None:
        if offset + length > len(buffer):
            raise ReplayFormatError(
                f"chunk {chunk.id}: {label} out of range at {offset}"
            )

    while offset < len(buffer):
        block_index = len(blocks)
        block_offset = offset

        need(1, "marker")
        marker = buffer[offset]
        offset += 1

        if marker & 0x80:
            need(1, "relative timestamp")
            accumulated_time += buffer[offset] * 0.001
            offset += 1
        else:
            need(4, "absolute timestamp")
            accumulated_time = f32(buffer, offset)
            offset += 4

        if marker & 0x10:
            need(1, "short payload length")
            payload_length = buffer[offset]
            offset += 1
        else:
            need(4, "payload length")
            payload_length = u32(buffer, offset)
            offset += 4

        if marker & 0x40:
            packet_id = previous_packet_id
        else:
            need(2, "packet id")
            packet_id = u16(buffer, offset)
            offset += 2

        if marker & 0x20:
            need(1, "relative param")
            param = (previous_param + buffer[offset]) & 0xFFFFFFFF
            offset += 1
        else:
            need(4, "param")
            param = u32(buffer, offset)
            offset += 4

        need(payload_length, "payload")
        payload_offset = offset
        payload = buffer[offset : offset + payload_length]
        offset += payload_length

        blocks.append(
            Block(
                chunk_id=chunk.id,
                chunk_type=chunk.type_name,
                block_index=block_index,
                marker=marker,
                timestamp=accumulated_time,
                packet_id=packet_id,
                param=param,
                payload_length=payload_length,
                payload_offset=payload_offset,
                payload=payload,
            )
        )

        previous_packet_id = packet_id
        previous_param = param

    return blocks


def parse_replay(path: Path) -> Tuple[Container, List[Block]]:
    buffer = path.read_bytes()
    game_version, header_end = parse_container_header(buffer)
    metadata_start, metadata_length, signature_start, metadata, stats = parse_footer(
        buffer, header_end
    )

    chunk_header_size, chunks = detect_chunks(buffer, header_end, signature_start)

    all_blocks: List[Block] = []

    for chunk in chunks:
        chunk.decompressed = decompress_chunk(chunk)

        if chunk.uncompressed_length and len(chunk.decompressed) != chunk.uncompressed_length:
            raise ReplayFormatError(
                f"chunk {chunk.id}: decompressed length {len(chunk.decompressed)} "
                f"!= expected {chunk.uncompressed_length}"
            )

        all_blocks.extend(parse_blocks(chunk))

    container = Container(
        game_version=game_version,
        header_end=header_end,
        metadata_start=metadata_start,
        metadata_length=metadata_length,
        signature_start=signature_start,
        metadata=metadata,
        stats=stats,
        chunk_header_size=chunk_header_size,
        chunks=chunks,
    )

    return container, all_blocks


def format_duration(milliseconds: Any) -> str:
    try:
        total_seconds = float(milliseconds) / 1000
    except (TypeError, ValueError):
        return "unknown"

    minutes, seconds = divmod(int(total_seconds), 60)
    return f"{minutes}:{seconds:02d}"


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_packet_summary(blocks: List[Block]) -> List[Dict[str, Any]]:
    summary: Dict[int, Dict[str, Any]] = {}

    for block in blocks:
        item = summary.get(block.packet_id)

        if item is None:
            item = {
                "packetId": block.packet_id,
                "count": 0,
                "totalPayloadBytes": 0,
                "firstTimestamp": block.timestamp,
                "lastTimestamp": block.timestamp,
            }
            summary[block.packet_id] = item

        item["count"] += 1
        item["totalPayloadBytes"] += block.payload_length
        item["firstTimestamp"] = min(item["firstTimestamp"], block.timestamp)
        item["lastTimestamp"] = max(item["lastTimestamp"], block.timestamp)

    return sorted(
        summary.values(), key=lambda item: item["count"], reverse=True
    )


def unpack(
    path: Path,
    output_directory: Optional[Path],
    write_chunks: bool,
    write_blocks: bool,
    raw_blocks: bool,
) -> int:
    container, blocks = parse_replay(path)

    output = output_directory or path.with_name(path.stem + "_unpacked")
    output.mkdir(parents=True, exist_ok=True)

    stats = container.stats
    metadata = dict(container.metadata)
    metadata["stats"] = stats

    write_json(output / "metadata.json", metadata)
    write_json(output / "stats.json", stats)

    chunk_records = []
    chunks_directory = output / "chunks"

    if write_chunks:
        chunks_directory.mkdir(exist_ok=True)

    for chunk in container.chunks:
        record = {
            "sequence": chunk.sequence,
            "id": chunk.id,
            "nextId": chunk.next_id,
            "type": chunk.type,
            "typeName": chunk.type_name,
            "offset": chunk.offset,
            "dataOffset": chunk.data_offset,
            "headerSize": chunk.header_size,
            "flags": chunk.flags,
            "storedLength": chunk.stored_length,
            "uncompressedLength": chunk.uncompressed_length,
            "compressedLength": chunk.compressed_length,
            "compression": chunk.compression,
            "sha256": hashlib.sha256(chunk.decompressed).hexdigest(),
        }
        chunk_records.append(record)

        if write_chunks:
            file_name = (
                f"{chunk.sequence:03d}_{chunk.id:05d}_{chunk.type_name}.bin"
            )
            (chunks_directory / file_name).write_bytes(chunk.decompressed)

    write_json(output / "chunks.json", chunk_records)

    if write_blocks:
        blocks_path = output / ("blocks.jsonl" if raw_blocks else "blocks.jsonl.gz")
        if raw_blocks:
            handle = blocks_path.open("w", encoding="utf-8")
        else:
            handle = gzip.open(blocks_path, "wt", encoding="utf-8", compresslevel=6)

        with handle:
            for block in blocks:
                hex_preview = block.payload[:64].hex()
                handle.write(
                    json.dumps(
                        {
                            "chunkId": block.chunk_id,
                            "chunkType": block.chunk_type,
                            "blockIndex": block.block_index,
                            "marker": block.marker,
                            "timestamp": round(block.timestamp, 3),
                            "packetId": block.packet_id,
                            "param": block.param,
                            "payloadLength": block.payload_length,
                            "payloadOffset": block.payload_offset,
                            "payloadHexPreview": hex_preview,
                            "payloadTruncated": block.payload_length > 64,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    packet_summary = build_packet_summary(blocks)
    write_json(output / "packets.json", packet_summary)

    total_payload_bytes = sum(block.payload_length for block in blocks)
    chunk_type_counts: Dict[str, int] = {}

    for chunk in container.chunks:
        chunk_type_counts[chunk.type_name] = (
            chunk_type_counts.get(chunk.type_name, 0) + 1
        )

    manifest = {
        "source": str(path),
        "sourceSize": path.stat().st_size,
        "exportedAt": datetime.now(tz=timezone.utc).isoformat(),
        "gameVersion": container.game_version,
        "gameLengthMs": container.metadata.get("gameLength"),
        "lastGameChunkId": container.metadata.get("lastGameChunkId"),
        "lastKeyFrameId": container.metadata.get("lastKeyFrameId"),
        "chunkHeaderSize": container.chunk_header_size,
        "chunkCount": len(container.chunks),
        "chunkTypes": chunk_type_counts,
        "blockCount": len(blocks),
        "packetIdCount": len(packet_summary),
        "totalPayloadBytes": total_payload_bytes,
        "statsPlayerCount": len(stats),
    }
    write_json(output / "manifest.json", manifest)

    summary_lines = [
        f"Source: {path}",
        f"Size: {path.stat().st_size / (1024 * 1024):.1f} MB",
        f"Game version: {container.game_version}",
        f"Game length: {format_duration(container.metadata.get('gameLength'))}",
        f"Players in stats: {len(stats)}",
        f"Chunks: {len(container.chunks)} "
        f"({', '.join(f'{k}={v}' for k, v in sorted(chunk_type_counts.items()))})",
        f"Chunk header size: {container.chunk_header_size}",
        f"Decoded blocks: {len(blocks)}",
        f"Distinct packet IDs: {len(packet_summary)}",
        f"Decoded payload bytes: {total_payload_bytes / (1024 * 1024):.1f} MB",
        "",
        "Top packet IDs:",
    ]

    for item in packet_summary[:15]:
        summary_lines.append(
            f"  packet {item['packetId']:5d}  count={item['count']:7d}  "
            f"bytes={item['totalPayloadBytes']:10d}  "
            f"t={item['firstTimestamp']:.1f}..{item['lastTimestamp']:.1f}s"
        )

    (output / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    readme = """ROFL2 unpack result
===================

metadata.json   Container metadata (gameLength, lastGameChunkId, lastKeyFrameId, statsJson).
stats.json      Final scoreboard stats parsed from metadata.statsJson.
chunks.json     Chunk table: id, type, offsets, lengths, sha256 of decompressed data.
chunks/         Decompressed chunks (startup / keyframe / game-data / startup-end).
blocks.jsonl.gz One JSON object per packet block:
                chunkId, blockIndex, timestamp, packetId, param, payloadLength,
                payloadOffset, payloadHexPreview (first 64 bytes).
packets.json    Aggregated per-packetId counts / bytes / time range.
summary.txt     Human-readable overview.
manifest.json   Export manifest.

Layers
------
1. Container: fully decoded here.
2. Blocks: timestamp / packetId / param / payload extracted here.
3. Semantic events (damage, items, spells): NOT decoded here. Those require a
   patch- and executable-specific schema (the exact League of Legends.exe).

Notes
-----
- timestamp is relative to the current chunk, not necessarily global game time.
- packet IDs are patch-specific; the same number means something different in
  another patch.
- This tool only reads the .rofl file and never modifies the game client.
"""
    (output / "README.txt").write_text(readme, encoding="utf-8")

    print("\n".join(summary_lines))
    print()
    print(f"Output: {output}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="解包 ROFL2 回放：元数据、统计、chunk、packet 块",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("replay", type=Path, help=".rofl 文件路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出目录，默认是 <replay>_unpacked",
    )
    parser.add_argument(
        "--no-chunks",
        action="store_true",
        help="不写出解压后的 chunk 文件",
    )
    parser.add_argument(
        "--no-blocks",
        action="store_true",
        help="不写出 blocks.jsonl.gz",
    )
    parser.add_argument(
        "--raw-blocks",
        action="store_true",
        help="写出未压缩的 blocks.jsonl（体积会很大）",
    )
    args = parser.parse_args(argv)

    if not args.replay.is_file():
        print(f"[错误] 找不到文件: {args.replay}", file=sys.stderr)
        return 1

    try:
        return unpack(
            args.replay,
            args.output,
            not args.no_chunks,
            not args.no_blocks,
            args.raw_blocks,
        )
    except ReplayFormatError as error:
        print(f"[错误] 回放格式不匹配: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"[错误] {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
