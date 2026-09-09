"""The recording session: frames, popups, drafts, and the bugs that hid inside the old handoff."""
import json
import os
import queue

import pytest

from smartops_desktop import fingerprint as fp
from smartops_desktop import targets
from smartops_desktop.discovery import DiscoveryManager
from smartops_desktop.session import (ARMING, IDLE, PAUSED, PREPARING, RECORDING, REVIEW, SAVED,
                                      STOPPING, TARGET_PICK, DraftJournal, RecordingSession, SessionError)

ENDPOINT = os.environ.get("SMARTOPS_TEST_CDP", "")
browser_only = pytest.mark.skipif(not ENDPOINT, reason="Set SMARTOPS_TEST_CDP to a Chrome debugging endpoint.")

FORM = """<h2>Daily production</h2>
<form id="report">
  <label for="qty">Quantity produced</label><input id="qty" name="quantity">
  <label for="line">Line</label><select id="line" name="line"><option>VD-01</option><option>VD-02</option></select>
  <label for="ok">Confirmed</label><input id="ok" type="checkbox">
  <input id="secret" type="password" name="password">
  <button id="save" type="button">Save report</button>
</form>"""

IFRAME = """<h2>Outer page</h2>
<div style="height:120px;background:#fff"></div>
<iframe id="reportFrame" style="width:420px;height:220px;border:0" srcdoc="
  &lt;body style='margin:0;background:#ffffff'&gt;
  &lt;div style='height:70px'&gt;&lt;/div&gt;
  &lt;button id='inner' style='width:180px;height:44px;background:#ff0000;border:0;color:#ff0000'&gt;X&lt;/button&gt;
  &lt;/body&gt;"></iframe>"""

NESTED = """<iframe id="outerFrame" style="width:500px;height:300px" srcdoc="
  &lt;iframe id='innerFrame' style='width:400px;height:200px' srcdoc=&quot;&lt;button id='deep'&gt;Deep button&lt;/button&gt;&quot;&gt;&lt;/iframe&gt;"></iframe>"""

NESTED_COLOURED = """<div style="height:80px"></div>
<iframe id="outerFrame" style="width:500px;height:300px;border:0" srcdoc="
  &lt;body style='margin:0'&gt;&lt;div style='height:60px'&gt;&lt;/div&gt;
  &lt;iframe id='innerFrame' style='width:400px;height:200px;border:0' srcdoc=&quot;&lt;body style='margin:0'&gt;&lt;div style='height:40px'&gt;&lt;/div&gt;&lt;button id='deep' style='width:160px;height:44px;background:#ff0000;border:0;color:#ff0000'&gt;X&lt;/button&gt;&lt;/body&gt;&quot;&gt;&lt;/iframe&gt;&lt;/body&gt;"></iframe>"""

POPUP = """<a id="opener" href="about:blank" target="_blank">Open a second tab</a>"""


def timeline(session, seconds=2.0):
    """The materialised, enriched steps.

    Raw capture is instant and lands in the journal immediately; discovery patches it afterwards,
    so evidence is read from the journal's timeline rather than from the raw return of pump().
    """
    session.pump(seconds)
    return [step for step in session.journal.steps() if step.get("origin") != "start_context"]


# --- state machine, no browser needed -----------------------------------------------------
def test_the_state_machine_refuses_a_transition_it_does_not_define(tmp_path):
    session = RecordingSession(None, tmp_path, queue.Queue())
    assert session.state == IDLE
    with pytest.raises(SessionError, match="Cannot go from idle to recording"):
        session.move(RECORDING)


def test_every_state_can_reach_a_safe_end(tmp_path):
    session = RecordingSession(None, tmp_path, queue.Queue())
    session.journal = DraftJournal(tmp_path)
    for state in (PREPARING, TARGET_PICK, ARMING, RECORDING, PAUSED, RECORDING, STOPPING, REVIEW, SAVED):
        session.move(state)
    assert session.state == SAVED


# --- draft journal, no browser needed -----------------------------------------------------
def test_a_step_is_on_disk_the_moment_it_is_accepted(tmp_path):
    journal = DraftJournal(tmp_path, target={"title": "Report"})
    journal.add("s0001", {"action": "click", "label": "Save"})
    assert json.loads((tmp_path / "draft.json").read_text())["steps"] == 1
    assert len(journal.steps()) == 1


