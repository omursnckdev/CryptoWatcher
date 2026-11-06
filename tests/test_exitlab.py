import random
import tempfile
import unittest
from pathlib import Path
from crypto_watcher import exitlab as xl
from crypto_watcher import signals as sg
from crypto_watcher.bot import Bot
from crypto_watcher.executor import Executor
from test_executor import Base
from test_signals import FakeMarket, bar, candidate
from helpers import Clock, make_infos, legacy_settings

HOUR = sg.HOUR_MS
BASE = dict(tp_r=3.0, breakeven_r=1.0, hold_hours=48, fee=0.0005)
COST = 2 * (0.0005 + sg.SLIPPAGE) * 100 / 2          # entry 100, 1R = 2 -> 0.07R round trip
SCALED = {k: xl.VARIANTS["scaled"][k] for k in ("tranches", "trail_after_r", "trail_r", "extend")}
TRAIL = {k: xl.VARIANTS["trail"][k] for k in ("tranches", "trail_after_r", "trail_r", "extend")}


def bars(rows, start=0):
    return [bar(i, *r, start=start) for i, r in enumerate(rows)]


def run(side, rows, frac=0.02, **kw):
    return xl.replay(side, frac, bars(rows), **{**BASE, **kw})


def random_path(rng, n, side):
    price, rows = 100.0, []
    for _ in range(n):
        o = price
        c = o * (1 + rng.gauss(0, 0.006))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.003)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.003)))
        if rng.random() < 0.02:                                          # occasional gaps
            c = h = l = o = o * (1 + rng.choice((-1, 1)) * 0.03)
        rows.append((o, h, l, c))
        price = c
    return rows


class ReplayEquivalenceTests(unittest.TestCase):
    def test_defaults_reproduce_the_signal_log_replay_exactly(self):
        rng = random.Random(7)
        checked = 0
        for trial in range(3000):
            side = rng.choice(("LONG", "SHORT"))
            rules = dict(tp_r=rng.choice((2.0, 3.0)), breakeven_r=rng.choice((0.0, 1.0)), hold_hours=rng.choice((0, 4, 48)),
                         fee=0.0005)
            rows = random_path(rng, rng.randint(1, 700), side)
            frac = rng.choice((0.01, 0.02, 0.04))
            lock = rng.choice(({}, dict(lock_r=2.0, lock_stop_r=1.0))) if rules["tp_r"] == 3.0 else {}
            old = sg.simulate(side, frac, bars(rows), **rules, **lock)
            new = xl.replay(side, frac, bars(rows), **rules, **lock)
            if old is None:
                self.assertIsNone(new, trial)
                continue
            checked += 1
            self.assertEqual((new["gross_r"], new["net_r"], new["exit"]), (old["gross_r"], old["net_r"], old["exit"]), trial)
        self.assertGreater(checked, 1500)


