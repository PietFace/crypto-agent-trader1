"""Backtest-verzoeken van de website oppakken en uitvoeren op deze pc.

De website kan alleen een backtest aanvragen: geen handel, geen instellingen. Dit bestand vraagt elke
30 seconden of er een verzoek klaarstaat (bot-poll), draait de backtest met de huidige instellingen
en stuurt het resultaat terug (bot-ingest, type backtest_result).
"""
import threading
import time

import requests


def poll_url(ingest_url):
    return ingest_url.rstrip("/").rsplit("/", 1)[0] + "/bot-poll"


def _slim(res, months):
    """Resultaat klein genoeg maken voor de website."""
    def pts(series, every):
        return [[int(t), round(v, 2)] for i, (t, v) in enumerate(series) if i % every == 0 or i == len(series) - 1]
    n = len(res.get("equity") or [])
    every = max(1, n // 300)

    def st(s):
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}
    trades = [{"market": t["market"], "side": t["side"], "open": int(t["open"]), "close": int(t["close"]),
               "eur": round(t["eur"], 2), "pnl": round(t["pnl"], 2), "reason": t["reason"]}
              for t in reversed(res["trades"][-50:])]
    return {"months": months, "start": res["start"], "end": res["end"], "budget": res["budget"],
            "final": round(res["final"], 2), "hold_final": round(res["hold_final"], 2) if res["hold_final"] else None,
            "max_drawdown": round(res["max_drawdown"], 2), "open": res["open"],
            "stats": {k: st(v) for k, v in res["stats"].items()},
            "per_market": {m: st(v) for m, v in res["per_market"].items()},
            "equity": pts(res["equity"], every), "hold": pts(res["hold"], every), "trades": trades}


class JobRunner:
    def __init__(self, get_cfg, log=lambda *a: None):
        self.get_cfg = get_cfg
        self.log = log
        self.busy = False
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(30)

    def _tick(self):
        cfg = self.get_cfg()
        rc = cfg.get("remote") or {}
        if self.busy or not (rc.get("enabled") and rc.get("url") and rc.get("token")):
            return
        r = requests.post(poll_url(rc["url"]), json={}, timeout=15,
                          headers={"x-bot-token": rc["token"], "content-type": "application/json"})
        if r.status_code != 200:
            return
        for req in r.json().get("requests") or []:
            self.busy = True
            try:
                self._run(cfg, rc, req)
            finally:
                self.busy = False

    def _send(self, rc, data):
        requests.post(rc["url"], timeout=30, json={"events": [{"type": "backtest_result", "data": data}]},
                      headers={"x-bot-token": rc["token"], "content-type": "application/json"})

    def _send_logs(self, rc, lines):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        events = [{"type": "log", "data": {"ts": ts, "kind": "info", "text": t}} for t in lines]
        requests.post(rc["url"], timeout=30, json={"events": events},
                      headers={"x-bot-token": rc["token"], "content-type": "application/json"})

    def _run(self, cfg, rc, req):
        import backtest
        import settings
        from python_bitvavo_api.bitvavo import Bitvavo
        months = int(req.get("months", 12))
        shorts = bool(req.get("shorts", True))
        self.log(f"Backtest aangevraagd via je telefoon ({months} maanden, shorts {'aan' if shorts else 'uit'})…", "head")
        try:
            markets = list(cfg.get("markets") or [])
            if "BTC-EUR" not in markets:
                markets = ["BTC-EUR"] + markets
            bv = Bitvavo({})
            data = {m: backtest.fetch_candles(bv, m, months) for m in markets}
            res = backtest.run(data, settings.effective_rules(cfg), shorts=shorts,
                               cycle_minutes=int(cfg.get("cycle_minutes", 120)))
            self._send(rc, {"id": req["id"], "status": "done", "result": _slim(res, months)})
            # Varianten naast elkaar; de uitkomst komt in het logboek, ook op je telefoon.
            try:
                rows = backtest.run_variants(data, settings.effective_rules(cfg), shorts=shorts,
                                             cycle_minutes=int(cfg.get("cycle_minutes", 120)))
                ex = "Kraken" if cfg.get("exchange") == "kraken" else "Bitvavo"
                lines = backtest.variants_text(rows, months, ex)
                for line in lines:
                    self.log(line, "info")
                self._send_logs(rc, lines)
            except Exception as e:
                self.log(f"Varianten-test mislukt: {e}", "bad")
            self.log(f"Backtest klaar: eindstand €{res['final']:.2f} (BTC vasthouden "
                     f"€{(res['hold_final'] or 0):.2f}). Resultaat staat op je telefoon.", "info")
        except Exception as e:
            self._send(rc, {"id": req["id"], "status": "error", "error": str(e)[:300]})
            self.log(f"Backtest mislukt: {e}", "bad")
