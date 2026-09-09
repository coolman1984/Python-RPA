# 0001 — A 90 MB build artifact lives in the source tree

Status: RECORDED, not acted on.

`SmartOps-Desktop-v0.1-Windows.zip` (≈86 MB) is committed at `ca5751c`. `.gitignore` already lists
`*.zip`, so it was added against the repository's own rule. It is a **build artifact**: `build.ps1`
produces exactly this file from `dist/SmartOps`.

Cost: every clone pays ~87 MB and the history keeps it for ever, even if it is deleted later.

Not deleted here. It may be the only copy of a build that was handed to someone, and removing it
from history rewrites every commit. That is the owner's decision, not this task's.

Rule recorded for the future: a binary release does not belong in the source tree unless a written
decision says otherwise. Adding another one should be refused.

Options, for whoever decides:
1. Leave it. Cost is already paid; no risk.
2. `git rm` it, keep it in a GitHub Release. Future clones get smaller; history does not.
3. Rewrite history to purge it. Smallest repository; invalidates every existing clone and SHA.
