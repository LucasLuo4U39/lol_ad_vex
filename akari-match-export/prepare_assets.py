#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把运行时需要的模型/数据复制到 assets/，供 PyInstaller 打包进 exe。

内容：
    assets/live_dataset/live_model3/model.txt
    assets/live_dataset/live_model5/model.txt
    assets/live_dataset/live_event_model/model_*.txt
    assets/opgg_stats.json

另外校验 ui/ 界面文件齐全（ui/ 目录本身由 PyInstaller 的 --add-data 直接打包，
不需要复制，这里只做构建前门禁，缺文件时返回 1 中止构建）。

v2.3.13 起额外生成 build_ui/：ui/ 的副本（剔除 next/previews 截图），
供 build_exe.cmd 以 --add-data "build_ui;ui" 打包，减小发布包体积。

用法：python prepare_assets.py
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATASET = BASE.parent / "dataset" / "live_dataset"
ASSETS = BASE / "assets"
UI = BASE / "ui"

UI_REQUIRED = [
    UI / "design-system.css",
    UI / "next" / "app.html",
    UI / "next" / "app.js",
    UI / "next" / "shell.html",
    UI / "next" / "shell.js",
    UI / "next" / "home.html",
    UI / "next" / "champ_select.html",
    UI / "next" / "dashboard.html",
    UI / "next" / "demo-data.js",
]


def _rmtree(path: Path) -> None:
    """删除目录树：Windows 上只读文件会 PermissionError，先去掉只读属性再删。"""

    def _force(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_force)
    except TypeError:  # Python < 3.12 没有 onexc
        shutil.rmtree(path, onerror=lambda f, t, e: _force(f, t, e))


def copy(source: Path, target: Path) -> bool:
    if not source.is_file():
        print(f"  跳过（不存在）: {source}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print(f"  {source.name} -> {target.relative_to(BASE)}")
    return True


def main() -> int:
    count = 0
    for model in ("live_model3", "live_model5"):
        count += copy(DATASET / model / "model.txt", ASSETS / "live_dataset" / model / "model.txt")
    for event_model in sorted((DATASET / "live_event_model").glob("model_*.txt")):
        count += copy(event_model, ASSETS / "live_dataset" / "live_event_model" / event_model.name)
    # 模型元数据（patch / trainedAt / featureStats）：model_monitor 的漂移基线来源
    for model in ("live_model3", "live_model5", "live_event_model"):
        count += copy(
            DATASET / model / "metrics.json",
            ASSETS / "live_dataset" / model / "metrics.json",
        )
    # T49：事件建议阈值（与模型 .txt 同级；缺失不阻断，运行时用内置缺省）
    count += copy(
        DATASET / "live_event_model" / "event_thresholds.json",
        ASSETS / "live_dataset" / "live_event_model" / "event_thresholds.json",
    )
    count += copy(BASE / "opgg_stats.json", ASSETS / "opgg_stats.json")

    missing = [str(p.relative_to(BASE)) for p in UI_REQUIRED if not p.is_file()]
    if missing:
        print(f"  缺少 ui/ 界面文件（{', '.join(missing)}），中止构建")
        return 1
    print(f"  ui/ 界面文件齐全（design-system.css + app + shell + 3 页面 + demo-data.js）")

    # 发布包暂存（v2.3.13）：复制 ui/ 到 build_ui/，剔除 previews 截图（约 2.3MB，
    # 只用于设计走查/文档，不必随包）；build_exe.cmd 以 --add-data "build_ui;ui" 打包。
    build_ui = BASE / "build_ui"
    if build_ui.is_dir():
        _rmtree(build_ui)
    shutil.copytree(UI, build_ui, symlinks=True)
    previews = build_ui / "next" / "previews"
    if previews.is_dir():
        shutil.rmtree(previews)
    packaged = sum(p.stat().st_size for p in build_ui.rglob("*") if p.is_file())
    print(
        f"  发布包 ui 暂存目录 build_ui/ 已生成"
        f"（{packaged / 1024 / 1024:.1f} MB，已剔除 next/previews 截图）"
    )

    # curves.json（T24）：选人页胜率-时间曲线；缺失不阻断（接口返回 null，页面回退）
    if (ASSETS / "curves.json").is_file():
        print("  assets/curves.json 已就绪（胜率-时间曲线随包）")
    else:
        print("  提示: assets/curves.json 缺失（曲线接口将返回 null，页面回退选人过程曲线）")

    # jungle_routes.json（T54）：打野雷达开野路线数据；缺失阻断（雷达功能依赖）
    if (ASSETS / "jungle_routes.json").is_file():
        print("  assets/jungle_routes.json 已就绪（打野雷达路线随包）")
    else:
        print("  缺少 assets/jungle_routes.json（打野雷达路线数据），中止构建")
        return 1

    print(f"\n完成：复制 {count} 个文件到 {ASSETS}")
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
