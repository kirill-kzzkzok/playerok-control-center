# Проверки

Начальное состояние: тестов и исходников в рабочем репозитории не было.
Результаты добавляются после фактических запусков, не по наличию кода.

Live-публикации, платежи, сообщения покупателям и изменения рабочих лотов не выполнялись.

## Запуски

- Базовые контрактные тесты + основа: 15 passed (после исправления отсутствующей requests).
- Категории/цены/изображения: 12 passed.
- Основа + финансы: 38 passed.
- Inbox/outbox, параллельные резервы, падения: полный suite 51 passed.
- FSM полного локального мастера, owner races, рендеринг и аналитика: 57 passed.
- Расширенные операции создания/публикации/редактирования/удаления добавлены; следующий запуск ещё не зафиксирован.

Команда: `.venv/Scripts/python -m pytest -q`.
Mock-ставки и данные тестов явно синтетические; они не используются как реальные комиссии.

## Live без записей

`.venv/Scripts/python -m app.diagnostics`: Worker getMe GET=200 ok=true,
POST=200 ok=true; неверный токен=401. Direct Telegram: ProxyError.
Playerok: авторизация, профиль и чтение games/items/orders/chats успешно.
Steam schema фактически вернула RANGE и TEXTAREA; upstream их терял → enum исправлен.
Локальные отчёты исключены из Git: data/live_diagnostics.json, data/live_schema.json.
Публичные bundles сайта прочитаны без обхода защиты и без отправки cookies.
Ни TLS verification, ни проверки безопасности не отключались.

## Дополнительная контрольная точка

- Расширенные mutation mock-tests: полный suite **63 passed**.
- Уведомления и transport guards: **69 passed** в полном suite.
- `ruff check app tests`: All checks passed; `pip check`: No broken requirements found.
- Реальный runtime: polling через Worker, getWebhookInfo без активного webhook,
  два pending updates обработаны. В SQLite закреплён один владелец, сохранено 217 заказов.
- Polling имел timeout сети; backoff отражён в логах. Загрузка Telegram-файлов не проверена.
- Live createItem: HTTP 400/BAD_REQUEST по правилам товара — **не успешный live-тест**.
  Уникальное имя не найдено при полной доступной read-only сверке своих лотов.
  Фото/редактирование/публикация live не подтверждены. Автоповтор технического объявления
  заблокирован. Платежи не выполнялись.

Полная приёмка PROJECT_SPEC.md ещё не пройдена.

Фильтры и пагинация заказов, безопасный поиск SQL, HTML и multi-value roundtrip: полный suite **72 passed** (19.79 s).

## PHASE 2 — расширенные проверки

Команда: `.venv/Scripts/python -m pytest -q` → **127 passed in 21.34s**.
`ruff check app tests` прошёл; последующее форматирование завершилось без ошибок.

- tests/test_wizard_attachments.py: реальная Pillow-обработка PNG/JPEG, ограниченный buffer,
  preview/replace/delete, сохранение файлов при конфликте ревизий, восстановление FSM,
  пустые схемы, пагинация, отрицательный индекс, запрет чужого файлового пути, multi-select.
- tests/test_telegram_integration.py: реальный aiogram Dispatcher/Router/Middleware/SQLiteStorage
  с mock Telegram session; полный локальный мастер с download фото, рестарт диспетчера,
  запрет повторных submit при read-only, отказ чужим callbacks и регистрации из группы.
  Это **не** live-проверка Telegram download/upload.
- tests/test_target_lock.py: гонка нескольких SQLite-соединений и блокировка товара при UNCERTAIN.
- tests/test_upload_cleanup.py: защита активных/неопределённых операций, безопасная очистка
  успешных загрузок, отказ от удаления при повреждённых ссылках; БД/история не удаляются.
- tests/test_operations.py: замена фотографий только с актуальными attachment ID, реальными
  байтами файла; проверки цены/названия/описания на границе mutation.
- tests/test_category_contracts.py: сериализация private comment в input.comment,
  обязательность данных, ограничения официальной формы/UTF-16, приоритет ставок,
  floor-округление оценки дохода.
- tests/test_incomplete_api.py: None/неполные объекты, неполный pageInfo, повторяющиеся курсоры;
  непроверенная страница не передаётся синхронизатору.

Read-only: data/live_phase2_schema.json фиксирует успешную авторизацию/категорию без
obtaining/dataFields и **mutations=0**. Реальные рабочие товары не изменялись.


## Условия, редактирование полей и скидки

Полный suite: **151 passed in 13.87s**; ruff чист, pip check без нарушений.

