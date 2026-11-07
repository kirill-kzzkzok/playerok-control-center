# FULL BOT UX AUDIT — проверяемая матрица

База:83d4462, финансовые исправления36e25af/48eba29 сохранены. Источник итоговой регрессии:c7e6af401bef159143a6ebbb2a9e88a755208625. Без субагентов и без изменений рабочей SQLite/.env/сессий/Happ. Реальные платежи, отправки, публикации и изменения лотов не выполнялись.

**Офлайн-приёмка: PASS. Полная эксплуатационная/CI-приёмка пока NOT VERIFIED.** Матрица отражает адресные RED/GREEN и существующие production journeys. Итог:2592passed/0failed/0errors/0skipped на одном SHA; первоначальные11 падений не скрыты. ALREADY_OK означает проверенный путь без ненужного исправления, а не доверие старому отчёту.

## Все33+5 требований

Пути UI-модулей относятся к app/telegram, services — к app/services, тестов — к tests. Полные декларации:UX_BUTTON_MAP.md. Все named journeys используют изолированную SQLite и контролируемые транспортные границы.

| ID | Требование | Статус адресной проверки | Причина/результат | Код | Доказательство | Коммит |
|---|---|---|---|---|---|---|
| 1 | Создание автоответа | FIXED | Четыре ввода и подтверждение заменены двумя вводами; правило остаётся выключенным. Отдельное разрешение будущей отправки сохранено. | auto_replies_panel.py | test_full_ux_auto_reply_edit.py; test_auto_replies.py | d9366d3 |
| 2 | Понятные названия выдачи | FIXED | В существующем обслуживании: история выдачи, доставленные заказы, ход выполнения и настройки товаров. Технический движок сохранён. | home_panel.py | test_full_ux_remaining_presentation.py | d5d35a1 |
| 3 | Сохранённые настройки Playerok | FIXED | paid_reply/mark_sent показаны независимо от текущей готовности; STOP не превращает сохранённое true в false. | platform_panel.py | test_full_ux_saved_intent.py; test_playerok_delivery.py | ab7fa5f |
| 4 | Настройки при публикации | FIXED | Из канонической конфигурации взяты сообщения, completion, inheritance, refund и platform reply. Явный false существующего товара сохраняется. Новые товары сохраняют стандарт, включая alternate publish entry. | services/listing_launch.py | test_full_ux_launch_source.py; test_full_ux_new_launch_defaults.py; test_final_launch_source_intent.py | c0ed4ba; b241726 |
| 5 | Остановка Gifts | ALREADY_OK | Реальный GiftService.stop останавливает канонические товары и проекции атомарно. Не снимает STOP при просмотре. Современные маршруты ведут в одну карточку. | gifts/service.py; gifts_panel.py | test_full_ux_existing_and_stop.py; test_unified_delivery_controls.py | 27544fb; 77c2e81 |
| 6 | Возврат из вывода средств | FIXED | Выбор и обновление способа возвращают в расчёт исходного товара; глобальный вход остаётся профилем. | finance_panel.py | test_full_ux_messages_finance.py | a436f21; ffe5cee |
| 7 | Один импорт склада | FIXED | Кнопка заказа делегирует StockPanel. Старое подтверждение использует Inventory и постоянный scope, без отдельного сырого item_id-пула. | orders_panel.py; services/order_management.py | test_full_ux_shared_stock.py; test_order_management.py; test_fix_acceptance.py | b0b6519 |
| 8 | Автоматическая статистика перевыставлений | FIXED | Группировка только по owner/account/подтверждённым алиасам UUID. Название не доказательство. Ручные неоднозначные группы сохранены. | services/statistics.py | test_full_ux_statistics_groups.py; test_global_ux_finance.py | aa39091 |
| 9 | Понятное продвижение и разрешения | ALREADY_OK | Текущий экран объясняет условие позиции/период, cooldown и бюджеты; выбранные товары и причины по ним. Разовая покупка отделена от автопокупок. Неизменные разрешения не восстанавливаются/не сбрасываются просмотром. | promotions_panel.py; paid_promotions_panel.py | test_manual_premium.py; test_simple_promotion_controls.py; test_paid_promotions.py; test_full_ux_saved_intent.py | Существующий механизм + b241726 |
| 10 | Тексты привязки | FIXED | Подтверждение кода не названо полным описанием. Ожидающая операция — изменение лота, сверка — без повторной отправки. Критические конфликты остались. | logical_products_panel.py | test_full_ux_binding_language.py; test_final_critical_edits.py | 3e60502; 4f69f8c |
| 11 | Изменение одного поля автоответа | FIXED | Одно поле/один ввод, остальные значения и enabled сохранены. CAS не позволяет пережить конкурентный STOP с восстановлением старого true. | auto_replies_panel.py | test_full_ux_auto_reply_edit.py | d9366d3 |
| 12 | Глобальное продвижение из публикации | ALREADY_OK | Текущий путь явно предупреждает об ОБЩИХ настройках и требует отдельного применения; отказ ничего не меняет. Не молчаливая запись из товара. | launch_panel.py | test_listing_launch.py::test_global_budget_proposal_decline_leaves_policy_untouched; test_dispatcher_premium_budget_schedule_saved_globally_before_launch | Существующий механизм |
| 13 | Фоновая синхронизация | FIXED | Кнопка только сохраняет существующий catchup request и будит Synchronizer; не ждёт полного обхода. | router.py | test_full_ux_local_views.py | 3e60502 |
| 14 | Старые подтверждения Gifts | FIXED | Известный товар открывает текущую логическую карточку; старая финансовая возможность не применяется. Неизвестный товар не угадывается. | gifts_panel.py | test_full_ux_gift_redirect.py; test_unified_delivery_controls.py | 77c2e81 |
| 15 | Запомненный фильтр | FIXED | Явно pinned owner-фильтр восстанавливается с checked_filters; неприкреплённая старая настройка не скрывает активные товары. | router.py | test_full_ux_filters.py; test_global_ux_finance.py; test_unified_drafts_tab.py | b06dab5; 6ca5e7f |
| 16 | Проблемные оплаченные заказы на главной | FIXED | main_menu ранее игнорировал attention. Одна контекстная ссылка с точным количеством из существующего paid_issues. Обычная главная по-прежнему шесть действий. | home_panel.py; keyboards.py | test_full_ux_remaining_presentation.py; test_final_paid_attention.py; test_canonical_waiting_state.py | d5d35a1 |
| 17 | Несохранённые настройки и склад | FIXED | Navigation хранит только editable values/id/revision/account. Назад создаёт новый nonce; arm_pending и финансовые подтверждения не восстанавливаются. | navigation.py; automation_panel.py | test_full_ux_draft_navigation.py; test_logical_navigation.py; test_logical_panel.py | 976e37e |
| 18 | Открытие публикации без тяжёлой проверки | FIXED | Сохранённый экран открывается локально. Бесплатный итоговый запуск делает свежий PREPARE перед существующим COMMIT. Платный тариф виден и проверяется отдельно. | launch_panel.py; services/listing_launch.py | test_full_ux_launch_local.py; test_launch_fault_matrix.py; test_manual_premium.py | 12cbcfa; b241726 |
| 19 | Кнопка остановки уже остановленных Gifts | ALREADY_OK | Реальный общий STOP выключает product и gift projections; текущая карточка предлагает Включить, просмотр не снимает глобальный STOP. | logical_products_panel.py; gifts/service.py | test_full_ux_existing_and_stop.py; test_final_diagnostic_actions.py | 27544fb |
| 20 | Количество при массовой остановке | FIXED | Include отдельно от exclude; область all описана отдельно. Локальный STOP и существующая CAS-проверка подтверждения без API. | stop_confirmation.py | test_full_ux_saved_intent.py; test_final_mass_stop_confirmation.py | ab7fa5f |
| 21 | Цена и скидка | FIXED | Видны base/sale/текущая скидка. Проверенный API-контракт меняет sale price и снимает скидку; rawPrice-мутация не выдумана. | router.py; services/discounts.py | test_full_ux_remaining_presentation.py; test_discounts.py; test_full_preview_ui.py | d5d35a1 |
| 22 | Себестоимость новых оплаченных заказов | FIXED | Датированная явная стоимость сохраняется один раз в order_costs при ingest PAID; logical UUID переносит её на проверенный relist. Старые/недатированные/конфликтующие заказы не получают выдуманную стоимость. | services/order_finance.py; services/orders.py; finance_panel.py | test_full_ux_cost_snapshot.py; test_order_finance.py | 063b05b |
| 23 | Склад без ожидания Playerok | FIXED | Владелец/аккаунт/логический scope подтверждены локально; просмотр и импорт не ждут сетевой готовности. Неизвестный не связанный товар всё ещё проверяется. | stock_panel.py; services/inventory.py | test_full_ux_local_views.py; test_stock_ui.py | 3e60502 |
| 24 | Пустой склад и доступ к единицам | FIXED | Нет категорий с нулём, пустой склад короткий; история ненулевых/карантинных статусов сохранена. Данные единицы доступны только владельцу с пагинацией и связью заказа. | stock_panel.py | test_full_ux_local_views.py; test_stock_ui.py | 3e60502 |
| 25 | Локальная карточка выдачи | FIXED | Существующий same-account cache/SQLite snapshot, время и устаревание обозначены; безопасная фоновая заявка через существующий readiness batch. Критический preflight свежий. | logical_products_panel.py; services/product_readiness.py | test_full_ux_local_views.py; test_readiness_batch.py | 3e60502; 74e073a |
| 26 | История переписки | FIXED | Существующие токены older/newer получили видимые кнопки, курсоры и безопасные проверки не переписаны. | chat_panel.py | test_full_ux_messages_finance.py; test_chat_panel.py | a436f21 |
| 27 | Вложения переписки | FIXED | Видимый переход к существующему media viewer; карточка уведомления содержит безопасные ссылки. Текст/URL escaped, чужой URL отфильтрован. | chat_panel.py; chat_cards.py | test_full_ux_messages_finance.py; test_chat_panel.py | a436f21 |
| 28 | Контекст после обычной ошибки | FIXED | Callback/input errors дают один безопасный родитель в том же сообщении; не replay commit/arm/toggle. Read-only error сохраняет исходный экран. | router.py | test_full_ux_remaining_presentation.py; test_ux_followup.py; test_message_scoped_navigation.py | d5d35a1 |
| 29 | Галерея без засорения | ALREADY_OK | Navigation уже редактирует media в исходном сообщении. Photo→text заменяет/удаляет старую панель, потому что Telegram не меняет тип media на text. | navigation.py; item_panel.py | test_item_panel.py; test_full_ux_messages_finance.py; test_message_scoped_navigation.py | Существующий механизм |
| 30 | Явный режим совпадения автоответа | FIXED | Exact по умолчанию для старых правил. Contains выбирает владелец, сохраняется атомарно вместе с revision; guard/лимиты/возраст/UNKNOWN не ослаблены. | services/auto_replies.py; auto_replies_panel.py | test_full_ux_auto_reply_edit.py; test_auto_replies.py | d9366d3 |
| 31 | Объяснение пропуска старого сообщения | FIXED | Matching сообщение с задержкой >5 минут записано SKIPPED/SYNC_DELAY и понятно объяснено в существующей истории. Старые ответы не рассылаются. | services/auto_replies.py; auto_replies_panel.py | test_full_ux_auto_reply_edit.py; test_auto_replies.py | d9366d3 |
| 32 | Профиль без лишних сообщений | ALREADY_OK | ProfilePanel использует общий Navigation PanelMessage. Проверенный профиль не инициирует повторную проверку при простом просмотре. | profile_panel.py; navigation.py | test_stability_profile_view.py; test_ux_followup.py; test_message_scoped_navigation.py | Существующий механизм |
| 33 | Понятные разрешения запуска | FIXED | Объяснены недоступные действия, невозможность включить env-флаги из бота, точные параметры и необходимость перезапуска. Защиты сохранены. | item_panel.py | test_full_ux_remaining_presentation.py; test_final_diagnostic_actions.py | d5d35a1 |
| N1 | Подключение существующего лота | ALREADY_OK | Исходный server listing → единая настройка → Outbox code edit → запуск; ни копии, ни смены UUID/истории. Три способа проверены из Dispatcher. | logical_products_panel.py; services/logical_products.py | test_full_ux_existing_and_stop.py; test_universal_delivery_e2e.py | 27544fb |
| N2 | Одна система Gifts | FIXED | Современные и старые известные callbacks открывают один LogicalProduct. Startup legacy projection идемпотентна; историческое исполнение не удалено и новые gift: bindings не создаются современным мастером. | gifts_panel.py; logical_products_panel.py | test_full_ux_existing_and_stop.py; test_full_ux_gift_redirect.py; test_final_modern_gift_creation.py | 77c2e81; 27544fb |
| N3 | Устаревшие быстрые ответы | ALREADY_OK | Старые quickreplies/quick/replypreview — безопасный read-only redirect к чатам, не кнопки отправки. Фразовые автоответы сохранены. | router.py; auto_replies_panel.py | test_full_ux_messages_finance.py; test_auto_replies.py | a436f21; d9366d3 |
| N4 | Одна карточка диалога | FIXED | Несколько сообщений обновляют тот же message_id, время/текст/вложения; Ответить/Закрыть. Existing CAS/NOT_DISPATCHED/UNKNOWN/restart применяются; удалённый текст не показывается как новый. | chat_cards.py | test_full_ux_messages_finance.py; test_fix_chat_card_recovery.py; test_fix_observability.py | a436f21; d0c3cdd |
| N5 | Единый STOP | ALREADY_OK | Локальные канонические stop, правильные включить/остановить и массовая CAS-confirmation с фактическим scope. Отсутствие API не запрещает STOP; никакого auto financial re-arm. | stop_confirmation.py; logical_products_panel.py | test_full_ux_existing_and_stop.py; test_full_ux_saved_intent.py; test_final_mass_stop_confirmation.py | ab7fa5f; 27544fb |

