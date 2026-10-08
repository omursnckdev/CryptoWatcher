import json
import tempfile
import unittest
from pathlib import Path
from crypto_watcher import journal as jr
from crypto_watcher.bot import Bot
from crypto_watcher.executor import Executor
from test_executor import Base
from helpers import Clock, make_infos

HOUR = jr.HOUR_MS


def trade(side="LONG", entry=100.0, distance=2.0, **extra):
    return {"symbol": "AAAUSDT", "side": side, "entry": entry, "risk_distance": distance, "opened_ms": 1_000_000, **extra}


def record(**extra):
    base = {"id": "AAAUSDT-1", "symbol": "AAAUSDT", "side": "LONG", "closed_ms": 10 * HOUR, "opened_ms": 0, "entry": 100.0,
            "risk_distance": 2.0, "reason": "STOP_LOSS", "r": -1.0, "mfe_r": 0.1, "mae_r": -1.0, "kind": "never_worked", "after": None}
    return {**base, **extra}


def bars(start_ms, prices, step=300_000):
    """5-minute bars [open_time, open, high, low, close]; `prices` are (open, high, low) triples."""
    return [[start_ms + i * step, str(o), str(h), str(l), str(o)] for i, (o, h, l) in enumerate(prices)]


class TrackTests(unittest.TestCase):
    def test_long_excursions_in_r(self):
        t = trade("LONG")
        for mark, expected in ((103.0, 1.5), (99.0, -0.5), (96.0, -2.0)):
            jr.track(t, mark, 5)
        self.assertEqual(t["mfe_r"], 1.5)
        self.assertEqual(t["mae_r"], -2.0)

    def test_short_is_mirrored(self):
        t = trade("SHORT")
        jr.track(t, 97.0, 5)       # price fell 3 = +1.5R for a short
        jr.track(t, 104.0, 6)      # price rose 4 = -2R for a short
        self.assertEqual((t["mfe_r"], t["mae_r"]), (1.5, -2.0))
        self.assertEqual((t["mfe_ms"], t["mae_ms"]), (5, 6))

    def test_extremes_never_shrink(self):
        t = trade("LONG")
        jr.track(t, 104.0, 1)
        jr.track(t, 100.0, 2)
        self.assertEqual((t["mfe_r"], t["mfe_ms"]), (2.0, 1))

    def test_bad_input_is_ignored(self):
        t = trade("LONG", risk_distance=0)
        jr.track(t, 101.0, 1)
        self.assertNotIn("mfe_r", t)


class DiagnoseTests(unittest.TestCase):
    def kind(self, **extra):
        return jr.diagnose(record(**extra))

    def test_each_outcome(self):
        self.assertEqual(self.kind(reason="TAKE_PROFIT", r=1.9, mfe_r=2.1), "win")
        self.assertEqual(self.kind(r=0.5, reason="TIME_STOP"), "win")
        self.assertEqual(self.kind(reason="STOP_LOSS", mfe_r=0.24), "never_worked")
        self.assertEqual(self.kind(reason="STOP_LOSS", mfe_r=0.25), "faded")
        self.assertEqual(self.kind(reason="STOP_LOSS", mfe_r=0.99), "faded")
        self.assertEqual(self.kind(reason="STOP_LOSS", mfe_r=1.0), "gave_back")
        self.assertEqual(self.kind(reason="BREAKEVEN_STOP", r=-0.1, mfe_r=1.2), "breakeven")
        self.assertEqual(self.kind(reason="TIME_STOP", r=-0.2), "time")
        self.assertEqual(self.kind(reason="EXTERNAL", r=-0.2), "external")
        self.assertEqual(self.kind(reason="STOP_LOSS", r=None), "unknown")


