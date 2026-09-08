from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, QProcess
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QStackedWidget, QLineEdit, QTextEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QFileDialog, QMessageBox,
    QInputDialog, QComboBox, QSpinBox, QFormLayout, QDialog, QDialogButtonBox, QCheckBox,
    QProgressBar, QFrame, QSplitter)

from . import fingerprint as fp
from .core import Store, ACTIONS, validate_workflow, atomic_text, http_url
from .worker import worker_main

STYLE = """
QWidget { font-family: 'Segoe UI'; font-size: 10pt; color: #192c45; }
QMainWindow, #content { background: #f4f6fa; }
#sidebar { background: #14253d; }
#brand { color: white; font-size: 23pt; font-weight: 700; }
#tagline { color: #9eafc6; font-size: 9pt; }
#nav { background: transparent; border: none; color: #cfdaea; outline: none; }
#nav::item { padding: 15px 14px; margin: 4px 0; border-radius: 7px; }
#nav::item:selected { background: #284462; color: white; }
#eyebrow { color: #59718e; font-size: 9pt; font-weight: 700; }
#heading { font-size: 24pt; font-weight: 650; color: #152b47; }
#subheading { color: #667b93; }
#card { background: white; border: 1px solid #dfe6ef; border-radius: 10px; }
QPushButton { background: white; border: 1px solid #d1dce8; border-radius: 6px; padding: 9px 14px; font-weight: 600; }
QPushButton:hover { background: #eaf0f8; border-color: #9eb2cc; }
QPushButton:disabled { color: #9aa8b9; background: #f0f3f7; }
QPushButton#primary { background: #2365d5; color: white; border: 1px solid #2365d5; }
QPushButton#primary:hover { background: #1955b9; }
QPushButton#primary:disabled { background: #9db8e6; border-color: #9db8e6; }
QPushButton#danger { color: #b33e4d; }
QLineEdit, QComboBox, QSpinBox, QTextEdit { background: white; border: 1px solid #d4deea; border-radius: 5px; padding: 7px; selection-background-color: #2365d5; }
QListWidget { background: white; border: 1px solid #dfe6ef; border-radius: 7px; }
QListWidget::item { padding: 13px 9px; }
QListWidget::item:selected { background: #e7effd; color: #174fb0; }
QTableWidget { background: white; alternate-background-color: #f8fafd; border: 1px solid #dfe6ef; border-radius: 7px; gridline-color: #edf1f7; selection-background-color: #e7effd; selection-color: #192c45; }
QHeaderView::section { background: #edf2f8; color: #526983; padding: 10px; border: none; font-weight: 600; }
QProgressBar { border: none; background: #e1e9f4; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #2773df; border-radius: 3px; }
"""


def button(text, callback, primary=False):
    item = QPushButton(text)
    if primary:
        item.setObjectName("primary")
    item.clicked.connect(callback)
    return item


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    item.setWordWrap(True)
    return item


