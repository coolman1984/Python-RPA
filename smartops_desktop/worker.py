"""One disposable automation process per operation; never owns the GUI database."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .core import cdp_url, http_url, validate_workflow, validate_xlsx, atomic_text
from .discovery import NetworkJournal, enrich_browser_capture, safe_http_url
from .desktop_discovery import DesktopInputRecorder, probe_windows_at


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


def record(page, output, stop, run_dir):
    """Attended discovery recorder: browser frames/popups plus optional Windows UIA/global input evidence."""
    from playwright.sync_api import Error

    script = Path(__file__).with_name("recorder.js").read_text(encoding="utf-8")
    journal = NetworkJournal()
    tracked_pages = set()
    page_tokens = {}
    page_relations = {}
    counter = [0]
    counter_lock = threading.Lock()

    def next_sequence():
        with counter_lock:
            counter[0] += 1
            return counter[0]

    def emit(step):
        try:
            cleaned = validate_workflow({"schema_version": 1, "name": "Capture", "steps": [step]})["steps"][0]
            output.put({"type": "recorded", "step": cleaned})
        except ValueError as exc:
            output.put({"type": "log", "message": "Skipped one unsafe or invalid captured action: " + safe_error(exc)})

    desktop = DesktopInputRecorder(emit, stop, Path(run_dir) / "discovery")
    desktop_started = desktop.start()

    def capture(source, step):
        source_page = source["page"]
        if source_page not in tracked_pages or stop.is_set():
            return
        sequence = next_sequence()
        try:
            enriched = enrich_browser_capture(
                source_page,
                source["frame"],
                step,
                run_dir,
                sequence,
                network=journal,
                windows_probe=probe_windows_at,
            )
            browser_fp = enriched.setdefault("fingerprint", {}).setdefault("browser", {})
            browser_fp["page_token"] = page_tokens.get(id(source_page), "page")
            browser_fp["relation"] = page_relations.get(id(source_page), "root")
            geometry = enriched.get("fingerprint", {}).get("geometry", {})
            if geometry.get("screen_x") is not None and geometry.get("screen_y") is not None:
                desktop.mark_browser_event(geometry["screen_x"], geometry["screen_y"])
            emit(enriched)
        except Exception as exc:
            output.put({"type": "log", "message": "Discovery enrichment failed for one action; base capture was retained where safe: " + safe_error(exc)})
            emit(step)

    def instrument(target_page, relation="popup"):
        if target_page in tracked_pages:
            return
        tracked_pages.add(target_page)
        page_tokens[id(target_page)] = "page-" + str(len(page_tokens) + 1)
        page_relations[id(target_page)] = relation
        journal.attach(target_page)
        try:
            target_page.expose_binding("__smartopsCapture", capture)
        except Exception as exc:
            output.put({"type": "log", "message": "Could not expose recorder binding on a tracked page: " + safe_error(exc)})
            return
        try:
            target_page.add_init_script(script)
        except Exception:
            pass
        for frame in list(target_page.frames):
            try:
                frame.evaluate(script)
            except Exception:
                pass
        target_page.on("popup", lambda child: instrument(child, "popup"))
        output.put({"type": "log", "message": f"Discovery attached to {page_tokens[id(target_page)]} ({relation})."})

    instrument(page, "root")
    parsed = urlsplit(page.url)
    initial = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    output.put({"type": "recorded", "step": {
        "action": "navigate",
        "url": initial,
        "label": "Open starting page (review URL parameters)",
        "fingerprint": {"schema_version": 1, "browser": {"url": safe_http_url(page.url), "page_token": "page-1", "relation": "root"}},
        "detected_by": ["web"],
    }})
    output.put({
        "type": "log",
        "message": "Universal discovery recording is active: DOM, frames, popups, ARIA, Nexacro clues, anchors, geometry, visual evidence, privacy-safe network metadata"
                   + (", Windows UIA and attended desktop clicks." if desktop_started else ". Windows desktop discovery is unavailable on this runtime."),
    })
    try:
        while not stop.is_set():
            open_pages = [p for p in tracked_pages if not p.is_closed()]
            if not open_pages:
                raise RuntimeError("All recorded browser pages were closed. Captured steps remain available for review.")
            page.wait_for_timeout(100)
    finally:
        desktop.stop()
        for tracked in list(tracked_pages):
            if tracked.is_closed():
                continue
            for frame in list(tracked.frames):
                try:
                    frame.evaluate("window.__smartopsCapture = undefined")
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
        elif action in {"desktop_click", "desktop_press"}:
            raise ValueError("This desktop action was captured by the discovery layer. Desktop replay is intentionally not enabled in this recorder-first branch yet.")
        elif page is None:
            raise ValueError("This action requires a connected Chrome tab.")
        elif action == "navigate":
            page.goto(http_url(step["url"]), wait_until="domcontentloaded")
        elif action == "nexacro_probe":
            result = page.evaluate("""() => {
                const available = typeof nexacro !== 'undefined';
                const app = available && typeof nexacro.getApplication === 'function' ? nexacro.getApplication() : null;
                const form = app && typeof app.getActiveForm === 'function' ? app.getActiveForm() : null;
                const components = [];
                if (form && form.components && typeof form.components.length === 'number') {
                    for (let i=0; i<Math.min(form.components.length, 200); i++) {
                        const c = form.components[i];
                        components.push({name: c && (c.name || c.id) || '', type: c && (c._type_name || c.constructor && c.constructor.name) || ''});
                    }
                }
                return {available, application: !!app, form: form && (form.name || form.id) || '', components, title: document.title, idCount: document.querySelectorAll('[id]').length};
            }""")
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
        non_browser_replay = {"demo_export", "validate_xlsx", "wait", "desktop_click", "desktop_press"}
        browser_needed = mode in {"tabs", "record"} or any(s["action"] not in non_browser_replay for s in workflow.get("steps", []))
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
