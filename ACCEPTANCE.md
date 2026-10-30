# Playerok Control Center — проверка требований

Состояние: 30.10.2026. Это карта доказательств, не акт полной приёмки.
Рабочая конфигурация: writes=false, automation=false; платные операции запрещены.
По последнему указанию пользователя любые новые live-записи требуют отдельного разрешения.

## PHASE 7: обязательные инъекции сбоев из §16 PROJECT_SPEC.md

Все перечисленные тесты используют временные БД и mock/fake API. Они не отправляют
сообщения покупателям и не меняют магазин. Названия указаны для повторного запуска pytest.

| № | Сценарий | Доказательство и предел проверки |
|---|---|---|
| 1–2 | Дублирующие/параллельные события заказа | `test_durability.py::test_duplicate_and_parallel_events`, `test_stock_cannot_be_given_to_two_buyers`: один inbox/order, уникальный резерв |
| 3 | Двойное «Опубликовать» | `test_dispatch_recovery.py::test_concurrent_publish_button_confirmations_dispatch_one_mutation`: настоящий Dispatcher, два concurrent callback, один outbox и один вызов publishItem |
| 4 | Обрыв WebSocket | `test_realtime.py::test_transport_failure_reconnects_and_keeps_api_catchup_durable`, `test_missing_pong_ends_connection`: reconnect/hints и deadline |
| 5 | Недоступность Telegram | `test_notifications.py::test_telegram_failure_is_uncertain_without_duplicate`: неизвестная отправка без повтора; обновление существующего уведомления отдельно проверено в `test_replies.py` |
| 6–8 | HTTP 429/500/истечение сессии | `test_dispatch_recovery.py::test_actual_write_transport_failure_is_unknown_without_blind_retry`: реальный SafeAccount на mock HTTP, ответ на mutation не повторяется; read backoff в `test_transactions.py`, `test_realtime.py`; 401/403 diagnostics в `test_auth_diagnostics.py` |
| 9–10 | Падение до/после внешней отправки | `test_dispatch_recovery.py::test_abrupt_process_exit_preserves_reservation_and_prevents_resend`: отдельный процесс с os._exit, mock remote receipt после send, повтор запрещён в обеих точках |
| 11 | Перезапуск во время выдачи | `test_durability.py::test_restart_after_external_call_quarantines_stock`, `test_delivery_chains.py::test_restart_resumes_queued_step_without_duplicate`: quarantine для неизвестного исхода, сохранённая очередь для ещё не начатого шага |
| 12 | Нулевой остаток | `test_durability.py::test_stock_cannot_be_given_to_two_buyers`, `test_automation.py::test_dry_run_reports_empty_stock_local_problem_and_prior_delivery`: последний код не выдаётся двум заказам, empty guard |
| 13 | Неполный ответ | `test_incomplete_api.py`, `test_operations.py::test_mark_read_incomplete_response_is_uncertain_without_retry`: неполный ответ не становится успехом |
| 14 | Устаревший PREMIUM | `test_operations.py::test_stale_paid_quote_blocks_before_mutation`, `test_placement_basis.py`: текущая базовая цена/класс и явный ноль обязательны для бесплатной публикации |
| 15 | Неправильный ввод | `test_products.py`, `test_category_contracts.py`, `test_discounts.py`, `test_fulfillment.py`: типы/границы/поля/условия, без fallback в неизвестной схеме |
| 16 | Два сценария настройки товара | `test_automation.py::test_overlapping_policy_edit_scenarios_commit_one_revision`: два overlapping чтения, один CAS commit; policy revision/nonce в Telegram-тестах |

Дополнительные найденные гонки:

- `test_delivery_chains.py::test_stop_while_waiting_for_api_lock_prevents_dispatch`:
  stop policy/runtime, локальная проблема, rollback, истечение/recovery lease между
  проверкой и отправкой. Проверка повторяется после блокировки API и в самом транспорте
  после rate-limit wait, до HTTP. GraphQL и REST имеют тесты этого барьера.
