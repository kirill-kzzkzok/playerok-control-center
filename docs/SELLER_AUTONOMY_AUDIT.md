# Карта исходной системы и адресные изменения

База ce4b749 (master == origin/master). Полная регрессия этой базы:
2133 passed, 0 failed, 0 skipped; GitHub run 41827473939 SUCCESS.
Это подтверждение исходной версии, не приёмка будущей переработки.

## Область и честные границы

Детерминированная инвентаризация `SELLER_SOURCE_INVENTORY.json` содержит все
148 production Python-модулей (40535 строк, включая vendored PlayerokAPI),
33 SQL-файла, 198 тестовых модулей/1195 объявлений тестов и 460 объявлений кнопок.
Хеши, определения, импорты, граничные вызовы и состояния сохранены по каждому
файлу. Генератор не читает .env/рабочие данные и не обращается к API.

AST-инвентаризация НЕ доказывает исполнение маршрута. Исторические AUDIT_COVERAGE,
AUDIT_FINDINGS и FINAL_STABILITY_AND_UX_AUDIT используются как карта существующих
проверок, но их старые номера строк и статусы не подменяют проверку нового HEAD.
Runtime-покрытие всех динамических кнопок, SLA и live-интеграций остаётся
NOT VERIFIED до соответствующей фазы. Не заявляется отсутствие всех багов.

## Карта ответственности

| Область | Реальные production-пути | Существующие доказательства / что проверить заново |
|---|---|---|
| Запуск/владелец/подключение | __main__, config, security/access+instance, account_binding, playerok_connection | startup/account/STOP tests; запрет второму процессу/смене магазина |
| UI/FSM/сообщения | router, navigation, navigation_context, callback_ack, storage, panel_tracking, input_cleanup, transport | tests_telegram_transport, source-message/navigation regressions; измерить задержки общего owner lock |
| Все экраны | все 48 app/telegram модулей, полный список деклараций в JSON | пройти новые главные маршруты через Dispatcher, а не считать совпадение префикса обработчиком |
| Каталог/товары/черновики | listings_panel, item_filters, item_panel, drafts_panel, item_category_picker, catalog, products | активные APPROVED отдельно; объединить входы локальных/серверных черновиков без удаления данных |
| Создание/публикация | wizard, launch_panel, ListingLaunch, Products, agreements, Outbox | tests_final_all_creation_modes/platform_creation/listing_launch/launch_fault_matrix; defaults False/False требуют изменения |
| Идентичность/перевыставление | product_codes, LogicalProducts, logical_products worker, code_migration, quick_shop | tests_title_binding_repair/universal_delivery_e2e/private-deal; автоматический title-only контракт вместо прежнего ручного |
| Намерение и проекции | logical_products.enabled/control_revision, gift_listings, delivery_policies, delivery:*:enabled | stage_a_controls/unified_delivery_controls; не создавать новый движок или новый независимый enabled |
| Оплата/склад/текст | Orders, Inventory, DeliveryChains, EnrollmentWorker, ChainWorker, Store | final_paid_load/delivery_load/dispatch_recovery/stock_auto_cycle; UNKNOWN изолирован по заказу, последняя единица уникальна |
| Выдаёт Playerok/завершение | PlatformOrders, Fulfillment, platform_panel, fulfillment_panel, chain/outbox | playerok_delivery/final_platform_creation; платформа не получает вторую выдачу, отчёт не выдумывается |
| Telegram Stars | gifts core/settings/mtproto/service/execution/outcomes/worker + gifts_panel | bundles/empty_caption/resource_lanes; возможный UNKNOWN расход удерживает Stars-покупки, не независимый склад |
| HTTP/WS | playerok adapter/thread_calls/fulfillment_transport/response_diagnostics, vendored account/parser/listener, realtime/sync/pagination | API boundary/late result/polling head tests; сериализацию незавершённой мутации нельзя снимать ради скорости |
| localVPN | app/localvpn, native service DEFAULT_EXE, tools/patches/localvpn-five-candidates.patch | прежние восемь native offline tests + typed proxy failure; установленный протокол/процесс проверять отдельно, live не доказан patch-файлом |
| Уведомления/переписка | Notifications, OperationResults, chat_cards, ChatPanel, chat_events, replies, auto_replies | chat_cards/final_notification_baseline/notification_priority; приоритет новых событий уже до фоновых редактирований |
| Продвижение/деньги | PromotionSettings, PaidPromotions, PromotionWorker, promotion_schedule, finance/withdrawals/financial_quotes/order_finance/settlements/transactions | paid/native-relist/budget/unknown tests; упростить входы, не лимиты исполнителя |
| Аналитика/профиль | statistics service/panel, profile_panel, rendering/cards/charts | exact IDs и подтверждённые группы аналитики, fee provenance/Decimal/неизвестные значения не ноль |
| Данные/восстановление | database/schema+migrations+Store, reconciliation/edit_confirmation/item_receipts, backup_retention/selfcheck | migrations/backup/restart/lease tests; schema33, не сбрасывать UNKNOWN и старые пакеты |

