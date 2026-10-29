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


## Условия и редактирование динамических полей

Снимок условий содержит полные документы, pending IDs и SHA256. Мастер показывает
документы без обрезки на нескольких страницах. Принятие на Playerok требует отдельного
явного подтверждения и outbox kind=agreements, привязанного к текущему аккаунту.
Перед отправкой перечитываются ID и тексты; после отправки проверяется отсутствие
pending. Частичный/неизвестный результат → UNCERTAIN, без повторной отправки.
Перед созданием товара проверяются новый снимок и локальное ознакомление с инструкцией.

Edit draft хранит edit_target и hash текущих attributes/data_fields/comment/category/
obtaining_type. Он не может быть использован для создания нового лота. Снимок сверяется
при enqueue и execute; приватные значения хранятся локально, но не выводятся в превью.
При edit_fields сохраняются существующие неизвестные атрибуты и не меняются другие
поля товара. Конкурирующие действия на одном target, включая UNCERTAIN, блокируются.

Discount — отдельная операция: обычный продавец меняет только price относительно
существующей raw_price. Новая фиктивная базовая цена не создаётся. Проверки диапазона,
5%, исходных цен и подтверждённого ответа выполняются перед/после mutation. Live-запись
не подтверждена; штатная конфигурация остаётся read-only.


## Финансовые оценки и миграция v2

withdrawals.py — чистые Decimal-расчёты и read-only получение provider/FX snapshots.
Формула: max(amount*fee/100 + extraFee, minFeeAmount); доплата USDT требует свежего
курса USDT_RUB. Итог в RUB (для USDT — эквивалент), фактическое округление неизвестно;
применяется консервативное округление net вниз до копеек и явная маркировка оценки.
Лимиты outgoing проверяются до расчёта получения; при нехватке данных net отсутствует.

Снимки provider/FX ограничены по возрасту; параметры исходных ставок и версия формулы
сохраняются в financial_quotes (migration 002). Это ESTIMATE_ONLY, отдельно от настоящих
financial_operations/promotion_expenses. Запись финансового quote не означает платежа,
прибыли или фактического поступления. Whitelist исключает private fields/выдачу/чаты.

FinancePanel — отдельный Telegram-компонент для методов вывода, суммы, себестоимости
и стоимости будущего размещения серверного черновика. Он не вызывает денежных mutations.
Настройки привязаны к owner ID, FSM сохраняется в SQLite. Wizard использует те же
расчёты. Стоимость размещения запрашивается по raw_price; неизвестная базовая цена
не заменяется скидочной. Premium preview не создаёт ни outbox оплаты, ни расхода.

Store.apply_migrations читает упорядоченные SQL-файлы и применяет каждую версию под
BEGIN IMMEDIATE с записью schema_migrations в той же транзакции. SQL включён в package data.


## Ограниченная сверка, cursor checkpoint и UI истории (v3)

Synchronizer.scan использует общую validate_page с Playerok.pages: повреждённый
page envelope не попадает в обработку. sync_cursors.cursor хранит JSON after/seen;
visited cursors ограничены 10000 на проход для защиты от бесконечной пагинации.
Каждая входящая запись сохраняется отдельной транзакцией Orders, checkpoint — после
всех записей страницы. Повтор страницы после interruption дедуплицируется по inbox.
Сетевые ошибки не стирают checkpoint; invalid cursor начинает новый проход с head.
Первая страница проверяется при каждом продолжении; циклическая полная сверка нужна,
поскольку серверная стабильность cursors и порядок обновления сделок не доказаны.

sync_chats содержит payload диалога, последнее время попытки, initial_complete и
error_code. NULL last_synced_at обслуживаются первыми, далее oldest attempt. В одном
цикле не более 12 чатов и четырёх страниц истории на чат плюс head при resume.
Это ограничивает запросы, но время обнаружения события зависит от размера очереди;
мгновенная доставка не гарантируется. once защищён asyncio.Lock. Полные проходы
заказов и списка чатов имеют persisted completed_at; до первичной сверки всех
известных чатов health=BACKFILL, ошибка потока отражается как ERROR.

ChatPanel хранит cursors/index/nonce в SQLite FSM. Он кеширует прочитанные страницы,
но не создаёт inbox events. В Telegram показываются безопасно экранированные куски
текста, автор/дата и счётчик фото; сам показ медиа пока не реализован. Исходное тело
сообщения остаётся в локальной БД. Прочитанность меняется только отдельным durable
mark_read после явного подтверждения; разрешённый trusted markChatAsRead проходит
через тот же guard/outbox и проверку ответа. Read-only конфигурация её не выполняет.

