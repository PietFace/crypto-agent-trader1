"""Volger-agent: kijkt wat goed presterende traders op Hyperliquid doen.

Alle posities op Hyperliquid zijn openbaar. Dit bestand:
- haalt eens per dag het openbare leaderboard op en kiest daar traders uit
  (of gebruikt adressen die je zelf hebt ingevuld);
- haalt elke ronde hun open posities op (clearinghouseState);
- berekent per munt een consensus: netto long of short, gewogen naar positiegrootte;
- ziet wijzigingen: een trader opent, sluit of draait een positie om.

Geen AI, geen kosten: alleen openbare gegevens. Alleen kijken, nooit kopiëren.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
LEADERBOARD_MAX_AGE = 24 * 3600


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def coin_of(market):
    """'BTC-EUR' -> 'BTC'"""
    return market.split("-")[0].upper()


def short_addr(a):
    return a[:6] + "…" + a[-4:] if a and len(a) > 12 else (a or "?")


def init_db(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS follower_traders(
        address TEXT PRIMARY KEY, name TEXT, account_value REAL, month_pnl REAL, month_roi REAL,
        alltime_pnl REAL, source TEXT, picked_at TEXT);
    CREATE TABLE IF NOT EXISTS follower_positions(
        address TEXT, coin TEXT, side TEXT, size REAL, notional REAL, entry REAL, leverage REAL,
        upnl REAL, since TEXT, PRIMARY KEY(address, coin));
    CREATE TABLE IF NOT EXISTS follower_events(
        id INTEGER PRIMARY KEY, ts TEXT, address TEXT, coin TEXT, event TEXT, side TEXT, notional REAL);
    """)


# ---------------------------------------------------------------- leaderboard
def _perf(row, window):
    for w in row.get("windowPerformances") or []:
        if isinstance(w, list) and len(w) == 2 and w[0] == window:
            d = w[1] or {}
            return {k: float(d.get(k) or 0) for k in ("pnl", "roi", "vlm")}
    return {"pnl": 0.0, "roi": 0.0, "vlm": 0.0}


def fetch_leaderboard(cache_path, timeout=60):
    """Leaderboard ophalen, met een cache van een dag (het bestand is groot)."""
    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < LEADERBOARD_MAX_AGE:
        try:
            return json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            pass
    r = requests.get(LEADERBOARD_URL, timeout=timeout)
    r.raise_for_status()
    rows = r.json().get("leaderboardRows") or []
    slim = []
    for row in rows:
        try:
            acct = float(row.get("accountValue") or 0)
        except (TypeError, ValueError):
            continue
        if acct < 10_000:
            continue
        slim.append({"address": row.get("ethAddress"), "name": row.get("displayName") or "",
                     "account_value": acct, "month": _perf(row, "month"), "week": _perf(row, "week"),
                     "alltime": _perf(row, "allTime")})
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(slim, f)
    return slim


def pick_traders(rows, n=10, min_account=50_000):
    """Kies traders die al langer goed draaien, geen market makers of gokkers.

    - minstens min_account dollar op het account
    - winst deze maand én over de hele looptijd
    - niet meer dan 15% verlies deze week (geen instorting op dit moment)
    - handelsvolume per maand hooguit 100x het account (filtert market makers en high-frequency bots)
    - maand-ROI hooguit 300% (filtert gelukstreffers met extreme hefboom)
    Gesorteerd op maandrendement (ROI), zodat grootte niet de doorslag geeft.
    """
    ok = []
    for r in rows:
        m, w, a = r["month"], r["week"], r["alltime"]
        acct = r["account_value"]
        if not r.get("address") or acct < min_account:
            continue
        if m["pnl"] <= 0 or a["pnl"] <= 0 or w["roi"] < -0.15:
            continue
        if m["vlm"] > acct * 100 or m["roi"] > 3:
            continue
        ok.append(r)
    ok.sort(key=lambda r: r["month"]["roi"], reverse=True)
    return ok[:n]