- test_agreements.py: 8 сценариев — полные страницы условий, read-only запрет,
  явная очередь, повторные callbacks, сверка, изменение текста, частичный timeout,
  чужая категория и GraphQL input через mock HTTP. Live-принятие не запускалось.
- test_item_fields.py: 5 сценариев — edit draft и запрет create, сохранение неизвестных
  атрибутов, приватность, FSM restart, изменения до enqueue/execute, владение/editable,
  сериализация comment.
- test_discounts.py: 10 сценариев — диапазон/5%/floor/снятие, неизвестная rawPrice,
  отсутствие выдуманной старой цены, свежая проверка и подтверждение цены в ответе.
  Только изолированные API; рабочие лоты не изменялись.
- test_transport_guards.py: operationName не позволяет скрыть платную mutation.

Live read-only: data/live_conditions_readonly.json — авторизация/соглашения прочитаны,
mutations=0. Introspection типов input → HTTP 500, не повторялась и не считается успехом.
Public bundles редактора/вывода: 10 успешных GET без авторизационных cookies.
Условия реального аккаунта, объявления, сделки и деньги на этом этапе не изменялись.

Финальная проверка контрольной точки: **151 passed in 11.94s**; JUnit: `data/phase2_conditions_results.xml` (вне Git). Ruff/pip check/secret scan прошли.


## PHASE 3 — вывод, себестоимость, PREMIUM preview

Полный suite: **181 passed**. Отдельный JUnit сохранён в `data/phase3_finance_results.xml`.
Ruff и pip check прошли в ходе контрольной точки; финальные проверки повторяются перед commit.

- test_withdrawals.py: проценты/минимум/лимиты, +1 USDT с актуальным FX, отсутствие
  выдуманного курса, неизвестные/устаревшие условия, слишком большая сумма Decimal,
  расчёт себестоимости/размещения отдельно, точный GET-контракт exchange rate,
  missing raw financial fields не трактуются как явный null.
- test_finance_panel.py: сохранение способа и cost после restart, разграничение владельцев,
  локальная cost для SOLD без рыночного редактирования, multi-step мастер, безопасный
  финансовый журнал, миграция v1→v2, PREMIUM quote без платежа и повторного списания.
- test_placement_basis.py: quote по rawPrice при скидке, отказ при её отсутствии,
  бесплатная публикация требует свежих обеих цен.
- test_telegram_integration.py: настоящий Dispatcher/Router/Middleware/FSM проходит
  финансовый UI на mock Telegram; чужой callback не читает API, outbox и реальные
  financial_operations остаются пустыми.
- test_finance.py: время источника показано Europe/Moscow с переходом даты.

Live read-only `data/live_withdrawal_quotes.json`: сессия авторизована, 5 способов вывода,
реальный курс USDT_RUB получен, расчёт для явно условной суммы 2000 RUB; **mutations=0**.
Не было реального вывода или сверки фактического settlement. Поэтому округление/поступления
не объявлены подтверждёнными, все такие суммы обозначены как оценки.
Рабочая локальная БД успешно мигрирована с backup: 217 заказов и 1 владелец сохранены.

Финальный запуск финансовой контрольной точки: **181 passed in 12.79s**; ruff/pip check чистые, JUnit: data/phase3_finance_results.xml.


## PHASE 4 checkpoint (2026-10-30)

196 passed in 11.01s; JUnit: data/phase4_sync_results.xml. Ruff clean; pip check: no
broken requirements. Secret scan of tracked and nonignored files clean; actual
Telegram token, full Playerok header and long cookie values checked without output.

test_sync_recovery.py: bounded history, refreshed head, durable cursor after database
close/reopen, partial-page interruption/replay, network failure retaining checkpoint,
looping cursor rejection before ingest, independent failed chats, round-robin fairness,
message updates without duplicate events. test_chat_panel.py: durable navigation,
stale callbacks, escaping/size limits, deleted message redaction and image counts;
viewing history creates no inbox events. test_notifications.py: self messages beyond
200-entry batch cannot starve a buyer notification. test_operations.py: mark_read
duplicate confirmation, verified chat/counter and UNCERTAIN on incomplete responses.

Live read-only: data/live_sync_readonly.json; 24 chats, 4 checkpoints, 217 preserved
orders, 1 preserved owner; BACKFILL, errors={}, mutations=0. Backup taken before v3.
This was one bounded cycle, not proof of complete historical coverage. No Telegram
notifications or buyer sends were run in this probe. mark_read and paging Telegram UI
are covered offline; live mark_read was not performed. WebSocket, quick replies,
photo transport, notification update after reply and manual UNCERTAIN resolution
remain pending alongside the rest of the original specification.


## PHASE 4 — replies checkpoint (2026-10-30)

