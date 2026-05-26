---
name: 260525-vop-plan
quick_id: 260525-vop
phase: quick
plan: 260525-vop
description: Add surgical ModalOverlay+Loom dismissal to dismiss_popups() so GHA daily-sweep wizard isn't blocked by DataSift's first-time-user onboarding modal
status: ready_for_execution
files_modified:
  - src/datasift_core.py
must_haves:
  truths:
    - dismiss_popups() removes any [class*="ModalOverlay"] element that contains a Loom video link (the DataSift onboarding modal signature)
    - Existing dismissals (Beamer NPS, Beamer push, notification overlays) remain byte-identical — additive change only
    - Legitimate DataSift wizard modals (Skip Send-To, upload wizard, etc.) that DON'T contain Loom links are NOT touched
    - Full pytest suite still passes (110 + 1 skip)
  key_links:
    - src/datasift_core.py:198 dismiss_popups function
    - GHA log evidence: a href="https://www.loom.com/share/b58b41ba94324790b8ae0b1947d5a299"
    - parent class signature: Modalstyles__ModalOverlay-gUheGS ipmZcI
---

# Plan 260525-vop — ModalOverlay+Loom Dismissal

## Goal

Unblock the GHA daily-sweep DataSift upload step by extending `dismiss_popups()` to remove the DataSift first-time-user onboarding modal (Loom-video tutorial) that intercepts every wizard click on a fresh browser session.

## Root cause (from GHA run #26412123825)

- GHA runs Playwright in an ephemeral container with no `.datasift_profile/` persistent profile
- DataSift treats the session as a first-time user and pops an onboarding modal: `<div class="Modalstyles__ModalOverlay-gUheGS ipmZcI">` containing `<a href="https://www.loom.com/share/...">Click here to learn how this section works</a>`
- This overlay intercepts ALL pointer events → every wizard click (Tag input, file input, list filter, etc.) times out after 30s
- Result: all 3 DataSift uploads on today's run returned `success=False Uploaded 0/1 splits`

## Tasks

### Task 1: Add surgical Loom-modal dismissal to dismiss_popups()

**File**: `src/datasift_core.py`

**Where**: Inside the JS evaluate block of `dismiss_popups()` (lines 215-246), after the existing fixed/absolute overlay removal (line 244), before `return removed;`.

**What to add** (~6 lines of JS):

```js
// Remove DataSift first-time-user onboarding modals (Loom video tutorials).
// Surgical: only remove ModalOverlay elements that contain a Loom link, so
// legitimate wizard-driven modals (Skip Send-To, upload wizard) are unaffected.
// Triggered on GHA runners (fresh browser, no .datasift_profile/ cookies).
document.querySelectorAll('[class*="ModalOverlay"]').forEach(el => {
    if (el.querySelector('a[href*="loom.com"]')) {
        el.remove();
        removed++;
    }
});
```

**Action**:
1. Open src/datasift_core.py
2. Find the `// Also try removing any fixed/absolute overlays` block
3. After its closing `}` (around line 244), insert the new dismissal block above `return removed;`
4. Update the function docstring (line 199-203) to mention the new Loom-modal dismissal

**Verify**:
- `grep -A 3 "loom.com" src/datasift_core.py` shows the new block
- `python -c "from datasift_core import dismiss_popups; print('import ok')"` succeeds (after `cd src/` or PYTHONPATH=src)
- `python -m pytest tests/unit/ -q` returns `110 passed, 1 skipped`

**Done**:
- New dismissal block present in dismiss_popups() JS
- Function docstring mentions the new Loom-modal handling
- Test suite green (no regression)
- All existing dismissals (Beamer NPS, Beamer push, notification overlays) unchanged
