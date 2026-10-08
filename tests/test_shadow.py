import json
import math
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from crypto_watcher import shadow as sh
from crypto_watcher.binance import BinanceError, MarketClient
from crypto_watcher.bot import Bot
from crypto_watcher.config import Settings
from crypto_watcher.data import DemoProvider
from crypto_watcher.shadow import DAY_MS, HOUR_MS, ShadowRecorder
from crypto_watcher.state import StateStore
from helpers import Clock, FakeExchange
from test_executor import Notifier

T = int(pd.Timestamp("2026-10-08 12:00", tz="UTC").timestamp() * 1000)       # the hour being evaluated
NOW = T / 1000 + 180                                                          # three minutes after the hour mark


class FakeMarket:
    """Open-interest history and funding as the Binance public endpoints return them."""
    def __init__(self, collapse=(), fail=(), clock=None):
        self.collapse, self.fail, self.calls, self.funding_rate = set(collapse), set(fail), [], 0.0001

    def open_interest_hist(self, symbol, period, limit):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise BinanceError("boom", -1000, 500)
        rng = np.random.default_rng(abs(hash(symbol)) % 1000)
        values = 1e9 * (1 + 0.01 * rng.standard_normal(limit))
        values[-25] = 1e9
        values[-1] = 0.85e9 if symbol in self.collapse else 1e9
        return [{"symbol": symbol, "sumOpenInterest": "1", "sumOpenInterestValue": f"{v:.4f}",
                 "timestamp": T - (limit - 1 - i) * HOUR_MS} for i, v in enumerate(values)]

    def funding_rates(self, symbol, start_ms, end_ms):
        times = [t for t in range(start_ms - DAY_MS, end_ms + DAY_MS, HOUR_MS) if (t // HOUR_MS) % 8 == 0]
        return [{"symbol": symbol, "fundingTime": t + 1, "fundingRate": f"{self.funding_rate:.8f}"} for t in times
                if start_ms - HOUR_MS <= t <= end_ms + HOUR_MS]


class FakeCandles:
    """Closed 1h candles: price falls into T (110 -> 100 over the previous 24h), jumps 2% in the next hour, then rises 0.5% per hour."""
    def __init__(self, clock, rising_into_T=False):
        self.clock, self.rising = clock, rising_into_T

    def price(self, ms):
        h = (ms - T) / HOUR_MS
        if h <= -24:
            return 90.0 if self.rising else 110.0
        if h <= 0:
            return 100 - 10 * (-h) / 24 if self.rising else 100 + 10 * (-h) / 24
        return 102 * 1.005 ** (h - 1)          # a +2% jump in the first hour after detection, then +0.5% per hour

    def klines(self, symbol, interval, limit):
        last_open = int(self.clock() * 1000) // HOUR_MS * HOUR_MS - HOUR_MS
        opens = [last_open - HOUR_MS * i for i in range(limit - 1, -1, -1)]
        return pd.DataFrame({"open": [self.price(ms) for ms in opens], "close": [self.price(ms + HOUR_MS) for ms in opens]},
                            index=pd.to_datetime(opens, unit="ms", utc=True))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "shadow.jsonl"
        self.clock = Clock(NOW)

    def recorder(self, market=None, candles=None):
        self.market = market or FakeMarket(collapse={"SOLUSDT"})
        self.candles = candles or FakeCandles(self.clock)
        return ShadowRecorder(self.market, self.candles, self.path, self.clock)


class DetectionTests(Base):
    def test_records_exactly_the_collapse_with_falling_price(self):
        rec = self.recorder()
        rec.tick(["SOLUSDT", "ETHUSDT"])
        self.assertEqual(set(rec.events), {f"SOLUSDT-{T}"})                 # ETH: no OI collapse
        event = rec.events[f"SOLUSDT-{T}"]
        self.assertLess(event["oi24"], event["threshold"])
        self.assertAlmostEqual(event["oi24"], -0.15, places=2)
        self.assertAlmostEqual(event["ret24"], math.log(100 / 110), places=6)
        self.assertEqual(event["price"], 100.0)
        self.assertIsNone(rec.last_error)

    def test_price_must_have_fallen(self):
        rec = self.recorder(candles=FakeCandles(self.clock, rising_into_T=True))
        rec.tick(["SOLUSDT"])
        self.assertEqual(rec.events, {})                                   # OI collapsed but price rose: not our pattern

    def test_one_evaluation_per_symbol_per_hour_and_no_duplicates_after_restart(self):
        rec = self.recorder()
        rec.tick(["SOLUSDT", "ETHUSDT"])
        calls = len(self.market.calls)
        self.clock.now += 300
        rec.tick(["SOLUSDT", "ETHUSDT"])
        self.assertEqual(len(self.market.calls), calls)                    # nothing re-fetched within the same hour
        again = ShadowRecorder(self.market, self.candles, self.path, self.clock)
        again.tick(["SOLUSDT"])
        self.assertEqual(len(again.events), 1)
        self.assertEqual(sum(1 for line in self.path.read_text().splitlines() if '"k": "event"' in line), 1)

    def test_next_hour_is_evaluated_again(self):
        rec = self.recorder()
        rec.tick(["ETHUSDT"])
        calls = len(self.market.calls)
        self.clock.now += HOUR_MS / 1000
        rec.tick(["ETHUSDT"])
        self.assertGreater(len(self.market.calls), calls)

    def test_failures_are_isolated_reported_and_never_raised(self):
        rec = self.recorder(FakeMarket(collapse={"SOLUSDT"}, fail={"BTCUSDT"}))
        rec.tick(["BTCUSDT", "SOLUSDT"])
        self.assertIn(f"SOLUSDT-{T}", rec.events)                          # the healthy symbol still got recorded
        self.assertIn("BTCUSDT", rec.last_error)
        rec.tick(["BTCUSDT"])                                              # failed symbols are retried, not marked done
        self.assertGreaterEqual(self.market.calls.count("BTCUSDT"), 2)

    def test_unexpected_response_shape_is_a_clear_message_not_a_crash(self):
        class Odd(FakeMarket):
            def open_interest_hist(self, symbol, period, limit):
                return [{"oi": 1, "ts": 2}]
        rec = self.recorder(Odd())
        rec.tick(["SOLUSDT"])
        self.assertIn("unexpected open-interest response shape", rec.last_error)
        self.assertEqual(rec.events, {})

    def test_stale_or_short_open_interest_is_refused(self):
        class Short(FakeMarket):
            def open_interest_hist(self, symbol, period, limit):
                return super().open_interest_hist(symbol, period, limit)[-100:]
        rec = self.recorder(Short(collapse={"SOLUSDT"}))
        rec.tick(["SOLUSDT"])
        self.assertIn("open-interest points", rec.last_error)
        self.clock.now += 5 * 3600                                         # data ends 5h before "now"
        rec2 = self.recorder()
        rec2.path = Path(self.tmp.name) / "other.jsonl"
        rec2.tick(["SOLUSDT"])
        self.assertIn("stale", rec2.last_error)

    def test_frozen_definition_cannot_drift_silently(self):
        self.assertEqual((sh.OI_PERIOD, sh.OI_LIMIT, sh.QUANTILE, sh.WINDOW, sh.MIN_PERIODS), ("1h", 500, 0.05, 480, 288))
        self.assertEqual((sh.HORIZONS, sh.COST_BPS), ((8, 24, 48), 14.0))
        self.assertEqual(sh.DECISION, {"min_events": 150, "min_days": 90, "min_mean_bps": 15.0, "min_t": 1.65,
                                       "min_positive_months": 0.6})


class OutcomeTests(Base):
    def test_outcomes_match_hand_calculation(self):
        rec = self.recorder()
        rec.tick(["SOLUSDT"])
        self.clock.now = T / 1000 + 10 * 3600 + 120                         # only the 8h position has closed (exit candle done)
        rec.tick([])
        self.assertEqual({h for (_, h) in rec.outcomes}, {8})
        self.clock.now = T / 1000 + 50 * 3600 + 120
        rec.tick([])
        self.assertEqual({h for (_, h) in rec.outcomes}, {8, 24, 48})
        step = math.log(1.005) * 1e4
        for horizon, settlements in ((8, 1), (24, 3), (48, 6)):            # entry 13:00; funding at 16:00, 00:00, 08:00 ...
            o = rec.outcomes[(f"SOLUSDT-{T}", horizon)]
            self.assertAlmostEqual(o["gross"], horizon * step, places=6)
            self.assertAlmostEqual(o["fund"], -settlements * 1.0, places=6)   # 0.0001 per settlement = 1 bp, a long pays it
            self.assertAlmostEqual(o["net"], horizon * step - settlements - 14.0, places=6)

    def test_funding_exactly_at_exit_is_not_charged_and_negative_funding_is_income(self):
        self.market = FakeMarket(collapse={"SOLUSDT"})
        self.market.funding_rate = -0.0002
        rec = self.recorder(self.market)
        rec.tick(["SOLUSDT"])
        self.clock.now = T / 1000 + 10 * 3600 + 120
        rec.tick([])
        self.assertAlmostEqual(rec.outcomes[(f"SOLUSDT-{T}", 8)]["fund"], +2.0, places=6)

    def test_funding_settlement_exactly_on_the_boundaries(self):
        """Archive data shows settlements stamped exactly on the hour. [entry, exit) counts: entry yes, exit no."""
        class Exact(FakeMarket):
            def funding_rates(self, symbol, start_ms, end_ms):
                return [{"fundingTime": t, "fundingRate": "0.00010000"} for t in range(T - DAY_MS, T + 4 * DAY_MS, HOUR_MS)
                        if (t // HOUR_MS) % 8 == 0]
        rec = self.recorder(Exact())
        hour = T + 3 * HOUR_MS                                              # detection 15:00 -> entry 16:00 (a settlement hour)
        event = {"k": "event", "id": f"SOLUSDT-{hour}", "symbol": "SOLUSDT", "hour": hour}
        rec.events[event["id"]] = event
        self.clock.now = hour / 1000 + 60 * 3600
        rec.tick([])
        # H=8: window [16:00, 00:00) holds the 16:00 settlement only (00:00 is the exit moment) -> 1 bp paid
        self.assertAlmostEqual(rec.outcomes[(event["id"], 8)]["fund"], -1.0, places=6)
        # H=24: [16:00, 16:00 next day) -> 16:00, 00:00, 08:00 = 3
        self.assertAlmostEqual(rec.outcomes[(event["id"], 24)]["fund"], -3.0, places=6)

    def test_outcomes_survive_a_restart_and_are_not_recomputed(self):
        rec = self.recorder()
        rec.tick(["SOLUSDT"])
        self.clock.now = T / 1000 + 10 * 3600 + 120
        rec.tick([])
        again = ShadowRecorder(self.market, self.candles, self.path, self.clock)
        self.assertEqual(set(again.outcomes), set(rec.outcomes))
        before = len(self.path.read_text().splitlines())
        again.tick([])
        self.assertEqual(len(self.path.read_text().splitlines()), before)

    def test_candles_that_scrolled_away_are_marked_lost_not_retried_forever(self):
        rec = self.recorder()
        rec.tick(["SOLUSDT"])
        class Gone(FakeCandles):
            def klines(self, symbol, interval, limit):
                return super().klines(symbol, interval, limit).iloc[-20:]
        rec.provider = Gone(self.clock)
        self.clock.now = T / 1000 + 400 * 3600
        rec.tick([])
        self.assertTrue(rec.outcomes[(f"SOLUSDT-{T}", 8)].get("lost"))
        self.assertEqual(rec.stats(8), {"n": 0})

    def test_corrupt_lines_are_skipped(self):
        rec = self.recorder()
        rec.tick(["SOLUSDT"])
        with self.path.open("a") as handle:
            handle.write("{not json\n{\"k\": \"event\"}\n\n")
        self.assertEqual(len(ShadowRecorder(self.market, self.candles, self.path, self.clock).events), 1)


class StatsTests(Base):
    def populate(self, rec, count, spacing_h, mean, std, symbols=16, seed=3):
        rng = np.random.default_rng(seed)
        base = T - count * spacing_h * HOUR_MS
        for k in range(count):
            hour = base + k * spacing_h * HOUR_MS
            event = {"k": "event", "id": f"S{k % symbols}-{hour}", "symbol": f"S{k % symbols}", "hour": hour}
            rec.events[event["id"]] = event
            for horizon in sh.HORIZONS:
                rec.outcomes[(event["id"], horizon)] = {"id": event["id"], "H": horizon, "net": float(mean + std * rng.standard_normal())}

    def test_overlapping_events_of_one_symbol_count_once(self):
        rec = self.recorder()
        for hour_offset in (0, 3, 20, 24, 25, 60):                          # entry is one hour after detection: a 24h hold blocks 25h
            event = {"id": f"X-{hour_offset}", "symbol": "X", "hour": T + hour_offset * HOUR_MS}
            rec.events[event["id"]] = event
            rec.outcomes[(event["id"], 24)] = {"net": 10.0}
        self.assertEqual([e["hour"] // HOUR_MS - T // HOUR_MS for e in rec.non_overlapping(24)], [0, 25, 60])
        self.assertEqual([e["hour"] // HOUR_MS - T // HOUR_MS for e in rec.non_overlapping(8)], [0, 20, 60])
        self.assertEqual(rec.stats(24)["n"], 3)

    def test_decision_rule_has_three_outcomes(self):
        rec = self.recorder()
        self.assertIn("yetersiz veri", rec.verdict())
        rec.start_ms = T - 100 * DAY_MS
        self.populate(rec, 160, 14, mean=30, std=60)
        stats = rec.stats(24)
        self.assertEqual(stats["n"], 160)
        self.assertGreaterEqual(stats["t"], 1.65)
        self.assertIn("KURALI GEÇTİ", rec.verdict())
        weak = self.recorder()
        weak.path = Path(self.tmp.name) / "weak.jsonl"
        weak.start_ms = T - 100 * DAY_MS
        self.populate(weak, 160, 14, mean=5, std=60)
        self.assertIn("KURALI GEÇMEDİ", weak.verdict())
        young = self.recorder()
        young.path = Path(self.tmp.name) / "young.jsonl"
        young.start_ms = T - 30 * DAY_MS
        self.populate(young, 160, 14, mean=30, std=60)
        self.assertIn("yetersiz veri", young.verdict())                    # strong numbers but too early to trust

    def test_summary_text_is_compact_and_honest(self):
        rec = self.recorder()
        self.assertIn("henüz sonuçlanan olay yok", rec.format_summary())
        rec.start_ms = T - 100 * DAY_MS
        self.populate(rec, 40, 14, mean=20, std=50)
        text = rec.format_summary()
        for expected in ("Gölge kayıt", "işlem açılmıyor", " 8sa:", "24sa:", "48sa:", "Geçmiş çalışma", "Karar:", "Kural:"):
            self.assertIn(expected, text)
        self.assertLessEqual(len(text.splitlines()), 10)
        rec.last_error = "BTCUSDT: boom"
        self.assertIn("Son sorun", rec.format_summary())


class BotIntegrationTests(Base):
    def make_bot(self, shadow):
        prices = {s: v["last"] for s, v in DemoProvider(int(self.clock() * 1000)).tickers().items()}
        notifier = Notifier()
        settings = Settings(min_quote_volume=1.0, scan_interval_seconds=300)
        bot = Bot(settings, DemoProvider(int(self.clock() * 1000)), "demo", None, notifier, StateStore(None), None,
                  FakeExchange(prices, self.clock), None, self.clock, sleep=lambda s: None, shadow=shadow)
        bot.start()
        return bot, notifier

    def test_golge_command_and_aliases(self):
        bot, _ = self.make_bot(self.recorder())
        self.assertIn("Gölge kayıt", bot._answer("/golge"))
        self.assertEqual(bot._answer("/shadow"), bot._answer("/golge"))
        off, _ = self.make_bot(None)
        self.assertIn("kapalı", off._answer("/golge"))
        self.assertIn("/golge", off._answer("/help"))

    def test_loop_feeds_the_recorder_on_its_own_schedule_and_survives_its_errors(self):
        calls = []
        class Spy:
            def tick(self, symbols): calls.append(list(symbols)); raise RuntimeError("shadow exploded")
        bot, _ = self.make_bot(Spy())
        bot.tick()
        bot.tick()                                                          # within the interval: not called again
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], bot.universe)
        self.clock.now += 301
        bot.tick()
        self.assertEqual(len(calls), 2)                                    # an exploding recorder never stops the bot

    def test_shadow_events_never_produce_telegram_messages(self):
        rec = self.recorder(FakeMarket(collapse={"SOLUSDT", "ETHUSDT"}))
        bot, notifier = self.make_bot(rec)
        bot.universe = ["SOLUSDT", "ETHUSDT"]
        bot.shadow = rec
        before = len(notifier.sent)
        bot._shadow_at = 0
        bot.tick()
        self.assertGreaterEqual(len(rec.events), 1)
        self.assertEqual(len(notifier.sent), before)


class ApiShapeTests(unittest.TestCase):
    def test_new_public_endpoints(self):
        class Session:
            def __init__(self): self.requests = []
            def request(self, method, url, params=None, headers=None, timeout=None):
                self.requests.append((method, url, params))
                return type("R", (), {"status_code": 200, "json": lambda self_: []})()
        session = Session()
        client = MarketClient(session=session)
        client.open_interest_hist("BTCUSDT", "1h", 500)
        client.funding_rates("BTCUSDT", 1, 2)
        self.assertEqual(session.requests[0][1], "https://fapi.binance.com/futures/data/openInterestHist")
        self.assertEqual(session.requests[0][2], {"symbol": "BTCUSDT", "period": "1h", "limit": 500})
        self.assertEqual(session.requests[1][1], "https://fapi.binance.com/fapi/v1/fundingRate")
        self.assertEqual(session.requests[1][2]["startTime"], 1)


if __name__ == "__main__":
    unittest.main()
