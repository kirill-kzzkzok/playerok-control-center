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
