#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export decompressed ROFL chunks (especially keyframes) for reverse engineering."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import unpack_replay as replay

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="导出 ROFL chunk 原始字节",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("replay", type=Path)
    parser.add_argument("--output", type=Path, default=Path.cwd() / "rofl-chunks")
    parser.add_argument(
        "--only",
        choices=["keyframe", "game-data", "all"],
        default="keyframe",
    )
    args = parser.parse_args(argv)

    container, blocks = replay.parse_replay(args.replay)
    block_counts = Counter(block.chunk_id for block in blocks)
    args.output.mkdir(parents=True, exist_ok=True)

    manifest = {
        "replay": str(args.replay),
        "gameVersion": container.game_version,
        "chunks": [],
        "blocks": len(blocks),
    }

    for chunk in container.chunks:
        if args.only != "all" and chunk.type_name != args.only:
            continue

        filename = f"{chunk.sequence:03d}_{chunk.type_name}_{chunk.id}.bin"
        path = args.output / filename
        path.write_bytes(chunk.decompressed)
        manifest["chunks"].append(
            {
                "sequence": chunk.sequence,
                "id": chunk.id,
                "type": chunk.type_name,
                "file": filename,
                "decompressedLength": len(chunk.decompressed),
                "blockCount": block_counts.get(chunk.id, 0),
            }
        )
        print(f"{filename}  {len(chunk.decompressed):,} bytes")

    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
