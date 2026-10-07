import contextlib
import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd
from crypto_watcher.backtest import (BacktestConfig, backtest_symbol, default_dates, ensure_files, fetch_plan, format_report,
                                     load_frame, read_klines_zip, run_backtest, settle, simulate_trade, to_ms)
from crypto_watcher.cli import main
from crypto_watcher.config import Settings

S = Settings()                      # stop 2 ATR, target 2R, breakeven 1R, 48h time stop
H, L, C = np.array, np.array, np.array


def candles(*rows):
    """rows of (high, low, close)."""
    h, l, c = zip(*rows)
    return np.array(h, float), np.array(l, float), np.array(c, float)


class SimulateTradeTests(unittest.TestCase):
    # entry 100, distance 2 -> long stop 98, target 104
    def run_long(self, rows, side="LONG"):
        h, l, c = candles(*rows)
        return simulate_trade(side, 100.0, 2.0, h, l, c, 0, S, 48)

    def test_target_and_stop(self):
        self.assertEqual(self.run_long([(101, 99.5, 100), (104.5, 100, 104)]), (104.0, "TAKE_PROFIT", 2))
        self.assertEqual(self.run_long([(101, 97.5, 98)]), (98.0, "STOP_LOSS", 1))

    def test_stop_wins_when_both_levels_are_inside_one_candle(self):
        self.assertEqual(self.run_long([(105, 97, 100)])[1], "STOP_LOSS")

    def test_breakeven_arms_after_one_r_and_applies_from_the_next_candle(self):
        be = 100 * (1 + 2 * S.taker_fee)
        # +1R reached in candle 0 (high 102); candle 1 dips to 99.9 -> breakeven stop (above 98) hit
        exit_price, reason, held = self.run_long([(102, 99.5, 101), (101, 99.9, 100)])
        self.assertEqual((reason, held), ("BREAKEVEN_STOP", 2))
        self.assertAlmostEqual(exit_price, be)
        # same candle reaching +1R and dipping to 99.5 does NOT stop at breakeven (only from the next candle)
        self.assertEqual(self.run_long([(102, 99.5, 101), (103, 101, 102.5)])[1:], ("END", 2))

    def test_time_stop(self):
        h, l, c = candles(*[(101, 99.5, 100.5)] * 5)
        self.assertEqual(simulate_trade("LONG", 100.0, 2.0, h, l, c, 0, S, 3), (100.5, "TIME_STOP", 3))

    def test_short_is_the_mirror_image(self):
        # short entry 100: stop 102, target 96
        h, l, c = candles((100.5, 95.5, 96))
        self.assertEqual(simulate_trade("SHORT", 100.0, 2.0, h, l, c, 0, S, 48), (96.0, "TAKE_PROFIT", 1))
        h, l, c = candles((102.5, 99, 102))
        self.assertEqual(simulate_trade("SHORT", 100.0, 2.0, h, l, c, 0, S, 48), (102.0, "STOP_LOSS", 1))


class SettleTests(unittest.TestCase):
    """Hand-calculated: fee 0.05% and slippage 0.02% per side, stop distance 2 on a price of 100."""
    CFG = BacktestConfig(start=date(2026, 1, 1), end=date(2026, 2, 1), split=date(2026, 1, 20), fee=0.0005, slippage=0.0002)

    def test_long_winner_at_target(self):
        entry = 100 * 1.0002                                     # 100.02
        net, cost, fill = settle("LONG", 100.0, entry, 104.02, 2.0, self.CFG)   # target = entry + 2R
        self.assertAlmostEqual(fill, 104.02 * 0.9998)
        fees = 0.0005 * (100.02 + 104.02 * 0.9998)               # 0.1020096
        self.assertAlmostEqual(net, ((104.02 * 0.9998 - 100.02) - fees) / 2.0)
        self.assertAlmostEqual(net, 1.93859, places=4)
        self.assertAlmostEqual(cost, (fees + 0.0002 * (100.0 + 104.02 * 0.9998)) / 2.0)
        self.assertAlmostEqual(net + cost, (104.02 - 100.0) / 2.0, places=4)   # gross = raw price move in R

    def test_short_loser_at_stop(self):
        entry = 100 * 0.9998                                     # 99.98
        net, cost, fill = settle("SHORT", 100.0, entry, 101.98, 2.0, self.CFG)  # stop = entry + 1R
        self.assertAlmostEqual(fill, 101.98 * 1.0002)
        fees = 0.0005 * (99.98 + 101.98 * 1.0002)
        self.assertAlmostEqual(net, ((99.98 - 101.98 * 1.0002) - fees) / 2.0)
        self.assertLess(net, -1.0)                               # a stop loses MORE than 1R once costs are added
        self.assertAlmostEqual(net + cost, (100.0 - 101.98) / 2.0, places=4)

    def test_no_costs_no_difference(self):
        free = BacktestConfig(start=date(2026, 1, 1), end=date(2026, 2, 1), split=date(2026, 1, 20), fee=0.0, slippage=0.0)
        net, cost, _ = settle("LONG", 100.0, 100.0, 104.0, 2.0, free)
        self.assertEqual((net, cost), (2.0, 0.0))

    def test_costs_scale_with_stop_distance(self):
        wide, _, _ = settle("LONG", 100.0, 100.02, 100.02, 4.0, self.CFG)
        tight, _, _ = settle("LONG", 100.0, 100.02, 100.02, 1.0, self.CFG)
        self.assertAlmostEqual(tight / wide, 4.0, places=6)      # same money lost, measured against a 4x smaller risk


