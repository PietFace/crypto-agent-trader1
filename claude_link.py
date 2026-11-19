"""Verbinding met Claude via een map naast het programma (Cryptotrader\\claude\\).

Claude zet een opdracht neer als <naam>.json, het programma voert hem uit en zet het antwoord in
<naam>.resultaat.json. Alleen opvragen en zoeken: Claude kan via deze map NIETS aan het handelen, de keys of de
instellingen veranderen. Een 'voorstel' komt alleen in de lijst bij Kopiëren; Frank klikt zelf op Toevoegen.

Opdrachten:
  {"type": "zoek", "name": "missed", "trades": [["BTC", 63071.0], ...], "account": 20748076, "style": "lang"}
  {"type": "posities", "addresses": ["0x...", ...]}
  {"type": "status"}
  {"type": "voorstel", "name": "missed", "address": "0x...", "style": "lang", "reden": "..."}
"""
import glob
import json
import os
import sqlite3
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
        con = sqlite3.connect(db)
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
    def __init__(self, on_proposal=lambda p: None, log=lambda t: None, interval=3):
        self.on_proposal, self.log, self.interval = on_proposal, log, interval
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
            trades = [(c, float(p)) for c, p in job["trades"]]
            res = copier.find_trader(trades, settings.data_dir(), days=int(job.get("days", 60)),
                                     account=job.get("account"),
                                     progress=lambda t: self._progress(stem, t))
            conf = copier.confidence(res)
            out = []
            for c in res[:6]:
                try:
                    st = copier.trader_state(c["address"])
                    c["account"], c["posities"] = st["account"], st["positions"]
                except Exception as e:
                    c["posities"] = f"fout: {e}"
                out.append(c)
            try:
                os.remove(stem + ".voortgang.txt")
            except Exception:
                pass
            return {"name": job.get("name"), "zekerheid": conf, "kandidaten": out}
        if kind == "posities":
            out = {}
            for a in job.get("addresses", [])[:30]:
                try:
                    out[a] = copier.trader_state(a)
                except Exception as e:
                    out[a] = {"fout": str(e)}
                time.sleep(0.2)
            return out
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
