#!/usr/bin/env bash
set -e
VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1; then
  echo "Création de l'environnement virtuel Python..."
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR" || { echo "Installe python3-venv : sudo apt install python3-venv"; exit 1; }
fi
PYTHON="$VENV_DIR/bin/python"
echo "Installation / mise à jour des dépendances..."
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements.txt
echo "Serveur local : http://127.0.0.1:8686"
echo "Serveur réseau : http://$(hostname -I | awk '{print $1}'):8686"
echo "Le serveur écoute sur toutes les interfaces réseau."
"$PYTHON" app.py
