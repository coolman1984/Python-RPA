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
import queue
import re
import threading
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import uuid4

from . import fingerprint as fp
from . import targets
from .discovery import DiscoveryManager, NetworkJournal, rank_locators

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


RECORDING_KIND, RADAR_KIND = "recording", "radar"

# --- worker -> GUI event contract -----------------------------------------------------------
# Every event name carries exactly ONE meaning, and no consumer may assume a field that has not
# arrived yet. A step is announced when it is recorded; its evidence arrives as a separate,
# separately-named event against the same step_id.
STEP_ADDED = "step_added"        # {stream, index, step_id, step}      step has NO fingerprint yet
STEP_ENRICHED = "step_enriched"  # {stream, step_id, patch, fingerprint}
SESSION_STATE = "session_state"  # {state}
CAPACITY = "capacity"            # {limit, message}
EVENT_SCHEMA = {
    STEP_ADDED: ("stream", "index", "step_id", "step"),
    STEP_ENRICHED: ("stream", "step_id", "patch", "fingerprint"),
    SESSION_STATE: ("state",),
    CAPACITY: ("limit", "message"),
}
ENRICH_BACKLOG = 400      # bounded: a slow detector may never grow memory without limit
ENRICH_PER_TICK = 3       # bounded: enrichment shares the loop, it never monopolises it
MAX_STEPS = 1000          # THE authority for capacity. No other module may hold its own limit.
PENDING, COMPLETE, ENRICH_FAILED, SKIPPED_CAPACITY = "pending", "complete", "failed", "skipped_capacity"