class StopVerdictTests(unittest.TestCase):
    CLOSED = 10 * HOUR

    def make(self, n, fn):
        return bars(self.CLOSED, [fn(i) for i in range(n)])

    def test_price_recovers_after_long_stopped_out(self):
        # entry 100, distance 2 => 1R = 2% of price. Reference 98; the high reaches 100.5 => +2.55% = 1.28R, low 97.9
        data = self.make(144, lambda i: (98.0, 100.5 if i == 50 else 98.2, 97.9))
        result = jr.stop_verdict(record(), data)
        self.assertEqual(result["verdict"], "too_tight")
        self.assertAlmostEqual(result["fav_r"], (100.5 / 98 - 1) * 50, places=2)

    def test_price_keeps_falling_after_long_stop(self):
        data = self.make(144, lambda i: (98.0, 98.3, 95.0 if i == 20 else 97.5))     # low 95 = -3.06% = 1.53R against
        result = jr.stop_verdict(record(), data)
        self.assertEqual(result["verdict"], "right_exit")

    def test_short_uses_the_opposite_direction(self):
        data = self.make(144, lambda i: (98.0, 100.5 if i == 50 else 98.2, 97.9))     # a rally hurts a short
        result = jr.stop_verdict(record(side="SHORT"), data)
        self.assertEqual(result["verdict"], "right_exit")

    def test_whipsaw_goes_to_the_larger_side(self):
        # both sides exceed 1R in the window: the bigger move decides
        up = self.make(144, lambda i: (98.0, 100.5 if i == 10 else 98.2, 94.0 if i == 20 else 97.9))    # +1.28R vs -2.04R
        down = self.make(144, lambda i: (98.0, 102.0 if i == 10 else 98.2, 96.0 if i == 20 else 97.9))   # +2.04R vs -1.02R
        self.assertEqual(jr.stop_verdict(record(), up)["verdict"], "right_exit")
        self.assertEqual(jr.stop_verdict(record(), down)["verdict"], "too_tight")

    def test_quiet_price_is_neutral(self):
        data = self.make(144, lambda i: (98.0, 98.4, 97.6))
        self.assertEqual(jr.stop_verdict(record(), data)["verdict"], "neutral")

    def test_incomplete_window_gives_no_verdict(self):
        self.assertIsNone(jr.stop_verdict(record(), self.make(60, lambda i: (98.0, 100.5, 97.9))))
        self.assertIsNone(jr.stop_verdict(record(), []))

    def test_bars_outside_the_window_are_ignored(self):
        inside = self.make(144, lambda i: (98.0, 98.4, 97.6))
        outside = bars(self.CLOSED + jr.FOLLOWUP_HOURS * HOUR, [(98.0, 120.0, 97.0)] * 5)
        before = bars(self.CLOSED - 3 * 300_000, [(98.0, 120.0, 70.0)] * 3)
        self.assertEqual(jr.stop_verdict(record(), before + inside + outside)["verdict"], "neutral")


class JournalFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state" / "journal.jsonl"
        self.addCleanup(self.dir.cleanup)

    def test_records_survive_a_restart_and_corrupt_lines_are_skipped(self):
        journal = jr.Journal(self.path)
        journal.add(record(id="a"))
        journal.add(record(id="b"))
        with self.path.open("a") as handle:
            handle.write("{not json\n")
            handle.write('{"id": "x"}\n')
        self.assertEqual([r["id"] for r in jr.Journal(self.path).records], ["a", "b"])

    def test_followup_waits_for_the_window_then_fills_and_persists(self):
        clock = Clock(0.0)
        journal = jr.Journal(self.path, clock)
        journal.add(record())
        calls = []

        class Market:
            def klines(self, symbol, interval, limit, start_ms=None, end_ms=None):
                calls.append((symbol, interval, start_ms, end_ms))
                return bars(start_ms, [(98.0, 100.5 if i == 50 else 98.2, 97.9) for i in range(144)])

        clock.now = (10 * HOUR + 11 * HOUR) / 1000            # window not over yet
        journal.followups(Market())
        self.assertEqual(calls, [])
        clock.now = (10 * HOUR + 12 * HOUR + 10 * 60_000) / 1000
        journal.followups(Market())
        self.assertEqual(calls, [("AAAUSDT", "5m", 10 * HOUR, 22 * HOUR)])
        self.assertEqual(journal.records[0]["after"]["verdict"], "too_tight")
        self.assertEqual(jr.Journal(self.path).records[0]["after"]["verdict"], "too_tight")   # rewritten on disk
        journal.followups(Market())
        self.assertEqual(len(calls), 1)                          # done once

    def test_followup_skips_wins_and_gives_up_when_too_late(self):
        clock = Clock((10 * HOUR + 30 * HOUR) / 1000)
        journal = jr.Journal(self.path, clock)
        journal.add(record(id="w", kind="win", r=1.9))
        journal.add(record(id="late"))

        class Market:
            def klines(self, *args, **kwargs):
                raise AssertionError("must not be called")

        journal.followups(Market())
        self.assertIsNone(journal.records[0]["after"])
        self.assertEqual(journal.records[1]["after"], {"verdict": "none"})

    def test_market_errors_are_isolated_and_retried(self):
        clock = Clock((10 * HOUR + 13 * HOUR) / 1000)
        journal = jr.Journal(self.path, clock)
        journal.add(record())

        class Broken:
            def klines(self, *args, **kwargs):
                raise OSError("network down")

        journal.followups(Broken())
        self.assertIn("network down", journal.last_error)
        self.assertIsNone(journal.records[0]["after"])


