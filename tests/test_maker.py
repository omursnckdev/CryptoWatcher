import unittest
from crypto_watcher.binance import BinanceError
from crypto_watcher.config import Settings
from crypto_watcher.executor import Executor
from helpers import legacy_settings, make_infos
from test_executor import Base

MAKER = dict(maker_entry=True, maker_take_profit=True, maker_time_stop=True, bnb_fee_discount=True, maker_wait_seconds=20)


class MakerBase(Base):
    def setUp(self):
        super().setUp()
        self.settings = legacy_settings(**MAKER)
        self.on_sleep = None
        self.slept = 0.0
        self.executor = Executor(self.ex, self.settings, self.state, self.notifier, make_infos(self.symbols), self.clock,
                                 sleep=self.sleep)
        self.executor.roll_day(15_000.0)

    def sleep(self, seconds):
        self.clock.now += seconds
        self.slept += seconds
        if self.on_sleep:
            self.on_sleep()

    def maker_orders(self):
        return [o for o in self.ex.orders.values() if o["clientOrderId"].startswith("cw-mk-")]

    def fill_latest(self, fraction=1.0):
        order = self.maker_orders()[-1]
        if order["status"] == "NEW":
            self.ex.fill_order(order["clientOrderId"], order["origQty"] * fraction)

    def markets(self):
        return self.ex.kinds("market")


class MakerEntryTests(MakerBase):
    def test_filled_maker_entry_sends_no_market_order(self):
        self.on_sleep = self.fill_latest
        trade = self.open("DOGEUSDT")
        (order,) = self.maker_orders()
        bid = self.prices["DOGEUSDT"] * (1 - self.ex.spread)
        self.assertEqual((order["side"], order["status"]), ("BUY", "FILLED"))
        self.assertAlmostEqual(order["price"], bid)
        self.assertEqual(self.markets(), [])
        self.assertAlmostEqual(trade["quantity"], order["origQty"])
        self.assertTrue(self.ex.fills[0]["maker"])
        self.assertAlmostEqual(trade["entry"], bid)                     # stop / target are computed from the real fill
        self.assertLess(self.slept, 20)                                  # no need to wait the full window

    def test_unfilled_maker_entry_goes_to_market_after_the_wait(self):
        trade = self.open("DOGEUSDT")
        (order,) = self.maker_orders()
        self.assertEqual(order["status"], "CANCELED")
        self.assertGreaterEqual(self.slept, 20)
        (market,) = self.markets()
        self.assertAlmostEqual(market[3], order["origQty"])              # the whole planned size, at market
        self.assertAlmostEqual(trade["quantity"], order["origQty"])

    def test_partial_fill_sends_only_the_rest_to_market(self):
        self.on_sleep = lambda: self.fill_latest(0.4) if self.maker_orders()[-1]["executedQty"] == 0 else None
        trade = self.open("DOGEUSDT")
        (order,) = self.maker_orders()
        (market,) = self.markets()
        self.assertAlmostEqual(market[3], order["origQty"] - order["executedQty"], places=6)
        self.assertAlmostEqual(trade["quantity"], order["origQty"], places=6)   # never more than planned

    def test_refused_post_only_goes_straight_to_market(self):
        self.ex.spread = -0.001                                          # our side of the book would take liquidity
        self.open("DOGEUSDT")
        self.assertEqual(self.maker_orders(), [])
        self.assertEqual(len(self.markets()), 1)
        self.assertEqual(self.slept, 0)

    def test_missing_order_book_goes_to_market(self):
        self.ex.book_error = BinanceError("timeout", -1007, 503)
        self.open("DOGEUSDT")
        self.assertEqual(len(self.markets()), 1)

    def test_a_fill_right_before_the_cancel_never_doubles_the_position(self):
        real_cancel = self.ex.cancel_order

        def fill_then_cancel(symbol, cid):
            self.ex.fill_order(cid)                                      # it trades at the last moment...
            return real_cancel(symbol, cid)                              # ...so the cancel says "unknown order"
        self.ex.cancel_order = fill_then_cancel
        trade = self.open("DOGEUSDT")
        self.assertEqual(self.markets(), [])
        self.assertAlmostEqual(abs(self.ex.pos["DOGEUSDT"]["amt"]), self.maker_orders()[0]["origQty"])
        self.assertAlmostEqual(trade["quantity"], self.maker_orders()[0]["origQty"])

    def test_an_order_that_cannot_be_cancelled_gets_no_market_order_on_top(self):
        self.ex.fail_cancel = True
        self.ex.cancel_all_orders = lambda symbol: (_ for _ in ()).throw(BinanceError("timeout", -1007, 503))
        self.assertIsNone(self.executor.open_trade(self.cands["DOGEUSDT"]))   # nothing filled, nothing sent at market
        self.assertEqual(self.markets(), [])
        self.assertIn("giriş emri gerçekleşmedi", self.state["last_error"]["text"])

    def test_short_entry_rests_on_the_ask(self):
        self.on_sleep = self.fill_latest
        self.open("XRPUSDT")
        (order,) = self.maker_orders()
        self.assertEqual(order["side"], "SELL")
        self.assertAlmostEqual(order["price"], self.prices["XRPUSDT"] * (1 + self.ex.spread))


