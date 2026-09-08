# SmartOps Desktop Core 0.1

A native Python / PySide6 Windows application. Workflow editing, attended browser recording and replay, separate automation processes, SQLite run history, versioned JSON workflows, YAML settings, Excel validation, and an experimental multi-layer discovery recorder.

## Run the Windows package

1. Extract the **entire** ZIP into a folder.
2. Double-click **SmartOps.exe** inside the SmartOps folder. Keep `_internal` beside the executable.
3. Select **Your first successful run**, then **Test run**. It creates a sample Excel report and validates its contents. No Python installation or terminal is needed.
4. Open **Run history** to inspect the result and open its output folder.

The app is unsigned. It stores workflows, settings, downloads and run history under `%LOCALAPPDATA%\SmartOpsDesktop`. Source and build files are included in the package's `source` folder. Moving the app does not delete its data.

## Workflows

Create a workflow and add, edit, reorder or remove steps. Use Save to save title/description changes; step edits save immediately. Import assigns a new ID, so an imported workflow cannot overwrite an existing workflow. Export creates versioned JSON for review or transfer. Review exports for any business data or captured field values before sharing.

Supported replay steps: navigate, click, fill, select, check, press, wait, download, validate_xlsx, nexacro_probe, demo_export. CSS and Playwright selectors are supported. Browser actions run on the selected tab, and an optional frame selector supports explicitly authored iframe steps. Steps execute once in order; failures stop the run. There are no unattended schedules, automatic retries, arbitrary Python or shell actions.

## Connect Google Chrome

This edition **attaches to an existing local Google Chrome debugging connection**. Normal Chrome windows do not expose CDP automatically. Enter the endpoint (default `http://127.0.0.1:9222`) in Settings, then click **Connect Chrome** and choose a unique tab. Only loopback endpoints are accepted.

Chrome remote debugging setup is an external prerequisite for browser features. Recent Chrome versions may require a separate user-data directory. SmartOps does not restart Chrome, copy a signed-in profile, enable debugging, or install an extension. On this machine use the approved Chrome launcher and Samsung corporate **Profile 19**. Sign in manually before recording or replay. The Settings URL button invokes the configured approved `open_chrome.py` helper; it does not enable CDP.

The local Excel demo works without Chrome. No corporate workflow is included as an allegedly verified automation.

## Multi-layer recording and discovery

When Record is active, SmartOps captures the normal replayable browser action and attaches independent discovery evidence to the same action. A failure in one discovery layer does not discard the recorded step.

Current browser discovery layers:

- DOM identity: selector, tag, id, name, role, label, visible text, ARIA data and geometry.
- Nexacro hints: framework/application presence, DOM id chain, internal-object candidates when exposed, and accessibility-id candidates.
- Chrome accessibility tree through CDP for main-frame DOM targets.
- Anchors: nearby label, sibling and container text candidates.
- Relative position: element rectangle, viewport and click position inside the target.
- Visual fingerprint: a small PNG crop plus SHA-256 fingerprint stored with the recording run.
- Child-frame context metadata and popup-page tracking.
- Safe navigation/shortcut keys in addition to Enter.

On Windows, a global discovery observer runs beside the browser recorder. For desktop clicks it records Windows UI Automation, Win32 window hierarchy, relative position, visible accessibility text and a small visual crop. It also observes navigation/shortcut keys. **Raw global character typing is deliberately not captured** to avoid silently recording passwords or sensitive text.

Desktop observations are evidence in the run journal and `desktop-discovery/observations.jsonl`; they are not silently converted into replayable desktop steps yet. This branch is intentionally discovery-first.

Visual crops are local evidence and are marked non-portable. Exporting a workflow JSON does not currently bundle those image files.

## Record → review → test

1. Sign in, navigate to the starting screen in Chrome, then select its tab in SmartOps.
2. Click Record. Perform the task, then click Stop in SmartOps.
3. A new browser workflow is saved for review; desktop discovery evidence remains in that recording run.
4. Review the starting URL: query parameters and fragments are deliberately omitted by the recorder. Add any required non-secret parameters yourself.
5. Review captured values, duplicate events, selectors and discovery fingerprints.
6. Convert an export-triggering click into **download** when appropriate, then Test run.

Password and recognizable sign-in/secret browser fields are skipped. The recorder does not infer business intent or success conditions. Sensitive business data can still exist in ordinary fields, accessibility names, nearby text and screenshots, so review recordings before sharing.

## G-MES / Nexacro

Nexacro discovery is now multi-source rather than a single availability probe. The recorder collects framework hints from the page and also checks Windows/Chrome accessibility information, which may expose a component path or automation id. This is designed to help identify controls such as forms, buttons and grids even when ordinary HTML selectors are weak.

This is **not yet a validated G-MES adapter**. Nexacro internals vary by product/version and some controls may still appear only as visual or accessibility targets. Real G-MES validation must be performed on the authorized machine before claiming reliable coverage.

## Excel verification

Downloads are initially saved with a generated `.bin` name, ignoring the server's filename. SmartOps checks ZIP integrity, required OOXML workbook members and readability, then checks data rows and configured column names. Only after passing are downloads renamed `.xlsx`. Failed bytes remain in the run folder for inspection. Each run has a unique directory.

Validation uses the selected worksheet or the first sheet. Its first row is treated as headers; blank rows are excluded. Formula values use cached results. Files without formula caches may fail row/content checks. Legacy XLS, CSV, HTML pretending to be Excel, encrypted workbooks and DRM-protected content are not supported by this validator. Expanded workbook contents are limited to 512 MB. Validation proves the configured workbook checks, not the business correctness of the report.

## Cancellation and recovery

Stop requests cooperative cancellation; a stuck worker is terminated after three seconds. The GUI remains responsive. Already completed actions cannot be undone, and partial files remain in the run folder. If the app exits unexpectedly, active runs are marked interrupted on the next launch. Browser connections are released without closing the user's Chrome. A lock prevents two desktop instances from writing the same database.

## Develop and build

Python 3.12 on Windows. Create `.venv`, install `requirements.txt`, then run `python main.py` from that environment. `build.ps1` runs the tests, builds a PyInstaller directory bundle, exercises the compiled executable with the real GUI and spawned demo worker, and creates the ZIP. PyInstaller and pytest are build dependencies, not required on the recipient's PC.

The branch also includes a Windows GitHub Actions workflow for syntax and unit tests. API-created commits may not trigger repository Actions depending on GitHub/App settings, so absence of an Actions run is not proof of success.

For an isolated diagnostic, set `SMARTOPS_SELFTEST_DIR` and run `SmartOps.exe --self-test`. This generates a JSON result and a GUI screenshot, using its own data directory. The ordinary GUI uses `SMARTOPS_DATA_DIR` only if you explicitly set that environment variable.
