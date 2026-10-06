import unittest
from crypto_watcher.binance import BinanceError
from crypto_watcher.bot import Bot
from crypto_watcher.config import Settings
from crypto_watcher.data import DemoProvider
from crypto_watcher.engine import actionable, scan
from crypto_watcher.executor import Executor
from crypto_watcher.state import StateStore
from crypto_watcher.telegram import TelegramNotifier
from helpers import Clock, FakeExchange, make_infos


class Notifier(TelegramNotifier):
    def poll(self, offset):
        commands, self.commands = getattr(self, "commands", []), []
        return commands, offset


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # scanning is the slow part; the candidates are read-only
        clock = Clock()
        cls.settings = Settings(min_quote_volume=1.0)
        cls.demo = DemoProvider(int(clock() * 1000))
        cls.symbols = cls.demo.symbols()
        cls.base_prices = {s: v["last"] for s, v in cls.demo.tickers().items()}
        report = scan(cls.demo, cls.settings, cls.symbols, 10_000.0, None, int(clock() * 1000), "demo")
        cls.base_cands = {c["symbol"]: c for c in actionable(report)}

    def setUp(self):
        self.clock = Clock()
        self.prices = dict(self.base_prices)
        self.cands = self.base_cands
        self.ex = FakeExchange(self.prices, self.clock)
        self.notifier = Notifier()
        self.state = StateStore(None)
        self.executor = Executor(self.ex, self.settings, self.state, self.notifier, make_infos(self.symbols), self.clock)
        self.executor.roll_day(15_000.0)

    def messages(self):
        return "\n".join(self.notifier.sent)

    def open(self, symbol):
        trade = self.executor.open_trade(self.cands[symbol])
        self.assertIsNotNone(trade, self.messages())
        return trade


class OpenTests(Base):
    def test_fixture_has_both_sides(self):
        self.assertEqual(self.cands["DOGEUSDT"]["side"], "LONG")
        self.assertEqual(self.cands["XRPUSDT"]["side"], "SHORT")

    def test_open_long_places_market_stop_and_target(self):
        trade = self.open("DOGEUSDT")
        market = self.ex.kinds("market")[0]
        self.assertEqual(market[2:], ("BUY", trade["quantity"], False))
        algos = {a["orderType"]: a for a in self.ex.algos.values()}
        self.assertEqual(algos["STOP_MARKET"]["side"], "SELL")
        self.assertLess(algos["STOP_MARKET"]["triggerPrice"], trade["entry"])
        self.assertGreater(algos["TAKE_PROFIT_MARKET"]["triggerPrice"], trade["entry"])
        reward = algos["TAKE_PROFIT_MARKET"]["triggerPrice"] - trade["entry"]
        risk = trade["entry"] - algos["STOP_MARKET"]["triggerPrice"]
        self.assertAlmostEqual(reward / risk, self.settings.tp_r, places=3)
        self.assertIn("DOGEUSDT", self.state["trades"])
        self.assertIn(("margin", "DOGEUSDT", "ISOLATED"), self.ex.calls)
        self.assertIn(("leverage", "DOGEUSDT", 5), self.ex.calls)
        text = self.messages()
        self.assertIn("LONG AÇILDI", text)
        self.assertIn("DOGEUSDT", text)

    def test_open_short_is_mirrored(self):
        trade = self.open("XRPUSDT")
        self.assertEqual(self.ex.kinds("market")[0][2], "SELL")
        algos = {a["orderType"]: a for a in self.ex.algos.values()}
        self.assertEqual(algos["STOP_MARKET"]["side"], "BUY")
        self.assertGreater(algos["STOP_MARKET"]["triggerPrice"], trade["entry"])
        self.assertLess(algos["TAKE_PROFIT_MARKET"]["triggerPrice"], trade["entry"])
        self.assertIn("SHORT AÇILDI", self.messages())

    def test_client_ids_are_unique_per_symbol_even_in_the_same_millisecond(self):
        from crypto_watcher.executor import client_id
        a, b = client_id("sl", "DOGEUSDT", 1791284400000), client_id("sl", "XRPUSDT", 1791284400000)
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(client_id("sl", "1000000BOBUSDT", 1791284400000)), 36)
        self.assertRegex(a, r"^[.A-Za-z0-9:/_-]{1,36}$")

    def test_risk_is_bounded(self):
        trade = self.open("DOGEUSDT")
        self.assertLessEqual(trade["planned_risk"], 10_000 * self.settings.risk_fraction * 1.001)
        margin = trade["quantity"] * trade["entry"] / trade["leverage"]
        self.assertLessEqual(margin, 10_000 * self.settings.max_margin_fraction * 1.001)

    def test_stop_failure_closes_position_and_never_tracks_it(self):
        self.ex.fail_algo = {"STOP_MARKET"}
        self.assertIsNone(self.executor.open_trade(self.cands["DOGEUSDT"]))
        self.assertEqual(self.ex.pos, {})
        self.assertEqual(self.ex.kinds("market")[-1][2:][-1], True)  # reduce-only close
        self.assertEqual(self.state["trades"], {})
        self.assertIn("kapatıldı", self.messages())

    def test_target_failure_keeps_protected_position(self):
        self.ex.fail_algo = {"TAKE_PROFIT_MARKET"}
        trade = self.open("DOGEUSDT")
        self.assertIsNone(trade["tp_id"])
        self.assertIn("DOGEUSDT", self.ex.pos)
        self.assertIn("take-profit emri konulamadı", self.messages())

    def test_leverage_falls_back_when_symbol_limit_is_lower(self):
        self.ex.reject_leverage_above = 2
        self.assertEqual(self.open("DOGEUSDT")["leverage"], 2)

    def test_existing_untracked_position_blocks_entry(self):
        self.ex.pos["DOGEUSDT"] = {"amt": 5.0, "entry": self.prices["DOGEUSDT"]}
        self.assertIsNone(self.executor.open_trade(self.cands["DOGEUSDT"]))
        self.assertEqual(self.ex.kinds("market"), [])

    def test_insufficient_balance_skips_without_orders(self):
        self.ex.wallet = 1.0
        self.assertIsNone(self.executor.open_trade(self.cands["DOGEUSDT"]))
        self.assertEqual(self.ex.kinds("market"), [])