def test_evidence_arrives_later_as_a_separate_operation_and_never_rewrites_the_step(tmp_path):
    """The user's action must be durable before any slow detector runs."""
    journal = DraftJournal(tmp_path)
    journal.add("s0001", {"action": "click", "label": "Save", "enriched": False})
    raw_only = journal.steps()[0]
    assert raw_only["enriched"] is False and "fingerprint" not in raw_only
    journal.enrich("s0001", {"enriched": True, "fingerprint": {"schema_version": 2}, "selector": "#save"})
    enriched = journal.steps()[0]
    assert enriched["enriched"] is True and enriched["selector"] == "#save"
    assert enriched["label"] == "Save", "enrichment adds; it does not overwrite what was recorded"
    operations = [entry["op"] for entry in journal.operations()]
    assert operations == ["add", "enrich"], "append-only: nothing is rewritten in place"


def test_a_crash_before_enrichment_still_keeps_the_users_action(tmp_path):
    journal = DraftJournal(tmp_path)
    journal.add("s0001", {"action": "fill", "label": "Quantity", "value": "120", "enriched": False})
    reopened = DraftJournal(tmp_path, session_id=journal.session_id)
    reopened.log_path = journal.log_path
    recovered = reopened.steps()
    assert len(recovered) == 1 and recovered[0]["value"] == "120"


def test_undo_and_edit_are_operations_that_survive_a_crash(tmp_path):
    journal = DraftJournal(tmp_path)
    journal.add("s0001", {"action": "click", "label": "One"})
    journal.add("s0002", {"action": "click", "label": "Two"})
    journal.undo("s0002")
    journal.edit("s0001", {"label": "Renamed"})
    assert [step["label"] for step in journal.steps()] == ["Renamed"]


