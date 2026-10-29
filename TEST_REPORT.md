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
