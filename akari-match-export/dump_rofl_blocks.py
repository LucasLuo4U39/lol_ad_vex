#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump every ROFL block with its full payload hex (for offline analysis)."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

import unpack_replay as replay

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="导出 ROFL 全部 block（含完整 payload hex）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("replay", type=Path)
    parser.add_argument("--output", type=Path, default=Path.cwd() / "blocks_full.jsonl.gz")
    args = parser.parse_args(argv)

    container, blocks = replay.parse_replay(args.replay)
    print(f"blocks: {len(blocks)}")

    with gzip.open(args.output, "wt", encoding="utf-8") as handle:
        for block in blocks:
            handle.write(
                json.dumps(
                    {
                        "chunkId": block.chunk_id,
                        "chunkType": block.chunk_type,
                        "blockIndex": block.block_index,
                        "timestamp": block.timestamp,
                        "packetId": block.packet_id,
                        "param": block.param,
                        "payloadLength": block.payload_length,
                        "payloadHex": block.payload.hex(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
