# Automatic promotion incident follow-up — 2026-11-07

Baseline: `0b320a0`. Scope: automatic PREMIUM result confirmation/notifications, stale `/status`, and technical console noise. No real POST, live settings, SQLite, sessions, routing or financial authority modified.

## Confirmed evidence

Read-only production inspection found both reported operations already SUCCEEDED with one attempt and distinct9 RUB server receipts:

- TXE `a7c4ae14037c4a5291a23c42fff5b3a7`: receipt `1f1dad1c-432a-6730-f542-ccbae17861fc`, server debit2026-11-07T17:36:29.988Z.
- VPM `d389b5bd6395405da2139ecc73637062`: receipt `1f1dad1f-6726-6c30-dcb4-97d49beae739`, server debit2026-11-07T17:37:54.291Z. Initial saved diagnostic: RECEIPT_VALIDATION/PREMIUM_NEW_RECEIPT_NOT_VISIBLE, new_id=false. The owner had already reconciled the operation. Exact proof independently checked read-only; no history rewritten.

The mutation/readback can temporarily expose the previous payment. That ID cannot prove a new debit. Existing exact receipt/transaction reconciliation is retained; an acknowledged new-version unknown is checked promptly in the background without another mutation. Missing or conflicting proof stays unknown. History matching, account/item/amount/direction/operation/time and receipt uniqueness remain mandatory.

The older unstarted TXE operation `3fbe03e4cc464bac92e3dbe57bfccd9d` had VALIDATION_FAILED and no started_at. Its exact historical validation message was not retained: no claim is made about that operation's precise cause. Separately reproduced transient identity preflight now retains the reservation and standing authorization for safe retry; real STOP and changed monetary terms still block.

The approved catalog returned five items including Steam [RZP]. `/status` had displayed an older four-item snapshot. A stale/incomplete cache now gets bounded approved-only readback, without overwriting the full catalog; temporary failure retains honestly labelled saved data. Foreign-account data is rejected. Command deletion, a new standalone status message and the single Close button remain. Green/red circles replace on/off; blocked enabled delivery is yellow rather than falsely green.

## User-visible result

One existing notification-loop message per owner is edited as each selected lot is checked, queued, confirmed or skipped. It replaces generic unknown + rich automatic-operation-result duplicates. Manual purchases retain their existing receipt screen. Example:

```
Автоподнятие · 20:37
Мишка [TXE] — ✅ успешно · 9.00 ₽
Сердечко [VPM] — ⏳ проверяем списание
Другой лот — позиция 21 · поднятие не требуется
```

Exact confirmation changes the second line to success in the same message. Success is never inferred from position. Unresolved financial outcomes remain explicit with an existing operation link. A potentially delivered first Telegram send is never blindly repeated. Slow Telegram RPCs continue under one owned task beyond the short flush waiting budget; cancellation journals an unknown result. Existing navigation locks prevent background edits of a message the owner opened as an operation card. Completed failed task references are drained once, allowing a later flush to resume.

Console keeps actual sequence/check/start/confirmed-result business lines. Readiness authentication-gate details and derived relist gate/recovery messages go to DEBUG. Durable health, primary account verifier and prolonged real service failures remain visible. No auth challenge bypass.

The existing live policy checks every2 minutes and permits purchases no more often than once per10 minutes. Neither interval, tariff, thresholds nor limits were changed. A summary is a common updated message, not a new message per item/check.

## RED/GREEN and verification

Reproductions recorded in PROGRESS.md. Tests: `test_auto_promotion_incident.py`, `test_quick_relist_status.py`, `test_readiness_refresh.py`, plus existing payment/receipt/notification/navigation suites. Controlled transports and temporary SQLite only. Profile and frozen-source full regression results will be appended after completion; earlier PASS belongs to earlier commits.

Remaining limits: an eventually consistent API cannot be forced to expose a new receipt; no proof means no confirmed success and no retry POST. Telegram first-send unknown cannot be safely recovered by blind resend. Telegram's single-message size limit bounds a very large catalog summary; remaining lots stay available through the existing promotion screen. Real paid dispatch, physical Happ outages and unattended24/7 operation are not claimed verified by offline tests.
