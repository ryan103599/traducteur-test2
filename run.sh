#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
echo
echo "======================================"
echo " Traducteur d'images"
echo " Google Traduction — mode Images"
echo "======================================"
echo " Local  : http://127.0.0.1:8686"
echo " Réseau : http://$(hostname -I | awk '{print $1}'):8686"
echo
exec .venv/bin/python app.py
