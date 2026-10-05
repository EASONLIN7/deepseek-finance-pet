"""业务编排：把余额 + 本地账本组合成气泡需要的一份数据快照。

这一层不依赖 Tkinter，因此可以被 CLI、单元测试或其它前端直接复用
（比如想把同一份数据接到托盘图标、菜单栏或 Web 面板上）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .balance import fetch_balance, format_money, humanize_age
from .codex_usage import scan_sessions
from .config import Config
from .ledger import Ledger, period_bounds


@dataclass
class Snapshot:
    payload: dict[str, Any]
    balance: dict[str, Any] = field(default_factory=dict)
    today: dict[str, Any] = field(default_factory=dict)
    week: dict[str, Any] = field(default_factory=dict)
    scan: dict[str, Any] = field(default_factory=dict)


def refresh_ledger(config: Config, ledger: Ledger) -> dict[str, Any]:
    """增量扫描 Codex rollout，把新用量写入账本。"""
    return scan_sessions(
        ledger=ledger,
        sessions_dir=config.sessions_dir,
        default_model=config.default_model,
        fx_usd_cny=config.fx_usd_cny,
        holidays=config.holidays,
        prices=config.prices,
    )


def collect_snapshot(
    config: Config,
    ledger: Ledger,
    *,
    fetch: bool = True,
    force_cache: bool = False,
    scan: bool = True,
) -> Snapshot:
    """采集一次完整快照（余额 + 今日 / 近 7 天消耗）。"""
    scan_result = refresh_ledger(config, ledger) if scan else {}
    if fetch:
        balance = fetch_balance(config, force_cache=force_cache)
    else:
        balance = {
            "ok": False, "stale": False, "source": "skipped", "error": None,
            "primary": {}, "balance_infos": [], "fetched_at": None, "latency_ms": None,
        }
    today = ledger.summary(*period_bounds("today"))
    week = ledger.summary(*period_bounds("7d"))
    return Snapshot(
        payload=_build_payload(config, balance, today, week, scan_result),
        balance=balance,
        today=today,
        week=week,
        scan=scan_result,
    )


def loading_payload(
    config: Config,
    ledger: Ledger,
    balance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """点击瞬间先渲染的"查询中"状态（用上一次的缓存垫底，避免闪烁）。"""
    today = ledger.summary(*period_bounds("today"))
    week = ledger.summary(*period_bounds("7d"))
    base = balance or {
        "ok": False, "stale": False, "source": "loading", "error": None,
        "primary": {}, "balance_infos": [], "fetched_at": None, "latency_ms": None,
    }
    payload = _build_payload(config, base, today, week, {})
    payload.update(
        state="loading",
        status_text="查询中",
        status_level="warn",
        footer_text="正在拉取最新余额…",
        show_retry=False,
    )
    return payload


def alert_payload(
    config: Config,
    ledger: Ledger,
    alert: Any,
    balance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一条 Alert 渲染成气泡数据（带高亮提醒行）。"""
    today = ledger.summary(*period_bounds("today"))
    week = ledger.summary(*period_bounds("7d"))

    base = _build_payload(
        config,
        balance or {
            "ok": False, "stale": False, "source": "alert", "error": None,
            "primary": {}, "balance_infos": [], "fetched_at": None, "latency_ms": None,
        },
        today,
        week,
        {},
    )
    base.update(
        state="alert",
        status_text=alert.status_text,
        status_level=alert.level,
        notice=alert.notice,
        footer_text=alert.footer,
        show_retry=False,
        alert_kind=alert.kind,
    )
    if alert.kind == "burst":
        now = datetime.now(timezone.utc)
        window_cost = ledger.summary(
            now - timedelta(minutes=config.burst_window_minutes), now
        ).get("cost", alert.amount)
        base["period_text"] = (
            f"近 {config.burst_window_minutes:g} 分钟 {format_money(window_cost, config.currency)}"
        )
    return base


def _build_payload(
    config: Config,
    balance: dict[str, Any],
    today: dict[str, Any],
    week: dict[str, Any],
    scan: dict[str, Any],
) -> dict[str, Any]:
    primary: dict[str, Any] = balance.get("primary") or {}
    currency = str(primary.get("currency") or config.currency)
    total = primary.get("total_balance")

    has_balance = bool(primary)
    balance_text = format_money(total, currency) if has_balance else "—"
    today_text = format_money(today.get("cost", 0.0), config.currency)
    period_text = f"近 7 天 {format_money(week.get('cost', 0.0), config.currency)}"

    source = balance.get("source")
    error = balance.get("error")
    now = datetime.now().strftime("%H:%M:%S")

    if source == "live" and balance.get("ok"):
        state, level = "ok", "ok"
        status_text = "已连接"
        latency = balance.get("latency_ms")
        suffix = f" · {latency:.0f}ms" if isinstance(latency, (int, float)) else ""
        footer = f"{now} 更新{suffix}"
        show_retry = False
    elif balance.get("stale"):
        state, level = "error", "warn"
        status_text = "缓存"
        footer = f"{error or '网络异常'}（{humanize_age(balance.get('fetched_at'))}的数据）"
        show_retry = True
    elif source == "skipped":
        state, level = "ok", "ok"
        status_text = "离线"
        footer = "未查询余额"
        show_retry = False
    else:
        state, level = "error", "err"
        status_text = "离线"
        footer = str(error or "未能获取余额")
        show_retry = True

    if has_balance and balance.get("is_available") is False:
        level = "warn"
        footer = f"{footer} · 余额可能已不足"

    currency_note = ""
    infos = balance.get("balance_infos") or []
    if len(infos) > 1:
        others = ", ".join(str(i.get("currency")) for i in infos if i is not primary)
        currency_note = f"+{others}"

    return {
        "state": state,
        "status_text": status_text,
        "status_level": level,
        "balance_text": balance_text,
        "today_text": today_text,
        "period_text": period_text,
        "footer_text": footer,
        "show_retry": show_retry,
        "currency_note": currency_note,
        "currency": currency,
        "is_available": balance.get("is_available"),
        "today_cost": today.get("cost", 0.0),
        "week_cost": week.get("cost", 0.0),
        "total_tokens_today": today.get("total_tokens", 0),
        "turns_today": today.get("turns", 0),
        "scan": scan,
        "fetched_at": balance.get("fetched_at"),
    }
