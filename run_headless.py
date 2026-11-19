"""CryptoTrader zonder scherm, voor een Raspberry Pi of server die altijd aan staat.

Gebruik:  python3 run_headless.py           (gebruikt ~/.crypto_trader/config.json en trader.db)
Stoppen:  Ctrl-C, of 'sudo systemctl stop cryptotrader' als hij als dienst draait.

Zelfde bot als het programma op je pc: dezelfde instellingen, regels, kopieer-modus, telefoon-website en
backtests op afstand. Laat NOOIT twee exemplaren tegelijk lopen (pc én Pi): dan zou alles dubbel gebeuren.
"""
import signal
import sys
import time

import settings
from bot import Engine


def log(text, kind=""):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{kind or 'info'}] {text}", flush=True)


def main():
    cfg = settings.load()
    eng = Engine(cfg, log)
    problems = eng.problems()
    if problems:
        for p in problems:
            log(f"Kan niet starten: {p}", "bad")
        log(f"Pas {settings.config_path()} aan (of zet de instellingen op je pc goed en kopieer config.json).", "bad")
        sys.exit(1)
    jobs = None
    if (cfg.get("remote") or {}).get("enabled"):
        try:
            from remote_jobs import JobRunner
            jobs = JobRunner(lambda: cfg, log)   # backtests die je via je telefoon aanvraagt
        except Exception as e:
            log(f"Backtests op afstand niet beschikbaar: {e}", "muted")

    stopping = {"now": False}

    def stop(*_):
        if not stopping["now"]:
            stopping["now"] = True
            log("Stoppen…", "info")
            eng.stop()
            if jobs:
                jobs.stop()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    log(f"CryptoTrader zonder scherm, gegevens in {settings.data_dir()}", "info")
    eng.start()
    while eng.running and not stopping["now"]:
        time.sleep(2)
    while eng.running:
        time.sleep(1)
    log("Gestopt.", "info")


if __name__ == "__main__":
    main()