**208 passed in 11.64s**, JUnit data/phase4_replies_results.xml; Ruff clean, pip check clean.
Tests cover owner isolation, optimistic revisions, 20-template limit, UTF-16 validation,
atomic/verified notification mapping and rollback, duplicate confirmation, complete
escaped preview, durable template FSM, unknown reply outcome, edit retry/backoff without
buyer resend and navigation back to a previously viewed chat page.

Real aiogram Dispatcher/Router/SQLiteStorage test: opening reply preserves the original
notification; manual input/confirmation survive a fresh dispatcher; double submit creates
one durable action/link; a mock successful executor updates the original Telegram ID with
the original question and remote answer ID. No real Telegram or Playerok sends performed.

Local migration evidence data/replies_migration_check.json: schema 1/2/3/4, 217 orders,
1 owner retained, 0 external requests/mutations; backup before-replies-v4 taken first.
Live photo transport, WebSocket, manual UNCERTAIN resolution and remaining specification
are not accepted by this checkpoint. No claim of full project readiness.


## PHASE 4 — chat images checkpoint (2026-10-30)

**223 passed in 13.28s**, data/phase4_images_results.xml; Ruff/pip check clean.
test_chat_images.py verifies exact multipart upload path and client ID, guarded
createChatMessage JSON imagesIds, disabled writes, upload journal before send, success
confirmation, uncertain upload/send/incorrect chat/expired upload/missing returned images,
no automatic replay, local file protection and cleanup, all paths checked before upload,
remote image size/host/redirect/content validation, deleted/stale media rejection.
Real Dispatcher integration verifies a photo/document reply with caption: normalized
preview, no outbox before confirmation, durable state across dispatcher restart and
one outbox after duplicate confirmation. All Telegram/Playerok sends were mocked.

Current unauthenticated official bundle GET: HTTP 200; upload/input/imagesIds/client ID
contract confirmed, raw source saved .vendor/public_web/chat-current.js; metadata and
SHA in data/chat_image_contract_check.json. No live temporary upload, message or paid
operation was performed. v5 local migration backed up first; schema 1–5, 217 orders,
1 owner preserved (data/chat_images_migration_check.json). Photo UI handles one image per
confirmed reply; automatic Telegram album aggregation remains outside this checkpoint.


## PHASE 4 — UNCERTAIN resolution checkpoint (2026-10-30)

**243 passed in 31.20s**, JUnit data/phase4_resolution_results.xml; Ruff/pip check clean.
test_reconciliation.py covers positive read evidence, wrong sender/text/date/deletion/
image count, bounded negative search without resend, owner/snapshot guards, transactional
delivery confirmation, rollback on storage failure, quarantine after STOP_UNCERTAIN,
read-state refresh, racing independent SQLite connections, preserved photo evidence and
upgrade backfill exactly once. Evidence omits private message/stock text.

Real Dispatcher/FSM test covers preview, owner comment, restart, separate confirmation,
duplicate click and foreign callback with writes disabled. Notifications tests prove
alerts are independent of initial sync, durable recovery deduplication, manual outcome
labelled as owner decision, and suppression of a resolved alert before send. No real
Telegram/Playerok messages or resolutions were performed.

Local migration v6: backed up first; schema 1–6, 217 orders and 1 owner preserved,
0 UNCERTAIN/0 decisions (data/resolution_migration_check.json). No external requests.
Positive photo evidence verifies count only; file contents require owner inspection.
Other mutations cannot be declared successful here. STOP_UNCERTAIN preserves unknown
external outcome and blocks automatic replay; inventory/photos remain quarantined/protected.
WebSocket, complete order cards/actions and later specification phases remain pending.


## PHASE 4 — realtime checkpoint (2026-10-30)

**256 passed in 35.26s**, JUnit data/phase4_realtime_results.xml; Ruff/pip check clean.
test_realtime.py covers scoped subscribe frames, no mutation queries, hint coalescing,
raw-message omission, invalid/foreign events, missing pong and absolute timeout despite
native transport activity, cancellation/resource cleanup, durable restart, revision
change during API await, request backoff/fairness, partial schema failure and reconnect.
An actual local aiohttp server exercises the client handshake and redirect guard;
credentials are never sent to the redirect handler. Other Telegram/operation tests pass.

Live probes were read-only: first vendor query attempt closed 4500; isolated minimal
userUpdated returned a union-field validation error (data/ws_schema_probe.json).
Inline fragments fixed that. A 45-second probe after the outer heartbeat-deadline fix
received connection_ack and JSON pong, with no subscription error in the observation
window (data/ws_heartbeat_fixed_probe.json). TimeoutError is the intentional end of the
bounded probe, not a claim of server failure. New buyer events were not induced or
received: events_verified=false, mutations=0, telegram_sends=0. Do not claim full event
flow acceptance from an idle connection. Earlier failed probes remain available.

