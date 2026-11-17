@echo off
REM Maakt dist\CryptoTrader.exe: een programma zonder Python-installatie nodig.
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name CryptoTrader app.py
echo.
echo Klaar. Start dist\CryptoTrader.exe
pause
