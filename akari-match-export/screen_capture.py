#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""屏幕采集 + 小地图敌方英雄图标模板匹配（MVP，打野雷达数据源）。

功能：
  * capture_game_region()     抓取游戏窗口/屏幕区域（mss），返回 RGB numpy 帧
  * detect_minimap_enemies()  在小地图区域内做英雄头像模板匹配，输出
    「是否现身 + 粗略区域（上/下半野区四象限）+ 时间戳」
  * fetch_icon_templates()    从 LCU 拉取英雄头像 PNG（本地缓存）
  * python screen_capture.py --self-test   离线自检（合成帧，无需 mss/LCU）

依赖：真实采集需要 `mss` + `Pillow`（requirements.txt 已加）；
--self-test 只依赖 numpy。

局限（使用前务必知悉）：
  1. 需要游戏运行在「无边框 / 窗口」模式；全屏独占（fullscreen exclusive）下
     mss 抓到的通常是黑帧，本模块检测到近全黑帧会返回明确错误提示。
  2. 小地图区域（region）默认取屏幕右下角，比例随分辨率/UI 缩放变化，
     需在 1080p/2K 下实测校准后传入；不传时抓整屏（由上层裁剪）。
  3. 输出坐标是截图像素坐标，未映射到游戏地图坐标。
  4. 英雄头像在小地图上很小（约 12–18px，随分辨率变化），模板匹配存在
     误检/漏检；threshold 需要按实际分辨率调参，默认 0.55 偏宽松。
  5. 只能回答「现身与否 + 四象限」，无法分辨具体营地/路线；战争迷雾内
     的英雄本来就不出现在小地图上。
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import ssl
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 默认小地图区域：屏幕右下角（占宽/高约 1/4，1080p 下的典型值；需实测校准）
DEFAULT_REGION = (1440, 810, 480, 270)  # (left, top, width, height)
DEFAULT_THRESHOLD = 0.55
DEFAULT_ICON_SIZE = 16  # 小地图英雄图标在 1080p 下的近似边长（像素）
DEFAULT_SCALES = (0.75, 1.0, 1.25)
ICON_URL_PATTERN = "https://127.0.0.1:{port}/lol-game-data/assets/v1/champion-icons/{id}.png"
DEFAULT_ICON_CACHE = Path(__file__).resolve().parent / "assets" / "champion-icons"


def icon_cache_dir() -> Path:
    """头像模板缓存目录（可写优先，mkdir 失败不抛）。

    冻结态（exe 安装目录通常只读）：优先 %LOCALAPPDATA%\\LoLAdvisor\\champion-icons，
    不可写则回退系统临时目录；源码态沿用 assets/champion-icons。
    """
    if getattr(sys, "frozen", False):
        candidates: List[Path] = []
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata) / "LoLAdvisor" / "champion-icons")
        candidates.append(Path(tempfile.gettempdir()) / "LoLAdvisor-champion-icons")
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                continue
            if candidate.is_dir():
                return candidate
        return Path(tempfile.gettempdir())
    try:
        DEFAULT_ICON_CACHE.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_ICON_CACHE


class CaptureError(RuntimeError):
    """截图/识别前置条件不满足（用户可读信息）。"""


def _import_mss():
    try:
        import mss  # noqa: PLC0415
    except ImportError as error:
        raise CaptureError(
            "缺少 mss 依赖：pip install mss（游戏需为无边框/窗口模式）"
        ) from error
    return mss


def _import_pil():
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as error:
        raise CaptureError("缺少 Pillow 依赖：pip install Pillow") from error
    return Image


