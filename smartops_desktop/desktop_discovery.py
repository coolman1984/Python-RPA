from __future__ import annotations

import hashlib
import math
import re
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


def _looks_sensitive(info, name, automation_id, class_name):
    hay = " ".join((name, automation_id, class_name))
    if re.search(r"password|passwd|secret|token|otp|one.?time|credit.?card|cvv|cvc|login|sign.?in|username|email", hay, re.I):
        return True
    try:
        native = getattr(info, "element", None)
        return bool(getattr(native, "CurrentIsPassword", False))
    except Exception:
        return False


def probe_windows_at(x, y, folder, sequence=0):
    if sys.platform != "win32":
        return {"windows_uia": {"available": False, "reason": "not_windows"}}
    try:
        from pywinauto import Desktop
        element = Desktop(backend="uia").from_point(int(x), int(y))
    except Exception as exc:
        return {"windows_uia": {"available": False, "reason": type(exc).__name__}}

    info = getattr(element, "element_info", None)
    name = ""
    try:
        name = _text(element.window_text())
    except Exception:
        pass
    automation_id = _text(getattr(info, "automation_id", ""), 140) if info else ""
    class_name = _text(getattr(info, "class_name", ""), 140) if info else ""
    control_type = _text(getattr(info, "control_type", ""), 80) if info else ""
    rect = _rect_dict(getattr(info, "rectangle", None)) if info else {}
    if _looks_sensitive(info, name, automation_id, class_name):
        return {"windows_uia": {"available": False, "reason": "sensitive_target", "sensitive": True}}

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
            sibling_name = _text(sibling.window_text())
            if sibling_name:
                anchors.append({"name": sibling_name, "control_type": _text(sibling.element_info.control_type, 80)})
            if len(anchors) >= 4:
                break
    except Exception:
        pass

    if rect and 0 < rect.get("width", 0) <= 1600 and 0 < rect.get("height", 0) <= 1200:
        bbox = (rect["left"], rect["top"], rect["right"], rect["bottom"])
    else:
        bbox = (int(x) - 100, int(y) - 70, int(x) + 100, int(y) + 70)
    return {
        "windows_uia": {
            "available": True,
            "name": name,
            "control_type": control_type,
            "automation_id": automation_id,
            "class_name": class_name,
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
        self._recent_browser_keys = deque(maxlen=30)
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

    def mark_browser_key(self, key):
        if key:
            self._recent_browser_keys.append((time.monotonic(), str(key).lower()))

    def _is_browser_duplicate(self, x, y):
        now = time.monotonic()
        return any(now - when <= 0.8 and math.hypot(float(x) - bx, float(y) - by) <= 12
                   for when, bx, by in list(self._recent_browser))

    def _is_browser_key_duplicate(self, name):
        now = time.monotonic()
        return any(now - when <= 0.8 and recorded == name.lower()
                   for when, recorded in list(self._recent_browser_keys))

    def _on_click(self, x, y, button, pressed, *args):
        if not pressed or self.stop_event.is_set():
            return
        timer = threading.Timer(0.35, self._emit_click, args=(x, y, str(button)))
        timer.daemon = True; self._timers.append(timer); timer.start()

    def _emit_click(self, x, y, button):
        """Raw physical input only.

        This used to probe UI Automation and take a screenshot here, which the session then threw
        away before running the very same discovery again: two lookups, two screenshots, and two
        answers that could disagree. Enrichment now belongs to the DiscoveryManager alone.
        """
        if self.stop_event.is_set() or self._is_browser_duplicate(x, y):
            return
        self._sequence += 1
        self.callback({"action": "desktop_click", "x": int(x), "y": int(y), "button": str(button),
                       "sequence": self._sequence, "at": time.monotonic(), "source": "desktop"})

    def _on_key(self, key, *args):
        if self.stop_event.is_set():
            return
        name = str(key).replace("Key.", "").lower()
        if name not in self.SPECIAL_KEYS:
            return
        timer = threading.Timer(0.25, self._emit_key, args=(name,))
        timer.daemon = True; self._timers.append(timer); timer.start()

    def _emit_key(self, name):
        if self.stop_event.is_set() or self._is_browser_key_duplicate(name):
            return
        self._sequence += 1
        self.callback({"action": "desktop_press", "value": name, "label": f"Press {name}",
                       "sequence": self._sequence, "at": time.monotonic(), "source": "desktop"})

    def stop(self):
        for listener in (self._mouse, self._keyboard):
            try:
                if listener: listener.stop()
            except Exception:
                pass
        for timer in self._timers:
            try: timer.cancel()
            except Exception: pass
