import tempfile
import unittest
from pathlib import Path
from crypto_watcher import signals as sg
from crypto_watcher.bot import Bot
from crypto_watcher.config import Settings
from crypto_watcher.executor import Executor
from crypto_watcher.journal import Journal
from test_executor import Base
from helpers import Clock, make_infos

HOUR, DAY = sg.HOUR_MS, 24 * sg.HOUR_MS
BASE = dict(tp_r=2.0, breakeven_r=1.0, hold_hours=48, fee=0.0005)


def bar(i, o, h, l, c, start=0):
    return [start + i * sg.BAR_MS, str(o), str(h), str(l), str(c)]


def sim(side, rows, frac=0.02, **override):
    return sg.simulate(side, frac, [bar(i, *r) for i, r in enumerate(rows)], **{**BASE, **override})


class SimulateTests(unittest.TestCase):
    # LONG: entry 100, distance 2 -> stop 98, target 104; round-trip cost = 2 * 0.0007 * 100 / 2 = 0.07R

    def test_take_profit(self):
        r = sim("LONG", [(100, 101, 99.5, 100.5), (100.5, 104.5, 100, 104)])
        self.assertEqual((r["exit"], r["gross_r"], r["net_r"]), ("TP", 2.0, 1.93))

    def test_stop_loss(self):
        r = sim("LONG", [(100, 100.5, 97.9, 98)])
        self.assertEqual((r["exit"], r["gross_r"], r["net_r"]), ("SL", -1.0, -1.07))

    def test_bar_touching_both_counts_as_stopped(self):
        self.assertEqual(sim("LONG", [(100, 105, 97, 100)])["exit"], "SL")

    def test_breakeven_starts_the_bar_after_it_was_reached(self):
        r = sim("LONG", [(100, 102.5, 99.5, 102), (102, 102, 99.9, 100)])      # +1R in bar 0, then back through entry
        self.assertEqual(r["exit"], "BE")
        self.assertEqual((r["gross_r"], r["net_r"]), (0.05, -0.02))          # stop at 100.1 = entry + 2 fees

    def test_without_breakeven_the_original_stop_stays(self):
        rows = [(100, 102.5, 99.5, 102), (102, 102, 99.9, 100), (100, 100, 97.5, 98)]
        self.assertEqual(sim("LONG", rows, breakeven_r=0)["exit"], "SL")
        self.assertEqual(sim("LONG", rows)["exit"], "BE")

    def test_short_is_mirrored(self):
        r = sim("SHORT", [(100, 100.5, 95.5, 96)])
        self.assertEqual((r["exit"], r["gross_r"], r["net_r"]), ("TP", 2.0, 1.93))
        self.assertEqual(sim("SHORT", [(100, 102.1, 99.5, 102)])["exit"], "SL")

    def test_gap_through_the_stop_fills_at_the_open(self):
        r = sim("LONG", [(100, 100.5, 99.5, 100), (97, 97.5, 96.5, 97)])
        self.assertEqual((r["exit"], r["gross_r"], r["net_r"]), ("SL", -1.5, -1.57))

    def test_touching_the_level_exactly_counts(self):
        self.assertEqual(sim("LONG", [(100, 100.5, 98.0, 99)])["exit"], "SL")               # low == stop
        self.assertEqual(sim("LONG", [(100, 104.0, 99.5, 103)])["exit"], "TP")              # high == target
        self.assertEqual(sim("SHORT", [(100, 102.0, 99.5, 101)])["exit"], "SL")
        self.assertEqual(sim("SHORT", [(100, 100.5, 96.0, 97)])["exit"], "TP")

    def test_time_stop_exits_at_the_close_of_the_last_bar(self):
        quiet = [(100, 100.5, 99.5, 100.2)] * 12
        r = sim("LONG", quiet, hold_hours=1)
        self.assertEqual((r["exit"], r["gross_r"], r["net_r"]), ("TIME", 0.1, 0.03))
        self.assertIsNone(sim("LONG", quiet[:11], hold_hours=1))          # not decided yet
        self.assertIsNone(sim("LONG", quiet))                              # 48h hold, 1h of bars

    def test_excursions_and_empty_input(self):
        r = sim("LONG", [(100, 103, 99, 100), (100, 100, 98.5, 98.5), (98.5, 98.5, 97, 97)], breakeven_r=0)
        self.assertEqual((r["mfe_r"], r["mae_r"]), (1.5, -1.5))
        self.assertIsNone(sg.simulate("LONG", 0.02, [], **BASE))
        self.assertIsNone(sg.simulate("LONG", 0.0, [bar(0, 100, 101, 99, 100)], **BASE))


def candidate(symbol="AAAUSDT", side="LONG", date="2026-10-08 10:00", score=75.0, price=100.0, distance=2.0, plan=True):
    risk = {"entry": price, "risk_distance": distance} if plan else None
    return {"symbol": symbol, "side": side, "date": date, "score": score, "market_regime": "BULL", "sides": {side: {"risk_plan": risk}}}


class FakeMarket:
    def __init__(self, bars_fn=None):
        self.calls, self.bars_fn, self.error = [], bars_fn, None

    def klines(self, symbol, interval, limit, start_ms=None, end_ms=None):
        self.calls.append((symbol, interval, limit, start_ms, end_ms))
        if self.error:
            raise self.error
        return self.bars_fn(symbol, start_ms, end_ms) if self.bars_fn else []


def stopped_out(symbol, start, end):
    return [bar(0, 100, 100.2, 97.5, 98, start)] + [bar(i, 98, 98.5, 97.5, 98, start) for i in range(1, 20)]


class SignalLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state" / "signals.jsonl"
        self.clock = Clock(1_791_000_000.0)
        self.market = FakeMarket(stopped_out)
        self.log = sg.SignalLog(self.path, self.market, Settings(), self.clock)
        self.seen_ms = int(self.clock() * 1000)

    def reopen(self):
        return sg.SignalLog(self.path, self.market, Settings(), self.clock)

    def test_labels(self):
        self.assertEqual(sg.skip_label("max same-direction (SHORT) positions"), "yön limiti")
        self.assertEqual(sg.skip_label("max open positions"), "toplam pozisyon limiti")
        self.assertEqual(sg.skip_label("cooldown"), "bekleme süresi")
        self.assertIsNone(sg.skip_label("already in a position"))
        self.assertIsNone(sg.skip_label("symbol not tradable on testnet"))

    def test_repeated_scans_of_one_candle_are_one_signal(self):
        for _ in range(5):
            self.log.note(candidate(), "yön limiti")
        self.log.note(candidate(date="2026-10-08 11:00"), "yön limiti")
        self.assertEqual(len(self.log.signals), 2)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)
        self.assertAlmostEqual(self.log.signals["AAAUSDT-2026-10-08 10:00"]["distance_frac"], 0.02)

    def test_signal_without_a_risk_plan_is_ignored(self):
        self.log.note(candidate(plan=False), "yön limiti")
        self.assertEqual(self.log.signals, {})

    def test_skipped_signal_upgrades_to_taken_once_and_never_back(self):
        self.log.note(candidate(), "yön limiti")
        self.clock.now += 120
        self.log.note(candidate(), sg.TAKEN, {"symbol": "AAAUSDT", "opened_ms": 42})
        record = self.log.signals["AAAUSDT-2026-10-08 10:00"]
        self.assertEqual((record["status"], record["trade_id"], record["seen_ms"]), (sg.TAKEN, "AAAUSDT-42", self.seen_ms + 120_000))
        self.log.note(candidate(), "yön limiti")
        self.assertEqual(self.log.signals["AAAUSDT-2026-10-08 10:00"]["status"], sg.TAKEN)
        again = self.reopen().signals["AAAUSDT-2026-10-08 10:00"]            # the update survives a restart
        self.assertEqual((again["status"], again["trade_id"], again["seen_ms"]), (sg.TAKEN, "AAAUSDT-42", self.seen_ms + 120_000))

    def test_replay_waits_then_resolves_once_with_closed_bars_only(self):
        self.log.note(candidate(), "yön limiti")
        self.log.resolve()
        self.assertEqual(self.market.calls, [])                                # too early
        self.clock.now += 3600
        self.log.resolve()
        symbol, interval, limit, start, end = self.market.calls[0]
        self.assertEqual((symbol, interval, start, end), ("AAAUSDT", "5m", self.seen_ms, int(self.clock() * 1000)))
        result = self.log.results["AAAUSDT-2026-10-08 10:00"]
        self.assertEqual((result["exit"], result["net_r"]), ("SL", -1.07))
        self.log.resolve()
        self.assertEqual(len(self.market.calls), 1)                            # done once
        self.assertEqual(self.reopen().results["AAAUSDT-2026-10-08 10:00"]["net_r"], -1.07)

    def test_bars_that_are_still_forming_are_not_used(self):
        quiet = lambda s, start: bar(0, 100, 100.5, 99.5, 100, start)
        forming = lambda s, start: bar(3, 100, 100.2, 97.5, 98, start)       # 4th bar: closes 1200 s after the signal
        self.market.bars_fn = lambda s, start, end: [quiet(s, start), forming(s, start)]
        self.log.note(candidate(), "yön limiti")
        self.clock.now += 1000
        self.log.resolve()
        self.assertEqual(len(self.market.calls), 1)
        self.assertEqual(self.log.results, {})                                  # the stop-out bar is still open: not used
        self.clock.now += 1800
        self.log.resolve()
        self.assertEqual(self.log.results["AAAUSDT-2026-10-08 10:00"]["exit"], "SL")

    def test_undecided_signal_is_retried_only_every_30_minutes(self):
        self.market.bars_fn = lambda s, start, end: [bar(0, 100, 100.5, 99.5, 100, start)]
        self.log.note(candidate(), "yön limiti")
        self.clock.now += 3600
        self.log.resolve()
        self.clock.now += 600
        self.log.resolve()
        self.assertEqual(len(self.market.calls), 1)
        self.clock.now += 1300
        self.log.resolve()
        self.assertEqual(len(self.market.calls), 2)
        self.assertEqual(self.log.results, {})

    def test_market_errors_are_isolated(self):
        self.log.note(candidate("AAAUSDT"), "yön limiti")
        self.log.note(candidate("BBBUSDT"), "yön limiti")
        self.clock.now += 3600
        self.market.error = OSError("down")
        self.log.resolve()
        self.assertIn("down", self.log.last_error)
        self.assertEqual(self.log.results, {})
        self.market.error = None
        self.log._tried.clear()
        self.log.resolve()
        self.assertEqual(len(self.log.results), 2)
        self.assertIsNone(self.log.last_error)

    def test_signals_that_never_resolve_expire(self):
        self.market.bars_fn = lambda s, start, end: []
        self.log.note(candidate(), "yön limiti")
        self.clock.now += (48 + 25) * 3600
        self.log.resolve()
        self.assertEqual(self.log.results["AAAUSDT-2026-10-08 10:00"]["exit"], "EXPIRED")
        self.assertEqual(self.log.rows(), [])
        self.assertEqual(self.market.calls, [])

    def test_corrupt_lines_are_skipped(self):
        self.log.note(candidate(), "yön limiti")
        with self.path.open("a") as handle:
            handle.write("garbage\n{\"k\": \"upd\", \"id\": \"nope\"}\n")
        self.assertEqual(len(self.reopen().signals), 1)