# ---------------------------------------------------------------- posities
def fetch_positions(address, timeout=15):
    r = requests.post(INFO_URL, json={"type": "clearinghouseState", "user": address}, timeout=timeout)
    r.raise_for_status()
    out = {}
    for ap in r.json().get("assetPositions") or []:
        p = ap.get("position") or {}
        try:
            szi = float(p.get("szi") or 0)
        except (TypeError, ValueError):
            continue
        if szi == 0:
            continue
        lev = p.get("leverage") or {}
        out[str(p.get("coin", "")).upper()] = {
            "side": "long" if szi > 0 else "short",
            "size": abs(szi),
            "notional": abs(float(p.get("positionValue") or 0)),
            "entry": float(p.get("entryPx") or 0),
            "leverage": float(lev.get("value") or 0) if isinstance(lev, dict) else 0.0,
            "upnl": float(p.get("unrealizedPnl") or 0),
        }
    return out


class Followers:
    def __init__(self, db_path, data_dir, cfg, log=lambda *a: None):
        self.db_path = db_path
        self.cache = os.path.join(data_dir, "leaderboard.json")
        self.cfg = cfg
        self.log = log
        self.last_refresh = 0.0
        self.seen = set()
        with sqlite3.connect(db_path) as con:
            init_db(con)

    # ---- welke traders
    def traders(self):
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute("SELECT * FROM follower_traders ORDER BY month_roi DESC")]

    def choose(self, force=False):
        """Traders (opnieuw) kiezen: eigen adressen, aangevuld vanuit het leaderboard."""
        if not force and time.time() - self.last_refresh < LEADERBOARD_MAX_AGE and self.traders():
            return self.traders()
        n = int(self.cfg.get("count", 10))
        manual = [a.strip().lower() for a in self.cfg.get("manual", []) if a.strip().startswith("0x")]
        rows = []
        if self.cfg.get("auto", True):
            try:
                rows = fetch_leaderboard(self.cache)
            except Exception as e:
                self.log(f"Volgers: leaderboard niet op te halen ({e})", "bad")
        by_addr = {r["address"].lower(): r for r in rows if r.get("address")}
        chosen = []
        for a in manual:
            r = by_addr.get(a, {"address": a, "name": "", "account_value": 0,
                                "month": {"pnl": 0, "roi": 0}, "alltime": {"pnl": 0}})
            chosen.append((r, "eigen"))
        if self.cfg.get("auto", True):
            for r in pick_traders(rows, n=max(0, n - len(manual)) + len(manual)):
                if r["address"].lower() not in manual and len(chosen) < n:
                    chosen.append((r, "leaderboard"))
        if not chosen:
            return self.traders()
        with sqlite3.connect(self.db_path) as con:
            keep = [r["address"].lower() for r, _ in chosen]
            con.execute("DELETE FROM follower_traders WHERE lower(address) NOT IN (%s)" % ",".join("?" * len(keep)), keep)
            con.execute("DELETE FROM follower_positions WHERE lower(address) NOT IN (%s)" % ",".join("?" * len(keep)), keep)
            for r, src in chosen:
                con.execute("INSERT OR REPLACE INTO follower_traders VALUES(?,?,?,?,?,?,?,?)",
                            (r["address"].lower(), r.get("name") or "", r.get("account_value", 0),
                             r["month"].get("pnl", 0), r["month"].get("roi", 0), r["alltime"].get("pnl", 0),
                             src, now()))
        self.last_refresh = time.time()
        self.log(f"Volgers: {len(chosen)} traders gekozen "
                 f"({sum(1 for _, s in chosen if s == 'eigen')} eigen, "
                 f"{sum(1 for _, s in chosen if s == 'leaderboard')} van het leaderboard).", "info")
        return self.traders()

    # ---- posities bijwerken
    def update(self, coins):
        """Posities van alle gevolgde traders ophalen. Geeft lijst van nieuwe events in onze munten."""
        coins = {c.upper() for c in coins}
        traders = self.choose()
        events, errors = [], 0
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            for t in traders:
                addr = t["address"]
                try:
                    pos = fetch_positions(addr)
                except Exception:
                    errors += 1
                    continue
                old = {r["coin"]: dict(r) for r in con.execute(
                    "SELECT * FROM follower_positions WHERE address=?", (addr,))}
                first_look = addr not in self.seen
                for coin in set(old) | set(pos):
                    o, n = old.get(coin), pos.get(coin)
                    ev = None
                    if n and not o:
                        ev = ("open", n["side"], n["notional"])
                    elif o and not n:
                        ev = ("sluit", o["side"], o["notional"])
                    elif o and n and o["side"] != n["side"]:
                        ev = ("draait", n["side"], n["notional"])
                    if n:
                        since = o["since"] if o and o["side"] == n["side"] else now()
                        con.execute("INSERT OR REPLACE INTO follower_positions VALUES(?,?,?,?,?,?,?,?,?)",
                                    (addr, coin, n["side"], n["size"], n["notional"], n["entry"],
                                     n["leverage"], n["upnl"], since))
                    else:
                        con.execute("DELETE FROM follower_positions WHERE address=? AND coin=?", (addr, coin))
                    # De eerste keer dat we een trader bekijken is alles 'nieuw': dat telt niet als event.
                    if ev and coin in coins and addr in self.seen:
                        con.execute("INSERT INTO follower_events(ts,address,coin,event,side,notional) "
                                    "VALUES(?,?,?,?,?,?)", (now(), addr, coin, ev[0], ev[1], ev[2]))
                        events.append({"address": addr, "coin": coin, "event": ev[0], "side": ev[1],
                                       "notional": round(ev[2])})
                if first_look:
                    self.seen.add(addr)
            con.execute("DELETE FROM follower_events WHERE id < (SELECT MAX(id) - 500 FROM follower_events)")
        if errors and errors == len(traders):
            raise RuntimeError("Hyperliquid niet bereikbaar")
        return events

    # ---- consensus
    def consensus(self, coin, min_notional=1000):
        """Per munt: hoeveel gevolgde traders long/short staan en een score van -1 (allemaal short) tot 1."""
        coin = coin.upper()
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute("SELECT p.address, p.side, p.notional, p.leverage, p.since, p.entry, p.upnl, "
                               "t.name FROM follower_positions p JOIN follower_traders t ON t.address=p.address "
                               "WHERE p.coin=? AND p.notional >= ?", (coin, min_notional)).fetchall()
            n_traders = con.execute("SELECT COUNT(*) FROM follower_traders").fetchone()[0]
            recent = con.execute("SELECT ts, address, event, side, notional FROM follower_events "
                                 "WHERE coin=? ORDER BY id DESC LIMIT 5", (coin,)).fetchall()
        longs = [r for r in rows if r[1] == "long"]
        shorts = [r for r in rows if r[1] == "short"]
        # Elke trader telt even zwaar (anders bepaalt één walvis alles); grootte alleen als info.
        score = (len(longs) - len(shorts)) / max(1, len(longs) + len(shorts)) if rows else 0.0
        return {
            "coin": coin, "volgers": n_traders, "long": len(longs), "short": len(shorts),
            "score": round(score, 2),
            "long_usd": round(sum(r[2] for r in longs)), "short_usd": round(sum(r[2] for r in shorts)),
            "gem_hefboom": round(sum(r[3] for r in rows) / len(rows), 1) if rows else None,
            "recent": [{"ts": r[0], "trader": short_addr(r[1]), "event": r[2], "side": r[3],
                        "usd": round(r[4] or 0)} for r in recent],
        }


