"""Crypto Trader: venster om de Bitvavo-bot te starten, te volgen en in te stellen."""
import datetime
import os
import queue
import sqlite3
import sys
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QPlainTextEdit, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import risk_gate
import settings
from bot import Engine, fmt_px, init_db

VERSION = "1.0"

C = {
    "bg": "#0F1216", "side": "#0B0D10", "card": "#171B21", "card2": "#1E232B", "line": "#262C35",
    "text": "#E8EAED", "muted": "#8A94A6", "accent": "#F7B500", "accent2": "#FFD15C",
    "good": "#3DDC84", "bad": "#FF5A5F", "info": "#5AB0FF",
}

QSS = f"""
* {{ font-family: 'Segoe UI', 'Inter', sans-serif; color: {C['text']}; }}
QMainWindow, #page {{ background: {C['bg']}; }}
#side {{ background: {C['side']}; border-right: 1px solid {C['line']}; }}
#brand {{ font-size: 17px; font-weight: 700; }}
#brandsub {{ color: {C['muted']}; font-size: 11px; }}
QPushButton#nav {{ text-align: left; padding: 10px 14px; border: none; border-radius: 8px;
    font-size: 14px; color: {C['muted']}; background: transparent; }}
QPushButton#nav:hover {{ background: {C['card']}; color: {C['text']}; }}
QPushButton#nav:checked {{ background: {C['card2']}; color: {C['text']}; border-left: 3px solid {C['accent']}; }}
#card {{ background: {C['card']}; border: 1px solid {C['line']}; border-radius: 12px; }}
#cap {{ color: {C['muted']}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
#big {{ font-size: 28px; font-weight: 700; }}
#sub {{ color: {C['muted']}; font-size: 12px; }}
#h1 {{ font-size: 22px; font-weight: 700; }}
#hint {{ color: {C['muted']}; font-size: 12px; }}
QPushButton {{ background: {C['card2']}; border: 1px solid {C['line']}; border-radius: 8px;
    padding: 8px 14px; font-size: 13px; }}
QPushButton:hover {{ border-color: {C['accent']}; }}
QPushButton#primary {{ background: {C['accent']}; color: #15100A; font-weight: 700; border: none; padding: 10px 22px; font-size: 14px; }}
QPushButton#primary:hover {{ background: {C['accent2']}; }}
QPushButton#danger {{ background: transparent; border: 1px solid {C['bad']}; color: {C['bad']}; font-weight: 600; padding: 10px 18px; }}
QTextEdit {{ background: {C['card']}; border: 1px solid {C['line']}; border-radius: 12px; padding: 8px; font-size: 13px; }}
QGroupBox {{ background: {C['card']}; border: 1px solid {C['line']}; border-radius: 12px; margin-top: 22px; padding: 16px 14px 12px 14px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 4px; top: 0px; color: {C['accent2']}; font-size: 13px; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background: {C['bg']}; border: 1px solid {C['line']}; border-radius: 6px; padding: 5px 8px; min-height: 20px; }}
QComboBox QAbstractItemView {{ background: {C['card2']}; selection-background-color: {C['accent']}; selection-color: #000; }}
QCheckBox {{ spacing: 8px; }}
QScrollArea {{ border: none; background: transparent; }}
QTableWidget {{ background: {C['card']}; border: 1px solid {C['line']}; border-radius: 10px; gridline-color: {C['line']}; font-size: 12px; }}
QHeaderView::section {{ background: {C['card2']}; color: {C['muted']}; border: none; padding: 6px; font-size: 11px; font-weight: 600; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {C['muted']}; padding: 8px 14px; font-size: 13px; }}
QTabBar::tab:selected {{ color: {C['text']}; border-bottom: 2px solid {C['accent']}; }}
#badge_dry {{ background: #12243A; color: {C['info']}; border-radius: 6px; padding: 4px 10px; font-weight: 700; font-size: 12px; }}
#badge_live {{ background: #3A1416; color: {C['bad']}; border-radius: 6px; padding: 4px 10px; font-weight: 700; font-size: 12px; }}
#banner {{ background: #2A2112; border: 1px solid #5C4313; border-radius: 10px; color: {C['accent2']}; padding: 8px 12px; }}
"""

LOG_COLORS = {"good": C["good"], "bad": C["bad"], "info": C["info"], "muted": C["muted"], "head": C["accent2"]}


def eur(x, sign=False):
    s = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return "€ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def lab(text="", name=None, wrap=False):
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    return w


def card():
    f = QFrame()
    f.setObjectName("card")
    return f


def query(sql, args=()):
    try:
        con = sqlite3.connect(settings.db_path())
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionMode(QTableWidget.NoSelection)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill(t, rows):
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))


def local_time(ts):
    try:
        return datetime.datetime.fromisoformat(ts).astimezone().strftime("%d-%m %H:%M")
    except Exception:
        return ts or ""


class StatTile(QFrame):
    def __init__(self, caption):
        super().__init__()
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.addWidget(lab(caption.upper(), "cap"))
        self.value = lab("–", "big")
        self.sub = lab("", "sub", wrap=True)
        v.addWidget(self.value)
        v.addWidget(self.sub)

    def set(self, value, sub="", color=None):
        self.value.setText(value)
        self.value.setStyleSheet(f"color: {color};" if color else "")
        self.sub.setText(sub)


