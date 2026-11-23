"""Verbinding met Claude via een map naast het programma (Cryptotrader\\claude\\).

Claude zet een opdracht neer als <naam>.json, het programma voert hem uit en zet het antwoord in
<naam>.resultaat.json. Alleen opvragen en zoeken: Claude kan via deze map NIETS aan het handelen, de keys of de
instellingen veranderen. Een 'voorstel' komt alleen in de lijst bij Kopiëren; Frank klikt zelf op Toevoegen.

Opdrachten:
  {"type": "zoek", "name": "missed", "trades": [["BTC", 63071.0], ...], "account": 20748076, "style": "lang"}
  {"type": "posities", "addresses": ["0x...", ...]}
  {"type": "status"}
  {"type": "tape", "coin": "BTC", "van": "2026-11-19T18:30:00+01:00", "tot": "2026-11-19T18:35:00+01:00",
   "richting": "verkoop", "px_min": 63000, "px_max": 63200, "min_bedrag": 1000, "controleer": 10}
  {"type": "tape_status"}
  {"type": "vingerafdruk", "trades": [["BTC", "verkoop", "2026-11-22T10:42:00+01:00", 60211], ...]}   (wie deed deze trades?)
  {"type": "leiders", "days": 2}
  {"type": "posmatch", "account": 346920, "positions": [["ZRO", "long"], ["LIT", "long"]], "tol_acct": 0.05}   (wie volgen de Invo-kopieerders? zie tape.leaders)
  {"type": "info", "requests": [{"type": "portfolio", "user": "0x..."}]}   (alleen openbare leesverzoeken)
  {"type": "ranglijst", "min_acct": 20000, "min_maand_pnl": 0, "min_week_pnl": 0, "min_activiteit": 5, "alleen_invo": false}
  {"type": "keuren", "addresses": ["0x...", ...], "dagen": 14}   (trades per dag, hoe lang open, winstkans)
  {"type": "voorstel", "name": "missed", "address": "0x...", "style": "lang", "reden": "..."}
"""
import glob
import json
import os
import sqlite3
import dbutil
import sys
import threading
import time
import traceback

import settings


def link_dir():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = settings.data_dir()
    d = os.path.join(base, "claude")
    os.makedirs(d, exist_ok=True)
    return d


def proposals_path():
    return os.path.join(settings.data_dir(), "voorstellen.json")


