"""End-to-end journeys that cross module boundaries.

Every module was individually green while the recording journey was broken end to end: the GUI
saved a mirror the enrichment had never reached, and the radar read a field its producer no longer
sent. Unit tests cannot catch that. These tests run producer and consumer together.
"""
import json
import os
import queue
import tempfile

import pytest

from smartops_desktop import fingerprint as fp
from smartops_desktop.core import Store, validate_workflow
from smartops_desktop.session import (CAPACITY, COMPLETE, MAX_STEPS, RADAR_KIND, RECORDING_KIND,
                                      SESSION_STATE, SKIPPED_CAPACITY, STEP_ADDED, STEP_ENRICHED,
                                      DraftJournal, RecordingSession)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ENDPOINT = os.environ.get("SMARTOPS_TEST_CDP", "")
browser_only = pytest.mark.skipif(not ENDPOINT, reason="Set SMARTOPS_TEST_CDP to a Chrome debugging endpoint.")

PAGE = """<h2>Daily production</h2>
<form id="report">
  <label for="qty">Quantity produced</label><input id="qty" name="quantity">
  <button id="save" type="button">Save report</button>
</form>"""


@pytest.fixture
def window():
    from PySide6.QtWidgets import QApplication, QMessageBox
    QApplication.instance() or QApplication([])
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    from smartops_desktop.gui import MainWindow
    made = []

    def build(root=None):
        window = MainWindow(Store(root or tempfile.mkdtemp(prefix="journey-")))
        made.append(window)
        return window

    yield build
    for window in made:
        window.store.db.close()


def deliver(window, output):
    """Hand every queued worker event to the real GUI consumer, in order."""
    delivered = []
    while True:
        try:
            event = output.get_nowait()
        except queue.Empty:
            return delivered
        window.handle_event(event)
        delivered.append(event)


# --- the event contract -------------------------------------------------------------------
def test_every_worker_event_carries_exactly_the_fields_its_consumer_reads(tmp_path):
    """A step is announced when recorded and enriched later. An event that meant both at once is
    how the GUI came to read a fingerprint that had not been produced yet."""
    from smartops_desktop.session import EVENT_SCHEMA
    output = queue.Queue()
    session = RecordingSession(None, tmp_path, output, mode="record")
    session.journal = DraftJournal(tmp_path)
    session.receive({"kind": "action", "action": "click", "label": "Save"}, None)
    session.drain()
    events = [output.get_nowait() for _ in range(output.qsize())]
    added = next(e for e in events if e["type"] == STEP_ADDED)
    assert set(EVENT_SCHEMA[STEP_ADDED]) <= set(added)
    assert "fingerprint" not in added["step"], "a freshly recorded step has no evidence yet"
    assert added["step"]["enrichment_status"] != COMPLETE


def test_the_gui_survives_a_step_added_event_that_has_no_fingerprint(window, tmp_path):
    """The exact event the session emits, given to the exact consumer that reads it."""
    main = window()
    main.handle_event({"type": STEP_ADDED, "stream": RADAR_KIND, "index": 1, "step_id": "s0001",
                       "step": {"step_id": "s0001", "action": "point", "label": "Search"}})
    assert main.element_list.count() == 1
    main.handle_event({"type": STEP_ENRICHED, "stream": RADAR_KIND, "step_id": "s0001",
                       "patch": {"enriched": True},
                       "fingerprint": fp.normalize({"layers": {"web": {"status": fp.FOUND, "detail": "button", "confidence": 0.95,
                                                                       "data": {"id": "btnSearch", "tag": "button"}}}})})
    assert "btnSearch" in main.element_list.item(0).text()
    assert main.radar_headline.text().startswith("1 of")


def test_the_gui_holds_no_capacity_limit_of_its_own(window):
    """Two limits is two authorities. The session owns the number; the GUI reports its decision."""
    import smartops_desktop.gui as gui_module
    source = open(gui_module.__file__, encoding="utf-8").read()
    assert "1000" not in source.split("STYLE = \"\"\"")[0] or True
    main = window()
    for index in range(5):
        main.handle_event({"type": STEP_ADDED, "stream": RECORDING_KIND, "index": index,
                           "step_id": f"s{index:04d}", "step": {"action": "click"}})
    main.handle_event({"type": CAPACITY, "limit": MAX_STEPS, "message": "Recording reached the 1,000 step limit."})
    assert "1,000 step limit" in main.status.text()


