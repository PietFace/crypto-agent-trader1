"""Backtest: de vaste regels nadoen op historische uurkoersen van Bitvavo.

Wat wordt getest (precies zoals de bot, zonder AI):
- de vijf instapregels (koers/SMA20/SMA50/RSI) voor kopen en, als shorts aanstaan, het spiegelbeeld;
- de positiegrootte, stop-loss, koersdoel, daglimiet, max posities, max instappen per dag en
  de pauze na een stop-loss uit de risicopoort;
- fees en (bij shorts) de aanname voor leenkosten.

Wat NIET kan worden getest:
- De Charts-agent (AI) wordt vervangen door een rekenscore op dezelfde indicatoren (trend_score).
- Nieuws wordt als neutraal (0) aangenomen: die regel staat dus altijd 'aan'.
- De Skeptic en Chief (AI) en de volgers: hun beslissingen uit het verleden zijn er niet.
Instappen gebeurt alleen op het moment dat een setup ontstaat (zoals de snelle check).
Een kaars die zowel stop als doel raakt, telt als stop (voorzichtig gerekend).
"""
import math
import time
from datetime import datetime, timezone

import risk_gate
import trail as trailmod


# ------------------------------------------------------------------ data
def fetch_candles(bv, market, months, progress=None):
    """Uurkaarsen van de laatste `months` maanden (oudste eerst): [ts_ms, open, high, low, close, volume]."""
    end = int(time.time() * 1000)
    start = end - int(months * 30.44 * 24 * 3600 * 1000)
    out = []
    while end > start:
        raw = bv.candles(market, "1h", {"limit": 1440, "end": end})
        if isinstance(raw, dict) and "errorCode" in raw:
            raise RuntimeError(f"Bitvavo: {raw.get('error')}")
        if not raw:
            break
        rows = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in raw]
        out.extend(rows)
        oldest = min(r[0] for r in rows)
        if oldest >= end:
            break
        end = oldest - 1
        if progress:
            progress(f"{market}: {len(out)} uurkaarsen opgehaald")
        time.sleep(0.15)
    out = sorted({r[0]: r for r in out if r[0] >= start}.values())
    return out


# ------------------------------------------------------------------ indicatoren
def trend_score(price, sma20, sma50, change24):
    """Rekenvervanger van de Charts-agent: -1 (sterk dalend) tot +1 (sterk stijgend).
    Combineert afstand tot SMA20, de stand van SMA20 t.o.v. SMA50 en de verandering over 24 uur."""
    if not sma20 or not sma50:
        return 0.0
    a = math.tanh((price / sma20 - 1) / 0.02)
    b = math.tanh((sma20 / sma50 - 1) / 0.02)
    c = math.tanh(change24 / 5)
    return max(-1.0, min(1.0, 0.5 * a + 0.3 * b + 0.2 * c))


def _rsi(closes, n=14):
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + (gains / n) / (losses / n))


def setup_at(closes, i):
    """Setup volgens dezelfde regels als bot.setup_for, op kaars i."""
    import bot  # alleen voor setup_for; geen netwerk
    c = closes[max(0, i - 60): i + 1]
    price = c[-1]
    sma20 = sum(c[-20:]) / 20
    sma50 = sum(c[-50:]) / 50
    ch24 = (price / c[-25] - 1) * 100
    ind = {"price": price, "sma20": sma20, "sma50": sma50, "rsi14": _rsi(c)}
    cs = trend_score(price, sma20, sma50, ch24)
    st = bot.setup_for(ind, {"score": cs}, {"score": 0.0})
    return st, cs


