# Playerok Control Center

## Ordered gift bundles and console events (schema22, 2026-10-31)

This extends schema21's GiftService/Outbox, not a second delivery engine. Canonical
gift_code now supports an ordered comma list; repetitions are quantity. Listings
and jobs retain a JSON bundle snapshot; gift_steps(order_id,position) stores each
paid intent/receipt with unique reference/form/Outbox IDs. Migration22 copies old
single intents with their states unchanged and never creates/rearms operations.

Enrollment atomically snapshots all positions, approved prices, description hash
and policy revision. Before initial dispatch every remaining gift is preflighted,
recipient is pinned, cumulative availability/balance/whole-order/day limits checked.
SQLite BEGIN IMMEDIATE reserves the remaining Stars. Daily accounting adds actual
or uncertain dispatches today plus all outstanding reservations (including old-day
reservations). A dispatch-count fence rejects stale balance observations from a
concurrent preflight. One SPENDING/UNCERTAIN paid operation blocks concurrent spends.

Each step uses existing gift_send and leases. Store.finish atomically commits the
step receipt and registers only the next pending position; all-step success sets
parent SUCCEEDED and the exact Stars sum. Partial failures retain successful steps,
parent BLOCKED/UNCERTAIN and reserved remainder. No full refund or SENT after any
payment intent unless the existing separate completion conditions are satisfied.
Owner retry only admits a blocked step that provably has no form/dispatch, unchanged
bundle/description and enabled current policy. UNKNOWN is only resolved by reliable
read evidence, never by an empty search or new purchase. Explicit read reconciliation
can advance one confirmed position while retaining the original UNKNOWN Outbox row.

GiftWorker still owns one final buyer notice/eligible Fulfillment after parent success.
Existing Inventory/DeliveryChains exclusion applies to every gift job. Owner receipts
and gift UI show per-position progress, confirmed versus unknown Stars, partial
state, reasons and operation history. Public lists/logs omit recipient/private peers.

Standard logging retains SecretFilter and rotating files, with Moscow-time console
formatter, ANSI feature detection and structured event/order/operation/position.
Console and file handlers flush normally; launcher is foreground/unbuffered UTF-8,
one process and existing InstanceLock. No new windows/background log process.
Success events follow committed receipts; preflight/dispatch are distinct. Unchanged
sync cycles are DEBUG, state changes INFO, repeated identical API read errors bounded.
SecretFilter also masks usernames, dict-form credential fields, JWT, session paths,
control characters and context injection even at DEBUG. Raw tracebacks remain excluded.

## Personal Telegram Gifts (schema21, 2026-10-30)

app.gifts is an integration into the existing bot/runtime, not a second bot or
independent payment queue. GiftWorker only enrolls future PAID own orders and
orchestrates follow-ups; gift_send/gift_refund/gift_notice run through the shared
OutboxWorker/Executor. Store.finish and lease recovery atomically update gift_jobs.
Inventory, Orders, existing chains and manual Fulfillment keep their original roles.

gift_listings pins owner/item/category/obtaining/receiving-field IDs, code, actual
approved Stars price, description hash, arm cutoff, revision and independent auto-SENT
opt-in. gift_catalog stores read MTProto observations, never treats seed prices as
current proof. gift_jobs is one durable intent per order, unique outbox/form/reference,
with recipient/payer IDs, private peer access hash, cost, form ID, dispatch time,
delivery receipt, refund state and completion outcome. These fields stay in private
SQLite/backups; public receipts use bounded outcomes and IDs without recipient data.

Before payment, fresh Playerok PAID ownership/field/schema/listing checks surround
MTProto preflight. resolveUsername is uncached and repeated before spending. Only
User/InputPeerUser is supported. getFullUser disallowed_gifts, catalog restrictions,
checkCanSendGift, balance, exact XTR form price and configured budgets must pass.
The personal account ID binds once to the DB; a changed session identity fails closed.
A session file lock also prevents concurrent use by local login/another runtime.

SPENDING is persisted in BEGIN IMMEDIATE with lease/policy/local-order checks and a
global paid-intent lock before exactly one sendStarsForm. Telethon request_retries=0,
auto_reconnect=false and flood_sleep_threshold=0 prevent implicit RPC-level replay.
The fixed pinned Telethon version is1.45.0 (layer229). Telegram documents same-form-ID
idempotency, but the application never relies on automatic resend/new forms after
an ambiguous paid call. UNKNOWN holds global purchase admission across day changes.

PaymentResult alone is not delivery. Independent message read must match an outbound
MessageActionStarGift, recipient, gift ID, date, non-refunded state and random opaque
invoice message reference. Receipt and SUCCEEDED commit together in Store.finish.
Cancellation/lease expiry after intent becomes UNCERTAIN. Pre-intent interruption is
BLOCKED, not automatically replayed. Explicit owner retry is allowed only for a proved
PERMANENT_FAILED preflight with no form/dispatch/refund; it retains the old receipt and
creates a new operation linked to the same unique order intent.

Gift and stock/chain enrollment/reservation are mutually exclusive under the same
transactions. Description/schema edit admission immediately disarms gift policies;
external edits are caught by fresh preflight. Price/availability changes require
explicit rebind. Completed follow-ups are excluded from bounded scans, avoiding
starvation by old deliveries. Customer notice failure never changes payment outcome.

