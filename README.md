# Traducteur d'images

Application Flask qui traduit automatiquement un dossier d'images et fournit un ZIP.

## Moteur de traduction

L'application utilise **Baidu Image Translation API**. L'API fait l'OCR, la traduction et la réinsertion du texte dans l'image directement côté service. Baidu documente le mode `paste=1` pour renvoyer l'image entière avec le texte traduit réinséré.

Baidu indique actuellement **1 000 appels gratuits par mois** pour l'API de traduction d'images. Voir la documentation officielle : https://api.fanyi.baidu.com/product/23

## Configuration

L'application utilise la nouvelle authentification Baidu **API Key (bce-v3)**. Elle n'a besoin que de la valeur **API Key** ; aucune Secret Key n'est nécessaire avec ce mode d'authentification.

Sous WSL/Linux :

```bash
export BAIDU_API_KEY="ta_api_key"
bash run.sh
```

L'API Key est envoyée dans l'en-tête HTTP :

```
Authorization: Bearer <API_KEY>
```

Ne mets jamais ta clé dans GitHub.

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

La clé API est lue uniquement depuis la variable d'environnement `BAIDU_API_KEY`. Ne la committe jamais dans le dépôt.
