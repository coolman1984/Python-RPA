"""Target ownership must survive everything a URL cannot: duplicates, navigation, SPA routing."""
import os

import pytest

from smartops_desktop import targets

ENDPOINT = os.environ.get("SMARTOPS_TEST_CDP", "")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="Set SMARTOPS_TEST_CDP to a Chrome debugging endpoint.")


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        yield playwright.chromium.connect_over_cdp(ENDPOINT, timeout=8000)


@pytest.fixture
def tab(browser):
    opened = []

    def make():
        page = browser.contexts[0].new_page()
        page.goto(ENDPOINT.rstrip("/") + "/json/version")
        opened.append(page)
        return page

    yield make
    for page in opened:
        if not page.is_closed():
            page.close()


def test_two_tabs_showing_the_same_address_are_still_told_apart(tab):
    first, second = tab(), tab()
    assert first.url == second.url
    assert targets.target_id(first) != targets.target_id(second)


def test_the_right_one_of_two_identical_tabs_is_returned(browser, tab):
    first, second = tab(), tab()
    wanted = targets.target_id(second)
    assert targets.page_for_target(browser, wanted) is second
    assert targets.page_for_target(browser, targets.target_id(first)) is first


def test_ownership_survives_a_full_navigation(browser, tab):
    page = tab()
    identifier = targets.target_id(page)
    page.goto(ENDPOINT.rstrip("/") + "/json/list")
    assert targets.target_id(page) == identifier
    assert targets.page_for_target(browser, identifier) is page


def test_ownership_survives_spa_navigation(browser, tab):
    page = tab()
    identifier = targets.target_id(page)
    page.evaluate("history.pushState({}, '', '/report/september')")
    assert page.url.endswith("/report/september")
    assert targets.page_for_target(browser, identifier) is page


def test_ownership_survives_set_content(browser, tab):
    page = tab()
    identifier = targets.target_id(page)
    page.set_content("<h1>Different document entirely</h1>")
    assert targets.page_for_target(browser, identifier) is page


def test_a_closed_target_is_reported_clearly_rather_than_silently_swapped(browser, tab):
    page = tab()
    identifier = targets.target_id(page)
    page.close()
    with pytest.raises(ValueError, match="no longer open"):
        targets.page_for_target(browser, identifier)


def test_the_picker_shows_something_a_person_can_recognise(browser, tab):
    page = tab()
    page.set_content("<title>Daily production report</title><h1>Report</h1>")
    row = next(r for r in targets.list_targets(browser) if r["target_id"] == targets.target_id(page))
    assert row["title"] == "Daily production report"
    assert row["location"].startswith("127.0.0.1")
    assert row["url"]
