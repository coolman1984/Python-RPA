"""Unified multi-layer discovery model and browser enrichment helpers."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import hashlib
import json
import time


@dataclass
class Evidence:
    layer: str
    available: bool
    confidence: float = 0.0
    identity: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self):
        self.confidence = max(0.0, min(1.0, float(self.confidence)))


@dataclass
class ElementFingerprint:
    action: str
    label: str = ""
    page_url: str = ""
    frame_url: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)

    def add(self, layer: str, available: bool, confidence: float = 0.0,
            identity: dict[str, Any] | None = None, reason: str = "") -> None:
        self.evidence.append(Evidence(layer, available, confidence, identity or {}, reason))

    def best(self) -> Evidence | None:
        usable = [item for item in self.evidence if item.available and item.identity]
        return max(usable, key=lambda item: item.confidence, default=None)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        best = self.best()
        data["best_layer"] = best.layer if best else None
        return data


LAYER_ORDER = (
    "web",
    "nexacro",
    "accessibility",
    "windows_uia",
    "anchor",
    "visual",
    "relative_position",
    "keyboard",
    "ocr",
    "computer_vision",
    "network",
)


def _clean(value, limit=240):
    return " ".join(str(value or "").split())[:limit]


def safe_http_url(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        port = parsed.port
    except Exception:
        return ""
    netloc = parsed.hostname + (f":{port}" if port else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def normalize_capture(step: dict[str, Any], *, page_url: str = "", frame_url: str = "",
                      metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize independent detector observations into one explicit fingerprint."""
    fp = ElementFingerprint(
        action=str(step.get("action", "")),
        label=str(step.get("label", "")),
        page_url=safe_http_url(page_url),
        frame_url=safe_http_url(frame_url),
        metadata=dict(metadata or {}),
    )
    supplied = {item.get("layer"): item for item in step.get("evidence", []) if isinstance(item, dict) and item.get("layer")}
    for layer in LAYER_ORDER:
        item = supplied.get(layer)
        if item:
            fp.add(layer, bool(item.get("available")), item.get("confidence", 0),
                   item.get("identity") or {}, str(item.get("reason", "")))
        elif layer == "web" and step.get("selector"):
            fp.add("web", True, 0.75, {"selector": step["selector"]})
        else:
            fp.add(layer, False, reason="not observed by this capture source")
    return fp.to_dict()


def fingerprint_layers(fingerprint: dict[str, Any]) -> list[str]:
    return [item.get("layer", "") for item in fingerprint.get("evidence", [])
            if isinstance(item, dict) and item.get("available") and item.get("identity")]


def fingerprint_size_ok(fp, limit=120_000):
    if not isinstance(fp, dict):
        return False
    try:
        return len(json.dumps(fp, ensure_ascii=False)) <= limit
    except Exception:
        return False


class NetworkJournal:
    """Privacy-preserving network clues: no headers, bodies, query strings or fragments."""

    def __init__(self, max_events=60):
        self.events = deque(maxlen=max_events)
        self._pages = set()

    def _push(self, kind, url, **extra):
        clean = safe_http_url(url)
        if clean:
            self.events.append({"kind": kind, "url": clean, "time": time.time(), **extra})

    def attach(self, page):
        marker = id(page)
        if marker in self._pages:
            return
        self._pages.add(marker)

        def on_request(request):
            try:
                self._push("request", request.url, method=request.method, resource_type=request.resource_type)
            except Exception:
                pass

        def on_response(response):
            try:
                self._push("response", response.url, status=int(response.status), resource_type=response.request.resource_type)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

    def snapshot(self, limit=8):
        return list(self.events)[-limit:]


def _frame_element_descriptor(frame):
    try:
        handle = frame.frame_element()
        attrs = handle.evaluate("""el => ({id:el.id||'',name:el.getAttribute('name')||'',title:el.getAttribute('title')||'',src:el.getAttribute('src')||''})""")
    except Exception:
        return {"selector": ""}
    selector = ""
    if attrs.get("id"):
        selector = "#" + str(attrs["id"]).replace('"', '\\"')
    elif attrs.get("name"):
        selector = 'iframe[name="' + str(attrs["name"]).replace('"', '\\"') + '"]'
    elif attrs.get("title"):
        selector = 'iframe[title="' + str(attrs["title"]).replace('"', '\\"') + '"]'
    return {"selector": selector, "id": _clean(attrs.get("id"), 120), "name": _clean(attrs.get("name"), 120),
            "title": _clean(attrs.get("title"), 160), "src": safe_http_url(attrs.get("src"))}


