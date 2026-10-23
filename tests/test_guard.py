import unittest
from crypto_watcher.binance import BinanceError
from crypto_watcher.config import Settings
from crypto_watcher.executor import Executor, RESTORE_ATTEMPTS
from helpers import make_infos, legacy_settings
from test_executor import Base


class GuardBase(Base):
    def passes(self, n=1, seconds=61):
        for _ in range(n):
            self.clock.now += seconds
            self.executor.manage()

    def algo_calls(self):
        return self.ex.kinds("algo")

    def live_stop(self, symbol):
        return [a for a in self.ex.algos.values() if a["symbol"] == symbol and a["orderType"] == "STOP_MARKET" and a["algoStatus"] == "NEW"]


class VanishedStopTests(GuardBase):
    def test_stop_is_placed_again_after_two_agreeing_passes(self):
        trade = self.open("DOGEUSDT")
        old_id = trade["sl_id"]
        self.ex.strand(old_id)                                     # triggered on the exchange but stuck
        before = len(self.algo_calls())
        self.passes(1)
        self.assertEqual((trade["sl_id"], len(self.algo_calls())), (old_id, before))     # one pass is only a strike
        self.passes(1)
        self.assertNotEqual(trade["sl_id"], old_id)
        (stop,) = self.live_stop("DOGEUSDT")
        self.assertEqual((stop["side"], stop["triggerPrice"], stop["closePosition"]), ("SELL", trade["stop"], True))
        self.assertIn("DOGEUSDT stop yeniden kondu", self.messages())
        self.assertIn("DOGEUSDT", self.state["trades"])
        self.assertEqual(self.ex.kinds("market")[1:], [])           # no close: the price was fine

    def test_short_trade_gets_a_buy_stop(self):
        trade = self.open("XRPUSDT")
        self.ex.strand(trade["sl_id"], "REJECTED")
        self.passes(2)
        (stop,) = self.live_stop("XRPUSDT")
        self.assertEqual((stop["side"], stop["triggerPrice"]), ("BUY", trade["stop"]))

    def test_a_stop_that_reappears_resets_the_strikes(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["sl_id"])
        self.passes(1)
        self.ex.algos[trade["sl_id"]]["algoStatus"] = "NEW"
        self.passes(1)
        self.assertEqual(trade.get("guard_misses"), 0)
        self.ex.strand(trade["sl_id"])
        before = len(self.algo_calls())
        self.passes(1)
        self.assertEqual(len(self.algo_calls()), before)            # starts counting again from one

    def test_price_through_the_missing_stop_closes_the_position(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["sl_id"])
        self.ex.prices["DOGEUSDT"] = trade["stop"] * 0.998         # below the stop; the stranded order cannot fill
        self.passes(2)
        self.assertEqual(self.state["trades"], {})
        self.assertEqual(self.ex.pos, {})
        self.assertEqual(self.state["history"][-1]["reason"], "STOP_LOSS")
        self.assertLess(self.state["history"][-1]["pnl"], 0)
        self.assertIn("DOGEUSDT LONG kapandı", self.messages())

    def test_short_through_its_stop_is_closed_too(self):
        trade = self.open("XRPUSDT")
        self.ex.strand(trade["sl_id"])
        self.ex.prices["XRPUSDT"] = trade["stop"] * 1.002
        self.passes(2)
        self.assertEqual(self.state["history"][-1]["reason"], "STOP_LOSS")

    def test_breakeven_stop_that_vanished_is_labelled_breakeven(self):
        trade = self.open("DOGEUSDT")
        self.ex.move("DOGEUSDT", trade["entry"] + 1.1 * trade["risk_distance"])
        self.passes(1)
        self.assertTrue(trade["breakeven_done"])
        self.ex.strand(trade["sl_id"])
        self.ex.prices["DOGEUSDT"] = trade["entry"]                  # back through the breakeven stop
        self.passes(2)
        self.assertEqual(self.state["history"][-1]["reason"], "BREAKEVEN_STOP")

    def test_immediate_trigger_answer_means_the_price_is_through_the_stop(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["sl_id"])
        real = self.ex.algo_order

        def refuse(symbol, side, order_type, trigger, cid, quantity=None):
            raise BinanceError("Order would immediately trigger", -2021, 400)
        self.ex.algo_order = refuse
        self.passes(2)
        self.assertEqual(self.state["trades"], {})
        self.assertEqual(self.state["history"][-1]["reason"], "STOP_LOSS")

    def test_closeposition_conflict_falls_back_to_a_quantity_stop(self):
        trade = self.open("DOGEUSDT")
        self.ex.algos[trade["sl_id"]]["closePosition"] = True
        self.ex.strand(trade["sl_id"])
        self.ex.algo_order("DOGEUSDT", "SELL", "STOP_MARKET", trade["stop"] * 0.99, "someone-else")   # another closePosition stop
        self.passes(2)
        mine = [a for a in self.live_stop("DOGEUSDT") if a["clientAlgoId"] == trade["sl_id"]]
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0]["closePosition"])
        self.assertAlmostEqual(mine[0]["quantity"], abs(self.ex.pos["DOGEUSDT"]["amt"]))

    def test_a_stop_that_cannot_be_placed_is_retried_then_the_position_is_closed(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["sl_id"])

        def refuse(symbol, side, order_type, trigger, cid, quantity=None):
            raise BinanceError("Order type not supported", -4120, 400)
        self.ex.algo_order = refuse
        self.passes(2)                                                # strike two: first attempt
        self.assertIn("DOGEUSDT", self.state["trades"])
        self.assertIn("KORUMASIZ POZİSYON", self.messages())
        self.assertIn(f"1/{RESTORE_ATTEMPTS}", self.messages())
        self.passes(RESTORE_ATTEMPTS - 2)
        self.assertIn("DOGEUSDT", self.state["trades"])               # attempts 2.. still keep the position
        self.passes(1)
        self.assertEqual(self.state["trades"], {})
        self.assertEqual(self.state["history"][-1]["reason"], "GUARD_CLOSE")
        self.assertIn("stop kurulamadığı için kapatıldı", self.messages())


