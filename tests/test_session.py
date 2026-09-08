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

POPUP = """<a id="opener" href="about:blank" target="_blank">Open a second tab</a>"""


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
    journal.append({"action": "click", "label": "Save"})
    assert json.loads((tmp_path / "draft.json").read_text())["steps"] == 1
    assert len(journal.steps()) == 1


def test_a_draft_truncated_by_a_crash_keeps_everything_before_the_break(tmp_path):
    journal = DraftJournal(tmp_path)
    journal.append({"action": "click", "label": "One"})
    journal.append({"action": "fill", "label": "Two"})
    with (tmp_path / "draft.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"action": "click", "lab')  # power cut mid-write
    recovered = journal.steps()
    assert [step["label"] for step in recovered] == ["One", "Two"]


def test_an_unfinished_recording_is_offered_back_and_a_saved_one_is_not(tmp_path):
    crashed = DraftJournal(tmp_path / "run-a")
    crashed.note(state=RECORDING)
    finished = DraftJournal(tmp_path / "run-b")
    finished.note(state=SAVED)
    offered = DraftJournal.unfinished(tmp_path)
    assert [item["session_id"] for item in offered] == [crashed.session_id]
    assert offered[0]["folder"].endswith("run-a")


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
    page.wait_for_timeout(250)
    steps = session.drain()
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
    steps = session.drain()
    secure = [step for step in steps if step.get("secure")]
    assert secure, "the control must still be recorded, just without its value"
    assert secure[0]["label"] == "Manual secure input required"
    assert "value" not in secure[0]
    assert "hunter2" not in json.dumps(steps)


@browser_only
def test_events_inside_an_iframe_are_recorded_not_discarded(recorder):
    session, page = recorder(IFRAME)
    page.frame_locator("#reportFrame").locator("#inner").click()
    page.wait_for_timeout(300)
    steps = session.drain()
    assert len(steps) == 1
    frame_layer = steps[0]["fingerprint"]["layers"]["frame"]
    assert frame_layer["data"]["top_level"] is False
    assert steps[0].get("frame")


@browser_only
def test_events_inside_a_nested_iframe_are_recorded_with_their_depth(recorder):
    session, page = recorder(NESTED)
    page.frame_locator("#outerFrame").frame_locator("#innerFrame").locator("#deep").click()
    page.wait_for_timeout(300)
    steps = session.drain()
    assert len(steps) == 1
    assert steps[0]["fingerprint"]["layers"]["frame"]["data"]["depth"] == 2


@browser_only
def test_the_element_picture_really_shows_the_element_inside_an_iframe(recorder):
    """The old code clipped the top-level page by frame-relative coordinates, photographing the
    wrong area. The button is pure red, so the pixels prove which element was captured."""
    session, page = recorder(IFRAME)
    page.frame_locator("#reportFrame").locator("#inner").click()
    page.wait_for_timeout(300)
    steps = session.drain()
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
    session.drain()
    for frame in page.frames:
        left = frame.evaluate("document.querySelectorAll('[data-smartops-probe]').length")
        assert left == 0, f"a marker was left in {frame.url or 'a frame'}"


@browser_only
def test_a_new_tab_opened_from_the_target_joins_the_same_session(recorder):
    session, page = recorder(POPUP)
    with page.expect_popup() as pending:
        page.click("#opener")
    popup = pending.value
    page.wait_for_timeout(400)
    assert len(session.pages) == 2 and any(p is popup for p in session.pages)
    popup.set_content("<button id='inPopup'>Continue</button>")
    session.install(popup)
    popup.click("#inPopup")
    popup.wait_for_timeout(300)
    steps = session.drain()
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
    assert session.drain() == []
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
    page.wait_for_timeout(250)
    steps = session.drain()
    assert len(steps) == 1, "the step must still be recorded"
    assert steps[0]["fingerprint"]["layers"]["web"]["status"] == fp.FAILED
    assert steps[0]["fingerprint"]["layers"]["anchor"]["status"] == fp.FOUND


@browser_only
def test_ten_consecutive_recordings_of_the_same_action_agree(recorder):
    session, page = recorder(FORM)
    seen = []
    for _ in range(10):
        page.click("#save")
        page.wait_for_timeout(180)
        steps = session.drain()
        assert len(steps) == 1
        web = steps[0]["fingerprint"]["layers"]["web"]
        seen.append((steps[0]["action"], steps[0]["selector"], web["confidence"], steps[0]["best_layer"]))
    assert len(set(seen)) == 1, f"recordings drifted: {set(seen)}"
