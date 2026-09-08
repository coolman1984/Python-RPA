# SmartOps Universal Recorder / Discovery v0.2

This branch changes the recorder from a single DOM selector capture into a multi-evidence discovery layer. Replay expansion is deliberately out of scope until discovery is validated on real systems.

## One captured action, one fingerprint

Each safe captured browser action can now carry independent evidence under `fingerprint`:

- `web`: tag, id/name/type, role, accessible name, text hints and ranked selector candidates.
- `accessibility`: Playwright ARIA snapshot when the target can be resolved.
- `nexacro`: framework availability, active form, focused component, component path, Grid cell and bound Dataset row when exposed by Nexacro.
- `anchors`: nearby label/container/text anchors.
- `geometry`: viewport-relative and screen coordinates; Canvas clicks also keep coordinates relative to the Canvas.
- `windows_uia`: Windows UI Automation element, automation id, class, parent chain and nearby UIA anchors when available.
- `visual`: local element/control screenshot plus SHA-256 fingerprint.
- `screen_text`: UI Automation text/name when available.
- `network`: a short ring of nearby request/response metadata. Query strings, headers and bodies are never stored.
- `frame`: nested frame path metadata. A single frame also populates the legacy frame selector when possible.
- `browser`: privacy-stripped page URL plus a recording page token and root/popup relation.

`detected_by` summarizes the layers that actually produced evidence for the action.

## Recording coverage added

- Main frame and child frames.
- Popups/new pages opened from a tracked page.
- Normal DOM controls.
- Canvas click geometry.
- Special keyboard actions and shortcuts in browser controls.
- Nexacro discovery clues using documented `nexacro.getApplication()`, active Form/component collections, focus and Grid cell APIs.
- Attended Windows desktop/native-dialog clicks while browser recording is active, using global input listening plus Windows UI Automation.
- Visual evidence capture for browser elements and Windows controls.

## Intentional gates

- Password/sign-in/secret-like fields remain excluded.
- Network evidence does not store headers, bodies, query parameters or fragments.
- Fingerprints are size-limited before they can be saved.
- `desktop_click` and `desktop_press` are discovery-only in this branch. Replay rejects them explicitly rather than pretending they are supported.
- OCR of image-only text is not bundled yet. UIA/DOM text and visual evidence are captured first; adding a heavyweight OCR engine should be a separate measured decision.
- Nexacro discovery must be validated on the real G-MES/Nexacro version before calling it reliable.

## Validation gate before merge

1. Run the unit tests.
2. Build the Windows package.
3. Record a normal web flow containing an iframe and a popup.
4. Confirm every captured action keeps a valid fingerprint and local visual evidence.
5. Record one native Windows dialog click while recording is active.
6. On G-MES/Nexacro, verify active form/component identity and Grid cell/row evidence on representative screens.
7. Repeat the same short recording ten times and compare fingerprint stability.

Do not merge to `main` merely because the code exists. Real Windows + Chrome + G-MES evidence is the acceptance gate.
