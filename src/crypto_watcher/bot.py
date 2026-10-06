"""The trading loop: scan -> decide -> execute -> manage -> notify."""
import logging
import signal
import time
from datetime import datetime, timezone
from .binance import BinanceError, MarketClient, TESTNET_URL
from .config import Settings
from .data import CachedProvider, choose_market_source, parse_exchange_info, select_universe
from .engine import actionable, scan
from .news import NewsService
from .state import StateStore
from . import telegram as tg

log = logging.getLogger(__name__)
COMMANDS = {"/help": "Komutlar: /status /positions /top /pnl /pause /resume"}


class Bot:
    def __init__(self, settings: Settings, provider, source: str, source_warning: str | None, notifier,
                 state: StateStore, executor=None, venue: MarketClient | None = None, news: NewsService | None = None,
                 clock=time.time, sleep=time.sleep):
        self.s, self.provider, self.source, self.warning = settings, provider, source, source_warning
        self.notify, self.state, self.executor = notifier, state, executor
        self.venue = venue or MarketClient(TESTNET_URL)
        self.news, self._clock, self._sleep = news, clock, sleep
        self.infos, self.universe, self.report = {}, [], None
        self._universe_at = self._scan_at = self._manage_at = 0.0
        self._stop = False

    @property
    def dry_run(self) -> bool:
        return self.executor is None

    # ---- lifecycle ----------------------------------------------------------
    def start(self):
        self.infos = parse_exchange_info(self.venue.exchange_info())
        if self.executor:
            self.executor.infos = self.infos
            self.executor.prepare()
        self._refresh_universe(force=True)
        balance = None
        if self.executor:
            balance = self.executor.balance()
            self.executor.roll_day(balance["wallet"] + balance["unrealized"])
            self.executor.reconcile()
        self.notify.send(tg.fmt_start(self.s, self.universe, self.source, self.dry_run, balance, self.warning))

    def _refresh_universe(self, force: bool = False):
        now = self._clock()
        if not force and now - self._universe_at < self.s.universe_refresh_minutes * 60:
            return
        self.universe = select_universe(self.s, self.infos, self.provider.tickers(), int(now * 1000))
        self._universe_at = now
        if not self.universe:
            raise RuntimeError("Universe is empty: lower min_quote_volume or check the data source")

    def stop(self, *_):
        self._stop = True

    def run_forever(self):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.start()
        try:
            while not self._stop:
                self.tick()
                self._sleep(self.s.manage_interval_seconds)
        finally:
            open_count = len(self.state["trades"])
            self.notify.send(f"🛑 <b>CryptoWatcher durduruldu.</b> Açık pozisyon: {open_count}"
                             + (" (borsada stop/TP emirleriyle korunuyor)" if open_count else ""))

    # ---- one iteration -------------------------------------------------------
    def tick(self):
        now = self._clock()
        try:
            self._commands()
            if self.executor:
                self.executor.manage()
                self._roll_day()
            if now - self._scan_at >= self.s.scan_interval_seconds:
                self._scan_at = now
                self._scan_and_trade()
        except BinanceError as error:
            self._error("binance", error)
        except Exception as error:  # keep the loop alive; the exchange-side stop still protects positions
            log.exception("Tick failed")
            self._error(type(error).__name__, error)

    def _error(self, key: str, error: Exception):
        last = self.state["alerts"].get(key, 0)
        if self._clock() - last >= 1800:
            self.state["alerts"][key] = self._clock()
            self.notify.send(tg.fmt_warning(f"{type(error).__name__}: {error}"))

    def _roll_day(self):
        if not self.executor.new_day():
            return
        balance = self.executor.balance()
        finished = self.executor.roll_day(balance["wallet"] + balance["unrealized"])
        if finished:
            self.notify.send(tg.fmt_daily(finished))

    def _scan_and_trade(self):
        self._refresh_universe()
        capital = self.s.capital_usdt or 10_000.0
        if self.executor:
            capital = self.executor.capital()
            if self.executor.daily_loss_locked():
                self._error("daily_loss", RuntimeError("Günlük zarar limiti doldu: yeni işlem açılmayacak."))
        report = scan(self.provider, self.s, self.universe, capital, self.news, int(self._clock() * 1000), self.source)
        self.report = report
        for error in report["errors"]:
            log.warning("Scan error %s: %s", error["symbol"], error["error"])
        for candidate in sorted(actionable(report), key=lambda c: -c["score"]):
            symbol = candidate["symbol"]
            if self.state["signaled"].get(symbol) == candidate["date"]:
                continue  # one attempt per symbol per candle
            if self.dry_run:
                plan = candidate["sides"][candidate["side"]]["risk_plan"]
                self.state["signaled"][symbol] = candidate["date"]
                self.notify.send(tg.fmt_open({"symbol": symbol, "side": candidate["side"], "leverage": plan["leverage"],
                                              "entry": plan["entry"], "quantity": plan["quantity"], "stop": plan["stop"],
                                              "take_profit": plan["take_profit"], "planned_risk": plan["planned_risk"],
                                              "risk_reward": plan["risk_reward"]}, candidate, dry_run=True))
                continue
            reason = self.executor.can_open(candidate)
            if reason:
                log.info("Skip %s %s: %s", symbol, candidate["side"], reason)
                continue
            self.executor.open_trade(candidate)
            self.state["signaled"][symbol] = candidate["date"]
        self.state.save()

    # ---- telegram commands -----------------------------------------------------
    def _commands(self):
        commands, offset = self.notify.poll(self.state["tg_offset"])
        if offset != self.state["tg_offset"]:
            self.state["tg_offset"] = offset
            self.state.save()
        for command in commands:
            self.notify.send(self._answer(command))

    def _answer(self, command: str) -> str:
        if command == "/pause" or command == "/resume":
            self.state["paused"] = command == "/pause"
            self.state.save()
            return "⏸ Yeni işlem açılışı durduruldu (açık pozisyonlar yönetilmeye devam eder)." if self.state["paused"] \
                else "▶️ Yeni işlem açılışı yeniden başladı."
        if command == "/top":
            return tg.fmt_top(self.report) if self.report else "Henüz tarama yapılmadı."
        if command == "/pnl":
            daily = self.state["daily"]
            rows = [f"{h['symbol']} {h['side']}: {h['pnl']:+.2f} ({h['reason']})" if h["pnl"] is not None
                    else f"{h['symbol']} {h['side']}: ? ({h['reason']})" for h in self.state["history"][-8:]]
            return tg.fmt_daily(daily) + ("\n" + "\n".join(tg.esc(r) for r in rows) if rows else "") if daily else "Veri yok."
        if command in ("/status", "/positions"):
            return self._status(command == "/positions")
        return COMMANDS["/help"]

    def _status(self, detail: bool) -> str:
        trades = self.state["trades"]
        lines = [f"ℹ️ <b>Durum</b>{' (DRY-RUN)' if self.dry_run else ''}: {'DURAKLATILDI' if self.state['paused'] else 'aktif'} · "
                 f"açık pozisyon {len(trades)}/{self.s.max_open_positions}"]
        if self.report:
            lines.append(f"BTC rejimi {tg.esc(self.report['market_regime'])} · veri {tg.esc(self.source)} · evren {len(self.universe)}")
        if self.executor:
            try:
                balance = self.executor.balance()
                lines.append(f"Bakiye {tg.money(balance['wallet'])} USDT · gerçekleşmemiş {balance['unrealized']:+.2f}")
                live = {p["symbol"]: p for p in self.executor.client.positions()}
            except BinanceError as error:
                return "\n".join(lines + [f"Borsa okunamadı: {tg.esc(error)}"])
            for symbol, t in trades.items():
                pnl = float(live[symbol]["unRealizedProfit"]) if symbol in live else 0.0
                lines.append(f"• <code>{tg.esc(symbol)}</code> {t['side']} {t['leverage']}x giriş {tg.price(t['entry'])} "
                             f"stop {tg.price(t['stop'])} hedef {tg.price(t['take_profit'])} PnL {pnl:+.2f}")
        return "\n".join(lines)


def build_bot(settings: Settings, secrets, dry_run: bool, session=None, state_path=None) -> Bot:
    from .binance import TradingClient
    from .executor import Executor
    provider, source, warning = choose_market_source(settings.market_data, session)
    notifier = tg.TelegramNotifier(secrets.telegram_token, secrets.telegram_chat_id, session=session)
    state = StateStore(state_path if state_path is not None else settings.state_file)
    news = NewsService(settings.news_feeds, settings.news_max_age_hours, settings.news_cache_minutes, session=session) \
        if settings.news_enabled else None
    cached = CachedProvider(provider)
    if dry_run:
        return Bot(settings, cached, source, warning, notifier, state, None, MarketClient(TESTNET_URL, session=session), news)
    if not secrets.has_binance:
        raise ValueError("BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET missing (see .env.example); use --dry-run to try without keys")
    client = TradingClient(secrets.api_key, secrets.api_secret, session=session)
    executor = Executor(client, settings, state, notifier, {})
    return Bot(settings, cached, source, warning, notifier, state, executor, client, news)
