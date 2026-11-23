"""Instellingen en mappen. Alles wordt bewaard in %APPDATA%\\CryptoTrader."""
import copy
import json
import os
import sys

APP_NAME = "CryptoTrader"

DEFAULTS = {
    "keys": {"anthropic": "", "bitvavo_key": "", "bitvavo_secret": "", "kraken_key": "", "kraken_secret": ""},
    "live": False,
    "live_bronnen": ["volgers", "beide"],
    "mode": "beide",
    # Eén budget voor alles: rules.budget_eur is het TOTAAL; copy_share is het deel voor de kopieer-modus.
    "copy_share": 0.5,  # "beide" (gewone bot + kopiëren), "bot" (alleen gewone bot) of "kopie" (alleen kopiëren)
    "exchange": "bitvavo",  # "bitvavo" of "kraken" (Kraken-perps, voorlopig alleen droog)
    "kraken": {"leverage": 3},  # max 5
    # Kopieer-modus: gekozen traders 1-op-1 volgen op Kraken. Live alleen als bot live op Kraken staat én "live" aan.
    "copy": {"enabled": False, "live": False, "realtime": True, "max_loss_eur": 5.0, "interval_s": 5, "eur_per_trade": 10, "max_leverage": 10,
             "max_positions": 5, "daily_loss_eur": 10, "stop_pct_lang": 10, "stop_pct_kort": 3, "traders": [],
             "follow_size": True, "resize_min_pct": 10, "max_scale": 3,
             "sizing": "verhouding", "altijd_minimum": True, "scale_pct": 100, "pot_risk_pct": 25, "noodrem_on": True, "noodrem_pct": 25, "trader_rem_pct": 8, "trader_pauze_uur": 24},  # alleen deze signalen met echt geld; de rest als schaduwtrade
    "trail": {"enabled": False, "activate_frac": 0.5, "distance_frac": 0.5, "buffer_pct": 0.2, "spread_pct": 0.1,
              "reentry": True, "max_reentries": 1, "reentry_band_pct": 0.5, "reentry_hours": 24},
    "tape": {"enabled": True, "min_notional": 250, "retention_h": 48},   # trades meeschrijven om traders te vinden
    "markets": ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR"],
    "cycle_minutes": 120,
    "model": "claude-haiku-5-5",
    "model_chief": "",  # leeg = zelfde als model
    "shorts_sim": True,
    "max_api_usd_month": 5.0,  # 0 = geen limiet
    "tick_seconds": 30,  # volgers, regels en stop-loss controleren
    "fast_check": True,  # elke 5 minuten zonder AI kijken; AI alleen bij een nieuwe setup
    "fast_cooldown_min": 30,
    "reopen_pause_min": 60,  # na zelf sluiten zo lang niet opnieuw dezelfde kant op  # per munt hooguit eens per zoveel minuten Skeptic+Chief tussendoor
    "followers": {
        "enabled": True, "auto": True, "invo": True, "quality": True, "conviction": True,
                  "follow_exit": True, "follow_stop_mult": 2.0, "count": 20, "manual": [],
        "min_traders": 4, "min_consensus": 0.6, "use_as_setup": True,
        "exit_on_flip": True,  # positie sluiten als de volgers duidelijk de andere kant op draaien
    },  # Chief mag shorts kiezen; die worden alleen gesimuleerd
    "remote": {"enabled": False, "url": "https://pietface-trader-live.lovable.app/api/public/bot-ingest", "token": ""},
    "rules": {
        "budget_eur": 200,
        "max_trade_eur": 20,
        "min_order_eur": 5,
        "max_positions": 3,
        "max_loss_per_trade_eur": 1.0,
        "daily_loss_limit_eur": 5,
        "max_buys_per_day": 4,
        "reward_risk": 2.0,
        "min_confidence": 0.65,
        "max_spread_pct": 0.4,
        "fee_pct": 0.25,
        "cooldown_hours_after_stop": 12,
        "daily_trend": False,  # alleen kopen boven / shorten onder het 20-daags gemiddelde (grafiekregels)
        "stop_pct": {"default": 6, "BTC-EUR": 5, "ETH-EUR": 5, "SOL-EUR": 7, "XRP-EUR": 7},
        "blocked_markets": [],
        "short_borrow_pct_day": 0.05,  # aanname leenkosten per dag voor gesimuleerde shorts
    },
}

