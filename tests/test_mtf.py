import tempfile
import unittest
from pathlib import Path
from unittest import mock
import numpy as np
import pandas as pd
from crypto_watcher import mtf
from crypto_watcher import signals as sg
from crypto_watcher.config import Settings
from helpers import Clock

T0 = 1_790_000_000_000


def frame(minutes: int, n: int, closes=None, start=T0):
    step = minutes * 60_000
    opens = start + np.arange(n) * step
    close = np.asarray(closes if closes is not None else 100 + np.arange(n) * 0.01, float)
    return pd.DataFrame({"close": close, "close_time": opens + step - 1}, index=pd.to_datetime(opens, unit="ms", utc=True))


class RuleTests(unittest.TestCase):
    def test_bar_score(self):
        self.assertEqual(mtf.bar_score(0.6, 0.6, 0.6, 0.6), 8)            # aligned long
        self.assertEqual(mtf.bar_score(-0.6, -0.6, -0.6, 0.6), -8)        # aligned short
        self.assertEqual(mtf.bar_score(0.1, 0.6, 0.9, 0.6), 5)            # early long: higher timeframes turned, 5m not yet
        self.assertEqual(mtf.bar_score(-0.1, -0.6, -0.9, 0.6), -5)
        self.assertEqual(mtf.bar_score(0.6, 0.6, 0.6, 0.5), 0)            # Hurst must be ABOVE 0.5
        self.assertEqual(mtf.bar_score(0.6, 0.6, 0.6, float("nan")), 0)
        self.assertEqual(mtf.bar_score(float("nan"), 0.6, 0.6, 0.6), 0)
        self.assertEqual(mtf.bar_score(0.6, 0.4, 0.6, 0.6), 0)            # one timeframe short of the threshold
        self.assertEqual(mtf.bar_score(0.4, 0.6, 0.9, 0.6), 0)            # 5m neither aligned (>0.5) nor quiet (<0.3)

    def test_momentum_is_scale_free_and_reacts_to_acceleration(self):
        rng = np.random.default_rng(1)
        base = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 400)))
        up = base.copy(); up[-15:] *= np.exp(np.cumsum(np.full(15, 0.004)))
        m = mtf.momentum(pd.Series(up))
        self.assertGreater(m.iloc[-1], 1.0)
        pd.testing.assert_series_equal(mtf.momentum(pd.Series(up * 37)), m)   # price level does not matter

    def test_higher_timeframe_is_read_only_after_its_bar_closed(self):
        h1 = frame(60, 150)
        with mock.patch.object(mtf, "momentum", lambda close: pd.Series(np.arange(len(close), dtype=float), index=close.index)):
            first_end = int(h1.close_time.iloc[0]) + 1
            got = mtf.on_grid(h1, np.array([first_end - 1, first_end, first_end + 3_599_999, first_end + 3_600_000]))
        self.assertTrue(np.isnan(got[0]))                                 # nothing closed yet
        self.assertEqual(list(got[1:]), [0.0, 0.0, 1.0])                  # bar 1 only once it has ended

    def test_hurst(self):
        rng = np.random.default_rng(2)
        iid = rng.normal(0, 1, 200)
        h = mtf.hurst_rs(iid)
        self.assertTrue(0.35 < h < 0.75, h)
        ar = np.zeros(200)
        for i in range(1, 200):
            ar[i] = 0.8 * ar[i - 1] + rng.normal()
        self.assertGreater(mtf.hurst_rs(ar), h)                            # persistent returns -> higher H
        self.assertTrue(np.isnan(mtf.hurst_rs(iid[:20])))
        self.assertTrue(np.isnan(mtf.hurst_rs(np.r_[iid, np.nan])))