class MakerTargetTests(MakerBase):
    def setUp(self):
        super().setUp()
        self.on_sleep = self.fill_latest

    def target_algo(self, trade):
        return self.ex.algos[trade["tp_id"]]

    def test_target_is_a_limit_order_placed_just_before_the_target(self):
        trade = self.open("DOGEUSDT")
        algo = self.target_algo(trade)
        self.assertEqual((algo["orderType"], algo["price"], trade["tp_kind"]), ("TAKE_PROFIT", trade["take_profit"], "limit"))
        self.assertAlmostEqual(algo["triggerPrice"], trade["take_profit"] - 0.1 * trade["risk_distance"], places=5)
        self.assertAlmostEqual(algo["quantity"], trade["quantity"])

    def test_target_fills_as_a_maker_at_the_target_price(self):
        trade = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", trade["take_profit"] * 1.001)
        self.executor.manage()
        self.assertEqual(self.state["history"][-1]["reason"], "TAKE_PROFIT")
        exit_fill = self.ex.fills[-1]
        self.assertTrue(exit_fill["maker"])
        self.assertAlmostEqual(float(exit_fill["price"]), trade["take_profit"])
        self.assertGreater(self.state["history"][-1]["pnl"], 0)

    def test_refused_limit_target_falls_back_to_a_market_target(self):
        self.ex.refuse_limit_tp = True
        trade = self.open("DOGEUSDT")
        self.assertEqual((self.target_algo(trade)["orderType"], trade["tp_kind"]), ("TAKE_PROFIT_MARKET", "market"))

    def test_guard_accepts_a_triggered_target_that_is_still_resting(self):
        trade = self.open("DOGEUSDT")
        just_below = trade["take_profit"] - 0.05 * trade["risk_distance"]   # past the trigger, short of the target
        self.ex.move("DOGEUSDT", just_below)
        self.assertEqual(self.target_algo(trade)["algoStatus"], "TRIGGERED")
        before = len(self.ex.kinds("algo"))
        for _ in range(3):
            self.clock.now += 61
            self.executor.manage()
        tp_algos = [c for c in self.ex.kinds("algo")[before:] if c[3] in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET")]
        self.assertEqual(tp_algos, [])                                   # no second target stacked on the resting one
        self.assertIn("DOGEUSDT", self.state["trades"])

    def test_restart_keeps_a_triggered_target_that_is_still_resting(self):
        trade = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", trade["take_profit"] - 0.05 * trade["risk_distance"])
        before = len(self.ex.kinds("algo"))
        self.executor.reconcile()
        self.assertEqual([c for c in self.ex.kinds("algo")[before:] if c[3].startswith("TAKE_PROFIT")], [])
        self.ex.orders.clear()                                           # the resting order is gone too -> a new target is placed
        self.executor.reconcile()
        self.assertEqual(len([c for c in self.ex.kinds("algo")[before:] if c[3] == "TAKE_PROFIT"]), 1)

    def test_target_check_is_skipped_when_open_orders_are_unknown(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["tp_id"], "REJECTED")
        self.ex.open_orders_error = BinanceError("timeout", -1007, 503)
        before = len(self.ex.kinds("algo"))
        for _ in range(3):
            self.clock.now += 61
            self.executor.manage()
        self.assertEqual(len(self.ex.kinds("algo")), before)


class MakerTimeStopTests(MakerBase):
    def test_time_stop_leaves_as_a_maker(self):
        self.on_sleep = self.fill_latest
        self.open("DOGEUSDT")
        self.clock.now += self.settings.max_hold_hours * 3600 + 1
        self.executor.manage()
        self.assertEqual(self.ex.pos, {})
        self.assertEqual(self.state["history"][-1]["reason"], "TIME_STOP")
        exit_order = self.maker_orders()[-1]
        self.assertEqual((exit_order["side"], exit_order["reduceOnly"]), ("SELL", "true"))
        self.assertEqual(self.markets(), [])
        self.assertTrue(self.ex.fills[-1]["maker"])

    def test_unfilled_time_stop_falls_back_to_the_closing_ladder(self):
        self.on_sleep = lambda: self.fill_latest() if len(self.maker_orders()) == 1 else None   # entry fills, exit does not
        self.open("DOGEUSDT")
        self.clock.now += self.settings.max_hold_hours * 3600 + 1
        self.executor.manage()
        self.assertEqual(self.ex.pos, {})
        self.assertEqual(self.state["history"][-1]["reason"], "TIME_STOP")
        self.assertEqual([c[4] for c in self.markets()], [True])        # one reduce-only market order after the maker try
        self.assertEqual(self.maker_orders()[-1]["status"], "CANCELED")

    def test_urgent_closes_never_wait_for_a_maker_fill(self):
        self.on_sleep = self.fill_latest
        trade = self.open("DOGEUSDT")
        waited = self.slept
        self.ex.strand(trade["sl_id"])
        self.ex.prices["DOGEUSDT"] = trade["stop"] * 0.998              # price through a vanished stop
        for _ in range(2):
            self.clock.now += 61
            self.executor.manage()
        self.assertEqual(self.state["history"][-1]["reason"], "STOP_LOSS")
        self.assertEqual(self.slept, waited)                             # closed at market immediately


class FeeTests(MakerBase):
    def test_bnb_fee_discount_is_requested_and_refusal_is_harmless(self):
        self.executor.prepare()
        self.assertEqual(self.ex.kinds("fee_burn"), [("fee_burn", True)])
        self.ex.fee_burn = BinanceError("Not supported on testnet", -1000, 400)
        self.executor.prepare()                                          # must not raise

    def test_off_means_off(self):
        self.executor.s = legacy_settings()
        self.executor.prepare()
        self.assertEqual(self.ex.kinds("fee_burn"), [])

    def test_defaults_and_validation(self):
        s = Settings()
        self.assertEqual((s.bnb_fee_discount, s.maker_entry, s.maker_take_profit, s.maker_time_stop, s.maker_wait_seconds,
                          s.maker_tp_trigger_r), (True, True, True, True, 20, 0.1))
        with self.assertRaises(ValueError):
            Settings(maker_wait_seconds=0)
        with self.assertRaises(ValueError):
            Settings(maker_tp_trigger_r=-0.1)


if __name__ == "__main__":
    unittest.main()
