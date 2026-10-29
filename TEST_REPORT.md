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
