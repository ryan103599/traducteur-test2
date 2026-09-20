import json
import threading
from datetime import datetime
from pathlib import Path

USAGE_FILE = Path(__file__).resolve().parent / "data" / "lara_usage.json"
USAGE_LOCK = threading.Lock()
IMAGE_PRICE_EUR = 0.20


def _month_key():
    return datetime.now().strftime("%Y-%m")


def _load():
    if not USAGE_FILE.exists():
        return {}
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(USAGE_FILE)


def record_image():
    month = _month_key()
    with USAGE_LOCK:
        data = _load()
        entry = data.setdefault(month, {"images": 0})
        entry["images"] = int(entry.get("images", 0)) + 1
        _save(data)
        return entry["images"]


def get_usage():
    month = _month_key()
    with USAGE_LOCK:
        data = _load()
        images = int(data.get(month, {}).get("images", 0))
    return {
        "month": month,
        "images": images,
        "estimated_cost_eur": round(images * IMAGE_PRICE_EUR, 2),
        "price_eur_per_image": IMAGE_PRICE_EUR,
    }
