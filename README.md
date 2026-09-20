# Traducteur d'images

Version reconstruite à zéro.

L'application utilise **Google Traduction — mode Images** dans un navigateur Chromium automatisé.

## Installation

Sous WSL/Linux :

```bash
bash run.sh
```

Le script crée l'environnement virtuel, installe les dépendances et installe Chromium.

## Utilisation

1. Ouvrir `http://127.0.0.1:8686`.
2. Choisir la langue cible.
3. Sélectionner un dossier d'images.
4. Cliquer sur **Traduire le dossier**.
5. Télécharger le ZIP.

Formats acceptés : JPG, JPEG, PNG et WebP.

## Architecture

Plus de PaddleOCR, deep-translator, MyMemory ou traitement local du texte. Chaque image passe par Google Traduction Images via Playwright, puis l'image traduite est téléchargée et placée dans le ZIP.

Google peut modifier son interface Web, ce qui peut nécessiter une adaptation des sélecteurs.
