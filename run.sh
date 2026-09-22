#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Charge automatiquement les variables du fichier .env local.
# Le fichier .env n'est pas versionné (voir .gitignore).
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo
echo "======================================"
echo " Traducteur d'images"
echo " Lara Translate Image Translation"
echo "======================================"
echo " Configuration : fichier .env (local)"
PORT="${PORT:-8686}"
echo " Local  : http://127.0.0.1:${PORT}"
echo " Réseau : http://$(hostname -I | awk '{print $1}'):${PORT}"
echo
exec .venv/bin/python app.py