Notifications выбирает только события без записи для хотя бы одного владельца,
пакетами 200. Собственные сообщения получают SKIPPED. Начальная установка BASELINE
и initialized атомарна. PROCESSING/UNCERTAIN не отправляются повторно автоматически.


## Шаблоны и связь ответов с Telegram (v4)

Replies хранит quick_replies с owner_id и optimistic revision. Текст валидируется по
UTF-16 лимиту Telegram. RepliesPanel использует SQLite FSM для create/edit и nonce
для выбора шаблона. Шаблон копируется в подтверждённый pending snapshot; изменения
шаблона после preview не меняют отправляемый текст. Preview включает весь snapshot.

Replies.origin разрешает источник только по сохранённым notifications + chat_messages
и владельцу. Replies.enqueue повторно сверяет origin/target/owner и записывает outbox
и notification_actions в одной транзакции. Связь не передаётся в Playerok mutation.
Операция без источника (из списка диалогов) также допустима, но уведомление не выдумывается.

Router сохраняет исходные notifications: при callback из такого сообщения display
создаёт отдельный экран. Notifications.refresh_actions читает durable outbox и редактирует
существующее уведомление с текущим состоянием последнего связанного действия. Успех
отправки показывается только после SUCCEEDED с API message ID. UNCERTAIN остаётся
видимым и не создаёт повторной отправки. Повтор editMessageText идемпотентен по тому
же Telegram ID/тексту; rendered_status записывается после edit, а retry_after/failures
сохраняются при сбое. Новая смена outbox status не теряется при параллельном завершении.


## Фото в сообщениях и журнал загрузок (v5)

Attachments.remote — ограниченная HTTP-загрузка публичного media URL без cookies и
redirects, с общей валидацией/нормализацией JPEG. ChatPanel.media перепроверяет сообщение
на той же API-странице; URL не приходит из callback. Индексы/nonce и исходные message IDs
живут в SQLite FSM. При изменении страницы пользователь обновляет диалог.

RepliesPanel.photo сохраняет нормализованный файл и pending snapshot, показывает фото
и полный текст подписи отдельно от подтверждения. Queue payload содержит photos/text;
origin notification связывается прежней транзакцией Replies.enqueue. Один Telegram
photo/document обрабатывается как один ответ; агрегатор Telegram albums не реализован.

Executor последовательно: свежий get_chat → валидация всех локальных файлов → journal
PROCESSING → guarded uploadChatImageIntoTemporaryStore → journal UPLOADED с ID/expiry →
проверка срока всех вложений → guarded createChatMessage(imagesIds) → проверка message ID
и числа image records → SUCCEEDED. Все потенциально принятые, но не подтверждённые
mutations переводятся в UNCERTAIN. Journal не даёт права повторять неизвестный запрос;
детерминированный clientAttachmentId служит корреляцией, не гарантией идемпотентности.
Cleanup защищает photos paths активного FSM/outbox и UNCERTAIN; успешные uploads можно
удалить локально после завершения. Реальный settlement/платежи в этом потоке отсутствуют.


## Локальная сверка неопределённых операций (v6)

Reconciliation разрешает UNCERTAIN только владельцу из administrators, проверяет
snapshot signature и повторяет read evidence непосредственно перед транзакцией.
Положительное сопоставление сообщения использует указанный владельцем ID, актуальный
chat scope, seller author, точный текст (без вывода), created_at и число фото. Для
delivery проверяются свежая продажа/предмет и тот же quarantined reservation. Это
ручное решение: совпадение количества фото не доказывает их содержимое. Выборка
ограничена 480 сообщениями; negative search result никогда не даёт права на resend.

CONFIRM_MESSAGE/CONFIRM_READ_STATE сохраняются как SUCCEEDED с resolution_source
OWNER_DECISION; STOP_UNCERTAIN — как PERMANENT_FAILED с отдельным кодом
MANUALLY_STOPPED_UNCERTAIN и external_outcome UNKNOWN. Последний означает локальную
остановку, а не доказанную внешнюю неудачу. Нет перехода в PENDING/RETRYABLE_FAILED.
Оригинальные result/error и причина решения остаются в operation_resolutions; журнал,
outbox, inventory и audit меняются атомарно с повторной проверкой CAS. Данные выдачи
не копируются в evidence, причины вводятся владельцем с предупреждением не писать секреты.

