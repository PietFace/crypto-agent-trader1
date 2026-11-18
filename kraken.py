"""Kraken Futures (perpetuals) — openbare koersen, zonder API-key.

De bot rekent in euro's. Kraken-perps zijn in USD genoteerd (PF_XBTUSD enz.), dus elke koers
wordt omgerekend met de EUR/USD-koers van Kraken zelf (PF_EURUSD).
Orders plaatsen zit hier nog niet in: Kraken draait voorlopig alleen droog.
"""
import threading
import time

import requests

TICKERS_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
TAKER_FEE_PCT = 0.05  # laagste tier, market-orders (Bitvavo: 0,25%)
MAX_LEVERAGE = 5

# Bitvavo-markt -> Kraken-perp. Bitcoin heet bij Kraken XBT.
SYMBOLS = {
    "BTC-EUR": "PF_XBTUSD", "ETH-EUR": "PF_ETHUSD", "SOL-EUR": "PF_SOLUSD", "XRP-EUR": "PF_XRPUSD",
    "ADA-EUR": "PF_ADAUSD", "DOGE-EUR": "PF_DOGEUSD", "LINK-EUR": "PF_LINKUSD", "DOT-EUR": "PF_DOTUSD",
}
EURUSD = "PF_EURUSD"


def sym_of(x):
    """Bitvavo-markt (BTC-EUR) of Kraken-symbool (PF_XBTUSD) -> Kraken-symbool."""
    return SYMBOLS.get(x, x)


def symbol_for_coin(coin):
    """Hyperliquid-munt (BTC, ETH, HYPE, ...) -> Kraken-perp. Bitcoin heet bij Kraken XBT."""
    c = str(coin).upper()
    return "PF_XBTUSD" if c == "BTC" else f"PF_{c}USD"


