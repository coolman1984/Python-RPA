from __future__ import annotations

import hashlib
import os
from pathlib import Path

MAX_TEXT = 160


def _text(value, limit=MAX_TEXT):
    if value is None:
        return ""
    value = " ".join(str(value).split())
    return value[:limit]


def _rect(value):
    if not value:
        return None
    try:
        if hasattr(value, "left"):
            return {
                "left": int(value.left),
                "top": int(value.top),
                "right": int(value.right),
                "bottom": int(value.bottom),
            }
        left, top, right, bottom = value
        return {"left": int(left), "top": int(top), "right": int(right), "bottom": int(bottom)}
    except Exception:
        return None


def _layer(status, **payload):
    return {"status": status, **payload}


def discover_native(screen_x, screen_y):
    """Best-effort Windows discovery. Failure in one native API never stops recording."""
    result = {
        "windows_uia": _layer("unavailable", reason="Windows UI Automation is not available on this platform."),
        "win32": _layer("unavailable", reason="Win32 window discovery is not available on this platform."),
    }
    if os.name != "nt" or screen_x is None or screen_y is None:
        return result

    x, y = int(screen_x), int(screen_y)
    try:
        import win32gui

        hwnd = win32gui.WindowFromPoint((x, y))
        chain = []
        seen = set()
        while hwnd and hwnd not in seen and len(chain) < 8:
            seen.add(hwnd)
            chain.append({
                "handle": int(hwnd),
                "class": _text(win32gui.GetClassName(hwnd), 100),
                "title": _text(win32gui.GetWindowText(hwnd)),
                "rect": _rect(win32gui.GetWindowRect(hwnd)),
            })
            hwnd = win32gui.GetParent(hwnd)
        result["win32"] = _layer("ok", point={"x": x, "y": y}, hierarchy=chain)
    except Exception as exc:
        result["win32"] = _layer("error", reason=_text(exc, 200))

    try:
        import uiautomation as automation

        control = automation.ControlFromPoint(x, y)
        if control:
            hierarchy = []
            current = control
            seen = set()
            while current and len(hierarchy) < 8:
                key = (getattr(current, "Handle", 0), getattr(current, "AutomationId", ""), getattr(current, "Name", ""))
                if key in seen:
                    break
                seen.add(key)
                hierarchy.append({
                    "name": _text(getattr(current, "Name", "")),
                    "automation_id": _text(getattr(current, "AutomationId", ""), 240),
                    "control_type": _text(getattr(current, "ControlTypeName", ""), 100),
                    "class": _text(getattr(current, "ClassName", ""), 100),
                    "handle": int(getattr(current, "Handle", 0) or 0),
                    "rect": _rect(getattr(current, "BoundingRectangle", None)),
                })
                try:
                    current = current.GetParentControl()
                except Exception:
                    break
            primary = hierarchy[0] if hierarchy else {}
            result["windows_uia"] = _layer(
                "ok",
                point={"x": x, "y": y},
                target=primary,
                hierarchy=hierarchy,
                nexacro_accessibility_candidate=(primary.get("automation_id") or "") if ".form." in (primary.get("automation_id") or "") else "",
            )
        else:
            result["windows_uia"] = _layer("miss", reason="No UI Automation control was found at the click point.")
    except ImportError:
        result["windows_uia"] = _layer("unavailable", reason="Install the uiautomation package to enable native UI Automation discovery.")
    except Exception as exc:
        result["windows_uia"] = _layer("error", reason=_text(exc, 200))
    return result


def discover_cdp_accessibility(page, selector, is_main_frame=True):
    """Read Chrome's accessibility tree for the selected DOM target when possible."""
    if not selector:
        return _layer("miss", reason="The recorded action has no DOM selector.")
    if not is_main_frame:
        return _layer("deferred", reason="Accessibility lookup for child frames is recorded from DOM/native layers for now.")
    session = None
    try:
        session = page.context.new_cdp_session(page)
        document = session.send("DOM.getDocument", {"depth": 0, "pierce": True})
        node_id = session.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector}).get("nodeId", 0)
        if not node_id:
            return _layer("miss", reason="Chrome accessibility target was not found for this selector.")
        described = session.send("DOM.describeNode", {"nodeId": node_id})["node"]
        backend_id = described.get("backendNodeId")
        response = session.send("Accessibility.getPartialAXTree", {"backendNodeId": backend_id, "fetchRelatives": True})
        nodes = []
        for node in response.get("nodes", [])[:12]:
            role = (node.get("role") or {}).get("value", "")
            name = (node.get("name") or {}).get("value", "")
            description = (node.get("description") or {}).get("value", "")
            value = (node.get("value") or {}).get("value", "")
            nodes.append({
                "role": _text(role, 100),
                "name": _text(name),
                "description": _text(description),
                "value": _text(value),
                "ignored": bool(node.get("ignored", False)),
            })
        return _layer("ok", target=nodes[0] if nodes else {}, nodes=nodes)
    except Exception as exc:
        return _layer("error", reason=_text(exc, 200))
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass


def capture_visual(page, geometry, run_dir, index):
    """Store a small visual crop as evidence/fallback fingerprint, not as the primary selector."""
    box = (geometry or {}).get("box") or {}
    try:
        x = max(0.0, float(box.get("x", 0)) - 24)
        y = max(0.0, float(box.get("y", 0)) - 24)
        width = max(1.0, float(box.get("width", 0)) + 48)
        height = max(1.0, float(box.get("height", 0)) + 48)
        viewport = page.viewport_size
        if viewport:
            width = min(width, max(1.0, viewport["width"] - x))
            height = min(height, max(1.0, viewport["height"] - y))
        if width <= 1 or height <= 1:
            return _layer("miss", reason="The target has no visible rectangle to capture.")
        folder = Path(run_dir) / "discovery"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"target-{index:04d}.png"
        data = page.screenshot(path=str(path), clip={"x": x, "y": y, "width": width, "height": height})
        return _layer(
            "ok",
            path=str(path),
            portable=False,
            sha256=hashlib.sha256(data).hexdigest(),
            clip={"x": round(x, 2), "y": round(y, 2), "width": round(width, 2), "height": round(height, 2)},
        )
    except Exception as exc:
        return _layer("error", reason=_text(exc, 200))


def enrich_recorded_step(page, frame, step, run_dir, index):
    """Attach independent discovery layers to one recorded user action."""
    discovery = dict(step.pop("discovery_seed", {}) or {})
    point = discovery.get("point") or {}
    layers = dict(discovery.get("layers") or {})
    layers.update(discover_native(point.get("screen_x"), point.get("screen_y")))
    layers["browser_accessibility"] = discover_cdp_accessibility(page, step.get("selector"), frame == page.main_frame)
    if step.get("action") in {"click", "fill", "select", "check", "press"}:
        layers["visual"] = capture_visual(page, discovery.get("geometry") or {}, run_dir, index)
    else:
        layers["visual"] = _layer("not_applicable")
    discovery["layers"] = layers
    discovery["version"] = 1
    step["discovery"] = discovery
    return step


def summarize_layers(step):
    layers = ((step.get("discovery") or {}).get("layers") or {})
    good = [name for name, value in layers.items() if isinstance(value, dict) and value.get("status") == "ok"]
    return good
