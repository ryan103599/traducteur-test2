import os
from pathlib import Path

from lara_sdk import AccessKey, Translator


# Lara utilise les codes régionaux complets. L'interface de l'application
# conserve des codes courts pour rester simple.
LANGUAGE_CODES = {
    "fr": "fr-FR",
    "en": "en-US",
    "es": "es-ES",
    "de": "de-DE",
    "it": "it-IT",
    "vi": "vi-VN",
    "th": "th-TH",
    "id": "id-ID",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh-CN": "zh-CN",
    "zh-TW": "zh-TW",
    "ru": "ru-RU",
    "ar": "ar-SA",
}


def _translator():
    access_key_id = os.getenv("LARA_ACCESS_KEY_ID", "").strip()
    access_key_secret = os.getenv("LARA_ACCESS_KEY_SECRET", "").strip()
    if not access_key_id or not access_key_secret:
        raise RuntimeError(
            "Identifiants Lara manquants. Ajoute LARA_ACCESS_KEY_ID et "
            "LARA_ACCESS_KEY_SECRET dans .env."
        )
    credentials = AccessKey(access_key_id, access_key_secret)
    return Translator(credentials)


def translate_image_with_lara(
    source: Path, destination: Path, target: str, source_lang: str = "auto"
) -> bool:
    source = Path(source)
    destination = Path(destination)

    target_code = LANGUAGE_CODES.get(target)
    if not target_code:
        raise RuntimeError(f"Langue cible non supportée par Lara : {target}")

    if source_lang == "auto":
        source_code = None
    else:
        source_code = LANGUAGE_CODES.get(source_lang)
        if not source_code:
            raise RuntimeError(f"Langue source non supportée par Lara : {source_lang}")

    if source_lang != "auto" and source_lang == target:
        destination.write_bytes(source.read_bytes())
        return False

    try:
        lara = _translator()
        translated = lara.images.translate(
            source=source_code,
            target=target_code,
            image_path=str(source),
            model="inpainting",
            style="faithful",
            no_trace=True,
        )
    except Exception as exc:
        raise RuntimeError(f"Lara n'a pas pu traduire l'image : {exc}") from exc

    if not translated:
        raise RuntimeError("Lara n'a renvoyé aucune image traduite.")

    try:
        destination.write_bytes(translated)
    except Exception as exc:
        raise RuntimeError(f"Impossible d'enregistrer l'image Lara : {exc}") from exc

    return True