ALL_MARKETS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "DOGE-EUR", "LINK-EUR", "DOT-EUR"]
MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5"]


def resource(name):
    """Pad naar een meegeleverd bestand (werkt ook in de .exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def data_dir():
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(root, APP_NAME)
    else:
        d = os.path.join(os.path.expanduser("~"), ".crypto_trader")
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    return os.path.join(data_dir(), "config.json")


def db_path():
    return os.path.join(data_dir(), "trader.db")


def stop_path():
    return os.path.join(data_dir(), "STOP")


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _import_env(cfg):
    """Eenmalig: een bestaande .env naast het programma overnemen."""
    for folder in (os.getcwd(), os.path.dirname(os.path.abspath(sys.argv[0] or "."))):
        p = os.path.join(folder, ".env")
        if not os.path.exists(p):
            continue
        env = {}
        for line in open(p, encoding="utf-8", errors="ignore"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        key = env.get("ANTHROPIC_API_KEY", "")
        if key and key != "sk-ant-...":
            cfg["keys"]["anthropic"] = key
        cfg["keys"]["bitvavo_key"] = env.get("BITVAVO_KEY", "")
        cfg["keys"]["bitvavo_secret"] = env.get("BITVAVO_SECRET", "")
        return True
    return False


CFG_VERSION = 4


def budgets(cfg):
    """(budget gewone bot, budget kopiëren) uit het totaal, de verdeling en de modus."""
    total = float(cfg["rules"]["budget_eur"])
    share = min(1.0, max(0.0, float(cfg.get("copy_share", 0.5))))
    mode = cfg.get("mode", "beide")
    if mode == "bot":
        return total, 0.0
    if mode == "kopie":
        return 0.0, total
    return total * (1 - share), total * share


def effective_rules(cfg):
    """Regels zoals de bot ze gebruikt. Op Kraken: lagere kosten, geen leenkosten voor shorts, hefboom."""
    rules = copy.deepcopy(cfg["rules"])
    rules["total_budget_eur"] = float(cfg["rules"]["budget_eur"])
    rules["budget_eur"] = budgets(cfg)[0] or rules["total_budget_eur"]   # deel voor de gewone bot
    if cfg.get("exchange") == "kraken":
        import kraken
        rules["fee_pct"] = kraken.TAKER_FEE_PCT
        rules["short_borrow_pct_day"] = 0.0
        lev = int((cfg.get("kraken") or {}).get("leverage", 3) or 1)
        rules["leverage"] = max(1, min(kraken.MAX_LEVERAGE, lev))
    else:
        rules["leverage"] = 1
    return rules


def load():
    cfg = copy.deepcopy(DEFAULTS)
    p = config_path()
    if os.path.exists(p):
        try:
            _merge(cfg, json.load(open(p, encoding="utf-8")))
        except Exception:
            pass
        if int(cfg.get("cfg_version", 1)) < 2:
            # Versie 2: 20 volgers, minstens 4 dezelfde kant op, elke 30 seconden kijken.
            cfg["followers"]["count"] = 20
            cfg["followers"]["min_traders"] = 4
            cfg["tick_seconds"] = 30
            # Kosten: Haiku voor alle agents en de grote analyse hooguit elke 2 uur.
            cfg["model"] = "claude-haiku-5-5"
            cfg["model_chief"] = ""
            cfg["cycle_minutes"] = max(120, int(cfg.get("cycle_minutes", 120)))
        if int(cfg.get("cfg_version", 1)) < 3:
            # Versie 3: één budget voor gewone bot + kopiëren: €200 totaal, 50/50.
            cfg["rules"]["budget_eur"] = 200
            cfg["copy_share"] = 0.5
        if int(cfg.get("cfg_version", 1)) < 4:
            # Noodrem ruimer (25%, vanaf echt geld) en per trader een eigen rem.
            if float(cfg["copy"].get("noodrem_pct", 15)) < 25:
                cfg["copy"]["noodrem_pct"] = 25
        if int(cfg.get("cfg_version", 1)) < CFG_VERSION:
            cfg["cfg_version"] = CFG_VERSION
            save(cfg)
    else:
        cfg["cfg_version"] = CFG_VERSION
        if _import_env(cfg):
            save(cfg)
    return cfg


def save(cfg):
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config_path())
