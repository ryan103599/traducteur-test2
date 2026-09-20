# traducteur-test2

Application web locale pour traduire automatiquement les textes présents dans un dossier d'images.

## Fonctionnement

1. Lance `bash run.sh`.
2. Ouvre http://127.0.0.1:8686.
3. Choisis un dossier d'images.
4. Sélectionne la langue cible.
5. Clique sur **Traduire le dossier**.
6. Quand le traitement est terminé, récupère `traduction_<langue>.zip`.

Le moteur OCR est PaddleOCR et la traduction du texte extrait utilise Google Traduction via `deep-translator`. L'image est reconstruite en supprimant le texte détecté puis en dessinant la traduction à sa place.

## Dépendances système

Sous Ubuntu/WSL :
```bash
sudo apt update
sudo apt install -y python3 python3-venv
```

Puis :
```bash
bash run.sh
```

La traduction utilise le service web de Google Traduction via la bibliothèque `deep-translator`; aucune clé Google Cloud n'est demandée.