class SummaryTests(unittest.TestCase):
    def make(self, rows):
        journal = jr.Journal(Path(tempfile.mkdtemp()) / "j.jsonl")
        for i, row in enumerate(rows):
            journal.records.append(record(id=str(i), **row))
        return journal

    def test_empty(self):
        self.assertIn("Henüz", self.make([]).format_summary())

    def test_small_sample_refuses_to_conclude(self):
        text = self.make([{"r": -1.0}, {"r": 1.8, "reason": "TAKE_PROFIT", "kind": "win", "mfe_r": 2.0}]).format_summary()
        self.assertIn("2 işlem", text)
        self.assertIn("Örnek küçük (2/20)", text)
        self.assertIn("kazanma %50", text)
        self.assertIn("ortalama +0.40R", text)

    def test_conclusion_flags_entry_problem(self):
        never = [{"r": -1.0, "mfe_r": 0.1, "kind": "never_worked", "after": {"verdict": "neutral"}}] * 14
        faded = [{"r": -1.0, "mfe_r": 0.5, "kind": "faded", "after": {"verdict": "too_tight"}}] * 6
        text = self.make(never + faded).format_summary()
        self.assertIn("hiç lehine gitmedi: sorun stop mesafesi değil", text)      # 14 of 20 stopped trades = 70%
        self.assertNotIn("stop çok dar olabilir", text)                            # only 30% reversed

    def test_stop_verdict_percentages_need_ten_judged(self):
        rows = [{"r": -1.0, "mfe_r": 0.5, "kind": "faded", "after": {"verdict": "too_tight"}}] * 12 + \
               [{"r": 1.8, "reason": "TAKE_PROFIT", "kind": "win", "mfe_r": 2.0}] * 8
        self.assertIn("stop çok dar olabilir", self.make(rows).format_summary())
        few = [{"r": -1.0, "mfe_r": 0.5, "kind": "faded", "after": {"verdict": "too_tight"}}] * 6 + \
              [{"r": 1.8, "reason": "TAKE_PROFIT", "kind": "win", "mfe_r": 2.0}] * 14
        self.assertNotIn("stop çok dar olabilir", self.make(few).format_summary())

    def test_stops_that_were_right_are_reported(self):
        rows = [{"r": -1.0, "mfe_r": 0.1, "kind": "never_worked", "after": {"verdict": "right_exit"}}] * 5 + \
               [{"r": -1.0, "mfe_r": 0.6, "kind": "faded", "after": {"verdict": "right_exit"}}] * 7 + \
               [{"r": 1.8, "reason": "TAKE_PROFIT", "kind": "win", "mfe_r": 2.0}] * 8
        text = self.make(rows).format_summary()
        self.assertIn("genişletmek zarar büyütür", text)
        self.assertIn("12× stoptan sonra fiyat aleyhe devam etti", text)

    def test_side_and_score_buckets_and_last_trades(self):
        rows = [{"r": -1.0, "side": "SHORT", "entry_ctx": {"score": 72}}, {"r": 2.0, "side": "LONG", "entry_ctx": {"score": 83},
                                                                          "reason": "TAKE_PROFIT", "kind": "win", "mfe_r": 2.0}]
        text = self.make(rows).format_summary()
        self.assertIn("SHORT: 1 işlem, ortalama -1.00R", text)
        self.assertIn("LONG: 1 işlem, ortalama +2.00R", text)
        self.assertIn("skor <75: 1 işlem, ortalama -1.00R", text)
        self.assertIn("skor ≥80: 1 işlem, ortalama +2.00R", text)
        self.assertIn("Son işlemler", text)