class ConfirmationTests(unittest.TestCase):
    def frames(self):
        return frame(5, 400), frame(60, 300), frame(240, 300)

    def run_with(self, values, hurst=0.6):
        """`values`: momentum per timeframe, chosen by bar spacing (5m / 1h / 4h)."""
        def fake(close):
            spacing = (close.index[1] - close.index[0]).total_seconds() if len(close) > 1 else 300
            return pd.Series(values[{300: "5m", 3600: "1h", 14400: "4h"}[spacing]], index=close.index)
        with mock.patch.object(mtf, "momentum", fake):
            return mtf.confirmation(*self.frames(), hurst_fn=lambda r: hurst)

    def test_directions(self):
        self.assertEqual(self.run_with({"5m": 0.7, "1h": 0.7, "4h": 0.7})["dir"], 1)
        self.assertEqual(self.run_with({"5m": -0.7, "1h": -0.7, "4h": -0.7})["dir"], -1)
        self.assertEqual(self.run_with({"5m": 0.1, "1h": 0.6, "4h": 0.9})["dir"], 1)        # early long
        self.assertEqual(self.run_with({"5m": 0.7, "1h": -0.7, "4h": 0.7})["dir"], 0)       # conflicting timeframes
        self.assertEqual(self.run_with({"5m": 0.7, "1h": 0.7, "4h": 0.7}, hurst=0.45)["dir"], 0)

    def test_values_are_reported(self):
        out = self.run_with({"5m": 0.7, "1h": 0.6, "4h": 0.9})
        self.assertEqual((out["m5"], out["m1h"], out["m4h"], out["hurst"]), (0.7, 0.6, 0.9, 0.6))

    def test_too_little_history_is_an_error(self):
        with self.assertRaises(ValueError):
            mtf.confirmation(frame(5, 150), frame(60, 300), frame(240, 300))

    def test_real_computation_runs_end_to_end(self):
        rng = np.random.default_rng(5)
        px = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.003, 400 * 48)))
        f5 = frame(5, len(px), px)
        h1 = frame(60, len(px) // 12, px[11::12])
        h4 = frame(240, len(px) // 48, px[47::48])
        out = mtf.confirmation(f5.iloc[-400:], h1.iloc[-300:], h4.iloc[-300:])
        self.assertIn(out["dir"], (-1, 0, 1))
        self.assertTrue(all(out[k] is None or np.isfinite(out[k]) for k in ("m5", "m1h", "m4h", "hurst")))


class ClusterDiffTests(unittest.TestCase):
    def test_difference_and_its_sign(self):
        values = [1.0, 1.2, 0.8, 1.1, 0.9, 0.0, 0.1, -0.1, 0.05, -0.05, 1.0, 0.0]
        groups = [True] * 5 + [False] * 5 + [True, False]
        days = [f"d{i % 6}" for i in range(12)]
        diff, t = mtf.cluster_diff(values, groups, days)
        self.assertAlmostEqual(diff, np.mean([1.0, 1.2, 0.8, 1.1, 0.9, 1.0]) - np.mean([0.0, 0.1, -0.1, 0.05, -0.05, 0.0]))
        self.assertGreater(t, 3)

    def test_undefined_cases(self):
        self.assertTrue(np.isnan(mtf.cluster_diff([1.0] * 12, [True] * 12, ["d"] * 12)[0]))
        self.assertTrue(np.isnan(mtf.cluster_diff([1.0, 2.0], [True, False], ["a", "b"])[0]))


def candidate(symbol="AAAUSDT", side="LONG", date="2026-10-08 10:00"):
    return {"symbol": symbol, "side": side, "date": date, "score": 75.0, "market_regime": "BULL",
            "sides": {side: {"risk_plan": {"entry": 100.0, "risk_distance": 2.0}}}}


class SignalLogMtfTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "signals.jsonl"
        self.clock = Clock(1_791_000_000.0)
        self.log = sg.SignalLog(self.path, None, Settings(), self.clock)

    def test_confirmation_is_recorded_once_and_survives_a_restart(self):
        calls = []
        confirm = lambda: calls.append(1) or {"dir": 1, "m5": 0.7, "m1h": 0.6, "m4h": 0.9, "hurst": 0.6}
        for _ in range(3):
            self.log.note(candidate(), "yön limiti", confirm=confirm)
        self.assertEqual(len(calls), 1)                                  # repeated scans of the same candle: computed once
        again = sg.SignalLog(self.path, None, Settings(), self.clock)
        self.assertEqual(again.signals["AAAUSDT-2026-10-08 10:00"]["mtf"]["dir"], 1)

    def test_a_failing_confirmation_never_loses_the_signal(self):
        def boom():
            raise ConnectionError("no 5m data")
        self.log.note(candidate(), "yön limiti", confirm=boom)
        rec = self.log.signals["AAAUSDT-2026-10-08 10:00"]
        self.assertIn("no 5m data", rec["mtf"]["error"])
        self.assertEqual(rec["status"], "yön limiti")

    def fill(self, days, confirmed_r, other_r):
        """Two signals per day for `days` days: one confirmed by MTF, one not; already replayed."""
        for i in range(days):
            for j, (direction, r) in enumerate(((1, confirmed_r(i)), (-1, other_r(i)))):
                key = f"S{i}-{j}"
                self.log.signals[key] = {"id": key, "symbol": "AAAUSDT", "side": "LONG", "status": "yön limiti",
                                         "seen_ms": 1_791_000_000_000 + i * 86_400_000 + j, "mtf": {"dir": direction}}
                self.log.results[key] = {"id": key, "net_r": r, "exit": "SL"}

    def test_summary_is_early_before_90_days(self):
        self.fill(30, lambda i: 0.5, lambda i: -0.5)
        text = self.log.format_summary()
        self.assertIn("MTF onayı (gölge", text)
        self.assertIn("onaylı: 30 sinyal · ortalama +0.50R", text)
        self.assertIn("onaysız: 30 sinyal · ortalama -0.50R", text)
        self.assertIn("henüz erken (29/90 gün)", text)

    def test_rule_passes_only_with_a_consistent_significant_edge(self):
        self.fill(100, lambda i: 0.4 + 0.3 * ((i * 7) % 5 - 2) / 2, lambda i: -0.2 + 0.3 * ((i * 3) % 5 - 2) / 2)
        self.assertIn("Karar: GEÇTİ", self.log.format_summary())

    def test_rule_fails_when_the_edge_flips_between_halves(self):
        self.fill(100, lambda i: 0.6 if i < 50 else -0.9, lambda i: 0.0)
        self.assertIn("Karar: GEÇMEDİ", self.log.format_summary())

    def test_a_strong_total_still_fails_if_one_half_is_negative(self):
        wobble = lambda i: 0.05 * ((i * 7) % 5 - 2)
        self.fill(100, lambda i: (1.5 if i < 50 else -0.1) + wobble(i), lambda i: wobble(i * 3))
        diff, t = mtf.cluster_diff([r["net_r"] for r in self.log.rows()],
                                   [r["mtf"]["dir"] == 1 for r in self.log.rows()], [str(r["seen_ms"] // 86_400_000) for r in self.log.rows()])
        self.assertGreater(t, 1.65)                                       # the pooled test alone would pass...
        self.assertIn("Karar: GEÇMEDİ", self.log.format_summary())        # ...but the second half is negative

    def test_signals_without_a_rating_are_ignored(self):
        self.log.signals["X"] = {"id": "X", "symbol": "AAAUSDT", "side": "LONG", "status": "yön limiti", "seen_ms": 1_791_000_000_000,
                                 "mtf": {"error": "x"}}
        self.log.results["X"] = {"id": "X", "net_r": 1.0, "exit": "TP"}
        self.assertNotIn("MTF onayı", self.log.format_summary())


if __name__ == "__main__":
    unittest.main()
