"""The ElementFingerprint contract: one element, many independent ways of recognising it.

This module is pure data. It defines the eleven discovery layers, the vocabulary every
detector must speak, and the merge/ranking rules. It never touches a browser, a screen,
the GUI or the filesystem, so every rule here is testable without any of them.

Two different judgements are kept apart on purpose:

  status    what happened to THIS observation of THIS element  (found / missing / unavailable / failed)
  maturity  how far the detector itself has been proven        (verified / implemented_unverified / not_implemented)

A detector whose code exists but has never run against the real thing reports
IMPLEMENTED_UNVERIFIED forever, however many times it returns FOUND. Confidence never
launders an unproven detector into a trusted one.
"""
from __future__ import annotations

from datetime import datetime, timezone

# --- what happened to this observation --------------------------------------------------
FOUND = "found"              # the detector identified the element
MISSING = "missing"          # the detector ran and could not identify it
UNAVAILABLE = "unavailable"  # the detector could not run here (wrong platform, engine absent)
FAILED = "failed"            # the detector raised; recording continued without it
STATUSES = (FOUND, MISSING, UNAVAILABLE, FAILED)
ICONS = {FOUND: "✅", MISSING: "❌", UNAVAILABLE: "⚪", FAILED: "⚠️"}

# --- how far the detector itself has been proven ----------------------------------------
VERIFIED = "VERIFIED"                              # exercised against the real thing
IMPLEMENTED_UNVERIFIED = "IMPLEMENTED_UNVERIFIED"  # code exists, never met the real thing
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"                # no detection logic at all
MATURITIES = (VERIFIED, IMPLEMENTED_UNVERIFIED, NOT_IMPLEMENTED)

# key, short name, title, what it answers, maturity claimed by this release
LAYERS = (
    ("web", "Web", "Web element", "Tag, id, name, text and selector of an ordinary page element.", VERIFIED),
    ("frame", "Frame", "Frame and tab context", "Which frame, which iframe chain, and whether the target opens a new tab.", VERIFIED),
    ("nexacro", "Nexacro", "Nexacro component", "Component type, name, path, its form, grid, row, column and parent.", IMPLEMENTED_UNVERIFIED),
    ("accessibility", "Accessibility", "Accessibility tree", "The role and accessible name the system publishes.", VERIFIED),
    ("windows", "Windows UIA", "Windows UI Automation", "Desktop windows, menus, dialogs and system controls.", IMPLEMENTED_UNVERIFIED),
    ("anchor", "Anchor", "Anchor", "A stable neighbour, so a moved or renamed element is still findable.", VERIFIED),
    ("visual", "Visual", "Visual fingerprint", "A picture of the element and of the area around it.", VERIFIED),
    ("relative", "Relative", "Relative position", "Placement inside its own container, never the whole screen.", VERIFIED),
    ("keyboard", "Keyboard", "Keyboard route", "Focus order and shortcuts, as a fallback path.", VERIFIED),
    ("ocr", "OCR", "Screen text", "Text read off the screen, used as a clue to the location.", IMPLEMENTED_UNVERIFIED),
    ("vision", "Vision", "Computer vision", "Last resort for what every other layer missed.", NOT_IMPLEMENTED),
    ("network", "Network", "Network clues", "Requests near the action, stripped of query strings, headers and bodies.", VERIFIED),
)
LAYER_KEYS = tuple(key for key, *_ in LAYERS)
SHORT = {key: short for key, short, *_ in LAYERS}
TITLES = {key: title for key, _short, title, *_ in LAYERS}
PURPOSES = {key: purpose for key, _short, _title, purpose, _maturity in LAYERS}
MATURITY = {key: maturity for key, _short, _title, _purpose, maturity in LAYERS}

# Frame and network context say WHERE and WHEN, not WHICH element. It is recorded and scored, but it never
# competes to be the strongest way of identifying the element itself.
CONTEXT_LAYERS = ("frame", "network")
IDENTITY_KEYS = tuple(key for key in LAYER_KEYS if key not in CONTEXT_LAYERS)

MAX_TEXT = 400
MAX_ITEMS = 60
MAX_DEPTH = 6
CONFIDENT_LAYERS = 2  # one lucky selector is not evidence; two independent layers is a start


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


