import contextlib
import io
from decimal import Decimal
import json
import logging
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from crypto_watcher import news as news_mod
from crypto_watcher.binance import (BinanceError, MarketClient, TradingClient, fmt_decimal, round_step, sign,
                                    ROUND_HALF_UP)
from crypto_watcher.cli import main
from crypto_watcher.config import Secrets, Settings, load_secrets, load_settings
from crypto_watcher.data import (CachedProvider, DemoProvider, SymbolInfo, parse_exchange_info, parse_klines,
                                 select_universe)
from crypto_watcher.engine import scan
from crypto_watcher.indicators import calculate, wilder
from crypto_watcher.state import StateStore
from crypto_watcher.strategy import base_asset, evaluate, market_regime, risk_plan, score_side
from crypto_watcher.telegram import TelegramNotifier, split_message


def frame(close, volume=1000.0):
    close = np.asarray(close, dtype=float)
    n = len(close)
    open_ = np.concatenate([[close[0]], close[:-1]])
    step = 3_600_000
    times = 1_700_000_000_000 + step * np.arange(n)
    df = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.002, "low": np.minimum(open_, close) * 0.998,
                       "close": close, "volume": volume, "quote_volume": volume * close, "taker_buy_base": volume * 0.5,
                       "close_time": times + step - 1}, index=pd.to_datetime(times, unit="ms", utc=True))
    return df


class IndicatorTests(unittest.TestCase):
    def test_wilder_hand_calculated(self):
        result = wilder(pd.Series([1., 2., 3., 6., 9.]), 3)
        self.assertTrue(result.iloc[:2].isna().all())
        np.testing.assert_allclose(result.iloc[2:], [2, 10 / 3, 47 / 9])

    def test_flat_market(self):
        row = calculate(frame(np.full(300, 100.0))).iloc[-1]
        self.assertEqual(row.rsi, 50)
        self.assertAlmostEqual(row.macd, 0)
        self.assertAlmostEqual(row.bb_pctb, 0.5)

    def test_monotonic_rise_has_rsi_100_and_positive_trend(self):
        row = calculate(frame(100 + np.arange(300.0))).iloc[-1]
        self.assertEqual(row.rsi, 100)
        self.assertGreater(row.pdi, row.mdi)
        self.assertGreater(row.ema20, row.ema50)

    def test_no_future_leakage(self):
        rng = np.random.default_rng(3)
        bars = frame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 320))))
        pd.testing.assert_frame_equal(calculate(bars.iloc[:-20]), calculate(bars).iloc[:-20])