Automatic completion is a separate explicit per-listing permission and only applies
to existing legacy Fulfillment after stored gift receipt proof. Fields/draft report
requirements remain in the existing owner form; provider identities are not invented.
The normal stock engine still never issues automatic SENT. Gift delivery confirmation
is independent of Playerok order completion and buyer confirmation.

Rollback contract was independently verified in fresh public Playerok deal-page JS:
seller OUT participant, active PAID/SENT, non-system; same updateDeal/ROLLED_BACK mutation.
The adapter admits a conservative subset: own PAID/no-problem/non-automated, known USER
seller_type/role. TG_GIFT_REFUNDS_ENABLED is an additional OFF-by-default permission.
Only definitive resolveUsername USERNAME_INVALID/USERNAME_NOT_OCCUPIED may enqueue a
single gift_refund before any Stars intent. Server rejection or lost ACK never creates
another refund. Independent get_deal ROLLED_BACK is required before refund-success
notice; unsupported/disabled/unknown cases are manual with honest buyer notification.
Telegram payment uncertainty permanently excludes automatic refund for that order.

Production checkout receiving field is OBTAINING_DATA with required=false. The app
requires its exact ID/value and fails closed; it cannot change Playerok's checkout
schema. No label/comment/chat/automation-field fallback exists. No real MTProto login,
paid gift, buyer message or rollback was executed for this integration. See README_GIFTS.md
for setup, documented contracts and the remaining controlled-live acceptance gate.

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
Fulfillment requirement/checklist описан ниже; platform problem reporting пока не реализован;
legacy updateDeal не используется как обход нового обязательного процесса.


## Seller fulfillment

Fulfillment отделён от выдачи сообщения/кода и от buyer CONFIRMED. Роль и статус
берутся из свежей собственной OUT-сделки. Обычный seller использует только SENT;
OFFICIAL_SELLER использует REST requirement (none/draft/fields). Отсутствующие роли
и ошибки не являются разрешением для legacy. Snapshot включает режим/роль/checkbox
и существенные поля сделки. Fields нормализуются по текущему официальному form contract.

fulfillment_transport принимает только фиксированные same-host public REST paths,
проверяет допустимый JSON shape и write permit/thread перед захватом RLock и внутри
него. Нет произвольного URL/redirect, нет повторной mutation. SafeAccount предоставляет
явные методы; чтения проходят whitelist Playerok.call, запись — Executor.mutate.

fulfill outbox конкурирует с delivery по target. PREVIEW не создаёт запись. Worker
повторяет preconditions, отправляет ровно одну mutation, затем ограниченно читает
состояние/summary отчёта. Неопределённый write или read-after-write переводится в
UNCERTAIN. HTTP 2xx POST без подтверждённого результата недостаточно. Повторные GET
не повторяют POST/updateDeal. Ручная сверка использует тот же result verifier;
CONFIRM_READ_STATE и actor/исходные ошибки остаются в журнале. Stopped-unknown fulfill
также блокирует новый POST, поскольку отсутствие записи не доказывает отсутствие приёма.

FulfillmentPanel сохраняет checkbox/поля/выбор поставщика в SQLite FSM, меняет nonce
на каждом экране и требует отдельный submit. Provider retrieval failure сохраняет
введённые данные. Поля отчёта не копируются в audit/order_history; стоимость отчёта
не создаёт financial_operation и не переводит деньги. Native SDK parser сохраняет
новые raw role/sellerType, чтобы OFFICIAL_SELLER не терялся до выбора workflow.


## DeliveryPolicy и dry-run оценки (v9)

Automation хранит desired DeliveryPolicy по item_id с revision/actor и транзакционным
CAS. Policy не является разрешением worker execution: runtime_enabled=False на уровне
оценки, никакого вызова Executor/Orders.reserve из этого сервиса нет. Изменение политики
не редактирует объявление. Режимы manual/text/stock, message fields и mark_sent имеют
строгую валидацию. Secret-bearing поля сохраняются в локальных policy/FSM, не audit.

AutomationPanel содержит per-item редактор и local global-stop. Кнопки имеют nonce;
сохранение сверяет свежего продавца и версию. Несохранённые изменения не используются
для dry-run. Общий список показывает последние 20, остальные доступны через карточки
товаров (полная пагинация настроенных политик ещё предстоит). Выключение меняет только
policy desired state; ручной durable outbox не отменяется по скрытому побочному эффекту.

simulate читает свежую продажу/чат и локальные резервы/операции/проблемы. Результат
automation_runs содержит причины/названия шагов и counts, без message/stock values.
Симуляция не резервирует и не исполняет шаги. Запрошенный mark_sent указывает на
необходимость отдельного Fulfillment workflow, а не на автоматическое CONFIRMED.
Greeting/after и fixed text сохраняются, но runtime цепочка ещё не подключена.


## Последовательности отправок (v10)

DeliveryChains создаёт order-scoped snapshot + step rows до первой отправки. Для stock
резерв атомарен с chain registration, без преждевременного delivery outbox. advance
под транзакцией привязывает к следующему шагу ровно один outbox ID и ждёт SUCCEEDED.
Unique order/step/operation constraints защищают restart/concurrent scheduling.
ChainWorker не enroll-ит заказы: запускаются только явно выбранные chains.

