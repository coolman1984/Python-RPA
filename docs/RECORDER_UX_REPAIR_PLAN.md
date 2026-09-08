# Recorder UX & recording-session repair plan

Branch: `fix/recorder-ux-session`, cut from `main` at `5888788`.

This document is the gate before code. It records what was inspected, what is actually broken,
which architecture wins, and what gets reused from where.

---

## 1. Repository state, verified not assumed

```
main                                    5888788   4 commits
claude/improve-code-first-s2uqdb        a0737b0   3 ahead / 3 behind main   (base ca5751c)
feature/universal-recorder-discovery-v2 c12faa9   15 ahead / 0 behind main  (clean fast-forward)
```

An earlier session in the Claude branch reported "there was no `discovery.py` to reuse". That was
true of that branch's base (`ca5751c`) and false of current `main`, which gained `discovery.py` in
`6fd5274`. The container had not fetched the other branches, so they were invisible. Corrected: the
branches exist, the claim was wrong about `main`, and nothing may be merged on that assumption.

### Three architectures currently pulling in three directions

| | `main` + `v2` | `claude/improve-code-first` |
| --- | --- | --- |
| contract | `Evidence` / `ElementFingerprint` dataclasses | `fingerprint.py` layer table |
| per-observation state | `available: bool` | `found` / `missing` / `unavailable` / `failed` |
| detector maturity | in prose in the docs only | `VERIFIED` / `IMPLEMENTED_UNVERIFIED` / `NOT_IMPLEMENTED`, in code |
| confidence | hardcoded in JavaScript | scored in Python, unit-tested without a browser |
| failure isolation | try/except per block | one manager isolating every adapter |
| recording coverage | frames, popups, network, Windows UIA, canvas, live Nexacro | **radar only — the recorder is untouched** |

### Decision

**Canonical = the Claude branch contract. Coverage = v2.**

The Claude branch has the better discipline and the worse reach; v2 has the better reach and the
weaker discipline. Neither survives whole. `Evidence`/`ElementFingerprint` and `normalize_capture`
are retired rather than left beside the new contract, so there is no parallel dead architecture.

---

## 2. Current user journey and where it fails

```
open app → pick/create a workflow → name it → Settings → read about CDP
   → Connect Chrome → refresh tabs → pick a tab by URL → Record
   → confirm a dialog → work → Stop → a second workflow appears called "X · recording"
   → read selectors and JSON to find out what happened
```

| # | Failure | Where |
| --- | --- | --- |
| F1 | Recording cannot start without an existing, named workflow | `gui.start_worker` requires `self.current` |
| F2 | The user must understand CDP, endpoints and tabs before recording | Settings page, `Connect Chrome` |
| F3 | The tab is identified by its full URL | `worker.select_page(browser, url)` |
| F4 | Two tabs with the same URL are refused outright | same |
| F5 | Navigating after selection loses the tab | same |
| F6 | Connect and record are two separate browser connections | `mode="tabs"` then `mode="record"` |
| F7 | Everything outside the main frame is discarded | `record()`: `source["frame"] != page.main_frame` |
| F8 | New tabs are never followed | no `popup` handler on `main` |
| F9 | A confirmation dialog after the user already clicked Record | `gui.start_worker` |
| F10 | Stop creates a *second* workflow named "X · recording" | `gui.finish_worker` |
| F11 | Editing a recorded step rebuilds it from visible fields, dropping the fingerprint | `StepDialog.value()` |
| F12 | The whole recording lives in RAM until Stop | `gui.captured` list |
| F13 | The radar uses `DiscoveryManager`; the recorder uses none of it | `worker.record` vs `worker.inspect` |

---

## 3. Target user journey

```
open SmartOps → 🔴 Record New Automation → pick the target visually → 3…2…1
   → work normally → ⏹ Stop → review a plain-language timeline → name it → Save
```

No workflow first. No Settings first. No CDP. No selectors. Everything technical lives under
**Advanced**.

---

## 4. Recording state machine

```
IDLE ──▶ PREPARING ──▶ TARGET_PICK ──▶ ARMING ──▶ RECORDING ⇄ PAUSED
                │            │            │            │
                └────────────┴────────────┴────────────┴──▶ STOPPING ──▶ REVIEW ──▶ SAVED
                                                                 │
   any failure ─────────────────────────────────────────────────▶ back to a safe state,
                                                                    captured work retained
```

Every transition is explicit and testable. No transition may discard captured steps.

---

## 5. Browser target ownership

One long-lived session owns the target from `ARMING` to `SAVED`. The disposable
`tabs`-then-`record` handoff is removed from the recording path.

Identity is the **CDP target id**, never the URL:

```python
session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
```

| Requirement | How |
| --- | --- |
| duplicate URLs | different target ids |
| navigation after selection | target id is unchanged by navigation |
| SPA navigation | no document swap at all |
| popups / new tabs | `page.on("popup")` attaches the child to the same session |
| target closed | session moves to `STOPPING`, keeps the draft |

---

## 6. Discovery pipeline — one pipeline, two callers

```
user interaction (radar point, or recorded action)
        │
        ▼
   probe.js  (ONE probe, mode = radar | record, installed in every frame of every tracked page)
        │  radar mode  : swallows the click, emits evidence
        │  record mode : lets the click through, emits action + evidence
        ▼
   RecordingSession / radar loop           ← owns pages, frames, popups, network journal
        ▼
   DiscoveryManager.discover_element(...)  ← isolates all detectors
        ▼
   fingerprint.normalize(...)              ← one ElementFingerprint
        ▼
   Recorded Step  =  replayable action fields  +  fingerprint metadata
        ▼
   Draft journal on disk (append-safe)  →  Review  →  Workflow
```

