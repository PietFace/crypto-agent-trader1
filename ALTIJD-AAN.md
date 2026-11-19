# CryptoTrader altijd aan (Raspberry Pi of server)

Op je pc stopt de bot zodra de pc uitgaat. Dan bewaakt niemand je posities, en de kopieer-modus mist
alles wat 's nachts gebeurt. Met een apparaat dat altijd aan staat, loopt hij gewoon door. Je telefoon-website
blijft precies hetzelfde werken.

## Wat je nodig hebt
- Een **Raspberry Pi 4 of 5** (2 GB is genoeg) met Raspberry Pi OS, of een kleine **VPS** (bijv. Hetzner CX22,
  ongeveer €4 per maand). Een VPS heeft een vast IP-adres, dat maakt de IP-whitelist bij Bitvavo/Kraken makkelijk.
- Je instellingen van de pc: `%APPDATA%\CryptoTrader\config.json` en `trader.db`.

## Installeren (één keer)
```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone https://github.com/PietFace/crypto-agent-trader1.git ~/cryptotrader
cd ~/cryptotrader
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-headless.txt
mkdir -p ~/.crypto_trader
```
Zet daarna `config.json` en `trader.db` van je pc in `~/.crypto_trader/` (bijv. met WinSCP of een USB-stick).

## Proberen
```bash
cd ~/cryptotrader && . .venv/bin/activate && python3 run_headless.py
```
Je ziet hetzelfde logboek als in het programma. Stoppen met Ctrl-C.

## Automatisch starten (ook na stroomuitval)
```bash
sudo tee /etc/systemd/system/cryptotrader.service >/dev/null <<UNIT
[Unit]
Description=CryptoTrader
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/cryptotrader
ExecStart=$HOME/cryptotrader/.venv/bin/python3 run_headless.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now cryptotrader
journalctl -u cryptotrader -f      # logboek bekijken
```

## Belangrijk
- **Nooit tegelijk op pc én Pi laten draaien.** Dan opent hij alles dubbel. Gebruik op de pc alleen nog de
  backtest, of zet de bot daar niet meer aan.
- **IP-whitelist**: zet het IP-adres van de Pi/server bij je Bitvavo- en Kraken-sleutel.
- Instellingen wijzigen: op de pc aanpassen, `config.json` opnieuw kopiëren en `sudo systemctl restart cryptotrader`.
- Bijwerken naar een nieuwe versie: `cd ~/cryptotrader && git pull && sudo systemctl restart cryptotrader`.
