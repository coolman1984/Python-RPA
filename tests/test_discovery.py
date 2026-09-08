from pathlib import Path

from smartops_desktop.core import validate_workflow
from smartops_desktop.discovery import summarize_layers
from smartops_desktop.desktop_recorder import _relative
from smartops_desktop.discovery_ui import layer_summary


def test_workflow_preserves_discovery_fingerprint():
    raw = {
        "schema_version": 1,
        "name": "Recorded",
        "steps": [{
            "action": "click",
            "selector": "#export",
            "discovery": {
                "version": 1,
                "layers": {
                    "dom": {"status": "ok", "role": "button", "text": "Export"},
                    "anchors": {"status": "ok", "candidates": [{"text": "Daily report"}]},
                },
            },
        }],
    }
    clean = validate_workflow(raw)
    assert clean["steps"][0]["discovery"]["layers"]["dom"]["role"] == "button"
    assert set(summarize_layers(clean["steps"][0])) == {"dom", "anchors"}


def test_relative_position_uses_window_not_absolute_screen():
    native = {
        "win32": {
            "status": "ok",
            "hierarchy": [{"rect": {"left": 100, "top": 200, "right": 500, "bottom": 600}}],
        }
    }
    result = _relative(native, 300, 300)
    assert result["status"] == "ok"
    assert result["relative_click"] == {"x": 0.5, "y": 0.25}


def test_recorder_contains_required_discovery_layers():
    script = Path(__file__).parents[1] / "smartops_desktop" / "recorder.js"
    text = script.read_text(encoding="utf-8")
    for marker in ["nexacro", "anchors", "visible_text", "relative_position", "screen_x", "aria_label"]:
        assert marker in text


def test_desktop_observer_never_declares_raw_typing_capture():
    source = (Path(__file__).parents[1] / "smartops_desktop" / "desktop_recorder.py").read_text(encoding="utf-8")
    assert "Raw character typing is deliberately not captured" in source
    assert "discover_native" in source
    assert "visual" in source


def test_fill_discovery_does_not_duplicate_value_in_visual_or_accessibility_evidence():
    source = (Path(__file__).parents[1] / "smartops_desktop" / "discovery.py").read_text(encoding="utf-8")
    assert 'skipped_privacy' in source
    assert '"value": _text(value)' not in source


def test_ui_summarizes_available_and_fallback_layers():
    step = {"discovery": {"layers": {
        "dom": {"status": "ok"},
        "nexacro": {"status": "unavailable"},
        "anchors": {"status": "ok"},
    }}}
    headline, detail = layer_summary(step)
    assert headline == "Seen 2/3"
    assert "dom" in detail and "anchors" in detail and "nexacro: unavailable" in detail