Local v7 migration followed backup; 217 orders/1 owner preserved. A real durable catchup
request ran bounded API reads with errors={}, pending=0, BACKFILL/backlog=46; evidence
 data/live_realtime_catchup_readonly.json. This proves processing of the request, not
complete historical coverage. No bot was left polling after the probes.


## PHASE 4 — order management checkpoint (2026-10-30)

**279 passed in 38.11s**, data/phase4_orders_results.xml; Ruff/pip check clean.
test_order_management.py covers proper native SDK keyword/persisted GET contract,
FAILED parsing, one unique delivery after duplicate confirmation, no updateDeal,
10 forbidden/unverified sale states, owner/preview/closed chat, readonly guard,
rollback/timeout after reserve, quarantine, empty-stock alert dedupe, local note
revision/isolation/search escaping, cache without secrets, nonregressing states,
problem reopening and suppression of obsolete paid notifications.

Real Dispatcher integration covers card/local problem, persisted confirmation across
restart, duplicate click, local SQL buyer search, stock preview without secret echo,
readonly delivery refusal and one reservation/outbox after enabling only the mock account.
Existing reservation/recovery/reconciliation tests remain green with transactional history.

Live first probe failed due to our incorrect id keyword (not an upstream schema change).
All four callers now use deal_id. The corrected native read succeeded for a real sale:
OUT/CONFIRMED, owner matched, both flags bool, 3 logs; no mutations/Telegram sends.
Evidence data/live_order_card_fixed_readonly.json; original failure preserved separately.
41 static read-call keyword contracts checked against SafeAccount signatures, 0 invalid.
Backup before v8; 217 orders/1 owner preserved. No real codes were added or delivered.
New fulfillment-report requirements were found in public official source; no unchecked
SENT/CONFIRMED/reportDealProblem mutation is claimed implemented or accepted here.


## PHASE 4 — seller fulfillment checkpoint (2026-10-30)

**305 passed in 35.36s**, JUnit data/phase4_fulfillment_results.xml; Ruff/pip check clean.
test_fulfillment.py covers legacy/draft/fields queue+worker+read verification, strict
SENT instead of CONFIRMED, unknown role/state, attestation, owner/read-only and stale
requirement guards, disappeared provider, no write replay after timeout/read-after-write,
atomic reservation rollback under target lock, manual read reconciliation, form validation,
native REST body/path/permit/redirect guards, parser role preservation and form restart.
Real aiogram Dispatcher test covers checkbox, readonly refusal, persisted confirmation,
one outbox on double submit and foreign callback denial. All writes were mocked.

Cross-thread REST regression initially deadlocked because permission was checked inside
an RLock held by the other thread. Those two test processes were interrupted; the guard
is now checked before locking as well as inside, and the regression is bounded by a
one-second timeout. The final full suite above includes the fix and passes.

Live evidence data/live_fulfillment_readonly.json: GET requirement=none, GET entry=absent,
GET providers HTTP403. No rights bypass/fallback, no actual SENT or POST. Existing 217
orders/1 owner preserved. This proves the read endpoints for this ordinary account,
not live write acceptance or official-provider access. The journal verifier checks a
summary (state/Telegram/time), not an echo of all supplier fields. Full live acceptance
and remaining original requirements are still pending.

After final UI wording adjustments, the fulfillment/Dispatcher subset passed again: **33 passed in 15.82s**. Format and actual-secret scans clean.


## PHASE 5 — desired delivery policies checkpoint (2026-10-30)

**316 passed in 37.80s**, JUnit data/phase5_delivery_policies_results.xml;
Ruff and dependency checks clean. test_automation.py verifies policy validation,
owner/seller isolation, CAS conflict, persisted edit FSM, stale nonce, stop-all,
readonly API simulations of PAID stock delivery, no reservations/outbox, safe audit/run
payloads and stock/local-problem/prior-operation refusal. Real aiogram Dispatcher test
covers configuration, restart, save, global stop and foreign callback. No runtime
chain is claimed by these tests; greeting/fixed text/after/SENT desired settings are
not automatically executed yet.

v9 backup and migration evidence data/delivery_policies_migration_check.json: schema
1–9, 217 orders, 1 owner, 0 configured policies; external_requests=0/mutations=0.
No live shop settings changed. Existing delivery/fulfillment/read-only tests remain green.
Further automation implementation and full specification acceptance remain pending.


## PHASE 5 — delivery chains checkpoint (2026-10-30)

