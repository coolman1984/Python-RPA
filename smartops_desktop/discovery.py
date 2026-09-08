"""The Discovery Manager and its detectors.

    user action -> DiscoveryManager -> every available detector -> normalised evidence
                -> one ElementFingerprint -> recorder storage

Each detector is an independent adapter with one method, `observe(context)`. The manager
calls them all and isolates every one of them: a detector that raises, hangs on a missing
library, or returns nonsense is recorded as FAILED or UNAVAILABLE and the other ten carry on.
Recording never depends on any single detector succeeding.

Where evidence is gathered and where it is judged are deliberately separate. probe.js gathers
raw evidence inside the page; the confidence rules live here, in Python, so they can be tested
without a browser. Confidence answers one question only: if this screen changes, how likely is
this evidence to still point at the same element?
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import fingerprint as fp

# Ids like "ctl00_x_1739284" or "a3f9c1e8" are generated per session or per build.
GENERATED_ID = re.compile(r"\d{4,}|[0-9a-f]{8,}", re.I)
POSITIONAL_SELECTOR = re.compile(r":nth-of-type|>")


def short_error(exc):
    first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return f"{type(exc).__name__}: {first}"[:300]


class DiscoveryContext:
    """Everything the detectors may look at for one pointed-at element."""

    def __init__(self, payload=None, page=None, run_dir=None, index=1, frame=None):
        self.payload = payload if isinstance(payload, dict) else {}
        self.page = page              # a Playwright page, or None for a non-browser target
        self.frame = frame            # the frame the click happened in; may be an iframe
        self.run_dir = Path(run_dir) if run_dir else None
        self.index = index

    def evidence(self, key):
        """What probe.js observed for one layer inside the page."""
        layers = self.payload.get("layers")
        entry = layers.get(key) if isinstance(layers, dict) else None
        return entry if isinstance(entry, dict) else {}


class Detector:
    key = ""

    def observe(self, context):
        raise NotImplementedError


class PageEvidenceDetector(Detector):
    """Scores evidence probe.js already gathered. Status comes from the page, judgement from here."""

    def observe(self, context):
        entry = context.evidence(self.key)
        status = entry.get("status")
        if status in (fp.MISSING, fp.UNAVAILABLE):
            return fp.observation(self.key, status, entry.get("detail", ""), 0.0, **(entry.get("data") or {}))
        if status != fp.FOUND:
            return fp.observation(self.key, fp.UNAVAILABLE, "The page probe reported nothing for this layer.")
        data = entry.get("data") or {}
        judged = self.judge(data, entry.get("detail", ""))
        if judged is None:
            return fp.observation(self.key, fp.MISSING, entry.get("detail", "") or "The evidence was too weak to use.", 0.0, **data)
        confidence, detail = judged
        return fp.observation(self.key, fp.FOUND, detail, confidence, **data)

    def judge(self, data, detail):
        """Return (confidence, detail), or None when the evidence is not usable after all."""
        raise NotImplementedError


# --- 1. ordinary web elements -----------------------------------------------------------
class WebDetector(PageEvidenceDetector):
    key = "web"

    def judge(self, data, detail):
        identifier, unique = data.get("id", ""), data.get("unique_selector")
        if identifier:
            confidence = 0.72 if GENERATED_ID.search(identifier) else 0.95
            how = f"id {identifier}" + (" (looks generated)" if confidence < 0.9 else "")
        elif data.get("testid"):
            confidence, how = 0.93, f"test id {data['testid']}"
        elif data.get("name") and data.get("unique_name"):
            confidence, how = 0.88, f"name {data['name']}"
        elif data.get("selector") and unique:
            positional = bool(POSITIONAL_SELECTOR.search(data["selector"]))
            confidence, how = (0.62 if positional else 0.80), ("position in the page" if positional else "a unique selector")
        elif data.get("text"):
            confidence, how = 0.55, f'the text "{data["text"]}"'
        else:
            return None
        if unique is False:
            confidence = min(confidence, 0.5)
            how += " (not unique on this page)"
        return confidence, f"{data.get('kind', 'element')} · {how}"


# --- 2. frame and tab context -----------------------------------------------------------
class FrameDetector(PageEvidenceDetector):
    key = "frame"

    def judge(self, data, detail):
        chain = data.get("chain") or []
        if data.get("top_level"):
            confidence, where = 0.99, "the tab's main frame"
        else:
            holder = chain[-1] if chain else {}
            if holder.get("id") or holder.get("name"):
                confidence, where = 0.85, f"iframe {holder.get('id') or holder.get('name')}"
            elif holder.get("cross_origin"):
                confidence, where = 0.45, "a cross-origin iframe that cannot be named from inside"
            elif holder.get("src"):
                confidence, where = 0.72, f"iframe loaded from {holder['src']}"
            else:
                confidence, where = 0.50, "an unnamed iframe"
            where += f" · {data.get('depth', 1)} level(s) deep"
        if data.get("opens_new_tab"):
            where += " · this target opens a new tab"
        return confidence, where


# --- 3. Nexacro -------------------------------------------------------------------------
class NexacroDetector(PageEvidenceDetector):
    key = "nexacro"

    def judge(self, data, detail):
        component = data.get("name") or data.get("component")
        if not component:
            return None
        if data.get("type") and data.get("path"):
            confidence = 0.90  # live component object agrees with the rendered path
        elif data.get("path") and data.get("form"):
            confidence = 0.85
        elif data.get("path"):
            confidence = 0.65
        else:
            confidence = 0.55
        where = [f"form {data['form']}" if data.get("form") else "", f"grid {data['grid']}" if data.get("grid") else ""]
        if data.get("row") is not None:
            # Row and column follow the data, not the screen: they will not survive a refresh.
            confidence = min(confidence, 0.75)
            where.append(f"row {data['row']}" + (f" column {data['column']}" if data.get("column") is not None else "") + " (positional)")
        label = " ".join(x for x in [data.get("type", ""), component] if x)
        return confidence, " · ".join([label] + [x for x in where if x])


# --- 4. accessibility tree --------------------------------------------------------------
class AccessibilityDetector(Detector):
    """Reads the real tree over CDP rather than rebuilding a guess from HTML attributes."""
    key = "accessibility"

    @staticmethod
    def session_for(page, frame):
        """A same-process iframe shares its parent's CDP session; only a separate one gets its own."""
        if frame is not None and frame != page.main_frame:
            try:
                return page.context.new_cdp_session(frame)
            except Exception:
                pass
        return page.context.new_cdp_session(page)

    @staticmethod
    def node_for(session, token):
        """Search the whole tree, piercing iframes, so a click inside a frame is still resolved."""
        session.send("DOM.enable")
        session.send("DOM.getDocument", {"depth": -1, "pierce": True})
        search = session.send("DOM.performSearch", {"query": f'[data-smartops-probe="{token}"]'})
        try:
            if not search.get("resultCount"):
                return 0
            results = session.send("DOM.getSearchResults", {"searchId": search["searchId"], "fromIndex": 0, "toIndex": 1})
            return (results.get("nodeIds") or [0])[0]
        finally:
            try:
                session.send("DOM.discardSearchResults", {"searchId": search["searchId"]})
            except Exception:
                pass

    def observe(self, context):
        page, token = context.page, context.payload.get("token")
        if page is None:
            return fp.observation(self.key, fp.UNAVAILABLE, "No browser page is attached to this target.")
        if not token:
            return fp.observation(self.key, fp.MISSING, "The element could not be tagged for lookup.")
        session = self.session_for(page, context.frame)
        try:
            node_id = self.node_for(session, token)
            if not node_id:
                return fp.observation(self.key, fp.MISSING, "The element was gone before the tree could be read.")
            nodes = session.send("Accessibility.getPartialAXTree", {"nodeId": node_id, "fetchRelatives": False}).get("nodes", [])
            node = next((n for n in nodes if not n.get("ignored")), None)
            if node is None:
                return fp.observation(self.key, fp.MISSING, "The system hides this element from assistive technology.", ignored=True)
            role = (node.get("role") or {}).get("value", "")
            name = (node.get("name") or {}).get("value", "")
            states = {p["name"]: p.get("value", {}).get("value") for p in node.get("properties", []) if isinstance(p, dict) and p.get("name")}
            if role and name:
                confidence, detail = 0.82, f'{role} named "{name}"'
            elif name:
                confidence, detail = 0.50, f'unnamed role, accessible name "{name}"'
            elif role:
                confidence, detail = 0.45, f"{role} with no accessible name"
            else:
                return fp.observation(self.key, fp.MISSING, "The tree exposes no role or name for this element.", states=states)
            return fp.observation(self.key, fp.FOUND, detail, confidence, role=role, name=name, states=states)
        finally:
            try:
                session.detach()
            except Exception:
                pass


