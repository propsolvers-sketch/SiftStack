---
name: 260525-vop-summary
quick_id: 260525-vop
status: complete
date: 2026-05-26
---

# Quick Task 260525-vop: ModalOverlay+Loom Dismissal — Summary

## Outcome

✅ **Complete.** `dismiss_popups()` now surgically removes DataSift first-time-user onboarding modals (Loom-video tutorials with `Modalstyles__ModalOverlay-*` overlay) that were blocking every wizard click on the GHA daily-sweep runner.

## Root cause (from GHA run #26412123825)

Fresh ephemeral GHA Playwright Chromium → no persistent `.datasift_profile/` → DataSift treats the session as a first-time user → pops onboarding modal with Loom video → overlay intercepts every click → Tag input, file input, list filter all time out after 30s → all 3 CSVs return `success=False Uploaded 0/1 splits` → `daily_finalize.py` exits 1 → `:rotating_light: SiftStack daily sweep FAILED` Slack post fires.

## What changed (1 file, 2 atomic commits)

| File | Change |
|---|---|
| `src/datasift_core.py` | Added ~7 JS lines inside `dismiss_popups()` (before `return removed;`) that nuke any `[class*="ModalOverlay"]` element containing a `loom.com` link. Docstring updated to describe the new dismissal. |

The change is **surgical** — it only targets onboarding modals (identified by the Loom-video link signature), so legitimate wizard-driven modals (Skip Send-To, upload wizard, etc.) that DON'T contain Loom links remain unaffected.

## Verification

| Check | Result |
|---|---|
| `grep "loom.com" src/datasift_core.py` | New block present at the bottom of the JS removal cascade |
| `python -m pytest tests/unit/ -q` | **110 passed, 1 skipped in 3.58s** (no regression) |
| `PYTHONPATH=src python -c "from datasift_core import dismiss_popups"` | `import ok` |
| Existing dismissals (Beamer NPS, Beamer push, notification overlays) | Byte-identical — additive change only |

## What can NOT be verified from this session

- **Live GHA run** — can't trigger the daily-sweep without waiting for the next 11:00 UTC cron OR manually dispatching the workflow. The fix should land first, then the user can run `gh workflow run daily-sweep.yml --ref feat/al-migration-organized` to validate.

If the next daily-sweep STILL fails with `Uploaded 0/1 splits`, the modal class signature may have changed (DataSift could ship a UI update with a different class name) — re-grep the GHA log for the new overlay class and extend the dismissal selector. The targeting pattern (overlay + Loom link) should be durable to most cosmetic UI changes.

## Related historical fixes (for context)

- `1e79f1f fix(datasift): dismiss Beamer modal before each wizard step` — same defensive pattern, different overlay class (`#beamerPushModal`). This task extends that approach to DataSift's own onboarding modal.

## Branch

`feat/al-migration-organized` — commits land on top of the morning's Phase 2 + TN→AL rename work.
