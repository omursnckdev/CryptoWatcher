"""Test doubles: a controllable clock and an in-memory Binance futures testnet."""
from crypto_watcher.binance import BinanceError
from crypto_watcher.config import Settings
from crypto_watcher.data import SymbolInfo

# The order-mechanics tests were written for (and still exercise) the original geometry: 2x ATR stop, 2R target, no
# trend-strength / regime filter. The demo fixture's signals would not pass the new filters; strategy filters have their own tests.
LEGACY = {"atr_stop_multiplier": 2.0, "tp_r": 2.0, "min_trend_extension_atr": 0.0, "allow_counter_regime": True,
          "bnb_fee_discount": False, "maker_entry": False, "maker_take_profit": False, "maker_time_stop": False}


def legacy_settings(**overrides) -> Settings:
    return Settings(**{**LEGACY, **overrides})


class Clock:
    def __init__(self, now=1_791_284_400.0):
        self.now = now

    def __call__(self):
        return self.now


def make_infos(symbols):
    return {s: SymbolInfo(s, s.removesuffix("USDT"), 1e-6, 0.001, 0.001, 1e9, 5.0, 0) for s in symbols}


class FakeExchange:
    """Implements the TradingClient surface used by Executor/Bot; `move()` simulates price action."""

    def __init__(self, prices, clock, wallet=15_000.0):
        self.prices, self.clock, self.wallet = dict(prices), clock, wallet
        self.pos, self.algos, self.fills, self.calls = {}, {}, [], []
        self.fail_algo, self.hedge, self.leverage = set(), False, {}
        self.reject_leverage_above = None
        self.forced, self.force_error = [], None
        self.reject_reduce_only, self.fail_plain_market, self.algo_list_error = set(), set(), None
        self.orders, self.spread, self.fee_burn = {}, 0.0001, None
        self.fail_cancel, self.refuse_limit_tp, self.book_error, self.open_orders_error = False, False, None, None

    # -- helpers
    def _log(self, *call):
        self.calls.append(call)

    def kinds(self, name):
        return [c for c in self.calls if c[0] == name]

    def exchange_info(self):
        return {"symbols": [{"symbol": s, "baseAsset": s.removesuffix("USDT"), "quoteAsset": "USDT",
                             "contractType": "PERPETUAL", "status": "TRADING", "onboardDate": 0,
                             "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.000001"},
                                         {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000000000"},
                                         {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in self.prices]}

    # -- account
    def sync_time(self):
        return 0

    def position_mode(self):
        return self.hedge

    def set_one_way_mode(self):
        self.hedge = False

    def usdt_balance(self):
        unrealized = sum((self.prices[s] - p["entry"]) * p["amt"] for s, p in self.pos.items())
        used = sum(abs(p["amt"]) * p["entry"] / self.leverage.get(s, 1) for s, p in self.pos.items())
        return {"wallet": self.wallet, "available": self.wallet + unrealized - used, "unrealized": unrealized}

    def set_margin_type(self, symbol, margin_type):
        self._log("margin", symbol, margin_type)

    def set_leverage(self, symbol, leverage):
        if self.reject_leverage_above and leverage > self.reject_leverage_above:
            raise BinanceError("Leverage not valid", -4028, 400)
        self.leverage[symbol] = leverage
        self._log("leverage", symbol, leverage)
        return {"symbol": symbol, "leverage": leverage}

    def premium_index(self, symbol=None):
        return {"symbol": symbol, "markPrice": str(self.prices[symbol])}

    def positions(self, symbol=None):
        rows = []
        for s, p in self.pos.items():
            if symbol and s != symbol:
                continue
            mark = self.prices[s]
            rows.append({"symbol": s, "positionAmt": str(p["amt"]), "entryPrice": str(p["entry"]), "markPrice": str(mark),
                         "unRealizedProfit": str((mark - p["entry"]) * p["amt"])})
        return rows

    def force_orders(self, symbol, start_ms, limit=50):
        if self.force_error:
            raise self.force_error
        return [o for o in self.forced if o["symbol"] == symbol and o["time"] >= start_ms]

    def user_trades(self, symbol, start_ms, limit=100):
        return [f for f in self.fills if f["symbol"] == symbol and f["time"] >= start_ms]

    # -- orders
    def _fill(self, symbol, side, qty, price, rate=0.0005):
        signed = qty if side == "BUY" else -qty
        pos = self.pos.get(symbol)
        realized = 0.0
        if pos and pos["amt"] * signed < 0:
            closed = min(abs(signed), abs(pos["amt"]))
            realized = (price - pos["entry"]) * closed * (1 if pos["amt"] > 0 else -1)
            pos["amt"] += signed
            if abs(pos["amt"]) < 1e-12:
                del self.pos[symbol]
        elif pos:                                   # adding in the same direction: weighted average entry
            total = pos["amt"] + signed
            pos["entry"] = (pos["entry"] * abs(pos["amt"]) + price * qty) / abs(total)
            pos["amt"] = total
        else:
            self.pos[symbol] = {"amt": signed, "entry": price}
        self.wallet += realized - qty * price * rate
        self.fills.append({"symbol": symbol, "side": side, "price": str(price), "qty": str(qty),
                           "realizedPnl": str(realized), "commission": str(qty * price * rate), "maker": rate < 0.0005,
                           "commissionAsset": "USDT", "time": int(self.clock() * 1000)})

    def market_order(self, symbol, side, quantity, reduce_only=False, client_id=None):
        self._log("market", symbol, side, quantity, reduce_only)
        pos = self.pos.get(symbol)
        if reduce_only and symbol in self.reject_reduce_only:
            raise BinanceError("ReduceOnly Order is rejected.", -2022, 400)
        if not reduce_only and symbol in self.fail_plain_market:
            raise BinanceError("Margin is insufficient.", -2019, 400)
        if reduce_only and (not pos or pos["amt"] * (1 if side == "BUY" else -1) > 0):
            raise BinanceError("ReduceOnly rejected", -2022, 400)
        self._fill(symbol, side, quantity, self.prices[symbol])
        return {"status": "FILLED"}

    # -- maker order surface
    def book_ticker(self, symbol):
        if self.book_error:
            raise self.book_error
        p = self.prices[symbol]
        return {"symbol": symbol, "bidPrice": str(p * (1 - self.spread)), "askPrice": str(p * (1 + self.spread))}

    def set_fee_burn(self, on=True):
        self._log("fee_burn", on)
        if self.fee_burn:
            raise self.fee_burn
        return {"code": 200, "msg": "success"}

    def limit_order(self, symbol, side, quantity, price, client_id, post_only=True, reduce_only=False):
        self._log("limit", symbol, side, quantity, price, post_only, reduce_only)
        book = self.book_ticker(symbol)
        crosses = price >= float(book["askPrice"]) if side == "BUY" else price <= float(book["bidPrice"])
        if post_only and crosses:
            raise BinanceError("Due to the order could not be executed as maker, the Post Only order will be rejected.", -5022, 400)
        self.orders[client_id] = {"clientOrderId": client_id, "symbol": symbol, "side": side, "price": price, "origQty": quantity,
                                  "executedQty": 0.0, "status": "NEW", "reduceOnly": "true" if reduce_only else "false"}
        return dict(self.orders[client_id])

    def get_order(self, symbol, client_id):
        if client_id not in self.orders:
            raise BinanceError("Order does not exist.", -2013, 400)
        return dict(self.orders[client_id])

    def cancel_order(self, symbol, client_id):
        self._log("cancel_order", symbol, client_id)
        if self.fail_cancel:
            raise BinanceError("Timeout", -1007, 503)
        order = self.orders.get(client_id)
        if not order or order["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise BinanceError("Unknown order sent.", -2011, 400)
        order["status"] = "CANCELED"
        return dict(order)

    def open_orders(self, symbol=None):
        if self.open_orders_error:
            raise self.open_orders_error
        return [dict(o) for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED") and (not symbol or o["symbol"] == symbol)]

    def fill_order(self, client_id, quantity=None):
        """The resting limit order trades (as a maker) at its own price."""
        order = self.orders[client_id]
        qty = order["origQty"] - order["executedQty"] if quantity is None else quantity
        if order["reduceOnly"] == "true":
            pos = self.pos.get(order["symbol"])
            qty = min(qty, abs(pos["amt"])) if pos else 0.0
        if qty > 0:
            self._fill(order["symbol"], order["side"], qty, order["price"], rate=0.0002)
        order["executedQty"] += qty
        order["status"] = "FILLED" if order["executedQty"] >= order["origQty"] - 1e-12 or (order["reduceOnly"] == "true" and order["symbol"] not in self.pos) else "PARTIALLY_FILLED"

    def algo_order(self, symbol, side, order_type, trigger_price, client_id, quantity=None, price=None):
        self._log("algo", symbol, side, order_type, trigger_price, client_id)
        if order_type == "TAKE_PROFIT" and self.refuse_limit_tp:
            raise BinanceError("Order type not supported for this symbol", -4120, 400)
        if order_type in self.fail_algo:
            raise BinanceError("Order type not supported", -4120, 400)
        mark = self.prices[symbol]
        sell_stop = order_type == "STOP_MARKET" and side == "SELL" and trigger_price >= mark
        buy_stop = order_type == "STOP_MARKET" and side == "BUY" and trigger_price <= mark
        sell_tp = order_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and side == "SELL" and trigger_price <= mark
        buy_tp = order_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and side == "BUY" and trigger_price >= mark
        if sell_stop or buy_stop or sell_tp or buy_tp:
            raise BinanceError("Order would immediately trigger", -2021, 400)
        if quantity is None:  # Binance allows one closePosition stop per direction (-4130)
            clash = [a for a in self.algos.values() if a["symbol"] == symbol and a["orderType"] == order_type
                     and a["side"] == side and a["algoStatus"] == "NEW" and a["closePosition"]]
            if clash:
                raise BinanceError("An open stop or take profit order with closePosition in the direction exists", -4130, 400)
        self.algos[client_id] = {"clientAlgoId": client_id, "orderType": order_type, "symbol": symbol, "side": side,
                                 "triggerPrice": trigger_price, "algoStatus": "NEW", "closePosition": quantity is None,
                                 "quantity": quantity, "price": price}
        return {"clientAlgoId": client_id}

    def get_algo_order(self, client_id):
        return self.algos[client_id]

    def open_algo_orders(self, symbol=None):
        self._log("open_algo_orders", symbol)
        if self.algo_list_error:
            raise self.algo_list_error
        return [a for a in self.algos.values() if a["algoStatus"] == "NEW" and (not symbol or a["symbol"] == symbol)]

    def cancel_algo_order(self, client_id):
        self._log("cancel_algo", client_id)
        if client_id in self.algos and self.algos[client_id]["algoStatus"] == "NEW":
            self.algos[client_id]["algoStatus"] = "CANCELED"

    def cancel_all_algo_orders(self, symbol):
        self._log("cancel_all_algo", symbol)
        for a in self.algos.values():
            if a["symbol"] == symbol and a["algoStatus"] == "NEW":
                a["algoStatus"] = "CANCELED"

    def cancel_all_orders(self, symbol):
        self._log("cancel_all", symbol)
        for order in self.orders.values():
            if order["symbol"] == symbol and order["status"] in ("NEW", "PARTIALLY_FILLED"):
                order["status"] = "CANCELED"

    # -- simulation
    def strand(self, client_id, status="TRIGGERING"):
        """The exchange 'triggered' a conditional order that then never executed: it is no longer open and never fills."""
        self.algos[client_id]["algoStatus"] = status

    def liquidate(self, symbol, price, auto_close_type=None):
        """The exchange closes the position by force at `price` (a gap through the stop); stop/TP orders never trigger."""
        pos = self.pos[symbol]
        side = "SELL" if pos["amt"] > 0 else "BUY"
        qty = abs(pos["amt"])
        self.prices[symbol] = price
        self._fill(symbol, side, qty, price)
        order = {"symbol": symbol, "side": side, "origQty": str(qty), "avgPrice": str(price), "time": int(self.clock() * 1000)}
        if auto_close_type:
            order["autoCloseType"] = auto_close_type
        self.forced.append(order)

    def move(self, symbol, price):
        self.prices[symbol] = price
        for algo in list(self.algos.values()):
            if algo["symbol"] != symbol or algo["algoStatus"] != "NEW" or symbol not in self.pos:
                continue
            trig, sell = algo["triggerPrice"], algo["side"] == "SELL"
            hit = (price <= trig if sell else price >= trig) if algo["orderType"] == "STOP_MARKET" else \
                  (price >= trig if sell else price <= trig)
            if hit and algo["orderType"] == "TAKE_PROFIT":       # limit target: becomes a resting reduce-only limit order
                algo["algoStatus"] = "TRIGGERED"
                cid = algo["clientAlgoId"] + "-x"
                self.orders[cid] = {"clientOrderId": cid, "symbol": symbol, "side": algo["side"], "price": algo["price"],
                                    "origQty": algo["quantity"], "executedQty": 0.0, "status": "NEW", "reduceOnly": "true"}
            elif hit:
                algo["algoStatus"] = "TRIGGERED"
                self._fill(symbol, algo["side"], abs(self.pos[symbol]["amt"]), trig)
        for order in list(self.orders.values()):                 # resting limit orders trade when the price reaches them
            if order["symbol"] != symbol or order["status"] not in ("NEW", "PARTIALLY_FILLED"):
                continue
            if order["reduceOnly"] == "true" and symbol not in self.pos:
                order["status"] = "EXPIRED"
                continue
            if (price >= order["price"]) if order["side"] == "SELL" else (price <= order["price"]):
                self.fill_order(order["clientOrderId"])
