"""赛博财务管家的单元测试（只用标准库 unittest，无需额外依赖）。

运行::

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finance_pet import pricing  # noqa: E402
from finance_pet.alerts import AlertEngine, BalanceHistory  # noqa: E402
from finance_pet.balance import fetch_balance, format_money, humanize_age  # noqa: E402
from finance_pet.codex_usage import scan_sessions  # noqa: E402
from finance_pet.click_hook import PetClickHook, parse_hotkey  # noqa: E402
from finance_pet.config import load_config  # noqa: E402
from finance_pet.ledger import Ledger, period_bounds  # noqa: E402
from finance_pet.pet_locator import MascotRect, locate  # noqa: E402


class PricingTests(unittest.TestCase):
    def test_peak_windows(self) -> None:
        # 2026-10-06 是周二；UTC 02:00 属于峰时，05:00 与 11:00 属于错峰
        peak = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)
        off1 = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
        off2 = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)
        self.assertTrue(pricing.is_peak(peak))
        self.assertFalse(pricing.is_peak(off1))
        self.assertFalse(pricing.is_peak(off2))

    def test_weekend_and_holiday_are_off_peak(self) -> None:
        saturday = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)
        self.assertFalse(pricing.is_peak(saturday))
        self.assertFalse(pricing.is_peak(datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc),
                                         holidays=("2026-10-06",)))

    def test_off_peak_is_half_price(self) -> None:
        peak = pricing.price_at("deepseek-flash", datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc))
        off = pricing.price_at("deepseek-flash", datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(peak.tier, "peak")
        self.assertEqual(off.tier, "off-peak")
        self.assertAlmostEqual(off.cache_miss * 2, peak.cache_miss)

    def test_cost_math(self) -> None:
        at = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)  # peak
        cost = pricing.compute_cost(
            "deepseek-flash",
            cache_hit_tokens=1_000_000,
            cache_miss_tokens=1_000_000,
            completion_tokens=1_000_000,
            at=at,
            fx_usd_cny=7.0,
        )
        # 0.006 + 0.30 + 1.20 = 1.506 USD -> 10.542 CNY
        self.assertAlmostEqual(cost["amount_usd"], 1.506, places=6)
        self.assertAlmostEqual(cost["amount"], 1.506 * 7.0, places=6)
        self.assertEqual(cost["tier"], "peak")

    def test_alias_and_fallback(self) -> None:
        canonical, assumed = pricing.normalize_model("deepseek-v4-flash")
        self.assertEqual(canonical, "deepseek-flash")
        self.assertFalse(assumed)
        _, assumed = pricing.normalize_model("some-unknown-model")
        self.assertTrue(assumed)

    def test_usage_to_tokens_splits_cache(self) -> None:
        tokens = pricing.usage_to_tokens(
            {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}
        )
        self.assertEqual(tokens["cache_hit_tokens"], 400)
        self.assertEqual(tokens["cache_miss_tokens"], 600)
        self.assertEqual(tokens["completion_tokens"], 50)

    def test_cached_tokens_clamped(self) -> None:
        tokens = pricing.usage_to_tokens({"input_tokens": 10, "cached_input_tokens": 99})
        self.assertEqual(tokens["cache_hit_tokens"], 10)
        self.assertEqual(tokens["cache_miss_tokens"], 0)


class LedgerTests(unittest.TestCase):
    def test_record_summary_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "finance.db")
            from finance_pet.ledger import UsageEvent

            event = UsageEvent(
                ts=datetime.now(timezone.utc),
                model="deepseek-flash",
                session_id="s1",
                turn_id="t1",
                response_id="r1",
                input_tokens=1000,
                cached_input_tokens=200,
                output_tokens=100,
                total_tokens=1100,
                cost_amount=1.5,
                cost_usd=0.2,
            )
            self.assertTrue(ledger.record(event))
            self.assertFalse(ledger.record(event))  # 幂等
            today = ledger.summary(*period_bounds("today"))
            self.assertEqual(today["turns"], 1)
            self.assertAlmostEqual(today["cost"], 1.5)
            self.assertEqual(today["total_tokens"], 1100)
            ledger.close()

    def test_scan_only_new_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions" / "2026" / "10" / "06"
            sessions.mkdir(parents=True)
            rollout = sessions / "rollout-2026-10-06T00-00-00-abc.jsonl"
            # 用"当前时间"附近的时间戳，保证落在 period_bounds("all") 区间内
            now = datetime.now(timezone.utc)
            ts_meta = (now - __import__("datetime").timedelta(minutes=5)).isoformat()
            ts_usage = (now - __import__("datetime").timedelta(minutes=4)).isoformat()
            meta = {"timestamp": ts_meta, "type": "session_meta",
                    "payload": {"model": "deepseek-flash", "model_provider": "deepseek"}}
            usage = {"timestamp": ts_usage, "type": "token_usage_record",
                     "payload": {"thread_id": "th", "turn_id": "tu", "session_id": "se",
                                 "response_id": "re",
                                 "usage": {"input_tokens": 100, "cached_input_tokens": 0,
                                           "output_tokens": 10, "total_tokens": 110}}}
            rollout.write_text(json.dumps(meta) + "\n" + json.dumps(usage) + "\n", encoding="utf-8")

            ledger = Ledger(root / "f.db")
            try:
                first = scan_sessions(ledger=ledger, sessions_dir=root / "sessions",
                                      default_model="deepseek-flash")
                self.assertEqual(first["new_events"], 1)
                second = scan_sessions(ledger=ledger, sessions_dir=root / "sessions",
                                       default_model="deepseek-flash")
                self.assertEqual(second["new_events"], 0)

                # 追加一行后应只增量读到新的一行
                with rollout.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(dict(usage, payload=dict(usage["payload"],
                                                                  response_id="re2"))) + "\n")
                third = scan_sessions(ledger=ledger, sessions_dir=root / "sessions",
                                      default_model="deepseek-flash")
                self.assertEqual(third["new_events"], 1)
                self.assertEqual(ledger.summary(*period_bounds("all"))["turns"], 2)
                # 成本按 deepseek-flash 错峰价估算，应当大于 0
                self.assertGreater(ledger.summary(*period_bounds("all"))["cost"], 0)
            finally:
                ledger.close()


class BalanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(Path(self.tmp.name) / "missing.json")
        self.config.data_dir = Path(self.tmp.name)
        self.config.api_key.value = "sk-test"
        self.config.api_key.source = "test"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake_response(self, payload: dict) -> mock.MagicMock:
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: False
        return response

    def test_live_then_cache_fallback(self) -> None:
        payload = {"is_available": True,
                   "balance_infos": [{"currency": "CNY", "total_balance": "12.80",
                                      "granted_balance": "0.00", "topped_up_balance": "12.80"}]}
        with mock.patch("urllib.request.urlopen", return_value=self._fake_response(payload)):
            live = fetch_balance(self.config)
        self.assertTrue(live["ok"])
        self.assertEqual(live["primary"]["total_balance"], "12.80")

        # 网络失败 -> 回落到缓存
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            cached = fetch_balance(self.config)
        self.assertFalse(cached["ok"])
        self.assertTrue(cached["stale"])
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(cached["primary"]["total_balance"], "12.80")
        self.assertIn("无法连接", cached["error"])

    def test_no_cache_reports_error(self) -> None:
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            result = fetch_balance(self.config)
        self.assertFalse(result["ok"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["source"], "none")

    def test_format_money_and_age(self) -> None:
        self.assertEqual(format_money("12.8", "CNY"), "¥12.80")
        self.assertEqual(format_money(3, "USD"), "$3.00")
        self.assertEqual(humanize_age(None), "无缓存")
        self.assertEqual(humanize_age(datetime.now(timezone.utc).isoformat()), "刚刚")


class PetLocatorTests(unittest.TestCase):
    def test_reads_anchor_from_global_state(self) -> None:
        state = {
            "electron-avatar-overlay-open": True,
            "electron-avatar-overlay-bounds": {
                "x": 2104, "y": 873, "displayId": 1060407849,
                "displayBounds": {"x": 0, "y": 0, "width": 1707, "height": 1067},
                "byDisplayId": {"1060407849": {
                    "x": 1292, "y": 434, "width": 366, "height": 330,
                    "anchor": {"x": 1454, "y": 566, "width": 166, "height": 180},
                }},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            rect = locate(path, use_windows=False)
        self.assertIsNotNone(rect)
        self.assertEqual(rect.as_tuple(), (1454, 566, 166, 180))

    def test_mascot_offset_within_window(self) -> None:
        state = {
            "electron-avatar-overlay-open": True,
            "electron-avatar-overlay-bounds": {
                "x": 2104, "y": 873, "displayId": 1,
                "byDisplayId": {"1": {
                    "x": 2104, "y": 873, "width": 356, "height": 320,
                    "mascot": {"left": 162, "top": 132, "width": 166, "height": 180},
                }},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            rect = locate(path, use_windows=False)
        self.assertEqual(rect.as_tuple(), (2266, 1005, 166, 180))

    def test_override_wins(self) -> None:
        rect = locate(Path("does-not-exist.json"), override=(1, 2, 3, 4), use_windows=False)
        self.assertEqual(rect.as_tuple(), (1, 2, 3, 4))
        self.assertTrue(rect.contains(2, 3))
        self.assertFalse(rect.contains(50, 50))


class ConfigTests(unittest.TestCase):
    def test_key_from_codex_config_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text(
                '[model_providers.deepseek]\n'
                'base_url = "https://api.deepseek.com/"\n'
                'experimental_bearer_token = "sk-from-toml"\n',
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}, clear=False):
                os_environ = dict(**__import__("os").environ)
                try:
                    __import__("os").environ.pop("DEEPSEEK_API_KEY", None)
                    cfg = load_config(home / "nope.json")
                finally:
                    __import__("os").environ.update(os_environ)
            self.assertTrue(cfg.api_key.ok)
            self.assertEqual(cfg.api_key.value, "sk-from-toml")
            self.assertIn("config.toml", cfg.api_key.source)

    def test_sensitive_key_never_in_status_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.json"
            cfg_file.write_text(json.dumps({"api_key": "sk-secret", "theme": "light"}), encoding="utf-8")
            cfg = load_config(cfg_file)
            self.assertEqual(cfg.api_key.value, "sk-secret")
            self.assertEqual(cfg.theme, "light")
            self.assertNotIn("sk-secret", cfg.api_key.source)


class AlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = load_config(Path(self.tmp.name) / "missing.json")
        self.config.data_dir = Path(self.tmp.name)
        self.ledger = Ledger(Path(self.tmp.name) / "a.db")

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def _add_spend(self, amount: float, *, minutes_ago: float = 1.0, tag: str = "") -> None:
        from datetime import timedelta

        from finance_pet.ledger import UsageEvent

        key = f"{amount}-{minutes_ago}-{tag}"
        self.ledger.record(UsageEvent(
            ts=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            model="deepseek-flash",
            session_id=f"s{key}",
            turn_id=f"t{key}",
            response_id=f"r{key}",
            cost_amount=amount,
            cost_usd=amount / 7.0,
            total_tokens=1000,
        ))

    def test_no_alert_when_everything_is_fine(self) -> None:
        engine = AlertEngine(self.config)
        self.assertIsNone(engine.evaluate(ledger=self.ledger, balance=100.0))

    def test_low_balance_alert_fires_once_then_cools_down(self) -> None:
        engine = AlertEngine(self.config)
        alert = engine.evaluate(ledger=self.ledger, balance=4.5, now=1000.0)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.kind, "low_balance")
        self.assertEqual(alert.level, "err")
        self.assertIn("4.50", alert.notice)

        # 冷却期内不重复提醒
        self.assertIsNone(engine.evaluate(ledger=self.ledger, balance=4.5, now=1001.0))
        # 超过冷却时间后再次提醒
        self.assertIsNotNone(
            engine.evaluate(ledger=self.ledger, balance=4.0, now=1000.0 + 61 * 60)
        )

    def test_low_balance_boundary(self) -> None:
        engine = AlertEngine(self.config)
        # 阈值 5.0：等于 5.0 不算"低于"
        self.assertIsNone(engine.evaluate(ledger=self.ledger, balance=5.0, now=1.0))
        self.assertIsNotNone(engine.evaluate(ledger=self.ledger, balance=4.99, now=1.0))

    def test_burst_alert_from_ledger(self) -> None:
        self._add_spend(1.2, minutes_ago=1, tag="a")
        self._add_spend(1.2, minutes_ago=5, tag="b")
        engine = AlertEngine(self.config)
        alert = engine.evaluate(ledger=self.ledger, balance=100.0, now=500.0)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.kind, "burst")
        self.assertIn("30 分钟", alert.notice)

    def test_burst_ignores_old_spend(self) -> None:
        self._add_spend(9.0, minutes_ago=120)   # 两小时前，超出 30 分钟窗口
        engine = AlertEngine(self.config)
        self.assertIsNone(engine.evaluate(ledger=self.ledger, balance=100.0, now=500.0))

    def test_burst_uses_balance_drop_when_larger(self) -> None:
        """本地账本没记录、但余额掉了（比如在网页版消费）也要能提醒。"""
        engine = AlertEngine(self.config)
        engine.history.add(100.0, at=0.0)
        engine.history.add(90.0, at=31 * 60)
        alert = engine.evaluate(
            ledger=self.ledger, balance=90.0, now=31 * 60, 
        )
        self.assertIsNotNone(alert)
        self.assertEqual(alert.kind, "burst")
        self.assertIn("含非本机消费", alert.notice)

    def test_alerts_can_be_disabled(self) -> None:
        self.config.alerts_enabled = False
        engine = AlertEngine(self.config)
        self.assertIsNone(engine.evaluate(ledger=self.ledger, balance=0.1, now=1.0))

    def test_balance_history_window(self) -> None:
        history = BalanceHistory()
        history.add(50.0, at=0.0)
        history.add(40.0, at=100.0)
        # 窗口 60 秒，起点之前最近的样本是 t=0 的 50.0
        self.assertAlmostEqual(history.drop_over(60, at=100.0), 10.0)
        # 窗口 10 秒：t=0 的样本不够老，无法覆盖窗口
        self.assertIsNone(history.drop_over(10, at=100.0))


class HideDurationTests(unittest.TestCase):
    def test_click_bubble_defaults_to_three_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp) / "none.json")
        self.assertEqual(cfg.auto_hide_seconds, 3.0)
        self.assertEqual(cfg.alert_hide_seconds, 8.0)

    def test_explicit_zero_means_never_hide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"auto_hide_seconds": 0}), encoding="utf-8")
            cfg = load_config(path)
        self.assertEqual(cfg.auto_hide_seconds, 0.0)


class DragFollowTests(unittest.TestCase):
    """跟随拖动：位移由鼠标决定，不依赖 Codex 落盘时机。"""

    def test_drag_delta_follows_mouse(self) -> None:
        hook = PetClickHook(lambda: None)
        hook.dragging = True
        hook.drag_start = (100, 100)
        hook.drag_point = (160, 130)
        self.assertEqual(hook.drag_delta(), (60, 30))

    def test_drag_delta_is_zero_without_drag(self) -> None:
        hook = PetClickHook(lambda: None)
        hook.drag_start = (10, 10)
        hook.drag_point = (90, 90)
        self.assertEqual(hook.drag_delta(), (0, 0))

    def test_dragged_rect_keeps_size(self) -> None:
        base = MascotRect(1000, 500, 166, 180)
        dx, dy = 40, -25
        moved = MascotRect(base.x + dx, base.y + dy, base.width, base.height, source="drag")
        self.assertEqual(moved.as_tuple(), (1040, 475, 166, 180))
        self.assertEqual(moved.width, base.width)
        self.assertTrue(moved.contains(1100, 550))

    def test_hotkey_parsing(self) -> None:
        self.assertEqual(parse_hotkey("ctrl+alt+b"), (0x4000 | 0x0002 | 0x0001, ord("B")))
        self.assertIsNone(parse_hotkey(""))
        self.assertIsNone(parse_hotkey("b"))       # 没有修饰键
        self.assertIsNone(parse_hotkey("ctrl+alt"))


if __name__ == "__main__":
    unittest.main()
