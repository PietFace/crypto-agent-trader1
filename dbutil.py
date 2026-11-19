"""Eén manier om de database te openen: met WAL (lezen en schrijven blokkeren elkaar niet meer) en een ruime
wachttijd. Voorkomt 'database is locked' als de bot, het kopiëren, het scherm en de Claude-map tegelijk bezig zijn."""
import sqlite3


def connect(path, timeout=30):
    con = sqlite3.connect(path, timeout=timeout)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return con
