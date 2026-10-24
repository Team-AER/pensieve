# Extension completion verification · 24 October 2026

The existing paper background, blue primary action, compact form and web-app design remain. A confirmed save now keeps its result and Open in Pensieve link visible instead of closing after 2.2 seconds. Save/Saving/Saved labels and `aria-busy` describe actual request state. Pending duplicate submissions are ignored, failure retains inputs and enables retry, and confirmed inputs become read-only. Completion fades in for 180 ms and respects reduced motion.

Actual browser execution also exposed a pre-existing classic-script binding collision: shared `settings` declarations prevented the popup from loading. The shared client is now privately scoped and still exports only the existing `self.PensieveExt` API used by popup and background. No service contract changed.

## Evidence



## Verification

- Node VM regressions: 3 passed, loading the actual scripts, asserting private shared exports, persistent completion and no auto-close, retained input on failure and pending duplicate prevention.
- Python suite: 387 passed, 2 skipped; Ruff passed. Backend was not changed.
- Browser uses the actual popup HTML/CSS/JS. All three widths fit without overflow; keyboard Space submits, notes become read-only after success, completion remains after 2.4 seconds, reduced motion sets completion animation to none, and failure keeps the note with Save enabled.

Installed extension packaging, browser host-permission prompts and authenticated live server integration remain unverified. The existing original local feature commit is preserved on its original branch and excluded from this main-based UI PR.

Visual evidence is retained locally outside Git. Review images and media are not committed; these notes retain the text verification and fixture boundaries.
