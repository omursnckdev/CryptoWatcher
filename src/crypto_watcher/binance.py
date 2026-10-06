"""Minimal Binance USDⓈ-M Futures REST client.

`MarketClient` is unsigned/read-only and may talk to mainnet or testnet.
`TradingClient` is signed and REFUSES any host other than the futures testnet.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode, urlparse
import hashlib
import hmac
import logging
import time
import requests

log = logging.getLogger(__name__)

MAINNET_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"
TRADING_HOSTS = frozenset({"testnet.binancefuture.com"})


def describe_network_error(error: BaseException) -> str:
    """Root cause of a requests/urllib3 exception, e.g. 'SSLCertVerificationError: certificate verify failed'."""
    root = error
    for _ in range(6):
        nxt = getattr(root, "reason", None) or (root.args[0] if root.args and isinstance(root.args[0], BaseException) else None) \
              or root.__cause__
        if not isinstance(nxt, BaseException):
            break
        root = nxt
    return f"{type(root).__name__}: {str(root)[:140]}"


class BinanceError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def fmt_decimal(value) -> str:
    """Plain (non-scientific) decimal string, as the API requires."""
    text = format(Decimal(str(value)).normalize(), "f")
    return text


def round_step(value: float, step: float, rounding=ROUND_DOWN) -> float:
    if step <= 0:
        return float(value)
    quant = Decimal(str(step))
    return float((Decimal(str(value)) / quant).to_integral_value(rounding) * quant)


class MarketClient:
    def __init__(self, base_url: str = MAINNET_URL, session=None, timeout: float = 15.0, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sleep = sleep

    def _request(self, method: str, path: str, params: dict | None = None, headers: dict | None = None,
                 retries: int = 3) -> dict | list:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self.session.request(method, url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as error:
                last = BinanceError(f"Network error on {path.split('?')[0]}: {describe_network_error(error)}")
                if attempt < retries:
                    self._sleep(min(2 ** attempt, 8))
                    continue
                raise last from error
            if response.status_code in (418, 429) and attempt < retries:
                self._sleep(min(float(response.headers.get("Retry-After", 2 ** (attempt + 1))), 60))
                continue
            if response.status_code >= 500 and attempt < retries and method == "GET":
                self._sleep(min(2 ** attempt, 8))
                continue
            try:
                payload = response.json()
            except ValueError:
                raise BinanceError(f"Non-JSON response (HTTP {response.status_code}): {response.text[:120]}",
                                   status=response.status_code)
            if response.status_code >= 400 or (isinstance(payload, dict) and isinstance(payload.get("code"), int)
                                               and payload["code"] < 0):
                code = payload.get("code") if isinstance(payload, dict) else None
                message = payload.get("msg", "") if isinstance(payload, dict) else str(payload)
                raise BinanceError(f"HTTP {response.status_code} code={code}: {message}", code, response.status_code)
            return payload
        raise last or BinanceError("Request failed")

    # ---- public endpoints ------------------------------------------------
    def ping(self):
        return self._request("GET", "/fapi/v1/ping")

    def server_time(self) -> int:
        return int(self._request("GET", "/fapi/v1/time")["serverTime"])

    def exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def klines(self, symbol: str, interval: str, limit: int = 300) -> list:
        return self._request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def ticker_24h(self) -> list:
        return self._request("GET", "/fapi/v1/ticker/24hr")

    def premium_index(self, symbol: str | None = None):
        return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol} if symbol else None)


class TradingClient(MarketClient):
    def __init__(self, api_key: str, api_secret: str, base_url: str = TESTNET_URL, recv_window: int = 10000, **kwargs):
        host = urlparse(base_url).hostname
        if host not in TRADING_HOSTS:
            raise ValueError(f"Refusing to trade on {host!r}: CryptoWatcher only trades on the Binance futures testnet")
        if not api_key or not api_secret:
            raise ValueError("Binance testnet API key and secret are required")
        super().__init__(base_url, **kwargs)
        self._key, self._secret, self.recv_window = api_key, api_secret, recv_window
        self._offset_ms = 0

    def sync_time(self) -> int:
        self._offset_ms = self.server_time() - int(time.time() * 1000)
        return self._offset_ms

    def _signed(self, method: str, path: str, params: dict | None = None, retries: int | None = None):
        # Never auto-retry order-mutating POSTs: a timeout may hide a filled order.
        if retries is None:
            retries = 0 if method == "POST" else 3
        for attempt in (0, 1):
            query = {k: v for k, v in (params or {}).items() if v is not None}
            query.update(timestamp=int(time.time() * 1000) + self._offset_ms, recvWindow=self.recv_window)
            encoded = urlencode(query)
            url = f"{path}?{encoded}&signature={sign(self._secret, encoded)}"
            try:
                return self._request(method, url, headers={"X-MBX-APIKEY": self._key}, retries=retries)
            except BinanceError as error:
                if error.code == -1021 and attempt == 0:  # clock skew: resync once
                    self.sync_time()
                    continue
                raise

    # ---- account ---------------------------------------------------------
    def account(self) -> dict:
        return self._signed("GET", "/fapi/v3/account")

    def usdt_balance(self) -> dict:
        """-> {wallet, available, unrealized} in USDT, tolerant of v3 layout differences."""
        data = self.account()
        for asset in data.get("assets", []):
            if asset.get("asset") == "USDT":
                return {"wallet": float(asset.get("walletBalance", 0)),
                        "available": float(asset.get("availableBalance", 0)),
                        "unrealized": float(asset.get("unrealizedProfit", 0))}
        return {"wallet": float(data.get("totalWalletBalance", 0)),
                "available": float(data.get("availableBalance", 0)),
                "unrealized": float(data.get("totalUnrealizedProfit", 0))}

    def position_mode(self) -> bool:
        """True when hedge (dual-side) mode is on."""
        return str(self._signed("GET", "/fapi/v1/positionSide/dual").get("dualSidePosition")).lower() == "true"

    def set_one_way_mode(self):
        try:
            self._signed("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})
        except BinanceError as error:
            if error.code != -4059:  # "No need to change position side"
                raise

    def set_margin_type(self, symbol: str, margin_type: str):
        try:
            self._signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except BinanceError as error:
            if error.code != -4046:  # "No need to change margin type"
                raise

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def positions(self, symbol: str | None = None) -> list[dict]:
        rows = self._signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
        return [r for r in rows if abs(float(r.get("positionAmt", 0))) > 0]

    def user_trades(self, symbol: str, start_ms: int, limit: int = 100) -> list[dict]:
        return self._signed("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": limit})

    # ---- orders ----------------------------------------------------------
    def market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False,
                     client_id: str | None = None) -> dict:
        return self._signed("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": fmt_decimal(quantity),
            "reduceOnly": "true" if reduce_only else None, "newClientOrderId": client_id,
            "newOrderRespType": "RESULT"})

    def algo_order(self, symbol: str, side: str, order_type: str, trigger_price: float, client_id: str,
                   quantity: float | None = None) -> dict:
        """STOP_MARKET / TAKE_PROFIT_MARKET conditional order.

        Without `quantity` it closes the whole position (closePosition). With `quantity` it is a reduce-only
        order, which may coexist with a closePosition order in the same direction (Binance allows only one of those).
        Binance moved conditional orders off /fapi/v1/order (error -4120) to the Algo Order API.
        """
        return self._signed("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": order_type,
            "triggerPrice": fmt_decimal(trigger_price), "workingType": "MARK_PRICE", "clientAlgoId": client_id,
            **({"closePosition": "true"} if quantity is None else {"quantity": fmt_decimal(quantity), "reduceOnly": "true"})})

    def get_algo_order(self, client_id: str) -> dict:
        return self._signed("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})

    def open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        rows = self._signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        return rows if isinstance(rows, list) else rows.get("orders", [])

    def cancel_algo_order(self, client_id: str):
        try:
            return self._signed("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id})
        except BinanceError as error:
            if error.code not in (-2011, -2013):  # unknown / already gone
                raise

    def cancel_all_algo_orders(self, symbol: str):
        return self._signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    def cancel_all_orders(self, symbol: str):
        return self._signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})


__all__ = ["MarketClient", "TradingClient", "BinanceError", "sign", "fmt_decimal", "round_step",
           "ROUND_DOWN", "ROUND_HALF_UP", "MAINNET_URL", "TESTNET_URL"]