def load_proposals():
    try:
        with open(proposals_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_proposals(items):
    with open(proposals_path(), "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)


def remove_proposals(addresses):
    addresses = {a.lower() for a in addresses}
    items = [p for p in load_proposals() if p.get("address", "").lower() not in addresses]
    save_proposals(items)


def _rows(db, sql):
    try:
        con = dbutil.connect(db)
        con.row_factory = sqlite3.Row
        out = [dict(r) for r in con.execute(sql).fetchall()]
        con.close()
        return out
    except Exception as e:
        return [{"fout": str(e)}]


def _log_tail(n=300):
    files = sorted(glob.glob(os.path.join(settings.data_dir(), "logs", "sessie_*.txt")))
    if not files:
        return []
    try:
        with open(files[-1], encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


class ClaudeLink:
    def __init__(self, on_proposal=lambda p: None, log=lambda t: None, interval=3, tape=None):
        self.on_proposal, self.log, self.interval, self.tape = on_proposal, log, interval, tape
        self.pause = 20 if interval >= 1 else 0
        self.dir = link_dir()
        self.busy = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                for path in sorted(glob.glob(os.path.join(self.dir, "*.json"))):
                    base = os.path.basename(path)
                    if base.endswith(".resultaat.json") or base.endswith(".bezig.json"):
                        continue
                    if time.time() - os.path.getmtime(path) < 1.0:
                        continue        # wordt misschien nog geschreven
                    self._run(path)
            except Exception:
                pass
            time.sleep(self.interval)

    def _run(self, path):
        stem = path[:-5]
        busy = stem + ".bezig.json"
        try:
            os.replace(path, busy)
            with open(busy, encoding="utf-8") as f:
                job = json.load(f)
        except Exception as e:
            self._write(stem, {"ok": False, "fout": f"opdracht onleesbaar: {e}"})
            return
        t0 = time.time()
        self.log(f"Claude-opdracht: {job.get('type')} {job.get('name', '')}".strip())
        try:
            res = self.handle(job, stem)
            out = {"ok": True, "type": job.get("type"), "resultaat": res}
        except Exception as e:
            out = {"ok": False, "type": job.get("type"), "fout": str(e), "trace": traceback.format_exc()[-2000:]}
        out["duur_s"] = round(time.time() - t0, 1)
        out["klaar"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._write(stem, out)
        try:
            os.remove(busy)
        except Exception:
            pass
        if job.get("type") in ("zoek", "posities", "info", "ranglijst", "keuren"):
            time.sleep(self.pause)  # Hyperliquid-limiet laten bijkomen voor de volgende opdracht

    def _write(self, stem, data):
        tmp = stem + ".resultaat.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(tmp, stem + ".resultaat.json")

    def _progress(self, stem, text):
        try:
            with open(stem + ".voortgang.txt", "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def handle(self, job, stem):
        import copier
        kind = job.get("type")
        if kind == "zoek":
            copier.LAST_DIAG.clear()
            trades = [(c, float(p)) for c, p in job["trades"]]
            opts = {}
            if job.get("tol_acct") is not None:
                opts["tol_acct"] = min(float(job["tol_acct"]), 0.3)
            if job.get("max_check") is not None:
                opts["max_check"] = min(int(job["max_check"]), 400)
            if job.get("vers"):
                opts["fresh"] = True
            res = copier.find_trader(trades, settings.data_dir(), days=int(job.get("days", 60)),
                                     account=job.get("account"),
                                     progress=lambda t: self._progress(stem, t), acct_opts=opts)
            conf = copier.confidence(res)
            out = []
            for c in res[:6]:
                try:
                    st = copier.trader_state(c["address"])
                    c["perps"], c["posities"] = st["account"], st["positions"]
                    c["spot_usdc"] = copier.spot_usdc(c["address"])
                    c["account"] = c["perps"] + c["spot_usdc"]
                except Exception as e:
                    c["posities"] = f"fout: {e}"
                out.append(c)
                time.sleep(1)
            try:
                os.remove(stem + ".voortgang.txt")
            except Exception:
                pass
            return {"name": job.get("name"), "zekerheid": conf, "kandidaten": out,
                    "account_zoektocht": dict(copier.LAST_DIAG)}
        if kind == "posities":
            out = {}
            for a in job.get("addresses", [])[:30]:
                try:
                    out[a] = copier.trader_state(a)
                    out[a]["spot_usdc"] = copier.spot_usdc(a)
                except Exception as e:
                    out[a] = {"fout": str(e)}
                time.sleep(0.2)
            return out
        if kind == "info":
            # Alleen openbare Hyperliquid-gegevens opvragen; niets wat iets kan veranderen.
            allowed = {"clearinghouseState", "spotClearinghouseState", "userFills", "userFillsByTime", "portfolio",
                       "openOrders", "frontendOpenOrders", "historicalOrders", "userFunding"}
            out = []
            for req in job.get("requests", [])[:20]:
                if req.get("type") not in allowed:
                    out.append({"fout": f"niet toegestaan: {req.get('type')}"})
                    continue
                try:
                    r = copier._http.post(copier.INFO_URL, json=req, timeout=20)
                    out.append(r.json())
                except Exception as e:
                    out.append({"fout": str(e)})
                time.sleep(0.5)
            return out
        if kind == "posmatch":
            # Live Portfolio met zichtbaar bedrag en open posities: zoek accounts van ongeveer dat bedrag die precies
            # dezelfde munten aan dezelfde kant open hebben. clearinghouseState is goedkoop, dus honderden kan.
            import followers
            account = float(job["account"])
            tol = min(float(job.get("tol_acct", 0.05)), 0.3)
            want = {str(c).upper(): str(sd).lower() for c, sd in job["positions"]}
            rows = followers.fetch_leaderboard(os.path.join(settings.data_dir(), "leaderboard.json"))
            near = [r for r in rows if r.get("address") and r.get("account_value")
                    and abs(r["account_value"] - account) / account <= tol]
            near.sort(key=lambda r: abs(r["account_value"] - account))
            near = near[:min(int(job.get("max_check", 400)), 1000)]
            hits, checked = [], 0
            for i, r in enumerate(near):
                if i % 10 == 0:
                    self._progress(stem, f"{i}/{len(near)} accounts gecontroleerd, {len(hits)} treffers")
                try:
                    st = copier._post_retry({"type": "clearinghouseState", "user": r["address"]})
                except Exception:
                    continue
                checked += 1
                pos = {}
                for ap in st.get("assetPositions") or []:
                    p_ = ap.get("position") or {}
                    try:
                        szi = float(p_.get("szi") or 0)
                    except (TypeError, ValueError):
                        continue
                    if szi:
                        pos[str(p_.get("coin")).upper()] = {"side": "long" if szi > 0 else "short",
                                                             "entry": p_.get("entryPx"),
                                                             "lev": (p_.get("leverage") or {}).get("value"),
                                                             "pnl": p_.get("unrealizedPnl")}
                ok = sum(1 for c, sd in want.items() if c in pos and (sd in ("", "?") or pos[c]["side"] == sd))
                if ok == len(want):
                    hits.append({"address": r["address"].lower(), "leaderboard_account": r["account_value"],
                                 "posities": pos})
                time.sleep(0.15)
            try:
                os.remove(stem + ".voortgang.txt")
            except Exception:
                pass
            return {"in_buurt": len(near), "gecontroleerd": checked, "treffers": hits}
        if kind == "ranglijst":
            # Leaderboard filteren op winst (maand en week), grootte en activiteit; Invo-gebruikers gemarkeerd.
            import followers
            dd = settings.data_dir()
            rows = followers.fetch_leaderboard(os.path.join(dd, "leaderboard.json"))
            try:
                invo = followers.fetch_invo_users(os.path.join(dd, "invo_users.json"),
                                                  days=int(job.get("invo_dagen", 7)))
            except Exception:
                invo = {}
            invo = {str(a).lower(): v for a, v in (invo or {}).items()}
            lo, hi = float(job.get("min_acct", 20000)), float(job.get("max_acct", 5e6))
            out = []
            for r in rows:
                a = str(r.get("address") or "").lower()
                acct = r.get("account_value") or 0
                m, w, al = r.get("month") or {}, r.get("week") or {}, r.get("alltime") or {}
                if not a or not (lo <= acct <= hi):
                    continue
                if m.get("pnl", 0) < float(job.get("min_maand_pnl", 0)) or w.get("pnl", 0) < float(job.get("min_week_pnl", 0)):
                    continue
                if al.get("pnl", 0) < float(job.get("min_alltime_pnl", 0)):
                    continue
                activiteit = m.get("vlm", 0) / acct if acct else 0  # maandvolume / account: hoe vaak hij handelt
                if activiteit < float(job.get("min_activiteit", 5)):
                    continue
                if job.get("alleen_invo") and a not in invo:
                    continue
                out.append({"address": a, "naam": r.get("name") or "", "account": round(acct),
                            "maand_pnl": round(m.get("pnl", 0)), "maand_roi": round(m.get("roi", 0) * 100, 1),
                            "week_pnl": round(w.get("pnl", 0)), "week_roi": round(w.get("roi", 0) * 100, 1),
                            "alltime_pnl": round(al.get("pnl", 0)), "activiteit": round(activiteit, 1),
                            "invo_volume": round(invo.get(a, 0))})
            key = job.get("sorteer", "maand_roi")
            out.sort(key=lambda x: x.get(key, 0), reverse=True)
            return {"leaderboard": len(rows), "invo_gebruikers": len(invo), "gevonden": len(out),
                    "traders": out[:min(int(job.get("limit", 60)), 300)]}
        if kind == "keuren":
            # Per adres de trades van de laatste dagen: hoeveel, hoe lang open, winstkans, resultaat, grootste verlies.
            res = []
            addrs = [str(a).lower() for a in job.get("addresses") or []][:25]
            days = max(1, min(int(job.get("dagen", 14)), 60))
            for i, a in enumerate(addrs):
                self._progress(stem, f"{i}/{len(addrs)} traders gekeurd")
                try:
                    fl = copier.fills_since(a, days=days, max_pages=5)
                except Exception as e:
                    res.append({"address": a, "fout": str(e)})
                    continue
                fl.sort(key=lambda f: f.get("time", 0))
                open_at, pnl_acc, trades = {}, {}, []
                for f in fl:
                    c = f.get("coin")
                    try:
                        start = float(f.get("startPosition") or 0)
                        sz = float(f.get("sz") or 0)
                        signed = sz if f.get("side") == "B" else -sz
                        pnl = float(f.get("closedPnl") or 0) - float(f.get("fee") or 0)
                    except (TypeError, ValueError):
                        continue
                    if abs(start) < 1e-12 and c not in open_at:
                        open_at[c] = f.get("time", 0)
                        pnl_acc[c] = 0.0
                    if c in open_at:
                        pnl_acc[c] += pnl
                    end = start + signed
                    if c in open_at and (abs(end) < 1e-9 * max(1.0, abs(start)) or start * end < 0):
                        trades.append({"coin": c, "uur": (f.get("time", 0) - open_at[c]) / 3.6e6,
                                       "pnl": pnl_acc[c]})
                        del open_at[c]
                        if start * end < 0:          # omgedraaid: nieuwe positie de andere kant op
                            open_at[c] = f.get("time", 0)
                            pnl_acc[c] = 0.0
                n = len(trades)
                try:
                    import kraken
                    _kp = getattr(self, "_kpub", None) or kraken.KrakenPublic(max_age=600)
                    self._kpub = _kp
                    ok = {c: _kp.supported(kraken.symbol_for_coin(c)) for c in {t["coin"] for t in trades}}
                except Exception:
                    ok = {}
                kt = [t for t in trades if ok.get(t["coin"])]
                holds = sorted(t["uur"] for t in trades)
                wins = sum(1 for t in trades if t["pnl"] > 0)
                munten = {}
                for t in trades:
                    munten[t["coin"]] = munten.get(t["coin"], 0) + 1
                res.append({"address": a, "fills": len(fl), "trades": n,
                            "per_dag": round(n / days, 1),
                            "mediaan_uur": round(holds[n // 2], 2) if n else None,
                            "winstkans": round(100 * wins / n) if n else None,
                            "resultaat": round(sum(t["pnl"] for t in trades)),
                            "grootste_verlies": round(min((t["pnl"] for t in trades), default=0)),
                            "grootste_winst": round(max((t["pnl"] for t in trades), default=0)),
                            "kraken_pct": round(100 * len(kt) / n) if n else None,
                            "kraken_resultaat": round(sum(t["pnl"] for t in kt)),
                            "kraken_winstkans": round(100 * sum(1 for t in kt if t["pnl"] > 0) / len(kt)) if kt else None,
                            "niet_op_kraken": sorted(c for c, v in ok.items() if not v)[:10],
                            "nu_open": sorted(open_at),
                            "munten": dict(sorted(munten.items(), key=lambda x: -x[1])[:8])})
                time.sleep(1)
            try:
                os.remove(stem + ".voortgang.txt")
            except Exception:
                pass
            return res
        if kind == "backtest":
            # Varianten naast elkaar op historische uurkoersen (Bitvavo), met de regels van nu.
            import backtest
            from python_bitvavo_api.bitvavo import Bitvavo
            cfg = settings.load()
            months = max(1, min(int(job.get("months", 12)), 36))
            markets = list(job.get("markets") or cfg.get("markets") or [])
            if "BTC-EUR" not in markets:
                markets = ["BTC-EUR"] + markets
            bv = Bitvavo({})
            data = {}
            for m in markets:
                self._progress(stem, f"koersen ophalen: {m}")
                data[m] = backtest.fetch_candles(bv, m, months)
            rules = settings.effective_rules(cfg)
            custom = [(str(n), dict(c)) for n, c in (job.get("varianten") or [])] or None
            rows = backtest.run_variants(data, rules, shorts=bool(job.get("shorts", cfg.get("shorts_sim", True))),
                                         cycle_minutes=int(cfg.get("cycle_minutes", 120)),
                                         progress=lambda t: self._progress(stem, t), variants=custom)
            ex = "Kraken" if cfg.get("exchange") == "kraken" else "Bitvavo"
            return {"months": months, "exchange": ex, "fee_pct": rules.get("fee_pct"),
                    "leverage": rules.get("leverage", 1), "varianten": rows,
                    "tekst": backtest.variants_text(rows, months, ex)}
        if kind == "invo_dag":
            # Controle: is het Invo-bestand van een dag er, en hoe ziet het eruit?
            import tape as tp
            import lz4.frame
            import requests
            day = str(job.get("day"))
            r = requests.get(tp.FILLS_URL.format(b=tp.INVO_BUILDER, d=day), timeout=60)
            out = {"day": day, "http": r.status_code, "bytes": len(r.content)}
            if r.status_code == 200:
                text = lz4.frame.decompress(r.content).decode("utf-8", "replace")
                lines = text.splitlines()
                out.update({"regels": len(lines), "eerste": lines[:4], "laatste": lines[-2:]})
                parsed = tp._invo_fills(settings.data_dir(), day)
                out["gelezen"] = len(parsed)
                out["voorbeeld_gelezen"] = parsed[:2]
                t_min = tp._con(tp.tape_db(settings.data_dir())).execute("SELECT MIN(t) FROM tape").fetchone()[0]
                out["na_tape_start"] = sum(1 for f in parsed if t_min and f[0] >= t_min)
            return out
        if kind == "leiders":
            import tape as tp
            return tp.leaders(settings.data_dir(), days=int(job.get("days", 2)),
                              window_s=float(job.get("venster_s", 5)), lookback_s=float(job.get("terug_s", 15)),
                              min_users=int(job.get("min_gebruikers", 4)), top=int(job.get("top", 40)),
                              min_waves=int(job.get("min_golven", 4)), progress=lambda t: self._progress(stem, t))
        if kind == "tape_status":
            return self.tape.status() if self.tape else {"fout": "meeschrijven staat uit"}
        if kind == "vingerafdruk":
            # Een paar trades van een (Invo-)trader met tijd en prijs: wie zat in de tape bij ALLE/de meeste?
            import tape as tp
            from datetime import datetime
            hits, per_event = {}, []
            events = job.get("trades") or []
            for ev in events[:12]:
                coin, kant, tijd, px = ev[0], ev[1], ev[2], float(ev[3])
                win = float(ev[4]) if len(ev) > 4 else 180
                tol = float(job.get("prijs_tol_pct", 0.05)) / 100
                t0 = datetime.fromisoformat(str(tijd)).timestamp()
                rows = tp.query(settings.data_dir(), coin, t0 - win, t0 + win, px * (1 - tol), px * (1 + tol),
                                min_usd=float(job.get("min_bedrag", 0)), limit=20000)
                # verkoop = short openen of long sluiten: de trader is de verkoper; koop andersom
                who = set()
                for r in rows:
                    a = r["verkoper"] if kant.startswith("verk") else r["koper"]
                    if a:
                        who.add(a.lower())
                per_event.append({"trade": ev, "tape_trades": len(rows), "adressen": len(who)})
                for a in who:
                    hits[a] = hits.get(a, 0) + 1
            need = int(job.get("min_treffers", max(2, len(events) - 1)))
            best = sorted(((a, n) for a, n in hits.items() if n >= need), key=lambda x: -x[1])[:30]
            out = []
            for a, n in best[:int(job.get("controleer", 15))]:
                r = {"address": a, "treffers": n}
                try:
                    st = copier.trader_state(a)
                    r["posities"] = st["positions"]
                    r["account"] = st["account"] + copier.spot_usdc(a)
                except Exception as e:
                    r["fout"] = str(e)
                out.append(r)
                time.sleep(0.5)
            return {"tape": tp.info(settings.data_dir()), "per_trade": per_event, "kandidaten": out,
                    "overig": [{"address": a, "treffers": n} for a, n in best[len(out):]]}
        if kind == "tape":
            import tape as tp
            def ms(v):
                if isinstance(v, (int, float)):
                    return int(v)
                from datetime import datetime
                return int(datetime.fromisoformat(str(v)).timestamp() * 1000)
            rows = tp.search(settings.data_dir(), job["coin"], ms(job["van"]), ms(job["tot"]),
                             direction=job.get("richting"), px_min=job.get("px_min"), px_max=job.get("px_max"),
                             min_notional=float(job.get("min_bedrag", 0)), limit=int(job.get("max", 40)))
            for r in rows[:int(job.get("controleer", 0))]:
                try:
                    st = copier.trader_state(r["address"])
                    r["posities"] = st["positions"]
                    r["account"] = st["account"] + copier.spot_usdc(r["address"])
                except Exception as e:
                    r["posities"] = f"fout: {e}"
                time.sleep(0.5)
            return {"aantal": len(rows), "trades": rows}
        if kind == "status":
            cfg = settings.load()
            db = settings.db_path()
            return {
                "mode": cfg.get("mode"), "exchange": cfg.get("exchange"),
                "copy": {k: v for k, v in cfg.get("copy", {}).items()},
                "budget_eur": cfg.get("rules", {}).get("budget_eur"),
                "kopie_open": _rows(db, "SELECT * FROM copy_positions"),
                "kopie_gesloten": _rows(db, "SELECT * FROM copy_trades ORDER BY rowid DESC LIMIT 40"),
                "trader_posities": _rows(db, "SELECT * FROM copy_trader_positions"),
                "voorstellen": load_proposals(),
                "log": _log_tail(),
            }
        if kind == "voorstel":
            addr = str(job["address"]).strip().lower()
            if not (addr.startswith("0x") and len(addr) == 42):
                raise ValueError("ongeldig adres")
            p = {"name": str(job.get("name", ""))[:40], "address": addr,
                 "style": job.get("style") if job.get("style") in ("lang", "kort") else "lang",
                 "reden": str(job.get("reden", ""))[:200], "account_invo": job.get("account"),
                 "tijd": time.strftime("%Y-%m-%d %H:%M")}
            items = [x for x in load_proposals() if x.get("address") != addr] + [p]
            save_proposals(items)
            self.on_proposal(p)
            return {"opgeslagen": p, "let_op": "Frank moet zelf op Toevoegen klikken bij Kopiëren."}
        raise ValueError(f"onbekende opdracht: {kind}")