class DataTests(unittest.TestCase):
    ROW = [1700000000000, "10", "11", "9", "10.5", "100", 1700003599999, "1000", 5, "60", "600", "0"]

    def test_forming_candle_is_dropped(self):
        later = [1700003600000, "10", "11", "9", "10.5", "1", 1700007199999, "10", 1, "0", "0", "0"]
        df = parse_klines([self.ROW, later], now_ms=1700004000000)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.close.iloc[0], 10.5)

    def test_rejects_bad_candles(self):
        bad_high = list(self.ROW); bad_high[2] = "9"
        for rows in ([bad_high], [self.ROW, self.ROW], []):
            with self.assertRaises(ValueError):
                parse_klines(rows, now_ms=1800000000000)
        nan = list(self.ROW); nan[4] = "nan"
        with self.assertRaises(ValueError):
            parse_klines([nan], now_ms=1800000000000)

    def test_exchange_info_and_universe(self):
        info = {"symbols": [
            {"symbol": "AAAUSDT", "baseAsset": "AAA", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": 0, "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                           {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "9"},
                                           {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
            {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": 0, "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                           {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "9"}]},
            {"symbol": "NEWUSDT", "baseAsset": "NEW", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING",
             "onboardDate": 1_700_000_000_000 - 86_400_000, "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                           {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "9"}]},
            {"symbol": "BTCDOMUSDT", "baseAsset": "BTCDOM", "quoteAsset": "USDT", "contractType": "PERPETUAL",
             "status": "TRADING", "underlyingType": "INDEX", "filters": []},
            {"symbol": "OLDUSDT", "baseAsset": "OLD", "quoteAsset": "USDT", "contractType": "PERPETUAL", "status": "BREAK",
             "filters": []}]}
        tradable = parse_exchange_info(info)
        self.assertEqual(set(tradable), {"AAAUSDT", "USDCUSDT", "NEWUSDT"})
        tickers = {s: {"last": 1.0, "quote_volume": 5e8, "change_pct": 0.0} for s in tradable}
        tickers["AAAUSDT"]["quote_volume"] = 9e8
        universe = select_universe(Settings(), tradable, tickers, 1_700_000_000_000)
        self.assertEqual(universe, ["AAAUSDT"])  # stablecoin and 1-day-old listing excluded
        self.assertEqual(select_universe(Settings(min_quote_volume=1e10, include_symbols=("NEWUSDT",)), tradable, tickers,
                                         1_700_000_000_000), ["NEWUSDT"])

    def test_cached_provider_only_refetches_after_candle_close(self):
        class Inner:
            calls = 0
            def klines(self, symbol, interval, limit):
                self.calls += 1
                return frame(np.full(300, 100.0))
        inner, now = Inner(), [1_700_000_000.0 + 300 * 3600 + 10]
        cached = CachedProvider(inner, clock=lambda: now[0])
        cached.klines("X", "1h", 300); cached.klines("X", "1h", 300)
        self.assertEqual(inner.calls, 1)
        now[0] += 3600
        cached.klines("X", "1h", 300)
        self.assertEqual(inner.calls, 2)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        rng = np.random.default_rng(7)
        noise = rng.normal(0, 0.004, 300)
        self.up = calculate(frame(100 * np.exp(np.cumsum(0.002 + noise))))
        self.down = calculate(frame(100 * np.exp(np.cumsum(-0.002 + noise))))

    def test_trend_direction_drives_side_scores(self):
        for f, strong, weak in ((self.up, "LONG", "SHORT"), (self.down, "SHORT", "LONG")):
            a = score_side(strong, f, f, None, 0.0, None, "NEUTRAL", self.s)["score"]
            b = score_side(weak, f, f, None, 0.0, None, "NEUTRAL", self.s)["score"]
            self.assertGreater(a, b + 40)

    def test_scores_are_bounded_and_unavailable_factors_reported(self):
        r = score_side("LONG", self.up, self.up, None, 0.0, None, "BULL", self.s)
        self.assertLessEqual(r["score"], 100)
        self.assertEqual(r["available_points"], 80)  # no news, BTC benchmark -> 20 points unavailable
        self.assertEqual(len(r["reasons"]["unavailable"]), 2)

    def test_news_moves_score_in_trade_direction_and_vetoes(self):
        good = news_mod.NewsResult(3, 0.8, ["x"])
        bad = news_mod.NewsResult(3, -0.8, ["x"])
        base = lambda n, side: score_side(side, self.up, self.up, self.up, 0.0, n, "BULL", self.s)
        self.assertGreater(base(good, "LONG")["components"]["news"], 5)
        self.assertLess(base(bad, "LONG")["components"]["news"], 5)
        self.assertGreater(base(bad, "SHORT")["components"]["news"], 5)
        last = float(self.up.close.iloc[-1])
        result = evaluate("XUSDT", self.up, self.up, self.up, "BULL", 0.0, last, bad, self.s, 10_000)
        self.assertTrue(any("Adverse news" in b for b in result["sides"]["LONG"]["blockers"]))
        self.assertNotEqual(result["signal"], "STRONG_LONG")

    def test_evaluate_blockers(self):
        last = float(self.up.close.iloc[-1])
        extreme = evaluate("XUSDT", self.up, self.up, self.up, "BULL", 0.01, last, None, self.s, 10_000)
        self.assertTrue(any("funding" in b.lower() for b in extreme["sides"]["LONG"]["blockers"]))
        self.assertEqual(extreme["side"], "SHORT" if extreme["sides"]["SHORT"]["score"] > extreme["sides"]["LONG"]["score"] else "LONG")
        hv = evaluate("XUSDT", self.up, self.up, self.up, "HIGH_VOLATILITY", 0.0, last, None, self.s, 10_000)
        self.assertEqual(hv["signal"], "NO_TRADE")
        drifted = evaluate("XUSDT", self.up, self.up, self.up, "BULL", 0.0, last * 1.5, None, self.s, 10_000)
        self.assertEqual(drifted["signal"], "NO_TRADE")
        only_short = evaluate("XUSDT", self.up, self.up, self.up, "BULL", 0.0, last, None, Settings(allow_long=False), 10_000)
        self.assertEqual(set(only_short["sides"]), {"SHORT"})

    def test_counter_regime_requires_higher_score(self):
        last = float(self.up.close.iloc[-1])
        bull = evaluate("XUSDT", self.up, self.up, self.up, "BULL", 0.0, last, None, self.s, 10_000)
        bear = evaluate("XUSDT", self.up, self.up, self.up, "BEAR", 0.0, last, None, self.s, 10_000)
        self.assertEqual(bear["sides"]["LONG"]["threshold"], bull["sides"]["LONG"]["threshold"] + 10)

    def test_undefined_indicators_raise(self):
        short = calculate(frame(100 + np.arange(60.0)))
        with self.assertRaises(ValueError):
            evaluate("XUSDT", short, short, None, "NEUTRAL", 0.0, 100.0, None, self.s, 10_000)

    def test_regime(self):
        self.assertEqual(market_regime(self.up, self.up, self.s), "BULL")
        self.assertEqual(market_regime(self.down, self.down, self.s), "BEAR")
        self.assertEqual(market_regime(self.up, self.up, Settings(btc_high_vol_atr_pct=0.0005)), "HIGH_VOLATILITY")

    def test_base_asset(self):
        self.assertEqual(base_asset("1000PEPEUSDT"), "PEPE")
        self.assertEqual(base_asset("SOLUSDT"), "SOL")
        self.assertEqual(base_asset("1INCHUSDT"), "1INCH")


class RiskPlanTests(unittest.TestCase):
    INFO = SymbolInfo("XUSDT", "X", 0.01, 0.001, 0.001, 1000.0, 5.0, 0)

    def test_long_and_short_geometry(self):
        s = Settings(atr_stop_multiplier=1.5)
        long = risk_plan("LONG", 100.0, 1.0, 10_000, s)
        short = risk_plan("SHORT", 100.0, 1.0, 10_000, s)
        self.assertEqual((long["stop"], long["take_profit"]), (98.5, 103.0))
        self.assertEqual((short["stop"], short["take_profit"]), (101.5, 97.0))
        self.assertAlmostEqual(long["planned_risk"], 100.0)  # 1% of 10k

    def test_margin_cap_binds_for_tight_stops(self):
        plan = risk_plan("LONG", 100.0, 0.4, 10_000, Settings())   # 0.8 stop -> risk budget alone would allow 125 units
        self.assertAlmostEqual(plan["margin"], 2000.0)       # 20% of capital
        self.assertAlmostEqual(plan["quantity"], 100.0)
        self.assertLess(plan["planned_risk"], 100.0)

    def test_rounding_and_minimums(self):
        plan = risk_plan("LONG", 123.456, 1.234, 10_000, Settings(), self.INFO)
        self.assertEqual(Decimal(str(plan["quantity"])) % Decimal("0.001"), 0)
        self.assertEqual(round(plan["stop"], 2), plan["stop"])
        with self.assertRaisesRegex(ValueError, "minimum order size"):
            risk_plan("LONG", 100.0, 1.0, 5, Settings(), self.INFO)  # 3.3 USDT notional < 5 minimum

    def test_rejections(self):
        s = Settings()
        with self.assertRaisesRegex(ValueError, "too wide"):
            risk_plan("LONG", 100.0, 10.0, 10_000, s)       # 20% stop at 5x
        with self.assertRaisesRegex(ValueError, "fees"):
            risk_plan("LONG", 100.0, 0.01, 10_000, s)
        with self.assertRaises(ValueError):
            risk_plan("SHORT", 1.0, 0.0, 10_000, s)
        with self.assertRaisesRegex(ValueError, "non-positive"):
            risk_plan("LONG", 1.0, 0.7, 10_000, Settings(leverage=1))  # stop below zero


class NewsTests(unittest.TestCase):
    def test_scoring_and_matching(self):
        self.assertLess(news_mod.headline_score("Exchange hacked, $50M drained"), -0.9)
        self.assertGreater(news_mod.headline_score("Solana surges to all-time high"), 0.5)
        self.assertEqual(news_mod.headline_score("Weekly market update"), 0)
        self.assertFalse(news_mod.mentions("Why people near the exit", "NEAR"))
        self.assertTrue(news_mod.mentions("NEAR hits new high", "NEAR"))
        self.assertTrue(news_mod.mentions("Solana network upgrade", "SOL"))
        self.assertFalse(news_mod.mentions("Dissolved fund", "SOL"))
        self.assertTrue(news_mod.mentions("Why $sol is moving", "SOL"))

    def test_aggregate_shrinks_with_few_headlines_and_decays(self):
        now = 1_000_000.0
        one = [news_mod.Headline("SOL hacked", now - 60, "t")]
        three = one * 3
        self.assertAlmostEqual(news_mod.aggregate(one, "SOL", now, 24).sentiment, -1 / 3, places=1)
        self.assertLess(news_mod.aggregate(three, "SOL", now, 24).sentiment, -0.9)
        old = [news_mod.Headline("SOL hacked", now - 30 * 3600, "t")]
        self.assertFalse(news_mod.aggregate(old, "SOL", now, 24).available)
        self.assertFalse(news_mod.aggregate(one, "ETH", now, 24).available)

    def test_parse_rss_atom_and_reject_entities(self):
        rss = b"""<rss><channel><item><title>BTC rallies &amp; &lt;b&gt;breaks out&lt;/b&gt;</title>
                  <pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate></item><item><title>No date</title></item></channel></rss>"""
        items = news_mod.parse_feed(rss, "x")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "BTC rallies & breaks out")
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>ETH upgrade</title>
                   <updated>2026-10-06T10:00:00Z</updated></entry></feed>"""
        self.assertEqual(news_mod.parse_feed(atom, "x")[0].title, "ETH upgrade")
        with self.assertRaises(ValueError):
            news_mod.parse_feed(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss/>', "x")

    def test_service_survives_feed_failures_and_caches(self):
        class Resp:
            def __init__(self, body): self.content = body
            def raise_for_status(self): pass
        class Session:
            calls = 0
            def get(self, url, **kw):
                self.calls += 1
                if "bad" in url:
                    raise requests.ConnectionError("down")
                return Resp(b"<rss><channel><item><title>BTC surges</title><pubDate>Tue, 06 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>")
        session, now = Session(), [1791280800.0 + 60]
        service = news_mod.NewsService(["https://good/rss", "https://bad/rss"], session=session, clock=lambda: now[0])
        self.assertTrue(service.for_coin("BTC").available)
        self.assertEqual(len(service.last_errors), 1)
        service.for_coin("BTC")
        self.assertEqual(session.calls, 2)  # cached


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        Settings()

    def test_invalid_values_rejected(self):
        for kwargs in ({"leverage": 0}, {"leverage": 50}, {"risk_fraction": 0.5}, {"tp_r": 1.0}, {"timeframe": "7m"},
                       {"htf": "1h"}, {"allow_long": False, "allow_short": False}, {"margin_type": "X"},
                       {"entry_score": 50.0}, {"kline_limit": 100}, {"leverage": True}, {"include_symbols": ("sol",)},
                       {"risk_fraction": float("nan")}, {"market_data": "spot"}, {"news_feeds": "http://x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_toml_loading_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.toml"
            path.write_text('leverage = 3\ninclude_symbols = ["SOLUSDT"]\nrisk_fraction = 0.005\n')
            s = load_settings(path)
            self.assertEqual((s.leverage, s.include_symbols, s.risk_fraction), (3, ("SOLUSDT",), 0.005))
            path.write_text("levrage = 3\n")
            with self.assertRaisesRegex(ValueError, "Unknown"):
                load_settings(path)

    def test_shipped_settings_file_loads(self):
        load_settings(Path(__file__).resolve().parents[1] / "config" / "settings.toml")

    def test_secrets_env_wins_over_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# c\nBINANCE_TESTNET_API_KEY=filekey\nBINANCE_TESTNET_API_SECRET='sec'\nTELEGRAM_CHAT_ID=42\n")
            secrets = load_secrets(env, {"BINANCE_TESTNET_API_KEY": "envkey", "TELEGRAM_BOT_TOKEN": "tok"})
            self.assertEqual((secrets.api_key, secrets.api_secret, secrets.telegram_chat_id), ("envkey", "sec", "42"))
            self.assertTrue(secrets.has_binance and secrets.has_telegram)
            self.assertFalse(load_secrets(None, {}).has_binance)


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.requests.append((method, url, params, headers))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, timeout=None):
        self.requests.append(("POST", url, json, None))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status_code, self.headers, self.text = payload, status, headers or {}, str(payload)

    def json(self):
        return self.payload


class BinanceTests(unittest.TestCase):
    def test_signature_matches_binance_documentation_vector(self):
        self.assertEqual(sign("NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j",
                              "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"),
                         "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_trading_client_is_testnet_only(self):
        for url in ("https://fapi.binance.com", "https://testnet.binancefuture.com.evil.com", "https://evil.com/testnet.binancefuture.com",
                    "http://localhost:8000"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                TradingClient("k", "s", url)
        with self.assertRaises(ValueError):
            TradingClient("", "")
        TradingClient("k", "s")  # default is the testnet

    def test_number_formatting(self):
        self.assertEqual(fmt_decimal(1e-7), "0.0000001")
        self.assertEqual(fmt_decimal(100.0), "100")
        self.assertEqual(fmt_decimal(0.00010), "0.0001")
        self.assertEqual(round_step(0.12999, 0.001), 0.129)
        self.assertEqual(round_step(2715.37, 0.1, ROUND_HALF_UP), 2715.4)

    def test_signed_request_shape_and_algo_order_payload(self):
        session = FakeSession([Resp({"clientAlgoId": "a"})])
        client = TradingClient("KEY", "SECRET", session=session)
        client.algo_order("BTCUSDT", "SELL", "STOP_MARKET", 85000.5, "cw-sl-1")
        method, url, params, headers = session.requests[0]
        self.assertEqual((method, headers), ("POST", {"X-MBX-APIKEY": "KEY"}))
        self.assertTrue(url.startswith("https://testnet.binancefuture.com/fapi/v1/algoOrder?"))
        query, _, signature = url.partition("?")[2].rpartition("&signature=")
        self.assertEqual(signature, sign("SECRET", query))
        for part in ("algoType=CONDITIONAL", "type=STOP_MARKET", "triggerPrice=85000.5", "closePosition=true",
                     "workingType=MARK_PRICE", "clientAlgoId=cw-sl-1", "timestamp="):
            self.assertIn(part, query)

    def test_reduce_only_algo_order_and_list_normalisation(self):
        session = FakeSession([Resp({"clientAlgoId": "a"}), Resp({"orders": [{"clientAlgoId": "x"}]}), Resp([])])
        client = TradingClient("K", "S", session=session)
        client.algo_order("BTCUSDT", "SELL", "STOP_MARKET", 1.5, "id", quantity=0.25)
        url = session.requests[0][1]
        self.assertIn("quantity=0.25", url)
        self.assertIn("reduceOnly=true", url)
        self.assertNotIn("closePosition", url)
        self.assertEqual(client.open_algo_orders(), [{"clientAlgoId": "x"}])
        self.assertEqual(client.open_algo_orders("BTCUSDT"), [])

    def test_none_params_are_dropped_and_reduce_only_flag(self):
        session = FakeSession([Resp({"status": "FILLED"})])
        TradingClient("K", "S", session=session).market_order("BTCUSDT", "BUY", 0.001, reduce_only=False)
        self.assertNotIn("reduceOnly", session.requests[0][1])
        self.assertNotIn("newClientOrderId", session.requests[0][1])

    def test_clock_skew_triggers_one_resync(self):
        session = FakeSession([Resp({"code": -1021, "msg": "skew"}, 400), Resp({"serverTime": 10**13}),
                               Resp({"dualSidePosition": False})])
        client = TradingClient("K", "S", session=session)
        self.assertFalse(client.position_mode())
        self.assertGreater(client._offset_ms, 0)

    def test_orders_are_not_retried_but_reads_are(self):
        post = FakeSession([requests.Timeout("t")])
        with self.assertRaises(BinanceError):
            TradingClient("K", "S", session=post, sleep=lambda s: None).market_order("BTCUSDT", "BUY", 1)
        self.assertEqual(len(post.requests), 1)
        get = FakeSession([requests.Timeout("t"), Resp([])])
        self.assertEqual(MarketClient(session=get, sleep=lambda s: None).ticker_24h(), [])

    def test_network_error_message_names_the_root_cause(self):
        import ssl
        import urllib3
        cause = ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate")
        wrapped = requests.exceptions.SSLError(urllib3.exceptions.MaxRetryError(None, "/fapi/v1/ticker/24hr", reason=cause))
        with self.assertRaises(BinanceError) as ctx:
            MarketClient(session=FakeSession([wrapped] * 4), sleep=lambda s: None).ticker_24h()
        self.assertIn("/fapi/v1/ticker/24hr", str(ctx.exception))
        self.assertIn("SSLCertVerificationError", str(ctx.exception))
        self.assertIn("certificate verify failed", str(ctx.exception))

    def test_rate_limit_backoff_and_error_mapping(self):
        sleeps = []
        session = FakeSession([Resp({"code": -1003, "msg": "slow"}, 429, {"Retry-After": "3"}), Resp({"serverTime": 5})])
        self.assertEqual(MarketClient(session=session, sleep=sleeps.append).server_time(), 5)
        self.assertEqual(sleeps, [3.0])
        with self.assertRaises(BinanceError) as ctx:
            MarketClient(session=FakeSession([Resp({"code": -2019, "msg": "Margin is insufficient."}, 400)])).ping()
        self.assertEqual(ctx.exception.code, -2019)

    def test_benign_errors_are_swallowed(self):
        session = FakeSession([Resp({"code": -4046, "msg": "No need to change margin type."}, 400),
                               Resp({"code": -4059, "msg": "No need to change position side."}, 400)])
        client = TradingClient("K", "S", session=session)
        client.set_margin_type("BTCUSDT", "ISOLATED")
        client.set_one_way_mode()

    def test_balance_parsing_handles_both_layouts(self):
        v3 = FakeSession([Resp({"assets": [{"asset": "USDT", "walletBalance": "100", "availableBalance": "80", "unrealizedProfit": "-2"}]})])
        self.assertEqual(TradingClient("K", "S", session=v3).usdt_balance(), {"wallet": 100.0, "available": 80.0, "unrealized": -2.0})
        flat = FakeSession([Resp({"totalWalletBalance": "50", "availableBalance": "40", "totalUnrealizedProfit": "1"})])
        self.assertEqual(TradingClient("K", "S", session=flat).usdt_balance()["wallet"], 50.0)


class TelegramTests(unittest.TestCase):
    def test_split_message(self):
        self.assertEqual(split_message("a\nb"), ["a\nb"])
        chunks = split_message("x" * 9000, 4000)
        self.assertEqual([len(c) for c in chunks], [4000, 4000, 1000])
        self.assertTrue(all(len(c) <= 4000 for c in split_message("\n".join(["line"] * 3000), 4000)))

    def test_send_uses_html_and_falls_back_to_plain_text(self):
        session = FakeSession([Resp({"ok": False, "description": "can't parse entities"}, 400), Resp({"ok": True})])
        notifier = TelegramNotifier("TOKEN", "1", session=session)
        self.assertTrue(notifier.send("<b>hi</b> &amp; bye"))
        first, second = session.requests[0][2], session.requests[1][2]
        self.assertEqual(first["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", second)
        self.assertEqual(second["text"], "hi & bye")

    def test_never_raises_and_scrubs_token(self):
        session = FakeSession([requests.ConnectionError("https://api.telegram.org/botTOKEN/sendMessage failed")] * 3)
        notifier = TelegramNotifier("TOKEN", "1", session=session, sleep=lambda s: None)
        with self.assertLogs("crypto_watcher.telegram", "WARNING") as logs:
            self.assertFalse(notifier.send("x"))
        self.assertNotIn("TOKEN", "\n".join(logs.output))
        self.assertFalse(TelegramNotifier().send("unconfigured"))

    def test_poll_accepts_only_the_authorised_chat_and_this_bot(self):
        updates = {"ok": True, "result": [
            {"update_id": 10, "message": {"chat": {"id": 1}, "from": {"id": 7}, "text": "/status@MyBot extra"}},
            {"update_id": 11, "message": {"chat": {"id": 999}, "from": {"id": 8}, "text": "/pause"}},
            {"update_id": 12, "message": {"chat": {"id": 1}, "from": {"id": 7}, "text": "hello"}},
            {"update_id": 13, "message": {"chat": {"id": 1}, "from": {"id": 7}, "text": "/pause@OtherBot"}},
            {"update_id": 14, "message": {"chat": {"id": 1}, "from": {"id": 9}, "text": "/PNL"}}]}
        notifier = TelegramNotifier("T", "1", session=FakeSession([Resp(updates)]))
        notifier.username = "mybot"
        self.assertEqual(notifier.poll(0), ([("/status", "7"), ("/pnl", "9")], 15))

    def test_group_chat_ids_are_negative_strings_and_discovery(self):
        updates = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": 55, "type": "private", "first_name": "Ömer"}, "from": {"id": 55, "first_name": "Ömer"}, "text": "hi"}},
            {"update_id": 2, "message": {"chat": {"id": -100777, "type": "supergroup", "title": "Kripto"}, "from": {"id": 55, "first_name": "Ömer"}, "text": "/x"}},
            {"update_id": 3, "my_chat_member": {"chat": {"id": -100888, "type": "group", "title": "Eski"}, "from": {"id": 12, "first_name": "Ali", "is_bot": False}}}]}
        notifier = TelegramNotifier("T", "0", session=FakeSession([Resp(updates)]))
        seen = notifier.discover()
        self.assertEqual(seen["chats"][-100777], ("supergroup", "Kripto"))
        self.assertEqual(seen["chats"][55][0], "private")
        self.assertEqual(seen["users"], {55: "Ömer", 12: "Ali"})
        group = TelegramNotifier("T", "-100777", session=FakeSession([Resp({"ok": True})]))
        self.assertTrue(group.send("x"))
        self.assertEqual(group.session.requests[0][2]["chat_id"], "-100777")

    def test_supergroup_migration_is_explained(self):
        session = FakeSession([Resp({"ok": False, "description": "group chat was upgraded to a supergroup chat",
                                     "parameters": {"migrate_to_chat_id": -100999}}, 400)] * 2)
        with self.assertLogs("crypto_watcher.telegram", "WARNING") as logs:
            TelegramNotifier("T", "-5", session=session).send("x")
        self.assertIn("TELEGRAM_CHAT_ID=-100999", "\n".join(logs.output))

    def test_allowed_user_ids_from_env(self):
        secrets = load_secrets(None, {"TELEGRAM_ALLOWED_USER_IDS": "12, 34 ,"})
        self.assertEqual(secrets.telegram_allowed_users, ("12", "34"))


class StateTests(unittest.TestCase):
    def test_atomic_roundtrip_and_history_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "state.json"
            state = StateStore(path)
            state["paused"] = True
            for i in range(60):
                state.record_close({"i": i})
            state.save()
            self.assertEqual([p.name for p in path.parent.iterdir()], ["state.json"])  # no temp leftovers
            again = StateStore(path)
            self.assertTrue(again["paused"])
            self.assertEqual(len(again["history"]), 50)
            self.assertEqual(again["history"][-1]["i"], 59)
        StateStore(None).save()


class ScanAndCliTests(unittest.TestCase):
    def setUp(self):  # main() installs a root log handler; keep test output quiet
        self.addCleanup(logging.getLogger().handlers.clear)

    def test_demo_scan_is_deterministic_and_json_safe(self):
        d = DemoProvider(1_791_284_400_000)
        a = scan(d, Settings(), d.symbols(), 10_000.0, None, 1_791_284_400_000, "demo", True)
        b = scan(d, Settings(), d.symbols(), 10_000.0, None, 1_791_284_400_000, "demo", True)
        self.assertEqual(json.dumps(a, allow_nan=False), json.dumps(b, allow_nan=False))
        self.assertEqual(a["errors"], [])
        self.assertEqual(len(a["signals"]), 8)
        self.assertEqual({s["side"] for s in a["signals"][:3]}, {"LONG", "SHORT"})

    def test_one_bad_symbol_is_isolated(self):
        d = DemoProvider(1_791_284_400_000)
        report = scan(d, Settings(), d.symbols() + ["NOPEUSDT"], 10_000.0, None, 1_791_284_400_000, "demo", True)
        self.assertEqual([e["symbol"] for e in report["errors"]], ["NOPEUSDT"])
        self.assertEqual(len(report["signals"]), 8)

    def test_cli_demo_text_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["scan", "--demo"]), 0)
        self.assertIn("DEMO_SYNTHETIC", out.getvalue())
        self.assertIn("BTCUSDT", out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            out, target = io.StringIO(), Path(tmp) / "r" / "scan.json"
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["scan", "--demo", "--json", "--output", str(target)]), 0)
            self.assertEqual(json.loads(out.getvalue())["mode"], "DEMO_SYNTHETIC")
            self.assertEqual(json.loads(target.read_text())["mode"], "DEMO_SYNTHETIC")

    def test_telegram_id_command(self):
        from unittest import mock
        from crypto_watcher import telegram as tgm
        with mock.patch.object(tgm.TelegramNotifier, "whoami", return_value="MyBot"), \
                mock.patch.object(tgm.TelegramNotifier, "discover", return_value={"chats": {-100777: ("supergroup", "Kripto")}, "users": {55: "Ömer"}}), \
                tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("TELEGRAM_BOT_TOKEN=abc\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--env-file", str(env), "telegram-id"]), 0)
            self.assertIn("-100777", out.getvalue())
            self.assertIn("TELEGRAM_ALLOWED_USER_IDS", out.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(["--env-file", str(Path(tmp) / "none"), "telegram-id"]), 2)

    def test_cli_rejects_bad_config_and_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.toml"
            bad.write_text("leverage = 99\n")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(["--config", str(bad), "scan", "--demo"]), 2)
            self.assertIn("leverage", err.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(main(["--env-file", str(Path(tmp) / "none"), "run", "--once"]), 2)
            self.assertIn("BINANCE_TESTNET_API_KEY", err.getvalue())


if __name__ == "__main__":
    unittest.main()