## Реальные регрессии и RED/GREEN

- Сохранённый intent Playerok и count mass STOP: RED2 → GREEN38/34.44s.
- Локальные карточки/склад/синхронизация: RED Dispatcher с3/6s API и offline stock → GREEN53/46.50s.
- Переписка/вложения/возврат финансов: RED3 → GREEN38/41.03s. Дополнительный refresh return RED1 → GREEN11/13.71s.
- Редактор автоответа: RED3 (STOP уже защищён) → GREEN36; explicit contains и объяснение задержки RED2 → GREEN38/24.61s.
- Pinned filter: RED1 → GREEN13/12.90s.
- Старое Gift подтверждение: RED2 → GREEN21/25.84s.
- Наследование saved launch config: RED2 → GREEN58/38.97s.
- Медленное открытие launch: RED15.093s при5 чтениях по3s → GREEN44/29.35s.
- Несохранённая выдача → Stock → Back: RED1 → GREEN31/21.34s, новый nonce без arm_pending.
- Общий склад из заказа: RED1 → GREEN35/27.55s, одинаковый постоянный scope.
- Статистика native relist: RED1 → GREEN47/50.28s.
- Датированная себестоимость будущего заказа: RED1 → GREEN25/23.69s, включая restart и real relist worker.
- Pending lot wording: RED1 → GREEN18/25.18s.
- Пять оставшихся presentation defects: RED5/33.52s. После полного исходного прогона адресный профиль91/61.80s прошёл все11 падений, новые5 presentation cases и alternate new launch inheritance RED1/1pass13.91s.