class GuardTests(Base):
    def test_can_open_rules(self):
        c = self.cands["DOGEUSDT"]
        self.assertIsNone(self.executor.can_open(c))
        self.state["paused"] = True
        self.assertEqual(self.executor.can_open(c), "paused")
        self.state["paused"] = False
        self.state["cooldowns"]["DOGEUSDT"] = self.clock() + 60
        self.assertEqual(self.executor.can_open(c), "cooldown")
        self.state["cooldowns"].clear()
        self.state["trades"]["DOGEUSDT"] = {"side": "LONG"}
        self.assertEqual(self.executor.can_open(c), "already in a position")

    def test_position_and_direction_limits(self):
        s = Settings(max_open_positions=2, max_same_direction=1, min_quote_volume=1.0)
        ex = Executor(self.ex, s, self.state, self.notifier, make_infos(self.symbols), self.clock)
        self.state["trades"]["BTCUSDT"] = {"side": "LONG"}
        self.assertIn("same-direction", ex.can_open(self.cands["DOGEUSDT"]))
        self.assertIsNone(ex.can_open(self.cands["XRPUSDT"]))
        self.state["trades"]["ETHUSDT"] = {"side": "SHORT"}
        self.assertEqual(ex.can_open(self.cands["XRPUSDT"]), "max open positions")

    def test_daily_loss_lock(self):
        self.assertFalse(self.executor.daily_loss_locked())
        self.state["daily"]["realized"] = -0.05 * 10_000
        self.assertTrue(self.executor.daily_loss_locked())
        self.assertEqual(self.executor.can_open(self.cands["DOGEUSDT"]), "daily loss limit reached")

    def test_roll_day_returns_finished_summary(self):
        self.state["daily"]["realized"] = 12.0
        self.assertIsNone(self.executor.roll_day(15_000))
        self.clock.now += 86_400
        finished = self.executor.roll_day(15_100)
        self.assertEqual(finished["realized"], 12.0)
        self.assertEqual(self.state["daily"]["realized"], 0.0)


