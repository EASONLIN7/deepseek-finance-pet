"""Windows 全局鼠标钩子：把"点击桌宠"变成回调。

Codex 的原生桌宠没有对外暴露 onClick，但它是一个独立窗口，位置已知。
这里用 ``WH_MOUSE_LL`` 低级鼠标钩子监听全局左键按下，命中桌宠矩形就触发
回调——效果等价于给桌宠挂上 onClick。顺带支持一个全局快捷键作为备用入口。

钩子运行在独立线程里（低级钩子要求所在线程跑消息循环），回调不能直接碰
Tkinter，所以事件统一丢进 ``self.events`` 队列，由 UI 线程用 ``after`` 轮询。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable

from .pet_locator import MascotRect

WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_user32.CallNextHookEx.restype = LRESULT
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]


def parse_hotkey(combo: str) -> tuple[int, int] | None:
    """把 "ctrl+alt+b" 解析成 (modifiers, virtual-key)。"""
    if not combo:
        return None
    mods = 0
    key: str | None = None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in {"ctrl", "control"}:
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in {"win", "super", "meta"}:
            mods |= MOD_WIN
        elif part:
            key = part
    if key is None or mods == 0:
        return None
    if len(key) == 1:
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return mods | MOD_NOREPEAT, vk


class PetClickHook:
    """监听桌宠点击的钩子。线程安全，可在 Tk 主循环旁并行运行。"""

    def __init__(
        self,
        rect_provider: Callable[[], MascotRect | None] | None,
        *,
        padding: int = 6,
        hotkey: str | None = "ctrl+alt+b",
        throttle: float = 0.25,
    ) -> None:
        self.rect_provider = rect_provider
        self.padding = padding
        self.hotkey = hotkey
        self.throttle = throttle

        self.events: queue.Queue[str] = queue.Queue()
        self.last_point: tuple[int, int] | None = None

        # 拖拽跟踪：按下桌宠后记录起点，移动时只更新"最新点"（不排队，
        # 否则每秒上百个 move 事件会把队列撑爆），松开时结束。
        self.dragging = False
        self.drag_start: tuple[int, int] | None = None
        self.drag_point: tuple[int, int] | None = None
        self.drag_moved = False

        self.installed = False
        self.hotkey_registered = False
        self.last_error: str | None = None

        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hook = None
        self._proc = None  # 必须持有引用，否则会被 GC
        self._last_fire = 0.0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return self.installed
        self._thread = threading.Thread(target=self._run, name="finance-pet-hook", daemon=True)
        self._thread.start()
        for _ in range(50):
            if self._thread_id is not None or not self._thread.is_alive():
                break
            time.sleep(0.02)
        return self.installed

    def stop(self) -> None:
        if self._thread_id:
            WM_QUIT = 0x0012
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.0)

    # ------------------------------------------------------------------ 内部
    def _is_pet_click(self, x: int, y: int) -> bool:
        if self.rect_provider is None:
            return True  # 校准模式：捕获任意一次点击
        rect = self.rect_provider()
        if not rect or not rect.overlay_open:
            return False
        return rect.contains(x, y, self.padding)

    def _emit(self, kind: str) -> None:
        now = time.monotonic()
        if now - self._last_fire < self.throttle:
            return
        self._last_fire = now
        self.events.put(kind)

    # ------------------------------------------------------------------ 拖拽
    def drag_delta(self) -> tuple[int, int]:
        """相对按下点的位移（没有在拖就返回 (0, 0)）。"""
        if not self.dragging or self.drag_start is None or self.drag_point is None:
            return 0, 0
        return self.drag_point[0] - self.drag_start[0], self.drag_point[1] - self.drag_start[1]

    def _run(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()

        def _callback(n_code, w_param, l_param):  # noqa: ANN001
            try:
                if n_code == 0:
                    if w_param in (WM_LBUTTONDOWN, WM_MOUSEMOVE, WM_LBUTTONUP):
                        info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        x, y = info.pt.x, info.pt.y
                        if w_param == WM_LBUTTONDOWN:
                            self.last_point = (x, y)
                            if self._is_pet_click(x, y):
                                # 在桌宠上按下：进入拖拽跟踪，并当成一次点击
                                self.dragging = True
                                self.drag_start = (x, y)
                                self.drag_point = (x, y)
                                self.drag_moved = False
                                self._emit("click")
                        elif w_param == WM_MOUSEMOVE and self.dragging:
                            self.drag_point = (x, y)
                            if self.drag_start and (
                                abs(x - self.drag_start[0]) > 3
                                or abs(y - self.drag_start[1]) > 3
                            ):
                                self.drag_moved = True
                        elif w_param == WM_LBUTTONUP and self.dragging:
                            self.drag_point = (x, y)
                            moved = self.drag_moved
                            self.dragging = False
                            self.drag_start = None
                            self.drag_moved = False
                            if moved:
                                self._emit("dragend")
            except Exception as exc:  # noqa: BLE001 - 钩子里绝不能抛
                self.last_error = str(exc)
            return _user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = HOOKPROC(_callback)
        self._hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, None, 0)
        if not self._hook:
            self.last_error = f"SetWindowsHookExW 失败 (err={ctypes.get_last_error()})"
        else:
            self.installed = True

        parsed = parse_hotkey(self.hotkey or "")
        if parsed:
            mods, vk = parsed
            self.hotkey_registered = bool(_user32.RegisterHotKey(None, 1, mods, vk))
            if not self.hotkey_registered:
                self.last_error = "全局快捷键注册失败（可能被占用）"

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self._emit("hotkey")
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            _user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self.hotkey_registered:
            _user32.UnregisterHotKey(None, 1)
            self.hotkey_registered = False
        self.installed = False

    def poll(self) -> list[str]:
        """取走自上次调用以来累积的事件（供 Tk 线程调用）。"""
        out: list[str] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out