Исходный полный прогон ffe5cee: **2572 passed,11 failed,0 errors,0 skipped** из2583. Артефакты data/full-ux-initial-{0..11}.xml/-manifest.json и data/full-ux-final-{0..11}.txt сохранены. Исправлены реальные дефекты свежести cache/title/status, stale DRAFT, сохранения PREMIUM choice/revision при отказе чтения и deleted-message rendering. Старые тесты менялись только при документированном контракте: два ввода вместо четырёх; pinned-фильтр действительно восстанавливается; cached card не требует API. No-send/owner/STOP/финансовые проверки сохранены. Старые stock boundary fixtures дополнены get_item, не подменой бизнес-логики.

## Измерения скорости

Условия: один production Dispatcher, одна временная SQLite, эмулятор Telegram без сетевой задержки,20 последовательных нажатий (товар/выдача/склад/home/settings по4раза), актуальный owner/account cache. База55e4b24 загружена в отдельные классы в тестовом процессе; рабочие файлы не откатывались. Искусственная задержка только API-boundary, одинаковая до/после. Сам benchmark:data/full_ux_benchmark.py,4passed/125.32s, JSON data/full-ux-benchmark-{before,after}-{3,6}.json.

| API задержка | Версия | Чтений | total p50/p95/p99 мс | ACK p95 мс |
|---|---|---|---|---|
|3s|До|12|3018.77 /3035.85 /3035.85|0.24|
|3s|После|0|12.75 /20.50 /20.50|0.24|
|6s|До|12|6018.76 /6033.94 /6033.94|0.31|
|6s|После|0|13.83 /20.04 /20.04|0.30|

