"""Houdt het echte tokenverbruik van de Claude API bij in trader.db.

Gebruikt alleen de 'usage' die de API al bij elk antwoord meestuurt:
geen extra aanroepen, geen extra kosten.

Overzicht bekijken:  python kosten.py
"""
import sqlite3
import sys
from datetime import datetime, timezone

try:
    import settings
    DB_PATH = settings.db_path()
except Exception:
    DB_PATH = "trader.db"

# Prijzen in dollar per miljoen tokens (input, output), prompts tot 100.000 tokens.
# Controleer af en toe op https://platform.claude.com/docs/en/about-claude/pricing
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def _cost(model, tin, tout):
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return tin / 1e6 * pin + tout / 1e6 * pout


def log_usage(agent, model, usage, db_path=DB_PATH):
    """Roep aan na elke ai.messages.create(). Fouten hier stoppen de bot nooit."""
    try:
        tin = int(getattr(usage, "input_tokens", 0) or 0)
        tout = int(getattr(usage, "output_tokens", 0) or 0)
        with sqlite3.connect(db_path) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS api_usage(id INTEGER PRIMARY KEY, ts TEXT, agent TEXT, "
                "model TEXT, input_tokens INTEGER, output_tokens INTEGER, usd REAL)"
            )
            con.execute(
                "INSERT INTO api_usage(ts,agent,model,input_tokens,output_tokens,usd) VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), agent, model,
                 tin, tout, _cost(model, tin, tout)),
            )
    except Exception as e:
        print("  (tokenlog mislukt:", e, ")")


def report(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    try:
        first, last, n, usd = con.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*), COALESCE(SUM(usd),0) FROM api_usage"
        ).fetchone()
    except sqlite3.OperationalError:
        print("Nog geen gegevens. Laat de bot eerst een cyclus draaien.")
        return
    if not n:
        print("Nog geen gegevens. Laat de bot eerst een cyclus draaien.")
        return

    print(f"{'agent':<10}{'aanroepen':>10}{'gem. in':>9}{'gem. uit':>9}{'max in':>8}{'kosten $':>10}")
    for row in con.execute(
        "SELECT agent, COUNT(*), AVG(input_tokens), AVG(output_tokens), MAX(input_tokens), SUM(usd) "
        "FROM api_usage GROUP BY agent ORDER BY SUM(usd) DESC"
    ):
        a, c, ai, ao, mi, u = row
        print(f"{a:<10}{c:>10}{ai:>9.0f}{ao:>9.0f}{mi:>8}{u:>10.4f}")

    hours = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() / 3600
    print(f"\nTotaal: {n} aanroepen, ${usd:.4f} sinds {first[:16].replace('T', ' ')} UTC")
    if hours >= 1:
        print(f"Verwachting per maand bij dit tempo: ${usd / hours * 24 * 30:.2f}")
    else:
        print("Draai minstens een paar uur voor een betrouwbare maandschatting.")


if __name__ == "__main__":
    report(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
