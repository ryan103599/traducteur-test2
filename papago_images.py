import base64
import io
import os
from pathlib import Path

import requests
from PIL import Image

PAPAGO_URL = "https://papago.apigw.ntruss.com/image-to-image/v1/translate"

SUPPORTED_SOURCE = {
    "auto", "ko", "en", "ja", "zh-CN", "zh-TW",
    "vi", "th", "id", "fr", "es", "ru",
}
SUPPORTED_TARGET = SUPPORTED_SOURCE - {"auto"} | {"de", "it"}

def _credentials():
    client_id = os.getenv("NAVER_CLOUD_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_CLOUD_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Identifiants NAVER Cloud manquants. Ajoute "
            "NAVER_CLOUD_CLIENT_ID et NAVER_CLOUD_CLIENT_SECRET dans .env."
        )
    return client_id, client_secret


def _prepare_image(source: Path):
    with Image.open(source) as original:
        original_size = original.size
        image = original.convert("RGB")

    # Papago accepte jusqu'à 1960x1960. On réduit uniquement les images
    # qui dépassent cette limite, puis on remet le résultat à la taille
    # originale après traduction.
    scale = min(1.0, 1960 / max(image.size))
    if scale < 1:
        size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        image = image.resize(size, Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), original_size


def translate_image_with_papago(
    source: Path, destination: Path, target: str, source_lang: str = "auto"
) -> None:
    source, destination = Path(source), Path(destination)

    if source_lang not in SUPPORTED_SOURCE:
        raise RuntimeError(f"Langue source non supportée par Papago : {source_lang}")
    if target not in SUPPORTED_TARGET:
        raise RuntimeError(f"Langue cible non supportée par Papago : {target}")
    if source_lang != "auto" and source_lang == target:
        with Image.open(source) as image:
            image.save(destination)
        return

    client_id, client_secret = _credentials()
    image_bytes, original_size = _prepare_image(source)

    try:
        response = requests.post(
            PAPAGO_URL,
            headers={
                "X-NCP-APIGW-API-KEY-ID": client_id,
                "X-NCP-APIGW-API-KEY": client_secret,
            },
            files={
                "image": (
                    source.with_suffix(".png").name,
                    image_bytes,
                    "image/png",
                )
            },
            data={"source": source_lang, "target": target},
            timeout=180,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = ""
        if getattr(exc, "response", None) is not None:
            detail = exc.response.text[:500]
        raise RuntimeError(f"Papago inaccessible : {exc}. {detail}") from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Papago a renvoyé une réponse JSON invalide.") from exc

    data = result.get("data") or {}
    rendered = data.get("renderedImage")
    if not rendered:
        raise RuntimeError(
            f"Papago n'a pas renvoyé d'image traduite. Réponse : {result}"
        )

    try:
        translated_bytes = base64.b64decode(rendered)
        with Image.open(io.BytesIO(translated_bytes)) as translated:
            translated = translated.convert("RGB")
            if translated.size != original_size:
                translated = translated.resize(
                    original_size, Image.Resampling.LANCZOS
                )
            # Papago renvoie l'image traduite en base64. On sauvegarde
            # directement ce rendu, sans OCR, rectangles ou réinjection locale.
            translated.save(destination, format="PNG")
    except Exception as exc:
        raise RuntimeError(f"Image traduite Papago invalide : {exc}") from exc
