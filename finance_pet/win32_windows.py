"""用 Win32 枚举顶层窗口，直接找到 Codex 桌宠的覆盖窗口。

比只读 JSON 更可靠：桌宠被拖动、换显示器、显示/隐藏时，窗口本身的位置
永远是最新的。Codex 桌面端覆盖窗口的特征是：

* 类名 ``Chrome_WidgetWin_1``（Electron 窗口）；
* **没有标题**（主窗口标题是 "ChatGPT" / "Codex"，据此区分）；
* 尺寸较小（桌宠窗口远小于主窗口）；
* 所属进程的可执行文件名是 ``ChatGPT.exe`` / ``Codex.exe``。

全部为只读操作，不修改任何窗口状态。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]

_CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

#: Codex 桌宠窗口所属进程名（桌面端可执行文件名为 ChatGPT.exe）
CODEX_PROCESS_NAMES = ("chatgpt.exe", "codex.exe")


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    process: str
    class_name: str
    title: str
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


def _process_name(hwnd: int) -> tuple[int, str]:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return pid.value, ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return pid.value, buf.value
        return pid.value, ""
    finally:
        _kernel32.CloseHandle(handle)


def enumerate_windows(*, visible_only: bool = True) -> list[WindowInfo]:
    """枚举顶层窗口。"""
    out: list[WindowInfo] = []

    def _callback(hwnd, _lparam):  # noqa: ANN001
        if visible_only and not _user32.IsWindowVisible(hwnd):
            return True
        try:
            title_buf = ctypes.create_unicode_buffer(300)
            _user32.GetWindowTextW(hwnd, title_buf, 300)
            class_buf = ctypes.create_unicode_buffer(300)
            _user32.GetClassNameW(hwnd, class_buf, 300)
            rect = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            pid, path = _process_name(hwnd)
            out.append(
                WindowInfo(
                    hwnd=hwnd,
                    pid=pid,
                    process=path.rsplit("\\", 1)[-1].lower() if path else "",
                    class_name=class_buf.value,
                    title=title_buf.value,
                    x=rect.left,
                    y=rect.top,
                    width=rect.right - rect.left,
                    height=rect.bottom - rect.top,
                )
            )
        except Exception:
            pass
        return True

    _user32.EnumWindows(_CB(_callback), 0)
    return out


def find_pet_windows(
    *,
    process_names: tuple[str, ...] = CODEX_PROCESS_NAMES,
    max_size: int = 700,
) -> list[WindowInfo]:
    """找出所有可能是桌宠覆盖层的窗口（按面积从小到大）。"""
    found: list[WindowInfo] = []
    for win in enumerate_windows():
        if win.process not in process_names:
            continue
        if win.title.strip():
            continue
        if win.width < 80 or win.height < 80:
            continue
        if win.width > max_size or win.height > max_size:
            continue
        if win.x <= -30000 or win.y <= -30000:
            continue
        found.append(win)
    found.sort(key=lambda w: w.width * w.height)
    return found