Executor.verify_step проверяет step ownership и все предыдущие операции, свежую сделку,
чат и локальную проблему, policy revision до и после awaited reads. Greeting/after
используют chain_message, fixed payload delivery_text; core stock — существующий delivery.
Timeout не повторяется. Failed/unknown шаг блокирует chain, резерв остаётся в карантине.
Изменение политики cancels chain. Сверка отдельного шага не возобновляет цепочку скрыто.
Запрошенный SENT требует отдельного Fulfillment workflow, NEEDS_FULFILLMENT лишь сигнал.

PLAYEROK_AUTOMATION_ENABLED=false и PLAYEROK_WRITES_ENABLED=false по умолчанию.
Для запуска нужны оба флага, сохранённая enabled policy, dry-run и явное подтверждение
конкретного заказа. Это не подписка на автоматическую выдачу всем будущим заказам.


## PromotionSettings / PromotionsPanel (v11)

Глобальная desired promotion policy имеет revision/actor и режим выбора all/selected;
per-item target overrides include/exclude. Сервис не вызывает mutation. Panel редактирует
снимок в SQLite FSM, сохраняет через CAS и проверяет владельца. Ночные часы задаются в
МСК; position mode требует две разнесённые, свежие observations той же policy revision.
priority_position scope явно ограничен: общий поисковый rank им не доказывается.

Evaluate перечитывает item/priority price по raw_price, проверяет status/selection,
outbox lock, интервалы и бюджет. Ledger RESERVED/SUCCEEDED/UNCERTAIN учитывается как
потенциально потраченные деньги; неизвестное не обнуляется. Превышение budget отключает
policy атомарным CAS с audit; evaluation хранится отдельной строкой без создания расхода.
Фактического платного исполнителя нет. Постоянный сбор rank/периодический планировщик
и уведомления budget guard требуют дальнейшей реализации. История расходов читает
только существующий ledger и не подменяет его dry-run журналом.


## Read-only PromotionWorker (v12)

Worker выполняет discover→leased claim→fresh API evaluation→persist next_due. Один
проход ограничен четырьмя лотами, discovery all — одной страницей, полный каталог
перечитывается после 300-секундного окна. selected не требует обхода API-каталога.
Курсор сохраняется только после внесения уникальных targets. Ошибка discovery сохраняет
исходный cursor и due/backoff, target failures независимы.

promotion_schedule хранит policy_revision, поэтому редактирование политики не ждёт
старого interval. Active lease не обходится даже новой revision; старый результат не
перезаписывает чужой token. Read-only replay после expiry допустим и не равен paid retry.
Проверяется current priority class; неоднозначная цена не заменяется выбранным premium.
Time/night интервалы планируют наблюдения, не фиктивные расходы/поднятия. Платного
исполнителя/резерва/queue mutations нет. Diagnostics показывает safe health.

Budget guard фиксирует inbox promotion_guard вместе с CAS-disable и audit. Notifications
обходит historical baseline для этой локальной тревоги и сообщает отсутствие списания.
Уведомление дедуплицируется устойчивым policy revision key. Shutdown worker ждёт stop
и оставляет leases/cursors в БД для восстановления следующего процесса.


## AutomationQueue UI

Фиксированный whitelist списков даёт полную пагинацию без API side effects. Сводки
не читают secret-bearing policy/text/stock columns; chain snapshot содержит только
метаданные и статусы шагов. Сохранённый hash snapshot + nonce разрешает stop только
для того же ACTIVE состояния. Под RLock/SQLite transaction повторяется snapshot check,
CANCELLED/quarantine/history фиксируются вместе. Внешний outbox outcome не подменяется;
finish по действующему lease остаётся возможным для запроса в полёте.

Local reschedule проверяет actor/актуального seller, не снимает active lease и не
включает disabled policy. Due/error сбрасываются только для следующего read-only worker
цикла. Unknown write нельзя повторить через эту кнопку. Списки ledger/evaluations
раздельны; historical evaluation не показан как актуальный quote. Telegram отображает
человеческие поля и МСК-время, не raw JSON. UI не добавляет auto-resume цепочек.


## Future-only EnrollmentWorker (v13)

auto_enroll — отдельный desired flag default false с явной аттестацией включения.
Policy updated_at является activation cutoff каждой новой версии. До orders baseline
worker не создаёт jobs; после него bounded discovery сохраняет PENDING/SKIPPED в
versioned auto_enrollment. Наличие local PAID недостаточно: серверный created_at должен
быть aware/после cutoff/не дальше пяти минут вперёд. Неизвестное время не подменяется
ingest timestamp. Fresh API response и revision повторно проверяются непосредственно
в транзакции DeliveryChains.create(automatic=True).

Leased jobs переживают restart/read errors. API retries безопасны, поскольку enrollment
не отправляет сообщения. Chain unique order key делает сбой после chain commit перед
completion идемпотентным локально; существующая chain не создаётся повторно. BLOCKED
требует owner review; durable notice записывается вместе с результатом job. Global
write/automation flags false выключают worker, policy opt-in сам их не включает.

Empty stock устанавливает per-item guard, учитываемый только при новом automatic
registration. Он не отменяет текущую зарезервированную последнюю единицу. Explicit
policy save восстанавливает desired guard; stock refill не включает его скрыто.
Смена revision отменяет активные chains по прежним правилам и меняет future cutoff.
SENT не выставляется без отдельного Fulfillment workflow; auto_enroll не является
buyer CONFIRMED и не вызывает денежные API операции.

## Statistics/profile data contract

