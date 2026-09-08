"""Unified discovery model for recorder evidence.

The recorder may observe one user target through several independent channels.
This module normalizes those observations into one fingerprint without pretending
that an unavailable detector succeeded.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any
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
)


def normalize_capture(step: dict[str, Any], *, page_url: str = "", frame_url: str = "") -> dict[str, Any]:
    """Turn a browser recorder event into a multi-layer fingerprint.

    Browser-side probes may already attach evidence. Native/visual collectors can
    enrich the same schema later. Missing layers are explicitly marked unavailable.
    """
    fp = ElementFingerprint(
        action=str(step.get("action", "")),
        label=str(step.get("label", "")),
        page_url=page_url,
        frame_url=frame_url,
    )
    supplied = {item.get("layer"): item for item in step.get("evidence", []) if isinstance(item, dict)}
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
