"""Multi-agent crypto trader voor Bitvavo: de motor.

Agents: Charts, News, Sentiment, Skeptic -> Chief beslist.
De risicopoort (risk_gate.py, gewone code) heeft altijd het laatste woord.

Normaal start je het programma (app.py / CryptoTrader.exe). Zonder venster:
    python bot.py
Instellingen staan in %APPDATA%\\CryptoTrader\\config.json (aan te passen in het programma).
"""
import json
import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import feedparser
import requests
from python_bitvavo_api.bitvavo import Bitvavo

# Alleen IPv4 gebruiken. Windows wisselt anders soms naar IPv6, en dan past het
# adres niet meer bij de IP-whitelist van je Bitvavo-key (fout 307).
try:
    import urllib3.util.connection as _uc
    _uc.HAS_IPV6 = False
except Exception:
    pass

import kosten
import remote
import risk_gate
import settings

FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/",
         "https://cointelegraph.com/rss"]
EXIT_CHECK_SECONDS = 300  # stop-loss en koersdoel elke 5 minuten

JSON_RULE = " Antwoord ALLEEN met geldig JSON, geen uitleg eromheen."
PROMPTS = {
    "charts": "Je bent de Charts-agent: technisch analist voor crypto. Je krijgt indicatoren voor "
              "1-uurs candles (koers, RSI14, SMA20, SMA50, verandering 24 uur en 7 dagen, volume). "
              "Geef een score van -1 (sterk dalend) tot 1 (sterk stijgend) voor de komende uren tot dagen. "
              "Gebruik het hele bereik: 0 alleen als er echt geen richting is. "
              "Geef JSON: {\"richting\": \"omhoog|omlaag|neutraal\", \"score\": -1..1, "
              "\"onderbouwing\": \"max 2 zinnen\"}." + JSON_RULE,
    "news": "Je bent de News-agent. Je krijgt recente crypto-koppen en een markt. Beoordeel of er "
            "nieuws is dat deze munt de komende uren/dagen raakt. Koersen in koppen staan meestal in "
            "dollars, de markt is in euro's: dat is geen tegenstrijdigheid. Geef JSON: {\"impact\": "
            "\"positief|negatief|geen\", \"score\": -1..1, \"onderbouwing\": \"max 2 zinnen\"}. "
            "Bij geen relevant nieuws: score 0." + JSON_RULE,
    "sentiment": "Je bent de Sentiment-agent. Je krijgt de Fear & Greed Index (laatste 3 dagen). "
                 "Contrair denken is toegestaan: extreme angst kan een koopkans zijn, extreme hebzucht "
                 "een waarschuwing. Geef JSON: {\"stemming\": \"...\", \"score\": -1..1, "
                 "\"onderbouwing\": \"max 2 zinnen\"}." + JSON_RULE,
    "skeptic": "Je bent de Skeptic. Je krijgt de rapporten van de andere agents en de setup die de "
               "regels hebben gevonden. Zoek het sterkste argument TEGEN deze trade en weeg hoe zwaar het is. "
               "Gewone onzekerheid hoort bij elke trade en is 'laag' of 'midden'. Gebruik 'hoog' alleen bij "
               "een concreet, ernstig probleem: groot negatief nieuws over deze munt (hack, delisting, "
               "rechtszaak), een crash of extreme beweging in de afgelopen uren, of duidelijk foute data. "
               "Geef JSON: {\"risico\": \"laag|midden|hoog\", \"sterkste_bezwaar\": \"max 2 zinnen\"}."
               + JSON_RULE,
    "chief": "Je bent de Chief van een kleine portefeuille (ongeveer 100 euro, posities van hooguit 20 euro). "
             "Je krijgt de rapporten van Charts, News, Sentiment en Skeptic, de huidige posities en een "
             "'setup' die vaste regels in code al hebben berekend. Spelregels:\n"
             "- setup.long is waar en de Skeptic zegt niet 'hoog': kies buy, tenzij je een concreet "
             "argument hebt dat in de rapporten staat. Geef dan confidence 0.65 tot 0.85.\n"
             "- setup.short is waar en de Skeptic zegt niet 'hoog': kies short (een gesimuleerde short), "
             "onder dezelfde voorwaarden.\n"
             "- Geen setup, of de Skeptic zegt 'hoog': kies hold.\n"
             "- Je hebt een koop-positie en de charts zijn duidelijk omgeslagen (score onder -0.3): sell. "
             "Je hebt een short en de charts zijn duidelijk omhoog (score boven 0.3): cover. "
             "Stop-loss en koersdoel worden verder automatisch bewaakt.\n"
             "- Bedrag: 20 euro; de risicopoort verkleint het zo nodig.\n"
             "Geef JSON: {\"action\": \"buy|sell|short|cover|hold\", \"amount_eur\": getal "
             "(alleen bij buy of short), \"confidence\": 0..1, \"reason\": \"max 3 zinnen\"}." + JSON_RULE,
}