326 passed in 32.10s; JUnit data/phase5_chains_results.xml. Ruff/pip check clean.
After final reuse of verified chat ID, targeted chains/Dispatcher suite: 19 passed
in 7.39s. Tests cover ordered stock/text sends, restart/no replay, unknown greeting
blocking all subsequent steps, quarantine, returned sale after greeting, global stop,
policy change during API await, disabled runtime/stale revision, manual reserve
rollback and separate fulfillment requirement. All external sends were mocked.

Migration v10 backup evidence data/delivery_chains_migration_check.json: 217 orders,
1 owner, 0 chains, writes/runtime false, external_requests=0/mutations=0. No live
chain was created. Registration is explicit per order; new-order automatic enrollment,
promotion controls and broader acceptance remain pending. Unknown chain states are
not retried; manual resolution of one step does not silently resume the sequence.
Secret scan clean.


## PHASE 5 — promotion settings checkpoint (2026-10-30)

341 passed in 33.29s; data/phase5_promotion_settings_results.xml; Ruff/pip check and
actual-secret scan clean. Tests cover policy CAS/owner guards, include/exclude/all,
night timing and two spaced observations, invalid intervals/bools/money, native mock
quotes via raw_price, ledger budget limits and automatic local disable, inactive/busy
items, separate evaluation ledger, restart/stale buttons and real Dispatcher settings.
No paid executor exists here; no paid outbox/expense created by dry-run.

Backup + v11 local migration: data/promotion_settings_migration_check.json; 217 orders,
1 owner preserved, 0 configured policies and expenses, external_requests=0/mutations=0.
No live market mutation/charge was tested. priority_position is explicitly scoped to
priority placement; global search rank is not proven. Periodic scheduling, full target
listing and asynchronous budget guard notices remain pending along with the full goal.


## PHASE 5 — promotion worker checkpoint (2026-10-30)

351 passed in 34.34s; data/phase5_promotion_worker_results.xml; Ruff/pip and actual
secret checks clean. Tests cover disabled no-network behavior, persisted schedule,
no replay before due, selected targets and exclusion, multi-page cursor continuation,
lease fencing/expiry, per-item backoff, policy revision change, discovery Retry-After,
shutdown and durable budget alerts outside historical baseline. No paid executor used.

v12 backup/migration probe data/promotion_worker_migration_check.json: 217 orders,
1 owner preserved, DISABLED, processed=0, external_requests=0/mutations=0. The worker
was invoked once locally against disabled policy, not left running. It does not charge
or record dry-run as a real promotion. Further full target queue UI and broader goal
acceptance remain pending.


## PHASE 5 — automation queue UI checkpoint (2026-10-30)

359 passed in 40.65s, JUnit data/phase5_queue_ui_results.xml; Ruff/dependencies/format
and actual-secret scan clean. Tests cover 27-record policy/target pagination without
missing/duplicate entries, escaped human-readable evaluation, full owner restrictions,
FSM stop confirmation after restart, stale status snapshot rejection, active lease
reschedule refusal and no implicit policy enable/outbox. Inflight stop regression
proves an accepted external result can still finish SUCCEEDED/DELIVERED after local
CANCELLED; it is not falsified or returned to available stock. Real aiogram Dispatcher
exercise covers chain inspection, restart, double stop and foreign callback denial.

All chain/queue changes occurred in temporary test databases. No working database
migration was needed. No Playerok/Telegram live requests, buyer messages, listing
changes or paid actions performed. Automatic unknown-chain resume is deliberately
absent; resolving a step does not authorize new sends. Remaining original goal
requirements and final acceptance are still pending.


## PHASE 5 — future-order enrollment checkpoint (2026-10-30)

374 passed in 38.26s; data/phase5_enrollment_results.xml; Ruff/dependencies/format and
actual-secret scan clean. Tests cover old/missing/naive/future server dates, timezone
comparison, duplicate registration, global disabled/baseline gates, explicit opt-in,
persistent read backoff/Retry-After, crash after chain commit, policy revision change,
positive API date revalidation, leases and last unique stock exclusion. Real Dispatcher
exercise proves opt-in requires a separate confirmation restored after restart, with
one policy save and no chain/outbox merely from configuring it.

Local backup + migration v13 probe: data/enrollment_migration_check.json; 217 orders,
1 owner retained, 0 chains/enrollments, DISABLED, external_requests=0/mutations=0. No
live sending or modification of shop settings performed. Automatic SENT remains a
separate explicit Fulfillment workflow; mock acceptance is not live buyer delivery
evidence. Remaining original project scope and final acceptance remain open.

## PHASE 6 — profile/statistics checkpoint (2026-10-30)

