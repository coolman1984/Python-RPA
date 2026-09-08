import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from smartops_desktop.core import Store, demo_workflow
from smartops_desktop.gui import MainWindow


def test_switching_workflows_preserves_unsaved_title(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = Store(tmp_path)
    other = demo_workflow() | {"id": "other", "name": "Other workflow"}
    store.save(other)
    window = MainWindow(store)
    demo = next(i for i, w in enumerate(window.items) if w["id"] == "welcome-demo")
    alt = next(i for i, w in enumerate(window.items) if w["id"] == "other")
    window.workflow_list.setCurrentRow(demo)
    window.name.setText("Renamed demo")
    window.workflow_list.setCurrentRow(alt)
    window.workflow_list.setCurrentRow(demo)
    assert window.name.text() == "Renamed demo"
    window.close()


def window_with(tmp_path):
    QApplication.instance() or QApplication([])
    store = Store(tmp_path)
    store.save(demo_workflow() | {"id": "other", "name": "Other workflow"})
    return MainWindow(store)


def test_invalid_name_never_traps_the_user_in_the_window(tmp_path, monkeypatch):
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox
    window = window_with(tmp_path)
    window.workflow_list.setCurrentRow(next(i for i, w in enumerate(window.items) if w["id"] == "welcome-demo"))
    window.name.setText("   ")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert asked and "could not be saved" in asked[0]


def test_invalid_name_is_reported_when_switching_away(tmp_path, monkeypatch):
    window = window_with(tmp_path)
    demo = next(i for i, w in enumerate(window.items) if w["id"] == "welcome-demo")
    alt = next(i for i, w in enumerate(window.items) if w["id"] == "other")
    window.workflow_list.setCurrentRow(demo)
    window.name.setText("")
    warned = []
    monkeypatch.setattr(window, "notice", lambda title, message: warned.append(str(message)))
    window.workflow_list.setCurrentRow(alt)
    assert warned and "name" in warned[0]
    window.close()


def test_tab_list_requires_an_explicit_choice_and_carries_stable_identity(tmp_path):
    window = window_with(tmp_path)
    same_url = "https://example.org/report"
    window.handle_event({"type": "tabs", "tabs": [
        {"target_id": "T1", "title": "Report", "url": same_url, "location": "example.org/report"},
        {"target_id": "T2", "title": "Report copy", "url": same_url, "location": "example.org/report"},
    ]})
    assert window.tabs.currentData() == {}, "the first entry must be a placeholder, not a real tab"
    assert window.tabs.count() == 3
    # Two tabs on the same address stay distinguishable, which a URL alone could never do.
    window.tabs.setCurrentIndex(1)
    first = window.tabs.currentData()["target_id"]
    window.tabs.setCurrentIndex(2)
    assert first != window.tabs.currentData()["target_id"]
    window.handle_event({"type": "tabs", "tabs": []})
    assert window.tabs.currentData() == {}
    window.close()


def test_recording_nothing_does_not_claim_a_saved_workflow(tmp_path):
    window = window_with(tmp_path)
    window.workflow_list.setCurrentRow(0)
    window.mode = "record"
    window.captured = []
    window.run_id = None
    window.last_result = {"status": "recorded"}
    window.process = type("P", (), {"join": lambda self, **k: None, "close": lambda self: None})()
    window.output = type("Q", (), {"close": lambda self: None})()
    before = len(window.store.list_workflows())
    window.finish_worker()
    assert "nothing was captured" in window.status.text()
    assert len(window.store.list_workflows()) == before
    window.close()


def test_recording_stops_collecting_at_the_schema_limit(tmp_path):
    window = window_with(tmp_path)
    window.mode = "record"
    window.captured = []
    window.run_id = None
    for _ in range(1005):
        window.handle_event({"type": "recorded", "step": {"action": "click", "selector": "#a"}})
    assert len(window.captured) == 1000
    assert "limit reached" in window.status.text()
    window.store.save({"schema_version": 1, "name": "Recording", "steps": window.captured})
    window.close()


def sample_fingerprint():
    from smartops_desktop import fingerprint as fp
    return fp.normalize({"source": "https://example.org/report", "layers": {
        "web": {"status": fp.FOUND, "detail": "button · id btnSearch", "confidence": 0.95, "data": {"id": "btnSearch", "tag": "button"}},
        "nexacro": {"status": fp.FOUND, "detail": "Button btnSearch · form divWork", "confidence": 0.90, "data": {"name": "btnSearch", "form": "divWork"}},
        "accessibility": {"status": fp.MISSING, "detail": "The system hides this element."},
        "anchor": {"status": fp.FOUND, "detail": '"Report date" above the element', "confidence": 0.70, "data": {"text": "Report date"}},
        "relative": {"status": fp.FOUND, "detail": "3% across, 91% down its form", "confidence": 0.52},
    }})


def test_radar_card_shows_one_row_per_layer_with_verdict_confidence_and_proof(tmp_path):
    from smartops_desktop import fingerprint as fp
    window = window_with(tmp_path)
    window.add_element(sample_fingerprint())
    assert window.radar_table.rowCount() == len(fp.LAYERS)
    rows = {window.radar_table.item(r, 0).text(): [window.radar_table.item(r, c).text() for c in range(5)]
            for r in range(window.radar_table.rowCount())}
    assert rows["Web element"][1:3] == ["✅", "0.95"]
    assert rows["Nexacro component"][1] == "✅"
    assert rows["Accessibility tree"][1:3] == ["❌", "—"]
    assert rows["Windows UI Automation"][1] == "⚪"
    # A detector that has never met the real thing must say so, however well it scored.
    assert rows["Nexacro component"][4] == "code only, never proven"
    assert rows["Web element"][4] == "verified in a real run"
    assert window.radar_headline.text().startswith("4 of 12")
    assert "Strongest: Web at 0.95" in window.radar_headline.text()
    assert window.element_list.count() == 1 and "btnSearch" in window.element_list.item(0).text()
    window.close()


def test_radar_keeps_every_pointed_element_and_switches_between_them(tmp_path):
    from smartops_desktop import fingerprint as fp
    window = window_with(tmp_path)
    window.add_element(sample_fingerprint())
    window.add_element(fp.normalize({"layers": {"web": {"status": fp.FOUND, "detail": "field · id qty", "confidence": 0.95, "data": {"id": "qty"}}}}))
    assert window.element_list.count() == 2
    window.element_list.setCurrentRow(0)
    assert window.radar_headline.text().startswith("4 of 12")
    window.element_list.setCurrentRow(1)
    assert window.radar_headline.text().startswith("1 of 12")
    window.close()


def test_starting_the_radar_without_a_tab_asks_for_one(tmp_path, monkeypatch):
    window = window_with(tmp_path)
    window.workflow_list.setCurrentRow(0)
    asked = []
    monkeypatch.setattr(window, "notice", lambda title, message: asked.append(title))
    window.start_worker("inspect")
    assert asked == ["Choose Chrome tab"]
    assert window.process is None
    window.close()


def test_editing_a_recorded_step_never_discards_its_fingerprint(tmp_path):
    """The old dialog rebuilt a step from its visible fields. Everything the recorder attached —
    fingerprint, detected_by, capture metadata — vanished on the first edit."""
    from smartops_desktop.gui import StepDialog
    QApplication.instance() or QApplication([])
    recorded = {
        "action": "click", "selector": "#btnSearch", "label": "Search",
        "fingerprint": {"schema_version": 2, "layers": {"web": {"status": "found", "confidence": 0.95}}},
        "detected_by": ["web", "anchor"], "best_layer": "web",
        "captured": "2026-09-08T16:00:00+00:00", "frame": "#reportFrame",
    }
    dialog = StepDialog(recorded)
    dialog.fields["label"].setText("Search for September")
    edited = dialog.value()
    assert edited["label"] == "Search for September"
    assert edited["fingerprint"] == recorded["fingerprint"]
    assert edited["detected_by"] == ["web", "anchor"]
    assert edited["best_layer"] == "web" and edited["captured"] == recorded["captured"]
    dialog.deleteLater()


def test_editing_a_step_still_drops_fields_the_new_action_does_not_use(tmp_path):
    from smartops_desktop.gui import StepDialog
    QApplication.instance() or QApplication([])
    dialog = StepDialog({"action": "wait", "seconds": 5, "fingerprint": {"kept": True}})
    dialog.action.setCurrentText("demo_export")
    edited = dialog.value()
    assert "seconds" not in edited and edited["action"] == "demo_export"
    assert edited["fingerprint"] == {"kept": True}
    dialog.deleteLater()
