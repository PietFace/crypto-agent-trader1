"""Meeschuivende stop en opnieuw instappen. Eén plek, zodat bot, kopiëren en backtest precies hetzelfde rekenen.

Hoe het werkt (long; short is het spiegelbeeld):
- Zolang de trade niet genoeg winst heeft, geldt alleen de gewone stop-loss.
- Staat de koers activate_frac x de stopafstand boven de instap (en ruim boven de kosten), dan gaat de
  meeschuivende stop aan. Die ligt nooit lager dan instap + alle kosten + een kleine buffer: een trade die
  daarna de stop raakt, sluit dus altijd met winst ná kosten.
- Loopt de koers verder op, dan schuift de stop mee op distance_frac x de stopafstand onder de hoogste koers.
  Hij gaat alleen omhoog, nooit terug.
- Opnieuw instappen: na het vastzetten van winst houden we de munt in de gaten. Komt de koers terug tot in de
  buurt van de oorspronkelijke instap en geldt het signaal nog, dan stappen we opnieuw in (max max_reentries
  keer per oorspronkelijke trade, binnen reentry_hours).
"""

DEFAULTS = {
    "enabled": False,
    "activate_frac": 0.5,     # aan zodra de winst de helft van de stopafstand is
    "distance_frac": 0.5,     # meeschuiven op de helft van de stopafstand onder de top
    "buffer_pct": 0.2,        # extra boven alle kosten (%)
    "spread_pct": 0.1,        # aanname voor de spread bij uitstappen (%)
    "reentry": True,
    "max_reentries": 1,
    "reentry_band_pct": 0.5,  # 'in de buurt van de instap': binnen 0,5%
    "reentry_hours": 24,
}


def conf(cfg):
    c = dict(DEFAULTS)
    c.update(cfg or {})
    return c


def cost_pct(fee_pct, c):
    """Alle kosten van in- en uitstappen plus buffer, in %."""
    return 2 * float(fee_pct) + float(c["spread_pct"]) + float(c["buffer_pct"])


def update(side, entry, stop_pct, peak, cur, hi, lo, fee_pct, cfg):
    """Nieuwe top en meeschuivende stop na een koers (of kaars hi/lo).
    stop_pct: afstand van de gewone stop in %. cur: huidige meeschuivende stop of None.
    Geeft (peak, trail_stop of None)."""
    c = conf(cfg)
    costs = cost_pct(fee_pct, c)
    act = max(float(c["activate_frac"]) * stop_pct, costs * 1.5)
    dist = float(c["distance_frac"]) * stop_pct / 100
    if side == "long":
        peak = max(peak or entry, hi)
        gain = (peak / entry - 1) * 100
        if gain < act:
            return peak, cur
        be = entry * (1 + costs / 100)
        new = max(be, peak * (1 - dist))
        return peak, max(new, cur or 0.0)
    peak = min(peak or entry, lo)
    gain = (1 - peak / entry) * 100
    if gain < act:
        return peak, cur
    be = entry * (1 - costs / 100)
    new = min(be, peak * (1 + dist))
    return peak, min(new, cur) if cur else new


def hit(side, trail_stop, price_lo, price_hi=None):
    """Is de meeschuivende stop geraakt? (long: koers <= stop; short: koers >= stop)"""
    if not trail_stop:
        return False
    if side == "long":
        return price_lo <= trail_stop
    return (price_hi if price_hi is not None else price_lo) >= trail_stop


def near_entry(entry, price, cfg):
    return abs(price / entry - 1) * 100 <= float(conf(cfg)["reentry_band_pct"])


def reason(side, trail_stop, entry):
    pct = (trail_stop / entry - 1) * 100 if side == "long" else (1 - trail_stop / entry) * 100
    return f"winst vastgezet (meeschuivende stop, {pct:+.1f}% t.o.v. instap)"
