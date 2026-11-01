# AUDIT_COVERAGE.md — карта покрытия независимого аудита

HEAD: dcb728305e0448066e73815b9fab15bd9b4ad8fc (master, clean)
Дата: 2026-11-01. Режим: RED TEAM / adversarial / read-only (production-код не менялся, live-операций нет, рабочие БД не тронуты).

## Зоны аудита и статус покрытия

| # | Зона | Ключевые файлы | Метод | Статус |
|---|------|----------------|-------|--------|
| 1 | Telegram navigation/FSM (/start, /panel_reset, back/close/recover, ack-gate) | router.py, navigation.py, callback_ack.py, storage.py, transport.py | личное чтение + subagent + parent-verify (G1-G6) | covered — P0-находки G1-G6, ключевые проверены лично |
| 2 | Уведомления/replies/operation_receipts | workers/notifications.py, services/replies.py, telegram/operation_results.py, message_notice.py | личное чтение + RED-тесты + subagent | covered — 8 findings (N1-N8), parent-verified |
| 3 | Playerok API layer (ошибки, 200+GraphQL errors, пагинация, session, enum name/value) | playerok/adapter.py, thread_calls.py, playerokapi/* | subagent + parent-verify | covered — 8 findings (F10,F14-F18), ключевые проверены лично |
| 4 | Автовыдача (non-gift): logical products, inventory, reservations, chains, fulfillment, enrollment | services/orders.py, logical_products.py, workers/* | subagent (repro исполнены) + parent-verify | covered — 8 findings (F10-F13, F11), reject-лист проверен |
| 5 | Gifts/Stars (money-critical) | app/gifts/*, gifts_panel.py | **лично родителем** (subagent дважды упал по HTTP 400 провайдера) | covered — инварианты подтверждены, P0 не найдено |
| 6 | Тестовый стенд (false-green) | tests/* | subagent + личные RED-тесты | covered — 8 false-green паттернов (T1-T8) |

## Лично подтверждённые (parent-verified, исполняемое доказательство)

- **RED-тесты**: `C:\Users\user\AppData\Local\hermes\cache\scratch\test_audit_red_repro.py` — 4 FAILED (exit 1) на чистой in-memory/temp SQLite, без production-правок.
  - A: `{{ITEM_PAID}}` → `Orders.ingest_message` → inbox kind='message' без фильтра системных событий (app/services/orders.py:222-236) → `message_notice` → «Неизвестный отправитель» + сырой `{{ITEM_PAID}}` (app/telegram/message_notice.py:8-10).
  - B: кнопка «Открыть результат» (router.py:446-451) после reply/mark_read всегда даёт `Conflict('Результат операции недоступен')` (operation_results.py:44-48), т.к. operation_receipts создаются только для видов {create,publish,edit,edit_fields,discount,delete,delivery,delivery_text,fulfill,gift_*,premium_*} (store.py:230-250), reply/mark_read исключены.

## Проверенные инварианты (не сломаны)

- `deliverable()` (order_management.py:24-35): повторная проверка PAID/has_problem=False/is_automated=False/владелец/чат в момент исполнения outbox delivery.
- outbox delivery executor (outbox.py:599-624): сверка резерва (status='RESERVED', item_id == stock_scope) перед отправкой секрета; `result.get('id')` пустой → UNCERTAIN, не SUCCESS.
- Устаревшие переходы статусов заказов блокируются (orders.py:24-29: CONFIRMED/ROLLED_BACK/FAILED не «откатываются» на PAID/PENDING).
- Notifications.once: событие не пере-отправляется при повторе (INSERT OR IGNORE event_key+owner), send failure → UNCERTAIN без слепого ретрая (notifications.py:198-222).
- WS realtime — read-only подписки, сигнал только в sync_requests (realtime.py:29-107).

## Лично подтверждённые инварианты (parent-verified, дополнительно)

- `Store.enqueue` (store.py:149-230): блокировка повторной регистрации выдачи (`delivery/fulfill` PENDING/PROCESSING/RETRYABLE/UNCERTAIN), взаимоисключение подарков/цепочек, блок конфликтных item-операций, disarming gift_listings при edit/delete.
- `Store.finish` (store.py:349-397): результат + состояние inventory (DELIVERED/QUARANTINED) + order_history в одной транзакции; `lease_token` CAS.
- `deliverable()` (order_management.py:24-35) + outbox delivery executor (outbox.py:599-624): повторная сверка PAID/без проблемы/не автоматического владельца/чата + резерва перед отправкой секрета; `result.get('id')` пустой → UNCERTAIN.
- TransactionWorker (workers/transactions.py): guard на смену account_id, экспоненциальный backoff, sanitize health.
- finance.py: чистый Decimal (ROUND_HALF_UP/FLOOR), rate только с провенансом.
- Router callback dispatcher (router.py:186-330+): все префиксы панелей покрыты; устаревшие токены (confirm_token/nonce/revision) отклоняются.

## Ограничения / непроверяемое из этой среды

- Реальная латентность Telegram через прокси (ACK 2s) — гипотеза правдоподобна, live-измерение запрещено.
- Поведение реального Playerok GraphQL (200+errors формы, повтор курсора) — код проверен, поведение API не проверялось live.
- Telethon/MTProto поведение при разрыве — статический анализ только.
