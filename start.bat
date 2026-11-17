@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Er is een .env aangemaakt. Vul je API-keys in en start opnieuw.
  notepad .env
  pause
  exit /b
)
if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate
pip install -q -r requirements.txt
python bot.py
pause
