"""Op afstand meekijken: stuurt stand, keuzes, trades en logboek naar het online dashboard.

Alleen versturen, nooit opdrachten ontvangen. Een storing hier stopt de bot nooit.
"""
import queue
import threading
import time

import requests


class Remote:
    def __init__(self, url, token, log=None):
        self.url = (url or "").strip()
        self.token = (token or "").strip()
        self.log = log or (lambda *a: None)
        self.q = queue.Queue(maxsize=5000)
        self._stop = threading.Event()
        self._warned = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def push(self, kind, data):
        try:
            self.q.put_nowait({"type": kind, "data": data})
        except queue.Full:
            pass

    def close(self):
        self._stop.set()

    def _send(self, events):
        r = requests.post(self.url, json={"events": events}, timeout=15,
                          headers={"x-bot-token": self.token, "content-type": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]}")

    def _run(self):
        while not self._stop.is_set() or not self.q.empty():
            batch = []
            try:
                batch.append(self.q.get(timeout=3))
            except queue.Empty:
                continue
            while len(batch) < 200:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            for attempt in range(3):
                try:
                    self._send(batch)
                    if self._warned:
                        self.log("Op afstand meekijken: verbinding weer in orde.", "info")
                        self._warned = False
                    break
                except Exception as e:
                    if attempt == 2:
                        if not self._warned:
                            self.log(f"Op afstand meekijken: versturen mislukt ({e}). De bot draait gewoon door.",
                                     "muted")
                            self._warned = True
                    else:
                        time.sleep(5)


def test(url, token):
    """Geeft (ok, melding) voor de knop 'Verbinding testen'."""
    from datetime import datetime, timezone
    try:
        r = requests.post(url.strip(), timeout=15,
                          headers={"x-bot-token": token.strip(), "content-type": "application/json"},
                          json={"events": [{"type": "log", "data": {
                              "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "kind": "info", "text": "Verbindingstest vanaf de pc gelukt."}}]})
        if r.status_code == 200:
            return True, "Verbinding gelukt. Op je telefoon staat een testregel in het logboek."
        if r.status_code == 401:
            return False, "Token klopt niet (401)."
        return False, f"Mislukt: HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:
        return False, f"Mislukt: {e}"