class StepDialog(QDialog):
    def __init__(self, step=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Workflow step")
        self.resize(560, 480)
        self.original = step or {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.action = QComboBox()
        self.action.addItems(ACTIONS)
        self.action.setCurrentText(self.original.get("action", "click"))
        self.fields = {}
        form.addRow("Action", self.action)
        for key, title in [("label", "Step name"), ("url", "Page URL"), ("selector", "CSS / Playwright selector"), ("value", "Value / key"), ("frame", "Frame selector (optional)"), ("path", "Existing XLSX path (optional)"), ("sheet", "Worksheet (optional)"), ("required_columns", "Required columns (comma separated)")]:
            value = self.original.get(key, "")
            if isinstance(value, list):
                value = ", ".join(value)
            field = QLineEdit(str(value))
            self.fields[key] = field
            form.addRow(title, field)
        self.rows = QSpinBox()
        self.rows.setRange(0, 10000000)
        self.rows.setValue(self.original.get("min_rows", 1))
        form.addRow("Minimum data rows", self.rows)
        self.seconds = QSpinBox()
        self.seconds.setRange(0, 300)
        self.seconds.setValue(int(self.original.get("seconds", 1)))
        form.addRow("Wait (seconds)", self.seconds)
        self.checked = QCheckBox("Checked")
        self.checked.setChecked(self.original.get("checked", True))
        form.addRow("Checkbox target", self.checked)
        layout.addLayout(form)
        layout.addWidget(label("Download waits for a file after clicking its selector, then validates Excel contents. Use manual sign-in before recording or replay.", "subheading"))
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.action.currentTextChanged.connect(self.update_fields)
        self.update_fields()

    def update_fields(self):
        action = self.action.currentText()
        enabled = {"label"}
        if action == "navigate": enabled.add("url")
        if action in {"click", "fill", "press", "select", "check", "download"}: enabled |= {"selector", "frame"}
        if action in {"fill", "press", "select"}: enabled.add("value")
        if action == "validate_xlsx": enabled |= {"path", "sheet", "required_columns"}
        if action == "download": enabled.add("required_columns")
        for key, field in self.fields.items(): field.setEnabled(key in enabled)
        self.rows.setEnabled(action in {"validate_xlsx", "download"})
        self.seconds.setEnabled(action == "wait")
        self.checked.setEnabled(action == "check")

    EDITABLE = ("label", "url", "selector", "value", "frame", "path", "sheet", "required_columns",
                "min_rows", "seconds", "checked")

    def value(self):
        # Start from the original step so fingerprint, detected_by, capture metadata and anything
        # else the recorder attached survive an edit. Only the editable keys are overwritten.
        result = json.loads(json.dumps(self.original)) if self.original else {}
        for key in self.EDITABLE:
            result.pop(key, None)
        result["action"] = self.action.currentText()
        for key, field in self.fields.items():
            if field.isEnabled() and (field.text() or key == "value"):
                result[key] = field.text() if key == "value" else field.text().strip()
        if "required_columns" in result:
            result["required_columns"] = [s.strip() for s in result["required_columns"].split(",") if s.strip()]
        if self.rows.isEnabled(): result["min_rows"] = self.rows.value()
        if self.seconds.isEnabled(): result["seconds"] = self.seconds.value()
        if self.checked.isEnabled(): result["checked"] = self.checked.isChecked()
        validate_workflow({"schema_version": 1, "name": "Step", "steps": [result]})
        return result

    def accept(self):
        try:
            self.value()
            super().accept()
        except Exception as exc:
            QMessageBox.warning(self, "Check step", str(exc))


class MainWindow(QMainWindow):
    def __init__(self, store=None):
        super().__init__()
        self.store = store or Store()
        self.current = None
        self.process = None
        self.run_id = None
        self.mode = None
        self.artifact = ""
        self.last_result = None
        self.captured = []
        self.elements = []
        self.stop_deadline = None
        self.dead_since = None
        self.aux = []
        self.setWindowTitle("SmartOps · Desktop Core")
        self.resize(1220, 840)
        self.setMinimumSize(1000, 720)
        root = QWidget()
        self.setCentralWidget(root)
        horizontal = QHBoxLayout(root)
        horizontal.setContentsMargins(0, 0, 0, 0)
        horizontal.setSpacing(0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(205)
        sidebar = QVBoxLayout(side)
        sidebar.setContentsMargins(20, 28, 20, 20)
        sidebar.addWidget(label("SmartOps", "brand"))
        sidebar.addWidget(label("YOUR DESKTOP. IN FLOW.", "tagline"))
        sidebar.addSpacing(35)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItems(["Workflows", "Element radar", "Run history", "Settings"])
        sidebar.addWidget(self.nav)
        sidebar.addWidget(label("DESKTOP CORE  /  0.1\nLocal storage · Windows", "tagline"))
        horizontal.addWidget(side)
        self.stack = QStackedWidget()
        self.stack.setObjectName("content")
        horizontal.addWidget(self.stack)
        self.build_workflows()
        self.build_radar()
        self.build_history()
        self.build_settings()
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.reload_workflows()
        self.reload_history()

    def page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 25, 28, 24)
        layout.setSpacing(12)
        layout.addWidget(label("SMARTOPS WORKSPACE", "eyebrow"))
        layout.addWidget(label(title, "heading"))
        layout.addWidget(label(subtitle, "subheading"))
        self.stack.addWidget(page)
        return layout

    def build_workflows(self):
        layout = self.page("Make repeatable work simple.", "Build a workflow, review each step, and keep evidence of every run.")
        toolbar = QHBoxLayout()
        self.new_button = button("+ New workflow", self.new_workflow, True)
        self.import_button = button("Import JSON", self.import_workflow)
        toolbar.addWidget(self.new_button)
        toolbar.addWidget(self.import_button)
        toolbar.addStretch()
        toolbar.addWidget(button("Open data folder", lambda: os.startfile(str(self.store.root))))
        layout.addLayout(toolbar)
        split = QSplitter()
        self.workflow_list = QListWidget()
        self.workflow_list.setMinimumWidth(185)
        self.workflow_list.currentRowChanged.connect(self.select_workflow)
        split.addWidget(self.workflow_list)
        right = QWidget()
        details = QVBoxLayout(right)
        details.setContentsMargins(14, 0, 0, 0)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Workflow name")
        self.description = QLineEdit()
        self.description.setPlaceholderText("What does this workflow accomplish?")
        details.addWidget(self.name)
        details.addWidget(self.description)
        chrome = QHBoxLayout()
        self.tabs = QComboBox()
        self.tabs.setMinimumWidth(160)
        self.tabs.addItem("Choose a Chrome tab for browser steps", "")
        self.refresh_button = button("Connect Chrome", lambda: self.start_worker("tabs"))
        chrome.addWidget(self.tabs, 1)
        chrome.addWidget(self.refresh_button)
        details.addLayout(chrome)
        actions = QHBoxLayout()
        self.run_button = button("Test run", lambda: self.start_worker("replay"), True)
        self.record_button = button("●  Record", lambda: self.start_worker("record"))
        self.stop_button = button("■  Stop", self.stop_worker)
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.save_button = button("Save", self.save_current)
        self.export_button = button("Export", self.export_workflow)
        for item in [self.run_button, self.record_button, self.stop_button, self.save_button, self.export_button]: actions.addWidget(item)
        details.addLayout(actions)
        self.status = label("Ready · Run the local demo to get started.")
        details.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        details.addWidget(self.progress)
        self.steps = QTableWidget(0, 4)
        self.steps.setHorizontalHeaderLabels(["#", "Action", "Step / target", "Status"])
        self.steps.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.steps.setColumnWidth(0, 35)
        self.steps.setColumnWidth(1, 115)
        self.steps.setColumnWidth(3, 85)
        self.steps.verticalHeader().hide()
        self.steps.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.steps.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.steps.setAlternatingRowColors(True)
        self.steps.cellDoubleClicked.connect(lambda *_: self.edit_step())
        details.addWidget(self.steps, 1)
        editor = QHBoxLayout()
        self.edit_buttons = [button("+ Step", self.add_step), button("Edit", self.edit_step), button("↑", lambda: self.move_step(-1)), button("↓", lambda: self.move_step(1)), button("Remove", self.remove_step)]
        for item in self.edit_buttons: editor.addWidget(item)
        editor.addStretch()
        details.addLayout(editor)
        self.artifact_label = label("No output file yet.", "subheading")
        details.addWidget(self.artifact_label)
        split.addWidget(right)
        split.setSizes([205, 725])
        layout.addWidget(split, 1)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(125)
        self.log.setPlaceholderText("Run activity appears here. Your workflows and results stay on this PC.")
        layout.addWidget(self.log)

    def build_radar(self):
        layout = self.page("What can SmartOps see?", "Point at any element and read back every independent way it can be recognised.")
        toolbar = QHBoxLayout()
        self.radar_button = button("📡  Start radar", lambda: self.start_worker("inspect"), True)
        self.radar_stop = button("■  Stop", self.stop_worker)
        self.radar_stop.setObjectName("danger")
        self.radar_stop.setEnabled(False)
        toolbar.addWidget(self.radar_button)
        toolbar.addWidget(self.radar_stop)
        toolbar.addStretch()
        toolbar.addWidget(button("Open fingerprint folder", self.open_radar_folder))
        layout.addLayout(toolbar)
        self.radar_status = label("Pick a Chrome tab on the Workflows page, then start the radar and click an element.")
        layout.addWidget(self.radar_status)
        split = QSplitter()
        self.element_list = QListWidget()
        self.element_list.setMinimumWidth(175)
        self.element_list.currentRowChanged.connect(self.show_element)
        split.addWidget(self.element_list)
        right = QWidget()
        card = QVBoxLayout(right)
        card.setContentsMargins(14, 0, 0, 0)
        self.radar_headline = label("No element pointed at yet.", "heading")
        card.addWidget(self.radar_headline)
        self.radar_table = QTableWidget(len(fp.LAYERS), 5)
        self.radar_table.setHorizontalHeaderLabels(["Layer", "Sees it?", "Confidence", "What it sees", "Detector proven?"])
        self.radar_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.radar_table.setColumnWidth(0, 150)
        self.radar_table.setColumnWidth(1, 68)
        self.radar_table.setColumnWidth(2, 80)
        self.radar_table.setColumnWidth(4, 175)
        self.radar_table.verticalHeader().hide()
        self.radar_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.radar_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.radar_table.setAlternatingRowColors(True)
        card.addWidget(self.radar_table, 1)
        self.radar_image = QLabel()
        self.radar_image.setFixedHeight(150)
        self.radar_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.radar_image.setObjectName("card")
        card.addWidget(self.radar_image)
        self.radar_json = QTextEdit()
        self.radar_json.setReadOnly(True)
        self.radar_json.setMaximumHeight(140)
        self.radar_json.setPlaceholderText("The full fingerprint appears here once you point at an element.")
        card.addWidget(self.radar_json)
        split.addWidget(right)
        split.setSizes([175, 755])
        layout.addWidget(split, 1)
        self.render_radar(None)

    def render_radar(self, fingerprint):
        rows = fp.radar(fingerprint or {})
        proof = {fp.VERIFIED: "verified in a real run", fp.IMPLEMENTED_UNVERIFIED: "code only, never proven", fp.NOT_IMPLEMENTED: "not implemented"}
        for row, item in enumerate(rows):
            values = [item["title"], item["icon"], f"{item['confidence']:.2f}" if item["status"] == fp.FOUND else "—",
                      item["detail"] or item["purpose"], proof[item["maturity"]]]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column in (1, 2):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 4 and item["maturity"] != fp.VERIFIED:
                    cell.setForeground(QColor("#a8701f"))
                elif item["status"] != fp.FOUND:
                    cell.setForeground(QColor("#8b98a8" if item["status"] == fp.UNAVAILABLE else "#b33e4d"))
                self.radar_table.setItem(row, column, cell)
        if not fingerprint:
            self.radar_headline.setText("No element pointed at yet.")
            self.radar_json.clear()
            self.radar_image.clear()
            self.radar_image.setText("No picture yet.")
            return
        self.radar_headline.setText(fp.score(fingerprint)["headline"])
        self.radar_json.setPlainText(json.dumps(fingerprint, indent=2, ensure_ascii=False))
        picture = (fingerprint["layers"]["visual"]["data"] or {}).get("context_png", "")
        pixmap = QPixmap(picture) if picture and Path(picture).is_file() else QPixmap()
        if pixmap.isNull():
            self.radar_image.clear()
            self.radar_image.setText("No picture for this element.")
        else:
            self.radar_image.setPixmap(pixmap.scaled(self.radar_image.width() or 600, 146, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def add_element(self, fingerprint):
        self.elements.append(fingerprint)
        web = fingerprint["layers"]["web"]["data"] or {}
        nexacro = fingerprint["layers"]["nexacro"]["data"] or {}
        name = nexacro.get("name") or nexacro.get("component") or web.get("text") or web.get("id") or web.get("tag") or "element"
        self.element_list.addItem(f"{len(self.elements)}. {name}"[:60])
        self.element_list.setCurrentRow(len(self.elements) - 1)
        summary = fp.score(fingerprint)
        self.radar_status.setText(f"Radar armed · {len(self.elements)} element(s) pointed at · last one recognised by "
                                  f"{summary['identified']} layer(s), strongest {summary['best'] or 'none'} at {summary['best_confidence']:.2f}")

    def show_element(self, index):
        self.render_radar(self.elements[index] if 0 <= index < len(self.elements) else None)

    def open_radar_folder(self):
        path = self.store.root / "runs" / (self.run_id or "")
        if not self.run_id or not path.is_dir():
            self.notice("No fingerprints yet", "Start the radar and point at an element first.")
            return
        os.startfile(str(path))

    def build_history(self):
        layout = self.page("Every run, accounted for.", "Inspect outcomes, validation results and the local output folder.")
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(["Started (UTC)", "Workflow", "Result", "Output file"])
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history.itemSelectionChanged.connect(self.show_run)
        layout.addWidget(self.history, 1)
        self.history_detail = QTextEdit()
        self.history_detail.setReadOnly(True)
        layout.addWidget(self.history_detail, 1)
        layout.addWidget(button("Open selected run folder", self.open_run_folder))

    def build_settings(self):
        layout = self.page("A workspace that stays yours.", "Chrome connection and local preferences. No cloud service is required.")
        settings = self.store.settings()
        form = QFormLayout()
        self.endpoint = QLineEdit(settings["cdp_url"])
        self.launcher_path = QLineEdit(settings["chrome_launcher"])
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setValue(int(settings["timeout_seconds"]))
        form.addRow("Chrome CDP endpoint", self.endpoint)
        form.addRow("Approved Chrome launcher", self.launcher_path)
        form.addRow("Step timeout (seconds)", self.timeout)
        layout.addLayout(form)
        layout.addWidget(button("Save settings", self.save_settings, True))
        layout.addSpacing(15)
        layout.addWidget(label("Connecting Chrome", "heading"))
        layout.addWidget(label("Recording and replay require Google Chrome already running with local remote debugging enabled. Enter its CDP address above, then click Connect Chrome on the Workflows page. SmartOps attaches to the tab you choose and disconnects when finished. It does not close Chrome."))
        layout.addWidget(label("Your normal signed-in Chrome profile may not support remote debugging. Chrome can require a separate user-data directory. This version does not reconfigure, restart or copy your Chrome profiles. Samsung portals must use your approved corporate Profile 19; sign in manually before recording."))
        layout.addWidget(label("The recorder captures clicks, field changes and Enter in the selected tab's main frame. Review the starting URL, remove duplicate actions, and change the final export click to a Download step. Cross-frame replay can use a frame selector; popup and Nexacro-specific flows need explicitly authored steps. The Nexacro probe reports availability only."))
        layout.addWidget(button("Open a URL with approved Chrome launcher", self.open_url))
        layout.addStretch()
        layout.addWidget(label("Local data: " + str(self.store.root), "subheading"))

    def notice(self, title, message):
        QMessageBox.warning(self, title, str(message))

    def reload_workflows(self, selected=None):
        self.items = self.store.list_workflows()
        self.workflow_list.blockSignals(True)
        self.workflow_list.clear()
        for flow in self.items:
            self.workflow_list.addItem(flow["name"])
        index = next((i for i, w in enumerate(self.items) if w["id"] == selected), 0)
        self.workflow_list.setCurrentRow(index)
        self.workflow_list.blockSignals(False)
        self.select_workflow(index)

    def select_workflow(self, index):
        if index < 0 or index >= len(self.items): return
        if self.current:
            error = self.persist()
            if error: self.notice("Edit not saved", error + "\n\nSwitching workflows discards that change.")
        self.current = json.loads(json.dumps(self.items[index]))
        self.name.setText(self.current["name"])
        self.description.setText(self.current.get("description", ""))
        self.render_steps()

    def render_steps(self):
        self.steps.setRowCount(len(self.current["steps"]))
        for row, step in enumerate(self.current["steps"]):
            for col, value in enumerate([row+1, step["action"], step.get("label") or step.get("selector") or step.get("url") or step.get("path") or "—", "Ready"]):
                self.steps.setItem(row, col, QTableWidgetItem(str(value)))

    def persist(self):
        """Save pending name/description edits. Returns None, or the reason it could not be saved."""
        if not self.current: return "No workflow is selected."
        try:
            self.current["name"] = self.name.text()
            self.current["description"] = self.description.text()
            self.current = self.store.save(self.current)
            # Look the row up by ID: while switching workflows the selected row is already the new one.
            for i, value in enumerate(self.items):
                if value["id"] == self.current["id"]:
                    self.items[i] = json.loads(json.dumps(self.current))
                    self.workflow_list.item(i).setText(self.current["name"])
            self.status.setText("Saved locally.")
            return None
        except Exception as exc:
            return str(exc)

    def save_current(self):
        error = self.persist()
        if error: self.notice("Could not save", error)
        return error is None

    def new_workflow(self):
        name, ok = QInputDialog.getText(self, "New workflow", "Workflow name")
        if ok and name.strip():
            if not self.save_current(): return
            workflow = self.store.save({"schema_version": 1, "name": name, "steps": []})
            self.current = None
            self.reload_workflows(workflow["id"])

    def import_workflow(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import workflow", "", "Workflow JSON (*.json)")
        if not path: return
        try:
            if Path(path).stat().st_size > 2 * 1024 * 1024: raise ValueError("Workflow exceeds 2 MB.")
            value = validate_workflow(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            value["id"] = str(uuid4())
            self.save_current()
            self.store.save(value)
            self.current = None
            self.reload_workflows(value["id"])
        except Exception as exc: self.notice("Import failed", exc)

    def export_workflow(self):
        if not self.save_current(): return
        path, _ = QFileDialog.getSaveFileName(self, "Export workflow", self.current["id"] + ".json", "Workflow JSON (*.json)")
        if path:
            try: atomic_text(Path(path), json.dumps(self.current, ensure_ascii=False, indent=2))
            except Exception as exc: self.notice("Export failed", exc)

    def add_step(self):
        dialog = StepDialog(parent=self)
        if dialog.exec():
            self.current["steps"].append(dialog.value())
            self.render_steps()
            self.save_current()

    def edit_step(self):
        if self.process: return
        row = self.steps.currentRow()
        if row < 0: return
        dialog = StepDialog(self.current["steps"][row], self)
        if dialog.exec():
            self.current["steps"][row] = dialog.value()
            self.render_steps()
            self.save_current()

    def move_step(self, offset):
        row = self.steps.currentRow()
        dest = row + offset
        if row >= 0 and 0 <= dest < len(self.current["steps"]):
            steps = self.current["steps"]
            steps[row], steps[dest] = steps[dest], steps[row]
            self.render_steps()
            self.steps.selectRow(dest)
            self.save_current()

    def remove_step(self):
        row = self.steps.currentRow()
        if row >= 0:
            self.current["steps"].pop(row)
            self.render_steps()
            self.save_current()

    def set_busy(self, busy):
        for item in [self.new_button, self.import_button, self.workflow_list, self.run_button, self.record_button, self.refresh_button, self.save_button, self.export_button, self.tabs, self.name, self.description, self.radar_button, *self.edit_buttons]: item.setEnabled(not busy)
        for item in [self.stop_button, self.radar_stop]: item.setEnabled(busy)

    def start_worker(self, mode):
        if self.process or not self.save_current(): return
        chosen = self.tabs.currentData() or {}
        target_id, page_url = chosen.get("target_id", ""), chosen.get("url", "")
        needs_page = mode in {"record", "inspect"} or (mode == "replay" and any(s.get("action") not in {"demo_export", "validate_xlsx", "wait"} for s in self.current["steps"]))
        if needs_page and not target_id:
            self.notice("Choose Chrome tab", "On the Workflows page click Connect Chrome, then choose the tab to work with. Connection details live under Settings.")
            return
        self.mode = mode
        self.captured = []
        self.artifact = ""
        self.last_result = None
        self.dead_since = None
        self.stop_deadline = None
        self.log.clear()
        self.artifact_label.setText("No output file from this operation yet.")
        if mode == "inspect":
            self.elements = []
            self.element_list.clear()
            self.render_radar(None)
        self.run_id = self.store.start(self.current, {"record": "recording", "inspect": "inspecting"}.get(mode, "running")) if mode != "tabs" else None
        self.run_dir = self.store.root / "runs" / (self.run_id or "connection")
        try:
            context = mp.get_context("spawn")
            self.output = context.Queue()
            self.stop_event = context.Event()
            self.process = context.Process(target=worker_main, args=(mode, self.current, self.store.settings(), str(self.run_dir), page_url, self.output, self.stop_event, target_id), daemon=True)
            self.process.start()
        except Exception as exc:
            if self.run_id: self.store.finish(self.run_id, "failed", detail=str(exc))
            self.process = None
            if hasattr(self, "output"): self.output.close()
            self.notice("Worker could not start", exc)
            return
        self.set_busy(True)
        self.render_steps()
        self.progress.setRange(0, 0)
        self.status.setText({"tabs": "Connecting to Google Chrome…", "record": "Recording · work normally in the selected tab; frames and new tabs are followed",
                             "inspect": "Radar armed · see the Element radar page", "replay": "Running · automation is isolated from this window"}[mode])
        if mode == "inspect":
            self.radar_status.setText("Radar armed · click any element in the selected Chrome tab. The click is captured, not passed to the page.")

    def stop_worker(self):
        if self.process:
            self.stop_event.set()
            self.stop_deadline = time.monotonic() + 3
            self.stop_button.setEnabled(False)
            self.status.setText("Stopping… Captured steps and completed files will be retained.")

    def poll(self):
        if not self.process: return
        for _ in range(100):
            try: event = self.output.get_nowait()
            except queue.Empty: break
            self.handle_event(event)
        alive = self.process.is_alive()
        if alive and self.stop_deadline and time.monotonic() >= self.stop_deadline:
            self.process.terminate()
            self.stop_deadline = None
        if not alive:
            if self.dead_since is None:
                self.dead_since = time.monotonic()
                return
            # Give the multiprocessing queue feeder time to deliver its terminal event.
            if time.monotonic() - self.dead_since < 0.25: return
            if self.last_result is None:
                self.last_result = {"status": "cancelled" if self.stop_event.is_set() else "failed", "message": "Worker stopped. Any completed files remain in the run folder."}
            self.finish_worker()

    def handle_event(self, event):
        kind = event["type"]
        if kind == "tab_updated":
            index = self.tabs.currentIndex()
            if index >= 0 and isinstance(self.tabs.itemData(index), dict):
                # URL is display metadata; the target id keeps ownership across navigation.
                self.tabs.setItemData(index, {**self.tabs.itemData(index), "url": event["url"]})
            return
        if kind == "element":
            if self.run_id: self.store.event(self.run_id, {"type": "element", "index": event["index"]})
            self.add_element(event["fingerprint"])
            return
        if kind == "recorded":
            if len(self.captured) >= 1000:
                self.status.setText("Recording · 1,000 step limit reached. Click Stop to keep what was captured.")
                return
            self.captured.append(event["step"])
            self.status.setText(f"Recording · {len(self.captured)} steps captured")
            self.log.insertPlainText(f"Captured: {event['step']['action']}\n")
            return
        if self.run_id: self.store.event(self.run_id, event)
        if event.get("message"): self.log.insertPlainText(event["message"] + "\n")
        if kind == "step":
            item = self.steps.item(event["index"], 3)
            if item:
                item.setText(event["status"].capitalize())
                item.setForeground(QColor("#167553" if event["status"] == "passed" else "#2365d5"))
            self.progress.setRange(0, max(1, len(self.current["steps"])))
            self.progress.setValue(event["index"] + (event["status"] == "passed"))
        elif kind in {"artifact", "validation"}:
            self.artifact = event["path"]
            self.artifact_label.setText(("Validated Excel · " if kind == "validation" else "Saved file · ") + self.artifact)
        elif kind == "tabs":
            self.tabs.clear()
            self.tabs.addItem("Choose a Chrome tab" if event["tabs"] else "No http(s) tabs found", {})
            for tab in event["tabs"]:
                # Two tabs may show the same address; the target id is what tells them apart.
                self.tabs.addItem(f"{tab['title']} — {tab['location']}"[:90], tab)
        elif kind == "done":
            self.last_result = event

    def finish_worker(self):
        result = self.last_result
        status = result["status"]
        if self.run_id:
            self.store.finish(self.run_id, status, result.get("artifact") or self.artifact, result.get("message", ""))
        self.process.join(timeout=0)
        self.process.close()
        self.process = None
        self.output.close()
        self.set_busy(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if status == "passed" else 0)
        self.status.setText({"passed": "Completed · " + ("Excel validation passed" if result.get("validated") else "all steps finished"), "connected": "Chrome connected · choose your tab", "recorded": "Recording saved · review the new workflow before replay", "cancelled": "Stopped · partial output retained", "failed": "Failed · " + result.get("message", "Check activity below")}.get(status, status))
        if status in {"failed", "cancelled"}:
            for row in range(self.steps.rowCount()):
                if self.steps.item(row, 3).text() == "Running": self.steps.item(row, 3).setText(status.capitalize())
        if self.mode == "record":
            if not self.captured:
                self.status.setText("Recording stopped · nothing was captured, so no workflow was saved.")
            else:
                try:
                    flow = self.store.save({"schema_version": 1, "name": self.current["name"] + " · recording", "description": "Review starting URL, captured values and selectors. Convert the export click to Download.", "steps": self.captured})
                    self.current = None
                    self.reload_workflows(flow["id"])
                except Exception as exc:
                    self.status.setText("Recording could not be saved · " + str(exc))
                    self.notice("Recording not saved", exc)
        self.reload_history()

    def reload_history(self):
        self.run_items = self.store.runs()
        self.history.setRowCount(len(self.run_items))
        for row, run in enumerate(self.run_items):
            for col, key in enumerate(["started", "name", "status", "artifact"]): self.history.setItem(row, col, QTableWidgetItem(run[key] or "—"))

    def show_run(self):
        row = self.history.currentRow()
        if row < 0 or row >= len(self.run_items): return
        run = self.run_items[row]
        self.history_detail.setPlainText(json.dumps({"run": run, "events": self.store.events(run["id"])}, indent=2, ensure_ascii=False))

    def open_run_folder(self):
        row = self.history.currentRow()
        if row < 0: return
        path = self.store.root / "runs" / self.run_items[row]["id"]
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def save_settings(self):
        try:
            self.store.save_settings({"cdp_url": self.endpoint.text().strip(), "chrome_launcher": self.launcher_path.text().strip(), "timeout_seconds": self.timeout.value()})
            QMessageBox.information(self, "Settings", "Saved locally.")
        except Exception as exc: self.notice("Check settings", exc)

    def open_url(self):
        url, ok = QInputDialog.getText(self, "Open in Google Chrome", "http(s) URL")
        if not ok: return
        try:
            http_url(url)
            helper = Path(self.store.settings()["chrome_launcher"])
            if not helper.is_file(): raise ValueError("Approved Chrome launcher not found. Set its path in Settings.")
            process = QProcess(self)
            if getattr(sys, "frozen", False):
                process.setProgram(sys.executable)
                process.setArguments(["--chrome-launcher", str(helper), url])
            else:
                process.setProgram(sys.executable)
                process.setArguments([str(helper), url])
            self.aux.append(process)
            process.finished.connect(lambda code, _status: self.notice("Chrome launcher", "The launcher failed; check your Chrome setup.") if code else None)
            process.start()
        except Exception as exc: self.notice("Could not open Chrome", exc)

    def closeEvent(self, event):
        if self.process:
            self.stop_worker()
            event.ignore()
            self.status.setText("Stopping worker. Close the window again after it finishes.")
            return
        error = self.persist()
        if error and QMessageBox.question(self, "Close without saving?", "Your latest edit could not be saved: " + error + "\n\nClose SmartOps and discard that change?") != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.store.db.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SmartOpsDesktop")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    # One writer per local data directory. The OS releases stale locks after a crash.
    from PySide6.QtCore import QLockFile
    from .core import data_root
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(root / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, "SmartOps", "SmartOps is already running for this data folder.")
        return 0
    window = MainWindow()
    window.show()
    return app.exec()