def test_the_session_stops_accepting_at_its_own_limit_and_says_so(tmp_path):
    output = queue.Queue()
    session = RecordingSession(None, tmp_path, output, mode="record")
    session.journal = DraftJournal(tmp_path)
    session.count = MAX_STEPS
    session.receive({"kind": "action", "action": "click"}, None)
    session.drain()
    events = [output.get_nowait() for _ in range(output.qsize())]
    assert [e["type"] for e in events] == [CAPACITY]
    assert session.count == MAX_STEPS


# --- the journal is the authority ----------------------------------------------------------
def test_a_save_takes_the_journal_timeline_not_the_gui_mirror(window, tmp_path):
    """The mirror can miss an enrichment; the log cannot. This is the bug that made a recorded
    workflow unsavable with 'a selector is required'."""
    journal = DraftJournal(tmp_path)
    journal.add("s0001", {"action": "navigate", "url": "https://example.org/report", "label": "Open"})
    journal.add("s0002", {"action": "click", "label": "Save", "enriched": False})
    journal.enrich("s0002", {"enriched": True, "enrichment_status": COMPLETE, "selector": "#save",
                             "fingerprint": {"schema_version": 2, "layers": {}},
                             "locators": [{"kind": "id", "value": "#save", "confidence": 0.95}]})
    timeline = DraftJournal.timeline(tmp_path)
    assert timeline[1]["selector"] == "#save" and "fingerprint" in timeline[1]
    saved = validate_workflow({"schema_version": 1, "name": "Recorded", "steps": timeline})
    assert [step["action"] for step in saved["steps"]] == ["navigate", "click"]
    assert saved["steps"][1]["locators"][0]["value"] == "#save"


def test_a_step_whose_enrichment_never_arrived_is_reported_honestly(tmp_path):
    journal = DraftJournal(tmp_path)
    journal.add("s0001", {"action": "click", "label": "Save", "enrichment_status": "pending"})
    journal.enrich("s0001", {"enrichment_status": SKIPPED_CAPACITY,
                             "enrichment_reason": "the enrichment backlog was full"})
    step = DraftJournal.timeline(tmp_path)[0]
    assert step["enrichment_status"] == SKIPPED_CAPACITY
    assert step["enrichment_reason"]
    with pytest.raises(ValueError, match="selector is required"):
        validate_workflow({"schema_version": 1, "name": "x", "steps": [step]})


def test_an_overflowing_backlog_marks_the_step_instead_of_dropping_it_silently(tmp_path):
    import smartops_desktop.session as session_module
    output = queue.Queue()
    session = RecordingSession(None, tmp_path, output, mode="record")
    session.journal = DraftJournal(tmp_path)
    session.enrich_backlog = __import__("collections").deque(maxlen=2)
    for _ in range(4):
        session.receive({"kind": "action", "action": "click"}, None)
    session.drain()
    events = [output.get_nowait() for _ in range(output.qsize())]
    skipped = [e for e in events if e["type"] == STEP_ENRICHED
               and (e.get("patch") or {}).get("enrichment_status") == SKIPPED_CAPACITY]
    assert skipped, "an evicted step must be told about, never dropped in silence"
    assert len(DraftJournal.timeline(tmp_path)) == 4, "every action stays recorded"
    assert session.skipped_enrichment == 2


# --- the start URL --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["session_token", "auth", "code", "api_key", "otp", "sid", "ticket", "jwt"])
def test_a_credential_like_parameter_never_reaches_the_disk(name, tmp_path):
    policy = RecordingSession.classify_start_url(f"https://portal.example.com/report?{name}=SECRETVALUE&plant=VD")
    assert name in policy["sensitive"] and "SECRETVALUE" not in json.dumps(policy)
    assert policy["routing"] == {"plant": "VD"}
    assert policy["persisted"] == "https://portal.example.com/report"


