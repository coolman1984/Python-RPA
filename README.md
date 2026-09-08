# SmartOps Desktop Core 0.2 · Discovery Branch

A native Python / PySide6 Windows application for attended automation recording, workflow review/replay, SQLite run history, versioned JSON workflows, local evidence and Excel validation. This branch focuses first on making the recorder understand a target through several independent discovery layers before expanding replay.

## Run the Windows package

1. Extract the **entire** ZIP into a folder.
2. Double-click **SmartOps.exe** inside the SmartOps folder. Keep `_internal` beside the executable.
3. Select **Your first successful run**, then **Test run**. It creates a sample Excel report and validates its contents. No Python installation or terminal is needed.
4. Open **Run history** to inspect the result and open its output folder.

The app is unsigned. It stores workflows, settings, downloads, discovery evidence and run history under `%LOCALAPPDATA%\SmartOpsDesktop`. Source and build files are included in the package's `source` folder. Moving the app does not delete its data.

## Workflows

Create a workflow and add, edit, reorder or remove steps. Use Save to save title/description changes; step edits save immediately. Import assigns a new ID, so an imported workflow cannot overwrite an existing workflow. Export creates versioned JSON for review or transfer. Review exports for any business data or captured field values before sharing.

Supported replay steps remain: navigate, click, fill, select, check, press, wait, download, validate_xlsx, nexacro_probe and demo_export. The discovery recorder can additionally capture `desktop_click` and `desktop_press`, but those native actions are deliberately blocked from replay in this branch until the discovery layer is validated on real Windows systems.

## Connect Google Chrome

This edition **attaches to an existing local Google Chrome debugging connection**. Normal Chrome windows do not expose CDP automatically. Enter the endpoint (default `http://127.0.0.1:9222`) in Settings, then click **Connect Chrome** and choose a unique tab. Only loopback endpoints are accepted.

Chrome remote debugging setup is an external prerequisite for browser features. Recent Chrome versions may require a separate user-data directory. SmartOps does not restart Chrome, copy a signed-in profile, enable debugging, or install an extension. On this machine use the approved Chrome launcher and Samsung corporate **Profile 19**. Sign in manually before recording or replay. The Settings URL button invokes the configured approved `open_chrome.py` helper; it does not enable CDP.

The local Excel demo works without Chrome. No corporate workflow is included as an allegedly verified automation.

## Record → discover → review

1. Sign in, navigate to the starting screen in Chrome, then select its tab in SmartOps.
2. Click Record and perform the task normally.
3. SmartOps records safe browser actions and builds one multi-layer fingerprint for each target.
4. The recorder follows child frames and popups opened from the tracked page.
5. While recording is active on Windows, it can also collect attended native-dialog/control clicks through Windows UI Automation.
6. Click Stop. A new workflow is saved for review; the existing workflow is preserved.

Each fingerprint can contain evidence from:

- Web/DOM identity and ranked selector candidates.
- Browser accessibility/ARIA information.
- Nexacro application, active form, focused component, component path and Grid/Dataset clues when exposed.
- Windows UI Automation control identity and parent/nearby controls.
- Nearby anchors and labels.
- Relative screen/viewport position and Canvas-relative click position.
- Local visual evidence of the target/control.
- Keyboard shortcuts/special keys.
- Privacy-stripped nearby network request/response metadata.

Password, login and secret-like fields are excluded. The browser sends a private suppression marker so the Windows fallback does not re-capture the same sensitive interaction. Browser and Windows hooks are also de-duplicated so one real click does not become two recorded steps.

Query strings, URL fragments, request/response headers and bodies are not stored in network evidence. Visual evidence remains local in the run folder and must be reviewed before sharing.

OCR and general computer-vision layers exist in the fingerprint model as explicit fallback slots but are **not** claimed as working engines yet. They stay unavailable until a real implementation is selected and tested.

## G-MES / Nexacro

The recorder now probes Nexacro more deeply than the original availability-only check. When the framework exposes the information, it records the application, active form, focused component, component path, display text, Grid cell position and bound Dataset row. The separate `nexacro_probe` step also reports active form/focus and a component summary.

This is still **not a validated G-MES adapter**. The actual factory Nexacro version and representative Grid screens must be tested before calling this layer reliable. Existing learned portal paths remain untouched.

## Replay boundary in this branch

Browser replay and the existing Excel validation path remain available. Native Windows actions collected by the new recorder are discovery evidence only and intentionally fail fast if replay is attempted. This prevents the project from pretending a new fallback works before it has been proven.

The next replay phase should choose among the stored discovery layers only after the recorder passes real stability tests.

## Excel verification

Downloads are initially saved with a generated `.bin` name, ignoring the server's filename. SmartOps checks ZIP integrity, required OOXML workbook members and readability, then checks data rows and configured column names. Only after passing are downloads renamed `.xlsx`. Failed bytes remain in the run folder for inspection. Each run has a unique directory.

Validation uses the selected worksheet or the first sheet. Its first row is treated as headers; blank rows are excluded. Formula values use cached results. Files without formula caches may fail row/content checks. Legacy XLS, CSV, HTML pretending to be Excel, encrypted workbooks and DRM-protected content are not supported by this validator. Expanded workbook contents are limited to 512 MB. Validation proves the configured workbook checks, not the business correctness of the report.

## Cancellation and recovery

Stop requests cooperative cancellation; a stuck worker is terminated after three seconds. The GUI remains responsive. Already completed actions cannot be undone, and partial files/evidence remain in the run folder. If the app exits unexpectedly, active runs are marked interrupted on the next launch. Browser connections are released without closing the user's Chrome. A lock prevents two desktop instances from writing the same database.

## Develop and build

Python 3.12 on Windows. Create `.venv`, install `requirements.txt`, then run `python main.py` from that environment. `build.ps1` runs the tests and explicitly bundles Playwright plus the Windows UI Automation, global-input and image-capture dependencies before building the PyInstaller package. It then exercises the compiled executable with the existing self-test and creates the ZIP.

For an isolated diagnostic, set `SMARTOPS_SELFTEST_DIR` and run `SmartOps.exe --self-test`.

## Acceptance gate before merge

Do not merge this branch merely because the code exists. Run a real Windows test containing a normal web page, iframe, popup, native Windows dialog and representative G-MES/Nexacro screen. Repeat the same short recording ten times and compare the fingerprints. See `docs/RECORDER_DISCOVERY_V0_2.md` for the detailed gate.