**398 passed in 51.03s**, JUnit data/phase6_statistics_results.xml. Ruff check/format
(103 files), pip check and actual-secret scan clean; .env and PROJECT_SPEC.md ignored.
A final pairwise lint correction was separately rechecked by 4 smoothing tests.

New tests cover server-date/timezone rolling boundaries, future/missing/naive dates,
unknown pending vs paid amounts, prior-window unknowns, current-status cohorts,
RUB-only generic expenses, SUCCEEDED-only promotion sources, empty/populated PNGs for
all five views/periods, Dispatcher access/escaping/no API writes, durable bounded date
backfill, preserved dates after sparse list updates, revision/backoff retention,
shape-preserving curves with uneven intervals/zeros/extrema/exact observations.
Profile tests use the actual PlayerokAPI parser/normalizer contract, distinguish
available/withdrawable and incoming/outgoing counters, preserve zero values, check
corrupt-avatar initials, bounded public media transport/no redirects/no credentials.

All chart views and profile inspected visually; data/phase6_visual includes separate
synthetic demo and existing-cache fixtures. Temporal charts changed to smooth factual
curves per the user's explicit preference; no fake sinusoid is inserted into data.

Read-only working-cache probe: 217 orders, 216 missing upstream dates. The subsequent
live get() returned UNAUTHORIZED_OR_CHALLENGE, before detail reads; no dates backfilled,
no external mutations. Evidence data/phase6_statistics_probe.json and
 data/phase6_date_backfill.json; SQLite backup before-statistics-date-backfill.sqlite3.
Financial transaction reconciliation/actual receipts/commissions/historical profit and
remaining final live acceptance are still open. Passing mocks do not prove live writes.

## PHASE 6 — transaction journal (2026-10-30)

409 passed in 52.22s; data/phase6_transactions_results.xml. Tests use temporary SQLite,
actual native parser + normalization, mock page envelopes, genuine Dispatcher/middleware.
They cover account composite uniqueness, credential omission, enum forward compatibility,
CONFIRMED-only completed-time cohorts, exact timezone boundaries/future exclusion,
unknown monetary/currency/direction/date omission, non-overlapping category labels,
no cost/profit/commission fabrication, sparse dates and rollback, bounded resume/head
refresh, partial ingest/replay, account change before empty-page checkpoint, durable
429 backoff and UI access via existing middleware. Existing sync tests verify extracted
PageScanner preserves original order/chat behavior. No production API mutation tested.

Local v14 migration after SQLite backup retained 217 orders and 1 administrator, with
zero transaction rows; data/transactions_migration_check.json. External requests=0,
mutations=0; read-only/automation-off configuration unchanged. Five public official
wallet bundles HTTP 200 support the journal data contract; authenticated live transaction
loading is still unverified due to the prior UNAUTHORIZED_OR_CHALLENGE response.

## PHASE 6 — historical order finance (2026-10-30)

418 passed in 37.46s; data/phase6_order_finance_results.xml. Nine new tests exercise explicit
unknown vs zero costs, owner review/CAS, negative results, matching receipt ambiguity/currency/
status, linked priority/withdrawal retention capacity, allocation replacement/removal, concurrent
SQLite connections, invariant against duplicate shared expenses, review surviving repeated
observed_at polls but invalidated by economic updates, owner/period isolation, real Dispatcher
with SQLite FSM restart, duplicate commits and no external API/outbox/ledger mutations.

Working v15 migration after backup retained 217 orders, one owner and no historical costs
or allocations fabricated; data/order_costs_migration_check.json. Actual authenticated financial
loading still needs the priority diagnosis requested by the user. Local owner-declared profit
is not an audited claim about every shop transaction.

## Priority auth diagnosis / authenticated financial loading (2026-10-30)

427 passed in 86.15s; data/auth_and_finance_live_results.xml. Auth-specific tests retain
exact 401/403 status and fixed endpoint without query values; redact credentials and JWT;
strip challenge scripts without execution; omit successful profile bodies and mutation
error messages; execute native viewer/user/transactions through mock HTTP; stop after
challenge with one request and no bypass. Existing transport guards still pass.

Real new-header checks: POST viewer, GET user and GET transactions to
https://playerok.com/graphql all HTTP 200 JSON (server ddos-guard). Evidence
 data/playerok_auth_diagnostics.json, including comparison with official _app source.
Old failure had no trace beyond merged 401/403 code; precise historical cause unproven.

Read-only backfill after backup completed 462 transactions / 217 dated orders; 402
confirmed transactions, 181 confirmed orders, no unknown dates/amounts/currencies in
confirmed transaction aggregates. data/live_finance_backfill.json and
 data/live_finance_acceptance.json. Visible completed pagination is not proof of all
