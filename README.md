# SmartOps Desktop Core 0.1

A native Python / PySide6 Windows application. A multi-layer element radar, workflow editing, attended browser recording and replay, separate automation processes, SQLite run history, versioned JSON workflows, YAML settings, and Excel validation.

## Run the Windows package

1. Extract the **entire** ZIP into a folder.
2. Double-click **SmartOps.exe** inside the SmartOps folder. Keep `_internal` beside the executable.
3. Select **Your first successful run**, then **Test run**. It creates a sample Excel report and validates its contents. No Python installation or terminal is needed.
4. Open **Run history** to inspect the result and open its output folder.

The app is unsigned. It stores workflows, settings, downloads and run history under `%LOCALAPPDATA%\SmartOpsDesktop`. Source and build files are included in the package's `source` folder. Moving the app does not delete its data.

## Workflows

Create a workflow and add, edit, reorder or remove steps. Use Save to save title/description changes; step edits save immediately. Import assigns a new ID, so an imported workflow cannot overwrite an existing workflow. Export creates versioned JSON for review or transfer. Review exports for any business data or captured field values before sharing.

Supported steps: navigate, click, fill, select, check, press, wait, download, validate_xlsx, nexacro_probe, demo_export. CSS and Playwright selectors are supported. Browser actions run on the selected tab, and an optional frame selector supports explicitly authored iframe steps. Steps execute once in order; failures stop the run. There are no unattended schedules, automatic retries, arbitrary Python or shell actions.

## Connect Google Chrome

This edition **attaches to an existing local Google Chrome debugging connection**. Normal Chrome windows do not expose CDP automatically. Enter the endpoint (default `http://127.0.0.1:9222`) in Settings, then click **Connect Chrome** and choose a unique tab. Only loopback endpoints are accepted.

Chrome remote debugging setup is an external prerequisite for browser features. Recent Chrome versions may require a separate user-data directory. SmartOps does not restart Chrome, copy a signed-in profile, enable debugging, or install an extension. On this machine use the approved Chrome launcher and Samsung corporate **Profile 19**. Sign in manually before recording or replay. The Settings URL button invokes the configured approved `open_chrome.py` helper; it does not enable CDP.

The local Excel demo works without Chrome. No corporate workflow is included as an allegedly verified automation.

## Record → review → test

1. Sign in, navigate to the starting screen in Chrome, then select its tab in SmartOps.
2. Click Record. Perform the task in that tab, then click Stop in SmartOps.
3. A new workflow is saved for review. The existing workflow is preserved.
4. Review the starting URL: query parameters and fragments are deliberately omitted by the recorder. Add any required non-secret parameters yourself.
5. Review steps and selectors. Convert the export-triggering click into **download**. That action waits for a real completed download and validates it as Excel.
6. Configure minimum data rows and required column names. Test run is a real replay and performs the recorded clicks in the selected tab.

Recorder scope: trusted clicks, input changes, select/checkbox changes and Enter in the selected tab's **main frame**. Password and recognizable sign-in/secret fields are skipped. It does not record new tabs, iframe interactions, canvas controls, download events or native dialogs. It does not infer business intent, success conditions, or a reliable Nexacro adapter. Some controls emit more than one event; review duplicates. Sensitive data can still exist in ordinary business fields, so review recordings before exporting.

## Element radar — the discovery layer

Before automating anything, ask what SmartOps can actually see. Pick a Chrome tab on the Workflows page, open **Element radar**, click **Start radar**, then click any element in that tab. The click is captured, not passed to the page, so pointing at a Save button never saves anything.

One user action produces one fingerprint through eleven independent detectors. `PROJECT-MAP.md` has the full flow, the confidence table and where each detector's output goes.

| # | Layer | What it answers | Detector proven? |
| --- | --- | --- | --- |
| 1 | Web element | Tag, id, name, text, selector | VERIFIED |
| 2 | Frame and tab context | Which frame, the iframe chain, whether the target opens a new tab | VERIFIED |
| 3 | Nexacro component | Component, type, path, form, grid, row, column, parent | IMPLEMENTED_UNVERIFIED |
| 4 | Accessibility tree | Role and accessible name, read over CDP, piercing iframes | VERIFIED |
| 5 | Windows UI Automation | Desktop windows, menus, dialogs, system controls | IMPLEMENTED_UNVERIFIED |
| 6 | Anchor | A stable neighbour, so a moved or renamed element is still findable | VERIFIED |
| 7 | Visual fingerprint | A picture of the element and of the area around it | VERIFIED |
| 8 | Relative position | Placement inside its own container, never the whole screen | VERIFIED |
| 9 | Keyboard route | Focus order and shortcuts, as a fallback path | VERIFIED |
| 10 | Screen text (OCR) | Text read off the screen as a clue to the location | IMPLEMENTED_UNVERIFIED |
| 11 | Computer vision | Last resort for what every other layer missed | IMPLEMENTED_UNVERIFIED |

