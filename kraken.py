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
        sym = SYMBOLS.get(market)
        t = self.tickers().get(sym) if sym else None
        return bool(t) and not t.get("suspended")

    def _ticker(self, market):
        sym = SYMBOLS.get(market)
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
