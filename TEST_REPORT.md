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

## Observed sale/withdrawal settlement reconciliation (2026-10-30)

535 passed in 92.63s; data/settlement_reconciliation_results.xml. Targeted settlements/
order_finance/transactions: 39 passed in 13.58s. Ruff/format (131 files), pip check and
actual-secret/JWT/ignored-path scan clean. New tests cover exact gross-fee=credit vs
percentage-like root values, zero sale fee, invalid/missing/foreign/nonconfirmed/date
sources, ambiguous/refunded receipts, period/owner/unknown-cost independence, fee changes
revoking reviewed profit without double deduction, explicit positive withdrawal net,
zero/missing/impossible net exclusion and actual Dispatcher/no external API/outbox.

Working cached API snapshots: 181 confirmed sale orders reconcile exactly, zero mismatches
or unattributed receipts. All 10 confirmed withdrawals lack explicit net RUB, so remain
unknown; root fee=0 is not evidence of no withdrawal commission. Read-only probe saved
 data/settlement_reconciliation_check.json; external requests=0, mutations=0, business
rows rewritten=0. No schema migration or live action performed; production flags false.
This does not prove complete shop history/bank receipt/current fee schedules or replace
missing historical costs. Sale fee display is a narrowly reconciled observed amount.


## Worker supervision checkpoint — 2026-10-30

Full suite: **542 passed in 87.02s**, data/worker_supervision_results.xml.
Targeted: 13 passed in 10.10s (7 new supervision regressions and notification suite).
Ruff check/format: clean, 133 files; pip check and git diff --check clean.
Actual-secret/JWT scan: clean; .env/spec/data/logs/build ignored; writes/automation false.
Offline selfcheck: OK, 82 imports, 17 migration files/schema 1–18, temporary backup
reopened, profile/chart PNG generated, external requests zero. Report in
 data/worker_supervision_selfcheck.json. Working bot/database not started or migrated.

Fault boundaries: exceptions and unexpected normal return restart a fresh loop after
backoff; persisted retry deadline survives cancellation/reconstruction; shutdown never
restarts; coalesced incident emits one mandatory notification before baseline despite
invalid category preferences. Persistence outage logs types only and leaves supervision
active. Critical injected outbox finish failure after mock ACK leaves PROCESSING, then
lease recovery yields UNCERTAIN with attempts=1/executor await_count=1, no resend.
No live buyer send, paid action, API mutation, challenge bypass or watchdog claim.


## Account binding checkpoint — 2026-10-30

Full suite: **562 passed in 92.21s**, data/account_binding_results.xml.
Twenty new regression cases: first/repeated bind, concurrent SQLite connections,
legacy/canonical/transaction/seller conflict, unattributed queue, invalid identity and
corrupt settings; actual run startup with fake account/bot stops before backup/workers
on different account or failed authorization (even with a stale nonempty client ID).
Existing saved account and pending operations are not reassigned, resources are closed.
Ruff check/format clean (135 files), pip check and git diff --check clean. Actual-secret/
JWT scan clean; .env/spec/data/logs/build ignored; runtime writes/automation false.

Read-only working cache probe: known current identity and consistent saved IDs,
transaction accounts and known order sellers; 217 orders/462 transactions. Ignored
 data/account_binding_readonly_check.json contains booleans/counts only. Requests=0,
database writes=0; no real startup, binding update, migration or buyer message. The next
normal authenticated startup will persist canonical binding after verification. This
proves a single-account safety barrier, not support for switching shops within one DB.


## Thread cancellation / resource shutdown — 2026-10-30

Final full suite: **571 passed in 88.34s**, data/thread_cancellation_final_results.xml.
Earlier intermediate: 569 passed in 96.08s; final suite includes added actual run
shutdown-order and auth_diagnostics cancellation tests. Nine new thread-specific cases.
Targeted first retry: 53 passed in15.83s; thread-specific then 8 passed in14.35s before
last auth diagnostic case. Ruff check/format clean137 files, pip check and diff clean,
actual-secret/JWT scan clean, private paths ignored, write/automation flags false.

