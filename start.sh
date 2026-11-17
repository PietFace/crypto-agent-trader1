#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Er is een .env aangemaakt. Vul je API-keys in en start opnieuw."
  exit 0
fi
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
python bot.py