Statistics aggregates an order-created cohort with CURRENT order status. Only aware
payload.created_at is historical evidence; the SQL ingest fallback is never used for
period attribution. Previous windows are half-open at the current start; the current
window includes its end. Amounts remain Decimal, unknown paid amounts block averages
and percentage growth. Coverage bounds describe known cached dates, not API completeness.
Confirmed RUB generic expenses and SUCCEEDED promotion ledger remain separate until
cross-source reconciliation is implemented. No calculator quote becomes a real charge.

Synchronizer schedules at most 32 missing-date deal details per full cycle through the
existing durable request queue (8 reads per drain, revision/backoff retained). An existing
completed detail request is not endlessly recreated merely because its date is absent.
Orders preserves a server date from the prior payload when list responses omit it.

StatisticsPanel and ProfilePanel own their Telegram output. Matplotlib generation uses
the existing process lock/to_thread, with shape-preserving Hermite temporal curves,
actual observation markers, real temporal spacing and explicit interpolation labels.
Smoothing cannot affect totals or produce extra extrema. Categorical comparisons use
bars. Profile fields follow AccountProfile parser contract, with no zero-to-unknown
coercion; public avatar download has a host/size/no-redirect/no-credentials boundary.

## Account transaction journal (v14)

account_transactions has composite account_id/id uniqueness, nullable unknown values
and server dates, raw-safe enum tokens and a small allowlist of monetary/deal fields.
Payment accounts/URLs/user data never enter this journal. No notification/outbox side
effect is generated from importing financial history. Repeated pages replace current
snapshots; terminal states reject stale PENDING/PROCESSING, same-state sparse responses
retain known server dates. Rollback removes a transaction from CONFIRMED aggregates.

PageScanner now contains the existing durable head-refresh/resume/opaque-cursor logic
shared by Synchronizer and TransactionWorker. The transaction worker independently
scans two pages/cycle, scopes cursors to API account identity, checks identity before
any page/cursor commit, persists error backoff/Retry-After and exposes health. Read-only
sync starts with other readers after account initialization; paid flags are irrelevant.

StatisticsPanel has a separate transaction view. It sums confirmed RUB movements by
server completed_at, discloses excluded undated/unknown/currency records and last full
pass. SELL credits, deposits, withdrawal debits, PREMIUM and other priorities are
distinct buckets. Unknown operations remain in direction totals without pretending to
be sales. Explicit server withdrawal net values remain distinct from balance debits.
Root fee is not treated as monetary commission; calculator quotes/generic expenses/
promotion ledger are not merged. Historical cost/profit and full reconciliation across
orders/withdrawals/promotion sources remain acceptance work.

## Historical order finance (v15)

order_costs stores owner-declared total order cost/other expenses with explicit unknown,
revision, actor and review fingerprint; product forecast cost is not retroactively copied.
order_expense_allocations links orders to existing account_transactions using a composite
foreign key. Eligible expense capacity is confirmed RUB priority OUT value or withdrawal
value minus explicit server net RUB. No current rate or ambiguous fee calculation.

Cost/allocations/review changes commit with owner + seller checks, per-order revision CAS
and source capacity constraints under BEGIN IMMEDIATE. Allocation sums across all orders
cannot exceed source capacity; zero removes a claim, even if the source later rolled back.
UI confirmation also checks source fingerprint in the same allocation transaction. Economic
fingerprint excludes local observed_at, includes receipt/source facts and current order status.
Any changed dependency revokes displayed reviewed profit without rewriting historical inputs.

Profit requires confirmed order, one linked confirmed RUB SELL receipt, known completion date,
explicit cost/other expenses and owner completeness attestation. No fabricated zero defaults.
Statistics aggregate only eligible reviewed orders by receipt completion date; incomplete and
unattributed coverage remains visible. All features are local reads/writes, with durable FSM,
preview, audit and no outbound payment, stock delivery or buyer-message side effects.

## HTTP auth diagnostics and exact receipt links

APIError retains the compatible public code and optionally structured sanitized HTTP
facts. SafeAccount keeps at most 16 response facts: fixed endpoint without query values,
method/operation/status/type/server/body size/hash and bounded credential-redacted errors.
Successful account bodies are omitted. Mutation error messages/excerpts are omitted to
protect delivery data. app.auth_diagnostics performs only get + one get_transactions page,
stops on errors without bypass/retry and saves a local ignored report. Existing main
logging remains code-only. Old merged 401/403 errors cannot reconstruct a historical cause.

Real transaction history often omits props.deal_id. OrderFinance also accepts exact
native deal.transaction.id, constrained to the same account/SELL/IN/CONFIRMED and a null
or matching transaction deal_id. Contradictory links or the same receipt referenced by
another saved order block reviewed profit; there is no amount/time similarity matching.

Temporal chart ticks now use a calendar locator rather than every Nth observation;
large historical gaps remain breaks, with interpolation only between adjacent hourly/
daily observations. Real 2024–2026 data exposed overlapping labels and gap interpolation
that synthetic evenly-spaced fixtures did not, and was used for visual regression.

## Dispatch boundary and late observations (v16)

Executor passes the claimed action to each mutation call. A synchronous local guard
checks writes_enabled, PROCESSING/token/unexpired lease and chain policy/runtime,
current PAID/local problem, ordered steps and stock reservation. It runs after the
shared API lock and again inside SafeAccount transport after rate throttling, before
HTTP dispatch; the REST fulfillment path uses the same guard. Earlier fresh remote
validation remains required. This closes stop-while-waiting without claiming remote
requests already sent can be revoked. No DB transaction is held across network I/O.