Initial drain implementation hung on Python3.12 done-future fast path. Minimal script
with faulthandler reproduced starvation in drain; affected test/debug processes were
explicitly terminated. Explicit yield now lets completion callbacks remove finished
tasks/register observers; targeted and both subsequent full runs passed. No test was
bypassed or failure hidden. Thread barriers and MockTransport prove lock retention,
queued cancellation/no dispatch, cancellation during throttle with zero HTTP calls,
late ACK/error one request/UNCERTAIN/allowlisted receipt and no resend. Actual run
shutdown with fake services proves active call completion precedes account/session/DB
close. Read-only auth diagnostics also drain before close on cancellation.

No real network request, buyer message, write, database migration or production bot
start. Work does not claim a hard shutdown/request deadline: HTTPX20s is inactivity/
chunk timeout. Forced exit or unavailable persistence can lose late evidence; unknown
operations still do not retry. Legacy live_test helper remains outside this runtime
change and must be reviewed before any authorized future use; rejected create not retried.


## API shutdown admission — 2026-10-30

Full suite: **576 passed in110.24s**, data/api_shutdown_admission_results.xml.
Targeted before last closed-mutation test:43 passed in17.02s. Five new cases plus
actual run shutdown strengthened with late handler. Close before registry creation,
repeated close, queued lock/thread-pool entry, new mutation on closed API and close
at transport throttle all block method/HTTP; actual late handler sees API_CLOSING.
The in-flight call still drains before account/session/DB close. Cancellation/late ACK
regressions remain green. Ruff check/format137 files, pip check, git diff --check and
actual-secret/JWT scan clean; runtime writes/automation false. No live API request,
mutation, message, working database change or migration. This does not establish a
hard total deadline or acceptance of non-API UI/cleanup threads/legacy live_test.

GitHub sync resumed at checkpoint start: normal push advanced private origin from
2b5542e to99f7f60, resolving the prior three server500 failures without force push.


## Retired technical live-write scenario — 2026-10-30

Full suite: **594 passed in88.59s**, data/retired_live_test_results.xml.
Eighteen new cases; initial targeted57 passed in18.96s before final type/style and
mounted-transport check, all included in final full. Ruff check/format139 files,
pip check and diff clean. Technical listing create/edit/finally-delete was removed;
app.live_test only inspects a bounded local journal with allowlisted recorded facts.
Missing/invalid/old terminal/uncertain histories never rearm writes; original bytes
are untouched. CLI subprocess and no Config/client construction verified. Actual
historical journal SHA256 unchanged, recorded rejection=true/remote ID absent; sanitized
report in data/retired_live_test_readonly_check.json. These are recorded historical
facts, not fresh remote absence proof. No remote request, buyer send, write, paid action,
working database access or migration in this checkpoint.

Four native contract suites now import a tests-only permit helper guarded against real
default/mounted transport. Existing multipart/create/edit/fields/agreements contracts
remain tested; normal durable product execution code unchanged. No restored technical
listing or claim of live acceptance. A future authorized real-goods acceptance scenario
is still required; PHASE8 and the overall project remain open.


## Paid-order volume rankings — 2026-10-30

Full JUnit: **603 tests, failures=0/errors=0/skipped=0**, time113.135s,
 data/bestsellers_results.xml. Completed process handle was no longer available on
continuation; authoritative full XML inspected instead of restarting. Targeted27 passed
in45.13s. Nine new cases cover distinct native IDs/same names, count vs revenue,
unknown-only/partial/zero, paid statuses/period/date boundaries, latest name/offset/ties,
actual Dispatcher five periods, owner restriction, escaping/top15/length and no API/outbox.
Ruff check/format140 files, pip check, diff and actual-secret/JWT scan clean.
Working read-only cache:217 dated orders,183 paid/183 distinct native IDs; item/category
counts and unknown coverage reconcile. data/bestsellers_readonly_check.json contains
counts/booleans only. No database writes, API request, migration, buyer send or payment.
Counts represent paid orders by native listing ID, not inferred logical SKU/units;
category names are not a verified cross-game taxonomy. Existing revenue top remains.