- `test_expired_lease_after_external_ack_keeps_late_receipt_without_retry`: поздний ID
  сохраняется в outbox_late_results, основной UNCERTAIN/quarantine не меняется.
  `test_late_response_cannot_overwrite_terminal_decision_and_is_deduplicated` защищает
  ручное решение и один журнал наблюдения. Текст выдачи в журнал не копируется.

Локальная остановка не может отозвать запрос, уже переданный HTTP-клиенту. Такая граница
оставляет неизвестный исход для сверки. Процесс, аварийно завершившийся после отправки,
не способен записать поздний ответ; evidence тогда получают только чтением Playerok.

## Проверенная интеграция и оставшиеся критерии

| Область | Подтверждено | Остаётся |
|---|---|---|
| Создание/публикация/edit/delete | Native signatures, mock executor, schema/conditions/photo checks, durable queue и Dispatcher | Успешная live-приёмка тестового лота не доказана; прежний create отклонён правилами, повтор запрещён |
| Заказы/цепочки | Уникальный запас, ordered greeting/delivery/after, restart/reconciliation, проблемные статусы | Реальная отправка/выдача не проверялась; SENT требует отдельного attestation workflow |
| Финансы | Decimal quotes с provenance; 462 live transaction rows, 217 заказов с серверными датами; owner cost/review/allocations | Не все неоднозначные комиссии имеют доказанную денежную семантику; прибыль всего магазина неизвестна без исторических расходов |
| Продвижение | Настройки/позиции/night/budgets, persistent read scheduling, отдельный dry-run ledger | Платный исполнитель не реализован/не принят в рамках запрета платных операций; не выдавать dry-run за поднятие |
| Профиль/аналитика | Parser fields, PNG и плавные линии реальных точек, периоды/coverage, transaction receipt links | Полнота всех исторических данных API независимо не доказана |
| Сообщения | История/поиск/фото/reply-preview, durable reconciliation/notifications | Live reply/upload/read-status mutation не приняты |
| Эксплуатация | SQLite migrations/backup, first-start owner, lock, logging redaction, worker health/backoff | Длительный unattended soak, финальная проверка всех UX/спецификации и packaging ещё не завершены |

Следующий проход: расширить эту карту до всех требований §1–24, проверить отсутствующие
сценарии и ограничения реализации, исправить конкретные пробелы. Не отмечать PHASE 8/9
или всю цель выполненными по одному зелёному набору тестов.

## Уточнение §14: настройки

SettingsPanel: подключения (сохранённые состояния), безопасность, переходы к finance,
statistics, fulfillment/manual boundary, automation/promotion, templates, backup/diagnostics.
NotificationPreferences: шесть категорий на владельца, defaults true, bool validation,
revision CAS, persistent suppression/no replay, обязательный uncertain. Доказательства:
`tests/test_notification_preferences.py` (включая real Dispatcher/restart/stale/foreign),
`tests/test_notifications.py` (baseline/failure/critical), existing replies status updates.
Очистка интерфейса реализована ниже; остаются live-приёмка rule-based autoresponder (реализация v18 ниже),
полностью автоматическое подтверждение продавцом с доказанной корректной attestation
семантикой. Наличие кнопки с описанием ограничения не считается реализацией этих функций.

## Уточнение §3/§4: длинный список черновиков

Убран недоступный хвост после 30 записей: DraftsPanel/draft_page со страницами, поиском
public name/ID, фильтром локальных состояний и устойчивым sort tie-breaker. Доказательство:
`tests/test_drafts_panel.py` — 57 записей/6 страниц, одинаковое время, owner isolation,
private fields не ищутся, unknown state, clamp после удаления, Dispatcher/restored search,
старый draft доступен, SQL/HTML escaping. Серверные статусы при этом не симулируются.

## Уточнение §10/§13: хронология заказов

`order_page` сортирует aware server creation time в UTC, не первое локальное receive.
`tests/test_catalog.py::test_order_list_uses_backfilled_aware_server_time_instead_of_ingest_time`
сначала воспроизвёл неверный порядок, затем проверяет backfill/offset/unknown/naive/equal
instants и сохранение известной даты при malformed ответах. Working-cache read-only probe:
30/30 позиций исправлены без перезаписи данных, data/order_chronology_readonly_check.json.

