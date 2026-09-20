import os
import re
import threading
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import argostranslate.package
    import argostranslate.translate
except ImportError as exc:
    raise RuntimeError(
        "Argos Translate n'est pas installé. Lance run.sh pour installer les dépendances."
    ) from exc

from paddleocr import PaddleOCR

try:
    from langdetect import detect
except ImportError:
    detect = None


# PaddleOCR 2.x language names.
OCR_LANGS = {
    "en": "en",
    "fr": "french",
    "de": "german",
    "es": "es",
    "it": "it",
    "pt": "pt",
    "ja": "japan",
    "ko": "korean",
    "zh-CN": "ch",
    "zh-TW": "chinese_cht",
    "ru": "ru",
    "ar": "ar",
}

ARGOS_CODES = {
    "fr": "fr",
    "en": "en",
    "es": "es",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "ja": "ja",
    "ko": "ko",
    "zh-CN": "zh",
    "zh-TW": "zh",
    "ru": "ru",
    "ar": "ar",
}

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

_PACKAGE_LOCK = threading.Lock()
_OCR_LOCK = threading.Lock()


def _font_path():
    configured = os.getenv("TRANSLATOR_FONT")
    if configured and Path(configured).exists():
        return configured
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


@lru_cache(maxsize=16)
def _ocr_engine(lang_name):
    # Models are loaded lazily so the web app starts quickly.
    return PaddleOCR(
        use_angle_cls=True,
        lang=lang_name,
        use_gpu=False,
        show_log=False,
    )


def _run_ocr(source: Path, lang_code: str):
    lang_name = OCR_LANGS.get(lang_code, "en")
    engine = _ocr_engine(lang_name)
    with _OCR_LOCK:
        result = engine.ocr(str(source), cls=True)

    lines = []
    if not result:
        return lines

    for page in result:
        if not page:
            continue
        for item in page:
            try:
                box, (text, score) = item
            except (TypeError, ValueError):
                continue
            if not text or float(score) < 0.35:
                continue
            lines.append(
                {
                    "box": box,
                    "text": str(text).strip(),
                    "score": float(score),
                }
            )
    return lines


def _script(text):
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u0400-\u04ff]", text):
        return "ru"
    if re.search(r"[\u0600-\u06ff]", text):
        return "ar"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-CN"
    return None


def detect_source_and_ocr(source: Path, requested: str = "auto"):
    if requested != "auto":
        return requested, _run_ocr(source, requested)

    # The Chinese/English detector is a useful first pass for Latin and CJK.
    first = _run_ocr(source, "zh-CN")
    sample = " ".join(x["text"] for x in first)
    script = _script(sample)

    # Re-run with the appropriate specialist model when the first pass
    # reveals a script that needs one.
    if script in {"ja", "ko", "ru", "ar"}:
        return script, _run_ocr(source, script)

    if first:
        if detect is not None:
            try:
                detected = detect(sample)
                if detected in ARGOS_CODES:
                    return detected, first
            except Exception:
                pass
        return "en", first

    # If the first pass found nothing, try the non-Latin models.
    for candidate in ("ja", "ko", "ru", "ar"):
        found = _run_ocr(source, candidate)
        if found:
            sample = " ".join(x["text"] for x in found)
            return candidate, found

    return "en", []


def _installed_pair(from_code, to_code):
    for package in argostranslate.package.get_installed_packages():
        if (
            getattr(package, "from_code", None) == from_code
            and getattr(package, "to_code", None) == to_code
        ):
            return True
    return False


def _available_pair(from_code, to_code):
    packages = argostranslate.package.get_available_packages()
    for package in packages:
        if package.from_code == from_code and package.to_code == to_code:
            return package
    return None


def _ensure_pair(from_code, to_code):
    if from_code == to_code:
        return

    with _PACKAGE_LOCK:
        if _installed_pair(from_code, to_code):
            return

        try:
            package = _available_pair(from_code, to_code)
        except Exception:
            argostranslate.package.update_package_index()
            package = _available_pair(from_code, to_code)

        if package is None:
            raise RuntimeError(
                f"Modèle Argos indisponible pour {from_code} → {to_code}."
            )

        package.install()


def _translate_one(text, from_code, to_code):
    if not text or from_code == to_code:
        return text

    _ensure_pair(from_code, to_code)
    return argostranslate.translate.translate(text, from_code, to_code)


