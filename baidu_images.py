import base64
import io
import os
from pathlib import Path

import requests
from PIL import Image


# Baidu Image Translation API V2.0.
# Endpoint public utilisé par l'API Image Translation.
BAIDU_IMAGE_URL = "https://aip.baidubce.com/file/2.0/mt/pictrans/v1"

TARGET_TO_BAIDU = {
    "fr": "fra",
    "en": "en",
    "es": "spa",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "ja": "jp",
    "ko": "kor",
    "zh-CN": "zh",
    "zh-TW": "cht",
    "ru": "ru",
    "ar": "ara",
}


def _get_api_key():
    api_key = os.getenv("BAIDU_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Baidu n'est pas configuré. Définis BAIDU_API_KEY avant "
            "de lancer l'application."
        )
    return api_key


def _prepare_image(source: Path):
    """Return (bytes, filename, mimetype) acceptable by Baidu."""
    raw = source.read_bytes()
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise RuntimeError(f"Image illisible : {exc}") from exc

    width, height = image.size
    if min(width, height) < 30:
        raise RuntimeError("Image trop petite : le plus petit côté doit faire au moins 30 px.")

    # Baidu limite le plus grand côté à 4096 px et la taille à 4 MiB.
    needs_resize = max(width, height) > 4096
    suffix = source.suffix.lower()
    supported = suffix in {".jpg", ".jpeg", ".png", ".webp"}

    if not needs_resize and supported and len(raw) <= 4 * 1024 * 1024:
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }[suffix]
        return raw, source.name, mime

    if needs_resize:
        scale = 4096 / max(width, height)
        image = image.resize(
            (max(30, round(width * scale)), max(30, round(height * scale))),
            Image.Resampling.LANCZOS,
        )

    # JPEG est utilisé comme format de secours pour rester sous la limite 4 MiB.
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")

    quality = 90
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        raw = buffer.getvalue()
        if len(raw) <= 4 * 1024 * 1024 or quality <= 55:
            break
        quality -= 5

    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("Impossible de réduire l'image sous la limite de 4 Mo de Baidu.")

    return raw, source.stem + ".jpg", "image/jpeg"


def _decode_paste_image(value):
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("data") or value.get("image") or value.get("pasteImg")
    if not isinstance(value, str):
        return None

    value = value.strip()
    if value.startswith("data:image"):
        value = value.split(",", 1)[-1]

    try:
        return base64.b64decode(value, validate=False)
    except Exception:
        return None


def translate_image_with_baidu(source: Path, destination: Path, target: str) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    to_lang = TARGET_TO_BAIDU.get(target)
    if not to_lang:
        raise RuntimeError(f"Langue cible non supportée par Baidu : {target}")

    image_bytes, filename, mime = _prepare_image(source)
    api_key = _get_api_key()

    try:
        response = requests.post(
            BAIDU_IMAGE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            files={"image": (filename, image_bytes, mime)},
            data={
                "from": "auto",
                "to": to_lang,
                "v": "3",
                # Demande à Baidu de renvoyer directement l'image entière
                # avec le texte traduit réinséré.
                "paste": "1",
            },
            timeout=120,
        )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Erreur réseau Baidu : {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Baidu a renvoyé une réponse JSON invalide.") from exc

    error_code = str(result.get("error_code", "0"))
    if error_code != "0":
        message = result.get("error_msg") or "Erreur inconnue"
        raise RuntimeError(f"Baidu ({error_code}) : {message}")

    data = result.get("data") or {}
    pasted = _decode_paste_image(data.get("pasteImg") or result.get("pasteImg"))

    if not pasted:
        raise RuntimeError(
            "Baidu a reconnu/traduit le texte mais n'a pas renvoyé l'image "
            "traduite. Vérifie que le mode 'paste=1' est disponible pour ton compte."
        )

    try:
        translated = Image.open(io.BytesIO(pasted))
        translated.load()
    except Exception as exc:
        raise RuntimeError(f"Image traduite Baidu illisible : {exc}") from exc

    # On conserve le format/extension demandés par l'application.
    suffix = destination.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        if translated.mode not in {"RGB", "L"}:
            translated = translated.convert("RGB")
        translated.save(destination, format="JPEG", quality=95)
    elif suffix == ".png":
        translated.save(destination, format="PNG")
    elif suffix == ".webp":
        translated.save(destination, format="WEBP", quality=95)
    else:
        translated.save(destination)
