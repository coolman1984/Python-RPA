"""One long-lived recording session that owns its browser target from arming to save.

The old path connected once to list tabs, threw that connection away, then connected again to
record and re-found the tab by URL. Two authorities for one tab, and an identity that any
navigation could break. This module replaces both: one session, one owner, target identity by CDP
target id, and every accepted step on disk the moment it is accepted.

The radar and the recorder run the same probe through the same DiscoveryManager. The only
difference is the probe's mode and what the session does with the result.
"""
from __future__ import annotations

import json
import os
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from . import fingerprint as fp
from . import targets
from .discovery import DiscoveryManager, NetworkJournal

IDLE, PREPARING, TARGET_PICK, ARMING, RECORDING, PAUSED, STOPPING, REVIEW, SAVED, DISCARDED = (
    "idle", "preparing", "target_pick", "arming", "recording", "paused", "stopping", "review", "saved", "discarded")
# Only a draft the user actually finished with stops being offered back. REVIEW is not finished:
# a crash between Stop and Save must not lose an attended recording.
CLOSED = {SAVED, DISCARDED}

TRANSITIONS = {
    IDLE: {PREPARING},
    PREPARING: {TARGET_PICK, STOPPING},
    TARGET_PICK: {ARMING, STOPPING},
    ARMING: {RECORDING, STOPPING},
    RECORDING: {PAUSED, STOPPING},
    PAUSED: {RECORDING, STOPPING},
    STOPPING: {REVIEW},
    REVIEW: {SAVED, DISCARDED, STOPPING},
    SAVED: set(),
    DISCARDED: set(),
}


class SessionError(RuntimeError):
    pass