OutboxWorker.finish distinguishes a lost lease from a still-owned commit failure.
Lost-lease outcomes are retained once per operation/token in outbox_late_results with
allowlisted receipt fields, never message text/report payload. Authoritative recovered
UNCERTAIN/quarantine or owner resolution remains untouched. The operation UI exposes
late receipt IDs for read reconciliation; there is no automatic resend or success
promotion. Failure while the lease is still owned propagates for recovery, rather than
being concealed as a late outcome. An actually crashed process cannot save a late reply.

## Per-owner notification preferences

NotificationPreferences uses the existing settings table with an explicit boolean map
and revision. Owner/CAS/audit writes are transactional. Notification suppression and the
notification deduplication key commit together; SUPPRESSED events are not replayed after
a later enable. Preferences apply at processing time. Mandatory operation_uncertain
bypasses preferences; invalid preference shape suppresses noncritical events with a
separate status and diagnostic marker rather than killing the worker or enabling sends.
Existing delivered notification action updates remain independent of preference toggles.
SettingsPanel owns local configuration navigation; it exposes no secret or payment toggle.

## Local draft navigation

DraftsPanel owns read-only pagination/search/filter state. draft_page scopes SQL to
owner_id, returns public name/ID/state/time fields, never the full payload, escapes
wildcards and clamps out-of-range pages after collection changes. Stable date/id sort
makes tied timestamps deterministic. Filters distinguish local workflow states from
remote listing statuses; unknown future states remain reachable. Existing draft opener
still handles wizard restore/binding links; reading the list creates no external work.

## Server chronology vs local ingest time

orders.created_at remains the historical local persistence field; no bulk rewrite is
needed to correct display order. Store registers deterministic SQL server_time(value):
aware ISO → UTC fixed-microsecond string, invalid/naive/overflow → NULL. Order listing
sorts the normalized payload server date descending, with local receive/ID only for
ties and unknown-date order. NULL dates remain last and are not attributed to sales
periods. The returned listing DTO includes the normalized server date. Ingest preserves
a previously verified date when newer sparse/malformed responses lack a usable date.

## Explicit Telegram interface cleanup (v17)

OwnerMiddleware sets/reset panel_owner ContextVar around authorized private handlers.
PanelTracking request middleware records only successful Message responses produced in
that context, scoped by bot/owner/message and without content. Background notification
sends have no context and remain untracked; notification mappings also exclude any later
UI-edited notification from cleanup. Untracked historical IDs are never enumerated.

InterfaceCleanup previews at most 20 ACTIVE records aged <=47h and untouched >=5min,
excluding current message. Exact snapshots and all eligibility conditions are rechecked
in BEGIN IMMEDIATE before the batch is marked DELETING. Telegram calls occur outside
transactions, with each result saved; timeout/429 stops the batch, cancellation/restart
leaves conservative UNCERTAIN. No automatic deletion/retry is scheduled. Panel exposes
aggregate statuses, explicit irreversible-delete confirmation and stale token rejection.
No Playerok/outbox/stock/customer message or historical data deletion path is connected.

## Rule-based auto replies (v18)

auto_reply_rules are owner/account scoped with CAS revision and arm cutoff;
auto_reply_events provides unique account/message registration and durable decisions.
Exact normalized trigger matching has no regex/external AI calls. Enabled rules require
explicit future-message attestation; runtime additionally requires writes+automation and
completed sync baseline. Both source server date and first inbox receipt must pass cutoff.

Worker considers 20 events/cycle, performs fresh PM chat/participants/message checks,
then transactionally registers an existing reply-kind outbox operation and limit claim.
Shared-chat cooldown, Moscow-day per-rule attempt limits, pending/uncertain reply and
order delivery/fulfillment guards prevent overlapping work. Attempts remain counted
across revisions and uncertain outcomes. Read errors block the event and persist a batch
backoff; no silent replay of old traffic. Outbox dispatch repeats source/rule/account
validation at the transport boundary. All real sending is inherited from the guarded
reply executor and its read reconciliation; registration alone never sends a message.

UI exposes create/edit disabled, separate arm confirmation, full paginated response
preview, stop, disabled delete and bounded journal. No secrets/content enter audits.
No runtime permission flag is changed by rule configuration; production stays disabled.

## Packaging and offline installation check

app.selfcheck imports packaged application modules, checks migration continuity and
certificate, exercises a disposable SQLite database/backup/reopen and PNG rendering.
It never calls Config.load or remote services and never opens the working database.
Reports contain module/dependency/schema metadata and fixed allowlisted error codes.
Console scripts expose pcc/pcc-check; README is wheel metadata and vendored MIT license
remains included. Installed-wheel smoke runs outside the source directory with editable
package discovery disabled and verifies package origins under the isolated target.

Store.backup uses contextlib.closing for its destination connection. A sqlite3 connection
context only commits/rolls back; it does not close. Retaining that handle prevented prompt
cleanup of backup files on Windows, exposed by offline installation checks. The regression
test asserts the destination connection is closed and the file can be immediately removed.

## Auto-reply arm boundary under equal clock timestamps

