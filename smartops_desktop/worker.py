"""One disposable automation process per operation; never owns the GUI database."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import targets
from .discovery import DiscoveryManager
from .session import RecordingSession
from .core import cdp_url, http_url, validate_workflow, validate_xlsx, atomic_text


class Cancelled(Exception):
    pass


def check_cancel(stop):
    if stop.is_set():
        raise Cancelled("Stopped by user.")


def connect(playwright, settings):
    endpoint = cdp_url(settings["cdp_url"])
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=8000)
    except Exception as exc:
        raise RuntimeError("Cannot connect to Chrome. In Settings, use a local Chrome CDP endpoint. Ordinary Chrome tabs do not expose CDP automatically.") from exc
    # CDP also exists in other Chromium browsers; verify Google Chrome explicitly.
    session = browser.new_browser_cdp_session()
    info = session.send("Browser.getVersion")
    session.detach()
    agent = info.get("userAgent", "")
    if "Chrome/" not in agent or any(word in agent for word in ("Edg/", "OPR/", "HeadlessChrome/")):
        raise RuntimeError("Connect to an interactive Google Chrome instance.")
    return browser


def pages_for(browser):
    return [p for context in browser.contexts for p in context.pages if p.url.startswith(("http://", "https://"))]


def select_page(browser, url):
    pages = [p for p in pages_for(browser) if p.url == url]
    if len(pages) != 1:
        raise ValueError("Choose one unique Chrome tab. Close duplicate tabs or refresh the tab list.")
    return pages[0]


def safe_error(exc):
    # Playwright call logs may include typed values or selectors. Keep the first line only.
    first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return re.sub(r"https?://\S+", "[page]", first)[:500]


def open_session(browser, run_dir, output, mode, target_id="", page_url=""):
    """One owner for the tab, from arming to review. Identity is the CDP target id, not the URL."""
    session = RecordingSession(browser, run_dir, output, mode=mode)
    session.prepare()
    if not target_id:
        # Older saved workflows only kept a URL. Resolve it once, then hold the target id.
        matches = [t for t in targets.list_targets(browser) if t["url"] == page_url]
        if len(matches) != 1:
            raise ValueError("Choose one recording target. Two tabs showing the same page cannot be told apart by address; pick it from the list.")
        target_id = matches[0]["target_id"]
    session.choose(target_id)
    session.arm()
    return session


def record(page_or_browser, output, stop, run_dir, target_id="", page_url=""):
    """Attended recording across frames and popups, through the same discovery pipeline as the radar."""
    session = open_session(page_or_browser, run_dir, output, "record", target_id, page_url)
    output.put({"type": "log", "message": "Recording started on " + (targets.short_location(session.page.url) or "the selected tab")
                + ". Frames and any new tabs it opens are followed automatically."})
    session.run(stop)
    return session


def inspect(browser, run_dir, output, stop, target_id="", page_url=""):
    """The element radar: the same probe and the same manager, pointing instead of recording."""
    session = open_session(browser, run_dir, output, "radar", target_id, page_url)
    output.put({"type": "log", "message": "Radar armed. Click any element in the selected tab; the click is captured, not passed to the page."})
    session.run(stop)
    return session


def replay(workflow, settings, run_dir, output, stop, page=None):
    from openpyxl import Workbook
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    last_file = None
    validated = False
    timeout = int(settings.get("timeout_seconds", 30)) * 1000
    if page:
        page.set_default_timeout(timeout)
        page.set_default_navigation_timeout(timeout)
    for number, step in enumerate(validate_workflow(workflow)["steps"]):
        check_cancel(stop)
        action = step["action"]
        output.put({"type": "step", "index": number, "status": "running", "message": f"Step {number+1}: {action}"})
        if action == "wait":
            if stop.wait(step.get("seconds", 1)):
                raise Cancelled()
        elif action == "demo_export":
            book = Workbook()
            sheet = book.active
            sheet.title = "Production"
            sheet.append(["Date", "Line", "Quantity"])
            for line, qty in [("VD-01", 120), ("VD-02", 98), ("VD-03", 144)]:
                sheet.append(["2026-09-08", line, qty])
            last_file = run_dir / "sample-production.xlsx"
            book.save(last_file)
            book.close()
            validated = False
            output.put({"type": "artifact", "path": str(last_file)})
        elif action == "validate_xlsx":
            candidate = Path(step["path"]) if step.get("path") else last_file
            if candidate is None:
                raise ValueError("Add a download step or choose an existing file before validation.")
            result = validate_xlsx(candidate, step.get("min_rows", 1), step.get("required_columns", []), step.get("sheet") or None)
            last_file = candidate
            validated = True
            output.put({"type": "validation", "path": str(candidate), "result": result, "message": f"Validation passed: {result['rows']} data rows in {result['sheet']}."})
        elif action == "secure_input":
            # Never claim a run passed when a human still has to type the secret.
            raise ValueError(f"Step {number+1} needs a password or code typed by hand. "
                             "Sign in manually first, then remove or replace this step; SmartOps will not type credentials.")
        elif page is None:
            raise ValueError("This action requires a connected Chrome tab.")
        elif action == "navigate":
            page.goto(http_url(step["url"]), wait_until="domcontentloaded")
        elif action == "nexacro_probe":
            result = page.evaluate("""() => ({available: typeof nexacro !== 'undefined', application: typeof nexacro !== 'undefined' && typeof nexacro.getApplication === 'function', title: document.title, idCount: document.querySelectorAll('[id]').length})""")
            atomic_text(run_dir / "nexacro-probe.json", json.dumps(result, indent=2))
            output.put({"type": "log", "message": "Nexacro probe: " + json.dumps(result)})
        else:
            target = page.frame_locator(step["frame"]).locator(step["selector"]) if step.get("frame") else page.locator(step["selector"])
            if action == "click":
                target.click()
            elif action == "fill":
                if target.get_attribute("type") == "password":
                    raise ValueError("Password fields require manual sign-in.")
                target.fill(step["value"])
            elif action == "select":
                target.select_option(step["value"])
            elif action == "check":
                target.set_checked(step.get("checked", True))
            elif action == "press":
                target.press(step["value"])
            elif action == "download":
                with page.expect_download(timeout=timeout) as pending:
                    target.click()
                download = pending.value
                # Ignore server filename and extension; validate bytes before naming XLSX.
                candidate = run_dir / f"download-{number+1}.bin"
                download.save_as(str(candidate))
                last_file = candidate
                validated = False
                output.put({"type": "artifact", "path": str(candidate)})
                result = validate_xlsx(candidate, step.get("min_rows", 1), step.get("required_columns", []))
                last_file = candidate.with_suffix(".xlsx")
                candidate.rename(last_file)
                validated = True
                output.put({"type": "validation", "path": str(last_file), "result": result, "message": f"Downloaded workbook validated: {result['rows']} rows."})
        check_cancel(stop)
        output.put({"type": "step", "index": number, "status": "passed", "message": f"Step {number+1} completed"})
    return {"artifact": str(last_file or ""), "validated": validated}


def worker_main(mode, workflow, settings, run_dir, page_url, output, stop, target_id=""):
    try:
        if mode == "replay" and not workflow.get("steps"):
            raise ValueError("Add or record at least one step before running.")
        # secure_input is refused outright, so there is no point connecting to Chrome to say so.
        offline_actions = {"demo_export", "validate_xlsx", "wait", "secure_input"}
        browser_needed = mode in {"tabs", "record", "inspect"} or any(
            s.get("action") not in offline_actions for s in workflow.get("steps", []))
        if browser_needed:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = connect(playwright, settings)
                if mode == "tabs":
                    output.put({"type": "tabs", "tabs": targets.list_targets(browser)})
                    output.put({"type": "done", "status": "connected"})
                    return
                if mode == "record":
                    record(browser, output, stop, run_dir, target_id, page_url)
                    output.put({"type": "done", "status": "recorded"})
                    return
                if mode == "inspect":
                    inspect(browser, run_dir, output, stop, target_id, page_url)
                    output.put({"type": "done", "status": "inspected"})
                    return
                page = targets.page_for_target(browser, target_id) if target_id else select_page(browser, page_url)
                result = replay(workflow, settings, run_dir, output, stop, page)
                output.put({"type": "tab_updated", "url": page.url})
                # Leaving Playwright disconnects; never close the user's Chrome browser.
        else:
            result = replay(workflow, settings, run_dir, output, stop)
        output.put({"type": "done", "status": "passed", **result})
    except Cancelled:
        output.put({"type": "done", "status": "cancelled", "message": "Stopped by user."})
    except Exception as exc:
        output.put({"type": "done", "status": "failed", "message": safe_error(exc)})