class DraftJournal:
    """Append-safe record of one recording. A crash loses at most the last line."""

    def __init__(self, folder, session_id=None, target=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or str(uuid4())
        self.header_path = self.folder / "draft.json"
        self.steps_path = self.folder / "draft.jsonl"
        self.header = {"session_id": self.session_id, "created": fp.now(), "state": PREPARING,
                       "target": target or {}, "steps": 0}
        self._write_header()

    def _write_header(self):
        handle, temporary = tempfile.mkstemp(prefix=".draft-", dir=self.folder)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.header, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.header_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def note(self, **fields):
        self.header.update(fields)
        self._write_header()

    def append(self, step):
        with self.steps_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(step, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.header["steps"] = self.header.get("steps", 0) + 1
        self._write_header()

    def steps(self):
        if not self.steps_path.exists():
            return []
        found = []
        for line in self.steps_path.read_text(encoding="utf-8").splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                break  # a crash mid-write truncates here; everything before it is intact
        return found

    @classmethod
    def unfinished(cls, root):
        """Drafts left behind by a crash, newest first."""
        found = []
        for header_path in sorted(Path(root).glob("*/draft.json")):
            try:
                header = json.loads(header_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if header.get("state") not in CLOSED:
                header["folder"] = str(header_path.parent)
                found.append(header)
        return sorted(found, key=lambda item: item.get("created", ""), reverse=True)


class RecordingSession:
    """Owns one browser target for the whole of a recording."""

    def __init__(self, browser, run_dir, output, manager=None, mode="record"):
        self.browser = browser
        self.run_dir = Path(run_dir)
        self.output = output
        self.manager = manager or DiscoveryManager()
        self.mode = mode
        self.armed_mode = mode   # what 'resume' goes back to
        self.state = IDLE
        self.target = {}
        self.page = None
        self.pages = []          # the owned target plus every popup it opened
        self.pending = []
        self.network = NetworkJournal()
        self.journal = None
        self.native = None
        self.native_running = False
        self.stop_event = threading.Event()
        self.count = 0
        self._ticks = 0
        self._new_pages = []
        self.refresh_every = 8   # ~1s at the default interval
        self._script = Path(__file__).with_name("probe.js").read_text(encoding="utf-8")

    # --- state machine --------------------------------------------------------------------
    def move(self, state):
        if state not in TRANSITIONS[self.state]:
            raise SessionError(f"Cannot go from {self.state} to {state}.")
        self.state = state
        if self.journal:
            self.journal.note(state=state)
        self.output.put({"type": "session", "state": state})
        return state

    # --- preparation ----------------------------------------------------------------------
    def prepare(self):
        self.move(PREPARING)
        self.journal = DraftJournal(self.run_dir)
        self.move(TARGET_PICK)
        return targets.list_targets(self.browser)

    def choose(self, target_id):
        """Take ownership of one tab by its stable target id, whatever it currently shows."""
        self.page = targets.page_for_target(self.browser, target_id)
        self.target = targets.describe(self.page)
        self.journal.note(target=self.target)
        self.move(ARMING)
        return self.target

    def reacquire(self):
        """Re-find the owned tab. Navigation and SPA routing never change its target id."""
        self.page = targets.page_for_target(self.browser, self.target["target_id"])
        return self.page

    # --- arming ---------------------------------------------------------------------------
    def own(self, page, relation="root"):
        """Instrument one page and follow anything it opens into the same session."""
        if any(owned is page for owned in self.pages):
            return
        self.pages.append(page)
        self.network.attach(page)
        try:
            page.expose_binding("__smartopsProbeCapture",
                                lambda source, payload: self.pending.append({"payload": payload, "frame": source["frame"]}))
        except Exception as exc:
            self.output.put({"type": "log", "message": "Could not attach the probe to one page: " + str(exc)[:200]})
            return
        page.add_init_script(self._script)
        # The popup callback only records the new page. Calling Playwright from inside a Playwright
        # event callback silently fails, so ownership is taken on the session's own loop instead.
        page.on("popup", lambda child: self._new_pages.append(child))
        # A frame that appears later — inserted by the page, navigated, or reloaded — must adopt the
        # session's current mode, not the probe's default. Otherwise a dynamic iframe could load in
        # radar mode mid-recording and swallow a real click instead of letting the page act on it.
        page.on("frameattached", self.adopt)
        page.on("framenavigated", self.adopt)
        self.stamp(page)
        self.install(page)
        self.output.put({"type": "log", "message": f"Recording {relation} page: {targets.short_location(page.url) or 'about:blank'}"})

    def adopt_new(self):
        """Take ownership of any page opened by a page we already own. Runs on the session's loop."""
        taken = 0
        while self._new_pages:
            child = self._new_pages.pop(0)
            if not any(owned is child for owned in self.pages):
                self.own(child, "popup")
                taken += 1
        return taken

    def adopt(self, frame):
        """Push the session's mode into one frame. Called for every frame that appears."""
        try:
            frame.evaluate(self._script)
            frame.evaluate("mode => { window.__smartopsProbeMode = mode; }", self.mode)
        except Exception:
            pass  # a frame mid-navigation or cross-origin simply keeps the safe 'off' default

    def stamp(self, page):
        """Bake the current mode into future documents, so a new frame is never briefly wrong."""
        try:
            page.add_init_script(f"window.__smartopsProbeMode = {json.dumps(self.mode)};")
        except Exception:
            pass

    def install(self, page):
        """Put the probe into every frame of a page. A frame that refuses is skipped, never fatal."""
        installed = 0
        for frame in page.frames:
            try:
                frame.evaluate(self._script)
                frame.evaluate("mode => { window.__smartopsProbeMode = mode; }", self.mode)
                installed += 1
            except Exception:
                pass
        return installed

    def arm(self):
        self.own(self.page, "root")
        self.move(RECORDING)
        if self.armed_mode == "record":
            self.start_context()   # the radar points at elements; it has no automation to start
        self.native_start()
        return len(self.pages)

    def start_context(self):
        """The starting page is step one. It goes through the journal like every other step, so a
        recovered draft always knows where the automation begins."""
        parsed = urlsplit(self.page.url)
        step = {"action": "navigate",
                "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                "label": "Open starting page (review URL parameters)",
                "captured": fp.now(), "origin": "start_context"}
        if self.journal:
            self.journal.append(step)
        self.output.put({"type": "recorded", "index": 0, "step": step})
        return step

    def finalize(self, state=SAVED):
        """Close the draft only once the workflow really is on disk. A failed save keeps it open."""
        if self.state == REVIEW:
            self.move(state)
        elif self.journal:
            self.journal.note(state=state)
        return self.state

    def discard(self):
        return self.finalize(DISCARDED)

    # --- recording ------------------------------------------------------------------------
    def native_start(self):
        """Attend to Windows-native clicks through the same session, when the platform allows it.

        v2's DesktopInputRecorder is the backend. It refuses to start off Windows, or without its
        input hooks, so this returns False there rather than pretending native capture is running.
        """
        if self.armed_mode != "record" or self.native is not None:
            return False
        from .desktop_discovery import DesktopInputRecorder
        self.native = DesktopInputRecorder(self.native_step, self.stop_event, self.run_dir / "discovery")
        self.native_running = bool(self.native.start())
        self.output.put({"type": "log", "message": "Windows-native clicks are being recorded too."
                         if self.native_running else "Windows-native capture is unavailable on this runtime; browser recording continues."})
        return self.native_running

    def native_step(self, step):
        """One native interaction, enriched by the same DiscoveryManager as every browser event."""
        payload = {"kind": "action", "action": step.get("action", "desktop_click"),
                   "screen": {"x": step.get("x"), "y": step.get("y")} if step.get("x") is not None else None,
                   "label": step.get("label", ""), "source": "desktop"}
        if step.get("value") is not None:
            payload["value"] = step["value"]
        self.pending.append({"payload": payload, "frame": None, "native": True})

    def mark_browser(self, payload):
        """Tell the native hook a click was already captured in the browser, so it is not counted twice."""
        if self.native is None:
            return
        point = payload.get("screen") or {}
        if point.get("x") is not None and point.get("y") is not None:
            self.native.mark_browser_event(point["x"], point["y"])
        if payload.get("value") and payload.get("action") == "press":
            self.native.mark_browser_key(payload["value"])

    def set_mode(self, mode):
        """The session is the single authority on mode, for frames that exist and frames to come."""
        if mode == self.mode:
            return
        self.mode = mode
        for page in self.pages:
            if page.is_closed():
                continue
            self.stamp(page)
            for frame in page.frames:
                try:
                    frame.evaluate("mode => { window.__smartopsProbeMode = mode; }", mode)
                except Exception:
                    pass

    def pause(self):
        self.move(PAUSED)
        self.set_mode("off")

    def resume(self):
        self.move(RECORDING)
        self.set_mode(self.armed_mode)

    def drain(self):
        """Turn queued interactions into fingerprinted steps. One failure never stops the rest."""
        produced = []
        while self.pending:
            queued = self.pending.pop(0) or {}
            payload, frame = queued.get("payload") or {}, queued.get("frame")
            if not queued.get("native"):
                self.mark_browser(payload)   # one physical click must never become two steps
            page = self.page_of(frame)
            self.count += 1
            payload = {**payload, "network_journal": self.network}
            try:
                result = self.manager.discover_element(payload, page=page, run_dir=self.run_dir,
                                                       index=self.count, frame=frame)
            except Exception as exc:
                # An interaction the user really performed is never dropped because enrichment
                # failed. It is recorded with an empty fingerprint and an explicit reason.
                self.output.put({"type": "log", "message": "Discovery failed for one interaction: " + str(exc)[:200]})
                result = fp.normalize({"layers": {key: fp.observation(key, fp.FAILED, "Discovery failed: " + str(exc)[:200])
                                                  for key in fp.LAYER_KEYS}})
            finally:
                self.forget(frame, payload.get("token"))
            step = self.step_for(payload, result)
            if self.journal:
                self.journal.append(step)
            produced.append(step)
            self.output.put({"type": "element" if payload.get("kind") == "point" else "recorded",
                             "index": self.count, "step": step, "fingerprint": result})
        return produced

    def step_for(self, payload, result):
        """A recorded step carries BOTH replayable fields and the fingerprint. Never one or the other."""
        web = result["layers"]["web"]["data"] or {}
        # Prefer the highest-ranked locator candidate over the structural path.
        usable = [c for c in (web.get("candidates") or []) if c.get("kind") != "role_name"]
        selector = usable[0]["value"] if usable else web.get("selector", "")
        nexacro = result["layers"]["nexacro"]["data"] or {}
        best = fp.rank(result)
        step = {
            "action": payload.get("action") or "point",
            "selector": selector,
            "label": (nexacro.get("name") or web.get("accessible_name") or web.get("text")
                      or web.get("id") or web.get("tag") or "element")[:120],
            "fingerprint": result,
            "detected_by": [key for key, _ in best],
            "best_layer": best[0][0] if best else "",
            "captured": fp.now(),
        }
        for key in ("value", "checked", "secure"):
            if key in payload:
                step[key] = payload[key]
        if payload.get("action") == "secure_input" or web.get("sensitive"):
            step.pop("value", None)
            step["secure"] = True
            step["label"] = "Manual secure input required"
        frame_data = result["layers"]["frame"]["data"] or {}
        if not frame_data.get("top_level"):
            chain = [hop for hop in (frame_data.get("chain") or []) if hop.get("selector")]
            if chain:
                # frame is the last hop, for today's single-level replay. frame_chain is the whole
                # route, so a future replayer can walk outer -> inner without re-recording.
                step["frame"] = chain[-1]["selector"]
                step["frame_chain"] = [hop["selector"] for hop in chain]
                step["frame_depth"] = frame_data.get("depth", len(chain))
        return step

    def page_of(self, frame):
        """Which owned page a frame belongs to, so popups are enriched against their own page."""
        for page in self.pages:
            try:
                if not page.is_closed() and frame in page.frames:
                    return page
            except Exception:
                continue
        return self.page

    def forget(self, frame, token):
        """Remove the probe marker from the frame it was set in, not just the main document."""
        if not token:
            return
        for target in ([frame] if frame is not None else []) + [f for page in self.pages if not page.is_closed() for f in page.frames]:
            try:
                target.evaluate("token => { const el = document.querySelector('[data-smartops-probe=\"' + token + '\"]'); if (el) el.removeAttribute('data-smartops-probe'); }", token)
            except Exception:
                continue

    def alive(self):
        return any(not page.is_closed() for page in self.pages)

    def refresh(self):
        """Re-assert the probe and the mode across every owned page.

        A document replaced by document.write, or a frame that appeared between events, would
        otherwise be left without listeners. The probe early-returns when it is already installed,
        so this is cheap to repeat.
        """
        for page in self.pages:
            if not page.is_closed():
                self.install(page)

    def tick(self):
        """One turn of the recording loop: adopt new tabs, re-assert ownership on cadence, collect."""
        self._ticks += 1
        self.adopt_new()
        if self._ticks % self.refresh_every == 0:
            self.refresh()
        return self.drain()

    def pump(self, seconds=1.0, interval=0.12):
        """Run the loop for a bounded time. This is exactly what run() does, without a stop event,
        so a test exercises the session's own automatic behaviour rather than reaching inside it."""
        captured = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            captured.extend(self.tick())
            try:
                self.page.wait_for_timeout(int(interval * 1000))
            except Exception:
                time.sleep(interval)
        return captured

    def run(self, stop, interval=0.12):
        """The recording loop. Returns the captured steps whatever ends it."""
        captured = []
        try:
            while not stop.is_set():
                if not self.alive():
                    self.output.put({"type": "log", "message": "Every recorded tab was closed. Captured steps are kept."})
                    break
                captured.extend(self.tick())
                try:
                    self.page.wait_for_timeout(int(interval * 1000))
                except Exception:
                    time.sleep(interval)
        finally:
            self.adopt_new()
            captured.extend(self.drain())
            self.finish()
        return captured

    def finish(self):
        self.stop_event.set()
        if self.native is not None:
            try:
                self.native.stop()
            except Exception:
                pass
            self.native = None
        self.set_mode("off")
        for page in self.pages:
            if page.is_closed():
                continue
            for frame in page.frames:
                try:
                    frame.evaluate("window.__smartopsProbeCapture = undefined;")
                except Exception:
                    pass
        if self.state in TRANSITIONS and STOPPING in TRANSITIONS[self.state]:
            self.move(STOPPING)
        if self.state == STOPPING:
            self.move(REVIEW)
