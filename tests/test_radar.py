"""Radar layers against a real Chromium over CDP.

Skipped unless SMARTOPS_TEST_CDP points at a running debugging endpoint, so the ordinary
build does not depend on a browser. To run these:

    chrome --headless=new --remote-debugging-port=9222 --user-data-dir=<temp>
    SMARTOPS_TEST_CDP=http://127.0.0.1:9222 pytest tests/test_radar.py
"""
import os
import queue

import pytest

from smartops_desktop import fingerprint as fp
from smartops_desktop.worker import arm_radar, disarm_radar, drain_radar

ENDPOINT = os.environ.get("SMARTOPS_TEST_CDP", "")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="Set SMARTOPS_TEST_CDP to a Chrome debugging endpoint.")

PLAIN = """<h2>Daily production</h2>
<form><label for="qty">Quantity produced</label><input id="qty" name="quantity" type="text">
<button id="save" type="button" onclick="document.title='PAGE REACTED'">Save report</button></form>"""

NEXACRO = """<h2>G-MES Daily Report</h2>
<div id="mainframe.WorkFrame.form.divWork.form">
  <div id="mainframe.WorkFrame.form.divWork.form.grdList">
    <div id="mainframe.WorkFrame.form.divWork.form.grdList.body.3.2" style="width:120px;height:24px">VD-01</div>
  </div>
  <button id="mainframe.WorkFrame.form.divWork.form.btnSearch">Search</button>
</div>
<script>window.nexacro = { getApplication: () => ({name: 'GMES'}) };</script>"""


@pytest.fixture
def radar(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ENDPOINT, timeout=8000)
        page = browser.contexts[0].new_page()
        # Bindings need a real http origin; the endpoint's own JSON page is the cheapest one.
        page.goto(ENDPOINT.rstrip("/") + "/json/version")
        page.set_viewport_size({"width": 1000, "height": 700})

        def point_at(html, *selectors):
            page.set_content(html)
            output, pending = queue.Queue(), []
            arm_radar(page, pending)
            for selector in selectors:
                page.click(selector, timeout=5000)
                page.wait_for_timeout(250)
            drain_radar(page, tmp_path, pending, output, 0)
            disarm_radar(page)
            events = [output.get_nowait() for _ in range(output.qsize())]
            return page, [event["fingerprint"] for event in events if event["type"] == "element"]

        yield point_at
        page.close()


def layers_of(fingerprint):
    return {key: value["status"] for key, value in fingerprint["layers"].items()}


def test_pointing_at_a_field_reports_several_independent_layers(radar):
    _page, found = radar(PLAIN, "#qty")
    assert len(found) == 1
    status = layers_of(found[0])
    assert status["web"] == status["accessibility"] == status["anchor"] == fp.FOUND
    assert status["image"] == status["relative"] == status["keyboard"] == fp.FOUND
    assert found[0]["layers"]["accessibility"]["data"]["role"] == "textbox"
    assert found[0]["layers"]["accessibility"]["data"]["name"] == "Quantity produced"
    assert found[0]["layers"]["anchor"]["data"]["text"] == "Quantity produced"
    assert fp.score(found[0])["confident"]


def test_layers_not_built_yet_are_reported_as_unavailable_not_failed(radar):
    _page, found = radar(PLAIN, "#save")
    status = layers_of(found[0])
    assert status["windows"] == status["ocr"] == status["vision"] == fp.UNAVAILABLE
    assert status["nexacro"] == fp.UNAVAILABLE  # an ordinary page is not a Nexacro screen


def test_pointing_never_lets_the_page_act_on_the_click(radar):
    page, found = radar(PLAIN, "#save")
    assert found and page.title() != "PAGE REACTED"


def test_nexacro_cell_reports_component_form_grid_row_and_column(radar):
    _page, found = radar(NEXACRO, "#mainframe\\.WorkFrame\\.form\\.divWork\\.form\\.grdList\\.body\\.3\\.2")
    nexacro = found[0]["layers"]["nexacro"]
    assert nexacro["status"] == fp.FOUND
    assert nexacro["data"]["component"] == "grdList"
    assert nexacro["data"]["grid"] == "grdList"
    assert nexacro["data"]["form"] == "divWork"
    assert nexacro["data"]["row"] == 3 and nexacro["data"]["column"] == 2


def test_nexacro_button_reports_its_component_and_parent(radar):
    _page, found = radar(NEXACRO, "#mainframe\\.WorkFrame\\.form\\.divWork\\.form\\.btnSearch")
    data = found[0]["layers"]["nexacro"]["data"]
    assert data["component"] == "btnSearch" and data["form"] == data["parent"] == "divWork"


def test_a_picture_and_a_json_fingerprint_are_kept_for_every_element(radar, tmp_path):
    _page, found = radar(PLAIN, "#qty", "#save")
    assert len(found) == 2
    for index, fingerprint in enumerate(found, 1):
        image = fingerprint["layers"]["image"]["data"]
        assert (tmp_path / f"element-{index}.json").is_file()
        assert os.path.isfile(image["element_png"]) and os.path.isfile(image["context_png"])


def test_the_probe_leaves_no_marker_behind_on_the_page(radar):
    page, found = radar(PLAIN, "#qty")
    assert found and page.evaluate("document.querySelectorAll('[data-smartops-probe]').length") == 0
