"""定位 Codex 原生桌宠在屏幕上的位置。

Codex 桌面端把桌宠渲染成一个独立的 Electron 覆盖窗口（avatar overlay），
并把它的几何信息写进 ``$CODEX_HOME/.codex-global-state.json`` 的
``electron-avatar-overlay-bounds`` 字段，形如::

    {"x": 2104, "y": 873, "displayId": 1060407849,
     "mascot": {"left": 162, "top": 132, "width": 166, "height": 180},
     "anchor": {"x": 1454, "y": 566, "width": 166, "height": 180},
     "byDisplayId": {"1060407849": {...}}, "byResolution": {"1707x1067": {...}}}

其中 ``mascot`` 相对窗口，``anchor`` 已是屏幕绝对坐标。
本模块把上式归一化成屏幕绝对矩形 ``(x, y, w, h)``，供点击判定与气泡定位使用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 兜底尺寸（v2 桌宠精灵格 192x208 的实际显示大小约 166x180）
FALLBACK_SIZE = (166, 180)


@dataclass
class MascotRect:
    x: int
    y: int
    width: int
    height: int
    overlay_open: bool = True
    source: str = "unknown"
    display_bounds: tuple[int, int, int, int] | None = None

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def contains(self, px: int, py: int, padding: int = 0) -> bool:
        return (
            self.x - padding <= px <= self.right + padding
            and self.y - padding <= py <= self.bottom + padding
        )

    def as_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height


def _entry_rect(entry: Any, key: str = "mascot") -> tuple[int, int, int, int] | None:
    """从 byDisplayId / byResolution 的子条目里取出绝对矩形。"""
    if not isinstance(entry, dict):
        return None
    anchor = entry.get("anchor")
    if isinstance(anchor, dict) and anchor.get("width"):
        return (
            int(anchor.get("x", 0)),
            int(anchor.get("y", 0)),
            int(anchor["width"]),
            int(anchor.get("height") or FALLBACK_SIZE[1]),
        )
    box = entry.get(key)
    if isinstance(box, dict) and box.get("width"):
        return (
            int(entry.get("x", 0)) + int(box.get("left", 0)),
            int(entry.get("y", 0)) + int(box.get("top", 0)),
            int(box["width"]),
            int(box.get("height") or FALLBACK_SIZE[1]),
        )
    if entry.get("width") and entry.get("height"):
        return (
            int(entry.get("x", 0)),
            int(entry.get("y", 0)),
            int(entry["width"]),
            int(entry["height"]),
        )
    return None


def read_global_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def locate(
    global_state_path: Path,
    *,
    override: tuple[int, int, int, int] | None = None,
    offset: tuple[int, int, int, int] | None = None,
    use_windows: bool = True,
) -> MascotRect | None:
    """解析出桌宠的屏幕矩形；解析不出来时返回 None。

    优先级：手动覆盖 > 实时窗口枚举 > global-state 里的锚点/偏移。
    """
    if override:
        return MascotRect(*override, source="config.mascot_rect")

    if use_windows:
        live = locate_from_windows()
        if live is not None:
            return live

    state = read_global_state(global_state_path)
    if not state:
        return None

    overlay_open = bool(state.get("electron-avatar-overlay-open", True))
    bounds = state.get("electron-avatar-overlay-bounds")
    if not isinstance(bounds, dict):
        return None

    display_bounds = None
    db = bounds.get("displayBounds")
    if isinstance(db, dict) and db.get("width"):
        display_bounds = (
            int(db.get("x", 0)),
            int(db.get("y", 0)),
            int(db["width"]),
            int(db.get("height", 0)),
        )

    display_id = bounds.get("displayId")
    by_display = bounds.get("byDisplayId") or {}
    entry = by_display.get(str(display_id)) if display_id is not None else None
    rect = _entry_rect(entry)
    source = "byDisplayId"

    if rect is None:
        rect = _entry_rect(bounds)
        source = "top-level"

    if rect is None and display_bounds:
        key = f"{display_bounds[2]}x{display_bounds[3]}"
        rect = _entry_rect((bounds.get("byResolution") or {}).get(key))
        source = "byResolution"

    if rect is None and bounds.get("x") is not None and bounds.get("y") is not None:
        rect = (int(bounds["x"]), int(bounds["y"]), *FALLBACK_SIZE)
        source = "fallback-size"

    if rect is None:
        return None

    # 叠加"精灵图内边距"修正量（由 --autodetect 自动测得）
    if offset:
        dx, dy, dw, dh = offset
        rect = (rect[0] + dx, rect[1] + dy, max(20, rect[2] + dw), max(20, rect[3] + dh))

    return MascotRect(
        x=rect[0],
        y=rect[1],
        width=rect[2],
        height=rect[3],
        overlay_open=overlay_open,
        source=source,
        display_bounds=display_bounds,
    )


#: v2 桌宠在覆盖窗口内的默认位置（窗口 356x320 时桌宠位于 left=162, top=132）
MASCOT_INSET = (162, 132, 166, 180)


def mascot_from_window(win: Any) -> MascotRect:
    """把覆盖窗口矩形换算成桌宠本体的矩形。"""
    if win.width <= 260 and win.height <= 260:
        # 自由定位时窗口通常刚好包住桌宠
        return MascotRect(win.x, win.y, win.width, win.height, source="window")
    left, top, mw, mh = MASCOT_INSET
    if win.width >= left + mw and win.height >= top + mh:
        return MascotRect(win.x + left, win.y + top, mw, mh, source="window+inset")
    return MascotRect(win.right - mw, win.bottom - mh, mw, mh, source="window+bottom-right")


def locate_from_windows() -> MascotRect | None:
    """通过枚举窗口实时定位桌宠；找不到时返回 None。"""
    try:
        from .win32_windows import find_pet_windows
    except Exception:
        return None
    try:
        windows = find_pet_windows()
    except Exception:
        return None
    if not windows:
        return None
    return mascot_from_window(windows[0])


def save_mascot_rect(config_path: Path, rect: tuple[int, int, int, int]) -> Path:
    """把校准结果写回 config.json（保留已有键）。"""
    data: dict[str, Any] = {}
    if config_path.is_file():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
    data["mascot_rect"] = [int(v) for v in rect]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


class PetWatcher:
    """监视 global-state 文件变化，按需重新解析桌宠矩形。

    Codex 拖动桌宠、切换显示器、隐藏桌宠都会改写这个文件，因此这里用
    mtime 做轻量检测；即使文件没变，也会按 ``min_interval`` 做一次实时
    窗口枚举兜底（桌宠移动不一定每次都落盘）。
    """

    def __init__(
        self,
        global_state_path: Path,
        *,
        override: tuple[int, int, int, int] | None = None,
        offset: tuple[int, int, int, int] | None = None,
        min_interval: float = 1.5,
    ) -> None:
        self.path = Path(global_state_path)
        self.override = override
        self.offset = offset
        self.min_interval = min_interval
        self._mtime: float | None = None
        self._rect: MascotRect | None = None
        self._last_locate = 0.0

    def poll(self, *, force: bool = False, fast: bool = False) -> MascotRect | None:
        """返回当前桌宠矩形。

        ``fast=True`` 用于"跟随拖动"这种高频调用：只做一次 stat，文件没变就
        直接返回缓存，也不去枚举窗口（省掉 EnumWindows 的开销）。
        """
        if self.override:
            self._rect = MascotRect(*self.override, source="config.mascot_rect")
            return self._rect
        now = time.monotonic()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._rect
        stale = (not fast) and (now - self._last_locate >= self.min_interval)
        if force or self._mtime != mtime or stale:
            self._mtime = mtime
            self._last_locate = now
            self._rect = locate(self.path, offset=self.offset, use_windows=not fast)
        return self._rect

    @property
    def rect(self) -> MascotRect | None:
        return self._rect