def confidence_of(value):
    try:
        return round(min(1.0, max(0.0, float(value))), 2)
    except (TypeError, ValueError):
        return 0.0


def observation(key, status, detail="", confidence=0.0, **data):
    """One detector's answer about one element. Confidence only means anything when FOUND."""
    status = status if status in STATUSES else MISSING
    return {"status": status, "confidence": confidence_of(confidence) if status == FOUND else 0.0,
            "detail": str(detail)[:MAX_TEXT], "maturity": MATURITY.get(key, NOT_IMPLEMENTED), "data": _clean(data) or {}}


def blank(reason="Not captured."):
    return {key: observation(key, UNAVAILABLE, reason) for key in LAYER_KEYS}


def normalize(raw):
    """Accept whatever the detectors produced and return a complete, bounded fingerprint."""
    raw = raw if isinstance(raw, dict) else {}
    incoming = raw.get("layers") if isinstance(raw.get("layers"), dict) else {}
    layers = blank("This detector did not run.")
    for key in LAYER_KEYS:
        value = incoming.get(key)
        if isinstance(value, dict) and value.get("status") in STATUSES:
            layers[key] = observation(key, value["status"], value.get("detail", ""), value.get("confidence", 0.0),
                                      **(value.get("data") if isinstance(value.get("data"), dict) else {}))
    return {
        "schema_version": 2,
        "captured": str(raw.get("captured") or now())[:40],
        "source": str(raw.get("source") or "")[:MAX_TEXT],
        "page_title": str(raw.get("page_title") or "")[:MAX_TEXT],
        "layers": layers,
    }


def rank(fingerprint):
    """Identified layers, strongest evidence first. This is what a replayer would walk."""
    layers = normalize(fingerprint)["layers"]
    found = [(key, layers[key]) for key in IDENTITY_KEYS if layers[key]["status"] == FOUND]
    # Ties break on capture order, which runs most specific first.
    return sorted(found, key=lambda item: (-item[1]["confidence"], LAYER_KEYS.index(item[0])))


def diagnostic(fingerprint):
    """The per-target read-out: one line per layer, evidence strength or the reason there is none."""
    layers = normalize(fingerprint)["layers"]
    width = max(len(SHORT[key]) for key in LAYER_KEYS)
    lines = []
    for key in LAYER_KEYS:
        entry = layers[key]
        verdict = f"{entry['confidence']:.2f}" if entry["status"] == FOUND else entry["status"]
        lines.append(f"{SHORT[key]:<{width}} {ICONS[entry['status']]} {verdict}")
    return lines


def radar(fingerprint):
    """One row per layer, in capture order, ready to render as the card."""
    layers = normalize(fingerprint)["layers"]
    return [{"key": key, "short": SHORT[key], "title": TITLES[key], "purpose": PURPOSES[key],
             "icon": ICONS[layers[key]["status"]], "status": layers[key]["status"],
             "confidence": layers[key]["confidence"], "maturity": layers[key]["maturity"],
             "detail": layers[key]["detail"]} for key in LAYER_KEYS]


def score(fingerprint):
    """How many independent ways this element can be found, and how much to trust that."""
    layers = normalize(fingerprint)["layers"]
    by_status = {status: [key for key in LAYER_KEYS if layers[key]["status"] == status] for status in STATUSES}
    ordered = rank(fingerprint)
    best = ordered[0] if ordered else None
    # Only layers whose detector has met the real thing may support a confident verdict.
    proven = [key for key, entry in ordered if entry["maturity"] == VERIFIED]
    context = {key: layers[key]["detail"] for key in CONTEXT_LAYERS if layers[key]["status"] == FOUND}
    return {
        "found": by_status[FOUND], "missing": by_status[MISSING],
        "unavailable": by_status[UNAVAILABLE], "failed": by_status[FAILED],
        "identified": len(by_status[FOUND]), "total": len(LAYER_KEYS),
        "best": best[0] if best else "", "best_confidence": best[1]["confidence"] if best else 0.0,
        "proven": proven, "confident": len(proven) >= CONFIDENT_LAYERS, "context": context,
        "headline": f"{len(by_status[FOUND])} of {len(LAYER_KEYS)} layers identified this element."
                    + (f" Strongest: {SHORT[best[0]]} at {best[1]['confidence']:.2f}." if best else ""),
    }