# --- 5. Windows UI Automation -----------------------------------------------------------
def windows_backend():
    """(module, reason). Never imports anything Windows-only off Windows."""
    if sys.platform != "win32":
        return None, f"Windows UI Automation needs Windows; this session runs on {sys.platform}."
    try:
        import uiautomation
    except Exception as exc:
        return None, "Install the 'uiautomation' package to enable desktop control discovery: " + short_error(exc)
    return uiautomation, ""


class WindowsUiaDetector(Detector):
    """Desktop controls. The availability check runs everywhere; the query path needs Windows."""
    key = "windows"

    def __init__(self, backend=windows_backend):
        self.backend = backend

    def observe(self, context):
        module, reason = self.backend()
        if module is None:
            return fp.observation(self.key, fp.UNAVAILABLE, reason)
        point = (context.payload.get("screen") or {})
        if not {"x", "y"} <= set(point):
            return fp.observation(self.key, fp.UNAVAILABLE, "No screen coordinates were captured for this target.")
        control = module.ControlFromPoint(int(point["x"]), int(point["y"]))
        if control is None:
            return fp.observation(self.key, fp.MISSING, "No desktop control sits at those screen coordinates.")
        data = {"control_type": getattr(control, "ControlTypeName", ""), "name": getattr(control, "Name", ""),
                "automation_id": getattr(control, "AutomationId", ""), "class_name": getattr(control, "ClassName", ""),
                "framework": getattr(control, "FrameworkId", "")}
        if data["automation_id"]:
            confidence, how = 0.90, "automation id " + data["automation_id"]
        elif data["name"] and data["control_type"]:
            confidence, how = 0.75, f'{data["control_type"]} named "{data["name"]}"'
        elif data["class_name"]:
            confidence, how = 0.50, "window class " + data["class_name"]
        else:
            return fp.observation(self.key, fp.MISSING, "The control exposes no usable identity.", **data)
        return fp.observation(self.key, fp.FOUND, how, confidence, **data)


