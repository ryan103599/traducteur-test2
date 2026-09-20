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

def _ocr_key():
    return os.getenv("OCR_SPACE_API_KEY", "helloworld")


def _ocr(source: Path):
    try:
        with source.open("rb") as handle:
            response = requests.post(
                OCRSPACE_URL,
                headers={"apikey": _ocr_key()},
                files={"file": (source.name, handle, "application/octet-stream")},
                data={"language": "auto", "isOverlayRequired": "true", "OCREngine": "2", "scale": "true", "detectOrientation": "true"},
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
                    x, y = int(word.get("Left", 0)), int(word.get("Top", 0))
                    w, h = int(word.get("Width", 0)), int(word.get("Height", 0))
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
    return {"zh-cn": "zh-CN", "zh-tw": "zh-TW"}.get(lang, lang)


def _translate_one(text, source, target):
    if not text.strip() or source == target:
        return text
    if len(text.encode("utf-8")) > 500:
        parts = re.split(r"(?<=[.!?。！？])\s+|\s+", text)
        chunks, current = [], ""
        for part in parts:
            candidate = part if not current else current + " " + part
            if len(candidate.encode("utf-8")) <= 450:
                current = candidate
            else:
                if current: chunks.append(current)
                current = part
        if current: chunks.append(current)
        return " ".join(_translate_one(chunk, source, target) for chunk in chunks)
    try:
        response = requests.get("https://api.mymemory.translated.net/get", params={"q": text, "langpair": f"{source}|{target}", "mt": "1"}, timeout=30)
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
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf"):
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


def _estimate_original_font_size(items):
    """Estimate the actual source character height from individual OCR words."""
    heights = []
    for item in items:
        box = item.get("box")
        if box:
            h = box[3] - box[1]
            if h >= 4:
                heights.append(h)
    if not heights:
        return 14
    heights.sort()
    median = heights[len(heights) // 2]
    # PIL font size is larger than the visible glyph height. This ratio is
    # intentionally based on glyph height, not the whole line bounding box.
    return max(10, int(median * 1.45))


def _draw(image, box, text, source_font_size):
    """Draw at the original vertical size; compress horizontally instead of shrinking the font."""
    draw = ImageDraw.Draw(image)
    ox1, oy1, ox2, oy2 = box
    original_height = max(10, oy2 - oy1)
    original_width = max(10, ox2 - ox1)

    # The original text rectangle is the reference. A small expansion is used
    # only for the background cleanup.
    pad_x = max(4, min(16, original_width // 12))
    pad_y = max(3, min(8, original_height // 4))
    x1, y1 = max(0, ox1 - pad_x), max(0, oy1 - pad_y)
    x2, y2 = min(image.width, ox2 + pad_x), min(image.height, oy2 + pad_y)

    bg = _background(image, (x1, y1, x2, y2))
    draw.rectangle((x1, y1, x2, y2), fill=bg)

    font = _font(max(10, int(source_font_size)))
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_w = max(1, text_bbox[2] - text_bbox[0])
    text_h = max(1, text_bbox[3] - text_bbox[1])

    # IMPORTANT: never reduce the font height because the translation is
    # longer. If it is wider than the source box, render it at the original
    # height and horizontally compress the rendered pixels. This preserves
    # the apparent font size much better than reducing the font to fit.
    target_w = max(10, x2 - x1 - 4)
    target_h = max(10, y2 - y1 - 4)
    scale_x = min(1.0, target_w / text_w)
    rendered_w = max(1, int(text_w * scale_x))
    rendered_h = text_h

    layer = Image.new("RGBA", (text_w + 8, text_h + 8), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.text((4 - text_bbox[0], 4 - text_bbox[1]), text, fill=(0, 0, 0, 255), font=font)

    if scale_x < 1.0:
        layer = layer.resize((rendered_w + 8, rendered_h + 8), Image.Resampling.LANCZOS)
        rendered_w += 0
    paste_x = x1 + max(0, (target_w - rendered_w) // 2)
    paste_y = y1 + max(0, (target_h - rendered_h) // 2)
    image.paste(layer, (paste_x, paste_y), layer)


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

    source_font_size = _estimate_original_font_size(items)

    cache, translated = {}, []
    for item in items:
        text = item["text"]
        if text not in cache:
            cache[text] = _translate_one(text, detected, target)
        value = cache[text]
        if value and value.strip() != text.strip():
            translated.append((item["box"], value))

    for box, value in translated:
        _draw(image, box, value, source_font_size)
    image.save(destination)
