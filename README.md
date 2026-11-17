# Crypto Trader (Bitvavo)

Programma met AI-agents (Charts, News, Sentiment, Skeptic -> Chief) die advies geven over BTC, ETH, SOL en XRP. Een risicopoort in gewone code (`risk_gate.py`) heeft altijd het laatste woord.

## Downloaden (Windows)

1. Ga naar **Releases > Laatste versie** en download `trader-windows.zip`.
2. Pak uit en start `CryptoTrader.exe`. Windows waarschuwt mogelijk de eerste keer: kies **Meer info > Toch uitvoeren**.
3. Vul onder **Instellingen** je Anthropic- en Bitvavo-keys in en klik **Opslaan**.
   Bitvavo-key: alleen "bekijken" + "handelen", nooit "opnemen", plus IP-whitelist.
4. Ga naar **Bot** en klik **Start bot**. Hij begint altijd met droog draaien (geen echte orders).
5. Na een tijd droog draaien kun je in Instellingen **Live** aanzetten.

De exe wordt automatisch opnieuw gebouwd bij elke wijziging in deze repository. Instellingen, keys en de database (`trader.db`) staan in `%APPDATA%\CryptoTrader`, dus een nieuwe versie neemt alles over.

## In het programma

- **Bot**: start/stop, de stand tegen BTC vasthouden, resultaat vandaag, API-kosten, open posities met stop-loss en koersdoel, en het logboek.
- **Overzicht**: alle trades, elk oordeel van de risicopoort en de API-kosten per agent met een maandschatting.
- **Instellingen**: keys, live of droog, markten, hoe vaak de agents analyseren, model, alle risicoregels en de stop-loss per munt.

## Standaardregels

| Regel | Waarde |
|---|---|
| Budget / max per aankoop / posities | €100 / €20 / 3 |
| Max verlies per trade (bepaalt de grootte) | €1 |
| Stop-loss | 5% BTC en ETH, 7% SOL en XRP |
| Koersdoel | 2x de stopafstand (+10% / +14%) |
| Daglimiet (verlies vandaag + open risico) | €5 |
| Aankopen per dag | max 4 |
| Na een stop-loss | 12 uur niet opnieuw kopen in die munt |
| Spread / zekerheid Chief | max 0,4% / min 0,65 |

Stop-loss en koersdoel worden elke 5 minuten gecontroleerd. Verkopen wordt nooit geblokkeerd.

## Met Python starten

`start.bat` (Windows) of `./start.sh` (Mac/Linux) installeert alles en opent het programma. Zelf een exe bouwen: `build_exe.bat`.

## Let op

- Experiment, geen beleggingsadvies. De kans dat de bot BTC vasthouden verslaat is klein.
- Stop-loss en koersdoel werken alleen zolang het programma open staat en de computer wakker is.
- Handel je zelf ook op dit Bitvavo-account, dan klopt de administratie in `trader.db` niet meer vanzelf.
- De Claude API wordt apart gefactureerd via de Console, buiten je Pro-abonnement.

Risicopoort gebaseerd op de ideeën van Jakeschincariol/day-trader-skill (MIT).
