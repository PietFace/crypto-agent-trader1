# Multi-agent trader (Bitvavo)

Agents: Charts, News, Sentiment, Skeptic -> Chief. Een risicopoort in gewone code (`risk_gate.py` + `rules.json`) heeft het laatste woord.

## Downloaden (Windows, zonder Python)

Ga naar **Releases > Laatste versie** en download `trader-windows.zip`. Pak uit, hernoem `.env.example` naar `.env`, vul je keys in en start `trader.exe`.
De exe wordt automatisch opnieuw gebouwd bij elke wijziging in deze repository.

## Starten met Python

1. Installeer Python 3.10 of nieuwer van python.org (vink "Add Python to PATH" aan).
2. Download deze repository (Code > Download ZIP) en pak uit in een eigen map.
3. Dubbelklik `start.bat`. De eerste keer opent `.env` in Kladblok: vul je Anthropic- en Bitvavo-keys in, sla op en dubbelklik `start.bat` opnieuw.
   Bitvavo-key: alleen "bekijken" + "handelen", nooit "opnemen", plus IP-whitelist.
4. Laat de bot minstens een week droog draaien (`LIVE=false`) en kijk dagelijks in het venster.
5. Tevreden? Zet `LIVE=true` in `.env`, zet €100 op Bitvavo en start opnieuw.

Mac/Linux: `./start.sh` in een terminal.
Zelf een exe bouwen: dubbelklik `build_exe.bat`; daarna staat `trader.exe` met `rules.json` in de map `dist`.

## Regels (rules.json)

| Regel | Waarde |
|---|---|
| Budget / max per trade / posities | €100 / €20 / 3 |
| Max verlies per trade (bepaalt de grootte) | €1 |
| Stop-loss | 5% BTC en ETH, 7% SOL en XRP |
| Koersdoel | 2x de stopafstand (+10% / +14%) |
| Dagrisico (verlies vandaag + open risico) | max €5 |
| Aankopen per dag | max 4 |
| Na een stop-loss | 12 uur niet opnieuw kopen in die munt |
| Spread / Chief-confidence | max 0,4% / min 0,65 |

`python risk_gate.py` toont hoe groot een trade per munt mag worden.
Stop-loss en koersdoel worden elke 5 minuten gecontroleerd. Verkopen wordt nooit geblokkeerd.
Noodrem: maak een leeg bestand `STOP` aan in deze map.

## Bekijken

```
python kosten.py                      (API-kosten per agent en maandschatting)
sqlite3 trader.db "select ts,market,verdict,amount_eur,reasons from risk_log order by id desc limit 20"
sqlite3 trader.db "select ts,market,side,eur,pnl,reason from trades"
sqlite3 trader.db "select ts,btc_hold_eur,bot_eur from benchmark order by id desc limit 20"
```

## Let op

- Experiment, geen beleggingsadvies. De kans dat de bot BTC vasthouden verslaat is klein.
- De stop-loss werkt alleen zolang de computer aan staat en wakker is.
- Handel je zelf ook op dit Bitvavo-account, dan klopt de administratie in `trader.db` niet meer vanzelf.
- De Claude API wordt apart gefactureerd via de Console, buiten je Pro-abonnement.

Risicopoort gebaseerd op de ideeën van Jakeschincariol/day-trader-skill (MIT).