## Уточнение §14: очистка интерфейса

PanelTracking/InterfaceCleanup/InterfaceCleanupPanel: bounded explicit preview+confirm,
owner/bot scoping, touched snapshot, exclude current/notifications/untracked historical,
no content storage, persistent per-message results and crash recovery/no retry.
`tests/test_interface_cleanup.py` covers actual Dispatcher/FSM/session middleware plus
service error/recovery cases. Startup tracking applies only to newly successful UI
responses. No live deletion performed; mock acceptance only. Data/cache/stock cleanup
is not part of this command. Telegram source: https://core.telegram.org/bots/api#deletemessage.

## Уточнение §14: автоответы

Rule-based auto replies реализованы отдельно от manual quick replies: future-only exact
phrase rules, explicit arm/CAS/account, shared-chat cooldown/Moscow-day attempts, duplicate
message registration, bounded worker/backoff, fresh chat/message validation, transport
stop guards and existing reply outbox/reconciliation. `tests/test_auto_replies.py` covers
these paths including Dispatcher/restart/arming. Production runtime flags false, zero
rules, no actual buyer sends. This is mock acceptance; live delivery remains unverified.

## Критерии §20: текущая карта приёмки

Здесь «локально»/«mock»/«live-read» обозначают разные уровни доказательств; ни один
из них автоматически не доказывает остальные. Успехи live относятся к датированным
проверкам, не обещают бессрочную авторизацию или непрерывную работу остановленного бота.

| № | Критерий | Состояние и доказательство |
|---|---|---|
| 1 | Запуск бота с реальной конфигурацией | Ранее подтверждён polling/owner claim в PROGRESS; длительная непрерывная работа не принята |
| 2 | Авторизация Playerok | Live-read viewer/user/transactions HTTP200: data/playerok_auth_diagnostics.json; будущая сессия может истечь |
| 3 | Доступ только владельцу | OwnerMiddleware, real first claim, test_telegram_integration/test_ui_and_rendering; групповые/чужие callbacks отклоняются |
| 4 | Полный мастер | Mock Dispatcher/schema/photo/preview/durable FSM в test_telegram_integration/test_wizard_attachments; успешный live create не принят |
| 5 | Динамические поля | test_category_contracts/test_item_fields/test_agreements; актуальные schema read, unknown types fail closed |
| 6 | Реальная загрузка фото | Telegram attachments и native multipart mock проверены; подтверждённого Playerok live upload/create нет — открыто |
| 7 | Live create/edit/publish тестового лота | Открыто: create отклонён правилами, отсутствие объекта проверено; новые live-записи требуют разрешения |
| 8 | PREMIUM до оплаты | Read-only quotes + Decimal preview, test_finance_panel/test_placement_basis; платная покупка не выполнялась |
| 9 | Комиссии без вымысла | Provenance/Decimal/unknown guard, financial/transaction tests; неоднозначные реальные удержания не объявляются точными |
| 10 | Управление существующими товарами | Read/schema/inline actions и mock executor; live working listings не менялись, реальное изменение не принято |
| 11 | Автовыдача в тестах | test_delivery_chains/test_enrollment/test_durability, ordered steps/stock; live buyer send не принят |
| 12 | Дубликаты без повторной выдачи | Unique inventory/reservation/inbox/outbox, concurrency/recovery tests и subprocess crash |
| 13 | Состояние после restart | SQLite FSM, migrations, backup, cursor/chain/outbox/reply recovery tests; working v18 retained data |
| 14 | Повтор callback без дублей | Real Dispatcher concurrent publish confirmations + queue CAS, test_dispatch_recovery/test_telegram_integration |
| 15 | Устойчивые сообщения/уведомления | Dedup, unknown/no retry, preference/suppression, auto_reply source/stop guards; live replies не приняты |
| 16 | Условия/стоимость поднятия | Read-only promotion policy/quotes/position/scheduling; реальный платный executor не принят/не включён |
| 17 | Денежные лимиты | Promotion dry-run budgets count RESERVED/UNCERTAIN, tests; реальные расходы запрещены, не выдаём dry-run за charge gate live |
| 18 | Реальный профиль | Native parser contract + real profile read; PNG rendering, avatar fallback; окончательная визуальная live-приёмка отдельно |
| 19 | Реальные/помеченные графики | Real cached history vs separate demo fixtures, visual QA; smooth points/gap/calendar tests |
| 20 | Ошибки сети/API | §16 matrix above, read429/backoff/401/403/sanitized trace, mutation unknown, cancellation/late ack |
| 21 | Все автоматические тесты | Последний полный результат фиксируется в TEST_REPORT/PROGRESS; успешный набор не закрывает live gaps |
| 22 | Live evidence documented | PLAYEROK_RESEARCH/TEST_REPORT and ignored data reports; successful/read-only and rejected/unverified operations distinguished |
| 23 | Рабочий магазин не повреждён | Новые записи/платежи/выдачи не выполнялись; настройки writes/automation false; исходный rejected create reconciled |
| 24 | Код/зависимости/настройки запуска | Lockfile/install/start, wheel resources/isolated-origin smoke и selfcheck; текущая Windows/dependency конфигурация, не все платформы |
| 25 | README | Установка/конфигурация/ограничения/восстановление/диагностика и private remote; общая приёмка всё ещё открыта |
| 26 | Честный отчёт | Настоящая карта/TEST_REPORT перечисляют непринятые возможности; проект не объявлен завершённым |

