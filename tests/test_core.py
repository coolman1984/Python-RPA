import json
import queue
import threading
from pathlib import Path

import pytest
from openpyxl import Workbook

from smartops_desktop.core import Store, validate_workflow, demo_workflow, validate_xlsx, cdp_url
from smartops_desktop.worker import worker_main, replay


def book(path, rows=None):
    wb = Workbook()
    for row in rows or [["Date", "Quantity"], ["2026-09-08", 12]]:
        wb.active.append(row)
    wb.save(path)
    wb.close()
    return path


def test_workflow_roundtrip_and_import_identity(tmp_path):
    store = Store(tmp_path)
    workflow = demo_workflow()
    workflow["name"] = "تقرير الإنتاج"
    store.save(workflow)
    assert store.list_workflows()[0]["name"] == workflow["name"]
    assert json.loads((store.workflows / "welcome-demo.json").read_text(encoding="utf-8")) == workflow
    store.db.close()


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"id": "../../escape"}, {"name": "  "},
    {"steps": [{"action": "shell", "command": "whoami"}]},
    {"steps": [{"action": "navigate", "url": "file:///C:/private"}]},
    {"steps": [{"action": "navigate", "url": "https://user:secret@example.org"}]},
    {"steps": [{"action": "fill", "selector": "#password", "value": "secret"}]},
    {"steps": [{"action": "wait", "seconds": 999}]},
    {"steps": [{"action": "check", "selector": "#x", "checked": "false"}]},
    {"steps": [{"action": "validate_xlsx", "min_rows": -1}]},
])
def test_invalid_workflows_fail_before_replay(change):
    with pytest.raises(ValueError): validate_workflow(demo_workflow() | change)


def test_validates_actual_bytes_not_extension(tmp_path):
    path = book(tmp_path / "report.bin")
    assert validate_xlsx(path, 1, ["Quantity"])["rows"] == 1


def test_html_disguised_as_excel_is_rejected(tmp_path):
    path = tmp_path / "report.xlsx"
    path.write_text("<html>Session expired</html>")
    with pytest.raises(ValueError, match="not an XLSX"): validate_xlsx(path)


def test_missing_columns_and_empty_data_fail(tmp_path):
    path = book(tmp_path / "report.xlsx", [["Date", "Quantity"]])
    with pytest.raises(ValueError, match="data rows"): validate_xlsx(path)
    with pytest.raises(ValueError, match="Missing columns"): validate_xlsx(path, 0, ["Line"])


def test_settings_reject_remote_cdp(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="loopback"):
        store.save_settings(store.settings() | {"cdp_url": "http://192.168.1.5:9222"})
    store.save_settings(store.settings() | {"timeout_seconds": 45})
    assert store.settings()["timeout_seconds"] == 45
    store.db.close()


def test_interrupted_run_recovered_on_restart(tmp_path):
    store = Store(tmp_path)
    run_id = store.start(demo_workflow())
    store.db.close()
    reopened = Store(tmp_path)
    assert reopened.runs()[0]["id"] == run_id
    assert reopened.runs()[0]["status"] == "interrupted"
    reopened.db.close()


def events(out):
    result = []
    while not out.empty(): result.append(out.get_nowait())
    return result


def test_complete_demo_records_validated_artifact(tmp_path):
    out = queue.Queue()
    worker_main("replay", demo_workflow(), {}, tmp_path, "", out, threading.Event())
    emitted = events(out)
    assert emitted[-1]["status"] == "passed"
    assert emitted[-1]["validated"] is True
    assert Path(emitted[-1]["artifact"]).is_file()
    assert len([e for e in emitted if e["type"] == "step" and e["status"] == "passed"]) == 2


def test_cancel_never_claims_success(tmp_path):
    out = queue.Queue()
    stop = threading.Event()
    stop.set()
    worker_main("replay", demo_workflow(), {}, tmp_path, "", out, stop)
    assert events(out)[-1]["status"] == "cancelled"
    assert not list(tmp_path.glob("*.xlsx"))


def test_empty_workflow_not_success(tmp_path):
    out = queue.Queue()
    worker_main("replay", demo_workflow() | {"steps": []}, {}, tmp_path, "", out, threading.Event())
    assert events(out)[-1]["status"] == "failed"


def test_failed_validation_is_terminal(tmp_path):
    out = queue.Queue()
    flow = demo_workflow()
    flow["steps"][1]["required_columns"] = ["Missing"]
    worker_main("replay", flow, {}, tmp_path, "", out, threading.Event())
    emitted = events(out)
    assert emitted[-1]["status"] == "failed"
    assert "Missing columns" in emitted[-1]["message"]
    assert any(e["type"] == "artifact" for e in emitted)


class Target:
    def __init__(self): self.calls = []
    def click(self): self.calls.append("click")
    def fill(self, value): self.calls.append(("fill", value))
    def get_attribute(self, key): return "text"
    def select_option(self, value): self.calls.append(("select", value))
    def set_checked(self, value): self.calls.append(("check", value))
    def press(self, value): self.calls.append(("press", value))


class Page:
    def __init__(self): self.target = Target(); self.visited = []
    def set_default_timeout(self, value): pass
    def set_default_navigation_timeout(self, value): pass
    def locator(self, value): return self.target
    def goto(self, value, **kwargs): self.visited.append(value)


def test_replay_dispatch_preserves_action_order_and_values(tmp_path):
    page = Page()
    steps = [{"action": "navigate", "url": "https://example.org"}, {"action": "click", "selector": "#a"}, {"action": "fill", "selector": "#b", "value": "2026-09-08"}, {"action": "check", "selector": "#c", "checked": False}, {"action": "press", "selector": "#b", "value": "Enter"}]
    result = replay(demo_workflow() | {"steps": steps}, {}, tmp_path, queue.Queue(), threading.Event(), page)
    assert page.visited == ["https://example.org"]
    assert page.target.calls == ["click", ("fill", "2026-09-08"), ("check", False), ("press", "Enter")]
    assert not result["validated"]


def test_download_uses_completed_bytes_and_ignores_filename(tmp_path):
    from contextlib import contextmanager
    class Download:
        suggested_filename = "../../untrusted.exe"
        def save_as(self, target): book(Path(target))
    class DownloadPage(Page):
        @contextmanager
        def expect_download(self, **kwargs):
            class Pending: value = Download()
            yield Pending()
    out = queue.Queue()
    flow = demo_workflow() | {"steps": [{"action": "download", "selector": "#export", "required_columns": ["Quantity"]}]}
    result = replay(flow, {}, tmp_path, out, threading.Event(), DownloadPage())
    assert result["validated"]
    assert Path(result["artifact"]) == tmp_path / "download-1.xlsx"
    assert not (tmp_path / "download-1.bin").exists()
    assert validate_xlsx(result["artifact"])["rows"] == 1


def test_invalid_download_bytes_retained_without_success(tmp_path):
    from contextlib import contextmanager
    class Download:
        def save_as(self, target): Path(target).write_text("<html>Sign in</html>")
    class DownloadPage(Page):
        @contextmanager
        def expect_download(self, **kwargs):
            class Pending: value = Download()
            yield Pending()
    out = queue.Queue()
    flow = demo_workflow() | {"steps": [{"action": "download", "selector": "#export"}]}
    with pytest.raises(ValueError, match="not an XLSX"):
        replay(flow, {}, tmp_path, out, threading.Event(), DownloadPage())
    assert (tmp_path / "download-1.bin").exists()
    assert not list(tmp_path.glob("*.xlsx"))
    assert not any(e["type"] == "validation" for e in events(out))
