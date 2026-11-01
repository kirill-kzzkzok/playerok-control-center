# Independent takeover review — Financial Safety / Concurrency

Date: 2026-11-01. Base: 9f5dee1. Reviewer: separate Codex subagent, not the implementation author's self-review. Scope is the launch/release/payment authority boundary; this document does not claim an independent review of every UI module or live Telegram delivery.

## Confirmed findings

| Finding | Priority | Reproduction and consequence | Correction |
|---|---|---|---|
| F1 | P1 | Real ListingLaunch.queue PREMIUM payload lacked delivery_revision while marker had revision1. Terminal pre-dispatch failure left the launch-created text policy enabled. | Parent added the exact committed revision to PREMIUM payload. Shared release can also recover revision from a matching older marker. |
| F2 | P1 | Real queued legacy PREMIUM, UNCERTAIN before any intent.started_at, owner STOP: reconciliation deleted marker before reading fallback revision; committed text delivery survived release. | One Store.release_launch implementation handles both terminal local failure and proven-never-started owner stop, inside the caller's transaction. Read matching marker before deletion. |
| F3 | P1 | Real DEFAULT queue followed by a newer Automation.save; old failure preserved new policy revision but unconditionally disabled delivery:item:enabled. | Set auxiliary enabled=false only if delivery_policies CAS actually changed the launch-owned revision. Draft release also checks launch revision. No guessed blanket disarm of revisionless legacy policies. |
| F8-AUTH | P0 | Policy enabled in calculation-only mode, B tariff approved/included, no global spending authority. Launch A with promotion=true caused real PromotionWorker and OutboxWorker to issue a mock paid RPC for B. | Launch grants authority only for its item/account/policy/launch revision. Scheduler and paid executor share authority selection. Separately confirmed existing shop-wide arm remains supported. Release deletes only the exact launch-owned scoped grant. |

All four findings have RED tests on the original faulty path. Tests do not synthesize a correct premium payload: they call real queue(). F3's newer owner state uses Automation.save(). F8 checks the actual paid mock RPC, not merely a row.

## Executed verification

- Initial RED: tests/test_takeover_release.py: 4 failed, 2 passed, exit1. Separately F8 actual scheduler/RPC reproduction: 1 failed, exit1.
- After release fixes: release + reconciliation + paid promotions: 64 passed, exit0.
- Additional reopen/lease and SQLite abort tests: same group 67 passed, exit0.
- Release + reconciliation + paid + launch fault matrix + listing launch: 98 passed in23.75s, exit0.
- Final dedicated tests including scoped grant deletion / newer grant preservation: 12 passed in7.75s, exit0.
- Ruff check and format --check of owned production/test files: exit0; git diff --check exit0.
- Gift/bundle/session/stock regressions: final result recorded below after completion.

Reopen uses a new Store connection to the temporary WAL file, not a reused Python service. Expired PROCESSING becomes UNCERTAIN; neither old operation nor old paid RPC can be claimed again. A started intent keeps its budget and launch blocked after owner STOP. An injected SQLite trigger failure at marker deletion rolls back draft, policy, outbox status and expense together.

## Independent review of the main engineer's fixes

Reviewed PREPARE/COMMIT and preflight changes in listing_launch.py, actual PaidPromotions guards/intent/receipt flow, Store lease recovery and late evidence, GiftService reservation/payment boundary, gift outcomes, MTProto pay/receipt and refund/notice guards. PREPARE observation cache refresh does not authorize spending. Local COMMIT is synchronous; a BEGIN IMMEDIATE transaction serializes budget reads with reservation writes. Uncertain expenses are counted and block new requests. No new external mutation path or payment retry was introduced.

The premium preflight revision normalization is narrowly justified by the schedule enable committed with that same operation: current revision must match the queued payload first, and the full budget (excluding only enabled) must match readiness. An intervening owner stop/save increments revision and blocks dispatch. Restoring the readiness revision for comparison does not grant a stale policy.

Receipt remains independent: PaymentResult alone does not establish delivery. Gift steps are keyed by order and position, exact recipient/gift/reference is checked in a separate history read, and SPENDING/UNCERTAIN never permits a blind repeat. Refund guards reject any paid intent or unknown gift outcome. Missing receipt is not non-payment evidence.

## Remaining boundaries and handed-off issue

- No actual premium charge, Playerok refund, buyer message, gift payment or working SQLite/session modification was performed by this reviewer.
- Playerok's input has no confirmed maxAmount field; fresh price and exact receipt checks do not create an atomic server-side spending ceiling.
- Playerok and Telegram have no shared transaction. External manual cancellation/refund or clock skew cannot be eliminated by local SQLite.
- Readiness digest includes observed balance: an unrelated authorized balance change can conservatively invalidate an otherwise affordable launch (P2 availability issue).
- Handed off a newly observed product/safety interaction: default gifts:stopped=true has no standalone resume path, and existing GiftService.arm clears that global stop after awaited preflight. A first-draft launch needs an explicit resume path; concurrent STOP must not be undone by an older arm. Main engineer owns reproduction/fix.

## Review verdict

**NOT READY for unconditional live execution at this checkpoint.** The local release/authority defects above are corrected and the payment/UNKNOWN invariants remain intact in tested paths. Before the single isolated 15-Star Heart test, the main engineer must close the STOP/resume interaction, finish the full suite and independent journey acceptance, and obtain fresh real payer/recipient/catalog/price/balance/route proofs under an exclusive session lock. No permission is inferred from test counts. This review does not authorize any real Playerok operation.

### Final related regression result

Gift safety audit + gift bundles + thread calls + stock auto cycle + new takeover release checks: **92 passed in48.05s**, exit0. These include mock lost ACK, paid-intent/restart, session lifecycle, ordered gifts, receipt/refund guards and legacy stock recovery. No live call or financial operation was made. Main engineer retains responsibility for the final full-suite result and STOP/resume handoff.

### Final independent STOP/resume and first-sale review

Reviewed the new explicit Gifts resume confirmation and stop_revision fence. A newer STOP increments the durable revision and disarms listings in the same transaction. Resume checks the screen's revision after awaited refresh; arm captures before network reads and rejects a changed revision before enabling. FSM confirmation is consumed before the awaited service call. No new payment RPC is introduced. No additional reproduced STOP/resume defect found in this pass.

A final first-sale fault injection is RED: tests/test_takeover_first_sale.py, exit1, 1 failed in6.91s. Through actual Dispatcher create, atomic launch, publication, Synchronizer and GiftWorker, a valid server PAID date with second precision in the arm second is imported but creates no gift job, because truncated created_at < microsecond armed_at. This is a demonstrated supported-input boundary, not a claim that the real server always truncates dates. No actual server order was created.

Handed to the main engineer for disposition. A blanket floor/tolerance in future_order would weaken historical-order exclusion and is rejected. Any precision handling must be limited to proven newly created, first-publication launch identity with matching account/item/delivery revision and durable source-create provenance, while generic existing-lot arming keeps its strict historical cutoff. This checkpoint does not claim that the first-sale issue is fixed.
