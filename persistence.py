import json
import os
import datetime
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")



def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_history(items):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

def append_history(item: dict, limit: int = 300):
    hist = load_history()
    item = dict(item)
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("date", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    hist.append(item)
    hist = hist[-limit:]
    save_history(hist)

def delete_history_item(item_id: str = "", url: str = "", type_: str = ""):
    hist = load_history()

    if item_id:
        new_hist = [x for x in hist if x.get("id") != item_id]
    else:
        # fallback if id missing
        new_hist = [x for x in hist if not (x.get("url") == url and x.get("type") == type_)]

    save_history(new_hist)


def upsert_fetched_history(item: dict):
    hist = load_history()
    updated = False

    for h in hist:
        if h.get("url") == item.get("url") and h.get("type") == item.get("type"):
            if not h.get("id"):
                h["id"] = str(uuid.uuid4())
            h["title"] = item.get("title", h.get("title"))
            h["status"] = "fetched"
            h["fetched_formats"] = item.get("fetched_formats", [])
            updated = True
            break

    if not updated:
        if not item.get("id"):
            item["id"] = str(uuid.uuid4())
        hist.append(item)

    save_history(hist)


def upsert_history(url: str, type_: str, **updates):
    """
    Keep ONE history record per (url, type_).
    Merge updates into it.
    """
    hist = load_history()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for h in hist:
        if h.get("url") == url and h.get("type") == type_:
            if not h.get("id"):
                h["id"] = str(uuid.uuid4())
            # merge updates
            for k, v in updates.items():
                h[k] = v
            h["last_date"] = now
            save_history(hist)
            return

    # not found -> create new
    item = {
        "id": str(uuid.uuid4()),
        "url": url,
        "type": type_,
        "last_date": now,
    }
    item.update(updates)
    hist.append(item)
    save_history(hist)

def dedupe_history():
    hist = load_history()
    seen = {}
    for item in hist:
        key = (item.get("url"), item.get("type"))
        # keep the latest by last_date/date
        ts = item.get("last_date") or item.get("date") or ""
        if key not in seen or ts > (seen[key].get("last_date") or seen[key].get("date") or ""):
            seen[key] = item

    save_history(list(seen.values()))