class KrakenPublic:
    """Haalt alle tickers in één keer op en bewaart ze een paar seconden."""

    def __init__(self, max_age=5.0, session=None):
        self.max_age = max_age
        self.http = session or requests.Session()
        self._lock = threading.Lock()
        self._ts = 0.0
        self._tickers = {}

    def _fetch(self):
        r = self.http.get(TICKERS_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("result") not in (None, "success"):
            raise RuntimeError(f"Kraken: {data.get('error') or data}")
        return {t["symbol"]: t for t in data.get("tickers", []) if t.get("symbol")}

    def tickers(self):
        with self._lock:
            if time.time() - self._ts > self.max_age or not self._tickers:
                self._tickers = self._fetch()
                self._ts = time.time()
            return self._tickers

    def supported(self, market):
        sym = sym_of(market)
        t = self.tickers().get(sym) if sym else None
        return bool(t) and not t.get("suspended")

    def _ticker(self, market):
        sym = sym_of(market)
        if not sym:
            raise KeyError(f"{market} heeft geen Kraken-perp")
        t = self.tickers().get(sym)
        if not t:
            raise KeyError(f"{sym} niet gevonden bij Kraken")
        if t.get("suspended"):
            raise RuntimeError(f"{sym} is bij Kraken tijdelijk stilgelegd")
        return t

    def eurusd(self):
        t = self.tickers().get(EURUSD) or {}
        bid, ask = t.get("bid"), t.get("ask")
        rate = (bid + ask) / 2 if bid and ask else (t.get("markPrice") or t.get("last"))
        if not rate:
            raise RuntimeError("EUR/USD-koers van Kraken ontbreekt")
        return float(rate)

    def price(self, market):
        """Middenkoers in euro's."""
        t = self._ticker(market)
        bid, ask = t.get("bid"), t.get("ask")
        usd = (bid + ask) / 2 if bid and ask else (t.get("markPrice") or t.get("last"))
        return float(usd) / self.eurusd()

    def spread_pct(self, market):
        t = self._ticker(market)
        bid, ask = t.get("bid"), t.get("ask")
        if not bid or not ask:
            return None
        return (ask - bid) / ((ask + bid) / 2) * 100

    def funding_pct_hour(self, market):
        """Relatieve funding per uur in % (positief = longs betalen shorts)."""
        t = self._ticker(market)
        rel = t.get("relativeFundingRate")
        return float(rel) * 100 if rel is not None else 0.0


# ---------------------------------------------------------------- handelen (API-key nodig)
import base64
import hashlib
import hmac
import math
from urllib.parse import urlencode

BASE = "https://futures.kraken.com/derivatives"
INSTRUMENTS_URL = BASE + "/api/v3/instruments"


class KrakenError(RuntimeError):
    pass


class KrakenPrivate:
    """Orders op Kraken Futures. Key: alleen 'General: view' en 'Trading' aanzetten, nooit opnemen."""

    def __init__(self, key, secret, public=None, session=None):
        if not key or not secret:
            raise KrakenError("Kraken API-key of -secret ontbreekt")
        self.key = key.strip()
        self.secret = secret.strip()
        self.http = session or requests.Session()
        self.pub = public or KrakenPublic(session=self.http)
        self._nonce = 0
        self._precision = {}

    # --- ondertekenen volgens de Kraken Futures-documentatie
    def _sign(self, endpoint, post_data, nonce):
        path = endpoint[len("/derivatives"):] if endpoint.startswith("/derivatives") else endpoint
        digest = hashlib.sha256((post_data + nonce + path).encode()).digest()
        mac = hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _next_nonce(self):
        n = max(int(time.time() * 1000), self._nonce + 1)
        self._nonce = n
        return str(n)

    def request(self, method, path, params=None):
        endpoint = "/derivatives/api/v3/" + path
        data = urlencode(params or {})
        nonce = self._next_nonce()
        headers = {"APIKey": self.key, "Nonce": nonce, "Authent": self._sign(endpoint, data, nonce)}
        url = "https://futures.kraken.com" + endpoint
        if method == "GET":
            r = self.http.get(url + ("?" + data if data else ""), headers=headers, timeout=20)
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            r = self.http.post(url, data=data, headers=headers, timeout=20)
        try:
            js = r.json()
        except Exception:
            raise KrakenError(f"Kraken gaf geen geldig antwoord (HTTP {r.status_code})")
        if js.get("result") != "success":
            raise KrakenError(f"Kraken: {js.get('error') or js}")
        return js

    # --- informatie
    def accounts(self):
        return self.request("GET", "accounts")

    def collateral_usd(self):
        """Waarde van je futures-account (flex) in USD, en wat er vrij is voor nieuwe posities."""
        acc = self.accounts().get("accounts", {}).get("flex") or {}
        return float(acc.get("portfolioValue") or 0), float(acc.get("availableMargin") or 0)

    def open_positions(self):
        """{symbol: (side, size)} van wat er nu echt open staat bij Kraken."""
        out = {}
        for p in self.request("GET", "openpositions").get("openPositions", []):
            out[p["symbol"]] = (p.get("side"), float(p.get("size") or 0))
        return out

    def precision(self, symbol):
        if not self._precision:
            js = self.http.get(INSTRUMENTS_URL, timeout=20).json()
            self._precision = {i["symbol"]: int(i.get("contractValueTradePrecision", 0))
                               for i in js.get("instruments", []) if "symbol" in i}
        return self._precision.get(symbol, 0)

    def size_for(self, market, eur):
        """Aantal munten voor een positie van 'eur' euro, afgerond naar beneden op Krakens stapgrootte."""
        sym = sym_of(market)
        units = eur / self.pub.price(market)
        p = self.precision(sym)
        step = 10 ** -p
        return math.floor(units / step) * step if p >= 0 else units, sym

    def usd_price(self, market, eur_price):
        return eur_price * self.pub.eurusd()

    # --- orders
    def market_order(self, market, side, size, reduce_only=False):
        """Market-order. Geeft (gevulde hoeveelheid, gemiddelde prijs in EUR, order_id)."""
        sym = sym_of(market)
        p = self.precision(sym)
        params = {"orderType": "mkt", "symbol": sym, "side": side, "size": f"{size:.{max(p, 0)}f}"}
        if reduce_only:
            params["reduceOnly"] = "true"
        st = self.request("POST", "sendorder", params).get("sendStatus") or {}
        status = st.get("status")
        fills = [e for e in st.get("orderEvents", []) if e.get("type") == "EXECUTION"]
        if not fills:
            raise KrakenError(f"order niet uitgevoerd ({status})")
        amount = sum(float(e["amount"]) for e in fills)
        usd = sum(float(e["amount"]) * float(e["price"]) for e in fills)
        return amount, (usd / amount) / self.pub.eurusd(), st.get("order_id")

    def stop_order(self, market, side, size, stop_eur):
        """Stop-loss bij Kraken zelf (reduce-only, op de mark-prijs). Werkt ook als je pc uit staat."""
        sym = sym_of(market)
        p = self.precision(sym)
        stop_usd = self.usd_price(market, stop_eur)
        tick = self.tick(sym)
        stop_usd = round(round(stop_usd / tick) * tick, 10)
        params = {"orderType": "stp", "symbol": sym, "side": side, "size": f"{size:.{max(p, 0)}f}",
                  "stopPrice": f"{stop_usd}", "triggerSignal": "mark", "reduceOnly": "true"}
        st = self.request("POST", "sendorder", params).get("sendStatus") or {}
        if st.get("status") not in ("placed", "untouched", "edited"):
            raise KrakenError(f"stop niet geplaatst ({st.get('status')})")
        return st.get("order_id")

    def tick(self, symbol):
        if not hasattr(self, "_ticks"):
            js = self.http.get(INSTRUMENTS_URL, timeout=20).json()
            self._ticks = {i["symbol"]: float(i.get("tickSize") or 0.01)
                           for i in js.get("instruments", []) if "symbol" in i}
        return self._ticks.get(symbol, 0.01)

    def cancel(self, order_id):
        if order_id:
            try:
                self.request("POST", "cancelorder", {"order_id": order_id})
            except Exception:
                pass  # al uitgevoerd of al weg
