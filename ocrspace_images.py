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

    lines = []
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
                lines.append({
                    "text": " ".join(w["text"] for w in line_words),
                    "box": (x1, y1, x2, y2),
                    "word_heights": [w["box"][3] - w["box"][1] for w in line_words],
                })

    return _group_lines(lines)


def _group_lines(lines):
    """Group only genuinely adjacent OCR lines; avoid merging distant bubbles."""
    if not lines:
        return []

    lines = sorted(lines, key=lambda item: (item["box"][1], item["box"][0]))
    groups = []

    for line in lines:
        x1, y1, x2, y2 = line["box"]
        h = max(1, y2 - y1)
        placed = None

        for group in reversed(groups):
            gx1, gy1, gx2, gy2 = group["box"]
            gh = max(1, gy2 - gy1)
            gap = y1 - gy2

            # Use the line height, not the accumulated group height, to decide
            # whether two consecutive lines are close enough to be one block.
            max_gap = max(4, min(h, gh) * 0.60)
            if gap < -min(h, gh) * 0.25 or gap > max_gap:
                continue

            overlap = max(0, min(x2, gx2) - max(x1, gx1))
            min_width = max(1, min(x2 - x1, gx2 - gx1))
            horizontal_overlap = overlap / min_width
            center_distance = abs((x1 + x2) / 2 - (gx1 + gx2) / 2)

            # Lines in a bubble are normally aligned or substantially
            # overlapping. Do not merge merely because their left edges happen
            # to be close on a large page.
            close_x = (
                horizontal_overlap >= 0.45
                or center_distance <= max(8, min(h, gh) * 1.2)
            )
            if close_x:
                placed = group
                break

        if placed is None:
            groups.append({
                "lines": [line],
                "box": line["box"],
                "word_heights": list(line["word_heights"]),
            })
        else:
            placed["lines"].append(line)
            bx1, by1, bx2, by2 = placed["box"]
            placed["box"] = (min(bx1, x1), min(by1, y1), max(bx2, x2), max(by2, y2))
            placed["word_heights"].extend(line["word_heights"])

    result = []
    for group in groups:
        group["lines"].sort(key=lambda item: (item["box"][1], item["box"][0]))
        result.append({
            "text": "\n".join(line["text"] for line in group["lines"]),
            "box": group["box"],
            "word_heights": group["word_heights"],
        })
    return sorted(result, key=lambda item: (item["box"][1], item["box"][0]))


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
        parts = re.split(r"(?<=[.!?。！？])\s+|\s+", text.replace("\n", " "))
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
    """Estimate local bubble background from a thin ring around the text box."""
    x1, y1, x2, y2 = box
    pts = []
    for x in range(x1, x2):
        if 0 <= y1 < image.height:
            pts.append(image.getpixel((x, y1)))
        if 0 <= y2 - 1 < image.height:
            pts.append(image.getpixel((x, y2 - 1)))
    for y in range(y1, y2):
        if 0 <= x1 < image.width:
            pts.append(image.getpixel((x1, y)))
        if 0 <= x2 - 1 < image.width:
            pts.append(image.getpixel((x2 - 1, y)))
    if not pts:
        return (255, 255, 255)
    return tuple(sorted(p[i] for p in pts)[len(pts) // 2] for i in range(3))


def _estimate_original_font_size(item):
    heights = [h for h in item.get("word_heights", []) if h >= 4]
    if not heights:
        return 14
    heights.sort()
    return max(10, int(heights[len(heights) // 2] * 1.45))


def _draw(image, box, text, source_font_size, cleanup_boxes=None):
    draw = ImageDraw.Draw(image)
    ox1, oy1, ox2, oy2 = box
    original_height = max(10, oy2 - oy1)
    original_width = max(10, ox2 - ox1)

    # Erase only the original OCR line areas, not the whole grouped bubble.
    for cleanup in cleanup_boxes or [box]:
        cx1, cy1, cx2, cy2 = cleanup
        pad_x, pad_y = 0, 0
        rx1, ry1 = max(0, cx1 - pad_x), max(0, cy1 - pad_y)
        rx2, ry2 = min(image.width, cx2 + pad_x), min(image.height, cy2 + pad_y)
        draw.rectangle((rx1, ry1, rx2, ry2), fill=_background(image, (rx1, ry1, rx2, ry2)))

    pad_x = 1
    pad_y = 1
    x1, y1 = max(0, ox1 - pad_x), max(0, oy1 - pad_y)
    x2, y2 = min(image.width, ox2 + pad_x), min(image.height, oy2 + pad_y)

    available_w = max(20, x2 - x1 - 4)
    available_h = max(20, y2 - y1 - 4)
    paragraphs = text.splitlines() or [text]
    selected = None

    for size in range(max(10, source_font_size), 7, -1):
        font = _font(size)
        all_lines = []
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            current = ""
            for word in paragraph.split():
                candidate = word if not current else current + " " + word
                if draw.textbbox((0, 0), candidate, font=font)[2] <= available_w:
                    current = candidate
                else:
                    if current:
                        all_lines.append(current)
                    current = word
            if current:
                all_lines.append(current)
        line_h = max(10, int(size * 1.12))
        if all_lines and len(all_lines) * line_h <= available_h:
            selected = (font, all_lines, line_h)
            break

    if selected is None:
        selected = (_font(8), [text.replace("\n", " ")], 10)

    font, lines, line_h = selected
    total_h = len(lines) * line_h
    y = y1 + max(0, (available_h - total_h) // 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x1 + max(0, (available_w - tw) // 2), y), line, fill=(0, 0, 0), font=font)
        y += line_h


def translate_image_with_ocrspace(source: Path, destination: Path, target: str, source_lang: str = "auto") -> None:
    source, destination = Path(source), Path(destination)
    with Image.open(source) as original:
        original_size = original.size
        image = original.convert("RGB")

    items = _ocr(source)
    if not items:
        image.save(destination)
        return

    detected = source_lang if source_lang != "auto" else _detect_source([x["text"] for x in items])
    if detected == target:
        image.save(destination)
        return

    cache, translated = {}, []
    for item in items:
        text = item["text"]
        if text not in cache:
            cache[text] = _translate_one(text, detected, target)
        value = cache[text]
        if value and value.strip() != text.strip():
            translated.append((item, value))

    for item, value in translated:
        _draw(image, item["box"], value, _estimate_original_font_size(item), cleanup_boxes=[line["box"] for line in item.get("lines", [])])

    # Never change the source canvas dimensions. The translation is an edit
    # inside the original canvas, not a crop or resize operation.
    if image.size != original_size:
        image = image.resize(original_size, Image.Resampling.LANCZOS)

    image.save(destination)
