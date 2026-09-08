from __future__ import annotations

import hashlib
import math
import sys
import threading
import time
from collections import deque
from pathlib import Path


def _text(value, limit=220):
    return " ".join(str(value or "").split())[:limit]


def _rect_dict(rect):
    try:
        return {"left": int(rect.left), "top": int(rect.top), "right": int(rect.right), "bottom": int(rect.bottom),
                "width": int(rect.width()), "height": int(rect.height())}
    except Exception:
        return {}


def _capture_patch(bbox, folder, sequence):
    try:
        from PIL import ImageGrab
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"desktop-{sequence:04d}.png"
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        image.save(path)
        return {"available": True, "artifact": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__}


def probe_windows_at(x, y, folder, sequence=0):
    if sys.platform != "win32":
        return {"windows_uia": {"available": False, "reason": "not_windows"}}
    try:
        from pywinauto import Desktop
        element = Desktop(backend="uia").from_point(int(x), int(y))
    except Exception as exc:
        return {"windows_uia": {"available": False, "reason": type(exc).__name__}}

    info = getattr(element, "element_info", None)
    chain = []
    current = element
    for _ in range(7):
        try:
            item_info = current.element_info
            chain.append({
                "name": _text(getattr(item_info, "name", "")),
                "control_type": _text(getattr(item_info, "control_type", ""), 80),
                "automation_id": _text(getattr(item_info, "automation_id", ""), 140),
                "class_name": _text(getattr(item_info, "class_name", ""), 140),
                "rectangle": _rect_dict(getattr(item_info, "rectangle", None)),
            })
            parent = current.parent()
            if not parent or parent == current:
                break
            current = parent
        except Exception:
            break

    anchors = []
    try:
        parent = element.parent()
        for sibling in parent.children()[:30]:
            if sibling == element:
                continue
            name = _text(sibling.window_text())
            if name:
                anchors.append({"name": name, "control_type": _text(sibling.element_info.control_type, 80)})
            if len(anchors) >= 4:
                break
    except Exception:
        pass

    name = ""
    try:
        name = _text(element.window_text())
    except Exception:
        pass
    rect = _rect_dict(getattr(info, "rectangle", None)) if info else {}
    if rect and 0 < rect.get("width", 0) <= 1600 and 0 < rect.get("height", 0) <= 1200:
        bbox = (rect["left"], rect["top"], rect["right"], rect["bottom"])
    else:
        bbox = (int(x) - 100, int(y) - 70, int(x) + 100, int(y) + 70)
    return {
        "windows_uia": {
            "available": True,
            "name": name,
            "control_type": _text(getattr(info, "control_type", ""), 80) if info else "",
            "automation_id": _text(getattr(info, "automation_id", ""), 140) if info else "",
            "class_name": _text(getattr(info, "class_name", ""), 140) if info else "",
            "rectangle": rect,
            "parent_chain": chain,
            "anchors": anchors,
        },
        "visual": _capture_patch(bbox, folder, sequence),
    }


class DesktopInputRecorder:
    SPECIAL_KEYS = {
        "enter", "tab", "esc", "escape", "space", "backspace", "delete", "insert", "home", "end",
        "page_up", "page_down", "up", "down", "left", "right", "f1", "f2", "f3", "f4", "f5", "f6",
        "f7", "f8", "f9", "f10", "f11", "f12",
    }

    def __init__(self, callback, stop_event, artifact_dir):
        self.callback = callback
        self.stop_event = stop_event
        self.artifact_dir = Path(artifact_dir)
        self._recent_browser = deque(maxlen=30)
        self._mouse = None
        self._keyboard = None
        self._timers = []
        self._sequence = 100000
        self.available = False
        self.reason = "not_started"

    def start(self):
        if sys.platform != "win32":
            self.reason = "not_windows"
            return False
        try:
            from pynput import mouse, keyboard
        except Exception as exc:
            self.reason = type(exc).__name__
            return False
        self._mouse = mouse.Listener(on_click=self._on_click)
        self._keyboard = keyboard.Listener(on_press=self._on_key)
        self._mouse.start(); self._keyboard.start()
        self.available = True; self.reason = ""
        return True

    def mark_browser_event(self, x, y):
        try:
            self._recent_browser.append((time.monotonic(), float(x), float(y)))
        except Exception:
            pass

    def _is_browser_duplicate(self, x, y):
        now = time.monotonic()
        return any(now - when <= 0.55 and math.hypot(float(x) - bx, float(y) - by) <= 12
                   for when, bx, by in list(self._recent_browser))

    def _on_click(self, x, y, button, pressed, *args):
        if not pressed or self.stop_event.is_set():
            return
        timer = threading.Timer(0.22, self._emit_click, args=(x, y, str(button)))
        timer.daemon = True; self._timers.append(timer); timer.start()

    def _emit_click(self, x, y, button):
        if self.stop_event.is_set() or self._is_browser_duplicate(x, y):
            return
        self._sequence += 1
        probe = probe_windows_at(x, y, self.artifact_dir, self._sequence)
        uia = probe.get("windows_uia", {})
        evidence = [
            {"layer": "windows_uia", "available": bool(uia.get("available")), "confidence": 0.9 if uia.get("available") else 0,
             "identity": {k:v for k,v in uia.items() if k != "available"}, "reason": "" if uia.get("available") else uia.get("reason", "unavailable")},
            {"layer": "relative_position", "available": True, "confidence": 0.3,
             "identity": {"screen_x": int(x), "screen_y": int(y), "button": button}},
        ]
        if uia.get("anchors"):
            evidence.append({"layer": "anchor", "available": True, "confidence": 0.6, "identity": {"neighbors": uia["anchors"]}})
        visual = probe.get("visual", {})
        if visual:
            evidence.append({"layer": "visual", "available": bool(visual.get("available")), "confidence": 0.45 if visual.get("available") else 0,
                             "identity": {k:v for k,v in visual.items() if k != "available"}, "reason": visual.get("reason", "")})
        label = uia.get("name") or uia.get("control_type") or "Desktop click"
        self.callback({"action": "desktop_click", "label": _text(label, 120), "evidence": evidence})

    def _on_key(self, key, *args):
        if self.stop_event.is_set():
            return
        name = str(key).replace("Key.", "").lower()
        if name in self.SPECIAL_KEYS:
            self.callback({"action": "desktop_press", "label": f"Press {name}", "value": name,
                           "evidence": [{"layer": "keyboard", "available": True, "confidence": 1.0, "identity": {"key": name}}]})

    def stop(self):
        for listener in (self._mouse, self._keyboard):
            try:
                if listener: listener.stop()
            except Exception:
                pass
        for timer in self._timers:
            try: timer.cancel()
            except Exception: pass
