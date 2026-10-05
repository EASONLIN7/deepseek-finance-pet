"""主动提醒：余额不足、短时间大额消耗。

和"点击才弹"不同，这里是后台按 ``poll_seconds`` 周期自检，命中条件就主动
弹气泡。两条规则：

* **余额不足** —— 当前余额低于 ``low_balance_threshold``；
* **短时大额消耗** —— 最近 ``burst_window_minutes`` 分钟内的消耗超过
  ``burst_threshold``。

消耗金额取两个来源的较大值：

1. 本地账本（Codex 在本机产生的用量，精确到每次调用）；
2. 余额采样落差（覆盖你在网页版或其它工具上的消费）。

两条规则各自有冷却时间，避免每隔几十秒就弹一次。状态保存在内存里，
重启后重新计冷却。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .balance import format_money
from .config import Config
from .ledger import Ledger


@dataclass
class Alert:
    """一条待展示的提醒。"""

    kind: str          # "low_balance" | "burst"
    level: str         # "warn" | "err"
    status_text: str   # 气泡右上角
    notice: str        # 气泡内的高亮提示行
    footer: str        # 气泡页脚
    amount: float = 0.0


class BalanceHistory:
    """内存里的余额采样环形缓冲，用来估算"余额掉了多少"。"""

    def __init__(self, max_age_seconds: float = 6 * 3600) -> None:
        self.max_age = max_age_seconds
        self._samples: list[tuple[float, float]] = []

    def add(self, balance: float, *, at: float | None = None) -> None:
        now = at if at is not None else time.monotonic()
        self._samples.append((now, float(balance)))
        cutoff = now - self.max_age
        self._samples = [s for s in self._samples if s[0] >= cutoff]

    def drop_over(self, window_seconds: float, *, at: float | None = None) -> float | None:
        """返回窗口内的余额落差；样本不足以覆盖窗口时返回 None。

        基准取"窗口起点之前最近的一次采样"，但要求这个基准不能太旧
        （最多再往前一个窗口），否则会把更早时段的消费误算进当前窗口。
        """
        if not self._samples:
            return None
        now = at if at is not None else time.monotonic()
        window_start = now - window_seconds
        older = [s for s in self._samples if s[0] <= window_start + 1e-6]
        if not older:
            return None
        baseline = older[-1][1]
        baseline_at = older[-1][0]
        if window_start - baseline_at > window_seconds:
            return None  # 基准太旧，无法判定窗口内的落差
        latest = self._samples[-1][1]
        return max(0.0, baseline - latest)

    def reset(self) -> None:
        self._samples.clear()


class AlertEngine:
    """判断当前是否应该主动弹提醒。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.history = BalanceHistory()
        self._last_fired: dict[str, float] = {}

    # ------------------------------------------------------------------ 冷却
    def _cooled_down(self, kind: str, cooldown_seconds: float, now: float) -> bool:
        last = self._last_fired.get(kind)
        return last is None or (now - last) >= cooldown_seconds

    def _fire(self, kind: str, now: float) -> None:
        self._last_fired[kind] = now

    def reset_cooldowns(self) -> None:
        self._last_fired.clear()

    # ------------------------------------------------------------------ 评估
    def observe_balance(self, balance: float | None) -> None:
        """每个采样周期把余额喂给历史缓冲。"""
        if balance is not None:
            self.history.add(balance)

    def spend_in_window(
        self,
        ledger: Ledger,
        window_minutes: float,
        *,
        now: float | None = None,
    ) -> tuple[float, str]:
        """窗口内消耗金额（本地账本 vs 余额落差，取较大者）。"""
        moment = datetime.now(timezone.utc)
        local = float(
            ledger.summary(moment - timedelta(minutes=window_minutes), moment).get("cost") or 0.0
        )
        drop = self.history.drop_over(window_minutes * 60, at=now)
        if drop is None:
            return local, "ledger"
        if drop > local:
            return drop, "balance"
        return local, "ledger"

    def evaluate(
        self,
        *,
        ledger: Ledger,
        balance: float | None,
        currency: str = "CNY",
        now: float | None = None,
    ) -> Alert | None:
        """返回本次需要弹的提醒；没有就返回 None。"""
        cfg = self.config
        if not cfg.alerts_enabled:
            return None
        stamp = now if now is not None else time.monotonic()

        # 1) 余额不足（优先级最高）
        if (
            balance is not None
            and balance < cfg.low_balance_threshold
            and self._cooled_down("low_balance", cfg.low_balance_cooldown_minutes * 60, stamp)
        ):
            self._fire("low_balance", stamp)
            return Alert(
                kind="low_balance",
                level="err",
                status_text="余额告急",
                notice=f"余额仅剩 {format_money(balance, currency)}，建议尽快充值",
                footer=f"低于 {format_money(cfg.low_balance_threshold, currency)} 阈值 · 自动提醒",
                amount=balance,
            )

        # 2) 短时间大额消耗
        if cfg.burst_threshold > 0:
            spend, source = self.spend_in_window(
                ledger, cfg.burst_window_minutes, now=stamp
            )
            if (
                spend >= cfg.burst_threshold
                and self._cooled_down("burst", cfg.burst_cooldown_minutes * 60, stamp)
            ):
                self._fire("burst", stamp)
                window = f"{cfg.burst_window_minutes:g} 分钟"
                hint = "（含非本机消费）" if source == "balance" else ""
                return Alert(
                    kind="burst",
                    level="warn",
                    status_text="消耗提醒",
                    notice=f"最近 {window} 已消耗 {format_money(spend, currency)}{hint}",
                    footer=f"超过 {format_money(cfg.burst_threshold, currency)} 阈值 · 自动提醒",
                    amount=spend,
                )
        return None
