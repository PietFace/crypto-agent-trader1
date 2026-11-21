"""Tape: live meeschrijven wie er op Hyperliquid handelt, om verborgen Invo-traders te vinden.

Hyperliquid stuurt bij elke trade via de websocket ook de twee adressen mee (koper en verkoper). Wij bewaren
die een paar dagen. Daarmee kunnen we twee dingen:

1. Opzoeken wie er rond een bepaald moment in een munt handelde (bijv. toen apx op Invo een trade opende).
2. Automatisch 'leiders' vinden: Invo-kopieerders (Mimic Trade) openen een paar seconden NA het origineel.
   Uit Invo's builder-bestanden weten we wanneer een groep Invo-gebruikers tegelijk dezelfde kant op ging.
   Wie in de seconden daarvóór dezelfde kant op handelde, en dat steeds weer, is vrijwel zeker de trader die ze
   kopiëren.

Alleen lezen en opslaan; dit doet niets met orders.
"""
import csv
import io
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
INVO_BUILDER = "0x557edb253b1d7ed5f15b248a5a3fd919fa5d3c81"
FILLS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills/{b}/{d}.csv.lz4"


def tape_db(data_dir):
    return os.path.join(data_dir, "tape.db")


def _con(path):
    con = sqlite3.connect(path, timeout=30)
    con.execute("""CREATE TABLE IF NOT EXISTS tape (t REAL, coin TEXT, side TEXT, px REAL, sz REAL,
                   buyer TEXT, seller TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS tape_ct ON tape(coin, t)")
    con.execute("CREATE INDEX IF NOT EXISTS tape_b ON tape(buyer)")
    con.execute("CREATE INDEX IF NOT EXISTS tape_s ON tape(seller)")
    return con


def top_coins(n=60):
    """De n munten met het meeste volume (daar zitten de Invo-traders vrijwel altijd in)."""
    r = requests.post(INFO_URL, json={"type": "metaAndAssetCtxs"}, timeout=20)
    r.raise_for_status()
    meta, ctxs = r.json()
    rows = []
    for a, c in zip(meta.get("universe", []), ctxs):
        try:
            rows.append((float(c.get("dayNtlVlm") or 0), a["name"]))
        except (TypeError, ValueError, KeyError):
            pass
    rows.sort(reverse=True)
    return [name for _, name in rows[:n]]


class TapeRecorder:
    """Websocket-thread die trades (met adressen) in tape.db schrijft. Houdt keep_hours uur bij."""

    def __init__(self, data_dir, coins=None, n_coins=60, keep_hours=72, log=lambda t: None):
        self.path = tape_db(data_dir)
        self.coins, self.n_coins, self.keep_hours, self.log = coins, n_coins, keep_hours, log
        self.stop = False
        self.count = 0
        self.buf = []
        self.lock = threading.Lock()
        threading.Thread(target=self._ws_loop, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()

    def _ws_loop(self):
        import websocket
        backoff = 5
        while not self.stop:
            try:
                coins = self.coins or top_coins(self.n_coins)
                ws = websocket.create_connection(WS_URL, timeout=30)
                for c in coins:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
                self.log(f"Tape: schrijft trades mee van {len(coins)} munten (voor het vinden van Invo-traders).")
                backoff = 5
                last_ping = time.time()
                while not self.stop:
                    if time.time() - last_ping > 30:
                        ws.send(json.dumps({"method": "ping"}))
                        last_ping = time.time()
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    d = json.loads(msg)
                    if d.get("channel") != "trades":
                        continue
                    rows = []
                    for tr in d.get("data") or []:
                        users = tr.get("users") or [None, None]
                        try:
                            rows.append((tr["time"] / 1000, tr["coin"], tr.get("side"), float(tr["px"]),
                                         float(tr["sz"]), (users[0] or "").lower(), (users[1] or "").lower()))
                        except (KeyError, TypeError, ValueError):
                            continue
                    with self.lock:
                        self.buf += rows
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as e:
                self.log(f"Tape: verbinding weg ({e}); opnieuw over {backoff} s.")
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def _writer(self):
        con = _con(self.path)
        last_prune = 0
        while not self.stop:
            time.sleep(5)
            with self.lock:
                rows, self.buf = self.buf, []
            if rows:
                try:
                    con.executemany("INSERT INTO tape VALUES (?,?,?,?,?,?,?)", rows)
                    con.commit()
                    self.count += len(rows)
                except Exception:
                    pass
            if time.time() - last_prune > 3600:
                try:
                    con.execute("DELETE FROM tape WHERE t < ?", (time.time() - self.keep_hours * 3600,))
                    con.commit()
                except Exception:
                    pass
                last_prune = time.time()


# ---------- opvragen ----------

def query(data_dir, coin, t_from, t_to, px_min=None, px_max=None, min_usd=0, limit=500):
    """Trades in één munt tussen twee tijdstippen (seconden), met adressen."""
    con = _con(tape_db(data_dir))
    sql = "SELECT t, coin, side, px, sz, buyer, seller FROM tape WHERE coin=? AND t BETWEEN ? AND ?"
    args = [coin.upper(), t_from, t_to]
    if px_min is not None:
        sql += " AND px >= ?"
        args.append(px_min)
    if px_max is not None:
        sql += " AND px <= ?"
        args.append(px_max)
    sql += " ORDER BY t LIMIT ?"
    args.append(limit)
    rows = [r for r in con.execute(sql, args).fetchall() if r[3] * r[4] >= min_usd]
    con.close()
    return [{"t": r[0], "tijd": datetime.fromtimestamp(r[0], timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             "coin": r[1], "side": r[2], "px": r[3], "sz": r[4], "usd": round(r[3] * r[4]),
             "koper": r[5], "verkoper": r[6]} for r in rows]


def info(data_dir):
    con = _con(tape_db(data_dir))
    n, t0, t1 = con.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM tape").fetchone()
    coins = con.execute("SELECT COUNT(DISTINCT coin) FROM tape").fetchone()[0]
    con.close()
    f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t else None
    return {"trades": n, "munten": coins, "van": f(t0), "tot": f(t1)}


def _invo_fills(data_dir, day):
    """Invo-fills van één dag mét kant (B = kopen, A = verkopen): (t, user, coin, side, px, sz)."""
    import lz4.frame
    path = os.path.join(data_dir, f"invo_side_{day}.json")
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if os.path.exists(path) and day != today:
        cached = json.load(open(path, encoding="utf-8"))
        if cached:
            return cached
    r = requests.get(FILLS_URL.format(b=INVO_BUILDER, d=day), timeout=60)
    if r.status_code != 200:
        return []
    text = lz4.frame.decompress(r.content).decode("utf-8", "replace")
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): v for k, v in row.items() if k}
        try:
            t = row.get("time") or ""
            try:
                ts = float(t)
                ts = ts / 1000 if ts > 1e11 else ts
            except ValueError:
                ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            out.append([ts, (row.get("user") or "").lower(), str(row.get("coin") or "").upper(),
                        (row.get("side") or "").upper()[:1], float(row.get("px") or 0), float(row.get("sz") or 0)])
        except (ValueError, TypeError):
            continue
    if day != today and out:          # nooit een leeg resultaat bewaren (dan blijft het voor altijd leeg)
        json.dump(out, open(path, "w", encoding="utf-8"))
    return out


def leaders(data_dir, days=2, window_s=5, lookback_s=15, min_users=4, top=40, min_waves=4,
            progress=lambda t: None):
    """Zoek wie de Invo-kopieerders volgen.

    Een 'golf' = minstens min_users verschillende Invo-gebruikers die binnen window_s seconden dezelfde munt dezelfde
    kant op handelen. Voor elke golf kijken we wie in de lookback_s seconden daarvóór dezelfde kant op handelde.
    Drukke handelaren (market makers, bots) zitten toevallig vaak vóór een golf; daarom rekenen we per adres hoe
    vaak dat bij toeval te verwachten was ('lift' = werkelijk / verwacht). Een echte leider zit er veel vaker dan
    toeval voor, en met een korte, vaste voorsprong."""
    con = _con(tape_db(data_dir))
    t_min, t_max = con.execute("SELECT MIN(t), MAX(t) FROM tape").fetchone()
    t_min = t_min or time.time()
    t_max = t_max or time.time()
    invo_users = set()
    waves = []
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y%m%d")
        progress(f"Invo-golven zoeken op {day}…")
        fills = [f for f in _invo_fills(data_dir, day) if t_min + lookback_s <= f[0] <= t_max]
        for f in fills:
            invo_users.add(f[1])
        by = defaultdict(list)
        for f in fills:
            by[(f[2], f[3])].append(f)
        for (coin, side), fl in by.items():
            fl.sort()
            j = 0
            while j < len(fl):
                k = j
                users = set()
                while k < len(fl) and fl[k][0] - fl[j][0] <= window_s:
                    users.add(fl[k][1])
                    k += 1
                if len(users) >= min_users:
                    waves.append((fl[j][0], coin, side, len(users)))
                    j = k
                else:
                    j += 1
    progress(f"{len(waves)} golven; voorgangers zoeken in de tape…")
    hits = defaultdict(lambda: {"golven": 0, "munten": defaultdict(int), "usd": 0.0, "voorsprong": []})
    coins_side = defaultdict(int)
    for t0, coin, side, n in waves:
        coins_side[(coin, side)] += 1
        col = "buyer" if side == "B" else "seller"
        rows = con.execute(f"SELECT t, px, sz, {col} FROM tape WHERE coin=? AND t BETWEEN ? AND ?",
                           (coin, t0 - lookback_s, t0 - 0.2)).fetchall()
        seen = {}
        for t, px, sz, who in rows:
            if not who or who in invo_users:
                continue
            s_ = seen.setdefault(who, [t, 0.0])
            s_[0] = max(s_[0], t)          # laatste trade vóór de golf
            s_[1] += px * sz
        for who, (t, u) in seen.items():
            h = hits[who]
            h["golven"] += 1
            h["munten"][coin] += 1
            h["usd"] += u
            h["voorsprong"].append(t0 - t)
    span = max(1.0, t_max - t_min)
    out = []
    for who, h in hits.items():
        if h["golven"] < min_waves:
            continue
        # Verwacht bij toeval: per munt/kant hoe vaak hij in een willekeurig venster van lookback_s handelt.
        expected = 0.0
        for (coin, side), nw in coins_side.items():
            if coin not in h["munten"]:
                continue
            col = "buyer" if side == "B" else "seller"
            cnt = con.execute(f"SELECT COUNT(DISTINCT CAST(t / ? AS INTEGER)) FROM tape WHERE coin=? AND {col}=?",
                              (lookback_s, coin, who)).fetchone()[0]
            p = min(1.0, cnt * lookback_s / span)
            expected += nw * p
        lift = h["golven"] / expected if expected else 0
        lead = sorted(h["voorsprong"])[len(h["voorsprong"]) // 2]
        out.append({"address": who, "golven": h["golven"], "verwacht": round(expected, 1), "lift": round(lift, 1),
                    "usd": round(h["usd"]), "voorsprong_s": round(lead, 1), "munten": dict(h["munten"])})
    con.close()
    out.sort(key=lambda x: (-x["lift"], -x["golven"]))
    return {"golven": len(waves), "invo_gebruikers": len(invo_users), "leiders": out[:top]}
