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
