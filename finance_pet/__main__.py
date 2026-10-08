"""命令行入口：``python -m finance_pet``。

常用参数::

    python -m finance_pet                 # 启动桌宠挂件（常驻）
    python -m finance_pet --once          # 只采一次数据并打印 JSON
    python -m finance_pet --preview out   # 导出明暗两套气泡预览图
    python -m finance_pet --no-hook       # 不装鼠标钩子（调试用）
    python -m finance_pet --show          # 启动后直接把气泡弹出来
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .balance import fetch_balance
from .alerts import AlertEngine
from .config import Config, load_config
from .ledger import Ledger
from .pet_locator import MascotRect, PetWatcher
from .service import alert_payload, collect_snapshot, loading_payload, refresh_ledger


def _init_streams() -> None:
    """pythonw.exe 下 stdout 为 None，这里统一兜底，避免 print 崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _enable_dpi_awareness() -> None:
    """让进程使用物理像素坐标。

    否则在缩放不是 100% 的屏幕上，Win32 窗口坐标与截屏像素会对不上，
    贴图和点击判定都会整体偏移。
    """
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass


_MUTEX = None


def _acquire_single_instance() -> bool:
    """防止重复启动（否则会装两个钩子、弹两个气泡）。

    返回 True 表示本次是唯一实例。
    """
    global _MUTEX
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\CodexFinancePetSingleton")
        _MUTEX = handle  # 保持引用，进程退出前不要释放
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _primary_balance(balance: dict[str, Any]) -> float | None:
    """从余额响应里取出主币种余额（拿不到就返回 None）。"""
    primary = (balance or {}).get("primary") or {}
    raw = primary.get("total_balance")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _primary_currency(balance: dict[str, Any], config: Config) -> str:
    primary = (balance or {}).get("primary") or {}
    return str(primary.get("currency") or config.currency)


def _note_balance(engine: AlertEngine, balance: dict[str, Any]) -> None:
    """把每次拿到的余额喂给提醒引擎的采样缓冲。"""
    value = _primary_balance(balance)
    if value is not None:
        engine.observe_balance(value)