class ExecutorIntegrationTests(Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.journal = jr.Journal(Path(self.dir.name) / "journal.jsonl", self.clock)
        self.executor = Executor(self.ex, self.settings, self.state, self.notifier, make_infos(self.symbols), self.clock,
                                 journal=self.journal)
        self.executor.roll_day(15_000.0)

    def test_stopped_trade_that_first_went_our_way_is_journaled(self):
        t = self.open("DOGEUSDT")
        self.assertEqual(t["snap"]["score"], round(self.cands["DOGEUSDT"]["sides"]["LONG"]["score"], 1))
        self.ex.move("DOGEUSDT", t["entry"] + 0.5 * t["risk_distance"])
        self.clock.now += 60
        self.executor.manage()                                     # +0.5R sampled
        self.ex.move("DOGEUSDT", t["stop"] * 0.999)                # then the stop is hit
        self.clock.now += 3600
        self.executor.manage()
        (rec,) = self.journal.records
        self.assertEqual((rec["symbol"], rec["side"], rec["reason"], rec["kind"]), ("DOGEUSDT", "LONG", "STOP_LOSS", "faded"))
        self.assertAlmostEqual(rec["mfe_r"], 0.5, places=2)
        self.assertLessEqual(rec["mae_r"], -0.99)                  # the exit price itself counts as an excursion
        self.assertAlmostEqual(rec["hold_h"], 1.02, places=2)
        self.assertIn("score", rec["entry_ctx"])
        self.assertIn("↳ en iyi +0.5R", self.messages())
        self.assertIn("bir miktar lehine gitti", self.messages())
        self.assertEqual([json.loads(l)["id"] for l in self.journal.path.read_text().splitlines()], [rec["id"]])

    def test_winner_and_short_are_journaled(self):
        t = self.open("XRPUSDT")                                   # SHORT in the fixture
        self.ex.move("XRPUSDT", t["take_profit"] * 0.999)
        self.executor.manage()
        (rec,) = self.journal.records
        self.assertEqual((rec["side"], rec["kind"]), ("SHORT", "win"))
        self.assertGreaterEqual(rec["mfe_r"], 1.9)
        self.assertIn("hedefe/kâra ulaştı", self.messages())

    def test_failing_journal_never_blocks_closing(self):
        class Broken(jr.Journal):
            def add(self, record):
                raise RuntimeError("disk on fire")
        self.executor.journal = Broken(Path(self.dir.name) / "x.jsonl")
        t = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", t["stop"] * 0.999)
        self.executor.manage()
        self.assertEqual(self.state["trades"], {})
        self.assertEqual(self.state["history"][-1]["reason"], "STOP_LOSS")
        self.assertIn("DOGEUSDT LONG kapandı", self.messages())
        self.assertNotIn("↳", self.messages())

    def test_missing_pnl_is_not_journaled_as_a_result(self):
        self.open("DOGEUSDT")
        self.ex.pos.clear()
        for _ in range(4):
            self.executor.manage()
        self.assertIsNone(self.journal.records[0]["r"])
        self.assertEqual(self.journal.records[0]["kind"], "unknown")
        self.assertNotIn("↳", self.messages())

    def test_without_a_journal_nothing_changes(self):
        self.executor.journal = None
        t = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", t["take_profit"] * 1.001)
        self.executor.manage()
        self.assertNotIn("↳", self.messages())

    def test_trade_opened_by_an_older_version_still_closes(self):
        t = self.open("DOGEUSDT")
        for key in ("snap", "mfe_r", "mae_r"):
            t.pop(key, None)
        self.ex.move("DOGEUSDT", t["stop"] * 0.999)
        self.executor.manage()
        self.assertIsNone(self.journal.records[0]["entry_ctx"])

    def test_analiz_command_and_followup_through_the_bot(self):
        bot = Bot(self.settings, self.demo, "demo", None, self.notifier, self.state, self.executor, self.ex, None, self.clock,
                  sleep=lambda s: None, journal=self.journal, journal_market=object())
        bot.start()
        self.assertIn("Henüz", bot._answer("/analiz"))
        self.assertIn("/analiz", bot._answer("/help"))
        t = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", t["stop"] * 0.999)
        self.executor.manage()
        reply = bot._answer("/analiz")
        self.assertIn("1 işlem", reply)
        self.assertEqual(bot._answer("/gunluk"), reply)
        self.assertEqual(bot._answer("/journal"), reply)

        calls = []
        self.journal.followups = lambda market: calls.append(market)     # wiring only; logic is tested above
        self.clock.now += 400
        bot.tick()
        self.assertEqual(len(calls), 1)

        def boom(market):
            raise RuntimeError("x")
        self.journal.followups = boom
        self.clock.now += 400
        bot.tick()                                                    # must not raise or stop trading

    def test_command_without_journal(self):
        bot = Bot(self.settings, self.demo, "demo", None, self.notifier, self.state, None, self.ex, None, self.clock, sleep=lambda s: None)
        self.assertIn("kapalı", bot._answer("/analiz"))


if __name__ == "__main__":
    unittest.main()
