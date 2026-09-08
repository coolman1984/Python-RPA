# SmartOps Universal Recorder / Discovery v0.2

This branch changes the recorder from a single DOM-selector capture into a multi-evidence discovery layer. Replay expansion is deliberately out of scope until discovery is validated on real systems.

## One action = one fingerprint

Every safe captured action can carry one normalized `fingerprint` containing independent evidence:

- `web`: DOM identity, role/name/text and ranked selector candidates.
- `nexacro`: application, active form, focused component, component path, Grid cell and bound Dataset row when Nexacro exposes them.
- `accessibility`: browser ARIA snapshot and Windows UI Automation evidence when available.
- `windows_uia`: native Windows control identity, automation ID, class, parent chain and nearby controls.
- `anchor`: nearby labels, siblings and stable text/context clues.
- `visual`: local target/control screenshot plus SHA-256 fingerprint.
- `relative_position`: viewport-relative position, screen coordinates and Canvas-relative click position.
- `keyboard`: special keys and shortcuts.
- `network`: nearby request/response clues with query strings, headers and bodies removed.
- `ocr` and `computer_vision`: reserved explicit layers. They stay unavailable until a real engine is selected and tested instead of pretending success.

`detected_by` summarizes only the layers that actually produced usable evidence.

## Recording coverage added

- Main frame and child frames.
- Popups/new pages opened from a tracked page.
- Normal web controls.
- Canvas click geometry.
- Special keyboard actions and shortcuts.
- Nexacro discovery through framework/application/form/component clues.
- Attended Windows/native-dialog clicks while recording is active, using global input listening plus Windows UI Automation.
- Local visual evidence for browser elements and Windows controls.
- Privacy-stripped network context near the user action.

## Safety and privacy gates

- Password, sign-in and secret-like fields remain excluded.
- Network evidence never stores request/response bodies, headers, query parameters or URL fragments.
- Discovery fingerprints have a hard size limit before saving.
- `desktop_click` and `desktop_press` are discovery-only in this branch. Replay rejects them explicitly rather than claiming unsupported reliability.
- Visual evidence stays local in the run folder and must be reviewed before sharing.

## What is not yet proven

- Windows UI Automation is implemented but still needs a real Windows run.
- Nexacro discovery is implemented but must be validated against the actual G-MES/Nexacro version and representative Grid screens.
- OCR and general computer vision are intentionally not bundled yet. They are fallback slots, not fake green checks.
- Pure desktop-only recording still starts from the attended recorder flow; a standalone desktop recorder mode can be added after this discovery layer is proven.

## Merge gate

Do not merge merely because the code exists. Before merging to `main`:

1. Run the complete unit-test suite.
2. Build the Windows package.
3. Record a normal web flow containing an iframe and a popup.
4. Confirm each captured action preserves a valid fingerprint and visual evidence where supported.
5. Record one native Windows dialog/control click.
6. On G-MES/Nexacro, verify active form/component identity and Grid cell/row evidence on representative screens.
7. Repeat the same short recording ten times and compare fingerprint stability.

The goal of this branch is a trustworthy discovery radar first. Replay fallback selection comes later.
