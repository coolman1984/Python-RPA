"""One disposable automation process per operation; never owns the GUI database."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .discovery import DiscoveryManager
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


def record(page, output, stop):
    from playwright.sync_api import Error
    script = Path(__file__).with_name("recorder.js").read_text(encoding="utf-8")
    def capture(source, step):
        if source["page"] != page or source["frame"] != page.main_frame or stop.is_set():
            return
        try:
            cleaned = validate_workflow({"schema_version": 1, "name": "Capture", "steps": [step]})["steps"][0]
            output.put({"type": "recorded", "step": cleaned})
        except ValueError:
            pass
    page.expose_binding("__smartopsCapture", capture)
    page.add_init_script(script)
    page.evaluate(script)
    parsed = urlsplit(page.url)
    initial = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    output.put({"type": "recorded", "step": {"action": "navigate", "url": initial, "label": "Open starting page (review URL parameters)"}})
    output.put({"type": "log", "message": "Recording this tab's main frame. Sign in before recording. Password and sign-in fields are excluded. Review captured values before saving."})
    try:
        while not stop.is_set():
            if page.is_closed():
                raise RuntimeError("Recorded tab was closed. Captured steps remain available for review.")
            page.wait_for_timeout(100)
    finally:
        try:
            page.evaluate("window.__smartopsCapture = undefined")
        except Error:
            pass


def arm_radar(page, pending):
    """Install the page probe in every frame. Trusted clicks land in `pending`, not on the page."""
    script = Path(__file__).with_name("probe.js").read_text(encoding="utf-8")
    # The binding only queues. Screenshots and CDP calls happen on the caller's loop, so no
    # Playwright call is ever made from inside a Playwright callback.
    page.expose_binding("__smartopsInspectCapture",
                        lambda source, payload: pending.append({"payload": payload, "frame": source["frame"]}) if source["page"] is page else None)
    page.add_init_script(script)
    installed = 0
    for frame in page.frames:
        # A frame that is cross-origin or mid-navigation is skipped, never fatal.
        try:
            frame.evaluate(script)
            installed += 1
        except Exception:
            pass
    return installed


def disarm_radar(page):
    for frame in page.frames:
        try:
            frame.evaluate("window.__smartopsProbeArmed = false; window.__smartopsInspectCapture = undefined;")
        except Exception:
            pass


def drain_radar(page, run_dir, pending, output, index=0, manager=None):
    """Turn every queued click into one stored fingerprint. Returns the new element count."""
    manager = manager or DiscoveryManager()
    while pending:
        index += 1
        queued = pending.pop(0) or {}
        payload, frame = queued.get("payload") or {}, queued.get("frame")
        result = manager.discover_element(payload, page=page, run_dir=run_dir, index=index, frame=frame)
        atomic_text(Path(run_dir) / f"element-{index}.json", json.dumps(result, ensure_ascii=False, indent=2))
        output.put({"type": "element", "index": index, "fingerprint": result})
        try:
            page.evaluate("token => { const el = document.querySelector('[data-smartops-probe=\"' + token + '\"]'); if (el) el.removeAttribute('data-smartops-probe'); }",
                          payload.get("token", ""))
        except Exception:
            pass
    return index


def inspect(page, run_dir, output, stop):
    """Arm the radar and report a fingerprint for each element the user points at."""
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    pending = []
    frames = arm_radar(page, pending)
    output.put({"type": "log", "message": f"Radar armed across {frames} frame(s). Click any element in the selected tab. The click is captured, not passed to the page."})
    index = 0
    manager = DiscoveryManager()
    try:
        while not stop.is_set():
            if page.is_closed():
                raise RuntimeError("The inspected tab was closed. Fingerprints already captured are kept.")
            index = drain_radar(page, run_dir, pending, output, index, manager)
            page.wait_for_timeout(120)
    finally:
        disarm_radar(page)


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


def worker_main(mode, workflow, settings, run_dir, page_url, output, stop):
    try:
        if mode == "replay" and not workflow.get("steps"):
            raise ValueError("Add or record at least one step before running.")
        browser_needed = mode in {"tabs", "record", "inspect"} or any(s.get("action") not in {"demo_export", "validate_xlsx", "wait"} for s in workflow.get("steps", []))
        if browser_needed:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = connect(playwright, settings)
                if mode == "tabs":
                    output.put({"type": "tabs", "tabs": [{"title": p.title(), "url": p.url} for p in pages_for(browser)]})
                    output.put({"type": "done", "status": "connected"})
                    return
                page = select_page(browser, page_url)
                if mode == "record":
                    record(page, output, stop)
                    output.put({"type": "tab_updated", "url": page.url})
                    output.put({"type": "done", "status": "recorded"})
                    return
                if mode == "inspect":
                    inspect(page, run_dir, output, stop)
                    output.put({"type": "tab_updated", "url": page.url})
                    output.put({"type": "done", "status": "inspected"})
                    return
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