def frame_descriptor(frame):
    path = []
    current = frame
    while current is not None:
        try:
            parent = current.parent_frame
        except Exception:
            parent = None
        if parent is None:
            break
        path.append(_frame_element_descriptor(current))
        current = parent
    path.reverse()
    try:
        url = safe_http_url(frame.url)
    except Exception:
        url = ""
    try:
        name = _clean(frame.name, 120)
    except Exception:
        name = ""
    return {"depth": len(path), "url": url, "name": name, "path": path}


def _visual_evidence(locator, run_dir, sequence):
    folder = Path(run_dir) / "discovery"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"element-{sequence:04d}.png"
    try:
        locator.screenshot(path=str(path), timeout=1500, animations="disabled")
        return {"layer": "visual", "available": True, "confidence": 0.55,
                "identity": {"artifact": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"layer": "visual", "available": False, "reason": type(exc).__name__}


def enrich_browser_capture(page, frame, step, run_dir, sequence, *, network=None, windows_probe=None,
                           page_token="page-1", relation="root"):
    """Add ARIA, visual, UIA, frame and network evidence to one browser event."""
    result = dict(step)
    evidence = [dict(item) for item in result.get("evidence", []) if isinstance(item, dict)]
    frame_info = frame_descriptor(frame)
    selector = result.get("selector")
    locator = None
    if selector:
        try:
            locator = frame.locator(selector).first
            snapshot = locator.aria_snapshot(depth=2, boxes=True, timeout=1200)[:6000]
            evidence.append({"layer": "accessibility", "available": True, "confidence": 0.88,
                             "identity": {"aria_snapshot": snapshot}})
        except Exception as exc:
            evidence.append({"layer": "accessibility", "available": False, "reason": type(exc).__name__})
        if locator is not None:
            evidence.append(_visual_evidence(locator, run_dir, sequence))

    geometry = None
    for item in evidence:
        if item.get("layer") == "relative_position" and isinstance(item.get("identity"), dict):
            geometry = item["identity"]
    if windows_probe and geometry and geometry.get("screen_x") is not None and geometry.get("screen_y") is not None:
        try:
            native = windows_probe(float(geometry["screen_x"]), float(geometry["screen_y"]), Path(run_dir) / "discovery", sequence)
            uia = native.get("windows_uia", {})
            evidence.append({"layer": "windows_uia", "available": bool(uia.get("available")),
                             "confidence": 0.9 if uia.get("available") else 0.0,
                             "identity": {k: v for k, v in uia.items() if k != "available"},
                             "reason": "" if uia.get("available") else str(uia.get("reason", "unavailable"))})
            if native.get("visual", {}).get("available") and not any(e.get("layer") == "visual" and e.get("available") for e in evidence):
                evidence.append({"layer": "visual", "available": True, "confidence": 0.45, "identity": native["visual"]})
        except Exception as exc:
            evidence.append({"layer": "windows_uia", "available": False, "reason": type(exc).__name__})

    if network is not None:
        recent = network.snapshot()
        if recent:
            evidence.append({"layer": "network", "available": True, "confidence": 0.25, "identity": {"recent": recent}})

    result["evidence"] = evidence
    metadata = {"page_token": page_token, "page_relation": relation, "frame": frame_info}
    result["fingerprint"] = normalize_capture(result, page_url=getattr(page, "url", ""), frame_url=frame_info["url"], metadata=metadata)
    result["detected_by"] = fingerprint_layers(result["fingerprint"])
    if frame_info["depth"] == 1 and frame_info["path"][0].get("selector") and not result.get("frame"):
        result["frame"] = frame_info["path"][0]["selector"]
    result.pop("evidence", None)
    return result
