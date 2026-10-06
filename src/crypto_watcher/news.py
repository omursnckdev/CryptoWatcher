"""Headline sentiment from public crypto RSS feeds (no API keys).

Deliberately simple and explainable: a weighted keyword lexicon applied to headlines
(and summaries) that mention the coin. It is a veto/tie-breaker, not a forecast.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
import requests

log = logging.getLogger(__name__)

# word/phrase -> weight (positive = bullish for the coin, negative = bearish)
LEXICON = {
    "etf approval": 1.0, "approved": 0.4, "partnership": 0.5, "integration": 0.3, "listing": 0.6, "lists": 0.4,
    "upgrade": 0.4, "mainnet launch": 0.5, "adoption": 0.4, "record high": 0.6, "all-time high": 0.7, "ath": 0.5,
    "surge": 0.6, "surges": 0.6, "soars": 0.7, "rally": 0.5, "rallies": 0.5, "jumps": 0.5, "gains": 0.3,
    "bullish": 0.5, "breakout": 0.5, "inflows": 0.4, "buyback": 0.5, "burn": 0.3, "accumulate": 0.3,
    "hack": -1.0, "hacked": -1.0, "exploit": -1.0, "exploited": -1.0, "drained": -0.9, "rug pull": -1.0,
    "delist": -1.0, "delisting": -1.0, "delists": -1.0, "insolvency": -1.0, "bankruptcy": -1.0, "bankrupt": -1.0,
    "lawsuit": -0.7, "sues": -0.6, "sued": -0.6, "sec charges": -1.0, "investigation": -0.5, "ban": -0.6,
    "banned": -0.6, "fraud": -0.9, "scam": -0.8, "outage": -0.5, "halted": -0.6, "suspends": -0.5,
    "crash": -0.8, "crashes": -0.8, "plunge": -0.7, "plunges": -0.7, "plummets": -0.7, "tumbles": -0.6,
    "slump": -0.5, "slumps": -0.5, "dumps": -0.6, "sell-off": -0.5, "selloff": -0.5, "bearish": -0.5,
    "outflows": -0.4, "liquidations": -0.4, "liquidated": -0.4, "vulnerability": -0.6, "downgrade": -0.4,
    "unlock": -0.3, "fud": -0.3,
}
ALIASES = {
    "BTC": ("bitcoin",), "ETH": ("ethereum", "ether"), "BNB": ("binance coin",), "SOL": ("solana",),
    "XRP": ("ripple",), "DOGE": ("dogecoin",), "ADA": ("cardano",), "AVAX": ("avalanche",),
    "LINK": ("chainlink",), "DOT": ("polkadot",), "LTC": ("litecoin",), "TRX": ("tron",),
    "TON": ("toncoin",), "BCH": ("bitcoin cash",), "NEAR": ("near protocol",), "UNI": ("uniswap",),
    "ATOM": ("cosmos",), "SUI": ("sui network",), "APT": ("aptos",), "ARB": ("arbitrum",),
    "OP": ("optimism",), "XLM": ("stellar",), "FIL": ("filecoin",), "AAVE": ("aave",),
    "SHIB": ("shiba inu",), "PEPE": ("pepe coin",), "HBAR": ("hedera",), "ETC": ("ethereum classic",),
    "ZEC": ("zcash",), "XMR": ("monero",), "INJ": ("injective",), "WLD": ("worldcoin",),
}
_LEX_PATTERNS = [(re.compile(rf"(?<![a-z]){re.escape(word)}(?![a-z])"), weight) for word, weight in LEXICON.items()]
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Headline:
    title: str
    published: float          # epoch seconds
    source: str
    summary: str = ""


@dataclass
class NewsResult:
    count: int = 0
    sentiment: float = 0.0    # -1 .. +1
    headlines: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.count > 0


def headline_score(text: str) -> float:
    lowered = text.lower()
    return max(-1.0, min(1.0, sum(weight for pattern, weight in _LEX_PATTERNS if pattern.search(lowered))))


def mentions(text: str, base: str, aliases: dict[str, tuple[str, ...]] = ALIASES) -> bool:
    """Case-sensitive uppercase ticker, $ticker, or a known coin name. Avoids matching 'near', 'one', 'op'."""
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(base)}(?![A-Za-z0-9])", text):
        return True
    if re.search(rf"\${re.escape(base)}(?![A-Za-z0-9])", text, re.IGNORECASE):
        return True
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", lowered) for name in aliases.get(base, ()))


def parse_feed(xml_bytes: bytes, source: str) -> list[Headline]:
    """RSS 2.0 / Atom. DTDs and entities are rejected outright (hostile-feed hardening)."""
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in xml_bytes[:65536].lower():
        raise ValueError("DTD/entities not allowed in feeds")
    root = ET.fromstring(xml_bytes)
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in node}
        title = html.unescape(_TAG.sub("", fields.get("title", ""))).strip()
        raw_date = fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date", "")
        try:
            stamp = parsedate_to_datetime(raw_date) if "," in raw_date or raw_date[:3].isalpha() else datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            published = stamp.timestamp()
        except (ValueError, TypeError, IndexError):
            continue
        summary = html.unescape(_TAG.sub(" ", fields.get("description") or fields.get("summary", ""))).strip()[:400]
        if title:
            items.append(Headline(title, published, source, summary))
    return items


def aggregate(headlines: list[Headline], base: str, now: float, max_age_hours: float,
              aliases: dict[str, tuple[str, ...]] = ALIASES, half_life_hours: float = 12.0) -> NewsResult:
    """Recency-weighted MEAN headline score, shrunk toward 0 when fewer than 3 headlines mention the coin."""
    weighted, weights, picked = 0.0, 0.0, []
    for item in headlines:
        age_h = (now - item.published) / 3600
        if age_h < -1 or age_h > max_age_hours or not mentions(item.title, base, aliases):
            continue
        recency = 0.5 ** (max(age_h, 0) / half_life_hours)
        score = headline_score(item.title)
        weighted += score * recency
        weights += recency
        picked.append((score != 0, recency, f"{item.title} ({item.source})"))
    if not picked:
        return NewsResult()
    picked.sort(reverse=True)
    confidence = min(1.0, len(picked) / 3)
    return NewsResult(len(picked), max(-1.0, min(1.0, weighted / weights * confidence)),
                      [text for _, _, text in picked[:3]])


class NewsService:
    def __init__(self, feeds, max_age_hours: float = 24.0, cache_minutes: int = 10, session=None,
                 clock=time.time, aliases=ALIASES, timeout: float = 10.0):
        self.feeds, self.max_age_hours, self.cache_seconds = tuple(feeds), max_age_hours, cache_minutes * 60
        self.session, self._clock, self.aliases, self.timeout = session or requests.Session(), clock, aliases, timeout
        self._items: list[Headline] = []
        self._fetched = float("-inf")
        self.last_errors: list[str] = []

    def refresh(self, force: bool = False):
        if not force and self._clock() - self._fetched < self.cache_seconds:
            return
        items, errors = [], []
        for url in self.feeds:
            try:
                response = self.session.get(url, timeout=self.timeout, headers={"User-Agent": "CryptoWatcher/0.1"})
                response.raise_for_status()
                items.extend(parse_feed(response.content, re.sub(r"^https?://(www\.)?", "", url).split("/")[0]))
            except (requests.RequestException, ET.ParseError, ValueError) as error:
                errors.append(f"{url}: {error}")
        self.last_errors = errors
        if items or not self._items:  # keep stale items if every feed failed
            self._items = items
        self._fetched = self._clock()
        if errors:
            log.warning("News feed errors: %s", "; ".join(errors))

    def for_coin(self, base: str) -> NewsResult:
        self.refresh()
        return aggregate(self._items, base, self._clock(), self.max_age_hours, self.aliases)
