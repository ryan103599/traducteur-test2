# Traducteur d'images

Application Flask qui traduit automatiquement un dossier d'images et fournit un ZIP.

## Moteur de traduction

L'application utilise **Lara Translate** pour traduire les images et réinsérer le texte traduit directement dans les images.

## Configuration de la clé Lara

La clé Lara doit être configurée dans le fichier **.env** sur la machine où l'application tourne.

Ajoute :

```env
LARA_ACCESS_KEY_ID="ta_access_key_id"
LARA_ACCESS_KEY_SECRET="ta_access_key_secret"
```

Puis redémarre l'application :

```bash
bash run.sh
```

### Où récupérer les clés Lara ?

Connecte-toi à ton compte Lara Translate et crée/récupère tes identifiants API (**Access Key ID** et **Access Key Secret**).

**Ne mets jamais tes clés Lara directement dans le code ni dans GitHub.** Le fichier `.env` doit rester local et ne doit pas être committe.

Si tu utilises Git, vérifie que `.env` est bien présent dans `.gitignore`.

## Utilisation

1. Ouvrir `http://127.0.0.1:8686`.
2. Choisir la langue cible.
3. Sélectionner un dossier d'images.
4. Cliquer sur **Traduire le dossier**.
5. Télécharger le ZIP.

Formats acceptés : JPG, JPEG, PNG et WebP.

## Sécurité

Les identifiants Lara sont lus uniquement depuis les variables d'environnement :

- `LARA_ACCESS_KEY_ID`
- `LARA_ACCESS_KEY_SECRET`

Ne les committe jamais dans le dépôt GitHub.
## Changer le port du serveur

Le port utilisé par le site peut être modifié facilement dans le fichier local **.env**.

Ajoute ou modifie :

```env
PORT=8686
```

Par exemple, pour utiliser le port 9000 :

```env
PORT=9000
```

Puis redémarre l'application avec :

```bash
bash run.sh
```

Le fichier **.env** reste local et n'est pas envoyé sur GitHub.
