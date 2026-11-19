"""Risicopoort en positiegrootte voor de Bitvavo-bot.

Idee overgenomen uit Jakeschincariol/day-trader-skill (trader-risk en
trader-budget, MIT-licentie), omgezet naar crypto in euro's:

- De stop-loss bepaalt de grootte: hoe verder de stop, hoe kleiner de positie,
  zodat één trade nooit meer kost dan MAX_LOSS_PER_TRADE_EUR.
- Elke aankoop krijgt PASS of BLOCK met reden. Een BLOCK is definitief.
- Verkopen wordt nooit geblokkeerd.
- Elke aankoop krijgt vooraf een stop en een koersdoel (standaard 2R).

Alle regels staan in rules.json. De AI kan hier niets aan veranderen.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Eerst naast waar je de bot start (werkt ook voor trader.exe), anders naast dit bestand.
RULES_PATH = Path("rules.json") if Path("rules.json").exists() else Path(__file__).with_name("rules.json")

DEFAULT_RULES = {
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
    "short_borrow_pct_day": 0.05,
}


def load_rules(path=RULES_PATH):
    rules = dict(DEFAULT_RULES)
    p = Path(path)
    if p.exists():
        rules.update(json.loads(p.read_text(encoding="utf-8")))
    return rules


def stop_pct(rules, market):
    sp = rules["stop_pct"]
    return float(sp.get(market, sp.get("default", 6)))


# Twee "boeken": long (echt kopen/verkopen) en short (alleen simulatie).
BOOKS = {
    "long": {"positions": "positions", "trades": "trades", "open": "buy", "close": "sell"},
    "short": {"positions": "short_positions", "trades": "short_trades", "open": "short", "close": "cover"},
}


def exit_plan(rules, market, entry_price, side="long"):
    """Stop en koersdoel vóór de instap. Doel = reward_risk x de stopafstand.
    Bij een short ligt de stop boven de instap en het doel eronder."""
    s = stop_pct(rules, market) / 100
    if side == "short":
        return {
            "stop_price": entry_price * (1 + s),
            "target_price": entry_price * (1 - s * rules["reward_risk"]),
            "stop_pct": s * 100,
            "target_pct": s * rules["reward_risk"] * 100,
        }
    return {
        "stop_price": entry_price * (1 - s),
        "target_price": entry_price * (1 + s * rules["reward_risk"]),
        "stop_pct": s * 100,
        "target_pct": s * rules["reward_risk"] * 100,
    }


def risk_per_eur(rules, market):
    """Verlies per ingelegde euro als de stop geraakt wordt, inclusief fees in en uit."""
    return stop_pct(rules, market) / 100 + 2 * rules["fee_pct"] / 100


def size_buy(rules, market, wanted_eur, invested_eur, realized_eur=0.0):
    """Grootste bedrag binnen vier grenzen. Geeft (bedrag, welke grens besliste).
    realized_eur: totaal gerealiseerd resultaat; verlies verkleint het beschikbare budget."""
    # Met hefboom (Kraken) is de positie groter dan het onderpand. Het verlies per trade blijft begrensd
    # door "max verlies per trade", want dat rekent met de stop-afstand over de hele positie.
    lev = max(1, int(rules.get("leverage", 1) or 1))
    limits = {
        "Chief-voorstel": wanted_eur,
        "max per trade": rules["max_trade_eur"] * lev,
        "budgetruimte": (rules["budget_eur"] + min(0.0, realized_eur)) * lev - invested_eur,
        "max verlies per trade": rules["max_loss_per_trade_eur"] / risk_per_eur(rules, market),
    }
    decider = min(limits, key=limits.get)
    return max(0.0, round(limits[decider], 2)), decider


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_stats(con, rules, book="long"):
    """Gerealiseerd resultaat, aantal instappen vandaag en open risico (UTC-dag)."""
    b = BOOKS[book]
    day = _today_utc() + "%"
    realized = con.execute(
        f"SELECT COALESCE(SUM(pnl),0) FROM {b['trades']} WHERE side=? AND ts LIKE ? AND COALESCE(schaduw,0)=0", (b["close"], day)
    ).fetchone()[0]
    buys = con.execute(
        f"SELECT COUNT(*) FROM {b['trades']} WHERE side=? AND ts LIKE ? AND COALESCE(schaduw,0)=0", (b["open"], day)
    ).fetchone()[0]
    open_risk = 0.0
    for market, cost in con.execute(f"SELECT market, cost_eur FROM {b['positions']} WHERE COALESCE(schaduw,0)=0"):
        open_risk += cost * risk_per_eur(rules, market)
    return realized, buys, open_risk


def copy_day(con):
    """Kopieer-modus vandaag: gerealiseerd resultaat en open risico (tot de noodstop, plus kosten)."""
    try:
        realized = con.execute("SELECT COALESCE(SUM(pnl_eur),0) FROM copy_trades WHERE closed LIKE ?",
                               (_today_utc() + "%",)).fetchone()[0]
        open_risk = 0.0
        for cost, entry, stop in con.execute("SELECT cost_eur, entry_price, stop_price FROM copy_positions"):
            if entry:
                open_risk += cost * (abs(entry - stop) / entry + 0.001)
        return realized, open_risk
    except sqlite3.OperationalError:
        return 0.0, 0.0


def day_risk_all(con, rules):
    """Eén daglimiet voor alles: (verlies vandaag, open risico) van gewone bot (long + short) en kopiëren samen."""
    realized = open_risk = 0.0
    for book in BOOKS:
        try:
            r, _b, o = day_stats(con, rules, book)
            realized += r
            open_risk += o
        except sqlite3.OperationalError:
            pass
    cr, co = copy_day(con)
    return max(0.0, -(realized + cr)), open_risk + co


def last_stop_loss(con, market, book="long"):
    b = BOOKS[book]
    r = con.execute(
        f"SELECT ts FROM {b['trades']} WHERE market=? AND side=? AND reason LIKE 'stop-loss%' "
        "AND COALESCE(schaduw,0)=0 ORDER BY id DESC LIMIT 1", (market, b["close"])
    ).fetchone()
    return datetime.fromisoformat(r[0]) if r else None


def check_buy(con, market, wanted_eur, confidence, spread, stop_file="STOP", rules=None, book="long"):
    """PASS of BLOCK voor een aankoop.

    con: sqlite3-verbinding met de tabellen positions en trades van bot.py.
    spread: spread in % (None = orderboek onleesbaar).
    Geeft dict: verdict, amount_eur, reasons, decider, plan-info.
    """
    rules = rules or load_rules()
    reasons = []
    b = BOOKS[book]
    # Schaduwtrades kosten geen echt geld: ze tellen niet mee voor budget, limieten en aantal posities.
    every = {m for (m,) in con.execute(f"SELECT market FROM {b['positions']}")}
    positions = {m: c for m, c in con.execute(
        f"SELECT market, cost_eur FROM {b['positions']} WHERE COALESCE(schaduw,0)=0")}
    invested = sum(positions.values())

    if Path(stop_file).exists():
        reasons.append("STOP-bestand aanwezig")
    if market in rules["blocked_markets"]:
        reasons.append(f"{market} staat op de blokkeerlijst")
    if market in every:
        reasons.append("al een positie in deze markt")
    if len(positions) >= rules["max_positions"]:
        reasons.append(f"maximaal {rules['max_positions']} posities bereikt")
    if confidence < rules["min_confidence"]:
        reasons.append(f"confidence {confidence:.2f} onder {rules['min_confidence']}")
    if spread is None:
        reasons.append("spread niet te bepalen")
    elif spread > rules["max_spread_pct"]:
        reasons.append(f"spread {spread:.2f}% boven {rules['max_spread_pct']}%")

    realized, buys, open_risk = day_stats(con, rules, book)
    if buys >= rules["max_buys_per_day"]:
        what = "shorts" if book == "short" else "aankopen"
        reasons.append(f"al {buys} {what} vandaag (max {rules['max_buys_per_day']})")

    last = last_stop_loss(con, market, book)
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        wait = last + timedelta(hours=rules["cooldown_hours_after_stop"])
        if datetime.now(timezone.utc) < wait:
            reasons.append(f"afkoelperiode na stop-loss tot {wait:%H:%M} UTC")

    realized_total = 0.0
    for bk in BOOKS.values():
        try:
            realized_total += con.execute(f"SELECT COALESCE(SUM(pnl),0) FROM {bk['trades']} WHERE side=? AND COALESCE(schaduw,0)=0",
                                          (bk["close"],)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    amount, decider = size_buy(rules, market, wanted_eur, invested, realized_total)
    new_risk = amount * risk_per_eur(rules, market)
    # Eén daglimiet voor alles: ook de andere kant (long/short) en de kopieer-modus tellen mee.
    loss_so_far, open_risk = day_risk_all(con, rules)
    if loss_so_far + open_risk + new_risk > rules["daily_loss_limit_eur"]:
        reasons.append(
            f"dagrisico te hoog: verlies vandaag €{loss_so_far:.2f} + open risico "
            f"€{open_risk:.2f} + deze trade €{new_risk:.2f} > €{rules['daily_loss_limit_eur']}"
        )
    if amount < rules["min_order_eur"]:
        if decider == "max verlies per trade":
            reasons.append(
                f"stop van {stop_pct(rules, market)}% te ver voor max verlies "
                f"€{rules['max_loss_per_trade_eur']} (zou €{amount:.2f} worden)"
            )
        else:
            reasons.append(f"bedrag €{amount:.2f} onder minimum (grens: {decider})")

    return {
        "verdict": "BLOCK" if reasons else "PASS",
        "amount_eur": 0.0 if reasons else amount,
        "decider": decider,
        "risk_eur": round(new_risk, 2),
        "reasons": reasons,
    }


def exit_signal(rules, market, entry_price, price, side="long"):
    """Reden om te verkopen (stop of doel geraakt), anders None. Verkopen mag altijd."""
    plan = exit_plan(rules, market, entry_price, side)
    change = (price / entry_price - 1) * 100
    if side == "short":
        # Voor een short is een koersdaling winst.
        if price >= plan["stop_price"]:
            return f"stop-loss bij koers {change:+.1f}%"
        if price <= plan["target_price"]:
            return f"koersdoel bij koers {change:+.1f}% ({rules['reward_risk']}R)"
        return None
    if price <= plan["stop_price"]:
        return f"stop-loss bij {change:.1f}%"
    if price >= plan["target_price"]:
        return f"koersdoel bij +{change:.1f}% ({rules['reward_risk']}R)"
    return None


def book_stats(con, book="long", bron=None):
    """Losse statistieken per boek (en eventueel per bron): gesloten trades, winrate, totaal, beste/slechtste.
    bron: 'regels', 'volgers' of 'beide' (None = alles)."""
    b = BOOKS[book]
    q, args = f"SELECT pnl FROM {b['trades']} WHERE side=?", [b["close"]]
    if bron:
        q += " AND COALESCE(bron,'regels')=?"
        args.append(bron)
    try:
        pnls = [r[0] or 0.0 for r in con.execute(q, args)]
    except sqlite3.OperationalError:
        pnls = []
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    q2, args2 = f"SELECT COUNT(*) FROM {b['positions']}", []
    if bron:
        q2 += " WHERE COALESCE(bron,'regels')=?"
        args2.append(bron)
    try:
        n_open = con.execute(q2, args2).fetchone()[0]
    except sqlite3.OperationalError:
        n_open = 0
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "winrate": round(len(wins) / len(pnls), 3) if pnls else None,
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "best": round(max(pnls), 2) if pnls else None,
        "worst": round(min(pnls), 2) if pnls else None,
        "open_positions": n_open,
    }


def log_verdict(con, market, result, book="long"):
    con.execute(
        "CREATE TABLE IF NOT EXISTS risk_log(id INTEGER PRIMARY KEY, ts TEXT, market TEXT, "
        "verdict TEXT, amount_eur REAL, risk_eur REAL, decider TEXT, reasons TEXT, book TEXT DEFAULT 'long')"
    )
    con.execute(
        "INSERT INTO risk_log(ts,market,verdict,amount_eur,risk_eur,decider,reasons,book) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), market, result["verdict"],
         result["amount_eur"], result["risk_eur"], result["decider"],
         "; ".join(result["reasons"]), book),
    )


if __name__ == "__main__":
    # Snel overzicht: hoe groot mag een trade per munt worden met deze regels?
    r = load_rules()
    print(f"Max verlies per trade: €{r['max_loss_per_trade_eur']}, doel {r['reward_risk']}R\n")
    for m in ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR"]:
        amt, why = size_buy(r, m, 999, 0)
        p = exit_plan(r, m, 100)
        print(f"{m}: max €{amt:.2f} ({why}), stop -{p['stop_pct']:.0f}%, doel +{p['target_pct']:.0f}%")
