# AUDIT_REPRODUCTIONS.md — исполняемые доказательства аудита

HEAD: dcb728305e0448066e73815b9fab15bd9b4ad8fc · 2026-11-01.

## RED-тесты (repo, сохранены)

Файл: `tests/test_audit_red_repro.py` (добавлен аудитом; production-код не менялся).
Запуск: `.venv/Scripts/python.exe -m pytest tests/test_audit_red_repro.py -q`
Фактический результат на HEAD: **4 failed, exit 1** (это и есть доказательство — тесты написаны под правильное поведение и падают на текущем коде).

| Тест | Доказывает | Фактическая ошибка |
|------|-----------|--------------------|
| `test_system_item_paid_event_is_not_a_user_message` | F1: системный шаблон инжестится как обычное сообщение покупателя | `system placeholder ingested as ordinary buyer message` (kind='message', text='{{ITEM_PAID}}') |
| `test_message_notice_renders_placeholder_as_unknown_sender` | F1: рендер «Неизвестный отправитель» + сырой `{{ITEM_PAID}}` | текст содержит `Неизвестный отправитель … {{ITEM_PAID}}` |
| `test_result_show_after_reply_is_available` | F2: result:show после reply | `Conflict: Результат операции недоступен` (operation_results.py:48) |
| `test_result_show_after_mark_read_is_available` | F2: result:show после mark_read | `Conflict: Результат операции недоступен` |

Тесты используют только temp-SQLite (tmp_path), никаких рабочих БД/сетей.

## Личные прогоны родителя (parent-verified)

1. `git rev-parse HEAD` → dcb7283…; `git status --short` → только untracked артефакты аудита (AUDIT_COVERAGE.md, AUDIT_FINDINGS.md, tests/test_audit_red_repro.py). Production-код и рабочие БД не тронуты.
2. Полное чтение money-цепочек (gifts service.py 560-690, 760-831; mtproto.py 230-260; execution.py; store.py enqueue/finish 149-230, 349-397; orders.py; outbox.py 596-650) — выводы в AUDIT_FINDINGS.md «Проверенные инварианты».
3. RED-тесты прогнаны дважды (из scratch и из tests/) — идентичный результат (4 failed, exit 1).

## RED-прогоны субагентов (воспроизводимы по описанию, файлы удалены им самим)

- F3: enqueue reply без origin_event → notification_actions пуст → bot.edits == [] (RED-проба субагента, код в transcripts).
- F4: receipt notice UNCERTAIN → 3 цикла once() → статус не меняется, отправки нет.
- F7: re-ingest сообщения с deleted_at → inbox payload без deleted_at.
- F8: две notification_actions на событие, один сбой edit → обе строки failures=1.

## Не выполнялось (запрещено брифом)

- Live-операции Telegram/Playerok/MTProto, покупки, публикации, выдачи.
- Изменения production-кода, рабочей SQLite, Telegram-сессий.