class LifecycleTests(Base):
    def test_take_profit_closes_notifies_and_cleans_up(self):
        trade = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", trade["take_profit"] * 1.001)
        self.clock.now += 3600
        self.executor.manage()
        self.assertEqual(self.state["trades"], {})
        self.assertEqual(self.ex.pos, {})
        self.assertIn(("cancel_all_algo", "DOGEUSDT"), self.ex.calls)
        self.assertEqual(self.state["history"][-1]["reason"], "TAKE_PROFIT")
        self.assertGreater(self.state["daily"]["realized"], 0)
        self.assertGreater(self.state["cooldowns"]["DOGEUSDT"], self.clock())
        self.assertIn("Take-profit", self.messages())
        self.assertIn("✅", self.messages())

    def test_stop_loss_is_a_loss_near_minus_one_r(self):
        trade = self.open("XRPUSDT")
        self.ex.move("XRPUSDT", trade["stop"] * 1.001)
        self.executor.manage()
        record = self.state["history"][-1]
        self.assertEqual(record["reason"], "STOP_LOSS")
        self.assertLess(record["pnl"], 0)
        self.assertAlmostEqual(-record["pnl"], trade["planned_risk"], delta=trade["planned_risk"] * 0.2)
        self.assertIn("❌", self.messages())

    def test_breakeven_places_new_stop_before_cancelling_old(self):
        trade = self.open("DOGEUSDT")
        old_id = trade["sl_id"]
        self.ex.move("DOGEUSDT", trade["entry"] + 1.05 * trade["risk_distance"])
        self.executor.manage()
        self.assertTrue(trade["breakeven_done"])
        self.assertNotEqual(trade["sl_id"], old_id)
        self.assertGreater(trade["stop"], trade["entry"])
        order = [c[0] + ":" + c[-1] for c in self.ex.calls if c[0] in ("algo", "cancel_algo")]
        self.assertLess(order.index(f"algo:{trade['sl_id']}"), order.index(f"cancel_algo:{old_id}"))
        self.assertIn("BAŞA BAŞA", self.messages())
        new = self.ex.algos[trade["sl_id"]]
        self.assertFalse(new["closePosition"])               # coexists with the closePosition stop (-4130 otherwise)
        self.assertEqual(new["quantity"], trade["quantity"])
        # price falls back through the new stop -> breakeven exit, tiny loss/gain only
        self.ex.move("DOGEUSDT", trade["stop"] * 0.999)
        self.executor.manage()
        self.assertEqual(self.state["history"][-1]["reason"], "BREAKEVEN_STOP")
        self.assertLess(abs(self.state["history"][-1]["pnl"]), 0.2 * trade["planned_risk"])

    def test_breakeven_not_triggered_below_threshold_and_only_once(self):
        trade = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", trade["entry"] + 0.5 * trade["risk_distance"])
        self.executor.manage()
        self.assertFalse(trade["breakeven_done"])
        self.ex.move("DOGEUSDT", trade["entry"] + 1.2 * trade["risk_distance"])
        self.executor.manage()
        self.executor.manage()
        self.assertEqual(len(self.ex.kinds("cancel_algo")), 1)

    def test_breakeven_stop_failure_leaves_old_stop_in_place(self):
        trade = self.open("DOGEUSDT")
        old_id = trade["sl_id"]
        self.ex.fail_algo = {"STOP_MARKET"}
        self.ex.move("DOGEUSDT", trade["entry"] + 1.1 * trade["risk_distance"])
        self.executor.manage()
        self.assertEqual(trade["sl_id"], old_id)
        self.assertEqual(self.ex.algos[old_id]["algoStatus"], "NEW")

    def test_time_stop_closes_at_market(self):
        trade = self.open("DOGEUSDT")
        self.clock.now += self.settings.max_hold_hours * 3600 + 1
        self.executor.manage()
        self.assertEqual(self.ex.pos, {})
        self.assertEqual(self.state["history"][-1]["reason"], "TIME_STOP")
        self.assertIn("Zaman stopu", self.messages())

    def test_external_close_is_reported_as_external(self):
        trade = self.open("DOGEUSDT")
        for a in self.ex.algos.values():
            a["algoStatus"] = "CANCELED"
        self.ex.market_order("DOGEUSDT", "SELL", trade["quantity"], reduce_only=True)
        self.executor.manage()
        self.assertEqual(self.state["history"][-1]["reason"], "EXTERNAL")

    def test_missing_fills_are_retried_then_reported_without_pnl(self):
        trade = self.open("DOGEUSDT")
        self.ex.pos.clear()          # position vanished, no fills available
        for _ in range(3):
            self.executor.manage()
            self.assertIn("DOGEUSDT", self.state["trades"])
        self.executor.manage()
        self.assertEqual(self.state["trades"], {})
        self.assertIn("PnL: alınamadı", self.messages())
        self.assertEqual(self.state["daily"]["trades"], 0)

    def test_reconcile_restores_missing_stop(self):
        trade = self.open("DOGEUSDT")
        for a in self.ex.algos.values():
            a["algoStatus"] = "CANCELED"
        self.executor.reconcile()
        live = self.ex.open_algo_orders("DOGEUSDT")
        self.assertEqual({a["orderType"] for a in live}, {"STOP_MARKET", "TAKE_PROFIT_MARKET"})
        self.assertIn("stop emri eksikti", self.messages())

    def test_reconcile_finalizes_trades_closed_while_offline(self):
        trade = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", trade["take_profit"] * 1.001)
        self.executor.reconcile()
        self.assertEqual(self.state["trades"], {})

    def test_unmanaged_position_is_flagged_not_touched(self):
        self.ex.pos["ADAUSDT"] = {"amt": 10.0, "entry": self.prices["ADAUSDT"]}
        self.executor.reconcile()
        self.assertIn("ADAUSDT", self.messages())
        self.assertEqual(self.ex.kinds("market"), [])


