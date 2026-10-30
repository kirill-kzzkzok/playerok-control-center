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
Остаются: отдельное управление очисткой интерфейса, полноценный rule-based autoresponder,
полностью автоматическое подтверждение продавцом с доказанной корректной attestation
семантикой. Наличие кнопки с описанием ограничения не считается реализацией этих функций.

## Уточнение §3/§4: длинный список черновиков

Убран недоступный хвост после 30 записей: DraftsPanel/draft_page со страницами, поиском
public name/ID, фильтром локальных состояний и устойчивым sort tie-breaker. Доказательство:
`tests/test_drafts_panel.py` — 57 записей/6 страниц, одинаковое время, owner isolation,
private fields не ищутся, unknown state, clamp после удаления, Dispatcher/restored search,
старый draft доступен, SQL/HTML escaping. Серверные статусы при этом не симулируются.