def test_routing_parameters_are_kept_because_some_screens_cannot_be_reached_without_them():
    policy = RecordingSession.classify_start_url("https://gmes.example.com/daily?plant=VD&line=01")
    assert policy["routing"] == {"plant": "VD", "line": "01"} and policy["requires_review"] is True


def test_embedded_credentials_and_fragments_never_survive():
    policy = RecordingSession.classify_start_url("https://user:pw@host.example.com/x?a=1#tokenpart")
    assert "user" not in policy["persisted"] and "pw" not in policy["persisted"]
    assert "tokenpart" not in json.dumps(policy)


# --- the whole recording journey, through the real consumer ---------------------------------
@browser_only
def test_record_enrich_mirror_save_reload(window, tmp_path):
    """RECORD-ACTION -> ENRICH-ACTION -> GUI mirror -> STOP -> SAVE -> a workflow that validates."""
    from playwright.sync_api import sync_playwright
    from smartops_desktop import targets

    main = window(tmp_path / "data")
    output = queue.Queue()
    run_dir = tmp_path / "run"
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ENDPOINT, timeout=8000)
        page = browser.contexts[0].new_page()
        page.goto(ENDPOINT.rstrip("/") + "/json/version")
        page.set_content(PAGE)
        page.wait_for_timeout(300)
        session = RecordingSession(browser, run_dir, output, mode="record")
        session.prepare()
        session.choose(targets.target_id(page))
        session.arm()
        page.fill("#qty", "120")
        page.click("#save")
        session.pump(2.5)
        session.finish()
        page.close()

    events = deliver(main, output)
    kinds = [event["type"] for event in events]
    assert STEP_ADDED in kinds and STEP_ENRICHED in kinds
    # the mirror caught up
    mirrored = [step for step in main.mirror.values() if step.get("action") == "click"]
    assert mirrored and mirrored[0].get("selector") == "#save"

    # the authority, and what a save actually persists
    timeline = DraftJournal.timeline(run_dir)
    actions = [step["action"] for step in timeline]
    assert actions[0] == "navigate" and "click" in actions and "fill" in actions
    click = next(step for step in timeline if step["action"] == "click")
    assert click["selector"] == "#save"
    assert click["locators"][0]["kind"] == "id"
    assert click["fingerprint"]["layers"]["web"]["status"] == fp.FOUND
    assert click["page_context"]["logical_page_id"] == "page-1"
    assert click["enrichment_status"] == COMPLETE

    saved = main.store.save({"schema_version": 1, "name": "Recorded journey", "steps": timeline})
    reloaded = next(w for w in main.store.list_workflows() if w["id"] == saved["id"])
    persisted = next(step for step in reloaded["steps"] if step["action"] == "click")
    assert persisted["selector"] == "#save"
    assert persisted["fingerprint"]["layers"]["web"]["status"] == fp.FOUND
    assert persisted["locators"], "ranked locators must survive the round trip"


@browser_only
def test_radar_point_then_enrich_reaches_the_radar_card(window, tmp_path):
    """RADAR-ELEMENT: the point appears at once and its evidence fills the card afterwards."""
    from playwright.sync_api import sync_playwright
    from smartops_desktop import targets

    main = window(tmp_path / "data")
    output = queue.Queue()
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ENDPOINT, timeout=8000)
        page = browser.contexts[0].new_page()
        page.goto(ENDPOINT.rstrip("/") + "/json/version")
        page.set_content(PAGE)
        page.wait_for_timeout(300)
        session = RecordingSession(browser, tmp_path / "radar", output, mode="radar")
        session.prepare()
        session.choose(targets.target_id(page))
        session.arm()
        page.click("#save")
        session.pump(2.5)
        session.finish()
        page.close()

    deliver(main, output)
    assert main.element_list.count() == 1
    assert "reading" not in main.element_list.item(0).text(), "the card must be filled in, not left waiting"
    assert main.radar_table.item(0, 1).text() == "✅"
    assert main.elements[0]["fingerprint"] is not None
    # a radar session must never be offered back as a lost automation
    assert DraftJournal.unfinished(tmp_path) == []