def setup_for(ind, charts, news):
    """Vaste regels in code: is er een koop- of short-setup? Geeft dict met long, short en redenen.

    Long:  charts-score >= 0.3, koers boven SMA20, SMA20 niet ver onder SMA50 (trend draait of loopt omhoog),
           nieuws niet negatief (score >= -0.2), RSI onder 70 (niet overkocht).
    Short: het spiegelbeeld.
    """
    def f(x, default=0.0):
        try:
            return float(x)
        except (TypeError, ValueError):
            return default
    price, sma20, sma50 = f(ind.get("price")), f(ind.get("sma20")), f(ind.get("sma50"))
    rsi14 = f(ind.get("rsi14"), 50)
    cs = f(charts.get("score")) if "fout" not in charts else 0.0
    ns = f(news.get("score")) if "fout" not in news else 0.0
    checks_long = {
        f"charts {cs:+.2f} >= +0.30": cs >= 0.3,
        "koers boven SMA20": price > sma20 > 0,
        "SMA20 hooguit 1% onder SMA50": sma50 > 0 and sma20 >= sma50 * 0.99,
        f"nieuws {ns:+.2f} >= -0.20": ns >= -0.2,
        f"RSI {rsi14:.0f} < 70": rsi14 < 70,
    }
    checks_short = {
        f"charts {cs:+.2f} <= -0.30": cs <= -0.3,
        "koers onder SMA20": 0 < price < sma20,
        "SMA20 hooguit 1% boven SMA50": sma50 > 0 and sma20 <= sma50 * 1.01,
        f"nieuws {ns:+.2f} <= +0.20": ns <= 0.2,
        f"RSI {rsi14:.0f} > 30": rsi14 > 30,
    }
    return {
        "long": all(checks_long.values()),
        "short": all(checks_short.values()),
        "long_checks": checks_long,
        "short_checks": checks_short,
    }


def setup_text(st):
    if st["long"]:
        return "koop-setup ✓"
    if st["short"]:
        return "short-setup ✓"
    best = max(("long", "short"), key=lambda k: sum(st[k + "_checks"].values()))
    missing = [k for k, ok in st[best + "_checks"].items() if not ok]
    n = sum(st[best + "_checks"].values())
    return f"geen setup ({best} {n}/5, mist: {', '.join(missing)})"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_px(x):
    """Koers leesbaar: 76000.00, 2660.00, 148.80, 2.1660"""
    return f"{x:.2f}" if x >= 10 else f"{x:.4f}"


def bv_check(res, what):
    """Bitvavo geeft fouten terug als {'errorCode': ..}. Maak er een duidelijke melding van."""
    if isinstance(res, dict) and "errorCode" in res:
        code = res.get("errorCode")
        if code == 307:
            raise RuntimeError("Bitvavo weigert dit IP-adres (307). Zet je huidige IPv4-adres op de "
                               "IP-whitelist van je API-key, of maak de whitelist leeg.")
        raise RuntimeError(f"Bitvavo-fout bij {what}: {res.get('error', res)} (code {code})")
    return res


def floor8(x):
    return math.floor(x * 1e8) / 1e8