def translate_texts(texts, from_code, to_code):
    if from_code == to_code:
        return list(texts)

    translated = []
    for text in texts:
        try:
            translated.append(_translate_one(text, from_code, to_code))
        except RuntimeError:
            # Argos can use an intermediate language, but if the direct
            # package is absent, install the common English pivot.
            if from_code != "en" and to_code != "en":
                _ensure_pair(from_code, "en")
                _ensure_pair("en", to_code)
                translated.append(
                    argostranslate.translate.translate(text, from_code, to_code)
                )
            else:
                raise
    return translated


def _background_color(image, polygon):
    xs = [int(p[0]) for p in polygon]
    ys = [int(p[1]) for p in polygon]
    left, right = max(0, min(xs)), min(image.width - 1, max(xs))
    top, bottom = max(0, min(ys)), min(image.height - 1, max(ys))

    samples = []
    margin = max(2, min(12, int(max(right - left, bottom - top) * 0.15)))
    for x in range(max(0, left - margin), min(image.width, right + margin + 1)):
        for y in (max(0, top - margin), min(image.height - 1, bottom + margin)):
            samples.append(image.getpixel((x, y))[:3])
    for y in range(max(0, top - margin), min(image.height, bottom + margin + 1)):
        for x in (max(0, left - margin), min(image.width - 1, right + margin)):
            samples.append(image.getpixel((x, y))[:3])

    if not samples:
        return (255, 255, 255)

    return tuple(
        sorted(pixel[channel] for pixel in samples)[len(samples) // 2]
        for channel in range(3)
    )


def _fit_font(text, box_width, box_height, font_path):
    max_size = max(10, int(box_height * 0.85))
    min_size = 8
    while max_size >= min_size:
        font = ImageFont.truetype(font_path, max_size) if font_path else ImageFont.load_default()
        bbox = font.getbbox(text or " ")
        if bbox[2] - bbox[0] <= max(10, box_width * 0.92) and bbox[3] - bbox[1] <= max(10, box_height * 0.9):
            return font
        max_size -= 1
    return ImageFont.truetype(font_path, min_size) if font_path else ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return [text]

    lines = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _paint_text(image, polygon, text, source_text):
    xs = [int(p[0]) for p in polygon]
    ys = [int(p[1]) for p in polygon]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    width = max(8, right - left)
    height = max(8, bottom - top)

    draw = ImageDraw.Draw(image)
    draw.polygon(polygon, fill=_background_color(image, polygon))

    font_path = _font_path()
    font = _fit_font(text, width, height, font_path)
    lines = _wrap_text(draw, text, font, width * 0.92)

    # Reduce the font until the wrapped text fits vertically.
    while len(lines) * (font.getbbox("Ag")[3] - font.getbbox("Ag")[1] + 2) > height * 0.9:
        size = max(8, getattr(font, "size", 12) - 1)
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        lines = _wrap_text(draw, text, font, width * 0.92)
        if size <= 8:
            break

    line_height = max(1, font.getbbox("Ag")[3] - font.getbbox("Ag")[1] + 2)
    total_height = line_height * len(lines)
    y = top + max(0, (height - total_height) // 2)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = left + max(0, (width - tw) // 2)
        draw.text((x, y), line, fill=(0, 0, 0), font=font)
        y += line_height


def translate_image_locally(source: Path, destination: Path, target: str, source_lang="auto"):
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    image = Image.open(source).convert("RGB")
    detected_source, entries = detect_source_and_ocr(source, source_lang)

    if not entries:
        raise RuntimeError("Aucun texte détecté dans l'image.")

    target_code = ARGOS_CODES.get(target)
    if not target_code:
        raise RuntimeError(f"Langue cible non supportée : {target}")

    texts = [entry["text"] for entry in entries]
    translated = translate_texts(texts, ARGOS_CODES.get(detected_source, detected_source), target_code)

    for entry, translated_text in zip(entries, translated):
        if not translated_text or translated_text.strip() == entry["text"].strip():
            continue
        _paint_text(image, entry["box"], translated_text, entry["text"])

    suffix = destination.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        image.save(destination, format="JPEG", quality=95)
    elif suffix == ".png":
        image.save(destination, format="PNG")
    elif suffix == ".webp":
        image.save(destination, format="WEBP", quality=95)
    else:
        image.save(destination)

    return detected_source, len(entries)