## Подтверждённые несоответствия новому контракту

| ID | Проблема → первопричина | Исправление | RED / критерий |
|---|---|---|---|
| A01 | Простое название требует владельца: verify_listing сравнивает title_snapshot, title repair из ce4b749 требует title_apply | автоматическое read-only принятие только прежней идентичности/описания/полей/конфигурации с CAS; существующий Worker также должен его использовать | переименовать после UI-arm, без title_apply: свежая проверка и первая оплата проходят один раз; критическое изменение/STOP/UNKNOWN не обходятся |
| A02 | Главное меню содержит Подарки/Ещё, статистика/продвижение спрятаны, проблемы отдельной кнопкой | шесть канонических входов; проблемы оплаченных заказов внутри Заказов и краткий сигнал на главной | реальный /start, 6 кнопок, все достижимы, нет навигационной мутации |
| A03 | Maincard содержит readiness/settings/finance/edit/promo/history/other и дополнительные кнопки | четыре основные функции + контекстное управление; причина внутри Выдачи, технические детали отдельно | /status→товар→каждая функция→назад в той же карточке |
| A04 | Ручной старт и выключенные defaults: Wizard.start delivery=manual, auto_confirm=False; ListingLaunch.open inherit=False, auto_complete=False | обязательный осознанный выбор способа, подготовка до публикации, разумные defaults для новых конфигураций; не менять старые разрешения | все пять режимов через настоящий Wizard/Launch/Outbox, новый заказ сразу после публикации |
| A05 | Безопасный preflight Gift при API-сбое может стать BLOCKED с ручной кнопкой retry | исследовать started_at/form_id/spent_at; автоматизировать только доказанно не начатую операцию с существующим журналом и backoff | транспортный сбой до начала → восстановление → одна покупка; после начала → UNKNOWN навсегда без receipt |
| A06 | Сериализация FSM на владельца держится во время read-only HTTP/MTProto | отделять безопасное чтение от короткого CAS/UI commit, сохранить per-message context и финансовые fences | медленный read A не блокирует локальный Back B; поздний A не затирает новый экран; p50/p95 |
| A07 | ProductReadiness.refresh_due обслуживает один лот раз в15s при TTL120s | измерить 50 лотов, уменьшить дубли и очередь без освобождения HTTP-lock незавершённой операции | SLA/нагрузка на50лотов,50оплат; свежая выдача не использует UI-cache как авторизацию |
| A08 | Продвижение содержит разные quote/participation/paid/schedule UI, настройки и черновики дублируют входы | один пользовательский вход, редкие параметры вне обычного потока; совместимость старых кнопок | до/после клики/экраны/подтверждения; без оплаты при навигации/неподтверждённом бюджете |

## Уже безопасные области — не переписывать без нового RED

- orders_sync_failed исключает только доказанно независимые chat-stream ошибки;
  неизвестная глобальная ошибка остаётся консервативной. Native chat403 требует
  новой проверки того же viewer перед возвращением прав записи.
- Enrollment сохраняет PENDING/backoff при APIError; старше900s допускается только
  доказанно после armed_at современной привязки, неизвестная дата не подменяется.
- Store/DeliveryChains ограничивают UNKNOWN и повторные отправки конкретным заказом;
  глобальная блокировка неизвестного расхода Stars находится в GiftService.
- Notifications уже обслуживает приоритетные новые события раньше старых edits,
  использует квоты для обычных событий; отправка после timeout не повторяется.
- ChatCards уже хранит один physical message_id на account/owner/chat_id, историю,
  закрытие/повторное открытие, дедупликацию и unknown send; не строить второй чат.
- Finance уже использует общий finance:{owner}:withdrawal, Decimal и provenance.
  Требуется объединить UX, не заводить второе хранилище/нулевые неизвестные комиссии.

## Приёмка и ограничения

Все строки SELLER_AUTONOMY_REWORK_SPEC.md обязательны. До окончательной фазы
непроверенные строки — NOT VERIFIED, а не PASS по одному source lookup.
Старые инварианты подтверждены базовым2133/CI, новая версия должна пройти всё
повторно. Live-сеть, фактические два товара и SLA ещё не проверены в этом запуске.
Никаких рабочих БД/секретов в тестах; обслуживание двух лотов отдельно после кода.