A rule save atomically captures MAX(inbox.rowid) into its settings boundary alongside
rule revision/cutoff. Matching requires a strictly newer first inbox row plus aware
created/received dates not before cutoff and recent age. This prevents historical
messages on the same clock tick from being armed retroactively. Duplicate imports do
not replace the original inbox row; missing boundary fails closed. The deterministic
regression fixes all three event timestamps equal and proves old skipped/new eligible.

## Observed settlement amounts

settlements.py contains pure sale/withdrawal reconciliation, independent of current
provider quotes. Sale fee is accepted only for confirmed RUB SELL IN with known date
and exact gross - root_fee == credit; no percent multiplication/fallback. OrderFinance
supplies seller/account/unique receipt evidence and captures gross/reported_fee in the
review fingerprint. Settlement aggregation uses receipt completion time and excludes
ambiguous/unknown evidence, with coverage counters. Owner cost completeness is unrelated
to whether a fee itself can be reconciled; net profit never subtracts that fee twice.

Withdrawal retention requires explicit positive server net RUB for a positive debit;
zero conversion fields are unknown, consistent with wallet UI truthy-field gating.
Statistics and expense allocation share the same helper, preventing a zero placeholder
from becoming a full-debit expense. Unknown withdrawal coverage is displayed; root fee
zero/current provider fee are never used to invent historical bank receipts or fees.


## Background loop supervision

Every configured background loop is launched through Supervisor. Unexpected exception
or normal return triggers persisted exponential backoff (5–300s, jitter); stable runs
>=60s reset consecutive failures. Cancellation and shutdown never restart a loop.
Factories create a fresh coroutine, retaining existing durable worker recovery rules.
Supervisor cannot retry individual requests or turn PROCESSING/UNCERTAIN into pending.

Health settings and coalesced worker_failed inbox incidents commit atomically. Database
failure is logged by exception type only and leaves in-memory restart delay active;
pending alerts are retried on the next health save. Critical notices bypass baseline
and category suppression, with existing one-attempt Telegram semantics. Diagnostics
show timestamps of last saved lifecycle state, not live API health or a hang watchdog.
Unexpired outbox leases remain PROCESSING; expired leases recover to UNCERTAIN, never
blind resend, even when external ACK preceded failure to save the local result.


## One database, one verified account

AccountBinding runs after successful get and before background tasks. It atomically
compares canonical/legacy identity settings, transaction account IDs and known order
seller IDs. A fresh database can bind once; repeated login is idempotent; conflicting
identities, malformed settings and unattributed existing orders/outbox fail closed.
BEGIN IMMEDIATE serializes competing first binds. No old queue/stock/finance is adopted
by another account. A different shop requires a different DATABASE_PATH. This explicitly
supports a single shop per database rather than claiming multi-account isolation.

PlayerokConnection shares SafeAccount/get and AccountBinding with ProfilePanel. Every
startup requires fresh verification; stored success is historical only. Auth/account
rejection is distinct from unknown network/challenge results. Failed verification keeps
local owner diagnostics available while write/automation admission is closed and read
workers wait. Explicit successful profile verification resumes configured workers within
existing permissions. Checks are serialized with a five-second completion cooldown;
the final HTTP write boundary also checks the verified account identity. Read responses
are rejected if identity changes during the call. Credentials/IDs are omitted from binding
errors, and conflict does not reset or reassign old data. No binding schema migration is
needed; canonical identity is stored in existing settings only after successful checks.


## Cancellation ownership for synchronous API calls

ThreadCalls owns each inner task, its async API lock and actual synchronous thread
lifetime. Cancelling the shielded caller signals a threading.Event without releasing
serialization. Queued calls check before thread entry; mutation transport guard also
checks cancellation after throttling. Once HTTP is in flight, cancellation cannot revoke
it. Outbox remains UNCERTAIN, never requeued. A late raw method result is not the full
executor success predicate: only narrow message/remote IDs are retained as UNCERTAIN
late observations, with no buyer text. Evidence observer errors log types only.

Main runtime and read-only diagnostics drain pending calls/observers before client/DB
close. Drain yields to completion callbacks explicitly (Python3.12 completed-future
fast path); the registry includes evidence observers. Forced process death or storage
failure may prevent late evidence persistence. Existing HTTPX20s inactivity limits do
not establish an absolute total-request or shutdown deadline. Legacy controlled
live_test helper is separate and must be reviewed before any new authorized use.


## Terminal API admission during shutdown

begin_close_calls marks an API closed even before its first ThreadCalls registry is
created. Closing is terminal for this client instance: new calls reject immediately;
queued lock/thread-pool entries recheck before method invocation. Registry closing
sets all active cancellation signals so mutation transport guard blocks at the last
pre-HTTP check. Requests already dispatched still finish under retained serialization.
Playerok read callers receive API_CLOSING; mutations not entered fail validation.

Main closes admission synchronously after stop.set and before worker cancellation,
then gathers worker journals and drains calls/observers. Read diagnostics close
admission in finally. Late Telegram handlers therefore cannot revive a transport after
resource closure. This does not revoke in-flight HTTP or impose a wall-clock deadline.


## Retired technical-listing scenario

app.live_test is an offline historical journal inspector, not a write executor. The
previous create/edit/automatic-cleanup technical-listing scenario was rejected and
removed. Missing/invalid/finished history cannot rearm it; no credentials or transport
are constructed. Output contains bounded allowlisted recorded state/booleans only;
remote IDs, names, cookies and raw errors are not reflected. The original journal is
never modified. Inspection cannot establish current remote state or authorize cleanup.

