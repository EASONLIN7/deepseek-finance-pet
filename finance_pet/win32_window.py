"""无边框悬浮窗：用 Win32 分层窗口（Layered Window）承载气泡贴图。

为什么不用 Tkinter / Qt：

* ``UpdateLayeredWindow`` + 预乘 alpha 能做到**真·逐像素透明**，圆角和
  阴影都是平滑的，不受 ``-transparentcolor`` 色键限制；
* 不依赖 Tcl/Tk —— 很多精简版 Python 运行时（包括 Codex 自带的运行时）
  没有可用的 Tcl 库，走原生 Win32 更稳；
* 气泡只需"展示 + 一个按钮"，用不上完整 GUI 框架。

窗口样式 = ``WS_POPUP`` + ``WS_EX_LAYERED | WS_EX_TOOLWINDOW |
WS_EX_TOPMOST | WS_EX_NOACTIVATE``，即：无边框、置顶、不进任务栏、不抢焦点。
点击在 ``WM_LBUTTONDOWN`` 里自行判定：命中重试按钮就重试，落在卡片其它
位置就关闭气泡。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Callable

from PIL import Image, ImageChops

from . import bubble as bubble_render

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
PM_REMOVE = 0x0001

DIB_RGB_COLORS = 0
BI_RGB = 0

LRESULT = ctypes.c_ssize_t


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p

_user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
_user32.RegisterClassExW.restype = wintypes.WORD
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
_user32.UpdateLayeredWindow.restype = wintypes.BOOL

_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]


def premultiplied_bgra(img: Image.Image) -> bytes:
    """Pillow RGBA -> Windows 分层窗口需要的预乘 BGRA 字节流。

    ``AC_SRC_ALPHA`` 要求颜色分量已经乘以 alpha，否则半透明像素会发亮。
    """
    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    premul = Image.merge(
        "RGBA",
        (
            ImageChops.multiply(r, a),
            ImageChops.multiply(g, a),
            ImageChops.multiply(b, a),
            a,
        ),
    )
    # 重新排列成 B,G,R,A —— 这样 tobytes() 的内存顺序正是 BGRA
    pb, pg, pr, pa = premul.split()
    return Image.merge("RGBA", (pb, pg, pr, pa)).tobytes()


class BubbleWindow:
    """置顶悬浮气泡窗口（原生 Win32 分层窗口）。"""

    _class_name = "CodexFinancePetBubble"
    _class_registered = False

    def __init__(
        self,
        *,
        width: int = 320,
        theme: str = "dark",
        on_retry: Callable[[], None] | None = None,
        on_dismiss: Callable[[], None] | None = None,
    ) -> None:
        self.width = width
        self.theme = theme if theme in bubble_render.PALETTES else "dark"
        self.on_retry = on_retry
        self.on_dismiss = on_dismiss

        self.hwnd: int | None = None
        self._proc = WNDPROC(self._wndproc)   # 必须持有引用，否则被 GC 后回调崩溃
        self._memdc: int | None = None
        self._bitmap: int | None = None
        self._old_bitmap: int | None = None
        self._bits: int | None = None
        self._dib_size = (0, 0)

        self._geometry: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._retry_rect: tuple[int, int, int, int] | None = None
        self._card_rect: tuple[int, int, int, int] | None = None
        self._hide_at: float | None = None
        self._visible = False
        self._mascot: Any = None
        self._offset = 8
        self._last_image: Image.Image | None = None
        self._last_payload: dict[str, Any] | None = None
        self._last_size: tuple[int, int] = (0, 0)
        self._last_tail = "down"
        self._last_content_h = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------- 窗口生命周期
    def _ensure_window(self) -> bool:
        if self.hwnd:
            return True
        hinst = _kernel32.GetModuleHandleW(None)
        if not BubbleWindow._class_registered:
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(self._proc, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                self.last_error = f"RegisterClassExW 失败 (err={ctypes.get_last_error()})"
                return False
            BubbleWindow._class_registered = True

        ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        hwnd = _user32.CreateWindowExW(
            ex_style, self._class_name, "赛博财务管家", WS_POPUP,
            0, 0, 32, 32, None, None, hinst, None,
        )
        if not hwnd:
            self.last_error = f"CreateWindowExW 失败 (err={ctypes.get_last_error()})"
            return False
        self.hwnd = hwnd
        return True

    def _ensure_dib(self, size: tuple[int, int]) -> bool:
        if self._dib_size == size and self._bits:
            return True
        self._release_dib()
        w, h = size
        screen_dc = _user32.GetDC(None)
        self._memdc = _gdi32.CreateCompatibleDC(screen_dc)
        _user32.ReleaseDC(None, screen_dc)
        if not self._memdc:
            self.last_error = "CreateCompatibleDC 失败"
            return False
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h            # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        self._bitmap = _gdi32.CreateDIBSection(
            self._memdc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not self._bitmap:
            self.last_error = "CreateDIBSection 失败"
            return False
        self._bits = bits.value
        self._old_bitmap = _gdi32.SelectObject(self._memdc, self._bitmap)
        self._dib_size = size
        return True

    def _release_dib(self) -> None:
        if self._memdc and self._old_bitmap:
            _gdi32.SelectObject(self._memdc, self._old_bitmap)
            self._old_bitmap = None
        if self._bitmap:
            _gdi32.DeleteObject(self._bitmap)
            self._bitmap = None
        if self._memdc:
            _gdi32.DeleteDC(self._memdc)
            self._memdc = None
        self._bits = None
        self._dib_size = (0, 0)

    # ------------------------------------------------------------------ 对外 API
    def set_anchor(self, rect: Any, offset: int = 8) -> None:
        """设置桌宠矩形与气泡间距。"""
        self._mascot = rect
        self._offset = offset

    @property
    def visible(self) -> bool:
        return self._visible

    def anchor_rect(self) -> tuple[int, int, int, int]:
        return self._geometry

    def reposition(self) -> bool:
        """按当前锚点重新摆放气泡（不重新渲染贴图）。

        拖拽跟随会每秒调用几十次，所以这里只做一次坐标计算 + SetWindowPos；
        只有"上方空间不够、尾巴要翻面"时才重新渲染。
        """
        if not self.hwnd or not self._visible or self._last_size == (0, 0):
            return False
        x, y = self._place(self._last_size)
        if (x, y) == (self._geometry[0], self._geometry[1]):
            return False
        if self._last_payload is not None:
            tail = self._choose_tail(self._last_content_h)
            if tail != self._last_tail:
                self._present(self._last_payload)
                return True
        ok = _user32.SetWindowPos(
            self.hwnd, None, x, y, 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        if ok:
            self._geometry = (x, y, self._last_size[0], self._last_size[1])
        return bool(ok)

    def show(self, payload: dict[str, Any], *, auto_hide: float = 20.0) -> None:
        self._present(payload)
        self._hide_at = time.monotonic() + auto_hide if auto_hide and auto_hide > 0 else None

    def update(self, payload: dict[str, Any], *, auto_hide: float = 20.0) -> None:
        self.show(payload, auto_hide=auto_hide)

    def hide(self) -> None:
        if self.hwnd:
            _user32.ShowWindow(self.hwnd, SW_HIDE)
        self._visible = False
        self._hide_at = None

    def destroy(self) -> None:
        if self.hwnd:
            _user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        self._release_dib()

    def pump(self) -> None:
        """处理窗口消息与自动隐藏计时；需在主线程周期调用。"""
        msg = wintypes.MSG()
        while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        if self._hide_at is not None and time.monotonic() >= self._hide_at:
            self.hide()

    # ------------------------------------------------------------------ 内部实现
    def _place(self, size: tuple[int, int]) -> tuple[int, int]:
        w, h = size
        screen_w = _user32.GetSystemMetrics(0)
        screen_h = _user32.GetSystemMetrics(1)
        rect = self._mascot
        if rect is None:
            return max(4, (screen_w - w) // 2), max(4, (screen_h - h) // 2)
        x = rect.x + rect.width // 2 - w // 2
        above = rect.y - h - self._offset
        y = above if above >= 0 else rect.y + rect.height + self._offset
        return max(4, min(x, screen_w - w - 4)), max(4, min(y, screen_h - h - 4))

    def _choose_tail(self, content_h: int) -> str:
        if self._mascot is None:
            return "down"
        total_h = content_h + bubble_render.TAIL_H + bubble_render.MARGIN * 2
        return "down" if self._mascot.y - total_h - self._offset >= 0 else "up"

    def _present(self, payload: dict[str, Any]) -> None:
        if not self._ensure_window():
            return
        m = bubble_render.metrics_for(
            self.width,
            bool(payload.get("show_retry")),
            has_notice=bool(payload.get("notice")),
        )
        payload = dict(payload)
        payload["_tail"] = self._choose_tail(m["content_h"])

        img, geometry = bubble_render.compose(payload, theme=self.theme, width=self.width)
        self._last_image = img
        self._last_payload = payload
        self._last_size = img.size
        self._last_tail = payload["_tail"]
        self._last_content_h = m["content_h"]
        self._retry_rect = geometry["retry"]
        self._card_rect = geometry["card"]

        size = img.size
        if not self._ensure_dib(size):
            return
        data = premultiplied_bgra(img)
        ctypes.memmove(self._bits, data, len(data))

        x, y = self._place(size)
        pt_dst = wintypes.POINT(x, y)
        pt_src = wintypes.POINT(0, 0)
        sz = wintypes.SIZE(size[0], size[1])
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        screen_dc = _user32.GetDC(None)
        ok = _user32.UpdateLayeredWindow(
            self.hwnd, screen_dc, ctypes.byref(pt_dst), ctypes.byref(sz),
            self._memdc, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA,
        )
        _user32.ReleaseDC(None, screen_dc)
        if not ok:
            self.last_error = f"UpdateLayeredWindow 失败 (err={ctypes.get_last_error()})"
            return
        _user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._geometry = (x, y, size[0], size[1])
        self._visible = True

    def _hit(self, x: int, y: int, rect: tuple[int, int, int, int] | None) -> bool:
        if not rect:
            return False
        rx, ry, rw, rh = rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    def _wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == WM_LBUTTONDOWN:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                if self._hit(x, y, self._retry_rect):
                    if callable(self.on_retry):
                        self.on_retry()
                elif callable(self.on_dismiss):
                    self.on_dismiss()
                else:
                    self.hide()
                return 0
            if msg == WM_RBUTTONDOWN:
                self.hide()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
        except Exception as exc:  # noqa: BLE001 - 窗口过程绝不能抛
            self.last_error = str(exc)
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