class VanishedTargetTests(GuardBase):
    def test_missing_target_is_placed_again(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["tp_id"], "REJECTED")
        old = trade["tp_id"]
        self.passes(2)
        self.assertNotEqual(trade["tp_id"], old)
        tps = [a for a in self.ex.algos.values() if a["orderType"] == "TAKE_PROFIT_MARKET" and a["algoStatus"] == "NEW"]
        self.assertEqual([(a["side"], a["triggerPrice"]) for a in tps if a["symbol"] == "DOGEUSDT"], [("SELL", trade["take_profit"])])
        self.assertNotIn("DOGEUSDT stop yeniden", self.messages())    # quiet: only the stop is Telegram-worthy
        self.assertIn("DOGEUSDT", self.state["trades"])

    def test_target_already_reached_closes_with_a_profit(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["tp_id"], "REJECTED")
        self.ex.prices["DOGEUSDT"] = trade["take_profit"] * 1.001
        self.passes(3)                                                # pass 1 moves the stop to breakeven (guard waits), 2 and 3 agree
        self.assertEqual(self.state["history"][-1]["reason"], "TAKE_PROFIT")
        self.assertGreater(self.state["history"][-1]["pnl"], 0)

    def test_price_exactly_at_the_missing_target_counts_as_reached(self):
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["tp_id"], "REJECTED")
        self.ex.prices["DOGEUSDT"] = trade["take_profit"]
        self.passes(3)
        self.assertEqual(self.state["history"][-1]["reason"], "TAKE_PROFIT")

    def test_trade_without_a_target_is_left_alone(self):
        trade = self.open("DOGEUSDT")
        trade["tp_id"] = None
        before = len(self.algo_calls())
        self.passes(3)
        self.assertEqual(len(self.algo_calls()), before)


