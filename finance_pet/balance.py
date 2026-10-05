"""DeepSeek 余额查询：实时请求 + 本地缓存 + 友好降级。

官方接口::

    GET https://api.deepseek.com/user/balance
    Authorization: Bearer <API_KEY>

响应::

    {"is_available": true,
     "balance_infos": [{"currency": "CNY", "total_balance": "110.00",
                        "granted_balance": "10.00",
                        "topped_up_balance": "100.00"}]}

任何失败都不会抛到 UI：要么返回上一次成功的缓存（``stale=True``），
要么返回带友好文案的错误（``ok=False``），气泡据此显示重试按钮。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config

SYMBOLS = {"CNY": "¥", "USD": "$", "JPY": "¥", "EUR": "€"}


def symbol_for(currency: str | None) -> str:
    return SYMBOLS.get((currency or "CNY").upper(), "")


def format_money(amount: float | str | None, currency: str | None = "CNY") -> str:
    """把 12.8 渲染成 ``¥12.80``（货币符号 + 两位小数）。"""
    try:
        value = float(amount)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 0.0
    return f"{symbol_for(currency)}{value:,.2f}"


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # 缓存失败不影响主流程


def _pick_primary(balance_infos: list[dict[str, Any]], prefer: str = "CNY") -> dict[str, Any]:
    if not balance_infos:
        return {}
    for info in balance_infos:
        if str(info.get("currency", "")).upper() == prefer.upper():
            return info
    return balance_infos[0]


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "API Key 无效或没有余额查询权限"
        if exc.code == 429:
            return "请求过于频繁，请稍后重试"
        if exc.code >= 500:
            return f"DeepSeek 服务端异常（{exc.code}）"
        return f"请求被拒绝（HTTP {exc.code}）"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            return "网络超时，请检查网络后重试"
        return "无法连接 DeepSeek 服务器"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "网络超时，请检查网络后重试"
    return f"查询失败：{type(exc).__name__}"


def fetch_balance(config: Config, *, force_cache: bool = False) -> dict[str, Any]:
    """查询余额。永远返回可渲染的结果字典，不抛异常。

    返回字段: ok / stale / source / is_available / balance_infos /
    primary / fetched_at / error / latency_ms
    """
    cache = _read_cache(config.cache_path)

    if not config.api_key.ok:
        return _from_cache(cache, "未找到 DeepSeek API Key，请在 config.json 中配置")

    if force_cache:
        return _from_cache(cache, "已切换到缓存数据")

    url = f"{config.api_base}/user/balance"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config.api_key.value}",
            "Accept": "application/json",
            "User-Agent": "codex-finance-pet/1.0",
        },
        method="GET",
    )

    started = datetime.now(timezone.utc)
    last_error: Exception | None = None
    for _ in range(max(1, config.retries + 1)):
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            latency = (datetime.now(timezone.utc) - started).total_seconds() * 1000
            infos = list(payload.get("balance_infos") or [])
            primary = _pick_primary(infos, config.currency)
            record = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "is_available": bool(payload.get("is_available")),
                "balance_infos": infos,
                "primary": primary,
            }
            _write_cache(config.cache_path, record)
            return {
                "ok": True,
                "stale": False,
                "source": "live",
                "error": None,
                "latency_ms": round(latency, 1),
                **record,
            }
        except Exception as exc:  # noqa: BLE001 - 统一转成友好文案
            last_error = exc

    message = _friendly_error(last_error) if last_error else "查询失败"
    return _from_cache(cache, message)


def _from_cache(cache: dict[str, Any] | None, message: str) -> dict[str, Any]:
    if cache and cache.get("balance_infos"):
        return {
            "ok": False,
            "stale": True,
            "source": "cache",
            "error": message,
            "latency_ms": None,
            "fetched_at": cache.get("fetched_at"),
            "is_available": cache.get("is_available"),
            "balance_infos": cache.get("balance_infos") or [],
            "primary": cache.get("primary") or {},
        }
    return {
        "ok": False,
        "stale": False,
        "source": "none",
        "error": message,
        "latency_ms": None,
        "fetched_at": None,
        "is_available": None,
        "balance_infos": [],
        "primary": {},
    }


def humanize_age(fetched_at: str | None) -> str:
    """把缓存时间渲染成 "3 分钟前"。"""
    if not fetched_at:
        return "无缓存"
    try:
        moment = datetime.fromisoformat(fetched_at)
    except ValueError:
        return "无缓存"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 5:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"
