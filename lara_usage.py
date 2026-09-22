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


def _key_id(access_key_id):
    return str(access_key_id or "").strip() or "default"


def _mask_key(access_key_id):
    key = str(access_key_id or "").strip()
    if not key:
        return "Clé inconnue"
    if len(key) <= 8:
        return key
    return f"{key[:4]}…{key[-4:]}"


def record_image(access_key_id=None):
    month = _month_key()
    key_id = _key_id(access_key_id)
    with USAGE_LOCK:
        data = _load()
        entry = data.setdefault(month, {"images": 0, "keys": {}})
        entry["images"] = int(entry.get("images", 0)) + 1
        keys = entry.setdefault("keys", {})
        key_entry = keys.setdefault(key_id, {"images": 0})
        key_entry["images"] = int(key_entry.get("images", 0)) + 1
        _save(data)
        return key_entry["images"]


def get_usage(access_key_id=None):
    month = _month_key()
    key_id = _key_id(access_key_id)
    with USAGE_LOCK:
        data = _load()
        entry = data.get(month, {})
        images = int(entry.get("keys", {}).get(key_id, {}).get("images", 0))
    return {
        "month": month,
        "images": images,
        "estimated_cost_eur": round(images * IMAGE_PRICE_EUR, 2),
        "price_eur_per_image": IMAGE_PRICE_EUR,
        "key": _mask_key(access_key_id),
    }