historical completeness. Costs/allocations remain 0; no profit fabricated.

Live missing props.deal_id exposed a journal/order join gap: exact deal.transaction.id
is now accepted with strict account/type/direction/status/conflicting-link checks. Tests
cover absent props, contradictory deal ID and duplicate reuse of a receipt. Real history
PNG also exposed overlapping time labels/curves across missing years; calendar locators
and explicit gaps fixed it, with a rendering regression test. No live mutation, listing
change, buyer sending, fulfillment or payment performed. User now requires separate
permission for any later live writes; no such permission requested or implied here.

## PHASE 7 — dispatch boundary/recovery (2026-10-30)

447 passed in 56.03s; data/phase7_dispatch_results.xml. Targeted regression first
reproduced the policy stop/api-lock race (hello sent after stop); the final guard
blocks policy/runtime/problem/rollback/expired/recovered lease before HTTP. GraphQL
and REST transport tests simulate a stop during throttle wait and assert no network.
Existing executor/photo/fulfillment tests passed after explicit write-enabled mock
contract correction. No production writes were enabled for tests.

Late-ack worker/thread test recovers the lease during send, then captures successful
message ID without changing UNCERTAIN/quarantine or permitting another step. Tests
prove deduplicated late evidence, preserved owner terminal decisions, still-owned
finish conflicts propagate, and escaped evidence is visible via real Dispatcher.
HTTP mutation 401/403/429/500 is tested through SafeAccount mock transport with one
request/no retry. Concurrent genuine publish callbacks produce one publishItem;
overlapping policy scenarios commit one revision. Actual subprocess os._exit before/
after mock send demonstrates durable recovery without graceful close/finally.

ACCEPTANCE.md maps all sixteen required fault-injection scenarios to inspected tests
and distinguishes mock evidence from unaccepted live behavior. v16 migration after
backup retained 217 orders, 462 transactions, one owner and zero late observations;
data/dispatch_migration_check.json. External requests/mutations=0, writes/automation
false. Full project/live/soak/remaining spec acceptance remains open.

## Settings and notification preferences (2026-10-30)

458 passed in 53.44s; data/settings_notifications_results.xml. Ruff check/format
(117 files), pip check, actual-secret scan and ignored-secret-file checks clean.
New tests cover explicit boolean defaults/invalid values, per-owner isolation, revision
CAS/foreign owner denial, suppression of 201 messages without starving later orders,
no historical replay after enable, all urgent configurable categories before baseline,
mandatory operation_uncertain despite mute/corrupt settings, concurrent notification
workers, actual Dispatcher restart/stale callback/security/connections navigation.

Corrupt preferences produce SUPPRESSED_INVALID and a diagnostic marker for noncritical
events; critical uncertainty alerts remain deliverable. Existing sent notification
updates remain independent of category preferences. No working preferences were changed:
data/notification_preferences_local_check.json shows schema16/one owner/zero preference
rows, external requests=0/mutations=0, writes=false/automation=false. No live Telegram
send or Playerok operation executed. No new schema migration was needed.

Settings completeness remains partial: cleanup UI controls, autonomous rule-based
responses and fully automatic seller confirmation remain explicitly open in ACCEPTANCE.md.

## Draft history navigation (2026-10-30)

Targeted tests/test_drafts_panel.py + test_catalog.py: 7 passed in 4.58s. Tests cover
57 owner drafts across six pages, timestamp tie ordering, no duplicate/missing IDs,
unknown states, clamp after removal, owner-scoped SQL and direct panel access guard,
literal wildcard/injection handling, private field exclusion, actual Dispatcher
search/filter/restart/escaped input, old cancelled record access and callback limits.
No remote calls/outbox/mutations. Existing creation/wizard opener behavior retained.
Ruff check/format (119 files), pip check and actual-secret scan clean. No schema
migration or production settings change required. Full suite result recorded below.

Контрольная точка drafts: **462 passed in 64.51s**; data/drafts_pagination_results.xml; Ruff/pip/format/secret scan чистые.

## Order server chronology (2026-10-30)

471 passed in 62.00s; data/order_chronology_results.xml. Ruff/format (119 files), pip
and actual-secret/ignored-file checks clean. Regression first failed: naive/receive-time
rows appeared before newer actual server dates. Final tests cover backfilled older vs
newer orders, UTC offset ordering, equivalent instants, fixed fractional precision,
unknown/naive/malformed dates, UTC overflow and preservation of known date on bad poll.
Targeted catalog/durability 25 passed; existing order-management/statistics checks pass.

