"""Test doubles: a controllable clock and an in-memory Binance futures testnet."""
from crypto_watcher.binance import BinanceError
from crypto_watcher.data import SymbolInfo


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

    def user_trades(self, symbol, start_ms, limit=100):
        return [f for f in self.fills if f["symbol"] == symbol and f["time"] >= start_ms]

    # -- orders
    def _fill(self, symbol, side, qty, price):
        signed = qty if side == "BUY" else -qty
        pos = self.pos.get(symbol)
        realized = 0.0
        if pos and pos["amt"] * signed < 0:
            closed = min(abs(signed), abs(pos["amt"]))
            realized = (price - pos["entry"]) * closed * (1 if pos["amt"] > 0 else -1)
            pos["amt"] += signed
            if abs(pos["amt"]) < 1e-12:
                del self.pos[symbol]
        elif pos:
            raise AssertionError("fake does not model adding to positions")
        else:
            self.pos[symbol] = {"amt": signed, "entry": price}
        self.wallet += realized - qty * price * 0.0005
        self.fills.append({"symbol": symbol, "side": side, "price": str(price), "qty": str(qty),
                           "realizedPnl": str(realized), "commission": str(qty * price * 0.0005),
                           "commissionAsset": "USDT", "time": int(self.clock() * 1000)})

    def market_order(self, symbol, side, quantity, reduce_only=False, client_id=None):
        self._log("market", symbol, side, quantity, reduce_only)
        pos = self.pos.get(symbol)
        if reduce_only and (not pos or pos["amt"] * (1 if side == "BUY" else -1) > 0):
            raise BinanceError("ReduceOnly rejected", -2022, 400)
        self._fill(symbol, side, quantity, self.prices[symbol])
        return {"status": "FILLED"}

    def algo_order(self, symbol, side, order_type, trigger_price, client_id, quantity=None):
        self._log("algo", symbol, side, order_type, trigger_price, client_id)
        if order_type in self.fail_algo:
            raise BinanceError("Order type not supported", -4120, 400)
        mark = self.prices[symbol]
        sell_stop = order_type == "STOP_MARKET" and side == "SELL" and trigger_price >= mark
        buy_stop = order_type == "STOP_MARKET" and side == "BUY" and trigger_price <= mark
        sell_tp = order_type == "TAKE_PROFIT_MARKET" and side == "SELL" and trigger_price <= mark
        buy_tp = order_type == "TAKE_PROFIT_MARKET" and side == "BUY" and trigger_price >= mark
        if sell_stop or buy_stop or sell_tp or buy_tp:
            raise BinanceError("Order would immediately trigger", -2021, 400)
        if quantity is None:  # Binance allows one closePosition stop per direction (-4130)
            clash = [a for a in self.algos.values() if a["symbol"] == symbol and a["orderType"] == order_type
                     and a["side"] == side and a["algoStatus"] == "NEW" and a["closePosition"]]
            if clash:
                raise BinanceError("An open stop or take profit order with closePosition in the direction exists", -4130, 400)
        self.algos[client_id] = {"clientAlgoId": client_id, "orderType": order_type, "symbol": symbol, "side": side,
                                 "triggerPrice": trigger_price, "algoStatus": "NEW", "closePosition": quantity is None,
                                 "quantity": quantity}
        return {"clientAlgoId": client_id}

    def get_algo_order(self, client_id):
        return self.algos[client_id]

    def open_algo_orders(self, symbol=None):
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

    # -- simulation
    def move(self, symbol, price):
        self.prices[symbol] = price
        for algo in list(self.algos.values()):
            if algo["symbol"] != symbol or algo["algoStatus"] != "NEW" or symbol not in self.pos:
                continue
            trig, sell = algo["triggerPrice"], algo["side"] == "SELL"
            hit = (price <= trig if sell else price >= trig) if algo["orderType"] == "STOP_MARKET" else \
                  (price >= trig if sell else price <= trig)
            if hit:
                algo["algoStatus"] = "TRIGGERED"
                self._fill(symbol, algo["side"], abs(self.pos[symbol]["amt"]), trig)
