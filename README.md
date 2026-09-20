# Traducteur d'images

Application Flask qui traduit automatiquement un dossier d'images et fournit un ZIP.

## Moteur de traduction

L'application utilise **Baidu Image Translation API**. L'API fait l'OCR, la traduction et la réinsertion du texte dans l'image directement côté service. Baidu documente le mode `paste=1` pour renvoyer l'image entière avec le texte traduit réinséré.

Baidu indique actuellement **1 000 appels gratuits par mois** pour l'API de traduction d'images. Voir la documentation officielle : https://api.fanyi.baidu.com/product/23

## Configuration

L'application a besoin d'une clé API Baidu et d'une clé secrète. Elles ne doivent **pas** être mises dans GitHub.

Sous WSL/Linux :

```bash
export BAIDU_API_KEY="ta_api_key"
export BAIDU_SECRET_KEY="ta_secret_key"
bash run.sh
```

Le programme récupère automatiquement un `access_token` auprès de Baidu et le réutilise pendant sa durée de validité.

## Utilisation

1. Ouvrir `http://127.0.0.1:8686`.
2. Choisir la langue cible.
3. Sélectionner un dossier d'images.
4. Cliquer sur **Traduire le dossier**.
5. Télécharger le ZIP.

Formats acceptés : JPG, JPEG, PNG et WebP.

## Limites Baidu

Pour l'API Image Translation, Baidu indique notamment une taille maximale de 4 Mo par image, un côté maximal de 4096 px et un côté minimal de 30 px. L'application compresse/redimensionne automatiquement les images trop grandes avant l'envoi.

## Sécurité

Les clés API sont lues uniquement depuis les variables d'environnement `BAIDU_API_KEY` et `BAIDU_SECRET_KEY`. Ne les committe jamais dans le dépôt.