История и текущая ветка теперь синхронизируются с приватным origin по прямому указанию
пользователя; Git не содержит .env/PROJECT_SPEC/data/logs. Перед первым push проверены
все 449 исторических blobs, actual secrets/JWT/private paths отсутствуют. Будущие изменения
также проверяются перед commit/push; рабочие secret/state files остаются только локально.

## Уточнение §5/§13/§24: сверенные комиссии продажи

Exact linked confirmed SELL IN RUB + completion date + gross-fee=credit gives a narrowly
verified sale fee, independent of unknown owner costs. Tests/test_settlements.py covers
incomplete/ambiguous/refunded/foreign/mismatched sources, percentage-vs-money confusion,
period/owner, no double deduction, review changes and actual Dispatcher. Local cached
probe reconciles181/181 sales; all10 withdrawals remain unknown for explicit net coverage.
Root fee of other operations is not summed. Withdrawal retention/allocations share a
positive-explicit-net helper; no zero-placeholder or current-rate fallback. This closes
part of observed sale-fee analytics, not all financial/withdrawal/bank/live requirements.


## Устойчивость фоновых циклов (§2/§16/§20)

All registered runtime tasks now use Supervisor with persisted bounded backoff and
sanitized critical incidents. Seven regression tests cover unexpected completion,
restart/cancellation, durable delay, storage failure, baseline bypass and ACK-before-DB
failure with exactly one executor call and expired lease UNCERTAIN. This proves local
mock recovery boundaries; no real buyer sends or live mutation acceptance is implied.
Last-saved worker lifecycle is distinct from API health; no hang detection is claimed.


## Изоляция магазина при смене credentials (§2/§5/§15/§16)

Startup verifies a single database/account binding against legacy settings, cached
transactions and known order sellers before launching workers. Conflicts/unknown old
ownership fail closed; another shop needs its own database. Tests/test_account_binding.py
covers idempotency, concurrent bind, malformed/legacy evidence and actual run startup
with fake services stopping before backup/workers on mismatch or failed authentication.
Read-only working cache check: 217 orders/462 transactions agree with saved identity;
no real startup, database write or API request performed by this checkpoint. This is
single-account enforcement, not a claim of multi-account support or live auth freshness.


## Безопасная отмена и shutdown (§15/§16)

