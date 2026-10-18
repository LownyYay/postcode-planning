#!/usr/bin/env sh
# Запуск ОРБИТЫ: окружение .venv в корне репозитория, общее для сервера и ядра.
set -eu
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python orbit/app.py "$@"
