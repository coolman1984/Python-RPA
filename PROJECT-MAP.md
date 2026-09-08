# Project map — Universal Discovery & Recording Layer

> Reconciled on `fix/recorder-ux-session` from three diverging architectures. The contract and the
> manager come from `claude/improve-code-first-s2uqdb`; the recording coverage, the Windows probe,
> the network journal and the locator-based visual capture come from
> `feature/universal-recorder-discovery-v2`. `main`'s `Evidence`/`ElementFingerprint` dataclasses
> and `recorder.js` are retired, not left running beside the new ones.
> See `docs/RECORDER_UX_REPAIR_PLAN.md` for the diagnosis and the staging.

One user action produces one `ElementFingerprint`. This map shows every detector, who calls
it, where its output goes, and how far each one has actually been proven.

## Flow

```
 user clicks an element in the tab the session owns (by CDP target id, never by URL)
              │
              │  probe.js swallows mousedown/mouseup/click/dblclick/contextmenu/submit
              │  so the page never acts on a click that was only meant to point
              ▼
 probe.js  (injected into every frame of every page the session owns)
   mode 'radar'  : swallows the click, reports a pointed-at element
   mode 'record' : lets the click through, reports a recorded action
   the SAME evidence either way: web · frame · nexacro · anchor · relative · keyboard
   tags the element with data-smartops-probe=<token> so the worker can find the same node
              │
              │  window.__smartopsInspectCapture(payload)   ── Playwright binding
              ▼
 RecordingSession  (smartops_desktop/session.py)
   owns the target, follows its popups, holds the network journal, runs the state machine
   the binding ONLY queues; no Playwright call is ever made inside a Playwright callback
              │
              ▼
 session.drain  → DiscoveryManager.discover_element(payload, page, run_dir, index, frame)
              │
              ▼
 DiscoveryManager  (smartops_desktop/discovery.py)
   runs all 11 detectors, each wrapped in its own try/except
   pass 1: every detector
   pass 2: ocr + vision re-run once the visual layer has produced a picture for them to read
              │
              ▼
 fingerprint.normalize  (smartops_desktop/fingerprint.py)
   fills every missing layer, bounds all page-supplied data, fixes the schema
              │
              ├─────────────► draft journal (append-safe, survives a crash):
              │                 runs/<run-id>/draft.json    session, target, state, count
              │                 runs/<run-id>/draft.jsonl   one recorded step per line
              │                 runs/<run-id>/element-N.png, element-N-context.png
              │
              └─► output queue  {"type": "element", ...}
                        │
                        ▼
                  gui.handle_event → gui.add_element → gui.render_radar
                  (Element radar page: one row per layer, verdict, confidence, proof)
```

## Detectors

Every detector is an independent adapter with one method, `observe(context)`. The manager
never lets one detector's failure reach another, or the recording.

| # | Layer | Class (`discovery.py`) | Evidence from | Output goes to | Maturity |
| --- | --- | --- | --- | --- | --- |
| 1 | web | `WebDetector` | probe.js `webLayer` | fingerprint `layers.web` | VERIFIED |
| 2 | frame | `FrameDetector` | probe.js `frameLayer` | `layers.frame` (context, never ranked) | VERIFIED |
| 3 | nexacro | `NexacroDetector` | probe.js `nexacroLayer` | `layers.nexacro` | IMPLEMENTED_UNVERIFIED |
| 4 | accessibility | `AccessibilityDetector` | CDP `Accessibility.getPartialAXTree` | `layers.accessibility` | VERIFIED |
| 5 | windows | `WindowsUiaDetector` | `desktop_discovery.probe_windows_at` (v2) | `layers.windows` | IMPLEMENTED_UNVERIFIED |
| 6 | anchor | `AnchorDetector` | probe.js `anchorLayer` | `layers.anchor` | VERIFIED |
| 7 | visual | `VisualDetector` | Playwright `page.screenshot` | `layers.visual` + 2 PNGs | VERIFIED |
| 8 | relative | `RelativeDetector` | probe.js `relativeLayer` | `layers.relative` | VERIFIED |
| 9 | keyboard | `KeyboardDetector` | probe.js `keyboardLayer` | `layers.keyboard` | VERIFIED |
| 10 | ocr | `OcrDetector` | `pytesseract` + the visual layer's PNG | `layers.ocr` | IMPLEMENTED_UNVERIFIED |
| 11 | vision | `VisionDetector` | `opencv-python` + the visual layer's PNG | `layers.vision` | IMPLEMENTED_UNVERIFIED |
| 12 | network | `NetworkDetector` | `NetworkJournal` (ported from v2) | `layers.network` (context, never ranked) | VERIFIED |

Detector order is declared once, in `fingerprint.LAYERS`. `discovery.DEFAULT_DETECTORS`
instantiates them in the same order.

