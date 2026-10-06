"""Typed settings (TOML) and secrets (environment / .env)."""
from dataclasses import dataclass, fields
from pathlib import Path
import math
import os
import re
import tomllib

INTERVAL_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400}

STABLE_BASES = ("USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "USD1", "EUR", "EURI", "AEUR", "XUSD", "BFUSD", "RLUSD")
DEFAULT_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
    "https://cryptoslate.com/feed/",
)


@dataclass(frozen=True)
class Settings:
    # --- market data -------------------------------------------------
    market_data: str = "auto"          # auto | mainnet | testnet (analysis only; orders are testnet-only)
    timeframe: str = "1h"
    htf: str = "4h"
    kline_limit: int = 300
    # --- universe ----------------------------------------------------
    min_quote_volume: float = 150_000_000.0   # 24h USDT volume
    max_symbols: int = 25
    min_listing_days: int = 30
    exclude_bases: tuple[str, ...] = STABLE_BASES
    include_symbols: tuple[str, ...] = ()
    universe_refresh_minutes: int = 60
    # --- signal ------------------------------------------------------
    entry_score: float = 70.0
    strong_score: float = 80.0
    watch_score: float = 55.0
    counter_regime_penalty: float = 10.0
    allow_long: bool = True
    allow_short: bool = True
    max_drift_atr: float = 1.0
    crowded_funding: float = 0.0005    # per 8h; loses points when crowded against us
    extreme_funding: float = 0.0015    # blocks entry
    min_atr_pct: float = 0.002
    max_atr_pct: float = 0.06
    btc_high_vol_atr_pct: float = 0.025
    # --- risk --------------------------------------------------------
    leverage: int = 5
    margin_type: str = "ISOLATED"
    capital_usdt: float = 10_000.0     # sizing capital = min(account equity, this); 0 = whole account
    risk_fraction: float = 0.01        # of capital lost if the stop is hit
    max_margin_fraction: float = 0.20  # margin per position / capital
    max_open_positions: int = 5
    max_same_direction: int = 3
    max_daily_loss_fraction: float = 0.05
    atr_stop_multiplier: float = 2.0
    tp_r: float = 2.0
    minimum_rr: float = 1.5
    breakeven_r: float = 1.0           # move stop to entry after +1R; 0 disables
    max_hold_hours: float = 48.0       # 0 disables
    cooldown_minutes: int = 120
    taker_fee: float = 0.0005
    max_cost_to_stop: float = 0.2      # round-trip fees / stop distance
    # --- loop --------------------------------------------------------
    scan_interval_seconds: int = 300
    manage_interval_seconds: int = 20
    state_file: str = "state/state.json"
    # --- news --------------------------------------------------------
    news_enabled: bool = True
    news_feeds: tuple[str, ...] = DEFAULT_FEEDS
    news_max_age_hours: float = 24.0
    news_cache_minutes: int = 10
    news_veto: float = 0.5             # |sentiment| beyond this against the trade blocks it

    def __post_init__(self):
        if self.market_data not in ("auto", "mainnet", "testnet"):
            raise ValueError("market_data must be auto, mainnet or testnet")
        for name in ("timeframe", "htf"):
            if getattr(self, name) not in INTERVAL_SECONDS:
                raise ValueError(f"{name} must be one of {sorted(INTERVAL_SECONDS)}")
        if INTERVAL_SECONDS[self.htf] <= INTERVAL_SECONDS[self.timeframe]:
            raise ValueError("htf must be longer than timeframe")
        if self.margin_type not in ("ISOLATED", "CROSSED"):
            raise ValueError("margin_type must be ISOLATED or CROSSED")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.type is bool:
                if not isinstance(value, bool):
                    raise ValueError(f"{field.name} must be true/false")
            elif field.type is int:
                if type(value) is not int:
                    raise ValueError(f"{field.name} must be an integer")
            elif field.type is float:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{field.name} must be a finite number")
            elif field.type == tuple[str, ...]:
                if not isinstance(value, tuple) or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"{field.name} must be an array of strings")
        for name in ("min_quote_volume", "capital_usdt", "max_hold_hours", "breakeven_r", "max_drift_atr"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("kline_limit", "max_symbols", "leverage", "max_open_positions", "max_same_direction",
                     "scan_interval_seconds", "manage_interval_seconds", "universe_refresh_minutes",
                     "news_cache_minutes", "risk_fraction", "max_margin_fraction", "atr_stop_multiplier",
                     "tp_r", "minimum_rr", "taker_fee", "news_max_age_hours"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.kline_limit < 250 or self.kline_limit > 1000:
            raise ValueError("kline_limit must be between 250 and 1000 (EMA200 needs history)")
        if not 1 <= self.leverage <= 20:
            raise ValueError("leverage must be between 1 and 20")
        if self.risk_fraction > 0.05 or self.max_margin_fraction > 1 or self.max_daily_loss_fraction > 1:
            raise ValueError("risk_fraction must be <= 0.05; margin and daily loss fractions <= 1")
        if self.tp_r < self.minimum_rr:
            raise ValueError("tp_r must be >= minimum_rr")
        if not 0 <= self.breakeven_r < self.tp_r:
            raise ValueError("breakeven_r must be in [0, tp_r)")
        if not (self.watch_score < self.entry_score <= self.strong_score <= 100):
            raise ValueError("Require watch_score < entry_score <= strong_score <= 100")
        if self.extreme_funding < self.crowded_funding:
            raise ValueError("extreme_funding must be >= crowded_funding")
        for symbol in (*self.include_symbols,):
            if not re.fullmatch(r"[A-Z0-9]{2,20}USDT", symbol):
                raise ValueError(f"Invalid symbol {symbol!r}; use e.g. SOLUSDT")
        if not (self.allow_long or self.allow_short):
            raise ValueError("At least one of allow_long / allow_short must be true")


def load_settings(path: Path | None) -> Settings:
    data = {} if path is None else tomllib.loads(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")
    for key, value in list(data.items()):
        if isinstance(value, list):
            data[key] = tuple(value)
    return Settings(**data)


@dataclass(frozen=True)
class Secrets:
    api_key: str = ""
    api_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @property
    def has_binance(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


def parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_secrets(env_file: Path | None = Path(".env"), environ=None) -> Secrets:
    """Real environment variables win over the .env file."""
    environ = os.environ if environ is None else environ
    merged = {**(parse_env_file(env_file) if env_file else {}), **{k: v for k, v in environ.items() if v}}
    return Secrets(api_key=merged.get("BINANCE_TESTNET_API_KEY", ""),
                   api_secret=merged.get("BINANCE_TESTNET_API_SECRET", ""),
                   telegram_token=merged.get("TELEGRAM_BOT_TOKEN", ""),
                   telegram_chat_id=str(merged.get("TELEGRAM_CHAT_ID", "")))
