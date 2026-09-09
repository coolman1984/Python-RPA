# Project Eye

The short human entry point. The machine-readable truth lives in `.project-eye/`, and
`tools/project_eye.py` is what enforces it.

## What SmartOps is

A Windows desktop application that watches one real user interaction, recognises the target through
as many independent layers as it can, and keeps the result as a durable, reviewable recording.
Replay exists but is deliberately narrow.

## Domains

| Domain | Owns |
| --- | --- |
| capture | probe.js, DiscoveryManager, the fingerprint contract, desktop and network discovery |
| recording | RecordingSession, DraftJournal, target identity, the worker process |
| persistence | the workflow schema and the Store |
| execution | replay |
| presentation | the GUI — and nothing else |
| delivery | packaging |

## The rule everything else rests on

```
DraftJournal materialised timeline   =   AUTHORITATIVE recording state
GUI timeline                         =   MIRROR ONLY
```

A save reads the journal. It has already gone wrong the other way once: the GUI kept its own copy,
enrichment never reached it, and the recorded workflow could not be saved at all.

## Who owns which state

| State | Owner |
| --- | --- |
| recording state, page ownership, capacity, enrichment scheduling, the worker event contract | `session.py` |
| the recorded timeline | `DraftJournal` |
| browser target identity | `targets.py` |
| layer set, status and maturity vocabulary | `fingerprint.py` |
| locator judgement and confidence | `discovery.py` |
| page-side facts (never judgement) | `probe.js` |
| workflow validation and the action set | `core.py` |
| persisted workflows and run history | `Store` |
| presentation | `gui.py` |

A second authority over any of these is an **ownership conflict**, and `validate` fails on it.

## Critical journeys

`RECORD-ACTION` · `ENRICH-ACTION` · `RADAR-ELEMENT` · `SAVE-RECORDING` · `RECOVER-DRAFT`
`CONNECT-TARGET` · `POPUP-RECORDING` · `NESTED-FRAME-RECORDING` · `REPLAY-WORKFLOW`
`DOWNLOAD-AND-VALIDATE` · `DESKTOP-RECORDING`

```
python3 tools/project_eye.py journey RECORD-ACTION
```

## Red zones

`session.py` · `gui.py` · `discovery.py` · `probe.js` · `core.py`

Each is central, each changes often, and each has broken a journey at least once.

## Where the project actually is

Stage 1.5. The recording kernel is repaired and proven on a real Chromium. The user journey is
still the old one — pick a workflow, Connect Chrome, choose a tab — and that is deliberate.

Three differences between what is intended and what is proven are recorded in
`.project-eye/current-state.yaml` rather than edited away:

- **replay is behind the recorder.** Ranked locators, the frame chain and the page context are all
  captured, persisted and survive a save. Replay reads only `selector` and one `frame`.
  **Recorded is not the same as replayable.**
- **recovery has a contract but no screen.** Unfinished drafts are detectable and tested; nothing
  offers them to the user yet.
- **Windows and Nexacro have never been exercised for real.** Their detectors run and their
  availability checks are tested. That is not verification.

## Using the tool

```
python3 tools/project_eye.py scan                     # re-read the tree, refresh the import graph
python3 tools/project_eye.py validate                 # fail on broken refs, contracts, rules
python3 tools/project_eye.py doctor                   # red zones, drift, stale proofs, gaps
python3 tools/project_eye.py impact --path smartops_desktop/session.py
python3 tools/project_eye.py journey SAVE-RECORDING
python3 tools/project_eye.py context --task "Fix recording save"
python3 tools/project_eye.py delta --base <commit>
```

`validate` exits non-zero when the map and the code disagree, so it belongs in the merge gate.
`tests/test_project_eye.py` proves it by breaking the map on purpose and asserting it refuses.

## Before changing anything

```
SEE → MAP → TRACE → IMPACT → TEST → CHANGE → RECONNECT → VALIDATE → UPDATE EYE
```

Start with `impact --path <file>`: it names the owner, the contracts produced and consumed, the
journeys touched, the tests to run, and whether the file is a red zone.

## Proof vocabulary

`none` · `synthetic` · `real-chromium` · `real-windows` · `real-nexacro`

**VERIFIED never appears without naming which of these it means.** A capability whose only evidence
is a mock, a synthetic page or a headless run says so, in `.project-eye/capabilities.yaml`.

## Memory

`.project-eye/memory.jsonl` is append-only engineering memory: decisions, verified lessons,
incidents, constraints and rejected approaches. Not transcripts, not guesses, no secrets.
