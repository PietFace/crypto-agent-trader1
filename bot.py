"""Multi-agent crypto trader voor Bitvavo.

Agents: Charts, News, Sentiment, Skeptic -> Chief beslist.
De risicopoort (risk_gate.py + rules.json, gewone code) heeft altijd het laatste woord.

Start:  python bot.py          (LIVE=false in .env = droog draaien, geen echte orders)
Stoppen: maak een leeg bestand STOP aan in deze map, of Ctrl+C.
"""
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import feedparser
import requests
from dotenv import load_dotenv
from python_bitvavo_api.bitvavo import Bitvavo

import kosten
import risk_gate

load_dotenv()

# ---------- Instellingen ----------
# Risico- en budgetregels staan in rules.json. Hier alleen de techniek.
LIVE = os.getenv("LIVE", "false").lower() == "true"
MARKETS = [m.strip() for m in os.getenv("MARKETS", "BTC-EUR,ETH-EUR,SOL-EUR,XRP-EUR").split(",")]
CYCLE_MINUTES = int(os.getenv("CYCLE_MINUTES", "120"))       # hoe vaak de agents draaien
EXIT_CHECK_SECONDS = 300                                      # stop/doel controle elke 5 min
DB_PATH = "trader.db"
RULES = risk_gate.load_rules()
BUDGET_EUR = float(RULES["budget_eur"])
SIM_FEE = RULES["fee_pct"] / 100                              # fee bij droog draaien

# Model per agent: MODEL_CHARTS, MODEL_NEWS, MODEL_SENTIMENT, MODEL_SKEPTIC, MODEL_CHIEF.
# Niet ingesteld? Dan geldt MODEL (standaard Haiku).
DEFAULT_MODEL = os.getenv("MODEL", "claude-haiku-5-5")
AGENT_NAMES = ["charts", "news", "sentiment", "skeptic", "chief"]
MODELS = {a: os.getenv(f"MODEL_{a.upper()}", DEFAULT_MODEL) for a in AGENT_NAMES}

bv = Bitvavo({
    "APIKEY": os.getenv("BITVAVO_KEY", ""),
    "APISECRET": os.getenv("BITVAVO_SECRET", ""),
})
ai = anthropic.Anthropic()


