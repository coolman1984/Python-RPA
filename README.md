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

## Element radar

Before automating anything, ask what SmartOps can actually see. Pick a Chrome tab on the Workflows page, open **Element radar**, click **Start radar**, then click any element in that tab. The click is captured, not passed to the page, so pointing at a Save button never saves anything.

Each element comes back as one card listing every independent way it was recognised:

| Layer | What it answers | This release |
| --- | --- | --- |
| Web element | Tag, id, name, text, selector, inner frame, new tab | built |
| Nexacro component | Component, its form, grid, row, column and parent | built |
| Accessibility tree | Role and accessible name, read over CDP | built |
| Windows control | Desktop windows, menus, dialogs, system buttons | not built yet |
| Anchor | A stable neighbour, so a moved or renamed element is still findable | built |
| Image | A picture of the element and of the area around it | built |
| Screen text | Text read off the screen as a clue to the location | not built yet |
| Relative position | Placement inside its window or anchor, never the whole screen | built |
| Keyboard route | Focus order and shortcuts, as a fallback path | built |
| Computer vision | Last resort for what every other layer missed | not built yet |

✅ means that layer identified the element, ❌ means it looked and could not, ⚪ means the layer is not available here. A layer that is not built yet always reads ⚪, never ❌, so the card never overstates what was tried.

Every element is stored in the run folder as `element-N.json` plus two PNGs, so a fingerprint can be reviewed or compared later. Fingerprints record identity only — never the contents of a field — so they can be shared without carrying business data. Nexacro identification is read both from the live component and from the dotted id Nexacro renders into the page; the id route keeps working when component internals are unavailable. It has been exercised against a reproduction of that id shape, not yet against the live G-MES screens.

Nothing replays from a fingerprint yet. This release captures and shows them; choosing the best available layer at replay time is the next step.

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