class ClosingLadderTests(GuardBase):
    def test_time_stop_falls_back_to_a_plain_order_when_reduce_only_is_rejected(self):
        trade = self.open("DOGEUSDT")
        qty = abs(self.ex.pos["DOGEUSDT"]["amt"])
        self.ex.reject_reduce_only.add("DOGEUSDT")
        self.clock.now += self.settings.max_hold_hours * 3600 + 1
        self.executor.manage()
        self.assertEqual(self.ex.pos, {})
        self.assertEqual(self.state["history"][-1]["reason"], "TIME_STOP")
        orders = [c for c in self.ex.kinds("market")[1:]]
        self.assertEqual([(c[2], c[3], c[4]) for c in orders], [("SELL", qty, True), ("SELL", qty, False)])
        self.assertIn("sadece-azalt emri reddedildi", self.messages())

    def test_nothing_is_sent_for_a_position_that_is_already_gone(self):
        self.open("DOGEUSDT")
        self.ex.pos.clear()
        before = len(self.ex.kinds("market"))
        self.assertEqual(self.executor._close_now("DOGEUSDT"), (True, False, None))
        self.assertEqual(len(self.ex.kinds("market")), before)

    def test_a_position_that_disappears_between_the_two_reads_gets_no_plain_order(self):
        self.open("DOGEUSDT")
        self.ex.reject_reduce_only.add("DOGEUSDT")
        real, calls = self.ex.positions, []

        def vanishing(symbol=None):
            calls.append(1)
            return real(symbol) if len(calls) == 1 else []
        self.ex.positions = vanishing
        before = len(self.ex.kinds("market"))
        self.assertEqual(self.executor._close_now("DOGEUSDT"), (True, False, None))
        self.assertEqual(len(self.ex.kinds("market")) - before, 1)      # only the rejected reduce-only attempt, no plain order

    def test_a_position_that_changed_direction_is_never_touched(self):
        self.open("DOGEUSDT")
        self.ex.reject_reduce_only.add("DOGEUSDT")
        real, calls = self.ex.positions, []

        def flipping(symbol=None):
            rows = real(symbol)
            calls.append(1)
            if len(calls) >= 2:
                rows = [{**r, "positionAmt": str(-float(r["positionAmt"]))} for r in rows]
            return rows
        self.ex.positions = flipping
        before = len(self.ex.kinds("market"))
        closed, fallback, error = self.executor._close_now("DOGEUSDT")
        self.assertEqual((closed, fallback), (False, False))
        self.assertEqual(len(self.ex.kinds("market")) - before, 1)      # only the rejected reduce-only attempt

    def test_when_everything_fails_the_warning_is_throttled_and_retries_are_spaced(self):
        self.open("DOGEUSDT")
        self.ex.reject_reduce_only.add("DOGEUSDT")
        self.ex.fail_plain_market.add("DOGEUSDT")
        self.clock.now += self.settings.max_hold_hours * 3600 + 1
        self.executor.manage()
        warnings = lambda: self.messages().count("pozisyon kapatılamadı")
        self.assertEqual(warnings(), 1)
        self.assertIn("DOGEUSDT", self.state["trades"])
        attempts = len(self.ex.kinds("market"))
        self.clock.now += 20
        self.executor.manage()
        self.assertEqual(len(self.ex.kinds("market")), attempts)        # backed off for 60 s
        self.passes(1)                                                  # retried, but no second Telegram message yet
        self.assertGreater(len(self.ex.kinds("market")), attempts)
        self.assertEqual(warnings(), 1)
        self.passes(1, seconds=1900)                                    # 30+ minutes later: warn again
        self.assertEqual(warnings(), 2)

    def test_emergency_close_after_a_failed_stop_also_uses_the_ladder(self):
        self.ex.reject_reduce_only.add("DOGEUSDT")
        self.ex.fail_algo.add("STOP_MARKET")
        self.assertIsNone(self.executor.open_trade(self.cands["DOGEUSDT"]))
        self.assertEqual(self.ex.pos, {})
        self.assertIn("normal piyasa emriyle kapatıldı", self.messages())


class GuardScheduleTests(GuardBase):
    def test_runs_at_most_once_per_interval(self):
        self.open("DOGEUSDT")
        self.executor.manage()
        self.clock.now += 20
        self.executor.manage()
        self.clock.now += 20
        self.executor.manage()
        self.assertEqual(len(self.ex.kinds("open_algo_orders")), 1)
        self.passes(1)
        self.assertEqual(len(self.ex.kinds("open_algo_orders")), 2)

    def test_can_be_switched_off(self):
        settings = legacy_settings(min_quote_volume=1.0, stop_guard_enabled=False)
        self.executor = Executor(self.ex, settings, self.state, self.notifier, make_infos(self.symbols), self.clock)
        self.executor.roll_day(15_000.0)
        trade = self.open("DOGEUSDT")
        self.ex.strand(trade["sl_id"])
        self.passes(4)
        self.assertEqual(self.ex.kinds("open_algo_orders"), [])
        self.assertIn("DOGEUSDT", self.state["trades"])

    def test_listing_failure_is_harmless(self):
        trade = self.open("DOGEUSDT")
        self.ex.algo_list_error = BinanceError("timeout", -1007, 503)
        self.passes(3)
        self.assertIn("DOGEUSDT", self.state["trades"])
        self.assertIn("kontrolü atlandı", self.state["last_error"]["text"])
        self.assertEqual(self.ex.kinds("market")[1:], [])

    def test_a_normal_lifecycle_triggers_no_replacement(self):
        trade = self.open("DOGEUSDT")
        self.passes(2)                                                # healthy
        self.ex.move("DOGEUSDT", trade["entry"] + 1.1 * trade["risk_distance"])
        self.passes(3)                                                # breakeven swaps the stop id, guard must not fight it
        self.assertTrue(trade["breakeven_done"])
        self.assertNotIn("yeniden kondu", self.messages())
        self.assertEqual(len(self.live_stop("DOGEUSDT")), 1)

    def test_trade_from_an_older_version_without_guard_fields_works(self):
        trade = self.open("DOGEUSDT")
        for key in ("guard_misses", "restore_failures", "crit_ms", "force_next_ms"):
            trade.pop(key, None)
        self.ex.strand(trade["sl_id"])
        self.passes(2)
        self.assertIn("stop yeniden kondu", self.messages())


if __name__ == "__main__":
    unittest.main()