Это **не** реальные сетевые SLA Playerok/Telegram. В JSON отдельно ACK/FSM/navigation/SQLite/Playerok/Telegram, отсутствующие инструментированные стадии отмечены samples0, а не ложным нулём. При20 отсчётах p99 имеет ограниченную статистическую точность. Финальный повтор after наc7e6af4:2passed/16.42s; шёл одновременно с четырьмя pytest-процессами, поэтому это дополнительная проверка при конкуренции за CPU, не замена исходного сопоставимого замера. Существующий test_api_read_resilience дополнительно проверяет50лотов,20локальных кликов и5карточек во время зависшей истории на настоящем SafeAccount/ThreadCalls. Не добавлены HTTP-клиенты/очереди/параллельные финансовые разрешения.

## Действия владельца и навигация

- Главная:6 обычных действий; только при настоящих оплаченных проблемах одна контекстная ссылка с числом, как прямо требовал пункт16.
- Автоответ: создание4ввода+localsave →2ввода; включение будущих отправок остаётся явным. Правка1поля: весь мастер+повторное arm →выбор поля+1ввод, intent не сброшен.
- Stock из заказа: отдельный order importer →тот же product importer; один preview/commit, duplicate confirmation не добавляет единицы.
- Launch: просмотр больше не является проверкой readiness. Для бесплатного запуска нет новой обязательной кнопки проверки; итоговое действие само делает PREPARE. Платная цена/полномочия остаются видимыми и свежими.
- Фильтр, provider return, старые Gift callbacks и Back к unsaved values больше не требуют заново искать/настраивать товар.
- Индивидуальные message_id/FSM scopes, obsolete capabilities, media replacement и фоновые ChatCards не переписывают соседние сообщения.
- UX_BUTTON_MAP:460 деклараций вместо451 базовых; это раскрытие требуемых существующих pager/media/field/unit действий и контекстного alert, **не**460 доказанных вручную сценариев. Технические детали остаются в существующей диагностике.

## Сквозная проверка

| Цепочка | Production доказательство |
|---|---|
|Создать/настроить/публиковать Gift→PAID→receipt→completion→restart|test_final_acceptance_e2e; test_listing_launch; test_launch_fault_matrix|
|Stock/text/native/manual creation и допустимое завершение|test_universal_delivery_e2e; test_playerok_delivery; test_launch_automatic_defaults; test_final_platform_creation|
|Существующий server lot→[XXX]→первый запуск без копии|test_full_ux_existing_and_stop; test_universal_delivery_e2e|
|Перевыставление UUID/склад/история/конфликт кода|test_universal_delivery_e2e; test_full_ux_statistics_groups; test_full_ux_cost_snapshot|
|Последняя единица/конкурентные оплаты/50лотов+заказов|test_final_paid_load; test_fifty_product_autonomy; test_stability_reserved_orders|
|Пустой склад→native pause→пополнение→resume; отказ снять|test_native_stock_visibility|
|Обрыв/предпроверка/UNKNOWN/restart/no duplicate платеж|test_final_acceptance_e2e; test_premium_receipt_fix; test_manual_premium|
|Один диалог/несколько сообщений/slow5s/send loss/restart|test_full_ux_messages_finance; test_fix_chat_card_recovery; test_fix_observability|
|Независимые карточки/Back/stale callbacks|test_message_scoped_navigation; test_logical_navigation; test_full_ux_draft_navigation|
|Безопасное ожидание данных и реальные оплаченные проблемы|test_canonical_waiting_state; test_final_paid_attention; test_automatic_delivery_preflight|
|Миграции/backup/restore/WAL|app.selfcheck на временных данных + полная регрессия миграций/backup|

## Остаточные ограничения

1. Реальная эксплуатационная скорость и физические обрывы Happ не измерялись в этом UX-запуске. Офлайн-тесты не доказывают автономность24/7 на конкретном Windows/VPN.
2. Неизвестный платёж/отправка без точного доказательства остаются в карантине. Старый PREMIUM62018 не изменялся; требуется отдельное согласие на описанную в PREMIUM_RECEIPT_FIX.md сверку рабочей SQLite.
3. Старые себестоимости без датированного owner declaration не становятся историческими автоматически. Новые значения сохраняются для будущих продаж; неизвестные суммы не заменены нулями.
4. rawPrice редактирование не реализовано: подтверждённый контракт обычного продавца позволяет sale price/discount. Возможности другого роли/API требуют отдельного доказательства, не выдуманного поля mutation.
5. При удалении/недоступности исходного Telegram-сообщения или неизвестном send нельзя гарантировать обновление именно его без подтверждения. Существующая защита от duplicate send остаётся важнее красивого экрана.
6. Изменённые финансовые условия, новый состав выдачи, обязательный отчёт Playerok, реальный STOP и неразрешимый конфликт по-прежнему требуют участия владельца.
7. Инвентаризация деклараций и тесты динамических factory не эквивалентны ручному live-нажатию каждой комбинации. Живые товары [TXE]/[VPM] не менялись и не перезапускались.

## Итоговая регрессия

Завершена на**c7e6af401bef159143a6ebbb2a9e88a755208625**. **2592passed /0failed /0errors /0skipped**.12непересекающихся shards, max4параллельно;2592уникальных manifest nodeids и2592уникальных XML testcase pairs, общий SHA/число collection/exit0 проверены. Не обнаружены lost-task, unretrieved exception, unawaited coroutine, closed-loop или RuntimeWarning в финальных outputs.

