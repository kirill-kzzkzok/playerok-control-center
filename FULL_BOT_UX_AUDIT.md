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

Новая приёмка по вложению 736f6a89, база `7d95fd5`. Предыдущие 2592 PASS не заменяют проверку этих требований. Рабочая SQLite исследована только `mode=ro`, `query_only=ON`, `total_changes=0`; платежей и внешних мутаций нет. Финальная полная регрессия новой версии ещё не выполнена.

| № | Модуль / проблема → причина → изменение | Доказательство | Результат | Статус | Коммит |
|---|---|---|---|---|---|
|1|Перечисленные непонятные подписи в reachable advanced/stock/result/launch заменены; несохранённые переключатели помечены как форма, прогноз как расчёт|RED2 → GREEN62/24,99с с настоящими Dispatcher/FSM и сохранённой policy/draft; callback IDs и денежные действия неизменны. Полная регрессия ещё требуется|PASS профиль|FIXED|1df9bec|
|2|Глобальные поля выглядели как настройки лота; ложный текст отзывал согласие после смены частоты. Теперь область всего магазина видна до ввода/сохранения, реальный финансовый контракт не изменён|Dispatcher cancel/back сохраняет исходный draft/policy; профиль62 с promotion isolation/grants/STOP/UNKNOWN; никаких расходов|PASS|FIXED|1df9bec|
|3|ProductReadiness: смена версии после подтверждённой правки вызывала GET в UI; теперь локальная карточка и фоновая сверка|test_final18_item_cache: настоящий Dispatcher → Outbox SUCCEEDED → API с задержкой6с → карточка <0,7с; restart и late-read fencing. RED1 → GREEN51 профиль|PASS|FIXED|4349dae|
|4|Recovery allowlist не охватывал Gifts/продвижение/операции; SQLite ошибке давал только Home. Теперь безопасный текущий display route из pre-save context|RED5 → GREEN33/14,27с: Dispatcher expired capabilities из4контекстов, SQLite Save failure, ввод остаётся, policy/платежи неизменны, no commit/arm tokens|PASS|FIXED|fe3bf73|
|5|Инвалидация карточек после мутаций и ограждение поздних чтений|Расширен item_version; SUCCEEDED-only overlay. Профиль51 PASS; расширенная матрица UNKNOWN/всех видов ещё требуется|PASS частично|NOT_VERIFIED|4349dae|
|6|GiftsPanel считал паузу раньше STOP, enabled-проекция оставляла кнопку остановки; теперь существующий runtime_reason и STOP → риск → PAUSE|Dispatcher RED3 → GREEN86/36,82с с финансовыми Gifts/unified controls; флаги и Outbox не изменяются от открытия|PASS|FIXED|ef4a67b|
|7|VPM: старая причина утрачена. Воспроизведён отдельный реальный дефект: временная проверка аккаунта становилась постоянным конфликтом; теперь retryable ожидание. Причины новых конфликтов типизированы|Readonly VPM: событие18:07UTC без кандидата, ошибка уже NULL после сверки19:13UTC. RED3 → GREEN37/19,29с: critical mismatch, gate race, сеть/неполные данные без ложного конфликта|PASS код / NOT VERIFIED старая причина|FIXED код; BLOCKED историческое доказательство|8da3295|
|8|Уведомление выбирало первый alias через LIMIT1. Теперь точный кандидат из события либо существующий список связей конкретного UUID, неизвестность обозначена|Dispatcher и реальные Notifications RPC, GREEN37; точный item:candidate, неизвестный кандидат без случайного старого лота/сетевого запроса|PASS|FIXED|8da3295|
|9|Консоль временных сетевых сбоев|Новая адресная проверка ещё не завершена|NOT_RUN|NOT_VERIFIED|—|
|10|Старый кандидат с тем же кодом мог уведомляться после нового. Сверка текущего инцидента/revision/control перед отправкой; решённые SKIPPED|Исходный Notifications2df7b3d в изолированном процессе RED1: два RPC вместо одного. Новый код GREEN37: один актуальный RPC, restart/resolved без повторов. Остальные типы уведомлений требуют расширенной матрицы|PASS для relist|NOT_VERIFIED общая матрица|8da3295|
|11|InputCleanup удалял отклонённый ввод; теперь удаляет только после durable acceptance|test_final18_input + input_cleanup/input_target/navigation/targeted UX: RED3 → GREEN40, ошибок ввода не скрывает, секреты не копирует|PASS|FIXED|cf3a5db|
|12|Home не учитывал длительное PENDING: теперь существующий раздел важных заказов показывает ожидание ≥180с с первого PAID, причину и следующий retry; заказ показывает те же факты|RED2 → GREEN56/34,67с: склад и текст, неполные flags, нет резерва/отправки, restart, восстановление через production workers, выдача ровно один раз|PASS|FIXED|2df7b3d|
|13|OrdersPanel: GET перед локальной карточкой и отсутствие bound-account проверки → локальный собственный снимок с временем + существующая sync_requests|RED2 → GREEN49/21,12с: Dispatcher/restart/фоновый Synchronizer, чужой seller/account, свежий prepare при недоступной сети; без отправок|PASS|FIXED|02f2931|
|14|Единственная открытая форма терялась после чужой карточки|Один waiting_forms для navigation и isolation; Dispatcher/restart, RED → GREEN40. >1 форм остаются Reply-only|PASS|FIXED|cf3a5db|
|15|Список/поиск товаров выполняет сетевую пагинацию в UI|Новая адресная проверка ещё не завершена|NOT_RUN|NOT_VERIFIED|—|
|16|Расчёт прибыли ждёт последовательные внешние чтения|Новая адресная проверка ещё не завершена|NOT_RUN|NOT_VERIFIED|—|
|17|Историческая себестоимость и явный ноль прочих расходов|Новая адресная проверка ещё не завершена|NOT_RUN|NOT_VERIFIED|—|
|18|Периодическая проверка временно закрывает запись|Снять fail-closed без доказательств нельзя; новая adversarial-проверка границ ещё не выполнена|NOT_RUN|NOT_VERIFIED|—|

Это промежуточный проверяемый checkpoint, не объявление завершения всех18 требований или готовности24/7. Для продолжения использовать соответствующие контракты Final18 в PROGRESS.md. Последний прежний CI не стартовал задания из-за GitHub Billing; зелёный CI новой версии не заявляется.

Полный checkpoint `fe3bf736f81091121691eaf07612050a80aff399`: **2615 PASS / 5 FAIL / 0 ERROR / 0 SKIP**, 2620 уникальных тестов, 12 частей ≤4 одновременно. Падения не скрыты: недостающий safe-return iv:edit, два случая неполного seller, тексты home/STOP. Источники исправлены, исходные проверки сохранены: соседний профиль **76 PASS /35,23с**, exit0. Это ещё не финальная полная приёмка.
