from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _clean_text(value, limit=240):
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def safe_http_url(value):
    try:
        parsed = urlsplit(str(value))
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    netloc = parsed.hostname
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def fingerprint_layers(step_or_fp):
    fp = step_or_fp.get("fingerprint", step_or_fp) if isinstance(step_or_fp, dict) else {}
    if not isinstance(fp, dict):
        return []
    found = []
    checks = [
        ("web", "web"),
        ("accessibility", "accessibility"),
        ("nexacro", "nexacro"),
        ("windows_uia", "windows_uia"),
        ("anchors", "anchors"),
        ("visual", "visual"),
        ("geometry", "geometry"),
        ("network", "network"),
        ("screen_text", "screen_text"),
    ]
    for label, key in checks:
        value = fp.get(key)
        if value and not (isinstance(value, dict) and value.get("available") is False):
            found.append(label)
    return found


class NetworkJournal:
    """Small privacy-preserving ring buffer. Never stores headers, bodies or query strings."""

    def __init__(self, max_events=60):
        self.events = deque(maxlen=max_events)
        self._pages = set()

    def _push(self, kind, url, **extra):
        clean = safe_http_url(url)
        if not clean:
            return
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
        attrs = handle.evaluate("""el => ({
            id: el.id || '', name: el.getAttribute('name') || '', title: el.getAttribute('title') || '',
            src: el.getAttribute('src') || '', tag: (el.tagName || '').toLowerCase()
        })""")
    except Exception:
        return {"selector": ""}
    selector = ""
    if attrs.get("id"):
        selector = "#" + str(attrs["id"]).replace('"', '\\"')
    elif attrs.get("name"):
        selector = f'iframe[name="{str(attrs["name"]).replace(chr(34), chr(92)+chr(34))}"]'
    elif attrs.get("title"):
        selector = f'iframe[title="{str(attrs["title"]).replace(chr(34), chr(92)+chr(34))}"]'
    return {
        "selector": selector,
        "id": _clean_text(attrs.get("id"), 120),
        "name": _clean_text(attrs.get("name"), 120),
        "title": _clean_text(attrs.get("title"), 160),
        "src": safe_http_url(attrs.get("src")),
    }


def frame_descriptor(frame):
    path = []
    current = frame
    try:
        frame_url = safe_http_url(frame.url)
    except Exception:
        frame_url = ""
    try:
        frame_name = _clean_text(frame.name, 120)
    except Exception:
        frame_name = ""
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
    return {"depth": len(path), "name": frame_name, "url": frame_url, "path": path}


def _visual_fingerprint(locator, run_dir, sequence):
    folder = Path(run_dir) / "discovery"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"element-{sequence:04d}.png"
    try:
        locator.screenshot(path=str(path), timeout=1500, animations="disabled")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"available": True, "artifact": str(path), "sha256": digest}
    except Exception as exc:
        path.unlink(missing_ok=True)
        return {"available": False, "reason": type(exc).__name__}


def enrich_browser_capture(page, frame, step, run_dir, sequence, network=None, windows_probe=None):
    """Add independent discovery evidence without changing the action itself."""
    result = dict(step)
    fp = dict(result.get("fingerprint") or {})
    fp["schema_version"] = 1
    try:
        title = _clean_text(page.title(), 200)
    except Exception:
        title = ""
    fp["browser"] = {"url": safe_http_url(getattr(page, "url", "")), "title": title}
    frame_info = frame_descriptor(frame)
    fp["frame"] = frame_info
    if frame_info["depth"] == 1 and frame_info["path"][0].get("selector") and not result.get("frame"):
        result["frame"] = frame_info["path"][0]["selector"]

    selector = result.get("selector")
    locator = None
    if selector:
        try:
            locator = frame.locator(selector).first
            fp["accessibility"] = {
                "available": True,
                "aria_snapshot": locator.aria_snapshot(depth=2, boxes=True, timeout=1200)[:6000],
            }
        except Exception as exc:
            fp["accessibility"] = {"available": False, "reason": type(exc).__name__}
        if locator is not None:
            fp["visual"] = _visual_fingerprint(locator, run_dir, sequence)

    geometry = fp.get("geometry") if isinstance(fp.get("geometry"), dict) else {}
    if windows_probe and geometry.get("screen_x") is not None and geometry.get("screen_y") is not None:
        try:
            win = windows_probe(float(geometry["screen_x"]), float(geometry["screen_y"]), Path(run_dir) / "discovery", sequence)
            if win:
                fp["windows_uia"] = win.get("windows_uia", win)
                if win.get("screen_text"):
                    fp["screen_text"] = win["screen_text"]
                if win.get("visual") and not fp.get("visual", {}).get("available"):
                    fp["visual"] = win["visual"]
        except Exception as exc:
            fp["windows_uia"] = {"available": False, "reason": type(exc).__name__}

    if network is not None:
        recent = network.snapshot()
        if recent:
            fp["network"] = recent

    result["fingerprint"] = fp
    result["detected_by"] = fingerprint_layers(fp)
    return result


def fingerprint_size_ok(fp, limit=100_000):
    if not isinstance(fp, dict):
        return False
    try:
        return len(json.dumps(fp, ensure_ascii=False)) <= limit
    except Exception:
        return False
