"""Multi-layer element identity: how many independent ways can one element be recognised?

Every layer answers the same question — "do I see this element, and by what?" — and writes
its answer into one shared shape. The radar card, the stored fingerprint and any future
replayer all read that shape, so a new layer is added without touching the others.

No browser, GUI or filesystem side effects on import. Layers are captured elsewhere
(probe.js in the page, worker.py over CDP, later a Windows or vision capturer) and normalised
here before anything else is allowed to read them.
"""
from __future__ import annotations

from datetime import datetime, timezone

FOUND = "found"            # the layer identified the element
MISSING = "missing"        # the layer ran and could not identify it
UNAVAILABLE = "unavailable"  # the layer could not run here at all
STATUSES = (FOUND, MISSING, UNAVAILABLE)
ICONS = {FOUND: "✅", MISSING: "❌", UNAVAILABLE: "⚪"}

# Capture order, most specific first. The replayer will later prefer the earliest FOUND layer.
LAYERS = (
    ("web", "Web element", "Tag, id, name, text and selector of an ordinary page element."),
    ("nexacro", "Nexacro component", "The real component: type, name, its form, grid, row, column and parent."),
    ("accessibility", "Accessibility tree", "What the system exposes: is this a button, a field, a list?"),
    ("windows", "Windows control", "Desktop windows, menus, dialogs and system buttons."),
    ("anchor", "Anchor", "A stable neighbour, so a moved or renamed element is still findable."),
    ("image", "Image", "A picture of the element and of the area around it."),
    ("ocr", "Screen text", "Text read off the screen, used as a clue to the location."),
    ("relative", "Relative position", "Placement inside its window or anchor, never the whole screen."),
    ("keyboard", "Keyboard route", "Focus order and shortcuts, as a fallback path."),
    ("vision", "Computer vision", "Last resort, for what every other layer missed."),
)
LAYER_KEYS = tuple(key for key, _title, _purpose in LAYERS)
TITLES = {key: title for key, title, _purpose in LAYERS}
PURPOSES = {key: purpose for key, _title, purpose in LAYERS}

MAX_TEXT = 400
MAX_ITEMS = 60
MAX_DEPTH = 6


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(value, depth=0):
    """Page-supplied data is untrusted: keep it small, flat and JSON-safe, or drop it."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if -1e12 < value < 1e12 else None
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {str(k)[:80]: _clean(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    return None


def layer(status, detail="", **data):
    return {"status": status if status in STATUSES else MISSING, "detail": str(detail)[:MAX_TEXT], "data": _clean(data) or {}}


def blank(reason="Not captured."):
    return {key: layer(UNAVAILABLE, reason) for key in LAYER_KEYS}


def normalize(raw):
    """Accept whatever the capturers produced and return a complete, bounded fingerprint."""
    raw = raw if isinstance(raw, dict) else {}
    incoming = raw.get("layers") if isinstance(raw.get("layers"), dict) else {}
    layers = blank("This layer is not built yet.")
    for key in LAYER_KEYS:
        value = incoming.get(key)
        if isinstance(value, dict) and value.get("status") in STATUSES:
            layers[key] = layer(value["status"], value.get("detail", ""), **(value.get("data") if isinstance(value.get("data"), dict) else {}))
    return {
        "schema_version": 1,
        "captured": str(raw.get("captured") or now())[:40],
        "source": str(raw.get("source") or "")[:MAX_TEXT],
        "page_title": str(raw.get("page_title") or "")[:MAX_TEXT],
        "layers": layers,
    }


def radar(fingerprint):
    """One row per layer, in capture order, ready to render as the card."""
    layers = normalize(fingerprint)["layers"]
    return [{"key": key, "title": TITLES[key], "purpose": PURPOSES[key], "icon": ICONS[layers[key]["status"]],
             "status": layers[key]["status"], "detail": layers[key]["detail"]} for key in LAYER_KEYS]


def score(fingerprint):
    """How many independent ways this element can be found, and whether that is enough to replay."""
    layers = normalize(fingerprint)["layers"]
    found = [key for key in LAYER_KEYS if layers[key]["status"] == FOUND]
    missing = [key for key in LAYER_KEYS if layers[key]["status"] == MISSING]
    return {"found": found, "missing": missing, "identified": len(found), "total": len(LAYER_KEYS),
            "confident": len(found) >= 2, "headline": f"{len(found)} of {len(LAYER_KEYS)} layers identified this element."}
