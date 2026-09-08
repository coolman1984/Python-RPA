"""Exercise the real GUI and spawned worker, including from the frozen executable."""
import json
import os
import tempfile
import time
from pathlib import Path


def main():
    root = Path(os.environ.get("SMARTOPS_SELFTEST_DIR") or tempfile.mkdtemp(prefix="smartops-selftest-"))
    root.mkdir(parents=True, exist_ok=True)
    os.environ["SMARTOPS_DATA_DIR"] = str(root / "data")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .gui import MainWindow, STYLE
    from .core import validate_xlsx
    app = QApplication([])
    from PySide6.QtGui import QFontDatabase
    for font in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    demo_index = next(i for i, w in enumerate(window.items) if w["id"] == "welcome-demo")
    window.workflow_list.setCurrentRow(demo_index)
    state = {"ticks": 0, "started": time.monotonic()}

    def finish(success, detail):
        result = {"passed": success, "detail": detail, "gui_heartbeat_ticks": state["ticks"]}
        (root / "selftest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        window.grab().save(str(root / "desktop.png"))
        if window.process:
            window.process.terminate()
            window.process.join(2)
            window.process = None
        app.exit(0 if success else 1)

    def poll():
        state["ticks"] += 1
        if time.monotonic() - state["started"] > 40:
            finish(False, "Worker timed out")
        elif window.last_result and window.process is None:
            try:
                assert window.last_result["status"] == "passed", window.last_result
                assert window.last_result["validated"] is True
                checked = validate_xlsx(window.last_result["artifact"], 3, ["Date", "Line", "Quantity"])
                assert window.store.runs()[0]["status"] == "passed"
                assert any(e["type"] == "validation" for e in window.store.events(window.run_id))
                assert state["ticks"] >= 2
                finish(True, {"validation": checked, "artifact": window.last_result["artifact"]})
            except Exception as exc:
                finish(False, str(exc))

    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(poll)
    timer.start()
    QTimer.singleShot(150, lambda: window.start_worker("replay"))
    return app.exec()