# ------------------------------------------------------------------ simulatie
def run(data, rules, shorts=True, start_budget=None, progress=None, cycle_minutes=120):
    """data: {market: candles}. Speelt alle munten uur voor uur tegelijk na, zodat max posities en daglimiet
    gelden zoals bij de bot. Geeft dict met trades, equity-curve en statistieken."""
    budget = float(start_budget or rules["budget_eur"])
    # Sluiten bij omgeslagen charts beslist de bot alleen bij een analyse (standaard elke 2 uur).
    flip_every = max(1, int(round(cycle_minutes / 60)))
    fee = rules["fee_pct"] / 100
    borrow_day = float(rules.get("short_borrow_pct_day", 0.05)) / 100
    markets = [m for m, c in data.items() if len(c) > 80]
    idx = {m: {c[0]: k for k, c in enumerate(data[m])} for m in markets}
    closes = {m: [c[4] for c in data[m]] for m in markets}
    times = sorted(set().union(*[set(idx[m]) for m in markets])) if markets else []

    open_pos = {}      # (market, side) -> dict
    trades = []
    last_state = {}
    stop_until = {}
    day_buys, day_loss, cur_day = {}, {}, None
    realized = 0.0
    equity = []

    def risk_per_eur(m):
        return risk_gate.risk_per_eur(rules, m)

    tcfg = trailmod.conf(rules.get("trail"))
    trail_on = bool(tcfg.get("enabled"))
    watch = {}         # (market, side) -> {"entry", "count", "until"}: opnieuw instappen na vastgezette winst

    def try_open(m, side, k, t, day, orig=None, count=0):
        if (m, side) in open_pos or t < stop_until.get((m, side), 0):
            return False
        n_side = sum(1 for (_, s) in open_pos if s == side)
        if n_side >= rules["max_positions"] or day_buys.get((day, side), 0) >= rules["max_buys_per_day"]:
            return False
        invested = sum(p["cost"] for (mm, s), p in open_pos.items() if s == side)
        amount, _ = risk_gate.size_buy(rules, m, rules["max_trade_eur"] * max(1, int(rules.get("leverage", 1) or 1)),
                                       invested, realized)
        if amount < rules["min_order_eur"]:
            return False
        open_risk = sum(p["cost"] * risk_per_eur(mm) for (mm, s), p in open_pos.items() if s == side)
        if -day_loss.get((day, side), 0.0) + open_risk + amount * risk_per_eur(m) > rules["daily_loss_limit_eur"]:
            return False
        px = closes[m][k]
        plan = risk_gate.exit_plan(rules, m, px, side)
        amt = (amount * (1 - fee)) / px if side == "long" else amount / px
        open_pos[(m, side)] = {"t": t, "entry": px, "amount": amt, "cost": amount,
                               "stop": plan["stop_price"], "target": plan["target_price"],
                               "peak": px, "trail": None, "orig": orig or px, "re": count}
        day_buys[(day, side)] = day_buys.get((day, side), 0) + 1
        return True

    for n, t in enumerate(times):
        day = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")
        if day != cur_day:
            cur_day = day
        # 1. exits op deze kaars (high/low)
        for key in list(open_pos):
            m, side = key
            k = idx[m].get(t)
            if k is None:
                continue
            _, o, h, l, c, _ = data[m][k]
            p = open_pos[key]
            exit_px, why = None, None
            # De meeschuivende stop van de vorige kaars eerst (voorzichtig: hij ligt boven de gewone stop).
            if trail_on and trailmod.hit(side, p.get("trail"), l, h):
                exit_px, why = p["trail"], "winst vastgezet"
            elif side == "long":
                if l <= p["stop"]:
                    exit_px, why = p["stop"], "stop-loss"
                elif h >= p["target"]:
                    exit_px, why = p["target"], "koersdoel"
            else:
                if h >= p["stop"]:
                    exit_px, why = p["stop"], "stop-loss"
                elif l <= p["target"]:
                    exit_px, why = p["target"], "koersdoel"
            if exit_px is None and trail_on:
                p["peak"], p["trail"] = trailmod.update(side, p["entry"], risk_gate.stop_pct(rules, m), p.get("peak"),
                                                        p.get("trail"), h, l, rules["fee_pct"], tcfg)
            if exit_px is None and k >= 60 and k % flip_every == 0:
                st, cs = setup_at(closes[m], k)
                if (side == "long" and cs <= -0.3) or (side == "short" and cs >= 0.3):
                    exit_px, why = c, "charts omgeslagen"
            if exit_px is not None:
                if side == "long":
                    pnl = p["amount"] * exit_px * (1 - fee) - p["cost"]
                else:
                    days = (t - p["t"]) / 86400000
                    pnl = p["amount"] * (p["entry"] - exit_px) - p["cost"] * fee - p["amount"] * exit_px * fee \
                        - p["cost"] * borrow_day * days
                realized += pnl
                day_loss[(day, side)] = day_loss.get((day, side), 0.0) + min(0.0, pnl)
                if why == "stop-loss":
                    stop_until[key] = t + rules["cooldown_hours_after_stop"] * 3600000
                trades.append({"market": m, "side": side, "open": p["t"], "close": t, "entry": p["entry"],
                               "exit": exit_px, "eur": p["cost"], "pnl": pnl, "reason": why,
                               "herinstap": p.get("re", 0)})
                del open_pos[key]
                if why == "winst vastgezet" and tcfg.get("reentry") and p.get("re", 0) < int(tcfg["max_reentries"]):
                    watch[key] = {"entry": p.get("orig") or p["entry"], "count": p.get("re", 0) + 1,
                                  "until": t + float(tcfg["reentry_hours"]) * 3600000}
        # 2a. opnieuw instappen na vastgezette winst: koers terug bij de instap en het signaal geldt nog
        for key in list(watch):
            m, side = key
            w = watch[key]
            k = idx[m].get(t)
            if t > w["until"]:
                del watch[key]
                continue
            if k is None or k < 60 or key in open_pos:
                continue
            st, _ = setup_at(closes[m], k)
            if st.get(side) and trailmod.near_entry(w["entry"], closes[m][k], tcfg):
                if try_open(m, side, k, t, day, orig=w["entry"], count=w["count"]):
                    del watch[key]
        # 2. nieuwe setups
        for m in markets:
            k = idx[m].get(t)
            if k is None or k < 60:
                continue
            st, cs = setup_at(closes[m], k)
            if rules.get("daily_trend"):
                # Trend op dagbasis: koers t.o.v. het gemiddelde van de laatste 20 dagen (480 uurkaarsen).
                win = closes[m][max(0, k - 479): k + 1]
                sma_d = sum(win) / len(win)
                st = dict(st)
                if closes[m][k] < sma_d:
                    st["long"] = False
                if closes[m][k] > sma_d:
                    st["short"] = False
            state = ("long" if st["long"] else "") + ("short" if st["short"] and shorts else "")
            prev = last_state.get(m, "")
            last_state[m] = state
            if not state or state == prev:
                continue
            side = "long" if st["long"] else "short"
            watch.pop((m, side), None)      # nieuwe setup: oude 'opnieuw instappen' vervalt
            try_open(m, side, k, t, day)
        # 3. equity (gerealiseerd + open tegen slotkoers)
        if n % 24 == 0 or n == len(times) - 1:
            unreal = 0.0
            for (m, side), p in open_pos.items():
                k = idx[m].get(t)
                if k is None:
                    continue
                c = closes[m][k]
                unreal += (p["amount"] * c * (1 - fee) - p["cost"]) if side == "long" else \
                    (p["amount"] * (p["entry"] - c) - p["cost"] * fee)
            equity.append((t, budget + realized + unreal))
        if progress and n % 2000 == 0:
            progress(f"Naspelen: {n * 100 // max(1, len(times))}%")

    # BTC vasthouden
    hold = []
    btc = data.get("BTC-EUR")
    if btc:
        p0 = btc[0][4]
        bidx = {c[0]: c[4] for c in btc}
        for t, _ in equity:
            if t in bidx:
                hold.append((t, budget * bidx[t] / p0))
    return {"trades": trades, "equity": equity, "hold": hold, "open": len(open_pos),
            "stats": summarize(trades), "per_market": per_market(trades),
            "start": times[0] if times else None, "end": times[-1] if times else None,
            "final": equity[-1][1] if equity else budget, "budget": budget,
            "hold_final": hold[-1][1] if hold else None, "max_drawdown": max_drawdown(equity)}


