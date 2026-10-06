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
MENU = [("/positions", "Açık pozisyonlar ve anlık kâr/zarar"), ("/pnl", "Kâr/zarar özeti"), ("/status", "Bot durumu ve bakiye"),
        ("/top", "En yüksek skorlu coinler"), ("/pause", "Yeni işlem açmayı durdur"), ("/resume", "Yeni işlem açmaya devam et"),
        ("/help", "Komut listesi")]
ALIASES = {"/pozisyon": "/positions", "/pozisyonlar": "/positions", "/kar": "/pnl", "/zarar": "/pnl", "/karzarar": "/pnl",
           "/bakiye": "/pnl", "/durum": "/status", "/start": "/help", "/yardim": "/help"}
HELP = ("Komutlar:\n/positions (/pozisyon): açık pozisyonlar, anlık fiyat ve kâr/zarar\n/pnl (/kar): günlük ve toplam kâr/zarar\n"
        "/status (/durum): bot durumu ve bakiye\n/top: en yüksek skorlar\n/pause, /resume: yeni işlem açmayı durdur/sürdür")


class Bot:
    def __init__(self, settings: Settings, provider, source: str, source_warning: str | None, notifier,
                 state: StateStore, executor=None, venue: MarketClient | None = None, news: NewsService | None = None,
                 clock=time.time, sleep=time.sleep, allowed_users: tuple[str, ...] = ()):
        self.s, self.provider, self.source, self.warning = settings, provider, source, source_warning
        self.notify, self.state, self.executor = notifier, state, executor
        self.venue = venue or MarketClient(TESTNET_URL)
        self.news, self._clock, self._sleep = news, clock, sleep
        self.allowed_users = tuple(allowed_users)
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
        self.notify.username = self.notify.whoami() if self.notify.configured else ""
        self.notify.set_commands(MENU)
        balance = None
        if self.executor:
            balance = self.executor.balance()
            self.executor.roll_day(balance["wallet"] + balance["unrealized"])
            self.executor.reconcile()
        log.info("Started (%s, %d symbols, source %s)%s", "dry-run" if self.dry_run else "testnet", len(self.universe),
                 self.source, f"; {self.warning}" if self.warning else "")

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
            next_manage = 0.0
            while not self._stop:
                if self._clock() >= next_manage:       # positions + scans on their own, slower rhythm
                    self.tick(commands=False)
                    next_manage = self._clock() + self.s.manage_interval_seconds
                self._poll_commands()                  # your Telegram commands are answered within seconds
                self._sleep(self.s.command_poll_seconds)
        finally:
            log.info("Stopped; %d open position(s) stay protected by exchange-side stop/TP", len(self.state["trades"]))

    # ---- one iteration -------------------------------------------------------
    def _poll_commands(self):
        try:
            self._commands()
        except Exception as error:  # never let a Telegram hiccup stop the trading loop
            log.warning("Command polling failed: %s", error)

    def tick(self, commands: bool = True):
        now = self._clock()
        try:
            if commands:
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

    def _error(self, key: str, error: Exception, every: int | None = None):
        """Loop errors are logged and shown by /status; Telegram stays quiet."""
        log.warning("%s: %s", key, error)
        self.state["last_error"] = {"text": f"{type(error).__name__}: {error}", "ms": int(self._clock() * 1000), "loop": True}

    def _roll_day(self):
        if not self.executor.new_day():
            return
        balance = self.executor.balance()
        self.executor.roll_day(balance["wallet"] + balance["unrealized"])

    def _scan_and_trade(self):
        self._refresh_universe()
        capital = self.s.capital_usdt or 10_000.0
        if self.executor:
            capital = self.executor.capital()
            if self.executor.daily_loss_locked():
                self._error("daily_loss", RuntimeError("Günlük zarar limiti doldu: yeni işlem açılmayacak."), every=86400)
        report = scan(self.provider, self.s, self.universe, capital, self.news, int(self._clock() * 1000), self.source)
        self.report = report
        for error in report["errors"]:
            log.warning("Scan error %s: %s", error["symbol"], error["error"])
        fresh = []
        for candidate in sorted(actionable(report), key=lambda c: -c["score"]):
            symbol = candidate["symbol"]
            if self.state["signaled"].get(symbol) == candidate["date"]:
                continue  # one attempt per symbol per candle
            if self.dry_run:
                self.state["signaled"][symbol] = candidate["date"]
                fresh.append(candidate)
                continue
            reason = self.executor.can_open(candidate)
            if reason:
                log.info("Skip %s %s: %s", symbol, candidate["side"], reason)
                continue
            self.executor.open_trade(candidate)
            self.state["signaled"][symbol] = candidate["date"]
        if fresh:  # one digest per scan instead of one message per coin
            self.notify.send(tg.fmt_signals(fresh))
        last = self.state["last_error"]
        if last and last.get("loop"):  # the scan loop works again: the old problem is resolved
            self.state["last_error"] = None
        self.state.save()

    # ---- telegram commands -----------------------------------------------------
    CONTROL = ("/pause", "/resume")

    def _commands(self):
        commands, offset = self.notify.poll(self.state["tg_offset"])
        if offset != self.state["tg_offset"]:
            self.state["tg_offset"] = offset
            self.state.save()
        for command, user in commands:
            self.notify.send(self._answer(command) if self._may(command, user) else
                             f"⛔ Bu komut için yetkiniz yok. Kullanıcı id'niz: <code>{tg.esc(user)}</code>\n"
                             "Yetki vermek için botun .env dosyasına TELEGRAM_ALLOWED_USER_IDS=" + tg.esc(user) +
                             " ekleyip botu yeniden başlatın.")

    def _may(self, command: str, user: str) -> bool:
        """Private chat: its owner may do everything. Group: anyone may read, only allowed users may control."""
        command = ALIASES.get(command, command)
        if command not in self.CONTROL or not self.notify.chat_id.startswith("-"):
            return True
        return user in self.allowed_users

    def _answer(self, command: str) -> str:
        command = ALIASES.get(command, command)
        if command in ("/pause", "/resume"):
            self.state["paused"] = command == "/pause"
            self.state.save()
            return "⏸ Yeni işlem açılışı durduruldu (açık pozisyonlar yönetilmeye devam eder)." if self.state["paused"] \
                else "▶️ Yeni işlem açılışı yeniden başladı."
        if command == "/top":
            return tg.fmt_top(self.report) if self.report else "Henüz tarama yapılmadı."
        if command in ("/pnl", "/positions"):
            try:
                return self._positions() if command == "/positions" else self._pnl()
            except BinanceError as error:
                return tg.fmt_warning(f"Borsa okunamadı: {error}")
        if command == "/status":
            return self._status()
        return HELP

    def _live_rows(self) -> tuple[list[dict], dict | None]:
        """Open positions merged with live exchange data (mark price, unrealized PnL, liquidation)."""
        if self.dry_run:
            return [], None
        balance = self.executor.balance()
        live = {p["symbol"]: p for p in self.executor.client.positions()}
        rows = []
        for symbol, trade in self.state["trades"].items():
            if symbol in live:
                position = live[symbol]
                rows.append({**trade, "mark": float(position["markPrice"]), "pnl": float(position["unRealizedProfit"]),
                             "liquidation": float(position.get("liquidationPrice") or 0) or None})
        return rows, balance

    def _positions(self) -> str:
        if self.dry_run:
            return "ℹ️ DRY-RUN modunda emir gönderilmediği için açık pozisyon yok."
        rows, balance = self._live_rows()
        return tg.fmt_positions(rows, balance, int(self._clock() * 1000), self.s.telegram_verbose)

    def _pnl(self) -> str:
        daily = self.state["daily"] or {"date": "-"}
        unrealized, balance = None, None
        if not self.dry_run:
            rows, balance = self._live_rows()
            unrealized = sum(r["pnl"] for r in rows)
        return tg.fmt_pnl(daily, self.state["totals"], unrealized, self.state["history"], balance)

    def _status(self) -> str:
        trades = self.state["trades"]
        line = (f"ℹ️ {'DURAKLATILDI' if self.state['paused'] else 'aktif'}{' (dry-run)' if self.dry_run else ''} · "
                f"{len(trades)}/{self.s.max_open_positions} pozisyon")
        if self.report:
            line += f" · BTC {tg.esc(self.report['market_regime'])} · {len(self.universe)} coin · veri {tg.esc(self.source)}"
        if self.executor:
            try:
                balance = self.executor.balance()
                line += f"\nBakiye {tg.money(balance['wallet'])} USDT · açık PnL {balance['unrealized']:+.2f} (/positions)"
            except BinanceError as error:
                line += f"\nBorsa okunamadı: {tg.esc(error)}"
        return line


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
        return Bot(settings, cached, source, warning, notifier, state, None, MarketClient(TESTNET_URL, session=session), news,
               allowed_users=secrets.telegram_allowed_users)
    if not secrets.has_binance:
        raise ValueError("BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET missing (see .env.example); use --dry-run to try without keys")
    client = TradingClient(secrets.api_key, secrets.api_secret, session=session)
    executor = Executor(client, settings, state, notifier, {})
    return Bot(settings, cached, source, warning, notifier, state, executor, client, news,
               allowed_users=secrets.telegram_allowed_users)