ThreadCalls preserves API serialization after caller cancellation, blocks queued/
pre-HTTP-cancelled mutations, drains work before client/DB close and retains allowlisted
late IDs as UNCERTAIN evidence. Tests/test_thread_calls.py uses actual Playerok,
SafeAccount+MockTransport, OutboxWorker and actual run shutdown with fake services.
ACK/error paths issue exactly one HTTP request; throttle cancellation issues zero.
This covers cooperative runtime/read-diagnostics shutdown; forced death, legacy live_test
and an absolute wall-clock HTTP/shutdown deadline are not accepted by these tests.


## Приём новых запросов после начала shutdown (§15/§16)

ThreadCalls closing/marker rejects new handlers even if no registry existed, queued
lock/thread-pool entries fail before method and closing signals block pre-HTTP mutation
at transport throttle. Added tests in test_thread_calls.py verify these paths plus
actual run late-handler rejection and closed mutation with zero HTTP. No forced-death,
absolute deadline or live-write acceptance is implied. Previously failed GitHub push
was retried successfully at the start of this checkpoint; normal private sync resumed.


## Legacy live-write audit (§2/§17/PHASE8)

The rejected technical listing's standalone create/edit/finally-delete code was removed.
app.live_test now performs bounded read-only local journal inspection, without Config,
credentials, HTTP or writes; all history states/missing/invalid files remain retired.
Original journal bytes preserved in tests and working historical SHA256 comparison.
Tests/test_legacy_live_test.py covers privacy, malformed/oversized/deep files, CLI,
missing directory, state handling and no credential/client construction. Native permit
contract helper moved to non-packaged tests with default/mounted MockTransport checks;
existing create/edit/agreements/fields contracts retained. No live operation occurred;
future real-goods live acceptance still requires separate authorization and evidence.


## §13: paid-order volume ranking

Separate bestsellers/category_counts views implement top15 by paid order count, retaining
known-revenue views. Test_bestsellers covers count vs revenue, unknown-only/partial/zero,
status/period/date exclusions, same-name IDs, latest server name/ties, actual Dispatcher
five periods/owner/escaping/length/stable cutoff. Read-only cache probe reconciles217
orders/183 paid with183 distinct native item IDs; all actual counts1 are disclosed,
not grouped into invented SKU families. Categories remain name-grouped and labelled.
No API, outbox, working data rewrite or financial interpretation change occurred.


## User list pagination (§3/§10/§15)

ListingsPanel validates server pages/cursor cycles, persists nonce-scoped continuation,
clears terminal cursor and preserves checkpoint on malformed response. Test_listings_panel
covers items/chats restart+EOF, cycle/schema errors, empty continuation, stale callback,
limit/long ID and actual Dispatcher double-click/filter reset/foreign owner. All read
mock only: no live API/mutation/working database write. UI pagination does not prove
complete upstream history beyond locally inspected pages.


## §6: composed item filters

Status/name/price/native category ID/exact priority filters implemented in persistent
FSM; filters compose, validate and reset page nonce. Tests/test_item_filters.py covers
invalid input, unknown-vs-zero, literal Unicode matching, boundaries, actual Dispatcher
restart/composition/reset/owner and SafeAccount native category wire with user scope.
Name/price/priority are explicitly per-server-page, not claimed global query support;
empty matching page still permits continuation. Category-name picker remains incomplete.
No live listing/API write performed; mock acceptance only.


## §6 named category picker follow-up

Previous manual-category-ID-only limitation is resolved locally: game name search,
validated opaque game pages, category pages and membership refresh before apply.
Test_item_category_picker covers restart/filter preservation, stale/cancel/foreign,
duplicate names, changed membership, malformed/cyclic pages and real Dispatcher flow.
No live API, write or full-history query acceptance is implied. ID entry remains optional.


## §6: item card/gallery follow-up

ItemPanel adds read-only photo navigation and explicit API view/deal counters with
unknown-vs-zero semantics, priority-scope position and fixed-domain encoded link.
Test_item_panel covers metadata/restart/reorder, fresh ownership/identity/hidden/removal,
stale/index validation and actual Dispatcher sendPhoto/owner/no outbox. Photos are
opened by button, not automatically included in the text card; counters are not claimed
settled-sale totals. Media download uses existing bounded Attachments path. Mock only;
no live screenshot/photo-send/listing mutation or server acceptance implied.