# ======================================================================
class BotPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(14)

        top = QHBoxLayout()
        top.addWidget(lab("Bot", "h1"))
        self.badge = lab("DROOG DRAAIEN", "badge_dry")
        top.addWidget(self.badge)
        top.addStretch(1)
        self.next_lbl = lab("", "sub")
        top.addWidget(self.next_lbl)
        self.btn = QPushButton("Start bot")
        self.btn.setObjectName("primary")
        self.btn.clicked.connect(win.toggle)
        top.addWidget(self.btn)
        v.addLayout(top)

        self.banner = lab("", "banner", wrap=True)
        self.banner.hide()
        v.addWidget(self.banner)

        g = QGridLayout()
        g.setSpacing(12)
        self.t_bot = StatTile("Bot nu")
        self.t_btc = StatTile("BTC vasthouden")
        self.t_today = StatTile("Resultaat vandaag")
        self.t_cost = StatTile("API-kosten 30 dagen")
        for i, t in enumerate([self.t_bot, self.t_btc, self.t_today, self.t_cost]):
            g.addWidget(t, 0, i)
        v.addLayout(g)

        pos_card = card()
        pv = QVBoxLayout(pos_card)
        pv.setContentsMargins(14, 12, 14, 12)
        pv.addWidget(lab("OPEN POSITIES", "cap"))
        self.pos = table(["Soort", "Munt", "Ingelegd", "Instapkoers", "Koers nu", "Waarde nu", "Resultaat",
                          "Stop-loss", "Koersdoel", "Sinds"])
        self.pos.setMaximumHeight(150)
        pv.addWidget(self.pos)
        v.addWidget(pos_card)

        v.addWidget(lab("LOGBOEK", "cap"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        v.addWidget(self.log, 1)

    def add_log(self, text, kind):
        color = LOG_COLORS.get(kind, C["text"])
        t = datetime.datetime.now().strftime("%H:%M")
        safe = text.replace("&", "&amp;").replace("<", "&lt;")
        weight = "font-weight:600;" if kind == "head" else ""
        self.log.append(f'<span style="color:{C["muted"]}">{t}</span>&nbsp; '
                        f'<span style="color:{color};{weight}">{safe}</span>')
        self.log.moveCursor(QTextCursor.End)

    def refresh(self):
        cfg = self.win.cfg
        rules = cfg["rules"]
        budget = float(rules["budget_eur"])
        kr = cfg.get("exchange") == "kraken"
        live = bool(cfg.get("live")) and (not kr or bool(cfg["keys"].get("kraken_key")))
        where = "KRAKEN" if kr else "BITVAVO"
        self.badge.setText(f"LIVE {where}: ECHTE ORDERS" if live else (f"{where} · DROOG"))
        self.badge.setObjectName("badge_live" if live else "badge_dry")
        self.badge.setStyleSheet(self.badge.styleSheet())
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

        running = self.win.engine is not None and self.win.engine.running
        self.btn.setText("Stop bot" if running else "Start bot")
        self.btn.setObjectName("danger" if running else "primary")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        eng = self.win.engine
        if running and eng.busy:
            self.next_lbl.setText("Agents zijn aan het analyseren…")
        elif running and eng.next_cycle:
            self.next_lbl.setText("Volgende analyse om "
                                  + datetime.datetime.fromtimestamp(eng.next_cycle).strftime("%H:%M"))
        else:
            self.next_lbl.setText("")

        b = query("SELECT bot_eur, btc_hold_eur, ts FROM benchmark ORDER BY id DESC LIMIT 1")
        meta = dict(query("SELECT key, value FROM meta WHERE key IN ('btc_price_now','btc_start_price','marks_ts')"))
        if meta.get("marks_ts"):
            realized = (query("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='sell' AND COALESCE(schaduw,0)=0") or [[0]])[0][0]
            open_pnl = (query("SELECT COALESCE(SUM(m.pnl_eur),0) FROM position_marks m JOIN positions p "
                              "ON p.market=m.market WHERE m.book='long' AND COALESCE(p.schaduw,0)=0") or [[0]])[0][0]
            live_bot = budget + realized + open_pnl
            live_hold = b[0][1] if b else None
            try:
                live_hold = budget * float(meta["btc_price_now"]) / float(meta["btc_start_price"])
            except Exception:
                pass
            b = [(live_bot, live_hold, meta["marks_ts"])]
        if b:
            bot_eur, hold, ts = b[0]
            self.t_bot.set(eur(bot_eur), f"{eur(bot_eur - budget, True)} sinds de start · live {local_time(ts)}",
                           C["good"] if bot_eur >= budget else C["bad"])
            if hold is not None:
                diff = bot_eur - hold
                self.t_btc.set(eur(hold), f"bot {eur(diff, True)} t.o.v. BTC", None)
            else:
                self.t_btc.set("–", "start na de eerste analyse")
        else:
            self.t_bot.set(eur(budget), "nog geen analyse gedraaid")
            self.t_btc.set("–", "start na de eerste analyse")

        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d") + "%"
        pnl = query("SELECT COALESCE(SUM(pnl),0), COUNT(*) FROM trades WHERE side='sell' AND ts LIKE ? AND COALESCE(schaduw,0)=0", (day,))
        buys = query("SELECT COUNT(*) FROM trades WHERE side='buy' AND ts LIKE ? AND COALESCE(schaduw,0)=0", (day,))
        p, n = pnl[0] if pnl else (0, 0)
        nb = buys[0][0] if buys else 0
        self.t_today.set(eur(p, True), f"{nb} gekocht, {n} verkocht · daglimiet {eur(rules['daily_loss_limit_eur'])}",
                         C["good"] if p > 0 else (C["bad"] if p < 0 else None))

        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
        cost = query("SELECT COALESCE(SUM(usd),0), COUNT(*) FROM api_usage WHERE ts >= ?", (since,))
        usd, calls = cost[0] if cost else (0, 0)
        self.t_cost.set(f"$ {usd:.2f}".replace(".", ","), f"{calls} aanroepen")

        marks = {(bk, m): (px, v, p) for bk, m, px, v, p in query(
            "SELECT book, market, price, value_eur, pnl_eur FROM position_marks")}

        def live_cols(mk, cost):
            if not mk:
                return ["…", "…", "…"]
            px, v, p = mk
            pct = p / cost * 100 if cost else 0
            return [fmt_px(px), eur(v), f"{eur(p, True)} ({pct:+.1f}%)"]

        rows = []
        for market, amount, entry, cost_eur, opened, shadow in query(
                "SELECT market, amount, entry_price, cost_eur, opened, COALESCE(schaduw,0) FROM positions"):
            plan = risk_gate.exit_plan(rules, market, entry)
            mk = marks.get(("long", market))
            rows.append(["kopen (schaduw)" if shadow else "kopen", market, eur(cost_eur), fmt_px(entry), *live_cols(mk, cost_eur),
                         fmt_px(plan['stop_price']), fmt_px(plan['target_price']), local_time(opened)])
        for market, amount, entry, cost_eur, opened, real in query(
                "SELECT market, amount, entry_price, cost_eur, opened, COALESCE(echt,0) FROM short_positions"):
            plan = risk_gate.exit_plan(rules, market, entry, "short")
            mk = marks.get(("short", market))
            rows.append(["short" if real else "short (sim)", market, eur(cost_eur), fmt_px(entry), *live_cols(mk, cost_eur),
                         fmt_px(plan['stop_price']), fmt_px(plan['target_price']), local_time(opened)])
        fill(self.pos, rows)
        from PySide6.QtGui import QColor
        for r, row in enumerate(rows):
            it = self.pos.item(r, 6)
            if it and it.text().startswith("€ +") and not it.text().startswith("€ +0,00"):
                it.setForeground(QColor(C["good"]))
            elif it and it.text().startswith("€ -"):
                it.setForeground(QColor(C["bad"]))
        if not rows:
            self.pos.setRowCount(0)


# ======================================================================
class OverviewPage(QWidget):
    def __init__(self, win):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 24)
        v.addWidget(lab("Overzicht", "h1"))
        tabs = QTabWidget()
        self.trades = table(["Tijd", "Munt", "Koop/verkoop", "Bedrag", "Koers", "Resultaat", "Live", "Reden"])
        self.risk = table(["Tijd", "Soort", "Munt", "Oordeel", "Bedrag", "Risico", "Grens", "Redenen"])
        self.shorts = table(["Tijd", "Munt", "Open/sluiten", "Bedrag", "Koers", "Resultaat", "Reden"])
        sw = QWidget()
        sv = QVBoxLayout(sw)
        sv.setContentsMargins(0, 8, 0, 0)
        sv.addWidget(lab("Gesimuleerde shorts: Bitvavo biedt shorten nog niet via de API. Er gaat geen echt geld "
                         "in om. Resultaat is inclusief fees en een aanname voor leenkosten (zie Instellingen).",
                         "hint", wrap=True))
        sv.addWidget(self.shorts, 1)
        # Statistieken: twee kaarten naast elkaar
        stw = QWidget()
        sth = QHBoxLayout(stw)
        sth.setContentsMargins(0, 12, 0, 0)
        sth.setSpacing(12)
        self.stat_cards = {}
        for book, title in [("long", "Kopen"), ("short", "Shorten (simulatie)")]:
            c = card()
            cv2 = QVBoxLayout(c)
            cv2.setContentsMargins(18, 16, 18, 16)
            cv2.addWidget(lab(title.upper(), "cap"))
            big = lab("–", "big")
            cv2.addWidget(big)
            grid = QGridLayout()
            grid.setVerticalSpacing(6)
            vals = {}
            for i, (key, label) in enumerate([("trades", "Gesloten trades"), ("wl", "Winst / verlies"),
                                              ("winrate", "Winrate"), ("avg_win", "Gem. winst"),
                                              ("avg_loss", "Gem. verlies"), ("best", "Beste trade"),
                                              ("worst", "Slechtste trade"), ("open", "Open posities"),
                                              ("b_regels", "Via de regels"), ("b_volgers", "Via de volgers"),
                                              ("b_beide", "Regels én volgers")]):
                grid.addWidget(lab(label, "sub"), i, 0)
                vl = lab("–")
                vl.setAlignment(Qt.AlignRight)
                grid.addWidget(vl, i, 1)
                vals[key] = vl
            cv2.addLayout(grid)
            cv2.addStretch(1)
            self.stat_cards[book] = (big, vals)
            sth.addWidget(c, 1)
        self.cost = table(["Agent", "Aanroepen", "Gem. tokens in", "Gem. tokens uit", "Kosten $"])
        self.cost_note = lab("", "hint", wrap=True)
        cw = QWidget()
        cv = QVBoxLayout(cw)
        cv.setContentsMargins(0, 8, 0, 0)
        cv.addWidget(self.cost)
        cv.addWidget(self.cost_note)
        tabs.addTab(stw, "Statistieken")
        tabs.addTab(self.trades, "Trades")
        tabs.addTab(sw, "Shorts (sim)")
        tabs.addTab(self.risk, "Risicopoort")
        tabs.addTab(cw, "API-kosten")
        v.addWidget(tabs, 1)

    def refresh(self):
        rows = []
        for ts, m, side, e, px, pnl, live, reason, shadow in query(
                "SELECT ts, market, side, eur, price, pnl, live, reason, COALESCE(schaduw,0) FROM trades "
                "ORDER BY id DESC LIMIT 300"):
            rows.append([local_time(ts), m, "koop" if side == "buy" else "verkoop", eur(e or 0),
                         fmt_px(px or 0), eur(pnl or 0, True) if side == "sell" else "",
                         "schaduw" if shadow else ("ja" if live else "nee"),
                         reason])
        fill(self.trades, rows)

        rows = [[local_time(ts), "short (sim)" if bk == "short" else "kopen", m, v, eur(a or 0), eur(r or 0), d, why]
                for ts, bk, m, v, a, r, d, why in query(
            "SELECT ts, COALESCE(book,'long'), market, verdict, amount_eur, risk_eur, decider, reasons "
            "FROM risk_log ORDER BY id DESC LIMIT 300")]
        fill(self.risk, rows)

        rows = []
        for ts, m, side, e, px, pnl, reason in query(
                "SELECT ts, market, side, eur, price, pnl, reason FROM short_trades ORDER BY id DESC LIMIT 300"):
            rows.append([local_time(ts), m, "short open" if side == "short" else "short dicht", eur(e or 0),
                         fmt_px(px or 0), eur(pnl or 0, True) if side == "cover" else "", reason])
        fill(self.shorts, rows)

        try:
            con = sqlite3.connect(settings.db_path())
            stats = {b: risk_gate.book_stats(con, b) for b in ("long", "short")}
            for b in ("long", "short"):
                stats[b]["per_bron"] = {br: risk_gate.book_stats(con, b, br) for br in ("regels", "volgers", "beide")}
            con.close()
        except Exception:
            stats = {}
        for book, (big, vals) in self.stat_cards.items():
            st = stats.get(book)
            if not st:
                continue
            big.setText(eur(st["total_pnl"], True))
            big.setStyleSheet(f"color: {C['good'] if st['total_pnl'] > 0 else C['bad'] if st['total_pnl'] < 0 else C['text']};")
            opt = lambda x: eur(x, True) if x is not None else "–"
            vals["trades"].setText(str(st["trades"]))
            vals["wl"].setText(f"{st['wins']} / {st['losses']}")
            vals["winrate"].setText(f"{st['winrate'] * 100:.0f}%" if st["winrate"] is not None else "–")
            vals["avg_win"].setText(opt(st["avg_win"]))
            vals["avg_loss"].setText(opt(st["avg_loss"]))
            vals["best"].setText(opt(st["best"]))
            vals["worst"].setText(opt(st["worst"]))
            vals["open"].setText(str(st["open_positions"]))
            for br in ("regels", "volgers", "beide"):
                sb = st["per_bron"][br]
                wr = f", {sb['winrate'] * 100:.0f}% raak" if sb["winrate"] is not None else ""
                vals["b_" + br].setText(f"{eur(sb['total_pnl'], True)} ({sb['trades']} trades{wr})")

        rows = [[a, c, f"{i:.0f}", f"{o:.0f}", f"{u:.4f}"] for a, c, i, o, u in query(
            "SELECT agent, COUNT(*), AVG(input_tokens), AVG(output_tokens), SUM(usd) FROM api_usage "
            "GROUP BY agent ORDER BY SUM(usd) DESC")]
        fill(self.cost, rows)
        span = query("SELECT MIN(ts), MAX(ts), COALESCE(SUM(usd),0) FROM api_usage")
        if span and span[0][0]:
            first, last, usd = span[0]
            hours = (datetime.datetime.fromisoformat(last) - datetime.datetime.fromisoformat(first)).total_seconds() / 3600
            if hours >= 1:
                self.cost_note.setText(f"Totaal $ {usd:.4f} sinds {local_time(first)}. "
                                       f"Verwachting per maand bij dit tempo: $ {usd / hours * 720:.2f}.")
            else:
                self.cost_note.setText(f"Totaal $ {usd:.4f}. Na een paar uur draaien volgt een maandschatting.")
        else:
            self.cost_note.setText("Nog geen gegevens: start de bot en wacht de eerste analyse af.")


# ======================================================================
class FollowersPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(lab("Volgers", "h1"))
        top.addStretch(1)
        self.btn = QPushButton("Traders opnieuw kiezen")
        self.btn.clicked.connect(self.repick)
        top.addWidget(self.btn)
        v.addLayout(top)
        v.addWidget(lab("Winstgevende traders op Hyperliquid (openbare gegevens). Elke 30 seconden bekijkt de bot hun "
                        "posities. Staan genoeg van hen dezelfde kant op, dan is dat een volger-setup. "
                        "Alleen kijken: er wordt niets automatisch gekopieerd.", "hint", wrap=True))
        self.note = lab("", "banner", wrap=True)
        self.note.hide()
        v.addWidget(self.note)
        v.addWidget(lab("PER MUNT", "cap"))
        self.cons = table(["Munt", "Long", "Short", "Consensus", "Long $", "Short $", "Gem. hefboom", "Laatste actie"])
        self.cons.setMaximumHeight(230)
        v.addWidget(self.cons)
        h = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(lab("GEVOLGDE TRADERS", "cap"))
        self.traders = table(["Trader", "Naam", "Account $", "Winst maand $", "ROI maand", "Bron"])
        self.traders.setSelectionMode(QTableWidget.ExtendedSelection)
        self.traders.setSelectionBehavior(QTableWidget.SelectRows)
        self._addrs = []
        left.addWidget(self.traders)
        cp = QHBoxLayout()
        cp.addWidget(lab("Geselecteerde traders", "sub"))
        self.cp_style = QComboBox()
        self.cp_style.addItems(["lang", "kort"])
        cp.addWidget(self.cp_style)
        self.cp_btn = QPushButton("Naar Kopiëren")
        self.cp_btn.clicked.connect(self.to_copy)
        cp.addWidget(self.cp_btn)
        cp.addStretch(1)
        left.addLayout(cp)
        self.cp_msg = lab("", "hint", wrap=True)
        left.addWidget(self.cp_msg)
        right = QVBoxLayout()
        right.addWidget(lab("RECENTE ACTIES", "cap"))
        self.events = table(["Tijd", "Trader", "Munt", "Actie", "Grootte $"])
        right.addWidget(self.events)
        h.addLayout(left, 3)
        h.addLayout(right, 2)
        v.addLayout(h, 1)

    def to_copy(self):
        """Geselecteerde volgers in de lijst van de kopieer-modus zetten en opslaan."""
        picked = sorted({i.row() for i in self.traders.selectedIndexes()})
        if not picked:
            self.cp_msg.setText("Selecteer eerst een of meer traders in de tabel (Ctrl-klik voor meerdere).")
            return
        cp = self.win.cp
        text = cp.traders.toPlainText().strip()
        added = []
        for r in picked:
            if r >= len(self._addrs):
                continue
            addr, name = self._addrs[r]
            if not addr or addr.lower() in text.lower():
                continue
            label = (name or addr[:8]).replace(" ", "_")
            text = (text + "\n" if text else "") + f"{addr.lower()} {label} {self.cp_style.currentText()}"
            added.append(label)
        cp.traders.setPlainText(text)
        cp.save()
        self.cp_msg.setText((f"Toegevoegd aan Kopiëren: {', '.join(added)}. " if added else
                             "Deze traders stonden al bij Kopiëren. ") +
                            "Zet de kopieer-modus aan bij Kopiëren en start de bot opnieuw.")

    def repick(self):
        import threading
        from followers import Followers
        self.btn.setEnabled(False)
        self.note.setText("Bezig met kiezen…")
        self.note.show()
        fcfg = dict(self.win.cfg.get("followers") or {})

        def work():
            try:
                f = Followers(settings.db_path(), settings.data_dir(), fcfg, self.win.log)
                if os.path.exists(f.cache):
                    os.remove(f.cache)
                f.choose(force=True)
                self.win.log("Volgers opnieuw gekozen. Hun posities worden bij de volgende ronde opgehaald.", "info")
            except Exception as e:
                self.win.log(f"Volgers kiezen mislukt: {e}", "bad")
            self._done = True
        self._done = False
        threading.Thread(target=work, daemon=True).start()

        def check():
            if self._done:
                self.btn.setEnabled(True)
                self.note.hide()
                self.refresh()
            else:
                QTimer.singleShot(500, check)
        QTimer.singleShot(500, check)

    def refresh(self):
        def usd(x):
            return f"{x:,.0f}".replace(",", ".")
        markets = [m.split("-")[0] for m in self.win.cfg.get("markets", [])]
        rows = []
        for coin in markets:
            r = query("SELECT side, COUNT(*), COALESCE(SUM(notional),0), AVG(leverage) FROM follower_positions "
                      "WHERE coin=? AND notional >= 1000 GROUP BY side", (coin,))
            d = {side: (n, tot, lev) for side, n, tot, lev in r}
            nl, tl, ll = d.get("long", (0, 0, None))
            ns, ts_, ls = d.get("short", (0, 0, None))
            score = (nl - ns) / (nl + ns) if (nl + ns) else 0
            levs = [x for x in (ll, ls) if x]
            last = query("SELECT ts, event, side FROM follower_events WHERE coin=? ORDER BY id DESC LIMIT 1", (coin,))
            last_txt = f"{local_time(last[0][0])} {last[0][1]} {last[0][2]}" if last else ""
            rows.append([coin, nl, ns, f"{score:+.2f}", usd(tl), usd(ts_),
                         f"{sum(levs) / len(levs):.1f}x" if levs else "", last_txt])
        fill(self.cons, rows)
        data = query("SELECT address, name, account_value, month_pnl, month_roi, source FROM follower_traders "
                     "ORDER BY month_roi DESC")
        self._addrs = [(a, n) for a, n, *_ in data]
        rows = [[(a[:6] + "…" + a[-4:]) if a else "", n, usd(acc or 0), usd(mp or 0), f"{(mr or 0) * 100:.1f}%",
                 src if src in ("eigen", "invo") else "leaderboard"]
                for a, n, acc, mp, mr, src in data]
        fill(self.traders, rows)
        rows = [[local_time(ts), (a[:6] + "…" + a[-4:]) if a else "", c, f"{e} {sd}", usd(nt or 0)]
                for ts, a, c, e, sd, nt in query(
            "SELECT ts, address, coin, event, side, notional FROM follower_events ORDER BY id DESC LIMIT 100")]
        fill(self.events, rows)


class EquityChart(QWidget):
    """Eenvoudige lijngrafiek: bot (goud) tegen BTC vasthouden (blauw)."""
    def __init__(self):
        super().__init__()
        self.series = []
        self.setMinimumHeight(220)

    def set(self, series):
        self.series = [s for s in series if s[1]]
        self.update()

    def paintEvent(self, e):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        qp.fillRect(0, 0, w, h, QColor(C["card"]))
        pts = [v for _, data in self.series for _, v in data]
        if not pts:
            qp.setPen(QColor(C["muted"]))
            qp.drawText(self.rect(), Qt.AlignCenter, "Nog geen test gedraaid")
            return
        lo, hi = min(pts), max(pts)
        if hi - lo < 1e-9:
            hi = lo + 1
        pad = 40
        t0 = min(t for _, d in self.series for t, _ in d)
        t1 = max(t for _, d in self.series for t, _ in d)
        span = max(1, t1 - t0)

        def xy(t, v):
            return pad + (t - t0) / span * (w - pad - 10), 10 + (hi - v) / (hi - lo) * (h - 40)
        qp.setPen(QPen(QColor(C["line"]), 1))
        for frac in (0, 0.5, 1):
            v = lo + (hi - lo) * frac
            _, y = xy(t0, v)
            qp.drawLine(pad, int(y), w - 10, int(y))
            qp.setPen(QColor(C["muted"]))
            qp.drawText(2, int(y) + 4, f"€{v:.0f}")
            qp.setPen(QPen(QColor(C["line"]), 1))
        for color, data in self.series:
            qp.setPen(QPen(QColor(color), 2))
            prev = None
            for t, v in data:
                p = xy(t, v)
                if prev:
                    qp.drawLine(int(prev[0]), int(prev[1]), int(p[0]), int(p[1]))
                prev = p
        qp.setPen(QColor(C["accent"]))
        qp.drawText(pad + 6, h - 8, "— bot (regels)")
        qp.setPen(QColor(C["info"]))
        qp.drawText(pad + 130, h - 8, "— BTC vasthouden")


class CopyPage(QWidget):
    """Kopieer-modus: traders 1-op-1 volgen op Kraken, plus de vingerafdruk-zoeker voor Invo-traders."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._q = queue.Queue()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        v = QVBoxLayout(inner)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(10)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        top = QHBoxLayout()
        top.addWidget(lab("Kopiëren", "h1"))
        top.addStretch(1)
        self.save_btn = QPushButton("Opslaan")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        top.addWidget(self.save_btn)
        v.addLayout(top)
        v.addWidget(lab("Volgt gekozen traders 1-op-1 op Kraken: opent hij een nieuwe positie, dan opent de bot "
                        "dezelfde kant; sluit hij, dan sluit de bot ook. Alleen nieuwe posities, nooit instappen in "
                        "een positie die al openstond. Elke kopie krijgt een noodstop. Wijzigingen gelden na het "
                        "opnieuw starten van de bot.", "hint", wrap=True))

        g = QGroupBox("Instellingen")
        f = QFormLayout(g)
        self.on = QCheckBox("Kopieer-modus aan")
        f.addRow("", self.on)
        self.live = QCheckBox("Live: echt geld (werkt alleen als de bot zelf live op Kraken staat)")
        self.live.toggled.connect(self._confirm_live)
        f.addRow("", self.live)
        self.every = QSpinBox(); self.every.setRange(2, 60); self.every.setSuffix(" s")
        f.addRow("Kijken elke", self.every)
        self.stake = QDoubleSpinBox(); self.stake.setRange(2, 500); self.stake.setPrefix("€ ")
        f.addRow("Inzet per trade", self.stake)
        self.maxlev = QSpinBox(); self.maxlev.setRange(1, 10); self.maxlev.setSuffix("x")
        f.addRow("Hefboom max", self.maxlev)
        self.maxpos = QSpinBox(); self.maxpos.setRange(1, 20)
        f.addRow("Max kopieën tegelijk", self.maxpos)
        self.dayloss = QDoubleSpinBox(); self.dayloss.setRange(1, 1000); self.dayloss.setPrefix("€ ")
        f.addRow("Daglimiet verlies", self.dayloss)
        self.stop_l = QDoubleSpinBox(); self.stop_l.setRange(0.5, 50); self.stop_l.setSuffix(" %")
        f.addRow("Noodstop lange traders", self.stop_l)
        self.stop_k = QDoubleSpinBox(); self.stop_k.setRange(0.5, 50); self.stop_k.setSuffix(" %")
        f.addRow("Noodstop korte traders", self.stop_k)
        self.hint = lab("", "hint", wrap=True)
        f.addRow("", self.hint)
        for w in (self.stake, self.maxlev, self.stop_l, self.stop_k):
            w.valueChanged.connect(self._update_hint)
        v.addWidget(g)

        v.addWidget(lab("TRADERS (één per regel: adres naam lang/kort)", "cap"))
        self.traders = QPlainTextEdit()
        self.traders.setPlaceholderText("0x1234…abcd blackstallion lang")
        self.traders.setMaximumHeight(140)
        v.addWidget(self.traders)

        v.addWidget(lab("INVO-TRADER ZOEKEN (vingerafdruk)", "cap"))
        v.addWidget(lab("Zoekt in de openbare Invo-trades op Hyperliquid naar het adres dat dezelfde trades deed als "
                        "op het Invo-profiel. De eerste keer duurt het een paar minuten (30 dagen aan bestanden), "
                        "daarna gaat het snel. Dubbelklik op een uitkomst om hem bij de traders te zetten.",
                        "hint", wrap=True))
        row = QHBoxLayout()
        import copier
        self.fp = QComboBox()
        self.fp.addItems(["Alle van onze lijst"] + list(copier.FINGERPRINTS))
        row.addWidget(self.fp)
        self.find_btn = QPushButton("Zoeken")
        self.find_btn.clicked.connect(self.find)
        row.addWidget(self.find_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.find_status = lab("", "sub", wrap=True)
        v.addWidget(self.find_status)
        self.found = table(["Invo-naam", "Adres", "Treffers", "Account $ nu", "Account $ op Invo", "Stijl"])
        self.found.setSelectionMode(QTableWidget.SingleSelection)
        self.found.setSelectionBehavior(QTableWidget.SelectRows)
        self.found.cellDoubleClicked.connect(self._add_found)
        self.found.setMinimumHeight(180)
        v.addWidget(self.found)

        v.addWidget(lab("OPEN KOPIEËN", "cap"))
        self.open_t = table(["Geopend", "Munt", "Kant", "Hefboom", "Positie", "Instap", "Noodstop", "Trader", "Echt"])
        self.open_t.setMinimumHeight(140)
        v.addWidget(self.open_t)
        v.addWidget(lab("GESLOTEN KOPIEËN (bot naast de trader)", "cap"))
        self.closed_t = table(["Dicht", "Munt", "Kant", "Trader", "Bot €", "Bot % inzet", "Trader % (zelfde hefboom)",
                               "Kosten", "Reden"])
        self.closed_t.setMinimumHeight(220)
        v.addWidget(self.closed_t)
        self.total = lab("", "sub", wrap=True)
        v.addWidget(self.total)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._loading = False

    # --- laden/opslaan
    def load(self, cfg):
        self._loading = True
        c = cfg.get("copy") or settings.DEFAULTS["copy"]
        d = settings.DEFAULTS["copy"]
        self.on.setChecked(bool(c.get("enabled")))
        self.live.setChecked(bool(c.get("live")))
        self.every.setValue(int(c.get("interval_s", d["interval_s"])))
        self.stake.setValue(float(c.get("eur_per_trade", d["eur_per_trade"])))
        self.maxlev.setValue(int(c.get("max_leverage", d["max_leverage"])))
        self.maxpos.setValue(int(c.get("max_positions", d["max_positions"])))
        self.dayloss.setValue(float(c.get("daily_loss_eur", d["daily_loss_eur"])))
        self.stop_l.setValue(float(c.get("stop_pct_lang", d["stop_pct_lang"])))
        self.stop_k.setValue(float(c.get("stop_pct_kort", d["stop_pct_kort"])))
        self.traders.setPlainText("\n".join(f"{t['address']} {t.get('name', '')} {t.get('style', 'lang')}"
                                            for t in c.get("traders", [])))
        self._loading = False
        self._update_hint()

    def save(self):
        traders = []
        for line in self.traders.toPlainText().splitlines():
            parts = line.split()
            if parts and parts[0].lower().startswith("0x") and len(parts[0]) == 42:
                style = parts[2].lower() if len(parts) > 2 and parts[2].lower() in ("lang", "kort") else "lang"
                traders.append({"address": parts[0].lower(), "name": parts[1] if len(parts) > 1 else parts[0][:8],
                                "style": style})
        self.win.cfg["copy"] = {
            "enabled": self.on.isChecked(), "live": self.live.isChecked(), "interval_s": self.every.value(),
            "eur_per_trade": round(self.stake.value(), 2), "max_leverage": self.maxlev.value(),
            "max_positions": self.maxpos.value(), "daily_loss_eur": round(self.dayloss.value(), 2),
            "stop_pct_lang": round(self.stop_l.value(), 2), "stop_pct_kort": round(self.stop_k.value(), 2),
            "traders": traders}
        settings.save(self.win.cfg)
        self.win.log(f"Kopieer-instellingen opgeslagen ({len(traders)} traders). Start de bot opnieuw om ze te "
                     "gebruiken.", "info")

    def _update_hint(self):
        if self._loading:
            return
        lev = self.maxlev.value()
        pos = self.stake.value() * lev
        sl = min(self.stop_l.value(), 80 / lev)
        sk = min(self.stop_k.value(), 80 / lev)
        self.hint.setText(f"Bij {lev}x: positie €{pos:.0f}. Max verlies per trade ongeveer €{pos * sl / 100:.2f} "
                          f"(lang) en €{pos * sk / 100:.2f} (kort), plus €{pos * 0.001:.2f} kosten. De noodstop ligt "
                          f"nooit verder dan 80% van je inzet.")

    def _confirm_live(self, on):
        if self._loading or not on:
            return
        ok = QMessageBox.question(self, "Kopiëren met echt geld",
                                  "De bot opent en sluit dan echte posities op Kraken, met hefboom, zodra de gekozen "
                                  "traders dat doen. Heb je het eerst droog getest en is de inzet een bedrag dat je "
                                  "kunt missen?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            self.live.setChecked(False)

    # --- vingerafdruk
    def find(self):
        import copier
        name = self.fp.currentText()
        names = list(copier.FINGERPRINTS) if name.startswith("Alle") else [name]
        self.find_btn.setEnabled(False)
        self.found.setRowCount(0)

        def work():
            rows = []
            try:
                for n in names:
                    fp = copier.FINGERPRINTS[n]
                    res = copier.find_trader(fp["trades"], settings.data_dir(), account=fp.get("account"),
                                             progress=lambda t, n=n: self._q.put(("msg", f"@{n}: {t}")))
                    for c in res[:3]:
                        rows.append([n, c["address"], f"{c['matches']} van {c['of']}",
                                     f"{c['account']:,.0f}" if c["account"] else "?",
                                     f"{fp['account']:,}" if fp.get("account") else "verborgen", fp["style"]])
                    if not res:
                        rows.append([n, "niet gevonden", "", "", "", fp["style"]])
                    self._q.put(("rows", list(rows)))
                self._q.put(("done", len(names)))
            except Exception as e:
                self._q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()
        self._timer.start(300)

    def _poll(self):
        while True:
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "msg":
                self.find_status.setText(val)
            elif kind == "rows":
                fill(self.found, val)
            elif kind == "err":
                self._timer.stop()
                self.find_btn.setEnabled(True)
                self.find_status.setText(f"Zoeken mislukt: {val}")
            else:
                self._timer.stop()
                self.find_btn.setEnabled(True)
                self.find_status.setText("Klaar. Bovenaan per naam staat de beste match: de meeste treffers en een "
                                         "accountwaarde die het dichtst bij Invo ligt. Dubbelklik om toe te voegen.")

    def _add_found(self, r, _c):
        name = self.found.item(r, 0).text()
        addr = self.found.item(r, 1).text()
        style = self.found.item(r, 5).text()
        if not addr.startswith("0x"):
            return
        text = self.traders.toPlainText().strip()
        if addr in text.lower():
            return
        self.traders.setPlainText((text + "\n" if text else "") + f"{addr} {name} {style}")
        self.find_status.setText(f"@{name} toegevoegd. Klik op Opslaan en start de bot opnieuw.")

    # --- tabellen
    def refresh(self):
        rows = []
        for r in query("SELECT opened, coin, side, leverage, cost_eur, entry_price, stop_price, trader_name, echt "
                       "FROM copy_positions ORDER BY opened DESC"):
            opened, coin, side, lev, cost, entry, stop, tn, real = r
            rows.append([local_time(opened), coin, side, f"{lev:.0f}x", eur(cost or 0), fmt_px(entry or 0),
                         fmt_px(stop or 0), tn, "ja" if real else "droog"])
        fill(self.open_t, rows)
        rows, tot, n, wins = [], 0.0, 0, 0
        for r in query("SELECT closed, coin, side, trader_name, pnl_eur, pnl_pct, trader_pct, fee_eur, reason "
                       "FROM copy_trades ORDER BY id DESC LIMIT 300"):
            closed, coin, side, tn, pnl, pct, tpct, fee, reason = r
            tot += pnl or 0
            n += 1
            wins += 1 if (pnl or 0) > 0 else 0
            rows.append([local_time(closed), coin, side, tn, eur(pnl or 0, True), f"{(pct or 0):+.1f}%",
                         f"{tpct:+.1f}%" if tpct is not None else "?", eur(fee or 0), reason])
        fill(self.closed_t, rows)
        self.total.setText(f"{n} kopieën gesloten, {wins} met winst, totaal {eur(tot, True)}." if n else
                           "Nog geen gesloten kopieën.")



def backtest_lines(rows, months, ex):
    import backtest
    return backtest.variants_text(rows, months, ex)


class BacktestPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        v = QVBoxLayout(inner)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(10)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        top = QHBoxLayout()
        top.addWidget(lab("Backtest", "h1"))
        top.addStretch(1)
        self.period = QComboBox()
        self.period.addItems(["3 maanden", "6 maanden", "12 maanden", "24 maanden"])
        self.period.setCurrentIndex(2)
        top.addWidget(self.period)
        self.with_shorts = QCheckBox("Met shorts")
        self.with_shorts.setChecked(True)
        top.addWidget(self.with_shorts)
        self.with_var = QCheckBox("Varianten vergelijken")
        self.with_var.setChecked(True)
        top.addWidget(self.with_var)
        self.btn = QPushButton("Test starten")
        self.btn.setObjectName("primary")
        self.btn.clicked.connect(self.start)
        top.addWidget(self.btn)
        v.addLayout(top)
        v.addWidget(lab("Speelt de vaste regels uur voor uur na op echte koersen van Bitvavo, met jouw instellingen: "
                        "de vijf instapregels, positiegrootte, stop-loss, koersdoel, daglimiet en fees. "
                        "Varianten: dezelfde test nog eens met telkens één instelling anders, zodat je kunt vergelijken. "
                        "Niet mee te testen: de AI (de Charts-score wordt een rekenscore op dezelfde indicatoren, "
                        "nieuws telt als neutraal, geen Skeptic/Chief) en de volgers. Kost geen API-tegoed.",
                        "hint", wrap=True))
        self.status = lab("", "sub", wrap=True)
        v.addWidget(self.status)

        g = QGridLayout()
        g.setSpacing(12)
        self.t_end = StatTile("Eindstand bot")
        self.t_hold = StatTile("BTC vasthouden")
        self.t_trades = StatTile("Trades")
        self.t_dd = StatTile("Grootste daling")
        for i, t in enumerate([self.t_end, self.t_hold, self.t_trades, self.t_dd]):
            g.addWidget(t, 0, i)
        v.addLayout(g)
        self.chart = EquityChart()
        v.addWidget(self.chart)
        h = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(lab("PER SOORT EN PER MUNT", "cap"))
        self.per = table(["", "Trades", "Winrate", "Totaal", "Gem. winst", "Gem. verlies", "Beste", "Slechtste"])
        self.per.setMinimumHeight(260)
        left.addWidget(self.per)
        right = QVBoxLayout()
        right.addWidget(lab("LAATSTE TRADES", "cap"))
        self.trades = table(["Open", "Munt", "Soort", "Inleg", "Resultaat", "Reden"])
        self.trades.setMinimumHeight(260)
        right.addWidget(self.trades)
        h.addLayout(left, 1)
        h.addLayout(right, 1)
        v.addLayout(h)
        v.addWidget(lab("VARIANTEN (zelfde koersen, één instelling anders)", "cap"))
        self.var = table(["Variant", "Eindstand", "Resultaat", "t.o.v. BTC", "Trades", "Winrate", "Kopen",
                          "Shorten", "Grootste daling"])
        self.var.setMinimumHeight(240)
        v.addWidget(self.var)
        self._q = queue.Queue()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)

    def start(self):
        import threading
        months = [3, 6, 12, 24][self.period.currentIndex()]
        markets = list(self.win.cfg.get("markets") or [])
        if "BTC-EUR" not in markets:
            markets = ["BTC-EUR"] + markets
        rules = settings.effective_rules(self.win.cfg)
        shorts = self.with_shorts.isChecked()
        with_var = self.with_var.isChecked()
        self.btn.setEnabled(False)
        self.status.setText("Koersen ophalen bij Bitvavo…")

        def work():
            try:
                import backtest
                from python_bitvavo_api.bitvavo import Bitvavo
                bv = Bitvavo({})
                data = {}
                for m in markets:
                    data[m] = backtest.fetch_candles(bv, m, months, lambda t: self._q.put(("msg", t)))
                self._q.put(("msg", "Naspelen…"))
                res = backtest.run(data, rules, shorts=shorts, progress=lambda t: self._q.put(("msg", t)),
                                   cycle_minutes=int(self.win.cfg.get("cycle_minutes", 120)))
                res["months"] = months
                if with_var:
                    res["variants"] = backtest.run_variants(
                        data, rules, shorts=shorts, cycle_minutes=int(self.win.cfg.get("cycle_minutes", 120)),
                        progress=lambda t: self._q.put(("msg", t)))
                self._q.put(("done", res))
            except Exception as e:
                self._q.put(("err", str(e)))
        threading.Thread(target=work, daemon=True).start()
        self._timer.start(300)

    def _poll(self):
        while True:
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "msg":
                self.status.setText(val)
            elif kind == "err":
                self._timer.stop()
                self.btn.setEnabled(True)
                self.status.setText(f"Test mislukt: {val}")
            else:
                self._timer.stop()
                self.btn.setEnabled(True)
                self.show_result(val)

    def show_variants(self, rows):
        o = lambda x: eur(x, True) if x is not None else "–"
        data = []
        for x in sorted(rows, key=lambda x: x["final"], reverse=True):
            data.append([x["name"], eur(x["final"]), o(x["result"]), o(x["vs_hold"]), x["trades"],
                         f"{x['winrate'] * 100:.0f}%" if x["winrate"] is not None else "–",
                         o(x["long"]), o(x["short"]), eur(x["max_drawdown"])])
        fill(self.var, data)

    def show_result(self, r):
        def d(ts):
            return datetime.datetime.fromtimestamp(ts / 1000).strftime("%d-%m-%Y") if ts else "?"
        b = r["budget"]
        self.status.setText(f"Periode {d(r['start'])} t/m {d(r['end'])} ({r['months']} maanden). "
                            f"Nog open aan het eind: {r['open']}.")
        self.t_end.set(eur(r["final"]), f"{eur(r['final'] - b, True)} op {eur(b)}",
                       C["good"] if r["final"] >= b else C["bad"])
        if r["hold_final"]:
            self.t_hold.set(eur(r["hold_final"]), f"bot {eur(r['final'] - r['hold_final'], True)} t.o.v. BTC")
        st = r["stats"]["all"]
        self.t_trades.set(str(st["trades"]), f"{st['winrate'] * 100:.0f}% winst" if st["winrate"] is not None else "")
        self.t_dd.set(eur(r["max_drawdown"]), "van hoogste naar laagste stand", C["bad"] if r["max_drawdown"] < 0 else None)
        self.chart.set([(C["accent"], r["equity"]), (C["info"], r["hold"])])
        if r.get("variants"):
            self.show_variants(r["variants"])
            ex = "Kraken" if self.win.cfg.get("exchange") == "kraken" else "Bitvavo"
            for line in backtest_lines(r["variants"], r["months"], ex):
                self.win.log(line, "info")

        def row(name, s):
            o = lambda x: eur(x, True) if x is not None else "–"
            return [name, s["trades"], f"{s['winrate'] * 100:.0f}%" if s["winrate"] is not None else "–",
                    o(s["total"]), o(s["avg_win"]), o(s["avg_loss"]), o(s["best"]), o(s["worst"])]
        rows = [row("Alles", st), row("Kopen", r["stats"]["long"]), row("Shorten", r["stats"]["short"])]
        rows += [row(m, s) for m, s in r["per_market"].items()]
        fill(self.per, rows)
        rows = [[d(t["open"]), t["market"], "kopen" if t["side"] == "long" else "short", eur(t["eur"]),
                 eur(t["pnl"], True), t["reason"]] for t in reversed(r["trades"][-300:])]
        fill(self.trades, rows)


class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        v = QVBoxLayout(inner)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(6)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        head = QHBoxLayout()
        head.addWidget(lab("Instellingen", "h1"))
        head.addStretch(1)
        save = QPushButton("Opslaan")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        head.addWidget(save)
        v.addLayout(head)
        self.note = lab("", "banner", wrap=True)
        self.note.hide()
        v.addWidget(self.note)

        # --- Keys
        g = QGroupBox("API-keys")
        f = QFormLayout(g)
        self.k_ant = self._secret()
        self.k_bv = self._secret()
        self.k_bvs = self._secret()
        f.addRow("Anthropic API-key", self.k_ant)
        f.addRow("Bitvavo API-key", self.k_bv)
        f.addRow("Bitvavo API-secret", self.k_bvs)
        self.k_kr = self._secret()
        self.k_krs = self._secret()
        f.addRow("Kraken futures API-key", self.k_kr)
        f.addRow("Kraken futures API-secret", self.k_krs)
        self.kr_test = QPushButton("Kraken-verbinding testen")
        self.kr_test.clicked.connect(self._test_kraken)
        self.kr_result = lab("", "hint", wrap=True)
        f.addRow("", self.kr_test)
        f.addRow("", self.kr_result)
        self.show_keys = QCheckBox("Keys tonen")
        self.show_keys.toggled.connect(self._toggle_keys)
        f.addRow("", self.show_keys)
        f.addRow("", lab("Bitvavo-key: alleen 'bekijken' en 'handelen' aanzetten, nooit 'opnemen'. "
                         "Zet ook een IP-whitelist aan. De keys worden alleen op deze computer bewaard.",
                         "hint", wrap=True))
        v.addWidget(g)

        # --- Handelen
        g = QGroupBox("Handelen")
        f = QFormLayout(g)
        self.exch = QComboBox()
        self.exch.addItems(["Bitvavo", "Kraken-perps (droog)"])
        f.addRow("Beurs", self.exch)
        self.exch.currentIndexChanged.connect(self._exchange_changed)
        self.lev = QSpinBox()
        self.lev.setRange(1, 5)
        self.lev.setSuffix("x")
        f.addRow("Hefboom (Kraken)", self.lev)
        f.addRow("", lab("Kraken: 0,05% kosten per kant in plaats van 0,25%, shorts zonder leenkosten. Met Live en een "
                         "Kraken-key gaan longs én shorts echt, elk met een stop-loss bij Kraken zelf. Hefboom maakt "
                         "posities groter, maar het maximale verlies per trade blijft je ingestelde bedrag.",
                         "hint", wrap=True))
        self.live = QCheckBox("Live: echte orders plaatsen (op de gekozen beurs)")
        self.live.toggled.connect(self._confirm_live)
        f.addRow("", self.live)
        lb = QWidget()
        lg = QHBoxLayout(lb)
        lg.setContentsMargins(0, 0, 0, 0)
        self.lb_checks = {}
        for key, txt in (("volgers", "alleen volgers"), ("beide", "regels + volgers"), ("regels", "alleen regels")):
            c = QCheckBox(txt)
            self.lb_checks[key] = c
            lg.addWidget(c)
        lg.addStretch(1)
        f.addRow("Live geld voor", lb)
        f.addRow("", lab("Signalen die hier uit staan worden als schaduwtrade nagespeeld: zelfde regels, "
                         "geen echt geld, wel eigen statistieken. De backtest liet zien dat 'alleen regels' "
                         "verlies gaf.", "hint", wrap=True))
        mk = QWidget()
        mg = QGridLayout(mk)
        mg.setContentsMargins(0, 0, 0, 0)
        self.m_checks = {}
        for i, m in enumerate(settings.ALL_MARKETS):
            c = QCheckBox(m)
            self.m_checks[m] = c
            mg.addWidget(c, i // 4, i % 4)
        f.addRow("Markten", mk)
        self.cycle = QSpinBox()
        self.cycle.setRange(15, 1440)
        self.cycle.setSingleStep(15)
        self.cycle.setSuffix(" minuten")
        f.addRow("Analyse elke", self.cycle)
        self.model = QComboBox()
        self.model.addItems(settings.MODELS)
        f.addRow("Model agents", self.model)
        self.model_chief = QComboBox()
        self.model_chief.addItems(["(zelfde als agents)"] + settings.MODELS)
        f.addRow("Model Chief", self.model_chief)
        self.shorts_on = QCheckBox("Chief mag ook shorts kiezen (alleen simulatie, geen echt geld)")
        f.addRow("Shorten", self.shorts_on)
        self.api_cap = QDoubleSpinBox()
        self.api_cap.setRange(0, 500)
        self.api_cap.setSingleStep(1)
        self.api_cap.setDecimals(2)
        self.api_cap.setPrefix("$ ")
        self.api_cap.setSpecialValueText("geen limiet")
        f.addRow("Maandlimiet API-kosten", self.api_cap)
        f.addRow("", lab("Haiku elke 2 uur kost ongeveer $1 per maand; Sonnet elk uur ongeveer $80. "
                         "Boven de maandlimiet stopt de bot met analyseren (stop-loss blijft werken).",
                         "hint", wrap=True))
        v.addWidget(g)

        # --- Risico
        g = QGroupBox("Risicoregels")
        f = QFormLayout(g)
        self.r = {}

        def money(key, label, lo, hi, step, dec=2, hint=None):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(dec)
            s.setPrefix("€ ")
            self.r[key] = s
            f.addRow(label, s)

        def num(key, label, lo, hi, step=1, dec=0, suffix=""):
            s = QDoubleSpinBox() if dec else QSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            if dec:
                s.setDecimals(dec)
            if suffix:
                s.setSuffix(suffix)
            self.r[key] = s
            f.addRow(label, s)

        money("budget_eur", "Totaalbudget", 10, 100000, 10, 0)
        money("max_trade_eur", "Max per aankoop", 5, 10000, 5, 0)
        money("max_loss_per_trade_eur", "Max verlies per trade", 0.1, 1000, 0.25)
        money("daily_loss_limit_eur", "Daglimiet (verlies + open risico)", 1, 10000, 1, 0)
        num("max_positions", "Max open posities", 1, 10)
        num("max_buys_per_day", "Max aankopen per dag", 1, 50)
        num("reward_risk", "Koersdoel (x de stopafstand)", 1, 5, 0.5, 1, " R")
        num("min_confidence", "Min. zekerheid Chief", 0.5, 0.95, 0.05, 2)
        num("max_spread_pct", "Max spread", 0.05, 2, 0.05, 2, " %")
        num("cooldown_hours_after_stop", "Pauze na stop-loss", 0, 72, 1, 0, " uur")
        num("fee_pct", "Bitvavo-fee per order", 0, 1, 0.05, 2, " %")
        num("short_borrow_pct_day", "Leenkosten short (aanname)", 0, 1, 0.01, 3, " % per dag")
        self.size_hint = lab("", "hint", wrap=True)
        f.addRow("", self.size_hint)
        for s in self.r.values():
            s.valueChanged.connect(self._update_hint)
        v.addWidget(g)

        # --- Stop-loss per munt
        g = QGroupBox("Stop-loss per munt")
        self.stop_form = QFormLayout(g)
        self.stops = {}
        for m in ["default"] + settings.ALL_MARKETS:
            s = QDoubleSpinBox()
            s.setRange(1, 30)
            s.setSingleStep(0.5)
            s.setDecimals(1)
            s.setSuffix(" %")
            s.valueChanged.connect(self._update_hint)
            self.stops[m] = s
            self.stop_form.addRow("Overige munten" if m == "default" else m, s)
        v.addWidget(g)

        g = QGroupBox("Volgers (Hyperliquid)")
        f = QFormLayout(g)
        self.fw_on = QCheckBox("Posities van winstgevende traders volgen (gratis, geen AI)")
        f.addRow("", self.fw_on)
        self.fw_setup = QCheckBox("Volgers mogen zelf een setup geven (naast de vijf regels)")
        f.addRow("", self.fw_setup)
        self.fw_exit = QCheckBox("Positie sluiten als de volgers duidelijk de andere kant op draaien")
        f.addRow("", self.fw_exit)
        self.fw_auto = QCheckBox("Automatisch kiezen van het leaderboard")
        f.addRow("", self.fw_auto)
        self.fw_invo = QCheckBox("Ook traders die via Invo handelen (tot de helft van de plekken)")
        f.addRow("", self.fw_invo)
        self.fw_count = QSpinBox()
        self.fw_count.setRange(3, 50)
        f.addRow("Aantal traders", self.fw_count)
        self.fw_min = QSpinBox()
        self.fw_min.setRange(1, 20)
        f.addRow("Minstens zoveel dezelfde kant op", self.fw_min)
        self.fw_cons = QDoubleSpinBox()
        self.fw_cons.setRange(0.2, 1.0)
        self.fw_cons.setSingleStep(0.1)
        self.fw_cons.setDecimals(1)
        f.addRow("Minimale eensgezindheid", self.fw_cons)
        self.fw_manual = QPlainTextEdit()
        self.fw_manual.setPlaceholderText("Eigen adressen, één per regel (0x…)")
        self.fw_manual.setFixedHeight(70)
        f.addRow("Eigen traders", self.fw_manual)
        f.addRow("", lab("Eensgezindheid 0,6 betekent: van de gevolgde traders met een positie in die munt staat "
                         "een duidelijke meerderheid dezelfde kant op (bijvoorbeeld 4 long, 1 short).",
                         "hint", wrap=True))
        self.fast_on = QCheckBox("Tussendoor checken; AI alleen bij een nieuwe setup")
        f.addRow("Snelle check", self.fast_on)
        self.tick = QSpinBox()
        self.tick.setRange(10, 600)
        self.tick.setSingleStep(10)
        self.tick.setSuffix(" seconden")
        f.addRow("Volgers en regels checken elke", self.tick)
        self.fast_cd = QSpinBox()
        self.fast_cd.setRange(5, 240)
        self.fast_cd.setSuffix(" minuten")
        f.addRow("Per munt hooguit eens per", self.fast_cd)
        v.addWidget(g)

        g = QGroupBox("Op afstand meekijken")
        f = QFormLayout(g)
        self.rm_on = QCheckBox("Stand, keuzes en logboek naar het online dashboard sturen")
        f.addRow("", self.rm_on)
        self.rm_url = QLineEdit()
        self.rm_url.setPlaceholderText("https://…/functions/v1/bot-ingest")
        f.addRow("Adres", self.rm_url)
        self.rm_token = self._secret()
        f.addRow("Token", self.rm_token)
        tb = QPushButton("Verbinding testen")
        tb.clicked.connect(self._test_remote)
        self.rm_result = lab("", "hint", wrap=True)
        row = QHBoxLayout()
        row.addWidget(tb)
        row.addWidget(self.rm_result, 1)
        f.addRow("", row)
        f.addRow("", lab("Alleen versturen: vanaf je telefoon kun je meekijken, maar niets bedienen.", "hint", wrap=True))
        v.addWidget(g)

        g = QGroupBox("Gegevens")
        h = QHBoxLayout(g)
        h.addWidget(lab(settings.data_dir(), "hint", wrap=True), 1)
        b = QPushButton("Map openen")
        b.clicked.connect(lambda: _open_folder(settings.data_dir()))
        h.addWidget(b)
        v.addWidget(g)
        v.addStretch(1)
        self._loading = False

    def _secret(self):
        e = QLineEdit()
        e.setEchoMode(QLineEdit.Password)
        return e

    def _test_remote(self):
        import remote
        self.rm_result.setText("Bezig…")
        QApplication.processEvents()
        ok, msg = remote.test(self.rm_url.text(), self.rm_token.text())
        if ok and not self.rm_on.isChecked():
            self.rm_on.setChecked(True)
            msg += " Versturen staat nu aan: klik op Opslaan en start de bot opnieuw."
        self.rm_result.setText(msg)
        self.rm_result.setStyleSheet(f"color: {C['good'] if ok else C['bad']};")

    def _toggle_keys(self, on):
        for e in (self.k_ant, self.k_bv, self.k_bvs, self.k_kr, self.k_krs, self.rm_token):
            e.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)

    def _test_kraken(self):
        import kraken
        try:
            kx = kraken.KrakenPrivate(self.k_kr.text(), self.k_krs.text())
            total, free = kx.collateral_usd()
            rate = kx.pub.eurusd()
            pos = kx.open_positions()
            self.kr_result.setText(f"Verbonden. Futures-account: {eur(total / rate)} (${total:.2f}), "
                                   f"vrij: {eur(free / rate)}. Open posities bij Kraken: {len(pos)}.")
        except Exception as e:
            self.kr_result.setText(f"Lukt niet: {e}")

    def _exchange_changed(self, _i):
        if self._loading or not self.live.isChecked():
            return
        self._loading = True
        self.live.setChecked(False)
        self._loading = False
        QMessageBox.information(self, "Andere beurs",
                                "Live is uitgezet omdat je van beurs wisselt. Zet hem bewust weer aan als je "
                                "op de nieuwe beurs met echt geld wilt handelen.")

    def _confirm_live(self, on):
        if self._loading or not on:
            return
        kr = self.exch.currentIndex() == 1
        ok = QMessageBox.question(
            self, "Live handelen",
            ("Met Live plaatst de bot echte orders met echt geld op Kraken: longs én shorts, met hefboom tot "
             f"{self.lev.value()}x en een stop-loss bij Kraken zelf.\n\n" if kr else
             "Met Live plaatst de bot echte orders met echt geld op Bitvavo.\n\n") +
            "Heb je de bot eerst een tijd droog laten draaien en is het budget een bedrag dat je kunt missen?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok != QMessageBox.Yes:
            self.live.setChecked(False)

    def _update_hint(self):
        if self._loading:
            return
        rules = self.collect_rules()
        parts = []
        for m in [m for m, c in self.m_checks.items() if c.isChecked()]:
            amt, why = risk_gate.size_buy(rules, m, rules["max_trade_eur"], 0)
            parts.append(f"{m.split('-')[0]} max {eur(amt)}")
        text = "Grootste aankoop met deze regels: " + ", ".join(parts) if parts else ""
        too_small = [m.split('-')[0] for m in [m for m, c in self.m_checks.items() if c.isChecked()]
                     if risk_gate.size_buy(rules, m, rules["max_trade_eur"], 0)[0] < rules["min_order_eur"]]
        if too_small:
            text += (f"\n⚠ Voor {', '.join(too_small)} is dat minder dan het Bitvavo-minimum van "
                     f"{eur(rules['min_order_eur'])}: die aankopen worden altijd geblokkeerd. "
                     "Verhoog 'Max verlies per trade'.")
            self.size_hint.setStyleSheet(f"color: {C['accent2']};")
        else:
            self.size_hint.setStyleSheet("")
        self.size_hint.setText(text)

    def load(self, cfg):
        self._loading = True
        k = cfg["keys"]
        self.k_ant.setText(k.get("anthropic", ""))
        self.k_bv.setText(k.get("bitvavo_key", ""))
        self.k_bvs.setText(k.get("bitvavo_secret", ""))
        self.k_kr.setText(k.get("kraken_key", ""))
        self.k_krs.setText(k.get("kraken_secret", ""))
        self.live.setChecked(bool(cfg.get("live")))
        for m, c in self.m_checks.items():
            c.setChecked(m in cfg.get("markets", []))
        self.cycle.setValue(int(cfg.get("cycle_minutes", 120)))
        rc = cfg.get("remote") or {}
        self.rm_on.setChecked(bool(rc.get("enabled")))
        fc = cfg.get("followers") or {}
        self.fw_on.setChecked(bool(fc.get("enabled", True)))
        self.fw_setup.setChecked(bool(fc.get("use_as_setup", True)))
        self.fw_auto.setChecked(bool(fc.get("auto", True)))
        self.fw_invo.setChecked(bool(fc.get("invo", True)))
        self.fw_exit.setChecked(bool(fc.get("exit_on_flip", True)))
        self.fw_count.setValue(int(fc.get("count", 10)))
        self.fw_min.setValue(int(fc.get("min_traders", 3)))
        self.fw_cons.setValue(float(fc.get("min_consensus", 0.6)))
        self.fw_manual.setPlainText("\n".join(fc.get("manual", [])))
        self.fast_on.setChecked(bool(cfg.get("fast_check", True)))
        self.fast_cd.setValue(int(cfg.get("fast_cooldown_min", 30)))
        self.tick.setValue(int(cfg.get("tick_seconds", 30)))
        self.rm_url.setText(rc.get("url", ""))
        self.rm_token.setText(rc.get("token", ""))
        self.model.setCurrentText(cfg.get("model") or settings.MODELS[0])
        self.model_chief.setCurrentText(cfg.get("model_chief") or "(zelfde als agents)")
        self.shorts_on.setChecked(bool(cfg.get("shorts_sim", True)))
        self.exch.setCurrentIndex(1 if cfg.get("exchange") == "kraken" else 0)
        self.lev.setValue(int((cfg.get("kraken") or {}).get("leverage", 3)))
        lbs = cfg.get("live_bronnen") or ["volgers", "beide"]
        for key, c in self.lb_checks.items():
            c.setChecked(key in lbs)
        self.api_cap.setValue(float(cfg.get("max_api_usd_month", 5.0) or 0))
        for key, s in self.r.items():
            s.setValue(cfg["rules"].get(key, settings.DEFAULTS["rules"][key]))
        sp = cfg["rules"].get("stop_pct", {})
        for m, s in self.stops.items():
            s.setValue(float(sp.get(m, sp.get("default", 6))))
        self._loading = False
        self._update_hint()

    def collect_rules(self):
        rules = dict(self.win.cfg["rules"])
        for key, s in self.r.items():
            val = s.value()
            rules[key] = int(val) if isinstance(s, QSpinBox) else round(float(val), 2)
        rules["stop_pct"] = {m: round(s.value(), 1) for m, s in self.stops.items()}
        return rules

    def save(self):
        cfg = self.win.cfg
        cfg["keys"] = {"anthropic": self.k_ant.text().strip(), "bitvavo_key": self.k_bv.text().strip(),
                       "bitvavo_secret": self.k_bvs.text().strip(),
                       "kraken_key": self.k_kr.text().strip(), "kraken_secret": self.k_krs.text().strip()}
        cfg["live"] = self.live.isChecked()
        cfg["live_bronnen"] = [k for k, c in self.lb_checks.items() if c.isChecked()]
        cfg["exchange"] = "kraken" if self.exch.currentIndex() == 1 else "bitvavo"
        cfg["kraken"] = {"leverage": self.lev.value()}
        cfg["markets"] = [m for m, c in self.m_checks.items() if c.isChecked()]
        cfg["cycle_minutes"] = self.cycle.value()
        cfg["model"] = self.model.currentText()
        mc = self.model_chief.currentText()
        cfg["model_chief"] = "" if mc.startswith("(") else mc
        cfg["shorts_sim"] = self.shorts_on.isChecked()
        cfg["max_api_usd_month"] = round(self.api_cap.value(), 2)
        cfg["rules"] = self.collect_rules()
        cfg["followers"] = {"enabled": self.fw_on.isChecked(), "use_as_setup": self.fw_setup.isChecked(),
                            "auto": self.fw_auto.isChecked(), "invo": self.fw_invo.isChecked(),
                            "count": self.fw_count.value(),
                            "exit_on_flip": self.fw_exit.isChecked(),
                            "min_traders": self.fw_min.value(), "min_consensus": round(self.fw_cons.value(), 1),
                            "manual": [x.strip() for x in self.fw_manual.toPlainText().splitlines()
                                       if x.strip().startswith("0x")]}
        cfg["fast_check"] = self.fast_on.isChecked()
        cfg["fast_cooldown_min"] = self.fast_cd.value()
        cfg["tick_seconds"] = self.tick.value()
        cfg["remote"] = {"enabled": self.rm_on.isChecked(), "url": self.rm_url.text().strip(),
                         "token": self.rm_token.text().strip()}
        settings.save(cfg)
        if self.win.engine and self.win.engine.running:
            self.note.setText("Opgeslagen. De bot draait nog met de oude instellingen: stop en start hem om ze te gebruiken.")
        else:
            self.note.setText("Opgeslagen.")
        self.note.show()
        QTimer.singleShot(6000, self.note.hide)
        self.win.refresh()


def _open_folder(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}"')
    except Exception:
        pass


# ======================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Crypto Trader")
        try:
            self.setWindowIcon(QIcon(settings.resource("icon.ico")))
        except Exception:
            pass
        self.resize(1180, 780)
        self.setMinimumSize(980, 640)
        self.cfg = settings.load()
        init_db(settings.db_path())
        self.engine = None
        self.msgq = queue.Queue()
        logs = os.path.join(settings.data_dir(), "logs")
        os.makedirs(logs, exist_ok=True)
        self.logf = open(os.path.join(logs, f"sessie_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"),
                         "a", encoding="utf-8")

        central = QWidget()
        central.setObjectName("page")
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(210)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 20, 14, 16)
        sv.setSpacing(4)
        sv.addWidget(lab("Crypto Trader", "brand"))
        sv.addWidget(lab("AI-agents voor Bitvavo", "brandsub"))
        sv.addSpacing(20)

        self.stack = QStackedWidget()
        self.bot = BotPage(self)
        self.over = OverviewPage(self)
        self.sets = SettingsPage(self)
        self.navs = []
        self.fol = FollowersPage(self)
        self.bt = BacktestPage(self)
        self.cp = CopyPage(self)
        for i, (name, page) in enumerate([("Bot", self.bot), ("Overzicht", self.over), ("Volgers", self.fol),
                                          ("Kopiëren", self.cp), ("Backtest", self.bt), ("Instellingen", self.sets)]):
            page.setObjectName("page")
            self.stack.addWidget(page)
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, x=i: self.go(x))
            sv.addWidget(b)
            self.navs.append(b)
        sv.addStretch(1)
        self.status = lab("● Gestopt", "sub")
        sv.addWidget(self.status)
        sv.addWidget(lab(f"versie {VERSION}", "brandsub"))
        h.addWidget(side)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.sets.load(self.cfg)
        self.cp.load(self.cfg)
        self.go(0)
        if not self.cfg["keys"].get("anthropic"):
            self.go(5)
            self.sets.note.setText("Welkom! Vul eerst je API-keys in en klik op Opslaan. "
                                   "De bot begint altijd met droog draaien (geen echte orders).")
            self.sets.note.show()

        # Backtest-verzoeken van de website (alleen backtests, niets anders)
        try:
            from remote_jobs import JobRunner
            self.jobs = JobRunner(lambda: self.cfg, self.log)
        except Exception:
            self.jobs = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self.slow = QTimer(self)
        self.slow.timeout.connect(self.refresh)
        self.slow.start(5000)
        self.refresh()

    def go(self, i):
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self.navs):
            b.setChecked(i == j)
        if i == 1:
            self.over.refresh()
        if i == 2:
            self.fol.refresh()
        if i == 3:
            self.cp.refresh()

    def log(self, text, kind=""):
        self.msgq.put((text, kind))

    def _tick(self):
        changed = False
        while True:
            try:
                text, kind = self.msgq.get_nowait()
            except queue.Empty:
                break
            self.bot.add_log(text, kind)
            try:
                self.logf.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
                self.logf.flush()
            except Exception:
                pass
            changed = True
        if changed:
            self.refresh()

    def refresh(self):
        running = self.engine is not None and self.engine.running
        live = self.engine.live if running else bool(self.cfg.get("live"))
        if running:
            self.status.setText("● Draait (" + ("LIVE" if live else "droog") + ")")
            self.status.setStyleSheet(f"color: {C['bad'] if live else C['good']};")
        else:
            self.status.setText("● Gestopt")
            self.status.setStyleSheet("")
        self.bot.refresh()
        if self.stack.currentIndex() == 1:
            self.over.refresh()
        if self.stack.currentIndex() == 2:
            self.fol.refresh()

    def toggle(self):
        if self.engine and self.engine.running:
            self.engine.stop()
            self.log("Bot stopt na de lopende stap…", "muted")
            return
        try:
            stop = settings.stop_path()
            if os.path.exists(stop):
                os.remove(stop)
        except Exception:
            pass
        self.engine = Engine(self.cfg, self.log)
        missing = self.engine.problems()
        if missing:
            QMessageBox.warning(self, "Kan niet starten", "Eerst nog regelen:\n\n• " + "\n• ".join(missing))
            self.engine = None
            self.go(5)
            return
        self.engine.start()
        self.refresh()

    def closeEvent(self, e):
        if self.engine and self.engine.running:
            r = QMessageBox.question(
                self, "Afsluiten",
                "De bot draait nog. Na afsluiten worden stop-loss en koersdoel niet meer bewaakt.\n\nToch afsluiten?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.engine.stop()
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