## Two judgements, deliberately kept apart

**status** — what happened to this observation of this element.

| | | |
| --- | --- | --- |
| ✅ `found` | the detector identified the element | carries a confidence |
| ❌ `missing` | the detector ran and could not identify it | confidence 0.0 |
| ⚪ `unavailable` | the detector could not run here (wrong platform, engine absent) | confidence 0.0 |
| ⚠️ `failed` | the detector raised; the other ten carried on | confidence 0.0 |

**maturity** — how far the detector itself has been proven, regardless of today's result.

| | |
| --- | --- |
| `VERIFIED` | exercised against the real thing (a real Chromium over CDP) |
| `IMPLEMENTED_UNVERIFIED` | the code exists and is wired in, but has never met the real thing |
| `NOT_IMPLEMENTED` | no detection logic at all |

A detector returning ✅ with 0.90 confidence still reports `IMPLEMENTED_UNVERIFIED` if it has
never met the real system. Confidence never launders an unproven detector into a trusted one.
`fingerprint.score()` only counts `VERIFIED` layers towards `confident`.

## Confidence

Confidence answers one question: **if this screen changes, how likely is this evidence to
still point at the same element?** It is computed in `discovery.py`, in Python, so it is
testable without a browser — `probe.js` gathers evidence and never scores it.

| Evidence | Score | Why |
| --- | --- | --- |
| stable unique id | 0.95 | survives layout and content changes |
| id that looks generated (`ctl00_x_1739284`) | 0.72 | rebuilt per session or per build |
| `data-testid` | 0.93 | put there to be depended on |
| unique `name` | 0.88 | stable, but shared across forms more often than ids |
| unique non-positional selector | 0.80 | attribute-based |
| positional selector (`div:nth-of-type(3) > button`) | 0.62 | breaks when a row is inserted |
| visible text only | 0.55 | breaks on translation or wording change |
| any of the above, but not unique on the page | capped 0.50 | matches more than one element |
| Nexacro live component + rendered path | 0.90 | two independent routes agree |
| Nexacro path + form | 0.85 | one route, well structured |
| Nexacro path only | 0.65 | no form context |
| Nexacro grid cell | capped 0.75 | row/column follow the data, not the screen |
| accessibility role + name | 0.82 | published contract, changes rarely |
| accessibility role only | 0.45 | thousands of elements share a role |
| anchor bound by `label[for=…]` | 0.90 | an explicit pointer, not proximity |
| anchor by proximity | 0.78 → 0.35 | decays with distance |
| picture of the element | 0.65 | 0.35 if under 8 px, 0.45 if it covers most of the view |
| position inside a real container | 0.52 | 0.35 inside bare `<body>` |
| keyboard tab stop | 0.62 | 0.45 in forms over 30 stops; +0.15 with an access key |
| Windows UIA automation id | 0.90 | the desktop equivalent of a stable id |

**Frame and network context are never ranked.** It says *where* to look, not *which* element, so
`fingerprint.rank()` excludes both and `score()["context"]` reports them separately.

## What is deliberately never captured

Field contents. `probe.js` reads no `value` from any input, and blanks the text of anything
matching `password|passwd|secret|token|otp|credit|card`, while still recording that the
element exists and is sensitive. A fingerprint can be shared without carrying business data.

## Where the tests live

| File | Covers | Needs a browser |
| --- | --- | --- |
| `tests/test_fingerprint.py` | schema, bounding, status/maturity vocabulary, ranking, scoring | no |
| `tests/test_discovery.py` | failure isolation, confidence ranking, evidence merging, adapter honesty | no |
| `tests/test_radar.py` | the whole path against a real Chromium over CDP | yes, `SMARTOPS_TEST_CDP` |
| `tests/test_gui.py` | the radar card, the element list, tab guard, **step edits preserve fingerprints** | no |
| `tests/test_session.py` | state machine, draft journal, frames, popups, pause/resume, isolation, stability | mostly |
| `tests/test_targets.py` | duplicate URLs, navigation, SPA routing, closed targets | yes |
| `tests/test_core.py` | workflows, Excel validation, replay, cancel — unchanged by this work | no |

`tests/test_radar.py` skips itself unless `SMARTOPS_TEST_CDP` names a debugging endpoint, so
`build.ps1` stays browser-free.

## Adding a detector later

1. Add its row to `fingerprint.LAYERS` with an honest maturity.
2. Add a `Detector` subclass to `discovery.py` and put it in `DEFAULT_DETECTORS`.
3. If it observes from inside the page, add its layer to `probe.js` and subclass
   `PageEvidenceDetector` so the scoring stays in Python.
4. Add tests to `tests/test_discovery.py`. Nothing else changes: the manager, the storage,
   the card and the diagnostic all read the layer list.
