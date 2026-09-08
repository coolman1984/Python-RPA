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
    """Best-effort Windows UI Automation probe at one screen point. Safe on non-Windows hosts."""
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
        "screen_text": {"source": "windows_uia", "text": name} if name else {},
        "visual": _capture_patch(bbox, folder, sequence),
    }


class DesktopInputRecorder:
    """Global attended Windows input listener used only while SmartOps recording is active."""

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
        self._mouse.start()
        self._keyboard.start()
        self.available = True
        self.reason = ""
        return True

    def mark_browser_event(self, x, y):
        try:
            self._recent_browser.append((time.monotonic(), float(x), float(y)))
        except Exception:
            pass

    def _is_browser_duplicate(self, x, y):
        now = time.monotonic()
        for when, bx, by in list(self._recent_browser):
            if now - when <= 0.55 and math.hypot(float(x) - bx, float(y) - by) <= 12:
                return True
        return False

    def _on_click(self, x, y, button, pressed, *args):
        if not pressed or self.stop_event.is_set():
            return
        timer = threading.Timer(0.22, self._emit_click, args=(x, y, str(button)))
        timer.daemon = True
        self._timers.append(timer)
        timer.start()

    def _emit_click(self, x, y, button):
        if self.stop_event.is_set() or self._is_browser_duplicate(x, y):
            return
        self._sequence += 1
        probe = probe_windows_at(x, y, self.artifact_dir, self._sequence)
        uia = probe.get("windows_uia", {})
        label = uia.get("name") or uia.get("control_type") or "Desktop click"
        fp = {
            "schema_version": 1,
            "geometry": {"screen_x": int(x), "screen_y": int(y), "button": button},
            **probe,
        }
        self.callback({"action": "desktop_click", "label": _text(label, 120), "fingerprint": fp,
                       "detected_by": [key for key in ("windows_uia", "screen_text", "visual", "geometry") if fp.get(key)]})

    def _on_key(self, key, *args):
        if self.stop_event.is_set():
            return
        name = str(key).replace("Key.", "").lower()
        if name not in self.SPECIAL_KEYS:
            return
        self.callback({"action": "desktop_press", "label": f"Press {name}", "value": name,
                       "fingerprint": {"schema_version": 1, "keyboard": {"key": name}}, "detected_by": ["keyboard"]})

    def stop(self):
        for listener in (self._mouse, self._keyboard):
            try:
                if listener:
                    listener.stop()
            except Exception:
                pass
        for timer in self._timers:
            try:
                timer.cancel()
            except Exception:
                pass
