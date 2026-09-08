"""Stable browser-target identity.

A tab is identified by its CDP target id, never by its URL. A URL is a property of what a tab is
showing right now; it changes on every navigation, and two tabs can show the same one. Ownership
of a recording must survive both.

    target id   stable for the life of the tab, unique across tabs
    url         metadata only, refreshed for display
"""
from __future__ import annotations

from urllib.parse import urlsplit


def target_id(page):
    """The Chrome target id behind a Playwright page. Unchanged by navigation."""
    session = page.context.new_cdp_session(page)
    try:
        return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        try:
            session.detach()
        except Exception:
            pass


def short_location(url):
    """The bit of a URL a person recognises: host, and a trimmed path."""
    try:
        parsed = urlsplit(url or "")
    except Exception:
        return ""
    if not parsed.hostname:
        return ""
    path = (parsed.path or "/").rstrip("/")
    return parsed.hostname + (path if len(path) <= 40 else path[:37] + "…")


def describe(page):
    """One row for the target picker. Never raises on a tab that is closing."""
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        identifier = target_id(page)
    except Exception:
        identifier = ""
    return {"target_id": identifier, "title": (title or short_location(url) or url)[:160],
            "url": url[:400], "location": short_location(url)}


def open_targets(browser):
    """Every http(s) tab the connected browser is showing, newest context last."""
    return [page for context in browser.contexts for page in context.pages
            if not page.is_closed() and page.url.startswith(("http://", "https://"))]


def list_targets(browser):
    return [describe(page) for page in open_targets(browser)]


def page_for_target(browser, identifier):
    """Find the page that still owns this target id, whatever it now displays."""
    if not identifier:
        raise ValueError("No recording target was chosen.")
    for page in open_targets(browser):
        try:
            if target_id(page) == identifier:
                return page
        except Exception:
            continue
    raise ValueError("That browser tab is no longer open. Choose the target again.")