# --- 6. anchors -------------------------------------------------------------------------
class AnchorDetector(PageEvidenceDetector):
    key = "anchor"

    def judge(self, data, detail):
        if not data.get("text"):
            return None
        distance = data.get("distance")
        if data.get("bound"):
            confidence = 0.90  # a label that points at the element by id, not by proximity
        elif isinstance(distance, (int, float)):
            confidence = max(0.35, 0.78 - min(distance, 600) / 1400)
        else:
            confidence = 0.45
        return confidence, f'"{data["text"]}" {data.get("side", "near")} the element'


# --- 7. visual fingerprint --------------------------------------------------------------
class VisualDetector(Detector):
    key = "visual"

    def __init__(self, padding=60):
        self.padding = padding

    def observe(self, context):
        page, box = context.page, context.payload.get("box")
        if page is None:
            return fp.observation(self.key, fp.UNAVAILABLE, "No browser page is attached to this target.")
        if context.run_dir is None:
            return fp.observation(self.key, fp.UNAVAILABLE, "No run folder was given to store pictures in.")
        if not isinstance(box, dict) or not (box.get("width") and box.get("height")):
            return fp.observation(self.key, fp.MISSING, "The element has no visible area to photograph.")
        size = page.viewport_size or {"width": 1920, "height": 1080}

        def clip(pad):
            left, top = max(0, box["x"] - pad), max(0, box["y"] - pad)
            return {"x": left, "y": top,
                    "width": max(1, min(box["width"] + pad * 2, size["width"] - left)),
                    "height": max(1, min(box["height"] + pad * 2, size["height"] - top))}

        context.run_dir.mkdir(parents=True, exist_ok=True)
        element_png = context.run_dir / f"element-{context.index}.png"
        context_png = context.run_dir / f"element-{context.index}-context.png"
        page.screenshot(path=str(element_png), clip=clip(0))
        page.screenshot(path=str(context_png), clip=clip(self.padding))
        area = box["width"] * box["height"]
        screen = max(1, size["width"] * size["height"])
        if box["width"] < 8 or box["height"] < 8:
            confidence, note = 0.35, " (too small to match reliably)"
        elif area > screen * 0.6:
            confidence, note = 0.45, " (covers most of the view)"
        else:
            confidence, note = 0.65, ""
        return fp.observation(self.key, fp.FOUND, f"{box['width']}x{box['height']} pixels, plus {self.padding} px of surroundings{note}.",
                              confidence, element_png=str(element_png), context_png=str(context_png), box=box)


# --- 8. relative position ---------------------------------------------------------------
class RelativeDetector(PageEvidenceDetector):
    key = "relative"

    def judge(self, data, detail):
        container = data.get("container", "")
        if not container:
            return None
        # Position inside a real container survives a resize; position inside <body> barely does.
        confidence = 0.35 if container.split("#")[0] in {"body", "html"} else 0.52
        return confidence, f"{round((data.get('fraction_x') or 0) * 100)}% across, {round((data.get('fraction_y') or 0) * 100)}% down its {container}"