Native permit contract helper now lives only in tests/transport_helpers, with guards
requiring MockTransport both as default and for nonempty mounts. Tests are not included
in installable app/playerokapi packages. Production mutations remain in durable
Executor/Products/Wizard paths. Future live acceptance needs a separately authorized
real-goods scenario; the retired technical listing is never retried.


## Volume ranking separate from known revenue

Statistics tracks paid order counts per native item ID and category name independently
of known money. Unknown amounts still count; per-group unknown counters disclose
coverage. Item names use latest aware server order time with order-ID tie-break, not
SQLite insertion order. Count ranking uses the same created-order cohort/current paid
statuses as existing revenue analytics; refunds/future/undated rows are excluded.

StatisticsPanel exposes bestsellers/category_counts for top15 count-based rows, while
items/categories remain known-revenue views. Unknown-only groups show no known sums;
true zero remains zero. Native IDs prevent merging same-named listings, but are not an
automatically inferred logical SKU. Current category grouping is by saved name and is
labelled accordingly. Count data does not change charts, quotes, transactions or profit.


## User-facing server list checkpoints

ListingsPanel owns item/chat page rendering and durable FSM cursor/seen/nonce/number.
Existing validate_page verifies envelope and detects continuation cycles before state
commit. Next buttons carry a per-page nonce; stale/legacy/wrong-kind callbacks fail
before network. Terminal pages clear cursor/nonce; failed responses preserve previous
checkpoint for read retry. Empty continuation is distinct from EOF; traversal capped
at1000 pages. Callback UTF8 size is checked before state writes. Owner middleware and
per-user callback isolation provide serialization; SQLite FSM preserves continuation
across dispatcher reconstruction. Filters restart traversal with upstream status enum.


## Item filter contract

item_filters.py validates composed status/name/price/category/priority settings. Native
get_my_items(category_id) handles category within current user's scope; exact status
uses native enum. Literal name, inclusive Decimal price and exact PREMIUM/DEFAULT are
local predicates per validated server page. Unknown price/priority never passes a
constrained predicate. UI labels per-page loaded/matched coverage and keeps continuation
on empty match pages. No unsupported server query fields or full-history search claim.
SQLite FSM persists settings; changes reset cursor and preserve other filter dimensions.
Category input currently uses native ID; a convenient category-name picker remains open.


## Named category filter picker

ItemCategoryPicker replaces manual-ID-only UX with get_games name/cursor pagination,
get_game categories and local10-row category pages. Durable FSM holds bounded public
ID/name snapshots and nonce/index callbacks. Fresh get_game identity/membership check
precedes application; changed/disappeared category fails without changing item filters.
Existing filter dimensions survive selection; item page cursor is reset. Names need
not be unique. Cancellation closes state; stale callbacks reject before API. Game pages
use validate_page/cursor cycle detection; category list bounded1000 entries, searches
1000 pages. Manual category-ID entry remains optional, not the only navigation path.


## Read-only item card/gallery

ItemPanel checks current account/item/user identity for card and every gallery read.
Gallery FSM holds only bounded attachment IDs and nonce; fresh item provides current
URL by ID, preserving selection through reordering and blocking removed/hidden media.
Existing Attachments.remote enforces media host, no credentials/redirects, size/decode
limits and temporary-file cleanup. Text card remains with a separate photo navigation
button; existing edit/delete/preview workflows are retained. Counters require nonnegative
int, with unknown distinct from0; deals_counter is labelled API deals, not settled sales.
Position is explicitly within priority scope. Link path segment is URL-encoded.


## Explicit publication/deletion receipts

SafeAccount overrides upstream remove_item because native upstream discards data and
returns True regardless of deletion evidence. Existing pinned mutation query returns
an Item object: exact ID and aware deletedAt required, otherwise uncertain APIError.
No raw item body retained. Executor delete accepts literal True only; no truthiness
fallback. Publication requires exact ID and one of APPROVED/PENDING_APPROVAL/
PENDING_MODERATION; missing or other states are not successful publication receipts.
Unknown outcomes keep normal UNCERTAIN/no-resend semantics. Moderation acceptance is
not active listing proof, deletion receipt is not a complete independent-history scan.


## Ordinary edit result evidence

item_receipts.edit_receipt_matches requires exact requested name/description echo,
finite exact Decimal price and exact target ID. Replacement images require exact count,
unique nonempty new IDs and no overlap with removed IDs. The predicate does not demand
unrequested fields or claim content-hash verification. Executor records unknown when
evidence is missing/inconsistent and never resends. Existing local upload cleanup keeps
files referenced by UNCERTAIN edits. Schema edit_fields is a distinct path, not accepted
by the ordinary-edit predicate. Positive mock fixtures now reflect requested fields.


## First-sale MVP boundary

Creation now validates DRAFT/current owner/category/requested fields and structural
photo IDs before binding/REMOTE_DRAFT. Bad/incomplete response remains UNKNOWN without
recreate. Existing stock policy is armed explicitly for the new remote ID before future
order cutoff; wizard delivery preferences do not silently enable automation. Connected
MVP test covers native service/worker handoffs, duplicate ingest, restart, quarantine
and existing explicit owner fulfillment. Stars/gift/provider transfers are outside this
stock-code engine and cannot be attested from a chat message receipt.

## Final gift boundary audit (31.10.2026)

