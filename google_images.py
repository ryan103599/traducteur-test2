from pathlib import Path
import re
import textwrap

from PIL import Image, ImageDraw, ImageFont
from paddleocr import PaddleOCR
from deep_translator import GoogleTranslator


# This module no longer automates Google Translate's image UI.
# It uses local OCR to read the text, translates the text separately,
# removes the detected text area, then draws the translation back into
# the original image.


_OCR = None

TARGET_TO_TRANSLATOR = {
    "fr": "fr",
    "en": "en",
    "es": "es",
    "de": "de",
    "it": "it",
    "pt": "pt",
    "ja": "ja",
    "ko": "ko",
    "zh-CN": "zh-CN",
    "zh-TW": "zh-TW",
    "ru": "ru",
    "ar": "ar",
}


def _get_ocr():
    global _OCR
    if _OCR is None:
        # English is the main source language currently targeted by the app.
        # PP-OCRv5's English model is optimized for English screenshots/images.
        _OCR = PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            engine="paddle",
        )
    return _OCR


def _extract_ocr(result):
    data = getattr(result, "json", None)
    if callable(data):
        data = data()
    if isinstance(data, list):
        data = data[0] if data else {}
    if isinstance(data, dict) and "res" in data:
        data = data["res"]

    if not isinstance(data, dict):
        return []

    texts = data.get("rec_texts") or []
    scores = data.get("rec_scores") or []
    boxes = data.get("rec_boxes")

    if boxes is None:
        boxes = data.get("rec_polys") or []

    items = []
    for i, text in enumerate(texts):
        text = str(text or "").strip()
        if not text:
            continue

        try:
            score = float(scores[i]) if i < len(scores) else 1.0
        except Exception:
            score = 1.0

        if score < 0.45:
            continue

        try:
            box = boxes[i]
            if len(box) == 4 and not isinstance(box[0], (list, tuple)):
                x1, y1, x2, y2 = [int(v) for v in box]
            else:
                points = [(int(p[0]), int(p[1])) for p in box]
                x1 = min(p[0] for p in points)
                y1 = min(p[1] for p in points)
                x2 = max(p[0] for p in points)
                y2 = max(p[1] for p in points)
        except Exception:
            continue

        items.append({
            "text": text,
            "score": score,
            "box": (max(0, x1), max(0, y1), max(x1 + 1, x2), max(y1 + 1, y2)),
        })

    return items


def _translate(text, target):
    target = TARGET_TO_TRANSLATOR.get(target, target)
    # Translate line-by-line to preserve short OCR fragments.
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    try:
        return GoogleTranslator(source="auto", target=target).translate(cleaned) or cleaned
    except Exception as exc:
        raise RuntimeError(
            f"Traduction du texte impossible ({target}) : {exc}"
        ) from exc


def _font_candidates():
    return [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    ]


def _font(size):
    for path in _font_candidates():
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _background_color(image, box):
    x1, y1, x2, y2 = box
    pixels = []
    sample = max(2, min(8, (x2 - x1) // 8, (y2 - y1) // 8))
    for x in range(x1, min(x2, x1 + sample)):
        pixels.append(image.getpixel((x, y1)))
    for x in range(x1, min(x2, x1 + sample)):
        pixels.append(image.getpixel((x, max(y1, y2 - 1))))
    for y in range(y1, min(y2, y1 + sample)):
        pixels.append(image.getpixel((x1, y)))
    for y in range(y1, min(y2, y1 + sample)):
        pixels.append(image.getpixel((max(x1, x2 - 1), y)))

    if not pixels:
        return (255, 255, 255)

    avg = tuple(sum(p[i] for p in pixels) // len(pixels) for i in range(3))
    return avg


def _draw_translated_text(image, box, translated):
    draw = ImageDraw.Draw(image)
    x1, y1, x2, y2 = box

    # Expand slightly around the detected text so the source lettering is
    # fully covered.
    pad_x = max(3, (x2 - x1) // 12)
    pad_y = max(3, (y2 - y1) // 5)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(image.width, x2 + pad_x)
    y2 = min(image.height, y2 + pad_y)

    fill = _background_color(image, (x1, y1, x2, y2))
    draw.rectangle((x1, y1, x2, y2), fill=fill)

    width = max(10, x2 - x1 - 6)
    height = max(10, y2 - y1 - 4)

    base_size = max(10, min(42, int(height * 0.78)))
    words = translated.split()
    if not words:
        return

    for size in range(base_size, 7, -1):
        font = _font(size)
        lines = []
        current = ""
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

        line_height = max(10, int(size * 1.15))
        if len(lines) * line_height <= height:
            break

    y = y1 + max(0, (height - len(lines) * line_height) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = x1 + max(0, (width - text_width) // 2)
        draw.text((x, y), line, fill=(0, 0, 0), font=font)
        y += line_height


def translate_image_with_google(source: Path, destination: Path, target: str) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        image = Image.open(source).convert("RGB")
    except Exception as exc:
        raise RuntimeError(f"Image illisible : {exc}") from exc

    try:
        ocr = _get_ocr()
        result = next(iter(ocr.predict(str(source))))
        items = _extract_ocr(result)
    except Exception as exc:
        raise RuntimeError(f"OCR impossible : {exc}") from exc

    if not items:
        # No text detected: preserve the image instead of inventing content.
        image.save(destination)
        return

    translated_items = []
    for item in items:
        text = item["text"]
        # Ignore isolated punctuation/noise.
        if len(re.sub(r"[^\wÀ-ÿ一-龥ぁ-んァ-ン]", "", text)) < 2:
            continue
        translated = _translate(text, target)
        if translated and translated.strip() != text.strip():
            translated_items.append((item["box"], translated))

    if not translated_items:
        raise RuntimeError(
            "Le texte a été détecté mais aucune traduction différente n'a été produite."
        )

    # Paint from top to bottom. Each OCR region is handled independently,
    # which works well for subtitles, screenshots, labels and speech bubbles.
    for box, translated in sorted(
        translated_items, key=lambda x: (x[0][1], x[0][0])
    ):
        _draw_translated_text(image, box, translated)

    image.save(destination)