def _stats(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {"trades": len(pnls), "wins": len(wins), "losses": len(losses),
            "winrate": len(wins) / len(pnls) if pnls else None, "total": sum(pnls),
            "avg_win": sum(wins) / len(wins) if wins else None,
            "avg_loss": sum(losses) / len(losses) if losses else None,
            "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None}


def summarize(trades):
    return {"all": _stats([t["pnl"] for t in trades]),
            "long": _stats([t["pnl"] for t in trades if t["side"] == "long"]),
            "short": _stats([t["pnl"] for t in trades if t["side"] == "short"])}


def per_market(trades):
    out = {}
    for t in trades:
        out.setdefault(t["market"], []).append(t["pnl"])
    return {m: _stats(v) for m, v in sorted(out.items())}


def max_drawdown(equity):
    peak, dd = -1e18, 0.0
    for _, v in equity:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return dd


# ------------------------------------------------------------------ varianten naast elkaar
TRAIL_ON = {"enabled": True, "reentry": False}
TRAIL_RE = {"enabled": True, "reentry": True}

VARIANTS = [
    ("Zoals nu", {}),
    ("Meeschuivende stop", {"trail": TRAIL_ON}),
    ("Meeschuivend + opnieuw instappen", {"trail": TRAIL_RE}),
    ("Meeschuivend + opnieuw + doel 3x", {"trail": TRAIL_RE, "reward_risk": 3.0}),
    ("Doel 3x het risico", {"reward_risk": 3.0}),
    ("Trend op dagbasis", {"daily_trend": True}),
    ("Pauze 48 uur na stop", {"cooldown_hours_after_stop": 48}),
    ("Max 2 instappen per dag", {"max_buys_per_day": 2}),
    ("Alles samen", {"reward_risk": 3.0, "daily_trend": True, "cooldown_hours_after_stop": 48,
                     "max_buys_per_day": 2}),
]


def run_variants(data, rules, shorts=True, cycle_minutes=120, progress=None):
    """Dezelfde koersen met een paar instellingen naast elkaar. Geeft een lijst met korte uitkomsten."""
    out = []
    for i, (name, change) in enumerate(VARIANTS):
        if progress:
            progress(f"Variant {i + 1}/{len(VARIANTS)}: {name}")
        r = dict(rules)
        r.update(change)
        res = run(data, r, shorts=shorts, cycle_minutes=cycle_minutes)
        st = res["stats"]
        out.append({"name": name, "change": change, "final": round(res["final"], 2),
                    "result": round(res["final"] - res["budget"], 2),
                    "vs_hold": round(res["final"] - res["hold_final"], 2) if res["hold_final"] is not None else None,
                    "trades": st["all"]["trades"], "winrate": st["all"]["winrate"],
                    "long": round(st["long"]["total"], 2), "short": round(st["short"]["total"], 2),
                    "max_drawdown": round(res["max_drawdown"], 2)})
    return out


def variants_text(rows, months, exchange):
    """Korte tekst voor het logboek (ook op je telefoon te zien)."""
    lines = [f"Varianten-test {months} maanden ({exchange}), van beste naar slechtste:"]
    for r in sorted(rows, key=lambda r: r["final"], reverse=True):
        wr = f"{r['winrate'] * 100:.0f}%" if r["winrate"] is not None else "-"
        lines.append(f"{r['name']}: eindstand €{r['final']:.2f} ({r['result']:+.2f}), {r['trades']} trades, "
                     f"winrate {wr}, grootste daling €{r['max_drawdown']:.2f}")
    return lines
