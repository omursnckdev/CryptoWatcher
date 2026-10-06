"""Order execution on the Binance futures TESTNET and trade lifecycle management.

Safety invariants:
  * a position is never left without an exchange-side stop: if the stop cannot be placed the position is closed;
  * the stop is moved (new one placed first, then the old one cancelled), never removed;
  * every order mutation is notified on Telegram.
"""
from datetime import datetime, timezone
import logging
import time
import zlib
from .binance import BinanceError, ROUND_HALF_UP, round_step
from .config import Settings
from .data import SymbolInfo
from .strategy import risk_plan
from . import telegram as tg

log = logging.getLogger(__name__)
DEAD_ALGO = {"NEW", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def client_id(kind: str, symbol: str, ms: int) -> str:
    """Unique per (symbol, time); within Binance's 36-char limit."""
    return f"cw-{kind}-{ms:x}{zlib.crc32(symbol.encode()) & 0xfffff:05x}"


def _today(clock) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).strftime("%Y-%m-%d")


class Executor:
    def __init__(self, client, settings: Settings, state, notifier, infos: dict[str, SymbolInfo], clock=time.time):
        self.client, self.s, self.state, self.notify = client, settings, state, notifier
        self.infos, self._clock = infos, clock

    # ---- account / limits -------------------------------------------------
    def prepare(self):
        self.client.sync_time()
        if self.client.position_mode():
            self.client.set_one_way_mode()
            if self.client.position_mode():
                raise RuntimeError("Account is in hedge mode and could not be switched to one-way mode")

    def balance(self) -> dict:
        return self.client.usdt_balance()

    def capital(self, balance: dict | None = None) -> float:
        balance = balance or self.balance()
        equity = balance["wallet"] + balance["unrealized"]
        return min(equity, self.s.capital_usdt) if self.s.capital_usdt > 0 else equity

    def new_day(self) -> bool:
        return self.state["daily"].get("date") != _today(self._clock)

    def roll_day(self, equity: float) -> dict | None:
        """Start a new UTC day; returns the finished day's summary (if any)."""
        today = _today(self._clock)
        daily = self.state["daily"]
        if daily.get("date") == today:
            return None
        finished = daily if daily.get("date") else None
        self.state["daily"] = {"date": today, "start_equity": equity, "realized": 0.0, "trades": 0, "wins": 0}
        self.state.save()
        return finished

    def daily_loss_locked(self) -> bool:
        daily = self.state["daily"]
        if not daily.get("start_equity"):
            return False
        return daily["realized"] <= -self.s.max_daily_loss_fraction * min(
            daily["start_equity"], self.s.capital_usdt or daily["start_equity"])

    def can_open(self, candidate: dict) -> str | None:
        """Reason NOT to open, or None."""
        symbol, side = candidate["symbol"], candidate["side"]
        trades = self.state["trades"]
        if self.state["paused"]:
            return "paused"
        if symbol not in self.infos:
            return "symbol not tradable on testnet"
        if symbol in trades:
            return "already in a position"
        if self._clock() < self.state["cooldowns"].get(symbol, 0):
            return "cooldown"
        if len(trades) >= self.s.max_open_positions:
            return "max open positions"
        if sum(1 for t in trades.values() if t["side"] == side) >= self.s.max_same_direction:
            return f"max same-direction ({side}) positions"
        if self.daily_loss_locked():
            return "daily loss limit reached"
        return None

    def _alert(self, key: str, text: str, every: int = 1800):
        """Throttled warning so a persistent error does not flood the chat."""
        last = self.state["alerts"].get(key, 0)
        if self._clock() - last >= every:
            self.state["alerts"][key] = self._clock()
            self.notify.send(tg.fmt_warning(text))

    # ---- opening ------------------------------------------------------------
    def _set_leverage(self, symbol: str) -> int:
        leverage = self.s.leverage
        while True:
            try:
                return int(self.client.set_leverage(symbol, leverage).get("leverage", leverage))
            except BinanceError as error:
                if error.code != -4028 or leverage == 1:  # -4028: leverage not valid for this symbol
                    raise
                leverage = max(1, leverage // 2)

    def open_trade(self, candidate: dict) -> dict | None:
        symbol, side = candidate["symbol"], candidate["side"]
        info = self.infos[symbol]
        sign, order_side, exit_side = (1, "BUY", "SELL") if side == "LONG" else (-1, "SELL", "BUY")
        try:
            if self.client.positions(symbol):
                self._alert(f"untracked:{symbol}", f"{symbol}: borsada izlenmeyen bir pozisyon var, yeni işlem açılmadı.")
                return None
            balance = self.balance()
            capital = self.capital(balance)
            self.client.set_margin_type(symbol, self.s.margin_type)
            leverage = self._set_leverage(symbol)
            mark = float(self.client.premium_index(symbol)["markPrice"])
            plan = risk_plan(side, mark, candidate["atr_pct"] * mark, capital, self.s, info, leverage)
            if plan["margin"] > balance["available"] * 0.95:
                raise ValueError(f"Insufficient available balance ({balance['available']:.2f} USDT) for {plan['margin']:.2f} margin")
        except (BinanceError, ValueError) as error:
            self._alert(f"open:{symbol}", f"{symbol} {side} açılamadı (emir öncesi): {error}", every=600)
            return None
        opened_ms = int(self._clock() * 1000)
        try:
            self.client.market_order(symbol, order_side, plan["quantity"], client_id=client_id("e", symbol, opened_ms))
        except BinanceError as error:
            log.error("Entry order error for %s: %s (checking exchange position)", symbol, error)
        positions = self.client.positions(symbol)
        if not positions:
            self._alert(f"open:{symbol}", f"{symbol} {side} giriş emri gerçekleşmedi.", every=600)
            return None
        position = positions[0]
        entry, quantity = float(position["entryPrice"]), abs(float(position["positionAmt"]))
        distance = plan["risk_distance"]
        stop = round_step(entry - sign * distance, info.tick, ROUND_HALF_UP)
        take = round_step(entry + sign * self.s.tp_r * distance, info.tick, ROUND_HALF_UP)
        trade = {"symbol": symbol, "side": side, "entry": entry, "quantity": quantity, "stop": stop, "initial_stop": stop,
                 "take_profit": take, "risk_distance": distance, "leverage": leverage, "margin_type": self.s.margin_type,
                 "opened_ms": opened_ms, "sl_id": client_id("sl", symbol, opened_ms), "tp_id": client_id("tp", symbol, opened_ms), "breakeven_done": False,
                 "breakeven_r": self.s.breakeven_r, "be_failures": 0, "finalize_attempts": 0,
                 "planned_risk": quantity * distance, "risk_reward": self.s.tp_r, "score": candidate["score"],
                 "signal": candidate["signal"]}
        try:
            self.client.algo_order(symbol, exit_side, "STOP_MARKET", stop, trade["sl_id"])
        except BinanceError as error:
            log.error("Stop placement failed for %s: %s; closing position", symbol, error)
            self._emergency_close(symbol, exit_side, quantity, f"Stop emri konulamadı ({error}); pozisyon güvenlik için kapatıldı.")
            return None
        self.state["trades"][symbol] = trade
        self.state["signaled"][symbol] = candidate["date"]
        self.state.save()
        try:
            self.client.algo_order(symbol, exit_side, "TAKE_PROFIT_MARKET", take, trade["tp_id"])
        except BinanceError as error:
            trade["tp_id"] = None
            self.state.save()
            self.notify.send(tg.fmt_warning(f"{symbol}: take-profit emri konulamadı ({error}). Pozisyon stop ile korunuyor."))
        self.notify.send(tg.fmt_open(trade, candidate))
        return trade

    def _emergency_close(self, symbol: str, exit_side: str, quantity: float, reason: str):
        try:
            self.client.market_order(symbol, exit_side, quantity, reduce_only=True)
            self.notify.send(tg.fmt_warning(f"{symbol}: {reason}"))
        except BinanceError as error:
            self.notify.send(tg.fmt_warning(f"{symbol}: KORUMASIZ POZİSYON! {reason} Kapatma da başarısız: {error}. Elle müdahale edin."))

    # ---- managing -----------------------------------------------------------
    def manage(self):
        if not self.state["trades"]:
            return
        try:
            positions = {p["symbol"]: p for p in self.client.positions()}
        except BinanceError as error:
            self._alert("manage:positions", f"Pozisyonlar okunamadı: {error}")
            return
        for symbol, trade in list(self.state["trades"].items()):
            try:
                position = positions.get(symbol)
                if position is None:
                    self._finalize(trade)
                    continue
                if self._time_stop(trade, position):
                    continue
                self._breakeven(trade, float(position["markPrice"]))
            except BinanceError as error:
                self._alert(f"manage:{symbol}", f"{symbol} yönetim hatası: {error}")

    def _time_stop(self, trade: dict, position: dict) -> bool:
        hours = self.s.max_hold_hours
        if not hours or self._clock() * 1000 - trade["opened_ms"] < hours * 3_600_000:
            return False
        exit_side = "SELL" if trade["side"] == "LONG" else "BUY"
        trade["close_hint"] = "TIME_STOP"  # persisted: fills may only become visible on a later tick
        self.state.save()
        self.client.market_order(trade["symbol"], exit_side, abs(float(position["positionAmt"])), reduce_only=True)
        self._finalize(trade)
        return True

    def _breakeven(self, trade: dict, mark: float):
        if not trade["breakeven_r"] or trade["breakeven_done"] or trade["be_failures"] >= 3:
            return
        sign = 1 if trade["side"] == "LONG" else -1
        if sign * (mark - trade["entry"]) < trade["breakeven_r"] * trade["risk_distance"]:
            return
        info, symbol = self.infos[trade["symbol"]], trade["symbol"]
        # stop slightly beyond entry so fees are covered
        new_stop = round_step(trade["entry"] * (1 + sign * 2 * self.s.taker_fee), info.tick, ROUND_HALF_UP)
        if sign * (mark - new_stop) <= 0:
            return
        exit_side = "SELL" if sign > 0 else "BUY"
        new_id = client_id("be", symbol, int(self._clock() * 1000))
        try:
            # reduce-only (not closePosition): Binance allows a single closePosition stop per direction
            self.client.algo_order(symbol, exit_side, "STOP_MARKET", new_stop, new_id, quantity=trade["quantity"])
        except BinanceError as error:
            trade["be_failures"] += 1
            self.state.save()
            self._alert(f"be:{symbol}", f"{symbol}: stop başa başa çekilemedi ({error}); eski stop yerinde.")
            return
        old_id = trade["sl_id"]
        trade.update(sl_id=new_id, stop=new_stop, breakeven_done=True)
        self.state.save()
        self.client.cancel_algo_order(old_id)
        self.notify.send(tg.fmt_breakeven(trade, new_stop, mark))

    def _classify(self, trade: dict, exit_price: float | None) -> str:
        for key, reason in (("sl_id", "BREAKEVEN_STOP" if trade["breakeven_done"] else "STOP_LOSS"),
                            ("tp_id", "TAKE_PROFIT")):
            if not trade.get(key):
                continue
            try:
                status = str(self.client.get_algo_order(trade[key]).get("algoStatus", "")).upper()
            except BinanceError:
                continue
            if status and status not in DEAD_ALGO:
                return reason
        if exit_price:
            tolerance = 0.25 * trade["risk_distance"]
            if abs(exit_price - trade["take_profit"]) <= tolerance:
                return "TAKE_PROFIT"
            if abs(exit_price - trade["stop"]) <= tolerance:
                return "BREAKEVEN_STOP" if trade["breakeven_done"] else "STOP_LOSS"
        return "EXTERNAL"

    def _finalize(self, trade: dict, hint: str | None = None):
        symbol, side = trade["symbol"], trade["side"]
        entry_side, exit_side = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
        fills = [f for f in self.client.user_trades(symbol, trade["opened_ms"] - 5000) if int(f["time"]) >= trade["opened_ms"] - 5000]
        exits = [f for f in fills if f["side"] == exit_side]
        if not exits and trade["finalize_attempts"] < 3:  # fills may lag the position update
            trade["finalize_attempts"] += 1
            return
        for cleanup in (self.client.cancel_all_algo_orders, self.client.cancel_all_orders):
            try:
                cleanup(symbol)
            except BinanceError as error:
                log.warning("Cleanup %s failed for %s: %s", cleanup.__name__, symbol, error)
        exit_qty = sum(float(f["qty"]) for f in exits)
        exit_price = sum(float(f["price"]) * float(f["qty"]) for f in exits) / exit_qty if exit_qty else None
        outcome = {"closed_ms": int(self._clock() * 1000), "exit_price": exit_price}
        if fills and exits:
            fees = 0.0
            for f in fills:
                notional = float(f["price"]) * float(f["qty"])
                fees += float(f["commission"]) if f.get("commissionAsset") == "USDT" else notional * self.s.taker_fee
            gross = sum(float(f["realizedPnl"]) for f in fills)
            net = gross - fees
            outcome.update(net_pnl=net, fees=fees, r_multiple=net / trade["planned_risk"] if trade["planned_risk"] else 0.0)
        else:
            outcome.update(net_pnl=None, fees=0.0, r_multiple=0.0)
        outcome["reason"] = hint or trade.get("close_hint") or self._classify(trade, exit_price)
        daily = self.state["daily"]
        if outcome["net_pnl"] is not None and daily:
            daily["realized"] += outcome["net_pnl"]
            daily["trades"] += 1
            daily["wins"] += outcome["net_pnl"] > 0
        if outcome["net_pnl"] is not None:
            totals = self.state["totals"]
            totals["trades"] += 1
            totals["wins"] += outcome["net_pnl"] > 0
            totals["realized"] += outcome["net_pnl"]
        self.state["trades"].pop(symbol, None)
        self.state["cooldowns"][symbol] = self._clock() + self.s.cooldown_minutes * 60
        self.state.record_close({"symbol": symbol, "side": side, "entry": trade["entry"], "exit": exit_price,
                                 "pnl": outcome["net_pnl"], "reason": outcome["reason"], "closed_ms": outcome["closed_ms"]})
        self.state.save()
        self.notify.send(tg.fmt_close(trade, outcome))

    # ---- startup ------------------------------------------------------------
    def reconcile(self):
        """After a restart: make sure every tracked position is protected, flag untracked ones."""
        positions = {p["symbol"]: p for p in self.client.positions()}
        open_algos = self.client.open_algo_orders()
        live_ids = {o.get("clientAlgoId") for o in open_algos}
        for symbol, trade in list(self.state["trades"].items()):
            if symbol not in positions:
                self._finalize(trade)
                continue
            exit_side = "SELL" if trade["side"] == "LONG" else "BUY"
            if trade["sl_id"] not in live_ids:
                trade["sl_id"] = client_id("sl", symbol, int(self._clock() * 1000))
                try:
                    self.client.algo_order(symbol, exit_side, "STOP_MARKET", trade["stop"], trade["sl_id"])
                    self.notify.send(tg.fmt_warning(f"{symbol}: yeniden başlatmada stop emri eksikti, yeniden kondu ({tg.price(trade['stop'])})."))
                except BinanceError as error:
                    self._emergency_close(symbol, exit_side, abs(float(positions[symbol]["positionAmt"])),
                                          f"Stop emri yeniden konulamadı ({error}); pozisyon kapatıldı.")
            if trade.get("tp_id") and trade["tp_id"] not in live_ids:
                trade["tp_id"] = client_id("tp", symbol, int(self._clock() * 1000))
                try:
                    self.client.algo_order(symbol, exit_side, "TAKE_PROFIT_MARKET", trade["take_profit"], trade["tp_id"])
                except BinanceError as error:
                    trade["tp_id"] = None
                    log.warning("TP re-placement failed for %s: %s", symbol, error)
        for symbol in positions.keys() - self.state["trades"].keys():
            self._alert(f"untracked:{symbol}", f"{symbol}: bot tarafından açılmamış pozisyon var; bot buna dokunmaz.", every=86400)
        self.state.save()