`recorder.js` is deleted. The radar and the recorder read the same evidence from the same probe
through the same manager, which is the whole point of the exercise.

---

## 7. Draft / recovery model

Every accepted step is appended to `runs/<session-id>/draft.jsonl` as one JSON line, plus a
`draft.json` header holding session id, created time, target metadata and state. Append-only, so a
crash truncates at most the last line. On launch, an unfinished draft offers **Recover unfinished
recording**.

---

## 8. Files

| File | Fate |
| --- | --- |
| `smartops_desktop/fingerprint.py` | **new here**, ported from the Claude branch — canonical contract |
| `smartops_desktop/discovery.py` | **replaced** — Claude's manager, absorbing v2's layers |
| `smartops_desktop/desktop_discovery.py` | **ported from v2** — Windows UIA + native input |
| `smartops_desktop/targets.py` | **new** — stable CDP target identity and the picker's data |
| `smartops_desktop/session.py` | **new** — recording state machine, page/frame/popup ownership, draft journal |
| `smartops_desktop/probe.js` | **new** — one probe, both modes, merged from Claude's probe + v2's recorder |
| `smartops_desktop/recorder.js` | **deleted** — superseded |
| `smartops_desktop/worker.py` | rewritten around the session; no detection logic left in it |
| `smartops_desktop/gui.py` | radar page ported; record journey in stage 2 |
| `smartops_desktop/core.py` | step schema keeps `fingerprint`; unknown keys preserved |

### Reused from `feature/universal-recorder-discovery-v2`

- `desktop_discovery.py` whole: `probe_windows_at`, `DesktopInputRecorder`, sensitivity guards
- `NetworkJournal` — privacy-stripped network clues
- `frame_descriptor` / `_frame_element_descriptor` — frame path resolution
- popup instrumentation (`instrument` / `page.on("popup")`)
- `locator.screenshot()` visual capture + SHA-256 — **this is also the fix for the iframe screenshot bug**
- live-Nexacro probing via `getApplication() → getActiveForm() → getFocus()`, `getCellPos()`, `getBindDataset().rowposition`
- ranked selector candidates (id / testid / name / aria / placeholder / structural)

### Reused from `claude/improve-code-first-s2uqdb`

- `fingerprint.py` contract: layer table, four-state status, maturity, confidence, `rank`, `diagnostic`, `score`
- `DiscoveryManager` and adapter isolation
- Python-side confidence scoring
- Element Radar screen
- `tests/test_fingerprint.py`, `tests/test_discovery.py`, `tests/test_radar.py`

---

## 9. Bugs fixed in this pass

| # | Bug | Fix |
| --- | --- | --- |
| B1 | Tab identified by URL | CDP target id (`targets.py`) |
| B2 | Main-frame-only recording | probe installed in every frame; no frame filter |
| B3 | New tabs never followed | `page.on("popup")` → same session |
| B4 | iframe element screenshot clipped from the **top-level** page, so the picture showed the wrong area | capture through `locator.screenshot()` on the originating frame |
| B5 | `data-smartops-probe` removed only from the main document, leaving markers inside iframes | removal runs in the originating frame; regression test asserts zero markers in **every** frame |
| B6 | Frame context ranked as the strongest way to *identify* an element | frame is context, excluded from `rank()` |
| B7 | `StepDialog` rebuilds a step from visible fields, discarding fingerprint metadata | deep-copy the original, overwrite only editable keys |
| B8 | "fingerprints are safe to share" overstated | OCR output classified sensitive; claim narrowed |
| B9 | Recorder and radar ran different detection code | one probe, one manager |

---

## 10. Migration risks

- **Two contracts in one tree.** Retiring `Evidence`/`ElementFingerprint` breaks `main`'s
  `tests/test_discovery.py`. Those tests are rewritten against the canonical contract in the same
  commit — not deleted, not left failing.
- **`validate_workflow` strips unknown keys.** A recorded step now carries `fingerprint`; the schema
  must preserve it or B7 returns through the back door.
- **Windows-only imports.** `desktop_discovery.py` imports `pywinauto`/`pynput`/`PIL` lazily inside
  functions. It must import cleanly on Linux and report unavailable.
- **PyInstaller data files.** `probe.js` replaces `recorder.js` in `build.ps1`.
- **Chromium reports `HeadlessChrome`,** which `connect()` rejects. Browser tests connect directly
  rather than weakening that guard.

---

## 11. Acceptance tests

| Scenario | Test |
| --- | --- |
| normal page, every control type | `test_radar.py`, `test_session.py` |
| iframe and nested iframe | `test_session.py` |
| popup / new tab ownership | `test_session.py` |
| two tabs with identical URLs | `test_targets.py` |
| URL changes after selection | `test_targets.py` |
| SPA navigation | `test_targets.py` |
| password field, no value leak | `test_session.py` |
| screenshot really contains the iframe element | `test_session.py` (pixel check, not existence) |
| zero probe markers in every frame | `test_session.py` |
| detector failure isolation | `test_discovery.py` |
| recorder and radar share one manager | `test_session.py` |
| step edit preserves fingerprint | `test_gui.py` |
| ten consecutive identical recordings | `test_session.py` |
| draft recovery after a crash | `test_session.py` |
| existing replay and Excel demo unaffected | `test_core.py`, `--self-test` |

---

## 12. Staging

**Stage 1 (this branch, now):** architecture reconciliation, the twelve issues, tests, evidence.
**Stage 2 (after review):** the user journey itself — Record New Automation, preflight, target
picker UI, countdown, recorder bar, review timeline, name-and-save, draft recovery UI.

The report at the end of stage 1 is the gate. Nothing merges to `main` in either stage without it.
