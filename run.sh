#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo
echo "======================================"
echo " Traducteur d'images"
echo " Baidu Image Translation API"
echo "======================================"
echo " Variables requises : BAIDU_API_KEY et BAIDU_SECRET_KEY"
echo " Local  : http://127.0.0.1:8686"
echo " Réseau : http://$(hostname -I | awk '{print $1}'):8686"
echo
exec .venv/bin/python app.py
