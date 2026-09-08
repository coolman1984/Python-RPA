from smartops_desktop.discovery import ElementFingerprint, LAYER_ORDER, normalize_capture


def test_normalize_web_capture_marks_all_layers():
    result = normalize_capture({"action": "click", "selector": "#go", "label": "Go"}, page_url="https://example.test")
    assert result["best_layer"] == "web"
    assert [e["layer"] for e in result["evidence"]] == list(LAYER_ORDER)
    web = result["evidence"][0]
    assert web["available"] is True
    assert web["identity"]["selector"] == "#go"


def test_supplied_evidence_can_beat_web():
    result = normalize_capture({
        "action": "click", "selector": "div:nth-of-type(9)",
        "evidence": [
            {"layer": "web", "available": True, "confidence": .3, "identity": {"selector": "div:nth-of-type(9)"}},
            {"layer": "nexacro", "available": True, "confidence": .95, "identity": {"id": "btnSearch", "form": "frmMain"}},
        ]
    })
    assert result["best_layer"] == "nexacro"


def test_confidence_is_clamped():
    fp = ElementFingerprint("click")
    fp.add("web", True, 5, {"selector": "#x"})
    assert fp.evidence[0].confidence == 1.0
