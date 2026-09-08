"""Discovery Manager rules: isolation, confidence, merging. No browser, no screen, no Windows."""
import pytest

from smartops_desktop import fingerprint as fp
from smartops_desktop.discovery import (AnchorDetector, DiscoveryContext, DiscoveryManager, Detector,
                                        FrameDetector, KeyboardDetector, NexacroDetector, OcrDetector,
                                        RelativeDetector, VisionDetector, WebDetector, WindowsUiaDetector)


def page_payload(**layers):
    return {"layers": {key: {"status": fp.FOUND, "detail": "", "data": data} for key, data in layers.items()}}


def observe(detector, data):
    return detector.observe(DiscoveryContext(page_payload(**{detector.key: data})))


# --- failure isolation ------------------------------------------------------------------
class Exploding(Detector):
    key = "web"

    def observe(self, context):
        raise RuntimeError("detector blew up")


class Nonsense(Detector):
    key = "anchor"

    def observe(self, context):
        return "not an observation"


def test_one_exploding_detector_never_stops_the_others():
    manager = DiscoveryManager([Exploding(), FrameDetector(), KeyboardDetector()])
    result = manager.discover(DiscoveryContext(page_payload(
        frame={"top_level": True}, keyboard={"reachable": True, "focus_order": 0, "focus_total": 4})))
    assert result["layers"]["web"]["status"] == fp.FAILED
    assert "detector blew up" in result["layers"]["web"]["detail"]
    assert result["layers"]["frame"]["status"] == fp.FOUND
    assert result["layers"]["keyboard"]["status"] == fp.FOUND


def test_a_detector_returning_nonsense_is_recorded_as_failed_not_trusted():
    result = DiscoveryManager([Nonsense()]).discover(DiscoveryContext({}))
    assert result["layers"]["anchor"]["status"] == fp.FAILED
    assert result["layers"]["anchor"]["confidence"] == 0.0


def test_detectors_left_out_of_the_run_are_marked_unavailable_not_missing():
    result = DiscoveryManager([FrameDetector()]).discover(DiscoveryContext(page_payload(frame={"top_level": True})))
    assert set(result["layers"]) == set(fp.LAYER_KEYS)
    assert result["layers"]["web"]["status"] == fp.UNAVAILABLE
    assert result["layers"]["ocr"]["status"] == fp.UNAVAILABLE


def test_every_default_detector_answers_even_with_an_empty_context():
    result = DiscoveryManager().discover(DiscoveryContext({}))
    assert all(entry["status"] in fp.STATUSES for entry in result["layers"].values())
    assert not [key for key, entry in result["layers"].items() if entry["status"] == fp.FAILED]


# --- confidence ranking -----------------------------------------------------------------
@pytest.mark.parametrize("data,expected", [
    ({"id": "btnSearch", "kind": "button", "unique_selector": True}, 0.95),
    ({"id": "ctl00_grid_1739284", "kind": "button", "unique_selector": True}, 0.72),
    ({"testid": "save", "kind": "button", "unique_selector": True}, 0.93),
    ({"name": "quantity", "unique_name": True, "kind": "field", "unique_selector": True}, 0.88),
    ({"selector": "form > div:nth-of-type(3) > button", "kind": "button", "unique_selector": True}, 0.62),
    ({"text": "Save report", "kind": "button", "unique_selector": None}, 0.55),
])
def test_web_confidence_follows_how_stable_the_evidence_is(data, expected):
    assert observe(WebDetector(), data)["confidence"] == expected


def test_web_evidence_that_matches_several_elements_is_capped():
    strong = observe(WebDetector(), {"id": "row", "kind": "button", "unique_selector": True})["confidence"]
    shared = observe(WebDetector(), {"id": "row", "kind": "button", "unique_selector": False})["confidence"]
    assert shared < strong and shared <= 0.5


def test_web_reports_missing_when_there_is_nothing_to_go_on():
    assert observe(WebDetector(), {"kind": "text"})["status"] == fp.MISSING


def test_nexacro_ranks_a_live_component_above_a_path_above_a_bare_name():
    live = observe(NexacroDetector(), {"name": "btnSearch", "type": "Button", "path": "a.form.btnSearch", "form": "divWork"})
    path = observe(NexacroDetector(), {"component": "btnSearch", "path": "a.form.btnSearch", "form": "divWork"})
    bare = observe(NexacroDetector(), {"component": "btnSearch"})
    assert live["confidence"] > path["confidence"] > bare["confidence"]


def test_a_nexacro_grid_cell_is_capped_because_row_and_column_follow_the_data():
    cell = observe(NexacroDetector(), {"name": "grdList", "type": "Grid", "path": "a.form.grdList.body.3.2",
                                       "form": "divWork", "grid": "grdList", "row": 3, "column": 2})
    assert cell["confidence"] <= 0.75
    assert "positional" in cell["detail"]


def test_an_anchor_bound_by_label_beats_one_found_by_distance():
    bound = observe(AnchorDetector(), {"text": "Quantity", "bound": True, "distance": 0, "side": "left of"})
    near = observe(AnchorDetector(), {"text": "Quantity", "bound": False, "distance": 40, "side": "left of"})
    far = observe(AnchorDetector(), {"text": "Quantity", "bound": False, "distance": 500, "side": "above"})
    assert bound["confidence"] > near["confidence"] > far["confidence"]