## User list pagination checkpoint — 2026-10-30

Full suite: **610 passed in98.20s**, data/listings_pagination_results.xml.
Targeted listings/Telegram integration19 passed in13.06s. Seven new parametrized
cases validate items/chats restart/EOF, cursor cycle and malformed envelope checkpoint
preservation, empty continuation, stale nonce/wrong kind, callback byte limit/traversal
limit, genuine Dispatcher concurrent double click/filter reset/legacy/foreign access.
Only one next-page API mock call on concurrent click; no outbox mutation.
Ruff check/format142 files, pip check/diff and actual-secret/JWT scan clean.
Offline selfcheck OK:85 imports,17 migration files/schema1–18, temporary SQLite backup
reopen, profile/chart PNG; data/listings_selfcheck.json. External requests0, working
bot/database not started, no migration/live writes/payment. Page validation and durable
continuation do not prove complete upstream history. Old next-page callbacks are
intentionally rejected and require opening the list again.


## Item filters — 2026-10-30

Full suite622 passed in101.96s, data/item_filters_results.xml. Twelve new parametrized
cases validate name/price/category/priority settings, literal Unicode name, exact price
bounds, unknown vs zero/default, actual Dispatcher composition/restart/reset/owner and
invalid input preservation. SafeAccount+MockTransport checks category native request:
userId/status/category retained, no invented price/priority API parameters. After full
start, router UI caption and pre-save invalid-status/state-clear handling changed;
final affected tests19 passed in13.36s, data/item_filters_final_targeted.xml. Ruff check/
format144 files, pip/diff/secret scan clean. No working database access, network request,
mutation, message, paid action or migration. Per-page filtering is explicitly labelled;
category-name picker and global name/price/priority search remain unaccepted.


## Named item category picker — 2026-10-30

Full suite632 passed in281.47s; data/item_category_picker_results.xml confirms632 tests,
errors0/failures0/skipped0. Targeted29 passed in11.13s. Ten new cases: game cursor and
category pagination/restart/filter preservation, duplicate category names and fresh
membership disappearance, malformed/cyclic game envelope, wrong game/incomplete
categories, invalid/empty search, cancel and actual Dispatcher flow/foreign owner/stale
callback. Output observation was delayed; existing process/session and full JUnit were
checked rather than restarting. Ruff check/format146 files, pip check/diff/actual-secret
scan clean. No working DB access, API request, buyer send, mutation, payment or migration.
Previous manual-ID-only category UX limitation resolved in mock acceptance; no full
upstream history or live-write acceptance claim. Native ID entry stays optional.


## Item card and read-only gallery — 2026-10-30

Full640 passed in103.96s, data/item_card_gallery_results.xml; targeted27 passed in14.26s.
Eight new cases cover card API counters/zero/unknown/escaping, photo SQLite restart and
reorder by ID, fresh owner/item identity/hidden/removal rejection before download,
stale/bad-index no API and actual Dispatcher card/sendPhoto/owner/no outbox. Ruff check/
format148 files, pip check/diff and actual-secret/JWT scan clean. No working DB access,
real API/Telegram request, mutation, payment or migration. Existing Attachments.remote
security/cleanup reused; photo viewing uses a separate button. API deals counter is
not settled sales, and priority position is not global marketplace ranking. No live
photo-send or listing acceptance claim; general project acceptance remains open.


## Explicit item mutation receipts — 2026-10-30