def _log(*parts: Any) -> None:
    if sys.stdout is None:
        return
    try:
        print(*parts)
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance_pet",
        description="赛博财务管家 —— 挂在 Codex 原生桌宠上的 DeepSeek 余额面板",
    )
    parser.add_argument("--config", type=Path, help="指定 config.json 路径")
    parser.add_argument("--once", action="store_true", help="只采集一次快照并打印 JSON")
    parser.add_argument("--scan-only", action="store_true", help="只增量扫描本地用量，不联网")
    parser.add_argument("--preview", type=Path, help="导出明暗两套气泡预览图到该目录")
    parser.add_argument("--no-hook", action="store_true", help="不安装全局鼠标钩子")
    parser.add_argument("--show", action="store_true", help="启动后立即显示气泡")
    parser.add_argument("--where", action="store_true", help="打印当前检测到的桌宠矩形后退出")
    parser.add_argument("--calibrate", action="store_true",
                        help="校准模式：点击一次桌宠，把它的位置写入 config.json")
    parser.add_argument("--autodetect", action="store_true",
                        help="自动探测桌宠的真实像素范围并写入 mascot_offset")
    parser.add_argument("--autodetect-frames", type=int, default=14, help=argparse.SUPPRESS)
    parser.add_argument("--once-ms", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    _init_streams()
    _enable_dpi_awareness()
    args = build_parser().parse_args(argv)
    config = load_config(args.config)

    if args.preview:
        from .bubble import export_preview

        args.preview.mkdir(parents=True, exist_ok=True)
        variants = [
            ("bubble-dark", dict(theme="dark")),
            ("bubble-dark-retry", dict(theme="dark", show_retry=True)),
            ("bubble-dark-error", dict(theme="dark", error=True)),
            ("bubble-dark-low", dict(theme="dark", variant="low")),
            ("bubble-dark-burst", dict(theme="dark", variant="burst")),
            ("bubble-light", dict(theme="light")),
            ("bubble-light-retry", dict(theme="light", show_retry=True)),
            ("bubble-light-error", dict(theme="light", error=True)),
            ("bubble-light-low", dict(theme="light", variant="low")),
            ("bubble-light-burst", dict(theme="light", variant="burst")),
        ]
        for name, kwargs in variants:
            target = args.preview / f"{name}.png"
            export_preview(str(target), width=config.width, **kwargs)
            _log(f"预览已导出: {target}")
        return 0

    # 下面这些命令只读 Desk 状态，不需要（也就会去创建）账本数据库
    if args.where:
        from .pet_locator import locate
        from .win32_windows import window_at_point

        rect = locate(config.global_state_path, override=config.mascot_rect,
                      offset=config.mascot_offset)
        hit = window_at_point(*rect.center) if rect else None
        _log(json.dumps(
            {
                "rect": list(rect.as_tuple()) if rect else None,
                "source": rect.source if rect else None,
                "overlay_open": rect.overlay_open if rect else None,
                # 该位置当前实际命中的窗口：不是 Codex 就说明桌宠被遮挡/已退出
                "hit_process": hit.process if hit else None,
                "hit_class": hit.class_name if hit else None,
                "hit_is_codex": bool(hit and hit.process in config.pet_process_names),
            },
            ensure_ascii=False,
        ))
        return 0

    if args.calibrate:
        return calibrate(config)

    if args.autodetect:
        return autodetect(config, frames=args.autodetect_frames)

    ledger = Ledger(config.db_path)

    if args.scan_only:
        _log(json.dumps(refresh_ledger(config, ledger), ensure_ascii=False))
        ledger.close()
        return 0

    if args.once:
        snapshot = collect_snapshot(config, ledger)
        _log(json.dumps(snapshot.payload, ensure_ascii=False, indent=2))
        ledger.close()
        return 0

    if not _acquire_single_instance():
        _log("赛博财务管家已经在运行了（找到已有实例），本次退出。")
        ledger.close()
        return 0

    return run_app(
        config,
        ledger,
        no_hook=args.no_hook,
        show_now=args.show,
        lifetime_ms=args.once_ms,
    )


def calibrate(config: Config, timeout: float = 30.0) -> int:
    """让用户点一次桌宠，把位置写入 config.json。"""
    from .click_hook import PetClickHook
    from .pet_locator import MASCOT_INSET, save_mascot_rect

    target = config.data_dir / "config.json"
    _log("校准模式：请在 30 秒内用鼠标左键点击一下桌宠本体……")
    hook = PetClickHook(None, hotkey=None, throttle=0.0)
    if not hook.start() or not hook.installed:
        _log(f"无法安装鼠标钩子：{hook.last_error}")
        return 1
    deadline = time.monotonic() + timeout
    point = None
    try:
        while time.monotonic() < deadline:
            if hook.poll():
                point = hook.last_point
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        hook.stop()

    if not point:
        _log("超时未检测到点击，未做修改。")
        return 1
    mw, mh = MASCOT_INSET[2], MASCOT_INSET[3]
    rect = (point[0] - mw // 2, point[1] - mh // 2, mw, mh)
    save_mascot_rect(target, rect)
    _log(f"已记录桌宠位置 {rect} -> {target}")
    return 0


def autodetect(config: Config, *, frames: int = 14) -> int:
    """用动画差分自动测出桌宠真实范围，并把修正量写入 config.json。"""
    from .pet_locator import locate
    from .screen_probe import best_of, detect_motion_bbox, expand_region

    base = locate(config.global_state_path, override=None, use_windows=True)
    if base is None:
        _log("拿不到 Codex 的桌宠矩形，先确认桌宠正在显示。")
        return 1
    _log(f"基础矩形（来自 Codex）：{base.as_tuple()}  [{base.source}]")

    screen = (ctypes.WinDLL("user32").GetSystemMetrics(0),
              ctypes.WinDLL("user32").GetSystemMetrics(1))
    wide = expand_region(base.as_tuple(), pad=140, screen=screen)
    tight = expand_region(base.as_tuple(), pad=60, screen=screen)

    _log(f"正在采集 {frames} 帧做动画差分，请让桌宠保持可见（约 {frames * 0.12:.1f} 秒）…")
    found = best_of([tight, wide])
    if found is None:
        # 兜底：直接按 Codex 矩形再试一次
        found = detect_motion_bbox(wide, frames=max(frames, 20), interval=0.16, threshold=6)
    if found is None:
        _log("没有检测到明显动画。可能桌宠此刻静止或被窗口遮挡；"
             "可改用 .\\run.ps1 -Calibrate 手动点一下。")
        return 1

    x, y, w, h = found
    bx, by, bw, bh = base.as_tuple()
    offset = (x - bx, y - by, w - bw, h - bh)
    _log(f"检测到桌宠本体：({x}, {y}) {w}x{h}")
    _log(f"相对 Codex 矩形的修正量：{offset}")

    target = config.data_dir / "config.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        data = {}
    # 存"偏移量"而不是固定矩形：桌宠被拖动/换屏后仍能跟随
    data.pop("mascot_rect", None)
    data["mascot_offset"] = list(offset)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"已写入 {target}（mascot_offset）")
    return 0


def run_app(
    config: Config,
    ledger: Ledger,
    *,
    no_hook: bool = False,
    show_now: bool = False,
    lifetime_ms: int = 0,
) -> int:
    """常驻循环：消息泵 + 点击事件 + 后台取数。"""
    from .click_hook import PetClickHook
    from .win32_window import BubbleWindow

    watcher = PetWatcher(
        config.global_state_path,
        override=config.mascot_rect,
        offset=config.mascot_offset,
    )
    watcher.poll(force=True)

    results: queue.Queue = queue.Queue()
    cache: dict[str, Any] = {"balance": None}
    engine = AlertEngine(config)

    def worker() -> None:
        try:
            snapshot = collect_snapshot(config, ledger)
            cache["balance"] = snapshot.balance
            _note_balance(engine, snapshot.balance)
            results.put(("snapshot", snapshot.payload))
        except Exception as exc:  # noqa: BLE001 - 后台异常不能拖垮 UI
            results.put(("error", str(exc)))

    def alert_worker() -> None:
        """后台自检：余额不足 / 短时大额消耗。"""
        try:
            refresh_ledger(config, ledger)
            balance = fetch_balance(config)
            cache["balance"] = balance
            _note_balance(engine, balance)
            alert = engine.evaluate(
                ledger=ledger,
                balance=_primary_balance(balance),
                currency=_primary_currency(balance, config),
            )
            if alert is not None:
                results.put(("alert", alert_payload(config, ledger, alert, balance)))
        except Exception:
            pass

    bubble = BubbleWindow(width=config.width, theme=config.theme)

    debug = bool(os.environ.get("FINANCE_PET_DEBUG"))

    def trigger(reason: str = "manual") -> None:
        rect = watcher.poll(force=True)
        bubble.set_anchor(rect, config.offset)
        bubble.show(
            loading_payload(config, ledger, cache["balance"]),
            auto_hide=config.auto_hide_seconds,
        )
        if debug:
            _log(f"[debug] trigger={reason} rect={rect} visible={bubble.visible} "
                 f"geom={bubble.anchor_rect()} err={bubble.last_error}")
        threading.Thread(target=worker, daemon=True).start()

    bubble.on_retry = lambda: trigger("retry")

    hook = None
    if not no_hook:
        hook = PetClickHook(
            lambda: watcher.poll(),
            padding=config.click_padding,
            hotkey=config.hotkey,
            require_pet_window=config.require_pet_window,
            pet_process_names=config.pet_process_names,
        )
        hook.start()

    def warmup() -> None:
        try:
            refresh_ledger(config, ledger)
            balance = fetch_balance(config)
            cache["balance"] = balance
            _note_balance(engine, balance)
        except Exception:
            pass

    threading.Thread(target=warmup, daemon=True).start()

    _log("赛博财务管家已启动")
    _log(f"  API Key  : {config.api_key.source}")
    _log(f"  桌宠坐标 : {watcher.rect}")
    _log(f"  数据目录 : {config.data_dir}")
    _log(f"  主题     : {config.theme}")
    _log(f"  快捷键   : {config.hotkey or '已关闭'}")
    if watcher.rect is not None:
        try:
            from .win32_windows import window_at_point

            hit = window_at_point(*watcher.rect.center)
            if hit is not None and config.require_pet_window:
                mark = "✓" if hit.process in config.pet_process_names else "✗（当前点不到，会被忽略）"
                _log(f"  桌宠位置 : {hit.process or '未知'} {mark}")
        except Exception:
            pass
    if hook is not None:
        detail = "已安装" if hook.installed else f"安装失败（{hook.last_error}）"
        _log(f"  鼠标钩子 : {detail}")
    _log("点击桌宠即可查看余额；Ctrl+C 退出。")

    deadline = time.monotonic() + lifetime_ms / 1000 if lifetime_ms else None
    if show_now:
        trigger("startup")

    # 主动提醒：启动 6 秒后先查一次，之后按 alerts_poll_seconds 周期自检
    alert_state = {"next": time.monotonic() + 6.0, "running": False}

    # 跟随拖动：以"按下那一刻的桌宠矩形 + 鼠标位移"推导新位置。
    # 期间如果 Codex 落盘了新坐标，就以它为准并重置基点，避免误差累积。
    drag_state: dict[str, Any] = {"rect": None, "point": None}

    def follow_bubble() -> None:
        """气泡可见期间让它实时跟着桌宠走（含拖动）。"""
        if not bubble.visible:
            drag_state["rect"] = None
            drag_state["point"] = None
            return

        if hook is not None and hook.dragging and config.follow_drag:
            if drag_state["rect"] is None:
                drag_state["rect"] = watcher.rect or watcher.poll()
                drag_state["point"] = hook.drag_point

            # Codex 若在拖动过程中更新了坐标，直接用新坐标做基准
            fresh = watcher.poll(fast=True)
            base = drag_state["rect"]
            if (
                fresh is not None
                and base is not None
                and fresh.as_tuple() != base.as_tuple()
            ):
                drag_state["rect"] = fresh
                drag_state["point"] = hook.drag_point
                base = fresh

            if base is None or drag_state["point"] is None or hook.drag_point is None:
                return
            dx = hook.drag_point[0] - drag_state["point"][0]
            dy = hook.drag_point[1] - drag_state["point"][1]
            bubble.set_anchor(
                MascotRect(base.x + dx, base.y + dy, base.width, base.height, source="drag"),
                config.offset,
            )
            bubble.reposition()
            return

        # 没在拖：松开后用 Codex 记录的位置把气泡对齐回去
        drag_state["rect"] = None
        drag_state["point"] = None
        rect = watcher.poll(fast=True)
        if rect is not None:
            bubble.set_anchor(rect, config.offset)
            bubble.reposition()

    def schedule_alerts() -> None:
        if not config.alerts_enabled or alert_state["running"]:
            return
        now_mono = time.monotonic()
        if now_mono < alert_state["next"]:
            return
        alert_state["next"] = now_mono + config.alerts_poll_seconds
        alert_state["running"] = True

        def _run() -> None:
            try:
                alert_worker()
            finally:
                alert_state["running"] = False

        threading.Thread(target=_run, daemon=True).start()

    try:
        while True:
            bubble.pump()
            schedule_alerts()
            follow_bubble()
            if hook is not None:
                for event in hook.poll():
                    if debug:
                        _log(f"[debug] hook event={event} point={hook.last_point}")
                    if event in {"click", "hotkey"}:
                        trigger(event)
            try:
                while True:
                    kind, payload = results.get_nowait()
                    if kind == "snapshot":
                        rect = watcher.poll()
                        if rect is not None:
                            bubble.set_anchor(rect, config.offset)
                        bubble.update(payload, auto_hide=config.auto_hide_seconds)
                    elif kind == "alert":
                        rect = watcher.poll(force=True)
                        if rect is not None:
                            bubble.set_anchor(rect, config.offset)
                        bubble.show(payload, auto_hide=config.alert_hide_seconds)
                        _log(f"[提醒] {payload.get('status_text')} - {payload.get('notice')}")
                    else:
                        bubble.update(
                            {
                                "state": "error",
                                "status_text": "异常",
                                "status_level": "err",
                                "balance_text": "—",
                                "today_text": "¥0.00",
                                "footer_text": f"内部错误：{payload}",
                                "show_retry": True,
                            },
                            auto_hide=config.auto_hide_seconds,
                        )
            except queue.Empty:
                pass
            if deadline and time.monotonic() >= deadline:
                break
            time.sleep(0.03)
    except KeyboardInterrupt:
        pass
    finally:
        if hook is not None:
            hook.stop()
        bubble.destroy()
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