class BotTests(Base):
    def make_bot(self, executor=True):
        bot = Bot(self.settings, self.demo, "demo", None, self.notifier, self.state, self.executor if executor else None,
                  self.ex, None, self.clock, sleep=lambda s: None)
        bot.start()
        return bot

    def test_end_to_end_opens_expected_trades_once_per_candle(self):
        bot = self.make_bot()
        bot.tick()
        self.assertEqual(set(self.state["trades"]), {"DOGEUSDT", "XRPUSDT", "BTCUSDT"})
        self.assertEqual({t["side"] for t in self.state["trades"].values()}, {"LONG", "SHORT"})
        orders = len(self.ex.kinds("market"))
        self.clock.now += 400
        bot.tick()
        self.assertEqual(len(self.ex.kinds("market")), orders)  # same candle: no re-entry

    def test_dry_run_announces_signals_without_orders(self):
        bot = self.make_bot(executor=False)
        bot.tick()
        self.assertEqual(self.ex.kinds("market"), [])
        self.assertEqual(self.ex.kinds("algo"), [])
        signals = [m for m in self.notifier.sent if "SİNYALİ" in m]
        self.assertEqual(len(signals), 3)
        self.clock.now += 400
        bot.tick()
        self.assertEqual(len([m for m in self.notifier.sent if "SİNYALİ" in m]), 3)

    def test_paused_bot_opens_nothing_and_commands_work(self):
        bot = self.make_bot()
        self.notifier.commands = ["/pause"]
        bot.tick()
        self.assertEqual(self.state["trades"], {})
        self.assertTrue(self.state["paused"])
        self.notifier.commands = ["/status", "/top", "/pnl", "/positions", "/bogus"]
        self.clock.now += 400
        bot.tick()
        text = self.messages()
        for expected in ("Durum", "En yüksek skorlar", "Kâr / Zarar", "Açık pozisyon yok", "Komutlar"):
            self.assertIn(expected, text)
        self.notifier.commands = ["/resume"]
        self.clock.now += 400
        bot.tick()
        self.assertFalse(self.state["paused"])
        self.assertEqual(len(self.state["trades"]), 3)

    def test_positions_command_shows_live_pnl(self):
        bot = self.make_bot()
        bot.tick()
        long_t, short_t = self.state["trades"]["DOGEUSDT"], self.state["trades"]["XRPUSDT"]
        self.ex.move("DOGEUSDT", long_t["entry"] + 0.5 * long_t["risk_distance"])      # +0.5R
        self.ex.move("XRPUSDT", short_t["entry"] + 0.5 * short_t["risk_distance"])      # short is losing 0.5R
        self.clock.now += 3600 + 600
        reply = bot._answer("/positions")
        self.assertIn("Açık pozisyonlar (3)", reply)
        self.assertIn("DOGEUSDT", reply)
        self.assertIn("+0.50R", reply)
        self.assertIn("-0.50R", reply)
        self.assertIn("1sa 10dk", reply)
        self.assertIn("Anlık", reply)
        expected = (long_t["quantity"] * 0.5 * long_t["risk_distance"]) - (short_t["quantity"] * 0.5 * short_t["risk_distance"])
        btc = self.state["trades"]["BTCUSDT"]
        self.assertIn("Toplam gerçekleşmemiş", reply)
        self.assertEqual(bot._answer("/pozisyon"), reply)  # Turkish alias
        self.assertIn("Stop", reply)
        self.assertIn("Hedef", reply)

    def test_pnl_command_combines_realized_unrealized_and_lifetime(self):
        bot = self.make_bot()
        bot.tick()
        trade = self.state["trades"]["DOGEUSDT"]
        self.ex.move("DOGEUSDT", trade["take_profit"] * 1.001)
        self.executor.manage()                                   # TP hit -> realized profit
        xrp = self.state["trades"]["XRPUSDT"]
        self.ex.move("XRPUSDT", xrp["entry"] - 0.4 * xrp["risk_distance"])   # short in profit, unrealized
        reply = bot._answer("/pnl")
        self.assertIn("Bugün gerçekleşen: <b>+", reply)
        self.assertIn("(1 işlem, 1 kazanan)", reply)
        self.assertIn("gerçekleşmemiş", reply)
        self.assertIn("Tüm zamanlar", reply)
        self.assertIn("isabet %100", reply)
        self.assertIn("DOGEUSDT LONG", reply)                    # recent trades list
        self.assertEqual(self.state["totals"]["trades"], 1)
        self.assertGreater(self.state["totals"]["realized"], 0)
        self.assertEqual(bot._answer("/kar"), reply)

    def test_commands_in_dry_run_and_on_exchange_failure(self):
        bot = self.make_bot(executor=False)
        self.assertIn("DRY-RUN", bot._answer("/positions"))
        self.assertIn("Tüm zamanlar", bot._answer("/pnl"))
        live = self.make_bot()
        self.ex.positions = lambda symbol=None: (_ for _ in ()).throw(BinanceError("down", -1000, 500))
        self.assertIn("Borsa okunamadı", live._answer("/positions"))
        self.assertIn("Borsa okunamadı", live._answer("/pnl"))

    def test_command_menu_is_registered_when_configured(self):
        class Session:
            def __init__(self): self.calls = []
            def post(self, url, json=None, timeout=None):
                self.calls.append((url.rsplit("/", 1)[1], json))
                class R:
                    status_code = 200
                    def json(self): return {"ok": True}
                return R()
        session = Session()
        TelegramNotifier("T", "1", session=session).set_commands([("/positions", "x"), ("/pnl", "y")])
        self.assertEqual(session.calls[0][0], "setMyCommands")
        self.assertEqual([c["command"] for c in session.calls[0][1]["commands"]], ["positions", "pnl"])

    def test_exchange_error_does_not_kill_the_loop(self):
        bot = self.make_bot()
        self.ex.positions = lambda symbol=None: (_ for _ in ()).throw(BinanceError("boom", -1000, 500))
        bot.tick()  # must not raise
        self.assertIn("boom", self.messages())

    def test_state_survives_restart(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s" / "state.json"
            state = StateStore(path)
            executor = Executor(self.ex, self.settings, state, self.notifier, make_infos(self.symbols), self.clock)
            executor.roll_day(15_000)
            executor.open_trade(self.cands["DOGEUSDT"])
            reloaded = StateStore(path)
            self.assertEqual(set(reloaded["trades"]), {"DOGEUSDT"})
            self.assertEqual(reloaded["trades"]["DOGEUSDT"]["side"], "LONG")


if __name__ == "__main__":
    unittest.main()