Full670 passed in95.23s, data/item_mutation_receipts_results.xml. Targeted55 passed
in7.23s. Thirty new cases cover incomplete/nonpublished/positive publish statuses,
null/bool/false/malformed/wrong-ID deletion data, missing/naive/valid aware deletedAt,
and strict executor delete literal True. SafeAccount real pinned query on MockTransport
proves one request and no resend; invalid response produces UNCERTAIN + durable alert,
positive delete receipt persists only removed=true. Contract fixture now uses native
object/deletedAt shape. Ruff check/format149 files, pip/diff/secret scan clean.
Primary upstream source inspected online confirms discarded removeItem response;
source/inference documented in PLAYEROK_RESEARCH. No Playerok live request/mutation,
buyer send, payment, working database write or migration. No claim of live publication,
active-listing state after moderation or independent full deletion scan.


## First-sale MVP checkpoint — 2026-10-30

Full692 passed in59.41s, data/mvp_checkpoint_results.xml. Targeted69 passed in8.59s.
Previous ordinary-edit predicate tests19 were completed before assessment interruption
(689 passed); all retained in this final full. Added three connected MVP cases using
real local services/workers with mock remote: photo create/free publish/future order/
atomic unique reserve/restart/delivery/explicit owner fulfillment/readback; lost ACK
keeps quarantine/no resend/no SENT; bad create receipt no binding/publication. Create
success now requires DRAFT/current owner/category/text/price/photo-ID evidence, not
just returned ID. Ruff check/format152 files, pip/diff clean.

Fresh real read-only auth/profile/items/item/games/category/deals/chats15:16UTC all
HTTP200 and shape checked, no challenge encountered. Report data/mvp_readonly_probe.json.
Stars schema read15:19UTC statusOK, amounts/obtaining/buyer data/seller rules obtained.
Unicode console print failed after report save (cp1251); saved report inspected without
repeating API. No mutations/acceptAgreement/paid operations/buyer sends or working DB
writes. Existing global writes/automation false. User authorized a Stars1000r test
creation/white TEST image, not publication or funds/delivery. Local image visually
verified; proposal local only: no real Stars inventory/provider established, earlier
technical listing scenario rejected and must not repeat. LIVE_MVP_PLAN.md records these
limits and exact future stages. No claim of live create/photo/publish/delivery success.


## Реальная приёмка управления одним лотом — 2026-10-30 20:24:52 МСК

Последний явный запрос пользователя разрешил реальный цикл, включая риск ограничений
аккаунта. Предыдущая остановка preflight не является результатом этой попытки.
Новый сценарий выполнен через Products.queue_create → OutboxWorker → Executor →
SafeAccount и локальный отдельный operations.sqlite3; рабочая БД/глобальные флаги
не включались. Создан ровно один объект, повторов mutation не было.

- ID: `1f1d4868-3234-6930-63e5-505a0d142f79`, Telegram→Stars, amount=50 (схемное тестовое значение,
  не подтверждение запаса), способ «По @username».
- createItem HTTP200; fresh item HTTP200: DRAFT, current owner verified,
  name=TEST TEST TEST, description=TEST TEST TEST — проверка работы системы.,
  price=1000, raw_price=1000, attachment ID=1f1d4868-31a4-6880-aded-10a244e88c32.
  Фото скачано обратно с i.playerok.com и визуально проверено: белый TEST + watermark.
- updateItem HTTP200: price=749, raw_price=1000. Немедленное чтение ещё возвращало
  1000/1000; дополнительное read-only чтение подтвердило749/1000. Mutation не повторяли.
  Снижение25.10%; произвольное редактирование названия/фото этим не проверялось.
- itemPriorityStatuses: DEFAULT, price=0, id=1efbe5bc-99a7-68e5-4534-85dad913b981.
  Executor повторно сверил quote перед publishItem. Платёжные операции не запускались.
- publishItem HTTP200: PENDING_APPROVAL. Последующие item HTTP200 подтвердили APPROVED,
  approval_date=2026-10-30T17:24:17.501Z, исходный текст/тот же attachment/749/1000.
  Moderation не объявлялась активной публикацией; APPROVED подтверждён отдельно.