class DraftJournal:
    """Append-only operation log for one session.

    Nothing is ever rewritten in place. A step arrives as ADD the instant the user performs it —
    before any slow discovery runs — and its evidence lands later as a separate ENRICH operation.
    A crash therefore always leaves the user's actions on disk, even if enrichment never happened.
    """

    ADD, ENRICH, UNDO, EDIT = "add", "enrich", "undo", "edit"

    def __init__(self, folder, session_id=None, target=None, kind=RECORDING_KIND):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or str(uuid4())
        self.kind = kind
        self.header_path = self.folder / "draft.json"
        self.log_path = self.folder / "draft.jsonl"
        self.header = {"session_id": self.session_id, "kind": kind, "created": fp.now(),
                       "state": PREPARING, "target": target or {}, "steps": 0}
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

    def _write(self, operation):
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(operation, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def add(self, step_id, step):
        self._write({"op": self.ADD, "step_id": step_id, "at": fp.now(), "step": step})
        self.header["steps"] = self.header.get("steps", 0) + 1
        self._write_header()
        return step_id

    def enrich(self, step_id, patch):
        self._write({"op": self.ENRICH, "step_id": step_id, "at": fp.now(), "patch": patch})

    def undo(self, step_id):
        self._write({"op": self.UNDO, "step_id": step_id, "at": fp.now()})
        self.header["steps"] = max(0, self.header.get("steps", 0) - 1)
        self._write_header()

    def edit(self, step_id, changes):
        self._write({"op": self.EDIT, "step_id": step_id, "at": fp.now(), "changes": changes})

    def operations(self):
        if not self.log_path.exists():
            return []
        found = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                break  # a crash mid-write truncates here; everything before it is intact
        return found

    def steps(self):
        """Materialise the current timeline from the operation log."""
        order, byid = [], {}
        for operation in self.operations():
            step_id, kind = operation.get("step_id"), operation.get("op")
            if kind == self.ADD:
                byid[step_id] = dict(operation.get("step") or {}, step_id=step_id)
                order.append(step_id)
            elif step_id not in byid:
                continue
            elif kind == self.ENRICH:
                byid[step_id].update(operation.get("patch") or {})
            elif kind == self.EDIT:
                byid[step_id].update(operation.get("changes") or {})
            elif kind == self.UNDO:
                byid.pop(step_id, None)
                order = [x for x in order if x != step_id]
        return [byid[step_id] for step_id in order if step_id in byid]

    @classmethod
    def timeline(cls, folder):
        """The authoritative recorded timeline, materialised from the log on disk.

        This is what a save must use. A GUI copy assembled from live events is a mirror and may
        have missed, reordered or never applied an enrichment; the log cannot.
        """
        reader = cls.__new__(cls)
        reader.log_path = Path(folder) / "draft.jsonl"
        reader.ADD, reader.ENRICH, reader.UNDO, reader.EDIT = cls.ADD, cls.ENRICH, cls.UNDO, cls.EDIT
        return reader.steps()

    @classmethod
    def unfinished(cls, root, kind=RECORDING_KIND):
        """Drafts a crash left behind, newest first. Radar diagnostics are never automations."""
        found = []
        for header_path in sorted(Path(root).glob("*/draft.json")):
            try:
                header = json.loads(header_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if header.get("state") in CLOSED:
                continue
            if kind is not None and header.get("kind", RECORDING_KIND) != kind:
                continue
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
        self.stream = RECORDING_KIND if mode == "record" else RADAR_KIND
        self.state = IDLE
        self.target = {}
        self.page = None
        self.pages = []          # the owned target plus every popup it opened
        # Native input arrives on listener threads, browser input on the Playwright thread.
        self.pending = queue.SimpleQueue()
        self.enrich_backlog = deque(maxlen=ENRICH_BACKLOG)
        self.skipped_enrichment = 0
        self.network = NetworkJournal()
        self.journal = None
        self.native = None
        self.native_running = False
        self.stop_event = threading.Event()
        self.count = 0
        self._ticks = 0
        self._new_pages = queue.SimpleQueue()
        self.logical = {}          # id(page) -> logical page identity, the replay contract
        self.at_capacity = False
        self.refresh_every = 8   # ~1s at the default interval
        self._script = Path(__file__).with_name("probe.js").read_text(encoding="utf-8")

    # --- state machine --------------------------------------------------------------------
    def move(self, state):
        if state not in TRANSITIONS[self.state]:
            raise SessionError(f"Cannot go from {self.state} to {state}.")
        self.state = state
        if self.journal:
            self.journal.note(state=state)
        self.output.put({"type": SESSION_STATE, "state": state})
        return state

    # --- preparation ----------------------------------------------------------------------
    def prepare(self):
        self.move(PREPARING)
        # A radar session is a diagnostic, not an unfinished automation, so its journal is tagged
        # and recovery never offers it back as lost work.
        self.journal = DraftJournal(self.run_dir, kind=RECORDING_KIND if self.armed_mode == "record" else RADAR_KIND)
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
        opener = self.logical.get(id(self.page), {}).get("logical_page_id") if self.pages else ""
        self.pages.append(page)
        self.logical[id(page)] = {
            "logical_page_id": "page-1" if relation == "root" else f"popup-{len(self.pages) - 1}",
            "relation": relation, "opener_page_id": "" if relation == "root" else (opener or "page-1"),
            "captured_target_id": self.target.get("target_id", ""),
            "title": (self.target.get("title", "") if relation == "root" else "")[:160],
        }
        self.network.attach(page)
        try:
            page.expose_binding("__smartopsProbeCapture",
                                lambda source, payload: self.receive(payload, source["frame"]))
        except Exception as exc:
            self.output.put({"type": "log", "message": "Could not attach the probe to one page: " + str(exc)[:200]})
            return
        page.add_init_script(self._script)
        # The popup callback only records the new page. Calling Playwright from inside a Playwright
        # event callback silently fails, so ownership is taken on the session's own loop instead.
        page.on("popup", lambda child: self._new_pages.put(child))
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
        while True:
            try:
                child = self._new_pages.get_nowait()
            except queue.Empty:
                return taken
            if not any(owned is child for owned in self.pages):
                self.own(child, "popup")
                taken += 1

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

    # A parameter whose NAME matches this never has its value written anywhere on disk.
    SENSITIVE_PARAM = re.compile(r"token|session|sid|auth|secret|key|otp|code|ticket|pass|pwd|credential|bearer|jwt|signature|sig", re.I)

    @classmethod
    def classify_start_url(cls, raw):
        """Four separate things, so a routing parameter is never confused with a credential.

        display        what a person is shown
        persisted      what goes into the draft and the saved workflow
        routing        parameter values the screen genuinely needs, minus anything secret-looking
        sensitive      names only — their values are dropped and never written down
        """
        parsed = urlsplit(raw or "")
        display = urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
        routing, sensitive = {}, []
        for name, values in parse_qs(parsed.query, keep_blank_values=True).items():
            if cls.SENSITIVE_PARAM.search(name):
                sensitive.append(name)
            else:
                routing[name] = values[0][:200]
        return {"display": display, "persisted": display, "routing": routing,
                "sensitive": sorted(sensitive), "had_fragment": bool(parsed.fragment),
                "requires_review": bool(routing or sensitive or parsed.fragment)}

    def start_context(self):
        """The starting page is step one. It goes through the journal like every other step, so a
        recovered draft always knows where the automation begins."""
        policy = self.classify_start_url(self.page.url)
        self.count += 1
        step_id = f"s{self.count:04d}"
        note = ""
        if policy["sensitive"]:
            note = " · a credential-like parameter was removed: " + ", ".join(policy["sensitive"])
        elif policy["routing"]:
            note = " · its address carries parameters, review before sharing"
        step = {"step_id": step_id, "action": "navigate", "url": policy["persisted"],
                "start_routing": policy["routing"], "removed_parameters": policy["sensitive"],
                "requires_review": policy["requires_review"],
                "label": "Open starting page" + note,
                "captured": fp.now(), "origin": "start_context", "enriched": True,
                "enrichment_status": COMPLETE, "page_context": self.page_context(None)}
        if self.journal:
            self.journal.add(step_id, step)
        self.output.put({"type": STEP_ADDED, "stream": self.stream, "index": self.count,
                         "step_id": step_id, "step": step})
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
        """One raw native interaction. Discovery happens later, once, in the manager."""
        payload = {"kind": "action", "action": step.get("action", "desktop_click"),
                   "label": step.get("label", ""), "source": "desktop",
                   "native_sequence": step.get("sequence"), "button": step.get("button", "")}
        if step.get("x") is not None:
            payload["screen"] = {"x": step["x"], "y": step["y"]}
        if step.get("value") is not None:
            payload["value"] = step["value"]
            # A desktop key press is keyboard evidence in its own right; it must not be lost
            # merely because no browser element was involved.
            payload["layers"] = {"keyboard": {"status": fp.FOUND, "detail": "desktop key " + str(step["value"]),
                                              "data": {"key": step["value"], "reachable": True,
                                                       "focus_order": 0, "focus_total": 1, "source": "desktop"}}}
        self.receive(payload, None, native=True)

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

    def receive(self, payload, frame, native=False):
        """Callback side. Enqueue only: mark the browser event immediately so the native hook
        cannot repeat it, then get out. No Playwright call may happen here."""
        payload = payload if isinstance(payload, dict) else {}
        if not native:
            self.mark_browser(payload)
        self.pending.put({"payload": payload, "frame": frame, "native": native,
                          "received": time.monotonic()})

    def drain(self):
        """Record every queued interaction immediately, then queue its enrichment separately.

        The user's action becomes durable before any screenshot, CDP call or UIA lookup runs, so a
        slow detector can never delay recording or lose a step.
        """
        produced = []
        while True:
            try:
                queued = self.pending.get_nowait()
            except queue.Empty:
                break
            payload, frame = queued.get("payload") or {}, queued.get("frame")
            if self.count >= MAX_STEPS:
                if not self.at_capacity:
                    self.at_capacity = True
                    self.output.put({"type": CAPACITY, "limit": MAX_STEPS,
                                     "message": f"Recording reached the {MAX_STEPS:,} step limit. Stop to keep what was captured."})
                continue
            self.count += 1
            step_id = f"s{self.count:04d}"
            step = self.raw_step(step_id, payload, queued)
            if self.journal:
                self.journal.add(step_id, step)
            self.output.put({"type": STEP_ADDED, "stream": self.stream, "index": self.count,
                             "step_id": step_id, "step": step})
            if len(self.enrich_backlog) == self.enrich_backlog.maxlen:
                # The action itself is safe on disk. Its evidence is not going to be gathered, and
                # that must be stated on the step rather than surfacing later as "selector required".
                dropped = self.enrich_backlog[0]
                self.skipped_enrichment += 1
                if self.journal:
                    self.journal.enrich(dropped["step_id"], {"enrichment_status": SKIPPED_CAPACITY,
                                                             "enrichment_reason": "the enrichment backlog was full"})
                self.output.put({"type": STEP_ENRICHED, "stream": self.stream, "step_id": dropped["step_id"],
                                 "patch": {"enrichment_status": SKIPPED_CAPACITY}, "fingerprint": None})
            self.enrich_backlog.append({"step_id": step_id, "payload": payload, "frame": frame})
            produced.append(step)
        return produced

    def enrich(self, budget=ENRICH_PER_TICK):
        """Run discovery for steps already on disk. Bounded, so the loop stays responsive."""
        done = []
        for _ in range(budget):
            if not self.enrich_backlog:
                break
            item = self.enrich_backlog.popleft()
            payload, frame = item["payload"], item["frame"]
            try:
                result = self.manager.discover_element({**payload, "network_journal": self.network},
                                                       page=self.page_of(frame), run_dir=self.run_dir,
                                                       index=int(item["step_id"][1:]), frame=frame)
            except Exception as exc:
                result = fp.normalize({"layers": {key: fp.observation(key, fp.FAILED, "Discovery failed: " + str(exc)[:200])
                                                 for key in fp.LAYER_KEYS}})
            finally:
                self.forget(frame, payload.get("token"))
            patch = self.evidence_patch(result)
            if self.journal:
                self.journal.enrich(item["step_id"], patch)
            self.output.put({"type": STEP_ENRICHED, "stream": self.stream, "step_id": item["step_id"],
                             "patch": patch, "fingerprint": result})
            done.append(item["step_id"])
        return done

    def raw_step(self, step_id, payload, queued):
        """What the user did, known instantly from the interaction itself. No discovery required."""
        layers = payload.get("layers") or {}
        web = ((layers.get("web") or {}).get("data")) or {}
        step = {"step_id": step_id, "action": payload.get("action") or "point",
                "label": (payload.get("label") or web.get("accessible_name") or web.get("text")
                          or web.get("id") or web.get("tag") or "element")[:120],
                "captured": fp.now(), "received": queued.get("received"),
                "source": "desktop" if queued.get("native") else "browser",
                "page_context": self.page_context(queued.get("frame")),
                "enriched": False, "enrichment_status": PENDING}
        for key in ("value", "checked", "secure"):
            if key in payload:
                step[key] = payload[key]
        if payload.get("action") == "secure_input" or web.get("sensitive"):
            step.pop("value", None)
            step["secure"] = True
            step["label"] = "Manual secure input required"
        if payload.get("capture_error"):
            step["capture_error"] = payload["capture_error"]
        return step

    def evidence_patch(self, result):
        """Everything discovery adds to a step once it has run."""
        ranked = rank_locators(result)
        best = fp.rank(result)
        patch = {"fingerprint": result, "enriched": True, "enrichment_status": COMPLETE,
                 "locators": ranked, "detected_by": [key for key, _ in best],
                 "best_layer": best[0][0] if best else ""}
        if ranked:
            patch["selector"] = ranked[0]["value"]
            patch["primary_locator"] = ranked[0]
            patch["fallback_locator"] = ranked[1] if len(ranked) > 1 else None
        nexacro = result["layers"]["nexacro"]["data"] or {}
        web = result["layers"]["web"]["data"] or {}
        better = nexacro.get("name") or web.get("accessible_name") or web.get("text")
        if better:
            patch["label"] = better[:120]
        frame_data = result["layers"]["frame"]["data"] or {}
        if not frame_data.get("top_level"):
            chain = [hop for hop in (frame_data.get("chain") or []) if hop.get("selector")]
            if chain:
                patch["frame"] = chain[-1]["selector"]
                patch["frame_chain"] = [hop["selector"] for hop in chain]
                patch["frame_depth"] = frame_data.get("depth", len(chain))
        return patch

    def page_context(self, frame):
        """The logical page a step happened on. Replay needs this, not a CDP target id."""
        page = self.page_of(frame)
        logical = self.logical.get(id(page)) if page is not None else None
        return dict(logical or {"logical_page_id": "page-1", "relation": "root"})

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
        """One turn: adopt new tabs, re-assert ownership on cadence, record, then enrich.

        Recording always runs before enrichment, so a backlog of screenshots can never delay a
        step reaching the journal or the screen.
        """
        self._ticks += 1
        self.adopt_new()
        if self._ticks % self.refresh_every == 0:
            self.refresh()
        produced = self.drain()
        self.enrich()
        return produced

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
            while self.enrich_backlog:      # evidence for steps already safely on disk
                self.enrich(budget=len(self.enrich_backlog))
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