# --- 9. keyboard route ------------------------------------------------------------------
class KeyboardDetector(PageEvidenceDetector):
    key = "keyboard"

    def judge(self, data, detail):
        if not data.get("reachable"):
            return None
        total = data.get("focus_total") or 0
        confidence = 0.62 if total <= 30 else 0.45  # long forms reshuffle their focus order
        if data.get("access_key"):
            confidence = min(0.85, confidence + 0.15)
        return confidence, f"Tab stop {(data.get('focus_order') or 0) + 1} of {total}" + (f" · shortcut {data['access_key']}" if data.get("access_key") else "")


# --- 10. OCR ----------------------------------------------------------------------------
def ocr_backend():
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
    except Exception as exc:
        return None, "Install pytesseract and the Tesseract engine to read text off the screen: " + short_error(exc)
    return pytesseract, ""


class OcrDetector(Detector):
    """Reads the visual layer's picture. Needs a real OCR engine; never guesses without one."""
    key = "ocr"

    def __init__(self, backend=ocr_backend):
        self.backend = backend

    def observe(self, context):
        module, reason = self.backend()
        if module is None:
            return fp.observation(self.key, fp.UNAVAILABLE, reason)
        picture = (context.payload.get("visual") or {}).get("context_png", "")
        if not picture or not Path(picture).is_file():
            return fp.observation(self.key, fp.UNAVAILABLE, "No picture was captured for this element to read.")
        words = (module.image_to_string(picture) or "").strip()
        if not words:
            return fp.observation(self.key, fp.MISSING, "No text could be read around this element.")
        return fp.observation(self.key, fp.FOUND, f'read "{words[:60]}"', 0.55, text=words[:400], picture=picture)


# --- 11. computer vision ----------------------------------------------------------------
def vision_backend():
    try:
        import cv2
    except Exception as exc:
        return None, "Install opencv-python to enable visual template matching: " + short_error(exc)
    return cv2, ""


class VisionDetector(Detector):
    """Last resort: a template descriptor of the element, for when nothing else identifies it."""
    key = "vision"

    def __init__(self, backend=vision_backend):
        self.backend = backend

    def observe(self, context):
        module, reason = self.backend()
        if module is None:
            return fp.observation(self.key, fp.UNAVAILABLE, reason)
        picture = (context.payload.get("visual") or {}).get("element_png", "")
        if not picture or not Path(picture).is_file():
            return fp.observation(self.key, fp.UNAVAILABLE, "No picture was captured for this element to describe.")
        image = module.imread(picture)
        if image is None:
            return fp.observation(self.key, fp.MISSING, "The stored picture could not be read back.")
        height, width = image.shape[:2]
        return fp.observation(self.key, fp.FOUND, f"template {width}x{height} kept for matching", 0.50,
                              template=picture, width=int(width), height=int(height))


DEFAULT_DETECTORS = (WebDetector(), FrameDetector(), NexacroDetector(), AccessibilityDetector(),
                     WindowsUiaDetector(), AnchorDetector(), VisualDetector(), RelativeDetector(),
                     KeyboardDetector(), OcrDetector(), VisionDetector())


class DiscoveryManager:
    """Runs every detector over one pointed-at element and merges the answers into one fingerprint."""

    def __init__(self, detectors=None):
        self.detectors = list(DEFAULT_DETECTORS if detectors is None else detectors)

    def discover(self, context):
        layers = {}
        for detector in self.detectors:
            # One detector must never be able to stop the other ten, or the recording.
            try:
                result = detector.observe(context)
                if not isinstance(result, dict) or result.get("status") not in fp.STATUSES:
                    raise ValueError("the detector returned no usable observation")
                layers[detector.key] = result
            except Exception as exc:
                layers[detector.key] = fp.observation(detector.key, fp.FAILED, "Detector error · " + short_error(exc))
        # The visual layer's files are what OCR and vision read, so pass them on within one pass.
        return fp.normalize({**context.payload, "layers": layers})

    def discover_element(self, payload, page=None, run_dir=None, index=1, frame=None):
        """Two passes: the picture is taken first so the picture-reading detectors have one."""
        context = DiscoveryContext(payload, page, run_dir, index, frame)
        first = self.discover(context)
        visual = first["layers"]["visual"]
        if visual["status"] == fp.FOUND and any(d.key in {"ocr", "vision"} for d in self.detectors):
            context.payload = {**context.payload, "visual": visual["data"]}
            rerun = {d.key: d for d in self.detectors if d.key in {"ocr", "vision"}}
            for key, detector in rerun.items():
                try:
                    first["layers"][key] = detector.observe(context)
                except Exception as exc:
                    first["layers"][key] = fp.observation(key, fp.FAILED, "Detector error · " + short_error(exc))
            first = fp.normalize(first)
        return first