- removeItem HTTP200; строгий adapter проверил exact target ID и aware deletedAt,
  штатный delete outbox SUCCEEDED. Независимое новое read-only соединение: item HTTP404,
  GraphQL NOT_FOUND; полный get_my_items Stars/statuses=None не содержит этого ID.
  Новых Stars ID после очистки относительно baseline нет. Итог DELETED_VERIFIED.

Все4 штатные outbox операции SUCCEEDED. Доказательство сохранено локально:
data/authorized_listing_20261030/report.json (HTTP facts/hash, readbacks, quote),
operations.sqlite3, server_photo.jpg; краткое безопасное свидетельство в
LIVE_ACCEPTANCE_20261030.json. HTTP404 после удаления ожидаем, не failure удаления.
Не было buyer messages, передачи товара, покупки, PREMIUM, платного продвижения,
глобальной автоматизации или изменений существующих лотов. Config после:
writes=false/automation=false. Полный цикл продажи/автовыдачи остаётся незакрытым.
Код не менялся; unit/mock tests не запускались и не используются как live-доказательство.


## Stock automatic delivery checkpoint —2026-10-30

Existing targeted pipeline suite97 passed in16.90s
(data/stock_delivery_baseline_results.xml). New connected stock tests7 passed in1.58s
(data/stock_auto_cycle_results.xml). Full suite699 passed in89.84s
(data/stock_delivery_checkpoint_results.xml). Ruff check and format153 files clean.
Production code unchanged; no reproduced P0/P1 in tested stock paths.

Stock tests connect actual sync/discovery/enrollment/reservation/chains/outbox/
fulfillment and SQLite across reopen, duplicate hints/polls, two-connection races,
lost ACK/missing ID, crash before HTTP/after send before commit. Only remote API
is simulated. Message-history verification is a test assertion, not a new production
automatic readback gate. SENT requires explicit owner attestation. None of these
checks accepts live stock delivery; see STOCK_LIVE_PLAN.md for exact remaining gates.


## Четыре UX-блока —2026-10-30

- Stock:48 passed in11.76s, data/ux_stock_results.xml.
- Shared readiness:78 passed in10.11s, data/ux_automation_status_results.xml.
- Operation results:28 passed in7.77s, data/ux_operation_results.xml;125 regressions
  passed in14.57s, data/ux_results_baseline.xml.
- Full preview:29 passed in9.32s, data/ux_full_preview_results.xml;68 existing
  preview/product/agreement checks passed in9.98s, data/ux_preview_baseline.xml.
- Offline selfcheck OK:92 modules, migrations1–20, temporary backup/reopen, profile/
  chart PNG; external requests0. Рабочая БД не используется selfcheck.

Сценарии используют настоящий Dispatcher/SQLite FSM с mock remote API: restart before
confirm, stale/foreign callbacks, competing import/state change, one-time stock
transfer, disabled policy, prepared-secret non-disclosure, queued vs terminal result,
Telegram lost ACK no resend, delivered→manual fulfillment navigation, preview
edit/category/obtaining/photos/pagination. Дополнительно fault rollback склада/
import marker проверяется с SQLite trigger; ошибку хранилища UI сообщает без raw details.
Промежуточная ошибка нового теста (неверное имя fixture variable) исправлена, итог
проверяется повторно. Полный suite финализируется после всех code changes.


Финал UX checkpoint:729 passed in94.31s, data/ux_four_blocks_final_results.xml;
последние stock/preview23 passed in10.84s (ux_final_targeted_results.xml). Ruff
check/format159 files clean; pip check clean. Read-only backup рабочей DB во временную
копию: migration18→20, восемь основных таблиц counts preserved, integrity_check=ok,
новые таблицы пусты. В первом вспомогательном backup script временный sqlite connection
не закрылся при cleanup Windows; corrected closing context прошёл, temporary files
удалены. Production Store.backup не менялся, исходная DB не модифицировалась.
Live API/Telegram owner/buyer sends0; новые live-критерии этой проверкой не закрыты.