class VariantTests(unittest.TestCase):
    # LONG: entry 100, 1R = 2 -> stop 98, 2R = 104, 3R = 106

    def test_scaled_takes_thirds_then_trails(self):
        r = run("LONG", [(100, 101, 99.5, 100.5), (100.5, 104.2, 100.5, 104), (104, 106.1, 103.5, 106),
                         (106, 110, 105.5, 109.5), (109.5, 109.6, 107, 107.5)], **SCALED)
        # 1/3 at 2R, 1/3 at 3R; best 110 (5R) -> trail at 108 (4R); the last bar falls through it
        self.assertEqual(r["exit"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], (2 + 3 + 4) / 3, places=3)
        self.assertAlmostEqual(r["net_r"], round((2 + 3 + 4) / 3 - COST, 3), places=3)

    def test_scaled_remainder_falls_back_to_breakeven(self):
        r = run("LONG", [(100, 104.1, 99.5, 104), (104, 104.5, 103, 103.5), (103.5, 103.6, 99, 99.5)], **SCALED)
        # 1/3 at 2R; trail 104.1 - 2 = 102.1, raised to 104.5 - 2 = 102.5 after bar 2 -> the rest leaves at 102.5 (1.25R)
        self.assertEqual(r["exit"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], (2 + 2 * 1.25) / 3, places=3)

    def test_bar_touching_stop_and_target_counts_as_stopped(self):
        r = run("LONG", [(100, 104.5, 97.5, 103)], **SCALED)
        self.assertEqual((r["exit"], r["gross_r"]), ("SL", -1.0))

    def test_trail_has_no_fixed_target_and_obeys_the_time_stop(self):
        rows = [(100, 101, 99.5, 100.5)] + [(100.5 + i * 0.02, 100.6 + i * 0.02, 100.4 + i * 0.02, 100.5 + i * 0.02) for i in range(600)]
        r = run("LONG", rows, **TRAIL)
        self.assertEqual(r["exit"], "TIME")
        self.assertIsNone(run("LONG", rows[:100], **TRAIL))             # undecided before the hold time is up

    def test_trailing_stop_gap_fills_at_the_open(self):
        r = run("LONG", [(100, 108, 100, 108), (108, 108.5, 107, 108), (101, 101.5, 100.5, 101)], **TRAIL)
        # best 108.5 -> trail 106.5; the third bar opens at 101 below it
        self.assertEqual(r["exit"], "TRAIL")
        self.assertAlmostEqual(r["gross_r"], 0.5, places=3)

    def test_short_is_mirrored(self):
        long = run("LONG", [(100, 101, 99.5, 100.5), (100.5, 104.2, 100.5, 104), (104, 106.1, 103.5, 106),
                            (106, 110, 105.5, 109.5), (109.5, 109.6, 107, 107.5)], **SCALED)
        short = run("SHORT", [(100, 100.5, 99, 99.5), (99.5, 99.5, 95.8, 96), (96, 96.5, 93.9, 94),
                              (94, 94.5, 90, 90.5), (90.5, 93, 90.4, 92.5)], **SCALED)
        self.assertEqual((short["exit"], short["gross_r"]), (long["exit"], long["gross_r"]))

    def test_time_plus_keeps_a_winner_and_cuts_a_loser(self):
        flat_up = [(100, 100.6, 99.9, 100.5)] + [(100.5, 100.7, 100.3, 100.5)] * 700      # +0.25R at 48 h, never 3R
        flat_down = [(100, 100.1, 99.4, 99.5)] + [(99.5, 99.7, 99.3, 99.5)] * 700          # -0.25R at 48 h
        base_up = run("LONG", flat_up)
        plus_up = run("LONG", flat_up, extend=True)
        self.assertEqual(base_up["exit"], "TIME")
        self.assertIsNone(plus_up)                                    # kept beyond 48 h, 96 h not reached yet in 701 bars
        longer = flat_up + [(100.5, 100.7, 100.3, 100.5)] * 500
        self.assertEqual(run("LONG", longer, extend=True)["exit"], "TIME")          # forced out at 2 x 48 h
        self.assertEqual(run("LONG", flat_down, extend=True), run("LONG", flat_down))   # a loser leaves on time as before

    def test_empty_and_invalid(self):
        self.assertIsNone(xl.replay("LONG", 0.02, [], **BASE))
        self.assertIsNone(xl.replay("LONG", 0.0, bars([(100, 101, 99, 100)]), **BASE))


class FakeSignalLog:
    def __init__(self):
        self.signals, self.results = {}, {}

    def add(self, key, seen_ms, net_r=0.0, status=sg.TAKEN, side="LONG"):
        self.signals[key] = {"id": key, "symbol": key.split("-")[0], "side": side, "seen_ms": seen_ms, "distance_frac": 0.02,
                             "status": status}
        if net_r is not None:
            self.results[key] = {"net_r": net_r}


def winning_bars(symbol, start, end):
    """Up to 3R within a few bars: every rule closes; the live rule makes 3R."""
    return bars([(100, 101, 99.5, 100.5), (100.5, 104.2, 100.5, 104), (104, 106.1, 103.5, 106), (106, 110, 105.5, 109.5),
                 (109.5, 109.6, 107, 107.5)], start)


class ExitLabTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state" / "exits.jsonl"
        self.clock = Clock(1_791_000_000.0)
        self.market = FakeMarket(winning_bars)
        self.log = FakeSignalLog()
        self.settings = legacy_settings(tp_r=3.0, atr_stop_multiplier=3.0)
        self.lab = xl.ExitLab(self.path, self.market, self.settings, self.log, self.clock)
        self.now_ms = int(self.clock() * 1000)

    def reopen(self):
        return xl.ExitLab(self.path, self.market, self.settings, self.log, self.clock)

    def test_waits_for_the_signal_log_then_resolves_once_and_persists(self):
        self.log.add("AAAUSDT-1", self.now_ms - 6 * HOUR, net_r=None)
        self.lab.resolve()
        self.assertEqual(self.market.calls, [])                       # the signal log has not resolved it yet
        self.log.results["AAAUSDT-1"] = {"net_r": round(3.0 - COST, 3)}
        self.lab.resolve()
        symbol, interval, limit, start, end = self.market.calls[0]
        self.assertEqual((symbol, interval, limit, start), ("AAAUSDT", "5m", xl.BAR_LIMIT, self.now_ms - 6 * HOUR))
        record = self.lab.results["AAAUSDT-1"]
        self.assertEqual((record["base"], record["exits"]["base"], record["base_matches_log"]), (round(3.0 - COST, 3), "TP", True))
        self.assertAlmostEqual(record["scaled"], round(3.0 - COST, 3), places=3)
        self.lab.resolve()
        self.assertEqual(len(self.market.calls), 1)
        again = self.reopen()
        self.assertEqual((again.results["AAAUSDT-1"]["base"], again.start_ms), (record["base"], self.lab.start_ms))

    def test_a_mismatch_with_the_signal_log_is_flagged(self):
        self.log.add("AAAUSDT-1", self.now_ms - 6 * HOUR, net_r=-1.07)
        self.lab.resolve()
        self.assertFalse(self.lab.results["AAAUSDT-1"]["base_matches_log"])
        self.assertIn("farklı çıktı", self.lab.format_summary())

    def test_undecided_signals_are_retried_every_30_minutes_and_finally_expire(self):
        self.market.bars_fn = lambda s, start, end: bars([(100, 100.5, 99.5, 100)], start)
        self.log.add("AAAUSDT-1", self.now_ms - 6 * HOUR)
        self.lab.resolve()
        self.clock.now += 600
        self.lab.resolve()
        self.assertEqual(len(self.market.calls), 1)
        self.clock.now += 1300
        self.lab.resolve()
        self.assertEqual((len(self.market.calls), self.lab.results), (2, {}))
        self.clock.now += 5 * 24 * 3600
        self.lab.resolve()
        self.assertTrue(self.lab.results["AAAUSDT-1"]["expired"])
        self.assertEqual(self.lab.rows(), [])

    def test_old_signals_are_replayed_from_history_not_expired(self):
        self.log.add("AAAUSDT-1", self.now_ms - 20 * 24 * HOUR)
        self.lab.resolve()
        self.assertNotIn("expired", self.lab.results["AAAUSDT-1"])

    def test_a_backlog_is_worked_off_in_batches_oldest_first(self):
        for i in range(xl.BATCH + 5):
            self.log.add(f"S{i:02d}USDT-1", self.now_ms - (100 - i) * HOUR)
        self.lab.resolve()
        self.assertEqual(len(self.lab.results), xl.BATCH)
        self.assertIn("S00USDT-1", self.lab.results)
        self.clock.now += 300
        self.lab.resolve()
        self.assertEqual(len(self.lab.results), xl.BATCH + 5)

    def test_market_errors_are_isolated(self):
        self.log.add("AAAUSDT-1", self.now_ms - 6 * HOUR)
        self.market.error = OSError("down")
        self.lab.resolve()
        self.assertIn("down", self.lab.last_error)
        self.assertIn("Son sorun", self.lab.format_summary())
        self.market.error = None
        self.lab._tried.clear()
        self.lab.resolve()
        self.assertIsNone(self.lab.last_error)
        self.assertEqual(len(self.lab.results), 1)

    def test_corrupt_lines_are_skipped(self):
        self.log.add("AAAUSDT-1", self.now_ms - 6 * HOUR)
        self.lab.resolve()
        with self.path.open("a", encoding="utf-8") as f:
            f.write("{broken\n")
        self.assertEqual(len(self.reopen().results), 1)


def rows_with(diffs_by_day, key="scaled", base=0.0):
    rows = []
    for day, diffs in enumerate(diffs_by_day):
        for j, d in enumerate(diffs):
            r = {"seen_ms": day * 24 * HOUR + j * HOUR, "base": base, "scaled": base, "trail": base, "time_plus": base}
            r[key] = base + d
            rows.append(r)
    return rows


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.lab = xl.ExitLab(Path(tempfile.mkdtemp()) / "exits.jsonl", FakeMarket(), legacy_settings(), FakeSignalLog(), Clock())

    def test_too_early(self):
        self.assertIn("henüz erken", self.lab.verdict(rows_with([[0.1]] * 30)))

    def test_consistent_improvement_passes(self):
        rng = random.Random(1)
        days = [[0.08 + rng.gauss(0, 0.05) for _ in range(2)] for _ in range(100)]
        self.assertIn("Kademeli", self.lab.verdict(rows_with(days)))

    def test_small_or_one_sided_improvement_fails(self):
        rng = random.Random(2)
        tiny = [[0.01 + rng.gauss(0, 0.005) for _ in range(2)] for _ in range(100)]
        self.assertIn("GEÇEN YOK", self.lab.verdict(rows_with(tiny)))                # t is huge but below +0.03R
        lopsided = [[0.3 + rng.gauss(0, 0.05)] * 2 for _ in range(50)] + [[-0.05 + rng.gauss(0, 0.05)] * 2 for _ in range(50)]
        self.assertIn("GEÇEN YOK", self.lab.verdict(rows_with(lopsided)))            # second half negative

    def test_day_clustering(self):
        same_day = rows_with([[0.1] * 10, [-0.1]] + [[0.0]] * 6)
        s = xl.paired_stats(same_day, "scaled")
        self.assertAlmostEqual(s["mean"], (1.0 - 0.1) / 17)
        self.assertEqual(s["n"], 17)


class BotWiringTests(Base):
    def build(self, **settings):
        self.settings = legacy_settings(min_quote_volume=1.0, **settings)
        self.executor = Executor(self.ex, self.settings, self.state, self.notifier, make_infos(self.symbols), self.clock)
        self.executor.roll_day(15_000.0)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.market = FakeMarket(lambda s, start, end: [])
        self.signals = sg.SignalLog(Path(self.dir.name) / "signals.jsonl", self.market, self.settings, self.clock)
        self.lab = xl.ExitLab(Path(self.dir.name) / "exits.jsonl", self.market, self.settings, self.signals, self.clock)
        bot = Bot(self.settings, self.demo, "demo", None, self.notifier, self.state, self.executor, self.ex, None, self.clock,
                  sleep=lambda s: None, signals=self.signals, exitlab=self.lab)
        bot.start()
        return bot

    def test_command_alias_and_help(self):
        bot = self.build()
        reply = bot._answer("/cikis")
        self.assertIn("Çıkış kuralı testi", reply)
        self.assertEqual(bot._answer("/exits"), reply)
        self.assertIn("/cikis", bot._answer("/help"))

    def test_trading_is_unchanged_and_a_failing_lab_never_stops_it(self):
        bot = self.build(max_same_direction=1)
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("disk on fire")
        self.lab.resolve = boom
        bot.tick()
        self.assertEqual(len(self.state["trades"]), 2)                # same trades as without the lab
        self.clock.now += 400
        bot.tick()                                                    # must not raise
        self.assertTrue(calls)                                        # it is wired into the loop
        self.assertNotIn("Çıkış", self.messages())                    # no Telegram pushes

    def test_command_without_lab(self):
        bot = Bot(legacy_settings(), self.demo, "demo", None, self.notifier, self.state, None, self.ex, None, self.clock, sleep=lambda s: None)
        self.assertIn("kapalı", bot._answer("/cikis"))


if __name__ == "__main__":
    unittest.main()
