import json
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from crypto_watcher import model_shadow as ms
from crypto_watcher.data import parse_klines
from crypto_watcher.model_shadow import HOUR_MS, ModelShadow

T0 = int(pd.Timestamp("2026-10-01 00:00", tz="UTC").timestamp() * 1000)


def synthetic_rows(n: int, seed: int, start_ms: int, drift: float = 0.0, vol: float = 0.006, trend: float = 0.0):
    """Raw Binance-style kline rows (all candles closed long ago)."""
    rng = np.random.default_rng(seed)
    ret = rng.normal(drift, vol, n)
    if trend:       # a persistent, learnable component: momentum regimes that flip every ~200 candles
        regime = np.sign(np.sin(np.arange(n) / 200 * np.pi))
        ret = ret + trend * regime
    close = 100 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[100.0], close[:-1]])
    high, low = np.maximum(open_, close) * 1.002, np.minimum(open_, close) * 0.998
    volume = rng.uniform(900, 1100, n)
    taker = volume * np.clip(0.5 + np.sign(ret) * 0.1, 0.05, 0.95)
    rows = []
    for i in range(n):
        t = start_ms + i * HOUR_MS
        rows.append([t, f"{open_[i]:.6f}", f"{high[i]:.6f}", f"{low[i]:.6f}", f"{close[i]:.6f}", f"{volume[i]:.3f}",
                     t + HOUR_MS - 1, f"{volume[i] * close[i]:.3f}", 100, f"{taker[i]:.3f}", "0", "0"])
    return rows


def bars(n=900, seed=1, **kw):
    return parse_klines(synthetic_rows(n, seed, T0, **kw), T0 + (n + 5) * HOUR_MS)


