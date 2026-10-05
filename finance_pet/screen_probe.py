"""屏幕探针：自动找出桌宠本体的真实像素范围。

Codex 给出的矩形是**精灵图单元格**，单元格里带透明内边距，所以直接用它做
点击判定会偏一二十像素。这里用"动画差分"自动纠正：

桌宠在待机时仍在做微小的呼吸/摆动动画，而周围界面通常是静止的。连续抓取
若干帧，统计每个像素的变化幅度，变化明显的那一团的包围盒就是桌宠本体。

不依赖任何窗口标题、类名或内部状态，也不需要用户手动点击。
"""

from __future__ import annotations

import time
from typing import Iterable

from PIL import Image, ImageChops, ImageGrab


def grab(bbox: tuple[int, int, int, int]) -> Image.Image:
    """抓取屏幕一块区域（物理像素，含多屏）。"""
    return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")


def _spans(mask_cols: list[int], mask_rows: list[int], min_run: int) -> tuple[int, int]:
    """在投影序列里找"包含最大值的那段连续有效区间"。"""
    def pick(values: list[int]) -> tuple[int, int]:
        if not values:
            return 0, -1
        peak = max(values)
        if peak <= 0:
            return 0, -1
        threshold = max(1, min(peak, peak // 6))
        best_lo = best_hi = 0
        best_len = -1
        lo = None
        for i, v in enumerate(values):
            if v >= threshold:
                if lo is None:
                    lo = i
            elif lo is not None:
                if i - lo > best_len:
                    best_lo, best_hi, best_len = lo, i - 1, i - lo
                lo = None
        if lo is not None and len(values) - lo > best_len:
            best_lo, best_hi, best_len = lo, len(values) - 1, len(values) - lo
        if best_len < min_run:
            return 0, -1
        return best_lo, best_hi

    return pick(mask_cols), pick(mask_rows)  # type: ignore[return-value]


def detect_motion_bbox(
    region: tuple[int, int, int, int],
    *,
    frames: int = 14,
    interval: float = 0.12,
    threshold: int = 9,
    min_size: int = 40,
) -> tuple[int, int, int, int] | None:
    """在 ``region``（屏幕绝对坐标 l,t,r,b）内找出正在动的那个物体的包围盒。

    返回绝对坐标 ``(x, y, w, h)``；没检测到明显动画时返回 None。
    """
    left, top, right, bottom = region
    if right - left < 20 or bottom - top < 20:
        return None

    shots: list[Image.Image] = []
    for i in range(max(3, frames)):
        shots.append(grab(region))
        if i < frames - 1:
            time.sleep(interval)

    base = shots[0]
    width, height = base.size
    acc = Image.new("L", (width, height), 0)
    for shot in shots[1:]:
        diff = ImageChops.difference(base, shot).convert("L")
        acc = ImageChops.lighter(acc, diff)

    mask = acc.point(lambda v: 255 if v >= threshold else 0)
    cols = [0] * width
    rows = [0] * height
    pixels = mask.load()
    for y in range(height):
        for x in range(width):
            if pixels[x, y]:
                cols[x] += 1
                rows[y] += 1

    (x0, x1), (y0, y1) = _spans(cols, rows, min_size)
    if x1 < x0 or y1 < y0:
        return None

    w = x1 - x0 + 1
    h = y1 - y0 + 1
    if w < min_size or h < min_size:
        return None
    return left + x0, top + y0, w, h


def expand_region(
    rect: tuple[int, int, int, int],
    *,
    pad: int = 130,
    screen: tuple[int, int] | None = None,
) -> tuple[int, int, int, int]:
    """把桌宠矩形扩成一个搜索区域，返回 ``(l, t, r, b)``。"""
    x, y, w, h = rect
    left, top = x - pad, y - pad
    right, bottom = x + w + pad, y + h + pad
    if screen:
        sw, sh = screen
        left, top = max(0, left), max(0, top)
        right, bottom = min(sw, right), min(sh, bottom)
    return left, top, right, bottom


def best_of(regions: Iterable[tuple[int, int, int, int]]) -> tuple[int, int, int, int] | None:
    """依次尝试多个搜索区域，返回第一个命中的结果。"""
    for region in regions:
        found = detect_motion_bbox(region)
        if found:
            return found
    return None