def capture_game_region(
    region: Optional[Tuple[int, int, int, int]] = None,
    monitor: int = 1,
    max_retries: int = 3,
) -> np.ndarray:
    """抓取屏幕区域，返回 RGB uint8 数组 (H, W, 3)。

    region=(left, top, width, height)；None 表示整块显示器（monitor 编号）。
    近全黑帧视为全屏独占不可用，抛 CaptureError。
    """
    mss = _import_mss()
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            with mss.mss() as sct:
                if region is not None:
                    left, top, width, height = region
                    shot = sct.grab(
                        {"left": left, "top": top, "width": width, "height": height}
                    )
                else:
                    shot = sct.grab(sct.monitors[monitor])
            frame = np.asarray(shot)[:, :, :3][:, :, ::-1].copy()  # BGRA -> RGB
            if frame.size == 0:
                raise CaptureError("截图为空")
            if float(frame.mean()) < 4.0:
                raise CaptureError(
                    "截图近全黑：游戏可能运行在全屏独占模式，请切换到无边框/窗口模式后重试"
                )
            return frame
        except CaptureError:
            raise
        except Exception as error:  # noqa: BLE001
            last_error = error
            continue

    raise CaptureError(f"截图失败（重试 {max_retries} 次）: {last_error}")


def to_gray(frame: np.ndarray) -> np.ndarray:
    """RGB -> 灰度（H, W）float64。"""
    if frame.ndim == 3:
        return (
            0.299 * frame[:, :, 0].astype(np.float64)
            + 0.587 * frame[:, :, 1].astype(np.float64)
            + 0.114 * frame[:, :, 2].astype(np.float64)
        )
    return frame.astype(np.float64)


def _window_sums(integral: np.ndarray, h: int, w: int) -> np.ndarray:
    """由积分图求所有 (h,w) 窗口和，返回 (H, W)。

    窗口 (y0,x0) 的和 = I[y0+h-1, x0+w-1] - I[y0-1, x0+w-1]
                    - I[y0+h-1, x0-1] + I[y0-1, x0-1]（越界项为 0）。
    """
    out_h = integral.shape[0] - h + 1
    out_w = integral.shape[1] - w + 1
    bottom_right = integral[h - 1 :, w - 1 :]
    top_right = np.zeros_like(bottom_right)
    top_right[1:] = integral[: out_h - 1, w - 1 :]
    bottom_left = np.zeros_like(bottom_right)
    bottom_left[:, 1:] = integral[h - 1 :, : out_w - 1]
    top_left = np.zeros_like(bottom_right)
    top_left[1:, 1:] = integral[: out_h - 1, : out_w - 1]
    return bottom_right - top_right - bottom_left + top_left


def match_template(
    gray: np.ndarray,
    template: np.ndarray,
    threshold: float = DEFAULT_THRESHOLD,
) -> Tuple[bool, int, int, float]:
    """灰度归一化互相关模板匹配（FFT 加速 + 积分图归一化）。

    返回 (found, y, x, score)。score ∈ [-1, 1]；模板大于帧时直接未命中。
    模板可为 RGB（自动转灰度）。
    """
    t = to_gray(template) if template.ndim == 3 else template.astype(np.float64)
    f = gray.astype(np.float64)

    if t.shape[0] > f.shape[0] or t.shape[1] > f.shape[1]:
        return False, -1, -1, 0.0

    th, tw = t.shape
    t_centered = t - t.mean()
    t_norm = float(np.linalg.norm(t_centered))
    if t_norm < 1e-9:
        return False, -1, -1, 0.0

    # 相关面：模板已去均值，窗口均值自动抵消
    corr = np.fft.ifft2(
        np.fft.fft2(f) * np.conj(np.fft.fft2(t_centered, s=f.shape))
    ).real

    # 帧窗口平方和（积分图，向量化）-> std = sqrt(sum((f-fm)^2))
    integral = np.cumsum(np.cumsum(f, axis=0), axis=1)
    integral2 = np.cumsum(np.cumsum(f * f, axis=0), axis=1)
    sums = _window_sums(integral, th, tw)
    sumsq = _window_sums(integral2, th, tw)
    n = float(th * tw)
    sq_sum = np.maximum(sumsq - (sums * sums) / n, 0.0)
    std = np.sqrt(sq_sum)

    score_map = np.zeros_like(sums)
    np.divide(corr[: sums.shape[0], : sums.shape[1]], std * t_norm,
              out=score_map, where=(std * t_norm) > 1e-9)

    peak = float(score_map.max())
    if not math.isfinite(peak) or peak < threshold:
        return False, -1, -1, peak if math.isfinite(peak) else 0.0

    index = int(np.argmax(score_map))
    y, x = divmod(index, score_map.shape[1])
    return True, int(y), int(x), peak


