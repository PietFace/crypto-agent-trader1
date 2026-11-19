"""Multi-agent crypto trader voor Bitvavo: de motor.

Agents: Charts, News, Sentiment, Skeptic -> Chief beslist.
De risicopoort (risk_gate.py, gewone code) heeft altijd het laatste woord.

Normaal start je het programma (app.py / CryptoTrader.exe). Zonder venster:
    python bot.py
Instellingen staan in %APPDATA%\\CryptoTrader\\config.json (aan te passen in het programma).
"""
import json
import math
import os
import sqlite3
import dbutil
import threading
import time
from datetime import datetime, timedelta, timezone
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
from followers import Followers, coin_of, follower_setup, short_addr
import risk_gate


def kraken_sym(market):
    import kraken
    return kraken.SYMBOLS.get(market, market)
import settings

FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/",
         "https://cointelegraph.com/rss"]
TICK_SECONDS = 30          # volgers, regels, stop-loss en koersdoel: elke 30 seconden
STATUS_EVERY_SECONDS = 60  # stand naar de telefoon: hooguit eens per minuut
EV_WORD = {"open": "opent", "sluit": "sluit", "draait": "draait naar"}  # stop-loss en koersdoel elke 5 minuten

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
             "- 'volgers' laat zien hoeveel gevolgde, winstgevende traders op Hyperliquid long of short staan "
             "en wat ze recent deden. Een setup kan van de regels of van de volgers komen; weeg mee hoe vers "
             "en hoe eensgezind hun posities zijn.\n"
             "- Sluit een positie alleen als de charts duidelijk zijn omgeslagen (koop-positie: score onder -0.3; "
             "short: score boven 0.3) of als de volgers duidelijk de andere kant op staan. 'Risico beperken' is "
             "geen reden: stop-loss en koersdoel bewaken de positie al. Anders: hold.\n"
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
        return "koop-setup ✓" + (f" ({st['bron_long']})" if st.get("bron_long") else "")
    if st["short"]:
        return "short-setup ✓" + (f" ({st['bron_short']})" if st.get("bron_short") else "")
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
    with dbutil.connect(path) as con:
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
        CREATE TABLE IF NOT EXISTS position_marks(
            book TEXT, market TEXT, price REAL, value_eur REAL, pnl_eur REAL, ts TEXT,
            PRIMARY KEY(book, market));
        CREATE TABLE IF NOT EXISTS short_positions(
            market TEXT PRIMARY KEY, amount REAL, entry_price REAL, cost_eur REAL, opened TEXT);
        CREATE TABLE IF NOT EXISTS short_trades(
            id INTEGER PRIMARY KEY, ts TEXT, market TEXT, side TEXT, amount REAL,
            price REAL, eur REAL, fee REAL, pnl REAL, reason TEXT);
        """)
        cols = [r[1] for r in con.execute("PRAGMA table_info(risk_log)")]
        if "book" not in cols:
            con.execute("ALTER TABLE risk_log ADD COLUMN book TEXT DEFAULT 'long'")
        import followers as _fw
        _fw.init_db(con)
        for t in ("positions", "trades", "short_positions", "short_trades"):
            if "bron" not in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]:
                con.execute(f"ALTER TABLE {t} ADD COLUMN bron TEXT DEFAULT 'regels'")
            if "schaduw" not in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]:
                con.execute(f"ALTER TABLE {t} ADD COLUMN schaduw INTEGER DEFAULT 0")
        for t in ("positions", "short_positions"):
            if "ext_stop" not in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]:
                con.execute(f"ALTER TABLE {t} ADD COLUMN ext_stop TEXT")
            if "echt" not in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]:
                con.execute(f"ALTER TABLE {t} ADD COLUMN echt INTEGER DEFAULT 0")
        import copier
        copier.init_db(con)


RESET_TABLES = ("positions", "trades", "short_positions", "short_trades", "position_marks", "benchmark", "risk_log",
                "copy_positions", "copy_trades")
RESET_META = ("btc_start_price", "bench_start", "btc_price_now", "marks_ts")


def reset_history(db_path):
    """Opnieuw beginnen: alle trades, posities en de vergelijking met BTC wissen, zodat de bot weer op het
    budget start. API-kosten (voor de maandlimiet) en de volgerslijst blijven. Eerst een reservekopie.
    Weigert als er nog echte posities open staan. Geeft het pad van de reservekopie."""
    import shutil
    init_db(db_path)
    with dbutil.connect(db_path) as con:
        real = 0
        for t in ("positions", "short_positions", "copy_positions"):
            real += con.execute(f"SELECT COUNT(*) FROM {t} WHERE COALESCE(echt,0)=1").fetchone()[0]
        if real:
            raise RuntimeError(f"Er staan nog {real} echte posities open. Sluit die eerst (in de bot of op de "
                               "beurs), anders raakt de bot ze kwijt.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(os.path.dirname(db_path), f"trader-reserve-{stamp}.db")
    shutil.copyfile(db_path, backup)
    with dbutil.connect(db_path) as con:
        for t in RESET_TABLES:
            con.execute(f"DELETE FROM {t}")
        con.execute(f"DELETE FROM meta WHERE key IN ({','.join('?' * len(RESET_META))})", RESET_META)
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('reset_at', ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    return backup


class Engine:
    """Draait de bot in een achtergrondthread. log(tekst, soort) krijgt alle meldingen."""

    def __init__(self, cfg, log=print, db_path=None):
        self.cfg = cfg
        self._ui_log = log
        self.db_path = db_path or settings.db_path()
        self.exchange = cfg.get("exchange", "bitvavo")
        self.rules = settings.effective_rules(cfg)
        self.live = bool(cfg.get("live"))
        self.kr = None
        self.lev = max(1, int(self.rules.get("leverage", 1) or 1))
        self.copier = None
        self.kx = None
        if self.exchange == "kraken":
            import kraken
            self.kr = kraken.KrakenPublic()
            k = cfg.get("keys", {})
            if self.live and k.get("kraken_key") and k.get("kraken_secret"):
                self.kx = kraken.KrakenPrivate(k["kraken_key"], k["kraken_secret"], public=self.kr)
            self.live = bool(self.kx)  # zonder Kraken-key nooit live, en zeker nooit via Bitvavo
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
        self.fcfg = cfg.get("followers") or {}
        self.fast = bool(cfg.get("fast_check", True))
        self.reports, self.last_state, self.last_decision = {}, {}, {}
        self.closed_at = {}
        self.followers = None
        if self.fcfg.get("enabled", True):
            self.followers = Followers(self.db_path, settings.data_dir(), self.fcfg, self._ui_log)
        self._thinking_ok = True
        init_db(self.db_path)
        rc = cfg.get("remote") or {}
        self.remote = None
        if rc.get("enabled") and rc.get("url") and rc.get("token"):
            self.remote = remote.Remote(rc["url"], rc["token"], self._ui_log)
        self.mode = cfg.get("mode", "beide")
        cc = cfg.get("copy") or {}
        if self.mode == "kopie" or (cc.get("enabled") and self.mode != "bot"):
            import copier
            self.copier = copier.CopyEngine(self, cc)

    def log(self, text, kind=""):
        self._ui_log(text, kind)
        if self.remote:
            self.remote.push("log", {"ts": now(), "kind": kind, "text": text})

    # ---------- start / stop ----------
    def problems(self):
        """Wat ontbreekt er nog voordat de bot kan starten?"""
        p = []
        if self.mode == "kopie":
            if not (self.cfg.get("copy") or {}).get("traders"):
                p.append("'Alleen kopiëren' staat aan, maar er staan nog geen traders bij Kopiëren")
            return p
        if not self.cfg["keys"].get("anthropic"):
            p.append("Anthropic API-key ontbreekt")
        if not self.markets:
            p.append("geen markten gekozen")
        if self.exchange == "kraken":
            if self.cfg.get("live") and not self.kx:
                p.append("Live staat aan, maar de Kraken-key of -secret ontbreekt")
        elif self.live and not (self.cfg["keys"].get("bitvavo_key") and self.cfg["keys"].get("bitvavo_secret")):
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
        if self.kr:
            lev = self.rules.get("leverage", 1)
            self.log(f"Beurs: Kraken-perps ({'LIVE' if self.kx else 'droog'}), hefboom max {lev}x, kosten "
                     f"{self.rules['fee_pct']}% per kant, shorts {'echt' if self.kx else 'gesimuleerd'} en zonder "
                     "leenkosten. Elke positie krijgt een stop-loss bij Kraken zelf.", "info")
            if self.kx:
                try:
                    total, free = self.kx.collateral_usd()
                    self.kraken_account = {"usd": round(total, 2), "free_usd": round(free, 2), "ts": now()}
                    self.log(f"Kraken-account: ${total:.2f} totaal, ${free:.2f} vrij voor nieuwe posities.", "info")
                except Exception as e:
                    self.log(f"Kraken-account niet te lezen: {e}", "bad")
            try:
                missing = [m for m in self.markets if not self.kr.supported(m)]
                if missing:
                    self.log(f"Niet bij Kraken te verhandelen, worden overgeslagen: {', '.join(missing)}", "bad")
                    self.markets = [m for m in self.markets if m not in missing]
            except Exception as e:
                self.log(f"Kraken niet bereikbaar: {e}", "bad")
        self.log({"beide": "Modus: gewone bot én kopiëren.", "bot": "Modus: alleen de gewone bot (kopiëren staat uit).",
                  "kopie": "Modus: alleen kopiëren. De gewone bot opent niets nieuws en gebruikt geen AI; open "
                           "posities van de gewone bot worden nog wel bewaakt (stop/doel)."}.get(self.mode, ""),
                 "info")
        self.log(f"Bot gestart ({'LIVE' if self.live else 'droog draaien'}): max €{self.rules['max_loss_per_trade_eur']} "
                 f"verlies per trade, max {self.rules['max_positions']} posities, daglimiet "
                 f"€{self.rules['daily_loss_limit_eur']}.", "info")
        self.log(f"Model: {self.models['charts']}"
                 + (f", Chief: {self.models['chief']}" if self.models['chief'] != self.models['charts'] else "")
                 + f"; analyse elke {self.cycle_minutes} min; volgers/regels elke "
                 + f"{max(10, int(self.cfg.get('tick_seconds', TICK_SECONDS)))} s; maandlimiet API "
                 + (f"${float(self.cfg.get('max_api_usd_month', 5)):.2f}." if float(self.cfg.get('max_api_usd_month', 5) or 0) > 0 else "uit."),
                 "info")
        self.log("Op afstand meekijken: " + ("aan, de stand gaat naar je telefoon." if self.remote else
                 "uit (zet het aan onder Instellingen > Op afstand meekijken)."), "info" if self.remote else "muted")
        if self.copier:
            self.copier.start()
        elif self.mode != "bot" and (self.cfg.get("copy") or {}).get("traders"):
            n = len(self.cfg["copy"]["traders"])
            self.log(f"Kopieer-modus staat UIT: {n} trader(s) klaar bij Kopiëren, maar 'Kopieer-modus aan' is niet "
                     "aangevinkt. Vink het aan, klik Opslaan en start de bot opnieuw.", "bad")
        last_cycle = 0.0
        last_status = 0.0
        tick = max(10, int(self.cfg.get("tick_seconds", TICK_SECONDS)))
        while not self._stop.is_set():
            if Path(settings.stop_path()).exists():
                self.log("STOP-bestand gevonden, bot stopt. Open posities blijven staan.", "bad")
                break
            try:
                self.check_exits()
                if self.mode == "kopie":
                    # Alleen kopiëren: geen nieuwe trades en geen AI voor de gewone bot. Staan er nog volgers-
                    # posities open, dan wel de volgers bijhouden zodat die netjes mee kunnen sluiten.
                    if self.followers and (self.get_positions() or self.get_short_positions()):
                        saved, self.fast = self.fast, False
                        try:
                            self.fast_check()
                        finally:
                            self.fast = saved
                elif self.followers or self.fast:
                    self.fast_check()
                if self.mode != "kopie" and time.time() - last_cycle >= self.cycle_minutes * 60:
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
            if time.time() - last_status >= STATUS_EVERY_SECONDS:
                self.push_status(True)
                last_status = time.time()
                try:
                    self.weekly_report()
                except Exception as e:
                    self.log(f"Weekoverzicht mislukt: {e}", "bad")
            self._stop.wait(tick)
        if self.copier:
            self.copier.stop()
        self.next_cycle = None
        self.busy = False
        self.push_status(False)
        self.log("Bot gestopt. Open posities blijven staan; stop-loss werkt pas weer als de bot draait.", "info")
        if self.remote:
            self.remote.close()

    # ---------- database ----------
    def db(self):
        con = dbutil.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def log_agent(self, market, agent, output):
        with self.db() as con:
            con.execute("INSERT INTO agent_log(ts,market,agent,output) VALUES(?,?,?,?)",
                        (now(), market, agent, json.dumps(output, ensure_ascii=False)))

    def get_positions(self):
        with self.db() as con:
            return {r["market"]: dict(r) for r in con.execute("SELECT * FROM positions")}

    def is_shadow(self, bron):
        """Live, maar dit signaal hoort niet bij de live-bronnen: dan alleen naspelen (schaduwtrade)."""
        return self.live and (bron or "regels") not in (self.cfg.get("live_bronnen") or ["volgers", "beide"])

    def mode_label(self, shadow):
        return "SCHADUW" if shadow else ("LIVE" if self.live else "DROOG")

    def meta_get(self, key):
        with self.db() as con:
            r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def meta_set(self, key, value):
        with self.db() as con:
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))

    # ---------- marktdata ----------
    def price(self, market):
        if self.kr:
            return self.kr.price(market)
        return float(bv_check(self.pub.tickerPrice({"market": market}), f"koers {market}")["price"])

    def spread_pct(self, market):
        if self.kr:
            try:
                return self.kr.spread_pct(market)
            except Exception:
                return None
        try:
            book = bv_check(self.pub.book(market, {"depth": 1}), "orderboek")
            bid = float(book["bids"][0][0])
            ask = float(book["asks"][0][0])
            return (ask - bid) / ((ask + bid) / 2) * 100
        except Exception:
            return None

    def daily_sma(self, market, days=20):
        """20-daags gemiddelde van de slotkoers (Bitvavo dagkaarsen, openbaar), een uur in cache."""
        cache = getattr(self, "_dsma", {})
        self._dsma = cache
        hit = cache.get(market)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        try:
            raw = bv_check(self.pub.candles(market, "1d", {"limit": days}), f"dagkaarsen {market}")
            closes = [float(c[4]) for c in raw]
            sma = sum(closes) / len(closes) if closes else None
        except Exception:
            sma = None
        cache[market] = (time.time(), sma)
        return sma

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
    def risk_check_buy(self, market, amount_eur, confidence, book="long", bron="regels"):
        rules = self.rules_for(bron)
        with self.db() as con:
            res = risk_gate.check_buy(con, market, amount_eur, confidence, self.spread_pct(market),
                                      stop_file=settings.stop_path(), rules=rules, book=book)
            risk_gate.log_verdict(con, market, res, book)
        self.last_verdict = res
        if res["verdict"] == "BLOCK":
            return 0, "; ".join(res["reasons"])
        plan = risk_gate.exit_plan(rules, market, self.price(market), book)
        tag = "PASS (short, simulatie)" if book == "short" else "PASS"
        doel = ("tot de volgers sluiten" if self.follower_held(bron) else fmt_px(plan["target_price"]))
        self.log(f"  {tag} €{res['amount_eur']:.2f} (grens: {res['decider']}, risico €{res['risk_eur']:.2f}), "
                 f"stop {fmt_px(plan['stop_price'])}, doel {doel}", "good")
        return res["amount_eur"], "ok"

    # ---------- orders ----------
    def follower_held(self, bron):
        return bool(self.fcfg.get("follow_exit", True)) and (bron or "regels") in ("volgers", "beide")

    def rules_for(self, bron):
        """Regels voor een positie. Volgers-trades: geen koersdoel (we blijven tot de volgers zelf sluiten) en een
        ruimere noodstop (standaard 2x). De positie wordt daardoor kleiner, zodat het max verlies gelijk blijft."""
        if not self.follower_held(bron):
            return self.rules
        r = dict(self.rules)
        mult = float(self.fcfg.get("follow_stop_mult", 2.0) or 2.0)
        r["stop_pct"] = {k: float(v) * mult for k, v in self.rules["stop_pct"].items()}
        r["reward_risk"] = 1000.0
        return r

    def main_holds_real(self, symbol):
        """Heeft de hoofdbot een echte Kraken-positie op dit symbool? (voor de kopieer-modus)"""
        if not self.kx:
            return False
        import kraken
        for book in (self.get_positions(), self.get_short_positions()):
            for m, p in book.items():
                if p.get("echt") and kraken.SYMBOLS.get(m) == symbol:
                    return True
        return False

    def kraken_conflict(self, market, book):
        """Kraken telt long en short op dezelfde munt bij elkaar op. Dus nooit allebei tegelijk echt."""
        if self.copier and kraken_sym(market) in self.copier.open_symbols(real_only=True):
            return True
        other = self.get_short_positions() if book == "long" else self.get_positions()
        pos = other.get(market)
        return bool(pos and pos.get("echt"))

    def kraken_open(self, market, side, eur, book, bron="regels"):
        """Echte positie openen bij Kraken plus stop-loss bij Kraken. Geeft (aantal, prijs, stop_id)."""
        size, _ = self.kx.size_for(market, eur)
        if size <= 0:
            raise RuntimeError(f"€{eur:.2f} is te weinig voor de kleinste ordergrootte van {market}")
        amount, fill_px, _ = self.kx.market_order(market, side, size)
        plan = risk_gate.exit_plan(self.rules_for(bron), market, fill_px, book)
        try:
            stop_id = self.kx.stop_order(market, "sell" if side == "buy" else "buy", amount, plan["stop_price"])
        except Exception as e:
            self.log(f"  Stop-loss bij Kraken mislukt ({e}): positie meteen weer gesloten", "bad")
            self.kx.market_order(market, "sell" if side == "buy" else "buy", amount, reduce_only=True)
            raise
        return amount, fill_px, stop_id

    def kraken_close(self, market, side, pos):
        """Echte positie sluiten. Geeft (aantal, prijs) of None als Kraken hem al gesloten had."""
        self.kx.cancel(pos.get("ext_stop"))
        try:
            amount, fill_px, _ = self.kx.market_order(market, side, pos["amount"], reduce_only=True)
            return amount, fill_px
        except Exception as e:
            if "wouldNotReducePosition" in str(e):
                return None
            raise

    def execute_buy(self, market, eur, reason, bron="regels"):
        px = self.price(market)
        fee_rate = self.rules["fee_pct"] / 100
        shadow = self.is_shadow(bron)
        real = self.live and not shadow
        ext_stop = None
        if real and self.kx:
            if self.kraken_conflict(market, "long"):
                self.log(f"  {market}: er staat al een echte short, Kraken kan niet tegelijk long. Overgeslagen.", "muted")
                return
            try:
                amount, fill_px, ext_stop = self.kraken_open(market, "buy", eur, "long", bron)
            except Exception as e:
                self.log(f"  KOOP bij Kraken mislukt: {e}", "bad")
                return
            spent = amount * fill_px
            fee = spent * fee_rate
            spent += fee
        elif real:
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
            con.execute("INSERT INTO positions(market,amount,entry_price,cost_eur,opened,bron,schaduw,ext_stop,echt) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (market, amount, fill_px, spent, now(), bron, int(shadow), ext_stop, int(real)))
            con.execute("INSERT INTO trades(ts,market,side,amount,price,eur,fee,pnl,live,reason,bron,schaduw) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now(), market, "buy", amount, fill_px, spent, fee, 0, int(real), reason, bron, int(shadow)))
        self.push_trade(market, "buy", spent, fill_px, 0, ("[schaduw] " if shadow else "") + reason, sim=not real,
                        schaduw=shadow)
        self.log(f"  {self.mode_label(shadow)} KOOP {market}: €{spent:.2f} @ {fmt_px(fill_px)}", "good")

    def execute_sell(self, market, reason):
        pos = self.get_positions().get(market)
        if not pos:
            return
        px = self.price(market)
        amount = pos["amount"]
        shadow = bool(pos.get("schaduw"))
        real = bool(pos.get("echt")) if self.kx else (self.live and not shadow)
        if real and self.kx:
            try:
                done = self.kraken_close(market, "sell", pos)
            except Exception as e:
                self.log(f"  VERKOOP bij Kraken mislukt: {e}. De stop-loss bij Kraken is mogelijk weg!", "bad")
                return
            if done is None:
                fill_px = risk_gate.exit_plan(self.rules_for(pos.get("bron")), market, pos["entry_price"])["stop_price"]
                reason = "stop-loss (al uitgevoerd bij Kraken)"
            else:
                amount, fill_px = done
            gross = amount * fill_px
            fee = gross * self.rules["fee_pct"] / 100
            net = gross - fee
        elif real:
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
            con.execute("INSERT INTO trades(ts,market,side,amount,price,eur,fee,pnl,live,reason,bron,schaduw) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now(), market, "sell", amount, fill_px, net, fee, pnl, int(real), reason,
                         pos.get("bron") or "regels", int(shadow)))
        self.push_trade(market, "sell", net, fill_px, pnl, ("[schaduw] " if shadow else "") + reason, sim=not real,
                        schaduw=shadow)
        self.log(f"  {self.mode_label(shadow)} VERKOOP {market}: €{net:.2f} (resultaat {pnl:+.2f})",
                 "good" if pnl >= 0 else "bad")

    def check_exits(self):
        """Stop-loss en koersdoel, en de actuele waarde van elke open positie. Verkopen wordt nooit geblokkeerd."""
        if self.kx:
            self.sync_kraken()
        fee = self.rules["fee_pct"] / 100
        marks = []
        for market, pos in self.get_positions().items():
            px = self.price(market)
            value = pos["amount"] * px * (1 - fee)  # wat je na verkoop (incl. fee) zou krijgen
            marks.append(("long", market, px, value, value - pos["cost_eur"]))
            why = risk_gate.exit_signal(self.rules_for(pos.get("bron")), market, pos["entry_price"], px)
            if why:
                self.log(f"[exit] {market}: {why}", "info")
                self.execute_sell(market, why)
        for market, pos in self.get_short_positions().items():
            px = self.price(market)
            pnl = self.short_pnl(pos, px)[0]
            marks.append(("short", market, px, pos["cost_eur"] + pnl, pnl))
            why = risk_gate.exit_signal(self.rules_for(pos.get("bron")), market, pos["entry_price"], px, "short")
            if why:
                self.log(f"[exit short] {market}: {why}", "info")
                self.close_short(market, why)
        try:
            self.meta_set("btc_price_now", self.price("BTC-EUR"))
            self.meta_set("marks_ts", now())
        except Exception:
            pass
        with self.db() as con:
            con.execute("DELETE FROM position_marks")
            con.executemany("INSERT INTO position_marks(book,market,price,value_eur,pnl_eur,ts) VALUES(?,?,?,?,?,?)",
                            [(b, m, px, v, p, now()) for b, m, px, v, p in marks])

    # ---------- shorts (gesimuleerd, behalve op Kraken live) ----------
    def get_short_positions(self):
        with self.db() as con:
            return {r["market"]: dict(r) for r in con.execute("SELECT * FROM short_positions")}

    def open_short(self, market, eur, reason, bron="regels"):
        """Short. Bitvavo en droog: gesimuleerd. Kraken live: echt, met stop-loss bij Kraken."""
        px = self.price(market)
        shadow = self.is_shadow(bron)
        real = bool(self.kx) and not shadow
        ext_stop = None
        if real:
            if self.kraken_conflict(market, "short"):
                self.log(f"  {market}: er staat al een echte long, Kraken kan niet tegelijk short. Overgeslagen.", "muted")
                return
            try:
                amount, px, ext_stop = self.kraken_open(market, "sell", eur, "short", bron)
            except Exception as e:
                self.log(f"  SHORT bij Kraken mislukt: {e}", "bad")
                return
            eur = amount * px
        else:
            amount = eur / px
        fee = eur * self.rules["fee_pct"] / 100
        with self.db() as con:
            con.execute("INSERT INTO short_positions(market,amount,entry_price,cost_eur,opened,bron,schaduw,ext_stop,echt) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (market, amount, px, eur, now(), bron, int(shadow and bool(self.kx)), ext_stop, int(real)))
            con.execute("INSERT INTO short_trades(ts,market,side,amount,price,eur,fee,pnl,reason,bron,schaduw) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (now(), market, "short", amount, px, eur, fee, 0, reason, bron, int(shadow and bool(self.kx))))
        self.push_trade(market, "short", eur, px, 0, reason, sim=not real)
        self.log(f"  {'LIVE' if real else 'SIM'} SHORT {market}: €{eur:.2f} @ {fmt_px(px)}", "good")

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
        real = bool(pos.get("echt")) and bool(self.kx)
        if real:
            try:
                done = self.kraken_close(market, "buy", pos)
            except Exception as e:
                self.log(f"  SHORT SLUITEN bij Kraken mislukt: {e}. De stop-loss bij Kraken is mogelijk weg!", "bad")
                return
            if done is None:
                px = risk_gate.exit_plan(self.rules_for(pos.get("bron")), market, pos["entry_price"],
                                         "short")["stop_price"]
                reason = "stop-loss (al uitgevoerd bij Kraken)"
            else:
                px = done[1]
        pnl, fees, borrow = self.short_pnl(pos, px)
        with self.db() as con:
            con.execute("DELETE FROM short_positions WHERE market=?", (market,))
            con.execute("INSERT INTO short_trades(ts,market,side,amount,price,eur,fee,pnl,reason,bron,schaduw) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (now(), market, "cover", pos["amount"], px, pos["amount"] * px, fees + borrow, pnl, reason,
                         pos.get("bron") or "regels", pos.get("schaduw") or 0))
        self.push_trade(market, "cover", pos["amount"] * px, px, pnl, reason, sim=not real)
        self.log(f"  {'LIVE' if real else 'SIM'} SHORT SLUITEN {market} @ {fmt_px(px)} (resultaat {pnl:+.2f}"
                 + (f", waarvan leenkosten {borrow:.2f})" if borrow else ")"), "good" if pnl >= 0 else "bad")

    def sync_kraken(self):
        """Posities die Kraken zelf gesloten heeft (stop-loss geraakt terwijl de bot niet keek) bijwerken."""
        try:
            open_at_kraken = self.kx.open_positions()
        except Exception as e:
            self.log(f"Kraken-posities niet te lezen: {e}", "bad")
            return
        import kraken
        for market, pos in self.get_positions().items():
            if pos.get("echt") and kraken.SYMBOLS.get(market) not in open_at_kraken:
                self.log(f"[Kraken] {market}: positie is bij Kraken gesloten (stop-loss)", "bad")
                self.execute_sell(market, "stop-loss (bij Kraken)")
        for market, pos in self.get_short_positions().items():
            if pos.get("echt") and kraken.SYMBOLS.get(market) not in open_at_kraken:
                self.log(f"[Kraken] {market}: short is bij Kraken gesloten (stop-loss)", "bad")
                self.close_short(market, "stop-loss (bij Kraken)")

    # ---------- cyclus ----------
    def api_cost_this_month(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        with self.db() as con:
            return con.execute("SELECT COALESCE(SUM(usd),0) FROM api_usage WHERE ts LIKE ?",
                               (month + "%",)).fetchone()[0]

    def cap_reached(self):
        cap = float(self.cfg.get("max_api_usd_month", 5) or 0)
        if cap > 0:
            spent = self.api_cost_this_month()
            if spent >= cap:
                if not getattr(self, "_cap_warned", False):
                    self.log(f"Maandlimiet API-kosten bereikt (${spent:.2f} van ${cap:.2f}). Geen nieuwe analyses "
                             "tot volgende maand; stop-loss en koersdoel worden wel bewaakt.", "bad")
                    self._cap_warned = True
                return True
        self._cap_warned = False
        return False

    def run_cycle(self):
        """Grote ronde (elke cycle_minutes): nieuws, sentiment en charts per munt, daarna een beslissing."""
        if self.cap_reached():
            return
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
            charts = self.ask("charts", ind)
            news_r = self.ask("news", {"market": market, "headlines": news})
            self.reports[market] = {"charts": charts, "news": news_r, "sentiment": sentiment, "t": time.time()}
            for name, out in [("charts", charts), ("news", news_r)]:
                self.log_agent(market, name, out)
                if "fout" in out:
                    self.log(f"{market}: {name}-agent gaf geen antwoord ({out['fout'][:120]})", "bad")
            self.decide(market, ind, "cyclus")

    def fast_check(self):
        """Snelle ronde (elke 5 minuten, zonder AI): volgers en regels bekijken.
        Alleen als er voor een munt een NIEUWE setup ontstaat, worden Skeptic en Chief gevraagd."""
        if self.followers:
            try:
                events = self.followers.update([coin_of(m) for m in self.markets])
                for ev in events:
                    self.log(f"Volger {short_addr(ev['address'])} {EV_WORD.get(ev['event'], ev['event'])} {ev['side'].upper()} {ev['coin']} "
                             f"(${ev['notional']:,})".replace(",", "."), "info")
            except Exception as e:
                if not getattr(self, "_fw_warned", False):
                    self.log(f"Volgers: posities niet op te halen ({e})", "bad")
                    self._fw_warned = True
            else:
                self._fw_warned = False
        try:
            self.follower_exits()
        except Exception as e:
            self.log(f"Volgers-exit mislukt: {e}", "bad")
        if not self.fast or not self.reports:
            return
        cooldown = float(self.cfg.get("fast_cooldown_min", 30)) * 60
        for market in self.markets:
            rep = self.reports.get(market)
            if not rep or self._stop.is_set():
                continue
            try:
                ind = self.indicators(market)
            except Exception:
                continue
            setup = self.combined_setup(market, ind, rep["charts"], rep["news"])
            state = ("long" if setup["long"] else "") + ("short" if setup["short"] else "")
            prev = self.last_state.get(market, "")
            self.last_state[market] = state
            if not state or state == prev:
                continue
            if time.time() - self.last_decision.get(market, 0) < cooldown:
                continue
            if self.cap_reached():
                return
            self.log(f"{market}: nieuwe setup tussendoor ({setup_text(setup)}): Skeptic en Chief gevraagd", "head")
            self.cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            self.decide(market, ind, "snelle check")

    def combined_setup(self, market, ind, charts, news_r):
        """Setup volgens de regels én volgens de volgers. Geeft ook aan welke bron het signaal gaf."""
        setup = setup_for(ind, charts, news_r)
        if self.rules.get("daily_trend"):
            # Trend op dagbasis (zoals in de backtest-variant): alleen kopen boven het 20-daags gemiddelde,
            # alleen shorten eronder. Geldt voor de grafiekregels, niet voor de volgers.
            sma = self.daily_sma(market)
            px = ind.get("price")
            if sma and px:
                if px < sma and setup["long"]:
                    setup["long"] = False
                    setup.setdefault("notes", []).append("onder 20-daags gemiddelde: geen koop")
                if px > sma and setup["short"]:
                    setup["short"] = False
                    setup.setdefault("notes", []).append("boven 20-daags gemiddelde: geen short")
        setup["bron_long"] = "regels" if setup["long"] else ""
        setup["bron_short"] = "regels" if setup["short"] else ""
        setup["volgers"] = None
        if self.followers:
            cons = self.followers.consensus(coin_of(market))
            fs = follower_setup(cons, ind, charts, self.fcfg)
            setup["volgers"] = cons
            setup["volger_checks"] = {"long": fs["long_checks"], "short": fs["short_checks"]}
            if self.fcfg.get("use_as_setup", True):
                for side in ("long", "short"):
                    if fs[side]:
                        setup["bron_" + side] = "beide" if setup[side] else "volgers"
                        setup[side] = True
        if not self.shorts:
            setup["short"] = False
        return setup

    def close_reasons(self, market, side, charts, bron=None):
        """Mag een positie op eigen initiatief (Chief of volgers) gesloten worden? Geeft lijst met redenen.
        side='long' (koop-positie) of 'short'. Leeg = niet sluiten; stop-loss en koersdoel blijven gewoon werken."""
        why = []
        try:
            cs = float(charts.get("score")) if charts and "fout" not in charts else 0.0
        except (TypeError, ValueError):
            cs = 0.0
        held = self.follower_held(bron)    # volgers-trade: de grafiek alleen is geen reden om eruit te gaan
        if side == "long" and cs <= -0.3 and not held:
            why.append(f"charts omgeslagen ({cs:+.2f})")
        if side == "short" and cs >= 0.3 and not held:
            why.append(f"charts omgeslagen ({cs:+.2f})")
        if self.followers:
            c = self.followers.consensus(coin_of(market))
            min_tr = int(self.fcfg.get("min_traders", 4))
            min_sc = float(self.fcfg.get("min_consensus", 0.6))
            if side == "long" and c["short"] >= min_tr and c["score"] <= -min_sc:
                why.append(f"volgers draaien naar short ({c['long']} long / {c['short']} short)")
            if side == "short" and c["long"] >= min_tr and c["score"] >= min_sc:
                why.append(f"volgers draaien naar long ({c['long']} long / {c['short']} short)")
        return why

    def followers_still_with(self, market, side):
        """Staan de volgers nog duidelijk aan dezelfde kant als onze positie?"""
        if not self.followers:
            return False
        c = self.followers.consensus(coin_of(market))
        min_tr = int(self.fcfg.get("min_traders", 4))
        min_sc = float(self.fcfg.get("min_consensus", 0.6))
        if side == "long":
            return c["long"] >= min_tr and c["score"] >= min_sc
        return c["short"] >= min_tr and c["score"] <= -min_sc

    def followers_left(self, market, side):
        """Zijn de volgers grotendeels uit deze kant gestapt? (minder dan de helft van het instap-minimum over)"""
        if not self.followers:
            return False
        c = self.followers.consensus(coin_of(market))
        need = max(2, int(self.fcfg.get("min_traders", 4)) // 2)
        return (c["long"] if side == "long" else c["short"]) < need

    def reopen_blocked(self, market, side):
        """Na zelf sluiten een uur niet opnieuw dezelfde kant op (voorkomt heen-en-weer en fees)."""
        t = self.closed_at.get((market, side))
        pause = float(self.cfg.get("reopen_pause_min", 60)) * 60
        return t is not None and time.time() - t < pause

    def follower_exits(self):
        """Zonder AI: sluit posities als de volgers duidelijk de andere kant op draaien."""
        if not self.followers or not self.fcfg.get("exit_on_flip", True):
            return
        for market, pos in self.get_positions().items():
            why = [w for w in self.close_reasons(market, "long", {}) if w.startswith("volgers")]
            if not why and self.follower_held(pos.get("bron")) and self.followers_left(market, "long"):
                why = ["volgers zijn uit hun long gestapt"]
            if why:
                self.log(f"[exit] {market}: {why[0]}", "info")
                self.execute_sell(market, why[0])
                self.closed_at[(market, "long")] = time.time()
        for market, pos in self.get_short_positions().items():
            why = [w for w in self.close_reasons(market, "short", {}) if w.startswith("volgers")]
            if not why and self.follower_held(pos.get("bron")) and self.followers_left(market, "short"):
                why = ["volgers zijn uit hun short gestapt"]
            if why:
                self.log(f"[exit short] {market}: {why[0]}", "info")
                self.close_short(market, why[0])
                self.closed_at[(market, "short")] = time.time()

    def decide(self, market, ind, trigger):
        """Skeptic en Chief vragen en de keuze uitvoeren (via de risicopoort)."""
        rep = self.reports.get(market) or {}
        charts, news_r, sentiment = rep.get("charts", {}), rep.get("news", {}), rep.get("sentiment", {})
        pos = self.get_positions().get(market)
        spos = self.get_short_positions().get(market) if self.shorts else None
        reports = {"charts": charts, "news": news_r, "sentiment": sentiment}
        setup = self.combined_setup(market, ind, charts, news_r)
        setup_brief = {"long": setup["long"], "short": setup["short"]}
        volgers = None
        if setup.get("volgers"):
            c = setup["volgers"]
            volgers = {"long": c["long"], "short": c["short"], "consensus": c["score"],
                       "van_de": c["volgers"], "recent": c["recent"][:3]}
        skeptic = self.ask("skeptic", {"market": market, "indicatoren": ind, "rapporten": reports,
                                       "volgers": volgers, "setup": setup_brief, "positie": pos})
        chief_in = {"market": market, "indicatoren": ind, "rapporten": reports, "volgers": volgers,
                    "skeptic": skeptic, "setup": setup_brief, "positie": pos}
        if self.shorts:
            chief_in["short_positie"] = spos
        else:
            chief_in["let_op"] = "short en cover zijn uitgeschakeld"
        decision = self.ask("chief", chief_in)
        self.last_decision[market] = time.time()
        for name, out in [("skeptic", skeptic), ("chief", decision)]:
            self.log_agent(market, name, out)
        if "fout" in skeptic:
            self.log(f"{market}: skeptic-agent gaf geen antwoord ({skeptic['fout'][:120]})", "bad")
        self.last_verdict = None
        self.last_setup = {"long": setup["long"], "short": setup["short"], "tekst": setup_text(setup),
                           "long_checks": setup["long_checks"], "short_checks": setup["short_checks"],
                           "bron_long": setup["bron_long"], "bron_short": setup["bron_short"],
                           "volgers": setup.get("volgers"), "volger_checks": setup.get("volger_checks"),
                           "trigger": trigger}
        executed = False
        if "fout" in decision:
            self.log(f"{market}: Chief gaf geen antwoord ({decision['fout'][:120]})", "bad")
            self.push_decision(market, ind, charts, news_r, sentiment, skeptic, decision, executed)
            return
        action = decision.get("action", "hold")
        vtxt = ""
        if setup.get("volgers"):
            c = setup["volgers"]
            vtxt = f"; volgers {c['long']} long / {c['short']} short"
        self.log(f"{market}: {setup_text(setup)}{vtxt}; Skeptic: {skeptic.get('risico', '?')}", "muted")
        self.log(f"{market}: Chief = {action} (zekerheid {decision.get('confidence')}) - {decision.get('reason')}")
        if action in ("buy", "short"):
            ok_setup = setup["long"] if action == "buy" else setup["short"]
            side = "long" if action == "buy" else "short"
            if ok_setup and self.reopen_blocked(market, side):
                self.log(f"  {market} is minder dan een uur geleden zelf gesloten: nog niet opnieuw {side}", "muted")
                action = "hold"
            elif not ok_setup:
                self.log(f"  geen {'koop' if action == 'buy' else 'short'}-setup: houden", "muted")
                action = "hold"
            elif str(skeptic.get("risico", "")).lower() == "hoog":
                self.log("  Skeptic ziet hoog risico: houden", "muted")
                action = "hold"
        if action == "buy" and not pos:
            amount, why = self.risk_check_buy(market, float(decision.get("amount_eur", 0) or 0) * self.lev,
                                              float(decision.get("confidence", 0) or 0), bron=setup["bron_long"])
            if amount:
                self.execute_buy(market, amount, decision.get("reason", ""), bron=setup["bron_long"])
                executed = True
            else:
                self.log(f"  risicopoort blokkeert: {why}", "muted")
        elif action == "sell" and pos:
            why = self.close_reasons(market, "long", charts, pos.get("bron"))
            if why:
                self.execute_sell(market, f"{decision.get('reason', '')} [{'; '.join(why)}]")
                self.closed_at[(market, "long")] = time.time()
                executed = True
            else:
                extra = " en de volgers staan nog long" if self.followers_still_with(market, "long") else ""
                self.log(f"  Chief wil verkopen, maar de charts zijn niet omgeslagen{extra}: positie blijft, "
                         "stop-loss en koersdoel bewaken hem", "muted")
        elif action == "short" and self.shorts and not spos:
            amount, why = self.risk_check_buy(market, float(decision.get("amount_eur", 0) or 0) * self.lev,
                                              float(decision.get("confidence", 0) or 0), book="short",
                                              bron=setup["bron_short"])
            if amount:
                self.open_short(market, amount, decision.get("reason", ""), bron=setup["bron_short"])
                executed = True
            else:
                self.log(f"  risicopoort blokkeert short: {why}", "muted")
        elif action == "cover" and spos:
            why = self.close_reasons(market, "short", charts, spos.get("bron"))
            if why:
                self.close_short(market, f"{decision.get('reason', '')} [{'; '.join(why)}]")
                self.closed_at[(market, "short")] = time.time()
                executed = True
            else:
                extra = " en de volgers staan nog short" if self.followers_still_with(market, "short") else ""
                self.log(f"  Chief wil de short sluiten, maar de charts zijn niet omgeslagen{extra}: short blijft, "
                         "stop-loss en koersdoel bewaken hem", "muted")
        self.push_decision(market, ind, charts, news_r, sentiment, skeptic, decision, executed)

    # ---------- op afstand meekijken ----------
    def push_trade(self, market, side, eur_amount, price, pnl, reason, sim=False, kind="bot", trader=None,
                   schaduw=False):
        if self.remote:
            self.remote.push("trade", {"ts": now(), "market": market, "side": side, "eur": round(eur_amount, 2),
                                       "price": price, "pnl": round(pnl, 2), "live": self.live and not sim,
                                       "sim": sim, "reason": reason, "kind": kind, "trader": trader,
                                       "schaduw": schaduw})

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
                                     "WHERE side='sell' AND ts LIKE ? AND COALESCE(schaduw,0)=0", (day,)).fetchone()
            buys = con.execute("SELECT COUNT(*) FROM trades WHERE side='buy' AND ts LIKE ? AND COALESCE(schaduw,0)=0", (day,)).fetchone()[0]
            since = datetime.now(timezone.utc).timestamp() - 30 * 86400
            since = datetime.fromtimestamp(since, timezone.utc).isoformat()
            usd, calls = con.execute("SELECT COALESCE(SUM(usd),0), COUNT(*) FROM api_usage WHERE ts >= ?",
                                     (since,)).fetchone()
            marks = {(r["book"], r["market"]): dict(r) for r in con.execute("SELECT * FROM position_marks")}
            positions = []
            for r in con.execute("SELECT market, cost_eur, entry_price, opened, bron, COALESCE(schaduw,0) AS schaduw "
                                 "FROM positions"):
                plan = risk_gate.exit_plan(self.rules_for(r["bron"] if "bron" in r.keys() else None), r["market"],
                                           r["entry_price"])
                mk = marks.get(("long", r["market"])) or {}
                positions.append({"market": r["market"], "cost_eur": round(r["cost_eur"], 2),
                                  "entry": r["entry_price"], "stop": plan["stop_price"],
                                  "target": plan["target_price"], "opened": r["opened"],
                                  "price_now": mk.get("price"), "value_now": round(mk["value_eur"], 2) if mk else None,
                                  "pnl_now": round(mk["pnl_eur"], 2) if mk else None,
                                  "pnl_pct": round(mk["pnl_eur"] / r["cost_eur"] * 100, 2) if mk and r["cost_eur"] else None,
                                  "marked_at": mk.get("ts"), "bron": r["bron"] or "regels",
                                  "schaduw": bool(r["schaduw"])})
            short_positions = []
            for r in con.execute("SELECT * FROM short_positions"):
                plan = risk_gate.exit_plan(self.rules_for(r["bron"] if "bron" in r.keys() else None), r["market"],
                                           r["entry_price"], "short")
                mk = marks.get(("short", r["market"])) or {}
                short_positions.append({"market": r["market"], "cost_eur": round(r["cost_eur"], 2),
                                        "entry": r["entry_price"], "stop": plan["stop_price"],
                                        "target": plan["target_price"], "opened": r["opened"],
                                        "price_now": mk.get("price"),
                                        "value_now": round(mk["value_eur"], 2) if mk else None,
                                        "pnl_now": round(mk["pnl_eur"], 2) if mk else None,
                                        "pnl_pct": round(mk["pnl_eur"] / r["cost_eur"] * 100, 2) if mk and r["cost_eur"] else None,
                                        "marked_at": mk.get("ts"),
                                        "bron": (r["bron"] if "bron" in r.keys() else None) or "regels",
                                        "schaduw": bool(r["schaduw"]) if "schaduw" in r.keys() else False,
                                        "echt": bool(r["echt"]) if "echt" in r.keys() else False})
            stats = {"long": risk_gate.book_stats(con, "long"), "short": risk_gate.book_stats(con, "short")}
            stats_bron = {bk: {br: risk_gate.book_stats(con, bk, br) for br in ("regels", "volgers", "beide")}
                          for bk in ("long", "short")}
        stats["long"]["open_pnl"], stats["short"]["open_pnl"] = self.open_pnl()
        # Live: gerealiseerd resultaat + actuele waarde van open posities, en BTC vasthouden tegen de koers van nu.
        real_pos = [p for p in positions if not p["schaduw"]]  # schaduwtrades zijn geen echt geld
        open_value = sum(p["value_now"] or 0 for p in real_pos)
        open_pnl = sum(p["pnl_now"] or 0 for p in real_pos)
        live_bot = live_hold = None
        try:
            with self.db() as con:
                realized = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='sell' AND COALESCE(schaduw,0)=0").fetchone()[0]
            live_bot = round(budget + realized + open_pnl, 2)
            start = self.meta_get("btc_start_price")
            if start:
                live_hold = round(budget * self.price("BTC-EUR") / float(start), 2)
        except Exception:
            pass
        nxt = None
        if running and self.next_cycle:
            nxt = datetime.fromtimestamp(self.next_cycle, timezone.utc).isoformat(timespec="seconds")
        return {"ts": now(), "running": running, "live": self.live, "busy": self.busy, "next_cycle": nxt,
                "budget": budget, "bot_eur": live_bot if live_bot is not None else (b["bot_eur"] if b else None),
                "btc_hold_eur": live_hold if live_hold is not None else (b["btc_hold_eur"] if b else None),
                "open_value": round(open_value, 2), "open_pnl": round(open_pnl, 2), "today_pnl": round(pnl, 2),
                "buys_today": buys, "sells_today": sells, "daily_limit": self.rules["daily_loss_limit_eur"],
                "api_usd_30d": round(usd, 4), "api_calls_30d": calls, "markets": self.markets,
                "positions": positions, "short_positions": short_positions, "stats": stats,
                "stats_bron": stats_bron, "followers": self.follower_snapshot(),
                # Extra voor de website (modus, beurs, kopiëren, limieten, reset)
                "mode": self.mode, "exchange": self.exchange, "leverage": self.lev,
                "total_budget": float(self.cfg["rules"]["budget_eur"]),
                "copy_budget": settings.budgets(self.cfg)[1],
                "kraken_account": getattr(self, "kraken_account", None),
                "copy": self.copy_snapshot(), "reset_at": self.meta_get("reset_at"),
                "limits": self.limits_snapshot()}

    def limits_snapshot(self):
        """Eén daglimiet voor alles: hoeveel is er vandaag al gebruikt?"""
        try:
            with self.db() as con:
                loss, open_risk = risk_gate.day_risk_all(con, self.rules)
            return {"daily_limit": self.rules["daily_loss_limit_eur"], "loss_today": round(loss, 2),
                    "open_risk": round(open_risk, 2),
                    "room": round(max(0.0, self.rules["daily_loss_limit_eur"] - loss - open_risk), 2)}
        except Exception:
            return None

    def weekly_report(self, force=False):
        """Elke maandag vanaf 8:00 (eigen tijd) een overzicht van de afgelopen 7 dagen: in het logboek (ook op je
        telefoon) en als bestand in de map 'rapporten'. Met force=True direct."""
        local = datetime.now().astimezone()
        week = local.strftime("%G-W%V")
        if not force and (local.weekday() != 0 or local.hour < 8 or self.meta_get("week_report") == week):
            return None
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        L = [f"Weekoverzicht t/m {local.strftime('%d-%m-%Y')}"]
        with self.db() as con:
            for label, sql in (("Gewone bot (echt geld of droog)",
                                "SELECT pnl, bron FROM trades WHERE side='sell' AND ts>=? AND COALESCE(schaduw,0)=0"),
                               ("Shorts", "SELECT pnl, bron FROM short_trades WHERE side='cover' AND ts>=? "
                                          "AND COALESCE(schaduw,0)=0"),
                               ("Schaduwtrades (niet echt)",
                                "SELECT pnl, bron FROM trades WHERE side='sell' AND ts>=? AND COALESCE(schaduw,0)=1")):
                rows = con.execute(sql, (since,)).fetchall()
                if rows:
                    tot = sum(r[0] or 0 for r in rows)
                    wins = sum(1 for r in rows if (r[0] or 0) > 0)
                    per = {}
                    for p_, b in rows:
                        per[b or "regels"] = per.get(b or "regels", 0) + (p_ or 0)
                    L.append(f"{label}: {len(rows)} trades, {wins} winst, totaal €{tot:+.2f} "
                             f"({', '.join(f'{k} €{v:+.2f}' for k, v in per.items())})")
                else:
                    L.append(f"{label}: geen gesloten trades")
            rows = con.execute("SELECT trader_name, pnl_eur FROM copy_trades WHERE closed>=?", (since,)).fetchall()
            if rows:
                per = {}
                for n_, p_ in rows:
                    per[n_] = per.get(n_, 0) + (p_ or 0)
                L.append(f"Kopiëren: {len(rows)} trades, totaal €{sum(per.values()):+.2f} — " +
                         ", ".join(f"@{k} €{v:+.2f}" for k, v in sorted(per.items(), key=lambda kv: -kv[1])))
            else:
                L.append("Kopiëren: geen gesloten kopieën")
            usd = con.execute("SELECT COALESCE(SUM(usd),0) FROM api_usage WHERE ts>=?", (since,)).fetchone()[0]
            L.append(f"API-kosten: ${usd:.2f}")
            b = con.execute("SELECT bot_eur, btc_hold_eur FROM benchmark ORDER BY id DESC LIMIT 1").fetchone()
            if b and b[0] is not None:
                L.append(f"Stand: bot €{b[0]:.2f}, BTC vasthouden €{(b[1] or 0):.2f}")
        for line in L:
            self.log(line, "info")
        try:
            folder = os.path.join(settings.data_dir(), "rapporten")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"week-{week}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# " + L[0] + "\n\n" + "\n".join("- " + x for x in L[1:]) + "\n")
        except Exception:
            path = None
        self.meta_set("week_report", week)
        return path

    def copy_snapshot(self):
        """Kopieer-modus voor de website: open kopieën, laatste gesloten, statistiek per trader."""
        try:
            with self.db() as con:
                opens = [dict(r) for r in con.execute(
                    "SELECT coin, side, leverage, cost_eur, entry_price, stop_price, trader_name, opened, echt "
                    "FROM copy_positions ORDER BY opened DESC")]
                closed = [dict(r) for r in con.execute(
                    "SELECT closed, coin, side, trader_name, pnl_eur, pnl_pct, trader_pct, fee_eur, reason, echt "
                    "FROM copy_trades ORDER BY id DESC LIMIT 30")]
                rows = con.execute("SELECT trader_name, pnl_eur, pnl_pct, trader_pct FROM copy_trades").fetchall()
                tpos = [dict(r) for r in con.execute(
                    "SELECT address, name, coin, side, size, entry, leverage, account, ts FROM copy_trader_positions "
                    "ORDER BY name, address, coin")]
        except Exception:
            return None
        kr = self.copier.kr if self.copier else self.kr
        for o in opens:
            try:
                import kraken
                px = kr.price(kraken.symbol_for_coin(o["coin"])) if kr else None
            except Exception:
                px = None
            if px:
                d = 1 if o["side"] == "long" else -1
                amount = o["cost_eur"] / o["entry_price"] if o["entry_price"] else 0
                o["price_now"] = px
                o["pnl_now"] = round(d * (px - o["entry_price"]) * amount, 2)
        per = {}
        for name, p, bp, tp in rows:
            x = per.setdefault(name or "?", {"trades": 0, "wins": 0, "pnl": 0.0, "bot_pct": [], "trader_pct": []})
            x["trades"] += 1
            x["wins"] += 1 if (p or 0) > 0 else 0
            x["pnl"] += p or 0
            if bp is not None and tp is not None:
                x["bot_pct"].append(bp)
                x["trader_pct"].append(tp)
        per_list = [{"trader": k, "trades": v["trades"], "winrate": round(v["wins"] / v["trades"], 3),
                     "pnl": round(v["pnl"], 2),
                     "bot_pct": round(sum(v["bot_pct"]) / len(v["bot_pct"]), 2) if v["bot_pct"] else None,
                     "trader_pct": round(sum(v["trader_pct"]) / len(v["trader_pct"]), 2) if v["trader_pct"] else None}
                    for k, v in per.items()]
        tot = sum(r[1] or 0 for r in rows)
        return {"enabled": bool(self.copier), "live": bool(self.copier and self.copier.live()),
                "realtime": bool(self.copier and self.copier.ws_ok),
                "traders": [t["name"] for t in (self.copier.traders if self.copier else [])],
                "open": opens, "closed": closed, "per_trader": per_list, "trader_positions": tpos,
                "totals": {"trades": len(rows), "wins": sum(1 for r in rows if (r[1] or 0) > 0), "pnl": round(tot, 2)}}

    def follower_snapshot(self):
        if not self.followers:
            return None
        try:
            traders = [{"address": t["address"], "kort": short_addr(t["address"]), "naam": t["name"],
                        "account": round(t["account_value"] or 0), "maand_pnl": round(t["month_pnl"] or 0),
                        "maand_roi": round((t["month_roi"] or 0) * 100, 1), "bron": t["source"]}
                       for t in self.followers.traders()]
            cons = {coin_of(m): self.followers.consensus(coin_of(m)) for m in self.markets}
            with self.db() as con:
                evs = [{"ts": r[0], "trader": short_addr(r[1]), "coin": r[2], "event": r[3], "side": r[4],
                        "usd": round(r[5] or 0)} for r in con.execute(
                    "SELECT ts,address,coin,event,side,notional FROM follower_events ORDER BY id DESC LIMIT 20")]
            return {"traders": traders, "consensus": cons, "events": evs}
        except Exception:
            return None

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
            if not p.get("schaduw"):
                unrealized += p["amount"] * self.price(m) - p["cost_eur"]
        with self.db() as con:
            pnl = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='sell' AND COALESCE(schaduw,0)=0").fetchone()[0]
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