Ruff check, Ruff format --check (488files), compileall app/playerokapi/tests/tools, pip check, selfcheck —exit0 на том же источнике. Selfcheck:OFFLINE_TEMPORARY_DATA/OK, external_requests0, миграции старой схемы и reopen/restore backup проверены на временных данных. Отдельные package/runtime проверки не выдаются за живые платежи.

Артефакты:data/full-ux-acceptance.json; data/full-ux-accepted-{0..11}.txt/.exit/.xml/-manifest.json; data/full-ux-accepted-{ruff,format,compile,pip,selfcheck}.txt. Четыре части полного набора дают обычные PytestWarning о record_property/xunit2; они не являются skipped/xfail и не скрывают провалы. Повторный профиль и исходные неуспешные прогоны сохранены. После принятого источника изменяются только Markdown-отчёты/инвентаризация; тождество production-кода и тестов проверяется черезgit diff.

GitHub Actions после push635cd2ba66551514c94696f3036596f0022e779b: [run42271543076](https://github.com/kirill-kzzkzok/playerok-control-center/actions/runs/42271543076), conclusion=failure. Все3jobs имеют steps=[], проверки не запускались. Annotation GitHub: recent account payments failed or spending limit needs increase (Billing & plans). Это **BLOCKED_CI**, не падение теста и неSUCCESS. Локальная приёмка не подменяет CI или доказательство24/7 в реальной сети.


## Дополнительная проверка после первого полного прогона

- Контрольный6ca5e7f:2587passed/3failed/0errors/0skipped из2590; data/full-ux-checkpoint-*.xml/-manifest.json + data/full-ux-release-*.txt. Два падения — изменившийся видимый заголовок и безопасный контекст возврата, финансовые/duplicate проверки сохранены. Одно —4s lane deadline при12 одновременно выполнявшихся pytest. Тот же production module с настоящим90s RPC timeout и неизменным4s ограничением прошёл в профиле22/122.01s. Таймауты не увеличивались, тест не исключался.
- Итоговый полный набор запускается max4процессами,12непересекающихся существующих shards. Нет selective retry/xfail/deselect; все2592 collected cases должны завершиться.
- Требование6.7 диагностики: RED2/18.20s → GREEN25/32.84s. В существующем app.telemetry сохраняются allowlisted callback_group/action без ID, static method +attempts/retries, numeric HTTP duration +старые ACK/FSM/locks/SQLite/Telegram. Медленная warning выбирает максимум только среди numeric durations, metadata не вызывает исключение. Поля HTTP не выдуманы из общего времени SDK. Проверка приватного token/item string и настоящего SafeAccount request на MockTransport:test_full_ux_trace.py. Отсутствующая инструментированная стадия остаётся unknown, а не0.
- Статический whitelist не является новой системой логирования/настройкой: нужен, чтобы не записывать динамический callback/PII. Ноль новых бизнес-состояний, таблиц, workers, клиентов или финансовых разрешений.
- Короткий run179d983 остановлен до приёмки для исправления Ruff SIM905 в неизменяющем поведение представлении whitelist. Частичные full-ux-sealed-* не засчитаны; финальный источникc7e6af4 имеет Ruff/format exit0.


Финальный повтор скорости наc7e6af4 при четырёх фоновых pytest-процессах:20кликов/0API, delay3s p50=14.94ms,p95=p99=21.21ms,ACK p95=0.39ms; delay6s p50=16.18ms,p95=p99=21.36ms,ACK p95=0.82ms. Первоначальная таблица выше — предшествующий сопоставимый замер; финальные after JSON обновлены этим повтором. Показатели сетевых сервисов по ним не утверждаются.

Предварительная проверка GitHub: origin/master8e9a83d является предком локального HEAD, чужие изменения не откатывались. Старый Actions run42234855377 не запускал jobs: GitHub сообщил failed recent account payments/spending limit. Это не зелёный CI и не ошибка pytest; финальный push/run нужно проверить отдельно.


## Итоговый вывод

Адресные исправления и офлайн-приёмка33+5 требований завершены. **Офлайн2592/0/0/0; выпуск с подтверждённым CI заблокирован внешней оплатой GitHub.** Реальные эксплуатационные SLA/24×7 не подтверждены этим запуском. Финальный принятый runtime/test SHA:c7e6af401bef159143a6ebbb2a9e88a755208625; следующие коммиты меняют только Markdown-отчёты/route inventory. Разница635cd2b против принятого источника — FULL_BOT_UX_AUDIT.md,PROGRESS.md,UX_BUTTON_MAP.md; идентичность production-кода/тестов проверена. Полный SHA отчётного HEAD и финальная ссылка Actions доступны в сообщении о завершении.

Применение: обычный перезапуск приложения для загрузки нового кода. Агент не останавливал рабочий магазин и не выполнял операции с реальными товарами. Не требуется пересоздавать товары или очищать историю/резервы/UNKNOWN.


## Финальная проверка: 18 направлений исправлений

Новая приёмка по вложению 736f6a89, база `7d95fd5`. Предыдущие 2592 PASS не заменяют проверку этих требований. Рабочая SQLite исследована только `mode=ro`, `query_only=ON`, `total_changes=0`; платежей и внешних мутаций нет. Итог полной регрессии новой версии приведён ниже.

| № | Модули в app (файлы .py) | Проблема, причина и исправление | Проверенное поведение и тест | Результат | Статус | Коммит |
|---|---|---|---|---|---|---|
|1|telegram/launch_panel, promotions_panel, item_panel, stock_panel, operation_results, logical_products_panel|Перечисленные непонятные подписи в reachable advanced/stock/result/launch заменены; несохранённые переключатели помечены как форма, прогноз как расчёт|RED2 → GREEN62/24,99с с настоящими Dispatcher/FSM и сохранённой policy/draft; callback IDs и денежные действия неизменны. Полная регрессия подтверждена ниже|PASS|FIXED|1df9bec|
|2|telegram/launch_panel, promotions_panel|Глобальные поля выглядели как настройки лота; ложный текст отзывал согласие после смены частоты. Теперь область всего магазина видна до ввода/сохранения, реальный финансовый контракт не изменён|Dispatcher cancel/back сохраняет исходный draft/policy; профиль62 с promotion isolation/grants/STOP/UNKNOWN; никаких расходов|PASS|FIXED|1df9bec|
|3|services/product_readiness, telegram/item_panel|ProductReadiness: смена версии после подтверждённой правки вызывала GET в UI; теперь локальная карточка и фоновая сверка|test_final18_item_cache: настоящий Dispatcher → Outbox SUCCEEDED → API с задержкой6с → карточка <0,7с; restart и late-read fencing. RED1 → GREEN51 профиль|PASS|FIXED|4349dae|
|4|telegram/router|Recovery allowlist не охватывал Gifts/продвижение/операции; SQLite ошибке давал только Home. Теперь безопасный текущий display route из pre-save context|RED5 → GREEN33/14,27с: Dispatcher expired capabilities из4контекстов, SQLite Save failure, ввод остаётся, policy/платежи неизменны, no commit/arm tokens|PASS|FIXED|fe3bf73|
|5|services/product_readiness, stock_visibility, telegram/item_panel|ProductReadiness: снимок, полученный во время мутации, не видел позднее подтверждение. Снимок теперь запоминает незавершённые операции и применяет только квитанцию SUCCEEDED; GET ограждён версией до чтения|test_final18_item_cache: RED гонки реального Dispatcher/Outbox и матрица edit/edit_fields/publish/PREMIUM/discount/stock_pause/resume, UNKNOWN не меняет отображаемые поля. Матрица использует сохранённые квитанции, а не реальные покупки|PASS|FIXED|b65766e, 99e28e6|
|6|gifts/service, telegram/gifts_panel|GiftsPanel считал паузу раньше STOP, enabled-проекция оставляла кнопку остановки; теперь существующий runtime_reason и STOP → риск → PAUSE|Dispatcher RED3 → GREEN86/36,82с с финансовыми Gifts/unified controls; флаги и Outbox не изменяются от открытия|PASS|FIXED|ef4a67b|
|7|services/logical_products, workers/logical_products|VPM: старая причина утрачена. Воспроизведён отдельный реальный дефект: временная проверка аккаунта становилась постоянным конфликтом; теперь retryable ожидание. Причины новых конфликтов типизированы|Readonly VPM: событие18:07UTC без кандидата, ошибка уже NULL после сверки19:13UTC. RED3 → GREEN37/19,29с: critical mismatch, gate race, сеть/неполные данные без ложного конфликта|PASS код / NOT VERIFIED старая причина|FIXED код; BLOCKED историческое доказательство|8da3295|
|8|workers/notifications, telegram/logical_products_panel|Уведомление выбирало первый alias через LIMIT1. Теперь точный кандидат из события либо существующий список связей конкретного UUID, неизвестность обозначена|Dispatcher и реальные Notifications RPC, GREEN37; точный item:candidate, неизвестный кандидат без случайного старого лота/сетевого запроса|PASS|FIXED|8da3295|
|9|services/playerok_connection, product_readiness, telegram/transport, workers/logical_products|playerok_connection, transport, logical_products, product_readiness: краткие сбои зашумляли консоль. Временные ошибки уходят в DEBUG, повторяющиеся группируются, восстановление видно для показанного инцидента|test_temporary_viewer_outage_logs_one_warning_and_one_recovery, test_transport_reports_only_persistent_connection_incident. Смена сетевого кода и restart viewer. test_managed_viewer_transport_failures_preserve_incident_accounting: десять ошибок через SafeAccount дают одну тревогу и одно восстановление; успех другого Telegram-метода не закрывает инцидент|PASS|FIXED|08620ca, 2b43203, 99e28e6, 0a102b5, 732c0ba|
|10|workers/notifications|Notifications: устаревшее предупреждение с безопасным retry оставалось PENDING. Закрытие меняет только PENDING или создаёт итоговую запись, не переопределяя PROCESSING/UNCERTAIN|test_obsolete_candidate_and_resolved_incident_are_not_sent_after_restart: RED PENDING, GREEN SKIPPED и один RPC актуального кандидата; test_notifications, test_final_worker_cycles|PASS|FIXED|8da3295, 9295afc|
|11|telegram/input_cleanup, router|InputCleanup удалял отклонённый ввод; теперь удаляет только после durable acceptance|test_final18_input + input_cleanup/input_target/navigation/targeted UX: RED3 → GREEN40, ошибок ввода не скрывает, секреты не копирует|PASS|FIXED|cf3a5db|
|12|services/order_management, telegram/home_panel, orders_panel, presentation|Home не учитывал длительное PENDING: теперь существующий раздел важных заказов показывает ожидание ≥180с с первого PAID, причину и следующий retry; заказ показывает те же факты|RED2 → GREEN56/34,67с: склад и текст, неполные flags, нет резерва/отправки, restart, восстановление через production workers, выдача ровно один раз|PASS|FIXED|2df7b3d|
|13|telegram/orders_panel, services/orders, workers/sync|OrdersPanel: GET перед локальной карточкой и отсутствие bound-account проверки → локальный собственный снимок с временем + существующая sync_requests|RED2 → GREEN49/21,12с: Dispatcher/restart/фоновый Synchronizer, чужой seller/account, свежий prepare при недоступной сети; без отправок|PASS|FIXED|02f2931|
|14|telegram/navigation_context, event_isolation|Единственная открытая форма терялась после чужой карточки|Один waiting_forms для navigation и isolation; Dispatcher/restart, RED → GREEN40. >1 форм остаются Reply-only|PASS|FIXED|cf3a5db|
|15|telegram/listings_panel, services/item_filters, product_readiness|ListingsPanel/item_filters/ProductReadiness: сетевое ожидание каталога. Известные страницы показываются из снимка; полный поиск продолжает существующий worker, по одной странице за цикл|test_dispatcher_reopens_known_catalog_page_during_outage: RED offline, GREEN снимок, 101 позиция, листание и поиск кода без RPC, исходный GET один; test_background_completion_does_not_shift_open_search_page RED: пропуск lot-10, GREEN: открытая страница остаётся на прежнем поколении|PASS|FIXED|99e28e6, a3a7a5c, 20b0245|
|16|telegram/finance_panel, services/financial_quotes, product_readiness|FinancePanel/financial_quotes/ProductReadiness: последовательные чтения скрывали сохранённый прогноз при сбое. Снимок по владельцу и аккаунту с временем, обновление ограничено существующим циклом|test_saved_profit_view_opens_without_playerok_reads: RED ошибка, GREEN старый расчёт без RPC, фоновый сбой и восстановление с новой ценой|PASS|FIXED|99e28e6|
|17|services/order_finance, telegram/finance_panel|OrderFinance/FinancePanel: первый CONFIRMED не получал снимок, нулевые прочие расходы не задавались. Учитываются датированные объявления владельца, сохранённые снимки неизменны|test_completed_sales_use_only_dated_owner_declarations и test_product_cost_snapshots_only_future_orders_and_survives_change_and_restart. 100 продаж, смена цены на 50-й, повторный ingest, старые/неизвестные даты и relist|PASS|FIXED|bba353d|
|18|services/playerok_connection, playerok/adapter|PlayerokConnection: поздний viewer мог отменить независимый отзыв авторизации. Введено ограждение результата по поколению. Сохранён fail-closed во время проверки|test_late_viewer_success_cannot_restore_revoked_identity RED→GREEN; test_runtime_read_identity и test_playerok_connection подтверждают продолжение чтений, сохранение резерва, восстановление и отказ при 401/403/смене аккаунта|PASS доказанные сценарии|BLOCKED сокращение окна без свежего подтверждения|539ccfa, 0a102b5|

Таблица разделяет подтверждённые исправления и ограничения. Контракты и последовательность RED/GREEN сохранены в PROGRESS.md. Исторический кандидат VPM и безопасное снятие запрета записи до завершения viewer остаются за границей доказанного результата.

Полный checkpoint `fe3bf736f81091121691eaf07612050a80aff399`: **2615 PASS / 5 FAIL / 0 ERROR / 0 SKIP**, 2620 уникальных тестов, 12 частей ≤4 одновременно. Падения не скрыты: недостающий safe-return iv:edit, два случая неполного seller, тексты home/STOP. Источники исправлены, исходные проверки сохранены: соседний профиль **76 PASS /35,23с**, exit0. Это ещё не финальная полная приёмка.


Сохранённый прогноз показывает прежние комиссии с датой снимка и оговоркой об актуальности. Складская выдача, отправка, публикация и деньги проходят прежние свежие проверки. Известный заказ и карточка товара проверены при искусственной задержке Playerok 6 с. Каталог и прогноз проверены при отключённом контролируемом API: повторное открытие делает 0 внешних чтений. У холодного каталога остаётся первое чтение одной страницы. У первого расчёта без снимка остаются чтения товара, комиссии, способа вывода и тарифа по необходимости.

Плановая проверка аккаунта временно закрывает запись и автоматизацию на время viewer, для которого действует граница 25 с. Если чтение завершилось временной ошибкой, новые опасные действия ждут следующей успешной проверки. Доказательства старой сессии дают продолжить допустимые чтения, включая заказы, но не дают права на новый платёж или выдачу. Гонка позднего ответа закрыта отдельным тестом. Производственные сетевые задержки и все комбинации одновременных операций в реальном магазине здесь не измерялись.

При смене себестоимости новые заказы получают новую сумму только при подходящей серверной дате. Уже сохранённые снимки не переписываются. Если поздно обнаруженная продажа старше последней декларации, а прежняя себестоимость для неё не зафиксирована, значение остаётся неизвестным. Ноль прочих расходов применяется только по датированному явному выбору владельца, отдельно от комиссий и размещения.


Офлайн замер после изменений на `99e28e6` проводился с 20 нажатиями на маршрут, контролируемыми Telegram и Playerok и четырьмя параллельными частями pytest. Подставленная задержка чтения Playerok составляла 6 с. За 140 нажатий интерфейс сделал 0 запросов Playerok. Отдельный viewer завершился по тайм-ауту за 25,04 с. В это время допустимые чтения оставались разрешены, платный guard отказал, сетевых мутаций не было.

| Маршрут | p50 / p95, мс | ACK p95, мс | Чтения Playerok из UI после |
|---|---:|---:|---:|
| Известная карточка товара | 66,94 / 88,88 | 0,263 | 0 |
| Товары | 88,98 / 99,66 | 0,266 | 0 |
| Возврат в список | 33,98 / 41,48 | 0,977 | 0 |
| Известный заказ | 64,19 / 95,51 | 0,309 | 0 |
| Сохранённый расчёт прибыли | 48,91 / 76,77 | 0,516 | 0 |
| Главная | 48,00 / 57,79 | 0,384 | 0 |
| Настройки | 34,49 / 40,49 | 0,511 | 0 |

В исходном коде открытие изменённого товара, списка и известного заказа ждало как минимум один сетевой ответ. Сохранённый расчёт тоже заново читал товар, а другие запросы зависели от доступности комиссий и тарифа. Для новых маршрутов сопоставимый базовый p50/p95 не измерялся. Предыдущие замеры на `c7e6af4` остаются выше в отчёте и не подменяют новую проверку. Подробные этапы нового офлайн замера доступны локально в `data/final18-performance.json`. Зафиксированные p99 отдельных первых нажатий достигали 804 мс под нагрузкой. Живой сетевой SLA этими числами не подтверждается.


Промежуточная полная регрессия `99e28e6251211a678a01a02690ea02487ee74f59` дала **2622 PASS / 13 FAIL / 0 ERROR / 0 SKIP** из 2635 уникальных тестов. Исправлены четыре регрессии совместимости листания без объекта аккаунта, свежий счётчик активных лотов, лишнее чтение после STOP worker и счётчик при восстановлении Telegram. Старые проверки, требовавшие повторного GET на каждом открытии поиска или тарифа, заменены проверками того же результата через фоновый цикл. Проверки отказа, истории расходов и отсутствия платной операции сохранены. Дополнительно устранены воспроизведённый пропуск позиции при публикации снимка и самоподавление предупреждения внутри реального адаптера viewer.

На исходниках `a3a7a5c4754070875b4fdcde152a639eb0bd6d5f` Ruff, Ruff format check (497 файлов), compileall, pip check и selfcheck завершились с кодом 0. Selfcheck использовал временные данные, подтвердил миграции 1–33 и повторное открытие SQLite backup. Внешних запросов 0. Повторный pytest собрал 2637 тестов. Прогон остановлен после подтверждения трёх проблем и не засчитывается: старое число на главной при частичном фоновом чтении (два сценария) и тест карточек, предполагавший 101 отправку за два ограниченных по времени цикла.

Профиль после исправлений `20b0245` и `9058337` дал **41 PASS / 0 FAIL**. Новое неполное наблюдение делает счётчик активных товаров неизвестным до полного подтверждения. Проверка 101 диалога теперь учитывает реальный бюджет цикла и требует прогресса, ровно 101 отправку без дублей и обновление последнего диалога. Лимит worker не изменён.


Финальная офлайн приёмка исходников `90583376038af017c6e351afa960d165624660b9` завершена **2637 PASS / 0 FAIL / 0 ERROR / 0 SKIP**. Все 12 непересекающихся частей вернули код 0, одновременно выполнялись не более четырёх. Сверены точные идентификаторы коллекции и XML в каждой части. В прогон вошли проверки старых callback, конкурентных заказов, 50 независимых лотов с потерянным подтверждением, STOP, карантина Stars/PREMIUM, миграций и восстановления после перезапуска. Ruff, Ruff format check (497 файлов), compileall, pip check и selfcheck завершились с кодом 0 на том же коде. Selfcheck использовал временную SQLite, подтвердил схему 1–33 и открыл резервную копию. Внешних запросов 0. Сводка локально в `data/final18-accept-90583376-result.json`.

Для старого события VPM рабочая база и сохранившийся журнал не связывают причину с конкретным кандидатом. На момент чтения ошибка уже отсутствовала после успешной сверки. Реальный магазин, платежи и сетевой SLA в этой приёмке не испытывались. Ранее проверенный [GitHub Actions](https://github.com/kirill-kzzkzok/playerok-control-center/actions/runs/42271746001) завершился до запуска заданий из-за GitHub Billing. Локальная приёмка отделена от статуса CI.

## Повторная проверка путей настройки товара

| Функция | Основной путь | Старые пути и границы |
| --- | --- | --- |
| Название, описание, фотографии, цена и характеристики | Товары → товар → Изменить | Кнопки истории возвращают в карточку объявления. |
| Способ выдачи, текст, складская политика и состав Gifts | Товары → товар → Выдача | Склад теперь открывает тот же экран для товара с привязкой и без неё. Старые Gift callback для привязанного товара ведут в действующие настройки. Старая кнопка Automation также ведёт туда для товара без сохранённой политики или подходящего черновика. |
| Настройки Playerok-native | Выдача → Выдаёт Playerok | PlatformPanel настраивает ответ и завершение после подтверждённой выдачи площадкой. Это отдельный способ выдачи с собственным контрактом. |
| Запас склада | Выдача → Склад | Склад меняет остаток. Правила выдачи редактируются из «Выдачи». |
| Telegram Gifts для магазина | Настройки → Личный Telegram | Баланс, подключение, общие лимиты, STOP и пауза действуют на магазин. Состав и лимиты конкретного товара остаются в его «Выдаче». |
| Автоподнятие для магазина | Продвижение → Дополнительные параметры | Экран запуска использует ту же форму. Старые кнопки бюджета и частоты открывают соответствующую группу, а старое предложение открывает форму без применения. Возврат синхронизирует сохранённые параметры с черновиком. |
| Разовое продвижение и тариф объявления | Товар → Продвижение | Это выбор тарифа конкретного объявления. Общий быстрый предел расходов сохраняет собственный контракт с пересчётом связанных лимитов. |
| Диагностика старых политик | Исторический раздел автоматизации | Редактор без постоянной привязки доступен только для уже сохранённой старой политики или подходящего черновика. Нажатие старой кнопки само по себе новый редактор не открывает. |

Глобальный STOP на экране настроек Gifts теперь определяет действие независимо от сохранённых проекций товара. Dispatcher-сценарии покрывают STOP с активной проекцией и комбинацию STOP + PAUSE + UNCERTAIN. Открытие настроек и шага подтверждения возобновления сохраняет STOP, паузу, задания, Outbox и проекции. Тестовый транспорт фиксирует ноль платежей и отправок.