def resize_template(template: np.ndarray, size: int) -> np.ndarray:
    """把模板缩放到 (size, size)（灰度，float）。"""
    if template.ndim == 3:
        gray = to_gray(template)
    else:
        gray = template.astype(np.float64)
    if gray.shape[0] == size and gray.shape[1] == size:
        return gray
    image = _import_pil()
    resized = image.fromarray(gray.astype(np.uint8)).resize(
        (size, size), image.Resampling.BILINEAR
    )
    return np.asarray(resized, dtype=np.float64)


def classify_quadrant(
    x: int, y: int, region: Tuple[int, int, int, int]
) -> Dict[str, str]:
    """把区域内的像素坐标分成上下半区四象限。"""
    left, top, width, height = region
    cx = left + width / 2.0
    cy = top + height / 2.0
    half = "TOP" if y < cy else "BOTTOM"
    lane = "LEFT" if x < cx else "RIGHT"
    return {"half": half, "quadrant": f"{half}_{lane}"}


def detect_minimap_enemies(
    frame: np.ndarray,
    icon_templates: Dict[str, np.ndarray],
    region: Tuple[int, int, int, int],
    threshold: float = DEFAULT_THRESHOLD,
    icon_size: int = DEFAULT_ICON_SIZE,
    scales: Sequence[float] = DEFAULT_SCALES,
    timestamp: Optional[str] = None,
) -> Dict[str, Dict[str, object]]:
    """在小地图区域内检测每个英雄图标。

    icon_templates: {key: RGB/灰度模板}；region=(left, top, width, height)。
    返回 {key: {"visible": bool, "score": float, "x": int, "y": int,
                "half": str, "quadrant": str, "timestamp": iso}}。
    未命中时 x/y=-1、half/quadrant 为空串，但保留最高 score 便于调参。
    """
    left, top, width, height = region
    if left < 0 or top < 0 or left + width > frame.shape[1] or top + height > frame.shape[0]:
        raise CaptureError(
            f"region {region} 超出帧范围 (H={frame.shape[0]}, W={frame.shape[1]})"
        )

    crop = frame[top : top + height, left : left + width]
    gray = to_gray(crop)
    timestamp = timestamp or datetime.now().astimezone().isoformat()

    results: Dict[str, Dict[str, object]] = {}
    for key, template in icon_templates.items():
        best_found = False
        best_score = -1.0
        best_position = (-1, -1)
        for scale in scales:
            size = max(2, int(round(icon_size * scale)))
            resized = resize_template(template, size)
            found, y, x, score = match_template(gray, resized, threshold=threshold)
            if score > best_score:
                best_score = score
                if found:
                    best_found = True
                    best_position = (y, x)

        y, x = best_position
        if best_found:
            zone = classify_quadrant(left + x, top + y, region)
            results[key] = {
                "visible": True,
                "score": round(best_score, 4),
                "x": int(left + x),
                "y": int(top + y),
                "half": zone["half"],
                "quadrant": zone["quadrant"],
                "timestamp": timestamp,
            }
        else:
            results[key] = {
                "visible": False,
                "score": round(best_score, 4),
                "x": -1,
                "y": -1,
                "half": "",
                "quadrant": "",
                "timestamp": timestamp,
            }
    return results


def fetch_icon_templates(
    lcu_port: int,
    lcu_token: str,
    champion_ids: Sequence[int],
    cache_dir: Optional[Path] = None,
    timeout: float = 15.0,
) -> Tuple[Dict[str, np.ndarray], List[str]]:
    """从 LCU 拉取英雄头像 PNG（本地缓存），返回 ({id_str: RGB 模板}, 失败列表)。

    缓存目录默认 assets/champion-icons；已缓存文件跳过下载。
    """
    cache_dir = cache_dir or icon_cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    image = _import_pil()

    context = ssl._create_unverified_context()
    auth = "Basic " + __import__("base64").b64encode(
        f"riot:{lcu_token}".encode("utf-8")
    ).decode("ascii")

    templates: Dict[str, np.ndarray] = {}
    failures: List[str] = []

    for champion_id in champion_ids:
        key = str(champion_id)
        cache_path = cache_dir / f"{key}.png"
        if not cache_path.is_file():
            url = ICON_URL_PATTERN.format(port=lcu_port, id=champion_id)
            request = urllib.request.Request(url, headers={"Authorization": auth})
            try:
                with urllib.request.urlopen(
                    request, timeout=timeout, context=context
                ) as response:
                    cache_path.write_bytes(response.read())
            except Exception as error:  # noqa: BLE001
                failures.append(f"{key}: {type(error).__name__}")
                continue
        try:
            with image.open(cache_path) as img:
                templates[key] = np.asarray(img.convert("RGB"))
        except Exception as error:  # noqa: BLE001
            failures.append(f"{key}: 解码失败 {error}")

    return templates, failures