# ---------- Database ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS positions(
            market TEXT PRIMARY KEY, amount REAL, entry_price REAL, cost_eur REAL, opened TEXT);
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY, ts TEXT, market TEXT, side TEXT, amount REAL,
            price REAL, eur REAL, fee REAL, pnl REAL, live INTEGER, reason TEXT);
        CREATE TABLE IF NOT EXISTS agent_log(
            id INTEGER PRIMARY KEY, ts TEXT, market TEXT, agent TEXT, output TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS benchmark(
            id INTEGER PRIMARY KEY, ts TEXT, btc_price REAL, btc_hold_eur REAL, bot_eur REAL);
        """)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_agent(market, agent, output):
    with db() as con:
        con.execute("INSERT INTO agent_log(ts,market,agent,output) VALUES(?,?,?,?)",
                    (now(), market, agent, json.dumps(output, ensure_ascii=False)))


# ---------- Marktdata ----------
def price(market):
    return float(bv.tickerPrice({"market": market})["price"])


def spread_pct(market):
    """Spread tussen beste bid en ask in %. None als het orderboek niet te lezen is."""
    try:
        book = bv.book(market, {"depth": 1})
        bid = float(book["bids"][0][0])
        ask = float(book["asks"][0][0])
        return (ask - bid) / ((ask + bid) / 2) * 100
    except Exception:
        return None


def rsi(closes, n=14):
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def indicators(market):
    raw = bv.candles(market, "1h", {"limit": 200})
    if not isinstance(raw, list) or len(raw) < 60:
        raise RuntimeError(f"Te weinig candles voor {market}: {raw}")
    raw = list(reversed(raw))  # oudste eerst
    closes = [float(c[4]) for c in raw]
    vols = [float(c[5]) for c in raw]
    last = closes[-1]
    return {
        "market": market,
        "price": last,
        "rsi14": round(rsi(closes), 1),
        "sma20": round(sum(closes[-20:]) / 20, 2),
        "sma50": round(sum(closes[-50:]) / 50, 2),
        "change_24h_pct": round((last / closes[-25] - 1) * 100, 2),
        "change_7d_pct": round((last / closes[-169] - 1) * 100, 2) if len(closes) > 169 else None,
        "volume_vs_24h_avg": round(vols[-1] / (sum(vols[-24:]) / 24), 2),
    }


FEEDS = ["https://www.coindesk.com/arc/outboundfeeds/rss/",
         "https://cointelegraph.com/rss"]


def headlines(limit=15):
    items = []
    for url in FEEDS:
        try:
            for e in feedparser.parse(url).entries[:8]:
                items.append(e.title)
        except Exception:
            pass
    return items[:limit]


def fear_greed():
    try:
        d = requests.get("https://api.alternative.me/fng/?limit=3", timeout=10).json()["data"]
        return [{"value": int(x["value"]), "label": x["value_classification"]} for x in d]
    except Exception:
        return None


# ---------- Agents ----------
JSON_RULE = " Antwoord ALLEEN met geldig JSON, geen uitleg eromheen."

PROMPTS = {
    "charts": "Je bent de Charts-agent: technisch analist voor crypto. Je krijgt indicatoren voor "
              "1-uurs candles. Geef JSON: {\"richting\": \"omhoog|omlaag|neutraal\", \"score\": -1..1, "
              "\"onderbouwing\": \"max 2 zinnen\"}." + JSON_RULE,
    "news": "Je bent de News-agent. Je krijgt recente crypto-koppen en een markt. Beoordeel of er "
            "nieuws is dat deze munt de komende uren/dagen raakt. Geef JSON: {\"impact\": "
            "\"positief|negatief|geen\", \"score\": -1..1, \"onderbouwing\": \"max 2 zinnen\"}. "
            "Bij geen relevant nieuws: score 0." + JSON_RULE,
    "sentiment": "Je bent de Sentiment-agent. Je krijgt de Fear & Greed Index (laatste 3 dagen). "
                 "Contrair denken is toegestaan: extreme angst kan een koopkans zijn, extreme hebzucht "
                 "een waarschuwing. Geef JSON: {\"stemming\": \"...\", \"score\": -1..1, "
                 "\"onderbouwing\": \"max 2 zinnen\"}." + JSON_RULE,
    "skeptic": "Je bent de Skeptic. Je krijgt de rapporten van de andere agents en moet het sterkste "
               "argument vinden om NIET te handelen. Wees kritisch op zwakke onderbouwing en op "
               "tegenstrijdigheden. Geef JSON: {\"risico\": \"laag|midden|hoog\", \"sterkste_bezwaar\": "
               "\"max 2 zinnen\"}." + JSON_RULE,
    "chief": "Je bent de Chief: je beslist over een kleine portefeuille (ongeveer 100 euro, posities van hooguit 20 euro). "
             "Je krijgt de rapporten van Charts, News, Sentiment en Skeptic, plus de huidige positie. "
             "Kies buy, sell of hold. 'hold' is de standaard: handel alleen bij een duidelijke edge en "
             "als de Skeptic geen zwaarwegend bezwaar heeft. Een kleine portefeuille kan zich geen "
             "dure foutjes veroorloven. Stop-loss en koersdoel worden automatisch gezet. "
             "Geef JSON: {\"action\": \"buy|sell|hold\", \"amount_eur\": getal "
             "(alleen bij buy), \"confidence\": 0..1, \"reason\": \"max 3 zinnen\"}." + JSON_RULE,
}


def ask(agent, payload):
    try:
        r = ai.messages.create(
            model=MODELS[agent], max_tokens=500, system=PROMPTS[agent],
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        kosten.log_usage(agent, MODELS[agent], r.usage, DB_PATH)
        text = r.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        return json.loads(text)
    except Exception as e:
        return {"fout": str(e), "score": 0}


# ---------- Posities en risicopoort ----------
def get_positions():
    with db() as con:
        return {r["market"]: dict(r) for r in con.execute("SELECT * FROM positions")}


def risk_check_buy(market, amount_eur, confidence):
    """Geeft (toegestaan_bedrag, reden). Regels staan in rules.json, de AI kan ze niet overrulen."""
    with db() as con:
        res = risk_gate.check_buy(con, market, amount_eur, confidence, spread_pct(market), rules=RULES)
        risk_gate.log_verdict(con, market, res)
    if res["verdict"] == "BLOCK":
        return 0, "; ".join(res["reasons"])
    plan = risk_gate.exit_plan(RULES, market, price(market))
    print(f"  PASS €{res['amount_eur']:.2f} (grens: {res['decider']}, risico €{res['risk_eur']:.2f}) "
          f"stop {plan['stop_price']:.4f} doel {plan['target_price']:.4f}")
    return res["amount_eur"], "ok"


# ---------- Orders ----------
def floor8(x):
    return math.floor(x * 1e8) / 1e8


def execute_buy(market, eur, reason):
    px = price(market)
    if LIVE:
        res = bv.placeOrder(market, "buy", "market", {"amountQuote": f"{eur:.2f}"})
        if "errorCode" in res:
            print("  BUY mislukt:", res)
            return
        amount = float(res["filledAmount"])
        spent = float(res["filledAmountQuote"])
        fee = float(res.get("feePaid", 0) or 0)
        fill_px = spent / amount if amount else px
    else:
        fee = eur * SIM_FEE
        amount = (eur - fee) / px
        spent, fill_px = eur, px
    with db() as con:
        con.execute("INSERT INTO positions VALUES(?,?,?,?,?)",
                    (market, amount, fill_px, spent, now()))
        con.execute("INSERT INTO trades(ts,market,side,amount,price,eur,fee,pnl,live,reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (now(), market, "buy", amount, fill_px, spent, fee, 0, int(LIVE), reason))
    print(f"  {'LIVE' if LIVE else 'DROOG'} BUY {market}: EUR {spent:.2f} @ {fill_px:.2f}")


def execute_sell(market, reason):
    pos = get_positions().get(market)
    if not pos:
        return
    px = price(market)
    amount = pos["amount"]
    if LIVE:
        base = market.split("-")[0]
        bal = bv.balance({"symbol": base})
        avail = float(bal[0]["available"]) if isinstance(bal, list) and bal else 0.0
        amount = floor8(min(amount, avail))
        if amount <= 0:
            print(f"  SELL {market}: geen saldo beschikbaar, positie blijft staan")
            return
        res = bv.placeOrder(market, "sell", "market", {"amount": f"{amount:.8f}"})
        if "errorCode" in res:
            print("  SELL mislukt:", res)
            return
        proceeds = float(res["filledAmountQuote"])
        fee = float(res.get("feePaid", 0) or 0)
        fill_px = proceeds / float(res["filledAmount"])
        net = proceeds - fee
    else:
        gross = amount * px
        fee = gross * SIM_FEE
        net, fill_px = gross - fee, px
    pnl = net - pos["cost_eur"]
    with db() as con:
        con.execute("DELETE FROM positions WHERE market=?", (market,))
        con.execute("INSERT INTO trades(ts,market,side,amount,price,eur,fee,pnl,live,reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (now(), market, "sell", amount, fill_px, net, fee, pnl, int(LIVE), reason))
    print(f"  {'LIVE' if LIVE else 'DROOG'} SELL {market}: EUR {net:.2f} (resultaat {pnl:+.2f})")


def check_exits():
    """Stop-loss en koersdoel. Verkopen wordt nooit geblokkeerd."""
    for market, pos in get_positions().items():
        why = risk_gate.exit_signal(RULES, market, pos["entry_price"], price(market))
        if why:
            print(f"[exit] {market}: {why}")
            execute_sell(market, why)


# ---------- Eén analyse-cyclus ----------
def run_cycle():
    print(f"\n=== Cyclus {now()} ({'LIVE' if LIVE else 'droog'}) ===")
    news = headlines()
    fg = fear_greed()
    sentiment = ask("sentiment", {"fear_greed": fg})
    log_agent("*", "sentiment", sentiment)

    for market in MARKETS:
        try:
            ind = indicators(market)
        except Exception as e:
            print(f"{market}: data-fout {e}")
            continue
        pos = get_positions().get(market)
        charts = ask("charts", ind)
        news_r = ask("news", {"market": market, "headlines": news})
        reports = {"charts": charts, "news": news_r, "sentiment": sentiment}
        skeptic = ask("skeptic", {"market": market, "indicatoren": ind, "rapporten": reports,
                                  "positie": pos})
        decision = ask("chief", {"market": market, "indicatoren": ind, "rapporten": reports,
                                 "skeptic": skeptic, "positie": pos})
        for name, out in [("charts", charts), ("news", news_r), ("skeptic", skeptic),
                          ("chief", decision)]:
            log_agent(market, name, out)

        action = decision.get("action", "hold")
        print(f"{market}: chief={action} conf={decision.get('confidence')} "
              f"- {decision.get('reason')}")

        if action == "buy" and not pos:
            amount, why = risk_check_buy(market, float(decision.get("amount_eur", 0) or 0),
                                         float(decision.get("confidence", 0) or 0))
            if amount:
                execute_buy(market, amount, decision.get("reason", ""))
            else:
                print(f"  risicopoort blokkeert: {why}")
        elif action == "sell" and pos:
            execute_sell(market, decision.get("reason", ""))


def meta_get(key):
    with db() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def meta_set(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))


def summary():
    pos = get_positions()
    unrealized = 0.0
    total = 0.0
    for m, p in pos.items():
        v = p["amount"] * price(m)
        total += v
        unrealized += v - p["cost_eur"]
        print(f"  {m}: {p['amount']:.6f} ingekocht voor EUR {p['cost_eur']:.2f}, nu EUR {v:.2f}")
    with db() as con:
        pnl = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='sell'").fetchone()[0]
    print(f"  Gerealiseerd totaal: EUR {pnl:+.2f} | open posities waarde: EUR {total:.2f}")

    # Benchmark: wat zou BUDGET_EUR in BTC kopen-en-vasthouden nu waard zijn?
    try:
        btc = price("BTC-EUR")
        start = meta_get("btc_start_price")
        if start is None:
            meta_set("btc_start_price", btc)
            meta_set("bench_start", now())
            start = btc
        btc_hold = BUDGET_EUR * btc / float(start)
        bot_eur = BUDGET_EUR + pnl + unrealized
        with db() as con:
            con.execute("INSERT INTO benchmark(ts,btc_price,btc_hold_eur,bot_eur) VALUES(?,?,?,?)",
                        (now(), btc, btc_hold, bot_eur))
        print(f"  Benchmark: bot EUR {bot_eur:.2f} vs BTC vasthouden EUR {btc_hold:.2f} "
              f"(verschil {bot_eur - btc_hold:+.2f})")
    except Exception as e:
        print("  Benchmark niet bijgewerkt:", e)


def main():
    init_db()
    if LIVE and not os.getenv("BITVAVO_KEY"):
        raise SystemExit("LIVE=true maar BITVAVO_KEY ontbreekt in .env")
    print(f"Regels: max €{RULES['max_loss_per_trade_eur']} verlies per trade, max "
          f"{RULES['max_positions']} posities, daglimiet €{RULES['daily_loss_limit_eur']}, "
          f"doel {RULES['reward_risk']}R ({'LIVE' if LIVE else 'droog draaien'})")
    last_cycle = 0.0
    while True:
        if Path("STOP").exists():
            print("STOP-bestand gevonden, bot stopt. Open posities blijven staan.")
            break
        try:
            check_exits()
            if time.time() - last_cycle >= CYCLE_MINUTES * 60:
                run_cycle()
                summary()
                last_cycle = time.time()
        except Exception as e:
            print("Fout in loop:", e)
        time.sleep(EXIT_CHECK_SECONDS)


if __name__ == "__main__":
    main()
