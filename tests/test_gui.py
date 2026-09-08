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


def test_tab_list_requires_an_explicit_choice(tmp_path):
    window = window_with(tmp_path)
    window.handle_event({"type": "tabs", "tabs": [{"title": "Report", "url": "https://example.org/a"}]})
    assert window.tabs.currentData() == ""
    assert window.tabs.count() == 2
    window.handle_event({"type": "tabs", "tabs": []})
    assert window.tabs.currentData() == ""
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