def rsi(closes, n=14):
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def init_db(path):
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS positions(
            market TEXT PRIMARY KEY, amount REAL, entry_price REAL, cost_eur REAL, opened TEXT);
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY, ts TEXT, market TEXT, side TEXT, amount REAL,
            price REAL, eur REAL, fee REAL, pnl REAL, live INTEGER, reason TEXT);
        CREATE TABLE IF NOT EXISTS agent_log(
            id INTEGER PRIMARY KEY, ts TEXT, market TEXT, agent TEXT, output TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS benchmark(
            id INTEGER PRIMARY KEY, ts TEXT, btc_price REAL, btc_hold_eur REAL, bot_eur REAL);
        CREATE TABLE IF NOT EXISTS risk_log(id INTEGER PRIMARY KEY, ts TEXT, market TEXT,
            verdict TEXT, amount_eur REAL, risk_eur REAL, decider TEXT, reasons TEXT);
        CREATE TABLE IF NOT EXISTS api_usage(id INTEGER PRIMARY KEY, ts TEXT, agent TEXT,
            model TEXT, input_tokens INTEGER, output_tokens INTEGER, usd REAL);
        CREATE TABLE IF NOT EXISTS short_positions(
            market TEXT PRIMARY KEY, amount REAL, entry_price REAL, cost_eur REAL, opened TEXT);
        CREATE TABLE IF NOT EXISTS short_trades(
            id INTEGER PRIMARY KEY, ts TEXT, market TEXT, side TEXT, amount REAL,
            price REAL, eur REAL, fee REAL, pnl REAL, reason TEXT);
        """)
        cols = [r[1] for r in con.execute("PRAGMA table_info(risk_log)")]
        if "book" not in cols:
            con.execute("ALTER TABLE risk_log ADD COLUMN book TEXT DEFAULT 'long'")


class Engine:
    """Draait de bot in een achtergrondthread. log(tekst, soort) krijgt alle meldingen."""

    def __init__(self, cfg, log=print, db_path=None):
        self.cfg = cfg
        self._ui_log = log
        self.db_path = db_path or settings.db_path()
        self.rules = cfg["rules"]
        self.live = bool(cfg.get("live"))
        self.markets = list(cfg.get("markets") or [])
        self.cycle_minutes = int(cfg.get("cycle_minutes", 120))
        self.models = {a: cfg.get("model") or "claude-haiku-5-5"
                       for a in ["charts", "news", "sentiment", "skeptic", "chief"]}
        if cfg.get("model_chief"):
            self.models["chief"] = cfg["model_chief"]
        keys = cfg["keys"]
        self.bv = Bitvavo({"APIKEY": keys.get("bitvavo_key", ""), "APISECRET": keys.get("bitvavo_secret", "")})
        # Koersen, orderboek en candles zijn openbaar: zonder key, dus zonder IP-whitelist.
        self.pub = Bitvavo({})
        self.ai = anthropic.Anthropic(api_key=keys.get("anthropic") or None)
        self._stop = threading.Event()
        self._thread = None
        self.next_cycle = None
        self.busy = False
        self.cycle_id = None
        self.shorts = bool(cfg.get("shorts_sim", True))
        self._thinking_ok = True
        init_db(self.db_path)
        rc = cfg.get("remote") or {}
        self.remote = None
        if rc.get("enabled") and rc.get("url") and rc.get("token"):
            self.remote = remote.Remote(rc["url"], rc["token"], self._ui_log)

    def log(self, text, kind=""):
        self._ui_log(text, kind)
        if self.remote:
            self.remote.push("log", {"ts": now(), "kind": kind, "text": text})

    # ---------- start / stop ----------
    def problems(self):
        """Wat ontbreekt er nog voordat de bot kan starten?"""
        p = []
        if not self.cfg["keys"].get("anthropic"):
            p.append("Anthropic API-key ontbreekt")
        if not self.markets:
            p.append("geen markten gekozen")
        if self.live and not (self.cfg["keys"].get("bitvavo_key") and self.cfg["keys"].get("bitvavo_secret")):
            p.append("Live staat aan, maar de Bitvavo-key of -secret ontbreekt")
        return p

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        self.log(f"Bot gestart ({'LIVE' if self.live else 'droog draaien'}): max €{self.rules['max_loss_per_trade_eur']} "
                 f"verlies per trade, max {self.rules['max_positions']} posities, daglimiet "
                 f"€{self.rules['daily_loss_limit_eur']}.", "info")
        self.log(f"Model: {self.models['charts']}"
                 + (f", Chief: {self.models['chief']}" if self.models['chief'] != self.models['charts'] else "")
                 + f"; analyse elke {self.cycle_minutes} min; maandlimiet API "
                 + (f"${float(self.cfg.get('max_api_usd_month', 5)):.2f}." if float(self.cfg.get('max_api_usd_month', 5) or 0) > 0 else "uit."),
                 "info")
        self.log("Op afstand meekijken: " + ("aan, de stand gaat naar je telefoon." if self.remote else
                 "uit (zet het aan onder Instellingen > Op afstand meekijken)."), "info" if self.remote else "muted")
        last_cycle = 0.0
        while not self._stop.is_set():
            if Path(settings.stop_path()).exists():
                self.log("STOP-bestand gevonden, bot stopt. Open posities blijven staan.", "bad")
                break
            try:
                self.check_exits()
                if time.time() - last_cycle >= self.cycle_minutes * 60:
                    self.busy = True
                    self.push_status(True)
                    self.run_cycle()
                    self.summary()
                    last_cycle = time.time()
                    self.busy = False
                self.next_cycle = last_cycle + self.cycle_minutes * 60
            except Exception as e:
                self.busy = False
                self.log(f"Fout in loop: {e}", "bad")
            self.push_status(True)
            self._stop.wait(EXIT_CHECK_SECONDS)
        self.next_cycle = None
        self.busy = False
        self.push_status(False)
        self.log("Bot gestopt. Open posities blijven staan; stop-loss werkt pas weer als de bot draait.", "info")
        if self.remote:
            self.remote.close()

    # ---------- database ----------
    def db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def log_agent(self, market, agent, output):
        with self.db() as con:
            con.execute("INSERT INTO agent_log(ts,market,agent,output) VALUES(?,?,?,?)",
                        (now(), market, agent, json.dumps(output, ensure_ascii=False)))

    def get_positions(self):
        with self.db() as con:
            return {r["market"]: dict(r) for r in con.execute("SELECT * FROM positions")}

    def meta_get(self, key):
        with self.db() as con:
            r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key, value):
        with self.db() as con:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))

    # ---------- marktdata ----------
    def price(self, market):
        return float(bv_check(self.pub.tickerPrice({"market": market}), f"koers {market}")["price"])

    def spread_pct(self, market):
        try:
            book = bv_check(self.pub.book(market, {"depth": 1}), "orderboek")
            bid = float(book["bids"][0][0])
            ask = float(book["asks"][0][0])
            return (ask - bid) / ((ask + bid) / 2) * 100
        except Exception:
            return None

    def indicators(self, market):
        raw = bv_check(self.pub.candles(market, "1h", {"limit": 200}), f"candles {market}")
        if not isinstance(raw, list) or len(raw) < 60:
            raise RuntimeError(f"te weinig candles: {raw}")
        raw = list(reversed(raw))
        closes = [float(c[4]) for c in raw]
        vols = [float(c[5]) for c in raw]
        last = closes[-1]
        return {
            "market": market, "price": last, "rsi14": round(rsi(closes), 1),
            "sma20": round(sum(closes[-20:]) / 20, 2), "sma50": round(sum(closes[-50:]) / 50, 2),
            "change_24h_pct": round((last / closes[-25] - 1) * 100, 2),
            "change_7d_pct": round((last / closes[-169] - 1) * 100, 2) if len(closes) > 169 else None,
            "volume_vs_24h_avg": round(vols[-1] / max(sum(vols[-24:]) / 24, 1e-12), 2),
        }

    @staticmethod
    def headlines(limit=15):
        items = []
        for url in FEEDS:
            try:
                for e in feedparser.parse(url).entries[:8]:
                    items.append(e.title)
            except Exception:
                pass
        return items[:limit]

    @staticmethod
    def fear_greed():
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=3", timeout=10).json()["data"]
            return [{"value": int(x["value"]), "label": x["value_classification"]} for x in d]
        except Exception:
            return None

    # ---------- agents ----------
    def _create(self, agent, payload, no_thinking=True):
        kwargs = dict(model=self.models[agent], max_tokens=800, system=PROMPTS[agent],
                      messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        if no_thinking:
            # Nadenken is hier niet nodig: korte JSON-antwoorden, en het kost extra tokens.
            kwargs["thinking"] = {"type": "disabled"}
        return self.ai.messages.create(**kwargs)

    def ask(self, agent, payload):
        try:
            try:
                r = self._create(agent, payload, no_thinking=self._thinking_ok)
            except anthropic.BadRequestError as e:
                if self._thinking_ok and "thinking" in str(e).lower():
                    self._thinking_ok = False  # model kent deze optie niet: zonder opnieuw
                    r = self._create(agent, payload, no_thinking=False)
                else:
                    raise
            kosten.log_usage(agent, self.models[agent], r.usage, self.db_path)
            # Alleen de tekstblokken: modellen kunnen ook denk-blokken teruggeven.
            text = "".join(getattr(b, "text", "") for b in r.content
                           if getattr(b, "type", "") == "text").strip()
            if not text:
                return {"fout": "leeg antwoord (geen tekst)", "score": 0}
            if text.startswith("```"):
                text = text.strip("`").removeprefix("json").strip()
            start, end = text.find("{"), text.rfind("}")
            if start > 0 or (end != -1 and end < len(text) - 1):
                text = text[start:end + 1]
            return json.loads(text)
        except Exception as e:
            return {"fout": str(e)[:300], "score": 0}

    # ---------- risicopoort ----------
    def risk_check_buy(self, market, amount_eur, confidence, book="long"):
        with self.db() as con:
            res = risk_gate.check_buy(con, market, amount_eur, confidence, self.spread_pct(market),
                                      stop_file=settings.stop_path(), rules=self.rules, book=book)
            risk_gate.log_verdict(con, market, res, book)
        self.last_verdict = res
        if res["verdict"] == "BLOCK":
            return 0, "; ".join(res["reasons"])
        plan = risk_gate.exit_plan(self.rules, market, self.price(market), book)
        tag = "PASS (short, simulatie)" if book == "short" else "PASS"
        self.log(f"  {tag} €{res['amount_eur']:.2f} (grens: {res['decider']}, risico €{res['risk_eur']:.2f}), "
                 f"stop {fmt_px(plan['stop_price'])}, doel {fmt_px(plan['target_price'])}", "good")
        return res["amount_eur"], "ok"

    # ---------- orders ----------
    def execute_buy(self, market, eur, reason):
        px = self.price(market)
        fee_rate = self.rules["fee_pct"] / 100
        if self.live:
            res = self.bv.placeOrder(market, "buy", "market", {"amountQuote": f"{eur:.2f}"})
            if res.get("errorCode") == 307:
                self.log("  KOOP mislukt: Bitvavo weigert dit IP-adres (307). Controleer de IP-whitelist van je key.", "bad")
                return
            if "errorCode" in res:
                self.log(f"  KOOP mislukt: {res}", "bad")
                return
            amount = float(res["filledAmount"])
            spent = float(res["filledAmountQuote"])
            fee = float(res.get("feePaid", 0) or 0)
            fill_px = spent / amount if amount else px
        else:
            fee = eur * fee_rate
            amount = (eur - fee) / px
            spent, fill_px = eur, px
        with self.db() as con:
            con.execute("INSERT INTO positions VALUES(?,?,?,?,?)", (market, amount, fill_px, spent, now()))
            con.execute("INSERT INTO trades(ts,market,side,amount,price,eur,fee,pnl,live,reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (now(), market, "buy", amount, fill_px, spent, fee, 0, int(self.live), reason))
        self.push_trade(market, "buy", spent, fill_px, 0, reason)
        self.log(f"  {'LIVE' if self.live else 'DROOG'} KOOP {market}: €{spent:.2f} @ {fmt_px(fill_px)}", "good")

    def execute_sell(self, market, reason):
        pos = self.get_positions().get(market)
        if not pos:
            return
        px = self.price(market)
        amount = pos["amount"]
        if self.live:
            base = market.split("-")[0]
            bal = self.bv.balance({"symbol": base})
            avail = float(bal[0]["available"]) if isinstance(bal, list) and bal else 0.0
            amount = floor8(min(amount, avail))
            if amount <= 0:
                self.log(f"  VERKOOP {market}: geen saldo beschikbaar, positie blijft staan", "bad")
                return
            res = self.bv.placeOrder(market, "sell", "market", {"amount": f"{amount:.8f}"})
            if res.get("errorCode") == 307:
                self.log("  VERKOOP mislukt: Bitvavo weigert dit IP-adres (307). Controleer de IP-whitelist van je key.", "bad")
                return
            if "errorCode" in res:
                self.log(f"  VERKOOP mislukt: {res}", "bad")
                return
            proceeds = float(res["filledAmountQuote"])
            fee = float(res.get("feePaid", 0) or 0)
            fill_px = proceeds / float(res["filledAmount"])
            net = proceeds - fee
        else:
            gross = amount * px
            fee = gross * self.rules["fee_pct"] / 100
            net, fill_px = gross - fee, px
        pnl = net - pos["cost_eur"]
        with self.db() as con:
            con.execute("DELETE FROM positions WHERE market=?", (market,))
            con.execute("INSERT INTO trades(ts,market,side,amount,price,eur,fee,pnl,live,reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (now(), market, "sell", amount, fill_px, net, fee, pnl, int(self.live), reason))
        self.push_trade(market, "sell", net, fill_px, pnl, reason)
        self.log(f"  {'LIVE' if self.live else 'DROOG'} VERKOOP {market}: €{net:.2f} (resultaat {pnl:+.2f})",
                 "good" if pnl >= 0 else "bad")

    def check_exits(self):
        """Stop-loss en koersdoel. Verkopen wordt nooit geblokkeerd."""
        for market, pos in self.get_positions().items():
            why = risk_gate.exit_signal(self.rules, market, pos["entry_price"], self.price(market))
            if why:
                self.log(f"[exit] {market}: {why}", "info")
                self.execute_sell(market, why)
        for market, pos in self.get_short_positions().items():
            why = risk_gate.exit_signal(self.rules, market, pos["entry_price"], self.price(market), "short")
            if why:
                self.log(f"[exit short] {market}: {why}", "info")
                self.close_short(market, why)

    # ---------- shorts (altijd simulatie) ----------
    def get_short_positions(self):
        with self.db() as con:
            return {r["market"]: dict(r) for r in con.execute("SELECT * FROM short_positions")}

    def open_short(self, market, eur, reason):
        """Gesimuleerde short: je 'leent' de munt en verkoopt hem tegen de huidige koers."""
        px = self.price(market)
        fee = eur * self.rules["fee_pct"] / 100
        amount = eur / px
        with self.db() as con:
            con.execute("INSERT INTO short_positions VALUES(?,?,?,?,?)", (market, amount, px, eur, now()))
            con.execute("INSERT INTO short_trades(ts,market,side,amount,price,eur,fee,pnl,reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (now(), market, "short", amount, px, eur, fee, 0, reason))
        self.push_trade(market, "short", eur, px, 0, reason, sim=True)
        self.log(f"  SIM SHORT {market}: €{eur:.2f} @ {fmt_px(px)}", "good")

    def short_pnl(self, pos, px):
        """Resultaat van een short bij koers px: koersdaling min fees en leenkosten."""
        fee_rate = self.rules["fee_pct"] / 100
        days = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(pos["opened"])).total_seconds() / 86400)
        borrow = pos["cost_eur"] * float(self.rules.get("short_borrow_pct_day", 0.05)) / 100 * days
        gross = pos["amount"] * (pos["entry_price"] - px)
        fees = pos["cost_eur"] * fee_rate + pos["amount"] * px * fee_rate
        return gross - fees - borrow, fees, borrow

    def close_short(self, market, reason):
        pos = self.get_short_positions().get(market)
        if not pos:
            return
        px = self.price(market)
        pnl, fees, borrow = self.short_pnl(pos, px)
        with self.db() as con:
            con.execute("DELETE FROM short_positions WHERE market=?", (market,))
            con.execute("INSERT INTO short_trades(ts,market,side,amount,price,eur,fee,pnl,reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (now(), market, "cover", pos["amount"], px, pos["amount"] * px, fees + borrow, pnl, reason))
        self.push_trade(market, "cover", pos["amount"] * px, px, pnl, reason, sim=True)
        self.log(f"  SIM SHORT SLUITEN {market} @ {fmt_px(px)} (resultaat {pnl:+.2f}, waarvan leenkosten "
                 f"{borrow:.2f})", "good" if pnl >= 0 else "bad")

    # ---------- cyclus ----------
    def api_cost_this_month(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.db() as con:
            return con.execute("SELECT COALESCE(SUM(usd),0) FROM api_usage WHERE ts LIKE ?",
                               (month + "%",)).fetchone()[0]

    def run_cycle(self):
        cap = float(self.cfg.get("max_api_usd_month", 5) or 0)
        if cap > 0:
            spent = self.api_cost_this_month()
            if spent >= cap:
                if not getattr(self, "_cap_warned", False):
                    self.log(f"Maandlimiet API-kosten bereikt (${spent:.2f} van ${cap:.2f}). Geen nieuwe analyses "
                             "tot volgende maand; stop-loss en koersdoel worden wel bewaakt.", "bad")
                    self._cap_warned = True
                return
        self._cap_warned = False
        self.log(f"=== Cyclus {datetime.now():%d-%m %H:%M} ({'LIVE' if self.live else 'droog'}) ===", "head")
        self.cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        news = self.headlines()
        sentiment = self.ask("sentiment", {"fear_greed": self.fear_greed()})
        self.log_agent("*", "sentiment", sentiment)
        for market in self.markets:
            if self._stop.is_set():
                return
            try:
                ind = self.indicators(market)
            except Exception as e:
                self.log(f"{market}: data-fout {e}", "bad")
                continue
            pos = self.get_positions().get(market)
            spos = self.get_short_positions().get(market) if self.shorts else None
            charts = self.ask("charts", ind)
            news_r = self.ask("news", {"market": market, "headlines": news})
            reports = {"charts": charts, "news": news_r, "sentiment": sentiment}
            setup = setup_for(ind, charts, news_r)
            if not self.shorts:
                setup["short"] = False
            setup_brief = {"long": setup["long"], "short": setup["short"]}
            skeptic = self.ask("skeptic", {"market": market, "indicatoren": ind, "rapporten": reports,
                                           "setup": setup_brief, "positie": pos})
            chief_in = {"market": market, "indicatoren": ind, "rapporten": reports,
                        "skeptic": skeptic, "setup": setup_brief, "positie": pos}
            if self.shorts:
                chief_in["short_positie"] = spos
            else:
                chief_in["let_op"] = "short en cover zijn uitgeschakeld"
            decision = self.ask("chief", chief_in)
            for name, out in [("charts", charts), ("news", news_r), ("skeptic", skeptic), ("chief", decision)]:
                self.log_agent(market, name, out)
                if "fout" in out and name != "chief":
                    self.log(f"{market}: {name}-agent gaf geen antwoord ({out['fout'][:120]})", "bad")
            self.last_verdict = None
            self.last_setup = {"long": setup["long"], "short": setup["short"], "tekst": setup_text(setup),
                               "long_checks": setup["long_checks"], "short_checks": setup["short_checks"]}
            executed = False
            if "fout" in decision:
                self.log(f"{market}: Chief gaf geen antwoord ({decision['fout'][:120]})", "bad")
                self.push_decision(market, ind, charts, news_r, sentiment, skeptic, decision, executed)
                continue
            action = decision.get("action", "hold")
            self.log(f"{market}: {setup_text(setup)}; Skeptic: {skeptic.get('risico', '?')}", "muted")
            self.log(f"{market}: Chief = {action} (zekerheid {decision.get('confidence')}) - {decision.get('reason')}")
            if action in ("buy", "short"):
                ok_setup = setup["long"] if action == "buy" else setup["short"]
                if not ok_setup:
                    self.log(f"  geen {'koop' if action == 'buy' else 'short'}-setup volgens de regels: houden", "muted")
                    action = "hold"
                elif str(skeptic.get("risico", "")).lower() == "hoog":
                    self.log("  Skeptic ziet hoog risico: houden", "muted")
                    action = "hold"
            if action == "buy" and not pos:
                amount, why = self.risk_check_buy(market, float(decision.get("amount_eur", 0) or 0),
                                                  float(decision.get("confidence", 0) or 0))
                if amount:
                    self.execute_buy(market, amount, decision.get("reason", ""))
                    executed = True
                else:
                    self.log(f"  risicopoort blokkeert: {why}", "muted")
            elif action == "sell" and pos:
                self.execute_sell(market, decision.get("reason", ""))
                executed = True
            elif action == "short" and self.shorts and not spos:
                amount, why = self.risk_check_buy(market, float(decision.get("amount_eur", 0) or 0),
                                                  float(decision.get("confidence", 0) or 0), book="short")
                if amount:
                    self.open_short(market, amount, decision.get("reason", ""))
                    executed = True
                else:
                    self.log(f"  risicopoort blokkeert short: {why}", "muted")
            elif action == "cover" and spos:
                self.close_short(market, decision.get("reason", ""))
                executed = True
            self.push_decision(market, ind, charts, news_r, sentiment, skeptic, decision, executed)

    # ---------- op afstand meekijken ----------
    def push_trade(self, market, side, eur_amount, price, pnl, reason, sim=False):
        if self.remote:
            self.remote.push("trade", {"ts": now(), "market": market, "side": side, "eur": round(eur_amount, 2),
                                       "price": price, "pnl": round(pnl, 2), "live": self.live and not sim,
                                       "sim": sim, "reason": reason})

    def push_decision(self, market, ind, charts, news_r, sentiment, skeptic, decision, executed):
        if not self.remote:
            return
        v = getattr(self, "last_verdict", None)
        self.remote.push("decision", {
            "ts": now(), "cycle_id": self.cycle_id, "market": market, "price": ind.get("price"),
            "indicators": ind, "charts": charts, "news": news_r, "sentiment": sentiment,
            "skeptic": skeptic, "chief": decision,
            "verdict": v["verdict"] if v else None, "verdict_reasons": v["reasons"] if v else [],
            "amount_eur": v["amount_eur"] if v else None, "executed": executed,
            "setup": getattr(self, "last_setup", None)})

    def status_snapshot(self, running):
        budget = float(self.rules["budget_eur"])
        with self.db() as con:
            b = con.execute("SELECT bot_eur, btc_hold_eur FROM benchmark ORDER BY id DESC LIMIT 1").fetchone()
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%"
            pnl, sells = con.execute("SELECT COALESCE(SUM(pnl),0), COUNT(*) FROM trades "
                                     "WHERE side='sell' AND ts LIKE ?", (day,)).fetchone()
            buys = con.execute("SELECT COUNT(*) FROM trades WHERE side='buy' AND ts LIKE ?", (day,)).fetchone()[0]
            since = datetime.now(timezone.utc).timestamp() - 30 * 86400
            since = datetime.fromtimestamp(since, timezone.utc).isoformat()
            usd, calls = con.execute("SELECT COALESCE(SUM(usd),0), COUNT(*) FROM api_usage WHERE ts >= ?",
                                     (since,)).fetchone()
            positions = []
            for r in con.execute("SELECT market, cost_eur, entry_price, opened FROM positions"):
                plan = risk_gate.exit_plan(self.rules, r["market"], r["entry_price"])
                positions.append({"market": r["market"], "cost_eur": round(r["cost_eur"], 2),
                                  "entry": r["entry_price"], "stop": plan["stop_price"],
                                  "target": plan["target_price"], "opened": r["opened"]})
            short_positions = []
            for r in con.execute("SELECT * FROM short_positions"):
                plan = risk_gate.exit_plan(self.rules, r["market"], r["entry_price"], "short")
                short_positions.append({"market": r["market"], "cost_eur": round(r["cost_eur"], 2),
                                        "entry": r["entry_price"], "stop": plan["stop_price"],
                                        "target": plan["target_price"], "opened": r["opened"]})
            stats = {"long": risk_gate.book_stats(con, "long"), "short": risk_gate.book_stats(con, "short")}
        stats["long"]["open_pnl"], stats["short"]["open_pnl"] = self.open_pnl()
        nxt = None
        if running and self.next_cycle:
            nxt = datetime.fromtimestamp(self.next_cycle, timezone.utc).isoformat(timespec="seconds")
        return {"ts": now(), "running": running, "live": self.live, "busy": self.busy, "next_cycle": nxt,
                "budget": budget, "bot_eur": b["bot_eur"] if b else None,
                "btc_hold_eur": b["btc_hold_eur"] if b else None, "today_pnl": round(pnl, 2),
                "buys_today": buys, "sells_today": sells, "daily_limit": self.rules["daily_loss_limit_eur"],
                "api_usd_30d": round(usd, 4), "api_calls_30d": calls, "markets": self.markets,
                "positions": positions, "short_positions": short_positions, "stats": stats}

    def open_pnl(self):
        """Open resultaat (long, short) tegen de huidige koersen."""
        lp = sp = 0.0
        try:
            for m, p in self.get_positions().items():
                lp += p["amount"] * self.price(m) * (1 - self.rules["fee_pct"] / 100) - p["cost_eur"]
            for m, p in self.get_short_positions().items():
                sp += self.short_pnl(p, self.price(m))[0]
        except Exception:
            pass
        return round(lp, 2), round(sp, 2)

    def push_status(self, running):
        if self.remote:
            try:
                self.remote.push("status", self.status_snapshot(running))
            except Exception:
                pass

    def summary(self):
        budget = float(self.rules["budget_eur"])
        unrealized = 0.0
        for m, p in self.get_positions().items():
            unrealized += p["amount"] * self.price(m) - p["cost_eur"]
        with self.db() as con:
            pnl = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='sell'").fetchone()[0]
        try:
            btc = self.price("BTC-EUR")
            start = self.meta_get("btc_start_price")
            if start is None:
                self.meta_set("btc_start_price", btc)
                self.meta_set("bench_start", now())
                start = btc
            btc_hold = budget * btc / float(start)
            bot_eur = budget + pnl + unrealized
            with self.db() as con:
                con.execute("INSERT INTO benchmark(ts,btc_price,btc_hold_eur,bot_eur) VALUES(?,?,?,?)",
                            (now(), btc, btc_hold, bot_eur))
            self.log(f"Stand: bot €{bot_eur:.2f} tegen BTC vasthouden €{btc_hold:.2f} "
                     f"(verschil {bot_eur - btc_hold:+.2f})", "info")
        except Exception as e:
            self.log(f"Benchmark niet bijgewerkt: {e}", "bad")


if __name__ == "__main__":
    cfg = settings.load()
    eng = Engine(cfg, lambda m, k="": print(m))
    missing = eng.problems()
    if missing:
        raise SystemExit("Kan niet starten: " + "; ".join(missing) + f"\nInstellingen: {settings.config_path()}")
    eng.start()
    try:
        while eng.running:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.stop()
