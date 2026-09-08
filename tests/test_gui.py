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