def test_a_draft_truncated_by_a_crash_keeps_everything_before_the_break(tmp_path):
    journal = DraftJournal(tmp_path)
    journal.add("s0001", {"action": "click", "label": "One"})
    journal.add("s0002", {"action": "fill", "label": "Two"})
    with (tmp_path / "draft.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"op": "add", "step_id": "s0003", "ste')  # power cut mid-write
    assert [step["label"] for step in journal.steps()] == ["One", "Two"]


def test_an_unfinished_recording_is_offered_back_and_a_saved_one_is_not(tmp_path):
    crashed = DraftJournal(tmp_path / "run-a")
    crashed.note(state=RECORDING)
    finished = DraftJournal(tmp_path / "run-b")
    finished.note(state=SAVED)
    offered = DraftJournal.unfinished(tmp_path)
    assert [item["session_id"] for item in offered] == [crashed.session_id]
    assert offered[0]["folder"].endswith("run-a")


def test_a_radar_session_is_never_offered_back_as_a_lost_automation(tmp_path):
    """Radar fingerprints are diagnostics. Offering them as unfinished recordings would invite the
    user to save a workflow they never recorded."""
    from smartops_desktop.session import RADAR_KIND
    radar = DraftJournal(tmp_path / "radar-run", kind=RADAR_KIND)
    radar.note(state=RECORDING)
    recording = DraftJournal(tmp_path / "record-run")
    recording.note(state=RECORDING)
    offered = DraftJournal.unfinished(tmp_path)
    assert [item["session_id"] for item in offered] == [recording.session_id]


# --- the browser paths --------------------------------------------------------------------
@pytest.fixture
def recorder(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ENDPOINT, timeout=8000)
        page = browser.contexts[0].new_page()
        page.goto(ENDPOINT.rstrip("/") + "/json/version")
        page.set_viewport_size({"width": 900, "height": 700})
        made = []

        def start(html, mode="record", manager=None):
            page.set_content(html)
            page.wait_for_timeout(300)
            session = RecordingSession(browser, tmp_path, queue.Queue(), manager=manager, mode=mode)
            session.prepare()
            session.choose(targets.target_id(page))
            session.arm()
            made.append(session)
            return session, page

        yield start
        for session in made:
            try:
                session.finish()
            except Exception:
                pass
        if not page.is_closed():
            page.close()


@browser_only
def test_recording_uses_the_same_discovery_manager_as_the_radar(recorder):
    manager = DiscoveryManager()
    session, page = recorder(FORM, manager=manager)
    assert session.manager is manager
    page.click("#save")
    steps = timeline(session)
    assert len(steps) == 1
    # A recorded step carries BOTH the replayable action and the full fingerprint.
    assert steps[0]["action"] == "click" and steps[0]["selector"]
    assert set(steps[0]["fingerprint"]["layers"]) == set(fp.LAYER_KEYS)
    assert steps[0]["best_layer"] == "web"


@browser_only
@pytest.mark.parametrize("do,expect", [
    (lambda page: page.click("#save"), "click"),
    (lambda page: page.fill("#qty", "September report"), "fill"),
    # select_option() dispatches an untrusted change event, which the probe rejects on purpose.
    # A real user changes a select with the keyboard, which is a genuine trusted event.
    (lambda page: (page.click("#line"), page.keyboard.press("ArrowDown"), page.keyboard.press("Enter")), "select"),
    (lambda page: page.check("#ok"), "check"),
    (lambda page: (page.click("#qty"), page.keyboard.press("Enter")), "press"),
])
def test_each_kind_of_control_is_recorded(recorder, do, expect):
    session, page = recorder(FORM)
    do(page)
    page.click("h2")  # blur, so a change event fires
    page.wait_for_timeout(300)
    assert expect in [step["action"] for step in session.drain()]


@browser_only
def test_typing_a_whole_value_is_one_step_not_one_per_keystroke(recorder):
    session, page = recorder(FORM)
    page.click("#qty")
    page.keyboard.type("September report")
    page.click("h2")
    page.wait_for_timeout(300)
    fills = [step for step in session.drain() if step["action"] == "fill"]
    assert len(fills) == 1 and fills[0]["value"] == "September report"


@browser_only
def test_a_password_field_records_its_identity_and_never_its_value(recorder):
    session, page = recorder(FORM)
    page.fill("#secret", "hunter2")
    page.click("h2")
    page.wait_for_timeout(300)
    steps = session.pump(1.2)
    secure = [step for step in steps if step.get("secure")]
    assert secure, "the control must still be recorded, just without its value"
    assert secure[0]["label"] == "Manual secure input required"
    assert "value" not in secure[0]
    assert "hunter2" not in json.dumps(steps)


@browser_only
def test_events_inside_an_iframe_are_recorded_not_discarded(recorder):
    session, page = recorder(IFRAME)
    page.frame_locator("#reportFrame").locator("#inner").click()
    steps = timeline(session)
    assert len(steps) == 1
    frame_layer = steps[0]["fingerprint"]["layers"]["frame"]
    assert frame_layer["data"]["top_level"] is False
    assert steps[0].get("frame")


@browser_only
def test_events_inside_a_nested_iframe_are_recorded_with_their_depth(recorder):
    session, page = recorder(NESTED)
    page.frame_locator("#outerFrame").frame_locator("#innerFrame").locator("#deep").click()
    steps = timeline(session)
    assert len(steps) == 1
    assert steps[0]["fingerprint"]["layers"]["frame"]["data"]["depth"] == 2


@browser_only
def test_the_element_picture_really_shows_the_element_inside_an_iframe(recorder):
    """The old code clipped the top-level page by frame-relative coordinates, photographing the
    wrong area. The button is pure red, so the pixels prove which element was captured."""
    session, page = recorder(IFRAME)
    page.frame_locator("#reportFrame").locator("#inner").click()
    steps = timeline(session)
    picture = steps[0]["fingerprint"]["layers"]["visual"]["data"]["element_png"]
    with open(picture, "rb") as handle:
        raw = handle.read()
    from PIL import Image  # noqa
    import io
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    total = image.size[0] * image.size[1]
    red = sum(count for count, (r, g, b) in (image.getcolors(maxcolors=total) or []) if r > 200 and g < 60 and b < 60)
    assert image.size[0] > 100, "the picture should be the button, not a stray sliver"
    assert red / total > 0.9, f"expected the red button, got {red}/{total} red pixels"


@browser_only
def test_no_probe_marker_is_left_behind_in_any_frame(recorder):
    session, page = recorder(IFRAME)
    page.frame_locator("#reportFrame").locator("#inner").click()
    page.wait_for_timeout(300)
    session.pump(1.5)
    for frame in page.frames:
        left = frame.evaluate("document.querySelectorAll('[data-smartops-probe]').length")
        assert left == 0, f"a marker was left in {frame.url or 'a frame'}"


@browser_only
def test_a_new_tab_opened_from_the_target_joins_the_same_session(recorder):
    session, page = recorder(POPUP)
    with page.expect_popup() as pending:
        page.click("#opener")
    popup = pending.value
    popup.set_content("<button id='inPopup'>Continue</button>")
    session.pump(1.2)   # the session adopts the new tab by itself; nothing is installed by hand
    assert len(session.pages) == 2 and any(p is popup for p in session.pages)
    popup.click("#inPopup")
    steps = session.pump(1.0)
    assert [step["action"] for step in steps] == ["click"]
    popup.close()


@browser_only
def test_ownership_survives_navigation_during_recording(recorder):
    session, page = recorder(FORM)
    identifier = session.target["target_id"]
    page.goto(ENDPOINT.rstrip("/") + "/json/list")
    assert session.reacquire() is page
    assert targets.target_id(page) == identifier


@browser_only
def test_pause_stops_capturing_and_resume_starts_again(recorder):
    session, page = recorder(FORM)
    session.pause()
    page.click("#save")
    page.wait_for_timeout(250)
    assert session.pump(1.0) == []
    session.resume()
    page.click("#save")
    page.wait_for_timeout(250)
    assert len(session.drain()) == 1


@browser_only
def test_one_failing_detector_never_stops_a_recording(recorder):
    class Sabotage:
        key = "web"

        def observe(self, context):
            raise RuntimeError("this detector is broken")

    from smartops_desktop.discovery import DEFAULT_DETECTORS
    manager = DiscoveryManager([Sabotage()] + [d for d in DEFAULT_DETECTORS if d.key != "web"])
    session, page = recorder(FORM, manager=manager)
    page.click("#save")
    steps = timeline(session)
    assert len(steps) == 1, "the step must still be recorded"
    assert steps[0]["fingerprint"]["layers"]["web"]["status"] == fp.FAILED
    assert steps[0]["fingerprint"]["layers"]["anchor"]["status"] == fp.FOUND


@browser_only
def test_ten_consecutive_recordings_of_the_same_action_agree(recorder):
    session, page = recorder(FORM)
    seen = []
    for _ in range(10):
        page.click("#save")
        steps = timeline(session, 1.2)
        assert len(steps) == len(seen) + 1
        latest = steps[-1]
        web = latest["fingerprint"]["layers"]["web"]
        seen.append((latest["action"], latest["selector"], web["confidence"], latest["best_layer"]))
    assert len(set(seen)) == 1, f"recordings drifted: {set(seen)}"


# --- P0-1: a recorded credential step must survive being saved -----------------------------
def test_a_recorded_secure_step_saves_reloads_and_never_carries_the_secret(tmp_path):
    """Capturing a password step is worthless if the workflow it belongs to cannot be saved."""
    from smartops_desktop.core import Store
    store = Store(tmp_path / "data")
    recorded = [
        {"action": "navigate", "url": "https://example.org/portal", "label": "Open starting page"},
        {"action": "secure_input", "selector": "#pw", "label": "Manual secure input required",
         "secure": True, "fingerprint": {"schema_version": 2, "layers": {}}},
        {"action": "click", "selector": "#save", "label": "Save report"},
    ]
    saved = store.save({"schema_version": 1, "name": "Portal sign-in", "steps": recorded})
    reloaded = next(w for w in store.list_workflows() if w["id"] == saved["id"])
    assert [step["action"] for step in reloaded["steps"]] == ["navigate", "secure_input", "click"]
    secure = reloaded["steps"][1]
    assert secure["secure"] is True and "value" not in secure
    assert secure["fingerprint"] == {"schema_version": 2, "layers": {}}, "the fingerprint must survive the round trip"
    assert "hunter2" not in json.dumps(reloaded)
    store.db.close()


def test_replay_refuses_a_secure_step_instead_of_reporting_success(tmp_path):
    from smartops_desktop.worker import worker_main
    output = queue.Queue()
    import threading as _threading
    workflow = {"schema_version": 1, "id": "secure-demo", "name": "Sign in", "steps": [
        {"action": "demo_export", "label": "Create a report"},
        {"action": "secure_input", "selector": "#pw", "label": "Manual secure input required"},
    ]}
    worker_main("replay", workflow, {}, tmp_path, "", output, _threading.Event())
    events = [output.get_nowait() for _ in range(output.qsize())]
    assert events[-1]["status"] == "failed"
    assert "typed by hand" in events[-1]["message"]


def test_a_secure_step_can_never_be_given_a_value(tmp_path):
    from smartops_desktop.core import validate_workflow
    with pytest.raises(ValueError, match="never carry a value"):
        validate_workflow({"schema_version": 1, "name": "x",
                           "steps": [{"action": "secure_input", "selector": "#pw", "value": "hunter2"}]})


# --- P0-3 / P0-4: what a crash leaves behind ----------------------------------------------
def test_a_crash_during_review_is_still_recoverable(tmp_path):
    session = RecordingSession(None, tmp_path / "run", queue.Queue())
    session.journal = DraftJournal(tmp_path / "run")
    for state in (PREPARING, TARGET_PICK, ARMING, RECORDING, STOPPING, REVIEW):
        session.move(state)
    offered = DraftJournal.unfinished(tmp_path)
    assert [item["state"] for item in offered] == [REVIEW], "review is not saved, so it must come back"


def test_a_draft_stops_being_offered_only_after_the_workflow_is_really_saved(tmp_path):
    session = RecordingSession(None, tmp_path / "run", queue.Queue())
    session.journal = DraftJournal(tmp_path / "run")
    for state in (PREPARING, TARGET_PICK, ARMING, RECORDING, STOPPING, REVIEW):
        session.move(state)
    assert DraftJournal.unfinished(tmp_path), "still open while the user is reviewing"
    session.finalize(SAVED)
    assert DraftJournal.unfinished(tmp_path) == []


def test_a_failed_save_leaves_the_draft_recoverable(tmp_path):
    session = RecordingSession(None, tmp_path / "run", queue.Queue())
    session.journal = DraftJournal(tmp_path / "run")
    for state in (PREPARING, TARGET_PICK, ARMING, RECORDING, STOPPING, REVIEW):
        session.move(state)
    try:
        raise RuntimeError("disk full")  # the workflow never reached storage
    except RuntimeError:
        pass
    assert DraftJournal.unfinished(tmp_path), "a draft may only close after a successful save"


def test_an_explicitly_discarded_draft_is_not_offered_back(tmp_path):
    session = RecordingSession(None, tmp_path / "run", queue.Queue())
    session.journal = DraftJournal(tmp_path / "run")
    for state in (PREPARING, TARGET_PICK, ARMING, RECORDING, STOPPING, REVIEW):
        session.move(state)
    session.discard()
    assert DraftJournal.unfinished(tmp_path) == []


@browser_only
def test_the_recovered_draft_knows_which_page_the_automation_starts_on(recorder, tmp_path):
    session, page = recorder(FORM)
    page.click("#save")
    page.wait_for_timeout(250)
    session.drain()
    recovered = session.journal.steps()
    assert recovered[0]["action"] == "navigate", "the starting page must be step one of the draft"
    assert recovered[0]["origin"] == "start_context"
    assert recovered[0]["url"].startswith("http")
    assert [step["action"] for step in recovered] == ["navigate", "click"]


# --- P0-2: a frame that appears after recording started ------------------------------------
@browser_only
def test_an_iframe_added_after_recording_starts_does_not_swallow_the_click(recorder):
    """The probe used to default to radar mode. A dynamically inserted frame would then eat the
    user's real click instead of letting the page act on it."""
    session, page = recorder("<h2>Outer</h2><div id='slot'></div>")
    page.evaluate("""() => {
        const frame = document.createElement('iframe');
        frame.id = 'lateFrame';
        frame.srcdoc = "<button id='late' onclick=\\"this.dataset.clicked='yes'\\">Later</button>";
        document.getElementById('slot').appendChild(frame);
    }""")
    page.wait_for_timeout(600)
    button = page.frame_locator("#lateFrame").locator("#late")
    button.click()
    steps = session.pump(1.5)
    assert button.get_attribute("data-clicked") == "yes", "the page must still act on the click"
    assert len(steps) == 1 and steps[0]["action"] == "click"


@browser_only
def test_a_frame_reloaded_after_recording_starts_keeps_recording(recorder):
    session, page = recorder(
        "<iframe id='f' srcdoc=\"<button id='b'>One</button>\"></iframe>")
    page.evaluate("""() => { document.getElementById('f').srcdoc = "<button id='b' onclick=\\"document.title='REACHED'\\">Two</button>"; }""")
    page.wait_for_timeout(600)
    session.pump(1.5)          # the session re-asserts the probe on its own cadence
    page.frame_locator("#f").locator("#b").click()
    steps = session.pump(1.0)
    assert len(steps) == 1 and steps[0]["action"] == "click"


@browser_only
def test_a_new_frame_while_paused_records_nothing_and_still_lets_the_page_act(recorder):
    session, page = recorder("<h2>Outer</h2><div id='slot'></div>")
    session.pause()
    page.evaluate("""() => {
        const frame = document.createElement('iframe');
        frame.id = 'pausedFrame';
        frame.srcdoc = "<button id='p' onclick=\\"document.title='REACHED'\\">Paused</button>";
        document.getElementById('slot').appendChild(frame);
    }""")
    page.wait_for_timeout(600)
    page.frame_locator("#pausedFrame").locator("#p").click()
    assert session.pump(1.5) == [], "paused means capture nothing"
    inner = page.frame_locator("#pausedFrame").locator("#p")
    assert inner.is_visible(), "and the page must keep working normally"


@browser_only
def test_a_popup_that_navigates_after_being_attached_keeps_recording(recorder):
    session, page = recorder(POPUP)
    with page.expect_popup() as pending:
        page.click("#opener")
    popup = pending.value
    page.wait_for_timeout(300)
    popup.goto(ENDPOINT.rstrip("/") + "/json/version")
    popup.set_content("<button id='afterNav'>Continue</button>")
    session.pump(1.5)        # the session re-asserts ownership of the rewritten document itself
    popup.click("#afterNav")
    steps = session.pump(1.0)
    assert [step["action"] for step in steps] == ["click"]
    popup.close()


# --- P0-5: native Windows capture, wired honestly ------------------------------------------
def test_native_capture_reports_unavailable_rather_than_pretending(tmp_path):
    session = RecordingSession(None, tmp_path, queue.Queue(), mode="record")
    session.armed_mode = "record"
    assert session.native_start() is False, "there is no Windows input hook on this runtime"
    assert session.native_running is False


def test_a_native_interaction_enters_the_same_pipeline_as_a_browser_one(tmp_path):
    session = RecordingSession(None, tmp_path, queue.Queue(), mode="record")
    session.journal = DraftJournal(tmp_path)
    session.native_step({"action": "desktop_click", "x": 640, "y": 480, "button": "left", "sequence": 1})
    steps = session.pump(1.2)
    assert len(steps) == 1 and steps[0]["action"] == "desktop_click"
    assert steps[0]["source"] == "desktop" and steps[0]["enriched"] is False
    assert session.journal.steps()[0]["action"] == "desktop_click"


def test_a_native_key_keeps_its_keyboard_evidence(tmp_path):
    """The raw hook no longer probes Windows itself, so its keyboard evidence must reach the
    manager rather than being thrown away with the rest of the old duplicate probe."""
    session = RecordingSession(None, tmp_path, queue.Queue(), mode="record")
    session.native_step({"action": "desktop_press", "value": "enter", "sequence": 2})
    queued = session.pending.get_nowait()
    keyboard = queued["payload"]["layers"]["keyboard"]
    assert keyboard["status"] == fp.FOUND and keyboard["data"]["key"] == "enter"


def test_the_native_hook_reports_raw_input_only(tmp_path, monkeypatch):
    """It used to probe UIA and screenshot here, then the session discovered everything again."""
    import smartops_desktop.desktop_discovery as dd
    called = []
    monkeypatch.setattr(dd, "probe_windows_at", lambda *a, **k: called.append(a) or {})
    sent = []
    recorder = dd.DesktopInputRecorder(sent.append, __import__("threading").Event(), tmp_path)
    recorder._emit_click(120, 340, "left")
    assert called == [], "the hook must not run discovery of its own"
    assert sent[0]["action"] == "desktop_click" and sent[0]["x"] == 120
    assert "evidence" not in sent[0]


def test_a_browser_click_marks_itself_so_the_native_hook_cannot_double_count_it(tmp_path):
    marked = []

    class FakeHook:
        def mark_browser_event(self, x, y): marked.append(("click", x, y))
        def mark_browser_key(self, key): marked.append(("key", key))
        def stop(self): pass

    session = RecordingSession(None, tmp_path, queue.Queue(), mode="record")
    session.native = FakeHook()
    session.mark_browser({"screen": {"x": 100, "y": 200}, "action": "click"})
    session.mark_browser({"action": "press", "value": "Control+S"})
    assert marked == [("click", 100, 200), ("key", "Control+S")]


# --- P1: the whole frame route survives ----------------------------------------------------
@browser_only
def test_a_nested_frame_step_keeps_the_entire_route_not_just_the_last_hop(recorder, tmp_path):
    from smartops_desktop.core import Store
    session, page = recorder(NESTED)
    page.frame_locator("#outerFrame").frame_locator("#innerFrame").locator("#deep").click()
    step = timeline(session)[0]
    assert step["frame_depth"] == 2
    assert len(step["frame_chain"]) == 2 and step["frame"] == step["frame_chain"][-1]
    # and it must survive save, reload and an edit
    store = Store(tmp_path / "chain-data")
    saved = store.save({"schema_version": 1, "name": "Nested", "steps": [step]})
    reloaded = next(w for w in store.list_workflows() if w["id"] == saved["id"])["steps"][0]
    assert reloaded["frame_chain"] == step["frame_chain"]
    store.db.close()


# --- A18: recording must never wait for discovery ------------------------------------------
@browser_only
def test_a_slow_detector_never_delays_the_recorded_step(recorder, tmp_path):
    """The whole point of deferring enrichment: a screenshot or a UIA lookup must not sit between
    the user's click and the step reaching disk."""
    import time as _time
    from smartops_desktop.discovery import DEFAULT_DETECTORS, Detector

    class Slow(Detector):
        key = "visual"

        def observe(self, context):
            _time.sleep(0.6)
            return fp.observation(self.key, fp.FOUND, "slow but done", 0.65)

    manager = DiscoveryManager([Slow()] + [d for d in DEFAULT_DETECTORS if d.key != "visual"])
    session, page = recorder(FORM, manager=manager)
    page.click("#save")
    page.wait_for_timeout(250)
    started = _time.monotonic()
    steps = session.drain()          # capture only
    elapsed = _time.monotonic() - started
    assert len(steps) == 1
    assert elapsed < 0.3, f"capture waited {elapsed:.2f}s on a detector it should not wait for"
    assert steps[0]["enriched"] is False
    recorded = [x for x in session.journal.steps() if x.get("origin") != "start_context"]
    assert recorded[0]["action"] == "click", "the action is durable before enrichment runs"
    assert recorded[0]["enriched"] is False
    session.enrich(budget=1)
    recorded = [x for x in session.journal.steps() if x.get("origin") != "start_context"]
    assert recorded[0]["enriched"] is True and "fingerprint" in recorded[0]


@browser_only
def test_enrichment_arrives_as_a_patch_and_carries_ranked_locators(recorder):
    session, page = recorder(FORM)
    page.click("#save")
    step = timeline(session)[0]
    assert step["enriched"] is True
    assert step["locators"] and step["locators"][0]["kind"] == "id"
    assert step["primary_locator"]["value"] == "#save"
    assert step["selector"] == "#save"


@browser_only
def test_a_step_records_which_logical_page_it_happened_on(recorder):
    session, page = recorder(POPUP)
    with page.expect_popup() as pending:
        page.click("#opener")
    popup = pending.value
    popup.set_content("<button id='inPopup'>Continue</button>")
    session.pump(1.2)
    popup.click("#inPopup")
    steps = session.pump(1.2)
    contexts = [step["page_context"] for step in steps]
    assert contexts[-1]["logical_page_id"] == "popup-1"
    assert contexts[-1]["relation"] == "popup"
    assert contexts[-1]["opener_page_id"] == "page-1"
    popup.close()


@browser_only
def test_the_context_picture_of_a_nested_iframe_element_shows_the_element(recorder):
    """Playwright already reports main-frame coordinates. Adding frame offsets to them
    double-counts, so the surrounding shot landed somewhere else entirely."""
    session, page = recorder(NESTED_COLOURED)
    page.frame_locator("#outerFrame").frame_locator("#innerFrame").locator("#deep").click()
    visual = timeline(session, 2.5)[0]["fingerprint"]["layers"]["visual"]["data"]
    from PIL import Image
    for name, floor in (("element_png", 0.9), ("context_png", 0.05)):
        image = Image.open(visual[name]).convert("RGB")
        total = image.size[0] * image.size[1]
        red = sum(count for count, (r, g, b) in (image.getcolors(maxcolors=total) or []) if r > 200 and g < 60 and b < 60)
        assert red / total > floor, f"{name} does not contain the target ({red}/{total} red)"