Each target reads back as a diagnostic:

```
Web           ✅ 0.95      Anchor        ✅ 0.74
Frame         ✅ 0.99      Visual        ✅ 0.65
Nexacro       ✅ 0.85      Relative      ✅ 0.35
Accessibility ✅ 0.82      Keyboard      ✅ 0.62
Windows UIA   ⚪ unavailable
```

✅ found · ❌ the detector looked and could not · ⚪ the detector could not run here · ⚠️ the detector raised and the other ten carried on. Confidence answers one question: if this screen changes, how likely is this evidence to still point at the same element? A stable id scores 0.95; a positional selector 0.62; a Nexacro grid cell is capped at 0.75 because row and column follow the data, not the screen. Frame context is recorded but never ranked as a way of finding the element — it says where to look, not which element.

**Proven and unproven are tracked separately from found and not found.** A detector that has never met the real system reports IMPLEMENTED_UNVERIFIED however well it scores today, and only VERIFIED layers count towards a confident verdict. Nexacro is IMPLEMENTED_UNVERIFIED: both routes run in a real browser against a reproduction of Nexacro's rendered id shape, but neither has met a live Nexacro runtime. Windows UIA, OCR and computer vision are adapters whose availability check is exercised everywhere and whose query path has never executed.

Every element is stored in the run folder as `element-N.json` plus two PNGs. Fingerprints record identity only — `probe.js` reads no field values at all, and blanks the text of anything that looks like a credential while still recording that the element exists — so a fingerprint can be shared without carrying business data.

Nothing replays from a fingerprint yet. This release observes, scores and stores; choosing the best available layer at replay time is the next step.

## G-MES / Nexacro

The Nexacro probe reports framework availability and a small structural summary without invoking business methods. Nexacro-specific selectors and reviewed steps can be authored, but this release does not include a validated G-MES Daily Report adapter. Existing learned portal paths must continue to use the established `gmes_actions.py` / `portal_actions.py` skills. No new corporate UI paths were learned or changed while building this package.

## Excel verification

Downloads are initially saved with a generated `.bin` name, ignoring the server's filename. SmartOps checks ZIP integrity, required OOXML workbook members and readability, then checks data rows and configured column names. Only after passing are downloads renamed `.xlsx`. Failed bytes remain in the run folder for inspection. Each run has a unique directory.

Validation uses the selected worksheet or the first sheet. Its first row is treated as headers; blank rows are excluded. Formula values use cached results. Files without formula caches may fail row/content checks. Legacy XLS, CSV, HTML pretending to be Excel, encrypted workbooks and DRM-protected content are not supported by this validator. Expanded workbook contents are limited to 512 MB. Validation proves the configured workbook checks, not the business correctness of the report.

## Cancellation and recovery

Stop requests cooperative cancellation; a stuck worker is terminated after three seconds. The GUI remains responsive. Already completed actions cannot be undone, and partial files remain in the run folder. If the app exits unexpectedly, active runs are marked interrupted on the next launch. Browser connections are released without closing the user's Chrome. A lock prevents two desktop instances from writing the same database.

## Develop and build

Python 3.12 on Windows. Create `.venv`, install `requirements.txt`, then run `python main.py` from that environment. `build.ps1` runs the tests, builds a PyInstaller directory bundle, exercises the compiled executable with the real GUI and spawned demo worker, and creates the ZIP. PyInstaller and pytest are build dependencies, not required on the recipient's PC.

For an isolated diagnostic, set `SMARTOPS_SELFTEST_DIR` and run `SmartOps.exe --self-test`. This generates a JSON result and a GUI screenshot, using its own data directory. The ordinary GUI uses `SMARTOPS_DATA_DIR` only if you explicitly set that environment variable.
