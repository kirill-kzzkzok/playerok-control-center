# Playerok category position evidence — 2026-11-05

Owner contract: threshold refers to the listing header «Звезды: N / total».

## Confirmed source

Read the production listing HTML and its served public frontend chunks, without mutations:

- `https://playerok.com/_next/static/chunks/pages/products/%5Bslug%5D-e131581a98c334f5.js`
- `https://playerok.com/_next/static/chunks/pages/_app-a42b11efeef6f55e.js`

The listing-header component reads `c=t?.sequence??null`, uses category name, and renders the sequence followed by `/` and `countItems`. `priorityPosition` is NOT used by this component. The count query is `countItems(filter: $filter)` with the listing category ID and status `[APPROVED]`. This establishes the field mapping without a rank conversion formula. The existing pinned PlayerokAPI parser already exposes `sequence`; no new HTTP client or scraping dependency is necessary.

The independent published `playerok-requests-api` 0.1.6 wheel was inspected as text (not installed/executed). Its `get_item_positioninfind` reads `data.item.sequence` from a persisted `item` query. This is corroboration; the served website code is the primary evidence.

## Two live read-only observations

2026-11-05, initial reads approximately 18:20–18:30 UTC:

| Product | Existing get_item sequence | priority_position |
|---|---:|---:|
| Мишка [TXE] | 329 | 10 |
| Сердечко [VPM] | 320 | 10 |

Repeated at 18:37:36 UTC:

| Product | Existing get_item sequence | priority_position |
|---|---:|---:|
| Мишка [TXE] | 330 | 10 |
| Сердечко [VPM] | 321 | 10 |

The category count read returned 4506 at the earlier observation. Rank and total are live values and cannot be compared across times as immutable facts. No promotion was purchased.

## Verification boundary

PASS: both current listings return sequence values, the served frontend directly uses that field for the requested header, and the production adapter exposes it unchanged. The previous 10 was the wrong source for this owner contract.

NOT VERIFIED: visual observation of both authenticated owner cards. The temporary public browser was unauthenticated and hid the owner-only counter. Attempt to access Chrome ended because the computer-use tool could not confidently determine the current URL. Authenticated HTML did not contain a usable SSR counter. We did not claim a screenshot match, log in, modify browser settings or bypass this restriction.

## Runtime contract

Position-mode observations now use only `item.sequence` with positive-integer validation. No fallback to `priority_position`. Missing rank blocks position-based purchase with a readable reason. Historical observations retain their original source and are excluded from the new two-read decision. Two fresh, correctly spaced category-rank observations are still required. Exact approved tariff, owner authority, budgets, cooldown, STOP, RESERVED/UNCERTAIN and Outbox fencing remain mandatory.

Regression evidence: seven new RED cases failed on the old source. The full promotion profile passed 85 tests (exit 0, 26.25s), including two eligible real scheduler targets, one purchase each, old-source isolation, malformed/missing sequence, exact tariff checks and no blind retries.