def rows(status, values, day0=1_790_000_000_000, per_day=1, **extra):
    return [{"id": f"{status}-{i}", "status": status, "net_r": v, "seen_ms": day0 + (i // per_day) * DAY, **extra} for i, v in enumerate(values)]


class SummaryTests(unittest.TestCase):
    def make(self, groups, pending=0):
        log = sg.SignalLog(Path(tempfile.mkdtemp()) / "s.jsonl", None, Settings())
        for r in sum(groups, []):
            log.signals[r["id"]] = {k: v for k, v in r.items() if k != "net_r"}
            log.results[r["id"]] = {"id": r["id"], "net_r": r["net_r"], "exit": "SL"}
        for i in range(pending):
            log.signals[f"p{i}"] = {"id": f"p{i}", "status": "yön limiti", "seen_ms": 0}
        return log

    def test_empty(self):
        text = self.make([], pending=2).format_summary()
        self.assertIn("2 sinyal, 0 sonuçlandı, 2 bekliyor", text)
        self.assertIn("Henüz sonuçlanmış sinyal yok", text)

    def test_groups_and_small_sample_refusal(self):
        text = self.make([rows(sg.TAKEN, [1.9, -1.1, -1.1, -1.1]), rows("yön limiti", [-1.1, 1.9], per_day=2)]).format_summary()
        self.assertIn("açıldı: 4 sinyal (4 gün) · kazanma %25 · ortalama -0.35R", text)
        self.assertIn("yön limiti: 2 sinyal (1 gün) · kazanma %50 · ortalama +0.40R", text)
        self.assertIn("henüz yorum için erken", text)
        self.assertIn("2/30", text)

    def values(self, n, win, lose, wins):
        return [win if i % n < wins else lose for i in range(n)]

    def test_losing_skipped_signals_mean_the_limit_helps(self):
        text = self.make([rows("yön limiti", self.values(40, 1.9, -1.1, 10))]).format_summary()      # mean -0.35R
        self.assertIn("limit zarar eden işlemleri engelliyor", text)
        self.assertIn("-0.35R", text)

    def test_positive_but_noisy_is_not_evidence(self):
        noisy = [3.0, -2.0, 3.5, -2.5] * 10                      # mean +0.5, but wildly inconsistent day to day
        text = self.make([rows("yön limiti", noisy)]).format_summary()
        self.assertIn("şansla açıklanabilir", text)
        self.assertNotIn("fırsat kaçırtıyor", text)

    def test_consistently_positive_is_flagged_but_not_proven(self):
        steady = [0.9, 1.1, 1.0, 0.8, 1.2] * 8
        text = self.make([rows("yön limiti", steady), rows(sg.TAKEN, [-0.2] * 40)]).format_summary()
        self.assertIn("limit fırsat kaçırtıyor olabilir", text)
        self.assertIn("açılanlardan iyi", text)
        self.assertIn("kanıt değil", text)

    def test_signals_of_the_same_day_count_as_one_observation(self):
        # 40 signals on only 4 days: under the 10-day minimum no matter how good they look
        text = self.make([rows("yön limiti", [1.0] * 40, per_day=10)]).format_summary()
        self.assertIn("4/10 gün", text)
        self.assertIn("henüz yorum için erken", text)

    def test_t_statistic_uses_one_observation_per_day(self):
        data = rows("x", [1.0] * 25 + [0.0], per_day=5)
        data[-1]["seen_ms"] += 0                      # 5 days of five signals at +1.0, then a day with one signal at 0.0
        # daily means [1, 1, 1, 1, 1, 0]: mean 0.8333, sd 0.4082, t = 5.0 (a flat t over 26 signals would be ~11)
        self.assertAlmostEqual(sg._t_stat(data), 5.0, places=6)
        self.assertTrue(sg._t_stat(data[:10]) != sg._t_stat(data[:10]))          # fewer than 6 days: NaN

    def test_replay_is_checked_against_real_trades(self):
        taken = rows(sg.TAKEN, [-1.07, 1.93, -1.07, -1.07, 1.93], trade_id=None)
        journal = type("J", (), {"records": []})()
        for i, row in enumerate(taken):
            row["trade_id"] = f"T{i}"
            journal.records.append({"id": f"T{i}", "r": row["net_r"] + 0.1})
        text = self.make([taken]).format_summary(journal)
        self.assertIn("simülasyon ortalaması +0.13R, gerçek +0.23R", text)
        self.assertNotIn("Doğrulama", self.make([taken[:3]]).format_summary(journal))             # fewer than 5 pairs


class BotIntegrationTests(Base):
    def build(self, **settings):
        self.settings = Settings(min_quote_volume=1.0, **settings)
        self.executor = Executor(self.ex, self.settings, self.state, self.notifier, make_infos(self.symbols), self.clock)
        self.executor.roll_day(15_000.0)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.market = FakeMarket(lambda s, start, end: [])
        self.signals = sg.SignalLog(Path(self.dir.name) / "signals.jsonl", self.market, self.settings, self.clock)
        self.journal = Journal(Path(self.dir.name) / "journal.jsonl", self.clock)
        bot = Bot(self.settings, self.demo, "demo", None, self.notifier, self.state, self.executor, self.ex, None, self.clock,
                  sleep=lambda s: None, journal=self.journal, signals=self.signals)
        bot.start()
        return bot

    def statuses(self):
        return {s["symbol"]: s["status"] for s in self.signals.signals.values()}

    def test_limit_blocked_signal_is_logged_with_its_reason_and_opened_ones_as_taken(self):
        bot = self.build(max_same_direction=1)
        bot.tick()
        self.assertEqual(self.statuses(), {"DOGEUSDT": sg.TAKEN, "BTCUSDT": "yön limiti", "XRPUSDT": sg.TAKEN})
        taken = self.signals.signals[next(k for k in self.signals.signals if k.startswith("DOGE"))]
        self.assertEqual(taken["trade_id"], f"DOGEUSDT-{self.state['trades']['DOGEUSDT']['opened_ms']}")
        self.assertEqual(len(self.state["trades"]), 2)                          # trading itself is unchanged
        self.clock.now += 400
        bot.tick()                                                              # same candle: nothing new
        self.assertEqual(len(self.signals.signals), 3)
        self.assertNotIn("atlan", self.messages().lower())                       # no Telegram pushes

    def test_without_limits_everything_is_taken(self):
        bot = self.build()
        bot.tick()
        self.assertEqual(set(self.statuses().values()), {sg.TAKEN})

    def test_command_and_alias(self):
        bot = self.build(max_same_direction=1)
        bot.tick()
        reply = bot._answer("/atlanan")
        self.assertIn("3 sinyal", reply)
        self.assertEqual(bot._answer("/limit"), reply)
        self.assertEqual(bot._answer("/skipped"), reply)
        self.assertIn("/atlanan", bot._answer("/help"))

    def test_a_failing_log_never_stops_trading(self):
        bot = self.build(max_same_direction=1)

        def boom(*args, **kwargs):
            raise RuntimeError("disk on fire")
        self.signals.note = boom
        bot.tick()
        self.assertEqual(len(self.state["trades"]), 2)
        self.signals.resolve = boom
        self.clock.now += 400
        bot.tick()                                                              # must not raise

    def test_resolution_is_wired_into_the_loop(self):
        bot = self.build(max_same_direction=1)
        bot.tick()
        self.clock.now += 3600
        bot.tick()
        self.assertTrue(self.market.calls)

    def test_command_without_signal_log(self):
        bot = Bot(self.settings, self.demo, "demo", None, self.notifier, self.state, None, self.ex, None, self.clock, sleep=lambda s: None)
        self.assertIn("kapalı", bot._answer("/atlanan"))


if __name__ == "__main__":
    unittest.main()
