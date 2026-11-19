"""Kopieer-modus: gekozen Hyperliquid-traders 1-op-1 volgen op Kraken.

- Elke paar seconden (standaard 5) de posities van de gekozen traders ophalen.
- Opent een trader een NIEUWE positie, dan opent de bot dezelfde kant op Kraken (droog of live).
- Sluit de trader (of draait hij om), dan sluit de bot ook.
- Altijd een noodstop: op Kraken zelf als je live gaat, en in de bot bij droog draaien.
- Een vaste inzet per trade, een maximale hefboom en een daglimiet.
- Per trade wordt bewaard wat de trader deed en wat de bot deed, zodat je ziet hoeveel de bot achterloopt.

Daarnaast: de vingerafdruk-zoeker, die een Invo-trader op Hyperliquid terugvindt aan de hand van een
paar van zijn trades (munt + prijs) in de openbare builder-bestanden van Invo.
"""
import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import kraken

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
_http = requests.Session()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS copy_positions(
        coin TEXT PRIMARY KEY, symbol TEXT, side TEXT, amount REAL, entry_price REAL, cost_eur REAL,
        leverage REAL, opened TEXT, trader TEXT, trader_name TEXT, trader_entry REAL, stop_price REAL,
        ext_stop TEXT, echt INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS copy_trades(
        id INTEGER PRIMARY KEY, opened TEXT, closed TEXT, coin TEXT, side TEXT, trader TEXT, trader_name TEXT,
        cost_eur REAL, leverage REAL, entry REAL, exit REAL, trader_entry REAL, trader_exit REAL,
        pnl_eur REAL, pnl_pct REAL, trader_pct REAL, fee_eur REAL, reason TEXT, echt INTEGER DEFAULT 0);
    """)
    cols = [r[1] for r in con.execute("PRAGMA table_info(copy_positions)")]
    for c in ("price_now", "pnl_now"):
        if c not in cols:
            con.execute(f"ALTER TABLE copy_positions ADD COLUMN {c} REAL")


# ---------------------------------------------------------------- Hyperliquid
def trader_state(address, timeout=10):
    """{'account': $, 'positions': {coin: {side, size, entry, leverage}}}"""
    r = _http.post(INFO_URL, json={"type": "clearinghouseState", "user": address}, timeout=timeout)
    r.raise_for_status()
    js = r.json()
    pos = {}
    for ap in js.get("assetPositions") or []:
        p = ap.get("position") or {}
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if szi == 0:
            continue
        lev = p.get("leverage") or {}
        pos[str(p.get("coin", "")).upper()] = {
            "side": "long" if szi > 0 else "short", "size": abs(szi),
            "entry": float(p.get("entryPx") or 0),
            "leverage": float(lev.get("value") or 1) if isinstance(lev, dict) else 1.0}
    acct = float((js.get("marginSummary") or {}).get("accountValue") or 0)
    return {"account": acct, "positions": pos}


def trader_style(address, short_hours=4.0, timeout=15):
    """Lang of kort, bepaald uit zijn eigen recente trades op Hyperliquid (tot 2000 fills).
    Geeft (stijl, mediane houdtijd in uren, aantal afgesloten posities). Te weinig gegevens: 'lang'."""
    r = _http.post(INFO_URL, json={"type": "userFills", "user": address}, timeout=timeout)
    r.raise_for_status()
    fills = sorted(r.json() or [], key=lambda f: f.get("time", 0))
    opened, holds = {}, []
    for f in fills:
        try:
            coin = f["coin"]
            start = float(f.get("startPosition") or 0)
            sz = float(f.get("sz") or 0)
            t = int(f.get("time") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        after = start + (sz if f.get("side") == "B" else -sz)
        eps = max(1e-9, abs(sz) * 1e-6)
        if abs(start) <= eps:
            opened[coin] = t                       # positie geopend vanuit niets
        elif abs(after) <= eps or (start > 0) != (after > 0):
            if coin in opened:
                holds.append((t - opened.pop(coin)) / 3600000)
            if abs(after) > eps:
                opened[coin] = t                   # omgedraaid: nieuwe positie de andere kant op
    if len(holds) < 3:
        return "lang", None, len(holds)
    holds.sort()
    med = holds[len(holds) // 2]
    return ("kort" if med < short_hours else "lang"), med, len(holds)


def hold_text(hours):
    if hours is None:
        return "onbekend"
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} uur"
    return f"{hours / 24:.1f} dagen"


# ---------------------------------------------------------------- kopieer-motor
class CopyEngine:
    def __init__(self, engine, cfg):
        self.e = engine            # bot.Engine: log, db, push_trade
        self.cfg = cfg
        self.kr = engine.kr or kraken.KrakenPublic()   # koersen: altijd Kraken
        self.kx = engine.kx                            # orders: alleen als de bot live op Kraken staat
        self.known = {}            # trader -> {coin: side} van de vorige ronde
        self.first_round = set()   # traders waarvan we de beginstand al kennen
        self._stop = threading.Event()
        self._wake = threading.Event()   # gezet door de live-verbinding zodra een gevolgde trader handelt
        self._thread = None
        self._ws = None
        self.ws_ok = False
        with self.e.db() as con:
            init_db(con)

    # --- instellingen
    @property
    def traders(self):
        out = []
        for t in self.cfg.get("traders", []):
            a = str(t.get("address", "")).strip().lower()
            if a.startswith("0x") and len(a) == 42:
                out.append({"address": a, "name": t.get("name") or a[:8], "style": t.get("style", "lang")})
        return out

    def live(self):
        """Echt geld alleen als de bot live op Kraken staat én kopiëren apart op live is gezet."""
        return bool(self.kx) and bool(self.cfg.get("live", False))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if self.cfg.get("realtime", True):
            threading.Thread(target=self._ws_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # --- live-verbinding: Hyperliquid meldt zelf elke trade van (max 10) gevolgde traders
    def _ws_loop(self):
        try:
            import websocket
        except Exception:
            self.e.log("Kopieer-modus: live-verbinding niet beschikbaar, alleen elke paar seconden kijken.", "muted")
            return
        users = [t["address"] for t in self.traders][:10]
        if not users:
            return
        backoff = 2
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(WS_URL, timeout=30)
                self._ws = ws
                for u in users:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "userFills", "user": u}}))
                if not self.ws_ok:
                    self.e.log(f"Kopieer-modus: live-verbinding open voor {len(users)} trader(s); trades komen "
                               "binnen ~1 seconde binnen.", "info")
                self.ws_ok, backoff = True, 2
                last_ping = time.time()
                while not self._stop.is_set():
                    if time.time() - last_ping > 45:
                        ws.send(json.dumps({"method": "ping"}))
                        last_ping = time.time()
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    try:
                        js = json.loads(msg)
                    except ValueError:
                        continue
                    data = js.get("data") or {}
                    if js.get("channel") == "userFills" and isinstance(data, dict) and not data.get("isSnapshot") \
                            and data.get("fills"):
                        self._wake.set()
            except Exception:
                pass
            finally:
                self.ws_ok = False
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(60, backoff * 2)

    def _loop(self):
        every = max(2, int(self.cfg.get("interval_s", 5)))
        names = ", ".join(t["name"] for t in self.traders) or "(nog geen traders)"
        self.e.log(f"Kopieer-modus {'LIVE' if self.live() else 'droog'}: elke {every} s kijken naar {names}. "
                   f"Budget €{self.budget():.0f}, max verlies €{self.max_loss():.2f} per kopie, "
                   f"hefboom max {self.cfg.get('max_leverage', 10)}x. Alleen nieuwe posities worden gekopieerd.",
                   "info")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as ex:
                self.e.log(f"Kopieer-modus: fout ({ex})", "bad")
            # Wachten tot de volgende ronde, of eerder als de live-verbinding een trade meldt.
            self._wake.wait(every)
            if self._wake.is_set():
                self._wake.clear()
                self._stop.wait(0.7)   # trade even laten verwerken bij Hyperliquid

    # --- één ronde
    def tick(self):
        if self.live():
            self.sync_kraken()
        mine = self.positions()
        for t in self.traders:
            try:
                st = trader_state(t["address"])
            except Exception as ex:
                self.e.log(f"Kopieer-modus: {t['name']} niet op te halen ({ex})", "muted")
                continue
            now_sides = {c: p["side"] for c, p in st["positions"].items()}
            before = self.known.get(t["address"])
            self.known[t["address"]] = now_sides
            if before is None:
                continue  # eerste keer: alleen de beginstand onthouden, niet instappen in oude posities
            # gesloten of omgedraaid -> wij sluiten
            for coin, side in before.items():
                if now_sides.get(coin) != side:
                    pos = mine.get(coin)
                    if pos and pos["trader"] == t["address"] and pos["side"] == side:
                        self.close(coin, f"{t['name']} sloot zijn {side}")
                        mine.pop(coin, None)
            # nieuw geopend -> wij openen
            for coin, side in now_sides.items():
                if before.get(coin) != side and coin not in mine:
                    p = st["positions"][coin]
                    if self.open(coin, side, p, t):
                        mine = self.positions()
        self.check_stops()

    # --- helpers
    def positions(self):
        with self.e.db() as con:
            return {r["coin"]: dict(r) for r in con.execute("SELECT * FROM copy_positions")}

    def realized_today(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%"
        with self.e.db() as con:
            return con.execute("SELECT COALESCE(SUM(pnl_eur),0) FROM copy_trades WHERE closed LIKE ?",
                               (day,)).fetchone()[0]

    def max_loss(self):
        """Eigen max verlies per kopie; anders dat van de gewone bot."""
        v = self.cfg.get("max_loss_eur")
        return float(v) if v else float(self.e.rules["max_loss_per_trade_eur"])

    def budget(self):
        import settings
        return settings.budgets(self.e.cfg)[1]

    def budget_room(self):
        """Vrij in het kopieer-budget: budget + eventueel verlies tot nu toe − inzet in open kopieën."""
        with self.e.db() as con:
            realized = con.execute("SELECT COALESCE(SUM(pnl_eur),0) FROM copy_trades").fetchone()[0]
            used = con.execute("SELECT COALESCE(SUM(cost_eur / MAX(leverage, 1)),0) FROM copy_positions").fetchone()[0]
        return self.budget() + min(0.0, realized) - used

    def stop_pct(self, style):
        return float(self.cfg.get("stop_pct_lang" if style == "lang" else "stop_pct_kort",
                                  10 if style == "lang" else 3))

    def open(self, coin, side, their, t):
        kr = self.kr
        sym = kraken.symbol_for_coin(coin)
        try:
            if not kr.supported(sym):
                self.e.log(f"Kopie {t['name']}: {coin} {side} — niet te verhandelen op Kraken, overgeslagen.", "muted")
                return False
        except Exception as ex:
            self.e.log(f"Kopie {t['name']}: Kraken niet bereikbaar ({ex})", "bad")
            return False
        if len(self.positions()) >= int(self.cfg.get("max_positions", 5)):
            self.e.log(f"Kopie {t['name']}: {coin} overgeslagen, al {self.cfg.get('max_positions', 5)} kopieën open.",
                       "muted")
            return False
        rules = self.e.rules
        limit = float(rules["daily_loss_limit_eur"])
        if self.e.main_holds_real(sym):
            self.e.log(f"Kopie {t['name']}: {coin} overgeslagen, de hoofdbot heeft daar al een echte positie.", "muted")
            return False
        # Zelfde hefboom als de trader, maar nooit boven jouw maximum en nooit boven 10x (EU-grens bij Kraken).
        lev = max(1.0, min(float(self.cfg.get("max_leverage", 10)), float(their.get("leverage") or 1), 10.0))
        # Noodstop nooit verder weg dan 80% van de inzet (bij 10x dus hooguit 8% koersbeweging): ruim vóór liquidatie.
        stop_pct = min(self.stop_pct(t["style"]), 80.0 / lev)
        # Grootte zoals bij de gewone bot: terugrekenen vanuit het max verlies per trade (incl. kosten),
        # begrensd door 'max per aankoop' x hefboom en door wat er nog vrij is in het kopieer-budget.
        fee2 = 2 * kraken.TAKER_FEE_PCT / 100
        by_risk = self.max_loss() / (stop_pct / 100 + fee2)
        room = self.budget_room()
        notional = min(by_risk, float(rules["max_trade_eur"]) * lev, max(0.0, room) * lev)
        why = ("max verlies per trade" if notional == by_risk else
               "max per aankoop" if notional == float(rules["max_trade_eur"]) * lev else "kopieer-budget")
        if notional < 5:
            self.e.log(f"Kopie {t['name']}: {coin} overgeslagen, kopieer-budget op (nog €{room:.2f} vrij).", "muted")
            return False
        with self.e.db() as con:
            import risk_gate
            loss, open_risk = risk_gate.day_risk_all(con, rules)
        new_risk = notional * (stop_pct / 100 + fee2)
        if loss + open_risk + new_risk > limit:
            self.e.log(f"Kopie {t['name']}: {coin} overgeslagen, daglimiet: verlies vandaag €{loss:.2f} + open "
                       f"risico €{open_risk:.2f} + deze €{new_risk:.2f} > €{limit:.2f}", "muted")
            return False
        px = kr.price(sym)
        ext = None
        if self.live():
            try:
                size, _ = self.kx.size_for(sym, notional)
                if size <= 0:
                    self.e.log(f"Kopie {t['name']}: €{notional:.0f} is te weinig voor de kleinste order van {coin}.",
                               "muted")
                    return False
                amount, px, _ = self.kx.market_order(sym, "buy" if side == "long" else "sell", size)
                stop = px * (1 - stop_pct / 100) if side == "long" else px * (1 + stop_pct / 100)
                try:
                    ext = self.kx.stop_order(sym, "sell" if side == "long" else "buy", amount, stop)
                except Exception as ex:
                    self.e.log(f"Kopie {t['name']}: noodstop bij Kraken mislukt ({ex}), positie direct gesloten.",
                               "bad")
                    self.kx.market_order(sym, "sell" if side == "long" else "buy", amount, reduce_only=True)
                    return False
                notional = amount * px
            except Exception as ex:
                self.e.log(f"Kopie {t['name']}: openen bij Kraken mislukt ({ex})", "bad")
                return False
        else:
            spread = (kr.spread_pct(sym) or 0) / 100
            px = px * (1 + spread / 2) if side == "long" else px * (1 - spread / 2)
            amount = notional / px
        stop = px * (1 - stop_pct / 100) if side == "long" else px * (1 + stop_pct / 100)
        with self.e.db() as con:
            con.execute("INSERT OR REPLACE INTO copy_positions(coin, symbol, side, amount, entry_price, cost_eur, "
                        "leverage, opened, trader, trader_name, trader_entry, stop_price, ext_stop, echt) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (coin, sym, side, amount, px, notional, lev, now(), t["address"], t["name"],
                         float(their.get("entry") or 0), stop, ext, int(self.live())))
        risk = notional * (stop_pct / 100 + fee2)
        self.e.log(f"{'LIVE' if self.live() else 'DROOG'} KOPIE OPEN {coin} {side} {lev:.0f}x van {t['name']}: "
                   f"positie €{notional:.2f} (inzet €{notional / lev:.2f}, grens: {why}) @ {px:.6g}, noodstop "
                   f"{stop:.6g} (max verlies ~€{risk:.2f})", "good")
        self.e.push_trade(f"{coin}-PERP", "buy" if side == "long" else "short", notional / lev, px, 0,
                          f"[kopie {t['name']}] {coin} {side} {lev:.0f}x", sim=not self.live(), kind="kopie",
                          trader=t["name"])
        return True

    def close(self, coin, reason, exit_px=None):
        pos = self.positions().get(coin)
        if not pos:
            return
        kr = self.kr
        sym = pos["symbol"]
        px = exit_px
        if pos["echt"] and self.kx and exit_px is None:
            self.kx.cancel(pos.get("ext_stop"))
            try:
                _, px, _ = self.kx.market_order(sym, "sell" if pos["side"] == "long" else "buy", pos["amount"],
                                                  reduce_only=True)
            except Exception as ex:
                if "wouldNotReducePosition" in str(ex):
                    px, reason = pos["stop_price"], "noodstop (al uitgevoerd bij Kraken)"
                else:
                    self.e.log(f"Kopie {coin}: sluiten bij Kraken mislukt ({ex}). Noodstop is mogelijk weg!", "bad")
                    return
        if px is None:
            px = kr.price(sym)
            spread = (kr.spread_pct(sym) or 0) / 100
            px = px * (1 - spread / 2) if pos["side"] == "long" else px * (1 + spread / 2)
        direction = 1 if pos["side"] == "long" else -1
        gross = direction * (px - pos["entry_price"]) * pos["amount"]
        fee = (pos["cost_eur"] + pos["amount"] * px) * kraken.TAKER_FEE_PCT / 100
        pnl = gross - fee
        margin = pos["cost_eur"] / max(1.0, pos["leverage"])
        pct = pnl / margin * 100 if margin else 0
        # Wat de trader zelf haalde op dezelfde hefboom, tegen de koers van nu (zijn exacte uitstap kennen we niet).
        t_entry = pos.get("trader_entry") or 0
        t_pct = None
        if t_entry:
            try:
                usd_now = px * kr.eurusd()
                t_pct = direction * (usd_now - t_entry) / t_entry * pos["leverage"] * 100
            except Exception:
                t_pct = None
        with self.e.db() as con:
            con.execute("DELETE FROM copy_positions WHERE coin=?", (coin,))
            con.execute("INSERT INTO copy_trades(opened,closed,coin,side,trader,trader_name,cost_eur,leverage,entry,"
                        "exit,trader_entry,trader_exit,pnl_eur,pnl_pct,trader_pct,fee_eur,reason,echt) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pos["opened"], now(), coin, pos["side"], pos["trader"], pos["trader_name"],
                         pos["cost_eur"], pos["leverage"], pos["entry_price"], px, t_entry, None, pnl, pct, t_pct,
                         fee, reason, pos["echt"]))
        extra = f"; {pos['trader_name']} zelf ~{t_pct:+.1f}%" if t_pct is not None else ""
        self.e.log(f"{'LIVE' if pos['echt'] else 'DROOG'} KOPIE DICHT {coin} {pos['side']}: €{pnl:+.2f} "
                   f"({pct:+.1f}% op inzet{extra}) — {reason}", "good" if pnl >= 0 else "bad")
        self.e.push_trade(f"{coin}-PERP", "sell" if pos["side"] == "long" else "cover", pos["amount"] * px / max(1, pos["leverage"]),
                          px, pnl, f"[kopie {pos['trader_name']}] {reason}", sim=not pos["echt"], kind="kopie",
                          trader=pos["trader_name"])

    def check_stops(self):
        """Live waarde bijwerken, en de noodstop bewaken bij droog draaien (live zit hij bij Kraken zelf)."""
        for coin, pos in self.positions().items():
            try:
                px = self.kr.price(pos["symbol"])
            except Exception:
                continue
            d = 1 if pos["side"] == "long" else -1
            pnl = d * (px - pos["entry_price"]) * pos["amount"] - pos["cost_eur"] * kraken.TAKER_FEE_PCT / 100 * 2
            with self.e.db() as con:
                con.execute("UPDATE copy_positions SET price_now=?, pnl_now=? WHERE coin=?", (px, pnl, coin))
            if pos["echt"]:
                continue
            hit = px <= pos["stop_price"] if pos["side"] == "long" else px >= pos["stop_price"]
            if hit:
                self.close(coin, "noodstop geraakt", exit_px=pos["stop_price"])

    def sync_kraken(self):
        try:
            open_at = self.kx.open_positions()
        except Exception:
            return
        for coin, pos in self.positions().items():
            if pos["echt"] and pos["symbol"] not in open_at:
                self.close(coin, "noodstop (bij Kraken)", exit_px=pos["stop_price"])

    def open_symbols(self, real_only=True):
        return {p["symbol"] for p in self.positions().values() if p["echt"] or not real_only}


# ---------------------------------------------------------------- vingerafdruk-zoeker
INVO_BUILDER = "0x557edb253b1d7ed5f15b248a5a3fd919fa5d3c81"
FILLS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{b}/{d}.csv.lz4"

# Trades zoals ze op Invo-profielen stonden (18 nov 2026): munt + uitstapprijs in USD.
# Accountwaarde ($) als die op het profiel zichtbaar was; helpt kopieerders uit te sluiten.
FINGERPRINTS = {
    "blackstallion": {"style": "lang", "account": 684499, "trades": [
        ("BTC", 63375.00), ("ETH", 1797.60), ("OP", 0.1176198), ("UNI", 6.8216759), ("XRP", 1.3573416),
        ("ENS", 5.5822016)]},
    # Toegevoegd 19 nov (gelezen van de Invo-profielen; Closed-tab, uitstap- en instapprijzen)
    "missed": {"style": "lang", "account": 20748076, "trades": [
        ("VVV", 20.25), ("MEGA", 0.0492592), ("AVAX", 7.1935013), ("XMR", 490.66), ("AAVE", 127.70)]},
    "ka5h": {"style": "kort", "account": None, "trades": [
        ("JUP", 0.272275), ("INIT", 0.10983), ("APE", 0.08106), ("ME", 0.03853), ("ANIME", 0.002148),
        ("SOPH", 0.001043)]},
    "amber_one": {"style": "lang", "account": 115186, "trades": [
        ("NEAR", 3.1442057), ("VVV", 19.75), ("HYPE", 86.57), ("SUI", 1.0499074), ("VVV", 22.72)]},
    "bravo": {"style": "lang", "account": 48889, "trades": [
        ("ETH", 1813.00), ("LIT", 2.7743645), ("LINK", 9.13), ("MEGA", 0.048616), ("VVV", 17.75)]},
    "mateus": {"style": "lang", "account": None, "trades": [
        ("TAO", 197.47), ("XRP", 0.969455), ("PAXG", 3787.91), ("XPL", 0.0421819), ("XPL", 0.047643)]},
    "driftmist": {"style": "lang", "account": 425432, "trades": [
        ("NEAR", 3.285), ("XMR", 498.63), ("JUP", 0.25617), ("JUP", 0.25929), ("ZRO", 1.4564)]},
    "retrace": {"style": "kort", "account": None, "trades": [
        ("BTC", 63691.50), ("BTC", 64190.00), ("BTC", 64282.00), ("BTC", 63866.00), ("BTC", 63920.00)]},
    "johndrockefeller": {"style": "kort", "account": 5353645, "trades": [
        ("BTC", 63565.00), ("BTC", 63550.00), ("BTC", 62906.00), ("BTC", 62100.00), ("BTC", 63539.47),
        ("BTC", 63079.81)]},
    "apx": {"style": "kort", "account": None, "trades": [
        ("BTC", 63071.00), ("BTC", 64421.50), ("ETH", 1823.30), ("BTC", 64886.00), ("SOL", 74.98)]},
    "suv": {"style": "kort", "account": None, "trades": [
        ("ETH", 1766.21), ("BTC", 63971.50), ("XRP", 0.99795), ("ETH", 1808.45), ("BTC", 64604.00)]},
    "ferall": {"style": "lang", "account": 107067, "trades": [
        ("XTEN", 0.1934371), ("LIT", 2.8310369), ("LIT", 2.5582063), ("LIT", 2.4058402), ("ZEC", 891.69)]},
}


def _day_fills(cache_dir, day, timeout=60):
    """Alle Invo-fills van één dag als lijst (user, coin, px, sz, time). Op schijf bewaard: een dag verandert niet."""
    import lz4.frame
    path = os.path.join(cache_dir, f"invo_fills_{day}.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    r = requests.get(FILLS_URL.format(b=INVO_BUILDER, d=day), timeout=timeout)
    if r.status_code != 200:
        return []
    text = lz4.frame.decompress(r.content).decode("utf-8", "replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): v for k, v in row.items() if k}
        try:
            rows.append([(row.get("user") or "").lower(), str(row.get("coin") or "").upper(),
                         float(row.get("px") or 0), float(row.get("sz") or 0), row.get("time") or ""])
        except ValueError:
            continue
    if day != datetime.now(timezone.utc).strftime("%Y%m%d"):
        json.dump(rows, open(path, "w", encoding="utf-8"))
    return rows


def _ts(v):
    """Tijd uit het builder-bestand naar seconden (ms-getal of ISO-tekst). None als onbekend."""
    try:
        x = float(v)
        return x / 1000 if x > 1e11 else x
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def find_trader(trades, cache_dir, days=60, tol_pct=0.05, account=None, progress=lambda t: None):
    """Zoek het Hyperliquid-adres dat dezelfde trades deed als op het Invo-profiel. trades: [(munt, prijs), ...].

    - Invo toont vaak een GEMIDDELDE uitstapprijs als een order in stukjes is gevuld. Daarom vergelijken we niet
      alleen losse fills, maar ook het gemiddelde van fills die binnen 2 minuten na elkaar kwamen.
    - Kopieerders via Invo doen dezelfde trades een paar seconden later tegen een net andere prijs; het origineel
      zit er het dichtst op. Bots die duizenden trades doen passen toevallig vaak op ronde BTC-prijzen; die zakken.
    Geeft kandidaten, beste eerst: {address, matches, of, diff, fills, account, bot}."""
    want = [(str(c).upper(), float(p)) for c, p in trades]
    coins = {c for c, _ in want}
    best = {}      # (user, k) -> kleinste verschil in %
    nfills = {}    # user -> aantal fills in de periode (hoge aantallen = bot)
    today = datetime.now(timezone.utc).date()

    def check(user, coin, px):
        for k, (c, p) in enumerate(want):
            if coin == c and p:
                diff = abs(px - p) / p * 100
                if diff <= tol_pct and diff < best.get((user, k), 99):
                    best[(user, k)] = diff

    for i in range(days):
        d = (today - timedelta(days=i)).strftime("%Y%m%d")
        progress(f"Invo-trades van {d[6:]}-{d[4:6]} doorzoeken…")
        try:
            rows = _day_fills(cache_dir, d)
        except Exception:
            continue
        groups = {}
        for user, coin, px, sz, ts in rows:
            nfills[user] = nfills.get(user, 0) + 1
            if coin not in coins:
                continue
            check(user, coin, px)
            groups.setdefault((user, coin), []).append((_ts(ts), px, sz))
        # gemiddelde prijs van fills die bij elkaar horen (binnen 2 minuten)
        for (user, coin), fl in groups.items():
            if len(fl) < 2:
                continue
            fl.sort(key=lambda x: x[0] or 0)
            run = [fl[0]]
            for f in fl[1:] + [None]:
                if f is not None and run[-1][0] is not None and f[0] is not None and f[0] - run[-1][0] <= 120:
                    run.append(f)
                    continue
                if len(run) > 1:
                    q = sum(x[2] for x in run)
                    if q > 0:
                        check(user, coin, sum(x[1] * x[2] for x in run) / q)
                if f is not None:
                    run = [f]
    per_user = {}
    for (u, k), diff in best.items():
        per_user.setdefault(u, {})[k] = diff
    need = min(len(want), 2)
    cands = []
    for u, ks in per_user.items():
        if len(ks) < need:
            continue
        bot = nfills.get(u, 0) > 300 * days      # meer dan ~300 trades per dag
        cands.append({"address": u, "matches": len(ks), "of": len(want), "diff": sum(ks.values()) / len(ks),
                      "fills": nfills.get(u, 0), "bot": bot, "account": None})
    cands.sort(key=lambda c: (c["bot"], -c["matches"], c["diff"]))
    out = cands[:8]
    for c in out:
        try:
            c["account"] = trader_state(c["address"])["account"]
        except Exception:
            pass
    if account:
        # Bij gelijke treffers: de accountwaarde die het dichtst bij Invo ligt wint.
        out.sort(key=lambda c: (c["bot"], -c["matches"], abs((c["account"] or 0) - account) / account + c["diff"]))
        if confidence(out) != "zeker":
            try:
                extra = find_by_account(trades, account, cache_dir, tol_pct=tol_pct, progress=progress)
            except Exception:
                extra = []
            seen = {c["address"] for c in out}
            out = [c for c in extra if c["address"] not in seen] + out
            out.sort(key=lambda c: (c["bot"], -c["matches"],
                                    abs((c["account"] or 0) - account) / account + c["diff"]))
    return out


def find_by_account(trades, account, cache_dir, tol_acct=0.05, tol_pct=0.05, max_check=40,
                    progress=lambda t: None):
    """Voor 'Live Portfolio's op Invo: die handelen vaak rechtstreeks op Hyperliquid (niet via Invo), dus ze staan
    niet in Invo's builder-bestanden. Wel zien we hun accountwaarde. Zoek in het leaderboard naar accounts met
    (bijna) dezelfde waarde en controleer hun eigen trades op de vingerafdruk."""
    import followers
    rows = followers.fetch_leaderboard(os.path.join(cache_dir, "leaderboard.json"))
    near = [r for r in rows if r.get("address") and r.get("account_value")
            and abs(r["account_value"] - account) / account <= tol_acct]
    near.sort(key=lambda r: abs(r["account_value"] - account))
    want = [(str(c).upper(), float(p)) for c, p in trades]
    out = []
    for i, r in enumerate(near[:max_check]):
        progress(f"Account {i + 1}/{min(len(near), max_check)} met ongeveer ${account:,.0f} controleren…")
        try:
            fl = _http.post(INFO_URL, json={"type": "userFills", "user": r["address"]}, timeout=15).json() or []
        except Exception:
            continue
        ks = {}
        for f in fl:
            try:
                coin, px = str(f.get("coin", "")).upper(), float(f.get("px") or 0)
            except (TypeError, ValueError):
                continue
            for k, (c, p) in enumerate(want):
                if coin == c and p and abs(px - p) / p * 100 <= tol_pct:
                    ks[k] = min(ks.get(k, 99), abs(px - p) / p * 100)
        if ks:
            out.append({"address": r["address"].lower(), "matches": len(ks), "of": len(want),
                        "diff": sum(ks.values()) / len(ks), "fills": len(fl), "bot": False,
                        "account": r["account_value"]})
        time.sleep(0.2)
    out.sort(key=lambda c: (-c["matches"], c["diff"]))
    return out


def confidence(res):
    """'zeker', 'waarschijnlijk' of 'twijfel' voor de beste kandidaat."""
    if not res:
        return None
    b = res[0]
    second = res[1] if len(res) > 1 else None
    clear_lead = not second or second["matches"] < b["matches"] or second["diff"] > b["diff"] * 3
    if b["bot"]:
        return "twijfel"
    if b["matches"] >= 3 and clear_lead:
        return "zeker"
    if b["matches"] >= 2 and clear_lead:
        return "waarschijnlijk"
    return "twijfel"