Before a gift intent is committed, the current payer is compared again to the pinned
personal identity. Definitive recipient rejection persists the exact private receiving
value; refund queueing and final dispatch recheck that field and item against a fresh
deal. Native item.user.id participates in SQLite account ownership validation.
PersonalGiftSender.close is terminal and serialized with connect; the session file lock
is released only after disconnect. Shop event handler failures cannot interrupt durable
worker transitions. Existing Outbox/intents/UNKNOWN/refund/stock contracts remain in place;
no new subsystem or migration was introduced. Evidence and residual transaction boundaries:
GIFT_SAFETY_AUDIT.md.

## Dedicated localVPN Playerok tunnel (31.10.2026)

app.localvpn owns a private JSON-over-pipes Go service, independent runtime/mutex/Job
and1080 engine, reading the existing catalogue only in Go. New NetworkSettings defaults
to localvpn; worker/legacy/direct require explicit mode and cannot act as fallback.
aiogram's common ProxyConnector includes downloads; Telethon/auth share SOCKS rdns.
Readiness/generation fences gift admission and final paid dispatch without changing
Outbox/receipt/UNKNOWN semantics. Service EOF/Job ownership controls crash cleanup.
Callback ACK precedes per-owner FSM isolation, with bounded timeout and conservative
mutating-action admission. Full contracts/evidence/Go file list: LOCALVPN_INTEGRATION.md.


## Narrow automatic seller agreements (31.10.2026)

SafeAccount.operation_allowed grants the separate auto flag only to the fixed
acceptGameCategoryAgreement mutation, behind verified current identity. Executor also
checks the SQLite binding/scope/lease; its thread-side final read uses the same native
client before each mutation. Kind/operation-ID-filtered Outbox claims prevent other
queued writes from executing in read-only mode. Products schema preparation drives
the same queue inline; open wizard waits persist and AgreementResumer uses the existing
Dispatcher FSM isolation after restart. Unknown results are reconciled by read-only
server-no-pending proof, never reset/retried. Existing settings hold version-hash journal
and churn budget; no schema migration or new delivery/payment architecture. Optional
terms inspector bypasses acceptance and uses fresh read-only schema snapshots.

## Takeover: границы полномочий и первого запуска (2026-11-01)
ListingLaunch PREPARE обновляет только наблюдения (каталог/баланс/проверенный payer); управление расписанием и денежная authority фиксируются вместе с локальной выдачей/расходом/Outbox/маркером. Scoped `promotions:launch_authority:<item>` связывает account/policy revision/launch revision; не включает расходы других лотов. Ранее явно подтверждённый глобальный arm остаётся отдельным полномочием. Release удаляет только grant своего запуска, а delivery:<item>:enabled выключает только после успешного CAS соответствующей policy revision.
`gifts:stop_revision` — монотонное локальное поколение STOP. Возобновление подтверждается отдельно, перечитывает сессию и сравнивает поколение внутри транзакции после сетевых чтений. Arm товара не отменяет глобальный STOP.
Для впервые созданного приложением DRAFT серверная created_at используется как enrollment cutoff только при совпадающей успешной create-квитанции, владельце/привязке, отсутствии прошлых публикаций/заказов. Фактическое время решения сохраняется отдельно в launch marker. Старые записи без доказательств не получают ретроактивного разрешения. Схема SQLite не меняется, миграция не требуется.

## Пустые подписи и корреляция receipt (2026-11-01)
InputInvoiceStarGift.message для новых отправок отсутствует. Поле reference остаётся внутренним audit/idempotency ключом. Миграция023: receipt_mode REFERENCE(default для старых строк)/MESSAGE_ID(новый intent), ack_message_id; уникальность payer+recipient+ACKID. Режим записывается в одной транзакции с SPENDING до внешнегоRPC. Единственный подходящий Telegram ID из PaymentResult updates сохраняется до независимого get_messages(ids=[ack]). Это наблюдение исхода, а не новое разрешение. Потерянный/неоднозначный ACK не заменяется эвристикой по времени/похожим Gift; UNCERTAIN остаётся без повтора. Старый reference-mode обслуживается только для совместимой read-only сверки уже начатых старых операций. SQL-сбой записи ACK также сохраняет неизвестность; исправленная история не создаёт новую форму/платёж.

## Logical product identity (schema24, 2026-11-01)

LogicalProducts is an identity/policy projection over existing Gifts and DeliveryChains, not an execution engine. Immutable public #7digit codes identify account-owned configurations; logical_listings tracks verified item IDs; logical_order_scopes pins order/version/config/canonical stock bucket. Inventory units are never copied on relist. Existing reservation, lease, Outbox, receipt and UNKNOWN boundaries remain authoritative.

Marker edits reuse edit/updateItem with fresh baseline guards and independent readback. Store.finish commits the verified binding with the operation. Unknown edits reconcile by read only. STOP increments a separate control_revision; commit_arm is invoked inside the existing policy/arm transaction, fencing late network completions. No network await is introduced inside SQLite transactions.

LogicalProductWorker reuses PageScanner with durable bounded listing/hydration checkpoints and account-scoped serialization. Only completed current active-catalog checks authorize opted-in inheritance. Fresh PAID deals supply already-SOLD candidate IDs without inventing parent_id or replaying historical orders. Legacy local projections never mutate existing gifts/intents or enable disabled policies. Details and limitations: UNIVERSAL_AUTODELIVERY_ACCEPTANCE.md.