def self_test() -> int:
    """离线自检：合成帧验证模板匹配命中/未命中与四象限判定（无需 mss/LCU）。"""
    failures: List[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(("PASS  " if condition else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
        if not condition:
            failures.append(name)

    rng = np.random.default_rng(42)
    frame = np.zeros((300, 300, 3), dtype=np.uint8)

    # 两个低互相关的可区分图标：白十字 + 白反斜线
    icon_cross = np.zeros((16, 16, 3), dtype=np.uint8)
    icon_cross[7, :] = 255
    icon_cross[:, 7] = 255
    icon_diag = np.zeros((16, 16, 3), dtype=np.uint8)
    for k in range(16):
        icon_diag[k, 15 - k] = 255

    frame[20:36, 20:36] = icon_cross        # 左上象限 TOP_LEFT
    frame[250:266, 250:266] = icon_diag     # 右下象限 BOTTOM_RIGHT
    # 轻微加噪（只加到背景，避免破坏图标）
    noise = rng.integers(0, 8, size=frame.shape, dtype=np.uint8)
    frame = np.clip(frame.astype(np.int16) + noise.astype(np.int16), 0, 255).astype(np.uint8)

    region = (0, 0, 300, 300)
    templates = {"cross": icon_cross, "diag": icon_diag}

    result = detect_minimap_enemies(
        frame, templates, region, threshold=0.6, icon_size=16, scales=(1.0,),
        timestamp="2026-01-01T00:00:00+08:00",
    )

    check("cross 命中", bool(result["cross"]["visible"]),
          f"score={result['cross']['score']} pos=({result['cross']['x']},{result['cross']['y']})")
    check("cross score ≈ 1", 0.9 < result["cross"]["score"] <= 1.01,
          f"score={result['cross']['score']}")
    check("cross 位置在左上附近", 15 <= result["cross"]["x"] <= 30 and 15 <= result["cross"]["y"] <= 30,
          f"pos=({result['cross']['x']},{result['cross']['y']})")
    check("cross 象限 TOP_LEFT（最高分处）", result["cross"]["quadrant"] == "TOP_LEFT",
          str(result["cross"]["quadrant"]))
    check("diag 命中", bool(result["diag"]["visible"]),
          f"score={result['diag']['score']} pos=({result['diag']['x']},{result['diag']['y']})")
    check("diag 象限 BOTTOM_RIGHT", result["diag"]["quadrant"] == "BOTTOM_RIGHT",
          str(result["diag"]["quadrant"]))
    check("时间戳写入", result["cross"]["timestamp"] == "2026-01-01T00:00:00+08:00")

    # 负例 1：空白噪声帧 -> 未命中
    blank = rng.integers(0, 12, size=(300, 300, 3), dtype=np.uint8)
    miss = detect_minimap_enemies(
        blank, {"cross": icon_cross}, region, threshold=0.9, icon_size=16,
        scales=(1.0,), timestamp="t"
    )
    check("负例未命中", not miss["cross"]["visible"],
          f"score={miss['cross']['score']}")
    check("负例字段为空", miss["cross"]["quadrant"] == "" and miss["cross"]["x"] == -1)

    # 负例 2：错误模板不误报（帧里只有 cross，用 diag 模板匹配 -> 低/负相关）
    cross_only = np.zeros((300, 300, 3), dtype=np.uint8)
    cross_only[20:36, 20:36] = icon_cross
    wrong = detect_minimap_enemies(
        cross_only, {"diag": icon_diag}, region, threshold=0.6, icon_size=16,
        scales=(1.0,), timestamp="t"
    )
    check("错误模板不误报", not wrong["diag"]["visible"],
          f"score={wrong['diag']['score']}")

    # 正例：半强度图标仍命中（模板匹配对整体亮度缩放不变）
    faint = np.zeros((300, 300, 3), dtype=np.uint8)
    faint[100:116, 100:116] = (icon_cross // 2)
    faint_result = detect_minimap_enemies(
        faint, {"cross": icon_cross}, region, threshold=0.9, icon_size=16,
        scales=(1.0,), timestamp="t"
    )
    check("半强度图标仍命中", bool(faint_result["cross"]["visible"]),
          f"score={faint_result['cross']['score']}")

    # 象限判定
    q = classify_quadrant(10, 10, region)
    check("quadrant TOP_LEFT", q["quadrant"] == "TOP_LEFT" and q["half"] == "TOP")
    q = classify_quadrant(299, 299, region)
    check("quadrant BOTTOM_RIGHT", q["quadrant"] == "BOTTOM_RIGHT")
    q = classify_quadrant(149, 200, (0, 0, 300, 300))
    check("quadrant BOTTOM_LEFT", q["quadrant"] == "BOTTOM_LEFT")

    # 模板大于帧 -> 未命中不崩溃
    found, y, x, score = match_template(
        np.zeros((8, 8), dtype=np.float64),
        np.zeros((16, 16), dtype=np.float64),
        threshold=0.5,
    )
    check("模板大于帧不崩溃", not found)

    # 区域越界 -> CaptureError
    try:
        detect_minimap_enemies(frame, {"cross": icon_cross}, (0, 0, 500, 300))
        check("区域越界报错", False)
    except CaptureError:
        check("区域越界报错", True)

    # mss 缺失时给出清晰提示（当前环境无 mss）
    try:
        _import_mss()
        print("  (环境存在 mss，跳过缺失提示检查)")
    except CaptureError as error:
        check("mss 缺失提示", "pip install mss" in str(error), str(error))

    if failures:
        print(f"\nFAILURES: {failures}")
        return 1
    print("\nSCREEN_CAPTURE SELF-TEST PASSED")
    return 0


def parse_region(value: Optional[str]) -> Optional[Tuple[int, int, int, int]]:
    if not value:
        return None
    parts = [int(p.strip()) for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("region 格式: left,top,width,height")
    return tuple(parts)  # type: ignore[return-value]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="小地图屏幕采集 + 英雄图标模板匹配（MVP）")
    parser.add_argument("--self-test", action="store_true", help="离线自检（合成帧）")
    parser.add_argument(
        "--once",
        action="store_true",
        help="抓一帧保存为 screenshot.png 并输出 JSON 识别结果（需 mss/Pillow）",
    )
    parser.add_argument(
        "--region",
        type=parse_region,
        default=None,
        help="截图区域 left,top,width,height（默认右下角小地图区域）",
    )
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--icon-size", type=int, default=DEFAULT_ICON_SIZE)
    parser.add_argument(
        "--templates",
        type=Path,
        default=None,
        help="本地图标模板目录（PNG 按 <key>.png），缺省不检测只截图",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("screenshot.png"),
        help="--once 的截图输出路径",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.once:
        parser.print_help()
        return 0

    region = args.region or DEFAULT_REGION
    frame = capture_game_region(region=region, monitor=args.monitor)

    templates: Dict[str, np.ndarray] = {}
    if args.templates and args.templates.is_dir():
        image = _import_pil()
        for path in sorted(args.templates.glob("*.png")):
            with image.open(path) as img:
                templates[path.stem] = np.asarray(img.convert("RGB"))
        print(f"加载模板 {len(templates)} 个: {list(templates)}")

    image = _import_pil()
    image.fromarray(frame).save(args.output)
    print(f"截图已保存: {args.output} 形状={frame.shape}")

    if templates:
        results = detect_minimap_enemies(
            frame, templates, region, threshold=args.threshold,
            icon_size=args.icon_size,
        )
        print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