class ParsingTests(unittest.TestCase):
    ROW = "1767225600000,100,101,99,100.5,10,1767229199999,1005,50,6,603,0"

    def zip_with(self, tmp, members, name="a.zip"):
        path = Path(tmp) / name
        with zipfile.ZipFile(path, "w") as z:
            for member, text in members.items():
                z.writestr(member, text)
        return path

    def test_reads_with_and_without_header_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
            for text in (self.ROW + "\n", header + self.ROW + "\n"):
                df = read_klines_zip(self.zip_with(tmp, {"x.csv": text}))
                self.assertEqual(len(df), 1)
                self.assertEqual(df.close.iloc[0], 100.5)

    def test_rejects_hostile_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            for members in ({"../evil.csv": self.ROW}, {"sub/x.csv": self.ROW}, {"x.exe": self.ROW},
                            {"a.csv": self.ROW, "b.csv": self.ROW}):
                with self.subTest(members=list(members)), self.assertRaises(ValueError):
                    read_klines_zip(self.zip_with(tmp, members))
            from crypto_watcher import backtest
            original, backtest.MAX_CSV_BYTES = backtest.MAX_CSV_BYTES, 10
            try:
                with self.assertRaisesRegex(ValueError, "too large"):
                    read_klines_zip(self.zip_with(tmp, {"x.csv": self.ROW}))
            finally:
                backtest.MAX_CSV_BYTES = original

    def test_to_ms_is_independent_of_index_resolution(self):
        for unit in ("s", "ms", "us", "ns"):
            index = pd.DatetimeIndex(pd.to_datetime([1_767_225_600, 1_767_229_200], unit="s", utc=True)).as_unit(unit)
            self.assertEqual(list(to_ms(index)), [1_767_225_600_000, 1_767_229_200_000])

    def test_load_frame_rejects_bad_data_and_counts_gaps(self):
        base = pd.DataFrame({"open_time": [0, 3_600_000, 3 * 3_600_000], "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                             "volume": 1.0, "close_time": [3_599_999, 7_199_999, 14_399_999], "quote_volume": 1.0,
                             "taker_buy_base": 0.5, "trades": 1, "taker_buy_quote": 1.0, "ignore": 0})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.zip"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("a.csv", base.to_csv(index=False, header=False))
            since, until = pd.Timestamp("1970-01-01", tz="UTC"), pd.Timestamp("1970-01-02", tz="UTC")
            self.assertEqual(load_frame([path], "1h", since, until).attrs["gaps"], 1)
            bad = base.assign(close=-1.0)
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("a.csv", bad.to_csv(index=False, header=False))
            with self.assertRaises(ValueError):
                load_frame([path], "1h", since, until)


class DownloadTests(unittest.TestCase):
    class Session:
        def __init__(self, available):
            self.available, self.requested = available, []

        def get(self, url, timeout=None):
            self.requested.append(url)
            ok = any(url.endswith(a) for a in self.available)
            return type("R", (), {"status_code": 200 if ok else 404, "content": b"PK-fake",
                                  "raise_for_status": lambda self: None})()

    def test_fetch_plan_uses_monthly_for_complete_months_and_days_for_the_running_one(self):
        plan = fetch_plan(date(2026, 1, 20), date(2026, 3, 10))
        self.assertEqual([(m, complete) for m, complete, _ in plan], [("2026-01", True), ("2026-02", True), ("2026-03", False)])
        self.assertEqual(plan[-1][2][-1], date(2026, 3, 10))
        self.assertEqual(len(plan[-1][2]), 10)

    def test_monthly_download_cache_and_daily_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            plan = fetch_plan(date(2026, 1, 1), date(2026, 2, 3))
            # January monthly exists; February monthly missing -> daily files for Feb 1..3 are used
            session = self.Session(["BTCUSDT-1h-2026-01.zip", "BTCUSDT-1h-2026-02-01.zip", "BTCUSDT-1h-2026-02-02.zip"])
            paths = ensure_files(session, cache, "BTCUSDT", "1h", plan)
            self.assertEqual([p.name for p in paths], ["BTCUSDT-1h-2026-01.zip", "BTCUSDT-1h-2026-02-01.zip", "BTCUSDT-1h-2026-02-02.zip"])
            first_round = len(session.requested)
            ensure_files(session, cache, "BTCUSDT", "1h", plan)       # second run: only the files that 404'd are retried
            self.assertLess(len(session.requested) - first_round, first_round)
            self.assertFalse(list(cache.rglob("*.part")))             # no half-written files left behind

    def test_default_dates(self):
        start, end = default_dates(date(2026, 10, 7), 180)
        self.assertEqual(end, date(2026, 10, 6))
        self.assertEqual((end - start).days, 180)


