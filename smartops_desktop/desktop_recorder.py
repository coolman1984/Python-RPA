from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .discovery import discover_native


def _safe(value, limit=160):
    return " ".join(str(value or "").split())[:limit]


def _visual_crop(x, y, run_dir, index):
    if os.name != "nt":
        return {"status": "unavailable", "reason": "Desktop visual capture is Windows-only."}
    try:
        from PIL import ImageGrab

        left, top = max(0, int(x) - 100), max(0, int(y) - 75)
        right, bottom = int(x) + 100, int(y) + 75
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        folder = Path(run_dir) / "desktop-discovery"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"desktop-{index:04d}.png"
        image.save(path, format="PNG")
        data = path.read_bytes()
        return {
            "status": "ok",
            "path": str(path),
            "portable": False,
            "sha256": hashlib.sha256(data).hexdigest(),
            "clip": {"left": left, "top": top, "right": right, "bottom": bottom},
        }
    except ImportError:
        return {"status": "unavailable", "reason": "Pillow is not installed."}
    except Exception as exc:
        return {"status": "error", "reason": _safe(exc, 200)}


def _relative(native, x, y):
    hierarchy = ((native.get("win32") or {}).get("hierarchy") or [])
    rect = hierarchy[0].get("rect") if hierarchy else None
    if not rect:
        return {"status": "miss"}
    width = max(1, rect["right"] - rect["left"])
    height = max(1, rect["bottom"] - rect["top"])
    return {
        "status": "ok",
        "window_rect": rect,
        "relative_click": {
            "x": round((x - rect["left"]) / width, 4),
            "y": round((y - rect["top"]) / height, 4),
        },
    }


class DesktopDiscoveryObserver:
    """Observe desktop clicks and safe navigation keys while browser recording is active.

    These observations are evidence only. They are stored in the run journal and are not
    silently converted into replayable workflow steps.
    """

    def __init__(self, output, stop, run_dir):
        self.output = output
        self.stop_event = stop
        self.run_dir = Path(run_dir)
        self.mouse_listener = None
        self.keyboard_listener = None
        self.index = 0
        self.lock = threading.Lock()
        self.modifiers = set()

    def start(self):
        if os.name != "nt":
            return False
        try:
            from pynput import mouse, keyboard
        except ImportError:
            self.output.put({"type": "log", "message": "Desktop discovery is unavailable because pynput is not installed."})
            return False

        def on_click(x, y, button, pressed, *args):
            if not pressed or self.stop_event.is_set():
                return
            with self.lock:
                self.index += 1
                index = self.index
            native = discover_native(x, y)
            uia_target = ((native.get("windows_uia") or {}).get("target") or {})
            observation = {
                "kind": "desktop_click",
                "time": time.time(),
                "point": {"screen_x": int(x), "screen_y": int(y)},
                "button": _safe(button, 40),
                "layers": {
                    **native,
                    "visual": _visual_crop(x, y, self.run_dir, index),
                    "relative_position": _relative(native, x, y),
                    "screen_text": {
                        "status": "ok" if uia_target.get("name") else "miss",
                        "text": _safe(uia_target.get("name")),
                        "automation_id": _safe(uia_target.get("automation_id"), 240),
                    },
                },
            }
            self._write(observation)
            target = uia_target.get("name") or uia_target.get("automation_id") or uia_target.get("control_type") or "desktop target"
            self.output.put({"type": "discovery_observation", "message": f"Desktop discovery: {_safe(target, 80)}", "observation": observation})

        modifier_keys = {
            keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
            keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
            keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r,
            keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r,
        }
        safe_keys = {
            keyboard.Key.tab, keyboard.Key.enter, keyboard.Key.esc,
            keyboard.Key.up, keyboard.Key.down, keyboard.Key.left, keyboard.Key.right,
            keyboard.Key.home, keyboard.Key.end, keyboard.Key.page_up, keyboard.Key.page_down,
            keyboard.Key.insert, keyboard.Key.delete,
        }

        def on_press(key, *args):
            if self.stop_event.is_set():
                return False
            if key in modifier_keys:
                self.modifiers.add(_safe(key, 30))
                return
            is_character_shortcut = bool(self.modifiers) and getattr(key, "char", None)
            if key not in safe_keys and not is_character_shortcut:
                return
            observation = {
                "kind": "desktop_key",
                "time": time.time(),
                "key": _safe(key, 40),
                "modifiers": sorted(self.modifiers),
                "privacy": "Raw character typing is deliberately not captured by the global desktop observer.",
            }
            self._write(observation)
            self.output.put({"type": "discovery_observation", "message": "Desktop key discovery: " + _safe(key, 40), "observation": observation})

        def on_release(key, *args):
            value = _safe(key, 30)
            self.modifiers.discard(value)

        self.mouse_listener = mouse.Listener(on_click=on_click)
        self.keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.mouse_listener.start()
        self.keyboard_listener.start()
        self.output.put({"type": "log", "message": "Global Windows discovery is active for desktop clicks and safe navigation/shortcut keys. Raw character typing is not globally captured."})
        return True

    def _write(self, observation):
        try:
            folder = self.run_dir / "desktop-discovery"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "observations.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(observation, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def stop(self):
        for listener in (self.mouse_listener, self.keyboard_listener):
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass
