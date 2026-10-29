# Playerok Control Center

Python 3.12, aiogram 3, SQLite WAL. Один магазин и один активный экземпляр.
Universal изучен, но запуск его монолитного runtime небезопасен для существующего магазина.
Переиспользуются запросы, парсеры, модели и интерфейсы PlayerokAPI с MIT attribution.

## Слои

- app/telegram: роутеры, клавиатуры, middleware allowlist, SQLite FSM.
- app/playerok: адаптер существующего API, единый ограниченный транспорт, нормализация ошибок.
- app/database: миграции, транзакции BEGIN IMMEDIATE, уникальные ключи, резервные копии.
- app/services: черновики/операции, финансы Decimal, запасы, аналитика, продвижение.
- app/workers: периодическая сверка API, durable outbox, health/backoff.
- app/rendering: Pillow-карточки и Matplotlib-графики без фиктивных данных.
- app/security: проверка доступа и редактирование секретов в логах.

## Данные

settings, administrators, fsm, drafts (optimistic revision), bindings, orders,
inventory (AVAILABLE/RESERVED/DELIVERED/QUARANTINED), reservations (unique order),
inbox (unique event key), outbox (unique operation key), chat_messages,
notifications, financial_operations, promotion_expenses, sync_cursors, audit_log,
schema_migrations. JSON применяется для payload, а не вместо транзакций.
Секреты товаров не записываются в audit_log. База приватная; резервные копии тоже.

## Жизненные циклы

Входящее событие → INSERT inbox → транзакционная обработка → отметка processed.
Заказ PAID → свежая проверка API → атомарный RESERVED + outbox → внешний запрос
→ подтверждённый SUCCEEDED/DELIVERED либо UNCERTAIN/QUARANTINED.
Истёкший PROCESSING нельзя автоматически повторять: результат мог быть принят сервером.
RETRYABLE_FAILED допускается лишь если доказано отсутствие побочного эффекта.
Публикация: локальный черновик → проверка схемы → создание серверного черновика
→ сохранение binding → свежий бесплатный priority quote → отдельная публикация.
У каждого внешнего действия устойчивый ключ; таймаут не приводит к повторному createItem.
Продавец может отметить SENT; это не CONFIRMED покупателя. Автовывод отсутствует.

## Синхронизация

API polling с пагинацией — источник сверки, WebSocket не является гарантией доставки.
Курсор сохраняется только после успешной записи страницы. Повторное чтение безопасно.
Уведомления имеют собственные ключи; неизвестный результат Telegram тоже виден.
Отмена workers управляемая; второй экземпляр блокируется локальным OS-lock.

## Безопасность

Все handlers защищены allowlist, личные чаты обязательны. OWNER_IDS задаётся локально.
TLS verification включена. Только playerok.com/graphql получает cookie.
Транспорт не повторяет mutations. Платные операции блокируются до сети.
Live-тесты не меняют рабочие лоты и сделки; тестовые ID сохраняются до дальнейших действий.


## Уточнение регистрации владельца

По последующему прямому указанию пользователя текущая локальная конфигурация
включает FIRST_START_CLAIM. Первый /start в private-чате атомарно вставляет
administrators под BEGIN IMMEDIATE; конкурентные претенденты отклоняются.
В новом шаблоне функция выключена; явный OWNER_IDS остаётся доступен.
FSM-операции одного пользователя сериализуются SimpleEventIsolation;
постоянное состояние — SQLiteStorage, межпроцессная защита — OS-lock.
