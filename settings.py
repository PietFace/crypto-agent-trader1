"""Instellingen en mappen. Alles wordt bewaard in %APPDATA%\\CryptoTrader."""
import copy
import json
import os
import sys

APP_NAME = "CryptoTrader"

DEFAULTS = {
    "keys": {"anthropic": "", "bitvavo_key": "", "bitvavo_secret": ""},
    "live": False,
    "markets": ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR"],
    "cycle_minutes": 120,
    "model": "claude-haiku-5-5",
    "model_chief": "",  # leeg = zelfde als model
    "shorts_sim": True,  # Chief mag shorts kiezen; die worden alleen gesimuleerd
    "remote": {"enabled": False, "url": "https://pietface-trader-live.lovable.app/api/public/bot-ingest", "token": ""},
    "rules": {
        "budget_eur": 100,
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


def load():
    cfg = copy.deepcopy(DEFAULTS)
    p = config_path()
    if os.path.exists(p):
        try:
            _merge(cfg, json.load(open(p, encoding="utf-8")))
        except Exception:
            pass
    elif _import_env(cfg):
        save(cfg)
    return cfg


def save(cfg):
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config_path())
