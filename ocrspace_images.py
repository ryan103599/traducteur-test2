import io
import os
import re
import time
from pathlib import Path

import requests
from langdetect import DetectorFactory, detect
from PIL import Image, ImageDraw, ImageFont

OCRSPACE_URL = "https://api.ocr.space/parse/image"

LANGUAGES = {
    "fr": "fr", "en": "en", "es": "es", "de": "de", "it": "it",
    "pt": "pt", "ja": "ja", "ko": "ko", "zh-CN": "zh-CN",
    "zh-TW": "zh-TW", "ru": "ru", "ar": "ar",
}

# OCR.space provides a free API. A personal key is recommended.
# The public "helloworld" key is intentionally only a fallback for quick tests.
def _ocr_key():
    return os.getenv("OCR_SPACE_API_KEY", "helloworld")


def _ocr(source: Path):
    try:
        with source.open("rb") as handle:
            response = requests.post(
                OCRSPACE_URL,
                headers={"apikey": _ocr_key()},
                files={"file": (source.name, handle, "application/octet-stream")},
                data={
                    "language": "auto",
                    "isOverlayRequired": "true",
                    "OCREngine": "2",
                    "scale": "true",
                    "detectOrientation": "true",
                },
                timeout=120,
            )
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"OCR.space inaccessible : {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("OCR.space a renvoyé une réponse JSON invalide.") from exc

    if result.get("IsErroredOnProcessing"):
        errors = result.get("ErrorMessage") or result.get("ErrorDetails") or "Erreur inconnue"
        raise RuntimeError(f"OCR.space : {errors}")

    parsed = result.get("ParsedResults") or []
    if not parsed:
        raise RuntimeError("OCR.space n'a détecté aucun texte.")

    words = []
    for parsed_result in parsed:
        overlay = parsed_result.get("TextOverlay") or {}
        for line in overlay.get("Lines") or []:
            line_words = []
            for word in line.get("Words") or []:
                text = str(word.get("WordText") or "").strip()
                if not text:
                    continue
                try:
                    x = int(word.get("Left", 0))
                    y = int(word.get("Top", 0))
                    w = int(word.get("Width", 0))
                    h = int(word.get("Height", 0))
                except (TypeError, ValueError):
                    continue
                if w > 0 and h > 0:
                    line_words.append({"text": text, "box": (x, y, x + w, y + h)})
            if line_words:
                x1 = min(w["box"][0] for w in line_words)
                y1 = min(w["box"][1] for w in line_words)
                x2 = max(w["box"][2] for w in line_words)
                y2 = max(w["box"][3] for w in line_words)
                words.append({"text": " ".join(w["text"] for w in line_words), "box": (x1, y1, x2, y2)})
    return words


def _detect_source(texts):
    joined = " ".join(texts).strip()
    if not joined:
        return "en"
    try:
        lang = detect(joined)
    except Exception:
        lang = "en"
    aliases = {"zh-cn": "zh-CN", "zh-tw": "zh-TW", "ko": "ko", "ja": "ja"}
    return aliases.get(lang, lang)


def _translate_one(text, source, target):
    if not text.strip():
        return text
    if source == target:
        return text
    # MyMemory accepts at most 500 bytes per request.
    if len(text.encode("utf-8")) > 500:
        parts = re.split(r"(?<=[.!?。！？])\s+|\s+", text)
        chunks, current = [], ""
        for part in parts:
            candidate = part if not current else current + " " + part
            if len(candidate.encode("utf-8")) <= 450:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = part
        if current:
            chunks.append(current)
        return " ".join(_translate_one(chunk, source, target) for chunk in chunks)

    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": f"{source}|{target}", "mt": "1"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Service de traduction inaccessible : {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("Le service de traduction a renvoyé du JSON invalide.") from exc

    if data.get("responseStatus") not in (200, "200", None):
        raise RuntimeError(f"MyMemory : {data.get('responseDetails') or 'erreur inconnue'}")
    translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not translated:
        raise RuntimeError("MyMemory n'a pas renvoyé de traduction.")
    time.sleep(0.15)
    return translated


def _font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    ):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _background(image, box):
    x1, y1, x2, y2 = box
    pts = []
    for x in range(x1, min(x2, x1 + 6)):
        pts += [image.getpixel((x, y1)), image.getpixel((x, max(y1, y2 - 1)))]
    for y in range(y1, min(y2, y1 + 6)):
        pts += [image.getpixel((x1, y)), image.getpixel((max(x1, x2 - 1), y))]
    if not pts:
        return (255, 255, 255)
    return tuple(sum(p[i] for p in pts) // len(pts) for i in range(3))


def _draw(image, box, text):
    draw = ImageDraw.Draw(image)
    x1, y1, x2, y2 = box
    pad = max(3, min(12, (x2 - x1) // 12))
    x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
    x2, y2 = min(image.width, x2 + pad), min(image.height, y2 + pad)
    draw.rectangle((x1, y1, x2, y2), fill=_background(image, (x1, y1, x2, y2)))
    width, height = max(10, x2 - x1 - 6), max(10, y2 - y1 - 4)
    words = text.split()
    if not words:
        return
    for size in range(max(10, min(42, int(height * .8))), 7, -1):
        font = _font(size)
        lines, current = [], ""
        for word in words:
            candidate = word if not current else current + " " + word
            if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        line_h = max(10, int(size * 1.15))
        if len(lines) * line_h <= height:
            break
    y = y1 + max(0, (height - len(lines) * line_h) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x1 + max(0, (width - tw) // 2), y), line, fill=(0, 0, 0), font=font)
        y += line_h


def translate_image_with_ocrspace(source: Path, destination: Path, target: str, source_lang: str = "auto") -> None:
    source, destination = Path(source), Path(destination)
    image = Image.open(source).convert("RGB")
    items = _ocr(source)
    if not items:
        image.save(destination)
        return

    detected = source_lang if source_lang != "auto" else _detect_source([x["text"] for x in items])
    if detected == target:
        image.save(destination)
        return

    cache = {}
    translated = []
    for item in items:
        text = item["text"]
        if text not in cache:
            cache[text] = _translate_one(text, detected, target)
        value = cache[text]
        if value and value.strip() != text.strip():
            translated.append((item["box"], value))
    for box, value in translated:
        _draw(image, box, value)
    image.save(destination)