def follower_setup(cons, ind, charts, cfg):
    """Volger-setup: genoeg gevolgde traders staan dezelfde kant op en de grafiek spreekt het niet hard tegen."""
    def f(x, d=0.0):
        try:
            return float(x)
        except (TypeError, ValueError):
            return d
    min_tr = int(cfg.get("min_traders", 3))
    min_score = float(cfg.get("min_consensus", 0.6))
    cs = f(charts.get("score")) if charts and "fout" not in charts else 0.0
    rsi = f(ind.get("rsi14"), 50)
    n = cons["long"] + cons["short"]
    long_checks = {
        f"{cons['long']} volgers long (min {min_tr})": cons["long"] >= min_tr,
        f"consensus {cons['score']:+.2f} >= +{min_score:.2f}": cons["score"] >= min_score,
        f"charts {cs:+.2f} niet onder -0.30": cs > -0.3,
        f"RSI {rsi:.0f} < 75": rsi < 75,
    }
    short_checks = {
        f"{cons['short']} volgers short (min {min_tr})": cons["short"] >= min_tr,
        f"consensus {cons['score']:+.2f} <= -{min_score:.2f}": cons["score"] <= -min_score,
        f"charts {cs:+.2f} niet boven +0.30": cs < 0.3,
        f"RSI {rsi:.0f} > 25": rsi > 25,
    }
    return {"long": n > 0 and all(long_checks.values()), "short": n > 0 and all(short_checks.values()),
            "long_checks": long_checks, "short_checks": short_checks}
