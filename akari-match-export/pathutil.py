#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""训练/验证产物里记录路径时用的小工具。

模型目录会被拷来拷去（打包、换机器、发给别人），所以在 `metrics.json` 里
记录**相对路径**比记录绝对路径更耐用：绝对路径一换机器就变成「数据集不一致」
的假象。这里统一把路径转成相对 `base_dir` 的 POSIX 形式。
"""

from __future__ import annotations

import os
from pathlib import Path


def portable_path(path: str | os.PathLike[str], base_dir: str | os.PathLike[str]) -> str:
    """把 *path* 表示成相对 *base_dir* 的路径；跨盘等无法相对化时退回原字符串。"""
    try:
        return Path(os.path.relpath(Path(path).resolve(), Path(base_dir).resolve())).as_posix()
    except (OSError, ValueError):
        return str(path)