class FeatureTests(unittest.TestCase):
    def test_features_do_not_look_ahead(self):
        base = bars(600, seed=3)
        btc = bars(600, seed=4)
        full = ms.prepare(base, btc)
        cut = 400
        part = ms.prepare(base.iloc[:cut], btc.iloc[:cut])
        # changing the future must not change any feature up to the cut
        altered = base.copy()
        altered.iloc[cut:, altered.columns.get_loc("close")] *= 3
        again = ms.prepare(altered.iloc[:cut], btc.iloc[:cut])
        pd.testing.assert_frame_equal(part[ms.FEATURES], again[ms.FEATURES])
        pd.testing.assert_frame_equal(full.iloc[:cut][ms.FEATURES].tail(100), part[ms.FEATURES].tail(100), check_exact=False, atol=1e-9)

    def test_label_uses_next_open_to_open_h(self):
        data = bars(300, seed=5)
        frame = ms.prepare(data, None)
        t = 100
        expected = np.log(data.open.iloc[t + 1 + ms.HORIZON] / data.open.iloc[t + 1]) * 1e4
        self.assertAlmostEqual(frame.fwd_bps.iloc[t], expected)
        self.assertTrue(np.isnan(frame.fwd_bps.iloc[-1]))
        self.assertTrue(np.isnan(frame.y.iloc[-1]))
        self.assertTrue(set(frame.y.dropna().unique()) <= {0.0, 1.0, 2.0})

    def test_label_classes_follow_the_volatility_band(self):
        opens = pd.Series(100.0, index=pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC"))
        opens.iloc[1 + ms.HORIZON:] = 110.0          # +10% between entry and exit for early rows
        atr_pct = pd.Series(0.005, index=opens.index)        # band = 0.5 * 0.005 * sqrt(24) = 1.2%
        y = ms.make_labels(opens, atr_pct)
        self.assertEqual(y.iloc[0], 2.0)
        opens2 = pd.Series(100.0, index=opens.index)
        self.assertEqual(ms.make_labels(opens2, atr_pct).iloc[0], 1.0)
        opens3 = opens2.copy()
        opens3.iloc[1 + ms.HORIZON:] = 90.0
        self.assertEqual(ms.make_labels(opens3, atr_pct).iloc[0], 0.0)

    def test_training_set_is_purged(self):
        frame = ms.prepare(bars(700, seed=6), None)
        cutoff = frame.index[500]
        X, y = ms.training_set({"X": frame}, cutoff)
        # the last usable row's label window must end at or before the cutoff
        last = X.index.max()
        self.assertLessEqual(last + pd.Timedelta(hours=ms.HORIZON + 1), cutoff)
        self.assertGreater(len(X), 100)


class DecisionTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(ms.decide(0.20, 0.30, 0.50), "LONG")
        self.assertEqual(ms.decide(0.50, 0.30, 0.20), "SHORT")
        self.assertIsNone(ms.decide(0.33, 0.33, 0.34))          # chance level
        self.assertIsNone(ms.decide(0.40, 0.18, 0.42))          # confident but not separated enough
        self.assertIsNone(ms.decide(0.20, 0.45, 0.35))          # flat favoured, up below P_MIN

    def test_probabilities_follow_class_order(self):
        class Fake:
            classes_ = np.array([0, 2])             # class 1 never seen -> column must still be zero-filled
            def predict_proba(self, X):
                return np.array([[0.3, 0.7]] * len(X))
        out = ms.probabilities(Fake(), pd.DataFrame({"a": [1.0, 2.0]}))
        np.testing.assert_allclose(out[0], [0.3, 0.0, 0.7])


class AnalysisTests(unittest.TestCase):
    def rows(self, n, gross, side=1, fund=0.0, symbol_cycle=("A", "B")):
        out = []
        for i in range(n):
            out.append({"symbol": symbol_cycle[i % len(symbol_cycle)], "hour": T0 + i * 30 * HOUR_MS, "side": side,
                        "gross": gross, "fund": fund, "ret24": 0.01})
        return out

    def test_short_gets_funding_and_costs_apply(self):
        stats = ms.analyse(self.rows(40, gross=-100.0, side=-1, fund=-10.0))
        # short: -(−100 + −10) − 14 = +96 bp
        self.assertAlmostEqual(stats["mean"], 96.0)
        self.assertEqual(stats["long_share"], 0.0)
        self.assertAlmostEqual(stats["baselines"]["always_long"], -110.0 - 14.0)
        self.assertAlmostEqual(stats["baselines"]["inverse"], -110.0 - 14.0)      # the inverse of a short is a long

    def test_baseline_semantics(self):
        stats = ms.analyse(self.rows(40, gross=50.0, side=1))
        self.assertAlmostEqual(stats["mean"], 50.0 - ms.COST_BPS)
        self.assertAlmostEqual(stats["baselines"]["inverse"], -50.0 - ms.COST_BPS)
        self.assertAlmostEqual(stats["baselines"]["momentum"], 50.0 - ms.COST_BPS)       # ret24 > 0 -> long
        self.assertAlmostEqual(stats["baselines"]["always_long"], 50.0 - ms.COST_BPS)

    def test_overlapping_events_collapse_per_symbol(self):
        rows = [{"symbol": "A", "hour": T0 + i * HOUR_MS, "side": 1, "gross": 1.0, "fund": 0.0, "ret24": 0.0} for i in range(30)]
        self.assertEqual(len(ms.non_overlapping(rows)), 2)       # first, then the first one 26h later

    def test_verdict_needs_data_then_applies_the_rule(self):
        small = ms.analyse(self.rows(10, gross=100.0))
        self.assertIn("yetersiz", ms.verdict(small, days=200))
        good = {"n": 200, "mean": 30.0, "t": 2.5, "months_positive": 0.8, "paired_t": 2.0}
        self.assertIn("GEÇTİ", ms.verdict(good, days=120))
        for key, bad in (("mean", 5.0), ("t", 1.0), ("months_positive", 0.4), ("paired_t", 0.5)):
            self.assertIn("GEÇMEDİ", ms.verdict({**good, key: bad}, days=120))
        self.assertIn("yetersiz", ms.verdict(good, days=30))

    def test_noise_does_not_pass(self):
        rng = np.random.default_rng(0)
        rows = [{"symbol": f"S{i % 25}", "hour": T0 + i * 30 * HOUR_MS, "side": int(rng.choice([-1, 1])),
                 "gross": float(rng.normal(0, 300)), "fund": 0.0, "ret24": float(rng.normal())} for i in range(600)]
        stats = ms.analyse(rows)
        self.assertLess(stats["mean"], ms.DECISION["min_mean_bps"])
        self.assertIn("GEÇMEDİ", ms.verdict(stats, days=400))


class WalkForwardTests(unittest.TestCase):
    def frames(self, trend):
        out = {}
        btc = bars(9500, seed=11, trend=trend)
        for i, symbol in enumerate(["BTCUSDT", "AUSDT", "BUSDT"]):
            data = btc if symbol == "BTCUSDT" else bars(9500, seed=20 + i, trend=trend)
            out[symbol] = ms.prepare(data, None if symbol == "BTCUSDT" else btc)
        return out

    def test_random_walk_has_no_edge(self):
        frames = self.frames(trend=0.0)
        index = frames["AUSDT"].index
        rows = ms.walk_forward(frames, index[6500], index[8800])
        stats = ms.analyse(rows)
        if stats["n"]:
            self.assertLess(stats["mean"], 20.0)         # a pure random walk must not look profitable after costs

    def test_learnable_pattern_is_found(self):
        frames = self.frames(trend=0.0006)
        index = frames["AUSDT"].index
        rows = ms.walk_forward(frames, index[6500], index[8800])
        stats = ms.analyse(rows)
        self.assertGreater(stats["n"], 20)
        self.assertGreater(stats["mean"], 0.0)

    def test_blocks_never_train_on_the_future(self):
        frames = self.frames(trend=0.0)
        index = frames["AUSDT"].index
        cutoff = index[7000]
        X, _ = ms.training_set(frames, cutoff)
        self.assertLessEqual(X.index.max() + pd.Timedelta(hours=ms.HORIZON + 1), cutoff)


class FakeClient:
    """Public Binance endpoints for the live loop."""
    def __init__(self, now_ms, n=9000, trend=0.0006):
        self.now_ms, self.n = now_ms, n
        self.start = (now_ms // HOUR_MS) * HOUR_MS - n * HOUR_MS
        self.data = {s: synthetic_rows(n, seed, self.start, trend=trend) for s, seed in (("BTCUSDT", 1), ("AUSDT", 2), ("BUSDT", 3))}
        self.calls = []

    def klines(self, symbol, interval, limit, start_ms=None, end_ms=None):
        rows = self.data[symbol]
        if start_ms is not None:
            rows = [r for r in rows if start_ms <= r[0] <= (end_ms or 10 ** 18)]
            return rows[:limit]
        return rows[-limit:]

    def funding_rates(self, symbol, start_ms, end_ms):
        return [{"symbol": symbol, "fundingTime": t + 1, "fundingRate": "0.0001"}
                for t in range(start_ms - 24 * HOUR_MS, end_ms + 24 * HOUR_MS, HOUR_MS) if (t // HOUR_MS) % 8 == 0]


class LiveLoopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = (T0 // HOUR_MS) * HOUR_MS + 2 * 60_000
        self.client = FakeClient(self.now)
        self.clock = [self.now / 1000]

    def service(self):
        return ModelShadow(self.client, self.dir.name, lambda: ["BTCUSDT", "AUSDT", "BUSDT"], clock=lambda: self.clock[0])

    def test_tick_trains_records_and_resolves(self):
        shadow = self.service()
        shadow.tick()
        self.assertIsNotNone(shadow.model)
        self.assertTrue((Path(self.dir.name) / "model.pkl").is_file())
        self.assertEqual(shadow.eval_hours, 1)
        self.assertEqual(shadow.evals, 3)
        # second tick in the same hour does not evaluate again
        shadow.tick()
        self.assertEqual(shadow.eval_hours, 1)
        # force a decision to check the outcome pipeline: pretend the model liked AUSDT one day ago
        hour = shadow._hour_done - 30 * HOUR_MS
        event = {"k": "event", "id": f"AUSDT-{hour}", "symbol": "AUSDT", "hour": hour, "side": -1, "p_down": 0.6, "p_flat": 0.2,
                 "p_up": 0.2, "ret24": 0.01, "price": 100.0}
        shadow.events[event["id"]] = event
        shadow.tick()
        outcome = shadow.outcomes[event["id"]]
        entry = pd.Timestamp(hour + HOUR_MS, unit="ms", tz="UTC")
        exit_ = pd.Timestamp(hour + HOUR_MS + ms.HORIZON * HOUR_MS, unit="ms", tz="UTC")
        data = parse_klines(self.client.data["AUSDT"], self.now + 10 ** 9).open
        self.assertAlmostEqual(outcome["gross"], np.log(data[exit_] / data[entry]) * 1e4)
        self.assertLess(outcome["fund"], 0)               # a long pays positive funding; the sign is stored from the long's view
        text = shadow.summary()
        self.assertIn("Model gölge kaydı", text)

    def test_state_survives_restart(self):
        first = self.service()
        first.tick()
        first.events["X-1"] = {"k": "event", "id": "X-1", "symbol": "AUSDT", "hour": 1, "side": 1}
        first._append(first.events["X-1"])
        again = self.service()
        self.assertIn("X-1", again.events)
        self.assertIsNotNone(again.model)
        self.assertEqual(again.evals, first.evals)
        def ticks():
            lines = (Path(self.dir.name) / "model_events.jsonl").read_text().splitlines()
            return sum(1 for line in lines if json.loads(line)["k"] == "tick")
        before = ticks()
        again.tick()                                   # same hour: the hour must not be evaluated a second time
        self.assertEqual(before, ticks())

    def test_bad_symbol_does_not_stop_the_others(self):
        shadow = self.service()
        shadow.symbols_fn = lambda: ["BTCUSDT", "AUSDT", "BUSDT", "NOPEUSDT"]
        original = self.client.klines

        def klines(symbol, *a, **k):
            if symbol == "NOPEUSDT":
                raise ValueError("boom")
            return original(symbol, *a, **k)
        self.client.klines = klines
        shadow.tick()
        self.assertEqual(shadow.evals, 3)
        self.assertIn("NOPEUSDT", shadow.last_error or "")

    def test_unreadable_lines_are_skipped(self):
        path = Path(self.dir.name) / "model_events.jsonl"
        path.write_text('{"k": "start", "ms": 5}\nnot json\n{"k": "event"}\n')
        shadow = self.service()
        self.assertEqual(shadow.start_ms, 5)
        self.assertEqual(shadow.events, {})


if __name__ == "__main__":
    unittest.main()
