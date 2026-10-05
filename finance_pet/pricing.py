"""DeepSeek 计费核心：价格表、峰谷时段判定、token -> 金额换算。

价格单位统一为 **USD / 1M tokens**，取自 DeepSeek 官方 "Models & Pricing"
页面的 peak（标准时段）价格：

    deepseek-flash    cache-hit $0.006   cache-miss $0.30   output $1.20
    deepseek-v4-pro   cache-hit $0.044   cache-miss $1.32   output $3.96

官方扣费规则：off-peak（错峰）价格恰为 peak 价的一半。
峰时 = UTC 周一至周五 01:00-04:00 与 06:00-10:00（不含中国法定节假日）。

价格会随官方调整，生产环境用 config.json 的 ``pricing`` 覆盖即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

MILLION = 1_000_000

#: 峰时时段（UTC 小时，左闭右开）
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

#: 默认价格表（USD / 1M tokens，peak 价）
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "deepseek-flash": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    "deepseek-v4-pro": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
}

#: 别名 -> 规范模型名
MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-flash-vision-exp": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
}

FALLBACK_MODEL = "deepseek-flash"


@dataclass(frozen=True)
class PriceSnapshot:
    """某一时刻生效的三档单价（USD / 1M tokens）。"""

    model: str
    tier: str  # "peak" | "off-peak"
    cache_hit: float
    cache_miss: float
    output: float


def normalize_model(
    model: str | None,
    prices: Mapping[str, Mapping[str, float]] | None = None,
) -> tuple[str, bool]:
    """归一化模型名，返回 ``(canonical_model, assumed)``。"""
    table = prices or DEFAULT_PRICES
    raw = (model or "").strip().lower()
    if raw in table:
        return raw, False
    alias = MODEL_ALIASES.get(raw)
    if alias and alias in table:
        return alias, False
    return FALLBACK_MODEL, True


def _to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_peak(moment: datetime, holidays: Iterable[str] = ()) -> bool:
    """判断该时刻是否处于 peak（标准）计费时段。"""
    utc = _to_utc(moment)
    if utc.weekday() >= 5:
        return False
    if utc.strftime("%Y-%m-%d") in set(holidays):
        return False
    return any(start <= utc.hour < end for start, end in PEAK_WINDOWS_UTC)


def price_at(
    model: str | None,
    moment: datetime | None = None,
    *,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    holidays: Iterable[str] = (),
) -> PriceSnapshot:
    """取某模型在某个时刻生效的单价。"""
    table = prices or DEFAULT_PRICES
    canonical, _ = normalize_model(model, table)
    spec = table[canonical]
    at = moment or datetime.now(timezone.utc)
    half = not is_peak(at, holidays)
    factor = 0.5 if half else 1.0
    return PriceSnapshot(
        model=canonical,
        tier="off-peak" if half else "peak",
        cache_hit=float(spec["cache_hit"]) * factor,
        cache_miss=float(spec["cache_miss"]) * factor,
        output=float(spec["output"]) * factor,
    )


def compute_cost(
    model: str | None,
    *,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
    completion_tokens: int = 0,
    at: datetime | None = None,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    holidays: Iterable[str] = (),
    fx_usd_cny: float = 7.1,
) -> dict[str, Any]:
    """把一次调用的 token 用量换算成 USD / CNY 金额。

    DeepSeek 的 ``usage`` 里：``cached_input_tokens`` 走 cache-hit 价，
    ``input_tokens - cached_input_tokens`` 走 cache-miss 价。
    """
    canonical, assumed = normalize_model(model, prices)
    snapshot = price_at(canonical, at, prices=prices, holidays=holidays)

    hit = max(int(cache_hit_tokens or 0), 0)
    miss = max(int(cache_miss_tokens or 0), 0)
    out = max(int(completion_tokens or 0), 0)

    cost_hit = hit / MILLION * snapshot.cache_hit
    cost_miss = miss / MILLION * snapshot.cache_miss
    cost_out = out / MILLION * snapshot.output
    total_usd = cost_hit + cost_miss + cost_out

    return {
        "model": model,
        "canonical_model": canonical,
        "assumed_model": assumed,
        "tier": snapshot.tier,
        "amount_usd": round(total_usd, 8),
        "amount": round(total_usd * float(fx_usd_cny), 8),
        "breakdown_usd": {
            "cache_hit": round(cost_hit, 8),
            "cache_miss": round(cost_miss, 8),
            "output": round(cost_out, 8),
        },
        "tokens": {"cache_hit": hit, "cache_miss": miss, "output": out},
    }


def usage_to_tokens(usage: Mapping[str, Any]) -> dict[str, int]:
    """把 Codex rollout 里的 usage 字段拆成 DeepSeek 计费口径。"""
    input_tokens = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    cached = max(0, min(cached, input_tokens))
    return {
        "cache_hit_tokens": cached,
        "cache_miss_tokens": max(0, input_tokens - cached),
        "completion_tokens": output,
    }


def cost_for_usage(
    model: str | None,
    usage: Mapping[str, Any],
    *,
    at: datetime | None = None,
    fx_usd_cny: float = 7.1,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    holidays: Iterable[str] = (),
) -> dict[str, Any]:
    """便捷封装：直接传入 Codex 的 usage 字典。"""
    return compute_cost(
        model,
        at=at,
        fx_usd_cny=fx_usd_cny,
        prices=prices,
        holidays=holidays,
        **usage_to_tokens(usage),
    )