def make_cache(root: Path, seed=7):
    """Synthetic BTC/ETH with trending segments: monthly zips for Dec 2025 - Mar 2026, plus daily zips for March."""
    rng = np.random.default_rng(seed)
    times = pd.date_range("2025-12-01", "2026-03-31 23:00", freq="h", tz="UTC")
    n = len(times)
    drift = np.repeat(rng.choice([-0.0025, 0.0, 0.0025], size=n // 120 + 1), 120)[:n]
    common = rng.normal(0, 0.005, n)
    for symbol, start_price, own in (("BTCUSDT", 60_000.0, 0.0), ("ETHUSDT", 3_000.0, 0.004)):
        close = start_price * np.exp(np.cumsum(drift + common + rng.normal(0, own, n)))
        open_ = np.concatenate([[start_price], close[:-1]])
        wiggle = np.abs(rng.normal(0.003, 0.001, n))
        volume = rng.uniform(800, 1200, n) * (1 + 40 * np.abs(np.diff(np.log(np.concatenate([[start_price], close])))))
        h1 = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + wiggle), "low": np.minimum(open_, close) * (1 - wiggle),
                           "close": close, "volume": volume}, index=times)
        h1["quote"] = h1.volume * h1.close
        h1["taker"] = h1.volume * np.clip(0.5 + np.sign(h1.close - h1.open) * 0.1, 0, 1)
        g = h1.resample("4h", origin="epoch")
        h4 = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(),
                           "volume": g.volume.sum(), "quote": g.quote.sum(), "taker": g.taker.sum()})
        for interval, frame, step in (("1h", h1, 3_600_000), ("4h", h4, 14_400_000)):
            ms = (frame.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)
            raw = pd.DataFrame({"t": ms, "o": frame.open, "h": frame.high, "l": frame.low, "c": frame.close, "v": frame.volume,
                                "ct": ms + step - 1, "q": frame["quote"], "n": 1, "tb": frame["taker"],
                                "tq": 1.0, "i": 0}).reset_index(drop=True)
            groups = [(f"monthly/{symbol}-{interval}-{key}", g_) for key, g_ in raw.groupby(frame.index.strftime("%Y-%m"))]
            groups += [(f"daily/{symbol}-{interval}-{key}", g_) for key, g_ in raw[frame.index.month == 3].groupby(frame.index[frame.index.month == 3].strftime("%Y-%m-%d"))]
            for name, g_ in groups:
                path = root / f"{name}.zip"
                path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(path, "w") as z:
                    z.writestr(Path(name).name + ".csv", g_.to_csv(index=False, header=False))


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("the backtest touched the network although every file is cached")


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cache = Path(cls.tmp.name)
        make_cache(cls.cache)
        cls.cfg = BacktestConfig(start=date(2026, 3, 1), end=date(2026, 3, 31), split=date(2026, 3, 20))
        cls.trades, cls.skipped = run_backtest(S, cls.cfg, ["ETHUSDT"], cls.cache, session=NoNetwork(), progress=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_produces_consistent_trades(self):
        t = self.trades
        self.assertGreater(len(t), 5)
        self.assertEqual(self.skipped, [])
        self.assertEqual(set(t.symbol), {"BTCUSDT", "ETHUSDT"})
        self.assertTrue(np.isfinite(t[["R", "inv_R", "cost_R", "atr_pct"]].to_numpy()).all())
        self.assertGreaterEqual(t.ts.min(), pd.Timestamp("2026-03-01", tz="UTC"))
        self.assertLess(t.ts.max(), pd.Timestamp("2026-04-01", tz="UTC"))
        self.assertTrue((t.cost_R > 0).all())
        self.assertTrue(set(t.reason) <= {"STOP_LOSS", "TAKE_PROFIT", "BREAKEVEN_STOP", "TIME_STOP", "END"})
        self.assertLessEqual(t.R.max(), S.tp_r)                          # costs only ever reduce a winner
        # one position per symbol at a time and the cooldown is respected
        for _, g in t.groupby("symbol"):
            g = g.reset_index(drop=True)
            free = g.ts + pd.to_timedelta(g.held, unit="h") + pd.Timedelta(minutes=S.cooldown_minutes) - pd.Timedelta(hours=1)
            self.assertTrue((g.ts.iloc[1:].reset_index(drop=True) >= free.iloc[:-1]).all())

    def test_costs_are_charged_and_inverse_is_a_distinct_simulation(self):
        t = self.trades
        self.assertGreater(t.cost_R.min(), 0)
        self.assertGreater(t.inv_cost_R.min(), 0)
        gross = t.R + t.cost_R
        # the target is measured from the slipped entry (as live), so raw price can pass 2R by slippage/(ATR stop)
        self.assertTrue(((gross - S.tp_r) <= self.cfg.slippage / (t.atr_pct * S.atr_stop_multiplier) + 1e-4).all())   # +1e-4: slippage^2 terms
        self.assertFalse(np.allclose(t.R, -t.inv_R))                 # not just the mirrored number
        self.assertTrue((t.cost_R < 1.0).all())                      # costs are a fraction of the 2-ATR stop

    def test_no_lookahead_signals_do_not_change_when_the_future_is_removed(self):
        short_cfg = BacktestConfig(start=date(2026, 3, 1), end=date(2026, 3, 20), split=date(2026, 3, 10))
        short, _ = run_backtest(S, short_cfg, ["ETHUSDT"], self.cache, session=NoNetwork(), progress=lambda *_: None)
        cut = pd.Timestamp("2026-03-16", tz="UTC")       # leave room for trades still open at the truncated end
        key = ["symbol", "ts", "side", "score"]
        a = self.trades[self.trades.ts < cut][key].reset_index(drop=True)
        b = short[short.ts < cut][key].reset_index(drop=True)
        self.assertGreater(len(a), 0)
        pd.testing.assert_frame_equal(a, b)

    def test_report_contains_the_decision_relevant_numbers(self):
        report = format_report(self.trades, self.cfg, S, 2, ["XYZUSDT: no data files"])
        for expected in ("TÜMÜ", "eğitim dönemi", "test dönemi", "yön LONG", "BTC rejimi", "ay 2026-03", "skor ", "TERSİ R",
                         "Çıkış sebepleri", "Atlanan semboller", "XYZUSDT", "Bilinmeyenler"):
            self.assertIn(expected, report)
        self.assertIn("Bu dönemde hiç sinyal", format_report(pd.DataFrame(), self.cfg, S, 0, []))

    def test_worker_pool_gives_the_same_trades(self):
        parallel, _ = run_backtest(S, self.cfg, ["ETHUSDT"], self.cache, workers=2, session=NoNetwork(), progress=lambda *_: None)
        key = ["symbol", "ts", "side", "R"]
        pd.testing.assert_frame_equal(self.trades.sort_values(["symbol", "ts"])[key].reset_index(drop=True),
                                      parallel.sort_values(["symbol", "ts"])[key].reset_index(drop=True))

    def test_missing_symbol_is_skipped_not_fatal(self):
        class Empty:
            def get(self, url, timeout=None):
                return type("R", (), {"status_code": 404, "content": b""})()
        trades, skipped = run_backtest(S, self.cfg, ["NEWUSDT"], self.cache, session=Empty(), progress=lambda *_: None)
        self.assertTrue(any(s.startswith("NEWUSDT") for s in skipped))
        self.assertGreater(len(trades), 0)

    def test_cli_runs_offline_from_the_cache_and_writes_csv(self):
        out = Path(self.tmp.name) / "out" / "trades.csv"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["backtest", "--start", "2026-03-01", "--end", "2026-03-31", "--split", "2026-03-20",
                         "--symbols", "BTCUSDT,ETHUSDT", "--data-dir", str(self.cache), "--output", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("TÜMÜ", buffer.getvalue())
        self.assertEqual(len(pd.read_csv(out)), len(self.trades))

    def test_cli_rejects_bad_dates(self):
        for args in (["--start", "2026-03-01", "--end", "2026-03-05"],
                     ["--start", "2026-03-01", "--end", "2026-03-31", "--split", "2026-04-15"]):
            err = io.StringIO()
            with self.subTest(args=args), contextlib.redirect_stderr(err):
                self.assertEqual(main(["backtest", "--data-dir", str(self.cache), *args]), 2)


if __name__ == "__main__":
    unittest.main()