Store.finish/recover создают operation_uncertain inbox в той же транзакции; v6 backfill
охватывает ранее сохранённые неопределённые операции. Notifications выводит такие
события до окончания первой синхронизации, независимо от подключения Playerok. Уже
закрытые события получают SKIPPED. Cleanup сохраняет файлы MANUALLY_STOPPED_UNCERTAIN
для последующей сверки; резерв не возвращается в AVAILABLE. OperationsPanel использует
SQLite FSM/nonce и отдельное подтверждение. Все разрешения локальные, mutations отсутствуют.


## WebSocket accelerator и sync_requests (v7)

Realtime имеет фиксированный WSS endpoint и три минимальные subscriptions из текущего
контракта официального клиента. UserProfile является union, поэтому userUpdated.id
получается через inline fragments User/UserFragment. В теле подписок нет mutations,
текстов ответов и данных выдачи. Origin middleware проверяет каждый handshake request,
включая redirects. TLS verification включена, max frame=1 МБ, cookie jar выключен.

Сообщения сокета не становятся заказами и не запускают выдачу. Сначала сохраняется
coalesced sync_requests revision по chat/deal/catchup, затем выставляется asyncio wake.
Synchronizer обрабатывает до 8 адресных requests за проход под общим lock; API ответы
проходят прежние проверки/envelope и durable inbox. completed_revision записывается
после сохранения канонических данных, поэтому сигнал, пришедший во время await,
остаётся pending. Ошибки/Retry-After сохраняют not_before и не блокируют другие цели.

Периодические full-pass chunks имеют отдельный monotonic deadline и не заменяются
WS hints. После ACK и обрыва запрашивается ограниченный catchup cycle; завершение этого
request не означает, что закончена вся история. API polling, cursors и BACKFILL сохраняют
свою семантику. Глобальные подписки ускоряют обнаружение, но мгновенная доставка каждого
события и наличие server replay не предполагаются. Перечитывание страниц и дедупликация
в inbox обязательны. Сырой WS payload не хранится; сохраняется только запрос сверки.

Сокет имеет handshake timeout, application ping/pong deadline поверх aiohttp.receive
(его внутренний таймер может продлеваться native ping), backoff/jitter и корректную
отмену всех child tasks. Health/queue сохранены в SQLite и доступны в диагностике.
PLAYEROK_REALTIME_ENABLED=false отключает сокет, сохраняя API polling. Зависимость
прямо объявлена aiohttp>=3.14,<4, установленная версия уже включена в requirements.lock.


## OrderManagement, OrdersPanel и история (v8)

OrderManagement — слой свежих sales read/guards поверх Orders. Sale проверяет OUT,
ID/предмет и продавца; deliverable требует PAID, явные false has_problem/is_automated,
владельца товара и чат. Queue дополнительно читает актуальную доступность чата и
сверяет snapshot. Executor повторяет guards перед mutation. Получение сообщения
не равнозначно SENT/CONFIRMED: ручная выдача вызывает только createChatMessage.

Orders.reserve атомарно закрепляет уникальный inventory и stable delivery key. Коллизия
другой операции не оставляет новый сиротский резерв. Empty stock выключает per-item
автоматику и сохраняет safe inbox notice по item/batch. API problem transition при
том же статусе имеет отдельное событие по committed history ID. Notifications не ждёт
baseline для таких тревог и не отправляет устаревший статус или уже пополненный stock alert.

order_history (v8) хранит наблюдения/действия без секретов inventory; order_annotations
имеет owner actor и optimistic revision. finish/recover/manual resolution пишут delivery
history внутри своей транзакции. Старые заказы импортируются как состояние на момент
миграции, а не полная реконструкция. API logs остаются отдельным источником снимка.

OrdersPanel инкапсулирует карточки/поиск/фильтры/history/notes/stock confirmations.
Кеш при APIError явно помечен; выдача из него недоступна. Stock values в confirmation
не отображаются. Сервис/native SDK contract использует get_deal(deal_id=...), а не id.
Статический аудит kwargs и реальный mock HTTP тест защищают границу SDK.
Fulfillment requirement/checklist и platform problem reporting пока не реализованы;
legacy updateDeal не используется как обход нового обязательного процесса.