def test_position_inside_a_real_container_beats_position_inside_the_page_body():
    inside = observe(RelativeDetector(), {"container": "form#report", "fraction_x": 0.3, "fraction_y": 0.9})
    loose = observe(RelativeDetector(), {"container": "body", "fraction_x": 0.3, "fraction_y": 0.9})
    assert inside["confidence"] > loose["confidence"]


def test_an_unreachable_element_gets_no_keyboard_route():
    assert observe(KeyboardDetector(), {"reachable": False})["status"] == fp.MISSING


def test_ranking_puts_the_strongest_evidence_first_and_ignores_layers_that_missed():
    result = DiscoveryManager([WebDetector(), AnchorDetector(), RelativeDetector()]).discover(DiscoveryContext(page_payload(
        web={"id": "btnSearch", "kind": "button", "unique_selector": True},
        anchor={"text": "Report date", "distance": 40, "side": "above"},
        relative={"container": "form#report", "fraction_x": 0.1, "fraction_y": 0.9})))
    assert [key for key, _ in fp.rank(result)] == ["web", "anchor", "relative"]
    assert fp.score(result)["best"] == "web"


# --- adapters that must not pretend on the wrong platform ---------------------------------
def test_windows_ocr_and_vision_report_unavailable_with_a_reason_not_a_guess():
    result = DiscoveryManager([WindowsUiaDetector(), OcrDetector(), VisionDetector()]).discover(DiscoveryContext({}))
    for key in ("windows", "ocr", "vision"):
        assert result["layers"][key]["status"] == fp.UNAVAILABLE
        assert result["layers"][key]["detail"], f"{key} must explain why it could not run"
        assert result["layers"][key]["confidence"] == 0.0


def test_the_windows_adapter_reads_a_control_when_a_backend_is_present(tmp_path):
    """The backend is v2's probe_windows_at, which answers {'windows_uia': {...}}."""
    def fake_probe(x, y, folder, sequence):
        return {"windows_uia": {"available": True, "control_type": "ButtonControl", "name": "Search",
                                "automation_id": "btnSearch", "class_name": "Button"}}

    detector = WindowsUiaDetector(backend=lambda: (fake_probe, ""))
    result = detector.observe(DiscoveryContext({"screen": {"x": 120, "y": 340}}, run_dir=tmp_path))
    assert result["status"] == fp.FOUND and result["confidence"] == 0.90
    assert result["data"]["automation_id"] == "btnSearch"
    # Code existing is not proof: the maturity claim must stay honest.
    assert result["maturity"] == fp.IMPLEMENTED_UNVERIFIED


def test_the_windows_adapter_reports_the_backend_reason_when_nothing_is_readable(tmp_path):
    def fake_probe(x, y, folder, sequence):
        return {"windows_uia": {"available": False, "reason": "no_control_at_point"}}

    result = WindowsUiaDetector(backend=lambda: (fake_probe, "")).observe(
        DiscoveryContext({"screen": {"x": 1, "y": 1}}, run_dir=tmp_path))
    assert result["status"] == fp.UNAVAILABLE and "no_control_at_point" in result["detail"]


def test_the_real_windows_backend_declines_cleanly_off_windows():
    """Ported from v2 and exercised here: it must not import anything Windows-only on Linux."""
    from smartops_desktop.desktop_discovery import probe_windows_at
    answer = probe_windows_at(10, 10, "/tmp", 1)
    assert answer["windows_uia"]["available"] is False


# --- evidence merging -------------------------------------------------------------------
def test_merging_keeps_page_context_alongside_the_layers():
    payload = {**page_payload(web={"id": "qty", "kind": "field", "unique_selector": True}),
               "source": "https://example.org/report", "page_title": "Daily report"}
    result = DiscoveryManager([WebDetector()]).discover(DiscoveryContext(payload))
    assert result["source"] == "https://example.org/report"
    assert result["page_title"] == "Daily report"
    assert result["schema_version"] == 2


def test_the_picture_is_taken_before_the_detectors_that_read_pictures(tmp_path):
    seen = {}

    class FakeVisual(Detector):
        key = "visual"

        def observe(self, context):
            return fp.observation(self.key, fp.FOUND, "captured", 0.65, element_png="e.png", context_png="c.png")

    class RecordingOcr(OcrDetector):
        def observe(self, context):
            seen["visual"] = context.payload.get("visual")
            return fp.observation(self.key, fp.UNAVAILABLE, "no engine")

    DiscoveryManager([FakeVisual(), RecordingOcr()]).discover_element({}, page=None, run_dir=tmp_path)
    assert seen["visual"] == {"element_png": "e.png", "context_png": "c.png"}


def test_a_fingerprint_never_carries_field_contents():
    payload = page_payload(web={"id": "pwd", "kind": "field", "sensitive": True, "text": "", "unique_selector": True})
    result = DiscoveryManager([WebDetector()]).discover(DiscoveryContext(payload))
    body = repr(result)
    assert result["layers"]["web"]["data"]["sensitive"] is True
    assert "value" not in result["layers"]["web"]["data"] and "password" not in body.lower()


def test_the_diagnostic_reads_one_line_per_layer():
    result = DiscoveryManager([WebDetector(), WindowsUiaDetector()]).discover(DiscoveryContext(page_payload(
        web={"id": "btnSearch", "kind": "button", "unique_selector": True})))
    lines = fp.diagnostic(result)
    assert len(lines) == len(fp.LAYER_KEYS)
    assert lines[0].startswith("Web") and lines[0].endswith("✅ 0.95")
    assert "⚪ unavailable" in next(line for line in lines if line.startswith("Windows UIA"))