Working-cache readonly check: first 30 orders have known descending UTC server dates;
30 positions differ from the former receive-time ordering, 217 orders retained.
Evidence data/order_chronology_readonly_check.json; database_rows_rewritten=0,
external_requests=0/mutations=0. No migration or working listing operation performed.

## Explicit interface cleanup (v17, 2026-10-30)

483 passed in 88.55s; data/interface_cleanup_results.xml. Ruff/format (123 files), pip
and actual-secret/ignored-file checks clean. Targeted cleanup + existing UI/rendering:
18 passed in 13.52s. New tests cover age/batch bounds/current message/notifications/
other bot/foreign owner exclusion, exact snapshot/revalidation, forged IDs, persisted
per-message results, no retry after unknown/rejection/rate limit, cancellation/recovery,
ContextVar scope and metadata-only tracking through genuine session middleware, real
Dispatcher confirm/restart/cancel/stale/double commit without Playerok calls/outbox.

Official Telegram deleteMessage limit checked against https://core.telegram.org/bots/api#deletemessage;
local threshold 47h leaves a margin below 48h. No Telegram messages actually deleted.
Production v17 after SQLite backup retains 217 orders, 462 transactions, one owner and
zero tracked messages: data/interface_cleanup_migration_check.json. External requests,
Telegram deletions and Playerok mutations all zero; writes/automation remain false.
This proves mock implementation and local migration, not live deletion acceptance.

## Rule-based automatic replies (v18, 2026-10-30)

514 passed in 68.54s; data/auto_replies_results.xml. Ruff/format (127 files) and pip clean.
Tests exercise exact normalized phrase, explicit arming/default disabled/cutoffs, recent
aware server and first-inbox dates, self/admin/system/automatic/deleted/image/nonmatching
rejection, fresh PM participants/texting/message/continuation checks, dedup/parallel
workers/restart, policy/runtime/cache edit/human response while awaiting API lock,
Moscow-day limits across revisions/shared-chat cooldown, pending stock delivery exclusion,
read429 persistent backoff, unknown mutation no resend, owner/CAS/duplicate phrase/delete,
and real Dispatcher disabled creation/separate enable/restart/stale confirmation/escaping.
The initial journal insert placeholder bug was exposed by failing tests and fixed using
explicit column names; no failures concealed or security gates bypassed.

Production v18 migration after backup: 217 orders, 462 transactions, one owner retained;
zero rules/events, worker once DISABLED, no external requests or mutations.
data/auto_replies_migration_check.json. No live auto-reply/customer sending performed;
mock acceptance does not prove live sending. Production flags remain disabled. Existing
reply reconciliation/uncertain alerts and stock-chain tests still pass with added guards.

## Installed wheel / backup handle / same-tick cutoff / private remote (2026-10-30)

**518 passed in 86.96s**; data/installation_results.xml. Ruff/format (129 files), pip
check clean. New offline selfcheck tests deny Config.load/Python socket connect, validate
temporary schema/backup/reopen/PNG and a missing migration error. Regression proves
backup destination connection is actually closed and the file can be removed immediately.

Initial installed-wheel smoke and full suite exposed PermissionError: sqlite3 context
only commits, leaving the backup connection open on Windows. Fixed with closing; no
failure concealed. Another full-suite failure exposed same-tick old/new auto-reply cutoff;
new deterministic regression + atomic inbox-row boundary fixes it without clock sleeps.
Targeted autoreply/installation suite: 35 passed in 15.84s, followed by the full result above.

Final wheel SHA256 a625f40ed9a3943575d48ed30acb7cb22dbb59b90017a354fb6091dcba3caef0. Installed --no-index --no-deps into a fresh
ignored target and executed outside the repository with editable discovery disabled.
Both app/playerokapi origins verified under that target; all 80 application modules
imported, 17 migration files/schema1–18/packaged certificate/MIT license present, SQLite
backup and PNG checks OK. Report data/wheel_installation_check.json, exit=0. Dependencies
were the current verified venv, not a newly provisioned OS/Python environment. Wheel
secret scan CLEAN, no .env/PROJECT_SPEC/data included. Runtime credentials never read by
selfcheck; production state/API not exercised. The initial --no-build-isolation command
failed for absent setuptools; the normal isolated PEP517 build succeeded.

Private GitHub repository created per updated user objective:
https://github.com/kirill-kzzkzok/playerok-control-center, isPrivate=true. First push of
verified history succeeded; local/remote master matched 9980c4050a069125be7b20035f45a36fdb6bc4cf.
Before that push, 449 historical blobs scanned: known secrets=0/JWT candidates=0/private
paths=0; data/git_history_secret_audit.json. Current changes are scanned before push too.
Remaining project acceptance/live gaps are still open, mapped against all 26 §20 criteria.
