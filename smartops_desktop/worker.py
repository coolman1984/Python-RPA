"""One disposable automation process per operation; never owns the GUI database."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .core import cdp_url, http_url, validate_workflow, validate_xlsx, atomic_text
from .discovery import enrich_recorded_step, summarize_layers


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
    first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return re.sub(r"https?://\S+", "[page]", first)[:500]


def _opener(target):
    try:
        value = target.opener
        return value() if callable(value) else value
    except Exception:
        return None


def record(page, output, stop, run_dir):
    """Record selected Chrome page plus its child frames and popups with independent discovery layers."""
    from playwright.sync_api import Error

    script = Path(__file__).with_name("recorder.js").read_text(encoding="utf-8")
    tracked_pages = set()
    capture_index = 0

    def belongs_to_recording(target):
        if target == page:
            return True
        opener = _opener(target)
        while opener:
            if opener in tracked_pages:
                return True
            opener = _opener(opener)
        return False

    def capture(source, step):
        nonlocal capture_index
        source_page = source.get("page")
        source_frame = source.get("frame")
        if source_page not in tracked_pages or stop.is_set():
            return
        try:
            cleaned = validate_workflow({"schema_version": 1, "name": "Capture", "steps": [step]})["steps"][0]
            capture_index += 1
            cleaned["page_context"] = {
                "url": source_page.url,
                "is_popup": source_page != page,
                "frame_url": source_frame.url if source_frame else source_page.url,
                "frame_name": source_frame.name if source_frame else "",
                "is_main_frame": bool(source_frame == source_page.main_frame),
            }
            cleaned = enrich_recorded_step(source_page, source_frame, cleaned, run_dir, capture_index)
            output.put({"type": "recorded", "step": cleaned, "layers": summarize_layers(cleaned)})
        except ValueError:
            pass
        except Exception as exc:
            try:
                fallback = validate_workflow({"schema_version": 1, "name": "Capture", "steps": [step]})["steps"][0]
                fallback["discovery"] = {"version": 1, "layers": {"enrichment": {"status": "error", "reason": safe_error(exc)}}}
                output.put({"type": "recorded", "step": fallback, "layers": []})
            except Exception:
                pass

    def attach(target):
        if target in tracked_pages or not belongs_to_recording(target):
            return
        tracked_pages.add(target)
        try:
            target.expose_binding("__smartopsCapture", capture)
        except Exception:
            pass
        try:
            target.add_init_script(script)
        except Exception:
            pass
        try:
            target.evaluate(script)
        except Exception:
            pass

    tracked_pages.add(page)
    try:
        page.expose_binding("__smartopsCapture", capture)
    except Exception:
        pass
    page.add_init_script(script)
    page.evaluate(script)
    page.context.on("page", attach)

    parsed = urlsplit(page.url)
    initial = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    output.put({"type": "recorded", "step": {"action": "navigate", "url": initial, "label": "Open starting page (review URL parameters)", "discovery": {"version": 1, "layers": {"navigation": {"status": "ok", "url": initial}}}}, "layers": ["navigation"]})
    output.put({"type": "log", "message": "Multi-layer recording is active: DOM, Nexacro hints, Chrome accessibility, Windows UI Automation, Win32, anchors, visual crop and relative position. Child frames and popups are observed when Chrome exposes them."})
    output.put({"type": "log", "message": "Sign in before recording. Password and recognizable sign-in fields are excluded. Review captured business values before sharing a workflow."})

    try:
        while not stop.is_set():
            live = [p for p in tracked_pages if not p.is_closed()]
            if not live:
                raise RuntimeError("All recorded tabs were closed. Captured steps remain available for review.")
            live[0].wait_for_timeout(100)
    finally:
        try:
            page.context.remove_listener("page", attach)
        except Exception:
            pass
        for target in list(tracked_pages):
            if target.is_closed():
                continue
            try:
                target.evaluate("window.__smartopsCapture = undefined")
            except Error:
                pass
            except Exception:
                pass


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
        browser_needed = mode in {"tabs", "record"} or any(s["action"] not in {"demo_export", "validate_xlsx", "wait"} for s in workflow.get("steps", []))
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
                    record(page, output, stop, run_dir)
                    output.put({"type": "tab_updated", "url": page.url if not page.is_closed() else page_url})
                    output.put({"type": "done", "status": "recorded"})
                    return
                result = replay(workflow, settings, run_dir, output, stop, page)
                output.put({"type": "tab_updated", "url": page.url})
        else:
            result = replay(workflow, settings, run_dir, output, stop)
        output.put({"type": "done", "status": "passed", **result})
    except Cancelled:
        output.put({"type": "done", "status": "cancelled", "message": "Stopped by user."})
    except Exception as exc:
        output.put({"type": "done", "status": "failed", "message": safe_error(exc)})
