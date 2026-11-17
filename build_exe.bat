@echo off
REM Maakt dist\trader.exe: een programma zonder Python-installatie nodig.
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements.txt pyinstaller
pyinstaller --onefile --name trader bot.py
copy .env.example dist\.env.example >nul
copy rules.json dist\rules.json >nul
echo.
echo Klaar. Map dist bevat trader.exe, rules.json en .env.example.
echo Kopieer .env.example naar .env in dezelfde map en vul je keys in.
pause
